# -*- coding: utf-8 -*-
"""BOSS 采集专用服务入口。

该服务应以单进程运行，由一个 ``BossWorkerPoolClient`` 统一调度多个 BOSS
账号 / Chrome profile / 代理出口。主 API 的多 worker 进程只通过内部 HTTP
调用本服务，不直接初始化真实 BOSS client。
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from api import boss_zhipin
from services.boss_zhipin_client import get_boss_client
from utils.logger import setup_module_logger
from utils.responses import create_standard_response, validation_exception_handler
from utils.settings import settings

logger = setup_module_logger(__name__, "logs/boss/boss_server.log")


async def _reconcile_boss_workers(app: FastAPI) -> None:
    """启动时以 boss-workers.json 为真相，清理孤儿+临时浏览器并对齐 Chrome worker。"""
    client = get_boss_client()
    workers = getattr(client, "workers", None)
    manager = getattr(client, "runtime_manager", None)
    if not workers or manager is None:
        logger.info("未启用 Chrome worker 托管（无 runtime_manager），跳过 reconcile")
        return
    from services.boss_worker_reconciler import reconcile_workers

    try:
        report = await asyncio.to_thread(reconcile_workers, workers, manager)
        app.state.boss_worker_report = report
    except Exception as exc:
        logger.error("BOSS worker reconcile 异常: %s", exc, exc_info=True)
        app.state.boss_worker_report = {"error": str(exc)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.boss_worker_report = None
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(boss_zhipin.lifespan_resources(app))
        await _reconcile_boss_workers(app)
        yield



app = FastAPI(
    title="X-Pilot BOSS Service",
    description="单进程 BOSS 采集服务",
    version="V1.0.1",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS,
    allow_headers=settings.CORS_ALLOW_HEADERS,
)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.include_router(boss_zhipin.router, prefix="/api", tags=["boss_zhipin"])


@app.get("/health")
async def health():
    report = getattr(app.state, "boss_worker_report", None)
    data = {"service": "boss", "workers": 1, "worker_report": report}
    return create_standard_response(data=data, message="ok")


if __name__ == "__main__":
    uvicorn.run(
        "boss_server:app",
        host="127.0.0.1",
        port=2926,
        workers=1,
    )
