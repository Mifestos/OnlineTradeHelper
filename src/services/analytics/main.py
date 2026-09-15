# src/services/analytics/main.py
import os
import sys
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware  # Добавлен импорт CORS
from pydantic import BaseModel
from typing import List
from celery.result import AsyncResult

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from src.database.connection import db_manager
from src.services.analytics.worker import celery_app

app = FastAPI(title="OnlineTradeHelper AI Analytics Core")

# НАСТРОЙКА CORS: Разрешаем браузеру принимать ответы от бэкенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Разрешаем запросы с любых локальных файлов
    allow_credentials=True,
    allow_methods=["*"],  # Разрешаем любые методы (GET, POST)
    allow_headers=["*"],  # Разрешаем любые заголовки
)

class OptimizationRequest(BaseModel):
    selected_tickers: List[str]
    model_name: str
    optimisation_strategy: str
    risk_aversion: float = 3.0
    days_to_forecast: int = 30

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
    if not request.selected_tickers:
        raise HTTPException(status_code=400, detail="Список тикеров не может быть пустым")
    
    task = celery_app.send_task(
        "tasks.compute_portfolio_optimization",
        args=[
            request.selected_tickers,
            request.model_name,
            request.optimisation_strategy,
            request.risk_aversion,
            request.days_to_forecast
        ]
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
