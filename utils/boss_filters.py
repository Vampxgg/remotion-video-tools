# -*- coding: utf-8 -*-
"""BOSS直聘全维度筛选码表运行时加载器。

读取 static/boss/filters.json（唯一 source of truth），把业务语义筛选（学历/经验/薪资/
公司规模/融资阶段/行业/工作区域/雇佣类型）映射为 BOSS 列表接口
GET /wapi/zpgeek/search/joblist.json 的原生 query 参数与码值。

设计与 utils/zhilian_filters.py 完全对齐：
- 进程内只加载一次（lru_cache）。
- 文件缺失或解析失败时回退到内置 _SEED，保证核心维度始终可用。
- 对外暴露语义查询：field_name / map_value / build_filters。

码值权威性：全部维度码值均在登录态（用户"何曦"）下实测确认——面板 ka 埋点给出码值、
点击后 URL 给出真实 query 参数名（ka 前缀 sel-job-rec-exp- 对应的真实参数名是
`experience`；ka 序号 sel-industry-N 不是行业真实码，industry 真实码由点击 URL 抓取）。

与智联的关键差异（务必区分，二者码值互不通用）：
- salary：BOSS 用**档位码**（402=3K以下 … 407=50K以上），本加载器只做「档位标签→档位码」
  映射，不做数值区间→档位推断；而智联 salary 是「min,max」数值区间透传。因此 BOSS 无
  passthrough 维度。
- experience/degree/scale：与智联码值完全不同，各自独立映射。
- district（工作区域）：BOSS 用 `multiBusinessDistrict`=国标行政区划码（如上海浦东=310115）。
  已实测 BOSS 码==国标码；码表覆盖全国 371 城 2899 区县（数据源:国家地名信息库），
  按城市（by_city）消歧重名区县（朝阳区/新华区等），需由调用方传入 city（build_filters(payload, city)）。

约定：
- 多选——同一维度传入以英文逗号分隔的多个标签/码值，逐项映射后重新用逗号拼接。
- 「不限」/空值——map_value 返回 None，build_filters 不写入该字段（保持不筛选）。
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/jobs/region_search.log")

_FILTER_FILE = Path(_settings.static_dir_abs) / "boss" / "filters.json"

# 表示「不限/不筛选」的输入，映射结果为 None（不写入该字段）。
_UNLIMITED_TOKENS = frozenset({"", "不限", "全部", "none", "null", "-1", "0"})

# 文件缺失时的兜底种子：dim -> {"field": BOSS query 参数名, "options": {标签: 码值}}。
# 覆盖已实测确认的全部维度全表，保证 JSON 缺失也能工作。
_SEED: Dict[str, Dict[str, Any]] = {
    "education": {
        "field": "degree",
        "options": {
            "初中及以下": "209", "初中": "209", "中专": "208", "中技": "208",
            "中专/中技": "208", "中职": "208", "高中": "206", "大专": "202",
            "专科": "202", "高职专科": "202", "本科": "203", "硕士": "204",
            "研究生": "204", "博士": "205",
        },
    },
    "experience": {
        "field": "experience",
        "options": {
            "经验不限": "101", "无经验": "102", "应届": "102", "应届生": "102",
            "在校/应届": "102", "在校生": "108", "1年以内": "103", "1年以下": "103",
            "1年及以下": "103", "1-3年": "104", "3-5年": "105", "5-10年": "106",
            "10年以上": "107",
        },
    },
    "salary": {
        "field": "salary",
        "options": {
            "3K以下": "402", "3k以下": "402", "3-5K": "403", "3-5k": "403",
            "5-10K": "404", "5-10k": "404", "10-20K": "405", "10-20k": "405",
            "20-50K": "406", "20-50k": "406", "50K以上": "407", "50k以上": "407",
        },
    },
    "company_scale": {
        "field": "scale",
        "options": {
            "0-20人": "301", "20人以下": "301", "20-99人": "302", "100-499人": "303",
            "100-299人": "303", "300-499人": "303", "500-999人": "304",
            "1000-9999人": "305", "10000人以上": "306",
        },
    },
    "stage": {
        "field": "stage",
        "options": {
            "未融资": "801", "天使轮": "802", "A轮": "803", "B轮": "804", "C轮": "805",
            "D轮及以上": "806", "D轮": "806", "已上市": "807", "上市": "807",
            "上市公司": "807", "不需要融资": "808",
        },
    },
    "employment_type": {
        "field": "jobType",
        "options": {"全职": "1901", "兼职": "1903", "兼职/临时": "1903"},
    },
    "district": {
        "field": "multiBusinessDistrict",
        "by_city": {
            "上海": {
                "浦东新区": "310115", "黄浦区": "310101", "徐汇区": "310104",
                "长宁区": "310105", "静安区": "310106", "普陀区": "310107",
                "虹口区": "310109", "杨浦区": "310110", "闵行区": "310112",
                "宝山区": "310113", "嘉定区": "310114", "金山区": "310116",
                "松江区": "310117", "青浦区": "310118", "奉贤区": "310120",
                "崇明区": "310151",
            },
        },
        "options": {
            "浦东新区": "310115", "黄浦区": "310101", "徐汇区": "310104",
            "长宁区": "310105", "静安区": "310106", "普陀区": "310107",
            "虹口区": "310109", "杨浦区": "310110", "闵行区": "310112",
            "宝山区": "310113", "嘉定区": "310114", "金山区": "310116",
            "松江区": "310117", "青浦区": "310118", "奉贤区": "310120",
            "崇明区": "310151",
        },
    },
    "industry": {"field": "industry", "options": {"互联网": "100020"}},
}


@lru_cache(maxsize=1)
def _load() -> Dict[str, Dict[str, Any]]:
    """加载并返回 dim -> {"field": ..., "options": {...}}。"""
    try:
        raw = json.loads(_FILTER_FILE.read_text(encoding="utf-8"))
        dims = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}
        if dims:
            logger.info("[boss_filters] 已加载 %s 个维度 (%s)", len(dims), _FILTER_FILE)
            return dims
        logger.warning("[boss_filters] %s 无有效维度，回退种子", _FILTER_FILE)
    except FileNotFoundError:
        logger.warning("[boss_filters] 未找到 %s，回退内置种子", _FILTER_FILE)
    except Exception as exc:
        logger.warning("[boss_filters] 加载 %s 失败(%s)，回退种子", _FILTER_FILE, exc)
    return _SEED


def field_name(dim: str) -> Optional[str]:
    """维度名 -> BOSS query 参数名；未知维度返回 None。"""
    info = _load().get(dim)
    return info.get("field") if info else None


def _district_options(city: Optional[str]) -> Dict[str, str]:
    """返回用于 district 维度映射的 {区县名: 码} 表。

    有 city 上下文时优先取该城市的区县表（by_city，消歧重名区县如朝阳区/新华区）；
    无 city 或该城市缺失时回退扁平 options（重名取首个，宽松兜底）。
    city 名做尾缀归一化（去「市/地区/自治州」等），与后端传入的城市名对齐。
    """
    info = _load().get("district") or {}
    by_city: Dict[str, Dict[str, str]] = info.get("by_city") or {}
    flat: Dict[str, str] = info.get("options") or {}
    if city:
        name = (city or "").strip()
        for suf in ("特别行政区", "自治州", "地区", "盟", "市"):
            if name.endswith(suf) and len(name) > len(suf):
                name = name[: -len(suf)]
                break
        city_opts = by_city.get(name) or by_city.get((city or "").strip())
        if city_opts:
            return city_opts
    return flat


def _map_single(dim: str, token: str, options: Dict[str, str]) -> Optional[str]:
    """把单个标签/码值映射为码值。

    - 「不限/空」→ None
    - 命中 options 键（业务标签/中文/别名）→ 对应码值
    - 已是 options 的码值 → 原样返回
    - 其余未知 token → 原样返回（宽松兜底，交由 BOSS 接口判定）
    """
    t = (token or "").strip()
    if t.lower() in _UNLIMITED_TOKENS:
        return None
    if t in options:
        return options[t]
    if t in set(options.values()):
        return t
    return t


def map_value(dim: str, raw: Optional[str], city: Optional[str] = None) -> Optional[str]:
    """把业务标签/中文/别名 -> 码值；支持逗号多选。

    多选：以英文逗号分隔逐项映射，去重保序后重新用逗号拼接。
    全部项都是「不限/空」时返回 None（不写入该字段）。

    ``city``：仅对 district 维度有意义——用于在该城市的区县表里消歧（重名区县）。
    其余维度忽略 city。
    """
    if raw is None:
        return None
    if dim == "district":
        options = _district_options(city)
    else:
        info = _load().get(dim)
        options = (info.get("options") if info else {}) or {}
    parts = [p for p in (raw.split(",") if isinstance(raw, str) else [str(raw)])]
    codes: list[str] = []
    seen: set[str] = set()
    for p in parts:
        code = _map_single(dim, p, options)
        if code and code not in seen:
            seen.add(code)
            codes.append(code)
    return ",".join(codes) if codes else None


def build_filters(payload: Optional[Dict[str, Any]], city: Optional[str] = None) -> Dict[str, str]:
    """入参为业务语义 dict（education/experience/salary/company_scale/stage/industry/
    district/employment_type），输出 {BOSS query 参数名: 码值}。

    - 未知维度、「不限/空」值一律跳过（不写入）。
    - 值支持逗号多选。
    - 供后端与其它调用方复用，是"业务语义 → BOSS 参数"的唯一映射入口。
    - BOSS 不支持的维度（如智联专属的 subway/company_type）不在本表内，会被静默跳过，
      由调用方按维度粒度提示。
    - ``city``：用于 district 维度按城市消歧（重名区县）。后端应传 payload.region.city，
      使"上海+浦东新区"等精确映射；缺省时 district 走扁平表兜底。
    """
    result: Dict[str, str] = {}
    if not payload:
        return result
    dims = _load()
    for dim, raw in payload.items():
        if raw is None or dim not in dims:
            continue
        code = map_value(dim, raw if isinstance(raw, str) else str(raw), city)
        if not code:
            continue
        field = dims[dim].get("field")
        if field:
            result[field] = code
    return result
