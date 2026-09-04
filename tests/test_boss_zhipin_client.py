# -*- coding: utf-8 -*-
"""BOSS 直聘采集客户端并发与风控边界测试。"""

import asyncio
import json
import time

import httpx
import pytest

from services.boss_zhipin_client import (
    BossAccessLimitedError,
    BossZhipinClient,
    BossWorkerPoolClient,
    _DirectBossSession,
    _configured_boss_workers,
    _single_profile_concurrency,
)
from services.boss_proxy_pool import BossProxyPool


class _FakeCookieJar:
    def __init__(self):
        self._values = {}

    def clear(self):
        self._values.clear()

    def set(self, name, value, domain=None):
        self._values[(domain, name)] = value


class _FakeBrowserCookies(dict):
    def as_dict(self):
        return dict(self)


class _FakeHttp:
    def __init__(self, bodies):
        self.cookies = _FakeCookieJar()
        self._bodies = list(bodies)
        self.calls = []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        body = self._bodies.pop(0)

        class _Response:
            status_code = 200
            text = ""

            def json(self_nonlocal):
                return body

        return _Response()


class _FakeTab:
    def __init__(self):
        self.get_calls = []

    def get(self, url):
        self.get_calls.append(url)

    def cookies(self, all_domains=True):
        return _FakeBrowserCookies({"__zp_stoken__": f"token-{len(self.get_calls)}"})

    def run_js(self, script):
        return "Fake UA"


@pytest.fixture(autouse=True)
def _isolate_boss_proxy_file_settings(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_PROXY_POOL_FILE",
        None,
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS_FILE",
        None,
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_PROXY_HEALTHCHECK_URL",
        "",
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_PROXY_SELECTION_STRATEGY",
        "ordered",
        raising=False,
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_PROXY_RECENT_AVOID_COUNT",
        0,
        raising=False,
    )


def test_single_profile_concurrency_rejects_values_above_two(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_MAX_CONCURRENCY",
        3,
    )

    with pytest.raises(ValueError, match="单 Chrome profile"):
        _single_profile_concurrency()


def test_single_profile_concurrency_accepts_one_or_two(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_MAX_CONCURRENCY",
        1,
    )
    assert _single_profile_concurrency() == 1

    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_MAX_CONCURRENCY",
        2,
    )
    assert _single_profile_concurrency() == 2


@pytest.mark.asyncio
async def test_boss_client_converts_sync_scrape_timeout_to_access_limited(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_SYNC_TIMEOUT_SEC",
        0.01,
    )
    client = BossZhipinClient(worker_id="boss-a")

    def slow_scrape(*args, **kwargs):
        time.sleep(0.1)
        return {"summary": {}, "jobs": [], "warnings": []}

    monkeypatch.setattr(client, "_scrape_many_sync", slow_scrape)

    with pytest.raises(BossAccessLimitedError, match="同步抓取超时"):
        await client.scrape_many(["python"], [101280600], 1, 1, False, False)


def test_configured_boss_workers_builds_five_isolated_clients(monkeypatch):
    configs = [
        {
            "worker_id": f"boss-{index}",
            "browser_host_port": f"127.0.0.1:{9526 + index}",
            "profile_id": f"account-{index}",
            "proxy_url": f"http://127.0.0.1:{7890 + index}",
            "per_worker_concurrency": 1,
        }
        for index in range(1, 6)
    ]
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS",
        configs,
    )

    workers = _configured_boss_workers()

    assert [worker.worker_id for worker in workers] == [
        "boss-1",
        "boss-2",
        "boss-3",
        "boss-4",
        "boss-5",
    ]
    assert [worker._browser_host_port for worker in workers] == [
        "127.0.0.1:9527",
        "127.0.0.1:9528",
        "127.0.0.1:9529",
        "127.0.0.1:9530",
        "127.0.0.1:9531",
    ]
    assert all(worker._concurrency == 1 for worker in workers)


def test_configured_boss_workers_rejects_unsafe_per_worker_concurrency(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS",
        [
            {
                "worker_id": "boss-a",
                "browser_host_port": "127.0.0.1:9527",
                "per_worker_concurrency": 3,
            }
        ],
    )

    with pytest.raises(ValueError, match="单 Chrome profile"):
        _configured_boss_workers()


def test_configured_boss_workers_rejects_missing_port_in_multi_worker(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS",
        [
            {"worker_id": "boss-a", "profile_id": "account-a"},
            {
                "worker_id": "boss-b",
                "profile_id": "account-b",
                "browser_host_port": "127.0.0.1:9528",
            },
        ],
    )

    with pytest.raises(ValueError, match="browser_host_port"):
        _configured_boss_workers()


def test_configured_boss_workers_rejects_duplicate_identity_boundaries(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS",
        [
            {
                "worker_id": "boss-a",
                "profile_id": "account-a",
                "browser_host_port": "127.0.0.1:9527",
            },
            {
                "worker_id": "boss-a",
                "profile_id": "account-b",
                "browser_host_port": "127.0.0.1:9528",
            },
        ],
    )

    with pytest.raises(ValueError, match="worker_id"):
        _configured_boss_workers()

    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS",
        [
            {
                "worker_id": "boss-a",
                "profile_id": "account-a",
                "browser_host_port": "127.0.0.1:9527",
            },
            {
                "worker_id": "boss-b",
                "profile_id": "account-b",
                "browser_host_port": "127.0.0.1:9527",
            },
        ],
    )

    with pytest.raises(ValueError, match="browser_host_port"):
        _configured_boss_workers()


def test_configured_boss_worker_defaults_to_one_concurrency(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS",
        [
            {
                "worker_id": "boss-a",
                "browser_host_port": "127.0.0.1:9527",
            }
        ],
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_MAX_CONCURRENCY",
        2,
    )

    [worker] = _configured_boss_workers()

    assert worker._concurrency == 1


def test_configured_boss_workers_resolves_proxy_pool_by_proxy_id(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_PROXY_POOL",
        [
            {
                "proxy_id": "boss-proxy-a",
                "local_proxy_url": "http://user:secret@127.0.0.1:18081",
                "chrome_proxy_server": "http=127.0.0.1:18081;https=127.0.0.1:18081",
                "upstream_label": "CF官方优选1",
            }
        ],
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS",
        [
            {
                "worker_id": "boss-a",
                "browser_host_port": "127.0.0.1:9527",
                "profile_id": "account-a",
                "proxy_id": "boss-proxy-a",
            }
        ],
    )

    [worker] = _configured_boss_workers()

    assert worker.proxy_id == "boss-proxy-a"
    assert worker._proxy_url == "http://user:secret@127.0.0.1:18081"
    assert worker.chrome_proxy_server == "http=127.0.0.1:18081;https=127.0.0.1:18081"


def test_configured_boss_workers_auto_assigns_proxy_pool(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_PROXY_POOL",
        [
            {
                "proxy_id": "boss-proxy-a",
                "local_proxy_url": "http://127.0.0.1:18081",
            },
            {
                "proxy_id": "boss-proxy-b",
                "local_proxy_url": "http://127.0.0.1:18082",
            },
        ],
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS",
        [
            {
                "worker_id": "boss-a",
                "browser_host_port": "127.0.0.1:9527",
                "profile_id": "account-a",
            },
            {
                "worker_id": "boss-b",
                "browser_host_port": "127.0.0.1:9528",
                "profile_id": "account-b",
            },
        ],
    )

    workers = _configured_boss_workers()

    assert [worker.proxy_id for worker in workers] == ["boss-proxy-a", "boss-proxy-b"]
    assert [worker._proxy_url for worker in workers] == [
        "http://127.0.0.1:18081",
        "http://127.0.0.1:18082",
    ]


def test_configured_boss_workers_rejects_when_proxy_pool_insufficient(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_PROXY_POOL",
        [
            {
                "proxy_id": "boss-proxy-a",
                "local_proxy_url": "http://127.0.0.1:18081",
            }
        ],
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS",
        [
            {
                "worker_id": "boss-a",
                "browser_host_port": "127.0.0.1:9527",
                "profile_id": "account-a",
            },
            {
                "worker_id": "boss-b",
                "browser_host_port": "127.0.0.1:9528",
                "profile_id": "account-b",
            },
        ],
    )

    with pytest.raises(ValueError, match="可用代理不足"):
        _configured_boss_workers()


def test_configured_boss_workers_loads_workers_and_proxy_pool_from_files(monkeypatch, tmp_path):
    proxy_file = tmp_path / "boss-proxy-pool.json"
    worker_file = tmp_path / "boss-workers.json"
    proxy_file.write_text(
        json.dumps([
            {
                "proxy_id": "boss-proxy-file",
                "local_proxy_url": "http://127.0.0.1:18091",
                "chrome_proxy_server": "http=127.0.0.1:18091;https=127.0.0.1:18091",
            }
        ]),
        encoding="utf-8",
    )
    worker_file.write_text(
        json.dumps([
            {
                "worker_id": "boss-file",
                "browser_host_port": "127.0.0.1:9627",
                "profile_id": "account-file",
                "proxy_id": "boss-proxy-file",
                "per_worker_concurrency": 1,
            }
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_PROXY_POOL",
        [
            {
                "proxy_id": "boss-proxy-env",
                "local_proxy_url": "http://127.0.0.1:18081",
            }
        ],
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS",
        [
            {
                "worker_id": "boss-env",
                "browser_host_port": "127.0.0.1:9527",
                "profile_id": "account-env",
                "proxy_id": "boss-proxy-env",
            }
        ],
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_PROXY_POOL_FILE",
        str(proxy_file),
        raising=False,
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_WORKERS_FILE",
        str(worker_file),
        raising=False,
    )

    [worker] = _configured_boss_workers()

    assert worker.worker_id == "boss-file"
    assert worker.proxy_id == "boss-proxy-file"
    assert worker._browser_host_port == "127.0.0.1:9627"
    assert worker._proxy_url == "http://127.0.0.1:18091"


def test_direct_session_refreshes_once_after_code_37(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_DIRECT_COOKIE_WAIT_SEC",
        0,
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_DIRECT_BUDGET_PER_TOKEN",
        5,
    )
    tab = _FakeTab()
    http = _FakeHttp([
        {"code": 37},
        {"code": 0, "zpData": {"jobList": []}},
    ])
    session = _DirectBossSession(tab, http)

    body = session.fetch_list("前端", 101280600, 1)

    assert body["code"] == 0
    assert session.refresh_count == 2
    assert len(http.calls) == 2


def test_direct_session_raises_access_limited_after_repeated_code_37(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_DIRECT_COOKIE_WAIT_SEC",
        0,
    )
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_DIRECT_BUDGET_PER_TOKEN",
        5,
    )
    tab = _FakeTab()
    http = _FakeHttp([
        {"code": 37},
        {"code": 37},
    ])
    session = _DirectBossSession(tab, http)

    with pytest.raises(BossAccessLimitedError, match="连续返回 code=37"):
        session.fetch_list("前端", 101280600, 1)

    assert session.refresh_count == 2
    assert len(http.calls) == 2


def test_direct_session_treats_http_timeout_as_access_limited(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_DIRECT_COOKIE_WAIT_SEC",
        0,
    )

    class _TimeoutHttp:
        def __init__(self):
            self.cookies = _FakeCookieJar()

        def get(self, url, params=None, headers=None):
            raise httpx.TimeoutException("timed out")

    session = _DirectBossSession(_FakeTab(), _TimeoutHttp())

    with pytest.raises(BossAccessLimitedError, match="直连代理请求失败"):
        session.fetch_list("前端", 101280600, 1)


class _FakeBossWorker:
    def __init__(
        self,
        worker_id,
        *,
        raises=None,
        proxy_id=None,
        browser_host_port=None,
        profile_id=None,
    ):
        self.worker_id = worker_id
        self.proxy_id = proxy_id
        self.profile_id = profile_id or worker_id
        self._browser_host_port = browser_host_port or f"127.0.0.1:{9500 + len(worker_id)}"
        self.calls = 0
        self._raises = raises
        self.closed = False

    async def scrape_many(self, *args, **kwargs):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return {
            "summary": {"worker_id": self.worker_id, "total_jobs": 0},
            "jobs": [],
            "warnings": [],
        }

    async def shutdown(self):
        self.closed = True


class _SlowFakeBossWorker(_FakeBossWorker):
    async def scrape_many(self, *args, **kwargs):
        self.calls += 1
        await asyncio.sleep(0.01)
        return {
            "summary": {"worker_id": self.worker_id, "total_jobs": 0},
            "jobs": [],
            "warnings": [],
        }


@pytest.mark.asyncio
async def test_worker_pool_round_robins_healthy_workers():
    worker_a = _FakeBossWorker("a")
    worker_b = _FakeBossWorker("b")
    pool = BossWorkerPoolClient([worker_a, worker_b])

    first = await pool.scrape_many(["前端"], [101280600], 1, 1, False, False)
    second = await pool.scrape_many(["前端"], [101280600], 1, 1, False, False)

    assert first["summary"]["worker_id"] == "a"
    assert second["summary"]["worker_id"] == "b"
    assert worker_a.calls == 1
    assert worker_b.calls == 1


def test_worker_pool_loads_initial_runtime_snapshot():
    worker_a = _FakeBossWorker("a")

    class _Runtime:
        def snapshot(self, worker_id):
            return {"pid": 4321, "devtools_ok": True}

    pool = BossWorkerPoolClient([worker_a], runtime_manager=_Runtime())

    status = pool.worker_status()["a"]
    assert status["chrome_pid"] == 4321
    assert status["devtools_ok"] is True


@pytest.mark.asyncio
async def test_worker_pool_assigns_three_concurrent_calls_to_three_workers():
    workers = [
        _SlowFakeBossWorker("a"),
        _SlowFakeBossWorker("b"),
        _SlowFakeBossWorker("c"),
    ]
    pool = BossWorkerPoolClient(workers)

    results = await asyncio.gather(*(
        pool.scrape_many(["前端"], [101280600], 1, 1, False, False)
        for _ in range(3)
    ))

    assert sorted(result["summary"]["worker_id"] for result in results) == ["a", "b", "c"]
    assert [worker.calls for worker in workers] == [1, 1, 1]


@pytest.mark.asyncio
async def test_worker_pool_cools_failed_worker_and_uses_next_available():
    worker_a = _FakeBossWorker(
        "a",
        raises=BossAccessLimitedError("访问受限", retry_after_seconds=60),
    )
    worker_b = _FakeBossWorker("b")
    pool = BossWorkerPoolClient([worker_a, worker_b])

    result = await pool.scrape_many(["前端"], [101280600], 1, 1, False, False)

    assert result["summary"]["worker_id"] == "b"
    assert worker_a.calls == 1
    assert worker_b.calls == 1
    status = pool.worker_status()
    assert status["a"]["state"] == "cooldown"
    assert status["b"]["state"] == "healthy"


@pytest.mark.asyncio
async def test_worker_pool_cools_proxy_when_worker_access_limited():
    proxy_pool = BossProxyPool([
        {"proxy_id": "proxy-a", "local_proxy_url": "http://127.0.0.1:18081"},
        {"proxy_id": "proxy-b", "local_proxy_url": "http://127.0.0.1:18082"},
    ])
    proxy_pool.lease_for_worker("a", requested_proxy_id="proxy-a")
    proxy_pool.lease_for_worker("b", requested_proxy_id="proxy-b")
    worker_a = _FakeBossWorker(
        "a",
        proxy_id="proxy-a",
        raises=BossAccessLimitedError("访问受限", retry_after_seconds=60),
    )
    worker_b = _FakeBossWorker("b", proxy_id="proxy-b")
    pool = BossWorkerPoolClient([worker_a, worker_b], proxy_pool=proxy_pool)

    result = await pool.scrape_many(["前端"], [101280600], 1, 1, False, False)

    assert result["summary"]["worker_id"] == "b"
    status = pool.worker_status()
    assert status["a"]["proxy_id"] == "proxy-a"
    assert status["a"]["proxy_state"] == "cooldown"
    assert status["a"]["local_proxy_url_masked"] == "http://127.0.0.1:18081"
    assert status["b"]["proxy_state"] == "leased"


@pytest.mark.asyncio
async def test_worker_pool_recovers_failed_worker_with_reassigned_proxy():
    proxy_pool = BossProxyPool([
        {"proxy_id": "proxy-a", "local_proxy_url": "http://127.0.0.1:18081"},
        {"proxy_id": "proxy-b", "local_proxy_url": "http://127.0.0.1:18082"},
        {"proxy_id": "proxy-c", "local_proxy_url": "http://127.0.0.1:18083"},
    ])
    proxy_pool.lease_for_worker("a", requested_proxy_id="proxy-a")
    proxy_pool.lease_for_worker("b", requested_proxy_id="proxy-b")
    worker_a = _FakeBossWorker(
        "a",
        proxy_id="proxy-a",
        browser_host_port="127.0.0.1:9527",
        profile_id="account-a",
        raises=BossAccessLimitedError("访问受限", retry_after_seconds=60),
    )
    worker_b = _FakeBossWorker(
        "b",
        proxy_id="proxy-b",
        browser_host_port="127.0.0.1:9528",
        profile_id="account-b",
    )
    restarts = []
    probes = []

    class _Runtime:
        def restart_worker(self, config, *, proxy_id, chrome_proxy_server):
            restarts.append((config, proxy_id, chrome_proxy_server))
            return {"pid": 4321, "devtools_ok": True}

    class _RecoveredWorker(_FakeBossWorker):
        async def probe_ready(self):
            probes.append(self.proxy_id)

    def worker_factory(old_worker, lease):
        return _RecoveredWorker(
            old_worker.worker_id,
            proxy_id=lease.proxy_id,
            browser_host_port=old_worker._browser_host_port,
            profile_id=old_worker.profile_id,
        )

    pool = BossWorkerPoolClient(
        [worker_a, worker_b],
        proxy_pool=proxy_pool,
        runtime_manager=_Runtime(),
        worker_factory=worker_factory,
        recover_failed_workers=True,
    )

    result = await pool.scrape_many(["前端"], [101280600], 1, 1, False, False)

    assert result["summary"]["worker_id"] == "b"
    assert worker_a.closed is True
    assert len(restarts) == 1
    assert restarts[0][0].worker_id == "a"
    assert restarts[0][0].browser_host_port == "127.0.0.1:9527"
    assert restarts[0][0].profile_id == "account-a"
    assert restarts[0][1] == "proxy-c"
    assert probes == ["proxy-c"]
    status = pool.worker_status()
    assert status["a"]["state"] == "healthy"
    assert status["a"]["proxy_id"] == "proxy-c"
    assert status["a"]["previous_proxy_id"] == "proxy-a"
    assert status["a"]["proxy_state"] == "leased"
    assert status["a"]["recovery_attempts"] == 1
    assert proxy_pool.status()["proxy-a"]["state"] == "cooldown"


@pytest.mark.asyncio
async def test_worker_pool_retries_single_recovered_worker_once():
    proxy_pool = BossProxyPool([
        {"proxy_id": "proxy-a", "local_proxy_url": "http://127.0.0.1:18081"},
        {"proxy_id": "proxy-b", "local_proxy_url": "http://127.0.0.1:18082"},
    ])
    proxy_pool.lease_for_worker("a", requested_proxy_id="proxy-a")
    worker_a = _FakeBossWorker(
        "a",
        proxy_id="proxy-a",
        browser_host_port="127.0.0.1:9527",
        profile_id="account-a",
        raises=BossAccessLimitedError("访问受限", retry_after_seconds=60),
    )
    restarts = []

    class _Runtime:
        def restart_worker(self, config, *, proxy_id, chrome_proxy_server):
            restarts.append((config, proxy_id, chrome_proxy_server))
            return {"pid": 4321, "devtools_ok": True}

    def worker_factory(old_worker, lease):
        return _FakeBossWorker(
            old_worker.worker_id,
            proxy_id=lease.proxy_id,
            browser_host_port=old_worker._browser_host_port,
            profile_id=old_worker.profile_id,
        )

    pool = BossWorkerPoolClient(
        [worker_a],
        proxy_pool=proxy_pool,
        runtime_manager=_Runtime(),
        worker_factory=worker_factory,
        recover_failed_workers=True,
    )

    result = await pool.scrape_many(["前端"], [101280600], 1, 1, False, False)

    assert result["summary"]["worker_id"] == "a"
    assert len(restarts) == 1
    assert worker_a.closed is True
    assert pool.worker_status()["a"]["proxy_id"] == "proxy-b"


@pytest.mark.asyncio
async def test_worker_pool_marks_login_required_without_proxy_rotation():
    proxy_pool = BossProxyPool([
        {"proxy_id": "proxy-a", "local_proxy_url": "http://127.0.0.1:18081"},
        {"proxy_id": "proxy-b", "local_proxy_url": "http://127.0.0.1:18082"},
    ])
    proxy_pool.lease_for_worker("a", requested_proxy_id="proxy-a")
    worker_a = _FakeBossWorker(
        "a",
        proxy_id="proxy-a",
        raises=BossAccessLimitedError("刷新 cookie 后仍缺少 __zp_stoken__，疑似未登录或被风控"),
    )
    restarts = []

    class _Runtime:
        def restart_worker(self, config, *, proxy_id, chrome_proxy_server):
            restarts.append((config, proxy_id, chrome_proxy_server))
            return {"pid": 4321, "devtools_ok": True}

    pool = BossWorkerPoolClient(
        [worker_a],
        proxy_pool=proxy_pool,
        runtime_manager=_Runtime(),
        recover_failed_workers=True,
    )

    with pytest.raises(BossAccessLimitedError, match="全部 BOSS worker"):
        await pool.scrape_many(["前端"], [101280600], 1, 1, False, False)

    status = pool.worker_status()
    proxy_status = proxy_pool.status()
    assert restarts == []
    assert status["a"]["state"] == "login_required"
    assert proxy_status["proxy-a"]["state"] == "leased"
    assert proxy_status["proxy-a"]["lease_worker_id"] == "a"
    assert proxy_status["proxy-b"]["state"] == "available"


@pytest.mark.asyncio
async def test_worker_pool_uses_short_chrome_cooldown_for_devtools_errors(monkeypatch):
    monkeypatch.setattr(
        "services.boss_zhipin_client._settings.BOSS_ZHIPIN_CHROME_RECOVERY_COOLDOWN_MINUTES",
        5,
        raising=False,
    )
    worker_a = _FakeBossWorker(
        "a",
        raises=BossAccessLimitedError("Chrome DevTools 端口未恢复"),
    )
    pool = BossWorkerPoolClient([worker_a])

    with pytest.raises(BossAccessLimitedError, match="全部 BOSS worker"):
        await pool.scrape_many(["前端"], [101280600], 1, 1, False, False)

    status = pool.worker_status()["a"]
    assert status["state"] == "cooldown"
    assert 0 < status["cooldown_remaining_seconds"] <= 300


@pytest.mark.asyncio
async def test_worker_pool_releases_new_proxy_when_recovery_restart_fails():
    proxy_pool = BossProxyPool([
        {"proxy_id": "proxy-a", "local_proxy_url": "http://127.0.0.1:18081"},
        {"proxy_id": "proxy-b", "local_proxy_url": "http://127.0.0.1:18082"},
    ])
    proxy_pool.lease_for_worker("a", requested_proxy_id="proxy-a")
    worker_a = _FakeBossWorker(
        "a",
        proxy_id="proxy-a",
        raises=BossAccessLimitedError("访问受限", retry_after_seconds=60),
    )

    class _Runtime:
        def restart_worker(self, config, *, proxy_id, chrome_proxy_server):
            raise RuntimeError("chrome restart failed")

    pool = BossWorkerPoolClient(
        [worker_a],
        proxy_pool=proxy_pool,
        runtime_manager=_Runtime(),
        recover_failed_workers=True,
    )

    with pytest.raises(BossAccessLimitedError, match="全部 BOSS worker"):
        await pool.scrape_many(["前端"], [101280600], 1, 1, False, False)

    status = proxy_pool.status()
    assert status["proxy-a"]["state"] == "cooldown"
    assert status["proxy-b"]["state"] == "available"
    assert status["proxy-b"]["lease_worker_id"] is None


@pytest.mark.asyncio
async def test_worker_pool_raises_access_limited_when_all_workers_cooling():
    worker_a = _FakeBossWorker(
        "a",
        raises=BossAccessLimitedError("访问受限", retry_after_seconds=60),
    )
    worker_b = _FakeBossWorker(
        "b",
        raises=BossAccessLimitedError("访问受限", retry_after_seconds=60),
    )
    pool = BossWorkerPoolClient([worker_a, worker_b])

    with pytest.raises(BossAccessLimitedError, match="全部 BOSS worker") as exc_info:
        await pool.scrape_many(["前端"], [101280600], 1, 1, False, False)

    assert exc_info.value.retry_after_seconds is not None
    assert exc_info.value.worker_status["a"]["state"] == "cooldown"
    assert pool.worker_status()["a"]["state"] == "cooldown"
    assert pool.worker_status()["b"]["state"] == "cooldown"
