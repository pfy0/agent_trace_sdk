"""采集层：Tracer / Turn / Step 的生命周期与手动埋点 API（模式①）。

用法：

    tracer = Tracer(agent_id="agent-demo")
    with tracer.turn(turn_id="t-1", session_id="s-1") as turn:
        turn.record_user_query("查看今日ai新闻热点")
        turn.record_reasoning("先搜索今日 AI 新闻……")
        turn.record_tool_call([("web_search", {"count": 8})])
        turn.record_tool_response(tool_call_id="call_1", content="{...}")
        turn.record_final_answer("今日要闻……")

退出 with 时自动：分配 step_no/message_id、校验成 Trace、交给导出器。
异常路径也会落一条终止 step 并把 Trace 级 completed 置为 false。

层级说明
--------
`conversations` 是一维数组，先后顺序由 `step_no` 表达；工具调用与返回之间用
`tool_calls[].id` <-> `tool_call_id` 关联。不再引入 span 层级字段；若业务方需要
标注"这一步属于哪个子 agent"等额外信息，用 Trace 级 `metadata` 承载。
"""
from __future__ import annotations

import functools
import inspect
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

from . import context as ctx
from .config import Config
from .schemas import (
    MESSAGE_TYPE_META,
    MessageType,
    Step,
    ToolCall,
    Trace,
    generate_id,
    generate_message_id,
    iso_timestamp,
    utc_now,
)

logger = logging.getLogger("agent_trace")


def _pop_start(kwargs: Dict[str, Any]) -> Optional[str]:
    """取出该 step 的开始时刻；兼容旧参数名 timestamp。"""
    return kwargs.pop("start_time", kwargs.pop("timestamp", None))


def _parse_iso(value: Any) -> Optional[datetime]:
    """把 ISO 8601 字符串解析成带时区的 datetime；失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


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
    # OpenAI 风格的 tool_call 常带 index 等附加键；ToolCall 已收紧为 extra="forbid"，
    # 这里按模型字段过滤，只保留契约内的 id / type / function。
    call_fields = set(ToolCall.model_fields)
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
                calls.append(
                    ToolCall.model_validate(
                        {key: value for key, value in item.items() if key in call_fields}
                    )
                )
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
        turn_no: int = 1,
        completed: Optional[bool] = None,
        partial: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> None:
        # extra 只接受 Trace 已有的字段名（用于按轮覆盖 user_id / source 等）；
        # 未知键直接报错，避免被契约层 extra="forbid" 静默丢弃。
        unknown = sorted(set(extra) - set(Trace.model_fields))
        if unknown:
            raise TypeError(
                "Turn 收到未知的 Trace 字段: %s。规范外信息请用 metadata / extra_info 承载"
                % ", ".join(unknown)
            )
        self.tracer = tracer
        self.turn_id = turn_id
        self.session_id = session_id
        self.turn_no = turn_no
        self.metadata = dict(metadata or {})
        self.extra = dict(extra)

        self.completed = tracer.config.default_completed if completed is None else bool(completed)
        self.partial = bool(partial)
        self.started_at = utc_now()

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

    def _resolve_tool_info(self, tool_call_id: Optional[str]) -> tuple:
        """按 tool_call_id 找到所属的 tool_call step，返回 (其 span_id, 工具名)。

        用于自动回填 tool_response 的 parent_span_id / tool_name（span 规则 B：
        tool_call 是父，指向它的 tool_response 是子）。
        """
        if not tool_call_id:
            return None, None
        for step in reversed(self._steps):
            if step.message_type is not MessageType.TOOL_CALL:
                continue
            for call in step.tool_calls or []:
                if call.id == tool_call_id:
                    name = (call.function or {}).get("name")
                    return step.span_id, name
        return None, None

    def _append(
        self,
        message_type: MessageType,
        content: Optional[str],
        start_time: Optional[str] = None,
        message_id: Optional[str] = None,
        latency_ms: Optional[int] = None,
        tool_calls: Optional[List[ToolCall]] = None,
        tool_call_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_success: Optional[int] = None,
        prompt_tokens_delta: Optional[int] = None,
        completion_tokens_delta: Optional[int] = None,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        is_follow_up: Optional[bool] = None,
    ) -> Step:
        if self._committed:
            raise RuntimeError("该 turn 已提交，不能再追加 step")
        role = MESSAGE_TYPE_META[message_type][1]
        resolved_id = message_id or self._next_message_id(message_type)

        # 时刻：start_time 取传入值或当前时间；end_time 有耗时则相加，否则等于 start_time
        start_dt = _parse_iso(start_time) or utc_now()
        if latency_ms:
            end_dt = start_dt + timedelta(milliseconds=latency_ms)
        else:
            end_dt = start_dt

        # span 树（规则 B）：tool_response 自动挂到对应 tool_call 下并回填工具名
        if message_type is MessageType.TOOL_RESPONSE:
            resolved_parent, resolved_name = self._resolve_tool_info(tool_call_id)
            if parent_span_id is None:
                parent_span_id = resolved_parent
            if tool_name is None:
                tool_name = resolved_name
            if tool_success is None:
                # 拿不到明确状态时记为「未知」
                tool_success = -1

        step = Step(
            message_id=resolved_id,
            step_no=len(self._steps) + 1,
            role=role,
            message_type=message_type,
            content=content,
            start_time=iso_timestamp(start_dt),
            end_time=iso_timestamp(end_dt),
            latency_ms=latency_ms,
            span_id=span_id or generate_id("span"),
            parent_span_id=parent_span_id,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_success=tool_success,
            prompt_tokens_delta=prompt_tokens_delta,
            completion_tokens_delta=completion_tokens_delta,
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
        return self._append(
            MessageType.USER_QUERY, content, _pop_start(kwargs), **kwargs
        )

    def record_reasoning(self, content: str, **kwargs: Any) -> Step:
        return self._append(
            MessageType.REASONING, content, _pop_start(kwargs), **kwargs
        )

    def record_system_prompt(self, content: str, **kwargs: Any) -> Step:
        return self._append(
            MessageType.SYSTEM_PROMPT, content, _pop_start(kwargs), **kwargs
        )

    def record_tool_call(self, tool_calls: Any = None, **kwargs: Any) -> Step:
        """记录一批工具调用。

        入参形态同 `_as_tool_calls`；也支持 `name=` / `arguments=` 简写：
        `record_tool_call(name="web_search", arguments={"count": 8})`。
        """
        raw = tool_calls if tool_calls is not None else kwargs.pop("calls", None)
        if raw is None and "name" in kwargs:
            raw = {"name": kwargs.pop("name"), "arguments": kwargs.pop("arguments", None)}
        if raw is None and "arguments" in kwargs:
            raise ValueError("record_tool_call 传 arguments 时必须同时传 name")
        calls = _as_tool_calls(raw)
        if not calls:
            raise ValueError("record_tool_call 需要至少一个工具调用")
        return self._append(
            MessageType.TOOL_CALL,
            None,
            _pop_start(kwargs),
            tool_calls=calls,
            **kwargs,
        )

    def record_tool_response(
        self, tool_call_id: str, content: Optional[str] = None, **kwargs: Any
    ) -> Step:
        return self._append(
            MessageType.TOOL_RESPONSE,
            content,
            _pop_start(kwargs),
            tool_call_id=tool_call_id,
            **kwargs,
        )

    def record_final_answer(self, content: str, **kwargs: Any) -> Step:
        return self._append(
            MessageType.FINAL_ANSWER, content, _pop_start(kwargs), **kwargs
        )

    # ---------- 步骤上下文管理器：自动计时 ----------

    def step(self, message_type: Union[MessageType, str], **kwargs: Any) -> "_StepContext":
        return _StepContext(self, MessageType(message_type), kwargs)

    def record_error(self, error: str, content: Optional[str] = None, **kwargs: Any) -> Step:
        """落一条终止 step，并把整个 turn 标记为未完成。

        Step 不再承载 `error` / `completed` 字段，失败信息只体现在 Trace 级
        `completed=False`；`error` 形参保留是为了兼容既有调用。
        """
        self.completed = False
        return self._append(
            MessageType.FINAL_ANSWER,
            content,
            kwargs.pop("start_time", kwargs.pop("timestamp", None)),
            **kwargs,
        )

    # ---------- 生命周期 ----------

    def __enter__(self) -> "Turn":
        token = ctx.set_current_turn(self)
        self._tokens.append(("turn", token))
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and self.completed is not False:
            self.completed = False
            # 异常路径：若还没有终止 step，补一条终止 final_answer
            if not self._steps or self._steps[-1].message_type is not MessageType.FINAL_ANSWER:
                self._append(MessageType.FINAL_ANSWER, None, None)
        for kind, token in reversed(self._tokens):
            if kind == "turn":
                ctx.reset_current_turn(token)
        self._tokens.clear()
        self.commit()
        return False  # 不吞异常

    def as_trace(self) -> Trace:
        """把当前采集结果组装成已校验的 Trace。"""
        ended = utc_now()
        payload: Dict[str, Any] = {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "turn_no": self.turn_no,
            "agent_id": self.tracer.config.agent_id,
            "parent_agent_id": self.tracer.config.parent_agent_id,
            "app_name": self.tracer.config.app_name,
            "app_id": self.tracer.config.app_id,
            "system_code": self.tracer.config.system_code,
            "user_id": self.tracer.config.user_id,
            "source": self.tracer.config.source,
            "start_time": iso_timestamp(self.started_at),
            "end_time": iso_timestamp(ended),
            "completed": self.completed,
            "partial": self.partial,
            "metadata": self.metadata or None,
            "conversations": [step.model_dump(mode="python") for step in self._steps],
        }
        payload.update(self.extra)
        return Trace.model_validate(payload)

    def commit(self) -> Optional[Trace]:
        """组装、校验并交给导出层。幂等，重复调用只提交一次。

        采集是旁路：若本轮数据不满足契约（例如没有任何 user_query），
        默认只记日志并丢弃这条 trace，绝不把 ValidationError 抛给业务方；
        `config.debug=True` 时才向上抛，便于排查。
        """
        if self._committed:
            return None
        self._committed = True
        if not self.tracer.config.enabled:
            return None
        try:
            trace = self.as_trace()
        except Exception as exc:
            if self.tracer.config.debug:
                raise
            logger.warning("trace 校验失败，已丢弃 turn_id=%s: %s", self.turn_id, exc)
            return None
        self.tracer._emit(trace)
        return trace

    @property
    def steps(self) -> List[Step]:
        return list(self._steps)


class _StepContext:
    """`with turn.step(...) as step:` 自动记录起止时间。"""

    def __init__(self, turn: Turn, message_type: MessageType, kwargs: Dict[str, Any]) -> None:
        self.turn = turn
        self.message_type = message_type
        self.kwargs = dict(kwargs)
        self.step: Optional[Step] = None
        self._start = None

    def __enter__(self) -> "_StepContext":
        self._start = utc_now()
        return self

    def _start_iso(self) -> str:
        return iso_timestamp(self._start)

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
        # 步骤级出错不落到 step 字段（Step 无 error/completed），异常继续向外抛；
        # Trace 级的 completed 由 Turn.__exit__ 统一置为 False。
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
                self.message_type, None, self._start_iso(), latency_ms=latency_ms,
                tool_calls=calls, **kwargs,
            )
        else:
            self.step = self.turn._append(
                self.message_type, content, self._start_iso(), latency_ms=latency_ms, **kwargs
            )
        return False


class Tracer:
    """SDK 入口。持有配置与导出器，负责创建 Turn 并分发已提交的 Trace。"""

    def __init__(
        self,
        agent_id: Optional[str] = None,
        parent_agent_id: Optional[str] = None,
        app_name: Optional[str] = None,
        app_id: Optional[str] = None,
        system_code: Optional[str] = None,
        user_id: Optional[str] = None,
        source: Optional[str] = None,
        exporters: Optional[Iterable[Any]] = None,
        config: Optional[Config] = None,
        **config_overrides: Any,
    ) -> None:
        if config is None:
            overrides: Dict[str, Any] = dict(config_overrides)
            if agent_id is not None:
                overrides["agent_id"] = agent_id
            if parent_agent_id is not None:
                overrides["parent_agent_id"] = parent_agent_id
            if app_name is not None:
                overrides["app_name"] = app_name
            if app_id is not None:
                overrides["app_id"] = app_id
            if system_code is not None:
                overrides["system_code"] = system_code
            if user_id is not None:
                overrides["user_id"] = user_id
            if source is not None:
                overrides["source"] = source
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
        """创建并进入一轮采集。`with tracer.turn(...) as turn:`。

        `turn_no` 为该会话内的第几轮（业务方传入，缺省 1）；
        `completed` / `partial` 为结束状态，缺省取 config（true / false）。
        """
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
            turn_no = kwargs.pop("turn_no", 1)
            with self.turn(turn_id=turn_id, session_id=session_id, turn_no=turn_no) as turn:
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
