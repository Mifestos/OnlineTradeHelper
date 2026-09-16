# src/services/streamer/app.py
import os
import sys
import asyncio
import json
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

# ИСПРАВЛЕНО: Теперь на старте нет никаких жестко зашитых тикеров! Список абсолютно пуст.
active_tickers = set()
figi_to_ticker = {}
ticker_to_figi = {}
subscription_queue = asyncio.Queue()

async def sync_instruments(client):
    global figi_to_ticker, ticker_to_figi
    print("Загрузка справочника инструментов Мосбиржи...")
    response = await client.instruments.shares()
    
    figi_to_ticker.clear()
    ticker_to_figi.clear()
    for share in response.instruments:
        figi_to_ticker[share.figi] = share.ticker
        ticker_to_figi[share.ticker] = share.figi
    print(f"Справочник loaded: {len(figi_to_ticker)} инструментов в памяти.")

async def listen_redis_channels():
    global active_tickers
    print(f"Подключение к шине Redis {REDIS_HOST}:{REDIS_PORT}...")
    redis_client = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("ticker_subscriptions")
    
    print("Слушатель Redis Pub/Sub успешно запущен и ждет команд с сайта...")
    
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                try:
                    command = json.loads(message["data"])
                    action = command.get("action")
                    ticker = command.get("ticker")
                    
                    if not ticker or ticker not in ticker_to_figi:
                        continue
                        
                    figi = ticker_to_figi[ticker]
                    
                    if action == "subscribe" and ticker not in active_tickers:
                        print(f"[REDIS EVENT] Пользователь выбрал на сайте новую акцию: {ticker}")
                        active_tickers.add(ticker)
                        await subscription_queue.put({"action": 1, "figi": figi, "ticker": ticker})
                        
                    elif action == "unsubscribe" and ticker in active_tickers:
                        print(f"[REDIS EVENT] Пользователь отменил подписку на акцию: {ticker}")
                        active_tickers.remove(ticker)
                        await subscription_queue.put({"action": 2, "figi": figi, "ticker": ticker})
                        
                except Exception as parse_err:
                    print(f"Ошибка парсинга сообщения Redis: {parse_err}")
            
            await asyncio.sleep(0.1)
            
    except asyncio.CancelledError:
        print("Слушатель Redis остановлен.")
    finally:
        await pubsub.unsubscribe("ticker_subscriptions")
        await redis_client.close()

async def handle_grpc_stream(client):
    # Инициализируем отдельный клиент Redis для записи свечей
    redis_writer = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    
    async def request_iterator():
        if active_tickers:
            initial_instruments = []
            for ticker in list(active_tickers):
                if ticker in ticker_to_figi:
                    initial_instruments.append(
                        CandleSubscription(figi=ticker_to_figi[ticker], interval=SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE)
                    )
            
            print(f"Отправка стартовой gRPC подписки брокеру для: {list(active_tickers)}...")
            yield MarketDataRequest(
                subscribe_candles_request=SubscribeCandlesRequest(
                    subscription_action=1,
                    instruments=initial_instruments
                )
            )
        else:
            print("gRPC поток инициализирован без стартовых подписок. Ожидание первого клика на сайте...")
        
        while True:
            cmd = await subscription_queue.get()
            yield MarketDataRequest(
                subscribe_candles_request=SubscribeCandlesRequest(
                    subscription_action=cmd["action"],
                    instruments=[CandleSubscription(figi=cmd["figi"], interval=SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE)]
                )
            )
            print(f"gRPC поток изменен. {'Добавлена' if cmd['action'] == 1 else 'Удалена'} подписка на: {cmd['ticker']}")
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
                
                # --- ДОБАВЛЕНО: Сохранение онлайн-хвоста в Redis ZSET ---
                redis_key = f"candles:live:{ticker}"
                candle_data = json.dumps({
                    "time": candle.time.isoformat(),
                    "open": open_price,
                    "close": close_price,
                    "volume": candle.volume
                })
                # Добавляем свечу в Sorted Set, где score — это timestamp
                await redis_writer.zadd(redis_key, {candle_data: timestamp})
                # Храним в Redis только последние 3 дня (259200 секунд), чтобы не переполнять память
                await redis_writer.zremrangebyscore(redis_key, "-inf", timestamp - 259200)
                # --------------------------------------------------------

                # Запись в PostgreSQL (оставляем как было для истории)
                async with db_manager.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO candles (time, ticker, open, close, high, low, volume)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (time, ticker) DO UPDATE SET close = EXCLUDED.close;
                        """,
                        candle.time, ticker, open_price, close_price, open_price, open_price, candle.volume
                    )
    except asyncio.CancelledError:
        pass
    finally:
        await redis_writer.close() # Закрываем соединение при остановке стримера


async def main():
    if not TINKOFF_TOKEN or TINKOFF_TOKEN.startswith("t.mock"):
        print("Критическая ошибка: В файле .env не указан реальный TINKOFF_TOKEN.")
        return

    CLEAN_TOKEN = TINKOFF_TOKEN.strip().replace('"', '').replace("'", "")
    await db_manager.connect()
    
    while True:
        try:
            async with AsyncClient(CLEAN_TOKEN, target=INVEST_GRPC_API_SANDBOX) as client:
                await sync_instruments(client)
                
                redis_task = asyncio.create_task(listen_redis_channels())
                stream_task = asyncio.create_task(handle_grpc_stream(client))
                
                await asyncio.gather(redis_task, stream_task)
                
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("Стример остановлен.")
            break
        except Exception as e:
            print(f"Поток приостановлен. Переподключение через 5 секунд... [Тип: {type(e).__name__}, Ошибка: {e}]")
            await asyncio.sleep(5)
    
    await db_manager.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Программа завершена.")
