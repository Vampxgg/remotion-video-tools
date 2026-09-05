# -*- coding: utf-8 -*-
"""boss_worker_reconciler 编排器测试。

覆盖：清理孤儿状态文件与临时浏览器、按配置动态 ensure、httpbin 出口验证、report 汇总。
全程 mock，不真起 Chrome、不发真实网络。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _FakeWorker:
    def __init__(self, worker_id, host_port, profile_id, proxy_id, chrome_proxy_server):
        self.worker_id = worker_id
        self._browser_host_port = host_port
        self.browser_host_port = host_port
        self.profile_id = profile_id
        self.proxy_id = proxy_id
        self.chrome_proxy_server = chrome_proxy_server


class _FakeManager:
    """记录 ensure/stop 调用，不真起 Chrome。"""

    def __init__(self, state_root, profile_root):
        self._state_root = Path(state_root)
        self._profile_root = Path(profile_root)
        self.ensured = []
        self.stopped = []

    # 编排器会用到的属性/方法
    @property
    def state_root(self):
        return self._state_root

    @property
    def profile_root(self):
        return self._profile_root

    def ensure_worker(self, config, *, proxy_id, chrome_proxy_server):
        self.ensured.append(config.worker_id)
        return {
            "worker_id": config.worker_id,
            "pid": 1000 + len(self.ensured),
            "browser_host_port": config.browser_host_port,
            "profile_id": config.profile_id,
            "proxy_id": proxy_id,
            "chrome_proxy_server": chrome_proxy_server,
            "devtools_ok": True,
        }

    def stop_pid(self, pid):
        self.stopped.append(pid)


def test_reconcile_cleans_orphan_state_and_ensures_configured_workers(tmp_path, monkeypatch):
    from services import boss_worker_reconciler as rec

    state_root = tmp_path / "boss-workers"
    profile_root = tmp_path / "chrome-profiles"
    state_root.mkdir(parents=True)
    profile_root.mkdir(parents=True)

    # 制造孤儿状态文件 boss-c（配置里已删除）
    (state_root / "boss-a.json").write_text("{}", encoding="utf-8")
    (state_root / "boss-b.json").write_text("{}", encoding="utf-8")
    (state_root / "boss-c.json").write_text('{"pid": 30764}', encoding="utf-8")

    workers = [
        _FakeWorker("boss-a", "127.0.0.1:9527", "account-a", "boss-proxy-001", "http=127.0.0.1:18081;https=127.0.0.1:18081"),
        _FakeWorker("boss-b", "127.0.0.1:9528", "account-b", "boss-proxy-002", "http=127.0.0.1:18082;https=127.0.0.1:18082"),
    ]
    manager = _FakeManager(state_root, profile_root)

    # mock 临时浏览器查杀
    killed_temp = []
    monkeypatch.setattr(rec, "_find_temp_drissionpage_pids", lambda: [777, 888])
    monkeypatch.setattr(rec, "_stop_pid", lambda pid: killed_temp.append(pid))
    # mock 出口验证：返回代理出口 IP
    monkeypatch.setattr(rec, "_probe_egress_ip", lambda host_port: "104.28.152.154")

    report = rec.reconcile_workers(workers, manager)

    # 两个配置 worker 都 ensure 了
    assert set(manager.ensured) == {"boss-a", "boss-b"}
    # 孤儿状态文件被删除
    assert not (state_root / "boss-c.json").exists()
    assert (state_root / "boss-a.json").exists()
    # 临时浏览器被杀
    assert set(killed_temp) == {777, 888}
    # report 汇总正确
    assert report["expected"] == 2
    assert report["started"] == 2
    ids = {w["worker_id"]: w for w in report["workers"]}
    assert ids["boss-a"]["egress_ip"] == "104.28.152.154"
    assert ids["boss-a"]["ok"] is True


def test_reconcile_marks_worker_not_ok_when_egress_probe_fails(tmp_path, monkeypatch):
    from services import boss_worker_reconciler as rec

    state_root = tmp_path / "boss-workers"
    profile_root = tmp_path / "chrome-profiles"
    state_root.mkdir(parents=True)
    profile_root.mkdir(parents=True)

    workers = [
        _FakeWorker("boss-a", "127.0.0.1:9527", "account-a", "boss-proxy-001", "http=127.0.0.1:18081;https=127.0.0.1:18081"),
    ]
    manager = _FakeManager(state_root, profile_root)

    monkeypatch.setattr(rec, "_find_temp_drissionpage_pids", lambda: [])
    monkeypatch.setattr(rec, "_stop_pid", lambda pid: None)
    # 出口探测失败（连不上/未起）→ egress_ip=None, ok=False
    monkeypatch.setattr(rec, "_probe_egress_ip", lambda host_port: None)

    report = rec.reconcile_workers(workers, manager)

    assert report["expected"] == 1
    assert report["workers"][0]["egress_ip"] is None
    assert report["workers"][0]["ok"] is False
