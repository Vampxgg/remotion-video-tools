# -*- coding: utf-8 -*-
"""可复用的 Vertex AI Gemini 文本/多模态客户端。

从 api/cre_image.py 的 Vertex 接入方式抽出共用逻辑，供"多模态知识理解"等
场景复用，避免每个调用点各写一套鉴权与区域轮询：

  - 鉴权：统一走 utils.gcp_credentials（显式 GCP_CREDENTIALS_FILE 服务账号，
    未配置则回退 google.auth.default()），与 cre_image / cre_video 共用同一凭证。
  - 调用：REST ``.../publishers/google/models/{model}:generateContent``。
  - 区域轮询：429 / 5xx 自动换区重试。

本模块只做"文本/多模态输入 -> 文本输出"，图片生成仍由 cre_image 负责。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from utils.gcp_credentials import get_access_token
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/gemini/vertex_client.log")

GOOGLE_PROJECT_ID = _settings.GCP_PROJECT_ID

# 与 cre_image 一致的区域候选；global 优先，其余作兜底。
GOOGLE_LOCATIONS: List[str] = [
    "global",
    "us-central1",
    "us-east4",
    "us-west1",
    "europe-west1",
    "europe-west4",
    "asia-northeast1",
    "asia-southeast1",
]

_VERTEX_ENDPOINT_TEMPLATE = (
    "https://aiplatform.googleapis.com/v1beta1/projects/{project}"
    "/locations/{location}/publishers/google/models/{model}:generateContent"
)

# 复用单个 AsyncClient，避免每次调用重建连接池。
_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient()
    return _http_client


async def aclose() -> None:
    """供 lifespan 关闭时调用（可选）。"""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


async def get_adc_token() -> str:
    """获取 Vertex access token；统一走 utils.gcp_credentials 共享凭证加载器。"""
    return await get_access_token()


def _locations_to_try(location: Optional[str]) -> List[str]:
    if location and location.strip():
        loc = location.strip()
        # 指定区域优先，仍保留少量兜底以应对单区域限流。
        rest = [x for x in GOOGLE_LOCATIONS if x != loc]
        return [loc] + rest[:3]
    return GOOGLE_LOCATIONS[:4]


async def generate_content(
    *,
    model: str,
    contents: List[Dict[str, Any]],
    generation_config: Optional[Dict[str, Any]] = None,
    system_instruction: Optional[str] = None,
    location: Optional[str] = None,
    timeout_sec: float = 300.0,
    max_locations: Optional[int] = None,
    request_id: str = "-",
) -> Dict[str, Any]:
    """调用 Vertex generateContent，返回原始 JSON。

    contents: 形如 ``[{"role": "user", "parts": [...]}]``。
    max_locations: 最多尝试的区域数（截断 _locations_to_try 结果）。用于限制总等待时间，
    避免区域轮询把偶发慢/限流放大成 区域数×timeout_sec 的超长阻塞。
    """
    body: Dict[str, Any] = {"contents": contents}
    if generation_config:
        body["generationConfig"] = generation_config
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    token = await get_adc_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    per_timeout = httpx.Timeout(connect=15.0, read=timeout_sec, write=120.0, pool=30.0)
    client = _get_http_client()

    last_exc: Optional[BaseException] = None
    locations = _locations_to_try(location)
    if max_locations and max_locations > 0:
        locations = locations[:max_locations]
    for attempt, loc in enumerate(locations):
        endpoint = _VERTEX_ENDPOINT_TEMPLATE.format(
            project=GOOGLE_PROJECT_ID, location=loc, model=model
        )
        try:
            logger.debug(
                f"[{request_id}] Vertex generateContent location={loc} "
                f"({attempt + 1}/{len(locations)}) model={model}"
            )
            resp = await client.post(
                endpoint, headers=headers, json=body, timeout=per_timeout
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            last_exc = e
            code = e.response.status_code
            if code == 429 or code >= 500:
                logger.warning(
                    f"[{request_id}] {loc} 失败 {code}: {e.response.text[:300]}"
                )
                continue
            logger.error(
                f"[{request_id}] {loc} 不可重试错误 {code}: {e.response.text[:300]}"
            )
            raise
        except Exception as e:  # noqa: BLE001
            last_exc = e
            logger.warning(f"[{request_id}] {loc} 异常: {e}")
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError("Vertex 调用失败且无异常信息")


def extract_text(data: Dict[str, Any]) -> str:
    """从 generateContent 响应里拼出全部文本（跳过 thought 片段）。"""
    out: List[str] = []
    for cand in data.get("candidates") or []:
        parts = (cand.get("content") or {}).get("parts") or []
        for part in parts:
            if part.get("thought"):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                out.append(text)
    return "".join(out)


def finish_reason(data: Dict[str, Any]) -> Optional[str]:
    cands = data.get("candidates") or []
    if not cands:
        return None
    return cands[0].get("finishReason")
