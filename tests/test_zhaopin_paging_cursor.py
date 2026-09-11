# -*- coding: utf-8 -*-
"""智联跨轮游标翻页：验证 scrape_many/scrape_combination 逐页推进与游标透出。

覆盖点：
- 单组合场景下 scrape_many 返回 {"jobs", "summary"}，summary 精确携带
  total_count / next_page / has_more。
- 模拟 count=145、pageSize=20：每轮 1 页，start_page 从 1 递增，has_more 在
  page<8 时为 True，page 8（isEndPage=1）为 False。
- 游标信息经返回值上浮，不再依赖实例属性；并发调用互不串写。
"""

import asyncio
from typing import Any, Dict, List, Tuple

import pytest

from services.zhaopin_client import ZhaopinSearchClient


TOTAL_COUNT = 145
PAGE_SIZE = 20
LAST_PAGE = 8  # ceil(145 / 20)
MAX_PAGES_PER_ROUND = 1  # data_server 对智联恒传 max_pages_per_source=1


def _make_client() -> ZhaopinSearchClient:
    """构造一个不触碰浏览器/网络的 client：override 掉外部依赖方法。"""
    client = ZhaopinSearchClient(browser_pool=object())  # type: ignore[arg-type]

    async def _fake_resolve(province: str):
        return "530"  # 广州 cityId 占位

    client._city.resolve = _fake_resolve  # type: ignore[assignment]

    async def _fake_search_positions(
        keyword: str, city_id: str, *, page: int = 1, filters=None
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """模拟直连列表：按 count=145/pageSize=20 分页。"""
        start_index = (page - 1) * PAGE_SIZE
        remaining = TOTAL_COUNT - start_index
        n = max(0, min(PAGE_SIZE, remaining))
        jobs = [
            {"number": f"zl-{start_index + i}", "name": keyword}
            for i in range(n)
        ]
        is_end = page >= LAST_PAGE
        meta = {"count": TOTAL_COUNT, "isEndPage": 1 if is_end else 0, "isVerification": 0}
        return jobs, meta

    client.search_positions = _fake_search_positions  # type: ignore[assignment]

    async def _fake_fetch_detail(position_number: str):
        return {"positionNumber": position_number}

    client.fetch_detail = _fake_fetch_detail  # type: ignore[assignment]
    return client


@pytest.mark.asyncio
async def test_scrape_many_single_combination_exposes_cursor():
    """单组合：每轮 1 页，summary 精确透出 total_count/next_page/has_more。"""
    client = _make_client()

    # 第 1 轮：start_page=1 → 拿 p1，next_page=2，has_more=True
    result = await client.scrape_many(
        ["食品检验"], ["广州"], MAX_PAGES_PER_ROUND, include_detail=False, start_page=1
    )
    assert isinstance(result, dict)
    summary = result["summary"]
    assert len(result["jobs"]) == PAGE_SIZE
    assert summary["total_count"] == TOTAL_COUNT
    assert summary["next_page"] == 2
    assert summary["has_more"] is True
    assert summary["start_page"] == 1


@pytest.mark.asyncio
async def test_cross_round_cursor_accumulates_to_end():
    """跨轮：start_page 从 next_page 递推，累积覆盖全部 145 条并在 p8 停止。"""
    client = _make_client()

    seen_numbers: set[str] = set()
    start_page = 1
    rounds = 0
    while True:
        rounds += 1
        result = await client.scrape_many(
            ["食品检验"], ["广州"], MAX_PAGES_PER_ROUND, include_detail=False, start_page=start_page
        )
        summary = result["summary"]
        for job in result["jobs"]:
            # 无重复落库：跨轮不应重复同一 positionNumber
            assert job["positionNumber"] not in seen_numbers
            seen_numbers.add(job["positionNumber"])
        if not summary["has_more"]:
            break
        assert summary["next_page"] == start_page + 1
        start_page = summary["next_page"]
        assert rounds <= LAST_PAGE + 2  # 防死循环

    assert rounds == LAST_PAGE
    assert len(seen_numbers) == TOTAL_COUNT  # 覆盖到第 60 条以后直至 145


@pytest.mark.asyncio
async def test_last_page_has_more_false_at_end_page():
    """直接从末页开始：isEndPage=1 → has_more=False（触发耗尽）。"""
    client = _make_client()
    result = await client.scrape_many(
        ["食品检验"], ["广州"], MAX_PAGES_PER_ROUND, include_detail=False, start_page=LAST_PAGE
    )
    summary = result["summary"]
    assert summary["has_more"] is False
    assert len(result["jobs"]) == TOTAL_COUNT - (LAST_PAGE - 1) * PAGE_SIZE  # 5


@pytest.mark.asyncio
async def test_concurrent_scrape_many_no_cross_write():
    """并发调用同一 client：游标经返回值上浮，互不串写。"""
    client = _make_client()

    async def _one(start_page: int) -> Dict[str, Any]:
        res = await client.scrape_many(
            ["食品检验"], ["广州"], MAX_PAGES_PER_ROUND, include_detail=False, start_page=start_page
        )
        return res["summary"]

    # 并发用不同 start_page，各自结果应与自身 start_page 严格对应
    summaries = await asyncio.gather(*(_one(sp) for sp in range(1, LAST_PAGE + 1)))
    for i, summary in enumerate(summaries, start=1):
        assert summary["start_page"] == i
        assert summary["next_page"] == i + 1
        expected_has_more = i < LAST_PAGE
        assert summary["has_more"] is expected_has_more
