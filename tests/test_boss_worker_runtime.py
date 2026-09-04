# -*- coding: utf-8 -*-
"""BOSS Chrome worker 生命周期管理测试。"""

import json

from services.boss_worker_runtime import BossWorkerRuntimeManager, WorkerRuntimeConfig


class _FakeProcess:
    def __init__(self, pid):
        self.pid = pid


def test_runtime_manager_restarts_worker_with_proxy_and_writes_pid_snapshot(tmp_path):
    started = []
    stopped = []

    def start_process(chrome_path, args):
        started.append((chrome_path, args))
        return _FakeProcess(4321)

    manager = BossWorkerRuntimeManager(
        chrome_path="chrome.exe",
        profile_root=tmp_path / "profiles",
        state_root=tmp_path / "state",
        process_starter=start_process,
        process_stopper=stopped.append,
        devtools_checker=lambda host, port, timeout: True,
        process_finder=lambda runtime_config: [],
    )
    config = WorkerRuntimeConfig(
        worker_id="boss-a",
        browser_host_port="127.0.0.1:9527",
        profile_id="account-a",
    )

    snapshot = manager.restart_worker(
        config,
        proxy_id="boss-proxy-004",
        chrome_proxy_server="http=127.0.0.1:18084;https=127.0.0.1:18084",
    )

    assert snapshot["worker_id"] == "boss-a"
    assert snapshot["pid"] == 4321
    assert snapshot["proxy_id"] == "boss-proxy-004"
    assert snapshot["devtools_ok"] is True
    assert started[0][0] == "chrome.exe"
    assert "--remote-debugging-port=9527" in started[0][1]
    assert "--user-data-dir=" + str(tmp_path / "profiles" / "account-a") in started[0][1]
    assert "--proxy-server=http=127.0.0.1:18084;https=127.0.0.1:18084" in started[0][1]

    state_file = tmp_path / "state" / "boss-a.json"
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["pid"] == 4321
    assert saved["proxy_id"] == "boss-proxy-004"


def test_runtime_manager_stops_existing_worker_before_restart(tmp_path):
    stopped = []
    start_count = 0

    def start_process(chrome_path, args):
        nonlocal start_count
        start_count += 1
        return _FakeProcess(1000 + start_count)

    manager = BossWorkerRuntimeManager(
        chrome_path="chrome.exe",
        profile_root=tmp_path / "profiles",
        state_root=tmp_path / "state",
        process_starter=start_process,
        process_stopper=stopped.append,
        devtools_checker=lambda host, port, timeout: True,
        process_finder=lambda runtime_config: [],
    )
    config = WorkerRuntimeConfig(
        worker_id="boss-a",
        browser_host_port="127.0.0.1:9527",
        profile_id="account-a",
    )

    manager.restart_worker(config, proxy_id="boss-proxy-001", chrome_proxy_server=None)
    manager.restart_worker(config, proxy_id="boss-proxy-002", chrome_proxy_server=None)

    assert stopped == [1001]
    saved = json.loads((tmp_path / "state" / "boss-a.json").read_text(encoding="utf-8"))
    assert saved["pid"] == 1002
    assert saved["proxy_id"] == "boss-proxy-002"


def test_runtime_manager_falls_back_to_port_or_profile_when_state_missing(tmp_path):
    stopped = []
    config = WorkerRuntimeConfig(
        worker_id="boss-a",
        browser_host_port="127.0.0.1:9527",
        profile_id="account-a",
    )

    manager = BossWorkerRuntimeManager(
        chrome_path="chrome.exe",
        profile_root=tmp_path / "profiles",
        state_root=tmp_path / "state",
        process_stopper=stopped.append,
        process_finder=lambda runtime_config: [2222],
    )

    assert manager.stop_worker("boss-a", config=config) is True
    assert stopped == [2222]
