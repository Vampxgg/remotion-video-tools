# -*- coding: utf-8 -*-
"""Web 搜索/抓取后端的请求与响应 Pydantic 模型。

设计原则（详见 docs/api/web_search.md 与计划稿）：
- 中性公共字段（``WebSearchCommon``）覆盖 90% 调用场景，跨 provider 一致；
- Tavily / SearchAPI Google 独有能力作为"逃生通道"，分别放在
  ``TavilyOverrides`` / ``SearchAPIGoogleOverrides``，不与中性字段冲突；
- ``provider.name=AUTO`` 才走 fallback 链；指定具体 provider 时不会被悄悄降级。
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, HttpUrl, model_validator

from utils.settings import settings as _settings


# =====================================================================
# 枚举
# =====================================================================

class WebSearchTopic(str, Enum):
    GENERAL = "general"
    NEWS = "news"
    FINANCE = "finance"


class WebSearchTimeRange(str, Enum):
    ANY = "any"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class WebSearchProvider(str, Enum):
    AUTO = "auto"
    TAVILY = "tavily"
    SEARCHAPI_GOOGLE = "searchapi_google"


# =====================================================================
# 公共请求字段
# =====================================================================

class WebSearchLocale(BaseModel):
    country: Optional[str] = Field(
        None,
        description="ISO-3166 国家代码（cn / us / jp ...）；provider 内部各自映射",
        examples=["cn"],
    )
    language: Optional[str] = Field(
        None,
        description="BCP-47 语言代码（zh-CN / en-US ...）",
        examples=["zh-CN"],
    )


class WebSearchCommon(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="搜索关键词")
    top_k: int = Field(
        default_factory=lambda: _settings.WEB_SEARCH_DEFAULT_TOP_K,
        ge=1,
        description="返回结果上限；硬上限由 WEB_SEARCH_MAX_TOP_K 控制",
    )
    time_range: WebSearchTimeRange = Field(
        WebSearchTimeRange.ANY,
        description="时间窗；start_date/end_date 任一非空时忽略本字段",
    )
    start_date: Optional[date] = Field(
        None, description="起始日期（YYYY-MM-DD）；与 end_date 必须成对"
    )
    end_date: Optional[date] = Field(
        None, description="结束日期（YYYY-MM-DD）；与 start_date 必须成对"
    )
    topic: WebSearchTopic = Field(
        WebSearchTopic.GENERAL,
        description="主题；仅 Tavily 原生支持，SearchAPI 忽略",
    )
    include_domains: List[str] = Field(
        default_factory=list,
        max_length=20,
        description="只搜索这些域；SearchAPI 内部装饰为 (site:a OR site:b)",
    )
    exclude_domains: List[str] = Field(
        default_factory=list,
        max_length=20,
        description="排除这些域；SearchAPI 内部装饰为 -site:a",
    )
    locale: WebSearchLocale = Field(default_factory=WebSearchLocale)
    safe_search: bool = Field(False, description="是否启用安全过滤")

    @model_validator(mode="after")
    def _check_top_k_and_dates(self) -> "WebSearchCommon":
        max_top_k = _settings.WEB_SEARCH_MAX_TOP_K
        if self.top_k > max_top_k:
            raise ValueError(f"top_k={self.top_k} 超过上限 {max_top_k}")
        if (self.start_date and not self.end_date) or (self.end_date and not self.start_date):
            raise ValueError("start_date 与 end_date 必须成对提供")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date 不能晚于 end_date")
        return self


# =====================================================================
# Provider-specific overrides（逃生通道）
# =====================================================================

class TavilyOverrides(BaseModel):
    """Tavily 独有能力。直通到 Tavily POST /search 同名字段。"""
    search_depth: Optional[Literal["basic", "advanced", "fast", "ultra-fast"]] = None
    chunks_per_source: Optional[int] = Field(None, ge=1, le=3)
    include_answer: Optional[Union[bool, Literal["basic", "advanced"]]] = None
    include_raw_content: Optional[Union[bool, Literal["markdown", "text"]]] = None
    include_images: Optional[bool] = None
    include_image_descriptions: Optional[bool] = None
    include_favicon: Optional[bool] = None
    auto_parameters: Optional[bool] = None
    exact_match: Optional[bool] = None
    include_usage: Optional[bool] = Field(
        True,
        description="默认带上 usage，便于在 attempts.credits_used 中反查计费",
    )


class SearchAPIGoogleOverrides(BaseModel):
    """SearchAPI Google 独有能力。直通到 GET /api/v1/search?engine=google 同名 query 参数。"""
    device: Optional[Literal["desktop", "mobile", "tablet"]] = None
    location: Optional[str] = None
    uule: Optional[str] = None
    nfpr: Optional[bool] = None
    verbatim: Optional[bool] = None
    optimization_strategy: Optional[Literal["performance", "ads"]] = None
    page: Optional[int] = Field(None, ge=1, le=10, description="分页（Google 锁 num=10）")


class ProviderConfig(BaseModel):
    name: WebSearchProvider = Field(
        WebSearchProvider.AUTO,
        description="auto = 按 fallback_chain（或 settings 默认链）逐个尝试；指定 provider 时不 fallback",
    )
    fallback_chain: Optional[List[WebSearchProvider]] = Field(
        None,
        description="仅当 name=auto 生效；为空时回落到 settings.WEB_SEARCH_DEFAULT_PROVIDERS",
    )
    tavily: Optional[TavilyOverrides] = None
    searchapi_google: Optional[SearchAPIGoogleOverrides] = None

    @model_validator(mode="after")
    def _normalize_chain(self) -> "ProviderConfig":
        if self.name == WebSearchProvider.AUTO and self.fallback_chain is not None:
            if not self.fallback_chain:
                raise ValueError("fallback_chain 非空时必须至少含 1 项")
            if WebSearchProvider.AUTO in self.fallback_chain:
                raise ValueError("fallback_chain 中不能再包含 auto")
        return self


# =====================================================================
# Fetch 选项
# =====================================================================

class FetchOptions(BaseModel):
    enabled: bool = Field(True, description="是否抓取正文；仅对 search-and-fetch 端点生效")
    max_content_chars: int = Field(
        default_factory=lambda: _settings.WEB_SEARCH_DEFAULT_CONTENT_CHARS,
        ge=500,
        description="单条正文最大字符数；超出按 max_content_chars 截断",
    )
    concurrency: int = Field(
        default_factory=lambda: _settings.WEB_SEARCH_DEFAULT_CONCURRENCY,
        ge=1,
        description="并发抓取的协程数；上限由 WEB_SEARCH_MAX_CONCURRENCY 控制",
    )
    html_timeout_sec: float = Field(
        default_factory=lambda: _settings.WEB_SEARCH_FETCH_HTML_TIMEOUT_SEC,
        ge=5.0,
        le=60.0,
    )
    doc_download_timeout_sec: float = Field(
        default_factory=lambda: _settings.WEB_SEARCH_DOC_DOWNLOAD_TIMEOUT_SEC,
        ge=10.0,
        le=180.0,
    )
    prefer_provider_native_content: bool = Field(
        True,
        description="若 provider 已返回 raw_content（如 Tavily），直接复用以省 1 跳",
    )
    only_first_n: Optional[int] = Field(
        None, ge=0, le=10, description="仅抓前 N 条；None=top_k"
    )

    @model_validator(mode="after")
    def _check_limits(self) -> "FetchOptions":
        if self.max_content_chars > _settings.WEB_SEARCH_MAX_CONTENT_CHARS:
            raise ValueError(
                f"max_content_chars={self.max_content_chars} 超过上限 "
                f"{_settings.WEB_SEARCH_MAX_CONTENT_CHARS}"
            )
        if self.concurrency > _settings.WEB_SEARCH_MAX_CONCURRENCY:
            raise ValueError(
                f"concurrency={self.concurrency} 超过上限 "
                f"{_settings.WEB_SEARCH_MAX_CONCURRENCY}"
            )
        return self


# =====================================================================
# 顶层请求体
# =====================================================================

class WebSearchRequest(BaseModel):
    search: WebSearchCommon
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    include_request_echo: bool = Field(
        False,
        description="是否在 data.meta.request 返回规范化后的请求回显（仅调试/审计）",
    )
    include_provider_attempts: bool = Field(
        False,
        description="是否在 data.meta.attempts 返回 provider 尝试历史（仅调试/审计）",
    )
    include_raw_provider_payload: bool = Field(
        False,
        description="返回 hits[].provider_raw 与 attempts[].raw（仅调试/审计）",
    )


class WebSearchAndFetchRequest(WebSearchRequest):
    fetch: FetchOptions = Field(default_factory=FetchOptions)


class WebFetchRequest(BaseModel):
    urls: List[HttpUrl] = Field(
        ..., min_length=1, max_length=10, description="待抓取的 URL 列表（≤10）"
    )
    options: FetchOptions = Field(default_factory=FetchOptions)
    include_request_echo: bool = Field(
        False,
        description="是否在 data.meta.request 返回规范化后的请求回显（仅调试/审计）",
    )


# =====================================================================
# 响应模型（供 OpenAPI 文档展示；运行期统一用 dict 装入 StandardResponse.data）
# =====================================================================

class ProviderErrorOut(BaseModel):
    code: Literal[
        "auth", "rate_limit", "plan_limit", "timeout", "empty",
        "network", "parse", "unconfigured", "unknown",
    ]
    http_status: Optional[int] = None
    message: str = ""


class ProviderAttemptOut(BaseModel):
    name: str
    ok: bool
    hit_count: int = 0
    elapsed_ms: int = 0
    credits_used: Optional[int] = None
    error: Optional[ProviderErrorOut] = None
    raw: Optional[Dict[str, Any]] = None


class ContentOut(BaseModel):
    status: Literal[
        "ok", "empty", "timeout", "http_error", "too_large",
        "skipped", "provider_native", "cached",
    ]
    kind: Optional[str] = Field(None, description="html/pdf/office/image/text/other")
    text: str = ""
    char_count: int = 0
    truncated: bool = False
    source: Literal[
        "url_content_fetch", "tavily_raw_content", "cached", "skipped", "provider_native"
    ] = "skipped"
    final_url: Optional[str] = None
    elapsed_ms: int = 0
    error: Optional[str] = None


class SearchHitOut(BaseModel):
    rank: int
    title: Optional[str] = None
    url: str
    display_url: Optional[str] = None
    snippet: Optional[str] = None
    published_at: Optional[str] = None
    score: Optional[float] = None
    favicon: Optional[str] = None
    provider: str
    content: Optional[ContentOut] = None
    provider_raw: Optional[Dict[str, Any]] = None


class ProviderBlockOut(BaseModel):
    selected: Optional[str] = None
    credits_used: Optional[int] = None
    elapsed_ms: int = 0


class MetaOut(BaseModel):
    request: Optional[Dict[str, Any]] = None
    attempts: Optional[List[ProviderAttemptOut]] = None


class FetchSummaryOut(BaseModel):
    requested: int = 0
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    elapsed_ms: int = 0


class WebSearchData(BaseModel):
    provider: ProviderBlockOut
    hits: List[SearchHitOut] = Field(default_factory=list)
    answer: Optional[str] = None
    meta: Optional[MetaOut] = None


class WebSearchAndFetchData(WebSearchData):
    fetch_summary: FetchSummaryOut = Field(default_factory=FetchSummaryOut)


class WebFetchData(BaseModel):
    results: List[ContentOut] = Field(default_factory=list)
    summary: FetchSummaryOut = Field(default_factory=FetchSummaryOut)
    meta: Optional[MetaOut] = None
