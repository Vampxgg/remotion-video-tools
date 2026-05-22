# -*- coding: utf-8 -*-
"""区域岗位数据统一获取接口。"""

import asyncio
import re
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from api.job_search_v2 import get_search_client as get_zhilian_client
from services.boss_zhipin_client import BossZhipinClient
from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/jobs/region_search.log")

router = APIRouter()
_boss_client = BossZhipinClient()


class SourceName(str, Enum):
    ZHILIAN = "zhilian"
    BOSS_ZHIPIN = "boss_zhipin"


class KeywordMode(str, Enum):
    ANY = "any"


class DetailLevel(str, Enum):
    SUMMARY = "summary"
    DESCRIPTION = "description"


class SourceErrorMode(str, Enum):
    CONTINUE = "continue"
    FAIL = "fail"


class RegionPlatformHints(BaseModel):
    zhilian_city_id: Optional[str] = Field(
        None,
        description="智联城市 ID；可选，不传时服务端按 city 解析",
        examples=["765"],
    )
    boss_city_code: Optional[int] = Field(
        None,
        description="BOSS 城市编码；可选，不传时服务端按 city 映射",
        examples=[101280600],
    )


class RegionSpec(BaseModel):
    country: str = Field("CN", description="国家/地区代码，第一版仅支持 CN")
    province: Optional[str] = Field(None, description="省份，例如 广东")
    city: str = Field(..., min_length=1, max_length=50, description="城市，例如 深圳")
    district: Optional[str] = Field(
        None,
        description="区县/区域；第一版只记录，不承诺平台级精准筛选",
    )
    platform_hints: RegionPlatformHints = Field(
        default_factory=RegionPlatformHints,
        description="平台编码提示；用于提高解析稳定性，不作为主输入",
    )

    @model_validator(mode="after")
    def _check_country(self):
        if self.country != "CN":
            raise ValueError("第一版仅支持 country=CN")
        return self


class QuerySpec(BaseModel):
    keywords: List[str] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="岗位关键词列表",
        examples=[["前端开发工程师"]],
    )
    keyword_mode: KeywordMode = Field(
        KeywordMode.ANY,
        description="关键词匹配模式；第一版仅支持 any",
    )


class CollectionOptions(BaseModel):
    max_pages_per_source: int = Field(
        1,
        ge=1,
        description="每个来源最多采集页数，不代表每页条数",
    )
    max_records_per_source: int = Field(
        20,
        ge=1,
        description="每个来源最多返回职位数",
    )
    detail_level: DetailLevel = Field(
        DetailLevel.SUMMARY,
        description="summary 只取列表字段；description 额外补岗位描述/职责",
    )
    timeout_seconds: float = Field(
        90.0,
        ge=10.0,
        le=300.0,
        description="单来源超时时间",
    )
    on_source_error: SourceErrorMode = Field(
        SourceErrorMode.CONTINUE,
        description="单来源失败时继续或整体失败",
    )

    @model_validator(mode="after")
    def _check_limits(self):
        if self.max_pages_per_source > _settings.REGION_JOBS_MAX_PAGES_PER_SOURCE:
            raise ValueError(
                f"max_pages_per_source={self.max_pages_per_source} 超过上限 "
                f"{_settings.REGION_JOBS_MAX_PAGES_PER_SOURCE}"
            )
        if self.max_records_per_source > _settings.REGION_JOBS_MAX_RECORDS_PER_SOURCE:
            raise ValueError(
                f"max_records_per_source={self.max_records_per_source} 超过上限 "
                f"{_settings.REGION_JOBS_MAX_RECORDS_PER_SOURCE}"
            )
        return self


class OutputOptions(BaseModel):
    deduplicate: bool = Field(True, description="是否进行保守去重")
    include_raw: bool = Field(False, description="是否返回各平台原始字段")
    include_source_metadata: bool = Field(
        True,
        description="是否返回各来源采集状态和平台区域编码",
    )


class RegionJobSearchPayload(BaseModel):
    region: RegionSpec
    query: QuerySpec
    sources: List[SourceName] = Field(
        default_factory=lambda: [SourceName.ZHILIAN, SourceName.BOSS_ZHIPIN],
        min_length=1,
        max_length=2,
        description="数据来源列表",
    )
    collection: CollectionOptions = Field(default_factory=CollectionOptions)
    output: OutputOptions = Field(default_factory=OutputOptions)

    @model_validator(mode="after")
    def _check_combinations(self):
        combinations = len(self.query.keywords) * len(self.sources)
        limit = _settings.REGION_JOBS_MAX_COMBINATIONS
        if combinations > limit:
            raise ValueError(
                f"keywords × sources = {combinations}，超过上限 {limit}"
            )
        return self


class SourceRunResult(BaseModel):
    source: SourceName
    ok: bool
    jobs: List[Dict[str, Any]] = Field(default_factory=list)
    pages_fetched: int = 0
    queries_attempted: int = 0
    pages_requested: int = 0
    region_code: Optional[Any] = None
    error: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


BOSS_CITY_CODES = {
    "全国": 100010000,
    "北京": 101010100,
    "上海": 101020100,
    "广州": 101280100,
    "深圳": 101280600,
    "杭州": 101210100,
    "天津": 101030100,
    "西安": 101110100,
    "苏州": 101190400,
    "武汉": 101200100,
    "厦门": 101230200,
    "长沙": 101250100,
    "成都": 101270100,
    "郑州": 101180100,
    "重庆": 101040100,
    "佛山": 101280800,
    "合肥": 101220100,
    "济南": 101120100,
    "青岛": 101120200,
    "南京": 101190100,
    "东莞": 101281600,
    "昆明": 101290100,
    "南昌": 101240100,
    "石家庄": 101090100,
    "宁波": 101210400,
    "福州": 101230100,
}


async def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
    configured_key = _settings.REGION_JOBS_API_KEY
    if not configured_key:
        return
    if x_api_key != configured_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


@router.post(
    "/jobs/region-search",
    summary="区域岗位数据统一搜索",
    description=(
        "以业务区域为主输入，同时适配智联招聘和 BOSS 直聘。\n"
        "接口返回统一职位字段、来源状态和保守去重后的区域岗位数据。"
    ),
    dependencies=[Depends(require_api_key)],
)
async def search_region_jobs(payload: RegionJobSearchPayload):
    logger.info(
        "[region-search] city=%s keywords=%s sources=%s detail=%s",
        payload.region.city,
        payload.query.keywords,
        [s.value for s in payload.sources],
        payload.collection.detail_level.value,
    )

    tasks = []
    if SourceName.ZHILIAN in payload.sources:
        tasks.append(_run_zhilian(payload))
    if SourceName.BOSS_ZHIPIN in payload.sources:
        tasks.append(_run_boss(payload))

    results = await asyncio.gather(*tasks)
    if payload.collection.on_source_error == SourceErrorMode.FAIL:
        failed = [r for r in results if not r.ok]
        if failed:
            return create_standard_response(
                code=503,
                message="区域岗位来源采集失败",
                data=_build_response_data(results, payload),
            )

    succeeded = [r for r in results if r.ok]
    if not succeeded:
        return create_standard_response(
            code=503,
            message="所有区域岗位来源均采集失败",
            data=_build_response_data(results, payload),
        )

    data = _build_response_data(results, payload)
    return create_standard_response(data=data, message=f"区域岗位搜索完成，共 {len(data['jobs'])} 条")


async def _run_zhilian(payload: RegionJobSearchPayload) -> SourceRunResult:
    city_name = payload.region.city
    city_id = payload.region.platform_hints.zhilian_city_id
    try:
        client = get_zhilian_client()
        include_detail = payload.collection.detail_level == DetailLevel.DESCRIPTION
        city_id_overrides = {city_name: city_id} if city_id else None
        raw_jobs = await asyncio.wait_for(
            client.scrape_many(
                payload.query.keywords,
                [city_name],
                payload.collection.max_pages_per_source,
                include_detail=include_detail,
                city_id_overrides=city_id_overrides,
            ),
            timeout=payload.collection.timeout_seconds,
        )
        if city_id is None:
            city_id = await _resolve_zhilian_city_id(client, city_name)

        summary = getattr(client, "_last_scrape_summary", {}) or {}
        limited = _limit_jobs_by_keyword(
            raw_jobs,
            payload.collection.max_records_per_source,
            payload.query.keywords,
            keyword_key="_query_keyword",
        )
        jobs = [
            _normalize_zhilian_job(
                raw,
                payload=payload,
                source_job_index=index,
            )
            for index, raw in enumerate(limited, start=1)
        ]
        return SourceRunResult(
            source=SourceName.ZHILIAN,
            ok=True,
            jobs=jobs,
            pages_fetched=int(summary.get("pages_fetched") or 0),
            queries_attempted=int(summary.get("combinations") or len(payload.query.keywords)),
            pages_requested=int(summary.get("pages_requested") or 0),
            region_code=city_id,
            warnings=_empty_result_warnings(SourceName.ZHILIAN, jobs),
        )
    except asyncio.TimeoutError:
        error = f"智联采集超时: {payload.collection.timeout_seconds:g}s"
        logger.warning(f"[region-search][zhilian] 失败: {error}", exc_info=True)
        return SourceRunResult(
            source=SourceName.ZHILIAN,
            ok=False,
            queries_attempted=len(payload.query.keywords),
            pages_requested=len(payload.query.keywords) * payload.collection.max_pages_per_source,
            region_code=city_id,
            error=error,
        )
    except Exception as exc:
        error = _source_error_message(exc, "智联采集异常")
        logger.warning(f"[region-search][zhilian] 失败: {error}", exc_info=True)
        return SourceRunResult(
            source=SourceName.ZHILIAN,
            ok=False,
            queries_attempted=len(payload.query.keywords),
            pages_requested=len(payload.query.keywords) * payload.collection.max_pages_per_source,
            region_code=city_id,
            error=error,
        )


async def _run_boss(payload: RegionJobSearchPayload) -> SourceRunResult:
    city_code = _resolve_boss_city_code(payload.region)
    if city_code is None:
        return SourceRunResult(
            source=SourceName.BOSS_ZHIPIN,
            ok=False,
            error=f"无法解析 BOSS 城市编码: {payload.region.city}",
        )

    try:
        include_description = payload.collection.detail_level == DetailLevel.DESCRIPTION
        max_items_per_query = _per_keyword_record_budget(
            payload.collection.max_records_per_source,
            payload.query.keywords,
        )
        raw_result = await asyncio.wait_for(
            _boss_client.scrape_many(
                payload.query.keywords,
                [city_code],
                payload.collection.max_pages_per_source,
                max_items_per_query,
                payload.output.include_raw,
                include_description,
            ),
            timeout=payload.collection.timeout_seconds,
        )
        raw_jobs = (raw_result or {}).get("jobs") or []
        limited = _limit_jobs_by_keyword(
            raw_jobs,
            payload.collection.max_records_per_source,
            payload.query.keywords,
            keyword_key="keyword",
        )
        jobs = [
            _normalize_boss_job(raw, payload=payload)
            for raw in limited
        ]
        summary = (raw_result or {}).get("summary") or {}
        warnings = (raw_result or {}).get("warnings") or []
        warnings.extend(_multi_keyword_page_warnings(summary, payload))
        warnings.extend(_empty_result_warnings(SourceName.BOSS_ZHIPIN, jobs))
        return SourceRunResult(
            source=SourceName.BOSS_ZHIPIN,
            ok=True,
            jobs=jobs,
            pages_fetched=int(summary.get("pages_fetched") or 0),
            queries_attempted=int(summary.get("combinations") or len(payload.query.keywords)),
            pages_requested=len(payload.query.keywords) * payload.collection.max_pages_per_source,
            region_code=city_code,
            warnings=warnings,
        )
    except asyncio.TimeoutError:
        error = f"BOSS 采集超时: {payload.collection.timeout_seconds:g}s"
        logger.warning(f"[region-search][boss_zhipin] 失败: {error}", exc_info=True)
        return SourceRunResult(
            source=SourceName.BOSS_ZHIPIN,
            ok=False,
            queries_attempted=len(payload.query.keywords),
            pages_requested=len(payload.query.keywords) * payload.collection.max_pages_per_source,
            region_code=city_code,
            error=error,
        )
    except Exception as exc:
        error = _source_error_message(exc, "BOSS 采集异常")
        logger.warning(f"[region-search][boss_zhipin] 失败: {error}", exc_info=True)
        return SourceRunResult(
            source=SourceName.BOSS_ZHIPIN,
            ok=False,
            queries_attempted=len(payload.query.keywords),
            pages_requested=len(payload.query.keywords) * payload.collection.max_pages_per_source,
            region_code=city_code,
            error=error,
        )


async def _resolve_zhilian_city_id(client, city_name: str) -> Optional[str]:
    city_resolver = getattr(client, "_city", None)
    if city_resolver and hasattr(city_resolver, "resolve"):
        try:
            return await city_resolver.resolve(city_name)
        except Exception:
            return None
    return None


def _resolve_boss_city_code(region: RegionSpec) -> Optional[int]:
    if region.platform_hints.boss_city_code:
        return region.platform_hints.boss_city_code
    return BOSS_CITY_CODES.get(region.city)


def _normalize_zhilian_job(
    raw: Dict[str, Any],
    *,
    payload: RegionJobSearchPayload,
    source_job_index: int,
) -> Dict[str, Any]:
    source_job_id = raw.get("positionNumber") or f"unknown-{source_job_index}"
    details = raw.get("job_details") if isinstance(raw.get("job_details"), dict) else {}
    description_text = _extract_zhilian_description(details)
    description_status = "success" if description_text else (
        "empty" if payload.collection.detail_level == DetailLevel.DESCRIPTION else "not_requested"
    )

    job = _base_job(
        source=SourceName.ZHILIAN.value,
        source_job_id=str(source_job_id),
        matched_keyword=_guess_matched_keyword(raw, payload.query.keywords),
        payload=payload,
    )
    job.update({
        "job_name": raw.get("name"),
        "company": {
            "name": raw.get("companyName"),
            "industry": raw.get("industryName"),
            "scale": raw.get("companySize"),
            "type_or_stage": raw.get("propertyName"),
            "logo_url": raw.get("companyLogo"),
            "profile_url": raw.get("companyUrl"),
        },
        "salary": _salary_object(raw.get("salary")),
        "location": {
            **job["location"],
            "address": raw.get("address"),
        },
        "requirements": {
            "experience": raw.get("workingExp"),
            "degree": raw.get("education"),
            "skills": _as_list(raw.get("jobSkillTags")),
            "labels": [],
        },
        "benefits": _as_list(raw.get("jobKnowledgeWelfareFeatures")),
        "description": {
            "text": description_text,
            "responsibilities": None,
            "requirements": None,
            "status": description_status,
        },
        "links": {
            "detail_url": raw.get("positionURL"),
            "company_url": raw.get("companyUrl"),
        },
        "metadata": {
            **job["metadata"],
            "query_keyword": raw.get("_query_keyword"),
            "raw_available": payload.output.include_raw,
        },
    })
    if payload.output.include_raw:
        job["raw"] = raw
    return job


def _normalize_boss_job(raw: Dict[str, Any], *, payload: RegionJobSearchPayload) -> Dict[str, Any]:
    source_job_id = raw.get("encrypt_job_id") or _fallback_job_id(raw)
    job = _base_job(
        source=SourceName.BOSS_ZHIPIN.value,
        source_job_id=str(source_job_id),
        matched_keyword=_guess_matched_keyword(raw, payload.query.keywords),
        payload=payload,
    )
    job.update({
        "job_name": raw.get("job_name"),
        "company": {
            "name": raw.get("company_name"),
            "industry": raw.get("company_industry"),
            "scale": raw.get("company_scale"),
            "type_or_stage": raw.get("company_stage"),
            "logo_url": None,
            "profile_url": None,
        },
        "salary": _salary_object(raw.get("salary")),
        "location": {
            **job["location"],
            "city": raw.get("city") or payload.region.city,
            "district": raw.get("district") or payload.region.district,
            "business_district": raw.get("business_district"),
            "gps": raw.get("gps"),
        },
        "requirements": {
            "experience": raw.get("experience"),
            "degree": raw.get("degree"),
            "skills": _as_list(raw.get("skills")),
            "labels": _as_list(raw.get("labels")),
        },
        "benefits": _as_list(raw.get("welfare")),
        "description": {
            "text": raw.get("job_description"),
            "responsibilities": raw.get("responsibilities"),
            "requirements": raw.get("requirements"),
            "status": raw.get("description_status") or "not_requested",
        },
        "links": {
            "detail_url": raw.get("detail_url"),
            "company_url": None,
        },
        "metadata": {
            **job["metadata"],
            "page": raw.get("page"),
            "query_keyword": raw.get("keyword"),
            "raw_available": payload.output.include_raw,
        },
    })
    if payload.output.include_raw and "raw" in raw:
        job["raw"] = raw["raw"]
    return job


def _base_job(
    *,
    source: str,
    source_job_id: str,
    matched_keyword: Optional[str],
    payload: RegionJobSearchPayload,
) -> Dict[str, Any]:
    return {
        "job_id": f"{source}:{source_job_id}",
        "source": source,
        "source_job_id": source_job_id,
        "matched_keyword": matched_keyword,
        "job_name": None,
        "company": {
            "name": None,
            "industry": None,
            "scale": None,
            "type_or_stage": None,
            "logo_url": None,
            "profile_url": None,
        },
        "salary": _salary_object(None),
        "location": {
            "country": payload.region.country,
            "province": payload.region.province,
            "city": payload.region.city,
            "district": payload.region.district,
            "business_district": None,
            "address": None,
            "gps": None,
        },
        "requirements": {
            "experience": None,
            "degree": None,
            "skills": [],
            "labels": [],
        },
        "benefits": [],
        "description": {
            "text": None,
            "responsibilities": None,
            "requirements": None,
            "status": "not_requested",
        },
        "links": {
            "detail_url": None,
            "company_url": None,
        },
        "metadata": {
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            "page": None,
            "raw_available": False,
        },
    }


def _extract_zhilian_description(details: Dict[str, Any]) -> Optional[str]:
    if not details:
        return None
    for key in (
        "jobDesc",
        "jobDescription",
        "description",
        "describe",
        "responsibility",
        "jobContent",
        "content",
    ):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return _clean_html(value)
    return None


def _clean_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _salary_object(text: Optional[Any]) -> Dict[str, Any]:
    salary_text = str(text).strip() if text is not None else None
    salary_min = None
    salary_max = None
    salary_months = None
    if salary_text:
        range_match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*K", salary_text, re.I)
        if range_match:
            salary_min = float(range_match.group(1))
            salary_max = float(range_match.group(2))
        month_match = re.search(r"[·xX*]\s*(\d{2})\s*薪", salary_text)
        if month_match:
            salary_months = int(month_match.group(1))
    return {
        "text": salary_text,
        "min": salary_min,
        "max": salary_max,
        "months": salary_months,
    }


def _as_list(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def _guess_matched_keyword(raw: Dict[str, Any], keywords: List[str]) -> Optional[str]:
    text = " ".join(str(v or "") for v in (
        raw.get("name"),
        raw.get("jobName"),
        raw.get("job_name"),
        raw.get("company_industry"),
        raw.get("companyIndustry"),
        " ".join(str(v or "") for v in _as_list(raw.get("skills"))),
        " ".join(str(v or "") for v in _as_list(raw.get("labels"))),
        " ".join(str(v or "") for v in _as_list(raw.get("jobSkillTags"))),
    ))
    for keyword in keywords:
        if keyword and keyword in text:
            return keyword
    return None


def _fallback_job_id(job: Dict[str, Any]) -> str:
    return "|".join(str(job.get(k) or "") for k in (
        "job_name",
        "company_name",
        "salary",
        "city",
        "district",
    ))


def _build_source_status(
    results: List[SourceRunResult],
    payload: RegionJobSearchPayload,
) -> Dict[str, Dict[str, Any]]:
    status_map = {
        source.value: {
            "ok": False,
            "count": 0,
            "pages_fetched": 0,
            "queries_attempted": 0,
            "pages_requested": 0,
            "region_code": None,
            "detail_level_applied": payload.collection.detail_level.value,
            "error": "not_requested",
            "warnings": [],
        }
        for source in payload.sources
    }
    for result in results:
        status_map[result.source.value] = {
            "ok": result.ok,
            "count": len(result.jobs),
            "pages_fetched": result.pages_fetched,
            "queries_attempted": result.queries_attempted,
            "pages_requested": result.pages_requested,
            "region_code": result.region_code,
            "detail_level_applied": payload.collection.detail_level.value,
            "error": result.error,
            "warnings": result.warnings,
        }
    return status_map


def _build_response_data(
    results: List[SourceRunResult],
    payload: RegionJobSearchPayload,
) -> Dict[str, Any]:
    all_jobs = []
    for result in results:
        all_jobs.extend(result.jobs)

    total_before_dedup = len(all_jobs)
    if payload.output.deduplicate:
        all_jobs = _deduplicate_jobs(all_jobs)

    data = {
        "request": {
            "region": _region_to_dict(payload.region),
            "keywords": payload.query.keywords,
            "keyword_mode": payload.query.keyword_mode.value,
            "sources": [source.value for source in payload.sources],
            "detail_level": payload.collection.detail_level.value,
        },
        "summary": {
            "total": len(all_jobs),
            "total_before_dedup": total_before_dedup,
            "deduplicated_count": total_before_dedup - len(all_jobs),
            "sources_succeeded": [r.source.value for r in results if r.ok],
            "sources_failed": [r.source.value for r in results if not r.ok],
        },
        "source_status": _build_source_status(results, payload),
        "jobs": all_jobs,
    }
    if not payload.output.include_source_metadata:
        data.pop("source_status", None)
    return data


def _source_error_message(exc: Exception, fallback: str) -> str:
    message = str(exc).strip()
    return message or fallback


def _empty_result_warnings(source: SourceName, jobs: List[Dict[str, Any]]) -> List[str]:
    if jobs:
        return []
    return [f"{source.value} 成功响应但没有返回职位结果"]


def _multi_keyword_page_warnings(
    summary: Dict[str, Any],
    payload: RegionJobSearchPayload,
) -> List[str]:
    combinations = int(summary.get("combinations") or len(payload.query.keywords))
    if combinations <= 1:
        return []
    pages = int(summary.get("pages_fetched") or 0)
    return [
        f"pages_fetched={pages} 为所有关键词查询累计页数，不是单个关键词页数"
    ]


def _per_keyword_record_budget(max_records: int, keywords: List[str]) -> int:
    keyword_count = max(1, len(keywords))
    return max(1, (max_records + keyword_count - 1) // keyword_count)


def _limit_jobs_by_keyword(
    jobs: List[Dict[str, Any]],
    max_records: int,
    keywords: List[str],
    *,
    keyword_key: str,
) -> List[Dict[str, Any]]:
    """按查询关键词轮转取数，避免第一个关键词占满来源配额。"""
    if len(jobs) <= max_records:
        return jobs

    buckets: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for job in jobs:
        buckets.setdefault(job.get(keyword_key), []).append(job)

    ordered_keys: List[Optional[str]] = list(keywords)
    ordered_keys.extend(k for k in buckets if k not in ordered_keys)

    limited: List[Dict[str, Any]] = []
    while len(limited) < max_records:
        progressed = False
        for key in ordered_keys:
            bucket = buckets.get(key) or []
            if not bucket:
                continue
            limited.append(bucket.pop(0))
            progressed = True
            if len(limited) >= max_records:
                break
        if not progressed:
            break
    return limited


def _deduplicate_jobs(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    deduped = []
    for job in jobs:
        fingerprints = _job_fingerprints(job)
        if any(fp in seen for fp in fingerprints):
            continue
        seen.update(fingerprints)
        deduped.append(job)
    return deduped


def _job_fingerprints(job: Dict[str, Any]) -> List[str]:
    fingerprints = []
    source_id = job.get("job_id")
    if source_id:
        fingerprints.append(f"source:{source_id}")
    company = job.get("company") or {}
    salary = job.get("salary") or {}
    location = job.get("location") or {}
    parts = [
        job.get("job_name"),
        company.get("name"),
        location.get("city"),
        salary.get("text"),
    ]
    if all(parts):
        fingerprints.append("weak:" + "|".join(_norm(v) for v in parts))
    return fingerprints or [f"fallback:{id(job)}"]


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _region_to_dict(region: RegionSpec) -> Dict[str, Any]:
    return {
        "country": region.country,
        "province": region.province,
        "city": region.city,
        "district": region.district,
    }
