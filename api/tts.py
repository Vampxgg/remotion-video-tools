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
import sys  # V12 新增: 用于配置日志输出到标准输出
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


# ─── 生命周期资源管理（lifespan_resources）───────────────────────────
# 旧版 @router.on_event 已 deprecated，改由 main.py 在 FastAPI lifespan
# 中通过 AsyncExitStack 进入；行为/资源乘数完全一致。
import contextlib  # noqa: E402


def _startup_resources() -> None:
    global tts_thread_pool
    tts_thread_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS,
        thread_name_prefix="Global_TTS_Worker"
    )
    logger.info(f"全局共享TTS线程池已创建，最大工作线程数: {MAX_WORKERS}")


def _shutdown_resources() -> None:
    global tts_thread_pool
    if tts_thread_pool:
        logger.info("正在关闭全局共享TTS线程池...")
        tts_thread_pool.shutdown(wait=True)
        logger.info("全局共享TTS线程池已成功关闭。")


@contextlib.asynccontextmanager
async def lifespan_resources(app):
    _startup_resources()
    try:
        yield
    finally:
        _shutdown_resources()


from utils.settings import settings as _settings  # noqa: E402  (settings 单点入口)

# --- Clash 代理设置区 ---
PROXY_URL = _settings.TTS_PROXY_URL or _settings.OUTBOUND_PROXY_URL or ""
if PROXY_URL:
    os.environ['HTTP_PROXY'] = PROXY_URL
    os.environ['HTTPS_PROXY'] = PROXY_URL
    os.environ['http_proxy'] = PROXY_URL
    os.environ['https_proxy'] = PROXY_URL
    logger.info(f"已配置全局 HTTP/HTTPS 代理: {PROXY_URL}")
else:
    logger.info("未配置代理，将直接进行网络连接。")

# --- 配置区（默认值与历史硬编码一致；可通过 .env 中 TTS_* 覆盖）---
ENGINE_MODEL = _settings.TTS_ENGINE_MODEL
AUDIO_FORMAT = _settings.TTS_AUDIO_FORMAT
PUBLIC_URL_TEMPLATE = _settings.TTS_PUBLIC_URL_TEMPLATE
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SOURCE_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
AUDIO_SAVE_PATH_TEMPLATE = os.path.join(SOURCE_DIR_TEMPLATE, "audio")
DEST_BASE_DIR = _settings.TTS_DEST_BASE_DIR
MAX_WORKERS = _settings.TTS_MAX_WORKERS
MAX_RETRIES = _settings.TTS_MAX_RETRIES
RETRY_DELAY = _settings.TTS_RETRY_DELAY
TEXT_SPLIT_THRESHOLD = _settings.TTS_TEXT_SPLIT_THRESHOLD
SENTENCE_SPLIT_PATTERN = r"([。！？，、；…])"
ENABLE_DYNAMIC_SPEED_ADJUSTMENT = _settings.TTS_ENABLE_DYNAMIC_SPEED_ADJUSTMENT
SPEED_ADJUST_THRESHOLD_RATIO = _settings.TTS_SPEED_ADJUST_THRESHOLD_RATIO
MAX_SPEECH_SPEED = _settings.TTS_MAX_SPEECH_SPEED  # 注：下方"在线估算与回退策略"会再次覆盖该值
START_PADDING_BUFFER_MS = _settings.TTS_START_PADDING_BUFFER_MS

# ======================================================================================
# 在线估算与回退策略参数（根据字幕目标时长调整生成速度并可回退到高质量 time-stretch）
# 默认值与历史硬编码一致；可通过 TTS_* 覆盖
# ======================================================================================
CHARS_PER_SEC_ESTIMATE = _settings.TTS_CHARS_PER_SEC_ESTIMATE
CHARS_PER_SEC_ALPHA = _settings.TTS_CHARS_PER_SEC_ALPHA
MIN_SPEECH_SPEED = _settings.TTS_MIN_SPEECH_SPEED
MAX_SPEECH_SPEED = _settings.TTS_MAX_SPEECH_SPEED  # 这里在局部覆盖原值
LIBROSA_FALLBACK = _settings.TTS_LIBROSA_FALLBACK
# ======================================================================================

# 【改】尝试导入 librosa 与 soundfile 以支持高质量 time-stretch 回退（可选依赖）
try:
    import librosa  # type: ignore
    import soundfile as sf  # type: ignore
except Exception:
    librosa = None
    sf = None
    logger.info("librosa/soundfile 未安装：将禁用高质量 time-stretch 回退（可通过 pip install librosa soundfile 启用）")


# ======================================================================================


class IntelligentParser:
    def __init__(self, raw_script: str):
        self.script_lines = raw_script.splitlines()
        # 用于匹配 "X秒 - Y秒" 格式的时间戳，无论它前面有什么或后面跟什么。
        self.time_pattern = re.compile(r"(\d+\.?\d*\s*秒\s*-\s*\d+\.?\d*\s*秒)")
        # 用于精确提取 <subtitle> 标签内的内容。
        self.subtitle_tag_pattern = re.compile(r"<subtitle>(.*?)</subtitle>", re.DOTALL)

    def parse(self) -> List[Dict[str, str]]:
        logger.info("启动V5智能解析器 (基于 <subtitle> 标签的精确提取)...")

        results: List[Dict[str, str]] = []
        current_time_range: str | None = None

        for line in self.script_lines:
            # 步骤1: 检查当前行是否包含时间戳，并更新状态
            time_match = self.time_pattern.search(line)
            if time_match:
                current_time_range = time_match.group(1).strip()
                logger.debug(f"发现并更新时间戳: {current_time_range}")

            # 步骤2: 检查当前行是否包含 <subtitle> 标签
            subtitle_match = self.subtitle_tag_pattern.search(line)

            # 步骤3: 如果找到了 subtitle 标签，并且我们已经有了一个有效的时间戳
            if subtitle_match and current_time_range:
                # 提取标签内的文本内容
                extracted_text = subtitle_match.group(1).strip()

                # 检查以避免重复添加（虽然在此逻辑下不太可能发生）
                # 确保我们不会因为一个时间戳行恰好也有字幕而添加两次
                is_duplicate = False
                if results:
                    # 仅当文本和时间戳都与最后一个条目相同时，才视为重复
                    last_entry = results[-1]
                    if last_entry["time_range"] == current_time_range and last_entry["text"] == extracted_text:
                        is_duplicate = True

                if not is_duplicate:
                    logger.info(f"成功匹配 -> 时间: [{current_time_range}], 字幕: [{extracted_text[:30]}...]")
                    results.append({
                        "time_range": current_time_range,
                        "text": extracted_text
                    })

        if not results:
            logger.warning("在整个文档中未能找到任何有效的 '<subtitle>...</subtitle>' 标签与其对应的时间戳组合。")
        else:
            logger.info(f"解析完成，共提取 {len(results)} 条带 <subtitle> 标签的字幕。")

        return results


# --------------------------------------------------------------------------------------
# 【改】新增：辅助函数 —— clamp、估算所需 speed、以及用真实时长校准 chars/sec
# --------------------------------------------------------------------------------------
def clamp(value: float, lo: float, hi: float) -> float:  # 【改】
    return max(lo, min(hi, value))


def estimate_required_speed_for_text(text: str, target_duration_sec: float) -> float:  # 【改】
    """
    估算：若 TTS 的“自然时长”由 CHARS_PER_SEC_ESTIMATE 决定，
    需要的 speed = natural_duration / target_duration
    speed > 1 表示需要加快（生成更短时长），speed < 1 表示放慢。
    返回值已被 clamp 到 [MIN_SPEECH_SPEED, MAX_SPEECH_SPEED]
    """
    global CHARS_PER_SEC_ESTIMATE
    if target_duration_sec <= 0:
        return 1.0
    estimated_natural_dur = max(len(text) / CHARS_PER_SEC_ESTIMATE, 0.2)
    raw_speed = estimated_natural_dur / target_duration_sec
    return clamp(raw_speed, MIN_SPEECH_SPEED, MAX_SPEECH_SPEED)


def update_chars_per_sec_estimate(text: str, duration_ms: int) -> None:  # 【改】
    """
    使用实际生成结果对 CHARS_PER_SEC_ESTIMATE 做 EMA 校准。
    duration_ms 应为生成音频片段的毫秒数。
    """
    global CHARS_PER_SEC_ESTIMATE
    if duration_ms <= 0:
        return
    measured = len(text) / (duration_ms / 1000.0)
    CHARS_PER_SEC_ESTIMATE = CHARS_PER_SEC_ALPHA * measured + (1 - CHARS_PER_SEC_ALPHA) * CHARS_PER_SEC_ESTIMATE
    logger.debug(f"[chars/sec 校准] new_estimate={CHARS_PER_SEC_ESTIMATE:.3f} (measured={measured:.3f})")


# --------------------------------------------------------------------------------------


# --- 所有业务逻辑函数 (generate_audio_single_task, _move_workflow_directory等) 保持不变 ---
# (代码保持不变，下面对 generate_audio_single_task 做必要增强)
def _apply_silence_padding(audio_path: str, task_info: Dict[str, Any]) -> bool:
    try:
        target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
        if target_duration_sec <= 0: return True
        audio = AudioSegment.from_file(audio_path)
        actual_duration_ms = len(audio)
        target_duration_ms = int(target_duration_sec * 1000)
        padding_needed_ms = target_duration_ms - actual_duration_ms
        if padding_needed_ms <= 0:
            if padding_needed_ms < -50:
                logger.warning(
                    f"[Task {task_info['id']}] 内容溢出: 音频({actual_duration_ms}ms) > 目标({target_duration_ms}ms)")
            return True
        start_padding_ms = min(padding_needed_ms // 2, START_PADDING_BUFFER_MS)
        end_padding_ms = padding_needed_ms - start_padding_ms
        start_silence = AudioSegment.silent(duration=start_padding_ms)
        end_silence = AudioSegment.silent(duration=end_padding_ms)
        padded_audio = start_silence + audio + end_silence
        padded_audio.export(audio_path, format=AUDIO_FORMAT)
        logger.info(f"[Task {task_info['id']}] 成功精修填充: 开头 {start_padding_ms}ms, 结尾 {end_padding_ms}ms")
        return True
    except Exception as e:
        logger.error(f"[Task {task_info['id']}] 填充静音时发生错误: {e}")
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


# --------------------------------------------------------------------------------------
# 【改】增强版 generate_audio_single_task：先计算并传 prosody.speed，再回退到 librosa/time-stretch 或 pydub.speedup
# --------------------------------------------------------------------------------------
def generate_audio_single_task(session: Session, task_info: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    task_id, subtitle_text, full_audio_path = task_info["id"], task_info["text"], task_info["local_path"]
    logger.info(f"[Task {task_id}] 开始处理: '{subtitle_text[:30]}...'")
    temp_dir = os.path.dirname(full_audio_path)
    for attempt in range(MAX_RETRIES):
        try:
            text_chunks = _split_text_into_chunks(subtitle_text, TEXT_SPLIT_THRESHOLD)
            if len(text_chunks) > 1:
                logger.info(f"[Task {task_id}] 文本过长，已切分为 {len(text_chunks)} 段进行处理。")

            # 【改】计算整条 subtitle 目标时长并估算需要的 prosody speed（将该 speed 用于所有分片请求）
            target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
            desired_speed = 1.0
            if target_duration_sec > 0 and ENABLE_DYNAMIC_SPEED_ADJUSTMENT:
                desired_speed = estimate_required_speed_for_text(subtitle_text, target_duration_sec)
                logger.info(f"[Task {task_id}] 计算得到的 prosody speed: {desired_speed:.3f}")

            audio_segments = []
            for i, chunk_text in enumerate(text_chunks):
                temp_chunk_path = os.path.join(temp_dir, f"temp_{task_id}_{i}.{AUDIO_FORMAT}")
                try:
                    logger.debug(f"[Task {task_id}-{i}] 生成分片: '{chunk_text[:30]}...' (prosody={desired_speed:.3f})")
                    # 【改】将计算好的 speed 传给 prosody（若 SDK 支持）
                    req = TTSRequest(text=chunk_text, reference_id=model_id, model=ENGINE_MODEL, format=AUDIO_FORMAT,
                                     prosody=Prosody(speed=desired_speed))
                    with open(temp_chunk_path, "wb") as f:
                        for chunk in session.tts(req):
                            f.write(chunk)
                    if os.path.getsize(temp_chunk_path) == 0:
                        raise ValueError(f"生成的音频分片 {i} 为空文件。")
                    seg = AudioSegment.from_file(temp_chunk_path)
                    audio_segments.append(seg)

                    # 【改】使用该分片的真实时长校准 CHARS_PER_SEC_ESTIMATE（在线学习）
                    try:
                        update_chars_per_sec_estimate(chunk_text, len(seg))
                    except Exception as e:
                        logger.debug(f"[Task {task_id}-{i}] 校准 chars/sec 失败: {e}")

                finally:
                    if os.path.exists(temp_chunk_path):
                        os.remove(temp_chunk_path)

            if not audio_segments:
                raise ValueError("未能生成任何有效的音频分片。")
            combined_audio = sum(audio_segments, AudioSegment.empty())

            # 目标与实际时长比较
            actual_duration_ms = len(combined_audio)
            target_duration_ms = int(target_duration_sec * 1000) if target_duration_sec > 0 else None

            final_audio = combined_audio  # 默认为合并音频

            # 【改】如果生成的音频明显比目标长（并且我们已经把 prosody 设置到上限），尝试高质量回退
            if target_duration_ms and actual_duration_ms > target_duration_ms * SPEED_ADJUST_THRESHOLD_RATIO:
                logger.warning(
                    f"[Task {task_id}] 音频过长({actual_duration_ms}ms > {target_duration_ms}ms * {SPEED_ADJUST_THRESHOLD_RATIO}),"
                    f" desired_speed={desired_speed:.3f}"
                )
                # 如果 desired_speed 已经达到或接近我们允许的最大值，则说明在线生成无法进一步加速
                if desired_speed >= MAX_SPEECH_SPEED:
                    # 尝试 librosa 高质量 time-stretch 回退（保留音高）
                    if LIBROSA_FALLBACK and librosa is not None and sf is not None:
                        try:
                            tmp_wav = os.path.join(temp_dir, f"tsrc_{task_id}.wav")
                            combined_audio.export(tmp_wav, format="wav")
                            y, sr = librosa.load(tmp_wav, sr=None)
                            stretch_rate = actual_duration_ms / target_duration_ms
                            # librosa.effects.time_stretch：rate >1 表示加速（更短）
                            y_stretched = librosa.effects.time_stretch(y, rate=stretch_rate)
                            out_wav = os.path.join(temp_dir, f"tsout_{task_id}.wav")
                            sf.write(out_wav, y_stretched, sr)
                            final_audio = AudioSegment.from_file(out_wav, format="wav")
                            # 清理临时文件
                            if os.path.exists(tmp_wav):
                                os.remove(tmp_wav)
                            if os.path.exists(out_wav):
                                os.remove(out_wav)
                            logger.info(
                                f"[Task {task_id}] 已使用 librosa 进行高质量 time-stretch, factor={stretch_rate:.3f}")
                        except Exception as e:
                            logger.error(
                                f"[Task {task_id}] librosa time-stretch 失败: {e}. 将回退到 pydub.speedup 方案。")
                            # 回退到 pydub.speedup（尽管质量不如 librosa）
                            try:
                                ratio = actual_duration_ms / target_duration_ms
                                speedup_factor = min(ratio, MAX_SPEECH_SPEED)
                                final_audio = combined_audio.speedup(playback_speed=speedup_factor)
                                logger.info(f"[Task {task_id}] 使用 pydub.speedup, factor={speedup_factor:.3f}")
                            except Exception as e2:
                                logger.error(f"[Task {task_id}] pydub.speedup 也失败: {e2}. 使用原始音频并等待填充。")
                                final_audio = combined_audio
                    else:
                        # librosa 不可用，使用 pydub.speedup 回退（尽量保留）
                        try:
                            ratio = actual_duration_ms / target_duration_ms
                            speedup_factor = min(ratio, MAX_SPEECH_SPEED)
                            final_audio = combined_audio.speedup(playback_speed=speedup_factor)
                            logger.info(f"[Task {task_id}] 使用 pydub.speedup, factor={speedup_factor:.3f}")
                        except Exception as e:
                            logger.error(f"[Task {task_id}] pydub.speedup 失败: {e}. 使用原始音频并等待填充。")
                            final_audio = combined_audio
                else:
                    # desired_speed 未达到上限，说明 API 的 speed 参数已经生效但仍不足，尝试在本地微幅加速
                    try:
                        ratio = actual_duration_ms / target_duration_ms
                        speedup_factor = min(ratio, MAX_SPEECH_SPEED)
                        final_audio = combined_audio.speedup(playback_speed=speedup_factor)
                        logger.info(f"[Task {task_id}] local pydub.speedup 用于微调, factor={speedup_factor:.3f}")
                    except Exception as e:
                        logger.error(f"[Task {task_id}] pydub.speedup 失败: {e}. 使用原始音频并等待填充。")
                        final_audio = combined_audio
            else:
                final_audio = combined_audio

            # 导出最终音频并做静音填充
            final_audio.export(full_audio_path, format=AUDIO_FORMAT)
            padding_ok = _apply_silence_padding(full_audio_path, task_info)
            if not padding_ok:
                raise Exception("音频已生成，但最终静音填充失败。")
            task_info["audio_path"] = task_info["public_url"]
            logger.info(f"[Task {task_id}] 成功完成。")
            break
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


# --------------------------------------------------------------------------------------


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


@router.post("/tts", summary="从脚本生成音频并处理文件", response_model=Dict[str, Any])
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
            session.close()
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

# # 文件: api/tts_router.py
#
# from fastapi import APIRouter, HTTPException
# from pydantic import BaseModel, Field
# import re
# import os
# import logging
# from typing import List, Dict, Any
#
#
# # --- Pydantic 模型 (与之前相同) ---
# class TTSRequest(BaseModel):
#     raw_script: str = Field(..., description="包含时间和字幕的原始脚本字符串。")
#     workflow_id: str = Field(..., description="用于创建独立存储目录的工作流ID。")
#
#
# class AudioTask(BaseModel):
#     id: int
#     time_range: str
#     start_sec: float
#     end_sec: float
#     text: str
#     audio_path: str  # 这个字段现在将包含一个完整的 URL
#
#
# class TTSResponse(BaseModel):
#     audio_tasks: List[AudioTask]
#
#
# API_BASE_URL = os.getenv("API_BASE_URL", "http://119.45.167.133:2906").rstrip('/')
# logging.info(f"API Base URL set to: {API_BASE_URL}")
#
# # --- API 路由器设置 ---
# router = APIRouter()
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
#
#
# # --- 核心业务逻辑函数 (已更新) ---
# def process_and_generate_audio(raw_script: str, workflow_id: str) -> List[Dict[str, Any]]:
#     """
#     处理原始脚本，生成音频文件，并返回包含完整URL的任务列表。
#     """
#     # 更改路径：现在保存到项目根目录下的 'static' 文件夹内
#     # 这是相对路径，将被 FastAPI 的 StaticFiles 提供服务
#     relative_save_dir = os.path.join('static', 'file', workflow_id, 'audio')
#
#     # 为了让 os.makedirs 和 save_to_file 工作，我们需要一个绝对的文件系统路径
#     # os.path.abspath 将相对路径转换为绝对路径
#     absolute_save_path = os.path.abspath(relative_save_dir)
#
#     # --- 步骤 1: 初始化 TTS 引擎 ---
#     try:
#         import pyttsx3
#         engine = pyttsx3.init()
#     except Exception as e:
#         error_msg = f"错误: 初始化 pyttsx3 引擎失败。请确保依赖已安装。详情: {e}"
#         logging.error(error_msg, exc_info=True)
#         raise RuntimeError(error_msg)
#
#     # --- 步骤 2: 准备文件系统 ---
#     try:
#         # 使用绝对路径创建目录
#         os.makedirs(absolute_save_path, exist_ok=True)
#         logging.info(f"音频将保存到目录: {absolute_save_path}")
#     except Exception as e:
#         error_msg = f"创建目录 {absolute_save_path} 时发生错误: {e}"
#         logging.error(error_msg, exc_info=True)
#         raise IOError(error_msg)
#
#     # --- 步骤 3: 解析脚本并生成音频 ---
#     time_pattern = re.compile(r"^\s*(\d+(\.\d+)?秒\s*-\s*\d+(\.\d+)?秒)\s*$")
#     subtitle_pattern = re.compile(r"^\s*字幕:\s*(.*)")
#
#     tasks_list = []
#     lines = raw_script.splitlines()
#     current_timestamp = None
#     id_counter = 0
#
#     for line in lines:
#         line = line.strip()
#         if not line: continue
#
#         time_match = time_pattern.match(line)
#         if time_match:
#             current_timestamp = time_match.group(1)
#             continue
#
#         if current_timestamp:
#             subtitle_match = subtitle_pattern.match(line)
#             if subtitle_match:
#                 subtitle_text = subtitle_match.group(1).strip()
#                 if not subtitle_text: continue
#
#                 time_numbers = re.findall(r"\d+\.?\d*", current_timestamp)
#                 start_sec = float(time_numbers[0]) if time_numbers else 0.0
#                 end_sec = float(time_numbers[1]) if len(time_numbers) > 1 else 0.0
#
#                 audio_filename = f"audio_{id_counter}_{start_sec}s.mp3"
#
#                 # 1. 构建用于【保存文件】的绝对文件系统路径
#                 file_save_path = os.path.join(absolute_save_path, audio_filename)
#
#                 # 2. 构建用于【API返回】的公开URL
#                 # os.path.join 在 Windows 上会用 '\'，但 URL 需要 '/'
#                 # 所以我们用 .replace(os.sep, '/') 来确保跨平台兼容性
#                 url_path_part = os.path.join(relative_save_dir, audio_filename).replace(os.sep, '/')
#                 public_audio_url = f"{API_BASE_URL}/{url_path_part}"
#
#                 logging.info(f"队列任务 ID {id_counter}: 保存到 {file_save_path}, URL: {public_audio_url}")
#                 engine.save_to_file(subtitle_text, file_save_path)
#
#                 tasks_list.append({
#                     "id": id_counter,
#                     "time_range": current_timestamp,
#                     "start_sec": start_sec,
#                     "end_sec": end_sec,
#                     "text": subtitle_text,
#                     "audio_path": public_audio_url  # <-- 返回完整的公开URL
#                 })
#                 current_timestamp = None
#                 id_counter += 1
#
#     if tasks_list:
#         logging.info(f"开始执行 {len(tasks_list)} 个音频生成任务...")
#         engine.runAndWait()
#         logging.info("所有音频文件已成功生成。")
#     else:
#         logging.warning("未找到有效字幕，没有生成任何音频。")
#
#     return tasks_list
#
#
# # --- API 端点定义 (与之前类似，但现在内部逻辑已更新) ---
# @router.post(
#     "/generate-audio",
#     response_model=TTSResponse,
#     summary="从脚本生成音频文件并返回URL",
#     description="接收脚本和工作流ID，为字幕生成音频文件，并返回每个文件的可公开访问URL。"
# )
# def generate_audio_endpoint(request: TTSRequest):
#     if not request.raw_script.strip():
#         raise HTTPException(status_code=400, detail="输入脚本 `raw_script` 不能为空。")
#     try:
#         tasks = process_and_generate_audio(request.raw_script, request.workflow_id)
#         return {"audio_tasks": tasks}
#     except (RuntimeError, IOError) as e:
#         raise HTTPException(status_code=500, detail=f"服务器内部错误: {e}")
#     except Exception as e:
#         logging.error(f"处理请求时发生未知错误: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail=f"处理请求时发生未知错误: {str(e)}")
