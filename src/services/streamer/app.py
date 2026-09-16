# src/services/streamer/app.py
import os
import sys
import asyncio
import json
import random
from datetime import datetime
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

# Режим работы: "true" — песочница с симуляцией, "false" — боевой токен с реальным стримом
USE_SANDBOX = os.getenv("USE_SANDBOX", "true").lower() == "true"

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
    Запускается один раз при старте стримера в боевом режиме.

    Признаки боевого контура:
      - GetAccounts возвращает хотя бы один счёт.
      - Счета имеют тип BROKER, IIS, INVEST_BOX и т.п.
      - Нет ошибок авторизации.
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

                    # В песочнице разрешаем только симулированные тикеры
                    if USE_SANDBOX and ticker not in SANDBOX_SIMULATED_TICKERS:
                        print(f"[REDIS EVENT] Песочница: тикер {ticker} не поддерживается в симуляции")
                        continue

                    if not USE_SANDBOX and ticker not in ticker_to_figi:
                        print(f"[REDIS EVENT] Тикер {ticker} не найден в справочнике")
                        continue

                    if action == "subscribe" and ticker not in active_tickers:
                        print(f"[REDIS EVENT] Подписка на тикер: {ticker}")
                        active_tickers.add(ticker)
                        if not USE_SANDBOX:
                            await subscription_queue.put({
                                "action": 1,
                                "figi": ticker_to_figi[ticker],
                                "ticker": ticker,
                            })

                    elif action == "unsubscribe" and ticker in active_tickers:
                        print(f"[REDIS EVENT] Отписка от тикера: {ticker}")
                        active_tickers.remove(ticker)
                        if not USE_SANDBOX:
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
    Симуляция свечей для песочницы.
    Генерирует случайные свечи по выбранным тикерам раз в 5 секунд
    и пишет их в Redis в том же формате, что и реальный стрим.
    """
    print("[SIMULATION] Запуск симуляции свечей (песочница)")
    redis_writer = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    current_prices = dict(SANDBOX_BASE_PRICES)

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

                print(f"🕒 [SIMULATION] Свеча {ticker} -> Close: {close_price} руб.")

            await asyncio.sleep(5)

    except asyncio.CancelledError:
        print("[SIMULATION] Остановлена.")
    finally:
        await redis_writer.aclose()


async def handle_grpc_stream(client):
    """
    Реальный gRPC-стрим от T-Invest API.
    Работает только на боевом токене — в песочнице MarketDataStream недоступен.
    """
    redis_writer = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

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
    if not TINKOFF_TOKEN or TINKOFF_TOKEN.startswith("t.mock"):
        print("[STARTUP] Критическая ошибка: в .env не указан TINKOFF_TOKEN.")
        return

    CLEAN_TOKEN = TINKOFF_TOKEN.strip().replace('"', '').replace("'", "")

    mode = "ПЕСОЧНИЦА (симуляция)" if USE_SANDBOX else "БОЕВОЙ (реальный стрим)"
    print(f"[STARTUP] Режим: {mode}")

    await db_manager.connect()

    while True:
        try:
            if USE_SANDBOX:
                # --- РЕЖИМ ПЕСОЧНИЦЫ: симуляция свечей ---
                redis_task = asyncio.create_task(listen_redis_channels())
                sim_task = asyncio.create_task(simulate_candles())
                await asyncio.gather(redis_task, sim_task)

            else:
                # --- БОЕВОЙ РЕЖИМ: реальный MarketDataStream ---
                async with AsyncClient(CLEAN_TOKEN) as client:
                    # ✅ ВАЛИДАЦИЯ: убеждаемся, что мы действительно на боевом контуре
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