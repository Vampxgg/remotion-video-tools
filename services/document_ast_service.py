# -*- coding: utf-8 -*-
"""文档 AST / 元素抽取层：把文档拆成"带锚点的元素序列"。

服务于元素级视觉理解：不再把整份 PDF 一次性喂大模型，而是先抽出
  - 图片元素：真实公网 URL（由基础解析层上传）+ 原始字节 + mime + bbox + 页号；
  - 表格元素：基础解析层已抽的 Markdown（作锚点与兜底）+ 是否低置信度 + 可选原页裁剪小图。

再由编排层仅对"去重/过滤后的图片"和"低置信度表格"逐元素调 VLM。

设计：
  - PDF/图片：用 PyMuPDF(fitz) 拿页坐标、抽图字节、find_tables 取表 bbox 并裁剪；
  - docx/pptx：用 LibreOffice 转 PDF 仅用于"取表格 bbox + 裁剪小图"（不是整份内联），
    图片仍复用基础解析层已上传的 URL；
  - 软失败：任何一步失败都不抛断整体，退化为"信任解析层"（表格不做视觉校对）。

本模块只做"抽取 + 打包"，不调用任何大模型。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from services.office_convert import _OFFICE_EXTS, office_bytes_to_pdf
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/document_ast.log")

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover - 缺失时全部降级为信任解析层
    fitz = None  # type: ignore[assignment]

_IMG_MD_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")


@dataclass
class ImageElement:
    """一个图片元素。anchor 用真实 URL（图片天然唯一锚点）。"""

    url: str
    data: Optional[bytes] = None
    mime: str = "image/png"
    bbox: Optional[List[float]] = None
    page: Optional[int] = None


@dataclass
class TableElement:
    """一个表格元素。anchor 用 <!--TBL:n--> 的数字编号 n（与 base markdown 对齐）。"""

    anchor: str
    base_markdown: str
    low_confidence: bool = False
    crop_png: Optional[bytes] = None
    page: Optional[int] = None


@dataclass
class DocumentAST:
    """文档抽取产物。images/tables 供编排层逐元素视觉。"""

    images: List[ImageElement] = field(default_factory=list)
    tables: List[TableElement] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # 供诊断：解析层 markdown 里的表格锚点总数、find_tables 命中数等。
    stats: Dict[str, int] = field(default_factory=dict)


_TBL_MARKER_RE = re.compile(r"^<!--TBL:(\d+)-->$")


def _count_anchored_tables(anchored_md: str) -> List[Tuple[str, str]]:
    """从带锚点 markdown 中取出 [(anchor_id, table_markdown), ...]，保序。"""
    lines = anchored_md.split("\n")
    out: List[Tuple[str, str]] = []
    i = 0
    while i < len(lines):
        m = _TBL_MARKER_RE.match(lines[i].strip())
        if m:
            anchor_id = m.group(1)
            i += 1
            start = i
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                i += 1
            out.append((anchor_id, "\n".join(lines[start:i])))
            continue
        i += 1
    return out


def _table_is_low_confidence(md_table: str) -> bool:
    """启发式判定表格是否"低置信度"，需要视觉校对。

    命中任一：空单元格占比高、列数不齐（各行 | 数量不一致）、疑似合并单元格
    （连续空列）、或超小表（可能被误切）。这些用解析层文本往往不可靠。
    """
    rows = [r for r in md_table.split("\n") if r.strip().startswith("|")]
    if len(rows) < 2:
        return True
    # 去掉分隔行（|---|---|）
    body = [r for r in rows if not re.fullmatch(r"\s*\|(?:\s*:?-+:?\s*\|)+\s*", r)]
    if not body:
        return True
    col_counts = [r.count("|") for r in body]
    # 列数不齐 -> 结构可疑。
    if len(set(col_counts)) > 1:
        return True
    total_cells = 0
    empty_cells = 0
    for r in body:
        cells = [c.strip() for c in r.strip().strip("|").split("|")]
        total_cells += len(cells)
        empty_cells += sum(1 for c in cells if c == "")
    if total_cells == 0:
        return True
    empty_ratio = empty_cells / total_cells
    return empty_ratio >= _settings.FILE_UNDERSTAND_TABLE_LOWCONF_EMPTY_RATIO


def _pdf_bytes_from(content: bytes, ext: str) -> Tuple[Optional[bytes], Optional[str]]:
    """得到用于取页坐标的 PDF 字节。pdf 直接用；office 转 pdf；其余 None。"""
    if ext == ".pdf":
        return content, None
    if ext in _OFFICE_EXTS:
        try:
            return office_bytes_to_pdf(content, ext), ext
        except Exception as e:  # noqa: BLE001
            logger.warning("office 转 PDF 失败（表格视觉校对将退化为信任解析层）：%s", e)
            return None, None
    return None, None


def _find_tables_with_timeout(page: Any, timeout_sec: float, pi: int) -> Optional[Any]:
    """在独立线程里跑单页 find_tables，超时即放弃该页。

    PyMuPDF 的 find_tables 是持 GIL 的 C 扩展调用，遇到复杂矢量页会退化到近乎无限的
    CPU 计算，直接在事件循环/主线程调用会冻结整个进程（已在 ROS 手册复现）。这里用
    单线程 executor 提交并 result(timeout)：超时线程无法被强制中断，但主流程可放弃该页
    结果继续处理其余页，避免整份文档卡死。timeout<=0 表示不限制。
    """
    finder = getattr(page, "find_tables", None)
    if finder is None:
        return None
    if not timeout_sec or timeout_sec <= 0:
        return page.find_tables()
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(page.find_tables)
        try:
            return fut.result(timeout=timeout_sec)
        except FuturesTimeout:
            logger.warning("第 %s 页 find_tables 超过 %.1fs，跳过该页（保留解析层表格）", pi + 1, timeout_sec)
            # 不显式取消：C 扩展线程无法中断，交由 executor 退出时自行了结。
            return None


def _crop_tables_from_pdf(
    pdf_bytes: bytes, dpi: int, max_tables: int, page_timeout_sec: float = 4.0
) -> List[Tuple[List[float], bytes, int]]:
    """从 PDF 用 find_tables 抽表格 bbox 并裁剪小图。

    返回 [(bbox, png_bytes, page_index), ...]，按页序 + 页内 y 序。
    find_tables 不可用（老版本 fitz）时返回空列表（降级信任解析层）。
    每页 find_tables 受 page_timeout_sec 硬超时约束，防止单页复杂矢量图冻结进程。
    """
    if fitz is None:
        return []
    out: List[Tuple[List[float], bytes, int]] = []
    zoom = dpi / 72.0
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for pi, page in enumerate(doc):
                if len(out) >= max_tables:
                    break
                if getattr(page, "find_tables", None) is None:
                    return []  # 版本不支持，整体降级
                try:
                    tabs = _find_tables_with_timeout(page, page_timeout_sec, pi)
                except Exception as e:  # noqa: BLE001
                    logger.debug("第 %s 页 find_tables 失败：%s", pi + 1, e)
                    continue
                if tabs is None:
                    continue
                tables = list(getattr(tabs, "tables", []) or [])
                # 页内按 y 排序，稳定对齐 base markdown 的表序。
                tables.sort(key=lambda t: (getattr(t, "bbox", [0, 0, 0, 0]) or [0])[1])
                for t in tables:
                    if len(out) >= max_tables:
                        break
                    bbox = list(getattr(t, "bbox", []) or [])
                    if len(bbox) != 4:
                        continue
                    try:
                        clip = fitz.Rect(*bbox)
                        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
                        png = pix.tobytes("png")
                    except Exception as e:  # noqa: BLE001
                        logger.debug("裁剪表格失败 page=%s bbox=%s: %s", pi + 1, bbox, e)
                        continue
                    out.append(([round(v, 2) for v in bbox], png, pi + 1))
    except Exception as e:  # noqa: BLE001
        logger.warning("PDF 表格裁剪整体失败（降级信任解析层）：%s", e)
        return []
    return out


def build_document_ast(
    *,
    content: bytes,
    ext: str,
    base_markdown: str,
    anchored_markdown: str,
    n_tables: int,
    embedded_images: Optional[List[Tuple[str, bytes, str]]] = None,
    url_by_order: Optional[List[str]] = None,
    enable_table_vision: bool = True,
) -> DocumentAST:
    """组装 DocumentAST。

    :param base_markdown: 基础解析层产出的 markdown（含真实图片 URL 与初步表格）。
    :param anchored_markdown: 已由 _anchor_tables 打了 <!--TBL:n--> 的 markdown。
    :param n_tables: anchored markdown 中的表格数。
    :param embedded_images: 可选 [(filename, bytes, mime)]，解析层抽到的图字节，按序对应 URL。
    :param url_by_order: base markdown 中出现的真实图片 URL（去重保序）。
    """
    ast = DocumentAST()
    urls = url_by_order if url_by_order is not None else list(
        dict.fromkeys(_IMG_MD_RE.findall(base_markdown))
    )

    # 图片元素：URL 为锚点；字节按序对应（有则带上，无则编排层回退按 URL 下载）。
    imgs = embedded_images or []
    for idx, url in enumerate(urls):
        data = None
        mime = "image/png"
        if idx < len(imgs):
            _fn, data, mime = imgs[idx]
        ast.images.append(ImageElement(url=url, data=data, mime=mime))

    # 表格元素：先取 anchored markdown 里的每个表（作 base 与锚点）。
    anchored_tables = _count_anchored_tables(anchored_markdown)
    ast.stats["md_tables"] = len(anchored_tables)

    crops: List[Tuple[List[float], bytes, int]] = []
    if enable_table_vision and anchored_tables and _settings.FILE_UNDERSTAND_TABLE_VISION_ENABLED:
        pdf_bytes, _conv = _pdf_bytes_from(content, ext)
        if pdf_bytes is not None:
            crops = _crop_tables_from_pdf(
                pdf_bytes,
                dpi=_settings.FILE_UNDERSTAND_TABLE_CROP_DPI,
                max_tables=_settings.FILE_UNDERSTAND_TABLE_VISION_MAX,
                page_timeout_sec=_settings.FILE_UNDERSTAND_TABLE_FIND_TIMEOUT_SEC,
            )
    ast.stats["cropped_tables"] = len(crops)

    # 对齐策略：find_tables 命中数与 markdown 表数一致时按序一一对应裁剪小图；
    # 不一致则不强行对齐（只按低置信度决定是否校对，crop 置空 -> 编排层无小图时退化）。
    aligned = len(crops) == len(anchored_tables) and len(crops) > 0
    for i, (anchor_id, tbl_md) in enumerate(anchored_tables):
        low = _table_is_low_confidence(tbl_md)
        crop_png = None
        page = None
        if aligned:
            bbox, crop_png, page = crops[i]
        ast.tables.append(
            TableElement(
                anchor=anchor_id,
                base_markdown=tbl_md,
                low_confidence=low,
                crop_png=crop_png,
                page=page,
            )
        )
    if crops and not aligned:
        ast.warnings.append(
            f"表格视觉裁剪数({len(crops)})与解析表数({len(anchored_tables)})不一致，"
            f"表格校对退化为信任解析层。"
        )
    return ast
