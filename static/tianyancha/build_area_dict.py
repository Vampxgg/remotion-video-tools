# -*- coding: utf-8 -*-
"""从国家统计局行政区划源数据构建天眼查 areaCode 静态字典。

背景：天眼查（金地）已下线其公开 OSS 字典 `newAreaCodeV2024.json` / `category.json`（均 404），
导致 `services/tianyancha_client.py::_load_area_codes/_load_categories` 拉取失败降级为空表，
`resolve_area_code` 对任意地区返回 code=None，区域企业调研退化成"纯关键词全国搜索"、地域过滤失效。

本脚本用权威公开数据（GB/T 2260 行政区划）重建 area_code 字典，落地为静态文件，
彻底摆脱对天眼查 OSS 的运行时依赖。产物结构与原 OSS `newAreaCodeV2024.json` 完全一致：

    {"areaCode": [
        {"name": 省, "areaCode": 6位码, "city": [
            {"name": 市, "areaCode": 6位码, "district": [
                {"name": 区, "areaCode": 6位码}
            ]}
        ]}
    ]}

源数据：modood/Administrative-divisions-of-China dist/pca-code.json（数据源国家统计局）。
该源省 2 位 / 市 4 位 / 区 6 位，统一右补零到 6 位对齐天眼查 areaCode 口径。
港澳台源数据缺失，此处按 GB/T 2260 标准补齐省级三条。

用法（在 script_tools 根目录）：
    python static/tianyancha/build_area_dict.py
"""
from __future__ import annotations

import json
from pathlib import Path

SRC = Path(__file__).with_name("_src_pca_code.json")
OUT = Path(__file__).with_name("area_code.json")

# 港澳台：源数据缺失，按 GB/T 2260 省级码补齐（无下级市/区）。
_SUPPLEMENT = [
    {"name": "香港特别行政区", "areaCode": "810000", "city": []},
    {"name": "澳门特别行政区", "areaCode": "820000", "city": []},
    {"name": "台湾省", "areaCode": "710000", "city": []},
]


def _pad6(code: str) -> str:
    """省 2 位 / 市 4 位 → 右补零到 6 位；区已是 6 位原样返回。"""
    return (code + "000000")[:6]


def build() -> dict:
    src = json.loads(SRC.read_text(encoding="utf-8"))
    provinces = []
    for p in src:
        p_node = {
            "name": p["name"],
            "areaCode": _pad6(p["code"]),
            "city": [],
        }
        for c in p.get("children", []):
            c_node = {
                "name": c["name"],
                "areaCode": _pad6(c["code"]),
                "district": [
                    {"name": d["name"], "areaCode": _pad6(d["code"])}
                    for d in c.get("children", [])
                ],
            }
            p_node["city"].append(c_node)
        provinces.append(p_node)
    provinces.extend(_SUPPLEMENT)
    return {"areaCode": provinces}


def main() -> None:
    data = build()
    OUT.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    provinces = data["areaCode"]
    cities = sum(len(p["city"]) for p in provinces)
    districts = sum(len(c["district"]) for p in provinces for c in p["city"])
    print(f"built {OUT.name}: provinces={len(provinces)} cities={cities} districts={districts}")


if __name__ == "__main__":
    main()
