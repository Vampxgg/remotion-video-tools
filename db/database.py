# -*- coding: utf-8 -*-
# @File：database.py
# @Time：2025/10/13 11:32
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
# db/database.py

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

from utils.settings import settings

# 数据库连接 URL：单点来自 settings（pydantic-settings 已在 utils/settings.py 内加载 .env）
# host='db' 是给容器化部署使用的；host='localhost' 是给宿主机直接运行使用的
DATABASE_URL = settings.url_async

# 创建异步数据库引擎；echo 打印 SQL 仅在显式 DB_ECHO=true 时启用
engine = create_async_engine(DATABASE_URL, echo=settings.DB_ECHO)

# 创建异步会话工厂
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# ORM模型将继承的基类
Base = declarative_base()


# FastAPI 依赖项，用于在请求中获取数据库会话
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
