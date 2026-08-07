# -*- coding: utf-8 -*-
"""缺失/低质量图片的逐图 VLM 补识别（only_missing 策略）。

文档级多模态理解完成后，仍可能有个别图片没被准确描述（描述缺失、泛化、过短、
重复、或 URL/描述错位）。本模块：

1. 以 base markdown 的真实图片 URL 为完整图片集合（覆盖率分母）；
2. 逐一核对增强 markdown 中每张图的描述质量；
3. 只对"缺失或明显低质量"的图片逐张调用 VLM 重新识别（不无条件重复所有图片）；
4. 逐图识别 Gemini 优先、失败切 Azure；结构化返回后回填 caption；
5. 仍不合格的保留原图并标记 unresolved，绝不编造描述。

诚实原则：VLM 无法从信息不足的图里恢复不存在的信息，因此不把模型自报置信度当作
准确性证明；无法可靠识别就如实标 unresolved，交由结果元数据暴露覆盖率。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from services import azure_vlm_client as avc
from services.azure_vlm_client import AzureVLMConnError, AzureVLMError
from services.file_understand_provider import ProviderError, UnderstandGenerationRequest, VisualDocument
from services.file_understand_providers import VertexGeminiProvider
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/file_understand_image_repair.log")

_IMG_MD_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_IMG_FULL_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")

# 泛化/无信息描述：命中即视为低质量，需要逐图补识别。
_GENERIC_CAPTIONS = {
    "", "图片", "配图", "示意图", "图", "image", "figure", "无法识别", "未知",
    "源文档图片", "文档图片", "表格图片",
}
_GENERIC_PREFIXES = ("源文档图片", "文档图片", "表格图片", "配图", "幻灯片")

_SINGLE_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "img_description": {"type": "string"},
        "img_purpose": {"type": "string"},
        "img_keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["img_description", "img_purpose"],
}

_SINGLE_IMAGE_SYSTEM = (
    "你是严谨的图片理解助手。你会收到文档中的一张图片。请只依据你真实看到的内容，"
    "客观说明这张图，并严格输出 JSON（不要输出任何额外文本或代码块围栏）：\n"
    "1. img_description：一句客观中文描述画面主体与关键信息，不臆造；\n"
    "2. img_purpose：这张图在文档里最可能的用途（如：展示设备结构/说明流程/呈现数据/示例效果）；\n"
    "3. img_keywords：3-8 个中文关键词数组。\n"
    "若图片模糊、损坏或信息不足以判断，img_description 必须如实写"
    "“图片信息不足，无法可靠识别”，不要编造。"
)


def _caption_of(url: str, md: str) -> Optional[str]:
    """取增强 markdown 中该 URL 的 alt/caption 文本；未出现返回 None。"""
    for alt, u in _IMG_FULL_RE.findall(md):
        if u == url:
            return (alt or "").strip()
    return None


def _is_low_quality(caption: Optional[str], seen_captions: Dict[str, int]) -> Tuple[bool, str]:
    """判定描述是否缺失/低质量。返回 (是否低质量, 原因)。"""
    if caption is None:
        return True, "图片未出现在增强结果中（描述缺失）"
    c = caption.strip()
    if c in _GENERIC_CAPTIONS:
        return True, "描述为空或泛化"
    if any(c.startswith(p) and len(c) <= len(p) + 3 for p in _GENERIC_PREFIXES):
        return True, "描述仅为占位/序号"
    if len(c) < _settings.FILE_UNDERSTAND_IMAGE_MIN_CAPTION_CHARS:
        return True, "描述过短"
    # 不同图片描述异常重复（同一句被套用到多张图）。
    if seen_captions.get(c, 0) >= 1:
        return True, "描述与其它图片重复"
    return False, ""


async def _download(url: str) -> Optional[Tuple[bytes, str]]:
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=False) as client:
            resp = await client.get(url, headers={"User-Agent": _settings.FETCH_USER_AGENT})
            resp.raise_for_status()
            mime = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/png"
            return resp.content, mime
    except Exception as e:  # noqa: BLE001
        logger.warning("下载待补识别图片失败 url=%s: %s", url, e)
        return None


async def _recognize_one(
    img_bytes: bytes, mime: str, request_id: str
) -> Optional[str]:
    """逐图识别：Gemini 优先，失败切 Azure。返回可用描述或 None（无法识别）。"""
    # 1) Gemini 单图
    try:
        provider = VertexGeminiProvider(_settings.FILE_UNDERSTAND_MODEL)
        req = UnderstandGenerationRequest(
            document=VisualDocument(data=img_bytes, mime_type=mime),
            system_instruction=_SINGLE_IMAGE_SYSTEM,
            user_text="这是文档中的一张图片，请按要求输出 JSON。",
            response_schema=_SINGLE_IMAGE_SCHEMA,
            temperature=_settings.FILE_UNDERSTAND_TEMPERATURE,
            max_output_tokens=1024,
            request_id=request_id,
            deadline_sec=_settings.FILE_UNDERSTAND_TIMEOUT_SEC,
        )
        result = await provider.generate(req)
        desc = _compose_caption(result.parsed_json)
        if desc:
            return desc
    except ProviderError as e:
        logger.warning("[%s] Gemini 逐图识别失败，切 Azure: %s", request_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] Gemini 逐图识别异常，切 Azure: %s", request_id, e)

    # 2) Azure 单图
    try:
        obj = await avc.caption_image(
            img_bytes, mime, None,
            with_page_context=False, chart_to_table=False, request_id=request_id,
        )
        desc = _compose_caption(obj)
        if desc:
            return desc
    except (AzureVLMConnError, AzureVLMError) as e:
        logger.warning("[%s] Azure 逐图识别失败: %s", request_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] Azure 逐图识别异常: %s", request_id, e)
    return None


def _compose_caption(obj: Optional[Dict[str, Any]]) -> Optional[str]:
    """把结构化识别结果拼成一句 caption；无信息/无法识别返回 None。"""
    if not isinstance(obj, dict):
        return None
    desc = (obj.get("img_description") or "").strip()
    if not desc or "无法可靠识别" in desc or "信息不足" in desc:
        return None
    purpose = (obj.get("img_purpose") or "").strip()
    if purpose and purpose not in desc:
        return f"{desc}（用途：{purpose}）"
    return desc


async def repair_missing_captions(
    enriched_md: str,
    base_md: str,
    *,
    request_id: str,
    deadline_at: float,
) -> Tuple[str, Dict[str, Any]]:
    """审计每张源图描述，对缺失/低质量者逐图补识别并回填。

    返回 (回填后的 markdown, 统计)。统计包含 described/repaired/unresolved_ids/coverage。
    """
    import time

    # 完整图片集合 = base markdown 的真实图片 URL（去重、保序）。
    all_urls = list(dict.fromkeys(_IMG_MD_RE.findall(base_md)))
    total = len(all_urls)
    stats: Dict[str, Any] = {
        "described": 0,
        "repaired": 0,
        "unresolved_ids": [],
        "coverage": 1.0 if total == 0 else 0.0,
    }
    if total == 0:
        return enriched_md, stats

    # 现有描述质量审计。
    seen_captions: Dict[str, int] = {}
    described = 0
    to_repair: List[str] = []
    for url in all_urls:
        cap = _caption_of(url, enriched_md)
        low, _reason = _is_low_quality(cap, seen_captions)
        if not low:
            described += 1
            if cap:
                seen_captions[cap] = seen_captions.get(cap, 0) + 1
        else:
            to_repair.append(url)

    max_repair = int(_settings.FILE_UNDERSTAND_IMAGE_REPAIR_MAX_IMAGES)
    if len(to_repair) > max_repair:
        logger.warning(
            "[%s] 待补识别图片 %s 张，超过上限 %s，仅处理前 %s 张。",
            request_id, len(to_repair), max_repair, max_repair,
        )
        # 超出上限的直接列入 unresolved（诚实暴露）。
        stats["unresolved_ids"].extend(to_repair[max_repair:])
        to_repair = to_repair[:max_repair]

    sem = asyncio.Semaphore(max(1, int(_settings.FILE_UNDERSTAND_IMAGE_REPAIR_CONCURRENCY)))
    new_captions: Dict[str, str] = {}
    unresolved: List[str] = []

    async def _worker(url: str) -> None:
        if time.time() >= deadline_at - 1.0:
            unresolved.append(url)
            return
        async with sem:
            dl = await _download(url)
            if not dl:
                unresolved.append(url)
                return
            desc = await _recognize_one(dl[0], dl[1], request_id)
            if desc:
                new_captions[url] = desc
            else:
                unresolved.append(url)

    await asyncio.gather(*[_worker(u) for u in to_repair], return_exceptions=True)

    # 回填：把新描述写进增强 markdown 对应图片的 alt。
    def _sub(m):
        alt, url = m.group(1), m.group(2)
        if url in new_captions:
            return f"![{new_captions[url]}]({url})"
        return m.group(0)

    repaired_md = _IMG_FULL_RE.sub(_sub, enriched_md)

    stats["described"] = described + len(new_captions)
    stats["repaired"] = len(new_captions)
    stats["unresolved_ids"] = stats["unresolved_ids"] + unresolved
    stats["coverage"] = round(stats["described"] / total, 4) if total else 1.0
    logger.info(
        "[%s] 逐图补识别完成 总图=%s 已描述=%s 补识别=%s 未解决=%s coverage=%.3f",
        request_id, total, stats["described"], stats["repaired"],
        len(stats["unresolved_ids"]), stats["coverage"],
    )
    return repaired_md, stats
