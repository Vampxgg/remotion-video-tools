# -*- coding: utf-8 -*-
"""单元素视觉底层操作：dHash 去重、重要性过滤、caption 组装。"""

import io
from unittest import TestCase

from services import file_understand_image_repair as rep

try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover
    PILImage = None


def _png(color, size=(80, 80)):
    """生成一张纯色 PNG 字节，用于确定性哈希/尺寸测试。"""
    assert PILImage is not None, "Pillow 必需"
    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _gradient_png(seed, size=(120, 120)):
    """生成一张带梯度/纹理的 PNG（不同 seed -> 不同 dHash、足够字节数）。"""
    assert PILImage is not None, "Pillow 必需"
    w, h = size
    im = PILImage.new("RGB", size)
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 3 + seed) % 256, (y * 5 + seed) % 256, (x * y + seed) % 256)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class ComposeCaptionTests(TestCase):
    def test_rejects_unrecognizable(self):
        self.assertIsNone(rep._compose_caption({"img_description": "图片信息不足，无法可靠识别"}))
        self.assertIsNone(rep._compose_caption({"img_description": ""}))

    def test_composes_with_purpose(self):
        self.assertEqual(
            rep._compose_caption({"img_description": "设备正视图", "img_purpose": "展示结构"}),
            "设备正视图（用途：展示结构）",
        )


class DedupTests(TestCase):
    def test_identical_images_dedup_to_one(self):
        if PILImage is None:
            self.skipTest("Pillow 未安装")
        a = _gradient_png(0)
        b = _gradient_png(0)  # 与 a 像素一致 -> dHash 相同
        c = _gradient_png(100)  # 明显不同的纹理
        reps, alias = rep.dedup_by_hash(
            [("u_a", a), ("u_b", b), ("u_c", c)], hamming_thresh=5
        )
        # a 为代表，b 归并到 a；c 独立。
        self.assertIn("u_a", reps)
        self.assertIn("u_c", reps)
        self.assertEqual(alias.get("u_b"), "u_a")
        self.assertNotIn("u_b", reps)

    def test_missing_bytes_treated_as_independent(self):
        reps, alias = rep.dedup_by_hash([("u1", b""), ("u2", b"")], hamming_thresh=5)
        self.assertEqual(set(reps), {"u1", "u2"})
        self.assertEqual(alias, {})


class ImportanceFilterTests(TestCase):
    def test_tiny_bytes_filtered(self):
        # 字节数低于阈值 -> 判为装饰图，跳过。
        self.assertFalse(rep.image_importance_ok(b"x" * 10))

    def test_small_dim_filtered(self):
        if PILImage is None:
            self.skipTest("Pillow 未安装")
        tiny = _png((0, 0, 0), size=(16, 16))
        # 16px < 默认 MIN_DIM(64) -> 跳过（但字节需先过 MIN_BYTES）。
        if len(tiny) < rep._settings.FILE_UNDERSTAND_IMAGE_FILTER_MIN_BYTES:
            self.skipTest("测试图字节过小，被字节阈值先拦截")
        self.assertFalse(rep.image_importance_ok(tiny))

    def test_normal_image_ok(self):
        if PILImage is None:
            self.skipTest("Pillow 未安装")
        big = _gradient_png(7, size=(200, 200))
        self.assertGreaterEqual(len(big), rep._settings.FILE_UNDERSTAND_IMAGE_FILTER_MIN_BYTES)
        self.assertTrue(rep.image_importance_ok(big))

    def test_disabled_filter_passes_all(self):
        from unittest.mock import patch

        with patch.object(rep._settings, "FILE_UNDERSTAND_IMAGE_FILTER_ENABLED", False):
            self.assertTrue(rep.image_importance_ok(b"x" * 10))
