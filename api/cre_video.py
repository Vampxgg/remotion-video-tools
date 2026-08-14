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

# 尝试导入您项目中的日志模块
# utils.logger 是仓库内必需模块，删除冗余 fallback；导入失败应直接报错暴露问题
from utils.logger import setup_module_logger

logger = setup_module_logger(__name__, "logs/video/veo.log")

router = APIRouter()

from utils.gcp_credentials import get_access_token, get_gcs_access_token  # noqa: E402,F401
from utils.gcp_project_pool import build_router, GcpProjectRuntime, GcpProjectRouter  # noqa: E402
from utils.settings import settings as _settings  # noqa: E402  (settings 单点入口)

# --- Google Vertex AI Veo API 配置 ---
# 必要时通过 .env 中的 GCP_*/GCS_*/CRE_VIDEO_* 覆盖
GOOGLE_PROJECT_ID = _settings.GCP_PROJECT_ID
GOOGLE_LOCATION_ID = _settings.GCP_LOCATION_ID
# 视频输出的 GCS URI（使用 workflow_id 创建独立目录）
GCS_OUTPUT_URI_TEMPLATE = _settings.CRE_VIDEO_GCS_OUTPUT_URI
# GCS 桶的公开访问 URL 前缀
GCS_PUBLIC_URL_PREFIX = _settings.GCS_PUBLIC_URL_PREFIX

# API 端点模板：参数化 project_id 与 location，以便多项目路由时 project 与所用
# access token 的 service account 项目成对切换（veo 长轮询要求 predict/fetch 锁定同项目）。
VEO_API_ENDPOINT_TEMPLATE = (
    "https://{location_id}-aiplatform.googleapis.com/v1/projects/{project_id}"
    "/locations/{location_id}/publishers/google/models/{model_id}"
)

# 轮询配置
POLLING_INTERVAL_SECONDS = _settings.CRE_VIDEO_POLLING_INTERVAL_SEC
POLLING_TIMEOUT_SECONDS = _settings.CRE_VIDEO_POLLING_TIMEOUT_SEC

# 多 GCP 项目路由器（进程内单例）。在 lifespan startup 组池；池为空则回退单项目。
project_router: Optional[GcpProjectRouter] = None

# 使用全局唯一的 httpx.AsyncClient 实例以获得更好的性能
# 我们将在应用的 startup/shutdown 事件中管理它
http_client: httpx.AsyncClient = None


# ─── 生命周期资源管理（lifespan_resources）───────────────────────────
# 旧版 @router.on_event 已 deprecated，改由 main.py 在 FastAPI lifespan
# 中通过 AsyncExitStack 进入；行为/资源乘数完全一致。
import contextlib  # noqa: E402


async def _startup_resources() -> None:
    global http_client, project_router
    timeout = httpx.Timeout(
        _settings.CRE_VIDEO_HTTPX_TIMEOUT,
        connect=_settings.CRE_VIDEO_HTTPX_CONNECT_TIMEOUT,
    )
    http_client = httpx.AsyncClient(timeout=timeout)
    # 组多项目生成池：池为空时 project_router.enabled=False，veo 回退单项目行为。
    project_router = build_router()
    if project_router.enabled:
        logger.info(
            f"veo 多项目负载均衡已启用，项目池={[p.name for p in project_router.projects]}"
        )
    else:
        logger.info(f"veo 多项目池为空，回退单项目行为 (project={GOOGLE_PROJECT_ID})。")
    logger.info("全局共享 httpx.AsyncClient 已创建。")


async def _shutdown_resources() -> None:
    global http_client, project_router
    if http_client:
        await http_client.aclose()
        logger.info("全局共享 httpx.AsyncClient 已成功关闭。")
    project_router = None


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
    """统一走 utils.gcp_credentials 共享凭证加载器（显式 SA / 回退 ADC）。"""
    return await get_access_token()


def _is_retryable_status(code: int) -> bool:
    """429 与 5xx 视为该项目暂时不可用，可换项目重试。"""
    return code == 429 or code >= 500


class _VeoSubmission:
    """一次 veo 提交成功后锁定的上下文：predict/fetch 必须锁定同一项目与凭证。

    veo 是长轮询：predictLongRunning 返回 operation_name 后，需对同一 project/location
    反复 fetchPredictOperation。operation 属于提交时的项目，故提交成功后必须锁定该项目，
    后续轮询用同一项目的 token（过期可经 runtime 重取）与同一 fetch endpoint。
    """

    def __init__(
        self,
        operation_name: str,
        location_id: str,
        model_id: str,
        project: Optional[GcpProjectRuntime],
    ):
        self.operation_name = operation_name
        self.location_id = location_id
        self.model_id = model_id
        self.project = project  # None 表示单项目回退模式
        self.fetch_endpoint = (
            VEO_API_ENDPOINT_TEMPLATE.format(
                location_id=location_id,
                project_id=(project.project_id if project else GOOGLE_PROJECT_ID),
                model_id=model_id,
            )
            + ":fetchPredictOperation"
        )

    async def auth_headers(self) -> Dict[str, str]:
        """取当前轮询用鉴权头：锁定项目重取 token（单项目模式走全局兜底）。"""
        token = (
            await self.project.get_access_token()
            if self.project is not None
            else await get_gcloud_auth_token()
        )
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }


async def submit_veo_task(
    workflow_id: str,
    model_id: str,
    request_body: Dict[str, Any],
) -> _VeoSubmission:
    """选项目 + 提交 veo 生成任务，跨项目做失败转移；提交成功后锁定该项目。

    - 池启用：按"最空闲优先"选健康项目，用其 token + 成对 endpoint 提交；
      429/5xx/网络异常 → 熔断该项目并换下一个重试（最多 router.max_attempts() 次）；
      4xx(非429) → 参数错误，直接抛出。
    - 池为空：回退单项目（GOOGLE_PROJECT_ID + 全局兜底凭证）。

    :raises httpx.HTTPStatusError / RuntimeError: 全部尝试失败时抛出，由调用方转 502。
    """
    location_id = GOOGLE_LOCATION_ID
    router = project_router

    if router is None or not router.enabled:
        headers = {
            "Authorization": f"Bearer {await get_gcloud_auth_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }
        endpoint = (
            VEO_API_ENDPOINT_TEMPLATE.format(
                location_id=location_id, project_id=GOOGLE_PROJECT_ID, model_id=model_id
            )
            + ":predictLongRunning"
        )
        resp = await http_client.post(endpoint, headers=headers, json=request_body)
        resp.raise_for_status()
        op = resp.json().get("name")
        if not op:
            raise RuntimeError("API 未返回有效的 operation name")
        return _VeoSubmission(op, location_id, model_id, None)

    max_attempts = router.max_attempts()
    excluded: set = set()
    last_exc: Optional[BaseException] = None

    for attempt in range(max_attempts):
        candidates = router.healthy_projects(excluded)
        if not candidates:
            logger.warning(
                f"[{workflow_id}] 无健康项目可用（已排除 {excluded}），尝试 {attempt}/{max_attempts}"
            )
            break
        project = candidates[0]
        excluded.add(project.name)
        try:
            token = await project.get_access_token()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            router.mark_failure(project, f"token 刷新失败: {e}")
            logger.warning(f"[{workflow_id}] project={project.name} token 刷新失败: {e}")
            continue

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        endpoint = (
            VEO_API_ENDPOINT_TEMPLATE.format(
                location_id=location_id, project_id=project.project_id, model_id=model_id
            )
            + ":predictLongRunning"
        )
        try:
            resp = await http_client.post(endpoint, headers=headers, json=request_body)
            resp.raise_for_status()
            op = resp.json().get("name")
            if not op:
                raise RuntimeError("API 未返回有效的 operation name")
            router.mark_success(project)
            logger.info(
                f"[{workflow_id}] veo 提交命中项目 {project.name} "
                f"(attempt {attempt + 1}/{max_attempts}) op={op}"
            )
            return _VeoSubmission(op, location_id, model_id, project)
        except httpx.HTTPStatusError as e:
            last_exc = e
            code = e.response.status_code
            if _is_retryable_status(code):
                router.mark_failure(project, f"HTTP {code}: {e.response.text[:300]}")
                logger.warning(
                    f"[{workflow_id}] project={project.name} 提交失败 {code}，换项目重试"
                )
                continue
            raise  # 4xx（非 429）参数/权限错误，换项目无益
        except Exception as e:  # noqa: BLE001（网络/超时等，换项目）
            last_exc = e
            router.mark_failure(project, f"异常: {e}")
            logger.warning(f"[{workflow_id}] project={project.name} 提交异常: {e}，换项目重试")
            continue

    if last_exc:
        raise last_exc
    raise RuntimeError("veo 提交失败：所有项目均不可用")


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
    # 根因：use_enum_values=True 不作用于「默认值」，若默认值写成枚举成员，未显式传
    # model_id（如 Dify 调用）时会保持为枚举对象，拼进 Veo endpoint 得到
    # "VeoModelID.XXX" 导致 400 Invalid Endpoint name。默认值直接取 .value。
    model_id: VeoModelID = Field(VeoModelID.VEO_2_0_GENERATE.value, description="要使用的Veo模型ID。")

    # 可选参数
    duration_sec: Optional[conint(ge=4, le=8)] = Field(8,
                                                       description="生成视频的时长（秒）。Veo 2: 5-8s; Veo 3: 4, 6, or 8s。")
    response_count: Optional[conint(ge=1, le=4)] = Field(1, description="要生成的视频文件数量。")
    aspect_ratio: Optional[AspectRatio] = Field(AspectRatio.LANDSCAPE.value, description="生成视频的宽高比。")
    negative_prompt: Optional[str] = Field(None, description="希望模型避免生成的内容。")
    person_generation: Optional[PersonGeneration] = Field(PersonGeneration.ALLOW_ADULT.value, description="人物生成安全设置。")
    resolution: Optional[Resolution] = Field(Resolution.HD_720P.value, description="生成视频的分辨率（仅Veo 3模型支持）。")
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
    # 默认值取 .value，理由同 GenerateVideoPayload（避免枚举对象拼进 endpoint）。
    model_id: VeoModelID = Field(VeoModelID.VEO_2_0_GENERATE.value, description="要使用的Veo模型ID。")
    duration_sec: Optional[conint(ge=4, le=8)] = Field(8, description="生成视频的时长（秒）。")
    response_count: Optional[conint(ge=1, le=4)] = Field(1, description="每个提示要生成的视频文件数量。")
    aspect_ratio: Optional[AspectRatio] = Field(AspectRatio.LANDSCAPE.value, description="生成视频的宽高比。")
    negative_prompt: Optional[str] = Field(None, description="希望模型避免生成的内容。")
    person_generation: Optional[PersonGeneration] = Field(PersonGeneration.ALLOW_ADULT.value, description="人物生成安全设置。")
    resolution: Optional[Resolution] = Field(Resolution.HD_720P.value, description="生成视频的分辨率（仅Veo 3模型支持）。")
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
        # 1. 构造请求体
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

        # 2. 选项目 + 提交任务（池启用则跨项目失败转移；提交成功后锁定该项目）
        submission = await submit_veo_task(payload.workflow_id, payload.model_id, request_body)
        operation_name = submission.operation_name
        logger.info(f"任务提交成功, Workflow ID: {payload.workflow_id}. Operation Name: {operation_name}")

        # 3. 轮询任务结果（锁定提交时的项目/凭证/endpoint）
        fetch_endpoint = submission.fetch_endpoint
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

            # 每次轮询取锁定项目的最新 token（token 会过期，runtime 内部按需刷新）。
            poll_headers = await submission.auth_headers()
            poll_response = await http_client.post(fetch_endpoint, headers=poll_headers,
                                                   json={"operationName": operation_name})
            poll_response.raise_for_status()

            data = poll_response.json()
            if data.get("done"):
                logger.info(f"任务完成! Workflow ID: {payload.workflow_id}")

                # done 之后必须无条件结束轮询并返回：Veo 完成响应形如
                # {"done": true, "response": {"videos": [{"gcsUri": ...}], "raiMediaFilteredCount": N}}。
                # 若被 RAI 安全策略过滤，done=true 但 videos 为空——此时应返回明确失败，
                # 绝不能掉回下面的 sleep 继续轮询（否则会一直「完成」到超时，下游收到 504）。
                response_data = data.get("response", {})
                if data.get("error"):
                    err = data["error"]
                    error_message = f"Veo 任务失败: {err.get('message', err)}"
                    logger.error(f"Workflow ID: {payload.workflow_id}, {error_message}")
                    return create_standard_response(
                        code=status.HTTP_502_BAD_GATEWAY, message=error_message
                    )

                videos_data = response_data.get("videos", [])
                video_results = [
                    VideoResult(
                        public_url=convert_gcs_to_public_url(item["gcsUri"]),
                        gcs_uri=item["gcsUri"],
                        mime_type=item.get("mimeType", "video/mp4"),
                    )
                    for item in videos_data
                    if item.get("gcsUri")
                ]

                if not video_results:
                    filtered = response_data.get("raiMediaFilteredCount", 0)
                    reasons = response_data.get("raiMediaFilteredReasons", [])
                    error_message = (
                        f"任务已完成但未返回视频（可能被安全策略过滤）。"
                        f"raiMediaFilteredCount={filtered}"
                        + (f", reasons={reasons}" if reasons else "")
                    )
                    logger.error(f"Workflow ID: {payload.workflow_id}, {error_message}")
                    return create_standard_response(
                        code=status.HTTP_502_BAD_GATEWAY, message=error_message
                    )

                success_data = GenerateVideoResponse(
                    workflow_id=payload.workflow_id,
                    videos=video_results,
                )
                return create_standard_response(
                    data=success_data.model_dump(),
                    message="视频生成成功",
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
        # 1. 构造批量请求体
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
        # 2. 选项目 + 提交任务（池启用则跨项目失败转移；提交成功后锁定该项目）
        submission = await submit_veo_task(payload.workflow_id, payload.model_id, request_body)
        operation_name = submission.operation_name
        logger.info(f"批量任务提交成功, Workflow ID: {payload.workflow_id}. Operation Name: {operation_name}")
        # 3. 轮询任务结果（锁定提交时的项目/凭证/endpoint）
        fetch_endpoint = submission.fetch_endpoint
        start_time = asyncio.get_event_loop().time()
        while True:
            elapsed_time = asyncio.get_event_loop().time() - start_time
            if elapsed_time > POLLING_TIMEOUT_SECONDS:
                return create_standard_response(code=status.HTTP_504_GATEWAY_TIMEOUT,
                                                message="Video generation task timed out.")
            logger.info(f"正在轮询批量任务状态... Workflow ID: {payload.workflow_id} (已用时 {int(elapsed_time)}s)")
            poll_headers = await submission.auth_headers()
            poll_response = await http_client.post(fetch_endpoint, headers=poll_headers,
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
