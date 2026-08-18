# -*- coding: utf-8 -*-
"""区域企业调研（/api/tianyancha/research/region-companies）异步任务的状态存储。

为什么需要它：区域调研单请求可能翻多页 + 逐家补详情，耗时可达数十秒到数分钟，
若走同步 HTTP 长连接，前置网关（~60s）和 Dify 工具 http 节点（~110s read）会把
请求掐断（502 / Reached maximum retries）。改为「提交即返回 job_id + 轮询结果」后，
每个 HTTP 请求都很短，真正的耗时放后台跑，彻底绕开单请求超时上限。

实现要点：
- 状态存 Redis（跨 worker 共享），key 形如 ``{prefix}:tyc:region_job:{job_id}``，带 TTL。
- Redis 不可用时降级为进程内内存字典（仅同 worker 有效）：后台任务本就在受理该提交的
  worker 上执行，long-poll 命中同 worker 仍可读到状态，保证功能不因 Redis 抖动而完全失效。
- 值用 JSON 序列化；异步 API（redis.asyncio），全部函数为协程。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional

from utils import redis_client
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/tianyancha/region_jobs.log")

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
_TERMINAL = {STATUS_SUCCEEDED, STATUS_FAILED}

# Redis 不可用时的进程内兜底（仅同 worker 有效）。
_MEM_STORE: Dict[str, Dict[str, Any]] = {}


def _ttl_sec() -> int:
    return int(getattr(_settings, "TIANYANCHA_REGION_JOB_TTL_SEC", 86400) or 86400)


def _key(job_id: str) -> str:
    prefix = (getattr(_settings, "REDIS_KEY_PREFIX", "") or "").strip()
    base = f"tyc:region_job:{job_id}"
    return f"{prefix}:{base}" if prefix else base


async def _write(job_id: str, record: Dict[str, Any]) -> None:
    record["updated_at"] = time.time()
    client = redis_client.get_redis()
    if client is not None:
        try:
            await client.set(_key(job_id), json.dumps(record, ensure_ascii=False), ex=_ttl_sec())
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 写 Redis 失败，降级内存：%s", job_id, exc)
    _MEM_STORE[job_id] = record


async def _read(job_id: str) -> Optional[Dict[str, Any]]:
    client = redis_client.get_redis()
    if client is not None:
        try:
            raw = await client.get(_key(job_id))
            if raw is not None:
                return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] 读 Redis 失败，降级内存：%s", job_id, exc)
    return _MEM_STORE.get(job_id)


async def create_job(params: Dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex
    now = time.time()
    record = {
        "job_id": job_id,
        "status": STATUS_PENDING,
        "params": params,
        "result": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    await _write(job_id, record)
    logger.info("[%s] 创建区域调研任务 params=%s", job_id, params)
    return job_id


async def _update(job_id: str, **patch: Any) -> None:
    record = await _read(job_id)
    if record is None:
        # 任务记录丢失（TTL 过期 / Redis 抖动）：用补丁重建最小记录，避免轮询彻底拿不到状态。
        record = {"job_id": job_id, "created_at": time.time(), "result": None, "error": None}
    record.update(patch)
    await _write(job_id, record)


async def mark_running(job_id: str) -> None:
    await _update(job_id, status=STATUS_RUNNING)
    logger.info("[%s] 开始处理", job_id)


async def mark_succeeded(job_id: str, result: Dict[str, Any]) -> None:
    await _update(job_id, status=STATUS_SUCCEEDED, result=result, error=None)
    logger.info("[%s] 处理成功", job_id)


async def mark_failed(job_id: str, code: Any, detail: str) -> None:
    await _update(
        job_id,
        status=STATUS_FAILED,
        result=None,
        error={"code": code, "detail": detail},
    )
    logger.warning("[%s] 处理失败 code=%s detail=%s", job_id, code, detail)


async def read_job(job_id: str) -> Optional[Dict[str, Any]]:
    return await _read(job_id)
