# -*- coding: utf-8 -*-
# @File：utils/responses.py
"""
全项目统一的"标准响应"模型与工厂函数。

历史代码中 10 处 ``api/*.py`` 各自定义了一份完全相同的 ``StandardResponse``/
``create_standard_response``，仅在文档字符串细节上偶有差异。本模块抽出唯一定义，
其余文件统一通过 ``from utils.responses import StandardResponse, create_standard_response`` 引用，
对外接口契约（HTTP status / JSON 字段顺序 / 字段含义）保持完全一致。
"""

from datetime import datetime
from typing import Any, Optional

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class StandardResponse(BaseModel):
    """标准的 API 响应模型（与历史各 router 中的同名类完全等价）。"""

    code: int = Field(200, description="HTTP状态码")
    message: str = Field("Success", description="响应消息")
    data: Optional[Any] = Field(None, description="响应数据")
    timestamp: str = Field(..., description="ISO 8601 格式的时间戳")


def create_standard_response(
    data: Optional[Any] = None,
    code: int = 200,
    message: str = "Success",
    *,
    exclude_none: bool = False,
) -> JSONResponse:
    """创建一个标准格式的 FastAPI 响应。

    与历史 ``api/*.py`` 中各自定义的同名函数行为完全一致：
    - HTTP status_code = code
    - JSON body = StandardResponse(code, message, data, now().isoformat()).model_dump(...)
    - ``exclude_none`` 仅供 ``api/cre_image.py`` 等保留 ``model_dump(exclude_none=True)`` 历史
      行为的 router 显式启用，默认 False 与其余 9 处保持一致。
    """
    content = StandardResponse(
        code=code,
        message=message,
        data=data,
        timestamp=datetime.now().isoformat(),
    ).model_dump(exclude_none=exclude_none)
    return JSONResponse(status_code=code, content=content)


__all__ = ["StandardResponse", "create_standard_response"]
