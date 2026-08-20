# -*- coding: utf-8 -*-
"""核心逻辑：查询 Azure Monitor 指标(ModelRequests) → 聚合 → 渲染 JSON/Markdown。

与 Functions 解耦，可独立本地运行：
    python -m shared.calls_report 2026-08-12          # 打印报告，不写 blob
    python shared/calls_report.py 2026-08-12

设计要点(严谨性)：
- 只用 ModelRequests 一个指标：实测它已覆盖 AzureOpenAI + Foundry 全部模型，
  与 AzureOpenAIRequests 对 gpt 系列数值一致，叠加会重复计数。
- 时间统一按北京时间(东八区)自然日切分：一份"北京 D 日"报告覆盖北京
  [00:00, 次日00:00)，对应 UTC [D-1 16:00Z, D 16:00Z)。指标底层仍按 UTC。
- 幂等：同一天多次运行结果一致，文件名带北京日期。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import defaultdict

# 北京时区(UTC+8)。Azure Monitor 指标底层按 UTC，但业务按北京自然日切分：
# 一份"北京 D 日"报告覆盖 [北京 D 00:00, 北京 D+1 00:00)，即 UTC [D-1 16:00Z, D 16:00Z)。
CST = dt.timezone(dt.timedelta(hours=8))

DEFAULT_ACCOUNTS = [
    "x-pilot",
    "x-pilot-2-resource", "x-pilot-3-resource", "x-pilot-4-resource",
    "x-pilot-5-resource", "x-pilot-6-resource", "x-pilot-7-resource",
    "x-pilot-8-resource", "x-pilot-10-practice-resource",
]
METRIC_NAME = "ModelRequests"
METRIC_NAMESPACE = "Microsoft.CognitiveServices/accounts"


def project_name(account: str) -> str:
    """把真实资源名映射为简洁的项目名(去掉 -resource 后缀)用于报告展示。"""
    return account[: -len("-resource")] if account.endswith("-resource") else account


def get_config():
    """从环境变量读取配置，缺失给出可用默认值。"""
    return {
        "subscription_id": os.environ.get(
            "COST_SUBSCRIPTION_ID", "a6dfdf96-3081-4996-bd76-7e07d8ea63b0"
        ),
        "resource_group": os.environ.get("COST_RESOURCE_GROUP", "x-pilot"),
        "accounts": [
            a.strip()
            for a in os.environ.get(
                "COST_COGNITIVE_ACCOUNTS", ",".join(DEFAULT_ACCOUNTS)
            ).split(",")
            if a.strip()
        ],
    }


def day_bounds_utc(date_str: str):
    """把"北京自然日" date_str 转成对应的 UTC ISO 查询区间 [start, end)。

    date_str 是北京日期，表示北京 [00:00, 次日00:00)；北京=UTC+8，
    对应 UTC 窗口 [date-1 16:00Z, date 16:00Z)。指标底层按 UTC 存储。
    """
    d_cst = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=CST)
    start = d_cst.astimezone(dt.timezone.utc)
    end = (d_cst + dt.timedelta(days=1)).astimezone(dt.timezone.utc)
    iso = lambda x: x.strftime("%Y-%m-%dT%H:%M:%SZ")
    return iso(start), iso(end)


def fetch_calls(credential, cfg: dict, date_str: str) -> dict:
    """查询每个账户的 ModelRequests，返回 {account: {model: calls}}。

    credential: 任意 azure-identity 凭据(本地 AzureCliCredential / 云上 ManagedIdentity)。
    """
    from azure.mgmt.monitor import MonitorManagementClient

    start, end = day_bounds_utc(date_str)
    client = MonitorManagementClient(credential, cfg["subscription_id"])
    out: dict[str, dict[str, int]] = {}

    for acc in cfg["accounts"]:
        resource_id = (
            f"/subscriptions/{cfg['subscription_id']}"
            f"/resourceGroups/{cfg['resource_group']}"
            f"/providers/{METRIC_NAMESPACE}/{acc}"
        )
        models: dict[str, int] = {}
        try:
            resp = client.metrics.list(
                resource_id,
                metricnames=METRIC_NAME,
                aggregation="Total",
                timespan=f"{start}/{end}",
                interval="P1D",
                filter="ModelDeploymentName eq '*'",
            )
        except Exception as exc:  # noqa: BLE001 - 单账户失败不应中断整体
            out[project_name(acc)] = {"__error__": str(exc)}
            continue

        for metric in resp.value:
            for ts in metric.timeseries or []:
                model = "(unknown)"
                for mv in ts.metadatavalues or []:
                    if mv.name.value.lower() == "modeldeploymentname":
                        model = mv.value
                total = 0
                for point in ts.data or []:
                    if point.total:
                        total += int(point.total)
                if total > 0:
                    models[model] = models.get(model, 0) + total
        out[project_name(acc)] = models
    return out


def aggregate(raw: dict) -> dict:
    """把 {account: {model: calls}} 聚合成结构化报告数据。"""
    model_total: dict[str, int] = defaultdict(int)
    proj_total: dict[str, int] = defaultdict(int)
    model_by_proj: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    errors: dict[str, str] = {}

    for acc, models in raw.items():
        if "__error__" in models:
            errors[acc] = models["__error__"]
            continue
        for model, calls in models.items():
            model_total[model] += calls
            proj_total[acc] += calls
            model_by_proj[model][acc] += calls

    grand = sum(proj_total.values())
    return {
        "grand_total": grand,
        "model_total": dict(model_total),
        "proj_total": dict(proj_total),
        "model_by_proj": {m: dict(p) for m, p in model_by_proj.items()},
        "errors": errors,
    }


def to_json(date_str: str, agg: dict) -> bytes:
    payload = {
        "date_beijing": date_str,
        "metric": METRIC_NAME,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "grand_total_calls": agg["grand_total"],
        "by_model": [
            {
                "model": m,
                "calls": c,
                "pct": round(c / agg["grand_total"] * 100, 4) if agg["grand_total"] else 0,
                "by_project": agg["model_by_proj"].get(m, {}),
            }
            for m, c in sorted(agg["model_total"].items(), key=lambda x: -x[1])
        ],
        "by_project": [
            {"project": p, "calls": c,
             "pct": round(c / agg["grand_total"] * 100, 4) if agg["grand_total"] else 0}
            for p, c in sorted(agg["proj_total"].items(), key=lambda x: -x[1])
        ],
        "errors": agg["errors"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _w(s: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    gap = width - _w(s)
    if gap <= 0:
        return s
    return (" " * gap + s) if right else (s + " " * gap)


def to_markdown(date_str: str, agg: dict) -> bytes:
    grand = agg["grand_total"]
    lines: list[str] = []
    W = 70
    lines.append("=" * W)
    lines.append(f"{date_str} 真实调用次数统计 (北京时间自然日 · 数据源: Azure Monitor {METRIC_NAME})")
    lines.append(f"当日全模型总调用次数: {grand:,} 次")
    if agg["errors"]:
        lines.append(f"注意: {len(agg['errors'])} 个账户查询失败 -> {list(agg['errors'])}")
    lines.append("=" * W)

    lines.append("")
    lines.append("【按模型 · 调用次数排行】")
    lines.append(
        f"{'#':>2} {_pad('模型', 26)} {_pad('调用次数', 10, True)} "
        f"{_pad('占比', 8, True)} {_pad('主要调用方(项目)', 20)}"
    )
    lines.append("-" * W)
    for i, (m, c) in enumerate(
        sorted(agg["model_total"].items(), key=lambda x: -x[1]), 1
    ):
        by = agg["model_by_proj"].get(m, {})
        top = max(by.items(), key=lambda x: x[1]) if by else ("-", 0)
        src = f"{top[0]}({top[1]})" if (len(by) == 1 or (c and top[1] / c > 0.6)) else "多项目分散"
        pct = c / grand * 100 if grand else 0
        lines.append(
            f"{i:>2} {_pad(m, 26)} {_pad(f'{c:,}', 10, True)} "
            f"{_pad(f'{pct:.2f}%', 8, True)} {_pad(src, 20)}"
        )
    lines.append("-" * W)
    lines.append(f"   {_pad('合计', 26)} {_pad(f'{grand:,}', 10, True)} {_pad('100.00%', 8, True)}")

    lines.append("")
    lines.append("=" * W)
    lines.append("【按项目(资源账户) · 调用次数汇总 = 谁在请求】")
    lines.append("=" * W)
    lines.append(f"{_pad('项目/资源', 24)} {_pad('调用次数', 10, True)} {_pad('占比', 8, True)}  Top模型")
    lines.append("-" * W)
    for proj, c in sorted(agg["proj_total"].items(), key=lambda x: -x[1]):
        by = agg["model_by_proj"]
        tm = sorted(
            ((m, p.get(proj, 0)) for m, p in by.items() if p.get(proj, 0) > 0),
            key=lambda x: -x[1],
        )[:2]
        tms = ", ".join(f"{m}:{n}" for m, n in tm)
        pct = c / grand * 100 if grand else 0
        lines.append(
            f"{_pad(proj, 24)} {_pad(f'{c:,}', 10, True)} {_pad(f'{pct:.2f}%', 8, True)}  {tms}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_reports(credential, cfg: dict, date_str: str):
    """返回 (json_bytes, md_bytes, agg)。"""
    raw = fetch_calls(credential, cfg, date_str)
    agg = aggregate(raw)
    return to_json(date_str, agg), to_markdown(date_str, agg), agg


def _yesterday_utc() -> str:
    """默认导出"昨天"(北京时间)。函数名保留兼容，实际返回北京昨天日期。"""
    return (dt.datetime.now(CST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    date_arg = sys.argv[1] if len(sys.argv) > 1 else _yesterday_utc()
    from azure.identity import AzureCliCredential

    cfg = get_config()
    _json, _md, _agg = build_reports(AzureCliCredential(), cfg, date_arg)
    print(_md.decode("utf-8"))
    print("\n--- JSON 预览(前 600 字) ---")
    print(_json.decode("utf-8")[:600])
