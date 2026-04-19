# -*- coding: utf-8 -*-
# @File：ceshi5.py
# @Time：2025/9/28 18:18
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com
import requests
import json
from typing import List, Dict, Any, Optional

# ==============================================================================
# 1. 配置您的Dify环境信息
#    请将下面的 "..." 替换为您的真实信息
# ==============================================================================
# Dify服务器的IP地址和端口, 例如: "192.168.1.100:80" 或 "your-dify-domain.com"
DIFY_BASE_IP = "119.45.167.133:5125"

# 您要查询的知识库ID
DIFY_DATABASE_ID = "c3517663-17cc-43ce-8c14-873ac6d7c9f4"

# 您的Dify API密钥 (在 设置 -> API密钥 中创建)
DIFY_TOKEN = "dataset-pJ11Qq6BAfhYR4AJfLGtulbv"


# ==============================================================================
# 2. 这是您在Dify中使用的代码，我们将其原样复制于此进行测试
# ==============================================================================

def retrieve_from_database(base_ip: str, database_id: str, token: str, query_text: str,
                           metadata_filter: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    从 Dify 知识库检索数据。
    增加了 metadata_filter 参数以支持元数据过滤。
    """
    url = f"http://{base_ip}/v1/datasets/{database_id}/retrieve"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "query": query_text,
        "retrieval_model": {
            "search_method": "hybrid_search",
            "reranking_enable": True,
            "reranking_model": {
                "reranking_provider_name": "siliconflow",
                "reranking_model_name": "BAAI/bge-reranker-v2-m3"
            },
            "top_k": 5,
            "score_threshold_enabled": True,
            "score_threshold": 0
        }
    }

    # 动态添加元数据过滤条件
    if metadata_filter and isinstance(metadata_filter, dict) and metadata_filter:
        print(f"\n[Debug] 应用元数据过滤器: {metadata_filter}")
        conditions = []
        for key, value in metadata_filter.items():
            conditions.append({
                "name": key,
                "comparison_operator": "is",
                "value": value
            })
        if conditions:
            payload["retrieval_model"]["metadata_filtering_conditions"] = {
                "logical_operator": "and",
                "conditions": conditions
            }
    else:
        print("\n[Debug] 未应用元数据过滤器。")

    try:
        # **关键**：打印出最终要发送的完整请求体
        print(f"[Debug] 正在为关键字 '{query_text}' 发送请求...")
        print("[Debug] Request Payload:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        print(f"[Info] 关键字 '{query_text}' 请求成功！")
        return response.json()
    except requests.exceptions.HTTPError as http_err:
        print(f"[Error] HTTP 错误 (关键字: {query_text}): {http_err}")
        print(f"[Error] 响应内容: {response.text}")
    except requests.exceptions.RequestException as req_err:
        print(f"[Error] 请求发生错误 (关键字: {query_text}): {req_err}")
    return None


def process_and_extract_content(data: dict) -> List[Dict[str, Any]]:
    if not data or "records" not in data:
        return []
    extracted_results = []
    for item in data.get("records", []):
        segment = item.get("segment", {})
        document = segment.get("document", {})
        content = segment.get("content")
        if content:
            extracted_results.append({
                "content": content,
                "score": item.get("score"),
                "source": document.get("name"),
                "chunk_id": segment.get("id")
            })
    return extracted_results


def main(query_obj: dict, base_ip: str, database_id: str, token: str,
         metadata_filter: Optional[Dict[str, Any]] = None) -> dict:
    keywords_to_search = query_obj.get("web_queries", [])
    if not keywords_to_search:
        return {"result": [], "query": ""}
    merged_keyword = " ".join(keywords_to_search)
    all_retrieved_content = []
    for keyword in keywords_to_search:
        retrieval_data = retrieve_from_database(
            base_ip=base_ip,
            database_id=database_id,
            token=token,
            query_text=keyword,
            metadata_filter=metadata_filter
        )
        if retrieval_data:
            processed_results = process_and_extract_content(retrieval_data)
            if processed_results:
                all_retrieved_content.append({"data": processed_results})
    if not all_retrieved_content:
        return {"result": [], "query": "未能从任何关键字中检索到内容。"}
    return {"result": all_retrieved_content, "query": merged_keyword}


# ==============================================================================
# 3. 本地测试执行入口
#    使用 if __name__ == "__main__": 确保这部分只在直接运行时执行
# ==============================================================================
if __name__ == "__main__":
    # --- 模拟Dify传入的参数 ---
    # 模拟查询对象
    mock_query_obj = {
        "web_queries": ["理想L9的续航里程怎么样"]
    }

    # 模拟元数据过滤器 (这是测试的关键！)
    # 确保 "vehicle_model" 是您在Dify知识库中设置的元数据字段名
    # 确保 "理想L9" 是对应的元数据值
    mock_metadata_filter = {
        "vehicle_model": "理想L9"
    }

    print("=" * 80)
    print("🚀 Test Case 1: With Metadata Filter")
    print("=" * 80)

    # 调用 main 函数，模拟Dify工作流的执行，并传入过滤器
    results_with_filter = main(
        query_obj=mock_query_obj,
        base_ip=DIFY_BASE_IP,
        database_id=DIFY_DATABASE_ID,
        token=DIFY_TOKEN,
        metadata_filter=mock_metadata_filter
    )

    print("\n--- Results (With Filter) ---")
    print(json.dumps(results_with_filter, ensure_ascii=False, indent=2))
    print("\n" * 2)

    print("=" * 80)
    print("🚀 Test Case 2: Without Metadata Filter")
    print("=" * 80)

    # 再次调用 main 函数，这次不传入过滤器，用于对比
    results_without_filter = main(
        query_obj=mock_query_obj,
        base_ip=DIFY_BASE_IP,
        database_id=DIFY_DATABASE_ID,
        token=DIFY_TOKEN,
        metadata_filter=None  # 关键：不提供过滤器
    )

    print("\n--- Results (Without Filter) ---")
    print(json.dumps(results_without_filter, ensure_ascii=False, indent=2))
    print("\n" * 2)

    print("✅✅✅ Test Finished. Compare the two results above. ✅✅✅")
