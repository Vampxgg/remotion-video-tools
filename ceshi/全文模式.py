# import asyncio
# import httpx
# import os
# from typing import Dict, Any, List, Coroutine, Callable
#
# # --- 全局配置 (建议使用环境变量以提高安全性) ---
# # 在 Dify 环境变量中设置 DIFY_API_BASE_IP 和 DIFY_API_AUTH_TOKEN
# BASE_IP = os.getenv("DIFY_API_BASE_IP", "119.45.167.133:5125")
# AUTH_TOKEN = os.getenv("DIFY_API_AUTH_TOKEN", "dataset-pJ11Qq6BAfhYR4AJfLGtulbv")
#
# # ==============================================================================
# # ====================== DIFY 本地调试辅助模块 =========================
# # ==============================================================================
# import pprint
#
# # --- 本地调试开关 ---
# # 在你的 IDE 中进行测试时，将此值设为 True。
# # 当你准备将代码复制到 Dify 平台时，请将其改回 False，或直接删除此调试模块。
# IS_LOCAL_DEBUG = True
#
#
# def _dify_debug_return(data: Dict[str, Any], label: str = "Final Return") -> Dict[str, Any]:
#     """
#     一个用于在 Dify 代码节点中进行本地调试的包装函数。
#
#     当 IS_LOCAL_DEBUG 为 True 时，它会漂亮地打印出最终要返回的数据，
#     然后原封不动地返回该数据，以便 Dify 平台能正确接收。
#
#     Args:
#         data (Dict[str, Any]): 准备从 Dify 节点返回的数据。
#         label (str, optional): 一个标签，用于在控制台输出中标识来源。默认为 "Final Return"。
#
#     Returns:
#         Dict[str, Any]: 传入的原始数据。
#     """
#     if IS_LOCAL_DEBUG:
#         # 打印一个清晰的分隔符和标签，方便在终端中识别
#         print("\n" + "=" * 40 + f" DIFY DEBUG OUTPUT [{label}] " + "=" * 40)
#
#         # 使用 pprint 模块进行美化输出，对复杂的嵌套字典特别友好
#         pprint.pprint(data, indent=2, width=120)
#
#         # 打印结束分隔符
#         print("=" * 105 + "\n")
#
#     # 无论是否打印，都必须原封不动地返回原始数据
#     return data
#
#
# class DifyDatasetProcessor:
#     """
#     一个健壮的、面向对象的处理器，用于与 Dify 数据集 API 进行异步交互。
#     它封装了三种核心功能：
#     1. 获取完整文档内容 (full_document_retrieval)
#     2. 在特定文档内进行分段检索 (segment_retrieval)
#     3. 在整个知识库内进行分段检索 (full_database_retrieval)
#     """
#
#     def __init__(self, base_ip: str, auth_token: str, timeout: int = 45):
#         self.base_url = f"http://{base_ip}/v1/datasets"
#         self.retrieve_base_url = f"http://{base_ip}/v1"
#         self.headers = {
#             'Authorization': f'Bearer {auth_token}',
#             'Content-Type': 'application/json'
#         }
#         self.client = httpx.AsyncClient(headers=self.headers, timeout=timeout)
#
#     async def __aenter__(self):
#         return self
#
#     async def __aexit__(self, exc_type, exc_val, exc_tb):
#         if self.client and not self.client.is_closed:
#             await self.client.aclose()
#
#     # --- 内部核心 API 调用方法 ---
#
#     async def _fetch_all_segments(self, database_id: str, document_id: str) -> List[Dict]:
#         """内部方法：通过分页获取一个文档的所有数据段。"""
#         all_segments = []
#         page = 1
#         api_url = f"{self.base_url}/{database_id}/documents/{document_id}/segments"
#         while True:
#             params = {'limit': 100, 'page': page}
#             response = await self.client.get(api_url, params=params)
#             response.raise_for_status()
#             response_data = response.json()
#             segments_on_page = response_data.get("data", [])
#             if not segments_on_page:
#                 break
#             all_segments.extend(segments_on_page)
#             if not response_data.get("has_more", False):
#                 break
#             page += 1
#         return all_segments
#
#     async def _get_document_details(self, database_id: str, document_id: str) -> Dict[str, Any]:
#         """【新增】内部方法：获取指定文档的完整详细信息。"""
#         url = f"{self.base_url}/{database_id}/documents/{document_id}"
#         response = await self.client.get(url)
#         response.raise_for_status()
#         return response.json()
#
#     async def _get_document_name(self, database_id: str, document_id: str) -> str:
#         """内部方法：获取指定文档的名称。"""
#         url = f"{self.base_url}/{database_id}/documents/{document_id}"
#         response = await self.client.get(url)
#         response.raise_for_status()
#         doc_data = response.json()
#         if 'name' not in doc_data:
#             raise ValueError(f"API 响应中未找到文档 '{document_id}' 的 'name' 字段")
#         return doc_data['name']
#
#     # --- 公共任务处理方法 ---
#
#     async def process_full_document_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
#         """
#         【已重写】公共方法：处理【获取完整文档内容】的单个任务。
#         现在会获取文档详情，并输出为指定的复杂 JSON 结构。
#         """
#         document_id = task.get("document_id", "未知")
#         try:
#             database_id = task["database_id"]
#
#             # --- 步骤 1: 并发执行两个网络请求 ---
#             # 创建获取文档详情和获取所有分段的协程任务
#             details_coro = self._get_document_details(database_id, document_id)
#             segments_coro = self._fetch_all_segments(database_id, document_id)
#
#             print(f"🔄 [文档: {document_id}] 开始并发获取文档详情和全部分段...")
#             # 使用 asyncio.gather 并发等待结果
#             doc_details, all_segments = await asyncio.gather(details_coro, segments_coro)
#             print(f"✅ [文档: {document_id}] 成功获取到详情和 {len(all_segments)} 个分段。")
#
#             # --- 步骤 2: 按照新结构组装结果 ---
#             # 从分段数据中提取并格式化 content_blocks
#             all_segments.sort(key=lambda x: x.get('position', float('inf')))
#             content_blocks = [
#                 {"position": seg.get("position"), "content": seg.get("content", "")}
#                 for seg in all_segments
#             ]
#
#             # 从文档详情中提取元数据
#             # Dify API 返回的 metadata 是一个对象(dict)，我们将其转换为您的目标格式
#             # 如果您希望 doc_metadata 是一个包含键值对的列表，可以使用此转换
#             metadata_obj = doc_details.get("metadata", {})
#             doc_metadata_list = doc_details.get("doc_metadata", [])
#
#             # 构建最终的输出字典
#             result = {
#                 "source_type": "document",
#                 "database_id": database_id,
#                 "document_id": document_id,
#                 "document_name": doc_details.get("name", "Unknown Name"),
#                 "doc_metadata": doc_metadata_list,
#                 "content_blocks": content_blocks
#             }
#             return result
#
#         except Exception as e:
#             error_message = f"{e.__class__.__name__}: {e}"
#             print(f"❌ [文档: {document_id}] 处理失败: {error_message}")
#             # 在出错时，返回一个包含错误信息的标准结构
#             return {
#                 "source_type": "document",
#                 "database_id": task.get("database_id"),
#                 "document_id": document_id,
#                 "error": error_message
#             }
#
#     async def process_segment_retrieval_task(self, task: Dict[str, Any], query_text: str) -> Dict[str, Any]:
#         """公共方法：处理【在文档内分段检索】的单个任务。"""
#         result = task.copy()
#         document_id = task.get("document_id", "未知")
#         try:
#             database_id = task["database_id"]
#             top_k = task.get("top_k", 5)
#
#             document_name = await self._get_document_name(database_id, document_id)
#             print(f"📄 [文档: {document_id}] 成功获取文档名称: '{document_name}'，将用于过滤。")
#
#             metadata_filter = {
#                 "logical_operator": "and",
#                 "conditions": [{"name": "document_name", "comparison_operator": "is", "value": document_name}]
#             }
#             result_segments = await self._perform_retrieval(database_id, query_text, top_k, metadata_filter)
#             result['retrieved_segments'] = result_segments
#             print(f"👍 [文档: {document_id}] 检索成功，找到 {len(result_segments)} 个相关片段。")
#
#         except Exception as e:
#             error_message = f"{e.__class__.__name__}: {e}"
#             print(f"❌ [文档: {document_id}] 处理失败: {error_message}")
#             result['error'] = error_message
#         return result
#
#     async def process_full_database_retrieval_task(self, task: Dict[str, Any], query_text: str) -> Dict[str, Any]:
#         """【新增】公共方法：处理【在整个知识库内检索】的单个任务。"""
#         result = task.copy()
#         database_id = task.get("database_id", "未知")
#         try:
#             top_k = task.get("top_k", 5)
#
#             print(f"🌐 [知识库: {database_id}] 开始在整个知识库中检索...")
#             result_segments = await self._perform_retrieval(database_id, query_text, top_k, metadata_filter=None)
#             result['retrieved_segments'] = result_segments
#             print(f"👍 [知识库: {database_id}] 检索成功，找到 {len(result_segments)} 个相关片段。")
#
#         except Exception as e:
#             error_message = f"{e.__class__.__name__}: {e}"
#             print(f"❌ [知识库: {database_id}] 处理失败: {error_message}")
#             result['error'] = error_message
#         return result
#
#     async def _perform_retrieval(self, database_id: str, query_text: str, top_k: int, metadata_filter: Dict = None) -> \
#             List[Dict]:
#         """【重构】通用的检索执行器。"""
#         retrieve_url = f"{self.retrieve_base_url}/datasets/{database_id}/retrieve"
#         payload = {
#             "query": query_text,
#             "retrieval_model": {
#                 "search_method": "hybrid_search", "reranking_enable": False,
#                 "top_k": top_k, "score_threshold_enabled": False,
#             }
#         }
#         if metadata_filter:
#             payload["retrieval_model"]["metadata_filtering_conditions"] = metadata_filter
#
#         response = await self.client.post(retrieve_url, json=payload)
#         response.raise_for_status()
#         retrieval_data = response.json()
#
#         records = retrieval_data.get("records", [])
#         return [
#             {
#                 "content": s.get("segment", {}).get("content"), "score": s.get("score"),
#                 "source": s.get("segment", {}).get("document", {}).get("name"),
#                 "chunk_id": s.get("segment", {}).get("id"),
#                 "document_id": s.get("segment", {}).get("document", {}).get("id")
#             } for s in records if s.get("segment", {}).get("content")
#         ]
#
#     async def run_tasks(self, tasks: List[Dict], worker_coro: Callable[..., Coroutine], **kwargs) -> List[Dict]:
#         """通用异步任务调度器。"""
#         async_tasks = [worker_coro(task, **kwargs) for task in tasks]
#         results = await asyncio.gather(*async_tasks, return_exceptions=True)
#
#         final_results = []
#         for res in results:
#             if isinstance(res, Exception):
#                 print(f"🔥 [严重错误] 一个协程任务本身执行失败: {res}")
#                 final_results.append({"error": f"内部协程错误: {res}"})
#             else:
#                 final_results.append(res)
#         return final_results
#
#
# def main(tasks: List[Dict], query_groups: List[Dict] = None) -> Dict[str, Any]:
#     """
#     Dify 代码节点的同步主入口。
#     【已重构】支持在 'tasks' 列表中混合不同 retrieval_mode 的任务，并对检索类任务应用批量查询。
#     """
#     if not isinstance(tasks, list) or not tasks:
#         return {"results": []}
#
#     # --- 1. 【核心改动】构建异步任务列表，为每个 task 独立判断模式 ---
#     async_tasks_to_run = []
#
#     # 在循环外创建一次 processor 实例，以复用 http client
#     processor_instance = DifyDatasetProcessor(base_ip=BASE_IP, auth_token=AUTH_TOKEN)
#
#     print("🚀 [任务开始] 开始解析并构建所有异步任务...")
#     for i, task in enumerate(tasks):
#         retrieval_mode = task.get("retrieval_mode")
#         has_document_id = "document_id" in task
#         worker_function = None
#         is_retrieval_mode = False
#         mode_name = "未知"
#
#         # --- 模式识别 (现在在循环内部，针对每个 task) ---
#         if retrieval_mode == "full_database_retrieval":
#             mode_name = "知识库检索"
#             worker_function = DifyDatasetProcessor.process_full_database_retrieval_task
#             is_retrieval_mode = True
#         elif retrieval_mode == "segment_retrieval" and has_document_id:
#             mode_name = "文档分段检索"
#             worker_function = DifyDatasetProcessor.process_segment_retrieval_task
#             is_retrieval_mode = True
#         elif retrieval_mode == "full_document_retrieval" and has_document_id:
#             mode_name = "获取完整文档"
#             worker_function = DifyDatasetProcessor.process_full_document_task
#
#         # --- 根据模式构建协程 ---
#         if worker_function:
#             # 绑定 worker_function 到 processor 实例
#             bound_worker = worker_function.__get__(processor_instance, DifyDatasetProcessor)
#
#             if is_retrieval_mode:
#                 # 是检索模式，需要应用所有 query_groups
#                 if not query_groups or not isinstance(query_groups, list):
#                     err_msg = f"任务 {i + 1} (模式: {mode_name}) 需要 'query_groups' 输入，但未提供或格式错误。"
#                     print(f"❌ {err_msg}")
#                     # 可以选择跳过或添加错误结果，这里选择跳过
#                     continue
#
#                 print(
#                     f"  - 任务 {i + 1} ({mode_name}): 将为其创建 {sum(len(qg.get('local_queries', [])) for qg in query_groups)} 个查询协程。")
#                 for group in query_groups:
#                     for query in group.get("local_queries", []):
#                         coro = bound_worker(task=task, query_text=query)
#                         async_tasks_to_run.append(coro)
#
#             else:
#                 # 非检索模式，直接创建任务协程
#                 print(f"  - 任务 {i + 1} ({mode_name}): 创建 1 个获取协程。")
#                 coro = bound_worker(task=task)
#                 async_tasks_to_run.append(coro)
#
#         else:
#             # 模式无法识别
#             err_msg = f"无法识别任务 {i + 1} 的模式。任务内容: {task}"
#             print(f"⚠️  [警告] {err_msg}")
#
#             # 可以创建一个返回错误的协程，或直接忽略
#             async def error_coro(err=err_msg):
#                 return {"error": err}
#
#             async_tasks_to_run.append(error_coro())
#
#     print(f"✅ [构建完成] 共创建 {len(async_tasks_to_run)} 个独立的异步任务。")
#
#     # --- 2. 异步执行所有构建好的任务 ---
#     async def async_main_runner():
#         async with processor_instance:  # 使用 async with 确保 client 在结束时关闭
#             # 使用 asyncio.gather 执行所有协程，并处理可能发生的异常
#             results = await asyncio.gather(*async_tasks_to_run, return_exceptions=True)
#
#             final_results = []
#             for res in results:
#                 if isinstance(res, Exception):
#                     # 捕获协程执行期间的未处理异常
#                     error_message = f"内部协程错误: {type(res).__name__}: {res}"
#                     print(f"🔥 [严重错误] {error_message}")
#                     final_results.append({"error": error_message})
#                 else:
#                     final_results.append(res)
#             return final_results
#
#     # --- 3. 运行并返回结果 ---
#     try:
#         if not async_tasks_to_run:
#             processed_results = []
#         else:
#             processed_results = asyncio.run(async_main_runner())
#     except Exception as e:
#         print(f"🔥 [主流程错误] {e}")
#         return {"results": [{"error": f"主流程执行失败: {e}"}]}
#
#     successful_count = sum(1 for r in processed_results if 'error' not in r)
#     failed_count = len(processed_results) - successful_count
#     print(f"🎉 [完成] 所有任务处理完毕。成功: {successful_count}，失败: {failed_count}。")
#
#     return _dify_debug_return({"results": processed_results})


# 版本二

import asyncio
import httpx
import os
from typing import Dict, Any, List
from collections import defaultdict
import pprint

# --- 全局配置 ---
BASE_IP = os.getenv("DIFY_API_BASE_IP", "119.45.167.133:5125")
AUTH_TOKEN = os.getenv("DIFY_API_AUTH_TOKEN", "dataset-pJ11Qq6BAfhYR4AJfLGtulbv")
IS_LOCAL_DEBUG = True


# --- 调试辅助模块 ---
def _dify_debug_return(data: Dict[str, Any], label: str = "Final Return") -> Dict[str, Any]:
    if IS_LOCAL_DEBUG:
        print("\n" + "=" * 40 + f" DIFY DEBUG OUTPUT [{label}] " + "=" * 40)
        pprint.pprint(data, indent=2, width=120)
        print("=" * 105 + "\n")
    return data


# --- 核心算法：Reciprocal Rank Fusion (RRF) ---
def reciprocal_rank_fusion(ranked_lists: List[List[Dict]], k: int = 60) -> List[Dict]:
    """
    对多个排序结果列表进行RRF融合。

    Args:
        ranked_lists: 每个元素是一个检索结果列表，列表中的字典需包含唯一标识符，如 'chunk_id'。
        k: RRF算法中的一个常数，用于调整长尾结果的权重。

    Returns:
        一个经过RRF分数计算后重新排序的融合结果列表。
    """
    scores = defaultdict(float)
    # 使用 chunk_id 作为唯一标识符，并存储最先遇到的完整对象
    fused_results_objects = {}

    for ranked_list in ranked_lists:
        if not ranked_list:
            continue
        for rank, result in enumerate(ranked_list):
            # 确保每个结果都有一个可用的唯一ID
            if 'chunk_id' in result and result['chunk_id'] is not None:
                doc_id = result['chunk_id']
                if doc_id not in fused_results_objects:
                    fused_results_objects[doc_id] = result
                scores[doc_id] += 1 / (k + rank)

    # 根据分数对唯一ID进行排序
    sorted_ids = sorted(scores.keys(), key=lambda id: scores[id], reverse=True)

    # 构造最终的排序列表
    final_ranked_list = [fused_results_objects[id] for id in sorted_ids]
    for doc in final_ranked_list:
        doc['rrf_score'] = scores[doc['chunk_id']]  # 添加RRF分数以供调试

    return final_ranked_list


# --- Dify API 处理器 ---
class DifyDatasetProcessor:
    """【Query-Set驱动架构版】"""

    def __init__(self, base_ip: str, auth_token: str, timeout: int = 45):
        self.base_url = f"http://{base_ip}/v1/datasets"
        self.retrieve_base_url = f"http://{base_ip}/v1"
        self.headers = {'Authorization': f'Bearer {auth_token}', 'Content-Type': 'application/json'}
        self.client = httpx.AsyncClient(headers=self.headers, timeout=timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client and not self.client.is_closed: await self.client.aclose()

    async def _get_document_name(self, database_id: str, document_id: str) -> str:
        url = f"{self.base_url}/{database_id}/documents/{document_id}"
        response = await self.client.get(url)
        response.raise_for_status()
        return response.json()['name']

    async def perform_single_retrieval(self, query: str, top_k: int, scope: Dict) -> List[Dict]:
        """
        在指定的Scope内，为单个query执行一次精确的检索。
        """
        # 注意：Dify当前API可能不支持在一次请求中跨多个database_id检索。
        # 这里的实现假定一个scope对应一个database_id，如果需要跨库，需多次调用。
        # 此处简化为只处理第一个database。真实生产系统需要更复杂的scope解析。
        database_id = scope['allowed_databases'][0] if scope['allowed_databases'] else None
        if not database_id:
            return []

        # 构建元数据过滤器
        metadata_filter = None
        if scope.get('allowed_documents'):
            doc_names = scope['allowed_documents'].get(database_id, [])
            if doc_names:
                # Dify API目前可能只支持 "is" 操作符，而非 "in"。
                # 如果支持 "in" 或 "or"，可以构建更复杂的过滤器。
                # 此处简化为只过滤第一个文档，或不进行文档级过滤。
                # 一个更健壮的实现是在应用层合并来自不同文档的检索结果。
                # 为了演示，我们此处假设可以对多个文档名进行OR操作（Dify未来可能支持）
                conditions = [{"name": "document_name", "comparison_operator": "is", "value": name} for name in
                              doc_names]
                if len(conditions) > 1:
                    metadata_filter = {"logical_operator": "or", "conditions": conditions}
                elif len(conditions) == 1:
                    metadata_filter = {"logical_operator": "and", "conditions": conditions}

        url = f"{self.retrieve_base_url}/datasets/{database_id}/retrieve"
        payload = {"query": query,
                   "retrieval_model": {"search_method": "hybrid_search", "reranking_enable": False, "top_k": top_k,
                                       "score_threshold_enabled": False}}
        if metadata_filter:
            payload["retrieval_model"]["metadata_filtering_conditions"] = metadata_filter

        try:
            response = await self.client.post(url, json=payload)
            response.raise_for_status()
            records = response.json().get("records", [])
            return [{"content": s.get("segment", {}).get("content"), "score": s.get("score"),
                     "source": s.get("segment", {}).get("document", {}).get("name"),
                     "chunk_id": s.get("segment", {}).get("id"),
                     "document_id": s.get("segment", {}).get("document", {}).get("id")} for s in records if
                    s.get("segment", {}).get("content")]
        except Exception as e:
            print(f"❌ [检索失败] Query: '{query}' on DB '{database_id}' failed: {e}")
            return []


# ----------------- 主逻辑入口 -----------------
async def async_main(tasks: List[Dict], query_groups: List[Dict] = None) -> Dict[str, Any]:
    """
    RAG Pipeline的主异步逻辑，采用“Query Set驱动”架构。
    """
    if not query_groups:
        return {"results": []}

    async with DifyDatasetProcessor(base_ip=BASE_IP, auth_token=AUTH_TOKEN) as processor:
        # --- Stage 1: 构建检索范围 (Virtual Knowledge Base Scope) ---
        print("🚀 [Stage 1] 构建检索范围 (Virtual KB Scope)...")
        scope = {"allowed_databases": set(), "allowed_documents": defaultdict(list)}
        doc_ids_to_resolve = defaultdict(list)

        # 遍历tasks，只为定义范围，不执行IO
        for task in tasks:
            mode = task.get("retrieval_mode")
            db_id = task.get("database_id")
            if not db_id: continue

            scope["allowed_databases"].add(db_id)
            if mode == "segment_retrieval":
                doc_id = task.get("document_id")
                if doc_id:
                    # 记录需要解析名称的文档ID
                    doc_ids_to_resolve[db_id].append(doc_id)
        scope["allowed_databases"] = list(scope["allowed_databases"])

        # 并发解析所有需要的文档名称
        name_resolution_coros = [processor._get_document_name(db, doc) for db, docs in doc_ids_to_resolve.items() for
                                 doc in docs]
        if name_resolution_coros:
            doc_names = await asyncio.gather(*name_resolution_coros, return_exceptions=True)
            i = 0
            for db, docs in doc_ids_to_resolve.items():
                for _ in docs:
                    if not isinstance(doc_names[i], Exception):
                        scope["allowed_documents"][db].append(doc_names[i])
                    i += 1
        print(
            f"✅ Scope构建完成: {len(scope['allowed_databases'])}个知识库, {sum(len(v) for v in scope['allowed_documents'].values())}个限定文档。")

        # --- Stage 2 & 3: 对每个Query-Set执行Multi-Query召回与Fusion ---
        print("\n🚀 [Stage 2 & 3] 开始处理所有Query-Set的召回与融合...")
        final_results = []
        for group in query_groups:
            slide_id = group.get("slide_id", "unknown_slide")
            query_set = group.get("local_queries", [])
            if not query_set:
                continue

            print(f"\n  - 正在处理 Slide: {slide_id} (包含 {len(query_set)} 个查询)...")

            # Stage 2: 为Query Set中的每个query并发执行召回
            # 注意: 此处假设所有检索任务共享相同的top_k，取输入中的最大值
            max_top_k = max([t.get("top_k", 10) for t in tasks] + [10])
            recall_coros = [processor.perform_single_retrieval(query, max_top_k, scope) for query in query_set]

            ranked_lists_per_query = await asyncio.gather(*recall_coros)

            # Stage 3: 对召回结果进行RRF融合
            fused_results = reciprocal_rank_fusion(ranked_lists_per_query)

            # 将该slide的结果添加到最终输出
            final_results.append({
                "slide_id": slide_id,
                "local_queries": query_set,
                "retrieve_data": fused_results[:max_top_k]  # 可以取融合后的Top K个结果
            })
            print(f"  ✅ Slide {slide_id} 处理完成，融合后得到 {len(fused_results)} 个候选证据。")

        return {"results": final_results}


def main(tasks: List[Dict], query_groups: List[Dict] = None) -> Dict[str, Any]:
    """Dify代码节点的同步入口。"""
    # 彻底移除`full_document_retrieval`的逻辑，因为它不属于检索阶段
    tasks_for_retrieval = [t for t in tasks if t.get("retrieval_mode") != "full_document_retrieval"]

    if not tasks_for_retrieval and any(t.get("retrieval_mode") == "full_document_retrieval" for t in tasks):
        print("⚠️ [警告] 输入只包含 'full_document_retrieval' 任务，这在新架构下不执行任何检索操作。")
        return {"results": []}

    try:
        results_data = asyncio.run(async_main(tasks_for_retrieval, query_groups))
        print(f"\n🎉 [完成] 所有 {len(query_groups or [])} 个Query-Set处理完毕。")
        return _dify_debug_return(results_data)
    except Exception as e:
        print(f"🔥 [主流程错误] {e}")
        return _dify_debug_return({"results": [{"error": f"主流程执行失败: {e}"}]})


main([
    {
        "database_id": "c3517663-17cc-43ce-8c14-873ac6d7c9f4",
        "document_id": "5e817dce-b6e1-4749-af61-c6f5756d79a8",
        "retrieval_mode": "full_document_retrieval"
    },
    {
        "database_id": "c3517663-17cc-43ce-8c14-873ac6d7c9f4",
        "document_id": "890be5ef-f78d-4667-b980-77b57344b3ee",
        "retrieval_mode": "segment_retrieval",
        "top_k": 100
    },
    {
        "database_id": "c6348dba-158b-4ee6-bade-21b34c919030",
        "document_id": "cfbbc4b2-8483-44ac-b4e4-e106d76e429b",
        "retrieval_mode": "segment_retrieval",
        "top_k": 100
    },
    {
        "database_id": "d0d6bf0f-9897-4add-8570-736b9e629eff",
        "retrieval_mode": "full_database_retrieval"
    }
], [
    {
        "local_queries": [
            "新能源汽车 高压系统组成",
            "电动汽车 高压安全风险",
            "比亚迪汉EV 高压部件"
        ],
        "slide_id": "chapter_1_slide_1"
    },
    {
        "local_queries": [
            "新能源汽车 高压操作规程",
            "高压作业 个人防护装备",
            "比亚迪汉EV 维修安全"
        ],
        "slide_id": "chapter_1_slide_2"
    },
    {
        "local_queries": [
            "比亚迪汉EV 高压断电步骤",
            "新能源汽车 验电操作规范",
            "高压维修 专用验电器"
        ],
        "slide_id": "chapter_1_slide_3"
    },
    {
        "local_queries": [
            "比亚迪汉EV 动力电池包结构",
            "刀片电池 布局",
            "汉EV 电池模块组成"
        ],
        "slide_id": "chapter_2_slide_1"
    },
    {
        "local_queries": [
            "比亚迪汉EV BMS功能",
            "电池管理系统 工作原理",
            "刀片电池 BMS"
        ],
        "slide_id": "chapter_2_slide_2"
    },
    {
        "local_queries": [
            "比亚迪汉EV 高压互锁原理",
            "电池包 绝缘监测系统",
            "新能源汽车 安全机制"
        ],
        "slide_id": "chapter_2_slide_3"
    },
    {
        "local_queries": [
            "比亚迪汉EV 车辆举升规范",
            "汽车维修 安全举升点",
            "动力电池包 拆卸准备"
        ],
        "slide_id": "chapter_3_slide_1"
    },
    {
        "local_queries": [
            "比亚迪汉EV 高压断电步骤",
            "动力电池包 放电操作",
            "汉EV 维修安全流程"
        ],
        "slide_id": "chapter_3_slide_2"
    },
    {
        "local_queries": [
            "比亚迪汉EV 电池包外部连接",
            "动力电池 冷却管路识别",
            "汉EV 高压线束位置"
        ],
        "slide_id": "chapter_3_slide_3"
    },
    {
        "local_queries": [
            "比亚迪汉EV 电池包螺栓拆卸",
            "动力电池包 紧固力矩",
            "汉EV 电池拆装顺序"
        ],
        "slide_id": "chapter_4_slide_1"
    },
    {
        "local_queries": [
            "比亚迪汉EV 电池线束分离",
            "动力电池 冷却管路断开",
            "汉EV 高压连接器拆卸"
        ],
        "slide_id": "chapter_4_slide_2"
    },
    {
        "local_queries": [
            "比亚迪汉EV 电池拆装工具",
            "动力电池包 安全搬运",
            "汉EV 专用举升设备"
        ],
        "slide_id": "chapter_4_slide_3"
    },
    {
        "local_queries": [
            "比亚迪汉EV BDU结构",
            "电池分配单元 功能",
            "动力电池 高压部件"
        ],
        "slide_id": "chapter_5_slide_1"
    },
    {
        "local_queries": [
            "比亚迪汉EV 继电器检测",
            "动力电池 熔断器更换",
            "汉EV 高压部件维护"
        ],
        "slide_id": "chapter_5_slide_2"
    },
    {
        "local_queries": [
            "比亚迪汉EV 高压连接器检查",
            "动力电池 线束维护",
            "汉EV 绝缘检测"
        ],
        "slide_id": "chapter_5_slide_3"
    },
    {
        "local_queries": [
            "比亚迪汉EV 电池包安装对位",
            "动力电池包 紧固力矩",
            "汉EV 电池复位"
        ],
        "slide_id": "chapter_6_slide_1"
    },
    {
        "local_queries": [
            "比亚迪汉EV 电池线束连接",
            "动力电池 冷却管路复位",
            "汉EV 高压连接器安装"
        ],
        "slide_id": "chapter_6_slide_2"
    },
    {
        "local_queries": [
            "比亚迪汉EV 高压互锁检查",
            "动力电池 系统复位",
            "汉EV 维修后检查"
        ],
        "slide_id": "chapter_6_slide_3"
    },
    {
        "local_queries": [
            "比亚迪汉EV 高压绝缘检测",
            "动力电池 泄漏测试",
            "汉EV 维修后安全检查"
        ],
        "slide_id": "chapter_7_slide_1"
    },
    {
        "local_queries": [
            "比亚迪汉EV 诊断仪连接",
            "电池系统 数据流分析",
            "汉EV BMS诊断"
        ],
        "slide_id": "chapter_7_slide_2"
    },
    {
        "local_queries": [
            "比亚迪汉EV 电池故障诊断",
            "动力电池 常见故障排除",
            "汉EV 维修指南"
        ],
        "slide_id": "chapter_7_slide_3"
    }
])
