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
            )

    def _scrape_many_sync(
        self,
        keywords: List[str],
        city_codes: List[int],
        max_pages: int,
        max_items_per_query: Optional[int],
        include_raw: bool,
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
                f"BOSS 职位接口未触发或超时: keyword={keyword}, city={city_code}, page={page_num}"
            )

        body = packet.response.body
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"BOSS 接口返回非 JSON 内容: {body[:120]}") from exc

        if not isinstance(body, dict):
            raise RuntimeError(f"BOSS 接口返回格式异常: {type(body).__name__}")

        code = body.get("code")
        if code != 0:
            message = body.get("message") or "未知错误"
            raise RuntimeError(f"BOSS 接口返回错误: code={code}, message={message}")

        return body

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
