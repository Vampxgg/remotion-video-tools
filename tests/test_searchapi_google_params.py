# -*- coding: utf-8 -*-
"""searchapi_google provider 参数构造回归。

根因回归：hl 语言码曾被 split("-")[0] 截成 "zh"，导致 SearchAPI google engine
返回 400 "Unsupported value `zh` in hl parameter"，进而 all_providers_failed→502。
hl 必须保留完整语言码（如 zh-cn）。
"""

from schemas.web_search import (
    SearchAPIGoogleOverrides,
    WebSearchCommon,
    WebSearchLocale,
    WebSearchTimeRange,
)
from services.web_search.searchapi_google_provider import _build_params


def _common(language: str = "zh-CN") -> WebSearchCommon:
    return WebSearchCommon(
        query="食品安全国家标准 公告",
        top_k=5,
        time_range=WebSearchTimeRange.ANY,
        include_domains=["nhc.gov.cn"],
        exclude_domains=[],
        locale=WebSearchLocale(country="cn", language=language),
        safe_search=False,
    )


def test_hl_keeps_full_language_code_not_truncated():
    params = _build_params(_common("zh-CN"), SearchAPIGoogleOverrides(engine="google"))
    # 关键断言：hl 必须是完整 zh-cn，而不是被截断的 zh（后者会被 google engine 400 拒绝）
    assert params["hl"] == "zh-cn"
    assert params["gl"] == "cn"


def test_hl_plain_language_stays_plain():
    params = _build_params(_common("en"), SearchAPIGoogleOverrides(engine="google"))
    assert params["hl"] == "en"
