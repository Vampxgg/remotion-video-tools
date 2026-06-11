# -*- coding: utf-8 -*-
"""
BOSS 直聘职位采集客户端。

实现原则：
- 复用本机已登录的 Chrome 调试端口；
- 默认走「直连模式」：浏览器只负责导航一次以铸造 ``__zp_stoken__`` cookie，
  之后列表 / 详情都用 httpx 直接调用官方 wapi 接口；
- ``__zp_stoken__`` 是消耗型令牌，单次铸造约支持 5 次成功调用（列表/详情共享），
  耗尽后（code=37）自动重新导航刷新 cookie；
- 直连不可用时回退到浏览器 listen 拦截（不做验证码/环境校验等强绕过）；
- 小批量串行采集，遇到异常立即返回给调用方处理。
"""

import asyncio
import json
import time
from random import uniform
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/jobs/boss_zhipin.log")

SEARCH_API_PATTERN = "/wapi/zpgeek/search/joblist.json"
SEARCH_PAGE_URL = "https://www.zhipin.com/web/geek/jobs"
LIST_API_URL = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
DETAIL_API_URL = "https://www.zhipin.com/wapi/zpgeek/job/detail.json"

# BOSS 反爬：环境异常错误码，表示 __zp_stoken__ 失效，需要刷新 cookie。
_BOSS_ENV_ERROR_CODE = 37


class _DirectBossSession:
    """BOSS 直连会话：用浏览器 tab 铸造 cookie，用 httpx 调官方接口。

    ``__zp_stoken__`` 由页面 JS 生成，属消耗型令牌：一次铸造约支持
    ``BOSS_ZHIPIN_DIRECT_BUDGET_PER_TOKEN`` 次成功调用，耗尽后自动刷新。
    """

    def __init__(self, tab, http: httpx.Client) -> None:
        self._tab = tab
        self._http = http
        self._ua: Optional[str] = None
        self._budget = 0
        self._refresh_count = 0

    @property
    def refresh_count(self) -> int:
        return self._refresh_count

    @property
    def tab(self):
        return self._tab

    def _refresh_cookies(self) -> None:
        """导航 BOSS 搜索页，铸造新的 __zp_stoken__ 并写入 httpx client。"""
        self._tab.get(
            f"{SEARCH_PAGE_URL}?{urlencode({'query': 'Java', 'city': 101280600})}"
        )
        time.sleep(_settings.BOSS_ZHIPIN_DIRECT_COOKIE_WAIT_SEC)
        raw = self._tab.cookies(all_domains=True)
        try:
            cookies = raw.as_dict()
        except Exception:
            cookies = {c.get("name"): c.get("value") for c in raw}
        if "__zp_stoken__" not in cookies:
            raise RuntimeError("刷新 cookie 后仍缺少 __zp_stoken__，疑似未登录或被风控")
        self._ua = self._tab.run_js("return navigator.userAgent;")
        # httpx Client 的 cookie jar 是累积的，先清空避免旧 stoken 残留覆盖。
        self._http.cookies.clear()
        for name, value in cookies.items():
            self._http.cookies.set(name, value, domain=".zhipin.com")
        self._budget = _settings.BOSS_ZHIPIN_DIRECT_BUDGET_PER_TOKEN
        self._refresh_count += 1
        logger.info(
            "BOSS 直连刷新 cookie 成功（第 %s 次），配额=%s",
            self._refresh_count,
            self._budget,
        )

    def _raw_get(self, url: str, params: Dict[str, Any], referer: str) -> Dict[str, Any]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self._ua or _settings.FETCH_USER_AGENT,
            "Referer": referer,
            "x-requested-with": "XMLHttpRequest",
        }
        resp = self._http.get(url, params=params, headers=headers)
        try:
            body = resp.json()
        except Exception as exc:
            preview = (resp.text or "")[:200]
            raise RuntimeError(
                f"BOSS 直连返回非 JSON: status={resp.status_code}, preview={preview!r}"
            ) from exc
        if not isinstance(body, dict):
            raise RuntimeError(f"BOSS 直连返回非对象: type={type(body).__name__}")
        return body

    def get(self, url: str, params: Dict[str, Any], referer: str) -> Dict[str, Any]:
        """带配额管理与 code=37 自动刷新重试的直连请求。"""
        if self._budget <= 0:
            self._refresh_cookies()

        body = self._raw_get(url, params, referer)
        if body.get("code") == _BOSS_ENV_ERROR_CODE:
            logger.info("BOSS 直连命中 code=37（token 失效），刷新 cookie 后重试一次")
            self._refresh_cookies()
            body = self._raw_get(url, params, referer)

        if body.get("code") == 0:
            self._budget -= 1
        return body

    def fetch_list(self, keyword: str, city_code: int, page_num: int) -> Dict[str, Any]:
        params = {
            "scene": 1,
            "query": keyword,
            "city": city_code,
            "page": page_num,
            "pageSize": 30,
        }
        referer = (
            f"{SEARCH_PAGE_URL}?{urlencode({'query': keyword, 'city': city_code})}"
        )
        body = self.get(LIST_API_URL, params, referer)
        code = body.get("code")
        if code != 0:
            raise RuntimeError(
                f"BOSS 直连列表失败: code={code}, message={body.get('message')!r}, "
                f"keyword={keyword}, city={city_code}, page={page_num}"
            )
        return body

    def fetch_description(self, security_id: str, lid: Optional[str]) -> Optional[str]:
        if not security_id:
            return None
        params: Dict[str, Any] = {"securityId": security_id}
        if lid:
            params["lid"] = lid
        body = self.get(DETAIL_API_URL, params, SEARCH_PAGE_URL)
        if body.get("code") != 0:
            return None
        job_info = (body.get("zpData") or {}).get("jobInfo") or {}
        return job_info.get("postDescription") or ""


class BossZhipinClient:
    """通过 DrissionPage 监听/铸造 cookie + httpx 直连 BOSS 直聘职位接口。

    浏览器 tab、httpx client、直连会话都是持久化复用的：首次调用时惰性创建，
    之后跨多次 ``scrape_many`` 复用，省掉每次建/关 tab 的开销；tab 失效时
    自动重建。所有浏览器操作由 ``self._lock`` 串行化。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._page = None
        self._http: Optional[httpx.Client] = None
        self._session: Optional[_DirectBossSession] = None

    async def scrape_many(
        self,
        keywords: List[str],
        city_codes: List[int],
        max_pages: int,
        max_items_per_query: Optional[int],
        include_raw: bool,
        include_description: bool,
    ) -> Dict[str, Any]:
        """串行采集多个关键词和城市组合。"""
        async with self._lock:
            return await asyncio.to_thread(
                self._scrape_many_sync,
                keywords,
                city_codes,
                max_pages,
                max_items_per_query,
                include_raw,
                include_description,
            )

    async def shutdown(self) -> None:
        """释放持久化浏览器 tab / httpx client（由 lifespan 调用）。"""
        async with self._lock:
            await asyncio.to_thread(self._shutdown_sync)

    # ─────────────── 持久化资源管理（worker 线程内调用）───────────────

    def _page_alive(self) -> bool:
        if self._page is None:
            return False
        try:
            _ = self._page.url
            return True
        except Exception:
            return False

    def _ensure_resources(self, direct_enabled: bool) -> None:
        """惰性创建/复用持久化 tab、httpx client、直连会话；tab 失效则重建。"""
        if not self._page_alive():
            self._close_page()
            from DrissionPage import ChromiumPage

            logger.info("BOSS 持久化 tab 初始化 …")
            self._page = ChromiumPage(
                _settings.BOSS_ZHIPIN_BROWSER_HOST_PORT
            ).new_tab()
            # tab 重建后旧会话失效，强制重建以绑定新 tab。
            self._session = None

        if direct_enabled:
            if self._http is None:
                self._http = httpx.Client(
                    timeout=_settings.BOSS_ZHIPIN_DIRECT_HTTP_TIMEOUT
                )
            if self._session is None or self._session.tab is not self._page:
                self._session = _DirectBossSession(self._page, self._http)

    def _close_page(self) -> None:
        if self._page is not None:
            try:
                self._page.close()
            except Exception as exc:
                logger.debug(f"关闭 BOSS 持久化 tab 失败: {exc}")
            self._page = None
        self._session = None

    def _shutdown_sync(self) -> None:
        self._close_page()
        if self._http is not None:
            try:
                self._http.close()
            except Exception as exc:
                logger.debug(f"关闭 BOSS httpx client 失败: {exc}")
            self._http = None
        logger.info("BOSS 持久化资源已释放")

    def _scrape_many_sync(
        self,
        keywords: List[str],
        city_codes: List[int],
        max_pages: int,
        max_items_per_query: Optional[int],
        include_raw: bool,
        include_description: bool,
    ) -> Dict[str, Any]:
        direct_enabled = _settings.BOSS_ZHIPIN_DIRECT_ENABLED
        self._ensure_resources(direct_enabled)
        page = self._page
        session = self._session
        refreshes_before = session.refresh_count if session is not None else 0

        jobs: List[Dict[str, Any]] = []
        seen_keys = set()
        warnings: List[str] = []
        combos = 0
        pages_fetched = 0

        try:
            for keyword in keywords:
                for city_code in city_codes:
                    combos += 1
                    query_count = 0
                    for page_num in range(1, max_pages + 1):
                        body = self._fetch_list(
                            page, session, keyword, city_code, page_num, warnings
                        )
                        zp_data = body.get("zpData") or {}
                        raw_jobs = zp_data.get("jobList") or []
                        pages_fetched += 1

                        if not raw_jobs:
                            warnings.append(
                                f"{keyword}/{city_code}/page={page_num} 未返回职位，停止该组合后续页。"
                            )
                            break

                        for raw_job in raw_jobs:
                            job = self._normalize_job(
                                raw_job,
                                keyword=keyword,
                                city_code=city_code,
                                page_num=page_num,
                                include_raw=include_raw,
                            )
                            key = self._job_key(job)
                            if key in seen_keys:
                                continue
                            if include_description:
                                self._enrich_description(page, session, job)
                            seen_keys.add(key)
                            jobs.append(job)
                            query_count += 1
                            if max_items_per_query and query_count >= max_items_per_query:
                                break

                        if max_items_per_query and query_count >= max_items_per_query:
                            break

                        has_more = bool(zp_data.get("hasMore"))
                        if not has_more:
                            break

                        self._sleep_between_calls(direct_enabled)
        except Exception:
            # 仅当 tab 确实失效时才丢弃（下次调用重建）；瞬时错误保留健康 tab 复用。
            if not self._page_alive():
                self._close_page()
            raise

        summary = {
            "keywords": keywords,
            "city_codes": city_codes,
            "max_pages": max_pages,
            "max_items_per_query": max_items_per_query,
            "include_raw": include_raw,
            "include_description": include_description,
            "combinations": combos,
            "pages_fetched": pages_fetched,
            "total_jobs": len(jobs),
            "mode": "direct" if direct_enabled else "browser",
        }
        if session is not None:
            summary["cookie_refreshes"] = session.refresh_count - refreshes_before
        return {
            "summary": summary,
            "jobs": jobs,
            "warnings": warnings,
        }

    @staticmethod
    def _sleep_between_calls(direct_enabled: bool) -> None:
        if direct_enabled:
            lo, hi = (
                _settings.BOSS_ZHIPIN_DIRECT_MIN_DELAY_SEC,
                _settings.BOSS_ZHIPIN_DIRECT_MAX_DELAY_SEC,
            )
        else:
            lo, hi = (
                _settings.BOSS_ZHIPIN_MIN_DELAY_SEC,
                _settings.BOSS_ZHIPIN_MAX_DELAY_SEC,
            )
        time.sleep(uniform(lo, hi))

    def _fetch_list(
        self,
        page,
        session: Optional["_DirectBossSession"],
        keyword: str,
        city_code: int,
        page_num: int,
        warnings: List[str],
    ) -> Dict[str, Any]:
        """优先直连列表接口，失败时回退浏览器 listen 拦截。"""
        if session is not None:
            try:
                return session.fetch_list(keyword, city_code, page_num)
            except Exception as exc:
                msg = (
                    f"{keyword}/{city_code}/page={page_num} 直连失败，"
                    f"回退浏览器: {exc}"
                )
                logger.warning(msg)
                warnings.append(msg)
        return self._fetch_page(page, keyword, city_code, page_num)

    def _enrich_description(
        self,
        page,
        session: Optional["_DirectBossSession"],
        job: Dict[str, Any],
    ) -> None:
        """优先直连详情接口拿描述，失败时回退浏览器导航详情页。"""
        if session is not None:
            try:
                description = session.fetch_description(
                    job.get("security_id"), job.get("lid")
                )
                if description:
                    job["job_description"] = description
                    parts = self._split_description(description)
                    job["responsibilities"] = parts.get("responsibilities")
                    job["requirements"] = parts.get("requirements")
                    job["description_status"] = "success"
                    self._sleep_between_calls(True)
                    return
                logger.info(
                    "BOSS 直连详情为空，回退浏览器: %s", job.get("detail_url")
                )
            except Exception as exc:
                logger.warning(
                    "BOSS 直连详情失败，回退浏览器 [%s]: %s",
                    job.get("detail_url"),
                    exc,
                )
        self._enrich_job_description(page, job)

    def _fetch_page(self, page, keyword: str, city_code: int, page_num: int) -> Dict[str, Any]:
        url = self._build_search_url(keyword, city_code, page_num)
        logger.info(f"BOSS 搜索: keyword={keyword}, city={city_code}, page={page_num}")

        try:
            page.listen.clear()
        except Exception:
            pass
        page.listen.start(SEARCH_API_PATTERN)
        page.get(url)

        packet = page.listen.wait(timeout=_settings.BOSS_ZHIPIN_LISTEN_TIMEOUT_SEC)
        try:
            page.listen.stop()
        except Exception:
            pass

        if not packet or not getattr(packet, "response", None):
            raise RuntimeError(
                "BOSS 职位接口未触发或超时: "
                f"keyword={keyword}, city={city_code}, page={page_num}, url={url}"
            )

        response = packet.response
        body = response.body
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError as exc:
                preview = body[:300].replace("\n", "\\n").replace("\r", "\\r")
                if not preview:
                    preview = "<empty>"
                status_code = getattr(response, "status", None) or getattr(response, "status_code", None)
                logger.warning(
                    "BOSS 接口返回非 JSON: keyword=%s city=%s page=%s url=%s "
                    "status=%s len=%s preview=%s",
                    keyword,
                    city_code,
                    page_num,
                    url,
                    status_code,
                    len(body),
                    preview,
                )
                raise RuntimeError(
                    "BOSS 接口返回非 JSON 内容: "
                    f"keyword={keyword}, city={city_code}, page={page_num}, "
                    f"status={status_code}, len={len(body)}, preview={preview}"
                ) from exc

        if not isinstance(body, dict):
            raise RuntimeError(
                "BOSS 接口返回格式异常: "
                f"type={type(body).__name__}, keyword={keyword}, city={city_code}, "
                f"page={page_num}, url={url}"
            )

        code = body.get("code")
        if code != 0:
            message = body.get("message") or "未知错误"
            raise RuntimeError(
                "BOSS 接口返回错误: "
                f"code={code}, message={message}, keyword={keyword}, "
                f"city={city_code}, page={page_num}, url={url}"
            )

        return body

    def _enrich_job_description(self, page, job: Dict[str, Any]) -> None:
        detail_url = job.get("detail_url")
        if not detail_url:
            job["description_status"] = "missing_detail_url"
            return

        try:
            logger.info(f"BOSS 详情: {detail_url}")
            page.get(detail_url)
            time.sleep(uniform(
                _settings.BOSS_ZHIPIN_DETAIL_MIN_DELAY_SEC,
                _settings.BOSS_ZHIPIN_DETAIL_MAX_DELAY_SEC,
            ))
            description = self._extract_detail_text(page)
            job["job_description"] = description
            parts = self._split_description(description)
            job["responsibilities"] = parts.get("responsibilities")
            job["requirements"] = parts.get("requirements")
            job["description_status"] = "success" if description else "empty"
        except Exception as exc:
            logger.warning(f"BOSS 详情提取失败 [{detail_url}]: {exc}")
            job["job_description"] = ""
            job["responsibilities"] = ""
            job["requirements"] = ""
            job["description_status"] = f"failed: {exc}"

    @staticmethod
    def _extract_detail_text(page) -> str:
        selectors = [
            "css:.job-detail-section .job-sec-text",
            "css:.job-sec-text",
        ]
        for selector in selectors:
            try:
                element = page.ele(selector, timeout=3)
                if element:
                    text = (element.text or "").strip()
                    if text:
                        return text
            except Exception:
                continue
        return ""

    @staticmethod
    def _split_description(description: str) -> Dict[str, str]:
        """按常见中文小标题粗略拆分职责和要求，保留完整描述作为主字段。"""
        if not description:
            return {"responsibilities": "", "requirements": ""}

        markers = {
            "responsibilities": ("岗位职责", "工作职责", "职位职责", "岗位描述", "工作内容"),
            "requirements": ("任职要求", "岗位要求", "职位要求", "任职资格", "能力要求"),
        }
        stop_markers = (
            "任职要求", "岗位要求", "职位要求", "任职资格", "能力要求",
            "加分项", "福利待遇", "薪资福利", "工作时间",
        )

        responsibilities = BossZhipinClient._slice_section(
            description,
            markers["responsibilities"],
            stop_markers,
        )
        requirements = BossZhipinClient._slice_section(
            description,
            markers["requirements"],
            ("加分项", "福利待遇", "薪资福利", "工作时间"),
        )
        return {
            "responsibilities": responsibilities,
            "requirements": requirements,
        }

    @staticmethod
    def _slice_section(text: str, starts: tuple, stops: tuple) -> str:
        start_pos = -1
        start_len = 0
        for marker in starts:
            pos = text.find(marker)
            if pos >= 0 and (start_pos < 0 or pos < start_pos):
                start_pos = pos
                start_len = len(marker)
        if start_pos < 0:
            return ""

        section_start = start_pos + start_len
        section_end = len(text)
        for marker in stops:
            pos = text.find(marker, section_start)
            if pos >= 0 and pos < section_end:
                section_end = pos
        return text[section_start:section_end].strip(" ：:\n\t")

    @staticmethod
    def _build_search_url(keyword: str, city_code: int, page_num: int) -> str:
        query = urlencode({
            "query": keyword,
            "city": city_code,
            "industry": "",
            "position": "",
            "page": page_num,
        })
        return f"{SEARCH_PAGE_URL}?{query}"

    @staticmethod
    def _normalize_job(
        raw: Dict[str, Any],
        *,
        keyword: str,
        city_code: int,
        page_num: int,
        include_raw: bool,
    ) -> Dict[str, Any]:
        detail_url = ""
        encrypt_job_id = raw.get("encryptJobId")
        security_id = raw.get("securityId")
        if encrypt_job_id:
            detail_url = f"https://www.zhipin.com/job_detail/{encrypt_job_id}.html"

        job = {
            "source": "boss_zhipin",
            "keyword": keyword,
            "query_city_code": city_code,
            "page": page_num,
            "job_name": raw.get("jobName"),
            "company_name": raw.get("brandName"),
            "salary": raw.get("salaryDesc"),
            "city": raw.get("cityName"),
            "district": raw.get("areaDistrict"),
            "business_district": raw.get("businessDistrict"),
            "experience": raw.get("jobExperience"),
            "degree": raw.get("jobDegree"),
            "skills": raw.get("skills") or [],
            "labels": raw.get("jobLabels") or [],
            "welfare": raw.get("welfareList") or [],
            "company_stage": raw.get("brandStageName"),
            "company_industry": raw.get("brandIndustry"),
            "company_scale": raw.get("brandScaleName"),
            "brand_logo": BossZhipinClient._absolutize_logo(raw.get("brandLogo")),
            "boss_title": raw.get("bossTitle"),
            "boss_online": raw.get("bossOnline"),
            "encrypt_job_id": encrypt_job_id,
            "security_id": security_id,
            "lid": raw.get("lid"),
            "detail_url": detail_url,
            "gps": raw.get("gps"),
        }
        if include_raw:
            job["raw"] = raw
        return job

    @staticmethod
    def _absolutize_logo(value: Optional[str]) -> Optional[str]:
        """BOSS brandLogo 兜底为绝对 URL。

        - 空值 → None
        - 已是 http(s) 绝对地址 → 原样返回
        - 协议相对 ``//host/...`` → 补 https:
        - 站内相对路径 ``/...`` → 拼 BOSS 图片 host
        """
        if not value or not isinstance(value, str):
            return None
        url = value.strip()
        if not url:
            return None
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("//"):
            return f"https:{url}"
        if url.startswith("/"):
            return f"https://img.bosszhipin.com{url}"
        return url

    @staticmethod
    def _job_key(job: Dict[str, Any]) -> str:
        return (
            job.get("encrypt_job_id")
            or "|".join(str(job.get(k) or "") for k in (
                "job_name",
                "company_name",
                "salary",
                "city",
                "district",
            ))
        )


# ══════════════════════════════════════════════════════════════════════
#  模块级单例：多个 router 共享同一个持久化 tab，避免重复占用浏览器
# ══════════════════════════════════════════════════════════════════════

_shared_client: Optional[BossZhipinClient] = None


def get_boss_client() -> BossZhipinClient:
    """返回进程内共享的 BossZhipinClient 单例。"""
    global _shared_client
    if _shared_client is None:
        _shared_client = BossZhipinClient()
    return _shared_client
