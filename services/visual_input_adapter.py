# -*- coding: utf-8 -*-
"""共享视觉输入适配器：把 PDF 按页渲染为 PNG。

Vertex Gemini 原生吃 PDF，但 Azure OpenAI 兼容端点只吃图片（image_url data URL），
因此备用 provider 需要先把 PDF 拆成页图。渲染能力复用 PyMuPDF（fitz），与
services/document_manifest_service.py 的 _render_page_png 同源，集中在此避免各处重复。
"""

from __future__ import annotations

from typing import List

from utils.logger import setup_module_logger

logger = setup_module_logger(__name__, "logs/file/visual_input.log")

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover - 缺失时上层据 [] 返回决定降级
    fitz = None  # type: ignore[assignment]


def render_pdf_pages_to_png(
    pdf_bytes: bytes, *, dpi: int = 150, max_pages: int = 0
) -> List[bytes]:
    """把 PDF 每页渲染为 PNG 字节。

    :param dpi: 渲染 DPI；150 兼顾清晰与体积。
    :param max_pages: 最多渲染页数，0=不限。超出的页丢弃（并由上层记 warning）。
    :return: PNG 字节列表；PyMuPDF 缺失或渲染失败返回空列表。
    """
    if fitz is None:
        logger.warning("PyMuPDF(fitz) 未安装，无法把 PDF 拆页为图片供备用 provider 使用。")
        return []
    out: List[bytes] = []
    zoom = dpi / 72.0
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for i, page in enumerate(doc):
                if max_pages and i >= max_pages:
                    break
                matrix = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                out.append(pix.tobytes("png"))
    except Exception as e:  # noqa: BLE001
        logger.warning("PDF 拆页渲染失败: %s", e)
        return out
    return out
