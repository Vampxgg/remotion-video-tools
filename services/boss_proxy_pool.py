# -*- coding: utf-8 -*-
"""BOSS worker 本地代理端口池。

本模块只管理 BOSS worker 可消费的本地 HTTP/SOCKS 代理端口，不解析 VLESS/
Clash 节点协议。上游节点应先由 Clash/mihomo 或认证转发器暴露成本地端口，
再由 worker 稳定绑定使用。
"""

from __future__ import annotations

import time
import threading
import random
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class ProxyLease:
    """某个 worker 对一个本地代理出口的稳定租约。"""

    proxy_id: str
    local_proxy_url: str
    chrome_proxy_server: Optional[str] = None
    upstream_label: Optional[str] = None


@dataclass
class _ProxyEntry:
    proxy_id: str
    local_proxy_url: str
    chrome_proxy_server: Optional[str] = None
    upstream_label: Optional[str] = None
    group: Optional[str] = None
    kind: str = "local_http"
    enabled: bool = True
    lease_worker_id: Optional[str] = None
    last_lease_worker_id: Optional[str] = None
    replaced_by: Optional[str] = None
    cooldown_until: Optional[float] = None
    last_error: Optional[str] = None
    unhealthy: bool = False
    health_checked: bool = False

    def to_lease(self) -> ProxyLease:
        return ProxyLease(
            proxy_id=self.proxy_id,
            local_proxy_url=self.local_proxy_url,
            chrome_proxy_server=self.chrome_proxy_server,
            upstream_label=self.upstream_label,
        )


class BossProxyPool:
    """为 BOSS worker 分配稳定本地代理端口，并记录代理级冷却状态。"""

    _CONSUMABLE_KINDS = {"local_http", "direct_http", "direct_socks", "auth_http"}
    _ALLOWED_SCHEMES = {"http", "https", "socks5", "socks5h"}

    def __init__(
        self,
        proxies: List[Dict[str, Any]],
        *,
        default_cooldown_seconds: int = 7200,
        health_checker: Optional[Callable[[str], bool]] = None,
        selection_strategy: str = "ordered",
        recent_avoid_count: int = 0,
        random_choice: Optional[Callable[[List[_ProxyEntry]], _ProxyEntry]] = None,
    ) -> None:
        self._default_cooldown_seconds = max(1, int(default_cooldown_seconds))
        self._health_checker = health_checker
        strategy = str(selection_strategy or "ordered").strip().lower()
        if strategy not in {"ordered", "random", "round_robin"}:
            raise ValueError(f"BOSS_ZHIPIN_PROXY_SELECTION_STRATEGY 不支持: {selection_strategy}")
        self._selection_strategy = strategy
        self._recent_avoid_count = max(0, int(recent_avoid_count or 0))
        self._recent_proxy_ids: List[str] = []
        self._random_choice = random_choice or random.choice
        self._round_robin_cursor = 0
        self._entries: Dict[str, _ProxyEntry] = {}
        self._worker_leases: Dict[str, str] = {}
        self._lock = threading.RLock()
        seen_urls: set[str] = set()

        for index, item in enumerate(proxies or [], start=1):
            if not isinstance(item, dict):
                raise ValueError("BOSS_ZHIPIN_PROXY_POOL 的每个元素必须是对象")

            proxy_id = str(item.get("proxy_id") or item.get("id") or f"boss-proxy-{index}")
            enabled = bool(item.get("enabled", True))
            kind = str(item.get("kind") or "local_http").strip()
            local_proxy_url = str(item.get("local_proxy_url") or item.get("proxy_url") or "").strip()
            if not local_proxy_url:
                raise ValueError(f"BOSS_ZHIPIN_PROXY_POOL 缺少 local_proxy_url: {proxy_id}")
            self._validate_kind_and_url(proxy_id, kind=kind, local_proxy_url=local_proxy_url)
            if proxy_id in self._entries:
                raise ValueError(f"BOSS_ZHIPIN_PROXY_POOL proxy_id 重复: {proxy_id}")
            if local_proxy_url in seen_urls:
                raise ValueError(
                    f"BOSS_ZHIPIN_PROXY_POOL local_proxy_url 重复: {self._mask_url(local_proxy_url)}"
                )
            seen_urls.add(local_proxy_url)

            entry = _ProxyEntry(
                proxy_id=proxy_id,
                local_proxy_url=local_proxy_url,
                chrome_proxy_server=item.get("chrome_proxy_server"),
                upstream_label=item.get("upstream_label"),
                group=item.get("group"),
                kind=kind,
                enabled=enabled,
            )
            self._entries[proxy_id] = entry

    def lease_for_worker(
        self,
        worker_id: str,
        *,
        requested_proxy_id: Optional[str] = None,
        requested_proxy_url: Optional[str] = None,
    ) -> ProxyLease:
        """给 worker 返回稳定代理租约。

        ``requested_proxy_url`` 用于兼容旧式 worker 静态代理配置；它不进入池内复用。
        """
        worker_id = str(worker_id)
        with self._lock:
            if requested_proxy_url and not requested_proxy_id:
                return ProxyLease(
                    proxy_id=f"{worker_id}-static",
                    local_proxy_url=str(requested_proxy_url),
                )

            existing_proxy_id = self._worker_leases.get(worker_id)
            if existing_proxy_id:
                entry = self._entries[existing_proxy_id]
                self._assert_entry_available(entry, requested_by=worker_id, check_health=True)
                return entry.to_lease()

            if requested_proxy_id:
                entry = self._entries.get(str(requested_proxy_id))
                if entry is None:
                    raise ValueError(f"BOSS_ZHIPIN_PROXY_POOL 未找到 proxy_id: {requested_proxy_id}")
                self._assert_entry_available(entry, requested_by=worker_id, check_health=True)
                self._bind(worker_id, entry)
                return entry.to_lease()

            entry = self._next_available_entry()
            if entry is not None:
                self._bind(worker_id, entry)
                return entry.to_lease()

        raise ValueError("BOSS_ZHIPIN_PROXY_POOL 可用代理不足，不能为更多 worker 分配稳定出口")

    def mark_cooldown(
        self,
        proxy_id: str,
        *,
        reason: str,
        seconds: Optional[int] = None,
    ) -> None:
        with self._lock:
            entry = self._entries.get(str(proxy_id))
            if entry is None:
                return
            entry.cooldown_until = time.monotonic() + max(
                1, int(seconds or self._default_cooldown_seconds)
            )
            entry.last_error = reason

    def release_worker(self, worker_id: str) -> Optional[ProxyLease]:
        """释放 worker 当前租约，返回被释放的代理。

        释放只解除 worker 与 proxy 的绑定，不改变 proxy 的健康/冷却状态。
        """
        worker_id = str(worker_id)
        with self._lock:
            proxy_id = self._worker_leases.pop(worker_id, None)
            if not proxy_id:
                return None
            entry = self._entries.get(proxy_id)
            if entry is None:
                return None
            if entry.lease_worker_id == worker_id:
                entry.lease_worker_id = None
                entry.last_lease_worker_id = worker_id
            return entry.to_lease()

    def reassign_worker(
        self,
        worker_id: str,
        *,
        bad_proxy_id: Optional[str] = None,
        reason: str,
        seconds: Optional[int] = None,
    ) -> ProxyLease:
        """隔离旧代理，并为同一 worker 分配一个新的健康空闲代理。"""
        worker_id = str(worker_id)
        with self._lock:
            previous_proxy_id = bad_proxy_id or self._worker_leases.get(worker_id)
            replacement = self._next_available_entry(exclude_proxy_id=previous_proxy_id)
            if replacement is None:
                if previous_proxy_id:
                    self.mark_cooldown(str(previous_proxy_id), reason=reason, seconds=seconds)
                raise ValueError("BOSS_ZHIPIN_PROXY_POOL 可用代理不足，不能为 worker 重新分配出口")

            if previous_proxy_id:
                previous = self._entries.get(str(previous_proxy_id))
                if previous is not None and previous.lease_worker_id == worker_id:
                    previous.lease_worker_id = None
                    previous.last_lease_worker_id = worker_id
                self._worker_leases.pop(worker_id, None)
                self.mark_cooldown(str(previous_proxy_id), reason=reason, seconds=seconds)

            self._bind(worker_id, replacement)
            lease = replacement.to_lease()
            if previous_proxy_id and str(previous_proxy_id) in self._entries:
                self._entries[str(previous_proxy_id)].replaced_by = lease.proxy_id
            return lease

    def status(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            now = time.monotonic()
            statuses: Dict[str, Dict[str, Any]] = {}
            for proxy_id, entry in self._entries.items():
                cooling = bool(entry.cooldown_until and entry.cooldown_until > now)
                if entry.unhealthy:
                    state = "unhealthy"
                elif not entry.enabled:
                    state = "disabled"
                elif cooling:
                    state = "cooldown"
                else:
                    state = "leased" if entry.lease_worker_id else "available"
                statuses[proxy_id] = {
                    "state": state,
                    "lease_worker_id": entry.lease_worker_id,
                    "last_lease_worker_id": entry.last_lease_worker_id,
                    "replaced_by": entry.replaced_by,
                    "local_proxy_url_masked": self._mask_url(entry.local_proxy_url),
                    "chrome_proxy_server": entry.chrome_proxy_server,
                    "upstream_label": entry.upstream_label,
                    "group": entry.group,
                    "kind": entry.kind,
                    "enabled": entry.enabled,
                    "cooldown_remaining_seconds": (
                        int(entry.cooldown_until - now)
                        if cooling and entry.cooldown_until
                        else 0
                    ),
                    "last_error": entry.last_error,
                }
            return statuses

    def _bind(self, worker_id: str, entry: _ProxyEntry) -> None:
        entry.lease_worker_id = worker_id
        self._worker_leases[worker_id] = entry.proxy_id
        if self._recent_avoid_count:
            self._recent_proxy_ids = [
                proxy_id for proxy_id in self._recent_proxy_ids if proxy_id != entry.proxy_id
            ]
            self._recent_proxy_ids.append(entry.proxy_id)
            self._recent_proxy_ids = self._recent_proxy_ids[-self._recent_avoid_count :]

    def _assert_entry_available(
        self,
        entry: _ProxyEntry,
        *,
        requested_by: str,
        check_health: bool = False,
    ) -> None:
        if not entry.enabled:
            raise ValueError(f"BOSS_ZHIPIN_PROXY_POOL proxy_id={entry.proxy_id} 已禁用")
        if check_health:
            self._refresh_health(entry)
        if entry.unhealthy:
            raise ValueError(f"BOSS_ZHIPIN_PROXY_POOL proxy_id={entry.proxy_id} 不健康")
        if entry.cooldown_until and entry.cooldown_until > time.monotonic():
            raise ValueError(f"BOSS_ZHIPIN_PROXY_POOL proxy_id={entry.proxy_id} 正在冷却")
        if entry.lease_worker_id and entry.lease_worker_id != requested_by:
            raise ValueError(
                "BOSS_ZHIPIN_PROXY_POOL proxy_id="
                f"{entry.proxy_id} 已绑定 worker={entry.lease_worker_id}"
            )

    def _is_entry_available(self, entry: _ProxyEntry, *, check_health: bool = False) -> bool:
        try:
            self._assert_entry_available(entry, requested_by="", check_health=check_health)
        except ValueError:
            return False
        return True

    def _next_available_entry(self, *, exclude_proxy_id: Optional[str] = None) -> Optional[_ProxyEntry]:
        excluded = str(exclude_proxy_id) if exclude_proxy_id else None
        entries = list(self._entries.values())
        candidates = [
            entry
            for entry in entries
            if entry.proxy_id != excluded
            and entry.lease_worker_id is None
            and entry.enabled
            and not entry.unhealthy
            and not (entry.cooldown_until and entry.cooldown_until > time.monotonic())
        ]
        if not candidates:
            return None
        if self._recent_avoid_count and len(candidates) > 1:
            recent_ids = set(self._recent_proxy_ids)
            filtered = [entry for entry in candidates if entry.proxy_id not in recent_ids]
            if filtered:
                candidates = filtered
        if self._selection_strategy == "random":
            while candidates:
                entry = self._random_choice(candidates)
                candidates.remove(entry)
                if self._is_entry_available(entry, check_health=True):
                    return entry
            return None
        if self._selection_strategy == "round_robin":
            for offset in range(len(entries)):
                index = (self._round_robin_cursor + offset) % len(entries)
                entry = entries[index]
                if entry in candidates and self._is_entry_available(entry, check_health=True):
                    self._round_robin_cursor = (index + 1) % len(entries)
                    return entry
            return None
        for entry in candidates:
            if self._is_entry_available(entry, check_health=True):
                return entry
        return None

    def _refresh_health(self, entry: _ProxyEntry) -> None:
        if entry.health_checked or self._health_checker is None:
            return
        entry.health_checked = True
        try:
            entry.unhealthy = not bool(self._health_checker(entry.local_proxy_url))
            if not entry.unhealthy and entry.last_error and entry.last_error.startswith("health_check_failed"):
                entry.last_error = None
        except Exception as exc:
            entry.unhealthy = True
            entry.last_error = f"health_check_failed: {type(exc).__name__}"

    @staticmethod
    def _mask_url(url: str) -> str:
        parsed = urlsplit(url)
        if not parsed.username:
            return url
        hostname = parsed.hostname or ""
        if parsed.port:
            hostname = f"{hostname}:{parsed.port}"
        masked_netloc = f"{parsed.username}:***@{hostname}"
        return urlunsplit((
            parsed.scheme,
            masked_netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        ))

    @classmethod
    def _validate_kind_and_url(cls, proxy_id: str, *, kind: str, local_proxy_url: str) -> None:
        if kind == "vless":
            raise ValueError(
                f"BOSS_ZHIPIN_PROXY_POOL proxy_id={proxy_id} 是 VLESS 节点，"
                "不能直接作为 Chrome/httpx 可消费代理；请先用 Clash/mihomo 暴露为本地 HTTP/SOCKS 端口。"
            )
        if kind not in cls._CONSUMABLE_KINDS:
            raise ValueError(f"BOSS_ZHIPIN_PROXY_POOL proxy_id={proxy_id} kind 不支持: {kind}")
        scheme = urlsplit(local_proxy_url).scheme
        if scheme not in cls._ALLOWED_SCHEMES:
            raise ValueError(
                "BOSS_ZHIPIN_PROXY_POOL proxy_id="
                f"{proxy_id} local_proxy_url scheme 不支持: {scheme or '<empty>'}"
            )
