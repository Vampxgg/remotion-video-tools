# -*- coding: utf-8 -*-
"""异步 Redis 客户端单例。

设计要点：
- 全项目共享一个 ``redis.asyncio.Redis`` 实例，由调用方在 lifespan 中触发 ``startup`` /
  ``shutdown``，避免每次请求都建/拆连接。
- ``startup`` 时执行一次 ``PING``：失败仅 ``logger.warning`` 不阻断应用启动；后续 ``get_redis()``
  会持续返回 ``None``，调用方据此降级为"不缓存"。
- 任何运行期异常都在调用方按"安全降级"处理，本文件只暴露最小 API。
"""

from __future__ import annotations

from typing import Optional

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/redis/client.log")

try:
    import redis.asyncio as redis_asyncio
    from redis.exceptions import RedisError
except Exception as exc:  # pragma: no cover - 包未装时彻底降级
    redis_asyncio = None  # type: ignore[assignment]
    RedisError = Exception  # type: ignore[assignment, misc]
    logger.warning("redis 包未安装，Web 搜索缓存层将永远降级为不缓存：%s", exc)


# 模块级单例（lifespan 控制生命周期）
_client: Optional["redis_asyncio.Redis"] = None  # type: ignore[name-defined]
_ready: bool = False


async def startup() -> None:
    """建立连接并 PING；失败仅记录 WARN，不抛出。

    与 ``utils.settings.RedisSettings.redis_url`` 对齐，从单一配置入口取值。
    """
    global _client, _ready
    if redis_asyncio is None:
        _client = None
        _ready = False
        return

    try:
        _client = redis_asyncio.from_url(
            _settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        pong = await _client.ping()
        _ready = bool(pong)
        if _ready:
            logger.info(
                "Redis 就绪：%s:%s/db=%s prefix=%s",
                _settings.REDIS_HOST,
                _settings.REDIS_PORT,
                _settings.REDIS_DB,
                _settings.REDIS_KEY_PREFIX,
            )
        else:
            logger.warning("Redis PING 返回非真值，将以未就绪状态降级")
    except Exception as exc:
        # 包含 RedisError / OSError / 配置错误等
        logger.warning("Redis 连接失败，Web 搜索缓存层将降级为不缓存：%s", exc)
        # 关闭半成品连接
        try:
            if _client is not None:
                await _client.aclose()
        except Exception:
            pass
        _client = None
        _ready = False


async def shutdown() -> None:
    """优雅释放连接；任何异常都吞掉，不影响主进程退出。"""
    global _client, _ready
    if _client is None:
        _ready = False
        return
    try:
        await _client.aclose()
        logger.info("Redis 连接已关闭")
    except Exception as exc:
        logger.warning("Redis 关闭异常（已忽略）：%s", exc)
    finally:
        _client = None
        _ready = False


def get_redis() -> Optional["redis_asyncio.Redis"]:  # type: ignore[name-defined]
    """返回当前可用的 Redis 客户端；未就绪时返回 None，调用方据此降级。"""
    return _client if _ready else None


def is_ready() -> bool:
    return _ready
