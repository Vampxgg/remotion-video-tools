# -*- coding: utf-8 -*-
"""每日花销守卫：按 IAM 用户近实时估算今日花销，超阈值挂 Deny，次日自动解除。

背景与根因(真实验证结论)：
- 需求是"限制每天花销"，但 Bedrock **没有原生的每日花销硬上限**(官方明确
  "no Stop at $X button")；AWS Budgets 只按天更新且周期最细到月，不能做每日硬闸。
- 且 Cost Explorer **无法按 IAM 用户拆成本**(无 IAM principal 维度)。要"按人+近实时"，
  唯一可行信号是 CloudWatch Logs invocation logging 的 identity.arn + 四类 token。
- 故本模块：从 Logs 聚合当天每个 IAM 用户×模型的 token → pricing 估算每人今日花销 →
  超过阈值就用子账号 Admin 给**该 IAM 用户**挂一条 inline Deny 策略(禁 bedrock 调用) →
  次日 0 点(北京)由调度器统一解除。谁超禁谁，互不影响。

阻断机制(为什么用 inline policy)：
- ``iam.put_user_policy`` 给用户挂一条独立的 inline Deny，与用户既有权限正交，
  加/删互不干扰、无需读改写原策略，最干净。Deny 显式优先于任何 Allow，立即生效。
- policy 名固定 ``SpendGuardDailyDeny``，便于幂等判断"是否已封"和统一解除。

估算 vs 实付：本模块用 token×单价的**估算**成本驱动封禁决策(strict mode，近实时)。
真实对账仍以每日报告的 CE 金额为准。估算偏保守(缓存写按高价、未知模型回退最贵档)，
宁可早封不可漏封。

时间口径：与 logs_fetch 一致，按**北京自然日** ``[date 00:00, date+1 00:00) CST``。

对外主入口：
- ``evaluate(cfg) -> GuardResult``   评估今日各用户花销并按需封禁(dry_run 可只评估不封)。
- ``release_all(cfg) -> dict``        解除所有 SpendGuard 封禁(次日重置 / 手动放开)。
- ``current_status(cfg) -> dict``     当前谁被封了(读 inline policy 存在性)。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import logging
from pathlib import Path
from types import ModuleType
from typing import Any, Optional, TypedDict

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
_FUNC_ROOT = _HERE.parent  # aws_cost_export_func/
CST = dt.timezone(dt.timedelta(hours=8))

# 固定的 inline policy 名：既是封禁标记，也是幂等/解除的锚点。
DENY_POLICY_NAME = "SpendGuardDailyDeny"

# 被封时禁用的动作：覆盖 Bedrock 运行时的全部推理入口。
_DENIED_ACTIONS = [
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
]


class GuardConfig(TypedDict):
    # AssumeRole / AWS 定位(复用报告那套)
    assume_role_arn: str
    region: str
    regions: list[str]
    log_group: str
    # 限额
    daily_limit_usd: float                 # 每人每日估算花销上限(USD)
    only_users: list[str]                  # 仅对这些 IAM 用户名生效(空=全部出现在日志里的用户)
    data_dir: Path                         # 复用 _data 根(src_cache 落 logs 聚合)


class UserSpend(TypedDict):
    user_arn: str
    user_name: str
    est_cost_usd: float
    invocations: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    over_limit: bool
    blocked: bool                          # 本次评估后该用户是否处于封禁态
    action: str                            # "blocked" / "already-blocked" / "ok" / "skipped-not-user" / "dry-run-would-block"


class GuardResult(TypedDict):
    date: str
    daily_limit_usd: float
    dry_run: bool
    users: list[UserSpend]
    total_est_cost_usd: float
    blocked_users: list[str]


# ──────────────────────────────────────────────────────────────────────
#  依赖加载(与 daily_pipeline 一致的按路径加载，避免包结构耦合)
# ──────────────────────────────────────────────────────────────────────

def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块 {name} @ {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pricing() -> ModuleType:
    return _load("spend_guard_pricing", _HERE / "pricing.py")


def _logs_fetch() -> ModuleType:
    return _load("aws_logs_fetch", _FUNC_ROOT / "logs_fetch.py")


def _aws_session(cfg: GuardConfig):
    sess_mod = _load("aws_session", _FUNC_ROOT / "aws_session.py")
    return sess_mod.AwsSession(sess_mod.AwsSessionConfig(
        assume_role_arn=cfg["assume_role_arn"], region=cfg["region"],
    ))


# ──────────────────────────────────────────────────────────────────────
#  工具
# ──────────────────────────────────────────────────────────────────────

def _today_cst() -> str:
    return dt.datetime.now(CST).strftime("%Y-%m-%d")


def _user_name_from_arn(arn: str) -> str:
    """arn:aws:iam::502225588666:user/cursor-bedrock-user -> cursor-bedrock-user。
    非 user ARN(如 assumed-role)返回空串，表示无法作为 put_user_policy 目标。
    """
    if ":user/" not in arn:
        return ""
    return arn.split(":user/", 1)[1]


def _deny_policy_document() -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "SpendGuardDenyBedrock",
            "Effect": "Deny",
            "Action": _DENIED_ACTIONS,
            "Resource": "*",
        }],
    }


def _is_blocked(iam: Any, user_name: str) -> bool:
    """该用户是否已挂 SpendGuard 的 Deny inline policy。"""
    try:
        iam.get_user_policy(UserName=user_name, PolicyName=DENY_POLICY_NAME)
        return True
    except iam.exceptions.NoSuchEntityException:
        return False


def _block_user(iam: Any, user_name: str) -> None:
    iam.put_user_policy(
        UserName=user_name,
        PolicyName=DENY_POLICY_NAME,
        PolicyDocument=json.dumps(_deny_policy_document()),
    )
    logger.warning("SpendGuard 已封禁用户 %s(挂 %s Deny)", user_name, DENY_POLICY_NAME)


def _unblock_user(iam: Any, user_name: str) -> bool:
    try:
        iam.delete_user_policy(UserName=user_name, PolicyName=DENY_POLICY_NAME)
        logger.info("SpendGuard 已解除用户 %s 的封禁", user_name)
        return True
    except iam.exceptions.NoSuchEntityException:
        return False


# ──────────────────────────────────────────────────────────────────────
#  核心：评估今日花销并按需封禁
# ──────────────────────────────────────────────────────────────────────

def _aggregate_user_spend(cfg: GuardConfig, date: str, session: Any) -> dict[str, dict[str, Any]]:
    """从 Logs 聚合今日每个 IAM 用户的四类 token + 按模型估算成本。

    返回 {user_arn: {tokens..., invocations, est_cost_usd}}。
    用 caller_model_stats(每人×每模型)而非 totals_by_caller，因不同模型单价不同。
    """
    logs_mod = _logs_fetch()
    pricing = _pricing()
    usage = logs_mod.fetch_logs_usage(
        date, session,
        {"log_group": cfg["log_group"], "regions": cfg["regions"],
         "src_cache_dir": cfg["data_dir"] / "src_cache"},
        write_detail=False,  # 守卫只需聚合，避免多余的逐条查询/落盘开销
    )

    acc: dict[str, dict[str, Any]] = {}
    for st in usage["caller_model_stats"]:
        arn = st["caller"]
        if not arn or arn == "(unknown)":
            continue
        cost = pricing.estimate_cost_usd(
            st["model"], st["input_tokens"], st["cache_read_tokens"],
            st["cache_write_tokens"], st["output_tokens"],
        )
        cur = acc.setdefault(arn, {
            "invocations": 0, "input_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0,
        })
        cur["invocations"] += st["invocations"]
        cur["input_tokens"] += st["input_tokens"]
        cur["cache_read_tokens"] += st["cache_read_tokens"]
        cur["cache_write_tokens"] += st["cache_write_tokens"]
        cur["output_tokens"] += st["output_tokens"]
        cur["est_cost_usd"] += cost
    return acc


def evaluate(cfg: GuardConfig, date: Optional[str] = None, dry_run: bool = False) -> GuardResult:
    """评估今日各 IAM 用户估算花销，超阈值则挂 Deny(dry_run=True 只评估不动作)。

    幂等：已被封的用户不会重复挂策略(action=already-blocked)。
    降级：Logs 拉取失败会抛出，由调用方(调度/接口)记录，不静默吞。
    """
    date = date or _today_cst()
    limit = cfg["daily_limit_usd"]
    only = set(cfg.get("only_users") or [])

    session = _aws_session(cfg)
    spend = _aggregate_user_spend(cfg, date, session)

    iam = None if dry_run else session.client("iam")

    users: list[UserSpend] = []
    blocked_users: list[str] = []
    total = 0.0

    for arn, agg in sorted(spend.items(), key=lambda kv: -kv[1]["est_cost_usd"]):
        total += agg["est_cost_usd"]
        user_name = _user_name_from_arn(arn)
        over = agg["est_cost_usd"] > limit

        row: UserSpend = {
            "user_arn": arn,
            "user_name": user_name,
            "est_cost_usd": round(agg["est_cost_usd"], 4),
            "invocations": agg["invocations"],
            "input_tokens": agg["input_tokens"],
            "cache_read_tokens": agg["cache_read_tokens"],
            "cache_write_tokens": agg["cache_write_tokens"],
            "output_tokens": agg["output_tokens"],
            "over_limit": over,
            "blocked": False,
            "action": "ok",
        }

        # 非 user ARN(assumed-role 等)无法作为 put_user_policy 目标，跳过封禁但仍计入统计。
        if not user_name:
            row["action"] = "skipped-not-user"
            users.append(row)
            continue
        # 限定名单：不在名单内的用户只统计不封。
        if only and user_name not in only:
            row["action"] = "skipped-not-user"
            users.append(row)
            continue

        if not over:
            users.append(row)
            continue

        # 超限。
        if dry_run:
            row["action"] = "dry-run-would-block"
            users.append(row)
            continue

        if _is_blocked(iam, user_name):
            row["blocked"] = True
            row["action"] = "already-blocked"
        else:
            _block_user(iam, user_name)
            row["blocked"] = True
            row["action"] = "blocked"
        blocked_users.append(user_name)
        users.append(row)

    return {
        "date": date,
        "daily_limit_usd": limit,
        "dry_run": dry_run,
        "users": users,
        "total_est_cost_usd": round(total, 4),
        "blocked_users": blocked_users,
    }


def release_all(cfg: GuardConfig) -> dict[str, Any]:
    """解除所有(或名单内)用户的 SpendGuard 封禁。调度器次日 0 点调用 / 手动放开。

    只删自己挂的 ``SpendGuardDailyDeny`` inline policy，绝不动用户其它策略。
    仅在 only_users 非空时限定范围，否则遍历所有 IAM 用户找已封的。
    """
    session = _aws_session(cfg)
    iam = session.client("iam")
    only = list(cfg.get("only_users") or [])

    candidates: list[str]
    if only:
        candidates = only
    else:
        candidates = []
        paginator = iam.get_paginator("list_users")
        for page in paginator.paginate():
            for u in page.get("Users", []):
                candidates.append(u["UserName"])

    released: list[str] = []
    for name in candidates:
        if _unblock_user(iam, name):
            released.append(name)
    logger.info("SpendGuard 解除完成，共解除 %d 个用户: %s", len(released), released)
    return {"released": released, "count": len(released)}


def current_status(cfg: GuardConfig) -> dict[str, Any]:
    """读当前谁处于 SpendGuard 封禁态(按 inline policy 存在性判断)。"""
    session = _aws_session(cfg)
    iam = session.client("iam")
    only = list(cfg.get("only_users") or [])

    if only:
        candidates = only
    else:
        candidates = []
        paginator = iam.get_paginator("list_users")
        for page in paginator.paginate():
            for u in page.get("Users", []):
                candidates.append(u["UserName"])

    blocked = [name for name in candidates if _is_blocked(iam, name)]
    return {"blocked_users": blocked, "count": len(blocked), "deny_policy_name": DENY_POLICY_NAME}


# ──────────────────────────────────────────────────────────────────────
#  CLI 自检
# ──────────────────────────────────────────────────────────────────────

def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import os
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    p = argparse.ArgumentParser(description="Bedrock 每日花销守卫(按 IAM 用户估算+硬阻断)。")
    p.add_argument("cmd", choices=["evaluate", "release", "status"])
    p.add_argument("date", nargs="?", help="评估日期 YYYY-MM-DD(北京日)，默认今天")
    p.add_argument("--limit", type=float,
                   default=float(os.environ.get("AWS_SPEND_GUARD_DAILY_LIMIT_USD", "50")))
    p.add_argument("--users", default=os.environ.get("AWS_SPEND_GUARD_ONLY_USERS", ""),
                   help="逗号分隔的 IAM 用户名(限定生效范围)")
    p.add_argument("--role-arn", default=os.environ.get(
        "AWS_USAGE_REPORT_ASSUME_ROLE_ARN",
        "arn:aws:iam::502225588666:role/OrganizationAccountAccessRole"))
    p.add_argument("--region", default=os.environ.get("AWS_USAGE_REPORT_REGION", "us-east-1"))
    p.add_argument("--regions", default=os.environ.get("AWS_USAGE_REPORT_REGIONS", "us-east-1"))
    p.add_argument("--log-group", default=os.environ.get(
        "AWS_USAGE_REPORT_LOG_GROUP", "/bedrock/model-invocations"))
    p.add_argument("--data-dir", default="./_data")
    p.add_argument("--dry-run", action="store_true", help="只评估不封禁")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    cfg: GuardConfig = {
        "assume_role_arn": args.role_arn,
        "region": args.region,
        "regions": [r.strip() for r in args.regions.split(",") if r.strip()],
        "log_group": args.log_group,
        "daily_limit_usd": args.limit,
        "only_users": [u.strip() for u in args.users.split(",") if u.strip()],
        "data_dir": Path(args.data_dir),
    }

    if args.cmd == "evaluate":
        res = evaluate(cfg, args.date, dry_run=args.dry_run)
    elif args.cmd == "release":
        res = release_all(cfg)
    else:
        res = current_status(cfg)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
