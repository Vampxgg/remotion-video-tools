# -*- coding: utf-8 -*-
# @File：scrape_probe.py
# 用途：旁路探针中间件，记录所有命中 /api/scrape/zhilian（及其 v2 子路径）的请求来源。
#
# 设计目标（零业务侵入）：
#   - 仅作为 Starlette 中间件挂载，不改动 job_search / job_search_v2 的任何业务逻辑。
#   - 记录真实调用方：优先解析 X-Forwarded-For / X-Real-IP（请求经 frp/反代转发，
#     FastAPI 直接看到的 request.client.host 往往是 frp/本机地址），全部字段一并留存。
#   - 落地到独立 SQLite（logs/scrape_probe/probe.db，WAL 模式，多 uvicorn worker 并发安全），
#     不接触任何业务库；同时复用项目 setup_module_logger 写按天滚动日志。
#   - 提供只读统计函数，供 /api/scrape/_probe/stats 端点查询调用量排行。
#
# 移除方式：删掉 main.py 中的 add_middleware / include 两行即可，无其它副作用。

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from utils.logger import setup_module_logger
from utils.settings import settings

logger = setup_module_logger(__name__, "logs/scrape_probe/probe.log")

# 监控的路径前缀：命中该前缀（含 v1 与 v2/sync、v2/async、v2/{task_id}）即记录。
_WATCH_PREFIX = "/api/scrape/zhilian"

# 记录的请求体最大字节数，超过则截断（避免大 payload 撑爆库/日志）。
_MAX_BODY_BYTES = int(os.getenv("SCRAPE_PROBE_MAX_BODY", "4096"))

# 北京时区，日志/统计按本地时间聚合更直观。
_CST = timezone(timedelta(hours=8))


def _db_path() -> str:
    base = settings.LOG_DIR
    if not os.path.isabs(base):
        base = os.path.join(str(settings.project_root), base)
    d = os.path.join(base, "scrape_probe")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "probe.db")


_DB_PATH = _db_path()
# 每个进程独立持有一个连接；WAL 模式允许多进程并发写。用锁保护本进程内多线程写。
_conn_local = threading.local()
_write_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_conn_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(_DB_PATH, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        _conn_local.conn = conn
    return conn


def _init_db() -> None:
    conn = _get_conn()
    with _write_lock:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scrape_hits (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc        TEXT NOT NULL,
                ts_cst        TEXT NOT NULL,
                day_cst       TEXT NOT NULL,
                method        TEXT,
                path          TEXT,
                client_ip     TEXT,          -- 解析后的“真实”来源 IP（XFF/XRealIP 优先）
                peer_ip       TEXT,          -- 直连 IP（frp/反代看到的地址）
                x_forwarded_for TEXT,
                x_real_ip     TEXT,
                user_agent    TEXT,
                referer       TEXT,
                query_string  TEXT,
                body_snippet  TEXT,          -- 截断后的请求体
                body_bytes    INTEGER,       -- 原始请求体字节数
                status_code   INTEGER,
                duration_ms   INTEGER
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hits_day ON scrape_hits(day_cst);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hits_ip ON scrape_hits(client_ip);")
        conn.commit()


_init_db()


def _client_ip_from(request: Request) -> tuple[str, str, Optional[str], Optional[str]]:
    """解析真实来源 IP。

    返回 (client_ip, peer_ip, x_forwarded_for, x_real_ip)。
    - X-Forwarded-For 取最左（最初的客户端），格式 "client, proxy1, proxy2"。
    - 若均无，回退直连 peer_ip。
    """
    xff = request.headers.get("x-forwarded-for")
    xri = request.headers.get("x-real-ip")
    peer_ip = request.client.host if request.client else ""
    client_ip = ""
    if xff:
        client_ip = xff.split(",")[0].strip()
    elif xri:
        client_ip = xri.strip()
    else:
        client_ip = peer_ip
    return client_ip, peer_ip, xff, xri


def _record(row: dict[str, Any]) -> None:
    try:
        conn = _get_conn()
        with _write_lock:
            conn.execute(
                """
                INSERT INTO scrape_hits (
                    ts_utc, ts_cst, day_cst, method, path, client_ip, peer_ip,
                    x_forwarded_for, x_real_ip, user_agent, referer, query_string,
                    body_snippet, body_bytes, status_code, duration_ms
                ) VALUES (
                    :ts_utc, :ts_cst, :day_cst, :method, :path, :client_ip, :peer_ip,
                    :x_forwarded_for, :x_real_ip, :user_agent, :referer, :query_string,
                    :body_snippet, :body_bytes, :status_code, :duration_ms
                )
                """,
                row,
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        # 探针绝不能影响主流程：落库失败仅告警。
        logger.warning("探针落库失败: %s", e)


class ScrapeZhilianProbeMiddleware(BaseHTTPMiddleware):
    """记录 /api/scrape/zhilian* 的调用来源与调用量。对其它路径零开销放行。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(_WATCH_PREFIX):
            return await call_next(request)

        client_ip, peer_ip, xff, xri = _client_ip_from(request)

        # 读取请求体后必须回填，否则下游 handler 读不到 body。
        body_bytes = b""
        try:
            body_bytes = await request.body()
        except Exception:  # noqa: BLE001
            body_bytes = b""

        async def _receive() -> dict:
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        request._receive = _receive  # type: ignore[attr-defined]

        snippet = body_bytes[:_MAX_BODY_BYTES]
        try:
            body_snippet = snippet.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body_snippet = repr(snippet)

        start = time.perf_counter()
        status_code = 0
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            now = datetime.now(timezone.utc)
            now_cst = now.astimezone(_CST)
            row = {
                "ts_utc": now.isoformat(),
                "ts_cst": now_cst.isoformat(),
                "day_cst": now_cst.strftime("%Y-%m-%d"),
                "method": request.method,
                "path": path,
                "client_ip": client_ip,
                "peer_ip": peer_ip,
                "x_forwarded_for": xff,
                "x_real_ip": xri,
                "user_agent": request.headers.get("user-agent"),
                "referer": request.headers.get("referer"),
                "query_string": request.url.query,
                "body_snippet": body_snippet,
                "body_bytes": len(body_bytes),
                "status_code": status_code,
                "duration_ms": duration_ms,
            }
            _record(row)
            logger.info(
                "SCRAPE_HIT ip=%s peer=%s method=%s path=%s status=%s dur=%sms xff=%s ua=%s",
                client_ip, peer_ip, request.method, path, status_code, duration_ms,
                xff, (request.headers.get("user-agent") or "")[:120],
            )


def get_stats(days: int = 7) -> dict[str, Any]:
    """只读统计：总量、按 IP、按天、按路径、最近命中。供 /api/scrape/_probe/stats 使用。"""
    conn = _get_conn()
    since = (datetime.now(_CST) - timedelta(days=max(1, days))).strftime("%Y-%m-%d")

    def q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    total = q("SELECT count(*) AS total FROM scrape_hits")[0]["total"]
    by_ip = q(
        """
        SELECT client_ip, count(*) AS calls, min(ts_cst) AS first_seen, max(ts_cst) AS last_seen
        FROM scrape_hits WHERE day_cst >= ?
        GROUP BY client_ip ORDER BY calls DESC LIMIT 100
        """,
        (since,),
    )
    by_day = q(
        """
        SELECT day_cst, count(*) AS calls, count(DISTINCT client_ip) AS unique_ips
        FROM scrape_hits WHERE day_cst >= ?
        GROUP BY day_cst ORDER BY day_cst DESC
        """,
        (since,),
    )
    by_path = q(
        """
        SELECT path, count(*) AS calls FROM scrape_hits WHERE day_cst >= ?
        GROUP BY path ORDER BY calls DESC
        """,
        (since,),
    )
    recent = q(
        """
        SELECT ts_cst, client_ip, peer_ip, method, path, status_code, duration_ms, user_agent
        FROM scrape_hits ORDER BY id DESC LIMIT 50
        """
    )
    return {
        "window_days": days,
        "since_day_cst": since,
        "total_all_time": total,
        "by_ip": by_ip,
        "by_day": by_day,
        "by_path": by_path,
        "recent": recent,
    }
