# -*- coding: utf-8 -*-
"""
智联招聘 V2 API 端点：浏览器 JS 调 API + httpx 拉详情。

端点：
- POST /scrape/zhilian/v2/sync   — 同步小请求，直接等待返回
- POST /scrape/zhilian/v2/async  — 异步大请求，立即返回 task_id
- GET  /scrape/zhilian/v2/{task_id} — 轮询异步任务状态
"""

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from services.zhaopin_client import BrowserPool, ZhaopinSearchClient
from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/jobs/zhilian_v2.log")

router = APIRouter()

# ══════════════════════════════════════════════════════════════════════
#  模块级单例（由 lifespan 初始化/销毁）
# ══════════════════════════════════════════════════════════════════════

_search_client: Optional[ZhaopinSearchClient] = None


def get_search_client() -> ZhaopinSearchClient:
    if _search_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="V2 搜索客户端尚未初始化",
        )
    return _search_client


@asynccontextmanager
async def lifespan_resources(app):
    """由 main.py lifespan 调用：启动时初始化浏览器 tab，关闭时释放资源。"""
    global _search_client
    pool = BrowserPool()
    _search_client = ZhaopinSearchClient(pool)
    try:
        await _search_client.startup()
        logger.info("V2 ZhaopinSearchClient 就绪")
    except Exception as exc:
        logger.warning(f"V2 启动预热失败（运行时会惰性重试）: {exc}")
    yield
    logger.info("V2 ZhaopinSearchClient 正在关闭 …")
    await _search_client.shutdown()
    _search_client = None


# ══════════════════════════════════════════════════════════════════════
#  任务存储 + GC
# ══════════════════════════════════════════════════════════════════════

class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


_tasks: Dict[str, Dict[str, Any]] = {}
_inflight: Dict[str, str] = {}
_gc_task: Optional[asyncio.Task] = None


async def _gc_loop() -> None:
    """定时清理过期的任务记录。"""
    ttl = _settings.JOB_SEARCH_V2_TASK_TTL_SECONDS
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        expired_ids = []
        for tid, t in _tasks.items():
            completed_at = t.get("completed_at")
            if completed_at:
                try:
                    dt = datetime.fromisoformat(completed_at)
                    if (now - dt).total_seconds() > ttl:
                        expired_ids.append(tid)
                except Exception:
                    pass
        for tid in expired_ids:
            _tasks.pop(tid, None)
            for k, v in list(_inflight.items()):
                if v == tid:
                    _inflight.pop(k, None)
        if expired_ids:
            logger.info(f"GC 清理了 {len(expired_ids)} 个过期任务")


def _ensure_gc_started() -> None:
    global _gc_task
    if _gc_task is None or _gc_task.done():
        _gc_task = asyncio.create_task(_gc_loop())


# ══════════════════════════════════════════════════════════════════════
#  鉴权依赖
# ══════════════════════════════════════════════════════════════════════

async def require_api_key(x_api_key: str = Header(None)) -> None:
    configured_key = _settings.JOB_SEARCH_V2_API_KEY
    if not configured_key:
        return
    if x_api_key != configured_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


# ══════════════════════════════════════════════════════════════════════
#  请求模型
# ══════════════════════════════════════════════════════════════════════

class V2Payload(BaseModel):
    keywords: List[str] = Field(
        ..., min_length=1, max_length=20,
        description="搜索关键词列表", examples=[["大数据", "Java工程师"]],
    )
    provinces: List[str] = Field(
        ..., min_length=1, max_length=20,
        description="省份/城市列表", examples=[["深圳", "北京"]],
    )
    page_size: int = Field(
        3, ge=1, le=10,
        description="每个组合最大爬取页数（1-10）",
    )

    @model_validator(mode="after")
    def _check_cartesian(self):
        n = len(self.keywords) * len(self.provinces)
        limit = _settings.JOB_SEARCH_V2_MAX_COMBINATIONS
        if n > limit:
            raise ValueError(
                f"keywords × provinces = {n}，超过上限 {limit}"
            )
        return self


def _payload_fingerprint(payload: V2Payload) -> str:
    raw = json.dumps(
        {"k": sorted(payload.keywords), "p": sorted(payload.provinces), "s": payload.page_size},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha1(raw.encode()).hexdigest()


# ══════════════════════════════════════════════════════════════════════
#  端点 1: POST /sync — 同步小请求
# ══════════════════════════════════════════════════════════════════════

@router.post(
    "/scrape/zhilian/v2/sync",
    summary="[V2] 智联招聘同步搜索（小请求）",
    description=(
        "同步等待结果返回，适合组合数 <= 10 且 page_size <= 2 的小请求。\n"
        "超限自动拒绝，建议改用 /async 端点。"
    ),
    dependencies=[Depends(require_api_key)],
)
async def scrape_sync(payload: V2Payload):
    combinations = len(payload.keywords) * len(payload.provinces)
    if combinations > 10 or payload.page_size > 2:
        return create_standard_response(
            code=422,
            message=(
                f"同步端点限制: combinations <= 10 且 page_size <= 2 "
                f"(当前 combinations={combinations}, page_size={payload.page_size})，"
                f"请使用 /async 端点"
            ),
        )

    client = get_search_client()
    logger.info(
        f"[v2/sync] keywords={payload.keywords}, "
        f"provinces={payload.provinces}, page_size={payload.page_size}"
    )

    try:
        data = await asyncio.wait_for(
            client.scrape_many(payload.keywords, payload.provinces, payload.page_size),
            timeout=_settings.JOB_SEARCH_V2_SYNC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return create_standard_response(
            code=504,
            message=f"同步请求超时（{_settings.JOB_SEARCH_V2_SYNC_TIMEOUT}s）",
        )
    except RuntimeError as exc:
        return create_standard_response(code=503, message=str(exc))

    msg = f"同步搜索完成，共 {len(data)} 条"
    logger.info(f"[v2/sync] {msg}")
    return create_standard_response(data=data, message=msg)


# ══════════════════════════════════════════════════════════════════════
#  端点 2: POST /async — 异步大请求
# ══════════════════════════════════════════════════════════════════════

async def _run_async_task(
    task_id: str,
    payload: V2Payload,
    client: ZhaopinSearchClient,
) -> None:
    _tasks[task_id]["status"] = TaskStatus.PROCESSING
    try:
        data = await client.scrape_many(
            payload.keywords, payload.provinces, payload.page_size,
        )
        _tasks[task_id].update({
            "status": TaskStatus.COMPLETED,
            "data": data,
            "total": len(data),
            "completed_at": datetime.now().isoformat(),
        })
        logger.info(f"[v2/async][{task_id}] 完成，{len(data)} 条")
    except Exception as exc:
        _tasks[task_id].update({
            "status": TaskStatus.FAILED,
            "error": str(exc),
            "completed_at": datetime.now().isoformat(),
        })
        logger.error(f"[v2/async][{task_id}] 失败: {exc}", exc_info=True)
    finally:
        fp = _payload_fingerprint(payload)
        _inflight.pop(fp, None)


@router.post(
    "/scrape/zhilian/v2/async",
    summary="[V2] 智联招聘异步搜索（大请求）",
    description=(
        "立即返回 task_id，后台异步执行采集。\n"
        "通过 GET /scrape/zhilian/v2/{task_id} 轮询进度。\n"
        "相同参数在途中会自动去重，复用已有 task_id。"
    ),
    dependencies=[Depends(require_api_key)],
)
async def scrape_async(payload: V2Payload):
    _ensure_gc_started()
    client = get_search_client()

    fp = _payload_fingerprint(payload)
    existing_tid = _inflight.get(fp)
    if existing_tid and existing_tid in _tasks:
        existing = _tasks[existing_tid]
        if existing["status"] in (TaskStatus.PENDING, TaskStatus.PROCESSING):
            logger.info(f"[v2/async] 在途去重，复用 task_id={existing_tid}")
            return create_standard_response(
                data={"task_id": existing_tid, "status": existing["status"], "deduplicated": True},
                message="相同参数的任务已在运行中，复用已有 task_id",
            )

    task_id = str(uuid.uuid4())
    _tasks[task_id] = {
        "task_id": task_id,
        "status": TaskStatus.PENDING,
        "keywords": payload.keywords,
        "provinces": payload.provinces,
        "page_size": payload.page_size,
        "submitted_at": datetime.now().isoformat(),
        "data": None,
        "total": None,
        "error": None,
        "completed_at": None,
    }
    _inflight[fp] = task_id

    logger.info(
        f"[v2/async] 新任务 {task_id}: "
        f"keywords={payload.keywords}, provinces={payload.provinces}, "
        f"page_size={payload.page_size}"
    )

    asyncio.create_task(_run_async_task(task_id, payload, client))

    return create_standard_response(
        data={"task_id": task_id, "status": TaskStatus.PENDING},
        message="任务已提交，通过 GET /api/scrape/zhilian/v2/{task_id} 轮询进度",
    )


# ══════════════════════════════════════════════════════════════════════
#  端点 3: GET /{task_id} — 查询任务状态
# ══════════════════════════════════════════════════════════════════════

@router.get(
    "/scrape/zhilian/v2/{task_id}",
    summary="[V2] 查询异步任务状态",
    dependencies=[Depends(require_api_key)],
)
async def query_task(task_id: str):
    task = _tasks.get(task_id)
    if not task:
        return create_standard_response(
            code=404,
            message=f"任务 {task_id} 不存在",
        )
    return create_standard_response(data=task)
