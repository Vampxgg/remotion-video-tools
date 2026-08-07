# -*- coding: utf-8 -*-
"""图片覆盖审计与缺失/低质量图片逐图补识别（only_missing 策略）。"""

import asyncio
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from services import file_understand_image_repair as rep


_BASE_MD = (
    "# t\n\n"
    "![](https://cdn/a.png)\n\n"          # 缺失描述
    "![图片](https://cdn/b.png)\n\n"      # 泛化描述
    "![一张清晰的设备结构示意图](https://cdn/c.png)\n"  # 合格
)


class ImageRepairAuditTests(TestCase):
    def test_low_quality_detection(self):
        seen = {}
        self.assertTrue(rep._is_low_quality(None, seen)[0])          # 缺失
        self.assertTrue(rep._is_low_quality("图片", seen)[0])        # 泛化
        self.assertTrue(rep._is_low_quality("源文档图片1", seen)[0])  # 占位
        self.assertTrue(rep._is_low_quality("短", seen)[0])          # 过短
        self.assertFalse(rep._is_low_quality("一张清晰的设备结构示意图", seen)[0])

    def test_duplicate_caption_flagged(self):
        seen = {"重复的同一句描述内容": 1}
        low, _ = rep._is_low_quality("重复的同一句描述内容", seen)
        self.assertTrue(low)

    def test_compose_caption_rejects_unrecognizable(self):
        self.assertIsNone(rep._compose_caption({"img_description": "图片信息不足，无法可靠识别"}))
        self.assertIsNone(rep._compose_caption({"img_description": ""}))
        self.assertEqual(
            rep._compose_caption({"img_description": "设备正视图", "img_purpose": "展示结构"}),
            "设备正视图（用途：展示结构）",
        )

    def test_repair_only_missing_and_accounts_all(self):
        # 只对 a/b 两张缺失/泛化补识别；c 已合格不动。补识别成功 a，失败 b。
        async def fake_recognize(img_bytes, mime, request_id):
            return "补识别得到的清晰描述" if img_bytes == b"A" else None

        async def fake_download(url):
            return (b"A" if url.endswith("a.png") else b"B", "image/png")

        async def _run():
            with patch.object(rep, "_download", AsyncMock(side_effect=fake_download)), \
                 patch.object(rep, "_recognize_one", AsyncMock(side_effect=fake_recognize)), \
                 patch.object(rep._settings, "FILE_UNDERSTAND_IMAGE_REPAIR_MAX_IMAGES", 60), \
                 patch.object(rep._settings, "FILE_UNDERSTAND_IMAGE_REPAIR_CONCURRENCY", 2):
                return await rep.repair_missing_captions(
                    _BASE_MD, _BASE_MD, request_id="t", deadline_at=1e18
                )

        md, stats = asyncio.run(_run())
        # 账目守恒：已描述 + 未解决 == 源图总数（3）。
        self.assertEqual(stats["described"] + len(stats["unresolved_ids"]), 3)
        self.assertEqual(stats["repaired"], 1)
        self.assertIn("https://cdn/b.png", stats["unresolved_ids"])
        # a 的新描述已回填。
        self.assertIn("补识别得到的清晰描述", md)

    def test_no_images_full_coverage(self):
        md, stats = asyncio.run(
            rep.repair_missing_captions("# t\n\n无图", "# t\n\n无图", request_id="t", deadline_at=1e18)
        )
        self.assertEqual(stats["coverage"], 1.0)
        self.assertEqual(stats["repaired"], 0)
