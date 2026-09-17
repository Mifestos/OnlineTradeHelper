# src/services/analytics/models/__init__.py
"""
Пакет моделей прогноза OnlineTradeHelper.
"""
from src.services.analytics.models.base import BaseModel, ModelOutput
from src.services.analytics.models.registry import ModelRegistry

# ВАЖНО: импорт implementations триггерит декораторы @ModelRegistry.register
from src.services.analytics.models.implementations import (
    MockModel,
    ProphetModel,
    XGBoostModel,   
)

__all__ = [
    "BaseModel",
    "ModelOutput",
    "ModelRegistry",
    "MockModel",
    "ProphetModel",
    "XGBoostModel",   
]