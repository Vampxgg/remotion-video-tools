# -*- coding: utf-8 -*-
"""SearchAPI.io Google 搜索 provider。

直接基于 ``httpx.AsyncClient`` 调 ``GET {base_url}?engine=google&...``：
- 鉴权：``Authorization: Bearer {SEARCHAPI_IO_API_KEY}``（也支持 ``api_key`` query；统一用 header）
- ``include_domains/exclude_domains`` 不是原生参数，通过装饰 ``q`` 实现：
  ``(site:a OR site:b) -site:c -site:d``
- ``time_range`` 映射到 ``time_period=last_*``；``start_date/end_date`` 映射到
  ``time_period_min/max`` 的 ``MM/DD/YYYY``
- Google 自 2025-09 起锁 ``num=10``；多于 10 条不可达，``top_k`` 由我们在结果切片实现

文档：https://www.searchapi.io/docs/google
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel

from schemas.web_search import (
    SearchAPIGoogleOverrides,
    WebSearchCommon,
    WebSearchTimeRange,
)
from services.web_search.base import (
    BaseSearchProvider,
    ProviderAttempt,
    ProviderError,
    SearchHit,
)
from utils.settings import settings as _settings

logger = logging.getLogger(__name__)


_TIME_RANGE_TO_PERIOD = {
    WebSearchTimeRange.DAY: "last_day",
    WebSearchTimeRange.WEEK: "last_week",
    WebSearchTimeRange.MONTH: "last_month",
    WebSearchTimeRange.YEAR: "last_year",
}


class SearchAPIGoogleProvider(BaseSearchProvider):
    name = "searchapi_google"

    def is_configured(self) -> bool:
        return bool(_settings.SEARCHAPI_IO_API_KEY)

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
                error=ProviderError("unconfigured", None, "SEARCHAPI_IO_API_KEY 未配置"),
            )

        params = _build_params(
            common,
            overrides if isinstance(overrides, SearchAPIGoogleOverrides) else None,
        )
        url = _settings.WEB_SEARCH_SEARCHAPI_BASE_URL
        headers = {"Authorization": f"Bearer {_settings.SEARCHAPI_IO_API_KEY}"}

        started = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                client.get(url, params=params, headers=headers),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            return _err_attempt(
                self.name,
                started,
                ProviderError("timeout", None, f"SearchAPI 调用超过 {timeout_sec:g}s"),
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

        organic = data.get("organic_results") or []
        hits: List[SearchHit] = []
        for idx, item in enumerate(organic[: common.top_k], start=1):
            url_str = item.get("link")
            if not url_str:
                continue
            hits.append(
                SearchHit(
                    rank=int(item.get("position") or idx),
                    title=str(item.get("title") or ""),
                    url=str(url_str),
                    display_url=item.get("displayed_link") or item.get("domain") or None,
                    snippet=item.get("snippet") or None,
                    published_at=item.get("date") or None,
                    score=None,
                    favicon=item.get("favicon") or None,
                    raw_content=None,
                    raw_content_format=None,
                    provider=self.name,
                    raw=item if include_raw_payload else None,
                )
            )

        ok = bool(hits)
        err: Optional[ProviderError] = None if ok else ProviderError(
            "empty", 200, "SearchAPI 返回空 organic_results"
        )
        return ProviderAttempt(
            name=self.name,
            ok=ok,
            hits=hits,
            elapsed_ms=elapsed_ms,
            credits_used=None,  # SearchAPI 不在响应里返回积分
            error=err,
            raw=data if include_raw_payload else None,
        )


# =====================================================================
# 内部辅助
# =====================================================================


def _build_params(
    common: WebSearchCommon,
    overrides: Optional[SearchAPIGoogleOverrides],
) -> Dict[str, Any]:
    q = _decorate_query(common)

    params: Dict[str, Any] = {
        "engine": "google",
        "q": q,
        "safe": "active" if common.safe_search else "off",
    }

    # 时间窗
    if common.start_date and common.end_date:
        params["time_period_min"] = _to_mdy(common.start_date)
        params["time_period_max"] = _to_mdy(common.end_date)
    else:
        period = _TIME_RANGE_TO_PERIOD.get(common.time_range)
        if period:
            params["time_period"] = period

    # locale
    if common.locale:
        if common.locale.country:
            params["gl"] = common.locale.country.strip().lower()
        if common.locale.language:
            # 取语言前缀，如 zh-CN -> zh
            params["hl"] = common.locale.language.split("-")[0].strip().lower()

    # overrides 直通
    if overrides is not None:
        for key, value in overrides.model_dump(exclude_none=True).items():
            params[key] = value

    return params


def _decorate_query(common: WebSearchCommon) -> str:
    parts = [common.query.strip()]
    if common.include_domains:
        joined = " OR ".join(f"site:{d.strip()}" for d in common.include_domains if d.strip())
        if joined:
            parts.append(f"({joined})")
    for d in common.exclude_domains:
        dd = d.strip()
        if dd:
            parts.append(f"-site:{dd}")
    return " ".join(p for p in parts if p)


def _to_mdy(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def _classify_http_error(resp: httpx.Response) -> ProviderError:
    sc = resp.status_code
    body = _safe_json(resp) or {}
    msg = ""
    if isinstance(body, dict):
        msg = str(body.get("error") or body.get("message") or "")
    if not msg:
        msg = (resp.text or "")[:300]

    if sc == 401:
        return ProviderError("auth", sc, msg or "SearchAPI 401 Unauthorized")
    if sc == 429:
        return ProviderError("rate_limit", sc, msg or "SearchAPI 429 Too Many Requests")
    if sc == 400:
        return ProviderError("parse", sc, msg or "SearchAPI 400 Bad Request")
    return ProviderError("unknown", sc, msg or f"SearchAPI HTTP {sc}")


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
