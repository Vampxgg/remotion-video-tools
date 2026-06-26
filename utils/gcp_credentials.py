# -*- coding: utf-8 -*-
"""GCP / Vertex 凭证的全项目唯一入口。

把原先散落在 ``cre_image`` / ``cre_video`` / ``gemini_vertex_client`` 里各写一套的
``google.auth.default()`` 鉴权收敛到此处，便于统一切换服务账号：

- 若配置了 ``settings.GCP_CREDENTIALS_FILE`` → 显式从该服务账号 JSON 加载凭证
  （相对路径按项目根解析）。
- 否则回退 ``google.auth.default()``（用户级 ADC / 环境变量），保持向后兼容。

凭证对象进程内缓存一次；token 过期时 ``refresh`` 会就地续期。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Optional

import google.auth
import google.auth.transport.requests
from google.oauth2 import service_account

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/gcp/credentials.log")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

_credentials = None
_lock = threading.Lock()


def _resolve_path(raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return p


def _load_credentials():
    raw = (_settings.GCP_CREDENTIALS_FILE or "").strip()
    if raw:
        path = _resolve_path(raw)
        if not path.is_file():
            raise FileNotFoundError(
                f"GCP_CREDENTIALS_FILE 指向的服务账号文件不存在: {path}"
            )
        creds = service_account.Credentials.from_service_account_file(
            str(path), scopes=_SCOPES
        )
        logger.info(f"已加载服务账号凭证: {path} (project={creds.project_id})")
        return creds
    creds, project = google.auth.default(scopes=_SCOPES)
    logger.info(f"未配置 GCP_CREDENTIALS_FILE，回退 ADC default (project={project})")
    return creds


def get_gcp_credentials():
    """返回缓存的凭证对象（首次调用时加载）。线程安全。"""
    global _credentials
    if _credentials is None:
        with _lock:
            if _credentials is None:
                _credentials = _load_credentials()
    return _credentials


def _refresh_token() -> str:
    creds = get_gcp_credentials()
    if not creds.valid:
        logger.info("access token 失效/缺失，执行 refresh…")
        creds.refresh(google.auth.transport.requests.Request())
        logger.info(
            f"access token 已刷新 (expiry={getattr(creds, 'expiry', None)} "
            f"sa={getattr(creds, 'service_account_email', None)})"
        )
    if not creds.token:
        raise RuntimeError("凭证刷新后仍无 token")
    return creds.token


async def get_access_token() -> str:
    """获取 cloud-platform access token；refresh 为阻塞调用，放线程池执行。"""
    try:
        return await asyncio.to_thread(_refresh_token)
    except Exception as e:  # noqa: BLE001
        logger.error(f"获取 GCP access token 失败: {e}")
        raise RuntimeError(f"Failed to obtain GCP access token: {e}") from e
