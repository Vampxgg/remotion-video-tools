# -*- coding: utf-8 -*-
"""多模态知识理解 API 路由。

在 /file/parse 之上叠加 Vertex Gemini 视觉理解，输出"看懂图表/图片、带源图源表"的
增强 Markdown。对外响应与 /file/parse 完全一致（FileParseResult），调用方仅需切换 URL。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import List, Optional, Set

from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from schemas.file_parse import FileParseBatchData, FileParseBatchSummary
from services import file_understand_jobs as jobs
from services.file_parse_service import FilePayload, ParseInputError
from services.file_understand_service import (
    UnderstandOptions,
    understand_file_payload,
)
from utils import redis_client
from utils.responses import create_standard_response
from utils.security import require_api_key
from utils.settings import settings as _settings

router = APIRouter(dependencies=[Depends(require_api_key("FILE_UNDERSTAND_API_KEY"))])
logger = logging.getLogger(__name__)

# 持有后台任务引用，避免 asyncio 在任务结束前将其回收。
_BG_TASKS: Set[asyncio.Task] = set()

# 提交(intake)削峰信号量：惰性初始化（Semaphore 需绑定运行中的事件循环，模块导入期尚无 loop）。
# 每个 worker 进程各持一个；包裹"读文件字节 + 派发后台任务"，平滑瞬时高并发提交。
_INTAKE_SEM: Optional[asyncio.Semaphore] = None


def _intake_semaphore() -> Optional[asyncio.Semaphore]:
    """返回进程内 intake 信号量；配置 <=0 表示不限流（返回 None）。"""
    global _INTAKE_SEM
    limit = int(getattr(_settings, "FILE_UNDERSTAND_INTAKE_CONCURRENCY", 0) or 0)
    if limit <= 0:
        return None
    if _INTAKE_SEM is None:
        _INTAKE_SEM = asyncio.Semaphore(limit)
    return _INTAKE_SEM


@asynccontextmanager
async def lifespan_resources(app):
    """file_understand 自有资源：确保全局限流器依赖的 Redis 已初始化。

    根因：限流器（vertex_global_limiter）依赖 redis_client 就绪。历史上 Redis 只在
    web_search 的 lifespan 里 startup，导致单独挂载本 router / 拆分部署时限流器永远不可用。
    这里让本 router 自己保证 Redis 就绪（若已就绪则跳过），生命周期不再依附 web_search。
    """
    if not redis_client.is_ready():
        await redis_client.startup()
    logger.info("file_understand router 就绪 (redis_ready=%s)", redis_client.is_ready())
    try:
        yield
    finally:
        # 不在此处 shutdown Redis：连接为全局共享单例，由应用退出时统一释放，
        # 避免与其它同样使用 Redis 的 router 抢关闭。
        # 关闭 Azure VLM 长生命周期连接池（本 router 独有资源，可安全释放）。
        try:
            from services.azure_vlm_client import aclose_clients
            await aclose_clients()
        except Exception:  # noqa: BLE001
            logger.warning("关闭 Azure VLM 连接池异常（忽略）", exc_info=True)


async def _payload_from_upload(
    upload: UploadFile,
    *,
    total_so_far: int = 0,
) -> tuple[FilePayload, int]:
    chunks = []
    size = 0
    single_limit = _settings.FILE_PARSE_MAX_UPLOAD_MB * 1024 * 1024
    total_limit = _settings.FILE_PARSE_MAX_TOTAL_MB * 1024 * 1024
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        total_so_far += len(chunk)
        if size > single_limit:
            raise ParseInputError(
                413,
                "file_too_large",
                f"文件过大，最大支持 {_settings.FILE_PARSE_MAX_UPLOAD_MB}MB。",
            )
        if total_so_far > total_limit:
            raise ParseInputError(
                413,
                "batch_too_large",
                f"批量文件总大小超过 {_settings.FILE_PARSE_MAX_TOTAL_MB}MB。",
            )
        chunks.append(chunk)
    return FilePayload(
        filename=upload.filename or "unknown",
        content=b"".join(chunks),
        media_type=upload.content_type,
    ), total_so_far


def _options(
    max_chars: Optional[int],
    enable_ocr: Optional[bool],
    enable_embedded_image_upload: Optional[bool],
    model: Optional[str],
    enable_vision: bool,
) -> UnderstandOptions:
    return UnderstandOptions(
        max_chars=max_chars,
        enable_ocr=enable_ocr,
        enable_embedded_image_upload=enable_embedded_image_upload,
        model=model,
        enable_vision=enable_vision,
    )


@router.post(
    "/file/understand",
    summary="多模态理解单个上传文件，输出带源图源表的增强 Markdown",
)
async def understand_file(
    file: UploadFile = File(..., description="唯一文件字段，字段名必须为 file"),
    enable_ocr: Optional[bool] = Form(None, description="是否启用图片 OCR（基础解析层）"),
    enable_embedded_image_upload: Optional[bool] = Form(
        None, description="是否上传文档内嵌图片为公网 URL（默认开启）"
    ),
    max_chars: Optional[int] = Form(None, description="返回内容最大字符数，上限由服务端配置控制"),
    model: Optional[str] = Form(None, description="覆盖默认 Vertex Gemini 模型"),
    enable_vision: bool = Form(True, description="是否启用视觉理解；关闭则等同 /file/parse"),
):
    try:
        payload, _ = await _payload_from_upload(file)
        result = await understand_file_payload(
            payload,
            _options(max_chars, enable_ocr, enable_embedded_image_upload, model, enable_vision),
        )
    except ParseInputError as exc:
        return create_standard_response(
            data={"error": {"code": exc.code, "detail": exc.detail}},
            code=exc.status_code,
            message=exc.detail,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("file understand failed: %s", exc)
        detail = "文件理解失败，请稍后重试或联系服务维护人员。"
        return create_standard_response(
            data={"error": {"code": "internal_error", "detail": detail}},
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=detail,
        )
    finally:
        await file.close()

    vision_status = (result.meta or {}).get("vision_status", "skipped")
    if vision_status == "enhanced":
        msg = "文件多模态理解完成"
    elif vision_status == "degraded":
        msg = "多模态理解未成功，已返回基础解析结果（见 meta.fallback_reason）"
    else:
        msg = "已返回基础解析结果（未启用/不适用视觉理解）"
    return create_standard_response(
        data=result.model_dump(),
        message=msg,
    )


async def _process_understand_job(
    job_id: str,
    payload: FilePayload,
    options: UnderstandOptions,
    budget_sec: Optional[float] = None,
) -> None:
    """后台执行单文件多模态理解，并把结果写入任务存储。

    基础解析（全文 + 全部真实图 URL）完成即通过 set_partial 写入中间结果，作为视觉理解
    未在预算内完成时的兜底；视觉阶段受 budget_sec 墙钟约束，到点返回已完成部分。
    """
    jobs.mark_running(job_id)

    def _on_base_ready(base_result) -> None:
        try:
            jobs.set_partial(job_id, base_result.model_dump())
        except Exception:  # noqa: BLE001
            logger.warning("write partial failed [%s]", job_id)

    try:
        result = await understand_file_payload(
            payload,
            options,
            budget_sec=budget_sec,
            on_base_ready=_on_base_ready,
        )
        stage = (result.meta or {}).get("vision_status") or "enhanced"
        # vision_status: enhanced=有增强；degraded=仅基础解析兜底；skipped=无需视觉。
        job_stage = "enhanced" if stage == "enhanced" else "base"
        jobs.mark_succeeded(job_id, result.model_dump(), stage=job_stage)
    except ParseInputError as exc:
        jobs.mark_failed(job_id, exc.code, exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("async file understand failed [%s]: %s", job_id, exc)
        jobs.mark_failed(job_id, "internal_error", str(exc))


@router.post(
    "/file/understand/async",
    summary="异步多模态理解：提交任务，立即返回 job_id（配合 /file/understand/result 轮询）",
)
async def understand_file_async(
    file: UploadFile = File(..., description="唯一文件字段，字段名必须为 file"),
    enable_ocr: Optional[bool] = Form(None, description="是否启用图片 OCR（基础解析层）"),
    enable_embedded_image_upload: Optional[bool] = Form(
        None, description="是否上传文档内嵌图片为公网 URL（默认开启）"
    ),
    max_chars: Optional[int] = Form(None, description="返回内容最大字符数，上限由服务端配置控制"),
    model: Optional[str] = Form(None, description="覆盖默认 Vertex Gemini 模型"),
    enable_vision: bool = Form(True, description="是否启用视觉理解；关闭则等同 /file/parse"),
    max_wait: Optional[float] = Form(
        None,
        description=(
            "本次理解的墙钟总预算(秒)，含排队+基础解析+视觉。到点即返回已完成部分(至少基础解析)。"
            "留空取服务端默认 FILE_UNDERSTAND_ASYNC_BUDGET_SEC。调用方(如 Dify 代码节点)"
            "应设为略小于其沙箱执行上限，确保能在被 kill 前轮询到 succeeded。"
        ),
    ),
):
    """接收文件后立即返回 ``job_id``，真正的多模态理解放后台执行。

    调用方随后用 ``GET /file/understand/result/{job_id}`` 轮询，直到 status 为
    ``succeeded``/``failed``。每个请求都很短，可绕开前置网关对单条长连接的读超时。
    """
    try:
        sem = _intake_semaphore()
        if sem is not None:
            async with sem:
                payload, _ = await _payload_from_upload(file)
        else:
            payload, _ = await _payload_from_upload(file)
    except ParseInputError as exc:
        return create_standard_response(
            data={"error": {"code": exc.code, "detail": exc.detail}},
            code=exc.status_code,
            message=exc.detail,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("async understand intake failed: %s", exc)
        detail = "文件接收失败，请稍后重试或联系服务维护人员。"
        return create_standard_response(
            data={"error": {"code": "internal_error", "detail": detail}},
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=detail,
        )
    finally:
        await file.close()

    options = _options(max_chars, enable_ocr, enable_embedded_image_upload, model, enable_vision)
    budget_sec = (
        float(max_wait) if (max_wait and max_wait > 0)
        else float(_settings.FILE_UNDERSTAND_ASYNC_BUDGET_SEC)
    )
    job_id = jobs.create_job(payload.filename)
    task = asyncio.create_task(_process_understand_job(job_id, payload, options, budget_sec))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)

    return create_standard_response(
        data={"job_id": job_id, "status": jobs.STATUS_PENDING},
        message="任务已受理，请轮询 /file/understand/result/{job_id} 获取结果",
    )


@router.get(
    "/file/understand/result/{job_id}",
    summary="查询异步多模态理解任务结果",
)
async def understand_file_result(job_id: str):
    """返回任务状态；succeeded 时 data.result 为与 /file/understand 一致的解析结果。"""
    job = jobs.read_job(job_id)
    if job is None:
        return create_standard_response(
            data={"job_id": job_id, "status": "not_found"},
            code=status.HTTP_404_NOT_FOUND,
            message="任务不存在或已过期。",
        )
    return create_standard_response(data=job, message="ok")


@router.post(
    "/file/understand/batch",
    summary="批量多模态理解上传文件",
)
async def understand_files_batch(
    files: List[UploadFile] = File(..., description="文件列表字段，字段名必须为 files"),
    enable_ocr: Optional[bool] = Form(None, description="是否启用图片 OCR（基础解析层）"),
    enable_embedded_image_upload: Optional[bool] = Form(
        None, description="是否上传文档内嵌图片为公网 URL（默认开启）"
    ),
    max_chars: Optional[int] = Form(None, description="每个文件返回内容最大字符数"),
    model: Optional[str] = Form(None, description="覆盖默认 Vertex Gemini 模型"),
    enable_vision: bool = Form(True, description="是否启用视觉理解；关闭则等同 /file/parse"),
):
    try:
        if len(files) > _settings.FILE_PARSE_MAX_BATCH_FILES:
            raise ParseInputError(
                413,
                "too_many_files",
                f"批量最多支持 {_settings.FILE_PARSE_MAX_BATCH_FILES} 个文件。",
            )
        items = []
        total_size = 0
        opts = _options(max_chars, enable_ocr, enable_embedded_image_upload, model, enable_vision)
        for upload in files:
            payload, total_size = await _payload_from_upload(upload, total_so_far=total_size)
            items.append(await understand_file_payload(payload, opts))
        ok_count = sum(1 for item in items if item.status == "ok")
        data = FileParseBatchData(
            summary=FileParseBatchSummary(
                requested=len(items),
                ok=ok_count,
                failed=len(items) - ok_count,
                partial_success=0 < ok_count < len(items),
            ),
            items=items,
        )
    except ParseInputError as exc:
        return create_standard_response(
            data={"error": {"code": exc.code, "detail": exc.detail}},
            code=exc.status_code,
            message=exc.detail,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("batch file understand failed: %s", exc)
        detail = "批量理解失败，请稍后重试或联系服务维护人员。"
        return create_standard_response(
            data={"error": {"code": "internal_error", "detail": detail}},
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=detail,
        )
    finally:
        for upload in files:
            await upload.close()

    return create_standard_response(
        data=data.model_dump(),
        message=f"批量多模态理解完成，成功 {data.summary.ok}/{data.summary.requested}",
    )
