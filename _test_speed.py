# # import os
# # import time
# # import logging
# # import statistics
# # from typing import List, Dict, Any
# #
# # # 确保已安装所需库:
# # # pip install fish-audio pydub python-dotenv
# # from fish_audio_sdk import Session
# # from fish_audio_sdk import TTSRequest, Prosody
# # from pydub import AudioSegment
# #
# # # --- 1. 全局配置 (请根据您的信息修改) ---
# #
# # # 强烈建议: 不要直接写在这里，而是创建一个 .env 文件，内容为:
# # # FISH_AUDIO_API_KEY="YOUR_API_KEY_HERE"
# # # 然后运行 `pip install python-dotenv` 并取消下面两行注释
# # # from dotenv import load_dotenv
# # # load_dotenv()
# #
# # # 你的 Fish Audio API Key，从环境变量读取更安全
# # FISH_AUDIO_API_KEY = "dae51de32a0743f6b4f2f7b6366747bf"
# #
# # # 你的参考声音/模型 ID
# # REFERENCE_VOICE_ID = "5c353fdb312f4888836a9a5680099ef0"
# #
# # # 用于测试的文本样本，覆盖不同长度和常见标点
# # # 目标是模拟真实脚本中的句子
# # TEST_TEXTS = [
# #     "Hello.",
# #     "Welcome to my channel.",
# #     "Today we're going to discuss a very interesting topic.",
# #     "A binary tree is a nonlinear tree data structure.",
# #     "It consists of nodes, each of which has at most two children, usually called a left child and a right child.",
# #     "Traversal is one of the most core operations of a binary tree. It allows us to visit all nodes without duplication or omission, such as in-order traversal.",
# #     "Congratulations on completing the introduction to binary trees! In the next lesson, we'll delve into a special type of binary tree—the binary search tree.",
# # ]
# #
# # # TTS 引擎参数 (与您的主应用保持一致)
# # ENGINE_MODEL = "speech-1.6"
# # AUDIO_FORMAT = "mp3"
# # # 使用与您主应用相同的语速，以确保测试结果的相关性
# # SPEECH_SPEED = 0.9
# #
# # # API调用之间的礼貌性延迟（秒）
# # API_CALL_DELAY_SECONDS = 1.5
# #
# # # --- 2. 日志与辅助功能 ---
# #
# # logging.basicConfig(
# #     level=logging.INFO,
# #     format='%(asctime)s - [%(levelname)s] - %(message)s',
# #     datefmt='%Y-%m-%d %H:%M:%S'
# # )
# #
# #
# # def measure_audio_duration_ms(file_path: str) -> int:
# #     """使用 pydub 测量音频文件的毫秒时长"""
# #     try:
# #         audio = AudioSegment.from_file(file_path)
# #         return len(audio)
# #     except Exception as e:
# #         logging.error(f"Pydub 无法加载或测量文件 '{file_path}': {e}")
# #         return 0
# #
# #
# # # --- 3. 核心测试函数 ---
# #
# # def test_speech_rate():
# #     """
# #     主测试函数，用于调用 Fish Audio API，生成音频并计算平均语速。
# #     """
# #     if not FISH_AUDIO_API_KEY or "YOUR_API_KEY_HERE" in FISH_AUDIO_API_KEY:
# #         logging.error("错误: 请在脚本顶部或 .env 文件中设置您的 FISH_AUDIO_API_KEY。")
# #         return
# #
# #     if not REFERENCE_VOICE_ID or "YOUR_REFERENCE_VOICE_ID_HERE" in REFERENCE_VOICE_ID:
# #         logging.error("错误: 请在脚本顶部设置您的 REFERENCE_VOICE_ID。")
# #         return
# #
# #     # 创建一个临时目录来存放音频文件
# #     temp_dir = "temp_audio_test"
# #     os.makedirs(temp_dir, exist_ok=True)
# #     logging.info(f"临时文件将存放在 '{temp_dir}/' 目录下。")
# #
# #     results = []
# #
# #     try:
# #         with Session(FISH_AUDIO_API_KEY) as session:
# #             for i, text in enumerate(TEST_TEXTS):
# #                 char_count = len(text)
# #                 temp_audio_path = os.path.join(temp_dir, f"test_{i}.{AUDIO_FORMAT}")
# #
# #                 logging.info(f"\n--- 测试样本 {i + 1}/{len(TEST_TEXTS)} ---")
# #                 logging.info(f"文本: '{text}' ({char_count} 个字符)")
# #
# #                 try:
# #                     # 1. 构建并发送TTS请求 (完全参考您的代码)
# #                     req = TTSRequest(
# #                         text=text,
# #                         reference_id=REFERENCE_VOICE_ID,
# #                         model=ENGINE_MODEL,
# #                         format=AUDIO_FORMAT,
# #                         prosody=Prosody(speed=SPEECH_SPEED)
# #                     )
# #
# #                     logging.info("正在请求 Fish Audio API...")
# #                     with open(temp_audio_path, "wb") as f:
# #                         for chunk in session.tts(req):
# #                             f.write(chunk)
# #
# #                     # 2. 验证并测量生成的音频
# #                     if not os.path.exists(temp_audio_path) or os.path.getsize(temp_audio_path) == 0:
# #                         logging.warning("API 调用成功，但生成的音频文件为空。跳过此样本。")
# #                         continue
# #
# #                     duration_ms = measure_audio_duration_ms(temp_audio_path)
# #                     if duration_ms == 0:
# #                         logging.warning("无法测量音频时长。跳过此样本。")
# #                         continue
# #
# #                     duration_sec = duration_ms / 1000.0
# #
# #                     # 3. 计算并记录结果
# #                     # 核心指标：Characters Per Second (CPS)
# #                     cps = char_count / duration_sec
# #                     logging.info(f"成功！生成音频时长: {duration_sec:.2f} 秒。")
# #                     logging.info(f"计算得出的语速: {cps:.2f} 字/秒。")
# #
# #                     results.append({
# #                         "text": text,
# #                         "char_count": char_count,
# #                         "duration_sec": duration_sec,
# #                         "cps": cps
# #                     })
# #
# #                 except Exception as e:
# #                     logging.error(f"处理样本时发生错误: {e}")
# #                 finally:
# #                     # 礼貌性延迟
# #                     time.sleep(API_CALL_DELAY_SECONDS)
# #
# #     finally:
# #         pass
# #         # # 4. 清理临时文件
# #         # for file_name in os.listdir(temp_dir):
# #         #     os.remove(os.path.join(temp_dir, file_name))
# #         # os.rmdir(temp_dir)
# #         # logging.info(f"已清理临时目录 '{temp_dir}'。")
# #
# #     # 5. 汇总并报告最终结果
# #     if not results:
# #         logging.error("测试未能成功收集任何数据点。请检查API Key和网络连接。")
# #         return
# #
# #     all_cps = [r['cps'] for r in results]
# #     average_cps = statistics.mean(all_cps)
# #
# #     print("\n\n" + "=" * 50)
# #     print(" " * 18 + "测试结果总结")
# #     print("=" * 50)
# #     print(f"{'样本序号':<8} {'字符数':<8} {'音频时长(s)':<12} {'语速(字/秒)':<12}")
# #     print("-" * 50)
# #     for i, res in enumerate(results):
# #         print(f"{i + 1:<8} {res['char_count']:<8} {res['duration_sec']:<12.2f} {res['cps']:<12.2f}")
# #     print("=" * 50)
# #
# #     print("\n[最终结论]")
# #     print(f"在 {len(results)} 个样本的测试中（语速设置为 {SPEECH_SPEED}）:")
# #     print(f"平均语速为: {average_cps:.2f} 字/秒")
# #     print(f"这意味着，1秒钟的音频大约对应 【 {average_cps:.2f} 】个汉字。")
# #     print(f"反过来，生成1个汉字的音频大约需要 {1 / average_cps:.3f} 秒。")
# #     print("=" * 50)
# #
# #
# # # --- 4. 脚本执行入口 ---
# # if __name__ == "__main__":
# #     test_speech_rate()
#
# import os
# import re
# import time
# import logging
# import statistics
# import datetime
# from typing import List, Dict, Any, Tuple
#
# # 确保已安装所需库:
# # pip install fish-audio pydub python-dotenv
# from fish_audio_sdk import Session
# from fish_audio_sdk import TTSRequest, Prosody
# from pydub import AudioSegment
# from dotenv import load_dotenv
#
# FISH_AUDIO_API_KEY = "dae51de32a0743f6b4f2f7b6366747bf"
# REFERENCE_VOICE_ID = "5c353fdb312f4888836a9a5680099ef0"  # <--- !! 请务必修改这里 !!
#
# # 覆盖中英文的测试样本
# TEST_TEXTS = [
#     # ---- Short Phrases ----
#     "Hello, world.",  # 2 words
#     "This is a simple test.",  # 5 words
#     "How is the weather today?",  # 5 words
#
#     # ---- Medium Sentences ----
#     "I am evaluating the performance and stability of this speech synthesis engine.",  # 13 words
#     "Artificial intelligence is changing our lives at an unprecedented speed.",  # 11 words
#     "Please note that the system will undergo routine maintenance at midnight.",  # 12 words
#
#     # ---- Long & Complex Sentences ----
#     "Data visualization is the process of converting data into graphics or charts to communicate information clearly and effectively.",
#     # 21 words
#     "In the field of machine learning, supervised learning is a common paradigm that uses labeled training data to build predictive models.",
#     # 23 words
#     "To ensure software quality, we need to establish a complete automated testing process, including unit tests, integration tests, and end-to-end tests.",
#     # 27 words
#
#     # ---- Paragraph-level Test ----
#     "The main objective of this project is to develop an efficient, scalable, and user-friendly online collaboration platform. We believe this goal is achievable through our team's collective effort."
#     # 32 words
# ]
#
# ENGINE_MODEL = "speech-1.6"
# AUDIO_FORMAT = "mp3"
# SPEECH_SPEED = 0.9
# API_CALL_DELAY_SECONDS = 1.5
#
# # --- 2. 日志系统设置 (包含文件记录) ---
#
# # 创建一个带时间戳的日志文件名
# log_filename = f"speech_rate_test_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
#
# # 创建 logger
# logger = logging.getLogger('SpeechRateTester')
# logger.setLevel(logging.DEBUG)  # 记录所有级别的日志
#
# # 创建文件处理器 (FileHandler)
# file_handler = logging.FileHandler(log_filename, encoding='utf-8')
# file_handler.setLevel(logging.DEBUG)
#
# # 创建控制台处理器 (StreamHandler)
# console_handler = logging.StreamHandler()
# console_handler.setLevel(logging.INFO)  # 控制台只显示 INFO 及以上级别
#
# # 创建日志格式
# formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
# file_handler.setFormatter(formatter)
# console_handler.setFormatter(formatter)
#
# # 将处理器添加到 logger
# logger.addHandler(file_handler)
# logger.addHandler(console_handler)
#
#
# # --- 3. 核心功能升级 ---
#
# def measure_audio_duration_ms(file_path: str) -> int:
#     """使用 pydub 测量音频文件的毫秒时长"""
#     try:
#         audio = AudioSegment.from_file(file_path)
#         return len(audio)
#     except Exception as e:
#         logger.error(f"Pydub 无法加载或测量文件 '{file_path}': {e}")
#         return 0
#
#
# def count_speech_units(text: str) -> Tuple[int, int, int]:
#     """
#     智能地计算文本中的“语音单元”。
#     - 中文部分: 计汉字数
#     - 英文部分: 计单词数
#     返回: (汉字数, 单词数, 总单元数)
#     """
#     # 匹配所有中文字符
#     chinese_chars = re.findall(r'[\u4e00-\u9fa5]', text)
#     num_chinese_chars = len(chinese_chars)
#
#     # 匹配所有英文单词 (由字母组成)
#     english_words = re.findall(r'\b[a-zA-Z]+\b', text)
#     num_english_words = len(english_words)
#
#     total_units = num_chinese_chars + num_english_words
#     return num_chinese_chars, num_english_words, total_units
#
#
# def test_speech_rate_advanced():
#     """
#     执行严谨的语速测试，并提供详细的统计分析和日志记录。
#     """
#     logger.info("=" * 60)
#     logger.info("启动高级语速测试（支持中英混合统计与日志记录）")
#     logger.info(f"日志将保存在: {log_filename}")
#     logger.info("=" * 60)
#
#     if not FISH_AUDIO_API_KEY:
#         logger.critical("致命错误: 环境变量 FISH_AUDIO_API_KEY 未设置或为空。请检查 .env 文件。")
#         return
#     if not REFERENCE_VOICE_ID or "YOUR" in REFERENCE_VOICE_ID:
#         logger.critical("致命错误: 请在脚本中设置您的 REFERENCE_VOICE_ID。")
#         return
#
#     temp_dir = "temp_audio_test"
#     os.makedirs(temp_dir, exist_ok=True)
#     logger.info(f"临时音频文件将存放在 '{temp_dir}/' 目录下。")
#
#     results = []
#
#     try:
#         with Session(FISH_AUDIO_API_KEY) as session:
#             for i, text in enumerate(TEST_TEXTS):
#                 num_cn, num_en, total_units = count_speech_units(text)
#                 temp_audio_path = os.path.join(temp_dir, f"test_{i}.{AUDIO_FORMAT}")
#
#                 logger.info(f"\n--- 测试样本 {i + 1}/{len(TEST_TEXTS)} ---")
#                 logger.info(f"原文: '{text}'")
#                 logger.info(f"解析: {num_cn}个汉字, {num_en}个英文单词, 共 {total_units} 个语音单元。")
#
#                 if total_units == 0:
#                     logger.warning("文本不含有效语音单元，跳过。")
#                     continue
#
#                 try:
#                     req = TTSRequest(text=text, reference_id=REFERENCE_VOICE_ID, model=ENGINE_MODEL,
#                                      format=AUDIO_FORMAT, prosody=Prosody(speed=SPEECH_SPEED))
#
#                     logger.info("正在向 Fish Audio API 发送请求...")
#                     with open(temp_audio_path, "wb") as f:
#                         for chunk in session.tts(req):
#                             f.write(chunk)
#
#                     if not os.path.exists(temp_audio_path) or os.path.getsize(temp_audio_path) == 0:
#                         logger.warning("API 调用成功，但生成的音频文件为空。跳过此样本。")
#                         continue
#
#                     duration_ms = measure_audio_duration_ms(temp_audio_path)
#                     if duration_ms == 0:
#                         continue
#
#                     duration_sec = duration_ms / 1000.0
#
#                     # 核心指标: Units Per Second (UPS)
#                     ups = total_units / duration_sec
#                     logger.info(f"成功！生成音频时长: {duration_sec:.3f} 秒。")
#                     logger.info(f"计算得出的混合速率: {ups:.3f} 语音单元/秒。")
#
#                     results.append({"units": total_units, "duration_sec": duration_sec, "ups": ups})
#
#                 except Exception as e:
#                     logger.error(f"处理样本时发生错误: {e}", exc_info=True)  # exc_info=True 会记录完整的错误堆栈
#                 finally:
#                     time.sleep(API_CALL_DELAY_SECONDS)
#
#     finally:
#         pass
#         # if os.path.exists(temp_dir):
#         #     for file_name in os.listdir(temp_dir):
#         #         os.remove(os.path.join(temp_dir, file_name))
#         #     os.rmdir(temp_dir)
#         #     logger.info(f"已清理临时目录 '{temp_dir}'。")
#
#     if not results:
#         logger.error("测试未能成功收集任何数据点。请检查API Key、模型ID和网络连接。")
#         return
#
#     all_ups = [r['ups'] for r in results]
#
#     # --- 全面统计分析 ---
#     mean_ups = statistics.mean(all_ups)
#     median_ups = statistics.median(all_ups)
#     # 仅当数据点多于1个时才计算标准差
#     stdev_ups = statistics.stdev(all_ups) if len(all_ups) > 1 else 0.0
#
#     # 使用 logger 打印，这样也会记录到文件中
#     logger.info("\n\n" + "=" * 60)
#     logger.info(" " * 22 + "最终测试报告")
#     logger.info("=" * 60)
#     summary_header = f"{'样本序号':<8} {'语音单元数':<12} {'音频时长(s)':<14} {'速率(单元/秒)':<15}"
#     logger.info(summary_header)
#     logger.info("-" * 60)
#     for i, res in enumerate(results):
#         summary_line = f"{i + 1:<8} {res['units']:<12} {res['duration_sec']:<14.3f} {res['ups']:<15.3f}"
#         logger.info(summary_line)
#     logger.info("=" * 60)
#
#     logger.info("\n[统计分析摘要]")
#     logger.info(f"测试样本总数: {len(results)}")
#     logger.info(f"TTS语速设定值: {SPEECH_SPEED}")
#     logger.info("-" * 30)
#     logger.info(f"平均速率 (Mean):         {mean_ups:.3f} 语音单元/秒")
#     logger.info(f"中位数速率 (Median):      {median_ups:.3f} 语音单元/秒 (更能代表通常情况)")
#     logger.info(f"标准差 (Std Dev):         {stdev_ups:.3f} (值越小，语速越稳定)")
#     logger.info("-" * 30)
#     logger.info("\n[最终结论]")
#     logger.info(
#         f"综合来看，对于此模型（ID: {REFERENCE_VOICE_ID}），您可以将【 {median_ups:.2f} 个语音单元/秒 】作为最可靠的估算基准。")
#     logger.info("（语音单元 = 汉字数 + 英文单词数）")
#     logger.info("=" * 60)
#     logger.info(f"测试完成，详细日志已保存至 {log_filename}")
#
#
# if __name__ == "__main__":
#     test_speech_rate_advanced()
# -*- coding: utf-8 -*-
import os
import re
import time
import logging
import statistics
import datetime
import math
from collections import defaultdict
from typing import List, Dict, Any, Tuple

from dotenv import load_dotenv

load_dotenv()  # [改] 使用 .env 或环境变量来存 API Key

FISH_AUDIO_API_KEY = os.getenv("FISH_AUDIO_API_KEY", "")  # [改] 不要硬编码到脚本
REFERENCE_VOICE_ID = os.getenv("REFERENCE_VOICE_ID", "")  # [改] 从 env 获取

# 注意: pydub 需要 ffmpeg 安装在系统中
try:
    from pydub import AudioSegment
except Exception as e:
    AudioSegment = None

# ---- 改进的 tokenization ----
# 更宽的 CJK 匹配（包含扩展与繁体部分）
_cjk_re = re.compile(r'[\u2E80-\u2EFF\u2F00-\u2FDF\u3040-\u30FF\u3100-\u312F\u3200-\u9FFF\uF900-\uFAFF]')


def split_chinese_chars(text: str) -> List[str]:
    """返回中文字符（更宽泛的范围），保留英文/数字可选（当前不计入中文）。"""
    return [ch for ch in text if _cjk_re.match(ch)]


def split_english_words(text: str) -> List[str]:
    """更稳健的英文单词分词：支持缩写、数字、连字符"""
    # 允许内部撇号和连字符，允许数字（例如 v2.0 可能视为 token）
    words = re.findall(r"[A-Za-z0-9]+(?:[\'\-][A-Za-z0-9]+)*", text)
    return words


def count_speech_units_improved(text: str) -> Tuple[int, int, int]:
    cn = len(split_chinese_chars(text))
    en = len(split_english_words(text))
    return cn, en, cn + en


# ---- 按秒分配（fractional/discrete） ----
def compute_per_second_counts(tokens: List[Dict[str, Any]], lang='zh', mode='fractional'):
    """
    tokens: [{'text':str,'start':float,'end':float}, ...]
    returns: dict second_index->float_count, total_seconds
    mode: 'fractional' or 'discrete'
    """
    if not tokens:
        return {}, 0
    # ensure float
    for t in tokens:
        t['start'] = float(t['start'])
        t['end'] = float(t['end'])
        if t['end'] <= t['start']:
            t['end'] = t['start'] + 0.001

    max_end = max(t['end'] for t in tokens)
    total_seconds = int(math.ceil(max_end))
    counts = defaultdict(float)

    for t in tokens:
        s = t['start'];
        e = t['end']
        if lang == 'zh':
            unit_count = len(split_chinese_chars(t['text']))
        else:
            unit_count = len(split_english_words(t['text']))
        if unit_count == 0:
            continue

        if mode == 'discrete':
            sec = int(math.floor(s))
            counts[sec] += unit_count
        else:  # fractional
            token_dur = e - s
            s0 = int(math.floor(s))
            s1 = int(math.floor(e))
            for sec in range(s0, s1 + 1):
                seg_start = max(s, sec)
                seg_end = min(e, sec + 1.0)
                overlap = max(0.0, seg_end - seg_start)
                if overlap > 0:
                    frac = overlap / token_dur
                    counts[sec] += unit_count * frac

    # fill zeros
    result = {sec: counts.get(sec, 0.0) for sec in range(total_seconds)}
    return result, total_seconds


# ---- 测量音频时长（保持你的实现，但加检测） ----
def measure_audio_duration_ms(file_path: str) -> int:
    if AudioSegment is None:
        raise RuntimeError("pydub/ffmpeg 未安装或导入失败。请确保已安装 ffmpeg 并安装 pydub。")
    audio = AudioSegment.from_file(file_path)
    return len(audio)


# ---- 主流程示意（替换你原来的 test_speech_rate_advanced 中关键部分） ----
def process_one_sample_with_possible_timestamps(session, text, tmp_path, idx):
    """
    1) 调用 fish audio 生成音频（与你原逻辑相同）
    2) 如果 TTS 返回 token timestamps（假设 SDK/回调提供），使用 compute_per_second_counts
    3) 否则返回整体 UPS（保持原行为）
    """
    # 构造请求（保持你的代码风格）
    # 下面的示例假设 session.tts(req) 仍然返回二进制 chunk 写入文件。
    # 但我们会尝试读取 token timestamps（如果 SDK 提供）。具体字段名视 SDK 而定。
    temp_audio_path = tmp_path
    # === 生成音频 ===
    # 你原有的请求构造
    from fish_audio_sdk import TTSRequest, Prosody
    req = TTSRequest(text=text, reference_id=REFERENCE_VOICE_ID, model="speech-1.6",
                     format="mp3", prosody=Prosody(speed=0.9))
    timestamps_tokens = None
    # 如果 SDK 支持同时返回 token timestamps，通常是另一个接口或回调。
    # 这里保守写法：先写音频文件，再检查是否有 session.get_last_timestamps() 之类方法（伪）
    with open(temp_audio_path, "wb") as f:
        for chunk in session.tts(req):
            # chunk 可能只包含音频字节流；若 SDK 在 chunk 中携带 metadata，需要按 SDK 文档解析
            f.write(chunk)
    # ==== 尝试从 SDK 或 session 获取 token timestamps（伪示例） ====
    # if hasattr(session, "last_timestamps"):
    #     timestamps_tokens = session.last_timestamps  # 期待格式 [{'text','start','end'}...]

    # 测时长
    duration_ms = measure_audio_duration_ms(temp_audio_path)
    duration_sec = duration_ms / 1000.0

    # 如果我们拿到了 token timestamps，则可以做逐秒统计
    if timestamps_tokens:
        # 采用 fractional 分配（更精确）
        per_sec, total_secs = compute_per_second_counts(timestamps_tokens, lang='zh', mode='fractional')
        return {'type': 'per_second', 'per_second': per_sec, 'total_secs': total_secs, 'duration_sec': duration_sec}
    else:
        # fallback: 只能给出平均 UPS
        cn, en, total_units = count_speech_units_improved(text)
        ups = total_units / max(1e-6, duration_sec)
        return {'type': 'overall', 'units': total_units, 'duration_sec': duration_sec, 'ups': ups}


# ---- 提示：如何获得 timestamps（若 fish audio 不直接支持） ----
SSML_MARK_HINT = """
如果 fish audio 支持 SSML <mark> 或 streaming token 回调，请在请求中插入 mark：
举例（伪）: <speak> <mark name="w0" />Hello<mark name="w1" /> world</speak>
服务会在合成时回调每个 mark 的时间，作为 token 的 start/end。
若服务不支持，请合成音频后用 forced-aligner(aeneas/gentle/MFA)对齐获取时间戳。
"""

# -------------- 使用说明 --------------
# 1) 将 API key 与 voice id 放入环境变量 FISH_AUDIO_API_KEY, REFERENCE_VOICE_ID
# 2) 若要逐秒统计，优先启用 TTS 的时间戳（SSML marks 或 stream token timestamps）
# 3) 若没有，则使用 aeneas/gentle 做 forced-alignment，得到 tokens 后调用 compute_per_second_counts
