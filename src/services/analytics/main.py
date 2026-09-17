# src/services/analytics/main.py
import os
import sys
import json
from typing import List, Optional
from redis import asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from celery.result import AsyncResult
import psycopg2

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
from src.services.analytics.models import ModelRegistry
from src.services.analytics.profiles import (
    list_profiles,
    resolve_profile_or_default,
    save_custom_profile,
    delete_custom_profile,
)
from src.settings import REDIS_HOST, REDIS_PORT, DATABASE_URL

current_global_subscriptions = set()


# ============================================================================
# Модели запросов
# ============================================================================

class OptimizationRequest(BaseModel):
    selected_tickers: List[str]
    profile_name: Optional[str] = None
    model_name: Optional[str] = None
    optimisation_strategy: Optional[str] = None
    risk_aversion: float = 3.0
    days_to_forecast: int = 30
    max_asset_weight: float = 1.0


class SaveProfileRequest(BaseModel):
    name: str
    display_name: str
    description: str = ""
    model_name: str
    optimisation_strategy: str
    max_asset_weight: float
    risk_aversion: float
    days_to_forecast: int = 30


class ScheduleRequest(BaseModel):
    profile_name: Optional[str] = None
    model_name: str
    optimisation_strategy: str
    tickers: List[str]
    risk_aversion: float
    max_asset_weight: float
    run_hour: int = 19
    run_minute: int = 0


# ============================================================================
# Lifecycle
# ============================================================================

@app.on_event("startup")
async def startup_event():
    await db_manager.init_tables()
    print("Бэкенд-API успешно запущен")


@app.on_event("shutdown")
async def shutdown_event():
    await db_manager.disconnect()
    print("Соединения бэкенда безопасно закрыты")


# ============================================================================
# Модели
# ============================================================================

@app.get("/api/v1/models")
async def list_models():
    """Список доступных моделей прогноза."""
    return {"models": ModelRegistry.list_available()}


# ============================================================================
# Профили
# ============================================================================

@app.get("/api/v1/profiles")
async def list_risk_profiles():
    """Список профилей риска (дефолтные + кастомные)."""
    return {"profiles": list_profiles()}


@app.post("/api/v1/profiles/save")
async def save_profile_endpoint(request: SaveProfileRequest):
    """Сохраняет кастомный профиль пользователя."""
    if request.model_name not in ModelRegistry.list_names():
        raise HTTPException(
            status_code=400,
            detail=f"Модель '{request.model_name}' не найдена. "
                   f"Доступные: {ModelRegistry.list_names()}"
        )

    if request.name in {"conservative", "balanced", "aggressive"}:
        raise HTTPException(
            status_code=400,
            detail=f"Имя '{request.name}' зарезервировано. Выберите другое."
        )

    success = save_custom_profile({
        "name": request.name,
        "display_name": request.display_name,
        "description": request.description,
        "model_name": request.model_name,
        "optimisation_strategy": request.optimisation_strategy,
        "max_asset_weight": request.max_asset_weight,
        "risk_aversion": request.risk_aversion,
        "days_to_forecast": request.days_to_forecast,
    })

    if not success:
        raise HTTPException(status_code=500, detail="Не удалось сохранить профиль")

    return {
        "status": "success",
        "message": f"Профиль '{request.display_name}' сохранён",
    }


@app.delete("/api/v1/profiles/{profile_name}")
async def delete_profile_endpoint(profile_name: str):
    """Удаляет кастомный профиль пользователя."""
    if profile_name in {"conservative", "balanced", "aggressive"}:
        raise HTTPException(
            status_code=400,
            detail="Нельзя удалить дефолтный профиль",
        )

    success = delete_custom_profile(profile_name)
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Профиль '{profile_name}' не найден",
        )

    return {
        "status": "success",
        "message": f"Профиль '{profile_name}' удалён",
    }


# ============================================================================
# История
# ============================================================================

@app.get("/api/v1/history")
async def get_history(limit: int = 20):
    """Последние N запусков оптимизации."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT id, run_at, profile_name, model_name, optimisation_strategy,
                   tickers, weights, cash_weight, risk_free_rate,
                   max_asset_weight, risk_aversion, fallback_used, optimiser_success
            FROM optimization_runs
            ORDER BY run_at DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        result = []
        for row in rows:
            result.append({
                "id": row[0],
                "run_at": row[1].isoformat() if row[1] else None,
                "profile_name": row[2],
                "model_name": row[3],
                "optimisation_strategy": row[4],
                "tickers": row[5],
                "weights": row[6],
                "cash_weight": row[7],
                "risk_free_rate": row[8],
                "max_asset_weight": row[9],
                "risk_aversion": row[10],
                "fallback_used": row[11],
                "optimiser_success": row[12],
            })

        return {"history": result}
    except Exception as e:
        print(f"[HISTORY] Ошибка чтения: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка чтения истории: {e}")


# ============================================================================
# Расписания
# ============================================================================

@app.get("/api/v1/schedules")
async def list_schedules():
    """Список расписаний."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT id, profile_name, model_name, optimisation_strategy,
                   tickers, risk_aversion, max_asset_weight,
                   run_hour, run_minute, is_active, created_at, last_run_at
            FROM schedules
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        result = []
        for row in rows:
            result.append({
                "id": row[0],
                "profile_name": row[1],
                "model_name": row[2],
                "optimisation_strategy": row[3],
                "tickers": row[4],
                "risk_aversion": row[5],
                "max_asset_weight": row[6],
                "run_hour": row[7],
                "run_minute": row[8],
                "is_active": row[9],
                "created_at": row[10].isoformat() if row[10] else None,
                "last_run_at": row[11].isoformat() if row[11] else None,
            })

        return {"schedules": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка чтения: {e}")


@app.post("/api/v1/schedules")
async def create_schedule(request: ScheduleRequest):
    """Создаёт расписание."""
    if request.model_name not in ModelRegistry.list_names():
        raise HTTPException(
            status_code=400,
            detail=f"Модель '{request.model_name}' не найдена. "
                   f"Доступные: {ModelRegistry.list_names()}"
        )

    if not (0 <= request.run_hour <= 23):
        raise HTTPException(status_code=400, detail="run_hour должен быть 0-23")

    if not (0 <= request.run_minute <= 59):
        raise HTTPException(status_code=400, detail="run_minute должен быть 0-59")

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO schedules
                (profile_name, model_name, optimisation_strategy, tickers,
                 risk_aversion, max_asset_weight, run_hour, run_minute)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            request.profile_name,
            request.model_name,
            request.optimisation_strategy,
            request.tickers,
            request.risk_aversion,
            request.max_asset_weight,
            request.run_hour,
            request.run_minute,
        ))
        schedule_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        return {"status": "success", "schedule_id": schedule_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка создания: {e}")


@app.delete("/api/v1/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int):
    """Удаляет расписание."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()

        if deleted == 0:
            raise HTTPException(status_code=404, detail="Расписание не найдено")

        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка удаления: {e}")


# ============================================================================
# Оптимизация
# ============================================================================

@app.post("/api/v1/optimize")
async def optimize_portfolio_endpoint(request: OptimizationRequest):
    global current_global_subscriptions

    if not request.selected_tickers:
        raise HTTPException(status_code=400, detail="Список тикеров не может быть пустым")

    # Разрешаем параметры: профиль имеет приоритет над ручными настройками
    profile = resolve_profile_or_default(request.profile_name)

    if profile:
        resolved_model = profile["model_name"]
        resolved_strategy = profile["optimisation_strategy"]
        resolved_risk_aversion = profile["risk_aversion"]
        resolved_days = profile["days_to_forecast"]
        resolved_max_weight = profile["max_asset_weight"]
        resolved_profile_name = request.profile_name
        print(f"[FASTAPI] Применён профиль: {profile['display_name']}")
    else:
        resolved_model = request.model_name
        resolved_strategy = request.optimisation_strategy
        resolved_risk_aversion = request.risk_aversion
        resolved_days = request.days_to_forecast
        resolved_max_weight = request.max_asset_weight
        resolved_profile_name = None
        print(f"[FASTAPI] Ручной режим: model={resolved_model}, strategy={resolved_strategy}")

    # Валидация модели
    if resolved_model not in ModelRegistry.list_names():
        raise HTTPException(
            status_code=400,
            detail=f"Модель '{resolved_model}' не найдена. "
                   f"Доступные: {ModelRegistry.list_names()}"
        )

    # Управление подписками в Redis Pub/Sub
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
            if ticker not in request_tickers_set:
                command = {"action": "unsubscribe", "ticker": ticker}
                await redis_client.publish("ticker_subscriptions", json.dumps(command))
                current_global_subscriptions.remove(ticker)
                print(f"[FASTAPI] Отправлена команда в Redis на отмену подписки: {ticker}")

        await redis_client.close()
    except Exception as redis_err:
        print(f"Ошибка отправки события в шину Redis Pub/Sub: {redis_err}")

    # Отправляем задачу в Celery
    task = celery_app.send_task(
        "tasks.compute_portfolio_optimization",
        kwargs={
            "selected_tickers": request.selected_tickers,
            "model_name": resolved_model,
            "optimisation_strategy": resolved_strategy,
            "risk_aversion": resolved_risk_aversion,
            "days_to_forecast": resolved_days,
            "max_asset_weight": resolved_max_weight,
            "profile_name": resolved_profile_name,
        }
    )

    return {
        "status": "pending",
        "message": "Задача оптимизации отправлена.",
        "task_id": task.id,
        "applied_profile": resolved_profile_name,
    }


# ============================================================================
# Статус задачи
# ============================================================================

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