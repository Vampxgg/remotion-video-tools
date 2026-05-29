# -*- coding: utf-8 -*-
"""FastAPI 通用安全依赖工厂。

把分散在各 router 中"x-api-key 守卫"的重复实现收敛到一处。
- 行为完全等价于 ``api/jobs_region.py`` 老 ``require_api_key``：
  - 配置侧 ``setting_attr`` 对应的字段为 ``None`` / 空字符串 → 直接放行（不启用鉴权）
  - 字段有值 → 必须 header ``x-api-key`` 完全匹配，否则 401

用法：

    from utils.security import require_api_key
    from fastapi import APIRouter, Depends

    router = APIRouter(dependencies=[Depends(require_api_key("WEB_SEARCH_API_KEY"))])
"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import Header, HTTPException, status

from utils.settings import settings as _settings


def require_api_key(setting_attr: str) -> Callable:
    """构造一个 FastAPI 依赖：按 settings 上指定字段做 x-api-key 校验。

    Parameters
    ----------
    setting_attr:
        ``utils.settings.Settings`` 上某个字段的名字（例如 ``"WEB_SEARCH_API_KEY"`` /
        ``"REGION_JOBS_API_KEY"``）。
    """

    async def _dep(x_api_key: Optional[str] = Header(None)) -> None:
        expected = getattr(_settings, setting_attr, None)
        if not expected:
            # 未配置 = 不启用鉴权，直接放行（与历史 router 行为一致）
            return
        if x_api_key != expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )

    # 给依赖函数挂个可读名，方便 OpenAPI 文档展示
    _dep.__name__ = f"require_api_key_{setting_attr.lower()}"
    return _dep
