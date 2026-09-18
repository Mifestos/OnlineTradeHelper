# src/services/analytics/worker.py
import os
import sys
import ssl
import json
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import psycopg2
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from celery import Celery
from celery.schedules import crontab

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from src.settings import REDIS_HOST, REDIS_PORT, DATABASE_URL
from src.services.analytics.models import ModelRegistry
from src.services.analytics.optimiser import OptimiserFactory
from src.services.analytics.profiles import get_profile

celery_app = Celery(
    "trade_tasks",
    broker=f"redis://{REDIS_HOST}:{REDIS_PORT}/0",
    backend=f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
)

celery_app.conf.beat_schedule = {
    "check-schedules-every-minute": {
        "task": "tasks.check_and_run_schedules",
        "schedule": crontab(minute="*"),
    },
}

# Дефолты execution (если профиль не задан)
DEFAULT_EXECUTION = {
    "cooldown_hours": 24,
    "no_trade_threshold": 0.05,
    "max_turnover": 0.20,
}


# ============================================================================
# CBR API
# ============================================================================

def get_actual_cbr_key_rate() -> float:
    """Скачивает актуальную ключевую ставку с SOAP-сервиса ЦБ РФ."""
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


# ============================================================================
# DB: история
# ============================================================================

def load_history_from_timescaledb(tickers: list) -> pd.DataFrame:
    """Загружает исторические дневные свечи из TimescaleDB."""
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


# ============================================================================
# DB: текущий портфель
# ============================================================================

def load_current_portfolio() -> dict:
    """
    Загружает текущий портфель пользователя (кол-во акций и средние цены).
    Возвращает {"positions": {...}, "avg_prices": {...}}.
    """
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT positions, avg_prices 
            FROM current_portfolios 
            WHERE user_id = 1
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            print("[PORTFOLIO] Текущий портфель не задан")
            return {"positions": {}, "avg_prices": {}}

        positions = row[0] or {}
        avg_prices = row[1] or {}
        print(f"[PORTFOLIO] Загружен портфель: {len(positions)} позиций")
        return {"positions": positions, "avg_prices": avg_prices}
    except Exception as e:
        print(f"[PORTFOLIO] Ошибка загрузки: {e}")
        return {"positions": {}, "avg_prices": {}}


def get_current_weights(tickers: list) -> dict:
    """
    Возвращает текущие веса портфеля (доли) на основе последних цен.
    
    Пример: 
        positions = {"SBER": 100, "YDEX": 5}  (акций)
        last_prices = {"SBER": 300, "YDEX": 4000}
        → {"SBER": 100*300 / total, "YDEX": 5*4000 / total}
    """
    portfolio = load_current_portfolio()
    positions = portfolio["positions"]

    if not positions:
        return {}

    # Загружаем последние цены из БД
    try:
        conn = psycopg2.connect(DATABASE_URL)
        tickers_str = ", ".join([f"'{t}'" for t in positions.keys()])
        query = f"""
            SELECT DISTINCT ON (ticker) ticker, close 
            FROM candles 
            WHERE ticker IN ({tickers_str})
            ORDER BY ticker, time DESC;
        """
        cur = conn.cursor()
        cur.execute(query)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        last_prices = {row[0]: row[1] for row in rows}
    except Exception as e:
        print(f"[PORTFOLIO] Ошибка загрузки последних цен: {e}")
        return {}

    # Считаем стоимость каждой позиции
    total_value = 0.0
    values = {}
    for ticker, qty in positions.items():
        price = last_prices.get(ticker, 0)
        if price > 0:
            value = qty * price
            values[ticker] = value
            total_value += value

    if total_value == 0:
        return {}

    # Нормализуем в доли
    weights = {t: round(v / total_value, 4) for t, v in values.items()}
    print(f"[PORTFOLIO] Текущие веса: {weights}")
    return weights


# ============================================================================
# Turnover logic
# ============================================================================

def calculate_turnover(current_weights: dict, target_weights: dict) -> float:
    """
    Считает turnover — половину суммы абсолютных отклонений.
    Формула: turnover = Σ|target - current| / 2
    """
    all_tickers = set(current_weights.keys()) | set(target_weights.keys())
    total_diff = 0.0
    for t in all_tickers:
        cw = current_weights.get(t, 0.0)
        tw = target_weights.get(t, 0.0)
        total_diff += abs(tw - cw)
    return round(total_diff / 2.0, 4)


def apply_no_trade_zone(current_weights, target_weights, threshold):
    """
    Если turnover < threshold — не торгуем.
    Возвращает (skip, turnover).
    """
    if not current_weights:
        # Нет текущего портфеля — торгуем всегда
        return False, 0.0

    turnover = calculate_turnover(current_weights, target_weights)

    if turnover < threshold:
        print(f"[TURNOVER] Отклонение {turnover*100:.2f}% < порога {threshold*100:.2f}% — не торгуем")
        return True, turnover

    print(f"[TURNOVER] Отклонение {turnover*100:.2f}% ≥ порога {threshold*100:.2f}% — торгуем")
    return False, turnover


def apply_turnover_limit(current_weights, target_weights, max_turnover):
    """
    Если turnover > max_turnover — интерполируем между текущими и целевыми весами.
    Возвращает (new_weights, was_interpolated, actual_turnover).
    """
    turnover = calculate_turnover(current_weights, target_weights)

    if not current_weights or turnover <= max_turnover:
        return target_weights, False, turnover

    # Интерполяция: new = current*(1-α) + target*α, где α = max_turnover / turnover
    alpha = max_turnover / turnover
    print(f"[TURNOVER] Ограничиваем turnover: {turnover*100:.2f}% → {max_turnover*100:.2f}% "
          f"(коэффициент α={alpha:.3f})")

    all_tickers = set(current_weights.keys()) | set(target_weights.keys())
    new_weights = {}
    for t in all_tickers:
        cw = current_weights.get(t, 0.0)
        tw = target_weights.get(t, 0.0)
        new_weights[t] = round(cw * (1 - alpha) + tw * alpha, 4)

    # Нормализация на случай погрешности округления
    total = sum(new_weights.values())
    if total > 0:
        new_weights = {t: round(v / total, 4) for t, v in new_weights.items()}

    actual_turnover = calculate_turnover(current_weights, new_weights)
    return new_weights, True, actual_turnover


# ============================================================================
# DB: сохранение в историю
# ============================================================================

def save_optimization_run(
    profile_name,
    model_name,
    optimisation_strategy,
    tickers,
    weights,
    cash_weight,
    risk_free_rate,
    max_asset_weight,
    risk_aversion,
    fallback_used,
    optimiser_success,
    turnover=None,
    rebalance_skipped=False,
    interpolated=False,
):
    """Сохраняет результат оптимизации в историю."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO optimization_runs
                (profile_name, model_name, optimisation_strategy, tickers, weights,
                 cash_weight, risk_free_rate, max_asset_weight, risk_aversion,
                 fallback_used, optimiser_success)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            profile_name,
            model_name,
            optimisation_strategy,
            tickers,
            json.dumps(weights),
            cash_weight,
            risk_free_rate,
            max_asset_weight,
            risk_aversion,
            fallback_used,
            optimiser_success,
        ))
        conn.commit()
        cur.close()
        conn.close()
        print(f"[HISTORY] Запуск сохранён (turnover={turnover}, "
              f"skipped={rebalance_skipped}, interpolated={interpolated})")
        return True
    except Exception as e:
        print(f"[HISTORY] Ошибка сохранения: {e}")
        return False


# ============================================================================
# SCHEDULER
# ============================================================================

@celery_app.task(name="tasks.check_and_run_schedules")
def check_and_run_schedules():
    """Проверяет расписания каждую минуту."""
    now = datetime.now()
    current_hour = now.hour
    current_minute = now.minute

    print(f"[SCHEDULER] Проверка расписаний: {now.strftime('%H:%M')}")

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
            SELECT id, profile_name, model_name, optimisation_strategy,
                   tickers, risk_aversion, max_asset_weight,
                   run_hour, run_minute, last_run_at
            FROM schedules
            WHERE is_active = TRUE
              AND run_hour = %s
              AND run_minute = %s
              AND (last_run_at IS NULL OR last_run_at::date < NOW()::date)
        """, (current_hour, current_minute))

        schedules = cur.fetchall()
        cur.close()
        conn.close()

        if not schedules:
            return {"status": "ok", "triggered": 0}

        print(f"[SCHEDULER] Найдено расписаний: {len(schedules)}")

        for sched in schedules:
            (schedule_id, profile_name, model_name, strategy,
             tickers, risk_aversion, max_asset_weight,
             run_hour, run_minute, last_run_at) = sched

            print(f"[SCHEDULER] Запуск #{schedule_id}: {model_name}/{strategy}")

            compute_portfolio_optimization.apply_async(
                kwargs={
                    "selected_tickers": list(tickers),
                    "model_name": model_name,
                    "optimisation_strategy": strategy,
                    "risk_aversion": risk_aversion,
                    "days_to_forecast": 30,
                    "max_asset_weight": max_asset_weight,
                    "profile_name": profile_name,
                }
            )

            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("UPDATE schedules SET last_run_at = NOW() WHERE id = %s", (schedule_id,))
            conn.commit()
            cur.close()
            conn.close()

        return {"status": "ok", "triggered": len(schedules)}

    except Exception as e:
        print(f"[SCHEDULER] Ошибка: {e}")
        return {"status": "error", "error": str(e)}


# ============================================================================
# ОСНОВНАЯ ЗАДАЧА
# ============================================================================

@celery_app.task(name="tasks.compute_portfolio_optimization")
def compute_portfolio_optimization(
    selected_tickers: list,
    model_name: str,
    optimisation_strategy: str,
    risk_aversion: float,
    days_to_forecast: int,
    max_asset_weight: float = 1.0,
    profile_name: str = None,
) -> dict:
    print(f"[CELERY WORKER] Входящий max_asset_weight: {max_asset_weight}")
    print(f"[CELERY WORKER] Профиль: {profile_name}")
    print(f"[CELERY WORKER] Тикеры: {selected_tickers}")
    num_assets = len(selected_tickers)

    # Загружаем execution-параметры из профиля
    if profile_name:
        profile = get_profile(profile_name)
        if profile:
            no_trade_threshold = profile.get("no_trade_threshold", DEFAULT_EXECUTION["no_trade_threshold"])
            max_turnover = profile.get("max_turnover", DEFAULT_EXECUTION["max_turnover"])
            print(f"[EXECUTION] Профиль '{profile_name}': "
                  f"no_trade={no_trade_threshold*100:.1f}%, "
                  f"max_turnover={max_turnover*100:.1f}%")
        else:
            no_trade_threshold = DEFAULT_EXECUTION["no_trade_threshold"]
            max_turnover = DEFAULT_EXECUTION["max_turnover"]
    else:
        no_trade_threshold = DEFAULT_EXECUTION["no_trade_threshold"]
        max_turnover = DEFAULT_EXECUTION["max_turnover"]
        print(f"[EXECUTION] Ручной режим: "
              f"no_trade={no_trade_threshold*100:.1f}%, "
              f"max_turnover={max_turnover*100:.1f}%")

    # 1. Загружаем историю
    full_history = load_history_from_timescaledb(selected_tickers)

    if full_history.empty:
        print("[CELERY WORKER] Истории в БД нет. Генерация fallback.")
        full_history = pd.DataFrame(
            {t: np.random.normal(100, 2, 100).cumsum() for t in selected_tickers},
            index=pd.date_range(
                end=pd.Timestamp.now() - pd.Timedelta(days=1),
                periods=100,
                freq="D",
            ).tz_localize(None),
        )

    # 2. Нормализация
    full_history.index = pd.to_datetime(full_history.index).normalize().tz_localize(None)
    full_history = full_history[~full_history.index.duplicated(keep="last")].sort_index()
    full_history = full_history.loc[:, ~full_history.columns.duplicated()]

    print(f"[CELERY WORKER] История: {len(full_history)} дней")

    # 3. Модель
    print(f"[CELERY WORKER] Запуск модели: {model_name}")
    try:
        model = ModelRegistry.get(model_name)
        model_output = model.fit_predict(full_history, days_to_forecast=days_to_forecast)
        expected_returns = model_output.expected_returns
        cov_matrix = model_output.cov_matrix
        print(f"[CELERY WORKER] Прогнозы:\n{expected_returns.to_string()}")
    except Exception as e:
        print(f"[CELERY WORKER] Ошибка модели: {e}. Откат.")
        returns_df = full_history.pct_change(fill_method=None).dropna()
        expected_returns = returns_df.mean() * 252
        cov_matrix = returns_df.cov() * 252

    # 4. Ковариация
    if cov_matrix is None or cov_matrix.isna().values.any() or (np.diag(cov_matrix) == 0).any():
        cov_matrix = pd.DataFrame(
            np.eye(num_assets) * 0.04,
            index=selected_tickers,
            columns=selected_tickers,
        )

    # 5. Оптимизация
    min_bounds = [0.0] * num_assets
    max_bounds = [max_asset_weight] * num_assets

    current_risk_free_rate = get_actual_cbr_key_rate()
    print(f"[CELERY WORKER] Ставка ЦБ: {current_risk_free_rate}")

    optimiser = OptimiserFactory.get_optimiser(optimisation_strategy)
    optimiser_result = optimiser.optimize(
        expected_returns=expected_returns,
        cov_matrix=cov_matrix,
        min_bounds=min_bounds,
        max_bounds=max_bounds,
        risk_aversion=risk_aversion,
        risk_free_rate=current_risk_free_rate,
    )

    weights_dict = optimiser_result["weights"].to_dict()

    # 6. Turnover logic
    current_weights = get_current_weights(selected_tickers)

    rebalance_skipped = False
    interpolated = False
    turnover = 0.0

    if current_weights:
        # No-trade zone
        skip, turnover = apply_no_trade_zone(current_weights, weights_dict, no_trade_threshold)
        if skip:
            rebalance_skipped = True
            print(f"[CELERY WORKER] Ребалансировка пропущена (turnover {turnover*100:.2f}%)")
        else:
            # Turnover limit
            weights_dict, interpolated, turnover = apply_turnover_limit(
                current_weights, weights_dict, max_turnover
            )
            if interpolated:
                print(f"[CELERY WORKER] Веса интерполированы (turnover {turnover*100:.2f}%)")
    else:
        print("[CELERY WORKER] Нет текущего портфеля — turnover не считается")

    # 7. Считаем остаток
    total_invested = sum(weights_dict.values())
    cash_weight = round(max(0.0, 1.0 - total_invested), 4)
    if cash_weight <= 0.001:
        cash_weight = 0.0

    # 8. Сохраняем в историю
    save_optimization_run(
        profile_name=profile_name,
        model_name=model_name,
        optimisation_strategy=optimisation_strategy,
        tickers=selected_tickers,
        weights=weights_dict,
        cash_weight=cash_weight,
        risk_free_rate=current_risk_free_rate,
        max_asset_weight=max_asset_weight,
        risk_aversion=risk_aversion,
        fallback_used=optimiser_result["fallback_used"],
        optimiser_success=optimiser_result["success"],
        turnover=turnover,
        rebalance_skipped=rebalance_skipped,
        interpolated=interpolated,
    )

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
        # Turnover info
        "turnover": turnover,
        "rebalance_skipped": rebalance_skipped,
        "interpolated": interpolated,
        "current_weights": current_weights,
    }# src/services/analytics/worker.py
import os
import sys
import ssl
import json
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import psycopg2
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from celery import Celery
from celery.schedules import crontab

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from src.settings import REDIS_HOST, REDIS_PORT, DATABASE_URL
from src.services.analytics.models import ModelRegistry
from src.services.analytics.optimiser import OptimiserFactory
from src.services.analytics.profiles import get_profile

celery_app = Celery(
    "trade_tasks",
    broker=f"redis://{REDIS_HOST}:{REDIS_PORT}/0",
    backend=f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
)

celery_app.conf.beat_schedule = {
    "check-schedules-every-minute": {
        "task": "tasks.check_and_run_schedules",
        "schedule": crontab(minute="*"),
    },
}

# Дефолты execution
DEFAULT_EXECUTION = {
    "cooldown_hours": 24,
    "no_trade_threshold": 0.05,
    "max_turnover": 0.20,
}


# ============================================================================
# CBR API
# ============================================================================

def get_actual_cbr_key_rate() -> float:
    """Скачивает актуальную ключевую ставку с SOAP-сервиса ЦБ РФ."""
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


# ============================================================================
# DB: история
# ============================================================================

def load_history_from_timescaledb(tickers: list) -> pd.DataFrame:
    """Загружает исторические дневные свечи из TimescaleDB."""
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


# ============================================================================
# DB: текущий портфель
# ============================================================================

def load_current_portfolio() -> dict:
    """Загружает текущий портфель пользователя."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT positions, avg_prices 
            FROM current_portfolios 
            WHERE user_id = 1
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            print("[PORTFOLIO] Текущий портфель не задан")
            return {"positions": {}, "avg_prices": {}}

        positions = row[0] or {}
        avg_prices = row[1] or {}
        print(f"[PORTFOLIO] Загружен портфель: {len(positions)} позиций")
        return {"positions": positions, "avg_prices": avg_prices}
    except Exception as e:
        print(f"[PORTFOLIO] Ошибка загрузки: {e}")
        return {"positions": {}, "avg_prices": {}}


def get_current_weights(tickers: list) -> dict:
    """Возвращает текущие веса портфеля (доли) на основе последних цен."""
    portfolio = load_current_portfolio()
    positions = portfolio["positions"]

    if not positions:
        return {}

    # Загружаем последние цены из БД
    try:
        conn = psycopg2.connect(DATABASE_URL)
        tickers_str = ", ".join([f"'{t}'" for t in positions.keys()])
        query = f"""
            SELECT DISTINCT ON (ticker) ticker, close 
            FROM candles 
            WHERE ticker IN ({tickers_str})
            ORDER BY ticker, time DESC;
        """
        cur = conn.cursor()
        cur.execute(query)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        last_prices = {row[0]: row[1] for row in rows}
    except Exception as e:
        print(f"[PORTFOLIO] Ошибка загрузки последних цен: {e}")
        return {}

    # Считаем стоимость каждой позиции
    total_value = 0.0
    values = {}
    for ticker, qty in positions.items():
        price = last_prices.get(ticker, 0)
        if price > 0:
            value = qty * price
            values[ticker] = value
            total_value += value

    if total_value == 0:
        return {}

    # Нормализуем в доли
    weights = {t: round(v / total_value, 4) for t, v in values.items()}
    print(f"[PORTFOLIO] Текущие веса: {weights}")
    return weights


# ============================================================================
# Turnover logic
# ============================================================================

def calculate_turnover(current_weights: dict, target_weights: dict) -> float:
    """Считает turnover — половину суммы абсолютных отклонений."""
    all_tickers = set(current_weights.keys()) | set(target_weights.keys())
    total_diff = 0.0
    for t in all_tickers:
        cw = current_weights.get(t, 0.0)
        tw = target_weights.get(t, 0.0)
        total_diff += abs(tw - cw)
    return round(total_diff / 2.0, 4)


def apply_no_trade_zone(current_weights, target_weights, threshold):
    """Если turnover < threshold — не торгуем."""
    if not current_weights:
        return False, 0.0

    turnover = calculate_turnover(current_weights, target_weights)

    if turnover < threshold:
        print(f"[TURNOVER] Отклонение {turnover*100:.2f}% < порога {threshold*100:.2f}% — не торгуем")
        return True, turnover

    print(f"[TURNOVER] Отклонение {turnover*100:.2f}% ≥ порога {threshold*100:.2f}% — торгуем")
    return False, turnover


def apply_turnover_limit(current_weights, target_weights, max_turnover):
    """Если turnover > max_turnover — интерполируем."""
    turnover = calculate_turnover(current_weights, target_weights)

    if not current_weights or turnover <= max_turnover:
        return target_weights, False, turnover

    alpha = max_turnover / turnover
    print(f"[TURNOVER] Ограничиваем turnover: {turnover*100:.2f}% → {max_turnover*100:.2f}% "
          f"(α={alpha:.3f})")

    all_tickers = set(current_weights.keys()) | set(target_weights.keys())
    new_weights = {}
    for t in all_tickers:
        cw = current_weights.get(t, 0.0)
        tw = target_weights.get(t, 0.0)
        new_weights[t] = round(cw * (1 - alpha) + tw * alpha, 4)

    total = sum(new_weights.values())
    if total > 0:
        new_weights = {t: round(v / total, 4) for t, v in new_weights.items()}

    actual_turnover = calculate_turnover(current_weights, new_weights)
    return new_weights, True, actual_turnover


# ============================================================================
# DB: сохранение в историю
# ============================================================================

def save_optimization_run(
    profile_name,
    model_name,
    optimisation_strategy,
    tickers,
    weights,
    cash_weight,
    risk_free_rate,
    max_asset_weight,
    risk_aversion,
    fallback_used,
    optimiser_success,
    turnover=None,
    rebalance_skipped=False,
    interpolated=False,
):
    """Сохраняет результат оптимизации в историю."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO optimization_runs
                (profile_name, model_name, optimisation_strategy, tickers, weights,
                 cash_weight, risk_free_rate, max_asset_weight, risk_aversion,
                 fallback_used, optimiser_success)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            profile_name,
            model_name,
            optimisation_strategy,
            tickers,
            json.dumps(weights),
            cash_weight,
            risk_free_rate,
            max_asset_weight,
            risk_aversion,
            fallback_used,
            optimiser_success,
        ))
        conn.commit()
        cur.close()
        conn.close()
        print(f"[HISTORY] Запуск сохранён (turnover={turnover}, "
              f"skipped={rebalance_skipped}, interpolated={interpolated})")
        return True
    except Exception as e:
        print(f"[HISTORY] Ошибка сохранения: {e}")
        return False


# ============================================================================
# SCHEDULER
# ============================================================================

@celery_app.task(name="tasks.check_and_run_schedules")
def check_and_run_schedules():
    """Проверяет расписания каждую минуту."""
    now = datetime.now()
    current_hour = now.hour
    current_minute = now.minute

    print(f"[SCHEDULER] Проверка расписаний: {now.strftime('%H:%M')}")

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
            SELECT id, profile_name, model_name, optimisation_strategy,
                   tickers, risk_aversion, max_asset_weight,
                   run_hour, run_minute, last_run_at
            FROM schedules
            WHERE is_active = TRUE
              AND run_hour = %s
              AND run_minute = %s
              AND (last_run_at IS NULL OR last_run_at::date < NOW()::date)
        """, (current_hour, current_minute))

        schedules = cur.fetchall()
        cur.close()
        conn.close()

        if not schedules:
            return {"status": "ok", "triggered": 0}

        print(f"[SCHEDULER] Найдено расписаний: {len(schedules)}")

        for sched in schedules:
            (schedule_id, profile_name, model_name, strategy,
             tickers, risk_aversion, max_asset_weight,
             run_hour, run_minute, last_run_at) = sched

            print(f"[SCHEDULER] Запуск #{schedule_id}: {model_name}/{strategy}")

            compute_portfolio_optimization.apply_async(
                kwargs={
                    "selected_tickers": list(tickers),
                    "model_name": model_name,
                    "optimisation_strategy": strategy,
                    "risk_aversion": risk_aversion,
                    "days_to_forecast": 30,
                    "max_asset_weight": max_asset_weight,
                    "profile_name": profile_name,
                }
            )

            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("UPDATE schedules SET last_run_at = NOW() WHERE id = %s", (schedule_id,))
            conn.commit()
            cur.close()
            conn.close()

        return {"status": "ok", "triggered": len(schedules)}

    except Exception as e:
        print(f"[SCHEDULER] Ошибка: {e}")
        return {"status": "error", "error": str(e)}


# ============================================================================
# ОСНОВНАЯ ЗАДАЧА
# ============================================================================

@celery_app.task(name="tasks.compute_portfolio_optimization")
def compute_portfolio_optimization(
    selected_tickers: list,
    model_name: str,
    optimisation_strategy: str,
    risk_aversion: float,
    days_to_forecast: int,
    max_asset_weight: float = 1.0,
    profile_name: str = None,
    # Execution-параметры (передаются из UI, имеют приоритет над профилем):
    cooldown_hours: float = None,
    no_trade_threshold: float = None,
    max_turnover: float = None,
) -> dict:
    print(f"[CELERY WORKER] Входящий max_asset_weight: {max_asset_weight}")
    print(f"[CELERY WORKER] Профиль: {profile_name}")
    print(f"[CELERY WORKER] Тикеры: {selected_tickers}")
    num_assets = len(selected_tickers)

    # === Приоритет execution-параметров ===
    # 1. Параметры из запроса (ручной режим)
    # 2. Параметры из профиля
    # 3. Дефолты

    profile_exec = {}
    if profile_name:
        profile = get_profile(profile_name)
        if profile:
            profile_exec = {
                "cooldown_hours": profile.get("cooldown_hours"),
                "no_trade_threshold": profile.get("no_trade_threshold"),
                "max_turnover": profile.get("max_turnover"),
            }

    # Итоговые значения
    final_no_trade = no_trade_threshold
    if final_no_trade is None:
        final_no_trade = profile_exec.get("no_trade_threshold")
    if final_no_trade is None:
        final_no_trade = DEFAULT_EXECUTION["no_trade_threshold"]

    final_max_turnover = max_turnover
    if final_max_turnover is None:
        final_max_turnover = profile_exec.get("max_turnover")
    if final_max_turnover is None:
        final_max_turnover = DEFAULT_EXECUTION["max_turnover"]

    # Источник параметров (для лога)
    if no_trade_threshold is not None or max_turnover is not None:
        source = "запрос (ручной режим)"
    elif profile_exec:
        source = f"профиль '{profile_name}'"
    else:
        source = "дефолты"

    print(f"[EXECUTION] Источник: {source} | "
          f"no_trade={final_no_trade*100:.1f}%, "
          f"max_turnover={final_max_turnover*100:.1f}%")

    # 1. Загружаем историю
    full_history = load_history_from_timescaledb(selected_tickers)

    if full_history.empty:
        print("[CELERY WORKER] Истории в БД нет. Генерация fallback.")
        full_history = pd.DataFrame(
            {t: np.random.normal(100, 2, 100).cumsum() for t in selected_tickers},
            index=pd.date_range(
                end=pd.Timestamp.now() - pd.Timedelta(days=1),
                periods=100,
                freq="D",
            ).tz_localize(None),
        )

    # 2. Нормализация
    full_history.index = pd.to_datetime(full_history.index).normalize().tz_localize(None)
    full_history = full_history[~full_history.index.duplicated(keep="last")].sort_index()
    full_history = full_history.loc[:, ~full_history.columns.duplicated()]

    print(f"[CELERY WORKER] История: {len(full_history)} дней")

    # 3. Модель
    print(f"[CELERY WORKER] Запуск модели: {model_name}")
    try:
        model = ModelRegistry.get(model_name)
        model_output = model.fit_predict(full_history, days_to_forecast=days_to_forecast)
        expected_returns = model_output.expected_returns
        cov_matrix = model_output.cov_matrix
        print(f"[CELERY WORKER] Прогнозы:\n{expected_returns.to_string()}")
    except Exception as e:
        print(f"[CELERY WORKER] Ошибка модели: {e}. Откат.")
        returns_df = full_history.pct_change(fill_method=None).dropna()
        expected_returns = returns_df.mean() * 252
        cov_matrix = returns_df.cov() * 252

    # 4. Ковариация
    if cov_matrix is None or cov_matrix.isna().values.any() or (np.diag(cov_matrix) == 0).any():
        cov_matrix = pd.DataFrame(
            np.eye(num_assets) * 0.04,
            index=selected_tickers,
            columns=selected_tickers,
        )

    # 5. Оптимизация
    min_bounds = [0.0] * num_assets
    max_bounds = [max_asset_weight] * num_assets

    current_risk_free_rate = get_actual_cbr_key_rate()
    print(f"[CELERY WORKER] Ставка ЦБ: {current_risk_free_rate}")

    optimiser = OptimiserFactory.get_optimiser(optimisation_strategy)
    optimiser_result = optimiser.optimize(
        expected_returns=expected_returns,
        cov_matrix=cov_matrix,
        min_bounds=min_bounds,
        max_bounds=max_bounds,
        risk_aversion=risk_aversion,
        risk_free_rate=current_risk_free_rate,
    )

    weights_dict = optimiser_result["weights"].to_dict()

    # 6. Turnover logic
    current_weights = get_current_weights(selected_tickers)

    rebalance_skipped = False
    interpolated = False
    turnover = 0.0

    if current_weights:
        # No-trade zone
        skip, turnover = apply_no_trade_zone(current_weights, weights_dict, final_no_trade)
        if skip:
            rebalance_skipped = True
            print(f"[CELERY WORKER] Ребалансировка пропущена (turnover {turnover*100:.2f}%)")
        else:
            # Turnover limit
            weights_dict, interpolated, turnover = apply_turnover_limit(
                current_weights, weights_dict, final_max_turnover
            )
            if interpolated:
                print(f"[CELERY WORKER] Веса интерполированы (turnover {turnover*100:.2f}%)")
    else:
        print("[CELERY WORKER] Нет текущего портфеля — turnover не считается")

    # 7. Считаем остаток
    total_invested = sum(weights_dict.values())
    cash_weight = round(max(0.0, 1.0 - total_invested), 4)
    if cash_weight <= 0.001:
        cash_weight = 0.0

    # 8. Сохраняем в историю
    save_optimization_run(
        profile_name=profile_name,
        model_name=model_name,
        optimisation_strategy=optimisation_strategy,
        tickers=selected_tickers,
        weights=weights_dict,
        cash_weight=cash_weight,
        risk_free_rate=current_risk_free_rate,
        max_asset_weight=max_asset_weight,
        risk_aversion=risk_aversion,
        fallback_used=optimiser_result["fallback_used"],
        optimiser_success=optimiser_result["success"],
        turnover=turnover,
        rebalance_skipped=rebalance_skipped,
        interpolated=interpolated,
    )

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
        "turnover": turnover,
        "rebalance_skipped": rebalance_skipped,
        "interpolated": interpolated,
        "current_weights": current_weights,
        # Для отладки — какие параметры применились
        "applied_no_trade_threshold": final_no_trade,
        "applied_max_turnover": final_max_turnover,
    }