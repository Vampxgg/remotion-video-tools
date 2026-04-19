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

# --- 核心依赖 ---
# trafilatura 用于从HTML提取主要内容
import trafilatura
from trafilatura.settings import use_config

# PyPDF2 用于解析PDF
from PyPDF2 import PdfReader

# BeautifulSoup 用于辅助解析HTML（例如提取视频）
from bs4 import BeautifulSoup


# --- 1. 输入解析模块 ---
def _parse_input_data(raw_input: Any) -> Dict[str, Any]:
    """
    健壮地解析上一个节点的输出，分离出web搜索URL和招聘查询参数。
    """
    if isinstance(raw_input, str):
        if not raw_input.strip(): return {"url_list": [], "career_payload": {}}
        try:
            data = json.loads(raw_input)
        except json.JSONDecodeError as e:
            raise ValueError(f"无法将输入字符串解析为JSON: {e}")
    elif isinstance(raw_input, dict):
        data = raw_input
    else:
        raise TypeError(f"期望的输入类型是 str 或 dict, 但收到了 {type(raw_input).__name__}")
    # 安全地深入到 'datas' 结构
    datas_obj = data.get("datas", {})
    if not isinstance(datas_obj, dict): datas_obj = {}
    comprehensive_data = datas_obj.get("comprehensive_data", [])
    career_data = datas_obj.get("career_data", {})
    # 1. 提取URL列表
    url_list = []
    if isinstance(comprehensive_data, list):
        for query_result in comprehensive_data:
            if not isinstance(query_result, dict): continue
            for res_list_key in ["web_results", "video_results"]:
                for res in query_result.get(res_list_key, []):
                    if not isinstance(res, dict): continue
                    url, title, provider = None, None, None
                    for key, value in res.items():
                        if key.endswith("_url"):
                            url = value
                            provider = key.split('_url')[0]
                            title = res.get(f"{provider}_title", "Untitled")
                            break
                    if url and provider:
                        url_list.append({"url": url, "title": title, "provider": provider})
    # 2. 提取职业查询负载
    career_payload = career_data if isinstance(career_data, dict) else {}

    return {"url_list": url_list, "career_payload": career_payload}


# --- 2. 抽象与实现分离：内容抓取器 ---
class ContentScraper(ABC):
    """内容抓取器的抽象基类。"""

    @abstractmethod
    async def scrape(self, url: str, title: str, client: httpx.AsyncClient) -> Dict[str, Any]:
        """
        抓取单个URL的内容。
        成功时返回: {"url": str, "title": str, "content": str, "status": "success"}
        失败时返回: {"url": str, "title": str, "content": "", "status": "failed", "error_message": str}
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
        try:
            reader = PdfReader(BytesIO(binary_content))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as e:
            print(f"⚠️ PDF 解析失败: {e}")
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
    async def scrape(self, url: str, title: str, client: httpx.AsyncClient) -> dict:
        print(f"🕸️ [SearchAPI Scraper] 开始处理: {url}")
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            response = await client.get(url, timeout=20, headers=headers, follow_redirects=True)
            response.raise_for_status()

            final_url = str(response.url)
            content_type = response.headers.get('content-type', '').lower()

            raw_content, html_for_video_parsing = "", ""

            if 'pdf' in content_type or final_url.lower().endswith(".pdf"):
                print(f"  📄 [SearchAPI Scraper] 检测到 PDF: {final_url}")
                pdf_bytes = await response.aread()
                raw_content = await asyncio.to_thread(self._extract_pdf_text, pdf_bytes)
            else:
                print(f"  📑 [SearchAPI Scraper] 检测到 HTML: {final_url}")
                html_content = response.text
                html_for_video_parsing = html_content
                raw_content = await asyncio.to_thread(
                    trafilatura.extract, html_content, config=self.trafilatura_config,
                    output_format='markdown', include_images=True, favor_recall=True)

            if not raw_content: raise ValueError("trafilatura 内容提取返回为空。")

            print(f"  🧹 [SearchAPI Scraper] 正在清洗内容: {final_url}")
            cleaned_content = await self._clean_content_async(raw_content, client)

            if html_for_video_parsing:
                videos = self._parse_videos_from_html(html_for_video_parsing, final_url)
                if videos:
                    video_section = "\n\n## 参考视频:\n" + "\n".join(f"- {vid}" for vid in videos)
                    cleaned_content += video_section

            print(f"✅ [SearchAPI Scraper] 成功: {url}")
            return {"url": final_url, "title": title, "content": cleaned_content, "status": "success"}

        except Exception as e:
            error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
            print(f"⚠️ [SearchAPI Scraper] {error_msg}")
            return {"url": url, "title": title, "content": "", "status": "failed", "error_message": str(e)}


# --- 2.2 FirecrawlScraper ---
class FirecrawlScraper(ContentScraper):
    def __init__(self):
        self.api_key = os.environ.get("FIRECRAWL_API_KEY", "fc-a36b7d2fb273485680d0fe6abd686935")
        if not self.api_key: raise ValueError("未提供 Firecrawl API Key。")
        self.base_url = "https://api.firecrawl.dev/v2/scrape"
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def scrape(self, url: str, title: str, client: httpx.AsyncClient) -> dict:
        print(f"🔥 [Firecrawl Scraper] 开始处理: {url}")
        try:
            # **关键修正：将 pageOptions 展平为顶级字段**
            payload = {
                "url": url,
                "onlyMainContent": True,
                # 也可以在这里添加其他顶级选项，例如：
                "removeBase64Images": True,
                "blockAds": True
            }
            resp = await client.post(self.base_url, headers=self.headers, json=payload, timeout=45)

            # 增加对4xx/5xx错误的详细日志记录
            if not resp.is_success:
                try:
                    error_details = resp.json()
                    raise httpx.HTTPStatusError(f"API返回错误: {error_details}", request=resp.request, response=resp)
                except json.JSONDecodeError:
                    resp.raise_for_status()  # 如果无法解析json，则抛出原始错误

            data = resp.json()

            # Firecrawl v2 的成功响应中没有 "success" 键，直接检查 data 字段
            content_data = data.get("data", {})
            if content_data is None:  # 可能是 null
                raise ValueError("API返回的 'data' 字段为 null。")

            content = content_data.get("markdown")  # markdown 可能为空字符串，这是正常的
            if content is None:
                raise ValueError("API未返回 'markdown' 字段。")

            print(f"✅ [Firecrawl Scraper] 成功: {url}")
            return {"url": url, "title": title, "content": content, "status": "success"}
        except Exception as e:
            error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
            print(f"⚠️ [Firecrawl Scraper] {error_msg}")
            return {"url": url, "title": title, "content": "", "status": "failed", "error_message": str(e)}


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

    async def scrape(self, url: str, title: str, client: httpx.AsyncClient) -> dict:
        print(f"🌀 [Jina Scraper] 开始处理: {url}")
        try:
            # Jina的Reader API有时对普通的GET请求更友好
            target_url = f"{self.base_url}{url}"
            resp = await client.get(target_url, headers=self.headers, timeout=45)
            resp.raise_for_status()
            content = resp.text
            if not content: raise ValueError("API 返回内容为空。")

            print(f"✅ [Jina Scraper] 成功: {url}")
            return {"url": url, "title": title, "content": content, "status": "success"}

        except Exception as e:
            error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
            print(f"⚠️ [Jina Scraper] {error_msg}")
            return {"url": url, "title": title, "content": "", "status": "failed", "error_message": str(e)}


# --- 2.4 TavilyScraper ---
class TavilyScraper(ContentScraper):
    def __init__(self):
        self.api_key = os.environ.get("TAVILY_API_KEY", "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")
        if not self.api_key: raise ValueError("未提供 Tavily API Key。")
        self.base_url = "https://api.tavily.com/extract"
        self.headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def scrape(self, url: str, title: str, client: httpx.AsyncClient) -> dict:
        print(f"🤖 [Tavily Scraper] 开始处理: {url}")
        try:
            # 注意：Tavily 的Python库没有extract方法，必须直接调用API
            payload = {"urls": [url], "format": "markdown"}
            resp = await client.post(self.base_url, json=payload, headers=self.headers, timeout=45)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("results") or not isinstance(data["results"], list):
                failed_info = data.get("failed_results", [])
                raise ValueError(f"API调用失败: {failed_info}")

            result = data["results"][0]
            content = result.get("raw_content")  # 文档显示raw_content，并且format=markdown
            if not content: raise ValueError("API未返回raw_content内容。")

            print(f"✅ [Tavily Scraper] 成功: {url}")
            return {"url": url, "title": title, "content": content, "status": "success"}

        except Exception as e:
            error_msg = f"处理失败 {url}: {type(e).__name__} - {e}"
            print(f"⚠️ [Tavily Scraper] {error_msg}")
            return {"url": url, "title": title, "content": "", "status": "failed", "error_message": str(e)}


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


# --- 4. 统一调度中心 (已重命名和扩展) ---
class DataOrchestrator:
    def __init__(self):
        self.content_scrapers: Dict[str, ContentScraper] = {
            "searchapi": SearchApiScraper(), "firecrawl": FirecrawlScraper(),
            "jina": JinaScraper(), "tavily": TavilyScraper(),
        }
        self.job_scraper = ZhiLianJobScraper()

    async def process_all(self, url_list: List[Dict[str, str]], career_payload: Dict) -> Dict[str, Any]:
        ssl_context = httpx.create_ssl_context(verify=False)
        async with httpx.AsyncClient(http2=True, verify=ssl_context, timeout=30, follow_redirects=True,
                                     limits=httpx.Limits(max_connections=50)) as client:
            # 创建两组任务
            content_tasks = []
            for item in url_list:
                scraper = self.content_scrapers.get(item.get("provider"))
                if scraper: content_tasks.append(scraper.scrape(item["url"], item["title"], client))

            job_task = self.job_scraper.scrape_jobs(career_payload, client)

            # 并发执行所有任务
            results = await asyncio.gather(*content_tasks, job_task, return_exceptions=True)

            # 分离结果
            content_results = results[:-1]
            job_result = results[-1]

            return {"content_results": content_results, "job_result": job_result}


# --- 5. Dify 节点主入口 ---
async def main_async(raw_input: Any) -> Dict[str, Any]:
    # 1. 解析输入
    parsed_data = _parse_input_data(raw_input)
    url_list = parsed_data["url_list"]
    career_payload = parsed_data["career_payload"]
    if not url_list and not career_payload.get("keywords"):
        print("🟡 输入中没有有效的URL或招聘查询，提前返回。")
        return {"scraped_datas": {}, "scraped_datas_str": "{}"}
    # 2. 运行调度器
    orchestrator = DataOrchestrator()
    results = await orchestrator.process_all(url_list, career_payload)

    # 3. 格式化网页内容输出
    comprehensive_content = []
    for result in results["content_results"]:
        if isinstance(result, Exception): continue
        if result.get("status") == "success" and result.get("content"):
            sanitized_url = re.sub(r'[^a-zA-Z0-9]', '-', result["url"].replace("https://", "").replace("http://", ""))
            comprehensive_content.append({
                "source_id": f"web-{sanitized_url[:100]}", "source_name": result["title"],
                "url": result["url"], "content": result["content"]
            })
    # 4. 格式化招聘信息输出
    career_postings = results["job_result"]
    if isinstance(career_postings, Exception):
        career_postings = {"status": "failed", "data": [], "message": f"任务异常: {career_postings}"}
    # 5. 组装最终输出
    final_output = {
        "scraped_datas": {
            "comprehensive_content": comprehensive_content,
            "career_postings": career_postings
        }
    }
    return {
        "scraped_datas": final_output["scraped_datas"],
        "scraped_datas_str": json.dumps(final_output, ensure_ascii=False, indent=2)
    }


def main(datas_input: Any) -> Dict[str, Any]:
    try:
        return asyncio.run(main_async(raw_input=datas_input))
    except Exception as e:
        print(f"‼️ 节点执行时发生顶层错误: {e}")
        error_payload = {
            "comprehensive_content": [{
                "source_id": "NODE_EXECUTION_ERROR", "source_name": "节点执行失败", "url": "",
                "content": f"An error occurred: {str(e)}\n\n{traceback.format_exc()}"
            }],
            "career_postings": {"status": "failed", "message": "节点执行失败", "data": []}
        }
        return {
            "scraped_datas": error_payload,
            "scraped_datas_str": json.dumps({"scraped_datas": error_payload}, ensure_ascii=False, indent=2)
        }
