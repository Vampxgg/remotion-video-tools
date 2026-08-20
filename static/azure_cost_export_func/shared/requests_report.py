# -*- coding: utf-8 -*-
"""核心逻辑：查询 Log Analytics 诊断日志 → 逐条请求明细 → 渲染 NDJSON + Markdown 汇总。

与 Functions 解耦，可独立本地运行：
    python -m shared.requests_report 2026-08-12      # 打印汇总 + NDJSON 预览，不写 blob
    python shared/requests_report.py 2026-08-12

设计要点(严谨性)：
- 数据源是 Cognitive Services 诊断日志(RequestResponse + AzureOpenAIRequestUsage)，
  经诊断设置汇入集中 workspace xpilot-diag-law，落在 AzureDiagnostics 表。
- RequestResponse 不含 prompt/completion 正文(Azure 隐私设计)；能拿到的是逐请求
  元数据：时间戳、部署/模型名、调用方 IP、操作名、状态码、延迟、token 用量。
- 时间统一按 UTC 日界，避免与东八区串日；文件名带 UTC 日期，幂等。
- 诊断日志无法回填：只能采到诊断设置开启时刻之后的请求。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import defaultdict

# 与 calls_report 一致的资源名 → 项目名映射所需后缀
_RESOURCE_SUFFIX = "-resource"

# 北京时区(UTC+8)。数据源(Log Analytics TimeGenerated)按 UTC 存储，但业务口径按
# 北京自然日切分：一份"北京 D 日"文件覆盖 [北京 D 00:00, 北京 D+1 00:00)，
# 换算为 UTC 查询窗口即 [D-1 16:00Z, D 16:00Z)。避免与东八区串日。
CST = dt.timezone(dt.timedelta(hours=8))


def project_name(resource: str) -> str:
    """把 AzureDiagnostics.Resource(大写资源名)映射为简洁项目名。

    Log Analytics 的 Resource 列通常是大写的账户名，如 X-PILOT-2-RESOURCE。
    统一转小写并去掉 -resource 后缀用于展示。
    """
    r = (resource or "").lower()
    return r[: -len(_RESOURCE_SUFFIX)] if r.endswith(_RESOURCE_SUFFIX) else r


def get_config():
    """从环境变量读取配置，缺失给出可用默认值。"""
    return {
        "workspace_id": os.environ.get(
            "COST_LAW_ID", "fb0b738e-52df-468f-8d82-741df02cdce2"
        ),
    }


def day_bounds_utc(date_str: str):
    """把"北京自然日" date_str 转成对应的 UTC datetime 查询区间 [start, end)。

    date_str 是北京日期(如 2026-08-13)，表示北京 [00:00, 次日00:00)。
    北京 = UTC+8，故对应 UTC 窗口为 [date-1 16:00Z, date 16:00Z)。
    返回带 UTC tz 的 datetime，供 KQL/timespan 使用(Log Analytics 底层按 UTC)。
    """
    d_cst = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=CST)
    start_utc = d_cst.astimezone(dt.timezone.utc)
    end_utc = (d_cst + dt.timedelta(days=1)).astimezone(dt.timezone.utc)
    return start_utc, end_utc


# 逐条请求明细：从 AzureDiagnostics 投影出稳定的字段集合。
# - 用 union isfuzzy=true 容忍诊断日志尚未产生任何数据时 AzureDiagnostics 表不存在。
# - properties_s 是诊断日志里的动态列；诊断日志尚无数据时该列不存在，用
#   column_ifexists 回退到空串，避免语义分析报 "Failed to resolve ... properties_s"。
# - token 用量为数组，取首元素(promptTokens[0] / generatedTokens[0])，与官方 schema 一致。
# - requestLength/responseLength 是 HTTP 请求/响应体的字节数(实测 100% 存在)。
#   注意：流式(Streaming)响应的 responseLength 统计的是整个 SSE 流累计字节(含 data:
#   分块框架、重复结构、[DONE])，会显著大于有效内容；非流式则接近纯响应体。汇总时分开看。
_KQL_DETAIL = """
union isfuzzy=true AzureDiagnostics
| where TimeGenerated >= datetime({start}) and TimeGenerated < datetime({end})
| where column_ifexists("ResourceProvider", "") == "MICROSOFT.COGNITIVESERVICES"
| where column_ifexists("Category", "") in ("RequestResponse", "AzureOpenAIRequestUsage")
| extend props = parse_json(column_ifexists("properties_s", ""))
| project
    TimeGenerated,
    Resource = column_ifexists("Resource", ""),
    Category = column_ifexists("Category", ""),
    OperationName = column_ifexists("OperationName", ""),
    DurationMs = column_ifexists("DurationMs", real(null)),
    ResultSignature = column_ifexists("ResultSignature", ""),
    CallerIPAddress = column_ifexists("CallerIPAddress", ""),
    apiName = tostring(props.apiName),
    modelDeploymentName = tostring(props.modelDeploymentName),
    modelName = tostring(props.modelName),
    modelVersion = tostring(props.modelVersion),
    streamType = tostring(props.streamType),
    requestLength = tolong(props.requestLength),
    responseLength = tolong(props.responseLength),
    promptTokens = tolong(props.promptTokens[0]),
    generatedTokens = tolong(props.generatedTokens[0]),
    timeToFirstTokenMs = todouble(props.timeToFirstTokenMs)
| extend totalTokens = coalesce(promptTokens, tolong(0)) + coalesce(generatedTokens, tolong(0))
| order by TimeGenerated asc
"""


def fetch_requests(credential, cfg: dict, date_str: str) -> list[dict]:
    """跑 KQL 拉取当日逐条请求明细，返回 list[dict](每个 dict 是一条请求)。

    credential: 任意 azure-identity 凭据(本地 AzureCliCredential / 云上 ManagedIdentity)。
    """
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus

    start, end = day_bounds_utc(date_str)
    client = LogsQueryClient(credential)
    query = _KQL_DETAIL.format(
        start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    resp = client.query_workspace(
        workspace_id=cfg["workspace_id"],
        query=query,
        timespan=(start, end),
    )

    if resp.status == LogsQueryStatus.FAILURE:
        raise RuntimeError(f"Log Analytics 查询失败: {resp}")

    tables = resp.tables if resp.status == LogsQueryStatus.SUCCESS else resp.partial_data
    rows: list[dict] = []
    for table in tables or []:
        cols = [c for c in table.columns]
        for r in table.rows:
            row = {}
            for i, col in enumerate(cols):
                val = r[i]
                if isinstance(val, dt.datetime):
                    # TimeGenerated 等时间列：原样存 UTC，并额外给出北京时间便于直读。
                    if col == "TimeGenerated":
                        utc_val = val if val.tzinfo else val.replace(tzinfo=dt.timezone.utc)
                        row["TimeBeijing"] = utc_val.astimezone(CST).strftime(
                            "%Y-%m-%dT%H:%M:%S.%f+08:00"
                        )
                    val = val.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                row[col] = val
            rows.append(row)
    return rows


def aggregate(rows: list[dict]) -> dict:
    """把逐条请求聚合成结构化汇总数据(按模型 / 项目)。"""
    model_calls: dict[str, int] = defaultdict(int)
    model_tokens: dict[str, int] = defaultdict(int)
    model_dur: dict[str, float] = defaultdict(float)
    model_reqbytes: dict[str, int] = defaultdict(int)
    model_respbytes: dict[str, int] = defaultdict(int)
    proj_calls: dict[str, int] = defaultdict(int)
    proj_tokens: dict[str, int] = defaultdict(int)
    proj_reqbytes: dict[str, int] = defaultdict(int)
    proj_respbytes: dict[str, int] = defaultdict(int)
    status_calls: dict[str, int] = defaultdict(int)
    # 字节量按 streamType 分桶：流式 responseLength 含 SSE 框架会虚高，分开统计避免误导。
    stream_reqbytes: dict[str, int] = defaultdict(int)
    stream_respbytes: dict[str, int] = defaultdict(int)
    stream_calls: dict[str, int] = defaultdict(int)

    for row in rows:
        # 用量事件与请求事件可能重复覆盖同一次请求：以 RequestResponse 计"次数"，
        # token 以任一事件里非空的 totalTokens 计。这里按行如实累加，交叉核对留给校验。
        model = row.get("modelDeploymentName") or row.get("modelName") or "(unknown)"
        proj = project_name(row.get("Resource", ""))
        cat = row.get("Category", "")

        if cat == "RequestResponse":
            model_calls[model] += 1
            proj_calls[proj] += 1
            status_calls[str(row.get("ResultSignature") or "-")] += 1
            if row.get("DurationMs"):
                model_dur[model] += float(row["DurationMs"])

            reqb = int(row.get("requestLength") or 0)
            respb = int(row.get("responseLength") or 0)
            model_reqbytes[model] += reqb
            model_respbytes[model] += respb
            proj_reqbytes[proj] += reqb
            proj_respbytes[proj] += respb
            st = row.get("streamType") or "(unknown)"
            stream_calls[st] += 1
            stream_reqbytes[st] += reqb
            stream_respbytes[st] += respb

        tt = row.get("totalTokens") or 0
        if tt:
            model_tokens[model] += int(tt)
            proj_tokens[proj] += int(tt)

    grand_calls = sum(proj_calls.values())
    return {
        "grand_calls": grand_calls,
        "grand_tokens": sum(proj_tokens.values()),
        "grand_reqbytes": sum(proj_reqbytes.values()),
        "grand_respbytes": sum(proj_respbytes.values()),
        "model_calls": dict(model_calls),
        "model_tokens": dict(model_tokens),
        "model_dur": dict(model_dur),
        "model_reqbytes": dict(model_reqbytes),
        "model_respbytes": dict(model_respbytes),
        "proj_calls": dict(proj_calls),
        "proj_tokens": dict(proj_tokens),
        "proj_reqbytes": dict(proj_reqbytes),
        "proj_respbytes": dict(proj_respbytes),
        "status_calls": dict(status_calls),
        "stream_calls": dict(stream_calls),
        "stream_reqbytes": dict(stream_reqbytes),
        "stream_respbytes": dict(stream_respbytes),
    }


def to_ndjson(rows: list[dict]) -> bytes:
    """逐条请求明细 → NDJSON(每行一条 JSON)，即"单次完整明细"。"""
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def _w(s: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    gap = width - _w(s)
    if gap <= 0:
        return s
    return (" " * gap + s) if right else (s + " " * gap)


def _hbytes(n: int) -> str:
    """字节数 → 人类可读(KB/MB/GB)。用于汇总展示，NDJSON 里仍是原始字节。"""
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def to_markdown(date_str: str, agg: dict, row_count: int) -> bytes:
    grand = agg["grand_calls"]
    lines: list[str] = []
    W = 96
    lines.append("=" * W)
    lines.append(f"{date_str} 单请求明细汇总 (北京时间自然日 · 数据源: 诊断日志 RequestResponse+Usage)")
    lines.append(f"当日请求条数: {grand:,} 次   总 token: {agg['grand_tokens']:,}")
    lines.append(
        f"请求体总量: {_hbytes(agg['grand_reqbytes'])}   "
        f"响应体总量: {_hbytes(agg['grand_respbytes'])} (流式含 SSE 框架, 偏大)"
    )
    lines.append(f"原始日志行数(含用量事件): {row_count:,}")
    lines.append("=" * W)

    lines.append("")
    lines.append("【按模型 · 请求次数 / token / 平均延迟 / 请求·响应字节量】")
    lines.append(
        f"{'#':>2} {_pad('模型', 26)} {_pad('次数', 8, True)} "
        f"{_pad('token', 10, True)} {_pad('平均延迟ms', 10, True)} "
        f"{_pad('请求量', 10, True)} {_pad('响应量', 10, True)}"
    )
    lines.append("-" * W)
    for i, (m, c) in enumerate(
        sorted(agg["model_calls"].items(), key=lambda x: -x[1]), 1
    ):
        tok = agg["model_tokens"].get(m, 0)
        avg = agg["model_dur"].get(m, 0) / c if c else 0
        reqb = agg["model_reqbytes"].get(m, 0)
        respb = agg["model_respbytes"].get(m, 0)
        lines.append(
            f"{i:>2} {_pad(m, 26)} {_pad(f'{c:,}', 8, True)} "
            f"{_pad(f'{tok:,}', 10, True)} {_pad(f'{avg:.0f}', 10, True)} "
            f"{_pad(_hbytes(reqb), 10, True)} {_pad(_hbytes(respb), 10, True)}"
        )
    lines.append("-" * W)
    grand_tok = agg["grand_tokens"]
    lines.append(
        f"   {_pad('合计', 26)} {_pad(f'{grand:,}', 8, True)} "
        f"{_pad(f'{grand_tok:,}', 10, True)} {_pad('', 10, True)} "
        f"{_pad(_hbytes(agg['grand_reqbytes']), 10, True)} "
        f"{_pad(_hbytes(agg['grand_respbytes']), 10, True)}"
    )

    lines.append("")
    lines.append("=" * W)
    lines.append("【按项目(资源账户) · 请求次数 / token / 请求·响应字节量 = 谁在请求】")
    lines.append("=" * W)
    lines.append(
        f"{_pad('项目/资源', 24)} {_pad('次数', 8, True)} {_pad('token', 10, True)} "
        f"{_pad('请求量', 10, True)} {_pad('响应量', 10, True)}"
    )
    lines.append("-" * W)
    for proj, c in sorted(agg["proj_calls"].items(), key=lambda x: -x[1]):
        tok = agg["proj_tokens"].get(proj, 0)
        reqb = agg["proj_reqbytes"].get(proj, 0)
        respb = agg["proj_respbytes"].get(proj, 0)
        lines.append(
            f"{_pad(proj, 24)} {_pad(f'{c:,}', 8, True)} {_pad(f'{tok:,}', 10, True)} "
            f"{_pad(_hbytes(reqb), 10, True)} {_pad(_hbytes(respb), 10, True)}"
        )

    if agg["stream_calls"]:
        lines.append("")
        lines.append("=" * W)
        lines.append("【按流式类型 · 字节量】(流式 responseLength 含 SSE 分块框架, 不等于纯内容大小)")
        lines.append("=" * W)
        lines.append(
            f"{_pad('streamType', 16)} {_pad('次数', 8, True)} "
            f"{_pad('请求量', 12, True)} {_pad('响应量', 12, True)}"
        )
        lines.append("-" * W)
        for st, c in sorted(agg["stream_calls"].items(), key=lambda x: -x[1]):
            reqb = agg["stream_reqbytes"].get(st, 0)
            respb = agg["stream_respbytes"].get(st, 0)
            lines.append(
                f"{_pad(st, 16)} {_pad(f'{c:,}', 8, True)} "
                f"{_pad(_hbytes(reqb), 12, True)} {_pad(_hbytes(respb), 12, True)}"
            )

    if agg["status_calls"]:
        lines.append("")
        lines.append("【按状态码 · 请求次数】")
        for st, c in sorted(agg["status_calls"].items(), key=lambda x: -x[1]):
            lines.append(f"  {st}: {c:,}")

    return ("\n".join(lines) + "\n").encode("utf-8")


def build_reports(credential, cfg: dict, date_str: str):
    """返回 (ndjson_bytes, md_bytes, agg, row_count)。"""
    rows = fetch_requests(credential, cfg, date_str)
    agg = aggregate(rows)
    return to_ndjson(rows), to_markdown(date_str, agg, len(rows)), agg, len(rows)


def _yesterday_utc() -> str:
    """默认导出"昨天"(北京时间)。函数名保留兼容，实际返回北京昨天日期。"""
    return (dt.datetime.now(CST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    date_arg = sys.argv[1] if len(sys.argv) > 1 else _yesterday_utc()
    from azure.identity import AzureCliCredential

    cfg = get_config()
    _ndjson, _md, _agg, _n = build_reports(AzureCliCredential(), cfg, date_arg)
    print(_md.decode("utf-8"))
    print(f"\n--- NDJSON 预览(共 {_n} 行, 前 3 行) ---")
    for _line in _ndjson.decode("utf-8").splitlines()[:3]:
        print(_line)
