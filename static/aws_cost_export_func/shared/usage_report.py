# -*- coding: utf-8 -*-
"""纯离线聚合器：把当天三类 AWS 数据源合并成"钱+量+调用者审计"三合一每日报告。

数据源(各司其职、互不重复计数)：
- CE JSON(cost_fetch)   —— 唯一带【金额】。真实用量成本(credit 前) / 实付(credit 后) /
                           credit 抵扣三口径 + 模型级金额。避免被 credit 显示为 0 误导。
- Logs JSON(logs_fetch) —— 权威【调用次数 + 真实 token】。按 identity.arn × modelId 聚合
                           四类 token(input/cacheRead/cacheWrite/output)。
- Trail JSON(trail_fetch) —— 补【调用者 IP + 活跃时段】。IP 为 Cursor 中转 IP,如实标注。

维度约定：
- 金额来自 CE(模型级)；成本按调用者分摊时，用 Logs 的调用次数占比把模型成本摊到 identity.arn。
- 调用次数/ token 以 Logs 为准；CloudTrail 事件数仅作交叉校验(口径略有差异)。
- "谁"以 identity.arn 为准(cursor-bedrock-user vs intern-bedrock)；IP 仅作来源网段参考。

独立运行：
    python -m shared.usage_report --cost <ce.json> --logs <logs_usage.json> \
        --trail <trail.json> --out-dir <dir> --date 2026-08-28
- 缺哪份，对应小节标注"数据缺失"而非报错。仅依赖标准库；HTML 内嵌 ECharts(CDN)。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from collections import defaultdict
from typing import Any, Optional, TypedDict

CST = dt.timezone(dt.timedelta(hours=8))


def _short_caller(arn: str) -> str:
    """把 identity.arn 截成短名：arn:aws:iam::502225588666:user/cursor-bedrock-user → cursor-bedrock-user。"""
    if not arn:
        return "(unknown)"
    if "/" in arn:
        return arn.rsplit("/", 1)[-1]
    return arn


# --------------------------------------------------------------------------- #
# 解析三类 JSON
# --------------------------------------------------------------------------- #

class ParsedCost(TypedDict):
    model_cost_real: dict[str, float]     # 模型级真实用量成本(credit 前)
    model_cost_paid: dict[str, float]     # 模型级实付(credit 后)
    record_type: dict[str, float]         # Usage/Credit/Tax...
    total_real: float
    total_paid: float
    total_credit: float
    granularity: str
    window: dict[str, str]


def parse_cost(path: str) -> ParsedCost:
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return ParsedCost(
        model_cost_real=d.get("model_cost_amortized", {}),
        model_cost_paid=d.get("model_cost_unblended", {}),
        record_type=d.get("record_type_amortized", {}),
        total_real=d.get("total_amortized", 0.0),
        total_paid=d.get("total_unblended", 0.0),
        total_credit=d.get("total_credit", 0.0),
        granularity=d.get("granularity", "DAILY"),
        window=d.get("window_utc", {}),
    )


class CallerModelToken(TypedDict):
    caller: str
    model: str
    invocations: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int


class ParsedLogs(TypedDict):
    stats: list[CallerModelToken]         # 按 caller×model
    totals_by_caller: dict[str, dict[str, int]]
    totals_by_model: dict[str, dict[str, int]]
    total_invocations: int
    log_group: str
    regions: list[str]
    detail_ndjson_path: Optional[str]


def parse_logs(path: str) -> ParsedLogs:
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return ParsedLogs(
        stats=d.get("caller_model_stats", []),
        totals_by_caller=d.get("totals_by_caller", {}),
        totals_by_model=d.get("totals_by_model", {}),
        total_invocations=d.get("total_invocations", 0),
        log_group=d.get("log_group", ""),
        regions=d.get("regions", []),
        detail_ndjson_path=d.get("detail_ndjson_path"),
    )


class CallerTrail(TypedDict):
    caller: str
    event_count: int
    source_ips: list[str]
    user_agents: list[str]
    first_seen_cst: Optional[str]
    last_seen_cst: Optional[str]
    hourly_cst: dict[str, int]


class ParsedTrail(TypedDict):
    caller_stats: list[CallerTrail]
    total_events: int
    ip_note: str
    regions: list[str]


def parse_trail(path: str) -> ParsedTrail:
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return ParsedTrail(
        caller_stats=d.get("caller_stats", []),
        total_events=d.get("total_events", 0),
        ip_note=d.get("ip_note", ""),
        regions=d.get("regions", []),
    )


# --------------------------------------------------------------------------- #
# 聚合成报告
# --------------------------------------------------------------------------- #

class ModelRow(TypedDict):
    model: str
    cost_real: float
    cost_paid: float
    cost_pct: float
    invocations: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    total_tokens: int
    unit_cost: float            # $/次(真实成本口径)
    cost_per_1k_tok: float


class CallerRow(TypedDict):
    caller: str                 # 短名
    caller_arn: str
    cost_real: float            # 按调用次数占比分摊的真实成本
    cost_pct: float
    invocations: int
    invocations_pct: float
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    total_tokens: int
    top_models: str
    source_ips: list[str]       # CloudTrail IP(Cursor 中转)
    ip_count: int
    trail_events: int
    active_window_cst: str      # 首末活跃时段
    hourly_cst: dict[str, int]


class Consistency(TypedDict):
    logs_invocations: int
    trail_events: int
    diff: int
    notes: list[str]


class Report(TypedDict):
    date: str
    generated_at: str
    has_cost: bool
    has_logs: bool
    has_trail: bool
    # 成本三口径
    total_cost_real: float
    total_cost_paid: float
    total_credit: float
    cost_granularity: str
    cost_window: dict[str, str]
    record_type: dict[str, float]
    # 用量
    total_invocations: int
    total_tokens: int
    token_split: dict[str, int]   # input/cache_read/cache_write/output
    active_models: int
    active_callers: int
    by_model: list[ModelRow]
    by_caller: list[CallerRow]
    consistency: Consistency
    ip_note: str
    log_group: str
    regions: list[str]


def _sum_tokens(t: dict[str, int]) -> int:
    return (t.get("input_tokens", 0) + t.get("cache_read_tokens", 0)
            + t.get("cache_write_tokens", 0) + t.get("output_tokens", 0))


def build_report(
    date: str,
    cost: Optional[ParsedCost],
    logs: Optional[ParsedLogs],
    trail: Optional[ParsedTrail],
) -> Report:
    cost = cost or ParsedCost(
        model_cost_real={}, model_cost_paid={}, record_type={}, total_real=0.0,
        total_paid=0.0, total_credit=0.0, granularity="DAILY", window={},
    )
    logs = logs or ParsedLogs(
        stats=[], totals_by_caller={}, totals_by_model={}, total_invocations=0,
        log_group="", regions=[], detail_ndjson_path=None,
    )
    trail = trail or ParsedTrail(caller_stats=[], total_events=0, ip_note="", regions=[])

    total_real = cost["total_real"]

    # ---- 按模型 ----
    # 模型名对齐：CE 用 "Claude Opus 4.8 (Amazon Bedrock Edition)"，Logs 用
    # "us.anthropic.claude-opus-4-8"。两者无法字符串直接对齐，故：
    # 金额按 CE 模型名展示；token/次数按 Logs 模型名展示；用"总量占比"把 CE 金额
    # 分摊到 Logs 模型(当只有一个计费模型时即 1:1)。这里以 Logs 模型为主键，
    # 金额取 CE 总真实成本按该模型调用次数占比分摊。
    logs_models = logs["totals_by_model"]
    total_inv = logs["total_invocations"] or 1
    by_model: list[ModelRow] = []
    for model, tk in logs_models.items():
        inv = tk.get("invocations", 0)
        # 按调用次数占比把 CE 真实/实付总额分摊到该 Logs 模型。
        cost_real = total_real * (inv / total_inv) if total_inv else 0.0
        cost_paid = cost["total_paid"] * (inv / total_inv) if total_inv else 0.0
        tot_tok = _sum_tokens(tk)
        by_model.append(ModelRow(
            model=model,
            cost_real=cost_real,
            cost_paid=cost_paid,
            cost_pct=(cost_real / total_real * 100) if total_real else 0.0,
            invocations=inv,
            input_tokens=tk.get("input_tokens", 0),
            cache_read_tokens=tk.get("cache_read_tokens", 0),
            cache_write_tokens=tk.get("cache_write_tokens", 0),
            output_tokens=tk.get("output_tokens", 0),
            total_tokens=tot_tok,
            unit_cost=(cost_real / inv) if inv else 0.0,
            cost_per_1k_tok=(cost_real / tot_tok * 1000) if tot_tok else 0.0,
        ))
    by_model.sort(key=lambda x: (-x["invocations"], -x["cost_real"]))

    # 若 CE 有模型金额但 Logs 无对应(极端),把 CE 模型金额作为无 token 行补入。
    if not by_model and cost["model_cost_real"]:
        for m, c in cost["model_cost_real"].items():
            if c <= 0:
                continue
            by_model.append(ModelRow(
                model=m, cost_real=c, cost_paid=cost["model_cost_paid"].get(m, 0.0),
                cost_pct=(c / total_real * 100) if total_real else 0.0,
                invocations=0, input_tokens=0, cache_read_tokens=0,
                cache_write_tokens=0, output_tokens=0, total_tokens=0,
                unit_cost=0.0, cost_per_1k_tok=0.0,
            ))

    # ---- 按调用者 ----
    trail_by_caller = {t["caller"]: t for t in trail["caller_stats"]}
    # caller top models(来自 logs.stats)
    caller_top_models: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for st in logs["stats"]:
        caller_top_models[st["caller"]].append((st["model"], st.get("invocations", 0)))

    # 调用者主键 = Logs ∪ CloudTrail 的并集。Logs 缺失(如日志组保留期外)时,
    # 仍能用 CloudTrail 的调用者 + IP + 活跃时段出"谁在用",不丢维度。
    all_callers = list(logs["totals_by_caller"].keys())
    for c in trail_by_caller:
        if c not in logs["totals_by_caller"]:
            all_callers.append(c)

    by_caller: list[CallerRow] = []
    for caller_arn in all_callers:
        tk = logs["totals_by_caller"].get(caller_arn, {})
        inv = tk.get("invocations", 0)
        cost_real = total_real * (inv / total_inv) if total_inv else 0.0
        tot_tok = _sum_tokens(tk)
        tr = trail_by_caller.get(caller_arn, {})
        ips = tr.get("source_ips", [])
        tops = sorted(caller_top_models.get(caller_arn, []), key=lambda x: -x[1])[:3]
        first = tr.get("first_seen_cst")
        last = tr.get("last_seen_cst")
        window = f"{first} ~ {last}" if first and last else "-"
        by_caller.append(CallerRow(
            caller=_short_caller(caller_arn),
            caller_arn=caller_arn,
            cost_real=cost_real,
            cost_pct=(cost_real / total_real * 100) if total_real else 0.0,
            invocations=inv,
            invocations_pct=(inv / total_inv * 100) if total_inv else 0.0,
            input_tokens=tk.get("input_tokens", 0),
            cache_read_tokens=tk.get("cache_read_tokens", 0),
            cache_write_tokens=tk.get("cache_write_tokens", 0),
            output_tokens=tk.get("output_tokens", 0),
            total_tokens=tot_tok,
            top_models=", ".join(f"{_short_model(m)}({n})" for m, n in tops),
            source_ips=ips,
            ip_count=len(ips),
            trail_events=tr.get("event_count", 0),
            active_window_cst=window,
            hourly_cst=tr.get("hourly_cst", {}),
        ))
    by_caller.sort(key=lambda x: (-x["invocations"], -x["trail_events"], -x["cost_real"]))

    # ---- token 结构 ----
    token_split = {
        "input": sum(t.get("input_tokens", 0) for t in logs["totals_by_caller"].values()),
        "cache_read": sum(t.get("cache_read_tokens", 0) for t in logs["totals_by_caller"].values()),
        "cache_write": sum(t.get("cache_write_tokens", 0) for t in logs["totals_by_caller"].values()),
        "output": sum(t.get("output_tokens", 0) for t in logs["totals_by_caller"].values()),
    }
    total_tokens = sum(token_split.values())

    # ---- 一致性校验 ----
    notes: list[str] = []
    diff = logs["total_invocations"] - trail["total_events"]
    if logs["total_invocations"] and trail["total_events"]:
        base = max(logs["total_invocations"], trail["total_events"])
        if abs(diff) > max(5, base * 0.5):
            notes.append(
                f"Logs 调用数({logs['total_invocations']:,})与 CloudTrail 事件数"
                f"({trail['total_events']:,})相差较大。CloudTrail 记录每次 API 事件"
                f"(含流式分片),Logs 记录每次模型 invocation,口径本就不同,仅供参考。"
            )
    if cost["total_paid"] == 0 and cost["total_real"] > 0:
        notes.append(
            f"实付 $0 但真实用量成本 ${cost['total_real']:,.2f}——被 AWS Credit 全额抵扣"
            f"(credit ${cost['total_credit']:,.2f})。真实消耗以真实成本口径为准。"
        )

    consistency = Consistency(
        logs_invocations=logs["total_invocations"],
        trail_events=trail["total_events"],
        diff=diff,
        notes=notes,
    )

    return Report(
        date=date,
        generated_at=dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S+08:00"),
        has_cost=bool(cost["model_cost_real"] or cost["record_type"]),
        has_logs=bool(logs["stats"]),
        has_trail=bool(trail["caller_stats"]),
        total_cost_real=cost["total_real"],
        total_cost_paid=cost["total_paid"],
        total_credit=cost["total_credit"],
        cost_granularity=cost["granularity"],
        cost_window=cost["window"],
        record_type=cost["record_type"],
        total_invocations=logs["total_invocations"],
        total_tokens=total_tokens,
        token_split=token_split,
        active_models=len([m for m in by_model if m["invocations"] > 0 or m["cost_real"] > 0]),
        active_callers=len(by_caller),
        by_model=by_model,
        by_caller=by_caller,
        consistency=consistency,
        ip_note=trail["ip_note"],
        log_group=logs["log_group"],
        regions=logs["regions"] or trail["regions"],
    )


def _short_model(m: str) -> str:
    """把模型名截短显示。

    - Logs 侧的 modelId(如 us.anthropic.claude-opus-4-8)取最后一段。
    - CE 侧的服务名(如 "Claude Opus 4.8 (Amazon Bedrock Edition)")含空格/括号/小数点,
      不能按 '.' 切(会把 4.8 切坏),原样返回。
    """
    if not m:
        return "(unknown)"
    if " " in m or "(" in m:
        return m
    return m.rsplit(".", 1)[-1] if "." in m else m


# --------------------------------------------------------------------------- #
# 格式化助手
# --------------------------------------------------------------------------- #

def _htok(n: int) -> str:
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


# --------------------------------------------------------------------------- #
# Markdown 渲染
# --------------------------------------------------------------------------- #

def to_markdown(rep: Report) -> bytes:
    L: list[str] = []
    L.append(f"# AWS Bedrock 每日用量报告 · {rep['date']}")
    L.append("")
    L.append(f"> 生成时间：{rep['generated_at']}　|　货币：USD　|　子账号用量")
    src = []
    src.append("成本CE✅" if rep["has_cost"] else "成本CE❌缺失")
    src.append("用量Logs✅" if rep["has_logs"] else "用量Logs❌缺失")
    src.append("审计CloudTrail✅" if rep["has_trail"] else "审计CloudTrail❌缺失")
    L.append(f"> 数据源：{'　'.join(src)}")
    if rep["regions"]:
        L.append(f"> region：{', '.join(rep['regions'])}　|　日志组：{rep['log_group']}")
    L.append("")

    # ---- 总览：成本三口径 ----
    L.append("## 总览 · 成本三口径(避免被 Credit 误导)")
    L.append("")
    L.append("| 指标 | 数值 | 说明 |")
    L.append("|---|---:|---|")
    L.append(f"| **真实用量成本** | **{_usd(rep['total_cost_real'])}** | 按标准价计的实际消耗(Credit 抵扣前) |")
    L.append(f"| 实付金额 | {_usd(rep['total_cost_paid'])} | 现金口径(Credit 抵扣后，可能为 0) |")
    L.append(f"| Credit 抵扣 | {_usd(rep['total_credit'])} | 促销/赠送额度抵扣(负数) |")
    gran = rep["cost_granularity"]
    win = rep["cost_window"]
    L.append(f"| 成本粒度/窗口 | {gran} | {win.get('basis', '')} |")
    L.append("")
    L.append("| 用量指标 | 数值 |")
    L.append("|---|---:|")
    L.append(f"| 总调用次数(Logs) | {rep['total_invocations']:,} |")
    L.append(f"| 总 token(输入+缓存读+缓存写+输出) | {_htok(rep['total_tokens'])} ({rep['total_tokens']:,}) |")
    L.append(f"| 活跃模型数 | {rep['active_models']} |")
    L.append(f"| 活跃调用者数 | {rep['active_callers']} |")
    if rep["by_caller"]:
        tc = rep["by_caller"][0]
        L.append(f"| 调用最多者 | {tc['caller']}（{tc['invocations']:,} 次，{tc['invocations_pct']:.1f}%） |")
    L.append("")

    # ---- 记录类型拆分 ----
    if rep["record_type"]:
        L.append("## 成本记录类型拆分(CE RECORD_TYPE)")
        L.append("")
        L.append("| 类型 | 金额 |")
        L.append("|---|---:|")
        for k, v in sorted(rep["record_type"].items(), key=lambda x: -abs(x[1])):
            L.append(f"| {k} | {_usd(v)} |")
        L.append("")

    # ---- 按模型 ----
    L.append("## 按模型 · 成本 / 次数 / token")
    L.append("")
    L.append("| # | 模型 | 真实成本 | 占比 | 调用次数 | 输入tok | 缓存读tok | 缓存写tok | 输出tok | $/次 |")
    L.append("|--:|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for i, m in enumerate(rep["by_model"], 1):
        L.append(
            f"| {i} | {_short_model(m['model'])} | {_usd(m['cost_real'])} | {m['cost_pct']:.1f}% | "
            f"{m['invocations']:,} | {_htok(m['input_tokens'])} | {_htok(m['cache_read_tokens'])} | "
            f"{_htok(m['cache_write_tokens'])} | {_htok(m['output_tokens'])} | {m['unit_cost']:.4f} |"
        )
    L.append("")

    # ---- 按调用者(核心：区分 cursor-bedrock-user vs intern-bedrock) ----
    L.append("## 按调用者 · 谁在用(以 IAM identity.arn 区分)")
    L.append("")
    if rep["ip_note"]:
        L.append(f"> ⚠ {rep['ip_note']}")
        L.append("")
    L.append("| # | 调用者 | 真实成本 | 成本占比 | 调用次数 | 次数占比 | 总token | 活跃时段(北京) | 来源IP数 |")
    L.append("|--:|---|--:|--:|--:|--:|--:|---|--:|")
    for i, c in enumerate(rep["by_caller"], 1):
        L.append(
            f"| {i} | {c['caller']} | {_usd(c['cost_real'])} | {c['cost_pct']:.1f}% | "
            f"{c['invocations']:,} | {c['invocations_pct']:.1f}% | {_htok(c['total_tokens'])} | "
            f"{c['active_window_cst']} | {c['ip_count']} |"
        )
    L.append("")
    L.append("### 各调用者 token 结构 + 来源 IP(Cursor 中转)")
    L.append("")
    L.append("| 调用者 | 输入 | 缓存读 | 缓存写 | 输出 | 主要模型 | 来源IP(前5) |")
    L.append("|---|--:|--:|--:|--:|---|---|")
    for c in rep["by_caller"]:
        ips = ", ".join(c["source_ips"][:5]) + (" …" if c["ip_count"] > 5 else "")
        L.append(
            f"| {c['caller']} | {_htok(c['input_tokens'])} | {_htok(c['cache_read_tokens'])} | "
            f"{_htok(c['cache_write_tokens'])} | {_htok(c['output_tokens'])} | {c['top_models']} | {ips or '-'} |"
        )
    L.append("")

    # ---- token 结构 ----
    ts = rep["token_split"]
    tt = rep["total_tokens"] or 1
    L.append("## token 结构拆分")
    L.append("")
    L.append("> 绝大部分输入走 prompt cache(缓存读)，故必须四类分列，否则严重低估用量。")
    L.append("")
    L.append("| 类型 | token | 占比 |")
    L.append("|---|--:|--:|")
    for k, label in (("input", "输入(未缓存)"), ("cache_read", "缓存读"),
                     ("cache_write", "缓存写"), ("output", "输出")):
        v = ts.get(k, 0)
        L.append(f"| {label} | {_htok(v)} | {v / tt * 100:.1f}% |")
    L.append("")

    # ---- 一致性 ----
    L.append("## 数据一致性校验")
    L.append("")
    cons = rep["consistency"]
    L.append(f"- Logs 调用数：{cons['logs_invocations']:,}　vs　CloudTrail 事件数：{cons['trail_events']:,}"
             f"　(差 {cons['diff']:+,})")
    if cons["notes"]:
        for n in cons["notes"]:
            L.append(f"- ⚠ {n}")
    else:
        L.append("- ✅ 未发现明显异常。")
    L.append("")

    return ("\n".join(L) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# HTML 渲染(内嵌 ECharts CDN + 数据 JSON)
# --------------------------------------------------------------------------- #

_ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AWS Bedrock 每日用量报告 · __DATE__</title>
<script src="__ECHARTS_CDN__"></script>
<style>
  :root { --bg:#0f1419; --card:#1a2029; --line:#2a323d; --fg:#e6e9ef; --muted:#8a94a6;
          --accent:#ff9900; --good:#3fb950; --warn:#d29922; --bad:#f85149; }
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
  .notes { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 18px; font-size:13px; }
  .notes li { margin:4px 0; }
  .warn { color:var(--warn); }
  .ok { color:var(--good); }
  .missing { color:var(--bad); }
  .tag { display:inline-block; padding:2px 8px; border-radius:6px; background:#222b36; font-size:12px; margin-right:6px; }
  .ipnote { background:#2a2213; border:1px solid #d29922; border-radius:8px; padding:10px 14px; font-size:13px; color:#e3b341; margin-bottom:16px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>AWS Bedrock 每日用量报告 · <span id="hdate"></span></h1>
  <div class="sub" id="hsub"></div>
  <div class="cards" id="cards"></div>

  <div class="grid2">
    <div class="chart"><h3>成本三口径 (真实 / 实付 / Credit)</h3><div id="c_cost3" class="box"></div></div>
    <div class="chart"><h3>调用次数按调用者</h3><div id="c_caller_inv" class="box"></div></div>
  </div>
  <div class="grid2">
    <div class="chart"><h3>token 结构 (输入/缓存读/缓存写/输出)</h3><div id="c_tokpie" class="box"></div></div>
    <div class="chart"><h3>各调用者 token 结构(堆叠)</h3><div id="c_caller_tok" class="box"></div></div>
  </div>
  <div class="chart" style="margin-bottom:18px;">
    <h3>调用者活跃时段 (北京时间 · CloudTrail)</h3>
    <div id="c_hourly" class="box tall"></div>
  </div>

  <div class="ipnote" id="ipnote"></div>

  <h2>按模型明细</h2>
  <table id="t_model"></table>
  <h2>按调用者明细 · 谁在用</h2>
  <table id="t_caller"></table>
  <h2>各调用者 token 结构 + 来源 IP</h2>
  <table id="t_caller_ip"></table>
  <h2>数据一致性校验</h2>
  <div class="notes" id="notes"></div>
</div>

<script>
const DATA = __DATA_JSON__;
const usd = n => (Math.abs(n) >= 0.01 ? '$' + n.toFixed(2) : '$' + n.toFixed(4));
const htok = n => n >= 1e9 ? (n/1e9).toFixed(2)+'B' : n >= 1e6 ? (n/1e6).toFixed(2)+'M'
                : n >= 1e3 ? (n/1e3).toFixed(1)+'K' : String(n);
const shortModel = m => (m && m.indexOf('.')>=0) ? m.split('.').pop() : (m||'-');
const PALETTE = ['#ff9900','#4f9cf9','#3fb950','#d29922','#f85149','#a371f7','#39c5cf','#db61a2'];
const axisStyle = { axisLine:{lineStyle:{color:'#3a4450'}}, axisLabel:{color:'#8a94a6'}, splitLine:{lineStyle:{color:'#222b36'}} };

document.getElementById('hdate').textContent = DATA.date;
document.getElementById('hsub').innerHTML =
  '生成时间 ' + DATA.generated_at + '　|　货币 USD　|　'
  + (DATA.has_cost?'<span class="tag ok">成本CE</span>':'<span class="tag missing">成本CE缺失</span>')
  + (DATA.has_logs?'<span class="tag ok">用量Logs</span>':'<span class="tag missing">用量Logs缺失</span>')
  + (DATA.has_trail?'<span class="tag ok">审计Trail</span>':'<span class="tag missing">审计Trail缺失</span>');

document.getElementById('ipnote').textContent = '⚠ ' + (DATA.ip_note || '');

const topC = DATA.by_caller[0] || {caller:'-',invocations:0,invocations_pct:0};
const cards = [
  {k:'真实用量成本(Credit前)', v:usd(DATA.total_cost_real), big:true},
  {k:'实付(Credit后)', v:usd(DATA.total_cost_paid)},
  {k:'Credit 抵扣', v:usd(DATA.total_credit)},
  {k:'总调用次数', v:DATA.total_invocations.toLocaleString()},
  {k:'总 token', v:htok(DATA.total_tokens)},
  {k:'活跃模型 / 调用者', v:DATA.active_models + ' / ' + DATA.active_callers},
  {k:'调用最多者', v:topC.caller + ' (' + topC.invocations_pct.toFixed(1) + '%)'},
];
document.getElementById('cards').innerHTML = cards.map(c =>
  '<div class="card"><div class="k">'+c.k+'</div><div class="v'+(c.big?' big':'')+'">'+c.v+'</div></div>').join('');

function mk(id, opt){ const el=document.getElementById(id); const ch=echarts.init(el,'dark');
  opt.backgroundColor='transparent'; opt.color=PALETTE; ch.setOption(opt);
  window.addEventListener('resize',()=>ch.resize()); }

const callers = DATA.by_caller;

mk('c_cost3', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}, formatter:p=>p[0].name+'<br/>'+usd(p[0].value)},
  grid:{left:80,right:30,top:20,bottom:40},
  xAxis:{type:'category', data:['真实成本','实付','Credit抵扣'], ...axisStyle},
  yAxis:{type:'value', ...axisStyle, axisLabel:{color:'#8a94a6',formatter:v=>'$'+v}},
  series:[{type:'bar', barMaxWidth:60, itemStyle:{borderRadius:[4,4,0,0]},
    data:[+DATA.total_cost_real.toFixed(2), +DATA.total_cost_paid.toFixed(2), +DATA.total_credit.toFixed(2)],
    label:{show:true,position:'top',color:'#8a94a6',formatter:o=>usd(o.value)}}]
});

mk('c_caller_inv', {
  tooltip:{trigger:'item', formatter:p=>p.name+'<br/>'+p.value.toLocaleString()+' 次 ('+p.percent+'%)'},
  legend:{type:'scroll', orient:'vertical', right:6, top:10, textStyle:{color:'#8a94a6'}},
  series:[{type:'pie', radius:['42%','70%'], center:['40%','52%'],
    label:{color:'#e6e9ef', formatter:'{b}\n{d}%'},
    data:callers.map(c=>({name:c.caller, value:c.invocations}))}]
});

const tsp = DATA.token_split;
mk('c_tokpie', {
  tooltip:{trigger:'item', formatter:p=>p.name+'<br/>'+htok(p.value)+' ('+p.percent+'%)'},
  legend:{orient:'vertical', right:6, top:10, textStyle:{color:'#8a94a6'}},
  series:[{type:'pie', radius:['42%','70%'], center:['42%','52%'],
    label:{color:'#e6e9ef', formatter:'{b}\n{d}%'},
    data:[{name:'输入',value:tsp.input},{name:'缓存读',value:tsp.cache_read},
          {name:'缓存写',value:tsp.cache_write},{name:'输出',value:tsp.output}]}]
});

mk('c_caller_tok', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}, formatter:p=>{
    let s=p[0].name+'<br/>'; p.forEach(x=>s+=x.marker+x.seriesName+': '+htok(x.value)+'<br/>'); return s;}},
  legend:{top:0, textStyle:{color:'#8a94a6'}},
  grid:{left:70,right:30,top:30,bottom:60},
  xAxis:{type:'category', data:callers.map(c=>c.caller), ...axisStyle, axisLabel:{color:'#8a94a6',rotate:20}},
  yAxis:{type:'value', ...axisStyle, axisLabel:{color:'#8a94a6',formatter:v=>htok(v)}},
  series:[
    {name:'输入', type:'bar', stack:'t', data:callers.map(c=>c.input_tokens)},
    {name:'缓存读', type:'bar', stack:'t', data:callers.map(c=>c.cache_read_tokens)},
    {name:'缓存写', type:'bar', stack:'t', data:callers.map(c=>c.cache_write_tokens)},
    {name:'输出', type:'bar', stack:'t', data:callers.map(c=>c.output_tokens)}
  ]
});

const hours = Array.from({length:24},(_,i)=>String(i).padStart(2,'0'));
mk('c_hourly', {
  tooltip:{trigger:'axis', axisPointer:{type:'shadow'}},
  legend:{type:'scroll', top:0, textStyle:{color:'#8a94a6'}},
  grid:{left:70,right:30,top:40,bottom:60},
  xAxis:{type:'category', data:hours.map(h=>h+':00'), ...axisStyle, axisLabel:{color:'#8a94a6',rotate:45,fontSize:10}},
  yAxis:{type:'value', ...axisStyle},
  series:callers.map(c=>({name:c.caller, type:'bar', stack:'h',
    data:hours.map(h=>(c.hourly_cst||{})[h]||0)}))
});

function table(el, head, rows){
  let h='<thead><tr>'+head.map(x=>'<th>'+x+'</th>').join('')+'</tr></thead>';
  h+='<tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody>';
  document.getElementById(el).innerHTML=h;
}
table('t_model',
  ['#','模型','真实成本','占比','调用次数','输入tok','缓存读','缓存写','输出tok','$/次'],
  DATA.by_model.map((m,i)=>[i+1,shortModel(m.model),usd(m.cost_real),m.cost_pct.toFixed(1)+'%',
    m.invocations.toLocaleString(),htok(m.input_tokens),htok(m.cache_read_tokens),
    htok(m.cache_write_tokens),htok(m.output_tokens),m.unit_cost.toFixed(4)])
);
table('t_caller',
  ['#','调用者','真实成本','成本占比','调用次数','次数占比','总token','活跃时段(北京)','来源IP数','CloudTrail事件'],
  callers.map((c,i)=>[i+1,c.caller,usd(c.cost_real),c.cost_pct.toFixed(1)+'%',
    c.invocations.toLocaleString(),c.invocations_pct.toFixed(1)+'%',htok(c.total_tokens),
    c.active_window_cst,c.ip_count,c.trail_events.toLocaleString()])
);
table('t_caller_ip',
  ['调用者','输入','缓存读','缓存写','输出','主要模型','来源IP(前5, Cursor中转)'],
  callers.map(c=>[c.caller,htok(c.input_tokens),htok(c.cache_read_tokens),htok(c.cache_write_tokens),
    htok(c.output_tokens),c.top_models||'-',
    (c.source_ips||[]).slice(0,5).join(', ')+(c.ip_count>5?' …':'')||'-'])
);

const cons = DATA.consistency;
let notes = '<ul><li>Logs 调用数 <b>'+cons.logs_invocations.toLocaleString()+'</b> vs CloudTrail 事件数 <b>'
  +cons.trail_events.toLocaleString()+'</b> (差 '+(cons.diff>=0?'+':'')+cons.diff+')</li>';
if(cons.notes && cons.notes.length){ cons.notes.forEach(n=>notes+='<li class="warn">⚠ '+n+'</li>'); }
else { notes+='<li class="ok">✅ 未发现明显异常。</li>'; }
notes+='</ul>';
document.getElementById('notes').innerHTML=notes;
</script>
</body>
</html>
"""


def to_html(rep: Report) -> bytes:
    data_json = json.dumps(rep, ensure_ascii=False).replace("</", "<\\/")
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

def _resolve(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    if os.path.exists(path):
        return path
    try:
        alt = path.encode("mbcs").decode("utf-8")
        if os.path.exists(alt):
            return alt
    except (UnicodeError, LookupError):
        pass
    return None


def generate(
    date: str,
    cost_path: Optional[str],
    logs_path: Optional[str],
    trail_path: Optional[str],
) -> tuple[Report, bytes, bytes]:
    """解析→聚合→渲染。缺失数据源静默降级，返回 (report, md, html)。"""
    cp = _resolve(cost_path)
    lp = _resolve(logs_path)
    tp = _resolve(trail_path)
    cost = parse_cost(cp) if cp else None
    logs = parse_logs(lp) if lp else None
    trail = parse_trail(tp) if tp else None
    rep = build_report(date, cost, logs, trail)
    return rep, to_markdown(rep), to_html(rep)


def _infer_date(args_date: Optional[str], *paths: Optional[str]) -> str:
    if args_date:
        return args_date
    pat = re.compile(r"(\d{4}-\d{2}-\d{2})")
    for p in paths:
        if p:
            m = pat.search(os.path.basename(p))
            if m:
                return m.group(1)
    return dt.datetime.now(CST).strftime("%Y-%m-%d")


def main(argv: Optional[list[str]] = None) -> int:
    try:
        import sys as _sys
        _sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    p = argparse.ArgumentParser(
        description="离线聚合 AWS Bedrock 每日用量报告(CE成本 + Logs用量 + CloudTrail审计 → MD + ECharts HTML)。",
    )
    p.add_argument("--cost", help="CE 成本 JSON(cost_fetch 产物)。")
    p.add_argument("--logs", help="Logs 用量 JSON(logs_fetch 产物 logs_usage.json)。")
    p.add_argument("--trail", help="CloudTrail 审计 JSON(trail_fetch 产物 trail.json)。")
    p.add_argument("--out-dir", default=".")
    p.add_argument("--date")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    date = _infer_date(args.date, args.cost, args.logs, args.trail)
    if not any((args.cost, args.logs, args.trail)):
        print("错误：至少提供 --cost / --logs / --trail 之一。")
        return 2

    rep, md_bytes, html_bytes = generate(date, args.cost, args.logs, args.trail)

    os.makedirs(args.out_dir, exist_ok=True)
    base = os.path.join(args.out_dir, f"aws-usage-report-{date}")
    with open(f"{base}.md", "wb") as f:
        f.write(md_bytes)
    with open(f"{base}.html", "wb") as f:
        f.write(html_bytes)
    outputs = [f"{base}.md", f"{base}.html"]
    if args.json:
        with open(f"{base}.json", "wb") as f:
            f.write(json.dumps(rep, ensure_ascii=False, indent=2).encode("utf-8"))
        outputs.append(f"{base}.json")

    print(f"日期: {date}")
    print(f"真实成本: ${rep['total_cost_real']:,.2f}  实付: ${rep['total_cost_paid']:,.2f}  Credit: ${rep['total_credit']:,.2f}")
    print(f"总调用: {rep['total_invocations']:,}  总token: {rep['total_tokens']:,}")
    for o in outputs:
        print(f"  -> {o}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
