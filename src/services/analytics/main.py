# src/services/analytics/main.py
import os
import sys
import json
from redis import asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
from celery.result import AsyncResult

app = FastAPI(title="OnlineTradeHelper AI Analytics Core")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from src.database.connection import db_manager
from src.services.analytics.worker import celery_app
from src.settings import REDIS_HOST, REDIS_PORT

current_global_subscriptions = set()

class OptimizationRequest(BaseModel):
    selected_tickers: List[str]
    model_name: str
    optimisation_strategy: str
    risk_aversion: float = 3.0
    days_to_forecast: int = 30
    max_asset_weight: float = 1 

@app.on_event("startup")
async def startup_event():
    await db_manager.init_tables()
    print("Бэкенд-API успешно запущен")

@app.on_event("shutdown")
async def shutdown_event():
    await db_manager.disconnect()
    print("Соединения бэкенда безопасно закрыты")

@app.post("/api/v1/optimize")
async def optimize_portfolio_endpoint(request: OptimizationRequest):
    global current_global_subscriptions
    if not request.selected_tickers:
        raise HTTPException(status_code=400, detail="Список тикеров не может быть пустым")
    
    try:
        redis_client = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        request_tickers_set = set(request.selected_tickers)
        
        for ticker in request_tickers_set:
            if ticker not in current_global_subscriptions:
                command = {"action": "subscribe", "ticker": ticker}
                await redis_client.publish("ticker_subscriptions", json.dumps(command))
                current_global_subscriptions.add(ticker)
                print(f"[FASTAPI] Отправлена команда в Redis на подписку: {ticker}")
                
        for ticker in list(current_global_subscriptions):
            if ticker not in request_tickers_set and ticker not in {"SBER", "YDEX", "LKOH"}:
                command = {"action": "unsubscribe", "ticker": ticker}
                await redis_client.publish("ticker_subscriptions", json.dumps(command))
                current_global_subscriptions.remove(ticker)
                print(f"[FASTAPI] Отправлена команда в Redis на отмену подписки: {ticker}")
                
        await redis_client.close()
    except Exception as redis_err:
        print(f"Ошибка отправки события в шину Redis Pub/Sub: {redis_err}")

    # ИСПРАВЛЕНО: Перевели отправку задачи с args на kwargs, чтобы исключить сдвиг позиций аргументов
    task = celery_app.send_task(
        "tasks.compute_portfolio_optimization",
        kwargs={
            "selected_tickers": request.selected_tickers,
            "model_name": request.model_name,
            "optimisation_strategy": request.optimisation_strategy,
            "risk_aversion": request.risk_aversion,
            "days_to_forecast": request.days_to_forecast,
            "max_asset_weight": request.max_asset_weight
        }
    )
    
    return {
        "status": "pending",
        "message": "Задача оптимизации успешно отправлена в фоновую очередь расчета.",
        "task_id": task.id
    }

@app.get("/api/v1/tasks/{task_id}")
async def get_task_status(task_id: str):
    task_result = AsyncResult(task_id, app=celery_app)
    response = {
        "task_id": task_id,
        "status": task_result.status
    }
    if task_result.status == "SUCCESS":
        response["result"] = task_result.result
    elif task_result.status == "FAILURE":
        response["error"] = str(task_result.info)
    return response
