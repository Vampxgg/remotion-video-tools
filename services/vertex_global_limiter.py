# -*- coding: utf-8 -*-
"""跨 worker 的 Vertex 视觉理解全局限流器。

多 worker 部署时，进程内 ``asyncio.Semaphore`` 会被 worker 数放大。这里用 Redis
中的有 TTL 租约来表达全局并发槽位，确保所有 worker 共享同一并发上限。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from utils import redis_client
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/gemini/vertex_limiter.log")

_ACQUIRE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
local active = redis.call('ZCARD', KEYS[1])
if active < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
  redis.call('PEXPIRE', KEYS[1], ARGV[5])
  return 1
end
return 0
"""


class VertexLimiterUnavailable(RuntimeError):
    """全局限流不可用，调用方应按策略降级或失败。"""


@dataclass
class VertexLimitLease:
    """一次 Vertex 调用持有的全局租约。"""

    acquired: bool
    token: str
    queued_sec: float
    active_after_acquire: Optional[int]
    limit: int
    pid: int


class VertexGlobalLimiter:
    def __init__(self, request_id: str):
        self.request_id = request_id
        self.lease: Optional[VertexLimitLease] = None
        self._key = f"{_settings.REDIS_KEY_PREFIX}:file_understand:vertex_limiter"
        self._token = f"{os.getpid()}:{request_id}:{uuid.uuid4().hex}"

    async def __aenter__(self) -> VertexLimitLease:
        limit = int(getattr(_settings, "FILE_UNDERSTAND_GLOBAL_CONCURRENCY", 0) or 0)
        enabled = bool(getattr(_settings, "FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED", True))
        if not enabled or limit <= 0:
            self.lease = VertexLimitLease(
                acquired=False,
                token=self._token,
                queued_sec=0.0,
                active_after_acquire=None,
                limit=limit,
                pid=os.getpid(),
            )
            return self.lease

        redis = redis_client.get_redis()
        if redis is None:
            policy = (
                getattr(_settings, "FILE_UNDERSTAND_LIMITER_UNAVAILABLE_POLICY", "fallback_base")
                or "fallback_base"
            ).strip().lower()
            msg = f"Redis 全局限流不可用 policy={policy}"
            if policy == "open":
                logger.warning("[%s] %s，临时放开 Vertex 调用 pid=%s", self.request_id, msg, os.getpid())
                self.lease = VertexLimitLease(False, self._token, 0.0, None, limit, os.getpid())
                return self.lease
            raise VertexLimiterUnavailable(msg)

        ttl_ms = int(
            float(getattr(_settings, "FILE_UNDERSTAND_GLOBAL_LEASE_TTL_SEC", 600) or 600)
            * 1000
        )
        interval = float(getattr(_settings, "FILE_UNDERSTAND_GLOBAL_WAIT_INTERVAL_SEC", 0.5) or 0.5)
        started = time.time()

        while True:
            now_ms = int(time.time() * 1000)
            expire_before = now_ms - ttl_ms
            try:
                acquired = await redis.eval(
                    _ACQUIRE_SCRIPT,
                    1,
                    self._key,
                    now_ms,
                    expire_before,
                    limit,
                    self._token,
                    ttl_ms,
                )
            except Exception as exc:  # noqa: BLE001
                raise VertexLimiterUnavailable(f"Redis 全局限流 acquire 失败：{exc}") from exc

            if int(acquired or 0) == 1:
                queued = time.time() - started
                active = await self._active_count(redis)
                self.lease = VertexLimitLease(
                    acquired=True,
                    token=self._token,
                    queued_sec=queued,
                    active_after_acquire=active,
                    limit=limit,
                    pid=os.getpid(),
                )
                return self.lease

            await asyncio.sleep(interval)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self.lease or not self.lease.acquired:
            return
        redis = redis_client.get_redis()
        if redis is None:
            logger.warning(
                "[%s] 释放 Vertex 全局租约时 Redis 不可用 token=%s pid=%s",
                self.request_id,
                self._token,
                os.getpid(),
            )
            return
        try:
            await redis.zrem(self._key, self._token)
        except Exception as release_exc:  # noqa: BLE001
            logger.warning(
                "[%s] 释放 Vertex 全局租约失败 token=%s pid=%s: %s",
                self.request_id,
                self._token,
                os.getpid(),
                release_exc,
            )

    async def _active_count(self, redis) -> Optional[int]:
        try:
            return int(await redis.zcard(self._key))
        except Exception:  # noqa: BLE001
            return None


def vertex_global_limit(request_id: str) -> VertexGlobalLimiter:
    return VertexGlobalLimiter(request_id)
