"""采集层：Tracer / Turn / Step 的生命周期与手动埋点 API（模式①）。

用法：

    tracer = Tracer(agent_name="news-daily", env="prod")
    with tracer.turn(turn_id="t-1", session_id="s-1") as turn:
        turn.record_user_query("查看今日ai新闻热点")
        turn.record_reasoning("先搜索今日 AI 新闻……")
        turn.record_tool_call(name="web_search", arguments={"count": 8})
        turn.record_tool_response(tool_call_id="call_1", content="{...}")
        turn.record_final_answer("今日要闻……")

退出 with 时自动：分配 step_no/message_id、计算 duration_ms、校验成 Trace、
交给导出器。异常路径也会落一条终止 step 并把 status 置为 error。

span 树
-------
每个 step 都会自动分配 `span_id`，并按当前嵌套层级填 `parent_span_id`，
形成父子树。同一 turn 内的所有 step 共用一条 trace，跨 agent 调用（子 agent、
子工具链）用嵌套 span 表达，而不是另开 trace —— 与 OTel / Langfuse 的惯例一致。

    with turn.span("agent", name="researcher") as parent:   # 父 span
        with turn.span("tool", name="web_search"):          # 子 span，parent_span_id 指向父
            ...

`with turn.step(...)` 与各 `record_*` 方法同样参与 span 栈：在 `turn.span(...)`
内部记录的 step 会自动挂到当前 span 下。
"""
from __future__ import annotations

import functools
import inspect
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

from . import context as ctx
from .config import Config
from .schemas import (
    MESSAGE_TYPE_META,
    MessageType,
    Status,
    Step,
    ToolCall,
    Trace,
    generate_id,
    generate_message_id,
    iso_timestamp,
    utc_now,
)


def _as_tool_calls(spec: Any) -> List[ToolCall]:
    """把多种入参形态统一成 ToolCall 列表。

    支持：
    - ToolCall / dict（OpenAI 风格）
    - (name, arguments) 元组
    - {"name": ..., "arguments": ...}
    - 上述任意组合的列表
    """
    if spec is None:
        return []
    if isinstance(spec, (ToolCall, dict)) or isinstance(spec, tuple):
        items: Sequence[Any] = [spec]
    elif isinstance(spec, Iterable):
        items = list(spec)
    else:
        items = [spec]

    calls: List[ToolCall] = []
    for item in items:
        if isinstance(item, ToolCall):
            calls.append(item)
            continue
        if isinstance(item, tuple) and len(item) == 2:
            name, arguments = item
            calls.append(
                ToolCall(
                    id=generate_id("call"),
                    type="function",
                    function={"name": name, "arguments": arguments},
                )
            )
            continue
        if isinstance(item, str):
            # 简写：字符串按工具名处理，参数为空
            calls.append(
                ToolCall(id=generate_id("call"), type="function", function={"name": item, "arguments": {}})
            )
            continue
        if isinstance(item, dict):
            if "id" in item and "function" in item:
                calls.append(ToolCall.model_validate(item))
                continue
            name = item.get("name")
            arguments = item.get("arguments")
            calls.append(
                ToolCall(
                    id=item.get("id") or generate_id("call"),
                    type=item.get("type", "function"),
                    function={"name": name, "arguments": arguments},
                )
            )
            continue
        raise TypeError("无法识别的 tool_calls 入参: %r" % (item,))
    return calls


class Turn:
    """一轮交互的采集句柄。由 `Tracer.turn(...)` 创建，不直接实例化。"""

    def __init__(
        self,
        tracer: "Tracer",
        turn_id: str,
        session_id: str,
        status: Optional[str] = None,
        model: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        meta: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> None:
        self.tracer = tracer
        self.turn_id = turn_id
        self.session_id = session_id
        self.model = model
        self.tags = dict(tags or {})
        self.meta = dict(meta or {})
        self.extra = dict(extra)

        self.status = Status(status or tracer.config.default_status)
        self.error: Optional[str] = None
        self.started_at = utc_now()
        self.ended_at: Optional[str] = None

        self._steps: List[Step] = []
        self._seq: Dict[MessageType, int] = {}
        self._tokens: List[tuple] = []
        self._committed = False
        self._lock = threading.Lock()

    # ---------- 内部：id 与 step 追加 ----------

    def _next_message_id(self, message_type: MessageType) -> str:
        seq = self._seq.get(message_type, 0) + 1
        self._seq[message_type] = seq
        return generate_message_id(message_type, seq)

    def _append(
        self,
        message_type: MessageType,
        content: Optional[str],
        timestamp: Optional[str],
        message_id: Optional[str] = None,
        latency_ms: Optional[int] = None,
        tool_calls: Optional[List[ToolCall]] = None,
        tool_call_id: Optional[str] = None,
        status: Optional[str] = None,
        error: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        model: Optional[str] = None,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        meta: Optional[Dict[str, Any]] = None,
        is_follow_up: Optional[bool] = None,
    ) -> Step:
        if self._committed:
            raise RuntimeError("该 turn 已提交，不能再追加 step")
        role = MESSAGE_TYPE_META[message_type][1]
        resolved_id = message_id or self._next_message_id(message_type)
        # span 树：未显式指定时，span_id 新分配、parent 取当前上下文里的栈顶
        if span_id is None:
            span_id = generate_id("span")
        if parent_span_id is None:
            parent_span_id = ctx.get_current_span_id()
        step = Step(
            message_id=resolved_id,
            role=role,
            message_type=message_type,
            content=content,
            timestamp=timestamp or iso_timestamp(),
            step_no=len(self._steps) + 1,
            latency_ms=latency_ms,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            status=Status(status) if status else None,
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            span_id=span_id,
            parent_span_id=parent_span_id,
            tags=tags,
            meta=meta,
            is_follow_up=is_follow_up,
        )
        with self._lock:
            self._steps.append(step)
        return step

    # ---------- 手动埋点 API ----------

    def record_user_query(self, content: str, follow_up: bool = False, **kwargs: Any) -> Step:
        """记录用户输入。同一 turn 内的第 2 条起属于追问/补充，可传 follow_up=True 标记。"""
        if follow_up and "is_follow_up" not in kwargs:
            kwargs["is_follow_up"] = True
        return self._append(MessageType.USER_QUERY, content, kwargs.pop("timestamp", None), **kwargs)

    def record_reasoning(self, content: str, **kwargs: Any) -> Step:
        return self._append(MessageType.REASONING, content, kwargs.pop("timestamp", None), **kwargs)

    def record_system_prompt(self, content: str, **kwargs: Any) -> Step:
        return self._append(MessageType.SYSTEM_PROMPT, content, kwargs.pop("timestamp", None), **kwargs)

    def record_tool_call(self, tool_calls: Any = None, **kwargs: Any) -> Step:
        calls = _as_tool_calls(tool_calls if tool_calls is not None else kwargs.pop("calls", None))
        if not calls:
            raise ValueError("record_tool_call 需要至少一个工具调用")
        return self._append(
            MessageType.TOOL_CALL,
            None,
            kwargs.pop("timestamp", None),
            tool_calls=calls,
            **kwargs,
        )

    def record_tool_response(
        self, tool_call_id: str, content: Optional[str] = None, **kwargs: Any
    ) -> Step:
        return self._append(
            MessageType.TOOL_RESPONSE,
            content,
            kwargs.pop("timestamp", None),
            tool_call_id=tool_call_id,
            **kwargs,
        )

    def record_final_answer(self, content: str, **kwargs: Any) -> Step:
        return self._append(MessageType.FINAL_ANSWER, content, kwargs.pop("timestamp", None), **kwargs)

    # ---------- 步骤上下文管理器：自动计时 ----------

    def step(self, message_type: Union[MessageType, str], **kwargs: Any) -> "_StepContext":
        return _StepContext(self, MessageType(message_type), kwargs)

    def span(self, kind: str = "agent", name: Optional[str] = None, **kwargs: Any) -> "_SpanContext":
        """开启一个嵌套 span，内部记录的 step / 子 span 都挂到它下面。

        kind 为自由文本标签（如 agent / tool / llm / retrieval），供下游区分层级用途；
        name 为该 span 的名称（如子 agent 名、工具名）。
        """
        return _SpanContext(self, kind=kind, name=name, kwargs=kwargs)

    def record_error(self, error: str, content: Optional[str] = None, **kwargs: Any) -> Step:
        """落一条终止 step，并把整个 turn 标记为 error。"""
        self.status = Status.ERROR
        self.error = error
        return self._append(
            MessageType.FINAL_ANSWER,
            content,
            kwargs.pop("timestamp", None),
            status=Status.ERROR.value,
            error=error,
            **kwargs,
        )

    # ---------- 生命周期 ----------

    def __enter__(self) -> "Turn":
        token = ctx.set_current_turn(self)
        self._tokens.append(("turn", token))
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and self.status is not Status.ERROR:
            self.status = Status.ERROR
            self.error = self.error or ("%s: %s" % (exc_type.__name__, exc))
            # 异常路径：若还没有终止 step，补一条 error 的 final_answer
            if not self._steps or self._steps[-1].message_type is not MessageType.FINAL_ANSWER:
                self._append(
                    MessageType.FINAL_ANSWER,
                    None,
                    None,
                    status=Status.ERROR.value,
                    error=self.error,
                )
        for kind, token in reversed(self._tokens):
            if kind == "turn":
                ctx.reset_current_turn(token)
        self._tokens.clear()
        self.commit()
        return False  # 不吞异常

    def as_trace(self) -> Trace:
        """把当前采集结果组装成已校验的 Trace。"""
        ended = utc_now()
        duration_ms = int((ended - self.started_at).total_seconds() * 1000)
        payload: Dict[str, Any] = {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "agent_name": self.tracer.config.agent_name,
            "agent_version": self.tracer.config.agent_version,
            "env": self.tracer.config.env,
            "started_at": iso_timestamp(self.started_at),
            "ended_at": iso_timestamp(ended),
            "duration_ms": duration_ms,
            "status": self.status.value,
            "error": self.error,
            "model": self.model,
            "tags": self.tags or None,
            "meta": self.meta or None,
            "conversation": [step.model_dump(mode="python") for step in self._steps],
        }
        payload.update(self.extra)
        return Trace.model_validate(payload)

    def commit(self) -> Optional[Trace]:
        """组装、校验并交给导出层。幂等，重复调用只提交一次。"""
        if self._committed:
            return None
        self._committed = True
        if not self.tracer.config.enabled:
            return None
        trace = self.as_trace()
        self.tracer._emit(trace)
        return trace

    @property
    def steps(self) -> List[Step]:
        return list(self._steps)


class _StepContext:
    """`with turn.step(...) as step:` 自动记录起止时间与 status。"""

    def __init__(self, turn: Turn, message_type: MessageType, kwargs: Dict[str, Any]) -> None:
        self.turn = turn
        self.message_type = message_type
        self.kwargs = dict(kwargs)
        self.step: Optional[Step] = None
        self._start = None

    def __enter__(self) -> "_StepContext":
        self._start = utc_now()
        return self

    def set_content(self, content: Any) -> None:
        """记录这一步的产出。

        对 tool_call 步骤，入参按工具调用规格解析（name/arguments、元组、
        ToolCall、dict）；其余步骤按文本内容处理。
        """
        if self.message_type is MessageType.TOOL_CALL:
            self.kwargs["tool_calls"] = _as_tool_calls(content)
        else:
            self.kwargs["content"] = None if content is None else _stringify(content)

    def __exit__(self, exc_type, exc, tb) -> bool:
        latency_ms = int((utc_now() - self._start).total_seconds() * 1000)
        kwargs = dict(self.kwargs)
        if exc is not None:
            kwargs.setdefault("status", Status.ERROR.value)
            kwargs.setdefault("error", "%s: %s" % (exc_type.__name__, exc))
        # content 统一从 kwargs 取出，避免与位置参数重复
        content = kwargs.pop("content", None)
        if self.message_type is MessageType.TOOL_CALL:
            raw_calls = kwargs.pop("tool_calls", None) or kwargs.pop("calls", None)
            calls = raw_calls if isinstance(raw_calls, list) and all(
                isinstance(item, ToolCall) for item in raw_calls
            ) else _as_tool_calls(raw_calls)
            if not calls:
                raise ValueError("tool_call 步骤需要至少一个工具调用")
            self.step = self.turn._append(
                self.message_type, None, None, latency_ms=latency_ms, tool_calls=calls, **kwargs
            )
        else:
            self.step = self.turn._append(
                self.message_type, content, None, latency_ms=latency_ms, **kwargs
            )
        return False


class _SpanContext:
    """嵌套 span 的上下文管理器：只维护层级，不产生 step。

    进入时把新 span_id 压栈，内部记录的 step / 子 span 自动挂到它下面；
    退出时弹栈。若传了 `record=True`，还会落一条用于标记该 span 起止的 step
    （便于在只有 step 的下游还原层级）。
    """

    def __init__(self, turn: Turn, kind: str, name: Optional[str], kwargs: Dict[str, Any]) -> None:
        self.turn = turn
        self.kind = kind
        self.name = name
        self.kwargs = dict(kwargs)
        self.span_id = generate_id("span")
        self.parent_span_id: Optional[str] = None
        self._start = None
        self._token = None

    def __enter__(self) -> "_SpanContext":
        self._start = utc_now()
        self.parent_span_id = ctx.get_current_span_id()
        # span 容器自身也作为一条 step 落库，保证 parent_span_id 能解析到真实节点；
        # 该 step 的用途由 meta.span_kind / meta.span_name 标注。
        self.turn._append(
            MessageType.REASONING,
            None,
            None,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            meta={"span_kind": self.kind, "span_name": self.name, "span_container": True},
        )
        self._token = ctx.push_span(self.span_id)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._token is not None:
            ctx.reset_span(self._token)
            self._token = None
        return False

    @property
    def latency_ms(self) -> Optional[int]:
        if self._start is None:
            return None
        return int((utc_now() - self._start).total_seconds() * 1000)


class Tracer:
    """SDK 入口。持有配置与导出器，负责创建 Turn 并分发已提交的 Trace。"""

    def __init__(
        self,
        agent_name: Optional[str] = None,
        agent_version: Optional[str] = None,
        env: Optional[str] = None,
        exporters: Optional[Iterable[Any]] = None,
        config: Optional[Config] = None,
        **config_overrides: Any,
    ) -> None:
        if config is None:
            overrides: Dict[str, Any] = dict(config_overrides)
            if agent_name is not None:
                overrides["agent_name"] = agent_name
            if agent_version is not None:
                overrides["agent_version"] = agent_version
            if env is not None:
                overrides["env"] = env
            config = Config.from_env(**overrides)
        self.config = config
        self._exporters: List[Any] = list(exporters or config.exporters or [])

    # ---------- 导出器管理 ----------

    def add_exporter(self, exporter: Any) -> None:
        self._exporters.append(exporter)

    def _emit(self, trace: Trace) -> None:
        """把已校验的 trace 交给每个导出器；单个导出器失败不影响其它。"""
        for exporter in list(self._exporters):
            try:
                exporter.export(trace)
            except Exception:
                if self.config.debug:
                    raise

    def flush(self) -> None:
        for exporter in list(self._exporters):
            flush = getattr(exporter, "flush", None)
            if callable(flush):
                flush()

    def close(self) -> None:
        self.flush()
        for exporter in list(self._exporters):
            close = getattr(exporter, "close", None)
            if callable(close):
                close()

    # ---------- turn 入口 ----------

    def turn(
        self,
        turn_id: Optional[str] = None,
        session_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Turn:
        """创建并进入一轮采集。`with tracer.turn(...) as turn:`。"""
        return Turn(
            self,
            turn_id=turn_id or generate_id("turn"),
            session_id=session_id or generate_id("sess"),
            **kwargs,
        )

    # ---------- 装饰器 ----------

    def trace(self, func: Callable) -> Callable:
        """装饰一个函数：整次调用 = 一个 turn。用户输入/返回值自动落 step。"""

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            turn_id = kwargs.pop("turn_id", None) or generate_id("turn")
            session_id = kwargs.pop("session_id", None) or generate_id("sess")
            with self.turn(turn_id=turn_id, session_id=session_id) as turn:
                turn.record_user_query(_describe_call(func, args, kwargs))
                result = func(*args, **kwargs)
                turn.record_final_answer(_stringify(result))
                return result

        return wrapper

    def step(self, message_type: Union[MessageType, str] = MessageType.REASONING) -> Callable:
        """装饰一个函数：函数执行 = 当前 turn 里的一步，自动计时。"""
        resolved = MessageType(message_type)

        def decorator(func: Callable) -> Callable:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                turn = ctx.get_current_turn()
                if turn is None:
                    return func(*args, **kwargs)
                with turn.step(resolved) as step_ctx:
                    result = func(*args, **kwargs)
                    step_ctx.set_content(result)
                    return result

            return wrapper

        return decorator


def _stringify(value: Any) -> str:
    import json

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _describe_call(func: Callable, args: tuple, kwargs: dict) -> str:
    import json

    try:
        signature = inspect.signature(func)
        bound = signature.bind_partial(*args, **kwargs)
        payload = {name: value for name, value in bound.arguments.items()}
    except (TypeError, ValueError):
        payload = {"args": list(args), "kwargs": kwargs}
    return json.dumps(payload, ensure_ascii=False, default=str)
