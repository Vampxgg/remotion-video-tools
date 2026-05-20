# -*- coding: utf-8 -*-
"""BOSS 直聘职位采集接口。"""

import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from services.boss_zhipin_client import BossZhipinClient
from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/jobs/boss_zhipin.log")

router = APIRouter()
_client = BossZhipinClient()


async def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    configured_key = _settings.BOSS_ZHIPIN_API_KEY
    if not configured_key:
        return
    if x_api_key != configured_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


class BossZhipinSearchPayload(BaseModel):
    keyword: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="搜索关键词",
        examples=["智能网联汽车技术"],
    )
    city_code: int = Field(
        101280600,
        description="BOSS 城市编码，例如深圳 101280600",
        examples=[101280600],
    )
    max_pages: int = Field(
        1,
        ge=1,
        description="最多采集页数，不代表每页条数",
    )
    max_items: Optional[int] = Field(
        None,
        ge=1,
        description="最多返回职位数；为空则按 max_pages 返回",
    )
    include_raw: bool = Field(
        False,
        description="是否返回 BOSS 原始字段；对外接口默认关闭",
    )

    @model_validator(mode="after")
    def _check_limits(self):
        if self.max_pages > _settings.BOSS_ZHIPIN_MAX_PAGES:
            raise ValueError(
                f"max_pages={self.max_pages} 超过上限 "
                f"{_settings.BOSS_ZHIPIN_MAX_PAGES}"
            )
        if self.max_items and self.max_items > _settings.BOSS_ZHIPIN_MAX_ITEMS_PER_QUERY:
            raise ValueError(
                f"max_items={self.max_items} 超过上限 "
                f"{_settings.BOSS_ZHIPIN_MAX_ITEMS_PER_QUERY}"
            )
        return self


class BossZhipinBatchPayload(BaseModel):
    keywords: List[str] = Field(
        ..., min_length=1, max_length=10,
        description="搜索关键词列表",
        examples=[["智能网联汽车技术", "智能座舱"]],
    )
    city_codes: List[int] = Field(
        default_factory=lambda: [101280600],
        min_length=1,
        max_length=10,
        description="BOSS 城市编码，例如深圳 101280600",
        examples=[[101280600]],
    )
    max_pages: int = Field(
        1,
        ge=1,
        description="每个关键词/城市组合最多采集页数，不代表每页条数",
    )
    max_items_per_query: Optional[int] = Field(
        None,
        ge=1,
        description="每个关键词/城市组合最多返回职位数；为空则按 max_pages 返回",
    )
    include_raw: bool = Field(
        False,
        description="是否返回 BOSS 原始字段；对外接口默认关闭",
    )

    @model_validator(mode="after")
    def _check_limits(self):
        if self.max_pages > _settings.BOSS_ZHIPIN_MAX_PAGES:
            raise ValueError(
                f"max_pages={self.max_pages} 超过上限 "
                f"{_settings.BOSS_ZHIPIN_MAX_PAGES}"
            )
        if (
            self.max_items_per_query
            and self.max_items_per_query > _settings.BOSS_ZHIPIN_MAX_ITEMS_PER_QUERY
        ):
            raise ValueError(
                f"max_items_per_query={self.max_items_per_query} 超过上限 "
                f"{_settings.BOSS_ZHIPIN_MAX_ITEMS_PER_QUERY}"
            )
        combinations = len(self.keywords) * len(self.city_codes)
        if combinations > _settings.BOSS_ZHIPIN_MAX_COMBINATIONS:
            raise ValueError(
                f"keywords × city_codes = {combinations}，超过上限 "
                f"{_settings.BOSS_ZHIPIN_MAX_COMBINATIONS}"
            )
        return self


async def _run_boss_search(
    *,
    keywords: List[str],
    city_codes: List[int],
    max_pages: int,
    max_items_per_query: Optional[int],
    include_raw: bool,
    log_scope: str,
):
    logger.info(
        f"[{log_scope}] keywords={keywords}, city_codes={city_codes}, "
        f"max_pages={max_pages}, max_items_per_query={max_items_per_query}, "
        f"include_raw={include_raw}"
    )

    try:
        data = await asyncio.wait_for(
            _client.scrape_many(
                keywords,
                city_codes,
                max_pages,
                max_items_per_query,
                include_raw,
            ),
            timeout=_settings.BOSS_ZHIPIN_SYNC_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        return create_standard_response(
            code=504,
            message=f"BOSS 同步请求超时（{_settings.BOSS_ZHIPIN_SYNC_TIMEOUT_SEC}s）",
        )
    except RuntimeError as exc:
        logger.warning(f"[{log_scope}] 采集失败: {exc}")
        return create_standard_response(code=503, message=str(exc))
    except Exception as exc:
        logger.error(f"[{log_scope}] 未预期异常: {exc}", exc_info=True)
        return create_standard_response(code=500, message=f"BOSS 采集异常: {exc}")

    total = data.get("summary", {}).get("total_jobs", 0)
    return create_standard_response(data=data, message=f"BOSS 搜索完成，共 {total} 条")


@router.post(
    "/scrape/boss/search",
    summary="BOSS 直聘单次职位搜索",
    description=(
        "面向对外调用的单关键词、单城市搜索接口。\n"
        "复用本机已登录 Chrome 调试端口，监听页面正常触发的职位列表 JSON。"
    ),
    dependencies=[Depends(require_api_key)],
)
async def search_boss_jobs(payload: BossZhipinSearchPayload):
    return await _run_boss_search(
        keywords=[payload.keyword],
        city_codes=[payload.city_code],
        max_pages=payload.max_pages,
        max_items_per_query=payload.max_items,
        include_raw=payload.include_raw,
        log_scope="boss/search",
    )


@router.post(
    "/scrape/boss/batch-search",
    summary="BOSS 直聘批量职位搜索",
    description=(
        "面向内部分析或批量调用的多关键词、多城市搜索接口。\n"
        "遇到登录失效、验证码、环境异常或接口超时会直接返回错误，不做反爬绕过。"
    ),
    dependencies=[Depends(require_api_key)],
)
async def batch_search_boss_jobs(payload: BossZhipinBatchPayload):
    return await _run_boss_search(
        keywords=payload.keywords,
        city_codes=payload.city_codes,
        max_pages=payload.max_pages,
        max_items_per_query=payload.max_items_per_query,
        include_raw=payload.include_raw,
        log_scope="boss/batch-search",
    )
