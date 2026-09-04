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
from pathlib import Path
from random import uniform
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from services.boss_proxy_pool import BossProxyPool, ProxyLease
from services.boss_worker_runtime import BossWorkerRuntimeManager, WorkerRuntimeConfig
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/jobs/boss_zhipin.log")
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

SEARCH_API_PATTERN = "/wapi/zpgeek/search/joblist.json"
SEARCH_PAGE_URL = "https://www.zhipin.com/web/geek/jobs"
LIST_API_URL = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
DETAIL_API_URL = "https://www.zhipin.com/wapi/zpgeek/job/detail.json"
_MAX_SINGLE_PROFILE_CONCURRENCY = 2

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
        worker_status: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.raw_hint = raw_hint
        self.worker_status = worker_status


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


def _boss_access_error_kind(exc: BossAccessLimitedError) -> str:
    text = f"{exc} {exc.raw_hint or ''}"
    if any(marker in text for marker in ("登录", "__zp_stoken__", "验证码", "人机验证")):
        return "login_required"
    if any(marker in text for marker in ("Chrome", "DevTools", "tab", "浏览器")):
        return "chrome_unhealthy"
    return "proxy_limited"


def _single_profile_concurrency(value: Optional[int] = None) -> int:
    """返回单 Chrome profile 内允许的 BOSS 并发。

    这里刻意拒绝静默 clamp：如果把总 worker 数误写到
    ``BOSS_ZHIPIN_MAX_CONCURRENCY``，服务应明确报配置错误，避免单 profile
    被误打到 3+ 并发后触发 stoken churn 和全局熔断。
    """
    raw = _settings.BOSS_ZHIPIN_MAX_CONCURRENCY if value is None else value
    concurrency = max(1, int(raw))
    if concurrency > _MAX_SINGLE_PROFILE_CONCURRENCY:
        raise ValueError(
            "BOSS 单 Chrome profile 并发不能超过 "
            f"{_MAX_SINGLE_PROFILE_CONCURRENCY}；如需更高总并发，请配置多个 "
            "BOSS_ZHIPIN_WORKERS（独立账号/profile/代理），不要调大 "
            "BOSS_ZHIPIN_MAX_CONCURRENCY。"
        )
    return concurrency


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
        try:
            resp = self._http.get(url, params=params, headers=headers)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise BossAccessLimitedError(
                f"BOSS 直连代理请求失败: {type(exc).__name__}"
            ) from exc
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

    def __init__(
        self,
        *,
        worker_id: str = "default",
        browser_host_port: Optional[str] = None,
        profile_id: Optional[str] = None,
        proxy_id: Optional[str] = None,
        proxy_url: Optional[str] = None,
        chrome_proxy_server: Optional[str] = None,
        per_worker_concurrency: Optional[int] = None,
    ) -> None:
        self.worker_id = worker_id
        self.profile_id = profile_id or worker_id
        self.proxy_id = proxy_id
        self.chrome_proxy_server = chrome_proxy_server
        self._browser_host_port = browser_host_port or _settings.BOSS_ZHIPIN_BROWSER_HOST_PORT
        self._proxy_url = proxy_url
        # 并发闸门：默认 1=严格串行（历史行为）。>1 时允许 N 个 scrape 并发。
        self._concurrency = _single_profile_concurrency(per_worker_concurrency)
        self._sema = asyncio.Semaphore(self._concurrency)
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
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        self._scrape_many_sync,
                        keywords,
                        city_codes,
                        max_pages,
                        max_items_per_query,
                        include_raw,
                        include_description,
                        start_page,
                        page_size,
                    ),
                    timeout=_settings.BOSS_ZHIPIN_SYNC_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError as exc:
                raise BossAccessLimitedError(
                    f"BOSS 同步抓取超时（{_settings.BOSS_ZHIPIN_SYNC_TIMEOUT_SEC:g}s）"
                ) from exc

    async def shutdown(self) -> None:
        """释放持久化浏览器 tab / httpx client（由 lifespan 调用）。"""
        async with self._lock:
            await asyncio.to_thread(self._shutdown_sync)

    async def probe_ready(self) -> None:
        """轻量验证 worker 的浏览器与直连 cookie 是否可用。

        只做资源初始化和 cookie 铸造，不拉取职位列表，避免恢复动作本身增加抓取压力。
        """
        await asyncio.to_thread(self._probe_ready_sync)

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

                logger.info("BOSS worker=%s 持久化 tab 初始化 …", self.worker_id)
                self._page = ChromiumPage(
                    self._browser_host_port
                ).new_tab()
                # tab 重建后旧会话池作废，强制重建以绑定新 tab。
                self._drain_pool_locked()

            if direct_enabled:
                concurrency = self._concurrency
                if self._session_pool is None or self._pool_size != concurrency:
                    self._build_pool_locked(concurrency)

    def _build_pool_locked(self, concurrency: int) -> None:
        """（持有 tab_lock 时调用）为每个并发槽建独立 httpx client + 会话。"""
        self._drain_pool_locked()
        pool: "queue.Queue[_DirectBossSession]" = queue.Queue()
        for _ in range(concurrency):
            http = httpx.Client(
                timeout=_settings.BOSS_ZHIPIN_DIRECT_HTTP_TIMEOUT,
                trust_env=False,
                proxy=self._proxy_url or None,
            )
            self._http_clients.append(http)
            pool.put(_DirectBossSession(self._page, http, self._tab_lock))
        self._session_pool = pool
        self._pool_size = concurrency
        logger.info(
            "BOSS worker=%s 直连会话池就绪：并发槽=%s proxy=%s",
            self.worker_id,
            concurrency,
            "已配置" if self._proxy_url else "直连",
        )

    def _probe_ready_sync(self) -> None:
        direct_enabled = _settings.BOSS_ZHIPIN_DIRECT_ENABLED
        self._ensure_resources(direct_enabled)
        if not direct_enabled or self._session_pool is None:
            return
        session = self._session_pool.get()
        try:
            session._refresh_cookies(force=True)
        finally:
            self._session_pool.put(session)

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
        summary["worker_id"] = self.worker_id
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


class BossWorkerPoolClient:
    """BOSS 多账号 / 多 profile worker 池。

    对外保持 ``BossZhipinClient.scrape_many`` 合同；内部把每次采集分配给一个
    healthy worker。单 worker 触发访问受限时只冷却该 worker，避免旧的全局单点
    风控把所有账号一起停掉。
    """

    def __init__(
        self,
        workers: List[Any],
        *,
        cooldown_seconds: Optional[int] = None,
        proxy_pool: Optional[BossProxyPool] = None,
        runtime_manager: Optional[BossWorkerRuntimeManager] = None,
        worker_factory=None,
        recover_failed_workers: bool = False,
    ) -> None:
        if not workers:
            raise ValueError("BossWorkerPoolClient 至少需要一个 worker")
        self._workers = list(workers)
        self._proxy_pool = proxy_pool
        self._runtime_manager = runtime_manager
        self._worker_factory = worker_factory or self._default_worker_factory
        self._recover_failed_workers = bool(recover_failed_workers)
        self._lock = threading.Lock()
        self._cursor = 0
        self._cooldown_until: Dict[str, float] = {}
        self._cooldown_reason: Dict[str, str] = {}
        self._recovering: set[str] = set()
        self._login_required: set[str] = set()
        self._previous_proxy_id: Dict[str, str] = {}
        self._recovery_attempts: Dict[str, int] = {}
        self._last_recovery_error: Dict[str, str] = {}
        self._runtime_snapshot: Dict[str, Dict[str, Any]] = {}
        runtime_snapshot = getattr(runtime_manager, "snapshot", None) if runtime_manager is not None else None
        if runtime_snapshot is not None:
            for worker in self._workers:
                snapshot = runtime_snapshot(self._worker_id(worker))
                snapshot_proxy_id = snapshot.get("proxy_id") if snapshot else None
                worker_proxy_id = getattr(worker, "proxy_id", None)
                if snapshot and (not snapshot_proxy_id or snapshot_proxy_id == worker_proxy_id):
                    self._runtime_snapshot[self._worker_id(worker)] = snapshot
        self._in_flight: Dict[str, int] = {
            self._worker_id(worker): 0 for worker in self._workers
        }
        self._cooldown_seconds = (
            cooldown_seconds
            if cooldown_seconds is not None
            else _settings.REGION_JOBS_BOSS_COOLDOWN_MINUTES * 60
        )

    async def scrape_many(self, *args, **kwargs) -> Dict[str, Any]:
        attempted: set[str] = set()
        recovery_retries: Dict[str, int] = {}
        last_error: Optional[BossAccessLimitedError] = None
        while True:
            worker = self._acquire_worker(exclude=attempted)
            if worker is None:
                reason = str(last_error) if last_error else "无可用 worker"
                status = self.worker_status()
                retry_after = self._next_retry_after_seconds()
                raise BossAccessLimitedError(
                    f"全部 BOSS worker 均不可用：{reason}",
                    retry_after_seconds=retry_after,
                    worker_status=status,
                )

            worker_id = self._worker_id(worker)
            attempted.add(worker_id)
            try:
                result = await worker.scrape_many(*args, **kwargs)
                self._record_success(worker_id)
                return self._attach_worker_status(result, worker_id, self.worker_status())
            except BossAccessLimitedError as exc:
                last_error = exc
                error_kind = _boss_access_error_kind(exc)
                if error_kind == "login_required":
                    self._record_login_required(worker_id, reason=str(exc))
                    continue
                recovered = await self._recover_worker(worker_id, exc)
                if recovered:
                    if (
                        not self._has_available_worker(exclude=attempted)
                        and recovery_retries.get(worker_id, 0) < 1
                    ):
                        recovery_retries[worker_id] = recovery_retries.get(worker_id, 0) + 1
                        attempted.discard(worker_id)
                else:
                    self._record_cooldown(
                        worker_id,
                        seconds=self._cooldown_seconds_for_error(exc, error_kind),
                        reason=str(exc),
                    )
            finally:
                self._release_worker(worker_id)

    async def shutdown(self) -> None:
        for worker in self._workers:
            shutdown = getattr(worker, "shutdown", None)
            if shutdown is not None:
                await shutdown()

    def worker_status(self) -> Dict[str, Dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            statuses: Dict[str, Dict[str, Any]] = {}
            for worker in self._workers:
                worker_id = self._worker_id(worker)
                cooldown_until = self._cooldown_until.get(worker_id)
                cooling = bool(cooldown_until and cooldown_until > now)
                proxy_id = getattr(worker, "proxy_id", None)
                proxy_info = self._proxy_status(proxy_id)
                runtime_snapshot = self._runtime_snapshot.get(worker_id, {})
                if worker_id in self._login_required:
                    state = "login_required"
                elif worker_id in self._recovering:
                    state = "recovering"
                elif cooling:
                    state = "cooldown"
                else:
                    state = "healthy"
                statuses[worker_id] = {
                    "state": state,
                    "in_flight": self._in_flight.get(worker_id, 0),
                    "cooldown_remaining_seconds": (
                        int(cooldown_until - now) if cooling and cooldown_until else 0
                    ),
                    "reason": (
                        self._cooldown_reason.get(worker_id)
                        if cooling or worker_id in self._login_required
                        else None
                    ),
                    "proxy_id": proxy_id,
                    "previous_proxy_id": self._previous_proxy_id.get(worker_id),
                    "proxy_state": proxy_info.get("state") if proxy_info else None,
                    "local_proxy_url_masked": (
                        proxy_info.get("local_proxy_url_masked") if proxy_info else None
                    ),
                    "upstream_label": proxy_info.get("upstream_label") if proxy_info else None,
                    "recovery_attempts": self._recovery_attempts.get(worker_id, 0),
                    "last_recovery_error": self._last_recovery_error.get(worker_id),
                    "chrome_pid": runtime_snapshot.get("pid"),
                    "devtools_ok": runtime_snapshot.get("devtools_ok"),
                }
            return statuses

    def _next_retry_after_seconds(self) -> Optional[int]:
        now = time.monotonic()
        with self._lock:
            remainings = [
                int(until - now)
                for until in self._cooldown_until.values()
                if until > now
            ]
        return max(1, min(remainings)) if remainings else None

    def _acquire_worker(self, *, exclude: set[str]) -> Optional[Any]:
        now = time.monotonic()
        with self._lock:
            worker_count = len(self._workers)
            for offset in range(worker_count):
                index = (self._cursor + offset) % worker_count
                worker = self._workers[index]
                worker_id = self._worker_id(worker)
                if worker_id in exclude:
                    continue
                if worker_id in self._recovering or worker_id in self._login_required:
                    continue
                cooldown_until = self._cooldown_until.get(worker_id)
                if cooldown_until and cooldown_until > now:
                    continue
                if cooldown_until and cooldown_until <= now:
                    self._cooldown_until.pop(worker_id, None)
                    self._cooldown_reason.pop(worker_id, None)
                self._cursor = (index + 1) % worker_count
                self._in_flight[worker_id] = self._in_flight.get(worker_id, 0) + 1
                return worker
            return None

    def _has_available_worker(self, *, exclude: set[str]) -> bool:
        now = time.monotonic()
        with self._lock:
            for worker in self._workers:
                worker_id = self._worker_id(worker)
                if worker_id in exclude:
                    continue
                if worker_id in self._recovering or worker_id in self._login_required:
                    continue
                cooldown_until = self._cooldown_until.get(worker_id)
                if cooldown_until and cooldown_until > now:
                    continue
                return True
            return False

    def _release_worker(self, worker_id: str) -> None:
        with self._lock:
            self._in_flight[worker_id] = max(0, self._in_flight.get(worker_id, 0) - 1)

    def _record_success(self, worker_id: str) -> None:
        with self._lock:
            self._cooldown_until.pop(worker_id, None)
            self._cooldown_reason.pop(worker_id, None)

    def _record_login_required(self, worker_id: str, *, reason: str) -> None:
        with self._lock:
            self._login_required.add(worker_id)
            self._cooldown_until.pop(worker_id, None)
            self._cooldown_reason[worker_id] = reason
        logger.warning("[boss-worker-pool] worker=%s 需要人工登录/验证：%s", worker_id, reason)

    def _record_cooldown(self, worker_id: str, *, seconds: int, reason: str) -> None:
        until = time.monotonic() + max(1, int(seconds))
        with self._lock:
            self._cooldown_until[worker_id] = until
            self._cooldown_reason[worker_id] = reason
        proxy_id = self._worker_proxy_id(worker_id)
        if self._proxy_pool is not None and proxy_id:
            self._proxy_pool.mark_cooldown(proxy_id, reason=reason, seconds=seconds)
        logger.warning(
            "[boss-worker-pool] worker=%s 冷却 %ss，原因：%s",
            worker_id,
            seconds,
            reason,
        )

    def _cooldown_seconds_for_error(self, exc: BossAccessLimitedError, error_kind: str) -> int:
        if exc.retry_after_seconds:
            return exc.retry_after_seconds
        if error_kind == "chrome_unhealthy":
            return _settings.BOSS_ZHIPIN_CHROME_RECOVERY_COOLDOWN_MINUTES * 60
        if error_kind == "login_required":
            return _settings.BOSS_ZHIPIN_LOGIN_REQUIRED_COOLDOWN_MINUTES * 60
        return self._cooldown_seconds

    @staticmethod
    def _attach_worker_status(
        result: Dict[str, Any],
        worker_id: str,
        worker_status: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return result
        summary = result.setdefault("summary", {})
        if isinstance(summary, dict):
            summary["worker_id"] = worker_id
            summary["worker_status"] = worker_status
        return result

    @staticmethod
    def _worker_id(worker: Any) -> str:
        return str(getattr(worker, "worker_id", None) or id(worker))

    def _worker_proxy_id(self, worker_id: str) -> Optional[str]:
        for worker in self._workers:
            if self._worker_id(worker) == worker_id:
                return getattr(worker, "proxy_id", None)
        return None

    def _proxy_status(self, proxy_id: Optional[str]) -> Dict[str, Any]:
        if self._proxy_pool is None or not proxy_id:
            return {}
        return self._proxy_pool.status().get(proxy_id, {})

    async def _recover_worker(self, worker_id: str, exc: BossAccessLimitedError) -> bool:
        if (
            not self._recover_failed_workers
            or self._proxy_pool is None
            or self._runtime_manager is None
        ):
            return False

        old_worker = self._find_worker(worker_id)
        if old_worker is None:
            return False
        old_proxy_id = getattr(old_worker, "proxy_id", None)
        seconds = exc.retry_after_seconds or self._cooldown_seconds

        with self._lock:
            if worker_id in self._recovering:
                return False
            self._recovering.add(worker_id)
            self._recovery_attempts[worker_id] = self._recovery_attempts.get(worker_id, 0) + 1

        try:
            lease = self._proxy_pool.reassign_worker(
                worker_id,
                bad_proxy_id=old_proxy_id,
                reason=str(exc),
                seconds=seconds,
            )
            shutdown = getattr(old_worker, "shutdown", None)
            if shutdown is not None:
                await shutdown()

            config = self._runtime_config_for_worker(old_worker)
            snapshot = await asyncio.to_thread(
                self._runtime_manager.restart_worker,
                config,
                proxy_id=lease.proxy_id,
                chrome_proxy_server=lease.chrome_proxy_server,
            )
            if snapshot and snapshot.get("devtools_ok") is False:
                raise RuntimeError("Chrome DevTools 端口未恢复")

            new_worker = self._worker_factory(old_worker, lease)
            probe = getattr(new_worker, "probe_ready", None)
            if probe is not None:
                await probe()
            self._replace_worker(worker_id, new_worker)
            with self._lock:
                if old_proxy_id:
                    self._previous_proxy_id[worker_id] = old_proxy_id
                self._runtime_snapshot[worker_id] = snapshot or {}
                self._cooldown_until.pop(worker_id, None)
                self._cooldown_reason.pop(worker_id, None)
                self._last_recovery_error.pop(worker_id, None)
                self._login_required.discard(worker_id)
            logger.info(
                "[boss-worker-pool] worker=%s 已切换代理 %s -> %s 并完成 Chrome 重启",
                worker_id,
                old_proxy_id,
                lease.proxy_id,
            )
            return True
        except Exception as recovery_exc:
            if self._proxy_pool is not None:
                self._proxy_pool.release_worker(worker_id)
            with self._lock:
                self._last_recovery_error[worker_id] = str(recovery_exc)
                recovery_text = f"{exc} {recovery_exc}"
                if "登录" in recovery_text or "stoken" in recovery_text:
                    self._login_required.add(worker_id)
            logger.warning(
                "[boss-worker-pool] worker=%s 自愈换代理失败：%s",
                worker_id,
                recovery_exc,
            )
            return False
        finally:
            with self._lock:
                self._recovering.discard(worker_id)

    def _find_worker(self, worker_id: str) -> Optional[Any]:
        for worker in self._workers:
            if self._worker_id(worker) == worker_id:
                return worker
        return None

    def _replace_worker(self, worker_id: str, new_worker: Any) -> None:
        with self._lock:
            for index, worker in enumerate(self._workers):
                if self._worker_id(worker) == worker_id:
                    self._workers[index] = new_worker
                    self._in_flight.setdefault(worker_id, 0)
                    return

    @staticmethod
    def _runtime_config_for_worker(worker: Any) -> WorkerRuntimeConfig:
        return WorkerRuntimeConfig(
            worker_id=str(getattr(worker, "worker_id")),
            browser_host_port=str(getattr(worker, "_browser_host_port")),
            profile_id=str(getattr(worker, "profile_id", getattr(worker, "worker_id"))),
        )

    @staticmethod
    def _default_worker_factory(old_worker: Any, lease: ProxyLease) -> BossZhipinClient:
        return BossZhipinClient(
            worker_id=str(getattr(old_worker, "worker_id")),
            browser_host_port=str(getattr(old_worker, "_browser_host_port")),
            profile_id=str(getattr(old_worker, "profile_id", getattr(old_worker, "worker_id"))),
            proxy_id=lease.proxy_id,
            proxy_url=lease.local_proxy_url,
            chrome_proxy_server=lease.chrome_proxy_server,
            per_worker_concurrency=int(getattr(old_worker, "_concurrency", 1)),
        )


def _boss_proxy_health_checker():
    url = str(getattr(_settings, "BOSS_ZHIPIN_PROXY_HEALTHCHECK_URL", "") or "").strip()
    if not url:
        return None

    def _check(proxy_url: str) -> bool:
        try:
            with httpx.Client(
                timeout=_settings.BOSS_ZHIPIN_PROXY_HEALTHCHECK_TIMEOUT_SEC,
                trust_env=False,
                proxy=proxy_url,
            ) as client:
                resp = client.get(url)
            return 200 <= resp.status_code < 400
        except Exception as exc:
            logger.warning(
                "BOSS 代理健康检查失败 proxy=%s error=%s",
                "已配置" if proxy_url else "空",
                exc,
            )
            return False

    return _check


_configured_proxy_pool: Optional[BossProxyPool] = None


def _load_json_list_from_file(path_value: Optional[str], *, setting_name: str) -> Optional[List[Dict[str, Any]]]:
    if not path_value:
        return None
    path = Path(str(path_value))
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"{setting_name} 指向的文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{setting_name} 不是合法 JSON: {path}") from exc
    if not isinstance(value, list):
        raise ValueError(f"{setting_name} 必须是 JSON 数组: {path}")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{setting_name} 的每个元素必须是对象: {path}")
    return value


def _boss_config_list(env_attr: str, file_attr: str, setting_name: str) -> List[Dict[str, Any]]:
    file_value = getattr(_settings, file_attr, None)
    loaded = _load_json_list_from_file(file_value, setting_name=setting_name)
    if loaded is not None:
        logger.info("%s 已从文件加载: %s", setting_name, file_value)
        return loaded
    return list(getattr(_settings, env_attr, []) or [])


def _configured_boss_workers() -> List[BossZhipinClient]:
    global _configured_proxy_pool
    workers_config = _boss_config_list(
        "BOSS_ZHIPIN_WORKERS",
        "BOSS_ZHIPIN_WORKERS_FILE",
        "BOSS_ZHIPIN_WORKERS_FILE",
    )
    proxy_pool_config = _boss_config_list(
        "BOSS_ZHIPIN_PROXY_POOL",
        "BOSS_ZHIPIN_PROXY_POOL_FILE",
        "BOSS_ZHIPIN_PROXY_POOL_FILE",
    )
    _configured_proxy_pool = (
        BossProxyPool(
            proxy_pool_config,
            default_cooldown_seconds=_settings.BOSS_ZHIPIN_PROXY_COOLDOWN_MINUTES * 60,
            health_checker=_boss_proxy_health_checker(),
            selection_strategy=_settings.BOSS_ZHIPIN_PROXY_SELECTION_STRATEGY,
            recent_avoid_count=_settings.BOSS_ZHIPIN_PROXY_RECENT_AVOID_COUNT,
        )
        if proxy_pool_config
        else None
    )
    workers: List[BossZhipinClient] = []
    multi_worker = len(workers_config) > 1
    seen_worker_ids: set[str] = set()
    seen_ports: set[str] = set()
    seen_profiles: set[str] = set()
    for index, item in enumerate(workers_config, start=1):
        if not isinstance(item, dict):
            raise ValueError("BOSS_ZHIPIN_WORKERS 的每个元素必须是对象")
        worker_id = str(item.get("worker_id") or item.get("id") or f"boss-{index}")
        browser_host_port = item.get("browser_host_port") or item.get("host_port")
        profile_id = item.get("profile_id") or item.get("account_id") or worker_id
        proxy_id = item.get("proxy_id")
        proxy_url = item.get("proxy_url")
        chrome_proxy_server = item.get("chrome_proxy_server")
        per_worker_concurrency = item.get("per_worker_concurrency", 1)
        if worker_id in seen_worker_ids:
            raise ValueError(f"BOSS_ZHIPIN_WORKERS worker_id 重复: {worker_id}")
        seen_worker_ids.add(worker_id)
        if multi_worker and not browser_host_port:
            raise ValueError("BOSS_ZHIPIN_WORKERS 多 worker 模式必须为每个 worker 配置 browser_host_port")
        if browser_host_port:
            browser_host_port = str(browser_host_port)
            if browser_host_port in seen_ports:
                raise ValueError(
                    f"BOSS_ZHIPIN_WORKERS browser_host_port 重复: {browser_host_port}"
                )
            seen_ports.add(browser_host_port)
        profile_id = str(profile_id)
        if profile_id in seen_profiles:
            raise ValueError(f"BOSS_ZHIPIN_WORKERS profile_id/account_id 重复: {profile_id}")
        seen_profiles.add(profile_id)
        if _configured_proxy_pool is not None:
            lease = _configured_proxy_pool.lease_for_worker(
                worker_id,
                requested_proxy_id=proxy_id,
                requested_proxy_url=proxy_url,
            )
            proxy_id = lease.proxy_id
            proxy_url = lease.local_proxy_url
            chrome_proxy_server = lease.chrome_proxy_server or chrome_proxy_server
        logger.info(
            "配置 BOSS worker: worker_id=%s profile=%s port=%s proxy=%s",
            worker_id,
            profile_id,
            browser_host_port,
            "已配置" if proxy_url else "直连",
        )
        workers.append(
            BossZhipinClient(
                worker_id=worker_id,
                browser_host_port=browser_host_port,
                profile_id=profile_id,
                proxy_id=proxy_id,
                proxy_url=proxy_url,
                chrome_proxy_server=chrome_proxy_server,
                per_worker_concurrency=per_worker_concurrency,
            )
        )
    return workers


# ══════════════════════════════════════════════════════════════════════
#  模块级单例：多个 router 共享同一个持久化 tab，避免重复占用浏览器
# ══════════════════════════════════════════════════════════════════════

_shared_client: Optional[Any] = None


def get_boss_client():
    """返回进程内共享的 BossZhipinClient 单例。"""
    global _shared_client
    if _shared_client is None:
        runtime_manager = (
            BossWorkerRuntimeManager(
                chrome_path=_settings.BOSS_ZHIPIN_CHROME_PATH,
                profile_root=_PROJECT_ROOT / _settings.BOSS_ZHIPIN_CHROME_PROFILE_ROOT,
                state_root=_PROJECT_ROOT / _settings.BOSS_ZHIPIN_WORKER_STATE_ROOT,
                devtools_timeout=_settings.BOSS_ZHIPIN_WORKER_DEVTOOLS_TIMEOUT_SEC,
            )
            if _settings.BOSS_ZHIPIN_MANAGE_CHROME_WORKERS
            else None
        )
        workers = _configured_boss_workers()
        _shared_client = (
            BossWorkerPoolClient(
                workers,
                proxy_pool=_configured_proxy_pool,
                runtime_manager=runtime_manager,
                recover_failed_workers=(
                    _settings.BOSS_ZHIPIN_RECOVER_WORKERS_ON_ACCESS_LIMIT
                    and runtime_manager is not None
                ),
            )
            if workers
            else BossZhipinClient()
        )
    return _shared_client
