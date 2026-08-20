# -*- coding: utf-8 -*-
"""Azure Functions v2 编程模型入口(单文件 + 装饰器)。

包含两个 Timer(每天北京时间 09:00 = UTC 01:00):
- daily_calls_export:    查 Monitor ModelRequests 指标 → calls/ 前缀 blob(JSON+MD)
- daily_requests_export: 查 Log Analytics 诊断日志 → requests/ 前缀 blob(NDJSON+MD)

核心逻辑仍在 shared/calls_report.py 与 shared/requests_report.py，本文件只做编排。
"""

import datetime as dt
import logging
import os
import sys

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

# v2 worker 索引 function_app.py 时不保证把应用根目录加入 sys.path，
# 导致 `from shared import ...` 报 ModuleNotFoundError。显式把本文件所在目录
# (应用根)加入 sys.path，确保 shared 包可被导入。
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

from shared import calls_report, requests_report

app = func.FunctionApp()


def _target_date() -> str:
    """默认导出昨天(北京时间)。支持用 App Setting COST_EXPORT_DATE 手动补跑指定日期。

    触发时刻是 UTC 01:00(北京 09:00)，此时北京已进入新的一天，导出"北京昨天"。
    date_str 语义为北京自然日，下游 report 会据此换算 UTC 查询窗口。
    """
    override = os.environ.get("COST_EXPORT_DATE", "").strip()
    if override:
        return override
    cst = dt.timezone(dt.timedelta(hours=8))
    return (dt.datetime.now(cst) - dt.timedelta(days=1)).strftime("%Y-%m-%d")


def _blob_container():
    account = os.environ["COST_STORAGE_ACCOUNT"]
    container = os.environ.get("COST_BLOB_CONTAINER", "cost-exports")
    bsc = BlobServiceClient(
        f"https://{account}.blob.core.windows.net", credential=DefaultAzureCredential()
    )
    return bsc.get_container_client(container)


def _upload(container_client, blob_name: str, data: bytes, content_type: str):
    container_client.upload_blob(
        name=blob_name,
        data=data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )
    logging.info("已写入 blob: %s (%d bytes)", blob_name, len(data))


@app.timer_trigger(
    schedule="0 0 1 * * *", arg_name="timer", run_on_startup=False, use_monitor=True
)
def daily_calls_export(timer: func.TimerRequest) -> None:
    date_str = _target_date()
    logging.info("开始导出调用次数报告, 目标日期(北京)=%s", date_str)

    cfg = calls_report.get_config()
    credential = DefaultAzureCredential()
    json_bytes, md_bytes, agg = calls_report.build_reports(credential, cfg, date_str)
    logging.info("聚合完成: 总调用 %s 次, 模型 %d 个",
                 agg["grand_total"], len(agg["model_total"]))
    if agg["errors"]:
        logging.warning("部分账户查询失败: %s", agg["errors"])

    prefix = os.environ.get("COST_BLOB_PREFIX", "calls").strip("/")
    y, m, _ = date_str.split("-")
    base = f"{prefix}/{y}/{m}/calls-{date_str}-CST"

    cc = _blob_container()
    _upload(cc, f"{base}.json", json_bytes, "application/json; charset=utf-8")
    _upload(cc, f"{base}.md", md_bytes, "text/markdown; charset=utf-8")
    logging.info("导出完成: %s.{json,md}", base)


@app.timer_trigger(
    schedule="0 0 1 * * *", arg_name="timer", run_on_startup=False, use_monitor=True
)
def daily_requests_export(timer: func.TimerRequest) -> None:
    date_str = _target_date()
    logging.info("开始导出单请求明细, 目标日期(北京)=%s", date_str)

    cfg = requests_report.get_config()
    credential = DefaultAzureCredential()
    ndjson_bytes, md_bytes, agg, row_count = requests_report.build_reports(
        credential, cfg, date_str
    )
    logging.info("拉取完成: 请求 %s 条, 原始日志行 %s, 模型 %d 个",
                 agg["grand_calls"], row_count, len(agg["model_calls"]))

    prefix = os.environ.get("COST_REQUESTS_PREFIX", "requests").strip("/")
    y, m, _ = date_str.split("-")
    base = f"{prefix}/{y}/{m}/requests-{date_str}-CST"

    cc = _blob_container()
    _upload(cc, f"{base}.ndjson", ndjson_bytes, "application/x-ndjson; charset=utf-8")
    _upload(cc, f"{base}.md", md_bytes, "text/markdown; charset=utf-8")
    logging.info("导出完成: %s.{ndjson,md}", base)
