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
import queue
import re
import threading
import time
from datetime import datetime
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

# 命中访问受限 / IP 异常 / 验证码等风控拦截页的文本特征。
_ACCESS_LIMITED_MARKERS = (
    "访问受限",
    "存在异常行为",
    "IP 存在异常",
    "IP存在异常",
    "请勿频繁",
    "恢复正常",
    "验证码",
    "人机验证",
)


class BossAccessLimitedError(RuntimeError):
    """BOSS 访问受限 / IP 异常 / 验证码 / 连续 code=37 等风控信号。

    这是**非重试型**错误：立即重试或换请求只会加重风控。调用方应据此熔断一段时间。
    ``retry_after_seconds`` 来自风控页「将于 X 恢复正常」提示（可能为空）；
    ``raw_hint`` 是脱敏后的提示摘要（已抹去 IP），仅供排查。
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: Optional[int] = None,
        raw_hint: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.raw_hint = raw_hint


def _looks_access_limited(text: Optional[str]) -> bool:
    if not text:
        return False
    return any(marker in text for marker in _ACCESS_LIMITED_MARKERS)


def _parse_recovery_seconds(text: Optional[str]) -> Optional[int]:
    """从「将于 2026-07-02 19:00 恢复正常」解析距恢复的秒数。"""
    if not text:
        return None
    match = re.search(r"将于\s*([\d\-:\s]+?)\s*恢复", text)
    if not match:
        return None
    raw = match.group(1).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            recover_at = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        delta = (recover_at - datetime.now()).total_seconds()
        if delta > 0:
            return int(delta)
    return None


def _sanitize_hint(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    scrubbed = re.sub(r"\d+\.\d+\.\d+\.\d+", "<ip>", text)
    return scrubbed.strip()[:200]


class _DirectBossSession:
    """BOSS 直连会话：用浏览器 tab 铸造 cookie，用 httpx 调官方接口。

    ``__zp_stoken__`` 由页面 JS 生成，属消耗型令牌：一次铸造约支持
    ``BOSS_ZHIPIN_DIRECT_BUDGET_PER_TOKEN`` 次成功调用，耗尽后自动刷新。
    """

    def __init__(self, tab, http: httpx.Client, tab_lock=None) -> None:
        self._tab = tab
        self._http = http
        # 共享浏览器 tab 的可重入锁：并发>1 时保证同一时刻只有一个会话在导航 tab
        # 铸造 cookie（tab 非线程安全）。并发=1 时该锁始终空闲，行为与历史一致。
        self._tab_lock = tab_lock or threading.RLock()
        self._ua: Optional[str] = None
        self._budget = 0
        self._refresh_count = 0

    @property
    def refresh_count(self) -> int:
        return self._refresh_count

    @property
    def tab(self):
        return self._tab

    def _refresh_cookies(self, force: bool = False) -> None:
        """导航 BOSS 搜索页，铸造新的 __zp_stoken__ 并写入 httpx client。

        并发>1 时用共享 ``tab_lock`` 串行化 tab 导航；进锁后若发现配额已被其它
        会话恰好刷新（且非 force），直接跳过，避免重复导航空耗 tab。
        """
        with self._tab_lock:
            if not force and self._budget > 0:
                return
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
                raise BossAccessLimitedError(
                    "刷新 cookie 后仍缺少 __zp_stoken__，疑似未登录或被风控"
                )
            self._ua = self._tab.run_js("return navigator.userAgent;")
        # httpx Client 的 cookie jar 是累积的，先清空避免旧 stoken 残留覆盖。
        # 每个会话有独立 http client/jar，无需担心跨会话污染。
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
            preview = (resp.text or "")[:500]
            if _looks_access_limited(preview) or resp.status_code in (403, 412, 429):
                raise BossAccessLimitedError(
                    f"BOSS 访问受限（非 JSON 风控页）: status={resp.status_code}",
                    retry_after_seconds=_parse_recovery_seconds(preview),
                    raw_hint=_sanitize_hint(preview),
                ) from exc
            raise RuntimeError(
                f"BOSS 直连返回非 JSON: status={resp.status_code}, preview={preview[:200]!r}"
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
            self._refresh_cookies(force=True)
            body = self._raw_get(url, params, referer)
            if body.get("code") == _BOSS_ENV_ERROR_CODE:
                raise BossAccessLimitedError(
                    "BOSS 连续返回 code=37（环境异常/风控），已停止重试"
                )

        if body.get("code") == 0:
            self._budget -= 1
        return body

    def fetch_list(
        self,
        keyword: str,
        city_code: int,
        page_num: int,
        page_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        params = {
            "scene": 1,
            "query": keyword,
            "city": city_code,
            "page": page_num,
            "pageSize": page_size or _settings.BOSS_ZHIPIN_DIRECT_PAGE_SIZE,
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
    自动重建。

    并发模型（``BOSS_ZHIPIN_MAX_CONCURRENCY``）：默认 1=严格串行（历史行为）。
    >1 时 ``scrape_many`` 由 ``self._sema`` 放行 N 个并发，每个并发槽从会话池借一个
    独立会话（独立 httpx client + 独立 __zp_stoken__ 配额）跑直连 API；唯一共享的
    浏览器 tab（铸造 cookie / 浏览器回退）由可重入的 ``self._tab_lock`` 串行化。

    并发硬上限 = 2（实测结论，勿调高）：单浏览器 profile 的 cookie 是浏览器级共享的，
    多个并发会话铸造的 ``__zp_stoken__`` 会互相挤失效（N=2 真实 50 详情实测每 unit 铸
    ~18 次 token，远超 BUDGET_PER_TOKEN=5，靠 code=37 自动重铸兜住）。N=3 时 stoken
    交叉失效的 churn 超过自愈能力，直连列表连续失败 → 回退单 tab 的浏览器 listen 拦截
    → 3 并发争用同一 tab 全部超时，整 unit 报错。要突破 2 只能上「N 个独立 Chrome
    实例（独立 profile + 各自登录）」，代价与风控面都不值。故本客户端并发上限锁定 2。
    """

    def __init__(self) -> None:
        # 并发闸门：默认 1=严格串行（历史行为）。>1 时允许 N 个 scrape 并发。
        self._sema = asyncio.Semaphore(
            max(1, int(_settings.BOSS_ZHIPIN_MAX_CONCURRENCY))
        )
        # 仅 shutdown 使用；scrape 的并发由 _sema 控制。
        self._lock = asyncio.Lock()
        # 共享浏览器 tab 的可重入锁（worker 线程内使用）。
        self._tab_lock = threading.RLock()
        self._page = None
        # 每个并发槽独立的 httpx client + 直连会话，避免共享 cookie jar 相互污染。
        self._http_clients: List[httpx.Client] = []
        self._session_pool: Optional["queue.Queue[_DirectBossSession]"] = None
        self._pool_size = 0

    async def scrape_many(
        self,
        keywords: List[str],
        city_codes: List[int],
        max_pages: int,
        max_items_per_query: Optional[int],
        include_raw: bool,
        include_description: bool,
        start_page: int = 1,
        page_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """串行采集多个关键词和城市组合。

        ``start_page`` 为翻页游标起始页（默认 1）：上层应用（data_server）按 unit 记录
        ``next_page``，每轮从上次的下一页继续，配合 summary 里返回的
        ``next_page`` / ``has_more`` / ``total_count`` 实现多轮精确累积。

        ``page_size`` 覆盖单页条数（默认走 settings）：逐条详情累积时用较小页保证单轮
        时间可控；summary 快路径不传，沿用默认页大小，外部 workflow 行为不变。
        """
        async with self._sema:
            return await asyncio.to_thread(
                self._scrape_many_sync,
                keywords,
                city_codes,
                max_pages,
                max_items_per_query,
                include_raw,
                include_description,
                start_page,
                page_size,
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
        """惰性创建/复用持久化 tab + 直连会话池；tab 失效或并发数变化则重建。

        用 ``_tab_lock``（可重入）串行化资源创建：并发>1 时多个 worker 线程会同时
        进入此方法，锁保证只建一次 tab / 一份会话池。
        """
        with self._tab_lock:
            if not self._page_alive():
                self._close_page_locked()
                from DrissionPage import ChromiumPage

                logger.info("BOSS 持久化 tab 初始化 …")
                self._page = ChromiumPage(
                    _settings.BOSS_ZHIPIN_BROWSER_HOST_PORT
                ).new_tab()
                # tab 重建后旧会话池作废，强制重建以绑定新 tab。
                self._drain_pool_locked()

            if direct_enabled:
                concurrency = max(1, int(_settings.BOSS_ZHIPIN_MAX_CONCURRENCY))
                if self._session_pool is None or self._pool_size != concurrency:
                    self._build_pool_locked(concurrency)

    def _build_pool_locked(self, concurrency: int) -> None:
        """（持有 tab_lock 时调用）为每个并发槽建独立 httpx client + 会话。"""
        self._drain_pool_locked()
        pool: "queue.Queue[_DirectBossSession]" = queue.Queue()
        for _ in range(concurrency):
            http = httpx.Client(timeout=_settings.BOSS_ZHIPIN_DIRECT_HTTP_TIMEOUT)
            self._http_clients.append(http)
            pool.put(_DirectBossSession(self._page, http, self._tab_lock))
        self._session_pool = pool
        self._pool_size = concurrency
        logger.info("BOSS 直连会话池就绪：并发槽=%s", concurrency)

    def _drain_pool_locked(self) -> None:
        """（持有 tab_lock 时调用）关闭并清空会话池与其 httpx client。"""
        for http in self._http_clients:
            try:
                http.close()
            except Exception as exc:
                logger.debug(f"关闭 BOSS httpx client 失败: {exc}")
        self._http_clients = []
        self._session_pool = None
        self._pool_size = 0

    def _close_page(self) -> None:
        with self._tab_lock:
            self._close_page_locked()

    def _close_page_locked(self) -> None:
        if self._page is not None:
            try:
                self._page.close()
            except Exception as exc:
                logger.debug(f"关闭 BOSS 持久化 tab 失败: {exc}")
            self._page = None
        self._drain_pool_locked()

    def _shutdown_sync(self) -> None:
        self._close_page()
        logger.info("BOSS 持久化资源已释放")

    def _scrape_many_sync(
        self,
        keywords: List[str],
        city_codes: List[int],
        max_pages: int,
        max_items_per_query: Optional[int],
        include_raw: bool,
        include_description: bool,
        start_page: int = 1,
        page_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        direct_enabled = _settings.BOSS_ZHIPIN_DIRECT_ENABLED
        self._ensure_resources(direct_enabled)
        page = self._page
        # 从会话池借一个独立会话（并发>1 时每个 worker 各用一个，互不污染 cookie）。
        pool_local = self._session_pool if direct_enabled else None
        session = pool_local.get() if pool_local is not None else None
        refreshes_before = session.refresh_count if session is not None else 0

        jobs: List[Dict[str, Any]] = []
        seen_keys = set()
        warnings: List[str] = []
        combos = 0
        pages_fetched = 0
        start_page = max(1, int(start_page or 1))
        # 翻页游标元数据（单 keyword×city 组合时才有明确意义）。
        total_count: Optional[int] = None
        res_count: Optional[int] = None
        any_has_more = False
        last_page_done = start_page - 1

        try:
            for keyword in keywords:
                for city_code in city_codes:
                    combos += 1
                    query_count = 0
                    combo_has_more = False
                    for page_num in range(start_page, start_page + max_pages):
                        body = self._fetch_list(
                            page, session, keyword, city_code, page_num, warnings,
                            page_size,
                        )
                        zp_data = body.get("zpData") or {}
                        raw_jobs = zp_data.get("jobList") or []
                        pages_fetched += 1
                        last_page_done = page_num
                        if zp_data.get("totalCount") is not None:
                            total_count = zp_data.get("totalCount")
                        if zp_data.get("resCount") is not None:
                            res_count = zp_data.get("resCount")
                        combo_has_more = bool(zp_data.get("hasMore"))

                        if not raw_jobs:
                            warnings.append(
                                f"{keyword}/{city_code}/page={page_num} 未返回职位，停止该组合后续页。"
                            )
                            combo_has_more = False
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

                        if not combo_has_more:
                            break

                        self._sleep_between_calls(direct_enabled)
                    any_has_more = any_has_more or combo_has_more
        except Exception:
            # 仅当 tab 确实失效时才丢弃（下次调用重建）；瞬时错误保留健康 tab 复用。
            if not self._page_alive():
                self._close_page()
            raise
        finally:
            # 归还会话到池（若 tab 已重建导致池被换，归还到旧池无害，旧池会被丢弃）。
            if session is not None and pool_local is not None:
                try:
                    pool_local.put(session)
                except Exception:
                    pass

        single_combo = len(keywords) == 1 and len(city_codes) == 1
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
            "page_size": page_size or _settings.BOSS_ZHIPIN_DIRECT_PAGE_SIZE,
            # 翻页游标：next_page 仅在单组合时有明确意义；has_more/total 供上层判界。
            "start_page": start_page,
            "next_page": (last_page_done + 1) if single_combo else None,
            "has_more": any_has_more,
            "total_count": total_count,
            "res_count": res_count,
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
        page_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """优先直连列表接口，失败时回退浏览器 listen 拦截。

        命中风控（``BossAccessLimitedError``）时直接向上抛，不回退浏览器：
        继续用浏览器导航同一个被封站点只会加重风控。
        """
        if session is not None:
            try:
                return session.fetch_list(keyword, city_code, page_num, page_size)
            except BossAccessLimitedError:
                raise
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
            except BossAccessLimitedError:
                raise
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

        # 浏览器回退：独占共享 tab（并发>1 时避免与其它会话的 tab 操作交叉）。
        with self._tab_lock:
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
            # 浏览器回退：独占共享 tab，导航 + DOM 提取全程串行。
            with self._tab_lock:
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
