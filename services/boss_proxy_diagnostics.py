# -*- coding: utf-8 -*-
"""BOSS 代理池只读诊断工具。"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx


TcpChecker = Callable[[str, int, float], bool]
EgressChecker = Callable[[str, float], Optional[str]]


def load_json_list_file(path: str) -> List[Dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{path} 必须是 JSON 对象数组")
    return value


def diagnose_proxy_pool(
    *,
    proxies: List[Dict[str, Any]],
    workers: List[Dict[str, Any]],
    tcp_checker: Optional[TcpChecker] = None,
    egress_checker: Optional[EgressChecker] = None,
    timeout: float = 3.0,
) -> Dict[str, Any]:
    tcp_checker = tcp_checker or _tcp_check
    proxy_ids = {str(item.get("proxy_id") or item.get("id") or "") for item in proxies}
    enabled_proxy_count = sum(1 for item in proxies if bool(item.get("enabled", True)))
    errors: List[str] = []
    proxy_results: List[Dict[str, Any]] = []

    for item in proxies:
        proxy_id = str(item.get("proxy_id") or item.get("id") or "")
        local_proxy_url = str(item.get("local_proxy_url") or item.get("proxy_url") or "")
        parsed = urlsplit(local_proxy_url)
        tcp_ok = False
        tcp_error = None
        if parsed.hostname and parsed.port:
            try:
                tcp_ok = bool(tcp_checker(parsed.hostname, parsed.port, timeout))
            except Exception as exc:
                tcp_error = type(exc).__name__
        else:
            tcp_error = "missing_host_or_port"

        egress_ip = None
        egress_error = None
        if egress_checker is not None and tcp_ok:
            try:
                egress_ip = egress_checker(local_proxy_url, timeout)
            except Exception as exc:
                egress_error = type(exc).__name__

        proxy_results.append({
            "proxy_id": proxy_id,
            "enabled": bool(item.get("enabled", True)),
            "kind": item.get("kind") or "local_http",
            "group": item.get("group"),
            "upstream_label": item.get("upstream_label"),
            "local_proxy_url_masked": mask_proxy_url(local_proxy_url),
            "tcp_ok": tcp_ok,
            "tcp_error": tcp_error,
            "egress_ip": egress_ip,
            "egress_error": egress_error,
        })

    worker_results: List[Dict[str, Any]] = []
    for item in workers:
        worker_id = str(item.get("worker_id") or item.get("id") or "")
        proxy_id = item.get("proxy_id")
        if proxy_id and str(proxy_id) not in proxy_ids:
            errors.append(f"worker={worker_id} 引用了不存在的 proxy_id={proxy_id}")
        worker_results.append({
            "worker_id": worker_id,
            "browser_host_port": item.get("browser_host_port") or item.get("host_port"),
            "profile_id": item.get("profile_id") or item.get("account_id") or worker_id,
            "proxy_id": proxy_id,
        })

    if len(workers) > enabled_proxy_count:
        errors.append(
            f"活跃 worker 数 {len(workers)} 大于 enabled 代理数 {enabled_proxy_count}"
        )

    return {
        "ok": not errors and all(item["tcp_ok"] for item in proxy_results if item["enabled"]),
        "errors": errors,
        "summary": {
            "workers": len(workers),
            "proxies": len(proxies),
            "enabled_proxies": enabled_proxy_count,
        },
        "proxies": proxy_results,
        "workers": worker_results,
    }


def default_egress_checker(healthcheck_url: str) -> EgressChecker:
    def _check(proxy_url: str, timeout: float) -> Optional[str]:
        with httpx.Client(timeout=timeout, trust_env=False, proxy=proxy_url) as client:
            resp = client.get(healthcheck_url)
            resp.raise_for_status()
        try:
            body = resp.json()
        except Exception:
            return resp.text.strip()[:200]
        return body.get("ip") or body.get("origin") or json.dumps(body, ensure_ascii=False)

    return _check


def _tcp_check(host: str, port: int, timeout: float) -> bool:
    with socket.create_connection((host, port), timeout=timeout):
        return True


def mask_proxy_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.username:
        return url
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((
        parsed.scheme,
        f"{parsed.username}:***@{hostname}",
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))
