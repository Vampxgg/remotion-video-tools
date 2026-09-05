# -*- coding: utf-8 -*-
"""智联招聘全维度筛选码表运行时加载器。

读取 static/zhilian/filters.json（唯一 source of truth），把业务语义筛选（学历/经验/
公司性质/公司规模/行业/职位类型/雇佣类型/薪资/地铁）映射为智联新版接口
POST /c/i/search/positions 的原生 S_SOU_* 字段与码值。

设计与 utils/region_map.py 完全对齐：
- 进程内只加载一次（lru_cache）。
- 文件缺失或解析失败时回退到内置 _SEED，保证学历/经验等核心维度始终可用。
- 对外暴露语义查询：field_name / map_value / build_filters。

码值权威性：学历/经验/公司性质/公司规模/雇佣类型均经真实接口实测确认生效
（见 dev 探测记录），行业/职位类型取自 dict.zhaopin.cn 字典服务。

约定：
- 多选——同一维度传入以英文逗号分隔的多个标签/码值，逐项映射后重新用逗号拼接。
- 薪资(salary)——值形如 "min,max"（单位元，如 "10001,15000"），不查 options，原样透传。
- 地铁(subway)——值为站点码，不查 options，原样透传。
- 「不限」/空值——map_value 返回 None，build_filters 不写入该字段（保持不筛选）。
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/jobs/region_search.log")

_FILTER_FILE = Path(_settings.static_dir_abs) / "zhilian" / "filters.json"

# 值本身即是数值区间/站点码、不做标签映射的维度（options 为空，直接透传）。
_PASSTHROUGH_DIMS = frozenset({"salary", "subway"})

# 表示「不限/不筛选」的输入，映射结果为 None（不写入该字段）。
# 注意："-1" 是智联「不限」的显式码值，若用户显式选择「不限」标签会经 options 命中，
# 这里仅拦截空串/None/常见「不限」措辞，避免误把有效码当成空。
_UNLIMITED_TOKENS = frozenset({"", "不限", "全部", "none", "null"})

# 文件缺失时的兜底种子：dim -> {"field": S_SOU_*, "options": {标签: 码值}}。
# 至少覆盖已实测确认的核心维度全表 + 各维度 field 名，保证 JSON 缺失也能工作。
_SEED: Dict[str, Dict[str, Any]] = {
    "education": {
        "field": "S_SOU_EDUCATION_LOWESTLEVEL",
        "options": {
            "不限": "-1", "高中": "7", "中专": "12", "中专/中技": "12", "中职": "12",
            "大专": "5", "专科": "5", "高职专科": "5", "本科": "4", "高职本科": "4",
            "硕士": "3", "研究生": "3", "博士": "1", "其他": "8",
        },
    },
    "experience": {
        "field": "S_SOU_WORK_EXPERIENCE",
        "options": {
            "不限": "-1", "无经验": "-1", "应届": "-1", "1年以下": "001", "1年及以下": "001",
            "1-3年": "103", "3-5年": "305", "5-10年": "510", "10年以上": "1099",
        },
    },
    "company_type": {
        "field": "S_SOU_COMPANY_TYPE",
        "options": {
            "国企": "1", "外商独资": "2", "外资": "2", "合资": "4", "民营": "5", "私企": "5",
            "国家机关": "6", "其它": "7", "其他": "7", "股份制企业": "8", "股份制": "8",
            "上市公司": "9", "事业单位": "10",
        },
    },
    "company_scale": {
        "field": "S_SOU_COMPANY_SCALE",
        "options": {
            "20人以下": "1", "20-99人": "2", "100-299人": "3", "300-499人": "8",
            "500-999人": "4", "1000-9999人": "5", "10000人以上": "6",
        },
    },
    "position_type": {"field": "S_SOU_POSITION_TYPE", "options": {}},
    "employment_type": {
        "field": "S_SOU_EMPLOYMENT_TYPE",
        "options": {"全职": "2", "兼职": "1", "兼职/临时": "1", "实习": "4", "校园": "5"},
    },
    "industry": {"field": "S_SOU_JD_INDUSTRY_LEVEL", "options": {}},
    "salary": {"field": "S_SOU_SALARY", "options": {}},
    "subway": {"field": "S_SOU_SUBWAY_STATION", "options": {}},
}


@lru_cache(maxsize=1)
def _load() -> Dict[str, Dict[str, Any]]:
    """加载并返回 dim -> {"field": ..., "options": {...}}。"""
    try:
        raw = json.loads(_FILTER_FILE.read_text(encoding="utf-8"))
        dims = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}
        if dims:
            logger.info("[zhilian_filters] 已加载 %s 个维度 (%s)", len(dims), _FILTER_FILE)
            return dims
        logger.warning("[zhilian_filters] %s 无有效维度，回退种子", _FILTER_FILE)
    except FileNotFoundError:
        logger.warning("[zhilian_filters] 未找到 %s，回退内置种子", _FILTER_FILE)
    except Exception as exc:
        logger.warning("[zhilian_filters] 加载 %s 失败(%s)，回退种子", _FILTER_FILE, exc)
    return _SEED


def field_name(dim: str) -> Optional[str]:
    """维度名 -> 智联 S_SOU_* 字段名；未知维度返回 None。"""
    info = _load().get(dim)
    return info.get("field") if info else None


def _map_single(dim: str, token: str, options: Dict[str, str]) -> Optional[str]:
    """把单个标签/码值映射为码值。

    - 「不限/空」→ None
    - 命中 options 键（业务标签/中文/别名）→ 对应码值
    - 已是 options 的码值 → 原样返回
    - passthrough 维度（salary/subway）→ 原样返回
    - 其余未知 token → 原样返回（宽松兜底，交由智联接口判定）
    """
    t = (token or "").strip()
    if t.lower() in _UNLIMITED_TOKENS:
        return None
    if dim in _PASSTHROUGH_DIMS:
        return t
    if t in options:
        return options[t]
    if t in set(options.values()):
        return t
    return t


def map_value(dim: str, raw: Optional[str]) -> Optional[str]:
    """把业务标签/中文/别名 -> 码值；支持逗号多选。

    多选：以英文逗号分隔逐项映射，去重保序后重新用逗号拼接。
    全部项都是「不限/空」时返回 None（不写入该字段）。
    """
    if raw is None:
        return None
    info = _load().get(dim)
    options: Dict[str, str] = (info.get("options") if info else {}) or {}
    parts = [p for p in (raw.split(",") if isinstance(raw, str) else [str(raw)])]
    codes: list[str] = []
    seen: set[str] = set()
    for p in parts:
        code = _map_single(dim, p, options)
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return ",".join(codes) if codes else None


def build_filters(payload: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """入参为业务语义 dict（education/salary/experience/company_type/company_scale/
    industry/position_type/employment_type/subway），输出 {S_SOU_*: 码值}。

    - 未知维度、「不限/空」值一律跳过（不写入）。
    - 值支持逗号多选。
    - 供后端与其它调用方复用，是"业务语义 → 智联字段"的唯一映射入口。
    """
    result: Dict[str, str] = {}
    if not payload:
        return result
    dims = _load()
    for dim, raw in payload.items():
        if raw is None or dim not in dims:
            continue
        code = map_value(dim, raw if isinstance(raw, str) else str(raw))
        if not code:
            continue
        field = dims[dim].get("field")
        if field:
            result[field] = code
    return result
