# -*- coding: utf-8 -*-
"""本次加固新增逻辑的回归测试：安全截断 + 语义级低价值过滤。"""

from unittest import TestCase

from services import file_understand_service as svc
from services import file_understand_element_vision as ev


class SafeTruncateTests(TestCase):
    def test_no_truncate_when_within_limit(self):
        text = "abc\n![x](https://h/a.png)\n"
        out, truncated = svc._truncate(text, 1000)
        self.assertFalse(truncated)
        self.assertEqual(out, text)

    def test_truncate_on_line_boundary_never_splits_image(self):
        # 构造超限文本：前面长段落，后面图片行。
        head = "\n".join([f"段落{i}" * 20 for i in range(50)])
        img = "![图A](https://h/keep1.png)\n![图B](https://h/keep2.png)"
        text = head + "\n" + img
        limit = len(head) - 100  # 强制截断发生在 head 中间某行边界
        out, truncated = svc._truncate(text, limit)
        self.assertTrue(truncated)
        # 绝不出现被切碎的半截图片语法：所有 ![ 都应配对 ](...)
        # 简化校验：输出里每个 '![' 后面都能找到 '](' 和 ')'
        for seg in out.split("![")[1:]:
            self.assertIn("](", seg)

    def test_truncate_salvages_dropped_images(self):
        # 少量 head + 大段纯文本(会被丢弃) + 末尾图片：预算够补回图片。
        head = "标题\n正文开头\n"
        filler = "\n".join([f"废话段落{i}" * 5 for i in range(60)])  # 大段纯文本
        img = "![补回图](https://h/salvage.png)"
        text = head + filler + "\n" + img
        # limit 留足 head + notice + 图片行，但远小于 filler 总长 → filler 被截、图片补回。
        limit = len(head) + 200
        self.assertGreater(len(text), limit)
        out, truncated = svc._truncate(text, limit)
        self.assertTrue(truncated)
        self.assertIn("https://h/salvage.png", out)


class LowValueCaptionTests(TestCase):
    def test_chart_never_low_value(self):
        self.assertFalse(ev._is_low_value_caption("一张展示数据的示意图。", "chart"))

    def test_vague_caption_is_low_value(self):
        self.assertTrue(ev._is_low_value_caption("一张展示机械设备内部结构的示意图。", "figure"))
        self.assertTrue(ev._is_low_value_caption("背景为蓝色的物体。", "figure"))
        self.assertTrue(ev._is_low_value_caption("可能是某种设备。", "figure"))

    def test_informative_caption_kept(self):
        # 含数字/型号/界面 → 有信息量，保留。
        self.assertFalse(ev._is_low_value_caption("6EVF-80 型号 12V 80Ah 电池。", "figure"))
        self.assertFalse(ev._is_low_value_caption("显示 NVIDIA CUDA 编译器版本信息的终端截图。", "figure"))
        self.assertFalse(ev._is_low_value_caption("电压等级划分表，含 1000V 分界。", "figure"))

    def test_empty_caption_is_low_value(self):
        self.assertTrue(ev._is_low_value_caption("", "figure"))
