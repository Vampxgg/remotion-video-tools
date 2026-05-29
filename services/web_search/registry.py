# -*- coding: utf-8 -*-
"""Web 搜索 provider 注册表 + 编排链路。"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel

from schemas.web_search import ProviderConfig, WebSearchProvider
from services.web_search.base import BaseSearchProvider
from services.web_search.searchapi_google_provider import SearchAPIGoogleProvider
from services.web_search.tavily_provider import TavilyProvider
from utils.settings import settings as _settings


# 单例：模块导入时构造一次，后续被多个 router/任务共享
PROVIDERS: Dict[str, BaseSearchProvider] = {
    "tavily": TavilyProvider(),
    "searchapi_google": SearchAPIGoogleProvider(),
}


def build_chain(config: ProviderConfig) -> List[BaseSearchProvider]:
    """根据 ProviderConfig 决定调用顺序。

    - ``name=AUTO``：用 ``fallback_chain`` 或 ``settings.WEB_SEARCH_DEFAULT_PROVIDERS``；
      未知名字静默跳过，便于配置漂移时灰度。
    - ``name`` 是具体 provider：仅试这一家，不 fallback；调用方拿到 unconfigured/auth
      错误时直接报错，避免悄悄降级造成账单意外。
    """
    if config.name != WebSearchProvider.AUTO:
        provider = PROVIDERS.get(config.name.value)
        return [provider] if provider else []

    if config.fallback_chain:
        names = [p.value for p in config.fallback_chain]
    else:
        names = list(_settings.WEB_SEARCH_DEFAULT_PROVIDERS)

    chain: List[BaseSearchProvider] = []
    for n in names:
        provider = PROVIDERS.get(n)
        if provider is not None:
            chain.append(provider)
    return chain


def pick_overrides(config: ProviderConfig, provider_name: str) -> Optional[BaseModel]:
    """从 ProviderConfig 上取出对应 provider 的 overrides 子对象。"""
    if provider_name == "tavily":
        return config.tavily
    if provider_name == "searchapi_google":
        return config.searchapi_google
    return None


def summarize_attempts(attempts: List) -> Tuple[Optional[str], int]:
    """汇总 attempts 的 credits_used，返回 (selected_name, total_credits)。

    selected_name = 最后一个 ok=True 的 provider；total_credits 仅累加 Tavily 这种返回过 credits 的来源。
    """
    selected = next((a.name for a in reversed(attempts) if a.ok), None)
    total = 0
    has_credit = False
    for a in attempts:
        if a.credits_used:
            total += a.credits_used
            has_credit = True
    return selected, (total if has_credit else 0)
