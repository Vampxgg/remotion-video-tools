# -*- coding: utf-8 -*-
"""全项目唯一的代理策略入口。

设计目标（核心约束）：
- **绝不写全局 ``os.environ['HTTP(S)_PROXY']``**。历史上多个 router 在 import 时
  写全局代理环境变量，会污染整个进程里所有 ``trust_env=True`` 的 HTTP 客户端
  （包括 ``google.auth`` 的 token 刷新），导致「配了 clash 代理 → GCP 鉴权也被
  强制走代理 → 代理不可达时全线报错」。
- 代理只由 ``.env`` 显式配置，且只作用到「需要它的那个 client」，不外溢。

用法：
- ``resolve_proxy(*names)``：按给定的 settings 字段名顺序取第一个非空代理 URL，
  取不到返回 ``None``（= 直连）。所有出网 client 统一用它拿代理。
- 创建 httpx client 时统一传 ``trust_env=False``（屏蔽进程环境变量），
  再显式把 ``resolve_proxy(...)`` 的结果传给 ``proxy=``。
- Fish Audio SDK 的 ``Session`` 内部自建 httpx client 且 ``trust_env=True`` 且不
  接受 proxy 参数，用 ``apply_proxy_to_fish_session()`` 就地替换其内部 client，
  使其既不吃全局环境变量、又能按需走显式代理。
"""

from __future__ import annotations

from typing import Optional

import httpx

from utils.settings import settings as _settings


def resolve_proxy(*setting_names: str) -> Optional[str]:
    """按字段名顺序返回第一个非空代理 URL；都为空则返回 None（直连）。

    例如 ``resolve_proxy("TTS_PROXY_URL", "OUTBOUND_PROXY_URL")`` 保持了
    「模块专属代理 → 全局兜底代理 → 直连」的既有优先级。
    """
    for name in setting_names:
        value = getattr(_settings, name, None)
        if value and str(value).strip():
            return str(value).strip()
    return None


def apply_proxy_to_fish_session(session, proxy_url: Optional[str]):
    """就地替换 Fish ``Session`` 的内部 httpx client，使代理只作用于该 session。

    Fish SDK 的 ``RemoteCall`` 用 ``httpx.Client/AsyncClient`` 且默认
    ``trust_env=True``、不暴露 proxy 入参。这里重建其 ``_sync_client`` /
    ``_async_client``，统一 ``trust_env=False``（不吃进程环境变量），仅当显式配置了
    ``proxy_url`` 时才走代理。保留原 base_url 与鉴权/UA 头，不改变其余行为。

    返回传入的 session，便于链式调用。
    """
    base_url = getattr(session, "_base_url", "https://api.fish.audio")
    apikey = getattr(session, "_apikey", None)
    headers = {
        "Authorization": f"Bearer {apikey}",
        "User-Agent": "fish-audio/python/legacy",
    }

    old_sync = getattr(session, "_sync_client", None)
    if old_sync is not None and not old_sync.is_closed:
        old_sync.close()
    session._sync_client = httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=None,
        trust_env=False,
        proxy=proxy_url,
    )

    old_async = getattr(session, "_async_client", None)
    if old_async is not None:
        session._async_client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=None,
            trust_env=False,
            proxy=proxy_url,
        )

    return session
