# -*- coding: utf-8 -*-
"""天眼查企业数据接口与 Dify Workflow 工具入口。"""

from enum import Enum
from typing import Any, Dict, List, Optional

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncSessionLocal, get_db
from services import tianyancha_jobs as region_jobs
from services.tianyancha_client import TianyanchaAPIError, TianyanchaClient
from utils import redis_client
from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/tianyancha/api.log")

router = APIRouter()
_client = TianyanchaClient()

# 持有异步区域调研后台任务引用，避免 asyncio 在任务结束前将其回收。
_REGION_BG_TASKS: "set[asyncio.Task]" = set()


@asynccontextmanager
async def lifespan_resources(app):
    """确保区域调研异步任务依赖的 Redis 已就绪（跨 worker 共享 job 状态）。

    Redis 不可用时 tianyancha_jobs 会降级为进程内内存兜底，功能不至于完全失效，
    但跨 worker 读取会失效，因此这里尽力保证 Redis startup。
    """
    if not redis_client.is_ready():
        await redis_client.startup()
    logger.info("tianyancha router 就绪 (redis_ready=%s)", redis_client.is_ready())
    yield
    # Redis 为全局共享单例，由应用退出统一释放，这里不 shutdown。


class DetailLevel(str, Enum):
    SUMMARY = "summary"
    BASEINFO = "baseinfo"


async def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    configured_key = _settings.TIANYANCHA_API_KEY
    if not configured_key:
        return
    if x_api_key != configured_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


class TianyanchaSearchPayload(BaseModel):
    word: Optional[str] = Field(None, description="关键词，例如 百度、智能座舱")
    category_guobiao: Optional[str] = Field(None, description="国民经济行业代码")
    area_code: Optional[str] = Field(None, description="天眼查地区代码")
    page_num: int = Field(1, ge=1, description="页码")
    page_size: int = Field(20, ge=1, description="每页条数，天眼查最大 20")
    enrich_detail: bool = Field(False, description="是否对本页企业补拉缺失或过期的基本信息")
    force_remote: bool = Field(False, description="是否跳过本地搜索缓存")
    refresh_detail: bool = Field(False, description="是否忽略详情 TTL 强制刷新详情")
    max_detail_calls: Optional[int] = Field(None, ge=0, description="本次最多补详情条数")

    @model_validator(mode="after")
    def _check_limits(self):
        if self.page_size > _settings.TIANYANCHA_MAX_PAGE_SIZE:
            raise ValueError(f"page_size 超过上限 {_settings.TIANYANCHA_MAX_PAGE_SIZE}")
        if self.page_num > _settings.TIANYANCHA_MAX_PAGES_PER_REQUEST:
            raise ValueError(f"page_num 超过上限 {_settings.TIANYANCHA_MAX_PAGES_PER_REQUEST}")
        if (
            self.max_detail_calls is not None
            and self.max_detail_calls > _settings.TIANYANCHA_MAX_DETAIL_CALLS_PER_REQUEST
        ):
            raise ValueError(
                "max_detail_calls 超过上限 "
                f"{_settings.TIANYANCHA_MAX_DETAIL_CALLS_PER_REQUEST}"
            )
        if not any([self.word, self.category_guobiao, self.area_code]):
            raise ValueError("word、category_guobiao、area_code 至少提供一个")
        return self


class RegionCompanyResearchPayload(BaseModel):
    region: str = Field(..., min_length=1, max_length=100, description="区域名称或 areaCode")
    industry: Optional[str] = Field(None, max_length=100, description="行业名称或行业代码")
    keywords: List[str] = Field(
        default_factory=list,
        max_length=10,
        description="企业搜索关键词；为空时使用 industry 或 region 兜底",
    )
    limit: int = Field(
        _settings.TIANYANCHA_DIFY_DEFAULT_LIMIT,
        ge=1,
        description="最多返回企业数",
    )
    detail_level: DetailLevel = Field(
        DetailLevel.SUMMARY,
        description="summary 返回搜索摘要；baseinfo 会尽量补齐企业基本信息",
    )
    force_remote: bool = Field(False, description="是否跳过搜索缓存并强制远程搜索")
    exhaustive: bool = Field(
        False,
        description=(
            "是否穷尽翻页：True 时忽略 limit 的够量即停，持续续翻直到该组合企业翻完"
            "（全量建档场景，如 data_server 采集）；False（默认）够 limit 即停，适合 agent 取样。"
        ),
    )

    @model_validator(mode="after")
    def _check_limits(self):
        if self.limit > _settings.TIANYANCHA_DIFY_MAX_LIMIT:
            raise ValueError(f"limit 超过上限 {_settings.TIANYANCHA_DIFY_MAX_LIMIT}")
        return self


def _http_code_for_tianyancha_error(error_code: int) -> int:
    if error_code == 300004:
        return 429
    if error_code in (300006, 300007):
        return 402
    if error_code in (300002, 300003, 300009):
        return 401
    if error_code in (300005, 300011):
        return 403
    if error_code in (300000, 300010):
        return 404
    return 502


def _error_parts(exc: Exception):
    """把异常映射为 (http_code, message, data)，供同步响应与异步 job 复用同一套错误语义。"""
    if isinstance(exc, TianyanchaAPIError):
        http_code = _http_code_for_tianyancha_error(exc.error_code)
        return http_code, exc.reason, {"tianyancha_error_code": exc.error_code}
    if isinstance(exc, httpx.HTTPError):
        return 502, f"天眼查网络请求失败: {exc}", None
    logger.error(f"天眼查接口异常: {exc}", exc_info=True)
    return 500, f"天眼查接口异常: {exc}", None


def _error_response(exc: Exception):
    code, message, data = _error_parts(exc)
    return create_standard_response(code=code, message=message, data=data)


@router.post(
    "/tianyancha/search",
    summary="天眼查企业高级搜索",
    dependencies=[Depends(require_api_key)],
)
async def search_companies(
    payload: TianyanchaSearchPayload,
    db: AsyncSession = Depends(get_db),
):
    try:
        data = await _client.search_companies(
            db,
            word=payload.word,
            category_guobiao=payload.category_guobiao,
            area_code=payload.area_code,
            page_num=payload.page_num,
            page_size=payload.page_size,
            enrich_detail=payload.enrich_detail,
            force_remote=payload.force_remote,
            refresh_detail=payload.refresh_detail,
            max_detail_calls=payload.max_detail_calls,
        )
    except Exception as exc:
        return _error_response(exc)
    return create_standard_response(data=data, message="天眼查企业搜索完成")


@router.get(
    "/tianyancha/company/{keyword}",
    summary="天眼查企业基本信息查询",
    dependencies=[Depends(require_api_key)],
)
async def get_company(
    keyword: str,
    force_remote: bool = Query(False, description="是否强制远程刷新"),
    db: AsyncSession = Depends(get_db),
):
    try:
        data = await _client.get_company(db, keyword=keyword, force_remote=force_remote)
    except Exception as exc:
        return _error_response(exc)
    return create_standard_response(data=data, message="天眼查企业详情查询完成")


@router.get(
    "/tianyancha/companies",
    summary="本地天眼查企业库查询",
    dependencies=[Depends(require_api_key)],
)
async def list_companies(
    keyword: Optional[str] = Query(None, description="企业名/统一信用代码/注册号/组织机构代码"),
    area: Optional[str] = Query(None, description="省/市/区关键字"),
    industry: Optional[str] = Query(None, description="行业关键字"),
    reg_status: Optional[str] = Query(None, description="经营状态"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    enrich_detail: bool = Query(False, description="是否为返回企业补拉缺失或过期的基本信息"),
    refresh_detail: bool = Query(False, description="补详情时是否忽略详情 TTL"),
    max_detail_calls: Optional[int] = Query(None, ge=0, description="本次最多补详情条数"),
    db: AsyncSession = Depends(get_db),
):
    if (
        max_detail_calls is not None
        and max_detail_calls > _settings.TIANYANCHA_MAX_DETAIL_CALLS_PER_REQUEST
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "max_detail_calls 超过上限 "
                f"{_settings.TIANYANCHA_MAX_DETAIL_CALLS_PER_REQUEST}"
            ),
        )
    try:
        data = await _client.list_local_companies(
            db,
            keyword=keyword,
            area=area,
            industry=industry,
            reg_status=reg_status,
            skip=skip,
            limit=limit,
            enrich_detail=enrich_detail,
            refresh_detail=refresh_detail,
            max_detail_calls=max_detail_calls,
        )
    except Exception as exc:
        return _error_response(exc)
    companies = data["companies"]
    message = f"本地企业库查询完成，共返回 {len(companies)} 条"
    if data["detail_remote_calls"]:
        message += f"，补拉详情 {data['detail_remote_calls']} 次"
    return create_standard_response(
        data=data,
        message=message,
    )


@router.post(
    "/tianyancha/research/region-companies",
    summary="Dify Workflow 区域企业调研工具",
    description=(
        "面向智能体的业务接口：用区域、行业和关键词调研区域企业。"
        "默认优先搜索缓存并去重入库，只有 detail_level=baseinfo 时才补企业基本信息。"
    ),
    dependencies=[Depends(require_api_key)],
)
async def research_region_companies(
    payload: RegionCompanyResearchPayload,
    db: AsyncSession = Depends(get_db),
):
    try:
        data = await _client.research_region_companies(
            db,
            region=payload.region,
            industry=payload.industry,
            keywords=payload.keywords,
            limit=payload.limit,
            detail_level=payload.detail_level.value,
            force_remote=payload.force_remote,
            exhaustive=payload.exhaustive,
        )
    except Exception as exc:
        return _error_response(exc)

    message = "区域企业调研完成"
    if data.get("need_clarification"):
        message = "区域或行业需要进一步确认"
    return create_standard_response(data=data, message=message)


async def _run_region_job(job_id: str, payload: "RegionCompanyResearchPayload") -> None:
    """后台执行区域调研；用独立 DB session，把结果/错误写入 job 状态。

    真正的耗时（翻页 + 并发补详情）都在这里，绕开单条 HTTP 请求的上游超时上限。
    """
    await region_jobs.mark_running(job_id)
    try:
        async with AsyncSessionLocal() as db:
            try:
                data = await _client.research_region_companies(
                    db,
                    region=payload.region,
                    industry=payload.industry,
                    keywords=payload.keywords,
                    limit=payload.limit,
                    detail_level=payload.detail_level.value,
                    force_remote=payload.force_remote,
                    exhaustive=payload.exhaustive,
                )
            finally:
                await db.close()
        await region_jobs.mark_succeeded(job_id, data)
    except Exception as exc:  # noqa: BLE001
        code, message, err_data = _error_parts(exc)
        await region_jobs.mark_failed(job_id, code, message)
        # 错误细节（如天眼查 error_code）一并落到 job，便于 result 端点透传诊断。
        if err_data is not None:
            job = await region_jobs.read_job(job_id)
            if job is not None and isinstance(job.get("error"), dict):
                job["error"]["data"] = err_data
                await region_jobs._write(job_id, job)  # noqa: SLF001


@router.post(
    "/tianyancha/research/region-companies/submit",
    summary="Dify Workflow 区域企业调研工具（异步提交，立即返回 job_id）",
    description=(
        "提交区域企业调研任务并立即返回 job_id，真正的翻页 + 并发补详情放后台执行。"
        "随后用 GET /tianyancha/research/region-companies/result/{job_id} 轮询结果，"
        "每个请求都很短，绕开网关 / Dify http 节点对单条长连接的读超时。"
    ),
    dependencies=[Depends(require_api_key)],
)
async def submit_region_companies(payload: RegionCompanyResearchPayload):
    params = payload.model_dump(mode="json")
    job_id = await region_jobs.create_job(params)
    task = asyncio.create_task(_run_region_job(job_id, payload))
    _REGION_BG_TASKS.add(task)
    task.add_done_callback(_REGION_BG_TASKS.discard)
    return create_standard_response(
        data={"job_id": job_id, "status": region_jobs.STATUS_PENDING},
        message="任务已受理，请轮询 /tianyancha/research/region-companies/result/{job_id} 获取结果",
    )


@router.get(
    "/tianyancha/research/region-companies/result/{job_id}",
    summary="查询区域企业调研异步任务结果（long-poll）",
    description=(
        "long-poll：内部等到任务完成或接近墙钟预算才返回，通常一次调用即拿到最终结果。"
        "succeeded 时 data 与同步 /region-companies 的 data 完全一致；仍在跑则返回 running。"
    ),
    dependencies=[Depends(require_api_key)],
)
async def region_companies_result(job_id: str):
    job = await region_jobs.read_job(job_id)
    if job is None:
        return create_standard_response(
            code=status.HTTP_404_NOT_FOUND,
            message="任务不存在或已过期。",
            data={"job_id": job_id, "status": "not_found"},
        )

    # long-poll：在墙钟预算内轮询本地 job 状态，直到 terminal。预算必须安全小于**上游
    # 网关**的读超时（K8s Nginx Ingress 默认 60s），否则网关先掐断长连接返回 502。
    budget = float(getattr(_settings, "TIANYANCHA_REGION_JOB_LONGPOLL_BUDGET_SEC", 25.0) or 25.0)
    started = asyncio.get_event_loop().time()
    deadline = started + budget
    interval = 1.0
    while job is not None and job.get("status") not in region_jobs._TERMINAL:  # noqa: SLF001
        if asyncio.get_event_loop().time() >= deadline:
            break
        await asyncio.sleep(interval)
        interval = min(interval * 1.5, 5.0)
        job = await region_jobs.read_job(job_id)

    if job is None:
        return create_standard_response(
            code=status.HTTP_404_NOT_FOUND,
            message="任务不存在或已过期。",
            data={"job_id": job_id, "status": "not_found"},
        )

    waited = asyncio.get_event_loop().time() - started
    job_status = job.get("status")
    logger.info(
        "[%s] result long-poll 返回 status=%s waited=%.1fs（预算 %.0fs）",
        job_id, job_status, waited, budget,
    )
    if job_status == region_jobs.STATUS_SUCCEEDED:
        data = job.get("result") or {}
        message = "区域企业调研完成"
        if isinstance(data, dict) and data.get("need_clarification"):
            message = "区域或行业需要进一步确认"
        return create_standard_response(data=data, message=message)

    if job_status == region_jobs.STATUS_FAILED:
        err = job.get("error") or {}
        code = err.get("code") if isinstance(err.get("code"), int) else 502
        return create_standard_response(
            code=code,
            message=err.get("detail") or "区域企业调研失败",
            data=err.get("data"),
        )

    # 仍在跑：让调用方稍后再查（HTTP 层仍 200，业务 code=202 表示处理中）。
    return create_standard_response(
        code=202,
        message="任务处理中，请稍后重试查询",
        data={"job_id": job_id, "status": job_status},
    )


@router.get(
    "/tianyancha/resolve/area",
    summary="解析天眼查地区代码",
    dependencies=[Depends(require_api_key)],
)
async def resolve_area(region: str = Query(..., min_length=1)):
    try:
        code, candidates = await _client.resolve_area_code(region)
    except Exception as exc:
        return _error_response(exc)
    return create_standard_response(
        data={"area_code": code, "candidates": candidates},
        message="地区代码解析完成",
    )


@router.get(
    "/tianyancha/resolve/category",
    summary="解析天眼查行业代码",
    dependencies=[Depends(require_api_key)],
)
async def resolve_category(industry: str = Query(..., min_length=1)):
    try:
        code, candidates = await _client.resolve_category_code(industry)
    except Exception as exc:
        return _error_response(exc)
    return create_standard_response(
        data={"category_guobiao": code, "candidates": candidates},
        message="行业代码解析完成",
    )
