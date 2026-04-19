# -*- coding: utf-8 -*-
# @File：/api/cre_image.py
# @Author：AI Assistant
# @email：hx1561958968@gmail.com

import asyncio
import base64
import json
import logging
import os
import random
import sys
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import google.auth
import google.auth.transport.requests
import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    conint,
    field_validator,
    model_validator,
)

try:
    from utils.logger import setup_module_logger
except ImportError:
    def setup_module_logger(logger_name: str, log_file: str) -> logging.Logger:
        logger = logging.getLogger(logger_name)
        if not logger.hasHandlers():
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        return logger


logger = setup_module_logger(__name__, "logs/image/gemini_image.log")

router = APIRouter()

GOOGLE_PROJECT_ID = "x-pilot-469902"

# Vertex：Gemini 图片模型在官方 Notebook 中常用 global；其后为区域轮询兜底
GOOGLE_LOCATIONS = [
    "global",
    "us-central1",
    "us-east4",
    "us-west1",
    "us-west4",
    "northamerica-northeast1",
    "europe-west1",
    "europe-west2",
    "europe-west3",
    "europe-west4",
    "europe-west9",
    "asia-northeast1",
    "asia-northeast3",
    "asia-southeast1",
]

GCS_BUCKET_NAME = "x-pilot-storage"
GCS_OUTPUT_DIR = "gemini_images"
GCS_PUBLIC_URL_PREFIX = "https://storage.googleapis.com/x-pilot-storage"

VERTEX_API_ENDPOINT_TEMPLATE = (
    f"https://aiplatform.googleapis.com/v1beta1/projects/{GOOGLE_PROJECT_ID}"
    f"/locations/{{location_id}}/publishers/google/models/{{model_id}}:generateContent"
)

GCS_UPLOAD_ENDPOINT = (
    f"https://storage.googleapis.com/upload/storage/v1/b/{GCS_BUCKET_NAME}/o"
    f"?uploadType=media&name="
)

# 与 Vertex 文档中单张 inline 图片上限一致的量级
MAX_REFERENCE_IMAGE_BYTES = 7 * 1024 * 1024

http_client: Optional[httpx.AsyncClient] = None

# --- 模型能力：宽高比、image_size、参考图数量、可选特性（与官方 Notebook 对齐）---

ASPECT_RATIOS_25 = frozenset(
    {
        "1:1",
        "3:2",
        "2:3",
        "3:4",
        "4:3",
        "4:5",
        "5:4",
        "9:16",
        "16:9",
        "21:9",
    }
)
ASPECT_RATIOS_31 = ASPECT_RATIOS_25 | frozenset({"1:4", "4:1", "1:8", "8:1"})
# 3 Pro Notebook 与 2.5 列表一致（无 1:4 等）
ASPECT_RATIOS_3_PRO = ASPECT_RATIOS_25

MODEL_CAPS: Dict[str, Dict[str, Any]] = {
    "gemini-2.5-flash-image": {
        "aspect_ratios": ASPECT_RATIOS_25,
        "image_sizes": frozenset(),  # 不在该模型 Notebook 中暴露
        "output_mime_types": frozenset({"image/png", "image/jpeg"}),
        "max_references": 10,
        "supports_text_modality": True,
        "supports_thinking": False,
        "supports_prominent_people": False,
        "read_timeout_sec": 120.0,
    },
    "gemini-3-pro-image-preview": {
        "aspect_ratios": ASPECT_RATIOS_3_PRO,
        "image_sizes": frozenset({"1K", "2K", "4K"}),
        "output_mime_types": frozenset({"image/png", "image/jpeg"}),
        "max_references": 6,
        "supports_text_modality": True,
        "supports_thinking": True,
        "supports_prominent_people": False,
        "read_timeout_sec": 300.0,
    },
    "gemini-3.1-flash-image-preview": {
        "aspect_ratios": ASPECT_RATIOS_31,
        "image_sizes": frozenset({"512", "1K", "2K", "4K"}),
        "output_mime_types": frozenset({"image/png", "image/jpeg"}),
        "max_references": 14,
        "supports_text_modality": True,
        "supports_thinking": True,
        "supports_prominent_people": True,
        "read_timeout_sec": 180.0,
    },
}

MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}


def _optional_url_host_whitelist() -> Optional[frozenset]:
    """若设置 CRE_IMAGE_ALLOWED_URL_HOSTS，则仅允许这些主机；未设置则任意 https 图片 URL 均可。"""
    raw = os.environ.get("CRE_IMAGE_ALLOWED_URL_HOSTS", "")
    if not raw.strip():
        return None
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _gcs_object_ext(content_type: str) -> str:
    base = (content_type or "").split(";")[0].strip().lower()
    return MIME_TO_EXT.get(base, "bin")


@router.on_event("startup")
async def startup_event():
    global http_client
    timeout = httpx.Timeout(connect=15.0, read=360.0, write=60.0, pool=30.0)
    http_client = httpx.AsyncClient(timeout=timeout)
    logger.info("全局 httpx.AsyncClient 已创建 (cre_image)，read 上限 360s 供单次请求覆盖使用。")


@router.on_event("shutdown")
async def shutdown_event():
    global http_client
    if http_client:
        await http_client.aclose()
        logger.info("httpx.AsyncClient 已关闭 (cre_image)。")


class StandardResponse(BaseModel):
    code: int = Field(200, description="HTTP状态码")
    message: str = Field("Success", description="响应消息")
    data: Optional[Any] = Field(None, description="响应数据")
    timestamp: str = Field(..., description="ISO 8601 格式的时间戳")


def create_standard_response(
    data: Optional[Any] = None,
    code: int = 200,
    message: str = "Success",
) -> JSONResponse:
    content = StandardResponse(
        code=code,
        message=message,
        data=data,
        timestamp=datetime.now().isoformat(),
    ).model_dump(exclude_none=True)
    return JSONResponse(status_code=code, content=content)


async def get_gcloud_auth_token() -> str:
    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        if not credentials.token:
            raise RuntimeError("凭证中无 token")
        return credentials.token
    except Exception as e:
        logger.error(f"获取 ADC 失败: {e}")
        raise RuntimeError(f"Failed to get application default credentials: {e}") from e


async def upload_to_gcs(image_data: bytes, content_type: str, folder: str = GCS_OUTPUT_DIR) -> str:
    ext = _gcs_object_ext(content_type)
    filename = f"{folder}/{uuid.uuid4()}.{ext}"
    upload_url = GCS_UPLOAD_ENDPOINT + filename
    auth_token = await get_gcloud_auth_token()
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": content_type,
    }
    try:
        resp = await http_client.post(upload_url, content=image_data, headers=headers)
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


class ImageModelID(str, Enum):
    GEMINI_3_PRO_IMAGE_PREVIEW = "gemini-3-pro-image-preview"
    GEMINI_2_5_FLASH = "gemini-2.5-flash-image"
    GEMINI_3_1_FLASH_PREVIEW = "gemini-3.1-flash-image-preview"


class PersonGeneration(str, Enum):
    ALLOW_ADULT = "allow_adult"
    ALLOW_ALL = "allow_all"
    DISALLOW = "disallow"


class ResponseMimeType(str, Enum):
    PNG = "image/png"
    JPEG = "image/jpeg"


class SafetyThreshold(str, Enum):
    OFF = "OFF"
    BLOCK_NONE = "BLOCK_NONE"
    BLOCK_LOW_AND_ABOVE = "BLOCK_LOW_AND_ABOVE"
    BLOCK_MEDIUM_AND_ABOVE = "BLOCK_MEDIUM_AND_ABOVE"
    BLOCK_ONLY_HIGH = "BLOCK_ONLY_HIGH"


class ProminentPeople(str, Enum):
    BLOCK_PROMINENT_PEOPLE = "BLOCK_PROMINENT_PEOPLE"


class ThinkingLevel(str, Enum):
    HIGH = "HIGH"
    MINIMAL = "MINIMAL"
    LOW = "LOW"


class ReferenceImageInput(BaseModel):
    """参考图：三选一。image_url 为任意可访问的 https 图片链接；可选环境变量 CRE_IMAGE_ALLOWED_URL_HOSTS 限制主机白名单。"""

    model_config = ConfigDict(extra="forbid")

    image_base64: Optional[str] = Field(None, description="Base64 图片数据，可与 mime_type 配合使用")
    mime_type: Optional[str] = Field(
        None, description="image/png 或 image/jpeg；base64/URL 下载时建议填写"
    )
    image_url: Optional[str] = Field(None, description="https 图片地址（任意公网可拉取的 URL）")
    gs_uri: Optional[str] = Field(None, description="gs://bucket/object，须可被当前项目访问")

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


class GenerateImagePayload(BaseModel):
    prompt: str = Field(..., min_length=1, description="生成/编辑说明（图生图时描述如何改）")
    model_id: ImageModelID = Field(
        ImageModelID.GEMINI_3_1_FLASH_PREVIEW,
        description="Vertex 模型 ID",
    )
    system_instruction: Optional[str] = Field(
        None,
        max_length=32000,
        description="系统指令，映射为 systemInstruction",
    )

    aspect_ratio: str = Field("1:1", description="宽高比，取值依赖 model_id")
    image_size: Optional[str] = Field(
        None,
        description="输出分辨率档位：3.1 支持 512/1K/2K/4K；3 Pro 支持 1K/2K/4K；2.5 不支持",
    )
    # Dify 等工具链常只能传字符串：单张参考图 URL，免写 reference_images JSON
    reference_image_url: Optional[str] = Field(
        None,
        description="单张参考图 https URL（与 reference_images 二选一；适合 Dify 字符串参数）",
    )
    response_mime_type: Optional[ResponseMimeType] = Field(
        None,
        description=(
            "可选。期望输出 MIME：image/png 或 image/jpeg。"
            "设置时写入 imageConfig.imageOutputOptions.mimeType（Google Gen AI SDK 约定）；"
            "省略则不传，由模型决定返回类型。若遇 400 未知字段可保持省略。"
        ),
    )

    response_count: Optional[conint(ge=1, le=8)] = Field(
        1,
        description="生成图片张数。图片输出仅支持 candidateCount=1，将在服务端并行多次调用 Vertex（可能产生多倍计费）",
    )

    negative_prompt: Optional[str] = Field(None, description="负向提示，拼入用户文本")
    person_generation: Optional[PersonGeneration] = Field(
        PersonGeneration.ALLOW_ADULT,
        description="人物生成策略；写入 imageConfig.personGeneration（ALLOW_ADULT 等）",
    )

    reference_images: Optional[List[ReferenceImageInput]] = Field(
        None,
        description="参考图列表，非空时为图生图/多参考图",
    )

    include_response_text: bool = Field(
        False,
        description="为 True 时 responseModalities 含 TEXT，便于配文；解析时收集非图片文本",
    )
    include_thoughts: bool = Field(
        False,
        description="为 True 时在支持的模型上请求思考过程（thinkingConfig.includeThoughts）",
    )
    thinking_level: Optional[ThinkingLevel] = Field(
        None,
        description="思考级别 HIGH/MINIMAL/LOW，仅 3.x 预览模型；与 include_thoughts 配合",
    )

    prominent_people: Optional[ProminentPeople] = Field(
        None,
        description="仅 gemini-3.1-flash-image-preview：imageConfig 知名人物限制",
    )

    location_override: Optional[str] = Field(
        None,
        description="指定 Vertex location（如 global），设置后不再轮询其他区域",
    )

    safety_filter_level: Optional[SafetyThreshold] = Field(
        SafetyThreshold.OFF,
        description="安全阈值，映射 safetySettings",
    )

    model_config = ConfigDict(use_enum_values=True)

    @field_validator(
        "system_instruction",
        "negative_prompt",
        "location_override",
        "image_size",
        mode="before",
    )
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

    @field_validator("response_count", mode="before")
    @classmethod
    def _coerce_response_count(cls, v: Any) -> Any:
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

    @field_validator("include_response_text", "include_thoughts", mode="before")
    @classmethod
    def _coerce_bool(cls, v: Any) -> Any:
        if v is None or v == "":
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "1", "yes", "on"):
                return True
            if s in ("false", "0", "no", "off"):
                return False
        return v

    @field_validator("reference_images", mode="before")
    @classmethod
    def _coerce_reference_images(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            try:
                return json.loads(s)
            except json.JSONDecodeError as e:
                raise ValueError(
                    "reference_images 须为 JSON 数组字符串，例如 "
                    '[{"image_url":"https://example.com/a.png"}]'
                ) from e
        return v

    @model_validator(mode="after")
    def _merge_dify_reference_url(self) -> "GenerateImagePayload":
        url = (self.reference_image_url or "").strip() if self.reference_image_url else ""
        if not url:
            return self
        refs = self.reference_images or []
        if refs:
            raise ValueError("请勿同时使用 reference_image_url 与 reference_images")
        return self.model_copy(
            update={"reference_images": [ReferenceImageInput(image_url=url)]}
        )


class ImageResult(BaseModel):
    public_url: str
    local_path: Optional[str] = None
    mime_type: str


class GenerateImageResponse(BaseModel):
    images: List[ImageResult] = []
    text_parts: List[str] = []


def _caps_for(model_id: str) -> Dict[str, Any]:
    caps = MODEL_CAPS.get(model_id)
    if not caps:
        raise HTTPException(
            status_code=422,
            detail=f"未知模型: {model_id}",
        )
    return caps


def _validate_payload_against_model(payload: GenerateImagePayload) -> None:
    mid = payload.model_id
    caps = _caps_for(mid)
    ar = (payload.aspect_ratio or "").strip()
    if ar not in caps["aspect_ratios"]:
        raise HTTPException(
            status_code=422,
            detail=(
                f"模型 {mid} 不支持 aspect_ratio={ar!r}。"
                f"允许: {sorted(caps['aspect_ratios'])}"
            ),
        )
    if payload.image_size is not None:
        sz = payload.image_size.strip()
        allowed_sz = caps["image_sizes"]
        if not allowed_sz:
            raise HTTPException(
                status_code=422,
                detail=f"模型 {mid} 不支持 image_size 参数",
            )
        if sz not in allowed_sz:
            raise HTTPException(
                status_code=422,
                detail=f"模型 {mid} 不支持 image_size={sz!r}，允许: {sorted(allowed_sz)}",
            )
    if payload.response_mime_type:
        mt = payload.response_mime_type
        mt_s = mt.value if isinstance(mt, ResponseMimeType) else str(mt)
        if mt_s not in caps["output_mime_types"]:
            raise HTTPException(
                status_code=422,
                detail=f"模型 {mid} 不支持 response_mime_type={mt_s}，允许: {sorted(caps['output_mime_types'])}",
            )
    if payload.prominent_people and not caps["supports_prominent_people"]:
        raise HTTPException(
            status_code=422,
            detail="prominent_people 仅支持 gemini-3.1-flash-image-preview",
        )
    if payload.thinking_level and not caps["supports_thinking"]:
        raise HTTPException(
            status_code=422,
            detail="thinking_level 仅支持 gemini-3-pro-image-preview 与 gemini-3.1-flash-image-preview",
        )
    if payload.include_thoughts and not caps["supports_thinking"]:
        raise HTTPException(
            status_code=422,
            detail="include_thoughts 仅支持 gemini-3-pro-image-preview 与 gemini-3.1-flash-image-preview",
        )
    refs = payload.reference_images or []
    if len(refs) > caps["max_references"]:
        raise HTTPException(
            status_code=422,
            detail=(
                f"模型 {mid} 最多 {caps['max_references']} 张参考图，当前 {len(refs)}"
            ),
        )


def _person_generation_rest_value(pg: Any) -> Optional[str]:
    if pg is None:
        return None
    key = pg.value if isinstance(pg, PersonGeneration) else str(pg)
    mapping = {
        "allow_adult": "ALLOW_ADULT",
        "allow_all": "ALLOW_ALL",
        "disallow": "DISALLOW",
    }
    return mapping.get(key)


def _build_prompt_text(payload: GenerateImagePayload) -> str:
    text = payload.prompt.strip()
    if payload.negative_prompt and payload.negative_prompt.strip():
        text += (
            "\n\nAvoid or do not include the following: "
            + payload.negative_prompt.strip()
        )
    return text


def _build_image_config(payload: GenerateImagePayload) -> Dict[str, Any]:
    mid = payload.model_id
    caps = MODEL_CAPS[mid]
    cfg: Dict[str, Any] = {"aspectRatio": payload.aspect_ratio.strip()}
    # SDK 文档：输出格式在 imageOutputOptions.mimeType（非顶层 outputMimeType，Vertex 会报 Unknown field）
    if payload.response_mime_type:
        rmt = payload.response_mime_type
        rmt_s = rmt.value if isinstance(rmt, ResponseMimeType) else str(rmt)
        cfg["imageOutputOptions"] = {"mimeType": rmt_s}
    if payload.image_size and caps["image_sizes"]:
        cfg["imageSize"] = payload.image_size.strip()
    # 2.5 Flash Image 官方示例仅展示 aspect_ratio；personGeneration 仅在 3.x 上发送以降低 400 风险
    if mid != "gemini-2.5-flash-image":
        pg = _person_generation_rest_value(payload.person_generation)
        if pg:
            cfg["personGeneration"] = pg
    pp = payload.prominent_people
    pp_s = pp.value if isinstance(pp, ProminentPeople) else pp
    if pp_s == "BLOCK_PROMINENT_PEOPLE":
        cfg["prominentPeople"] = "BLOCK_PROMINENT_PEOPLE"
    return cfg


def _build_generation_config(payload: GenerateImagePayload) -> Dict[str, Any]:
    caps = MODEL_CAPS[payload.model_id]
    modalities = ["IMAGE"]
    if payload.include_response_text and caps["supports_text_modality"]:
        modalities.append("TEXT")
    gen: Dict[str, Any] = {
        "temperature": 1,
        "topP": 0.95,
        "maxOutputTokens": 32768,
        "responseModalities": modalities,
        "candidateCount": 1,
        "imageConfig": _build_image_config(payload),
    }
    if payload.include_thoughts or payload.thinking_level:
        if not caps["supports_thinking"]:
            pass
        else:
            tc: Dict[str, Any] = {}
            if payload.include_thoughts:
                tc["includeThoughts"] = True
            if payload.thinking_level:
                tl = payload.thinking_level
                tc["thinkingLevel"] = tl.value if isinstance(tl, ThinkingLevel) else tl
            gen["thinkingConfig"] = tc
    return gen


def _build_safety_settings(threshold: SafetyThreshold) -> List[Dict[str, str]]:
    cats = [
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_HARASSMENT",
    ]
    return [{"category": c, "threshold": threshold} for c in cats]


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
        raise ValueError(f"主机 {host!r} 不在 CRE_IMAGE_ALLOWED_URL_HOSTS 白名单中")
    resp = await http_client.get(url, follow_redirects=True, timeout=60.0)
    resp.raise_for_status()
    body = resp.content
    if len(body) > MAX_REFERENCE_IMAGE_BYTES:
        raise ValueError("下载图片超过大小限制")
    ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        raise ValueError("URL 响应不是图片 Content-Type")
    return body, ct


async def _reference_to_part(ref: ReferenceImageInput) -> Dict[str, Any]:
    if ref.gs_uri:
        uri = ref.gs_uri.strip()
        if not uri.startswith("gs://"):
            raise ValueError("gs_uri 必须以 gs:// 开头")
        mime = (ref.mime_type or "image/jpeg").strip()
        return {"fileData": {"mimeType": mime, "fileUri": uri}}
    if ref.image_base64:
        raw = base64.b64decode(_normalize_b64(ref.image_base64))
        if len(raw) > MAX_REFERENCE_IMAGE_BYTES:
            raise ValueError("参考图 base64 解码后超过大小限制")
        mime = (ref.mime_type or "image/png").strip()
        b64_out = base64.standard_b64encode(raw).decode("ascii")
        return {"inlineData": {"mimeType": mime, "data": b64_out}}
    if ref.image_url:
        raw, ct = await _download_image_url(ref.image_url.strip())
        b64_out = base64.standard_b64encode(raw).decode("ascii")
        return {"inlineData": {"mimeType": ref.mime_type or ct, "data": b64_out}}
    raise ValueError("无效的参考图项")


async def build_user_parts(payload: GenerateImagePayload) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = []
    for ref in payload.reference_images or []:
        parts.append(await _reference_to_part(ref))
    parts.append({"text": _build_prompt_text(payload)})
    return parts


def build_request_body(payload: GenerateImagePayload, user_parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": user_parts}],
        "generationConfig": _build_generation_config(payload),
        "safetySettings": _build_safety_settings(payload.safety_filter_level),
    }
    si = (payload.system_instruction or "").strip()
    if si:
        body["systemInstruction"] = {"role": "system", "parts": [{"text": si}]}
    return body


def _locations_to_try(payload: GenerateImagePayload) -> List[str]:
    if payload.location_override and payload.location_override.strip():
        return [payload.location_override.strip()]
    locs = GOOGLE_LOCATIONS.copy()
    random.shuffle(locs)
    # global 优先尝试一次再 shuffle？已在列表中 shuffle；将 global 固定为第一项更稳
    if "global" in locs:
        locs.remove("global")
        random.shuffle(locs)
        return ["global"] + locs[: max(0, 3)]
    return locs[:4]


async def call_vertex_generate_content(
    request_id: str,
    model_id: str,
    request_body: Dict[str, Any],
    locations: List[str],
    read_timeout: float,
) -> Dict[str, Any]:
    auth_token = await get_gcloud_auth_token()
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    per_timeout = httpx.Timeout(connect=15.0, read=read_timeout, write=120.0, pool=30.0)
    last_exc: Optional[BaseException] = None
    for attempt, location in enumerate(locations):
        endpoint = VERTEX_API_ENDPOINT_TEMPLATE.format(
            location_id=location,
            model_id=model_id,
        )
        try:
            logger.debug(
                f"[{request_id}] Vertex 尝试 location={location} ({attempt + 1}/{len(locations)})"
            )
            resp = await http_client.post(
                endpoint, headers=headers, json=request_body, timeout=per_timeout
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            last_exc = e
            code = e.response.status_code
            if code == 429 or code >= 500:
                logger.warning(
                    f"[{request_id}] {location} 失败 {code}: {e.response.text[:500]}"
                )
                continue
            raise
        except Exception as e:
            last_exc = e
            logger.warning(f"[{request_id}] {location} 异常: {e}")
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError("Vertex 调用失败且无异常信息")


async def extract_images_and_texts(
    request_id: str,
    data: Dict[str, Any],
    collect_text: bool,
) -> Tuple[List[ImageResult], List[str]]:
    images: List[ImageResult] = []
    texts: List[str] = []
    candidates = data.get("candidates") or []
    if not candidates:
        return images, texts
    for cand in candidates:
        parts = (cand.get("content") or {}).get("parts") or []
        for part in parts:
            if part.get("thought"):
                continue
            inline = part.get("inlineData")
            if inline and str(inline.get("mimeType", "")).startswith("image/"):
                mime = inline.get("mimeType")
                b64 = inline.get("data")
                if b64:
                    img_bytes = base64.b64decode(b64)
                    url = await upload_to_gcs(img_bytes, mime)
                    images.append(
                        ImageResult(public_url=url, mime_type=mime, local_path=None)
                    )
                continue
            if collect_text and part.get("text"):
                texts.append(part["text"])
    return images, texts


async def _single_generation_round(
    request_id: str,
    payload: GenerateImagePayload,
    user_parts: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[ImageResult], List[str]]:
    parts = user_parts if user_parts is not None else await build_user_parts(payload)
    body = build_request_body(payload, parts)
    caps = MODEL_CAPS[payload.model_id]
    read_to = float(caps["read_timeout_sec"])
    locs = _locations_to_try(payload)
    data = await call_vertex_generate_content(
        request_id, payload.model_id, body, locs, read_to
    )
    return await extract_images_and_texts(
        request_id,
        data,
        collect_text=payload.include_response_text,
    )


@router.post("/generate_image", summary="文生图 / 图生图（Vertex Gemini）")
async def generate_image(payload: GenerateImagePayload):
    request_id = str(uuid.uuid4())
    logger.info(
        f"图片请求 [{request_id}] model={payload.model_id} refs={len(payload.reference_images or [])} "
        f"n={payload.response_count}"
    )
    try:
        _validate_payload_against_model(payload)
        n = int(payload.response_count or 1)
        if n < 1:
            n = 1

        merged_images: List[ImageResult] = []
        merged_texts: List[str] = []

        shared_parts = await build_user_parts(payload)

        if n == 1:
            imgs, txs = await _single_generation_round(
                request_id, payload, user_parts=shared_parts
            )
            merged_images = imgs
            merged_texts = txs
        else:
            sem = asyncio.Semaphore(min(4, n))

            async def _one(i: int):
                async with sem:
                    sub = f"{request_id}:{i}"
                    return await _single_generation_round(
                        sub, payload, user_parts=shared_parts
                    )

            results = await asyncio.gather(*(_one(i) for i in range(n)), return_exceptions=True)
            for i, r in enumerate(results):
                if isinstance(r, BaseException):
                    logger.exception(f"[{request_id}] 并行第 {i} 次失败: {r}")
                    return create_standard_response(
                        code=status.HTTP_502_BAD_GATEWAY,
                        message=f"并行生成失败（第 {i + 1}/{n} 次）: {r}",
                    )
                imgs, txs = r
                merged_images.extend(imgs)
                merged_texts.extend(txs)

        if not merged_images:
            return create_standard_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message="API 响应中未找到图片数据",
            )

        out = GenerateImageResponse(
            images=merged_images,
            text_parts=merged_texts if payload.include_response_text else [],
        )
        return create_standard_response(
            data=out.model_dump(),
            message="图片生成成功",
        )
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        err = f"Google API 请求失败: {e.response.status_code} - {e.response.text}"
        logger.error(f"[{request_id}] {err}")
        return create_standard_response(code=status.HTTP_502_BAD_GATEWAY, message=err)
    except ValueError as e:
        return create_standard_response(
            code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            message=str(e),
        )
    except Exception as e:
        err = f"图片生成内部错误: {e}"
        logger.exception(f"[{request_id}] {err}")
        return create_standard_response(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=err,
        )
