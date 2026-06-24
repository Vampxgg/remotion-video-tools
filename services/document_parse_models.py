# -*- coding: utf-8 -*-
"""核心文档解析的内部结构化结果模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DocumentParseWarning:
    code: str
    message: str
    level: str = "warning"

    def to_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "level": self.level,
        }


@dataclass
class DocumentAsset:
    filename: str
    mime_type: str
    size: int
    status: str = "extracted"
    url: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "status": self.status,
            "url": self.url,
            "error": self.error,
        }


@dataclass
class DocumentParseResult:
    markdown: str
    content_kind: str
    parser_used: str = "DocumentParserService"
    fallback_used: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)
    warnings: List[DocumentParseWarning] = field(default_factory=list)
    assets: List[DocumentAsset] = field(default_factory=list)
    truncated: bool = False

    def warning_messages(self) -> List[str]:
        return [warning.message for warning in self.warnings]

    def assets_as_dicts(self) -> List[Dict[str, Any]]:
        return [asset.to_dict() for asset in self.assets]
