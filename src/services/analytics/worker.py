import os
import sys
import json
import numpy as np
import pandas as pd
import redis  # Используем синхронный клиент для Celery
from celery import Celery

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from src.settings import REDIS_HOST, REDIS_PORT
from src.services.analytics.models import ModelFactory
from src.services.analytics.optimiser import OptimiserFactory

celery_app = Celery(
    "trade_tasks",
    broker=f"redis://{REDIS_HOST}:{REDIS_PORT}/0",
    backend=f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
)

def load_live_data_from_redis(tickers: list) -> pd.DataFrame:
    """Загружает самые свежие минутные свечи из Redis и формирует DataFrame цены Close"""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    combined_data = {}
    
    for ticker in tickers:
        redis_key = f"candles:live:{ticker}"
        # Забираем все свечи из Sorted Set
        raw_candles = r.zrange(redis_key, 0, -1)
        
        times = []
        closes = []
        for raw in raw_candles:
            candle = json.loads(raw)
            times.append(pd.to_datetime(candle["time"]))
            closes.append(float(candle["close"]))
            
        if closes:
            combined_data[ticker] = pd.Series(closes, index=times)
            print(f"[REDIS READ] Извлечено {len(closes)} живых свечей для {ticker}")
            
    if not combined_data:
        return pd.DataFrame()
        
    # Превращаем в общий DataFrame, выравнивая по времени, и заполняем пропуски (FFILL)
    df = pd.DataFrame(combined_data).sort_index().ffill()
    return df

@celery_app.task(name="tasks.compute_portfolio_optimization")
def compute_portfolio_optimization(selected_tickers: list, model_name: str, optimisation_strategy: str, risk_aversion: float, days_to_forecast: int) -> dict:
    print(f"[CELERY WORKER] Начало выполнения тяжелой ИИ-задачи для тикеров: {selected_tickers}")
    
    num_assets = len(selected_tickers)
    
    # 1. Формируем историческую базу (базовые 100 дней истории)
    # В будущем здесь будет запрос к PostgreSQL: SELECT ... FROM candles WHERE ...
    base_history = pd.DataFrame(
        {t: np.random.normal(250 if t == "SBER" else 100, 2, 100).cumsum() for t in selected_tickers},
        index=pd.date_range(end=pd.Timestamp.now() - pd.Timedelta(days=1), periods=100, freq='D')
    )
    
    # 2. Подтягиваем «живой хвост» реалтайм-данных из Redis
    live_history = load_live_data_from_redis(selected_tickers)
    
    # 3. Склеиваем историю с живыми данными
        # 3. Склеиваем историю с живыми данными
    if not live_history.empty:
        # Ресемплим минутные свечи к дневному формату (берем последнюю цену дня), чтобы состыковать с базой
        live_daily = live_history.resample('D').last().ffill()
        # Объединяем, удаляя дубликаты индексов в пользу самых свежих данных
        full_history = pd.concat([base_history, live_daily])
        vanity = ~full_history.index.duplicated(keep='last')  #
        full_history = full_history[vanity].sort_index()
        print("[CELERY WORKER] Живой хвост из Redis успешно пристыкован к истории.")
    else:
        full_history = base_history
        print("[CELERY WORKER] Данных в Redis не обнаружено, расчет только по исторической базе.")

    # 4. Рассчитываем реальные доходности и ковариацию на основе склеенных данных
    returns_df = full_history.pct_change().dropna()
    expected_returns = returns_df.mean() * 252 # Годовая доходность
    cov_matrix = returns_df.cov() * 252        # Годовая матрица ковариации
    
    # Защита от нулевой дисперсии, если данных слишком мало
    if cov_matrix.isna().values.any() or (np.diag(cov_matrix) == 0).any():
        cov_matrix = pd.DataFrame(np.eye(num_assets) * 0.04, index=selected_tickers, columns=selected_tickers)

    # 5. Обучение ИИ-модели Prophet
    model = ModelFactory.get_model(model_name)
    print(f"[CELERY WORKER] Запуск обучения модели {model_name}...")
    forecasted_prices = model.fit_forecast(full_history, days_to_forecast)
    
    # 6. Оптимизация портфеля Марковица / Шарпа
    min_bounds = [0.0] * num_assets
    max_bounds = [1.0] * num_assets
    
    optimiser = OptimiserFactory.get_optimiser(optimisation_strategy)
    optimized_weights = optimiser.optimize(
        expected_returns=expected_returns,
        cov_matrix=cov_matrix,
        min_bounds=min_bounds,
        max_bounds=max_bounds,
        risk_aversion=risk_aversion,
        risk_free_rate=0.18
    )
    
    print("[CELERY WORKER] Расчет успешно завершен.")
    return {
        "status": "success",
        "applied_model": model_name,
        "applied_strategy": optimisation_strategy,
        "weights": optimized_weights.to_dict()
    }
