# -*- coding: utf-8 -*-

import asyncio
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from api.file_parser import router
from api.document_parser_service import DocumentParserService as ApiDocumentParserService
from schemas.file_parse import FileParseMode
from services.document_asset_service import DocumentAssetUploadService
from services.document_parse_models import DocumentParseResult, DocumentParseWarning
from services.document_parser_service import DocumentParserService
from services.file_parse_service import (
    FileParseOptions,
    FilePayload,
    ParseInputError,
    parse_batch_payloads,
    parse_file_payload,
)
from utils.settings import settings
from api.url_content_fetch import fetch_url_content


class FileParseServiceTests(TestCase):
    def test_api_compat_import_points_to_services_parser(self):
        self.assertIs(ApiDocumentParserService, DocumentParserService)

    def test_parse_text_payload(self):
        payload = FilePayload(filename="sample.txt", content=b"hello", media_type="text/plain")
        result = asyncio.run(parse_file_payload(payload, FileParseOptions(mode=FileParseMode.STRUCTURED)))

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.file.extension, ".txt")
        self.assertEqual(result.content.text, "hello")
        self.assertEqual(result.parser.content_kind, "text")

    def test_max_chars_can_exceed_default_but_not_hard_limit(self):
        payload = FilePayload(filename="sample.txt", content=b"hello", media_type="text/plain")
        result = asyncio.run(
            parse_file_payload(
                payload,
                FileParseOptions(mode=FileParseMode.TEXT, max_chars=settings.FILE_PARSE_DEFAULT_MAX_CHARS + 1),
            )
        )
        self.assertEqual(result.status, "ok")

        with self.assertRaises(ParseInputError) as ctx:
            asyncio.run(
                parse_file_payload(
                    payload,
                    FileParseOptions(mode=FileParseMode.TEXT, max_chars=settings.FILE_PARSE_MAX_CONTENT_CHARS + 1),
                )
            )
        self.assertEqual(ctx.exception.code, "max_chars_too_large")

    def test_batch_total_limit(self):
        payload = FilePayload(filename="sample.txt", content=b"x", media_type="text/plain")

        with patch.object(settings, "FILE_PARSE_MAX_TOTAL_MB", 0):
            with self.assertRaises(ParseInputError) as ctx:
                asyncio.run(parse_batch_payloads([payload], FileParseOptions()))

        self.assertEqual(ctx.exception.code, "batch_too_large")

    def test_unconfigured_asset_upload_does_not_call_http(self):
        with patch.object(settings, "DOC_PARSER_IMAGE_UPLOAD_URL", None):
            with patch("services.document_asset_service.httpx.Client") as client_cls:
                urls = DocumentAssetUploadService.upload_images([("a.png", b"12345", "image/png")])

        self.assertEqual(urls, {})
        client_cls.assert_not_called()

    def test_asset_upload_uses_oss_single_file_contract(self):
        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"status": True, "data": {"url": "https://server.x-pilot.cn/static/a.png"}}

        class FakeClient:
            def __init__(self):
                self.posts = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, **kwargs):
                self.posts.append((url, kwargs))
                return FakeResponse()

        fake_client = FakeClient()
        DocumentAssetUploadService._token = None
        with patch.object(settings, "DOC_PARSER_IMAGE_UPLOAD_URL", "https://server.x-pilot.cn/static/file/upload"):
            with patch.object(settings, "DOC_PARSER_IMAGE_UPLOAD_FIELD", "file"):
                with patch.object(settings, "DOC_PARSER_IMAGE_UPLOAD_TOKEN", "token-1"):
                    with patch("services.document_asset_service.httpx.Client", return_value=fake_client):
                        urls = DocumentAssetUploadService.upload_images([("a.png", b"12345", "image/png")])

        self.assertEqual(urls, {"a.png": "https://server.x-pilot.cn/static/a.png"})
        self.assertEqual(fake_client.posts[0][0], "https://server.x-pilot.cn/static/file/upload")
        self.assertIn("file", fake_client.posts[0][1]["files"])
        self.assertEqual(fake_client.posts[0][1]["headers"]["Authorization"], "Bearer token-1")

    def test_http_route_parse_text(self):
        app = FastAPI()
        app.include_router(router, prefix="/api")
        client = TestClient(app)

        resp = client.post(
            "/api/file/parse",
            files={"file": ("sample.txt", b"hello", "text/plain")},
            data={"mode": "structured"},
        )
        body = resp.json()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["code"], 200)
        self.assertEqual(body["data"]["status"], "ok")
        self.assertEqual(body["data"]["file"]["extension"], ".txt")

    def test_stream_upload_limit(self):
        app = FastAPI()
        app.include_router(router, prefix="/api")
        client = TestClient(app)

        with patch.object(settings, "FILE_PARSE_MAX_UPLOAD_MB", 0):
            resp = client.post(
                "/api/file/parse",
                files={"file": ("sample.txt", BytesIO(b"x"), "text/plain")},
            )
        body = resp.json()

        self.assertEqual(resp.status_code, 413)
        self.assertEqual(body["data"]["error"]["code"], "file_too_large")

    def test_url_content_fetch_keeps_compatibility_with_structured_parser(self):
        class FakeHead:
            headers = {"content-type": "text/plain", "content-length": "5"}

        class FakeStream:
            async def __aenter__(self):
                return FakeHead()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeGetResponse:
            url = "https://example.com/a.txt"

            def raise_for_status(self):
                return None

            async def aread(self):
                return b"hello"

        class FakeClient:
            def stream(self, *args, **kwargs):
                return FakeStream()

            async def get(self, *args, **kwargs):
                return FakeGetResponse()

        class FakeParser:
            def parse_document(self, *args, **kwargs):
                return DocumentParseResult(
                    markdown="hello",
                    content_kind="text",
                    meta={"source": "fake"},
                    warnings=[DocumentParseWarning(code="fake_warning", message="fake warning")],
                )

        with patch("api.url_content_fetch._get_parser", return_value=FakeParser()):
            result = asyncio.run(fetch_url_content("https://example.com/a.txt", FakeClient()))

        self.assertEqual(result["content_fetch_status"], "ok")
        self.assertEqual(result["content_text"], "hello")
        self.assertEqual(result["content_meta"], {"source": "fake"})
        self.assertEqual(result["content_warnings"], ["fake warning"])
