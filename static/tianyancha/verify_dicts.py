# -*- coding: utf-8 -*-
"""端到端验证：TianyanchaClient 是否已从本地静态字典正确加载并解析地区/行业码。

不触网、不碰 DB、不消耗天眼查余额，仅走 _load_area_codes/_load_categories + resolve_*。

用法（在 script_tools 根目录）：
    python static/tianyancha/verify_dicts.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 允许从 static/tianyancha 子目录运行时导入项目根的 services 包。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services.tianyancha_client import TianyanchaClient

AREA_CASES = [
    "新疆维吾尔自治区", "乌鲁木齐市", "克拉玛依市", "吐鲁番市", "哈密市",
    "伊犁哈萨克自治州", "喀什地区", "阿克苏地区", "昌吉回族自治州",
    "深圳市", "北京市", "广东省",
]
CATEGORY_CASES = [
    "农副食品加工业", "食品制造业", "酒、饮料和精制茶制造业",
    "乳制品制造", "水产品加工", "调味品、发酵制品制造",
]


async def main() -> None:
    c = TianyanchaClient()
    areas = await c._load_area_codes()  # noqa: SLF001
    cats = await c._load_categories()   # noqa: SLF001
    print(f"[loaded] area entries={len(areas)}  category entries={len(cats)}")

    print("\n[area resolve]")
    ok = miss = 0
    for name in AREA_CASES:
        code, cands = await c.resolve_area_code(name)
        if code:
            ok += 1
            print(f"  OK   {name} -> {code}")
        else:
            miss += 1
            tag = f"AMBIGUOUS n={len(cands)}" if cands else "MISS"
            print(f"  {tag}  {name}")

    print("\n[category resolve]")
    for name in CATEGORY_CASES:
        code, cands = await c.resolve_category_code(name)
        tag = code if code else (f"AMBIGUOUS n={len(cands)}" if cands else "MISS")
        print(f"  {name} -> {tag}")

    assert areas, "area dict empty!"
    assert cats, "category dict empty!"
    assert ok >= 9, f"area exact-resolve too few: {ok}"
    print(f"\n[PASS] area exact={ok} miss/ambiguous={miss}; dicts loaded from local static.")


if __name__ == "__main__":
    asyncio.run(main())
