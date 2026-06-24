# -*- coding: utf-8 -*-
"""文件解析服务的对外响应 schema。"""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class FileParseMode(str, Enum):
    MARKDOWN = "markdown"
    TEXT = "text"
    STRUCTURED = "structured"


class FileParseFileInfo(BaseModel):
    filename: str
    extension: str
    size: int
    media_type: Optional[str] = None


class FileParseContent(BaseModel):
    markdown: Optional[str] = None
    text: Optional[str] = None
    char_count: int = 0
    truncated: bool = False


class FileParseParserInfo(BaseModel):
    content_kind: str
    parser_used: str = "DocumentParserService"
    fallback_used: Optional[bool] = None


class FileParseError(BaseModel):
    code: str
    detail: str


class FileParseResult(BaseModel):
    status: str = Field("ok", description="ok 或 failed")
    file: FileParseFileInfo
    content: FileParseContent
    parser: FileParseParserInfo
    meta: Dict[str, Any] = Field(default_factory=dict)
    assets: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    error: Optional[FileParseError] = None


class FileParseBatchSummary(BaseModel):
    requested: int
    ok: int
    failed: int
    partial_success: bool


class FileParseBatchData(BaseModel):
    summary: FileParseBatchSummary
    items: List[FileParseResult]
