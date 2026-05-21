# -*- coding: utf-8 -*-
"""天眼查企业数据接口与 Dify Workflow 工具入口。"""

from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import get_db
from services.tianyancha_client import TianyanchaAPIError, TianyanchaClient
from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/tianyancha/api.log")

router = APIRouter()
_client = TianyanchaClient()


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
    enrich_detail: bool = Field(False, description="是否对本页企业补拉基本信息")
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
        description="summary 只查列表；baseinfo 会按上限补企业基本信息",
    )
    force_remote: bool = Field(False, description="是否跳过搜索缓存并强制远程搜索")

    @model_validator(mode="after")
    def _check_limits(self):
        if self.limit > _settings.TIANYANCHA_DIFY_MAX_LIMIT:
            raise ValueError(f"limit 超过上限 {_settings.TIANYANCHA_DIFY_MAX_LIMIT}")
        if self.detail_level == DetailLevel.BASEINFO and not _settings.TIANYANCHA_ENABLE_AUTO_DETAIL:
            # 允许显式 detail_level，但通过响应中的成本字段体现受控调用；这里不拒绝。
            return self
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


def _error_response(exc: Exception):
    if isinstance(exc, TianyanchaAPIError):
        http_code = _http_code_for_tianyancha_error(exc.error_code)
        return create_standard_response(
            code=http_code,
            message=exc.reason,
            data={"tianyancha_error_code": exc.error_code},
        )
    if isinstance(exc, httpx.HTTPError):
        return create_standard_response(code=502, message=f"天眼查网络请求失败: {exc}")
    logger.error(f"天眼查接口异常: {exc}", exc_info=True)
    return create_standard_response(code=500, message=f"天眼查接口异常: {exc}")


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
    db: AsyncSession = Depends(get_db),
):
    try:
        data = await _client.list_local_companies(
            db,
            keyword=keyword,
            area=area,
            industry=industry,
            reg_status=reg_status,
            skip=skip,
            limit=limit,
        )
    except Exception as exc:
        return _error_response(exc)
    return create_standard_response(
        data={"companies": data, "skip": skip, "limit": limit},
        message=f"本地企业库查询完成，共返回 {len(data)} 条",
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
        )
    except Exception as exc:
        return _error_response(exc)

    message = "区域企业调研完成"
    if data.get("need_clarification"):
        message = "区域或行业需要进一步确认"
    return create_standard_response(data=data, message=message)


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
