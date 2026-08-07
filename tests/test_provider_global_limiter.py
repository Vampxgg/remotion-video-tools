# -*- coding: utf-8 -*-
"""provider 级全局限流：Redis 不可用策略、独立 key、排队超时、租约续期。"""

import asyncio
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from services import vertex_global_limiter as lim
from services.vertex_global_limiter import (
    VertexLimiterTimeout,
    VertexLimiterUnavailable,
    vertex_global_limit,
)


class LimiterTests(TestCase):
    def test_provider_specific_keys(self):
        v = vertex_global_limit("r", "vertex")
        a = vertex_global_limit("r", "azure")
        self.assertIn("vertex_limiter", v._key)
        self.assertIn("azure_limiter", a._key)
        self.assertNotEqual(v._key, a._key)

    def test_disabled_limiter_no_redis(self):
        async def _run():
            with patch.object(lim._settings, "FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED", False):
                async with vertex_global_limit("r", "vertex") as lease:
                    return lease
        lease = asyncio.run(_run())
        self.assertFalse(lease.acquired)

    def test_redis_unavailable_fallback_base_raises(self):
        async def _run():
            with patch.object(lim._settings, "FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED", True), \
                 patch.object(lim._settings, "FILE_UNDERSTAND_GLOBAL_CONCURRENCY", 3), \
                 patch.object(lim._settings, "FILE_UNDERSTAND_LIMITER_UNAVAILABLE_POLICY", "fallback_base"), \
                 patch.object(lim.redis_client, "get_redis", return_value=None):
                async with vertex_global_limit("r", "vertex"):
                    pass
        with self.assertRaises(VertexLimiterUnavailable):
            asyncio.run(_run())

    def test_redis_unavailable_open_passes(self):
        async def _run():
            with patch.object(lim._settings, "FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED", True), \
                 patch.object(lim._settings, "FILE_UNDERSTAND_GLOBAL_CONCURRENCY", 3), \
                 patch.object(lim._settings, "FILE_UNDERSTAND_LIMITER_UNAVAILABLE_POLICY", "open"), \
                 patch.object(lim.redis_client, "get_redis", return_value=None):
                async with vertex_global_limit("r", "vertex") as lease:
                    return lease
        lease = asyncio.run(_run())
        self.assertFalse(lease.acquired)

    def test_max_wait_timeout(self):
        # Redis 一直返回未取得槽位(0)，超过最大等待应抛超时。
        fake_redis = AsyncMock()
        fake_redis.eval = AsyncMock(return_value=0)

        async def _run():
            with patch.object(lim._settings, "FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED", True), \
                 patch.object(lim._settings, "FILE_UNDERSTAND_GLOBAL_CONCURRENCY", 1), \
                 patch.object(lim._settings, "FILE_UNDERSTAND_GLOBAL_WAIT_INTERVAL_SEC", 0.01), \
                 patch.object(lim._settings, "FILE_UNDERSTAND_GLOBAL_MAX_WAIT_SEC", 0.05), \
                 patch.object(lim.redis_client, "get_redis", return_value=fake_redis):
                async with vertex_global_limit("r", "vertex"):
                    pass
        with self.assertRaises(VertexLimiterTimeout):
            asyncio.run(_run())

    def test_acquire_and_release(self):
        fake_redis = AsyncMock()
        fake_redis.eval = AsyncMock(return_value=1)
        fake_redis.zcard = AsyncMock(return_value=1)
        fake_redis.zrem = AsyncMock(return_value=1)

        async def _run():
            with patch.object(lim._settings, "FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED", True), \
                 patch.object(lim._settings, "FILE_UNDERSTAND_GLOBAL_CONCURRENCY", 2), \
                 patch.object(lim.redis_client, "get_redis", return_value=fake_redis):
                async with vertex_global_limit("r", "vertex") as lease:
                    self.assertTrue(lease.acquired)
            return True
        self.assertTrue(asyncio.run(_run()))
        fake_redis.zrem.assert_awaited()  # 释放调用
