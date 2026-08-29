# -*- coding: utf-8 -*-
"""IP/审计补充(CloudTrail LookupEvents)：给报告补 IP 维度 + 精确调用者/活跃时段。

数据源：CloudTrail 管理事件历史(免费查询)。用 ``LookupAttributes`` 按
``EventSource=bedrock-runtime.amazonaws.com``(Converse/InvokeModel 走 runtime)过滤，
避免全量扫描(全量分页极慢)。

**关键真实结论(务必如实呈现)**：CloudTrail 事件里的 ``sourceIPAddress`` 是
**Cursor 云端中转服务器的 AWS 网段 IP，不是真人 IP**——因为调用经 Cursor 代理发出。
故 IP 列仅作"来源网段"参考，报告明确标注"Cursor 中转 IP，非真人"。区分"谁"仍以
CloudWatch Logs 的 ``identity.arn`` 为准。

按 ``identity.arn`` 聚合：出现的 sourceIPAddress 集合 + 事件次数 + 首末时间 + 逐小时分布，
落 ``_data/src_cache/<date>/trail.json``。

时间口径：CloudTrail 用 UTC datetime；窗口按北京自然日
``[date 00:00 CST, date+1 00:00 CST)``。

独立运行：
    python -m trail_fetch 2026-08-28
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional, TypedDict

logger = logging.getLogger(__name__)

CST = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc

# Bedrock 调用事件的 EventSource。运行时调用(Converse/InvokeModel)走 bedrock-runtime；
# 控制面(如配置)走 bedrock。两者都查以防漏。
BEDROCK_EVENT_SOURCES = ["bedrock-runtime.amazonaws.com", "bedrock.amazonaws.com"]


class TrailConfig(TypedDict):
    regions: list[str]
    src_cache_dir: Path


def _beijing_day_utc_window(date: str) -> tuple[dt.datetime, dt.datetime]:
    d = dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=CST)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def _extract_caller_arn(rec: dict[str, Any]) -> str:
    """从 CloudTrail 事件的 userIdentity 提取调用者标识(对齐 Logs 的 identity.arn)。"""
    ui = rec.get("userIdentity", {})
    return ui.get("arn") or ui.get("principalId") or ui.get("type") or "(unknown)"


class CallerTrailStat(TypedDict):
    caller: str
    event_count: int
    source_ips: list[str]        # 去重后的 sourceIPAddress(Cursor 中转 IP)
    user_agents: list[str]
    first_seen_utc: Optional[str]
    last_seen_utc: Optional[str]
    first_seen_cst: Optional[str]
    last_seen_cst: Optional[str]
    hourly_cst: dict[str, int]   # 北京时 "00".."23" -> 次数


class TrailAudit(TypedDict):
    date: str
    regions: list[str]
    window_utc: dict[str, str]
    caller_stats: list[CallerTrailStat]
    total_events: int
    ip_note: str
    has_data: bool


def fetch_trail_audit(date: str, session: Any, cfg: TrailConfig) -> TrailAudit:
    start_utc, end_utc = _beijing_day_utc_window(date)
    regions = cfg["regions"]

    # 聚合容器：caller -> 累加状态
    agg: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "event_count": 0,
        "source_ips": set(),
        "user_agents": set(),
        "first": None,
        "last": None,
        "hourly": defaultdict(int),
    })
    total_events = 0

    for region in regions:
        ct = session.client("cloudtrail", region=region)
        paginator = ct.get_paginator("lookup_events")
        for src in BEDROCK_EVENT_SOURCES:
            page_iter = paginator.paginate(
                LookupAttributes=[{"AttributeKey": "EventSource", "AttributeValue": src}],
                StartTime=start_utc,
                EndTime=end_utc,
            )
            for page in page_iter:
                for ev in page.get("Events", []):
                    try:
                        rec = json.loads(ev["CloudTrailEvent"])
                    except (KeyError, json.JSONDecodeError):
                        continue
                    caller = _extract_caller_arn(rec)
                    et_raw = rec.get("eventTime")  # ISO8601 UTC, e.g. 2026-08-28T12:00:00Z
                    et = _parse_iso_utc(et_raw)
                    a = agg[caller]
                    a["event_count"] += 1
                    total_events += 1
                    ip = rec.get("sourceIPAddress")
                    if ip:
                        a["source_ips"].add(ip)
                    ua = rec.get("userAgent")
                    if ua:
                        a["user_agents"].add(ua)
                    if et is not None:
                        if a["first"] is None or et < a["first"]:
                            a["first"] = et
                        if a["last"] is None or et > a["last"]:
                            a["last"] = et
                        hour_cst = et.astimezone(CST).strftime("%H")
                        a["hourly"][hour_cst] += 1

    caller_stats: list[CallerTrailStat] = []
    for caller, a in agg.items():
        first = a["first"]
        last = a["last"]
        caller_stats.append({
            "caller": caller,
            "event_count": a["event_count"],
            "source_ips": sorted(a["source_ips"]),
            "user_agents": sorted(a["user_agents"]),
            "first_seen_utc": first.strftime("%Y-%m-%d %H:%M:%S") if first else None,
            "last_seen_utc": last.strftime("%Y-%m-%d %H:%M:%S") if last else None,
            "first_seen_cst": first.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S") if first else None,
            "last_seen_cst": last.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S") if last else None,
            "hourly_cst": dict(sorted(a["hourly"].items())),
        })
    caller_stats.sort(key=lambda x: x["event_count"], reverse=True)

    return {
        "date": date,
        "regions": regions,
        "window_utc": {
            "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "caller_stats": caller_stats,
        "total_events": total_events,
        "ip_note": "sourceIPAddress 为 Cursor 云端中转服务器 AWS 网段 IP，非真人 IP；区分调用者以 identity.arn 为准。",
        "has_data": bool(caller_stats),
    }


def _parse_iso_utc(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def save_trail_audit(audit: TrailAudit, cfg: TrailConfig) -> Path:
    day_dir = cfg["src_cache_dir"] / audit["date"]
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "trail.json"
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("CloudTrail 审计已落盘: %s (%d 事件)", path, audit["total_events"])
    return path


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import os
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    from aws_session import AwsSession, AwsSessionConfig

    p = argparse.ArgumentParser(description="拉取子账号 Bedrock CloudTrail 审计(IP/时段)。")
    p.add_argument("date", nargs="?")
    p.add_argument("--role-arn", default=os.environ.get(
        "AWS_USAGE_REPORT_ASSUME_ROLE_ARN",
        "arn:aws:iam::502225588666:role/OrganizationAccountAccessRole"))
    p.add_argument("--region", default=os.environ.get("AWS_USAGE_REPORT_REGION", "us-east-1"))
    p.add_argument("--regions", default=os.environ.get("AWS_USAGE_REPORT_REGIONS", "us-east-1"))
    p.add_argument("--out-dir", default="./_data/src_cache")
    args = p.parse_args(argv)

    date = args.date or (dt.datetime.now(CST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    session = AwsSession(AwsSessionConfig(assume_role_arn=args.role_arn, region=args.region))
    cfg: TrailConfig = {
        "regions": [r.strip() for r in args.regions.split(",") if r.strip()],
        "src_cache_dir": Path(args.out_dir),
    }
    audit = fetch_trail_audit(date, session, cfg)
    save_trail_audit(audit, cfg)
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_main())
