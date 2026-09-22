"""离线导入：把多轮 messages 数据按 turn_id 切分成多条 trace。

用于「批量导入历史数据」的场景；实时采集请用 `Tracer.turn()`。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .schemas import Trace


def _extract_steps(payload: Any, depth: int = 0, max_depth: int = 8) -> Optional[List[Dict[str, Any]]]:
    """在任意嵌套结构里定位 step 列表，兼容 conversation / messages 键名。"""
    if depth > max_depth:
        return None
    if isinstance(payload, list):
        if payload and all(isinstance(item, dict) and "message_type" in item for item in payload):
            return list(payload)
        return None
    if isinstance(payload, dict):
        for key in ("conversation", "messages"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return list(inner)
        for value in payload.values():
            found = _extract_steps(value, depth + 1, max_depth)
            if found is not None:
                return found
    return None


def split_by_turn(
    payload: Any,
    session_id: Optional[str] = None,
    **trace_meta: Any,
) -> List[Trace]:
    """多轮数据按 turn_id 切分成多条 trace。

    step 里必须带 turn_id；缺 turn_id 的消息会被跳过（无法归属到任何轮次）。
    切出的 trace 按首次出现的顺序排列。
    """
    steps = _extract_steps(payload)
    if steps is None:
        raise ValueError("未在 payload 中找到 conversation / messages 数组")

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        turn_id = raw.pop("turn_id", None)
        if turn_id is None:
            continue
        if turn_id not in buckets:
            buckets[turn_id] = []
            order.append(turn_id)
        buckets[turn_id].append(raw)

    if not order:
        raise ValueError("消息里没有 turn_id，无法按轮次切分")

    traces: List[Trace] = []
    for turn_id in order:
        steps = buckets[turn_id]
        # 同一 turn 内第 2 条及以后的 user_query 自动标记为追问
        seen_user_query = False
        for step in steps:
            if step.get("message_type") == "user_query":
                if seen_user_query and "is_follow_up" not in step:
                    step["is_follow_up"] = True
                seen_user_query = True
        traces.append(
            Trace.from_raw(
                {
                    "turn_id": turn_id,
                    "session_id": session_id or turn_id,
                    "conversation": steps,
                    **trace_meta,
                }
            )
        )
    return traces
