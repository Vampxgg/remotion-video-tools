# -*- coding: utf-8 -*-
"""
粉笔招考网关：仅暴露 GET /scrape/fenbi/meta 与 POST /scrape/fenbi/action。
上游与官网 PC 一致：market-api.fenbi.com、hera-webapp.fenbi.com。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

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


logger = setup_module_logger(__name__, "logs/jobs/fenbi_gateway.log")

router = APIRouter()

HERA_ORIGIN = "https://hera-webapp.fenbi.com"
MARKET_PC = "https://market-api.fenbi.com/toolkit/api/v1/pc"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

META_CACHE_TTL_SEC = 600
_META_LOCK = asyncio.Lock()
_META_CACHE: Dict[Tuple[Optional[int], int], Tuple[float, Dict[str, Any]]] = {}

NOISE_LINES = ("免费报考咨询", "考情随时掌握", "尽在粉笔")

# 未设置环境变量 FENBI_COOKIE 时使用的默认 Cookie（职位详情等需登录字段）。
# 生产环境请改用环境变量或 .env，勿将含有效 sess 的代码推送到公共仓库。
_DEFAULT_FENBI_COOKIE = (
    "sess=YTKcCC/Qi+iYb9gWv/E9lnplSHqoxb97m6+ARo8JkVlS9X5tJON7KTIimiZ2Tl7NkCQ6DdISA+mo1nZP+PwL1FW9dVr5GCzHU8UR5BEA8DE=; "
    "userid=165100008"
)


def _fenbi_cookie() -> Optional[str]:
    """
    返回发往 market-api / hera-webapp 的 Cookie 头。
    - 若环境变量 FENBI_COOKIE 已设置（含显式空字符串）：仅用其值，strip 后为空则不带 Cookie。
    - 若未设置 FENBI_COOKIE：使用模块内 _DEFAULT_FENBI_COOKIE。
    """
    if "FENBI_COOKIE" in os.environ:
        raw = os.environ["FENBI_COOKIE"]
        c = (raw or "").strip()
        return c if c else None
    c = _DEFAULT_FENBI_COOKIE.strip()
    return c if c else None


def _headers() -> Dict[str, str]:
    h: Dict[str, str] = {
        "User-Agent": DEFAULT_UA,
        "Referer": "https://fenbi.com/",
        "Origin": "https://fenbi.com",
    }
    ck = _fenbi_cookie()
    if ck:
        h["Cookie"] = ck
    return h


def _market_qs(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    p = {"app": "web", "av": 100, "hav": 100, "kav": 100}
    if extra:
        p.update(extra)
    return p


def _parse_article_id(raw: str) -> str:
    s = (raw or "").strip()
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
    raise ValueError(f"无法解析粉笔公告 id: {raw!r}")


def _strip_noise(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if all(n in line for n in ("免费报考咨询", "尽在粉笔")):
            continue
        if line in NOISE_LINES:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _html_to_body(html: str) -> str:
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
            return _strip_noise(tr_out)
    except Exception:
        pass

    candidates: List[Tuple[int, str]] = []
    for div in soup.find_all("div"):
        raw = div.get_text("\n", strip=True)
        if len(raw) < 1200:
            continue
        if "公告" not in raw and "招聘" not in raw:
            continue
        candidates.append((len(raw), raw))

    if not candidates:
        body = soup.get_text("\n", strip=True)
        return _strip_noise(body)[:50000]

    candidates.sort(key=lambda x: x[0])
    return _strip_noise(candidates[0][1])[:50000]


class StandardResponse(BaseModel):
    code: int = 200
    message: str = "Success"
    data: Optional[Any] = None
    timestamp: str = ""


def _ok(data: Any, message: str = "Success") -> JSONResponse:
    body = StandardResponse(
        code=200,
        message=message,
        data=data,
        timestamp=datetime.now().isoformat(),
    ).model_dump()
    return JSONResponse(content=body)


def _err(code: int, message: str) -> JSONResponse:
    body = StandardResponse(
        code=code,
        message=message,
        data=None,
        timestamp=datetime.now().isoformat(),
    ).model_dump()
    return JSONResponse(status_code=code, content=body)


async def _hera_article_bundle(
    client: httpx.AsyncClient, article_id: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    aid = str(article_id).strip()
    sum_url = f"{HERA_ORIGIN}/api/website/article/summary"
    r_sum = await client.get(sum_url, params={"id": aid})
    if r_sum.status_code != 200:
        return None, f"摘要 HTTP {r_sum.status_code}"
    try:
        j_sum = r_sum.json()
    except Exception:
        return None, "摘要非 JSON"
    if j_sum.get("code") != 1:
        return None, j_sum.get("msg") or "摘要业务错误"
    data_sum = j_sum.get("data") or {}
    if not data_sum.get("id"):
        return None, "未找到该公告"
    detail_url = data_sum.get("contentURL") or f"{HERA_ORIGIN}/api/article/detail?id={aid}"
    r_html = await client.get(detail_url)
    if r_html.status_code != 200:
        return None, f"详情页 HTTP {r_html.status_code}"
    body_text = _html_to_body(r_html.text)
    return {
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
    }, None


async def _market_position_detail(
    client: httpx.AsyncClient, position_id: int
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"{MARKET_PC}/position/detail"
    r = await client.get(url, params=_market_qs({"positionId": position_id}))
    if r.status_code != 200:
        return None, f"职位详情 HTTP {r.status_code}"
    try:
        j = r.json()
    except Exception:
        return None, "职位详情非 JSON"
    if j.get("code") != 1:
        return None, j.get("msg") or "职位详情业务错误"
    return j.get("data"), None


async def _build_major_tree(
    client: httpx.AsyncClient, major_type_id: int, major_degree: int
) -> List[Dict[str, Any]]:
    base = f"{MARKET_PC}/major/listByLevel"
    r1 = await client.get(
        base, params=_market_qs({"majorTypeId": major_type_id, "majorDegree": major_degree})
    )
    if r1.status_code != 200:
        return []
    j1 = r1.json()
    l1 = j1.get("datas") or []
    if not l1:
        return []

    async def level(parent_code: str) -> List[Dict[str, Any]]:
        r = await client.get(
            base,
            params=_market_qs(
                {
                    "majorTypeId": major_type_id,
                    "majorDegree": major_degree,
                    "parentCode": parent_code,
                }
            ),
        )
        if r.status_code != 200:
            return []
        return r.json().get("datas") or []

    res2 = await asyncio.gather(*[level(c["value"]) for c in l1], return_exceptions=True)
    l2_map: Dict[str, List[Dict[str, Any]]] = {}
    for parent, r in zip(l1, res2):
        if not isinstance(r, Exception):
            l2_map[parent["value"]] = r

    all_l2: List[Dict[str, Any]] = []
    for lst in l2_map.values():
        all_l2.extend(lst)

    res3 = await asyncio.gather(*[level(c["value"]) for c in all_l2], return_exceptions=True)
    l3_map: Dict[str, List[Dict[str, Any]]] = {}
    for parent, r in zip(all_l2, res3):
        if not isinstance(r, Exception):
            l3_map[parent["value"]] = r

    tree: List[Dict[str, Any]] = []
    for c1 in l1:
        n1 = {"name": c1["name"], "value": c1["value"], "children": []}
        for c2 in l2_map.get(c1["value"], []):
            n2 = {"name": c2["name"], "value": c2["value"], "children": []}
            for c3 in l3_map.get(c2["value"], []):
                n2["children"].append({"name": c3["name"], "value": c3["value"]})
            n1["children"].append(n2)
        tree.append(n1)
    return tree


def _district_ids_for_position(district_id: int) -> List[int]:
    return [district_id] if district_id else [district_id]


class FenbiActionBody(BaseModel):
    op: str = Field(
        ...,
        description="announcements | positions | both | article | position_detail",
        pattern="^(announcements|positions|both|article|position_detail)$",
    )
    exam_type: Optional[int] = Field(None, description="考试类型，列表类必填")
    district_id: int = Field(0, description="地区，0 表示全国等")
    year: int = Field(default_factory=lambda: datetime.now().year)
    enroll_status: int = 0
    recruit_num_code: int = 0
    start: int = Field(0, ge=0)
    page_size: int = Field(20, ge=1, le=50)
    need_total: bool = True
    title_keyword: Optional[str] = None
    include_detail: bool = False
    max_details: int = Field(5, ge=0, le=30)
    detail_concurrency: int = Field(3, ge=1, le=10)
    exam_id: Optional[int] = None
    major_degree: Optional[int] = None
    major_code: Optional[str] = None
    option_contents: Optional[List[Dict[str, Any]]] = None
    article_id: Optional[str] = Field(None, description="op=article 时必填")
    position_id: Optional[int] = Field(None, description="op=position_detail 时必填")

    @model_validator(mode="after")
    def _validate_op(self) -> FenbiActionBody:
        if self.op in ("announcements", "positions", "both"):
            if self.exam_type is None:
                raise ValueError("列表类 op 需要 exam_type（请先用 GET /meta 查看 exam_types）")
        if self.op == "article":
            if not (self.article_id and str(self.article_id).strip()):
                raise ValueError("op=article 需要 article_id")
        if self.op == "position_detail":
            if self.position_id is None:
                raise ValueError("op=position_detail 需要 position_id")
        return self


def _position_post_body(body: FenbiActionBody) -> Dict[str, Any]:
    d = _district_ids_for_position(body.district_id)
    out: Dict[str, Any] = {
        "examType": body.exam_type,
        "districtIds": d[-1:] if len(d) > 1 else d,
        "start": body.start,
        "len": body.page_size,
    }
    if body.exam_id is not None:
        out["examId"] = body.exam_id
    if body.major_degree is not None:
        out["majorDegree"] = body.major_degree
    if body.major_code:
        out["majorCode"] = body.major_code
    if body.option_contents is not None:
        out["optionContents"] = body.option_contents
    return out


async def _run_announcements(
    client: httpx.AsyncClient, body: FenbiActionBody
) -> Dict[str, Any]:
    url = f"{MARKET_PC}/exam/queryByCondition"
    req_body = {
        "districtId": body.district_id,
        "examType": body.exam_type,
        "year": body.year,
        "enrollStatus": body.enroll_status,
        "recruitNumCode": body.recruit_num_code,
        "start": body.start,
        "len": body.page_size,
        "needTotal": body.need_total,
    }
    r = await client.post(
        url,
        params=_market_qs(),
        json=req_body,
        headers={**_headers(), "Content-Type": "application/json"},
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
    if body.title_keyword:
        kw = body.title_keyword.strip().lower()
        merged = [x for x in merged if kw in (x.get("title") or "").lower()]

    items: List[Dict[str, Any]] = []
    for raw in merged:
        aid = raw.get("id")
        items.append({"article_id": str(aid) if aid is not None else None, "list_row": raw})

    warnings: List[str] = []
    if body.include_detail and body.max_details > 0:
        aids = [i["article_id"] for i in items if i.get("article_id")]
        take = aids[: body.max_details]
        if len(aids) > len(take):
            warnings.append(f"详情仅拉取前 {body.max_details} 条，其余 {len(aids) - len(take)} 条已跳过")
        sem = asyncio.Semaphore(body.detail_concurrency)

        async def one(aid: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
            async with sem:
                d, e = await _hera_article_bundle(client, aid)
                return aid, d, e

        results = await asyncio.gather(*[one(a) for a in take])
        dm = {aid: (d, e) for aid, d, e in results}
        for it in items:
            aid = it.get("article_id")
            if aid and aid in dm:
                d, e = dm[aid]
                if e:
                    it["detail"] = None
                    it["detail_error"] = e
                else:
                    it["detail"] = d

    return {
        "total": total,
        "stick_top_count": len(stick),
        "page_items_count": len(articles),
        "items": items,
        "warnings": warnings,
        "upstream": {"path": "/exam/queryByCondition", "request_body": req_body},
    }


async def _run_positions(client: httpx.AsyncClient, body: FenbiActionBody) -> Dict[str, Any]:
    url = f"{MARKET_PC}/position/queryByConditions"
    req_body = _position_post_body(body)
    r = await client.post(
        url,
        params=_market_qs(),
        json=req_body,
        headers={**_headers(), "Content-Type": "application/json"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"queryByConditions HTTP {r.status_code}")
    j = r.json()
    if j.get("code") != 1:
        raise RuntimeError(j.get("msg") or "queryByConditions 业务错误")
    rows = j.get("datas") or []
    total = j.get("total")
    items = [{"position_id": raw.get("id"), "list_row": raw} for raw in rows]
    warnings: List[str] = []

    if body.include_detail and body.max_details > 0:
        pids = [i["position_id"] for i in items if i.get("position_id") is not None]
        take = pids[: body.max_details]
        if len(pids) > len(take):
            warnings.append(f"职位详情仅拉取前 {body.max_details} 条")
        sem = asyncio.Semaphore(body.detail_concurrency)

        async def one(pid: int) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
            async with sem:
                d, e = await _market_position_detail(client, pid)
                return pid, d, e

        results = await asyncio.gather(*[one(int(x)) for x in take])
        pm = {pid: (d, e) for pid, d, e in results}
        for it in items:
            pid = it.get("position_id")
            if pid is not None and int(pid) in pm:
                d, e = pm[int(pid)]
                if e:
                    it["detail"] = None
                    it["detail_error"] = e
                else:
                    it["detail"] = d

    return {
        "total": total,
        "items": items,
        "warnings": warnings,
        "upstream": {"path": "/position/queryByConditions", "request_body": req_body},
    }


async def _assemble_meta(
    client: httpx.AsyncClient,
    exam_type: Optional[int],
    expand_majors: int,
) -> Dict[str, Any]:
    r_exam = await client.get(f"{MARKET_PC}/exam/conditions", params=_market_qs())
    if r_exam.status_code != 200:
        raise RuntimeError(f"exam/conditions HTTP {r_exam.status_code}")
    try:
        j_exam = r_exam.json()
    except Exception:
        raise RuntimeError("exam/conditions 非 JSON")
    if j_exam.get("code") != 1:
        raise RuntimeError(j_exam.get("msg") or "exam/conditions 业务错误")
    exam_data = j_exam.get("data") or {}

    out: Dict[str, Any] = {
        "districts": exam_data.get("districtList", []),
        "exam_types": exam_data.get("examTypeList", []),
        "years": exam_data.get("yearList", []),
        "enroll_statuses": exam_data.get("enrollStatusList", []),
        "recruit_nums": exam_data.get("recruitNumList", []),
        "position_conditions": None,
        "major_tree": None,
        "cache_ttl_sec": META_CACHE_TTL_SEC,
        "exam_type_requested": exam_type,
        "expand_majors": bool(expand_majors),
    }

    if exam_type is not None:
        r_pos = await client.get(
            f"{MARKET_PC}/position/commonConditions",
            params=_market_qs({"examType": str(exam_type)}),
        )
        if r_pos.status_code != 200:
            raise RuntimeError(f"commonConditions HTTP {r_pos.status_code}")
        j_pos = r_pos.json()
        if j_pos.get("code") != 1:
            raise RuntimeError(j_pos.get("msg") or "commonConditions 业务错误")
        pos_data = j_pos.get("data") or {}
        out["position_conditions"] = pos_data

        if expand_majors:
            mid = pos_data.get("majorTypeId")
            degrees = pos_data.get("majorDegrees") or []
            if mid and degrees:
                trees: Dict[str, Any] = {}
                tasks = [_build_major_tree(client, int(mid), int(md["value"])) for md in degrees]
                done = await asyncio.gather(*tasks, return_exceptions=True)
                for md, tr in zip(degrees, done):
                    if not isinstance(tr, Exception):
                        trees[str(md["value"])] = tr
                out["major_tree"] = trees

    return out


@router.get(
    "/scrape/fenbi/meta",
    summary="粉笔 — 字典与可选职位侧条件",
    description=(
        "拉取 exam/conditions。若提供 query 参数 exam_type，则再拉该考试类型下的 "
        "position/commonConditions；expand_majors=1 时再展开三级专业树。结果带 TTL 缓存。"
    ),
)
async def fenbi_meta(
    exam_type: Optional[int] = Query(None, description="考试类型 id；不传则不含职位侧 majors 配置"),
    expand_majors: int = Query(0, ge=0, le=1, description="1 时在已传 exam_type 前提下展开专业树"),
):
    if expand_majors and exam_type is None:
        return _err(400, "expand_majors=1 时必须同时提供 exam_type")

    key = (exam_type, expand_majors)
    now = time.monotonic()
    async with _META_LOCK:
        ent = _META_CACHE.get(key)
        if ent and ent[0] > now:
            return _ok({**ent[1], "cached": True})

    try:
        async with httpx.AsyncClient(
            headers=_headers(),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100),
            timeout=60.0,
        ) as client:
            payload = await _assemble_meta(client, exam_type, expand_majors)
    except RuntimeError as e:
        return _err(502, str(e))
    except Exception as e:
        logger.exception("fenbi_meta failed")
        return _err(502, str(e))

    payload["cached"] = False
    async with _META_LOCK:
        _META_CACHE[key] = (now + META_CACHE_TTL_SEC, payload)
    return _ok(payload)


@router.post(
    "/scrape/fenbi/action",
    summary="粉笔 — 统一数据动作",
    description="通过 op 拉公告列表、职位列表、单条公告正文、单条职位详情或组合。",
)
async def fenbi_action(body: FenbiActionBody):
    try:
        async with httpx.AsyncClient(
            headers=_headers(), follow_redirects=True, timeout=60.0
        ) as client:
            if body.op == "article":
                try:
                    aid = _parse_article_id(body.article_id or "")
                except ValueError as e:
                    return _err(400, str(e))
                out, err = await _hera_article_bundle(client, aid)
                if err:
                    if "未找到" in (err or ""):
                        return _err(404, err)
                    return _err(502, err)
                msg = (
                    "ok"
                    if len((out or {}).get("content_text") or "") >= 200
                    else "正文较短，可能需调整解析规则或页面结构已变"
                )
                return _ok(out, message=msg)

            if body.op == "position_detail":
                d, err = await _market_position_detail(client, int(body.position_id or 0))
                if err:
                    return _err(502, err)
                return _ok(d)

            if body.op == "announcements":
                try:
                    data = await _run_announcements(client, body)
                except RuntimeError as e:
                    return _err(502, str(e))
                return _ok(data)

            if body.op == "positions":
                try:
                    data = await _run_positions(client, body)
                except RuntimeError as e:
                    return _err(502, str(e))
                return _ok(data)

            if body.op == "both":
                try:
                    ann, pos = await asyncio.gather(
                        _run_announcements(client, body),
                        _run_positions(client, body),
                    )
                except RuntimeError as e:
                    return _err(502, str(e))
                return _ok({"announcements": ann, "positions": pos})

    except Exception as e:
        logger.exception("fenbi_action failed")
        return _err(502, str(e))

    return _err(500, "未知 op")
