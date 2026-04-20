# -*- coding: utf-8 -*-
# URL 抓取 + 类型分流：对齐 ceshi/ceshi2.py SearchApiScraper.scrape（无 print 调试输出）

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx
import trafilatura
from bs4 import BeautifulSoup
from trafilatura.settings import use_config

from api.document_parser_service import DataCleaningPipeline, DocumentParserService
from utils.settings import settings as _settings

DEFAULT_UA = _settings.FETCH_USER_AGENT
MAX_DOCUMENT_BYTES = _settings.URL_FETCH_MAX_DOCUMENT_BYTES

SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".csv",
    ".txt", ".md", ".markdown", ".json", ".xml",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
}

_parser_singleton: Optional[DocumentParserService] = None


def _get_parser() -> DocumentParserService:
    global _parser_singleton
    if _parser_singleton is None:
        _parser_singleton = DocumentParserService()
    return _parser_singleton


def _content_kind_from_ext(ext: str) -> str:
    ext = ext.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"):
        return "office"
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return "image"
    if ext in (".txt", ".md", ".markdown", ".json", ".xml", ".csv"):
        return "text"
    return "other"


class _HtmlContentCleaner:
    """与 ceshi2 SearchApiScraper._clean_content_async / _parse_videos_from_html 对齐（同步子集）。"""

    NOISY_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
        r"^\s*$", r"^[\-=*#_]{3,}$", r".*\.(html|shtml|htm|php)\s*$",
        r".{0,50}(搜狐|网易|腾讯|新浪|登录|注册|版权所有|版权声明).{0,50}$",
        r"\[\d+\]|\[下一页\]|\[上一页\]", r"\[(编辑|查看历史|讨论|阅读|来源|原标题)\]",
        r"^\*+\s*\[.*?\]\(.*?\)",
        r"^\s*(分享到|扫描二维码|返回搜狐|查看更多|责任编辑|记者|通讯员)",
        r"^\s*([京公网安备京网文京ICP备]|互联网新闻信息服务许可证|信息网络传播视听节目许可证)",
    ]]
    IMG_PATTERN = re.compile(r"(!\[(.*?)\]\((.*?)\))")
    LINK_PATTERN = re.compile(r"\[.*?\]\(.*?\)")
    EDITOR_PATTERN = re.compile(r"(\(|\[)\s*责任编辑：.*?\s*(\)|\])")

    @classmethod
    def _is_noisy_line(cls, line: str) -> bool:
        stripped = line.strip()
        for pat in cls.NOISY_PATTERNS:
            if pat.search(stripped):
                return True
        links = cls.LINK_PATTERN.findall(stripped)
        if len(links) > 2 and len(stripped) / (len(links) + 1) < 30:
            return True
        return False

    @classmethod
    def clean_markdown_sync(cls, text: str) -> str:
        if not text:
            return ""
        lines = text.splitlines()
        cleaned_lines = []
        for line in lines:
            if cls._is_noisy_line(line):
                continue
            line = cls.EDITOR_PATTERN.sub("", line).strip()
            if line:
                cleaned_lines.append(line)
        out = []
        for i, line in enumerate(cleaned_lines):
            if i > 0 and not line.strip() and not cleaned_lines[i - 1].strip():
                continue
            out.append(line)
        return "\n".join(out).strip()

    @staticmethod
    def parse_videos(html: str, base_url: str) -> List[str]:
        try:
            soup = BeautifulSoup(html, "lxml")
            videos = []
            for video in soup.find_all("video"):
                src = video.get("src")
                if src:
                    videos.append(urljoin(base_url, src))
                for source in video.find_all("source"):
                    src = source.get("src")
                    if src:
                        videos.append(urljoin(base_url, src))
            for iframe in soup.find_all("iframe"):
                src = iframe.get("src")
                if src and any(k in src for k in ("youtube", "vimeo", "embed", ".mp4")):
                    videos.append(urljoin(base_url, src))
            return list(dict.fromkeys(videos))
        except Exception:
            return []


async def _validate_images_in_md(md: str, client: httpx.AsyncClient) -> str:
    """简化版：与 DataCleaningPipeline.validate_image_urls 一致思路。"""
    cleaner = DataCleaningPipeline()
    return await cleaner.validate_image_urls(md, client)


async def fetch_url_content(
    url: str,
    client: httpx.AsyncClient,
    *,
    doc_download_timeout: float = 60.0,
    html_timeout: float = 20.0,
    max_chars: int = 8000,
) -> Dict[str, Any]:
    """
    抓取单个 URL，返回 content_text（已截断）、content_fetch_status、content_kind、final_url。
    """
    base_out: Dict[str, Any] = {
        "content_text": "",
        "content_fetch_status": "skipped",
        "content_kind": "other",
        "content_error": None,
        "final_url": url,
    }
    if not url or not url.startswith(("http://", "https://")):
        base_out["content_fetch_status"] = "skipped"
        base_out["content_error"] = "非 http(s) URL"
        return base_out

    headers = {"User-Agent": os.environ.get("HTTP_FETCH_USER_AGENT", DEFAULT_UA)}
    url_lower = url.lower()
    ext = os.path.splitext(url_lower)[1]
    is_document = ext in SUPPORTED_EXTENSIONS
    content_type = ""
    content_length = 0

    try:
        try:
            async with client.stream(
                "HEAD", url, headers=headers, follow_redirects=True, timeout=15.0
            ) as head_response:
                content_type = (head_response.headers.get("content-type") or "").lower()
                cl = head_response.headers.get("content-length") or "0"
                try:
                    content_length = int(cl)
                except ValueError:
                    content_length = 0

                if not is_document:
                    if "pdf" in content_type:
                        is_document = True
                        ext = ".pdf"
                    elif "word" in content_type or "officedocument" in content_type:
                        is_document = True
                        ext = ".docx"
                    elif "excel" in content_type or "spreadsheet" in content_type:
                        is_document = True
                        ext = ".xlsx"
                    elif "powerpoint" in content_type or "presentation" in content_type:
                        is_document = True
                        ext = ".pptx"
                    elif "csv" in content_type:
                        is_document = True
                        ext = ".csv"
                    elif "image/" in content_type:
                        is_document = True
                        if "png" in content_type:
                            ext = ".png"
                        elif "gif" in content_type:
                            ext = ".gif"
                        elif "webp" in content_type:
                            ext = ".webp"
                        elif "bmp" in content_type:
                            ext = ".bmp"
                        else:
                            ext = ".jpg"
                    elif "application/json" in content_type:
                        is_document = True
                        ext = ".json"
                    elif "text/xml" in content_type or "application/xml" in content_type:
                        is_document = True
                        ext = ".xml"
                    elif "text/plain" in content_type and ext not in SUPPORTED_EXTENSIONS:
                        is_document = True
                        ext = ".txt"
                    elif "text/markdown" in content_type:
                        is_document = True
                        ext = ".md"

                if is_document and content_length > MAX_DOCUMENT_BYTES:
                    base_out["content_fetch_status"] = "too_large"
                    base_out["content_error"] = (
                        f"文档过大 ({content_length / 1024 / 1024:.2f}MB > 20MB)"
                    )
                    base_out["content_kind"] = _content_kind_from_ext(ext)
                    return base_out
        except httpx.HTTPError:
            content_type = ""
            content_length = 0

        parser = _get_parser()
        cfg = use_config()
        cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "10")

        if is_document:
            base_out["content_kind"] = _content_kind_from_ext(ext or ".bin")

            async def _download():
                resp = await client.get(
                    url, headers=headers, follow_redirects=True, timeout=doc_download_timeout
                )
                resp.raise_for_status()
                return str(resp.url), await resp.aread()

            try:
                final_url, file_bytes = await asyncio.wait_for(_download(), timeout=doc_download_timeout)
            except asyncio.TimeoutError:
                base_out["content_fetch_status"] = "timeout"
                base_out["content_error"] = "下载超时"
                return base_out
            except httpx.HTTPError as e:
                base_out["content_fetch_status"] = "http_error"
                base_out["content_error"] = str(e)
                return base_out

            base_out["final_url"] = final_url
            if len(file_bytes) > MAX_DOCUMENT_BYTES:
                base_out["content_fetch_status"] = "too_large"
                base_out["content_error"] = "下载后超过 20MB"
                return base_out

            raw = await asyncio.to_thread(
                parser.parse, file_bytes, ext or ".bin", url
            )
            if not (raw and raw.strip()):
                base_out["content_fetch_status"] = "empty"
                base_out["content_error"] = "解析结果为空"
                return base_out
            text = raw[:max_chars] if len(raw) > max_chars else raw
            base_out["content_text"] = text
            base_out["content_fetch_status"] = "ok"
            return base_out

        # HTML
        base_out["content_kind"] = "html"
        try:
            response = await client.get(
                url, headers=headers, follow_redirects=True, timeout=html_timeout
            )
            response.raise_for_status()
        except asyncio.TimeoutError:
            base_out["content_fetch_status"] = "timeout"
            base_out["content_error"] = "GET 超时"
            return base_out
        except httpx.HTTPError as e:
            base_out["content_fetch_status"] = "http_error"
            base_out["content_error"] = str(e)
            return base_out

        final_url = str(response.url)
        base_out["final_url"] = final_url
        html_content = response.text
        raw_content = await asyncio.to_thread(
            trafilatura.extract,
            html_content,
            config=cfg,
            output_format="markdown",
            include_images=True,
            favor_recall=True,
        )
        if raw_content:
            cleaned = _HtmlContentCleaner.clean_markdown_sync(raw_content)
            cleaned = await _validate_images_in_md(cleaned, client)
            videos = _HtmlContentCleaner.parse_videos(html_content, final_url)
            if videos:
                cleaned += "\n\n## 参考视频:\n" + "\n".join(f"- {v}" for v in videos)
            raw_content = cleaned

        if not raw_content:
            base_out["content_fetch_status"] = "empty"
            base_out["content_error"] = "内容提取为空"
            return base_out

        text = raw_content[:max_chars] if len(raw_content) > max_chars else raw_content
        base_out["content_text"] = text
        base_out["content_fetch_status"] = "ok"
        return base_out

    except Exception as e:
        base_out["content_fetch_status"] = "http_error"
        base_out["content_error"] = f"{type(e).__name__}: {e}"
        return base_out
