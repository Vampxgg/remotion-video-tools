# -*- coding: utf-8 -*-
"""Bedrock 每日花销守卫 router：近实时按 IAM 用户限额 + 硬阻断 + 次日自动解除。

根因背景：Bedrock 无原生"每日花销硬上限"，Cost Explorer 也无法按 IAM 用户拆成本。
故用 CloudWatch Logs 的 identity.arn + 四类 token × 单价**估算**每人今日花销，
超阈值就给该 IAM 用户挂 inline Deny(禁 bedrock 调用)，次日 0 点(北京)自动解除。
纯逻辑在 ``static/aws_cost_export_func/spend_guard/spend_guard.py``，本文件只做
调度接入 + 在线端点，复用 aws_usage_report 那套 AssumeRole/region/log_group 定位。

两个调度循环(仅当 AWS_SPEND_GUARD_ENABLE=True 时启动)：
- 轮询循环：每 AWS_SPEND_GUARD_POLL_MINUTES 分钟评估今日花销并按需封禁。
- 重置循环：每天北京 AWS_SPEND_GUARD_RESET_HHMM 解除前一日所有封禁。

端点(前缀 /api)：
- ``POST /api/aws-spend-guard/evaluate``  立即评估(body 可选 dry_run/date)，后台或同步
- ``POST /api/aws-spend-guard/release``   立即解除所有 SpendGuard 封禁
- ``GET  /api/aws-spend-guard/status``    当前谁被封 + 调度状态 + 最近一次评估
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.security import require_api_key
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/aws_usage/spend_guard.log")

router = APIRouter()

CST = dt.timezone(dt.timedelta(hours=8))

_GUARD_PATH = (
    Path(_settings.static_dir_abs) / "aws_cost_export_func" / "spend_guard" / "spend_guard.py"
)

_poll_task: Optional[asyncio.Task] = None
_reset_task: Optional[asyncio.Task] = None
_last_eval: dict[str, Any] = {}
_last_reset: dict[str, Any] = {}


# ══════════════════════════════════════════════════════════════════════
#  加载纯逻辑模块 + 组装 GuardConfig
# ══════════════════════════════════════════════════════════════════════

def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aws_spend_guard_core", str(_GUARD_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 spend_guard @ {_GUARD_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_cfg() -> dict[str, Any]:
    return {
        "assume_role_arn": _settings.AWS_USAGE_REPORT_ASSUME_ROLE_ARN,
        "region": _settings.AWS_USAGE_REPORT_REGION,
        "regions": [r.strip() for r in _settings.AWS_USAGE_REPORT_REGIONS.split(",") if r.strip()],
        "log_group": _settings.AWS_USAGE_REPORT_LOG_GROUP,
        "daily_limit_usd": _settings.AWS_SPEND_GUARD_DAILY_LIMIT_USD,
        "only_users": [u.strip() for u in _settings.AWS_SPEND_GUARD_ONLY_USERS.split(",") if u.strip()],
        "data_dir": _settings.aws_usage_report_data_dir_abs,
    }


def _evaluate_sync(date: Optional[str], dry_run: bool) -> dict[str, Any]:
    guard = _load_guard()
    return guard.evaluate(_build_cfg(), date=date, dry_run=dry_run)


def _release_sync() -> dict[str, Any]:
    guard = _load_guard()
    return guard.release_all(_build_cfg())


def _status_sync() -> dict[str, Any]:
    guard = _load_guard()
    return guard.current_status(_build_cfg())


async def _run(fn, *args) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


# ══════════════════════════════════════════════════════════════════════
#  调度：轮询封禁 + 次日重置
# ══════════════════════════════════════════════════════════════════════

def _seconds_until_next(hhmm: str) -> tuple[float, dt.datetime]:
    hh, mm = (int(x) for x in hhmm.split(":"))
    now = dt.datetime.now(CST)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds(), target


async def _poll_loop() -> None:
    interval = max(1, _settings.AWS_SPEND_GUARD_POLL_MINUTES) * 60
    logger.info("SpendGuard 轮询循环启动(每 %d 分钟评估一次，阈值 $%.2f/人/日)",
                _settings.AWS_SPEND_GUARD_POLL_MINUTES, _settings.AWS_SPEND_GUARD_DAILY_LIMIT_USD)
    while True:
        try:
            result = await _run(_evaluate_sync, None, False)
            _last_eval.clear()
            _last_eval.update({"at": dt.datetime.now(CST).isoformat(), **result})
            if result.get("blocked_users"):
                logger.warning("SpendGuard 本轮封禁: %s", result["blocked_users"])
        except Exception as e:
            _last_eval.clear()
            _last_eval.update({"at": dt.datetime.now(CST).isoformat(), "error": str(e)})
            logger.error("SpendGuard 评估失败: %s", e, exc_info=True)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("SpendGuard 轮询循环收到取消信号，退出。")
            raise


async def _reset_loop() -> None:
    hhmm = _settings.AWS_SPEND_GUARD_RESET_HHMM
    logger.info("SpendGuard 重置循环启动(每天北京 %s 解除前一日封禁)", hhmm)
    while True:
        wait_s, target = _seconds_until_next(hhmm)
        logger.info("下一次 SpendGuard 重置: %s(%.0f 秒后)", target.isoformat(), wait_s)
        try:
            await asyncio.sleep(wait_s)
        except asyncio.CancelledError:
            logger.info("SpendGuard 重置循环收到取消信号，退出。")
            raise
        try:
            result = await _run(_release_sync)
            _last_reset.clear()
            _last_reset.update({"at": dt.datetime.now(CST).isoformat(), **result})
            logger.info("SpendGuard 次日重置完成: %s", result)
        except Exception as e:
            _last_reset.clear()
            _last_reset.update({"at": dt.datetime.now(CST).isoformat(), "error": str(e)})
            logger.error("SpendGuard 重置失败: %s", e, exc_info=True)


@asynccontextmanager
async def lifespan_resources(app):
    global _poll_task, _reset_task
    if _settings.AWS_SPEND_GUARD_ENABLE:
        _poll_task = asyncio.create_task(_poll_loop())
        _reset_task = asyncio.create_task(_reset_loop())
        logger.info("aws_spend_guard router 就绪(守卫已开启)")
    else:
        logger.info("aws_spend_guard router 就绪(守卫未开启，AWS_SPEND_GUARD_ENABLE=False)")
    try:
        yield
    finally:
        logger.info("aws_spend_guard router 正在关闭 …")
        for t in (_poll_task, _reset_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        _poll_task = None
        _reset_task = None


# ══════════════════════════════════════════════════════════════════════
#  端点
# ══════════════════════════════════════════════════════════════════════

class EvaluateRequest(BaseModel):
    date: Optional[str] = Field(None, description="评估日期 YYYY-MM-DD(北京日)；留空=今天。")
    dry_run: bool = Field(False, description="只评估不封禁(试算)。")
    wait: bool = Field(True, description="同步等待返回评估结果(默认 True)。")


@router.post("/aws-spend-guard/evaluate", summary="立即评估今日各用户花销并按需封禁",
             dependencies=[Depends(require_api_key("AWS_SPEND_GUARD_API_KEY"))])
async def evaluate_now(req: EvaluateRequest):
    if req.wait:
        try:
            result = await _run(_evaluate_sync, req.date, req.dry_run)
        except Exception as e:
            logger.error("手动评估失败: %s", e, exc_info=True)
            raise HTTPException(status_code=502, detail=f"评估失败: {e}")
        if not req.dry_run:
            _last_eval.clear()
            _last_eval.update({"at": dt.datetime.now(CST).isoformat(), **result})
        return create_standard_response(data=result, message="评估完成")

    async def _bg() -> None:
        try:
            result = await _run(_evaluate_sync, req.date, req.dry_run)
            if not req.dry_run:
                _last_eval.clear()
                _last_eval.update({"at": dt.datetime.now(CST).isoformat(), **result})
        except Exception as e:
            logger.error("后台评估失败: %s", e, exc_info=True)

    asyncio.create_task(_bg())
    return create_standard_response(data={"accepted": True}, code=202, message="已受理，后台评估中")


@router.post("/aws-spend-guard/release", summary="立即解除所有 SpendGuard 封禁",
             dependencies=[Depends(require_api_key("AWS_SPEND_GUARD_API_KEY"))])
async def release_now():
    try:
        result = await _run(_release_sync)
    except Exception as e:
        logger.error("手动解除失败: %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail=f"解除失败: {e}")
    _last_reset.clear()
    _last_reset.update({"at": dt.datetime.now(CST).isoformat(), "triggered": "manual", **result})
    return create_standard_response(data=result, message="解除完成")


@router.get("/aws-spend-guard/status", summary="当前封禁状态与调度状态")
async def guard_status():
    try:
        blocked = await _run(_status_sync)
    except Exception as e:
        logger.error("查询封禁状态失败: %s", e, exc_info=True)
        blocked = {"error": str(e)}
    return create_standard_response(data={
        "guard_enabled": _settings.AWS_SPEND_GUARD_ENABLE,
        "daily_limit_usd": _settings.AWS_SPEND_GUARD_DAILY_LIMIT_USD,
        "only_users": _settings.AWS_SPEND_GUARD_ONLY_USERS,
        "poll_minutes": _settings.AWS_SPEND_GUARD_POLL_MINUTES,
        "reset_hhmm_cst": _settings.AWS_SPEND_GUARD_RESET_HHMM,
        "poll_running": _poll_task is not None and not _poll_task.done(),
        "reset_running": _reset_task is not None and not _reset_task.done(),
        "currently_blocked": blocked,
        "last_eval": _last_eval or None,
        "last_reset": _last_reset or None,
    })
