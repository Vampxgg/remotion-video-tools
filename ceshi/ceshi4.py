# Dify 依赖管理: 请确保已添加 httpx, json-repair, trafilatura, pypdf2, beautifulsoup4, lxml
import asyncio
import httpx
import re
import os
import json
import time
import traceback
import warnings
from typing import Any, Dict, List, Literal, Optional
from abc import ABC, abstractmethod
from io import BytesIO
from urllib.parse import urljoin

# --- 核心依赖 ---
# trafilatura 用于从HTML提取主要内容
import trafilatura
from trafilatura.settings import use_config

# PyPDF2 用于解析PDF
import pdfplumber
import fitz
# BeautifulSoup 用于辅助解析HTML（例如提取视频）
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# 忽略 XMLParsedAsHTMLWarning 警告
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# ==============================================================================
# ====================== DIFY 本地调试辅助模块 =========================
# ==============================================================================
import pprint

# --- 本地调试开关 ---
# 在你的 IDE 中进行测试时，将此值设为 True。
# 当你准备将代码复制到 Dify 平台时，请将其改回 False，或直接删除此调试模块。
IS_LOCAL_DEBUG = False


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


def _parse_input_data(raw_input: Any) -> Dict[str, Any]:
    """
    健壮地解析上一个节点的输出，能同时处理带 "datas" 包装和不带包装的两种结构。
    并分离出网页搜索URL、视频URL、招聘查询参数和企业名称，同时保留元数据。
    """
    print(
        f"============== 步骤 1: 接收到原始输入 ==============\nTYPE: {type(raw_input)}\nVALUE: {raw_input}\n=======================================================")

    data_list = []

    # 1. 统一转换为列表处理
    if isinstance(raw_input, list):
        data_list = raw_input
    elif isinstance(raw_input, str):
        if not raw_input.strip(): return {"items": []}
        try:
            parsed = json.loads(raw_input)
            if isinstance(parsed, list):
                data_list = parsed
            elif isinstance(parsed, dict):
                # 检查是否是被 datas 包装的列表
                if "datas" in parsed and isinstance(parsed["datas"], list):
                    data_list = parsed["datas"]
                # 检查是否是被 datas 包装的字典 (旧格式)
                elif "datas" in parsed and isinstance(parsed["datas"], dict):
                    data_list = [parsed["datas"]]
                else:
                    data_list = [parsed]
            else:
                data_list = [parsed]
        except json.JSONDecodeError as e:
            # 尝试作为单个对象解析
            try:
                # 某些情况下可能是单个对象的JSON字符串
                data_list = [json.loads(raw_input)]
            except:
                raise ValueError(f"无法将输入字符串解析为JSON: {e}")
    elif isinstance(raw_input, dict):
        # 检查是否是被 datas 包装的列表
        if "datas" in raw_input and isinstance(raw_input["datas"], list):
            data_list = raw_input["datas"]
        # 检查是否是被 datas 包装的字典 (旧格式)
        elif "datas" in raw_input and isinstance(raw_input["datas"], dict):
            data_list = [raw_input["datas"]]
        else:
            data_list = [raw_input]
    else:
        raise TypeError(f"期望的输入类型是 str, list 或 dict, 但收到了 {type(raw_input).__name__}")

    parsed_items = []

    for data_item in data_list:
        # 此时 data_item 应该是单个的数据对象
        if not isinstance(data_item, dict): continue

        # 再次检查内部是否有 datas 包装 (针对某些奇怪的嵌套结构)
        if "datas" in data_item and isinstance(data_item["datas"], dict):
            datas_obj = data_item["datas"]
        else:
            datas_obj = data_item

        comprehensive_data = datas_obj.get("comprehensive_data", [])
        career_data = datas_obj.get("career_data", {})
        tianyan_data = datas_obj.get("tianyan_check_data", [])

        web_url_info_list = []
        video_url_info_list = []

        if isinstance(comprehensive_data, list):
            for query_result in comprehensive_data:
                if not isinstance(query_result, dict): continue
                query_text = query_result.get("query", "")

                for key, result_list in query_result.items():
                    if not key.endswith("_results") or not isinstance(result_list, list):
                        continue

                    for res in result_list:
                        if not isinstance(res, dict): continue

                        # provider 提取逻辑
                        provider = next(
                            (k[:-4] for k in res if
                             k.endswith('_url') and '_embed_' not in k and '_thumbnail_' not in k),
                            None)
                        if not provider:
                            provider = next((k.split('_')[0] for k in res if '_url' in k), None)
                            if not provider: continue

                        result_type = res.get(f"{provider}_type")
                        if result_type == 'video':
                            info = {
                                "url": res.get(f"{provider}_url"), "title": res.get(f"{provider}_title", "Untitled"),
                                "source": res.get(f"{provider}_source"), "snippet": res.get(f"{provider}_snippet"),
                                "provider": provider, "query": query_text,
                                "video_id": res.get(f"{provider}_video_id"),
                                "embed_url": res.get(f"{provider}_embed_url"),
                                "thumbnail_url": res.get(f"{provider}_thumbnail_url"),
                            }
                            if info["url"]: video_url_info_list.append(info)
                        else:
                            info = {
                                "url": res.get(f"{provider}_url"), "title": res.get(f"{provider}_title", "Untitled"),
                                "source": res.get(f"{provider}_source"), "snippet": res.get(f"{provider}_snippet"),
                                "provider": provider, "query": query_text,
                            }
                            if info["url"]: web_url_info_list.append(info)

        career_payload = career_data if isinstance(career_data, dict) else {}
        enterprise_names: List[str] = []
        if isinstance(tianyan_data, str) and tianyan_data.strip():
            enterprise_names.append(tianyan_data.strip())
        elif isinstance(tianyan_data, list):
            enterprise_names = [str(name).strip() for name in tianyan_data if
                                isinstance(name, str) and str(name).strip()]

        parsed_items.append({
            "web_url_info_list": web_url_info_list,
            "video_url_info_list": video_url_info_list,
            "career_payload": career_payload,
            "enterprise_names": enterprise_names
        })

    return {"items": parsed_items}


# --- 1. 输入解析模块 ---
# 【已修复】替换这个函数
# def _parse_input_data(raw_input: Any) -> Dict[str, Any]:
#     """
#     健壮地解析上一个节点的输出，能同时处理带 "datas" 包装和不带包装的两种结构。
#     """
#     print(
#         f"============== 步骤 1: 接收到原始输入 ==============\nTYPE: {type(raw_input)}\nVALUE: {raw_input}\n=======================================================")
#     if isinstance(raw_input, str):
#         if not raw_input.strip(): return {"url_list": [], "career_payload": {}, "enterprise_name": ""}
#         try:
#             data = json.loads(raw_input)
#         except json.JSONDecodeError as e:
#             raise ValueError(f"无法将输入字符串解析为JSON: {e}")
#     elif isinstance(raw_input, dict):
#         data = raw_input
#     else:
#         raise TypeError(f"期望的输入类型是 str 或 dict, 但收到了 {type(raw_input).__name__}")
#
#     # --- 核心修复逻辑 ---
#     # 检查顶层是否有 "datas" 键，如果没有，就认为当前整个对象就是我们要的数据体。
#     if "datas" in data and isinstance(data["datas"], dict):
#         print("  [解析器] 检测到 'datas' 包装层，将使用其内部数据。")
#         datas_obj = data["datas"]
#     else:
#         print("  [解析器] 未检测到 'datas' 包装层，将直接使用顶层数据。")
#         datas_obj = data
#     # --- 修复结束 ---
#
#     if not isinstance(datas_obj, dict): datas_obj = {}
#
#     comprehensive_data = datas_obj.get("comprehensive_data", [])
#     career_data = datas_obj.get("career_data", {})
#     tianyan_data = datas_obj.get("tianyan_check_data", "")
#
#     url_list = []
#     if isinstance(comprehensive_data, list):
#         for query_result in comprehensive_data:
#             if not isinstance(query_result, dict): continue
#             for res_list_key in ["web_results", "video_results"]:
#                 for res in query_result.get(res_list_key, []):
#                     if not isinstance(res, dict): continue
#                     url, title, provider = None, None, None
#                     for key, value in res.items():
#                         if key.endswith("_url"):
#                             url, provider = value, key.split('_url')[0]
#                             title = res.get(f"{provider}_title", "Untitled")
#                             break
#                     if url and provider:
#                         url_list.append({"url": url, "title": title, "provider": provider})
#     career_payload = career_data if isinstance(career_data, dict) else {}
#     enterprise_name = tianyan_data if isinstance(tianyan_data, str) else ""
#     parsed_result = {"url_list": url_list, "career_payload": career_payload, "enterprise_name": enterprise_name.strip()}
#
#     print(
#         f"============== 步骤 2: 输入解析完毕 ==============\nURL 数量: {len(url_list)}\n招聘负载: {career_payload}\n企业名称: '{enterprise_name.strip()}'\n=======================================================")
#
#     return parsed_result


# def _parse_input_data(raw_input: Any) -> Dict[str, Any]:
#     """
#     健壮地解析上一个节点的输出，分离出web搜索URL和招聘查询参数。
#     """
#     if isinstance(raw_input, str):
#         if not raw_input.strip(): return {"url_list": [], "career_payload": {}}
#         try:
#             data = json.loads(raw_input)
#         except json.JSONDecodeError as e:
#             raise ValueError(f"无法将输入字符串解析为JSON: {e}")
#     elif isinstance(raw_input, dict):
#         data = raw_input
#     else:
#         raise TypeError(f"期望的输入类型是 str 或 dict, 但收到了 {type(raw_input).__name__}")
#     # 安全地深入到 'datas' 结构
#     datas_obj = data.get("datas", {})
#     if not isinstance(datas_obj, dict): datas_obj = {}
#     comprehensive_data = datas_obj.get("comprehensive_data", [])
#     career_data = datas_obj.get("career_data", {})
#     tianyan_data = datas_obj.get("tianyan_check_data", "")
#     # 1. 提取URL列表
#     url_list = []
#     if isinstance(comprehensive_data, list):
#         for query_result in comprehensive_data:
#             if not isinstance(query_result, dict): continue
#             for res_list_key in ["web_results", "video_results"]:
#                 for res in query_result.get(res_list_key, []):
#                     if not isinstance(res, dict): continue
#                     url, title, provider = None, None, None
#                     for key, value in res.items():
#                         if key.endswith("_url"):
#                             url = value
#                             provider = key.split('_url')[0]
#                             title = res.get(f"{provider}_title", "Untitled")
#                             break
#                     if url and provider:
#                         url_list.append({"url": url, "title": title, "provider": provider})
#     # 2. 提取职业查询负载
#     career_payload = career_data if isinstance(career_data, dict) else {}

#     # 3. 提取企业名称
#     enterprise_name = tianyan_data if isinstance(tianyan_data, str) else ""
#     return {"url_list": url_list, "career_payload": career_payload, "enterprise_name": enterprise_name.strip()}

# --- 2. 抽象与实现分离：内容抓取器 ---
class ContentScraper(ABC):
    """内容抓取器的抽象基类。"""

    # 【修改】方法签名，接收一个包含所有元数据的字典
    @abstractmethod
    async def scrape(self, item_info: Dict[str, Any], client: httpx.AsyncClient) -> Dict[str, Any]:
        """
        抓取单个URL的内容，并返回合并了原始信息的结果。
        """
        pass


# --- 2.1 SearchAPI.io 的手动抓取与清洗实现 ---
class SearchApiScraper(ContentScraper):
    def __init__(self):
        self.trafilatura_config = use_config()
        self.trafilatura_config.set("DEFAULT", "EXTRACTION_TIMEOUT", "10")

        # 编译常用的正则表达式以提高性能
        self.NOISY_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
            r'^\s*$', r'^[\-=*#_]{3,}$', r'.*\.(html|shtml|htm|php)\s*$',
            r'.{0,50}(搜狐|网易|腾讯|新浪|登录|注册|版权所有|版权声明).{0,50}$',
            r'\[\d+\]|\[下一页\]|\[上一页\]', r'\[(编辑|查看历史|讨论|阅读|来源|原标题)\]',
            r'^\*+\s*\[.*?\]\(.*?\)',
            r'^\s*(分享到|扫描二维码|返回搜狐|查看更多|责任编辑|记者|通讯员)',
            r'^\s*([京公网安备京网文京ICP备]|互联网新闻信息服务许可证|信息网络传播视听节目许可证)',
        ]]
        self.IMG_PATTERN = re.compile(r'(!\[(.*?)\]\((.*?)\))')
        self.LINK_PATTERN = re.compile(r'\[.*?\]\(.*?\)')
        self.EDITOR_PATTERN = re.compile(r'(\(|\[)\s*责任编辑：.*?\s*(\)|\])')

    # --- 2.1.1 内容提取工具 (来自您的代码) ---
    def _extract_pdf_text(self, binary_content: bytes) -> str:
        """
        使用 PyMuPDF (fitz) 从 PDF 的二进制内容中快速提取文本。
        """
        text_parts = []
        try:
            # 直接从内存中的字节流打开 PDF
            with fitz.open(stream=binary_content, filetype="pdf") as doc:
                # 限制处理的页数，避免处理超大文件
                num_pages_to_process = min(len(doc), self.PDF_MAX_PAGES_TO_PROCESS)
                if len(doc) > self.PDF_MAX_PAGES_TO_PROCESS:
                    print(f"  📄 PDF 页数过多 ({len(doc)} pages), 只处理前 {self.PDF_MAX_PAGES_TO_PROCESS} 页。")
                for i in range(num_pages_to_process):
                    page = doc.load_page(i)
                    page_text = page.get_text("text", sort=True)  # sort=True 尝试保持阅读顺序
                    if page_text:
                        text_parts.append(page_text)

            return "\n\n".join(text_parts).strip()
        except Exception as e:
            print(f"⚠️ PyMuPDF (fitz) 解析失败: {e}")
            return ""

    def _parse_videos_from_html(self, html: str, base_url: str) -> List[str]:
        try:
            soup = BeautifulSoup(html, "lxml")
            videos = []
            for video in soup.find_all("video"):
                src = video.get("src")
                if src: videos.append(urljoin(base_url, src))
                for source in video.find_all("source"):
                    src = source.get("src")
                    if src: videos.append(urljoin(base_url, src))
            for iframe in soup.find_all("iframe"):
                src = iframe.get("src")
                if src and any(k in src for k in ["youtube", "vimeo", "embed", ".mp4"]):
                    videos.append(urljoin(base_url, src))
            return list(dict.fromkeys(videos))  # 去重并保持顺序
        except Exception as e:
            print(f"⚠️ 视频解析失败: {e}")
            return []

    # --- 2.1.2 内容清洗工具 (来自您的代码，已优化和异步化) ---
    async def _is_valid_image_url_async(self, url: str, client: httpx.AsyncClient) -> bool:
        if not url or not url.startswith(('http://', 'https://')): return False
        try:
            resp = await client.head(url, timeout=5, follow_redirects=True)
            content_type = resp.headers.get('content-type', '').lower()
            return resp.is_success and 'image' in content_type
        except httpx.RequestError:
            return False

    async def _remove_invalid_images_async(self, md: str, client: httpx.AsyncClient) -> str:
        # 【性能优化】禁用图片验证以避免请求风暴 (111 URLs * 25 Images = ~2700 Requests)
        # 直接返回原始内容，不做图片有效性检查
        return md

    def _is_noisy_line(self, line: str) -> bool:
        stripped = line.strip()
        for pat in self.NOISY_PATTERNS:
            if pat.search(stripped): return True
        links = self.LINK_PATTERN.findall(stripped)
        if len(links) > 2 and len(stripped) / (len(links) + 1) < 30: return True
        return False

    async def _clean_content_async(self, text: str, client: httpx.AsyncClient) -> str:
        if not text: return ""
        # 【性能优化】不再验证图片有效性，避免大量 HTTP 请求
        # text = await self._remove_invalid_images_async(text, client)

        lines = text.splitlines()
        cleaned_lines = []
        for line in lines:
            if not self._is_noisy_line(line):
                line = self.EDITOR_PATTERN.sub('', line).strip()
                if line: cleaned_lines.append(line)

        # 去除连续空行
        out = []
        for i, line in enumerate(cleaned_lines):
            if i > 0 and not line.strip() and not cleaned_lines[i - 1].strip():
                continue
            out.append(line)

        return "\n".join(out).strip()

    # --- 2.1.3 主抓取函数 (来自您的代码，封装为scrape方法) ---
    async def scrape(self, item_info: Dict[str, Any], client: httpx.AsyncClient) -> dict:
        url = item_info.get("url")
        print(f"🕸️ [SearchAPI Scraper] 开始处理: {url}")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}

        try:
            # 增加对 PDF 的特殊预处理
            is_pdf = False
            if url.lower().endswith(".pdf"):
                is_pdf = True
            else:
                # 【优化】对于不确定类型，先发 HEAD 请求探测
                async with client.stream("HEAD", url, headers=headers,
                                         follow_redirects=True) as head_response:
                    content_type = head_response.headers.get('content-type', '').lower()
                    if 'pdf' in content_type:
                        is_pdf = True
                        # 【优化】检查文件大小
                        content_length = int(head_response.headers.get('content-length', 0))
                        if content_length > self.PDF_MAX_SIZE_MB * 1024 * 1024:
                            raise ValueError(
                                f"PDF 文件过大 ({content_length / 1024 / 1024:.2f}MB > {self.PDF_MAX_SIZE_MB}MB)，跳过处理。")
            # 根据类型执行不同逻辑
            raw_content, final_url = "", url
            if is_pdf:
                print(f"  📄 [SearchAPI Scraper] 检测到 PDF，开始下载: {url}")
                # 【优化】为PDF下载和处理设置独立超时
                async with asyncio.timeout(30):  # 缩短超时时间到30秒
                    response = await client.get(url, timeout=None, headers=headers, follow_redirects=True)  # 使用外部超时
                    response.raise_for_status()
                    final_url = str(response.url)
                    pdf_bytes = await response.aread()

                print(f"   rocket [SearchAPI Scraper] 正在使用 PyMuPDF 快速解析...")
                raw_content = await asyncio.to_thread(self._extract_pdf_text, pdf_bytes)
            else:
                print(f"  📑 [SearchAPI Scraper] 检测到 HTML: {url}")
                response = await client.get(url, timeout=20, headers=headers, follow_redirects=True)
                response.raise_for_status()
                final_url = str(response.url)
                html_content = response.text
                raw_content = await asyncio.to_thread(
                    trafilatura.extract, html_content, config=self.trafilatura_config,
                    output_format='markdown', include_images=True, favor_recall=True)

                # HTML的视频解析和清洗
                if raw_content:
                    print(f"  🧹 [SearchAPI Scraper] 正在清洗HTML内容: {final_url}")
                    cleaned_content = await self._clean_content_async(raw_content, client)
                    videos = self._parse_videos_from_html(html_content, final_url)
                    if videos:
                        video_section = "\n\n## 参考视频:\n" + "\n".join(f"- {vid}" for vid in videos)
                        cleaned_content += video_section
                    raw_content = cleaned_content

            if not raw_content: raise ValueError("内容提取返回为空。")
            print(f"✅ [SearchAPI Scraper] 成功: {url}")
            return {**item_info, "url": final_url, "content": raw_content, "status": "success"}
        except Exception as e:
            error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
            print(f"⚠️ [SearchAPI Scraper] {error_msg}")
            return {**item_info, "content": "", "status": "failed", "error_message": str(e)}


# --- 2.2 FirecrawlScraper ---
class FirecrawlScraper(ContentScraper):
    def __init__(self):
        self.api_key = os.environ.get("FIRECRAWL_API_KEY", "fc-a36b7d2fb273485680d0fe6abd686935")
        if not self.api_key: raise ValueError("未提供 Firecrawl API Key。")
        self.base_url = "https://api.firecrawl.dev/v2/scrape"
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def scrape(self, item_info: Dict[str, Any], client: httpx.AsyncClient) -> dict:
        url = item_info.get("url")
        print(f"🔥 [Firecrawl Scraper] 开始处理: {url}")
        try:
            # 【修改】根据文档，移除 pageOptions，将选项置于顶层
            payload = {
                "url": url,
                "onlyMainContent": True,
                "removeBase64Images": True,
                "blockAds": True
            }
            resp = await client.post(self.base_url, headers=self.headers, json=payload, timeout=45)

            if not resp.is_success:
                try:
                    error_details = resp.json()
                    raise httpx.HTTPStatusError(f"API返回错误: {error_details.get('error', str(error_details))}",
                                                request=resp.request, response=resp)
                except json.JSONDecodeError:
                    resp.raise_for_status()

            data_wrapper = resp.json()

            # 【修改】根据文档，检查顶层 success 键和 data 字段
            if not data_wrapper.get("success"):
                raise ValueError(f"API返回失败状态: {data_wrapper.get('error', '未知错误')}")

            data = data_wrapper.get("data")
            if not data:
                raise ValueError("API返回的 'data' 字段为空。")

            content = data.get("markdown")
            if content is None:
                raise ValueError("API未返回 'markdown' 字段。")

            final_url = data.get("metadata", {}).get("sourceURL", url)

            print(f"✅ [Firecrawl Scraper] 成功: {url}")
            return {**item_info, "url": final_url, "content": content, "status": "success"}

        except Exception as e:
            error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
            print(f"⚠️ [Firecrawl Scraper] {error_msg}")
            return {**item_info, "content": "", "status": "failed", "error_message": str(e)}


# --- 2.3 JinaScraper ---
class JinaScraper(ContentScraper):
    def __init__(self):
        self.api_key = os.environ.get("JINA_API_KEY",
                                      "jina_b4348ffc39ca47bfbe753b95f59428c7i6ifkOFXRPdF3dRa5Rwb6T8FvrLH")
        if not self.api_key: raise ValueError("未提供 Jina API Key。")
        self.base_url = "https://r.jina.ai/"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Return-Format": "markdown"  # 关键：直接获取Markdown
        }

    async def scrape(self, item_info: Dict[str, Any], client: httpx.AsyncClient) -> dict:
        url = item_info.get("url")
        print(f"🌀 [Jina Scraper] 开始处理: {url}")
        try:
            # Jina的Reader API 对GET请求更友好，直接拼接URL
            target_url = f"{self.base_url}{url}"
            resp = await client.get(target_url, headers=self.headers, timeout=45)
            resp.raise_for_status()

            # 【修改】根据文档，Jina 可能直接返回 Markdown 文本，也可能返回 JSON
            content_type = resp.headers.get("content-type", "").lower()
            if "application/json" in content_type:
                data_wrapper = resp.json()
                if data_wrapper.get("code") == 200 and "data" in data_wrapper:
                    data = data_wrapper["data"]
                    content = data.get("content")
                    final_url = data.get("url", url)
                    if content is None: raise ValueError("API JSON响应中缺少 'content' 字段。")
                else:
                    raise ValueError(f"API JSON响应错误: {data_wrapper}")
            else:
                # 假设直接返回Markdown文本
                content = resp.text
                final_url = url

            if not content.strip(): raise ValueError("API 返回内容为空。")

            print(f"✅ [Jina Scraper] 成功: {url}")
            return {**item_info, "url": final_url, "content": content, "status": "success"}

        except Exception as e:
            error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
            print(f"⚠️ [Jina Scraper] {error_msg}")
            return {**item_info, "content": "", "status": "failed", "error_message": str(e)}


# --- 2.4 TavilyScraper ---
class TavilyScraper(ContentScraper):
    def __init__(self):
        self.api_key = os.environ.get("TAVILY_API_KEY", "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")
        if not self.api_key: raise ValueError("未提供 Tavily API Key。")
        self.base_url = "https://api.tavily.com/extract"
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def scrape(self, item_info: Dict[str, Any], client: httpx.AsyncClient) -> dict:
        url = item_info.get("url")
        print(f"🤖 [Tavily Scraper] 开始处理: {url}")
        try:
            # 【修改】根据文档，urls字段应该是列表，且使用 format: markdown
            payload = {"urls": [url], "format": "markdown"}
            resp = await client.post(self.base_url, json=payload, headers=self.headers, timeout=45)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("results") or not isinstance(data["results"], list):
                failed_info = data.get("failed_results", [])
                raise ValueError(f"API调用失败: {failed_info}")

            result = data["results"][0]
            # 【修改】根据文档，内容字段是 raw_content
            content = result.get("raw_content")
            if content is None: raise ValueError("API未返回raw_content内容。")

            final_url = result.get("url", url)

            print(f"✅ [Tavily Scraper] 成功: {url}")
            return {**item_info, "url": final_url, "content": content, "status": "success"}

        except Exception as e:
            error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
            print(f"⚠️ [Tavily Scraper] {error_msg}")
            return {**item_info, "content": "", "status": "failed", "error_message": str(e)}


# --- 2.5 ZhiLianJobScraper ---
class ZhiLianJobScraper:
    def __init__(self):
        self.api_url = "http://119.45.167.133:12906/api/scrape/zhilian"
        self.headers = {'accept': 'application/json', 'Content-Type': 'application/json'}

    async def scrape_jobs(self, payload: Dict[str, Any], client: httpx.AsyncClient) -> Dict[str, Any]:
        print(f"💼 [ZhiLian Scraper] 开始使用负载调用API: {json.dumps(payload, ensure_ascii=False)}")
        if not payload or not payload.get("keywords") or not payload.get("provinces"):
            msg = "负载无效，缺少 'keywords' 或 'provinces'。"
            print(f"⚠️ [ZhiLian Scraper] {msg}")
            return {"status": "skipped", "data": [], "message": msg}
        try:
            # 确保 page_size 是整数
            if 'page_size' in payload: payload['page_size'] = int(payload['page_size'])

            resp = await client.post(self.api_url, headers=self.headers, json=payload, timeout=60)
            resp.raise_for_status()
            response_data = resp.json()
            if response_data.get("code") == 200:
                print(f"✅ [ZhiLian Scraper] 成功: {response_data.get('message')}")
                return {"status": "success", "data": response_data.get("data", []),
                        "message": response_data.get("message")}
            else:
                msg = f"API返回错误码 {response_data.get('code')}: {response_data.get('message')}"
                print(f"API returned non-200 code: {msg}")
                return {"status": "failed", "data": [], "message": msg}
        except Exception as e:
            error_msg = f"API请求失败: {type(e).__name__} - {e}"
            print(f"⚠️ [ZhiLian Scraper] {error_msg}")
            return {"status": "failed", "data": [], "message": error_msg}


class TianyanEnterpriseScraper:
    def __init__(self):
        self.api_url = "http://open.api.tianyancha.com/services/open/ic/baseinfo/normal"
        # 从环境变量或直接硬编码获取Token
        self.token = os.environ.get("TIANYANCHA_TOKEN", "4d882100-ed23-4c22-a83b-c77af2e4be42")
        self.headers = {'Authorization': self.token}

    async def scrape_enterprise(self, name: str, client: httpx.AsyncClient) -> Dict[str, Any]:
        print(f"🏢 [Tianyan Scraper] 开始查询企业: {name}")
        base_return = {"query_name": name}
        if not name:
            msg = "企业名称为空，跳过查询。"
            print(f"🟡 [Tianyan Scraper] {msg}")
            return {**base_return, "status": "skipped", "data": None, "message": msg}
        try:
            params = {"keyword": name}
            resp = await client.get(self.api_url, headers=self.headers, params=params, timeout=30)
            resp.raise_for_status()
            response_data = resp.json()
            if response_data.get("error_code") == 0:
                print(f"✅ [Tianyan Scraper] 成功查询到: {name}")
                return {**base_return, "status": "success", "data": response_data.get("result"),
                        "message": response_data.get("reason")}
            else:
                msg = f"API返回错误码 {response_data.get('error_code')}: {response_data.get('reason')}"
                print(f"⚠️ [Tianyan Scraper] {msg}")
                return {**base_return, "status": "failed", "data": None, "message": msg}
        except Exception as e:
            error_msg = f"API请求失败: {type(e).__name__} - {e}"
            print(f"⚠️ [Tianyan Scraper] {error_msg}")
            return {**base_return, "status": "failed", "data": None, "message": error_msg}


class DataOrchestrator:
    def __init__(self):
        self.content_scrapers: Dict[str, ContentScraper] = {
            "searchapi": SearchApiScraper(), "firecrawl": FirecrawlScraper(),
            "jina": JinaScraper(), "tavily": TavilyScraper(),
        }
        self.job_scraper = ZhiLianJobScraper()
        self.enterprise_scraper = TianyanEnterpriseScraper()

    # 【调整】整个 process_all 方法被重构，以实现条件化任务调度。
    async def process_all(
            self,
            web_url_info_list: List[Dict[str, Any]],
            career_payload: Dict,
            enterprise_names: List[str],  # 接收列表
            client: httpx.AsyncClient  # 【性能优化】接收全局共享的 client
    ) -> Dict[str, Any]:
        """
        根据有效的输入，条件化地创建并并发执行所有抓取任务。
        """
        final_results = {
            "content_results": [],
            "job_result": None,
            "enterprise_results": [],  # 默认返回空列表
        }

        # 【性能优化】不再在每次 process_all 内部创建 client
        # ssl_context = httpx.create_ssl_context(verify=False)
        # async with httpx.AsyncClient...

        # 直接使用传入的 client
        content_tasks = []
        if web_url_info_list:
            print(f"  [Orchestrator] 准备 {len(web_url_info_list)}个网页抓取任务。")
            for item in web_url_info_list:
                scraper = self.content_scrapers.get(item.get("provider")) or self.content_scrapers["searchapi"]
                content_tasks.append(scraper.scrape(item, client))
        job_tasks = []
        if career_payload and career_payload.get("keywords") and career_payload.get("provinces"):
            print("  [Orchestrator] 准备招聘信息抓取任务。")
            job_tasks.append(self.job_scraper.scrape_jobs(career_payload, client))
        else:
            print("  [Orchestrator] 招聘信息负载无效，跳过任务。")

        # 【调整】为列表中的每个企业名称创建查询任务
        enterprise_tasks = []
        if enterprise_names:
            print(f"  [Orchestrator] 准备 {len(enterprise_names)}个企业信息查询任务。")
            for name in enterprise_names:
                enterprise_tasks.append(self.enterprise_scraper.scrape_enterprise(name, client))
        else:
            print("  [Orchestrator] 企业名称列表为空，跳过任务。")

        tasks_to_run = content_tasks + job_tasks + enterprise_tasks
        if not tasks_to_run:
            print("  [Orchestrator] 没有可执行的任务。")
            return final_results

            # 【性能优化】使用 asyncio.gather 并发执行时，如果任务数量巨大，可能会导致瞬间内存飙升或 CPU 占用过高。
        # 虽然外层有 Semaphore 限制了 item 级别的并发，但单个 item 内部可能有多个 URL 需要抓取。
        # 这里我们不需要额外的 Semaphore，因为 orchestrator 的 limits 已经在 client 层做了限制。
        # 但为了保险起见，如果任务数超过 50，我们分批执行。

        BATCH_SIZE = 50
        all_results = []
        for i in range(0, len(tasks_to_run), BATCH_SIZE):
            batch = tasks_to_run[i:i + BATCH_SIZE]
            batch_results = await asyncio.gather(*batch, return_exceptions=True)
            all_results.extend(batch_results)

        # all_results = await asyncio.gather(*tasks_to_run, return_exceptions=True)

        # 【调整】安全地解析和分离三组任务的结果
        content_end_idx = len(content_tasks)
        job_end_idx = content_end_idx + len(job_tasks)
        final_results["content_results"] = all_results[:content_end_idx]

        job_task_results = all_results[content_end_idx:job_end_idx]
        if job_task_results:
            final_results["job_result"] = job_task_results[0]

        final_results["enterprise_results"] = all_results[job_end_idx:]
        return final_results


# --- 5. Dify 节点主入口 ---
async def main_async(raw_input: Any) -> Dict[str, Any]:
    # 1. 解析输入
    parsed_data = _parse_input_data(raw_input)
    items = parsed_data["items"]

    if not items:
        print("🟡 所有输入均为空，提前返回。")
        return {"scraped_datas": [], "scraped_datas_str": "[]"}

    # 2. 运行调度器
    orchestrator = DataOrchestrator()

    final_results_list = []

    # 3. 批量处理
    # 注意：这里可以进一步优化为并发处理所有items，但为了控制并发量，
    # 也可以选择逐个处理或分批处理。这里简单起见，我们对每个item并发处理其内部任务，
    # 但item之间如果是独立的，也可以并发。
    # 考虑到 DataOrchestrator 内部已经有并发控制，我们这里并发处理所有items

    # 限制并发数为 50，提高处理速度
    semaphore = asyncio.Semaphore(50)

    # 【性能优化】创建全局共享的 Client
    ssl_context = httpx.create_ssl_context(verify=False)
    # 增加连接池大小以应对高并发，同时设置合理的超时
    async with httpx.AsyncClient(http2=True, verify=ssl_context, timeout=30, follow_redirects=True,
                                 limits=httpx.Limits(max_connections=100, max_keepalive_connections=50)) as client:

        async def process_single_item(item):
            async with semaphore:
                web_url_info_list = item["web_url_info_list"]
                video_url_info_list = item["video_url_info_list"]
                career_payload = item["career_payload"]
                enterprise_names = item["enterprise_names"]

                # 传入共享的 client
                results = await orchestrator.process_all(web_url_info_list, career_payload, enterprise_names, client)

                # 3. 格式化网页内容输出
                all_source_list = []
                for result in results["content_results"]:
                    if isinstance(result, Exception): continue
                    if result.get("status") == "success":
                        sanitized_url = re.sub(r'[^a-zA-Z0-9]', '-',
                                               result.get("url", "").replace("https://", "").replace("http://", ""))
                        all_source_list.append({
                            "type": "web", "source_id": f"web-{sanitized_url[:100]}", "url": result.get("url"),
                            "title": result.get("title"), "source": result.get("source"),
                            "snippet": result.get("snippet"),
                            "query": result.get("query"), "content": result.get("content", "")
                        })
                # 4. 格式化视频内容输出
                all_video_list = []
                for video_item in video_url_info_list:
                    all_video_list.append({
                        "type": "video", "url": video_item.get("url"), "title": video_item.get("title"),
                        "source": video_item.get("source"), "snippet": video_item.get("snippet"),
                        "video_id": video_item.get("video_id"), "embed_url": video_item.get("embed_url"),
                        "thumbnail_url": video_item.get("thumbnail_url"), "query": video_item.get("query")
                    })

                # 处理招聘和企业信息结果
                career_postings = results.get("job_result")
                if career_postings is None:
                    career_postings = {}
                elif isinstance(career_postings, Exception):
                    career_postings = {"status": "failed", "data": [], "message": f"任务异常: {career_postings}"}

                enterprise_infos_raw = results.get("enterprise_results", [])
                enterprise_infos_output = {}
                if enterprise_names:
                    successful_data = []
                    failed_queries = []

                    for item in enterprise_infos_raw:
                        res = item
                        if isinstance(item, Exception):
                            res = {"status": "failed", "message": f"任务执行异常: {str(item)}", "query_name": "Unknown"}

                        if res.get("status") == "success" and res.get("data"):
                            successful_data.append(res["data"])
                        elif res.get("status") in ["failed", "skipped"]:
                            failed_queries.append({
                                "query_name": res.get("query_name", "N/A"),
                                "error_message": res.get("message", "未知错误")
                            })
                    final_status = "skipped"
                    if successful_data or failed_queries:
                        if not failed_queries:
                            final_status = "success"
                        elif not successful_data:
                            final_status = "failed"
                        else:
                            final_status = "partial_success"

                    enterprise_infos_output = {
                        "status": final_status,
                        "data": successful_data,
                        "failed_queries": failed_queries,
                        "summary": f"共查询 {len(enterprise_names)} 个企业，成功 {len(successful_data)} 个，失败 {len(failed_queries)} 个。"
                    }

                # 6. 组装单个item的输出
                comprehensive_data_output = {"all_source_list": all_source_list, "all_video_list": all_video_list}
                return {
                    "comprehensive_data": comprehensive_data_output,
                    "career_postings": career_postings,
                    "enterprise_infos": enterprise_infos_output
                }

        tasks = [process_single_item(item) for item in items]
        final_results_list = await asyncio.gather(*tasks)

    final_output = {
        "scraped_datas": final_results_list
    }
    return {
        "scraped_datas": final_output["scraped_datas"],
        "scraped_datas_str": json.dumps(final_output, ensure_ascii=False, indent=2)
    }


def main(datas_input: Any) -> Dict[str, Any]:
    try:
        return _dify_debug_return(asyncio.run(main_async(raw_input=datas_input)))
    except Exception as e:
        print(f"‼️ 节点执行时发生顶层错误: {e}")
        # 构造一个包含错误信息的列表结构返回
        error_payload = [{
            "comprehensive_data": {
                "all_source_list": [
                    {"type": "web", "source_id": "NODE_EXECUTION_ERROR", "title": "节点执行失败", "url": "",
                     "content": f"An error occurred: {str(e)}\n\n{traceback.format_exc()}"}],
                "all_video_list": []
            },
            "career_postings": {},
            "enterprise_infos": {
                "status": "failed",
                "data": [],
                "failed_queries": [{"query_name": "Node Execution", "error_message": "节点顶层异常"}],
                "summary": "节点执行失败"
            }
        }]
        return _dify_debug_return({
            "scraped_datas": error_payload,
            "scraped_datas_str": json.dumps({"scraped_datas": error_payload}, ensure_ascii=False, indent=2)
        })


main([
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "高职智能网联汽车专业毕业生在自动驾驶HIL测试工程师岗位的日常工作内容与真实场景",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://career.shisu.edu.cn/position.html?wid=26030215360878419819&jobtype=2",
                        "searchapi_title": "广州虹科电子科技有限公司浏览次数：29",
                        "searchapi_source": "上海外国语大学学生就业创业服务网",
                        "searchapi_snippet": "对自动驾驶、AR、通信、网络可视化、测试测量、汽车电子、生物科技、环境监测 ... 负责智能驾驶系统场景搭建，基于专业仿真软件构建openX系列场景，如交通环境 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "http://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=001285&id=11479030",
                        "searchapi_title": "瑞立科密：首次公开发行股票并在主板上市招股说明书 ...",
                        "searchapi_source": "手机新浪网",
                        "searchapi_snippet": "历经20余年发展，公司已成为国内商用车主动安全系统龙头企业。报告期内，公司营业收入、净利润维持在相对较高水平。公司业务模式成熟、主要客户稳定，下游 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://kyy.hfut.edu.cn/_upload/article/files/74/e8/b93ea6a64a20a4ce65c547120c24/51644423-8164-42f9-ad79-02cbb2996c80.xls",
                        "searchapi_title": "修改(2) - 科研院",
                        "searchapi_source": "合肥工业大学",
                        "searchapi_snippet": "... 测试验证能力，具有成熟的解决方案和必要的测试验证能力，能满足自动驾驶网联的技术需求。若揭榜方具有以上智能驾驶环境感知系统的开发设计经验则优先选择。另外鉴于 ..."
                    }
                ]
            },
            {
                "query": "车企自动驾驶域控制器HIL台架搭建与场景配置标准操作流程SOP",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/1939695171764754342",
                        "searchapi_title": "智能网联新规将来，全链条收紧多个环节？安全如何落到每 ...",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "首先，要求企业在机动车合格证系统中完整、准确填报组合驾驶辅助系统、储能装置单体及总成等关键信息；其次，严格执行OTA活动分类管理，未经备案不得开展OTA， ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "http://xxgk.jl.gov.cn/zcbm/fgw_97992/xxgkmlqy/202512/P020251229513740369959.doc",
                        "searchapi_title": "附件5.岗位说明书参考示例.doc",
                        "searchapi_source": "吉林省人民政府",
                        "searchapi_snippet": "... 域控制器的设计、开发与集成； 3.攻关高精度定位、多传感器融合感知、预测性规划 ... HIL测试台架优先使用权； 2.数据与样本：可获取公司积累的海量电池实验室 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.magna.com/zh/%E4%BA%A7%E5%93%81%E6%8A%80%E6%9C%AF/%E6%95%B4%E8%BD%A6/%E6%95%B4%E8%BD%A6%E5%B7%A5%E7%A8%8B",
                        "searchapi_title": "整车工程- 全球一站式整车代工开发和生产的汽车供应商",
                        "searchapi_source": "Magna International",
                        "searchapi_snippet": "... HiL）测试对于开发早期阶段的系统验证至关重要。麦格纳不仅开展域控制器硬件在环测试，还借助合作伙伴网络提供的设备，针对摄像头、激光雷达和雷达开展复现式硬件在环测试。"
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "高等职业教育智能网联汽车专业HIL仿真测试课程的知识能力素养三维教学目标",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.researchgate.net/publication/394140381_zhinengwanglianqichexunifangzhenhunheshijiaoxuemoshishijian",
                        "searchapi_title": "智能网联汽车虚拟仿真混合式教学模式实践",
                        "searchapi_source": "ResearchGate",
                        "searchapi_snippet": "为深入推进教育数字化战略行动，提升教育教学的数字化建设水平，文章通过分析本学院智能网联汽车技术相关课程信息学生和学生学情，深入挖掘课程建设要点， ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "http://word.baidu.com/view/eba9b7174e7302768e9951e79b89680203d86b29.html",
                        "searchapi_title": "智能网联汽车测试教学反思",
                        "searchapi_source": "百度一下",
                        "searchapi_snippet": "知识目标：学生需掌握智能网联汽车测试的核心技术框架，包括虚拟仿真测试、封闭场地测试、开放道路测试的技术逻辑；理解V2X通信测试、自动驾驶功能验证（如自动泊车、AEB紧急制 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://gxq.cq.gov.cn/ggtz/202405/t20240516_13213474.html",
                        "searchapi_title": "2024年二季度重庆高新区急需紧缺人才目录",
                        "searchapi_source": "重庆高新区",
                        "searchapi_snippet": "熟悉汽车理论、智能网联基础知识、智能网联汽车各系统构成及功能，了解智能网联汽车行业标准和技术导向，具有良好的动手实操能力。 91. 轻度紧缺. 汽车 ..."
                    }
                ]
            },
            {
                "query": "自动驾驶硬件在环仿真测试工程师岗位能力要求与职业素养规范",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_44124323/article/details/115396542",
                        "searchapi_title": "自动驾驶团队- 能力要求和职能划分原创",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "仿真测试工程师. 负责自动驾驶的算法离线仿真测试。 配合算法工程师设计各模块测试案例，搭建测试系统和测试工具。 根据自动驾驶运行场景, 使用3D建模 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.eisa.xyz/index/information/178.html",
                        "searchapi_title": "工信部强国杯| 自动驾驶仿真测试技术赛项正式开始报名",
                        "searchapi_source": "易飒科技",
                        "searchapi_snippet": "竞赛内容完全覆盖多家行业企业亟需的智能驾驶仿真测试工程师典型岗位的核心能力要求，覆盖《驾驶自动化仿真测试》专业核心课程的重要知识点，能够培养和检验 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://gxq.cq.gov.cn/ggtz/202405/t20240516_13213474.html",
                        "searchapi_title": "2024年二季度重庆高新区急需紧缺人才目录",
                        "searchapi_source": "重庆高新区",
                        "searchapi_snippet": "1.本科及以上学历，汽车、机械、电气、软件等相关专业；2.5年及以上EE架构工作经验；3.熟练掌握CANoe、CAD、CATIA、office等工具，熟识EE架构开发流程；4.从事 ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "汽车电子V型开发流程中MIL SIL HIL VIL的区别与详细对比讲解",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/606321816",
                        "searchapi_title": "自动驾驶仿真及其工具链（6万字扫盲）",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "前面提到，按照自动驾驶算法的开发流程，完整的开发需要经历MIL、SIL、HIL、VIL、DIL。一般情况仿真软件的支持会止于HIL。对于仿真软件，需要提供与被 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_38135620/article/details/125020306",
                        "searchapi_title": "ADAS HIL仿真测试及基于CANoe的交通信号灯仿真",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "博主前面的博文已经简要介绍了Carsim、Prescan 与Simulink 在“V”型开发中MIL、SIL的应用，对于同样重要的HIL硬件在环仿真测试，就在本篇博文来讲讲，这次 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/sluck_0430/article/details/136427298",
                        "searchapi_title": "仿真&在环仿真相关资料|在环仿真快速入门原创",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "目前许多汽车公司已经从传统的开发模式转移到V形开发模型，以减少 ... 模型开发中的常用概念：MIL、SIL、PIL和HIL (3cst.cn) · 虚拟仿真测试介绍 ..."
                    }
                ]
            },
            {
                "query": "自动驾驶HIL硬件在环仿真在Corner Cases危险场景测试中的优势与具体数据",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/388368795",
                        "searchapi_title": "一文读懂自动驾驶仿真测试技术现状",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "覆盖的场景工况有限，尤其是对于“corner case”，很难复现; 对于一些极端的危险场景，道路测试安全性无法保障. 仿真测试的优势：. 测试场景配置灵活，场景覆盖 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://auto.sina.cn/zz/hy/2022-12-02/detail-imqqsmrp8291784.d.html",
                        "searchapi_title": "何为自动驾驶开发“刚需”？昆易数据回注系统填补国产测试链 ...",
                        "searchapi_source": "新浪网",
                        "searchapi_snippet": "相比之下，仿真测试场景配置灵活，场景覆盖率高，测试过程安全，能够复现Corner Case进行再测试，通过自动化仿真测试能够显著降本增效。 近年来，随着智能驾驶 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.pnpchina.com/resources/Mobility20220409",
                        "searchapi_title": "出行洞察：仿真软件在自动驾驶上的应用",
                        "searchapi_source": "Plug and Play 中国",
                        "searchapi_snippet": "普通场景下的自动驾驶仿真算法已经比较完善，突破难点在于一些极端场景（corner cases）。由于极端场景在现实中可遇不可求，利用仿真平台可以便捷生成，所以 ..."
                    }
                ]
            },
            {
                "query": "自动驾驶传统道路测试与HIL仿真测试的时间成本与经济成本数据对比分析",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/388368795",
                        "searchapi_title": "一文读懂自动驾驶仿真测试技术现状",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "自动驾驶汽车商用化需经历的三个测试阶段：仿真测试、封闭场地测试、开放道路测试。 自动驾驶仿真测试：主要是以数学建模的方式将自动驾驶的应用场景进行 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.opal-rt.com/zh/%E5%8D%9A%E5%AE%A2/a-guide-to-hardware-in-the-loop-testing/",
                        "searchapi_title": "硬件在环(HIL) 测试指南| 实时验证与成本节约",
                        "searchapi_source": "OPAL-RT",
                        "searchapi_snippet": "硬件在环(HIL) 测试是一种实时仿真，通过将嵌入式控制系统连接到其所控制的物理系统的高保真数字仿真上，对其进行验证和测试。工程师不使用实际硬件原型进行 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://transport.chd.edu.cn/article/doi/10.19818/j.cnki.1671-1637.2023.06.002",
                        "searchapi_title": "自动驾驶测试与评价技术研究进展 - 交通运输工程学报",
                        "searchapi_source": "交通运输工程学报",
                        "searchapi_snippet": "摘要: 针对实际复杂交通运行环境中自动驾驶车辆整车级测试成本高、周期长、覆盖度低、缺乏完善工具链等难题，分析了自动驾驶测试与评价技术7大领域的研究现状，展望了 ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "详细对比智能网联汽车测试MIL SIL HIL VIL的阶段 对象 真实度 成本与优缺点 表格",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://transport.chd.edu.cn/article/doi/10.19818/j.cnki.1671-1637.2023.06.002",
                        "searchapi_title": "自动驾驶测试与评价技术研究进展 - 交通运输工程学报",
                        "searchapi_source": "交通运输工程学报",
                        "searchapi_snippet": "摘要: 针对实际复杂交通运行环境中自动驾驶车辆整车级测试成本高、周期长、覆盖度低、缺乏完善工具链等难题，分析了自动驾驶测试与评价技术7大领域的研究现状，展望了 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.researchgate.net/publication/375927759_zidongjiashiqichechangjingceshiyanjiujinzhanzongshu",
                        "searchapi_title": "自动驾驶汽车场景测试研究进展综述",
                        "searchapi_source": "ResearchGate",
                        "searchapi_snippet": "... 汽车典型场景. 的测试覆盖度，需要建立面向不同复杂度和数据来源的. 预处理、挖掘、分析和场景提取的技术体系。 5.1.2 研究边缘场景的表征机理. 受真实 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/CV_Autobot/article/details/135421058",
                        "searchapi_title": "30个够么！自动驾驶仿真框架&模拟器汇总转载",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "本文汇总了传统仿真软件、新型仿真框架，以及仿真平台、光学仿真、仿真引擎，可作为学习、研究、开发的参考资料。 1.仿真引擎. Unity."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "智能网联汽车HIL测试系统台架硬件结构详细构成 上位机 实时计算机 NI dSPACE",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/683531049",
                        "searchapi_title": "2023年中国半实物仿真模拟（HiL）行业洞察报告",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "下位机在整个系统中扮演核心引擎的角色，负责协同控制器与被控对象之间的交互任务，以确保系统在虚拟环境中的运行与实际环境一致。上位机则是一台普通计算机 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.eet-china.com/mp/a355654.html",
                        "searchapi_title": "对HIL台架系统的一点认识",
                        "searchapi_source": "电子工程专辑",
                        "searchapi_snippet": "总之，ECU系统层级的HIL测试涵盖了ECU硬件和软件的设计验证，通常在汽车研发体系中还有ECU软件层级的HIL测试，相对于ECU系统层级是针对ECU系统需求的验证，ECU ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/lovely_yoshino/article/details/128590923",
                        "searchapi_title": "最全国内外自动驾驶仿真软件总结转载",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "CarSim同时提供了RT版本，可以支持主流的HIL测试系统，如dSpace和NI的系统，方便的联合进行HIL仿真。 在这里插入图片描述"
                    }
                ]
            },
            {
                "query": "自动驾驶HIL测试台架IO板卡 通信板卡 故障注入模块FIU 负载箱 接线原理与功能",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/614657717",
                        "searchapi_title": "全球十大品牌实时仿真机系统介绍",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "Typhoon HIL工具提供了独特的用户体验，没有第三方软件和硬件的复杂性，所有的库安装只需单击一次，模型在几秒钟内编译，数字输入采样低至3.5 ns分辨率，实时模拟运行的时间步长 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://chiplayout.net/Layout/4ofxu/",
                        "searchapi_title": "微电子词典 - 芯片版图|Chiplayout",
                        "searchapi_source": "chiplayout.net",
                        "searchapi_snippet": "automatic circuit board tester 自动电路板测试机ACBT automatic circuit exchange 自动电路交换机，自动电路交换ACE automatic circuit tester 自动 ..."
                    }
                ]
            },
            {
                "query": "高职汽车专业HIL仿真测试台架硬件拓扑逻辑与接口定义",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.opal-rt.com/zh/%E5%8D%9A%E5%AE%A2/complete-guide-to-data-center-simulation-and-testing/",
                        "searchapi_title": "数据中心仿真测试完整指南",
                        "searchapi_source": "OPAL-RT",
                        "searchapi_snippet": "具备硬件在环（HIL）功能的实时仿真平台：以固定步长执行模型运算，并通过I/O接口连接外部硬件设备。工程师无需实体设备即可零风险验证继电保护逻辑、控制器 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/jiuzhang_0402/article/details/121359092",
                        "searchapi_title": "一文读懂自动驾驶仿真测试技术现状",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "自动驾驶仿真测试：主要是以数学建模的方式将自动驾驶的应用场景进行数字化还原，建立尽可能接近真实世界的系统模型，无需实车直接通过软件进行仿真测试便可 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://transport.chd.edu.cn/article/doi/10.19818/j.cnki.1671-1637.2023.06.002",
                        "searchapi_title": "自动驾驶测试与评价技术研究进展 - 交通运输工程学报",
                        "searchapi_source": "交通运输工程学报",
                        "searchapi_snippet": "摘要: 针对实际复杂交通运行环境中自动驾驶车辆整车级测试成本高、周期长、覆盖度低、缺乏完善工具链等难题，分析了自动驾驶测试与评价技术7大领域的研究现状，展望了 ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "自动驾驶HIL测试系统软件工具链架构解析 NI VeriStand dSPACE ControlDesk使用原理",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_29317963/article/details/151997773",
                        "searchapi_title": "硬件在环（HIL）测试系统设计与实战资源包原创",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "两种方法形成分层验证体系，HIL侧重硬件性能验证，SIL侧重算法逻辑验证。主流工具链包括dSPACE、NI PXI和Simulink等，应用场景涵盖自动驾驶、航空航天等领域 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.zhihu.com/question/50355287",
                        "searchapi_title": "关于HIL测试，dSPACE系统ETAS系统NI系统三者的利弊？？",
                        "searchapi_source": "知乎",
                        "searchapi_snippet": "... 系统原理如下图所示：. VCU HiL 测试系统中上位机电脑安装Veristand、Teststand 软件用于测试过程管理和测试序列编辑，通过以太网与PXI 机箱中的实时 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://upjianli.com/jianlifanwen/1788.html",
                        "searchapi_title": "L3智能驾驶域控制器HIL台架搭建与百万公里仿真测试经验",
                        "searchapi_source": "upjianli.com",
                        "searchapi_snippet": "主导并从零搭建L3级智能驾驶域控制器全闭环HIL测试台架，负责系统架构设计、硬件选型、软件集成与调试，构建了高效的测试验证平台。"
                    }
                ]
            },
            {
                "query": "CarSim与VTD在HIL硬件在环系统中的联合仿真原理与UDP/TCP通信机制",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/qq_42779033/article/details/157218493",
                        "searchapi_title": "揭秘CarSim实时部署的“三重门” 原创",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "自动驾驶仿真：Carsim、NI和VTD联合仿真课题二. 文章目录前言一、设备配置1、硬件需求2、网络配置二、Carsim工程配置1、创建工程2、创建数据库3、参数 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.eet-china.com/mp/a160504.html",
                        "searchapi_title": "一文盘点国内外24家自动驾驶仿真软件",
                        "searchapi_source": "电子工程专辑",
                        "searchapi_snippet": "CarSim自带标准的Matlab/Simulink接口，可以方便的与Matlab/Simulink进行联合仿真，用于控制算法的开发，同时在仿真时可以产生大量数据结果用于后续使用 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.scribd.com/document/937486213/%E6%99%BA%E8%83%BD%E7%BD%91%E8%81%94%E6%B1%BD%E8%BD%A6%E9%80%9A%E7%94%A8%E8%B7%A8%E5%B9%B3%E5%8F%B0%E5%AE%9E%E6%97%B6%E4%BB%BF%E7%9C%9F%E7%B3%BB%E7%BB%9F%E6%9E%B6%E6%9E%84%E5%8F%8A%E5%BA%94%E7%94%A8-%E8%83%A1%E8%80%98%E6%B5%A9",
                        "searchapi_title": "智能网联汽车通用跨平台实时仿真系统架构及应用胡耘浩",
                        "searchapi_source": "Scribd",
                        "searchapi_snippet": "本文提出了一种面向智能网联汽车的跨平台实时仿真系统架构，旨在解决数据通信不通用、系统架构多样且难以扩展的问题。通过借鉴车载以太网传输协议，设计了通用的数据通信 ..."
                    }
                ]
            },
            {
                "query": "HIL系统仿真模型编译过程与实时操作系统(RTOS)运行底层逻辑详解",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/qq_45191106/article/details/143698769",
                        "searchapi_title": "自动驾驶仿真：软件在环（SIL）测试详解（精简版入门） 原创",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "在SIL仿真中，自动驾驶控制算法以软件模型的形式运行，并与仿真平台（如CarSim、PreScan等）进行连接。仿真平台模拟虚拟车辆的物理行为、传感器输入和外部环境 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.eet-china.com/mp/a417727.html",
                        "searchapi_title": "嵌入式项目管理全流程",
                        "searchapi_source": "电子工程专辑",
                        "searchapi_snippet": "实时操作系统(RTOS)需求评估; 开发工具链评估（编译器，调试器，IDE ... 硬件在环(HIL)测试系统; 持续集成流水线中的自动化测试; 测试覆盖率分析 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "http://www.360doc.com/content/16/0807/08/35553204_581376685.shtml",
                        "searchapi_title": "半实物仿真技术发展综述",
                        "searchapi_source": "360DOC",
                        "searchapi_snippet": "仿真计算机是实时仿真系统的核心部分，它运行实体对象和仿真环境的数学模型和程序。一般来说，采用层次化、模块化的建模法，将模块化程序划分为不同的速率块， ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "CarSim车辆动力学建模基础教程 界面操作与核心参数配置对仿真的影响",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.carsim.net.cn/support.html",
                        "searchapi_title": "CarSim教程中心-车辆动力学仿真软件使用技巧",
                        "searchapi_source": "carsim.net.cn",
                        "searchapi_snippet": "在使用CarSim进行整车动力学仿真时，精确的整车参数标定是实现高保真模型的前提。参数标定不到位会导致仿真结果与实车行为偏差较大，进而影响后续控制策略设计、零部件优化或 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.carsim.net.cn/tuijian/cs-erbusvni.html",
                        "searchapi_title": "CarSim怎么建立整车模型CarSim车辆动力学仿真结果不稳定 ...",
                        "searchapi_source": "carsim.net.cn",
                        "searchapi_snippet": "一、CarSim整车模型的构建步骤 · 1、选择合适的模板车型 · 2、设定车辆结构参数 · 3、配置动力系统模型 · 4、调整制动系统与轮胎特性 · 5、定义测试场景与激励 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/efc123456/article/details/154978481",
                        "searchapi_title": "CarSim 2017新手必看：从零开始掌握主界面操作（附实用 ...",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "左侧面板是定义车辆物理特性的核心区域，这里每一个参数都直接影响仿真结果的准确性。新手常犯的错误是直接修改系统自带的基准参数集(Dataset)，这可能导致 ..."
                    }
                ]
            },
            {
                "query": "自动驾驶HIL测试中轮胎魔术公式模型与悬架多体动力学建模深度原理解析",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_62244995/article/details/146985504",
                        "searchapi_title": "新能源汽车整车动力学模型：从理论方程到HIL测试的硬核解析",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "本文从牛顿-欧拉方程出发，深度解构新能源汽车16自由度耦合动力学模型，覆盖纵向/侧向/垂向动力学及电驱耦合效应。面向HIL测试工程师、电机控制工程师、 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://transport.chd.edu.cn/article/doi/10.19818/j.cnki.1671-1637.2023.06.002",
                        "searchapi_title": "自动驾驶测试与评价技术研究进展 - 交通运输工程学报",
                        "searchapi_source": "交通运输工程学报",
                        "searchapi_snippet": "摘要: 针对实际复杂交通运行环境中自动驾驶车辆整车级测试成本高、周期长、覆盖度低、缺乏完善工具链等难题，分析了自动驾驶测试与评价技术7大领域的研究现状，展望了 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/1979500365935310143",
                        "searchapi_title": "跨越“仿真到实车”的鸿沟：如何构建端到端高置信度验证体系？",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "模型迭代：调整动力学模型参数（如轮胎魔术公式参数、悬挂刚度），直到仿真与实测的响应曲线RMSE在预设范围内（如横摆角速度误差<5%）[10]。 系统精度验证：在HIL ..."
                    }
                ]
            },
            {
                "query": "高职汽车专业CarSim仿真软件实训操作详细步骤与配置案例",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/gitblog_06764/article/details/147204508",
                        "searchapi_title": "汽车主动避撞与跟车功能联合仿真资源包",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "使用说明 · 下载并解压资源包。 · 在Matlab Simulink中打开相应的模型文件。 · 在Carsim中导入相应的模型文件。 · 根据需求修改模型参数，并进行联合仿真。"
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://transport.chd.edu.cn/article/doi/10.19818/j.cnki.1671-1637.2023.06.002",
                        "searchapi_title": "自动驾驶测试与评价技术研究进展 - 交通运输工程学报",
                        "searchapi_source": "交通运输工程学报",
                        "searchapi_snippet": "摘要: 针对实际复杂交通运行环境中自动驾驶车辆整车级测试成本高、周期长、覆盖度低、缺乏完善工具链等难题，分析了自动驾驶测试与评价技术7大领域的研究现状，展望了 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "http://m.jiuzhang-ai.com/nd.jsp?id=37",
                        "searchapi_title": "一文读懂自动驾驶仿真测试技术现状 - 九章智驾官网",
                        "searchapi_source": "jiuzhang-ai.com",
                        "searchapi_snippet": "仿真流程主要分三个步骤：路网搭建， 动态场景配置， 仿真运行. ——提供图形化的交互式路网编辑器Road Network Editor (ROD)， 在构建路网仿真环境的 ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "自动驾驶HIL系统摄像头视频暗箱测试与视频数据流直接注入技术原理解析对比",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/1974412440289702107",
                        "searchapi_title": "dSPACE 智能驾驶SIL/HIL 仿真验证解决方案：技术赋能 ...",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "由AURELION生成的摄像头数据，一方面可以使用真实车辆摄像头通过视频暗箱直接采集仿真场景图像，输入给控制单元；另一方面可以通过视频注入设备直接将仿真 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/vincent_321/article/details/147412656",
                        "searchapi_title": "对智能驾驶HIL仿真系统的一些总结与反思",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "与其它领域的HIL仿真系统相比，智能驾驶的HIL仿真要求更高，尤其是场景保真度、传感器模型精度、仿真实时性、数据带宽、数据接口稳定性等方面带来了巨大的 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.dfzk.com/solu_115.html",
                        "searchapi_title": "ADAS硬件在环（ADAS HiL）(东方中科)-在环仿真测试",
                        "searchapi_source": "东方中科",
                        "searchapi_snippet": "搭建ADAS HIL测试系统的主要目的是通过HIL仿真技术，验证并完善ADAS系统开发的各项功能，因此HIL测试系统必须能够仿真雷达与摄像头接收的大量并且丰富的道路交通场景信号， ..."
                    }
                ]
            },
            {
                "query": "自动驾驶仿真中毫米波雷达目标级仿真与原始信号级物理机理仿真深度剖析",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.eet-china.com/mp/a188942.html",
                        "searchapi_title": "自动驾仿真测试平台干货内容梳理",
                        "searchapi_source": "电子工程专辑",
                        "searchapi_snippet": "依据仿真的难易程度，传感器仿真的又可分为三个层级：物理信号仿真、原始信号仿真和目标级信号仿真。 ... 信号分别是毫米波雷达和超声波雷达的物理信号。"
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://radars.ac.cn/cn/article/doi/10.12000/JR23119?viewType=HTML",
                        "searchapi_title": "汽车毫米波雷达信号处理技术综述",
                        "searchapi_source": "雷达学报",
                        "searchapi_snippet": "深度学习在毫米波雷达信号处理中应用越来越广泛，且已在实际应用中取得了很好 ... 特征级融合也称为中层融合，基于深度学习的目标检测模型可以同时提取雷达和图像 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/CV_Autobot/article/details/126296174",
                        "searchapi_title": "自动驾驶| 毫米波雷达视觉融合方案综述（数据级/决策级/特征 ...",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "激光雷达的测量包含语义信息，并满足先进自主驾驶的感知要求，而毫米波雷达缺乏这一点；. 无法从毫米波雷达测量中完全滤除杂波，导致雷达信号处理中出现错误；."
                    }
                ]
            },
            {
                "query": "高精度激光雷达3D点云仿真在HIL硬件在环测试中的实时渲染与注入实现方法",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.dspace.com/zh/zho/home/applicationfields/stories/smvic-of-sensors-and-actuator.cfm",
                        "searchapi_title": "SMVIC：传感器和执行器",
                        "searchapi_source": "dSPACE",
                        "searchapi_snippet": "摄像头传感器由原始数据仿真进行测试，在点云等级上对激光雷达传感器进行仿真测试。基于物理的渲染和经过扩展的3D点云是dSPACE AURELION的两个核心元素，要生成其中需要的 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/SYNKROTRON/article/details/147891530",
                        "searchapi_title": "多传感器注入HIL仿真系统用户案例",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "硬件在环（HIL，Hardware In the Loop）仿真作为关键验证手段，需支持多传感器物理级建模，以应对复杂场景下感知算法训练、控制策略验证的严苛需求，但传统仿真 ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "自动驾驶仿真OpenDRIVE高精地图路网与OpenSCENARIO动态场景XML标准语法详解",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/zataji/article/details/134093946",
                        "searchapi_title": "ASAM OpenDRIVE V1.7协议超详解（一）",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "OpenDRIVE是一种用于虚拟仿真场景的开放标准，旨在描述道路网络和场景的详细信息，是仿真场景中的静态组成部分，以支持自动驾驶和驾驶辅助系统的开发和测试。"
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/LiangDao_2020/article/details/108992976",
                        "searchapi_title": "中文版ASAM OpenSCENARIO与OpenDRIVE标准正式发布",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "OpenDRIVE定义了一个标准的静态仿真场景格式，OpenSCENARIO则主要规范了仿真动态驾驶场景的描述语言与变量信息，以实现不同仿真测试软件的兼容性。 在这里 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/qq_41854291/article/details/116860289",
                        "searchapi_title": "OpenX系列标准：OpenDRIVE标准简述",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "​ ASAM OpenDRIVE格式提供了用可扩展标记语言(XML)语法描述道路网络的通用基础，使用文件扩展名xodr。 存储在ASAM OpenDRIVE文件中的数据描述了道路、车道 ..."
                    }
                ]
            },
            {
                "query": "VTD与PreScan仿真软件搭建复杂交通场景、树木建筑及恶劣雨雪天气详细教程",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.51cto.com/whaosoft143/13774809",
                        "searchapi_title": "51c自动驾驶~合集5",
                        "searchapi_source": "51CTO博客",
                        "searchapi_snippet": "... 软件；在静态场景仿真方面有一些大规模城市构建仿真软件；在构建复杂交通流场景方面也有一些软件。这些软件都可以纳入到整个自动驾驶仿真体系里来。"
                    }
                ]
            },
            {
                "query": "高职智能网联汽车交通场景仿真软件微观交通流配置与实操教学案例",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_42584758/article/details/147546404",
                        "searchapi_title": "Vissim软件操作与交通仿真实战入门教程原创",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "简介：Vissim是用于交通工程微观仿真的专业软件，由PTV集团开发。本教程为初学者提供从基础到实践的全面指南，详细介绍如何使用Vissim进行城市交通系统的 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://transport.chd.edu.cn/article/doi/10.19818/j.cnki.1671-1637.2020.05.004",
                        "searchapi_title": "智能网联汽车协同生态驾驶策略综述",
                        "searchapi_source": "交通运输工程学报",
                        "searchapi_snippet": "摘要: 为了跟踪近年来智能网联汽车(CAV)协同生态驾驶策略的研究进展, 分析了车辆、驾驶行为、交通网络和社会这4类因素对CAV能耗的影响程度, 以车辆、基础设施和旅行者为 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.sy.uestc.edu.cn/",
                        "searchapi_title": "实验科学与技术",
                        "searchapi_source": "实验科学与技术",
                        "searchapi_snippet": "高职实践教学适应性改革——以开放式迭代创新训练平台的研制为例. 罗华安, 朱方园 ... 基于虚拟仿真实验教学资源和智能教学平台的混合式实验教学体系，结合线上线下 ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "E-NCAP与C-NCAP自动紧急制动AEB测试法规与VRU行人鬼探头典型工况详解",
                "errors": [],
                "web_results": []
            },
            {
                "query": "基于HIL台架的AEB自动紧急制动系统硬件在环测试全流程操作与3D场景搭建",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/629034168",
                        "searchapi_title": "自动驾驶数据闭环与工程化",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "比如T5 HiL台架的测试、T6 ViL 测试和T7量产车的测试，其测试执行 ... 假设我们在T7样车上进行AEB 功能测试，测试场景是过马路时检测到行人紧急制动。"
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://transport.chd.edu.cn/article/doi/10.19818/j.cnki.1671-1637.2023.06.002",
                        "searchapi_title": "自动驾驶测试与评价技术研究进展 - 交通运输工程学报",
                        "searchapi_source": "交通运输工程学报",
                        "searchapi_snippet": "摘要: 针对实际复杂交通运行环境中自动驾驶车辆整车级测试成本高、周期长、覆盖度低、缺乏完善工具链等难题，分析了自动驾驶测试与评价技术7大领域的研究现状，展望了 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.eet-china.com/mp/a228007.html",
                        "searchapi_title": "详解整车五大域控制器测试解决方案",
                        "searchapi_source": "电子工程专辑",
                        "searchapi_snippet": "ADAS域测试系统主要功能包括：支持国内外多种法规下的场景搭建；支持高清地图导入，支持演示算法；支持模型在环（MIL）、实时软件在环（SIL）、硬件在环（HIL） 等多 ..."
                    }
                ]
            },
            {
                "query": "自动驾驶域控制器AEB测试中CAN总线制动指令抓取与底层波形数据分析",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_32306683/article/details/152764831",
                        "searchapi_title": "深入解析汽车总线CAN FD架构与实战应用原创",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "简介：CAN FD（Controller Area Network Flexible Data-rate）作为传统CAN协议的升级版，通过提升数据传输速率至5 Mbps以上，并支持最大64字节的数据长度， ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/641152888",
                        "searchapi_title": "从域控制器到中央计算，全场景车芯优势凸显",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "现在汽车的电子电气架构逐步在更新，越来越多的ECU进行整合，从原来的分布式阶段逐步演进到域控制器和中央计算架构。在架构演进过程中，支撑这一变革的底层芯片也在逐步发展， ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://patents.google.com/patent/CN113734166A/zh",
                        "searchapi_title": "一种基于感知融合swc的汽车自动驾驶控制系统及方法",
                        "searchapi_source": "Google Patents",
                        "searchapi_snippet": "本发明请求保护一种基于感知融合的汽车自动驾驶控制系统，涉及汽车自动驾驶技术，本发明在开源编译环境中集成三级自动驾驶感知代码及AEB、ACC等基于simulink的SWC功能 ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "ACC自适应巡航系统HIL硬件在环仿真前车切入(Cut-in)前车切出(Cut-out)测试场景搭建步骤",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.scribd.com/document/573149745/%E6%99%BA%E8%83%BD%E7%BD%91%E8%81%94%E6%B1%BD%E8%BD%A6%E9%A2%84%E6%9C%9F%E5%8A%9F%E8%83%BD%E5%AE%89%E5%85%A8%E5%89%8D%E6%B2%BF%E6%8A%80%E6%9C%AF%E7%A0%94%E7%A9%B6%E6%8A%A5%E5%91%8A-%E5%8F%91%E5%B8%83%E7%89%88",
                        "searchapi_title": "《智能网联汽车预期功能安全前沿技术研究报告》发布版",
                        "searchapi_source": "Scribd",
                        "searchapi_snippet": "Adaptive Cruise Control ACC 自适应巡航系统. National Highway Traffic NHTSA ... 硬件在环测试主要包括环境感知系统在环测试、决策规划系统在环测试与控制执行 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://huggingface.co/openbmb/cpm-bee-1b/commit/bd72a61dd7a59086ed7456f1dfcaa995c8ec58a3.diff",
                        "searchapi_title": "1",
                        "searchapi_source": "Hugging Face",
                        "searchapi_snippet": "... 车+巨+牙+屯+戈+比+互+切+瓦+止+少+曰+日+中+贝+冈+内+水+见+午+牛+手+气+毛+壬+ ... 出+辽+奶+奴+召+加+皮+边+孕+发+圣+对+台+矛+纠+母+幼+丝+邦+式+迂+刑+戎+动 ..."
                    }
                ]
            },
            {
                "query": "ACC系统基于毫米波雷达仿真模型的距离与相对速度数据流向与PID闭环控制解析",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/574083655",
                        "searchapi_title": "六万字一文读懂汽车自适应巡航控制系统（ACC）",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "对于ACC的毫米波雷达来说，主要利用发送和接受信号的频率差和时间差分别得到目标物体的相对速度和距离（多普勒效应）。因此，对于信号的反射强度就有一定的要求，例如行人、动物 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/checkpaper/article/details/152448180",
                        "searchapi_title": "基于模型预测控制的电动汽车自适应巡航多目标优化与坡度 ...",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "该模型不仅能够提供与前车的相对距离和相对速度信息，还模拟了真实雷达的测量噪声、探测范围和角度限制，使得仿真环境更加贴近实际。为了实现联合仿真，我们 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "http://www.uml.org.cn/car/202405224.asp",
                        "searchapi_title": "自适应巡航控制系统（ACC）超强解析 - 火龙果UML提示",
                        "searchapi_source": "uml.org.cn",
                        "searchapi_snippet": "环境感知由毫米波雷达和摄像头等组成，通过数据融合，感知周边障碍物信息,如相对速度、纵向距离、横向距离、目标加速度以及置信率等。测距传感器用来 ..."
                    }
                ]
            },
            {
                "query": "基于CarSim与VTD的ACC自适应巡航系统联合仿真测试典型案例与控制曲线分析",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/checkpaper/article/details/143050819",
                        "searchapi_title": "CarSim车辆动力学仿真-汽车工程方向【附73案例】 原创",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "CarSim车辆动力学仿真-汽车工程方向【附73案例】 原创 · CarSim、Simulink联合仿真介绍及实例 · CarSim相关教程休资料CarSim仿真案例文档资料18个合集.zip."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/606321816",
                        "searchapi_title": "自动驾驶仿真及其工具链（6万字扫盲）",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "自动驾驶的仿真测试：即以建立车辆模型并将其应用场景进行数字化还原，建立尽可能尽可能接近真实世界的系统模型，如此通过软件仿真即可对自动驾驶系统和算法 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_29240343/article/details/158230952",
                        "searchapi_title": "Carsim&Veristand联合仿真实战指南-常见TCP/IP问题排查与 ...",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "文章浏览阅读287次，点赞3次，收藏3次。本文针对Carsim与Veristand联合仿真中常见的TCP/IP通信问题，提供了从基础排查到高级优化的实战指南。"
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "LKA车道保持辅助系统HIL仿真大曲率弯道、车道线磨损与进出隧道光照突变极限测试场景设计",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_42584507/article/details/153884711",
                        "searchapi_title": "PreScan仿真平台与V2V通信学习实战资料合集原创",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "本学习资料包涵盖PDF文档、代码示例和演示视频，系统讲解PreScan的核心功能，重点聚焦V2V（车对车）通信技术的建模与仿真。学习者可通过理论学习、代码实践和 ..."
                    }
                ]
            },
            {
                "query": "自动驾驶LKA横向控制算法仿真测试原理及横向偏移量(TLC)跨越时间计算模型",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/574144756",
                        "searchapi_title": "五万字一文读懂汽车车道保持辅助系统LKA",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "LKA是一定需要的，因为车道偏移预警只在车辆偏移行车线时才会发挥作用，但是LKA是经常辅助方向盘操作的功能，从而大幅度减轻驾驶的负担；车道偏离预警系统能帮助驾驶员意识到 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_49199313/article/details/158126626",
                        "searchapi_title": "【信息科学与工程学】【智能交通】第五篇自动驾驶02 ...",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "... 偏移→通过解调波长偏移量反推被测物理量. 光谱峰值检测算法；温度-应变交叉敏感解耦；多传感器寻址与数据处理. 1. 特种光纤制备(掺锗等)；2. 紫外激光 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "http://houtai.microbell.com/data/2d148a1d5ea7cc69884d03c73976fdcb.html",
                        "searchapi_title": "逆全球化下中国科技三大发展路径-220822-研究报告-行业分析",
                        "searchapi_source": "迈博汇金",
                        "searchapi_snippet": "... 自动驾驶和高级辅助驾驶产品的大规模量产。 魔视智能自主研发的自动驾驶和高级辅助驾驶产品，涵盖乘用车及商用车、行车及泊车、舱内及舱外、前装及后装等主流市场，量 ..."
                    }
                ]
            },
            {
                "query": "HIL测试台架摄像头视频流注入模型车道线识别与EPS转向扭矩闭环控制测试",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.eet-china.com/mp/a227817.html",
                        "searchapi_title": "详解整车五大域控制器测试解决方案",
                        "searchapi_source": "电子工程专辑",
                        "searchapi_snippet": "底盘域测试系统针对底盘域相关控制器进行仿真功能测试。台架系统可以支持接入真实的方向盘等转向机构和制动、油门、挡位、点火钥匙等驾驶员操作部件。"
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=301221&id=7278142",
                        "searchapi_title": "光庭信息(301221)_公司公告_1-1招股说明书（申报稿） ...",
                        "searchapi_source": "手机新浪网",
                        "searchapi_snippet": "该项目利用高速摄像头进行视觉图像处理的先行研发工作，可基于RIPOC算法在高速视觉图像上实现高精度定位，并围绕高速摄像头实现车辆编队、车道线识别和信号检测等功能。"
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_49199313/article/details/158383188",
                        "searchapi_title": "自动驾驶车辆制造全尺度零部件与制造装备知识库 ...",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "线控制动系统全功能HIL测试台. p_wheel(t)， a_vehicle(t). 轮缸压力模拟精度±0.5%. 整车动力学仿真， 故障注入与诊断测试， ESP/ADAS功能测试. 实时处理 ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "自动泊车APA系统HIL硬件在环仿真测试超声波雷达与AVM环视摄像头数据高精度同步注入原理",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.scribd.com/document/788586057/2023%E5%B9%B4-251%E9%A1%B5-2023%E4%B8%AD%E5%9B%BD%E6%99%BA%E8%83%BD%E7%BD%91%E8%81%94%E6%B1%BD%E8%BD%A6%E4%BA%A7%E4%B8%9A%E6%B4%9E%E5%AF%9F%E6%9A%A8%E7%94%9F%E6%80%81%E5%9B%BE%E8%B0%B1%E6%8A%A5%E5%91%8A",
                        "searchapi_title": "2023年【251页】2023中国智能网联汽车产业洞察暨生态图谱 ...",
                        "searchapi_source": "Scribd",
                        "searchapi_snippet": "... 超声波雷达产品研发商，其自主研发的具有ADAS 功能的软硬件可以实现泊车辅助、自动泊车、遥控泊车、 低速紧急制动、盲区监测、低速驾驶辅助等功能。公司已向多家知名整车 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.plecoforums.com/download/technology_wordfreq-release_utf-8-txt.2599/",
                        "searchapi_title": "2",
                        "searchapi_source": "plecoforums.com",
                        "searchapi_snippet": "... 高 2174746 学生 2162242 ／ 2154423 把 2145130 重要 2143681 我国 2108840 系统 2105748 说 2101240 分析 2079492 已 2070573 建设 2068608 被 2064372 它 2053383 ..."
                    }
                ]
            },
            {
                "query": "低速自动泊车地下车库平行与垂直车位三维仿真场景搭建教程与难点解析",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://news.eeworld.com.cn/qrs/ic634714.html",
                        "searchapi_title": "什么是自动泊车系统？自动泊车路径规划和跟踪技术分析",
                        "searchapi_source": "电子工程世界（EEWorld）",
                        "searchapi_snippet": "1.自动泊车系统自动泊车又称为自动泊车入位，顾名思义就是汽车不用人工干预，系统能够自动帮用户将车辆停入车位。当找到了一个理想的停车地点，只需轻轻启动 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.cnblogs.com/clnchanpin/p/19178247",
                        "searchapi_title": "完整教程：AVM标定：解锁360°全景影像的秘密- clnchanpin",
                        "searchapi_source": "博客园",
                        "searchapi_snippet": "一、AVM 标定是什么？​. 低速挪车，都能大幅减少视觉盲区，提升驾驶的安全性和便捷性。 · 二、AVM 标定的原理大揭秘​ · 三、AVM 标定的实际操作流程​ · 四、AVM ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.51cto.com/u_14439393/5732380",
                        "searchapi_title": "关于自主泊车与自动泊车",
                        "searchapi_source": "51CTO博客",
                        "searchapi_snippet": "自动泊车超声波车位探测系统主要是由布置在车身侧面的超声测距模块构成的， 通过超声传感器对车辆侧面的障碍物进行探测， 即可完成车位探测及定位。"
                    }
                ]
            },
            {
                "query": "自动泊车APA系统复杂路径规划算法与底盘低速微调协同闭环控制仿真测试",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://zhuanlan.zhihu.com/p/574145601",
                        "searchapi_title": "五万字一文读懂汽车自动泊车辅助系统APA",
                        "searchapi_source": "知乎专栏",
                        "searchapi_snippet": "自动车辆控制的一个重要目标是提高安全性和驾驶员的舒适性，APAS通过泊车操作来实现这个目标。自动泊车是一个常见的低速操作场景，是解决自动驾驶最后一公里的核心技术，也是 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_35748962/article/details/153081676",
                        "searchapi_title": "基于模糊算法的MATLAB自动泊车路径规划与仿真系统设计",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "本项目以MATLAB为开发平台，采用模糊逻辑算法实现自动泊车中的路径规划与仿真，涵盖环境建模、传感器数据处理、模糊控制规则设计、路径生成与优化等核心环节 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://news.eeworld.com.cn/qrs/ic634714.html",
                        "searchapi_title": "什么是自动泊车系统？自动泊车路径规划和跟踪技术分析",
                        "searchapi_source": "电子工程世界（EEWorld）",
                        "searchapi_snippet": "1.自动泊车系统自动泊车又称为自动泊车入位，顾名思义就是汽车不用人工干预，系统能够自动帮用户将车辆停入车位。当找到了一个理想的停车地点，只需轻轻启动 ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "自动驾驶多传感器融合算法HIL仿真测试恶劣天气暴雪Corner Case鲁棒性评估",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_49199313/article/details/158383188",
                        "searchapi_title": "自动驾驶车辆制造全尺度零部件与制造装备知识库 ...",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "优化传感器融合算法. 生物医学工程， 传感器融合， 信号处理. 客观评估用于乘员状态监测的各类车载传感技术的性能， 并研究多传感器融合提升精度与鲁棒性的 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.zhihu.com/question/582752246",
                        "searchapi_title": "什么是自动驾驶仿真测试？",
                        "searchapi_source": "知乎",
                        "searchapi_snippet": "... 气候条件。 自动驾驶仿真测试的基本原理就是在计算机仿真环境（场景）内，将真实控制器变成算法，结合传感器仿真等技术，完成对自动驾驶算法的测试和验证。"
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://home.eeworld.com.cn/space-uid-1535315.html",
                        "searchapi_title": "康谋自动驾驶的个人空间动态 - 博客",
                        "searchapi_source": "电子工程世界",
                        "searchapi_snippet": "尤其在极端天气、颠簸路面和电磁干扰等恶劣工况下，如何实现多源传感器数据的高可靠采集、高精度同步与高效率处理，是行业中常遇到的难题。 下文将结合行业实践，系统拆解多 ..."
                    }
                ]
            },
            {
                "query": "基于HIL硬件在环测试的域控制器数据级与目标级多传感器融合算法失效降级策略",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://patents.google.com/patent/CN115755865B/zh",
                        "searchapi_title": "一种商用车辅助驾驶硬件在环测试系统及方法",
                        "searchapi_source": "Google Patents",
                        "searchapi_snippet": "本发明公开一种商用车辅助驾驶硬件在环测试系统，涉及车辆辅助驾驶测试技术领域，包括：上位机模块，用于搭建模型、编译下载、监控模型运算，同时读取控制器中的变量， ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/IOT5570/article/details/155319504",
                        "searchapi_title": "智能底盘在环测试技术：让汽车开发进入“虚拟孪生”时代",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "要理解在环测试，先得明白“智能底盘”是什么。传统底盘负责承载车身、传递动力、控制行驶，而智能底盘则在此基础上融合了电子电气架构，成为连接动力系统、制 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://m.eefocus.com/article/1926570.html",
                        "searchapi_title": "硬件在环仿真（HIL）对于自动驾驶来说有何意义？",
                        "searchapi_source": "与非网",
                        "searchapi_snippet": "硬件在环（HIL）测试是一种将真实硬件置于虚拟环境中进行测试的方法，主要用于自动驾驶系统开发中验证控制逻辑和硬件接口。HIL的关键在于实时性和接口还原， ..."
                    }
                ]
            },
            {
                "query": "大雪恶劣天气下仿真摄像头致盲与毫米波雷达仿真模型信噪比严重衰减的参数设置与验证",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "http://www.zzszq.gov.cn/zt/kjyzt/kjcgzt/202404/P020240408368267365571.doc",
                        "searchapi_title": "一、机器人技术",
                        "searchapi_source": "枣庄市市中区人民政府",
                        "searchapi_snippet": "雷达脉内特征提取与识别技术指标. 完成信噪比10-20dB条件下的脉冲压缩体制雷达信号的识别，采用新的调制特征识别参数的特征提取技术包括：相像系数特征、熵特征、复杂度 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.hf.cas.cn/hzjl/ydhz/hzxm/201402/t20140213_4032258.html",
                        "searchapi_title": "中国科学院合肥物质科学研究院应用型科技成果（2014）",
                        "searchapi_source": "中国科学院合肥物质科学研究院",
                        "searchapi_snippet": "项目针对快速监测区域污染需求，研发了机载成像差分吸收光谱仪。该仪器采用凸面光栅Offner结构超光谱成像、差分吸收光谱（DOAS）、精确控温、整体化工程设计 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "http://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=300397&id=11674569",
                        "searchapi_title": "公司公告_天和防务：2024年度向特定对象发行股票募集 ...",
                        "searchapi_source": "手机新浪网",
                        "searchapi_snippet": "可完成侦察、跟踪、分类，形成全天时的海空一体监测能力。系统采用雷达、光电、AIS、ADS-B和边缘计算盒等前端设备进行全天候的监测和智能识别，结合“边海防 ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "自动驾驶HIL台架测试常见硬件故障诊断排查与接线引脚虚接短路解决方案",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "http://m.microbell.com/wap_detail.aspx?id=d0a27bd86a7d594e70032e070bc9a96f",
                        "searchapi_title": "电动汽车安全指南（2022版）-230131-研究报告-行业分析",
                        "searchapi_source": "迈博汇金",
                        "searchapi_snippet": "软件安全要求验证中的测试环境可为硬件在环，测试台架，或者整车环境。 ... 例如：诊断，控制硬件故障的恢复，为了解决系统失效机制。 5）评估资源 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.scribd.com/document/634800040/Untitled",
                        "searchapi_title": "电动汽车安全指南2019版",
                        "searchapi_source": "Scribd",
                        "searchapi_snippet": "1）明确顺序和故障响应 2）推荐测试用例 3）识别软件故障规避策略 4）安全机制的效果展示。例如：诊断，控制硬件故障的恢复，为了解决系统失效机制。 5）评估资源使用 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/weixin_39689854/article/details/149277330",
                        "searchapi_title": "汽车零部件元件专业词汇原创",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "... 接深沟球轴承轴孔输入齿轮轴第一轴外圈轴承支撑智能管理智能管理模块侧板部节气门开度挠性阳极区数字处理器阴极区矩形电池阻风门不对称负载电路化油器 ..."
                    }
                ]
            },
            {
                "query": "HIL硬件在环仿真CAN/车载以太网通信总线负载率过高丢帧原因与报文排查方法",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://blog.csdn.net/dsafefvf/article/details/154800054",
                        "searchapi_title": "嵌入式系统百问精解：从底层原理到工程实践的95个核心问题 ...",
                        "searchapi_source": "CSDN博客",
                        "searchapi_snippet": "场景：CAN总线负载率>30%时必须使用滤波器，否则CPU被中断淹没。 27. 用 ... 日志上报：通过CAN/以太网上报故障码. 黑匣子：故障时保存寄存器、栈 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.scribd.com/document/596259553/%E4%B8%AD%E5%9B%BD%E6%B1%BD%E8%BD%A6%E5%9F%BA%E7%A1%80%E8%BD%AF%E4%BB%B6%E5%8F%91%E5%B1%95%E7%99%BD%E7%9A%AE%E4%B9%A63-0",
                        "searchapi_title": "中国汽车基础软件发展白皮书3 0 | PDF",
                        "searchapi_source": "Scribd",
                        "searchapi_snippet": "... 汽车主干网中常用的总线通信类型大致包含CAN 总线、 LIN 总线、以太网三类。此外 ... 特殊网络管理策略测试主要用来验证控制器在极端总线条件下（如总线高负载率或总线busoff） ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.eet-china.com/mp/a119158.html",
                        "searchapi_title": "两万字详解自动驾驶开发工具链的现状与趋势",
                        "searchapi_source": "电子工程专辑",
                        "searchapi_snippet": "在汽车电子、工业控制、智能设备等场景中，温度、压力、电压、速度这些物理世界的「模拟信号」，如何精准转化为CAN/CAN FD总线上可传输的「数字报文」？"
                    }
                ]
            },
            {
                "query": "HIL仿真系统实时机步长超时(Task Overrun)与复杂模型编译报错底层成因与解决SOP",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://huggingface.co/openbmb/cpm-bee-1b/commit/bd72a61dd7a59086ed7456f1dfcaa995c8ec58a3.diff",
                        "searchapi_title": "1",
                        "searchapi_source": "Hugging Face",
                        "searchapi_snippet": "... 长+仁+什+片+仆+化+仇+币+仍+仅+斤+爪+反+介+父+从+仑+今+凶+分+乏+公+仓+月+氏+勿+欠+风+丹+匀+乌+勾+凤+六+文+亢+方+火+为+斗+忆+计+订+户+认+冗+讥+心+尺+引+ ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "高职智能网联汽车HIL仿真测试实训室台架操作标准流程SOP 开机 场景配置 数据导出",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.scribd.com/document/596259553/%E4%B8%AD%E5%9B%BD%E6%B1%BD%E8%BD%A6%E5%9F%BA%E7%A1%80%E8%BD%AF%E4%BB%B6%E5%8F%91%E5%B1%95%E7%99%BD%E7%9A%AE%E4%B9%A63-0",
                        "searchapi_title": "中国汽车基础软件发展白皮书3 0 | PDF",
                        "searchapi_source": "Scribd",
                        "searchapi_snippet": "汽标委目前已启动了《智能网联汽车车控操作系统技术要求》、 《智能网联汽车车载操作系统技术要求》标准的研制工作，其中均包含信息安全方面的要求。 2. 国际相关标准（1） ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "http://m.hibor.com.cn/wap_detail.aspx?id=3244081",
                        "searchapi_title": "中国工业软件产业白皮书（2020）-210531-研报",
                        "searchapi_source": "慧博投研",
                        "searchapi_snippet": "工业软件是工业技术软件化的结果，是智能制造、工业互联网的核心内容，是工业化和信息化深度融合的重要支撑，是推进我国工业化进程的重要手段。"
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://money.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=301221&id=7278142",
                        "searchapi_title": "光庭信息(301221)_公司公告_1-1招股说明书（申报稿） ...",
                        "searchapi_source": "手机新浪网",
                        "searchapi_snippet": "智能网联汽车实车测试是汽车整车制造商各车型量产落地的必经阶段。实车测试服务 ... 智能驾驶、智能网联汽车测试以及移动地图数据服务。公司成立伊始，发行人与 ..."
                    }
                ]
            },
            {
                "query": "汽车电子实验室HIL台架高压操作安全准则与防静电手环佩戴等安全管理规范详细条例",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.scribd.com/document/634800040/Untitled",
                        "searchapi_title": "电动汽车安全指南2019版",
                        "searchapi_source": "Scribd",
                        "searchapi_snippet": "本指南2018 版沿电动汽车产业链和生命周期，将电动汽车安全性分成电动乘用车安全、电动客车安全、电池单体和模组、电池管理系统、电机与电控、充电安全、数据监控管理、维修 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://cgzb.tyut.edu.cn/info/1092/29780.htm",
                        "searchapi_title": "太原理工大学2026年02月至12月政府采购意向公开",
                        "searchapi_source": "太原理工大学",
                        "searchapi_snippet": "采购集成光量子芯片制备与表征系统设备，主要用于光量子芯片的制备、表征和性能测试。其中包括两个模块设备，1.光量子芯片制备设备：脉冲激光沉积仪、磁控溅 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.fraunhofer.cn/plus/list.php?tid=11",
                        "searchapi_title": "更多_德国弗劳恩霍夫应用研究促进协会-北京代表处",
                        "searchapi_source": "fraunhofer.cn",
                        "searchapi_snippet": "HSA部门还专注于移动神经技术，以便在实验室外记录大脑活动，并使用在此过程中获得的数据。奥登堡技术的应用领域包括消费电子、交通、汽车、生产、安全、电信和健康。"
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    },
    {
        "comprehensive_data": [
            {
                "query": "国家级高等职业教育智能网联汽车专业理实一体化实训项目任务考核评价表模板范例",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://m.book118.com/html/2026/0110/7064042015011040.shtm",
                        "searchapi_title": "2025教学研究：高职汽车专业群实训教学“三化管理”的思考.docx",
                        "searchapi_source": "原创力",
                        "searchapi_snippet": "不少学校建成了集传统燃油车维修、新能源汽车检测、智能网联系统调试于一体的综合性实训中心，配备了整车故障诊断平台、动力电池测试系统、CAN总线分析仪等一批先进设备。"
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://ycswgz.com/car/images/upload/2021/06/8/202106081444410730.doc",
                        "searchapi_title": "盐城生物工程高等职业技术学校",
                        "searchapi_source": "盐城生物工程高等职业技术学校",
                        "searchapi_snippet": "掌握汽车发动机的基本知识和汽车发动机维修的基本技能。通过理实一体化的教学和实践技能训练，使学生系统掌握汽车发动机的结构、基本工作原理、使用和 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.researchgate.net/publication/394058873_rengongzhinengbeijingxiazhiyejiaoyuchuangxinchuangyeshixunshijianshedeyanjiu",
                        "searchapi_title": "人工智能背景下职业教育创新创业实训室建设的研究",
                        "searchapi_source": "ResearchGate",
                        "searchapi_snippet": "通过分析人工智能技术对职业教育的赋能作用，进一步优化实训室建设的核心要素和实训教学效果，包括智能化教学设备、虚拟仿真环境、校企合作模式以及创新项目孵化机制。旨在 ..."
                    }
                ]
            },
            {
                "query": "自动驾驶硬件在环HIL测试工程师实训岗位能力与硬核专业操作评价打分标准细则",
                "errors": [],
                "web_results": [
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.scribd.com/document/808220651/%E6%B1%BD%E8%BD%A6%E8%A1%8C%E4%B8%9A",
                        "searchapi_title": "汽车行业| PDF",
                        "searchapi_source": "Scribd",
                        "searchapi_snippet": "在云上实现算法更新和测试，实. 现功能的快速迭代开发，实现每日敏捷开发敏捷开发. N/A “集成”，及时发现不匹配和潜在- 软件定义汽车- 自动驾驶软件在环测试. 的安全和 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://www.cnblogs.com/apachecn/p/18467321",
                        "searchapi_title": "TowardsDataScience-博客中文翻译-2020-四十六",
                        "searchapi_source": "博客园",
                        "searchapi_snippet": "自动驾驶汽车是的第一个孩子从现代自动驾驶的角度来看，它们是每个人都在 ... 评价标准中最重要的。当我们考虑投资一家汽车公司时，他们的车能跑 ..."
                    },
                    {
                        "searchapi_type": "web",
                        "searchapi_url": "https://huggingface.co/openbmb/cpm-bee-1b/commit/bd72a61dd7a59086ed7456f1dfcaa995c8ec58a3.diff",
                        "searchapi_title": "1",
                        "searchapi_source": "Hugging Face",
                        "searchapi_snippet": "... 训+议+必+讯+记+永+司+尼+民+弗+弘+出+辽+奶+奴+召+加+皮+边+孕+发+圣+对+台+矛+ ... 环+武+青+责+现+玫+表+规+抹+卦+坷+坯+拓+拢+拔+坪+拣+坦+担+坤+押+抽+拐+拖 ..."
                    }
                ]
            }
        ],
        "career_data": {},
        "tianyan_check_data": []
    }
])

# # --- 4. 统一调度中心 (已重命名和扩展) ---
# class DataOrchestrator:
#     def __init__(self):
#         self.content_scrapers: Dict[str, ContentScraper] = {
#             "searchapi": SearchApiScraper(), "firecrawl": FirecrawlScraper(),
#             "jina": JinaScraper(), "tavily": TavilyScraper(),
#         }
#         self.job_scraper = ZhiLianJobScraper()
#         self.enterprise_scraper = TianyanEnterpriseScraper()
#
#     # async def process_all(self, url_list: List[Dict[str, str]], career_payload: Dict) -> Dict[str, Any]:
#     # ssl_context = httpx.create_ssl_context(verify=False)
#     # async with httpx.AsyncClient(http2=True, verify=ssl_context, timeout=30, follow_redirects=True,
#     #                              limits=httpx.Limits(max_connections=50)) as client:
#     #     # 创建两组任务
#     #     content_tasks = []
#     #     for item in url_list:
#     #         scraper = self.content_scrapers.get(item.get("provider"))
#     #         if scraper: content_tasks.append(scraper.scrape(item["url"], item["title"], client))
#
#     #     job_task = self.job_scraper.scrape_jobs(career_payload, client)
#
#     #     # 并发执行所有任务
#     #     results = await asyncio.gather(*content_tasks, job_task, return_exceptions=True)
#
#     #     # 分离结果
#     #     content_results = results[:-1]
#     #     job_result = results[-1]
#
#     #     return {"content_results": content_results, "job_result": job_result}
#     async def process_all(self, web_url_info_list: List[Dict[str, Any]], career_payload: Dict, enterprise_name: str) -> \
#             Dict[str, Any]:
#         ssl_context = httpx.create_ssl_context(verify=False)
#         async with httpx.AsyncClient(http2=True, verify=ssl_context, timeout=30, follow_redirects=True,
#                                      limits=httpx.Limits(max_connections=50)) as client:
#             content_tasks = []
#
#             # 【修改】从 web_url_info_list 创建抓取任务
#             for item in web_url_info_list:
#                 scraper = self.content_scrapers.get(item.get("provider"))
#                 # 默认使用 SearchApiScraper 作为备选
#                 if not scraper: scraper = self.content_scrapers["searchapi"]
#                 content_tasks.append(scraper.scrape(item, client))
#
#             job_task = self.job_scraper.scrape_jobs(career_payload, client)
#             enterprise_task = self.enterprise_scraper.scrape_enterprise(enterprise_name, client)
#
#             all_tasks = content_tasks + [job_task, enterprise_task]
#             results = await asyncio.gather(*all_tasks, return_exceptions=True)
#
#             content_results = results[:len(content_tasks)]
#             job_result = results[len(content_tasks)]
#             enterprise_result = results[len(content_tasks) + 1]
#             return {"content_results": content_results, "job_result": job_result,
#                     "enterprise_result": enterprise_result}
#
#
# # --- 5. Dify 节点主入口 ---
# async def main_async(raw_input: Any) -> Dict[str, Any]:
#     # 1. 解析输入
#     parsed_data = _parse_input_data(raw_input)
#     # 【修改】获取分离后的网页和视频列表
#     web_url_info_list = parsed_data["web_url_info_list"]
#     video_url_info_list = parsed_data["video_url_info_list"]
#     career_payload = parsed_data["career_payload"]
#     enterprise_name = parsed_data["enterprise_name"]
#
#     if not web_url_info_list and not video_url_info_list and not career_payload.get("keywords") and not enterprise_name:
#         print("🟡 所有输入均为空，提前返回。")
#         return {"scraped_datas": {}, "scraped_datas_str": "{}"}
#
#     # 2. 运行调度器 (只抓取网页内容)
#     orchestrator = DataOrchestrator()
#     results = await orchestrator.process_all(web_url_info_list, career_payload, enterprise_name)
#
#     # 3. 【修改】格式化网页内容输出，构建 all_source_list
#     all_source_list = []
#     for result in results["content_results"]:
#         if isinstance(result, Exception): continue
#         if result.get("status") == "success":  # 即使 content 为空也保留，以便下游判断
#             sanitized_url = re.sub(r'[^a-zA-Z0-9]', '-',
#                                    result.get("url", "").replace("https://", "").replace("http://", ""))
#             all_source_list.append({
#                 "type": "web",
#                 "source_id": f"web-{sanitized_url[:100]}",
#                 "url": result.get("url"),
#                 "title": result.get("title"),
#                 "source": result.get("source"),
#                 "snippet": result.get("snippet"),
#                 "query": result.get("query"),
#                 "content": result.get("content", "")  # 确保有 content 字段
#             })
#
#     # 【修改】格式化视频内容输出，构建 all_video_list
#     all_video_list = []
#     for video_item in video_url_info_list:
#         all_video_list.append({
#             "type": "video",
#             "url": video_item.get("url"),
#             "title": video_item.get("title"),
#             "source": video_item.get("source"),
#             "snippet": video_item.get("snippet"),
#             "video_id": video_item.get("video_id"),
#             "embed_url": video_item.get("embed_url"),
#             "thumbnail_url": video_item.get("thumbnail_url"),
#             "query": video_item.get("query")
#         })
#
#     # 4. 格式化招聘信息输出
#     career_postings = results["job_result"]
#     if isinstance(career_postings, Exception): career_postings = {"status": "failed", "data": [],
#                                                                   "message": f"任务异常: {career_postings}"}
#
#     # 5. 格式化企业信息输出
#     enterprise_info = results["enterprise_result"]
#     if isinstance(enterprise_info, Exception): enterprise_info = {"status": "failed", "data": None,
#                                                                   "message": f"任务异常: {enterprise_info}"}
#
#     # 6. 【修改】组装最终输出以符合新的数据结构
#     comprehensive_data_output = {
#         "all_source_list": all_source_list,
#         "all_video_list": all_video_list
#     }
#
#     final_output = {
#         "scraped_datas": {
#             "comprehensive_data": comprehensive_data_output,  # 修改此处的键和值
#             "career_postings": career_postings,
#             "enterprise_info": enterprise_info
#         }
#     }
#
#     return {
#         "scraped_datas": final_output["scraped_datas"],
#         "scraped_datas_str": json.dumps(final_output, ensure_ascii=False, indent=2)
#     }
#
#
# def main(datas_input: Any) -> Dict[str, Any]:
#     try:
#         return asyncio.run(main_async(raw_input=datas_input))
#     except Exception as e:
#         print(f"‼️ 节点执行时发生顶层错误: {e}")
#         # 【修改】错误负载以匹配新的 comprehensive_data 结构
#         error_payload = {
#             "comprehensive_data": {
#                 "all_source_list": [
#                     {"type": "web", "source_id": "NODE_EXECUTION_ERROR", "title": "节点执行失败", "url": "",
#                      "content": f"An error occurred: {str(e)}\n\n{traceback.format_exc()}"}],
#                 "all_video_list": []
#             },
#             "career_postings": {"status": "failed", "message": "节点执行失败", "data": []},
#             "enterprise_info": {"status": "failed", "message": "节点执行失败", "data": None}
#         }
#         return {
#             "scraped_datas": error_payload,
#             "scraped_datas_str": json.dumps({"scraped_datas": error_payload}, ensure_ascii=False, indent=2)
#         }

# async def main_async(raw_input: Any) -> Dict[str, Any]:
#     # 1. 解析输入
#     parsed_data = _parse_input_data(raw_input)
#     url_list = parsed_data["url_list"]
#     career_payload = parsed_data["career_payload"]
#     if not url_list and not career_payload.get("keywords"):
#         print("🟡 输入中没有有效的URL或招聘查询，提前返回。")
#         return {"scraped_datas": {}, "scraped_datas_str": "{}"}
#     enterprise_name = parsed_data["enterprise_name"]
#     # 2. 运行调度器
#     orchestrator = DataOrchestrator()
#     results = await orchestrator.process_all(url_list, career_payload, enterprise_name)

#     # 3. 格式化网页内容输出
#     comprehensive_content = []
#     for result in results["content_results"]:
#         if isinstance(result, Exception): continue
#         if result.get("status") == "success" and result.get("content"):
#             sanitized_url = re.sub(r'[^a-zA-Z0-9]', '-', result["url"].replace("https://", "").replace("http://", ""))
#             comprehensive_content.append({
#                 "source_id": f"web-{sanitized_url[:100]}", "source_name": result["title"],
#                 "url": result["url"], "content": result["content"]
#             })
#     # 4. 格式化招聘信息输出
#     career_postings = results["job_result"]
#     if isinstance(career_postings, Exception):
#         career_postings = {"status": "failed", "data": [], "message": f"任务异常: {career_postings}"}
#     # 5. 组装最终输出
#     final_output = {
#         "scraped_datas": {
#             "comprehensive_content": comprehensive_content,
#             "career_postings": career_postings
#         }
#     }
#     return {
#         "scraped_datas": final_output["scraped_datas"],
#         "scraped_datas_str": json.dumps(final_output, ensure_ascii=False, indent=2)
#     }

# def main(datas_input: Any) -> Dict[str, Any]:
#     try:
#         return asyncio.run(main_async(raw_input=datas_input))
#     except Exception as e:
#         print(f"‼️ 节点执行时发生顶层错误: {e}")
#         error_payload = {
#             "comprehensive_content": [{
#                 "source_id": "NODE_EXECUTION_ERROR", "source_name": "节点执行失败", "url": "",
#                 "content": f"An error occurred: {str(e)}\n\n{traceback.format_exc()}"
#             }],
#             "career_postings": {"status": "failed", "message": "节点执行失败", "data": []}
#         }
#         return {
#             "scraped_datas": error_payload,
#             "scraped_datas_str": json.dumps({"scraped_datas": error_payload}, ensure_ascii=False, indent=2)

# # Dify 依赖管理: 请确保已添加 httpx, json-repair, trafilatura, pypdf2, beautifulsoup4, lxml
# import asyncio
# import httpx
# import re
# import os
# import json
# import time
# import traceback
# from typing import Any, Dict, List, Literal, Optional
# from abc import ABC, abstractmethod
# from io import BytesIO
# from urllib.parse import urljoin

# # --- 核心依赖 ---
# # trafilatura 用于从HTML提取主要内容
# import trafilatura
# from trafilatura.settings import use_config

# # PyPDF2 用于解析PDF
# from PyPDF2 import PdfReader

# # BeautifulSoup 用于辅助解析HTML（例如提取视频）
# from bs4 import BeautifulSoup

# # --- 1. 输入解析模块 ---
# # 【已修复】替换这个函数
# def _parse_input_data(raw_input: Any) -> Dict[str, Any]:
#     """
#     健壮地解析上一个节点的输出，能同时处理带 "datas" 包装和不带包装的两种结构。
#     """
#     print(f"============== 步骤 1: 接收到原始输入 ==============\nTYPE: {type(raw_input)}\nVALUE: {raw_input}\n=======================================================")
#     if isinstance(raw_input, str):
#         if not raw_input.strip(): return {"url_list": [], "career_payload": {}, "enterprise_name": ""}
#         try:
#             data = json.loads(raw_input)
#         except json.JSONDecodeError as e:
#             raise ValueError(f"无法将输入字符串解析为JSON: {e}")
#     elif isinstance(raw_input, dict):
#         data = raw_input
#     else:
#         raise TypeError(f"期望的输入类型是 str 或 dict, 但收到了 {type(raw_input).__name__}")

#     # --- 核心修复逻辑 ---
#     # 检查顶层是否有 "datas" 键，如果没有，就认为当前整个对象就是我们要的数据体。
#     if "datas" in data and isinstance(data["datas"], dict):
#         print("  [解析器] 检测到 'datas' 包装层，将使用其内部数据。")
#         datas_obj = data["datas"]
#     else:
#         print("  [解析器] 未检测到 'datas' 包装层，将直接使用顶层数据。")
#         datas_obj = data
#     # --- 修复结束 ---

#     if not isinstance(datas_obj, dict): datas_obj = {}

#     comprehensive_data = datas_obj.get("comprehensive_data", [])
#     career_data = datas_obj.get("career_data", {})
#     tianyan_data = datas_obj.get("tianyan_check_data", "")

#     url_list = []
#     if isinstance(comprehensive_data, list):
#         for query_result in comprehensive_data:
#             if not isinstance(query_result, dict): continue
#             for res_list_key in ["web_results", "video_results"]:
#                 for res in query_result.get(res_list_key, []):
#                     if not isinstance(res, dict): continue
#                     url, title, provider = None, None, None
#                     for key, value in res.items():
#                         if key.endswith("_url"):
#                             url, provider = value, key.split('_url')[0]
#                             title = res.get(f"{provider}_title", "Untitled")
#                             break
#                     if url and provider:
#                         url_list.append({"url": url, "title": title, "provider": provider})
#     career_payload = career_data if isinstance(career_data, dict) else {}
#     enterprise_name = tianyan_data if isinstance(tianyan_data, str) else ""
#     parsed_result = {"url_list": url_list, "career_payload": career_payload, "enterprise_name": enterprise_name.strip()}

#     print(f"============== 步骤 2: 输入解析完毕 ==============\nURL 数量: {len(url_list)}\n招聘负载: {career_payload}\n企业名称: '{enterprise_name.strip()}'\n=======================================================")

#     return parsed_result

# # def _parse_input_data(raw_input: Any) -> Dict[str, Any]:
# #     """
# #     健壮地解析上一个节点的输出，分离出web搜索URL和招聘查询参数。
# #     """
# #     if isinstance(raw_input, str):
# #         if not raw_input.strip(): return {"url_list": [], "career_payload": {}}
# #         try:
# #             data = json.loads(raw_input)
# #         except json.JSONDecodeError as e:
# #             raise ValueError(f"无法将输入字符串解析为JSON: {e}")
# #     elif isinstance(raw_input, dict):
# #         data = raw_input
# #     else:
# #         raise TypeError(f"期望的输入类型是 str 或 dict, 但收到了 {type(raw_input).__name__}")
# #     # 安全地深入到 'datas' 结构
# #     datas_obj = data.get("datas", {})
# #     if not isinstance(datas_obj, dict): datas_obj = {}
# #     comprehensive_data = datas_obj.get("comprehensive_data", [])
# #     career_data = datas_obj.get("career_data", {})
# #     tianyan_data = datas_obj.get("tianyan_check_data", "")
# #     # 1. 提取URL列表
# #     url_list = []
# #     if isinstance(comprehensive_data, list):
# #         for query_result in comprehensive_data:
# #             if not isinstance(query_result, dict): continue
# #             for res_list_key in ["web_results", "video_results"]:
# #                 for res in query_result.get(res_list_key, []):
# #                     if not isinstance(res, dict): continue
# #                     url, title, provider = None, None, None
# #                     for key, value in res.items():
# #                         if key.endswith("_url"):
# #                             url = value
# #                             provider = key.split('_url')[0]
# #                             title = res.get(f"{provider}_title", "Untitled")
# #                             break
# #                     if url and provider:
# #                         url_list.append({"url": url, "title": title, "provider": provider})
# #     # 2. 提取职业查询负载
# #     career_payload = career_data if isinstance(career_data, dict) else {}

# #     # 3. 提取企业名称
# #     enterprise_name = tianyan_data if isinstance(tianyan_data, str) else ""
# #     return {"url_list": url_list, "career_payload": career_payload, "enterprise_name": enterprise_name.strip()}

# # --- 2. 抽象与实现分离：内容抓取器 ---
# class ContentScraper(ABC):
#     """内容抓取器的抽象基类。"""

#     @abstractmethod
#     async def scrape(self, url: str, title: str, client: httpx.AsyncClient) -> Dict[str, Any]:
#         """
#         抓取单个URL的内容。
#         成功时返回: {"url": str, "title": str, "content": str, "status": "success"}
#         失败时返回: {"url": str, "title": str, "content": "", "status": "failed", "error_message": str}
#         """
#         pass

# # --- 2.1 SearchAPI.io 的手动抓取与清洗实现 ---
# class SearchApiScraper(ContentScraper):
#     def __init__(self):
#         self.trafilatura_config = use_config()
#         self.trafilatura_config.set("DEFAULT", "EXTRACTION_TIMEOUT", "10")

#         # 编译常用的正则表达式以提高性能
#         self.NOISY_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
#             r'^\s*$', r'^[\-=*#_]{3,}$', r'.*\.(html|shtml|htm|php)\s*$',
#             r'.{0,50}(搜狐|网易|腾讯|新浪|登录|注册|版权所有|版权声明).{0,50}$',
#             r'\[\d+\]|\[下一页\]|\[上一页\]', r'\[(编辑|查看历史|讨论|阅读|来源|原标题)\]',
#             r'^\*+\s*\[.*?\]\(.*?\)',
#             r'^\s*(分享到|扫描二维码|返回搜狐|查看更多|责任编辑|记者|通讯员)',
#             r'^\s*([京公网安备京网文京ICP备]|互联网新闻信息服务许可证|信息网络传播视听节目许可证)',
#         ]]
#         self.IMG_PATTERN = re.compile(r'(!\[(.*?)\]\((.*?)\))')
#         self.LINK_PATTERN = re.compile(r'\[.*?\]\(.*?\)')
#         self.EDITOR_PATTERN = re.compile(r'(\(|\[)\s*责任编辑：.*?\s*(\)|\])')

#     # --- 2.1.1 内容提取工具 (来自您的代码) ---
#     def _extract_pdf_text(self, binary_content: bytes) -> str:
#         try:
#             reader = PdfReader(BytesIO(binary_content))
#             return "\n".join(page.extract_text() or "" for page in reader.pages)
#         except Exception as e:
#             print(f"⚠️ PDF 解析失败: {e}")
#             return ""

#     def _parse_videos_from_html(self, html: str, base_url: str) -> List[str]:
#         try:
#             soup = BeautifulSoup(html, "lxml")
#             videos = []
#             for video in soup.find_all("video"):
#                 src = video.get("src")
#                 if src: videos.append(urljoin(base_url, src))
#                 for source in video.find_all("source"):
#                     src = source.get("src")
#                     if src: videos.append(urljoin(base_url, src))
#             for iframe in soup.find_all("iframe"):
#                 src = iframe.get("src")
#                 if src and any(k in src for k in ["youtube", "vimeo", "embed", ".mp4"]):
#                     videos.append(urljoin(base_url, src))
#             return list(dict.fromkeys(videos))  # 去重并保持顺序
#         except Exception as e:
#             print(f"⚠️ 视频解析失败: {e}")
#             return []

#     # --- 2.1.2 内容清洗工具 (来自您的代码，已优化和异步化) ---
#     async def _is_valid_image_url_async(self, url: str, client: httpx.AsyncClient) -> bool:
#         if not url or not url.startswith(('http://', 'https://')): return False
#         try:
#             resp = await client.head(url, timeout=5, follow_redirects=True)
#             content_type = resp.headers.get('content-type', '').lower()
#             return resp.is_success and 'image' in content_type
#         except httpx.RequestError:
#             return False

#     async def _remove_invalid_images_async(self, md: str, client: httpx.AsyncClient) -> str:
#         MAX_IMAGES_TO_VALIDATE = 25
#         matches = list(self.IMG_PATTERN.finditer(md))
#         urls_to_check_all = {m.group(3).strip() for m in matches}

#         urls_to_check = set(list(urls_to_check_all)[:MAX_IMAGES_TO_VALIDATE])
#         if len(urls_to_check_all) > MAX_IMAGES_TO_VALIDATE:
#             print(f"⚠️ 图片数量过多 ({len(urls_to_check_all)}), 只验证前 {MAX_IMAGES_TO_VALIDATE} 张。")

#         tasks = {url: self._is_valid_image_url_async(url, client) for url in urls_to_check}
#         results = await asyncio.gather(*tasks.values(), return_exceptions=True)

#         url_status = dict(zip(tasks.keys(), results))
#         valid_urls = {url for url, res in url_status.items() if isinstance(res, bool) and res}
#         valid_urls.update(urls_to_check_all - urls_to_check)  # 未检查的默认有效

#         def replacer(match: re.Match):
#             return match.group(0) if match.group(3).strip() in valid_urls else ""

#         return self.IMG_PATTERN.sub(replacer, md)

#     def _is_noisy_line(self, line: str) -> bool:
#         stripped = line.strip()
#         for pat in self.NOISY_PATTERNS:
#             if pat.search(stripped): return True
#         links = self.LINK_PATTERN.findall(stripped)
#         if len(links) > 2 and len(stripped) / (len(links) + 1) < 30: return True
#         return False

#     async def _clean_content_async(self, text: str, client: httpx.AsyncClient) -> str:
#         if not text: return ""
#         text = await self._remove_invalid_images_async(text, client)

#         lines = text.splitlines()
#         cleaned_lines = []
#         for line in lines:
#             if not self._is_noisy_line(line):
#                 line = self.EDITOR_PATTERN.sub('', line).strip()
#                 if line: cleaned_lines.append(line)

#         # 去除连续空行
#         out = []
#         for i, line in enumerate(cleaned_lines):
#             if i > 0 and not line.strip() and not cleaned_lines[i - 1].strip():
#                 continue
#             out.append(line)

#         return "\n".join(out).strip()

#     # --- 2.1.3 主抓取函数 (来自您的代码，封装为scrape方法) ---
#     async def scrape(self, url: str, title: str, client: httpx.AsyncClient) -> dict:
#         print(f"🕸️ [SearchAPI Scraper] 开始处理: {url}")
#         try:
#             headers = {
#                 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
#             response = await client.get(url, timeout=20, headers=headers, follow_redirects=True)
#             response.raise_for_status()

#             final_url = str(response.url)
#             content_type = response.headers.get('content-type', '').lower()

#             raw_content, html_for_video_parsing = "", ""

#             if 'pdf' in content_type or final_url.lower().endswith(".pdf"):
#                 print(f"  📄 [SearchAPI Scraper] 检测到 PDF: {final_url}")
#                 pdf_bytes = await response.aread()
#                 raw_content = await asyncio.to_thread(self._extract_pdf_text, pdf_bytes)
#             else:
#                 print(f"  📑 [SearchAPI Scraper] 检测到 HTML: {final_url}")
#                 html_content = response.text
#                 html_for_video_parsing = html_content
#                 raw_content = await asyncio.to_thread(
#                     trafilatura.extract, html_content, config=self.trafilatura_config,
#                     output_format='markdown', include_images=True, favor_recall=True)

#             if not raw_content: raise ValueError("trafilatura 内容提取返回为空。")

#             print(f"  🧹 [SearchAPI Scraper] 正在清洗内容: {final_url}")
#             cleaned_content = await self._clean_content_async(raw_content, client)

#             if html_for_video_parsing:
#                 videos = self._parse_videos_from_html(html_for_video_parsing, final_url)
#                 if videos:
#                     video_section = "\n\n## 参考视频:\n" + "\n".join(f"- {vid}" for vid in videos)
#                     cleaned_content += video_section

#             print(f"✅ [SearchAPI Scraper] 成功: {url}")
#             return {"url": final_url, "title": title, "content": cleaned_content, "status": "success"}

#         except Exception as e:
#             error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
#             print(f"⚠️ [SearchAPI Scraper] {error_msg}")
#             return {"url": url, "title": title, "content": "", "status": "failed", "error_message": str(e)}

# # --- 2.2 FirecrawlScraper ---
# class FirecrawlScraper(ContentScraper):
#     def __init__(self):
#         self.api_key = os.environ.get("FIRECRAWL_API_KEY", "fc-a36b7d2fb273485680d0fe6abd686935")
#         if not self.api_key: raise ValueError("未提供 Firecrawl API Key。")
#         self.base_url = "https://api.firecrawl.dev/v2/scrape"
#         self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

#     async def scrape(self, url: str, title: str, client: httpx.AsyncClient) -> dict:
#         print(f"🔥 [Firecrawl Scraper] 开始处理: {url}")
#         try:
#             # **关键修正：将 pageOptions 展平为顶级字段**
#             payload = {
#                 "url": url,
#                 "onlyMainContent": True,
#                 # 也可以在这里添加其他顶级选项，例如：
#                 "removeBase64Images": True,
#                 "blockAds": True
#             }
#             resp = await client.post(self.base_url, headers=self.headers, json=payload, timeout=45)

#             # 增加对4xx/5xx错误的详细日志记录
#             if not resp.is_success:
#                 try:
#                     error_details = resp.json()
#                     raise httpx.HTTPStatusError(f"API返回错误: {error_details}", request=resp.request, response=resp)
#                 except json.JSONDecodeError:
#                     resp.raise_for_status()  # 如果无法解析json，则抛出原始错误

#             data = resp.json()

#             # Firecrawl v2 的成功响应中没有 "success" 键，直接检查 data 字段
#             content_data = data.get("data", {})
#             if content_data is None:  # 可能是 null
#                 raise ValueError("API返回的 'data' 字段为 null。")

#             content = content_data.get("markdown")  # markdown 可能为空字符串，这是正常的
#             if content is None:
#                 raise ValueError("API未返回 'markdown' 字段。")

#             print(f"✅ [Firecrawl Scraper] 成功: {url}")
#             return {"url": url, "title": title, "content": content, "status": "success"}
#         except Exception as e:
#             error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
#             print(f"⚠️ [Firecrawl Scraper] {error_msg}")
#             return {"url": url, "title": title, "content": "", "status": "failed", "error_message": str(e)}

# # --- 2.3 JinaScraper ---
# class JinaScraper(ContentScraper):
#     def __init__(self):
#         self.api_key = os.environ.get("JINA_API_KEY",
#                                       "jina_b4348ffc39ca47bfbe753b95f59428c7i6ifkOFXRPdF3dRa5Rwb6T8FvrLH")
#         if not self.api_key: raise ValueError("未提供 Jina API Key。")
#         self.base_url = "https://r.jina.ai/"
#         self.headers = {
#             "Authorization": f"Bearer {self.api_key}",
#             "Content-Type": "application/json",
#             "Accept": "application/json",
#             "X-Return-Format": "markdown"  # 关键：直接获取Markdown
#         }

#     async def scrape(self, url: str, title: str, client: httpx.AsyncClient) -> dict:
#         print(f"🌀 [Jina Scraper] 开始处理: {url}")
#         try:
#             # Jina的Reader API有时对普通的GET请求更友好
#             target_url = f"{self.base_url}{url}"
#             resp = await client.get(target_url, headers=self.headers, timeout=45)
#             resp.raise_for_status()
#             content = resp.text
#             if not content: raise ValueError("API 返回内容为空。")

#             print(f"✅ [Jina Scraper] 成功: {url}")
#             return {"url": url, "title": title, "content": content, "status": "success"}

#         except Exception as e:
#             error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
#             print(f"⚠️ [Jina Scraper] {error_msg}")
#             return {"url": url, "title": title, "content": "", "status": "failed", "error_message": str(e)}

# # --- 2.4 TavilyScraper ---
# class TavilyScraper(ContentScraper):
#     def __init__(self):
#         self.api_key = os.environ.get("TAVILY_API_KEY", "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")
#         if not self.api_key: raise ValueError("未提供 Tavily API Key。")
#         self.base_url = "https://api.tavily.com/extract"
#         self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

#     async def scrape(self, url: str, title: str, client: httpx.AsyncClient) -> dict:
#         print(f"🤖 [Tavily Scraper] 开始处理: {url}")
#         try:
#             # 注意：Tavily 的Python库没有extract方法，必须直接调用API
#             payload = {"urls": [url], "format": "markdown"}
#             resp = await client.post(self.base_url, json=payload, headers=self.headers, timeout=45)
#             resp.raise_for_status()
#             data = resp.json()

#             if not data.get("results") or not isinstance(data["results"], list):
#                 failed_info = data.get("failed_results", [])
#                 raise ValueError(f"API调用失败: {failed_info}")

#             result = data["results"][0]
#             content = result.get("raw_content")  # 文档显示raw_content，并且format=markdown
#             if not content: raise ValueError("API未返回raw_content内容。")

#             print(f"✅ [Tavily Scraper] 成功: {url}")
#             return {"url": url, "title": title, "content": content, "status": "success"}

#         except Exception as e:
#             error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
#             print(f"⚠️ [Tavily Scraper] {error_msg}")
#             return {"url": url, "title": title, "content": "", "status": "failed", "error_message": str(e)}

# # --- 2.5 ZhiLianJobScraper ---
# class ZhiLianJobScraper:
#     def __init__(self):
#         self.api_url = "http://119.45.167.133:12906/api/scrape/zhilian"
#         self.headers = {'accept': 'application/json', 'Content-Type': 'application/json'}

#     async def scrape_jobs(self, payload: Dict[str, Any], client: httpx.AsyncClient) -> Dict[str, Any]:
#         print(f"💼 [ZhiLian Scraper] 开始使用负载调用API: {json.dumps(payload, ensure_ascii=False)}")
#         if not payload or not payload.get("keywords") or not payload.get("provinces"):
#             msg = "负载无效，缺少 'keywords' 或 'provinces'。"
#             print(f"⚠️ [ZhiLian Scraper] {msg}")
#             return {"status": "skipped", "data": [], "message": msg}
#         try:
#             # 确保 page_size 是整数
#             if 'page_size' in payload: payload['page_size'] = int(payload['page_size'])

#             resp = await client.post(self.api_url, headers=self.headers, json=payload, timeout=60)
#             resp.raise_for_status()
#             response_data = resp.json()
#             if response_data.get("code") == 200:
#                 print(f"✅ [ZhiLian Scraper] 成功: {response_data.get('message')}")
#                 return {"status": "success", "data": response_data.get("data", []),
#                         "message": response_data.get("message")}
#             else:
#                 msg = f"API返回错误码 {response_data.get('code')}: {response_data.get('message')}"
#                 print(f"API returned non-200 code: {msg}")
#                 return {"status": "failed", "data": [], "message": msg}
#         except Exception as e:
#             error_msg = f"API请求失败: {type(e).__name__} - {e}"
#             print(f"⚠️ [ZhiLian Scraper] {error_msg}")
#             return {"status": "failed", "data": [], "message": error_msg}

# class TianyanEnterpriseScraper:
#     def __init__(self):
#         self.api_url = "http://open.api.tianyancha.com/services/open/ic/baseinfo/normal"
#         # 从环境变量或直接硬编码获取Token
#         self.token = os.environ.get("TIANYANCHA_TOKEN", "4d882100-ed23-4c22-a83b-c77af2e4be42")
#         self.headers = {'Authorization': self.token}
#     async def scrape_enterprise(self, name: str, client: httpx.AsyncClient) -> Dict[str, Any]:
#         """根据企业名称查询基本信息。"""
#         print(f"🏢 [Tianyan Scraper] 开始查询企业: {name}")
#         if not name:
#             msg = "企业名称为空，跳过查询。"
#             print(f"🟡 [Tianyan Scraper] {msg}")
#             return {"status": "skipped", "data": None, "message": msg}
#         try:
#             params = {"keyword": name}
#             resp = await client.get(self.api_url, headers=self.headers, params=params, timeout=30)
#             resp.raise_for_status()
#             response_data = resp.json()
#             if response_data.get("error_code") == 0:
#                 print(f"✅ [Tianyan Scraper] 成功查询到: {name}")
#                 return {"status": "success", "data": response_data.get("result"), "message": response_data.get("reason")}
#             else:
#                 msg = f"API返回错误码 {response_data.get('error_code')}: {response_data.get('reason')}"
#                 print(f"⚠️ [Tianyan Scraper] {msg}")
#                 return {"status": "failed", "data": None, "message": msg}
#         except Exception as e:
#             error_msg = f"API请求失败: {type(e).__name__} - {e}"
#             print(f"⚠️ [Tianyan Scraper] {error_msg}")
#             return {"status": "failed", "data": None, "message": error_msg}

# # --- 4. 统一调度中心 (已重命名和扩展) ---
# class DataOrchestrator:
#     def __init__(self):
#         self.content_scrapers: Dict[str, ContentScraper] = {
#             "searchapi": SearchApiScraper(), "firecrawl": FirecrawlScraper(),
#             "jina": JinaScraper(), "tavily": TavilyScraper(),
#         }
#         self.job_scraper = ZhiLianJobScraper()
#         self.enterprise_scraper = TianyanEnterpriseScraper()
#     # async def process_all(self, url_list: List[Dict[str, str]], career_payload: Dict) -> Dict[str, Any]:
#         # ssl_context = httpx.create_ssl_context(verify=False)
#         # async with httpx.AsyncClient(http2=True, verify=ssl_context, timeout=30, follow_redirects=True,
#         #                              limits=httpx.Limits(max_connections=50)) as client:
#         #     # 创建两组任务
#         #     content_tasks = []
#         #     for item in url_list:
#         #         scraper = self.content_scrapers.get(item.get("provider"))
#         #         if scraper: content_tasks.append(scraper.scrape(item["url"], item["title"], client))

#         #     job_task = self.job_scraper.scrape_jobs(career_payload, client)

#         #     # 并发执行所有任务
#         #     results = await asyncio.gather(*content_tasks, job_task, return_exceptions=True)

#         #     # 分离结果
#         #     content_results = results[:-1]
#         #     job_result = results[-1]

#         #     return {"content_results": content_results, "job_result": job_result}
#     async def process_all(self, url_list: List[Dict[str, str]], career_payload: Dict, enterprise_name: str) -> Dict[str, Any]:
#         ssl_context = httpx.create_ssl_context(verify=False)
#         async with httpx.AsyncClient(http2=True, verify=ssl_context, timeout=30, follow_redirects=True, limits=httpx.Limits(max_connections=50)) as client:
#             # 创建三组任务
#             content_tasks, job_task, enterprise_task = [], None, None

#             for item in url_list:
#                 scraper = self.content_scrapers.get(item.get("provider"))
#                 if scraper: content_tasks.append(scraper.scrape(item["url"], item["title"], client))

#             job_task = self.job_scraper.scrape_jobs(career_payload, client)
#             enterprise_task = self.enterprise_scraper.scrape_enterprise(enterprise_name, client) # 【新增】

#             # 并发执行所有任务
#             all_tasks = content_tasks + [job_task, enterprise_task]
#             results = await asyncio.gather(*all_tasks, return_exceptions=True)

#             # 分离结果
#             content_results = results[:len(content_tasks)]
#             job_result = results[len(content_tasks)]
#             enterprise_result = results[len(content_tasks) + 1] # 【新增】
#             return {"content_results": content_results, "job_result": job_result, "enterprise_result": enterprise_result}

# # --- 5. Dify 节点主入口 ---
# async def main_async(raw_input: Any) -> Dict[str, Any]:
#     # 1. 解析输入
#     parsed_data = _parse_input_data(raw_input)
#     url_list = parsed_data["url_list"]
#     career_payload = parsed_data["career_payload"]
#     enterprise_name = parsed_data["enterprise_name"] # 【新增】

#     if not url_list and not career_payload.get("keywords") and not enterprise_name:
#         print("🟡 所有输入均为空，提前返回。")
#         return {"scraped_datas": {}, "scraped_datas_str": "{}"}
#     # 2. 运行调度器
#     orchestrator = DataOrchestrator()
#     results = await orchestrator.process_all(url_list, career_payload, enterprise_name)
#     # 3. 格式化网页内容输出
#     comprehensive_content = []
#     for result in results["content_results"]:
#         if isinstance(result, Exception): continue
#         if result.get("status") == "success" and result.get("content"):
#             sanitized_url = re.sub(r'[^a-zA-Z0-9]', '-', result["url"].replace("https://", "").replace("http://", ""))
#             comprehensive_content.append({"source_id": f"web-{sanitized_url[:100]}", "source_name": result["title"], "url": result["url"], "content": result["content"]})
#     # 4. 格式化招聘信息输出
#     career_postings = results["job_result"]
#     if isinstance(career_postings, Exception): career_postings = {"status": "failed", "data": [], "message": f"任务异常: {career_postings}"}
#     # 5. 【新增】格式化企业信息输出
#     enterprise_info = results["enterprise_result"]
#     if isinstance(enterprise_info, Exception): enterprise_info = {"status": "failed", "data": None, "message": f"任务异常: {enterprise_info}"}
#     # 6. 【调整】组装最终输出
#     final_output = {
#         "scraped_datas": {
#             "comprehensive_content": comprehensive_content,
#             "career_postings": career_postings,
#             "enterprise_info": enterprise_info
#         }
#     }
#     return {
#         "scraped_datas": final_output["scraped_datas"],
#         "scraped_datas_str": json.dumps(final_output, ensure_ascii=False, indent=2)
#     }
# # 【调整】main 函数
# def main(datas_input: Any) -> Dict[str, Any]:
#     try:
#         return asyncio.run(main_async(raw_input=datas_input))
#     except Exception as e:
#         print(f"‼️ 节点执行时发生顶层错误: {e}")
#         error_payload = {
#             "comprehensive_content": [{"source_id": "NODE_EXECUTION_ERROR", "source_name": "节点执行失败", "url": "", "content": f"An error occurred: {str(e)}\n\n{traceback.format_exc()}"}],
#             "career_postings": {"status": "failed", "message": "节点执行失败", "data": []},
#             "enterprise_info": {"status": "failed", "message": "节点执行失败", "data": None}
#         }
#         return {
#             "scraped_datas": error_payload,
#             "scraped_datas_str": json.dumps({"scraped_datas": error_payload}, ensure_ascii=False, indent=2)
#         }

# # async def main_async(raw_input: Any) -> Dict[str, Any]:
# #     # 1. 解析输入
# #     parsed_data = _parse_input_data(raw_input)
# #     url_list = parsed_data["url_list"]
# #     career_payload = parsed_data["career_payload"]
# #     if not url_list and not career_payload.get("keywords"):
# #         print("🟡 输入中没有有效的URL或招聘查询，提前返回。")
# #         return {"scraped_datas": {}, "scraped_datas_str": "{}"}
# #     enterprise_name = parsed_data["enterprise_name"]
# #     # 2. 运行调度器
# #     orchestrator = DataOrchestrator()
# #     results = await orchestrator.process_all(url_list, career_payload, enterprise_name)

# #     # 3. 格式化网页内容输出
# #     comprehensive_content = []
# #     for result in results["content_results"]:
# #         if isinstance(result, Exception): continue
# #         if result.get("status") == "success" and result.get("content"):
# #             sanitized_url = re.sub(r'[^a-zA-Z0-9]', '-', result["url"].replace("https://", "").replace("http://", ""))
# #             comprehensive_content.append({
# #                 "source_id": f"web-{sanitized_url[:100]}", "source_name": result["title"],
# #                 "url": result["url"], "content": result["content"]
# #             })
# #     # 4. 格式化招聘信息输出
# #     career_postings = results["job_result"]
# #     if isinstance(career_postings, Exception):
# #         career_postings = {"status": "failed", "data": [], "message": f"任务异常: {career_postings}"}
# #     # 5. 组装最终输出
# #     final_output = {
# #         "scraped_datas": {
# #             "comprehensive_content": comprehensive_content,
# #             "career_postings": career_postings
# #         }
# #     }
# #     return {
# #         "scraped_datas": final_output["scraped_datas"],
# #         "scraped_datas_str": json.dumps(final_output, ensure_ascii=False, indent=2)
# #     }

# # def main(datas_input: Any) -> Dict[str, Any]:
# #     try:
# #         return asyncio.run(main_async(raw_input=datas_input))
# #     except Exception as e:
# #         print(f"‼️ 节点执行时发生顶层错误: {e}")
# #         error_payload = {
# #             "comprehensive_content": [{
# #                 "source_id": "NODE_EXECUTION_ERROR", "source_name": "节点执行失败", "url": "",
# #                 "content": f"An error occurred: {str(e)}\n\n{traceback.format_exc()}"
# #             }],
# #             "career_postings": {"status": "failed", "message": "节点执行失败", "data": []}
# #         }
# #         return {
# #             "scraped_datas": error_payload,
# #             "scraped_datas_str": json.dumps({"scraped_datas": error_payload}, ensure_ascii=False, indent=2)
