# -*- coding: utf-8 -*-
"""文档结构化解析 API 路由（manifest，仿 Genspark import_pdf）。

对外两种入口，产物均为 ``DocumentManifest``（每页 PNG + 文本层 + 内嵌图，含 VLM 描述）：
  - ``POST /parse/document``           : multipart 直接上传文件（字段名 file）。
  - ``POST /parse/document/by-url``     : JSON 传 file_url，由服务端拉取后解析。

供 PPT workflow 把"参考附件"直接结构化为多模态素材，替换历史 large_text 纯文本入口。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from schemas.document_manifest import DocumentParseOptions, DocumentParseRequest
from services.document_manifest_service import (
    DocumentManifestService,
    ManifestParseError,
)
from services.document_vlm_caption_service import caption_manifest_images
from services.slide_self_check_service import check_slide
from utils.responses import create_standard_response
from utils.security import require_api_key
from utils.settings import settings as _settings
from pydantic import BaseModel, Field
from typing import List as _List

router = APIRouter(dependencies=[Depends(require_api_key("DOC_IMPORT_API_KEY"))])
logger = logging.getLogger(__name__)


def _build_options(
    dpi: Optional[int],
    extract_images: Optional[bool],
    vlm_caption: Optional[bool],
    vlm_chart_to_table: Optional[bool],
    max_pages: Optional[int],
) -> DocumentParseOptions:
    return DocumentParseOptions(
        dpi=dpi or _settings.DOC_IMPORT_DEFAULT_DPI,
        extract_images=True if extract_images is None else extract_images,
        vlm_caption=True if vlm_caption is None else vlm_caption,
        vlm_chart_to_table=True if vlm_chart_to_table is None else vlm_chart_to_table,
        max_pages=max_pages,
    )


async def _read_upload(upload: UploadFile) -> bytes:
    limit = _settings.DOC_IMPORT_MAX_UPLOAD_MB * 1024 * 1024
    chunks = []
    size = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise ManifestParseError(
                413, "file_too_large",
                f"文件过大，最大支持 {_settings.DOC_IMPORT_MAX_UPLOAD_MB}MB。",
            )
        chunks.append(chunk)
    if size == 0:
        raise ManifestParseError(400, "empty_file", "上传文件为空。")
    return b"".join(chunks)


async def _fetch_url(url: str) -> tuple[bytes, Optional[str], str]:
    """按 file_url 拉取源文件，返回 (content, mime, filename)。"""
    limit = _settings.DOC_IMPORT_FETCH_MAX_MB * 1024 * 1024
    timeout = httpx.Timeout(
        _settings.DOC_IMPORT_FETCH_TIMEOUT_SEC, connect=15.0
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, verify=False) as client:
        resp = await client.get(url, headers={"User-Agent": _settings.FETCH_USER_AGENT})
        resp.raise_for_status()
        content = resp.content
        if len(content) > limit:
            raise ManifestParseError(
                413, "file_too_large",
                f"下载文件过大，最大支持 {_settings.DOC_IMPORT_FETCH_MAX_MB}MB。",
            )
        mime = resp.headers.get("content-type", "").split(";")[0].strip() or None
        filename = Path(url.split("?")[0]).name or "download"
        return content, mime, filename


async def _run(
    content: bytes, filename: str, mime: Optional[str], options: DocumentParseOptions
):
    service = DocumentManifestService()
    manifest = await service.build_manifest(content, filename, mime, options)
    if options.vlm_caption:
        await caption_manifest_images(manifest, options)
    return manifest


@router.post(
    "/parse/document",
    summary="结构化解析上传文件为 manifest（每页PNG+文本层+内嵌图，含VLM描述）",
)
async def parse_document(
    file: UploadFile = File(..., description="唯一文件字段，字段名必须为 file"),
    dpi: Optional[int] = Form(None, description="每页PNG渲染DPI，默认服务端配置"),
    extract_images: Optional[bool] = Form(None, description="是否抽取内嵌图，默认True"),
    vlm_caption: Optional[bool] = Form(None, description="是否为内嵌图做VLM打标，默认True"),
    vlm_chart_to_table: Optional[bool] = Form(None, description="chart是否转Markdown表，默认True"),
    max_pages: Optional[int] = Form(None, description="最大处理页数，默认服务端配置"),
):
    filename = file.filename or "unknown"
    try:
        content = await _read_upload(file)
        options = _build_options(dpi, extract_images, vlm_caption, vlm_chart_to_table, max_pages)
        manifest = await _run(content, filename, file.content_type, options)
    except ManifestParseError as exc:
        return create_standard_response(
            data={"error": {"code": exc.code, "detail": exc.detail}},
            code=exc.status_code,
            message=exc.detail,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("document manifest parse failed: %s", exc)
        detail = "文档结构化解析失败，请稍后重试或联系服务维护人员。"
        return create_standard_response(
            data={"error": {"code": "internal_error", "detail": detail}},
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=detail,
        )
    finally:
        await file.close()

    return create_standard_response(
        data=manifest.model_dump(),
        message="文档结构化解析完成",
    )


# ============ 单页自检自愈（档C） ============

class SlideCheckItem(BaseModel):
    slide_id: Optional[str] = Field(None, description="页 ID")
    layout_type: Optional[str] = Field(None, description="layout_1..7")
    generated_markdown: str = Field(..., description="该页生成的 Markdown")


class SlideCheckRequest(BaseModel):
    slides: _List[SlideCheckItem] = Field(..., description="待校验的单页列表")
    check_liveness: bool = Field(True, description="是否 HEAD 探测图片死链")


@router.post(
    "/parse/slide/self_check",
    summary="单页版式/图文自检，返回 issues 与局部重写指令（档C，供 ppt_synthesizing 前自愈）",
)
async def slide_self_check(payload: SlideCheckRequest):
    try:
        results = []
        for item in payload.slides:
            res = await check_slide(
                markdown=item.generated_markdown,
                layout_type=item.layout_type,
                slide_id=item.slide_id,
                check_liveness=payload.check_liveness,
            )
            results.append(res.to_dict())
        needs_rewrite = [r for r in results if r["needs_rewrite"]]
        data = {
            "summary": {
                "total": len(results),
                "ok": sum(1 for r in results if r["ok"]),
                "needs_rewrite": len(needs_rewrite),
            },
            "results": results,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("slide self_check failed: %s", exc)
        detail = "单页自检失败，请稍后重试或联系服务维护人员。"
        return create_standard_response(
            data={"error": {"code": "internal_error", "detail": detail}},
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=detail,
        )
    return create_standard_response(data=data, message="单页自检完成")


@router.post(
    "/parse/document/by-url",
    summary="按 file_url 结构化解析为 manifest",
)
async def parse_document_by_url(payload: DocumentParseRequest):
    if not payload.file_url:
        return create_standard_response(
            data={"error": {"code": "missing_file_url", "detail": "需提供 file_url。"}},
            code=status.HTTP_400_BAD_REQUEST,
            message="需提供 file_url。",
        )
    try:
        content, mime, filename = await _fetch_url(payload.file_url)
        if payload.filename:
            filename = payload.filename
        if payload.mime:
            mime = payload.mime
        manifest = await _run(content, filename, mime, payload.options)
    except ManifestParseError as exc:
        return create_standard_response(
            data={"error": {"code": exc.code, "detail": exc.detail}},
            code=exc.status_code,
            message=exc.detail,
        )
    except httpx.HTTPError as exc:
        logger.warning("fetch file_url failed: %s", exc)
        return create_standard_response(
            data={"error": {"code": "fetch_failed", "detail": f"拉取 file_url 失败：{exc}"}},
            code=status.HTTP_502_BAD_GATEWAY,
            message="拉取源文件失败。",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("document manifest by-url failed: %s", exc)
        detail = "文档结构化解析失败，请稍后重试或联系服务维护人员。"
        return create_standard_response(
            data={"error": {"code": "internal_error", "detail": detail}},
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=detail,
        )

    return create_standard_response(
        data=manifest.model_dump(),
        message="文档结构化解析完成",
    )
