# -*- coding: utf-8 -*-
"""Azure VLM provider：PDF 页图分批、多批补丁合并去重、错误归类。"""

import asyncio
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from services import file_understand_providers as fp
from services.file_understand_provider import (
    ProviderRateLimitError,
    ProviderUnsupportedInputError,
    UnderstandGenerationRequest,
    VisualDocument,
)

_SCHEMA = {"type": "object", "properties": {}, "required": []}


def _req(doc):
    return UnderstandGenerationRequest(
        document=doc,
        system_instruction="sys",
        user_text="do it",
        response_schema=_SCHEMA,
        temperature=0.2,
        max_output_tokens=1024,
        request_id="t",
    )


class AzureProviderTests(TestCase):
    def test_pdf_without_pages_unsupported(self):
        prov = fp.AzureVLMProvider("m")
        doc = VisualDocument(data=b"x", mime_type="application/pdf", rendered_pages=None)
        with self.assertRaises(ProviderUnsupportedInputError):
            asyncio.run(prov.generate(_req(doc)))

    def test_pages_batched_and_patches_merged(self):
        prov = fp.AzureVLMProvider("m")
        doc = VisualDocument(
            data=b"x", mime_type="application/pdf",
            rendered_pages=[b"p1", b"p2", b"p3", b"p4", b"p5"],
        )
        # 每批 2 页 -> 3 批；每批返回不同 anchor/url，验证合并去重。
        batch_responses = [
            {"tables": [{"anchor": "1", "markdown": "t1"}], "images": [{"url": "u1", "kind": "figure", "caption": "c1"}]},
            {"tables": [{"anchor": "1", "markdown": "dup"}], "images": [{"url": "u2", "kind": "figure", "caption": "c2"}]},
            {"tables": [{"anchor": "2", "markdown": "t2"}], "images": []},
        ]
        import json as _json
        call = {"i": 0}

        async def fake_call_endpoints(body, req):
            i = call["i"]
            call["i"] += 1
            return {"choices": [{"message": {"content": _json.dumps(batch_responses[i])}}]}

        with patch.object(fp._settings, "FILE_UNDERSTAND_AZURE_PAGES_PER_REQUEST", 2), \
             patch.object(prov, "_call_endpoints", AsyncMock(side_effect=fake_call_endpoints)):
            result = asyncio.run(prov.generate(_req(doc)))

        merged = result.parsed_json
        anchors = {t["anchor"] for t in merged["tables"]}
        urls = {im["url"] for im in merged["images"]}
        self.assertEqual(anchors, {"1", "2"})  # anchor 1 去重
        self.assertEqual(urls, {"u1", "u2"})
        self.assertEqual(result.attempts, 3)

    def test_429_propagates(self):
        prov = fp.AzureVLMProvider("m")
        doc = VisualDocument(data=b"x", mime_type="image/png")
        with patch.object(prov, "_call_endpoints", AsyncMock(side_effect=ProviderRateLimitError("429"))):
            with self.assertRaises(ProviderRateLimitError):
                asyncio.run(prov.generate(_req(doc)))
