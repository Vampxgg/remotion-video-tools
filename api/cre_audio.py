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
import io
import math
import asyncio
from asyncio import Semaphore
import threading
from typing import Dict, List, Any, Tuple, Optional
# FastAPI 相关导入
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

# Fish Audio SDK 导入
from fish_audio_sdk import Session, TTSRequest, Prosody

# pydub 导入，用于音频处理
import numpy as np
import pyrubberband
from pydub import AudioSegment
from pydub.exceptions import PydubException

# ======================================================================================
# --- [V12] 工业级日志配置 (替换原有的 basicConfig) ---
# ======================================================================================
# 工业级日志：utils.logger 是仓库内必需模块，删除冗余 fallback；导入失败应直接报错暴露问题
from utils.logger import setup_module_logger

logger = setup_module_logger(__name__, "logs/audio/fish.log")
# logger.setLevel(logging.INFO)  # 设置全局最低日志级别
#
# # 2. 清除任何已存在的 handlers，避免重复输出
# if logger.hasHandlers():
#     logger.handlers.clear()
#
# # 3. 创建一个 handler，用于将日志输出到标准输出 (控制台)
# #    sys.stdout 确保日志流被立即处理，而不是被 uvicorn 缓冲
# stream_handler = logging.StreamHandler(sys.stdout)
# stream_handler.setLevel(logging.INFO)  # 这个 handler 只处理 INFO 及以上级别的日志
#
# # 4. 定义日志的格式
# #    %(threadName)s 会显示执行该日志代码的线程名称
# #    这对于调试并发问题至关重要
# log_formatter = logging.Formatter(
#     '%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s'
# )
# stream_handler.setFormatter(log_formatter)
#
# # 5. 将 handler 添加到 logger
# logger.addHandler(stream_handler)
#
# logger.info("日志系统已配置为实时输出模式。")
# ======================================================================================

router = APIRouter()

# 全局唯一的线程池
tts_thread_pool: concurrent.futures.ThreadPoolExecutor = None
api_semaphore: asyncio.Semaphore = None
global_session_pool: queue.Queue = None
pool_init_lock = threading.Lock()
dir_creation_lock = threading.Lock()


# ─── 生命周期资源管理 ────────────────────────────────────────────────
# 旧版使用 @router.on_event("startup"/"shutdown")，FastAPI 已将其标记为 deprecated。
# 现在改为暴露 ``lifespan_resources(app)`` 异步上下文管理器，由 main.py 用
# AsyncExitStack 统一在 lifespan 内进入：每个 worker 进程仍会跑一遍，资源
# 乘数（×workers）保持与历史完全一致。
import contextlib  # noqa: E402


def _startup_resources() -> None:
    """与历史 startup_event() 行为完全等价。"""
    global tts_thread_pool
    global api_semaphore
    tts_thread_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS,
        thread_name_prefix="Global_TTS_Worker"
    )
    api_semaphore = Semaphore(_settings.CRE_AUDIO_API_SEMAPHORE)
    logger.info(f"全局共享TTS线程池已创建，最大工作线程数: {MAX_WORKERS}")
    logger.info(f"全局信号量已创建，许可数: {_settings.CRE_AUDIO_API_SEMAPHORE}")


def _shutdown_resources() -> None:
    """与历史 shutdown_event() 行为完全等价。"""
    global tts_thread_pool
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


@contextlib.asynccontextmanager
async def lifespan_resources(app):
    """供 main.py 在 FastAPI lifespan 中通过 AsyncExitStack 进入的统一入口。"""
    _startup_resources()
    try:
        yield
    finally:
        _shutdown_resources()


from utils.settings import settings as _settings  # noqa: E402  (settings 单点入口)

# --- Clash 代理设置区 ---
# 优先 router 自身的 CRE_AUDIO_PROXY_URL；未设置则回落到全局 OUTBOUND_PROXY_URL
PROXY_URL = _settings.CRE_AUDIO_PROXY_URL or _settings.OUTBOUND_PROXY_URL or ""
if PROXY_URL:
    os.environ['HTTP_PROXY'] = PROXY_URL
    os.environ['HTTPS_PROXY'] = PROXY_URL
    os.environ['http_proxy'] = PROXY_URL
    os.environ['https_proxy'] = PROXY_URL
    logger.info(f"已配置全局 HTTP/HTTPS 代理: {PROXY_URL}")
else:
    logger.info("未配置代理，将直接进行网络连接。")

# --- 配置区 (V9 更新) ---
# 默认值与历史硬编码完全一致；通过 .env 中 CRE_AUDIO_* 变量按需覆盖
ENGINE_MODEL = _settings.CRE_AUDIO_ENGINE_MODEL
AUDIO_FORMAT = _settings.CRE_AUDIO_AUDIO_FORMAT
PUBLIC_URL_TEMPLATE = _settings.CRE_AUDIO_PUBLIC_URL_TEMPLATE
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SOURCE_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
AUDIO_SAVE_PATH_TEMPLATE = os.path.join(SOURCE_DIR_TEMPLATE, "audio")
DEST_BASE_DIR = _settings.CRE_AUDIO_DEST_BASE_DIR
MAX_WORKERS = _settings.CRE_AUDIO_MAX_WORKERS
SESSION_POOL_SIZE = MAX_WORKERS
MAX_RETRIES = _settings.CRE_AUDIO_MAX_RETRIES
RETRY_DELAY = _settings.CRE_AUDIO_RETRY_DELAY
TEXT_SPLIT_THRESHOLD = _settings.CRE_AUDIO_TEXT_SPLIT_THRESHOLD
SENTENCE_SPLIT_PATTERN = r"([。！？，、；…])"
ENABLE_DYNAMIC_SPEED_ADJUSTMENT = _settings.CRE_AUDIO_ENABLE_DYNAMIC_SPEED_ADJUSTMENT
SPEED_ADJUST_THRESHOLD_RATIO = _settings.CRE_AUDIO_SPEED_ADJUST_THRESHOLD_RATIO
MAX_SPEECH_SPEED = _settings.CRE_AUDIO_MAX_SPEECH_SPEED
ENABLE_DYNAMIC_DECELERATION = _settings.CRE_AUDIO_ENABLE_DYNAMIC_DECELERATION
MIN_SPEECH_SPEED = _settings.CRE_AUDIO_MIN_SPEECH_SPEED  # (最慢不低于0.95倍速)
START_PADDING_BUFFER_MS = _settings.CRE_AUDIO_START_PADDING_BUFFER_MS
# 时间单位定义
TIME_KEYWORDS = {
    # 注意: 连接符中的横杠、波浪线等建议使用半角符号以保持一致性
    'zh': {'minute_keys': ['分'], 'second_keys': ['秒'], 'separator_keys': ['-', '到', '至', '~', '～']},
    'en': {'minute_keys': ['minute', 'minutes', 'min', 'm'], 'second_keys': ['second', 'seconds', 'sec', 's'],
           'separator_keys': ['-', 'to', '–', '—']},
    'de': {'minute_keys': ['minute', 'minuten', 'm'], 'second_keys': ['sekunde', 'sekunden', 'sek', 's'],
           'separator_keys': ['-', 'bis', '–']},  # 德语 (bis)
    'fr': {'minute_keys': ['minute', 'minutes', 'min', 'm'], 'second_keys': ['seconde', 'secondes', 'sec', 's'],
           'separator_keys': ['-', 'à', '–']},  # 法语 (à)
    'es': {'minute_keys': ['minuto', 'minutos', 'min', 'm'], 'second_keys': ['segundo', 'segundos', 'seg', 's'],
           'separator_keys': ['-', 'a', '–']},  # 西班牙语 (a)
    'ko': {'minute_keys': ['분'], 'second_keys': ['초'], 'separator_keys': ['-', '~', '～']},  # 韩文
    'ja': {'minute_keys': ['分'], 'second_keys': ['秒'], 'separator_keys': ['-', '~', '～', 'から']},  # 日语 (から)
    'ar': {'minute_keys': ['دقيقة', 'دقائق'], 'second_keys': ['ثانية', 'ثواني'],
           'separator_keys': ['-', ' إلى ', 'إلي']},  # 阿拉伯语 (إلى)
}


# ======================================================================================


class IntelligentParser:
    def __init__(self, raw_script: str):
        self.script_lines = raw_script.splitlines()
        # self.time_pattern = re.compile(r"((?:(?:\d+\s*分\s*)?\d+\.?\d*)\s*秒\s*-\s*(?:(?:\d+\s*分\s*)?\d+\.?\d*)\s*秒)")
        # time_value_pattern = r"(?:(?:\d+\s*分\s*)?\d+\.?\d*|\d+:\d+\.?\d*)\s*秒"

        # # 定义分钟单位，匹配 "分" 或 "m"，并且是可选的
        # minute_unit = r"(?:\s*(?:分|m)\s*)"
        # # 定义秒单位，匹配 "秒" 或 "s"
        # second_unit = r"(?:\s*(?:秒|s))"
        # 定义一个时间数值的模式，能匹配以下所有组合：
        # 1. ((\d+\.?\d*)\s*(?:分|m|:)\s*)?  --> 可选的分钟部分，捕获分钟数。
        #    - `(\d+\.?\d*)`  : 匹配并捕获分钟数字 (e.g., "1")
        #    - `\s*(?:分|m|:)\s*` : 匹配分钟单位 ("分", "m") 或分隔符 (":")
        #    - `?`             : 整个分钟部分是可选的
        # 2. (\d+\.?\d*)                      --> 必需的秒数部分，捕获秒数。
        #    - `(\d+\.?\d*)`  : 匹配并捕获秒数数字 (e.g., "26.5")
        # 3. (?:秒|s)                         --> 必需的秒单位。

        # 注意：上面的思路用于 _parse_time_to_seconds。对于 time_pattern，我们只需要匹配整个范围，不需要捕获内部。

        # 我们构建一个能匹配单个时间点描述的模式：
        # (?: ... ) 是非捕获组

        # 1. 收集所有语言的所有“秒”、“分”和“连接符”关键字
        all_second_keys = [re.escape(key) for lang_data in TIME_KEYWORDS.values() for key in lang_data['second_keys']]
        all_minute_keys = [re.escape(key) for lang_data in TIME_KEYWORDS.values() for key in lang_data['minute_keys']]
        all_separator_keys = [re.escape(key) for lang_data in TIME_KEYWORDS.values() for key in
                              lang_data['separator_keys']]

        # 2. 创建一个能匹配任何已知单位的模式 (优先匹配长关键字，避免歧义)
        second_units_pattern = '|'.join(sorted(list(set(all_second_keys)), key=len, reverse=True))
        minute_units_pattern = '|'.join(sorted(list(set(all_minute_keys)), key=len, reverse=True))
        separator_pattern = '|'.join(sorted(list(set(all_separator_keys)), key=len, reverse=True))
        # 3. 构建一个能匹配单个时间点描述的模式 (e.g., "1分20.5秒", "86.5s", "1m20.5s")
        time_point_pattern = (
                r"(?:"
                r"(?:\d+\.?\d*\s*(?:(?:" + minute_units_pattern + r")|:)\s*)?\d+\.?\d*"
                                                                  r")"
                                                                  r"\s*(?:" + second_units_pattern + r")"
        )
        # 4. 构建最终的时间范围匹配模式 "时间点 <任意连接符> 时间点"
        #    使用非捕获组 (?:...) 来包裹连接符，因为我们只想捕获整个时间范围
        self.time_pattern = re.compile(
            f"({time_point_pattern}\\s*(?:{separator_pattern})\\s*{time_point_pattern})"
        )
        # 这个正则将会在 parse 方法内部使用
        self.split_regex = re.compile(f'\\s*({separator_pattern})\\s*', re.UNICODE)
        self.subtitle_tag_pattern = re.compile(r"<subtitle>(.*?)</subtitle>", re.DOTALL)
        # 场景标题仍然可以作为可选的逻辑重置点，但在这个新设计中已不是必需
        self.scene_header_pattern = re.compile(r"^\s*##")

    def parse(self) -> List[Dict[str, str]]:
        logger.info("启动V15智能解析器 (基于邻近关系 + 时间线补全)...")

        logger.info("[阶段1/3] 正在使用邻近关系逻辑提取已定义的片段...")
        explicit_tasks = []
        lines = self.script_lines
        num_lines = len(lines)

        # # [核心] 使用一个集合来跟踪已经被成功匹配并使用的行的索引
        # used_line_indices = set()
        #
        # # 使用索引进行遍历，这是实现邻近检查的关键
        # for i, line in enumerate(lines):
        #     # 如果当前行已经被作为时间戳或字幕使用过了，则直接跳过
        #     if i in used_line_indices:
        #         continue
        #
        #     # 锚点：只对包含 <subtitle> 的行触发匹配逻辑
        #     subtitle_match = self.subtitle_tag_pattern.search(line)
        #     if subtitle_match:
        #         subtitle_text = subtitle_match.group(1).strip()
        #         time_range = None
        #         time_line_index = -1
        #
        #         # 步骤 1: 检查前一行 (i-1) 是否为有效且未使用的时间戳
        #         if i > 0 and (i - 1) not in used_line_indices:
        #             prev_line = lines[i - 1]
        #             time_match = self.time_pattern.search(prev_line)
        #             if time_match:
        #                 time_range = time_match.group(1).strip()
        #                 time_line_index = i - 1
        #                 logger.debug(f"在行 {i} 的字幕上方(行 {i - 1})找到时间戳: {time_range}")
        #
        #         # 步骤 2: 如果前一行没有，再检查后一行 (i+1) 是否为有效且未使用的时间戳
        #         if time_range is None and (i + 1) < num_lines and (i + 1) not in used_line_indices:
        #             next_line = lines[i + 1]
        #             time_match = self.time_pattern.search(next_line)
        #             if time_match:
        #                 time_range = time_match.group(1).strip()
        #                 time_line_index = i + 1
        #                 logger.debug(f"在行 {i} 的字幕下方(行 {i + 1})找到时间戳: {time_range}")
        #
        #         # 步骤 3: 如果成功找到了相邻的时间戳
        #         if time_range:
        #             log_text = f"'{subtitle_text[:30]}...'" if subtitle_text else "'[空字幕/静音]'"
        #             logger.info(f"成功配对 -> 字幕(行 {i}) 与 时间(行 {time_line_index}): [{time_range}], {log_text}")
        #
        #             explicit_tasks.append({
        #                 "time_range": time_range,
        #                 "text": subtitle_text
        #             })
        #
        #             # [至关重要] 将字幕行和时间戳行都标记为“已使用”，防止重复匹配
        #             used_line_indices.add(i)
        #             used_line_indices.add(time_line_index)
        #         else:
        #             # 如果字幕的邻居都不是时间戳，则它是一个孤立的字幕
        #             log_text = f"'{subtitle_text[:30]}...'" if subtitle_text else "'[空字幕/静音]'"
        #             logger.warning(f"在行 {i} 发现孤立字幕 {log_text}，其相邻行没有找到匹配的时间戳，已跳过。")
        # 【调整】: 核心逻辑变更，不再是检查 i-1 和 i+1
        # 我们将以字幕为锚点，在有限的范围内向下搜索时间戳
        # SEARCH_WINDOW 定义了从字幕行开始，向下搜索多少行来寻找时间戳
        SEARCH_WINDOW = 5

        for i, line in enumerate(lines):
            subtitle_match = self.subtitle_tag_pattern.search(line)
            if subtitle_match:
                subtitle_text = subtitle_match.group(1).strip()
                log_text = f"'{subtitle_text[:30]}...'" if subtitle_text else "'[空字幕/静音]'"
                logger.debug(f"在行 {i} 发现字幕: {log_text}")

                time_range = None
                time_line_index = -1

                # 【调整】: 在字幕行下方的 SEARCH_WINDOW 范围内搜索时间戳
                # search_end_index 确保我们不会超出文件末尾
                search_end_index = min(i + SEARCH_WINDOW, num_lines)

                for j in range(i + 1, search_end_index):
                    time_match = self.time_pattern.search(lines[j])
                    if time_match:
                        time_range = time_match.group(1).strip()
                        time_line_index = j
                        logger.debug(f"  -> 在行 {j} (字幕下方) 找到匹配的时间戳: {time_range}")
                        # 找到第一个就停止，避免一个字幕匹配多个时间
                        break

                        # 如果成功找到了时间戳
                if time_range:
                    logger.info(f"成功配对 -> 字幕(行 {i}) 与 时间(行 {time_line_index}): [{time_range}], {log_text}")
                    explicit_tasks.append({
                        "time_range": time_range,
                        "text": subtitle_text
                    })
                else:
                    logger.warning(
                        f"在行 {i} 发现字幕 {log_text}，但在其后 {SEARCH_WINDOW - 1} 行内未找到匹配的时间戳，已跳过。")

        if not explicit_tasks:
            logger.warning("在整个文档中未能找到任何有效的任务组合。将生成一个错误提示音作为返回。")
            error_message = (
                "Audio parsing failed."
                "Please check: 1. Is the language supported 【currently only English/Chinese is supported】"
                "2. Did the user specify in the custom request that subtitles are not required?"
                "3. Could not find any valid subtitle and timestamp combinations. Please check your script format and ensure that each <subtitle> tag is immediately followed by a valid time range, such as '5 seconds - 10 seconds'."
            )
            return [
                {
                    "time_range": "0.000秒 - 19.000秒",  # 给错误提示音一个合理的时长
                    "text": error_message
                }
            ]
        else:
            logger.info(f"解析完成，共提取 {len(explicit_tasks)} 条字幕任务。")
            # --- 步骤 2: 将提取的任务转换为带浮点数时间的结构，并排序 ---
            logger.info("[阶段2/3] 解析时间并按时间线排序...")

            timed_segments = []
            for task in explicit_tasks:
                try:
                    # 使用在 __init__ 中定义的 split_regex 进行分割
                    time_parts = self.split_regex.split(task["time_range"], maxsplit=1)
                    # re.split 会保留分隔符，结果是 [start, separator, end]
                    if len(time_parts) != 3:
                        raise ValueError(f"未能使用多语言分隔符正确分割时间范围。分割结果: {time_parts}")
                    start_str = time_parts[0].strip()
                    end_str = time_parts[2].strip()
                    start_sec = _parse_time_to_seconds(start_str)
                    end_sec = _parse_time_to_seconds(end_str)
                    if end_sec > start_sec:
                        timed_segments.append({
                            "start_sec": start_sec,
                            "end_sec": end_sec,
                            "text": task["text"]
                        })
                    else:
                        logger.warning(f"发现无效时间范围 '{task['time_range']}' (结束时间不大于开始时间)，已忽略。")
                except (ValueError, IndexError) as e:
                    logger.warning(f"解析时间范围 '{task['time_range']}' 失败，跳过此任务: {e}")

            if not timed_segments:
                logger.error("所有显式任务的时间范围都无法解析，无法构建时间线。将生成一个错误提示音。")
                error_message = ""
                return [
                    {
                        "time_range": "0.000秒 - 0.000秒",
                        "text": error_message
                    }
                ]

            # 按开始时间排序，这是构建完整时间线的关键
            sorted_segments = sorted(timed_segments, key=lambda x: x["start_sec"])

            # --- 步骤 3: 遍历排序后的片段，自动填充时间线上的静音间隙 ---
            logger.info("[阶段3/3] 构建完整时间线，自动填充静音间隙...")

            complete_timeline_tasks = []
            last_end_time = 0.0

            for segment in sorted_segments:
                current_start_time = segment["start_sec"]

                # 检查是否存在静音间隙
                gap_duration = current_start_time - last_end_time
                if gap_duration > 0.05:  # 忽略50ms以下的微小间隙
                    logger.info(
                        f"发现静音间隙: 从 {last_end_time:.3f}s 到 {current_start_time:.3f}s (时长: {gap_duration:.3f}s)")
                    complete_timeline_tasks.append({
                        "time_range": f"{last_end_time:.3f}秒 - {current_start_time:.3f}秒",
                        "text": ""  # 空文本代表静音
                    })

                # 添加当前的任务（无论是有声还是显式定义的静音）
                complete_timeline_tasks.append({
                    "time_range": f"{segment['start_sec']:.3f}秒 - {segment['end_sec']:.3f}秒",
                    "text": segment["text"]
                })

                last_end_time = segment["end_sec"]

            final_silent_tasks_count = sum(1 for task in complete_timeline_tasks if not task["text"])
            original_silent_tasks_count = sum(1 for task in explicit_tasks if not task["text"])
            auto_generated_silent_count = final_silent_tasks_count - original_silent_tasks_count

            logger.info(f"时间线补全完成！最终生成 {len(complete_timeline_tasks)} 个任务。")
            logger.info(f"(其中包含 {auto_generated_silent_count} 个自动生成的静音任务)")

            return complete_timeline_tasks


# class IntelligentParser:
#
#     def __init__(self, raw_script: str):
#         self.script_lines = raw_script.splitlines()
#         # 正则表达式保持不变
#         self.time_pattern = re.compile(r"(\d+\.?\d*\s*秒\s*-\s*\d+\.?\d*\s*秒)")
#         self.subtitle_tag_pattern = re.compile(r"<subtitle>(.*?)</subtitle>", re.DOTALL)
#         # [新增] 匹配场景标题 (如 "## 场景一...")，用作解析状态的重置锚点
#         self.scene_header_pattern = re.compile(r"^\s*##")
#
#     def parse(self) -> List[Dict[str, str]]:
#         logger.info("启动V13智能解析器 (支持字幕与时间戳任意顺序，完全向后兼容)...")
#         results: List[Dict[str, str]] = []
#
#         # --- [核心改动] ---
#         # 使用两个独立的状态变量来分别捕获最近的字幕和时间。
#         # 它们不再依赖于彼此的出现顺序。
#         current_subtitle_text: Optional[str] = None
#         current_time_range: Optional[str] = None
#
#         for line_num, line in enumerate(self.script_lines):
#             # 1. 检测是否进入一个新场景。如果是，则清空上一场景可能遗留的未配对信息。
#             #    这可以防止上一个场景末尾的孤立时间戳/字幕错误地与新场景的内容配对。
#             if self.scene_header_pattern.search(line):
#                 if current_subtitle_text is not None or current_time_range is not None:
#                     logger.warning(f"在行 {line_num} 进入新场景时，发现上一个场景有未成功配对的信息，将被丢弃。")
#
#                 logger.debug(f"检测到新场景 '{line.strip()}'，重置解析器状态。")
#                 current_subtitle_text = None
#                 current_time_range = None
#                 continue  # 处理下一行
#
#             # 2. 在当前行查找字幕
#             subtitle_match = self.subtitle_tag_pattern.search(line)
#             if subtitle_match:
#                 # 如果已经有一个未配对的字幕，这可能表示脚本格式有误，发出警告但仍使用新的。
#                 if current_subtitle_text is not None:
#                     logger.warning(f"在行 {line_num} 发现新字幕，但之前的字幕尚未配对。将覆盖为新字幕。")
#
#                 # 捕获字幕内容，strip()后即使是<subtitle></subtitle>也会得到空字符串 ""
#                 # 这完美兼容了您生成静音的逻辑。
#                 current_subtitle_text = subtitle_match.group(1).strip()
#                 log_text = f"'{current_subtitle_text[:30]}...'" if current_subtitle_text else "'[空字幕/静音]'"
#                 logger.debug(f"在行 {line_num} 捕获到字幕: {log_text}")
#
#             # 3. 在当前行查找时间戳
#             time_match = self.time_pattern.search(line)
#             if time_match:
#                 if current_time_range is not None:
#                     logger.warning(f"在行 {line_num} 发现新时间戳，但之前的时间戳尚未配对。将覆盖为新时间戳。")
#
#                 current_time_range = time_match.group(1).strip()
#                 logger.debug(f"在行 {line_num} 捕获到时间戳: {current_time_range}")
#
#             # 4. [关键配对逻辑] 只要字幕和时间戳两个条件都满足，就视为成功配对。
#             if current_subtitle_text is not None and current_time_range is not None:
#                 log_text = f"'{current_subtitle_text[:30]}...'" if current_subtitle_text else "'[空字幕/静音]'"
#                 logger.info(f"成功配对 -> 时间: [{current_time_range}], 字幕: {log_text}")
#
#                 # 检查重复。这里的逻辑与原版稍有不同，但目标一致：避免意外的重复条目。
#                 # 在新逻辑下，因状态立即重置，几乎不可能产生逻辑错误导致的重复。
#                 # 只有当脚本中明确写了完全相同的内容时才可能重复，这是正常的。
#                 is_duplicate = False
#                 if results and results[-1]["time_range"] == current_time_range and results[-1][
#                     "text"] == current_subtitle_text:
#                     is_duplicate = True
#                     logger.warning(f"检测到与上一条完全相同的条目，已跳过。时间：{current_time_range}")
#
#                 if not is_duplicate:
#                     results.append({
#                         "time_range": current_time_range,
#                         "text": current_subtitle_text
#                     })
#
#                 # [至关重要] 配对成功后，立即重置两个状态变量，准备捕获下一对。
#                 # 这确保了一个字幕或时间戳只会被使用一次。
#                 current_subtitle_text = None
#                 current_time_range = None
#
#         if not results:
#             logger.warning("在整个文档中未能找到任何有效的 '<subtitle>' 与时间戳的配对组合。")
#         else:
#             logger.info(f"解析完成，共提取 {len(results)} 条字幕任务（包含静音任务）。")
#         return results


# class IntelligentParser:
#
#     def __init__(self, raw_script: str):
#         self.script_lines = raw_script.splitlines()
#         self.time_pattern = re.compile(r"(\d+\.?\d*\s*秒\s*-\s*\d+\.?\d*\s*秒)")
#         self.subtitle_tag_pattern = re.compile(r"<subtitle>(.*?)</subtitle>", re.DOTALL)
#
#     def parse(self) -> List[Dict[str, str]]:
#         logger.info("启动V5.2智能解析器 (基于 <subtitle> 标签, 输出纯净静音信号)...")
#         results: List[Dict[str, str]] = []
#         current_time_range: str | None = None
#
#         for line in self.script_lines:
#             time_match = self.time_pattern.search(line)
#             if time_match:
#                 current_time_range = time_match.group(1).strip()
#                 logger.debug(f"发现并更新时间戳: {current_time_range}")
#
#             subtitle_match = self.subtitle_tag_pattern.search(line)
#             if subtitle_match and current_time_range:
#                 # --- [V5.2 核心改动] ---
#                 # 直接使用提取的文本，如果为空，就是空字符串 ""。不再使用任何占位符。
#                 extracted_text = subtitle_match.group(1).strip()
#                 # -------------------------
#
#                 log_msg = f"发现空字幕标签于时间 [{current_time_range}]，将标记为本地静音任务。" if not extracted_text else f"成功匹配 -> 时间: [{current_time_range}], 字幕: [{extracted_text[:30]}...]"
#                 logger.info(log_msg)
#
#                 is_duplicate = False
#                 if results and results[-1]["time_range"] == current_time_range and results[-1][
#                     "text"] == extracted_text:
#                     is_duplicate = True
#
#                 if not is_duplicate:
#                     results.append({"time_range": current_time_range, "text": extracted_text})
#
#         if not results:
#             logger.warning("在整个文档中未能找到任何有效的 '<subtitle>...</subtitle>' 标签与其对应的时间戳组合。")
#         else:
#             logger.info(f"解析完成，共提取 {len(results)} 条字幕任务（包含静音任务）。")
#
#         return results


# class IntelligentParser:
#     def __init__(self, raw_script: str):
#         self.script_lines = raw_script.splitlines()
#         # 用于匹配 "X秒 - Y秒" 格式的时间戳，无论它前面有什么或后面跟什么。
#         self.time_pattern = re.compile(r"(\d+\.?\d*\s*秒\s*-\s*\d+\.?\d*\s*秒)")
#         # 用于精确提取 <subtitle> 标签内的内容。
#         self.subtitle_tag_pattern = re.compile(r"<subtitle>(.*?)</subtitle>", re.DOTALL)
#
#     def parse(self) -> List[Dict[str, str]]:
#         logger.info("启动V5智能解析器 (基于 <subtitle> 标签的精确提取)...")
#
#         results: List[Dict[str, str]] = []
#         current_time_range: str | None = None
#
#         for line in self.script_lines:
#             # 步骤1: 检查当前行是否包含时间戳，并更新状态
#             time_match = self.time_pattern.search(line)
#             if time_match:
#                 current_time_range = time_match.group(1).strip()
#                 logger.debug(f"发现并更新时间戳: {current_time_range}")
#
#             # 步骤2: 检查当前行是否包含 <subtitle> 标签
#             subtitle_match = self.subtitle_tag_pattern.search(line)
#
#             # 步骤3: 如果找到了 subtitle 标签，并且我们已经有了一个有效的时间戳
#             if subtitle_match and current_time_range:
#                 # 提取标签内的文本内容
#                 extracted_text = subtitle_match.group(1).strip()
#
#                 # 检查以避免重复添加（虽然在此逻辑下不太可能发生）
#                 # 确保我们不会因为一个时间戳行恰好也有字幕而添加两次
#                 is_duplicate = False
#                 if results:
#                     # 仅当文本和时间戳都与最后一个条目相同时，才视为重复
#                     last_entry = results[-1]
#                     if last_entry["time_range"] == current_time_range and last_entry["text"] == extracted_text:
#                         is_duplicate = True
#
#                 if not is_duplicate:
#                     logger.info(f"成功匹配 -> 时间: [{current_time_range}], 字幕: [{extracted_text[:30]}...]")
#                     results.append({
#                         "time_range": current_time_range,
#                         "text": extracted_text
#                     })
#
#         if not results:
#             logger.warning("在整个文档中未能找到任何有效的 '<subtitle>...</subtitle>' 标签与其对应的时间戳组合。")
#         else:
#             logger.info(f"解析完成，共提取 {len(results)} 条带 <subtitle> 标签的字幕。")
#
#         return results


# --- 所有业务逻辑函数 (generate_audio_single_task, _move_workflow_directory等) 保持不变 ---
# (代码保持不变)


# def _apply_silence_padding(audio_path: str, task_info: Dict[str, Any]) -> bool:
#     try:
#         target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
#         if target_duration_sec <= 0: return True
#         audio = AudioSegment.from_file(audio_path)
#         actual_duration_ms = len(audio)
#         target_duration_ms = int(target_duration_sec * 1000)
#         padding_needed_ms = target_duration_ms - actual_duration_ms
#         if padding_needed_ms <= 0:
#             if padding_needed_ms < -50:
#                 logger.warning(
#                     f"[Task {task_info['id']}] 内容溢出: 音频({actual_duration_ms}ms) > 目标({target_duration_ms}ms)")
#             return True
#         start_padding_ms = min(padding_needed_ms // 2, START_PADDING_BUFFER_MS)
#         end_padding_ms = padding_needed_ms - start_padding_ms
#         start_silence = AudioSegment.silent(duration=start_padding_ms)
#         end_silence = AudioSegment.silent(duration=end_padding_ms)
#         padded_audio = start_silence + audio + end_silence
#         padded_audio.export(audio_path, format=AUDIO_FORMAT)
#         logger.info(f"[Task {task_info['id']}] 成功精修填充: 开头 {start_padding_ms}ms, 结尾 {end_padding_ms}ms")
#         return True
#     except Exception as e:
#         logger.error(f"[Task {task_info['id']}] 填充静音时发生错误: {e}")
#         return False

def _process_and_finalize_audio(
        audio: AudioSegment,
        task_info: Dict[str, Any]
) -> AudioSegment:
    """
    在内存中对音频进行最终处理，包括动态加速和精确静音填充。
    确保最终音频时长与目标时长完全一致（除非音频过长且不裁剪）。

    :param audio: 经过TTS生成的原始 pydub AudioSegment 对象。
    :param task_info: 包含目标时长等信息的任务字典。
    :return: 经过处理后的最终 AudioSegment 对象，可以直接导出。
    """
    task_id = task_info['id']
    target_duration_sec = task_info["end_sec"] - task_info["start_sec"]

    # 如果目标时长无效，直接返回原始音频
    if target_duration_sec <= 0:
        logger.warning(f"[Task {task_id}] 目标时长无效 ({target_duration_sec}s)，返回原始TTS音频。")
        return audio

    processed_audio = audio
    target_duration_ms = int(target_duration_sec * 1000)
    actual_duration_ms = len(processed_audio)

    # 情况 A: 音频过长，需要加速
    if ENABLE_DYNAMIC_SPEED_ADJUSTMENT and actual_duration_ms > target_duration_ms * SPEED_ADJUST_THRESHOLD_RATIO:
        ratio = actual_duration_ms / target_duration_ms
        # safe_ratio = math.ceil(ratio * 100) / 100
        # # 额外安全余量：在基础安全倍率上再增加 0.01，为 pydub 的不精确性提供缓冲
        # final_safe_ratio = safe_ratio + 0.05
        # new_speed = final_safe_ratio
        logger.warning(
            f"[Task {task_id}] 音频过长({actual_duration_ms}ms > 目标 {target_duration_ms}ms)，"
            f"将使用 pydub 进行本地加速，速度: {ratio:.2f}x。"
        )
        # processed_audio = processed_audio.speedup(playback_speed=new_speed)
        # logger.info(f"[Task {task_id}] 加速后音频时长: {len(processed_audio)}ms")

        # 平滑的声音高质量方案
        # 1. 从 pydub AudioSegment 获取原始音频数据 (numpy array) 和采样率
        samples = np.array(processed_audio.get_array_of_samples())
        sample_rate = processed_audio.frame_rate

        # 2. 使用 pyrubberband 进行时域拉伸
        # 注意：pyrubberband.time_stretch 的 speed 参数是“速度”，所以 > 1 是加速
        stretched_samples = pyrubberband.time_stretch(samples, sample_rate, ratio)

        # 3. 将处理后的 numpy array 转换回 pydub AudioSegment
        # pydub 需要的是 bytes, 我们需要正确地转换
        processed_audio = AudioSegment(
            stretched_samples.tobytes(),
            frame_rate=sample_rate,
            sample_width=processed_audio.sample_width,
            channels=processed_audio.channels
        )
        logger.info(f"[Task {task_id}] 高质量加速后音频时长: {len(processed_audio)}ms")

    # 情况 B: 音频过短，尝试智能减速
    elif ENABLE_DYNAMIC_DECELERATION and actual_duration_ms < target_duration_ms:
        # 计算所需的减速比率 (会小于1.0)
        calculated_ratio = actual_duration_ms / target_duration_ms

        # 只有在减速比率在我们设定的安全范围内时，才执行减速
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

    # --- 步骤 2: 精确时长对齐 (微调) ---
    # 重新获取处理后（可能已加速）的音频时长
    final_duration_ms = len(processed_audio)
    duration_diff_ms = target_duration_ms - final_duration_ms

    if duration_diff_ms > 0:
        # 情况A: 音频比目标短，需要精确填充静音
        # 智能分配填充：在开头加一小段缓冲，其余全部加到结尾
        start_padding_ms = min(duration_diff_ms, START_PADDING_BUFFER_MS)
        end_padding_ms = duration_diff_ms - start_padding_ms

        start_silence = AudioSegment.silent(duration=start_padding_ms)
        end_silence = AudioSegment.silent(duration=end_padding_ms)

        final_audio = start_silence + processed_audio + end_silence

        logger.info(
            f"[Task {task_id}] 成功填充静音以匹配目标时长。 "
            f"填充: {duration_diff_ms}ms (头:{start_padding_ms}ms, 尾:{end_padding_ms}ms)。"
            f"最终时长: {len(final_audio)}ms / 目标: {target_duration_ms}ms"
        )
        # 此时 len(final_audio) 应该严格等于 target_duration_ms
        return final_audio

    # elif duration_diff_ms < -10:  # 允许10ms的浮点计算误差
    #     # 情况B: 音频依然比目标长 (根据要求，不进行裁剪)
    #     logger.warning(
    #         f"[Task {task_id}] 内容溢出: 最终音频({final_duration_ms}ms) > 目标({target_duration_ms}ms)。"
    #         "根据要求，不进行裁剪。"
    #     )
    #     return processed_audio
    elif duration_diff_ms < 0:
        # 情况B: 音频依然比目标长，需要进行裁剪
        ms_to_crop = final_duration_ms - target_duration_ms
        logger.warning(
            f"[Task {task_id}] 内容溢出: 最终音频({final_duration_ms}ms) > 目标({target_duration_ms}ms)。"
            f"将从尾部裁剪 {ms_to_crop}ms 以强制匹配时长。"
        )
        # 使用切片操作裁剪音频，[:target_duration_ms] 表示从开头到目标时长
        faded_audio = processed_audio.fade_out(duration=10)
        final_audio = faded_audio[:target_duration_ms]

        # 确认裁剪后的时长
        if len(final_audio) != target_duration_ms:
            logger.error(f"[Task {task_id}] 裁剪后时长异常！期望 {target_duration_ms}ms, 实际 {len(final_audio)}ms")

        return final_audio

    else:
        # 情况C: 时长完美匹配或在误差范围内
        logger.info(f"[Task {task_id}] 音频时长({final_duration_ms}ms)已在目标({target_duration_ms}ms)范围内，无需调整。")
        return processed_audio


# def _parse_time_to_seconds(time_str: str) -> float:
#     """
#     将 "X分Y.Y秒" 或 "Y.Y秒" 格式的字符串转换为总秒数。
#     例如: "1分4.5秒" -> 64.5, "48.5秒" -> 48.5
#     """
#     total_seconds = 0.0
#     # 匹配分钟部分
#     min_match = re.search(r"(\d+)\s*分", time_str)
#     if min_match:
#         total_seconds += int(min_match.group(1)) * 60
#     # 匹配秒数部分
#     sec_match = re.search(r"(\d+\.?\d*)\s*秒", time_str)
#     if sec_match:
#         total_seconds += float(sec_match.group(1))
#     return total_seconds

def _parse_time_to_seconds(time_str: str) -> float:
    """
    [V17 多语言升级版]
    解析几乎所有在 TIME_KEYWORDS 中定义的分钟/秒组合格式的字符串，并转换为总秒数。
    它只处理单个时间点，如 "1分26.5秒"，而不是一个范围。
    """
    time_str = time_str.strip()

    # --- [V17 核心逻辑] ---
    # 1. 在函数内部动态构建所有语言的单位模式
    #    这确保了函数是自包含的，并且总是使用最新的配置
    all_second_keys = [re.escape(key) for lang_data in TIME_KEYWORDS.values() for key in lang_data['second_keys']]
    all_minute_keys = [re.escape(key) for lang_data in TIME_KEYWORDS.values() for key in lang_data['minute_keys']]

    # 优先匹配长关键字 (例如，匹配 "seconds" 而不是 "s")
    second_units_pattern = '|'.join(sorted(list(set(all_second_keys)), key=len, reverse=True))
    minute_units_pattern = '|'.join(sorted(list(set(all_minute_keys)), key=len, reverse=True))

    # 2. 创建一个能捕获分钟和秒数值的通用正则表达式
    #    - 捕获组 1 (可选): 分钟数值
    #    - 捕获组 2: 秒数值
    pattern = re.compile(
        # 匹配 "可选的分钟部分" + "必需的秒部分"
        # 例如: (?:(1)\s*(?:分|m|:|minute)\s*)? (30.5)\s*(?:秒|s|second)
        r"^(?:(\d+\.?\d*)\s*(?:(?:" + minute_units_pattern + r")|:))?\s*(\d+\.?\d*)\s*(?:" + second_units_pattern + r")$"
    )
    # --- [V17 逻辑结束] ---

    match = pattern.match(time_str)

    if not match:
        # 如果上面的模式不匹配，尝试作为纯数字或简单格式处理
        # 这是一种强大的容错机制
        try:
            # 移除所有已知的单位关键字后尝试转换
            cleaned_str = time_str
            # 使用集合操作提高效率
            all_keys = set(all_second_keys) | set(all_minute_keys)
            for key in all_keys:
                # re.escape() 会给 's' 加上反斜杠，所以这里要用原始 key
                original_key = re.sub(r'\\(.)', r'\1', key)
                cleaned_str = cleaned_str.replace(original_key, '')

            return float(cleaned_str.strip())
        except (ValueError, IndexError):
            logger.error(f"无法将时间字符串 '{time_str}' 解析为任何已知格式或纯数字。返回 0.0。")
            return 0.0

    try:
        minutes_str = match.group(1)
        seconds_str = match.group(2)  # 秒数部分总是存在于这个模式中

        total_seconds = 0.0

        if minutes_str:
            total_seconds += float(minutes_str) * 60

        if seconds_str:
            total_seconds += float(seconds_str)
        else:
            # 这是一个理论上的保护，因为我们的正则要求秒数必须存在
            logger.warning(f"在 '{time_str}' 中未能解析出秒数，这不应该发生。")
            return 0.0  # 如果没有秒数，时间是无效的

        return total_seconds

    except (ValueError, IndexError) as e:
        logger.error(f"解析时间字符串 '{time_str}' 时发生数值转换错误: {e}")
        return 0.0


# def _parse_time_to_seconds(time_str: str) -> float:
#     """
#     解析几乎所有合理的分钟/秒组合格式的字符串，并转换为总秒数。
#     支持的格式:
#     - "1分26.5秒", "1分26.5s"
#     - "1m26.5秒", "1m26.5s"
#     - "1:26.5秒", "1:26.5s"
#     - "86.5秒", "86.5s"
#     """
#     time_str = time_str.strip()
#
#     # 设计一个能捕获分钟和秒的正则表达式
#     # Group 1: ((\d+\.?\d*)\s*(?:分|m|:))?  -> 可选的分钟部分, 内部 Group 2 是分钟的数字
#     # Group 3: (\d+\.?\d*)                  -> 秒的数字
#     pattern = re.compile(
#         r"^(?:(\d+\.?\d*)\s*(?:分|m|:))?\s*(\d+\.?\d*)\s*(?:秒|s)$"
#     )
#
#     match = pattern.match(time_str)
#
#     if not match:
#         # 如果上面的模式不匹配，尝试匹配一个纯数字的模式（可能单位被外部逻辑剥离了）
#         try:
#             return float(time_str.replace('秒', '').replace('s', ''))
#         except (ValueError, IndexError):
#             logger.error(f"无法将时间字符串 '{time_str}' 解析为任何已知格式。")
#             return 0.0
#
#     try:
#         minutes_str = match.group(1)
#         seconds_str = match.group(2)
#
#         total_seconds = 0.0
#
#         if minutes_str:
#             total_seconds += float(minutes_str) * 60
#
#         if seconds_str:
#             total_seconds += float(seconds_str)
#
#         return total_seconds
#
#     except (ValueError, IndexError) as e:
#         logger.error(f"解析时间字符串 '{time_str}' 时发生数值转换错误: {e}")
#         return 0.0


# def _parse_time_to_seconds(time_str: str) -> float:
#     """
#     [健壮性升级]
#     将 "X分Y.Y秒", "Y.Y秒" 或 "M:S.S秒" 格式的字符串转换为总秒数。
#     例如:
#     - "1分4.5秒" -> 64.5
#     - "48.5秒" -> 48.5
#     - "1:26.5秒" -> 86.5
#     """
#     time_str = time_str.strip()
#     total_seconds = 0.0
#
#     # 优先处理 "分钟:秒" 格式
#     if ':' in time_str:
#         try:
#             parts = time_str.split(':')
#             minutes = int(parts[0])
#             # 从第二部分提取秒数
#             sec_match = re.search(r'(\d+\.?\d*)', parts[1])
#             if sec_match:
#                 seconds = float(sec_match.group(1))
#                 total_seconds = minutes * 60 + seconds
#             else:
#                 # 如果冒号后没有有效数字，则只计算分钟
#                 total_seconds = minutes * 60
#             return total_seconds
#         except (ValueError, IndexError) as e:
#             logger.error(f"解析 ' M:S ' 时间格式 '{time_str}' 失败: {e}")
#             return 0.0
#     else:
#         # 匹配分钟部分 (可选)
#         min_match = re.search(r"(\d+)\s*分", time_str)
#         if min_match:
#             total_seconds += int(min_match.group(1)) * 60
#         sec_match = re.search(r"(\d+\.?\d*)\s*秒", time_str)
#         if sec_match:
#             # 如果有分钟，就累加；如果没有，就直接赋值
#             if min_match:
#                 total_seconds += float(sec_match.group(1))
#             else:
#                 total_seconds = float(sec_match.group(1))
#
#     return total_seconds


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


# --- [V8.1核心函数] 升级后的核心工作函数 (增加静音快速通道) ---
def generate_audio_single_task(session_pool: queue.Queue, task_info: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    task_id, subtitle_text, full_audio_path = task_info["id"], task_info["text"], task_info["local_path"]

    # 1. 获取要操作的目录路径
    audio_save_path = os.path.dirname(full_audio_path)
    # 2. 在执行任何文件操作前，先确保目录存在，并用锁保护
    try:
        with dir_creation_lock:
            # 再次检查，因为在等待锁的过程中，其他线程可能已经创建了目录
            if not os.path.exists(audio_save_path):
                os.makedirs(audio_save_path, exist_ok=True)
                logger.debug(f"[Task {task_id}] 目录 {audio_save_path} 已创建。")
    except Exception as e:
        # 如果目录创建失败，任务无法继续，直接返回错误
        logger.error(f"[Task {task_id}] 在任务开始时创建目录 {audio_save_path} 失败: {e}")
        task_info["audio_path"] = None
        task_info["error"] = f"Failed to create directory: {e}"
        task_info.pop("local_path", None)
        task_info.pop("public_url", None)
        return task_info

    logger.info(f"[Task {task_id}] 开始处理: '{subtitle_text[:30] if subtitle_text else '[S I L E N C E]'}'")
    if not subtitle_text.strip():
        try:
            target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
            if target_duration_sec <= 0:
                logger.warning(f"[Task {task_id}] 目标时长无效 ({target_duration_sec}s)，将生成一个空文件。")
                # 创建一个空文件以表示占位
                open(full_audio_path, 'a').close()
            else:
                target_duration_ms = int(target_duration_sec * 1000)
                logger.info(f"[Task {task_id}] 检测到静音任务，将在本地直接生成 {target_duration_ms}ms 的静音音频。")
                # 使用 pydub 生成指定时长的静音
                silence = AudioSegment.silent(duration=target_duration_ms)
                silence.export(full_audio_path, format=AUDIO_FORMAT)

            # 更新任务信息并成功返回
            task_info["audio_path"] = task_info["public_url"]
            task_info.pop("local_path", None)
            task_info.pop("public_url", None)
            logger.info(f"[Task {task_id}] 本地静音生成成功。")
            return task_info
        except Exception as e:
            logger.error(f"[Task {task_id}] 在本地生成静音时发生致命错误: {e}")
            task_info["audio_path"] = None
            task_info["error"] = f"Local silence generation failed: {str(e)}"
            task_info.pop("local_path", None)
            task_info.pop("public_url", None)
            return task_info
        finally:
            task_info.pop("local_path", None)
            task_info.pop("public_url", None)
            return task_info
    session = None
    # session = Session(fish_api_key)
    try:
        session = session_pool.get(timeout=60)
        logger.debug(f"[Task {task_id}] 成功从池中获取 Session。")
        temp_dir = os.path.dirname(full_audio_path)
        for attempt in range(MAX_RETRIES):
            try:
                # ... 此处开始，是您提供的原有函数的完整逻辑，保持不变 ...
                text_chunks = _split_text_into_chunks(subtitle_text, TEXT_SPLIT_THRESHOLD)
                if len(text_chunks) > 1:
                    logger.info(f"[Task {task_id}] 文本过长，已切分为 {len(text_chunks)} 段进行处理。")
                audio_segments = []
                for i, chunk_text in enumerate(text_chunks):
                    temp_chunk_path = os.path.join(temp_dir, f"temp_{task_id}_{i}.{AUDIO_FORMAT}")
                    try:
                        logger.debug(f"[Task {task_id}-{i}] 生成分片: '{chunk_text}'")
                        req = TTSRequest(text=chunk_text, reference_id=model_id, model=ENGINE_MODEL,
                                         format=AUDIO_FORMAT,
                                         prosody=Prosody(speed=0.9))
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

                # 合并音频片段
                combined_audio = sum(audio_segments, AudioSegment.empty())
                # 精加工函数处理时长
                final_audio = _process_and_finalize_audio(combined_audio, task_info)

                # target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
                # if target_duration_sec > 0 and ENABLE_DYNAMIC_SPEED_ADJUSTMENT:
                #     actual_duration_ms = len(combined_audio)
                #     target_duration_ms = int(target_duration_sec * 1000)
                #     if actual_duration_ms > target_duration_ms * SPEED_ADJUST_THRESHOLD_RATIO:
                #         ratio = actual_duration_ms / target_duration_ms
                #         new_speed = min(ratio, MAX_SPEECH_SPEED)
                #         logger.warning(
                #             f"[Task {task_id}] 音频过长({actual_duration_ms}ms > {target_duration_ms}ms)，"
                #             f"将使用 pydub 进行本地加速，速度: {new_speed:.2f}x。"
                #         )
                #         final_audio = combined_audio.speedup(playback_speed=new_speed)
                #     else:
                #         final_audio = combined_audio
                # else:
                #     final_audio = combined_audio

                # 写到磁盘！
                final_audio.export(full_audio_path, format=AUDIO_FORMAT)

                # padding_ok = _apply_silence_padding(full_audio_path, task_info)
                # if not padding_ok:
                #     raise Exception("音频已生成，但最终静音填充失败。")

                task_info["audio_path"] = task_info["public_url"]
                logger.info(f"[Task {task_id}] TTS音频生成成功。")
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
                    break  # 所有重试用完，退出循环
    except queue.Empty:
        logger.error(f"[Task {task_id}] 等待60秒后仍无法从 Session 池中获取连接。")
        task_info["audio_path"] = None
        task_info["error"] = "Failed to get a session from the pool (timeout)."

    except Exception as e:
        # 捕获 get/put 之外的任何其他潜在错误
        logger.error(f"[Task {task_id}] 任务执行期间发生未处理的异常: {e}")
        task_info["audio_path"] = None
        task_info["error"] = f"Unhandled exception: {str(e)}"
    finally:
        # [关键] 无论任务成功与否，都必须将 Session 归还到池中
        if session:
            session_pool.put(session)
            logger.debug(f"[Task {task_id}] 已将 Session 归还到池中。")

        task_info.pop("local_path", None)
        task_info.pop("public_url", None)
    # finally:
    #     if session and hasattr(session, 'close'):
    #         session.close()
    #     task_info.pop("local_path", None)
    #     task_info.pop("public_url", None)
    # task_info.pop("local_path", None)
    # task_info.pop("public_url", None)
    return task_info


# def generate_audio_single_task(session: Session, task_info: Dict[str, Any], model_id: str) -> Dict[str, Any]:
#     task_id, subtitle_text, full_audio_path = task_info["id"], task_info["text"], task_info["local_path"]
#     logger.info(f"[Task {task_id}] 开始处理: '{subtitle_text[:30]}...'")
#     temp_dir = os.path.dirname(full_audio_path)
#     for attempt in range(MAX_RETRIES):
#         try:
#             text_chunks = _split_text_into_chunks(subtitle_text, TEXT_SPLIT_THRESHOLD)
#             if len(text_chunks) > 1:
#                 logger.info(f"[Task {task_id}] 文本过长，已切分为 {len(text_chunks)} 段进行处理。")
#             audio_segments = []
#             for i, chunk_text in enumerate(text_chunks):
#                 temp_chunk_path = os.path.join(temp_dir, f"temp_{task_id}_{i}.{AUDIO_FORMAT}")
#                 try:
#                     logger.debug(f"[Task {task_id}-{i}] 生成分片: '{chunk_text}'")
#                     req = TTSRequest(text=chunk_text, reference_id=model_id, model=ENGINE_MODEL, format=AUDIO_FORMAT,
#                                      prosody=Prosody(speed=0.9))
#                     with open(temp_chunk_path, "wb") as f:
#                         for chunk in session.tts(req):
#                             f.write(chunk)
#                     if os.path.getsize(temp_chunk_path) == 0:
#                         raise ValueError(f"生成的音频分片 {i} 为空文件。")
#                     audio_segments.append(AudioSegment.from_file(temp_chunk_path))
#                 finally:
#                     if os.path.exists(temp_chunk_path):
#                         os.remove(temp_chunk_path)
#             if not audio_segments:
#                 raise ValueError("未能生成任何有效的音频分片。")
#             combined_audio = sum(audio_segments, AudioSegment.empty())
#             target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
#             if target_duration_sec > 0 and ENABLE_DYNAMIC_SPEED_ADJUSTMENT:
#                 actual_duration_ms = len(combined_audio)
#                 target_duration_ms = int(target_duration_sec * 1000)
#                 if actual_duration_ms > target_duration_ms * SPEED_ADJUST_THRESHOLD_RATIO:
#                     ratio = actual_duration_ms / target_duration_ms
#                     new_speed = min(ratio, MAX_SPEECH_SPEED)
#                     logger.warning(
#                         f"[Task {task_id}] 音频过长({actual_duration_ms}ms > {target_duration_ms}ms)，"
#                         f"将使用 pydub 进行本地加速，速度: {new_speed:.2f}x。"
#                     )
#                     final_audio = combined_audio.speedup(playback_speed=new_speed)
#                 else:
#                     final_audio = combined_audio
#             else:
#                 final_audio = combined_audio
#             final_audio.export(full_audio_path, format=AUDIO_FORMAT)
#             padding_ok = _apply_silence_padding(full_audio_path, task_info)
#             if not padding_ok:
#                 raise Exception("音频已生成，但最终静音填充失败。")
#             task_info["audio_path"] = task_info["public_url"]
#             logger.info(f"[Task {task_id}] 成功完成。")
#             break
#         except Exception as e:
#             if attempt < MAX_RETRIES - 1:
#                 wait_time = RETRY_DELAY * (2 ** attempt)
#                 logger.warning(
#                     f"[Task {task_id}] TTS主流程第 {attempt + 1}/{MAX_RETRIES} 次尝试失败: {e}. 将在 {wait_time} 秒后重试...")
#                 time.sleep(wait_time)
#             else:
#                 logger.error(f"[Task {task_id}] 所有重试均失败，最终错误: {e}")
#                 task_info["audio_path"] = None
#                 task_info["error"] = f"TTS generation failed after {MAX_RETRIES} retries: {str(e)}"
#                 break
#     task_info.pop("local_path", None)
#     task_info.pop("public_url", None)
#     return task_info
#
# def run_tts_tasks_in_parallel(
#         session_pool: queue.Queue, tasks: List[Dict], model_id: str
# ) -> List[Dict]:
#     """
#     这是一个同步函数，它负责管理线程池的并发任务并收集结果。
#     """
#     final_results = []
#     # 注意：这里的 executor 是从全局变量获取的
#     executor = tts_thread_pool
#     if not executor:
#         # 在实践中，应该在API函数中处理这个错误，这里只是防御性编程
#         raise RuntimeError("Global TTS thread pool is not initialized.")
#
#     future_to_task = {
#         executor.submit(generate_audio_single_task, session_pool, task, model_id): task
#         for task in tasks
#     }
#     for future in concurrent.futures.as_completed(future_to_task):
#         try:
#             result = future.result()
#             final_results.append(result)
#         except Exception as exc:
#             task = future_to_task[future]
#             logger.error(f'任务 {task["id"]} 在线程池中执行时产生未捕获的异常: {exc}')
#             task["audio_path"] = None
#             task["error"] = f"Unhandled exception during task execution: {str(exc)}"
#             task.pop("local_path", None)
#             task.pop("public_url", None)
#             final_results.append(task)
#
#     return final_results


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
async def generate_audio_workflow(payload: TTSRequestPayload):
    async with api_semaphore:
        global global_session_pool
        logger.info(f"获得并发许可, 开始处理 workflow_id: '{payload.workflow_id}'")
        raw_script, model_id, workflow_id, fish_api_key = payload.raw_script, payload.model_id, payload.workflow_id, payload.fish_api_key
        if not all([fish_api_key, model_id, workflow_id, raw_script]):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="输入参数不完整。")
        logger.info(f"收到请求, workflow_id: '{workflow_id}'")

        if global_session_pool is None:
            with pool_init_lock:
                # 双重检查，防止多个线程在等待锁时重复创建
                if global_session_pool is None:
                    logger.info(f"检测到全局 Session 池未初始化，正在创建... (大小: {SESSION_POOL_SIZE})")
                    try:
                        # 创建一个新的队列作为池
                        new_pool = queue.Queue(maxsize=SESSION_POOL_SIZE)
                        # 使用第一个请求的 API Key 填充池
                        for i in range(SESSION_POOL_SIZE):
                            new_session = Session(fish_api_key)
                            new_pool.put(new_session)
                            logger.debug(f"已创建并放入第 {i + 1}/{SESSION_POOL_SIZE} 个 Session 到全局池中。")

                        global_session_pool = new_pool
                        logger.info("全局 Session 池成功创建并已缓存。")
                    except Exception as e:
                        logger.error(f"创建全局 Session 池失败，请检查首次请求的 API Key '...{fish_api_key[-4:]}': {e}")
                        # 初始化失败，重置为 None 以便下次重试
                        global_session_pool = None
                        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                            detail=f"创建 TTS Session 池失败，可能是 API Key 无效: {e}")
        # tts_session = None
        # with session_lock:  # 使用线程锁保护字典访问
        #     if fish_api_key in global_sessions:
        #         tts_session = global_sessions[fish_api_key]
        #         logger.info(f"为 API Key '...{fish_api_key[-4:]}' 复用已存在的 Session。")
        #     else:
        #         try:
        #             tts_session = Session(fish_api_key)
        #             global_sessions[fish_api_key] = tts_session
        #             logger.info(f"为 API Key '...{fish_api_key[-4:]}' 创建了新的共享 Session。")
        #         except Exception as e:
        #             logger.error(f"为 API Key '...{fish_api_key[-4:]}' 创建 Session 失败: {e}")
        #             raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
        #                                 detail=f"创建 TTS Session 失败，可能是 API Key 无效: {e}")

        audio_save_path = AUDIO_SAVE_PATH_TEMPLATE.format(workflow_id=workflow_id)
        # try:
        #     os.makedirs(audio_save_path, exist_ok=True)
        # except Exception as e:
        #     logger.error(f"创建目录失败: {audio_save_path}, 错误: {e}")
        #     raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"创建工作目录失败: {e}")
        parser = IntelligentParser(raw_script)
        parsed_items = parser.parse()
        if not parsed_items:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="智能解析失败：在脚本中未能识别出任何有效的字幕行。")

        # 动态构建用于分割的正则表达式，一次性完成
        all_separator_keys = [re.escape(key) for lang_data in TIME_KEYWORDS.values() for key in
                              lang_data['separator_keys']]
        separator_pattern_for_split = '|'.join(sorted(list(set(all_separator_keys)), key=len, reverse=True))
        split_regex = re.compile(f'\\s*({separator_pattern_for_split})\\s*')

        tasks_to_process = []
        for id_counter, item in enumerate(parsed_items):
            current_timestamp = item["time_range"]
            # time_numbers = re.findall(r"\d+\.?\d*", item["time_range"])
            # start_sec = float(time_numbers[0]) if time_numbers else 0.0
            # end_sec = float(time_numbers[1]) if len(time_numbers) > 1 else 0.0
            try:
                # time_parts = item["time_range"].split('-')
                time_parts = split_regex.split(item["time_range"], maxsplit=1)
                if len(time_parts) != 3:
                    raise ValueError("时间范围格式不正确，未能找到有效的分隔符。")

                start_str = time_parts[0].strip()
                end_str = time_parts[2].strip()

                start_sec = _parse_time_to_seconds(start_str)
                end_sec = _parse_time_to_seconds(end_str)

            except (ValueError, IndexError) as e:
                logger.error(f"解析时间范围 '{item['time_range']}' 时出错: {e}。跳过此任务。")
                continue
            audio_filename = f"audio_{id_counter:03d}_{start_sec:.1f}s.{AUDIO_FORMAT}"
            tasks_to_process.append({
                "id": id_counter, "time_range": current_timestamp, "start_sec": start_sec, "end_sec": end_sec,
                "text": item["text"], "local_path": os.path.join(audio_save_path, audio_filename),
                "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
            })
        logger.info(f"智能解析完成，共 {len(tasks_to_process)} 个任务待处理。将任务提交到全局线程池。")

        final_tasks_list = []
        # session = None
        try:
            # 1. 获取当前事件循环
            loop = asyncio.get_running_loop()
            # 2. 创建异步任务列表
            #    我们使用 asyncio.to_thread 将同步阻塞的函数 generate_audio_single_task
            #    包装成一个可以在事件循环中被 await 的协程。
            #    FastAPI (uvicorn) 会自动将这些协程调度到后台线程池中执行。
            async_tasks = [
                loop.run_in_executor(
                    tts_thread_pool,  # 明确指定使用我们的全局线程池
                    generate_audio_single_task,
                    global_session_pool,
                    task,
                    model_id
                )
                for task in tasks_to_process
            ]
            # 3. 并发执行所有异步任务并等待结果
            #    asyncio.gather 会并发运行所有任务，并按顺序返回结果。
            #    在等待期间，事件循环是自由的，可以处理其他网络请求。
            results = await asyncio.gather(*async_tasks)
            # 4. 收集结果
            final_tasks_list = list(results)

            # # 同步请求
            # # session = Session(fish_api_key)
            # executor = tts_thread_pool
            # if executor is None:
            #     raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            #                         detail="TTS服务尚未准备就绪，请稍后重试。")
            # future_to_task = {
            #     executor.submit(generate_audio_single_task, global_session_pool, task, model_id): task
            #     for task in tasks_to_process
            # }
            # for future in concurrent.futures.as_completed(future_to_task):
            #     try:
            #         result = future.result()
            #         final_tasks_list.append(result)
            #     except Exception as exc:
            #         task = future_to_task[future]
            #         logger.error(f'任务 {task["id"]} 在线程池中执行时产生未捕获的异常: {exc}')
            #         task["audio_path"] = None
            #         task["error"] = f"Unhandled exception during task execution: {str(exc)}"
            #         task.pop("local_path", None)
            #         task.pop("public_url", None)
            #         final_tasks_list.append(task)
        except Exception as e:
            logger.error(f"并行处理任务时发生主错误: {e}")
            if "auth" in str(e).lower() or "apikey" in str(e).lower():
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                    detail=f"Fish Audio认证失败，请检查API Key: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=f"并行处理任务时发生主错误: {e}")
        # finally:
        # if session and hasattr(session, 'close'):
        #     session.close()
        final_tasks_list.sort(key=lambda x: x['id'])
        move_error = None
        if any(task.get("audio_path") for task in final_tasks_list):
            try:
                # _move_workflow_directory(workflow_id)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _move_workflow_directory, workflow_id)
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

# #
# # # # -*- coding: utf-8 -*-
# # # # @File：main_app.py
# # # # @Time：2025/8/5 18:30 (V9 Refactored)
# # # # @Author：_不咬闰土的猹丶
# # # # @email：hx1561958968@gmail.com
# # #
# # # # --- 导入模块 ---
# # # import re
# # # import json
# # # import os
# # # import shutil
# # # import logging
# # # import concurrent.futures
# # # import time
# # # from typing import Dict, List, Any, Tuple
# # #
# # # # FastAPI 相关导入
# # # from fastapi import APIRouter, HTTPException, status
# # # from pydantic import BaseModel, Field
# # #
# # # # Fish Audio SDK 导入
# # # from fish_audio_sdk import Session, TTSRequest, Prosody
# # #
# # # # pydub 导入，用于音频处理
# # # from pydub import AudioSegment
# # # from pydub.exceptions import PydubException
# # #
# # # # --- 日志配置 ---
# # # logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# # # logger = logging.getLogger(__name__)
# # #
# # # # --- FastAPI 应用初始化 ---
# # # router = APIRouter()
# # #
# # # # --- Clash 代理设置区 ---
# # # # 提示: 如果问题持续存在，请尝试临时移除代理以排查是否为代理本身的问题。
# # # PROXY_URL = "http://127.0.0.1:7890"
# # # if PROXY_URL:
# # #     os.environ['HTTP_PROXY'] = PROXY_URL
# # #     os.environ['HTTPS_PROXY'] = PROXY_URL
# # #     os.environ['http_proxy'] = PROXY_URL
# # #     os.environ['https_proxy'] = PROXY_URL
# # #     logger.info(f"已配置全局 HTTP/HTTPS 代理: {PROXY_URL}")
# # # else:
# # #     logger.info("未配置代理，将直接进行网络连接。")
# # #
# # # # --- 配置区 (V9 更新) ---
# # # ENGINE_MODEL = "speech-1.6"
# # # AUDIO_FORMAT = "mp3"
# # # PUBLIC_URL_TEMPLATE = "http://119.45.167.133:17752/meta-doc/video/{workflow_id}/audio/{filename}"
# # # BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# # # STATIC_DIR = os.path.join(BASE_DIR, "static")
# # # SOURCE_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
# # # AUDIO_SAVE_PATH_TEMPLATE = os.path.join(SOURCE_DIR_TEMPLATE, "audio")
# # # DEST_BASE_DIR = "E:\\Server\\x-pilot-oss\\uploads\\meta-doc\\video"
# # # # 调整并发数，以更温和的方式请求API，避免触发速率限制
# # # MAX_WORKERS = 5
# # # # 增加重试次数，以提高对网络波动的容忍度
# # # MAX_RETRIES = 3
# # # RETRY_DELAY = 2
# # #
# # # # ======================================================================================
# # # # --- [V9核心配置] ---
# # # # 长文本分段阈值（字符数），超过这个长度就启用分段策略
# # # TEXT_SPLIT_THRESHOLD = 60
# # # # 用于切分句子的正则表达式，该表达式会保留分隔符
# # # SENTENCE_SPLIT_PATTERN = r"([。！？，、；…])"
# # # # 是否启用动态语速调整功能
# # # ENABLE_DYNAMIC_SPEED_ADJUSTMENT = True
# # # # 触发语速调整的阈值。例如1.05表示当音频时长超过目标时长的5%时，才启动加速。
# # # SPEED_ADJUST_THRESHOLD_RATIO = 1.05
# # # # 允许的最大语速，防止语速过快导致听感严重下降
# # # MAX_SPEECH_SPEED = 1.3
# # # # 开头静音缓冲，用于最终精修
# # # START_PADDING_BUFFER_MS = 150
# # #
# # #
# # # # ======================================================================================
# # #
# # # # --- V4 智能解析器类 (保持不变) ---
# # # class IntelligentParser:
# # #     def __init__(self, raw_script: str):
# # #         self.script_lines = raw_script.splitlines()
# # #         self.subtitle_pattern = re.compile(r"^\s*(\d+\.?\d*\s*秒\s*-\s*\d+\.?\d*\s*秒)\s*:\s*(.+)$")
# # #         self.title_pattern = re.compile(r"^(#+.*|【.*】|\*\*.*\*\*|##\s.*)$")
# # #         self.positive_keywords = {"字幕": 5, "脚本": 3, "script": 3, "subtitle": 5, "text": 2, "文本": 2}
# # #         self.negative_keywords = {"分镜": -4, "视频": -4, "画面": -4, "视觉": -4, "场景": -4, "video": -4, "visual": -4}
# # #
# # #     def _find_all_potential_subtitles(self) -> List[Tuple[int, Dict[str, str]]]:
# # #         potential_subs = []
# # #         for i, line in enumerate(self.script_lines):
# # #             match = self.subtitle_pattern.match(line.strip())
# # #             if match: potential_subs.append((i, {"time_range": match.group(1).strip(), "text": match.group(2).strip()}))
# # #         return potential_subs
# # #
# # #     def _cluster_subtitles(self, potential_subs: List[Tuple[int, Dict[str, str]]]) -> List[
# # #         List[Tuple[int, Dict[str, str]]]]:
# # #         if not potential_subs: return []
# # #         clusters, current_cluster = [], [potential_subs[0]]
# # #         for i in range(1, len(potential_subs)):
# # #             if potential_subs[i][0] - potential_subs[i - 1][0] <= 3:
# # #                 current_cluster.append(potential_subs[i])
# # #             else:
# # #                 clusters.append(current_cluster);
# # #                 current_cluster = [potential_subs[i]]
# # #         clusters.append(current_cluster);
# # #         return clusters
# # #
# # #     def _score_cluster(self, cluster: List[Tuple[int, Dict[str, str]]]) -> Tuple[int, List[Dict[str, str]]]:
# # #         start_line_num, score = cluster[0][0], len(cluster)
# # #         for i in range(start_line_num - 1, max(-1, start_line_num - 6), -1):
# # #             line = self.script_lines[i].lower().strip()
# # #             if self.title_pattern.match(line) or len(line) < 15:
# # #                 for keyword, value in {**self.positive_keywords, **self.negative_keywords}.items():
# # #                     if keyword in line: score += value
# # #                 break
# # #         return score, [item[1] for item in cluster]
# # #
# # #     def parse(self) -> List[Dict[str, str]]:
# # #         logger.info("启动V4智能解析器...")
# # #         potential_subs = self._find_all_potential_subtitles()
# # #         if not potential_subs: logger.warning("在整个文档中未能找到任何符合 '时间: 文本' 格式的行。"); return []
# # #         clusters = self._cluster_subtitles(potential_subs)
# # #         scored_clusters = [self._score_cluster(c) for c in clusters]
# # #         if not scored_clusters: return []
# # #         best_cluster = max(scored_clusters, key=lambda item: item[0])
# # #         if best_cluster[0] < 5 and len(potential_subs) > len(best_cluster[1]):
# # #             logger.warning(f"最佳区块得分({best_cluster[0]})过低，将采用降级策略返回所有匹配行。");
# # #             return [item[1] for item in potential_subs]
# # #         logger.info(f"决策：选择得分最高的区块（得分: {best_cluster[0]}）。");
# # #         return best_cluster[1]
# # #
# # #
# # # # --- V7 智能不对称填充函数 (保持不变, 作为最终打磨工具) ---
# # # def _apply_silence_padding(audio_path: str, task_info: Dict[str, Any]) -> bool:
# # #     try:
# # #         target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
# # #         if target_duration_sec <= 0: return True
# # #         audio = AudioSegment.from_file(audio_path)
# # #         actual_duration_ms = len(audio)
# # #         target_duration_ms = int(target_duration_sec * 1000)
# # #         padding_needed_ms = target_duration_ms - actual_duration_ms
# # #         if padding_needed_ms <= 0:
# # #             if padding_needed_ms < -50:  # 允许50ms的误差
# # #                 logger.warning(
# # #                     f"[Task {task_info['id']}] 内容溢出: 音频({actual_duration_ms}ms) > 目标({target_duration_ms}ms)")
# # #             return True
# # #         start_padding_ms = min(padding_needed_ms // 2, START_PADDING_BUFFER_MS)
# # #         end_padding_ms = padding_needed_ms - start_padding_ms
# # #         start_silence = AudioSegment.silent(duration=start_padding_ms)
# # #         end_silence = AudioSegment.silent(duration=end_padding_ms)
# # #         padded_audio = start_silence + audio + end_silence
# # #         padded_audio.export(audio_path, format=AUDIO_FORMAT)
# # #         logger.info(f"[Task {task_info['id']}] 成功精修填充: 开头 {start_padding_ms}ms, 结尾 {end_padding_ms}ms")
# # #         return True
# # #     except Exception as e:
# # #         logger.error(f"[Task {task_info['id']}] 填充静音时发生错误: {e}")
# # #         return False
# # #
# # #
# # # # --- [V9新增] 智能文本分块辅助函数 ---
# # # def _split_text_into_chunks(text: str, max_len: int) -> List[str]:
# # #     """
# # #     智能地将长文本切分为不超过 max_len 的小块，同时尽量保持句子完整性。
# # #     """
# # #     if len(text) <= max_len:
# # #         return [text]
# # #
# # #     parts = re.split(SENTENCE_SPLIT_PATTERN, text)
# # #     sentences = []
# # #     # 将文本和紧随其后的标点符号合并成一句话
# # #     for i in range(0, len(parts) - 1, 2):
# # #         sentence = parts[i] + (parts[i + 1] if i + 1 < len(parts) and parts[i + 1] else '')
# # #         sentences.append(sentence)
# # #     # 如果分割后最后一部分没有匹配到分隔符，则单独添加
# # #     if len(parts) % 2 == 1 and parts[-1]:
# # #         sentences.append(parts[-1])
# # #
# # #     chunks = []
# # #     current_chunk = ""
# # #     for sentence in sentences:
# # #         if not sentence.strip():
# # #             continue
# # #         if len(current_chunk) + len(sentence) <= max_len:
# # #             current_chunk += sentence
# # #         else:
# # #             if current_chunk:
# # #                 chunks.append(current_chunk)
# # #             # 如果单句本身就超长，则强制成为一个独立的块
# # #             if len(sentence) > max_len:
# # #                 if current_chunk:  # 确保前面的块被添加
# # #                     current_chunk = ""
# # #                 chunks.append(sentence)
# # #             else:
# # #                 current_chunk = sentence
# # #
# # #     if current_chunk:
# # #         chunks.append(current_chunk)
# # #
# # #     return chunks if chunks else [text]
# # #
# # #
# # # # ======================================================================================
# # # # --- [V9核心函数] 升级后的核心工作函数 ---
# # # # ======================================================================================
# # # def generate_audio_single_task(session: Session, task_info: Dict[str, Any], model_id: str) -> Dict[str, Any]:
# # #     task_id, subtitle_text, full_audio_path = task_info["id"], task_info["text"], task_info["local_path"]
# # #     logger.info(f"[Task {task_id}] 开始处理: '{subtitle_text[:30]}...'")
# # #
# # #     temp_dir = os.path.dirname(full_audio_path)
# # #
# # #     for attempt in range(MAX_RETRIES):
# # #         try:
# # #             # --- 阶段零：智能文本分段 (Smart Text Splitting) ---
# # #             text_chunks = _split_text_into_chunks(subtitle_text, TEXT_SPLIT_THRESHOLD)
# # #             if len(text_chunks) > 1:
# # #                 logger.info(f"[Task {task_id}] 文本过长，已切分为 {len(text_chunks)} 段进行处理。")
# # #
# # #             audio_segments = []
# # #             for i, chunk_text in enumerate(text_chunks):
# # #                 temp_chunk_path = os.path.join(temp_dir, f"temp_{task_id}_{i}.{AUDIO_FORMAT}")
# # #
# # #                 # --- 为每个分片生成音频，并确保临时文件被清理 ---
# # #                 try:
# # #                     logger.debug(f"[Task {task_id}-{i}] 生成分片: '{chunk_text}'")
# # #                     req = TTSRequest(text=chunk_text, reference_id=model_id, model=ENGINE_MODEL, format=AUDIO_FORMAT)
# # #                     with open(temp_chunk_path, "wb") as f:
# # #                         for chunk in session.tts(req):
# # #                             f.write(chunk)
# # #
# # #                     # 检查文件是否有效生成
# # #                     if os.path.getsize(temp_chunk_path) == 0:
# # #                         raise ValueError(f"生成的音频分片 {i} 为空文件。")
# # #
# # #                     audio_segments.append(AudioSegment.from_file(temp_chunk_path))
# # #                 finally:
# # #                     if os.path.exists(temp_chunk_path):
# # #                         os.remove(temp_chunk_path)
# # #
# # #             # --- 阶段一：合并音频 (Concatenate) ---
# # #             if not audio_segments:
# # #                 raise ValueError("未能生成任何有效的音频分片。")
# # #
# # #             combined_audio = sum(audio_segments, AudioSegment.empty())
# # #
# # #             # --- 阶段二：测量与决策 (Measure & Decide on combined audio) ---
# # #             target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
# # #             if target_duration_sec > 0 and ENABLE_DYNAMIC_SPEED_ADJUSTMENT:
# # #                 actual_duration_ms = len(combined_audio)
# # #                 target_duration_ms = int(target_duration_sec * 1000)
# # #
# # #                 if actual_duration_ms > target_duration_ms * SPEED_ADJUST_THRESHOLD_RATIO:
# # #                     # --- 阶段三：高效本地加速 (Efficient Local Speed-Up) ---
# # #                     ratio = actual_duration_ms / target_duration_ms
# # #                     new_speed = min(ratio, MAX_SPEECH_SPEED)
# # #
# # #                     logger.warning(
# # #                         f"[Task {task_id}] 音频过长({actual_duration_ms}ms > {target_duration_ms}ms)，"
# # #                         f"将使用 pydub 进行本地加速，速度: {new_speed:.2f}x。"
# # #                     )
# # #                     final_audio = combined_audio.speedup(playback_speed=new_speed)
# # #                 else:
# # #                     final_audio = combined_audio
# # #             else:
# # #                 final_audio = combined_audio
# # #
# # #             # 导出最终处理过的音频
# # #             final_audio.export(full_audio_path, format=AUDIO_FORMAT)
# # #
# # #             # --- 阶段四：最终精修填充 (Final Polish) ---
# # #             padding_ok = _apply_silence_padding(full_audio_path, task_info)
# # #             if not padding_ok:
# # #                 raise Exception("音频已生成，但最终静音填充失败。")
# # #
# # #             # 所有流程成功
# # #             task_info["audio_path"] = task_info["public_url"]
# # #             logger.info(f"[Task {task_id}] 成功完成。")
# # #             break  # 成功，跳出重试循环
# # #
# # #         except Exception as e:
# # #             if attempt < MAX_RETRIES - 1:
# # #                 wait_time = RETRY_DELAY * (2 ** attempt)
# # #                 logger.warning(
# # #                     f"[Task {task_id}] TTS主流程第 {attempt + 1}/{MAX_RETRIES} 次尝试失败: {e}. 将在 {wait_time} 秒后重试...")
# # #                 time.sleep(wait_time)
# # #             else:
# # #                 logger.error(f"[Task {task_id}] 所有重试均失败，最终错误: {e}")
# # #                 task_info["audio_path"] = None
# # #                 task_info["error"] = f"TTS generation failed after {MAX_RETRIES} retries: {str(e)}"
# # #                 break
# # #
# # #     task_info.pop("local_path", None)
# # #     task_info.pop("public_url", None)
# # #     return task_info
# # #
# # #
# # # # --- 内部辅助函数：移动目录 (保持不变) ---
# # # def _move_workflow_directory(workflow_id: str):
# # #     source_dir = SOURCE_DIR_TEMPLATE.format(workflow_id=workflow_id)
# # #     dest_path = os.path.join(DEST_BASE_DIR, workflow_id)
# # #     if not os.path.exists(source_dir):
# # #         raise FileNotFoundError(f"源目录 '{source_dir}' 不存在")
# # #     try:
# # #         os.makedirs(DEST_BASE_DIR, exist_ok=True)
# # #         if os.path.exists(dest_path):
# # #             shutil.rmtree(dest_path)
# # #         shutil.move(source_dir, DEST_BASE_DIR)
# # #         logger.info(f"成功移动文件夹 '{workflow_id}' 到 '{DEST_BASE_DIR}'")
# # #     except Exception as e:
# # #         raise IOError(f"移动文件夹时发生错误: {str(e)}")
# # #
# # #
# # # class TTSRequestPayload(BaseModel):
# # #     raw_script: str = Field(..., description="包含时间和字幕的原始脚本。")
# # #     model_id: str = Field(..., description="使用的 TTS 模型ID。")
# # #     workflow_id: str = Field(..., description="本次任务的唯一工作流ID。")
# # #     fish_api_key: str = Field(..., description="Fish Audio 的 API Key。")
# # #
# # #
# # # @router.post("/generate_audio", summary="从脚本生成音频并处理文件", response_model=Dict[str, Any])
# # # def generate_audio_workflow(payload: TTSRequestPayload):
# # #     raw_script, model_id, workflow_id, fish_api_key = payload.raw_script, payload.model_id, payload.workflow_id, payload.fish_api_key
# # #     if not all([fish_api_key, model_id, workflow_id, raw_script]):
# # #         raise HTTPException(
# # #             status_code=status.HTTP_400_BAD_REQUEST, detail="输入参数不完整。")
# # #
# # #     logger.info(f"收到请求, workflow_id: '{workflow_id}'")
# # #     audio_save_path = AUDIO_SAVE_PATH_TEMPLATE.format(workflow_id=workflow_id)
# # #     try:
# # #         os.makedirs(audio_save_path, exist_ok=True)
# # #     except Exception as e:
# # #         logger.error(f"创建目录失败: {audio_save_path}, 错误: {e}")
# # #         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"创建工作目录失败: {e}")
# # #
# # #     parser = IntelligentParser(raw_script)
# # #     parsed_items = parser.parse()
# # #     if not parsed_items:
# # #         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
# # #                             detail="智能解析失败：在脚本中未能识别出任何有效的字幕行。")
# # #
# # #     tasks_to_process = []
# # #     for id_counter, item in enumerate(parsed_items):
# # #         current_timestamp = item["time_range"]
# # #         time_numbers = re.findall(r"\d+\.?\d*", item["time_range"])
# # #         start_sec = float(time_numbers[0]) if time_numbers else 0.0
# # #         end_sec = float(time_numbers[1]) if len(time_numbers) > 1 else 0.0
# # #         audio_filename = f"audio_{id_counter:03d}_{start_sec:.1f}s.{AUDIO_FORMAT}"
# # #         tasks_to_process.append({
# # #             "id": id_counter, "time_range": current_timestamp, "start_sec": start_sec, "end_sec": end_sec,
# # #             "text": item["text"], "local_path": os.path.join(audio_save_path, audio_filename),
# # #             "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
# # #         })
# # #
# # #     logger.info(f"智能解析完成，共 {len(tasks_to_process)} 个任务待处理。")
# # #     final_tasks_list = []
# # #     session = None
# # #
# # #     try:
# # #         session = Session(fish_api_key)
# # #         # --- [ 修正区域结束 ] ---
# # #
# # #         with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS,
# # #                                                    thread_name_prefix="TTS_Worker") as executor:
# # #             future_to_task = {executor.submit(generate_audio_single_task, session, task, model_id): task for task in
# # #                               tasks_to_process}
# # #
# # #             for future in concurrent.futures.as_completed(future_to_task):
# # #                 try:
# # #                     result = future.result()
# # #                     final_tasks_list.append(result)
# # #                 except Exception as exc:
# # #                     task = future_to_task[future]
# # #                     logger.error(f'任务 {task["id"]} 在线程池中执行时产生未捕获的异常: {exc}')
# # #                     task["audio_path"] = None
# # #                     task["error"] = f"Unhandled exception during task execution: {str(exc)}"
# # #                     task.pop("local_path", None)
# # #                     task.pop("public_url", None)
# # #                     final_tasks_list.append(task)
# # #
# # #     except Exception as e:
# # #         # 这里的异常现在很可能就是初始化Session时API Key错误导致的
# # #         logger.error(f"并行处理任务时发生主错误: {e}")
# # #         # 如果是认证错误，返回401 Unauthorized 更合适
# # #         if "auth" in str(e).lower() or "apikey" in str(e).lower():
# # #             raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
# # #                                 detail=f"Fish Audio认证失败，请检查API Key: {e}")
# # #         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"并行处理任务时发生主错误: {e}")
# # #     finally:
# # #         # Session对象可能没有 close 方法，最好进行检查
# # #         if session and hasattr(session, 'close'):
# # #             session.close()
# # #
# # #     final_tasks_list.sort(key=lambda x: x['id'])
# # #     move_error = None
# # #     if any(task.get("audio_path") for task in final_tasks_list):
# # #         try:
# # #             _move_workflow_directory(workflow_id)
# # #         except (FileNotFoundError, IOError) as e:
# # #             move_error = str(e)
# # #             logger.error(f"文件移动操作失败: {e}")
# # #     else:
# # #         logger.warning("没有任何音频生成成功，跳过文件移动操作。")
# # #
# # #     final_result = {
# # #         "audio_tasks": final_tasks_list,
# # #         "audio_tasks_str": json.dumps(final_tasks_list, ensure_ascii=False)
# # #     }
# # #     if move_error:
# # #         final_result["move_operation_error"] = move_error
# # #
# # #     return final_result
# # #
# # # #
# # # # # -*- coding: utf-8 -*-
# # # # # @File：main_app.py
# # # # # @Time：2025/8/5 18:30
# # # # # @Author：_不咬闰土的猹丶
# # # # # @email：hx1561958968@gmail.com
# # # #
# # # # # --- 导入模块 ---
# # # # import re
# # # # import json
# # # # import os
# # # # import shutil
# # # # import logging
# # # # import concurrent.futures
# # # # import time
# # # # from typing import Dict, List, Any, Tuple
# # # #
# # # # # FastAPI 相关导入
# # # # from fastapi import APIRouter, HTTPException, status
# # # # from pydantic import BaseModel, Field
# # # #
# # # # # Fish Audio SDK 导入
# # # # from fish_audio_sdk import Session, TTSRequest, Prosody
# # # #
# # # # # pydub 导入，用于音频处理
# # # # from pydub import AudioSegment
# # # # from pydub.exceptions import PydubException
# # # #
# # # # logger = logging.getLogger(__name__)
# # # #
# # # # # --- FastAPI 应用初始化 ---
# # # # router = APIRouter()
# # # #
# # # # # --- Clash 代理设置区 (保持不变) ---
# # # # PROXY_URL = "http://127.0.0.1:7890"
# # # # if PROXY_URL:
# # # #     os.environ['HTTP_PROXY'] = PROXY_URL
# # # #     os.environ['HTTPS_PROXY'] = PROXY_URL
# # # #     os.environ['http_proxy'] = PROXY_URL
# # # #     os.environ['https_proxy'] = PROXY_URL
# # # #     logger.info(f"已配置全局 HTTP/HTTPS 代理: {PROXY_URL}")
# # # # else:
# # # #     logger.info("未配置代理，将直接进行网络连接。")
# # # #
# # # # # --- 配置区 (部分新增) ---
# # # # ENGINE_MODEL = "speech-1.6"
# # # # AUDIO_FORMAT = "mp3"
# # # # PUBLIC_URL_TEMPLATE = "http://119.45.167.133:17752/meta-doc/video/{workflow_id}/audio/{filename}"
# # # # BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# # # # STATIC_DIR = os.path.join(BASE_DIR, "static")
# # # # SOURCE_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
# # # # AUDIO_SAVE_PATH_TEMPLATE = os.path.join(SOURCE_DIR_TEMPLATE, "audio")
# # # # DEST_BASE_DIR = "E:\\Server\\x-pilot-oss\\uploads\\meta-doc\\video"
# # # # MAX_WORKERS = 5
# # # # MAX_RETRIES = 1  # 因为我们有内部重生成逻辑，外部重试次数可以减少
# # # # RETRY_DELAY = 2
# # # #
# # # # # ======================================================================================
# # # # # --- [V8核心配置] ---
# # # # # 是否启用动态语速调整功能
# # # # ENABLE_DYNAMIC_SPEED_ADJUSTMENT = True
# # # # # 触发语速调整的阈值。例如1.05表示当音频时长超过目标时长的5%时，才启动加速。
# # # # SPEED_ADJUST_THRESHOLD_RATIO = 1.05
# # # # # 允许的最大语速，防止语速过快导致听感严重下降
# # # # MAX_SPEECH_SPEED = 1.3
# # # # # V7版本的开头静音缓冲，依然保留用于最终精修
# # # # START_PADDING_BUFFER_MS = 150
# # # #
# # # #
# # # # # ======================================================================================
# # # #
# # # # # --- V4 智能解析器类 (保持不变) ---
# # # # class IntelligentParser:
# # # #     # ... 此处省略 IntelligentParser 内部实现细节，保持V7版本不变 ...
# # # #     def __init__(self, raw_script: str):
# # # #         self.script_lines = raw_script.splitlines()
# # # #         self.subtitle_pattern = re.compile(r"^\s*(\d+\.?\d*\s*秒\s*-\s*\d+\.?\d*\s*秒)\s*:\s*(.+)$")
# # # #         self.title_pattern = re.compile(r"^(#+.*|【.*】|\*\*.*\*\*|##\s.*)$")
# # # #         self.positive_keywords = {"字幕": 5, "脚本": 3, "script": 3, "subtitle": 5, "text": 2, "文本": 2}
# # # #         self.negative_keywords = {"分镜": -4, "视频": -4, "画面": -4, "视觉": -4, "场景": -4, "video": -4, "visual": -4}
# # # #
# # # #     def _find_all_potential_subtitles(self) -> List[Tuple[int, Dict[str, str]]]:
# # # #         potential_subs = []
# # # #         for i, line in enumerate(self.script_lines):
# # # #             match = self.subtitle_pattern.match(line.strip())
# # # #             if match: potential_subs.append((i, {"time_range": match.group(1).strip(), "text": match.group(2).strip()}))
# # # #         return potential_subs
# # # #
# # # #     def _cluster_subtitles(self, potential_subs: List[Tuple[int, Dict[str, str]]]) -> List[
# # # #         List[Tuple[int, Dict[str, str]]]]:
# # # #         if not potential_subs: return []
# # # #         clusters, current_cluster = [], [potential_subs[0]]
# # # #         for i in range(1, len(potential_subs)):
# # # #             if potential_subs[i][0] - potential_subs[i - 1][0] <= 3:
# # # #                 current_cluster.append(potential_subs[i])
# # # #             else:
# # # #                 clusters.append(current_cluster);
# # # #                 current_cluster = [potential_subs[i]]
# # # #         clusters.append(current_cluster);
# # # #         return clusters
# # # #
# # # #     def _score_cluster(self, cluster: List[Tuple[int, Dict[str, str]]]) -> Tuple[int, List[Dict[str, str]]]:
# # # #         start_line_num, score = cluster[0][0], len(cluster)
# # # #         for i in range(start_line_num - 1, max(-1, start_line_num - 6), -1):
# # # #             line = self.script_lines[i].lower().strip()
# # # #             if self.title_pattern.match(line) or len(line) < 15:
# # # #                 for keyword, value in {**self.positive_keywords, **self.negative_keywords}.items():
# # # #                     if keyword in line: score += value
# # # #                 break
# # # #         return score, [item[1] for item in cluster]
# # # #
# # # #     def parse(self) -> List[Dict[str, str]]:
# # # #         logger.info("启动V4智能解析器...")
# # # #         potential_subs = self._find_all_potential_subtitles()
# # # #         if not potential_subs: logger.warning("在整个文档中未能找到任何符合 '时间: 文本' 格式的行。"); return []
# # # #         clusters = self._cluster_subtitles(potential_subs)
# # # #         scored_clusters = [self._score_cluster(c) for c in clusters]
# # # #         if not scored_clusters: return []
# # # #         best_cluster = max(scored_clusters, key=lambda item: item[0])
# # # #         if best_cluster[0] < 5 and len(potential_subs) > len(best_cluster[1]):
# # # #             logger.warning(f"最佳区块得分({best_cluster[0]})过低，将采用降级策略返回所有匹配行。");
# # # #             return [item[1] for item in potential_subs]
# # # #         logger.info(f"决策：选择得分最高的区块（得分: {best_cluster[0]}）。");
# # # #         return best_cluster[1]
# # # #
# # # #
# # # # # --- V7 智能不对称填充函数 (保持不变, 作为最终打磨工具) ---
# # # # def _apply_silence_padding(audio_path: str, task_info: Dict[str, Any]) -> bool:
# # # #     # ... 此函数内部实现与 V7 版本完全相同，这里不再重复 ...
# # # #     try:
# # # #         target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
# # # #         if target_duration_sec <= 0: return True
# # # #         audio = AudioSegment.from_file(audio_path)
# # # #         actual_duration_ms = len(audio)
# # # #         target_duration_ms = int(target_duration_sec * 1000)
# # # #         padding_needed_ms = target_duration_ms - actual_duration_ms
# # # #         if padding_needed_ms <= 0:
# # # #             if padding_needed_ms < -50:  # 允许50ms的误差
# # # #                 logger.warning(
# # # #                     f"[Task {task_info['id']}] 内容溢出: 音频({actual_duration_ms}ms) > 目标({target_duration_ms}ms)")
# # # #             return True
# # # #         start_padding_ms = min(padding_needed_ms // 2, START_PADDING_BUFFER_MS)
# # # #         end_padding_ms = padding_needed_ms - start_padding_ms
# # # #         start_silence = AudioSegment.silent(duration=start_padding_ms)
# # # #         end_silence = AudioSegment.silent(duration=end_padding_ms)
# # # #         padded_audio = start_silence + audio + end_silence
# # # #         padded_audio.export(audio_path, format=AUDIO_FORMAT)
# # # #         logger.info(f"[Task {task_info['id']}] 成功精修填充: 开头 {start_padding_ms}ms, 结尾 {end_padding_ms}ms")
# # # #         return True
# # # #     except Exception as e:
# # # #         logger.error(f"[Task {task_info['id']}] 填充静音时发生错误: {e}")
# # # #         return False
# # # #
# # # #
# # # # # ======================================================================================
# # # # # --- [V8核心函数] 升级后的核心工作函数 ---
# # # # # ======================================================================================
# # # # def generate_audio_single_task(session: Session, task_info: Dict[str, Any], model_id: str) -> Dict[str, Any]:
# # # #     task_id, subtitle_text, full_audio_path = task_info["id"], task_info["text"], task_info["local_path"]
# # # #     logger.info(f"[Task {task_id}] 开始处理: '{subtitle_text[:30]}...'")
# # # #
# # # #     for attempt in range(MAX_RETRIES):
# # # #         try:
# # # #             # --- 阶段一：标准速度生成 (First Pass) ---
# # # #             logger.info(f"[Task {task_id}] 尝试标准速度(1.0)生成...")
# # # #             req = TTSRequest(text=subtitle_text, reference_id=model_id, model=ENGINE_MODEL, format=AUDIO_FORMAT)
# # # #             with open(full_audio_path, "wb") as f:
# # # #                 for chunk in session.tts(req):
# # # #                     f.write(chunk)
# # # #
# # # #             # --- 阶段二：测量与决策 (Measure & Decide) ---
# # # #             target_duration_sec = task_info["end_sec"] - task_info["start_sec"]
# # # #             if target_duration_sec <= 0:
# # # #                 logger.warning(f"[Task {task_id}] 目标时长无效，跳过速度调整和填充。")
# # # #                 break  # 直接进入最终处理
# # # #
# # # #             try:
# # # #                 audio = AudioSegment.from_file(full_audio_path)
# # # #                 actual_duration_ms = len(audio)
# # # #                 target_duration_ms = int(target_duration_sec * 1000)
# # # #
# # # #                 # 检查是否需要主动加速
# # # #                 if ENABLE_DYNAMIC_SPEED_ADJUSTMENT and actual_duration_ms > target_duration_ms * SPEED_ADJUST_THRESHOLD_RATIO:
# # # #                     # --- 阶段三：主动加速重生成 (Proactive Speed-Up) ---
# # # #                     ratio = actual_duration_ms / target_duration_ms
# # # #                     new_speed = min(ratio, MAX_SPEECH_SPEED)
# # # #
# # # #                     logger.warning(
# # # #                         f"[Task {task_id}] 音频过长({actual_duration_ms}ms > {target_duration_ms}ms)，"
# # # #                         f"将以 {new_speed:.2f}x 速度重生成。"
# # # #                     )
# # # #
# # # #                     # 使用新的语速参数重新请求
# # # #                     prosody = Prosody(speed=new_speed)
# # # #                     req_respeed = TTSRequest(
# # # #                         text=subtitle_text, reference_id=model_id, model=ENGINE_MODEL,
# # # #                         format=AUDIO_FORMAT, prosody=prosody
# # # #                     )
# # # #                     with open(full_audio_path, "wb") as f:
# # # #                         for chunk in session.tts(req_respeed):
# # # #                             f.write(chunk)
# # # #
# # # #                     logger.info(f"[Task {task_id}] 加速重生成完成。")
# # # #             except Exception as e:
# # # #                 logger.error(f"[Task {task_id}] 在测量或加速重生成阶段发生错误: {e}")
# # # #                 # 这种错误可能无法恢复，直接进入最终失败流程
# # # #                 raise e
# # # #
# # # #             # --- 阶段四：最终精修填充 (Final Polish) ---
# # # #             padding_ok = _apply_silence_padding(full_audio_path, task_info)
# # # #             if not padding_ok:
# # # #                 raise Exception("Audio generated, but final silence padding failed.")
# # # #
# # # #             # 所有流程成功
# # # #             task_info["audio_path"] = task_info["public_url"]
# # # #             task_info.pop("local_path", None)
# # # #             task_info.pop("public_url", None)
# # # #             return task_info
# # # #
# # # #         except Exception as e:
# # # #             if attempt < MAX_RETRIES - 1:
# # # #                 wait_time = RETRY_DELAY * (2 ** attempt)
# # # #                 logger.warning(
# # # #                     f"[Task {task_id}] TTS主流程第 {attempt + 1}/{MAX_RETRIES} 次尝试失败: {e}. 将在 {wait_time} 秒后重试...")
# # # #                 time.sleep(wait_time)
# # # #             else:
# # # #                 logger.error(f"[Task {task_id}] 所有重试均失败，最终错误: {e}")
# # # #                 task_info["audio_path"] = None
# # # #                 task_info["error"] = f"TTS generation failed after {MAX_RETRIES} retries: {str(e)}"
# # # #                 break  # 所有重试用完，退出循环
# # # #
# # # #     task_info.pop("local_path", None);
# # # #     task_info.pop("public_url", None)
# # # #     return task_info
# # # #
# # # #
# # # # # --- 内部辅助函数：移动目录 (保持不变) ---
# # # # def _move_workflow_directory(workflow_id: str):
# # # #     source_dir = SOURCE_DIR_TEMPLATE.format(workflow_id=workflow_id)
# # # #     dest_path = os.path.join(DEST_BASE_DIR, workflow_id)
# # # #     if not os.path.exists(source_dir): raise FileNotFoundError(f"源目录 '{source_dir}' 不存在")
# # # #     try:
# # # #         os.makedirs(DEST_BASE_DIR, exist_ok=True)
# # # #         if os.path.exists(dest_path): shutil.rmtree(dest_path)
# # # #         shutil.move(source_dir, DEST_BASE_DIR)
# # # #         logger.info(f"成功移动文件夹 '{workflow_id}' 到 '{DEST_BASE_DIR}'")
# # # #     except Exception as e:
# # # #         raise IOError(f"移动文件夹时发生错误: {str(e)}")
# # # #
# # # #
# # # # # --- FastAPI 主接口 (保持不变) ---
# # # # class TTSRequestPayload(BaseModel):
# # # #     raw_script: str = Field(..., description="包含时间和字幕的原始脚本。")
# # # #     model_id: str = Field(..., description="使用的 TTS 模型ID。")
# # # #     workflow_id: str = Field(..., description="本次任务的唯一工作流ID。")
# # # #     fish_api_key: str = Field(..., description="Fish Audio 的 API Key。")
# # # #
# # # #
# # # # @router.post("/generate_audio", summary="从脚本生成音频并处理文件", response_model=Dict[str, Any])
# # # # def generate_audio_workflow(payload: TTSRequestPayload):
# # # #     raw_script, model_id, workflow_id, fish_api_key = payload.raw_script, payload.model_id, payload.workflow_id, payload.fish_api_key
# # # #     if not all([fish_api_key, model_id, workflow_id, raw_script]): raise HTTPException(
# # # #         status_code=status.HTTP_400_BAD_REQUEST, detail="输入参数不完整。")
# # # #     logger.info(f"收到请求, workflow_id: '{workflow_id}'")
# # # #     audio_save_path = AUDIO_SAVE_PATH_TEMPLATE.format(workflow_id=workflow_id)
# # # #     try:
# # # #         os.makedirs(audio_save_path, exist_ok=True)
# # # #     except Exception as e:
# # # #         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
# # # #     parser = IntelligentParser(raw_script);
# # # #     parsed_items = parser.parse()
# # # #     if not parsed_items: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
# # # #                                              detail="智能解析失败：在脚本中未能识别出任何有效的字幕行。")
# # # #     tasks_to_process = []
# # # #     for id_counter, item in enumerate(parsed_items):
# # # #         current_timestamp, time_numbers = item["time_range"], re.findall(r"\d+\.?\d*", item["time_range"])
# # # #         start_sec, end_sec = (float(time_numbers[0]) if time_numbers else 0.0), (
# # # #             float(time_numbers[1]) if len(time_numbers) > 1 else 0.0)
# # # #         audio_filename = f"audio_{id_counter:03d}_{start_sec:.1f}s.{AUDIO_FORMAT}"
# # # #         tasks_to_process.append({
# # # #             "id": id_counter, "time_range": current_timestamp, "start_sec": start_sec, "end_sec": end_sec,
# # # #             "text": item["text"], "local_path": os.path.join(audio_save_path, audio_filename),
# # # #             "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
# # # #         })
# # # #     logger.info(f"智能解析完成，共 {len(tasks_to_process)} 个任务待处理。")
# # # #     final_tasks_list = []
# # # #     try:
# # # #         session = Session(fish_api_key)
# # # #         with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS,
# # # #                                                    thread_name_prefix="TTS_Worker") as executor:
# # # #             future_to_task = {executor.submit(generate_audio_single_task, session, task, model_id): task for task in
# # # #                               tasks_to_process}
# # # #             for future in concurrent.futures.as_completed(future_to_task): final_tasks_list.append(future.result())
# # # #     except Exception as e:
# # # #         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"并行处理任务时发生主错误: {e}")
# # # #     final_tasks_list.sort(key=lambda x: x['id'])
# # # #     move_error = None
# # # #     if any(task.get("audio_path") for task in final_tasks_list):
# # # #         try:
# # # #             _move_workflow_directory(workflow_id)
# # # #         except (FileNotFoundError, IOError) as e:
# # # #             move_error = str(e)
# # # #             logger.error(f"文件移动操作失败: {e}")
# # # #     else:
# # # #         logger.warning("没有任何音频生成成功，跳过文件移动操作。")
# # # #     final_result = {"audio_tasks": final_tasks_list,
# # # #                     "audio_tasks_str": json.dumps(final_tasks_list, ensure_ascii=False)}
# # # #     if move_error: final_result["move_operation_error"] = move_error
# # # #     return final_result
# # #
# # # # # # -*- coding: utf-8 -*-
# # # # # # @File：main_app.py
# # # # # # @Time：2025/8/5 18:30
# # # # # # @Author：_不咬闰土的猹丶
# # # # # # @email：hx1561958968@gmail.com
# # # # #
# # # # # # --- 导入模块 ---
# # # # # import re
# # # # # import json
# # # # # import os
# # # # # import shutil
# # # # # import logging
# # # # # import concurrent.futures
# # # # # import time
# # # # # from typing import Dict, List, Any, Tuple
# # # # #
# # # # # # FastAPI 相关导入
# # # # # from fastapi import APIRouter, HTTPException, status
# # # # # from pydantic import BaseModel, Field
# # # # #
# # # # # # Fish Audio SDK 导入
# # # # # from fish_audio_sdk import Session, TTSRequest
# # # # #
# # # # # # --- 日志设置 ---
# # # # # logging.basicConfig(
# # # # #     level=logging.INFO,
# # # # #     format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s'
# # # # # )
# # # # # logger = logging.getLogger(__name__)
# # # # #
# # # # # # --- FastAPI 应用初始化 ---
# # # # # router = APIRouter()
# # # # #
# # # # # # ===================================================================
# # # # # # --- Clash 代理设置区 (保持不变) ---
# # # # # PROXY_URL = "http://127.0.0.1:7890"
# # # # #
# # # # # if PROXY_URL:
# # # # #     os.environ['HTTP_PROXY'] = PROXY_URL
# # # # #     os.environ['HTTPS_PROXY'] = PROXY_URL
# # # # #     os.environ['http_proxy'] = PROXY_URL
# # # # #     os.environ['https_proxy'] = PROXY_URL
# # # # #     logger.warning(f"已配置全局 HTTP/HTTPS 代理: {PROXY_URL}")
# # # # # else:
# # # # #     logger.warning("未配置代理，将直接进行网络连接。")
# # # # # # ===================================================================
# # # # #
# # # # # # --- 配置区 (保持不变) ---
# # # # # ENGINE_MODEL = "speech-1.6"
# # # # # AUDIO_FORMAT = "mp3"
# # # # # PUBLIC_URL_TEMPLATE = "http://119.45.167.133:7752/meta-doc/video/{workflow_id}/audio/{filename}"
# # # # # BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# # # # # STATIC_DIR = os.path.join(BASE_DIR, "static")
# # # # # SOURCE_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
# # # # # AUDIO_SAVE_PATH_TEMPLATE = os.path.join(SOURCE_DIR_TEMPLATE, "audio")
# # # # # DEST_BASE_DIR = "/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
# # # # # MAX_WORKERS = 5
# # # # # MAX_RETRIES = 3
# # # # # RETRY_DELAY = 2
# # # # #
# # # # #
# # # # # # ======================================================================================
# # # # # # [全新集成] V4 终极智能解析器类
# # # # # # 该类将替换掉原来主函数中写死的解析逻辑，提供强大的兼容性。
# # # # # # ======================================================================================
# # # # # class IntelligentParser:
# # # # #     """
# # # # #     一个基于启发式规则和模式识别的智能解析器，用于从LLM输出中提取字幕。
# # # # #     它不依赖任何固定的标题字符串。
# # # # #     """
# # # # #
# # # # #     def __init__(self, raw_script: str):
# # # # #         self.script_lines = raw_script.splitlines()
# # # # #         self.line_count = len(self.script_lines)
# # # # #         self.subtitle_pattern = re.compile(r"^\s*(\d+\.?\d*\s*秒\s*-\s*\d+\.?\d*\s*秒)\s*:\s*(.+)$")
# # # # #         self.title_pattern = re.compile(r"^(#+.*|【.*】|\*\*.*\*\*|##\s.*)$")
# # # # #         self.positive_keywords = {"字幕": 5, "脚本": 3, "script": 3, "subtitle": 5, "text": 2, "文本": 2}
# # # # #         self.negative_keywords = {"分镜": -4, "视频": -4, "画面": -4, "视觉": -4, "场景": -4, "video": -4, "visual": -4}
# # # # #
# # # # #     def _find_all_potential_subtitles(self) -> List[Tuple[int, Dict[str, str]]]:
# # # # #         potential_subs = []
# # # # #         for i, line in enumerate(self.script_lines):
# # # # #             match = self.subtitle_pattern.match(line.strip())
# # # # #             if match:
# # # # #                 potential_subs.append(
# # # # #                     (i, {"time_range": match.group(1).strip(), "text": match.group(2).strip()})
# # # # #                 )
# # # # #         return potential_subs
# # # # #
# # # # #     def _cluster_subtitles(self, potential_subs: List[Tuple[int, Dict[str, str]]]) -> List[
# # # # #         List[Tuple[int, Dict[str, str]]]]:
# # # # #         if not potential_subs: return []
# # # # #         clusters = []
# # # # #         current_cluster = [potential_subs[0]]
# # # # #         for i in range(1, len(potential_subs)):
# # # # #             if potential_subs[i][0] - potential_subs[i - 1][0] <= 3:
# # # # #                 current_cluster.append(potential_subs[i])
# # # # #             else:
# # # # #                 clusters.append(current_cluster)
# # # # #                 current_cluster = [potential_subs[i]]
# # # # #         clusters.append(current_cluster)
# # # # #         return clusters
# # # # #
# # # # #     def _score_cluster(self, cluster: List[Tuple[int, Dict[str, str]]]) -> Tuple[int, List[Dict[str, str]]]:
# # # # #         start_line_num = cluster[0][0]
# # # # #         score = len(cluster)
# # # # #         for i in range(start_line_num - 1, max(-1, start_line_num - 6), -1):
# # # # #             line = self.script_lines[i].lower().strip()
# # # # #             if self.title_pattern.match(line) or len(line) < 15:
# # # # #                 for keyword, value in self.positive_keywords.items():
# # # # #                     if keyword in line: score += value
# # # # #                 for keyword, value in self.negative_keywords.items():
# # # # #                     if keyword in line: score += value
# # # # #                 break
# # # # #         return score, [item[1] for item in cluster]
# # # # #
# # # # #     def parse(self) -> List[Dict[str, str]]:
# # # # #         logger.warning("启动V4智能解析器...")
# # # # #         potential_subs = self._find_all_potential_subtitles()
# # # # #         if not potential_subs:
# # # # #             logger.warning("在整个文档中未能找到任何符合 '时间: 文本' 格式的行。")
# # # # #             return []
# # # # #         logger.warning(f"找到 {len(potential_subs)} 行潜在字幕。")
# # # # #         clusters = self._cluster_subtitles(potential_subs)
# # # # #         logger.warning(f"将潜在字幕聚类成 {len(clusters)} 个区块。")
# # # # #         if not clusters: return []
# # # # #         scored_clusters = [self._score_cluster(c) for c in clusters]
# # # # #         best_cluster = max(scored_clusters, key=lambda item: item[0])
# # # # #         if best_cluster[0] < 5 and len(potential_subs) > len(best_cluster[1]):
# # # # #             logger.warning(
# # # # #                 f"最佳区块得分({best_cluster[0]})过低，但全局找到了更多零散匹配项。将采用降级策略，返回所有匹配行。")
# # # # #             return [item[1] for item in potential_subs]
# # # # #         logger.warning(f"决策：选择得分最高的区块（得分: {best_cluster[0]}），包含 {len(best_cluster[1])} 行字幕。")
# # # # #         return best_cluster[1]
# # # # #
# # # # #
# # # # # # --- 内部辅助函数：移动目录 (保持不变) ---
# # # # # def _move_workflow_directory(workflow_id: str):
# # # # #     """[内部函数] 将指定 workflow_id 的目录从临时位置移动到最终位置。"""
# # # # #     source_dir = SOURCE_DIR_TEMPLATE.format(workflow_id=workflow_id)
# # # # #     # 注意：您的原代码是移动到 dest_dir 内部，而不是替换它，shutil.move的行为就是如此
# # # # #     # 如果dest_dir是/www/wwwroot/x-pilot-oss/uploads/meta-doc/video，源是static/file/abc
# # # # #     # 结果会是 /www/wwwroot/x-pilot-oss/uploads/meta-doc/video/abc
# # # # #     # 这里保持您原有的逻辑
# # # # #     dest_path = os.path.join(DEST_BASE_DIR, workflow_id)
# # # # #
# # # # #     logger.warning(f"准备移动文件夹: 从 '{source_dir}' 到 '{DEST_BASE_DIR}' (最终路径为: {dest_path})")
# # # # #
# # # # #     if not os.path.exists(source_dir):
# # # # #         raise FileNotFoundError(f"源目录 '{source_dir}' 不存在，无法移动。")
# # # # #
# # # # #     try:
# # # # #         os.makedirs(DEST_BASE_DIR, exist_ok=True)
# # # # #         # 如果目标路径已存在，先删除，防止shutil.move出错
# # # # #         if os.path.exists(dest_path):
# # # # #             logger.warning(f"目标路径 {dest_path} 已存在，将被覆盖。")
# # # # #             shutil.rmtree(dest_path)
# # # # #
# # # # #         shutil.move(source_dir, DEST_BASE_DIR)
# # # # #         logger.warning(f"成功将工作流 '{workflow_id}' 的文件夹移动到 '{DEST_BASE_DIR}'")
# # # # #     except Exception as e:
# # # # #         raise IOError(f"移动文件夹时发生错误: {str(e)}")
# # # # #
# # # # #
# # # # # # --- 音频生成工作函数 (保持不变) ---
# # # # # def generate_audio_single_task(session: Session, task_info: Dict[str, Any], model_id: str) -> Dict[str, Any]:
# # # # #     """[核心工作函数] 负责处理单个字幕的音频生成，并增加了重试逻辑。"""
# # # # #     task_id, subtitle_text, full_audio_path = task_info["id"], task_info["text"], task_info["local_path"]
# # # # #     logger.warning(f"[Task {task_id}] 开始处理: '{subtitle_text[:30]}...'")
# # # # #     for attempt in range(MAX_RETRIES):
# # # # #         try:
# # # # #             req = TTSRequest(text=subtitle_text, reference_id=model_id, model=ENGINE_MODEL, format=AUDIO_FORMAT)
# # # # #             with open(full_audio_path, "wb") as f:
# # # # #                 for chunk in session.tts(req):
# # # # #                     f.write(chunk)
# # # # #             logger.warning(f"[Task {task_id}] 音频成功保存到本地: {full_audio_path}")
# # # # #             task_info["audio_path"] = task_info["public_url"]
# # # # #             task_info.pop("local_path", None);
# # # # #             task_info.pop("public_url", None)
# # # # #             return task_info
# # # # #         except Exception as e:
# # # # #             if attempt < MAX_RETRIES - 1:
# # # # #                 wait_time = RETRY_DELAY * (2 ** attempt)
# # # # #                 logger.warning(
# # # # #                     f"[Task {task_id}] 第 {attempt + 1}/{MAX_RETRIES} 次尝试失败: {e}. 将在 {wait_time} 秒后重试...")
# # # # #                 time.sleep(wait_time)
# # # # #             else:
# # # # #                 logger.error(f"[Task {task_id}] 所有重试均失败，最终错误: {e}")
# # # # #                 task_info["audio_path"] = None
# # # # #                 task_info["error"] = f"After {MAX_RETRIES} retries, final error: {str(e)}"
# # # # #                 task_info.pop("local_path", None);
# # # # #                 task_info.pop("public_url", None)
# # # # #                 return task_info
# # # # #     return task_info
# # # # #
# # # # #
# # # # # # --- FastAPI 主接口 ---
# # # # # class TTSRequestPayload(BaseModel):
# # # # #     raw_script: str = Field(..., description="包含时间和字幕的原始脚本。")
# # # # #     model_id: str = Field(..., description="使用的 TTS 模型ID。")
# # # # #     workflow_id: str = Field(..., description="本次任务的唯一工作流ID。")
# # # # #     fish_api_key: str = Field(..., description="Fish Audio 的 API Key。")
# # # # #
# # # # #
# # # # # @router.post("/generate_audio", summary="从脚本生成音频并处理文件", response_model=Dict[str, Any])
# # # # # def generate_audio_workflow(payload: TTSRequestPayload):
# # # # #     """
# # # # #     接收脚本和配置，完成整个音频生成和文件移动的工作流。
# # # # #     [V4升级版] 使用智能解析器，兼容任意格式的脚本。
# # # # #     """
# # # # #     raw_script, model_id, workflow_id, fish_api_key = \
# # # # #         payload.raw_script, payload.model_id, payload.workflow_id, payload.fish_api_key
# # # # #
# # # # #     logger.warning(f"收到新的音频生成请求, workflow_id: '{workflow_id}'")
# # # # #     if not all([fish_api_key, model_id, workflow_id, raw_script]):
# # # # #         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="输入参数不完整。")
# # # # #
# # # # #     # --- 步骤 1: 初始化环境 ---
# # # # #     audio_save_path = AUDIO_SAVE_PATH_TEMPLATE.format(workflow_id=workflow_id)
# # # # #     try:
# # # # #         os.makedirs(audio_save_path, exist_ok=True)
# # # # #         logger.warning(f"为 workflow '{workflow_id}' 确保目录存在: {audio_save_path}")
# # # # #     except Exception as e:
# # # # #         logger.error(f"创建目录 {audio_save_path} 时发生严重错误: {e}")
# # # # #         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
# # # # #
# # # # #     # ======================================================================================
# # # # #     # --- [核心修改点] 任务解析逻辑替换 ---
# # # # #     # 旧的、脆弱的解析逻辑被下面更强大的代码块取代
# # # # #     # ======================================================================================
# # # # #
# # # # #     # 步骤 1-B: [全新] 使用V4智能解析器提取字幕
# # # # #     parser = IntelligentParser(raw_script)
# # # # #     parsed_items = parser.parse()
# # # # #
# # # # #     if not parsed_items:
# # # # #         raise HTTPException(
# # # # #             status_code=status.HTTP_400_BAD_REQUEST,
# # # # #             detail="智能解析失败：在脚本中未能识别出任何有效的字幕行 (格式应为 '时间: 文本')。"
# # # # #         )
# # # # #
# # # # #     # 步骤 1-C: 根据解析结果构建完整的任务列表 (此逻辑与您原来一致，但数据源更可靠)
# # # # #     tasks_to_process = []
# # # # #     for id_counter, item in enumerate(parsed_items):
# # # # #         current_timestamp = item["time_range"]
# # # # #         subtitle_text = item["text"]
# # # # #
# # # # #         time_numbers = re.findall(r"\d+\.?\d*", current_timestamp)
# # # # #         start_sec = float(time_numbers[0]) if time_numbers else 0.0
# # # # #         # 使用 .1f 格式化秒数，使文件名更规整，例如 audio_001_5.2s.mp3
# # # # #         audio_filename = f"audio_{id_counter:03d}_{start_sec:.1f}s.{AUDIO_FORMAT}"
# # # # #
# # # # #         tasks_to_process.append({
# # # # #             "id": id_counter,
# # # # #             "time_range": current_timestamp,
# # # # #             "start_sec": start_sec,
# # # # #             "end_sec": float(time_numbers[1]) if len(time_numbers) > 1 else 0.0,
# # # # #             "text": subtitle_text,
# # # # #             "local_path": os.path.join(audio_save_path, audio_filename),
# # # # #             "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
# # # # #         })
# # # # #
# # # # #     logger.warning(f"智能解析完成，共 {len(tasks_to_process)} 个任务待处理。")
# # # # #
# # # # #     # --- 步骤 2: 并行执行任务 (保持不变) ---
# # # # #     final_tasks_list = []
# # # # #     try:
# # # # #         session = Session(fish_api_key)
# # # # #         logger.warning(f"Fish Audio 会话初始化成功。将使用 {MAX_WORKERS} 个工作线程和 {MAX_RETRIES} 次重试机会处理任务。")
# # # # #         with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS,
# # # # #                                                    thread_name_prefix="TTS_Worker") as executor:
# # # # #             future_to_task = {
# # # # #                 executor.submit(generate_audio_single_task, session, task, model_id): task
# # # # #                 for task in tasks_to_process
# # # # #             }
# # # # #             for future in concurrent.futures.as_completed(future_to_task):
# # # # #                 final_tasks_list.append(future.result())
# # # # #     except Exception as e:
# # # # #         logger.error(f"并行处理任务时发生主错误: {e}")
# # # # #         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"并行处理任务时发生主错误: {e}")
# # # # #
# # # # #     final_tasks_list.sort(key=lambda x: x['id'])
# # # # #     logger.warning("所有音频生成任务已完成处理。")
# # # # #
# # # # #     # --- 步骤 3: 移动文件夹 (保持不变) ---
# # # # #     move_error = None
# # # # #     if any(task.get("audio_path") for task in final_tasks_list):
# # # # #         logger.warning("至少有一个音频生成成功，开始执行文件移动操作。")
# # # # #         try:
# # # # #             _move_workflow_directory(workflow_id)
# # # # #         except (FileNotFoundError, IOError) as e:
# # # # #             move_error = str(e)
# # # # #             logger.error(f"文件移动操作失败: {e}. 但音频结果仍会返回。")
# # # # #     else:
# # # # #         logger.warning("没有任何音频生成成功，跳过文件移动操作。")
# # # # #
# # # # #     # --- 步骤 4: 返回最终结果 (保持不变) ---
# # # # #     logger.warning(f"工作流 '{workflow_id}' 全部流程结束，准备返回最终结果。")
# # # # #     final_result = {
# # # # #         "audio_tasks": final_tasks_list,
# # # # #         "audio_tasks_str": json.dumps(final_tasks_list, ensure_ascii=False, indent=2)
# # # # #     }
# # # # #     if move_error:
# # # # #         final_result["move_operation_error"] = move_error
# # # # #
# # # # #     return final_result
# # # # #
# # # # # # # -*- coding: utf-8 -*-
# # # # # # # @File：main_app_v5_websocket.py
# # # # # # # @Time：2025/8/6 10:00
# # # # # # # @Author：_不咬闰土的猹丶 (Upgraded by AI)
# # # # # # # @email：hx1561958968@gmail.com
# # # # # #
# # # # # # # --- 导入模块 ---
# # # # # # import re
# # # # # # import json
# # # # # # import os
# # # # # # import shutil
# # # # # # import logging
# # # # # # import asyncio
# # # # # # import uuid
# # # # # # from typing import Dict, List, Any, Tuple
# # # # # # import ormsgpack
# # # # # # import websockets
# # # # # # import aioredis
# # # # # #
# # # # # # # FastAPI 相关导入
# # # # # # from fastapi import APIRouter, HTTPException, status, Depends
# # # # # # from pydantic import BaseModel, Field
# # # # # #
# # # # # # # --- 日志设置 ---
# # # # # # logging.basicConfig(
# # # # # #     level=logging.INFO,
# # # # # #     format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s'
# # # # # # )
# # # # # # logger = logging.getLogger(__name__)
# # # # # #
# # # # # # # --- FastAPI 应用初始化 ---
# # # # # # router = APIRouter()
# # # # # #
# # # # # # # ===================================================================
# # # # # # # --- Clash 代理设置区 (保持不变) ---
# # # # # # PROXY_URL = "http://127.0.0.1:7890"
# # # # # #
# # # # # # # 注意：websockets库不直接使用环境变量的代理，需要在连接时手动指定
# # # # # # # 但为了其他可能的http请求（如回调），这里依旧保留
# # # # # # if PROXY_URL:
# # # # # #     os.environ['HTTP_PROXY'] = PROXY_URL
# # # # # #     os.environ['HTTPS_PROXY'] = PROXY_URL
# # # # # #     logger.warning(f"已配置全局 HTTP/HTTPS 代理: {PROXY_URL}")
# # # # # # else:
# # # # # #     logger.warning("未配置代理，将直接进行网络连接。")
# # # # # # # ===================================================================
# # # # # #
# # # # # # # --- 配置区 ---
# # # # # # # [V5 核心] API并发限制，这将决定我们的WebSocket池大小
# # # # # # API_CONCURRENCY_LIMIT = 5
# # # # # #
# # # # # # # Fish Audio TTS 引擎相关配置
# # # # # # ENGINE_MODEL = "speech-1.6"
# # # # # # AUDIO_FORMAT = "mp3"  # 虽然WebSocket API支持opus, 但为保持一致性用mp3
# # # # # #
# # # # # # # Redis配置，用于任务分发和结果回收
# # # # # # # 请确保您已启动一个Redis服务
# # # # # # REDIS_URL = "redis://localhost"
# # # # # #
# # # # # # # 路径和URL配置
# # # # # # PUBLIC_URL_TEMPLATE = "http://119.45.167.133:2906/uploads/meta-doc/video/{workflow_id}/audio/{filename}"
# # # # # # BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# # # # # # STATIC_DIR = os.path.join(BASE_DIR, "static")
# # # # # # SOURCE_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
# # # # # # AUDIO_SAVE_PATH_TEMPLATE = os.path.join(SOURCE_DIR_TEMPLATE, "audio")
# # # # # # DEST_BASE_DIR = "/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
# # # # # #
# # # # # #
# # # # # # # ======================================================================================
# # # # # # # 全新架构：WebSocket工作者与连接池
# # # # # # # ======================================================================================
# # # # # # class WebSocketWorker:
# # # # # #     """封装单个持久WebSocket连接及其任务处理逻辑"""
# # # # # #     BASE_URI = "wss://api.fish.audio/v1/tts/live"
# # # # # #
# # # # # #     def __init__(self, worker_id: int, api_key: str, redis_pool):
# # # # # #         self.worker_id = worker_id
# # # # # #         self.api_key = api_key
# # # # # #         self.redis = redis_pool
# # # # # #         self.websocket: websockets.WebSocketClientProtocol = None
# # # # # #         self.task_queue = asyncio.Queue()
# # # # # #         self.is_running = False
# # # # # #         self._main_task_handle = None
# # # # # #         self.reconnect_delay = 5
# # # # # #
# # # # # #     async def _connect(self):
# # # # # #         """建立或重建WebSocket连接"""
# # # # # #         headers = {"Authorization": f"Bearer {self.api_key}"}
# # # # # #         # websockets 不使用环境变量代理，需要这样设置
# # # # # #         proxy = websockets.uri.parse_uri(PROXY_URL) if PROXY_URL else None
# # # # # #
# # # # # #         while self.is_running:
# # # # # #             try:
# # # # # #                 logger.warning(f"[Worker-{self.worker_id}] 正在连接到 {self.BASE_URI}...")
# # # # # #                 self.websocket = await websockets.connect(
# # # # # #                     self.BASE_URI,
# # # # # #                     extra_headers=headers,
# # # # # #                     # http_proxy_host=proxy.host if proxy else None,
# # # # # #                     # http_proxy_port=proxy.port if proxy else None,
# # # # # #                     # ssl=True if self.BASE_URI.startswith('wss') else False # 根据需要调整
# # # # # #                 )
# # # # # #                 logger.warning(f"[Worker-{self.worker_id}] WebSocket 连接成功.")
# # # # # #                 return True
# # # # # #             except Exception as e:
# # # # # #                 logger.error(f"[Worker-{self.worker_id}] 连接失败: {e}. 将在 {self.reconnect_delay}秒后重试...")
# # # # # #                 await asyncio.sleep(self.reconnect_delay)
# # # # # #         return False
# # # # # #
# # # # # #     async def _process_task(self, task: Dict):
# # # # # #         """通过WebSocket处理单个TTS任务"""
# # # # # #         task_id = task['internal_task_id']
# # # # # #         logger.warning(f"[Worker-{self.worker_id}] 开始处理任务 {task_id[:8]}: {task['text'][:30]}...")
# # # # # #
# # # # # #         try:
# # # # # #             # 1. 发送 'start' 事件
# # # # # #             start_payload = {
# # # # # #                 "event": "start",
# # # # # #                 "request": {
# # # # # #                     "text": "",
# # # # # #                     "format": AUDIO_FORMAT,
# # # # # #                     "reference_id": task["model_id"],
# # # # # #                     "latency": "normal",
# # # # # #                 },
# # # # # #                 "model": ENGINE_MODEL
# # # # # #             }
# # # # # #             await self.websocket.send(ormsgpack.packb(start_payload))
# # # # # #
# # # # # #             # 2. 发送 'text' 事件
# # # # # #             text_payload = {"event": "text", "text": task["text"]}
# # # # # #             await self.websocket.send(ormsgpack.packb(text_payload))
# # # # # #
# # # # # #             # 3. 发送 'stop' 事件来 flush buffer 并获取音频
# # # # # #             await self.websocket.send(ormsgpack.packb({"event": "stop"}))
# # # # # #
# # # # # #             # 4. 接收音频数据
# # # # # #             audio_chunks = []
# # # # # #             while True:
# # # # # #                 message = await asyncio.wait_for(self.websocket.recv(), timeout=30)
# # # # # #                 data = ormsgpack.unpackb(message, use_list=True)
# # # # # #                 if data["event"] == "audio":
# # # # # #                     audio_chunks.append(data["audio"])
# # # # # #                 elif data["event"] == "finish":
# # # # # #                     logger.warning(f"[Worker-{self.worker_id}] 任务 {task_id[:8]} 完成接收.")
# # # # # #                     break
# # # # # #                 elif data["event"] == "error":
# # # # # #                     raise Exception(f"API返回错误: {data.get('reason')}")
# # # # # #
# # # # # #             # 5. 保存文件并发布结果
# # # # # #             full_audio_content = b"".join(audio_chunks)
# # # # # #             with open(task["local_path"], "wb") as f:
# # # # # #                 f.write(full_audio_content)
# # # # # #
# # # # # #             result = task.copy()
# # # # # #             result['status'] = 'success'
# # # # # #             result['audio_path'] = result.pop('public_url')  # 重命名
# # # # # #             result.pop('local_path', None)
# # # # # #             await self.redis.publish(f"task_result:{task_id}", json.dumps(result))
# # # # # #
# # # # # #         except Exception as e:
# # # # # #             logger.error(f"[Worker-{self.worker_id}] 处理任务 {task_id[:8]} 失败: {e}")
# # # # # #             result = task.copy()
# # # # # #             result['status'] = 'failed'
# # # # # #             result['error'] = str(e)
# # # # # #             result.pop('local_path', None)
# # # # # #             result.pop('public_url', None)
# # # # # #             await self.redis.publish(f"task_result:{task_id}", json.dumps(result))
# # # # # #
# # # # # #     async def _run(self):
# # # # # #         """工作者的主循环"""
# # # # # #         self.is_running = True
# # # # # #         if not await self._connect():
# # # # # #             return  # 如果启动时无法连接，则工作者退出
# # # # # #
# # # # # #         while self.is_running:
# # # # # #             try:
# # # # # #                 task = await self.task_queue.get()
# # # # # #                 await self._process_task(task)
# # # # # #                 self.task_queue.task_done()
# # # # # #             except (websockets.ConnectionClosed, asyncio.CancelledError) as e:
# # # # # #                 logger.warning(f"[Worker-{self.worker_id}] 连接丢失或任务取消: {e}. 尝试重连...")
# # # # # #                 if self.is_running:  # 只有在服务还在运行时才重连
# # # # # #                     if not await self._connect():
# # # # # #                         logger.error(f"[Worker-{self.worker_id}] 重连失败，工作者将停止。")
# # # # # #                         break  # 重连失败，退出循环
# # # # # #             except Exception as e:
# # # # # #                 logger.error(f"[Worker-{self.worker_id}] 主循环发生未知错误: {e}")
# # # # # #                 await asyncio.sleep(1)  # 防止错误导致CPU空转
# # # # # #
# # # # # #     def start(self):
# # # # # #         self._main_task_handle = asyncio.create_task(self._run())
# # # # # #
# # # # # #     async def stop(self):
# # # # # #         self.is_running = False
# # # # # #         if self.websocket:
# # # # # #             await self.websocket.close()
# # # # # #         if self._main_task_handle:
# # # # # #             self._main_task_handle.cancel()
# # # # # #             try:
# # # # # #                 await self._main_task_handle
# # # # # #             except asyncio.CancelledError:
# # # # # #                 pass
# # # # # #         logger.warning(f"[Worker-{self.worker_id}] 已停止。")
# # # # # #
# # # # # #     async def submit(self, task: Dict):
# # # # # #         await self.task_queue.put(task)
# # # # # #
# # # # # #
# # # # # # class WebSocketPool:
# # # # # #     """管理WebSocketWorker池和任务分发"""
# # # # # #
# # # # # #     def __init__(self):
# # # # # #         self.workers: List[WebSocketWorker] = []
# # # # # #         self.redis: aioredis.Redis = None
# # # # # #         self._next_worker_idx = 0
# # # # # #
# # # # # #     async def startup(self, num_workers: int):
# # # # # #         self.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
# # # # # #         self.workers = [WebSocketWorker(i, self.redis) for i in range(num_workers)]
# # # # # #         for worker in self.workers:
# # # # # #             worker.start()
# # # # # #         logger.warning(f"WebSocket池已启动，包含 {num_workers} 个工作者。")
# # # # # #
# # # # # #     async def shutdown(self):
# # # # # #         for worker in self.workers:
# # # # # #             await worker.stop()
# # # # # #         await self.redis.close()
# # # # # #         logger.warning("WebSocket池已关闭。")
# # # # # #
# # # # # #     async def submit_task(self, task: Dict) -> asyncio.Future:
# # # # # #         """提交任务到池中，并返回一个future用于等待结果"""
# # # # # #         internal_task_id = str(uuid.uuid4())
# # # # # #         task['internal_task_id'] = internal_task_id
# # # # # #
# # # # # #         # 使用轮询策略分发任务
# # # # # #         worker = self.workers[self._next_worker_idx]
# # # # # #         self._next_worker_idx = (self._next_worker_idx + 1) % len(self.workers)
# # # # # #
# # # # # #         future = asyncio.get_event_loop().create_future()
# # # # # #
# # # # # #         async def _wait_for_result():
# # # # # #             pubsub = self.redis.pubsub()
# # # # # #             await pubsub.subscribe(f"task_result:{internal_task_id}")
# # # # # #             try:
# # # # # #                 async for message in pubsub.listen():
# # # # # #                     if message['type'] == 'message':
# # # # # #                         result_data = json.loads(message['data'])
# # # # # #                         future.set_result(result_data)
# # # # # #                         break
# # # # # #             finally:
# # # # # #                 await pubsub.unsubscribe(f"task_result:{internal_task_id}")
# # # # # #
# # # # # #         await worker.submit(task)
# # # # # #         asyncio.create_task(_wait_for_result())
# # # # # #         return future
# # # # # #
# # # # # #
# # # # # # # 全局池实例
# # # # # # ws_pool = WebSocketPool()
# # # # # #
# # # # # #
# # # # # # # --- 智能解析器 (与之前版本一致) ---
# # # # # # # ... (IntelligentParser类的代码可以从之前版本完整复制过来，这里为简洁省略)
# # # # # # # 为确保完整性，这里粘贴IntelligentParser
# # # # # # class IntelligentParser:
# # # # # #     def __init__(self, raw_script: str):
# # # # # #         self.script_lines = raw_script.splitlines()
# # # # # #         self.subtitle_pattern = re.compile(r"^\s*(\d+\.?\d*\s*秒\s*-\s*\d+\.?\d*\s*秒)\s*:\s*(.+)$")
# # # # # #         self.title_pattern = re.compile(r"^(#+.*|【.*】|\*\*.*\*\*|##\s.*)$")
# # # # # #         self.positive_keywords = {"字幕": 5, "脚本": 3, "script": 3, "subtitle": 5, "text": 2, "文本": 2}
# # # # # #         self.negative_keywords = {"分镜": -4, "视频": -4, "画面": -4, "视觉": -4, "场景": -4}
# # # # # #
# # # # # #     def _find_all_potential_subtitles(self) -> List[Tuple[int, Dict[str, str]]]:
# # # # # #         potential_subs = []
# # # # # #         for i, line in enumerate(self.script_lines):
# # # # # #             match = self.subtitle_pattern.match(line.strip())
# # # # # #             if match:
# # # # # #                 potential_subs.append((i, {"time_range": match.group(1).strip(), "text": match.group(2).strip()}))
# # # # # #         return potential_subs
# # # # # #
# # # # # #     def _cluster_subtitles(self, subs: List[Tuple[int, Dict[str, str]]]) -> List[List[Tuple[int, Dict[str, str]]]]:
# # # # # #         if not subs: return []
# # # # # #         clusters = [];
# # # # # #         current_cluster = [subs[0]]
# # # # # #         for i in range(1, len(subs)):
# # # # # #             if subs[i][0] - subs[i - 1][0] <= 3:
# # # # # #                 current_cluster.append(subs[i])
# # # # # #             else:
# # # # # #                 clusters.append(current_cluster);
# # # # # #                 current_cluster = [subs[i]]
# # # # # #         clusters.append(current_cluster)
# # # # # #         return clusters
# # # # # #
# # # # # #     def _score_cluster(self, cluster: List[Tuple[int, Dict[str, str]]]) -> Tuple[int, List[Dict[str, str]]]:
# # # # # #         start_line_num = cluster[0][0]
# # # # # #         score = len(cluster)
# # # # # #         for i in range(start_line_num - 1, max(-1, start_line_num - 6), -1):
# # # # # #             line = self.script_lines[i].lower().strip()
# # # # # #             if self.title_pattern.match(line) or len(line) < 15:
# # # # # #                 for k, v in self.positive_keywords.items():
# # # # # #                     if k in line: score += v
# # # # # #                 for k, v in self.negative_keywords.items():
# # # # # #                     if k in line: score += v
# # # # # #                 break
# # # # # #         return score, [item[1] for item in cluster]
# # # # # #
# # # # # #     def parse(self) -> List[Dict[str, str]]:
# # # # # #         logger.warning("启动V4智能解析器...");
# # # # # #         potential_subs = self._find_all_potential_subtitles()
# # # # # #         if not potential_subs: logger.warning("未找到任何'时间: 文本'格式的行。"); return []
# # # # # #         clusters = self._cluster_subtitles(potential_subs)
# # # # # #         if not clusters: return []
# # # # # #         scored_clusters = [self._score_cluster(c) for c in clusters]
# # # # # #         best_cluster = max(scored_clusters, key=lambda item: item[0])
# # # # # #         if best_cluster[0] < 5 and len(potential_subs) > len(best_cluster[1]):
# # # # # #             logger.warning("最佳区块得分低，返回所有匹配项。")
# # # # # #             return [item[1] for item in potential_subs]
# # # # # #         return best_cluster[1]
# # # # # #
# # # # # #
# # # # # # # --- 辅助函数：移动目录 (与之前版本一致) ---
# # # # # # # ... ( _move_workflow_directory 函数的代码可以从之前版本完整复制过来，这里为简洁省略)
# # # # # # def _move_workflow_directory(workflow_id: str):
# # # # # #     source_dir = SOURCE_DIR_TEMPLATE.format(workflow_id=workflow_id)
# # # # # #     dest_path = os.path.join(DEST_BASE_DIR, workflow_id)
# # # # # #     if not os.path.exists(source_dir): raise FileNotFoundError(f"源目录不存在: {source_dir}")
# # # # # #     os.makedirs(DEST_BASE_DIR, exist_ok=True)
# # # # # #     if os.path.exists(dest_path): shutil.rmtree(dest_path)
# # # # # #     shutil.move(source_dir, DEST_BASE_DIR)
# # # # # #     logger.warning(f"文件夹移动成功: {source_dir} -> {dest_path}")
# # # # # #
# # # # # #
# # # # # # # --- FastAPI 事件钩子，用于启动和关闭连接池 ---
# # # # # # @router.on_event("startup")
# # # # # # async def startup_event():
# # # # # #     # api_key = os.getenv("FISH_API_KEY")  # 从环境变量获取API Key更安全
# # # # # #     # if not api_key:
# # # # # #     #     raise ValueError("请设置环境变量 FISH_API_KEY")
# # # # # #     await ws_pool.startup(API_CONCURRENCY_LIMIT)
# # # # # #
# # # # # #
# # # # # # @router.on_event("shutdown")
# # # # # # async def shutdown_event():
# # # # # #     await ws_pool.shutdown()
# # # # # #
# # # # # #
# # # # # # # --- FastAPI 主接口 ---
# # # # # # class TTSRequestPayload(BaseModel):
# # # # # #     raw_script: str = Field(..., description="原始脚本")
# # # # # #     model_id: str = Field(..., description="TTS 模型ID")
# # # # # #     workflow_id: str = Field(..., description="工作流ID")
# # # # # #     fish_api_key: str = Field(..., description="api_key")
# # # # # #
# # # # # #
# # # # # # @router.post("/generate_audio", summary="高并发生成音频", response_model=Dict[str, Any])
# # # # # # async def generate_audio_workflow(payload: TTSRequestPayload, pool: WebSocketPool = Depends(lambda: ws_pool)):
# # # # # #     """接收脚本，通过全局WebSocket池处理所有TTS任务，能承受高并发。"""
# # # # # #     raw_script, model_id, workflow_id = payload.raw_script, payload.model_id, payload.workflow_id
# # # # # #     logger.warning(f"收到请求, workflow_id: '{workflow_id}'")
# # # # # #
# # # # # #     # 1. 创建目录
# # # # # #     audio_save_path = AUDIO_SAVE_PATH_TEMPLATE.format(workflow_id=workflow_id)
# # # # # #     os.makedirs(audio_save_path, exist_ok=True)
# # # # # #
# # # # # #     # 2. 解析脚本
# # # # # #     parser = IntelligentParser(raw_script)
# # # # # #     parsed_items = parser.parse()
# # # # # #     if not parsed_items:
# # # # # #         raise HTTPException(status.HTTP_400_BAD_REQUEST, "智能解析失败：未识别出有效字幕行。")
# # # # # #
# # # # # #     # 3. 构建任务并提交到池
# # # # # #     tasks_to_submit = []
# # # # # #     for id_counter, item in enumerate(parsed_items):
# # # # # #         start_sec = float(re.findall(r"\d+\.?\d*", item["time_range"])[0])
# # # # # #         audio_filename = f"audio_{id_counter:03d}_{start_sec:.1f}s.{AUDIO_FORMAT}"
# # # # # #         tasks_to_submit.append({
# # # # # #             "id": id_counter, "time_range": item["time_range"], "start_sec": start_sec,
# # # # # #             "text": item["text"], "model_id": model_id,
# # # # # #             "local_path": os.path.join(audio_save_path, audio_filename),
# # # # # #             "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
# # # # # #         })
# # # # # #
# # # # # #     # 4. 并行提交所有任务并等待结果
# # # # # #     futures = [await pool.submit_task(task) for task in tasks_to_submit]
# # # # # #     final_tasks_list = await asyncio.gather(*futures)
# # # # # #     final_tasks_list.sort(key=lambda x: x['id'])
# # # # # #
# # # # # #     # 5. 移动文件夹
# # # # # #     move_error = None
# # # # # #     if any(task.get("status") == "success" for task in final_tasks_list):
# # # # # #         try:
# # # # # #             # 文件IO是阻塞的，最好在线程池中运行以避免阻塞事件循环
# # # # # #             loop = asyncio.get_event_loop()
# # # # # #             await loop.run_in_executor(None, _move_workflow_directory, workflow_id)
# # # # # #         except Exception as e:
# # # # # #             move_error = str(e);
# # # # # #             logger.error(f"文件移动失败: {e}")
# # # # # #
# # # # # #     # 6. 返回结果
# # # # # #     return {
# # # # # #         "audio_tasks": final_tasks_list,
# # # # # #         "audio_tasks_str": json.dumps(final_tasks_list, ensure_ascii=False, indent=2),
# # # # # #         "move_operation_error": move_error
# # # # # #     }
# # # #
# # # #
# # # # # -*- coding: utf-8 -*-
# # # # # @File：main_app.py
# # # # # @Time：2025/8/5 18:30
# # # # # @Author：_不咬闰土的猹丶
# # # # # @email：hx1561958968@gmail.com
# # # #
# # # # # --- 导入模块 ---
# # # # import re
# # # # import json
# # # # import os
# # # # import shutil
# # # # import logging
# # # # import concurrent.futures
# # # # import time
# # # # from typing import Dict, List, Any, Tuple
# # # #
# # # # # FastAPI 相关导入
# # # # from fastapi import APIRouter, HTTPException, status
# # # # from pydantic import BaseModel, Field
# # # #
# # # # # Fish Audio SDK 导入 (增加了Prosody)
# # # # from fish_audio_sdk import Session, TTSRequest, Prosody
# # # #
# # # # # --- 日志设置 ---
# # # # logging.basicConfig(
# # # #     level=logging.DEBUG,
# # # #     format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s'
# # # # )
# # # # logger = logging.getLogger(__name__)
# # # #
# # # # # --- FastAPI 应用初始化 ---
# # # # router = APIRouter()
# # # #
# # # # # --- Clash 代理设置区 -
# # # #
# # # # # --- 配置区 ---
# # # # ENGINE_MODEL = "speech-1.6"
# # # # AUDIO_FORMAT = "mp3"
# # # # PUBLIC_URL_TEMPLATE = "http://119.45.167.133:2906/uploads/meta-doc/video/{workflow_id}/audio/{filename}"
# # # # BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# # # # STATIC_DIR = os.path.join(BASE_DIR, "static")
# # # # SOURCE_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
# # # # AUDIO_SAVE_PATH_TEMPLATE = os.path.join(SOURCE_DIR_TEMPLATE, "audio")
# # # # DEST_BASE_DIR = "/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
# # # # MAX_WORKERS = 5
# # # # MAX_RETRIES = 3
# # # # RETRY_DELAY = 2
# # # #
# # # # # --- [新增] 动态语速控制参数 ---
# # # # # 基准语速：每秒播报的字符数 (最关键的调节参数，根据实际效果微调)
# # # # CHARS_PER_SECOND_ESTIMATE = 4.8
# # # # # 允许的最小语速
# # # # MIN_SPEED = 0.6
# # # # # 允许的最大语速
# # # # MAX_SPEED = 2.0
# # # #
# # # #
# # # # # --- V4 终极智能解析器类 (完整实现) ---
# # # # class IntelligentParser:
# # # #     """
# # # #     一个基于启发式规则和模式识别的智能解析器，用于从LLM输出中提取字幕。
# # # #     """
# # # #
# # # #     def __init__(self, raw_script: str):
# # # #         self.script_lines = raw_script.splitlines()
# # # #         self.line_count = len(self.script_lines)
# # # #         self.subtitle_pattern = re.compile(r"^\s*(\d+\.?\d*\s*秒\s*-\s*\d+\.?\d*\s*秒)\s*:\s*(.+)$")
# # # #         self.title_pattern = re.compile(r"^(#+.*|【.*】|\*\*.*\*\*|##\s.*)$")
# # # #         self.positive_keywords = {"字幕": 5, "脚本": 3, "script": 3, "subtitle": 5, "text": 2, "文本": 2}
# # # #         self.negative_keywords = {"分镜": -4, "视频": -4, "画面": -4, "视觉": -4, "场景": -4, "video": -4, "visual": -4}
# # # #
# # # #     def _find_all_potential_subtitles(self) -> List[Tuple[int, Dict[str, str]]]:
# # # #         potential_subs = []
# # # #         for i, line in enumerate(self.script_lines):
# # # #             match = self.subtitle_pattern.match(line.strip())
# # # #             if match:
# # # #                 potential_subs.append(
# # # #                     (i, {"time_range": match.group(1).strip(), "text": match.group(2).strip()})
# # # #                 )
# # # #         return potential_subs
# # # #
# # # #     def _cluster_subtitles(self, potential_subs: List[Tuple[int, Dict[str, str]]]) -> List[
# # # #         List[Tuple[int, Dict[str, str]]]]:
# # # #         if not potential_subs: return []
# # # #         clusters = []
# # # #         current_cluster = [potential_subs[0]]
# # # #         for i in range(1, len(potential_subs)):
# # # #             if potential_subs[i][0] - potential_subs[i - 1][0] <= 3:
# # # #                 current_cluster.append(potential_subs[i])
# # # #             else:
# # # #                 clusters.append(current_cluster)
# # # #                 current_cluster = [potential_subs[i]]
# # # #         clusters.append(current_cluster)
# # # #         return clusters
# # # #
# # # #     def _score_cluster(self, cluster: List[Tuple[int, Dict[str, str]]]) -> Tuple[int, List[Dict[str, str]]]:
# # # #         start_line_num = cluster[0][0]
# # # #         score = len(cluster)
# # # #         for i in range(start_line_num - 1, max(-1, start_line_num - 6), -1):
# # # #             line = self.script_lines[i].lower().strip()
# # # #             if self.title_pattern.match(line) or len(line) < 15:
# # # #                 for keyword, value in self.positive_keywords.items():
# # # #                     if keyword in line: score += value
# # # #                 for keyword, value in self.negative_keywords.items():
# # # #                     if keyword in line: score += value
# # # #                 break
# # # #         return score, [item[1] for item in cluster]
# # # #
# # # #     def parse(self) -> List[Dict[str, str]]:
# # # #         logger.warning("启动V4智能解析器...")
# # # #         potential_subs = self._find_all_potential_subtitles()
# # # #         if not potential_subs:
# # # #             logger.warning("在整个文档中未能找到任何符合 '时间: 文本' 格式的行。")
# # # #             return []
# # # #         logger.warning(f"找到 {len(potential_subs)} 行潜在字幕。")
# # # #         clusters = self._cluster_subtitles(potential_subs)
# # # #         if not clusters: return []
# # # #         scored_clusters = [self._score_cluster(c) for c in clusters]
# # # #         best_cluster = max(scored_clusters, key=lambda item: item[0])
# # # #         if best_cluster[0] < 5 and len(potential_subs) > len(best_cluster[1]):
# # # #             logger.warning(
# # # #                 f"最佳区块得分({best_cluster[0]})过低，但全局找到了更多零散匹配项。将采用降级策略，返回所有匹配行。")
# # # #             return [item[1] for item in potential_subs]
# # # #         logger.warning(f"决策：选择得分最高的区块（得分: {best_cluster[0]}），包含 {len(best_cluster[1])} 行字幕。")
# # # #         return best_cluster[1]
# # # #
# # # #
# # # # # --- 内部辅助函数：移动目录 (完整实现) ---
# # # # def _move_workflow_directory(workflow_id: str):
# # # #     source_dir = SOURCE_DIR_TEMPLATE.format(workflow_id=workflow_id)
# # # #     dest_path = os.path.join(DEST_BASE_DIR, workflow_id)
# # # #     logger.warning(f"准备移动文件夹: 从 '{source_dir}' 到 '{DEST_BASE_DIR}' (最终路径为: {dest_path})")
# # # #     if not os.path.exists(source_dir):
# # # #         raise FileNotFoundError(f"源目录 '{source_dir}' 不存在，无法移动。")
# # # #     try:
# # # #         os.makedirs(DEST_BASE_DIR, exist_ok=True)
# # # #         if os.path.exists(dest_path):
# # # #             logger.warning(f"目标路径 {dest_path} 已存在，将被覆盖。")
# # # #             shutil.rmtree(dest_path)
# # # #         shutil.move(source_dir, DEST_BASE_DIR)
# # # #         logger.warning(f"成功将工作流 '{workflow_id}' 的文件夹移动到 '{DEST_BASE_DIR}'")
# # # #     except Exception as e:
# # # #         raise IOError(f"移动文件夹时发生错误: {str(e)}")
# # # #
# # # #
# # # # # --- [核心修改] 支持动态语速的核心工作函数 (完整实现) ---
# # # # def generate_audio_single_task(session: Session, task_info: Dict[str, Any], model_id: str) -> Dict[str, Any]:
# # # #     task_id, subtitle_text, full_audio_path = task_info["id"], task_info["text"], task_info["local_path"]
# # # #
# # # #     # 动态语速计算
# # # #     target_duration = task_info["end_sec"] - task_info["start_sec"]
# # # #     speed_control_enabled = target_duration > 0.1
# # # #     req: TTSRequest
# # # #
# # # #     if speed_control_enabled:
# # # #         estimated_natural_duration = len(subtitle_text) / CHARS_PER_SECOND_ESTIMATE
# # # #         required_speed = estimated_natural_duration / target_duration
# # # #         final_speed = max(MIN_SPEED, min(MAX_SPEED, required_speed))
# # # #         logger.warning(
# # # #             f"[Task {task_id}] 语速控制: 目标时长={target_duration:.2f}s, "
# # # #             f"估算自然时长={estimated_natural_duration:.2f}s, "
# # # #             f"计算速度={required_speed:.2f}, 最终应用速度={final_speed:.2f}"
# # # #         )
# # # #         prosody_config = Prosody(speed=final_speed)
# # # #         req = TTSRequest(text=subtitle_text, reference_id=model_id, model=ENGINE_MODEL, format=AUDIO_FORMAT,
# # # #                          prosody=prosody_config)
# # # #     else:
# # # #         logger.warning(f"[Task {task_id}] 目标时长过短或无效，不启用语速控制。")
# # # #         req = TTSRequest(text=subtitle_text, reference_id=model_id, model=ENGINE_MODEL, format=AUDIO_FORMAT)
# # # #
# # # #     # 音频生成与重试
# # # #     logger.warning(f"[Task {task_id}] 开始处理: '{subtitle_text[:30]}...'")
# # # #     for attempt in range(MAX_RETRIES):
# # # #         try:
# # # #             with open(full_audio_path, "wb") as f:
# # # #                 for chunk in session.tts(req):
# # # #                     f.write(chunk)
# # # #             logger.warning(f"[Task {task_id}] 音频成功保存到本地: {full_audio_path}")
# # # #             task_info["audio_path"] = task_info["public_url"]
# # # #             task_info.pop("local_path", None);
# # # #             task_info.pop("public_url", None)
# # # #             return task_info
# # # #         except Exception as e:
# # # #             if attempt < MAX_RETRIES - 1:
# # # #                 wait_time = RETRY_DELAY * (2 ** attempt)
# # # #                 logger.warning(
# # # #                     f"[Task {task_id}] 第 {attempt + 1}/{MAX_RETRIES} 次尝试失败: {e}. 将在 {wait_time} 秒后重试...")
# # # #                 time.sleep(wait_time)
# # # #             else:
# # # #                 logger.error(f"[Task {task_id}] 所有重试均失败，最终错误: {e}")
# # # #                 task_info["error"] = f"After {MAX_RETRIES} retries, final error: {str(e)}"
# # # #                 task_info.pop("local_path", None);
# # # #                 task_info.pop("public_url", None)
# # # #                 return task_info
# # # #     return task_info
# # # #
# # # #
# # # # # --- FastAPI 主接口 ---
# # # # class TTSRequestPayload(BaseModel):
# # # #     raw_script: str = Field(..., description="包含时间和字幕的原始脚本。")
# # # #     model_id: str = Field(..., description="使用的 TTS 模型ID。")
# # # #     workflow_id: str = Field(..., description="本次任务的唯一工作流ID。")
# # # #     fish_api_key: str = Field(..., description="Fish Audio 的 API Key。")
# # # #
# # # #
# # # # @router.post("/generate_audio", summary="从脚本生成音频并处理文件", response_model=Dict[str, Any])
# # # # def generate_audio_workflow(payload: TTSRequestPayload):
# # # #     raw_script, model_id, workflow_id, fish_api_key = \
# # # #         payload.raw_script, payload.model_id, payload.workflow_id, payload.fish_api_key
# # # #
# # # #     logger.warning(f"收到新的音频生成请求, workflow_id: '{workflow_id}'")
# # # #     if not all([fish_api_key, model_id, workflow_id, raw_script]):
# # # #         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="输入参数不完整。")
# # # #
# # # #     # --- 步骤 1: 使用智能解析器解析脚本并构建任务列表 ---
# # # #     parser = IntelligentParser(raw_script)
# # # #     parsed_items = parser.parse()
# # # #
# # # #     if not parsed_items:
# # # #         raise HTTPException(
# # # #             status_code=status.HTTP_400_BAD_REQUEST,
# # # #             detail="智能解析失败：在脚本中未能识别出任何有效的字幕行 (格式应为 '时间: 文本')。"
# # # #         )
# # # #
# # # #     tasks_to_process = []
# # # #     audio_save_path = AUDIO_SAVE_PATH_TEMPLATE.format(workflow_id=workflow_id)
# # # #     os.makedirs(audio_save_path, exist_ok=True)
# # # #
# # # #     for id_counter, item in enumerate(parsed_items):
# # # #         current_timestamp = item["time_range"]
# # # #         time_numbers = re.findall(r"\d+\.?\d*", current_timestamp)
# # # #         start_sec = float(time_numbers[0]) if time_numbers else 0.0
# # # #         end_sec = float(time_numbers[1]) if len(time_numbers) > 1 else 0.0
# # # #         audio_filename = f"audio_{id_counter:03d}_{start_sec:.1f}s.{AUDIO_FORMAT}"
# # # #
# # # #         tasks_to_process.append({
# # # #             "id": id_counter,
# # # #             "time_range": current_timestamp,
# # # #             "start_sec": start_sec,
# # # #             "end_sec": end_sec,
# # # #             "text": item["text"],
# # # #             "local_path": os.path.join(audio_save_path, audio_filename),
# # # #             "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
# # # #         })
# # # #
# # # #     logger.warning(f"智能解析完成，共 {len(tasks_to_process)} 个任务待处理。")
# # # #
# # # #     # --- 步骤 2: 并行执行任务 ---
# # # #     final_tasks_list = []
# # # #     try:
# # # #         session = Session(fish_api_key)
# # # #         with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS,
# # # #                                                    thread_name_prefix="TTS_Worker") as executor:
# # # #             future_to_task = {
# # # #                 executor.submit(generate_audio_single_task, session, task, model_id): task
# # # #                 for task in tasks_to_process
# # # #             }
# # # #             for future in concurrent.futures.as_completed(future_to_task):
# # # #                 final_tasks_list.append(future.result())
# # # #     except Exception as e:
# # # #         logger.error(f"并行处理任务时发生主错误: {e}")
# # # #         raise HTTPException(status_code=500, detail=f"并行处理任务时发生主错误: {e}")
# # # #
# # # #     final_tasks_list.sort(key=lambda x: x['id'])
# # # #     logger.warning("所有音频生成任务已完成处理。")
# # # #
# # # #     # --- 步骤 3: 移动文件夹 ---
# # # #     move_error = None
# # # #     if any(task.get("audio_path") for task in final_tasks_list):
# # # #         logger.warning("至少有一个音频生成成功，开始执行文件移动操作。")
# # # #         try:
# # # #             _move_workflow_directory(workflow_id)
# # # #         except (FileNotFoundError, IOError) as e:
# # # #             move_error = str(e)
# # # #             logger.error(f"文件移动操作失败: {e}. 但音频结果仍会返回。")
# # # #     else:
# # # #         logger.warning("没有任何音频生成成功，跳过文件移动操作。")
# # # #
# # # #     # --- 步骤 4: 返回最终结果 ---
# # # #     logger.warning(f"工作流 '{workflow_id}' 全部流程结束，准备返回最终结果。")
# # # #     final_result = {
# # # #         "audio_tasks": final_tasks_list,
# # # #         "audio_tasks_str": json.dumps(final_tasks_list, ensure_ascii=False, indent=2)
# # # #     }
# # # #     if move_error:
# # # #         final_result["move_operation_error"] = move_error
# # # #
# # # #     return final_result
