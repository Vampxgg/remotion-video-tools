# -*- coding: utf-8 -*-
# 粉笔招考：走官方 Hera / market-api，避免对 fenbi.com 纯静态抓取拿不到正文

import asyncio
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

try:
    from utils.logger import setup_module_logger
except ImportError:
    import logging
    import sys

    def setup_module_logger(name: str, path: str):
        lg = logging.getLogger(name)
        if not lg.handlers:
            h = logging.StreamHandler(sys.stdout)
            lg.addHandler(h)
            lg.setLevel(logging.INFO)
        return lg


logger = setup_module_logger(__name__, "logs/jobs/fenbi_recruit.log")

router = APIRouter()

# --- 缓存设计 ---
_GLOBAL_OPTIONS_CACHE: Optional[Dict[str, Any]] = None

HERA_BASE = "https://hera-webapp.fenbi.com"
MARKET_API = "https://market-api.fenbi.com/toolkit/api/v1/pc"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

FENBI_NOISE_LINES = (
    "免费报考咨询",
    "考情随时掌握",
    "尽在粉笔",
)


def _client_headers() -> Dict[str, str]:
    return {
        "User-Agent": DEFAULT_UA,
        "Referer": "https://fenbi.com/",
        "Origin": "https://fenbi.com",
    }


def _market_params(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p = {"app": "web", "av": 100, "hav": 100, "kav": 100}
    if extra:
        p.update(extra)
    return p


def parse_article_id(user_input: str) -> str:
    """支持纯数字或 fenbi 详情页 URL。"""
    s = (user_input or "").strip()
    if s.isdigit():
        return s
    m = re.search(r"exam-information-detail/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=(\d+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{10,20}", s):
        return s
    raise ValueError(f"无法解析粉笔公告 id: {user_input!r}")


def _strip_fenbi_noise(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if all(n in line for n in ("免费报考咨询", "尽在粉笔")):
            continue
        if line in FENBI_NOISE_LINES:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def extract_body_from_hera_html(html: str) -> str:
    """
    Hera 返回的详情页为整页 HTML。选取「含公告/招聘关键词且文本足够长」的 div 中
    文本最短的一条，近似最内层正文容器；再可选 trafilatura 增强。
    """
    soup = BeautifulSoup(html, "lxml")

    try:
        import trafilatura
        from trafilatura.settings import use_config

        cfg = use_config()
        cfg.set("DEFAULT", "EXTRACTION_TIMEOUT", "15")
        tr_out = (
            trafilatura.extract(
                html,
                config=cfg,
                output_format="markdown",
                include_images=False,
                favor_recall=True,
            )
            or ""
        ).strip()
        if len(tr_out) >= 500:
            return _strip_fenbi_noise(tr_out)
    except Exception:
        pass

    candidates: List[tuple] = []
    for div in soup.find_all("div"):
        raw = div.get_text("\n", strip=True)
        if len(raw) < 1200:
            continue
        if "公告" not in raw and "招聘" not in raw:
            continue
        candidates.append((len(raw), raw))

    if not candidates:
        body = soup.get_text("\n", strip=True)
        return _strip_fenbi_noise(body)[:50000]

    candidates.sort(key=lambda x: x[0])
    text = candidates[0][1]
    return _strip_fenbi_noise(text)[:50000]


async def fetch_article_structured(
    client: httpx.AsyncClient, article_id: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    调用 Hera 摘要 + 详情 HTML，返回与单条 article 接口一致的结构化 dict。
    失败时 (None, 错误说明)。
    """
    aid = str(article_id).strip()
    sum_url = f"{HERA_BASE}/api/website/article/summary"
    r_sum = await client.get(sum_url, params={"id": aid})
    if r_sum.status_code != 200:
        return None, f"摘要接口 HTTP {r_sum.status_code}"
    try:
        j_sum = r_sum.json()
    except Exception:
        return None, "摘要接口非 JSON"
    if j_sum.get("code") != 1:
        return None, j_sum.get("msg") or "摘要接口业务错误"
    data_sum = j_sum.get("data") or {}
    if not data_sum.get("id"):
        return None, "未找到该公告"
    detail_url = data_sum.get("contentURL") or f"{HERA_BASE}/api/article/detail?id={aid}"
    r_html = await client.get(detail_url)
    if r_html.status_code != 200:
        return None, f"详情页 HTTP {r_html.status_code}"
    body_text = extract_body_from_hera_html(r_html.text)
    out = {
        "article_id": aid,
        "title": data_sum.get("title"),
        "source": data_sum.get("source"),
        "issue_time_ms": data_sum.get("issueTime"),
        "update_time_ms": data_sum.get("updateTime"),
        "business_type": data_sum.get("businessType"),
        "content_type": data_sum.get("contentType"),
        "favorite_num": data_sum.get("favoriteNum"),
        "detail_url": detail_url,
        "summary_api": sum_url,
        "content_text": body_text,
        "content_chars": len(body_text),
    }
    return out, None


async def fetch_position_detail_structured(
    client: httpx.AsyncClient, position_id: int
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"{MARKET_API}/position/detail"
    params = _market_params({"positionId": position_id})
    r = await client.get(url, params=params)
    if r.status_code != 200:
        return None, f"职位详情 HTTP {r.status_code}"
    try:
        j = r.json()
    except Exception:
        return None, "职位详情非 JSON"
    if j.get("code") != 1:
        return None, j.get("msg") or "职位详情业务错误"
    return j.get("data"), None


class StandardResponse(BaseModel):
    code: int = 200
    message: str = "Success"
    data: Optional[Any] = None
    timestamp: str = ""


def _json_ok(data: Any, message: str = "Success") -> JSONResponse:
    body = StandardResponse(
        code=200,
        message=message,
        data=data,
        timestamp=datetime.now().isoformat(),
    ).model_dump()
    return JSONResponse(content=body)


def _json_err(code: int, message: str) -> JSONResponse:
    body = StandardResponse(
        code=code,
        message=message,
        data=None,
        timestamp=datetime.now().isoformat(),
    ).model_dump()
    return JSONResponse(status_code=code, content=body)


async def fetch_major_tree(client: httpx.AsyncClient, major_type_id: int, major_degree: int) -> List[Dict[str, Any]]:
    # L1
    r1 = await client.get(f'{MARKET_API}/major/listByLevel?majorTypeId={major_type_id}&majorDegree={major_degree}')
    if r1.status_code != 200: return []
    l1 = r1.json().get('datas', [])
    if not l1: return []
    
    # L2
    tasks2 = [client.get(f'{MARKET_API}/major/listByLevel?majorTypeId={major_type_id}&majorDegree={major_degree}&parentCode={c["value"]}') for c in l1]
    res2 = await asyncio.gather(*tasks2, return_exceptions=True)
    l2_map = {}
    for parent, r in zip(l1, res2):
        if not isinstance(r, Exception) and r.status_code == 200:
            l2_map[parent["value"]] = r.json().get("datas", [])
            
    # L3
    all_l2 = []
    for l2_list in l2_map.values():
        all_l2.extend(l2_list)
        
    tasks3 = [client.get(f'{MARKET_API}/major/listByLevel?majorTypeId={major_type_id}&majorDegree={major_degree}&parentCode={c["value"]}') for c in all_l2]
    res3 = await asyncio.gather(*tasks3, return_exceptions=True)
    l3_map = {}
    for parent, r in zip(all_l2, res3):
        if not isinstance(r, Exception) and r.status_code == 200:
            l3_map[parent["value"]] = r.json().get("datas", [])
            
    # Build tree
    tree = []
    for c1 in l1:
        node1 = {"name": c1["name"], "value": c1["value"], "children": []}
        for c2 in l2_map.get(c1["value"], []):
            node2 = {"name": c2["name"], "value": c2["value"], "children": []}
            for c3 in l3_map.get(c2["value"], []):
                node2["children"].append({"name": c3["name"], "value": c3["value"]})
            node1["children"].append(node2)
        tree.append(node1)
    return tree

# --- 请求体 ---


class FenbiArticleRequest(BaseModel):
    article_id: str = Field(
        ...,
        description="公告数字 id，或完整链接如 https://fenbi.com/page/exam-information-detail/464861463860224",
    )


class FenbiTimelineRequest(BaseModel):
    district_id: int = Field(0, description="地区 id，0 表示全国/不限")
    offset: int = Field(0, ge=0)
    size: int = Field(10, ge=1, le=50)


class FenbiHomeLinksRequest(BaseModel):
    max_links: int = Field(80, ge=1, le=200, description="从首页解析的最大链接数")

class FenbiDataRequest(BaseModel):
    query_type: str = Field(
        ...,
        description="请求类型，可选 'announcements', 'positions', 'both'",
        pattern="^(announcements|positions|both)$"
    )
    exam_type: int = Field(..., description="考试类型 ID（从 options 获取）")
    district_id: int = Field(0, description="地区 ID，0 为全国")
    year: int = Field(datetime.now().year, description="年份")
    enroll_status: int = Field(0, description="报名状态")
    recruit_num_code: int = Field(0, description="招录人数区间")
    start: int = Field(0, ge=0, description="分页起始")
    page_size: int = Field(20, ge=1, le=50, description="每页条数")
    
    # 公告额外参数
    need_total: bool = Field(True, description="是否请求公告总数")
    title_keyword: Optional[str] = Field(None, description="在公告当前页进行标题子串过滤")
    include_detail: bool = Field(False, description="是否并发拉取正文详情（同时适用公告和职位）")
    max_details: int = Field(5, ge=0, le=30, description="拉取详情数量上限")
    detail_concurrency: int = Field(3, ge=1, le=10, description="拉详情并发数")
    
    # 职位额外参数
    exam_id: Optional[int] = Field(None, description="限定某次考试")
    major_degree: Optional[int] = Field(None, description="学历档 majorDegree")
    major_code: Optional[str] = Field(None, description="终端专业 majorCode（无需传上级门类学科，后端自动解析或单传此项）")
    option_contents: Optional[List[Dict[str, Any]]] = Field(None, description="与前端 optionContents 一致的高级筛选")




def _normalize_district_ids_for_positions(ids: List[int]) -> List[int]:
    if len(ids) > 1:
        return [ids[-1]]
    return ids


def _build_position_query_body(req: FenbiDataRequest) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "examType": req.exam_type,
        "districtIds": _normalize_district_ids_for_positions([req.district_id]),
        "start": req.start,
        "len": req.page_size,
    }
    if req.exam_id is not None:
        body["examId"] = req.exam_id
    if req.major_degree is not None:
        body["majorDegree"] = req.major_degree
    if req.major_code is not None:
        # 直接透传至 majorCode。如果用户在树中点选学科或门类，也可以借 option_contents 传入高级筛选
        body["majorCode"] = req.major_code
    if req.option_contents is not None:
        body["optionContents"] = req.option_contents
    return body


async def _announcements_payload(
    client: httpx.AsyncClient, req: FenbiDataRequest
) -> Dict[str, Any]:
    url = f"{MARKET_API}/exam/queryByCondition"
    body = {
        "districtId": req.district_id,
        "examType": req.exam_type,
        "year": req.year,
        "enrollStatus": req.enroll_status,
        "recruitNumCode": req.recruit_num_code,
        "start": req.start,
        "len": req.page_size,
        "needTotal": req.need_total,
    }
    r = await client.post(
        url,
        params=_market_params(),
        json=body,
        headers={**_client_headers(), "Content-Type": "application/json"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"queryByCondition HTTP {r.status_code}")
    j = r.json()
    if j.get("code") != 1:
        raise RuntimeError(j.get("msg") or "queryByCondition 业务错误")
    data = j.get("data") or {}
    stick = data.get("stickTopArticles") or []
    articles = data.get("articles") or []
    total = data.get("total")

    merged = stick + articles
    if req.title_keyword:
        kw = req.title_keyword.strip().lower()
        merged = [x for x in merged if kw in (x.get("title") or "").lower()]

    warnings: List[str] = []
    out_items: List[Dict[str, Any]] = []
    for raw in merged:
        aid = raw.get("id")
        item = {
            "article_id": str(aid) if aid is not None else None,
            "list_row": raw,
        }
        out_items.append(item)

    if req.include_detail and req.max_details > 0:
        to_fetch: List[str] = []
        for it in out_items:
            aid = it.get("article_id")
            if aid:
                to_fetch.append(aid)
        to_fetch = to_fetch[: req.max_details]
        skipped = max(0, len([i for i in out_items if i.get("article_id")]) - len(to_fetch))
        if skipped:
            warnings.append(f"详情仅拉取前 {req.max_details} 条，其余 {skipped} 条已跳过")

        sem = asyncio.Semaphore(req.detail_concurrency)

        async def fetch_one(aid: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
            async with sem:
                d, err = await fetch_article_structured(client, aid)
                return aid, d, err

        results = await asyncio.gather(*[fetch_one(a) for a in to_fetch])
        detail_map = {aid: (d, err) for aid, d, err in results}
        for it in out_items:
            aid = it.get("article_id")
            if aid and aid in detail_map:
                d, err = detail_map[aid]
                if err:
                    it["detail"] = None
                    it["detail_error"] = err
                else:
                    it["detail"] = d
    return {
        "total": total,
        "stick_top_count": len(stick),
        "page_items_count": len(articles),
        "items": out_items,
        "warnings": warnings,
        "upstream": {"path": "/exam/queryByCondition", "request_body": body},
    }


async def _positions_payload(
    client: httpx.AsyncClient, req: FenbiDataRequest
) -> Dict[str, Any]:
    url = f"{MARKET_API}/position/queryByConditions"
    body = _build_position_query_body(req)
    r = await client.post(
        url,
        params=_market_params(),
        json=body,
        headers={**_client_headers(), "Content-Type": "application/json"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"queryByConditions HTTP {r.status_code}")
    j = r.json()
    if j.get("code") != 1:
        raise RuntimeError(j.get("msg") or "queryByConditions 业务错误")
    rows = j.get("datas") or []
    total = j.get("total")
    warnings: List[str] = []

    out_items: List[Dict[str, Any]] = []
    for raw in rows:
        pid = raw.get("id")
        out_items.append({"position_id": pid, "list_row": raw})

    if req.include_detail and req.max_details > 0:
        ids = [it["position_id"] for it in out_items if it.get("position_id") is not None]
        ids = ids[: req.max_details]
        if len([i for i in out_items if i.get("position_id")]) > len(ids):
            warnings.append(
                f"职位详情仅拉取前 {req.max_details} 条，其余已跳过"
            )
        sem = asyncio.Semaphore(req.detail_concurrency)

        async def fetch_one(pid: int) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
            async with sem:
                d, err = await fetch_position_detail_structured(client, pid)
                return pid, d, err

        results = await asyncio.gather(*[fetch_one(int(i)) for i in ids])
        pmap = {pid: (d, err) for pid, d, err in results}
        for it in out_items:
            pid = it.get("position_id")
            if pid is not None and int(pid) in pmap:
                d, err = pmap[int(pid)]
                if err:
                    it["detail"] = None
                    it["detail_error"] = err
                else:
                    it["detail"] = d

    return {
        "total": total,
        "items": out_items,
        "warnings": warnings,
        "upstream": {"path": "/position/queryByConditions", "request_body": body},
    }


@router.post(
    "/scrape/fenbi/article",
    summary="粉笔招考 — 公告正文（Hera 官方通道）",
    description=(
        "根据公告 id 调用 hera-webapp 的 JSON 摘要与 HTML 详情页，提取结构化字段与正文。"
        "解决 fenbi.com 前端壳 + 通用 trafilatura 抽不到内容的问题。"
    ),
)
async def fenbi_article(payload: FenbiArticleRequest):
    try:
        aid = parse_article_id(payload.article_id)
    except ValueError as e:
        return _json_err(400, str(e))

    async with httpx.AsyncClient(
        headers=_client_headers(), follow_redirects=True, timeout=60.0
    ) as client:
        out, err = await fetch_article_structured(client, aid)
    if err:
        if "未找到" in (err or ""):
            return _json_err(404, err)
        if err.startswith("摘要接口 HTTP"):
            return _json_err(502, err)
        return _json_err(502, err or "未知错误")

    msg = (
        "ok"
        if len((out or {}).get("content_text") or "") >= 200
        else "正文较短，可能需调整解析规则或页面结构已变"
    )
    return _json_ok(out, message=msg)


@router.post(
    "/scrape/fenbi/timeline",
    summary="粉笔 — 考试日历时间线（market-api）",
    description="分页获取首页「考试日历」类条目，字段含 id/topic/省份等，详情需再用 /scrape/fenbi/article 或 exam-timeline-detail 页。",
)
async def fenbi_timeline(payload: FenbiTimelineRequest):
    url = f"{MARKET_API}/exam/getTimeLineDetails"
    params = _market_params(
        {
            "districtId": payload.district_id,
            "offset": payload.offset,
            "size": payload.size,
        }
    )
    async with httpx.AsyncClient(headers=_client_headers(), timeout=30.0) as client:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            return _json_err(502, f"HTTP {r.status_code}")
        try:
            d = r.json()
        except Exception:
            return _json_err(502, "非 JSON 响应")

    return _json_ok(
        {
            "total": d.get("total"),
            "items": d.get("datas") or [],
            "raw_code": d.get("code"),
            "raw_msg": d.get("msg"),
        }
    )


@router.get(
    "/scrape/fenbi/options",
    summary="粉笔 — 全局筛选条件与专业级联字典（极致组合版）",
    description="自动聚合招考和职位库的字典（地区、考试类型、年份、招录人数区间、学历及全量专业级联树）。具备常驻内存缓存，后续调用耗时接近 0ms。",
)
async def fenbi_options():
    global _GLOBAL_OPTIONS_CACHE
    if _GLOBAL_OPTIONS_CACHE is not None:
        return _json_ok(_GLOBAL_OPTIONS_CACHE)

    async with httpx.AsyncClient(
        headers=_client_headers(), limits=httpx.Limits(max_connections=100), timeout=60.0
    ) as client:
        # 并发获取基础 exam conditions 和 common conditions
        res_exam, res_pos = await asyncio.gather(
            client.get(f"{MARKET_API}/exam/conditions", params=_market_params()),
            client.get(
                f"{MARKET_API}/position/commonConditions",
                params=_market_params({"examType": "4"}),  # 使用事业编作为基准拿专业配置
            ),
            return_exceptions=True
        )

        if isinstance(res_exam, Exception) or res_exam.status_code != 200:
            return _json_err(502, "无法获取 exam conditions")
        if isinstance(res_pos, Exception) or res_pos.status_code != 200:
            return _json_err(502, "无法获取 position conditions")

        j_exam = res_exam.json().get("data", {})
        j_pos = res_pos.json().get("data", {})

        # 合并返回字典
        major_type_id = j_pos.get("majorTypeId")
        major_degrees = j_pos.get("majorDegrees", [])

        out = {
            "districts": j_exam.get("districtList", []),
            "exam_types": j_exam.get("examTypeList", []),
            "years": j_exam.get("yearList", []),
            "enroll_statuses": j_exam.get("enrollStatusList", []),
            "recruit_nums": j_exam.get("recruitNumList", []),
            "major_degrees": major_degrees,
            "major_tree": {},  # 按 major_degree 分类的全量树
        }

        if major_type_id and major_degrees:
            # 针对所有学历并发拉取对应的整棵专业树
            tree_tasks = [
                fetch_major_tree(client, major_type_id, int(md["value"]))
                for md in major_degrees
            ]
            trees = await asyncio.gather(*tree_tasks, return_exceptions=True)
            for md, tree in zip(major_degrees, trees):
                if not isinstance(tree, Exception):
                    out["major_tree"][str(md["value"])] = tree

        _GLOBAL_OPTIONS_CACHE = out
        return _json_ok(out)

@router.post(
    "/scrape/fenbi/home-links",
    summary="粉笔首页 — 招考信息详情链接列表",
    description="抓取 fenbi.com 首页 HTML，正则提取 /page/exam-information-detail/{id} 链接与标题（SSR 可见部分）。",
)
async def fenbi_home_links(payload: FenbiHomeLinksRequest):
    async with httpx.AsyncClient(headers=_client_headers(), timeout=45.0) as client:
        r = await client.get("https://fenbi.com/")
        if r.status_code != 200:
            return _json_err(502, f"首页 HTTP {r.status_code}")
        html = r.text

    pat = re.compile(
        r'href="(https://fenbi\.com/page/exam-information-detail/(\d+))"[^>]*>([^<]{2,300})</a>',
        re.I,
    )
    seen = set()
    items: List[Dict[str, str]] = []
    for m in pat.finditer(html):
        url, aid, title = m.group(1), m.group(2), m.group(3).strip()
        if aid in seen:
            continue
        seen.add(aid)
        items.append({"article_id": aid, "title": title, "url": url})
        if len(items) >= payload.max_links:
            break

    return _json_ok({"total": len(items), "items": items})


