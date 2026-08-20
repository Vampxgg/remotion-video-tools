# -*- coding: utf-8 -*-
"""每日消耗报告编排(纯逻辑，无 FastAPI 依赖)。

把三类数据源合并成一份"钱+量+质"三合一的每日报告：

1. 账单 CSV      —— 由 billing_fetch.ensure_daily_csv 拉取(触发导出+切分)。
2. calls JSON    —— 复用现有 Azure Functions 已导出到 blob(calls/ 前缀)的当天文件。
3. requests NDJSON —— 复用现有 Azure Functions 已导出到 blob(requests/ 前缀)的当天文件。

再调用 shared/usage_report.generate() 渲染 md/html/json，落盘到唯一数据根
_data/reports/<date>/，并可选回传 blob(usage/ 前缀)。

幂等与多 worker 安全：
- uvicorn --workers N 会每进程各起一个调度器，到点可能并发触发。用 <date>.lock
  文件做进程级互斥 + "当天已生成则跳过"，避免重复拉账单/重复上传。

所有落盘严格限制在 cfg['data_dir'] 子目录树内，不碰其它业务目录。

对外主入口：``run(date, cfg, upload=None) -> dict``。
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
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
CST = dt.timezone(dt.timedelta(hours=8))


class PipelineConfig(TypedDict):
    """run() 所需配置(由上层从 settings 组装)。billing 子配置见 BillingConfig。"""

    # billing_fetch.BillingConfig 的全部字段
    subscription_id: str
    resource_group: str
    storage_account: str
    blob_container: str
    export_name: str
    api_version: str
    daily_dir: Path
    poll_seconds: int
    poll_max: int
    skip_if_csv_exists: bool
    # pipeline 专属
    data_dir: Path
    calls_prefix: str
    requests_prefix: str
    out_prefix: str
    upload_blob: bool
    # calls/requests blob 文件名后缀(线上云函数为 UTC)；usage 回传后缀(北京日 CST)。
    src_suffix: str
    out_suffix: str


def _load_module_by_path(name: str, path: Path) -> ModuleType:
    """按文件路径加载模块，避开 static 目录非包导致的 import 路径问题。"""
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块 {name} @ {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _usage_report() -> ModuleType:
    """加载 shared/usage_report.py(含 generate())。"""
    return _load_module_by_path(
        "usage_report_core", _HERE / "shared" / "usage_report.py"
    )


def _billing() -> ModuleType:
    """加载同目录 billing_fetch.py(按文件路径，static 非包也可用)。"""
    return _load_module_by_path("billing_fetch", _HERE / "billing_fetch.py")


def _blob_service_client(cfg: PipelineConfig, credential):
    from azure.storage.blob import BlobServiceClient

    return BlobServiceClient(
        f"https://{cfg['storage_account']}.blob.core.windows.net",
        credential=credential,
    )


def _blob_base(prefix: str, date: str, stem: str, suffix: str) -> str:
    """还原云函数的 blob 命名: <prefix>/<YYYY>/<MM>/<stem>-<date>-<suffix>。

    调用方拼上扩展名。stem 为 calls / requests / usage；suffix 为 UTC / CST。

    时间口径说明(重要)：
    - 线上云函数导出的 calls/requests 文件名带 **-UTC** 后缀，其 <date> 为 **UTC 自然日**
      (实测 blob 内 JSON 的 date_utc 字段与文件名一致)。故下载时用 suffix="UTC"。
    - 本模块自身回传的 usage 报告沿用 -CST(北京日语义)，与 <date> 入参一致。
    """
    y, m, _ = date.split("-")
    return f"{prefix.strip('/')}/{y}/{m}/{stem}-{date}-{suffix}"


def _try_download(cfg: PipelineConfig, credential, blob_name: str, dest: Path) -> bool:
    """尝试把一个 blob 下载到本地 dest；不存在返回 False(降级用)。"""
    from azure.core.exceptions import ResourceNotFoundError

    bsc = _blob_service_client(cfg, credential)
    bc = bsc.get_container_client(cfg["blob_container"]).get_blob_client(blob_name)
    try:
        data = bc.download_blob().readall()
    except ResourceNotFoundError:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


def _download_calls_requests(
    cfg: PipelineConfig, credential, date: str
) -> tuple[str | None, str | None]:
    """从 blob 下载当天 calls JSON 与 requests NDJSON 到 src_cache/<date>/。

    缺失任一份返回对应 None(generate() 会降级并在报告中标注"数据缺失")。
    """
    cache = cfg["data_dir"] / "src_cache" / date
    calls_dest = cache / "calls.json"
    req_dest = cache / "requests.ndjson"

    calls_blob = _blob_base(cfg["calls_prefix"], date, "calls", cfg["src_suffix"]) + ".json"
    req_blob = _blob_base(cfg["requests_prefix"], date, "requests", cfg["src_suffix"]) + ".ndjson"

    calls_ok = _try_download(cfg, credential, calls_blob, calls_dest)
    if not calls_ok:
        logger.warning("blob 缺少当天 calls: %s(报告将标注调用次数数据缺失)", calls_blob)
    req_ok = _try_download(cfg, credential, req_blob, req_dest)
    if not req_ok:
        logger.warning("blob 缺少当天 requests: %s(报告将标注请求明细数据缺失)", req_blob)

    return (str(calls_dest) if calls_ok else None,
            str(req_dest) if req_ok else None)


def _upload_reports(
    cfg: PipelineConfig, credential, date: str, md: bytes, html: bytes, report_json: bytes
) -> None:
    """回传 md/html/json 到 blob(usage/ 前缀)，覆盖写。"""
    from azure.storage.blob import ContentSettings

    bsc = _blob_service_client(cfg, credential)
    cc = bsc.get_container_client(cfg["blob_container"])
    base = _blob_base(cfg["out_prefix"], date, "usage", cfg["out_suffix"])
    items = [
        (f"{base}.md", md, "text/markdown; charset=utf-8"),
        (f"{base}.html", html, "text/html; charset=utf-8"),
        (f"{base}.json", report_json, "application/json; charset=utf-8"),
    ]
    for name, data, ctype in items:
        cc.upload_blob(
            name=name,
            data=data,
            overwrite=True,
            content_settings=ContentSettings(content_type=ctype),
        )
        logger.info("已回传 blob: %s (%d bytes)", name, len(data))


def _report_dir(cfg: PipelineConfig, date: str) -> Path:
    return cfg["data_dir"] / "reports" / date


def report_paths(cfg: PipelineConfig, date: str) -> dict[str, Path]:
    """当天产物的本地路径(md/html/json)。"""
    d = _report_dir(cfg, date)
    return {
        "md": d / f"usage-{date}-CST.md",
        "html": d / f"usage-{date}-CST.html",
        "json": d / f"usage-{date}-CST.json",
    }


def is_generated(cfg: PipelineConfig, date: str) -> bool:
    """当天 md 与 html 均已存在则视为已生成(幂等判断)。"""
    p = report_paths(cfg, date)
    return p["md"].exists() and p["html"].exists()


def default_date() -> str:
    """默认目标日期 = 昨天(北京自然日)。"""
    return (dt.datetime.now(CST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")


def run(date: str, cfg: PipelineConfig, upload: bool | None = None) -> dict[str, Any]:
    """执行一次完整流水线：拉 CSV → 下 calls/requests → generate → 落盘 → 可选回传。

    Parameters
    ----------
    date: 目标日期 YYYY-MM-DD(北京自然日)。
    cfg: PipelineConfig。
    upload: 是否回传 blob；None 表示用 cfg['upload_blob']。

    Returns
    -------
    dict: {date, skipped, total_cost_usd, total_calls, total_tokens,
           has_csv/calls/requests, paths{md,html,json}, uploaded}。
    """
    do_upload = cfg["upload_blob"] if upload is None else upload
    out_dir = _report_dir(cfg, date)
    out_dir.mkdir(parents=True, exist_ok=True)

    lock = out_dir / ".lock"
    # 进程级互斥：O_CREAT|O_EXCL 原子创建；已被其它 worker 持有则直接返回"已在处理"。
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        # 陈旧锁(>1h)清理，避免异常退出后永久卡死。
        try:
            if time.time() - lock.stat().st_mtime > 3600:
                lock.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("当天 %s 已有流水线在处理(锁存在)，本次跳过。", date)
        return {"date": date, "skipped": True, "reason": "locked"}

    try:
        if is_generated(cfg, date) and cfg["skip_if_csv_exists"]:
            logger.info("当天 %s 报告已存在，跳过。", date)
            paths = report_paths(cfg, date)
            return {"date": date, "skipped": True, "reason": "exists",
                    "paths": {k: str(v) for k, v in paths.items()}}

        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()

        bf = _billing()
        billing_cfg = {
            "subscription_id": cfg["subscription_id"],
            "resource_group": cfg["resource_group"],
            "storage_account": cfg["storage_account"],
            "blob_container": cfg["blob_container"],
            "export_name": cfg["export_name"],
            "api_version": cfg["api_version"],
            "daily_dir": cfg["daily_dir"],
            "poll_seconds": cfg["poll_seconds"],
            "poll_max": cfg["poll_max"],
            "skip_if_csv_exists": cfg["skip_if_csv_exists"],
        }
        csv_path = bf.ensure_daily_csv(date, credential, billing_cfg)

        calls_path, req_path = _download_calls_requests(cfg, credential, date)

        ur = _usage_report()
        report, md, html = ur.generate(date, str(csv_path), calls_path, req_path)
        report_json = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")

        paths = report_paths(cfg, date)
        paths["md"].write_bytes(md)
        paths["html"].write_bytes(html)
        paths["json"].write_bytes(report_json)
        logger.info("报告已落盘: %s", out_dir)

        uploaded = False
        if do_upload:
            try:
                _upload_reports(cfg, credential, date, md, html, report_json)
                uploaded = True
            except Exception as e:  # 回传失败不影响本地产物可用
                logger.error("回传 blob 失败(本地产物已生成): %s", e, exc_info=True)

        return {
            "date": date,
            "skipped": False,
            "total_cost_usd": report.get("total_cost_usd"),
            "total_infra_cost_usd": report.get("total_infra_cost_usd"),
            "total_calls": report.get("total_calls"),
            "total_tokens": report.get("total_tokens"),
            "has_csv": report.get("has_csv"),
            "has_calls": report.get("has_calls"),
            "has_requests": report.get("has_requests"),
            "paths": {k: str(v) for k, v in paths.items()},
            "uploaded": uploaded,
        }
    finally:
        lock.unlink(missing_ok=True)
