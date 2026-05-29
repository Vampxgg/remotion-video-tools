# -*- coding: utf-8 -*-
"""Web 搜索 provider 的抽象基类与数据契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

import httpx
from pydantic import BaseModel

from schemas.web_search import WebSearchCommon


ProviderErrorCode = Literal[
    "auth",
    "rate_limit",
    "plan_limit",
    "timeout",
    "empty",
    "network",
    "parse",
    "unconfigured",
    "unknown",
]


@dataclass(frozen=True)
class ProviderError:
    """归一化的 provider 错误。

    code 取值：
    - auth：401 类
    - rate_limit：429
    - plan_limit：Tavily 432/433 等套餐/PayGo 超限
    - timeout：asyncio.TimeoutError
    - empty：HTTP 200 但 results 为空（视为"软失败"，便于 fallback）
    - network：连接/SSL/DNS 类
    - parse：JSON 解析失败
    - unconfigured：本 provider 的 API key 未配置
    - unknown：其他 5xx / 未知异常
    """
    code: ProviderErrorCode
    http_status: Optional[int] = None
    message: str = ""


@dataclass(frozen=True)
class SearchHit:
    """中性的搜索命中条目，由 provider 各自转换得到。"""
    rank: int
    title: str
    url: str
    display_url: Optional[str] = None
    snippet: Optional[str] = None
    published_at: Optional[str] = None
    score: Optional[float] = None
    favicon: Optional[str] = None
    # 仅 Tavily 在 include_raw_content 启用时会填；fetcher 据此做短路
    raw_content: Optional[str] = None
    raw_content_format: Optional[str] = None
    provider: str = ""
    raw: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ProviderAttempt:
    """单次 provider 调用的归一结果。"""
    name: str
    ok: bool
    hits: List[SearchHit] = field(default_factory=list)
    elapsed_ms: int = 0
    credits_used: Optional[int] = None
    answer: Optional[str] = None
    error: Optional[ProviderError] = None
    raw: Optional[Dict[str, Any]] = None


class BaseSearchProvider(ABC):
    """Web 搜索 provider 抽象基类。

    子类只需实现 ``name`` / ``is_configured`` / ``search``。
    超时、并发、缓存、fallback 都由编排层处理。
    """

    name: str = ""

    @abstractmethod
    def is_configured(self) -> bool:
        """返回本 provider 当前是否可用（一般 = 已配置 API key）。"""

    @abstractmethod
    async def search(
        self,
        *,
        common: WebSearchCommon,
        overrides: Optional[BaseModel],
        client: httpx.AsyncClient,
        timeout_sec: float,
        include_raw_payload: bool = False,
    ) -> ProviderAttempt:
        """执行一次 provider 调用并归一为 ProviderAttempt。"""
