# -*- coding: utf-8 -*-
"""统一 Web 搜索/抓取后端 router。

三个端点：
- ``POST /api/web/search``：只做 SERP；约 1~2s
- ``POST /api/web/search-and-fetch``：SERP + 并发抓正文；约 5~30s
- ``POST /api/web/fetch``：仅按 URL 抓正文；约 3~10s

下沉的能力：
- Tavily / SearchAPI Google 两家 provider（密钥仅留在后端）
- ``fallback_chain`` 编排（auto 模式）
- Redis 缓存（不可用时静默降级）
- 顶层 ``asyncio.wait_for``
- ``meta.attempts[*]`` 可选记录所有尝试，便于审计/计费
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from schemas.web_search import (
    WebFetchRequest,
    WebSearchAndFetchRequest,
    WebSearchRequest,
)
from services.web_search import cache as web_cache
from services.web_search.base import (
    BaseSearchProvider,
    ProviderAttempt,
    SearchHit,
)
from services.web_search.fetcher import (
    display_url_of,
    enrich_with_content,
    fetch_urls,
    slugify_url,
)
from services.web_search.registry import build_chain, pick_overrides, summarize_attempts
from utils import redis_client
from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.security import require_api_key
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/web/search.log")

router = APIRouter()


# ══════════════════════════════════════════════════════════════════════
#  Lifespan：共享 httpx.AsyncClient + Redis ping
# ══════════════════════════════════════════════════════════════════════

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    if _client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Web 搜索客户端尚未初始化",
        )
    return _client


@asynccontextmanager
async def lifespan_resources(app):
    """由 main.py lifespan 调用：建 httpx 单例 + Redis ping。"""
    global _client
    proxy = _settings.OUTBOUND_PROXY_URL
    transport_kwargs: Dict[str, Any] = {
        "timeout": httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
        "limits": httpx.Limits(max_connections=20, max_keepalive_connections=10),
        "follow_redirects": True,
    }
    if proxy:
        transport_kwargs["proxy"] = proxy
    _client = httpx.AsyncClient(**transport_kwargs)
    await redis_client.startup()
    logger.info("web_search router 就绪 (proxy=%s, redis_ready=%s)", bool(proxy), redis_client.is_ready())
    try:
        yield
    finally:
        logger.info("web_search router 正在关闭 …")
        try:
            await _client.aclose()
        finally:
            _client = None
        await redis_client.shutdown()


# ══════════════════════════════════════════════════════════════════════
#  辅助：组包、错误码 → HTTP、缓存命中标记
# ══════════════════════════════════════════════════════════════════════

# provider error code → HTTP 状态码（用于 search/search-and-fetch 全部失败时的对外码）
_ERR_TO_HTTP: Dict[str, int] = {
    "auth": 502,
    "unconfigured": 422,
    "rate_limit": 502,
    "plan_limit": 502,
    "timeout": 504,
    "network": 502,
    "parse": 502,
    "unknown": 502,
    "empty": 200,  # 空结果不是错误
}


def _attempt_to_dict(a: ProviderAttempt, *, include_raw: bool) -> Dict[str, Any]:
    err = None
    if a.error:
        err = {
            "code": a.error.code,
            "http_status": a.error.http_status,
            "message": a.error.message,
        }
    return {
        "name": a.name,
        "ok": a.ok,
        "hit_count": len(a.hits),
        "elapsed_ms": a.elapsed_ms,
        "credits_used": a.credits_used,
        "error": err,
        "raw": a.raw if include_raw else None,
    }


def _hit_to_dict(
    h: SearchHit,
    *,
    include_raw: bool,
    content: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "rank": h.rank,
        "title": h.title,
        "url": h.url,
        "display_url": h.display_url or display_url_of(h.url),
        "snippet": h.snippet,
        "published_at": h.published_at,
        "score": h.score,
        "favicon": h.favicon,
        "provider": h.provider,
        "source_id": slugify_url(h.url),
        "content": content,
        "provider_raw": h.raw if include_raw else None,
    }


def _failure_http_code(attempts: List[ProviderAttempt]) -> int:
    """所有 provider 都失败时，从 attempts 推导对外 HTTP 状态码。"""
    if not attempts:
        return 503
    # 取错误等级最低的码作为对外码（auth/unconfigured/timeout 等优先于 unknown）
    codes = [(_ERR_TO_HTTP.get(a.error.code, 502) if a.error else 502) for a in attempts]
    # 如果至少一个 attempt 没有 ok 且是 timeout，整体认为是 504；否则 502/503
    if any(a.error and a.error.code == "timeout" for a in attempts):
        return 504
    if all(a.error and a.error.code == "unconfigured" for a in attempts):
        return 422
    if all(not a.ok for a in attempts):
        return 502 if all(c == 502 for c in codes) else max(codes)
    return 503


def _failure_message(attempts: List[ProviderAttempt]) -> str:
    parts: List[str] = []
    for a in attempts:
        if a.error:
            parts.append(f"{a.name}:{a.error.code}({a.error.http_status or '-'})")
        elif not a.ok:
            parts.append(f"{a.name}:ok=False")
    return "all_providers_failed; " + ("; ".join(parts) if parts else "no_attempt")


async def _run_chain(
    chain: List[BaseSearchProvider],
    payload: WebSearchRequest,
) -> Tuple[List[ProviderAttempt], Optional[ProviderAttempt]]:
    """逐个 provider 调用；遇到 ok 且 hits 非空就停止。

    指定具体 provider 时 chain 只有 1 项，行为退化为单次尝试。
    """
    client = _get_client()
    attempts: List[ProviderAttempt] = []
    selected: Optional[ProviderAttempt] = None
    for provider in chain:
        overrides = pick_overrides(payload.provider, provider.name)
        attempt = await provider.search(
            common=payload.search,
            overrides=overrides,
            client=client,
            timeout_sec=_settings.WEB_SEARCH_PROVIDER_TIMEOUT_SEC,
            include_raw_payload=payload.include_raw_provider_payload,
        )
        attempts.append(attempt)
        if attempt.ok and attempt.hits:
            selected = attempt
            break
    return attempts, selected


def _request_echo_for_search(payload: WebSearchRequest) -> Dict[str, Any]:
    echo = {
        "search": payload.search.model_dump(mode="json"),
        "provider": payload.provider.model_dump(mode="json"),
        "include_request_echo": payload.include_request_echo,
        "include_provider_attempts": payload.include_provider_attempts,
        "include_raw_provider_payload": payload.include_raw_provider_payload,
    }
    if isinstance(payload, WebSearchAndFetchRequest):
        echo["fetch"] = payload.fetch.model_dump(mode="json")
    return echo


def _request_echo_for_fetch(payload: WebFetchRequest, url_strs: List[str]) -> Dict[str, Any]:
    return {
        "urls": url_strs,
        "options": payload.options.model_dump(mode="json"),
        "include_request_echo": payload.include_request_echo,
    }


def _cache_key_payload(payload: WebSearchRequest) -> Dict[str, Any]:
    """Response-shaping debug flags should not fragment the business cache."""
    data = payload.model_dump(mode="json")
    data.pop("include_request_echo", None)
    data.pop("include_provider_attempts", None)
    return data


def _cache_key_payload_fetch(payload: WebFetchRequest) -> Dict[str, Any]:
    data = payload.model_dump(mode="json")
    data.pop("include_request_echo", None)
    return data


def _add_search_meta(
    data: Dict[str, Any],
    payload: WebSearchRequest,
    *,
    attempts: Optional[List[ProviderAttempt]] = None,
) -> None:
    meta: Dict[str, Any] = {}
    if payload.include_request_echo:
        meta["request"] = _request_echo_for_search(payload)
    if payload.include_provider_attempts and attempts is not None:
        meta["attempts"] = [
            _attempt_to_dict(a, include_raw=payload.include_raw_provider_payload)
            for a in attempts
        ]
    if meta:
        data["meta"] = meta


def _add_fetch_meta(
    data: Dict[str, Any],
    payload: WebFetchRequest,
    url_strs: List[str],
) -> None:
    if payload.include_request_echo:
        data["meta"] = {"request": _request_echo_for_fetch(payload, url_strs)}


def _normalize_cached_search_data(
    data: Dict[str, Any],
    payload: WebSearchRequest,
    *,
    elapsed_ms: int,
) -> Dict[str, Any]:
    """Remove legacy debug fields from cached payloads before returning them."""
    data.pop("request", None)
    data.pop("meta", None)
    provider = data.setdefault("provider", {})
    provider.pop("attempts", None)
    provider["elapsed_ms"] = elapsed_ms
    if payload.include_provider_attempts:
        data["meta"] = {
            "attempts": [
                web_cache.make_cache_hit_marker(elapsed_ms, len(data.get("hits") or []))
            ]
        }
    if payload.include_request_echo:
        data.setdefault("meta", {})["request"] = _request_echo_for_search(payload)
    return data


# ══════════════════════════════════════════════════════════════════════
#  /api/web/search
# ══════════════════════════════════════════════════════════════════════

@router.post(
    "/web/search",
    summary="统一 Web 搜索（仅 SERP，不抓正文）",
    description=(
        "调度 Tavily / SearchAPI Google 等 provider，按 fallback 链取首个非空结果。"
        " 命中即缓存（Redis），TTL 由 WEB_SEARCH_CACHE_TTL_SEARCH_SEC 控制。"
    ),
    dependencies=[Depends(require_api_key("WEB_SEARCH_API_KEY"))],
)
async def web_search(payload: WebSearchRequest):
    overall_started = time.monotonic()
    cache_key = web_cache.cache_key_search(_cache_key_payload(payload))

    cached = await web_cache.get_or_none(cache_key)
    if cached is not None:
        elapsed_ms = int((time.monotonic() - overall_started) * 1000)
        cached = _normalize_cached_search_data(cached, payload, elapsed_ms=elapsed_ms)
        return create_standard_response(
            data=cached,
            message=f"web 搜索命中缓存，共 {len(cached.get('hits') or [])} 条",
        )

    chain = build_chain(payload.provider)
    if not chain:
        return create_standard_response(
            code=422,
            message="provider_unconfigured: 无可用 provider（请检查 TAVILY_API_KEY / SEARCHAPI_IO_API_KEY）",
            data=None,
        )

    try:
        attempts, selected = await asyncio.wait_for(
            _run_chain(chain, payload),
            timeout=_settings.WEB_SEARCH_REQUEST_TIMEOUT_SEARCH_SEC,
        )
    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - overall_started) * 1000)
        data = {
            "provider": {"selected": None, "credits_used": None, "elapsed_ms": elapsed_ms},
            "hits": [],
            "answer": None,
        }
        _add_search_meta(data, payload, attempts=[])
        return create_standard_response(
            code=504,
            message="provider_timeout: 顶层等待超时",
            data=data,
        )

    elapsed_ms = int((time.monotonic() - overall_started) * 1000)
    selected_name, total_credits = summarize_attempts(attempts)

    data: Dict[str, Any] = {
        "provider": {
            "selected": selected_name,
            "credits_used": total_credits or None,
            "elapsed_ms": elapsed_ms,
        },
        "hits": [],
        "answer": selected.answer if selected else None,
    }
    _add_search_meta(data, payload, attempts=attempts)

    if not selected:
        return create_standard_response(
            code=_failure_http_code(attempts),
            message=_failure_message(attempts),
            data=data,
        )

    data["hits"] = [
        _hit_to_dict(h, include_raw=payload.include_raw_provider_payload) for h in selected.hits
    ]

    await web_cache.set(cache_key, data, _settings.WEB_SEARCH_CACHE_TTL_SEARCH_SEC)
    return create_standard_response(
        data=data,
        message=f"web 搜索完成，命中 {len(selected.hits)} 条（provider={selected.name}）",
    )


# ══════════════════════════════════════════════════════════════════════
#  /api/web/search-and-fetch
# ══════════════════════════════════════════════════════════════════════

@router.post(
    "/web/search-and-fetch",
    summary="统一 Web 搜索 + 并发抓正文",
    description=(
        "SERP 命中后并发抓正文。Tavily 在启用 include_raw_content 时短路省抓取；"
        " 其他 provider 走 url_content_fetch 流水线（HEAD 嗅探 + 文档解析 + Markdown 清洗）。"
    ),
    dependencies=[Depends(require_api_key("WEB_SEARCH_API_KEY"))],
)
async def web_search_and_fetch(payload: WebSearchAndFetchRequest):
    overall_started = time.monotonic()
    cache_key = web_cache.cache_key_search_and_fetch(_cache_key_payload(payload))

    cached = await web_cache.get_or_none(cache_key)
    if cached is not None:
        elapsed_ms = int((time.monotonic() - overall_started) * 1000)
        cached = _normalize_cached_search_data(cached, payload, elapsed_ms=elapsed_ms)
        for h in cached.get("hits") or []:
            if isinstance(h.get("content"), dict):
                h["content"]["source"] = "cached"
        return create_standard_response(
            data=cached,
            message=f"web 搜索+正文命中缓存，共 {len(cached.get('hits') or [])} 条",
        )

    chain = build_chain(payload.provider)
    if not chain:
        return create_standard_response(
            code=422,
            message="provider_unconfigured: 无可用 provider",
            data=None,
        )

    try:
        attempts, selected = await asyncio.wait_for(
            _run_chain(chain, payload),
            timeout=_settings.WEB_SEARCH_REQUEST_TIMEOUT_SEARCH_AND_FETCH_SEC,
        )
    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - overall_started) * 1000)
        data = {
            "provider": {"selected": None, "credits_used": None, "elapsed_ms": elapsed_ms},
            "hits": [],
            "answer": None,
            "fetch_summary": {"requested": 0, "ok": 0, "skipped": 0, "failed": 0, "elapsed_ms": 0},
        }
        _add_search_meta(data, payload, attempts=[])
        return create_standard_response(
            code=504,
            message="provider_timeout: 顶层等待超时",
            data=data,
        )

    selected_name, total_credits = summarize_attempts(attempts)

    data: Dict[str, Any] = {
        "provider": {
            "selected": selected_name,
            "credits_used": total_credits or None,
            "elapsed_ms": 0,  # 下方覆盖
        },
        "hits": [],
        "answer": selected.answer if selected else None,
        "fetch_summary": {"requested": 0, "ok": 0, "skipped": 0, "failed": 0, "elapsed_ms": 0},
    }
    _add_search_meta(data, payload, attempts=attempts)

    if not selected:
        data["provider"]["elapsed_ms"] = int((time.monotonic() - overall_started) * 1000)
        return create_standard_response(
            code=_failure_http_code(attempts),
            message=_failure_message(attempts),
            data=data,
        )

    contents, fetch_summary = await enrich_with_content(
        selected.hits, payload.fetch, _get_client()
    )

    hits_out: List[Dict[str, Any]] = []
    for hit, content in zip(selected.hits, contents):
        hits_out.append(
            _hit_to_dict(hit, include_raw=payload.include_raw_provider_payload, content=content)
        )

    elapsed_ms = int((time.monotonic() - overall_started) * 1000)
    data["provider"]["elapsed_ms"] = elapsed_ms
    data["hits"] = hits_out
    data["fetch_summary"] = fetch_summary

    await web_cache.set(cache_key, data, _settings.WEB_SEARCH_CACHE_TTL_FETCH_SEC)
    return create_standard_response(
        data=data,
        message=(
            f"web 搜索+正文完成，命中 {len(hits_out)} 条；"
            f"正文 OK {fetch_summary['ok']}/{fetch_summary['requested']}（provider={selected.name}）"
        ),
    )


# ══════════════════════════════════════════════════════════════════════
#  /api/web/fetch
# ══════════════════════════════════════════════════════════════════════

@router.post(
    "/web/fetch",
    summary="按 URL 抓取正文（不做搜索）",
    description=(
        "对一批已知 URL 调 url_content_fetch 流水线（HEAD 分流 + 文档解析 + Markdown 清洗）。"
        " 适合二次抓取或外部链接补全。"
    ),
    dependencies=[Depends(require_api_key("WEB_SEARCH_API_KEY"))],
)
async def web_fetch(payload: WebFetchRequest):
    overall_started = time.monotonic()
    url_strs = [str(u) for u in payload.urls]
    cache_payload = _cache_key_payload_fetch(payload)
    cache_options = cache_payload["options"]

    # 单 URL 缓存：避免某条 URL 频繁被打
    cache_keys = [
        web_cache.cache_key_fetch(u, cache_options)
        for u in url_strs
    ]

    cached_pairs: List[Tuple[int, Dict[str, Any]]] = []
    miss_indices: List[int] = []
    for i, key in enumerate(cache_keys):
        cached = await web_cache.get_or_none(key)
        if cached is not None:
            cached["source"] = "cached"
            cached_pairs.append((i, cached))
        else:
            miss_indices.append(i)

    try:
        miss_results: List[Dict[str, Any]] = []
        if miss_indices:
            miss_urls = [url_strs[i] for i in miss_indices]
            miss_results, _ = await asyncio.wait_for(
                fetch_urls(miss_urls, payload.options, _get_client()),
                timeout=_settings.WEB_SEARCH_REQUEST_TIMEOUT_FETCH_SEC,
            )
            # 写回缓存（只缓存抓成功/部分有内容的）
            for url, key, result in zip(miss_urls, [cache_keys[i] for i in miss_indices], miss_results):
                if result.get("status") in ("ok", "provider_native"):
                    await web_cache.set(key, result, _settings.WEB_SEARCH_CACHE_TTL_FETCH_SEC)
    except asyncio.TimeoutError:
        data = {
            "results": [],
            "summary": {
                "requested": len(url_strs),
                "ok": 0,
                "skipped": 0,
                "failed": len(url_strs),
                "elapsed_ms": int((time.monotonic() - overall_started) * 1000),
            },
        }
        _add_fetch_meta(data, payload, url_strs)
        return create_standard_response(
            code=504,
            message="fetch_timeout: 顶层等待超时",
            data=data,
        )

    # 拼回原顺序
    by_index: Dict[int, Dict[str, Any]] = {i: r for i, r in cached_pairs}
    for i, r in zip(miss_indices, miss_results):
        by_index[i] = r
    results = [by_index[i] for i in range(len(url_strs))]

    summary = {
        "requested": len(results),
        "ok": sum(1 for r in results if r.get("status") in ("ok", "provider_native")),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
        "failed": sum(1 for r in results if r.get("status") not in ("ok", "provider_native", "skipped")),
        "elapsed_ms": int((time.monotonic() - overall_started) * 1000),
    }

    data = {
        "results": results,
        "summary": summary,
    }
    _add_fetch_meta(data, payload, url_strs)

    return create_standard_response(
        data=data,
        message=f"web fetch 完成，OK {summary['ok']}/{summary['requested']}",
    )
