# -*- coding: utf-8 -*-
"""成本拉取(Cost Explorer)：对齐 Azure 账单 CSV 的"金额来源"角色。

关键设计(真实验证结论)：
- **credit 会把 UnblendedCost 抵成 0**(官方确认)。若只看 UnblendedCost，会误判"没花钱"。
  故本模块同时取 **UnblendedCost(实付/现金口径)** 与 **AmortizedCost(真实发生成本)**，
  并额外按 **RECORD_TYPE** 拆出 Usage / Credit / Tax / Refund，报告层三行并列展示。
- CE 按 SERVICE 分组时，Bedrock 各模型是**独立 SERVICE 名**(如
  "Claude Opus 4.8 (Amazon Bedrock Edition)")，天然给出**模型级金额**，无需像 Azure 靠 tags 拆。
- 只统计子账号 502225588666(LINKED_ACCOUNT 过滤)。

时间口径：CE 的 HOURLY 粒度需 **Payer 账号在 Cost Explorer 设置里 opt-in**(实测未开，
报 "Hourly data granularity is an opt-in only feature")。故默认用 **DAILY**(按 UTC 日界)，
查询北京日 date 对应的起始 UTC 日 ``[date-1, date)``；成本 CE 端本就按 UTC 归集，这与
用量/日志侧按北京日的口径存在最多 8h 边界偏移，报告层如实标注。若日后 Payer 开了 HOURLY
opt-in，把 granularity 设为 HOURLY 即可精确对齐北京自然日(窗口 ``[date-1 16:00Z, date 16:00Z)``)。

对外主入口：``fetch_daily_cost(date, session, cfg) -> dict``，并落盘 JSON。

独立运行：
    python -m cost_fetch 2026-08-28
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Optional, TypedDict

logger = logging.getLogger(__name__)

CST = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc


class CostConfig(TypedDict):
    linked_account: str      # 子账号 ID，如 "502225588666"
    daily_cost_dir: Path     # 落盘目录 _data/daily_cost
    granularity: str         # "HOURLY" 或 "DAILY"


def _beijing_day_utc_window(date: str) -> tuple[dt.datetime, dt.datetime]:
    """北京自然日 date → [start_utc, end_utc)。

    北京 date 00:00 = UTC (date-1) 16:00；跨 24 小时。
    """
    d = dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=CST)
    start_cst = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end_cst = start_cst + dt.timedelta(days=1)
    return start_cst.astimezone(UTC), end_cst.astimezone(UTC)


def _iso_hour(t: dt.datetime) -> str:
    """CE HOURLY 需要的 ISO8601 时间(带 Z)，形如 2026-08-27T16:00:00Z。"""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _linked_account_filter(linked_account: str) -> dict[str, Any]:
    return {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [linked_account]}}


def _sum_groups_by_key(
    results_by_time: list[dict[str, Any]], metric: str
) -> dict[str, float]:
    """把多个时间片(HOURLY 会有 24 片)的 Groups 按 Key 累加成 {key: amount}。"""
    agg: dict[str, float] = {}
    for slot in results_by_time:
        for g in slot.get("Groups", []):
            key = g["Keys"][0]
            try:
                amt = float(g["Metrics"][metric]["Amount"])
            except (KeyError, ValueError, TypeError):
                amt = 0.0
            agg[key] = agg.get(key, 0.0) + amt
    return agg


def _sum_total(results_by_time: list[dict[str, Any]], metric: str) -> float:
    """把多个时间片的 Total[metric] 累加(无分组查询用)。"""
    total = 0.0
    for slot in results_by_time:
        try:
            total += float(slot["Total"][metric]["Amount"])
        except (KeyError, ValueError, TypeError):
            pass
    return total


class DailyCost(TypedDict):
    date: str                       # 北京自然日
    granularity: str
    window_utc: dict[str, str]      # {start, end}
    # 模型级金额(SERVICE 分组)：两种口径。
    model_cost_amortized: dict[str, float]  # 真实用量成本(过滤 Credit 记录类型，不被抵消)
    model_cost_unblended: dict[str, float]  # 实付(含 credit 抵扣后的净额)
    # 记录类型拆分：Usage / Credit / Tax / Refund ...(UnblendedCost 口径)
    record_type_amortized: dict[str, float]
    # 汇总
    total_amortized: float          # 真实用量成本合计(credit 前)
    total_unblended: float          # 实付合计(credit 后)
    total_credit: float             # credit 抵扣额(负数)
    has_data: bool


def fetch_daily_cost(
    date: str, session: Any, cfg: CostConfig
) -> DailyCost:
    """拉取子账号在北京自然日 date 的成本(双口径 + 模型级 + 记录类型拆分)。

    Parameters
    ----------
    date: 北京自然日 YYYY-MM-DD。
    session: aws_session.AwsSession(已 AssumeRole 到子账号)。
    cfg: CostConfig。
    """
    ce = session.client("ce")
    start_utc, end_utc = _beijing_day_utc_window(date)
    granularity = cfg.get("granularity", "DAILY").upper()
    acct_filter = _linked_account_filter(cfg["linked_account"])

    if granularity == "HOURLY":
        time_period = {"Start": _iso_hour(start_utc), "End": _iso_hour(end_utc)}
        window_desc = {"start": _iso_hour(start_utc), "end": _iso_hour(end_utc),
                       "basis": "beijing-day (HOURLY, precise)"}
    else:
        # DAILY：CE 用日期字符串，按 UTC 日界。取与北京日 date 同名的 UTC 日
        # [date, date+1)。CE 成本本按 UTC 归集，这是最接近控制台按该日期看到的口径。
        time_period = {
            "Start": date,
            "End": (dt.datetime.strptime(date, "%Y-%m-%d") + dt.timedelta(days=1)).strftime("%Y-%m-%d"),
        }
        window_desc = {"start": time_period["Start"], "end": time_period["End"],
                       "basis": "UTC-day (DAILY; up to 8h boundary offset vs Beijing day)"}

    def _query(metrics: list[str], group_by_key: Optional[str],
               extra_filter: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        base_filter = acct_filter
        if extra_filter is not None:
            base_filter = {"And": [acct_filter, extra_filter]}
        kwargs: dict[str, Any] = {
            "TimePeriod": time_period,
            "Granularity": granularity,
            "Metrics": metrics,
            "Filter": base_filter,
        }
        if group_by_key:
            kwargs["GroupBy"] = [{"Type": "DIMENSION", "Key": group_by_key}]
        return ce.get_cost_and_usage(**kwargs)

    # 1) 模型级"真实成本"：SERVICE 分组，但**过滤掉 Credit 记录类型**，否则 credit 会
    #    被摊到各 service 上把模型成本抵成 0(实测 8/28 即如此)。用 RECORD_TYPE=Usage
    #    过滤后的 UnblendedCost 才是"未被 credit 抵消的真实用量成本"。
    usage_only = {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}
    r_service = _query(["UnblendedCost"], "SERVICE", extra_filter=usage_only)
    model_amortized = _sum_groups_by_key(r_service["ResultsByTime"], "UnblendedCost")

    # 2) 模型级"实付"：SERVICE 分组，不过滤记录类型，取 UnblendedCost(被 credit 抵后)。
    r_service_net = _query(["UnblendedCost"], "SERVICE")
    model_unblended = _sum_groups_by_key(r_service_net["ResultsByTime"], "UnblendedCost")

    # 3) 记录类型拆分：Usage / Credit / Tax ...(UnblendedCost 口径，看 credit 抵扣额)。
    r_record = _query(["UnblendedCost"], "RECORD_TYPE")
    record_amortized = _sum_groups_by_key(r_record["ResultsByTime"], "UnblendedCost")

    total_amortized = sum(model_amortized.values())
    total_unblended = sum(model_unblended.values())
    total_credit = record_amortized.get("Credit", 0.0)

    result: DailyCost = {
        "date": date,
        "granularity": granularity,
        "window_utc": window_desc,
        "model_cost_amortized": model_amortized,
        "model_cost_unblended": model_unblended,
        "record_type_amortized": record_amortized,
        "total_amortized": total_amortized,
        "total_unblended": total_unblended,
        "total_credit": total_credit,
        "has_data": bool(model_amortized),
    }
    return result


def save_daily_cost(result: DailyCost, cfg: CostConfig) -> Path:
    """把成本结果落盘到 _data/daily_cost/<date>.json。"""
    out_dir = cfg["daily_cost_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result['date']}.json"
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("成本已落盘: %s (真实成本 $%.4f / 实付 $%.4f)",
                path, result["total_amortized"], result["total_unblended"])
    return path


def ensure_daily_cost(
    date: str, session: Any, cfg: CostConfig, skip_if_exists: bool = False
) -> Path:
    """幂等封装：已存在且 skip_if_exists 则直接返回，否则拉取并落盘。"""
    path = cfg["daily_cost_dir"] / f"{date}.json"
    if skip_if_exists and path.exists():
        logger.info("成本文件已存在，跳过拉取: %s", path)
        return path
    result = fetch_daily_cost(date, session, cfg)
    return save_daily_cost(result, cfg)


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import os
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    from aws_session import AwsSession, AwsSessionConfig

    p = argparse.ArgumentParser(description="拉取子账号 Bedrock 每日成本(CE 双口径)。")
    p.add_argument("date", nargs="?", help="北京自然日 YYYY-MM-DD(默认昨天)。")
    p.add_argument("--linked-account", default=os.environ.get(
        "AWS_USAGE_REPORT_LINKED_ACCOUNT", "502225588666"))
    p.add_argument("--role-arn", default=os.environ.get(
        "AWS_USAGE_REPORT_ASSUME_ROLE_ARN",
        "arn:aws:iam::502225588666:role/OrganizationAccountAccessRole"))
    p.add_argument("--region", default=os.environ.get("AWS_USAGE_REPORT_REGION", "us-east-1"))
    p.add_argument("--granularity", default="DAILY")
    p.add_argument("--out-dir", default="./_data/daily_cost")
    args = p.parse_args(argv)

    date = args.date or (dt.datetime.now(CST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    session = AwsSession(AwsSessionConfig(assume_role_arn=args.role_arn, region=args.region))
    cfg: CostConfig = {
        "linked_account": args.linked_account,
        "daily_cost_dir": Path(args.out_dir),
        "granularity": args.granularity,
    }
    result = fetch_daily_cost(date, session, cfg)
    save_daily_cost(result, cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_main())
