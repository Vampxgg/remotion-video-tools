"""Local TCP + real-thread fault verification. Never opens Chrome or calls BOSS.

Run: python scripts/verify_boss_worker_isolation.py
Uses the production worker/client and health router with a controlled synchronous
browser backend; binds only an ephemeral localhost port. No application lifespan,
configured worker pool, production credentials, database writes or collection.
"""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

from api import jobs_region
from services.boss_zhipin_client import BossAccessLimitedError, BossWorkerPoolClient, BossZhipinClient


async def verify():
    worker = BossZhipinClient(worker_id="isolation-check", per_worker_concurrency=1)
    release, entered = threading.Event(), threading.Event()
    state = {"broken": True, "stall": False, "calls": 0, "repairs": 0}

    def scrape(*args):
        state["calls"] += 1
        if state["broken"]:
            raise RuntimeError("Chrome DevTools connection refused (controlled fault)")
        if state["stall"]:
            entered.set()
            if not release.wait(15):
                raise RuntimeError("verification failed to release test thread")
        return {"summary": {}, "jobs": []}

    def probe():
        if state["broken"]:
            raise RuntimeError("Chrome DevTools connection refused (controlled fault)")

    class Runtime:
        def ensure_worker(self, config, **kwargs):
            state["repairs"] += 1
            state["broken"] = False

    pool = BossWorkerPoolClient([worker], runtime_manager=Runtime())
    app = FastAPI()
    app.include_router(jobs_region.router, prefix="/api")

    @app.post("/verification/attempt")
    async def attempt():
        try:
            return await pool.scrape_many(["controlled-test"], [1], 1, 1, False, False)
        except BossAccessLimitedError as error:
            return JSONResponse(status_code=409, content={"error": str(error)})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    address = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    with ExitStack() as stack:
        for target, value in (
            ("api.jobs_region._boss_client", pool),
            ("api.jobs_region._boss_region_search_proxy_enabled", False),
            ("api.jobs_region._boss_circuit", jobs_region._BossCircuitBreaker()),
            ("api.jobs_region._settings.BOSS_SERVICE_API_KEY", "local-isolation-check"),
            ("services.boss_zhipin_client._settings.BOSS_ZHIPIN_SYNC_TIMEOUT_SEC", 2),
        ):
            stack.enter_context(patch(target, value))
        stack.enter_context(patch.object(worker, "_scrape_many_sync", scrape))
        stack.enter_context(patch.object(worker, "_probe_ready_sync", probe))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=address, timeout=3, trust_env=False,
                                         headers={"x-api-key": "local-isolation-check"}) as client:
                health_url = "/api/jobs/sources/boss_zhipin/health"

                async def health():
                    response = await client.get(health_url)
                    response.raise_for_status()
                    return response.json()["data"]

                async def wait_state(expected):
                    async with asyncio.timeout(3):
                        while True:
                            data = await health()
                            if data["workers"][worker.worker_id]["state"] == expected:
                                return data
                            await asyncio.sleep(0.01)

                assert (await client.post("/verification/attempt")).status_code == 409
                data = await health()
                assert data["available_workers"] == 0
                assert data["workers"][worker.worker_id]["verification"]["reason_kind"] == "chrome_unhealthy", data
                assert (await client.post(health_url, json={"manual": False})).json()["data"]["verification_accepted"]
                await wait_state("candidate")
                assert state["repairs"] == 1
                assert (await client.post("/verification/attempt")).status_code == 200
                await wait_state("healthy")
                print("PASS: plain browser exception -> isolated -> health verification repairs -> business success", flush=True)

                state["stall"] = True
                jobs_region._settings.BOSS_ZHIPIN_SYNC_TIMEOUT_SEC = 0.05
                assert (await client.post("/verification/attempt")).status_code == 409
                assert entered.is_set() and worker.has_pending_execution
                data = await health()
                assert data["available_workers"] == 0
                assert data["workers"][worker.worker_id]["in_flight"] == 1
                assert data["workers"][worker.worker_id]["verification"]["state"] == "unknown_execution"
                calls = state["calls"]
                assert (await client.post("/verification/attempt")).status_code == 409
                data = (await client.post(health_url, json={"manual": True})).json()["data"]
                # Existing draining operation is not a newly accepted verification.
                assert not data["verification_accepted"]
                assert not pool._verification_tasks and state["calls"] == calls
                assert state["repairs"] == 1
                print("PASS: real thread outlives HTTP failure; second request and verification cannot reuse worker", flush=True)

                release.set()
                data = await wait_state("unknown")
                assert data["workers"][worker.worker_id]["in_flight"] == 0
                assert data["available_workers"] == 0
                assert (await client.post(health_url, json={"manual": False})).json()["data"]["verification_accepted"]
                await wait_state("candidate")
                state["stall"] = False
                assert (await client.post("/verification/attempt")).status_code == 200
                await wait_state("healthy")
                print("PASS: thread completion -> ownership released -> explicit verification -> usable", flush=True)
                print(json.dumps({"real_http": True, "real_thread": True, "boss_requests": 0,
                                  "chrome_started": 0, "controlled_attempts": state["calls"]}), flush=True)
        finally:
            release.set()
            await pool.shutdown()
            server.should_exit = True
            await serving
            sock.close()


if __name__ == "__main__":
    asyncio.run(verify())
