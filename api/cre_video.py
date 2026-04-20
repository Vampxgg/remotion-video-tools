# -*- coding: utf-8 -*-
# @File：Online_search/video/main_app.py
# @Time：2025/8/6 10:00
# @Author：_不咬闰土的猹丶 & AI Assistant
# @email：hx1561958968@gmail.com

# --- 导入模块 ---
import asyncio
import logging
import sys
import os
from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime
import httpx
from fastapi.responses import JSONResponse
from fastapi import APIRouter, HTTPException, status, Body
from pydantic import BaseModel, Field, conint, confloat, constr
import google.auth
import google.auth.transport.requests

# 尝试导入您项目中的日志模块
# utils.logger 是仓库内必需模块，删除冗余 fallback；导入失败应直接报错暴露问题
from utils.logger import setup_module_logger

logger = setup_module_logger(__name__, "logs/video/veo.log")

router = APIRouter()

from utils.settings import settings as _settings  # noqa: E402  (settings 单点入口)

# --- Google Vertex AI Veo API 配置 ---
# 必要时通过 .env 中的 GCP_*/GCS_*/CRE_VIDEO_* 覆盖
GOOGLE_PROJECT_ID = _settings.GCP_PROJECT_ID
GOOGLE_LOCATION_ID = _settings.GCP_LOCATION_ID
# 视频输出的 GCS URI（使用 workflow_id 创建独立目录）
GCS_OUTPUT_URI_TEMPLATE = _settings.CRE_VIDEO_GCS_OUTPUT_URI
# GCS 桶的公开访问 URL 前缀
GCS_PUBLIC_URL_PREFIX = _settings.GCS_PUBLIC_URL_PREFIX

# API 端点模板
VEO_API_ENDPOINT_TEMPLATE = (
    f"https://{GOOGLE_LOCATION_ID}-aiplatform.googleapis.com/v1/projects/{GOOGLE_PROJECT_ID}"
    f"/locations/{GOOGLE_LOCATION_ID}/publishers/google/models/{{model_id}}"
)

# 轮询配置
POLLING_INTERVAL_SECONDS = _settings.CRE_VIDEO_POLLING_INTERVAL_SEC
POLLING_TIMEOUT_SECONDS = _settings.CRE_VIDEO_POLLING_TIMEOUT_SEC

# 使用全局唯一的 httpx.AsyncClient 实例以获得更好的性能
# 我们将在应用的 startup/shutdown 事件中管理它
http_client: httpx.AsyncClient = None


# ─── 生命周期资源管理（lifespan_resources）───────────────────────────
# 旧版 @router.on_event 已 deprecated，改由 main.py 在 FastAPI lifespan
# 中通过 AsyncExitStack 进入；行为/资源乘数完全一致。
import contextlib  # noqa: E402


async def _startup_resources() -> None:
    global http_client
    timeout = httpx.Timeout(
        _settings.CRE_VIDEO_HTTPX_TIMEOUT,
        connect=_settings.CRE_VIDEO_HTTPX_CONNECT_TIMEOUT,
    )
    http_client = httpx.AsyncClient(timeout=timeout)
    logger.info("全局共享 httpx.AsyncClient 已创建。")


async def _shutdown_resources() -> None:
    global http_client
    if http_client:
        await http_client.aclose()
        logger.info("全局共享 httpx.AsyncClient 已成功关闭。")


@contextlib.asynccontextmanager
async def lifespan_resources(app):
    await _startup_resources()
    try:
        yield
    finally:
        await _shutdown_resources()


# 统一从 utils.responses 引入；本 router 历史行为是 model_dump(exclude_none=True)，
# 因此用一层薄包装显式打开 exclude_none，对外接口字段集合保持完全不变
from utils.responses import StandardResponse  # noqa: F401
from utils.responses import create_standard_response as _shared_create_standard_response


def create_standard_response(
        data: Optional[Any] = None,
        code: int = 200,
        message: str = "Success"
) -> JSONResponse:
    return _shared_create_standard_response(
        data=data, code=code, message=message, exclude_none=True
    )


# --- 工具函数 ---
async def get_gcloud_auth_token() -> str:
    """
    [最佳实践] 使用 google-auth 库获取应用默认凭证 (ADC)。
    这种方法无需调用外部 gcloud 命令，稳定且跨平台。
    """
    try:
        # 自动查找凭证 (来自 `gcloud auth application-default login` 或环境变量)
        credentials, project_id = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])

        # 刷新凭证以确保它是有效的
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)

        if not credentials.token:
            raise Exception("获取到的凭证中没有 token。")

        logger.info("成功通过 google-auth 库获取并刷新了 Access Token。")
        return credentials.token

    except Exception as e:
        logger.error(f"使用 google-auth 获取默认凭证失败: {e}")
        logger.error("请确保您已运行 'gcloud auth application-default login' 或已正确设置服务账户环境变量。")
        raise Exception(f"Failed to get application default credentials: {e}")


def convert_gcs_to_public_url(gcs_uri: str) -> str:
    """将 gs://bucket/object/path 格式转换为公开可访问的 URL"""
    if not gcs_uri.startswith("gs://"):
        return gcs_uri

    # 移除 "gs://" 前缀并分割 bucket 和 object_path
    path_without_prefix = gcs_uri[5:]
    bucket_name, _, object_path = path_without_prefix.partition('/')

    # 使用配置中的公开URL前缀构建最终URL
    # 这里我们假设桶名已经包含在 prefix 中了，如果不是，需要调整
    # 例如：f"https://storage.googleapis.com/{bucket_name}/{object_path}"
    return f"{GCS_PUBLIC_URL_PREFIX}/{object_path}"


# --- Pydantic API 模型 ---

class VeoModelID(str, Enum):
    VEO_2_0_GENERATE = "veo-2.0-generate-001"
    VEO_3_0_GENERATE = "veo-3.0-generate-001"
    VEO_3_0_FAST_GENERATE = "veo-3.0-fast-generate-001"
    VEO_3_0_PREVIEW = "veo-3.0-generate-preview"
    VEO_3_0_FAST_PREVIEW = "veo-3.0-fast-generate-preview"


class AspectRatio(str, Enum):
    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"


class PersonGeneration(str, Enum):
    ALLOW_ADULT = "allow_adult"
    DISALLOW = "disallow"


class Resolution(str, Enum):
    HD_720P = "720p"
    HD_1080P = "1080p"


class GenerateVideoPayload(BaseModel):
    workflow_id: str = Field(..., description="用于追踪和存储的唯一工作流ID。")
    prompt: str = Field(..., description="用于指导视频生成的文本提示。", min_length=1)
    model_id: VeoModelID = Field(VeoModelID.VEO_2_0_GENERATE, description="要使用的Veo模型ID。")

    # 可选参数
    duration_sec: Optional[conint(ge=4, le=8)] = Field(8,
                                                       description="生成视频的时长（秒）。Veo 2: 5-8s; Veo 3: 4, 6, or 8s。")
    response_count: Optional[conint(ge=1, le=4)] = Field(1, description="要生成的视频文件数量。")
    aspect_ratio: Optional[AspectRatio] = Field(AspectRatio.LANDSCAPE, description="生成视频的宽高比。")
    negative_prompt: Optional[str] = Field(None, description="希望模型避免生成的内容。")
    person_generation: Optional[PersonGeneration] = Field(PersonGeneration.ALLOW_ADULT, description="人物生成安全设置。")
    resolution: Optional[Resolution] = Field(Resolution.HD_720P, description="生成视频的分辨率（仅Veo 3模型支持）。")
    seed: Optional[conint(ge=0, le=4294967295)] = Field(None, description="用于生成确定性视频的种子。")

    class Config:
        use_enum_values = True


class PromptItem(BaseModel):
    task_id: str = Field(..., description="用于追踪的自定义唯一ID，例如字幕ID。")
    prompt: constr(min_length=1) = Field(..., description="该视频的文本提示。")


# 【V3.0 批量新增】批量生成视频的请求模型
class BatchGenerateVideoPayload(BaseModel):
    workflow_id: str = Field(..., description="整个批量任务的唯一工作流ID。")
    prompts: List[PromptItem] = Field(..., description="包含多个提示的列表。", min_length=1)

    # 以下为本批次所有视频共享的参数
    model_id: VeoModelID = Field(VeoModelID.VEO_2_0_GENERATE, description="要使用的Veo模型ID。")
    duration_sec: Optional[conint(ge=4, le=8)] = Field(8, description="生成视频的时长（秒）。")
    response_count: Optional[conint(ge=1, le=4)] = Field(1, description="每个提示要生成的视频文件数量。")
    aspect_ratio: Optional[AspectRatio] = Field(AspectRatio.LANDSCAPE, description="生成视频的宽高比。")
    negative_prompt: Optional[str] = Field(None, description="希望模型避免生成的内容。")
    person_generation: Optional[PersonGeneration] = Field(PersonGeneration.ALLOW_ADULT, description="人物生成安全设置。")
    resolution: Optional[Resolution] = Field(Resolution.HD_720P, description="生成视频的分辨率（仅Veo 3模型支持）。")
    seed: Optional[conint(ge=0, le=4294967295)] = Field(None, description="用于生成确定性视频的种子。")

    class Config:
        use_enum_values = True


class VideoResult(BaseModel):
    public_url: str
    gcs_uri: str
    mime_type: str


class BatchVideoResult(BaseModel):
    prompt_id: str
    videos: List[VideoResult] = []
    error: Optional[str] = None


class GenerateVideoResponse(BaseModel):
    workflow_id: str
    videos: List[VideoResult] = []
    results: List[BatchVideoResult] = []


# --- API 端点实现 ---

@router.post(
    "/generate_video",
    summary="通过文本提示生成视频"
)
async def generate_video(payload: GenerateVideoPayload):
    """
    接收文本提示和配置，调用 Google Veo API 生成视频。
    这是一个长轮询过程的封装：
    1. 提交生成任务。
    2. 轮询任务状态直到完成或超时。
    3. 返回生成的视频的公开访问链接。
    """
    logger.info(f"收到视频生成请求，Workflow ID: {payload.workflow_id}, Prompt: '{payload.prompt[:50]}...'")

    try:
        # 1. 获取认证 Token
        auth_token = await get_gcloud_auth_token()
        headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        # 2. 构造请求体并提交任务
        predict_endpoint = VEO_API_ENDPOINT_TEMPLATE.format(model_id=payload.model_id) + ":predictLongRunning"

        request_body = {
            "instances": [{"prompt": payload.prompt}],
            "parameters": {
                # 根据文档，duration是在parameters里的，但您的文档没有显示，这里加上以防万一
                # "duration": payload.duration_sec,
                "storageUri": GCS_OUTPUT_URI_TEMPLATE,
                "sampleCount": payload.response_count,
                "aspectRatio": payload.aspect_ratio,
                "personGeneration": payload.person_generation,
                # 其他可选参数
                **({"negativePrompt": payload.negative_prompt} if payload.negative_prompt else {}),
                **({"resolution": payload.resolution} if payload.model_id.startswith("veo-3.0") else {}),
                **({"seed": payload.seed} if payload.seed is not None else {}),
            }
        }

        logger.debug(f"向 {predict_endpoint} 发送请求体: {request_body}")

        init_response = await http_client.post(predict_endpoint, headers=headers, json=request_body)
        init_response.raise_for_status()  # 如果状态码不是 2xx，则抛出异常

        operation_name = init_response.json().get("name")
        if not operation_name:
            error_message = "API 未返回有效的 operation name"
            return create_standard_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=error_message
            )

        logger.info(f"任务提交成功, Workflow ID: {payload.workflow_id}. Operation Name: {operation_name}")

        # 3. 轮询任务结果
        fetch_endpoint = VEO_API_ENDPOINT_TEMPLATE.format(model_id=payload.model_id) + ":fetchPredictOperation"
        start_time = asyncio.get_event_loop().time()

        while True:
            # 检查是否超时
            elapsed_time = asyncio.get_event_loop().time() - start_time
            if elapsed_time > POLLING_TIMEOUT_SECONDS:
                logger.error(f"任务轮询超时, Workflow ID: {payload.workflow_id}")
                error_message = "Video generation task timed out."
                return create_standard_response(
                    code=status.HTTP_504_GATEWAY_TIMEOUT,
                    message=error_message
                )

            logger.info(f"正在轮询任务状态... Workflow ID: {payload.workflow_id} (已用时 {int(elapsed_time)}s)")

            poll_response = await http_client.post(fetch_endpoint, headers=headers,
                                                   json={"operationName": operation_name})
            poll_response.raise_for_status()

            data = poll_response.json()
            if data.get("done"):
                logger.info(f"任务完成! Workflow ID: {payload.workflow_id}")

                response_data = data.get("response", {})
                videos_data = response_data.get("videos", [])

                video_results = []
                for video_item in videos_data:
                    gcs_uri = video_item.get("gcsUri")
                    if gcs_uri:
                        video_results.append(
                            VideoResult(
                                public_url=convert_gcs_to_public_url(gcs_uri),
                                gcs_uri=gcs_uri,
                                mime_type=video_item.get("mimeType", "video/mp4")
                            )
                        )

                        # 构建原始成功数据体
                        success_data = GenerateVideoResponse(
                            workflow_id=payload.workflow_id,
                            videos=video_results
                        )

                        return create_standard_response(
                            data=success_data.model_dump(),
                            message="视频生成成功"
                        )

            # 等待指定间隔后再次轮询
            await asyncio.sleep(POLLING_INTERVAL_SECONDS)

    except httpx.HTTPStatusError as e:
        error_detail = f"Google API 请求失败: {e.response.status_code} - {e.response.text}"
        logger.error(f"Workflow ID: {payload.workflow_id}, {error_detail}")
        return create_standard_response(
            code=status.HTTP_502_BAD_GATEWAY,
            message=error_detail
        )
    except Exception as e:
        error_detail = f"视频生成过程中发生内部错误: {str(e)}"
        logger.exception(f"Workflow ID: {payload.workflow_id}, {error_detail}")  # 使用 exception 记录堆栈
        return create_standard_response(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=error_detail
        )


@router.post(
    "/generate_videos_batch",
    summary="通过多个文本提示批量生成视频",
)
async def generate_videos_batch(payload: BatchGenerateVideoPayload):
    """
    接收一个提示列表，一次性调用 Google Veo API 生成多个视频。
    所有视频共享相同的配置参数（时长、分辨率等）。
    """
    logger.info(f"收到批量视频生成请求，Workflow ID: {payload.workflow_id}, 提示数量: {len(payload.prompts)}")
    try:
        # 1. 获取认证 Token
        auth_token = await get_gcloud_auth_token()
        headers = {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json; charset=utf-8"}
        # 2. 构造批量请求体并提交任务
        predict_endpoint = VEO_API_ENDPOINT_TEMPLATE.format(model_id=payload.model_id) + ":predictLongRunning"

        # 将每个 PromptItem 转换为 API 需要的格式
        instances = [{"prompt": item.prompt} for item in payload.prompts]

        request_body = {
            "instances": instances,
            "parameters": {
                "storageUri": GCS_OUTPUT_URI_TEMPLATE,
                "sampleCount": payload.response_count,
                "aspectRatio": payload.aspect_ratio,
                "personGeneration": payload.person_generation,
                **({"negativePrompt": payload.negative_prompt} if payload.negative_prompt else {}),
                **({"resolution": payload.resolution} if payload.model_id.startswith("veo-3.0") else {}),
                **({"seed": payload.seed} if payload.seed is not None else {}),
            }
        }
        logger.debug(f"向 {predict_endpoint} 发送批量请求体: {request_body}")
        init_response = await http_client.post(predict_endpoint, headers=headers, json=request_body)
        init_response.raise_for_status()
        operation_name = init_response.json().get("name")
        if not operation_name:
            return create_standard_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message="API 未返回有效的 operation name"
            )
        logger.info(f"批量任务提交成功, Workflow ID: {payload.workflow_id}. Operation Name: {operation_name}")
        # 3. 轮询任务结果
        fetch_endpoint = VEO_API_ENDPOINT_TEMPLATE.format(model_id=payload.model_id) + ":fetchPredictOperation"
        start_time = asyncio.get_event_loop().time()
        while True:
            elapsed_time = asyncio.get_event_loop().time() - start_time
            if elapsed_time > POLLING_TIMEOUT_SECONDS:
                return create_standard_response(code=status.HTTP_504_GATEWAY_TIMEOUT,
                                                message="Video generation task timed out.")
            logger.info(f"正在轮询批量任务状态... Workflow ID: {payload.workflow_id} (已用时 {int(elapsed_time)}s)")
            poll_response = await http_client.post(fetch_endpoint, headers=headers,
                                                   json={"operationName": operation_name})
            poll_response.raise_for_status()
            data = poll_response.json()
            if data.get("done"):
                logger.info(f"批量任务完成! Workflow ID: {payload.workflow_id}")

                # Google API 返回的 videos 列表与输入的 instances 列表是按顺序对应的
                all_videos_data = data.get("response", {}).get("videos", [])

                # 创建一个字典来映射 prompt_id 到结果
                results_map = {item.id: BatchVideoResult(prompt_id=item.id) for item in payload.prompts}
                # 假设 response_count=1，API返回的video数量应等于prompt数量
                # 如果 response_count>1, API返回 video数量 = prompt数量 * response_count
                if len(all_videos_data) == len(payload.prompts) * payload.response_count:
                    for i, prompt_item in enumerate(payload.prompts):
                        # 为当前prompt提取对应的video切片
                        start_index = i * payload.response_count
                        end_index = start_index + payload.response_count
                        prompt_videos_data = all_videos_data[start_index:end_index]

                        video_results = [
                            VideoResult(
                                public_url=convert_gcs_to_public_url(item.get("gcsUri")),
                                gcs_uri=item.get("gcsUri"),
                                mime_type=item.get("mimeType", "video/mp4")
                            ) for item in prompt_videos_data if item.get("gcsUri")
                        ]
                        results_map[prompt_item.id].videos = video_results
                else:
                    logger.warning(
                        f"API返回的视频数量 ({len(all_videos_data)}) 与预期的 "
                        f"({len(payload.prompts) * payload.response_count}) 不匹配。"
                        "可能部分任务失败。将尝试按顺序分配，未匹配的将为空。"
                    )
                    # 即使数量不匹配，也尽力按顺序分配
                    for i, prompt_item in enumerate(payload.prompts):
                        start_index = i * payload.response_count
                        end_index = start_index + payload.response_count
                        if start_index < len(all_videos_data):
                            prompt_videos_data = all_videos_data[start_index:end_index]
                            results_map[prompt_item.id].videos = [
                                VideoResult(
                                    public_url=convert_gcs_to_public_url(item.get("gcsUri")),
                                    gcs_uri=item.get("gcsUri"),
                                    mime_type=item.get("mimeType", "video/mp4")
                                ) for item in prompt_videos_data if item.get("gcsUri")
                            ]
                success_data = GenerateVideoResponse(
                    workflow_id=payload.workflow_id,
                    status="completed",
                    results=list(results_map.values())
                )
                return create_standard_response(data=success_data.model_dump(), message="批量视频生成成功")
            await asyncio.sleep(POLLING_INTERVAL_SECONDS)
    except httpx.HTTPStatusError as e:
        error_detail = f"Google API 请求失败: {e.response.status_code} - {e.response.text}"
        logger.error(f"Workflow ID: {payload.workflow_id}, {error_detail}")
        return create_standard_response(code=status.HTTP_502_BAD_GATEWAY, message=error_detail)
    except Exception as e:
        error_detail = f"视频生成过程中发生内部错误: {str(e)}"
        logger.exception(f"Workflow ID: {payload.workflow_id}, {error_detail}")
        return create_standard_response(code=status.HTTP_500_INTERNAL_SERVER_ERROR, message=error_detail)
