# -*- coding: utf-8 -*-
"""Vertex Gemini 客户端故障注入：429/5xx/超时换区、Retry-After、4xx 归类、401 刷新。"""

import asyncio
from unittest import TestCase
from unittest.mock import AsyncMock, patch

import httpx

from services import gemini_vertex_client as gvc
from services.file_understand_provider import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
)


def _resp(status_code: int, json_body=None, headers=None):
    request = httpx.Request("POST", "https://x")
    return httpx.Response(
        status_code, json=json_body or {}, headers=headers or {}, request=request
    )


class GeminiVertexClientTests(TestCase):
    def setUp(self):
        # 每个用例独立，避免复用被 mock 的全局 client。
        gvc._http_client = None

    def _call(self, **overrides):
        kwargs = dict(
            model="gemini-2.5-flash",
            contents=[{"role": "user", "parts": [{"text": "hi"}]}],
            location="global",
            timeout_sec=5.0,
            max_locations=2,
            request_id="t",
        )
        kwargs.update(overrides)
        return asyncio.run(gvc.generate_content(**kwargs))

    def test_success_returns_data(self):
        ok = _resp(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})
        with patch.object(gvc, "get_adc_token", AsyncMock(return_value="tok")), \
             patch.object(gvc, "_get_http_client") as gc:
            gc.return_value.post = AsyncMock(return_value=ok)
            data = self._call()
        self.assertEqual(gvc.extract_text(data), "ok")

    def test_429_all_regions_raises_rate_limit(self):
        r429 = _resp(429, {"error": "rate"}, headers={"Retry-After": "0"})
        with patch.object(gvc, "get_adc_token", AsyncMock(return_value="tok")), \
             patch.object(gvc, "_get_http_client") as gc:
            gc.return_value.post = AsyncMock(return_value=r429)
            with self.assertRaises(ProviderRateLimitError):
                self._call()

    def test_5xx_all_regions_raises_unavailable(self):
        r500 = _resp(500, {"error": "boom"})
        with patch.object(gvc, "get_adc_token", AsyncMock(return_value="tok")), \
             patch.object(gvc, "_get_http_client") as gc:
            gc.return_value.post = AsyncMock(return_value=r500)
            with self.assertRaises(ProviderUnavailableError):
                self._call()

    def test_deterministic_4xx_raises_request_error(self):
        r400 = _resp(400, {"error": "bad schema"})
        with patch.object(gvc, "get_adc_token", AsyncMock(return_value="tok")), \
             patch.object(gvc, "_get_http_client") as gc:
            gc.return_value.post = AsyncMock(return_value=r400)
            with self.assertRaises(ProviderRequestError):
                self._call()

    def test_timeout_all_regions_raises(self):
        with patch.object(gvc, "get_adc_token", AsyncMock(return_value="tok")), \
             patch.object(gvc, "_get_http_client") as gc:
            gc.return_value.post = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
            # net 超时归类为 timeout。
            from services.file_understand_provider import ProviderTimeoutError
            with self.assertRaises(ProviderTimeoutError):
                self._call()

    def test_401_forces_refresh_then_succeeds(self):
        r401 = _resp(401, {"error": "expired"})
        ok = _resp(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})
        post = AsyncMock(side_effect=[r401, ok])
        token = AsyncMock(side_effect=["tok1", "tok2"])
        with patch.object(gvc, "get_adc_token", AsyncMock(return_value="tok0")), \
             patch.object(gvc, "get_access_token", token), \
             patch.object(gvc, "_get_http_client") as gc:
            gc.return_value.post = post
            data = self._call()
        self.assertEqual(gvc.extract_text(data), "ok")
        token.assert_awaited()  # 强制刷新被调用
