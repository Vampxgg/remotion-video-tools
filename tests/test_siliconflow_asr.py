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
        model = patch.object(asr, 'SILICONFLOW_ASR_MODEL', 'FunAudioLLM/SenseVoiceSmall')
        model.start()
        self.addCleanup(model.stop)

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

    async def test_xingchen_verbose_json_preserves_timestamps(self):
        payload = {'text': '你好。测试。', 'duration': 6.0, 'segments': [
            {'text': '你好。', 'start': 0.72, 'end': 2.84, 'speaker': '0'},
            {'text': '测试。', 'start': 3.48, 'end': 5.759},
        ]}
        for model in ('XingChenASR-V3.2', 'XingChenASR-V3.2-Ultra', 'XingChenASR-Diarize-V3.0'):
            with self.subTest(model=model):
                client = AsyncMock()
                client.post.return_value = httpx.Response(200, json=payload, request=httpx.Request('POST', 'https://test'))
                with patch.object(asr, 'SILICONFLOW_API_KEY', 'test-key'), patch.object(asr, 'async_http_client', client), patch.object(asr, 'SILICONFLOW_ASR_MODEL', 'XingChenAGI/' + model):
                    result = await self.call_asr()
                self.assertTrue(result['timestamps_available'])
                self.assertEqual(result['segments'], payload['segments'])
                self.assertEqual(result['duration'], 6.0)
                self.assertEqual(client.post.call_args.kwargs['data']['response_format'], 'verbose_json')

    async def test_ignore_timestamps_skips_verbose_format(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(200, json={'text': '测试', 'segments': [{'text': '测试', 'start': 0, 'end': 1}]}, request=httpx.Request('POST', 'https://test'))
        with patch.object(asr, 'SILICONFLOW_API_KEY', 'test-key'), patch.object(asr, 'async_http_client', client), patch.object(asr, 'SILICONFLOW_ASR_MODEL', 'XingChenAGI/XingChenASR-V3.2'):
            result = await asr._perform_asr_request('test', 'zh', True, UploadFile(filename='test.wav', file=io.BytesIO(b'audio')))
        self.assertNotIn('response_format', client.post.call_args.kwargs['data'])
        self.assertFalse(result['timestamps_available'])
        self.assertEqual(result['segments'], [{'text': '测试'}])

    async def test_fish_contract_preserved(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(200, json={'text': '测试', 'segments': [{'text': '测试', 'start': 0, 'end': 1}]}, request=httpx.Request('POST', 'https://test'))
        with patch.object(asr, 'ASR_PROVIDER', 'fish'), patch.object(asr, 'FISH_API_KEY', 'test-key'), patch.object(asr, 'async_http_client', client):
            result = await self.call_asr()
        self.assertTrue(result['timestamps_available'])
        self.assertEqual(client.post.call_args.kwargs['data'], {'language': 'zh', 'ignore_timestamps': 'false'})
        self.assertIn('audio', client.post.call_args.kwargs['files'])


class TranscriptNormalizationTests(unittest.TestCase):
    def test_empty_segments_keep_available_text(self):
        result = asr._normalize_transcript({'text': '全文', 'segments': []}, False)
        self.assertEqual(result['segments'], [{'text': '全文'}])
        self.assertFalse(result['timestamps_available'])

    def test_silence_has_no_timestamps(self):
        result = asr._normalize_transcript({'text': '', 'segments': []}, False)
        self.assertEqual(result['segments'], [])
        self.assertFalse(result['timestamps_available'])

    def test_partial_timestamps_are_not_advertised_as_complete(self):
        result = asr._normalize_transcript({'text': '全文', 'segments': [{'text': '全文', 'start': 0}]}, False)
        self.assertFalse(result['timestamps_available'])

    def test_invalid_timestamps_are_rejected(self):
        for start, end in [(None, 1), ('0', 1), (True, 1), (-1, 1), (2, 1), (0, float('inf')), (float('nan'), 1)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                asr._normalize_transcript({'text': '全文', 'segments': [{'text': '全文', 'start': start, 'end': end}]}, False)

    def test_invalid_segments_are_rejected(self):
        for segments in ['text', [None], [{'text': 42}]]:
            with self.subTest(segments=segments), self.assertRaises(ValueError):
                asr._normalize_transcript({'text': '全文', 'segments': segments}, False)


if __name__ == '__main__':
    unittest.main()
