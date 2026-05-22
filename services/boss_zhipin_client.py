# -*- coding: utf-8 -*-
"""
BOSS 直聘职位采集客户端。

实现原则：
- 复用本机已登录的 Chrome 调试端口；
- 只监听页面正常触发的职位列表 JSON；
- 不做验证码、token、环境校验等反爬绕过；
- 小批量串行采集，遇到异常立即返回给调用方处理。
"""

import asyncio
import json
import time
from random import uniform
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/jobs/boss_zhipin.log")

SEARCH_API_PATTERN = "/wapi/zpgeek/search/joblist.json"
SEARCH_PAGE_URL = "https://www.zhipin.com/web/geek/jobs"


class BossZhipinClient:
    """通过 DrissionPage 监听 BOSS 直聘搜索页职位接口。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

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

    def _scrape_many_sync(
        self,
        keywords: List[str],
        city_codes: List[int],
        max_pages: int,
        max_items_per_query: Optional[int],
        include_raw: bool,
        include_description: bool,
    ) -> Dict[str, Any]:
        from DrissionPage import ChromiumPage

        page = ChromiumPage(_settings.BOSS_ZHIPIN_BROWSER_HOST_PORT).new_tab()
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
                        body = self._fetch_page(page, keyword, city_code, page_num)
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
                                self._enrich_job_description(page, job)
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

                        time.sleep(uniform(
                            _settings.BOSS_ZHIPIN_MIN_DELAY_SEC,
                            _settings.BOSS_ZHIPIN_MAX_DELAY_SEC,
                        ))
        finally:
            try:
                page.close()
            except Exception as exc:
                logger.debug(f"关闭 BOSS 测试 tab 失败: {exc}")

        return {
            "summary": {
                "keywords": keywords,
                "city_codes": city_codes,
                "max_pages": max_pages,
                "max_items_per_query": max_items_per_query,
                "include_raw": include_raw,
                "include_description": include_description,
                "combinations": combos,
                "pages_fetched": pages_fetched,
                "total_jobs": len(jobs),
            },
            "jobs": jobs,
            "warnings": warnings,
        }

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
