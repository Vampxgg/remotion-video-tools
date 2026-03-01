# -*- coding: utf-8 -*-
# @File：logger.py
# @Time：2025/1/21 11:00
# @Author：_不咬闰土的猹丶
# @email：hx1561958968@gmail.com

import logging
import os
import sys


def setup_module_logger(logger_name: str, log_file: str) -> logging.Logger:
    """
    配置一个模块专用的 logger。

    - 日志总是会写入到指定的文件。
    - [核心功能] 通过环境变量 `DISABLE_CONSOLE_LOG` 控制是否在终端输出。
      - 如果未设置或值为 'false', '0', 'no'，则会在终端输出。
      - 如果设置为 'true', '1', 'yes'，则会禁用终端输出。
    """
    logger = logging.getLogger(logger_name)

    if logger.hasHandlers():
        logger.handlers.clear()

    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)-8s - [%(name)s] [%(threadName)s] - %(message)s'
    )

    # --- 1. 文件日志 Handler (始终启用) ---
    log_dir = os.path.dirname(log_file)
    os.makedirs(log_dir, exist_ok=True)

    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)

    # --- 2. 终端日志 Handler (根据环境变量决定是否启用) ---
    # 读取环境变量，默认为 'false'
    disable_console_str = os.getenv('DISABLE_CONSOLE_LOG', 'true').lower()

    # 判断环境变量的值是否表示 "禁用"
    if disable_console_str not in ('true', '1', 'yes'):
        # 如果不禁用，则添加 StreamHandler
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(log_formatter)
        logger.addHandler(stream_handler)

    return logger
