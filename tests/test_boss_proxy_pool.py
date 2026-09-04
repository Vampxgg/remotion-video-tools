# -*- coding: utf-8 -*-
"""BOSS 代理池稳定租约与脱敏状态测试。"""

import pytest

from services.boss_proxy_pool import BossProxyPool


def _proxy_pool():
    return [
        {
            "proxy_id": "boss-proxy-a",
            "local_proxy_url": "http://user:secret-a@127.0.0.1:18081",
            "chrome_proxy_server": "http=127.0.0.1:18081;https=127.0.0.1:18081",
            "upstream_label": "CF官方优选1",
        },
        {
            "proxy_id": "boss-proxy-b",
            "local_proxy_url": "http://user:secret-b@127.0.0.1:18082",
            "chrome_proxy_server": "http=127.0.0.1:18082;https=127.0.0.1:18082",
            "upstream_label": "CF官方优选2",
        },
    ]


def _three_proxy_pool():
    return [
        *_proxy_pool(),
        {
            "proxy_id": "boss-proxy-c",
            "local_proxy_url": "http://user:secret-c@127.0.0.1:18083",
            "chrome_proxy_server": "http=127.0.0.1:18083;https=127.0.0.1:18083",
            "upstream_label": "CF官方优选3",
        },
    ]


def test_proxy_pool_rejects_duplicate_proxy_id():
    with pytest.raises(ValueError, match="proxy_id"):
        BossProxyPool([
            {"proxy_id": "dup", "local_proxy_url": "http://127.0.0.1:18081"},
            {"proxy_id": "dup", "local_proxy_url": "http://127.0.0.1:18082"},
        ])


def test_proxy_pool_rejects_duplicate_local_proxy_url():
    with pytest.raises(ValueError, match="local_proxy_url"):
        BossProxyPool([
            {"proxy_id": "a", "local_proxy_url": "http://127.0.0.1:18081"},
            {"proxy_id": "b", "local_proxy_url": "http://127.0.0.1:18081"},
        ])


def test_proxy_pool_leases_requested_proxy_id_and_masks_credentials():
    pool = BossProxyPool(_proxy_pool())

    lease = pool.lease_for_worker("boss-a", requested_proxy_id="boss-proxy-a")

    assert lease.proxy_id == "boss-proxy-a"
    assert lease.local_proxy_url == "http://user:secret-a@127.0.0.1:18081"
    assert lease.chrome_proxy_server == "http=127.0.0.1:18081;https=127.0.0.1:18081"
    status = pool.status()
    assert status["boss-proxy-a"]["lease_worker_id"] == "boss-a"
    assert "secret-a" not in status["boss-proxy-a"]["local_proxy_url_masked"]
    assert status["boss-proxy-a"]["local_proxy_url_masked"] == "http://user:***@127.0.0.1:18081"


def test_proxy_pool_auto_assigns_stable_unique_leases():
    pool = BossProxyPool(_proxy_pool())

    first = pool.lease_for_worker("boss-a")
    second = pool.lease_for_worker("boss-b")
    first_again = pool.lease_for_worker("boss-a")

    assert first.proxy_id == "boss-proxy-a"
    assert second.proxy_id == "boss-proxy-b"
    assert first_again.proxy_id == first.proxy_id


def test_proxy_pool_random_strategy_uses_configured_random_choice():
    pool = BossProxyPool(
        _three_proxy_pool(),
        selection_strategy="random",
        random_choice=lambda entries: entries[-1],
    )

    lease = pool.lease_for_worker("boss-a")

    assert lease.proxy_id == "boss-proxy-c"


def test_proxy_pool_round_robin_strategy_rotates_released_leases():
    pool = BossProxyPool(_three_proxy_pool(), selection_strategy="round_robin")

    first = pool.lease_for_worker("boss-a")
    pool.release_worker("boss-a")
    second = pool.lease_for_worker("boss-b")

    assert first.proxy_id == "boss-proxy-a"
    assert second.proxy_id == "boss-proxy-b"


def test_proxy_pool_recent_avoid_skips_recently_used_proxy_when_possible():
    pool = BossProxyPool(
        _three_proxy_pool(),
        selection_strategy="ordered",
        recent_avoid_count=1,
    )

    first = pool.lease_for_worker("boss-a")
    pool.release_worker("boss-a")
    second = pool.lease_for_worker("boss-b")

    assert first.proxy_id == "boss-proxy-a"
    assert second.proxy_id == "boss-proxy-b"


def test_proxy_pool_rejects_when_healthy_proxy_insufficient():
    pool = BossProxyPool(_proxy_pool())
    pool.lease_for_worker("boss-a")
    pool.lease_for_worker("boss-b")

    with pytest.raises(ValueError, match="可用代理不足"):
        pool.lease_for_worker("boss-c")


def test_proxy_pool_cools_proxy_and_removes_from_assignment():
    pool = BossProxyPool(_proxy_pool(), default_cooldown_seconds=60)
    pool.lease_for_worker("boss-a", requested_proxy_id="boss-proxy-a")

    pool.mark_cooldown("boss-proxy-a", reason="访问受限", seconds=120)

    status = pool.status()
    assert status["boss-proxy-a"]["state"] == "cooldown"
    assert status["boss-proxy-a"]["cooldown_remaining_seconds"] > 0
    assert status["boss-proxy-a"]["last_error"] == "访问受限"
    with pytest.raises(ValueError, match="冷却"):
        pool.lease_for_worker("boss-c", requested_proxy_id="boss-proxy-a")


def test_proxy_pool_reassigns_worker_to_next_healthy_proxy():
    pool = BossProxyPool([
        *_proxy_pool(),
        {
            "proxy_id": "boss-proxy-c",
            "local_proxy_url": "http://user:secret-c@127.0.0.1:18083",
            "chrome_proxy_server": "http=127.0.0.1:18083;https=127.0.0.1:18083",
            "upstream_label": "CF官方优选3",
        },
    ])
    pool.lease_for_worker("boss-a", requested_proxy_id="boss-proxy-a")
    pool.lease_for_worker("boss-b", requested_proxy_id="boss-proxy-b")

    lease = pool.reassign_worker(
        "boss-a",
        bad_proxy_id="boss-proxy-a",
        reason="访问受限",
        seconds=120,
    )

    assert lease.proxy_id == "boss-proxy-c"
    status = pool.status()
    assert status["boss-proxy-a"]["state"] == "cooldown"
    assert status["boss-proxy-a"]["lease_worker_id"] is None
    assert status["boss-proxy-a"]["last_lease_worker_id"] == "boss-a"
    assert status["boss-proxy-a"]["replaced_by"] == "boss-proxy-c"
    assert status["boss-proxy-b"]["lease_worker_id"] == "boss-b"
    assert status["boss-proxy-c"]["state"] == "leased"
    assert status["boss-proxy-c"]["lease_worker_id"] == "boss-a"


def test_proxy_pool_health_checker_excludes_failed_proxy():
    def health_check(proxy_url):
        return proxy_url.endswith(":18082")

    pool = BossProxyPool(_proxy_pool(), health_checker=health_check)

    lease = pool.lease_for_worker("boss-a")

    assert lease.proxy_id == "boss-proxy-b"
    assert pool.status()["boss-proxy-a"]["state"] == "unhealthy"


def test_proxy_pool_skips_disabled_proxy_and_exposes_group_kind():
    pool = BossProxyPool([
        {
            "proxy_id": "disabled-proxy",
            "enabled": False,
            "kind": "local_http",
            "group": "backup",
            "local_proxy_url": "http://127.0.0.1:18081",
        },
        {
            "proxy_id": "active-proxy",
            "enabled": True,
            "kind": "local_http",
            "group": "primary",
            "local_proxy_url": "http://127.0.0.1:18082",
        },
    ])

    lease = pool.lease_for_worker("boss-a")
    status = pool.status()

    assert lease.proxy_id == "active-proxy"
    assert status["disabled-proxy"]["state"] == "disabled"
    assert status["active-proxy"]["kind"] == "local_http"
    assert status["active-proxy"]["group"] == "primary"


def test_proxy_pool_rejects_vless_as_consumable_proxy():
    with pytest.raises(ValueError, match="VLESS"):
        BossProxyPool([
            {
                "proxy_id": "vless-node",
                "kind": "vless",
                "local_proxy_url": "vless://example",
            }
        ])


def test_proxy_pool_rejects_invalid_proxy_scheme_without_leaking_credentials():
    with pytest.raises(ValueError) as exc_info:
        BossProxyPool([
            {
                "proxy_id": "bad",
                "local_proxy_url": "ftp://user:secret@127.0.0.1:18081",
            }
        ])

    assert "secret" not in str(exc_info.value)
