# -*- coding: utf-8 -*-
"""天眼查企业池语义单元测试。

覆盖企业池改造的核心可验证逻辑（不依赖数据库）：
- ``build_pool_fingerprint``：三元组指纹稳定、且与五元组分页指纹不碰撞。
- ``plan_pool_fetch``：池状态 → 取数决策的全部分支。
"""

from services.tianyancha_client import (
    TianyanchaAPIError,
    TianyanchaClient,
    build_pool_fingerprint,
    build_search_fingerprint,
    plan_pool_fetch,
)
import pytest


# ─────────────── build_pool_fingerprint ───────────────

def test_pool_fingerprint_stable_and_order_independent():
    fp1 = build_pool_fingerprint(word="新能源", category_guobiao="65", area_code="310100")
    fp2 = build_pool_fingerprint(area_code="310100", word="新能源", category_guobiao="65")
    assert fp1 == fp2


def test_pool_fingerprint_ignores_pagination_by_construction():
    """三元组指纹只吃 word/area/category，天然不含分页。"""
    fp_a = build_pool_fingerprint(word="新能源", category_guobiao="65", area_code="310100")
    fp_b = build_pool_fingerprint(word="新能源", category_guobiao="65", area_code="310100")
    assert fp_a == fp_b


def test_pool_and_paged_fingerprints_never_collide():
    """池指纹（三元组）与分页指纹（五元组）落在同一张表，必须永不碰撞。"""
    pool_fp = build_pool_fingerprint(word="新能源", category_guobiao="65", area_code="310100")
    paged_fp = build_search_fingerprint(
        {"word": "新能源", "categoryGuobiao": "65", "areaCode": "310100", "pageNum": 1, "pageSize": 20}
    )
    assert pool_fp != paged_fp


def test_pool_fingerprint_differs_by_word():
    fp1 = build_pool_fingerprint(word="新能源", category_guobiao="65", area_code="310100")
    fp2 = build_pool_fingerprint(word="光伏", category_guobiao="65", area_code="310100")
    assert fp1 != fp2


# ─────────────── plan_pool_fetch ───────────────

def _plan(**overrides):
    base = dict(
        pool_size=0,
        need=20,
        max_page_fetched=0,
        exhausted=False,
        total=None,
        page_size=20,
        max_pages_per_request=5,
    )
    base.update(overrides)
    return plan_pool_fetch(**base)


def test_plan_hit_when_pool_already_enough():
    """池已够 need → 不打远程。"""
    plan = _plan(pool_size=25, need=20)
    assert plan["remote_needed"] is False


def test_plan_no_remote_when_exhausted():
    """池翻无可翻 → 即便不够也不打远程。"""
    plan = _plan(pool_size=8, need=20, exhausted=True, max_page_fetched=3)
    assert plan["remote_needed"] is False


def test_plan_first_fetch_from_page_one():
    """空池 → 从第 1 页开始翻。"""
    plan = _plan(pool_size=0, need=20, max_page_fetched=0, page_size=20)
    assert plan["remote_needed"] is True
    assert plan["start_page"] == 1
    # 需要 20 条、每页 20 → 1 页
    assert plan["end_page"] == 1


def test_plan_resume_from_next_page():
    """已翻到第 2 页、池里 30 条仍不够 60 → 从第 3 页续翻。"""
    plan = _plan(pool_size=30, need=60, max_page_fetched=2, page_size=20)
    assert plan["remote_needed"] is True
    assert plan["start_page"] == 3
    # 还差 30 条 → 2 页 → 翻到第 4 页
    assert plan["end_page"] == 4


def test_plan_capped_by_max_pages_per_request():
    """单次翻页数受 max_pages_per_request 限制。"""
    plan = _plan(pool_size=0, need=200, page_size=20, max_pages_per_request=3)
    assert plan["start_page"] == 1
    assert plan["end_page"] == 3  # 而非 10 页


def test_plan_capped_by_total_pages():
    """不超过 total 推算的总页数。total=25、page_size=20 → 只有 2 页。"""
    plan = _plan(pool_size=0, need=200, total=25, page_size=20, max_pages_per_request=5)
    assert plan["start_page"] == 1
    assert plan["end_page"] == 2


def test_plan_no_remote_when_past_total_pages():
    """已翻过 total 全部页 → 不再打远程。total=20→1 页，已翻 1 页。"""
    plan = _plan(pool_size=20, need=200, total=20, max_page_fetched=1, page_size=20)
    assert plan["remote_needed"] is False


# ─────────────── search_company_pool 编排（假 DB） ───────────────

class _FakePoolRecord:
    def __init__(self, *, company_ids=None, max_page_fetched=0, exhausted=False, total=None):
        self.company_ids = list(company_ids or [])
        self.max_page_fetched = max_page_fetched
        self.exhausted = exhausted
        self.total = total


class _FakeDB:
    """只提供 search_company_pool 用到的 commit / rollback（async no-op）。"""
    async def commit(self):
        return None

    async def rollback(self):
        return None


class _Company:
    def __init__(self, cid):
        self.id = cid


def _install_pool_stubs(monkeypatch, client, *, initial_pool, pages):
    """给 client 打桩：池读/写、按页返回企业、加载企业、序列化。

    ``pages`` 是 ``page_num -> {"companies": [id...], "total": int}`` 的字典，
    模拟 search_companies 逐页远程返回。upsert 会把最终状态写回 captured。
    """
    state = {"pool": initial_pool, "captured": None}

    async def fake_get_pool_query(db, fingerprint):
        return state["pool"]

    async def fake_search_companies(db, *, page_num, **kwargs):
        page = pages.get(page_num, {"companies": [], "total": 0})
        err_code = page.get("error")
        if err_code is not None:
            raise TianyanchaAPIError(err_code, "")
        return {
            "remote_called": True,
            "detail_remote_calls": 0,
            "warnings": [],
            "total": page.get("total", 0),
            "companies": [{"id": cid} for cid in page["companies"]],
        }

    async def fake_upsert_pool_query(db, **kwargs):
        state["captured"] = kwargs
        return _FakePoolRecord(
            company_ids=kwargs["company_ids"],
            max_page_fetched=kwargs["max_page_fetched"],
            exhausted=kwargs["exhausted"],
            total=kwargs["total"],
        )

    async def fake_load_companies_by_ids(db, company_ids):
        return [_Company(cid) for cid in company_ids]

    monkeypatch.setattr(client, "_get_pool_query", fake_get_pool_query)
    monkeypatch.setattr(client, "search_companies", fake_search_companies)
    monkeypatch.setattr(client, "_upsert_pool_query", fake_upsert_pool_query)
    monkeypatch.setattr(client, "_load_companies_by_ids", fake_load_companies_by_ids)
    monkeypatch.setattr(client, "company_to_dict", lambda c, **kw: {"id": c.id})
    return state


@pytest.mark.asyncio
async def test_pool_hit_no_remote_when_enough(monkeypatch):
    """池已够 need → 0 远程调用，直接返回缓存。"""
    client = TianyanchaClient()
    pool = _FakePoolRecord(company_ids=[1, 2, 3, 4, 5], max_page_fetched=1, total=5)
    _install_pool_stubs(monkeypatch, client, initial_pool=pool, pages={})

    result = await client.search_company_pool(
        _FakeDB(), word="新能源", category_guobiao="65", area_code="310100", need=5,
    )
    assert result["remote_search_calls"] == 0
    assert result["cache_hit"] is True
    assert len(result["companies"]) == 5


@pytest.mark.asyncio
async def test_pool_first_fetch_accumulates(monkeypatch):
    """空池 → 从第 1 页翻，企业写入池。"""
    client = TianyanchaClient()
    pages = {1: {"companies": list(range(1, 21)), "total": 100}}
    state = _install_pool_stubs(monkeypatch, client, initial_pool=None, pages=pages)

    result = await client.search_company_pool(
        _FakeDB(), word="新能源", category_guobiao="65", area_code="310100", need=20,
    )
    assert result["remote_search_calls"] == 1
    assert result["pool_size"] == 20
    assert state["captured"]["max_page_fetched"] == 1
    assert state["captured"]["exhausted"] is False


@pytest.mark.asyncio
async def test_pool_resume_and_union_dedup(monkeypatch):
    """已翻 1 页（池 20 条），need=30 → 续翻第 2 页，重叠 id 去重。"""
    client = TianyanchaClient()
    pool = _FakePoolRecord(company_ids=list(range(1, 21)), max_page_fetched=1, total=100)
    # 第 2 页故意含与第 1 页重叠的 id(15..20) + 新 id(21..34)
    pages = {2: {"companies": list(range(15, 35)), "total": 100}}
    state = _install_pool_stubs(monkeypatch, client, initial_pool=pool, pages=pages)

    result = await client.search_company_pool(
        _FakeDB(), word="新能源", category_guobiao="65", area_code="310100", need=30,
    )
    assert result["remote_search_calls"] == 1
    # 并集去重：1..34 共 34 个唯一 id
    assert result["pool_size"] == 34
    assert state["captured"]["max_page_fetched"] == 2


@pytest.mark.asyncio
async def test_pool_marks_exhausted_on_short_page(monkeypatch):
    """本页不足一整页 → 标记 exhausted。"""
    client = TianyanchaClient()
    pages = {1: {"companies": [1, 2, 3], "total": 3}}
    state = _install_pool_stubs(monkeypatch, client, initial_pool=None, pages=pages)

    result = await client.search_company_pool(
        _FakeDB(), word="冷门词", category_guobiao=None, area_code="310100", need=20,
    )
    assert result["exhausted"] is True
    assert state["captured"]["exhausted"] is True


@pytest.mark.asyncio
async def test_pool_no_remote_when_exhausted(monkeypatch):
    """池已 exhausted 且不足 need → 不再远程，返回现有池。"""
    client = TianyanchaClient()
    pool = _FakePoolRecord(company_ids=[1, 2, 3], max_page_fetched=1, exhausted=True, total=3)
    _install_pool_stubs(monkeypatch, client, initial_pool=pool, pages={})

    result = await client.search_company_pool(
        _FakeDB(), word="冷门词", category_guobiao=None, area_code="310100", need=20,
    )
    assert result["remote_search_calls"] == 0
    assert result["pool_size"] == 3


# ─────────────── 远程受限降级（402/429 → 本地缓存） ───────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", [300004, 300006, 300007])
async def test_pool_degrades_to_cache_on_quota_error(monkeypatch, error_code):
    """池已有缓存但不足 need，续翻时远程返回配额/限流类错误 →
    降级返回已有缓存企业，而非让整个调用因 402/429 失败。"""
    client = TianyanchaClient()
    pool = _FakePoolRecord(company_ids=[1, 2, 3], max_page_fetched=1, total=100)
    pages = {2: {"error": error_code}}
    state = _install_pool_stubs(monkeypatch, client, initial_pool=pool, pages=pages)

    result = await client.search_company_pool(
        _FakeDB(), word="新能源", category_guobiao="65", area_code="310100", need=20,
    )
    # 缓存里的 3 家企业照常带出，未因远程受限而丢失
    assert [c["id"] for c in result["companies"]] == [1, 2, 3]
    assert result["pool_size"] == 3
    assert any("降级" in w for w in result["warnings"])
    # 已有积累 → 仍持久化进度，便于配额恢复后断点续翻，且不误标 exhausted
    assert state["captured"] is not None
    assert state["captured"]["exhausted"] is False


@pytest.mark.asyncio
async def test_pool_first_fetch_quota_error_returns_empty_without_polluting(monkeypatch):
    """首次调用（空池）第一页即遇配额错误 → 返回空企业列表 + warning，
    且不写入误导性的空池缓存记录。"""
    client = TianyanchaClient()
    pages = {1: {"error": 300006}}
    state = _install_pool_stubs(monkeypatch, client, initial_pool=None, pages=pages)

    result = await client.search_company_pool(
        _FakeDB(), word="新能源", category_guobiao="65", area_code="310100", need=20,
    )
    assert result["companies"] == []
    assert result["pool_size"] == 0
    assert any("降级" in w for w in result["warnings"])
    # 空池 + 降级 → 跳过写入，避免污染后续命中
    assert state["captured"] is None


@pytest.mark.asyncio
async def test_pool_reraises_non_quota_error(monkeypatch):
    """非配额类错误（如账号失效 300002）不降级，照常抛出。"""
    client = TianyanchaClient()
    pages = {1: {"error": 300002}}
    _install_pool_stubs(monkeypatch, client, initial_pool=None, pages=pages)

    with pytest.raises(TianyanchaAPIError) as exc_info:
        await client.search_company_pool(
            _FakeDB(), word="新能源", category_guobiao="65", area_code="310100", need=20,
        )
    assert exc_info.value.error_code == 300002


# ─────────────── exhaustive 穷尽模式（全量建档） ───────────────

@pytest.mark.asyncio
async def test_pool_exhaustive_fetches_all_pages_across_batches(monkeypatch):
    """穷尽模式：忽略 need，跨多批续翻直到短页，拿全量企业并标记 exhausted。

    构造 7 满页 + 第 8 页短页；max_pages_per_request=5 → 需要 2 批
    （1..5 页、6..8 页）才能翻完。
    """
    client = TianyanchaClient()
    pages = {n: {"companies": list(range((n - 1) * 20 + 1, n * 20 + 1)), "total": 148}
             for n in range(1, 8)}  # 1..7 满页
    pages[8] = {"companies": list(range(141, 149)), "total": 148}  # 第 8 页 8 条（短页）
    state = _install_pool_stubs(monkeypatch, client, initial_pool=None, pages=pages)

    result = await client.search_company_pool(
        _FakeDB(), word="新能源", category_guobiao="65", area_code="310100",
        need=20, exhaustive=True,
    )
    # need=20 但穷尽模式忽略它，翻完全部 148 家
    assert result["pool_size"] == 148
    assert result["exhausted"] is True
    # 返回不被 need 截断，全量带出
    assert len(result["companies"]) == 148
    # 跨批：7 满页 + 1 短页 = 8 次远程搜索
    assert result["remote_search_calls"] == 8
    assert state["captured"]["exhausted"] is True


@pytest.mark.asyncio
async def test_pool_exhaustive_stops_at_total_pages(monkeypatch):
    """穷尽模式：整页恰好翻到 total 推算总页数即停，不无限翻页。

    total=40、page_size=20 → 恰好 2 满页，无短页，仍应标记 exhausted 并停止。
    """
    client = TianyanchaClient()
    pages = {
        1: {"companies": list(range(1, 21)), "total": 40},
        2: {"companies": list(range(21, 41)), "total": 40},
    }
    _install_pool_stubs(monkeypatch, client, initial_pool=None, pages=pages)

    result = await client.search_company_pool(
        _FakeDB(), word="新能源", category_guobiao="65", area_code="310100",
        need=20, exhaustive=True,
    )
    assert result["pool_size"] == 40
    assert result["exhausted"] is True
    assert result["remote_search_calls"] == 2


@pytest.mark.asyncio
async def test_pool_exhaustive_resumes_from_exhausted_pool_no_remote(monkeypatch):
    """穷尽模式命中已 exhausted 的池 → 0 远程，直接返回全量缓存。"""
    client = TianyanchaClient()
    pool = _FakePoolRecord(
        company_ids=list(range(1, 41)), max_page_fetched=2, exhausted=True, total=40,
    )
    _install_pool_stubs(monkeypatch, client, initial_pool=pool, pages={})

    result = await client.search_company_pool(
        _FakeDB(), word="新能源", category_guobiao="65", area_code="310100",
        need=20, exhaustive=True,
    )
    assert result["remote_search_calls"] == 0
    assert result["pool_size"] == 40
    assert len(result["companies"]) == 40


# ─────────────── research_region_companies：detail_level 投影 + exhaustive 透传 ───────────────

def _install_research_stubs(monkeypatch, client, *, companies, exhaustive_seen):
    """给 research_region_companies 打桩：区域/行业解析直通，pool 返回带完整字段的企业。"""
    async def fake_resolve_area(region):
        return "310100", []

    async def fake_resolve_category(industry):
        return None, []

    async def fake_search_company_pool(db, *, exhaustive=False, **kwargs):
        exhaustive_seen.append(exhaustive)
        return {
            "cache_hit": False,
            "remote_search_calls": 1,
            "detail_remote_calls": len(companies),
            "pool_size": len(companies),
            "max_page_fetched": 1,
            "exhausted": True,
            "total": len(companies),
            "companies": companies,
            "warnings": [],
        }

    monkeypatch.setattr(client, "resolve_area_code", fake_resolve_area)
    monkeypatch.setattr(client, "resolve_category_code", fake_resolve_category)
    monkeypatch.setattr(client, "search_company_pool", fake_search_company_pool)

    async def fake_fallback(db, **kwargs):
        return []

    monkeypatch.setattr(client, "_fallback_local_region_search", fake_fallback)


@pytest.mark.asyncio
async def test_research_summary_projects_lean_fields(monkeypatch):
    """detail_level=summary → 返回精简字段（不含 business_scope 等详情字段）。"""
    client = TianyanchaClient()
    full = {
        "id": 1, "name": "某公司", "credit_code": "X1", "reg_status": "存续",
        "business_scope": "研发销售", "staff_num_range": "50-99",
        "category_code_third": "3841", "baseinfo_fetched_at": "2026-01-01T00:00:00",
    }
    seen = []
    _install_research_stubs(monkeypatch, client, companies=[full], exhaustive_seen=seen)

    data = await client.research_region_companies(
        _FakeDB(), region="上海", industry=None, keywords=["新能源"],
        limit=20, detail_level="summary", force_remote=False,
    )
    c = data["companies"][0]
    assert c["name"] == "某公司"
    assert "business_scope" not in c
    assert "staff_num_range" not in c


@pytest.mark.asyncio
async def test_research_baseinfo_returns_full_fields(monkeypatch):
    """detail_level=baseinfo → 返回完整字段（含详情）。"""
    client = TianyanchaClient()
    full = {
        "id": 1, "name": "某公司", "credit_code": "X1",
        "business_scope": "研发销售", "baseinfo_fetched_at": "2026-01-01T00:00:00",
    }
    seen = []
    _install_research_stubs(monkeypatch, client, companies=[full], exhaustive_seen=seen)

    data = await client.research_region_companies(
        _FakeDB(), region="上海", industry=None, keywords=["新能源"],
        limit=20, detail_level="baseinfo", force_remote=False,
    )
    c = data["companies"][0]
    assert c["business_scope"] == "研发销售"


@pytest.mark.asyncio
async def test_research_passes_exhaustive_to_pool(monkeypatch):
    """exhaustive=True 透传到 search_company_pool。"""
    client = TianyanchaClient()
    seen = []
    _install_research_stubs(
        monkeypatch, client, companies=[{"id": 1, "name": "n"}], exhaustive_seen=seen,
    )
    await client.research_region_companies(
        _FakeDB(), region="上海", industry=None, keywords=["新能源"],
        limit=20, detail_level="baseinfo", force_remote=False, exhaustive=True,
    )
    assert seen == [True]

