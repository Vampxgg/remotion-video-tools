# -*- coding: utf-8 -*-
"""文档解析资产上传服务。"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import httpx

from utils.settings import settings as _settings

logger = logging.getLogger(__name__)


class DocumentAssetUploadService:
    _token: Optional[str] = None
    _token_lock = threading.Lock()

    @classmethod
    def _current_token(cls) -> Optional[str]:
        return cls._token or _settings.DOC_PARSER_IMAGE_UPLOAD_TOKEN

    @classmethod
    def _auth_headers(cls) -> Dict[str, str]:
        token = cls._current_token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    @classmethod
    def _refresh_token(cls, client: httpx.Client) -> bool:
        login_url = _settings.DOC_PARSER_IMAGE_UPLOAD_LOGIN_URL
        login = _settings.DOC_PARSER_IMAGE_UPLOAD_LOGIN
        password = _settings.DOC_PARSER_IMAGE_UPLOAD_PASSWORD
        if not (login_url and login and password):
            return False

        # 并发上传时多个线程可能同时 401；加锁 + 双检，避免重复登录刷新。
        token_before = cls._token
        with cls._token_lock:
            if cls._token and cls._token != token_before:
                return True  # 已被其它线程刷新
            try:
                resp = client.post(
                    login_url,
                    data={"login": login, "password": password},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                resp.raise_for_status()
                resp_data = resp.json()
                token = (resp_data.get("data") or {}).get("token")
                if not token:
                    logger.warning("image upload token refresh response missing token")
                    return False
                cls._token = token
                logger.info("image upload token refreshed")
                return True
            except Exception as exc:
                logger.warning("image upload token refresh failed: %s", exc)
                return False

    @staticmethod
    def _extract_uploaded_url(resp_data: Dict, filename: str) -> Optional[str]:
        data = resp_data.get("data")
        if isinstance(data, dict):
            url = data.get("url")
            return url if isinstance(url, str) and url else None
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                orig = item.get("originalname") or item.get("filename") or filename
                url = item.get("url")
                if orig == filename and isinstance(url, str) and url:
                    return url
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]:
                    return item["url"]
        return None

    @classmethod
    def _upload_one(
            cls,
            client: httpx.Client,
            upload_url: str,
            filename: str,
            data: bytes,
            mime_type: str,
    ) -> Optional[str]:
        files_payload = {
            _settings.DOC_PARSER_IMAGE_UPLOAD_FIELD: (filename, data, mime_type),
        }
        resp = client.post(upload_url, headers=cls._auth_headers(), files=files_payload)
        if resp.status_code == 401 and cls._refresh_token(client):
            resp = client.post(upload_url, headers=cls._auth_headers(), files=files_payload)
        resp.raise_for_status()
        resp_data = resp.json()
        if resp_data.get("status") is not True:
            logger.warning("image upload response status false: %s", resp_data.get("message"))
            return None
        return cls._extract_uploaded_url(resp_data, filename)

    @classmethod
    def upload_images(cls, images: List[tuple]) -> Dict[str, str]:
        """并发上传图片，返回 {原始文件名: URL}。

        大图量文档（教材常 300-500 图）串行上传耗时数十秒且阻塞基础解析；改为线程池
        并发（共享 httpx.Client 连接池）。先串行传第一张预热鉴权 token，避免首轮并发
        全部 401 触发重复登录刷新。
        """
        if not images:
            return {}
        upload_url = _settings.DOC_PARSER_IMAGE_UPLOAD_URL
        if not upload_url:
            return {}

        concurrency = max(1, int(getattr(_settings, "DOC_PARSER_IMAGE_UPLOAD_CONCURRENCY", 10) or 1))
        url_map: Dict[str, str] = {}
        lock = threading.Lock()

        with httpx.Client(timeout=60, verify=False) as client:
            def _do(item):
                fname, data, mime = item
                try:
                    url = cls._upload_one(client, upload_url, fname, data, mime)
                    if url:
                        with lock:
                            url_map[fname] = url
                        logger.info("embedded image uploaded: %s -> %s", fname, url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("embedded image upload failed for %s: %s", fname, exc)

            # 预热：串行传第一张，确保 token 就绪后再并发。
            pending = list(images)
            if concurrency > 1 and len(pending) > 1:
                _do(pending[0])
                pending = pending[1:]
                with ThreadPoolExecutor(max_workers=concurrency) as ex:
                    list(ex.map(_do, pending))
            else:
                for item in pending:
                    _do(item)
        return url_map
