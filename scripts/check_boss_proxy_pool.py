# -*- coding: utf-8 -*-
"""检查 BOSS 代理池端口、出口与 worker 绑定。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.boss_proxy_diagnostics import (  # noqa: E402
    default_egress_checker,
    diagnose_proxy_pool,
    load_json_list_file,
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Check BOSS proxy pool and worker mapping.")
    parser.add_argument("--proxy-file", required=True, help="Path to boss-proxy-pool.json")
    parser.add_argument("--workers-file", required=True, help="Path to boss-workers.json")
    parser.add_argument("--healthcheck-url", default="https://httpbin.org/ip")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--skip-egress", action="store_true", help="Only check local TCP ports")
    args = parser.parse_args()

    proxies = load_json_list_file(args.proxy_file)
    workers = load_json_list_file(args.workers_file)
    egress_checker = None if args.skip_egress else default_egress_checker(args.healthcheck_url)
    result = diagnose_proxy_pool(
        proxies=proxies,
        workers=workers,
        egress_checker=egress_checker,
        timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
