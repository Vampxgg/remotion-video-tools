# Dify 依赖管理: 请确保已添加 httpx, json-repair, tavily-python
import asyncio
import random
from itertools import cycle

import httpx
import re
import os
import json
import traceback
from typing import Any, Dict, List, Coroutine, Literal, Optional, Union
from abc import ABC, abstractmethod

# 引入新依赖的客户端
from tavily import AsyncTavilyClient
import json_repair

# ==============================================================================
# ====================== DIFY 本地调试辅助模块 =========================
# ==============================================================================
import pprint

# --- 本地调试开关 ---
# 在你的 IDE 中进行测试时，将此值设为 True。
# 当你准备将代码复制到 Dify 平台时，请将其改回 False，或直接删除此调试模块。
IS_LOCAL_DEBUG = True


def _dify_debug_return(data: Dict[str, Any], label: str = "Final Return") -> Dict[str, Any]:
    """
    一个用于在 Dify 代码节点中进行本地调试的包装函数。

    当 IS_LOCAL_DEBUG 为 True 时，它会漂亮地打印出最终要返回的数据，
    然后原封不动地返回该数据，以便 Dify 平台能正确接收。

    Args:
        data (Dict[str, Any]): 准备从 Dify 节点返回的数据。
        label (str, optional): 一个标签，用于在控制台输出中标识来源。默认为 "Final Return"。

    Returns:
        Dict[str, Any]: 传入的原始数据。
    """
    if IS_LOCAL_DEBUG:
        # 打印一个清晰的分隔符和标签，方便在终端中识别
        print("\n" + "=" * 40 + f" DIFY DEBUG OUTPUT [{label}] " + "=" * 40)

        # 使用 pprint 模块进行美化输出，对复杂的嵌套字典特别友好
        pprint.pprint(data, indent=2, width=120)

        # 打印结束分隔符
        print("=" * 105 + "\n")

    # 无论是否打印，都必须原封不动地返回原始数据
    return data


def _intelligent_input_parser(raw_input: Any) -> Dict[str, Any]:
    """
    健壮地解析复杂的输入结构，提取web搜索查询、职业查询数据和企业查询名称。
    """
    if isinstance(raw_input, list) and len(raw_input) > 0:
        actual_input = raw_input[0]
    else:
        actual_input = raw_input

    if isinstance(actual_input, str):
        if not actual_input.strip():
            # 【调整】如果输入为空字符串，返回所有字段都为空的结构，避免下游报错。
            return {"comprehensive_queries": [], "career_query_data": {}, "tianyan_enterprise_names": []}
        try:
            data = json_repair.loads(actual_input)
        except Exception as e:
            raise ValueError(f"Failed to parse input string as JSON: {e}\nOriginal input: {str(actual_input)[:500]}...")
    elif isinstance(actual_input, dict):
        data = actual_input
    else:
        raise TypeError(f"Expected input type is str, list, or dict, but received {type(actual_input).__name__}")
    if not isinstance(data, dict):
        raise ValueError("Parsed data is not a valid object (dictionary).")

    # 安全地提取 web_queries 对象
    web_queries_obj = data.get("web_queries", {})
    if not isinstance(web_queries_obj, dict): web_queries_obj = {}
    # 1. 提取 comprehensive_query
    comprehensive_queries = []
    query_list = web_queries_obj.get("comprehensive_query", [])
    if isinstance(query_list, list) and all(isinstance(item, str) for item in query_list):
        comprehensive_queries = query_list

    # 2. 提取 career_query
    career_query_data = web_queries_obj.get("career_query", {})
    if not isinstance(career_query_data, dict): career_query_data = {}
    # 3. 提取 tianyan_check_enterprise
    tianyan_input = web_queries_obj.get("tianyan_check_enterprise", [])  # 默认值改为空列表
    tianyan_enterprise_names: List[str] = []

    if isinstance(tianyan_input, str):
        # 如果是字符串，清理后包装成单元素列表
        cleaned_name = tianyan_input.strip()
        if cleaned_name:
            tianyan_enterprise_names.append(cleaned_name)
    elif isinstance(tianyan_input, list):
        # 如果是列表，清理并过滤掉无效元素
        tianyan_enterprise_names = [
            str(item).strip() for item in tianyan_input
            if isinstance(item, str) and str(item).strip()
        ]

    return {
        "comprehensive_queries": comprehensive_queries,
        "career_query_data": career_query_data,
        "tianyan_enterprise_names": tianyan_enterprise_names
    }


# ==============================================================================
# ====================== 全局搜索策略配置 ======================
# ==============================================================================
SEARCH_STRATEGY_CONFIG = {
    # 1. 泛用网页搜索
    "web": {
        "includes": [
            # --- 行业报告与分析 ---
            "site:36kr.com",  # 36氪
            "site:iresearch.com.cn",  # 艾瑞咨询
            "site:questmobile.com.cn",  # QuestMobile
            "site:caixin.com",  # 财新网
            "site:iyiou.com",  # 亿欧网
            "site:pedata.cn",  # 清科研究
            "site:www.deloitte.com/cn/",  # 德勤中国
            "site:www.pwccn.com",  # 普华永道中国
            "site:www.ey.com/zh_cn",  # 安永中国
            "site:home.kpmg/cn/",  # 毕马威中国
            "site:research.cicc.com",  # 中金公司研究部

            # --- 政策文件与专利 ---
            "site:gov.cn",  # 中国政府网
            "site:ndrc.gov.cn",  # 发改委
            "site:miit.gov.cn",  # 工信部
            "site:most.gov.cn",  # 科技部
            "site:cnipa.gov.cn",  # 国家知识产权局
            "site:patents.google.com",  # 谷歌专利
            "site:soopat.com",  # Soopat 专利搜索

            # --- 专家访谈与专业洞见 ---
            "site:zhihu.com",  # 知乎
            "site:infoq.cn",  # InfoQ中国
            "site:csdn.net",  # CSDN
            "site:xueqiu.com",  # 雪球

            # --- 企业招聘与岗位描述 ---
            "site:zhipin.com",  # BOSS直聘
            "site:liepin.com",  # 猎聘
            "site:zhaopin.com",  # 智联招聘
            "site:lagou.com",  # 拉勾网
            "site:maimai.cn"  # 脉脉
        ],
        "excludes": [
            # 通用的排除规则，以过滤掉噪音
            "-filetype:pdf", "-filetype:docx", "-filetype:xlsx", "-filetype:pptx",
            "-inurl:login", "-inurl:register"
        ]
    },

    # 2. 视频搜索 (Video Search)
    "video": {
        "includes": [
            "site:douyin.com",
            "site:bilibili.com",
            "site:ixigua.com",
            "site:youtube.com"
        ]
    }
}


def _build_filtered_query(original_query: str, search_type: str) -> str:
    """
    根据全局配置，为原始查询构建带有过滤条件的最终查询字符串。
    【新功能】: 对于 'web' 类型，会从 'includes' 列表中随机抽取指定数量的域进行搜索。
    """
    strategy = SEARCH_STRATEGY_CONFIG.get(search_type, {})
    final_query_parts = [original_query]

    # --- 处理 'includes' 规则 ---
    inclusions = strategy.get("includes", [])
    if inclusions:
        selected_sites = []
        if search_type == 'web':
            # 【核心逻辑】如果是web搜索，进行随机抽样
            num_to_select = 3  # 每次随机选择2个域
            # 确保抽样数量不超过列表总数，防止出错
            k = min(num_to_select, len(inclusions))
            if k > 0:
                selected_sites = random.sample(inclusions, k)
                print(f"  -> [策略] 随机选择 {k} 个高质量域: {selected_sites}")
        else:
            # 对于其他类型（如'video'），使用所有指定的域
            selected_sites = inclusions

        if selected_sites:
            sites_query_part = f"({' OR '.join(selected_sites)})"
            final_query_parts.append(sites_query_part)

    # --- 处理 'excludes' 规则 ---
    exclusions = strategy.get("excludes", [])
    if exclusions:
        final_query_parts.extend(exclusions)
    # 3. 将所有部分用空格连接成最终的查询字符串
    print("-" * 20)
    print(" ".join(final_query_parts).strip())
    return " ".join(final_query_parts).strip()


DEFAULT_VIDEO_THUMBNAIL = "https://server.x-pilot.cn/static/meta-doc/png/498e68eb20054a03f2f8eb00f46a81d3.png"


def _parse_video_url(url: str) -> Dict[str, Optional[str]]:
    """
    一个通用的视频URL解析器，用于从URL中提取元数据。
    可轻松扩展以支持更多平台。
    """
    # 抖音 (douyin.com)
    douyin_match = re.search(r'/video/(\d+)', url)
    if douyin_match:
        video_id = douyin_match.group(1)
        return {"video_id": video_id, "embed_url": url, "thumbnail_url": DEFAULT_VIDEO_THUMBNAIL}
    # Bilibili (bilibili.com)
    bilibili_match = re.search(r'bilibili\.com/video/(BV[a-zA-Z0-9]+)', url)
    if bilibili_match:
        video_id = bilibili_match.group(1)
        return {"video_id": video_id, "embed_url": f"//player.bilibili.com/player.html?bvid={video_id}",
                "thumbnail_url": DEFAULT_VIDEO_THUMBNAIL}  # B站封面图需要API获取，暂不处理
    # 如果没有匹配到任何已知平台，返回基本信息
    return {"video_id": None, "embed_url": url, "thumbnail_url": DEFAULT_VIDEO_THUMBNAIL}


# --- 智能解析模块 ---
# def _intelligent_input_parser(raw_input: Any) -> Dict[str, Any]:
#     """
#     健壮地解析复杂的输入结构，提取web搜索查询、职业查询数据和企业查询名称。
#     """
#     if isinstance(raw_input, list) and len(raw_input) > 0:
#         actual_input = raw_input[0]
#     else:
#         actual_input = raw_input
#     if isinstance(actual_input, str):
#         if not actual_input.strip(): raise ValueError("Input string is empty or contains only whitespace.")
#         try:
#             data = json_repair.loads(actual_input)
#         except Exception as e:
#             raise ValueError(f"Failed to parse input string as JSON: {e}\nOriginal input: {str(actual_input)[:500]}...")
#     elif isinstance(actual_input, dict):
#         data = actual_input
#     else:
#         raise TypeError(f"Expected input type is str, list, or dict, but received {type(actual_input).__name__}")
#
#     if not isinstance(data, dict): raise ValueError("Parsed data is not a valid object (dictionary).")
#     # 安全地提取 web_queries 对象
#     web_queries_obj = data.get("web_queries", {})
#     if not isinstance(web_queries_obj, dict): web_queries_obj = {}
#     # 1. 提取 comprehensive_query
#     comprehensive_queries = []
#     query_list = web_queries_obj.get("comprehensive_query", [])
#     if isinstance(query_list, list) and all(isinstance(item, str) for item in query_list):
#         comprehensive_queries = query_list
#     # 2. 提取 career_query
#     career_query_data = web_queries_obj.get("career_query", {})
#     if not isinstance(career_query_data, dict): career_query_data = {}
#
#     # 3. 【调整】提取 tianyan_check_enterprise
#     tianyan_enterprise_name = web_queries_obj.get("tianyan_check_enterprise", "")
#     if not isinstance(tianyan_enterprise_name, str): tianyan_enterprise_name = ""
#     return {
#         "comprehensive_queries": comprehensive_queries,
#         "career_query_data": career_query_data,
#         "tianyan_enterprise_name": tianyan_enterprise_name.strip()
#     }
#

# 【新增】一个可靠的抖音元数据抓取函数
# async def _fetch_douyin_metadata_reliably(
#         url: str,
#         client: httpx.AsyncClient
# ) -> Optional[Dict[str, str]]:
#     """
#     通过访问抖音页面HTML来可靠地获取视频ID和封面图。
#     - 自动处理 v.douyin.com 短链接跳转。
#     - 从页面的 <meta property="og:image"> 标签中解析封面图。
#     - 从最终的URL中解析视频ID。
#     """
#     try:
#         print(f"🔗 [Douyin Scraper] 开始解析 URL: {url}")
#
#         # 步骤 1: 发送 HEAD 请求以处理短链接跳转，获取最终的URL
#         # 使用 HEAD 请求比 GET 更快，因为它只获取头部信息
#         headers = {
#             'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1'
#         }
#         # 【注意】需要设置 allow_redirects=True
#         head_resp = await client.head(url, headers=headers, timeout=15, follow_redirects=True)
#         final_url = str(head_resp.url)
#         print(f"  -> 跳转后最终 URL: {final_url}")
#
#         # 步骤 2: 从最终URL中解析视频ID
#         video_id_match = re.search(r'/video/(\d+)', final_url)
#         if not video_id_match:
#             print(f"  -> ⚠️ 无法从最终URL中解析到 video_id。")
#             return None
#         video_id = video_id_match.group(1)
#
#         # 步骤 3: 访问最终页面，获取HTML内容
#         get_resp = await client.get(final_url, headers=headers, timeout=15)
#         get_resp.raise_for_status()
#         html_content = get_resp.text
#
#         # 步骤 4: 使用正则表达式快速从HTML中解析 og:image 标签内容
#         # 这种方法比完整的BeautifulSoup解析更快，对于目标明确的场景非常高效
#         og_image_match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html_content)
#
#         if og_image_match:
#             thumbnail_url = og_image_match.group(1)
#             print(f"  -> ✅ 成功抓取到封面图: {thumbnail_url}")
#             return {
#                 "video_id": video_id,
#                 "embed_url": final_url,
#                 "thumbnail_url": thumbnail_url
#             }
#         else:
#             print(f"  -> ⚠️ 页面HTML中未找到 og:image 标签。")
#             return {"video_id": video_id, "embed_url": final_url, "thumbnail_url": None}
#
#     except Exception as e:
#         print(f"  -> ❌ 解析抖音URL时发生错误: {e}")
#         return None


# --- 检索策略模块 (保持不变) ---
class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, client: httpx.AsyncClient, num_results: int,
                     search_type: Literal['web', 'video']) -> List[Dict[str, Any]]: pass

    def _prefix_keys(self, result: Dict[str, Any], prefix: str) -> Dict[str, Any]: return {f"{prefix}_{key}": value for
                                                                                           key, value in result.items()}


class SearchApiIoProvider(SearchProvider):
    # 省略实现...
    def __init__(self):
        self.api_key = os.environ.get("SEARCHAPI_IO_API_KEY", "7MPC8616NewB263CoB1NMcgS")
        if not self.api_key: raise ValueError("SearchAPI.io API Key is not provided.")
        self.base_url = "https://www.searchapi.io/api/v1/search"
        self.prefix = "searchapi"

    # @staticmethod
    # def _extract_youtube_info(url: str) -> Dict[str, Optional[str]]:
    #     match = re.search(r"(?:v=|youtu\.be/|embed/)([a-zA-Z0-9_-]{11})", url)
    #     if not match: return {"video_id": None, "embed_url": None, "thumbnail_url": None}
    #     video_id = match.group(1)
    #     return {"video_id": video_id, "embed_url": f"https://www.youtube.com/embed/{video_id}",
    #             "thumbnail_url": f"https://img.youtube.com/vi/{video_id}/0.jpg"}
    #
    # def _extract_douyin_info(self, url: str) -> Dict[str, Optional[str]]:
    #     """
    #     从抖音的分享链接中提取视频ID，并返回固定的占位缩略图。
    #     """
    #     video_id = None
    #     # 正则表达式匹配抖音视频ID (通常是一长串数字)
    #     match = re.search(r'/video/(\d+)', url)
    #     if match:
    #         video_id = match.group(1)
    #     # 【调整】无论是否提取到 video_id，都返回固定的占位图URL
    #     return {
    #         "video_id": video_id,
    #         "embed_url": url,  # 直接使用原始URL作为嵌入链接
    #         "thumbnail_url": self.DEFAULT_DOUYIN_THUMBNAIL
    #     }

    async def search(self, query: str, client: httpx.AsyncClient, num_results: int,
                     search_type: Literal['web', 'video'] = 'web') -> List[Dict[str, Any]]:
        try:
            params = {"q": query, "engine": "google", "gl": "cn", "hl": "zh-cn", "num": num_results,
                      "api_key": self.api_key}
            resp = await client.get(self.base_url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json().get("organic_results", [])

            results = []
            for item in data:
                if not item.get("link"): continue
                base_info = {"type": search_type, "url": item.get("link"), "title": item.get("title"),
                             "source": item.get("source", ""), "snippet": item.get("snippet", "")}
                if search_type == 'video': base_info.update(_parse_video_url(item.get("link")))
                results.append(self._prefix_keys(base_info, self.prefix))
            return results

            # tasks = []
            # original_items = []
            # # 【调整】对于视频搜索，我们先收集所有链接，然后并发地去抓取元数据
            # if search_type == 'video':
            #     for item in data:
            #         link = item.get("link")
            #         if link:
            #             # 创建并发任务
            #             tasks.append(_fetch_douyin_metadata_reliably(link, client))
            #             original_items.append(item)
            #
            #     # 并发执行所有抖音元数据抓取任务
            #     metadata_results = await asyncio.gather(*tasks)
            #     # 将抓取结果与原始搜索结果合并
            #     results = []
            #     for i, item in enumerate(original_items):
            #         metadata = metadata_results[i]
            #         base_info = {
            #             "type": search_type,
            #             "url": item.get("link"),
            #             "title": item.get("title"),
            #             "source": item.get("source", "抖音"),
            #             "snippet": item.get("snippet", "")
            #         }
            #         if metadata:
            #             base_info.update(metadata)
            #         else:
            #             # 如果抓取失败，则填充空值
            #             base_info.update({"video_id": None, "embed_url": item.get("link"), "thumbnail_url": None})
            #
            #         results.append(self._prefix_keys(base_info, self.prefix))
            #     return results
            # # 对于Web搜索，逻辑保持不变
            # else:
            #     results = []
            #     for item in data:
            #         if not item.get("link"): continue
            #         base_info = {
            #             "type": search_type,
            #             "url": item.get("link"),
            #             "title": item.get("title"),
            #             "source": item.get("source", ""),
            #             "snippet": item.get("snippet", "")
            #         }
            #         results.append(self._prefix_keys(base_info, self.prefix))
            #     return results

        except Exception as e:
            return [self._prefix_keys({"type": search_type, "error": f"SearchAPI.io request failed for '{query}': {e}"},
                                      self.prefix)]


class JinaSearchProvider(SearchProvider):
    def __init__(self):
        self.api_key = os.environ.get("JINA_API_KEY",
                                      "jina_b4348ffc39ca47bfbe753b95f59428c7i6ifkOFXRPdF3dRa5Rwb6T8FvrLH")
        if not self.api_key: raise ValueError("Jina.ai API Key is not provided.")
        self.base_url = "https://s.jina.ai/"
        # **关键修正 1: 更新请求头，与官方文档保持一致**
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Respond-With": "no-content"
        }
        self.prefix = "jina"

    async def search(self, query: str, client: httpx.AsyncClient, num_results: int,
                     search_type: Literal['web', 'video'] = 'web') -> List[Dict[str, Any]]:
        # if search_type == 'video': return []
        try:
            # **关键修正 2: 更新请求体，加入本地化参数**
            data = {
                "q": query,
                "gl": "CN",
                "hl": "zh-cn"
            }
            resp = await client.post(self.base_url, headers=self.headers, json=data, timeout=30)  # 增加超时时间
            resp.raise_for_status()

            # **关键修正 3: 直接解析响应文本，避免潜在的编码问题**
            # Jina API 返回的可能是非标准JSON，直接用 .text
            # httpx 的 .json() 可能会严格检查 content-type
            api_response = json.loads(resp.text)

            api_results = api_response.get("data", [])
            results = []
            for item in api_results[:num_results]:
                if not item.get("url"): continue
                base_info = {"type": search_type, "url": item.get("url"), "title": item.get("title"),
                             "snippet": item.get("description"), "content": item.get("content", "")}
                if search_type == 'video': base_info.update(_parse_video_url(item.get("url")))
                results.append(self._prefix_keys(base_info, self.prefix))
            return results
        except Exception as e:
            return [
                self._prefix_keys({"type": search_type, "error": f"Jina.ai request failed for '{query}': {e}"},
                                  self.prefix)]


class FirecrawlSearchProvider(SearchProvider):
    # 省略实现...
    def __init__(self):
        self.api_key = os.environ.get("FIRECRAWL_API_KEY", "fc-a36b7d2fb273485680d0fe6abd686935")
        if not self.api_key: raise ValueError("Firecrawl API Key is not provided.")
        self.base_url = "https://api.firecrawl.dev/v2/search"
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        self.prefix = "firecrawl"

    async def search(self, query: str, client: httpx.AsyncClient, num_results: int,
                     search_type: Literal['web', 'video'] = 'web') -> List[Dict[str, Any]]:
        # if search_type == 'video': return []
        try:
            data = {"query": query, "limit": num_results}
            resp = await client.post(self.base_url, headers=self.headers, json=data, timeout=30)
            resp.raise_for_status()
            api_results = resp.json().get("data", {}).get("web", [])
            results = []
            for item in api_results:
                if not item.get("url"): continue
                base_info = {"type": search_type, "url": item.get("url"), "title": item.get("title"),
                             "snippet": item.get("description"), "markdown": item.get("markdown", "")}
                if search_type == 'video': base_info.update(_parse_video_url(item.get("url")))
                results.append(self._prefix_keys(base_info, self.prefix))
            return results
        except Exception as e:
            return [self._prefix_keys({"type": search_type, "error": f"Firecrawl request failed for '{query}': {e}"},
                                      self.prefix)]


class TavilySearchProvider(SearchProvider):
    # 省略实现...
    def __init__(self):
        self.api_key = os.environ.get("TAVILY_API_KEY", "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")
        if not self.api_key: raise ValueError("Tavily API Key is not provided.")
        self.async_client = AsyncTavilyClient(self.api_key)
        self.prefix = "tavily"

    async def search(self, query: str, client: httpx.AsyncClient, num_results: int,
                     search_type: Literal['web', 'video'] = 'web') -> List[Dict[str, Any]]:
        # if search_type == 'video': return []
        try:
            api_results = await self.async_client.search(query=query, search_depth="basic", max_results=num_results)
            results = []
            for item in api_results.get("results", []):
                if not item.get("url"): continue
                base_info = {"type": search_type, "url": item.get("url"), "title": item.get("title"),
                             "snippet": item.get("content"), "score": item.get("score")}
                if search_type == 'video': base_info.update(_parse_video_url(item.get("url")))
                results.append(self._prefix_keys(base_info, self.prefix))
            return results
        except Exception as e:
            return [
                self._prefix_keys({"type": search_type, "error": f"Tavily request failed for '{query}': {e}"},
                                  self.prefix)]


class ZhiLianJobProvider:
    """一个“虚拟”提供商，仅用于提取和传递职业数据。"""

    def get_data(self, career_query_input: Dict) -> Dict:
        print("💼 [ZhiLianJobProvider] 正在提取职业数据...")
        return career_query_input


class TianyanCheckProvider:
    def get_data(self, tianyan_input: Any) -> Any:
        print("🔭 [TianyanCheckProvider] 正在提取企业数据...")
        return tianyan_input


# --- 核心检索控制器 (保持不变) ---
# class MultiSourceSearcher:
#     # 省略实现...
#     def __init__(self):
#         self.providers: Dict[str, SearchProvider] = {"searchapi_io": SearchApiIoProvider(),
#                                                      "jina": JinaSearchProvider(),
#                                                      "firecrawl": FirecrawlSearchProvider(),
#                                                      "tavily": TavilySearchProvider()}
#
#     async def _search_single_provider(self, queries, provider_name, client, web_count, video_count):
#         provider = self.providers[provider_name]
#         tasks = []
#         for q in queries:
#             if web_count > 0: tasks.append(provider.search(q, client, web_count, 'web'))
#             if video_count > 0: tasks.append(provider.search(q, client, video_count, 'video'))
#         task_results = await asyncio.gather(*tasks, return_exceptions=True)
#         urls_info, task_idx = [], 0
#         for query in queries:
#             res = {"query": query, "web_results": [], "video_results": [], "errors": []}
#             for stype, count, key in [('web', web_count, 'web_results'), ('video', video_count, 'video_results')]:
#                 if count > 0:
#                     result = task_results[task_idx]
#                     task_idx += 1
#                     if isinstance(result, Exception):
#                         res["errors"].append(f"[{provider.prefix}] {stype.capitalize()} search task failed: {result}")
#                     elif result and result[0].get(f'{provider.prefix}_error'):
#                         res["errors"].append(result[0][f'{provider.prefix}_error'])
#                     else:
#                         res[key] = result
#             urls_info.append(res)
#         return urls_info
#
#     async def _search_all_providers(self, queries, client, web_count, video_count):
#         async def search_and_tag(p_name, provider, query, num, stype):
#             try:
#                 data = await provider.search(query, client, num, stype)
#                 return {"query": query, "provider": p_name, "type": stype, "data": data, "exception": None}
#             except Exception as e:
#                 return {"query": query, "provider": p_name, "type": stype, "data": [], "exception": e}
#
#         tasks = []
#         for q in queries:
#             for name, provider in self.providers.items():
#                 if web_count > 0: tasks.append(search_and_tag(name, provider, q, web_count, 'web'))
#                 if video_count > 0: tasks.append(search_and_tag(name, provider, q, video_count, 'video'))
#         task_results = await asyncio.gather(*tasks)
#         agg = {q: {"query": q, "web_results": [], "video_results": [], "errors": []} for q in queries}
#         for res in task_results:
#             query, p_name, stype, data, exc = res["query"], res["provider"], res["type"], res["data"], res["exception"]
#             provider_prefix = self.providers[p_name].prefix
#             if exc:
#                 agg[query]["errors"].append(
#                     f"[{provider_prefix}] {stype.capitalize()} search task threw an exception: {exc}")
#                 continue
#             if data and data[0].get(f'{provider_prefix}_error'):
#                 agg[query]["errors"].append(data[0][f'{provider_prefix}_error'])
#             elif stype == 'web':
#                 agg[query]["web_results"].extend(data)
#             elif stype == 'video':
#                 agg[query]["video_results"].extend(data)
#         return list(agg.values())
#
#     async def search(self, queries, provider_name, web_count, video_count):
#         async with httpx.AsyncClient() as client:
#             if provider_name.lower() == "all":
#                 return await self._search_all_providers(queries, client, web_count, video_count)
#             else:
#                 if provider_name.lower() not in self.providers: raise ValueError(
#                     f"Provider '{provider_name}' not found.")
#                 return await self._search_single_provider(queries, provider_name.lower(), client, web_count,
#                                                           video_count)
#
#
# # --- Dify 异步主函数 ---
# async def main_async(raw_input: Any, provider_name: str, web_results_count: int, video_results_count: int) -> Dict[
#     str, Any]:
#     # 这个函数现在只处理成功路径，所有异常由调用者 main 捕获
#     parsed_input = _intelligent_input_parser(raw_input)
#     queries = parsed_input["queries"]
#     searcher = MultiSourceSearcher()
#     urls_info = await searcher.search(
#         queries,
#         provider_name=provider_name,
#         web_count=web_results_count,
#         video_count=video_results_count
#     )
#     return {
#         "urls_info": urls_info,
#         "urls_info_str": json.dumps(urls_info, ensure_ascii=False, indent=2)
#     }
#
#
# # --- Dify 同步入口 (已彻底重构错误处理和输入验证) ---
# def main(
#         raw_input: Any,
#         provider: str = "tavily",
#         web_results: Any = 3,
#         video_results: Any = 0
# ) -> Dict[str, Any]:
#     """
#     Dify同步入口。任何情况下都返回 {"urls_info": [], "urls_info_str": ""} 结构。
#     """
#     try:
#         # **关键修正 1: 健壮的输入处理**
#         # Dify 可能传入 None 或 ""，在这里将其转换为有效的整数
#         try:
#             web_count = int(web_results) if web_results not in [None, ""] else 3
#         except (ValueError, TypeError):
#             web_count = 3  # 如果传入无法转换的字符串，则使用默认值
#
#         try:
#             video_count = int(video_results) if video_results not in [None, ""] else 0
#         except (ValueError, TypeError):
#             video_count = 0  # 如果传入无法转换的字符串，则使用默认值
#
#         # 运行核心异步逻辑
#         return asyncio.run(main_async(
#             raw_input=raw_input,
#             provider_name=provider,
#             web_results_count=web_count,
#             video_results_count=video_count
#         ))
#
#     except Exception as e:
#         # **关键修正 2: 统一的错误输出结构**
#         # 捕获所有异常，并将其打包到符合输出变量定义的字典中
#         error_message = f"An error occurred in the node: {str(e)}"
#         # 创建一个包含错误信息的 payload，其结构与正常输出的 urls_info 一致
#         error_payload = [
#             {
#                 "query": "NODE_EXECUTION_ERROR",
#                 "web_results": [],
#                 "video_results": [],
#                 "errors": [error_message, traceback.format_exc()]  # 包含简短和详细的错误
#             }
#         ]
#
#         # 返回与成功时完全相同的 key
#         return {
#             "urls_info": error_payload,
#             "urls_info_str": json.dumps(error_payload, ensure_ascii=False, indent=2)
#         }
class MultiSourceSearcher:
    def __init__(self):
        # 常规Web搜索提供商池
        self.web_providers: Dict[str, SearchProvider] = {
            "searchapi_io": SearchApiIoProvider(),
            "jina": JinaSearchProvider(),
            "firecrawl": FirecrawlSearchProvider(),
            "tavily": TavilySearchProvider()
        }
        # 特殊任务提供商
        self.zhilian_provider = ZhiLianJobProvider()
        self.tianyan_provider = TianyanCheckProvider()

    async def web_search(self, queries: List[str], providers_to_use: List[str], client: httpx.AsyncClient,
                         web_count: int, video_count: int) -> List[Dict[str, Any]]:
        # ... [这里的轮询调度逻辑保持不变] ...
        async def search_and_tag(p_name: str, query: str, num: int, stype: str):
            provider = self.web_providers[p_name]
            filtered_query = _build_filtered_query(query, stype)
            print(f"  -> Provider: {p_name}, Type: {stype}, Final Query: \"{filtered_query}\"")
            try:
                data = await provider.search(filtered_query, client, num, stype);
                return {"query": query, "provider": p_name,
                        "type": stype, "data": data}
            except Exception as e:
                error_data = [provider._prefix_keys({"type": stype, "error": f"Task failed for '{query}': {e}"},
                                                    provider.prefix)];
                return {"query": query, "provider": p_name,
                        "type": stype, "data": error_data}

        provider_cycle = cycle(providers_to_use);
        tasks_by_query = {q: [] for q in queries}
        for query in queries:
            assigned_provider = next(provider_cycle)
            if web_count > 0: tasks_by_query[query].append(search_and_tag(assigned_provider, query, web_count, 'web'))
            if video_count > 0: tasks_by_query[query].append(
                search_and_tag(assigned_provider, query, video_count, 'video'))
        all_tasks = [task for task_list in tasks_by_query.values() for task in task_list];
        task_results = await asyncio.gather(*all_tasks)
        agg = {q: {"query": q, "web_results": [], "video_results": [], "errors": []} for q in queries}
        for res in task_results:
            query, p_name, stype, data = res["query"], res["provider"], res["type"], res["data"];
            provider_prefix = self.web_providers[p_name].prefix;
            clean_data = []
            for item in data:
                if item.get(f'{provider_prefix}_error'):
                    agg[query]["errors"].append(item[f'{provider_prefix}_error'])
                else:
                    clean_data.append(item)
            if stype == 'web':
                agg[query]["web_results"].extend(clean_data)
            elif stype == 'video':
                agg[query]["video_results"].extend(clean_data)
        return list(agg.values())


# --- 5. Dify 异步主函数 (总指挥) ---
async def main_async(raw_input: Any, provider_selection: Union[str, List[str]], web_results_count: int,
                     video_results_count: int) -> Dict[str, Any]:
    # 1. 解析所有潜在输入
    parsed_data = _intelligent_input_parser(raw_input)
    comprehensive_queries = parsed_data["comprehensive_queries"]
    career_query_data = parsed_data["career_query_data"]
    tianyan_enterprise_names = parsed_data["tianyan_enterprise_names"]

    # 2. 初始化结果容器
    comprehensive_results = []
    career_results = {}
    tianyan_results: List[str] = []

    # 3. 解析用户选择的 provider
    searcher = MultiSourceSearcher()
    all_web_provider_names = list(searcher.web_providers.keys())

    selected_providers = []
    if isinstance(provider_selection, str):
        selected_providers = [p.strip().lower() for p in provider_selection.split(',')]
    elif isinstance(provider_selection, list):
        selected_providers = [str(p).lower() for p in provider_selection]

    # 处理 "all" 关键字
    if "all" in selected_providers:
        # 将 "all" 替换为所有 web provider，并与其他特殊任务去重合并
        selected_providers.remove("all")
        selected_providers = list(set(selected_providers + all_web_provider_names))
    # 4. 任务分派与执行
    web_search_providers_to_use = [p for p in selected_providers if p in all_web_provider_names]
    is_zhilian_requested = "zhilian_job" in selected_providers
    is_tianyan_requested = "tianyan_check_enterprises" in selected_providers

    # 4.1 执行Web搜索（如果需要）
    if web_search_providers_to_use and comprehensive_queries:
        print(f"🌐 [Web Search] 使用 {web_search_providers_to_use} 搜索 {len(comprehensive_queries)} 个查询...")
        async with httpx.AsyncClient(http2=True, verify=False) as client:
            comprehensive_results = await searcher.web_search(
                queries=comprehensive_queries,
                providers_to_use=web_search_providers_to_use,
                client=client,
                web_count=web_results_count,
                video_count=video_results_count
            )
    else:
        print("🟡 [Web Search] 无需执行Web搜索。(查询为空或未选择任何有效的Web提供商)")

    # 4.2 执行ZhiLian数据提取（如果需要）
    if is_zhilian_requested:
        career_results = searcher.zhilian_provider.get_data(career_query_data)
    else:
        print("🟡 [ZhiLian] 未请求招聘数据提取。")

    # 4.3 执行Tianyan数据提取（如果需要）
    if is_tianyan_requested:
        tianyan_results = searcher.tianyan_provider.get_data(tianyan_enterprise_names)
    else:
        print("🟡 [Tianyan] 未请求企业数据提取。")

    # 5. 组装最终输出
    final_output = {
        "datas": {
            "comprehensive_data": comprehensive_results,
            "career_data": career_results,
            "tianyan_check_data": tianyan_results
        }
    }

    return {
        "datas": final_output["datas"],
        "datas_str": json.dumps(final_output, ensure_ascii=False, indent=2)
    }


# --- Dify 同步入口 ---
# 【调整】重构 main 函数以适应新的异步逻辑和更复杂的 provider 输入
def main(
        raw_input: Any,
        provider: Union[str, List[str]] = "tavily",
        web_results: Any = 3,
        video_results: Any = 0
) -> Dict[str, Any]:
    # 定义一个标准的空/错误返回结构
    error_datas_structure = {"comprehensive_data": [], "career_data": {}, "tianyan_check_data": []}

    def construct_error_payload(e, trace):
        error_message = f"An error occurred in the node: {str(e)}"
        error_datas_structure["comprehensive_data"] = [
            {"query": "NODE_EXECUTION_ERROR", "web_results": [], "video_results": [], "errors": [error_message, trace]}
        ]
        return {
            "datas": error_datas_structure,
            "datas_str": json.dumps({"datas": error_datas_structure}, ensure_ascii=False, indent=2)
        }

    try:
        # 1. 健壮地处理 provider 输入
        provider_selection = provider
        if isinstance(provider, str) and provider.strip().startswith('[') and provider.strip().endswith(']'):
            try:
                # 尝试将字符串形式的列表解析为真实的 Python 列表
                provider_selection = json.loads(provider)
            except json.JSONDecodeError:
                raise ValueError(f"Provider input '{provider}' looks like a list but is not valid JSON.")

        # 2. 健壮地处理数字输入
        try:
            web_count = int(web_results) if web_results not in [None, ""] else 3
        except (ValueError, TypeError):
            web_count = 3
        try:
            video_count = int(video_results) if video_results not in [None, ""] else 0
        except (ValueError, TypeError):
            video_count = 0

        # 3. 运行核心异步逻辑
        res = asyncio.run(main_async(
            raw_input=raw_input,
            provider_selection=provider_selection,
            web_results_count=web_count,
            video_results_count=video_count
        ))

        return _dify_debug_return(res, label='Success')
    except Exception as e:
        trace = traceback.format_exc()
        print(f"‼️ 节点执行时发生顶层错误: {e}\n{trace}")
        error_payload = construct_error_payload(e, trace)
        return _dify_debug_return(error_payload, label='Exception')


main({
    "component_id": "comp_008",
    "web_queries": {
        "career_query": {},
        "comprehensive_query": [
            "理想L8座椅加热不工作的原因有哪些？",
            "如何自行检查理想L8座椅加热是否正常？",
            "理想L8座椅加热系统常见故障及解决方法"
        ],
        "tianyan_check_enterprise": ""
    }
}, "[\"searchapi_io\",\"tianyan_check_enterprises\"]",
    web_results='10')

# async def main_async(raw_input: Any, provider_selection: Union[str, List[str]], web_results_count: int,
#                      video_results_count: int) -> Dict[str, Any]:
#     # 1. 解析输入
#     parsed_data = _intelligent_input_parser(raw_input)
#     comprehensive_queries = parsed_data["comprehensive_queries"]
#     career_query_data = parsed_data["career_query_data"]
#     tianyan_enterprise_name = parsed_data["tianyan_enterprise_name"]  # 【调整】获取新数据
#
#     # 2. 初始化结果容器
#     comprehensive_results = []
#     career_results = {}
#     tianyan_results = ""  # 【调整】初始化新结果容器
#
#     # 3. 解析和分派任务
#     searcher = MultiSourceSearcher()
#     all_web_provider_names = list(searcher.web_providers.keys())
#
#     selected_providers = []
#     if isinstance(provider_selection, str):
#         if provider_selection.lower() == "all":
#             selected_providers = all_web_provider_names
#         else:
#             selected_providers = [provider_selection.lower()]
#     elif isinstance(provider_selection, list):
#         selected_providers = [str(p).lower() for p in provider_selection]
#     # 【调整】任务分派逻辑
#     web_search_providers_to_use = [p for p in selected_providers if p in all_web_provider_names]
#     is_zhilian_requested = "zhilian_job" in selected_providers
#     is_tianyan_requested = "tianyan_check_enterprises" in selected_providers  # 【新增】
#     # 3.2 执行Web搜索
#     if web_search_providers_to_use and comprehensive_queries:
#         print(f"🌐 [Web Search] 使用 {web_search_providers_to_use} 搜索 {len(comprehensive_queries)} 个查询...")
#         async with httpx.AsyncClient() as client:
#             comprehensive_results = await searcher.web_search(queries=comprehensive_queries,
#                                                               providers_to_use=web_search_providers_to_use,
#                                                               client=client, web_count=web_results_count,
#                                                               video_count=video_results_count)
#     else:
#         print("🟡 [Web Search] 无需执行Web搜索。")
#     # 3.3 执行ZhiLian数据提取
#     if is_zhilian_requested:
#         career_results = searcher.zhilian_provider.get_data(career_query_data)
#
#     # 【新增】3.4 执行Tianyan数据提取
#     if is_tianyan_requested:
#         tianyan_results = searcher.tianyan_provider.get_data(tianyan_enterprise_name)
#     # 4. 【调整】组装最终输出
#     final_output = {
#         "datas": {
#             "comprehensive_data": comprehensive_results,
#             "career_data": career_results,
#             "tianyan_check_data": tianyan_results  # 【新增】
#         }
#     }
#     return {
#         "datas": final_output["datas"],  # 【调整】直接返回datas对象
#         "datas_str": json.dumps(final_output, ensure_ascii=False, indent=2)
#     }
#
#
# # --- Dify 同步入口 ---
# def main(raw_input: Any, provider: Union[str, List[str]] = "tavily", web_results: Any = 3, video_results: Any = 0) -> \
# Dict[str, Any]:
#     # 【调整】统一的错误输出结构
#     error_datas_structure = {"comprehensive_data": [], "career_data": {}, "tianyan_check_data": ""}
#     error_payload = {
#         "datas": error_datas_structure,
#         "datas_str": json.dumps({"datas": error_datas_structure}, ensure_ascii=False, indent=2)
#     }
#     try:
#         provider_selection = provider
#         if isinstance(provider, str) and provider.strip().startswith('[') and provider.strip().endswith(']'):
#             try:
#                 provider_selection = json.loads(provider)
#             except json.JSONDecodeError:
#                 raise ValueError(f"Provider input '{provider}' looks like a list but is not valid JSON.")
#
#         try:
#             web_count = int(web_results) if web_results not in [None, ""] else 3
#         except (ValueError, TypeError):
#             web_count = 3
#         try:
#             video_count = int(video_results) if video_results not in [None, ""] else 0
#         except (ValueError, TypeError):
#             video_count = 0
#
#         res = asyncio.run(
#             main_async(raw_input=raw_input, provider_selection=provider_selection, web_results_count=web_count,
#                        video_results_count=video_count))
#         return _dify_debug_return(res, label='Success')
#
#     except Exception as e:
#         error_message = f"An error occurred in the node: {str(e)}"
#         trace = traceback.format_exc()
#
#         # 【调整】将错误信息放入 comprehensive_data 中
#         error_datas_structure["comprehensive_data"] = [
#             {"query": "NODE_EXECUTION_ERROR", "web_results": [], "video_results": [], "errors": [error_message, trace]}
#         ]
#         error_payload["datas"] = error_datas_structure
#         error_payload["datas_str"] = json.dumps({"datas": error_datas_structure}, ensure_ascii=False, indent=2)
#         print(f"‼️ 节点执行时发生顶层错误: {e}\n{trace}")
#         return _dify_debug_return(error_payload, label='Exception')

# class MultiSourceSearcher:
#     def __init__(self):
#         self.providers: Dict[str, SearchProvider] = {"searchapi_io": SearchApiIoProvider(),
#                                                      "jina": JinaSearchProvider(),
#                                                      "firecrawl": FirecrawlSearchProvider(),
#                                                      "tavily": TavilySearchProvider()}
#
#     async def _search_with_providers(self, queries: List[str], providers_to_use: List[str], client: httpx.AsyncClient,
#                                      web_count: int, video_count: int) -> List[Dict[str, Any]]:
#         async def search_and_tag(p_name: str, query: str, num: int, stype: str):
#             provider = self.providers[p_name]
#             try:
#                 data = await provider.search(query, client, num, stype)
#                 return {"query": query, "provider": p_name, "type": stype, "data": data}
#             except Exception as e:
#                 error_data = [
#                     provider._prefix_keys({"type": stype, "error": f"Task execution failed for '{query}': {e}"},
#                                           provider.prefix)]
#                 return {"query": query, "provider": p_name, "type": stype, "data": error_data}
#
#         provider_cycle = cycle(providers_to_use)
#         tasks_by_query = {q: [] for q in queries}
#         for query in queries:
#             assigned_provider = next(provider_cycle)
#             if web_count > 0:
#                 tasks_by_query[query].append(search_and_tag(assigned_provider, query, web_count, 'web'))
#             if video_count > 0 and assigned_provider == "searchapi_io":  # Only searchapi supports video
#                 tasks_by_query[query].append(search_and_tag(assigned_provider, query, video_count, 'video'))
#         all_tasks = [task for task_list in tasks_by_query.values() for task in task_list]
#         task_results = await asyncio.gather(*all_tasks)
#         # 聚合结果
#         agg = {q: {"query": q, "web_results": [], "video_results": [], "errors": []} for q in queries}
#         for res in task_results:
#             query, p_name, stype, data = res["query"], res["provider"], res["type"], res["data"]
#             provider_prefix = self.providers[p_name].prefix
#
#             clean_data = []
#             for item in data:
#                 if item.get(f'{provider_prefix}_error'):
#                     agg[query]["errors"].append(item[f'{provider_prefix}_error'])
#                 else:
#                     clean_data.append(item)
#
#             if stype == 'web':
#                 agg[query]["web_results"].extend(clean_data)
#             elif stype == 'video':
#                 agg[query]["video_results"].extend(clean_data)
#
#         return list(agg.values())
#
#     async def search(self, queries: List[str], provider_selection: Union[str, List[str]], web_count: int,
#                      video_count: int) -> List[Dict[str, Any]]:
#         providers_to_use = []
#         if isinstance(provider_selection, str):
#             if provider_selection.lower() == "all":
#                 providers_to_use = list(self.providers.keys())
#             else:
#                 if provider_selection.lower() not in self.providers: raise ValueError(
#                     f"Provider '{provider_selection}' not found.")
#                 providers_to_use = [provider_selection.lower()]
#         elif isinstance(provider_selection, list):
#             providers_to_use = [p.lower() for p in provider_selection if p.lower() in self.providers]
#             if not providers_to_use: raise ValueError("None of the specified providers are valid.")
#
#         if not providers_to_use: raise ValueError("No valid providers selected for search.")
#         async with httpx.AsyncClient() as client:
#             return await self._search_with_providers(queries, providers_to_use, client, web_count, video_count)
#
#
# # --- Dify 异步主函数 ---
# async def main_async(raw_input: Any, provider_selection: Union[str, List[str]], web_results_count: int,
#                      video_results_count: int) -> Dict[str, Any]:
#     parsed_input = _intelligent_input_parser(raw_input)
#     queries = parsed_input["queries"]
#     searcher = MultiSourceSearcher()
#     urls_info = await searcher.search(queries, provider_selection=provider_selection, web_count=web_results_count,
#                                       video_count=video_results_count)
#     return {"urls_info": urls_info, "urls_info_str": json.dumps(urls_info, ensure_ascii=False, indent=2)}
#
#
# # --- Dify 同步入口 ---
# def main(raw_input: Any, provider: Union[str, List[str]] = "tavily", web_results: Any = 3, video_results: Any = 0) -> \
#         Dict[str, Any]:
#     try:
#         provider_selection = provider
#         if isinstance(provider, str) and provider.strip().startswith('[') and provider.strip().endswith(']'):
#             try:
#                 provider_selection = json.loads(provider)
#             except json.JSONDecodeError:
#                 raise ValueError(f"Provider input '{provider}' looks like a list but is not valid JSON.")
#         try:
#             web_count = int(web_results) if web_results not in [None, ""] else 3
#         except (ValueError, TypeError):
#             web_count = 3
#         try:
#             video_count = int(video_results) if video_results not in [None, ""] else 0
#         except (ValueError, TypeError):
#             video_count = 0
#         res = asyncio.run(
#             main_async(raw_input=raw_input, provider_selection=provider_selection, web_results_count=web_count,
#                        video_results_count=video_count))
#         return _dify_debug_return(res)
#     except Exception as e:
#         error_payload = [{"query": "NODE_EXECUTION_ERROR", "web_results": [], "video_results": [],
#                           "errors": [f"An error occurred in the node: {str(e)}", traceback.format_exc()]}]
#         err = {"urls_info": error_payload, "urls_info_str": json.dumps(error_payload, ensure_ascii=False, indent=2)}
#         return _dify_debug_return(err, label='exc')
