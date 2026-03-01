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
import sys
from typing import Dict, List, Any, Tuple, Optional

# FastAPI 相关导入
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

# Fish Audio SDK 导入
from fish_audio_sdk import Session, TTSRequest, Prosody

# pydub 导入，用于音频处理
from pydub import AudioSegment
from pydub.exceptions import PydubException

# ======================================================================================
# --- [V12] 工业级日志配置 (替换原有的 basicConfig) ---
# ======================================================================================
# 1. 获取根 logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)  # 设置全局最低日志级别

# 2. 清除任何已存在的 handlers，避免重复输出
if logger.hasHandlers():
    logger.handlers.clear()

# 3. 创建一个 handler，用于将日志输出到标准输出 (控制台)
#    sys.stdout 确保日志流被立即处理，而不是被 uvicorn 缓冲
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setLevel(logging.INFO)  # 这个 handler 只处理 INFO 及以上级别的日志

# 4. 定义日志的格式
#    %(threadName)s 会显示执行该日志代码的线程名称
#    这对于调试并发问题至关重要
log_formatter = logging.Formatter(
    '%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s'
)
stream_handler.setFormatter(log_formatter)

# 5. 将 handler 添加到 logger
logger.addHandler(stream_handler)

logger.info("日志系统已配置为实时输出模式。")
# ======================================================================================

router = APIRouter()

# 全局唯一的线程池
tts_thread_pool: concurrent.futures.ThreadPoolExecutor = None


@router.on_event("startup")
def startup_event():
    """在应用启动时执行的函数"""
    global tts_thread_pool
    # 注意: MAX_WORKERS 在下方的配置区定义
    tts_thread_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS,
        thread_name_prefix="Global_TTS_Worker"
    )
    logger.info(f"全局共享TTS线程池已创建，最大工作线程数: {MAX_WORKERS}")


@router.on_event("shutdown")
def shutdown_event():
    """在应用关闭时执行的函数"""
    global tts_thread_pool
    if tts_thread_pool:
        logger.info("正在关闭全局共享TTS线程池...")
        tts_thread_pool.shutdown(wait=True)
        logger.info("全局共享TTS线程池已成功关闭。")


# --- Clash 代理设置区 ---
PROXY_URL = "http://127.0.0.1:7890"
if PROXY_URL:
    os.environ['HTTP_PROXY'] = PROXY_URL
    os.environ['HTTPS_PROXY'] = PROXY_URL
    os.environ['http_proxy'] = PROXY_URL
    os.environ['https_proxy'] = PROXY_URL
    logger.info(f"已配置全局 HTTP/HTTPS 代理: {PROXY_URL}")
else:
    logger.info("未配置代理，将直接进行网络连接。")

# --- 配置区 ---
ENGINE_MODEL = "speech-1.6"
AUDIO_FORMAT = "mp3"
PUBLIC_URL_TEMPLATE = "http://119.45.167.133:17752/meta-doc/video/{workflow_id}/audio/{filename}"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SOURCE_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
AUDIO_SAVE_PATH_TEMPLATE = os.path.join(SOURCE_DIR_TEMPLATE, "audio")
DEST_BASE_DIR = "E:\\Server\\x-pilot-oss\\uploads\\meta-doc\\video"
MAX_WORKERS = 5
MAX_RETRIES = 3
RETRY_DELAY = 2
TEXT_SPLIT_THRESHOLD = 60
SENTENCE_SPLIT_PATTERN = r"([。！？，、；…])"


# --- V13 智能解析器 ---
class IntelligentParser:

    def __init__(self, raw_script: str):
        self.script_lines = raw_script.splitlines()
        self.time_pattern = re.compile(r"(\d+\.?\d*\s*秒\s*-\s*\d+\.?\d*\s*秒)")
        self.subtitle_tag_pattern = re.compile(r"<subtitle>(.*?)</subtitle>", re.DOTALL)
        self.scene_header_pattern = re.compile(r"^\s*##")

    def parse(self) -> List[Dict[str, str]]:
        logger.info("启动V13智能解析器 (支持字幕与时间戳任意顺序，完全向后兼容)...")
        results: List[Dict[str, str]] = []

        current_subtitle_text: Optional[str] = None
        current_time_range: Optional[str] = None

        for line_num, line in enumerate(self.script_lines):
            if self.scene_header_pattern.search(line):
                if current_subtitle_text is not None or current_time_range is not None:
                    logger.warning(f"在行 {line_num} 进入新场景时，发现上一个场景有未成功配对的信息，将被丢弃。")

                logger.debug(f"检测到新场景 '{line.strip()}'，重置解析器状态。")
                current_subtitle_text = None
                current_time_range = None
                continue

            subtitle_match = self.subtitle_tag_pattern.search(line)
            if subtitle_match:
                if current_subtitle_text is not None:
                    logger.warning(f"在行 {line_num} 发现新字幕，但之前的字幕尚未配对。将覆盖为新字幕。")
                current_subtitle_text = subtitle_match.group(1).strip()
                log_text = f"'{current_subtitle_text[:30]}...'" if current_subtitle_text else "'[空字幕/静音]'"
                logger.debug(f"在行 {line_num} 捕获到字幕: {log_text}")

            time_match = self.time_pattern.search(line)
            if time_match:
                if current_time_range is not None:
                    logger.warning(f"在行 {line_num} 发现新时间戳，但之前的时间戳尚未配对。将覆盖为新时间戳。")
                current_time_range = time_match.group(1).strip()
                logger.debug(f"在行 {line_num} 捕获到时间戳: {current_time_range}")

            if current_subtitle_text is not None and current_time_range is not None:
                log_text = f"'{current_subtitle_text[:30]}...'" if current_subtitle_text else "'[空字幕/静音]'"
                logger.info(f"成功配对 -> 时间: [{current_time_range}], 字幕: {log_text}")

                is_duplicate = False
                if results and results[-1]["time_range"] == current_time_range and results[-1][
                    "text"] == current_subtitle_text:
                    is_duplicate = True
                    logger.warning(f"检测到与上一条完全相同的条目，已跳过。时间：{current_time_range}")

                if not is_duplicate:
                    results.append({
                        "time_range": current_time_range,
                        "text": current_subtitle_text
                    })

                current_subtitle_text = None
                current_time_range = None

        if not results:
            logger.warning("在整个文档中未能找到任何有效的 '<subtitle>' 与时间戳的配对组合。")
        else:
            logger.info(f"解析完成，共提取 {len(results)} 条字幕任务（包含静音任务）。")
        return results


# ======================================================================================
# --- [V14 新增] 智能语速计算核心工具函数 ---
# ======================================================================================

# 基于您大量测试得出的黄金标准
TARGET_CN_CHARS_PER_SECOND = 5.0
TARGET_EN_WORDS_PER_SECOND = 2.0
MIN_SPEECH_SPEED = 0.5  # 限制最低语速，防止过慢
MAX_SPEECH_SPEED = 2.0  # 限制最高语速，防止过快听不清


def _count_speech_units(text: str) -> Tuple[int, int]:
    """
    智能地计算文本中的语音单元。
    返回: (汉字数, 英文单词数)
    """
    chinese_chars = re.findall(r'[\u4e00-\u9fa5]', text)
    english_words = re.findall(r'\b[a-zA-Z]+\b', text)
    return len(chinese_chars), len(english_words)


def _calculate_optimal_speed(text: str, target_duration_sec: float) -> float:
    """
    根据文本内容和目标时长，计算出最佳的TTS speed参数。
    """
    if target_duration_sec <= 0 or not text.strip():
        return 1.0  # 默认语速

    num_cn, num_en = _count_speech_units(text)

    # 1. 根据黄金标准，估算在默认语速(1.0)下，读完这段文本需要的时间
    estimated_base_duration = (num_cn / TARGET_CN_CHARS_PER_SECOND) + \
                              (num_en / TARGET_EN_WORDS_PER_SECOND)

    if estimated_base_duration == 0:
        return 1.0  # 如果没有可发音内容，返回默认值

    # 2. 计算理想语速
    # speed = base_duration / target_duration
    # 例如：估算需要10秒，目标是5秒，则 speed = 10 / 5 = 2.0 (两倍速)
    # 例如：估算需要5秒，目标是10秒，则 speed = 5 / 10 = 0.5 (半速)
    calculated_speed = estimated_base_duration / target_duration_sec

    # 3. 将计算出的语速限制在合理范围内
    clamped_speed = max(MIN_SPEECH_SPEED, min(calculated_speed, MAX_SPEECH_SPEED))

    logger.info(
        f"智能语速计算: {num_cn}字, {num_en}词. "
        f"估算基础时长 {estimated_base_duration:.2f}s, 目标时长 {target_duration_sec:.2f}s. "
        f"计算速度 {calculated_speed:.2f}, 最终采用 {clamped_speed:.2f}"
    )

    return clamped_speed


# --- [V14 修改] 使用新的固定时长填充函数 ---
def _apply_fixed_padding(audio_path: str, task_id: int) -> bool:
    """
    为音频文件前后各添加1秒的固定静音。
    """
    try:
        padding_ms = 1000  # 1秒 = 1000毫秒
        audio = AudioSegment.from_file(audio_path)

        # 创建1秒的静音段
        one_second_silence = AudioSegment.silent(duration=padding_ms)

        # 将静音段拼接到原始音频前后
        padded_audio = one_second_silence + audio + one_second_silence

        # 导出覆盖原文件
        padded_audio.export(audio_path, format=AUDIO_FORMAT)

        logger.info(f"[Task {task_id}] 成功添加前后各1秒的静音填充。")
        return True
    except Exception as e:
        logger.error(f"[Task {task_id}] 添加固定静音填充时发生错误: {e}")
        return False


def _split_text_into_chunks(text: str, max_len: int) -> List[str]:
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


# --- [V14 核心重构] 自动化语速计算与固定填充 ---
def generate_audio_single_task(session: Session, task_info: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    task_id, subtitle_text, full_audio_path = task_info["id"], task_info["text"], task_info["local_path"]
    logger.info(f"[Task {task_id}] 开始处理: '{subtitle_text[:30] if subtitle_text else '[S I L E N C E]'}'")

    # --- 静音任务快速通道 (逻辑保持不变) ---
    if not subtitle_text.strip():
        try:
            target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
            if target_duration_sec <= 0:
                logger.warning(f"[Task {task_id}] 静音任务目标时长无效 ({target_duration_sec}s)，将生成一个空文件。")
                open(full_audio_path, 'a').close()
            else:
                target_duration_ms = int(target_duration_sec * 1000)
                logger.info(f"[Task {task_id}] 将在本地直接生成 {target_duration_ms}ms 的静音音频。")
                silence = AudioSegment.silent(duration=target_duration_ms)
                silence.export(full_audio_path, format=AUDIO_FORMAT)
            task_info["audio_path"] = task_info["public_url"]
            logger.info(f"[Task {task_id}] 本地静音生成成功。")
            task_info.pop("local_path", None);
            task_info.pop("public_url", None)
            return task_info
        except Exception as e:
            logger.error(f"[Task {task_id}] 在本地生成静音时发生致命错误: {e}")
            task_info["audio_path"] = None;
            task_info["error"] = f"Local silence generation failed: {str(e)}"
            task_info.pop("local_path", None);
            task_info.pop("public_url", None)
            return task_info

    # --- 带有TTS的主流程 ---
    temp_dir = os.path.dirname(full_audio_path)
    for attempt in range(MAX_RETRIES):
        try:
            # 1. [V14 新增] 计算目标时长，并为前后各1秒的填充留出空间
            total_target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
            # 纯语音内容的目标时长 = 总时长 - 2秒 (前后填充)
            speech_target_duration_sec = max(0.1, total_target_duration_sec - 2.0)

            # 2. [V14 新增] 调用新函数，自动计算最佳语速
            optimal_speed = _calculate_optimal_speed(subtitle_text, speech_target_duration_sec)

            text_chunks = _split_text_into_chunks(subtitle_text, TEXT_SPLIT_THRESHOLD)
            if len(text_chunks) > 1:
                logger.info(f"[Task {task_id}] 文本过长，已切分为 {len(text_chunks)} 段进行处理。")

            audio_segments = []
            for i, chunk_text in enumerate(text_chunks):
                temp_chunk_path = os.path.join(temp_dir, f"temp_{task_id}_{i}.{AUDIO_FORMAT}")
                try:
                    logger.debug(f"[Task {task_id}-{i}] 生成分片: '{chunk_text}'")
                    # 3. [V14 修改] 使用计算出的 optimal_speed
                    req = TTSRequest(
                        text=chunk_text,
                        reference_id=model_id,
                        model=ENGINE_MODEL,
                        format=AUDIO_FORMAT,
                        prosody=Prosody(speed=optimal_speed)  # <-- 使用动态语速
                    )
                    with open(temp_chunk_path, "wb") as f:
                        for chunk in session.tts(req):
                            f.write(chunk)
                    if os.path.getsize(temp_chunk_path) == 0:
                        raise ValueError(f"生成的音频分片 {i} 为空文件。")
                    audio_segments.append(AudioSegment.from_file(temp_chunk_path))
                finally:
                    if os.path.exists(temp_chunk_path):
                        os.remove(temp_chunk_path)

            if not audio_segments:
                raise ValueError("未能生成任何有效的音频分片。")

            combined_audio = sum(audio_segments, AudioSegment.empty())

            # 先导出纯语音部分
            combined_audio.export(full_audio_path, format=AUDIO_FORMAT)

            # 5. [V14 修改] 调用新的固定填充函数进行精修
            padding_ok = _apply_fixed_padding(full_audio_path, task_id)
            if not padding_ok:
                raise Exception("音频已生成，但最终固定静音填充失败。")

            # 检查最终音频的总时长是否严重超出时间范围
            final_audio = AudioSegment.from_file(full_audio_path)
            final_duration_ms = len(final_audio)
            target_total_ms = int(total_target_duration_sec * 1000)
            if final_duration_ms > target_total_ms * 1.1:  # 允许10%的误差
                logger.warning(
                    f"[Task {task_id}] 内容可能溢出: 最终音频({final_duration_ms}ms) > 目标总时长({target_total_ms}ms)"
                )

            task_info["audio_path"] = task_info["public_url"]
            logger.info(f"[Task {task_id}] TTS音频生成及精修成功。")
            break  # 成功完成，跳出重试循环

        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (2 ** attempt)
                logger.warning(
                    f"[Task {task_id}] TTS主流程第 {attempt + 1}/{MAX_RETRIES} 次尝试失败: {e}. 将在 {wait_time} 秒后重试...")
                time.sleep(wait_time)
            else:
                logger.error(f"[Task {task_id}] 所有重试均失败，最终错误: {e}")
                task_info["audio_path"] = None
                task_info["error"] = f"TTS generation failed after {MAX_RETRIES} retries: {str(e)}"
                break

    task_info.pop("local_path", None)
    task_info.pop("public_url", None)
    return task_info


def _move_workflow_directory(workflow_id: str):
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


class TTSRequestPayload(BaseModel):
    raw_script: str = Field(..., description="包含时间和字幕的原始脚本。")
    model_id: str = Field(..., description="使用的 TTS 模型ID。")
    workflow_id: str = Field(..., description="本次任务的唯一工作流ID。")
    fish_api_key: str = Field(..., description="Fish Audio 的 API Key。")


@router.post("/generate_audio", summary="从脚本生成音频并处理文件", response_model=Dict[str, Any])
def generate_audio_workflow(payload: TTSRequestPayload):
    raw_script, model_id, workflow_id, fish_api_key = payload.raw_script, payload.model_id, payload.workflow_id, payload.fish_api_key
    if not all([fish_api_key, model_id, workflow_id, raw_script]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="输入参数不完整。")

    logger.info(f"收到请求, workflow_id: '{workflow_id}'")
    audio_save_path = AUDIO_SAVE_PATH_TEMPLATE.format(workflow_id=workflow_id)
    try:
        os.makedirs(audio_save_path, exist_ok=True)
    except Exception as e:
        logger.error(f"创建目录失败: {audio_save_path}, 错误: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"创建工作目录失败: {e}")
    parser = IntelligentParser(raw_script)
    parsed_items = parser.parse()
    if not parsed_items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="智能解析失败：在脚本中未能识别出任何有效的字幕行。")
    tasks_to_process = []
    for id_counter, item in enumerate(parsed_items):
        current_timestamp = item["time_range"]
        time_numbers = re.findall(r"\d+\.?\d*", item["time_range"])
        start_sec = float(time_numbers[0]) if time_numbers else 0.0
        end_sec = float(time_numbers[1]) if len(time_numbers) > 1 else 0.0
        audio_filename = f"audio_{id_counter:03d}_{start_sec:.1f}s.{AUDIO_FORMAT}"
        tasks_to_process.append({
            "id": id_counter, "time_range": current_timestamp, "start_sec": start_sec, "end_sec": end_sec,
            "text": item["text"], "local_path": os.path.join(audio_save_path, audio_filename),
            "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
        })
    logger.info(f"智能解析完成，共 {len(tasks_to_process)} 个任务待处理。将任务提交到全局线程池。")
    final_tasks_list = []
    session = None
    try:
        session = Session(fish_api_key)
        executor = tts_thread_pool
        if executor is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="TTS服务尚未准备就绪，请稍后重试。")
        future_to_task = {executor.submit(generate_audio_single_task, session, task, model_id): task for task in
                          tasks_to_process}
        for future in concurrent.futures.as_completed(future_to_task):
            try:
                result = future.result()
                final_tasks_list.append(result)
            except Exception as exc:
                task = future_to_task[future]
                logger.error(f'任务 {task["id"]} 在线程池中执行时产生未捕获的异常: {exc}')
                task["audio_path"] = None
                task["error"] = f"Unhandled exception during task execution: {str(exc)}"
                task.pop("local_path", None)
                task.pop("public_url", None)
                final_tasks_list.append(task)
    except Exception as e:
        logger.error(f"并行处理任务时发生主错误: {e}")
        if "auth" in str(e).lower() or "apikey" in str(e).lower():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail=f"Fish Audio认证失败，请检查API Key: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"并行处理任务时发生主错误: {e}")
    finally:
        if session and hasattr(session, 'close'):
            session.close()  # 确保 session 被关闭

    final_tasks_list.sort(key=lambda x: x['id'])
    move_error = None
    if any(task.get("audio_path") for task in final_tasks_list):
        try:
            _move_workflow_directory(workflow_id)
        except (FileNotFoundError, IOError) as e:
            move_error = str(e)
            logger.error(f"文件移动操作失败: {e}")
    else:
        logger.warning("没有任何音频生成成功，跳过文件移动操作。")
    final_result = {
        "audio_tasks": final_tasks_list,
        "audio_tasks_str": json.dumps(final_tasks_list, ensure_ascii=False)
    }
    if move_error:
        final_result["move_operation_error"] = move_error
    return final_result
