# -*- coding: utf-8 -*-
"""多模态知识理解的业务编排层（元素级 AST + 锚点定位）。

在 /file/parse（抽取：内嵌图->公网URL、表格、文本）的基础上，用元素级视觉理解增强：

  1) 先复用 file_parse_service 得到含真实图片 URL 与初步表格的基础 Markdown；
  2) 用 document_ast_service 把文档拆成"带锚点的元素"（图片=URL 锚点、表格=<!--TBL:n-->）；
     - PDF/图片直接用 PyMuPDF 抽字节/页坐标；docx/pptx 用 LibreOffice 转 PDF 仅用于表格裁剪；
  3) 元素级编排器（file_understand_element_vision）仅对"去重+过滤后的图片"和"低置信度表格"
     逐元素并发调 VLM，产出补丁；
  4) 用 _apply_patches 按锚点/URL 确定性合并，_reconcile_images 做图片 URL 白名单校正。

不再把整份 PDF 一次性 base64 内联给大模型：从根上规避请求体膨胀、输出 token 撑爆、单请求
超时被区域轮询放大等问题。对外响应仍复用 FileParseResult，契约与 /file/parse 一致。
失败时优雅降级为基础解析结果（不让单文件理解失败拖垮整批/整条工作流）。
"""

from __future__ import annotations

import asyncio
import difflib
import re
import time
import uuid
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from schemas.file_parse import (
    FileParseContent,
    FileParseFileInfo,
    FileParseMode,
    FileParseParserInfo,
    FileParseResult,
)
from services.document_ast_service import build_document_ast
from services.file_parse_service import (
    FileParseOptions,
    FilePayload,
    ParseInputError,
    parse_file_payload,
)
from services.file_understand_element_vision import run_element_vision
from services.vertex_global_limiter import (
    VertexLimiterTimeout,
    VertexLimiterUnavailable,
    vertex_global_limit,
)
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/file_understand.log")

_IMG_MD_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_IMG_FULL_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")


@dataclass(frozen=True)
class UnderstandOptions:
    max_chars: Optional[int] = None
    enable_ocr: Optional[bool] = None
    enable_embedded_image_upload: Optional[bool] = None
    model: Optional[str] = None
    # 关闭视觉理解时退化为纯解析（用于排障/省成本）。
    enable_vision: bool = True


def _resolve_model(options: UnderstandOptions) -> str:
    if options.model and options.model.strip():
        return options.model.strip()
    return _settings.FILE_UNDERSTAND_MODEL


# --------------------------- 图片 URL 白名单校正 ---------------------------

def _reconcile_images(enriched: str, base_markdown: str) -> Tuple[str, dict]:
    """以 base 解析真实上传的图片 URL 为白名单，校正输出里的图片链接：

    - URL 在白名单 → 原样保留；
    - URL 不在白名单但与某白名单 URL 高度相似 → 判定为改写，纠正回真实 URL（保留位置）；
    - 其余（如 img.example.com 等编造链接）→ 判定为幻觉，剥离图片语法、仅保留文字描述；
    - 白名单中仍未出现的真实源图 → 补回文末。
    """
    stats = {"fake_dropped": 0, "corrupted_fixed": 0, "reappended": 0}
    whitelist = list(dict.fromkeys(_IMG_MD_RE.findall(base_markdown)))
    wl_set = set(whitelist)

    def _to_caption(alt: str) -> str:
        alt = (alt or "").strip()
        return f"（配图：{alt}）" if alt else ""

    if not whitelist:
        def _strip_all(m):
            stats["fake_dropped"] += 1
            return _to_caption(m.group(1))

        return _IMG_FULL_RE.sub(_strip_all, enriched), stats

    def _repl(m):
        alt, url = m.group(1), m.group(2)
        if url in wl_set:
            return m.group(0)
        best = max(whitelist, key=lambda w: difflib.SequenceMatcher(None, url, w).ratio())
        if difflib.SequenceMatcher(None, url, best).ratio() >= 0.92:
            stats["corrupted_fixed"] += 1
            return f"![{alt}]({best})"
        stats["fake_dropped"] += 1
        return _to_caption(alt)

    enriched = _IMG_FULL_RE.sub(_repl, enriched)

    present = set(_IMG_MD_RE.findall(enriched))
    missing = [u for u in whitelist if u not in present]
    if missing:
        lines = ["", "", "## 源文档图片", ""]
        for i, url in enumerate(missing, 1):
            lines.append(f"![源文档图片{i}]({url})")
            lines.append("")
        enriched += "\n".join(lines)
        stats["reappended"] = len(missing)
    return enriched, stats


# --------------------------- 表格锚点 + 补丁合并 ---------------------------

_TBL_MARKER_RE = re.compile(r"^<!--TBL:(\d+)-->$")


def _anchor_tables(md: str) -> Tuple[str, int]:
    """在每个 Markdown 表格块前插入 `<!--TBL:n-->` 锚点，供元素级校对定位与本地合并。

    表格块=连续 ≥2 行（含表头与分隔行）以 `|` 开头的行。返回 (带锚点的 md, 表格数)。
    """
    lines = md.split("\n")
    out: List[str] = []
    i = 0
    idx = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            if j - i >= 2:
                idx += 1
                out.append(f"<!--TBL:{idx}-->")
                out.extend(lines[i:j])
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out), idx


def _norm_anchor(a: str) -> str:
    m = re.search(r"(\d+)", a or "")
    return m.group(1) if m else (a or "").strip()


def _apply_patches(anchored_md: str, patches: dict) -> Tuple[str, dict]:
    """把补丁 JSON 合并进带锚点的 base markdown（确定性本地合并）。

    - tables：按锚点数字替换对应表格块；缺失的锚点保留原表；
    - images：按真实 URL 命中后，figure 改写描述、chart 在图后插入转写表；未命中保留原样。
    """
    stats = {"tables": 0, "charts": 0, "figures": 0}
    tbl_map: dict = {}
    for t in (patches.get("tables") or []):
        if not isinstance(t, dict):
            continue
        a = _norm_anchor(str(t.get("anchor", "")))
        m = t.get("markdown")
        if a and isinstance(m, str) and m.strip():
            tbl_map[a] = m.strip()
    img_map: dict = {}
    for im in (patches.get("images") or []):
        if not isinstance(im, dict):
            continue
        u = (im.get("url") or "").strip()
        if u:
            img_map[u] = im

    # 1) 表格按锚点替换（按行处理）。
    lines = anchored_md.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        mm = _TBL_MARKER_RE.match(lines[i].strip())
        if mm:
            anchor_id = mm.group(1)
            i += 1
            start = i
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                i += 1
            original = lines[start:i]
            patch_md = tbl_map.get(anchor_id)
            if patch_md:
                out.append(patch_md)
                stats["tables"] += 1
            else:
                out.extend(original)
            continue
        out.append(lines[i])
        i += 1
    text = "\n".join(out)

    # 2) 图片按 URL 命中后改写/插表（正则）。
    def _img_sub(m):
        alt, url = m.group(1), m.group(2)
        im = img_map.get(url)
        if not im:
            return m.group(0)
        caption = (im.get("caption") or alt or "").strip()
        base_img = f"![{caption}]({url})"
        if (im.get("kind") or "figure").strip() == "chart":
            tm = (im.get("table_markdown") or "").strip()
            if tm:
                stats["charts"] += 1
                return base_img + "\n\n" + tm
        stats["figures"] += 1
        return base_img

    text = _IMG_FULL_RE.sub(_img_sub, text)
    return text, stats


def _truncate(text: str, max_chars: int) -> Tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _effective_max_chars(max_chars: Optional[int]) -> int:
    hard = _settings.FILE_UNDERSTAND_MAX_CONTENT_CHARS
    if max_chars is None:
        return hard
    if max_chars <= 0:
        raise ParseInputError(400, "invalid_max_chars", "max_chars 必须大于 0。")
    return min(max_chars, hard)


# --------------------------- 主入口 ---------------------------

async def understand_file_payload(
    payload: FilePayload,
    options: UnderstandOptions,
    *,
    budget_sec: Optional[float] = None,
    on_base_ready: Optional[Callable[[FileParseResult], None]] = None,
) -> FileParseResult:
    request_id = uuid.uuid4().hex[:8]
    t_start = time.time()
    logger.info(
        f"[{request_id}] 开始理解 file={payload.filename!r} ext={payload.extension} "
        f"size={payload.size // 1024}KB vision={'on' if options.enable_vision else 'off'} "
        f"model={_resolve_model(options)}"
    )
    # 1) 基础解析（强制开启内嵌图上传，确保 markdown 带真实图 URL）。
    t_base = time.time()
    base = await parse_file_payload(
        payload,
        FileParseOptions(
            mode=FileParseMode.MARKDOWN,
            max_chars=_settings.FILE_UNDERSTAND_MAX_CONTENT_CHARS,
            enable_ocr=options.enable_ocr,
            enable_embedded_image_upload=(
                True if options.enable_embedded_image_upload is None
                else options.enable_embedded_image_upload
            ),
        ),
    )
    # 基础解析即已含全文 + 全部真实图 URL：立即上报供异步任务写 partial_result（兜底）。
    if on_base_ready is not None:
        try:
            on_base_ready(base)
        except Exception:  # noqa: BLE001
            logger.warning("[%s] on_base_ready 回调异常（忽略，不影响主流程）", request_id)
    base_md = base.content.markdown or ""
    warnings = list(base.warnings)
    meta = dict(base.meta) if base.meta else {}
    content_kind = base.parser.content_kind
    base_imgs = len(set(_IMG_MD_RE.findall(base_md)))
    logger.info(
        f"[{request_id}] 基础解析完成 耗时={time.time() - t_base:.2f}s kind={content_kind} "
        f"chars={len(base_md)} 源图={base_imgs} parser={base.parser.parser_used}"
    )

    understanding_applied = False
    model_used: Optional[str] = None
    vision_status = "skipped"  # skipped | enhanced | degraded
    fallback_reason = ""
    element_stats: dict = {}
    image_stats = {"fake_dropped": 0, "corrupted_fixed": 0, "reappended": 0}

    enriched = base_md
    anchored_md, n_tables = _anchor_tables(base_md)

    if not options.enable_vision:
        warnings.append("本次请求未启用视觉理解，返回基础解析结果。")
        logger.info(f"[{request_id}] 跳过视觉理解（enable_vision=off）。")
    elif base_imgs == 0 and n_tables == 0:
        # 无图无表：视觉理解无从增强，直接返回基础解析。
        vision_status = "skipped"
        warnings.append("文档无图片/表格元素，无需视觉理解。")
        logger.info(f"[{request_id}] 跳过视觉理解（无图无表）。")
    else:
        model_used = _resolve_model(options)
        # 墙钟预算：视觉阶段自身上限 VISION_DEADLINE_SEC；若调用方给了整体 budget_sec
        # （异步 iteration 场景，需 ≤ Dify 沙箱 300s），则以"从本次理解开始算起不超过
        # budget_sec"为准取更紧的一档。deadline 到点由 run_element_vision 收集已完成补丁。
        deadline_at = time.time() + _settings.FILE_UNDERSTAND_VISION_DEADLINE_SEC
        if budget_sec and budget_sec > 0:
            deadline_at = min(deadline_at, t_start + float(budget_sec))
        t_wait = time.time()
        try:
            async with vertex_global_limit(request_id) as lease:
                queued = time.time() - t_wait
                logger.info(
                    f"[{request_id}] 视觉理解获得全局租约 pid={lease.pid} "
                    f"limit={lease.limit} active={lease.active_after_acquire} queued={queued:.2f}s"
                )
                remaining_budget = deadline_at - time.time()
                if remaining_budget <= 1.0:
                    # 排队已耗尽预算：直接降级为基础解析，不再进入视觉。
                    enriched = base_md
                    fallback_reason = "wallclock_budget: 排队耗尽预算，未进入视觉"
                    warnings.append("视觉理解预算在排队阶段耗尽，已降级为基础解析。")
                    logger.warning(
                        f"[{request_id}] 排队后剩余预算不足({remaining_budget:.1f}s)，降级基础解析。"
                    )
                else:
                    t_vis = time.time()
                    # 2) 构建元素级 AST。
                    ast = build_document_ast(
                        content=payload.content,
                        ext=payload.extension,
                        base_markdown=base_md,
                        anchored_markdown=anchored_md,
                        n_tables=n_tables,
                        embedded_images=None,  # 字节缺失时编排层按 URL 现下载
                        url_by_order=None,
                    )
                    for w in ast.warnings:
                        warnings.append(w)
                    # 3) 逐元素视觉，产出补丁。墙钟由 deadline_at 收敛；wait_for 作为
                    #    硬安全网防止任何单点卡死（多给 5s 让内部优雅收集已完成补丁）。
                    try:
                        patch, element_stats = await asyncio.wait_for(
                            run_element_vision(
                                ast, request_id=request_id, deadline_at=deadline_at
                            ),
                            timeout=max(1.0, deadline_at - time.time() + 5.0),
                        )
                    except asyncio.TimeoutError:
                        patch, element_stats = {"tables": [], "images": []}, {}
                        fallback_reason = "wallclock_budget: 视觉阶段墙钟硬超时"
                        warnings.append("视觉理解墙钟超时，已返回基础解析（含全部源图）。")
                        logger.warning(f"[{request_id}] 视觉阶段 wait_for 硬超时，降级基础解析。")
                    # 4) 确定性合并补丁。
                    merged, merge_stats = _apply_patches(anchored_md, patch)
                    understanding_applied = bool(
                        merge_stats["tables"] or merge_stats["charts"] or merge_stats["figures"]
                    )
                    enriched = merged if understanding_applied else base_md
                    logger.info(
                        f"[{request_id}] 视觉理解完成 耗时={time.time() - t_vis:.2f}s "
                        f"applied={understanding_applied} 表格={merge_stats['tables']} "
                        f"图表转表={merge_stats['charts']} 图片描述={merge_stats['figures']}"
                    )
        except VertexLimiterUnavailable as e:
            policy = (
                getattr(_settings, "FILE_UNDERSTAND_LIMITER_UNAVAILABLE_POLICY", "fallback_base")
                or "fallback_base"
            ).strip().lower()
            if policy == "fail":
                raise
            logger.warning(
                f"[{request_id}] Vertex 全局限流不可用，降级为基础解析 "
                f"queued={time.time() - t_wait:.2f}s file={payload.filename!r}: {e}"
            )
            enriched = base_md
            warnings.append(f"视觉理解限流不可用，已降级为基础解析：{e}")
            fallback_reason = f"limiter_unavailable: {e}"
        except VertexLimiterTimeout as e:
            logger.warning(
                f"[{request_id}] 视觉全局限流排队超时，降级为基础解析 "
                f"queued={time.time() - t_wait:.2f}s file={payload.filename!r}: {e}"
            )
            enriched = base_md
            warnings.append(f"视觉理解排队超时，已降级为基础解析：{e}")
            fallback_reason = f"limiter_timeout: {e}"

        if understanding_applied:
            vision_status = "enhanced"
        elif not fallback_reason:
            # 走完流程但零增强（元素全失败/无低置信度表/全被过滤）。
            vision_status = "degraded"
            fallback_reason = "no_effective_change"
        else:
            vision_status = "degraded"

    # 5) 图片 URL 白名单校正：剔除编造链接、纠正改写链接、补回丢失源图。
    if understanding_applied:
        enriched, image_stats = _reconcile_images(enriched, base_md)
        if image_stats["fake_dropped"]:
            warnings.append(f"已剔除模型编造的 {image_stats['fake_dropped']} 个图片链接（保留描述）。")
        if image_stats["corrupted_fixed"]:
            warnings.append(f"已纠正模型改写的 {image_stats['corrupted_fixed']} 个图片 URL。")
        if image_stats["reappended"]:
            warnings.append(f"已补回模型丢弃的 {image_stats['reappended']} 张源图 URL。")

    max_chars = _effective_max_chars(options.max_chars)
    markdown, truncated = _truncate(enriched, max_chars)
    if truncated:
        warnings.append("API 返回内容已按 max_chars 截断。")

    source_image_count = len(set(_IMG_MD_RE.findall(base_md)))
    unresolved_ids = element_stats.get("unresolved_ids", []) if element_stats else []
    if unresolved_ids:
        warnings.append(
            f"仍有 {len(unresolved_ids)} 个图片元素无法可靠识别，已保留原图并标记 unresolved。"
        )
    meta.update(
        {
            "understanding_applied": understanding_applied,
            "understanding_model": model_used if understanding_applied else None,
            "vision_status": vision_status,
            "understanding_mode": "element",
            "fallback_reason": fallback_reason or None,
            "source_image_count": source_image_count,
            "final_image_count": len(set(_IMG_MD_RE.findall(markdown))),
            "images_hallucinated_dropped": image_stats["fake_dropped"],
            "images_url_corrected": image_stats["corrupted_fixed"],
            "images_reappended": image_stats["reappended"],
            "images_total": element_stats.get("images_total", source_image_count),
            "images_deduped": element_stats.get("images_deduped", 0),
            "images_filtered": element_stats.get("images_filtered", 0),
            "images_vision_called": element_stats.get("images_vision_called", 0),
            "tables_total": element_stats.get("tables_total", n_tables),
            "table_vision_calls": element_stats.get("table_vision_calls", 0),
            "unresolved_image_ids": unresolved_ids,
        }
    )

    logger.info(
        f"[{request_id}] 理解结束 file={payload.filename!r} 总耗时={time.time() - t_start:.2f}s "
        f"applied={understanding_applied} status={vision_status} "
        f"最终chars={len(markdown)} 源图={source_image_count} 终图={meta.get('final_image_count')} "
        f"图片视觉={meta.get('images_vision_called')} 表格视觉={meta.get('table_vision_calls')} "
        f"去重={meta.get('images_deduped')} 过滤={meta.get('images_filtered')} "
        f"未解决={len(unresolved_ids)} truncated={truncated} warns={len(warnings)}"
    )

    return FileParseResult(
        status="ok",
        file=FileParseFileInfo(
            filename=payload.filename,
            extension=payload.extension,
            size=payload.size,
            media_type=payload.media_type,
        ),
        content=FileParseContent(
            markdown=markdown,
            text=None,
            char_count=len(markdown),
            truncated=truncated,
        ),
        parser=FileParseParserInfo(
            content_kind=content_kind,
            parser_used=(
                f"{base.parser.parser_used}+element_vision"
                if understanding_applied
                else base.parser.parser_used
            ),
            fallback_used=base.parser.fallback_used,
        ),
        meta=meta,
        assets=base.assets,
        warnings=warnings,
        error=None,
    )
