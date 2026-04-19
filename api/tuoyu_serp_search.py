# -*- coding: utf-8 -*-
# 托育相关招聘 — Google 搜索 SERP（DrissionPage）+ 可选 URL 正文抓取

import asyncio
import os
import random
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse, urlencode, quote_plus

import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from DrissionPage import ChromiumPage
    from DrissionPage.common import By
except ImportError:
    ChromiumPage = None
    By = None

try:
    from utils.logger import setup_module_logger
except ImportError:
    import logging
    import sys

    def setup_module_logger(logger_name: str, log_file: str) -> logging.Logger:
        log = logging.getLogger(logger_name)
        if not log.handlers:
            h = logging.StreamHandler(sys.stdout)
            h.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
            log.addHandler(h)
            log.setLevel(logging.INFO)
        return log


logger = setup_module_logger(__name__, "logs/jobs/tuoyu_serp.log")

router = APIRouter()

BROWSER_HOST_PORT = "127.0.0.1:9527"
GOOGLE_SEARCH_HOST = os.environ.get("GOOGLE_SEARCH_HOST", "www.google.com")

INCLUDE_WECHAT_SITE_QUERY = True

# Google 自然结果（多 XPath 兜底，改版时需调整）
XPATH_GOOGLE_BLOCK_CANDIDATES = [
    '//div[@id="rso"]//div[contains(@class,"g ")][.//h3]',
    '//div[@id="rso"]//div[contains(@class,"MjjYud")][.//h3]',
    '//div[@id="search"]//div[contains(@class,"g ")][.//h3]',
    '//div[@id="search"]//div[contains(@class,"Gx5Zad")][.//h3]',
    '//div[@id="center_col"]//div[contains(@class,"g ")][.//h3]',
]
XPATH_SPONSORED_OR_AD = (
    './/span[contains(text(),"赞助")]|.//span[contains(text(),"广告")]'
    '|.//div[contains(@aria-label,"广告")]'
    '|.//span[contains(@class,"rz5jw")]'
)
XPATH_TITLE_LINK = './/a[.//h3]|.//h3/ancestor::a[1]'
XPATH_SNIPPET_GOOGLE = (
    './/div[contains(@class,"VwiC3b")]'
    '|.//div[contains(@class,"yXK7lf")]'
    '|.//div[contains(@class,"lEBKkf")]'
    '|.//div[contains(@class,"IsZvec")]'
)


class StandardResponse(BaseModel):
    code: int = Field(200, description="业务状态码")
    message: str = Field("Success", description="文本消息")
    data: Optional[Any] = Field(None, description="负载")
    timestamp: str = Field(..., description="ISO 时间戳")


def create_standard_response(
    data: Optional[Any] = None,
    code: int = 200,
    message: str = "Success",
) -> JSONResponse:
    content = StandardResponse(
        code=code,
        message=message,
        data=data,
        timestamp=datetime.now().isoformat(),
    ).model_dump()
    return JSONResponse(status_code=code, content=content)


def build_default_queries(keyword: str) -> List[str]:
    kw = keyword.strip()
    queries = [
        f"事业单位招聘 {kw} 粉笔",
        f"{kw}招聘 本地宝",
    ]
    if INCLUDE_WECHAT_SITE_QUERY:
        queries.append(f"{kw} 招聘 site:mp.weixin.qq.com")
    return queries


def _host_from_cite(cite_text: str) -> str:
    if not cite_text:
        return ""
    t = cite_text.strip().split()[0]
    t = re.sub(r"^https?://", "", t, flags=re.I)
    t = t.split("/")[0]
    return t.lower()


def classify_source(display_host: str, href: str) -> str:
    blob = f"{display_host} {href}".lower()
    if "fenbi.com" in blob:
        return "fenbi"
    if "bendibao" in blob:
        return "bendibao"
    if "mp.weixin.qq.com" in blob or "weixin.qq.com" in blob:
        return "wechat_public"
    if ".gov.cn" in blob:
        return "gov"
    if "google." in blob and "/url" in blob:
        return "google_redirect"
    if "baidu.com/link" in blob or "baidu.com/baidu.php" in blob:
        return "baidu_redirect"
    if display_host:
        return "other"
    return "other"


def normalize_url_for_dedup(url: str, cite_host: str) -> str:
    if not url:
        return ""
    u = url.strip()
    parsed = urlparse(u)
    if "google." in parsed.netloc.lower() and "/url" in parsed.path:
        qs = parse_qs(parsed.query)
        for key in ("q", "url"):
            vals = qs.get(key)
            if vals and vals[0]:
                inner = unquote(vals[0])
                p2 = urlparse(inner if "://" in inner else "http://" + inner)
                if p2.netloc:
                    path = p2.path or "/"
                    return f"{p2.scheme}://{p2.netloc}{path}".lower().rstrip("/")
        if cite_host:
            return f"cite:{cite_host}"
        return u
    if parsed.netloc.endswith("baidu.com") and "link" in parsed.path:
        qs = parse_qs(parsed.query)
        enc = qs.get("url", [None])[0]
        if enc:
            try:
                inner = unquote(enc)
                p2 = urlparse(inner if "://" in inner else "http://" + inner)
                if p2.netloc:
                    path = p2.path or "/"
                    return f"{p2.scheme}://{p2.netloc}{path}".lower().rstrip("/")
            except Exception:
                pass
        if cite_host:
            return f"cite:{cite_host}"
        return u
    path = parsed.path or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}".lower().rstrip("/")


def _text(ele) -> str:
    if ele is None:
        return ""
    try:
        t = ele.text
        return t.strip() if t else ""
    except Exception:
        return ""


def _is_google_anomaly_page(page) -> bool:
    try:
        u = (page.url or "").lower()
        if "google.com/sorry" in u or "/sorry/" in u:
            return True
        if "consent.google" in u or "consent.youtube" in u:
            return True
        title = (page.title or "").lower()
        if "unusual traffic" in title or "captcha" in title or "robot" in title:
            return True
        html_snip = ""
        try:
            html_snip = (getattr(page, "html", None) or "")[:8000]
        except Exception:
            pass
        if "recaptcha" in html_snip.lower():
            return True
    except Exception:
        pass
    return False


def _collect_google_blocks(page) -> list:
    if not By:
        return []
    for xp in XPATH_GOOGLE_BLOCK_CANDIDATES:
        try:
            blocks = page.eles((By.XPATH, xp), timeout=5)
            if blocks:
                return list(blocks)
        except Exception as e:
            logger.debug("Google XPath 无结果: %s — %s", xp, e)
    return []


def parse_google_serp_page(page, query_used: str, max_results: int) -> List[Dict[str, Any]]:
    from api.search_url_utils import resolve_google_redirect_url

    items: List[Dict[str, Any]] = []
    if not By:
        return items

    blocks = _collect_google_blocks(page)
    if not blocks:
        logger.warning("当前页未解析到 Google 自然结果块（DOM 可能已改版）")

    for block in blocks:
        if len(items) >= max_results:
            break
        try:
            try:
                bad = block.ele((By.XPATH, XPATH_SPONSORED_OR_AD), timeout=0.2)
                if bad:
                    continue
            except Exception:
                pass

            try:
                a = block.ele((By.XPATH, XPATH_TITLE_LINK), timeout=1)
            except Exception:
                a = None
            if not a:
                continue

            title = _text(a)
            href = ""
            try:
                href = a.attr("href") or ""
            except Exception:
                pass
            resolved = resolve_google_redirect_url(href) if href else ""
            link_for_item = resolved or href

            if not title and not link_for_item:
                continue

            snippet = ""
            try:
                sn_el = block.ele((By.XPATH, XPATH_SNIPPET_GOOGLE), timeout=0.3)
                snippet = _text(sn_el)
            except Exception:
                pass

            cite_host = ""
            try:
                cite_el = block.ele((By.XPATH, './/cite|//span[contains(@class,"fYyStc")]'), timeout=0.2)
                cite_host = _host_from_cite(_text(cite_el))
            except Exception:
                pass

            if not cite_host and link_for_item.startswith("http"):
                try:
                    cite_host = urlparse(link_for_item).netloc.lower()
                except Exception:
                    pass

            channel = classify_source(cite_host, link_for_item)
            items.append(
                {
                    "title": title,
                    "url": link_for_item,
                    "snippet": snippet,
                    "query_used": query_used,
                    "source_channel": channel,
                    "display_host": cite_host or None,
                }
            )
        except Exception as e:
            logger.debug("单条 Google 结果解析跳过: %s", e)

    return items


def dedupe_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for it in items:
        dh = it.get("display_host") or ""
        key = normalize_url_for_dedup(it.get("url") or "", dh)
        if not key:
            key = f"title:{it.get('title', '')}"
        if key in seen:
            continue
        seen.add(key)
        clean = {k: v for k, v in it.items() if k != "display_host"}
        out.append(clean)
    return out


def _google_search_url(query: str) -> str:
    q = quote_plus(query)
    return f"https://{GOOGLE_SEARCH_HOST}/search?q={q}&hl=zh-CN&num=20"


def run_tuoyu_google_serp(
    keyword: str,
    max_results_per_query: int,
    queries: Optional[List[str]],
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    if not ChromiumPage or not By:
        raise RuntimeError("DrissionPage 未安装")

    qlist = queries if queries else build_default_queries(keyword)
    warnings: List[str] = []
    accumulated: List[Dict[str, Any]] = []

    page = None
    try:
        page = ChromiumPage(BROWSER_HOST_PORT).new_tab()
        for q in qlist:
            time.sleep(random.uniform(0.5, 1.2))
            search_url = _google_search_url(q)
            logger.info("Google 搜索: %s", q)
            page.get(search_url)
            time.sleep(random.uniform(0.8, 1.5))

            if _is_google_anomaly_page(page):
                w = f"查询可能触发 Google 验证/同意页，已跳过或结果不完整: {q}"
                logger.warning(w)
                warnings.append(w)
                continue

            batch = parse_google_serp_page(page, q, max_results_per_query)
            logger.info("查询「%s」解析到 %s 条", q, len(batch))
            accumulated.extend(batch)

    finally:
        if page:
            try:
                page.close()
            except Exception as e:
                logger.debug("关闭 tab: %s", e)

    deduped = dedupe_items(accumulated)
    return deduped, qlist, warnings


async def _enrich_items_with_content(
    items: List[Dict[str, Any]],
    max_urls: int,
    max_content_chars: int,
    fetch_timeout_sec: float,
    concurrency: int = 4,
) -> List[Dict[str, Any]]:
    from api.url_content_fetch import fetch_url_content  # 延迟导入，避免无 httpx 时加载解析栈

    if not items or max_urls <= 0:
        for it in items:
            it.setdefault("content_text", "")
            it.setdefault("content_fetch_status", "skipped")
            it.setdefault("content_kind", "other")
            it.setdefault("content_error", None)
        return items

    sem = asyncio.Semaphore(concurrency)
    to_fetch = items[:max_urls]

    async def one(it: Dict[str, Any]) -> None:
        url = it.get("url") or ""
        async with sem:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                r = await fetch_url_content(
                    url,
                    client,
                    doc_download_timeout=min(60.0, fetch_timeout_sec * 3),
                    html_timeout=fetch_timeout_sec,
                    max_chars=max_content_chars,
                )
        it["content_text"] = r.get("content_text") or ""
        it["content_fetch_status"] = r.get("content_fetch_status") or "skipped"
        it["content_kind"] = r.get("content_kind") or "other"
        it["content_error"] = r.get("content_error")
        if r.get("final_url"):
            it["fetched_url"] = r["final_url"]

    await asyncio.gather(*(one(it) for it in to_fetch))
    for it in items[max_urls:]:
        it.setdefault("content_text", "")
        it.setdefault("content_fetch_status", "skipped")
        it.setdefault("content_kind", "other")
        it.setdefault("content_error", None)
    return items


class TuoyuSerpRequest(BaseModel):
    keyword: str = Field(..., description="托育相关核心词，如：托育", min_length=1, max_length=64)
    max_results_per_query: int = Field(
        10,
        description="每个查询最多保留的搜索结果条数",
        ge=1,
        le=30,
    )
    queries: Optional[List[str]] = Field(
        None,
        description="自定义查询列表；为空则使用内置模板（事业单位+粉笔、本地宝、微信站内）",
    )
    fetch_page_content: bool = Field(
        False,
        description="是否对前 N 条结果抓取正文（HEAD 分流：文档走 DocumentParserService，网页走 trafilatura）",
    )
    max_urls_to_fetch: int = Field(8, description="最多抓取正文的条数（从去重后列表头部计）", ge=0, le=30)
    max_content_chars: int = Field(8000, description="单条正文最大字符数", ge=500, le=200000)
    fetch_timeout_sec: float = Field(20.0, description="单页 HTML GET 超时（秒）；文档下载可更长", ge=5.0, le=120.0)


@router.post(
    "/scrape/tuoyu-serp",
    summary="托育招聘 — Google 搜索 SERP（可选正文）",
    description=(
        "根据关键词使用内置或自定义查询串行访问 Google（主机可通过环境变量 GOOGLE_SEARCH_HOST 配置，"
        "默认 www.google.com），返回标题、链接、摘要及来源归类。"
        "可选 fetch_page_content 对结果 URL 抓取正文（api/url_content_fetch + api/document_parser_service）。"
        "需本机 DrissionPage 连接调试浏览器（默认 127.0.0.1:9527）。"
        "国内网络访问 Google 可能不稳定或出现验证页。"
    ),
)
async def tuoyu_google_serp(payload: TuoyuSerpRequest):
    if not ChromiumPage:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="未安装 DrissionPage，该接口不可用。",
        )

    loop = asyncio.get_running_loop()
    try:
        items, executed, warnings = await loop.run_in_executor(
            None,
            run_tuoyu_google_serp,
            payload.keyword,
            payload.max_results_per_query,
            payload.queries,
        )
    except Exception as e:
        logger.error("tuoyu-serp 执行失败: %s", e, exc_info=True)
        return create_standard_response(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=f"执行失败: {e}",
            data=None,
        )

    if payload.fetch_page_content:
        items = await _enrich_items_with_content(
            items,
            payload.max_urls_to_fetch,
            payload.max_content_chars,
            payload.fetch_timeout_sec,
        )
    else:
        for it in items:
            it.setdefault("content_text", "")
            it.setdefault("content_fetch_status", "skipped")
            it.setdefault("content_kind", "other")

    msg_parts = [f"共 {len(items)} 条（去重后），执行查询 {len(executed)} 个"]
    if warnings:
        msg_parts.append("部分查询可能受 Google 验证/同意页影响，结果不完整")
    if payload.fetch_page_content:
        ok_n = sum(1 for x in items if x.get("content_fetch_status") == "ok")
        msg_parts.append(f"正文抓取成功 {ok_n}/{min(len(items), payload.max_urls_to_fetch)} 条（上限内）")
    message = "；".join(msg_parts)

    return create_standard_response(
        data={
            "items": items,
            "queries_executed": executed,
            "total": len(items),
            "warnings": warnings,
            "google_host": GOOGLE_SEARCH_HOST,
        },
        message=message,
    )
