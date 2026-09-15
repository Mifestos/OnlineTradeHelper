# src/services/analytics/main.py
import os
import sys
import numpy as np  
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from src.database.connection import db_manager
from src.services.analytics.optimiser import OptimiserFactory

app = FastAPI(title="OnlineTradeHelper AI Analytics Core")

class OptimizationRequest(BaseModel):
    selected_tickers: List[str]
    model_name: str
    optimisation_strategy: str
    risk_aversion: float = 3.0

@app.on_event("startup")
async def startup_event():
    await db_manager.init_tables()
    print("Бэкенд успешно запущен и подключен к СУБД")

@app.on_event("shutdown")
async def shutdown_event():
    await db_manager.disconnect()
    print("Соединения бэкенда с СУБД безопасно закрыты")

@app.get("/api/v1/health")
async def health_check():
    return {
        "status": "healthy", 
        "database": "connected" if db_manager.pool else "disconnected"
    }

@app.post("/api/v1/optimize")
async def optimize_portfolio_endpoint(request: OptimizationRequest):
    if not request.selected_tickers:
        raise HTTPException(status_code=400, detail="Список тикеров не может быть пустым")
        
    num_assets = len(request.selected_tickers)
    
    import pandas as pd
    mock_returns = pd.Series([0.15, 0.20, 0.12][:num_assets], index=request.selected_tickers[:3])
    if num_assets > 3:
        mock_returns = pd.Series([0.15] * num_assets, index=request.selected_tickers)
        
    mock_cov = pd.DataFrame(
        np.eye(num_assets) * 0.04, 
        index=request.selected_tickers, 
        columns=request.selected_tickers
    )
    
    min_bounds = [0.0] * num_assets
    max_bounds = [1.0] * num_assets

    try:
        optimiser = OptimiserFactory.get_optimiser(request.optimisation_strategy)
        optimized_weights = optimiser.optimize(
            expected_returns=mock_returns,
            cov_matrix=mock_cov,
            min_bounds=min_bounds,
            max_bounds=max_bounds,
            risk_aversion=request.risk_aversion,
            risk_free_rate=0.18
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    return {
        "status": "success",
        "applied_model": request.model_name,
        "applied_strategy": request.optimisation_strategy,
        "weights": optimized_weights.to_dict()
    }
