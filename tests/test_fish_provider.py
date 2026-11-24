import io
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import UploadFile
from api import fish_asr as asr


class FishProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_fish_contract_preserves_timestamps(self):
        payload = {'text': '你好', 'duration': 1.0, 'segments': [{'text': '你好', 'start': 0.0, 'end': 1.0}]}
        client = AsyncMock()
        client.post.return_value = httpx.Response(200, json=payload, request=httpx.Request('POST', 'https://test'))
        with patch.object(asr, 'ASR_PROVIDER', 'fish', create=True), patch.object(asr, 'FISH_API_KEY', 'test', create=True), patch.object(asr, 'async_http_client', client):
            result = await asr._perform_asr_request('test', 'zh', False, UploadFile(filename='test.wav', file=io.BytesIO(b'audio')))
        self.assertEqual(result['segments'], payload['segments'])
        self.assertEqual(result['duration'], 1.0)
        args = client.post.call_args
        self.assertEqual(args.args[0], 'https://api.fish.audio/v1/asr')
        self.assertEqual(args.kwargs['data'], {'language': 'zh', 'ignore_timestamps': 'false'})
        self.assertEqual(args.kwargs['files']['audio'][1], b'audio')
        self.assertEqual(args.kwargs['headers']['Authorization'], 'Bearer test')

    async def test_fish_missing_key_fails_before_network(self):
        client = AsyncMock()
        with patch.object(asr, 'ASR_PROVIDER', 'fish', create=True), patch.object(asr, 'FISH_API_KEY', '', create=True), patch.object(asr, 'async_http_client', client):
            with self.assertRaisesRegex(ValueError, 'FISH_API_KEY'):
                await asr._perform_asr_request('test', 'zh', False, UploadFile(filename='test.wav', file=io.BytesIO(b'audio')))
        client.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
