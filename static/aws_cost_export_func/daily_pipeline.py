# -*- coding: utf-8 -*-
"""AWS Bedrock 每日用量报告编排(纯逻辑，无 FastAPI 依赖)。

把三类数据源合并成一份"钱+量+调用者审计"三合一每日报告：

1. CE 成本      —— cost_fetch.ensure_daily_cost 拉取(双口径 + credit 拆分)。
2. Logs 用量    —— logs_fetch.fetch_logs_usage 查 CloudWatch Logs(次数 + 四类 token)。
3. CloudTrail   —— trail_fetch.fetch_trail_audit 查审计(IP + 活跃时段)。

再调用 shared/usage_report.generate() 渲染 md/html/json，落盘到唯一数据根
_data/reports/<date>/。

幂等与多 worker 安全：
- uvicorn --workers N 会每进程各起一个调度器，到点可能并发触发。用 <date>.lock
  文件做进程级互斥 + "当天已生成则跳过"，避免重复查询 AWS API(CE 每次计费)。

所有落盘严格限制在 cfg['data_dir'] 子目录树内，不碰其它业务目录。

对外主入口：``run(date, cfg) -> dict``。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import logging
import os
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Optional, TypedDict

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
CST = dt.timezone(dt.timedelta(hours=8))


class PipelineConfig(TypedDict):
    # AssumeRole / AWS 定位
    assume_role_arn: str
    region: str                  # 默认 region(CE 固定 us-east-1)
    regions: list[str]           # Logs/CloudTrail 查询的 region 列表
    linked_account: str
    log_group: str
    # 落盘
    data_dir: Path               # _data 根
    granularity: str             # CE 粒度 DAILY/HOURLY
    skip_if_exists: bool         # 当天报告已存在则跳过


def _load_module_by_path(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块 {name} @ {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mod(name: str) -> ModuleType:
    return _load_module_by_path(name, _HERE / f"{name}.py")


def _usage_report() -> ModuleType:
    return _load_module_by_path(
        "aws_usage_report_core", _HERE / "shared" / "usage_report.py"
    )


def _aws_session(cfg: PipelineConfig):
    sess_mod = _mod("aws_session")
    return sess_mod.AwsSession(sess_mod.AwsSessionConfig(
        assume_role_arn=cfg["assume_role_arn"], region=cfg["region"],
    ))


def _report_dir(cfg: PipelineConfig, date: str) -> Path:
    return cfg["data_dir"] / "reports" / date


def report_paths(cfg: PipelineConfig, date: str) -> dict[str, Path]:
    d = _report_dir(cfg, date)
    return {
        "md": d / f"aws-usage-{date}-CST.md",
        "html": d / f"aws-usage-{date}-CST.html",
        "json": d / f"aws-usage-{date}-CST.json",
    }


def is_generated(cfg: PipelineConfig, date: str) -> bool:
    p = report_paths(cfg, date)
    return p["md"].exists() and p["html"].exists()


def default_date() -> str:
    """默认目标日期 = 昨天(北京自然日)。"""
    return (dt.datetime.now(CST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")


def run(date: str, cfg: PipelineConfig) -> dict[str, Any]:
    """执行一次完整流水线：CE 成本 → Logs 用量 → CloudTrail 审计 → generate → 落盘。

    三类数据源任一失败都不阻断其它(降级为空，报告标注缺失)，最大化产出可用报告。
    """
    out_dir = _report_dir(cfg, date)
    out_dir.mkdir(parents=True, exist_ok=True)

    lock = out_dir / ".lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime > 3600:
                lock.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("当天 %s 已有流水线在处理(锁存在)，本次跳过。", date)
        return {"date": date, "skipped": True, "reason": "locked"}

    try:
        if is_generated(cfg, date) and cfg["skip_if_exists"]:
            logger.info("当天 %s 报告已存在，跳过。", date)
            paths = report_paths(cfg, date)
            return {"date": date, "skipped": True, "reason": "exists",
                    "paths": {k: str(v) for k, v in paths.items()}}

        session = _aws_session(cfg)
        daily_cost_dir = cfg["data_dir"] / "daily_cost"
        src_cache_dir = cfg["data_dir"] / "src_cache"

        # 1) CE 成本(失败降级)
        cost_path: Optional[str] = None
        try:
            cost_mod = _mod("cost_fetch")
            cp = cost_mod.ensure_daily_cost(
                date, session,
                {"linked_account": cfg["linked_account"],
                 "daily_cost_dir": daily_cost_dir,
                 "granularity": cfg["granularity"]},
                skip_if_exists=cfg["skip_if_exists"],
            )
            cost_path = str(cp)
        except Exception as e:
            logger.error("CE 成本拉取失败(报告将标注成本缺失): %s", e, exc_info=True)

        # 2) Logs 用量(失败降级)
        logs_path: Optional[str] = None
        try:
            logs_mod = _mod("logs_fetch")
            usage = logs_mod.fetch_logs_usage(
                date, session,
                {"log_group": cfg["log_group"], "regions": cfg["regions"],
                 "src_cache_dir": src_cache_dir},
                write_detail=True,
            )
            logs_path = str(logs_mod.save_logs_usage(usage, {
                "log_group": cfg["log_group"], "regions": cfg["regions"],
                "src_cache_dir": src_cache_dir}))
        except Exception as e:
            logger.error("Logs 用量拉取失败(报告将标注用量缺失): %s", e, exc_info=True)

        # 3) CloudTrail 审计(失败降级)
        trail_path: Optional[str] = None
        try:
            trail_mod = _mod("trail_fetch")
            audit = trail_mod.fetch_trail_audit(
                date, session,
                {"regions": cfg["regions"], "src_cache_dir": src_cache_dir})
            trail_path = str(trail_mod.save_trail_audit(audit, {
                "regions": cfg["regions"], "src_cache_dir": src_cache_dir}))
        except Exception as e:
            logger.error("CloudTrail 审计拉取失败(报告将标注审计缺失): %s", e, exc_info=True)

        # 4) 生成报告
        ur = _usage_report()
        report, md, html = ur.generate(date, cost_path, logs_path, trail_path)
        report_json = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")

        paths = report_paths(cfg, date)
        paths["md"].write_bytes(md)
        paths["html"].write_bytes(html)
        paths["json"].write_bytes(report_json)
        logger.info("报告已落盘: %s", out_dir)

        return {
            "date": date,
            "skipped": False,
            "total_cost_real": report.get("total_cost_real"),
            "total_cost_paid": report.get("total_cost_paid"),
            "total_credit": report.get("total_credit"),
            "total_invocations": report.get("total_invocations"),
            "total_tokens": report.get("total_tokens"),
            "has_cost": report.get("has_cost"),
            "has_logs": report.get("has_logs"),
            "has_trail": report.get("has_trail"),
            "paths": {k: str(v) for k, v in paths.items()},
        }
    finally:
        lock.unlink(missing_ok=True)


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    p = argparse.ArgumentParser(description="AWS Bedrock 每日用量报告流水线(一键)。")
    p.add_argument("date", nargs="?", default=default_date())
    p.add_argument("--role-arn", default=os.environ.get(
        "AWS_USAGE_REPORT_ASSUME_ROLE_ARN",
        "arn:aws:iam::502225588666:role/OrganizationAccountAccessRole"))
    p.add_argument("--region", default=os.environ.get("AWS_USAGE_REPORT_REGION", "us-east-1"))
    p.add_argument("--regions", default=os.environ.get("AWS_USAGE_REPORT_REGIONS", "us-east-1"))
    p.add_argument("--linked-account", default=os.environ.get(
        "AWS_USAGE_REPORT_LINKED_ACCOUNT", "502225588666"))
    p.add_argument("--log-group", default=os.environ.get(
        "AWS_USAGE_REPORT_LOG_GROUP", "/bedrock/model-invocations"))
    p.add_argument("--granularity", default="DAILY")
    p.add_argument("--data-dir", default="./_data")
    p.add_argument("--force", action="store_true", help="忽略已存在，强制重跑。")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    cfg: PipelineConfig = {
        "assume_role_arn": args.role_arn,
        "region": args.region,
        "regions": [r.strip() for r in args.regions.split(",") if r.strip()],
        "linked_account": args.linked_account,
        "log_group": args.log_group,
        "data_dir": Path(args.data_dir),
        "granularity": args.granularity,
        "skip_if_exists": not args.force,
    }
    result = run(args.date, cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
