# -*- coding: utf-8 -*-
"""单元素视觉理解的底层操作库（供元素级编排器复用）。

历史上本模块做"文档级理解后的逐图补识别"（repair_missing_captions）。重构为元素级
视觉理解后，逐元素识别成为主力，补识别后置步骤不再需要；本模块收敛为一组
provider 无关的"单元素"操作：

  - recognize_image_full：单图视觉（Gemini 优先、失败切 Azure），返回结构化 dict（含 chart 转表）；
  - proofread_table：单表截图视觉校对，返回校对后的 Markdown；
  - dhash / hamming / dedup_by_hash：图片近重复去重（Pillow 自算 dHash，免额外依赖）；
  - image_importance_ok：重要性过滤（跳过疑似 Logo/装饰图）；
  - _download：按 URL 取图字节（AST 未带字节时回退用）。

诚实原则：VLM 无法从信息不足的图里恢复不存在的信息；无法可靠识别就返回 None，
交由编排层标 unresolved，绝不编造描述。
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Tuple

import httpx

from services import azure_vlm_client as avc
from services.azure_vlm_client import AzureVLMConnError, AzureVLMError
from services.file_understand_provider import ProviderError, UnderstandGenerationRequest, VisualDocument
from services.file_understand_providers import VertexGeminiProvider
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover - 缺失时去重/过滤降级为不处理
    PILImage = None  # type: ignore[assignment]

logger = setup_module_logger(__name__, "logs/file/file_understand_image_repair.log")

_SINGLE_IMAGE_SYSTEM = (
    "你是严谨的图片理解助手。你会收到文档中的一张图片。请只依据你真实看到的内容，"
    "客观说明这张图，并严格输出 JSON（不要输出任何额外文本或代码块围栏）：\n"
    "1. img_description：一句客观中文描述画面主体与关键信息，不臆造；\n"
    "2. img_purpose：这张图在文档里最可能的用途（如：展示设备结构/说明流程/呈现数据/示例效果）；\n"
    "3. img_keywords：3-8 个中文关键词数组。\n"
    "描述必须落到具体信息（可读的文字/数字/型号/部件名/界面元素/数据），严禁使用"
    "‘可能是/大概/一张展示…的示意图/某种设备/背景为…’等空泛无信息量的措辞。\n"
    "若图片模糊、损坏或信息不足以判断，img_description 必须如实写"
    "“图片信息不足，无法可靠识别”，不要编造，也不要用泛化措辞充数。"
)


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


def compose_caption_public(obj: Optional[Dict[str, Any]]) -> Optional[str]:
    """对外暴露的 caption 组装（编排层复用）。"""
    return _compose_caption(obj)


async def _download(url: str) -> Optional[Tuple[bytes, str]]:
    """按 URL 取图字节（AST 未带字节时回退用）。"""
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=False) as client:
            resp = await client.get(url, headers={"User-Agent": _settings.FETCH_USER_AGENT})
            resp.raise_for_status()
            mime = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/png"
            return resp.content, mime
    except Exception as e:  # noqa: BLE001
        logger.warning("下载图片失败 url=%s: %s", url, e)
        return None


# ============== 图片近重复去重 (dHash) 与重要性过滤 ==============

def dhash(img_bytes: bytes, hash_size: int = 8) -> Optional[int]:
    """计算图片 dHash（差值哈希）；返回 64 位整数。Pillow 缺失/解码失败返回 None。

    dHash 对缩放/轻微压缩鲁棒，适合"同一张 Logo/装饰图重复出现"的近重复判定，
    比 pHash 更快且无需额外依赖（只用 Pillow）。
    """
    if PILImage is None:
        return None
    try:
        with PILImage.open(io.BytesIO(img_bytes)) as im:
            small = im.convert("L").resize((hash_size + 1, hash_size), PILImage.LANCZOS)
            # get_flattened_data(新)优先，回退 getdata(旧)，兼容不同 Pillow 版本。
            getter = getattr(small, "get_flattened_data", None)
            px = list(getter()) if callable(getter) else list(small.getdata())
    except Exception as e:  # noqa: BLE001
        logger.debug("dHash 计算失败：%s", e)
        return None
    bits = 0
    idx = 0
    for row in range(hash_size):
        base = row * (hash_size + 1)
        for col in range(hash_size):
            left = px[base + col]
            right = px[base + col + 1]
            bits |= (1 if left > right else 0) << idx
            idx += 1
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedup_by_hash(
    items: List[Tuple[str, bytes]], hamming_thresh: int
) -> Tuple[List[str], Dict[str, str]]:
    """按 dHash 近重复聚类。

    :param items: [(url, img_bytes), ...]，保序。
    :return: (representative_urls, alias_to_rep) —— representative_urls 为需要真正识别的
      代表图 URL（保序）；alias_to_rep 把被去重的 URL 映射到其代表 URL（用于复用 caption）。
    """
    reps: List[Tuple[str, int]] = []  # (url, hash)
    representative_urls: List[str] = []
    alias_to_rep: Dict[str, str] = {}
    for url, data in items:
        h = dhash(data) if data else None
        if h is None:
            # 无法计算哈希：当作独立图，照常识别。
            representative_urls.append(url)
            continue
        matched = None
        for rep_url, rep_h in reps:
            if hamming(h, rep_h) <= hamming_thresh:
                matched = rep_url
                break
        if matched is not None:
            alias_to_rep[url] = matched
        else:
            reps.append((url, h))
            representative_urls.append(url)
    return representative_urls, alias_to_rep


def image_importance_ok(img_bytes: Optional[bytes]) -> bool:
    """重要性过滤：疑似 Logo/装饰图（过小/字节过少）返回 False（跳过视觉）。

    未启用过滤或无法判定尺寸时一律放行（宁可多识别，不误杀）。
    """
    if not _settings.FILE_UNDERSTAND_IMAGE_FILTER_ENABLED:
        return True
    if not img_bytes:
        return True
    if len(img_bytes) < int(_settings.FILE_UNDERSTAND_IMAGE_FILTER_MIN_BYTES):
        return False
    if PILImage is None:
        return True
    try:
        with PILImage.open(io.BytesIO(img_bytes)) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        return True
    min_dim = int(_settings.FILE_UNDERSTAND_IMAGE_FILTER_MIN_DIM)
    return min(w, h) >= min_dim


# ============== 单元素视觉：图片识别 / 表格校对 ==============

async def recognize_image_full(
    img_bytes: bytes, mime: str, request_id: str, *, chart_to_table: bool = True
) -> Optional[Dict[str, Any]]:
    """通用单图视觉：Azure 多区域端点池优先、Vertex 最后兜底，返回结构化 dict（含 chart 转表）。

    返回完整结构化对象（img_type/description/keywords/chart_table_markdown），供编排层
    判定 chart/figure 并做图表转表。无法识别返回 None。

    编排顺序说明：Azure gpt-4o 多区域池国内可达、配额池大且质量一致，作为主力；
    Vertex 出网不稳（oauth 常卡），降为整池失败后的最后兜底。
    """
    # 1) Azure 多区域端点池（主力）
    try:
        obj = await avc.caption_image(
            img_bytes, mime, None,
            with_page_context=False, chart_to_table=chart_to_table, request_id=request_id,
        )
        if isinstance(obj, dict) and _compose_caption(obj):
            return obj
    except (AzureVLMConnError, AzureVLMError) as e:
        logger.warning("[%s] Azure 单图识别失败，切 Vertex 兜底: %s", request_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] Azure 单图识别异常，切 Vertex 兜底: %s", request_id, e)

    # 2) Vertex 单图（最后兜底）
    try:
        provider = VertexGeminiProvider(_settings.FILE_UNDERSTAND_MODEL)
        sys_prompt = _SINGLE_IMAGE_SYSTEM + (
            "\n4. 若为数据型图表(chart)，在 chart_table_markdown 里把数值转写为规范 Markdown 表格"
            "（保留系列名/量纲/单位）；非图表填空字符串。"
            if chart_to_table else ""
        )
        schema = {
            "type": "object",
            "properties": {
                "img_type": {"type": "string"},
                "img_description": {"type": "string"},
                "img_purpose": {"type": "string"},
                "img_keywords": {"type": "array", "items": {"type": "string"}},
                "chart_table_markdown": {"type": "string"},
            },
            "required": ["img_description"],
        }
        req = UnderstandGenerationRequest(
            document=VisualDocument(data=img_bytes, mime_type=mime),
            system_instruction=sys_prompt,
            user_text="这是文档中的一张图片，请按要求输出 JSON。",
            response_schema=schema,
            temperature=_settings.FILE_UNDERSTAND_TEMPERATURE,
            max_output_tokens=1536,
            request_id=request_id,
            deadline_sec=_settings.FILE_UNDERSTAND_ELEMENT_TIMEOUT_SEC,
        )
        result = await provider.generate(req)
        if isinstance(result.parsed_json, dict) and _compose_caption(result.parsed_json):
            return result.parsed_json
    except ProviderError as e:
        logger.warning("[%s] Vertex 单图兜底失败: %s", request_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] Vertex 单图兜底异常: %s", request_id, e)
    return None


_TABLE_PROOFREAD_SCHEMA = {
    "type": "object",
    "properties": {"table_markdown": {"type": "string"}},
    "required": ["table_markdown"],
}

_TABLE_PROOFREAD_SYSTEM = (
    "你是严谨的表格视觉校对助手。你会收到一张表格的截图，以及从文档抽取的该表初步 Markdown。"
    "请只依据截图忠实校对/补全该表，输出规范、完整、无遗漏的 Markdown 表格（多层表头合理合并）。"
    "严格输出 JSON：{\"table_markdown\":\"...\"}，不要输出任何额外文本或代码块围栏。"
    "不臆造数据；截图不清晰无法校对时，table_markdown 原样返回给定的初步 Markdown。"
)


async def proofread_table(
    crop_png: bytes, base_markdown: str, request_id: str
) -> Optional[str]:
    """单表视觉校对：Azure 多区域池优先、Vertex 最后兜底。返回校对后的 Markdown 或 None。"""
    user_text = "这是表格截图，下面是初步抽取的 Markdown，请校对后输出 JSON：\n\n" + base_markdown
    # 1) Azure（复用 caption_image 的 json_object，取 chart_table_markdown 承载校对结果）
    try:
        obj = await avc.caption_image(
            crop_png, "image/png", None,
            with_page_context=False, chart_to_table=True, request_id=request_id,
        )
        if isinstance(obj, dict):
            md = (obj.get("chart_table_markdown") or "").strip()
            if md:
                return md
    except (AzureVLMConnError, AzureVLMError) as e:
        logger.warning("[%s] Azure 表格校对失败，切 Vertex 兜底: %s", request_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] Azure 表格校对异常，切 Vertex 兜底: %s", request_id, e)

    # 2) Vertex（最后兜底）
    try:
        provider = VertexGeminiProvider(_settings.FILE_UNDERSTAND_MODEL)
        req = UnderstandGenerationRequest(
            document=VisualDocument(data=crop_png, mime_type="image/png"),
            system_instruction=_TABLE_PROOFREAD_SYSTEM,
            user_text=user_text,
            response_schema=_TABLE_PROOFREAD_SCHEMA,
            temperature=_settings.FILE_UNDERSTAND_TEMPERATURE,
            max_output_tokens=4096,
            request_id=request_id,
            deadline_sec=_settings.FILE_UNDERSTAND_ELEMENT_TIMEOUT_SEC,
        )
        result = await provider.generate(req)
        if isinstance(result.parsed_json, dict):
            md = (result.parsed_json.get("table_markdown") or "").strip()
            if md:
                return md
    except ProviderError as e:
        logger.warning("[%s] Vertex 表格校对兜底失败: %s", request_id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] Vertex 表格校对兜底异常: %s", request_id, e)
    return None
