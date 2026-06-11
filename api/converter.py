import pypandoc
import asyncio
import os
import uuid
import re
import requests
import shutil
import subprocess
import sys
from pathlib import Path
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Optional

from utils.settings import settings as _settings  # noqa: E402  (settings 单点入口)

# --- 基础目录配置（默认值与历史硬编码一致；可通过 .env 中 STATIC_DIR / CONVERTER_* 覆盖）---
STATIC_DIR_NAME = _settings.STATIC_DIR
GENERATED_FILES_SUBDIR = _settings.CONVERTER_GENERATED_FILES_SUBDIR
TEMP_IMAGES_SUBDIR = _settings.CONVERTER_TEMP_IMAGES_SUBDIR  # 这是所有临时图片目录的根目录
DOC_TEMP_SUBDIR = _settings.CONVERTER_DOC_TEMP_SUBDIR

# 确保基础目录存在
_STATIC_ROOT = Path(_settings.static_dir_abs)
STATIC_FILES_PATH = _STATIC_ROOT / GENERATED_FILES_SUBDIR
TEMP_IMAGES_ROOT_PATH = _STATIC_ROOT / TEMP_IMAGES_SUBDIR
DOC_TEMP_ROOT_PATH = _STATIC_ROOT / DOC_TEMP_SUBDIR
os.makedirs(STATIC_FILES_PATH, exist_ok=True)
os.makedirs(TEMP_IMAGES_ROOT_PATH, exist_ok=True)
os.makedirs(DOC_TEMP_ROOT_PATH, exist_ok=True)

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DOC_UPLOAD_BYTES = _settings.CONVERTER_DOC_MAX_UPLOAD_MB * 1024 * 1024
DOC_CONVERT_TIMEOUT_SEC = _settings.CONVERTER_DOC_CONVERT_TIMEOUT_SEC


# ... (Pydantic 模型定义部分保持不变) ...
class MarkdownRequest(BaseModel):
    md_text: str = Field(..., example="# 示例文档...", description="要转换的 Markdown 格式字符串")
    output_filename: str = Field("document.docx", example="人才培养方案.docx", description="输出的 Word 文件名")

    @field_validator('output_filename')
    def validate_filename(cls, v):
        if not v.endswith('.docx'):
            return f"{v}.docx"
        return v


class FileDetail(BaseModel):
    dify_model_identity: str = "__dify__file__"
    id: Any = None
    tenant_id: str
    type: str = "document"
    transfer_method: str = "tool_file"
    remote_url: Any = None
    related_id: str
    filename: str
    extension: str
    mime_type: str = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    size: int
    url: str


class DifyResponse(BaseModel):
    text: str = ""
    files: List[FileDetail]
    json_data: List[Dict[str, List]] = Field(default=[{"data": []}], alias="json")


router = APIRouter()


# ... (preprocess_markdown 和 run_pandoc_conversion 辅助函数保持不变) ...
def preprocess_markdown(text: str) -> str:
    lines = text.split('\n')
    processed_lines = []
    is_first_title_processed = False
    for line in lines:
        stripped_line = line.strip()
        if not is_first_title_processed and stripped_line.startswith('**') and stripped_line.endswith('**'):
            processed_lines.append(f'# {stripped_line[2:-2]}')
            is_first_title_processed = True;
            continue
        if re.match(r'^\*\*[一二三四五六七八九十]+、.*?\*\*$', stripped_line) or re.match(r'^\*\*附：.*?\*\*$',
                                                                                         stripped_line):
            processed_lines.append(f'## {stripped_line[2:-2]}');
            continue
        if re.match(r'^\*\*（[一二三四五六七八九十]+）.*?\*\*$', stripped_line) or re.match(r'^\*\*\d+\.\s.*?\*\*$',
                                                                                          stripped_line):
            processed_lines.append(f'### {stripped_line[2:-2]}');
            continue
        processed_lines.append(line)
    return '\n'.join(processed_lines)


def run_pandoc_conversion(markdown_str: str, output_path: str):
    try:
        pypandoc.convert_text(markdown_str, 'docx', format='md', outputfile=output_path)
    except OSError as e:
        raise RuntimeError(f"Pandoc 执行错误: {e}. 请确保 Pandoc 已正确安装并位于系统 PATH 中。")
    except Exception as e:
        raise RuntimeError(f"Markdown 转换失败: {e}")


def _find_soffice_executable() -> Optional[str]:
    configured_path = _settings.CONVERTER_SOFFICE_PATH
    if configured_path and Path(configured_path).is_file():
        return configured_path

    for name in ("soffice", "libreoffice"):
        resolved = shutil.which(name)
        if resolved:
            return resolved

    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "LibreOffice" / "program" / "soffice.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "LibreOffice" / "program" / "soffice.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _convert_doc_with_soffice(input_path: Path, output_dir: Path) -> Path:
    soffice = _find_soffice_executable()
    if not soffice:
        raise RuntimeError(
            "未找到 LibreOffice/soffice。请安装 LibreOffice，或通过 CONVERTER_SOFFICE_PATH 指定 soffice 路径。"
        )

    profile_dir = output_dir / "lo_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        soffice,
        "--headless",
        f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
        "--convert-to",
        "docx",
        "--outdir",
        str(output_dir),
        str(input_path),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=str(output_dir),
            capture_output=True,
            text=True,
            timeout=DOC_CONVERT_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"LibreOffice 转换超时（{DOC_CONVERT_TIMEOUT_SEC} 秒）") from exc

    output_path = output_dir / f"{input_path.stem}.docx"
    if result.returncode != 0 or not output_path.is_file():
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"LibreOffice 转换失败: {detail or '未生成 docx 文件'}")
    return output_path


def _convert_doc_with_word(input_path: Path, output_path: Path) -> Path:
    if sys.platform != "win32":
        raise RuntimeError("Microsoft Word COM 转换仅支持 Windows 环境。")

    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as exc:
        raise RuntimeError("未安装 pywin32，无法调用 Microsoft Word COM 转换。") from exc

    pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(str(input_path.resolve()), ReadOnly=True, AddToRecentFiles=False)
        doc.SaveAs2(str(output_path.resolve()), FileFormat=16)
    finally:
        if doc is not None:
            doc.Close(False)
        if word is not None:
            word.Quit()
        pythoncom.CoUninitialize()

    if not output_path.is_file():
        raise RuntimeError("Microsoft Word 转换失败：未生成 docx 文件。")
    return output_path


def convert_legacy_doc_to_docx(input_path: Path, output_dir: Path) -> Path:
    try:
        return _convert_doc_with_soffice(input_path, output_dir)
    except RuntimeError as soffice_error:
        output_path = output_dir / f"{input_path.stem}.docx"
        try:
            return _convert_doc_with_word(input_path, output_path)
        except RuntimeError as word_error:
            raise RuntimeError(f"{soffice_error}；Word 备用转换也失败: {word_error}") from word_error


async def _save_doc_upload(upload: UploadFile, dest: Path) -> int:
    size = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_DOC_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件过大，最大支持 {_settings.CONVERTER_DOC_MAX_UPLOAD_MB}MB。",
                )
            f.write(chunk)
    return size


def _docx_download_name(original_name: str) -> str:
    stem = Path(original_name).stem.strip() or "document"
    stem = re.sub(r'[\\/:*?"<>|\r\n]+', "_", stem).strip(" ._") or "document"
    return f"{stem}.docx"


async def cleanup_resources(paths_to_delete: List[Path], delay: int = 600):
    """后台任务：在延迟后，安全地删除指定的文件和目录。"""
    await asyncio.sleep(delay)
    for path in paths_to_delete:
        try:
            if path.is_file():
                path.unlink()
                print(f"Cleaned up file: {path}")
            elif path.is_dir():
                shutil.rmtree(path)
                print(f"Cleaned up directory: {path}")
        except Exception as e:
            print(f"Error during cleanup of {path}: {e}")


def download_images_and_update_md(md_text: str, dedicated_temp_dir: Path) -> str:
    """查找、下载图片, 并用本地绝对路径替换 URL。所有图片都保存在专用的临时目录中。"""
    image_pattern = re.compile(r'!\[.*?\]\((https?://[^\s)]+)\)')
    urls = image_pattern.findall(md_text)
    updated_md = md_text

    for url in set(urls):
        try:
            response = requests.get(url, stream=True, timeout=15)
            response.raise_for_status()

            file_ext = Path(url.split('?')[0]).suffix or '.png'
            local_filename = f"{uuid.uuid4()}{file_ext}"
            local_path = dedicated_temp_dir / local_filename

            with open(local_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            absolute_path_str = str(local_path.resolve())
            updated_md = updated_md.replace(url, absolute_path_str)
        except requests.exceptions.RequestException as e:
            print(f"Warning: Failed to download image {url}. Error: {e}. Skipping.")

    return updated_md


_DOC_TO_DOCX_RESPONSES = {
    200: {
        "description": "成功：返回转换后的 docx 文件流。",
        "content": {
            DOCX_MEDIA_TYPE: {
                "schema": {
                    "type": "string",
                    "format": "binary",
                    "description": "完整 docx 文件字节",
                }
            }
        },
    },
    400: {"description": "上传文件不是 .doc"},
    413: {"description": "上传文件超过大小限制"},
    500: {"description": "转换失败或服务器缺少 LibreOffice/Microsoft Word 转换能力"},
}


@router.post(
    "/doc_to_docx",
    summary="将旧版 .doc 文件转换为 .docx 并返回文件流",
    description=(
        "**Body**：`multipart/form-data`，字段名为 **`file`**，只接受旧版 `.doc` 文件。\n"
        "服务端优先调用 LibreOffice/soffice 转换；Windows 环境下如果 LibreOffice 不可用，会尝试 Microsoft Word COM。"
    ),
    response_class=FileResponse,
    responses=_DOC_TO_DOCX_RESPONSES,
)
async def convert_doc_to_docx(
        background_tasks: BackgroundTasks,
        file: UploadFile = File(..., description="旧版 Word 文档，扩展名必须为 .doc"),
):
    original_name = file.filename or "document.doc"
    if Path(original_name).suffix.lower() != ".doc":
        raise HTTPException(status_code=400, detail="仅支持旧版 .doc 文件，请不要上传 .docx 或其他格式。")

    request_dir = DOC_TEMP_ROOT_PATH / str(uuid.uuid4())
    input_path = request_dir / "input.doc"
    request_dir.mkdir(parents=True, exist_ok=True)

    try:
        await _save_doc_upload(file, input_path)
        output_path = await asyncio.to_thread(convert_legacy_doc_to_docx, input_path, request_dir)
        background_tasks.add_task(cleanup_resources, [request_dir], delay=_settings.CONVERTER_CLEANUP_DELAY_SEC)

        return FileResponse(
            path=str(output_path),
            media_type=DOCX_MEDIA_TYPE,
            filename=_docx_download_name(original_name),
        )
    except HTTPException:
        shutil.rmtree(request_dir, ignore_errors=True)
        raise
    except RuntimeError as e:
        shutil.rmtree(request_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        shutil.rmtree(request_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"doc 转 docx 失败: {e}")
    finally:
        await file.close()


@router.post(
    "/to_docx",
    response_model=DifyResponse,
    summary="将 Markdown 转换为 Word 并返回 Dify 格式的 JSON 响应（并发安全）"
)
async def convert_md_to_dify_format(
        payload: MarkdownRequest,
        request: Request,
        background_tasks: BackgroundTasks
):
    # [并发安全关键点 1]: 为每一次请求，创建一个唯一的、隔离的临时图片目录
    # 比如：./static/temp_images/2d3b2a5a-1b4f-4b9b-9e4a-5f0e1f4b8c7b/
    # 这样用户 A、B、C 的图片会分别存放在三个完全不同的目录中，互不影响。
    request_temp_image_dir = TEMP_IMAGES_ROOT_PATH / str(uuid.uuid4())
    os.makedirs(request_temp_image_dir, exist_ok=True)

    # [并发安全关键点 2]: Word 文件名也使用 UUID，确保唯一性
    # 避免用户 A 和 B 同时请求生成 'document.docx' 导致文件被覆盖。
    internal_filename = f"{uuid.uuid4()}.docx"
    docx_filepath = STATIC_FILES_PATH / internal_filename

    # 定义一个列表，用于收集本次请求产生的所有需要被清理的资源
    resources_to_cleanup = [request_temp_image_dir, docx_filepath]

    try:
        processed_md = preprocess_markdown(payload.md_text)

        # [并发安全关键点 3]: 所有图片都下载到上面创建的那个专属目录中
        updated_md = await asyncio.to_thread(
            download_images_and_update_md, processed_md, request_temp_image_dir
        )

        await asyncio.to_thread(run_pandoc_conversion, updated_md, str(docx_filepath))

        if not docx_filepath.exists():
            raise HTTPException(status_code=500, detail="文件转换失败，服务器未能生成输出文件。")

        file_size = docx_filepath.stat().st_size
        file_url = f"{str(request.base_url)}{STATIC_DIR_NAME}/{GENERATED_FILES_SUBDIR}/{internal_filename}"

        file_details = FileDetail(
            tenant_id=str(uuid.uuid4()),
            related_id=str(uuid.uuid4()),
            filename=payload.output_filename,  # 返回给用户的是他们期望的文件名
            extension=".docx",
            size=file_size,
            url=file_url
        )

        response_data = DifyResponse(files=[file_details])

        return response_data

    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # [并发安全关键点 4]: 清理任务只针对本次请求创建的专属路径
        # 例如，它会删除 A 请求的 './.../2d3b2a5a.../' 目录和 A 的 Word 文件。
        # 这个操作完全不会触碰到 B 请求的 './.../another-uuid/.../' 目录或 B 的文件。
        background_tasks.add_task(cleanup_resources, resources_to_cleanup, delay=_settings.CONVERTER_CLEANUP_DELAY_SEC)
