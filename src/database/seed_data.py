# src/database/seed_data.py
import os
import sys
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

ENV_PATH = Path(BASE_DIR) / ".env"
load_dotenv(dotenv_path=ENV_PATH)

from t_tech.invest import AsyncClient
from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX
from t_tech.invest.schemas import CandleInterval
from src.database.connection import db_manager

TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")

async def fetch_tinkoff_history(client, figi: str, ticker: str) -> list:
    """Скачивает дневные свечи за последние 3 года, разбивая запрос на 3 куска по 1 году"""
    now = datetime.utcnow()
    
    # ИСПРАВЛЕНО: Разбили историю на 3 интервала по 1 году (всего 3 года истории)
    intervals = [
        (now - timedelta(days=1095), now - timedelta(days=730)), # Первый год
        (now - timedelta(days=730), now - timedelta(days=365)),  # Второй год
        (now - timedelta(days=365), now)                         # Третий год
    ]
    
    records = []
    for start, end in intervals:
        try:
            response = await client.market_data.get_candles(
                figi=figi,
                from_=start,
                to=end,
                interval=CandleInterval.CANDLE_INTERVAL_DAY
            )
            
            for candle in response.candles:
                open_p = candle.open.units + (candle.open.nano / 1e9)
                close_p = candle.close.units + (candle.close.nano / 1e9)
                high_p = candle.high.units + (candle.high.nano / 1e9)
                low_p = candle.low.units + (candle.low.nano / 1e9)
                
                dtvan = candle.time.replace(tzinfo=None)
                
                records.append((
                    dtvan,
                    ticker,
                    round(open_p, 4),
                    round(close_p, 4),
                    round(high_p, 4),
                    round(low_p, 4),
                    int(candle.volume)
                ))
        except Exception as e:
            print(f"[TINKOFF ERROR] Ошибка загрузки интервала для {ticker}: {e}")
            
    return records

async def seed_historical_candles():
    print("Запуск скачивания РЕАЛЬНЫХ исторических котировок за 3 ГОДА через API Т-Инвестиций...")
    
    if not TINKOFF_TOKEN or TINKOFF_TOKEN.startswith("t.mock"):
        print("Ошибка: В .env файле не указан или указан неверный TINKOFF_TOKEN.")
        return

    CLEAN_TOKEN = TINKOFF_TOKEN.strip().replace('"', '').replace("'", "")
    await db_manager.connect()
    
    all_records = []
    
    async with AsyncClient(CLEAN_TOKEN, target=INVEST_GRPC_API_SANDBOX) as client:
        print("Загрузка справочника инструментов для поиска FIGI...")
        shares_resp = await client.instruments.shares()
        ticker_to_figi = {share.ticker: share.figi for share in shares_resp.instruments}
        
        target_tickers = ["SBER", "GAZP", "LKOH", "YDEX", "ROSN"]
        
        for ticker in target_tickers:
            figi = ticker_to_figi.get(ticker)
            if not figi:
                print(f"Внимание: Не найден FIGI для тикера {ticker}")
                continue
                
            print(f"Загрузка 3-летней истории для {ticker} через gRPC...")
            records = await fetch_tinkoff_history(client, figi, ticker)
            if records:
                all_records.extend(records)
                print(f"Успешно загружено {len(records)} свечей для {ticker}")
            
            # Легкая пауза 1.5 секунды, чтобы брокер не обрубил нас по лимитам частоты (Rate Limits)
            await asyncio.sleep(1.5)

    if not all_records:
        print("Ошибка: Не удалось получить данные через API Т-Банка. База не изменена.")
        await db_manager.disconnect()
        return

    # --- ДОБАВЛЕНО: Полная очистка дубликатов на стыках годов ---
    all_records = list(set(all_records))
    # -------------------------------------------------------------

    print(f"Всего собрано {len(all_records)} уникальных записей. Запись в TimescaleDB...")
    
    async with db_manager.pool.acquire() as conn:
        await conn.execute("TRUNCATE TABLE candles;")
        await conn.copy_records_to_table(
            'candles',
            records=all_records,
            columns=['time', 'ticker', 'open', 'close', 'high', 'low', 'volume']
        )
        
    print("База данных успешно наполнена 3-летними котировками из Т-Инвестиций!")
    await db_manager.disconnect()

if __name__ == "__main__":
    asyncio.run(seed_historical_candles())
