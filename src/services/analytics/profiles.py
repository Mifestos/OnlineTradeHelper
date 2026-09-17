# src/services/analytics/profiles.py
"""
Профили риска для OnlineTradeHelper.

Дефолтные профили (в коде) + кастомные профили (в БД).
"""
from typing import Optional
import psycopg2
from src.settings import DATABASE_URL


# ============================================================================
# Дефолтные профили
# ============================================================================

PROFILES = {
    "conservative": {
        "name": "conservative",
        "display_name": "🛡️ Консервативный",
        "description": "Низкий риск, стабильная доходность. Максимум 20% на одну акцию.",
        "model_name": "prophet",
        "optimisation_strategy": "max_sharpe",
        "max_asset_weight": 0.20,
        "risk_aversion": 5.0,
        "days_to_forecast": 30,
        "is_custom": False,
    },
    "balanced": {
        "name": "balanced",
        "display_name": "⚖️ Сбалансированный",
        "description": "Умеренный риск и доходность. Максимум 35% на одну акцию.",
        "model_name": "xgboost",
        "optimisation_strategy": "max_sharpe",
        "max_asset_weight": 0.35,
        "risk_aversion": 3.0,
        "days_to_forecast": 30,
        "is_custom": False,
    },
    "aggressive": {
        "name": "aggressive",
        "display_name": "🚀 Агрессивный",
        "description": "Высокий риск, максимальная доходность. Максимум 50% на одну акцию.",
        "model_name": "xgboost",
        "optimisation_strategy": "markowitz",
        "max_asset_weight": 0.50,
        "risk_aversion": 1.5,
        "days_to_forecast": 30,
        "is_custom": False,
    },
}


# ============================================================================
# Работа с БД
# ============================================================================

def _ensure_user_profiles_table():
    """Создаёт таблицу user_profiles, если её нет."""
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            id SERIAL PRIMARY KEY,
            profile_name VARCHAR(100) NOT NULL UNIQUE,
            display_name VARCHAR(200) NOT NULL,
            description TEXT,
            model_name VARCHAR(50) NOT NULL,
            optimisation_strategy VARCHAR(50) NOT NULL,
            max_asset_weight DOUBLE PRECISION NOT NULL,
            risk_aversion DOUBLE PRECISION NOT NULL,
            days_to_forecast INT NOT NULL DEFAULT 30,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    conn.commit()
    cur.close()
    conn.close()


def load_custom_profiles() -> list:
    """Загружает кастомные профили из БД."""
    try:
        _ensure_user_profiles_table()
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT profile_name, display_name, description,
                   model_name, optimisation_strategy,
                   max_asset_weight, risk_aversion, days_to_forecast
            FROM user_profiles
            ORDER BY created_at ASC
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        result = []
        for row in rows:
            result.append({
                "name": row[0],
                "display_name": row[1],
                "description": row[2] or "Пользовательский профиль",
                "model_name": row[3],
                "optimisation_strategy": row[4],
                "max_asset_weight": row[5],
                "risk_aversion": row[6],
                "days_to_forecast": row[7],
                "is_custom": True,
            })
        return result
    except Exception as e:
        print(f"[PROFILES] Ошибка загрузки кастомных профилей: {e}")
        return []


def save_custom_profile(profile: dict) -> bool:
    """Сохраняет кастомный профиль в БД."""
    try:
        _ensure_user_profiles_table()
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_profiles
                (profile_name, display_name, description, model_name,
                 optimisation_strategy, max_asset_weight, risk_aversion, days_to_forecast)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (profile_name) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                description = EXCLUDED.description,
                model_name = EXCLUDED.model_name,
                optimisation_strategy = EXCLUDED.optimisation_strategy,
                max_asset_weight = EXCLUDED.max_asset_weight,
                risk_aversion = EXCLUDED.risk_aversion,
                days_to_forecast = EXCLUDED.days_to_forecast
        """, (
            profile["name"],
            profile["display_name"],
            profile.get("description", ""),
            profile["model_name"],
            profile["optimisation_strategy"],
            profile["max_asset_weight"],
            profile["risk_aversion"],
            profile.get("days_to_forecast", 30),
        ))
        conn.commit()
        cur.close()
        conn.close()
        print(f"[PROFILES] Сохранён профиль: {profile['name']}")
        return True
    except Exception as e:
        print(f"[PROFILES] Ошибка сохранения: {e}")
        return False


def delete_custom_profile(profile_name: str) -> bool:
    """Удаляет кастомный профиль из БД."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("DELETE FROM user_profiles WHERE profile_name = %s", (profile_name,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        print(f"[PROFILES] Удалено профилей: {deleted}")
        return deleted > 0
    except Exception as e:
        print(f"[PROFILES] Ошибка удаления: {e}")
        return False


# ============================================================================
# Публичные функции
# ============================================================================

def get_profile(name: str) -> Optional[dict]:
    """Возвращает параметры профиля: сначала дефолтные, потом кастомные."""
    if name in PROFILES:
        return PROFILES[name]

    custom = load_custom_profiles()
    for profile in custom:
        if profile["name"] == name:
            return profile

    return None


def list_profiles() -> list:
    """Дефолтные + кастомные профили для API."""
    result = []

    for profile in PROFILES.values():
        result.append({
            "name": profile["name"],
            "display_name": profile["display_name"],
            "description": profile["description"],
            "is_custom": False,
            "parameters": {
                "model_name": profile["model_name"],
                "optimisation_strategy": profile["optimisation_strategy"],
                "max_asset_weight": profile["max_asset_weight"],
                "risk_aversion": profile["risk_aversion"],
                "days_to_forecast": profile["days_to_forecast"],
            },
        })

    for profile in load_custom_profiles():
        result.append({
            "name": profile["name"],
            "display_name": profile["display_name"],
            "description": profile["description"],
            "is_custom": True,
            "parameters": {
                "model_name": profile["model_name"],
                "optimisation_strategy": profile["optimisation_strategy"],
                "max_asset_weight": profile["max_asset_weight"],
                "risk_aversion": profile["risk_aversion"],
                "days_to_forecast": profile["days_to_forecast"],
            },
        })

    return result


def resolve_profile_or_default(profile_name: Optional[str]) -> Optional[dict]:
    """Возвращает параметры профиля или None."""
    if not profile_name:
        return None
    return get_profile(profile_name)