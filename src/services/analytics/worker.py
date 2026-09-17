# src/services/analytics/worker.py
import os
import sys
import ssl
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import psycopg2  # Синхронный клиент для Celery
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from celery import Celery

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from src.settings import REDIS_HOST, REDIS_PORT, DATABASE_URL
from src.services.analytics.models import ModelRegistry
from src.services.analytics.optimiser import OptimiserFactory

celery_app = Celery(
    "trade_tasks",
    broker=f"redis://{REDIS_HOST}:{REDIS_PORT}/0",
    backend=f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
)


def get_actual_cbr_key_rate() -> float:
    """
    Скачивает актуальную ключевую ставку с SOAP-сервиса ЦБ РФ.
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


@celery_app.task(name="tasks.compute_portfolio_optimization")
def compute_portfolio_optimization(
    selected_tickers: list,
    model_name: str,
    optimisation_strategy: str,
    risk_aversion: float,
    days_to_forecast: int,
    max_asset_weight: float = 1.0,
) -> dict:
    print(f"[CELERY WORKER] Входящий max_asset_weight: {max_asset_weight}")
    print(f"[CELERY WORKER] Начало выполнения задачи для тикеров: {selected_tickers}")
    num_assets = len(selected_tickers)

    # 1. Загружаем дневную историю из TimescaleDB
    full_history = load_history_from_timescaledb(selected_tickers)

    # Фолбэк: если данных нет
    if full_history.empty:
        print("[CELERY WORKER] Внимание: Истории в БД нет. Временный откат на генерацию.")
        full_history = pd.DataFrame(
            {t: np.random.normal(100, 2, 100).cumsum() for t in selected_tickers},
            index=pd.date_range(
                end=pd.Timestamp.now() - pd.Timedelta(days=1),
                periods=100,
                freq="D",
            ).tz_localize(None),
        )

    # 2. Нормализация индекса и дедупликация
    full_history.index = pd.to_datetime(full_history.index).normalize().tz_localize(None)
    full_history = full_history[~full_history.index.duplicated(keep="last")].sort_index()
    full_history = full_history.loc[:, ~full_history.columns.duplicated()]

    print(f"[CELERY WORKER] История: {len(full_history)} дней, "
          f"тикеры: {list(full_history.columns)}")

    # 3. Обучение модели через ModelRegistry
    print(f"[CELERY WORKER] Запуск модели: {model_name}")
    try:
        model = ModelRegistry.get(model_name)
        model_output = model.fit_predict(
            full_history,
            days_to_forecast=days_to_forecast,
        )

        expected_returns = model_output.expected_returns
        cov_matrix = model_output.cov_matrix

        print(f"[CELERY WORKER] Модель '{model.name}' вернула прогноз:")
        print(f"[CELERY WORKER]   Ожидаемые годовые доходности:")
        print(expected_returns.to_string())
        print(f"[CELERY WORKER]   Метаданные: {model_output.model_metadata}")

    except Exception as e:
        print(f"[CELERY WORKER] Ошибка модели '{model_name}': {e}")
        print("[CELERY WORKER] Откат на исторические доходности.")
        returns_df = full_history.pct_change(fill_method=None).dropna()
        expected_returns = returns_df.mean() * 252
        cov_matrix = returns_df.cov() * 252

    # 4. Фолбэк ковариации
    if cov_matrix is None or cov_matrix.isna().values.any() or (np.diag(cov_matrix) == 0).any():
        print("[CELERY WORKER] Ковариация невалидна, использую единичную матрицу.")
        cov_matrix = pd.DataFrame(
            np.eye(num_assets) * 0.04,
            index=selected_tickers,
            columns=selected_tickers,
        )

    # 5. Оптимизация портфеля
    min_bounds = [0.0] * num_assets
    max_bounds = [max_asset_weight] * num_assets

    print(f"[MATHEMATICS DASHBOARD] Количество активов: {num_assets}")
    print(f"[MATHEMATICS DASHBOARD] Переданные тикеры: {selected_tickers}")
    print(f"[MATHEMATICS DASHBOARD] Сформированные min_bounds: {min_bounds}")
    print(f"[MATHEMATICS DASHBOARD] Сформированные max_bounds: {max_bounds}")

    current_risk_free_rate = get_actual_cbr_key_rate()
    print(f"[CELERY WORKER] Безрисковая ставка для оптимизации: {current_risk_free_rate}")

    optimiser = OptimiserFactory.get_optimiser(optimisation_strategy)
    optimiser_result = optimiser.optimize(
        expected_returns=expected_returns,
        cov_matrix=cov_matrix,
        min_bounds=min_bounds,
        max_bounds=max_bounds,
        risk_aversion=risk_aversion,
        risk_free_rate=current_risk_free_rate,
    )

    # 6. Считаем остаток — сколько осталось в наличных
    weights_dict = optimiser_result["weights"].to_dict()
    total_invested = sum(weights_dict.values())
    cash_weight = round(max(0.0, 1.0 - total_invested), 4)

    if cash_weight > 0.001:
        print(f"[CELERY WORKER] ⚠️ Не все средства распределены. "
              f"В наличных: {cash_weight * 100:.2f}%")
    else:
        cash_weight = 0.0

    print("[CELERY WORKER] Расчет успешно завершен.")
    return {
        "status": "success",
        "applied_model": model_name,
        "applied_strategy": optimisation_strategy,
        "risk_free_rate": current_risk_free_rate,
        "weights": weights_dict,
        "cash_weight": cash_weight,
        "optimiser_success": optimiser_result["success"],
        "fallback_used": optimiser_result["fallback_used"],
        "optimiser_message": optimiser_result["message"],
    }