# -*- coding: utf-8 -*-
"""元素级视觉编排与端到端 meta：逐元素补丁产出、单元素失败隔离、降级语义、账目守恒。"""

import asyncio
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from services import file_understand_element_vision as ev
from services import file_understand_service as fus
from services.document_ast_service import DocumentAST, ImageElement, TableElement


class ElementVisionTests(TestCase):
    """直接驱动 run_element_vision，隔离补丁产出与统计。"""

    def _run(self, ast):
        return asyncio.run(
            ev.run_element_vision(ast, request_id="t", deadline_at=1e18)
        )

    def test_image_recognized_emits_patch(self):
        ast = DocumentAST(images=[ImageElement(url="https://cdn/a.png", data=b"A", mime="image/png")])

        async def fake_recog(img_bytes, mime, request_id, *, chart_to_table=True):
            return {"img_type": "figure", "img_description": "一张设备照片"}

        with patch.object(ev, "recognize_image_full", AsyncMock(side_effect=fake_recog)), \
             patch.object(ev, "image_importance_ok", lambda b: True), \
             patch.object(ev._settings, "FILE_UNDERSTAND_IMAGE_DEDUP_ENABLED", False):
            patch_dict, stats = self._run(ast)

        self.assertEqual(len(patch_dict["images"]), 1)
        self.assertEqual(patch_dict["images"][0]["url"], "https://cdn/a.png")
        self.assertEqual(patch_dict["images"][0]["kind"], "figure")
        self.assertEqual(stats["images_vision_called"], 1)

    def test_chart_emits_table_markdown(self):
        ast = DocumentAST(images=[ImageElement(url="https://cdn/chart.png", data=b"C", mime="image/png")])

        async def fake_recog(img_bytes, mime, request_id, *, chart_to_table=True):
            return {
                "img_type": "chart",
                "img_description": "季度营收柱状图",
                "chart_table_markdown": "| Q | 值 |\n| - | - |\n| Q1 | 5 |",
            }

        with patch.object(ev, "recognize_image_full", AsyncMock(side_effect=fake_recog)), \
             patch.object(ev, "image_importance_ok", lambda b: True), \
             patch.object(ev._settings, "FILE_UNDERSTAND_IMAGE_DEDUP_ENABLED", False):
            patch_dict, _ = self._run(ast)

        img = patch_dict["images"][0]
        self.assertEqual(img["kind"], "chart")
        self.assertIn("| Q1 | 5 |", img["table_markdown"])

    def test_dedup_reuses_caption_single_call(self):
        # 两张同 URL 前缀不同但字节一致 -> 去重后只识别一次，别名复用结果。
        ast = DocumentAST(images=[
            ImageElement(url="https://cdn/a.png", data=b"SAME", mime="image/png"),
            ImageElement(url="https://cdn/b.png", data=b"SAME", mime="image/png"),
        ])
        calls = {"n": 0}

        async def fake_recog(img_bytes, mime, request_id, *, chart_to_table=True):
            calls["n"] += 1
            return {"img_type": "figure", "img_description": "同一张 Logo"}

        with patch.object(ev, "recognize_image_full", AsyncMock(side_effect=fake_recog)), \
             patch.object(ev, "image_importance_ok", lambda b: True), \
             patch.object(ev, "dedup_by_hash", lambda items, th: (["https://cdn/a.png"], {"https://cdn/b.png": "https://cdn/a.png"})):
            patch_dict, stats = self._run(ast)

        self.assertEqual(calls["n"], 1)  # 只识别代表图
        urls = {im["url"] for im in patch_dict["images"]}
        self.assertEqual(urls, {"https://cdn/a.png", "https://cdn/b.png"})  # 别名也回填
        self.assertEqual(stats["images_deduped"], 1)

    def test_unrecognized_image_marked_unresolved(self):
        ast = DocumentAST(images=[ImageElement(url="https://cdn/x.png", data=b"X", mime="image/png")])

        async def fake_recog(*a, **k):
            return None

        with patch.object(ev, "recognize_image_full", AsyncMock(side_effect=fake_recog)), \
             patch.object(ev, "image_importance_ok", lambda b: True), \
             patch.object(ev._settings, "FILE_UNDERSTAND_IMAGE_DEDUP_ENABLED", False):
            patch_dict, stats = self._run(ast)

        self.assertEqual(patch_dict["images"], [])
        self.assertIn("https://cdn/x.png", stats["unresolved_ids"])

    def test_filtered_image_skips_vision(self):
        ast = DocumentAST(images=[ImageElement(url="https://cdn/logo.png", data=b"L", mime="image/png")])

        recog = AsyncMock()
        with patch.object(ev, "recognize_image_full", recog), \
             patch.object(ev, "image_importance_ok", lambda b: False):
            patch_dict, stats = self._run(ast)

        recog.assert_not_called()
        self.assertEqual(stats["images_filtered"], 1)
        self.assertEqual(patch_dict["images"], [])

    def test_low_confidence_table_proofread(self):
        ast = DocumentAST(tables=[
            TableElement(anchor="1", base_markdown="| a |\n| - |\n|  |", low_confidence=True, crop_png=b"PNG"),
            TableElement(anchor="2", base_markdown="| x | y |\n| - | - |\n| 1 | 2 |", low_confidence=False, crop_png=b"PNG"),
        ])

        async def fake_proof(crop, base_md, request_id):
            return "| a |\n| - |\n| 校对后 |"

        with patch.object(ev, "proofread_table", AsyncMock(side_effect=fake_proof)):
            patch_dict, stats = self._run(ast)

        # 只有低置信度表被校对。
        self.assertEqual(len(patch_dict["tables"]), 1)
        self.assertEqual(patch_dict["tables"][0]["anchor"], "1")
        self.assertEqual(stats["table_vision_calls"], 1)


_BASE_MD = "# t\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n![x](https://cdn/img1.png)"


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

    def _run_understand(self, patch_dict, stats):
        from services.file_parse_service import FilePayload
        from services.file_understand_service import UnderstandOptions

        payload = FilePayload(filename="d.pdf", content=b"%PDF-1.4 x", media_type="application/pdf")

        async def fake_ev(ast, *, request_id, deadline_at):
            return patch_dict, stats

        async def _run():
            with patch.object(fus, "parse_file_payload", AsyncMock(return_value=self._fake_base())), \
                 patch.object(fus, "build_document_ast", return_value=DocumentAST()), \
                 patch.object(fus, "run_element_vision", AsyncMock(side_effect=fake_ev)), \
                 patch.object(fus._settings, "FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED", False):
                return await fus.understand_file_payload(payload, UnderstandOptions())

        return asyncio.run(_run())

    def test_enhanced_when_patch_applies(self):
        patch_dict = {
            "tables": [{"anchor": "1", "markdown": "| a | b |\n| - | - |\n| 10 | 20 |"}],
            "images": [{"url": "https://cdn/img1.png", "kind": "figure", "caption": "一张示意图"}],
        }
        stats = {
            "images_total": 1, "images_deduped": 0, "images_filtered": 0,
            "images_vision_called": 1, "tables_total": 1, "table_vision_calls": 1,
            "unresolved_ids": [],
        }
        result = self._run_understand(patch_dict, stats)
        meta = result.meta
        self.assertEqual(meta["vision_status"], "enhanced")
        self.assertTrue(meta["understanding_applied"])
        self.assertEqual(meta["understanding_mode"], "element")
        self.assertEqual(meta["source_image_count"], 1)
        self.assertIn("| 10 | 20 |", result.content.markdown)

    def test_degraded_when_zero_effect(self):
        patch_dict = {"tables": [], "images": []}
        stats = {
            "images_total": 1, "images_deduped": 0, "images_filtered": 0,
            "images_vision_called": 1, "tables_total": 1, "table_vision_calls": 0,
            "unresolved_ids": ["https://cdn/img1.png"],
        }
        result = self._run_understand(patch_dict, stats)
        meta = result.meta
        self.assertEqual(meta["vision_status"], "degraded")
        self.assertFalse(meta["understanding_applied"])
        self.assertEqual(result.content.markdown, _BASE_MD)
        self.assertTrue(meta["fallback_reason"])
        self.assertIn("https://cdn/img1.png", meta["unresolved_image_ids"])
