import asyncio
import json
import tempfile
import threading
from pathlib import Path

import pytest

from services.boss_zhipin_client import BossAccessLimitedError, BossWorkerPoolClient, BossZhipinClient


class Worker:
    worker_id = "a"
    profile_id = "profile-a"
    _browser_host_port = "127.0.0.1:9999"

    def __init__(self):
        self.error = None
        self.release = None
        self.calls = 0

    async def probe_ready(self):
        self.calls += 1
        if self.release:
            await self.release.wait()
        if self.error:
            raise self.error

    async def scrape_many(self, *args, **kwargs):
        return {"summary": {}, "jobs": []}


async def settle(pool):
    await asyncio.gather(*list(pool._verification_tasks.values()))


@pytest.mark.asyncio
async def test_manual_login_verification_releases_latch_then_business_success():
    worker = Worker()
    pool = BossWorkerPoolClient([worker])
    pool._record_login_required("a", reason="login __zp_stoken__")
    pool.begin_verification()
    assert worker.calls == 0
    pool.begin_verification(manual=True)
    assert pool.worker_status()["a"]["state"] == "recovering"
    await settle(pool)
    assert pool.worker_status()["a"]["state"] == "candidate"
    await pool.scrape_many()
    assert pool.worker_status()["a"]["state"] == "healthy"
    assert pool.worker_status()["a"]["verification"]["last_success_at"]


@pytest.mark.asyncio
async def test_duplicate_verification_is_exclusive_and_operation_is_stable():
    worker = Worker()
    worker.release = asyncio.Event()
    pool = BossWorkerPoolClient([worker])
    first = pool.begin_verification(manual=True)
    second = pool.begin_verification(manual=True)
    assert first["a"]["verification"]["operation_id"] == second["a"]["verification"]["operation_id"]
    await asyncio.sleep(0)
    with pytest.raises(BossAccessLimitedError):
        await pool.scrape_many()
    worker.release.set()
    await settle(pool)
    assert worker.calls == 1


@pytest.mark.asyncio
async def test_account_restriction_survives_restart_and_needs_manual_verification():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "health.json"
        worker = Worker()
        pool = BossWorkerPoolClient([worker], health_file=path)
        pool._record_login_required("a", reason="account code=32")
        restored = BossWorkerPoolClient([worker], health_file=path)
        restored.begin_verification()
        assert not restored._verification_tasks
        assert restored.worker_status()["a"]["verification"]["reason_kind"] == "account_restricted"
        restored.begin_verification(manual=True)
        await settle(restored)
        assert restored.worker_status()["a"]["state"] == "candidate"
        restarted = BossWorkerPoolClient([worker], health_file=path)
        assert restarted.worker_status()["a"]["state"] == "unknown"
        assert json.loads(path.read_text())["a"]["binding"]


@pytest.mark.asyncio
async def test_failed_revalidation_keeps_login_quarantine():
    worker = Worker()
    worker.error = BossAccessLimitedError("login __zp_stoken__ missing")
    pool = BossWorkerPoolClient([worker])
    pool.begin_verification(manual=True)
    await settle(pool)
    assert pool.worker_status()["a"]["state"] == "login_required"
    with pytest.raises(BossAccessLimitedError):
        await pool.scrape_many()


@pytest.mark.asyncio
async def test_cancelled_collection_holds_worker_until_underlying_execution_finishes():
    worker = Worker()
    release = asyncio.Event()

    async def scrape():
        await release.wait()
        return {"summary": {}, "jobs": []}

    worker.scrape_many = scrape
    pool = BossWorkerPoolClient([worker])
    running = asyncio.create_task(pool.scrape_many())
    await asyncio.sleep(0)
    running.cancel()
    await asyncio.sleep(0)
    pool.begin_verification(manual=True)
    assert not pool._verification_tasks
    assert pool.worker_status()["a"]["in_flight"] == 1
    release.set()
    await asyncio.gather(running, return_exceptions=True)
    await asyncio.gather(*list(pool._drain_tasks))
    assert pool.worker_status()["a"]["in_flight"] == 0


@pytest.mark.asyncio
async def test_failed_transport_verification_does_not_erase_account_quarantine():
    worker = Worker()
    worker.error = RuntimeError("temporary connection failure")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "health.json"
        pool = BossWorkerPoolClient([worker], health_file=path)
        pool._record_login_required("a", reason="account code=32")
        pool.begin_verification(manual=True)
        await settle(pool)
        restored = BossWorkerPoolClient([worker], health_file=path)
        assert restored.worker_status()["a"]["verification"]["reason_kind"] == "account_restricted"
        restored.begin_verification()
        assert not restored._verification_tasks


@pytest.mark.asyncio
async def test_verification_timeout_keeps_worker_reserved(monkeypatch):
    worker = Worker()
    worker.release = asyncio.Event()
    pool = BossWorkerPoolClient([worker])
    original_wait = asyncio.wait_for

    async def quick_timeout(awaitable, timeout):
        return await original_wait(awaitable, timeout=0.01)

    monkeypatch.setattr("services.boss_worker_health.asyncio.wait_for", quick_timeout)
    pool.begin_verification(manual=True)
    await asyncio.sleep(0.03)
    assert pool.worker_status()["a"]["verification"]["state"] == "unknown_execution"
    assert pool.worker_status()["a"]["state"] == "recovering"
    with pytest.raises(BossAccessLimitedError):
        await pool.scrape_many()
    worker.release.set()
    await settle(pool)
    assert pool.worker_status()["a"]["state"] == "candidate"


@pytest.mark.asyncio
async def test_plain_browser_error_is_isolated_then_verified_and_repaired():
    broken, healthy = Worker(), Worker()
    healthy.worker_id = "b"
    error = RuntimeError("Chrome DevTools connection refused")
    broken.error = error

    async def failed_scrape(*args, **kwargs):
        raise error

    broken.scrape_many = failed_scrape
    repairs = []

    class Runtime:
        def ensure_worker(self, config, **kwargs):
            repairs.append(config.worker_id)
            broken.error = None

    pool = BossWorkerPoolClient([broken, healthy], runtime_manager=Runtime())
    result = await pool.scrape_many()
    assert result["summary"]["worker_id"] == "b"
    status = pool.worker_status()["a"]
    assert status["state"] == "unknown"
    assert status["verification"]["reason_kind"] == "chrome_unhealthy"
    assert pool._acquire_worker(exclude={"b"}) is None
    pool.begin_verification(worker_id="a")
    await settle(pool)
    assert repairs == ["a"] and broken.calls == 2
    assert pool.worker_status()["a"]["state"] == "candidate"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["internal-timeout", "caller-cancelled"])
@pytest.mark.parametrize("late_error", [None, "account code=32"])
async def test_real_thread_keeps_pool_ownership_until_completion(monkeypatch, cancel, late_error):
    monkeypatch.setattr("services.boss_zhipin_client._settings.BOSS_ZHIPIN_SYNC_TIMEOUT_SEC", 10 if cancel else 0.03)
    worker = BossZhipinClient(worker_id="a", per_worker_concurrency=1)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def scrape(*args):
        calls.append(1)
        entered.set()
        assert release.wait(5), "test did not release thread"
        if late_error:
            raise BossAccessLimitedError(late_error)
        return {"summary": {}, "jobs": []}

    monkeypatch.setattr(worker, "_scrape_many_sync", scrape)
    monkeypatch.setattr(worker, "_probe_ready_sync", lambda: None)
    pool = BossWorkerPoolClient([worker])
    args = (["test"], [1], 1, 1, False, False)
    request = asyncio.create_task(pool.scrape_many(*args))
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        if cancel:
            request.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else BossAccessLimitedError):
            await asyncio.wait_for(request, 1)
        assert worker.has_pending_execution
        assert worker._sema.locked()
        assert pool.worker_status()["a"]["in_flight"] == 1
        assert pool.worker_status()["a"]["state"] == "recovering"
        pool.begin_verification(manual=True)
        assert not pool._verification_tasks
        with pytest.raises(BossAccessLimitedError):
            await pool.scrape_many(*args)
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(*list(pool._drain_tasks)), 2)
        await worker.wait_for_idle()
    assert pool.worker_status()["a"]["in_flight"] == 0
    assert pool.worker_status()["a"]["state"] == ("login_required" if late_error else "unknown")
    assert pool._acquire_worker(exclude=set()) is None
    pool.begin_verification(manual=True)
    await settle(pool)
    assert pool.worker_status()["a"]["state"] == "candidate"


@pytest.mark.asyncio
async def test_cancelled_verification_releases_only_after_real_probe_finishes(monkeypatch):
    worker = BossZhipinClient(worker_id="a", per_worker_concurrency=1)
    entered, release = threading.Event(), threading.Event()

    def probe():
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(worker, "_probe_ready_sync", probe)
    pool = BossWorkerPoolClient([worker])
    pool.begin_verification(manual=True)
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        task = pool._verification_tasks["a"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert pool.worker_status()["a"]["state"] == "recovering"
        assert pool._acquire_worker(exclude=set()) is None
        pool.begin_verification(manual=True)
        assert not pool._verification_tasks
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*list(pool._drain_tasks)), 2)
    assert pool.worker_status()["a"]["state"] == "unknown"
