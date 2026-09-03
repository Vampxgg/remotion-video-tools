# -*- coding: utf-8 -*-
# @File：api/cre_video_omni.py
# @Author：AI Assistant
# @email：hx1561958968@gmail.com
"""Gemini Omni Flash 视频生成 / 会话式编辑接入（Vertex / Gemini Enterprise Agent Platform）。

与 api/cre_video.py（Veo）的关键区别（务必区分，二者不可混用）：
  - 端点：Omni 走 Interactions API
    ``POST https://aiplatform.googleapis.com/v1beta1/projects/{project}/locations/global/interactions``
    （location 固定 ``global``）；Veo 走
    ``{region}-aiplatform.googleapis.com/v1/.../models/{id}:predictLongRunning``。
  - 请求体：Omni 是 ``input`` / ``response_format`` / ``generation_config.video_config``；
    Veo 是 ``instances`` / ``parameters``。
  - 轮询：Omni 是 ``GET .../interactions/{id}`` 看 ``status``；
    Veo 是 ``:fetchPredictOperation`` 看 ``done``。
  - 结果：Omni 在 ``steps[].content[]`` 里 ``type=="video"`` 的 ``uri``/``data``；
    Veo 在 ``response.videos[].gcsUri``。

复用现有基础设施：
  - 鉴权：Omni 同样用 cloud-platform access token（``Bearer``），与 Veo/出图共用
    ``utils.gcp_project_pool`` 的多项目池；提交成功后锁定该项目做后续轮询（op 属提交项目）。
  - 存储：``delivery="uri"`` + ``gcs_uri`` 让 Omni 直接把视频写入现有 GCS 桶；若返回
    inline base64（见下方"轮询返回 base64"坑），则用 GCS 上传专用凭证回传桶后给公开 URL。

关键坑位（均来自官方文档，已核实）：
  - **轮询返回 base64**：官方明确 GET/POST 轮询即使当初 ``delivery:"uri"`` 也会返回 inline
    base64，``uri`` 只保证出现在**创建响应**/SSE 中。故本模块 background 轮询到 completed 后，
    优先取 ``uri``，缺失则把 ``data``(base64) 解码上传 GCS 兜底，保证对外始终返回可访问 URL。
  - **编辑链约束**：``previous_interaction_id`` 指向的交互必须 ``completed``（in_progress 会
    400），且 ``store`` 不能为 false（否则不可继续编辑）。
  - **RAI 过滤**：``completed`` 但 steps 内无 video content 时，判为被安全策略过滤，返回明确失败。
  - **区域限制**：编辑用户上传的视频在 EEA/瑞士/英国/部分美国州被封（请求成功但空输出）；
    编辑模型自己生成的视频不受限——项目跑在 global，编辑链走生成物即可规避。
  - **成本**：≈$0.10/秒 720p，每次 edit 重渲染整段重新计费。
"""

import asyncio
import base64
import contextlib
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, conint

from utils.logger import setup_module_logger

logger = setup_module_logger(__name__, "logs/video/omni.log")

router = APIRouter()

from utils.gcp_credentials import get_gcs_access_token  # noqa: E402
from utils.gcp_project_pool import (  # noqa: E402
    build_router,
    GcpProjectRuntime,
    GcpProjectRouter,
    CAPABILITY_OMNI,
)
from utils.settings import settings as _settings  # noqa: E402
from utils.responses import create_standard_response as _shared_create_standard_response  # noqa: E402

# --- 配置 ---
GOOGLE_PROJECT_ID = _settings.GCP_PROJECT_ID
GCS_OMNI_OUTPUT_URI = _settings.CRE_OMNI_GCS_OUTPUT_URI
GCS_PUBLIC_URL_PREFIX = _settings.GCS_PUBLIC_URL_PREFIX
GCS_BUCKET_NAME = _settings.GCS_BUCKET_NAME

POLLING_INTERVAL_SECONDS = _settings.CRE_OMNI_POLLING_INTERVAL_SEC
POLLING_TIMEOUT_SECONDS = _settings.CRE_OMNI_POLLING_TIMEOUT_SEC

# Interactions API 端点：location 固定 global（Omni 仅在 global 提供）。
OMNI_INTERACTIONS_ENDPOINT_TEMPLATE = (
    "https://aiplatform.googleapis.com/v1beta1/projects/{project_id}"
    "/locations/global/interactions"
)

# base64 兜底上传：把解码后的视频回写 GCS 桶（存储凭证与生成凭证解耦，见 gcp_credentials）。
GCS_UPLOAD_ENDPOINT = (
    f"https://storage.googleapis.com/upload/storage/v1/b/{GCS_BUCKET_NAME}/o"
    f"?uploadType=media&name="
)
# 兜底上传目录：从 CRE_OMNI_GCS_OUTPUT_URI 解析出对象前缀，保证与 uri delivery 落同一目录风格。
_OMNI_OBJECT_PREFIX = GCS_OMNI_OUTPUT_URI.replace("gs://", "").partition("/")[2].strip("/") or "omni_video"

project_router: Optional[GcpProjectRouter] = None
http_client: Optional[httpx.AsyncClient] = None


def create_standard_response(
    data: Optional[Any] = None,
    code: int = 200,
    message: str = "Success",
) -> JSONResponse:
    """与 cre_video 行为一致：exclude_none=True。"""
    return _shared_create_standard_response(
        data=data, code=code, message=message, exclude_none=True
    )


# ─── 生命周期资源（与 cre_video 一致：建全局 http_client + 组多项目池）───────────────
async def _startup_resources() -> None:
    global http_client, project_router
    timeout = httpx.Timeout(
        _settings.CRE_OMNI_HTTPX_TIMEOUT,
        connect=_settings.CRE_OMNI_HTTPX_CONNECT_TIMEOUT,
    )
    http_client = httpx.AsyncClient(timeout=timeout)
    project_router = build_router()
    if project_router.enabled:
        omni_projects = [p.name for p in project_router.projects_with_capability(CAPABILITY_OMNI)]
        logger.info(f"omni 多项目池已启用，具备 omni 能力的项目={omni_projects}")
    else:
        logger.info(f"omni 多项目池为空，回退单项目行为 (project={GOOGLE_PROJECT_ID})。")
    logger.info("omni 全局共享 httpx.AsyncClient 已创建。")


async def _shutdown_resources() -> None:
    global http_client, project_router
    if http_client:
        await http_client.aclose()
        logger.info("omni 全局共享 httpx.AsyncClient 已关闭。")
    project_router = None


@contextlib.asynccontextmanager
async def lifespan_resources(app):
    await _startup_resources()
    try:
        yield
    finally:
        await _shutdown_resources()


def _is_retryable_status(code: int) -> bool:
    """429 与 5xx 视为该项目暂时不可用，可换项目重试。"""
    return code == 429 or code >= 500


def convert_gcs_to_public_url(gcs_uri: str) -> str:
    """gs://bucket/object → 公开 URL（与 cre_video 同源逻辑）。"""
    if not gcs_uri.startswith("gs://"):
        return gcs_uri
    object_path = gcs_uri[5:].partition("/")[2]
    return f"{GCS_PUBLIC_URL_PREFIX}/{object_path}"


# --- Pydantic 模型 ---

class OmniModelID(str, Enum):
    # 增强版（2026-08-27）：支持 360p/720p/1080p/4k、图生/参考/首尾帧/扩展/编辑。推荐默认。
    OMNI_1_1_FLASH_PREVIEW = "gemini-omni-1.1-flash-preview"
    # 初版（2026-06-30）：仅 720p，仅 text_to_video + edit。
    OMNI_FLASH_PREVIEW = "gemini-omni-flash-preview"


class OmniTask(str, Enum):
    TEXT_TO_VIDEO = "text_to_video"
    IMAGE_TO_VIDEO = "image_to_video"
    REFERENCE_TO_VIDEO = "reference_to_video"
    EDIT = "edit"


class OmniAspectRatio(str, Enum):
    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"


class OmniResolution(str, Enum):
    # 360p/1080p/4k 仅 gemini-omni-1.1-flash-preview 支持；gemini-omni-flash-preview 仅 720p。
    P360 = "360p"
    P720 = "720p"
    P1080 = "1080p"
    P4K = "4k"


class OmniImageInput(BaseModel):
    """参考图/首帧图输入：优先 gcs uri（无需上传），也可传 base64。二选一。"""
    uri: Optional[str] = Field(None, description="图片的 GCS URI，如 gs://bucket/path.png。")
    data: Optional[str] = Field(None, description="图片的 base64 数据（uri 缺省时使用）。")
    mime_type: str = Field("image/png", description="图片 MIME，如 image/png、image/jpeg。")


class GenerateOmniVideoPayload(BaseModel):
    """Omni 文生 / 图生 / 参考生视频请求。"""
    workflow_id: str = Field(..., description="用于追踪的唯一工作流 ID。")
    prompt: str = Field(..., description="指导视频生成的文本提示。", min_length=1)
    model_id: OmniModelID = Field(
        OmniModelID.OMNI_1_1_FLASH_PREVIEW.value,
        description="Omni 模型 ID，默认 gemini-omni-1.1-flash-preview。",
    )
    task: OmniTask = Field(
        OmniTask.TEXT_TO_VIDEO.value,
        description="视频生成任务模式；不确定时模型会依据 prompt/输入自动推断。",
    )
    aspect_ratio: OmniAspectRatio = Field(
        OmniAspectRatio.LANDSCAPE.value, description="宽高比 16:9 或 9:16。"
    )
    resolution: OmniResolution = Field(
        OmniResolution.P720.value,
        description="输出分辨率；360p/1080p/4k 仅 1.1 版支持，flash-preview 仅 720p。",
    )
    duration_sec: conint(ge=3, le=10) = Field(8, description="视频时长秒数，3-10。")
    images: Optional[List[OmniImageInput]] = Field(
        None, description="图生/参考生视频的输入图（image_to_video/reference_to_video 使用）。"
    )
    store: bool = Field(
        True, description="是否存储交互以便后续编辑链；关掉则不可用 previous_interaction_id 续编辑。"
    )

    class Config:
        use_enum_values = True


class EditOmniVideoPayload(BaseModel):
    """基于上一轮交互的会话式编辑请求（链式改视频，无需重传视频）。"""
    workflow_id: str = Field(..., description="用于追踪的唯一工作流 ID。")
    previous_interaction_id: str = Field(
        ..., description="上一轮交互 ID；必须已 completed，且上一轮 store=true。"
    )
    prompt: str = Field(
        ..., description="编辑指令。建议简短并追加『Keep everything else the same.』", min_length=1
    )
    model_id: OmniModelID = Field(
        OmniModelID.OMNI_1_1_FLASH_PREVIEW.value,
        description="Omni 模型 ID，默认 gemini-omni-1.1-flash-preview。",
    )
    store: bool = Field(True, description="是否存储本轮结果以便继续链式编辑。")

    class Config:
        use_enum_values = True


class OmniVideoResult(BaseModel):
    public_url: str
    gcs_uri: Optional[str] = None
    mime_type: str = "video/mp4"


class OmniVideoResponse(BaseModel):
    workflow_id: str
    interaction_id: str
    videos: List[OmniVideoResult] = []


# ─── 提交与轮询上下文 ─────────────────────────────────────────────────
class _OmniSubmission:
    """一次 Omni 提交成功后锁定的上下文：创建返回 interaction_id 后，轮询/编辑须锁定同项目。

    Interactions API 有状态：创建（background=true）立即返回 ``{id, status:"in_progress"}``，
    随后需对同一 project/global 反复 ``GET .../interactions/{id}`` 轮询到 completed。
    interaction 属于提交时的项目，故提交成功后锁定该项目，后续轮询用同项目 token 与端点。
    """

    def __init__(
        self,
        interaction_id: str,
        create_response: Dict[str, Any],
        project: Optional[GcpProjectRuntime],
    ):
        self.interaction_id = interaction_id
        self.create_response = create_response  # 创建响应（同步时可能已含 video uri）
        self.project = project  # None = 单项目回退模式
        project_id = project.project_id if project else GOOGLE_PROJECT_ID
        base = OMNI_INTERACTIONS_ENDPOINT_TEMPLATE.format(project_id=project_id)
        # 轮询端点：实测企业平台使用 GET .../interactions/{id} 取回终态；
        # POST 会返回 404（Google 文档中存在 POST/GET 描述不一致）。
        self.poll_endpoint = f"{base}/{interaction_id}"

    async def auth_headers(self) -> Dict[str, str]:
        """轮询鉴权头：锁定项目重取 token（单项目模式走全局池兜底）。"""
        if self.project is not None:
            token = await self.project.get_access_token()
        else:
            # 单项目回退：直接用全局凭证加载器（与 cre_video 单项目分支一致）。
            from utils.gcp_credentials import get_access_token

            token = await get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }


async def _post_create(
    endpoint: str, headers: Dict[str, str], request_body: Dict[str, Any]
) -> Dict[str, Any]:
    """提交创建请求并返回 JSON；抛 HTTPStatusError 供上层做失败转移判定。"""
    resp = await http_client.post(endpoint, headers=headers, json=request_body)
    resp.raise_for_status()
    return resp.json()


async def submit_omni_task(
    workflow_id: str,
    request_body: Dict[str, Any],
) -> _OmniSubmission:
    """选项目 + 提交 Omni 创建请求，跨项目做失败转移；提交成功后锁定该项目。

    - 池启用：按"最空闲优先"选具备 omni 能力的健康项目提交；429/5xx/网络异常 → 熔断该项目
      并换下一个（最多 router.max_attempts(CAPABILITY_OMNI) 次）；4xx(非429) → 直接抛。
    - 池为空：回退单项目（GOOGLE_PROJECT_ID + 全局兜底凭证）。

    :raises httpx.HTTPStatusError / RuntimeError: 全部尝试失败时抛出，由调用方转 502。
    """
    router_ = project_router

    if router_ is None or not router_.enabled:
        from utils.gcp_credentials import get_access_token

        headers = {
            "Authorization": f"Bearer {await get_access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }
        endpoint = OMNI_INTERACTIONS_ENDPOINT_TEMPLATE.format(project_id=GOOGLE_PROJECT_ID)
        data = await _post_create(endpoint, headers, request_body)
        interaction_id = data.get("id")
        if not interaction_id:
            raise RuntimeError("Interactions API 未返回有效的 interaction id")
        return _OmniSubmission(interaction_id, data, None)

    if not router_.has_capability(CAPABILITY_OMNI):
        raise RuntimeError(
            "生成池中没有任何具备 omni(is_omni) 能力的项目，请检查 gcp-endpoints.yaml 能力标注"
        )

    max_attempts = router_.max_attempts(CAPABILITY_OMNI)
    excluded: set = set()
    last_exc: Optional[BaseException] = None

    for attempt in range(max_attempts):
        candidates = router_.healthy_projects(excluded, capability=CAPABILITY_OMNI)
        if not candidates:
            logger.warning(
                f"[{workflow_id}] 无健康 omni 项目可用（已排除 {excluded}），"
                f"尝试 {attempt}/{max_attempts}"
            )
            break
        project = candidates[0]
        excluded.add(project.name)
        try:
            token = await project.get_access_token()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            router_.mark_failure(project, f"token 刷新失败: {e}")
            logger.warning(f"[{workflow_id}] project={project.name} token 刷新失败: {e}")
            continue

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        endpoint = OMNI_INTERACTIONS_ENDPOINT_TEMPLATE.format(project_id=project.project_id)
        try:
            data = await _post_create(endpoint, headers, request_body)
            interaction_id = data.get("id")
            if not interaction_id:
                raise RuntimeError("Interactions API 未返回有效的 interaction id")
            router_.mark_success(project)
            logger.info(
                f"[{workflow_id}] omni 提交命中项目 {project.name} "
                f"(attempt {attempt + 1}/{max_attempts}) id={interaction_id}"
            )
            return _OmniSubmission(interaction_id, data, project)
        except httpx.HTTPStatusError as e:
            last_exc = e
            code = e.response.status_code
            if _is_retryable_status(code):
                router_.mark_failure(project, f"HTTP {code}: {e.response.text[:300]}")
                logger.warning(
                    f"[{workflow_id}] project={project.name} 提交失败 {code}，换项目重试"
                )
                continue
            raise  # 4xx（非 429）参数/权限错误，换项目无益
        except Exception as e:  # noqa: BLE001（网络/超时等，换项目）
            last_exc = e
            router_.mark_failure(project, f"异常: {e}")
            logger.warning(f"[{workflow_id}] project={project.name} 提交异常: {e}，换项目重试")
            continue

    if last_exc:
        raise last_exc
    raise RuntimeError("omni 提交失败：所有 omni 项目均不可用")


# ─── 请求体构造 ──────────────────────────────────────────────────────
def _build_input_parts(prompt: str, images: Optional[List[OmniImageInput]]) -> List[Dict[str, Any]]:
    """构造 Interactions API 的 input parts：文本 + 可选参考图（uri 优先，否则 base64）。"""
    parts: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img in images or []:
        if img.uri:
            parts.append({"type": "image", "uri": img.uri, "mime_type": img.mime_type})
        elif img.data:
            parts.append({"type": "image", "data": img.data, "mime_type": img.mime_type})
    return parts


def _build_generate_body(payload: GenerateOmniVideoPayload) -> Dict[str, Any]:
    """文生/图生/参考生视频请求体。

    background=true：Omni 生成常 >1min，同步请求易撞 HTTP 60s 超时，故统一走异步 + 轮询。
    delivery="uri" + gcs_uri：让视频直接落 GCS 桶（大视频免 base64 膨胀），公开 URL 直取。
    """
    return {
        "model": payload.model_id,
        "background": True,
        "store": payload.store,
        "input": _build_input_parts(payload.prompt, payload.images),
        "response_format": [
            {
                "type": "video",
                "delivery": "uri",
                "gcs_uri": GCS_OMNI_OUTPUT_URI,
                "aspect_ratio": payload.aspect_ratio,
                "resolution": payload.resolution,
                "duration": f"{int(payload.duration_sec)}s",
            }
        ],
        "generation_config": {"video_config": {"task": payload.task}},
    }


def _build_edit_body(payload: EditOmniVideoPayload) -> Dict[str, Any]:
    """会话式编辑请求体：previous_interaction_id 链式改视频，无需重传视频。

    task 固定 edit；不重复传 response_format 的 gcs_uri 也可（继承上一轮），但显式带上更稳。
    previous_interaction_id 只携带会话历史，system_instruction/generation_config 不继承，故每轮重传。
    """
    return {
        "model": payload.model_id,
        "background": True,
        "store": payload.store,
        "previous_interaction_id": payload.previous_interaction_id,
        "input": [{"type": "text", "text": payload.prompt}],
        "response_format": [
            {
                "type": "video",
                "delivery": "uri",
                "gcs_uri": GCS_OMNI_OUTPUT_URI,
            }
        ],
        "generation_config": {"video_config": {"task": OmniTask.EDIT.value}},
    }


# ─── base64 兜底上传 GCS ─────────────────────────────────────────────
async def _upload_base64_video_to_gcs(b64_data: str, mime_type: str) -> str:
    """把轮询返回的 inline base64 视频回写 GCS 桶，返回 gs:// URI。

    根因：官方明确轮询响应即使当初 delivery:"uri" 也会返回 inline base64，uri 只保证在
    创建响应/SSE 中。background 轮询到 completed 时若只拿到 data，则在此兜底落桶，保证对外
    始终返回可访问的 GCS 公开 URL（与 uri delivery 落同一目录）。存储用 GCS 专用凭证。
    """
    ext = "mp4"
    filename = f"{_OMNI_OBJECT_PREFIX}/{uuid.uuid4()}.{ext}"
    upload_url = GCS_UPLOAD_ENDPOINT + filename
    video_bytes = base64.b64decode(b64_data)
    token = await get_gcs_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": mime_type or "video/mp4",
    }
    resp = await http_client.post(upload_url, content=video_bytes, headers=headers)
    resp.raise_for_status()
    gcs_uri = f"gs://{GCS_BUCKET_NAME}/{filename}"
    logger.info(f"omni base64 视频已兜底上传 GCS: {gcs_uri}")
    return gcs_uri


# ─── 结果提取 ────────────────────────────────────────────────────────
def _extract_video_contents(interaction: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 completed 交互的 steps 里收集所有 model_output 的 video content。"""
    videos: List[Dict[str, Any]] = []
    for step in interaction.get("steps", []) or []:
        if step.get("type") != "model_output":
            continue
        for content in step.get("content", []) or []:
            if content.get("type") == "video":
                videos.append(content)
    return videos


async def _resolve_video_results(
    video_contents: List[Dict[str, Any]],
) -> List[OmniVideoResult]:
    """把 video content（uri 或 base64 data）统一解析为对外的 OmniVideoResult。

    uri 优先（多为 gs:// 直取公开 URL）；仅有 base64 data 时兜底上传 GCS 再给公开 URL。
    """
    results: List[OmniVideoResult] = []
    for vc in video_contents:
        mime_type = vc.get("mime_type", "video/mp4")
        uri = vc.get("uri")
        data = vc.get("data")
        if uri:
            results.append(
                OmniVideoResult(
                    public_url=convert_gcs_to_public_url(uri),
                    gcs_uri=uri if uri.startswith("gs://") else None,
                    mime_type=mime_type,
                )
            )
        elif data:
            gcs_uri = await _upload_base64_video_to_gcs(data, mime_type)
            results.append(
                OmniVideoResult(
                    public_url=convert_gcs_to_public_url(gcs_uri),
                    gcs_uri=gcs_uri,
                    mime_type=mime_type,
                )
            )
    return results


async def _poll_until_terminal(
    workflow_id: str, submission: _OmniSubmission
) -> Optional[Dict[str, Any]]:
    """轮询交互到终态；返回终态交互 JSON，超时返回 None。

    轮询用 GET .../interactions/{id}。若创建响应已是 completed（同步兜底），直接返回。
    """
    create = submission.create_response
    if create.get("status") == "completed":
        return create

    start_time = asyncio.get_event_loop().time()
    terminal = {"completed", "failed", "cancelled", "incomplete"}
    while True:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > POLLING_TIMEOUT_SECONDS:
            logger.error(f"[{workflow_id}] omni 轮询超时 (id={submission.interaction_id})")
            return None

        headers = await submission.auth_headers()
        resp = await http_client.get(submission.poll_endpoint, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        status_str = data.get("status")
        logger.info(
            f"[{workflow_id}] omni 轮询 status={status_str} "
            f"(id={submission.interaction_id}, 已用时 {int(elapsed)}s)"
        )
        if status_str in terminal:
            return data
        await asyncio.sleep(POLLING_INTERVAL_SECONDS)


async def _run_and_respond(
    workflow_id: str, request_body: Dict[str, Any]
) -> JSONResponse:
    """提交 + 轮询 + 提取 + 组装标准响应的公共流程（generate/edit 共用）。"""
    try:
        submission = await submit_omni_task(workflow_id, request_body)
        logger.info(
            f"[{workflow_id}] omni 任务已提交, interaction_id={submission.interaction_id}"
        )

        interaction = await _poll_until_terminal(workflow_id, submission)
        if interaction is None:
            return create_standard_response(
                code=status.HTTP_504_GATEWAY_TIMEOUT,
                message="Omni video generation task timed out.",
            )

        final_status = interaction.get("status")
        if final_status != "completed":
            err = interaction.get("error") or {}
            msg = err.get("message") or f"交互终态为 {final_status}"
            logger.error(f"[{workflow_id}] omni 任务未成功: {msg}")
            return create_standard_response(
                code=status.HTTP_502_BAD_GATEWAY, message=f"Omni 任务失败: {msg}"
            )

        video_contents = _extract_video_contents(interaction)
        if not video_contents:
            # completed 但无 video content：通常被 RAI 安全策略过滤，或区域限制导致空输出。
            logger.error(
                f"[{workflow_id}] omni 已完成但无视频输出（可能被安全策略过滤或区域限制）。"
            )
            return create_standard_response(
                code=status.HTTP_502_BAD_GATEWAY,
                message="任务已完成但未返回视频（可能被安全策略过滤或触发区域限制）。",
            )

        video_results = await _resolve_video_results(video_contents)
        success = OmniVideoResponse(
            workflow_id=workflow_id,
            interaction_id=submission.interaction_id,
            videos=video_results,
        )
        return create_standard_response(data=success.model_dump(), message="Omni 视频生成成功")

    except httpx.HTTPStatusError as e:
        detail = f"Google Interactions API 请求失败: {e.response.status_code} - {e.response.text}"
        logger.error(f"[{workflow_id}] {detail}")
        return create_standard_response(code=status.HTTP_502_BAD_GATEWAY, message=detail)
    except Exception as e:  # noqa: BLE001
        detail = f"Omni 视频生成过程中发生内部错误: {str(e)}"
        logger.exception(f"[{workflow_id}] {detail}")
        return create_standard_response(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR, message=detail
        )


# ─── API 端点 ────────────────────────────────────────────────────────
@router.post("/generate_omni_video", summary="用 Gemini Omni Flash 生成视频（文生/图生/参考生）")
async def generate_omni_video(payload: GenerateOmniVideoPayload):
    """提交 Omni 视频生成任务，异步轮询到完成后返回 GCS 公开 URL 与 interaction_id。

    interaction_id 可用于随后调用 /edit_omni_video 做会话式链式编辑（要求本次 store=true）。
    """
    logger.info(
        f"收到 omni 视频生成请求, Workflow ID: {payload.workflow_id}, "
        f"task={payload.task}, model={payload.model_id}, prompt='{payload.prompt[:50]}...'"
    )
    request_body = _build_generate_body(payload)
    return await _run_and_respond(payload.workflow_id, request_body)


@router.post("/edit_omni_video", summary="基于上一轮交互对 Omni 视频做会话式链式编辑")
async def edit_omni_video(payload: EditOmniVideoPayload):
    """会话式编辑：传 previous_interaction_id + 编辑指令，无需重传视频。

    约束（官方）：previous_interaction_id 指向的交互必须已 completed（in_progress 会 400），
    且上一轮 store 不能为 false。返回新的 interaction_id，可继续链式编辑。
    编辑模型自己生成的视频不受区域限制；编辑用户上传视频在部分地区会空输出。
    """
    logger.info(
        f"收到 omni 视频编辑请求, Workflow ID: {payload.workflow_id}, "
        f"previous_interaction_id={payload.previous_interaction_id}, "
        f"prompt='{payload.prompt[:50]}...'"
    )
    request_body = _build_edit_body(payload)
    return await _run_and_respond(payload.workflow_id, request_body)
