# -*- coding: utf-8 -*-
"""BOSS 代理池诊断逻辑测试。"""

from services.boss_proxy_diagnostics import diagnose_proxy_pool


def test_diagnose_proxy_pool_checks_ports_and_masks_credentials():
    result = diagnose_proxy_pool(
        proxies=[
            {
                "proxy_id": "proxy-a",
                "local_proxy_url": "http://user:secret@127.0.0.1:18081",
                "enabled": True,
            }
        ],
        workers=[
            {
                "worker_id": "boss-a",
                "browser_host_port": "127.0.0.1:9527",
                "proxy_id": "proxy-a",
            }
        ],
        tcp_checker=lambda host, port, timeout: host == "127.0.0.1" and port == 18081,
        egress_checker=lambda proxy_url, timeout: "203.0.113.10",
    )

    proxy_status = result["proxies"][0]
    assert result["ok"] is True
    assert proxy_status["tcp_ok"] is True
    assert proxy_status["egress_ip"] == "203.0.113.10"
    assert proxy_status["local_proxy_url_masked"] == "http://user:***@127.0.0.1:18081"
    assert "secret" not in str(result)


def test_diagnose_proxy_pool_reports_worker_proxy_mismatch():
    result = diagnose_proxy_pool(
        proxies=[
            {
                "proxy_id": "proxy-a",
                "local_proxy_url": "http://127.0.0.1:18081",
                "enabled": True,
            }
        ],
        workers=[
            {"worker_id": "boss-a", "proxy_id": "proxy-a"},
            {"worker_id": "boss-b", "proxy_id": "missing-proxy"},
        ],
        tcp_checker=lambda host, port, timeout: True,
    )

    assert result["ok"] is False
    assert any("missing-proxy" in error for error in result["errors"])
