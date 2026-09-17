# src/database/test_db.py
import os
import sys
import asyncio

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

from src.database.connection import db_manager

async def test_pipeline():
    print("Попытка подключения к инфраструктуре базы данных...")
    try:
        await db_manager.init_tables()
        print("Успешно! Подключение к TimescaleDB и Redis установлено.")
        
        async with db_manager.pool.acquire() as conn:
            table_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'candles');"
            )
            if table_exists:
                print("Гипертаблица candles успешно создана in TimescaleDB!")
            else:
                print("Внимание: Подключение есть, но таблица candles не найдена.")
                
    except Exception as e:
        print(f"Критическая ошибка при работе с БД: {e}")
    finally:
        await db_manager.disconnect()
        print("Соединения с базами данных безопасно закрыты.")

if __name__ == "__main__":
    asyncio.run(test_pipeline())
