# -*- coding: utf-8 -*-
"""从 GB/T 4754-2017 国民经济行业分类源数据构建天眼查 categoryGuobiao 静态字典。

背景同 build_area_dict.py：天眼查已下线公开 OSS 字典 `category.json`（404），
`services/tianyancha_client.py::_load_categories` 拉取失败降级为空表，
`resolve_category_code` 对任意行业名返回 code=None。本脚本用权威公开数据重建。

产物结构与原 OSS `category.json` 完全一致（三级：门类不入，直接从大类起）：

    {"category": [
        {"primInduName": 大类(2位), "code": 2位码, "secList": [
            {"secnduName": 中类(3位), "code": 3位码, "terList": [
                {"terInduName": 小类(4位), "code": 4位码}
            ]}
        ]}
    ]}

映射口径：源数据是四级（门类字母 A-T / 大类2位 / 中类3位 / 小类4位）。天眼查 categoryGuobiao
用 GB/T 4754 数字码（2~4 位），门类字母不参与匹配，故丢弃门类层，从大类起构建三级，
对齐原 OSS category.json 的 primInduName/secnduName/terInduName 三级键名。

源数据：ramwin/china-public-data 国民经济行业分类_2017.json（对标 GB/T 4754-2017）。

用法（在 script_tools 根目录）：
    python static/tianyancha/build_category_dict.py
"""
from __future__ import annotations

import json
from pathlib import Path

SRC = Path(__file__).with_name("_src_industry_2017.json")
OUT = Path(__file__).with_name("category.json")


def build() -> dict:
    src = json.loads(SRC.read_text(encoding="utf-8"))
    primaries = []
    # 源顶层是门类（字母 code），其 children 是大类（2 位数字 code）——从大类起构建。
    for menlei in src:
        for dalei in menlei.get("children", []):
            prim = {
                "primInduName": dalei["name"],
                "code": dalei["code"],
                "secList": [],
            }
            for zhonglei in dalei.get("children", []):
                sec = {
                    "secnduName": zhonglei["name"],
                    "code": zhonglei["code"],
                    "terList": [
                        {"terInduName": xiaolei["name"], "code": xiaolei["code"]}
                        for xiaolei in zhonglei.get("children", [])
                    ],
                }
                prim["secList"].append(sec)
            primaries.append(prim)
    return {"category": primaries}


def main() -> None:
    data = build()
    OUT.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    prim = data["category"]
    sec = sum(len(p["secList"]) for p in prim)
    ter = sum(len(s["terList"]) for p in prim for s in p["secList"])
    print(f"built {OUT.name}: primary(大类)={len(prim)} secondary(中类)={sec} tertiary(小类)={ter}")


if __name__ == "__main__":
    main()
