# -*- coding: utf-8 -*-
"""用量拉取(CloudWatch Logs Insights)：对齐 Azure calls JSON + requests NDJSON。

数据源：昨天已开启的 Bedrock model invocation logging，写入 CloudWatch Logs 组
``/bedrock/model-invocations``。官方 schema + 实测确认每条记录含:

    timestamp / accountId / region / requestId / operation / modelId
    input.{inputTokenCount, cacheReadInputTokenCount, cacheWriteInputTokenCount}
    output.outputTokenCount
    identity.arn        # 区分"谁"的可靠维度(cursor-bedrock-user vs intern-bedrock)

**关键真实发现**：绝大部分输入 token 走 prompt cache
(如 inputTokenCount=2 但 cacheReadInputTokenCount=95500)，故必须同时统计四类 token，
否则会严重低估用量。

本模块做两件事：
1) 聚合查询：``stats ... by identity.arn, modelId`` 得每个"调用者×模型"的
   次数 + 四类 token 合计 → 报告的主表数据源。
2) 逐条落盘：把当天每条 invocation 摘要写 NDJSON(对齐 Azure requests 字段)，
   供审计/明细预览。

时间口径：Logs Insights 用 epoch 秒；报告按**北京自然日**，窗口
``[date 00:00 CST, date+1 00:00 CST)``。

独立运行：
    python -m logs_fetch 2026-08-28
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional, TypedDict

logger = logging.getLogger(__name__)

CST = dt.timezone(dt.timedelta(hours=8))
UTC = dt.timezone.utc

LOG_GROUP_DEFAULT = "/bedrock/model-invocations"

# 聚合查询：按 调用者 × 模型 汇总次数与四类 token。
_AGG_QUERY = """
fields @timestamp, identity.arn as caller, modelId as model,
       input.inputTokenCount as inTok,
       input.cacheReadInputTokenCount as cacheRead,
       input.cacheWriteInputTokenCount as cacheWrite,
       output.outputTokenCount as outTok
| stats count(*) as invocations,
        sum(inTok) as sumInputTokens,
        sum(cacheRead) as sumCacheReadTokens,
        sum(cacheWrite) as sumCacheWriteTokens,
        sum(outTok) as sumOutputTokens
        by caller, model
| sort invocations desc
""".strip()

# 逐条明细查询(落 NDJSON)。limit 上限 10000(Logs Insights 单查询硬上限)。
_DETAIL_QUERY = """
fields @timestamp, identity.arn as caller, modelId as model, operation as op,
       region as region, requestId as requestId,
       input.inputTokenCount as inputTokens,
       input.cacheReadInputTokenCount as cacheReadTokens,
       input.cacheWriteInputTokenCount as cacheWriteTokens,
       output.outputTokenCount as outputTokens
| sort @timestamp asc
| limit 10000
""".strip()


class LogsConfig(TypedDict):
    log_group: str
    regions: list[str]           # 需查询的 region 列表(可能多 region 用量)
    src_cache_dir: Path          # _data/src_cache/<date> 的父目录 _data/src_cache


def _beijing_day_epoch_window(date: str) -> tuple[int, int]:
    d = dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=CST)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _run_insights_query(
    logs_client: Any, log_group: str, query: str, start: int, end: int,
    poll_max: int = 60, poll_interval: float = 1.0,
) -> list[list[dict[str, str]]]:
    """跑一个 Logs Insights 查询并轮询到完成，返回 results(行的列表)。"""
    q = logs_client.start_query(
        logGroupName=log_group, startTime=start, endTime=end, queryString=query
    )
    qid = q["queryId"]
    r = None
    for _ in range(poll_max):
        r = logs_client.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(poll_interval)
    if r is None or r["status"] != "Complete":
        raise RuntimeError(f"Logs Insights 查询未完成: status={r['status'] if r else 'None'}")
    return r["results"]


def _row_to_dict(row: list[dict[str, str]]) -> dict[str, str]:
    return {f["field"]: f["value"] for f in row}


def _to_int(v: Optional[str]) -> int:
    try:
        return int(float(v)) if v not in (None, "") else 0
    except (ValueError, TypeError):
        return 0


class CallerModelStat(TypedDict):
    caller: str          # identity.arn(短名在报告层再截取)
    model: str
    invocations: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int


class LogsUsage(TypedDict):
    date: str
    log_group: str
    regions: list[str]
    window_epoch: dict[str, int]
    # 按 调用者×模型 聚合。
    caller_model_stats: list[CallerModelStat]
    # 便捷汇总：按调用者、按模型。
    totals_by_caller: dict[str, dict[str, int]]
    totals_by_model: dict[str, dict[str, int]]
    total_invocations: int
    detail_ndjson_path: Optional[str]
    has_data: bool


def _accumulate(dst: dict[str, dict[str, int]], key: str, stat: CallerModelStat) -> None:
    cur = dst.setdefault(key, {
        "invocations": 0, "input_tokens": 0, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "output_tokens": 0,
    })
    cur["invocations"] += stat["invocations"]
    cur["input_tokens"] += stat["input_tokens"]
    cur["cache_read_tokens"] += stat["cache_read_tokens"]
    cur["cache_write_tokens"] += stat["cache_write_tokens"]
    cur["output_tokens"] += stat["output_tokens"]


def fetch_logs_usage(
    date: str, session: Any, cfg: LogsConfig, write_detail: bool = True
) -> LogsUsage:
    """聚合当天 Bedrock invocation 用量(多 region 合并)，可选逐条落 NDJSON。"""
    start, end = _beijing_day_epoch_window(date)
    log_group = cfg["log_group"]
    regions = cfg["regions"]

    merged: dict[tuple[str, str], CallerModelStat] = {}
    detail_rows: list[dict[str, Any]] = []

    for region in regions:
        logs_client = session.client("logs", region=region)
        # 1) 聚合
        try:
            agg = _run_insights_query(logs_client, log_group, _AGG_QUERY, start, end)
        except logs_client.exceptions.ResourceNotFoundException:
            logger.warning("region %s 无日志组 %s，跳过", region, log_group)
            continue
        except logs_client.exceptions.MalformedQueryException as e:
            # 查询窗口早于日志组创建时间 / 超出保留期(如 logging 刚开启，历史日补不到)。
            # 这是数据保留的真实边界，不是错误：该 region 该日无可查数据，降级为空。
            logger.warning("region %s 该日窗口无可查日志(可能早于日志组创建/超出保留期): %s",
                           region, e)
            continue
        for row in agg:
            d = _row_to_dict(row)
            caller = d.get("caller", "(unknown)")
            model = d.get("model", "(unknown)")
            stat: CallerModelStat = {
                "caller": caller, "model": model,
                "invocations": _to_int(d.get("invocations")),
                "input_tokens": _to_int(d.get("sumInputTokens")),
                "cache_read_tokens": _to_int(d.get("sumCacheReadTokens")),
                "cache_write_tokens": _to_int(d.get("sumCacheWriteTokens")),
                "output_tokens": _to_int(d.get("sumOutputTokens")),
            }
            k = (caller, model)
            if k in merged:
                m = merged[k]
                for f in ("invocations", "input_tokens", "cache_read_tokens",
                          "cache_write_tokens", "output_tokens"):
                    m[f] += stat[f]  # type: ignore[literal-required]
            else:
                merged[k] = stat

        # 2) 逐条明细
        if write_detail:
            try:
                det = _run_insights_query(logs_client, log_group, _DETAIL_QUERY, start, end)
            except (logs_client.exceptions.ResourceNotFoundException,
                    logs_client.exceptions.MalformedQueryException):
                det = []
            for row in det:
                d = _row_to_dict(row)
                ts_raw = d.get("@timestamp")  # 形如 "2026-08-28 12:00:00.000"(UTC)
                detail_rows.append({
                    "timestamp_utc": ts_raw,
                    "timestamp_cst": _utc_str_to_cst(ts_raw),
                    "caller": d.get("caller"),
                    "model": d.get("model"),
                    "operation": d.get("op"),
                    "region": d.get("region") or region,
                    "requestId": d.get("requestId"),
                    "input_tokens": _to_int(d.get("inputTokens")),
                    "cache_read_tokens": _to_int(d.get("cacheReadTokens")),
                    "cache_write_tokens": _to_int(d.get("cacheWriteTokens")),
                    "output_tokens": _to_int(d.get("outputTokens")),
                })

    stats = sorted(merged.values(), key=lambda x: x["invocations"], reverse=True)
    totals_by_caller: dict[str, dict[str, int]] = {}
    totals_by_model: dict[str, dict[str, int]] = {}
    for st in stats:
        _accumulate(totals_by_caller, st["caller"], st)
        _accumulate(totals_by_model, st["model"], st)

    detail_path: Optional[str] = None
    if write_detail:
        detail_path = _write_detail_ndjson(date, detail_rows, cfg)

    return {
        "date": date,
        "log_group": log_group,
        "regions": regions,
        "window_epoch": {"start": start, "end": end},
        "caller_model_stats": stats,
        "totals_by_caller": totals_by_caller,
        "totals_by_model": totals_by_model,
        "total_invocations": sum(s["invocations"] for s in stats),
        "detail_ndjson_path": detail_path,
        "has_data": bool(stats),
    }


def _utc_str_to_cst(ts_raw: Optional[str]) -> Optional[str]:
    """Logs Insights 的 @timestamp 是 UTC 字符串 'YYYY-MM-DD HH:MM:SS.mmm'。转北京时间。"""
    if not ts_raw:
        return None
    try:
        t = dt.datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
    except ValueError:
        try:
            t = dt.datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None
    return t.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def _write_detail_ndjson(date: str, rows: list[dict[str, Any]], cfg: LogsConfig) -> str:
    day_dir = cfg["src_cache_dir"] / date
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "invocations.ndjson"
    rows.sort(key=lambda r: r.get("timestamp_utc") or "")
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    logger.info("逐条明细已落盘: %s (%d 条)", path, len(rows))
    return str(path)


def save_logs_usage(usage: LogsUsage, cfg: LogsConfig) -> Path:
    day_dir = cfg["src_cache_dir"] / usage["date"]
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "logs_usage.json"
    path.write_text(json.dumps(usage, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("用量聚合已落盘: %s (总调用 %d 次)", path, usage["total_invocations"])
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

    p = argparse.ArgumentParser(description="拉取子账号 Bedrock 每日用量(CloudWatch Logs)。")
    p.add_argument("date", nargs="?")
    p.add_argument("--role-arn", default=os.environ.get(
        "AWS_USAGE_REPORT_ASSUME_ROLE_ARN",
        "arn:aws:iam::502225588666:role/OrganizationAccountAccessRole"))
    p.add_argument("--region", default=os.environ.get("AWS_USAGE_REPORT_REGION", "us-east-1"))
    p.add_argument("--regions", default=os.environ.get("AWS_USAGE_REPORT_REGIONS", "us-east-1"),
                   help="逗号分隔的 region 列表")
    p.add_argument("--log-group", default=os.environ.get(
        "AWS_USAGE_REPORT_LOG_GROUP", LOG_GROUP_DEFAULT))
    p.add_argument("--out-dir", default="./_data/src_cache")
    p.add_argument("--no-detail", action="store_true")
    args = p.parse_args(argv)

    date = args.date or (dt.datetime.now(CST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    session = AwsSession(AwsSessionConfig(assume_role_arn=args.role_arn, region=args.region))
    cfg: LogsConfig = {
        "log_group": args.log_group,
        "regions": [r.strip() for r in args.regions.split(",") if r.strip()],
        "src_cache_dir": Path(args.out_dir),
    }
    usage = fetch_logs_usage(date, session, cfg, write_detail=not args.no_detail)
    save_logs_usage(usage, cfg)
    print(json.dumps({
        "date": usage["date"], "total_invocations": usage["total_invocations"],
        "totals_by_caller": usage["totals_by_caller"],
        "caller_model_stats": usage["caller_model_stats"][:10],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_main())
