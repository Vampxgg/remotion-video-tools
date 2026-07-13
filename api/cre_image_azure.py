# -*- coding: utf-8 -*-
# @File：/api/cre_image_azure.py
# @Author：AI Assistant
# @email：hx1561958968@gmail.com
"""Azure OpenAI gpt-image-2 文生图 / 图生图接口。

与 api/cre_image.py（Vertex Gemini）物理隔离：本模块不 import 也不复用 cre_image
的模型/常量，仅共享纯基础设施（utils.settings / utils.responses / GCS 上传凭证）。

- 鉴权：Azure OpenAI 的 ``api-key`` 头（settings.CRE_IMAGE_AZURE_API_KEY）。
- 生成：httpx 直连 REST，不引入 openai SDK。
  * 纯文生图 -> POST .../images/generations（application/json）
  * 带参考图 -> POST .../images/edits（multipart/form-data，image[] + prompt [+ mask]）
- 输出：gpt-image-2 始终返回 base64（data[].b64_json），本模块解码后上传 GCS，
  返回公网可访问的 public_url，响应结构与 cre_image.py 对齐（images[] + text_parts）。
"""

import asyncio
import base64
import contextlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlparse

import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, conint, field_validator, model_validator

from utils.logger import setup_module_logger
from utils.gcp_credentials import get_access_token  # 共享基础设施：GCS 上传凭证
from utils.settings import settings as _settings
from utils.responses import create_standard_response as _shared_create_standard_response

logger = setup_module_logger(__name__, "logs/image/azure_image.log")

router = APIRouter()

# ─── 配置快照 ───────────────────────────────────────────────
AZURE_ENDPOINT = (_settings.CRE_IMAGE_AZURE_ENDPOINT or "").rstrip("/")
AZURE_DEPLOYMENT = _settings.CRE_IMAGE_AZURE_DEPLOYMENT
AZURE_API_VERSION = _settings.CRE_IMAGE_AZURE_API_VERSION

GCS_BUCKET_NAME = _settings.GCS_BUCKET_NAME
GCS_OUTPUT_DIR = _settings.CRE_IMAGE_AZURE_OUTPUT_DIR
GCS_PUBLIC_URL_PREFIX = _settings.GCS_PUBLIC_URL_PREFIX
GCS_UPLOAD_ENDPOINT = (
    f"https://storage.googleapis.com/upload/storage/v1/b/{GCS_BUCKET_NAME}/o"
    f"?uploadType=media&name="
)

MAX_REFERENCE_IMAGE_BYTES = _settings.CRE_IMAGE_AZURE_MAX_REFERENCE_BYTES

# 方案 A：两套出网策略，均 trust_env=False（屏蔽进程 HTTP(S)_PROXY，避免代理掐断长请求）。
# - http_client：Azure API 调用 + 任意公网参考图 URL 下载（默认直连）。
# - gcs_client：GCS 上传 + gs_uri 下载（默认直连，可配代理翻墙访问 Google）。
http_client: Optional[httpx.AsyncClient] = None
gcs_client: Optional[httpx.AsyncClient] = None
azure_endpoints: List["AzureEndpointRuntime"] = []

MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}


@dataclass
class AzureEndpointRuntime:
    name: str
    endpoint: str
    api_key: str
    deployment: str
    weight: int = 1
    max_concurrency: int = 5
    semaphore: asyncio.Semaphore = field(init=False)
    inflight: int = 0
    consecutive_failures: int = 0
    circuit_until: float = 0.0

    def __post_init__(self) -> None:
        self.endpoint = self.endpoint.rstrip("/")
        self.weight = max(1, int(self.weight or 1))
        self.max_concurrency = max(1, int(self.max_concurrency or 1))
        self.semaphore = asyncio.Semaphore(self.max_concurrency)

    def url(self, route: str) -> str:
        deployment = quote(self.deployment, safe="")
        return (
            f"{self.endpoint}/openai/deployments/{deployment}/images/{route}"
            f"?api-version={AZURE_API_VERSION}"
        )

# ─── gpt-image-2 尺寸能力 ───────────────────────────────────
# 约束：两边均为 16 的倍数；长边 ≤ 3840；长短比 ≤ 3:1；像素数 655360–8294400。
_SIZE_EDGE_MULTIPLE = 16
_SIZE_MAX_EDGE = 3840
_SIZE_MAX_RATIO = 3.0
_SIZE_MIN_PIXELS = 655_360
_SIZE_MAX_PIXELS = 8_294_400

# 便捷入参 aspect_ratio -> 具体 size（均满足上述约束，基准约 1K 档）。
ASPECT_TO_SIZE: Dict[str, str] = {
    "1:1": "1024x1024",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
    "4:3": "1360x1024",
    "3:4": "1024x1360",
    "4:5": "1024x1280",
    "5:4": "1280x1024",
    "16:9": "1536x864",
    "9:16": "864x1536",
    "21:9": "2016x864",
}

_SIZE_RE = re.compile(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*$")


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, default)


def _parse_azure_endpoint_pool() -> List[AzureEndpointRuntime]:
    raw = (_settings.CRE_IMAGE_AZURE_ENDPOINTS_JSON or "").strip()
    endpoints: List[AzureEndpointRuntime] = []
    default_concurrency = _coerce_positive_int(
        _settings.CRE_IMAGE_AZURE_ENDPOINT_MAX_CONCURRENCY, 5
    )

    if raw:
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError("CRE_IMAGE_AZURE_ENDPOINTS_JSON 不是合法 JSON") from e
        if not isinstance(items, list):
            raise RuntimeError("CRE_IMAGE_AZURE_ENDPOINTS_JSON 必须是 JSON 数组")
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                raise RuntimeError(f"CRE_IMAGE_AZURE_ENDPOINTS_JSON 第 {idx} 项必须是对象")
            endpoint = str(item.get("endpoint") or "").strip().rstrip("/")
            api_key = str(item.get("api_key") or "").strip()
            deployment = str(item.get("deployment") or AZURE_DEPLOYMENT or "").strip()
            if not endpoint or not api_key or not deployment:
                raise RuntimeError(
                    "CRE_IMAGE_AZURE_ENDPOINTS_JSON 每项必须包含 endpoint/api_key/deployment"
                )
            name = str(item.get("name") or urlparse(endpoint).hostname or f"endpoint-{idx}").strip()
            endpoints.append(
                AzureEndpointRuntime(
                    name=name,
                    endpoint=endpoint,
                    api_key=api_key,
                    deployment=deployment,
                    weight=_coerce_positive_int(item.get("weight"), 1),
                    max_concurrency=_coerce_positive_int(
                        item.get("max_concurrency"), default_concurrency
                    ),
                )
            )

    if endpoints:
        return endpoints

    # 向后兼容：没有配置多区域池时，继续使用原来的单 endpoint 配置。
    api_key = (_settings.CRE_IMAGE_AZURE_API_KEY or "").strip()
    endpoint = (AZURE_ENDPOINT or "").strip()
    deployment = (AZURE_DEPLOYMENT or "").strip()
    if api_key and endpoint and deployment:
        name = urlparse(endpoint).hostname or "default"
        return [
            AzureEndpointRuntime(
                name=name,
                endpoint=endpoint,
                api_key=api_key,
                deployment=deployment,
                max_concurrency=default_concurrency,
            )
        ]
    return []


class ImageQuality(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class OutputFormat(str, Enum):
    PNG = "png"
    JPEG = "jpeg"


class ImageBackground(str, Enum):
    AUTO = "auto"
    TRANSPARENT = "transparent"


# ─── 生命周期资源管理 ───────────────────────────────────────
async def _startup_resources() -> None:
    global http_client, gcs_client, azure_endpoints
    timeout = httpx.Timeout(
        connect=_settings.CRE_IMAGE_AZURE_HTTPX_CONNECT_TIMEOUT,
        read=_settings.CRE_IMAGE_AZURE_HTTPX_READ_TIMEOUT,
        write=_settings.CRE_IMAGE_AZURE_HTTPX_WRITE_TIMEOUT,
        pool=_settings.CRE_IMAGE_AZURE_HTTPX_POOL_TIMEOUT,
    )
    azure_proxy = (_settings.CRE_IMAGE_AZURE_PROXY_URL or "").strip() or None
    gcs_proxy = (_settings.CRE_IMAGE_AZURE_GCS_PROXY_URL or "").strip() or None
    # trust_env=False：不继承进程环境代理，出网策略完全由上面两项显式控制。
    http_client = httpx.AsyncClient(timeout=timeout, trust_env=False, proxy=azure_proxy)
    gcs_client = httpx.AsyncClient(timeout=timeout, trust_env=False, proxy=gcs_proxy)
    azure_endpoints = _parse_azure_endpoint_pool()
    logger.info(
        f"httpx client 已创建 (cre_image_azure)：Azure 代理={azure_proxy or '直连'}，"
        f"GCS 代理={gcs_proxy or '直连'}，read 上限 {_settings.CRE_IMAGE_AZURE_HTTPX_READ_TIMEOUT}s，"
        f"endpoint 数={len(azure_endpoints)}。"
    )


async def _shutdown_resources() -> None:
    global http_client, gcs_client, azure_endpoints
    if http_client:
        await http_client.aclose()
    if gcs_client:
        await gcs_client.aclose()
    azure_endpoints = []
    logger.info("httpx client 已关闭 (cre_image_azure)。")


@contextlib.asynccontextmanager
async def lifespan_resources(app):
    await _startup_resources()
    try:
        yield
    finally:
        await _shutdown_resources()


def create_standard_response(
    data: Optional[Any] = None,
    code: int = 200,
    message: str = "Success",
) -> JSONResponse:
    return _shared_create_standard_response(
        data=data, code=code, message=message, exclude_none=True
    )


# ─── GCS 上传（自包含，仅依赖共享凭证）───────────────────────
def _gcs_object_ext(content_type: str) -> str:
    base = (content_type or "").split(";")[0].strip().lower()
    return MIME_TO_EXT.get(base, "bin")


async def upload_to_gcs(image_data: bytes, content_type: str, folder: str = GCS_OUTPUT_DIR) -> str:
    ext = _gcs_object_ext(content_type)
    filename = f"{folder}/{uuid.uuid4()}.{ext}"
    upload_url = GCS_UPLOAD_ENDPOINT + filename
    auth_token = await get_access_token()
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": content_type,
    }
    try:
        resp = await gcs_client.post(upload_url, content=image_data, headers=headers)
        resp.raise_for_status()
        public_url = f"{GCS_PUBLIC_URL_PREFIX}/{filename}"
        logger.info(f"图片已上传至 GCS: {public_url}")
        return public_url
    except httpx.HTTPStatusError as e:
        logger.error(f"GCS 上传失败: {e.response.status_code} - {e.response.text}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload image to storage.",
        ) from e


# ─── URL 白名单 / 参考图下载 ────────────────────────────────
def _optional_url_host_whitelist() -> Optional[frozenset]:
    raw = _settings.CRE_IMAGE_AZURE_ALLOWED_URL_HOSTS or ""
    if not raw.strip():
        return None
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _normalize_b64(data: str) -> str:
    s = data.strip()
    if "base64," in s:
        s = s.split("base64,", 1)[1]
    return s


async def _download_image_url(url: str) -> Tuple[bytes, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("image_url 仅支持 https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("image_url 缺少有效主机名")
    whitelist = _optional_url_host_whitelist()
    if whitelist is not None and host not in whitelist:
        raise ValueError(f"主机 {host!r} 不在 CRE_IMAGE_AZURE_ALLOWED_URL_HOSTS 白名单中")
    resp = await http_client.get(url, follow_redirects=True, timeout=60.0)
    resp.raise_for_status()
    body = resp.content
    if len(body) > MAX_REFERENCE_IMAGE_BYTES:
        raise ValueError("下载图片超过大小限制")
    ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        raise ValueError("URL 响应不是图片 Content-Type")
    return body, ct


# ─── 请求 / 响应模型 ────────────────────────────────────────
class ReferenceImageInput(BaseModel):
    """参考图：三选一。命中任意参考图时，请求走 images/edits 编辑接口。"""

    model_config = ConfigDict(extra="forbid")

    image_base64: Optional[str] = Field(None, description="Base64 图片数据")
    mime_type: Optional[str] = Field(None, description="image/png 或 image/jpeg")
    image_url: Optional[str] = Field(None, description="https 图片地址（公网可拉取）")
    gs_uri: Optional[str] = Field(None, description="gs://bucket/object，须当前凭证可访问")

    @model_validator(mode="after")
    def _one_source(self):
        sources = sum(
            1
            for x in (self.image_base64, self.image_url, self.gs_uri)
            if x is not None and str(x).strip()
        )
        if sources != 1:
            raise ValueError("参考图必须且仅能指定 image_base64、image_url、gs_uri 其中之一")
        return self


class GenerateAzureImagePayload(BaseModel):
    prompt: str = Field(..., min_length=1, description="生成/编辑说明")
    negative_prompt: Optional[str] = Field(None, description="负向提示，拼入 prompt 文本")

    # size 二选一：显式 size（高级，支持到 4K）优先；否则用 aspect_ratio 查表。
    size: Optional[str] = Field(
        None,
        description=(
            "显式输出尺寸 '宽x高'。约束：两边均为 16 倍数；长边≤3840；长短比≤3:1；"
            "像素 655360–8294400。留空则由 aspect_ratio 决定。"
        ),
    )
    aspect_ratio: str = Field(
        "1:1",
        description="便捷宽高比，映射为具体 size。支持 1:1/3:2/2:3/4:3/3:4/4:5/5:4/16:9/9:16/21:9。",
    )

    n: conint(ge=1, le=10) = Field(1, description="单请求生成张数（原生 1-10）")
    quality: Optional[ImageQuality] = Field(None, description="low/medium/high，缺省取配置默认")
    output_format: Optional[OutputFormat] = Field(None, description="png/jpeg，缺省取配置默认")
    output_compression: Optional[conint(ge=0, le=100)] = Field(
        None, description="0-100，仅 jpeg 生效"
    )
    background: Optional[ImageBackground] = Field(
        None, description="auto/transparent；transparent 需 png"
    )

    reference_images: Optional[List[ReferenceImageInput]] = Field(
        None, description="参考图列表，非空则走 images/edits 编辑接口"
    )
    reference_image_url: Optional[str] = Field(
        None, description="单张参考图 https URL（与 reference_images 二选一，便于字符串参数场景）"
    )

    model_config = ConfigDict(use_enum_values=True)

    @field_validator("negative_prompt", "size", mode="before")
    @classmethod
    def _empty_optional_str(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("aspect_ratio", mode="before")
    @classmethod
    def _aspect_ratio_default(cls, v: Any) -> Any:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return "1:1"
        return v

    @field_validator("n", mode="before")
    @classmethod
    def _coerce_n(cls, v: Any) -> Any:
        if v is None or v == "":
            return 1
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return 1
            try:
                return int(s)
            except ValueError:
                return v
        return v

    @model_validator(mode="after")
    def _merge_single_reference_url(self) -> "GenerateAzureImagePayload":
        url = (self.reference_image_url or "").strip() if self.reference_image_url else ""
        if not url:
            return self
        if self.reference_images:
            raise ValueError("请勿同时使用 reference_image_url 与 reference_images")
        return self.model_copy(
            update={"reference_images": [ReferenceImageInput(image_url=url)]}
        )


class ImageResult(BaseModel):
    public_url: str
    mime_type: str


class GenerateImageResponse(BaseModel):
    images: List[ImageResult] = []
    text_parts: List[str] = []


# ─── 尺寸解析与校验 ─────────────────────────────────────────
def _validate_explicit_size(size: str) -> str:
    m = _SIZE_RE.match(size)
    if not m:
        raise HTTPException(status_code=422, detail=f"size 格式非法: {size!r}，应形如 '1024x1024'")
    w, h = int(m.group(1)), int(m.group(2))
    if w % _SIZE_EDGE_MULTIPLE or h % _SIZE_EDGE_MULTIPLE:
        raise HTTPException(status_code=422, detail="size 两边均须为 16 的倍数")
    if max(w, h) > _SIZE_MAX_EDGE:
        raise HTTPException(status_code=422, detail=f"size 长边不得超过 {_SIZE_MAX_EDGE}px")
    ratio = max(w, h) / min(w, h)
    if ratio > _SIZE_MAX_RATIO + 1e-6:
        raise HTTPException(status_code=422, detail="size 长短比不得超过 3:1")
    pixels = w * h
    if pixels < _SIZE_MIN_PIXELS or pixels > _SIZE_MAX_PIXELS:
        raise HTTPException(
            status_code=422,
            detail=f"size 像素数须在 {_SIZE_MIN_PIXELS}–{_SIZE_MAX_PIXELS} 之间（当前 {pixels}）",
        )
    return f"{w}x{h}"


def _resolve_size(payload: GenerateAzureImagePayload) -> str:
    if payload.size:
        return _validate_explicit_size(payload.size)
    ar = (payload.aspect_ratio or "1:1").strip()
    mapped = ASPECT_TO_SIZE.get(ar)
    if not mapped:
        raise HTTPException(
            status_code=422,
            detail=(
                f"aspect_ratio={ar!r} 不受 gpt-image-2 支持（长短比上限 3:1）。"
                f"可用: {sorted(ASPECT_TO_SIZE)}；或改用显式 size。"
            ),
        )
    return mapped


def _resolved_quality(payload: GenerateAzureImagePayload) -> str:
    q = payload.quality
    if q:
        return q.value if isinstance(q, ImageQuality) else str(q)
    return _settings.CRE_IMAGE_AZURE_DEFAULT_QUALITY


def _resolved_output_format(payload: GenerateAzureImagePayload) -> str:
    f = payload.output_format
    if f:
        return f.value if isinstance(f, OutputFormat) else str(f)
    return _settings.CRE_IMAGE_AZURE_DEFAULT_OUTPUT_FORMAT


def _build_prompt_text(payload: GenerateAzureImagePayload) -> str:
    text = payload.prompt.strip()
    if payload.negative_prompt and payload.negative_prompt.strip():
        text += "\n\nAvoid or do not include the following: " + payload.negative_prompt.strip()
    return text


def _require_endpoint_pool() -> List[AzureEndpointRuntime]:
    if not azure_endpoints:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CRE_IMAGE_AZURE_ENDPOINTS_JSON 或单 endpoint 配置未提供可用 Azure endpoint。",
        )
    return azure_endpoints


# ─── 参考图 -> (filename, bytes, content_type) ──────────────
async def _reference_to_file(ref: ReferenceImageInput) -> Tuple[str, bytes, str]:
    if ref.gs_uri:
        uri = ref.gs_uri.strip()
        if not uri.startswith("gs://"):
            raise ValueError("gs_uri 必须以 gs:// 开头")
        # 通过 GCS JSON API 以当前凭证下载对象
        bucket_obj = uri[len("gs://"):]
        bucket, _, obj = bucket_obj.partition("/")
        if not bucket or not obj:
            raise ValueError("gs_uri 格式应为 gs://bucket/object")
        token = await get_access_token()
        dl_url = (
            f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/"
            f"{quote(obj, safe='')}?alt=media"
        )
        resp = await gcs_client.get(dl_url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        raw = resp.content
        ct = resp.headers.get("content-type", "").split(";")[0].strip().lower() or "image/png"
    elif ref.image_base64:
        raw = base64.b64decode(_normalize_b64(ref.image_base64))
        ct = (ref.mime_type or "image/png").strip()
    elif ref.image_url:
        raw, dl_ct = await _download_image_url(ref.image_url.strip())
        ct = (ref.mime_type or dl_ct).strip()
    else:
        raise ValueError("无效的参考图项")

    if len(raw) > MAX_REFERENCE_IMAGE_BYTES:
        raise ValueError("参考图超过大小限制")
    if ct not in ("image/png", "image/jpeg", "image/jpg"):
        # Azure edits 仅接受 PNG/JPG；其余一律标注为 png 交由服务端判定
        ct = "image/png"
    ext = _gcs_object_ext(ct)
    return (f"ref_{uuid.uuid4().hex}.{ext}", raw, ct)


# ─── Azure 调用（含跨区域退避重试）───────────────────────────
def _extract_azure_error(data: Dict[str, Any]) -> Optional[str]:
    err = data.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        msg = err.get("message")
        return f"{code}: {msg}" if code else str(msg)
    return None


def _healthy_endpoints(excluded: Set[str]) -> List[AzureEndpointRuntime]:
    now = time.monotonic()
    healthy = [
        ep for ep in _require_endpoint_pool()
        if ep.name not in excluded and ep.circuit_until <= now
    ]
    return sorted(
        healthy,
        key=lambda ep: (ep.inflight / max(1, ep.weight), ep.consecutive_failures, ep.name),
    )


def _mark_endpoint_success(ep: AzureEndpointRuntime) -> None:
    ep.consecutive_failures = 0
    ep.circuit_until = 0.0


def _mark_endpoint_failure(ep: AzureEndpointRuntime, reason: str) -> None:
    ep.consecutive_failures += 1
    threshold = _coerce_positive_int(_settings.CRE_IMAGE_AZURE_CIRCUIT_FAILURE_THRESHOLD, 3)
    if ep.consecutive_failures >= threshold:
        cooldown = max(1.0, float(_settings.CRE_IMAGE_AZURE_CIRCUIT_COOLDOWN_SECONDS))
        ep.circuit_until = time.monotonic() + cooldown
        logger.warning(
            f"Azure endpoint {ep.name} 连续失败 {ep.consecutive_failures} 次，"
            f"熔断 {cooldown:.0f}s；最后原因: {reason}"
        )


def _retry_backoff(attempt: int) -> float:
    return min(15.0, 2.0 * (2 ** max(0, attempt)))


async def _post_with_retry(
    route: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    files: Optional[list] = None,
) -> Dict[str, Any]:
    max_retries = max(0, int(_settings.CRE_IMAGE_AZURE_MAX_RETRIES))
    read_to = float(_settings.CRE_IMAGE_AZURE_HTTPX_READ_TIMEOUT)
    per_timeout = httpx.Timeout(connect=15.0, read=read_to, write=120.0, pool=30.0)
    max_attempts = max(1, min(len(_require_endpoint_pool()) + max_retries, len(_require_endpoint_pool()) * 2))
    excluded: Set[str] = set()
    last_exc: Optional[BaseException] = None

    for attempt in range(max_attempts):
        candidates = _healthy_endpoints(excluded)
        if not candidates:
            excluded.clear()
            candidates = _healthy_endpoints(excluded)
        if not candidates:
            if last_exc:
                raise last_exc
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="所有 Azure image endpoint 当前均处于熔断状态。",
            )

        ep = candidates[0]
        excluded.add(ep.name)
        headers = {"api-key": ep.api_key}
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            ep.inflight += 1
            try:
                async with ep.semaphore:
                    resp = await http_client.post(
                        ep.url(route),
                        headers=headers,
                        json=json_body,
                        data=data,
                        files=files,
                        timeout=per_timeout,
                    )
            finally:
                ep.inflight = max(0, ep.inflight - 1)

            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts - 1:
                _mark_endpoint_failure(ep, f"HTTP {resp.status_code}")
                backoff = _retry_backoff(attempt)
                logger.warning(
                    f"Azure endpoint {ep.name} 返回 {resp.status_code}，"
                    f"退避 {backoff:.1f}s 后切换 endpoint 重试"
                )
                await asyncio.sleep(backoff)
                continue

            resp.raise_for_status()
            _mark_endpoint_success(ep)
            logger.info(f"Azure endpoint {ep.name} 调用成功 route={route}")
            return resp.json()

        except (
            httpx.RemoteProtocolError,
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        ) as e:
            last_exc = e
            _mark_endpoint_failure(ep, e.__class__.__name__)
            if attempt >= max_attempts - 1:
                raise
            backoff = _retry_backoff(attempt)
            logger.warning(
                f"Azure endpoint {ep.name} 网络瞬时错误 {e.__class__.__name__}，"
                f"退避 {backoff:.1f}s 后切换 endpoint 重试"
            )
            await asyncio.sleep(backoff)
            continue

    if last_exc:
        raise last_exc
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Azure endpoint 重试耗尽")


async def _extract_images(data: Dict[str, Any], out_mime: str) -> List[ImageResult]:
    images: List[ImageResult] = []
    for item in data.get("data") or []:
        b64 = item.get("b64_json")
        if not b64:
            continue
        img_bytes = base64.b64decode(b64)
        url = await upload_to_gcs(img_bytes, out_mime)
        images.append(ImageResult(public_url=url, mime_type=out_mime))
    return images


# ─── 主入口 ────────────────────────────────────────────────
@router.post("/generate_image_azure", summary="文生图 / 图生图（Azure gpt-image-2）")
async def generate_image_azure(payload: GenerateAzureImagePayload):
    request_id = str(uuid.uuid4())
    refs = payload.reference_images or []
    logger.info(
        f"Azure 图片请求 [{request_id}] refs={len(refs)} n={payload.n} "
        f"ar={payload.aspect_ratio} size={payload.size}"
    )
    try:
        _require_endpoint_pool()
        size = _resolve_size(payload)
        quality = _resolved_quality(payload)
        out_format = _resolved_output_format(payload)
        out_mime = "image/jpeg" if out_format == "jpeg" else "image/png"
        prompt_text = _build_prompt_text(payload)

        if refs:
            # 图生图 / 编辑：multipart/form-data
            files: list = []
            for ref in refs:
                fname, raw, ct = await _reference_to_file(ref)
                files.append(("image[]", (fname, raw, ct)))
            form: Dict[str, Any] = {
                "prompt": prompt_text,
                "size": size,
                "n": str(payload.n),
                "quality": quality,
            }
            data = await _post_with_retry("edits", data=form, files=files)
        else:
            # 文生图：application/json
            body: Dict[str, Any] = {
                "prompt": prompt_text,
                "size": size,
                "n": payload.n,
                "quality": quality,
                "output_format": out_format,
            }
            if payload.output_compression is not None and out_format == "jpeg":
                body["output_compression"] = int(payload.output_compression)
            if payload.background:
                bg = payload.background
                body["background"] = bg.value if isinstance(bg, ImageBackground) else str(bg)
            data = await _post_with_retry("generations", json_body=body)

        err = _extract_azure_error(data)
        if err:
            logger.warning(f"[{request_id}] Azure 返回错误: {err}")
            code = 422 if "contentFilter" in err else status.HTTP_502_BAD_GATEWAY
            return create_standard_response(code=code, message=f"Azure 生成失败: {err}")

        images = await _extract_images(data, out_mime)
        if not images:
            return create_standard_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message="API 响应中未找到图片数据",
            )

        out = GenerateImageResponse(images=images, text_parts=[])
        return create_standard_response(data=out.model_dump(), message="图片生成成功")

    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        body_txt = e.response.text[:800]
        err = f"Azure API 请求失败: {e.response.status_code} - {body_txt}"
        logger.error(f"[{request_id}] {err}")
        code = e.response.status_code
        if code == 401:
            return create_standard_response(code=401, message="Azure 鉴权失败：api-key 无效或缺失")
        if code == 429:
            return create_standard_response(code=429, message="Azure 触发限流，请稍后重试或提配额")
        return create_standard_response(code=status.HTTP_502_BAD_GATEWAY, message=err)
    except ValueError as e:
        return create_standard_response(
            code=status.HTTP_422_UNPROCESSABLE_ENTITY, message=str(e)
        )
    except Exception as e:  # noqa: BLE001
        err = f"图片生成内部错误: {e}"
        logger.exception(f"[{request_id}] {err}")
        return create_standard_response(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR, message=err
        )
