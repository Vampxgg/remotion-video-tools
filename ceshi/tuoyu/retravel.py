# coding: utf-8
import asyncio
import json
import httpx
import os
import pprint
import re
from datetime import datetime
from typing import Dict, Any, List, Optional, Set, Tuple
from collections import defaultdict


# --- 配置管理 ---
class Config:
    # 替换为您的实际配置
    BASE_IP = os.getenv("DIFY_API_BASE_IP", "119.45.167.133:5125")
    AUTH_TOKEN = os.getenv("DIFY_API_AUTH_TOKEN", "dataset-pJ11Qq6BAfhYR4AJfLGtulbv")
    SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "sk-zdxzbykdzqbmpjlnasfjpapuzdkupupghxsaopftaqnvyfrv")
    SILICONFLOW_RERANK_URL = "https://api.siliconflow.cn/v1/rerank"
    RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
    IS_DEBUG = True
    TIMEOUT = 90


# --- 调试辅助 ---
def debug_print(data: Any, label: str = "DEBUG"):
    if Config.IS_DEBUG:
        print(f"\n{'=' * 30} {label} {'=' * 30}")
        pprint.pprint(data, indent=2, width=120)
        print("=" * 70 + "\n")
    return data


# --- 模块一：Dify API 客户端 ---
class DifyApiClient:
    """负责与 Dify Dataset 服务通信，并清洗数据"""

    def __init__(self):
        self.base_url = f"http://{Config.BASE_IP}/v1/datasets"
        self.headers = {
            'Authorization': f'Bearer {Config.AUTH_TOKEN}',
            'Content-Type': 'application/json'
        }
        self.client = httpx.AsyncClient(headers=self.headers, timeout=Config.TIMEOUT)

    async def close(self):
        if not self.client.is_closed:
            await self.client.aclose()

    async def fetch_document_detail(self, database_id: str, document_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/{database_id}/documents/{document_id}"
        try:
            resp = await self.client.get(url)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"⚠️ [Meta Error] DB: {database_id}, Doc: {document_id} - {e}")
            return {}

    async def fetch_all_segments(self, database_id: str, document_id: str) -> List[Dict]:
        """通过分页循环，获取一个文档下的所有切片"""
        all_segments = []
        page = 1
        # 注意：这里的 URL 需要根据您的 Dify 版本确认。
        # 原代码逻辑是 /datasets/{db}/documents/{doc}/segments
        url = f"{self.base_url}/{database_id}/documents/{document_id}/segments"

        while True:
            params = {'limit': 100, 'page': page}  # Dify 最大 limit 通常是 100
            success = False
            for attempt in range(3):
                try:
                    resp = await self.client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()

                    segments = data.get("data", [])
                    if not segments:
                        success = True
                        break

                    all_segments.extend(segments)

                    if not data.get("has_more", False):
                        success = True
                        break

                    success = True
                    break
                except Exception as e:
                    if attempt < 2:
                        await asyncio.sleep(1)
                        continue
                    print(
                        f"⚠️ [Fetch Segments Error] DB:{database_id} Doc:{document_id} Page:{page} - {type(e).__name__}: {e}")

            if not success:
                break

            if not data.get("has_more", False):
                break
            page += 1

        return all_segments

    async def retrieve(self, query: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        # 提取 Dify 接口需要的 Database ID
        db_id = payload.pop("database_id_for_url")
        url = f"{self.base_url}/{db_id}/retrieve"

        # 构造 Dify 标准请求体
        req_body = {
            "query": query,
            "retrieval_model": {
                "search_method": "hybrid_search",
                "reranking_enable": False,
                "top_k": 100,  # 尽可能多召回，让后续 RRF 和外部 Rerank 决定排名
                "score_threshold_enabled": False,
            }
        }
        if "metadata_filtering_conditions" in payload:
            req_body["retrieval_model"]["metadata_filtering_conditions"] = payload["metadata_filtering_conditions"]

        try:
            resp = await self.client.post(url, json=req_body)
            resp.raise_for_status()
            data = resp.json()

            # 数据清洗：将 Dify 复杂的嵌套结构扁平化
            clean_results = []
            for rec in data.get("records", []):
                seg = rec.get("segment", {})
                d = seg.get("document", {})
                if seg.get("content"):
                    clean_results.append({
                        "id": seg.get("id"),
                        "chunk_id": seg.get("id"),  # 关键 ID，用于去重
                        "content": seg.get("content", ""),
                        "score": rec.get("score", 0.0),  # 原始分数
                        "database_id": db_id,
                        "document_id": seg.get("document_id"),
                        "document_name": d.get("name"),
                        "position": seg.get("position", 0),
                        "doc_metadata": d.get("doc_metadata") or d.get("metadata") or {}
                    })
            return clean_results
        except Exception as e:
            print(f"⚠️ [Retrieve Error] Query: '{query}' - {e}")
            return []


# --- 模块二：RAG 服务 (算法核心) ---
class RagService:
    @staticmethod
    def reciprocal_rank_fusion(list_of_lists: List[List[Dict]], k: int = 60) -> List[Dict]:
        """
        【修正版 RRF】
        输入：List[List[Dict]] -> 包含多个 Query 检索结果的列表
        逻辑：平行投票。每个列表的第 n 名享有同等的权重。
        """
        scores = defaultdict(float)
        obj_map = {}

        # 外层循环：遍历不同的 Query 来源 (Q1, Q2, Q3...)
        for ranked_list in list_of_lists:
            if not ranked_list: continue

            # 内层循环：遍历该 Query 下的排名
            for rank, item in enumerate(ranked_list):
                cid = item.get('chunk_id')
                if not cid: continue

                # 记录对象以便最后返回
                if cid not in obj_map:
                    obj_map[cid] = item

                # 累加 RRF 分数
                scores[cid] += 1.0 / (k + rank)

        # 按融合分数降序排列
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        return [obj_map[uid] for uid in sorted_ids]

    @staticmethod
    async def compute_rerank_scores(query: str, chunks: List[Dict]) -> List[Dict]:
        """
        【优化版 Rerank】
        1. 使用 httpx 异步调用，不阻塞。
        2. 全量打分 (top_n = len)，不进行截断，将截断权交给下游业务。
        """
        if not chunks: return []

        # 限制单次最大打分数量，防止 HTTP 包过大 (可按需调整)
        candidates = chunks[:100]
        doc_contents = [c["content"] for c in candidates]

        payload = {
            "model": Config.RERANK_MODEL,
            "query": query,
            "documents": doc_contents,
            "return_documents": False,
            "top_n": len(doc_contents)  # 关键：返回所有候选的分数
        }
        headers = {
            "Authorization": f"Bearer {Config.SILICONFLOW_API_KEY}",
            "Content-Type": "application/json"
        }

        try:
            # 使用临时 Client 或单例 Client 均可，此处使用上下文管理器确保连接关闭
            async with httpx.AsyncClient(timeout=30) as temp_client:
                resp = await temp_client.post(Config.SILICONFLOW_RERANK_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])

            # 将新分数回填入 Chunk 对象
            scored_chunks = []
            for item in results:
                original_idx = item["index"]
                new_score = item["relevance_score"]

                chunk = candidates[original_idx].copy()
                chunk["score"] = round(new_score, 4)  # 更新为模型重排分
                scored_chunks.append(chunk)

            return scored_chunks  # 返回已打分的列表，顺序通常已经是降序

        except Exception as e:
            print(f"⚠️ [Rerank Failed] {e}. Falling back to RRF results.")
            # 降级策略：如果重排失败，直接返回原始列表（保留 RRF 排序）
            return chunks


# --- 模块三：流程编排器 (业务逻辑) ---
class RetrievalOrchestrator:
    def __init__(self, api_client: DifyApiClient):
        self.api = api_client
        self.doc_meta_cache = {}  # 缓存 (db_id, doc_id) -> Detail info

    async def prefetch_metadata(self, tasks: List[Dict]):
        """Stage 0: 预加载文档元数据，减少后续循环中的 API 调用"""
        print("🚀 [Init] Prefetching document metadata...")
        needed = set()
        for t in tasks:
            if t.get("document_id") and t.get("database_id"):
                needed.add((t["database_id"], t["document_id"]))

        if not needed: return

        # 并发获取
        coros = [self.api.fetch_document_detail(db, doc) for db, doc in needed]
        results = await asyncio.gather(*coros, return_exceptions=True)

        for (key, detail) in zip(needed, results):
            if detail:
                self.doc_meta_cache[key] = detail

            # 1. 提取 Ugly List -> Clean Dict
            raw_meta_list = detail.get("doc_metadata", [])
            clean_meta = ContentFormatter.clean_metadata(raw_meta_list)
            # 2. 补充 Root Level 的关键信息到 metadata 中 (比如 doc_form)
            # 有时候 document info 在外面，不在 doc_metadata list 里
            if "doc_form" in detail:
                clean_meta["doc_form"] = detail["doc_form"]

            # 3. 构造极简 Cache 对象
            self.doc_meta_cache[key] = {
                "id": detail.get("id"),
                "name": detail.get("name"),
                "doc_metadata": clean_meta,  # 只有这是一个干净的字典
                "_source": "api_detail"  # 标记来源
            }
        print(self.doc_meta_cache)

    def build_execution_plan(self, tasks: List[Dict]) -> List[Dict]:
        """Stage 1: 构建高效检索计划，合并相同 DB 的请求"""
        # 数据结构: db_id -> {doc_names: set, include_full: bool}
        plan_map = defaultdict(lambda: {"doc_names": set(), "include_full": False})

        for t in tasks:
            db = t.get("database_id")
            if not db: continue

            mode = t.get("retrieval_mode")
            if mode == "segment_retrieval":
                d_id = t.get("document_id")
                # 从缓存中拿名字 (Dify 过滤依赖名字)
                meta = self.doc_meta_cache.get((db, d_id))
                if meta and meta.get("name"):
                    plan_map[db]["doc_names"].add(meta["name"])
            elif mode == "full_database_retrieval":
                plan_map[db]["include_full"] = True

        # 生成实际的 Job Payloads
        jobs = []
        for db_id, constraints in plan_map.items():
            # Job A: 带 Metadata Filter 的检索 (针对指定文档)
            if constraints["doc_names"]:
                names = list(constraints["doc_names"])
                filter_cond = {
                    "logical_operator": "or" if len(names) > 1 else "and",
                    "conditions": [{"name": "document_name", "comparison_operator": "is", "value": n} for n in names]
                }
                jobs.append({
                    "database_id_for_url": db_id,
                    "metadata_filtering_conditions": filter_cond
                })

            # Job B: 全库检索 (如果需要)
            if constraints["include_full"]:
                jobs.append({"database_id_for_url": db_id})

        return jobs

    def _inject_metadata_from_search_results(self, chunks: List[Dict]):
        """从搜索结果注入时，也保持结构一致"""
        for c in chunks:
            key = (c["database_id"], c["document_id"])
            existing = self.doc_meta_cache.get(key)

            # 如果已有全量详情，跳过
            if existing and existing.get("_source") == "api_detail":
                continue
            # 注入搜索快照
            if c.get("doc_metadata") or c.get("document_name"):
                # 注意 retrieve 应该已经把 c["doc_metadata"] 洗成字典了
                self.doc_meta_cache[key] = {
                    "id": c["document_id"],
                    "name": c["document_name"],
                    "doc_metadata": c["doc_metadata"],  # 也是干净的字典
                    "_source": "retrieve_snapshot"
                }

    def distribute_chunks_to_tasks(self, scored_chunks: List[Dict], tasks: List[Dict]) -> List[Dict]:
        """
        Stage 4: 库存分发 (Inventory Slicing)
        将打好分的大列表，按照 Task 的需求（文档ID、TopK）分发出去。
        """
        # 1. 建立库存索引：DB -> Doc -> Chunks
        inventory = defaultdict(lambda: defaultdict(list))
        for c in scored_chunks:
            inventory[c['database_id']][c['document_id']].append(c)

        final_results = []
        used_chunk_ids = set()  # 用于全库检索时防止重复

        # 2. 遍历 Task 进行“进货”
        for task in tasks:
            mode = task.get("retrieval_mode")
            db = task.get("database_id")
            top_k = task.get("top_k", 20)

            candidates = []

            if mode == "segment_retrieval":
                # 只取特定文档的库存
                doc_id = task.get("document_id")
                candidates = inventory.get(db, {}).get(doc_id, [])

            elif mode == "full_database_retrieval":
                # 取该 DB 下所有文档的库存
                docs_in_db = inventory.get(db, {})
                for doc_chunks in docs_in_db.values():
                    candidates.extend(doc_chunks)

            # 3. 排序并截断 (基于 Rerank 分数)
            # 注意：candidates 是引用，这里 sort 不会影响原始库存列表的完整性，
            # 但为了安全，Rerank 步骤通常已经排好序了，这里只是再次确保。
            candidates.sort(key=lambda x: x["score"], reverse=True)

            # 4. 选取 Top K (带去重逻辑)
            picked_count = 0
            for c in candidates:
                if picked_count >= top_k:
                    break

                # 如果是 segment 任务，直接拿；如果是 full 任务，避免拿重复的(如果前面 segment 任务拿过了)
                # 注：这里的逻辑取决于：是否允许同一个 chunk 出现在不同的 Task 结果中？
                # 如果 Dify 要求每个 chunk 只能出现一次，保持这个 if。
                # 如果允许不同 Task 包含相同 chunk，可移除 check。
                if c['chunk_id'] not in used_chunk_ids:
                    final_results.append(c)
                    used_chunk_ids.add(c['chunk_id'])
                    picked_count += 1

        return final_results

    # 在 RetrievalOrchestrator 类中更新/添加此方法

    # async def process_full_document_task(self, task: Dict) -> Dict:
    #     """
    #     处理全文档任务。
    #     关键点：返回的结构必须与 format_output 生成的结构完全一致（Schema Alignment）。
    #     """
    #     db_id = task.get("database_id")
    #     doc_id = task.get("document_id")
    #
    #     # 1. 尝试从缓存拿 Meta，拿不到就去 Fetch
    #     # 这里的 fetch_document_detail 内部应实现防重复请求，或者依赖外部 prefetch
    #     doc_meta = self.doc_meta_cache.get((db_id, doc_id))
    #
    #     # 2. 获取全部分段 (如果 Meta 没拿到，这里并发去拿 Meta 和 Segments)
    #     tasks_list = [self.api.fetch_all_segments(db_id, doc_id)]
    #     if not doc_meta:
    #         tasks_list.append(self.api.fetch_document_detail(db_id, doc_id))
    #
    #     results = await asyncio.gather(*tasks_list)
    #     all_segments = results[0]
    #
    #     # 如果刚才并发取了 Meta，更新一下
    #     if not doc_meta and len(results) > 1:
    #         doc_meta = results[1]
    #         if doc_meta:
    #             self.doc_meta_cache[(db_id, doc_id)] = doc_meta
    #
    #     # 安全取值
    #     doc_name = doc_meta.get("name", "Unknown") if doc_meta else "Unknown"
    #     doc_metadata_dict = doc_meta.get("doc_metadata") or doc_meta.get("metadata") or {}
    #
    #     # 3. 排序分段 (保证阅读顺序)
    #     all_segments.sort(key=lambda x: x.get('position', float('inf')))
    #
    #     # 4. 构造 Content Blocks
    #     content_blocks = [
    #         {
    #             "position": s.get("position"),
    #             "content": s.get("content", ""),
    #             "score": None  # 全文档默认满分，表示是硬性指定的
    #         }
    #         for s in all_segments
    #     ]
    #
    #     # 5. 【关键】返回标准化结构 (Standardized Schema)
    #     # 这就是必须放入 retrieve_data 里的那个对象
    #     return {
    #         "database_id": db_id,
    #         "document_infos": [
    #             {
    #                 "doc_metadata": doc_metadata_dict,
    #                 "document_id": doc_id,
    #                 "document_name": doc_name,
    #                 "source_type": "document",  # 区别于 'excerpt'
    #                 "content_blocks": content_blocks
    #             }
    #         ]
    #     }

    async def process_full_document_task(self, task: Dict) -> Dict:
        """
        处理全文档任务 (适配多模态)。
        """
        db_id = task.get("database_id")
        doc_id = task.get("document_id")
        print(db_id, doc_id)
        # 1. 获取元数据和全部分段
        doc_meta_obj = self.doc_meta_cache.get((db_id, doc_id))
        if not doc_meta_obj or doc_meta_obj.get("_source") != "api_detail":
            raw_detail = await self.api.fetch_document_detail(db_id, doc_id)
            if raw_detail:

                raw_list = raw_detail.get("doc_metadata", [])
                clean_meta = ContentFormatter.clean_metadata(raw_list)
                # 补充 root level 信息
                if "doc_form" in raw_detail: clean_meta["doc_form"] = raw_detail["doc_form"]
                doc_meta_obj = {
                    "id": raw_detail.get("id"),
                    "name": raw_detail.get("name"),
                    "doc_metadata": clean_meta,
                    "_source": "full_detail"
                }
                self.doc_meta_cache[(db_id, doc_id)] = doc_meta_obj

        all_segments = await self.api.fetch_all_segments(db_id, doc_id)

        # 安全检查
        if not doc_meta_obj or not all_segments:
            return {
                "database_id": db_id,
                "document_infos": [{
                    "document_id": doc_id,
                    "document_name": "Error: Not Found",
                    "source_type": "error",
                    "content_blocks": []
                }]
            }

        # 2. 转换为内部 chunk 结构
        temp_chunks = [
            {"content": s.get("content"), "position": s.get("position"), "score": None,
             "document_id": doc_id, "database_id": db_id, "document_name": doc_meta_obj.get("name")}
            for s in all_segments
        ]

        # 3. 调用统一格式化器，并明确告知上下文是 'full_doc'
        document_info = ContentFormatter.format_document(temp_chunks, doc_meta_obj, context='full_doc')
        # 4. 返回标准结构
        return {"database_id": db_id, "document_infos": [document_info] if document_info else []}

    def format_output(self, chunks: List[Dict]) -> List[Dict]:
        """Stage 5: 格式化为 Dify 要求的 JSON 结构"""
        grouped = defaultdict(lambda: defaultdict(list))
        for c in chunks:
            grouped[c['database_id']][c['document_id']].append(c)

        formatted_sources = []
        for db_id, docs_map in grouped.items():
            doc_infos = []
            for doc_id, chunk_list in docs_map.items():
                if not chunk_list: continue
                # # 按原文位置排序，方便阅读
                # chunk_list.sort(key=lambda x: x.get('score', 0), reverse=True)

                # 从缓存读取元数据
                meta = self.doc_meta_cache.get((db_id, doc_id), {})

                formatted_doc = ContentFormatter.format_document(chunk_list, meta, context='rag')
                if formatted_doc:
                    doc_infos.append(formatted_doc)

                # doc_meta_dict = meta.get("doc_metadata") or meta.get("metadata") or {}
                # doc_infos.append({
                #     "doc_metadata": doc_meta_dict,
                #     "document_id": doc_id,
                #     "document_name": chunk_list[0]['document_name'] or "Unknown",
                #     "source_type": "excerpt",
                #     "content_blocks": [
                #         {
                #             "content": c["content"],
                #             "position": c["position"],
                #             "score": c["score"]
                #         } for c in chunk_list
                #     ]
                # })

            if doc_infos:
                formatted_sources.append({
                    "database_id": db_id,
                    "document_infos": doc_infos
                })
        return formatted_sources

    async def process_slide(self, query_group: Dict, execution_plan: List[Dict], tasks: List[Dict]) -> Dict:
        """处理单页 PPT 逻辑：Retrieve -> RRF -> Rerank -> Slice -> Format"""
        # slide_id = slide_group.get("slide_id", "unknown")
        queries = query_group.get("local_queries", [])

        result_object = query_group.copy()
        if not queries:
            result_object["retrieve_data"] = []
            return result_object

        print(f"👉 Processing Slide:^_^ ({len(queries)} queries)")

        # 1. 并发 Execute Retrieval Jobs
        # 生成 (Query数量 x Plan Job数量) 个异步请求
        fetch_tasks = []
        for q in queries:
            for job in execution_plan:
                # 必须 copy job，因为 retrieve 内部会 pop 字段
                fetch_tasks.append(self.api.retrieve(q, job.copy()))
        # print(fetch_tasks)

        # 等待所有 API 返回
        # raw_results_list 的结构是: [ [ChunkA1, ChunkA2], [ChunkB1], [] ... ]
        raw_results_list = await asyncio.gather(*fetch_tasks)
        # print(raw_results_list)

        # 2. RRF 融合 (修复点：直接传入 List[List])
        # 过滤掉空结果，传入 RRF
        valid_results = [res for res in raw_results_list if res]
        fused_chunks = RagService.reciprocal_rank_fusion(valid_results)

        # 3. 全局 Rerank 打分 (修复点：异步调用，全量不截断)
        rerank_context = " ".join(queries)  # 简单拼接做 context
        scored_chunks = await RagService.compute_rerank_scores(rerank_context, fused_chunks)

        # 4. 库存分发 (Slicing)
        final_chunks = self.distribute_chunks_to_tasks(scored_chunks, tasks)
        # print(final_chunks)
        # 存储cache文档信息
        for res_list in raw_results_list:
            if res_list:
                self._inject_metadata_from_search_results(final_chunks)
        print(self.doc_meta_cache)

        # 5. 挂载数据到副本中
        result_object["retrieve_data"] = self.format_output(final_chunks)
        # 6. 格式化输出
        return result_object


# --- 模块四：多模态内容格式化器 ---
class ContentFormatter:
    """
    负责将不同源类型的 chunk 内容格式化为标准输出结构。
    这是一个可扩展的模块，未来可添加 Image, Audio 等格式化器。
    """

    @staticmethod
    def clean_metadata(raw_data: Any) -> Dict[str, Any]:
        """
        【核心清洗器】
        输入：可能是 Dify 详情接口返回的 List[Dict]，也可能是检索接口返回的 Dict。
        输出：统一的扁平 Dict {key: value}。
        """
        if not raw_data:
            return {}
        # 场景 A: Retrieve 接口返回的已经是漂亮的 Dict
        # 例如: {"source": "file_upload", "description": "xxx", "source_type": "document"}
        if isinstance(raw_data, dict):
            return raw_data
        # 场景 B: Detail 接口返回的 Ugly List
        # 例如: [{"name": "vehicle_model", "value": "理想L8"}, {"name": "doc_type", "value": "维修手册"}]
        result = {}
        if isinstance(raw_data, list):
            for item in raw_data:
                # 容错：必须是字典且包含 name 和 value
                if isinstance(item, dict) and 'name' in item:
                    # 优先取 value，如果没有 value 可能是空字段
                    val = item.get('value')
                    # 有些特殊字段 value 是 "NULL" 字符串，转为 None 或空
                    if val == "NULL":
                        val = None
                    result[item['name']] = val
            return result
        return {}

    @staticmethod
    def _transform_metadata(meta_list_or_dict: Any) -> Dict[str, Any]:
        """
        【关键修复】
        将 Dify 返回的元数据列表 `[{'name': k, 'value': v}, ...]`
        转换为标准的字典 `{'k': 'v', ...}`。
        同时兼容已经是字典的情况。
        """
        if isinstance(meta_list_or_dict, dict):
            return meta_list_or_dict  # 如果已经是字典，直接返回
        if not isinstance(meta_list_or_dict, list):
            return {}  # 如果是其他未知类型，返回空字典
        # 核心转换逻辑
        transformed_meta = {}
        for item in meta_list_or_dict:
            if isinstance(item, dict) and 'name' in item and 'value' in item:
                transformed_meta[item['name']] = item['value']
        return transformed_meta

    @staticmethod
    def _parse_key_value_string(content: str) -> Dict[str, Any]:
        """
        解析 "key1":"value1";"key2":"value2" 格式的字符串。
        """
        data = {}
        if not isinstance(content, str):
            return data

        pairs = content.strip().split(';')
        for pair in pairs:
            if ':' in pair:
                key, val = pair.split(':', 1)
                # 去除键和值的引号
                key = key.strip().strip('"')
                val = val.strip().strip('"')
                data[key] = val
        return data

    @classmethod
    def _format_video_document(cls, chunks: List[Dict], meta: Dict) -> Dict:
        """
        将视频类型的 Chunks 列表格式化为视频文档结构。
        """
        if not chunks:
            return {}

        chunks.sort(key=lambda x: x.get('position', 0))
        content_blocks = []
        video_info = {}

        # 1. 解析所有 video chunks，构建 frame 列表
        for chunk in chunks:
            raw_data = cls._parse_key_value_string(chunk.get("content", ""))

            # 提取全局视频信息 (只需一次)
            if not video_info:
                video_info = {
                    "duration": float(raw_data.get("视频时长", 0.0)),
                    "videoUrl": raw_data.get("视频链接", ""),
                    "videoName": raw_data.get("视频名称", ""),
                }

            # 2. 构建单个 frame 对象
            try:
                frame = {
                    "frameId": raw_data.get("视频片段ID"),
                    "frameName": raw_data.get("视频片段名称"),
                    "frameUrl": raw_data.get("视频片段分段URL"),
                    "frameImageUrl": raw_data.get("视频片段帧图片URL"),
                    "startTime": float(raw_data.get("开始时间", 0.0)),
                    "endTime": float(raw_data.get("结束时间", 0.0)),
                    "frameDuration": float(raw_data.get("视频片段时长", 0.0)),
                    "description": raw_data.get("视频片段描述", ""),
                    # 保留检索信息
                    "position": chunk.get("position"),
                    "score": chunk.get("score")
                }
                content_blocks.append(frame)
            except (ValueError, TypeError) as e:
                print(f"⚠️ [Video Frame Parse Error] Skipping frame. Chunk ID: {chunk.get('id')}, Error: {e}")
                continue

        # 3. 组装最终的视频文档对象
        doc_meta_dict = meta.get("doc_metadata") or meta.get("metadata") or {}
        raw_doc_name = chunks[0].get("document_name") or video_info.get("videoName") or "Unknown"
        final_doc_name = raw_doc_name
        # 获取目标后缀 (e.g., "mp4")
        target_ext = doc_meta_dict.get("extension")
        if target_ext and isinstance(raw_doc_name, str):
            # 移除旧后缀并添加新后缀
            if "." in raw_doc_name:
                # 分割文件名，保留最后一个点之前的所有内容作为 basename
                # e.g., "my.video.file.xlsx" -> "my.video.file"
                basename = raw_doc_name.rsplit(".", 1)[0]
                final_doc_name = f"{basename}.{target_ext}"
            else:
                # 如果原文件名没有后缀，直接追加
                final_doc_name = f"{raw_doc_name}.{target_ext}"

        return {
            "doc_metadata": doc_meta_dict,
            "document_id": chunks[0].get("document_id"),
            "document_name": final_doc_name,
            "source_type": "video",
            "duration": video_info.get("duration"),
            "videoUrl": video_info.get("videoUrl"),
            "content_blocks": content_blocks
        }

    @staticmethod
    def _format_text_document(chunks: List[Dict], meta: Dict, final_source_type: str) -> Dict:
        """
        格式化文本文档。
        final_source_type 必须是 'excerpt' 或 'document'。
        """
        if final_source_type == 'excerpt':
            chunks.sort(key=lambda x: x.get('score', 0), reverse=True)
        else:  # 'document'
            chunks.sort(key=lambda x: x.get('position', 0))
        doc_meta_dict = meta.get("doc_metadata") or meta.get("metadata") or {}

        return {
            "doc_metadata": doc_meta_dict,
            "document_id": chunks[0].get("document_id"),
            "document_name": chunks[0].get("document_name") or "Unknown",
            "source_type": final_source_type,
            "content_blocks": [
                {
                    "content": c["content"],
                    "position": c["position"],
                    "score": c["score"]
                } for c in chunks
            ]
        }

    @classmethod
    def format_document(cls, chunks: List[Dict], meta: Dict, context: str) -> Dict:
        """
        总分发器：根据元数据决定使用哪个格式化函数。
        【关键修复点】增加对 source_type 为 None 的处理。
        """
        if not chunks:
            return {}
        # print(chunks)
        # 优先从缓存的详细元数据中获取
        doc_meta = meta.get("doc_metadata", {}) or meta.get("metadata", {})
        # 如果缓存中没有，尝试从第一个 chunk 的元数据中降级获取 (虽然不推荐，但可作为备用)
        # if not doc_meta and chunks[0].get("metadata"):
        #    doc_meta = chunks[0].get("metadata")
        # 2. 【核心修复】调用转换函数，确保得到的是一个字典
        doc_meta_dict = cls._transform_metadata(doc_meta)
        # 3. 从转换后的字典中安全地获取 source_type
        #    这里的'doc_form'是根据日志猜测的，如果不行，可以尝试'doc_type'
        inherent_type = doc_meta_dict.get("source_type") or doc_meta_dict.get("doc_type")
        # --- DEBUGGING ---
        # # 增加更详细的日志，方便定位问题
        # if not source_type:
        #     print(f"⚠️ [Formatter Warning] No 'source_type' found for doc_id: {chunks[0].get('document_id')}. "
        #           f"Defaulting to 'text' format. Metadata received: {meta}")
        # ---------------
        # 排序：根据类型决定排序策略
        # 视频按原文位置（position）排序更合理，因为时间戳解析在格式化内部

        # if source_type == "video":
        #     chunks.sort(key=lambda x: x.get('position', 0))
        #     return cls._format_video_document(chunks, meta)
        # else:
        #     # 对于所有其他情况 (文本, None, 或未知的 source_type)，都按分数排序
        #     chunks.sort(key=lambda x: x.get('score', 0), reverse=True)
        #     return cls._format_text_document(chunks, meta)

        # 2. 根据“固有类型”和“调用上下文”决定最终的平行类型并分发
        if inherent_type == "video":
            return cls._format_video_document(chunks, meta)
        # elif inherent_type == "image":
        #     return cls._format_image_document(chunks, meta) # 未来扩展
        # elif inherent_type == "audio":
        #     return cls._format_audio_document(chunks, meta) # 未来扩展
        else:
            # 默认为文本处理逻辑
            if context == 'rag':
                final_type = 'excerpt'
            elif context == 'full_doc':
                final_type = 'document'
            else:
                # 默认降级为 excerpt
                final_type = 'excerpt'
            return cls._format_text_document(chunks, meta, final_source_type=final_type)


def parse_survey_content(content: str) -> dict:
    """
    解析问卷/访谈类内容（支持 Markdown、列表、纯文本格式）
    """
    lines = content.strip().split('\n')

    # 1. 提取元数据 (Metadata) - 通常在最后一行，以分号分隔
    metadata = {}
    if lines and ';' in lines[-1] and ':' in lines[-1]:
        meta_line = lines[-1].strip()
        # 移除开头的分号（如果有）
        if meta_line.startswith(';'):
            meta_line = meta_line[1:]

        parts = meta_line.split(';')
        for part in parts:
            if ':' in part:
                k, v = part.split(':', 1)
                metadata[k.strip()] = v.strip()

        # 提取完 metadata 后，从 lines 中移除最后一行，避免干扰后续解析
        lines = lines[:-1]

    # 2. 提取基本信息 (Basic Info)
    basic_info = {
        "city": metadata.get("city"),
        "job_role": metadata.get("job_role"),
        "institution_type": metadata.get("institution_type"),
        "institution_host": metadata.get("institution_host"),
        "institution_name": metadata.get("institution_name"),
        "is_inclusive": None,  # 需从正文提取
        "education": metadata.get("education"),
        "major": metadata.get("major")
    }

    # 尝试从第一行/标题行提取缺失的基本信息 (如果 metadata 不全)
    # 格式示例: 城市：北京-海淀区 | 岗位：保教主任 | 机构：幼儿园托班 | 性质：公办
    if lines:
        header_line = lines[0]
        if '|' in header_line:
            parts = header_line.split('|')
            for part in parts:
                if '：' in part or ':' in part:
                    sep = '：' if '：' in part else ':'
                    k, v = part.split(sep, 1)
                    k = k.strip()
                    v = v.strip()
                    if k == '城市' and not basic_info['city']: basic_info['city'] = v
                    if k == '岗位' and not basic_info['job_role']: basic_info['job_role'] = v
                    if k == '机构' and not basic_info['institution_type']: basic_info['institution_type'] = v
                    if k == '性质' and not basic_info['institution_host']: basic_info['institution_host'] = v

    # 3. 扫描正文提取补充信息 (Contents 解析用于辅助提取 Basic Info)
    # 虽然最终输出可能不需要详细的 contents 结构，但我们需要遍历正文来获取
    # 是否普惠、学历、专业 等可能遗漏在 Basic Info 中的字段

    for line in lines:
        line = line.strip()
        if not line: continue

        # 忽略第一行如果是 header
        if '|' in line and '城市' in line and line == lines[0]:
            continue

        # 识别问答对
        if ':' in line or '：' in line:
            # 移除开头的 -
            clean_line = line.lstrip('-').strip()

            # 分割 Key-Value
            sep = '：' if '：' in clean_line else ':'
            parts = clean_line.split(sep, 1)
            if len(parts) == 2:
                q = parts[0].strip()
                a = parts[1].strip()

                # 特殊处理：是否普惠
                if q == '是否普惠' and basic_info['is_inclusive'] is None:
                    if a == '是':
                        basic_info['is_inclusive'] = True
                    elif a == '否':
                        basic_info['is_inclusive'] = False

                # 特殊处理：学历/专业 (如果 metadata 没提取到，这里补救)
                if q == '学历' and not basic_info['education']: basic_info['education'] = a
                if q == '专业' and not basic_info['major']: basic_info['major'] = a

    return {
        "basic_info": basic_info,
        "raw_text": content
    }


def parse_institution_info(content: str) -> dict:
    """
    解析机构备案信息 (Key: Value 行格式)
    """
    lines = content.strip().split('\n')
    data = {}
    for line in lines:
        line = line.strip()
        if not line: continue

        sep = '：' if '：' in line else ':'
        if sep in line:
            k, v = line.split(sep, 1)
            data[k.strip()] = v.strip()

    return {
        "institution_info": {
            "name": data.get("机构名称"),
            "alias": data.get("别名"),
            "credit_code": data.get("统一社会信用代码"),
            "type": data.get("机构类型"),
            "address": data.get("详细地址"),
            "registration_date": data.get("备案及完成时间"),
            "region_code": data.get("区域编号")
        }
    }


def parse_school_major_info(content: str) -> dict:
    """
    解析高校专业信息 (Key: Value 行格式)
    """
    lines = content.strip().split('\n')
    data = {}
    for line in lines:
        line = line.strip()
        if not line: continue

        sep = '：' if '：' in line else ':'
        if sep in line:
            k, v = line.split(sep, 1)
            data[k.strip()] = v.strip()

    # 解析专业代码: "临床医学 (630101)" -> name="临床医学", code="630101"
    raw_major = data.get("开设专业", "")
    major_name = raw_major
    major_code = ""
    if "(" in raw_major and ")" in raw_major:
        match = re.match(r"(.*?)\s*\((.*?)\)", raw_major)
        if match:
            major_name = match.group(1).strip()
            major_code = match.group(2).strip()

    return {
        "school_info": {
            "school_name": data.get("机构名称"),
            "province": data.get("省份"),
            "school_code": data.get("学校标识码")
        },
        "major_info": {
            "major_name": major_name,
            "major_code": major_code,
            "duration_years": int(data.get("修业年限")) if data.get("修业年限") and data.get(
                "修业年限").isdigit() else None,
            "year": int(data.get("年份")) if data.get("年份") and data.get("年份").isdigit() else None,
            "note": data.get("备注", "")
        }
    }


def auto_parse(content: str) -> dict:
    """
    自动识别内容类型并分发解析
    """
    if "学校标识码" in content and "开设专业" in content:
        return parse_school_major_info(content)
    elif "统一社会信用代码" in content and "备案及完成时间" in content:
        return parse_institution_info(content)
    else:
        # 默认为问卷/访谈
        return parse_survey_content(content)


# --- 模块五：Tuoyu 专用处理器 ---

# --- 统一元数据管理模块 ---
class UnifiedMetaManager:
    """
    统一管理区域、学历、专业映射，并提供智能匹配逻辑。
    """

    # 预留专业映射 (目前为空，精确匹配)
    MAJOR_MAP = {}

    # 学历映射 (迁移自 TuoyuProcessor)
    EDUCATION_MAP = {
        "高职（专科）": "高等职业教育（专科）",
        "高职专科": "高等职业教育（专科）",
        "专科": "高等职业教育（专科）",
        "高职": "高等职业教育（专科）",
        "高等职业教育（专科）": "高等职业教育（专科）",
        "大专": "高等职业教育（专科）",
        "vocational_college": "高等职业教育（专科）",

        "本科": "普通本科",
        "普通本科": "普通本科",
        "本科及以上": "普通本科",
        "undergraduate": "普通本科",

        "中职": "中等职业教育",
        "中专": "中等职业教育",
        "高中/中职": "中等职业教育",
        "senior_high_school": "中等职业教育",

        "硕士": "硕士研究生",
        "研究生": "硕士研究生",
        "硕士研究生": "硕士研究生",
        "master_degree": "硕士研究生",
    }

    # 中国行政区划映射 (省份 -> 城市列表)
    # 包含所有地级市、自治州、盟、地区
    CHINA_REGIONS = {
        "北京": ["北京", "东城", "西城", "朝阳", "丰台", "石景山", "海淀", "门头沟", "房山", "通州", "顺义", "昌平",
                 "大兴", "怀柔", "平谷", "密云", "延庆"],
        "天津": ["天津", "和平", "河东", "河西", "南开", "河北", "红桥", "东丽", "西青", "津南", "北辰", "武清", "宝坻",
                 "滨海新区", "宁河", "静海", "蓟州"],
        "河北": ["石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定", "张家口", "承德", "沧州", "廊坊", "衡水"],
        "山西": ["太原", "大同", "阳泉", "长治", "晋城", "朔州", "晋中", "运城", "忻州", "临汾", "吕梁"],
        "内蒙古": ["呼和浩特", "包头", "乌海", "赤峰", "通辽", "鄂尔多斯", "呼伦贝尔", "巴彦淖尔", "乌兰察布", "兴安",
                   "锡林郭勒", "阿拉善"],
        "辽宁": ["沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口", "阜新", "辽阳", "盘锦", "铁岭", "朝阳",
                 "葫芦岛"],
        "吉林": ["长春", "吉林", "四平", "辽源", "通化", "白山", "松原", "白城", "延边"],
        "黑龙江": ["哈尔滨", "齐齐哈尔", "鸡西", "鹤岗", "双鸭山", "大庆", "伊春", "佳木斯", "七台河", "牡丹江", "黑河",
                   "绥化", "大兴安岭"],
        "上海": ["上海", "黄浦", "徐汇", "长宁", "静安", "普陀", "虹口", "杨浦", "闵行", "宝山", "嘉定", "浦东", "金山",
                 "松江", "青浦", "奉贤", "崇明"],
        "江苏": ["南京", "无锡", "徐州", "常州", "苏州", "南通", "连云港", "淮安", "盐城", "扬州", "镇江", "泰州",
                 "宿迁"],
        "浙江": ["杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "衢州", "舟山", "台州", "丽水"],
        "安徽": ["合肥", "芜湖", "蚌埠", "淮南", "马鞍山", "淮北", "铜陵", "安庆", "黄山", "滁州", "阜阳", "宿州",
                 "六安", "亳州", "池州", "宣城"],
        "福建": ["福州", "厦门", "莆田", "三明", "泉州", "漳州", "南平", "龙岩", "宁德"],
        "江西": ["南昌", "景德镇", "萍乡", "九江", "新余", "鹰潭", "赣州", "吉安", "宜春", "抚州", "上饶"],
        "山东": ["济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "济宁", "泰安", "威海", "日照", "临沂", "德州",
                 "聊城", "滨州", "菏泽"],
        "河南": ["郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作", "濮阳", "许昌", "漯河", "三门峡",
                 "南阳", "商丘", "信阳", "周口", "驻马店", "济源"],
        "湖北": ["武汉", "黄石", "十堰", "宜昌", "襄阳", "鄂州", "荆门", "孝感", "荆州", "黄冈", "咸宁", "随州", "恩施",
                 "仙桃", "潜江", "天门", "神农架"],
        "湖南": ["长沙", "株洲", "湘潭", "衡阳", "邵阳", "岳阳", "常德", "张家界", "益阳", "郴州", "永州", "怀化",
                 "娄底", "湘西"],
        "广东": ["广州", "深圳", "珠海", "汕头", "佛山", "韶关", "湛江", "肇庆", "江门", "茂名", "惠州", "梅州", "汕尾",
                 "河源", "阳江", "清远", "东莞", "中山", "潮州", "揭阳", "云浮"],
        "广西": ["南宁", "柳州", "桂林", "梧州", "北海", "防城港", "钦州", "贵港", "玉林", "百色", "贺州", "河池",
                 "来宾", "崇左"],
        "海南": ["海口", "三亚", "三沙", "儋州", "五指山", "琼海", "文昌", "万宁", "东方", "定安", "屯昌", "澄迈",
                 "临高", "白沙", "昌江", "乐东", "陵水", "保亭", "琼中"],
        "重庆": ["重庆", "万州", "涪陵", "渝中", "大渡口", "江北", "沙坪坝", "九龙坡", "南岸", "北碚", "綦江", "大足",
                 "渝北", "巴南", "黔江", "长寿", "江津", "合川", "永川", "南川", "璧山", "铜梁", "潼南", "荣昌", "开州",
                 "梁平", "武隆", "城口", "丰都", "垫江", "忠县", "云阳", "奉节", "巫山", "巫溪", "石柱", "秀山", "酉阳",
                 "彭水"],
        "四川": ["成都", "自贡", "攀枝花", "泸州", "德阳", "绵阳", "广元", "遂宁", "内江", "乐山", "南充", "眉山",
                 "宜宾", "广安", "达州", "雅安", "巴中", "资阳", "阿坝", "甘孜", "凉山"],
        "贵州": ["贵阳", "六盘水", "遵义", "安顺", "毕节", "铜仁", "黔西南", "黔东南", "黔南"],
        "云南": ["昆明", "曲靖", "玉溪", "保山", "昭通", "丽江", "普洱", "临沧", "楚雄", "红河", "文山", "西双版纳",
                 "大理", "德宏", "怒江", "迪庆"],
        "西藏": ["拉萨", "日喀则", "昌都", "林芝", "山南", "那曲", "阿里"],
        "陕西": ["西安", "铜川", "宝鸡", "咸阳", "渭南", "延安", "汉中", "榆林", "安康", "商洛"],
        "甘肃": ["兰州", "嘉峪关", "金昌", "白银", "天水", "武威", "张掖", "平凉", "酒泉", "庆阳", "定西", "陇南",
                 "临夏", "甘南"],
        "青海": ["西宁", "海东", "海北", "黄南", "海南", "果洛", "玉树", "海西"],
        "宁夏": ["银川", "石嘴山", "吴忠", "固原", "中卫"],
        "新疆": ["乌鲁木齐", "克拉玛依", "吐鲁番", "哈密", "昌吉", "博尔塔拉", "巴音郭楞", "阿克苏", "克孜勒苏", "喀什",
                 "和田", "伊犁", "塔城", "阿勒泰", "石河子", "阿拉尔", "图木舒克", "五家渠", "北屯", "铁门关", "双河",
                 "可克达拉", "昆玉", "胡杨河", "新星", "白杨"],
        "台湾": ["台湾", "台北", "高雄", "基隆", "台中", "台南", "新竹", "嘉义"],
        "香港": ["香港"],
        "澳门": ["澳门"]
    }

    def __init__(self):
        # 构建反向索引: City -> Province
        self.city_to_province = {}
        # 为了处理"吉林"既是省又是市的情况，我们优先记录省份
        # 但反向索引主要是为了通过城市找省份
        for prov, cities in self.CHINA_REGIONS.items():
            for city in cities:
                # 注意：如果城市名和省名相同（如吉林市），这里会被记录为 吉林->吉林省
                self.city_to_province[city] = prov

    def normalize_name(self, name: str) -> str:
        """移除常见的行政区划后缀"""
        if not name: return ""
        # 移除常见后缀 (注意顺序，先长后短)
        suffixes = ["自治区", "自治州", "特别行政区", "地区", "盟", "省", "市", "区", "县"]

        # 特殊处理：如果名字本身只有两个字且包含后缀，可能需要保留？
        # 一般来说 "四川省" -> "四川", "成都市" -> "成都"
        # "内蒙古自治区" -> "内蒙古"

        clean_name = name.strip()
        for suffix in suffixes:
            if clean_name.endswith(suffix):
                # 只有当移除后缀后长度仍 >= 2 (除特殊情况) 才移除
                # 比如 "沙市" -> "沙" (不妥)，但地级市一般至少2个字
                # 这里假设输入都是地级市或省份
                if len(clean_name) > len(suffix):
                    clean_name = clean_name[:-len(suffix)]
                    break  # 只移除最外层的一个后缀
        return clean_name

    def parse_location(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """
        从文本中解析出 (省份, 城市)
        Input: "四川", "四川省", "四川-成都", "成都市", "四川省成都市"
        Output: (Province, City) 归一化后的名称
        """
        if not text: return None, None

        # 1. 预处理
        text = text.replace(" ", "").replace("-", "").replace("_", "")

        # 2. 尝试匹配省份
        matched_province = None
        matched_city = None

        # 遍历所有省份
        for prov in self.CHINA_REGIONS.keys():
            if prov in text:
                matched_province = prov
                break

        # 3. 尝试匹配城市
        # 策略：如果已经匹配到省份，优先在该省份下找城市
        # 如果没匹配到省份，全量找城市 (并反推省份)

        if matched_province:
            cities = self.CHINA_REGIONS[matched_province]
            # 按长度降序匹配，防止子串误判
            for city in sorted(cities, key=len, reverse=True):
                if city in text:
                    matched_city = city
                    break

            # 特殊情况处理：吉林（省）和 吉林（市）
            # 如果 text="吉林"，matched_province="吉林"
            # 此时 matched_city 也会匹配到 "吉林" (因为吉林市在吉林省列表里)
            # 如果输入仅仅是 "吉林" 或 "吉林省"，应该视为省份查询
            # 如果输入是 "吉林市"，视为城市查询
            if matched_province == matched_city:
                if "市" in text:
                    pass  # 确认为城市
                else:
                    matched_city = None  # 视为省份

        else:
            # 全局搜索城市
            # 这比较慢，但为了准确性
            for city, prov in self.city_to_province.items():
                if city in text:
                    # 再次确认：如果是短名，是否真的匹配？
                    # 比如 text="南宁市", city="南宁" -> OK
                    # 比如 text="河南", city="南" (假设有这个城市) -> No
                    # 我们的 city list 都是地级市，一般不会太短
                    matched_city = city
                    matched_province = prov
                    break

        return matched_province, matched_city

    def check_region_match(self, rule_scope: str, doc_scope: str) -> bool:
        """
        核心区域匹配逻辑
        rule_scope: 用户输入的查询/规则 (e.g. "四川")
        doc_scope: 文档中的区域信息 (e.g. "成都")

        逻辑：
        1. 规则是指向城市 -> 必须精确匹配城市 (e.g. Rule="成都" -> Doc="成都" OK; Doc="四川" Fail; Doc="南充" Fail)
        2. 规则是指向省份 -> 匹配该省份下的所有城市 (e.g. Rule="四川" -> Doc="成都" OK; Doc="四川" OK)
        """
        if not rule_scope: return True  # 无规则限制
        if not doc_scope: return False  # 有规则但文档无区域信息

        r_prov, r_city = self.parse_location(rule_scope)
        d_prov, d_city = self.parse_location(doc_scope)

        # Debug
        # print(f"Match: Rule({r_prov}, {r_city}) vs Doc({d_prov}, {d_city})")

        # Case 1: 规则指定了具体城市
        if r_city:
            # 文档必须也是该城市
            if d_city == r_city:
                return True
            # 或者文档只写了省份，但省份不匹配？不行，必须精确到城市
            # 如果文档只写了 "四川"，规则是 "成都"，我们无法确定文档是否包含成都信息
            # 严格模式：False
            return False

        # Case 2: 规则只指定了省份
        if r_prov:
            # 文档只要属于该省份即可 (省份相同 OR 城市属于该省)
            if d_prov == r_prov:
                return True
            # 如果 d_prov 解析失败，但 d_city 解析成功，且 d_city 属于 r_prov
            if d_city and self.city_to_province.get(d_city) == r_prov:
                return True
            return False

        # Case 3: 规则解析不出省市 (可能是其他区域描述，如 "华东")
        # 降级为简单的字符串包含
        return rule_scope in doc_scope

    def normalize_education(self, text: str) -> str:
        if not text: return ""
        text = text.strip()
        # 1. 直接查表
        if text in self.EDUCATION_MAP:
            return self.EDUCATION_MAP[text]
        # 2. 包含匹配 (简单的反向查找，优先匹配长词)
        sorted_keys = sorted(self.EDUCATION_MAP.keys(), key=len, reverse=True)
        for k in sorted_keys:
            if k in text:
                return self.EDUCATION_MAP[k]
        return text


class TuoyuContentParser:

    @staticmethod
    def parse_key_value_lines(content: str) -> Dict[str, Any]:
        """
        使用内部定义的 auto_parse 进行统一解析
        返回结构化字典
        """
        try:
            parsed_data = auto_parse(content)
            # print(parsed_data)
            # 为了兼容旧逻辑 (check_rules 依赖扁平字典)，我们需要把 parsed_data 扁平化
            # 但同时保留结构化数据供后续使用

            flat_data = {}

            # 1. 处理问卷数据 (Survey)
            if 'basic_info' in parsed_data:
                # 提取 basic_info 到顶层
                for k, v in parsed_data['basic_info'].items():
                    if v is not None:
                        flat_data[k] = str(v)
                        # 兼容旧键名
                        if k == 'job_role': flat_data['岗位'] = str(v)
                        if k == 'city': flat_data['城市'] = str(v)
                        if k == 'education': flat_data['学历'] = str(v)
                        if k == 'major': flat_data['专业'] = str(v)

                # 提取 contents 里的问答对到顶层 (可选，用于更细粒度的规则检查?)
                # 目前 check_rules 主要检查 basic_info，所以这里可以简化

            # 2. 处理机构数据 (Institution)
            elif 'institution_info' in parsed_data:
                info = parsed_data['institution_info']
                for k, v in info.items():
                    if v:
                        flat_data[k] = str(v)
                        # 兼容旧键名
                        if k == 'name': flat_data['机构名称'] = str(v)
                        if k == 'type': flat_data['机构类型'] = str(v)
                        if k == 'address': flat_data['详细地址'] = str(v)
                        if k == 'registration_date': flat_data['备案及完成时间'] = str(v)

            # 3. 处理高校数据 (School Major)
            elif 'school_info' in parsed_data:
                s_info = parsed_data['school_info']
                m_info = parsed_data['major_info']

                for k, v in s_info.items():
                    if v: flat_data[k] = str(v)
                for k, v in m_info.items():
                    if v: flat_data[k] = str(v)

                # 兼容旧键名
                if s_info.get('school_code'): flat_data['学校标识码'] = str(s_info['school_code'])
                if m_info.get('major_name'): flat_data['开设专业'] = str(m_info['major_name'])

            # 将原始结构化数据挂载到特殊字段，方便后续提取
            flat_data['_structured_data'] = parsed_data

            return flat_data
        except Exception as e:
            print(f"⚠️ [Parse Error] {e}")
            return {}


class TuoyuProcessor:
    def __init__(self, api_client: DifyApiClient):
        self.api = api_client
        self.meta_manager = UnifiedMetaManager()

    def parse_time_filter(self, time_filter: str) -> Tuple[Optional[datetime], Optional[datetime]]:

        if not time_filter:
            return None, None

        now = datetime.now()

        if '近三年' in time_filter:
            # 近三年通常指当前年份往前推3年，或者365*3天
            # 这里取当前年份-3的1月1日开始
            start_date = now.replace(year=now.year - 3, month=1, day=1, hour=0, minute=0, second=0)
            return start_date, now

        # Try range "YYYY-MM-DD - YYYY-MM-DD"
        # 兼容各种分隔符
        range_match = re.match(r'(\d{4}-\d{2}-\d{2})\s*[-~to至]\s*(\d{4}-\d{2}-\d{2})', time_filter)
        if range_match:
            try:
                start = datetime.strptime(range_match.group(1), '%Y-%m-%d')
                end = datetime.strptime(range_match.group(2), '%Y-%m-%d')
                # End date implies end of that day?
                end = end.replace(hour=23, minute=59, second=59)
                return start, end
            except:
                pass

        # Try single date "YYYY-MM-DD" (Start Date)
        single_date_match = re.match(r'^(\d{4}-\d{2}-\d{2})$', time_filter.strip())
        if single_date_match:
            try:
                start = datetime.strptime(single_date_match.group(1), '%Y-%m-%d')
                return start, now
            except:
                pass

        # Try single year "2014"
        year_match = re.match(r'^\d{4}$', time_filter.strip())
        if year_match:
            try:
                year = int(year_match.group(0))
                start = datetime(year, 1, 1)
                end = datetime(year, 12, 31, 23, 59, 59)
                return start, end
            except:
                pass

        return None, None

    def extract_date_from_content(self, data: Dict[str, str]) -> Optional[datetime]:
        # 1. 备案及完成时间: 2019-12-31 15:42:13
        if '备案及完成时间' in data:
            try:
                # 可能包含时间，也可能只有日期
                val = data['备案及完成时间']
                if len(val) > 10:
                    return datetime.strptime(val, '%Y-%m-%d %H:%M:%S')
                else:
                    return datetime.strptime(val, '%Y-%m-%d')
            except:
                pass

        # 2. 年份: 2014 (中文 Key)
        if '年份' in data:
            try:
                return datetime(int(data['年份']), 1, 1)
            except:
                pass

        # 3. year: 2014 (英文 Key，来自 MOE 解析)
        if 'year' in data:
            try:
                return datetime(int(data['year']), 1, 1)
            except:
                pass

        # 4. 尝试从 content 字段本身找（如果 parser 没提取出来）
        # 暂时依赖 parser
        return None

    def normalize_education(self, text: str) -> str:
        return self.meta_manager.normalize_education(text)

    def check_rules(self, data: Dict[str, str], regional_rules: Dict, time_range: Tuple) -> bool:

        # 1. Regional Rules Check
        if regional_rules:
            # --- 问卷星数据过滤逻辑 (Questionnaire) ---
            # 识别特征：包含 "岗位" 或 "job_role"
            is_questionnaire = '岗位' in data or 'job_role' in data

            if is_questionnaire:
                # 过滤条件：major（专业）、scope（区域）、level（学历等级）
                # 【重要】问卷数据不需要时间过滤

                # (1) Major Check
                req_major = regional_rules.get('major')
                if req_major:
                    major = data.get('专业') or data.get('major')
                    # 模糊匹配
                    if not major or req_major not in major:
                        return False

                # (2) Scope Check (City/Province)
                req_scope = regional_rules.get('scope')
                if req_scope:
                    loc = data.get('城市') or data.get('省份') or data.get('city') or data.get('province') or ""
                    # 使用 UnifiedMetaManager 进行智能匹配
                    if not self.meta_manager.check_region_match(req_scope, loc):
                        return False

                # (3) Level Check (Education)
                req_level = regional_rules.get('level')
                if req_level:
                    edu = data.get('学历') or data.get('education')

                    # 使用归一化逻辑
                    norm_req = self.normalize_education(req_level)
                    norm_edu = self.normalize_education(edu)

                    # 宽松匹配：归一化后相等，或者互相包含
                    match = False
                    if not edu:
                        match = False
                    elif norm_req == norm_edu:
                        match = True
                    elif norm_req in norm_edu or norm_edu in norm_req:
                        match = True

                    if not match:
                        return False

                # 问卷数据直接返回 True，跳过后续的时间检查
                return True

            else:
                # --- 非问卷数据 (机构备案 & MOE) ---

                # (3) MOE Special Logic
                # 识别特征：包含 "学校标识码" 或 "开设专业"
                is_moe = '学校标识码' in data or ('开设专业' in data and '岗位' not in data)

                if is_moe:
                    # MOE 数据额外检查 major 和 level
                    req_major = regional_rules.get('major')
                    if req_major:
                        major = data.get('开设专业') or data.get('专业') or data.get('major')
                        print(req_major, major)
                        if not major or req_major not in major:
                            return False

                    # Level Check: 只有 regional_rules 里的 level 是 ‘高职’/'高等职业教育（专科）'/'专科' 时才使用 MOE 数据
                    req_level = regional_rules.get('level')
                    # 这里的 valid_moe_levels 也可以用 normalize 判断，但为了保险先保留 list
                    valid_moe_levels = ['高职', '高等职业教育（专科）', '专科', '高职（专科）', '高职专科']

                    # 检查 req_level 是否属于高职类
                    is_vocational = False
                    norm_req = self.normalize_education(req_level)
                    # print(norm_req)
                    if norm_req == "高等职业教育（专科）":
                        # print(11)
                        is_vocational = True
                    else:
                        for v in valid_moe_levels:
                            if v in req_level:
                                is_vocational = True
                                break

                    if not is_vocational:
                        return False

                    # MOE 数据也需要检查 School
                    req_school = regional_rules.get('school')
                    if req_school:

                        name = data.get('school_name') or data.get('institution_name') or data.get(
                            '机构名称') or data.get(
                            '别名')
                        print(req_school, name)
                        if not name or req_school not in name:
                            return False

                    # MOE 数据也需要检查 Scope
                    req_scope = regional_rules.get('scope')
                    if req_scope:
                        loc = data.get('城市') or data.get('省份') or data.get('city') or data.get('province') or ""
                        # 使用 UnifiedMetaManager 进行智能匹配
                        print(req_scope, loc)
                        if not self.meta_manager.check_region_match(req_scope, loc):
                            return False


                else:
                    # --- 托育机构备案数据 (Tuoyu_institution) ---
                    # 过滤条件：scope（区域）和 time_filter（时间范围）
                    # 【重要】School 不是通用的！机构备案数据不检查 School！

                    # (2) Scope Check (通用)
                    req_scope = regional_rules.get('scope')
                    if req_scope:
                        # 字段：城市, 省份, 区域编号(需要映射?), city, province
                        # 机构备案数据通常有 "详细地址" 或 "区域编号"
                        loc = data.get('城市') or data.get('省份') or data.get('city') or data.get(
                            'province') or data.get('详细地址') or ""
                        # 使用 UnifiedMetaManager 进行智能匹配
                        if not self.meta_manager.check_region_match(req_scope, loc):
                            return False

        # 2. Time Filter Check
        # 需求：对于托育机构备案数据使用scope（区域）和time_filter（时间范围）作为条件
        # MOE 数据和问卷数据是否需要时间过滤？
        # 用户明确指出：问卷数据不需要时间过滤。
        # MOE 数据需要时间过滤吗？之前的需求里提到了 MOE 使用 time_filter。
        # 机构备案数据明确需要时间过滤。

        # 因此，只有非问卷数据才进行时间检查
        # is_questionnaire 已经在上面处理并返回了，能走到这里的都是非问卷数据

        if time_range and time_range[0]:
            date_obj = self.extract_date_from_content(data)
            if date_obj:
                start, end = time_range
                # 注意：end 可能是 None (如果输入只是开始时间)
                # 但 parse_time_filter 对于单一日期返回的是 (start, now)
                # 所以这里可以直接比较范围
                if not (start <= date_obj <= end):
                    return False
            else:
                # 如果有时间筛选要求，但数据里没有时间：
                # 严格模式下过滤掉。
                return False

        return True

    async def process(self, tasks: List[Dict], query_groups: List[Dict], regional_rules: Dict, time_filter: str) -> \
            Dict[str, Any]:
        print(f"🚀 [Tuoyu Mode] Rules: {regional_rules}, Time: {time_filter}")

        # 0. 健壮性处理：确保 regional_rules 是字典
        if regional_rules and isinstance(regional_rules, str):
            try:
                # 尝试解析 JSON 字符串
                regional_rules = json.loads(regional_rules)
            except json.JSONDecodeError:
                print(f"⚠️ [Warning] regional_rules is a string but not valid JSON: {regional_rules}")
                regional_rules = {}  # 降级为空字典

        # 1. 构造查询 Query List
        # 为了避免 Rules 中的特定字段（如 school）污染其他类型数据的召回（如机构备案数据），
        # 我们构造两组 Rule String：
        # A. Full Rules: 包含所有字段 (针对 MOE 等强匹配)
        # B. General Rules: 排除 school 字段 (针对 机构备案/问卷 等通用匹配)

        rule_parts_full = []
        rule_parts_general = []

        if regional_rules:
            for k, v in regional_rules.items():
                if not v: continue
                v_str = str(v)

                # Full 包含所有
                rule_parts_full.append(v_str)

                # General 排除 school
                if k != 'school':
                    rule_parts_general.append(v_str)

        rule_str_full = " ".join(rule_parts_full)
        rule_str_general = " ".join(rule_parts_general)

        queries_to_run = set()

        # 基础策略：如果没有 query_groups，直接使用 Rule Strings
        if not query_groups:
            if rule_str_full: queries_to_run.add(rule_str_full)
            if rule_str_general: queries_to_run.add(rule_str_general)
            if not queries_to_run: queries_to_run.add("全部")
        else:
            # 组合策略：Local Query + Rule String
            for group in query_groups:
                for q in group.get('local_queries', []):
                    # 组合 Full
                    if rule_str_full:
                        queries_to_run.add(f"{q} {rule_str_full}".strip())
                    # 组合 General (如果与 Full 不同)
                    if rule_str_general and rule_str_general != rule_str_full:
                        queries_to_run.add(f"{q} {rule_str_general}".strip())
                    # 如果没有 Rules，就只用 q
                    if not rule_str_full and not rule_str_general:
                        queries_to_run.add(q)

        queries_to_run = list(queries_to_run)
        print(f"📋 Generated {len(queries_to_run)} queries: {queries_to_run}")

        time_range = self.parse_time_filter(time_filter)

        # Store results as (db_id, doc_info)
        final_results_list = []

        for task in tasks:
            db_id = task.get('database_id')
            if not db_id: continue

            print(f"🔍 [Tuoyu] Searching DB: {db_id}")

            # Step 1: 并发召回所有 Query 的结果 (Retrieve Chunks)
            # 这里的 payload 可以复用
            payload = {
                "database_id_for_url": db_id,
                # "top_k": 100
            }

            # Create retrieve tasks
            retrieve_coros = [self.api.retrieve(q, payload.copy()) for q in queries_to_run]
            results_list = await asyncio.gather(*retrieve_coros)

            # Flatten chunks
            all_chunks = []
            seen_chunk_ids = set()
            for res in results_list:
                for chunk in res:
                    # 简单去重，避免重复处理
                    cid = chunk.get('id')
                    if cid not in seen_chunk_ids:
                        all_chunks.append(chunk)
                        seen_chunk_ids.add(cid)

            print(f"   -> Retrieved {len(all_chunks)} unique chunks from raw search")

            # Step 2: 筛选相关文档 ID (去重 + 规则过滤)
            relevant_doc_ids = set()
            for chunk in all_chunks:
                # 解析内容
                content_data = TuoyuContentParser.parse_key_value_lines(chunk['content'])
                # 规则检查
                if self.check_rules(content_data, regional_rules, time_range):
                    relevant_doc_ids.add(chunk['document_id'])

            print(f"   -> Found {len(relevant_doc_ids)} relevant unique documents")

            # Step 3: 获取完整文档并再次过滤 (Fetch Full Doc & Filter Segments)
            async def process_doc(d_id):
                # 获取详情
                d_detail = await self.api.fetch_document_detail(db_id, d_id)
                if not d_detail: return None

                # 获取分段
                segs = await self.api.fetch_all_segments(db_id, d_id)

                # 过滤分段
                valid_segs = []
                for seg in segs:
                    s_content = seg.get('content', '')
                    s_data = TuoyuContentParser.parse_key_value_lines(s_content)
                    if self.check_rules(s_data, regional_rules, time_range):
                        valid_segs.append(seg)

                if not valid_segs: return None

                # 构造结果
                content_blocks = []
                # 保持原有的排序逻辑
                valid_segs.sort(key=lambda x: x.get('position', 0))

                for s in valid_segs:
                    # 解析内容以获取结构化数据
                    s_content = s.get("content", "")
                    s_parsed = TuoyuContentParser.parse_key_value_lines(s_content)
                    structured_data = s_parsed.get('_structured_data', {})

                    content_blocks.append({
                        "content": s_content,
                        "position": s.get("position"),
                        "score": 1.0,
                        # 关键修改：将结构化数据直接放在每个 block 的 doc_metadata 字段中
                        "doc_metadata": structured_data
                    })

                # 确定 Source Type
                sample_content = valid_segs[0].get('content', '') if valid_segs else ""
                sample_data = TuoyuContentParser.parse_key_value_lines(sample_content)

                if '岗位' in sample_data or 'job_role' in sample_data:
                    source_type = 'Tuoyu_Questionnaire'
                else:
                    source_type = 'Tuoyu_institution'

                # 手动构建最终文档结构，绕过 ContentFormatter 以保留 block 级别的 metadata
                # 顶层 doc_metadata 保留原始文档的元数据 (清洗后)
                top_meta = ContentFormatter.clean_metadata(d_detail.get("doc_metadata", []))

                fmt_doc = {
                    "doc_metadata": top_meta,
                    "document_id": d_id,
                    "document_name": d_detail.get('name'),
                    "source_type": source_type,
                    "content_blocks": content_blocks
                }

                return fmt_doc

            # Execute doc processing
            doc_tasks = [process_doc(did) for did in relevant_doc_ids]
            doc_results = await asyncio.gather(*doc_tasks, return_exceptions=True)

            for res in doc_results:
                if isinstance(res, Exception):
                    print(f"⚠️ [Doc Process Error] {repr(res)}")
                    continue
                if res:
                    final_results_list.append((db_id, res))

        # 构造最终输出，保留 query_groups 中的 ID 等信息
        packaged_data = self._package_results(final_results_list)

        final_output_list = []
        if query_groups:
            for group in query_groups:
                # 复制原始组信息 (包含 id 等)
                res_item = group.copy()
                # 挂载检索结果
                # 注意：Tuoyu 模式下，所有 group 共享同一份基于规则的全局检索结果
                res_item["retrieve_data"] = packaged_data
                final_output_list.append(res_item)
        else:
            # 如果没有 query_groups，返回默认结构
            final_output_list.append({"retrieve_data": packaged_data})

        return {"result": final_output_list}

    def _package_results(self, results_list: List[Tuple[str, Dict]]) -> List[Dict]:
        grouped = defaultdict(list)
        for db_id, doc in results_list:
            grouped[db_id].append(doc)

        output = []
        for db_id, docs in grouped.items():
            output.append({
                "database_id": db_id,
                "document_infos": docs
            })
        return output


# --- 主程序入口 ---
async def async_main(tasks: List[Dict], query_groups: List[Dict] = None,
                     regional_rules: Any = None, time_filter: Any = None, run_mode: str = "X-Pilot") -> Dict[str, Any]:
    if not tasks: return {"result": []}

    client = DifyApiClient()

    try:
        # --- Tuoyu Mode Branch ---
        if run_mode == "Tuoyu":
            processor = TuoyuProcessor(client)
            # Re-implement packaging logic inside process or here
            # Let's verify process implementation

            # We need to pass DB ID out.
            # Let's modify TuoyuProcessor.process slightly to return structured data directly
            # Or handle it here.

            # Better to fix TuoyuProcessor.process to return the correct structure.
            return await processor.process(tasks, query_groups, regional_rules, time_filter)

        # --- Standard X-Pilot Mode ---
        if not query_groups: query_groups = [{"slide_id": "default", "local_queries": []}]

        orchestrator = RetrievalOrchestrator(client)
        # ... existing logic ...

        # --- Stage 0: 任务归类 ---
        rag_tasks = [t for t in tasks if t.get("retrieval_mode") != "full_document_retrieval"]
        full_doc_tasks = [t for t in tasks if t.get("retrieval_mode") == "full_document_retrieval"]

        # Step 1: 预热 (Metadata Prefetch)
        await orchestrator.prefetch_metadata(tasks)

        # # Step 2: 计划 (Plan Construction)
        # plan = orchestrator.build_execution_plan(tasks)
        # debug_print(plan, "Execution Plan")
        #
        # # Step 3: 执行 (Concurrent Slide Processing)
        # slide_tasks = [
        #     orchestrator.process_slide(group, plan, tasks)
        #     for group in query_groups
        # ]
        # slide_results = await asyncio.gather(*slide_tasks)
        #
        # return {"result": slide_results}

        # --- Stage 2: 并发计划 ---
        # 我们需要同时做两件事：
        # A. 跑所有的 Slide RAG
        # B. 跑一次 Full Document 下载

        # 2.1 准备 RAG 任务
        plan = orchestrator.build_execution_plan(rag_tasks)

        # 2.2 定义 A 组协程 (RAG)
        rag_coros = [
            orchestrator.process_slide(group, plan, rag_tasks)
            for group in query_groups
        ]

        # 2.3 定义 B 组协程 (Full Doc)
        full_doc_coros = [
            orchestrator.process_full_document_task(t)
            for t in full_doc_tasks
        ]

        print(f"🚀 [Execute] Running {len(rag_coros)} slides RAG & {len(full_doc_coros)} doc fetches...")

        # --- Stage 3: 并发执行 A 和 B ---
        # all_results 结构: [Slide1_Res, Slide2_Res, ..., Doc1_Res, Doc2_Res]
        all_results = await asyncio.gather(*(rag_coros + full_doc_coros))

        # --- Stage 4: 结果分离与注入 (The Injection) ---

        # 切分结果列表
        split_idx = len(rag_coros)
        slide_results = list(all_results[:split_idx])  # 只有 Slide 结果
        doc_resources = list(all_results[split_idx:])  # 只有 Full Doc 结果 (标准化的 Dict)
        # 【核心逻辑】：将 doc_resources 注入到每一个 Slide 的 retrieve_data 中
        # 这样保证了“一次获取，处处可用”，且维持了 retrieve_data 的结构统一性
        if doc_resources:
            for slide in slide_results:
                # 建立当前 slide 已有的 DB 映射表，方便合并
                existing_dbs = {item["database_id"]: item for item in slide["retrieve_data"]}

                for res in doc_resources:
                    db_id = res["database_id"]

                    # 如果该 Database 已存在于 RAG 结果中，则将 Full Doc 的 document_infos 合并进去
                    if db_id in existing_dbs:
                        # res["document_infos"] 里的元素已经带有了 source_type="document"
                        existing_dbs[db_id]["document_infos"].extend(res["document_infos"])
                    else:
                        # 如果是新的 Database，直接添加
                        slide["retrieve_data"].append(res)
        return {"result": slide_results}

    finally:
        await client.close()


def main(tasks: List[Dict], query_groups: List[Dict] = None,
         regional_rules: Any = None, time_filter: Any = None, run_mode: str = "X-Pilot") -> Dict[str, Any]:
    """Dify 节点的主入口点"""
    try:
        return debug_print(asyncio.run(async_main(tasks, query_groups, regional_rules, time_filter, run_mode)))
    except Exception as e:
        import traceback
        return {
            "result": [
                {
                    "error": f"Workflow Failed: {str(e)}",
                    "traceback": traceback.format_exc()
                }
            ]
        }


main([
    # {
    #     "database_id": "5bf50c7a-3ba4-46c7-bbdc-71d68f641e0a",
    #     "document_id": "6cc5b1e2-3bf3-47a8-b370-0eb0c4516c08",
    #     "retrieval_mode": "segment_retrieval"
    # },
    # {
    #     "database_id": "e22029c2-dcbe-448e-89bc-fcca8ef70f5f",
    #     "retrieval_mode": "full_database_retrieval"
    # },
    # {
    #     "database_id": "150eb839-1a35-4577-b994-c6c274dc9d15",
    #     "retrieval_mode": "full_database_retrieval"
    # },
    {
        "database_id": "74355311-f07c-4aec-b5e5-12d19652ded6",
        "retrieval_mode": "full_database_retrieval"
    },
],
    [
        {
            "local_queries": [
                "上海市 上海海事大学",
                "国际航运业务管理"
            ],
            "slide_id": "chapter_1_slide_1"
        }
    ],
    regional_rules={
        "school": "上海应用技术大学",
        "major": "机电一体化技术",  # ,学前教育
        "scope": "上海市",
        "level": "高职（专科）"}, run_mode="Tuoyu", time_filter="2020")
