# -*- coding: utf-8 -*-
# @File：main_app.py
# @Time：2025/8/5 18:30
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com

# --- 导入模块 (与V8版相同) ---
import re
import json
import os
import shutil
import logging
import concurrent.futures
import time
from typing import Dict, List, Any, Tuple
import requests
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from murf import Murf

try:
    from murf.exceptions import MurfException
except ImportError:
    MurfException = Exception
logger = logging.getLogger(__name__)
# ... (所有配置项保持不变，此处省略以保持清晰) ...
router = APIRouter()
PROXY_URL = "http://127.0.0.1:7890"
if PROXY_URL:
    os.environ['HTTP_PROXY'], os.environ['HTTPS_PROXY'] = PROXY_URL, PROXY_URL
    logger.info(f"已配置全局代理: {PROXY_URL}")
AUDIO_FORMAT = "mp3"
PUBLIC_URL_TEMPLATE = "http://119.45.167.133:7752/meta-doc/video/{workflow_id}/audio/{filename}"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SOURCE_DIR_TEMPLATE = os.path.join(STATIC_DIR, "file", "{workflow_id}")
AUDIO_SAVE_PATH_TEMPLATE = os.path.join(SOURCE_DIR_TEMPLATE, "audio")
DEST_BASE_DIR = "/www/wwwroot/x-pilot-oss/uploads/meta-doc/video"
MAX_WORKERS = 5
MAX_RETRIES = 3
RETRY_DELAY = 2


# --- V4 智能解析器类 (保持不变) ---
class IntelligentParser:
    # ... (内部代码完全不变，此处省略) ...
    def __init__(self, raw_script: str):
        self.script_lines = raw_script.splitlines()
        self.subtitle_pattern = re.compile(r"^\s*(\d+\.?\d*\s*秒\s*-\s*\d+\.?\d*\s*秒)\s*:\s*(.+)$")
        self.title_pattern = re.compile(r"^(#+.*|【.*】|\*\*.*\*\*|##\s.*)$")
        self.positive_keywords = {"字幕": 5, "脚本": 3, "script": 3, "subtitle": 5, "text": 2, "文本": 2}
        self.negative_keywords = {"分镜": -4, "视频": -4, "画面": -4, "视觉": -4, "场景": -4, "video": -4, "visual": -4}

    def _find_all_potential_subtitles(self) -> List[Tuple[int, Dict[str, str]]]:
        potential_subs = []
        for i, line in enumerate(self.script_lines):
            match = self.subtitle_pattern.match(line.strip())
            if match:
                potential_subs.append((i, {"time_range": match.group(1).strip(), "text": match.group(2).strip()}))
        return potential_subs

    def _cluster_subtitles(self, potential_subs: List[Tuple[int, Dict[str, str]]]) -> List[
        List[Tuple[int, Dict[str, str]]]]:
        if not potential_subs: return []
        clusters, current_cluster = [], [potential_subs[0]]
        for i in range(1, len(potential_subs)):
            if potential_subs[i][0] - potential_subs[i - 1][0] <= 3:
                current_cluster.append(potential_subs[i])
            else:
                clusters.append(current_cluster)
                current_cluster = [potential_subs[i]]
        clusters.append(current_cluster)
        return clusters

    def _score_cluster(self, cluster: List[Tuple[int, Dict[str, str]]]) -> Tuple[int, List[Dict[str, str]]]:
        start_line_num, score = cluster[0][0], len(cluster)
        for i in range(start_line_num - 1, max(-1, start_line_num - 6), -1):
            line = self.script_lines[i].lower().strip()
            if self.title_pattern.match(line) or len(line) < 15:
                for keyword, value in {**self.positive_keywords, **self.negative_keywords}.items():
                    if keyword in line: score += value
                break
        return score, [item[1] for item in cluster]

    def parse(self) -> List[Dict[str, str]]:
        logger.info("启动V4智能解析器...")
        potential_subs = self._find_all_potential_subtitles()
        if not potential_subs: return []
        clusters = self._cluster_subtitles(potential_subs)
        scored_clusters = [self._score_cluster(c) for c in clusters]
        if not scored_clusters: return []
        best_cluster = max(scored_clusters, key=lambda item: item[0])
        if best_cluster[0] < 5: return [item[1] for item in potential_subs]
        return best_cluster[1]


# --- 内部辅助函数：移动目录 (保持不变) ---
def _move_workflow_directory(workflow_id: str):
    # ... (内部代码完全不变) ...
    source_dir = SOURCE_DIR_TEMPLATE.format(workflow_id=workflow_id)
    dest_path = os.path.join(DEST_BASE_DIR, workflow_id)
    if not os.path.exists(source_dir):
        raise FileNotFoundError(f"源目录 '{source_dir}' 不存在。")
    try:
        if os.path.exists(dest_path): shutil.rmtree(dest_path)
        shutil.move(source_dir, DEST_BASE_DIR)
        logger.info(f"成功移动文件夹: 从 '{source_dir}' 到 '{dest_path}'")
    except Exception as e:
        raise IOError(f"移动文件夹时发生错误: {str(e)}")


# ======================================================================================
# --- [V9核心修改] 最终修正版的 Murf API 工作函数 ---
# ======================================================================================
def generate_audio_single_task(client: Murf, task_info: Dict[str, Any], voice_id: str) -> Dict[str, Any]:
    task_id, subtitle_text, full_audio_path = task_info["id"], task_info["text"], task_info["local_path"]
    logger.info(f"[Task {task_id}] 开始处理 (Murf): '{subtitle_text[:30]}...'")

    target_duration = round(task_info["end_sec"] - task_info["start_sec"], 2)
    if target_duration <= 0:
        task_info.update({"audio_path": None, "error": "Invalid target duration."})
        task_info.pop("local_path", None);
        task_info.pop("public_url", None)
        return task_info

    for attempt in range(MAX_RETRIES):
        try:
            # 步骤 1: 生成音频任务，获取响应
            response_obj = client.text_to_speech.generate(
                text=subtitle_text, voice_id=voice_id,
                audio_duration=target_duration, format=AUDIO_FORMAT
            )

            # [调试技巧] 打印出响应对象的所有属性，以便查看其真实结构
            # 如果遇到问题，取消这行注释，就能看到 'audioFile' 字段
            # logger.info(f"[Task {task_id}] Murf API 响应对象: {vars(response_obj)}")

            # === [V9核心修正] ===
            # 从响应中提取URL。根据您提供的API文档，字段名为 `audioFile`
            audio_url = getattr(response_obj, 'audioFile', None)  # 修正：使用 'audioFile'

            if not audio_url:
                # API返回200 OK但响应体中没有audioFile，这可能是一个警告或特殊情况
                warning_msg = getattr(response_obj, 'warning', 'No warning message.')
                raise MurfException(f"Murf API did not return 'audioFile' URL. Warning: {warning_msg}")

            # 步骤 2: 下载音频
            logger.info(f"[Task {task_id}] 获取到下载链接，正在下载...")
            audio_data_response = requests.get(audio_url, timeout=60,
                                               proxies={"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None)
            audio_data_response.raise_for_status()

            with open(full_audio_path, "wb") as f:
                f.write(audio_data_response.content)

            logger.info(f"[Task {task_id}] Murf 音频成功保存到: {full_audio_path}")

            task_info["audio_path"] = task_info["public_url"]
            task_info.pop("local_path", None);
            task_info.pop("public_url", None)
            return task_info

        except (MurfException, requests.exceptions.RequestException, Exception) as e:
            if attempt < MAX_RETRIES - 1:
                wait_time = RETRY_DELAY * (2 ** attempt)
                logger.warning(f"[Task {task_id}] 流程失败 ({attempt + 1}/{MAX_RETRIES}): {e}. {wait_time}s 后重试...")
                time.sleep(wait_time)
            else:
                logger.error(f"[Task {task_id}] 所有重试均失败，最终错误: {e}")
                task_info.update({"audio_path": None, "error": f"Failed after {MAX_RETRIES} retries: {str(e)}"})
                break

    task_info.pop("local_path", None);
    task_info.pop("public_url", None)
    return task_info


# --- FastAPI 主接口 (与V8版相同) ---
class MurfTTSRequestPayload(BaseModel):
    # ... (定义不变) ...
    raw_script: str = Field(..., description="包含时间和字幕的原始脚本。")
    voice_id: str = Field(..., description="要使用的 Murf 声音ID (例如 'en-US-miles')。")
    workflow_id: str = Field(..., description="本次任务的唯一工作流ID。")
    murf_api_key: str = Field(..., description="您的 Murf API Key。")


@router.post("/generate_audio_murf", summary="[V9] 使用Murf生成精准时长音频(最终修正版)", response_model=Dict[str, Any])
def generate_audio_workflow_murf(payload: MurfTTSRequestPayload):
    # ... (此函数的其余部分与 V8 完全相同，无需修改，此处省略) ...
    raw_script, voice_id, workflow_id, murf_api_key = \
        payload.raw_script, payload.voice_id, payload.workflow_id, payload.murf_api_key

    if not all([murf_api_key, voice_id, workflow_id, raw_script]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="输入参数不完整。")

    audio_save_path = AUDIO_SAVE_PATH_TEMPLATE.format(workflow_id=workflow_id)
    os.makedirs(audio_save_path, exist_ok=True)

    parser = IntelligentParser(raw_script)
    parsed_items = parser.parse()
    if not parsed_items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="智能解析失败。")

    tasks_to_process = []
    for id_counter, item in enumerate(parsed_items):
        time_numbers = re.findall(r"(\d+\.?\d*)", item["time_range"])
        start_sec, end_sec = (float(t) for t in time_numbers) if len(time_numbers) > 1 else (0.0, 0.0)
        audio_filename = f"audio_{id_counter:03d}_{start_sec:.1f}s.{AUDIO_FORMAT}"
        tasks_to_process.append({
            "id": id_counter, "time_range": item["time_range"], "start_sec": start_sec, "end_sec": end_sec,
            "text": item["text"], "local_path": os.path.join(audio_save_path, audio_filename),
            "public_url": PUBLIC_URL_TEMPLATE.format(workflow_id=workflow_id, filename=audio_filename)
        })
    logger.info(f"解析完成，共 {len(tasks_to_process)} 个任务待处理。")

    final_tasks_list = []
    try:
        murf_client = Murf(api_key=murf_api_key)
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                                   thread_name_prefix="TTS_Worker_Murf") as executor:
            future_to_task = {
                executor.submit(generate_audio_single_task, murf_client, task, voice_id): task
                for task in tasks_to_process
            }
            for future in concurrent.futures.as_completed(future_to_task):
                final_tasks_list.append(future.result())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"并发处理时发生主错误: {e}")

    final_tasks_list.sort(key=lambda x: x['id'])

    move_error = None
    if any(task.get("audio_path") for task in final_tasks_list):
        try:
            _move_workflow_directory(workflow_id)
        except (FileNotFoundError, IOError) as e:
            move_error = str(e)
    else:
        logger.warning("无音频生成，跳过文件移动。")

    final_result = {"audio_tasks": final_tasks_list,
                    "audio_tasks_str": json.dumps(final_tasks_list, ensure_ascii=False, indent=2)}
    if move_error: final_result["move_operation_error"] = move_error
    return final_result
