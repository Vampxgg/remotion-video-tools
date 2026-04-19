import requests
import json
from typing import Dict, Any, Optional


def main(user_id: str,
         run_id: Optional[str] = None,
         agent_id: Optional[str] = None,
         user_input: Optional[str] = None,
         llm_output: Optional[str] = None,
         action: Optional[str] = "add",
         memory_type: Optional[str] = None) -> Dict[str, Any]:
    """
    Dify 通用记忆管理节点 (Universal Memory Manager)

    Args:
        user_id (str): 用户唯一标识 (Required)
        run_id (str, optional): 会话/运行标识 (用于 L3 级隔离)
        agent_id (str, optional): 智能体/角色标识 (用于 Agent 级隔离)
        user_input (str, optional): 用户的原始输入内容
        llm_output (str, optional): 智能体的输出或思考内容
        action (str, optional): 操作类型 -> 'add' | 'search' | 'delete'。默认为 'add'。
        memory_type (str, optional): 记忆类型，如 'procedural_memory'。

    Returns:
        Dict: 统一包含 'status', 'message', 'data', 'raw' 字段
    """

    MEM0_API_URL = "http://35.232.154.66:8890/memories"

    final_response = {
        "status": "success",
        "message": "",
        "data": "",
        "raw": {},
        "debug": {}  # 新增调试字段
    }

    try:
        # 1. 参数预处理
        # 记录原始输入类型用于调试
        final_response["debug"]["input_types"] = {
            "user_id": str(type(user_id)),
            "action": str(type(action)),
            "user_input": str(type(user_input))
        }

        user_id = str(user_id) if user_id is not None else ""
        run_id = str(run_id) if run_id is not None else ""
        agent_id = str(agent_id) if agent_id is not None else ""
        user_input = str(user_input) if user_input is not None else ""
        llm_output = str(llm_output) if llm_output is not None else ""

        # 安全处理 action
        if action is None:
            action = "search"
        elif not isinstance(action, str):
            action = str(action)
        action = action.lower()

        memory_type = memory_type or None

        if not user_id:
            final_response["status"] = "error"
            final_response["message"] = "Missing user_id"
            return final_response

        # 2. 构建 Scope (基础参数)
        scope_params = {"user_id": user_id}
        if run_id: scope_params["run_id"] = run_id
        if agent_id: scope_params["agent_id"] = agent_id

        # 辅助函数：清洗 Payload
        def clean_payload(data):
            try:
                if isinstance(data, dict):
                    return {k: v for k, v in data.items() if v is not None and v != ""}
                return data
            except Exception:
                return data

        # --- ADD ---
        if action == "add":
            messages = []
            if user_input:
                messages.append({"role": "user", "content": user_input})
            if llm_output:
                messages.append({"role": "assistant", "content": llm_output})

            if not messages:
                final_response["status"] = "error"
                final_response["message"] = "Missing content for add action (user_input or llm_output)"
                return final_response

            payload = {
                "messages": messages,
                "memory_type": memory_type,
                **scope_params
            }
            print(payload)

            # 使用 clean_payload 清洗，但保留 payload 结构
            cleaned_payload = clean_payload(payload)
            print(cleaned_payload)
            resp = requests.post(MEM0_API_URL, json=cleaned_payload, timeout=10)

            if resp.status_code in [200, 201]:
                final_response["message"] = "Memory added successfully"
                try:
                    raw_data = resp.json()
                    # 确保 raw 是字典，防止下游处理报错
                    if isinstance(raw_data, list):
                        final_response["raw"] = {"list_data": raw_data}
                    elif isinstance(raw_data, dict):
                        final_response["raw"] = raw_data
                    else:
                        final_response["raw"] = {"data": raw_data}
                except Exception:
                    final_response["raw"] = {}
            else:
                final_response["status"] = "error"
                final_response["message"] = f"API Error: {resp.text}"

        # --- SEARCH (Actually FETCH) ---
        elif action == "search":
            # 针对 Chatflow 模式优化：不使用语义搜索，直接按时间顺序获取最新 10 条记录
            params = {"limit": 10, **scope_params}
            cleaned_params = clean_payload(params)

            resp = requests.get(MEM0_API_URL, params=cleaned_params, timeout=10)

            if resp.status_code == 200:
                data_json = resp.json()
                # 兼容处理：Mem0 有时直接返回列表，有时返回带 results 的字典
                if isinstance(data_json, list):
                    memories = data_json
                elif isinstance(data_json, dict):
                    memories = data_json.get("results", [])
                else:
                    memories = []

                memory_texts = [m["memory"] for m in memories if isinstance(m, dict) and "memory" in m]

                final_response["data"] = "\n".join([f"- {t}" for t in memory_texts]) if memory_texts else "暂无记录"

                # 确保 raw 是字典
                if isinstance(data_json, list):
                    final_response["raw"] = {"list_data": data_json}
                else:
                    final_response["raw"] = data_json
            else:
                final_response["status"] = "error"
                final_response["message"] = f"API Error: {resp.text}"

        # --- DELETE ---
        elif action == "delete":
            cleaned_params = clean_payload(scope_params)
            resp = requests.delete(MEM0_API_URL, params=cleaned_params, timeout=10)
            if resp.status_code in [200, 204]:
                final_response["message"] = "Memories deleted successfully"
            else:
                final_response["status"] = "error"
                final_response["message"] = f"API Error: {resp.text}"

        else:
            final_response["status"] = "error"
            final_response["message"] = f"Unknown action: {action}"

    except Exception as e:
        final_response["status"] = "error"
        final_response["message"] = f"Exception: {str(e)}"

    return final_response


main(user_id="12345", user_input="详解鸡兔同笼")
