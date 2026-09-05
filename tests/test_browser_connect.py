# -*- coding: utf-8 -*-
"""browser_connect.connect_existing 连接工具测试。

锁定根因：连接时必须走 existing_only，端口不通直接抛错，绝不自起临时 profile。
"""

import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _install_fake_drissionpage(monkeypatch, *, page_factory):
    """装一个假的 DrissionPage 模块，记录 options 调用轨迹。"""
    calls = {"existing_only": 0, "address": None}

    class _FakeOptions:
        def set_address(self, addr):
            calls["address"] = addr
            return self

        def existing_only(self):
            calls["existing_only"] += 1
            return self

    module = types.ModuleType("DrissionPage")
    module.ChromiumOptions = _FakeOptions
    module.ChromiumPage = page_factory
    monkeypatch.setitem(sys.modules, "DrissionPage", module)
    return calls


def test_connect_existing_sets_existing_only(monkeypatch):
    captured = {}

    def page_factory(options):
        captured["options"] = options
        return "PAGE"

    calls = _install_fake_drissionpage(monkeypatch, page_factory=page_factory)

    from services.browser_connect import connect_existing

    page = connect_existing("127.0.0.1:9527")

    assert page == "PAGE"
    assert calls["address"] == "127.0.0.1:9527"
    assert calls["existing_only"] == 1


def test_connect_existing_propagates_error_without_temp_browser(monkeypatch):
    """端口连不上时直接抛错，不吞异常、不自起临时 profile。"""

    def page_factory(options):
        raise RuntimeError("无法连接浏览器，端口未监听")

    _install_fake_drissionpage(monkeypatch, page_factory=page_factory)

    from services.browser_connect import connect_existing

    try:
        connect_existing("127.0.0.1:9527")
    except RuntimeError as exc:
        assert "端口未监听" in str(exc)
    else:
        raise AssertionError("connect_existing 应在端口不通时抛错")
