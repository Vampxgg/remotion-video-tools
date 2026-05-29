# -*- coding: utf-8 -*-
"""Web 搜索/抓取的 Redis 缓存薄壳。

设计要点：
- 单一入口 ``get_or_none`` / ``set``，内部全 try/except；Redis 不可用时静默回退到不缓存
- key 形态：``{REDIS_KEY_PREFIX}:web_search:v1:{kind}:{sha256_hex}``
  - ``kind`` ∈ {``search``, ``search_and_fetch``, ``fetch``}
  - 计算 sha256 时把业务请求字段做规范化排序的 JSON 序列化
- 缓存命中且调用方开启 ``include_provider_attempts`` 时，调用方可在 ``meta.attempts``
  返回一条 ``name="cache"`` 的伪 attempt；``search-and-fetch`` 还会把每条 hit 的
  ``content.source`` 改为 ``cached``，对调用方语义清晰
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from utils.redis_client import get_redis
from utils.settings import settings as _settings

logger = logging.getLogger(__name__)


def _canonical_json(value: Any) -> str:
    """按 key 排序的 JSON 序列化，作为 sha256 输入。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _prefix() -> str:
    return f"{_settings.REDIS_KEY_PREFIX}:web_search:v1"


def cache_key_search(payload: Dict[str, Any]) -> str:
    return f"{_prefix()}:search:{_hash(payload)}"


def cache_key_search_and_fetch(payload: Dict[str, Any]) -> str:
    return f"{_prefix()}:search_and_fetch:{_hash(payload)}"


def cache_key_fetch(url: str, options: Dict[str, Any]) -> str:
    return f"{_prefix()}:fetch:{_hash({'url': url, 'options': options})}"


async def get_or_none(key: str) -> Optional[Dict[str, Any]]:
    """读缓存；任何异常都吞掉返回 None，由调用方走 miss 路径。"""
    if not _settings.WEB_SEARCH_CACHE_ENABLED:
        return None
    redis = get_redis()
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
    except Exception as exc:
        logger.warning("Redis GET 失败（已降级为不缓存）：%s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Redis 缓存反序列化失败（已忽略）：%s", exc)
        return None


async def set(key: str, value: Dict[str, Any], ttl_sec: int) -> None:
    """写缓存；任何异常都吞掉。"""
    if not _settings.WEB_SEARCH_CACHE_ENABLED:
        return
    redis = get_redis()
    if redis is None:
        return
    try:
        await redis.set(key, _canonical_json(value), ex=max(1, int(ttl_sec)))
    except Exception as exc:
        logger.warning("Redis SET 失败（已忽略）：%s", exc)


def make_cache_hit_marker(elapsed_ms: int, hit_count: int) -> Dict[str, Any]:
    """构造一条"缓存命中"的伪 attempt，用于覆盖响应中的 attempts 字段。"""
    return {
        "name": "cache",
        "ok": True,
        "hit_count": hit_count,
        "elapsed_ms": max(0, int(elapsed_ms)),
        "credits_used": None,
        "error": None,
    }
