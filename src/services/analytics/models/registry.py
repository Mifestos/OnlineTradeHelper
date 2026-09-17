# src/services/analytics/models/registry.py
"""
Реестр моделей прогноза.

Позволяет добавлять новые модели через декоратор @ModelRegistry.register
без правки worker'а, API или UI. Модели регистрируются автоматически
при импорте модуля.
"""
from typing import Dict, Type, List
from src.services.analytics.models.base import BaseModel


class ModelRegistry:
    """Реестр доступных моделей прогноза."""
    
    _models: Dict[str, Type[BaseModel]] = {}
    
    @classmethod
    def register(cls, model_class: Type[BaseModel]) -> Type[BaseModel]:
        """
        Декоратор для регистрации модели.
        
        Использование:
            @ModelRegistry.register
            class MyModel(BaseModel):
                name = "my_model"
                ...
        """
        instance = model_class()
        if instance.name in cls._models:
            raise ValueError(
                f"Модель '{instance.name}' уже зарегистрирована. "
                f"Используй другое имя."
            )
        cls._models[instance.name] = model_class
        print(f"[MODEL REGISTRY] Зарегистрирована модель: '{instance.name}' "
              f"({instance.description})")
        return model_class
    
    @classmethod
    def get(cls, name: str) -> BaseModel:
        """Возвращает экземпляр модели по имени."""
        if name not in cls._models:
            available = ", ".join(cls._models.keys()) or "(пусто)"
            raise ValueError(
                f"Модель '{name}' не зарегистрирована. "
                f"Доступные: {available}"
            )
        return cls._models[name]()
    
    @classmethod
    def list_available(cls) -> List[dict]:
        """Возвращает список моделей для API /api/v1/models."""
        result = []
        for model_class in cls._models.values():
            instance = model_class()
            result.append({
                "name": instance.name,
                "description": instance.description,
                "category": instance.category,
            })
        return result
    
    @classmethod
    def list_names(cls) -> List[str]:
        """Возвращает список имён зарегистрированных моделей."""
        return list(cls._models.keys())