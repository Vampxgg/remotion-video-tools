# -*- coding: utf-8 -*-
"""
压测 /api/compress_video/dify（Dify 专用：multipart 字段名 video，Query 传 crf/preset 等）。

用法示例（在项目根目录、已激活虚拟环境）::

    python ceshi/benchmark_compress_video_dify.py --video D:\\samples\\test.mp4 --concurrency 8 --total 16

    # 预读整文件到内存再并发上传（减轻磁盘争用，大文件慎用）
    python ceshi/benchmark_compress_video_dify.py --video test.mp4 -c 8 -n 16 --preload

    # 结果写入 JSON
    python ceshi/benchmark_compress_video_dify.py --video test.mp4 -c 10 -n 30 --json-out ceshi/bench_result.json

说明：
- 服务端 ``VIDEO_COMPRESS_MAX_CONCURRENT_FFMPEG`` 默认较小（如 2）时，高并发请求会在服务端排队，
  这是预期行为；本脚本统计的是「客户端观测到的端到端耗时」。
- 成功时 Content-Type 为 video/mp4；失败时多为 JSON（``create_standard_response``）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

DEFAULT_BASE = os.environ.get("VIDEO_COMPRESS_BENCH_BASE", "http://127.0.0.1:2906").rstrip("/")
DEFAULT_PATH = "/api/compress_video/dify"


@dataclass
class RequestRecord:
    index: int
    ok: bool
    status_code: int
    elapsed_sec: float
    ttfb_sec: Optional[float]
    response_bytes: int
    content_type: str
    error: Optional[str] = None


@dataclass
class BenchSummary:
    base_url: str
    path: str
    video_path: str
    video_size_bytes: int
    concurrency: int
    total_requests: int
    wall_clock_sec: float
    success: int
    failed: int
    status_histogram: Dict[str, int] = field(default_factory=dict)
    content_type_histogram: Dict[str, int] = field(default_factory=dict)
    latency_sec: Dict[str, Optional[float]] = field(default_factory=dict)
    ttfb_sec: Dict[str, Optional[float]] = field(default_factory=dict)
    throughput_rps: float = 0.0
    success_throughput_rps: float = 0.0
    bytes_total: int = 0
    bytes_per_success_avg: float = 0.0
    query_params: Dict[str, Any] = field(default_factory=dict)
    preload: bool = False
    records_sample: List[Dict[str, Any]] = field(default_factory=list)


def _percentile_nearest_rank(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if p <= 0:
        return sorted_vals[0]
    if p >= 100:
        return sorted_vals[-1]
    k = max(0, min(len(sorted_vals) - 1, math.ceil(p / 100 * len(sorted_vals)) - 1))
    return sorted_vals[k]


def _histogram_status(codes: List[int]) -> Dict[str, int]:
    c = Counter(str(x) for x in codes)
    return dict(sorted(c.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999))


def _histogram_content_type(types: List[str]) -> Dict[str, int]:
    return dict(Counter(types).most_common())


def generate_sample_mp4(dest: Path, duration_sec: float = 2.0) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration_sec}:size=640x360:rate=24",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


async def _one_request(
    client: httpx.AsyncClient,
    index: int,
    url: str,
    video_path: Path,
    params: Dict[str, Any],
    upload_body: Optional[bytes],
) -> RequestRecord:
    ttfb: Optional[float] = None
    nbytes = 0
    ctype = ""
    t0 = time.perf_counter()
    try:
        if upload_body is not None:
            files = {"video": (video_path.name, upload_body, "video/mp4")}
            async with client.stream("POST", url, params=params, files=files) as resp:
                ctype = resp.headers.get("content-type", "") or ""
                ttfb = time.perf_counter() - t0
                async for chunk in resp.aiter_bytes():
                    nbytes += len(chunk)
                ok = 200 <= resp.status_code < 300
                elapsed = time.perf_counter() - t0
                return RequestRecord(
                    index=index,
                    ok=ok,
                    status_code=resp.status_code,
                    elapsed_sec=elapsed,
                    ttfb_sec=ttfb,
                    response_bytes=nbytes,
                    content_type=ctype,
                    error=None if ok else f"HTTP {resp.status_code}",
                )
        with open(video_path, "rb") as fh:
            files = {"video": (video_path.name, fh, "video/mp4")}
            async with client.stream("POST", url, params=params, files=files) as resp:
                ctype = resp.headers.get("content-type", "") or ""
                ttfb = time.perf_counter() - t0
                async for chunk in resp.aiter_bytes():
                    nbytes += len(chunk)
                ok = 200 <= resp.status_code < 300
                elapsed = time.perf_counter() - t0
                return RequestRecord(
                    index=index,
                    ok=ok,
                    status_code=resp.status_code,
                    elapsed_sec=elapsed,
                    ttfb_sec=ttfb,
                    response_bytes=nbytes,
                    content_type=ctype,
                    error=None if ok else f"HTTP {resp.status_code}",
                )
    except httpx.TimeoutException as e:
        return RequestRecord(
            index=index,
            ok=False,
            status_code=0,
            elapsed_sec=time.perf_counter() - t0,
            ttfb_sec=ttfb,
            response_bytes=nbytes,
            content_type=ctype,
            error=f"timeout: {e}",
        )
    except Exception as e:
        return RequestRecord(
            index=index,
            ok=False,
            status_code=0,
            elapsed_sec=time.perf_counter() - t0,
            ttfb_sec=ttfb,
            response_bytes=nbytes,
            content_type=ctype,
            error=repr(e),
        )


def build_summary(
    records: List[RequestRecord],
    wall_sec: float,
    video_path: Path,
    base: str,
    path: str,
    concurrency: int,
    params: Dict[str, Any],
    preload: bool,
    sample_errors: int = 20,
) -> BenchSummary:
    success = sum(1 for r in records if r.ok)
    failed = len(records) - success
    latencies = sorted(r.elapsed_sec for r in records)
    ttfbs = sorted(r.ttfb_sec for r in records if r.ttfb_sec is not None)

    def lat_stats(xs: List[float]) -> Dict[str, Any]:
        if not xs:
            return {"count": 0, "min": None, "max": None, "mean": None, "stdev": None, "p50": None, "p90": None, "p95": None, "p99": None}
        return {
            "count": len(xs),
            "min": xs[0],
            "max": xs[-1],
            "mean": statistics.fmean(xs),
            "stdev": statistics.pstdev(xs) if len(xs) > 1 else 0.0,
            "p50": _percentile_nearest_rank(xs, 50),
            "p90": _percentile_nearest_rank(xs, 90),
            "p95": _percentile_nearest_rank(xs, 95),
            "p99": _percentile_nearest_rank(xs, 99),
        }

    bytes_total = sum(r.response_bytes for r in records)
    summary = BenchSummary(
        base_url=base,
        path=path,
        video_path=str(video_path.resolve()),
        video_size_bytes=video_path.stat().st_size,
        concurrency=concurrency,
        total_requests=len(records),
        wall_clock_sec=wall_sec,
        success=success,
        failed=failed,
        status_histogram=_histogram_status([r.status_code for r in records]),
        content_type_histogram=_histogram_content_type([r.content_type or "(empty)"]),
        latency_sec=lat_stats(latencies),
        ttfb_sec=lat_stats(ttfbs),
        throughput_rps=len(records) / wall_sec if wall_sec > 0 else 0.0,
        success_throughput_rps=success / wall_sec if wall_sec > 0 else 0.0,
        bytes_total=bytes_total,
        bytes_per_success_avg=(bytes_total / success) if success else 0.0,
        query_params=params,
        preload=preload,
        records_sample=[],
    )

    bad = [r for r in records if not r.ok][:sample_errors]
    summary.records_sample = [
        {
            "index": r.index,
            "status_code": r.status_code,
            "elapsed_sec": round(r.elapsed_sec, 4),
            "response_bytes": r.response_bytes,
            "content_type": r.content_type,
            "error": r.error,
        }
        for r in bad
    ]
    return summary


def print_report(summary: BenchSummary) -> None:
    print("\n" + "=" * 72)
    print("compress_video/dify 压测报告")
    print("=" * 72)
    print(f"时间(UTC):     {datetime.now(timezone.utc).isoformat()}")
    print(f"接口:        {summary.base_url}{summary.path}")
    print(f"本地视频:    {summary.video_path}")
    print(f"上传体积:    {summary.video_size_bytes / 1024 / 1024:.2f} MiB")
    print(f"Query:       {summary.query_params}")
    print(f"并发数:      {summary.concurrency}")
    print(f"总请求数:    {summary.total_requests}")
    print(f"墙钟耗时:    {summary.wall_clock_sec:.3f} s")
    print(f"成功 / 失败: {summary.success} / {summary.failed}")
    print(f"总吞吐:      {summary.throughput_rps:.3f} req/s（全部完成）")
    print(f"成功吞吐:    {summary.success_throughput_rps:.3f} req/s")
    print(f"响应字节合计:{summary.bytes_total / 1024 / 1024:.2f} MiB")
    if summary.success:
        print(f"成功均响应:  {summary.bytes_per_success_avg / 1024 / 1024:.2f} MiB/次")
    up_mib = summary.video_size_bytes * summary.total_requests / 1024 / 1024
    if summary.wall_clock_sec > 0:
        print(f"上传吞吐:    {up_mib / summary.wall_clock_sec:.2f} MiB/s（请求×文件/墙钟，粗算）")
    print(f"预读上传:    {summary.preload}")
    print("\n--- HTTP 状态码分布 ---")
    for k, v in summary.status_histogram.items():
        print(f"  {k}: {v}")
    print("\n--- Content-Type 分布 ---")
    for k, v in summary.content_type_histogram.items():
        print(f"  {v}x  {k[:80]}")

    def _print_lat(title: str, d: Dict[str, Any]) -> None:
        print(f"\n--- {title} (秒) ---")
        if d.get("count", 0) == 0:
            print("  (无数据)")
            return
        for key in ("min", "mean", "stdev", "p50", "p90", "p95", "p99", "max"):
            val = d.get(key)
            if val is None:
                continue
            if isinstance(val, float):
                print(f"  {key:6s} {val:.4f}")
            else:
                print(f"  {key:6s} {val}")

    _print_lat("端到端耗时 elapsed", summary.latency_sec)
    _print_lat("首字节 TTFB", summary.ttfb_sec)

    if summary.records_sample:
        print("\n--- 失败样例（最多 20 条）---")
        for row in summary.records_sample:
            print(f"  {row}")


def main() -> int:
    p = argparse.ArgumentParser(description="压测 POST /api/compress_video/dify")
    p.add_argument("--base", default=DEFAULT_BASE, help=f"服务根 URL，默认 {DEFAULT_BASE}")
    p.add_argument("--path", default=DEFAULT_PATH, help="接口路径")
    p.add_argument("--video", type=str, default="", help="待上传的本地视频路径（与 --gen-sample 二选一）")
    p.add_argument("--gen-sample", action="store_true", help="用 ffmpeg 在临时目录生成小视频并压测")
    p.add_argument("--sample-seconds", type=float, default=2.0, help="--gen-sample 时测试片时长（秒）")
    p.add_argument("-c", "--concurrency", type=int, default=4, help="并发数（客户端侧信号量）")
    p.add_argument("-n", "--total", type=int, default=8, help="总请求数")
    p.add_argument("--crf", type=int, default=28)
    p.add_argument("--preset", type=str, default="veryfast")
    p.add_argument("--resolution", type=str, default="")
    p.add_argument("--max-bitrate", type=str, default="")
    p.add_argument("--audio-bitrate", type=str, default="128k")
    p.add_argument("--timeout", type=float, default=3600.0, help="单次请求总超时（秒），视频压缩可能很慢")
    p.add_argument(
        "--preload",
        action="store_true",
        help="启动时把整文件读入内存，各请求复用同一份 bytes（大文件占内存 = 文件×1，非×并发）",
    )
    p.add_argument("--max-connections", type=int, default=256, help="httpx 连接池上限")
    p.add_argument("--json-out", type=str, default="", help="将汇总 JSON 写入该路径")
    args = p.parse_args()

    video_path: Path
    if args.gen_sample:
        tmp = Path(tempfile.gettempdir()) / "bench_compress_video_dify_sample.mp4"
        try:
            generate_sample_mp4(tmp, duration_sec=args.sample_seconds)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print("生成样例视频失败，请安装 ffmpeg 并加入 PATH，或使用 --video 指定文件。", file=sys.stderr)
            print(repr(e), file=sys.stderr)
            return 1
        video_path = tmp
    else:
        if not args.video:
            print("请指定 --video 路径，或使用 --gen-sample。", file=sys.stderr)
            return 1
        video_path = Path(args.video)
        if not video_path.is_file():
            print(f"文件不存在: {video_path}", file=sys.stderr)
            return 1

    params: Dict[str, Any] = {
        "crf": args.crf,
        "preset": args.preset,
        "audio_bitrate": args.audio_bitrate,
    }
    if args.resolution.strip():
        params["resolution"] = args.resolution.strip()
    if args.max_bitrate.strip():
        params["max_bitrate"] = args.max_bitrate.strip()

    upload_body: Optional[bytes] = None
    if args.preload:
        upload_body = video_path.read_bytes()

    async def run_shared_client() -> Tuple[List[RequestRecord], float]:
        url = f"{args.base.rstrip('/')}{args.path}"
        limits = httpx.Limits(
            max_connections=max(32, args.max_connections),
            max_keepalive_connections=max(16, args.max_connections // 2),
        )
        timeout = httpx.Timeout(args.timeout, connect=60.0)
        sem = asyncio.Semaphore(args.concurrency)

        async def one(i: int) -> RequestRecord:
            async with sem:
                return await _one_request(shared_client, i, url, video_path, params, upload_body)

        wall0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as shared_client:
            records = await asyncio.gather(*[one(i) for i in range(args.total)])
        wall = time.perf_counter() - wall0
        return list(records), wall

    records, wall = asyncio.run(run_shared_client())
    summary = build_summary(
        records,
        wall,
        video_path,
        args.base.rstrip("/"),
        args.path,
        args.concurrency,
        params,
        args.preload,
    )
    print_report(summary)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(summary)
        payload["latency_sec"] = dict(summary.latency_sec)
        payload["ttfb_sec"] = dict(summary.ttfb_sec)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n已写入 JSON: {out_path.resolve()}")

    if any(not r.ok for r in records):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
