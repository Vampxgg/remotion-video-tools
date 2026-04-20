import pypandoc
import asyncio
import os
import uuid
import re
import requests
import shutil
from pathlib import Path
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Tuple

from utils.settings import settings as _settings  # noqa: E402  (settings 单点入口)

# --- 基础目录配置（默认值与历史硬编码一致；可通过 .env 中 STATIC_DIR / CONVERTER_* 覆盖）---
STATIC_DIR_NAME = _settings.STATIC_DIR
GENERATED_FILES_SUBDIR = _settings.CONVERTER_GENERATED_FILES_SUBDIR
TEMP_IMAGES_SUBDIR = _settings.CONVERTER_TEMP_IMAGES_SUBDIR  # 这是所有临时图片目录的根目录

# 确保基础目录存在
STATIC_FILES_PATH = Path(STATIC_DIR_NAME) / GENERATED_FILES_SUBDIR
TEMP_IMAGES_ROOT_PATH = Path(STATIC_DIR_NAME) / TEMP_IMAGES_SUBDIR
os.makedirs(STATIC_FILES_PATH, exist_ok=True)
os.makedirs(TEMP_IMAGES_ROOT_PATH, exist_ok=True)


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
