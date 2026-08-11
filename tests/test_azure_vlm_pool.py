# -*- coding: utf-8 -*-
"""Azure VLM 多区域端点池：429/5xx/连接错自动切区、整轮失败抛 ConnError、4xx 不切、游标轮转。"""

import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from services import azure_vlm_client as avc
from utils.azure_models import AzureEndpoint


def _eps(names):
    return [
        AzureEndpoint(
            name=n,
            endpoint=f"https://{n}.example.com",
            api_key="k",
            deployment="gpt-4o",
            api_version="2025-04-01-preview",
        )
        for n in names
    ]


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _ok_payload(desc="ok"):
    return {"choices": [{"message": {"content": '{"img_description":"%s"}' % desc}}]}


class MultiRegionTests(IsolatedAsyncioTestCase):
    def setUp(self):
        # 固定 4 个区域，开启轮询。
        self._eps = _eps(["r1", "r2", "r3", "r4"])
        # 每个模型独立游标，测试间清空以免相互影响。
        avc._rr_cursors.clear()

    async def _call(self):
        return await avc.caption_image(
            b"imgbytes", "image/png", None,
            with_page_context=False, chart_to_table=True, request_id="t", model="gpt-4o",
        )

    async def test_429_switches_to_next_region(self):
        # r1 返回 429（含重试），r2 成功。
        calls = []

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, url, headers=None, json=None):
                calls.append(url)
                if "r1." in url:
                    return _FakeResp(429, text="rate")
                return _FakeResp(200, _ok_payload("hit-r2"))

        with patch.object(avc, "resolve_model", return_value=type("R", (), {"endpoints": self._eps})()), \
             patch.object(avc.httpx, "AsyncClient", _Client), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_MAX_RETRIES", 0), \
             patch.object(avc._settings, "FILE_UNDERSTAND_VLM_REGION_ROTATION", False), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_ENDPOINT", None), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_API_KEY", None):
            obj = await self._call()
        self.assertEqual(obj["img_description"], "hit-r2")
        # 至少打到了 r1 与 r2 两个区域。
        self.assertTrue(any("r1." in u for u in calls))
        self.assertTrue(any("r2." in u for u in calls))

    async def test_4xx_does_not_switch(self):
        # r1 返回 400（图片非法），应直接失败，不切 r2。
        calls = []

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, url, headers=None, json=None):
                calls.append(url)
                return _FakeResp(400, text="bad image")

        with patch.object(avc, "resolve_model", return_value=type("R", (), {"endpoints": self._eps})()), \
             patch.object(avc.httpx, "AsyncClient", _Client), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_MAX_RETRIES", 0), \
             patch.object(avc._settings, "FILE_UNDERSTAND_VLM_REGION_ROTATION", False), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_ENDPOINT", None), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_API_KEY", None):
            with self.assertRaises(avc.AzureVLMError):
                await self._call()
        # 只打了一个区域（r1），没有切区。
        self.assertEqual(len([u for u in calls if "r1." in u]), 1)
        self.assertFalse(any("r2." in u for u in calls))

    async def test_all_conn_fail_raises_connerror(self):
        # 所有区域连接失败 -> AzureVLMConnError。
        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, url, headers=None, json=None):
                raise httpx.ConnectError("boom")

        with patch.object(avc, "resolve_model", return_value=type("R", (), {"endpoints": self._eps})()), \
             patch.object(avc.httpx, "AsyncClient", _Client), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_MAX_RETRIES", 0), \
             patch.object(avc._settings, "FILE_UNDERSTAND_VLM_REGION_ROTATION", False), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_ENDPOINT", None), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_API_KEY", None):
            with self.assertRaises(avc.AzureVLMConnError):
                await self._call()

    async def test_rotation_advances_cursor(self):
        # 轮询开启：连续两次调用起始区域应不同（游标推进）。
        seen_starts = []

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def post(self, url, headers=None, json=None):
                # 记录本次调用命中的第一个区域（起始区）。
                seen_starts.append(url)
                return _FakeResp(200, _ok_payload("ok"))

        with patch.object(avc, "resolve_model", return_value=type("R", (), {"endpoints": self._eps})()), \
             patch.object(avc.httpx, "AsyncClient", _Client), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_MAX_RETRIES", 0), \
             patch.object(avc._settings, "FILE_UNDERSTAND_VLM_REGION_ROTATION", True), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_ENDPOINT", None), \
             patch.object(avc._settings, "DOC_IMPORT_AZURE_API_KEY", None):
            await self._call()
            first = seen_starts[-1]
            await self._call()
            second = seen_starts[-1]
        # 两次起始区域不同（游标从 0 -> 1）。
        self.assertNotEqual(first, second)
