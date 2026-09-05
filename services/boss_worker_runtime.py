# -*- coding: utf-8 -*-
"""BOSS Chrome worker 生命周期管理。

本模块只负责本机 Chrome 进程的启动、停止与运行快照，不参与 BOSS
业务数据解析。业务层在 worker 代理重租约后调用这里重启对应调试端口。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

import httpx


ProcessStarter = Callable[[str, List[str]], Any]
ProcessStopper = Callable[[int], None]
DevtoolsChecker = Callable[[str, int, float], bool]
ProcessFinder = Callable[["WorkerRuntimeConfig"], List[int]]


@dataclass(frozen=True)
class WorkerRuntimeConfig:
    worker_id: str
    browser_host_port: str
    profile_id: str

    @property
    def host(self) -> str:
        parsed = _split_host_port(self.browser_host_port)
        return parsed["host"]

    @property
    def port(self) -> int:
        parsed = _split_host_port(self.browser_host_port)
        return parsed["port"]


class BossWorkerRuntimeManager:
    """启动/停止单个 BOSS Chrome worker，并保存 PID 快照。"""

    def __init__(
        self,
        *,
        chrome_path: Optional[str] = None,
        profile_root: Path | str = "runtime/chrome-profiles",
        state_root: Path | str = "runtime/boss-workers",
        devtools_timeout: float = 10.0,
        process_starter: Optional[ProcessStarter] = None,
        process_stopper: Optional[ProcessStopper] = None,
        devtools_checker: Optional[DevtoolsChecker] = None,
        process_finder: Optional[ProcessFinder] = None,
    ) -> None:
        self._chrome_path = chrome_path or _find_chrome()
        self._profile_root = Path(profile_root)
        self._state_root = Path(state_root)
        self._devtools_timeout = devtools_timeout
        self._process_starter = process_starter or _start_process
        self._process_stopper = process_stopper or _stop_process
        self._devtools_checker = devtools_checker or _check_devtools
        self._process_finder = process_finder or self._find_processes_by_runtime

    @property
    def state_root(self) -> Path:
        return self._state_root

    @property
    def profile_root(self) -> Path:
        return self._profile_root

    def restart_worker(
        self,
        config: WorkerRuntimeConfig,
        *,
        proxy_id: Optional[str],
        chrome_proxy_server: Optional[str],
    ) -> Dict[str, Any]:
        self.stop_worker(config.worker_id, config=config)
        return self.start_worker(
            config,
            proxy_id=proxy_id,
            chrome_proxy_server=chrome_proxy_server,
        )

    def ensure_worker(
        self,
        config: WorkerRuntimeConfig,
        *,
        proxy_id: Optional[str],
        chrome_proxy_server: Optional[str],
    ) -> Dict[str, Any]:
        snapshot = self.snapshot(config.worker_id)
        if (
            snapshot.get("proxy_id") == proxy_id
            and snapshot.get("chrome_proxy_server") == chrome_proxy_server
            and self._devtools_checker(config.host, config.port, 1.0)
        ):
            snapshot["devtools_ok"] = True
            return snapshot
        return self.restart_worker(
            config,
            proxy_id=proxy_id,
            chrome_proxy_server=chrome_proxy_server,
        )

    def start_worker(
        self,
        config: WorkerRuntimeConfig,
        *,
        proxy_id: Optional[str],
        chrome_proxy_server: Optional[str],
    ) -> Dict[str, Any]:
        self._profile_root.mkdir(parents=True, exist_ok=True)
        self._state_root.mkdir(parents=True, exist_ok=True)
        profile_dir = self._profile_root / config.profile_id
        profile_dir.mkdir(parents=True, exist_ok=True)

        args = [
            f"--remote-debugging-address={config.host}",
            f"--remote-debugging-port={config.port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if chrome_proxy_server:
            args.append(f"--proxy-server={chrome_proxy_server}")

        process = self._process_starter(self._chrome_path, args)
        pid = int(getattr(process, "pid", 0) or 0)
        devtools_ok = self._devtools_checker(config.host, config.port, self._devtools_timeout)
        snapshot = {
            "worker_id": config.worker_id,
            "pid": pid,
            "browser_host_port": config.browser_host_port,
            "profile_id": config.profile_id,
            "proxy_id": proxy_id,
            "chrome_proxy_server": chrome_proxy_server,
            "devtools_ok": devtools_ok,
            "started_at": int(time.time()),
        }
        self._state_file(config.worker_id).write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return snapshot

    def stop_worker(self, worker_id: str, *, config: Optional[WorkerRuntimeConfig] = None) -> bool:
        state_file = self._state_file(worker_id)
        if not state_file.exists():
            if config is None:
                return False
            return self._stop_fallback_processes(config)
        try:
            snapshot = json.loads(state_file.read_text(encoding="utf-8-sig"))
        except Exception:
            if config is None:
                return False
            return self._stop_fallback_processes(config)
        pid = int(snapshot.get("pid") or 0)
        if pid <= 0:
            if config is None:
                return False
            return self._stop_fallback_processes(config)
        self._process_stopper(pid)
        return True

    def snapshot(self, worker_id: str) -> Dict[str, Any]:
        state_file = self._state_file(worker_id)
        if not state_file.exists():
            return {}
        try:
            return json.loads(state_file.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}

    def _state_file(self, worker_id: str) -> Path:
        return self._state_root / f"{worker_id}.json"

    def _stop_fallback_processes(self, config: WorkerRuntimeConfig) -> bool:
        stopped = False
        for pid in self._process_finder(config):
            if pid <= 0:
                continue
            self._process_stopper(pid)
            stopped = True
        return stopped

    def _find_processes_by_runtime(self, config: WorkerRuntimeConfig) -> List[int]:
        pids = set(_find_listening_pids(config.port))
        profile_dir = str((self._profile_root / config.profile_id).resolve())
        pids.update(_find_chrome_pids_by_profile(profile_dir))
        return sorted(pids)


def _split_host_port(value: str) -> Dict[str, Any]:
    parsed = urlsplit(f"//{value}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"browser_host_port must be host:port, got {value}")
    return {"host": parsed.hostname, "port": int(parsed.port)}


def _find_chrome() -> str:
    candidates = [
        os.environ.get("CHROME_PATH"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LocalAppData", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return "chrome.exe"


def _start_process(chrome_path: str, args: List[str]) -> subprocess.Popen:
    return subprocess.Popen([chrome_path, *args])


def _stop_process(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _find_listening_pids(port: int) -> List[int]:
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return []
    pids: List[int] = []
    marker = f":{port}"
    for line in result.stdout.splitlines():
        if marker not in line or "LISTENING" not in line.upper():
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pids.append(int(parts[-1]))
        except ValueError:
            continue
    return pids


def _find_chrome_pids_by_profile(profile_dir: str) -> List[int]:
    if os.name != "nt":
        return []
    escaped = profile_dir.replace("'", "''")
    command = (
        "$needle = '" + escaped + "'; "
        "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($needle) } | "
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


def _check_devtools(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.1, timeout)
    url = f"http://{host}:{port}/json/version"
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=1.0)
            if 200 <= resp.status_code < 400:
                return True
        except Exception:
            time.sleep(0.2)
    return False
