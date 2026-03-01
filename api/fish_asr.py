# -*- coding: utf-8 -*-
# @File：main_app_asr.py
# @Time：2025/8/6 10:00
# @Author：_不咬闰土的猹丶 & Your Senior Software Engineer
# @email：hx1561958968@gmail.com

# --- 导入模块 ---
import os
import logging
import concurrent.futures
import time
import queue
import sys
import asyncio
from asyncio import Semaphore
import threading
from typing import Dict, List, Any, Optional

# HTTP 客户端
import httpx

# FastAPI 相关导入
from fastapi import APIRouter, HTTPException, status, UploadFile, File, Form
from pydantic import BaseModel, Field

# 沿用你已有的优秀设计
from datetime import datetime
from fastapi.responses import JSONResponse

# ======================================================================================
# --- [V1] 工业级日志配置 (沿用现有标准) ---
# ======================================================================================
try:
    # 假设你有一个集中的日志设置模块
    from utils.logger import setup_module_logger
except ImportError:
    # 如果找不到，提供一个备用方案，确保应用可以启动
    def setup_module_logger(logger_name: str, log_file: str) -> logging.Logger:
        logger = logging.getLogger(logger_name)
        if not logger.hasHandlers():
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            # 打印一条关键信息，告知正在使用备用日志记录器
            print(f"CRITICAL: Using fallback console logger for {logger_name}.")
        return logger

# 初始化模块日志记录器
logger = setup_module_logger(__name__, "logs/audio/fish_asr.log")
# ======================================================================================

# 创建 ASR 模块专用的 APIRouter，便于模块化管理
# 可以在主应用中通过 app.include_router(router_asr) 集成
router_asr = APIRouter(prefix="/asr", tags=["Audio Speech Recognition"])

# --- 全局资源 ---
asr_thread_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
api_semaphore_asr: Optional[asyncio.Semaphore] = None
async_http_client: Optional[httpx.AsyncClient] = None


# --- 应用生命周期事件管理 ---
@router_asr.on_event("startup")
def startup_event():
    global asr_thread_pool, api_semaphore_asr, async_http_client

    # 初始化用于CPU密集型任务或阻塞IO的线程池
    asr_thread_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=10,
        thread_name_prefix="Global_ASR_Worker"
    )
    # 初始化信号量，控制对外部API的并发请求数量
    api_semaphore_asr = Semaphore(20)

    # 初始化可复用的异步HTTP客户端
    proxies = PROXY_URL if PROXY_URL else None
    timeout = httpx.Timeout(10.0, connect=5.0, read=60.0, write=10.0)
    async_http_client = httpx.AsyncClient(timeout=timeout)

    logger.info("ASR 模块启动完成。")
    logger.info(f"ASR 线程池已创建，最大工作线程数: 10")
    logger.info(f"ASR API 信号量已创建，许可数: 20")
    logger.info(f"全局共享的 httpx.AsyncClient 已创建。代理: {'启用' if proxies else '未配置'}")


@router_asr.on_event("shutdown")
async def shutdown_event():
    global asr_thread_pool, async_http_client

    # 优雅关闭线程池
    if asr_thread_pool:
        logger.info("正在关闭 ASR 线程池...")
        asr_thread_pool.shutdown(wait=True)
        logger.info("ASR 线程池已成功关闭。")

    # 优雅关闭HTTP客户端连接池
    if async_http_client and not async_http_client.is_closed:
        logger.info("正在关闭全局共享的 httpx.AsyncClient...")
        await async_http_client.aclose()
        logger.info("全局共享的 httpx.AsyncClient 已成功关闭。")

    logger.info("ASR 模块已成功关闭。")


# --- 配置区 ---
PROXY_URL = ""  # 示例: "http://127.0.0.1:7890"
FISH_ASR_API_URL = "https://api.fish.audio/v1/asr"
FISH_API_KEY = "dae51de32a0743f6b4f2f7b6366747bf"  # 强烈建议从环境变量或配置文件中加载
MAX_ASR_RETRIES = 3
ASR_RETRY_DELAY = 1.5  # 秒


# ======================================================================================
# --- API 响应模型与 Pydantic 模型定义 ---
# ======================================================================================

class StandardResponse(BaseModel):
    """标准的API响应模型"""
    code: int = Field(200, description="HTTP状态码")
    message: str = Field("Success", description="响应消息")
    data: Optional[Any] = Field(None, description="响应数据")
    timestamp: str = Field(..., description="ISO 8601 格式的时间戳")


class ASRResultItem(BaseModel):
    """单个音频文件在批量处理中的结果"""
    filename: str = Field(..., description="原始上传的文件名")
    status: str = Field(..., description="处理状态 ('success' 或 'failed')")
    data: Optional[Dict[str, Any]] = Field(None, description="成功时，ASR API返回的数据")
    error: Optional[str] = Field(None, description="失败时，具体的错误信息")


class BatchASRResponseData(BaseModel):
    """批量ASR任务的响应数据模型"""
    total_files: int = Field(..., description="接收到的文件总数")
    success_count: int = Field(..., description="成功处理的文件数")
    failed_count: int = Field(..., description="处理失败的文件数")
    results: List[ASRResultItem] = Field(..., description="每个文件的详细处理结果列表")


# --- 工具函数 ---

def create_standard_response(
        data: Optional[Any] = None,
        code: int = 200,
        message: str = "Success"
) -> JSONResponse:
    """创建一个标准格式的 FastAPI 响应。"""
    content = StandardResponse(
        code=code,
        message=message,
        data=data,
        timestamp=datetime.now().isoformat()
    ).model_dump()
    return JSONResponse(status_code=code, content=content)


# ======================================================================================
# --- 核心业务逻辑函数 ---
# ======================================================================================

async def _perform_asr_request(
        workflow_id: str,
        language: str,
        ignore_timestamps: bool,
        audio_file: UploadFile
) -> Dict[str, Any]:
    """
    执行对 Fish Audio ASR API 的核心请求，包含重试逻辑。
    此函数是所有ASR操作的基础。

    :param workflow_id: 用于日志记录的工作流ID。
    :param language: 音频语言。
    :param ignore_timestamps: 是否忽略时间戳。
    :param audio_file: 上传的音频文件对象。
    :return: ASR API 返回的 JSON 数据。
    :raises: 如果所有重试都失败，则抛出 Exception。
    """
    headers = {'Authorization': f'Bearer {FISH_API_KEY}'}
    form_data = {
        'language': language,
        'ignore_timestamps': str(ignore_timestamps).lower()  # API需要字符串'true'/'false'
    }

    audio_content = await audio_file.read()
    if not audio_content:
        raise ValueError(f"提供的音频文件 '{audio_file.filename}' 为空。")

    files = {'audio': (audio_file.filename, audio_content, audio_file.content_type)}

    last_exception = None
    for attempt in range(MAX_ASR_RETRIES):
        try:
            logger.info(f"[{workflow_id}] 第 {attempt + 1}/{MAX_ASR_RETRIES} 次尝试调用 ASR API...")

            response = await async_http_client.post(
                FISH_ASR_API_URL,
                headers=headers,
                data=form_data,
                files=files
            )

            response.raise_for_status()

            logger.info(f"[{workflow_id}] ASR API 调用成功，状态码: {response.status_code}")
            return response.json()

        except httpx.HTTPStatusError as e:
            last_exception = e
            error_body = e.response.text
            logger.error(
                f"[{workflow_id}] ASR API 返回错误状态码 {e.response.status_code}。"
                f"Body: {error_body[:200]}"
            )
            if 400 <= e.response.status_code < 500:
                break
            if attempt < MAX_ASR_RETRIES - 1:
                wait_time = ASR_RETRY_DELAY * (2 ** attempt)
                logger.warning(f"[{workflow_id}] 将在 {wait_time:.1f} 秒后重试...")
                await asyncio.sleep(wait_time)
        except httpx.RequestError as e:
            last_exception = e
            logger.error(f"[{workflow_id}] ASR API 请求时发生网络错误: {e}")
            if attempt < MAX_ASR_RETRIES - 1:
                wait_time = ASR_RETRY_DELAY * (2 ** attempt)
                logger.warning(f"[{workflow_id}] 将在 {wait_time:.1f} 秒后重试...")
                await asyncio.sleep(wait_time)
        except Exception as e:
            last_exception = e
            logger.error(f"[{workflow_id}] ASR 处理中发生未知异常: {e}", exc_info=True)
            break

    raise Exception(f"ASR 请求在 {MAX_ASR_RETRIES} 次尝试后失败。最终错误: {last_exception}")


async def _process_single_audio_file(
        workflow_id: str,
        language: str,
        ignore_timestamps: bool,
        audio_file: UploadFile
) -> ASRResultItem:
    """
    处理单个音频文件的工作单元，设计用于并发执行。
    该函数会捕获所有异常，并总是返回一个 ASRResultItem 对象。

    :param workflow_id: 工作流ID
    :param language: 语言
    :param ignore_timestamps: 是否忽略时间戳
    :param audio_file: 上传的音频文件对象
    :return: 包含处理结果的 ASRResultItem
    """
    async with api_semaphore_asr:
        try:
            log_id = f"{workflow_id}-{audio_file.filename}"
            result_data = await _perform_asr_request(
                workflow_id=log_id,
                language=language,
                ignore_timestamps=ignore_timestamps,
                audio_file=audio_file
            )
            return ASRResultItem(
                filename=audio_file.filename,
                status="success",
                data=result_data,
                error=None
            )
        except Exception as e:
            log_id = f"{workflow_id}-{audio_file.filename}"
            logger.error(f"[{log_id}] 在批量任务中处理失败. Error: {e}", exc_info=False)
            return ASRResultItem(
                filename=audio_file.filename,
                status="failed",
                data=None,
                error=str(e)
            )


# ======================================================================================
# --- API 路由实现 ---
# ======================================================================================

@router_asr.post(
    "/transcribe",
    summary="将单个音频文件转换为文本",
    response_model=StandardResponse
)
async def speech_to_text(
        workflow_id: str = Form(..., description="用于追踪和日志记录的唯一工作流ID。"),
        language: str = Form(..., description="音频的语言代码, 例如 'zh', 'en'。"),
        ignore_timestamps: bool = Form(False, description="是否在结果中忽略时间戳信息。"),
        audio: UploadFile = File(..., description="要进行语音识别的音频文件。")
):
    """
    接收一个音频文件和相关参数，调用 Fish Audio ASR 服务进行语音转文本。
    """
    log_id = f"{workflow_id}-{audio.filename}"
    logger.info(f"[{log_id}] 收到单文件 ASR 请求。")
    start_time = time.monotonic()

    try:
        # 复用我们的工作单元函数，尽管这里只有一个任务
        result_item = await _process_single_audio_file(
            workflow_id=workflow_id,
            language=language,
            ignore_timestamps=ignore_timestamps,
            audio_file=audio
        )

        processing_time = time.monotonic() - start_time

        if result_item.status == "success":
            logger.info(f"[{log_id}] ASR 任务成功完成，耗时: {processing_time:.2f} 秒。")
            return create_standard_response(data=result_item.data, message="语音识别成功完成。")
        else:
            logger.error(f"[{log_id}] ASR 任务处理失败，耗时: {processing_time:.2f} 秒。错误: {result_item.error}")
            # 确定一个合适的错误码，500 表示服务器端处理失败
            return create_standard_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=f"处理语音识别任务时发生错误: {result_item.error}"
            )

    except Exception as e:
        # 这个 catch 块主要用于捕获 _process_single_audio_file 本身可能出现的、未被内部 try-except 捕获的罕见错误
        processing_time = time.monotonic() - start_time
        logger.error(f"[{log_id}] ASR 任务发生严重未知错误，耗时: {processing_time:.2f} 秒。", exc_info=True)
        return create_standard_response(
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=f"处理语音识别任务时发生严重的内部错误: {e}"
        )


@router_asr.post(
    "/transcribe/batch",
    summary="批量将多个音频文件转换为文本",
    response_model=StandardResponse
)
async def speech_to_text_batch(
        workflow_id: str = Form(..., description="用于追踪整个批量任务的唯一工作流ID。"),
        language: str = Form(..., description="所有音频文件的语言代码, 例如 'zh', 'en'。"),
        ignore_timestamps: bool = Form(False, description="是否在所有结果中忽略时间戳信息。"),
        audios: List[UploadFile] = File(..., description="要进行语音识别的音频文件列表。")
):
    """
    接收一个音频文件列表并并发处理，显著提高处理效率。
    """
    logger.info(f"[{workflow_id}] 收到批量 ASR 请求，共 {len(audios)} 个文件。")
    start_time = time.monotonic()

    if not audios:
        return create_standard_response(code=status.HTTP_400_BAD_REQUEST, message="未提供任何音频文件。")

    tasks = [
        _process_single_audio_file(
            workflow_id=workflow_id,
            language=language,
            ignore_timestamps=ignore_timestamps,
            audio_file=audio
        )
        for audio in audios
    ]

    results: List[ASRResultItem] = await asyncio.gather(*tasks)

    success_count = sum(1 for r in results if r.status == "success")
    failed_count = len(audios) - success_count

    response_data = BatchASRResponseData(
        total_files=len(audios),
        success_count=success_count,
        failed_count=failed_count,
        results=results
    )

    processing_time = time.monotonic() - start_time
    summary_message = (
        f"批量处理完成。成功: {success_count}, 失败: {failed_count}。"
        f"总耗时: {processing_time:.2f} 秒。"
    )
    logger.info(f"[{workflow_id}] {summary_message}")

    return create_standard_response(
        data=response_data.model_dump(),
        message=summary_message
    )

# ======================================================================================
# --- 如何集成到主应用 ---
# from fastapi import FastAPI
#
# app = FastAPI(title="My Awesome Multimedia Service")
#
# # 包含你的 TTS 路由
# # from . import main_app_tts
# # app.include_router(main_app_tts.router)
#
# # 包含这个 ASR 路由
# app.include_router(router_asr)
#
# @app.get("/")
# def read_root():
#     return {"message": "Service is running."}
# ======================================================================================
