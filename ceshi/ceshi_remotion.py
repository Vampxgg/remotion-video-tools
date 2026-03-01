# ==============================================================================
#  Dify 知识库代码节点：智能 JSON 解析器 (Remotion 视频场景专用版)
#  Author: Optimized Version
#  Version: 3.1
#
#  核心功能:
#  1. 智能提取并修复格式不规范的 JSON (支持单引号、尾逗号、注释等)
#  2. 数据清洗：确保所有嵌套列表转换为字典，防止下游 .items() 报错
#  3. 灵活的场景数据提取，支持多种 JSON 结构
#
#  输入输出:
#  - 输入: main(json_str: str)
#  - 输出: {"result": [...]}  (保持原有结构不变)
# ==============================================================================

import json
import re
from typing import Any, Dict, List, Union

# 确保已在 Dify 的"依赖管理"中添加了 json-repair 依赖
# pip install json-repair
import json_repair


# ==============================================================================
#  核心 JSON 提取逻辑 (高鲁棒性)
# ==============================================================================
def _extract_most_significant_json(text: str) -> Union[Dict, List]:
    """
    智能地从可能包含噪音、注释或多个 JSON 片段的文本中提取最主要的 JSON 对象或列表。

    智能性体现在：
    1.  **分层解析**: 优先使用快速修复库，失败后才进行候选者提取。
    2.  **候选者识别**: 即使文本中散落着多个 JSON 片段，也能识别出它们。
    3.  **信息量最大原则**: 通过比较大小和复杂性，选择最可能符合用户意图的那个 JSON 对象。
    4.  **鲁棒性**: 结合预处理和强大的修复库，最大化解析成功率。
    """

    # --- 阶段1: 快速通道 - 尝试直接修复解析 ---
    try:
        result = json_repair.loads(text)
        # 确保返回的是 dict 或 list，而非原始字符串
        if isinstance(result, (dict, list)):
            return result
    except Exception:
        pass

    # --- 阶段2: 高级提取 - 寻找并评估候选 JSON ---
    # 使用正则匹配所有潜在的 JSON 块 (对象或数组)
    potential_json_strings = re.findall(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)

    if not potential_json_strings:
        raise ValueError("在输入文本中未能找到任何潜在的 JSON 结构 (即 {...} 或 [...] 块)")

    valid_jsons = []
    for candidate in potential_json_strings:
        try:
            parsed_json = json_repair.loads(candidate)
            if isinstance(parsed_json, (dict, list)):
                valid_jsons.append(parsed_json)
        except Exception:
            continue

    if not valid_jsons:
        raise ValueError("找到了潜在的 JSON 结构，但没有一个可以被成功修复和解析")

    # --- 智能决策：选择信息量最大的 JSON ---
    most_significant_json = max(valid_jsons, key=lambda x: len(json.dumps(x, ensure_ascii=False)))

    return most_significant_json


# ==============================================================================
#  场景数据提取 (Remotion 专用)
# ==============================================================================
def _extract_scenes(data: Union[Dict, List]) -> List:
    """
    从解析后的数据中提取 scenes 列表。
    支持多种数据结构：
    1. {"scenes": [...]} - 标准结构
    2. [...] - 直接就是场景列表
    3. {"result": {"scenes": [...]}} - 嵌套结构
    4. {"data": {"scenes": [...]}} - 另一种嵌套结构
    """
    if isinstance(data, list):
        return data
    
    if isinstance(data, dict):
        # 尝试直接获取 scenes
        if "scenes" in data:
            return data["scenes"]
        
        # 尝试从 result 中获取
        if "result" in data:
            result = data["result"]
            if isinstance(result, list):
                return result
            if isinstance(result, dict) and "scenes" in result:
                return result["scenes"]
        
        # 尝试从 data 字段中获取
        if "data" in data:
            inner_data = data["data"]
            if isinstance(inner_data, list):
                return inner_data
            if isinstance(inner_data, dict) and "scenes" in inner_data:
                return inner_data["scenes"]
        
        # 如果没有 scenes 键，检查是否整个 dict 就是一个场景
        # (包含典型场景字段如 narration, visual, duration 等)
        scene_fields = {"narration", "visual", "duration", "scene", "audio", "text", "image"}
        if any(field in data for field in scene_fields):
            return [data]
    
    return []


# ==============================================================================
#  数据清洗逻辑 (防止下游 .items() 报错)
# ==============================================================================
def _sanitize_data(data: Any) -> Any:
    """
    递归清洗数据：
    如果发现 '列表里的元素还是列表' (比如矩阵行)，
    就把它强制转换成字典 {"0": val1, "1": val2}。
    这样下游程序调用 .items() 就安全了。
    """
    if isinstance(data, dict):
        # 如果是字典，递归清洗每个 value
        return {k: _sanitize_data(v) for k, v in data.items()}
    
    elif isinstance(data, list):
        # 如果是列表，检查里面的元素
        cleaned_list = []
        for item in data:
            # 递归处理当前元素
            cleaned_item = _sanitize_data(item)
            
            # 【核心防护】如果清洗后的元素依然是列表（List[List] 结构），
            # 将其包装成字典，确保下游调用 .items() 安全
            if isinstance(cleaned_item, list):
                # 将 ["x", "y"] 转换为 {"0": "x", "1": "y"}
                item_as_dict = {str(i): v for i, v in enumerate(cleaned_item)}
                cleaned_list.append(item_as_dict)
            else:
                cleaned_list.append(cleaned_item)
        return cleaned_list
    
    else:
        # 基本数据类型 (str, int, float, bool, None)，直接返回
        return data


# ==============================================================================
#  主执行函数 (保持原有输入输出结构)
# ==============================================================================
def main(json_str: str) -> Dict[str, Any]:
    """
    Dify 代码执行节点主函数 - Remotion 视频场景专用版

    核心功能:
    1. 接受 JSON 字符串输入
    2. 智能修复格式不规范的 JSON (单引号、尾逗号、注释等)
    3. 灵活提取场景数据，支持多种 JSON 结构
    4. 数据清洗：确保嵌套列表转换为字典，防止下游 .items() 报错
    5. 输出保持原有结构: {"result": [...]}

    参数:
        json_str: 包含场景数据的 JSON 字符串

    返回:
        {"result": [...]} - 清洗后的场景列表
    """
    try:
        # --- 空输入处理 ---
        if not json_str or not json_str.strip():
            return {"result": []}

        # --- 智能 JSON 解析 ---
        parsed_data = _extract_most_significant_json(json_str)

        # --- 灵活提取场景数据 ---
        scenes = _extract_scenes(parsed_data)

        # --- 数据清洗：确保 .items() 调用安全 ---
        sanitized_scenes = _sanitize_data(scenes)

        return {"result": sanitized_scenes}

    except ValueError as e:
        # JSON 提取/解析失败
        return {"result": [], "error": str(e)}
    except Exception as e:
        # 其他意外错误
        return {"result": [], "error": f"处理失败: {e}"}