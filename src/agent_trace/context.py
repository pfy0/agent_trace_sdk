"""contextvars 实现 trace 上下文传播，支持 async 与并发任务。

业务方在 `Tracer.turn()` 里埋点，子协程 / 子线程可以拿到当前 turn，
无需层层传参。contextvars 在 asyncio 任务间自动隔离，不会串线。
"""
from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .tracer import Turn

# 当前 turn
_current_turn: contextvars.ContextVar[Optional["Turn"]] = contextvars.ContextVar(
    "agent_trace_current_turn", default=None
)

# 当前 span 栈（不可变元组，栈顶在末尾）。用 contextvar 而非实例属性，
# 保证 asyncio 并发任务 / 线程各持一份拷贝，span 层级不会互相污染。
_span_stack: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "agent_trace_span_stack", default=()
)


def get_current_turn() -> Optional["Turn"]:
    """返回当前上下文里的 Turn，没有则 None。"""
    return _current_turn.get()


def set_current_turn(turn: Optional["Turn"]) -> contextvars.Token:
    """设置当前 Turn，返回 token 供 reset 用。"""
    return _current_turn.set(turn)


def reset_current_turn(token: contextvars.Token) -> None:
    """恢复到设置前的状态。

    token 必须与 set 时处于同一个 context。LangGraph 等框架会把
    `before_*` / `after_*` 钩子调度到不同 context 执行，此时 reset 会抛
    ValueError；这种跨 context 的情况按"尽力恢复"处理——该 context 本就是
    临时的，重新绑定一个显式值比抛异常更安全。
    """
    try:
        _current_turn.reset(token)
    except ValueError:
        _current_turn.set(None)


def get_span_stack() -> tuple:
    """返回当前 span 栈（元组，栈顶在末尾）。"""
    return _span_stack.get()


def get_current_span_id() -> Optional[str]:
    """返回当前 span_id，没有则 None（即顶层）。"""
    stack = _span_stack.get()
    return stack[-1] if stack else None


def push_span(span_id: str) -> contextvars.Token:
    """把 span_id 压栈，返回 token 供 pop 用。"""
    return _span_stack.set(_span_stack.get() + (span_id,))


def reset_span(token: contextvars.Token) -> None:
    """恢复到压栈前的状态；跨 context 时按尽力恢复处理（见 reset_current_turn）。"""
    try:
        _span_stack.reset(token)
    except ValueError:
        _span_stack.set(())
