# -*- coding: utf-8 -*-
"""BOSS Chrome worker 启动编排（reconcile）。

在 boss_server 单进程启动时调用，以 ``secrets/boss-workers.json`` 为唯一真相，
自动完成：

1. 清理：杀掉 DrissionPage 自起的临时 profile 浏览器；删除配置里已不存在的
   孤儿运行态状态文件（如从 3 个 worker 缩到 2 个后残留的 ``boss-c.json``）。
2. 对齐：遍历配置里的每个 worker，逐个 ``ensure_worker``——端口已在监听且代理
   一致就复用，否则用绝对路径重启 Chrome。
3. 验证：每个 worker 打开 ``https://httpbin.org/ip`` 校验出口 IP，把结果写进
   report 与日志，用户可直观核对出口是否正确；随后把页面停在 BOSS 搜索首页。

这样一条 ``uvicorn boss_server:app --workers 1`` 命令即可完成清理+对齐+验证，
不再需要手动 kill / 跑 ps1 / 查端口。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.boss_worker_runtime import WorkerRuntimeConfig
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/boss/reconciler.log")

# BOSS 搜索首页（park 页面用），与 boss_zhipin_client.SEARCH_PAGE_URL 一致
_BOSS_SEARCH_PAGE_URL = "https://www.zhipin.com/web/geek/jobs"
_EGRESS_CHECK_URL = "https://httpbin.org/ip"


def reconcile_workers(workers: List[Any], manager: Any) -> Dict[str, Any]:
    """按配置对齐 BOSS Chrome worker，返回启动 report。

    :param workers: 已装配的 worker 列表（每个含 worker_id/profile_id/proxy_id/
        chrome_proxy_server/browser_host_port）。
    :param manager: ``BossWorkerRuntimeManager`` 实例。
    """
    configured_ids = {_worker_id(w) for w in workers}

    # ── 1. 清理临时浏览器 ──
    temp_pids = _find_temp_drissionpage_pids()
    for pid in temp_pids:
        logger.warning("清理 DrissionPage 临时浏览器 pid=%s", pid)
        _stop_pid(pid)

    # ── 1b. 清理孤儿状态文件 ──
    _clean_orphan_state_files(manager, configured_ids)

    # ── 2 + 3. 逐 worker 对齐并验证 ──
    results: List[Dict[str, Any]] = []
    started = 0
    for worker in workers:
        worker_id = _worker_id(worker)
        host_port = _host_port(worker)
        proxy_id = getattr(worker, "proxy_id", None)
        chrome_proxy_server = getattr(worker, "chrome_proxy_server", None)
        config = WorkerRuntimeConfig(
            worker_id=worker_id,
            browser_host_port=host_port,
            profile_id=getattr(worker, "profile_id", worker_id),
        )
        snapshot: Dict[str, Any] = {}
        ensure_error: Optional[str] = None
        try:
            snapshot = manager.ensure_worker(
                config,
                proxy_id=proxy_id,
                chrome_proxy_server=chrome_proxy_server,
            )
            started += 1
        except Exception as exc:  # 单个 worker 起不来不影响其它
            ensure_error = str(exc)
            logger.error("worker=%s ensure 失败: %s", worker_id, exc)

        egress_ip = _probe_egress_ip(host_port) if not ensure_error else None
        ok = bool(egress_ip)
        results.append(
            {
                "worker_id": worker_id,
                "browser_host_port": host_port,
                "proxy_id": proxy_id,
                "chrome_pid": snapshot.get("pid") if snapshot else None,
                "egress_ip": egress_ip,
                "ok": ok,
                "error": ensure_error,
            }
        )
        logger.info(
            "worker=%s port=%s proxy_id=%s egress_ip=%s ok=%s",
            worker_id,
            host_port,
            proxy_id,
            egress_ip,
            ok,
        )

    report = {
        "expected": len(workers),
        "started": started,
        "workers": results,
    }
    logger.info("BOSS worker reconcile 完成: expected=%s started=%s", len(workers), started)
    return report


# ══════════════════════════════════════════════════════════════════════
#  内部工具
# ══════════════════════════════════════════════════════════════════════


def _worker_id(worker: Any) -> str:
    return getattr(worker, "worker_id", None) or "default"


def _host_port(worker: Any) -> str:
    return (
        getattr(worker, "browser_host_port", None)
        or getattr(worker, "_browser_host_port", None)
        or _settings.BOSS_ZHIPIN_BROWSER_HOST_PORT
    )


def _clean_orphan_state_files(manager: Any, configured_ids: set) -> None:
    state_root = getattr(manager, "state_root", None)
    if state_root is None:
        return
    state_root = Path(state_root)
    if not state_root.exists():
        return
    for state_file in state_root.glob("*.json"):
        if state_file.stem not in configured_ids:
            logger.warning("清理孤儿 worker 状态文件: %s", state_file.name)
            try:
                state_file.unlink()
            except OSError as exc:
                logger.error("删除孤儿状态文件失败 %s: %s", state_file.name, exc)


def _probe_egress_ip(host_port: str) -> Optional[str]:
    """连接 worker 的 Chrome，打开 httpbin 校验出口 IP，并把页面停在搜索首页。

    连不上（端口未起/未监听）返回 None。绝不自起临时浏览器（走 existing_only）。
    """
    try:
        from services.browser_connect import connect_existing

        page = connect_existing(host_port).new_tab()
    except Exception as exc:
        logger.error("worker %s 连接 Chrome 失败: %s", host_port, exc)
        return None

    egress_ip: Optional[str] = None
    try:
        page.get(_EGRESS_CHECK_URL)
        text = page.run_js('return document.body ? document.body.innerText : "";') or ""
        egress_ip = _extract_origin(text)
    except Exception as exc:
        logger.error("worker %s 出口探测失败: %s", host_port, exc)
    finally:
        # 停回搜索首页，供采集直接复用
        try:
            page.get(_BOSS_SEARCH_PAGE_URL)
        except Exception:
            pass
        try:
            page.close()
        except Exception:
            pass
    return egress_ip


def _extract_origin(text: str) -> Optional[str]:
    """从 httpbin.org/ip 的 JSON 文本里取 origin IP。"""
    import json

    try:
        data = json.loads(text)
        origin = data.get("origin")
        if origin:
            # 可能是 "1.2.3.4, 5.6.7.8"，取第一个
            return origin.split(",")[0].strip()
    except Exception:
        pass
    return None


def _find_temp_drissionpage_pids() -> List[int]:
    """查找所有 --user-data-dir 落在 Temp\\DrissionPage\\userData 的 chrome.exe。"""
    if os.name != "nt":
        return []
    command = (
        "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -match 'DrissionPage\\\\userData' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return []
    pids: List[int] = []
    for line in result.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def _stop_pid(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.kill(pid, 15)
    except ProcessLookupError:
        pass
