# -*- coding: utf-8 -*-
"""BOSS 服务拆分与主服务代理边界测试。"""

import importlib
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_importing_boss_router_does_not_initialize_real_client(monkeypatch):
    """导入 router 不能触发真实 BOSS client 初始化，避免主服务 4 worker 重复抢 Chrome。"""
    sys.modules.pop("api.boss_zhipin", None)

    import services.boss_zhipin_client as client_module

    def fail_if_called():
        raise AssertionError("get_boss_client must not run at import time")

    monkeypatch.setattr(client_module, "get_boss_client", fail_if_called)

    module = importlib.import_module("api.boss_zhipin")

    assert module.router is not None


def test_boss_server_exposes_real_boss_routes_without_main_app(monkeypatch):
    sys.modules.pop("boss_server", None)

    module = importlib.import_module("boss_server")

    paths = {route.path for route in module.app.routes}
    assert "/health" in paths
    assert "/api/scrape/boss/search" in paths
    assert "/api/scrape/boss/batch-search" in paths

    with TestClient(module.app) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["data"]["service"] == "boss"


def test_main_mounts_boss_proxy_not_real_boss_router(monkeypatch):
    sys.modules.pop("main", None)
    sys.modules.pop("api.boss_zhipin", None)
    sys.modules.pop("api.boss_proxy", None)

    import services.boss_zhipin_client as client_module

    def fail_if_called():
        raise AssertionError("main app must not initialize real BOSS client")

    monkeypatch.setattr(client_module, "get_boss_client", fail_if_called)

    module = importlib.import_module("main")
    boss_search_routes = [
        route for route in module.app.routes if getattr(route, "path", None) == "/api/scrape/boss/search"
    ]

    assert boss_search_routes
    assert boss_search_routes[0].endpoint.__module__ == "api.boss_proxy"


def test_boss_proxy_forwards_search_payload_to_internal_service(monkeypatch):
    sys.modules.pop("api.boss_proxy", None)
    module = importlib.import_module("api.boss_proxy")
    monkeypatch.setattr(module._settings, "BOSS_SERVICE_URL", "http://boss.local")
    monkeypatch.setattr(module._settings, "BOSS_SERVICE_API_KEY", "internal-key", raising=False)
    monkeypatch.setattr(module._settings, "BOSS_PROXY_TIMEOUT_SEC", 3.0, raising=False)

    calls = []

    class _FakeAsyncClient:
        def __init__(self, *, timeout, trust_env):
            self.timeout = timeout
            self.trust_env = trust_env

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, json, headers):
            calls.append({"url": url, "json": json, "headers": headers})
            return httpx.Response(
                200,
                json={"code": 200, "message": "ok", "data": {"summary": {"total_jobs": 1}}},
            )

    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeAsyncClient)

    app = module.create_router_app_for_test()
    with TestClient(app) as client:
        resp = client.post(
            "/api/scrape/boss/search",
            json={"keyword": "前端", "city_code": 101280600, "max_pages": 1},
            headers={"x-api-key": "external-key"},
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["summary"]["total_jobs"] == 1
    assert calls == [
        {
            "url": "http://boss.local/api/scrape/boss/search",
            "json": {
                "keyword": "前端",
                "city_code": 101280600,
                "max_pages": 1,
                "max_items": None,
                "include_raw": False,
                "include_description": False,
            },
            "headers": {"x-api-key": "internal-key"},
        }
    ]
