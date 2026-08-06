# -*- coding: utf-8 -*-
"""Azure 模型注册表：全项目唯一的 Azure OpenAI endpoint/apiKey 解析入口。

把原先散落在 settings 默认值、.env、各脚本里的 Azure endpoint + apiKey 明文收敛到单一
密钥源 ``secrets/azure-models.yaml``（已被 .gitignore 忽略，不入库）。范式对齐
``utils/gcp_credentials.py``：相对路径按项目根解析、进程内缓存一次、缺失给清晰报错。

yaml 结构（见 secrets/azure-models.yaml）::

    apiVersion: "2025-04-01-preview"
    regions:
      centralUS:
        endpoint: "https://x-pilot-10-practice-resource.openai.azure.com"
        apiKey: "..."
    models:
      FW-Kimi-K2.7-Code:
        api: chat
        regions: [centralUS]          # 首项=主用，其余=按序 fallback

用法::

    from utils.azure_models import resolve_model, resolve_single
    m = resolve_model("FW-Kimi-K2.7-Code")   # -> AzureModelResolved(api_version, endpoints[])
    ep = resolve_single("gpt-image-2")       # -> 首选 AzureEndpoint

deployment 直接取模型名（Azure 以部署名寻址，本清单里部署名==模型键名）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_YAML_REL = "secrets/azure-models.yaml"

_lock = threading.Lock()
# 缓存: 解析后的 yaml 原始 dict，按绝对路径缓存，避免每次调用都读盘。
_raw_cache: Dict[str, dict] = {}


class AzureModelsConfigError(RuntimeError):
    """azure-models.yaml 缺失 / 格式错误 / 模型或 region 未定义 / 缺 key。"""


@dataclass(frozen=True)
class AzureEndpoint:
    """一个可直接发起调用的 Azure 端点（region 维度）。"""

    name: str          # region 名，如 centralUS
    endpoint: str      # 形如 https://<resource>.openai.azure.com（无尾斜杠）
    api_key: str
    deployment: str    # 部署名（==模型键名）
    api_version: str


@dataclass(frozen=True)
class AzureModelResolved:
    """一个模型解析后的全部可用端点（首项=主用，其余=fallback）。"""

    model: str
    api: str                       # chat / responses / image / embedding / ...
    api_version: str
    endpoints: List[AzureEndpoint]

    @property
    def primary(self) -> AzureEndpoint:
        return self.endpoints[0]


def _resolve_yaml_path(raw: Optional[str]) -> Path:
    p = Path((raw or _DEFAULT_YAML_REL).strip() or _DEFAULT_YAML_REL)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return p


def _load_raw(path: Path) -> dict:
    key = str(path)
    cached = _raw_cache.get(key)
    if cached is not None:
        return cached
    with _lock:
        cached = _raw_cache.get(key)
        if cached is not None:
            return cached
        if not path.is_file():
            raise AzureModelsConfigError(
                f"Azure 模型清单不存在: {path}（应位于 secrets/azure-models.yaml，"
                f"已被 .gitignore 忽略，需在部署机放置真实密钥文件）"
            )
        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:  # noqa: BLE001
            raise AzureModelsConfigError(f"解析 {path} 失败: {e}") from e
        if not isinstance(data, dict):
            raise AzureModelsConfigError(f"{path} 顶层必须是映射(dict)")
        _raw_cache[key] = data
        return data


def _get_yaml_path() -> Path:
    """从 settings 读取 AZURE_MODELS_FILE（延迟导入避免循环依赖）。"""
    try:
        from utils.settings import settings as _settings

        raw = getattr(_settings, "AZURE_MODELS_FILE", None)
    except Exception:  # noqa: BLE001
        raw = None
    return _resolve_yaml_path(raw)


def resolve_model(
    model: str,
    *,
    yaml_path: Optional[str] = None,
) -> AzureModelResolved:
    """按模型名解析出其 api_version + 按 region 优先级排好的端点列表。

    :param model: 模型键名，如 ``FW-Kimi-K2.7-Code`` / ``gpt-image-2``。
    :param yaml_path: 可选，覆盖清单路径（默认 settings.AZURE_MODELS_FILE）。
    :raises AzureModelsConfigError: 清单缺失/模型未定义/region 缺 endpoint 或 key。
    """
    path = _resolve_yaml_path(yaml_path) if yaml_path else _get_yaml_path()
    data = _load_raw(path)

    default_api_version = str(data.get("apiVersion") or "").strip()
    regions_map = data.get("regions") or {}
    models_map = data.get("models") or {}
    if not isinstance(regions_map, dict) or not isinstance(models_map, dict):
        raise AzureModelsConfigError(f"{path} 的 regions/models 必须是映射(dict)")

    spec = models_map.get(model)
    if not isinstance(spec, dict):
        raise AzureModelsConfigError(
            f"模型 {model!r} 未在 {path} 的 models 中定义"
        )
    api = str(spec.get("api") or "").strip()
    region_names = spec.get("regions") or []
    if not isinstance(region_names, list) or not region_names:
        raise AzureModelsConfigError(f"模型 {model!r} 未配置 regions 优先级列表")

    endpoints: List[AzureEndpoint] = []
    for region in region_names:
        reg = regions_map.get(region)
        if not isinstance(reg, dict):
            raise AzureModelsConfigError(
                f"模型 {model!r} 引用的 region {region!r} 未在 regions 中定义"
            )
        endpoint = str(reg.get("endpoint") or "").strip().rstrip("/")
        api_key = str(reg.get("apiKey") or "").strip()
        if not endpoint or not api_key:
            raise AzureModelsConfigError(
                f"region {region!r} 缺少 endpoint 或 apiKey"
            )
        endpoints.append(
            AzureEndpoint(
                name=str(region),
                endpoint=endpoint,
                api_key=api_key,
                deployment=model,
                api_version=default_api_version,
            )
        )

    return AzureModelResolved(
        model=model,
        api=api,
        api_version=default_api_version,
        endpoints=endpoints,
    )


def resolve_single(
    model: str,
    *,
    yaml_path: Optional[str] = None,
) -> AzureEndpoint:
    """返回模型的首选（主用）端点；供单端点消费者使用。"""
    return resolve_model(model, yaml_path=yaml_path).primary


def clear_cache() -> None:
    """清空进程内缓存（改 yaml 后热重载 / 测试用）。"""
    with _lock:
        _raw_cache.clear()
