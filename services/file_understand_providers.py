# -*- coding: utf-8 -*-
"""多模态理解的 provider adapter：Vertex Gemini（主）与 Azure VLM（备）。

两者都实现同一个协议：``async def generate(req) -> UnderstandGenerationResult``，
并声明 ``capabilities``。编排层（file_understand_service）据此选择输入编码、按 fallback
顺序调用、按错误类型决定重试/切换。

- VertexGeminiProvider：原生吃 PDF/图片，走 gemini_vertex_client；结构化输出用 responseSchema。
- AzureVLMProvider：只吃图片，PDF 用 visual_input_adapter 拆页后分批发送；结构化输出用
  response_format=json_object，并把多批结果合并。异构鉴权/网络/端点，作为 Gemini 故障的独立故障域。
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

import httpx

from services import gemini_vertex_client as gvc
from services import visual_input_adapter
from services.file_understand_provider import (
    ProviderAuthError,
    ProviderCapabilities,
    ProviderInvalidResponseError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnsupportedInputError,
    UnderstandGenerationRequest,
    UnderstandGenerationResult,
)
from utils.azure_models import AzureModelsConfigError, resolve_model
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/file_understand_providers.log")

_IMAGE_MIMES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
)


# =========================== Vertex Gemini ===========================


class VertexGeminiProvider:
    """主 provider：Vertex Gemini，原生多模态。"""

    name = "vertex"

    def __init__(self, model: str):
        self.model = model

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            native_pdf=True,
            image_mime_types=_IMAGE_MIMES,
            json_object=True,
            json_schema=True,
        )

    def _generation_config(self, req: UnderstandGenerationRequest) -> dict:
        cfg: dict = {
            "temperature": req.temperature,
            "maxOutputTokens": req.max_output_tokens,
        }
        res = _settings.FILE_UNDERSTAND_MEDIA_RESOLUTION
        if res and "gemini-3" in self.model:
            cfg["mediaResolution"] = f"MEDIA_RESOLUTION_{res.strip().upper()}"
        budget = _settings.FILE_UNDERSTAND_THINKING_BUDGET
        if budget is not None and budget >= 0 and (
            "gemini-2.5" in self.model or "gemini-3" in self.model
        ):
            cfg["thinkingConfig"] = {"thinkingBudget": budget}
        if req.response_schema is not None:
            cfg["responseMimeType"] = "application/json"
            cfg["responseSchema"] = req.response_schema
        return cfg

    async def generate(
        self, req: UnderstandGenerationRequest
    ) -> UnderstandGenerationResult:
        doc = req.document
        b64 = base64.b64encode(doc.data).decode("ascii")
        vision_part = {"inlineData": {"mimeType": doc.mime_type, "data": b64}}
        contents = [
            {"role": "user", "parts": [vision_part, {"text": req.user_text}]}
        ]
        timeout = req.deadline_sec or _settings.FILE_UNDERSTAND_TIMEOUT_SEC
        # gemini_vertex_client 已将底层异常归一为 Provider* 错误，这里直接透传。
        data = await gvc.generate_content(
            model=self.model,
            contents=contents,
            generation_config=self._generation_config(req),
            system_instruction=req.system_instruction,
            location=_settings.FILE_UNDERSTAND_LOCATION,
            timeout_sec=timeout,
            max_locations=_settings.FILE_UNDERSTAND_MAX_REGIONS,
            request_id=req.request_id,
        )
        text = gvc.extract_text(data).strip()
        if not text:
            raise ProviderInvalidResponseError("Vertex 未返回有效文本")
        parsed = None
        if req.response_schema is not None:
            try:
                parsed = json.loads(text)
            except Exception as e:  # noqa: BLE001
                raise ProviderInvalidResponseError(f"Vertex 返回非法 JSON: {e}") from e
        return UnderstandGenerationResult(
            text=text,
            parsed_json=parsed,
            provider=self.name,
            model=self.model,
            finish_reason=gvc.finish_reason(data),
        )


# =========================== Azure VLM ===========================


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _azure_extract_content(resp_json: Dict[str, Any]) -> str:
    choices = resp_json.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        ).strip()
    return (content or "").strip()


class AzureVLMProvider:
    """备用 provider：Azure OpenAI 兼容 VLM（异构故障域）。

    只吃图片：PDF 由编排层通过 document.rendered_pages 提供页图，这里按批发送。
    结构化输出用 response_format=json_object（无严格 schema，编排层再做本地校验）。
    """

    name = "azure"

    def __init__(self, model: str):
        self.model = model

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            native_pdf=False,
            image_mime_types=_IMAGE_MIMES,
            json_object=True,
            json_schema=False,
            max_pages_per_request=max(
                1, int(_settings.FILE_UNDERSTAND_AZURE_PAGES_PER_REQUEST)
            ),
        )

    def _resolve_endpoints(self):
        """解析 Azure 端点列表（首项主用，其余 fallback）。"""
        override_ep = (_settings.DOC_IMPORT_AZURE_ENDPOINT or "").strip().rstrip("/")
        override_key = (_settings.DOC_IMPORT_AZURE_API_KEY or "").strip()
        if override_ep and override_key:
            from utils.azure_models import AzureEndpoint

            return [
                AzureEndpoint(
                    name="override",
                    endpoint=override_ep,
                    api_key=override_key,
                    deployment=(
                        _settings.DOC_IMPORT_AZURE_DEPLOYMENT or self.model
                    ).strip(),
                    api_version=(_settings.DOC_IMPORT_AZURE_API_VERSION or "").strip(),
                )
            ]
        try:
            resolved = resolve_model(self.model)
        except AzureModelsConfigError as e:
            raise ProviderRequestError(f"Azure 模型解析失败({self.model}): {e}") from e
        return resolved.endpoints

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=_settings.DOC_IMPORT_AZURE_CONNECT_TIMEOUT,
            read=_settings.DOC_IMPORT_AZURE_READ_TIMEOUT,
            write=_settings.DOC_IMPORT_AZURE_WRITE_TIMEOUT,
            pool=_settings.DOC_IMPORT_AZURE_CONNECT_TIMEOUT,
        )

    def _build_image_parts(self, req: UnderstandGenerationRequest) -> List[dict]:
        """把输入编码成 Azure image_url parts。PDF 用页图，图片直接发。"""
        doc = req.document
        parts: List[dict] = []
        if doc.mime_type in _IMAGE_MIMES:
            b64 = base64.b64encode(doc.data).decode("ascii")
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{doc.mime_type};base64,{b64}"}}
            )
            return parts
        pages = doc.rendered_pages or []
        if not pages:
            raise ProviderUnsupportedInputError(
                f"Azure 不支持原生 {doc.mime_type}，且未提供页图 rendered_pages。"
            )
        for png in pages:
            b64 = base64.b64encode(png).decode("ascii")
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            )
        return parts

    async def _post_once(
        self, endpoint, body: dict, req: UnderstandGenerationRequest
    ) -> Dict[str, Any]:
        url = (
            f"{endpoint.endpoint}/openai/deployments/{endpoint.deployment}"
            f"/chat/completions?api-version={endpoint.api_version}"
        )
        headers = {"api-key": endpoint.api_key, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                proxy=_settings.DOC_IMPORT_AZURE_PROXY_URL or None,
                timeout=self._timeout(),
            ) as client:
                resp = await client.post(url, headers=headers, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            raise ProviderTimeoutError(f"连接 Azure 失败: {e}") from e
        except httpx.TimeoutException as e:
            raise ProviderTimeoutError(f"Azure 超时: {e}") from e
        except httpx.HTTPError as e:
            raise ProviderUnavailableError(f"请求 Azure 异常: {e}") from e

        code = resp.status_code
        if code == 200:
            return resp.json()
        if code == 429:
            ra = resp.headers.get("Retry-After")
            try:
                retry_after = float(ra) if ra else None
            except (TypeError, ValueError):
                retry_after = None
            raise ProviderRateLimitError(
                f"Azure HTTP 429: {resp.text[:200]}", retry_after=retry_after
            )
        if code in (401, 403):
            raise ProviderAuthError(f"Azure 鉴权失败 HTTP {code}: {resp.text[:200]}")
        if code >= 500:
            raise ProviderUnavailableError(f"Azure HTTP {code}: {resp.text[:200]}")
        raise ProviderRequestError(f"Azure HTTP {code}: {resp.text[:200]}")

    async def _call_endpoints(
        self, body: dict, req: UnderstandGenerationRequest
    ) -> Dict[str, Any]:
        """按端点列表逐个尝试；可切换错误换端点，确定性错误立即抛。"""
        endpoints = self._resolve_endpoints()
        last: Optional[Exception] = None
        for i, ep in enumerate(endpoints):
            try:
                return await self._post_once(ep, body, req)
            except (ProviderRateLimitError, ProviderUnavailableError, ProviderTimeoutError, ProviderAuthError) as e:
                last = e
                logger.warning(
                    "[%s] Azure 端点 %s (%s/%s) 失败可切换: %s",
                    req.request_id, ep.name, i + 1, len(endpoints), e,
                )
                continue
            except ProviderRequestError:
                raise
        if last:
            raise last
        raise ProviderInvalidResponseError("Azure 无可用端点")

    async def generate(
        self, req: UnderstandGenerationRequest
    ) -> UnderstandGenerationResult:
        image_parts = self._build_image_parts(req)
        # 按每请求页数上限分批（图片输入只有 1 张时即单批）。
        per = self.capabilities.max_pages_per_request or len(image_parts)
        batches = [image_parts[i : i + per] for i in range(0, len(image_parts), per)] or [[]]

        merged_tables: List[dict] = []
        merged_images: List[dict] = []
        merged_text_parts: List[str] = []
        seen_anchors: set = set()
        seen_urls: set = set()
        attempts = 0

        for bi, batch in enumerate(batches):
            user_text = req.user_text
            if len(batches) > 1:
                user_text = (
                    f"（本文档被拆为 {len(batches)} 批页图，这是第 {bi + 1} 批）\n" + user_text
                )
            parts = list(batch) + [{"type": "text", "text": user_text}]
            body = {
                "messages": [
                    {"role": "system", "content": req.system_instruction},
                    {"role": "user", "content": parts},
                ],
                "max_tokens": _settings.DOC_IMPORT_AZURE_MAX_TOKENS,
                "temperature": req.temperature,
            }
            if req.response_schema is not None:
                body["response_format"] = {"type": "json_object"}

            resp_json = await self._call_endpoints(body, req)
            attempts += 1
            raw = _azure_extract_content(resp_json)
            if not raw:
                raise ProviderInvalidResponseError("Azure 返回空内容")
            if req.response_schema is None:
                merged_text_parts.append(raw)
                continue
            try:
                obj = json.loads(_strip_code_fence(raw))
            except Exception as e:  # noqa: BLE001
                raise ProviderInvalidResponseError(
                    f"Azure 返回非法 JSON: {e}; raw={raw[:200]}"
                ) from e
            # 合并各批补丁，anchor/url 去重（后批不覆盖前批）。
            for t in (obj.get("tables") or []):
                if isinstance(t, dict):
                    a = str(t.get("anchor", "")).strip()
                    if a and a not in seen_anchors:
                        seen_anchors.add(a)
                        merged_tables.append(t)
            for im in (obj.get("images") or []):
                if isinstance(im, dict):
                    u = (im.get("url") or "").strip()
                    if u and u not in seen_urls:
                        seen_urls.add(u)
                        merged_images.append(im)

        if req.response_schema is not None:
            merged = {"tables": merged_tables, "images": merged_images}
            return UnderstandGenerationResult(
                text=json.dumps(merged, ensure_ascii=False),
                parsed_json=merged,
                provider=self.name,
                model=self.model,
                attempts=attempts,
            )
        return UnderstandGenerationResult(
            text="\n\n".join(merged_text_parts).strip(),
            parsed_json=None,
            provider=self.name,
            model=self.model,
            attempts=attempts,
        )
