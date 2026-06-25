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
import difflib
import os
import re
import shutil
import subprocess
import tempfile
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

_SYSTEM_PROMPT = (
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


def _generation_config(model: str) -> dict:
    cfg: dict = {
        "temperature": _settings.FILE_UNDERSTAND_TEMPERATURE,
        "maxOutputTokens": _settings.FILE_UNDERSTAND_MAX_OUTPUT_TOKENS,
    }
    res = _settings.FILE_UNDERSTAND_MEDIA_RESOLUTION
    # media_resolution 仅 Gemini 3 系支持；其它模型设置会报错，故仅 3 系附加。
    if res and "gemini-3" in model:
        cfg["mediaResolution"] = f"MEDIA_RESOLUTION_{res.strip().upper()}"
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
    # 1) 基础解析（强制开启内嵌图上传，确保 markdown 带真实图 URL）。
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

    understanding_applied = False
    model_used: Optional[str] = None

    if not options.enable_vision:
        enriched = base_md
        warnings.append("本次请求未启用视觉理解，返回基础解析结果。")
    else:
        vision_part, skip_reason = await _prepare_vision_part(payload, content_kind)
        if vision_part is None:
            enriched = base_md
            if skip_reason:
                warnings.append(skip_reason)
        else:
            model_used = _resolve_model(options)
            user_text = (
                "以下是从该文档已抽取的 Markdown（含真实图片 URL 与初步内容），"
                "请结合随附的原始文件进行多模态增强后，输出最终 Markdown：\n\n" + base_md
            )
            contents = [
                {
                    "role": "user",
                    "parts": [vision_part, {"text": user_text}],
                }
            ]
            try:
                data = await gvc.generate_content(
                    model=model_used,
                    contents=contents,
                    generation_config=_generation_config(model_used),
                    system_instruction=_SYSTEM_PROMPT,
                    location=_settings.FILE_UNDERSTAND_LOCATION,
                    timeout_sec=_settings.FILE_UNDERSTAND_TIMEOUT_SEC,
                    request_id=request_id,
                )
                enriched = gvc.extract_text(data).strip()
                if not enriched:
                    enriched = base_md
                    warnings.append("Gemini 未返回有效内容，降级为基础解析结果。")
                else:
                    understanding_applied = True
                    finish = gvc.finish_reason(data)
                    if finish and finish not in ("STOP", "MAX_TOKENS"):
                        warnings.append(f"Gemini finishReason={finish}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{request_id}] Gemini 理解失败，降级为基础解析: {e}")
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
