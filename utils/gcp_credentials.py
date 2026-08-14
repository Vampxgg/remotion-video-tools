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
import requests as _requests
from google.oauth2 import service_account

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/gcp/credentials.log")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# token 刷新的网络超时（秒）：不设则 oauth2 出网不稳时可能长时间挂起。
_REFRESH_TIMEOUT_SEC = 20.0

_credentials = None
_lock = threading.Lock()
# 刷新锁：并发请求同时发现 token 失效时，只让一个线程真正 refresh，其余复用结果。
_refresh_lock = threading.Lock()

# GCS 上传专用凭证（与"生成"凭证解耦）。
# 背景：GCS 桶 x-pilot-storage 属于主项目 x-pilot-469902，只有它的 SA 有写权限；
# 而出图/veo 生成走新买的 videomaker-endpoint-* 令牌（这些 SA 对该桶无写权限，实测 403）。
# 因此上传必须用一份能写该桶的凭证：优先 GCS_CREDENTIALS_FILE，留空则回退全局
# GCP_CREDENTIALS_FILE / ADC（向后兼容）。
_gcs_credentials = None
_gcs_lock = threading.Lock()
_gcs_refresh_lock = threading.Lock()


class _TimeoutRequest(google.auth.transport.requests.Request):
    """带默认超时的 Request：google.auth 的 Request 构造函数只接受 session，
    超时只能在 __call__ 时传。这里注入默认 timeout，让 creds.refresh() 内部
    每次发起 token 请求都受该超时约束，避免 oauth2 出网挂起长时间阻塞。
    """

    def __init__(self, session=None, timeout: float = _REFRESH_TIMEOUT_SEC):
        super().__init__(session=session)
        self._default_timeout = timeout

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        if timeout is None:
            timeout = self._default_timeout
        return super().__call__(
            url, method=method, body=body, headers=headers, timeout=timeout, **kwargs
        )


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


def _resolve_gcp_proxy() -> Optional[str]:
    """GCP/oauth2 token 刷新的出网代理。

    与 gemini_vertex_client 的 Vertex 代理保持同源：国内服务器直连
    ``oauth2.googleapis.com`` 会超时，token 刷新必须与 generateContent 走同一代理，
    否则"能连 Vertex 但取不到 token"，鉴权照样雪崩。
    优先 FILE_UNDERSTAND_VERTEX_PROXY_URL，回落 OUTBOUND_PROXY_URL；都空=直连。
    """
    for name in ("FILE_UNDERSTAND_VERTEX_PROXY_URL", "OUTBOUND_PROXY_URL"):
        val = getattr(_settings, name, None)
        if val and str(val).strip():
            return str(val).strip()
    return None


def _build_auth_request() -> google.auth.transport.requests.Request:
    """构造 token 刷新用的 Request，底层 requests.Session 强制 trust_env=False。

    根因防护：进程内任何地方一旦设置了 HTTP(S)_PROXY 环境变量，google.auth 默认会
    读它去连 oauth2.googleapis.com。这里显式屏蔽环境代理（trust_env=False），出网策略
    完全由 _resolve_gcp_proxy() 显式控制：配置了代理就走代理（国内机必需），否则直连。
    """
    session = _requests.Session()
    session.trust_env = False
    proxy = _resolve_gcp_proxy()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return _TimeoutRequest(session=session, timeout=_REFRESH_TIMEOUT_SEC)


def _refresh_token(force: bool = False) -> str:
    creds = get_gcp_credentials()
    # 刷新锁内二次检查：并发线程只让第一个真正 refresh，其余复用刚刷新的 token。
    if force or not creds.valid:
        with _refresh_lock:
            if force or not creds.valid:
                logger.info("access token 失效/缺失，执行 refresh…（force=%s）", force)
                creds.refresh(_build_auth_request())
                logger.info(
                    f"access token 已刷新 (expiry={getattr(creds, 'expiry', None)} "
                    f"sa={getattr(creds, 'service_account_email', None)})"
                )
    if not creds.token:
        raise RuntimeError("凭证刷新后仍无 token")
    return creds.token


async def get_access_token(force_refresh: bool = False) -> str:
    """获取 cloud-platform access token；refresh 为阻塞调用，放线程池执行。

    :param force_refresh: 强制刷新（用于 401 后重取，规避 token 过期误判）。
    """
    try:
        return await asyncio.to_thread(_refresh_token, force_refresh)
    except Exception as e:  # noqa: BLE001
        logger.error(f"获取 GCP access token 失败: {e}")
        raise RuntimeError(f"Failed to obtain GCP access token: {e}") from e


# =====================================================================
# GCS 上传专用凭证（存储与生成解耦）
# =====================================================================

def _load_gcs_credentials():
    """加载 GCS 上传凭证：优先 GCS_CREDENTIALS_FILE，留空回退全局兜底凭证。

    回退到 get_gcp_credentials() 而非 ADC，保证"未单独配置 GCS 凭证"时行为与改造前
    完全一致（历史上传就是用全局兜底凭证）。
    """
    raw = (getattr(_settings, "GCS_CREDENTIALS_FILE", None) or "").strip()
    if raw:
        path = _resolve_path(raw)
        if not path.is_file():
            raise FileNotFoundError(
                f"GCS_CREDENTIALS_FILE 指向的服务账号文件不存在: {path}"
            )
        creds = service_account.Credentials.from_service_account_file(
            str(path), scopes=_SCOPES
        )
        logger.info(f"已加载 GCS 上传专用凭证: {path} (project={creds.project_id})")
        return creds
    logger.info("未配置 GCS_CREDENTIALS_FILE，GCS 上传回退全局兜底凭证。")
    return get_gcp_credentials()


def get_gcs_credentials():
    """返回缓存的 GCS 上传凭证对象（首次调用时加载）。线程安全。"""
    global _gcs_credentials
    if _gcs_credentials is None:
        with _gcs_lock:
            if _gcs_credentials is None:
                _gcs_credentials = _load_gcs_credentials()
    return _gcs_credentials


def _refresh_gcs_token(force: bool = False) -> str:
    creds = get_gcs_credentials()
    if force or not creds.valid:
        with _gcs_refresh_lock:
            if force or not creds.valid:
                creds.refresh(_build_auth_request())
                logger.info(
                    f"GCS 上传 access token 已刷新 (expiry={getattr(creds, 'expiry', None)} "
                    f"sa={getattr(creds, 'service_account_email', None)})"
                )
    if not creds.token:
        raise RuntimeError("GCS 凭证刷新后仍无 token")
    return creds.token


async def get_gcs_access_token(force_refresh: bool = False) -> str:
    """获取 GCS 上传用 access token（与生成凭证 get_access_token 解耦）。"""
    try:
        return await asyncio.to_thread(_refresh_gcs_token, force_refresh)
    except Exception as e:  # noqa: BLE001
        logger.error(f"获取 GCS access token 失败: {e}")
        raise RuntimeError(f"Failed to obtain GCS access token: {e}") from e
