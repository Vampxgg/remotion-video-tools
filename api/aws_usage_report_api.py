# -*- coding: utf-8 -*-
"""每日 AWS Bedrock 用量报告 router(自动化 + 在线预览)。

融入现有 FastAPI 项目标准范式(仿 api/usage_report_api.py)：
- 定时：``lifespan_resources`` 内起一个 asyncio 每日调度循环，每天北京
  ``AWS_USAGE_REPORT_SCHEDULE_HHMM`` 到点在线程池里跑 daily_pipeline.run。
- 鉴权：写端点挂 ``require_api_key("AWS_USAGE_REPORT_API_KEY")``(留空=不鉴权)。
- 落盘：全部产物在 ``static/aws_cost_export_func/_data/`` 下，html 经已挂载 ``/static``
  在线访问，md 由本 router 用 markdown 库在线渲染。

数据链：本机弱权限用户 AssumeRole 到子账号 Admin → CE(成本双口径+credit) +
CloudWatch Logs(次数+四类token) + CloudTrail(IP+活跃时段)。

端点(前缀 /api)：
- ``POST /api/aws-usage/run``           手动触发/补跑(body 可选 date)，后台执行
- ``GET  /api/aws-usage/``              在线预览首页：报告列表
- ``GET  /api/aws-usage/reports``       列出已生成报告日期 + md/html 链接
- ``GET  /api/aws-usage/report/{date}/md``    在线渲染 md 为 HTML
- ``GET  /api/aws-usage/report/{date}/html``  302 跳转到 /static 下自包含 html
- ``GET  /api/aws-usage/status``        调度器状态 / last_run / 下次触发
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.security import require_api_key
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/aws_usage/report.log")

router = APIRouter()

CST = dt.timezone(dt.timedelta(hours=8))
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_PIPELINE_PATH = (
    Path(_settings.static_dir_abs) / "aws_cost_export_func" / "daily_pipeline.py"
)

_scheduler_task: Optional[asyncio.Task] = None
_last_run: dict[str, Any] = {}
_next_run_iso: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════
#  加载纯逻辑模块 + 组装 PipelineConfig
# ══════════════════════════════════════════════════════════════════════

def _load_pipeline() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aws_usage_daily_pipeline", str(_PIPELINE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 daily_pipeline @ {_PIPELINE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_cfg() -> dict[str, Any]:
    data_dir = _settings.aws_usage_report_data_dir_abs
    return {
        "assume_role_arn": _settings.AWS_USAGE_REPORT_ASSUME_ROLE_ARN,
        "region": _settings.AWS_USAGE_REPORT_REGION,
        "regions": [r.strip() for r in _settings.AWS_USAGE_REPORT_REGIONS.split(",") if r.strip()],
        "linked_account": _settings.AWS_USAGE_REPORT_LINKED_ACCOUNT,
        "log_group": _settings.AWS_USAGE_REPORT_LOG_GROUP,
        "data_dir": data_dir,
        "granularity": _settings.AWS_USAGE_REPORT_GRANULARITY,
        "skip_if_exists": _settings.AWS_USAGE_REPORT_SKIP_IF_EXISTS,
    }


def _run_pipeline_sync(date: str) -> dict[str, Any]:
    """在工作线程里同步执行流水线(阻塞 IO：AssumeRole/CE/Logs/CloudTrail)。"""
    pipeline = _load_pipeline()
    cfg = _build_cfg()
    return pipeline.run(date, cfg)


async def _run_pipeline(date: str) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_pipeline_sync, date)


# ══════════════════════════════════════════════════════════════════════
#  定时调度
# ══════════════════════════════════════════════════════════════════════

def _default_date() -> str:
    return (dt.datetime.now(CST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")


def _seconds_until_next(hhmm: str) -> tuple[float, dt.datetime]:
    hh, mm = (int(x) for x in hhmm.split(":"))
    now = dt.datetime.now(CST)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds(), target


async def _scheduler_loop() -> None:
    global _next_run_iso
    hhmm = _settings.AWS_USAGE_REPORT_SCHEDULE_HHMM
    logger.info("AWS 用量报告调度器已启动(每天北京 %s)", hhmm)
    while True:
        wait_s, target = _seconds_until_next(hhmm)
        _next_run_iso = target.isoformat()
        logger.info("下一次自动生成: %s(%.0f 秒后)", _next_run_iso, wait_s)
        try:
            await asyncio.sleep(wait_s)
        except asyncio.CancelledError:
            logger.info("调度器收到取消信号，退出。")
            raise
        date = _default_date()
        logger.info("到点，自动生成 %s 的 AWS 用量报告…", date)
        try:
            result = await _run_pipeline(date)
            _last_run.clear()
            _last_run.update({"triggered": "scheduler", "at": dt.datetime.now(CST).isoformat(), **result})
            logger.info("自动生成完成: %s", result)
        except Exception as e:
            _last_run.clear()
            _last_run.update({"triggered": "scheduler", "at": dt.datetime.now(CST).isoformat(),
                              "date": date, "error": str(e)})
            logger.error("自动生成失败(%s): %s", date, e, exc_info=True)


@asynccontextmanager
async def lifespan_resources(app):
    global _scheduler_task
    if _settings.AWS_USAGE_REPORT_ENABLE_SCHEDULER:
        _scheduler_task = asyncio.create_task(_scheduler_loop())
        logger.info("aws_usage_report router 就绪(调度器已开启)")
    else:
        logger.info("aws_usage_report router 就绪(调度器未开启)")
    try:
        yield
    finally:
        logger.info("aws_usage_report router 正在关闭 …")
        if _scheduler_task is not None:
            _scheduler_task.cancel()
            try:
                await _scheduler_task
            except asyncio.CancelledError:
                pass
            finally:
                _scheduler_task = None


# ══════════════════════════════════════════════════════════════════════
#  端点
# ══════════════════════════════════════════════════════════════════════

class RunRequest(BaseModel):
    date: Optional[str] = Field(None, description="目标日期 YYYY-MM-DD(北京自然日)；留空=昨天。")
    wait: bool = Field(False, description="是否同步等待生成完成再返回(默认后台执行立即受理)。")


def _validate_date(date: str) -> str:
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date 必须是 YYYY-MM-DD 格式")
    return date


def _static_url(date: str, ext: str) -> str:
    subdir = _settings.AWS_USAGE_REPORT_DATA_SUBDIR.strip("/")
    return f"/{_settings.STATIC_DIR}/{subdir}/reports/{date}/aws-usage-{date}-CST.{ext}"


@router.post("/aws-usage/run", summary="手动触发/补跑指定日期的 AWS 用量报告",
             dependencies=[Depends(require_api_key("AWS_USAGE_REPORT_API_KEY"))])
async def run_report(req: RunRequest):
    date = _validate_date(req.date) if req.date else _default_date()

    if req.wait:
        try:
            result = await _run_pipeline(date)
        except Exception as e:
            logger.error("手动生成失败(%s): %s", date, e, exc_info=True)
            raise HTTPException(status_code=502, detail=f"生成失败: {e}")
        _last_run.clear()
        _last_run.update({"triggered": "manual", "at": dt.datetime.now(CST).isoformat(), **result})
        return create_standard_response(data=result, message="生成完成")

    async def _bg() -> None:
        try:
            result = await _run_pipeline(date)
            _last_run.clear()
            _last_run.update({"triggered": "manual", "at": dt.datetime.now(CST).isoformat(), **result})
            logger.info("后台手动生成完成: %s", result)
        except Exception as e:
            _last_run.clear()
            _last_run.update({"triggered": "manual", "at": dt.datetime.now(CST).isoformat(),
                              "date": date, "error": str(e)})
            logger.error("后台手动生成失败(%s): %s", date, e, exc_info=True)

    asyncio.create_task(_bg())
    return create_standard_response(
        data={"date": date, "accepted": True}, code=202, message="已受理，后台生成中",
    )


@router.get("/aws-usage", response_class=HTMLResponse, include_in_schema=False)
@router.get("/aws-usage/", response_class=HTMLResponse, summary="在线预览首页：报告列表")
async def reports_index():
    data_dir = _settings.aws_usage_report_data_dir_abs
    reports_dir = data_dir / "reports"
    rows: list[str] = []
    count = 0
    if reports_dir.exists():
        for d in sorted(reports_dir.iterdir(), reverse=True):
            if not d.is_dir() or not _DATE_RE.match(d.name):
                continue
            date = d.name
            has_html = (d / f"aws-usage-{date}-CST.html").exists()
            has_md = (d / f"aws-usage-{date}-CST.md").exists()
            if not (has_html or has_md):
                continue
            count += 1
            html_btn = (
                f'<a class="btn primary" href="/api/aws-usage/report/{date}/html">图表版</a>'
                if has_html else '<span class="btn disabled">无图表版</span>'
            )
            md_btn = (
                f'<a class="btn" href="/api/aws-usage/report/{date}/md">Markdown 版</a>'
                if has_md else '<span class="btn disabled">无 MD</span>'
            )
            rows.append(
                f'<tr><td class="date">{date}</td>'
                f'<td class="acts">{html_btn}{md_btn}</td></tr>'
            )
    body = (
        "".join(rows) if rows
        else '<tr><td colspan="2" class="empty">暂无已生成的报告，'
             '可调用 <code>POST /api/aws-usage/run</code> 生成。</td></tr>'
    )
    next_run = _next_run_iso or "未开启调度"
    page = _INDEX_PAGE_TEMPLATE.format(count=count, rows=body, next_run=next_run)
    return HTMLResponse(content=page)


@router.get("/aws-usage/reports", summary="列出已生成的每日报告")
async def list_reports():
    data_dir = _settings.aws_usage_report_data_dir_abs
    reports_dir = data_dir / "reports"
    items: list[dict[str, Any]] = []
    if reports_dir.exists():
        for d in sorted(reports_dir.iterdir(), reverse=True):
            if not d.is_dir() or not _DATE_RE.match(d.name):
                continue
            date = d.name
            md = d / f"aws-usage-{date}-CST.md"
            html = d / f"aws-usage-{date}-CST.html"
            if not (md.exists() or html.exists()):
                continue
            items.append({
                "date": date,
                "has_md": md.exists(),
                "has_html": html.exists(),
                "md_preview": f"/api/aws-usage/report/{date}/md" if md.exists() else None,
                "html_url": _static_url(date, "html") if html.exists() else None,
            })
    return create_standard_response(data={"count": len(items), "reports": items})


@router.get("/aws-usage/report/{date}/html", summary="打开自包含 HTML 报告")
async def report_html(date: str):
    _validate_date(date)
    html = _settings.aws_usage_report_data_dir_abs / "reports" / date / f"aws-usage-{date}-CST.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail=f"{date} 的 HTML 报告不存在")
    return RedirectResponse(url=_static_url(date, "html"))


@router.get("/aws-usage/report/{date}/md", response_class=HTMLResponse,
            summary="在线渲染 Markdown 报告")
async def report_md(date: str):
    _validate_date(date)
    md_path = _settings.aws_usage_report_data_dir_abs / "reports" / date / f"aws-usage-{date}-CST.md"
    if not md_path.exists():
        raise HTTPException(status_code=404, detail=f"{date} 的 Markdown 报告不存在")
    try:
        import markdown as _md
    except ImportError:
        raise HTTPException(status_code=500, detail="缺少 markdown 依赖，请安装 markdown")

    text = md_path.read_text(encoding="utf-8")
    body = _md.markdown(text, extensions=["tables", "fenced_code", "toc"])
    page = _MD_PAGE_TEMPLATE.format(date=date, body=body)
    return HTMLResponse(content=page)


@router.get("/aws-usage/status", summary="调度器状态与最近一次运行")
async def status_report():
    return create_standard_response(data={
        "scheduler_enabled": _settings.AWS_USAGE_REPORT_ENABLE_SCHEDULER,
        "schedule_hhmm_cst": _settings.AWS_USAGE_REPORT_SCHEDULE_HHMM,
        "scheduler_running": _scheduler_task is not None and not _scheduler_task.done(),
        "next_run": _next_run_iso,
        "last_run": _last_run or None,
        "data_dir": str(_settings.aws_usage_report_data_dir_abs),
    })


_MD_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AWS Bedrock 用量报告 · {date}</title>
<style>
  body {{ margin:0; background:#0f1620; color:#dfe6ee;
         font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; line-height:1.6; }}
  .wrap {{ max-width:1000px; margin:0 auto; padding:28px 22px 80px; }}
  h1,h2,h3 {{ color:#eaf1f8; }}
  h1 {{ font-size:24px; border-bottom:2px solid #2a3644; padding-bottom:8px; }}
  h2 {{ font-size:19px; border-left:4px solid #ff9900; padding-left:10px; margin-top:32px; }}
  a {{ color:#ff9900; }}
  table {{ width:100%; border-collapse:collapse; margin:14px 0; font-size:13px;
          background:#151d28; border:1px solid #26313f; border-radius:8px; overflow:hidden; }}
  th,td {{ padding:8px 12px; border-bottom:1px solid #26313f; text-align:right; }}
  th:first-child,td:first-child {{ text-align:left; }}
  th {{ background:#1c2733; color:#9fb0c2; }}
  tbody tr:hover {{ background:#1a2430; }}
  code,pre {{ background:#0b1016; border-radius:6px; }}
  pre {{ padding:12px; overflow:auto; }}
  code {{ padding:2px 5px; }}
  blockquote {{ border-left:3px solid #d29922; margin:12px 0; padding:4px 14px; color:#e3b341; }}
</style></head>
<body><div class="wrap">{body}</div></body></html>"""


_INDEX_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AWS Bedrock 用量报告 · 在线预览</title>
<style>
  body {{ margin:0; background:#0f1620; color:#dfe6ee;
         font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; line-height:1.6; }}
  .wrap {{ max-width:900px; margin:0 auto; padding:36px 22px 80px; }}
  h1 {{ font-size:26px; color:#eaf1f8; margin:0 0 6px; }}
  .sub {{ color:#8ea0b4; font-size:13px; margin-bottom:26px; }}
  .sub b {{ color:#cfe0f2; }}
  table {{ width:100%; border-collapse:collapse; background:#151d28;
          border:1px solid #26313f; border-radius:10px; overflow:hidden; }}
  th,td {{ padding:14px 18px; border-bottom:1px solid #26313f; text-align:left; }}
  th {{ background:#1c2733; color:#9fb0c2; font-size:13px; }}
  tbody tr:hover {{ background:#1a2430; }}
  tbody tr:last-child td {{ border-bottom:none; }}
  td.date {{ font-size:16px; font-weight:600; color:#eaf1f8; letter-spacing:.5px; }}
  td.acts {{ text-align:right; white-space:nowrap; }}
  .btn {{ display:inline-block; margin-left:10px; padding:7px 16px; border-radius:7px;
         font-size:13px; text-decoration:none; border:1px solid #2f4054; color:#cfe0f2;
         background:#1b2634; transition:.15s; }}
  .btn:hover {{ background:#243244; border-color:#ff9900; }}
  .btn.primary {{ background:#ff9900; border-color:#ff9900; color:#1a1200; }}
  .btn.primary:hover {{ background:#ffad33; }}
  .btn.disabled {{ opacity:.4; }}
  .empty {{ text-align:center; color:#8ea0b4; padding:32px; }}
  code {{ background:#0b1016; padding:2px 6px; border-radius:5px; }}
</style></head>
<body><div class="wrap">
  <h1>AWS Bedrock 模型用量报告</h1>
  <div class="sub">共 <b>{count}</b> 份报告 · 下一次自动生成：<b>{next_run}</b></div>
  <table>
    <thead><tr><th>日期(北京自然日)</th><th style="text-align:right">预览</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div></body></html>"""
