# -*- coding: utf-8 -*-
"""多模态理解的 provider 无关契约与错误分类。

编排层（file_understand_service）只依赖本模块定义的请求/结果/错误抽象，具体的
Vertex Gemini / Azure VLM 调用由各自 adapter 实现。这样：

- 主/备 provider 可以互换、可以按错误类型决定"重试本 provider"还是"切下一 provider"；
- 错误被归一成有限几类语义，编排层不需要认识 httpx / google.auth 的底层异常；
- 新增 provider 只需实现一个 ``generate`` 方法并声明能力，不用改编排层。

设计取舍：不引入过度抽象。请求只承载"视觉输入 + 文本 + 结构化 schema"这几样编排层
真正需要的东西；能力声明只保留决定 fallback 分支所必需的字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# --------------------------- 错误分类 ---------------------------
#
# 编排层据此决定：可重试(本 provider 换区/退避) / 切下一 provider / 直接失败。


class ProviderError(Exception):
    """所有 provider 抛出的错误基类。"""


class ProviderAuthError(ProviderError):
    """鉴权失败（token 刷新失败、401/403）。切下一 provider（异构鉴权可绕开）。"""


class ProviderRateLimitError(ProviderError):
    """限流（429）。可短暂退避重试本 provider，耗尽后切下一 provider。"""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class ProviderUnavailableError(ProviderError):
    """服务端不可用（5xx）。切下一 provider。"""


class ProviderTimeoutError(ProviderError):
    """网络超时/断连。切下一 provider（异构网络可绕开）。"""


class ProviderUnsupportedInputError(ProviderError):
    """provider 不支持该输入（如原生不吃 PDF、超出 max_input_bytes）。切下一 provider，但不算故障。"""


class ProviderInvalidResponseError(ProviderError):
    """响应非法（空、无 candidates、JSON 解析失败）。可重试一次，再切下一 provider。"""


class ProviderRequestError(ProviderError):
    """确定性请求错误（4xx 非 429/401/403，如 schema/参数错误）。不重试、不盲目切换。"""


# --------------------------- 领域数据结构 ---------------------------


@dataclass(frozen=True)
class VisualDocument:
    """一份要做视觉理解的原始输入。

    - ``data`` + ``mime_type``：原始文件字节（PDF/图片）。provider 若原生支持该 mime
      则直接发送；不支持（如 Azure 不吃 PDF）则用 ``rendered_pages`` 页图。
    - ``rendered_pages``：可选的按页渲染 PNG 列表，供不支持原生 PDF 的 provider 使用。
    """

    data: bytes
    mime_type: str
    filename: str = ""
    rendered_pages: Optional[List[bytes]] = None


@dataclass(frozen=True)
class UnderstandGenerationRequest:
    """一次 provider 调用请求（provider 无关）。"""

    document: VisualDocument
    system_instruction: str
    user_text: str
    response_schema: Optional[Dict[str, Any]] = None
    temperature: float = 0.2
    max_output_tokens: int = 8192
    request_id: str = "-"
    # 本次调用允许消耗的墙钟预算（秒）。None=用 provider 默认超时。
    deadline_sec: Optional[float] = None


@dataclass
class UnderstandGenerationResult:
    """一次 provider 调用的归一化结果。"""

    text: str
    parsed_json: Optional[Dict[str, Any]]
    provider: str
    model: str
    finish_reason: Optional[str] = None
    attempts: int = 1
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderCapabilities:
    """provider 能力声明，供编排层决定输入编码与是否可用。"""

    name: str
    native_pdf: bool
    image_mime_types: frozenset
    json_object: bool
    json_schema: bool
    # 单请求最大输入字节（近似，用于超限时切换）。0=不限。
    max_input_bytes: int = 0
    # 页图模式下单请求最多携带的页数（0=不分批）。
    max_pages_per_request: int = 0
