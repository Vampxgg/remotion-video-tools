# -*- coding: utf-8 -*-
# @File：main_app_refactored_google_tts_v4_simplified_model_id.py
# @Time：2025/08/07 12:30
# @Author：_不咬闰土的猹丶 (Refactored by Senior Software Engineer, Simplified Model ID Input)
# @email：hx1561958968@gmail.com

import io
# --- 导入模块 ---
import re
import os
import shutil
import logging
import concurrent.futures
import time
import sys
import asyncio
from asyncio import Semaphore
import threading
from typing import Dict, List, Any, Optional, Generator
from datetime import datetime

# FastAPI 相关导入
from fastapi import APIRouter, HTTPException, status, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Google Cloud Text-to-Speech SDK 导入
try:
    from google.cloud import texttospeech
    from google.api_core import exceptions as google_exceptions
    from google.protobuf.json_format import MessageToJson
    import json
except ImportError:
    print("CRITICAL: google-cloud-texttospeech library not found. Please run 'pip install google-cloud-texttospeech'")
    sys.exit(1)

# pydub 导入，用于音频处理
import numpy as np
import pyrubberband
from pydub import AudioSegment

# ======================================================================================
# --- 日志配置 ---
# ======================================================================================
try:
    from utils.logger import setup_module_logger
except ImportError:
    def setup_module_logger(logger_name: str, log_file: str) -> logging.Logger:
        logger = logging.getLogger(logger_name)
        if not logger.hasHandlers():
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            print(f"CRITICAL: Using fallback console logger for {logger_name}.")
        return logger
logger = setup_module_logger(__name__, "logs/audio/google_tts_json.log")
# ======================================================================================

router = APIRouter()

# --- 全局资源 ---
tts_thread_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
api_semaphore: Optional[asyncio.Semaphore] = None
google_tts_client: Optional[texttospeech.TextToSpeechClient] = None
client_init_lock = threading.Lock()
dir_creation_lock = threading.Lock()

# --- 配置区 ---
PROXY_URL = ""
AUDIO_FORMAT = "mp3"
API_BASE_URL = "https://server.x-pilot.ai"
PUBLIC_URL_TEMPLATE = f"{API_BASE_URL}/static/meta-doc/video/{{workflow_id}}/audio/{{filename}}"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMP_WORK_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
FINAL_DEST_DIR = "/data/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
MAX_WORKERS = 4
MAX_RETRIES = 3
RETRY_DELAY = 2
TEXT_SPLIT_THRESHOLD = 4500
SENTENCE_SPLIT_PATTERN = r"([。！？，、；…])"
ENABLE_DYNAMIC_SPEED_ADJUSTMENT = True
SPEED_ADJUST_THRESHOLD_RATIO = 1.05
MAX_SPEECH_SPEED = 1.3
ENABLE_DYNAMIC_DECELERATION = True
MIN_SPEECH_SPEED = 0.95
START_PADDING_BUFFER_MS = 150

# --- [变更 1] ---
# 新增一个模型ID模板，用于拼接
MODEL_ID_TEMPLATE = "{language}-Chirp3-HD-{character}"
LANGUAGE_CODE_MAPPING = {
    # 亚洲
    'zh': 'cmn-CN',  # 中文普通话 (中国大陆)
    'ja': 'ja-JP',  # 日语 (日本) - 注意：前端应使用 'ja' 而非 'jp'
    'ko': 'ko-KR',  # 韩语 (韩国)
    'vi': 'vi-VN',  # 越南语 (越南)
    'th': 'th-TH',  # 泰语 (泰国)
    'id': 'id-ID',  # 印度尼西亚语 (印度尼西亚)
    'hi': 'hi-IN',  # 印地语 (印度)
    'bn': 'bn-IN',  # 孟加拉语 (印度)
    'gu': 'gu-IN',  # 古吉拉特语 (印度)
    'kn': 'kn-IN',  # 卡纳达语 (印度)
    'ml': 'ml-IN',  # 马拉雅拉姆语 (印度)
    'mr': 'mr-IN',  # 马拉地语 (印度)
    'ta': 'ta-IN',  # 泰米尔语 (印度)
    'te': 'te-IN',  # 泰卢固语 (印度)
    'ur': 'ur-IN',  # 乌尔都语 (印度)
    'ar': 'ar-XA',  # 阿拉伯语 (通用)
    # 欧洲
    'en': 'en-US',  # 英语 (默认美国，可根据需求改为 en-GB 或 en-AU)
    'de': 'de-DE',  # 德语 (德国)
    'fr': 'fr-FR',  # 法语 (默认法国，可根据需求改为 fr-CA)
    'es': 'es-ES',  # 西班牙语 (默认西班牙，可根据需求改为 es-US)
    'ru': 'ru-RU',  # 俄语 (俄罗斯)
    'it': 'it-IT',  # 意大利语 (意大利)
    'pt': 'pt-BR',  # 葡萄牙语 (巴西)
    'pl': 'pl-PL',  # 波兰语 (波兰)
    'nl': 'nl-NL',  # 荷兰语 (荷兰)
    'da': 'da-DK',  # 丹麦语 (丹麦)
    'fi': 'fi-FI',  # 芬兰语 (芬兰)
    'nb': 'nb-NO',  # 挪威博克马尔语 (挪威)
    'sv': 'sv-SE',  # 瑞典语 (瑞典)
    'tr': 'tr-TR',  # 土耳其语 (土耳其)
    'uk': 'uk-UA',  # 乌克兰语 (乌克兰)

    # 非洲
    'sw': 'sw-KE',  # 斯瓦希里语 (肯尼亚)
}
REVERSE_LANGUAGE_CODE_MAPPING = {v: k for k, v in LANGUAGE_CODE_MAPPING.items()}
REQUIRED_MODEL_SUBSTRING = "Chirp3-HD"  # 定义我们需要的模型子串，方便未来修改

# Google TTS 音频编码映射
GOOGLE_AUDIO_ENCODING = {
    "mp3": texttospeech.AudioEncoding.MP3,
    "wav": texttospeech.AudioEncoding.LINEAR16,
    "ogg": texttospeech.AudioEncoding.OGG_OPUS,
}
if AUDIO_FORMAT not in GOOGLE_AUDIO_ENCODING:
    raise ValueError(f"Unsupported AUDIO_FORMAT for Google TTS: {AUDIO_FORMAT}")


# ======================================================================================
# --- 生命周期事件 (无变化) ---
# ======================================================================================
@router.on_event("startup")
def startup_event():
    global tts_thread_pool, api_semaphore, google_tts_client
    if PROXY_URL:
        logger.warning(f"检测到代理配置: {PROXY_URL}. 请确保 gRPC 流量已正确路由。")
    tts_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                                            thread_name_prefix="Global_GoogleTTS_Worker")
    api_semaphore = Semaphore(50)
    with client_init_lock:
        if google_tts_client is None:
            try:
                logger.info("正在初始化全局 Google TextToSpeechClient...")
                google_tts_client = texttospeech.TextToSpeechClient()
                google_tts_client.list_voices()
                logger.info("全局 Google TTS 客户端成功创建并验证。")
            except Exception as e:
                logger.critical(f"应用启动时创建 Google TTS 客户端失败: {e}", exc_info=True)
                sys.exit(1)
    logger.info(f"全局共享TTS线程池已创建，最大工作线程数: {MAX_WORKERS}")
    logger.info(f"全局信号量已创建，许可数: {50}")


@router.on_event("shutdown")
def shutdown_event():
    global tts_thread_pool, google_tts_client
    if tts_thread_pool:
        logger.info("正在关闭全局共享TTS线程池...")
        tts_thread_pool.shutdown(wait=True)
        logger.info("全局共享TTS线程池已成功关闭。")
    if google_tts_client and hasattr(google_tts_client, 'close'):
        logger.info("正在关闭全局 Google TTS 客户端连接...")
        try:
            google_tts_client.close()
            logger.info("全局 Google TTS 客户端已成功关闭。")
        except Exception as e:
            logger.error(f"关闭 Google TTS 客户端时出错: {e}")


# ======================================================================================
# --- API 响应模型与工具函数 (无变化) ---
# ======================================================================================
class StandardResponse(BaseModel):
    code: int = Field(200, description="业务状态码")
    message: str = Field("Success", description="响应消息")
    data: Optional[Any] = Field(None, description="响应数据")
    timestamp: str = Field(..., description="ISO 8601格式时间戳")


def create_standard_response(data: Optional[Any] = None, code: int = 200, message: str = "Success") -> JSONResponse:
    content = StandardResponse(code=code, message=message, data=data, timestamp=datetime.now().isoformat()).model_dump()
    return JSONResponse(status_code=code, content=content)


def extract_subtitles_from_json(data: Any) -> Generator[Dict[str, Any], None, None]:
    if isinstance(data, dict):
        if all(k in data for k in ['text', 'start_time_seconds', 'end_time_seconds']):
            yield data
            return
        for value in data.values():
            yield from extract_subtitles_from_json(value)
    elif isinstance(data, list):
        for item in data:
            yield from extract_subtitles_from_json(item)


# ======================================================================================
# --- 核心业务逻辑函数 (无变化，函数签名和内部逻辑保持稳定) ---
# ======================================================================================
def _process_and_finalize_audio(audio: AudioSegment, task_info: Dict[str, Any]) -> AudioSegment:
    # ... (此函数代码无变化, 此处省略)
    task_id = task_info['id']
    target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
    if target_duration_sec <= 0: return audio
    processed_audio, target_duration_ms, actual_duration_ms = audio, int(target_duration_sec * 1000), len(audio)
    if ENABLE_DYNAMIC_SPEED_ADJUSTMENT and actual_duration_ms > target_duration_ms * SPEED_ADJUST_THRESHOLD_RATIO:
        ratio = actual_duration_ms / target_duration_ms
        logger.warning(f"[Task {task_id}] 音频过长，使用pyrubberband加速: {ratio:.2f}x.")
        samples = np.array(processed_audio.get_array_of_samples())
        stretched_samples = pyrubberband.time_stretch(samples, processed_audio.frame_rate, ratio)
        processed_audio = AudioSegment(stretched_samples.tobytes(), frame_rate=processed_audio.frame_rate,
                                       sample_width=processed_audio.sample_width, channels=processed_audio.channels)
    elif ENABLE_DYNAMIC_DECELERATION and actual_duration_ms < target_duration_ms:
        calculated_ratio = actual_duration_ms / target_duration_ms
        if calculated_ratio >= MIN_SPEECH_SPEED:
            logger.info(f"[Task {task_id}] 音频偏短，进行高质量减速，比率:{calculated_ratio:.2f}x")
            samples = np.array(processed_audio.get_array_of_samples())
            stretched_samples = pyrubberband.time_stretch(samples, processed_audio.frame_rate, calculated_ratio)
            processed_audio = AudioSegment(stretched_samples.tobytes(), frame_rate=processed_audio.frame_rate,
                                           sample_width=processed_audio.sample_width, channels=processed_audio.channels)
    final_duration_ms = len(processed_audio)
    duration_diff_ms = target_duration_ms - final_duration_ms
    if duration_diff_ms > 0:
        start_padding_ms = min(duration_diff_ms, START_PADDING_BUFFER_MS)
        return AudioSegment.silent(duration=start_padding_ms) + processed_audio + AudioSegment.silent(
            duration=(duration_diff_ms - start_padding_ms))
    elif duration_diff_ms < 0:
        logger.warning(f"[Task {task_id}] 内容溢出，从尾部裁剪 {abs(duration_diff_ms)}ms。")
        return processed_audio[:target_duration_ms]
    return processed_audio


def _split_text_into_chunks(text: str, max_len: int) -> List[str]:
    # ... (此函数代码无变化, 此处省略)
    if len(text) <= max_len: return [text]
    parts, sentences = re.split(SENTENCE_SPLIT_PATTERN, text), []
    for i in range(0, len(parts) - 1, 2): sentences.append(
        parts[i] + (parts[i + 1] if i + 1 < len(parts) and parts[i + 1] else ''))
    if len(parts) % 2 == 1 and parts[-1]: sentences.append(parts[-1])
    chunks, current_chunk = [], ""
    for sentence in sentences:
        if not sentence.strip(): continue
        if len(current_chunk) + len(sentence) <= max_len:
            current_chunk += sentence
        else:
            if current_chunk: chunks.append(current_chunk)
            current_chunk = sentence if len(sentence) <= max_len else ""
            if len(sentence) > max_len: chunks.append(sentence)
    if current_chunk: chunks.append(current_chunk)
    return chunks if chunks else [text]


def _move_workflow_directory(workflow_id: str):
    # ... (此函数代码无变化, 此处省略)
    source_dir, dest_path = TEMP_WORK_DIR_TEMPLATE.format(workflow_id=workflow_id), os.path.join(FINAL_DEST_DIR,
                                                                                                 workflow_id)
    if not os.path.isdir(source_dir): logger.warning(f"源目录 '{source_dir}' 不存在，跳过移动操作。"); return
    try:
        os.makedirs(FINAL_DEST_DIR, exist_ok=True)
        if os.path.exists(dest_path): shutil.rmtree(dest_path)
        shutil.move(source_dir, FINAL_DEST_DIR)
        logger.info(f"成功移动文件夹从 '{source_dir}' 到 '{dest_path}'")
    except Exception as e:
        logger.error(f"移动文件夹时发生严重错误: {e}", exc_info=True); raise IOError(f"移动文件夹时发生错误: {str(e)}")


def generate_audio_single_task(client: texttospeech.TextToSpeechClient, task_info: Dict[str, Any], model_id: str,
                               language_code: str) -> None:
    # ... (此函数代码无变化，它接收的是已经拼接好的完整model_id, 此处省略)
    task_id, subtitle_obj, subtitle_text, full_audio_path, public_url = task_info["id"], task_info[
        "original_subtitle_obj"], task_info["original_subtitle_obj"].get("text", ""), task_info["local_path"], \
    task_info["public_url"]
    audio_save_path = os.path.dirname(full_audio_path)
    try:
        with dir_creation_lock:
            os.makedirs(audio_save_path, exist_ok=True)
    except Exception as e:
        error_msg = f"创建目录失败: {e}"; logger.error(f"[Task {task_id}] {error_msg}"); subtitle_obj.update(
            {"error": error_msg, "audio_path": None}); return
    target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
    if not str(subtitle_text).strip():
        try:
            if target_duration_sec > 0:
                AudioSegment.silent(duration=int(target_duration_sec * 1000)).export(full_audio_path,
                                                                                     format=AUDIO_FORMAT)
            else:
                open(full_audio_path, 'a').close()
            subtitle_obj["audio_path"] = public_url;
            logger.info(f"[Task {task_id}] 静音音频生成成功。");
            return
        except Exception as e:
            error_msg = f"生成静音文件时失败: {e}"; logger.error(f"[Task {task_id}] {error_msg}",
                                                                 exc_info=True); subtitle_obj.update(
                {"error": error_msg, "audio_path": None}); return
    try:
        for attempt in range(MAX_RETRIES):
            try:
                full_text = " ".join(_split_text_into_chunks(subtitle_text, TEXT_SPLIT_THRESHOLD))
                synthesis_input = texttospeech.SynthesisInput(text=full_text)
                voice = texttospeech.VoiceSelectionParams(language_code=language_code, name=model_id)
                audio_config = texttospeech.AudioConfig(audio_encoding=GOOGLE_AUDIO_ENCODING[AUDIO_FORMAT])
                response = client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
                if not response.audio_content: raise ValueError("Google TTS API 返回了空的音频内容。")
                with io.BytesIO(response.audio_content) as buffer:
                    generated_audio = AudioSegment.from_file(buffer, format=AUDIO_FORMAT)
                final_audio = _process_and_finalize_audio(generated_audio, task_info)
                final_audio.export(full_audio_path, format=AUDIO_FORMAT)
                subtitle_obj["audio_path"] = public_url;
                logger.info(f"[Task {task_id}] Google TTS 音频生成成功。");
                return
            except google_exceptions.GoogleAPICallError as e:
                is_retryable = e.is_retryable() if hasattr(e, 'is_retryable') else (e.code() in [429, 500, 503])
                if attempt < MAX_RETRIES - 1 and is_retryable:
                    time.sleep(RETRY_DELAY * (2 ** attempt))
                else:
                    error_msg = f"Google TTS API Error (Code: {e.code()}): {e.message}"; logger.error(
                        f"[Task {task_id}] API调用最终失败: {error_msg}", exc_info=False); subtitle_obj.update(
                        {"error": error_msg, "audio_path": None}); return
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (2 ** attempt))
                else:
                    error_msg = f"TTS generation failed after {MAX_RETRIES} retries: {str(e)}"; logger.error(
                        f"[Task {task_id}] 所有重试均失败: {error_msg}", exc_info=True); subtitle_obj.update(
                        {"error": error_msg, "audio_path": None}); return
    except Exception as e:
        error_msg = f"任务执行期间发生未处理的异常: {e}"; logger.error(f"[Task {task_id}] {error_msg}",
                                                                       exc_info=True); subtitle_obj.update(
            {"error": error_msg, "audio_path": None})


async def _process_workflow(workflow_id: str, raw_script: Any, full_model_id: str, language: str) -> (
Any, Optional[str]):
    # ... (函数签名接收 full_model_id, 此处省略内部代码)
    if google_tts_client is None: raise HTTPException(status_code=503, detail="服务暂时不可用：TTS 客户端未初始化。")
    try:
        subtitle_objects = list(extract_subtitles_from_json(raw_script))
        if not subtitle_objects: raise ValueError("在提供的JSON结构中未能找到任何有效的字幕对象。")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    tasks_to_process, audio_save_path = [], os.path.join(TEMP_WORK_DIR_TEMPLATE.format(workflow_id=workflow_id),
                                                         "audio")
    for subtitle_obj in subtitle_objects:
        try:
            subtitle_id, start_sec, end_sec = subtitle_obj['id'], float(subtitle_obj["start_time_seconds"]), float(
                subtitle_obj["end_time_seconds"])
            if end_sec <= start_sec: subtitle_obj.update(
                {'error': 'Invalid time range (end <= start)', 'audio_path': None}); continue
        except (KeyError, TypeError, ValueError) as e:
            subtitle_obj.update({'error': f'解析字幕对象时出错: {e}', 'audio_path': None}); continue
        safe_subtitle_id = re.sub(r'[\\/*?:"<>|]', "_", str(subtitle_id))
        audio_filename = f"audio_{safe_subtitle_id}.{AUDIO_FORMAT}"
        tasks_to_process.append(
            {"id": subtitle_id, "start_sec": start_sec, "end_sec": end_sec, "original_subtitle_obj": subtitle_obj,
             "local_path": os.path.join(audio_save_path, audio_filename),
             "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)})
    logger.info(f"[{workflow_id}] JSON解析完成，共 {len(tasks_to_process)} 个有效任务待处理。提交到线程池。")
    try:
        loop = asyncio.get_running_loop()
        await asyncio.gather(*(
        loop.run_in_executor(tts_thread_pool, generate_audio_single_task, google_tts_client, task, full_model_id,
                             language) for task in tasks_to_process))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"并行处理任务时发生主错误: {e}")
    move_error = None
    if any("audio_path" in s and s["audio_path"] is not None for s in subtitle_objects):
        try:
            await asyncio.get_running_loop().run_in_executor(None, _move_workflow_directory, workflow_id)
        except (FileNotFoundError, IOError) as e:
            move_error = str(e)
    else:
        logger.warning(f"[{workflow_id}] 没有任何音频生成成功，跳过文件移动操作。")
    return raw_script, move_error


# ======================================================================================
# --- API 端点 (RESTful 风格) ---
# ======================================================================================

# --- [变更 2] ---
# Pydantic 输入模型更新：model_id
class TTSRequestPayload(BaseModel):
    raw_script: Any = Field(..., description="包含字幕信息的原始JSON结构。")
    language: str = Field(..., description="目标语言的BCP-47代码，例如 'en-US'。")
    model_id: str = Field(..., description="语音角色名称，例如 'Charon'。系统将自动拼接成完整的模型ID。")
    workflow_id: str = Field(..., description="本次任务的唯一工作流ID。")


class RegenerateSinglePayload(BaseModel):
    subtitle_data: Dict[str, Any] = Field(..., description="要更新的单个字幕对象的完整数据。")
    language: str = Field(..., description="目标语言的BCP-47代码，例如 'en-US'。")
    model_id: str = Field(..., description="语音角色名称，例如 'Charon'。系统将自动拼接成完整的模型ID。")


# --- API 路由实现 ---
@router.post("/generate_audio_google_json", summary="通过脚本JSON创建并生成全套音频")
async def create_and_generate_workflow(payload: TTSRequestPayload):
    async with api_semaphore:
        # --- [变更 3] ---
        # 在此处拼接完整的 model_id
        # 1. 语言代码转换与校验
        short_lang_code = payload.language.lower()  # 转换为小写以支持大小写不敏感
        resolved_language_code = LANGUAGE_CODE_MAPPING.get(short_lang_code)
        if not resolved_language_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported language code '{payload.language}'. Supported codes are: {list(LANGUAGE_CODE_MAPPING.keys())}"
            )
        try:
            full_model_id = MODEL_ID_TEMPLATE.format(language=resolved_language_code, character=payload.model_id)
        except KeyError:
            raise HTTPException(status_code=400, detail="无法构建有效的模型ID，请检查模板和输入。")

        updated_script, move_error = await _process_workflow(
            workflow_id=payload.workflow_id,
            raw_script=payload.raw_script,
            full_model_id=full_model_id,  # 向下传递拼接好的ID
            language=resolved_language_code
        )
        all_subtitles = list(extract_subtitles_from_json(updated_script))
        total_tasks, failed_tasks = len(all_subtitles), sum(1 for sub in all_subtitles if sub.get("error"))

        if failed_tasks == 0 and not move_error:
            return create_standard_response(data=updated_script, code=status.HTTP_200_OK,
                                            message="All audio generated successfully.")
        elif failed_tasks == total_tasks or move_error:
            message = f"Workflow processing failed. Move error: {move_error}." if move_error else "All audio generation tasks failed."
            return create_standard_response(data=updated_script, code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                            message=message)
        else:
            return create_standard_response(data=updated_script, code=status.HTTP_207_MULTI_STATUS,
                                            message=f"Workflow processing completed with partial success. {total_tasks - failed_tasks}/{total_tasks} tasks succeeded.")


@router.put("/audio_google/{workflow_id}", summary="重新生成整个工作流的所有音频文件")
async def regenerate_workflow_audio(workflow_id: str, payload: TTSRequestPayload):
    async with api_semaphore:
        logger.info(f"收到工作流 '{workflow_id}' 的批量重新生成请求 (TTS: Google Cloud)。")
        if workflow_id != payload.workflow_id:
            return create_standard_response(code=status.HTTP_400_BAD_REQUEST,
                                            message="URL与请求体中的workflow_id不匹配。")

        # 1. 语言代码转换与校验
        short_lang_code = payload.language.lower()  # 转换为小写以支持大小写不敏感
        resolved_language_code = LANGUAGE_CODE_MAPPING.get(short_lang_code)
        if not resolved_language_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported language code '{payload.language}'. Supported codes are: {list(LANGUAGE_CODE_MAPPING.keys())}"
            )
        # --- [变更 3] ---
        try:
            full_model_id = MODEL_ID_TEMPLATE.format(language=resolved_language_code, character=payload.model_id)
        except KeyError:
            raise HTTPException(status_code=400, detail="无法构建有效的模型ID，请检查模板和输入。")

        updated_script, move_error = await _process_workflow(
            workflow_id=workflow_id,
            raw_script=payload.raw_script,
            full_model_id=full_model_id,  # 向下传递拼接好的ID
            language=resolved_language_code
        )
        all_subtitles = list(extract_subtitles_from_json(updated_script))
        total_tasks, failed_tasks = len(all_subtitles), sum(1 for sub in all_subtitles if sub.get("error"))

        if failed_tasks == 0 and not move_error:
            return create_standard_response(data=updated_script, code=status.HTTP_200_OK,
                                            message="All audio successfully regenerated.")
        elif failed_tasks == total_tasks or move_error:
            message = f"Workflow regeneration failed. Move error: {move_error}." if move_error else "All audio regeneration tasks failed."
            return create_standard_response(data=updated_script, code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                            message=message)
        else:
            return create_standard_response(data=updated_script, code=status.HTTP_207_MULTI_STATUS,
                                            message=f"Workflow regeneration completed with partial success. {total_tasks - failed_tasks}/{total_tasks} tasks succeeded.")


@router.put("/audio_google/{workflow_id}/{subtitle_id}", summary="重新生成并替换单个音频文件")
async def regenerate_single_audio(workflow_id: str, subtitle_id: str, payload: RegenerateSinglePayload):
    async with api_semaphore:
        logger.info(
            f"收到为 workflow '{workflow_id}' 下的字幕ID '{subtitle_id}' 的单个重新生成请求 (TTS: Google Cloud)。")

        if google_tts_client is None:
            return create_standard_response(code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                            message="服务暂时不可用：TTS客户端未初始化。")
        # 1. 语言代码转换与校验
        short_lang_code = payload.language.lower()  # 转换为小写以支持大小写不敏感
        resolved_language_code = LANGUAGE_CODE_MAPPING.get(short_lang_code)
        if not resolved_language_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported language code '{payload.language}'. Supported codes are: {list(LANGUAGE_CODE_MAPPING.keys())}"
            )
        # --- [变更 3] ---
        try:
            full_model_id = MODEL_ID_TEMPLATE.format(language=resolved_language_code, character=payload.model_id)
        except KeyError:
            raise HTTPException(status_code=400, detail="无法构建有效的模型ID，请检查模板和输入。")

        subtitle_obj, language = payload.subtitle_data, resolved_language_code

        if str(subtitle_obj.get('id')) != subtitle_id:
            return create_standard_response(code=status.HTTP_400_BAD_REQUEST,
                                            message="URL与请求体中的subtitle_id不匹配。")

        safe_subtitle_id = re.sub(r'[\\/*?:"<>|]', "_", str(subtitle_id))
        audio_filename = f"audio_{safe_subtitle_id}.{AUDIO_FORMAT}"
        dest_dir = os.path.join(FINAL_DEST_DIR, workflow_id, "audio")
        final_audio_path = os.path.join(dest_dir, audio_filename)
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception as e:
            return create_standard_response(code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                            message=f"为 '{workflow_id}/{subtitle_id}' 创建目标目录失败: {e}")

        task_info = {
            "id": subtitle_id, "start_sec": subtitle_obj["start_time_seconds"],
            "end_sec": subtitle_obj["end_time_seconds"],
            "original_subtitle_obj": subtitle_obj, "local_path": final_audio_path,
            "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
        }

        try:
            await asyncio.get_running_loop().run_in_executor(
                tts_thread_pool, generate_audio_single_task, google_tts_client, task_info, full_model_id, language
            )
        except Exception as e:
            return create_standard_response(code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                            message=f"为 '{workflow_id}/{subtitle_id}' 执行单个生成任务时发生未知错误: {e}")

        if subtitle_obj.get("error"):
            return create_standard_response(data=subtitle_obj, code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                            message=f"Audio generation failed: {subtitle_obj['error']}")

        return create_standard_response(data=subtitle_obj,
                                        message=f"Audio for subtitle '{subtitle_id}' regenerated successfully.")


# ======================================================================================
# --- 新增API端点：查询可用语音模型 ---
# ======================================================================================
@router.get("/voices", summary="获取所有支持的Google TTS语音模型列表 (Chirp3-HD)")
async def get_available_voices(
        language: Optional[str] = Query(
            None,
            description="可选的简化语言代码 (例如 'en', 'zh') 用于筛选结果。如果未提供，则返回所有支持语言的语音。",
            examples=["en", "ja"]
        )
):
    """
    获取 Google TTS 中所有 'Chirp3-HD' 系列的可用语音模型。

    - **按语言过滤**: 可通过 `language` 查询参数筛选特定语言。
    - **模型过滤**: 内部已硬编码，只返回名字包含 'Chirp3-HD' 的模型。
    - **响应格式**: 返回的 'name' 字段是简化后的角色名，顶级键是简化的语言代码。
    """
    logger.info(f"收到获取可用语音列表的请求。筛选语言: {language or '无'}")
    if google_tts_client is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="服务暂时不可用：TTS客户端尚未初始化。")

    # 1. 如果提供了语言参数，进行校验和转换
    bcp47_language_code = None
    if language:
        short_lang_code = language.lower()
        bcp47_language_code = LANGUAGE_CODE_MAPPING.get(short_lang_code)
        if not bcp47_language_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported language code '{language}'. Supported codes are: {list(LANGUAGE_CODE_MAPPING.keys())}"
            )

    try:
        # 2. 调用 Google API，如果需要，传入 language_code 进行预筛选
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: google_tts_client.list_voices(language_code=bcp47_language_code)
        )

        # 3. 对返回结果进行深度处理、过滤和格式化
        grouped_voices: Dict[str, List[Dict[str, Any]]] = {}
        for voice in response.voices:
            # 3.1 按模型系列过滤
            if REQUIRED_MODEL_SUBSTRING not in voice.name:
                continue

            # 3.2 简化语音名称为角色名
            # 假设格式为 {language-code}-{series}-{character}
            try:
                character_name = voice.name.split('-')[-1]
            except IndexError:
                logger.warning(f"无法从 '{voice.name}' 中解析角色名，跳过此语音。")
                continue

            # 准备要返回的语音详情对象
            ssml_gender_str = texttospeech.SsmlVoiceGender(voice.ssml_gender).name
            voice_details = {
                "name": character_name,
                "gender": ssml_gender_str,
                "natural_sample_rate_hertz": voice.natural_sample_rate_hertz
            }

            # 3.3 按简化的语言代码对结果进行分组
            for bcp_code in voice.language_codes:
                short_code = REVERSE_LANGUAGE_CODE_MAPPING.get(bcp_code)
                if not short_code:
                    continue  # 如果Google返回的语言我们不支持，则忽略

                if short_code not in grouped_voices:
                    grouped_voices[short_code] = []

                # 避免重复添加同一个角色到同一个语言下
                if not any(v['name'] == character_name for v in grouped_voices[short_code]):
                    grouped_voices[short_code].append(voice_details)

        message = "Successfully retrieved available voices."
        if language and not grouped_voices:
            message = f"No '{REQUIRED_MODEL_SUBSTRING}' voices found for language '{language}'."

        return create_standard_response(
            data=grouped_voices,
            message=message
        )

    except google_exceptions.GoogleAPICallError as e:
        error_msg = f"调用 Google TTS API 获取语音列表失败 (Code: {e.code()}): {e.message}"
        logger.error(error_msg, exc_info=False)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=error_msg)
    except Exception as e:
        logger.error(f"获取语音列表时发生未知错误: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"An unexpected error occurred: {str(e)}")


# ... 紧随 /voices 接口之后 ...

@router.get("/voices/raw", summary="获取原始的Google TTS语音模型列表 (未经处理)", include_in_schema=True)
async def get_raw_available_voices():
    """
    获取未经任何处理的、来自 Google Cloud Text-to-Speech API 的原始语音模型列表响应。
    此接口主要用于调试和查看完整的可用字段，返回的数据结构直接映射自 Google 的 Protobuf 响应。
    """
    logger.info("收到获取原始语音列表的请求。")
    if google_tts_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务暂时不可用：TTS客户端尚未初始化。"
        )

    try:
        loop = asyncio.get_running_loop()
        # 异步执行 Google SDK 的同步调用
        list_voices_response = await loop.run_in_executor(
            None,
            google_tts_client.list_voices
        )

        # 核心步骤：将 Protobuf 响应对象转换为 JSON 字符串
        # MessageToJson 会处理所有字段，包括枚举、嵌套结构等
        response_json_string = MessageToJson(list_voices_response._pb)

        # 将 JSON 字符串解析回 Python 字典，以便 FastAPI 正确处理
        raw_response_dict = json.loads(response_json_string)

        logger.info("成功获取原始语音列表并将其序列化为JSON。")

        # 使用我们的标准响应包装器返回原始数据字典
        return create_standard_response(
            data=raw_response_dict,
            message="Successfully retrieved raw available voices list."
        )

    except google_exceptions.GoogleAPICallError as e:
        error_msg = f"调用 Google TTS API 获取语音列表失败 (Code: {e.code()}): {e.message}"
        logger.error(error_msg, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_msg
        )
    except Exception as e:
        logger.error(f"获取原始语音列表时发生未知错误: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred while fetching raw voices list: {str(e)}"
        )


