# -*- coding: utf-8 -*-
"""纯离线聚合器：把当天导出的三类文件合并成一份"钱+量+质"三合一的每日消耗报告。

设计定位(严谨性)：
- 不连 Azure，只吃本地文件，随时可重跑、幂等。三类数据源各司其职、互不重复计数：
  * CSV(账单导出)      —— 唯一带【金额】。costInUsd + quantity/unitOfMeasure(token 反推)
                          + meterName(区分 输入/输出/缓存) + tags{deployment, project}。
  * calls JSON(Monitor) —— 权威【调用次数】(by_model / by_project)。
  * requests NDJSON(LA) —— 逐请求【延迟/字节/状态码/流式类型】(token 字段恒为 null,故不取)。
- token 只从 CSV quantity 反推(unitOfMeasure=1M→×1e6, 1K→×1e3)；日志侧无 token。
- 调用次数以 calls JSON(指标)为准；NDJSON 次数仅作交叉校验。
- project 归一：CSV tags.project 的 "x-pilot-default" 归到 "x-pilot"，与指标/日志口径对齐。
- 非模型开销(Storage/LogAnalytics/Bandwidth/...) 单列"基础设施成本"，不混入模型单位成本。

独立运行：
    python -m shared.usage_report --csv <csv> --calls <json> --requests <ndjson> \
        --out-dir <dir> --date 2026-08-19
- 参数可缺省：只给 --csv 也能出"纯花销报告"；缺哪份，对应小节标注"数据缺失"而非报错。
- 仅依赖标准库；HTML 内嵌 ECharts(CDN)。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
from collections import defaultdict
from typing import Any, TypedDict

CST = dt.timezone(dt.timedelta(hours=8))

# CSV tags.project 中的默认部署别名 → 归一到的项目名(与指标/日志的 x-pilot 对齐)。
_PROJECT_ALIASES = {"x-pilot-default": "x-pilot"}

# 只有这些 meterCategory 记为"模型开销"，其余归入基础设施成本。
_MODEL_METER_CATEGORY = "Foundry Models"


class ModelAgg(TypedDict):
    cost_usd: float
    calls: int
    tok_input: int
    tok_output: int
    tok_cached: int
    dur_ms_sum: float
    dur_calls: int


class ProjectAgg(TypedDict):
    cost_usd: float
    calls: int
    tok_input: int
    tok_output: int
    tok_cached: int
    top_models: dict[str, float]


def normalize_project(name: str) -> str:
    """把各来源的项目/资源名归一成统一项目名。

    - 去掉 -resource 后缀(日志 Resource 列常是大写资源名)。
    - x-pilot-default(账单默认部署别名) 归到 x-pilot。
    - 统一小写。
    """
    r = (name or "").strip().lower()
    if r.endswith("-resource"):
        r = r[: -len("-resource")]
    return _PROJECT_ALIASES.get(r, r)


def classify_token_kind(meter_name: str) -> str:
    """按 meterName 关键词判定 token 类别：cached_input / output / input。

    判定顺序至关重要：先判缓存(cache/cchd/cd inp/cached)，否则 "cached Inp"
    会被误归为普通输入；再判输出(outp/opt/output)，最后才是输入(inp/inpt)。
    对非 token 计量(不含以上关键词)返回 "other"。
    """
    m = (meter_name or "").lower()
    if any(k in m for k in ("cache", "cchd", "cd inp", "cached")):
        return "cached_input"
    if any(k in m for k in ("outp", "output")) or re.search(r"\bopt\b", m):
        return "output"
    if any(k in m for k in ("inp", "inpt")):
        return "input"
    return "other"


def _tokens_from_quantity(quantity: float, unit: str) -> int:
    """把账单 quantity + unitOfMeasure 反推成 token 数。

    unitOfMeasure 形如 "1M"/"1K"/"1M Tokens"/"1K Tokens"。1M→×1e6, 1K→×1e3。
    其余单位(如存储 GB、操作 10K)不属于 token,返回 0。
    """
    u = (unit or "").strip().upper()
    if u.startswith("1M"):
        return int(round(quantity * 1_000_000))
    if u.startswith("1K"):
        return int(round(quantity * 1_000))
    return 0


class ParsedCsv(TypedDict):
    model_cost: dict[str, float]
    model_tokens: dict[str, dict[str, int]]  # model -> {input/output/cached_input: n}
    project_cost: dict[str, float]
    project_tokens: dict[str, dict[str, int]]
    model_project_cost: dict[str, dict[str, float]]  # model -> {project: cost}
    infra_cost: dict[str, float]  # meterCategory -> cost (非模型)
    total_model_cost: float
    total_infra_cost: float
    models: set[str]
    # 成本按 token 类型(输入/输出/缓存)拆分：全局 + 模型级。
    cost_by_kind: dict[str, float]  # {input/output/cached_input: cost}
    model_cost_by_kind: dict[str, dict[str, float]]  # model -> {kind: cost}
    # 成本按计费区域(meterRegion)：全局 + 模型级。
    region_cost: dict[str, float]  # region -> cost
    model_region_cost: dict[str, dict[str, float]]  # model -> {region: cost}


def parse_csv(path: str) -> ParsedCsv:
    """解析账单 CSV：金额(costInUsd) + token(quantity 反推) + tags{deployment, project}。

    模型开销(meterCategory == Foundry Models)按 deployment/project 聚合；
    其余(Storage/LogAnalytics/Bandwidth/...) 汇入 infra_cost 单列。
    """
    model_cost: dict[str, float] = defaultdict(float)
    model_tokens: dict[str, dict[str, int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cached_input": 0}
    )
    project_cost: dict[str, float] = defaultdict(float)
    project_tokens: dict[str, dict[str, int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cached_input": 0}
    )
    model_project_cost: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    infra_cost: dict[str, float] = defaultdict(float)
    models: set[str] = set()
    cost_by_kind: dict[str, float] = defaultdict(float)
    model_cost_by_kind: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    region_cost: dict[str, float] = defaultdict(float)
    model_region_cost: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                cost = float(row.get("costInUsd") or 0.0)
            except ValueError:
                cost = 0.0
            category = (row.get("meterCategory") or "").strip()

            if category != _MODEL_METER_CATEGORY:
                if cost:
                    infra_cost[category or "(unknown)"] += cost
                continue

            tags_raw = row.get("tags") or ""
            deployment = "(unknown)"
            project = "(unknown)"
            if tags_raw:
                try:
                    tags = json.loads(tags_raw)
                    deployment = (tags.get("deployment") or deployment).strip()
                    project = normalize_project(tags.get("project") or project)
                except (json.JSONDecodeError, AttributeError):
                    pass

            models.add(deployment)
            model_cost[deployment] += cost
            project_cost[project] += cost
            model_project_cost[deployment][project] += cost

            region = (row.get("meterRegion") or "(unknown)").strip() or "(unknown)"
            region_cost[region] += cost
            model_region_cost[deployment][region] += cost

            # 成本按 token 类型拆分：用 meterName 判定这条计费属于 输入/输出/缓存。
            cost_kind = classify_token_kind(row.get("meterName", ""))
            if cost_kind == "other":
                cost_kind = "input"
            cost_by_kind[cost_kind] += cost
            model_cost_by_kind[deployment][cost_kind] += cost

            try:
                quantity = float(row.get("quantity") or 0.0)
            except ValueError:
                quantity = 0.0
            tok = _tokens_from_quantity(quantity, row.get("unitOfMeasure", ""))
            if tok:
                kind = classify_token_kind(row.get("meterName", ""))
                if kind == "other":
                    kind = "input"  # 未识别的 token 计量保守归入输入
                model_tokens[deployment][kind] += tok
                project_tokens[project][kind] += tok

    return ParsedCsv(
        model_cost=dict(model_cost),
        model_tokens={k: dict(v) for k, v in model_tokens.items()},
        project_cost=dict(project_cost),
        project_tokens={k: dict(v) for k, v in project_tokens.items()},
        model_project_cost={k: dict(v) for k, v in model_project_cost.items()},
        infra_cost=dict(infra_cost),
        total_model_cost=sum(model_cost.values()),
        total_infra_cost=sum(infra_cost.values()),
        models=models,
        cost_by_kind=dict(cost_by_kind),
        model_cost_by_kind={k: dict(v) for k, v in model_cost_by_kind.items()},
        region_cost=dict(region_cost),
        model_region_cost={k: dict(v) for k, v in model_region_cost.items()},
    )


class ParsedCalls(TypedDict):
    grand_total: int
    model_calls: dict[str, int]
    project_calls: dict[str, int]
    model_project_calls: dict[str, dict[str, int]]
    models: set[str]


def parse_calls(path: str) -> ParsedCalls:
    """解析 calls JSON(Monitor 指标)：权威调用次数(按模型 / 项目 / 模型x项目)。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    model_calls: dict[str, int] = {}
    model_project_calls: dict[str, dict[str, int]] = {}
    for entry in data.get("by_model", []):
        model = entry.get("model", "(unknown)")
        model_calls[model] = int(entry.get("calls", 0))
        by_proj = {normalize_project(p): int(c) for p, c in (entry.get("by_project") or {}).items()}
        model_project_calls[model] = by_proj

    project_calls: dict[str, int] = defaultdict(int)
    for entry in data.get("by_project", []):
        project_calls[normalize_project(entry.get("project", "(unknown)"))] += int(entry.get("calls", 0))

    return ParsedCalls(
        grand_total=int(data.get("grand_total_calls", sum(model_calls.values()))),
        model_calls=model_calls,
        project_calls=dict(project_calls),
        model_project_calls=model_project_calls,
        models=set(model_calls),
    )


class ParsedRequests(TypedDict):
    row_count: int
    grand_calls: int
    model_dur_sum: dict[str, float]
    model_dur_calls: dict[str, int]
    status_calls: dict[str, int]
    stream_calls: dict[str, int]
    stream_reqbytes: dict[str, int]
    stream_respbytes: dict[str, int]
    grand_reqbytes: int
    grand_respbytes: int
    # 按调用方 IP：真实"谁在请求"。
    ip_calls: dict[str, int]
    ip_dur_sum: dict[str, float]
    ip_dur_calls: dict[str, int]
    ip_projects: dict[str, dict[str, int]]  # ip -> {project: calls}
    ip_models: dict[str, dict[str, int]]  # ip -> {model: calls}
    ip_status: dict[str, dict[str, int]]  # ip -> {status: calls}
    # 成本分摊所需：IP 在每个 (model, project) 组合下的次数，及该组合全局总次数。
    # CSV 成本按 (model, project) 记且无 IP，故按次数占比把钱分摊到 IP。
    ip_model_project_calls: dict[str, dict[str, dict[str, int]]]  # ip -> model -> project -> calls
    model_project_calls_log: dict[str, dict[str, int]]  # model -> project -> calls(日志侧)
    # 延迟分位数(基于全部 DurationMs)、按北京小时分布、操作类型、错误下钻。
    durations: list[float]  # 全部请求延迟(ms)，用于算 p50/p90/p95/p99/max
    hour_calls: dict[str, int]  # 北京小时(00..23) -> calls
    hour_errors: dict[str, int]  # 北京小时 -> 非2xx calls
    operation_calls: dict[str, int]  # OperationName -> calls
    error_by_model: dict[str, dict[str, int]]  # status -> {model: n}
    error_by_ip: dict[str, dict[str, int]]  # status -> {ip: n}
    error_by_hour: dict[str, dict[str, int]]  # status -> {hour: n}


def _beijing_hour(time_beijing: str | None, time_utc: str | None) -> str | None:
    """从日志时间戳解析北京小时(00..23)。

    优先用日志已附的 TimeBeijing(形如 2026-08-19T09:00:00.000000+08:00)，直接取
    第 11-13 位小时；否则从 UTC 时间戳换算 +8 小时。都取不到返回 None。
    """
    if time_beijing and len(time_beijing) >= 13 and time_beijing[10] == "T":
        return time_beijing[11:13]
    if time_utc:
        try:
            s = time_utc.replace("Z", "+00:00")
            d = dt.datetime.fromisoformat(s)
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return d.astimezone(CST).strftime("%H")
        except (ValueError, TypeError):
            return None
    return None


def _is_double_write_shell(r: dict) -> bool:
    """判断一行是否为 Azure 双写产生的"空壳"RequestResponse 记录。

    Azure 平台自 2026-08-29 起对 create-response(responses API)每个请求落两条
    RequestResponse：一条真实(带 IP、DurationMs>0、字节/token 齐全)，一条空壳
    (CallerIPAddress 空、DurationMs=0、requestLength/responseLength 全 0)，二者
    共享同一 CorrelationId。空壳若不剔除会被误当成一个占约一半流量的 (unknown)
    调用方，并把成本按次数错误分摊给它。

    只有同时满足"空 IP + 零延迟 + 请求/响应字节皆 0"才判为空壳；真实请求即使
    是流式(requestLength 可能为 0)也一定有 DurationMs>0，故此判据不会误伤。
    """
    if r.get("Category") != "RequestResponse":
        return False
    ip = (r.get("CallerIPAddress") or "").strip()
    dur = r.get("DurationMs") or 0
    reqb = r.get("requestLength") or 0
    respb = r.get("responseLength") or 0
    return not ip and not dur and not reqb and not respb


def _dedup_double_write(rows: list[dict]) -> list[dict]:
    """剔除 Azure 双写空壳记录，使 NDJSON 回归"每请求一条"口径。

    两级策略，对历史(无 CorrelationId)与新导出(带 CorrelationId)文件都正确且幂等：
    1. 硬过滤空壳：丢弃 _is_double_write_shell 命中的行(历史双写文件靠此清理)。
    2. 若行带 CorrelationId：同一 RequestResponse 的 CorrelationId 仅保留 DurationMs
       最大的一条，兜底极少数"两条都 DurationMs>0"的重复。

    对正常单写日(无空壳、CorrelationId 唯一)不改变任何结果。
    """
    kept: list[dict] = []
    best_by_corr: dict[str, int] = {}  # CorrelationId -> kept 列表下标
    for r in rows:
        if _is_double_write_shell(r):
            continue
        corr = r.get("CorrelationId")
        if r.get("Category") == "RequestResponse" and corr:
            prev = best_by_corr.get(corr)
            if prev is None:
                best_by_corr[corr] = len(kept)
                kept.append(r)
            elif (r.get("DurationMs") or 0) > (kept[prev].get("DurationMs") or 0):
                kept[prev] = r
        else:
            kept.append(r)
    return kept


def parse_requests(path: str) -> ParsedRequests:
    """解析 requests NDJSON(Log Analytics)：逐请求延迟/字节/状态码/流式类型/调用方 IP。

    以 Category == RequestResponse 计"次数"，与日志侧口径一致；token 字段恒为
    null 故不取(token 一律以 CSV 为准)。CallerIPAddress 即真实调用来源 IP,
    按 IP 汇总次数/项目/模型/延迟/状态码,回答"谁在请求"。
    """
    model_dur_sum: dict[str, float] = defaultdict(float)
    model_dur_calls: dict[str, int] = defaultdict(int)
    status_calls: dict[str, int] = defaultdict(int)
    stream_calls: dict[str, int] = defaultdict(int)
    stream_reqbytes: dict[str, int] = defaultdict(int)
    stream_respbytes: dict[str, int] = defaultdict(int)
    ip_calls: dict[str, int] = defaultdict(int)
    ip_dur_sum: dict[str, float] = defaultdict(float)
    ip_dur_calls: dict[str, int] = defaultdict(int)
    ip_projects: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ip_models: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ip_status: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ip_model_project_calls: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    model_project_calls_log: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    durations: list[float] = []
    hour_calls: dict[str, int] = defaultdict(int)
    hour_errors: dict[str, int] = defaultdict(int)
    operation_calls: dict[str, int] = defaultdict(int)
    error_by_model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    error_by_ip: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    error_by_hour: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    grand_reqbytes = 0
    grand_respbytes = 0
    grand_calls = 0
    row_count = 0

    with open(path, "r", encoding="utf-8-sig") as f:
        raw_rows: list[dict] = []
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw_rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # 双写去重：剔除 Azure 2026-08-29 起产生的空壳 RequestResponse 记录，回归
    # "每请求一条"。对已在 KQL 侧去重的新文件与正常单写日均幂等。
    rows_dedup = _dedup_double_write(raw_rows)
    for r in rows_dedup:
        row_count += 1
        if r.get("Category") != "RequestResponse":
            continue

        grand_calls += 1
        model = r.get("modelDeploymentName") or r.get("modelName") or "(unknown)"
        status = str(r.get("ResultSignature") or "-")
        status_calls[status] += 1
        dur = r.get("DurationMs")
        if dur is not None:
            model_dur_sum[model] += float(dur)
            model_dur_calls[model] += 1
            durations.append(float(dur))

        ip = (r.get("CallerIPAddress") or "(unknown)").strip() or "(unknown)"
        proj = normalize_project(r.get("Resource", ""))
        ip_calls[ip] += 1
        ip_projects[ip][proj] += 1
        ip_models[ip][model] += 1
        ip_status[ip][status] += 1
        ip_model_project_calls[ip][model][proj] += 1
        model_project_calls_log[model][proj] += 1
        if dur is not None:
            ip_dur_sum[ip] += float(dur)
            ip_dur_calls[ip] += 1

        operation_calls[r.get("OperationName") or "(unknown)"] += 1

        # 北京小时分桶：优先用日志里已给的 TimeBeijing(带 +08:00)，否则从 UTC 换算。
        hour = _beijing_hour(r.get("TimeBeijing"), r.get("TimeGenerated"))
        if hour is not None:
            hour_calls[hour] += 1
            is_err = not status.startswith("2")
            if is_err:
                hour_errors[hour] += 1
                error_by_hour[status][hour] += 1
        if not status.startswith("2"):
            error_by_model[status][model] += 1
            error_by_ip[status][ip] += 1

        reqb = int(r.get("requestLength") or 0)
        respb = int(r.get("responseLength") or 0)
        grand_reqbytes += reqb
        grand_respbytes += respb
        st = r.get("streamType") or "(unknown)"
        stream_calls[st] += 1
        stream_reqbytes[st] += reqb
        stream_respbytes[st] += respb

    return ParsedRequests(
        row_count=row_count,
        grand_calls=grand_calls,
        model_dur_sum=dict(model_dur_sum),
        model_dur_calls=dict(model_dur_calls),
        status_calls=dict(status_calls),
        stream_calls=dict(stream_calls),
        stream_reqbytes=dict(stream_reqbytes),
        stream_respbytes=dict(stream_respbytes),
        grand_reqbytes=grand_reqbytes,
        grand_respbytes=grand_respbytes,
        ip_calls=dict(ip_calls),
        ip_dur_sum=dict(ip_dur_sum),
        ip_dur_calls=dict(ip_dur_calls),
        ip_projects={k: dict(v) for k, v in ip_projects.items()},
        ip_models={k: dict(v) for k, v in ip_models.items()},
        ip_status={k: dict(v) for k, v in ip_status.items()},
        ip_model_project_calls={
            ip: {m: dict(pc) for m, pc in mpc.items()}
            for ip, mpc in ip_model_project_calls.items()
        },
        model_project_calls_log={m: dict(pc) for m, pc in model_project_calls_log.items()},
        durations=durations,
        hour_calls=dict(hour_calls),
        hour_errors=dict(hour_errors),
        operation_calls=dict(operation_calls),
        error_by_model={k: dict(v) for k, v in error_by_model.items()},
        error_by_ip={k: dict(v) for k, v in error_by_ip.items()},
        error_by_hour={k: dict(v) for k, v in error_by_hour.items()},
    )


# --------------------------------------------------------------------------- #
# 聚合：把三类解析结果合并成模型 / 项目 双维度视图 + 一致性校验
# --------------------------------------------------------------------------- #


class ModelRow(TypedDict):
    model: str
    cost_usd: float
    cost_pct: float
    calls: int
    tok_input: int
    tok_output: int
    tok_cached: int
    tok_total: int
    unit_cost: float  # $/次
    cost_per_1k_tok: float  # $/1k token
    avg_latency_ms: float


class ProjectRow(TypedDict):
    project: str
    cost_usd: float
    cost_pct: float
    calls: int
    tok_total: int
    top_models: str


class Consistency(TypedDict):
    calls_json: int
    calls_ndjson: int
    calls_diff: int
    models_only_in_csv: list[str]
    models_only_in_calls: list[str]
    notes: list[str]


class IpRow(TypedDict):
    ip: str
    calls: int
    calls_pct: float
    cost_usd: float  # 按次数占比从 CSV 分摊到该 IP 的花销
    cost_pct: float
    cost_by_model: dict[str, float]  # 该 IP 在各模型上的分摊花销
    avg_latency_ms: float
    top_projects: str
    top_models: str
    err_calls: int  # 非 2xx 次数


class Report(TypedDict):
    date: str
    generated_at: str
    has_csv: bool
    has_calls: bool
    has_requests: bool
    total_cost_usd: float
    total_infra_cost_usd: float
    total_calls: int
    total_tokens: int
    active_models: int
    active_projects: int
    active_ips: int
    by_model: list[ModelRow]
    by_project: list[ProjectRow]
    by_ip: list[IpRow]
    token_split: dict[str, int]  # input/output/cached_input 合计
    infra_cost: dict[str, float]
    status_calls: dict[str, int]
    stream_calls: dict[str, int]
    stream_reqbytes: dict[str, int]
    stream_respbytes: dict[str, int]
    grand_reqbytes: int
    grand_respbytes: int
    consistency: Consistency
    # 深度维度
    cost_by_kind: dict[str, float]  # 全局 输入/输出/缓存 成本
    model_cost_by_kind: dict[str, dict[str, float]]  # model -> {kind: cost}
    region_cost: dict[str, float]  # region -> cost
    model_region_cost: dict[str, dict[str, float]]  # model -> {region: cost}
    latency_pct: dict[str, float]  # {p50/p90/p95/p99/max/avg}
    hour_calls: dict[str, int]  # 北京小时 -> calls
    hour_errors: dict[str, int]  # 北京小时 -> 非2xx
    operation_calls: dict[str, int]  # OperationName -> calls
    error_by_model: dict[str, dict[str, int]]
    error_by_ip: dict[str, dict[str, int]]
    error_by_hour: dict[str, dict[str, int]]
    total_errors: int


def build_report(
    date: str,
    csv_data: ParsedCsv | None,
    calls_data: ParsedCalls | None,
    req_data: ParsedRequests | None,
) -> Report:
    """把三类解析结果合并成一份结构化报告。缺失的数据源以空值降级，不报错。

    维度约定：
    - 金额/ token 来自 CSV；调用次数优先来自 calls JSON，缺失时回退 NDJSON。
    - 单位成本($/次) = 模型花销 / 模型调用次数(次数取自指标；无则回退日志)。
    - 平均延迟来自 NDJSON(指标不含延迟)。
    """
    csv_data = csv_data or ParsedCsv(
        model_cost={}, model_tokens={}, project_cost={}, project_tokens={},
        model_project_cost={}, infra_cost={}, total_model_cost=0.0,
        total_infra_cost=0.0, models=set(),
        cost_by_kind={}, model_cost_by_kind={}, region_cost={}, model_region_cost={},
    )
    calls_data = calls_data or ParsedCalls(
        grand_total=0, model_calls={}, project_calls={}, model_project_calls={}, models=set(),
    )
    req_data = req_data or ParsedRequests(
        row_count=0, grand_calls=0, model_dur_sum={}, model_dur_calls={},
        status_calls={}, stream_calls={}, stream_reqbytes={}, stream_respbytes={},
        grand_reqbytes=0, grand_respbytes=0,
        ip_calls={}, ip_dur_sum={}, ip_dur_calls={}, ip_projects={}, ip_models={},
        ip_status={},
        ip_model_project_calls={}, model_project_calls_log={},
        durations=[], hour_calls={}, hour_errors={}, operation_calls={},
        error_by_model={}, error_by_ip={}, error_by_hour={},
    )

    total_cost = csv_data["total_model_cost"]

    # 所有出现过的模型(取并集，避免任一来源漏模型)。
    all_models = set(csv_data["model_cost"]) | set(calls_data["model_calls"])

    by_model: list[ModelRow] = []
    for m in all_models:
        cost = csv_data["model_cost"].get(m, 0.0)
        toks = csv_data["model_tokens"].get(m, {})
        ti = toks.get("input", 0)
        to = toks.get("output", 0)
        tc = toks.get("cached_input", 0)
        tok_total = ti + to + tc
        # 次数优先指标；指标无该模型时回退日志的按模型延迟计数(近似次数)。
        calls = calls_data["model_calls"].get(m, 0) or req_data["model_dur_calls"].get(m, 0)
        dur_sum = req_data["model_dur_sum"].get(m, 0.0)
        dur_calls = req_data["model_dur_calls"].get(m, 0)
        by_model.append(ModelRow(
            model=m,
            cost_usd=cost,
            cost_pct=(cost / total_cost * 100) if total_cost else 0.0,
            calls=calls,
            tok_input=ti, tok_output=to, tok_cached=tc, tok_total=tok_total,
            unit_cost=(cost / calls) if calls else 0.0,
            cost_per_1k_tok=(cost / tok_total * 1000) if tok_total else 0.0,
            avg_latency_ms=(dur_sum / dur_calls) if dur_calls else 0.0,
        ))
    by_model.sort(key=lambda x: -x["cost_usd"])

    # 项目维度：花销 + 次数 + token + Top 模型(按该项目下的花销)。
    all_projects = set(csv_data["project_cost"]) | set(calls_data["project_calls"])
    proj_top_models: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for model, projs in csv_data["model_project_cost"].items():
        for proj, c in projs.items():
            proj_top_models[proj].append((model, c))

    by_project: list[ProjectRow] = []
    for p in all_projects:
        cost = csv_data["project_cost"].get(p, 0.0)
        ptoks = csv_data["project_tokens"].get(p, {})
        tok_total = ptoks.get("input", 0) + ptoks.get("output", 0) + ptoks.get("cached_input", 0)
        tops = sorted(proj_top_models.get(p, []), key=lambda x: -x[1])[:2]
        top_str = ", ".join(f"{m}(${c:.2f})" for m, c in tops)
        by_project.append(ProjectRow(
            project=p,
            cost_usd=cost,
            cost_pct=(cost / total_cost * 100) if total_cost else 0.0,
            calls=calls_data["project_calls"].get(p, 0),
            tok_total=tok_total,
            top_models=top_str,
        ))
    by_project.sort(key=lambda x: -x["cost_usd"])

    token_split = {
        "input": sum(t.get("input", 0) for t in csv_data["model_tokens"].values()),
        "output": sum(t.get("output", 0) for t in csv_data["model_tokens"].values()),
        "cached_input": sum(t.get("cached_input", 0) for t in csv_data["model_tokens"].values()),
    }
    total_tokens = sum(token_split.values())
    total_calls = calls_data["grand_total"] or req_data["grand_calls"]

    # 一致性校验
    notes: list[str] = []
    diff = calls_data["grand_total"] - req_data["grand_calls"]
    if calls_data["grand_total"] and req_data["grand_calls"] and abs(diff) > max(5, calls_data["grand_total"] * 0.01):
        notes.append(
            f"指标次数({calls_data['grand_total']:,})与日志次数({req_data['grand_calls']:,})"
            f"相差 {abs(diff):,}，超过 1% 阈值，请核查诊断日志是否有缺采。"
        )
    only_csv = sorted(csv_data["models"] - calls_data["models"]) if calls_data["models"] else []
    only_calls = sorted(calls_data["models"] - csv_data["models"]) if csv_data["models"] else []
    if only_csv:
        notes.append(f"以下模型有花销但无指标次数(可能仅缓存/异步计费)：{', '.join(only_csv)}")
    if only_calls:
        notes.append(f"以下模型有调用次数但当日无账单花销(可能免费额度/延迟入账)：{', '.join(only_calls)}")

    consistency = Consistency(
        calls_json=calls_data["grand_total"],
        calls_ndjson=req_data["grand_calls"],
        calls_diff=diff,
        models_only_in_csv=only_csv,
        models_only_in_calls=only_calls,
        notes=notes,
    )

    # 按调用方 IP：真实"谁在请求"。次数占比基于日志总次数(而非指标)。
    # 成本分摊：CSV 成本按 (model, project) 记且无 IP，按该 IP 在此组合下的次数占比
    # 把钱分摊到 IP。分摊后各 IP 成本之和 == CSV 模型总花销(组合完全对齐时)。
    ip_grand = req_data["grand_calls"] or 1
    total_cost_alloc = 0.0
    ip_cost: dict[str, float] = defaultdict(float)
    ip_cost_by_model: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for ip, mpc in req_data["ip_model_project_calls"].items():
        for model, pc in mpc.items():
            for proj, cnt in pc.items():
                denom = req_data["model_project_calls_log"].get(model, {}).get(proj, 0)
                if not denom:
                    continue
                combo_cost = csv_data["model_project_cost"].get(model, {}).get(proj, 0.0)
                share = combo_cost * (cnt / denom)
                ip_cost[ip] += share
                ip_cost_by_model[ip][model] += share
    total_cost_alloc = sum(ip_cost.values())

    by_ip: list[IpRow] = []
    for ip, c in req_data["ip_calls"].items():
        dur_sum = req_data["ip_dur_sum"].get(ip, 0.0)
        dur_calls = req_data["ip_dur_calls"].get(ip, 0)
        tp = sorted(req_data["ip_projects"].get(ip, {}).items(), key=lambda x: -x[1])[:3]
        tm = sorted(req_data["ip_models"].get(ip, {}).items(), key=lambda x: -x[1])[:3]
        err = sum(n for s, n in req_data["ip_status"].get(ip, {}).items()
                  if not s.startswith("2"))
        cost = ip_cost.get(ip, 0.0)
        cbm = dict(sorted(ip_cost_by_model.get(ip, {}).items(), key=lambda x: -x[1]))
        by_ip.append(IpRow(
            ip=ip,
            calls=c,
            calls_pct=c / ip_grand * 100,
            cost_usd=cost,
            cost_pct=(cost / total_cost * 100) if total_cost else 0.0,
            cost_by_model=cbm,
            avg_latency_ms=(dur_sum / dur_calls) if dur_calls else 0.0,
            top_projects=", ".join(f"{p}({n})" for p, n in tp),
            top_models=", ".join(f"{m}({n})" for m, n in tm),
            err_calls=err,
        ))
    by_ip.sort(key=lambda x: (-x["cost_usd"], -x["calls"]))

    # 延迟分位数：对全部 DurationMs 排序取分位。平均值会被长尾拉高，分位更能反映真实体验。
    durs = sorted(req_data["durations"])
    def _pct(q: float) -> float:
        if not durs:
            return 0.0
        idx = min(len(durs) - 1, int(len(durs) * q))
        return float(durs[idx])
    latency_pct = {
        "avg": (sum(durs) / len(durs)) if durs else 0.0,
        "p50": _pct(0.50),
        "p90": _pct(0.90),
        "p95": _pct(0.95),
        "p99": _pct(0.99),
        "max": float(durs[-1]) if durs else 0.0,
    }
    total_errors = sum(n for s, n in req_data["status_calls"].items() if not s.startswith("2"))

    # 成本分摊自洽校验：分摊到各 IP 的成本之和应等于模型总花销(组合完全对齐时)。
    if total_cost and total_cost_alloc and abs(total_cost_alloc - total_cost) > max(0.01, total_cost * 0.001):
        consistency["notes"].append(
            f"IP 成本分摊合计 ${total_cost_alloc:,.2f} 与模型总花销 ${total_cost:,.2f} 不符"
            f"(差 ${total_cost_alloc - total_cost:+,.2f})，可能有 (模型,项目) 组合在账单与日志间未对齐。"
        )

    return Report(
        date=date,
        generated_at=dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S+08:00"),
        has_csv=bool(csv_data["model_cost"] or csv_data["infra_cost"]),
        has_calls=bool(calls_data["model_calls"]),
        has_requests=bool(req_data["grand_calls"]),
        total_cost_usd=total_cost,
        total_infra_cost_usd=csv_data["total_infra_cost"],
        total_calls=total_calls,
        total_tokens=total_tokens,
        active_models=len([m for m in by_model if m["cost_usd"] > 0 or m["calls"] > 0]),
        active_projects=len([p for p in by_project if p["cost_usd"] > 0 or p["calls"] > 0]),
        active_ips=len(by_ip),
        by_model=by_model,
        by_project=by_project,
        by_ip=by_ip,
        token_split=token_split,
        infra_cost=dict(sorted(csv_data["infra_cost"].items(), key=lambda x: -x[1])),
        status_calls=req_data["status_calls"],
        stream_calls=req_data["stream_calls"],
        stream_reqbytes=req_data["stream_reqbytes"],
        stream_respbytes=req_data["stream_respbytes"],
        grand_reqbytes=req_data["grand_reqbytes"],
        grand_respbytes=req_data["grand_respbytes"],
        consistency=consistency,
        cost_by_kind=csv_data["cost_by_kind"],
        model_cost_by_kind=csv_data["model_cost_by_kind"],
        region_cost=dict(sorted(csv_data["region_cost"].items(), key=lambda x: -x[1])),
        model_region_cost=csv_data["model_region_cost"],
        latency_pct=latency_pct,
        hour_calls=req_data["hour_calls"],
        hour_errors=req_data["hour_errors"],
        operation_calls=dict(sorted(req_data["operation_calls"].items(), key=lambda x: -x[1])),
        error_by_model=req_data["error_by_model"],
        error_by_ip=req_data["error_by_ip"],
        error_by_hour=req_data["error_by_hour"],
        total_errors=total_errors,
    )


def _hbytes(n: int | float) -> str:
    """字节数 → 人类可读(B/KB/MB/GB)。"""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _htok(n: int) -> str:
    """token 数 → 人类可读(K/M/B)。"""
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _usd(n: float) -> str:
    return f"${n:,.2f}" if abs(n) >= 0.01 else f"${n:,.4f}"


def to_markdown(rep: Report) -> bytes:
    """渲染 GitHub 风格 Markdown 报告(表格对齐、可直接在编辑器/网页预览)。"""
    L: list[str] = []
    L.append(f"# Azure 模型每日消耗报告 · {rep['date']}")
    L.append("")
    L.append(f"> 生成时间：{rep['generated_at']}　|　货币：USD")
    src = []
    src.append("账单CSV✅" if rep["has_csv"] else "账单CSV❌缺失")
    src.append("调用次数JSON✅" if rep["has_calls"] else "调用次数JSON❌缺失")
    src.append("请求日志NDJSON✅" if rep["has_requests"] else "请求日志NDJSON❌缺失")
    L.append(f"> 数据源：{'　'.join(src)}")
    L.append("")

    L.append("## 总览")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|---:|")
    L.append(f"| 当日模型总花销 | **{_usd(rep['total_cost_usd'])}** |")
    L.append(f"| 基础设施花销(存储/日志/带宽等) | {_usd(rep['total_infra_cost_usd'])} |")
    L.append(f"| 当日总花销 | **{_usd(rep['total_cost_usd'] + rep['total_infra_cost_usd'])}** |")
    L.append(f"| 总调用次数 | {rep['total_calls']:,} |")
    L.append(f"| 总 token(输入+输出+缓存) | {_htok(rep['total_tokens'])} ({rep['total_tokens']:,}) |")
    L.append(f"| 活跃模型数 | {rep['active_models']} |")
    L.append(f"| 活跃项目数 | {rep['active_projects']} |")
    L.append(f"| 活跃调用方 IP 数 | {rep['active_ips']} |")
    if rep["by_model"]:
        tm = rep["by_model"][0]
        L.append(f"| 花销最高模型 | {tm['model']}（{_usd(tm['cost_usd'])}，{tm['cost_pct']:.1f}%） |")
    if rep["by_project"]:
        tp = rep["by_project"][0]
        L.append(f"| 花销最高项目 | {tp['project']}（{_usd(tp['cost_usd'])}，{tp['cost_pct']:.1f}%） |")
    L.append("")

    L.append("## 按模型 · 花销 / 次数 / token / 单位成本")
    L.append("")
    L.append("| # | 模型 | 花销 | 占比 | 调用次数 | 输入tok | 输出tok | 缓存tok | 单位成本($/次) | $/1k tok | 平均延迟ms |")
    L.append("|--:|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for i, m in enumerate(rep["by_model"], 1):
        L.append(
            f"| {i} | {m['model']} | {_usd(m['cost_usd'])} | {m['cost_pct']:.2f}% | "
            f"{m['calls']:,} | {_htok(m['tok_input'])} | {_htok(m['tok_output'])} | "
            f"{_htok(m['tok_cached'])} | {m['unit_cost']:.4f} | {m['cost_per_1k_tok']:.4f} | "
            f"{m['avg_latency_ms']:.0f} |"
        )
    L.append(f"| | **合计** | **{_usd(rep['total_cost_usd'])}** | 100% | "
             f"**{sum(m['calls'] for m in rep['by_model']):,}** | | | | | | |")
    L.append("")

    L.append("## 按项目 · 谁在花钱")
    L.append("")
    L.append("| # | 项目 | 花销 | 占比 | 调用次数 | token | Top 模型(花销) |")
    L.append("|--:|---|--:|--:|--:|--:|---|")
    for i, p in enumerate(rep["by_project"], 1):
        L.append(
            f"| {i} | {p['project']} | {_usd(p['cost_usd'])} | {p['cost_pct']:.2f}% | "
            f"{p['calls']:,} | {_htok(p['tok_total'])} | {p['top_models']} |"
        )
    L.append("")

    if rep["by_ip"]:
        L.append("## 按调用方 IP · 谁在真实请求 + 花了多少钱")
        L.append("")
        L.append("> IP 来自诊断日志 CallerIPAddress(末段脱敏)。花销为按次数占比从账单分摊(账单无 IP 维度)，各 IP 花销之和 = 模型总花销。")
        L.append("")
        L.append("| # | 调用方 IP | 总花销 | 花销占比 | 请求次数 | 次数占比 | 平均延迟ms | 错误数 | 主要项目 |")
        L.append("|--:|---|--:|--:|--:|--:|--:|--:|---|")
        for i, ip in enumerate(rep["by_ip"], 1):
            L.append(
                f"| {i} | {ip['ip']} | {_usd(ip['cost_usd'])} | {ip['cost_pct']:.2f}% | "
                f"{ip['calls']:,} | {ip['calls_pct']:.2f}% | {ip['avg_latency_ms']:.0f} | "
                f"{ip['err_calls']} | {ip['top_projects']} |"
            )
        L.append("")
        L.append("### 每个 IP 的模型花销明细")
        L.append("")
        L.append("| 调用方 IP | 总花销 | 按模型花销(降序) |")
        L.append("|---|--:|---|")
        for ip in rep["by_ip"]:
            detail = ", ".join(f"{m} {_usd(c)}" for m, c in ip["cost_by_model"].items()) or "-"
            L.append(f"| {ip['ip']} | {_usd(ip['cost_usd'])} | {detail} |")
        L.append("")

    ts = rep["token_split"]
    tt = rep["total_tokens"] or 1
    L.append("## token 结构拆分")
    L.append("")
    L.append("| 类型 | token | 占比 |")
    L.append("|---|--:|--:|")
    L.append(f"| 输入(input) | {_htok(ts['input'])} | {ts['input'] / tt * 100:.1f}% |")
    L.append(f"| 输出(output) | {_htok(ts['output'])} | {ts['output'] / tt * 100:.1f}% |")
    L.append(f"| 缓存输入(cached) | {_htok(ts['cached_input'])} | {ts['cached_input'] / tt * 100:.1f}% |")
    L.append("")

    # 成本结构拆分(输入/输出/缓存)
    cbk = rep["cost_by_kind"]
    ctot = sum(cbk.values()) or 1
    if cbk:
        L.append("## 成本结构拆分 · 钱花在输入/输出/缓存")
        L.append("")
        L.append("| 类型 | 花销 | 占比 |")
        L.append("|---|--:|--:|")
        for k, label in (("input", "输入"), ("output", "输出"), ("cached_input", "缓存输入")):
            v = cbk.get(k, 0.0)
            L.append(f"| {label} | {_usd(v)} | {v / ctot * 100:.1f}% |")
        L.append("")
        L.append("**各模型成本拆分 + 缓存命中占比**（缓存占比越高越省钱）")
        L.append("")
        L.append("| 模型 | 输入$ | 输出$ | 缓存$ | 缓存占比 |")
        L.append("|---|--:|--:|--:|--:|")
        mck = rep["model_cost_by_kind"]
        for m in rep["by_model"]:
            kc = mck.get(m["model"], {})
            mt = sum(kc.values()) or 1
            L.append(
                f"| {m['model']} | {_usd(kc.get('input', 0))} | {_usd(kc.get('output', 0))} | "
                f"{_usd(kc.get('cached_input', 0))} | {kc.get('cached_input', 0) / mt * 100:.0f}% |"
            )
        L.append("")

    # 区域成本
    if rep["region_cost"]:
        L.append("## 成本按区域(部署地)")
        L.append("")
        L.append("| 区域 | 花销 | 占比 |")
        L.append("|---|--:|--:|")
        rtot = sum(rep["region_cost"].values()) or 1
        for reg, c in rep["region_cost"].items():
            L.append(f"| {reg} | {_usd(c)} | {c / rtot * 100:.1f}% |")
        L.append("")

    if rep["has_requests"]:
        L.append("## 请求质量")
        L.append("")
        L.append(f"请求体总量 {_hbytes(rep['grand_reqbytes'])}　|　响应体总量 {_hbytes(rep['grand_respbytes'])}（流式含 SSE 框架，偏大）")
        L.append("")
        lp = rep["latency_pct"]
        L.append("**延迟分位数(ms)**　平均值会被长尾拉高，分位更能反映真实体验")
        L.append("")
        L.append("| 平均 | p50(中位) | p90 | p95 | p99 | 最大 |")
        L.append("|--:|--:|--:|--:|--:|--:|")
        L.append(
            f"| {lp['avg']:.0f} | {lp['p50']:.0f} | {lp['p90']:.0f} | "
            f"{lp['p95']:.0f} | {lp['p99']:.0f} | {lp['max']:.0f} |"
        )
        L.append("")
        if rep["operation_calls"]:
            L.append("**操作类型(API 接口)**")
            L.append("")
            L.append("| OperationName | 次数 |")
            L.append("|---|--:|")
            for op, c in rep["operation_calls"].items():
                L.append(f"| {op} | {c:,} |")
            L.append("")
        if rep["hour_calls"]:
            L.append("**按小时请求分布(北京时间)**")
            L.append("")
            L.append("| 时段 | 请求数 | 错误数 |")
            L.append("|---|--:|--:|")
            for h in sorted(rep["hour_calls"]):
                L.append(f"| {h}:00 | {rep['hour_calls'][h]:,} | {rep['hour_errors'].get(h, 0)} |")
            L.append("")
        L.append("**状态码分布**")
        L.append("")
        L.append("| 状态码 | 次数 |")
        L.append("|---|--:|")
        for st, c in sorted(rep["status_calls"].items(), key=lambda x: -x[1]):
            L.append(f"| {st} | {c:,} |")
        L.append("")
        if rep["stream_calls"]:
            L.append("**流式类型**")
            L.append("")
            L.append("| streamType | 次数 | 请求量 | 响应量 |")
            L.append("|---|--:|--:|--:|")
            for st, c in sorted(rep["stream_calls"].items(), key=lambda x: -x[1]):
                L.append(
                    f"| {st} | {c:,} | {_hbytes(rep['stream_reqbytes'].get(st, 0))} | "
                    f"{_hbytes(rep['stream_respbytes'].get(st, 0))} |"
                )
            L.append("")

    if rep["infra_cost"]:
        L.append("## 基础设施成本(非模型)")
        L.append("")
        L.append("| 类别 | 花销 |")
        L.append("|---|--:|")
        for cat, c in rep["infra_cost"].items():
            L.append(f"| {cat} | {_usd(c)} |")
        L.append("")

    if rep["total_errors"] > 0:
        L.append("## 错误下钻 · 谁在报错")
        L.append("")
        L.append(f"当日错误(非2xx)共 {rep['total_errors']} 次。下面按错误码归因到模型 / IP / 时段。")
        L.append("")
        L.append("| 错误码 | 次数 | 主要模型 | 主要来源 IP | 高发时段 |")
        L.append("|---|--:|---|---|---|")
        err_codes = sorted(
            (s for s in rep["status_calls"] if not s.startswith("2")),
            key=lambda s: -rep["status_calls"][s],
        )
        for s in err_codes:
            n = rep["status_calls"][s]
            tm = sorted(rep["error_by_model"].get(s, {}).items(), key=lambda x: -x[1])[:2]
            ti = sorted(rep["error_by_ip"].get(s, {}).items(), key=lambda x: -x[1])[:2]
            th = sorted(rep["error_by_hour"].get(s, {}).items(), key=lambda x: -x[1])[:2]
            L.append(
                f"| {s} | {n} | {', '.join(f'{m}({c})' for m, c in tm) or '-'} | "
                f"{', '.join(f'{i}({c})' for i, c in ti) or '-'} | "
                f"{', '.join(f'{h}:00({c})' for h, c in th) or '-'} |"
            )
        L.append("")

    L.append("## 数据一致性校验")
    L.append("")
    cons = rep["consistency"]
    L.append(f"- 指标次数(JSON)：{cons['calls_json']:,}　vs　日志次数(NDJSON)：{cons['calls_ndjson']:,}　"
             f"(差 {cons['calls_diff']:+,})")
    if cons["notes"]:
        for n in cons["notes"]:
            L.append(f"- ⚠ {n}")
    else:
        L.append("- ✅ 未发现明显异常。")
    L.append("")

    return ("\n".join(L) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# 渲染：自包含 HTML(内嵌 ECharts CDN + 数据 JSON)
# --------------------------------------------------------------------------- #

_ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Azure 模型每日消耗报告 · __DATE__</title>
<script src="__ECHARTS_CDN__"></script>
<style>
  :root { --bg:#0f1419; --card:#1a2029; --line:#2a323d; --fg:#e6e9ef; --muted:#8a94a6;
          --accent:#4f9cf9; --good:#3fb950; --warn:#d29922; --bad:#f85149; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; }
  .wrap { max-width:1200px; margin:0 auto; padding:24px 20px 60px; }
  h1 { font-size:24px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-bottom:24px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; }
  .card .k { color:var(--muted); font-size:12px; margin-bottom:6px; }
  .card .v { font-size:22px; font-weight:700; }
  .card .v.big { color:var(--accent); }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-bottom:18px; }
  @media (max-width:820px){ .grid2 { grid-template-columns:1fr; } }
  .chart { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px; }
  .chart h3 { margin:4px 8px 8px; font-size:15px; font-weight:600; }
  .box { width:100%; height:340px; }
  .box.tall { height:420px; }
  h2 { font-size:18px; margin:28px 0 12px; border-left:4px solid var(--accent); padding-left:10px; }
  table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line);
          border-radius:12px; overflow:hidden; font-size:13px; }
  th,td { padding:9px 12px; text-align:right; border-bottom:1px solid var(--line); white-space:nowrap; }
  th:nth-child(2),td:nth-child(2){ text-align:left; }
  th { background:#222b36; color:var(--muted); font-weight:600; position:sticky; top:0; }
  tbody tr:hover { background:#20293433; }
  tfoot td { font-weight:700; color:var(--accent); }
  .notes { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 18px; font-size:13px; }
  .notes li { margin:4px 0; }
  .warn { color:var(--warn); }
  .ok { color:var(--good); }
  .missing { color:var(--bad); }
  .tag { display:inline-block; padding:2px 8px; border-radius:6px; background:#222b36; font-size:12px; margin-right:6px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Azure 模型每日消耗报告 · <span id="hdate"></span></h1>
  <div class="sub" id="hsub"></div>
  <div class="cards" id="cards"></div>

  <div class="grid2">
    <div class="chart"><h3>花销按模型(USD)</h3><div id="c_cost_model" class="box"></div></div>
    <div class="chart"><h3>花销按项目(USD)</h3><div id="c_cost_proj" class="box"></div></div>
  </div>
  <div class="chart" style="margin-bottom:18px;">
    <h3>调用次数 vs 花销 (双轴对比 · 找"贵但少 / 便宜但多"的模型)</h3>
    <div id="c_calls_cost" class="box tall"></div>
  </div>
  <div class="grid2">
    <div class="chart"><h3>token 结构(输入/输出/缓存 按模型堆叠)</h3><div id="c_tokens" class="box"></div></div>
    <div class="chart"><h3>平均延迟(ms)</h3><div id="c_latency" class="box"></div></div>
  </div>
  <div class="grid2">
    <div class="chart"><h3>状态码分布</h3><div id="c_status" class="box"></div></div>
    <div class="chart"><h3>token 总体占比</h3><div id="c_tokpie" class="box"></div></div>
  </div>
  <div class="chart" style="margin-bottom:18px;">
    <h3>调用方 IP · 花销 vs 请求量 (谁在真实花钱)</h3>
    <div id="c_ip" class="box tall"></div>
  </div>
  <div class="chart" style="margin-bottom:18px;">
    <h3>调用方 IP × 模型 花销拆分 (每个 IP 的钱花在哪些模型)</h3>
    <div id="c_ip_model" class="box tall"></div>
  </div>

  <div class="grid2">
    <div class="chart"><h3>成本拆分(输入/输出/缓存 按模型)</h3><div id="c_cost_kind" class="box"></div></div>
    <div class="chart"><h3>成本按区域(部署地)</h3><div id="c_region" class="box"></div></div>
  </div>
  <div class="grid2">
    <div class="chart"><h3>延迟分位数(ms · 长尾比平均更真实)</h3><div id="c_latpct" class="box"></div></div>
    <div class="chart"><h3>按小时请求分布(北京时间)</h3><div id="c_hour" class="box"></div></div>
  </div>

  <h2>按模型明细</h2>
  <table id="t_model"></table>
  <h2>按项目明细</h2>
  <table id="t_proj"></table>
  <h2>按调用方 IP 明细 · 谁在真实请求</h2>
  <table id="t_ip"></table>
  <h2>错误下钻 · 谁在报错</h2>
  <table id="t_err"></table>
  <h2>数据一致性校验</h2>
  <div class="notes" id="notes"></div>
</div>

<script>
const DATA = __DATA_JSON__;
const usd = n => (Math.abs(n) >= 0.01 ? '$' + n.toFixed(2) : '$' + n.toFixed(4));
const htok = n => n >= 1e9 ? (n/1e9).toFixed(2)+'B' : n >= 1e6 ? (n/1e6).toFixed(2)+'M'
                : n >= 1e3 ? (n/1e3).toFixed(1)+'K' : String(n);
const PALETTE = ['#4f9cf9','#3fb950','#d29922','#f85149','#a371f7','#39c5cf','#db61a2','#e3b341','#57ab5a','#6e7681'];
const baseGrid = { left:70, right:30, top:30, bottom:60 };
const axisStyle = { axisLine:{lineStyle:{color:'#3a4450'}}, axisLabel:{color:'#8a94a6'}, splitLine:{lineStyle:{color:'#222b36'}} };

document.getElementById('hdate').textContent = DATA.date;
document.getElementById('hsub').innerHTML =
  '生成时间 ' + DATA.generated_at + '　|　货币 USD　|　'
  + (DATA.has_csv?'<span class="tag ok">账单CSV</span>':'<span class="tag missing">账单CSV缺失</span>')
  + (DATA.has_calls?'<span class="tag ok">次数JSON</span>':'<span class="tag missing">次数JSON缺失</span>')
  + (DATA.has_requests?'<span class="tag ok">日志NDJSON</span>':'<span class="tag missing">日志NDJSON缺失</span>');

const topM = DATA.by_model[0] || {model:'-',cost_usd:0,cost_pct:0};
const topP = DATA.by_project[0] || {project:'-',cost_usd:0,cost_pct:0};
const cards = [
  {k:'当日模型总花销', v:usd(DATA.total_cost_usd), big:true},
  {k:'基础设施花销', v:usd(DATA.total_infra_cost_usd)},
  {k:'总调用次数', v:DATA.total_calls.toLocaleString()},
  {k:'总 token', v:htok(DATA.total_tokens)},
  {k:'活跃模型 / 项目', v:DATA.active_models + ' / ' + DATA.active_projects},
  {k:'活跃调用方 IP', v:DATA.active_ips},
  {k:'花销最高模型', v:topM.model + ' (' + topM.cost_pct.toFixed(1) + '%)'},
  {k:'花销最高项目', v:topP.project + ' (' + topP.cost_pct.toFixed(1) + '%)'},
];
document.getElementById('cards').innerHTML = cards.map(c =>
  '<div class="card"><div class="k">'+c.k+'</div><div class="v'+(c.big?' big':'')+'">'+c.v+'</div></div>').join('');

function mk(id, opt){ const el=document.getElementById(id); const ch=echarts.init(el,'dark');
  opt.backgroundColor='transparent'; opt.color=PALETTE; ch.setOption(opt);
  window.addEventListener('resize',()=>ch.resize()); }

const models = DATA.by_model;
const projs = DATA.by_project;

mk('c_cost_model', {
  tooltip:{trigger:'item', formatter:p=>p.name+'<br/>'+usd(p.value)+' ('+p.percent+'%)'},
  legend:{type:'scroll', orient:'vertical', right:6, top:10, textStyle:{color:'#8a94a6'}},
  series:[{type:'pie', radius:['42%','70%'], center:['40%','52%'],
    label:{color:'#e6e9ef', formatter:'{b}\n{d}%'},
    data:models.filter(m=>m.cost_usd>0).map(m=>({name:m.model, value:+m.cost_usd.toFixed(4)}))}]
});

mk('c_cost_proj', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}, formatter:p=>p[0].name+'<br/>'+usd(p[0].value)},
  grid:baseGrid,
  xAxis:{type:'category', data:projs.map(p=>p.project), ...axisStyle, axisLabel:{color:'#8a94a6',rotate:30}},
  yAxis:{type:'value', ...axisStyle},
  series:[{type:'bar', data:projs.map(p=>+p.cost_usd.toFixed(4)), barMaxWidth:40,
    itemStyle:{borderRadius:[4,4,0,0]}, label:{show:true,position:'top',color:'#8a94a6',formatter:o=>usd(o.value)}}]
});

mk('c_calls_cost', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}},
  legend:{top:0, textStyle:{color:'#8a94a6'}},
  grid:{left:70,right:70,top:40,bottom:80},
  xAxis:{type:'category', data:models.map(m=>m.model), ...axisStyle, axisLabel:{color:'#8a94a6',rotate:30}},
  yAxis:[{type:'value', name:'次数', ...axisStyle},
         {type:'value', name:'花销$', position:'right', ...axisStyle, splitLine:{show:false}}],
  series:[
    {name:'调用次数', type:'bar', data:models.map(m=>m.calls), barMaxWidth:36, itemStyle:{borderRadius:[4,4,0,0]}},
    {name:'花销($)', type:'line', yAxisIndex:1, smooth:true, symbolSize:8,
     data:models.map(m=>+m.cost_usd.toFixed(4)), lineStyle:{width:3}}
  ]
});

mk('c_tokens', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}, formatter:p=>{
    let s=p[0].name+'<br/>'; p.forEach(x=>s+=x.marker+x.seriesName+': '+htok(x.value)+'<br/>'); return s;}},
  legend:{top:0, textStyle:{color:'#8a94a6'}},
  grid:baseGrid,
  xAxis:{type:'category', data:models.map(m=>m.model), ...axisStyle, axisLabel:{color:'#8a94a6',rotate:30}},
  yAxis:{type:'value', ...axisStyle, axisLabel:{color:'#8a94a6',formatter:v=>htok(v)}},
  series:[
    {name:'输入', type:'bar', stack:'t', data:models.map(m=>m.tok_input)},
    {name:'输出', type:'bar', stack:'t', data:models.map(m=>m.tok_output)},
    {name:'缓存', type:'bar', stack:'t', data:models.map(m=>m.tok_cached)}
  ]
});

const latM = models.filter(m=>m.avg_latency_ms>0).sort((a,b)=>b.avg_latency_ms-a.avg_latency_ms);
mk('c_latency', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}, formatter:p=>p[0].name+'<br/>'+p[0].value.toFixed(0)+' ms'},
  grid:{left:120,right:30,top:20,bottom:40},
  xAxis:{type:'value', ...axisStyle},
  yAxis:{type:'category', data:latM.map(m=>m.model).reverse(), ...axisStyle},
  series:[{type:'bar', data:latM.map(m=>+m.avg_latency_ms.toFixed(0)).reverse(), barMaxWidth:22,
    itemStyle:{borderRadius:[0,4,4,0]}, label:{show:true,position:'right',color:'#8a94a6'}}]
});

const stArr = Object.entries(DATA.status_calls||{}).sort((a,b)=>b[1]-a[1]);
mk('c_status', {
  tooltip:{trigger:'item', formatter:p=>p.name+'<br/>'+p.value.toLocaleString()+' ('+p.percent+'%)'},
  legend:{orient:'vertical', right:6, top:10, textStyle:{color:'#8a94a6'}},
  series:[{type:'pie', radius:['42%','70%'], center:['42%','52%'],
    label:{color:'#e6e9ef', formatter:'{b}: {c}'},
    data:stArr.map(([k,v])=>({name:k, value:v}))}]
});

const tsp = DATA.token_split;
mk('c_tokpie', {
  tooltip:{trigger:'item', formatter:p=>p.name+'<br/>'+htok(p.value)+' ('+p.percent+'%)'},
  legend:{orient:'vertical', right:6, top:10, textStyle:{color:'#8a94a6'}},
  series:[{type:'pie', radius:['42%','70%'], center:['42%','52%'],
    label:{color:'#e6e9ef', formatter:'{b}\n{d}%'},
    data:[{name:'输入',value:tsp.input},{name:'输出',value:tsp.output},{name:'缓存',value:tsp.cached_input}]}]
});

const ips = DATA.by_ip || [];
const ipTop = ips.slice(0, 15);
mk('c_ip', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}, formatter:p=>{
    const d=ipTop[p[0].dataIndex];
    return d.ip+'<br/>花销 '+usd(d.cost_usd)+' ('+d.cost_pct.toFixed(1)+'%)'
      +'<br/>请求 '+d.calls.toLocaleString()+' ('+d.calls_pct.toFixed(1)+'%)'
      +'<br/>平均延迟 '+d.avg_latency_ms.toFixed(0)+'ms<br/>错误 '+d.err_calls
      +'<br/>项目: '+d.top_projects;}},
  legend:{top:0, textStyle:{color:'#8a94a6'}},
  grid:{left:130,right:70,top:40,bottom:40},
  xAxis:[{type:'value', name:'花销$', ...axisStyle},
         {type:'value', name:'次数', position:'top', ...axisStyle, splitLine:{show:false}}],
  yAxis:{type:'category', data:ipTop.map(d=>d.ip).reverse(), ...axisStyle},
  series:[
    {name:'花销($)', type:'bar', data:ipTop.map(d=>+d.cost_usd.toFixed(2)).reverse(), barMaxWidth:16,
     itemStyle:{borderRadius:[0,4,4,0]}, label:{show:true,position:'right',color:'#8a94a6',formatter:o=>usd(o.value)}},
    {name:'请求次数', type:'bar', xAxisIndex:1, data:ipTop.map(d=>d.calls).reverse(), barMaxWidth:16,
     itemStyle:{borderRadius:[0,4,4,0]}}
  ]
});

const ipModels = [...new Set(ipTop.flatMap(d=>Object.keys(d.cost_by_model||{})))];
mk('c_ip_model', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}, formatter:p=>{
    let s=p[0].name+'<br/>'; p.forEach(x=>{ if(x.value>0) s+=x.marker+x.seriesName+': '+usd(x.value)+'<br/>'; }); return s;}},
  legend:{type:'scroll', top:0, textStyle:{color:'#8a94a6'}},
  grid:{left:130,right:40,top:40,bottom:40},
  xAxis:{type:'value', ...axisStyle, axisLabel:{color:'#8a94a6',formatter:v=>'$'+v}},
  yAxis:{type:'category', data:ipTop.map(d=>d.ip).reverse(), ...axisStyle},
  series:ipModels.map(m=>({name:m, type:'bar', stack:'ipm',
    data:ipTop.map(d=>+((d.cost_by_model||{})[m]||0).toFixed(2)).reverse()}))
});

const mck = DATA.model_cost_by_kind || {};
mk('c_cost_kind', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}, formatter:p=>{
    let s=p[0].name+'<br/>'; p.forEach(x=>s+=x.marker+x.seriesName+': '+usd(x.value)+'<br/>'); return s;}},
  legend:{top:0, textStyle:{color:'#8a94a6'}},
  grid:baseGrid,
  xAxis:{type:'category', data:models.map(m=>m.model), ...axisStyle, axisLabel:{color:'#8a94a6',rotate:30}},
  yAxis:{type:'value', ...axisStyle, axisLabel:{color:'#8a94a6',formatter:v=>'$'+v}},
  series:[
    {name:'输入', type:'bar', stack:'c', data:models.map(m=>+((mck[m.model]||{}).input||0).toFixed(2))},
    {name:'输出', type:'bar', stack:'c', data:models.map(m=>+((mck[m.model]||{}).output||0).toFixed(2))},
    {name:'缓存', type:'bar', stack:'c', data:models.map(m=>+((mck[m.model]||{}).cached_input||0).toFixed(2))}
  ]
});

const regs = Object.entries(DATA.region_cost||{}).filter(e=>e[1]>0);
mk('c_region', {
  tooltip:{trigger:'item', formatter:p=>p.name+'<br/>'+usd(p.value)+' ('+p.percent+'%)'},
  legend:{type:'scroll', orient:'vertical', right:6, top:10, textStyle:{color:'#8a94a6'}},
  series:[{type:'pie', radius:['42%','70%'], center:['40%','52%'],
    label:{color:'#e6e9ef', formatter:'{b}\n{d}%'},
    data:regs.map(([k,v])=>({name:k, value:+v.toFixed(4)}))}]
});

const lp = DATA.latency_pct||{};
mk('c_latpct', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}, formatter:p=>p[0].name+'<br/>'+p[0].value.toLocaleString()+' ms'},
  grid:baseGrid,
  xAxis:{type:'category', data:['平均','p50','p90','p95','p99','最大'], ...axisStyle},
  yAxis:{type:'value', ...axisStyle, axisLabel:{color:'#8a94a6',formatter:v=>v>=1000?(v/1000)+'s':v+'ms'}},
  series:[{type:'bar', barMaxWidth:44, itemStyle:{borderRadius:[4,4,0,0]},
    data:[lp.avg,lp.p50,lp.p90,lp.p95,lp.p99,lp.max].map(v=>Math.round(v||0)),
    label:{show:true,position:'top',color:'#8a94a6',formatter:o=>o.value>=1000?(o.value/1000).toFixed(1)+'s':o.value+'ms'}}]
});

const hc = DATA.hour_calls||{}, he = DATA.hour_errors||{};
const hours = Array.from({length:24},(_,i)=>String(i).padStart(2,'0'));
mk('c_hour', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}},
  legend:{top:0, textStyle:{color:'#8a94a6'}},
  grid:baseGrid,
  xAxis:{type:'category', data:hours.map(h=>h+':00'), ...axisStyle, axisLabel:{color:'#8a94a6',rotate:45,fontSize:10}},
  yAxis:{type:'value', ...axisStyle},
  series:[
    {name:'请求数', type:'bar', data:hours.map(h=>hc[h]||0), barMaxWidth:22, itemStyle:{borderRadius:[3,3,0,0]}},
    {name:'错误数', type:'line', smooth:true, data:hours.map(h=>he[h]||0)}
  ]
});

function table(el, head, rows, foot){
  let h='<thead><tr>'+head.map(x=>'<th>'+x+'</th>').join('')+'</tr></thead>';
  h+='<tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody>';
  if(foot) h+='<tfoot><tr>'+foot.map(c=>'<td>'+c+'</td>').join('')+'</tr></tfoot>';
  document.getElementById(el).innerHTML=h;
}
table('t_model',
  ['#','模型','花销','占比','调用次数','输入tok','输出tok','缓存tok','$/次','$/1k tok','平均延迟ms'],
  models.map((m,i)=>[i+1,m.model,usd(m.cost_usd),m.cost_pct.toFixed(2)+'%',m.calls.toLocaleString(),
    htok(m.tok_input),htok(m.tok_output),htok(m.tok_cached),m.unit_cost.toFixed(4),
    m.cost_per_1k_tok.toFixed(4),m.avg_latency_ms.toFixed(0)]),
  ['','合计',usd(DATA.total_cost_usd),'100%',models.reduce((s,m)=>s+m.calls,0).toLocaleString(),'','','','','','']
);
table('t_proj',
  ['#','项目','花销','占比','调用次数','token','Top 模型(花销)'],
  projs.map((p,i)=>[i+1,p.project,usd(p.cost_usd),p.cost_pct.toFixed(2)+'%',p.calls.toLocaleString(),
    htok(p.tok_total),p.top_models||'-'])
);
table('t_ip',
  ['#','调用方 IP','总花销','花销占比','请求次数','次数占比','平均延迟ms','错误','按模型花销明细'],
  ips.map((d,i)=>[i+1,d.ip,usd(d.cost_usd),d.cost_pct.toFixed(2)+'%',d.calls.toLocaleString(),
    d.calls_pct.toFixed(2)+'%',d.avg_latency_ms.toFixed(0),d.err_calls,
    Object.entries(d.cost_by_model||{}).sort((a,b)=>b[1]-a[1]).map(e=>e[0]+' '+usd(e[1])).join(', ')||'-'])
);

const sc = DATA.status_calls||{}, ebm=DATA.error_by_model||{}, ebi=DATA.error_by_ip||{}, ebh=DATA.error_by_hour||{};
const errCodes = Object.keys(sc).filter(s=>!s.startsWith('2')).sort((a,b)=>sc[b]-sc[a]);
const top2 = (o,fmt) => Object.entries(o||{}).sort((a,b)=>b[1]-a[1]).slice(0,2).map(fmt).join(', ')||'-';
if(errCodes.length){
  table('t_err',
    ['错误码','次数','主要模型','主要来源 IP','高发时段'],
    errCodes.map(s=>[s, sc[s],
      top2(ebm[s], e=>e[0]+'('+e[1]+')'),
      top2(ebi[s], e=>e[0]+'('+e[1]+')'),
      top2(ebh[s], e=>e[0]+':00('+e[1]+')')])
  );
} else {
  document.getElementById('t_err').innerHTML='<tbody><tr><td style="text-align:left">当日无错误(非2xx)。</td></tr></tbody>';
}

const cons = DATA.consistency;
let notes = '<ul><li>指标次数(JSON) <b>'+cons.calls_json.toLocaleString()+'</b> vs 日志次数(NDJSON) <b>'
  +cons.calls_ndjson.toLocaleString()+'</b> (差 '+(cons.calls_diff>=0?'+':'')+cons.calls_diff+')</li>';
if(cons.notes && cons.notes.length){ cons.notes.forEach(n=>notes+='<li class="warn">⚠ '+n+'</li>'); }
else { notes+='<li class="ok">✅ 未发现明显异常。</li>'; }
notes+='</ul>';
document.getElementById('notes').innerHTML=notes;
</script>
</body>
</html>
"""


def to_html(rep: Report) -> bytes:
    """渲染自包含单文件 HTML：内嵌 ECharts(CDN) + 数据 JSON。"""
    data_json = json.dumps(rep, ensure_ascii=False)
    # 避免注入的数据里出现 </script> 提前闭合脚本标签。
    data_json = data_json.replace("</", "<\\/")
    html = (
        _HTML_TEMPLATE
        .replace("__DATE__", rep["date"])
        .replace("__ECHARTS_CDN__", _ECHARTS_CDN)
        .replace("__DATA_JSON__", data_json)
    )
    return html.encode("utf-8")


# --------------------------------------------------------------------------- #
# 编排 + CLI
# --------------------------------------------------------------------------- #


def _infer_date(args_date: str | None, *paths: str | None) -> str:
    """确定报告日期：优先 --date，否则从文件名里的 YYYY-MM-DD 猜，最后回退今天。"""
    if args_date:
        return args_date
    pat = re.compile(r"(\d{4}-\d{2}-\d{2})")
    for p in paths:
        if p:
            m = pat.search(os.path.basename(p))
            if m:
                return m.group(1)
    return dt.datetime.now(CST).strftime("%Y-%m-%d")


def generate(
    date: str,
    csv_path: str | None,
    calls_path: str | None,
    requests_path: str | None,
) -> tuple[Report, bytes, bytes]:
    """解析→聚合→渲染。缺失的数据源静默降级(对应小节标注缺失)，返回 (report, md, html)。"""
    rp_csv = _resolve(csv_path)
    rp_calls = _resolve(calls_path)
    rp_req = _resolve(requests_path)
    csv_data = parse_csv(rp_csv) if rp_csv else None
    calls_data = parse_calls(rp_calls) if rp_calls else None
    req_data = parse_requests(rp_req) if rp_req else None
    rep = build_report(date, csv_data, calls_data, req_data)
    return rep, to_markdown(rep), to_html(rep)


def _resolve(path: str | None) -> str | None:
    """判断路径是否可用；返回可用路径或 None。

    Windows 下命令行传中文路径时，shell 可能以本地代码页(GBK/ANSI)而非 UTF-8
    交给 Python，导致 os.path.exists 误判为不存在。这里在直判失败时，尝试用
    文件系统编码往返重解码兜底一次，避免误吞中文路径的数据源。
    """
    if not path:
        return None
    if os.path.exists(path):
        return path
    try:
        alt = path.encode("mbcs").decode("utf-8")  # mbcs 仅 Windows 可用
        if os.path.exists(alt):
            return alt
    except (UnicodeError, LookupError):
        pass
    return None


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="离线聚合 Azure 每日模型消耗报告(CSV账单 + 次数JSON + 请求NDJSON → MD + ECharts HTML)。",
    )
    p.add_argument("--csv", help="账单 CSV(含 costInUsd/quantity/tags)，唯一金额来源。")
    p.add_argument("--calls", help="calls-*.json(Monitor 指标)，权威调用次数。")
    p.add_argument("--requests", help="requests-*.ndjson(诊断日志)，延迟/字节/状态码。")
    p.add_argument("--out-dir", default=".", help="输出目录(默认当前目录)。")
    p.add_argument("--date", help="报告日期 YYYY-MM-DD(默认从文件名推断，再回退今天)。")
    p.add_argument("--json", action="store_true", help="额外输出结构化 JSON。")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        import sys as _sys
        _sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = _build_arg_parser().parse_args(argv)
    date = _infer_date(args.date, args.csv, args.calls, args.requests)

    if not any((args.csv, args.calls, args.requests)):
        print("错误：至少提供 --csv / --calls / --requests 之一。")
        return 2

    rep, md_bytes, html_bytes = generate(date, args.csv, args.calls, args.requests)

    os.makedirs(args.out_dir, exist_ok=True)
    base = os.path.join(args.out_dir, f"usage-report-{date}")
    md_path = f"{base}.md"
    html_path = f"{base}.html"
    with open(md_path, "wb") as f:
        f.write(md_bytes)
    with open(html_path, "wb") as f:
        f.write(html_bytes)
    outputs = [md_path, html_path]

    if args.json:
        json_path = f"{base}.json"
        with open(json_path, "wb") as f:
            f.write(json.dumps(rep, ensure_ascii=False, indent=2).encode("utf-8"))
        outputs.append(json_path)

    print(f"日期: {date}")
    print(f"模型总花销: ${rep['total_cost_usd']:,.2f}  基础设施: ${rep['total_infra_cost_usd']:,.4f}")
    print(f"总调用次数: {rep['total_calls']:,}  总token: {rep['total_tokens']:,}")
    print(f"数据源: csv={rep['has_csv']} calls={rep['has_calls']} requests={rep['has_requests']}")
    for o in outputs:
        print(f"  -> {o}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
