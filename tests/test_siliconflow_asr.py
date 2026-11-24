import io
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import UploadFile
from api import fish_asr as asr


class SiliconFlowAsrTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        provider = patch.object(asr, 'ASR_PROVIDER', 'siliconflow')
        provider.start()
        self.addCleanup(provider.stop)

    async def call_asr(self):
        return await asr._perform_asr_request(
            'test', 'zh', False, UploadFile(filename='test.wav', file=io.BytesIO(b'audio'))
        )

    async def test_provider_contract_and_dify_segments(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(200, json={'text': '测试字幕'}, request=httpx.Request('POST', 'https://test'))
        with patch.object(asr, 'SILICONFLOW_API_KEY', 'test-key', create=True), patch.object(asr, 'async_http_client', client):
            result = await self.call_asr()
        self.assertEqual(result['segments'], [{'text': '测试字幕'}])
        self.assertEqual(result['text'], '测试字幕')
        self.assertFalse(result['timestamps_available'])
        args = client.post.call_args
        self.assertEqual(args.args[0], 'https://api.siliconflow.cn/v1/audio/transcriptions')
        self.assertEqual(args.kwargs['data'], {'model': 'FunAudioLLM/SenseVoiceSmall'})
        self.assertEqual(args.kwargs['files']['file'][1], b'audio')
        self.assertEqual(args.kwargs['headers']['Authorization'], 'Bearer test-key')

    async def test_missing_key_fails_before_network(self):
        client = AsyncMock()
        with patch.object(asr, 'SILICONFLOW_API_KEY', '', create=True), patch.object(asr, 'async_http_client', client):
            with self.assertRaisesRegex(ValueError, 'SILICONFLOW_API_KEY'):
                await self.call_asr()
        client.post.assert_not_called()

    async def test_invalid_response_is_rejected(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(200, json={'unexpected': 'body'}, request=httpx.Request('POST', 'https://test'))
        with patch.object(asr, 'SILICONFLOW_API_KEY', 'test-key', create=True), patch.object(asr, 'async_http_client', client):
            with self.assertRaisesRegex(Exception, 'text'):
                await self.call_asr()
        self.assertEqual(client.post.call_count, 1)

    async def test_unauthorized_is_not_retried(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(401, json={'message': 'invalid token'}, request=httpx.Request('POST', 'https://test'))
        with patch.object(asr, 'SILICONFLOW_API_KEY', 'test-key', create=True), patch.object(asr, 'async_http_client', client):
            with self.assertRaises(Exception):
                await self.call_asr()
        self.assertEqual(client.post.call_count, 1)


if __name__ == '__main__':
    unittest.main()
