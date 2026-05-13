# -*- coding: utf-8 -*-
"""
智联招聘 V2 客户端。

搜索策略：
1. 保持一个持久化的 DrissionPage 浏览器 tab
2. 对每次搜索，直接导航到 ``www.zhaopin.com/sou/jl{cityId}/kw{keyword}/p{page}``
3. 用 ``page.listen`` 拦截 ``/c/i/search/positions`` 的 API 响应
4. 若页面加载未触发 API 调用，则通过在搜索框按回车强制触发
5. 最终 fallback: 从页面 DOM 解析可见的职位卡片

已验证结论:
- ``/c/i/sou`` API 已废弃，始终返回空
- ``/c/i/search/positions`` 仍然有效，但只在浏览器交互时触发
- 搜索 URL 中关键词用 URL-encoded 中文: ``kw%E5%A4%A7%E6%95%B0%E6%8D%AE``
- 城市 API ``/c/i/city-page/user-city`` 走纯 httpx（无需 cookie）
"""

import asyncio
import json
import re
import time
from random import uniform
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/jobs/zhilian_v2.log")


# ══════════════════════════════════════════════════════════════════════
#  BrowserPool — 持久化浏览器 tab 管理
# ══════════════════════════════════════════════════════════════════════

class BrowserPool:
    """管理一个持久化的 DrissionPage 浏览器 tab。

    使用 asyncio.Lock 保证同一时刻只有一个协程操作浏览器。
    """

    def __init__(self) -> None:
        self._page = None
        self._lock = asyncio.Lock()
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready and self._page is not None

    async def startup(self) -> None:
        async with self._lock:
            if self._ready:
                return
            logger.info("BrowserPool 启动: 初始化浏览器 tab …")
            try:
                self._page = await asyncio.to_thread(self._init_browser)
                self._ready = True
                logger.info("BrowserPool 就绪")
            except Exception as exc:
                logger.error(f"BrowserPool 初始化失败: {exc}", exc_info=True)
                raise

    @staticmethod
    def _init_browser():
        from DrissionPage import ChromiumPage

        page = ChromiumPage(_settings.JOB_SEARCH_BROWSER_HOST_PORT).new_tab()
        page.get(_settings.ZHAOPIN_LIST_URL)
        time.sleep(3)
        _handle_login_if_needed(page)
        logger.info("浏览器 tab 已初始化并登录")
        return page

    async def navigate_and_listen(
        self,
        keyword: str,
        city_id: str,
        page_num: int = 1,
    ) -> Dict[str, Any]:
        """导航到搜索 URL 并拦截 ``/c/i/search/positions`` 的 API 响应。"""
        async with self._lock:
            if not self._ready:
                await self.startup()
            return await asyncio.to_thread(
                self._do_navigate_and_listen, keyword, city_id, page_num
            )

    def _do_navigate_and_listen(
        self, keyword: str, city_id: str, page_num: int
    ) -> Dict[str, Any]:
        from DrissionPage.common import Keys

        kw_encoded = quote(keyword, safe="")
        url = f"https://www.zhaopin.com/sou/jl{city_id}/kw{kw_encoded}/p{page_num}"

        # 先跳离智联，打破 SPA 客户端路由
        self._page.get("about:blank")
        time.sleep(0.3)
        self._page.get(url)
        time.sleep(3)

        # 策略 1: 立即提取 SSR positionList（纯中文关键词不需要登录即可 SSR）
        ssr_results = self._extract_ssr_position_list()
        if ssr_results:
            return {
                "code": 200,
                "data": {"list": ssr_results},
                "_source": "ssr",
            }

        # 策略 2: SSR 为空（含英文关键词需要登录态才有数据）
        # 通过搜索框输入关键词 + 回车，触发浏览器内 JS 发 API 调用
        logger.info(
            f"SSR 为空（关键词可能含英文），改用搜索框输入触发 API …"
        )
        return self._do_search_via_input(keyword, city_id, page_num)

    def _do_search_via_input(
        self, keyword: str, city_id: str, page_num: int
    ) -> Dict[str, Any]:
        """通过搜索框输入关键词触发搜索，用 listen 拦截 API 响应。

        当 SSR 为空（含英文关键词需登录）时：
        1. 回退到已知可用的初始搜索页（有完整搜索表单）
        2. 在搜索框输入关键词 + 回车
        3. listen 拦截 /c/i/search/positions 的 API 响应
        """
        from DrissionPage.common import By, Keys

        # 导航到初始搜索页（确保有可用的搜索表单）
        init_url = _settings.ZHAOPIN_LIST_URL
        self._page.get(init_url)
        time.sleep(2)

        api_pattern = "/c/i/search/positions?"
        self._page.listen.start(api_pattern)

        # 输入关键词
        input_selectors = [
            '//div[@class="query-search__content-input__wrap"]/input',
            'tag:input@@placeholder:搜索职位',
            'css:.query-search input',
        ]
        search_input = None
        for sel in input_selectors:
            try:
                search_input = self._page.ele(sel, timeout=3)
                if search_input:
                    break
            except Exception:
                continue

        if search_input:
            search_input.clear()
            time.sleep(0.3)
            search_input.input(keyword)
            time.sleep(0.3)
            search_input.input(Keys.ENTER)
            logger.info(f"已在搜索框输入 '{keyword}' 并回车")
            time.sleep(1)

            # 选择城市（通过 UI 交互）
            try:
                self._page.listen.clear()
                area_obj = self._page.ele(
                    (By.XPATH, '//div[@class="content-s"]/div[1]'), timeout=3
                )
                if area_obj:
                    area_obj.click()
                    time.sleep(0.5)
                    area_input = self._page.ele(
                        (By.XPATH, '//div[@class="query-other-city"]/input'),
                        timeout=3,
                    )
                    if area_input:
                        city_name = self._resolve_city_name(city_id)
                        if city_name:
                            area_input.input(city_name)
                            time.sleep(0.5)
                            first_item = self._page.ele(
                                (By.XPATH,
                                 '//ul[@class="query-other-city__list"]/li[1]'),
                                timeout=5,
                            )
                            if first_item:
                                first_item.click()
                                logger.info(f"已选择城市: {city_name}")
                                time.sleep(0.5)
            except Exception as exc:
                logger.debug(f"城市选择失败（不影响搜索）: {exc}")

            self._scroll_page()
        else:
            logger.warning("所有搜索框选择器均未命中")
            self._scroll_page()

        packet = self._page.listen.wait(timeout=15)
        self._page.listen.stop()

        if packet and hasattr(packet, "response") and packet.response:
            body = packet.response.body
            if isinstance(body, dict):
                count = len(body.get("data", {}).get("list", []))
                logger.info(f"listen 拦截成功: {count} 条")
                return body
            if isinstance(body, str):
                try:
                    return json.loads(body)
                except Exception:
                    pass

        logger.warning("搜索框输入后 listen 仍未捕获 API 响应")
        return {"code": -1, "data": {"list": []}}

    def _resolve_city_name(self, city_id: str) -> Optional[str]:
        """根据 cityId 反查城市名（用于 UI 选择城市）。"""
        id_to_name = {
            "530": "北京", "538": "上海", "765": "深圳", "763": "广州",
            "801": "成都", "749": "杭州", "551": "天津", "600": "武汉",
            "613": "南京", "635": "重庆", "653": "西安", "719": "长沙",
            "736": "厦门", "702": "合肥", "854": "东莞",
        }
        return id_to_name.get(city_id)

    def _extract_ssr_position_list(self) -> List[Dict[str, Any]]:
        """提取 __INITIAL_STATE__.positionList（纯中文关键词可 SSR）。"""
        try:
            state = self._page.run_js("return window.__INITIAL_STATE__;")
            if not state or not isinstance(state, dict):
                return []
            pos_list = state.get("positionList")
            if isinstance(pos_list, list) and pos_list:
                logger.info(
                    f"SSR 提取成功: {len(pos_list)} 条 "
                    f"(positionCount={state.get('positionCount', '?')})"
                )
                return pos_list
        except Exception as exc:
            logger.warning(f"SSR 提取异常: {exc}")
        return []

    def _scroll_page(self) -> None:
        """滚动页面触发完整渲染。"""
        for fraction in (0.3, 0.6, 0.9):
            js = (
                "document.documentElement.scrollTop = "
                f"document.documentElement.scrollHeight * {fraction}"
            )
            self._page.run_js(js)
            time.sleep(uniform(0.3, 0.6))

    def _extract_from_dom(self) -> List[Dict[str, Any]]:
        """从页面渲染的 DOM 中解析职位卡片数据。

        智联搜索结果页的职位卡片通常是 <a> 标签，类名包含 ``joblist-box__item``。
        此方法尝试多种常见选择器，解析可见的职位信息。
        """
        results: List[Dict[str, Any]] = []

        # 优先直接提取 __INITIAL_STATE__.positionList（已确认结构）
        try:
            state = self._page.run_js("return window.__INITIAL_STATE__;")
            if state and isinstance(state, dict):
                pos_list = state.get("positionList")
                if isinstance(pos_list, list) and pos_list:
                    logger.info(
                        f"从 __INITIAL_STATE__.positionList 直接提取 "
                        f"{len(pos_list)} 条（positionCount={state.get('positionCount')}）"
                    )
                    return pos_list
                logger.warning(
                    f"__INITIAL_STATE__ 存在但 positionList 为空/缺失, "
                    f"top keys: {list(state.keys())[:10]}, "
                    f"positionList type={type(pos_list).__name__}"
                )
        except Exception as exc:
            logger.warning(f"__INITIAL_STATE__ 提取异常: {exc}")

        # 通用 SSR 状态提取
        for var in ("window.__NEXT_DATA__", "window.__APP_DATA__"):
            try:
                data = self._page.run_js(f"return {var};")
                if data and isinstance(data, dict):
                    logger.info(f"从 {var} 提取到 SSR 状态")
                    items = self._dig_list_from_state(data)
                    if items:
                        return items
            except Exception:
                continue

        # 尝试从 <script> 标签提取序列化的 JSON 状态
        try:
            scripts = self._page.eles("tag:script")
            for s in scripts:
                text = s.text or ""
                if len(text) > 200 and (
                    '"positionList"' in text
                    or '"searchResult"' in text
                    or '"list"' in text
                    or '"numFound"' in text
                ):
                    json_str = self._extract_json_from_script(text)
                    if json_str:
                        try:
                            parsed = json.loads(json_str)
                            items = self._dig_list_from_state(parsed)
                            if items:
                                logger.info("从 <script> 标签提取到搜索数据")
                                return items
                        except Exception:
                            continue
        except Exception as exc:
            logger.debug(f"script 标签扫描异常: {exc}")

        # 最后手段：解析 DOM 元素
        card_selectors = [
            '.joblist-box__item',
            '.positionlist .sou-job-item',
            '.sou-job-list .sou-job-item',
            'a[data-itemid]',
        ]
        for selector in card_selectors:
            try:
                cards = self._page.eles(selector, timeout=2)
                if cards:
                    logger.info(f"通过 '{selector}' 找到 {len(cards)} 个职位卡片")
                    for card in cards:
                        job = self._parse_card_element(card)
                        if job:
                            results.append(job)
                    if results:
                        return results
            except Exception:
                continue

        logger.warning("DOM 解析也未提取到职位数据")
        return results

    @staticmethod
    def _dig_list_from_state(data: dict) -> Optional[List[Dict]]:
        """从嵌套的 SSR 状态对象中递归查找职位列表。"""
        if not isinstance(data, dict):
            return None
        for key in ("list", "results", "items", "positionList"):
            v = data.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                if any(k in v[0] for k in ("name", "jobName", "salary", "salary60")):
                    return v
        for key in (
            "data", "searchResult", "searchData", "positionResult",
            "props", "pageProps", "serverData",
        ):
            child = data.get(key)
            if isinstance(child, dict):
                found = BrowserPool._dig_list_from_state(child)
                if found:
                    return found
        return None

    @staticmethod
    def _extract_json_from_script(text: str) -> Optional[str]:
        """从 script 内容中提取 JSON 字符串。"""
        for pattern in (
            r"__INITIAL_STATE__\s*=\s*",
            r"__NEXT_DATA__\s*=\s*",
            r"window\.__APP_DATA__\s*=\s*",
        ):
            m = re.search(pattern, text)
            if m:
                start = m.end()
                candidate = text[start:].strip().rstrip(";")
                if candidate.startswith("{"):
                    return candidate
        return None

    @staticmethod
    def _parse_card_element(card) -> Optional[Dict[str, Any]]:
        """从单个职位卡片 DOM 元素中提取数据。"""
        try:
            name_el = card.ele('.iteminfo__line1__jobname', timeout=0.5)
            name = name_el.text if name_el else None
            if not name:
                name_el = card.ele('tag:span@@class:iteminfo__line1__jobname__name', timeout=0.5)
                name = name_el.text if name_el else None
            if not name:
                return None

            salary_el = card.ele('.iteminfo__line1__salary', timeout=0.5)
            salary = salary_el.text if salary_el else ""

            loc_el = card.ele('.iteminfo__line2__jobdesc__city', timeout=0.5)
            address = loc_el.text if loc_el else ""

            exp_el = card.ele('.iteminfo__line2__jobdesc__demand__experience', timeout=0.5)
            exp = exp_el.text if exp_el else ""

            edu_el = card.ele('.iteminfo__line2__jobdesc__demand__education', timeout=0.5)
            edu = edu_el.text if edu_el else ""

            company_el = card.ele('.iteminfo__line1__compname', timeout=0.5)
            company_name = company_el.text if company_el else ""

            href = card.attr("href") or ""
            position_number = ""
            if "/jobs/" in href:
                parts = href.split("/jobs/")
                if len(parts) > 1:
                    position_number = parts[1].rstrip("/").split(".")[0]

            return {
                "name": name,
                "salary": salary,
                "jobSkillTags": [],
                "jobKnowledgeWelfareFeatures": None,
                "province": "",
                "address": address,
                "workingExp": exp,
                "education": edu,
                "companyName": company_name,
                "companyLogo": None,
                "companyUrl": None,
                "positionURL": href,
                "positionNumber": position_number,
                "companySize": "",
                "propertyName": "",
                "industryName": "",
            }
        except Exception:
            return None

    async def shutdown(self) -> None:
        async with self._lock:
            if self._page:
                try:
                    await asyncio.to_thread(self._page.close)
                except Exception:
                    pass
                self._page = None
            self._ready = False
            logger.info("BrowserPool 已关闭")


def _handle_login_if_needed(page) -> None:
    """检测登录弹窗并自动登录（复用 v1 逻辑）。"""
    try:
        login_popup = page.ele('//div[@class="pass-login-container"]', timeout=3)
        if login_popup and login_popup.is_displayed():
            logger.info("检测到登录弹窗，正在自动登录 …")
            page.ele(
                '//div[@class="pass-login-tab-item pass-login-tab-item__password"]'
            ).click()
            time.sleep(0.5)
            page.ele('//input[@placeholder="请输入手机号或邮箱"]').input(
                _settings.ZHILIAN_USERNAME or ""
            )
            time.sleep(0.5)
            page.ele('//input[@placeholder="请输入密码"]').input(
                _settings.ZHILIAN_PASSWORD or ""
            )
            time.sleep(0.5)
            page.ele('//button[contains(@class, "pass-login-submit")]').click()
            logger.info("已提交登录信息，等待跳转 …")
            time.sleep(3)
            logger.info("自动登录完成。")
    except Exception:
        logger.info("未检测到登录弹窗或已登录，继续。")


# ══════════════════════════════════════════════════════════════════════
#  CityResolver — 纯 httpx，不需要浏览器
# ══════════════════════════════════════════════════════════════════════

class CityResolver:
    """省份/城市名 → cityId。

    使用独立的 httpx 请求（不带浏览器 cookie），
    因为 user-city API 会被 cookie 中的城市偏好覆盖结果。
    """

    def __init__(self) -> None:
        self._cache: Dict[str, str] = {}

    async def resolve(self, name: str) -> Optional[str]:
        if name in self._cache:
            return self._cache[name]
        base_url = _settings.ZHAOPIN_CITY_API_TEMPLATE.split("?")[0]
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(base_url, params={"ipCity": name})
            if resp.status_code == 200:
                data = resp.json()
                code = data.get("data", {}).get("code")
                if code:
                    self._cache[name] = str(code)
                    logger.info(f"城市解析: {name} -> cityId={code}")
                    return str(code)
            logger.warning(f"城市解析失败: {name}, status={resp.status_code}")
        except Exception as exc:
            logger.warning(f"城市解析异常: {name}, {exc}")
        return None


# ══════════════════════════════════════════════════════════════════════
#  ZhaopinSearchClient
# ══════════════════════════════════════════════════════════════════════

class ZhaopinSearchClient:
    """智联招聘 V2 搜索客户端。

    - 列表搜索: BrowserPool（导航 + listen 拦截 + DOM 解析 fallback）
    - 城市解析: CityResolver（纯 httpx）
    - 职位详情: 纯 httpx（不需要浏览器 session）
    """

    def __init__(self, browser_pool: BrowserPool) -> None:
        self._browser = browser_pool
        self._city = CityResolver()
        self._detail_semaphore = asyncio.Semaphore(
            _settings.JOB_SEARCH_V2_HTTP_CONCURRENCY
        )

    async def startup(self) -> None:
        await self._browser.startup()

    async def shutdown(self) -> None:
        await self._browser.shutdown()

    # ─────────────── 职位列表搜索 ───────────────

    async def search_positions(
        self,
        keyword: str,
        city_id: str,
        *,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """单页列表搜索：导航到搜索 URL + listen 拦截 + DOM 解析 fallback。"""
        result = await self._browser.navigate_and_listen(keyword, city_id, page)

        source = result.get("_source", "listen")
        data = result.get("data", {})
        results = data.get("list") or data.get("results") or []

        if not results and isinstance(data, dict):
            for key in ("searchResult", "positionList", "jobList"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    results = (
                        nested.get("list")
                        or nested.get("results")
                        or nested.get("items")
                        or []
                    )
                    if results:
                        break
                elif isinstance(nested, list):
                    results = nested
                    break

        logger.info(
            f"列表搜索: {keyword}/cityId={city_id} p{page} "
            f"→ {len(results)} 条 (source={source})"
        )
        return results

    # ─────────────── 职位详情 ───────────────

    async def fetch_detail(
        self, position_number: str
    ) -> Optional[Dict[str, Any]]:
        if not position_number:
            return None
        url = _settings.ZHAOPIN_DETAIL_API_TEMPLATE.format(
            number=position_number
        )
        try:
            async with self._detail_semaphore:
                async with httpx.AsyncClient(
                    headers={"User-Agent": _settings.FETCH_USER_AGENT},
                    timeout=_settings.JOB_SEARCH_V2_HTTP_TIMEOUT,
                ) as client:
                    resp = await client.get(url)
            if resp.status_code == 200:
                body = resp.json()
                if body.get("code") == 200 and "data" in body:
                    return body["data"]
                logger.warning(
                    f"详情 {position_number} 业务码: code={body.get('code')}"
                )
            else:
                logger.warning(f"详情 {position_number} HTTP {resp.status_code}")
        except Exception as exc:
            logger.error(f"详情 {position_number} 异常: {exc}")
        return None

    # ─────────────── 单组合全量采集 ───────────────

    async def scrape_combination(
        self,
        keyword: str,
        province: str,
        max_pages: int,
    ) -> List[Dict[str, Any]]:
        """一个"关键词 × 省份"组合，按页拉列表 + 并发拉详情。"""
        city_id = await self._city.resolve(province)
        if not city_id:
            logger.warning(f"[v2] 无法解析城市 '{province}'，跳过")
            return []

        all_jobs: List[Dict[str, Any]] = []

        for page_num in range(1, max_pages + 1):
            t0 = time.monotonic()
            raw_list = await self.search_positions(
                keyword, city_id, page=page_num
            )
            cost = round(time.monotonic() - t0, 2)
            logger.info(
                f"[v2] {keyword}|{province}(cityId={city_id}) "
                f"p{page_num}: {len(raw_list)} 条, {cost}s"
            )

            if not raw_list:
                break

            page_jobs = self._normalize_list(raw_list, province)

            position_numbers = [
                j.get("positionNumber")
                for j in page_jobs
                if j.get("positionNumber")
            ]
            if position_numbers:
                details = await asyncio.gather(
                    *(self.fetch_detail(pn) for pn in position_numbers)
                )
                detail_map = dict(zip(position_numbers, details))
                for job in page_jobs:
                    pn = job.get("positionNumber")
                    job["job_details"] = detail_map.get(pn)
                ok = sum(1 for d in details if d)
                logger.info(
                    f"[v2] {keyword}|{province} p{page_num} "
                    f"详情 {ok}/{len(position_numbers)}"
                )

            all_jobs.extend(page_jobs)

            if page_num < max_pages:
                await asyncio.sleep(uniform(0.3, 0.8))

        return all_jobs

    @staticmethod
    def _normalize_list(
        raw_list: List[Dict[str, Any]], province: str
    ) -> List[Dict[str, Any]]:
        """把列表接口或 DOM 解析的原始字段映射为 v1 兼容格式。"""
        result = []
        for data in raw_list:
            # cardCustomJson → address
            address = None
            card_json = data.get("cardCustomJson")
            if card_json:
                try:
                    if isinstance(card_json, str):
                        address = json.loads(card_json).get("address")
                    elif isinstance(card_json, dict):
                        address = card_json.get("address")
                except Exception:
                    pass

            # workingExp 可能是 str 或 dict
            working_exp = data.get("workingExp")
            if isinstance(working_exp, dict):
                working_exp = working_exp.get("name", "")

            # education 同理
            education = data.get("education") or data.get("eduLevel")
            if isinstance(education, dict):
                education = education.get("name", "")

            # skill tags
            skill_tags = (
                data.get("jobSkillTags")
                or data.get("skillLabel")
                or []
            )
            if skill_tags and isinstance(skill_tags[0], dict):
                skill_tags = [t.get("name", "") for t in skill_tags]

            result.append({
                "name": data.get("name") or data.get("jobName"),
                "salary": data.get("salary60") or data.get("salary"),
                "jobSkillTags": skill_tags,
                "jobKnowledgeWelfareFeatures": data.get(
                    "jobKnowledgeWelfareFeatures"
                ),
                "province": province,
                "address": address or data.get("address", ""),
                "workingExp": working_exp,
                "education": education,
                "companyName": data.get("companyName"),
                "companyLogo": data.get("companyLogo"),
                "companyUrl": data.get("companyUrl"),
                "positionURL": data.get("positionURL")
                or data.get("positionUrl"),
                "positionNumber": data.get("number")
                or data.get("positionNumber"),
                "companySize": data.get("companySize", ""),
                "propertyName": data.get("propertyName", ""),
                "industryName": data.get("industryName", ""),
            })
        return result

    # ─────────────── 批量入口 ───────────────

    async def scrape_many(
        self,
        keywords: List[str],
        provinces: List[str],
        page_size: int,
    ) -> List[Dict[str, Any]]:
        """顺序执行多个组合（浏览器操作由 Lock 串行化），汇总结果。"""
        all_data: List[Dict[str, Any]] = []
        for kw in keywords:
            for prov in provinces:
                try:
                    result = await self.scrape_combination(
                        kw, prov, page_size
                    )
                    all_data.extend(result)
                except Exception as exc:
                    logger.error(
                        f"[v2] {kw}|{prov} 失败: {exc}", exc_info=True
                    )
        return all_data
