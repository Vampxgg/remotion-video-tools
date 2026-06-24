# -*- coding: utf-8 -*-
"""文件上传解析的业务编排层。

本模块不声明 FastAPI router，只负责校验业务参数、调用核心文档解析器、
组装对外稳定的解析结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from schemas.file_parse import (
    FileParseBatchData,
    FileParseBatchSummary,
    FileParseContent,
    FileParseError,
    FileParseFileInfo,
    FileParseMode,
    FileParseParserInfo,
    FileParseResult,
)
from services.document_parser_service import DocumentParserService
from utils.settings import settings as _settings


class ParseInputError(Exception):
    def __init__(self, status_code: int, code: str, detail: str):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class FilePayload:
    filename: str
    content: bytes
    media_type: Optional[str] = None

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def extension(self) -> str:
        return Path(self.filename or "").suffix.lower()


@dataclass(frozen=True)
class FileParseOptions:
    mode: FileParseMode = FileParseMode.MARKDOWN
    max_chars: Optional[int] = None
    enable_ocr: Optional[bool] = None
    enable_embedded_image_upload: Optional[bool] = None


def allowed_extensions() -> set[str]:
    return {
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in _settings.FILE_PARSE_ALLOWED_EXTENSIONS
    }


def content_kind_from_ext(ext: str) -> str:
    if ext == ".pdf":
        return "pdf"
    if ext in (".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls"):
        return "office"
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return "image"
    if ext in (".csv", ".txt", ".md", ".markdown", ".json", ".xml"):
        return "text"
    if ext in (".html", ".htm"):
        return "html"
    return "other"


def validate_payload(payload: FilePayload) -> str:
    ext = payload.extension
    if not ext:
        raise ParseInputError(400, "missing_extension", "文件名缺少扩展名，无法选择解析器。")
    if ext not in allowed_extensions():
        raise ParseInputError(400, "unsupported_extension", f"不支持的文件类型: {ext}")
    if payload.size == 0:
        raise ParseInputError(400, "empty_file", "上传文件为空。")
    max_bytes = _settings.FILE_PARSE_MAX_UPLOAD_MB * 1024 * 1024
    if payload.size > max_bytes:
        raise ParseInputError(
            413,
            "file_too_large",
            f"文件过大，最大支持 {_settings.FILE_PARSE_MAX_UPLOAD_MB}MB。",
        )
    return ext


def validate_batch_payloads(payloads: List[FilePayload]) -> None:
    if len(payloads) > _settings.FILE_PARSE_MAX_BATCH_FILES:
        raise ParseInputError(
            400,
            "too_many_files",
            f"单次最多支持 {_settings.FILE_PARSE_MAX_BATCH_FILES} 个文件。",
        )
    total_size = sum(payload.size for payload in payloads)
    total_limit = _settings.FILE_PARSE_MAX_TOTAL_MB * 1024 * 1024
    if total_size > total_limit:
        raise ParseInputError(
            413,
            "batch_too_large",
            f"批量文件总大小超过 {_settings.FILE_PARSE_MAX_TOTAL_MB}MB。",
        )


def failed_result(payload: FilePayload, exc: ParseInputError) -> FileParseResult:
    ext = payload.extension
    return FileParseResult(
        status="failed",
        file=FileParseFileInfo(
            filename=payload.filename,
            extension=ext,
            size=payload.size,
            media_type=payload.media_type,
        ),
        content=FileParseContent(),
        parser=FileParseParserInfo(content_kind=content_kind_from_ext(ext)),
        meta={},
        assets=[],
        warnings=[],
        error=FileParseError(code=exc.code, detail=exc.detail),
    )


def _markdown_to_text(markdown: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", markdown)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"```[a-zA-Z0-9_-]*\n?", "", text)
    text = text.replace("```", "")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"^[#>*\-\s]+", "", text, flags=re.MULTILINE)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _effective_max_chars(max_chars: Optional[int]) -> int:
    default = _settings.FILE_PARSE_DEFAULT_MAX_CHARS
    hard_limit = _settings.FILE_PARSE_MAX_CONTENT_CHARS
    if max_chars is None:
        return min(default, hard_limit)
    if max_chars <= 0:
        raise ParseInputError(400, "invalid_max_chars", "max_chars 必须大于 0。")
    if max_chars > hard_limit:
        raise ParseInputError(
            400,
            "max_chars_too_large",
            f"max_chars 不能超过服务端上限 {hard_limit}。",
        )
    return max_chars


async def parse_file_payload(payload: FilePayload, options: FileParseOptions) -> FileParseResult:
    ext = validate_payload(payload)
    effective_max_chars = _effective_max_chars(options.max_chars)
    ocr_enabled = (
        _settings.FILE_PARSE_ENABLE_OCR_DEFAULT
        if options.enable_ocr is None
        else options.enable_ocr
    )
    image_upload_enabled = (
        _settings.FILE_PARSE_ENABLE_IMAGE_UPLOAD_DEFAULT
        if options.enable_embedded_image_upload is None
        else options.enable_embedded_image_upload
    )

    parser = DocumentParserService(
        enable_embedded_image_upload=image_upload_enabled,
        enable_ocr=ocr_enabled,
    )
    core_result = await parser.parse_document_async(payload.content, ext)
    raw_markdown = core_result.markdown
    if not raw_markdown or raw_markdown.startswith("[无法解析 "):
        raise ParseInputError(503, "parse_failed", raw_markdown or "解析结果为空。")

    markdown, markdown_truncated = _truncate(raw_markdown, effective_max_chars)
    text_value = _markdown_to_text(raw_markdown)
    text, text_truncated = _truncate(text_value, effective_max_chars)

    include_markdown = options.mode in (FileParseMode.MARKDOWN, FileParseMode.STRUCTURED)
    include_text = options.mode in (FileParseMode.TEXT, FileParseMode.STRUCTURED)
    content_text = markdown if include_markdown else text
    route_truncated = markdown_truncated if include_markdown else text_truncated

    warnings = core_result.warning_messages()
    if route_truncated and "API 返回内容已按 max_chars 截断。" not in warnings:
        warnings.append("API 返回内容已按 max_chars 截断。")
    if ext in (".doc", ".ppt") and "旧版 Office 格式依赖 MarkItDown 兜底解析，结果稳定性取决于运行环境。" not in warnings:
        warnings.append("旧版 Office 格式依赖 MarkItDown 兜底解析，结果稳定性取决于运行环境。")
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp") and not ocr_enabled:
        warnings.append("本次请求未启用图片 OCR。")

    return FileParseResult(
        status="ok",
        file=FileParseFileInfo(
            filename=payload.filename,
            extension=ext,
            size=payload.size,
            media_type=payload.media_type,
        ),
        content=FileParseContent(
            markdown=markdown if include_markdown else None,
            text=text if include_text else None,
            char_count=len(content_text),
            truncated=route_truncated,
        ),
        parser=FileParseParserInfo(
            content_kind=core_result.content_kind,
            parser_used=core_result.parser_used,
            fallback_used=core_result.fallback_used,
        ),
        meta=core_result.meta if options.mode == FileParseMode.STRUCTURED else {},
        assets=core_result.assets_as_dicts(),
        warnings=warnings,
        error=None,
    )


async def parse_batch_payloads(
        payloads: List[FilePayload],
        options: FileParseOptions,
) -> FileParseBatchData:
    validate_batch_payloads(payloads)
    items: List[FileParseResult] = []
    for payload in payloads:
        try:
            items.append(await parse_file_payload(payload, options))
        except ParseInputError as exc:
            items.append(failed_result(payload, exc))

    ok = sum(1 for item in items if item.status == "ok")
    failed = len(items) - ok
    return FileParseBatchData(
        summary=FileParseBatchSummary(
            requested=len(payloads),
            ok=ok,
            failed=failed,
            partial_success=ok > 0 and failed > 0,
        ),
        items=items,
    )
