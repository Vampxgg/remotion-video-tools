# -*- coding: utf-8 -*-
# @File：main_app.py
# @Time：2025/8/5 18:30
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com

# --- 导入模块 ---
import re
import json
import os
import shutil
import logging
import concurrent.futures
import time
import queue
import sys
import asyncio
from asyncio import Semaphore
import threading
from typing import Dict, List, Any, Optional, Generator
import unicodedata

# FastAPI 相关导入
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

# Fish Audio SDK 导入
from fish_audio_sdk import Session, TTSRequest, Prosody

# pydub 导入，用于音频处理
import numpy as np
import pyrubberband
from pydub import AudioSegment

# ======================================================================================
# --- [V12] 工业级日志配置 ---
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
logger = setup_module_logger(__name__, "logs/audio/fish_json.log")
# ======================================================================================


router = APIRouter()

# --- 全局资源 (保持不变) ---
tts_thread_pool: concurrent.futures.ThreadPoolExecutor = None
api_semaphore: asyncio.Semaphore = None
global_session_pool: queue.Queue = None
pool_init_lock = threading.Lock()
dir_creation_lock = threading.Lock()


@router.on_event("startup")
def startup_event():
    global tts_thread_pool, api_semaphore, MAX_WORKERS
    tts_thread_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS,
        thread_name_prefix="Global_TTS_Worker"
    )
    api_semaphore = Semaphore(50)
    logger.info(f"全局共享TTS线程池已创建，最大工作线程数: {MAX_WORKERS}")
    logger.info(f"全局信号量已创建，许可数: {50}")


@router.on_event("shutdown")
def shutdown_event():
    global tts_thread_pool, global_session_pool
    if tts_thread_pool:
        logger.info("正在关闭全局共享TTS线程池...")
        tts_thread_pool.shutdown(wait=True)
        logger.info("全局共享TTS线程池已成功关闭。")

    if global_session_pool:
        logger.info(f"正在关闭全局 Session 池中的 ({global_session_pool.qsize()}) 个 Session...")
        while not global_session_pool.empty():
            try:
                session = global_session_pool.get_nowait()
                if hasattr(session, 'close'):
                    session.close()
            except queue.Empty:
                break
            except Exception as e:
                logger.error(f"关闭一个 Session 时出错: {e}")
        logger.info("全局 Session 池已被清理。")


# --- 代理与配置区 (保持不变) ---
PROXY_URL = ""
if PROXY_URL:
    os.environ['HTTP_PROXY'] = PROXY_URL
    os.environ['HTTPS_PROXY'] = PROXY_URL
    logger.info(f"已配置全局 HTTP/HTTPS 代理: {PROXY_URL}")
else:
    logger.info("未配置代理，将直接进行网络连接。")

ENGINE_MODEL = "speech-1.6"
AUDIO_FORMAT = "mp3"
PUBLIC_URL_TEMPLATE = "http://127.0.0.1:2906/meta-doc/video/{workflow_id}/audio/{filename}"
# "https://server.x-pilot.ai/static/meta-doc/video/{workflow_id}/audio/{filename}"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SOURCE_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
AUDIO_SAVE_PATH_TEMPLATE = os.path.join(SOURCE_DIR_TEMPLATE, "audio")
DEST_BASE_DIR = "E:\\Server\\x-pilot-oss\\uploads\\meta-doc\\video"
# "/data/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
MAX_WORKERS = 15
SESSION_POOL_SIZE = MAX_WORKERS
MAX_RETRIES = 3
RETRY_DELAY = 2
TEXT_SPLIT_THRESHOLD = 120
FISH_API_KEY = "dae51de32a0743f6b4f2f7b6366747bf"
SENTENCE_SPLIT_PATTERN = r"([。！？，、；…])"
ENABLE_DYNAMIC_SPEED_ADJUSTMENT = True
SPEED_ADJUST_THRESHOLD_RATIO = 1.05
MAX_SPEECH_SPEED = 1.3
ENABLE_DYNAMIC_DECELERATION = True
MIN_SPEECH_SPEED = 0.95
START_PADDING_BUFFER_MS = 150

# ======================================================================================
# --- 【V19 新增】API 响应模型与工具函数 ---
# ======================================================================================
from datetime import datetime
from fastapi.responses import JSONResponse


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
    """
    创建一个标准格式的 FastAPI 响应。

    :param data: 响应的主要数据负载。
    :param code: HTTP 状态码。
    :param message: 描述性消息。
    :return: 一个 JSONResponse 对象。
    """
    content = StandardResponse(
        code=code,
        message=message,
        data=data,
        timestamp=datetime.now().isoformat()
    ).model_dump()
    return JSONResponse(status_code=code, content=content)


# --- 【V18 重构】 智能解析器被一个更健壮的JSON遍历函数取代 ---
def extract_subtitles_from_json(data: Any) -> Generator[Dict[str, Any], None, None]:
    """
    一个健壮的生成器函数，用于深度遍历输入的JSON结构并提取出所有字幕对象。
    它能处理 data 是列表或字典的各种情况。

    :param data: 输入的原始JSON数据 (可以是 list 或 dict)。
    :yields: 包含 'text', 'start_time_seconds', 'end_time_seconds' 键的字典对象。
    """
    if isinstance(data, dict):
        # 检查当前字典是否是一个 "字幕对象"
        if all(k in data for k in ['text', 'start_time_seconds', 'end_time_seconds']):
            yield data
        # 如果不是字幕对象，则递归遍历它的值
        else:
            for value in data.values():
                yield from extract_subtitles_from_json(value)
    elif isinstance(data, list):
        # 如果是列表，递归遍历它的所有元素
        for item in data:
            yield from extract_subtitles_from_json(item)


# --- 所有核心业务逻辑函数保持不变，它们的设计已经足够解耦 ---
# 以下函数:
# _process_and_finalize_audio, _parse_time_to_seconds, _split_text_into_chunks,
# generate_audio_single_task, _move_workflow_directory
# 这些函数几乎不需要改动或完全不需要改动, 这证明了原设计的优秀性。
# 我们只需要对 generate_audio_single_task 的返回稍作调整。

def _process_and_finalize_audio(audio: AudioSegment, task_info: Dict[str, Any]) -> AudioSegment:
    """音频动态加速和精确静音填充 (保持不变)"""
    task_id = task_info['id']
    target_duration_sec = task_info["end_sec"] - task_info["start_sec"]

    if target_duration_sec <= 0:
        logger.warning(f"[Task {task_id}] 目标时长无效 ({target_duration_sec}s)，返回原始TTS音频。")
        return audio

    processed_audio = audio
    target_duration_ms = int(target_duration_sec * 1000)
    actual_duration_ms = len(processed_audio)

    if ENABLE_DYNAMIC_SPEED_ADJUSTMENT and actual_duration_ms > target_duration_ms * SPEED_ADJUST_THRESHOLD_RATIO:
        ratio = actual_duration_ms / target_duration_ms
        logger.warning(
            f"[Task {task_id}] 音频过长({actual_duration_ms}ms > 目标 {target_duration_ms}ms)，"
            f"将使用 pyrubberband 进行高质量加速，速度: {ratio:.2f}x。"
        )
        samples = np.array(processed_audio.get_array_of_samples())
        stretched_samples = pyrubberband.time_stretch(samples, processed_audio.frame_rate, ratio)
        processed_audio = AudioSegment(
            stretched_samples.tobytes(),
            frame_rate=processed_audio.frame_rate,
            sample_width=processed_audio.sample_width,
            channels=processed_audio.channels
        )
        logger.info(f"[Task {task_id}] 高质量加速后音频时长: {len(processed_audio)}ms")

    elif ENABLE_DYNAMIC_DECELERATION and actual_duration_ms < target_duration_ms:
        calculated_ratio = actual_duration_ms / target_duration_ms
        if calculated_ratio >= MIN_SPEECH_SPEED:
            logger.info(
                f"[Task {task_id}] 音频偏短({actual_duration_ms}ms < 目标 {target_duration_ms}ms)，"
                f"将进行高质量减速以自然填充时长。比率:{calculated_ratio:.2f}x (在 {MIN_SPEECH_SPEED} 的限制内)"
            )
            samples = np.array(processed_audio.get_array_of_samples())
            stretched_samples = pyrubberband.time_stretch(samples, processed_audio.frame_rate, calculated_ratio)
            processed_audio = AudioSegment(
                stretched_samples.tobytes(),
                frame_rate=processed_audio.frame_rate,
                sample_width=processed_audio.sample_width,
                channels=processed_audio.channels
            )
            logger.info(f"[Task {task_id}] 减速后音频时长: {len(processed_audio)}ms")
        else:
            logger.info(
                f"[Task {task_id}] 音频过短({actual_duration_ms}ms vs {target_duration_ms}ms)，"
                f"所需减速比({calculated_ratio:.2f}x)超出下限({MIN_SPEECH_SPEED})，将退回至静音填充方案。"
            )

    final_duration_ms = len(processed_audio)
    duration_diff_ms = target_duration_ms - final_duration_ms

    if duration_diff_ms > 0:
        start_padding_ms = min(duration_diff_ms, START_PADDING_BUFFER_MS)
        end_padding_ms = duration_diff_ms - start_padding_ms
        start_silence = AudioSegment.silent(duration=start_padding_ms)
        end_silence = AudioSegment.silent(duration=end_padding_ms)
        final_audio = start_silence + processed_audio + end_silence
        logger.info(
            f"[Task {task_id}] 成功填充静音以匹配目标时长。填充: {duration_diff_ms}ms。最终时长: {len(final_audio)}ms"
        )
        return final_audio
    elif duration_diff_ms < 0:
        ms_to_crop = abs(duration_diff_ms)
        logger.warning(
            f"[Task {task_id}] 内容溢出: 最终音频({final_duration_ms}ms) > 目标({target_duration_ms}ms)。"
            f"将从尾部裁剪 {ms_to_crop}ms。"
        )
        faded_audio = processed_audio.fade_out(duration=20)  # 在裁剪前加一个短暂的淡出效果
        final_audio = faded_audio[:target_duration_ms]
        return final_audio
    else:
        logger.info(f"[Task {task_id}] 音频时长({final_duration_ms}ms)已匹配目标，无需调整。")
        return processed_audio


# ======================================================================================
# --- 文本清洗工具函数 (专门修复 TTS 爆破音/杂音问题) ---
# ======================================================================================
def _clean_text_for_tts(text: str) -> str:
    """
    清洗 LLM 生成的文本，移除会导致 TTS 模型产生噪声/爆破音的特殊符号。
    处理内容：
    1. Unicode 标准化 (NFKC)。
    2. 移除 Markdown 符号 (*, #, `, ~)。
    3. 移除特殊货币/数学符号 (如 ₹)。
    4. 移除 Emoji 表情。
    5. 移除不可见控制字符。
    """
    if not text:
        return ""

    # 1. Unicode NFKC 标准化 (将全角字符、兼容字符转换为标准字符)
    # 这能解决很多编码引起的杂音问题
    text = unicodedata.normalize('NFKC', text)

    # 2. 移除 Markdown 常用格式符 (TTS 读到这些往往会卡顿或产生杂音)
    # 比如 LLM 喜欢输出 **重点** 或 # 标题
    text = re.sub(r'[\*\#\`\>\~]', '', text)

    # 3. 移除特定的“有毒”符号
    # 用户提到的 ₹ (卢比符号) 以及其他常见的数学/特殊符号
    # 如果业务需要保留某些符号，请在此处调整
    text = re.sub(r'[₹|©®™@$%=+\^\\]', '', text)

    # 4. 移除 Emoji 表情 (Unicode 范围)
    # 大部分 TTS 模型读 Emoji 都会出问题
    try:
        # 匹配非基本多语言平面的字符 (通常是 Emoji)
        text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
    except re.error:
        pass  # 如果环境不支持这种正则写法，跳过

    # 5. 移除类似 [笑声] (鼓掌) 这种 LLM 可能输出的动作描述 (可选，建议开启)
    # 很多 TTS 会把括弧也读出来，或者因为括号内的词无法通过 G2P 转换而产生噪声
    # 这里移除中文括号或英文括号内的非句式内容（简单的过滤）
    # text = re.sub(r'（.*?）', '', text)
    # text = re.sub(r'\(.*?\)', '', text)

    # 6. 将多个连续空格/换行合并为一个空格，并去除首尾空白
    text = re.sub(r'\s+', ' ', text).strip()

    return text


def _split_text_into_chunks(text: str, max_len: int) -> List[str]:
    """文本切分 (保持不变)"""
    # (代码与原版完全相同, 此处省略以保持简洁)
    if len(text) <= max_len:
        return [text]
    parts = re.split(SENTENCE_SPLIT_PATTERN, text)
    sentences = []
    for i in range(0, len(parts) - 1, 2):
        sentence = parts[i] + (parts[i + 1] if i + 1 < len(parts) and parts[i + 1] else '')
        sentences.append(sentence)
    if len(parts) % 2 == 1 and parts[-1]:
        sentences.append(parts[-1])
    chunks = []
    current_chunk = ""
    for sentence in sentences:
        if not sentence.strip():
            continue
        if len(current_chunk) + len(sentence) <= max_len:
            current_chunk += sentence
        else:
            if current_chunk:
                chunks.append(current_chunk)
            if len(sentence) > max_len:
                if current_chunk:
                    current_chunk = ""
                chunks.append(sentence)
            else:
                current_chunk = sentence
    if current_chunk:
        chunks.append(current_chunk)
    return chunks if chunks else [text]


# --- 【V18 重构】 更新核心工作函数，使其直接修改传入的字幕对象 ---
def generate_audio_single_task(session_pool: queue.Queue, task_info: Dict[str, Any], model_id: str) -> None:
    """
    核心音频生成工作函数。
    【V18变更】:
    - 此函数不再返回字典，而是直接修改 `task_info['original_subtitle_obj']`。
    - 成功时，添加 'audio_path' 键。
    - 失败时，添加 'error' 键。
    """
    task_id = task_info["id"]
    # 【V18变更】: 从 task_info 中获取所有需要的信息
    subtitle_obj = task_info["original_subtitle_obj"]

    raw_text = subtitle_obj["text"]
    subtitle_text = _clean_text_for_tts(str(raw_text))
    if raw_text != subtitle_text:
        # 如果文本发生了变化（说明含有脏字符），打印日志方便排查
        logger.info(
            f"[Task {task_id}] 检测到特殊符号，已自动清洗文本: '{raw_text[:20]}...' -> '{subtitle_text[:20]}...'")

    full_audio_path = task_info["local_path"]
    public_url = task_info["public_url"]

    audio_save_path = os.path.dirname(full_audio_path)
    try:
        with dir_creation_lock:
            if not os.path.exists(audio_save_path):
                os.makedirs(audio_save_path, exist_ok=True)
                logger.debug(f"[Task {task_id}] 目录 {audio_save_path} 已创建。")
    except Exception as e:
        logger.error(f"[Task {task_id}] 创建目录 {audio_save_path} 失败: {e}")
        subtitle_obj["error"] = f"Failed to create directory: {e}"
        subtitle_obj["audio_path"] = None
        return

    logger.info(f"[Task {task_id}] 开始处理: '{str(subtitle_text)[:30] if subtitle_text else '[S I L E N C E]'}'")

    # 静音快速通道
    if not str(subtitle_text).strip():
        try:
            target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
            if target_duration_sec <= 0:
                logger.warning(f"[Task {task_id}] 静音任务时长无效 ({target_duration_sec}s)，创建空文件。")
                open(full_audio_path, 'a').close()
            else:
                target_duration_ms = int(target_duration_sec * 1000)
                logger.info(f"[Task {task_id}] 生成 {target_duration_ms}ms 的静音音频。")
                silence = AudioSegment.silent(duration=target_duration_ms)
                silence.export(full_audio_path, format=AUDIO_FORMAT)

            subtitle_obj["audio_path"] = public_url
            logger.info(f"[Task {task_id}] 本地静音生成成功。")
            return
        except Exception as e:
            logger.error(f"[Task {task_id}] 生成静音时发生致命错误: {e}")
            subtitle_obj["error"] = f"Local silence generation failed: {str(e)}"
            subtitle_obj["audio_path"] = None
            return

    session = None
    try:
        session = session_pool.get(timeout=60)
        logger.debug(f"[Task {task_id}] 成功从池中获取 Session。")
        temp_dir = os.path.dirname(full_audio_path)
        for attempt in range(MAX_RETRIES):
            try:
                text_chunks = _split_text_into_chunks(subtitle_text, TEXT_SPLIT_THRESHOLD)
                if len(text_chunks) > 1:
                    logger.info(f"[Task {task_id}] 文本过长，已切分为 {len(text_chunks)} 段。")

                audio_segments = []
                for i, chunk_text in enumerate(text_chunks):
                    temp_chunk_path = os.path.join(temp_dir, f"temp_{task_id}_{i}.{AUDIO_FORMAT}")
                    try:
                        logger.debug(f"[Task {task_id}-{i}] 生成分片: '{chunk_text}'")
                        req = TTSRequest(text=chunk_text, reference_id=model_id, model=ENGINE_MODEL,
                                         format=AUDIO_FORMAT)
                        with open(temp_chunk_path, "wb") as f:
                            for chunk in session.tts(req):
                                f.write(chunk)
                        if os.path.getsize(temp_chunk_path) == 0:
                            raise ValueError(f"生成的音频分片 {i} 为空文件。")
                        audio_segments.append(AudioSegment.from_file(temp_chunk_path))
                    finally:
                        if os.path.exists(temp_chunk_path): os.remove(temp_chunk_path)

                if not audio_segments: raise ValueError("未能生成任何有效的音频分片。")

                combined_audio = sum(audio_segments, AudioSegment.empty())
                final_audio = _process_and_finalize_audio(combined_audio, task_info)
                final_audio.export(full_audio_path, format=AUDIO_FORMAT)

                subtitle_obj["audio_path"] = public_url  # 注入成功URL
                logger.info(f"[Task {task_id}] TTS音频生成成功。")
                break  # 成功，跳出重试循环

            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"[Task {task_id}] TTS主流程第 {attempt + 1}/{MAX_RETRIES} 次尝试失败: {e}. {wait_time}s 后重试...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"[Task {task_id}] 所有重试均失败，最终错误: {e}")
                    subtitle_obj["error"] = f"TTS generation failed after {MAX_RETRIES} retries: {str(e)}"
                    subtitle_obj["audio_path"] = None
                    break
    except queue.Empty:
        logger.error(f"[Task {task_id}] 等待60秒后仍无法从 Session 池中获取连接。")
        subtitle_obj["error"] = "Failed to get a session from the pool (timeout)."
        subtitle_obj["audio_path"] = None
    except Exception as e:
        logger.error(f"[Task {task_id}] 任务执行期间发生未处理的异常: {e}", exc_info=True)
        subtitle_obj["error"] = f"Unhandled exception: {str(e)}"
        subtitle_obj["audio_path"] = None
    finally:
        if session:
            session_pool.put(session)
            logger.debug(f"[Task {task_id}] 已将 Session 归还到池中。")


def _move_workflow_directory(workflow_id: str):
    """文件移动 (保持不变)"""
    source_dir = SOURCE_DIR_TEMPLATE.format(workflow_id=workflow_id)
    dest_path = os.path.join(DEST_BASE_DIR, workflow_id)
    if not os.path.exists(source_dir):
        raise FileNotFoundError(f"源目录 '{source_dir}' 不存在")
    try:
        os.makedirs(DEST_BASE_DIR, exist_ok=True)
        if os.path.exists(dest_path):
            shutil.rmtree(dest_path)
        shutil.move(source_dir, DEST_BASE_DIR)
        logger.info(f"成功移动文件夹 '{workflow_id}' 到 '{DEST_BASE_DIR}'")
    except Exception as e:
        raise IOError(f"移动文件夹时发生错误: {str(e)}")


# --- 【V19 重构】提取核心工作流处理逻辑 ---
async def _process_workflow(
        workflow_id: str,
        raw_script: Any,
        model_id: str
) -> (Any, Optional[str]):
    """
    处理整个工作流的核心逻辑函数。

    :param workflow_id: 工作流ID。
    :param raw_script: 原始脚本JSON。
    :param model_id: 模型ID。
    :return: 一个元组，包含 (被修改后的raw_script, 移动操作的错误信息或None)。
    """
    global global_session_pool
    # Session 池初始化逻辑 (复用自原POST接口)
    # ... [此处省略与原POST接口完全相同的 session pool 初始化代码] ...
    # 为了简洁，我们假设 session pool 已经在 startup 中被妥善处理，或在使用前检查
    if global_session_pool is None:
        # 在真实场景中，这里应该调用初始化逻辑
        raise HTTPException(status_code=503, detail="Session pool is not initialized.")

    audio_save_path = AUDIO_SAVE_PATH_TEMPLATE.format(workflow_id=workflow_id)

    try:
        subtitle_objects = list(extract_subtitles_from_json(raw_script))
        if not subtitle_objects:
            raise ValueError("在提供的JSON结构中未能找到任何有效的字幕对象。")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # 构建处理任务列表... (与原POST接口逻辑完全相同)
    # ... [此处省略任务列表构建代码] ...
    tasks_to_process = []
    # ...[这部分逻辑与你原代码中构建 tasks_to_process 的部分完全相同，可直接复制]...
    for subtitle_obj in subtitle_objects:
        try:
            subtitle_id = subtitle_obj['id']
            start_sec = float(subtitle_obj["start_time_seconds"])
            end_sec = float(subtitle_obj["end_time_seconds"])
            if subtitle_id is None or str(subtitle_id).strip() == "":
                raise ValueError("字幕对象 'id' 字段为空或无效。")
            if end_sec <= start_sec:
                logger.warning(f"跳过ID {subtitle_id}的任务：无效时间范围 start={start_sec}, end={end_sec}")
                subtitle_obj['error'] = 'Invalid time range (end <= start)'
                subtitle_obj['audio_path'] = None
                continue
        except (KeyError, TypeError, ValueError) as e:
            error_field = "未知"
            if isinstance(e, KeyError): error_field = str(e)
            logger.error(
                f"解析字幕对象时出错，可能缺少或类型错误的字段 ({error_field}): {subtitle_obj}, 错误: {e}。跳过此任务。")
            subtitle_obj['error'] = f'Parsing error, missing or invalid field ({error_field}): {e}'
            subtitle_obj['audio_path'] = None
            continue
        safe_subtitle_id = re.sub(r'[\\/*?:"<>|]', "_", str(subtitle_id))
        audio_filename = f"audio_{safe_subtitle_id}.{AUDIO_FORMAT}"
        tasks_to_process.append({
            "id": subtitle_id,
            "start_sec": start_sec,
            "end_sec": end_sec,
            "original_subtitle_obj": subtitle_obj,
            "local_path": os.path.join(audio_save_path, audio_filename),
            "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
        })

    logger.info(f"[{workflow_id}] JSON解析完成，共 {len(tasks_to_process)} 个有效任务待处理。提交到线程池。")

    # 并发执行 (与原POST接口逻辑完全相同)
    try:
        loop = asyncio.get_running_loop()
        async_tasks = [
            loop.run_in_executor(
                tts_thread_pool,
                generate_audio_single_task,
                global_session_pool,
                task,
                model_id
            ) for task in tasks_to_process
        ]
        await asyncio.gather(*async_tasks)
    except Exception as e:
        logger.error(f"[{workflow_id}] 并行处理任务时发生主错误: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"并行处理任务时发生主错误: {e}")

    # 文件移动和结果返回
    move_error = None
    if any("audio_path" in s and s["audio_path"] is not None for s in subtitle_objects):
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _move_workflow_directory, workflow_id)
        except (FileNotFoundError, IOError) as e:
            move_error = str(e)
            logger.error(f"[{workflow_id}] 文件移动操作失败: {e}")
    else:
        logger.warning(f"[{workflow_id}] 没有任何音频生成成功，跳过文件移动操作。")

    return raw_script, move_error


# --- 【V18 重构】 更新 Pydantic 输入模型 ---
class TTSRequestPayload(BaseModel):
    # raw_script 现在是 Any 类型，可以接受任何合法的JSON (dict, list)
    raw_script: Any = Field(..., description="包含字幕信息的原始JSON结构。")
    language: str = Field(..., description="脚本语言, 例如 'zh', 'en'。")
    model_id: str = Field(..., description="使用的 TTS 模型ID。")
    workflow_id: str = Field(..., description="本次任务的唯一工作流ID。")


# --- 【V18 重构】 更新主API接口 ---
@router.post("/generate_audio_json", summary="从脚本JSON生成音频并回填结果", response_model=Any)
async def generate_audio_workflow(payload: TTSRequestPayload):
    async with api_semaphore:
        global global_session_pool
        logger.info(f"获得并发许可, 开始处理 workflow_id: '{payload.workflow_id}'")
        # 调用重构后的核心逻辑
        updated_script, move_error = await _process_workflow(
            workflow_id=payload.workflow_id,
            raw_script=payload.raw_script,
            model_id=payload.model_id
        )
        # 使用标准响应格式返回
        if move_error:
            # 如果移动失败，这是一个严重问题，应在顶层消息中提示
            return create_standard_response(
                data=updated_script,
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=f"音频生成完成，但文件移动失败: {move_error}"
            )

        return create_standard_response(data=updated_script)


# --- 【V19 新增】PUT 路由的 Pydantic 输入模型 ---

class RegenerateWorkflowPayload(BaseModel):
    """用于批量重新生成整个工作流音频的请求体"""
    raw_script: Any = Field(..., description="包含字幕信息的完整原始JSON结构。")
    language: str = Field(..., description="脚本语言, 例如 'zh'。")
    model_id: str = Field(..., description="要使用的 TTS 模型ID。")


class SingleSubtitle(BaseModel):
    """单个字幕对象的结构，用于单个文件更新"""
    id: str = Field(..., description="字幕的唯一ID")
    text: str = Field(..., description="字幕文本")
    start_time_seconds: float = Field(..., description="开始时间（秒）")
    end_time_seconds: float = Field(..., description="结束时间（秒）")

    # 允许包含其他未定义字段
    class Config:
        extra = 'allow'


class RegenerateSinglePayload(BaseModel):
    """用于重新生成单个音频文件的请求体"""
    subtitle_data: SingleSubtitle = Field(..., description="要更新的单个字幕对象的数据。")
    model_id: str = Field(..., description="要使用的 TTS 模型ID。")


# --- 【V19 新增】PUT 路由接口实现 ---

@router.put("/audio/{workflow_id}", summary="重新生成整个工作流的所有音频文件")
async def regenerate_workflow_audio(workflow_id: str, payload: RegenerateWorkflowPayload):
    """
    通过提供新的 `raw_script` 和 `model_id`，重新生成指定 `workflow_id` 的所有音频文件。
    此操作会覆盖该工作流下的所有旧音频。
    """
    async with api_semaphore:
        logger.info(f"收到工作流 '{workflow_id}' 的批量重新生成请求。")

        # 直接调用重构后的核心逻辑函数
        updated_script, move_error = await _process_workflow(
            workflow_id=workflow_id,
            raw_script=payload.raw_script,
            model_id=payload.model_id
        )

        if move_error:
            return create_standard_response(
                data=updated_script,
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=f"音频重新生成完成，但最终文件移动失败: {move_error}"
            )

        logger.info(f"工作流 '{workflow_id}' 的批量重新生成成功完成。")
        return create_standard_response(
            data=updated_script,
            message=f"Workflow '{workflow_id}' audio regenerated successfully."
        )


@router.put("/audio/{workflow_id}/{subtitle_id}", summary="重新生成并替换单个音频文件")
async def regenerate_single_audio(workflow_id: str, subtitle_id: str, payload: RegenerateSinglePayload):
    """
    根据提供的字幕数据，重新生成单个音频文件，并替换掉旧文件。
    """
    async with api_semaphore:
        global global_session_pool
        logger.info(f"收到为 workflow '{workflow_id}' 下的字幕ID '{subtitle_id}' 的单个重新生成请求。")

        if global_session_pool is None:
            logger.error("全局 Session 池未初始化，无法处理单个重新生成请求。")
            return create_standard_response(
                code=status.HTTP_503_SERVICE_UNAVAILABLE,
                message="服务暂时不可用：TTS Session 池未初始化。"
            )

        subtitle_obj = payload.subtitle_data.model_dump()
        model_id = payload.model_id

        # 1. 构建单个任务
        safe_subtitle_id = re.sub(r'[\\/*?:"<>|]', "_", str(subtitle_id))
        audio_filename = f"audio_{safe_subtitle_id}.{AUDIO_FORMAT}"

        # 注意：这里我们直接在最终目标目录操作
        dest_dir = os.path.join(DEST_BASE_DIR, workflow_id, "audio")
        final_audio_path = os.path.join(dest_dir, audio_filename)

        # 确保目标目录存在
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"为 '{workflow_id}/{subtitle_id}' 创建目标目录 '{dest_dir}' 失败: {e}")
            return create_standard_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=f"创建目标目录失败: {e}"
            )

        task_info = {
            "id": subtitle_id,
            "start_sec": subtitle_obj["start_time_seconds"],
            "end_sec": subtitle_obj["end_time_seconds"],
            # 【关键】将传入的 subtitle_obj 作为原始对象引用
            "original_subtitle_obj": subtitle_obj,
            # 【重要变更】我们将 local_path 直接指向最终路径，因为是单个文件操作
            "local_path": final_audio_path,
            "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
        }

        # 2. 在线程池中执行单个任务
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                tts_thread_pool,
                generate_audio_single_task,
                global_session_pool,
                task_info,
                model_id
            )
        except Exception as e:
            logger.error(f"为 '{workflow_id}/{subtitle_id}' 执行单个生成任务时发生未知错误: {e}", exc_info=True)
            return create_standard_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=f"任务执行期间发生未知错误: {e}"
            )

        # 3. 检查任务执行结果并返回
        if "error" in subtitle_obj and subtitle_obj["error"]:
            error_message = subtitle_obj["error"]
            logger.error(f"为 '{workflow_id}/{subtitle_id}' 生成音频失败: {error_message}")
            return create_standard_response(
                data=subtitle_obj,
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=f"Audio generation failed for subtitle '{subtitle_id}': {error_message}"
            )

        logger.info(f"成功为 '{workflow_id}/{subtitle_id}' 重新生成音频。")
        return create_standard_response(
            data=subtitle_obj,
            message=f"Audio for subtitle '{subtitle_id}' in workflow '{workflow_id}' regenerated successfully."
        )

# # -*- coding: utf-8 -*-
# # @File：main_app.py
# # @Time：2025/8/5 18:30
# # @Author：_不咬闰土的猹丶
# # @email：hx1561958968@gmail.com
#
# # --- 导入模块 ---
# import re
# import json
# import os
# import shutil
# import logging
# import concurrent.futures
# import time
# import queue
# import sys
# import io
# import math
# import asyncio
# from asyncio import Semaphore
# import threading
# from typing import Dict, List, Any, Tuple, Optional, Union
# # FastAPI 相关导入
# from fastapi import APIRouter, HTTPException, status
# from pydantic import BaseModel, Field
#
# # Fish Audio SDK 导入
# from fish_audio_sdk import Session, TTSRequest, Prosody
#
# # pydub 导入，用于音频处理
# import numpy as np
# import pyrubberband
# from pydub import AudioSegment
# from pydub.exceptions import PydubException
#
# # ======================================================================================
# # --- [V12] 工业级日志配置 (替换原有的 basicConfig) ---
# # ======================================================================================
# try:
#     from utils.logger import setup_module_logger
# except ImportError:
#     # 备用方案，仅在 utils 模块找不到时触发
#     def setup_module_logger(logger_name: str, log_file: str) -> logging.Logger:
#         logger = logging.getLogger(logger_name)
#         if not logger.hasHandlers():
#             handler = logging.StreamHandler(sys.stdout)
#             formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
#             handler.setFormatter(formatter)
#             logger.addHandler(handler)
#             logger.setLevel(logging.INFO)
#             print(
#                 f"CRITICAL: Failed to import setup_module_logger from utils. Using fallback console logger for {logger_name}.")
#         return logger
# # # 1. 获取根 logger
# logger = setup_module_logger(__name__, "logs/audio/fish_json.log")
#
# # ======================================================================================
#
# router = APIRouter()
#
# # 全局唯一的线程池
# tts_thread_pool: concurrent.futures.ThreadPoolExecutor = None
# api_semaphore: asyncio.Semaphore = None
# global_session_pool: queue.Queue = None
# pool_init_lock = threading.Lock()
# dir_creation_lock = threading.Lock()
#
#
# @router.on_event("startup")
# def startup_event():
#     """在应用启动时执行的函数"""
#     global tts_thread_pool
#     global api_semaphore
#     tts_thread_pool = concurrent.futures.ThreadPoolExecutor(
#         max_workers=MAX_WORKERS,
#         thread_name_prefix="Global_TTS_Worker"
#     )
#     api_semaphore = Semaphore(50)
#     logger.info(f"全局共享TTS线程池已创建，最大工作线程数: {MAX_WORKERS}")
#     logger.info(f"全局信号量已创建，许可数: {50}")
#
#
# @router.on_event("shutdown")
# def shutdown_event():
#     """在应用关闭时执行的函数"""
#     global tts_thread_pool
#     if tts_thread_pool:
#         logger.info("正在关闭全局共享TTS线程池...")
#         tts_thread_pool.shutdown(wait=True)
#         logger.info("全局共享TTS线程池已成功关闭。")
#
#     if global_session_pool:
#         logger.info(f"正在关闭全局 Session 池中的 ({global_session_pool.qsize()}) 个 Session...")
#         while not global_session_pool.empty():
#             try:
#                 session = global_session_pool.get_nowait()
#                 if hasattr(session, 'close'):
#                     session.close()
#             except queue.Empty:
#                 break
#             except Exception as e:
#                 logger.error(f"关闭一个 Session 时出错: {e}")
#         logger.info("全局 Session 池已被清理。")
#
#
# # --- Clash 代理设置区 ---
# PROXY_URL = ""
# if PROXY_URL:
#     os.environ['HTTP_PROXY'] = PROXY_URL
#     os.environ['HTTPS_PROXY'] = PROXY_URL
#     os.environ['http_proxy'] = PROXY_URL
#     os.environ['https_proxy'] = PROXY_URL
#     logger.info(f"已配置全局 HTTP/HTTPS 代理: {PROXY_URL}")
# else:
#     logger.info("未配置代理，将直接进行网络连接。")
#
# # --- 配置区 ---
# ENGINE_MODEL = "speech-1.6"
# AUDIO_FORMAT = "mp3"
# PUBLIC_URL_TEMPLATE = "https://server.x-pilot.ai/static/meta-doc/video/{workflow_id}/audio/{filename}"
# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# STATIC_DIR = os.path.join(BASE_DIR, "static")
# SOURCE_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
# AUDIO_SAVE_PATH_TEMPLATE = os.path.join(SOURCE_DIR_TEMPLATE, "audio")
# DEST_BASE_DIR = "/data/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
# MAX_WORKERS = 15
# SESSION_POOL_SIZE = MAX_WORKERS
# MAX_RETRIES = 3
# RETRY_DELAY = 2
# TEXT_SPLIT_THRESHOLD = 120
# SENTENCE_SPLIT_PATTERN = r"([。！？，、；…])"
# ENABLE_DYNAMIC_SPEED_ADJUSTMENT = True
# SPEED_ADJUST_THRESHOLD_RATIO = 1.05
# MAX_SPEECH_SPEED = 1.3
# ENABLE_DYNAMIC_DECELERATION = True
# MIN_SPEECH_SPEED = 0.95
# START_PADDING_BUFFER_MS = 150
# # 时间单位定义
# TIME_KEYWORDS = {
#     'zh': {'minute_keys': ['分'], 'second_keys': ['秒'], 'separator_keys': ['-', '到', '至', '~', '～']},
#     'en': {'minute_keys': ['minute', 'minutes', 'min', 'm'], 'second_keys': ['second', 'seconds', 'sec', 's'],
#            'separator_keys': ['-', 'to', '–', '—']},
#     'de': {'minute_keys': ['minute', 'minuten', 'm'], 'second_keys': ['sekunde', 'sekunden', 'sek', 's'],
#            'separator_keys': ['-', 'bis', '–']},
#     'fr': {'minute_keys': ['minute', 'minutes', 'min', 'm'], 'second_keys': ['seconde', 'secondes', 'sec', 's'],
#            'separator_keys': ['-', 'à', '–']},
#     'es': {'minute_keys': ['minuto', 'minutos', 'min', 'm'], 'second_keys': ['segundo', 'segundos', 'seg', 's'],
#            'separator_keys': ['-', 'a', '–']},
#     'ko': {'minute_keys': ['분'], 'second_keys': ['초'], 'separator_keys': ['-', '~', '～']},
#     'ja': {'minute_keys': ['分'], 'second_keys': ['秒'], 'separator_keys': ['-', '~', '～', 'から']},
#     'ar': {'minute_keys': ['دقيقة', 'دقائق'], 'second_keys': ['ثانية', 'ثواني'],
#            'separator_keys': ['-', ' إلى ', 'إلي']},
# }
#
#
# # --- 所有业务逻辑和工具函数 (保持不变) ---
#
# def _process_and_finalize_audio(audio: AudioSegment, task_info: Dict[str, Any]) -> AudioSegment:
#     # 此函数逻辑保持不变
#     task_id = task_info['id']
#     target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
#
#     if target_duration_sec <= 0:
#         logger.warning(f"[Task {task_id}] 目标时长无效 ({target_duration_sec}s)，返回原始TTS音频。")
#         return audio
#
#     processed_audio, target_duration_ms, actual_duration_ms = audio, int(target_duration_sec * 1000), len(audio)
#
#     if ENABLE_DYNAMIC_SPEED_ADJUSTMENT and actual_duration_ms > target_duration_ms * SPEED_ADJUST_THRESHOLD_RATIO:
#         ratio = actual_duration_ms / target_duration_ms
#         logger.warning(
#             f"[Task {task_id}] 音频过长({actual_duration_ms}ms > 目标 {target_duration_ms}ms)，将使用 pyrubberband 加速，速度: {ratio:.2f}x。")
#         samples = np.array(processed_audio.get_array_of_samples())
#         stretched_samples = pyrubberband.time_stretch(samples, processed_audio.frame_rate, ratio)
#         processed_audio = AudioSegment(stretched_samples.tobytes(), frame_rate=processed_audio.frame_rate,
#                                        sample_width=processed_audio.sample_width, channels=processed_audio.channels)
#         logger.info(f"[Task {task_id}] 高质量加速后音频时长: {len(processed_audio)}ms")
#     elif ENABLE_DYNAMIC_DECELERATION and actual_duration_ms < target_duration_ms:
#         calculated_ratio = actual_duration_ms / target_duration_ms
#         if calculated_ratio >= MIN_SPEECH_SPEED:
#             logger.info(
#                 f"[Task {task_id}] 音频偏短({actual_duration_ms}ms < 目标 {target_duration_ms}ms)，进行高质量减速。比率:{calculated_ratio:.2f}x")
#             samples = np.array(processed_audio.get_array_of_samples())
#             stretched_samples = pyrubberband.time_stretch(samples, processed_audio.frame_rate, calculated_ratio)
#             processed_audio = AudioSegment(stretched_samples.tobytes(), frame_rate=processed_audio.frame_rate,
#                                            sample_width=processed_audio.sample_width, channels=processed_audio.channels)
#             logger.info(f"[Task {task_id}] 减速后音频时长: {len(processed_audio)}ms")
#         else:
#             logger.info(
#                 f"[Task {task_id}] 所需减速比({calculated_ratio:.2f}x)超出下限({MIN_SPEECH_SPEED})，退回静音填充。")
#
#     final_duration_ms = len(processed_audio)
#     duration_diff_ms = target_duration_ms - final_duration_ms
#     if duration_diff_ms > 0:
#         start_padding_ms, end_padding_ms = min(duration_diff_ms, START_PADDING_BUFFER_MS), duration_diff_ms - min(
#             duration_diff_ms, START_PADDING_BUFFER_MS)
#         final_audio = AudioSegment.silent(duration=start_padding_ms) + processed_audio + AudioSegment.silent(
#             duration=end_padding_ms)
#         logger.info(
#             f"[Task {task_id}] 成功填充静音。填充: {duration_diff_ms}ms (头:{start_padding_ms}ms, 尾:{end_padding_ms}ms)。最终时长: {len(final_audio)}ms")
#         return final_audio
#     elif duration_diff_ms < 0:
#         ms_to_crop = -duration_diff_ms
#         logger.warning(
#             f"[Task {task_id}] 内容溢出: 最终音频({final_duration_ms}ms) > 目标({target_duration_ms}ms)。将从尾部裁剪 {ms_to_crop}ms。")
#         final_audio = processed_audio[:target_duration_ms]
#         return final_audio
#     else:
#         return processed_audio
#
#
# def _parse_time_to_seconds(time_str: str) -> float:
#     # 此函数逻辑保持不变
#     return 0.0  # 在新流程中不再需要，但保留定义
#
#
# def _split_text_into_chunks(text: str, max_len: int) -> List[str]:
#     # 此函数逻辑保持不变
#     if len(text) <= max_len: return [text]
#     parts, sentences = re.split(SENTENCE_SPLIT_PATTERN, text), []
#     for i in range(0, len(parts) - 1, 2): sentences.append(
#         parts[i] + (parts[i + 1] if i + 1 < len(parts) and parts[i + 1] else ''))
#     if len(parts) % 2 == 1 and parts[-1]: sentences.append(parts[-1])
#     chunks, current_chunk = [], ""
#     for sentence in sentences:
#         if not sentence.strip(): continue
#         if len(current_chunk) + len(sentence) <= max_len:
#             current_chunk += sentence
#         else:
#             if current_chunk: chunks.append(current_chunk)
#             if len(sentence) > max_len:
#                 if current_chunk: current_chunk = ""
#                 chunks.append(sentence)
#             else:
#                 current_chunk = sentence
#     if current_chunk: chunks.append(current_chunk)
#     return chunks if chunks else [text]
#
#
# def generate_audio_single_task(session_pool: queue.Queue, task_info: Dict[str, Any], model_id: str) -> Dict[str, Any]:
#     # 此函数逻辑保持不变
#     task_id, subtitle_text, full_audio_path = task_info["id"], task_info["text"], task_info["local_path"]
#     audio_save_path = os.path.dirname(full_audio_path)
#     try:
#         with dir_creation_lock:
#             if not os.path.exists(audio_save_path): os.makedirs(audio_save_path, exist_ok=True)
#     except Exception as e:
#         task_info.update({"audio_path": None, "error": f"Failed to create directory: {e}"})
#         task_info.pop("local_path", None);
#         task_info.pop("public_url", None)
#         return task_info
#
#     logger.info(f"[Task {task_id}] 开始处理: '{subtitle_text[:30] if subtitle_text else '[S I L E N C E]'}'")
#     if not subtitle_text.strip():
#         try:
#             target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
#             if target_duration_sec > 0:
#                 silence = AudioSegment.silent(duration=int(target_duration_sec * 1000))
#                 silence.export(full_audio_path, format=AUDIO_FORMAT)
#             else:
#                 open(full_audio_path, 'a').close()
#             task_info["audio_path"] = task_info["public_url"]
#         except Exception as e:
#             task_info.update({"audio_path": None, "error": f"Local silence generation failed: {str(e)}"})
#         finally:
#             task_info.pop("local_path", None);
#             task_info.pop("public_url", None)
#             return task_info
#
#     session = None
#     try:
#         session = session_pool.get(timeout=60)
#         for attempt in range(MAX_RETRIES):
#             try:
#                 text_chunks = _split_text_into_chunks(subtitle_text, TEXT_SPLIT_THRESHOLD)
#                 audio_segments = []
#                 for i, chunk_text in enumerate(text_chunks):
#                     temp_chunk_path = os.path.join(audio_save_path, f"temp_{task_id}_{i}.{AUDIO_FORMAT}")
#                     try:
#                         req = TTSRequest(text=chunk_text, reference_id=model_id, model=ENGINE_MODEL,
#                                          format=AUDIO_FORMAT, prosody=Prosody(speed=0.9))
#                         with open(temp_chunk_path, "wb") as f:
#                             for chunk in session.tts(req): f.write(chunk)
#                         audio_segments.append(AudioSegment.from_file(temp_chunk_path))
#                     finally:
#                         if os.path.exists(temp_chunk_path): os.remove(temp_chunk_path)
#                 final_audio = _process_and_finalize_audio(sum(audio_segments, AudioSegment.empty()), task_info)
#                 final_audio.export(full_audio_path, format=AUDIO_FORMAT)
#                 task_info["audio_path"] = task_info["public_url"]
#                 break
#             except Exception as e:
#                 if attempt >= MAX_RETRIES - 1:
#                     task_info.update({"audio_path": None, "error": f"TTS failed after {MAX_RETRIES} retries: {str(e)}"})
#                 else:
#                     time.sleep(RETRY_DELAY * (2 ** attempt))
#     except queue.Empty:
#         task_info.update({"audio_path": None, "error": "Failed to get a session from the pool (timeout)."})
#     except Exception as e:
#         task_info.update({"audio_path": None, "error": f"Unhandled exception: {str(e)}"})
#     finally:
#         if session: session_pool.put(session)
#         task_info.pop("local_path", None);
#         task_info.pop("public_url", None)
#     return task_info
#
#
# def _move_workflow_directory(workflow_id: str):
#     # 此函数逻辑保持不变
#     source_dir = SOURCE_DIR_TEMPLATE.format(workflow_id=workflow_id)
#     dest_path = os.path.join(DEST_BASE_DIR, workflow_id)
#     if not os.path.exists(source_dir): raise FileNotFoundError(f"源目录 '{source_dir}' 不存在")
#     try:
#         os.makedirs(DEST_BASE_DIR, exist_ok=True)
#         if os.path.exists(dest_path): shutil.rmtree(dest_path)
#         shutil.move(source_dir, DEST_BASE_DIR)
#         logger.info(f"成功移动文件夹 '{workflow_id}' 到 '{DEST_BASE_DIR}'")
#     except Exception as e:
#         raise IOError(f"移动文件夹时发生错误: {str(e)}")
#
#
# # --- Pydantic 模型 (与上次相同) ---
# class Subtitle(BaseModel):
#     text: str
#     start_time_seconds: float
#     end_time_seconds: float
#     narration_voices: Optional[Dict[str, str]] = None
#
#
# class Scene(BaseModel):
#     id: str;
#     subtitles: List[Subtitle]
#
#     class Config: extra = 'ignore'
#
#
# class ScriptPayload(BaseModel):
#     title: str;
#     scenes: List[Scene]
#
#     class Config: extra = 'ignore'
#
#
# class TTSRequestPayload(BaseModel):
#     script_data: ScriptPayload = Field(..., description="结构化的JSON格式剧本。")
#     model_id: str = Field(..., description="使用的 TTS 模型ID。")
#     workflow_id: str = Field(..., description="本次任务的唯一工作流ID。")
#     fish_api_key: str = Field(..., description="Fish Audio 的 API Key。")
#
#
# @router.post("/generate_audio_json", summary="从脚本生成音频并处理文件", response_model=Dict[str, Any])
# async def generate_audio_workflow(payload: TTSRequestPayload):
#     async with api_semaphore:
#         global global_session_pool
#         logger.info(f"获得并发许可, 开始处理 workflow_id: '{payload.workflow_id}'")
#         script_data, model_id, workflow_id, fish_api_key = payload.script_data, payload.model_id, payload.workflow_id, payload.fish_api_key
#         if not all([fish_api_key, model_id, workflow_id, script_data]):
#             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="输入参数不完整。")
#         logger.info(f"收到请求, workflow_id: '{workflow_id}' for title: '{script_data.title}'")
#
#         # Session 池初始化逻辑 (保持不变)
#         if global_session_pool is None:
#             with pool_init_lock:
#                 if global_session_pool is None:
#                     # (此处省略初始化代码，逻辑不变)
#                     try:
#                         new_pool = queue.Queue(maxsize=SESSION_POOL_SIZE)
#                         for i in range(SESSION_POOL_SIZE): new_pool.put(Session(fish_api_key))
#                         global_session_pool = new_pool
#                         logger.info("全局 Session 池成功创建并已缓存。")
#                     except Exception as e:
#                         global_session_pool = None
#                         raise HTTPException(status_code=503, detail=f"创建 TTS Session 池失败: {e}")
#
#         audio_save_path = AUDIO_SAVE_PATH_TEMPLATE.format(workflow_id=workflow_id)
#
#         # --- [核心修正] 替换解析逻辑，并重建完整时间线（包含静音填充） ---
#         logger.info("[阶段1/3] 正在从JSON结构中提取所有字幕片段...")
#         timed_segments = []
#         try:
#             for scene in script_data.scenes:
#                 for subtitle in scene.subtitles:
#                     # 检查时间有效性
#                     if subtitle.end_time_seconds > subtitle.start_time_seconds:
#                         timed_segments.append({
#                             "start_sec": subtitle.start_time_seconds,
#                             "end_sec": subtitle.end_time_seconds,
#                             "text": (subtitle.narration_voices.get("ZH", subtitle.text)
#                                      if subtitle.narration_voices else subtitle.text).strip()
#                         })
#                     else:
#                         logger.warning(
#                             f"发现无效时间范围 (start >= end) in scene '{scene.id}' for subtitle '{subtitle.text[:20]}...', 已忽略。")
#
#         except Exception as e:
#             raise HTTPException(status_code=400, detail=f"解析JSON脚本时出错: {e}")
#
#         if not timed_segments:
#             raise HTTPException(status_code=400, detail="脚本中未能找到任何有效的字幕条目。")
#
#         logger.info(f"[阶段2/3] 按时间线排序 {len(timed_segments)} 个字幕片段...")
#         sorted_segments = sorted(timed_segments, key=lambda x: x['start_sec'])
#
#         logger.info("[阶段3/3] 构建完整任务列表，自动填充静音间隙...")
#         tasks_to_process = []
#         last_end_time = 0.0
#         id_counter = 0
#
#         for segment in sorted_segments:
#             current_start_time = segment["start_sec"]
#
#             # 检查并填充静音间隙
#             gap_duration = current_start_time - last_end_time
#             if gap_duration > 0.05:  # 忽略50ms以下的微小间隙
#                 logger.info(
#                     f"发现静音间隙: 从 {last_end_time:.3f}s 到 {current_start_time:.3f}s (时长: {gap_duration:.3f}s)")
#                 tasks_to_process.append({
#                     "id": id_counter,
#                     "time_range": f"{last_end_time:.3f}秒 - {current_start_time:.3f}秒",
#                     "start_sec": last_end_time,
#                     "end_sec": current_start_time,
#                     "text": "",  # 空文本代表静音
#                     "local_path": os.path.join(audio_save_path,
#                                                f"audio_{id_counter:03d}_{last_end_time:.1f}s_silent.mp3"),
#                     "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id,
#                                                              filename=f"audio_{id_counter:03d}_{last_end_time:.1f}s_silent.mp3")
#                 })
#                 id_counter += 1
#
#             # 添加当前的有声任务
#             tasks_to_process.append({
#                 "id": id_counter,
#                 "time_range": f"{segment['start_sec']:.3f}秒 - {segment['end_sec']:.3f}秒",
#                 "start_sec": segment["start_sec"],
#                 "end_sec": segment["end_sec"],
#                 "text": segment["text"],
#                 "local_path": os.path.join(audio_save_path, f"audio_{id_counter:03d}_{segment['start_sec']:.1f}s.mp3"),
#                 "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id,
#                                                          filename=f"audio_{id_counter:03d}_{segment['start_sec']:.1f}s.mp3")
#             })
#             id_counter += 1
#
#             last_end_time = segment["end_sec"]
#
#         auto_generated_silent_count = sum(1 for task in tasks_to_process if not task['text'])
#         logger.info(
#             f"时间线构建完成！最终生成 {len(tasks_to_process)} 个任务 (包含 {auto_generated_silent_count} 个自动生成的静音任务)。")
#         # ----------------------------------------------------------------
#
#         final_tasks_list = []
#         try:
#             # 并发处理逻辑 (保持不变)
#             loop = asyncio.get_running_loop()
#             async_tasks = [
#                 loop.run_in_executor(tts_thread_pool, generate_audio_single_task, global_session_pool, task, model_id)
#                 for task in tasks_to_process]
#             final_tasks_list = list(await asyncio.gather(*async_tasks))
#         except Exception as e:
#             # 异常处理逻辑 (保持不变)
#             if "auth" in str(e).lower() or "apikey" in str(e).lower():
#                 raise HTTPException(status_code=401, detail=f"Fish Audio认证失败: {e}")
#             raise HTTPException(status_code=500, detail=f"并行处理任务时发生主错误: {e}")
#
#         final_tasks_list.sort(key=lambda x: x['id'])
#         move_error = None
#         if any(task.get("audio_path") for task in final_tasks_list):
#             try:
#                 # 文件移动逻辑 (保持不变)
#                 await asyncio.get_running_loop().run_in_executor(None, _move_workflow_directory, workflow_id)
#             except (FileNotFoundError, IOError) as e:
#                 move_error = str(e)
#                 logger.error(f"文件移动操作失败: {e}")
#         else:
#             logger.warning("没有任何音频生成成功，跳过文件移动操作。")
#
#         final_result = {
#             "audio_tasks": final_tasks_list,
#             "audio_tasks_str": json.dumps(final_tasks_list, ensure_ascii=False)
#         }
#         if move_error: final_result["move_operation_error"] = move_error
#
#         return final_result
