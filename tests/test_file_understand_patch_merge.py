# -*- coding: utf-8 -*-
"""补丁本地合并与图片白名单校正：表格替换、图表转表、幻觉链接剔除、源图补回。"""

from unittest import TestCase

from services import file_understand_service as fus


class PatchMergeTests(TestCase):
    def test_table_patch_replaces_anchored_table(self):
        base = "前言\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n后文"
        anchored, n = fus._anchor_tables(base)
        self.assertEqual(n, 1)
        patches = {"tables": [{"anchor": "1", "markdown": "| a | b |\n| - | - |\n| 10 | 20 |"}], "images": []}
        merged, stats = fus._apply_patches(anchored, patches)
        self.assertIn("| 10 | 20 |", merged)
        self.assertNotIn("| 1 | 2 |", merged)
        self.assertEqual(stats["tables"], 1)

    def test_chart_image_inserts_table_after(self):
        base = "![旧](https://cdn/chart.png)"
        anchored, _ = fus._anchor_tables(base)
        patches = {
            "tables": [],
            "images": [{
                "url": "https://cdn/chart.png", "kind": "chart",
                "caption": "季度营收", "table_markdown": "| Q | 值 |\n| - | - |\n| Q1 | 5 |",
            }],
        }
        merged, stats = fus._apply_patches(anchored, patches)
        self.assertIn("![季度营收](https://cdn/chart.png)", merged)
        self.assertIn("| Q1 | 5 |", merged)
        self.assertEqual(stats["charts"], 1)

    def test_reconcile_drops_hallucinated_url(self):
        base = "![真图](https://cdn/real.png)"
        enriched = "![真图](https://cdn/real.png)\n\n![假图](https://img.example.com/fake.png)"
        fixed, stats = fus._reconcile_images(enriched, base)
        self.assertIn("https://cdn/real.png", fixed)
        self.assertNotIn("img.example.com", fixed)
        self.assertEqual(stats["fake_dropped"], 1)

    def test_reconcile_reappends_missing_source_image(self):
        base = "![a](https://cdn/a.png)\n\n![b](https://cdn/b.png)"
        enriched = "![a](https://cdn/a.png)"  # 丢了 b
        fixed, stats = fus._reconcile_images(enriched, base)
        self.assertIn("https://cdn/b.png", fixed)
        self.assertEqual(stats["reappended"], 1)

    def test_reconcile_fixes_corrupted_url(self):
        base = "![a](https://cdn/abcdefghijklmn.png)"
        # 模型把 URL 改写了一个字符（高度相似）。
        enriched = "![a](https://cdn/abcdefghijklmX.png)"
        fixed, stats = fus._reconcile_images(enriched, base)
        self.assertIn("https://cdn/abcdefghijklmn.png", fixed)
        self.assertEqual(stats["corrupted_fixed"], 1)
