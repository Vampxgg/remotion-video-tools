# Dify 依赖管理: 请确保已添加 httpx, json-repair, trafilatura, pypdf2, beautifulsoup4, lxml
import asyncio
import httpx
import re
import os
import json
import time
import traceback
from typing import Any, Dict, List, Literal, Optional
from abc import ABC, abstractmethod
from io import BytesIO
from urllib.parse import urljoin
import tempfile
try:
    from markitdown import MarkItDown
except ImportError as e:
    print(f"‼️ MarkItDown Import Error: {e}")
    MarkItDown = None
except Exception as e:
    print(f"‼️ MarkItDown Unexpected Error: {e}")
    MarkItDown = None


# --- 核心依赖 ---
# trafilatura 用于从HTML提取主要内容
import trafilatura
from trafilatura.settings import use_config

# PyPDF2 用于解析PDF
import pdfplumber
import fitz
# BeautifulSoup 用于辅助解析HTML（例如提取视频）
from bs4 import BeautifulSoup

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


def _parse_input_data(raw_input: Any) -> Dict[str, Any]:
    """
    健壮地解析上一个节点的输出，能同时处理带 "datas" 包装和不带包装的两种结构。
    并分离出网页搜索URL、视频URL、招聘查询参数和企业名称，同时保留元数据。
    """
    print(
        f"============== 步骤 1: 接收到原始输入 ==============\nTYPE: {type(raw_input)}\nVALUE: {raw_input}\n=======================================================")
    if isinstance(raw_input, str):
        if not raw_input.strip(): return {"web_url_info_list": [], "video_url_info_list": [], "career_payload": {},
                                          "enterprise_names": []}
        try:
            data = json.loads(raw_input)
        except json.JSONDecodeError as e:
            raise ValueError(f"无法将输入字符串解析为JSON: {e}")
    elif isinstance(raw_input, dict):
        data = raw_input
    else:
        raise TypeError(f"期望的输入类型是 str 或 dict, 但收到了 {type(raw_input).__name__}")

    if "datas" in data and isinstance(data["datas"], dict):
        print("  [解析器] 检测到 'datas' 包装层，将使用其内部数据。")
        datas_obj = data["datas"]
    else:
        print("  [解析器] 未检测到 'datas' 包装层，将直接使用顶层数据。")
        datas_obj = data

    if not isinstance(datas_obj, dict): datas_obj = {}

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

                    # 【【修复】】 provider 提取逻辑，使其更精确，避免被 _embed_url 等键干扰。
                    # 我们寻找的键必须是单纯的 "xxx_url"，而不是 "xxx_embed_url"。
                    provider = next(
                        (k[:-4] for k in res if k.endswith('_url') and '_embed_' not in k and '_thumbnail_' not in k),
                        None)
                    if not provider:
                        # 如果找不到，做一个备选方案，以防万一
                        provider = next((k.split('_')[0] for k in res if '_url' in k), None)
                        if not provider: continue

                    result_type = res.get(f"{provider}_type")
                    # 分类逻辑保持不变，但现在它的输入 (result_type) 是正确的了
                    if result_type == 'video':
                        info = {
                            "url": res.get(f"{provider}_url"), "title": res.get(f"{provider}_title", "Untitled"),
                            "source": res.get(f"{provider}_source"), "snippet": res.get(f"{provider}_snippet"),
                            "provider": provider, "query": query_text,
                            "video_id": res.get(f"{provider}_video_id"), "embed_url": res.get(f"{provider}_embed_url"),
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
        print("  [解析器] 检测到 tianyan_check_data 为字符串，将处理单个企业。")
        enterprise_names.append(tianyan_data.strip())
    elif isinstance(tianyan_data, list):
        print(f"  [解析器] 检测到 tianyan_check_data 为列表，将处理 {len(tianyan_data)} 个企业。")
        enterprise_names = [str(name).strip() for name in tianyan_data if isinstance(name, str) and str(name).strip()]
    parsed_result = {
        "web_url_info_list": web_url_info_list,
        "video_url_info_list": video_url_info_list,
        "career_payload": career_payload,
        "enterprise_names": enterprise_names
    }

    print(
        f"============== 步骤 2: 输入解析完毕 ==============\n网页URL数量: {len(web_url_info_list)}\n视频URL数量: {len(video_url_info_list)}\n招聘负载: {career_payload}\n企业名称列表: {enterprise_names} (共 {len(enterprise_names)} 个)\n=======================================================")

    return parsed_result


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

class ResourceParser:
    """
    Unified resource parser using MarkItDown to handle various document formats
    (PDF, DOCX, PPTX, XLSX, CSV, etc.) and convert them to Markdown.
    """
    def __init__(self):
        if MarkItDown:
            try:
                self.md = MarkItDown()
            except Exception as e:
                print(f"⚠️ Failed to initialize MarkItDown instance: {e}")
                print(traceback.format_exc())
                self.md = None
        else:
            print("⚠️ MarkItDown package not found.")
            self.md = None

    def parse(self, binary_content: bytes, file_extension: str) -> str:
        """
        Parse binary content based on file extension.
        """
        if not self.md:
            return "ResourceParser is not available (MarkItDown not initialized)."

        # MarkItDown typically requires a file path to auto-detect specific formats correctly
        # We will create a temporary file with the correct extension
        suffix = file_extension if file_extension.startswith(".") else f".{file_extension}"
        
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
                tmp_file.write(binary_content)
                tmp_path = tmp_file.name
            
            # Perform conversion
            result = self.md.convert(tmp_path)
            if result and result.text_content:
                return result.text_content
            return ""
        except Exception as e:
            print(f"⚠️ MarkItDown parsing failed for {suffix} file: {e}")
            return f"Error parsing document: {e}"
        finally:
            # Clean up the temporary file
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

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
        self.resource_parser = ResourceParser()

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
        MAX_IMAGES_TO_VALIDATE = 25
        matches = list(self.IMG_PATTERN.finditer(md))
        urls_to_check_all = {m.group(3).strip() for m in matches}

        urls_to_check = set(list(urls_to_check_all)[:MAX_IMAGES_TO_VALIDATE])
        if len(urls_to_check_all) > MAX_IMAGES_TO_VALIDATE:
            print(f"⚠️ 图片数量过多 ({len(urls_to_check_all)}), 只验证前 {MAX_IMAGES_TO_VALIDATE} 张。")

        tasks = {url: self._is_valid_image_url_async(url, client) for url in urls_to_check}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        url_status = dict(zip(tasks.keys(), results))
        valid_urls = {url for url, res in url_status.items() if isinstance(res, bool) and res}
        valid_urls.update(urls_to_check_all - urls_to_check)  # 未检查的默认有效

        def replacer(match: re.Match):
            return match.group(0) if match.group(3).strip() in valid_urls else ""

        return self.IMG_PATTERN.sub(replacer, md)

    def _is_noisy_line(self, line: str) -> bool:
        stripped = line.strip()
        for pat in self.NOISY_PATTERNS:
            if pat.search(stripped): return True
        links = self.LINK_PATTERN.findall(stripped)
        if len(links) > 2 and len(stripped) / (len(links) + 1) < 30: return True
        return False

    async def _clean_content_async(self, text: str, client: httpx.AsyncClient) -> str:
        if not text: return ""
        text = await self._remove_invalid_images_async(text, client)

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
            # 1. 检测是否为支持的文档类型
            supported_extensions = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".csv"}
            url_lower = url.lower()
            ext = os.path.splitext(url_lower)[1]
            
            is_document = ext in supported_extensions
            content_type = ""

            # 如果这类已知扩展名，可以直接认定为文档，减少 HEAD 请求（或保留 HEAD 以检测大小）
            # 这里保留 HEAD 请求以获取 Content-Type 和 Content-Length
            async with client.stream("HEAD", url, headers=headers, follow_redirects=True) as head_response:
                content_type = head_response.headers.get('content-type', '').lower()
                content_length = int(head_response.headers.get('content-length', 0))
                
                # Check for document mime types if extension wasn't obvious
                if not is_document:
                    if 'pdf' in content_type: is_document = True; ext = ".pdf"
                    elif 'word' in content_type or 'officedocument' in content_type: is_document = True; ext = ".docx" # Simplification
                    elif 'excel' in content_type or 'spreadsheet' in content_type: is_document = True; ext = ".xlsx"
                    elif 'powerpoint' in content_type or 'presentation' in content_type: is_document = True; ext = ".pptx"
                    elif 'csv' in content_type: is_document = True; ext = ".csv"

                # 【优化】检查文件大小 (限制为 20MB)
                MAX_SIZE = 20 * 1024 * 1024
                if is_document and content_length > MAX_SIZE:
                    raise ValueError(f"文档文件过大 ({content_length / 1024 / 1024:.2f}MB > 20MB)，跳过处理。")

            # 根据类型执行不同逻辑
            raw_content, final_url = "", url

            if is_document:
                print(f"  📄 [SearchAPI Scraper] 检测到文档 ({ext}): {url}")
                try:
                    async def _download_doc():
                        resp = await client.get(url, timeout=None, headers=headers, follow_redirects=True)
                        resp.raise_for_status()
                        return str(resp.url), await resp.aread()
                    
                    final_url, file_bytes = await asyncio.wait_for(_download_doc(), timeout=60)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"下载超时 (60s): {url}")

                print(f"   rocket [SearchAPI Scraper] 正在使用 ResourceParser (MarkItDown) 解析...")
                # Run synchronous parsing in a thread
                raw_content = await asyncio.to_thread(self.resource_parser.parse, file_bytes, ext or ".bin")
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
            enterprise_names: List[str]  # 接收列表
    ) -> Dict[str, Any]:
        """
        根据有效的输入，条件化地创建并并发执行所有抓取任务。
        """
        final_results = {
            "content_results": [],
            "job_result": None,
            "enterprise_results": [],  # 默认返回空列表
        }
        ssl_context = httpx.create_ssl_context(verify=False)
        async with httpx.AsyncClient(http2=True, verify=ssl_context, timeout=30, follow_redirects=True,
                                     limits=httpx.Limits(max_connections=50)) as client:

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
            all_results = await asyncio.gather(*tasks_to_run, return_exceptions=True)
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
    web_url_info_list = parsed_data["web_url_info_list"]
    video_url_info_list = parsed_data["video_url_info_list"]
    career_payload = parsed_data["career_payload"]
    enterprise_names = parsed_data["enterprise_names"]
    if not web_url_info_list and not video_url_info_list and (
            not career_payload or not career_payload.get("keywords")) and not enterprise_names:
        print("🟡 所有输入均为空，提前返回。")
        return {"scraped_datas": {}, "scraped_datas_str": "{}"}
    # 2. 运行调度器
    orchestrator = DataOrchestrator()
    results = await orchestrator.process_all(web_url_info_list, career_payload, enterprise_names)
    # 3. 格式化网页内容输出
    all_source_list = []
    for result in results["content_results"]:
        if isinstance(result, Exception): continue
        if result.get("status") == "success":
            sanitized_url = re.sub(r'[^a-zA-Z0-9]', '-',
                                   result.get("url", "").replace("https://", "").replace("http://", ""))
            all_source_list.append({
                "type": "web", "source_id": f"web-{sanitized_url[:100]}", "url": result.get("url"),
                "title": result.get("title"), "source": result.get("source"), "snippet": result.get("snippet"),
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
    # 【调整】处理招聘和企业信息结果时，检查它们是否存在（是否为None）
    career_postings = results.get("job_result")
    if career_postings is None:
        career_postings = {}  # 如果未执行，则返回空对象
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

    # 6. 组装最终输出
    comprehensive_data_output = {"all_source_list": all_source_list, "all_video_list": all_video_list}
    final_output = {
        "scraped_datas": {
            "comprehensive_data": comprehensive_data_output,
            "career_postings": career_postings,
            "enterprise_infos": enterprise_infos_output
        }
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
        error_payload = {
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
        }
        return _dify_debug_return({
            "scraped_datas": error_payload,
            "scraped_datas_str": json.dumps({"scraped_datas": error_payload}, ensure_ascii=False, indent=2)
        })


main({
  "comprehensive_data": [
    {
      "errors": [],
      "policy_regional_results": [
        {
          "searchapi_snippet": "发布《2024 年网络安全产业人才发展报告》。报告显示，我国网络. 安全产业规模持续增长。根据中国信通院的统计测算，2022 年我国. 网络安全产业规模达到2055.3 亿元...",
          "searchapi_source": "中华人民共和国教育部政府门户网站",
          "searchapi_title": "1.学校基本情况 - 登录- 教育部",
          "searchapi_type": "policy_regional",
          "searchapi_url": "https://server.x-pilot.cn/static/meta-doc/pdf/cb3d7e4957f9f2908b2e930dfc090c8f.pdf"
        }
      ],
      "query": "湖北省 中医药产业 市场规模与发展趋势分析报告 2024-2026"
    }
  ]
})

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
