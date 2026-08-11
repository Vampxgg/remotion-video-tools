# -*- coding: utf-8 -*-
"""Azure OpenAI 兼容多模态打标客户端（FW-Kimi-K2.7-Code）。

为什么存在：文档内嵌图 VLM 打标原走谷歌 Vertex Gemini，但线上这台国内服务器到
``oauth2.googleapis.com`` 的出网通道不稳定，refresh token 常卡满 120s 导致整体雪崩。
改走 Azure OpenAI 兼容端点（国内可达、已在 describe_image / cursor_azure_proxy 实战），
用 ``api-key`` 头直连 ``/openai/deployments/{deployment}/chat/completions``，多模态用
标准 ``image_url`` data URL，结构化输出用 ``response_format={"type":"json_object"}``。

出网范式对齐 api/cre_image_azure.py：``httpx.AsyncClient(trust_env=False)`` 屏蔽进程
代理、默认直连；连接超时压到 10s，网络不通时快速失败而非硬扛。
"""

from __future__ import annotations

import base64
import itertools
import json
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from utils.azure_models import AzureEndpoint, AzureModelsConfigError, resolve_model, resolve_single
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/document_vlm_caption.log")


class AzureVLMConnError(Exception):
    """连接层不可达（连接超时/拒绝/网络错误）。供调用方做整批短路熔断。"""


class AzureVLMError(Exception):
    """其它打标失败（HTTP 4xx/5xx、响应解析失败等），不触发熔断。"""


_SYSTEM_PROMPT = (
    "你是严谨的图片理解助手。你会收到从文档中抽取的一张图片（可能另附其所在页的整页截图作上下文）。"
    "请只依据你看到的内容，客观理解这张图片，并严格输出一个 JSON 对象（不要输出任何额外文本、解释或代码块围栏）。"
    "JSON 字段要求：\n"
    "1. img_type：判定为 chart(数据型图表:柱/折线/饼/雷达等)、diagram(流程图/结构示意图)、"
    "screenshot(软件/网页截图)、photo(照片/实物/人物)之一；无法判断填 unknown；\n"
    "2. img_description：一句客观中文描述这张图的主题与关键信息，不臆造、不加评价；\n"
    "3. img_keywords：3-8 个中文关键词组成的数组，覆盖图中主体、场景、用途，便于检索；\n"
    "4. chart_table_markdown：仅当 img_type=chart 且要求转表时，把图中可读的数值尽量转写为一个规范 "
    "Markdown 表格（保留系列名、量纲、单位）；非 chart 或读不出数值或未要求时填空字符串。\n"
    "重要约束：描述必须落到具体信息（可读的文字/数字/型号/部件名/界面元素/数据），"
    "严禁使用‘可能是/大概/一张展示…的示意图/某种设备/背景为…’等空泛无信息量的措辞；"
    "若图片模糊、损坏或信息不足以给出具体描述，img_description 必须如实写"
    "“图片信息不足，无法可靠识别”，绝不编造或用泛化措辞充数。\n"
    '示例：{"img_type":"chart","img_description":"...","img_keywords":["a","b"],"chart_table_markdown":""}'
)


def bytes_to_data_url(data: bytes, mime: str) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime or 'image/png'};base64,{b64}"


@dataclass(frozen=True)
class _ResolvedTarget:
    endpoint: str
    api_key: str
    deployment: str
    api_version: str


def _resolve_target() -> _ResolvedTarget:
    """解析本次打标要用的 endpoint/key/deployment/api_version。

    优先级：settings 显式覆盖（DOC_IMPORT_AZURE_ENDPOINT/API_KEY/...）> yaml。
    只要 settings 里完整配了 endpoint+api_key 就走覆盖；否则从 azure-models.yaml
    按 DOC_IMPORT_AZURE_MODEL 解析主用端点。
    """
    override_ep = (_settings.DOC_IMPORT_AZURE_ENDPOINT or "").strip().rstrip("/")
    override_key = (_settings.DOC_IMPORT_AZURE_API_KEY or "").strip()
    model = (_settings.DOC_IMPORT_AZURE_MODEL or "").strip()

    if override_ep and override_key:
        return _ResolvedTarget(
            endpoint=override_ep,
            api_key=override_key,
            deployment=(_settings.DOC_IMPORT_AZURE_DEPLOYMENT or model or "").strip(),
            api_version=(_settings.DOC_IMPORT_AZURE_API_VERSION or "").strip(),
        )

    try:
        ep = resolve_single(model)
    except AzureModelsConfigError as e:
        raise AzureVLMError(f"Azure 模型解析失败({model}): {e}") from e
    return _ResolvedTarget(
        endpoint=ep.endpoint,
        api_key=ep.api_key,
        # settings 显式配了 deployment/api_version 则覆盖 yaml。
        deployment=(_settings.DOC_IMPORT_AZURE_DEPLOYMENT or ep.deployment).strip(),
        api_version=(_settings.DOC_IMPORT_AZURE_API_VERSION or ep.api_version).strip(),
    )


def current_deployment() -> str:
    """供调用方写 meta['vlm_model'] 用：当前实际使用的部署/模型名。"""
    try:
        return _resolve_target().deployment
    except AzureVLMError:
        return (_settings.DOC_IMPORT_AZURE_MODEL or "").strip()


# ============== 多区域端点池：跨请求轮询 + 单请求内换区 ==============

# 每个模型一个进程内轮询游标，让连续请求分散到不同 region（并用多个配额池）。
_rr_lock = threading.Lock()
_rr_cursors: Dict[str, "itertools.count[int]"] = {}


def _next_rotation(model: str) -> int:
    with _rr_lock:
        c = _rr_cursors.get(model)
        if c is None:
            c = itertools.count(0)
            _rr_cursors[model] = c
        return next(c)


def _pool_endpoints(model: str) -> List[AzureEndpoint]:
    """解析模型的全部 region 端点，按全局游标旋转顺序返回（实现跨请求负载分散）。

    - 显式覆盖（DOC_IMPORT_AZURE_ENDPOINT+API_KEY）优先：返回单端点，不参与轮询（应急）。
    - 关闭轮询（FILE_UNDERSTAND_VLM_REGION_ROTATION=False）：按 yaml 原序返回（首选区优先）。
    """
    override_ep = (_settings.DOC_IMPORT_AZURE_ENDPOINT or "").strip().rstrip("/")
    override_key = (_settings.DOC_IMPORT_AZURE_API_KEY or "").strip()
    if override_ep and override_key:
        return [
            AzureEndpoint(
                name="override",
                endpoint=override_ep,
                api_key=override_key,
                deployment=(_settings.DOC_IMPORT_AZURE_DEPLOYMENT or model or "").strip(),
                api_version=(_settings.DOC_IMPORT_AZURE_API_VERSION or "").strip(),
            )
        ]
    try:
        resolved = resolve_model(model)
    except AzureModelsConfigError as e:
        raise AzureVLMError(f"Azure 模型解析失败({model}): {e}") from e
    eps = list(resolved.endpoints)
    if not eps:
        raise AzureVLMError(f"模型 {model!r} 无可用 region 端点")
    if not bool(getattr(_settings, "FILE_UNDERSTAND_VLM_REGION_ROTATION", True)) or len(eps) == 1:
        return eps
    # 旋转：起始位置随全局游标推进，把连续请求分散到不同 region。
    start = _next_rotation(model) % len(eps)
    return eps[start:] + eps[:start]


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=_settings.DOC_IMPORT_AZURE_CONNECT_TIMEOUT,
        read=_settings.DOC_IMPORT_AZURE_READ_TIMEOUT,
        write=_settings.DOC_IMPORT_AZURE_WRITE_TIMEOUT,
        pool=_settings.DOC_IMPORT_AZURE_CONNECT_TIMEOUT,
    )


def _extract_content(resp_json: Dict[str, Any]) -> str:
    """从 chat/completions 响应取出文本 content，兼容 content 为分段数组的实现。"""
    choices = resp_json.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
        ).strip()
    return (content or "").strip()


def _strip_code_fence(text: str) -> str:
    """去掉可能的 ```json ... ``` 围栏，容错模型不守规矩的情况。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


class _SwitchRegion(Exception):
    """内部信号：当前 region 不可用，应切换到下一区域（不对外暴露）。"""

    def __init__(self, msg: str, *, switch_reason: str, conn: bool):
        super().__init__(msg)
        self.switch_reason = switch_reason
        self.conn = conn


async def _call_endpoint(
    ep: AzureEndpoint, body: Dict[str, Any], request_id: str
) -> Dict[str, Any]:
    """对单个 region 端点发起一次打标调用（含该端点内的轻重试）。

    :returns: 解析后的结构化 dict（成功）。
    :raises _SwitchRegion: 该端点 429/5xx/连接层错误，应切换到下一区域。
    :raises AzureVLMError: 4xx(非429) 等不可切换错误，直接失败。
    """
    headers = {"api-key": ep.api_key, "Content-Type": "application/json"}
    url = (
        f"{ep.endpoint}/openai/deployments/{ep.deployment}"
        f"/chat/completions?api-version={ep.api_version}"
    )
    max_retries = max(0, int(_settings.DOC_IMPORT_AZURE_MAX_RETRIES))
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                proxy=_settings.DOC_IMPORT_AZURE_PROXY_URL or None,
                timeout=_timeout(),
            ) as client:
                resp = await client.post(url, headers=headers, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            last_exc = e
            if attempt < max_retries:
                continue
            # 连接层不可达：本区不可用，切下一区域。
            raise _SwitchRegion(f"连接失败: {e}", switch_reason="conn", conn=True) from e
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < max_retries:
                continue
            raise _SwitchRegion(f"请求异常: {e}", switch_reason="http_err", conn=False) from e

        if resp.status_code == 200:
            try:
                raw = _extract_content(resp.json())
            except Exception as e:  # noqa: BLE001
                raise AzureVLMError(f"解析 Azure 响应失败: {e}") from e
            if not raw:
                raise AzureVLMError("Azure 返回空内容")
            try:
                return json.loads(_strip_code_fence(raw))
            except Exception as e:  # noqa: BLE001
                raise AzureVLMError(f"Azure 返回非法 JSON: {e}; raw={raw[:300]}") from e

        # 5xx / 429：本区内轻重试，仍失败则切下一区域。
        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt < max_retries:
                last_exc = AzureVLMError(f"Azure HTTP {resp.status_code}")
                continue
            raise _SwitchRegion(
                f"HTTP {resp.status_code}", switch_reason=str(resp.status_code), conn=False
            )
        # 4xx(非429)：请求本身有问题（如图片非法/部署不存在），切区无益，直接失败。
        raise AzureVLMError(f"Azure 返回 HTTP {resp.status_code}: {resp.text[:300]}")

    raise _SwitchRegion(f"未知失败: {last_exc}", switch_reason="unknown", conn=False)


async def caption_image(
    img_bytes: bytes,
    mime: str,
    page_png_bytes: Optional[bytes],
    *,
    with_page_context: bool,
    chart_to_table: bool,
    request_id: str,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """对单张图打标，返回结构化 dict。多区域端点池：起始区随全局游标轮询，
    单端点 429/5xx/连接错自动切下一区域（最多轮一圈），4xx 直接失败。

    返回字段：img_type / img_description / img_keywords(list) / chart_table_markdown。

    :param model: 视觉模型键名；留空取 FILE_UNDERSTAND_VLM_PRIMARY_MODEL。
    :raises AzureVLMConnError: 整轮区域均连接不可达（供上层降级）。
    :raises AzureVLMError: 其它失败（4xx、响应解析失败、整轮均失败）。
    """
    parts: List[dict] = [
        {"type": "image_url", "image_url": {"url": bytes_to_data_url(img_bytes, mime)}}
    ]
    user_text = "这是从文档抽取的目标图片，请理解并按要求输出 JSON。"
    if page_png_bytes and with_page_context:
        parts.append(
            {"type": "image_url", "image_url": {"url": bytes_to_data_url(page_png_bytes, "image/png")}}
        )
        user_text = (
            "第一张是从文档抽取的目标图片，第二张是它所在页的整页截图（仅作上下文）。"
            "请只针对第一张目标图片输出 JSON。"
        )
    if chart_to_table:
        user_text += " 若这是数据图表(chart)，请在 chart_table_markdown 中把数值转写成 Markdown 表格。"
    parts.append({"type": "text", "text": user_text})

    body = {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": parts},
        ],
        "max_tokens": _settings.DOC_IMPORT_AZURE_MAX_TOKENS,
        "temperature": _settings.DOC_IMPORT_VLM_TEMPERATURE,
        "response_format": {"type": "json_object"},
    }

    use_model = (model or _settings.FILE_UNDERSTAND_VLM_PRIMARY_MODEL or _settings.DOC_IMPORT_AZURE_MODEL).strip()
    endpoints = _pool_endpoints(use_model)

    all_conn = True  # 整轮是否都为连接层失败（决定抛 ConnError 还是普通 Error）
    last_switch: Optional[_SwitchRegion] = None
    for ep in endpoints:
        try:
            return await _call_endpoint(ep, body, request_id)
        except _SwitchRegion as sw:
            last_switch = sw
            all_conn = all_conn and sw.conn
            logger.warning(
                "[%s] Azure VLM 区域 %s 失败(%s)，切下一区域", request_id, ep.name, sw.switch_reason
            )
            continue
        # AzureVLMError(4xx/解析失败) 直接抛出，不切区。

    # 整轮都失败。
    detail = str(last_switch) if last_switch else "无可用端点"
    if all_conn:
        raise AzureVLMConnError(f"连接 Azure 失败（全部 {len(endpoints)} 区域）: {detail}")
    raise AzureVLMError(f"Azure 打标失败（全部 {len(endpoints)} 区域）: {detail}")
