# -*- coding: utf-8 -*-
"""内嵌图 VLM 语义打标服务（供 manifest 检索与图文匹配）。

对 ``DocumentManifest`` 里每张内嵌图（``ManifestImage``）用 Vertex Gemini 打标，
就地填充：
  - ``img_description``   : 一句客观中文语义描述（供检索/图文匹配）；
  - ``img_keywords``      : 关键词列表（供召回）；
  - ``img_type``          : chart|photo|diagram|screenshot|unknown；
  - ``chart_table_markdown``: 当判定为 chart 且开启转表时，图表数值转写的 Markdown 表。

设计：
  - 复用 gemini_vertex_client（多模态）+ vertex_global_limiter（跨 worker 限流）+
    file_understand 的同一凭证/区域体系，不引入新依赖。
  - 结构化输出（responseSchema）保证可解析；失败仅记 warning 不阻断 manifest。
  - 可选把"所在页整页 PNG"作为上下文一起喂，让模型更懂图在讲什么。
  - 页内多图并发打标，受 DOC_IMPORT_VLM_CONCURRENCY 与全局限流共同约束。
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import List, Optional

import httpx

from schemas.document_manifest import DocImageType, DocumentManifest, DocumentParseOptions, ManifestImage
from services import gemini_vertex_client as gvc
from services.vertex_global_limiter import VertexLimiterUnavailable, vertex_global_limit
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/document_vlm_caption.log")

_SYSTEM_PROMPT = (
    "你是严谨的图片理解助手。你会收到从文档中抽取的一张图片（可能另附其所在页的整页截图作上下文）。"
    "请只依据你看到的内容，客观理解这张图片，严格按给定 JSON Schema 输出：\n"
    "1. img_type：判定为 chart(数据型图表:柱/折线/饼/雷达等)、diagram(流程图/结构示意图)、"
    "screenshot(软件/网页截图)、photo(照片/实物/人物)之一；无法判断填 unknown；\n"
    "2. img_description：一句客观中文描述这张图的主题与关键信息，不臆造、不加评价；\n"
    "3. img_keywords：3-8 个中文关键词，覆盖图中主体、场景、用途，便于检索；\n"
    "4. chart_table_markdown：仅当 img_type=chart 时，把图中可读的数值尽量转写为一个规范 Markdown 表格"
    "（保留系列名、量纲、单位）；非 chart 或读不出数值时留空字符串；\n"
    "5. 不要输出正文、解释或任何额外文本。"
)

_CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "img_type": {
            "type": "string",
            "enum": ["chart", "diagram", "screenshot", "photo", "unknown"],
        },
        "img_description": {"type": "string"},
        "img_keywords": {"type": "array", "items": {"type": "string"}},
        "chart_table_markdown": {"type": "string"},
    },
    "required": ["img_type", "img_description", "img_keywords"],
}

_VALID_TYPES = {
    DocImageType.CHART, DocImageType.DIAGRAM, DocImageType.SCREENSHOT,
    DocImageType.PHOTO, DocImageType.UNKNOWN,
}


def _generation_config(model: str, *, chart_to_table: bool) -> dict:
    cfg: dict = {
        "temperature": _settings.DOC_IMPORT_VLM_TEMPERATURE,
        "maxOutputTokens": _settings.DOC_IMPORT_VLM_MAX_OUTPUT_TOKENS,
        "responseMimeType": "application/json",
        "responseSchema": _CAPTION_SCHEMA,
    }
    budget = _settings.DOC_IMPORT_VLM_THINKING_BUDGET
    if budget is not None and budget >= 0 and ("gemini-2.5" in model or "gemini-3" in model):
        cfg["thinkingConfig"] = {"thinkingBudget": budget}
    return cfg


async def _download_bytes(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=False) as client:
            resp = await client.get(url, headers={"User-Agent": _settings.FETCH_USER_AGENT})
            resp.raise_for_status()
            return resp.content
    except Exception as e:  # noqa: BLE001
        logger.warning("download image for caption failed url=%s: %s", url, e)
        return None


def _inline_part(data: bytes, mime: str) -> dict:
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}}


async def _caption_one(
    image: ManifestImage,
    page_png_bytes: Optional[bytes],
    model: str,
    chart_to_table: bool,
    request_id: str,
) -> bool:
    """对单张图打标，就地写回 image。返回是否成功。"""
    if not image.img_url:
        return False
    img_bytes = await _download_bytes(image.img_url)
    if not img_bytes:
        return False

    parts: List[dict] = [_inline_part(img_bytes, image.mime_type or "image/png")]
    user_text = "这是从文档抽取的目标图片，请理解并打标。"
    if page_png_bytes and _settings.DOC_IMPORT_VLM_WITH_PAGE_CONTEXT:
        parts.append(_inline_part(page_png_bytes, "image/png"))
        user_text = (
            "第一张是从文档抽取的目标图片，第二张是它所在页的整页截图（仅作上下文）。"
            "请只针对第一张目标图片打标。"
        )
    parts.append({"text": user_text})

    data = await gvc.generate_content(
        model=model,
        contents=[{"role": "user", "parts": parts}],
        generation_config=_generation_config(model, chart_to_table=chart_to_table),
        system_instruction=_SYSTEM_PROMPT,
        location=_settings.DOC_IMPORT_VLM_LOCATION,
        timeout_sec=_settings.DOC_IMPORT_VLM_TIMEOUT_SEC,
        max_locations=_settings.DOC_IMPORT_VLM_MAX_REGIONS,
        request_id=request_id,
    )
    raw = gvc.extract_text(data).strip()
    if not raw:
        return False
    try:
        obj = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("caption json parse failed url=%s: %s", image.img_url, e)
        return False

    img_type = (obj.get("img_type") or DocImageType.UNKNOWN).strip()
    if img_type not in _VALID_TYPES:
        img_type = DocImageType.UNKNOWN
    image.img_type = img_type
    image.img_description = (obj.get("img_description") or "").strip() or None
    kws = obj.get("img_keywords") or []
    image.img_keywords = [str(k).strip() for k in kws if str(k).strip()][:8]
    if chart_to_table and img_type == DocImageType.CHART:
        tbl = (obj.get("chart_table_markdown") or "").strip()
        image.chart_table_markdown = tbl or None
    return True


async def caption_manifest_images(
    manifest: DocumentManifest, options: DocumentParseOptions
) -> None:
    """遍历 manifest 所有页的内嵌图并发打标，就地写回。失败仅记 warning。"""
    if not options.vlm_caption:
        return
    model = _settings.DOC_IMPORT_VLM_MODEL
    chart_to_table = options.vlm_chart_to_table
    request_id = uuid.uuid4().hex[:8]

    # 预取每页整页 PNG（作上下文），避免每张图重复下载。
    page_png_cache: dict = {}
    if _settings.DOC_IMPORT_VLM_WITH_PAGE_CONTEXT:
        for page in manifest.pages:
            if page.images and page.page_png_url:
                page_png_cache[page.index] = await _download_bytes(page.page_png_url)

    sem = asyncio.Semaphore(max(1, _settings.DOC_IMPORT_VLM_CONCURRENCY))
    ok = 0
    fail = 0

    async def _guarded(image: ManifestImage, page_index: int) -> None:
        nonlocal ok, fail
        async with sem:
            try:
                async with vertex_global_limit(request_id):
                    success = await _caption_one(
                        image,
                        page_png_cache.get(page_index),
                        model,
                        chart_to_table,
                        request_id,
                    )
            except VertexLimiterUnavailable as e:
                manifest.warnings.append(f"VLM 打标限流不可用，部分图未打标：{e}")
                fail += 1
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("caption image failed url=%s: %s", image.img_url, e)
                fail += 1
                return
        if success:
            ok += 1
        else:
            fail += 1

    tasks = [
        _guarded(image, page.index)
        for page in manifest.pages
        for image in page.images
    ]
    if not tasks:
        return
    await asyncio.gather(*tasks, return_exceptions=True)

    manifest.meta["vlm_captioned"] = ok
    manifest.meta["vlm_caption_failed"] = fail
    if fail:
        manifest.warnings.append(f"内嵌图 VLM 打标：成功 {ok}，失败/跳过 {fail}。")
    logger.info(
        "caption done doc_id=%s ok=%s fail=%s model=%s",
        manifest.doc_id, ok, fail, model,
    )
