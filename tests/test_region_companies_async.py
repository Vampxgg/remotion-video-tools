# -*- coding: utf-8 -*-
"""区域企业调研异步化相关单元测试。

覆盖三块新逻辑（不依赖真实 DB / Redis）：
- ``_enrich_company_details`` 并发补详情：并发拉取不超信号量、串行落库、额度截断、
  priority 优先保留、单家失败只告警不中断。
- ``services.tianyancha_jobs`` job 存储：create/mark/read 全流程（Redis 未就绪走内存兜底）。
- ``api.tianyancha`` 异步端点：``_run_region_job`` 成功/失败落库、result long-poll 状态映射。
"""

import asyncio

import pytest

from services.tianyancha_client import TianyanchaAPIError, TianyanchaClient


# ─────────────── 假对象 ───────────────

class _FakeCompany:
    def __init__(self, cid, name, *, fetched=False):
        self.id = cid
        self.name = name
        self.tianyancha_id = cid
        self.credit_code = f"C{cid}"
        # 无 baseinfo_fetched_at → _needs_baseinfo_refresh 返回 True
        self.baseinfo_fetched_at = "2020-01-01T00:00:00" if fetched else None


class _FakeDB:
    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def close(self):
        return None


# ─────────────── _enrich_company_details 并发补详情 ───────────────

def _install_enrich_stubs(monkeypatch, client, *, concurrency_probe=None, fail_ids=None):
    fail_ids = fail_ids or set()
    inflight = {"cur": 0, "max": 0}

    async def fake_needs(company):
        return None  # 不用；见下方直接 patch 方法

    async def fake_fetch_baseinfo(keyword):
        # 记录并发峰值
        inflight["cur"] += 1
        inflight["max"] = max(inflight["max"], inflight["cur"])
        try:
            await asyncio.sleep(0.02)
            cid = int(keyword)
            if cid in fail_ids:
                raise TianyanchaAPIError(300000, "无数据")
            return {"id": cid, "name": f"n{cid}", "credit_code": f"C{cid}"}
        finally:
            inflight["cur"] -= 1

    upserted = []

    async def fake_upsert(db, detail, *, fetched_at):
        upserted.append(detail["id"])
        return _FakeCompany(detail["id"], detail["name"], fetched=True), False

    monkeypatch.setattr(client, "fetch_baseinfo", fake_fetch_baseinfo)
    monkeypatch.setattr(client, "upsert_company_from_baseinfo", fake_upsert)
    # 所有企业都视为需要补详情
    monkeypatch.setattr(client, "_needs_baseinfo_refresh", lambda c: True)
    if concurrency_probe is not None:
        concurrency_probe.update(inflight)
    return inflight, upserted


@pytest.mark.asyncio
async def test_enrich_concurrent_not_exceed_semaphore(monkeypatch):
    """并发补详情：同时在飞的请求数不超过 TIANYANCHA_DETAIL_CONCURRENCY。"""
    from utils.settings import settings as _settings
    monkeypatch.setattr(_settings, "TIANYANCHA_DETAIL_CONCURRENCY", 3, raising=False)

    client = TianyanchaClient()
    inflight, upserted = _install_enrich_stubs(monkeypatch, client)
    companies = [_FakeCompany(i, f"c{i}") for i in range(1, 11)]

    calls, warnings = await client._enrich_company_details(
        _FakeDB(), companies,
        enrich_detail=True, refresh_detail=False, max_detail_calls=None,
        max_allowed_detail_calls=20,
    )
    assert calls == 10
    assert inflight["max"] <= 3
    assert len(upserted) == 10


@pytest.mark.asyncio
async def test_enrich_respects_detail_limit(monkeypatch):
    """额度截断：max_detail_calls 限制实际补详情数，并给出 warning。"""
    client = TianyanchaClient()
    _install_enrich_stubs(monkeypatch, client)
    companies = [_FakeCompany(i, f"c{i}") for i in range(1, 11)]

    calls, warnings = await client._enrich_company_details(
        _FakeDB(), companies,
        enrich_detail=True, refresh_detail=False, max_detail_calls=4,
        max_allowed_detail_calls=20,
    )
    assert calls == 4
    assert any("额度不足" in w for w in warnings)


@pytest.mark.asyncio
async def test_enrich_priority_kept_when_truncated(monkeypatch):
    """priority 企业在额度截断时优先保留（排在候选前）。"""
    client = TianyanchaClient()
    _, upserted = _install_enrich_stubs(monkeypatch, client)
    companies = [_FakeCompany(i, f"c{i}") for i in range(1, 11)]

    # 指定 id 9,10 为优先 → 截断到 2 家时应优先补 9,10
    calls, _ = await client._enrich_company_details(
        _FakeDB(), companies,
        enrich_detail=True, refresh_detail=False, max_detail_calls=2,
        max_allowed_detail_calls=20,
        priority_company_ids={9, 10},
    )
    assert calls == 2
    assert set(upserted) == {9, 10}


@pytest.mark.asyncio
async def test_enrich_single_failure_only_warns(monkeypatch):
    """单家详情失败只记 warning，不影响其它企业补详情。"""
    client = TianyanchaClient()
    _, upserted = _install_enrich_stubs(monkeypatch, client, fail_ids={2})
    companies = [_FakeCompany(i, f"c{i}") for i in range(1, 6)]

    calls, warnings = await client._enrich_company_details(
        _FakeDB(), companies,
        enrich_detail=True, refresh_detail=False, max_detail_calls=None,
        max_allowed_detail_calls=20,
    )
    assert calls == 4  # 5 家里 1 家失败
    assert 2 not in upserted
    assert any("详情补拉失败" in w for w in warnings)


# ─────────────── tianyancha_jobs 存储（Redis 未就绪 → 内存兜底） ───────────────

@pytest.mark.asyncio
async def test_job_store_lifecycle_memory_fallback(monkeypatch):
    """Redis 未就绪时走内存兜底，create/mark/read 全流程可用。"""
    from services import tianyancha_jobs as jobs
    from utils import redis_client
    monkeypatch.setattr(redis_client, "get_redis", lambda: None)

    job_id = await jobs.create_job({"region": "上海", "limit": 20})
    job = await jobs.read_job(job_id)
    assert job["status"] == jobs.STATUS_PENDING
    assert job["params"]["region"] == "上海"

    await jobs.mark_running(job_id)
    assert (await jobs.read_job(job_id))["status"] == jobs.STATUS_RUNNING

    await jobs.mark_succeeded(job_id, {"companies": [{"id": 1}]})
    done = await jobs.read_job(job_id)
    assert done["status"] == jobs.STATUS_SUCCEEDED
    assert done["result"]["companies"] == [{"id": 1}]


@pytest.mark.asyncio
async def test_job_store_mark_failed(monkeypatch):
    from services import tianyancha_jobs as jobs
    from utils import redis_client
    monkeypatch.setattr(redis_client, "get_redis", lambda: None)

    job_id = await jobs.create_job({"region": "深圳"})
    await jobs.mark_failed(job_id, 502, "天眼查网络请求失败")
    job = await jobs.read_job(job_id)
    assert job["status"] == jobs.STATUS_FAILED
    assert job["error"]["code"] == 502


# ─────────────── 异步端点：_run_region_job + result 状态映射 ───────────────

@pytest.mark.asyncio
async def test_run_region_job_success(monkeypatch):
    """后台任务成功 → job 落 succeeded，result 为 research 返回的 data。"""
    import api.tianyancha as ty
    from services import tianyancha_jobs as jobs
    from utils import redis_client
    monkeypatch.setattr(redis_client, "get_redis", lambda: None)

    # 独立 session 工厂打桩为假 DB
    monkeypatch.setattr(ty, "AsyncSessionLocal", lambda: _AsyncCtxDB())

    async def fake_research(db, **kwargs):
        return {"need_clarification": False, "companies": [{"id": 1}], "warnings": []}
    monkeypatch.setattr(ty._client, "research_region_companies", fake_research)

    payload = ty.RegionCompanyResearchPayload(region="上海市", limit=20)
    job_id = await jobs.create_job(payload.model_dump(mode="json"))
    await ty._run_region_job(job_id, payload)

    job = await jobs.read_job(job_id)
    assert job["status"] == jobs.STATUS_SUCCEEDED
    assert job["result"]["companies"] == [{"id": 1}]


@pytest.mark.asyncio
async def test_run_region_job_failure_maps_error(monkeypatch):
    """后台任务抛天眼查错误 → job 落 failed，错误码经 _error_parts 映射。"""
    import api.tianyancha as ty
    from services import tianyancha_jobs as jobs
    from utils import redis_client
    monkeypatch.setattr(redis_client, "get_redis", lambda: None)
    monkeypatch.setattr(ty, "AsyncSessionLocal", lambda: _AsyncCtxDB())

    async def fake_research(db, **kwargs):
        raise TianyanchaAPIError(300006, "余额不足")
    monkeypatch.setattr(ty._client, "research_region_companies", fake_research)

    payload = ty.RegionCompanyResearchPayload(region="上海市", limit=20)
    job_id = await jobs.create_job(payload.model_dump(mode="json"))
    await ty._run_region_job(job_id, payload)

    job = await jobs.read_job(job_id)
    assert job["status"] == jobs.STATUS_FAILED
    # 300006 → 402
    assert job["error"]["code"] == 402
    assert job["error"]["data"]["tianyancha_error_code"] == 300006


class _AsyncCtxDB:
    """async with AsyncSessionLocal() as db 的最小替身。"""
    async def __aenter__(self):
        return _FakeDB()

    async def __aexit__(self, *a):
        return False
