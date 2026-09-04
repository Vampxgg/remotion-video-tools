# -*- coding: utf-8 -*-
"""验证 BOSS 单 worker 换代理自愈链路。

默认只做配置级/租约级验证，不启动或停止 Chrome。传 ``--live-restart`` 后会真实
重启目标 worker Chrome，并检查 DevTools 端口是否恢复。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.boss_proxy_pool import BossProxyPool  # noqa: E402
from services.boss_worker_runtime import BossWorkerRuntimeManager, WorkerRuntimeConfig  # noqa: E402


def _load_json_list(path: Path) -> List[Dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{path} 必须是 JSON 对象数组")
    return value


def _worker_by_id(workers: List[Dict[str, Any]], worker_id: str) -> Dict[str, Any]:
    for worker in workers:
        if str(worker.get("worker_id") or worker.get("id") or "") == worker_id:
            return worker
    raise ValueError(f"worker_id not found: {worker_id}")


def _lease_initial_workers(pool: BossProxyPool, workers: List[Dict[str, Any]]) -> None:
    for worker in workers:
        worker_id = str(worker.get("worker_id") or worker.get("id") or "")
        if not worker_id:
            raise ValueError("workers file contains item without worker_id")
        pool.lease_for_worker(
            worker_id,
            requested_proxy_id=worker.get("proxy_id"),
            requested_proxy_url=worker.get("proxy_url"),
        )


def verify_recovery(
    *,
    workers_file: Path,
    proxy_file: Path,
    worker_id: str,
    replacement_proxy_id: Optional[str] = None,
    live_restart: bool = False,
    chrome_path: Optional[str] = None,
    profile_root: Path | str = PROJECT_ROOT / "runtime" / "chrome-profiles",
    state_root: Path | str = PROJECT_ROOT / "runtime" / "boss-workers",
    cooldown_seconds: int = 120,
) -> Dict[str, Any]:
    workers = _load_json_list(workers_file)
    proxies = _load_json_list(proxy_file)
    target_worker = _worker_by_id(workers, worker_id)

    pool = BossProxyPool(proxies)
    _lease_initial_workers(pool, workers)
    old_proxy_id = target_worker.get("proxy_id")
    lease = pool.reassign_worker(
        worker_id,
        bad_proxy_id=old_proxy_id,
        requested_proxy_id=replacement_proxy_id,
        reason="verification_injected_access_limited",
        seconds=cooldown_seconds,
    )

    runtime_result: Dict[str, Any] = {"mode": "dry_run"}
    if live_restart:
        config = WorkerRuntimeConfig(
            worker_id=worker_id,
            browser_host_port=str(target_worker.get("browser_host_port") or target_worker.get("host_port") or ""),
            profile_id=str(target_worker.get("profile_id") or target_worker.get("account_id") or worker_id),
        )
        runtime = BossWorkerRuntimeManager(
            chrome_path=chrome_path,
            profile_root=profile_root,
            state_root=state_root,
        )
        runtime_result = {
            "mode": "live_restart",
            **runtime.restart_worker(
                config,
                proxy_id=lease.proxy_id,
                chrome_proxy_server=lease.chrome_proxy_server,
            ),
        }

    return {
        "ok": True,
        "summary": {
            "workers": len(workers),
            "proxies": len(proxies),
            "live_restart": live_restart,
        },
        "target": {
            "worker_id": worker_id,
            "old_proxy_id": old_proxy_id,
            "new_proxy_id": lease.proxy_id,
            "new_local_proxy_url": lease.local_proxy_url,
            "new_chrome_proxy_server": lease.chrome_proxy_server,
        },
        "runtime": runtime_result,
        "proxy_status": pool.status(),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers-file", type=Path, default=PROJECT_ROOT / "secrets" / "boss-workers.json")
    parser.add_argument("--proxy-file", type=Path, default=PROJECT_ROOT / "secrets" / "boss-proxy-pool.json")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--replacement-proxy-id", default=None)
    parser.add_argument("--live-restart", action="store_true")
    parser.add_argument("--chrome-path", default=None)
    parser.add_argument("--profile-root", type=Path, default=PROJECT_ROOT / "runtime" / "chrome-profiles")
    parser.add_argument("--state-root", type=Path, default=PROJECT_ROOT / "runtime" / "boss-workers")
    parser.add_argument("--cooldown-seconds", type=int, default=120)
    args = parser.parse_args()

    result = verify_recovery(
        workers_file=args.workers_file,
        proxy_file=args.proxy_file,
        worker_id=args.worker_id,
        replacement_proxy_id=args.replacement_proxy_id,
        live_restart=args.live_restart,
        chrome_path=args.chrome_path,
        profile_root=args.profile_root,
        state_root=args.state_root,
        cooldown_seconds=args.cooldown_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
