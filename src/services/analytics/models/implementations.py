# src/services/analytics/models/implementations.py
"""
Модели прогноза для OnlineTradeHelper.

Все модели реализуют контракт BaseModel и автоматически
регистрируются в ModelRegistry через декоратор.
"""
import numpy as np
import pandas as pd
from prophet import Prophet

from src.services.analytics.models.base import BaseModel, ModelOutput
from src.services.analytics.models.registry import ModelRegistry


# ============================================================================
# MOCK MODEL
# ============================================================================

@ModelRegistry.register
class MockModel(BaseModel):
    """Заглушка. Возвращает исторические доходности как прогноз."""
    
    @property
    def name(self) -> str:
        return "mock"
    
    @property
    def description(self) -> str:
        return "Заглушка — исторические доходности как прогноз (быстро)"
    
    @property
    def category(self) -> str:
        return "return"
    
    def fit_predict(self, history: pd.DataFrame, **kwargs) -> ModelOutput:
        returns = history.pct_change(fill_method=None).dropna()
        expected = returns.mean() * 252
        cov = returns.cov() * 252
        
        return ModelOutput(
            expected_returns=expected,
            cov_matrix=cov,
            volatility=np.sqrt(np.diag(cov)),
            forecast_horizon_days=kwargs.get("days_to_forecast", 30),
            model_metadata={"type": "historical"},
        )


# ============================================================================
# PROPHET MODEL
# ============================================================================

@ModelRegistry.register
class ProphetModel(BaseModel):
    """Facebook Prophet. Прогнозирует цены, конвертирует в доходности."""
    
    @property
    def name(self) -> str:
        return "prophet"
    
    @property
    def description(self) -> str:
        return "Facebook Prophet — прогноз цен и трендов"
    
    @property
    def category(self) -> str:
        return "price"
    
    def fit_predict(self, history: pd.DataFrame, **kwargs) -> ModelOutput:
        days = kwargs.get("days_to_forecast", 30)
        forecast_dict = {}
        
        for ticker in history.columns:
            series = history[ticker].dropna()
            if len(series) < 10:
                forecast_dict[ticker] = [series.iloc[-1] if len(series) > 0 else 100.0] * days
                continue
            
            df_ticker = series.reset_index()
            df_ticker.columns = ["ds", "y"]
            
            if df_ticker["ds"].dt.tz is not None:
                df_ticker["ds"] = df_ticker["ds"].dt.tz_localize(None)
            
            model = Prophet(
                daily_seasonality=False,
                weekly_seasonality=True,
                yearly_seasonality=True,
            )
            model.fit(df_ticker)
            
            future = model.make_future_dataframe(periods=days, freq="D")
            forecast = model.predict(future)
            
            forecast_dict[ticker] = forecast["yhat"].iloc[-days:].values
        
        forecast_prices = pd.DataFrame(forecast_dict)
        mean_forecast = forecast_prices.mean()
        last_prices = history.iloc[-1]
        
        # Годовая доходность с клиппингом ±50%
        expected_returns = (mean_forecast / last_prices - 1) * (252 / days)
        expected_returns = expected_returns.clip(lower=-0.5, upper=0.5)
        
        returns = history.pct_change(fill_method=None).dropna()
        cov = returns.cov() * 252
        
        return ModelOutput(
            expected_returns=expected_returns,
            cov_matrix=cov,
            volatility=np.sqrt(np.diag(cov)),
            forecast_horizon_days=days,
            model_metadata={"type": "price_based", "model": "prophet"},
        )


# ============================================================================
# XGBOOST MODEL — прогноз доходностей на технических фичах
# ============================================================================

@ModelRegistry.register
class XGBoostModel(BaseModel):
    """
    XGBoost — ML-модель для прогноза доходностей.
    
    В отличие от Prophet (прогноз цен), XGBoost прогнозирует
    доходность следующего дня напрямую на основе:
        - лаговых доходностей (1, 5, 20 дней)
        - momentum (5, 20 дней)
        - волатильности (20 дней)
        - RSI (14 дней)
    
    Затем агрегирует дневной прогноз в годовую доходность.
    
    Для предотвращения переобучения используются:
        - L1/L2 регуляризация (reg_alpha, reg_lambda)
        - subsample, colsample_bytree
        - min_child_weight
        - клиппинг дневного прогноза (±3%)
        - клиппинг годовой доходности (±50%)
    """
    
    def __init__(
        self,
        n_estimators: int = 100,
        max_depth: int = 3,
        learning_rate: float = 0.05,
        reg_alpha: float = 1.0,
        reg_lambda: float = 5.0,
        subsample: float = 0.8,
        colsample_bytree: float = 0.8,
        min_child_weight: int = 5,
    ):
        self.params = {
            "n_estimators": n_estimators,
            "max_depth": max_depth,
            "learning_rate": learning_rate,
            "reg_alpha": reg_alpha,
            "reg_lambda": reg_lambda,
            "subsample": subsample,
            "colsample_bytree": colsample_bytree,
            "min_child_weight": min_child_weight,
            "objective": "reg:squarederror",
            "verbosity": 0,
        }
    
    @property
    def name(self) -> str:
        return "xgboost"
    
    @property
    def description(self) -> str:
        return "XGBoost — ML-прогноз доходностей на технических фичах"
    
    @property
    def category(self) -> str:
        return "return"
    
    @staticmethod
    def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
        """Индекс относительной силы (RSI)."""
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = -delta.clip(upper=0).rolling(period).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - 100 / (1 + rs)
    
    def _build_features(self, prices: pd.Series) -> pd.DataFrame:
        """Строит фичи для одного тикера."""
        series = prices.dropna()
        returns = series.pct_change()
        
        df = pd.DataFrame(index=series.index)
        df["ret_lag_1"] = returns.shift(1)
        df["ret_lag_5"] = returns.shift(5)
        df["ret_lag_20"] = returns.shift(20)
        df["momentum_5"] = series.pct_change(5)
        df["momentum_20"] = series.pct_change(20)
        df["volatility_20"] = returns.rolling(20).std()
        df["rsi_14"] = self._rsi(series, 14)
        
        return df.dropna()
    
    def fit_predict(self, history: pd.DataFrame, **kwargs) -> ModelOutput:
        try:
            import xgboost as xgb
        except ImportError:
            raise ImportError(
                "XGBoost не установлен. Добавь в pyproject.toml: xgboost = '^2.0.3'"
            )
        
        results = {}
        
        for ticker in history.columns:
            series = history[ticker].dropna()
            if len(series) < 100:
                print(f"[XGBOOST] {ticker}: мало данных ({len(series)}), прогноз = 0")
                results[ticker] = 0.0
                continue
            
            returns = series.pct_change()
            features = self._build_features(series)
            
            # Целевая переменная — доходность следующего дня
            y = returns.shift(-1)
            
            # Выравниваем индексы
            common_idx = features.index.intersection(y.dropna().index)
            if len(common_idx) < 50:
                print(f"[XGBOOST] {ticker}: мало общих индексов ({len(common_idx)}), прогноз = 0")
                results[ticker] = 0.0
                continue
            
            X = features.loc[common_idx].values
            y_aligned = y.loc[common_idx].values
            
            # Обучение
            model = xgb.XGBRegressor(**self.params)
            model.fit(X, y_aligned)
            
            # Прогноз на последний день
            last_features = features.iloc[[-1]].values
            predicted_daily = float(model.predict(last_features)[0])
            
            # Клиппинг дневного прогноза: ±3% в день
            # (это ~±750% годовых, но отсекает абсурд типа -52% за день)
            predicted_daily_clipped = max(min(predicted_daily, 0.03), -0.03)
            
            # Годовая доходность с клиппингом ±50%
            annual_return = predicted_daily_clipped * 252
            annual_return = max(min(annual_return, 0.5), -0.5)
            
            results[ticker] = annual_return
            print(f"[XGBOOST] {ticker}: дневной прогноз = {predicted_daily:.6f} "
                  f"(clip: {predicted_daily_clipped:.6f}), годовой = {annual_return:.4f}")
        
        expected_returns = pd.Series(results)
        
        # Ковариация из исторических доходностей
        returns_df = history.pct_change(fill_method=None).dropna()
        cov = returns_df.cov() * 252
        
        return ModelOutput(
            expected_returns=expected_returns,
            cov_matrix=cov,
            volatility=np.sqrt(np.diag(cov)),
            forecast_horizon_days=kwargs.get("days_to_forecast", 30),
            model_metadata={
                "type": "return_based",
                "model": "xgboost",
                "n_estimators": self.params["n_estimators"],
                "max_depth": self.params["max_depth"],
                "reg_alpha": self.params["reg_alpha"],
                "reg_lambda": self.params["reg_lambda"],
            },
        )