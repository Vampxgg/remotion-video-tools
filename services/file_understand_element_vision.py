# -*- coding: utf-8 -*-
"""元素级视觉理解编排器（AST -> 补丁）。

输入 DocumentAST，仅对"去重 + 重要性过滤后的图片"与"低置信度表格"逐元素并发调 VLM，
产出与 file_understand_service._apply_patches 完全兼容的补丁 dict：

    {"tables": [{"anchor": "1", "markdown": "..."}],
     "images": [{"url": "...", "kind": "chart|figure", "caption": "...",
                 "table_markdown": "..."}]}

要点：
  - 单元素输入很小（几十 KB），从根上避免整份内联的请求体/输出 token/超时三堵墙；
  - 并发受本地 Semaphore + 剩余 deadline 双重约束；单元素失败隔离、不拖垮整体；
  - 图片近重复只识别一次，其余复用代表图 caption；疑似 Logo/装饰图按配置跳过；
  - 图片字节缺失时按 URL 现下载（复用 image_repair._download）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Dict, List, Optional, Tuple

from services.document_ast_service import DocumentAST, ImageElement, TableElement
from services.file_understand_image_repair import (
    _download,
    compose_caption_public,
    dedup_by_hash,
    image_importance_ok,
    proofread_table,
    recognize_image_full,
)
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/file_understand_element_vision.log")


async def _run_bounded(
    coros: List[Awaitable[Any]],
    *,
    deadline_at: float,
    request_id: str,
    stage: str,
) -> None:
    """并发执行一批协程，受墙钟 deadline 约束：到点立即取消未完成的任务。

    与 asyncio.gather 的区别：gather 会一直等到全部完成（deadline 只在协程"开始时"
    检查形同虚设）；这里用 asyncio.wait(timeout=剩余预算) 让"能跑完多少算多少"真正生效。
    每个协程内部自行把成功结果写入外部累加器（patch/stats），故本函数无需返回值。
    未完成的任务会被 cancel；由协程自身在识别失败/被取消时把元素标为 unresolved。
    """
    if not coros:
        return
    remaining = deadline_at - time.time()
    tasks = [asyncio.ensure_future(c) for c in coros]
    if remaining <= 0:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.warning("[%s] %s 阶段无剩余预算，跳过 %d 个元素", request_id, stage, len(tasks))
        return
    done, pending = await asyncio.wait(tasks, timeout=remaining)
    if pending:
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning(
            "[%s] %s 阶段墙钟到点(%.1fs)：完成 %d/%d，取消 %d",
            request_id, stage, remaining, len(done), len(tasks), len(pending),
        )


def _kind_of(obj: Dict[str, Any]) -> str:
    """把结构化识别结果映射为 _apply_patches 的 kind：chart 或 figure。"""
    t = (obj.get("img_type") or "").strip().lower()
    if t == "chart" and (obj.get("chart_table_markdown") or "").strip():
        return "chart"
    return "figure"


async def run_element_vision(
    ast: DocumentAST,
    *,
    request_id: str,
    deadline_at: float,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """对 AST 逐元素视觉，返回 (补丁 dict, 统计 dict)。

    补丁 dict 交给 _apply_patches 合并；统计 dict 供 meta 暴露可观测性。
    """
    stats: Dict[str, Any] = {
        "images_total": len(ast.images),
        "images_deduped": 0,
        "images_filtered": 0,
        "images_vision_called": 0,
        "tables_total": len(ast.tables),
        "table_vision_calls": 0,
        "unresolved_ids": [],
    }
    patch: Dict[str, Any] = {"tables": [], "images": []}

    sem = asyncio.Semaphore(max(1, int(_settings.FILE_UNDERSTAND_ELEMENT_CONCURRENCY)))

    # ---------------- 图片元素 ----------------
    await _process_images(ast.images, patch, stats, sem, request_id, deadline_at)

    # ---------------- 表格元素（仅低置信度且有裁剪小图） ----------------
    await _process_tables(ast.tables, patch, stats, sem, request_id, deadline_at)

    logger.info(
        "[%s] 元素级视觉完成 图片 total=%s dedup=%s filtered=%s called=%s；"
        "表格 total=%s called=%s；unresolved=%s",
        request_id, stats["images_total"], stats["images_deduped"],
        stats["images_filtered"], stats["images_vision_called"],
        stats["tables_total"], stats["table_vision_calls"], len(stats["unresolved_ids"]),
    )
    return patch, stats


async def _ensure_bytes(img: ImageElement) -> Optional[Tuple[bytes, str]]:
    """拿到图片字节：AST 已带则用，否则按 URL 下载。"""
    if img.data:
        return img.data, img.mime or "image/png"
    dl = await _download(img.url)
    return dl


async def _process_images(
    images: List[ImageElement],
    patch: Dict[str, Any],
    stats: Dict[str, Any],
    sem: asyncio.Semaphore,
    request_id: str,
    deadline_at: float,
) -> None:
    if not images:
        return

    # 先拿字节（并发、受墙钟 deadline 约束）。
    url_bytes: Dict[str, Tuple[bytes, str]] = {}

    async def _fetch(img: ImageElement) -> None:
        if time.time() >= deadline_at - 1.0:
            return
        try:
            async with sem:
                dl = await _ensure_bytes(img)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return
        if dl:
            url_bytes[img.url] = dl

    await _run_bounded(
        [_fetch(im) for im in images],
        deadline_at=deadline_at,
        request_id=request_id,
        stage="图片下载",
    )

    # 重要性过滤：疑似 Logo/装饰图直接跳过（不占视觉预算）。
    kept: List[ImageElement] = []
    for img in images:
        dl = url_bytes.get(img.url)
        if dl is None:
            stats["unresolved_ids"].append(img.url)
            continue
        if not image_importance_ok(dl[0]):
            stats["images_filtered"] += 1
            continue
        kept.append(img)

    # 近重复去重：仅代表图真正识别，别名复用其 caption/kind。
    alias_to_rep: Dict[str, str] = {}
    if _settings.FILE_UNDERSTAND_IMAGE_DEDUP_ENABLED:
        rep_urls, alias_to_rep = dedup_by_hash(
            [(im.url, url_bytes[im.url][0]) for im in kept],
            int(_settings.FILE_UNDERSTAND_IMAGE_DEDUP_HAMMING),
        )
        stats["images_deduped"] = len(alias_to_rep)
        rep_set = set(rep_urls)
        rep_images = [im for im in kept if im.url in rep_set]
    else:
        rep_images = kept

    # 上限保护：超出的标 unresolved。
    max_imgs = int(_settings.FILE_UNDERSTAND_ELEMENT_MAX_IMAGES)
    if len(rep_images) > max_imgs:
        for im in rep_images[max_imgs:]:
            stats["unresolved_ids"].append(im.url)
        rep_images = rep_images[:max_imgs]

    rep_result: Dict[str, Dict[str, Any]] = {}

    async def _recog(img: ImageElement) -> None:
        if time.time() >= deadline_at - 1.0:
            stats["unresolved_ids"].append(img.url)
            return
        try:
            async with sem:
                dl = url_bytes[img.url]
                obj = await recognize_image_full(dl[0], dl[1], request_id, chart_to_table=True)
        except asyncio.CancelledError:
            # 墙钟到点被取消：该图未完成，标 unresolved（保留原图）。
            stats["unresolved_ids"].append(img.url)
            return
        except Exception:  # noqa: BLE001
            stats["unresolved_ids"].append(img.url)
            return
        stats["images_vision_called"] += 1
        if obj:
            rep_result[img.url] = obj
        else:
            stats["unresolved_ids"].append(img.url)

    await _run_bounded(
        [_recog(im) for im in rep_images],
        deadline_at=deadline_at,
        request_id=request_id,
        stage="图片识别",
    )

    # 组装图片补丁：代表图 + 别名复用代表结果。
    def _emit(url: str, obj: Dict[str, Any]) -> None:
        caption = compose_caption_public(obj) or (obj.get("img_description") or "").strip()
        kind = _kind_of(obj)
        entry: Dict[str, Any] = {"url": url, "kind": kind, "caption": caption}
        if kind == "chart":
            entry["table_markdown"] = (obj.get("chart_table_markdown") or "").strip()
        patch["images"].append(entry)

    for url, obj in rep_result.items():
        _emit(url, obj)
    for alias_url, rep_url in alias_to_rep.items():
        obj = rep_result.get(rep_url)
        if obj:
            _emit(alias_url, obj)


async def _process_tables(
    tables: List[TableElement],
    patch: Dict[str, Any],
    stats: Dict[str, Any],
    sem: asyncio.Semaphore,
    request_id: str,
    deadline_at: float,
) -> None:
    if not tables or not _settings.FILE_UNDERSTAND_TABLE_VISION_ENABLED:
        return

    # 仅对"低置信度且有裁剪小图"的表做视觉校对；其余信任解析层（不产补丁）。
    targets = [t for t in tables if t.low_confidence and t.crop_png]

    async def _proof(tbl: TableElement) -> None:
        if time.time() >= deadline_at - 1.0:
            return
        try:
            async with sem:
                md = await proofread_table(tbl.crop_png, tbl.base_markdown, request_id)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return
        stats["table_vision_calls"] += 1
        if md and md.strip():
            patch["tables"].append({"anchor": tbl.anchor, "markdown": md.strip()})

    await _run_bounded(
        [_proof(t) for t in targets],
        deadline_at=deadline_at,
        request_id=request_id,
        stage="表格校对",
    )
