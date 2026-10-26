"""utils/log_utils.py - 纯函数工具：ANSI 清洗与时间戳。"""

import re
from datetime import datetime


def clean_ansi(text: str) -> str:
    """移除 ANSI 转义序列（颜色码、光标控制等）。"""
    return re.sub(r'\x1b\[\d+(;\d+)*m', '', text)


def timestamp() -> str:
    """当前时间，格式 HH:MM:SS。"""
    return datetime.now().strftime("%H:%M:%S")
