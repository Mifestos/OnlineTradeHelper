# src/services/analytics/worker.py
import os
import sys
import json
import ssl
import time
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import redis
import psycopg2  # Синхронный клиент для Celery
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from celery import Celery

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from src.settings import REDIS_HOST, REDIS_PORT, DATABASE_URL
from src.services.analytics.models import ModelFactory
from src.services.analytics.optimiser import OptimiserFactory

celery_app = Celery(
    "trade_tasks",
    broker=f"redis://{REDIS_HOST}:{REDIS_PORT}/0",
    backend=f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
)

# Таймаут ожидания готовности streamer'а (в секундах).
# Должен быть с запасом перекрывать интервал первой свечи streamer'а:
#   - симуляция: 5 сек
#   - боевой стрим: ~60 сек
READY_WAIT_TIMEOUT = float(os.getenv("READY_WAIT_TIMEOUT", "90.0"))


def get_actual_cbr_key_rate() -> float:
    """
    Скачивает актуальную ключевую ставку с SOAP-сервиса ЦБ РФ.
    Метод KeyRateXML требует обязательные параметры fromDate и ToDate.
    В ответе ЦБ дата лежит в теге <DT>, ставка — в <Rate>.
    Записи сортируются по дате — берётся самая свежая.
    """
    url = "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx"

    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema"
               xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <KeyRateXML xmlns="http://web.cbr.ru/">
      <fromDate>{date_from}</fromDate>
      <ToDate>{date_to}</ToDate>
    </KeyRateXML>
  </soap:Body>
</soap:Envelope>"""

    req = Request(
        url,
        data=soap_body.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": '"http://web.cbr.ru/KeyRateXML"',
            "User-Agent": "Mozilla/5.0",
        },
    )

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urlopen(req, timeout=15, context=ctx) as response:
            xml_data = response.read()
            root = ET.fromstring(xml_data)

            kr_entries = []
            for elem in root.iter():
                if elem.tag.split("}")[-1] == "KR":
                    date_str = None
                    rate_str = None
                    for child in elem:
                        tag = child.tag.split("}")[-1]
                        if tag in ("DT", "Date") and child.text:
                            date_str = child.text
                        elif tag == "Rate" and child.text:
                            rate_str = child.text
                    if date_str and rate_str:
                        kr_entries.append((date_str, rate_str))

            if not kr_entries:
                raise ValueError("В ответе ЦБ не найдено ни одной записи с датой и ставкой")

            kr_entries.sort(key=lambda x: x[0])
            latest_date, latest_rate_str = kr_entries[-1]

            rate_val = float(latest_rate_str.replace(",", "."))
            actual_rate = rate_val / 100.0
            print(f"[CBR API] Актуальная ставка ЦБ на {latest_date}: {rate_val}% (доля: {actual_rate})")
            return actual_rate

    except Exception as e:
        print(f"[CBR API] Не удалось загрузить ставку ЦБ ({e}). Откат на дефолтные 14%.")
    return 0.14


def wait_for_live_data(tickers: list, timeout: float = READY_WAIT_TIMEOUT) -> bool:
    """
    Инженерное ожидание готовности streamer'а через Redis-флаги.

    Логика:
    1. Быстрая проверка — если флаг `streamer:ready:{ticker}` уже стоит,
       тикер готов, ждать не нужно.
    2. Если флага нет — worker блокирующе ждёт сигнала через BLPOP
       из очереди `streamer:ready_queue` с общим таймаутом.

    Возвращает True, если все тикеры готовы, False при таймауте.
    """
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    start = time.time()

    # Шаг 1: быстрая проверка — кто уже готов
    missing = {t for t in tickers if not r.exists(f"streamer:ready:{t}")}

    if not missing:
        print(f"[REDIS WAIT] ✅ Все тикеры уже готовы (флаги стояли)")
        return True

    print(f"[REDIS WAIT] Ожидание готовности: {missing} (таймаут {timeout}с)...")

    # Шаг 2: блокирующее ожидание сигналов через BLPOP
    while missing and (time.time() - start) < timeout:
        remaining_timeout = max(1, int(timeout - (time.time() - start)))
        # BLPOP блокирует до появления элемента или таймаута
        result = r.blpop("streamer:ready_queue", timeout=remaining_timeout)
        if result:
            _, ticker = result
            print(f"[REDIS WAIT] 📩 Получен сигнал готовности: {ticker}")
            missing.discard(ticker)

    if not missing:
        elapsed = time.time() - start
        print(f"[REDIS WAIT] ✅ Все тикеры готовы за {elapsed:.2f}с")
        return True

    print(f"[REDIS WAIT] ⏱ Таймаут {timeout}с. Не дождались: {missing}")
    return False


def load_history_from_timescaledb(tickers: list) -> pd.DataFrame:
    """Загружает реальные исторические дневные свечи из TimescaleDB"""
    print(f"[DB READ] Запрос истории из TimescaleDB для: {tickers}")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        tickers_str = ", ".join([f"'{t}'" for t in tickers])
        query = f"""
            SELECT time, ticker, close 
            FROM candles 
            WHERE ticker IN ({tickers_str})
            ORDER BY time ASC;
        """
        df_raw = pd.read_sql_query(query, conn)
        conn.close()

        if df_raw.empty:
            print("[DB READ] Предупреждение: База данных вернула 0 записей.")
            return pd.DataFrame()

        df_raw["time"] = pd.to_datetime(df_raw["time"]).dt.tz_localize(None)

        df_pivot = df_raw.pivot(index="time", columns="ticker", values="close")
        print(f"[DB READ] Успешно загружено {len(df_pivot)} исторических дней.")
        return df_pivot
    except Exception as err:
        print(f"[DB READ] Ошибка при чтении из TimescaleDB: {err}")
        return pd.DataFrame()


def load_live_data_from_redis(tickers: list) -> pd.DataFrame:
    """Загружает самые свежие минутные свечи из Redis и формирует DataFrame цены Close"""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    combined_data = {}

    for ticker in tickers:
        redis_key = f"candles:live:{ticker}"
        raw_candles = r.zrange(redis_key, 0, -1)

        times = []
        closes = []
        for raw in raw_candles:
            candle = json.loads(raw)
            times.append(pd.to_datetime(candle["time"]).tz_localize(None))
            closes.append(float(candle["close"]))

        if closes:
            combined_data[ticker] = pd.Series(closes, index=times)
            print(f"[REDIS READ] Извлечено {len(closes)} живых свечей для {ticker}")
        else:
            print(f"[REDIS READ] Для {ticker} live-свечей не найдено")

    if not combined_data:
        return pd.DataFrame()

    df = pd.DataFrame(combined_data).sort_index().ffill()
    return df


@celery_app.task(name="tasks.compute_portfolio_optimization")
def compute_portfolio_optimization(
    selected_tickers: list,
    model_name: str,
    optimisation_strategy: str,
    risk_aversion: float,
    days_to_forecast: int,
    max_asset_weight: float = 1.0,
) -> dict:
    # Мониторинг входящих параметров
    print(f"[CELERY WORKER] Входящий max_asset_weight: {max_asset_weight} (тип: {type(max_asset_weight)})")
    print(f"[CELERY WORKER] Начало выполнения задачи для тикеров: {selected_tickers}")
    num_assets = len(selected_tickers)

    # 1. Загружаем реальную историю из TimescaleDB
    base_history = load_history_from_timescaledb(selected_tickers)

    # Резервный фолбэк: если база пустая, создаем временный фрейм
    if base_history.empty:
        print("[CELERY WORKER] Внимание: Истории в БД нет. Временный откат на генерацию.")
        base_history = pd.DataFrame(
            {t: np.random.normal(100, 2, 100).cumsum() for t in selected_tickers},
            index=pd.date_range(
                end=pd.Timestamp.now() - pd.Timedelta(days=1),
                periods=100,
                freq="D",
            ).tz_localize(None),
        )

    # 2. Ждём сигнал готовности streamer'а через Redis-флаги и BLPOP
    wait_for_live_data(selected_tickers)

    # 3. Подтягиваем «живой хвост» реалтайм-данных из Redis
    live_history = load_live_data_from_redis(selected_tickers)

    # 4. Склеиваем историю с живыми данными
    if not live_history.empty:
        live_daily = live_history.resample("D").last().ffill()
        full_history = pd.concat([base_history, live_daily])
        # Оставляем последнюю запись для каждой даты
        full_history = full_history[~full_history.index.duplicated(keep="last")].sort_index()
        print("[CELERY WORKER] Живой хвост из Redis успешно пристыкован к истории.")
    else:
        full_history = base_history
        print("[CELERY WORKER] Данных в Redis не обнаружено, расчет по исторической базе данных.")

    # 5. Рассчитываем реальные доходности и ковариацию на основе склеенных данных
    returns_df = full_history.pct_change(fill_method=None).dropna()
    expected_returns = returns_df.mean() * 252
    cov_matrix = returns_df.cov() * 252

    if cov_matrix.isna().values.any() or (np.diag(cov_matrix) == 0).any():
        cov_matrix = pd.DataFrame(
            np.eye(num_assets) * 0.04,
            index=selected_tickers,
            columns=selected_tickers,
        )

    # 6. Обучение ИИ-модели Prophet
    model = ModelFactory.get_model(model_name)
    print(f"[CELERY WORKER] Запуск обучения модели {model_name}...")
    forecasted_prices = model.fit_forecast(full_history, days_to_forecast)

    # 7. Оптимизация портфеля Марковица / Шарпа
    min_bounds = [0.0] * num_assets
    max_bounds = [max_asset_weight] * num_assets

    print(f"[MATHEMATICS DASHBOARD] Количество активов: {num_assets}")
    print(f"[MATHEMATICS DASHBOARD] Переданные тикеры: {selected_tickers}")
    print(f"[MATHEMATICS DASHBOARD] Сформированные min_bounds: {min_bounds}")
    print(f"[MATHEMATICS DASHBOARD] Сформированные max_bounds: {max_bounds}")

    # Автоматически подтягиваем реальную ключевую ставку с API Центробанка
    current_risk_free_rate = get_actual_cbr_key_rate()
    print(f"[CELERY WORKER] Безрисковая ставка для оптимизации: {current_risk_free_rate}")

    optimiser = OptimiserFactory.get_optimiser(optimisation_strategy)
    optimized_weights = optimiser.optimize(
        expected_returns=expected_returns,
        cov_matrix=cov_matrix,
        min_bounds=min_bounds,
        max_bounds=max_bounds,
        risk_aversion=risk_aversion,
        risk_free_rate=current_risk_free_rate,
    )

    print("[CELERY WORKER] Расчет успешно завершен.")
    return {
        "status": "success",
        "applied_model": model_name,
        "applied_strategy": optimisation_strategy,
        "risk_free_rate": current_risk_free_rate,
        "weights": optimized_weights.to_dict(),
    }