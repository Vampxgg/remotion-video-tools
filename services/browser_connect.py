# -*- coding: utf-8 -*-
"""统一的 DrissionPage 连接工具。

根因治理：项目里多处用 ``ChromiumPage("127.0.0.1:<port>")`` 只连接已存在的
Chrome 调试端口。但 DrissionPage 在端口未监听且 ``existing_only=False``（默认）
时会**静默自起**一个 ``%TEMP%\\DrissionPage\\userData\\<port>`` 的空白、无登录、
无代理临时浏览器，导致采集命中登录墙/风控且极难排查。

这里统一封装：连接时强制 ``existing_only()``，端口连不上就直接抛错，绝不自起
临时 profile。所有连接点复用本工具，避免重复补丁。
"""

from __future__ import annotations

from typing import Any


def connect_existing(host_port: str) -> Any:
    """连接一个**已存在**的 Chrome 调试端口，返回 ``ChromiumPage``。

    端口未监听/连不上时由 DrissionPage 直接抛错，不会自起临时 profile 浏览器。

    :param host_port: 形如 ``127.0.0.1:9527`` 的调试地址。
    """
    from DrissionPage import ChromiumPage, ChromiumOptions

    options = ChromiumOptions().set_address(host_port)
    options.existing_only()
    return ChromiumPage(options)
