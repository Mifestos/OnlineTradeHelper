# src/services/analytics/optimiser.py
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from abc import ABC, abstractmethod

class BaseOptimiser(ABC):
    @abstractmethod
    def optimize(self, expected_returns: pd.Series, cov_matrix: pd.DataFrame, min_bounds: list, max_bounds: list, **kwargs) -> pd.Series:
        pass

class MarkowitzOptimiser(BaseOptimiser):
    def optimize(self, expected_returns: pd.Series, cov_matrix: pd.DataFrame, min_bounds: list, max_bounds: list, **kwargs) -> pd.Series:
        risk_aversion = kwargs.get("risk_aversion", 3.0)
        num_assets = len(expected_returns)
        
        def objective_function(weights):
            portfolio_return = np.sum(expected_returns * weights)
            portfolio_variance = np.dot(weights.T, np.dot(cov_matrix, weights))
            utility = portfolio_return - 0.5 * risk_aversion * portfolio_variance
            return -utility

        constraints = ({'type': 'eq', 'fun': lambda weights: np.sum(weights) - 1.0})
        bounds = tuple((min_bounds[i], max_bounds[i]) for i in range(num_assets))
        initial_weights = np.array([1.0 / num_assets] * num_assets)
        
        result = minimize(fun=objective_function, x0=initial_weights, method='SLSQP', bounds=bounds, constraints=constraints)
        return pd.Series(np.round(result.x, 4), index=expected_returns.index)

class MaxSharpeOptimiser(BaseOptimiser):
    def optimize(self, expected_returns: pd.Series, cov_matrix: pd.DataFrame, min_bounds: list, max_bounds: list, **kwargs) -> pd.Series:
        risk_free_rate = kwargs.get("risk_free_rate", 0.05)
        num_assets = len(expected_returns)
        
        def objective_function(weights):
            portfolio_return = np.sum(expected_returns * weights)
            portfolio_volatility = np.sqrt(np.dot(weights.T, np.dot(cov_matrix, weights)))
            if portfolio_volatility == 0:
                return 0
            sharpe_ratio = (portfolio_return - risk_free_rate) / portfolio_volatility
            return -sharpe_ratio

        constraints = ({'type': 'eq', 'fun': lambda weights: np.sum(weights) - 1.0})
        bounds = tuple((min_bounds[i], max_bounds[i]) for i in range(num_assets))
        initial_weights = np.array([1.0 / num_assets] * num_assets)
        
        # Запускаем оптимизацию
        result = minimize(fun=objective_function, x0=initial_weights, method='SLSQP', bounds=bounds, constraints=constraints)
        
        # --- ЛОГ СБОЯ SCIPY ---
        print(f"[SCIPY ENGINE] Успешность: {result.success}")
        print(f"[SCIPY ENGINE] Сообщение: {result.message}")
        print(f"[SCIPY ENGINE] Сырой результат x: {result.x}")
        # ----------------------
        
        return pd.Series(np.round(result.x, 4), index=expected_returns.index)


class OptimiserFactory:
    @staticmethod
    def get_optimiser(strategy_name: str) -> BaseOptimiser:
        strategies = {
            "markowitz": MarkowitzOptimiser,
            "max_sharpe": MaxSharpeOptimiser
        }
        if strategy_name not in strategies:
            raise ValueError(f"Стратегия оптимизации '{strategy_name}' не поддерживается системой.")
        return strategies[strategy_name]()
