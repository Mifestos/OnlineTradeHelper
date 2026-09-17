import os
import asyncpg
import redis.asyncio as aioredis
from src.settings import DATABASE_URL, REDIS_HOST, REDIS_PORT, BASE_DIR

class DatabaseManager:
    def __init__(self):
        self.pool = None
        self.redis = None

    async def connect(self):
        if not self.pool:
            self.pool = await asyncpg.create_pool(DATABASE_URL)
        if not self.redis:
            self.redis = aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    async def disconnect(self):
        if self.pool:
            await self.pool.close()
            self.pool = None
        if self.redis:
            await self.redis.close()
            self.redis = None

    async def init_tables(self):
        await self.connect()
        sql_path = os.path.join(BASE_DIR, "src", "database", "init_db.sql")
        
        if not os.path.exists(sql_path):
            raise FileNotFoundError(f"SQL file not found at: {sql_path}")
            
        with open(sql_path, "r") as f:
            sql_script = f.read()

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql_script)

db_manager = DatabaseManager()
