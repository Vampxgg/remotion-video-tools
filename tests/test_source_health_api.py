import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from api import jobs_region
from services.boss_zhipin_client import BossWorkerPoolClient


class Worker:
    worker_id = "a"

    def __init__(self):
        self.calls = 0

    async def probe_ready(self):
        self.calls += 1


@pytest.mark.asyncio
async def test_health_is_passive_and_owner_auth_and_circuit_are_respected(monkeypatch):
    worker = Worker()
    pool = BossWorkerPoolClient([worker])
    pool._record_login_required("a", reason="login __zp_stoken__")
    monkeypatch.setattr(jobs_region, "_boss_client", pool)
    monkeypatch.setattr(jobs_region, "_boss_region_search_proxy_enabled", False)
    monkeypatch.setattr(jobs_region._settings, "BOSS_SERVICE_API_KEY", "internal-test")
    monkeypatch.setattr(jobs_region._settings, "REGION_JOBS_API_KEY", "external-test")
    monkeypatch.setattr(jobs_region, "_boss_circuit", jobs_region._BossCircuitBreaker())
    app = FastAPI()
    app.include_router(jobs_region.router, prefix="/api")
    url = "/api/jobs/sources/boss_zhipin/health"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(url)).status_code == 401
        assert (await client.get(url, headers={"x-api-key": "external-test"})).status_code == 401
        headers = {"x-api-key": "internal-test"}
        response = await client.get(url, headers=headers)
        assert response.json()["data"]["available_workers"] == 0 and worker.calls == 0
        jobs_region._boss_circuit.trip(seconds=60, reason="existing cooldown")
        response = await client.post(url, headers=headers, json={"manual": True})
        assert not response.json()["data"]["verification_accepted"] and worker.calls == 0
        jobs_region._boss_circuit.reset()
        response = await client.post(url, headers=headers, json={"manual": True, "worker_id": "a"})
        assert response.json()["data"]["verification_accepted"]
        await asyncio.gather(*list(pool._verification_tasks.values()))
        assert (await client.get(url, headers=headers)).json()["data"]["workers"]["a"]["state"] == "candidate"
        assert worker.calls == 1


@pytest.mark.asyncio
async def test_main_proxy_forwards_verification_to_single_owner(monkeypatch):
    import json
    requests = []
    original_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "data": {"supported": True, "verification_accepted": True}})

    monkeypatch.setattr(jobs_region, "_boss_region_search_proxy_enabled", True)
    monkeypatch.setattr(jobs_region._settings, "BOSS_SERVICE_URL", "http://boss-owner")
    monkeypatch.setattr(jobs_region._settings, "BOSS_SERVICE_API_KEY", "owner-key")
    monkeypatch.setattr(jobs_region.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(jobs_region, "_get_boss_region_client", lambda: pytest.fail("main API must not own browsers"))
    response = await jobs_region._source_health("boss_zhipin", jobs_region.SourceVerificationRequest(manual=True, worker_id="a"))
    assert response["data"]["verification_accepted"]
    assert len(requests) == 1 and requests[0].url.host == "boss-owner"
    assert requests[0].headers["x-api-key"] == "owner-key"
    assert json.loads(requests[0].content) == {"manual": True, "worker_id": "a"}


@pytest.mark.asyncio
async def test_draining_execution_is_not_reported_as_accepted_verification(monkeypatch):
    worker = Worker()
    release = asyncio.Event()

    async def scrape(*args):
        await release.wait()
        return {"summary": {}, "jobs": []}

    worker.scrape_many = scrape
    pool = BossWorkerPoolClient([worker])
    monkeypatch.setattr(jobs_region, "_boss_client", pool)
    monkeypatch.setattr(jobs_region, "_boss_region_search_proxy_enabled", False)
    monkeypatch.setattr(jobs_region, "_boss_circuit", jobs_region._BossCircuitBreaker())
    running = asyncio.create_task(pool.scrape_many())
    await asyncio.sleep(0)
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    try:
        response = await jobs_region._source_health(
            "boss_zhipin", jobs_region.SourceVerificationRequest(manual=True, worker_id="a"),
        )
        response = json.loads(response.body)
        assert not response["data"]["verification_accepted"]
        assert response["data"]["available_workers"] == 0
        assert response["data"]["workers"]["a"]["in_flight"] == 1
        assert worker.calls == 0
    finally:
        release.set()
        await asyncio.gather(*list(pool._drain_tasks))
