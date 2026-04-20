# -*- coding: utf-8 -*-
# @File：logger.py
# @Time：2025/1/21 11:00
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com

import logging
import os
import re
import sys
from logging.handlers import TimedRotatingFileHandler

from utils.settings import settings


def setup_module_logger(
    logger_name: str,
    log_file: str,
    when: str = 'midnight',
    backup_count: int | None = None,
) -> logging.Logger:
    """
    配置一个模块专用的 logger。

    - 日志总是会写入到指定的文件，并按时间自动滚动归档。
      默认按天滚动（每天 0 点），归档文件名形如 ``xxx.log.YYYY-MM-DD``，
      并自动保留最近 ``backup_count`` 个归档文件，超过会被自动删除。
    - [核心功能] 通过环境变量 `DISABLE_CONSOLE_LOG` 控制是否在终端输出。
      - 默认值为 'true'，即未设置时禁用终端输出（沿用历史行为，避免污染 nohup/uvicorn 输出）。
      - 设置为 'false', '0', 'no' 时会在终端输出，便于本地调试。
      - 设置为 'true', '1', 'yes' 时显式禁用终端输出。

    Args:
        logger_name: logger 名称，通常传入 ``__name__``。
        log_file:    日志文件路径。
        when:        滚动周期，传给 ``TimedRotatingFileHandler``。
                     常用值：'midnight'（每天 0 点）、'H'（每小时）、'S'（每秒，仅调试用）。
                     默认 'midnight'。
        backup_count: 保留的归档文件数量。按天滚动时即"保留多少天"。
                     未传值时取 ``settings.LOG_BACKUP_COUNT``（默认 90）。
    """
    logger = logging.getLogger(logger_name)

    if logger.hasHandlers():
        logger.handlers.clear()

    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)-8s - [%(name)s] [%(threadName)s] - %(message)s'
    )

    if backup_count is None:
        backup_count = settings.LOG_BACKUP_COUNT

    # 若 log_file 是相对路径，则相对 settings.LOG_DIR（再相对项目根）
    log_path = log_file
    if not os.path.isabs(log_path):
        base = settings.LOG_DIR
        if not os.path.isabs(base):
            base = os.path.join(str(settings.project_root), base)
        # 既兼容 "logs/audio/x.log" 这种已带 logs/ 前缀的写法，又兼容 "audio/x.log"
        if log_file.replace("\\", "/").startswith(settings.LOG_DIR.replace("\\", "/") + "/"):
            log_path = os.path.join(str(settings.project_root), log_file)
        else:
            log_path = os.path.join(base, log_file)

    # --- 1. 文件日志 Handler (始终启用，按时间滚动) ---
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        filename=log_path,
        when=when,
        interval=1,
        backupCount=backup_count,
        encoding='utf-8',
        delay=False,
        utc=False,
    )
    # 让归档文件名形如 fish.log.2026-04-20，并让 backupCount 清理逻辑能正确识别这些文件。
    # 不显式设置 extMatch 时，Python 默认正则在某些版本下匹配不到自定义 suffix，
    # 会导致旧文件无法被自动清理。
    if when == 'midnight' or when.upper().startswith('D'):
        file_handler.suffix = "%Y-%m-%d"
        file_handler.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}(\.\w+)?$")
    elif when.upper() == 'H':
        file_handler.suffix = "%Y-%m-%d_%H"
        file_handler.extMatch = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}(\.\w+)?$")
    # 其他取值（如 'S'、'M'）保持 TimedRotatingFileHandler 默认行为，便于调试验证。

    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)

    # --- 2. 终端日志 Handler (根据 settings.LOG_DISABLE_CONSOLE 决定是否启用) ---
    # 默认 True（沿用历史行为：禁用终端输出，避免污染 nohup/uvicorn 输出）
    # 仍兼容老的 DISABLE_CONSOLE_LOG 环境变量直接覆盖
    legacy = os.getenv('DISABLE_CONSOLE_LOG')
    if legacy is not None:
        disable_console = legacy.lower() in ('true', '1', 'yes')
    else:
        disable_console = settings.LOG_DISABLE_CONSOLE

    if not disable_console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(log_formatter)
        logger.addHandler(stream_handler)

    return logger
