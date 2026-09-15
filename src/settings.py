# src/settings.py
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Подтягиваем доступы к TimescaleDB из переменных окружения (файла .env)
DB_USER = os.getenv("DB_USER", "trade_admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "trade_secure_pass123")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "trade_db")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Подтягиваем доступы к Redis
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# Подтягиваем токен Т-Инвестиций для будущего стримера котировок
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN")
