# -*- coding: utf-8 -*-
"""内嵌图 VLM 语义打标服务（供 manifest 检索与图文匹配）。

对 ``DocumentManifest`` 里每张内嵌图（``ManifestImage``）用 Azure 多模态模型
（FW-Kimi-K2.7-Code）打标，就地填充：
  - ``img_description``   : 一句客观中文语义描述（供检索/图文匹配）；
  - ``img_keywords``      : 关键词列表（供召回）；
  - ``img_type``          : chart|photo|diagram|screenshot|unknown；
  - ``chart_table_markdown``: 当判定为 chart 且开启转表时，图表数值转写的 Markdown 表。

设计：
  - 打标走 Azure（国内可达，见 services/azure_vlm_client.py），彻底摆脱谷歌
    oauth2.googleapis.com 出网超时导致的整体雪崩；不再依赖 Vertex/全局限流器。
  - 结构化输出用 Azure 的 response_format=json_object；失败仅记 warning 不阻断 manifest。
  - 可选把"所在页整页 PNG"作为上下文一起喂，让模型更懂图在讲什么。
  - 页内多图并发打标，受 DOC_IMPORT_VLM_CONCURRENCY 约束。
  - 快速失败护栏：连接超时压到 10s；连续多张因连接不可达失败则整批短路跳过，
    避免网络断时把请求硬拖到分钟级。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

import httpx

from schemas.document_manifest import DocImageType, DocumentManifest, DocumentParseOptions, ManifestImage
from services import azure_vlm_client as avc
from services.azure_vlm_client import AzureVLMConnError, AzureVLMError
from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/document_vlm_caption.log")

_VALID_TYPES = {
    DocImageType.CHART, DocImageType.DIAGRAM, DocImageType.SCREENSHOT,
    DocImageType.PHOTO, DocImageType.UNKNOWN,
}


async def _download_bytes(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, verify=False) as client:
            resp = await client.get(url, headers={"User-Agent": _settings.FETCH_USER_AGENT})
            resp.raise_for_status()
            return resp.content
    except Exception as e:  # noqa: BLE001
        logger.warning("download image for caption failed url=%s: %s", url, e)
        return None


async def _caption_one(
    image: ManifestImage,
    page_png_bytes: Optional[bytes],
    chart_to_table: bool,
    request_id: str,
) -> bool:
    """对单张图打标，就地写回 image。返回是否成功。

    :raises AzureVLMConnError: 连接层不可达（交由上层做整批熔断判断）。
    """
    if not image.img_url:
        return False
    img_bytes = await _download_bytes(image.img_url)
    if not img_bytes:
        return False

    # 连接错误向上抛（触发熔断）；其它错误在此吞掉记 warning。
    try:
        obj = await avc.caption_image(
            img_bytes,
            image.mime_type or "image/png",
            page_png_bytes,
            with_page_context=_settings.DOC_IMPORT_VLM_WITH_PAGE_CONTEXT,
            chart_to_table=chart_to_table,
            request_id=request_id,
        )
    except AzureVLMError as e:
        logger.warning("caption image failed url=%s: %s", image.img_url, e)
        return False

    img_type = (obj.get("img_type") or DocImageType.UNKNOWN)
    img_type = img_type.strip() if isinstance(img_type, str) else DocImageType.UNKNOWN
    if img_type not in _VALID_TYPES:
        img_type = DocImageType.UNKNOWN
    image.img_type = img_type
    image.img_description = (obj.get("img_description") or "").strip() or None
    kws = obj.get("img_keywords") or []
    if isinstance(kws, list):
        image.img_keywords = [str(k).strip() for k in kws if str(k).strip()][:8]
    else:
        image.img_keywords = [s.strip() for s in str(kws).split("、") if s.strip()][:8]
    if chart_to_table and img_type == DocImageType.CHART:
        tbl = (obj.get("chart_table_markdown") or "").strip()
        image.chart_table_markdown = tbl or None
    return True


async def caption_manifest_images(
    manifest: DocumentManifest, options: DocumentParseOptions
) -> None:
    """遍历 manifest 所有页的内嵌图并发打标（走 Azure），就地写回。失败仅记 warning。

    快速失败护栏：连续多张因“连接不可达”失败即整批短路跳过剩余图，避免网络断时雪崩。
    """
    if not options.vlm_caption:
        return
    chart_to_table = options.vlm_chart_to_table
    request_id = uuid.uuid4().hex[:8]

    # 预取每页整页 PNG（作上下文），避免每张图重复下载。
    page_png_cache: dict = {}
    if _settings.DOC_IMPORT_VLM_WITH_PAGE_CONTEXT:
        for page in manifest.pages:
            if page.images and page.page_png_url:
                page_png_cache[page.index] = await _download_bytes(page.page_png_url)

    sem = asyncio.Semaphore(max(1, _settings.DOC_IMPORT_VLM_CONCURRENCY))
    ok = 0
    fail = 0
    conn_fail_streak = 0
    circuit_open = False
    conn_threshold = max(1, int(_settings.DOC_IMPORT_AZURE_CIRCUIT_CONSECUTIVE_CONN_FAILS))

    async def _guarded(image: ManifestImage, page_index: int) -> None:
        nonlocal ok, fail, conn_fail_streak, circuit_open
        if circuit_open:
            fail += 1
            return
        async with sem:
            if circuit_open:
                fail += 1
                return
            try:
                success = await _caption_one(
                    image,
                    page_png_cache.get(page_index),
                    chart_to_table,
                    request_id,
                )
            except AzureVLMConnError as e:
                conn_fail_streak += 1
                fail += 1
                logger.warning(
                    "caption image conn-failed url=%s streak=%s: %s",
                    image.img_url, conn_fail_streak, e,
                )
                if conn_fail_streak >= conn_threshold and not circuit_open:
                    circuit_open = True
                    logger.error(
                        "Azure 连续 %s 张图连接失败，整批短路跳过剩余打标 doc_id=%s",
                        conn_fail_streak, manifest.doc_id,
                    )
                return
            except Exception as e:  # noqa: BLE001
                fail += 1
                logger.warning("caption image failed url=%s: %s", image.img_url, e)
                return
        # 成功一张即重置连接失败连击。
        conn_fail_streak = 0
        if success:
            ok += 1
        else:
            fail += 1

    tasks = [
        _guarded(image, page.index)
        for page in manifest.pages
        for image in page.images
    ]
    if not tasks:
        return
    await asyncio.gather(*tasks, return_exceptions=True)

    manifest.meta["vlm_captioned"] = ok
    manifest.meta["vlm_caption_failed"] = fail
    manifest.meta["vlm_provider"] = "azure"
    manifest.meta["vlm_model"] = avc.current_deployment()
    if circuit_open:
        manifest.warnings.append(
            f"Azure 打标连接不可达，已熔断跳过剩余图（成功 {ok}，失败/跳过 {fail}）。"
            "图片仍可复用，仅缺 VLM 语义描述。"
        )
    elif fail:
        manifest.warnings.append(f"内嵌图 VLM 打标：成功 {ok}，失败/跳过 {fail}。")
    logger.info(
        "caption done doc_id=%s ok=%s fail=%s provider=azure model=%s circuit=%s",
        manifest.doc_id, ok, fail, avc.current_deployment(), circuit_open,
    )
