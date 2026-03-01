# -*- coding: utf-8 -*-
# @File：main_app_refactored.py
# @Time：2025/08/06 10:00
# @Author：_不咬闰土的猹丶 (Refactored by Senior Software Engineer)
# @email：hx1561958968@gmail.com
import io
# --- 导入模块 ---
import re
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
from datetime import datetime
import unicodedata

# FastAPI 相关导入
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Fish Audio SDK 导入
from fish_audio_sdk import Session, TTSRequest

# pydub 导入，用于音频处理
import numpy as np
import pyrubberband
from pydub import AudioSegment

# ======================================================================================
# --- [V12] 工业级日志配置 (保持不变) ---
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

# --- 全局资源 ---
tts_thread_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
api_semaphore: Optional[asyncio.Semaphore] = None
global_session_pool: Optional[queue.Queue] = None
pool_init_lock = threading.Lock()
dir_creation_lock = threading.Lock()

# --- 代理与配置区 (保持不变) ---
PROXY_URL = ""
ENGINE_MODEL = "speech-1.6"
AUDIO_FORMAT = "mp3"
# 【建议】将 HOST 和端口配置化，便于部署
API_BASE_URL = "https://server.x-pilot.ai"
PUBLIC_URL_TEMPLATE = f"{API_BASE_URL}/static/meta-doc/video/{{workflow_id}}/audio/{{filename}}"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
# 临时工作目录
TEMP_WORK_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
# 最终产物目录
FINAL_DEST_DIR = "/data/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
MAX_WORKERS = 15
SESSION_POOL_SIZE = MAX_WORKERS
MAX_RETRIES = 3
RETRY_DELAY = 2
TEXT_SPLIT_THRESHOLD = 120
FISH_API_KEY = "dae51de32a0743f6b4f2f7b6366747bf"  # 【安全建议】应从环境变量或配置文件读取
SENTENCE_SPLIT_PATTERN = r"([。！？，、；…])"
ENABLE_DYNAMIC_SPEED_ADJUSTMENT = True
SPEED_ADJUST_THRESHOLD_RATIO = 1.05
MAX_SPEECH_SPEED = 1.3
ENABLE_DYNAMIC_DECELERATION = True
MIN_SPEECH_SPEED = 0.95
START_PADDING_BUFFER_MS = 150


# ======================================================================================
# --- 生命周期事件 (Startup / Shutdown) ---
# ======================================================================================

@router.on_event("startup")
def startup_event():
    """在应用启动时初始化全局资源。"""
    global tts_thread_pool, api_semaphore, global_session_pool

    if PROXY_URL:
        os.environ['HTTP_PROXY'] = PROXY_URL
        os.environ['HTTPS_PROXY'] = PROXY_URL
        logger.info(f"已配置全局 HTTP/HTTPS 代理: {PROXY_URL}")
    else:
        logger.info("未配置代理，将直接进行网络连接。")

    tts_thread_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS,
        thread_name_prefix="Global_TTS_Worker"
    )
    api_semaphore = Semaphore(50)

    # 预先初始化 Session Pool
    with pool_init_lock:
        if global_session_pool is None:
            try:
                logger.info(f"正在初始化全局 Session 池，大小为 {SESSION_POOL_SIZE}...")
                new_pool = queue.Queue(maxsize=SESSION_POOL_SIZE)
                # 【优化】可以在一个线程里完成这个初始化，避免阻塞主事件循环启动
                for i in range(SESSION_POOL_SIZE):
                    new_pool.put(Session(FISH_API_KEY))
                global_session_pool = new_pool
                logger.info("全局 Session 池成功创建并已缓存。")
            except Exception as e:
                logger.critical(f"应用启动时创建 TTS Session 池失败: {e}", exc_info=True)
                # 如果 Session 池创建失败，应用可能无法正常工作，可以选择退出
                # sys.exit(1)

    logger.info(f"全局共享TTS线程池已创建，最大工作线程数: {MAX_WORKERS}")
    logger.info(f"全局信号量已创建，许可数: {50}")


@router.on_event("shutdown")
def shutdown_event():
    """在应用关闭时优雅地释放资源。"""
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
                    session.close()  # 假设 Session 对象有 close 方法
            except queue.Empty:
                break
            except Exception as e:
                logger.error(f"关闭一个 Session 时出错: {e}")
        logger.info("全局 Session 池已被清理。")


# ======================================================================================
# --- API 响应模型与工具函数 ---
# ======================================================================================

class StandardResponse(BaseModel):
    """标准的API响应模型，提供一致的返回结构。"""
    code: int = Field(200, description="业务状态码，通常与HTTP状态码一致")
    message: str = Field("Success", description="响应的文本消息")
    data: Optional[Any] = Field(None, description="实际的响应数据负载")
    timestamp: str = Field(..., description="服务器响应时的ISO 8601格式时间戳")


def create_standard_response(
        data: Optional[Any] = None,
        code: int = 200,
        message: str = "Success"
) -> JSONResponse:
    """创建一个标准格式的 FastAPI 响应，便于客户端统一处理。"""
    content = StandardResponse(
        code=code,
        message=message,
        data=data,
        timestamp=datetime.now().isoformat()
    ).model_dump()
    return JSONResponse(status_code=code, content=content)


def extract_subtitles_from_json(data: Any) -> Generator[Dict[str, Any], None, None]:
    """
    【核心重构】一个健壮的生成器函数，用于深度遍历任何JSON结构并提取字幕对象。
    这使得API不再依赖于固定的JSON schema。
    """
    if isinstance(data, dict):
        # 检查当前字典是否是一个 "字幕对象"
        if all(k in data for k in ['text', 'start_time_seconds', 'end_time_seconds']):
            yield data
            return  # 找到后不再深入此分支，避免重复提取
        # 如果不是字幕对象，则递归遍历它的值
        for value in data.values():
            yield from extract_subtitles_from_json(value)
    elif isinstance(data, list):
        # 如果是列表，递归遍历它的所有元素
        for item in data:
            yield from extract_subtitles_from_json(item)


# ======================================================================================
# --- 核心业务逻辑函数 (解耦且可重用) ---
# ======================================================================================
# 以下函数: _process_and_finalize_audio, _split_text_into_chunks,
# _move_workflow_directory 保持了原有的优秀设计，无需大改。

def _process_and_finalize_audio(audio: AudioSegment, task_info: Dict[str, Any]) -> AudioSegment:
    """音频动态加速和精确静音填充 (保持不变)"""
    # ... (此处代码与你提供的版本完全相同，为简洁省略)
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

    final_duration_ms = len(processed_audio)
    duration_diff_ms = target_duration_ms - final_duration_ms

    if duration_diff_ms > 0:
        start_padding_ms = min(duration_diff_ms, START_PADDING_BUFFER_MS)
        end_padding_ms = duration_diff_ms - start_padding_ms
        start_silence = AudioSegment.silent(duration=start_padding_ms)
        end_silence = AudioSegment.silent(duration=end_padding_ms)
        final_audio = start_silence + processed_audio + end_silence
        return final_audio
    elif duration_diff_ms < 0:
        logger.warning(
            f"[Task {task_id}] 内容溢出: 最终音频({final_duration_ms}ms) > 目标({target_duration_ms}ms)。"
            f"将从尾部裁剪 {abs(duration_diff_ms)}ms。"
        )
        return processed_audio[:target_duration_ms]
    else:
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
    # ... (此处代码与你提供的版本完全相同，为简洁省略)
    if len(text) <= max_len:
        return [text]
    parts = re.split(SENTENCE_SPLIT_PATTERN, text)
    sentences = []
    for i in range(0, len(parts) - 1, 2):
        sentence = parts[i] + (parts[i + 1] if i + 1 < len(parts) and parts[i + 1] else '')
        sentences.append(sentence)
    if len(parts) % 2 == 1 and parts[-1]:
        sentences.append(parts[-1])
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
    """将临时工作目录的内容移动到最终位置。"""
    source_dir = TEMP_WORK_DIR_TEMPLATE.format(workflow_id=workflow_id)
    dest_path = os.path.join(FINAL_DEST_DIR, workflow_id)
    if not os.path.isdir(source_dir):
        # 如果源目录不存在，可能意味着没有任何音频成功生成，这不是一个致命错误。
        logger.warning(f"源目录 '{source_dir}' 不存在，跳过移动操作。")
        return
    try:
        os.makedirs(FINAL_DEST_DIR, exist_ok=True)
        if os.path.exists(dest_path):
            shutil.rmtree(dest_path)
        shutil.move(source_dir, FINAL_DEST_DIR)
        logger.info(f"成功移动文件夹从 '{source_dir}' 到 '{dest_path}'")
    except Exception as e:
        logger.error(f"移动文件夹时发生严重错误: {e}", exc_info=True)
        raise IOError(f"移动文件夹时发生错误: {str(e)}")


def generate_audio_single_task(session_pool: queue.Queue, task_info: Dict[str, Any], model_id: str) -> None:
    """
    【核心重构】核心音频生成工作函数。
    - 不再返回任何内容 (None)。
    - 直接修改 task_info['original_subtitle_obj'] 来注入结果 ('audio_path' 或 'error')。
    """
    task_id = task_info["id"]
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
            os.makedirs(audio_save_path, exist_ok=True)
    except Exception as e:
        error_msg = f"创建目录失败: {e}"
        logger.error(f"[Task {task_id}] {error_msg}")
        subtitle_obj["error"] = error_msg
        subtitle_obj["audio_path"] = None
        return

    # 静音快速通道
    if not str(subtitle_text).strip():
        try:
            target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
            if target_duration_sec <= 0:
                open(full_audio_path, 'a').close()
            else:
                AudioSegment.silent(duration=int(target_duration_sec * 1000)).export(full_audio_path,
                                                                                     format=AUDIO_FORMAT)
            subtitle_obj["audio_path"] = public_url
            logger.info(f"[Task {task_id}] 静音音频生成成功。")
            return
        except Exception as e:
            error_msg = f"生成静音文件时失败: {e}"
            logger.error(f"[Task {task_id}] {error_msg}", exc_info=True)
            subtitle_obj["error"] = error_msg
            subtitle_obj["audio_path"] = None
            return

    session = None
    try:
        session = session_pool.get(timeout=60)
        # TTS 生成逻辑（包含重试）
        for attempt in range(MAX_RETRIES):
            try:
                text_chunks = _split_text_into_chunks(subtitle_text, TEXT_SPLIT_THRESHOLD)
                audio_segments = []
                for i, chunk_text in enumerate(text_chunks):
                    req = TTSRequest(text=chunk_text, reference_id=model_id, model=ENGINE_MODEL, format=AUDIO_FORMAT)
                    # 使用 io.BytesIO 避免写临时文件，更高效
                    with io.BytesIO() as buffer:
                        for chunk in session.tts(req):
                            buffer.write(chunk)
                        buffer.seek(0)
                        if buffer.getbuffer().nbytes == 0:
                            raise ValueError(f"生成的音频分片 {i} 为空。")
                        audio_segments.append(AudioSegment.from_file(buffer, format=AUDIO_FORMAT))

                combined_audio = sum(audio_segments, AudioSegment.empty())
                final_audio = _process_and_finalize_audio(combined_audio, task_info)
                final_audio.export(full_audio_path, format=AUDIO_FORMAT)

                subtitle_obj["audio_path"] = public_url
                logger.info(f"[Task {task_id}] TTS音频生成成功。")
                return  # 成功后直接返回

            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    wait_time = RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"[Task {task_id}] TTS主流程第 {attempt + 1}/{MAX_RETRIES} 次尝试失败: {e}. {wait_time}s 后重试...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"[Task {task_id}] 所有重试均失败，最终错误: {e}", exc_info=True)
                    error_msg = f"TTS generation failed after {MAX_RETRIES} retries: {str(e)}"
                    subtitle_obj["error"] = error_msg
                    subtitle_obj["audio_path"] = None
                    return
    except queue.Empty:
        error_msg = "从 Session 池获取连接超时。"
        logger.error(f"[Task {task_id}] {error_msg}")
        subtitle_obj["error"] = error_msg
        subtitle_obj["audio_path"] = None
    except Exception as e:
        error_msg = f"任务执行期间发生未处理的异常: {e}"
        logger.error(f"[Task {task_id}] {error_msg}", exc_info=True)
        subtitle_obj["error"] = error_msg
        subtitle_obj["audio_path"] = None
    finally:
        if session:
            session_pool.put(session)


# --- [核心重构] 提取出的可重用工作流处理逻辑 ---
async def _process_workflow(
        workflow_id: str,
        raw_script: Any,
        model_id: str
) -> (Any, Optional[str]):
    """
    处理整个工作流的核心逻辑函数，被所有相关API端点复用。
    返回修改后的原始脚本和可能的移动操作错误信息。
    """
    if global_session_pool is None:
        logger.critical(f"[{workflow_id}] 全局 Session 池未初始化，无法处理请求。")
        raise HTTPException(status_code=503, detail="服务暂时不可用：TTS Session 池未初始化。")

    audio_save_path = os.path.join(TEMP_WORK_DIR_TEMPLATE.format(workflow_id=workflow_id), "audio")

    try:
        # 使用新的智能提取器
        subtitle_objects = list(extract_subtitles_from_json(raw_script))
        if not subtitle_objects:
            raise ValueError("在提供的JSON结构中未能找到任何有效的字幕对象。")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    tasks_to_process = []
    for subtitle_obj in subtitle_objects:
        try:
            # 必须为每个字幕对象生成一个唯一的、确定的ID
            subtitle_id = subtitle_obj.get('id')
            if not subtitle_id:
                # 如果原始数据没有ID，我们可以基于内容和时间生成一个，但这有风险。
                # 更好的做法是要求输入数据必须有唯一ID。
                raise KeyError("字幕对象缺少 'id' 字段。")

            start_sec = float(subtitle_obj["start_time_seconds"])
            end_sec = float(subtitle_obj["end_time_seconds"])

            if end_sec <= start_sec:
                logger.warning(f"跳过ID {subtitle_id}的任务：无效时间范围 start={start_sec}, end={end_sec}")
                subtitle_obj['error'] = 'Invalid time range (end <= start)'
                subtitle_obj['audio_path'] = None
                continue
        except (KeyError, TypeError, ValueError) as e:
            error_msg = f'解析字幕对象时出错: {e}'
            logger.error(f"{error_msg} | 对象内容: {str(subtitle_obj)[:100]}...")
            subtitle_obj['error'] = error_msg
            subtitle_obj['audio_path'] = None
            continue

        safe_subtitle_id = re.sub(r'[\\/*?:"<>|]', "_", str(subtitle_id))
        audio_filename = f"audio_{safe_subtitle_id}.{AUDIO_FORMAT}"
        tasks_to_process.append({
            "id": subtitle_id,
            "start_sec": start_sec,
            "end_sec": end_sec,
            "original_subtitle_obj": subtitle_obj,  # 引用原始对象
            "local_path": os.path.join(audio_save_path, audio_filename),
            "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
        })

    logger.info(f"[{workflow_id}] JSON解析完成，共 {len(tasks_to_process)} 个有效任务待处理。提交到线程池。")

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


# ======================================================================================
# --- API 端点 (RESTful 风格) ---
# ======================================================================================

# --- Pydantic 输入模型 ---
class TTSRequestPayload(BaseModel):
    raw_script: Any = Field(..., description="包含字幕信息的原始JSON结构，可以是任意合法的JSON对象(dict或list)。")
    language: str = Field("zh", description="脚本语言, 例如 'zh', 'en'。")  # 提供默认值
    model_id: str = Field(..., description="使用的 TTS 模型ID。")
    workflow_id: str = Field(..., description="本次任务的唯一工作流ID。")


class RegenerateSinglePayload(BaseModel):
    subtitle_data: Dict[str, Any] = Field(..., description="要更新的单个字幕对象的完整数据。")
    model_id: str = Field(..., description="要使用的 TTS 模型ID。")


# --- API 路由实现 ---
@router.post("/generate_audio_json", summary="通过脚本JSON创建并生成全套音频")
async def create_and_generate_workflow(payload: TTSRequestPayload):
    """
    首次创建工作流并生成所有音频。
    此接口会先在临时目录生成文件，成功后再移动到最终位置。
    """
    async with api_semaphore:
        logger.info(f"获得并发许可, 开始处理 workflow_id: '{payload.workflow_id}' 的创建请求。")

        updated_script, move_error = await _process_workflow(
            workflow_id=payload.workflow_id,
            raw_script=payload.raw_script,
            model_id=payload.model_id
        )

        # --- 新增的多状态检查逻辑 ---
        all_subtitles = list(extract_subtitles_from_json(updated_script))

        # 统计成功和失败的数量
        total_tasks = len(all_subtitles)
        failed_tasks = sum(1 for sub in all_subtitles if "error" in sub and sub["error"])

        # 场景1: 全部成功
        if failed_tasks == 0 and not move_error:
            return create_standard_response(
                data=updated_script,
                code=status.HTTP_200_OK,
                message="All audio generated successfully."
            )

        # 场景2: 全部失败或有致命的移动错误
        elif failed_tasks == total_tasks or move_error:
            message = f"Workflow processing failed. Move error: {move_error}." if move_error else "All audio generation tasks failed."
            return create_standard_response(
                data=updated_script,
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=message
            )

        # 场景3: 部分成功，部分失败 (核心场景)
        else:  # 0 < failed_tasks < total_tasks
            return create_standard_response(
                data=updated_script,
                # 使用 207 Multi-Status
                code=status.HTTP_207_MULTI_STATUS,
                message=f"Workflow processing completed with partial success. {total_tasks - failed_tasks}/{total_tasks} tasks succeeded."
            )


@router.put("/audio/{workflow_id}", summary="重新生成整个工作流的所有音频文件")
async def regenerate_workflow_audio(workflow_id: str, payload: TTSRequestPayload):
    """
    通过提供新的 `raw_script` 和 `model_id`，重新生成指定 `workflow_id` 的所有音频文件。
    此操作会覆盖该工作流下的所有旧音频。
    """
    async with api_semaphore:
        logger.info(f"收到工作流 '{workflow_id}' 的批量重新生成请求。")

        # 【注意】确保传入的 workflow_id 一致
        if workflow_id != payload.workflow_id:
            return create_standard_response(
                code=status.HTTP_400_BAD_REQUEST,
                message=f"URL中的workflow_id '{workflow_id}' 与请求体中的 '{payload.workflow_id}' 不匹配。"
            )

        updated_script, move_error = await _process_workflow(
            workflow_id=workflow_id,
            raw_script=payload.raw_script,
            model_id=payload.model_id
        )

        # --- 新增的多状态检查逻辑 ---
        all_subtitles = list(extract_subtitles_from_json(updated_script))

        # 统计成功和失败的数量
        total_tasks = len(all_subtitles)
        failed_tasks = sum(1 for sub in all_subtitles if "error" in sub and sub["error"])

        # 场景1: 全部成功
        if failed_tasks == 0 and not move_error:
            return create_standard_response(
                data=updated_script,
                code=status.HTTP_200_OK,
                message="All audio generated successfully."
            )

        # 场景2: 全部失败或有致命的移动错误
        elif failed_tasks == total_tasks or move_error:
            message = f"Workflow processing failed. Move error: {move_error}." if move_error else "All audio generation tasks failed."
            return create_standard_response(
                data=updated_script,
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=message
            )

        # 场景3: 部分成功，部分失败 (核心场景)
        else:  # 0 < failed_tasks < total_tasks
            return create_standard_response(
                data=updated_script,
                # 使用 207 Multi-Status
                code=status.HTTP_207_MULTI_STATUS,
                message=f"Workflow processing completed with partial success. {total_tasks - failed_tasks}/{total_tasks} tasks succeeded."
            )


@router.put("/audio/{workflow_id}/{subtitle_id}", summary="重新生成并替换单个音频文件")
async def regenerate_single_audio(workflow_id: str, subtitle_id: str, payload: RegenerateSinglePayload):
    """
    根据提供的字幕数据，重新生成单个音频文件，并直接在最终目标位置替换掉旧文件。
    """
    async with api_semaphore:
        logger.info(f"收到为 workflow '{workflow_id}' 下的字幕ID '{subtitle_id}' 的单个重新生成请求。")

        if global_session_pool is None:
            return create_standard_response(
                code=status.HTTP_503_SERVICE_UNAVAILABLE,
                message="服务暂时不可用：TTS Session 池未初始化。"
            )

        subtitle_obj = payload.subtitle_data
        model_id = payload.model_id

        # 检查ID是否匹配
        if str(subtitle_obj.get('id')) != subtitle_id:
            return create_standard_response(
                code=status.HTTP_400_BAD_REQUEST,
                message=f"URL中的subtitle_id '{subtitle_id}' 与请求体中的 '{subtitle_obj.get('id')}' 不匹配。"
            )

        safe_subtitle_id = re.sub(r'[\\/*?:"<>|]', "_", str(subtitle_id))
        audio_filename = f"audio_{safe_subtitle_id}.{AUDIO_FORMAT}"

        # 【关键区别】单个文件更新直接操作最终目录，而不是临时目录
        dest_dir = os.path.join(FINAL_DEST_DIR, workflow_id, "audio")
        final_audio_path = os.path.join(dest_dir, audio_filename)

        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception as e:
            return create_standard_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=f"为 '{workflow_id}/{subtitle_id}' 创建目标目录 '{dest_dir}' 失败: {e}"
            )

        task_info = {
            "id": subtitle_id,
            "start_sec": subtitle_obj["start_time_seconds"],
            "end_sec": subtitle_obj["end_time_seconds"],
            "original_subtitle_obj": subtitle_obj,  # 引用
            "local_path": final_audio_path,  # 直接指向最终路径
            "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
        }

        try:
            loop = asyncio.get_running_loop()
            # 这里也使用线程池执行，保持一致性
            await loop.run_in_executor(
                tts_thread_pool,
                generate_audio_single_task,
                global_session_pool,
                task_info,
                model_id
            )
        except Exception as e:
            return create_standard_response(
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=f"为 '{workflow_id}/{subtitle_id}' 执行单个生成任务时发生未知错误: {e}"
            )

        # 检查任务执行结果并返回
        if subtitle_obj.get("error"):
            error_message = subtitle_obj["error"]
            return create_standard_response(
                data=subtitle_obj,
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message=f"Audio generation failed for subtitle '{subtitle_id}': {error_message}"
            )

        return create_standard_response(
            data=subtitle_obj,
            message=f"Audio for subtitle '{subtitle_id}' in workflow '{workflow_id}' regenerated successfully."
        )
