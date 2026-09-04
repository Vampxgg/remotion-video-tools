# -*- coding: utf-8 -*-
"""区域岗位数据统一获取接口。"""

import asyncio
import re
import smtplib
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from enum import Enum
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator, model_validator

from api.job_search_v2 import get_search_client as get_zhilian_client
from services.boss_zhipin_client import BossAccessLimitedError, get_boss_client
from utils import region_map
from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.security import require_api_key
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/jobs/region_search.log")

router = APIRouter()
_boss_client: Optional[Any] = None


def _get_boss_region_client():
    """惰性获取 BOSS client，避免主服务导入区域岗位路由时初始化 Chrome/proxy。"""
    global _boss_client
    if _boss_client is None:
        _boss_client = get_boss_client()
    return _boss_client


class _BossCircuitBreaker:
    """BOSS 来源进程内熔断器。

    命中访问受限 / IP 异常 / 验证码 / 连续 code=37 / 采集超时后短暂拉闸，冷却期内
    BOSS 请求直接快速返回，不再触碰浏览器和 BOSS 接口，避免越采越封，也给可能仍在
    运行的旧同步线程留出自然收尾的时间。进程内共享（模块级单例），线程安全。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._blocked_until: Optional[datetime] = None
        self._reason: Optional[str] = None

    def trip(self, *, seconds: int, reason: str) -> None:
        seconds = max(1, int(seconds))
        until = datetime.now() + timedelta(seconds=seconds)
        with self._lock:
            if self._blocked_until is None or until > self._blocked_until:
                self._blocked_until = until
                self._reason = reason
        logger.warning(
            "[region-search][boss_zhipin] 熔断开启，冷却 %ss，原因：%s", seconds, reason
        )

    def state(self) -> Tuple[bool, Optional[str], Optional[int], Optional[str]]:
        """返回 (是否处于冷却, blocked_until_iso, retry_after_seconds, reason)。"""
        with self._lock:
            if self._blocked_until is None:
                return False, None, None, None
            now = datetime.now()
            if now >= self._blocked_until:
                self._blocked_until = None
                self._reason = None
                return False, None, None, None
            retry_after = int((self._blocked_until - now).total_seconds())
            return True, self._blocked_until.isoformat(timespec="seconds"), retry_after, self._reason

    def reset(self) -> None:
        with self._lock:
            self._blocked_until = None
            self._reason = None


_boss_circuit = _BossCircuitBreaker()


def _boss_worker_status(client: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    candidate = client if client is not None else _boss_client
    if candidate is None:
        return None
    status_fn = getattr(candidate, "worker_status", None)
    if status_fn is None:
        return None
    try:
        return status_fn()
    except Exception as exc:
        logger.debug("[region-search][boss_zhipin] 读取 worker_status 失败: %s", exc)
        return None


# 账户需要登录 / 命中风控时向管理员发提醒邮件的进程内冷却状态（按来源分别冷却）。
_notify_lock = Lock()
_last_notify_ts: Dict[str, float] = {}

# 智联采集异常里「账户需登录 / 命中风控」的文本特征；命中才发提醒邮件，
# 避免为普通瞬时错误（超时/结构异常等）误报打扰管理员。
_LOGIN_OR_BLOCK_MARKERS = (
    "登录",
    "扫码",
    "验证码",
    "风控",
    "访问受限",
    "人机验证",
    "受限",
)


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
        max_length=20,
        description="岗位关键词列表",
        examples=[["前端开发工程师"]],
    )
    keyword_mode: KeywordMode = Field(
        KeywordMode.ANY,
        description="关键词匹配模式；第一版仅支持 any",
    )

    @field_validator("keywords")
    @classmethod
    def _normalize_keywords(cls, value: List[str]) -> List[str]:
        """逐项 strip、去空、去重（保序）；清洗后为空则视为参数错误。"""
        cleaned: List[str] = []
        seen: set[str] = set()
        for item in value:
            kw = (item or "").strip()
            if not kw or kw in seen:
                continue
            seen.add(kw)
            cleaned.append(kw)
        if not cleaned:
            raise ValueError("keywords 不能为空（去除空白后无有效关键词）")
        return cleaned


class CollectionOptions(BaseModel):
    max_pages_per_source: int = Field(
        1,
        ge=1,
        description="每个来源最多采集页数，不代表每页条数",
    )
    max_records_per_source: int = Field(
        20,
        ge=1,
        description="每个来源最多返回职位数（BOSS 逐条详情时另受服务端安全上限约束）",
    )
    start_page: int = Field(
        1,
        ge=1,
        description="翻页游标起始页（当前仅 BOSS 生效）；配合响应里的 next_page/has_more 多轮累积",
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

    @field_validator("sources")
    @classmethod
    def _dedupe_sources(cls, value: List[SourceName]) -> List[SourceName]:
        """去重并保序，避免重复来源虚增 combinations / 重复采集同一源。"""
        deduped: List[SourceName] = []
        for source in value:
            if source not in deduped:
                deduped.append(source)
        return deduped

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
    # 扩展字段（向后兼容，不替换 ok/error/warnings）：
    # error_code    机器可读的失败类型（如 boss_access_limited / boss_cooling_down）
    # retry_after_seconds / blocked_until  BOSS 熔断/风控恢复提示
    # retryable     该失败是否值得上游自动重试；BOSS 风控/冷却为 False
    error_code: Optional[str] = None
    retry_after_seconds: Optional[int] = None
    blocked_until: Optional[str] = None
    retryable: bool = True
    # 翻页游标（当前仅 BOSS 提供）：total=可翻页总数上限，has_more=是否还有下一页，
    # next_page=下一轮应传的起始页；上层应用据此多轮累积到目标量。
    total: Optional[int] = None
    has_more: Optional[bool] = None
    next_page: Optional[int] = None
    # BOSS worker pool 观测字段（向后兼容扩展）：
    worker_id: Optional[str] = None
    worker_status: Optional[Dict[str, Any]] = None


def _province_for_city(city: Optional[str], province: Optional[str] = None) -> Optional[str]:
    """按城市名查省份；走全量城市表（缺文件时回退种子）。"""
    return region_map.province_for_city(city, province)


def _looks_login_or_blocked(text: Optional[str]) -> bool:
    """判断错误文本是否属于「账户需登录 / 命中风控」，据此决定是否提醒管理员。"""
    if not text:
        return False
    return any(marker in text for marker in _LOGIN_OR_BLOCK_MARKERS)


def _notify_admin_source_blocked(
    source: SourceName,
    reason: str,
    *,
    error_code: str,
    blocked_until: Optional[str] = None,
) -> bool:
    """来源账户需登录 / 命中风控时邮件提醒管理员，保证接口稳定在线。

    复用 JOB_SEARCH_SMTP_* 邮件配置，收件人为 REGION_JOBS_ADMIN_EMAIL；按来源做
    冷却，避免风控冷却期内对同一来源重复轰炸。SMTP 为阻塞调用，调用方应放到线程执行。
    """
    smtp_host = _settings.JOB_SEARCH_SMTP_HOST
    smtp_username = _settings.JOB_SEARCH_SMTP_USERNAME
    smtp_password = _settings.JOB_SEARCH_SMTP_PASSWORD
    smtp_from = _settings.JOB_SEARCH_SMTP_FROM or smtp_username
    admin_email = _settings.REGION_JOBS_ADMIN_EMAIL
    if not all([smtp_host, smtp_username, smtp_password, smtp_from, admin_email]):
        logger.warning(
            "[region-search] 风控/登录提醒邮件未发送：缺少 JOB_SEARCH_SMTP_HOST / "
            "JOB_SEARCH_SMTP_USERNAME / JOB_SEARCH_SMTP_PASSWORD / "
            "JOB_SEARCH_SMTP_FROM / REGION_JOBS_ADMIN_EMAIL 配置。"
        )
        return False

    now = time.monotonic()
    cooldown = max(0, _settings.REGION_JOBS_NOTIFY_COOLDOWN_SEC)
    with _notify_lock:
        last = _last_notify_ts.get(source.value, 0.0)
        if cooldown and now - last < cooldown:
            logger.info(
                "[region-search][%s] 风控/登录提醒邮件仍在冷却期内，本次不重复发送。",
                source.value,
            )
            return False
        _last_notify_ts[source.value] = now

    subject = f"【script_tools】区域岗位接口需处理：{source.value} 需要登录 / 命中风控"
    lines = [
        "区域岗位统一搜索接口检测到来源需要人工处理，接口稳定性受影响。",
        "",
        f"来源：{source.value}",
        f"类型：{error_code}",
        f"原因：{reason}",
    ]
    if blocked_until:
        lines.append(f"预计恢复：{blocked_until}")
    lines.extend([
        f"服务地址：{_settings.APP_PUBLIC_BASE_URL}",
        f"浏览器调试端口：{_settings.JOB_SEARCH_BROWSER_HOST_PORT}",
        f"触发时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "处理方式：",
        "1. 打开正在运行的调试 Chrome 浏览器。",
        "2. 在对应平台完成扫码 / 短信验证码登录或人机验证。",
        "3. 处理完成后，熔断冷却结束接口会自动恢复采集。",
    ])
    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = admin_email
    msg.set_content(body)

    try:
        if _settings.JOB_SEARCH_SMTP_USE_SSL:
            with smtplib.SMTP_SSL(
                smtp_host,
                _settings.JOB_SEARCH_SMTP_PORT,
                timeout=15,
            ) as smtp:
                smtp.login(smtp_username, smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(
                smtp_host,
                _settings.JOB_SEARCH_SMTP_PORT,
                timeout=15,
            ) as smtp:
                if _settings.JOB_SEARCH_SMTP_STARTTLS:
                    smtp.starttls()
                smtp.login(smtp_username, smtp_password)
                smtp.send_message(msg)
        logger.info(
            "[region-search][%s] 已发送风控/登录提醒邮件至 %s。", source.value, admin_email
        )
        return True
    except Exception as exc:
        logger.error(
            "[region-search][%s] 发送风控/登录提醒邮件失败: %s",
            source.value,
            exc,
            exc_info=True,
        )
        return False


@router.post(
    "/jobs/region-search",
    summary="区域岗位数据统一搜索",
    description=(
        "以业务区域为主输入，同时适配智联招聘和 BOSS 直聘。\n"
        "接口返回统一职位字段、来源状态和保守去重后的区域岗位数据。"
    ),
    dependencies=[Depends(require_api_key("REGION_JOBS_API_KEY"))],
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
    failed = [r for r in results if not r.ok]

    if payload.collection.on_source_error == SourceErrorMode.FAIL and failed:
        return create_standard_response(
            code=_failure_status_code(failed),
            message="区域岗位来源采集失败",
            data=_build_response_data(results, payload),
        )

    succeeded = [r for r in results if r.ok]
    if not succeeded:
        return create_standard_response(
            code=_failure_status_code(failed),
            message="所有区域岗位来源均采集失败",
            data=_build_response_data(results, payload),
        )

    data = _build_response_data(results, payload)
    return create_standard_response(data=data, message=f"区域岗位搜索完成，共 {len(data['jobs'])} 条")


def _failure_status_code(failed: List["SourceRunResult"]) -> int:
    """决定「全部/整体失败」时返回的 HTTP 状态码。

    - 只要有一个失败来源是可重试的瞬时错误（超时/上游 5xx 等），返回 503，
      让调用方按既有重试策略处理。
    - 若所有失败都是**非重试型**（BOSS 风控/冷却/城市未解析等），返回 409：
      避免 data_server 的 script_tools client 把它当 503 自动重试而加重风控。
    """
    if any(r.retryable for r in failed):
        return 503
    return 409


async def _run_zhilian(payload: RegionJobSearchPayload) -> SourceRunResult:
    city_name = payload.region.city
    city_id = payload.region.platform_hints.zhilian_city_id
    if city_id is None:
        city_id = region_map.zhilian_id_for_city(city_name, payload.region.province)
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

        # scrape_many 内部对每个 keyword×city 组合做 try/except，只累计
        # failed_combinations 而不抛出。这里据此区分「真实无岗位」和「采集异常」，
        # 避免所有组合都失败时仍被当成成功的空结果返回给调用方。
        combinations = int(summary.get("combinations") or len(payload.query.keywords))
        failed_combinations = int(summary.get("failed_combinations") or 0)
        if combinations and failed_combinations >= combinations and not jobs:
            error = f"智联全部关键词组合采集失败（{failed_combinations}/{combinations}）"
            logger.warning(f"[region-search][zhilian] 失败: {error}")
            return SourceRunResult(
                source=SourceName.ZHILIAN,
                ok=False,
                error=error,
                error_code="zhilian_all_failed",
                pages_fetched=int(summary.get("pages_fetched") or 0),
                queries_attempted=combinations,
                pages_requested=int(summary.get("pages_requested") or 0),
                region_code=city_id,
            )

        warnings = _empty_result_warnings(SourceName.ZHILIAN, jobs)
        if failed_combinations:
            warnings.append(
                f"智联部分关键词组合采集失败（{failed_combinations}/{combinations}），"
                f"返回结果可能不完整"
            )
        return SourceRunResult(
            source=SourceName.ZHILIAN,
            ok=True,
            jobs=jobs,
            pages_fetched=int(summary.get("pages_fetched") or 0),
            queries_attempted=combinations,
            pages_requested=int(summary.get("pages_requested") or 0),
            region_code=city_id,
            warnings=warnings,
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
            error_code="zhilian_timeout",
        )
    except Exception as exc:
        error = _source_error_message(exc, "智联采集异常")
        logger.warning(f"[region-search][zhilian] 失败: {error}", exc_info=True)
        if _looks_login_or_blocked(error):
            await asyncio.to_thread(
                _notify_admin_source_blocked,
                SourceName.ZHILIAN,
                error,
                error_code="zhilian_login_required",
            )
        return SourceRunResult(
            source=SourceName.ZHILIAN,
            ok=False,
            queries_attempted=len(payload.query.keywords),
            pages_requested=len(payload.query.keywords) * payload.collection.max_pages_per_source,
            region_code=city_id,
            error=error,
            error_code="zhilian_error",
        )


async def _run_boss(payload: RegionJobSearchPayload) -> SourceRunResult:
    boss_client: Optional[Any] = None
    pages_requested = len(payload.query.keywords) * payload.collection.max_pages_per_source
    city_code = _resolve_boss_city_code(payload.region)
    if city_code is None:
        # 城市编码缺失是配置问题，不是瞬时错误 → 非重试
        return SourceRunResult(
            source=SourceName.BOSS_ZHIPIN,
            ok=False,
            error=f"无法解析 BOSS 城市编码: {payload.region.city}",
            error_code="boss_city_unresolved",
            queries_attempted=len(payload.query.keywords),
            pages_requested=pages_requested,
            retryable=False,
        )

    # 熔断优先：冷却期内不触碰浏览器 / BOSS 接口
    is_open, blocked_until, retry_after, reason = _boss_circuit.state()
    if is_open:
        logger.info(
            "[region-search][boss_zhipin] 冷却中，跳过采集（%ss 后恢复）：%s",
            retry_after,
            reason,
        )
        return SourceRunResult(
            source=SourceName.BOSS_ZHIPIN,
            ok=False,
            error=f"BOSS 冷却中：{reason}",
            error_code="boss_cooling_down",
            retry_after_seconds=retry_after,
            blocked_until=blocked_until,
            region_code=city_code,
            queries_attempted=len(payload.query.keywords),
            pages_requested=pages_requested,
            retryable=False,
            worker_status=_boss_worker_status(),
        )

    try:
        boss_client = _get_boss_region_client()
        include_description = payload.collection.detail_level == DetailLevel.DESCRIPTION
        # BOSS 串行 + 慢节奏：逐条拉详情耗时随记录数线性增长。为保证单轮能在
        # timeout_seconds 内收尾（超时->熔断反而拿不到数据），当需要详情时对本轮实际
        # 抓取的记录数做服务端安全上限；调用方仍可要更多，由上层应用分多轮"慢慢"累积。
        effective_max_records = payload.collection.max_records_per_source
        boss_detail_cap = _settings.REGION_JOBS_BOSS_MAX_DETAIL_RECORDS
        capped_warning: Optional[str] = None
        if include_description and boss_detail_cap > 0 and effective_max_records > boss_detail_cap:
            capped_warning = (
                f"BOSS 逐条详情串行慢采，本轮记录数由 {effective_max_records} "
                f"收敛到服务端安全上限 {boss_detail_cap}，其余请分多轮累积。"
            )
            effective_max_records = boss_detail_cap
        max_items_per_query = _per_keyword_record_budget(
            effective_max_records,
            payload.query.keywords,
        )
        # 逐条详情累积路径：用较小 pageSize（=本轮每关键词预算，封顶默认页大小）保证
        # "每轮 1 页带详情"的串行慢采单轮时间可控，并与 next_page 页游标对齐。
        # summary 快路径不覆盖，沿用默认页大小，外部 workflow 行为不变。
        boss_page_size = (
            min(max_items_per_query, _settings.BOSS_ZHIPIN_DIRECT_PAGE_SIZE)
            if include_description
            else None
        )
        raw_result = await asyncio.wait_for(
            boss_client.scrape_many(
                payload.query.keywords,
                [city_code],
                payload.collection.max_pages_per_source,
                max_items_per_query,
                payload.output.include_raw,
                include_description,
                payload.collection.start_page,
                boss_page_size,
            ),
            timeout=payload.collection.timeout_seconds,
        )
        raw_jobs = (raw_result or {}).get("jobs") or []
        limited = _limit_jobs_by_keyword(
            raw_jobs,
            effective_max_records,
            payload.query.keywords,
            keyword_key="keyword",
        )
        jobs = [
            _normalize_boss_job(raw, payload=payload)
            for raw in limited
        ]
        summary = (raw_result or {}).get("summary") or {}
        warnings = (raw_result or {}).get("warnings") or []
        if capped_warning:
            warnings.append(capped_warning)
        warnings.extend(_boss_city_hint_warnings(payload.region, city_code))
        warnings.extend(_multi_keyword_page_warnings(summary, payload))
        warnings.extend(_empty_result_warnings(SourceName.BOSS_ZHIPIN, jobs))
        total_val = summary.get("total_count")
        if total_val is None:
            total_val = summary.get("res_count")
        return SourceRunResult(
            source=SourceName.BOSS_ZHIPIN,
            ok=True,
            jobs=jobs,
            pages_fetched=int(summary.get("pages_fetched") or 0),
            queries_attempted=int(summary.get("combinations") or len(payload.query.keywords)),
            pages_requested=pages_requested,
            region_code=city_code,
            warnings=warnings,
            total=int(total_val) if total_val is not None else None,
            has_more=bool(summary.get("has_more")),
            next_page=summary.get("next_page"),
            worker_id=summary.get("worker_id"),
            worker_status=summary.get("worker_status"),
        )
    except BossAccessLimitedError as exc:
        # 命中风控 → 拉闸冷却（优先用风控页给出的恢复时间），返回非重试型失败
        cooldown_sec = exc.retry_after_seconds or _settings.REGION_JOBS_BOSS_COOLDOWN_MINUTES * 60
        _boss_circuit.trip(seconds=cooldown_sec, reason=str(exc))
        _, blocked_until, retry_after, _ = _boss_circuit.state()
        logger.warning("[region-search][boss_zhipin] 访问受限：%s", exc)
        await asyncio.to_thread(
            _notify_admin_source_blocked,
            SourceName.BOSS_ZHIPIN,
            str(exc),
            error_code="boss_access_limited",
            blocked_until=blocked_until,
        )
        return SourceRunResult(
            source=SourceName.BOSS_ZHIPIN,
            ok=False,
            error=str(exc),
            error_code="boss_access_limited",
            retry_after_seconds=retry_after,
            blocked_until=blocked_until,
            region_code=city_code,
            queries_attempted=len(payload.query.keywords),
            pages_requested=pages_requested,
            retryable=False,
            worker_status=getattr(exc, "worker_status", None) or _boss_worker_status(boss_client),
        )
    except asyncio.TimeoutError:
        # 超时：底层同步线程可能仍在跑同一个共享 tab / httpx 会话，
        # 拉闸一小段时间让它自然收尾，避免下个请求进来抢资源。
        _boss_circuit.trip(
            seconds=_settings.REGION_JOBS_BOSS_TIMEOUT_COOLDOWN_SEC,
            reason="BOSS 采集超时，暂停以保护共享浏览器资源",
        )
        _, blocked_until, retry_after, _ = _boss_circuit.state()
        error = f"BOSS 采集超时: {payload.collection.timeout_seconds:g}s"
        logger.warning(f"[region-search][boss_zhipin] 失败: {error}")
        return SourceRunResult(
            source=SourceName.BOSS_ZHIPIN,
            ok=False,
            error=error,
            error_code="boss_timeout",
            retry_after_seconds=retry_after,
            blocked_until=blocked_until,
            queries_attempted=len(payload.query.keywords),
            pages_requested=pages_requested,
            region_code=city_code,
            retryable=False,
            worker_status=_boss_worker_status(boss_client),
        )
    except Exception as exc:
        error = _source_error_message(exc, "BOSS 采集异常")
        logger.warning(f"[region-search][boss_zhipin] 失败: {error}", exc_info=True)
        return SourceRunResult(
            source=SourceName.BOSS_ZHIPIN,
            ok=False,
            error=error,
            error_code="boss_error",
            queries_attempted=len(payload.query.keywords),
            pages_requested=pages_requested,
            region_code=city_code,
            worker_status=_boss_worker_status(boss_client),
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
    return region_map.boss_code_for_city(region.city, region.province)


def _boss_city_hint_warnings(region: RegionSpec, city_code: Optional[int]) -> List[str]:
    """当显式传入的 boss_city_code 与 city 名映射不一致时给出告警。

    不阻断请求，仅提示返回数据将按编码（而非 city 名）为准，避免静默地域错配。
    """
    hint = region.platform_hints.boss_city_code
    if not hint:
        return []
    expected = region_map.boss_code_for_city(region.city, region.province)
    if expected is not None and hint != expected:
        return [
            f"请求 city={region.city} 但 boss_city_code={hint} 与之不一致"
            f"（{region.city} 应为 {expected}），返回数据按编码 {hint} 为准"
        ]
    return []


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
    published_at = _extract_zhilian_publish_time(details)

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
        "published_at": published_at,
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
            "logo_url": raw.get("brand_logo"),
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
        "published_at": (
            raw.get("lastModifyTime") or raw.get("publishTime") or raw.get("publish_date")
        ),
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
    # 智联职位详情接口返回结构为 {detailedCompany, detailedPosition, taskId}，
    # 岗位描述实际在 detailedPosition.jobDesc / jobDescPC；优先深入该层，
    # 同时兼容历史的顶层结构。
    scopes: list[Dict[str, Any]] = []
    nested = details.get("detailedPosition")
    if isinstance(nested, dict):
        scopes.append(nested)
    scopes.append(details)
    for scope in scopes:
        for key in (
            "jobDesc",
            "jobDescPC",
            "jobDescription",
            "description",
            "describe",
            "responsibility",
            "jobContent",
            "content",
        ):
            value = scope.get(key)
            if isinstance(value, str) and value.strip():
                return _clean_html(value)
    return None


def _extract_zhilian_publish_time(details: Dict[str, Any]) -> Optional[Any]:
    """从智联职位详情提取发布时间（原值透传，下游按时间戳/日期串解析）。

    时间字段在 detailedPosition 层（positionPublishTime / publishTime），
    兼容历史顶层结构。优先绝对时间字段，避免拿到「3天前」这类相对描述。
    """
    if not details:
        return None
    scopes: list[Dict[str, Any]] = []
    nested = details.get("detailedPosition")
    if isinstance(nested, dict):
        scopes.append(nested)
    scopes.append(details)
    for scope in scopes:
        for key in ("positionPublishTime", "publishTime", "firstPublishTime", "refreshTime"):
            value = scope.get(key)
            if value:
                return value
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
            # 扩展字段（向后兼容）：
            "error_code": None,
            "retry_after_seconds": None,
            "blocked_until": None,
            "retryable": True,
            # 翻页游标（当前仅 BOSS 提供）：
            "total": None,
            "has_more": None,
            "next_page": None,
            # BOSS worker pool 观测字段（向后兼容扩展）：
            "worker_id": None,
            "worker_status": None,
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
            # 扩展字段（向后兼容，不替换 ok/error/warnings）：
            "error_code": result.error_code,
            "retry_after_seconds": result.retry_after_seconds,
            "blocked_until": result.blocked_until,
            "retryable": result.retryable,
            # 翻页游标（当前仅 BOSS 提供）：
            "total": result.total,
            "has_more": result.has_more,
            "next_page": result.next_page,
            # BOSS worker pool 观测字段（向后兼容扩展）：
            "worker_id": result.worker_id,
            "worker_status": result.worker_status,
        }
    return status_map


def _build_response_data(
    results: List[SourceRunResult],
    payload: RegionJobSearchPayload,
) -> Dict[str, Any]:
    all_jobs = []
    for result in results:
        all_jobs.extend(result.jobs)

    all_jobs = [_fill_region_fields(job) for job in all_jobs]

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


def _split_zhilian_address(address: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """从智联 address（如 "厦门 思明 莲前"）解析 (district, business_district)。

    约定格式为「城市 区 街道」，按空白切分：
    - 第 2 段为 district，缺行政区后缀（区/县/市/旗）时补「区」；
    - 第 3 段及之后为 business_district。
    无法解析时返回 (None, None)。
    """
    if not address:
        return None, None
    parts = [p for p in re.split(r"\s+", address.strip()) if p]
    if len(parts) < 2:
        return None, None
    district = parts[1]
    if district and district[-1] not in "区县市旗":
        district = f"{district}区"
    business_district = parts[2] if len(parts) >= 3 else None
    return district, business_district


def _fill_region_fields(job: Dict[str, Any]) -> Dict[str, Any]:
    """统一回填地域字段：province 兜底 + 智联 district/business_district 解析。

    仅在字段为空时填充，不覆盖来源已有值。
    """
    location = job.get("location") or {}

    if not location.get("province"):
        province = _province_for_city(location.get("city"), location.get("province"))
        if province:
            location["province"] = province

    if job.get("source") == SourceName.ZHILIAN.value:
        if not location.get("district"):
            district, business_district = _split_zhilian_address(location.get("address"))
            if district:
                location["district"] = district
            if business_district and not location.get("business_district"):
                location["business_district"] = business_district

    job["location"] = location
    return job


def _region_to_dict(region: RegionSpec) -> Dict[str, Any]:
    return {
        "country": region.country,
        "province": region.province,
        "city": region.city,
        "district": region.district,
    }
