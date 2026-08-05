# -*- coding: utf-8 -*-
"""文档结构化解析（manifest）的对外响应 schema。

产物结构仿 Genspark ``import_pdf``：把文档拆成"每页高清 PNG + 该页文本层 +
该页内嵌图（带 bbox / VLM 描述 / 关键词）"，供下游多模态 workflow：
  - 每页 PNG 交给 VLM「看版式/图表」；
  - 文本层交给检索/大纲；
  - 内嵌图（真实公网 URL）可被最终 Markdown 直接复用 ``![desc](url)``。

本 schema 是前后端定死的稳定契约，内部实现（渲染 DPI、抽图算法、VLM 打标策略）
可以持续增强而不破坏契约。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DocImageType(str):
    """内嵌图语义类型（字符串枚举，便于向前兼容新增类型）。"""

    CHART = "chart"
    PHOTO = "photo"
    DIAGRAM = "diagram"
    SCREENSHOT = "screenshot"
    UNKNOWN = "unknown"


class ManifestImage(BaseModel):
    """一张页面内嵌图。"""

    img_url: Optional[str] = Field(None, description="抽取的内嵌图公网 URL，可被 Markdown 直接复用；上传失败为 None")
    filename: str = Field(..., description="内嵌图原始/生成文件名，用于排序与去重")
    mime_type: str = Field("image/png", description="图片 MIME")
    size: int = Field(0, description="图片字节数")
    bbox: Optional[List[float]] = Field(
        None, description="图片在页面中的位置 [x0, y0, x1, y1]（PDF 坐标，左上原点）"
    )
    width: Optional[int] = Field(None, description="图片像素宽")
    height: Optional[int] = Field(None, description="图片像素高")
    img_description: Optional[str] = Field(
        None, description="VLM 生成的图片语义描述（供检索与图文匹配）；未打标为 None"
    )
    img_keywords: List[str] = Field(default_factory=list, description="VLM 抽取的关键词，供召回")
    img_type: str = Field(DocImageType.UNKNOWN, description="chart|photo|diagram|screenshot|unknown")
    chart_table_markdown: Optional[str] = Field(
        None, description="当 img_type=chart 时，图表数值转写的 Markdown 表格；否则 None"
    )
    upload_status: str = Field("extracted", description="extracted|uploaded|skipped|failed")
    upload_error: Optional[str] = Field(None, description="上传失败原因")


class ManifestPage(BaseModel):
    """一页的结构化产物。"""

    index: int = Field(..., description="页码，从 1 开始")
    page_png_url: Optional[str] = Field(
        None, description="该页高清渲染 PNG 的公网 URL；渲染/上传失败为 None"
    )
    page_width: Optional[int] = Field(None, description="渲染 PNG 像素宽")
    page_height: Optional[int] = Field(None, description="渲染 PNG 像素高")
    text: str = Field("", description="该页文本层纯文本（供检索/大纲）")
    images: List[ManifestImage] = Field(default_factory=list, description="该页内嵌图列表")


class ManifestSource(BaseModel):
    """源文件元信息。"""

    name: str = Field(..., description="源文件名")
    mime: Optional[str] = Field(None, description="源文件 MIME")
    ext: str = Field(..., description="源文件扩展名（含点，如 .pdf）")
    size: int = Field(0, description="源文件字节数")
    page_count: int = Field(0, description="总页数")
    converted_from: Optional[str] = Field(
        None, description="若经 LibreOffice 转 PDF，这里记录原始扩展名（如 .pptx）"
    )


class DocumentManifest(BaseModel):
    """/parse/document 的核心产物（manifest）。"""

    doc_id: str = Field(..., description="幂等文档 ID（同 file_url + 选项 => 同 doc_id）")
    source: ManifestSource
    pages: List[ManifestPage] = Field(default_factory=list)
    assets_base: Optional[str] = Field(None, description="资产（页图/内嵌图）公网前缀，便于排障")
    meta: Dict[str, Any] = Field(default_factory=dict, description="解析过程元信息（DPI、耗时、限页等）")
    warnings: List[str] = Field(default_factory=list, description="降级/截断/未打标等告警")


class DocumentParseOptions(BaseModel):
    """/parse/document 的可选项（内部实现可增强，字段保持向前兼容）。"""

    dpi: int = Field(150, ge=72, le=300, description="每页 PNG 渲染 DPI")
    extract_images: bool = Field(True, description="是否抽取页面内嵌图")
    vlm_caption: bool = Field(True, description="是否为内嵌图做 VLM 语义打标（img_description/keywords/type）")
    vlm_chart_to_table: bool = Field(
        True, description="打标时若判定为 chart，是否顺带把数值转写成 Markdown 表格"
    )
    max_pages: Optional[int] = Field(None, description="最大处理页数；None 用服务端默认")
    min_img_bytes: Optional[int] = Field(None, description="内嵌图最小字节阈值；None 用服务端默认")
    min_img_dim: Optional[int] = Field(None, description="内嵌图最小边像素阈值；None 用服务端默认")


class DocumentParseRequest(BaseModel):
    """JSON 入口（传 file_url）。也支持 multipart 直接上传文件（见路由）。"""

    file_url: Optional[str] = Field(None, description="源文件公网 URL")
    file_id: Optional[str] = Field(None, description="对象存储内部 file_id（与 file_url 二选一）")
    filename: Optional[str] = Field(None, description="覆盖文件名（决定扩展名/解析器选择）")
    mime: Optional[str] = Field(None, description="源文件 MIME")
    options: DocumentParseOptions = Field(default_factory=DocumentParseOptions)
