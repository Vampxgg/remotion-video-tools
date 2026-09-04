# -*- coding: utf-8 -*-
"""``/api/jobs/region-search`` 共享接口契约与稳定性测试。

覆盖：
- 成功返回统一信封 + data.request/summary/source_status/jobs
- region_job_market_scan (1).yml 同款 payload 的字段兼容
- 空 keyword → 统一 422 信封（保留 HTTP 422）
- 重复 sources 去重
- BOSS 熔断冷却中不触碰 BOSS 客户端，返回非重试型状态
- BOSS 风控（BossAccessLimitedError）返回非重试型 source_status，且不影响智联
- 智联全部组合失败 → source_status.zhilian.ok=False
- on_source_error=fail 时整体失败返回非 5xx 的非重试码
"""

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from api import jobs_region
from services.boss_zhipin_client import BossAccessLimitedError
from utils.responses import validation_exception_handler


# ─────────────── 测试替身 ───────────────

class _FakeBossClient:
    """记录调用并按脚本返回/抛错的 BOSS 客户端替身。"""

    def __init__(self, *, jobs=None, raises=None):
        self.calls = []
        self._jobs = jobs or []
        self._raises = raises

    async def scrape_many(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return {
            "summary": {"combinations": 1, "pages_fetched": 1, "total_jobs": len(self._jobs)},
            "jobs": self._jobs,
            "warnings": [],
        }


class _FakeZhilianClient:
    def __init__(self, *, jobs=None, summary=None):
        self.calls = []
        self._jobs = jobs or []
        self._last_scrape_summary = summary or {
            "combinations": 1,
            "failed_combinations": 0,
            "pages_fetched": 1,
            "pages_requested": 1,
        }

    async def scrape_many(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return list(self._jobs)


def _boss_job():
    return {
        "job_name": "BOSS前端工程师",
        "company_name": "BOSS公司",
        "salary": "15-25K",
        "city": "深圳",
        "encrypt_job_id": "boss-1",
        "keyword": "前端",
    }


def _zhilian_job():
    return {
        "name": "智联前端工程师",
        "companyName": "智联公司",
        "salary": "10-20K",
        "positionNumber": "zl-1",
        "positionURL": "https://example.com/zl-1",
        "_query_keyword": "前端",
    }


@pytest.fixture
def client():
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(jobs_region.router, prefix="/api")
    # 每个用例前重置 BOSS 熔断器，避免用例间串扰
    jobs_region._boss_circuit.reset()
    with TestClient(app) as test_client:
        yield test_client
    jobs_region._boss_circuit.reset()


def _payload(**overrides):
    base = {
        "region": {
            "country": "CN",
            "province": "广东",
            "city": "深圳",
            "district": None,
            "platform_hints": {"zhilian_city_id": "765", "boss_city_code": 101280600},
        },
        "query": {"keywords": ["前端"], "keyword_mode": "any"},
        "sources": ["zhilian", "boss_zhipin"],
        "collection": {
            "max_pages_per_source": 1,
            "max_records_per_source": 20,
            "detail_level": "summary",
            "timeout_seconds": 90.0,
            "on_source_error": "continue",
        },
        "output": {"deduplicate": True, "include_raw": False, "include_source_metadata": True},
    }
    base.update(overrides)
    return base


# ─────────────── 用例 ───────────────

def test_success_dual_source_envelope(client, monkeypatch):
    monkeypatch.setattr(jobs_region, "_boss_client", _FakeBossClient(jobs=[_boss_job()]))
    monkeypatch.setattr(jobs_region, "get_zhilian_client", lambda: _FakeZhilianClient(jobs=[_zhilian_job()]))

    resp = client.post("/api/jobs/region-search", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert set(["request", "summary", "source_status", "jobs"]).issubset(body["data"].keys())
    data = body["data"]
    assert data["summary"]["total"] == 2
    assert set(data["summary"]["sources_succeeded"]) == {"zhilian", "boss_zhipin"}
    assert data["source_status"]["zhilian"]["ok"] is True
    assert data["source_status"]["boss_zhipin"]["ok"] is True


def test_yaml_style_payload_compat(client, monkeypatch):
    """使用 region_job_market_scan (1).yml 构造的字段形态，响应字段保持兼容。"""
    monkeypatch.setattr(jobs_region, "_boss_client", _FakeBossClient(jobs=[_boss_job()]))
    monkeypatch.setattr(jobs_region, "get_zhilian_client", lambda: _FakeZhilianClient(jobs=[_zhilian_job()]))

    resp = client.post("/api/jobs/region-search", json=_payload())
    assert resp.status_code == 200
    data = resp.json()["data"]
    # YAML 依赖的关键字段必须存在
    assert "sources_failed" in data["summary"]
    for source in ("zhilian", "boss_zhipin"):
        status = data["source_status"][source]
        assert set(["ok", "error", "warnings"]).issubset(status.keys())
    job = data["jobs"][0]
    for key in ("job_name", "source", "company", "salary", "location", "links"):
        assert key in job


def test_empty_keyword_returns_422_envelope(client):
    resp = client.post("/api/jobs/region-search", json=_payload(query={"keywords": ["  "], "keyword_mode": "any"}))
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == 422
    assert body["message"] == "请求参数错误"
    assert body["data"]["errors"]


def test_duplicate_sources_deduped(client, monkeypatch):
    fake_boss = _FakeBossClient(jobs=[_boss_job()])
    monkeypatch.setattr(jobs_region, "_boss_client", fake_boss)
    monkeypatch.setattr(jobs_region, "get_zhilian_client", lambda: _FakeZhilianClient(jobs=[_zhilian_job()]))

    resp = client.post("/api/jobs/region-search", json=_payload(sources=["boss_zhipin", "boss_zhipin"]))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["request"]["sources"] == ["boss_zhipin"]
    assert list(data["source_status"].keys()) == ["boss_zhipin"]
    # 去重后只跑一次
    assert len(fake_boss.calls) == 1


def test_boss_cooling_skips_client(client, monkeypatch):
    fake_boss = _FakeBossClient(jobs=[_boss_job()])
    monkeypatch.setattr(jobs_region, "_boss_client", fake_boss)
    # 预先拉闸熔断
    jobs_region._boss_circuit.trip(seconds=600, reason="测试冷却")

    resp = client.post("/api/jobs/region-search", json=_payload(sources=["boss_zhipin"]))
    # 仅 BOSS 且冷却（非重试型失败）→ 非 5xx 的 409
    assert resp.status_code == 409
    status = resp.json()["data"]["source_status"]["boss_zhipin"]
    assert status["ok"] is False
    assert status["error_code"] == "boss_cooling_down"
    assert status["retryable"] is False
    assert status["retry_after_seconds"] is not None
    # 冷却期内不触碰 BOSS 客户端
    assert fake_boss.calls == []


def test_boss_access_limited_non_retry(client, monkeypatch):
    fake_boss = _FakeBossClient(
        raises=BossAccessLimitedError(
            "访问受限",
            retry_after_seconds=1800,
            worker_status={"boss-a": {"state": "cooldown", "in_flight": 0}},
        )
    )
    monkeypatch.setattr(jobs_region, "_boss_client", fake_boss)
    monkeypatch.setattr(jobs_region, "get_zhilian_client", lambda: _FakeZhilianClient(jobs=[_zhilian_job()]))

    resp = client.post("/api/jobs/region-search", json=_payload())
    # 智联成功 → 整体 200
    assert resp.status_code == 200
    status = resp.json()["data"]["source_status"]["boss_zhipin"]
    assert status["ok"] is False
    assert status["error_code"] == "boss_access_limited"
    assert status["retryable"] is False
    assert status["retry_after_seconds"] is not None
    assert status["worker_status"]["boss-a"]["state"] == "cooldown"
    # 熔断器已开启
    is_open, _, _, _ = jobs_region._boss_circuit.state()
    assert is_open is True


def test_zhilian_all_failed_marks_not_ok(client, monkeypatch):
    summary = {"combinations": 1, "failed_combinations": 1, "pages_fetched": 0, "pages_requested": 1}
    monkeypatch.setattr(
        jobs_region,
        "get_zhilian_client",
        lambda: _FakeZhilianClient(jobs=[], summary=summary),
    )

    resp = client.post("/api/jobs/region-search", json=_payload(sources=["zhilian"]))
    # 智联失败是可重试的瞬时错误 → 503
    assert resp.status_code == 503
    status = resp.json()["data"]["source_status"]["zhilian"]
    assert status["ok"] is False
    assert status["error_code"] == "zhilian_all_failed"


def test_boss_detail_records_capped_server_side(client, monkeypatch):
    """BOSS 逐条详情（description）时，服务端对单轮记录数做安全上限，避免慢采超时。"""
    fake_boss = _FakeBossClient(jobs=[_boss_job()])
    monkeypatch.setattr(jobs_region, "_boss_client", fake_boss)
    monkeypatch.setattr(jobs_region._settings, "REGION_JOBS_BOSS_MAX_DETAIL_RECORDS", 15)

    collection = {
        "max_pages_per_source": 3,
        "max_records_per_source": 50,
        "detail_level": "description",
        "timeout_seconds": 90.0,
        "on_source_error": "continue",
    }
    resp = client.post(
        "/api/jobs/region-search",
        json=_payload(sources=["boss_zhipin"], collection=collection),
    )
    assert resp.status_code == 200
    # scrape_many 第 4 个位置参数是 max_items_per_query，应被收敛到 15（单关键词）
    args, _kwargs = fake_boss.calls[0]
    assert args[3] == 15
    warnings = resp.json()["data"]["source_status"]["boss_zhipin"]["warnings"]
    assert any("安全上限" in w for w in warnings)


def test_boss_summary_not_capped(client, monkeypatch):
    """summary 快速路径不受详情安全上限影响，仍按请求量抓取。"""
    fake_boss = _FakeBossClient(jobs=[_boss_job()])
    monkeypatch.setattr(jobs_region, "_boss_client", fake_boss)
    monkeypatch.setattr(jobs_region._settings, "REGION_JOBS_BOSS_MAX_DETAIL_RECORDS", 15)

    collection = {
        "max_pages_per_source": 3,
        "max_records_per_source": 50,
        "detail_level": "summary",
        "timeout_seconds": 90.0,
        "on_source_error": "continue",
    }
    resp = client.post(
        "/api/jobs/region-search",
        json=_payload(sources=["boss_zhipin"], collection=collection),
    )
    assert resp.status_code == 200
    args, _kwargs = fake_boss.calls[0]
    assert args[3] == 50


def test_boss_pagination_cursor_exposed_and_start_page_threaded(client, monkeypatch):
    """BOSS 透出翻页游标 total/has_more/next_page，且 start_page 透传到客户端。"""

    class _PagedBoss:
        def __init__(self):
            self.calls = []

        async def scrape_many(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            start_page = args[6]
            return {
                "summary": {
                    "combinations": 1,
                    "pages_fetched": 1,
                    "total_jobs": 1,
                    "start_page": start_page,
                    "next_page": start_page + 1,
                    "has_more": True,
                    "total_count": 300,
                    "res_count": 450,
                },
                "jobs": [_boss_job()],
                "warnings": [],
            }

    fake = _PagedBoss()
    monkeypatch.setattr(jobs_region, "_boss_client", fake)

    collection = {
        "max_pages_per_source": 1,
        "max_records_per_source": 15,
        "start_page": 3,
        "detail_level": "description",
        "timeout_seconds": 90.0,
        "on_source_error": "continue",
    }
    resp = client.post(
        "/api/jobs/region-search",
        json=_payload(sources=["boss_zhipin"], collection=collection),
    )
    assert resp.status_code == 200
    status = resp.json()["data"]["source_status"]["boss_zhipin"]
    assert status["has_more"] is True
    assert status["next_page"] == 4
    assert status["total"] == 300
    # start_page 透传（scrape_many 第 7 个位置参数）
    args, _kwargs = fake.calls[0]
    assert args[6] == 3


def test_boss_default_start_page_is_one(client, monkeypatch):
    """未显式传 start_page 时默认从第 1 页开始（外部 YAML 兼容）。"""
    fake_boss = _FakeBossClient(jobs=[_boss_job()])
    monkeypatch.setattr(jobs_region, "_boss_client", fake_boss)
    monkeypatch.setattr(jobs_region, "get_zhilian_client", lambda: _FakeZhilianClient(jobs=[_zhilian_job()]))

    resp = client.post("/api/jobs/region-search", json=_payload())
    assert resp.status_code == 200
    args, _kwargs = fake_boss.calls[0]
    assert args[6] == 1


def test_boss_worker_metadata_exposed_compatibly(client, monkeypatch):
    """BOSS worker pool metadata 作为 source_status 扩展字段透出，不破坏旧字段。"""

    class _WorkerMetaBoss:
        def __init__(self):
            self.calls = []

        async def scrape_many(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {
                "summary": {
                    "combinations": 1,
                    "pages_fetched": 1,
                    "total_jobs": 1,
                    "worker_id": "boss-a",
                    "worker_status": {
                        "boss-a": {
                            "state": "healthy",
                            "in_flight": 0,
                            "proxy_id": "boss-proxy-a",
                            "proxy_state": "leased",
                            "local_proxy_url_masked": "http://user:***@127.0.0.1:18081",
                        },
                    },
                },
                "jobs": [_boss_job()],
                "warnings": [],
            }

    monkeypatch.setattr(jobs_region, "_boss_client", _WorkerMetaBoss())

    resp = client.post("/api/jobs/region-search", json=_payload(sources=["boss_zhipin"]))

    assert resp.status_code == 200
    status = resp.json()["data"]["source_status"]["boss_zhipin"]
    assert status["ok"] is True
    assert status["worker_id"] == "boss-a"
    assert status["worker_status"]["boss-a"]["state"] == "healthy"
    assert status["worker_status"]["boss-a"]["proxy_id"] == "boss-proxy-a"
    assert status["worker_status"]["boss-a"]["proxy_state"] == "leased"
    assert "secret" not in status["worker_status"]["boss-a"]["local_proxy_url_masked"]
    assert set(["ok", "error", "warnings"]).issubset(status.keys())


def test_boss_generic_error_exposes_worker_status(client, monkeypatch):
    """BOSS 通用异常也要带 worker_status，便于灰度定位是哪一个 worker 异常。"""

    class _ErrorBoss:
        async def scrape_many(self, *args, **kwargs):
            raise RuntimeError("浏览器异常")

        def worker_status(self):
            return {"boss-a": {"state": "healthy", "in_flight": 0}}

    monkeypatch.setattr(jobs_region, "_boss_client", _ErrorBoss())

    resp = client.post("/api/jobs/region-search", json=_payload(sources=["boss_zhipin"]))

    assert resp.status_code == 503
    status = resp.json()["data"]["source_status"]["boss_zhipin"]
    assert status["error_code"] == "boss_error"
    assert status["worker_status"]["boss-a"]["state"] == "healthy"


def test_on_source_error_fail_returns_non_retry_code(client, monkeypatch):
    fake_boss = _FakeBossClient(raises=BossAccessLimitedError("访问受限"))
    monkeypatch.setattr(jobs_region, "_boss_client", fake_boss)
    monkeypatch.setattr(jobs_region, "get_zhilian_client", lambda: _FakeZhilianClient(jobs=[_zhilian_job()]))

    collection = {
        "max_pages_per_source": 1,
        "max_records_per_source": 20,
        "detail_level": "summary",
        "timeout_seconds": 90.0,
        "on_source_error": "fail",
    }
    resp = client.post("/api/jobs/region-search", json=_payload(collection=collection))
    # fail 模式下只要有失败即整体失败；失败源为 BOSS 风控（非重试）→ 409
    assert resp.status_code == 409
    data = resp.json()["data"]
    assert data["source_status"]["boss_zhipin"]["ok"] is False
    assert "boss_zhipin" in data["summary"]["sources_failed"]
