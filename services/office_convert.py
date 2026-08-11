# -*- coding: utf-8 -*-
"""LibreOffice: office 文档 -> PDF 的通用转换（同步，需在线程池调用）。

从 file_understand_service 迁出，成为不依赖多模态链路的通用工具，供元素级视觉
（表格 bbox 裁剪需要页坐标）与 document_manifest_service 共用，避免循环依赖。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from utils.settings import settings as _settings

_OFFICE_EXTS = {".docx", ".doc", ".pptx", ".ppt"}


def find_soffice() -> Optional[str]:
    """定位 LibreOffice/soffice 可执行文件；找不到返回 None。"""
    configured = _settings.CONVERTER_SOFFICE_PATH
    if configured and Path(configured).is_file():
        return configured
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


def office_bytes_to_pdf(content: bytes, ext: str) -> bytes:
    """用 LibreOffice 把 office 文档字节转成 PDF 字节（同步，需在线程池调用）。"""
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError(
            "未找到 LibreOffice/soffice，无法将 office 文档转 PDF；"
            "请安装 LibreOffice 或配置 CONVERTER_SOFFICE_PATH。"
        )
    timeout = _settings.CONVERTER_DOC_CONVERT_TIMEOUT_SEC
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / f"input{ext}"
        input_path.write_bytes(content)
        profile_dir = tmp_path / "lo_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            soffice,
            "--headless",
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(tmp_path),
            str(input_path),
        ]
        result = subprocess.run(
            command,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output_path = tmp_path / "input.pdf"
        if result.returncode != 0 or not output_path.is_file():
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"LibreOffice 转 PDF 失败: {detail or '未生成 pdf 文件'}")
        return output_path.read_bytes()
