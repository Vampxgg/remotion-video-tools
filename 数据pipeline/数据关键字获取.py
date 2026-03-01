# -*- coding: utf-8 -*-
from typing import Any, List, Dict, Union
from copy import deepcopy

def get_default_web_queries_structure() -> Dict[str, Any]:
    """
    定义并返回 web_queries 的默认模板结构。
    """
    return {
        "comprehensive_query": [],
        "career_query": {},
        "tianyan_check_enterprise": ""
    }

def main(input_array: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    一个真正健壮且通用的查询任务分发器。
    它能正确处理 web_queries 为字典、列表或不存在等多种情况，
    并将 hybrid_queries 智能地合并进去。
    """
    local_queries_output = []
    web_queries_output = []

    if not isinstance(input_array, list) or not input_array:
        return {"local_output": [], "web_output": []}

    try:
        # 动态ID识别逻辑保持不变，它本身是健壮的
        first_item_keys = list(input_array[0].keys())
        known_data_keys = {'local_queries', 'web_queries', 'hybrid_queries'}
        id_key = next((key for key in first_item_keys if key not in known_data_keys), "id") # 默认 'id'
    except (IndexError, AttributeError):
        return {"local_output": [], "web_output": []}

    for item in input_array:
        id_value = item.get(id_key)
        
        local_queries = item.get("local_queries") or []
        web_queries = item.get("web_queries")
        hybrid_queries = item.get("hybrid_queries") or []

        # --- 1. Local Queries (逻辑不变) ---
        final_local_queries = list(dict.fromkeys(local_queries + hybrid_queries))
        local_obj = {id_key: id_value, "local_queries": final_local_queries}
        local_queries_output.append(local_obj)

        # --- 2. Web Queries (【核心修正】采用更精细的逻辑分支) ---
        
        base_web_structure: Dict[str, Any]

        # 步骤 A: 根据 web_queries 的真实类型，确定基础结构
        if isinstance(web_queries, dict):
            # 情况1: web_queries 是一个字典，直接作为模板基础
            print(f"[{id_value}] Web queries type: dict. Using as base.")
            base_web_structure = deepcopy(web_queries)
        
        elif isinstance(web_queries, list):
            # 【关键修正】情况2: web_queries 是一个列表
            # 创建默认模板，并将列表内容填充到 'comprehensive_query'
            print(f"[{id_value}] Web queries type: list. Populating default structure.")
            base_web_structure = get_default_web_queries_structure()
            # 使用去重后的 web_queries 列表填充
            base_web_structure["comprehensive_query"] = list(dict.fromkeys(web_queries))
            
        else:
            # 情况3: web_queries 是 None 或其他类型，视为缺失
            # 直接创建一个空的默认模板
            print(f"[{id_value}] Web queries is missing or invalid type. Creating default structure.")
            base_web_structure = get_default_web_queries_structure()

        # 步骤 B: 将 hybrid_queries 智能合并到已确定的基础结构中
        target_key = "comprehensive_query"
        # 确保目标字段存在且为列表类型，再进行合并
        if target_key in base_web_structure and isinstance(base_web_structure.get(target_key), list):
            existing_queries = base_web_structure[target_key]
            # 合并 hybrid_queries 并去重
            base_web_structure[target_key] = list(dict.fromkeys(existing_queries + hybrid_queries))

        # 步骤 C: 构建最终输出对象
        web_obj = {id_key: id_value, "web_queries": base_web_structure}
        web_queries_output.append(web_obj)

    return {
        "local_output": local_queries_output,
        "web_output": web_queries_output
    }