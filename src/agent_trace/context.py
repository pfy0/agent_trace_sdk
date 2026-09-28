"""contextvars 实现 trace 上下文传播。

业务方在 `Tracer.turn()` 里埋点后，**同一线程内**可隐式取到当前 turn，
无需层层传参；asyncio 子任务在创建时会自动复制当前 Context，因此也能取到，
且任务间互相隔离、不会串线。

**跨线程不传播**：`threading.Thread` / 线程池启动的新线程使用独立的空 Context，
读不到调用线程设置的 turn（`contextvars` 的设计语义，非缺陷）。因此
`Tracer.step` / `Tracer.trace` 装饰器若在线程池等**子线程**里执行，`get_current_turn()`
会返回 `None`，埋点将被跳过。需要在线程里采集时，请**显式传递 `Turn`**（用
`turn.record_*`），或在线程入口用 `contextvars.copy_context().run(...)` 手动带上
Context。
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

def get_current_turn() -> Optional["Turn"]:
    """返回**当前线程**上下文里的 Turn，没有则 None。

    注意：`contextvars` 不会自动传播到新线程，子线程 / 线程池里调用本函数
    通常得到 None（asyncio 子任务则会自动继承）。详见模块文档。
    """
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
