# -*- coding: utf-8 -*-
# @File：video_compress.py
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com

import asyncio
import os
import sys
import uuid
import json
import logging
import shutil
import subprocess
from enum import Enum
from pathlib import Path
from typing import Optional, Any, Dict, List

import httpx
from fastapi import APIRouter, UploadFile, File, Form, BackgroundTasks, Request, Query
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from datetime import datetime

# utils.logger 是仓库内必需模块，删除冗余 fallback；导入失败应直接报错暴露问题
from utils.logger import setup_module_logger

logger = setup_module_logger(__name__, "logs/video/compress.log")

router = APIRouter()

from utils.settings import settings as _settings  # noqa: E402  (settings 单点入口)

# ──────────────────────────── 目录配置 ────────────────────────────
# STATIC_DIR_NAME 与 main.py 的 settings.STATIC_DIR 保持同源，避免历史上 ./static 与 api/static 不一致
STATIC_DIR_NAME = _settings.STATIC_DIR
COMPRESS_UPLOAD_SUBDIR = _settings.VIDEO_COMPRESS_UPLOAD_SUBDIR
COMPRESS_OUTPUT_SUBDIR = _settings.VIDEO_COMPRESS_OUTPUT_SUBDIR

UPLOAD_DIR = Path(STATIC_DIR_NAME) / COMPRESS_UPLOAD_SUBDIR
OUTPUT_DIR = Path(STATIC_DIR_NAME) / COMPRESS_OUTPUT_SUBDIR
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".ts", ".mts"}
MAX_UPLOAD_SIZE_MB = _settings.VIDEO_COMPRESS_MAX_UPLOAD_MB

# ──────────────────────────── 并发控制 ────────────────────────────
MAX_CONCURRENT_FFMPEG = _settings.VIDEO_COMPRESS_MAX_CONCURRENT_FFMPEG
ffmpeg_semaphore: Optional[asyncio.Semaphore] = None

# ──────────────────────────── 任务存储 ────────────────────────────
# 进程内任务状态字典；如果多 worker 需共享状态则应换成 Redis
tasks: Dict[str, dict] = {}


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class CompressionPreset(str, Enum):
    ULTRAFAST = "ultrafast"
    SUPERFAST = "superfast"
    VERYFAST = "veryfast"
    FASTER = "faster"
    FAST = "fast"
    MEDIUM = "medium"
    SLOW = "slow"


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


# ──────────────────────────── 工具函数 ────────────────────────────

async def cleanup_files(paths: list[Path], delay: int = 600):
    await asyncio.sleep(delay)
    for path in paths:
        try:
            if path.is_file():
                path.unlink()
                logger.info(f"已清理文件: {path}")
            elif path.is_dir():
                shutil.rmtree(path)
                logger.info(f"已清理目录: {path}")
        except Exception as e:
            logger.warning(f"清理 {path} 失败: {e}")


def get_video_info(file_path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        file_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {}
        return json.loads(result.stdout)
    except Exception:
        return {}


def compress_video(
        input_path: str,
        output_path: str,
        crf: int = 28,
        preset: str = "veryfast",
        resolution: Optional[str] = None,
        max_bitrate: Optional[str] = None,
        audio_bitrate: str = "128k",
) -> dict:
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", preset,
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-movflags", "+faststart",
    ]

    if resolution and "x" in resolution:
        parts = resolution.split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            cmd.extend(["-vf", f"scale={parts[0]}:{parts[1]}"])

    if max_bitrate:
        cmd.extend(["-maxrate:v", max_bitrate, "-bufsize:v", max_bitrate])

    cmd.append(output_path)

    logger.info(f"执行 FFmpeg 命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg 压缩失败: {result.stderr[-500:]}")

    original_size = os.path.getsize(input_path)
    compressed_size = os.path.getsize(output_path)
    ratio = (1 - compressed_size / original_size) * 100 if original_size > 0 else 0

    return {
        "original_size_bytes": original_size,
        "compressed_size_bytes": compressed_size,
        "original_size_mb": round(original_size / (1024 * 1024), 2),
        "compressed_size_mb": round(compressed_size / (1024 * 1024), 2),
        "compression_ratio": round(ratio, 2),
    }


# ──────────────────────────── 后台压缩协程 ────────────────────────────

async def _run_compress_task(
        task_id: str,
        input_path: Path,
        output_path: Path,
        base_url: str,
        crf: int,
        preset: str,
        resolution: Optional[str],
        max_bitrate: Optional[str],
        audio_bitrate: str,
):
    global ffmpeg_semaphore
    if ffmpeg_semaphore is None:
        ffmpeg_semaphore = asyncio.Semaphore(MAX_CONCURRENT_FFMPEG)

    tasks[task_id]["status"] = TaskStatus.PENDING
    logger.info(f"[{task_id}] 排队等待信号量 (当前限制 {MAX_CONCURRENT_FFMPEG} 并发)")

    try:
        async with ffmpeg_semaphore:
            tasks[task_id]["status"] = TaskStatus.PROCESSING
            logger.info(f"[{task_id}] 获得信号量，开始压缩")

            compress_result = await asyncio.to_thread(
                compress_video,
                str(input_path),
                str(output_path),
                crf=crf,
                preset=preset,
                resolution=resolution,
                max_bitrate=max_bitrate,
                audio_bitrate=audio_bitrate,
            )

            download_url = (
                f"{base_url.rstrip('/')}/{STATIC_DIR_NAME}/"
                f"{COMPRESS_OUTPUT_SUBDIR}/{output_path.name}"
            )

            tasks[task_id].update({
                "status": TaskStatus.COMPLETED,
                "download_url": download_url,
                "completed_at": datetime.now().isoformat(),
                **compress_result,
            })

            logger.info(
                f"[{task_id}] 压缩完成: "
                f"{compress_result['original_size_mb']}MB -> {compress_result['compressed_size_mb']}MB "
                f"(压缩率 {compress_result['compression_ratio']}%)"
            )

    except Exception as e:
        tasks[task_id].update({
            "status": TaskStatus.FAILED,
            "error": str(e),
            "completed_at": datetime.now().isoformat(),
        })
        output_path.unlink(missing_ok=True)
        logger.error(f"[{task_id}] 压缩失败: {e}")

    finally:
        # 无论成功失败，延迟清理上传的原始文件；成功时也延迟清理输出文件
        paths_to_clean = [input_path]
        if tasks[task_id]["status"] == TaskStatus.COMPLETED:
            paths_to_clean.append(output_path)
        asyncio.create_task(cleanup_files(paths_to_clean, delay=600))


# ──────────────────────────── API 端点 ────────────────────────────

@router.post(
    "/compress_video",
    summary="提交视频压缩任务",
    description="上传视频文件后立即返回 task_id，压缩在后台异步执行。通过 GET /compress_video/{task_id} 查询进度。"
)
async def submit_compress_task(
        request: Request,
        video: UploadFile = File(..., description="要压缩的视频文件"),
        crf: int = Form(28, description="恒定质量因子 (0-51)，值越大压缩率越高、质量越低，推荐 23-28", ge=0, le=51),
        preset: CompressionPreset = Form(CompressionPreset.VERYFAST, description="编码速度预设，越慢质量越好"),
        resolution: Optional[str] = Form(None, description="目标分辨率，如 1280x720，留空保持原始分辨率"),
        max_bitrate: Optional[str] = Form(None, description="最大码率限制，如 2M、5M"),
        audio_bitrate: str = Form("128k", description="音频码率，如 64k、128k、192k"),
):
    resolution = resolution.strip() if resolution else None
    max_bitrate = max_bitrate.strip() if max_bitrate else None

    ext = Path(video.filename).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return create_standard_response(
            code=400,
            message=f"不支持的视频格式 '{ext}'，支持的格式: {', '.join(ALLOWED_VIDEO_EXTENSIONS)}"
        )

    task_id = str(uuid.uuid4())
    input_filename = f"{task_id}_input{ext}"
    output_filename = f"{task_id}_compressed.mp4"
    input_path = UPLOAD_DIR / input_filename
    output_path = OUTPUT_DIR / output_filename

    try:
        file_size = 0
        with open(input_path, "wb") as f:
            while chunk := await video.read(1024 * 1024):
                file_size += len(chunk)
                if file_size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                    input_path.unlink(missing_ok=True)
                    return create_standard_response(
                        code=413,
                        message=f"文件过大，最大支持 {MAX_UPLOAD_SIZE_MB}MB"
                    )
                f.write(chunk)

        logger.info(f"[{task_id}] 文件已保存: {input_path} ({round(file_size / 1024 / 1024, 2)}MB)")

        tasks[task_id] = {
            "task_id": task_id,
            "status": TaskStatus.PENDING,
            "original_filename": video.filename,
            "original_size_mb": round(file_size / 1024 / 1024, 2),
            "submitted_at": datetime.now().isoformat(),
            "download_url": None,
            "error": None,
        }

        asyncio.create_task(
            _run_compress_task(
                task_id=task_id,
                input_path=input_path,
                output_path=output_path,
                base_url=str(request.base_url),
                crf=crf,
                preset=preset.value,
                resolution=resolution,
                max_bitrate=max_bitrate,
                audio_bitrate=audio_bitrate,
            )
        )

        return create_standard_response(
            data={"task_id": task_id, "status": TaskStatus.PENDING},
            message="任务已提交，请通过 GET /api/compress_video/{task_id} 查询进度"
        )

    except Exception as e:
        input_path.unlink(missing_ok=True)
        logger.exception(f"[{task_id}] 提交失败: {e}")
        return create_standard_response(code=500, message=f"提交任务失败: {str(e)}")


@router.get(
    "/compress_video/{task_id}",
    summary="查询视频压缩任务状态",
    description="通过 task_id 查询压缩任务的当前状态和结果。"
)
async def query_compress_task(task_id: str):
    task = tasks.get(task_id)
    if not task:
        return create_standard_response(code=404, message=f"任务 {task_id} 不存在")

    return create_standard_response(data=task)


# ══════════════════════════════════════════════════════════════════
#  Dify 专用端点
#  输入：JSON {"url": "...", "name": "..."} 或 form-data 文件直传
#  输出：压缩后的视频二进制流 (video/mp4)
#        Dify HTTP 节点收到二进制 → create_file_by_raw → files 变量
# ══════════════════════════════════════════════════════════════════

async def _download_video(url: str, dest: Path, timeout: int = 300) -> int:
    size = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                        raise ValueError(f"文件过大，超过 {MAX_UPLOAD_SIZE_MB}MB 限制")
                    f.write(chunk)
    return size


async def _save_upload(video: UploadFile, dest: Path) -> int:
    size = 0
    with open(dest, "wb") as f:
        while chunk := await video.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                raise ValueError(f"文件过大，超过 {MAX_UPLOAD_SIZE_MB}MB 限制")
            f.write(chunk)
    return size


def _get_semaphore() -> asyncio.Semaphore:
    global ffmpeg_semaphore
    if ffmpeg_semaphore is None:
        ffmpeg_semaphore = asyncio.Semaphore(MAX_CONCURRENT_FFMPEG)
    return ffmpeg_semaphore


@router.post(
    "/compress_video/dify",
    summary="[Dify] 视频压缩 → 返回视频二进制",
    description=(
        "压缩视频后直接返回 video/mp4 二进制流。\n\n"
        "Dify HTTP 节点收到二进制响应会自动 `create_file_by_raw`，"
        "输出变量即为 `File` 类型，可直接传入 LLM Vision 节点。\n\n"
        "**输入方式 1 — JSON Body（Dify 工作流）**\n"
        '```json\n{"url": "{{video_resources.url}}"}\n```\n\n'
        "**输入方式 2 — form-data（文件直传）**\n"
        "字段名 `video`，值为视频文件。\n\n"
        "压缩参数通过 Query Params 传入，如 `?crf=28&preset=veryfast`。"
    ),
    response_class=FileResponse,
    responses={
        200: {"content": {"video/mp4": {}}, "description": "压缩后的视频文件"},
    },
)
async def compress_video_for_dify(
        request: Request,
        background_tasks: BackgroundTasks,
        crf: int = Query(28, description="恒定质量因子 (0-51)", ge=0, le=51),
        preset: CompressionPreset = Query(CompressionPreset.VERYFAST, description="编码速度预设"),
        resolution: Optional[str] = Query(None, description="目标分辨率，如 1280x720"),
        max_bitrate: Optional[str] = Query(None, description="最大码率限制，如 2M"),
        audio_bitrate: str = Query("128k", description="音频码率"),
):
    content_type = request.headers.get("content-type", "")
    resolution = resolution.strip() if resolution else None
    max_bitrate = max_bitrate.strip() if max_bitrate else None

    task_id = str(uuid.uuid4())
    input_path: Optional[Path] = None
    output_path: Optional[Path] = None

    try:
        # ── 模式 1: form-data 文件直传 ──
        if "multipart/form-data" in content_type:
            form = await request.form()
            video = form.get("video")
            if video is None or not hasattr(video, "read"):
                return create_standard_response(code=400, message="form-data 中缺少 video 文件字段")

            original_name = video.filename or "upload.mp4"
            ext = Path(original_name).suffix.lower() or ".mp4"
            if ext not in ALLOWED_VIDEO_EXTENSIONS:
                return create_standard_response(code=400, message=f"不支持的视频格式 '{ext}'")

            input_path = UPLOAD_DIR / f"{task_id}_input{ext}"
            output_path = OUTPUT_DIR / f"{task_id}_compressed.mp4"

            logger.info(f"[dify][{task_id}] 模式: 文件直传 ({original_name})")
            file_size = await _save_upload(video, input_path)
            logger.info(f"[dify][{task_id}] 已保存: {round(file_size / 1024 / 1024, 2)}MB")

        # ── 模式 2: JSON body (Dify HTTP 节点) ──
        else:
            body = await request.json()
            file_url = body.get("url") or body.get("remote_url")
            if not file_url:
                return create_standard_response(code=400, message="JSON body 中缺少 url 字段")

            original_name = body.get("name") or body.get("filename") or Path(file_url).name or "video.mp4"
            ext = Path(original_name).suffix.lower()
            if not ext:
                ext = body.get("extension", ".mp4")
                if not ext.startswith("."):
                    ext = f".{ext}"
            if ext not in ALLOWED_VIDEO_EXTENSIONS:
                return create_standard_response(code=400, message=f"不支持的视频格式 '{ext}'")

            input_path = UPLOAD_DIR / f"{task_id}_input{ext}"
            output_path = OUTPUT_DIR / f"{task_id}_compressed.mp4"

            logger.info(f"[dify][{task_id}] 模式: URL 下载 ({file_url})")
            file_size = await _download_video(file_url, input_path)
            logger.info(f"[dify][{task_id}] 下载完成: {round(file_size / 1024 / 1024, 2)}MB")

        # ── 压缩 ──
        sem = _get_semaphore()
        logger.info(f"[dify][{task_id}] 等待信号量 (并发限制 {MAX_CONCURRENT_FFMPEG})")

        async with sem:
            logger.info(f"[dify][{task_id}] 开始压缩")
            result = await asyncio.to_thread(
                compress_video,
                str(input_path),
                str(output_path),
                crf=crf,
                preset=preset.value,
                resolution=resolution,
                max_bitrate=max_bitrate,
                audio_bitrate=audio_bitrate,
            )

        logger.info(
            f"[dify][{task_id}] 完成: "
            f"{result['original_size_mb']}MB -> {result['compressed_size_mb']}MB "
            f"(压缩率 {result['compression_ratio']}%)"
        )

        # 清理上传的原始文件；输出文件在 FileResponse 发送完毕后由 background_tasks 清理
        input_path.unlink(missing_ok=True)
        background_tasks.add_task(cleanup_files, [output_path], delay=600)

        compressed_name = f"compressed_{Path(original_name).stem}.mp4"
        return FileResponse(
            path=str(output_path),
            media_type="video/mp4",
            filename=compressed_name,
        )

    except ValueError as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        logger.warning(f"[dify][{task_id}] {e}")
        return create_standard_response(code=413, message=str(e))
    except httpx.HTTPStatusError as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        logger.error(f"[dify][{task_id}] 下载失败: {e.response.status_code}")
        return create_standard_response(code=502, message=f"下载视频失败: HTTP {e.response.status_code}")
    except RuntimeError as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        if output_path:
            output_path.unlink(missing_ok=True)
        logger.error(f"[dify][{task_id}] 压缩失败: {e}")
        return create_standard_response(code=500, message=str(e))
    except Exception as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        if output_path:
            output_path.unlink(missing_ok=True)
        logger.exception(f"[dify][{task_id}] 未知错误: {e}")
        return create_standard_response(code=500, message=f"内部错误: {str(e)}")
