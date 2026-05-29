# -*- coding: utf-8 -*-
"""天眼查企业数据客户端与本地去重入库逻辑。"""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import TianyanchaCompany, TianyanchaSearchQuery
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/tianyancha/client.log")

CANDIDATE_PREVIEW_LIMIT = 10


ERROR_MESSAGES = {
    0: "请求成功",
    300000: "无数据",
    300001: "请求失败",
    300002: "账号失效",
    300003: "账号过期",
    300004: "访问频率过快",
    300005: "无权限访问此 API",
    300006: "余额不足",
    300007: "剩余次数不足",
    300008: "缺少必要参数",
    300009: "账号信息有误",
    300010: "URL 不存在",
    300011: "此 IP 无权限访问此 API",
    300012: "报告生成中",
}


class TianyanchaAPIError(RuntimeError):
    """天眼查远程接口错误。"""

    def __init__(self, error_code: int, reason: str):
        self.error_code = error_code
        self.reason = reason or ERROR_MESSAGES.get(error_code, "天眼查接口错误")
        super().__init__(f"天眼查接口错误: error_code={error_code}, reason={self.reason}")


def normalize_company_name(name: Optional[str]) -> str:
    """去掉 HTML 标签和空白，用作保守兜底匹配。"""
    if not name:
        return ""
    text = re.sub(r"<[^>]+>", "", str(name))
    return re.sub(r"\s+", "", text).strip()


def parse_remote_datetime(value: Any) -> Optional[datetime]:
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OSError, ValueError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def parse_remote_int(value: Any) -> Optional[int]:
    if value in (None, "", "-"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def join_remote_text(value: Any) -> Optional[str]:
    if value in (None, "", "-", [], {}):
        return None
    if isinstance(value, list):
        return ";".join(str(item) for item in value if item not in (None, "", "-"))
    return str(value)


def _non_empty(value: Any) -> bool:
    return value not in (None, "", "-", [], {})


def build_search_fingerprint(params: Dict[str, Any]) -> str:
    payload = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_clarification(
    *,
    input_value: Optional[str],
    candidates: List[Dict[str, str]],
    retry_field: str,
    retry_with: str,
    limit: int = CANDIDATE_PREVIEW_LIMIT,
) -> Dict[str, Any]:
    returned = min(len(candidates), limit)
    return {
        "input": input_value,
        "total": len(candidates),
        "returned": returned,
        "truncated": len(candidates) > limit,
        "retry_field": retry_field,
        "retry_with": retry_with,
    }


class TianyanchaClient:
    """封装天眼查远程调用、搜索缓存和企业去重入库。"""

    def __init__(self) -> None:
        self._area_cache: Optional[List[Dict[str, str]]] = None
        self._category_cache: Optional[List[Dict[str, str]]] = None

    async def search_companies(
        self,
        db: AsyncSession,
        *,
        word: Optional[str],
        category_guobiao: Optional[str],
        area_code: Optional[str],
        page_num: int,
        page_size: int,
        enrich_detail: bool = False,
        force_remote: bool = False,
        refresh_detail: bool = False,
        max_detail_calls: Optional[int] = None,
        max_allowed_detail_calls: Optional[int] = None,
    ) -> Dict[str, Any]:
        page_size = min(page_size, _settings.TIANYANCHA_MAX_PAGE_SIZE)
        params = {
            "word": word or None,
            "categoryGuobiao": category_guobiao or None,
            "areaCode": area_code or None,
            "pageNum": page_num,
            "pageSize": page_size,
        }
        params = {k: v for k, v in params.items() if v not in (None, "")}
        fingerprint = build_search_fingerprint(params)

        cached_query = await self._get_cached_query(db, fingerprint)
        if cached_query and not force_remote:
            companies = await self._load_companies_by_ids(db, cached_query.company_ids or [])
            detail_calls, warnings = await self._enrich_company_details(
                db,
                companies,
                enrich_detail=enrich_detail,
                refresh_detail=refresh_detail,
                max_detail_calls=max_detail_calls,
                max_allowed_detail_calls=max_allowed_detail_calls,
            )
            if detail_calls:
                await db.commit()
            return {
                "source": "cache",
                "cache_hit": True,
                "remote_called": False,
                "detail_remote_calls": detail_calls,
                "total": cached_query.total,
                "companies": [self.company_to_dict(company) for company in companies],
                "query": self._query_to_dict(cached_query),
                "warnings": warnings,
            }

        payload = await self._request(_settings.TIANYANCHA_SEARCH_URL, params)
        error_code = int(payload.get("error_code", 300001))
        reason = payload.get("reason") or ERROR_MESSAGES.get(error_code, "")
        if error_code not in (0, 300000):
            raise TianyanchaAPIError(error_code, reason)

        result = payload.get("result") or {}
        items = result.get("items") or []
        now = datetime.now(timezone.utc)
        companies: List[TianyanchaCompany] = []
        created_company_ids: Set[int] = set()
        created_count = 0
        updated_count = 0

        for item in items:
            company, created = await self.upsert_company_from_search(db, item, seen_at=now)
            companies.append(company)
            if created:
                created_count += 1
                if company.id is not None:
                    created_company_ids.add(company.id)
            else:
                updated_count += 1

        should_enrich_detail = enrich_detail or (
            _settings.TIANYANCHA_ENRICH_NEW_COMPANIES
            and bool(created_company_ids)
            and max_detail_calls != 0
        )
        detail_calls, detail_warnings = await self._enrich_company_details(
            db,
            companies,
            enrich_detail=should_enrich_detail,
            refresh_detail=refresh_detail,
            max_detail_calls=max_detail_calls,
            fetched_at=now,
            max_allowed_detail_calls=max_allowed_detail_calls,
            priority_company_ids=created_company_ids,
        )

        company_ids = [company.id for company in companies if company.id is not None]
        query = await self._upsert_search_query(
            db,
            fingerprint=fingerprint,
            params=params,
            total=result.get("total", 0),
            company_ids=company_ids,
            error_code=error_code,
            reason=reason,
            fetched_at=now,
        )
        await db.commit()

        return {
            "source": "remote",
            "cache_hit": False,
            "remote_called": True,
            "detail_remote_calls": detail_calls,
            "created_count": created_count,
            "updated_count": updated_count,
            "total": result.get("total", 0),
            "companies": [self.company_to_dict(company) for company in companies],
            "query": self._query_to_dict(query),
            "warnings": ([] if error_code == 0 else [reason]) + detail_warnings,
        }

    async def get_company(
        self,
        db: AsyncSession,
        *,
        keyword: str,
        force_remote: bool = False,
    ) -> Dict[str, Any]:
        local = await self.find_local_company(db, keyword)
        if local and not force_remote and not self._needs_baseinfo_refresh(local):
            return {
                "source": "cache",
                "cache_hit": True,
                "remote_called": False,
                "company": self.company_to_dict(local, include_raw=True),
            }

        detail = await self.fetch_baseinfo(keyword)
        company, created = await self.upsert_company_from_baseinfo(
            db,
            detail,
            fetched_at=datetime.now(timezone.utc),
        )
        await db.commit()
        return {
            "source": "remote",
            "cache_hit": False,
            "remote_called": True,
            "created": created,
            "company": self.company_to_dict(company, include_raw=True),
        }

    async def list_local_companies(
        self,
        db: AsyncSession,
        *,
        keyword: Optional[str] = None,
        area: Optional[str] = None,
        industry: Optional[str] = None,
        reg_status: Optional[str] = None,
        skip: int = 0,
        limit: int = 50,
        enrich_detail: bool = False,
        refresh_detail: bool = False,
        max_detail_calls: Optional[int] = None,
    ) -> Dict[str, Any]:
        query = select(TianyanchaCompany).order_by(TianyanchaCompany.updated_at.desc())
        if keyword:
            normalized = normalize_company_name(keyword)
            like = f"%{keyword}%"
            query = query.where(
                or_(
                    TianyanchaCompany.name.ilike(like),
                    TianyanchaCompany.normalized_name.ilike(f"%{normalized}%"),
                    TianyanchaCompany.credit_code == keyword,
                    TianyanchaCompany.reg_number == keyword,
                    TianyanchaCompany.org_number == keyword,
                )
            )
        if area:
            like = f"%{area}%"
            query = query.where(
                or_(
                    TianyanchaCompany.base.ilike(like),
                    TianyanchaCompany.city.ilike(like),
                    TianyanchaCompany.district.ilike(like),
                )
            )
        if industry:
            query = query.where(TianyanchaCompany.industry.ilike(f"%{industry}%"))
        if reg_status:
            query = query.where(TianyanchaCompany.reg_status == reg_status)
        result = await db.execute(query.offset(skip).limit(limit))
        companies = list(result.scalars().all())
        detail_calls, warnings = await self._enrich_company_details(
            db,
            companies,
            enrich_detail=enrich_detail,
            refresh_detail=refresh_detail,
            max_detail_calls=max_detail_calls,
        )
        if detail_calls:
            await db.commit()
        return {
            "companies": [self.company_to_dict(company) for company in companies],
            "skip": skip,
            "limit": limit,
            "detail_remote_calls": detail_calls,
            "warnings": warnings,
        }

    async def research_region_companies(
        self,
        db: AsyncSession,
        *,
        region: str,
        industry: Optional[str],
        keywords: List[str],
        limit: int,
        detail_level: str,
        force_remote: bool,
    ) -> Dict[str, Any]:
        area_code, area_candidates = await self.resolve_area_code(region)
        category_code, category_candidates = await self.resolve_category_code(industry)
        if area_candidates or category_candidates:
            return {
                "need_clarification": True,
                "area_candidates": area_candidates[:CANDIDATE_PREVIEW_LIMIT],
                "category_candidates": category_candidates[:CANDIDATE_PREVIEW_LIMIT],
                "clarification": {
                    "area": _candidate_clarification(
                        input_value=region,
                        candidates=area_candidates,
                        retry_field="region",
                        retry_with=(
                            "候选项 code，例如 110105，或更完整名称，例如 北京市朝阳区"
                        ),
                    ),
                    "category": _candidate_clarification(
                        input_value=industry,
                        candidates=category_candidates,
                        retry_field="industry",
                        retry_with="候选项 code，例如 65，或更完整行业名称",
                    ),
                },
                "message": (
                    "区域或行业匹配不唯一，请选择候选项 code 后重试；"
                    "若未看到目标项，请输入更完整名称。"
                ),
            }

        safe_limit = min(limit, _settings.TIANYANCHA_DIFY_MAX_LIMIT)
        page_size = min(safe_limit, _settings.TIANYANCHA_MAX_PAGE_SIZE)
        max_pages = min(
            max(1, (safe_limit + page_size - 1) // page_size),
            _settings.TIANYANCHA_MAX_PAGES_PER_REQUEST,
        )
        enrich_detail = detail_level == "baseinfo"
        max_detail_calls = min(
            _settings.TIANYANCHA_DIFY_MAX_DETAIL_CALLS_PER_REQUEST,
            safe_limit,
        )

        collected: Dict[int, Dict[str, Any]] = {}
        remote_search_calls = 0
        detail_calls = 0
        warnings: List[str] = []
        query_results = []

        search_words = keywords or [industry or region]
        for word in search_words:
            for page_num in range(1, max_pages + 1):
                result = await self.search_companies(
                    db,
                    word=word,
                    category_guobiao=category_code,
                    area_code=area_code,
                    page_num=page_num,
                    page_size=page_size,
                    enrich_detail=enrich_detail,
                    force_remote=force_remote,
                    max_detail_calls=max_detail_calls - detail_calls,
                    max_allowed_detail_calls=_settings.TIANYANCHA_DIFY_MAX_DETAIL_CALLS_PER_REQUEST,
                )
                query_results.append({
                    "word": word,
                    "page_num": page_num,
                    "cache_hit": result["cache_hit"],
                    "total": result.get("total"),
                })
                if result["remote_called"]:
                    remote_search_calls += 1
                detail_calls += result.get("detail_remote_calls", 0)
                warnings.extend(result.get("warnings") or [])
                for company in result.get("companies") or []:
                    company_id = company.get("id")
                    if company_id is not None:
                        collected[company_id] = company
                    if len(collected) >= safe_limit:
                        break
                if len(collected) >= safe_limit:
                    break
            if len(collected) >= safe_limit:
                break

        companies = list(collected.values())[:safe_limit]
        detail_complete_count = sum(
            1 for company in companies if company.get("baseinfo_fetched_at")
        )
        missing_detail_count = len(companies) - detail_complete_count
        if enrich_detail and missing_detail_count:
            warnings.append(
                f"仍有 {missing_detail_count} 条企业未取得详情，可稍后重试或提高详情额度"
            )
        return {
            "need_clarification": False,
            "summary": {
                "region": region,
                "area_code": area_code,
                "industry": industry,
                "category_guobiao": category_code,
                "keywords": search_words,
                "requested_limit": limit,
                "returned_count": len(companies),
            },
            "companies": companies,
            "cache": {
                "query_results": query_results,
            },
            "cost_control": {
                "remote_search_calls": remote_search_calls,
                "remote_detail_calls": detail_calls,
                "detail_budget": max_detail_calls,
                "detail_required": enrich_detail,
                "detail_complete_count": detail_complete_count,
                "missing_detail_count": missing_detail_count,
                "detail_complete": (not enrich_detail) or missing_detail_count == 0,
                "detail_level": detail_level,
                "force_remote": force_remote,
            },
            "warnings": warnings,
        }

    async def fetch_baseinfo(self, keyword: str) -> Dict[str, Any]:
        payload = await self._request(_settings.TIANYANCHA_BASEINFO_URL, {"keyword": keyword})
        error_code = int(payload.get("error_code", 300001))
        reason = payload.get("reason") or ERROR_MESSAGES.get(error_code, "")
        if error_code != 0:
            raise TianyanchaAPIError(error_code, reason)
        return payload.get("result") or {}

    async def upsert_company_from_search(
        self,
        db: AsyncSession,
        raw: Dict[str, Any],
        *,
        seen_at: datetime,
    ) -> Tuple[TianyanchaCompany, bool]:
        data = self._map_search_company(raw)
        data["raw_search"] = raw
        data["search_seen_at"] = seen_at
        return await self._upsert_company(db, data, prefer_existing_detail=True)

    async def upsert_company_from_baseinfo(
        self,
        db: AsyncSession,
        raw: Dict[str, Any],
        *,
        fetched_at: datetime,
    ) -> Tuple[TianyanchaCompany, bool]:
        data = self._map_baseinfo_company(raw)
        data["raw_baseinfo"] = raw
        data["baseinfo_fetched_at"] = fetched_at
        return await self._upsert_company(db, data, prefer_existing_detail=False)

    async def _enrich_company_details(
        self,
        db: AsyncSession,
        companies: List[TianyanchaCompany],
        *,
        enrich_detail: bool,
        refresh_detail: bool,
        max_detail_calls: Optional[int],
        fetched_at: Optional[datetime] = None,
        max_allowed_detail_calls: Optional[int] = None,
        priority_company_ids: Optional[Set[int]] = None,
    ) -> Tuple[int, List[str]]:
        if not enrich_detail or not companies:
            return 0, []

        allowed_detail_calls = (
            _settings.TIANYANCHA_MAX_DETAIL_CALLS_PER_REQUEST
            if max_allowed_detail_calls is None
            else max_allowed_detail_calls
        )
        limit = (
            allowed_detail_calls
            if max_detail_calls is None
            else max_detail_calls
        )
        limit = max(0, min(limit, allowed_detail_calls))
        if limit == 0:
            return 0, []

        detail_calls = 0
        warnings: List[str] = []
        now = fetched_at or datetime.now(timezone.utc)
        priority_company_ids = priority_company_ids or set()
        indexed_companies = list(enumerate(companies))
        indexed_companies.sort(
            key=lambda item: (
                0 if item[1].id in priority_company_ids else 1,
                item[0],
            )
        )
        detail_candidates = [
            (index, company)
            for index, company in indexed_companies
            if refresh_detail or self._needs_baseinfo_refresh(company)
        ]
        if len(detail_candidates) > limit:
            warnings.append(
                f"详情补拉额度不足，本次需补 {len(detail_candidates)} 条，实际最多补 {limit} 条"
            )

        for index, company in detail_candidates:
            if detail_calls >= limit:
                break

            keyword = str(company.tianyancha_id or company.credit_code or company.name)
            try:
                detail = await self.fetch_baseinfo(keyword)
            except TianyanchaAPIError as exc:
                warnings.append(f"{company.name} 详情补拉失败: {exc.reason}")
                continue

            enriched_company, _ = await self.upsert_company_from_baseinfo(
                db,
                detail,
                fetched_at=now,
            )
            companies[index] = enriched_company
            detail_calls += 1

        return detail_calls, warnings

    async def find_local_company(self, db: AsyncSession, keyword: str) -> Optional[TianyanchaCompany]:
        normalized = normalize_company_name(keyword)
        conditions = [
            TianyanchaCompany.credit_code == keyword,
            TianyanchaCompany.reg_number == keyword,
            TianyanchaCompany.org_number == keyword,
            TianyanchaCompany.tax_number == keyword,
            TianyanchaCompany.name == keyword,
            TianyanchaCompany.normalized_name == normalized,
        ]
        if keyword.isdigit():
            conditions.insert(0, TianyanchaCompany.tianyancha_id == int(keyword))
        result = await db.execute(select(TianyanchaCompany).where(or_(*conditions)).limit(1))
        return result.scalar_one_or_none()

    async def resolve_area_code(self, region: Optional[str]) -> Tuple[Optional[str], List[Dict[str, str]]]:
        if not region:
            return None, []
        if re.fullmatch(r"[0-9A-Za-z]{6,12}", region):
            return region, []
        areas = await self._load_area_codes()
        exact = [item for item in areas if item["name"] == region or item["full_name"] == region]
        if len(exact) == 1:
            return exact[0]["code"], []
        fuzzy = [
            item for item in areas
            if region in item["full_name"] or region in item["name"]
        ]
        if len(fuzzy) == 1:
            return fuzzy[0]["code"], []
        return None, fuzzy

    async def resolve_category_code(self, industry: Optional[str]) -> Tuple[Optional[str], List[Dict[str, str]]]:
        if not industry:
            return None, []
        if re.fullmatch(r"[A-Za-z]|\d{2,4}", industry):
            return industry, []
        categories = await self._load_categories()
        exact = [item for item in categories if item["name"] == industry]
        if len(exact) == 1:
            return exact[0]["code"], []
        fuzzy = [item for item in categories if industry in item["name"]]
        if len(fuzzy) == 1:
            return fuzzy[0]["code"], []
        return None, fuzzy

    async def _request(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not _settings.TIANYANCHA_ENABLE_REMOTE:
            raise RuntimeError("TIANYANCHA_ENABLE_REMOTE=false，已禁止远程调用")
        if not _settings.TIANYANCHA_TOKEN:
            raise RuntimeError("未配置 TIANYANCHA_TOKEN，无法调用天眼查接口")
        async with httpx.AsyncClient(timeout=_settings.TIANYANCHA_HTTP_TIMEOUT) as client:
            response = await client.get(
                url,
                params=params,
                headers={"Authorization": _settings.TIANYANCHA_TOKEN},
            )
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("天眼查接口返回格式异常")
        return data

    async def _fetch_public_json(self, url: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=_settings.TIANYANCHA_HTTP_TIMEOUT) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"字典接口返回格式异常: {url}")
        return data

    async def _load_area_codes(self) -> List[Dict[str, str]]:
        if self._area_cache is not None:
            return self._area_cache
        data = await self._fetch_public_json(_settings.TIANYANCHA_AREA_CODE_URL)
        flattened: List[Dict[str, str]] = []
        for province in data.get("areaCode", []):
            province_name = province.get("name") or ""
            flattened.append({
                "name": province_name,
                "full_name": province_name,
                "code": str(province.get("areaCode") or ""),
                "level": "province",
            })
            for city in province.get("city", []) or []:
                city_name = city.get("name") or ""
                flattened.append({
                    "name": city_name,
                    "full_name": f"{province_name}{city_name}",
                    "code": str(city.get("areaCode") or ""),
                    "level": "city",
                })
                for district in city.get("district", []) or []:
                    district_name = district.get("name") or ""
                    flattened.append({
                        "name": district_name,
                        "full_name": f"{province_name}{city_name}{district_name}",
                        "code": str(district.get("areaCode") or ""),
                        "level": "district",
                    })
        self._area_cache = [item for item in flattened if item["code"]]
        return self._area_cache

    async def _load_categories(self) -> List[Dict[str, str]]:
        if self._category_cache is not None:
            return self._category_cache
        data = await self._fetch_public_json(_settings.TIANYANCHA_CATEGORY_URL)
        flattened: List[Dict[str, str]] = []
        for primary in data.get("category", []) or []:
            primary_name = primary.get("primInduName") or ""
            flattened.append({
                "name": primary_name,
                "code": str(primary.get("code") or ""),
                "level": "primary",
            })
            for secondary in primary.get("secList", []) or []:
                secondary_name = secondary.get("secnduName") or ""
                flattened.append({
                    "name": secondary_name,
                    "code": str(secondary.get("code") or ""),
                    "level": "secondary",
                    "parent": primary_name,
                })
                for tertiary in secondary.get("terList", []) or []:
                    flattened.append({
                        "name": tertiary.get("terInduName") or "",
                        "code": str(tertiary.get("code") or ""),
                        "level": "tertiary",
                        "parent": secondary_name,
                    })
        self._category_cache = [item for item in flattened if item["code"]]
        return self._category_cache

    async def _get_cached_query(
        self,
        db: AsyncSession,
        fingerprint: str,
    ) -> Optional[TianyanchaSearchQuery]:
        result = await db.execute(
            select(TianyanchaSearchQuery).where(TianyanchaSearchQuery.fingerprint == fingerprint)
        )
        query = result.scalar_one_or_none()
        if not query or not query.fetched_at:
            return None
        fetched_at = query.fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - fetched_at
        if age.total_seconds() > _settings.TIANYANCHA_SEARCH_CACHE_TTL_SECONDS:
            return None
        return query

    async def _load_companies_by_ids(
        self,
        db: AsyncSession,
        company_ids: List[int],
    ) -> List[TianyanchaCompany]:
        if not company_ids:
            return []
        result = await db.execute(
            select(TianyanchaCompany).where(TianyanchaCompany.id.in_(company_ids))
        )
        by_id = {company.id: company for company in result.scalars().all()}
        return [by_id[item_id] for item_id in company_ids if item_id in by_id]

    async def _upsert_search_query(
        self,
        db: AsyncSession,
        *,
        fingerprint: str,
        params: Dict[str, Any],
        total: int,
        company_ids: List[int],
        error_code: int,
        reason: str,
        fetched_at: datetime,
    ) -> TianyanchaSearchQuery:
        result = await db.execute(
            select(TianyanchaSearchQuery).where(TianyanchaSearchQuery.fingerprint == fingerprint)
        )
        query = result.scalar_one_or_none()
        if query is None:
            query = TianyanchaSearchQuery(fingerprint=fingerprint)
            db.add(query)
        query.word = params.get("word")
        query.category_guobiao = params.get("categoryGuobiao")
        query.area_code = params.get("areaCode")
        query.page_num = int(params.get("pageNum", 1))
        query.page_size = int(params.get("pageSize", 20))
        query.total = int(total or 0)
        query.company_ids = company_ids
        query.request_params = params
        query.response_error_code = error_code
        query.response_reason = reason
        query.fetched_at = fetched_at
        await db.flush()
        return query

    async def _upsert_company(
        self,
        db: AsyncSession,
        data: Dict[str, Any],
        *,
        prefer_existing_detail: bool,
    ) -> Tuple[TianyanchaCompany, bool]:
        company = await self._find_company_by_identity(db, data)
        created = company is None
        if company is None:
            company = TianyanchaCompany(
                name=data.get("name") or data.get("credit_code") or "未知企业",
                normalized_name=data.get("normalized_name") or normalize_company_name(data.get("name")) or "未知企业",
            )
            db.add(company)

        for field, value in data.items():
            if field in {"name", "normalized_name"} and not _non_empty(value):
                continue
            current = getattr(company, field, None)
            if field.startswith("raw_") or field.endswith("_at"):
                setattr(company, field, value)
            elif prefer_existing_detail and _non_empty(current) and not _non_empty(value):
                continue
            elif _non_empty(value):
                setattr(company, field, value)
        await db.flush()
        return company, created

    async def _find_company_by_identity(
        self,
        db: AsyncSession,
        data: Dict[str, Any],
    ) -> Optional[TianyanchaCompany]:
        conditions = []
        if data.get("tianyancha_id"):
            conditions.append(TianyanchaCompany.tianyancha_id == data["tianyancha_id"])
        if data.get("credit_code"):
            conditions.append(TianyanchaCompany.credit_code == data["credit_code"])
        if data.get("reg_number"):
            conditions.append(TianyanchaCompany.reg_number == data["reg_number"])
        if data.get("org_number"):
            conditions.append(TianyanchaCompany.org_number == data["org_number"])
        if not conditions and data.get("normalized_name"):
            conditions.append(TianyanchaCompany.normalized_name == data["normalized_name"])
        if not conditions:
            return None
        result = await db.execute(select(TianyanchaCompany).where(or_(*conditions)).limit(1))
        return result.scalar_one_or_none()

    def _map_search_company(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        name = raw.get("name") or ""
        return {
            "tianyancha_id": parse_remote_int(raw.get("id")),
            "name": name,
            "normalized_name": normalize_company_name(name),
            "credit_code": raw.get("creditCode"),
            "reg_number": raw.get("regNumber"),
            "org_number": raw.get("orgNumber"),
            "reg_status": raw.get("regStatus"),
            "reg_capital": raw.get("regCapital"),
            "legal_person_name": raw.get("legalPersonName"),
            "company_type": parse_remote_int(raw.get("companyType")),
            "legal_type": parse_remote_int(raw.get("type")),
            "base": raw.get("base"),
            "established_at": parse_remote_datetime(raw.get("estiblishTime")),
        }

    def _map_baseinfo_company(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        name = raw.get("name") or ""
        industry_all = raw.get("industryAll") or {}
        history_names = join_remote_text(raw.get("historyNameList") or raw.get("historyNames"))
        return {
            "tianyancha_id": parse_remote_int(raw.get("id")),
            "name": name,
            "normalized_name": normalize_company_name(name),
            "credit_code": raw.get("creditCode"),
            "reg_number": raw.get("regNumber"),
            "org_number": raw.get("orgNumber"),
            "tax_number": raw.get("taxNumber"),
            "reg_status": raw.get("regStatus"),
            "reg_capital": raw.get("regCapital"),
            "actual_capital": raw.get("actualCapital"),
            "legal_person_name": raw.get("legalPersonName"),
            "company_org_type": raw.get("companyOrgType"),
            "legal_type": parse_remote_int(raw.get("type")),
            "base": raw.get("base"),
            "city": raw.get("city"),
            "district": raw.get("district"),
            "district_code": raw.get("districtCode"),
            "industry": raw.get("industry"),
            "category": industry_all.get("category"),
            "category_code_first": industry_all.get("categoryCodeFirst"),
            "category_code_second": industry_all.get("categoryCodeSecond"),
            "category_code_third": industry_all.get("categoryCodeThird"),
            "category_code_fourth": industry_all.get("categoryCodeFourth"),
            "established_at": parse_remote_datetime(raw.get("estiblishTime")),
            "approved_at": parse_remote_datetime(raw.get("approvedTime")),
            "from_time": parse_remote_datetime(raw.get("fromTime")),
            "to_time": parse_remote_datetime(raw.get("toTime")),
            "updated_remote_at": parse_remote_datetime(raw.get("updateTimes")),
            "reg_institute": raw.get("regInstitute"),
            "reg_location": raw.get("regLocation"),
            "business_scope": raw.get("businessScope"),
            "staff_num_range": raw.get("staffNumRange"),
            "social_staff_num": parse_remote_int(raw.get("socialStaffNum")),
            "tags": join_remote_text(raw.get("tags")),
            "history_names": history_names,
            "percentile_score": parse_remote_int(raw.get("percentileScore")),
            "is_micro_ent": parse_remote_int(raw.get("isMicroEnt")),
        }

    def _needs_baseinfo_refresh(self, company: TianyanchaCompany) -> bool:
        if not company.baseinfo_fetched_at:
            return True
        fetched_at = company.baseinfo_fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - fetched_at > timedelta(
            days=_settings.TIANYANCHA_BASEINFO_TTL_DAYS
        )

    def company_to_dict(
        self,
        company: TianyanchaCompany,
        *,
        include_raw: bool = False,
    ) -> Dict[str, Any]:
        data = {
            "id": company.id,
            "tianyancha_id": company.tianyancha_id,
            "name": company.name,
            "credit_code": company.credit_code,
            "reg_number": company.reg_number,
            "org_number": company.org_number,
            "tax_number": company.tax_number,
            "reg_status": company.reg_status,
            "reg_capital": company.reg_capital,
            "actual_capital": company.actual_capital,
            "legal_person_name": company.legal_person_name,
            "company_type": company.company_type,
            "company_org_type": company.company_org_type,
            "legal_type": company.legal_type,
            "base": company.base,
            "city": company.city,
            "district": company.district,
            "district_code": company.district_code,
            "industry": company.industry,
            "category": company.category,
            "category_code_first": company.category_code_first,
            "category_code_second": company.category_code_second,
            "category_code_third": company.category_code_third,
            "category_code_fourth": company.category_code_fourth,
            "established_at": company.established_at.isoformat() if company.established_at else None,
            "approved_at": company.approved_at.isoformat() if company.approved_at else None,
            "from_time": company.from_time.isoformat() if company.from_time else None,
            "to_time": company.to_time.isoformat() if company.to_time else None,
            "updated_remote_at": (
                company.updated_remote_at.isoformat() if company.updated_remote_at else None
            ),
            "reg_institute": company.reg_institute,
            "business_scope": company.business_scope,
            "reg_location": company.reg_location,
            "staff_num_range": company.staff_num_range,
            "social_staff_num": company.social_staff_num,
            "tags": company.tags,
            "history_names": company.history_names,
            "percentile_score": company.percentile_score,
            "is_micro_ent": company.is_micro_ent,
            "search_seen_at": company.search_seen_at.isoformat() if company.search_seen_at else None,
            "baseinfo_fetched_at": (
                company.baseinfo_fetched_at.isoformat() if company.baseinfo_fetched_at else None
            ),
        }
        if include_raw:
            data["raw_search"] = company.raw_search
            data["raw_baseinfo"] = company.raw_baseinfo
        return data

    def _query_to_dict(self, query: TianyanchaSearchQuery) -> Dict[str, Any]:
        return {
            "fingerprint": query.fingerprint,
            "word": query.word,
            "category_guobiao": query.category_guobiao,
            "area_code": query.area_code,
            "page_num": query.page_num,
            "page_size": query.page_size,
            "total": query.total,
            "fetched_at": query.fetched_at.isoformat() if query.fetched_at else None,
        }
