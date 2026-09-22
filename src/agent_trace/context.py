"""contextvars 实现 trace 上下文传播，支持 async 与并发任务。

业务方在 `Tracer.turn()` 里埋点，子协程 / 子线程可以拿到当前 turn，
无需层层传参。contextvars 在 asyncio 任务间自动隔离，不会串线。
"""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .tracer import Turn

# 当前 turn
_current_turn: contextvars.ContextVar[Optional["Turn"]] = contextvars.ContextVar(
    "agent_trace_current_turn", default=None
)

# agent 级 baggage，随 trace 一起带上，便于业务方透传租户 / 用户等信息
_baggage: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "agent_trace_baggage", default={}
)


def get_current_turn() -> Optional["Turn"]:
    """返回当前上下文里的 Turn，没有则 None。"""
    return _current_turn.get()


def set_current_turn(turn: Optional["Turn"]) -> contextvars.Token:
    """设置当前 Turn，返回 token 供 reset 用。"""
    return _current_turn.set(turn)


def reset_current_turn(token: contextvars.Token) -> None:
    """恢复到设置前的状态。"""
    _current_turn.reset(token)


def get_baggage() -> Dict[str, Any]:
    """返回当前上下文的 baggage 副本。"""
    return dict(_baggage.get())


def set_baggage(**items: Any) -> contextvars.Token:
    """合并写入 baggage，返回 token。"""
    merged = dict(_baggage.get())
    merged.update(items)
    return _baggage.set(merged)


def reset_baggage(token: contextvars.Token) -> None:
    _baggage.reset(token)
