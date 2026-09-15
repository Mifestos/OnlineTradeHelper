# src/services/analytics/models.py
from abc import ABC, abstractmethod
import pandas as pd
from prophet import Prophet

class BaseForecastModel(ABC):
    @abstractmethod
    def fit_forecast(self, historical_data: pd.DataFrame, days_to_forecast: int) -> pd.DataFrame:
        pass

class MockForecastModel(BaseForecastModel):
    def fit_forecast(self, historical_data: pd.DataFrame, days_to_forecast: int) -> pd.DataFrame:
        forecast_dict = {}
        for ticker in historical_data.columns:
            last_price = historical_data[ticker].iloc[-1]
            forecast_dict[ticker] = [last_price * 1.01] * days_to_forecast
            
        future_index = pd.date_range(
            start=historical_data.index[-1] + pd.Timedelta(days=1), 
            periods=days_to_forecast, 
            freq='D'
        )
        return pd.DataFrame(forecast_dict, index=future_index)

class ProphetForecastModel(BaseForecastModel):
    def fit_forecast(self, historical_data: pd.DataFrame, days_to_forecast: int) -> pd.DataFrame:
        forecast_dict = {}
        
        for ticker in historical_data.columns:
            df_ticker = historical_data[[ticker]].reset_index()
            df_ticker.columns = ['ds', 'y']
            
            if df_ticker['ds'].dt.tz is not None:
                df_ticker['ds'] = df_ticker['ds'].dt.tz_localize(None)
                
            model = Prophet(daily_seasonality=False, weekly_seasonality=True, yearly_seasonality=True)
            model.fit(df_ticker)
            
            future = model.make_future_dataframe(periods=days_to_forecast, freq='D')
            forecast = model.predict(future)
            
            forecast_dict[ticker] = forecast['yhat'].iloc[-days_to_forecast:].values
            
        future_index = pd.date_range(
            start=historical_data.index[-1] + pd.Timedelta(days=1), 
            periods=days_to_forecast, 
            freq='D'
        )
        return pd.DataFrame(forecast_dict, index=future_index)

class ModelFactory:
    @staticmethod
    def get_model(model_name: str) -> BaseForecastModel:
        models = {
            "mock": MockForecastModel,
            "prophet": ProphetForecastModel
        }
        if model_name not in models:
            raise ValueError(f"Модель машинного обучения '{model_name}' не поддерживается системой.")
        return models[model_name]()
