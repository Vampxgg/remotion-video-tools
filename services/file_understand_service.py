# -*- coding: utf-8 -*-
"""多模态知识理解的业务编排层。

在 /file/parse（抽取：内嵌图->公网URL、表格、文本）的基础上，再用 Vertex Gemini
对原始文档做"视觉级"理解：

  1) 先复用 file_parse_service 得到含真实图片 URL 与初步表格的基础 Markdown；
  2) 把"原始文件(PDF/图片)"作为视觉输入、基础 Markdown 作为文本上下文，一起喂 Gemini；
     - docx/doc/pptx/ppt 先用 LibreOffice 转 PDF 再走视觉；
     - 数据型图表由 Gemini 转写为 Markdown 数据表（矢量图也能保住信息）；
     - 源表格由 Gemini 视觉忠实转写；
  3) 后处理强制保留基础 Markdown 中的真实图片 URL，避免 Gemini 丢弃源图。

对外响应复用 schemas.file_parse.FileParseResult，content.markdown 即增强后的 Markdown，
因此 /file/understand 与 /file/parse 契约完全一致，调用方仅需切换 URL。
失败时优雅降级为基础解析结果（不让单文件理解失败拖垮整批/整条工作流）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from schemas.file_parse import (
    FileParseContent,
    FileParseFileInfo,
    FileParseMode,
    FileParseParserInfo,
    FileParseResult,
)
from services import gemini_vertex_client as gvc
from services.file_parse_service import (
    FileParseOptions,
    FilePayload,
    ParseInputError,
    parse_file_payload,
)
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/file_understand.log")

# 视觉理解(Vertex)并发闸门：基础解析不限，仅给昂贵且依赖出口带宽的 Gemini 调用限流，
# 避免高并发把出口打满导致连接被中途掐断(~125s)再换区慢重试(~350s)。懒加载绑定当前事件循环。
_vision_sem: Optional["asyncio.Semaphore"] = None


def _get_vision_sem() -> Optional["asyncio.Semaphore"]:
    global _vision_sem
    limit = getattr(_settings, "FILE_UNDERSTAND_MAX_CONCURRENCY", 0) or 0
    if limit <= 0:
        return None
    if _vision_sem is None:
        _vision_sem = asyncio.Semaphore(int(limit))
    return _vision_sem

_OFFICE_EXTS = {".docx", ".doc", ".pptx", ".ppt"}
_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_IMG_MD_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_IMG_FULL_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")

# 整篇重写模式（FILE_UNDERSTAND_PATCH_MODE=False 时使用）：让 Gemini 输出完整增强 Markdown。
_SYSTEM_PROMPT_FULL = (
    "你是严谨的文档多模态理解助手。你会收到一份文档的原始文件（PDF 或图片，用于视觉理解）"
    "以及一份已抽取的 Markdown（其中包含真实的图片 URL 和初步表格）。请产出一份"
    "“多模态增强 Markdown”，严格遵守：\n"
    "1. 完全忠实于原文档，不臆造、不杜撰任何数据、数字或结论；\n"
    "2. 用视觉逐一理解文档中的图片、图表、流程图、示意图：\n"
    "   - 对数据型图表（柱状/折线/饼图/雷达等），把其中的数值尽量转写为紧跟其后的 Markdown 表格，"
    "保留原始量纲、单位与系列名；\n"
    "   - 对非数据图（照片/示意图/Logo 等），用一句客观描述说明其内容；\n"
    "3. 图片 URL 规则（极重要）：只能使用输入 Markdown 中已存在的真实图片 URL，"
    "URL 字符必须逐字原样复制、严禁改动或臆造；只可把方括号内描述替换为你看懂后的准确说明，"
    "并放在其在文中应处的位置。对于你在原始文件里看到、但输入 Markdown 中没有对应 URL 的图，"
    "绝对不要编造任何链接（如 img.example.com 等），只用一句文字客观描述其内容即可；\n"
    "4. 用视觉校对并忠实转写文档中的所有表格为规范 Markdown 表格；\n"
    "5. 保持文档原有的章节标题层级与正文顺序；\n"
    "6. 仅输出最终 Markdown 正文，不要任何前言、解释或额外代码围栏包裹整体。"
)

# 仅补丁模式（默认）：不重写正文，只产结构化补丁，本地合并。主要提速来源。
_SYSTEM_PROMPT_PATCH = (
    "你是严谨的文档多模态理解助手。你会收到一份原始文件（PDF/图片）用于视觉理解，"
    "以及一份已抽取的 Markdown（正文、标题、表格均已可靠提取）。\n"
    "你的任务【不是】重写全文，而是【只】产出结构化补丁 JSON，严格遵守：\n"
    "1. 完全忠实原文，不臆造任何数据、数字或结论；\n"
    "2. tables：逐一视觉校对每个带 `<!--TBL:n-->` 锚点的表格，输出规范、完整、无遗漏的 Markdown "
    "表格（多层表头合理合并）；anchor 字段填该表的数字编号（如 \"1\"）；只需返回需要的表格，"
    "未变化也应原样返回以保证完整；\n"
    "3. images：对每个给定 URL 的图片，判定 kind 为 chart（数据型图表）或 figure（普通图片/照片/Logo/示意图）；"
    "chart 必须在 table_markdown 给出图表数值转写的 Markdown 表（保留量纲/单位/系列名），"
    "figure 只需用一句客观中文描述作 caption；\n"
    "4. 图片 URL 必须逐字使用输入给定的真实 URL，严禁改写或臆造；\n"
    "5. 严格按给定 JSON Schema 输出，不要输出正文、解释或任何额外文本。"
)

# Vertex responseSchema（OpenAPI 子集）：强制 Gemini 返回可解析的补丁 JSON。
_PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string"},
                    "markdown": {"type": "string"},
                },
                "required": ["anchor", "markdown"],
            },
        },
        "images": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "kind": {"type": "string", "enum": ["chart", "figure"]},
                    "caption": {"type": "string"},
                    "table_markdown": {"type": "string"},
                },
                "required": ["url", "kind", "caption"],
            },
        },
    },
    "required": ["tables", "images"],
}


@dataclass(frozen=True)
class UnderstandOptions:
    max_chars: Optional[int] = None
    enable_ocr: Optional[bool] = None
    enable_embedded_image_upload: Optional[bool] = None
    model: Optional[str] = None
    # 关闭视觉理解时退化为纯解析（用于排障/省成本）。
    enable_vision: bool = True


# --------------------------- LibreOffice: office -> pdf ---------------------------

def _find_soffice() -> Optional[str]:
    configured = _settings.CONVERTER_SOFFICE_PATH
    if configured and Path(configured).is_file():
        return configured
    for name in ("soffice", "libreoffice"):
        resolved = shutil.which(name)
        if resolved:
            return resolved
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "LibreOffice" / "program" / "soffice.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "LibreOffice" / "program" / "soffice.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _office_bytes_to_pdf(content: bytes, ext: str) -> bytes:
    """用 LibreOffice 把 office 文档字节转成 PDF 字节（同步，需在线程池调用）。"""
    soffice = _find_soffice()
    if not soffice:
        raise RuntimeError(
            "未找到 LibreOffice/soffice，无法将 office 文档转 PDF 做视觉理解；"
            "请安装 LibreOffice 或配置 CONVERTER_SOFFICE_PATH。"
        )
    timeout = _settings.CONVERTER_DOC_CONVERT_TIMEOUT_SEC
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / f"input{ext}"
        input_path.write_bytes(content)
        profile_dir = tmp_path / "lo_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            soffice,
            "--headless",
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(tmp_path),
            str(input_path),
        ]
        result = subprocess.run(
            command,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output_path = tmp_path / "input.pdf"
        if result.returncode != 0 or not output_path.is_file():
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"LibreOffice 转 PDF 失败: {detail or '未生成 pdf 文件'}")
        return output_path.read_bytes()


# --------------------------- Gemini 视觉理解 ---------------------------

def _resolve_model(options: UnderstandOptions) -> str:
    if options.model and options.model.strip():
        return options.model.strip()
    return _settings.FILE_UNDERSTAND_MODEL


def _generation_config(model: str, *, patch: bool) -> dict:
    cfg: dict = {
        "temperature": _settings.FILE_UNDERSTAND_TEMPERATURE,
        "maxOutputTokens": _settings.FILE_UNDERSTAND_MAX_OUTPUT_TOKENS,
    }
    res = _settings.FILE_UNDERSTAND_MEDIA_RESOLUTION
    # media_resolution 仅 Gemini 3 系支持；其它模型设置会报错，故仅 3 系附加。
    if res and "gemini-3" in model:
        cfg["mediaResolution"] = f"MEDIA_RESOLUTION_{res.strip().upper()}"
    # 思考预算仅 gemini-2.5/3 支持；0=关闭扩展思考以提速（转写/校对无需思考）。
    budget = _settings.FILE_UNDERSTAND_THINKING_BUDGET
    if budget is not None and budget >= 0 and ("gemini-2.5" in model or "gemini-3" in model):
        cfg["thinkingConfig"] = {"thinkingBudget": budget}
    # 补丁模式强制 JSON 结构化输出，保证可解析。
    if patch:
        cfg["responseMimeType"] = "application/json"
        cfg["responseSchema"] = _PATCH_SCHEMA
    return cfg


async def _prepare_vision_part(
    payload: FilePayload, content_kind: str
) -> Tuple[Optional[dict], Optional[str]]:
    """返回 (inlineData part, 跳过原因)。无视觉输入时 part 为 None。"""
    ext = payload.extension
    max_pdf_bytes = _settings.FILE_UNDERSTAND_MAX_PDF_MB * 1024 * 1024

    if content_kind == "pdf":
        if len(payload.content) > max_pdf_bytes:
            return None, f"PDF 超过 {_settings.FILE_UNDERSTAND_MAX_PDF_MB}MB，跳过视觉理解。"
        pdf_bytes = payload.content
    elif content_kind == "office" and ext in _OFFICE_EXTS:
        try:
            pdf_bytes = await asyncio.to_thread(_office_bytes_to_pdf, payload.content, ext)
        except Exception as e:  # noqa: BLE001
            return None, f"office 转 PDF 失败，跳过视觉理解：{e}"
        if len(pdf_bytes) > max_pdf_bytes:
            return None, f"转换后 PDF 超过 {_settings.FILE_UNDERSTAND_MAX_PDF_MB}MB，跳过视觉理解。"
    elif content_kind == "image":
        mime = _IMAGE_MIME.get(ext) or payload.media_type or "image/png"
        b64 = base64.b64encode(payload.content).decode("ascii")
        return {"inlineData": {"mimeType": mime, "data": b64}}, None
    else:
        return None, f"内容类型 {content_kind} 无需/不支持视觉理解。"

    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    return {"inlineData": {"mimeType": "application/pdf", "data": b64}}, None


def _reconcile_images(enriched: str, base_markdown: str) -> Tuple[str, dict]:
    """以 base 解析真实上传的图片 URL 为白名单，校正 Gemini 输出里的图片链接：

    - URL 在白名单 → 原样保留；
    - URL 不在白名单但与某白名单 URL 高度相似 → 判定为 Gemini 改写，纠正回真实 URL（保留位置）；
    - 其余（如 img.example.com / githubusercontent 等编造链接）→ 判定为幻觉，剥离图片语法、仅保留文字描述；
    - 白名单中仍未出现的真实源图 → 补回文末。

    可消除 LLM 编造/改写图片 URL 导致最终稿出现死链的问题。
    """
    stats = {"fake_dropped": 0, "corrupted_fixed": 0, "reappended": 0}
    whitelist = list(dict.fromkeys(_IMG_MD_RE.findall(base_markdown)))
    wl_set = set(whitelist)

    def _to_caption(alt: str) -> str:
        alt = (alt or "").strip()
        return f"（配图：{alt}）" if alt else ""

    if not whitelist:
        # 无任何真实源图 URL：Gemini 给出的图片链接必为编造，全部降级为文字描述。
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


_TBL_MARKER_RE = re.compile(r"^<!--TBL:(\d+)-->$")


def _anchor_tables(md: str) -> Tuple[str, int]:
    """在每个 Markdown 表格块前插入 `<!--TBL:n-->` 锚点，供 Gemini 定位与本地合并。

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
    """把 Gemini 的补丁 JSON 合并进带锚点的 base markdown（确定性本地合并）。

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


def _build_patch_user_text(anchored_md: str, n_tables: int, img_urls: List[str]) -> str:
    urls_block = "\n".join(f"- {u}" for u in img_urls) or "（无）"
    return (
        "下面是从该文档抽取的 Markdown，正文与标题已可靠提取，你无需重复输出正文。\n"
        f"其中有 {n_tables} 个表格，已用 `<!--TBL:n-->` 标注锚点；图片真实 URL 列表如下：\n"
        f"{urls_block}\n\n"
        "请结合随附的原始文件做视觉理解，只输出补丁 JSON（不要输出正文）：\n"
        "- tables：对每个锚点给出视觉校对后的规范 Markdown 表格，anchor 用对应数字；\n"
        "- images：对每个 URL 判定 chart/figure，chart 给 table_markdown，figure 给 caption；\n"
        "- URL 必须逐字使用上面列表中的原值，严禁改写或臆造。\n\n"
        "已抽取 Markdown：\n" + anchored_md
    )


async def _run_understand_full(
    vision_part: dict, base_md: str, model: str, request_id: str
) -> Tuple[str, bool, List[str]]:
    """整篇重写模式：Gemini 输出完整增强 Markdown。"""
    warns: List[str] = []
    user_text = (
        "以下是从该文档已抽取的 Markdown（含真实图片 URL 与初步内容），"
        "请结合随附的原始文件进行多模态增强后，输出最终 Markdown：\n\n" + base_md
    )
    contents = [{"role": "user", "parts": [vision_part, {"text": user_text}]}]
    data = await gvc.generate_content(
        model=model,
        contents=contents,
        generation_config=_generation_config(model, patch=False),
        system_instruction=_SYSTEM_PROMPT_FULL,
        location=_settings.FILE_UNDERSTAND_LOCATION,
        timeout_sec=_settings.FILE_UNDERSTAND_TIMEOUT_SEC,
        max_locations=_settings.FILE_UNDERSTAND_MAX_REGIONS,
        request_id=request_id,
    )
    enriched = gvc.extract_text(data).strip()
    if not enriched:
        return base_md, False, ["Gemini 未返回有效内容，降级为基础解析结果。"]
    finish = gvc.finish_reason(data)
    if finish and finish not in ("STOP", "MAX_TOKENS"):
        warns.append(f"Gemini finishReason={finish}")
    return enriched, True, warns


async def _run_understand_patch(
    vision_part: dict, base_md: str, model: str, request_id: str
) -> Tuple[str, bool, List[str]]:
    """仅补丁模式：Gemini 只产表格校对/图表转表/图片描述补丁，本地合并 base_md。"""
    warns: List[str] = []
    anchored_md, n_tables = _anchor_tables(base_md)
    img_urls = list(dict.fromkeys(_IMG_MD_RE.findall(base_md)))
    contents = [
        {
            "role": "user",
            "parts": [vision_part, {"text": _build_patch_user_text(anchored_md, n_tables, img_urls)}],
        }
    ]
    data = await gvc.generate_content(
        model=model,
        contents=contents,
        generation_config=_generation_config(model, patch=True),
        system_instruction=_SYSTEM_PROMPT_PATCH,
        location=_settings.FILE_UNDERSTAND_LOCATION,
        timeout_sec=_settings.FILE_UNDERSTAND_TIMEOUT_SEC,
        max_locations=_settings.FILE_UNDERSTAND_MAX_REGIONS,
        request_id=request_id,
    )
    raw = gvc.extract_text(data).strip()
    if not raw:
        return base_md, False, ["Gemini 未返回有效内容，降级为基础解析结果。"]
    try:
        patches = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return base_md, False, [f"补丁 JSON 解析失败，降级为基础解析：{e}"]
    enriched, stats = _apply_patches(anchored_md, patches)
    finish = gvc.finish_reason(data)
    if finish and finish not in ("STOP", "MAX_TOKENS"):
        warns.append(f"Gemini finishReason={finish}")
    warns.append(
        f"补丁合并：表格 {stats['tables']}、图表转表 {stats['charts']}、图片描述 {stats['figures']}。"
    )
    return enriched, True, warns


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


async def understand_file_payload(
    payload: FilePayload, options: UnderstandOptions
) -> FileParseResult:
    request_id = uuid.uuid4().hex[:8]
    t_start = time.time()
    logger.info(
        f"[{request_id}] 开始理解 file={payload.filename!r} ext={payload.extension} "
        f"size={payload.size // 1024}KB vision={'on' if options.enable_vision else 'off'} "
        f"patch_mode={_settings.FILE_UNDERSTAND_PATCH_MODE} model={_resolve_model(options)}"
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

    if not options.enable_vision:
        enriched = base_md
        warnings.append("本次请求未启用视觉理解，返回基础解析结果。")
        logger.info(f"[{request_id}] 跳过视觉理解（enable_vision=off）。")
    else:
        vision_part, skip_reason = await _prepare_vision_part(payload, content_kind)
        if vision_part is None:
            enriched = base_md
            if skip_reason:
                warnings.append(skip_reason)
            logger.info(f"[{request_id}] 跳过视觉理解：{skip_reason}")
        else:
            model_used = _resolve_model(options)
            runner = (
                _run_understand_patch
                if _settings.FILE_UNDERSTAND_PATCH_MODE
                else _run_understand_full
            )
            sem = _get_vision_sem()
            t_wait = time.time()
            ctx = sem if sem is not None else contextlib.nullcontext()
            async with ctx:
                queued = time.time() - t_wait
                if queued > 1.0:
                    logger.info(
                        f"[{request_id}] 视觉理解排队 {queued:.1f}s 后开始"
                        f"（并发上限={_settings.FILE_UNDERSTAND_MAX_CONCURRENCY}）"
                    )
                t_vis = time.time()
                try:
                    enriched, understanding_applied, w = await runner(
                        vision_part, base_md, model_used, request_id
                    )
                    warnings.extend(w)
                    logger.info(
                        f"[{request_id}] 视觉理解完成 耗时={time.time() - t_vis:.2f}s "
                        f"applied={understanding_applied} mode={'patch' if _settings.FILE_UNDERSTAND_PATCH_MODE else 'full'}"
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[{request_id}] Gemini 理解失败，降级为基础解析 耗时={time.time() - t_vis:.2f}s "
                        f"file={payload.filename!r}: {type(e).__name__}: {e}"
                    )
                    enriched = base_md
                    warnings.append(f"视觉理解失败，已降级为基础解析：{e}")

    # 3) 图片 URL 白名单校正：剔除编造链接、纠正改写链接、补回丢失源图。
    image_stats = {"fake_dropped": 0, "corrupted_fixed": 0, "reappended": 0}
    if understanding_applied:
        enriched, image_stats = _reconcile_images(enriched, base_md)
        if image_stats["fake_dropped"]:
            warnings.append(f"已剔除 Gemini 编造的 {image_stats['fake_dropped']} 个图片链接（保留描述）。")
        if image_stats["corrupted_fixed"]:
            warnings.append(f"已纠正 Gemini 改写的 {image_stats['corrupted_fixed']} 个图片 URL。")
        if image_stats["reappended"]:
            warnings.append(f"已补回 Gemini 丢弃的 {image_stats['reappended']} 张源图 URL。")

    max_chars = _effective_max_chars(options.max_chars)
    markdown, truncated = _truncate(enriched, max_chars)
    if truncated:
        warnings.append("API 返回内容已按 max_chars 截断。")

    meta.update(
        {
            "understanding_applied": understanding_applied,
            "understanding_model": model_used,
            "source_image_count": len(
                set(_IMG_MD_RE.findall(base_md))
            ),
            "final_image_count": len(set(_IMG_MD_RE.findall(markdown))),
            "images_hallucinated_dropped": image_stats["fake_dropped"],
            "images_url_corrected": image_stats["corrupted_fixed"],
            "images_reappended": image_stats["reappended"],
        }
    )

    logger.info(
        f"[{request_id}] 理解结束 file={payload.filename!r} 总耗时={time.time() - t_start:.2f}s "
        f"applied={understanding_applied} 最终chars={len(markdown)} "
        f"源图={meta.get('source_image_count')} 终图={meta.get('final_image_count')} "
        f"剔除编造={image_stats['fake_dropped']} 纠正={image_stats['corrupted_fixed']} 补回={image_stats['reappended']} "
        f"truncated={truncated} warns={len(warnings)}"
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
                f"{base.parser.parser_used}+gemini:{model_used}"
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
