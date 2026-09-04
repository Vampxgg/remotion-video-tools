# -*- coding: utf-8 -*-
"""BOSS 采集专用服务入口。

该服务应以单进程运行，由一个 ``BossWorkerPoolClient`` 统一调度多个 BOSS
账号 / Chrome profile / 代理出口。主 API 的多 worker 进程只通过内部 HTTP
调用本服务，不直接初始化真实 BOSS client。
"""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from api import boss_zhipin
from utils.responses import create_standard_response, validation_exception_handler
from utils.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(boss_zhipin.lifespan_resources(app))
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
    return create_standard_response(data={"service": "boss", "workers": 1}, message="ok")


if __name__ == "__main__":
    uvicorn.run(
        "boss_server:app",
        host="127.0.0.1",
        port=2926,
        workers=1,
    )
