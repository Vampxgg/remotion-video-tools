# -*- coding: utf-8 -*-
"""搜索页 URL 工具（无重型依赖）。"""

from urllib.parse import parse_qs, unquote, urlparse


def resolve_google_redirect_url(href: str) -> str:
    """将 Google /url?q=... 或 /url?url=... 转为真实链接。"""
    if not href:
        return ""
    try:
        p = urlparse(href.strip())
        if "google." not in p.netloc.lower():
            return href
        if not p.path.startswith("/url"):
            return href
        qs = parse_qs(p.query)
        for key in ("q", "url"):
            vals = qs.get(key)
            if vals and vals[0]:
                return unquote(vals[0])
    except Exception:
        pass
    return href
