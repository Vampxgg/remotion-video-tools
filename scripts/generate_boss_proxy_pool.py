# -*- coding: utf-8 -*-
"""Generate BOSS dedicated mihomo listeners and proxy-pool mapping.

Input can be either a full Clash/Mihomo YAML containing ``proxies`` or a YAML
list of proxy objects. Secrets are copied into the mihomo config only; the
BOSS application consumes local listener ports from ``boss-proxy-pool.json``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import yaml


DEFAULT_SOURCE = Path("static/proxy/mihomo-boss/config.yaml")
DEFAULT_MIHOMO_CONFIG = Path("static/proxy/mihomo-boss/config.yaml")
DEFAULT_PROXY_POOL = Path("secrets/boss-proxy-pool.json")
DEFAULT_START_PORT = 18081


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))


def _extract_proxies(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        proxies = value
    elif isinstance(value, dict):
        proxies = value.get("proxies") or []
    else:
        raise ValueError("source YAML must be a list or an object with a proxies field")
    if not all(isinstance(item, dict) for item in proxies):
        raise ValueError("every proxy entry must be an object")
    return [dict(item) for item in proxies]


def _normalize_proxy(proxy: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(proxy)
    if str(normalized.get("type", "")).lower() == "vless":
        normalized.setdefault("encryption", "none")
    return normalized


def _proxy_id(index: int) -> str:
    return f"boss-proxy-{index:03d}"


def generate(
    *,
    source_yaml: Path,
    mihomo_config: Path,
    proxy_pool: Path,
    start_port: int,
    limit: int | None,
) -> Dict[str, Any]:
    proxies = [_normalize_proxy(item) for item in _extract_proxies(_load_yaml(source_yaml))]
    if limit is not None:
        proxies = proxies[:limit]
    if not proxies:
        raise ValueError("source YAML contains no proxies")

    listeners: List[Dict[str, Any]] = []
    pool: List[Dict[str, Any]] = []
    for idx, proxy in enumerate(proxies, start=1):
        name = proxy.get("name")
        if not name:
            raise ValueError(f"proxy #{idx} is missing name")
        port = start_port + idx - 1
        proxy_id = _proxy_id(idx)
        listeners.append(
            {
                "name": proxy_id,
                "type": "mixed",
                "listen": "127.0.0.1",
                "port": port,
                "proxy": name,
            }
        )
        pool.append(
            {
                "proxy_id": proxy_id,
                "enabled": True,
                "kind": "local_http",
                "group": "mihomo-boss",
                "local_proxy_url": f"http://127.0.0.1:{port}",
                "chrome_proxy_server": f"http=127.0.0.1:{port};https=127.0.0.1:{port}",
                "upstream_label": name,
            }
        )

    mihomo = {
        "log-level": "info",
        "allow-lan": False,
        "mode": "rule",
        "ipv6": False,
        "external-controller": "127.0.0.1:19090",
        "listeners": listeners,
        "proxies": proxies,
        "rules": ["MATCH,DIRECT"],
    }

    mihomo_config.parent.mkdir(parents=True, exist_ok=True)
    proxy_pool.parent.mkdir(parents=True, exist_ok=True)
    mihomo_config.write_text(
        yaml.safe_dump(mihomo, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    proxy_pool.write_text(
        json.dumps(pool, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "proxies": len(proxies),
        "first_port": start_port,
        "last_port": start_port + len(proxies) - 1,
        "mihomo_config": str(mihomo_config),
        "proxy_pool": str(proxy_pool),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-yaml", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--mihomo-config", type=Path, default=DEFAULT_MIHOMO_CONFIG)
    parser.add_argument("--proxy-pool", type=Path, default=DEFAULT_PROXY_POOL)
    parser.add_argument("--start-port", type=int, default=DEFAULT_START_PORT)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    result = generate(
        source_yaml=args.source_yaml,
        mihomo_config=args.mihomo_config,
        proxy_pool=args.proxy_pool,
        start_port=args.start_port,
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
