# -*- coding: utf-8 -*-
"""文档结构化解析服务（manifest，仿 Genspark import_pdf 产物结构）。

与既有 ``services/document_parser_service.py``（文档 -> LLM 友好 Markdown）**互补**：
本服务不产 Markdown，而是把文档拆成"每页高清 PNG + 该页文本层 + 该页内嵌图"，
每个资产上传为公网 URL，供下游多模态 workflow：
  - 每页 PNG 给 VLM「看版式/图表」；
  - 文本层给检索/大纲；
  - 内嵌图（真实 URL）被最终 Markdown 直接复用。

设计原则：
  - 复用现有基础设施：LibreOffice(office->pdf)、PyMuPDF(渲染+抽图)、
    DocumentAssetUploadService(上传对象存储)。
  - 幂等：同一 (file 内容 + 关键选项) 产出同一 doc_id（sha256），便于上游缓存。
  - 软失败：单页/单图渲染或上传失败不阻断整篇，记入 warnings 并把对应 URL 置 None。

依赖（硬）: PyMuPDF (fitz)、Pillow。缺失时抛出明确错误，不静默降级。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
from pathlib import Path
from typing import List, Optional, Tuple

from schemas.document_manifest import (
    DocImageType,
    DocumentManifest,
    DocumentParseOptions,
    ManifestImage,
    ManifestPage,
    ManifestSource,
)
from services.document_asset_service import DocumentAssetUploadService
from services.office_convert import office_bytes_to_pdf as _office_bytes_to_pdf  # 复用 LibreOffice 转 PDF
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - 部署环境应安装
    fitz = None

try:
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover
    PILImage = None

logger = setup_module_logger(__name__, "logs/file/document_manifest.log")

_OFFICE_EXTS = {".docx", ".doc", ".pptx", ".ppt"}
_PDF_EXT = ".pdf"
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class ManifestParseError(Exception):
    def __init__(self, status_code: int, code: str, detail: str):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _normalize_ext(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    return ext


def compute_doc_id(content: bytes, options: DocumentParseOptions) -> str:
    """幂等 doc_id：内容 + 影响产物的关键选项。"""
    h = hashlib.sha256()
    h.update(content)
    # 仅纳入会改变产物字节的选项，caption 只影响文字描述不改页图，故也纳入以便缓存分层。
    key = f"|dpi={options.dpi}|imgs={int(options.extract_images)}|cap={int(options.vlm_caption)}"
    h.update(key.encode("utf-8"))
    return h.hexdigest()[:32]


class DocumentManifestService:
    """把文档拆成 manifest（每页 PNG + 文本 + 内嵌图）。"""

    def __init__(self) -> None:
        if fitz is None:
            raise ManifestParseError(
                503, "pymupdf_unavailable", "服务端未安装 PyMuPDF，无法做结构化拆页。"
            )
        self.default_max_pages = _settings.DOC_IMPORT_MAX_PAGES
        self.default_min_img_bytes = _settings.DOC_IMPORT_MIN_IMG_BYTES
        self.default_min_img_dim = _settings.DOC_IMPORT_MIN_IMG_DIM

    # ── 上传：单张图片 -> 公网 URL（走既有对象存储服务，与内嵌图同源） ──
    @staticmethod
    def _upload_one(filename: str, data: bytes, mime: str) -> Optional[str]:
        url_map = DocumentAssetUploadService.upload_images([(filename, data, mime)])
        return url_map.get(filename)

    async def _upload_one_async(self, filename: str, data: bytes, mime: str) -> Optional[str]:
        return await asyncio.to_thread(self._upload_one, filename, data, mime)

    # ── office -> pdf ────────────────────────────────────────
    async def _ensure_pdf_bytes(
        self, content: bytes, ext: str
    ) -> Tuple[bytes, Optional[str]]:
        """返回 (pdf_bytes, converted_from)。pdf/图片直接处理，office 转 pdf。"""
        if ext == _PDF_EXT:
            return content, None
        if ext in _OFFICE_EXTS:
            pdf_bytes = await asyncio.to_thread(_office_bytes_to_pdf, content, ext)
            return pdf_bytes, ext
        raise ManifestParseError(
            400, "unsupported_ext", f"结构化拆页暂不支持扩展名 {ext}（仅 pdf/office）。"
        )

    # ── 渲染单页为 PNG ───────────────────────────────────────
    @staticmethod
    def _render_page_png(page, dpi: int) -> Tuple[bytes, int, int]:
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        png_bytes = pix.tobytes("png")
        return png_bytes, pix.width, pix.height

    # ── 抽单页内嵌图（带 bbox） ──────────────────────────────
    def _extract_page_images(
        self, page, page_index: int, min_bytes: int, min_dim: int
    ) -> List[Tuple[str, bytes, str, List[float], int, int]]:
        """返回 [(filename, data, mime, bbox, w, h), ...]，按页面 y 顺序。"""
        out: List[Tuple[str, bytes, str, List[float], int, int]] = []
        try:
            page_dict = page.get_text("dict", sort=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("page %s get_text dict failed: %s", page_index, e)
            return out
        img_idx = 0
        for block in page_dict.get("blocks", []):
            if block.get("type") != 1:
                continue
            bbox = block.get("bbox", [0, 0, 0, 0])
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            if w < min_dim or h < min_dim:
                continue
            img_bytes = block.get("image", b"")
            if len(img_bytes) < min_bytes:
                continue
            img_idx += 1
            ext = "png"
            if img_bytes[:3] == b"\xff\xd8\xff":
                ext = "jpg"
            filename = f"doc_p{page_index}_img{img_idx}.{ext}"
            mime = f"image/{'jpeg' if ext == 'jpg' else ext}"
            px_w, px_h = self._pixel_size(img_bytes)
            out.append((filename, img_bytes, mime, [round(v, 2) for v in bbox], px_w, px_h))
        return out

    @staticmethod
    def _pixel_size(img_bytes: bytes) -> Tuple[int, int]:
        if PILImage is None:
            return 0, 0
        try:
            with PILImage.open(io.BytesIO(img_bytes)) as im:
                return im.size[0], im.size[1]
        except Exception:  # noqa: BLE001
            return 0, 0

    # ── 图片文件（单图）走独立分支 ───────────────────────────
    async def _build_single_image_manifest(
        self, content: bytes, ext: str, filename: str, mime: Optional[str],
        options: DocumentParseOptions, doc_id: str,
    ) -> DocumentManifest:
        warnings: List[str] = []
        px_w, px_h = self._pixel_size(content)
        img_mime = mime or f"image/{'jpeg' if ext in ('.jpg', '.jpeg') else ext.lstrip('.')}"
        url = await self._upload_one_async(filename or f"{doc_id}{ext}", content, img_mime)
        if not url:
            warnings.append("图片上传失败，img_url 为空。")
        image = ManifestImage(
            img_url=url,
            filename=filename or f"{doc_id}{ext}",
            mime_type=img_mime,
            size=len(content),
            width=px_w or None,
            height=px_h or None,
            img_type=DocImageType.UNKNOWN,
            upload_status="uploaded" if url else "failed",
        )
        page = ManifestPage(
            index=1,
            page_png_url=url,  # 单图：页图即其本身
            page_width=px_w or None,
            page_height=px_h or None,
            text="",
            images=[image],
        )
        return DocumentManifest(
            doc_id=doc_id,
            source=ManifestSource(
                name=filename or f"{doc_id}{ext}", mime=img_mime, ext=ext,
                size=len(content), page_count=1,
            ),
            pages=[page],
            assets_base=_settings.DOC_PARSER_IMAGE_UPLOAD_URL or None,
            meta={"kind": "image"},
            warnings=warnings,
        )

    # ── 主流程（同步核心，在线程池调用） ────────────────────
    def _parse_pdf_sync(
        self, pdf_bytes: bytes, options: DocumentParseOptions,
    ) -> Tuple[List[dict], int, List[str]]:
        """渲染每页 PNG + 抽文本 + 抽内嵌图（未上传）。返回 (pages_raw, total_pages, warnings)。

        pages_raw[i] = {index, png_bytes, w, h, text, images:[(fname,data,mime,bbox,w,h)]}
        """
        warnings: List[str] = []
        max_pages = options.max_pages or self.default_max_pages
        min_bytes = options.min_img_bytes or self.default_min_img_bytes
        min_dim = options.min_img_dim or self.default_min_img_dim
        pages_raw: List[dict] = []

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            total = len(doc)
            limit = min(total, max_pages)
            if total > limit:
                warnings.append(f"文档共 {total} 页，已处理前 {limit} 页。")
            for pi in range(limit):
                page = doc.load_page(pi)
                index = pi + 1
                entry: dict = {"index": index}
                # 页图
                try:
                    png_bytes, w, h = self._render_page_png(page, options.dpi)
                    entry.update({"png_bytes": png_bytes, "w": w, "h": h})
                except Exception as e:  # noqa: BLE001
                    logger.warning("render page %s failed: %s", index, e)
                    warnings.append(f"第 {index} 页渲染失败，page_png_url 为空。")
                    entry.update({"png_bytes": None, "w": None, "h": None})
                # 文本层
                try:
                    entry["text"] = page.get_text("text", sort=True).strip()
                except Exception as e:  # noqa: BLE001
                    logger.warning("page %s text failed: %s", index, e)
                    entry["text"] = ""
                # 内嵌图
                if options.extract_images:
                    entry["images"] = self._extract_page_images(page, index, min_bytes, min_dim)
                else:
                    entry["images"] = []
                pages_raw.append(entry)
            return pages_raw, total, warnings

    async def build_manifest(
        self,
        content: bytes,
        filename: str,
        mime: Optional[str],
        options: DocumentParseOptions,
    ) -> DocumentManifest:
        t0 = time.time()
        ext = _normalize_ext(filename)
        if not ext:
            raise ManifestParseError(400, "missing_extension", "文件名缺少扩展名。")
        doc_id = compute_doc_id(content, options)

        # 单图分支
        if ext in _IMAGE_EXTS:
            return await self._build_single_image_manifest(
                content, ext, filename, mime, options, doc_id
            )

        # pdf / office 分支
        converted_from: Optional[str] = None
        pdf_bytes, converted_from = await self._ensure_pdf_bytes(content, ext)

        pages_raw, total_pages, warnings = await asyncio.to_thread(
            self._parse_pdf_sync, pdf_bytes, options
        )

        # 上传所有页图 + 内嵌图（并发），组装 manifest
        pages: List[ManifestPage] = []
        upload_tasks: List = []
        # 先收集需要上传的资产，用占位记录回填。
        for entry in pages_raw:
            index = entry["index"]
            page_png_url_task = None
            if entry.get("png_bytes"):
                page_png_url_task = self._upload_one_async(
                    f"{doc_id}_page_{index:03d}.png", entry["png_bytes"], "image/png"
                )
            img_tasks = []
            for (fname, data, img_mime, bbox, iw, ih) in entry.get("images", []):
                img_tasks.append(
                    (fname, img_mime, bbox, iw, ih, len(data),
                     self._upload_one_async(fname, data, img_mime))
                )
            upload_tasks.append((entry, page_png_url_task, img_tasks))

        for (entry, page_png_url_task, img_tasks) in upload_tasks:
            index = entry["index"]
            page_png_url = await page_png_url_task if page_png_url_task else None
            if entry.get("png_bytes") and not page_png_url:
                warnings.append(f"第 {index} 页页图上传失败，page_png_url 为空。")
            images: List[ManifestImage] = []
            for (fname, img_mime, bbox, iw, ih, size, task) in img_tasks:
                url = await task
                images.append(
                    ManifestImage(
                        img_url=url,
                        filename=fname,
                        mime_type=img_mime,
                        size=size,
                        bbox=bbox,
                        width=iw or None,
                        height=ih or None,
                        img_type=DocImageType.UNKNOWN,
                        upload_status="uploaded" if url else "failed",
                        upload_error=None if url else "upload_failed",
                    )
                )
            pages.append(
                ManifestPage(
                    index=index,
                    page_png_url=page_png_url,
                    page_width=entry.get("w"),
                    page_height=entry.get("h"),
                    text=entry.get("text", ""),
                    images=images,
                )
            )

        if not _settings.DOC_PARSER_IMAGE_UPLOAD_URL:
            warnings.append("未配置 DOC_PARSER_IMAGE_UPLOAD_URL，页图/内嵌图未上传（URL 为空）。")

        manifest = DocumentManifest(
            doc_id=doc_id,
            source=ManifestSource(
                name=filename,
                mime=mime,
                ext=ext,
                size=len(content),
                page_count=total_pages,
                converted_from=converted_from,
            ),
            pages=pages,
            assets_base=_settings.DOC_PARSER_IMAGE_UPLOAD_URL or None,
            meta={
                "kind": "document",
                "dpi": options.dpi,
                "processed_pages": len(pages),
                "total_pages": total_pages,
                "embedded_image_count": sum(len(p.images) for p in pages),
                "elapsed_sec": round(time.time() - t0, 2),
            },
            warnings=warnings,
        )
        logger.info(
            "manifest built doc_id=%s ext=%s pages=%s imgs=%s converted_from=%s elapsed=%.2fs",
            doc_id, ext, len(pages), manifest.meta["embedded_image_count"],
            converted_from, time.time() - t0,
        )
        return manifest
