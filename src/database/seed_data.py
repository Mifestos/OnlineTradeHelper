# src/database/seed_data.py
import os
import sys
import asyncio
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from src.database.connection import db_manager

async def seed_historical_candles():
    print("Запуск генерации исторических данных для обучения...")
    await db_manager.connect()
    
    tickers = ["SBER", "GAZP", "LKOH", "YDEX", "ROSN"]
    base_prices = {"SBER": 250.0, "GAZP": 130.0, "LKOH": 7000.0, "YDEX": 4000.0, "ROSN": 500.0}
    vols = {"SBER": 0.015, "GAZP": 0.012, "LKOH": 0.01, "YDEX": 0.025, "ROSN": 0.014}
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365 * 2)
    
    # Исправлено: Добавлен параметр tz='UTC' для генерации дат с часовым поясом
    date_range = pd.date_range(start=start_date, end=end_date, freq='B', tz='UTC')
    
    records = []
    
    for ticker in tickers:
        current_price = base_prices[ticker]
        vol = vols[ticker]
        
        for current_time in date_range:
            returns = np.random.normal(0.0002, vol)
            open_p = current_price * (1 + returns * 0.2)
            close_p = current_price * (1 + returns)
            high_p = max(open_p, close_p) * (1 + abs(np.random.normal(0, 0.003)))
            low_p = min(open_p, close_p) * (1 - abs(np.random.normal(0, 0.003)))
            volume = int(np.random.normal(50000, 15000))
            
            # current_time.to_pydatetime() переводит Timestamp в чистый datetime объект Python
            records.append((
                current_time.to_pydatetime(),
                ticker,
                round(open_p, 4),
                round(close_p, 4),
                round(high_p, 4),
                round(low_p, 4),
                max(100, volume)
            ))
            current_price = close_p

    print(f"Сгенерировано {len(records)} записей. Запись в TimescaleDB...")
    
    async with db_manager.pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE candles;")
        await conn.copy_records_to_table(
            'candles',
            records=records,
            columns=['time', 'ticker', 'open', 'close', 'high', 'low', 'volume']
        )
        
    print("База данных успешно наполнена историческими котировками.")
    await db_manager.disconnect()

if __name__ == "__main__":
    asyncio.run(seed_historical_candles())
