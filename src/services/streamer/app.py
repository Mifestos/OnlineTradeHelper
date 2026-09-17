# src/services/streamer/app.py
import os
import sys
import asyncio
import random
import json
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import redis.asyncio as aioredis

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
ENV_PATH = ROOT_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

from t_tech.invest import AsyncClient
from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX
from t_tech.invest.schemas import CandleInterval
from src.database.connection import db_manager
from src.settings import REDIS_HOST, REDIS_PORT

TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")

# Режим: "true" — песочница (симуляция), "false" — боевой T-Invest
USE_SANDBOX = os.getenv("USE_SANDBOX", "true").lower() == "true"

# Сколько дней истории загружать при старте
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "365"))

# Время ежедневного обновления (МСК, час)
DAILY_UPDATE_HOUR = int(os.getenv("DAILY_UPDATE_HOUR", "19"))

# Тикеры, которые отслеживаем
TRACKED_TICKERS = ["SBER", "YDEX", "LKOH", "GAZP", "ROSN"]

# Базовые цены для симуляции
SANDBOX_BASE_PRICES = {
    "SBER": 300.0,
    "YDEX": 4000.0,
    "LKOH": 7000.0,
    "GAZP": 130.0,
    "ROSN": 550.0,
}

STREAM_MODE = "sandbox_daily" if USE_SANDBOX else "production_daily"
print(f"[CONFIG] USE_SANDBOX={USE_SANDBOX}, STREAM_MODE={STREAM_MODE}")


async def fetch_daily_candles_from_tinkoff(client, figi: str, days: int) -> list:
    """Скачивает дневные свечи из T-Invest за последние N дней."""
    now = datetime.utcnow()
    from_time = now - timedelta(days=days)

    response = await client.market_data.get_candles(
        figi=figi,
        from_=from_time,
        to=now,
        interval=CandleInterval.CANDLE_INTERVAL_DAY,
    )

    records = []
    for candle in response.candles:
        records.append({
            "time": candle.time.replace(tzinfo=None),
            "open": candle.open.units + candle.open.nano / 1e9,
            "close": candle.close.units + candle.close.nano / 1e9,
            "high": candle.high.units + candle.high.nano / 1e9,
            "low": candle.low.units + candle.low.nano / 1e9,
            "volume": int(candle.volume),
        })
    return records


async def generate_simulated_daily_candles(ticker: str, days: int) -> list:
    """Генерирует симулированные дневные свечи (для песочницы)."""
    base_price = SANDBOX_BASE_PRICES.get(ticker, 100.0)
    current_price = base_price
    records = []

    now = datetime.utcnow()

    for i in range(days, 0, -1):
        date = now - timedelta(days=i)
        # Пропускаем выходные
        if date.weekday() >= 5:
            continue

        # Случайное блуждание ±1.5% за день
        delta = current_price * random.uniform(-0.015, 0.015)
        current_price = round(current_price + delta, 2)

        open_price = round(current_price * (1 + random.uniform(-0.005, 0.005)), 2)
        close_price = current_price
        high_price = round(max(open_price, close_price) * (1 + random.uniform(0, 0.01)), 2)
        low_price = round(min(open_price, close_price) * (1 - random.uniform(0, 0.01)), 2)

        records.append({
            "time": date.replace(hour=0, minute=0, second=0, microsecond=0),
            "open": open_price,
            "close": close_price,
            "high": high_price,
            "low": low_price,
            "volume": random.randint(100000, 1000000),
        })

    return records


async def load_history_from_tinkoff(client):
    """Загружает дневную историю по всем тикерам из T-Invest."""
    print(f"[STREAMER] Загрузка дневной истории из T-Invest ({HISTORY_DAYS} дней)...")

    shares_resp = await client.instruments.shares()
    ticker_to_figi = {s.ticker: s.figi for s in shares_resp.instruments}

    total_records = 0

    async with db_manager.pool.acquire() as conn:
        for ticker in TRACKED_TICKERS:
            figi = ticker_to_figi.get(ticker)
            if not figi:
                print(f"[STREAMER] ⚠️ FIGI для {ticker} не найден, пропуск")
                continue

            try:
                records = await fetch_daily_candles_from_tinkoff(client, figi, HISTORY_DAYS)
                if not records:
                    print(f"[STREAMER] {ticker}: свечей нет")
                    continue

                # Пишем в БД (upsert)
                rows = [
                    (r["time"], ticker, r["open"], r["close"], r["high"], r["low"], r["volume"])
                    for r in records
                ]
                await conn.executemany(
                    """
                    INSERT INTO candles (time, ticker, open, close, high, low, volume)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (time, ticker) DO UPDATE
                    SET open = EXCLUDED.open, close = EXCLUDED.close,
                        high = EXCLUDED.high, low = EXCLUDED.low, volume = EXCLUDED.volume;
                    """,
                    rows,
                )
                total_records += len(rows)
                print(f"[STREAMER] {ticker}: загружено {len(rows)} дневных свечей")

                await asyncio.sleep(0.5)  # rate limit

            except Exception as e:
                print(f"[STREAMER] Ошибка загрузки {ticker}: {e}")

    print(f"[STREAMER] ✅ Загружено {total_records} свечей через T-Invest")


async def load_history_simulated():
    """Загружает симулированную дневную историю (для песочницы)."""
    print(f"[STREAMER] Загрузка симулированной дневной истории ({HISTORY_DAYS} дней)...")

    total_records = 0

    async with db_manager.pool.acquire() as conn:
        for ticker in TRACKED_TICKERS:
            try:
                records = await generate_simulated_daily_candles(ticker, HISTORY_DAYS)
                rows = [
                    (r["time"], ticker, r["open"], r["close"], r["high"], r["low"], r["volume"])
                    for r in records
                ]
                await conn.executemany(
                    """
                    INSERT INTO candles (time, ticker, open, close, high, low, volume)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (time, ticker) DO UPDATE
                    SET open = EXCLUDED.open, close = EXCLUDED.close,
                        high = EXCLUDED.high, low = EXCLUDED.low, volume = EXCLUDED.volume;
                    """,
                    rows,
                )
                total_records += len(rows)
                print(f"[STREAMER] {ticker}: сгенерировано {len(rows)} дневных свечей")

            except Exception as e:
                print(f"[STREAMER] Ошибка генерации {ticker}: {e}")

    print(f"[STREAMER] ✅ Сгенерировано {total_records} свечей")


async def add_daily_candle_simulated(ticker: str, date: datetime):
    """Добавляет одну симулированную дневную свечу за сегодня."""
    base_price = SANDBOX_BASE_PRICES.get(ticker, 100.0)

    # Смотрим последнюю цену в БД
    async with db_manager.pool.acquire() as conn:
        last_price = await conn.fetchval(
            "SELECT close FROM candles WHERE ticker = $1 ORDER BY time DESC LIMIT 1",
            ticker,
        )

    current = last_price if last_price else base_price
    delta = current * random.uniform(-0.015, 0.015)
    close_price = round(current + delta, 2)
    open_price = round(current * (1 + random.uniform(-0.005, 0.005)), 2)
    high_price = round(max(open_price, close_price) * 1.005, 2)
    low_price = round(min(open_price, close_price) * 0.995, 2)

    async with db_manager.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO candles (time, ticker, open, close, high, low, volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (time, ticker) DO UPDATE
            SET open = EXCLUDED.open, close = EXCLUDED.close,
                high = EXCLUDED.high, low = EXCLUDED.low, volume = EXCLUDED.volume;
            """,
            date.replace(hour=0, minute=0, second=0, microsecond=0),
            ticker, open_price, close_price, high_price, low_price,
            random.randint(100000, 1000000),
        )

    print(f"[STREAMER] 🕒 {ticker}: добавлена дневная свеча {date.date()} (close={close_price})")


async def daily_updater():
    """Раз в день добавляет свежую дневную свечу."""
    print(f"[STREAMER] Запуск ежедневного обновления в {DAILY_UPDATE_HOUR}:00 МСК")

    while True:
        now = datetime.now()

        # Вычисляем время следующего запуска
        next_run = now.replace(
            hour=DAILY_UPDATE_HOUR, minute=0, second=0, microsecond=0
        )
        if next_run <= now:
            next_run += timedelta(days=1)

        wait_seconds = (next_run - now).total_seconds()
        print(f"[STREAMER] Следующее обновление через {wait_seconds / 3600:.1f} часов")
        await asyncio.sleep(wait_seconds)

        # Запускаем обновление
        print(f"[STREAMER] 🌙 Ежедневное обновление — {datetime.now()}")
        today = datetime.now()

        for ticker in TRACKED_TICKERS:
            try:
                await add_daily_candle_simulated(ticker, today)
            except Exception as e:
                print(f"[STREAMER] Ошибка {ticker}: {e}")

        print(f"[STREAMER] ✅ Ежедневное обновление завершено")


async def listen_redis_channels():
    """Слушает Redis Pub/Sub (для совместимости с API)."""
    print(f"[STREAMER] Подключение к Redis Pub/Sub {REDIS_HOST}:{REDIS_PORT}...")
    redis_client = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("ticker_subscriptions")
    print("[STREAMER] Слушатель Pub/Sub запущен")

    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                try:
                    command = json.loads(message["data"])
                    action = command.get("action")
                    ticker = command.get("ticker")
                    print(f"[STREAMER] Pub/Sub: {action} {ticker} (в дневном режиме игнорируется)")
                except Exception as e:
                    print(f"[STREAMER] Ошибка парсинга Pub/Sub: {e}")
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        print("[STREAMER] Pub/Sub остановлен")
    finally:
        await pubsub.unsubscribe("ticker_subscriptions")
        await redis_client.aclose()


async def main():
    if not USE_SANDBOX and (not TINKOFF_TOKEN or TINKOFF_TOKEN.startswith("t.mock")):
        print("[STARTUP] Критическая ошибка: TINKOFF_TOKEN не указан для production")
        return

    print(f"[STARTUP] Фактический режим: {STREAM_MODE}")
    await db_manager.connect()

    try:
        if USE_SANDBOX:
            # Песочница: генерируем историю локально
            await load_history_simulated()
        else:
            # Production: загружаем из T-Invest
            clean_token = TINKOFF_TOKEN.strip().replace('"', '').replace("'", "")
            async with AsyncClient(clean_token, target=INVEST_GRPC_API_SANDBOX) as client:
                await load_history_from_tinkoff(client)

        # Запускаем фоновые задачи
        redis_task = asyncio.create_task(listen_redis_channels())
        daily_task = asyncio.create_task(daily_updater())

        await asyncio.gather(redis_task, daily_task)

    except (asyncio.CancelledError, KeyboardInterrupt):
        print("[SHUTDOWN] Streamer остановлен")
    finally:
        await db_manager.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[SHUTDOWN] Программа завершена")