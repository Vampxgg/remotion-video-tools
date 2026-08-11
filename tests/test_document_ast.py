# -*- coding: utf-8 -*-
"""文档 AST 抽取：表格锚点解析、低置信度判定、build_document_ast 组装与对齐。"""

from unittest import TestCase
from unittest.mock import patch

from services import document_ast_service as ast_svc
from services.file_understand_service import _anchor_tables


class TableLowConfidenceTests(TestCase):
    def test_regular_table_high_confidence(self):
        md = "| a | b |\n| - | - |\n| 1 | 2 |"
        self.assertFalse(ast_svc._table_is_low_confidence(md))

    def test_empty_cells_low_confidence(self):
        # 大量空单元格 -> 低置信度，需要视觉校对。
        md = "| a | b | c |\n| - | - | - |\n|  |  |  |\n|  | 1 |  |"
        self.assertTrue(ast_svc._table_is_low_confidence(md))

    def test_ragged_columns_low_confidence(self):
        md = "| a | b | c |\n| - | - | - |\n| 1 | 2 |"  # 列数不齐
        self.assertTrue(ast_svc._table_is_low_confidence(md))

    def test_single_row_low_confidence(self):
        self.assertTrue(ast_svc._table_is_low_confidence("| only |"))


class AnchoredTablesParsingTests(TestCase):
    def test_count_anchored_tables_roundtrip(self):
        base = "前言\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n中段\n\n| x |\n| - |\n| 9 |"
        anchored, n = _anchor_tables(base)
        self.assertEqual(n, 2)
        parsed = ast_svc._count_anchored_tables(anchored)
        self.assertEqual([a for a, _ in parsed], ["1", "2"])
        self.assertIn("| 1 | 2 |", parsed[0][1])


class BuildAstTests(TestCase):
    def test_images_use_url_as_anchor_and_bytes(self):
        base = "![](https://cdn/a.png)\n\n![](https://cdn/b.png)"
        anchored, n = _anchor_tables(base)
        ast = ast_svc.build_document_ast(
            content=b"", ext=".png", base_markdown=base, anchored_markdown=anchored,
            n_tables=n, embedded_images=[("a", b"AA", "image/png"), ("b", b"BB", "image/png")],
        )
        self.assertEqual([im.url for im in ast.images], ["https://cdn/a.png", "https://cdn/b.png"])
        self.assertEqual(ast.images[0].data, b"AA")

    def test_tables_marked_low_confidence(self):
        base = "| a | b |\n| - | - |\n|  |  |"  # 低置信度
        anchored, n = _anchor_tables(base)
        # 非 pdf/office 且无裁剪，crop 为空；仍应标 low_confidence。
        ast = ast_svc.build_document_ast(
            content=b"", ext=".png", base_markdown=base, anchored_markdown=anchored, n_tables=n,
        )
        self.assertEqual(len(ast.tables), 1)
        self.assertTrue(ast.tables[0].low_confidence)
        self.assertIsNone(ast.tables[0].crop_png)

    def test_crop_alignment_mismatch_degrades(self):
        base = "| a |\n| - |\n| 1 |\n\n| b |\n| - |\n| 2 |"  # 两个表
        anchored, n = _anchor_tables(base)
        # 模拟 find_tables 只裁到 1 个表 -> 数量不一致 -> 退化不对齐（crop 全空 + 警告）。
        with patch.object(ast_svc, "_pdf_bytes_from", return_value=(b"%PDF", None)), \
             patch.object(ast_svc, "_crop_tables_from_pdf", return_value=[([0, 0, 1, 1], b"PNG", 1)]), \
             patch.object(ast_svc._settings, "FILE_UNDERSTAND_TABLE_VISION_ENABLED", True):
            ast = ast_svc.build_document_ast(
                content=b"%PDF", ext=".pdf", base_markdown=base, anchored_markdown=anchored, n_tables=n,
            )
        self.assertEqual(len(ast.tables), 2)
        self.assertTrue(all(t.crop_png is None for t in ast.tables))
        self.assertTrue(any("不一致" in w for w in ast.warnings))

    def test_crop_alignment_match_attaches_png(self):
        base = "| a |\n| - |\n| 1 |\n\n| b |\n| - |\n| 2 |"
        anchored, n = _anchor_tables(base)
        with patch.object(ast_svc, "_pdf_bytes_from", return_value=(b"%PDF", None)), \
             patch.object(ast_svc, "_crop_tables_from_pdf", return_value=[
                 ([0, 0, 1, 1], b"PNG1", 1), ([0, 2, 1, 3], b"PNG2", 1),
             ]), \
             patch.object(ast_svc._settings, "FILE_UNDERSTAND_TABLE_VISION_ENABLED", True):
            ast = ast_svc.build_document_ast(
                content=b"%PDF", ext=".pdf", base_markdown=base, anchored_markdown=anchored, n_tables=n,
            )
        self.assertEqual(ast.tables[0].crop_png, b"PNG1")
        self.assertEqual(ast.tables[1].crop_png, b"PNG2")
