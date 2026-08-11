# -*- coding: utf-8 -*-
"""单元素视觉的 Vertex Gemini adapter。

元素级视觉理解只发送"单个小元素"（单张图片 / 单个表格裁剪 PNG），因此这里只保留
Vertex Gemini 的单次多模态调用（原生吃图片，结构化输出用 responseSchema）。

历史上的整份 PDF 内联主链路与 Azure PDF 拆页分批 provider 已随重构移除：
  - 备用故障域下沉到 azure_vlm_client（单图 chat/completions），由上层 image_repair 调用；
  - 不再需要 provider 能力声明/多 provider fallback 编排（元素级由编排器直接调度）。
"""

from __future__ import annotations

import base64
import json

from services import gemini_vertex_client as gvc
from services.file_understand_provider import (
    ProviderInvalidResponseError,
    UnderstandGenerationRequest,
    UnderstandGenerationResult,
)
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/file_understand_providers.log")


class VertexGeminiProvider:
    """Vertex Gemini 单元素多模态 adapter。"""

    name = "vertex"

    def __init__(self, model: str):
        self.model = model

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
