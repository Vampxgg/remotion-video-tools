# -*- coding: utf-8 -*-
"""全量城市地域映射表运行时加载器。

读取 static/regions/region_map.json（由 scripts/build_region_map.py 离线生成），
对外提供按城市名解析 province / BOSS 城市编码 / 智联 cityId 的查询。

- 进程内只加载一次（lru_cache）。
- 文件缺失或解析失败时回退到内置 25 城种子，保证服务可用。
- 城市名支持去尾「市」等后缀后重试，支持可选 province 参数消歧。
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/jobs/region_search.log")

_REGION_FILE = Path(_settings.static_dir_abs) / "regions" / "region_map.json"

# 文件缺失时的兜底种子：city -> (province, boss_code, zhilian_id)
_SEED: Dict[str, Tuple[Optional[str], Optional[int], Optional[str]]] = {
    "全国": (None, 100010000, None),
    "北京": ("北京", 101010100, "530"),
    "上海": ("上海", 101020100, None),
    "天津": ("天津", 101030100, None),
    "重庆": ("重庆", 101040100, None),
    "广州": ("广东", 101280100, None),
    "深圳": ("广东", 101280600, "765"),
    "佛山": ("广东", 101280800, None),
    "东莞": ("广东", 101281600, None),
    "杭州": ("浙江", 101210100, "653"),
    "宁波": ("浙江", 101210400, None),
    "西安": ("陕西", 101110100, None),
    "苏州": ("江苏", 101190400, None),
    "南京": ("江苏", 101190100, None),
    "武汉": ("湖北", 101200100, None),
    "厦门": ("福建", 101230200, "682"),
    "福州": ("福建", 101230100, None),
    "长沙": ("湖南", 101250100, None),
    "成都": ("四川", 101270100, None),
    "郑州": ("河南", 101180100, None),
    "合肥": ("安徽", 101220100, None),
    "济南": ("山东", 101120100, None),
    "青岛": ("山东", 101120200, None),
    "昆明": ("云南", 101290100, None),
    "南昌": ("江西", 101240100, None),
    "石家庄": ("河北", 101090100, None),
}

# 去尾后缀重试用；按长度优先匹配。
_CITY_SUFFIXES = ("特别行政区", "自治州", "地区", "盟", "市")


@lru_cache(maxsize=1)
def _load() -> Dict[str, Dict[str, Any]]:
    """加载并返回 city -> {province, boss_code, zhilian_id}。"""
    try:
        raw = json.loads(_REGION_FILE.read_text(encoding="utf-8"))
        cities = raw.get("cities") or {}
        if cities:
            logger.info(
                "[region_map] 已加载 %s 城市 (%s)",
                len(cities),
                _REGION_FILE,
            )
            return cities
        logger.warning("[region_map] %s 无 cities 字段，回退种子", _REGION_FILE)
    except FileNotFoundError:
        logger.warning("[region_map] 未找到 %s，回退内置种子", _REGION_FILE)
    except Exception as exc:
        logger.warning("[region_map] 加载 %s 失败(%s)，回退种子", _REGION_FILE, exc)

    return {
        name: {"province": prov, "boss_code": boss, "zhilian_id": zhi}
        for name, (prov, boss, zhi) in _SEED.items()
    }


def _normalize_candidates(city: str) -> Tuple[str, ...]:
    """生成城市名候选：原名 + 去尾后缀名。"""
    name = (city or "").strip()
    candidates = [name]
    for suffix in _CITY_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            candidates.append(name[: -len(suffix)])
    # 去重保序
    seen = set()
    return tuple(c for c in candidates if c and not (c in seen or seen.add(c)))


def _lookup(city: Optional[str], province: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not city:
        return None
    cities = _load()
    for name in _normalize_candidates(city):
        info = cities.get(name)
        if info and (not province or not info.get("province") or info["province"] == province):
            return info
    # province 不匹配时退化为只按城市名
    for name in _normalize_candidates(city):
        info = cities.get(name)
        if info:
            return info
    return None


def province_for_city(city: Optional[str], province: Optional[str] = None) -> Optional[str]:
    info = _lookup(city, province)
    return info.get("province") if info else None


def boss_code_for_city(city: Optional[str], province: Optional[str] = None) -> Optional[int]:
    info = _lookup(city, province)
    code = info.get("boss_code") if info else None
    return int(code) if code is not None else None


def zhilian_id_for_city(city: Optional[str], province: Optional[str] = None) -> Optional[str]:
    info = _lookup(city, province)
    zid = info.get("zhilian_id") if info else None
    return str(zid) if zid is not None else None
