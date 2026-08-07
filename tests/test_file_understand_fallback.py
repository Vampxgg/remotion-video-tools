# -*- coding: utf-8 -*-
"""多模态编排 fallback 与有效增强判定：主失败切备用、零增强判无效、两家失败兜底。"""

import asyncio
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from services import file_understand_service as fus
from services.file_understand_provider import (
    ProviderCapabilities,
    ProviderRateLimitError,
    UnderstandGenerationResult,
    VisualDocument,
)


class _FakeProvider:
    def __init__(self, name, *, native_pdf=True, result=None, error=None):
        self.name = name
        self._native_pdf = native_pdf
        self._result = result
        self._error = error
        self.calls = 0

    @property
    def capabilities(self):
        return ProviderCapabilities(
            name=self.name,
            native_pdf=self._native_pdf,
            image_mime_types=frozenset({"image/png"}),
            json_object=True,
            json_schema=self.name == "vertex",
        )

    async def generate(self, req):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


_BASE_MD = "# t\n\n<!--TBL:1-->\n| a | b |\n| - | - |\n| 1 | 2 |\n\n![x](https://cdn/img1.png)"


def _patch_result(provider_name):
    parsed = {
        "tables": [{"anchor": "1", "markdown": "| a | b |\n| - | - |\n| 10 | 20 |"}],
        "images": [{"url": "https://cdn/img1.png", "kind": "figure", "caption": "一张示意图"}],
    }
    return UnderstandGenerationResult(
        text="{}", parsed_json=parsed, provider=provider_name, model="m"
    )


def _empty_patch_result(provider_name):
    return UnderstandGenerationResult(
        text="{}", parsed_json={"tables": [], "images": []}, provider=provider_name, model="m"
    )


class OrchestrationTests(TestCase):
    def setUp(self):
        # 关闭逐图补识别，隔离 fallback 逻辑本身。
        self._p = patch.object(fus._settings, "FILE_UNDERSTAND_IMAGE_REPAIR_ENABLED", False)
        self._p.start()
        self._p2 = patch.object(fus._settings, "FILE_UNDERSTAND_PATCH_MODE", True)
        self._p2.start()

    def tearDown(self):
        self._p.stop()
        self._p2.stop()

    def _run(self, providers):
        doc = VisualDocument(data=b"x", mime_type="application/pdf", rendered_pages=[b"p"])
        return asyncio.run(
            fus._run_vision_with_fallback(
                providers, doc, _BASE_MD, "t", deadline_at=1e18
            )
        )

    def test_validate_rejects_zero_effect_patch(self):
        anchored, _ = fus._anchor_tables(_BASE_MD)
        ok, _reason = fus._validate_patches({"tables": [], "images": []}, anchored, ["https://cdn/img1.png"])
        self.assertFalse(ok)

    def test_validate_accepts_hitting_patch(self):
        anchored, _ = fus._anchor_tables(_BASE_MD)
        patches = {"tables": [{"anchor": "1", "markdown": "| a |\n| - |\n| 9 |"}], "images": []}
        ok, _reason = fus._validate_patches(patches, anchored, ["https://cdn/img1.png"])
        self.assertTrue(ok)

    def test_primary_success_no_fallback(self):
        vertex = _FakeProvider("vertex", result=_patch_result("vertex"))
        azure = _FakeProvider("azure", native_pdf=False, result=_patch_result("azure"))
        enriched, applied, used, trace, _w = self._run([vertex, azure])
        self.assertTrue(applied)
        self.assertEqual(used, "vertex")
        self.assertEqual(azure.calls, 0)

    def test_primary_429_falls_back_to_azure(self):
        vertex = _FakeProvider("vertex", error=ProviderRateLimitError("429"))
        azure = _FakeProvider("azure", native_pdf=False, result=_patch_result("azure"))
        enriched, applied, used, trace, _w = self._run([vertex, azure])
        self.assertTrue(applied)
        self.assertEqual(used, "azure")
        self.assertEqual(azure.calls, 1)

    def test_zero_effect_primary_falls_back(self):
        vertex = _FakeProvider("vertex", result=_empty_patch_result("vertex"))
        azure = _FakeProvider("azure", native_pdf=False, result=_patch_result("azure"))
        enriched, applied, used, trace, _w = self._run([vertex, azure])
        self.assertTrue(applied)
        self.assertEqual(used, "azure")

    def test_all_fail_degrades_to_base(self):
        vertex = _FakeProvider("vertex", error=ProviderRateLimitError("429"))
        azure = _FakeProvider("azure", native_pdf=False, error=ProviderRateLimitError("429"))
        enriched, applied, used, trace, _w = self._run([vertex, azure])
        self.assertFalse(applied)
        self.assertEqual(enriched, _BASE_MD)
        self.assertEqual(used, "")

    def test_azure_skipped_when_no_rendered_pages(self):
        # PDF 无页图时 azure 不可用，应被跳过（trace 记 unsupported_input）。
        doc = VisualDocument(data=b"x", mime_type="application/pdf", rendered_pages=None)
        azure = _FakeProvider("azure", native_pdf=False, result=_patch_result("azure"))
        enriched, applied, used, trace, _w = asyncio.run(
            fus._run_vision_with_fallback([azure], doc, _BASE_MD, "t", deadline_at=1e18)
        )
        self.assertFalse(applied)
        self.assertEqual(azure.calls, 0)
        self.assertEqual(trace[0]["status"], "unsupported_input")


class EndToEndMetaTests(TestCase):
    """经 understand_file_payload 走通全链路，校验 meta 语义与图片账目守恒。"""

    def _fake_base(self):
        from schemas.file_parse import (
            FileParseContent,
            FileParseFileInfo,
            FileParseParserInfo,
            FileParseResult,
        )

        return FileParseResult(
            status="ok",
            file=FileParseFileInfo(filename="d.pdf", extension=".pdf", size=10, media_type="application/pdf"),
            content=FileParseContent(markdown=_BASE_MD, text=None, char_count=len(_BASE_MD), truncated=False),
            parser=FileParseParserInfo(content_kind="pdf", parser_used="base", fallback_used=False),
            meta={},
            assets=[],
            warnings=[],
            error=None,
        )

    def test_enhanced_status_and_coverage(self):
        from services.file_parse_service import FilePayload

        payload = FilePayload(filename="d.pdf", content=b"%PDF-1.4 x", media_type="application/pdf")
        vertex = _FakeProvider("vertex", result=_patch_result("vertex"))

        async def _run():
            with patch.object(fus, "parse_file_payload", AsyncMock(return_value=self._fake_base())), \
                 patch.object(fus, "_prepare_vision_document", AsyncMock(return_value=(VisualDocument(data=b"x", mime_type="application/pdf", rendered_pages=[b"p"]), None))), \
                 patch.object(fus, "_build_providers", return_value=[vertex]), \
                 patch.object(fus._settings, "FILE_UNDERSTAND_IMAGE_REPAIR_ENABLED", False), \
                 patch.object(fus._settings, "FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED", False):
                from services.file_understand_service import UnderstandOptions

                return await fus.understand_file_payload(payload, UnderstandOptions())

        result = asyncio.run(_run())
        meta = result.meta
        self.assertEqual(meta["vision_status"], "enhanced")
        self.assertTrue(meta["understanding_applied"])
        self.assertEqual(meta["understanding_provider"], "vertex")
        self.assertEqual(meta["source_image_count"], 1)

    def test_all_fail_degraded_status(self):
        from services.file_parse_service import FilePayload

        payload = FilePayload(filename="d.pdf", content=b"%PDF-1.4 x", media_type="application/pdf")
        vertex = _FakeProvider("vertex", error=ProviderRateLimitError("429"))

        async def _run():
            with patch.object(fus, "parse_file_payload", AsyncMock(return_value=self._fake_base())), \
                 patch.object(fus, "_prepare_vision_document", AsyncMock(return_value=(VisualDocument(data=b"x", mime_type="application/pdf", rendered_pages=[b"p"]), None))), \
                 patch.object(fus, "_build_providers", return_value=[vertex]), \
                 patch.object(fus._settings, "FILE_UNDERSTAND_IMAGE_REPAIR_ENABLED", False), \
                 patch.object(fus._settings, "FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED", False):
                from services.file_understand_service import UnderstandOptions

                return await fus.understand_file_payload(payload, UnderstandOptions())

        result = asyncio.run(_run())
        meta = result.meta
        self.assertEqual(meta["vision_status"], "degraded")
        self.assertFalse(meta["understanding_applied"])
        self.assertEqual(result.content.markdown, _BASE_MD)
        self.assertTrue(meta["fallback_reason"])
