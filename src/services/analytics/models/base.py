# src/services/analytics/models/base.py
"""
Базовый контракт для всех моделей прогноза в OnlineTradeHelper.

Любая модель — Prophet, XGBoost, LSTM, ансамбль — реализует BaseModel
и возвращает ModelOutput. Worker работает ТОЛЬКО с этим контрактом
и не знает, что внутри модели.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class ModelOutput:
    """
    Универсальный выход ЛЮБОЙ модели прогноза.
    """
    expected_returns: pd.Series
    cov_matrix: Optional[pd.DataFrame] = None
    volatility: Optional[pd.Series] = None
    forecast_horizon_days: int = 30
    model_metadata: dict = field(default_factory=dict)
    # Новые поля:
    optimiser_success: bool = True          # Сошёлся ли оптимизатор
    fallback_used: bool = False             # Использован ли fallback


class BaseModel(ABC):
    """
    Абстрактный контракт для любой модели прогноза.
    
    Чтобы добавить новую модель:
        1. Унаследуйся от BaseModel.
        2. Реализуй метод fit_predict().
        3. Зарегистрируй через @ModelRegistry.register.
    
    Worker, API, БД — НЕ ТРОГАЮТСЯ при добавлении новой модели.
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Уникальное имя модели (используется в API и UI)."""
        ...
    
    @property
    def description(self) -> str:
        """Человекочитаемое описание для UI."""
        return "Модель прогноза доходностей"
    
    @property
    def category(self) -> str:
        """
        Категория модели:
            'price'  — прогнозирует цены (Prophet)
            'return' — прогнозирует доходности напрямую (XGBoost)
            'hybrid' — комбинация (ансамбль)
        """
        return "return"
    
    @abstractmethod
    def fit_predict(self, history: pd.DataFrame, **kwargs) -> ModelOutput:
        """
        Обучает модель и возвращает прогноз в универсальном формате.
        
        Args:
            history: DataFrame, index=time, columns=tickers, values=close prices.
            **kwargs: гиперпараметры (days_to_forecast, risk_aversion и т.д.).
        
        Returns:
            ModelOutput с ожидаемыми доходностями и опционально ковариацией.
        """
        ...