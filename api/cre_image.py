# -*- coding: utf-8 -*-
# @File：/api/cre_image.py
# @Author：AI Assistant
# @email：hx1561958968@gmail.com

# --- 导入模块 ---
import asyncio
import base64
import logging
import sys
import uuid
import os
from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, status, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, conint
import google.auth
import google.auth.transport.requests

# 尝试导入您项目中的日志模块
try:
    from utils.logger import setup_module_logger
except ImportError:
    # 备用日志方案
    def setup_module_logger(logger_name: str, log_file: str) -> logging.Logger:
        logger = logging.getLogger(logger_name)
        if not logger.hasHandlers():
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            print(f"CRITICAL: Failed to import setup_module_logger. Using fallback console logger for {logger_name}.")
        return logger

# --- 配置区 ---
# 使用您项目中的日志工具
logger = setup_module_logger(__name__, "logs/image/gemini_image.log")

router = APIRouter()

# --- Google Vertex AI & GCS 配置 ---
GOOGLE_PROJECT_ID = "x-pilot-469902"
GOOGLE_LOCATION_ID = "global"  # us-central1
# 图片输出的GCS桶
GCS_BUCKET_NAME = "x-pilot-storage"
GCS_OUTPUT_DIR = "gemini_images"
# GCS桶的公开访问URL前缀
GCS_PUBLIC_URL_PREFIX = "https://storage.googleapis.com/x-pilot-storage"

# API 端点模板
# https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{MODEL}:generateContent
VERTEX_API_ENDPOINT_TEMPLATE = (
    f"https://aiplatform.googleapis.com/v1beta1/projects/{GOOGLE_PROJECT_ID}"
    f"/locations/{GOOGLE_LOCATION_ID}/publishers/google/models/{{model_id}}:generateContent"
)

# GCS Upload API Endpoint
GCS_UPLOAD_ENDPOINT = f"https://storage.googleapis.com/upload/storage/v1/b/{GCS_BUCKET_NAME}/o?uploadType=media&name="

# 使用全局唯一的 httpx.AsyncClient 实例
http_client: httpx.AsyncClient = None


@router.on_event("startup")
async def startup_event():
    """在应用启动时创建全局 HTTP 客户端"""
    global http_client
    # 设置一个合理的超时，包括连接和读写
    timeout = httpx.Timeout(30.0, connect=10.0)
    http_client = httpx.AsyncClient(timeout=timeout)
    logger.info("全局共享 httpx.AsyncClient 已创建 (cre_image)。")


@router.on_event("shutdown")
async def shutdown_event():
    """在应用关闭时优雅地关闭 HTTP 客户端"""
    global http_client
    if http_client:
        await http_client.aclose()
        logger.info("全局共享 httpx.AsyncClient 已成功关闭 (cre_image)。")


class StandardResponse(BaseModel):
    """标准的API响应模型"""
    code: int = Field(200, description="HTTP状态码")
    message: str = Field("Success", description="响应消息")
    data: Optional[Any] = Field(None, description="响应数据")
    timestamp: str = Field(..., description="ISO 8601 格式的时间戳")


def create_standard_response(
        data: Optional[Any] = None,
        code: int = 200,
        message: str = "Success"
) -> JSONResponse:
    content = StandardResponse(
        code=code,
        message=message,
        data=data,
        timestamp=datetime.now().isoformat()
    ).model_dump(exclude_none=True)
    return JSONResponse(status_code=code, content=content)


# --- 工具函数 ---
async def get_gcloud_auth_token() -> str:
    """
    使用 google-auth 库获取应用默认凭证 (ADC)。
    """
    try:
        credentials, project_id = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)

        if not credentials.token:
            raise Exception("获取到的凭证中没有 token。")

        return credentials.token

    except Exception as e:
        logger.error(f"使用 google-auth 获取默认凭证失败: {e}")
        raise Exception(f"Failed to get application default credentials: {e}")


async def upload_to_gcs(image_data: bytes, content_type: str, folder: str = GCS_OUTPUT_DIR) -> str:
    """
    将二进制图片数据上传到 Google Cloud Storage 并返回公开 URL
    """
    filename = f"{folder}/{uuid.uuid4()}.png"
    upload_url = GCS_UPLOAD_ENDPOINT + filename

    auth_token = await get_gcloud_auth_token()
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": content_type
    }

    try:
        response = await http_client.post(upload_url, content=image_data, headers=headers)
        response.raise_for_status()

        # 构建公开 URL
        # 假设 Bucket 是公开可读的，或者通过 Signed URL (这里简化为直接拼接公开链接)
        public_url = f"{GCS_PUBLIC_URL_PREFIX}/{filename}"
        logger.info(f"图片已上传至 GCS: {public_url}")
        return public_url

    except httpx.HTTPStatusError as e:
        logger.error(f"GCS 上传失败: {e.response.status_code} - {e.response.text}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to upload image to storage.")


# --- Pydantic API 模型 ---

class ImageModelID(str, Enum):
    GEMINI_3_PRO_IMAGE_PREVIEW = "gemini-3-pro-image-preview"
    GEMINI_2_5_FLASH = "gemini-2.5-flash-image"
    GEMINI_3_1_FLASH_PREVIEW = "gemini-3.1-flash-image-preview"


class AspectRatio(str, Enum):
    SQUARE = "1:1"
    LANDSCAPE_16_9 = "16:9"
    PORTRAIT_9_16 = "9:16"
    LANDSCAPE_4_3 = "4:3"
    PORTRAIT_3_4 = "3:4"


class PersonGeneration(str, Enum):
    ALLOW_ADULT = "allow_adult"
    ALLOW_ALL = "allow_all"
    DISALLOW = "disallow"


class ResponseMimeType(str, Enum):
    PNG = "image/png"
    JPEG = "image/jpeg"


class SafetyThreshold(str, Enum):
    OFF = "OFF"
    BLOCK_LOW_AND_ABOVE = "BLOCK_LOW_AND_ABOVE"
    BLOCK_MEDIUM_AND_ABOVE = "BLOCK_MEDIUM_AND_ABOVE"
    BLOCK_ONLY_HIGH = "BLOCK_ONLY_HIGH"


class GenerateImagePayload(BaseModel):
    prompt: str = Field(..., description="用于指导图片生成的文本提示。", min_length=1)
    model_id: ImageModelID = Field(ImageModelID.GEMINI_3_1_FLASH_PREVIEW, description="要使用的模型ID。")

    # 配置参数
    response_mime_type: Optional[ResponseMimeType] = Field(ResponseMimeType.PNG, description="输出图片格式。")
    
    response_count: Optional[conint(ge=1, le=4)] = Field(1, description="生成的图片数量。")
    
    aspect_ratio: Optional[AspectRatio] = Field(AspectRatio.SQUARE, description="图片宽高比。")
    negative_prompt: Optional[str] = Field(None, description="负向提示词。")
    person_generation: Optional[PersonGeneration] = Field(PersonGeneration.ALLOW_ADULT, description="人物生成限制。")
    
    # 安全设置
    safety_filter_level: Optional[SafetyThreshold] = Field(SafetyThreshold.OFF, description="安全过滤级别。")

    class Config:
        use_enum_values = True


class ImageResult(BaseModel):
    public_url: str
    local_path: Optional[str] = None
    mime_type: str


class GenerateImageResponse(BaseModel):
    images: List[ImageResult] = []


# --- API 端点实现 ---

@router.post(
    "/generate_image",
    summary="通过文本提示生成图片 (Gemini)"
)
async def generate_image(payload: GenerateImagePayload):
    """
    调用 Google Gemini 模型生成图片，并上传至 GCS。
    """
    request_id = str(uuid.uuid4())
    logger.info(f"收到图片生成请求 [{request_id}], Model: {payload.model_id}, Prompt: '{payload.prompt[:50]}...'")

    try:
        # 1. 获取认证 Token
        auth_token = await get_gcloud_auth_token()
        headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        # 2. 构造请求体
        endpoint = VERTEX_API_ENDPOINT_TEMPLATE.format(model_id=payload.model_id)

        # 构造 Safety Settings
        safety_categories = [
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_HARASSMENT"
        ]
        safety_settings = [
            {"category": cat, "threshold": payload.safety_filter_level}
            for cat in safety_categories
        ]

        # 处理 Prompt (加入 aspect_ratio 和 negative_prompt)
        prompt_text = payload.prompt
        if payload.aspect_ratio:
            prompt_text += f" --aspect_ratio {payload.aspect_ratio}"
        if payload.negative_prompt:
            prompt_text += f" --negative_prompt {payload.negative_prompt}"
        if payload.person_generation:
             prompt_text += f" --person_generation {payload.person_generation}"

        request_body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt_text}]
                }
            ],
            "generationConfig": {
                "temperature": 1,
                "topP": 0.95,
                "maxOutputTokens": 32768,
                "responseModalities": ["IMAGE"], # Gemini Image API 仅支持 IMAGE 或 TEXT, IMAGE
                "candidateCount": payload.response_count,
                 # 简单区分 mediaResolution
                "mediaResolution": "MEDIA_RESOLUTION_LOW" if "flash" in payload.model_id else "MEDIA_RESOLUTION_HIGH",
            },
            "safetySettings": safety_settings
        }

        logger.debug(f"[{request_id}] 发送请求到 {endpoint}")

        response = await http_client.post(endpoint, headers=headers, json=request_body)
        response.raise_for_status()

        data = response.json()

        # 3. 解析响应并上传图片
        image_results = []

        candidates = data.get("candidates", [])
        if not candidates:
            logger.warning(f"[{request_id}] API 返回成功但没有 candidates: {data}")
            return create_standard_response(message="未生成任何内容", data={})

        for i, candidate in enumerate(candidates):
            parts = candidate.get("content", {}).get("parts", [])
            for j, part in enumerate(parts):
                # 检查是否有内联数据 (inlineData)
                inline_data = part.get("inlineData")
                if inline_data and inline_data.get("mimeType", "").startswith("image/"):
                    mime_type = inline_data.get("mimeType")
                    b64_data = inline_data.get("data")
                    
                    if b64_data:
                        # 解码
                        image_bytes = base64.b64decode(b64_data)

                        # 上传到 GCS
                        public_url = await upload_to_gcs(image_bytes, mime_type)

                        image_results.append(ImageResult(
                            public_url=public_url,
                            mime_type=mime_type,
                            local_path=None # 本地路径为空，因为直接上传到了GCS
                        ))

        if not image_results:
             logger.warning(f"[{request_id}] 在响应中未找到图片数据。完整响应: {data}")
             return create_standard_response(code=status.HTTP_500_INTERNAL_SERVER_ERROR, message="API响应中未找到图片")

        logger.info(f"[{request_id}] 成功生成 {len(image_results)} 张图片并上传至 GCS")

        return create_standard_response(
            data=GenerateImageResponse(images=image_results).model_dump(),
            message="图片生成成功"
        )

    except httpx.HTTPStatusError as e:
        error_detail = f"Google API 请求失败: {e.response.status_code} - {e.response.text}"
        logger.error(f"[{request_id}] {error_detail}")
        return create_standard_response(
            code=status.HTTP_502_BAD_GATEWAY,
            message=error_detail
        )
    except Exception as e:
        error_detail = f"图片生成过程中发生内部错误: {str(e)}"
        logger.exception(f"[{request_id}] {error_detail}")
        return create_standard_response(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=error_detail
        )
