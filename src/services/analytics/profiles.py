# src/services/analytics/profiles.py
"""
Профили риска для OnlineTradeHelper.

Профиль — это набор предустановленных параметров оптимизации:
    - модель прогноза
    - стратегия оптимизации
    - максимальная доля одного актива
    - неприятие риска (risk_aversion)
    - горизонт прогноза

Пользователь выбирает один из трёх профилей — и получает готовое
решение без необходимости разбираться в параметрах.

Продвинутые пользователи могут выбрать "custom" и настроить всё вручную.
"""
from typing import Optional


# ============================================================================
# Определения профилей
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
    },
}


# ============================================================================
# Публичные функции
# ============================================================================

def get_profile(name: str) -> Optional[dict]:
    """
    Возвращает параметры профиля по имени.
    Если профиль не найден — возвращает None.
    """
    return PROFILES.get(name)


def list_profiles() -> list:
    """
    Возвращает список профилей для API /api/v1/profiles.
    Каждый профиль содержит только поля для UI:
        - name
        - display_name
        - description
        - параметры (для отображения в UI)
    """
    result = []
    for profile in PROFILES.values():
        result.append({
            "name": profile["name"],
            "display_name": profile["display_name"],
            "description": profile["description"],
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
    """
    Возвращает параметры профиля, если он задан и существует.
    Иначе возвращает None (значит, использовать параметры из запроса).
    """
    if not profile_name:
        return None
    return PROFILES.get(profile_name)