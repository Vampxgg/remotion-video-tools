# -*- coding: utf-8 -*-
"""/file/understand 异步任务的轻量级任务存储。

为什么需要它：多模态理解单文件可能耗时数分钟（视觉理解 + 区域重试），若用同步 HTTP
长连接，前置 nginx/网关的 ~120s 读超时会把请求掐断（详见排障报告）。改为
「提交即返回 job_id + 轮询结果」后，每个 HTTP 请求都很短，真正的耗时放后台跑，
彻底绕开单请求超时上限。

实现要点：
- 基于「每个 job 一个 JSON 文件 + 原子替换」的文件存储，故而同主机多 worker 间
  也能读到状态（处理只在受理该提交的 worker 上进行，其它 worker 仍可读结果）。
- 仅依赖标准库，无需引入 redis 等外部依赖。
- 访问时顺带清理过期文件（best-effort）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from utils.logger import setup_module_logger
from utils.settings import settings as _settings

logger = setup_module_logger(__name__, "logs/file/file_understand_jobs.log")

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
_TERMINAL = {STATUS_SUCCEEDED, STATUS_FAILED}


def _job_dir() -> Path:
    raw = (getattr(_settings, "FILE_UNDERSTAND_JOB_DIR", None) or "").strip()
    base = Path(raw) if raw else Path(tempfile.gettempdir()) / "file_understand_jobs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _path(job_id: str) -> Path:
    # 仅允许 hex job_id，避免路径穿越。
    safe = "".join(c for c in job_id if c in "0123456789abcdef")
    return _job_dir() / f"{safe}.json"


def _atomic_write(path: Path, record: Dict[str, Any]) -> None:
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取任务文件失败 {path.name}: {e}")
        return None


def _ttl_sec() -> int:
    return int(getattr(_settings, "FILE_UNDERSTAND_JOB_TTL_SEC", 86400) or 86400)


def cleanup_expired() -> None:
    """删除超过 TTL 的任务文件；best-effort，不抛错。"""
    ttl = _ttl_sec()
    now = time.time()
    try:
        for p in _job_dir().glob("*.json"):
            try:
                if now - p.stat().st_mtime > ttl:
                    p.unlink()
            except OSError:
                continue
    except Exception as e:  # noqa: BLE001
        logger.debug(f"清理过期任务失败: {e}")


def create_job(filename: str) -> str:
    job_id = uuid.uuid4().hex
    now = time.time()
    record = {
        "job_id": job_id,
        "status": STATUS_PENDING,
        "filename": filename,
        "created_at": now,
        "updated_at": now,
        "result": None,
        "error": None,
    }
    _atomic_write(_path(job_id), record)
    cleanup_expired()
    logger.info(f"[{job_id}] 创建任务 file={filename!r}")
    return job_id


def _update(job_id: str, **patch: Any) -> None:
    path = _path(job_id)
    record = _read(path)
    if record is None:
        # 任务文件丢失（被清理/未创建）：用补丁重建一条最小记录，避免轮询拿不到状态。
        record = {"job_id": job_id, "created_at": time.time(), "result": None, "error": None}
    record.update(patch)
    record["updated_at"] = time.time()
    _atomic_write(path, record)


def mark_running(job_id: str) -> None:
    _update(job_id, status=STATUS_RUNNING)
    logger.info(f"[{job_id}] 开始处理")


def mark_succeeded(job_id: str, result: Dict[str, Any]) -> None:
    _update(job_id, status=STATUS_SUCCEEDED, result=result, error=None)
    logger.info(f"[{job_id}] 处理成功")


def mark_failed(job_id: str, code: str, detail: str) -> None:
    _update(job_id, status=STATUS_FAILED, result=None, error={"code": code, "detail": detail})
    logger.warning(f"[{job_id}] 处理失败 code={code} detail={detail}")


def read_job(job_id: str) -> Optional[Dict[str, Any]]:
    return _read(_path(job_id))
