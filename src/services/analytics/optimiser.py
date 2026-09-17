# src/services/analytics/optimiser.py
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from abc import ABC, abstractmethod


class BaseOptimiser(ABC):
    @abstractmethod
    def optimize(self, expected_returns, cov_matrix, min_bounds, max_bounds, **kwargs) -> dict:
        pass


class MarkowitzOptimiser(BaseOptimiser):
    def optimize(self, expected_returns, cov_matrix, min_bounds, max_bounds, **kwargs) -> dict:
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
        
        print(f"[SCIPY / Markowitz] Успешность: {result.success}")
        print(f"[SCIPY / Markowitz] Сообщение: {result.message}")
        
        if not result.success:
            print("[SCIPY FALLBACK / Markowitz] Оптимизатор не сошёлся, использую fallback")
            weights = _fallback_weights(expected_returns, min_bounds, max_bounds, num_assets)
            return {
                "weights": weights,
                "success": False,
                "fallback_used": True,
                "message": str(result.message),
            }
        
        return {
            "weights": pd.Series(np.round(result.x, 4), index=expected_returns.index),
            "success": True,
            "fallback_used": False,
            "message": "Optimization terminated successfully",
        }


class MaxSharpeOptimiser(BaseOptimiser):
    def optimize(self, expected_returns, cov_matrix, min_bounds, max_bounds, **kwargs) -> dict:
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
        
        result = minimize(fun=objective_function, x0=initial_weights, method='SLSQP', bounds=bounds, constraints=constraints)
        
        print(f"[SCIPY / MaxSharpe] Успешность: {result.success}")
        print(f"[SCIPY / MaxSharpe] Сообщение: {result.message}")
        
        if not result.success:
            print("[SCIPY FALLBACK / MaxSharpe] Оптимизатор не сошёлся, использую fallback")
            weights = _fallback_weights(expected_returns, min_bounds, max_bounds, num_assets)
            return {
                "weights": weights,
                "success": False,
                "fallback_used": True,
                "message": str(result.message),
            }
        
        return {
            "weights": pd.Series(np.round(result.x, 4), index=expected_returns.index),
            "success": True,
            "fallback_used": False,
            "message": "Optimization terminated successfully",
        }


def _fallback_weights(expected_returns, min_bounds, max_bounds, num_assets):
    """
    Fallback: веса пропорционально прогнозам, с уважением к bounds.
    
    Логика:
    1. Распределяем веса пропорционально "силе" прогноза.
    2. Итеративно применяем bounds и перераспределяем излишек.
    3. Финальная нормализация НЕ делается — она ломает bounds.
    """
    returns_arr = expected_returns.values.astype(float)
    
    # Сдвигаем прогнозы в положительную область
    min_return = returns_arr.min()
    returns_shifted = returns_arr - min_return + 0.01
    raw_weights = returns_shifted / returns_shifted.sum()
    
    # Применяем bounds
    clipped = np.clip(raw_weights, min_bounds, max_bounds)
    
    # Итеративно перераспределяем "излишек"
    for _ in range(50):  # больше итераций для сходимости
        diff = 1.0 - clipped.sum()
        
        if abs(diff) < 0.001:
            break
        
        if diff > 0:
            # Нужно добавить — распределяем на активы ниже max
            below = clipped < np.array(max_bounds) - 0.0001
            if below.sum() == 0:
                break
            # Проверяем, сколько можно добавить
            capacity = (np.array(max_bounds) - clipped)[below].sum()
            to_add = min(diff, capacity)
            clipped[below] += to_add / below.sum()
        else:
            # Нужно убрать — вычитаем у активов выше min
            above = clipped > np.array(min_bounds) + 0.0001
            if above.sum() == 0:
                break
            # Проверяем, сколько можно убрать
            room = (clipped - np.array(min_bounds))[above].sum()
            to_remove = min(abs(diff), room)
            clipped[above] -= to_remove / above.sum()
        
        clipped = np.clip(clipped, min_bounds, max_bounds)
    
    # НЕ нормализуем в конце — это ломает bounds.
    # Если сумма всё ещё не 1 — оставляем как есть (bounds важнее).
    
    # Округляем с сохранением суммы
    rounded = np.round(clipped, 4)
    
    # Корректируем остаток от округления
    remainder = 1.0 - rounded.sum()
    if abs(remainder) > 0.0001:
        # Добавляем остаток к активу с самым большим "запасом" до max
        capacity = np.array(max_bounds) - rounded
        if capacity.max() > 0:
            idx = np.argmax(capacity)
            rounded[idx] += remainder
    
    print(f"[SCIPY FALLBACK] Прогнозы: {returns_arr}")
    print(f"[SCIPY FALLBACK] Raw weights: {raw_weights}")
    print(f"[SCIPY FALLBACK] Clipped weights: {clipped}")
    print(f"[SCIPY FALLBACK] Final (rounded): {rounded}")
    print(f"[SCIPY FALLBACK] Sum: {rounded.sum():.4f}")
    print(f"[SCIPY FALLBACK] Bounds: min={min_bounds}, max={max_bounds}")
    
    return pd.Series(rounded, index=expected_returns.index)

class OptimiserFactory:
    @staticmethod
    def get_optimiser(strategy_name):
        strategies = {
            "markowitz": MarkowitzOptimiser,
            "max_sharpe": MaxSharpeOptimiser,
        }
        if strategy_name not in strategies:
            raise ValueError(f"Стратегия '{strategy_name}' не поддерживается.")
        return strategies[strategy_name]()