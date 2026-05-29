# -*- coding: utf-8 -*-
"""Tavily 搜索 provider。

直接基于 ``httpx.AsyncClient`` 调 ``POST {base_url}/search``：
- 鉴权：``Authorization: Bearer {TAVILY_API_KEY}``（不再在 body 里塞 api_key）
- 字段映射见 ``_build_payload``
- 错误归一映射：详见 ``_classify_http_error``

文档：https://docs.tavily.com/documentation/api-reference/endpoint/search
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel

from schemas.web_search import TavilyOverrides, WebSearchCommon, WebSearchTimeRange, WebSearchTopic
from services.web_search.base import (
    BaseSearchProvider,
    ProviderAttempt,
    ProviderError,
    SearchHit,
)
from utils.settings import settings as _settings

logger = logging.getLogger(__name__)


# ISO 2-letter → Tavily 国家全名（小写）。仅覆盖常用，未覆盖时不传 country。
# 完整列表见 Tavily OpenAPI 文档；按需扩充即可。
_ISO_TO_TAVILY_COUNTRY: Dict[str, str] = {
    "cn": "china",
    "us": "united states",
    "gb": "united kingdom",
    "uk": "united kingdom",
    "jp": "japan",
    "kr": "south korea",
    "sg": "singapore",
    "de": "germany",
    "fr": "france",
    "in": "india",
    "ru": "russia",
    "br": "brazil",
    "ca": "canada",
    "au": "australia",
}


class TavilyProvider(BaseSearchProvider):
    name = "tavily"

    def is_configured(self) -> bool:
        return bool(_settings.TAVILY_API_KEY)

    async def search(
        self,
        *,
        common: WebSearchCommon,
        overrides: Optional[BaseModel],
        client: httpx.AsyncClient,
        timeout_sec: float,
        include_raw_payload: bool = False,
    ) -> ProviderAttempt:
        if not self.is_configured():
            return ProviderAttempt(
                name=self.name,
                ok=False,
                error=ProviderError("unconfigured", None, "TAVILY_API_KEY 未配置"),
            )

        payload = _build_payload(common, overrides if isinstance(overrides, TavilyOverrides) else None)
        url = f"{_settings.WEB_SEARCH_TAVILY_BASE_URL.rstrip('/')}/search"
        headers = {
            "Authorization": f"Bearer {_settings.TAVILY_API_KEY}",
            "Content-Type": "application/json",
        }

        started = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                client.post(url, json=payload, headers=headers),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            return _err_attempt(
                self.name,
                started,
                ProviderError("timeout", None, f"Tavily 调用超过 {timeout_sec:g}s"),
            )
        except httpx.HTTPError as exc:
            return _err_attempt(
                self.name,
                started,
                ProviderError("network", None, f"{type(exc).__name__}: {exc}"),
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code != 200:
            return ProviderAttempt(
                name=self.name,
                ok=False,
                elapsed_ms=elapsed_ms,
                error=_classify_http_error(resp),
                raw=_safe_json(resp) if include_raw_payload else None,
            )

        try:
            data = resp.json()
        except Exception as exc:
            return ProviderAttempt(
                name=self.name,
                ok=False,
                elapsed_ms=elapsed_ms,
                error=ProviderError("parse", resp.status_code, str(exc)),
            )

        raw_results = data.get("results") or []
        max_n = common.top_k
        hits: List[SearchHit] = []
        for idx, item in enumerate(raw_results[:max_n], start=1):
            url_str = item.get("url")
            if not url_str:
                continue
            hits.append(
                SearchHit(
                    rank=idx,
                    title=str(item.get("title") or ""),
                    url=str(url_str),
                    display_url=None,
                    snippet=item.get("content") or None,
                    published_at=item.get("published_date") or None,
                    score=_safe_float(item.get("score")),
                    favicon=item.get("favicon") or None,
                    raw_content=item.get("raw_content") or None,
                    raw_content_format=_infer_raw_content_format(overrides),
                    provider=self.name,
                    raw=item if include_raw_payload else None,
                )
            )

        credits_used: Optional[int] = None
        usage = data.get("usage")
        if isinstance(usage, dict):
            credits_used = _safe_int(usage.get("credits"))

        answer = data.get("answer") or None
        ok = bool(hits)
        err: Optional[ProviderError] = None if ok else ProviderError("empty", 200, "Tavily 返回空 results")

        return ProviderAttempt(
            name=self.name,
            ok=ok,
            hits=hits,
            elapsed_ms=elapsed_ms,
            credits_used=credits_used,
            answer=answer,
            error=err,
            raw=data if include_raw_payload else None,
        )


# =====================================================================
# 内部辅助
# =====================================================================


def _build_payload(common: WebSearchCommon, overrides: Optional[TavilyOverrides]) -> Dict[str, Any]:
    """把中性请求 + Tavily overrides 组装成 Tavily POST /search 的 body。"""
    payload: Dict[str, Any] = {
        "query": common.query.strip(),
        "max_results": min(common.top_k, 20),
        "topic": common.topic.value if isinstance(common.topic, WebSearchTopic) else common.topic,
        "safe_search": common.safe_search,
    }

    # 时间窗：start/end 优先
    if common.start_date and common.end_date:
        payload["start_date"] = common.start_date.isoformat()
        payload["end_date"] = common.end_date.isoformat()
    elif common.time_range != WebSearchTimeRange.ANY:
        payload["time_range"] = common.time_range.value

    if common.include_domains:
        payload["include_domains"] = common.include_domains
    if common.exclude_domains:
        payload["exclude_domains"] = common.exclude_domains

    # locale.country：仅 topic=general 支持；映射不到就跳过
    if common.topic == WebSearchTopic.GENERAL and common.locale and common.locale.country:
        full = _ISO_TO_TAVILY_COUNTRY.get(common.locale.country.strip().lower())
        if full:
            payload["country"] = full

    # 写入 overrides（None 不传，避免覆盖 Tavily 默认）
    if overrides is not None:
        for key, value in overrides.model_dump(exclude_none=True).items():
            payload[key] = value

    # 默认带 include_usage，便于回填 credits_used
    payload.setdefault("include_usage", True)
    return payload


def _classify_http_error(resp: httpx.Response) -> ProviderError:
    sc = resp.status_code
    body = _safe_json(resp) or {}
    detail = body.get("detail")
    msg = ""
    if isinstance(detail, dict):
        msg = str(detail.get("error") or detail)
    elif isinstance(detail, str):
        msg = detail
    else:
        msg = (resp.text or "")[:300]

    if sc == 401:
        return ProviderError("auth", sc, msg or "Tavily 401 Unauthorized")
    if sc == 429:
        return ProviderError("rate_limit", sc, msg or "Tavily 429 Too Many Requests")
    if sc in (432, 433):
        return ProviderError("plan_limit", sc, msg or "Tavily 套餐/PayGo 超限")
    if sc == 400:
        return ProviderError("parse", sc, msg or "Tavily 400 Bad Request")
    return ProviderError("unknown", sc, msg or f"Tavily HTTP {sc}")


def _err_attempt(name: str, started: float, err: ProviderError) -> ProviderAttempt:
    return ProviderAttempt(
        name=name,
        ok=False,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        error=err,
    )


def _safe_json(resp: httpx.Response) -> Optional[Dict[str, Any]]:
    try:
        return resp.json()
    except Exception:
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _infer_raw_content_format(overrides: Optional[BaseModel]) -> Optional[str]:
    """根据 overrides.include_raw_content 推断格式，给后续 ContentOut 标注用。"""
    if not isinstance(overrides, TavilyOverrides) or overrides.include_raw_content is None:
        return None
    val = overrides.include_raw_content
    if val is True or val == "markdown":
        return "markdown"
    if val == "text":
        return "text"
    return None
