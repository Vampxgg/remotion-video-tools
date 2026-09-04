# -*- coding: utf-8 -*-
"""主 API 中的 BOSS 采集代理路由。

主服务可以使用多个 Uvicorn worker；真实 BOSS 采集由单进程 ``boss_server``
统一调度，避免多个 API 进程争抢同一批 Chrome profile / 调试端口 / 代理出口。
"""

from __future__ import annotations

from typing import Optional

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, status
from fastapi.responses import JSONResponse

from api.boss_zhipin import BossZhipinBatchPayload, BossZhipinSearchPayload, require_api_key
from utils.responses import create_standard_response
from utils.settings import settings as _settings


router = APIRouter()


def _boss_service_url(path: str) -> str:
    return f"{_settings.BOSS_SERVICE_URL.rstrip('/')}{path}"


def _forward_headers(x_api_key: Optional[str]) -> dict[str, str]:
    api_key = _settings.BOSS_SERVICE_API_KEY or x_api_key
    return {"x-api-key": api_key} if api_key else {}


async def _forward_boss_request(
    path: str,
    payload: dict,
    *,
    x_api_key: Optional[str],
) -> JSONResponse:
    try:
        async with httpx.AsyncClient(
            timeout=_settings.BOSS_PROXY_TIMEOUT_SEC,
            trust_env=False,
        ) as client:
            resp = await client.post(
                _boss_service_url(path),
                json=payload,
                headers=_forward_headers(x_api_key),
            )
    except httpx.TimeoutException:
        return create_standard_response(
            code=status.HTTP_504_GATEWAY_TIMEOUT,
            message=f"BOSS 服务代理请求超时（{_settings.BOSS_PROXY_TIMEOUT_SEC:g}s）",
        )
    except httpx.RequestError as exc:
        return create_standard_response(
            code=status.HTTP_503_SERVICE_UNAVAILABLE,
            message=f"BOSS 服务不可用: {exc}",
        )

    try:
        content = resp.json()
    except ValueError:
        return create_standard_response(
            code=status.HTTP_502_BAD_GATEWAY,
            message=f"BOSS 服务返回非 JSON 响应: status={resp.status_code}",
        )
    return JSONResponse(status_code=resp.status_code, content=content)


@router.post(
    "/scrape/boss/search",
    summary="BOSS 直聘单次职位搜索（代理到 BOSS 专用服务）",
    dependencies=[Depends(require_api_key)],
)
async def search_boss_jobs_proxy(
    payload: BossZhipinSearchPayload,
    x_api_key: Optional[str] = Header(None),
):
    return await _forward_boss_request(
        "/api/scrape/boss/search",
        payload.model_dump(),
        x_api_key=x_api_key,
    )


@router.post(
    "/scrape/boss/batch-search",
    summary="BOSS 直聘批量职位搜索（代理到 BOSS 专用服务）",
    dependencies=[Depends(require_api_key)],
)
async def batch_search_boss_jobs_proxy(
    payload: BossZhipinBatchPayload,
    x_api_key: Optional[str] = Header(None),
):
    return await _forward_boss_request(
        "/api/scrape/boss/batch-search",
        payload.model_dump(),
        x_api_key=x_api_key,
    )


def create_router_app_for_test() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app
