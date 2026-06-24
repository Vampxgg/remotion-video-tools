# -*- coding: utf-8 -*-
"""正式文件上传解析 API 路由。"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from schemas.file_parse import FileParseBatchData, FileParseBatchSummary, FileParseMode
from services.file_parse_service import (
    FileParseOptions,
    FilePayload,
    ParseInputError,
    failed_result,
    parse_file_payload,
)
from utils.responses import create_standard_response
from utils.security import require_api_key
from utils.settings import settings as _settings


router = APIRouter(dependencies=[Depends(require_api_key("FILE_PARSE_API_KEY"))])
logger = logging.getLogger(__name__)


async def _payload_from_upload(
        upload: UploadFile,
        *,
        total_so_far: int = 0,
) -> tuple[FilePayload, int]:
    chunks = []
    size = 0
    single_limit = _settings.FILE_PARSE_MAX_UPLOAD_MB * 1024 * 1024
    total_limit = _settings.FILE_PARSE_MAX_TOTAL_MB * 1024 * 1024
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        total_so_far += len(chunk)
        if size > single_limit:
            raise ParseInputError(
                413,
                "file_too_large",
                f"文件过大，最大支持 {_settings.FILE_PARSE_MAX_UPLOAD_MB}MB。",
            )
        if total_so_far > total_limit:
            raise ParseInputError(
                413,
                "batch_too_large",
                f"批量文件总大小超过 {_settings.FILE_PARSE_MAX_TOTAL_MB}MB。",
            )
        chunks.append(chunk)
    return FilePayload(
        filename=upload.filename or "unknown",
        content=b"".join(chunks),
        media_type=upload.content_type,
    ), total_so_far


def _options(
        mode: FileParseMode,
        max_chars: Optional[int],
        enable_ocr: Optional[bool],
        enable_embedded_image_upload: Optional[bool],
) -> FileParseOptions:
    return FileParseOptions(
        mode=mode,
        max_chars=max_chars,
        enable_ocr=enable_ocr,
        enable_embedded_image_upload=enable_embedded_image_upload,
    )


@router.post(
    "/file/parse",
    summary="解析单个上传文件为 LLM 友好的 Markdown/文本结果",
)
async def parse_file(
    file: UploadFile = File(..., description="唯一文件字段，字段名必须为 file"),
    mode: FileParseMode = Form(FileParseMode.MARKDOWN, description="输出模式"),
    enable_ocr: Optional[bool] = Form(None, description="是否启用图片 OCR"),
    enable_embedded_image_upload: Optional[bool] = Form(None, description="是否上传文档内嵌图片"),
    max_chars: Optional[int] = Form(None, description="返回内容最大字符数，上限由服务端配置控制"),
):
    try:
        payload, _ = await _payload_from_upload(file)
        result = await parse_file_payload(
            payload,
            _options(mode, max_chars, enable_ocr, enable_embedded_image_upload),
        )
    except ParseInputError as exc:
        return create_standard_response(
            data={"error": {"code": exc.code, "detail": exc.detail}},
            code=exc.status_code,
            message=exc.detail,
        )
    except Exception as exc:
        logger.exception("file parse failed: %s", exc)
        detail = "文件解析失败，请稍后重试或联系服务维护人员。"
        return create_standard_response(
            data={"error": {"code": "internal_error", "detail": detail}},
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=detail,
        )
    finally:
        await file.close()

    return create_standard_response(
        data=result.model_dump(),
        message="文件解析完成",
    )


@router.post(
    "/file/parse/batch",
    summary="批量解析上传文件为 LLM 友好的 Markdown/文本结果",
)
async def parse_files_batch(
    files: List[UploadFile] = File(..., description="文件列表字段，字段名必须为 files"),
    mode: FileParseMode = Form(FileParseMode.MARKDOWN, description="输出模式"),
    enable_ocr: Optional[bool] = Form(None, description="是否启用图片 OCR"),
    enable_embedded_image_upload: Optional[bool] = Form(None, description="是否上传文档内嵌图片"),
    max_chars: Optional[int] = Form(None, description="每个文件返回内容最大字符数，上限由服务端配置控制"),
):
    try:
        if len(files) > _settings.FILE_PARSE_MAX_BATCH_FILES:
            raise ParseInputError(
                413,
                "too_many_files",
                f"批量最多支持 {_settings.FILE_PARSE_MAX_BATCH_FILES} 个文件。",
            )
        items = []
        total_size = 0
        parse_options = _options(mode, max_chars, enable_ocr, enable_embedded_image_upload)
        for upload in files:
            payload, total_size = await _payload_from_upload(upload, total_so_far=total_size)
            try:
                items.append(await parse_file_payload(payload, parse_options))
            except ParseInputError as item_exc:
                items.append(failed_result(payload, item_exc))
        ok_count = sum(1 for item in items if item.status == "ok")
        data = FileParseBatchData(
            summary=FileParseBatchSummary(
                requested=len(items),
                ok=ok_count,
                failed=len(items) - ok_count,
                partial_success=0 < ok_count < len(items),
            ),
            items=items,
        )
    except ParseInputError as exc:
        return create_standard_response(
            data={"error": {"code": exc.code, "detail": exc.detail}},
            code=exc.status_code,
            message=exc.detail,
        )
    except Exception as exc:
        logger.exception("batch file parse failed: %s", exc)
        detail = "批量解析失败，请稍后重试或联系服务维护人员。"
        return create_standard_response(
            data={"error": {"code": "internal_error", "detail": detail}},
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message=detail,
        )
    finally:
        for upload in files:
            await upload.close()

    return create_standard_response(
        data=data.model_dump(),
        message=f"批量文件解析完成，成功 {data.summary.ok}/{data.summary.requested}",
    )
