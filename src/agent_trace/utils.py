"""通用工具：id 生成、时间格式化、结构化序列化。"""
from __future__ import annotations

import json
from typing import Any

from .schemas import generate_id, generate_message_id, iso_timestamp, utc_now

__all__ = [
    "generate_id",
    "generate_message_id",
    "iso_timestamp",
    "utc_now",
    "stringify",
]


def stringify(value: Any) -> str:
    """把任意值转成字符串；非字符串走 JSON 序列化，失败则退化为 str()。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
