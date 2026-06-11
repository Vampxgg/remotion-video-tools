# -*- coding: utf-8 -*-
# @File：video_compress.py
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com

import asyncio
import os
import uuid
import json
import re
import logging
import shutil
import subprocess
from enum import Enum
from pathlib import Path
from typing import Optional, Any, Dict, Tuple
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
# 与 main.py 的 StaticFiles(directory=settings.static_dir_abs) 同一物理根目录
STATIC_DIR_NAME = _settings.STATIC_DIR
COMPRESS_UPLOAD_SUBDIR = _settings.VIDEO_COMPRESS_UPLOAD_SUBDIR
COMPRESS_OUTPUT_SUBDIR = _settings.VIDEO_COMPRESS_OUTPUT_SUBDIR
COMPRESS_TASK_STATE_SUBDIR = _settings.VIDEO_COMPRESS_TASK_STATE_SUBDIR

_STATIC_ROOT = Path(_settings.static_dir_abs)
UPLOAD_DIR = _STATIC_ROOT / COMPRESS_UPLOAD_SUBDIR
OUTPUT_DIR = _STATIC_ROOT / COMPRESS_OUTPUT_SUBDIR
TASK_STATE_DIR = _settings.project_root / COMPRESS_TASK_STATE_SUBDIR
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TASK_STATE_DIR, exist_ok=True)

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".ts", ".mts"}
MAX_UPLOAD_SIZE_MB = _settings.VIDEO_COMPRESS_MAX_UPLOAD_MB

# ──────────────────────────── 并发控制 ────────────────────────────
MAX_CONCURRENT_FFMPEG = _settings.VIDEO_COMPRESS_MAX_CONCURRENT_FFMPEG
FFMPEG_THREADS = _settings.VIDEO_COMPRESS_FFMPEG_THREADS
FFMPEG_TIMEOUT_SEC = _settings.VIDEO_COMPRESS_TIMEOUT_SEC
CLEANUP_DELAY_SEC = _settings.VIDEO_COMPRESS_CLEANUP_DELAY_SEC
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


class WebmPreset(str, Enum):
    QUALITY = "quality"
    BALANCED = "balanced"
    FAST = "fast"
    FASTEST = "fastest"


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


def _normalize_ffmpeg_max_bitrate(value: Optional[str]) -> Optional[str]:
    """过滤 Swagger/Dify 占位符与非法码率，避免 ffmpeg 收到字面量 'string' 等。"""
    if value is None:
        return None
    t = value.strip()
    if not t:
        return None
    tl = t.lower()
    if tl in ("string", "none", "null", "undefined", "-", "nan", "optional", "text"):
        return None
    # 典型 ffmpeg 码率：2M、500k、1.5M、8000000（纯数字视为比特率，谨慎允许 4–9 位）
    if re.fullmatch(r"\d+(\.\d+)?[kKmMgG]?", t):
        return t
    if re.fullmatch(r"\d{4,9}", t):
        return t
    return None


def _normalize_webm_bitrate(value: Optional[str]) -> Optional[str]:
    """WebM 转码只接受 ffmpeg 常见码率格式；空值表示 VP9 CRF 模式。"""
    if value is None:
        return None
    t = value.strip()
    if not t:
        return None
    tl = t.lower()
    if tl in ("string", "none", "null", "undefined", "-", "nan", "optional", "text"):
        return None
    if re.fullmatch(r"\d+(\.\d+)?[kKmMgG]?", t):
        return t
    if re.fullmatch(r"\d{4,9}", t):
        return t
    return None


def _state_path(task_id: str) -> Path:
    return TASK_STATE_DIR / f"{task_id}.json"


def _write_task_state(task_id: str, payload: Dict[str, Any]) -> None:
    path = _state_path(task_id)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _read_task_state(task_id: str) -> Optional[Dict[str, Any]]:
    path = _state_path(task_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"[dify][{task_id}] 读取任务状态失败: {exc}")
        return None


def _public_task_payload(task: Dict[str, Any]) -> Dict[str, Any]:
    hidden = {"input_path", "output_path", "state_path"}
    return {k: v for k, v in task.items() if k not in hidden}


def _elapsed_sec(task: Dict[str, Any]) -> Optional[float]:
    submitted_at = task.get("submitted_at")
    if not submitted_at:
        return None
    try:
        start = datetime.fromisoformat(submitted_at)
        end_raw = task.get("completed_at")
        end = datetime.fromisoformat(end_raw) if end_raw else datetime.now()
        return round((end - start).total_seconds(), 3)
    except Exception:
        return None


def _task_urls(base_url: str, task_id: str) -> Tuple[str, str]:
    base = base_url.rstrip("/")
    status_url = f"{base}/api/compress_video/dify/tasks/{task_id}"
    file_url = f"{base}/api/compress_video/dify/tasks/{task_id}/file"
    return status_url, file_url


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
        threads: int = FFMPEG_THREADS,
        timeout_sec: int = FFMPEG_TIMEOUT_SEC,
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

    if threads and threads > 0:
        cmd.extend(["-threads", str(threads)])

    if resolution and "x" in resolution:
        parts = resolution.split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            cmd.extend(["-vf", f"scale={parts[0]}:{parts[1]}"])

    mb = _normalize_ffmpeg_max_bitrate(max_bitrate)
    if mb:
        cmd.extend(["-maxrate:v", mb, "-bufsize:v", mb])

    cmd.append(output_path)

    logger.info(f"执行 FFmpeg 命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)

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


def build_webm_video_options(
        preset: str,
        crf: int,
        bitrate: Optional[str],
        threads: int = FFMPEG_THREADS,
) -> list[str]:
    """复刻 convert_webm.py 的预设逻辑，并让线程数沿用服务端配置。"""
    preset_value = WebmPreset(preset)
    normalized_bitrate = _normalize_webm_bitrate(bitrate)
    if preset_value == WebmPreset.FASTEST:
        return [
            "-c:v", "libvpx",
            "-deadline", "realtime",
            "-cpu-used", "8",
            "-b:v", normalized_bitrate if normalized_bitrate else "1200k",
        ]

    cpu_used = {
        WebmPreset.QUALITY: "2",
        WebmPreset.BALANCED: "4",
        WebmPreset.FAST: "6",
    }[preset_value]

    return [
        "-c:v", "libvpx-vp9",
        "-deadline", "good",
        "-cpu-used", cpu_used,
        "-row-mt", "1",
        "-threads", str(threads if threads and threads > 0 else 0),
        "-crf", str(crf),
        "-b:v", normalized_bitrate if normalized_bitrate else "0",
    ]


def convert_video_to_webm(
        input_path: str,
        output_path: str,
        crf: int = 32,
        bitrate: Optional[str] = None,
        preset: str = WebmPreset.FASTEST.value,
        audio_bitrate: str = "96k",
        threads: int = FFMPEG_THREADS,
        timeout_sec: int = FFMPEG_TIMEOUT_SEC,
) -> dict:
    input_file = Path(input_path)
    output_file = Path(output_path)
    if not input_file.is_file():
        raise FileNotFoundError(f"输入文件不存在: {input_file}")
    output_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i", str(input_file),
        *build_webm_video_options(preset, crf, bitrate, threads=threads),
        "-c:a", "libopus",
        "-b:a", audio_bitrate,
        str(output_file),
    ]

    logger.info(f"执行 WebM 转换命令: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg WebM 转换失败: {result.stderr[-500:]}")

    original_size = os.path.getsize(input_file)
    converted_size = os.path.getsize(output_file)
    ratio = (1 - converted_size / original_size) * 100 if original_size > 0 else 0
    return {
        "original_size_bytes": original_size,
        "converted_size_bytes": converted_size,
        "original_size_mb": round(original_size / (1024 * 1024), 2),
        "converted_size_mb": round(converted_size / (1024 * 1024), 2),
        "compression_ratio": round(ratio, 2),
        "preset": WebmPreset(preset).value,
        "crf": crf,
        "bitrate": _normalize_webm_bitrate(bitrate),
        "audio_bitrate": audio_bitrate,
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
        asyncio.create_task(cleanup_files(paths_to_clean, delay=CLEANUP_DELAY_SEC))


async def _run_dify_async_task(
        task_id: str,
        input_path: Path,
        output_path: Path,
        crf: int,
        preset: str,
        resolution: Optional[str],
        max_bitrate: Optional[str],
        audio_bitrate: str,
):
    sem = _get_semaphore()
    task = _read_task_state(task_id) or {}
    task.update({
        "status": TaskStatus.PENDING,
        "updated_at": datetime.now().isoformat(),
        "elapsed_sec": _elapsed_sec(task),
    })
    _write_task_state(task_id, task)
    logger.info(f"[dify-async][{task_id}] 排队等待信号量 (并发限制 {MAX_CONCURRENT_FFMPEG})")

    try:
        async with sem:
            task = _read_task_state(task_id) or task
            task.update({
                "status": TaskStatus.PROCESSING,
                "started_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "elapsed_sec": _elapsed_sec(task),
            })
            _write_task_state(task_id, task)
            logger.info(f"[dify-async][{task_id}] 开始压缩")

            result = await asyncio.to_thread(
                compress_video,
                str(input_path),
                str(output_path),
                crf=crf,
                preset=preset,
                resolution=resolution,
                max_bitrate=max_bitrate,
                audio_bitrate=audio_bitrate,
            )

            task = _read_task_state(task_id) or task
            task.update({
                "status": TaskStatus.COMPLETED,
                "completed_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                **result,
            })
            task["elapsed_sec"] = _elapsed_sec(task)
            _write_task_state(task_id, task)
            input_path.unlink(missing_ok=True)
            asyncio.create_task(cleanup_files([output_path, _state_path(task_id)], delay=CLEANUP_DELAY_SEC))

            logger.info(
                f"[dify-async][{task_id}] 完成: "
                f"{result['original_size_mb']}MB -> {result['compressed_size_mb']}MB "
                f"(压缩率 {result['compression_ratio']}%)"
            )

    except Exception as e:
        input_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        task = _read_task_state(task_id) or task
        task.update({
            "status": TaskStatus.FAILED,
            "error": str(e),
            "completed_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        })
        task["elapsed_sec"] = _elapsed_sec(task)
        _write_task_state(task_id, task)
        logger.exception(f"[dify-async][{task_id}] 压缩失败: {e}")


async def _run_webm_task(
        task_id: str,
        input_path: Path,
        output_path: Path,
        base_url: str,
        crf: int,
        bitrate: Optional[str],
        preset: str,
        audio_bitrate: str,
):
    sem = _get_semaphore()
    task = _read_task_state(task_id) or {}
    task.update({
        "status": TaskStatus.PENDING,
        "updated_at": datetime.now().isoformat(),
        "elapsed_sec": _elapsed_sec(task),
    })
    _write_task_state(task_id, task)
    logger.info(f"[webm][{task_id}] 排队等待信号量 (当前限制 {MAX_CONCURRENT_FFMPEG} 并发)")

    try:
        async with sem:
            task = _read_task_state(task_id) or task
            task.update({
                "status": TaskStatus.PROCESSING,
                "started_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "elapsed_sec": _elapsed_sec(task),
            })
            _write_task_state(task_id, task)
            logger.info(f"[webm][{task_id}] 获得信号量，开始转换 WebM")

            convert_result = await asyncio.to_thread(
                convert_video_to_webm,
                str(input_path),
                str(output_path),
                crf=crf,
                bitrate=bitrate,
                preset=preset,
                audio_bitrate=audio_bitrate,
            )

            download_url = (
                f"{base_url.rstrip('/')}/{STATIC_DIR_NAME}/"
                f"{COMPRESS_OUTPUT_SUBDIR}/{output_path.name}"
            )

            task = _read_task_state(task_id) or task
            task.update({
                "status": TaskStatus.COMPLETED,
                "download_url": download_url,
                "completed_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                **convert_result,
            })
            task["elapsed_sec"] = _elapsed_sec(task)
            _write_task_state(task_id, task)
            input_path.unlink(missing_ok=True)
            asyncio.create_task(cleanup_files([output_path, _state_path(task_id)], delay=CLEANUP_DELAY_SEC))

            logger.info(
                f"[webm][{task_id}] 转换完成: "
                f"{convert_result['original_size_mb']}MB -> {convert_result['converted_size_mb']}MB "
                f"(压缩率 {convert_result['compression_ratio']}%)"
            )

    except Exception as e:
        input_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        task = _read_task_state(task_id) or task
        task.update({
            "status": TaskStatus.FAILED,
            "error": str(e),
            "completed_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        })
        task["elapsed_sec"] = _elapsed_sec(task)
        _write_task_state(task_id, task)
        logger.exception(f"[webm][{task_id}] 转换失败: {e}")


# ──────────────────────────── API 端点 ────────────────────────────

@router.post(
    "/compress_video",
    summary="提交视频压缩任务",
    description=(
        "上传视频文件后立即返回 task_id，压缩在后台异步执行；通过 GET /api/compress_video/{task_id} 查询进度。\n\n"
        "**Swagger 试用**：点「Try it out」后，请求体类型为 **multipart/form-data**；"
        "必须选择 **video** 文件字段，并按需填写下方其它表单字段（勿选 raw JSON）。"
    ),
)
async def submit_compress_task(
        request: Request,
        video: UploadFile = File(..., description="要压缩的视频文件（form-data 字段名 video）"),
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


@router.post(
    "/convert_webm",
    summary="提交视频转 WebM 任务",
    description=(
        "封装 convert_webm.py：上传视频文件后立即返回 task_id，后台使用 ffmpeg 转为 WebM。"
        "通过 GET /api/convert_webm/{task_id} 查询进度，完成后可下载 .webm 文件。"
    ),
)
async def submit_webm_task(
        request: Request,
        video: UploadFile = File(..., description="要转换为 WebM 的视频文件（form-data 字段名 video）"),
        crf: int = Form(32, description="VP9 模式下的 CRF 压缩质量，值越大文件越小，推荐 28-38", ge=0, le=63),
        bitrate: Optional[str] = Form(None, description="视频码率，如 800k、1M；fastest 留空默认 1200k，VP9 留空则使用 CRF 模式"),
        preset: WebmPreset = Form(WebmPreset.FASTEST, description="转换速度预设；默认 fastest 使用 VP8 实时编码，速度优先"),
        audio_bitrate: str = Form("96k", description="Opus 音频码率，如 64k、96k、128k"),
):
    bitrate = _normalize_webm_bitrate(bitrate)
    ext = Path(video.filename or "").suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return create_standard_response(
            code=400,
            message=f"不支持的视频格式 '{ext}'，支持的格式: {', '.join(ALLOWED_VIDEO_EXTENSIONS)}"
        )

    task_id = str(uuid.uuid4())
    input_filename = f"{task_id}_input{ext}"
    output_filename = f"{task_id}_converted.webm"
    input_path = UPLOAD_DIR / input_filename
    output_path = OUTPUT_DIR / output_filename

    try:
        file_size = await _save_upload(video, input_path)
        logger.info(f"[webm][{task_id}] 文件已保存: {input_path} ({round(file_size / 1024 / 1024, 2)}MB)")

        base_url = str(request.base_url).rstrip("/")
        state = {
            "task_id": task_id,
            "status": TaskStatus.PENDING,
            "original_filename": video.filename,
            "original_size_mb": round(file_size / 1024 / 1024, 2),
            "submitted_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "status_url": f"{base_url}/api/convert_webm/{task_id}",
            "file_url": f"{base_url}/api/convert_webm/{task_id}/file",
            "download_url": None,
            "input_path": str(input_path),
            "output_path": str(output_path),
            "state_path": str(_state_path(task_id)),
            "error": None,
            "crf": crf,
            "bitrate": bitrate,
            "preset": preset.value,
            "audio_bitrate": audio_bitrate,
        }
        _write_task_state(task_id, state)

        asyncio.create_task(
            _run_webm_task(
                task_id=task_id,
                input_path=input_path,
                output_path=output_path,
                base_url=str(request.base_url),
                crf=crf,
                bitrate=bitrate,
                preset=preset.value,
                audio_bitrate=audio_bitrate,
            )
        )

        return create_standard_response(
            data=_public_task_payload(state),
            message="WebM 转换任务已提交，请通过 GET /api/convert_webm/{task_id} 查询进度"
        )

    except ValueError as e:
        input_path.unlink(missing_ok=True)
        return create_standard_response(code=413, message=str(e))
    except Exception as e:
        input_path.unlink(missing_ok=True)
        logger.exception(f"[webm][{task_id}] 提交失败: {e}")
        return create_standard_response(code=500, message=f"提交任务失败: {str(e)}")


@router.get(
    "/convert_webm/{task_id}",
    summary="查询 WebM 转换任务状态",
    description="通过 task_id 查询 WebM 转换任务的当前状态和结果。"
)
async def query_webm_task(task_id: str):
    task = _read_task_state(task_id) or tasks.get(task_id)
    if not task:
        output_path = OUTPUT_DIR / f"{task_id}_converted.webm"
        if output_path.is_file():
            return create_standard_response(data={
                "task_id": task_id,
                "status": TaskStatus.COMPLETED,
                "file_url": f"/api/convert_webm/{task_id}/file",
                "download_url": f"/{STATIC_DIR_NAME}/{COMPRESS_OUTPUT_SUBDIR}/{output_path.name}",
                "converted_size_bytes": output_path.stat().st_size,
                "converted_size_mb": round(output_path.stat().st_size / (1024 * 1024), 2),
                "warning": "任务状态不在当前 worker 或已丢失，已根据输出文件恢复结果",
            })
        return create_standard_response(code=404, message=f"任务 {task_id} 不存在")
    task["elapsed_sec"] = _elapsed_sec(task)
    return create_standard_response(data=_public_task_payload(task))


@router.get(
    "/convert_webm/{task_id}/file",
    summary="下载 WebM 转换完成的视频文件",
    response_class=FileResponse,
)
async def download_webm_file(task_id: str):
    task = _read_task_state(task_id) or tasks.get(task_id)
    if not task:
        output_path = OUTPUT_DIR / f"{task_id}_converted.webm"
        if output_path.is_file():
            return FileResponse(
                path=str(output_path),
                media_type="video/webm",
                filename=f"{task_id}.webm",
            )
        return create_standard_response(code=404, message=f"任务 {task_id} 不存在")
    if task.get("status") != TaskStatus.COMPLETED:
        return create_standard_response(
            code=409,
            message=f"任务尚未完成，当前状态: {task.get('status')}"
        )

    output_path = Path(task.get("output_path") or OUTPUT_DIR / f"{task_id}_converted.webm")
    if not output_path.is_file():
        return create_standard_response(code=404, message="WebM 文件不存在或已清理")

    original_name = task.get("original_filename") or "upload"
    converted_name = f"{Path(original_name).stem}.webm"
    return FileResponse(
        path=str(output_path),
        media_type="video/webm",
        filename=converted_name,
    )


# ══════════════════════════════════════════════════════════════════
#  Dify / Swagger：Body 仅 multipart，且只包含一个文件字段 video
#  压缩参数全部走 URL Query；成功响应仍为 video/mp4 二进制。
# ══════════════════════════════════════════════════════════════════


class DifyInputError(Exception):
    __slots__ = ("code", "message")

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


async def _dify_persist_upload(video: UploadFile, task_id: str) -> Tuple[Path, str]:
    """将已绑定的 UploadFile 落盘；字段名由 FastAPI/OpenAPI 固定为 video。"""
    original_name = video.filename or "upload.mp4"
    ext = Path(original_name).suffix.lower() or ".mp4"
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise DifyInputError(400, f"不支持的视频格式 '{ext}'")
    dest = UPLOAD_DIR / f"{task_id}_input{ext}"
    n = await _save_upload(video, dest)
    logger.info(f"[dify][{task_id}] 已接收 video ({original_name}) {round(n / 1024 / 1024, 2)}MB")
    return dest, original_name


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
    "/compress_video/dify/tasks",
    summary="[Dify] 异步提交视频压缩任务",
    description=(
        "上传 multipart/form-data 字段 video 后立即返回 task_id。"
        "压缩在后台执行，Dify 可轮询 status_url，完成后通过 file_url 获取 video/mp4。"
    ),
)
async def submit_dify_compress_task(
        request: Request,
        video: UploadFile = File(..., description="唯一 body 字段：视频文件，字段名必须为 video"),
        crf: int = Query(28, description="恒定质量因子 (0-51)", ge=0, le=51),
        preset: CompressionPreset = Query(CompressionPreset.VERYFAST, description="编码速度预设"),
        resolution: Optional[str] = Query(None, description="目标分辨率，如 1280x720"),
        max_bitrate: Optional[str] = Query(None, description="最大码率限制，如 2M"),
        audio_bitrate: str = Query("128k", description="音频码率"),
):
    resolution = resolution.strip() if resolution else None
    max_bitrate = max_bitrate.strip() if max_bitrate else None

    task_id = str(uuid.uuid4())
    input_path: Optional[Path] = None
    try:
        input_path, original_name = await _dify_persist_upload(video, task_id)
        output_path = OUTPUT_DIR / f"{task_id}_compressed.mp4"
        status_url, file_url = _task_urls(str(request.base_url), task_id)

        state = {
            "task_id": task_id,
            "status": TaskStatus.PENDING,
            "original_filename": original_name,
            "submitted_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "status_url": status_url,
            "file_url": file_url,
            "input_path": str(input_path),
            "output_path": str(output_path),
            "state_path": str(_state_path(task_id)),
            "crf": crf,
            "preset": preset.value,
            "resolution": resolution,
            "max_bitrate": max_bitrate,
            "audio_bitrate": audio_bitrate,
            "ffmpeg_threads": FFMPEG_THREADS,
            "error": None,
        }
        _write_task_state(task_id, state)

        asyncio.create_task(
            _run_dify_async_task(
                task_id=task_id,
                input_path=input_path,
                output_path=output_path,
                crf=crf,
                preset=preset.value,
                resolution=resolution,
                max_bitrate=max_bitrate,
                audio_bitrate=audio_bitrate,
            )
        )

        return create_standard_response(
            data=_public_task_payload(state),
            message="任务已提交，请轮询 status_url；完成后通过 file_url 下载压缩视频"
        )

    except DifyInputError as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        return create_standard_response(code=e.code, message=e.message)
    except ValueError as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        return create_standard_response(code=413, message=str(e))
    except Exception as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        logger.exception(f"[dify-async][{task_id}] 提交失败: {e}")
        return create_standard_response(code=500, message=f"提交任务失败: {str(e)}")


@router.get(
    "/compress_video/dify/tasks/{task_id}",
    summary="[Dify] 查询异步视频压缩任务状态",
)
async def query_dify_compress_task(task_id: str):
    task = _read_task_state(task_id)
    if not task:
        return create_standard_response(code=404, message=f"任务 {task_id} 不存在或已清理")
    task["elapsed_sec"] = _elapsed_sec(task)
    return create_standard_response(data=_public_task_payload(task))


@router.get(
    "/compress_video/dify/tasks/{task_id}/file",
    summary="[Dify] 下载异步压缩完成的视频文件",
    response_class=FileResponse,
)
async def download_dify_compress_file(task_id: str):
    task = _read_task_state(task_id)
    if not task:
        return create_standard_response(code=404, message=f"任务 {task_id} 不存在或已清理")
    if task.get("status") != TaskStatus.COMPLETED:
        return create_standard_response(
            code=409,
            message=f"任务尚未完成，当前状态: {task.get('status')}"
        )

    output_path = Path(task.get("output_path", ""))
    if not output_path.is_file():
        return create_standard_response(code=404, message="压缩文件不存在或已清理")

    original_name = task.get("original_filename") or "upload.mp4"
    compressed_name = f"compressed_{Path(original_name).stem}.mp4"
    return FileResponse(
        path=str(output_path),
        media_type="video/mp4",
        filename=compressed_name,
    )


# Swagger/OpenAPI：为二进制 200 声明 schema，减少「Undocumented」；并声明 JSON 类错误体
_COMPRESS_DIFY_ERR_JSON = {
    "application/json": {
        "schema": {
            "type": "object",
            "properties": {
                "code": {"type": "integer"},
                "message": {"type": "string"},
                "data": {"nullable": True},
                "timestamp": {"type": "string"},
            },
        }
    }
}

_COMPRESS_VIDEO_DIFY_OPENAPI_RESPONSES: Dict[int, Any] = {
    200: {
        "description": (
            "成功：返回压缩后的 MP4 文件流。"
            "Swagger UI 无法可靠预览二进制，可能仍显示 Undocumented 或空白；以 HTTP **200** 为准，"
            "请在 Swagger 使用「Download」或使用 curl：`curl ... -o out.mp4`。"
        ),
        "content": {
            "video/mp4": {
                "schema": {
                    "type": "string",
                    "format": "binary",
                    "description": "完整 MP4 文件字节",
                }
            }
        },
        "headers": {
            "Content-Disposition": {
                "description": "浏览器/客户端下载文件名",
                "schema": {"type": "string", "example": 'attachment; filename="compressed_xxx.mp4"'},
            }
        },
    },
    400: {"description": "业务拒绝（如扩展名不支持）", "content": _COMPRESS_DIFY_ERR_JSON},
    413: {"description": "文件过大", "content": _COMPRESS_DIFY_ERR_JSON},
    422: {"description": "参数校验失败（如未上传 multipart、字段名不是 video）", "content": _COMPRESS_DIFY_ERR_JSON},
    500: {"description": "压缩失败或内部错误", "content": _COMPRESS_DIFY_ERR_JSON},
}


@router.post(
    "/compress_video/dify",
    summary="[Dify] 视频压缩 → 返回 video/mp4 二进制",
    description=(
        "**Body**：`multipart/form-data`，且**只传一个文件**，字段名必须为 **`video`**（Swagger / Dify 一致）。\n"
        "**参数**：`crf`、`preset`、`resolution`、`max_bitrate`、`audio_bitrate` 全部放在 **URL Query**，不要放在 form 里。\n\n"
        "**Swagger 说明**：成功时返回 **video/mp4 二进制**，与 JSON 接口不同；界面可能无法预览视频、或仍显示 Undocumented，"
        "属 Swagger UI 限制，**只要状态码为 200 即成功**，请下载保存为 `.mp4` 后播放。"
    ),
    response_class=FileResponse,
    responses=_COMPRESS_VIDEO_DIFY_OPENAPI_RESPONSES,
)
async def compress_video_for_dify(
        background_tasks: BackgroundTasks,
        video: UploadFile = File(
            ...,
            description="唯一 body 字段：视频文件，multipart 中字段名必须为 video",
        ),
        crf: int = Query(28, description="恒定质量因子 (0-51)", ge=0, le=51),
        preset: CompressionPreset = Query(CompressionPreset.VERYFAST, description="编码速度预设"),
        resolution: Optional[str] = Query(None, description="目标分辨率，如 1280x720"),
        max_bitrate: Optional[str] = Query(None, description="最大码率限制，如 2M"),
        audio_bitrate: str = Query("128k", description="音频码率"),
):
    resolution = resolution.strip() if resolution else None
    max_bitrate = max_bitrate.strip() if max_bitrate else None

    task_id = str(uuid.uuid4())
    input_path: Optional[Path] = None
    output_path: Optional[Path] = None

    try:
        input_path, original_name = await _dify_persist_upload(video, task_id)
        output_path = OUTPUT_DIR / f"{task_id}_compressed.mp4"
        preset_s = preset.value

        sem = _get_semaphore()
        logger.info(f"[dify][{task_id}] 等待信号量 (并发限制 {MAX_CONCURRENT_FFMPEG})")

        async with sem:
            logger.info(f"[dify][{task_id}] 开始压缩")
            result = await asyncio.to_thread(
                compress_video,
                str(input_path),
                str(output_path),
                crf=crf,
                preset=preset_s,
                resolution=resolution,
                max_bitrate=max_bitrate,
                audio_bitrate=audio_bitrate,
            )

        logger.info(
            f"[dify][{task_id}] 完成: "
            f"{result['original_size_mb']}MB -> {result['compressed_size_mb']}MB "
            f"(压缩率 {result['compression_ratio']}%)"
        )

        input_path.unlink(missing_ok=True)
        background_tasks.add_task(cleanup_files, [output_path], delay=CLEANUP_DELAY_SEC)

        compressed_name = f"compressed_{Path(original_name).stem}.mp4"
        return FileResponse(
            path=str(output_path),
            media_type="video/mp4",
            filename=compressed_name,
        )

    except DifyInputError as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        return create_standard_response(code=e.code, message=e.message)
    except ValueError as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        logger.warning(f"[dify][{task_id}] {e}")
        return create_standard_response(code=413, message=str(e))
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


_CONVERT_WEBM_DIFY_OPENAPI_RESPONSES: Dict[int, Any] = {
    200: {
        "description": "成功：返回转换后的 WebM 文件流。",
        "content": {
            "video/webm": {
                "schema": {
                    "type": "string",
                    "format": "binary",
                    "description": "完整 WebM 文件字节",
                }
            }
        },
    },
    400: {"description": "业务拒绝（如扩展名不支持）", "content": _COMPRESS_DIFY_ERR_JSON},
    413: {"description": "文件过大", "content": _COMPRESS_DIFY_ERR_JSON},
    422: {"description": "参数校验失败（如未上传 multipart、字段名不是 video）", "content": _COMPRESS_DIFY_ERR_JSON},
    500: {"description": "转换失败或内部错误", "content": _COMPRESS_DIFY_ERR_JSON},
}


@router.post(
    "/convert_webm/dify",
    summary="[Dify] 视频转 WebM → 返回 video/webm 二进制",
    description=(
        "**Body**：`multipart/form-data`，且只传一个文件，字段名必须为 **`video`**。\n"
        "**参数**：`crf`、`bitrate`、`preset`、`audio_bitrate` 全部放在 **URL Query**。\n"
        "封装 `convert_webm.py` 的转换逻辑，输出 `.webm` 文件。"
    ),
    response_class=FileResponse,
    responses=_CONVERT_WEBM_DIFY_OPENAPI_RESPONSES,
)
async def convert_webm_for_dify(
        background_tasks: BackgroundTasks,
        video: UploadFile = File(
            ...,
            description="唯一 body 字段：视频文件，multipart 中字段名必须为 video",
        ),
        crf: int = Query(32, description="VP9 模式下的 CRF 压缩质量，值越大文件越小，推荐 28-38", ge=0, le=63),
        bitrate: Optional[str] = Query(None, description="视频码率，如 800k、1M；fastest 留空默认 1200k，VP9 留空则使用 CRF 模式"),
        preset: WebmPreset = Query(WebmPreset.FASTEST, description="转换速度预设；默认 fastest 使用 VP8 实时编码，速度优先"),
        audio_bitrate: str = Query("96k", description="Opus 音频码率"),
):
    task_id = str(uuid.uuid4())
    input_path: Optional[Path] = None
    output_path: Optional[Path] = None

    try:
        input_path, original_name = await _dify_persist_upload(video, task_id)
        output_path = OUTPUT_DIR / f"{task_id}_converted.webm"
        normalized_bitrate = _normalize_webm_bitrate(bitrate)

        sem = _get_semaphore()
        logger.info(f"[webm-dify][{task_id}] 等待信号量 (并发限制 {MAX_CONCURRENT_FFMPEG})")
        async with sem:
            logger.info(f"[webm-dify][{task_id}] 开始转换 WebM")
            result = await asyncio.to_thread(
                convert_video_to_webm,
                str(input_path),
                str(output_path),
                crf=crf,
                bitrate=normalized_bitrate,
                preset=preset.value,
                audio_bitrate=audio_bitrate,
            )

        logger.info(
            f"[webm-dify][{task_id}] 完成: "
            f"{result['original_size_mb']}MB -> {result['converted_size_mb']}MB "
            f"(压缩率 {result['compression_ratio']}%)"
        )

        input_path.unlink(missing_ok=True)
        background_tasks.add_task(cleanup_files, [output_path], delay=CLEANUP_DELAY_SEC)
        converted_name = f"{Path(original_name).stem}.webm"
        return FileResponse(
            path=str(output_path),
            media_type="video/webm",
            filename=converted_name,
        )

    except DifyInputError as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        return create_standard_response(code=e.code, message=e.message)
    except ValueError as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        logger.warning(f"[webm-dify][{task_id}] {e}")
        return create_standard_response(code=413, message=str(e))
    except RuntimeError as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        if output_path:
            output_path.unlink(missing_ok=True)
        logger.error(f"[webm-dify][{task_id}] 转换失败: {e}")
        return create_standard_response(code=500, message=str(e))
    except Exception as e:
        if input_path:
            input_path.unlink(missing_ok=True)
        if output_path:
            output_path.unlink(missing_ok=True)
        logger.exception(f"[webm-dify][{task_id}] 未知错误: {e}")
        return create_standard_response(code=500, message=f"内部错误: {str(e)}")
