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

import os
import time
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


def _classify_exc(e: BaseException) -> str:
    """把异常归类成可检索的短标签，便于在日志里快速分辨故障性质。

    - net_disconnect : 连接被对端在收到响应前断开（典型为出口/代理被掐断，如 GFW、egress 不稳）
    - net_connect    : 连不上（DNS/拒绝/不可达）
    - net_timeout    : 建连/读/写/连接池超时
    - net_protocol   : 其它协议层错误
    - http_status    : 服务端返回了 4xx/5xx
    - other          : 兜底
    """
    if isinstance(e, httpx.HTTPStatusError):
        return "http_status"
    if isinstance(e, httpx.RemoteProtocolError):
        # "Server disconnected without sending a response" 属于此类
        return "net_disconnect"
    if isinstance(e, httpx.ConnectError):
        return "net_connect"
    if isinstance(e, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.TimeoutException)):
        return "net_timeout"
    if isinstance(e, httpx.TransportError):
        return "net_protocol"
    return "other"


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

    # 请求体大小：诊断 “大请求体 -> 连接被断” 假设的关键指标（不打印 base64 本身）。
    n_parts = sum(len(c.get("parts") or []) for c in contents)
    n_inline = sum(
        1 for c in contents for p in (c.get("parts") or []) if isinstance(p, dict) and "inlineData" in p
    )
    try:
        import json as _json
        body_kb = len(_json.dumps(body).encode("utf-8")) // 1024
    except Exception:  # noqa: BLE001
        body_kb = -1
    t_all = time.time()
    logger.info(
        f"[{request_id}] Vertex 调用开始 pid={os.getpid()} model={model} 区域候选={locations} "
        f"parts={n_parts}(含inline={n_inline}) body≈{body_kb}KB read_timeout={timeout_sec}s"
    )

    for attempt, loc in enumerate(locations):
        endpoint = _VERTEX_ENDPOINT_TEMPLATE.format(
            project=GOOGLE_PROJECT_ID, location=loc, model=model
        )
        t0 = time.time()
        try:
            logger.debug(
                f"[{request_id}] Vertex generateContent location={loc} "
                f"({attempt + 1}/{len(locations)}) model={model}"
            )
            resp = await client.post(
                endpoint, headers=headers, json=body, timeout=per_timeout
            )
            resp.raise_for_status()
            data = resp.json()
            dt = time.time() - t0
            n_cand = len(data.get("candidates") or [])
            fr = finish_reason(data)
            usage = data.get("usageMetadata") or {}
            logger.info(
                f"[{request_id}] Vertex 成功 location={loc} ({attempt + 1}/{len(locations)}) "
                f"耗时={dt:.2f}s 总耗时={time.time() - t_all:.2f}s candidates={n_cand} "
                f"finishReason={fr} tokens={usage.get('totalTokenCount')} resp≈{len(resp.content)//1024}KB"
            )
            return data
        except httpx.HTTPStatusError as e:
            last_exc = e
            dt = time.time() - t0
            code = e.response.status_code
            if code == 429 or code >= 500:
                logger.warning(
                    f"[{request_id}] {loc} 可重试HTTP {code} ({attempt + 1}/{len(locations)}) "
                    f"耗时={dt:.2f}s body={e.response.text[:300]}"
                )
                continue
            logger.error(
                f"[{request_id}] {loc} 不可重试HTTP {code} ({attempt + 1}/{len(locations)}) "
                f"耗时={dt:.2f}s body={e.response.text[:300]}"
            )
            raise
        except Exception as e:  # noqa: BLE001
            last_exc = e
            dt = time.time() - t0
            kind = _classify_exc(e)
            logger.warning(
                f"[{request_id}] {loc} {kind} ({attempt + 1}/{len(locations)}) "
                f"耗时={dt:.2f}s {type(e).__name__}: {e}"
            )
            continue

    total = time.time() - t_all
    if last_exc:
        logger.error(
            f"[{request_id}] Vertex 全部 {len(locations)} 个区域失败 总耗时={total:.2f}s "
            f"最后错误={_classify_exc(last_exc)} {type(last_exc).__name__}: {last_exc}"
        )
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
