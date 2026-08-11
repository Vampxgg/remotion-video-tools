# -*- coding: utf-8 -*-
"""异步多模态理解的墙钟预算兜底：视觉阶段超预算时仍返回完整基础解析（全文+全部真实图URL）。

覆盖计划核心不变量：
- run_element_vision 到点被墙钟中断时，understand_file_payload 仍返回 base_md 全文与全部源图 URL；
- on_base_ready 回调在基础解析完成后立即触发（供异步任务写 partial_result 兜底）；
- 视觉零增强时 vision_status=degraded，但内容不为空。
"""

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from schemas.file_parse import (
    FileParseContent,
    FileParseFileInfo,
    FileParseParserInfo,
    FileParseResult,
)
from services import file_understand_service as svc
from services.file_parse_service import FilePayload


def _fake_base_result() -> FileParseResult:
    md = (
        "# 教材\n\n正文一段。\n\n"
        "![图1](https://example.com/a.png)\n\n"
        "![图2](https://example.com/b.png)\n\n"
        "结尾。"
    )
    return FileParseResult(
        status="ok",
        file=FileParseFileInfo(filename="t.docx", extension=".docx", size=123, media_type=None),
        content=FileParseContent(markdown=md, text=None, char_count=len(md), truncated=False),
        parser=FileParseParserInfo(content_kind="office", parser_used="python-docx"),
        meta={},
        assets=[],
        warnings=[],
    )


class BudgetFallbackTests(IsolatedAsyncioTestCase):
    async def _run(self, *, vision_delay: float, budget: float):
        payload = FilePayload(filename="t.docx", content=b"x" * 10, media_type=None)
        base = _fake_base_result()
        base_ready_hits = []

        async def _slow_vision(ast, *, request_id, deadline_at):
            # 模拟耗时视觉：睡到超过预算，模拟内部墙钟中断后返回空补丁。
            await asyncio.sleep(vision_delay)
            return {"tables": [], "images": []}, {}

        with patch.object(svc, "parse_file_payload", return_value=base), \
             patch.object(svc, "build_document_ast", return_value=type("A", (), {"warnings": []})()), \
             patch.object(svc, "run_element_vision", side_effect=_slow_vision), \
             patch.object(svc._settings, "FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED", False):
            result = await svc.understand_file_payload(
                payload,
                svc.UnderstandOptions(enable_vision=True),
                budget_sec=budget,
                on_base_ready=lambda b: base_ready_hits.append(b),
            )
        return result, base_ready_hits

    async def test_budget_exceeded_returns_full_base(self):
        # 预算 1s，但视觉要睡 5s -> wait_for 硬超时 -> 返回完整 base。
        result, hits = await self._run(vision_delay=5.0, budget=1.0)
        md = result.content.markdown or ""
        # 全文与两张真实图 URL 都在（内容绝不为空）。
        self.assertIn("正文一段", md)
        self.assertIn("https://example.com/a.png", md)
        self.assertIn("https://example.com/b.png", md)
        self.assertEqual(result.meta.get("vision_status"), "degraded")
        self.assertTrue((result.meta.get("fallback_reason") or "").startswith("wallclock_budget"))

    async def test_on_base_ready_fired_before_vision(self):
        result, hits = await self._run(vision_delay=5.0, budget=1.0)
        # 基础解析就绪回调必被触发一次，且拿到的是含图 URL 的 base 结果。
        self.assertEqual(len(hits), 1)
        self.assertIn("https://example.com/a.png", hits[0].content.markdown)

    async def test_partial_enhancement_merged_when_some_done(self):
        # 视觉在预算内返回了 1 张图的补丁（模拟"能识别多少算多少"），应合并且状态 enhanced。
        payload = FilePayload(filename="t.docx", content=b"x" * 10, media_type=None)
        base = _fake_base_result()

        async def _partial_vision(ast, *, request_id, deadline_at):
            return (
                {"tables": [], "images": [
                    {"url": "https://example.com/a.png", "kind": "figure", "caption": "识别得到的说明"}
                ]},
                {"images_vision_called": 1, "unresolved_ids": ["https://example.com/b.png"]},
            )

        with patch.object(svc, "parse_file_payload", return_value=base), \
             patch.object(svc, "build_document_ast", return_value=type("A", (), {"warnings": []})()), \
             patch.object(svc, "run_element_vision", side_effect=_partial_vision), \
             patch.object(svc._settings, "FILE_UNDERSTAND_GLOBAL_LIMITER_ENABLED", False):
            result = await svc.understand_file_payload(
                payload,
                svc.UnderstandOptions(enable_vision=True),
                budget_sec=60.0,
            )
        md = result.content.markdown or ""
        self.assertEqual(result.meta.get("vision_status"), "enhanced")
        # 已识别图的 caption 合并进去，另一张未识别的源图仍保留 URL（不丢图）。
        self.assertIn("识别得到的说明", md)
        self.assertIn("https://example.com/b.png", md)


class RunBoundedTests(IsolatedAsyncioTestCase):
    async def test_collects_done_and_cancels_pending(self):
        from services import file_understand_element_vision as ev

        done_marks = []

        async def _fast():
            done_marks.append("fast")

        async def _slow():
            try:
                await asyncio.sleep(10)
                done_marks.append("slow")
            except asyncio.CancelledError:
                done_marks.append("cancelled")
                raise

        import time as _t
        await ev._run_bounded(
            [_fast(), _slow()],
            deadline_at=_t.time() + 0.3,
            request_id="test",
            stage="unit",
        )
        # 快任务完成、慢任务被取消（不会阻塞到 10s）。
        self.assertIn("fast", done_marks)
        self.assertIn("cancelled", done_marks)
        self.assertNotIn("slow", done_marks)
