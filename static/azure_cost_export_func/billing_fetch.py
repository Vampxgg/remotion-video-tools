# -*- coding: utf-8 -*-
"""Azure Cost Management 账单 CSV 拉取(无人值守版)。

由桌面工具 refresh_billing.py 改造而来，用于在 Linux 服务器/FastAPI 进程内无人值守
运行，与交互式 `az login` 彻底解耦：

- ARM 侧(触发按需导出 + 轮询 runHistory + 列容器)改用 REST(标准库 urllib) +
  ``DefaultAzureCredential`` 取 ARM 令牌(生产走服务主体环境变量)。
- 数据面(下载 blob CSV)改用 ``azure-storage-blob`` SDK + ``DefaultAzureCredential``，
  不再写死存储账户密钥。
- 结果按 date 列切分，写到 ``static/azure_cost_export_func/_data/daily_csv/<date>.csv``，
  覆盖同名文件(保证拿到该天最新值)。

时间口径：Cost Management 导出的 date 列为形如 ``08/19/2026`` 的本地自然日字符串，
切分后文件名归一为 ``YYYY-MM-DD.csv``，与上层"北京自然日"口径一致。

对外主入口：``ensure_daily_csv(date, credential, cfg) -> pathlib.Path``。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

_SSL_CTX = ssl.create_default_context()
_ARM = "https://management.azure.com"
_ARM_SCOPE = "https://management.azure.com/.default"


class BillingConfig(TypedDict):
    """billing_fetch 所需的配置切片(由上层从 settings 组装)。"""

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


def _arm_token(credential) -> str:
    """用 DefaultAzureCredential 取 ARM 访问令牌。

    credential 通常为 azure.identity.DefaultAzureCredential 实例；生产环境下它会
    从 AZURE_CLIENT_ID/AZURE_TENANT_ID/AZURE_CLIENT_SECRET 环境变量识别服务主体。
    """
    return credential.get_token(_ARM_SCOPE).token


def _arm_request(
    method: str,
    url: str,
    token: str,
    body: Any | None = None,
    retries: int = 5,
) -> tuple[int, dict[str, Any]]:
    """对 ARM REST 端点发一次请求，带 429/网络抖动重试。返回 (status, json)。"""
    data = json.dumps(body).encode() if body is not None else None
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=90, context=_SSL_CTX) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            try:
                return e.code, json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                return e.code, {"raw": raw}
        except (urllib.error.URLError, ConnectionError, ssl.SSLError, TimeoutError) as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"ARM 网络请求多次失败({url}): {last_err}")


def _find_export(cfg: BillingConfig, token: str) -> tuple[str, str, str]:
    """确认导出任务名，返回 (name, container, root_folder_path)。

    优先匹配 cfg['export_name']；找不到则回退订阅下第一个导出。
    """
    url = (
        f"{_ARM}/subscriptions/{cfg['subscription_id']}"
        f"/providers/Microsoft.CostManagement/exports?api-version={cfg['api_version']}"
    )
    status, resp = _arm_request("GET", url, token)
    if status != 200:
        raise RuntimeError(f"列出 Cost Management 导出失败 HTTP {status}: {resp}")
    exports = resp.get("value", [])
    if not exports:
        raise RuntimeError("该订阅下没有任何 Cost Management 导出任务。")
    chosen = None
    if cfg["export_name"]:
        for e in exports:
            if e.get("name") == cfg["export_name"]:
                chosen = e
                break
    if chosen is None:
        chosen = exports[0]
    dest = chosen["properties"]["deliveryInfo"]["destination"]
    return (
        chosen["name"],
        dest.get("container", cfg["blob_container"]),
        dest.get("rootFolderPath", ""),
    )


def _trigger_run(cfg: BillingConfig, token: str, name: str) -> bool:
    """触发一次按需导出(run-now)。返回是否成功触发。"""
    url = (
        f"{_ARM}/subscriptions/{cfg['subscription_id']}"
        f"/providers/Microsoft.CostManagement/exports/{name}/run"
        f"?api-version={cfg['api_version']}"
    )
    status, resp = _arm_request("POST", url, token)
    if status not in (200, 202, 204):
        logger.warning("触发按需导出返回 HTTP %s: %s(将回退到现有最新文件)", status, resp)
        return False
    logger.info("已触发按需导出，等待生成…")
    return True


def _wait_latest_run(cfg: BillingConfig, token: str, name: str) -> str | None:
    """轮询 runHistory，返回最新一次 Completed 的 fileName；超时返回 None。"""
    url = (
        f"{_ARM}/subscriptions/{cfg['subscription_id']}"
        f"/providers/Microsoft.CostManagement/exports/{name}/runHistory"
        f"?api-version={cfg['api_version']}"
    )
    for i in range(cfg["poll_max"]):
        status, resp = _arm_request("GET", url, token)
        runs = resp.get("value", []) if status == 200 else []
        runs.sort(key=lambda r: r["properties"].get("submittedTime") or "", reverse=True)
        top = runs[0] if runs else None
        if top:
            st = top["properties"].get("status")
            fn = top["properties"].get("fileName")
            logger.info("导出轮询 [%d/%d] status=%s", i + 1, cfg["poll_max"], st)
            if st == "Completed" and fn:
                return fn
            if st == "Failed":
                raise RuntimeError("Cost Management 导出运行失败(Failed)。")
        time.sleep(cfg["poll_seconds"])
    logger.warning("等待导出完成超时，回退到容器现有最新文件。")
    return None


def _blob_service_client(cfg: BillingConfig, credential):
    """构造 BlobServiceClient(数据面用 DefaultAzureCredential，不用账户密钥)。"""
    from azure.storage.blob import BlobServiceClient

    return BlobServiceClient(
        f"https://{cfg['storage_account']}.blob.core.windows.net",
        credential=credential,
    )


def _latest_blob(cfg: BillingConfig, credential, container: str, prefix: str) -> str:
    """在容器 prefix 下取最新一份 .csv 的 blob 名(按 last_modified)。"""
    bsc = _blob_service_client(cfg, credential)
    cc = bsc.get_container_client(container)
    items = [
        b for b in cc.list_blobs(name_starts_with=prefix) if b.name.endswith(".csv")
    ]
    if not items:
        raise RuntimeError(f"容器 {container}/{prefix} 下没有找到 CSV。")
    items.sort(key=lambda b: b.last_modified, reverse=True)
    return items[0].name


def _download_blob(cfg: BillingConfig, credential, container: str, name: str) -> bytes:
    """下载指定 blob 的原始字节。"""
    bsc = _blob_service_client(cfg, credential)
    bc = bsc.get_container_client(container).get_blob_client(name)
    return bc.download_blob().readall()


def _split_and_write(raw_bytes: bytes, daily_dir: Path) -> list[tuple[str, int, float]]:
    """按 date 列拆分整份导出 CSV，逐天覆盖写入 daily_dir/<YYYY-MM-DD>.csv。

    返回每天 (文件名, 行数, 该天 costInUsd 合计) 的汇总列表。
    """
    text = raw_bytes.decode("utf-8-sig", "replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise RuntimeError("下载的账单 CSV 为空。")
    fields = list(rows[0].keys())
    by_day: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by_day[(r.get("date") or "").strip()].append(r)

    daily_dir.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, int, float]] = []
    for day, drows in sorted(by_day.items()):
        if not day:
            continue
        # 08/19/2026 -> 2026-08-19；无法解析时做安全替换避免整体失败。
        try:
            mm, dd, yyyy = day.split("/")
            fname = f"{yyyy}-{mm}-{dd}.csv"
        except ValueError:
            safe = day
            for ch in '\\/:*?"<>|':
                safe = safe.replace(ch, "-")
            fname = f"{safe}.csv"
        path = daily_dir / fname
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(drows)
        cost = 0.0
        for r in drows:
            try:
                cost += float(r.get("costInUsd") or 0)
            except (TypeError, ValueError):
                pass
        written.append((fname, len(drows), cost))
    return written


def ensure_daily_csv(date: str, credential, cfg: BillingConfig) -> Path:
    """确保 daily_dir 下存在 <date>.csv，返回其路径。

    流程：(可选)触发按需导出 + 轮询 → 下载整份导出 → 按天切分覆盖写。
    若 cfg['skip_if_csv_exists'] 且目标文件已存在则直接返回(幂等/省时)。

    Parameters
    ----------
    date: 目标日期 YYYY-MM-DD(北京自然日语义，与切分后文件名对齐)。
    credential: DefaultAzureCredential 实例，用于 ARM 令牌与 blob 数据面。
    cfg: BillingConfig。
    """
    target = cfg["daily_dir"] / f"{date}.csv"
    if cfg["skip_if_csv_exists"] and target.exists():
        logger.info("当天 CSV 已存在，跳过拉取: %s", target)
        return target

    token = _arm_token(credential)
    name, container, root = _find_export(cfg, token)
    prefix = f"{root}/{name}/" if root else f"{name}/"
    logger.info("定位导出: export=%s container=%s prefix=%s", name, container, prefix)

    file_name = None
    if _trigger_run(cfg, token, name):
        file_name = _wait_latest_run(cfg, token, name)
    if not file_name:
        file_name = _latest_blob(cfg, credential, container, prefix)
    logger.info("最新账单文件: %s", file_name)

    raw = _download_blob(cfg, credential, container, file_name)
    logger.info("已下载账单原始导出: %d bytes", len(raw))

    written = _split_and_write(raw, cfg["daily_dir"])
    for fname, n, cost in written:
        logger.info("切分 %s: %d 行, $%.2f", fname, n, cost)

    if not target.exists():
        raise RuntimeError(
            f"账单导出中未包含目标日期 {date} 的数据(可能当天尚无消费或导出未覆盖该日)。"
        )
    return target
