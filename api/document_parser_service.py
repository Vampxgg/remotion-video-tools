# -*- coding: utf-8 -*-
"""兼容导出：核心文档解析实现已移动到 services 层。

新代码请直接引用 ``services.document_parser_service``。保留本模块是为了避免
历史调用方在迁移期间因 ``api.document_parser_service`` 导入路径中断。
"""

from services.document_parser_service import (  # noqa: F401
    DataCleaningPipeline,
    DocumentParserService,
    EmbeddedImageUploader,
)

__all__ = [
    "EmbeddedImageUploader",
    "DataCleaningPipeline",
    "DocumentParserService",
]
