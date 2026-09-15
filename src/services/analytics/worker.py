# src/services/analytics/worker.py
import os
import sys
import numpy as np
import pandas as pd
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

@celery_app.task(name="tasks.compute_portfolio_optimization")
def compute_portfolio_optimization(selected_tickers: list, model_name: str, optimisation_strategy: str, risk_aversion: float, days_to_forecast: int) -> dict:
    print(f"[CELERY WORKER] Начало выполнения тяжелой ИИ-задачи для тикеров: {selected_tickers}")
    
    num_assets = len(selected_tickers)
    mock_returns = pd.Series([0.15, 0.20, 0.12][:num_assets], index=selected_tickers[:3])
    if num_assets > 3:
        mock_returns = pd.Series([0.15] * num_assets, index=selected_tickers)
        
    mock_cov = pd.DataFrame(
        np.eye(num_assets) * 0.04, 
        index=selected_tickers, 
        columns=selected_tickers
    )
    
    model = ModelFactory.get_model(model_name)
    
    fake_history = pd.DataFrame(
        {t: np.random.normal(100, 2, 100) for t in selected_tickers},
        index=pd.date_range(end=pd.Timestamp.now(), periods=100, freq='D')
    )
    
    print("[CELERY WORKER] Запуск обучения модели Prophet...")
    forecasted_prices = model.fit_forecast(fake_history, days_to_forecast)
    
    min_bounds = [0.0] * num_assets
    max_bounds = [1.0] * num_assets
    
    optimiser = OptimiserFactory.get_optimiser(optimisation_strategy)
    optimized_weights = optimiser.optimize(
        expected_returns=mock_returns,
        cov_matrix=mock_cov,
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
