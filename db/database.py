# -*- coding: utf-8 -*-
# @File：database.py
# @Time：2025/10/13 11:32
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
# db/database.py

import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

# 加载 .env 文件
load_dotenv()

# 数据库连接URL。注意 'postgresql+asyncpg' 这个方言
# host='db' 是给未来Python应用也容器化时使用的
# host='localhost' 是给当前在宿主机直接运行Python应用时使用的
DB_HOST = os.getenv("DB_HOST", "localhost")
DATABASE_URL = (f"postgresql+asyncpg://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
                f"@{DB_HOST}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}")

# 创建异步数据库引擎
engine = create_async_engine(DATABASE_URL, echo=True)  # echo=True 会打印SQL语句，便于调试

# 创建异步会话工厂
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# ORM模型将继承的基类
Base = declarative_base()


# FastAPI 依赖项，用于在请求中获取数据库会话
async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
