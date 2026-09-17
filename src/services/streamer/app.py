# src/services/streamer/app.py
import os
import sys

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import asyncio
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import redis.asyncio as aioredis

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
ENV_PATH = ROOT_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

from t_tech.invest import AsyncClient, MarketDataRequest, SubscribeCandlesRequest, CandleSubscription
from t_tech.invest.constants import INVEST_GRPC_API_SANDBOX
from t_tech.invest.schemas import SubscriptionInterval
from src.database.connection import db_manager
from src.settings import REDIS_HOST, REDIS_PORT

TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")

# --- Определение режима работы ---
USE_SANDBOX = os.getenv("USE_SANDBOX", "true").lower() == "true"
SANDBOX_MODE = os.getenv("SANDBOX_MODE", "simulation").lower()  # "simulation" | "real"
SANDBOX_POLL_INTERVAL = int(os.getenv("SANDBOX_POLL_INTERVAL", "60"))

# Фактический режим:
#   "sandbox_simulation" — генерируем свечи сами (быстро, без API)
#   "sandbox_real"       — тянем реальные свечи через GetCandles
#   "production"         — реальный MarketDataStream (боевой токен)
if not USE_SANDBOX:
    STREAM_MODE = "production"
elif SANDBOX_MODE == "real":
    STREAM_MODE = "sandbox_real"
else:
    STREAM_MODE = "sandbox_simulation"

print(f"[CONFIG] USE_SANDBOX={USE_SANDBOX}, SANDBOX_MODE={SANDBOX_MODE}")
print(f"[CONFIG] STREAM_MODE={STREAM_MODE}")

# Список инструментов для симуляции в песочнице
SANDBOX_SIMULATED_TICKERS = ["SBER", "YDEX", "LKOH", "GAZP", "ROSN"]

# Базовые цены для симуляции (грубые, для наглядности)
SANDBOX_BASE_PRICES = {
    "SBER": 300.0,
    "YDEX": 4000.0,
    "LKOH": 7000.0,
    "GAZP": 130.0,
    "ROSN": 550.0,
}

# TTL флага готовности (в секундах)
READY_FLAG_TTL = 60

active_tickers = set()
figi_to_ticker = {}
ticker_to_figi = {}
subscription_queue = asyncio.Queue()


async def sync_instruments(client):
    """Загружает справочник инструментов Мосбиржи."""
    global figi_to_ticker, ticker_to_figi
    print("[INSTRUMENTS] Загрузка справочника инструментов Мосбиржи...")
    response = await client.instruments.shares()

    figi_to_ticker.clear()
    ticker_to_figi.clear()
    for share in response.instruments:
        figi_to_ticker[share.figi] = share.ticker
        ticker_to_figi[share.ticker] = share.figi
    print(f"[INSTRUMENTS] Справочник загружен: {len(figi_to_ticker)} инструментов в памяти.")


async def validate_production_connection(client):
    """
    Проверяет, что мы действительно подключены к боевому контуру T-Invest.
    """
    try:
        response = await client.users.get_accounts()
        accounts = response.accounts

        if not accounts:
            print("[VALIDATION] ⚠️ У пользователя нет доступных счетов.")
            print("[VALIDATION] Возможно, вы используете sandbox-токен с боевым эндпоинтом.")
            return False

        for acc in accounts:
            print(f"[VALIDATION] Счёт: {acc.name} | Тип: {acc.type} | Доступ: {acc.access_level}")

        print(f"[VALIDATION] ✅ Боевой контур подтверждён. Счетов: {len(accounts)}")
        return True

    except Exception as e:
        print(f"[VALIDATION] ❌ Ошибка подключения: {e}")
        print("[VALIDATION] Проверьте, что токен выпущен для боевого счёта (не sandbox).")
        return False


async def listen_redis_channels():
    """Слушает Redis Pub/Sub на канал ticker_subscriptions."""
    global active_tickers
    print(f"[REDIS PUBSUB] Подключение к шине Redis {REDIS_HOST}:{REDIS_PORT}...")
    redis_client = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("ticker_subscriptions")

    print("[REDIS PUBSUB] Слушатель запущен и ждет команд с сайта...")

    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                try:
                    command = json.loads(message["data"])
                    action = command.get("action")
                    ticker = command.get("ticker")

                    if not ticker:
                        continue

                    # В симуляции разрешаем только известные тикеры
                    if STREAM_MODE == "sandbox_simulation" and ticker not in SANDBOX_SIMULATED_TICKERS:
                        print(f"[REDIS EVENT] Симуляция: тикер {ticker} не поддерживается")
                        continue

                    # В реальных режимах тикер должен быть в справочнике
                    if STREAM_MODE != "sandbox_simulation" and ticker not in ticker_to_figi:
                        print(f"[REDIS EVENT] Тикер {ticker} не найден в справочнике")
                        continue

                    if action == "subscribe" and ticker not in active_tickers:
                        print(f"[REDIS EVENT] Подписка на тикер: {ticker}")
                        active_tickers.add(ticker)
                        # Сбрасываем флаг готовности — streamer опубликует его заново
                        await redis_client.delete(f"streamer:ready:{ticker}")
                        # В production нужно уведомить MarketDataStream
                        if STREAM_MODE == "production":
                            await subscription_queue.put({
                                "action": 1,
                                "figi": ticker_to_figi[ticker],
                                "ticker": ticker,
                            })

                    elif action == "unsubscribe" and ticker in active_tickers:
                        print(f"[REDIS EVENT] Отписка от тикера: {ticker}")
                        active_tickers.remove(ticker)
                        await redis_client.delete(f"streamer:ready:{ticker}")
                        if STREAM_MODE == "production":
                            await subscription_queue.put({
                                "action": 2,
                                "figi": ticker_to_figi[ticker],
                                "ticker": ticker,
                            })

                except Exception as parse_err:
                    print(f"[REDIS PUBSUB] Ошибка парсинга: {parse_err}")

            await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        print("[REDIS PUBSUB] Слушатель остановлен.")
    finally:
        await pubsub.unsubscribe("ticker_subscriptions")
        await redis_client.aclose()


async def simulate_candles():
    """
    РЕЖИМ sandbox_simulation.
    Генерирует случайные свечи и публикует сигнал готовности после первой свечи.
    """
    print("[SIMULATION] Запуск симуляции свечей (без обращения к T-Invest)")
    redis_writer = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    current_prices = dict(SANDBOX_BASE_PRICES)
    ready_tickers = set()

    try:
        while True:
            if not active_tickers:
                await asyncio.sleep(1.0)
                continue

            for ticker in list(active_tickers):
                if ticker not in current_prices:
                    current_prices[ticker] = 100.0

                base = current_prices[ticker]
                delta = base * random.uniform(-0.003, 0.003)
                close_price = round(base + delta, 2)
                current_prices[ticker] = close_price

                now = datetime.now()
                timestamp = int(now.timestamp())

                redis_key = f"candles:live:{ticker}"
                candle_data = json.dumps({
                    "time": now.isoformat(),
                    "open": close_price,
                    "close": close_price,
                    "volume": random.randint(100, 1000),
                })

                await redis_writer.zadd(redis_key, {candle_data: timestamp})
                await redis_writer.zremrangebyscore(redis_key, "-inf", timestamp - 259200)

                # Публикуем сигнал готовности один раз
                if ticker not in ready_tickers:
                    ready_tickers.add(ticker)
                    await redis_writer.set(f"streamer:ready:{ticker}", "1", ex=READY_FLAG_TTL)
                    await redis_writer.lpush("streamer:ready_queue", ticker)
                    print(f"[SIMULATION] ✅ Тикер {ticker} готов (сигнал отправлен)")

                print(f"🕒 [SIMULATION] Свеча {ticker} -> Close: {close_price} руб.")

            await asyncio.sleep(5)

    except asyncio.CancelledError:
        print("[SIMULATION] Остановлена.")
    finally:
        await redis_writer.aclose()


async def poll_real_candles_sandbox(client):
    """
    РЕЖИМ sandbox_real.
    Опрашивает GetCandles раз в SANDBOX_POLL_INTERVAL секунд,
    пишет реальные свечи в Redis и публикует сигнал готовности.
    """
    print(f"[SANDBOX REAL] Запуск опроса GetCandles (интервал {SANDBOX_POLL_INTERVAL}с)")
    redis_writer = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    ready_tickers = set()

    try:
        while True:
            if not active_tickers:
                await asyncio.sleep(2.0)
                continue

            for ticker in list(active_tickers):
                figi = ticker_to_figi.get(ticker)
                if not figi:
                    continue

                try:
                    now = datetime.now()
                    from_time = now - timedelta(minutes=5)

                    response = await client.market_data.get_candles(
                        figi=figi,
                        from_=from_time,
                        to=now,
                        interval=SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
                    )

                    if not response.candles:
                        print(f"[SANDBOX REAL] {ticker}: свечей за 5 мин нет")
                        continue

                    for candle in response.candles:
                        open_price = candle.open.units + (candle.open.nano / 1e9)
                        close_price = candle.close.units + (candle.close.nano / 1e9)
                        timestamp = int(candle.time.timestamp())

                        redis_key = f"candles:live:{ticker}"
                        candle_data = json.dumps({
                            "time": candle.time.isoformat(),
                            "open": open_price,
                            "close": close_price,
                            "volume": candle.volume,
                        })
                        await redis_writer.zadd(redis_key, {candle_data: timestamp})

                    cutoff = int(now.timestamp()) - 259200
                    await redis_writer.zremrangebyscore(redis_key, "-inf", cutoff)

                    if ticker not in ready_tickers:
                        ready_tickers.add(ticker)
                        await redis_writer.set(f"streamer:ready:{ticker}", "1", ex=READY_FLAG_TTL)
                        await redis_writer.lpush("streamer:ready_queue", ticker)
                        print(f"[SANDBOX REAL] ✅ Тикер {ticker} готов ({len(response.candles)} свечей)")
                    else:
                        print(f"[SANDBOX REAL] {ticker}: обновлено {len(response.candles)} свечей")

                except Exception as e:
                    print(f"[SANDBOX REAL] Ошибка для {ticker}: {type(e).__name__}: {e}")

            await asyncio.sleep(SANDBOX_POLL_INTERVAL)

    except asyncio.CancelledError:
        print("[SANDBOX REAL] Остановлен.")
    finally:
        await redis_writer.aclose()


async def handle_grpc_stream(client):
    """
    РЕЖИМ production.
    Реальный MarketDataStream от T-Invest. Публикует сигнал готовности
    после первой свечи по каждому тикеру.
    """
    redis_writer = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    ready_tickers = set()

    async def request_iterator():
        if active_tickers:
            initial_instruments = []
            for ticker in list(active_tickers):
                if ticker in ticker_to_figi:
                    initial_instruments.append(
                        CandleSubscription(
                            figi=ticker_to_figi[ticker],
                            interval=SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
                        )
                    )

            print(f"[gRPC] Стартовая подписка для: {list(active_tickers)}")
            yield MarketDataRequest(
                subscribe_candles_request=SubscribeCandlesRequest(
                    subscription_action=1,
                    instruments=initial_instruments,
                )
            )

        while True:
            cmd = await subscription_queue.get()
            yield MarketDataRequest(
                subscribe_candles_request=SubscribeCandlesRequest(
                    subscription_action=cmd["action"],
                    instruments=[
                        CandleSubscription(
                            figi=cmd["figi"],
                            interval=SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
                        )
                    ],
                )
            )
            print(f"[gRPC] Изменение подписки: {'+' if cmd['action'] == 1 else '-'} {cmd['ticker']}")
            subscription_queue.task_done()

    try:
        async for response in client.market_data_stream.market_data_stream(request_iterator()):
            if response.candle:
                candle = response.candle
                ticker = figi_to_ticker.get(candle.figi, "UNKNOWN")

                open_price = candle.open.units + (candle.open.nano / 1e9)
                close_price = candle.close.units + (candle.close.nano / 1e9)
                timestamp = int(candle.time.timestamp())

                print(f"🕒 [{candle.time.strftime('%H:%M:%S')}] Свеча {ticker} -> Close: {close_price} руб.")

                redis_key = f"candles:live:{ticker}"
                candle_data = json.dumps({
                    "time": candle.time.isoformat(),
                    "open": open_price,
                    "close": close_price,
                    "volume": candle.volume,
                })
                await redis_writer.zadd(redis_key, {candle_data: timestamp})
                await redis_writer.zremrangebyscore(redis_key, "-inf", timestamp - 259200)

                if ticker not in ready_tickers:
                    ready_tickers.add(ticker)
                    await redis_writer.set(f"streamer:ready:{ticker}", "1", ex=READY_FLAG_TTL)
                    await redis_writer.lpush("streamer:ready_queue", ticker)
                    print(f"[gRPC] ✅ Тикер {ticker} готов (сигнал отправлен)")

                async with db_manager.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO candles (time, ticker, open, close, high, low, volume)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (time, ticker) DO UPDATE SET close = EXCLUDED.close;
                        """,
                        candle.time, ticker, open_price, close_price,
                        open_price, open_price, candle.volume,
                    )

    except asyncio.CancelledError:
        pass
    finally:
        await redis_writer.aclose()


async def main():
    # Проверка токена — нужна для всех режимов, кроме чистой симуляции
    if STREAM_MODE != "sandbox_simulation":
        if not TINKOFF_TOKEN or TINKOFF_TOKEN.startswith("t.mock"):
            print("[STARTUP] Критическая ошибка: в .env не указан TINKOFF_TOKEN.")
            return

    CLEAN_TOKEN = (TINKOFF_TOKEN or "").strip().replace('"', '').replace("'", "")

    print(f"[STARTUP] Фактический режим: {STREAM_MODE}")

    await db_manager.connect()

    while True:
        try:
            # --- РЕЖИМ 1: СИМУЛЯЦИЯ (без T-Invest) ---
            if STREAM_MODE == "sandbox_simulation":
                redis_task = asyncio.create_task(listen_redis_channels())
                sim_task = asyncio.create_task(simulate_candles())
                await asyncio.gather(redis_task, sim_task)

            # --- РЕЖИМ 2: SANDBOX + GetCandles ---
            elif STREAM_MODE == "sandbox_real":
                async with AsyncClient(CLEAN_TOKEN, target=INVEST_GRPC_API_SANDBOX) as client:
                    await sync_instruments(client)

                    redis_task = asyncio.create_task(listen_redis_channels())
                    poll_task = asyncio.create_task(poll_real_candles_sandbox(client))

                    await asyncio.gather(redis_task, poll_task)

            # --- РЕЖИМ 3: PRODUCTION ---
            else:  # STREAM_MODE == "production"
                async with AsyncClient(CLEAN_TOKEN) as client:
                    if not await validate_production_connection(client):
                        print("[STARTUP] Отказ от запуска: боевой контур не подтверждён.")
                        return

                    await sync_instruments(client)

                    redis_task = asyncio.create_task(listen_redis_channels())
                    stream_task = asyncio.create_task(handle_grpc_stream(client))

                    await asyncio.gather(redis_task, stream_task)

        except (asyncio.CancelledError, KeyboardInterrupt):
            print("[SHUTDOWN] Стример остановлен.")
            break
        except Exception as e:
            print(f"[RECONNECT] Переподключение через 5 секунд... [{type(e).__name__}: {e}]")
            await asyncio.sleep(5)

    await db_manager.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[SHUTDOWN] Программа завершена.")