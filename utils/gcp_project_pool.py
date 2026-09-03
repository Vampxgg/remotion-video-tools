# -*- coding: utf-8 -*-
"""GCP "生成"端点池路由层（Vertex 出图 cre_image / veo 视频 cre_video 共享）。

背景/根因：``cre_image`` / ``cre_video`` 原先全程只用单个 GCP 项目 + 单缓存凭证。
Vertex 配额**按项目**计，单项目配额被打满/抖动即 429/5xx，且现有"多区域轮询"因所有
区域共享同一项目配额而无法缓解。

本模块把**生成**流量分散到多个**独立 GCP 项目**（各自独立 Vertex 配额池）：

- 每个项目一个 ``GcpProjectRuntime``：持有该项目的 service_account 凭证、按项目独立
  刷新/缓存 access token、并发信号量、在途计数、连续失败计数与熔断截止时间。
- ``GcpProjectRouter`` 负责组池、选路（挑最空闲且未熔断的项目）、成功/失败回写熔断，
  并持有池级策略（熔断阈值/冷却/最大尝试次数，来自 YAML defaults）。

关键约束：**Vertex 调用的 project 必须与 access token 的 service account 项目一致**。
调用方必须成对使用 ``runtime.project_id``（拼进 endpoint）与
``runtime.get_access_token()``（该项目 SA 的 token）。

职责边界：本池只管**生成**；结果写入 GCS 桶用的是独立的 GCS 上传凭证
（见 utils/gcp_credentials.get_gcs_access_token），两者解耦。

配置来源（见 build_router）：默认从 ``secrets/gcp-endpoints.yaml`` 加载；文件缺失/
projects 为空 → 返回空池，调用方回退单项目行为（向后兼容、零破坏）。

凭证刷新的出网策略（trust_env=False + 可选代理 + 超时）与 ``utils/gcp_credentials.py``
保持一致，避免"能连 Vertex 但取不到 token"的鉴权雪崩。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import google.auth.transport.requests
import requests as _requests
import yaml
from google.oauth2 import service_account

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/gcp/project_pool.log")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
# token 刷新的网络超时（秒）：与 gcp_credentials 一致，防 oauth2 出网挂起长时间阻塞。
_REFRESH_TIMEOUT_SEC = 20.0

# 池级策略默认值（YAML defaults 缺省时使用）。
_DEFAULT_MAX_CONCURRENCY = 5
_DEFAULT_CIRCUIT_THRESHOLD = 3
_DEFAULT_CIRCUIT_COOLDOWN = 60.0
_DEFAULT_MAX_ATTEMPTS = 3


class _TimeoutRequest(google.auth.transport.requests.Request):
    """带默认超时的 Request（与 utils/gcp_credentials._TimeoutRequest 同源）。"""

    def __init__(self, session=None, timeout: float = _REFRESH_TIMEOUT_SEC):
        super().__init__(session=session)
        self._default_timeout = timeout

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        if timeout is None:
            timeout = self._default_timeout
        return super().__call__(
            url, method=method, body=body, headers=headers, timeout=timeout, **kwargs
        )


def _resolve_gcp_proxy() -> Optional[str]:
    """token 刷新出网代理：与 gcp_credentials 同源。

    优先 FILE_UNDERSTAND_VERTEX_PROXY_URL，回落 OUTBOUND_PROXY_URL；都空=直连。
    """
    for name in ("FILE_UNDERSTAND_VERTEX_PROXY_URL", "OUTBOUND_PROXY_URL"):
        val = getattr(_settings, name, None)
        if val and str(val).strip():
            return str(val).strip()
    return None


def _build_auth_request() -> google.auth.transport.requests.Request:
    """构造 token 刷新用 Request；底层 requests.Session 强制 trust_env=False。"""
    session = _requests.Session()
    session.trust_env = False
    proxy = _resolve_gcp_proxy()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return _TimeoutRequest(session=session, timeout=_REFRESH_TIMEOUT_SEC)


def _resolve_path(raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return p


def _coerce_positive_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, default)


def _coerce_positive_float(value, default: float, floor: float = 1.0) -> float:
    try:
        return max(floor, float(value))
    except (TypeError, ValueError):
        return max(floor, default)


def _coerce_bool(value, default: bool = True) -> bool:
    """解析 YAML 布尔：缺省 default；显式 false/no/0/off/"" 记为 False，其余记为 True。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "no", "0", "off", "")


# 生成能力标注取值：出图 / veo 视频 / omni 视频。用于按能力过滤项目池。
CAPABILITY_IMG = "img"
CAPABILITY_VEO = "veo"
CAPABILITY_OMNI = "omni"


@dataclass
class PoolPolicy:
    """池级策略（来自 YAML defaults），由 router 持有，替代散落在 settings 的平铺项。"""

    circuit_failure_threshold: int = _DEFAULT_CIRCUIT_THRESHOLD
    circuit_cooldown_seconds: float = _DEFAULT_CIRCUIT_COOLDOWN
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS


@dataclass
class GcpProjectRuntime:
    """单个 GCP 项目的运行时状态：凭证 + token 缓存 + 并发/熔断计数。"""

    name: str
    project_id: str
    credentials_file: str
    weight: int = 1
    max_concurrency: int = _DEFAULT_MAX_CONCURRENCY
    # 能力标注：该项目分别是否具备「出图」「veo 视频」「omni 视频」的生成访问权。缺省都为 True
    # （向后兼容：不写=均参与）。现实中项目能力不均等（都能出图，仅个别能 veo/omni），
    # 用布尔位精确描述，路由时按能力过滤，避免把请求轮到无该能力的项目导致 404。
    is_img: bool = True
    is_veo: bool = True
    is_omni: bool = True

    # 运行时（非入参）
    _credentials: Optional[service_account.Credentials] = field(default=None, init=False)
    _refresh_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    semaphore: asyncio.Semaphore = field(init=False)
    inflight: int = field(default=0, init=False)
    consecutive_failures: int = field(default=0, init=False)
    circuit_until: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.weight = max(1, int(self.weight or 1))
        self.max_concurrency = max(1, int(self.max_concurrency or 1))
        self.semaphore = asyncio.Semaphore(self.max_concurrency)

    def has_capability(self, capability: Optional[str]) -> bool:
        """该项目是否具备指定生成能力。capability 为 None 时不过滤（视为具备）。"""
        if capability is None:
            return True
        if capability == CAPABILITY_IMG:
            return self.is_img
        if capability == CAPABILITY_VEO:
            return self.is_veo
        if capability == CAPABILITY_OMNI:
            return self.is_omni
        raise ValueError(f"未知能力标注: {capability}")

    def _load_credentials(self) -> service_account.Credentials:
        path = _resolve_path(self.credentials_file)
        if not path.is_file():
            raise FileNotFoundError(
                f"生成项目 {self.name} 的服务账号文件不存在: {path}"
            )
        creds = service_account.Credentials.from_service_account_file(
            str(path), scopes=_SCOPES
        )
        # 校验 JSON 内 project_id 与声明一致，避免 token/endpoint 项目错配。
        file_project = getattr(creds, "project_id", None)
        if file_project and self.project_id and file_project != self.project_id:
            logger.warning(
                f"项目 {self.name} 声明 project_id={self.project_id} 与 SA 文件内 "
                f"project_id={file_project} 不一致，以 SA 文件为准。"
            )
            self.project_id = file_project
        elif not self.project_id and file_project:
            self.project_id = file_project
        return creds

    def _refresh_token(self, force: bool = False) -> str:
        if self._credentials is None:
            with self._refresh_lock:
                if self._credentials is None:
                    self._credentials = self._load_credentials()
                    logger.info(
                        f"已加载生成项目凭证 {self.name} (project={self.project_id})"
                    )
        creds = self._credentials
        if force or not creds.valid:
            with self._refresh_lock:
                if force or not creds.valid:
                    creds.refresh(_build_auth_request())
                    logger.info(
                        f"项目 {self.name} access token 已刷新 "
                        f"(expiry={getattr(creds, 'expiry', None)})"
                    )
        if not creds.token:
            raise RuntimeError(f"项目 {self.name} 凭证刷新后仍无 token")
        return creds.token

    async def get_access_token(self, force_refresh: bool = False) -> str:
        """获取该项目 SA 的 cloud-platform access token（refresh 放线程池）。"""
        return await asyncio.to_thread(self._refresh_token, force_refresh)


class GcpProjectRouter:
    """多 GCP 项目路由器：组池 + 选路 + 熔断。进程内单例（每 worker 各持一份）。"""

    def __init__(self, projects: List[GcpProjectRuntime], policy: Optional[PoolPolicy] = None):
        self.projects = projects
        self.policy = policy or PoolPolicy()

    def __len__(self) -> int:
        return len(self.projects)

    def projects_with_capability(self, capability: Optional[str]) -> List[GcpProjectRuntime]:
        """按能力取项目子集（不含健康/熔断过滤）。用于统计与判空。"""
        return [p for p in self.projects if p.has_capability(capability)]

    @property
    def enabled(self) -> bool:
        """池内有 >=1 个项目时才启用多项目路由；否则调用方回退单项目行为。"""
        return len(self.projects) > 0

    def has_capability(self, capability: Optional[str]) -> bool:
        """池内是否至少有一个项目具备该能力。"""
        return any(p.has_capability(capability) for p in self.projects)

    def max_attempts(self, capability: Optional[str] = None) -> int:
        """单次生成请求最多跨项目尝试次数：取 min(具备该能力的项目数, 策略配置)，至少 1。"""
        n = len(self.projects_with_capability(capability))
        return max(1, min(n, max(1, self.policy.max_attempts)))

    def healthy_projects(
        self, excluded: Optional[set] = None, capability: Optional[str] = None
    ) -> List[GcpProjectRuntime]:
        """返回具备指定能力、未熔断、未被本次请求排除的项目，按"最空闲优先"排序。

        排序键 (inflight/weight, consecutive_failures, name)：优先挑在途负载低、
        近期健康的项目，权重高的项目分到更多流量。capability 为 None 时不按能力过滤。
        """
        excluded = excluded or set()
        now = time.monotonic()
        healthy = [
            p for p in self.projects
            if p.name not in excluded
            and p.circuit_until <= now
            and p.has_capability(capability)
        ]
        return sorted(
            healthy,
            key=lambda p: (p.inflight / max(1, p.weight), p.consecutive_failures, p.name),
        )

    def mark_success(self, project: GcpProjectRuntime) -> None:
        project.consecutive_failures = 0
        project.circuit_until = 0.0

    def mark_failure(self, project: GcpProjectRuntime, reason: str) -> None:
        project.consecutive_failures += 1
        if project.consecutive_failures >= self.policy.circuit_failure_threshold:
            cooldown = self.policy.circuit_cooldown_seconds
            project.circuit_until = time.monotonic() + cooldown
            logger.warning(
                f"生成项目 {project.name} 连续失败 {project.consecutive_failures} 次，"
                f"熔断 {cooldown:.0f}s；最后原因: {reason}"
            )


def _endpoints_file() -> Path:
    """生成池配置文件路径：settings.GCP_ENDPOINTS_FILE，相对路径按项目根解析。"""
    raw = (getattr(_settings, "GCP_ENDPOINTS_FILE", None) or "secrets/gcp-endpoints.yaml").strip()
    return _resolve_path(raw)


def _parse_policy(defaults: dict) -> PoolPolicy:
    defaults = defaults or {}
    return PoolPolicy(
        circuit_failure_threshold=_coerce_positive_int(
            defaults.get("circuit_failure_threshold"), _DEFAULT_CIRCUIT_THRESHOLD
        ),
        circuit_cooldown_seconds=_coerce_positive_float(
            defaults.get("circuit_cooldown_seconds"), _DEFAULT_CIRCUIT_COOLDOWN, floor=1.0
        ),
        max_attempts=_coerce_positive_int(
            defaults.get("max_attempts"), _DEFAULT_MAX_ATTEMPTS
        ),
    )


def _parse_projects(items: list, default_concurrency: int) -> List[GcpProjectRuntime]:
    projects: List[GcpProjectRuntime] = []
    for idx, item in enumerate(items or []):
        if not isinstance(item, dict):
            raise RuntimeError(f"gcp-endpoints.yaml projects 第 {idx} 项必须是对象")
        cred_file = str(item.get("credentials_file") or "").strip()
        if not cred_file:
            raise RuntimeError(
                f"gcp-endpoints.yaml projects 第 {idx} 项缺少 credentials_file"
            )
        project_id = str(item.get("project_id") or "").strip()
        name = str(item.get("name") or project_id or f"project-{idx}").strip()
        projects.append(
            GcpProjectRuntime(
                name=name,
                project_id=project_id,
                credentials_file=cred_file,
                weight=_coerce_positive_int(item.get("weight"), 1),
                max_concurrency=_coerce_positive_int(
                    item.get("max_concurrency"), default_concurrency
                ),
                is_img=_coerce_bool(item.get("is_img"), default=True),
                is_veo=_coerce_bool(item.get("is_veo"), default=True),
                is_omni=_coerce_bool(item.get("is_omni"), default=True),
            )
        )
    return projects


def build_router() -> GcpProjectRouter:
    """从 secrets/gcp-endpoints.yaml 组生成池。

    文件缺失 / projects 为空 → 返回空 router（调用方回退单项目行为，向后兼容）。
    """
    path = _endpoints_file()
    if not path.is_file():
        logger.info(f"生成池配置文件不存在，回退单项目行为: {path}")
        return GcpProjectRouter([])

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        raise RuntimeError(f"解析生成池配置失败 {path}: {e}") from e

    if not isinstance(raw, dict):
        raise RuntimeError(f"生成池配置根节点必须是映射: {path}")

    defaults = raw.get("defaults") or {}
    policy = _parse_policy(defaults)
    default_concurrency = _coerce_positive_int(
        defaults.get("max_concurrency"), _DEFAULT_MAX_CONCURRENCY
    )
    projects = _parse_projects(raw.get("projects") or [], default_concurrency)

    if not projects:
        logger.info(f"生成池配置 projects 为空，回退单项目行为: {path}")
        return GcpProjectRouter([])

    logger.info(
        f"生成池加载 {len(projects)} 个项目：{[p.name for p in projects]} "
        f"(threshold={policy.circuit_failure_threshold}, "
        f"cooldown={policy.circuit_cooldown_seconds}s, max_attempts={policy.max_attempts})"
    )
    img_names = [p.name for p in projects if p.is_img]
    veo_names = [p.name for p in projects if p.is_veo]
    omni_names = [p.name for p in projects if p.is_omni]
    logger.info(f"能力分布：出图池={img_names}；veo池={veo_names}；omni池={omni_names}")
    return GcpProjectRouter(projects, policy)
