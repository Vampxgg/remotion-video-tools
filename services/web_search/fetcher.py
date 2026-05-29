# -*- coding: utf-8 -*-
"""把 SearchHit 列表丰富成"含正文"的 ContentOut。

两条通路：
1. **Provider native 短路**：Tavily 在 ``include_raw_content=markdown/text`` 时已经返回了 markdown
   正文；我们直接复用，省一轮 HTTP，并把 ``content.source`` 标成 ``provider_native``。
2. **trafilatura 流水线**：调 ``api.url_content_fetch.fetch_url_content``，复用项目已有的
   HEAD 嗅探 + 文档解析 + 图片校验等能力；用 ``asyncio.Semaphore`` 控制并发。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

import httpx

from schemas.web_search import FetchOptions
from services.web_search.base import SearchHit
from utils.settings import settings as _settings

logger = logging.getLogger(__name__)


# ContentOut.kind 与 url_content_fetch.content_kind 的映射保持一致；这里只是别名集合
_TRUNCATE_HINT = "\n\n[正文已截断]"


def slugify_url(url: str, max_len: int = 120) -> str:
    """对外暴露的稳定 source_id 生成器；供 router 在 hit 上挂 source_id 使用。"""
    s = re.sub(r"https?://", "", url or "")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s)
    return ("web-" + s).strip("-")[:max_len]


def display_url_of(url: str, fallback: str = "") -> str:
    try:
        return urlparse(url).netloc or fallback
    except Exception:
        return fallback


def _truncate(text: str, max_chars: int) -> Tuple[str, bool]:
    if not text:
        return "", False
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + _TRUNCATE_HINT, True


def _build_native_content(hit: SearchHit, opts: FetchOptions) -> Dict[str, Any]:
    text, truncated = _truncate(hit.raw_content or "", opts.max_content_chars)
    return {
        "status": "provider_native",
        "kind": "html",
        "text": text,
        "char_count": len(text),
        "truncated": truncated,
        "source": "tavily_raw_content" if hit.provider == "tavily" else "provider_native",
        "final_url": hit.url,
        "elapsed_ms": 0,
        "error": None,
    }


def _build_skipped_content(reason: str = "fetch disabled") -> Dict[str, Any]:
    return {
        "status": "skipped",
        "kind": None,
        "text": "",
        "char_count": 0,
        "truncated": False,
        "source": "skipped",
        "final_url": None,
        "elapsed_ms": 0,
        "error": reason,
    }


async def _fetch_one(
    hit: SearchHit,
    opts: FetchOptions,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
) -> Dict[str, Any]:
    # 延迟导入，避免 api/url_content_fetch 模块顶层依赖（DocumentParserService 等）
    # 在 web_search 单测里被强制加载
    from api.url_content_fetch import fetch_url_content

    started = time.monotonic()
    async with sem:
        try:
            r = await fetch_url_content(
                hit.url,
                client,
                doc_download_timeout=opts.doc_download_timeout_sec,
                html_timeout=opts.html_timeout_sec,
                max_chars=opts.max_content_chars,
            )
        except Exception as exc:
            return {
                "status": "http_error",
                "kind": None,
                "text": "",
                "char_count": 0,
                "truncated": False,
                "source": "url_content_fetch",
                "final_url": None,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}",
            }

    text = r.get("content_text") or ""
    char_count = len(text)
    # url_content_fetch 已按 max_chars 截断，但其语义是"硬截断不加尾标"；这里补 truncated 标志
    truncated = char_count >= opts.max_content_chars  # 近似判断
    return {
        "status": r.get("content_fetch_status") or "skipped",
        "kind": r.get("content_kind") or None,
        "text": text,
        "char_count": char_count,
        "truncated": bool(truncated and char_count > 0),
        "source": "url_content_fetch",
        "final_url": r.get("final_url") or hit.url,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "error": r.get("content_error"),
    }


async def enrich_with_content(
    hits: List[SearchHit],
    opts: FetchOptions,
    client: httpx.AsyncClient,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """把 SearchHit 列表批量挂上 content 子对象。

    Returns
    -------
    (contents, summary)
        - ``contents[i]`` 与 ``hits[i]`` 一一对应；
        - ``summary`` = {requested, ok, skipped, failed, elapsed_ms}
    """
    summary = {"requested": 0, "ok": 0, "skipped": 0, "failed": 0, "elapsed_ms": 0}
    if not hits:
        return [], summary

    started = time.monotonic()

    # 抓取范围
    fetch_count = opts.only_first_n if opts.only_first_n is not None else len(hits)
    fetch_count = max(0, min(fetch_count, len(hits)))

    # 不抓的尾巴直接 skipped
    tail = [_build_skipped_content("beyond only_first_n")] * (len(hits) - fetch_count)
    head_hits = hits[:fetch_count]
    summary["requested"] = len(head_hits)

    if not opts.enabled:
        contents = [_build_skipped_content("fetch disabled") for _ in head_hits] + tail
        summary["skipped"] = len(hits)
        summary["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return contents, summary

    # 拆分：能短路的 / 需要抓取的
    sem = asyncio.Semaphore(min(opts.concurrency, _settings.WEB_SEARCH_MAX_CONCURRENCY))
    tasks: List[Any] = []
    placeholders: Dict[int, Dict[str, Any]] = {}
    for idx, hit in enumerate(head_hits):
        if opts.prefer_provider_native_content and hit.raw_content:
            placeholders[idx] = _build_native_content(hit, opts)
            tasks.append(None)
        else:
            tasks.append(_fetch_one(hit, opts, client, sem))

    # 并发执行需要 HTTP 的那部分
    pending_indices = [i for i, t in enumerate(tasks) if t is not None]
    pending_results: List[Any] = []
    if pending_indices:
        pending_results = await asyncio.gather(
            *[tasks[i] for i in pending_indices],
            return_exceptions=True,
        )

    contents: List[Dict[str, Any]] = []
    pi = 0
    for idx, hit in enumerate(head_hits):
        if idx in placeholders:
            content = placeholders[idx]
        else:
            res = pending_results[pi]
            pi += 1
            if isinstance(res, BaseException):
                content = {
                    "status": "http_error",
                    "kind": None,
                    "text": "",
                    "char_count": 0,
                    "truncated": False,
                    "source": "url_content_fetch",
                    "final_url": None,
                    "elapsed_ms": 0,
                    "error": f"{type(res).__name__}: {res}",
                }
            else:
                content = res

        if content["status"] in ("ok", "provider_native"):
            summary["ok"] += 1
        elif content["status"] == "skipped":
            summary["skipped"] += 1
        else:
            summary["failed"] += 1
        contents.append(content)

    contents.extend(tail)
    summary["skipped"] += len(tail)
    summary["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return contents, summary


async def fetch_urls(
    urls: List[str],
    opts: FetchOptions,
    client: httpx.AsyncClient,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """供 /api/web/fetch 端点直接使用：把一批 URL 抓成 ContentOut 列表。"""
    hits = [
        SearchHit(rank=i + 1, title="", url=u, provider="external")
        for i, u in enumerate(urls)
    ]
    return await enrich_with_content(hits, opts, client)
