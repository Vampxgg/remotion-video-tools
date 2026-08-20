# -*- coding: utf-8 -*-
"""每日 Azure 模型消耗报告 router(自动化 + 在线预览)。

融入现有 FastAPI 项目的标准范式(仿 api/web_search.py)：
- 定时：``lifespan_resources`` 内起一个 asyncio 每日调度循环替代 systemd/cron，
  每天北京 ``USAGE_REPORT_SCHEDULE_HHMM`` 到点在线程池里跑 daily_pipeline.run。
- 鉴权：``require_api_key("USAGE_REPORT_API_KEY")``(留空=不鉴权)。
- 落盘：全部产物在 ``static/azure_cost_export_func/_data/`` 下，html 可经已挂载的
  ``/static`` 直接在线访问，md 由本 router 用 markdown 库在线渲染。

端点(前缀 /api)：
- ``POST /api/usage/run``      手动触发/补跑(body 可选 date、upload)，后台执行
- ``GET  /api/usage/reports``  列出已生成的报告日期 + md/html 链接
- ``GET  /api/usage/report/{date}/md``    在线渲染 md 为 HTML 预览
- ``GET  /api/usage/report/{date}/html``  302 跳转到 /static 下自包含 html
- ``GET  /api/usage/status``   调度器状态 / last_run / 下次触发时间

本 router 不改动现有云函数(function_app.py)，仅复用其 calls/requests blob 产物。
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

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from utils.logger import setup_module_logger
from utils.responses import create_standard_response
from utils.security import require_api_key
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/usage/report.log")

# 只读预览端点(首页/列表/html/md/status)完全公开，方便浏览器直接打开；
# 仅写操作端点(POST /usage/run)单独挂 x-api-key 守卫，防误触/滥用。
router = APIRouter()

CST = dt.timezone(dt.timedelta(hours=8))
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 项目根下 static/azure_cost_export_func/daily_pipeline.py 的绝对路径。
_PIPELINE_PATH = (
    Path(_settings.static_dir_abs) / "azure_cost_export_func" / "daily_pipeline.py"
)

# 调度器运行态(仅本进程可见；多 worker 各自持有)。
_scheduler_task: Optional[asyncio.Task] = None
_last_run: dict[str, Any] = {}
_next_run_iso: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════
#  加载纯逻辑模块 + 组装 PipelineConfig
# ══════════════════════════════════════════════════════════════════════

def _load_pipeline() -> ModuleType:
    """按文件路径加载 daily_pipeline(static 目录非包，避免 import 路径问题)。"""
    spec = importlib.util.spec_from_file_location("usage_daily_pipeline", str(_PIPELINE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 daily_pipeline @ {_PIPELINE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_cfg() -> dict[str, Any]:
    """从 settings 组装 PipelineConfig(daily_pipeline.run 消费的 dict)。"""
    data_dir = _settings.usage_report_data_dir_abs
    return {
        "subscription_id": _settings.USAGE_REPORT_SUBSCRIPTION_ID,
        "resource_group": _settings.USAGE_REPORT_RESOURCE_GROUP,
        "storage_account": _settings.USAGE_REPORT_STORAGE_ACCOUNT,
        "blob_container": _settings.USAGE_REPORT_BLOB_CONTAINER,
        "export_name": _settings.USAGE_REPORT_EXPORT_NAME,
        "api_version": _settings.USAGE_REPORT_ARM_API_VERSION,
        "daily_dir": data_dir / "daily_csv",
        "poll_seconds": _settings.USAGE_REPORT_POLL_SECONDS,
        "poll_max": _settings.USAGE_REPORT_POLL_MAX,
        "skip_if_csv_exists": _settings.USAGE_REPORT_SKIP_IF_CSV_EXISTS,
        "data_dir": data_dir,
        "calls_prefix": _settings.USAGE_REPORT_CALLS_PREFIX,
        "requests_prefix": _settings.USAGE_REPORT_REQUESTS_PREFIX,
        "out_prefix": _settings.USAGE_REPORT_OUT_PREFIX,
        "upload_blob": _settings.USAGE_REPORT_UPLOAD_BLOB,
        "src_suffix": _settings.USAGE_REPORT_SRC_SUFFIX,
        "out_suffix": _settings.USAGE_REPORT_OUT_SUFFIX,
    }


def _run_pipeline_sync(date: str, upload: Optional[bool]) -> dict[str, Any]:
    """在工作线程里同步执行流水线(阻塞 IO：ARM 轮询/下载/生成)。"""
    pipeline = _load_pipeline()
    cfg = _build_cfg()
    return pipeline.run(date, cfg, upload=upload)


async def _run_pipeline(date: str, upload: Optional[bool]) -> dict[str, Any]:
    """把阻塞流水线丢到默认线程池，避免堵塞事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_pipeline_sync, date, upload)


# ══════════════════════════════════════════════════════════════════════
#  定时调度(lifespan 内 asyncio 循环，替代 cron)
# ══════════════════════════════════════════════════════════════════════

def _default_date() -> str:
    """昨天(北京自然日)。"""
    return (dt.datetime.now(CST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")


def _seconds_until_next(hhmm: str) -> tuple[float, dt.datetime]:
    """计算距离下一个北京 HH:MM 的秒数与目标时刻。"""
    hh, mm = (int(x) for x in hhmm.split(":"))
    now = dt.datetime.now(CST)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds(), target


async def _scheduler_loop() -> None:
    """每天到点跑一次昨日报告。异常不致命，记录后等下一轮。"""
    global _next_run_iso
    hhmm = _settings.USAGE_REPORT_SCHEDULE_HHMM
    logger.info("每日消耗报告调度器已启动(每天北京 %s)", hhmm)
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
        logger.info("到点，自动生成 %s 的消耗报告…", date)
        try:
            result = await _run_pipeline(date, None)
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
    """由 main.py lifespan 调用：按配置起/停每日调度器。"""
    global _scheduler_task
    if _settings.USAGE_REPORT_ENABLE_SCHEDULER:
        _scheduler_task = asyncio.create_task(_scheduler_loop())
        logger.info("usage_report router 就绪(调度器已开启)")
    else:
        logger.info("usage_report router 就绪(调度器未开启 USAGE_REPORT_ENABLE_SCHEDULER=False)")
    try:
        yield
    finally:
        logger.info("usage_report router 正在关闭 …")
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
    """POST /api/usage/run 的请求体。"""

    date: Optional[str] = Field(
        None, description="目标日期 YYYY-MM-DD(北京自然日)；留空=昨天。"
    )
    upload: Optional[bool] = Field(
        None, description="是否回传 blob；留空=用 USAGE_REPORT_UPLOAD_BLOB 默认。"
    )
    wait: bool = Field(
        False, description="是否同步等待生成完成再返回(默认 False，后台执行立即受理)。"
    )


def _validate_date(date: str) -> str:
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=400, detail="date 必须是 YYYY-MM-DD 格式")
    return date


def _static_url(date: str, ext: str) -> str:
    """当天产物在 /static 下的可访问 URL。"""
    subdir = _settings.USAGE_REPORT_DATA_SUBDIR.strip("/")
    return (
        f"/{_settings.STATIC_DIR}/{subdir}/reports/{date}/usage-{date}-CST.{ext}"
    )


@router.post("/usage/run", summary="手动触发/补跑指定日期的消耗报告",
             dependencies=[Depends(require_api_key("USAGE_REPORT_API_KEY"))])
async def run_report(req: RunRequest):
    """触发一次流水线。默认后台执行立即返回；wait=True 则同步等待产物。"""
    date = _validate_date(req.date) if req.date else _default_date()

    if req.wait:
        try:
            result = await _run_pipeline(date, req.upload)
        except Exception as e:
            logger.error("手动生成失败(%s): %s", date, e, exc_info=True)
            raise HTTPException(status_code=502, detail=f"生成失败: {e}")
        _last_run.clear()
        _last_run.update({"triggered": "manual", "at": dt.datetime.now(CST).isoformat(), **result})
        return create_standard_response(data=result, message="生成完成")

    async def _bg() -> None:
        try:
            result = await _run_pipeline(date, req.upload)
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
        data={"date": date, "accepted": True},
        code=202,
        message="已受理，后台生成中",
    )


@router.get("/usage", response_class=HTMLResponse, include_in_schema=False)
@router.get("/usage/", response_class=HTMLResponse,
            summary="在线预览首页：报告列表")
async def reports_index():
    """扫描 _data/reports/，渲染一个报告列表首页(纯只读，浏览器直接打开)。

    每个日期给两个入口：图表版自包含 HTML 与 Markdown 渲染版。
    """
    data_dir = _settings.usage_report_data_dir_abs
    reports_dir = data_dir / "reports"
    rows: list[str] = []
    count = 0
    if reports_dir.exists():
        for d in sorted(reports_dir.iterdir(), reverse=True):
            if not d.is_dir() or not _DATE_RE.match(d.name):
                continue
            date = d.name
            has_html = (d / f"usage-{date}-CST.html").exists()
            has_md = (d / f"usage-{date}-CST.md").exists()
            if not (has_html or has_md):
                continue
            count += 1
            html_btn = (
                f'<a class="btn primary" href="/api/usage/report/{date}/html">图表版</a>'
                if has_html else '<span class="btn disabled">无图表版</span>'
            )
            md_btn = (
                f'<a class="btn" href="/api/usage/report/{date}/md">Markdown 版</a>'
                if has_md else '<span class="btn disabled">无 MD</span>'
            )
            rows.append(
                f'<tr><td class="date">{date}</td>'
                f'<td class="acts">{html_btn}{md_btn}</td></tr>'
            )
    body = (
        "".join(rows)
        if rows
        else '<tr><td colspan="2" class="empty">暂无已生成的报告，'
             '可调用 <code>POST /api/usage/run</code> 生成。</td></tr>'
    )
    next_run = _next_run_iso or "未开启调度"
    page = _INDEX_PAGE_TEMPLATE.format(count=count, rows=body, next_run=next_run)
    return HTMLResponse(content=page)


@router.get("/usage/reports", summary="列出已生成的每日报告")
async def list_reports():
    """扫描 _data/reports/ 下的日期目录，倒序返回可预览链接。"""
    data_dir = _settings.usage_report_data_dir_abs
    reports_dir = data_dir / "reports"
    items: list[dict[str, Any]] = []
    if reports_dir.exists():
        for d in sorted(reports_dir.iterdir(), reverse=True):
            if not d.is_dir() or not _DATE_RE.match(d.name):
                continue
            date = d.name
            md = d / f"usage-{date}-CST.md"
            html = d / f"usage-{date}-CST.html"
            if not (md.exists() or html.exists()):
                continue
            items.append({
                "date": date,
                "has_md": md.exists(),
                "has_html": html.exists(),
                "md_preview": f"/api/usage/report/{date}/md" if md.exists() else None,
                "html_url": _static_url(date, "html") if html.exists() else None,
            })
    return create_standard_response(data={"count": len(items), "reports": items})


@router.get("/usage/report/{date}/html", summary="打开自包含 HTML 报告")
async def report_html(date: str):
    """302 跳转到 /static 下的自包含 HTML(内嵌 ECharts)。"""
    _validate_date(date)
    html = _settings.usage_report_data_dir_abs / "reports" / date / f"usage-{date}-CST.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail=f"{date} 的 HTML 报告不存在")
    return RedirectResponse(url=_static_url(date, "html"))


@router.get("/usage/report/{date}/md", response_class=HTMLResponse,
            summary="在线渲染 Markdown 报告")
async def report_md(date: str):
    """读取本地 md 用 markdown 库渲染成带样式 HTML 在线预览。"""
    _validate_date(date)
    md_path = _settings.usage_report_data_dir_abs / "reports" / date / f"usage-{date}-CST.md"
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


@router.get("/usage/status", summary="调度器状态与最近一次运行")
async def status_report():
    """返回调度器开关、下次触发时间、最近一次运行结果。"""
    return create_standard_response(data={
        "scheduler_enabled": _settings.USAGE_REPORT_ENABLE_SCHEDULER,
        "schedule_hhmm_cst": _settings.USAGE_REPORT_SCHEDULE_HHMM,
        "scheduler_running": _scheduler_task is not None and not _scheduler_task.done(),
        "next_run": _next_run_iso,
        "last_run": _last_run or None,
        "data_dir": str(_settings.usage_report_data_dir_abs),
    })


_MD_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Azure 消耗报告 · {date}</title>
<style>
  body {{ margin:0; background:#0f1620; color:#dfe6ee;
         font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
         line-height:1.6; }}
  .wrap {{ max-width:1000px; margin:0 auto; padding:28px 22px 80px; }}
  h1,h2,h3 {{ color:#eaf1f8; }}
  h1 {{ font-size:24px; border-bottom:2px solid #2a3644; padding-bottom:8px; }}
  h2 {{ font-size:19px; border-left:4px solid #4c9aff; padding-left:10px; margin-top:32px; }}
  a {{ color:#4c9aff; }}
  table {{ width:100%; border-collapse:collapse; margin:14px 0; font-size:13px;
          background:#151d28; border:1px solid #26313f; border-radius:8px; overflow:hidden; }}
  th,td {{ padding:8px 12px; border-bottom:1px solid #26313f; text-align:right; }}
  th:first-child,td:first-child {{ text-align:left; }}
  th {{ background:#1c2733; color:#9fb0c2; }}
  tbody tr:hover {{ background:#1a2430; }}
  code,pre {{ background:#0b1016; border-radius:6px; }}
  pre {{ padding:12px; overflow:auto; }}
  code {{ padding:2px 5px; }}
  blockquote {{ border-left:3px solid #3a4757; margin:12px 0; padding:4px 14px; color:#9fb0c2; }}
</style></head>
<body><div class="wrap">{body}</div></body></html>"""


_INDEX_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Azure 消耗报告 · 在线预览</title>
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
  .btn:hover {{ background:#243244; border-color:#4c9aff; }}
  .btn.primary {{ background:#2563d6; border-color:#2563d6; color:#fff; }}
  .btn.primary:hover {{ background:#3b78ea; }}
  .btn.disabled {{ opacity:.4; }}
  .empty {{ text-align:center; color:#8ea0b4; padding:32px; }}
  code {{ background:#0b1016; padding:2px 6px; border-radius:5px; }}
</style></head>
<body><div class="wrap">
  <h1>Azure 模型消耗报告</h1>
  <div class="sub">共 <b>{count}</b> 份报告 · 下一次自动生成：<b>{next_run}</b></div>
  <table>
    <thead><tr><th>日期(北京自然日)</th><th style="text-align:right">预览</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div></body></html>"""
