"""DeerFlow 集成：把 DeerFlow / LangGraph 的消息流自动还原成 trace。

用法（宿主通过 `plugins:` 加载，见 install_deerflow_extension）

设计要点：
- 不改 DeerFlow 的执行代码，而是一个中间件：
  `before_agent` 开一轮 trace（turn_id 取 run_id、session_id 取 thread_id），
  `after_model` 把新产生的 AI / Tool 消息映射成 step，
  `after_agent` 收尾提交。
- 消息映射遵循 SDK 契约（见 schemas.MessageType）：
  HumanMessage -> user_query（同轮第 2 条起算追问）
  AIMessage.content -> reasoning（伴随 tool_calls 时）/ final_answer（收尾时）
  AIMessage.tool_calls -> tool_call（多个调用合并为 1 个 step）
  ToolMessage -> tool_response
- 层级：`conversations` 一维数组，先后顺序由 step_no 表达；工具调用与返回用
  `tool_calls[].id` <-> `tool_call_id` 关联。多 agent 场景下每个 run 各自产出一条
  trace，靠 session_id 关联。

依赖：langchain 存在时中间件继承真正的 `AgentMiddleware`（DeerFlow 的隔离包装器
要求如此）；不存在时退化为普通类，仍可 import 本模块做纯逻辑单测。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..schemas import MessageType
from ..schemas import ToolCall as TraceToolCall
from ..tracer import Tracer, Turn

try:  # pragma: no cover - 依赖宿主环境
    from langchain.agents.middleware import AgentMiddleware as _AgentMiddleware
except Exception:  # pragma: no cover - 无 langchain 时的退化路径
    class _AgentMiddleware:  # type: ignore[no-redef]
        """占位基类：没有 langchain 时也能 import 本模块。"""

        name = "AgentTraceMiddleware"


# ---- 消息判定 ---------------------------------------------------------------

_KNOWN_ROLES = ("human", "ai", "tool", "system")


def _role_of(message: Any) -> Optional[str]:
    """返回消息类型标签：human / ai / tool / system / None。

    优先用 LangChain 消息的标准 `.type` 属性；缺失时按类名回退，
    保证对自定义消息子类同样可用。
    """
    role = getattr(message, "type", None)
    if isinstance(role, str) and role in _KNOWN_ROLES:
        return role
    name = type(message).__name__.lower()
    for candidate in _KNOWN_ROLES:
        if name in (candidate, candidate + "message"):
            return candidate
    return None


def _text_of(message: Any) -> str:
    """把消息 content 归一化成纯文本；多模态 content 只取文本块。"""
    content = getattr(message, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type")
                if block_type in ("text", "output_text") and isinstance(block.get("text"), str):
                    parts.append(block["text"])
        return "\n".join(parts)
    return str(content)


def _tool_calls_of(message: Any) -> List[Dict[str, Any]]:
    """抽取 AIMessage 的 tool_calls，统一为 {id, type, function} 形态。"""
    raw = getattr(message, "tool_calls", None) or []
    calls: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            if "function" in item:
                calls.append(item)
                continue
            calls.append(
                {
                    "id": item.get("id") or "",
                    "type": item.get("type", "function"),
                    "function": {
                        "name": item.get("name"),
                        "arguments": item.get("args", item.get("arguments")),
                    },
                }
            )
        else:  # pydantic ToolCall 对象
            calls.append(
                {
                    "id": getattr(item, "id", "") or "",
                    "type": "function",
                    "function": {
                        "name": getattr(item, "name", None),
                        "arguments": getattr(item, "args", None),
                    },
                }
            )
    return calls


def _token_deltas(message: Any) -> tuple:
    """从消息的 usage_metadata 取本次 token 增量 (prompt, completion)。

    取不到（模型未上报用量）时返回 (None, None)，字段留空。
    """
    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, dict):
        return None, None
    prompt = usage.get("input_tokens")
    completion = usage.get("output_tokens")
    return prompt, completion


def _additional_kwargs(message: Any) -> Dict[str, Any]:
    kwargs = getattr(message, "additional_kwargs", None)
    return kwargs if isinstance(kwargs, dict) else {}


def is_clarification_answer(message: Any) -> bool:
    """是否为对 ask_clarification 的回答（结构化 human_input_response）。"""
    raw = _additional_kwargs(message).get("human_input_response")
    if not isinstance(raw, dict):
        return False
    if raw.get("version") != 1 or raw.get("kind") != "human_input_response":
        return False
    return bool(raw.get("value")) and bool(raw.get("request_id"))


def is_genuine_user_message(message: Any) -> bool:
    """是否为真实的用户输入，而非框架注入的 human 消息。

    与 DeerFlow 的 `_is_genuine_user_message` 保持同一语义：
    `hide_from_ui` 标记的是内部注入（memory 画像、动态上下文提醒、
    待办提醒、摘要等），但澄清答复同样带该标记且属于真实用户输入，
    因此只在「没有有效 human_input_response」时才判定为注入。
    """
    if _role_of(message) != "human":
        return False
    kwargs = _additional_kwargs(message)
    if kwargs.get("hide_from_ui") and not is_clarification_answer(message):
        return False
    return True


def _injected_label(message: Any) -> Optional[str]:
    """给内部注入的 human 消息一个来源标签，便于下游区分。"""
    kwargs = _additional_kwargs(message)
    if kwargs.get("dynamic_context_reminder"):
        return "dynamic_context"
    if kwargs.get("hide_from_ui"):
        return "injected"
    return None


def _message_key(message: Any) -> str:
    """消息去重键：优先用 LangChain 的 message.id，退化到内容指纹。"""
    mid = getattr(message, "id", None)
    if mid:
        return "id:%s" % mid
    return "fp:%s|%s" % (_role_of(message), _text_of(message)[:200])


# ---- 中间件 -----------------------------------------------------------------


class AgentTraceMiddleware(_AgentMiddleware):
    """把一轮 DeerFlow run 采集为一条 trace。

    消息对象只在运行时按属性读取（鸭子类型），因此不依赖 langchain 的具体
    消息类；但继承关系上必须是真正的 `AgentMiddleware`，DeerFlow 的隔离包装器
    才会镜像本类的钩子。
    """

    #: 中间件名，LangChain / DeerFlow 用于标识与去重
    name = "AgentTraceMiddleware"

    def __init__(
        self,
        tracer: Tracer,
        *,
        agent_id: Optional[str] = None,
    ) -> None:
        self.tracer = tracer
        self.agent_id = agent_id
        # 每个 session 已开出的轮次计数，用于自动填 turn_no（会话内第几轮）
        self._turn_no_by_session: Dict[str, int] = {}
        # 每轮 run 的状态：key 为 run 标识，value 为该轮的采集状态
        self._turns: Dict[str, Dict[str, Any]] = {}
        # 每个 session 最近一次采集的 turn，用于把澄清续跑并入同一轮：
        # session_id -> {"turn": Turn, "turn_key": str}
        self._last_turn_by_session: Dict[str, Dict[str, Any]] = {}

    # ---- 内部：定位/创建当前轮的采集状态 ----

    def _state_for(self, runtime: Any) -> Optional[Dict[str, Any]]:
        key = self._run_key(runtime)
        if key is None:
            return None
        return self._turns.get(key)

    @staticmethod
    def _runtime_context(runtime: Any) -> Dict[str, Any]:
        context = getattr(runtime, "context", None) or {}
        return context if isinstance(context, dict) else {}

    def _run_key(self, runtime: Any) -> Optional[str]:
        context = self._runtime_context(runtime)
        return context.get("run_id") or context.get("thread_id")

    def _open_turn(self, state: Any, runtime: Any) -> None:
        """before_agent：开一轮采集。"""
        key = self._run_key(runtime)
        if key is None or key in self._turns:
            return
        context = self._runtime_context(runtime)
        run_id = context.get("run_id") or key
        session_id = context.get("thread_id") or run_id

        messages = []
        getter = getattr(state, "get", None)
        if callable(getter):
            messages = list(getter("messages", []) or [])

        # 本轮用户输入：最后一条「真实用户消息」（排除框架注入的 human 消息）。
        # 澄清答复（human_input_response）也属于真实用户输入，只是语义上是追问。
        last_genuine_human = None
        for index, message in enumerate(messages):
            if is_genuine_user_message(message):
                last_genuine_human = index

        # 澄清续跑判定：DeerFlow 的 ask_clarification 会中断 run，用户回答后
        # 另起一个 run_id 继续。此时不应新开 trace，而要并入同 session 的上一轮，
        # 否则「提问 -> 反问 -> 澄清 -> 完成」会被错误拆成两条 trace。
        continuation = False
        if last_genuine_human is not None:
            continuation = is_clarification_answer(messages[last_genuine_human])
        previous = self._last_turn_by_session.get(str(session_id))
        if continuation and previous is not None and not previous["turn"]._committed:
            turn = previous["turn"]
            entry: Dict[str, Any] = {
                "turn": turn,
                "seen": set(),
                # 续跑：不重采 system_prompt
                "system_prompt_done": True,
            }
            # 历史消息标为已见（不重复采集）；但**澄清答复本身是本轮的用户输入**，
            # 必须保留给 after_model 采集，因此从已见集合里排除它及其之后的消息。
            for message in messages[: last_genuine_human]:
                entry["seen"].add(_message_key(message))
            self._turns[key] = entry
            return

        # turn_no = 该会话内的第几轮；中间件按 session 自增计数（澄清续跑不占用新轮次）
        session_key = str(session_id)
        self._turn_no_by_session[session_key] = self._turn_no_by_session.get(session_key, 0) + 1
        turn = self.tracer.turn(
            turn_id=str(run_id),
            session_id=session_key,
            turn_no=self._turn_no_by_session[session_key],
        )
        turn.__enter__()  # 手动进入，生命周期由中间件控制（after_agent 提交）
        entry: Dict[str, Any] = {
            "turn": turn,
            "seen": set(),
            "system_prompt_done": False,
        }

        # 一轮 run 的初始 state 里同时带着「历史」和「本轮输入」：DeerFlow 会把
        # 本次用户输入作为最后一条真实用户消息放进初始 state。以它（或其后的
        # AI 消息）为界——之前的属于历史，标记为已见避免重复采集；它本身及其
        # 之后的消息属于本轮，交给 after_model 正常处理。
        boundary = last_genuine_human
        if boundary is None:
            # 没有真实用户输入（例如纯系统触发的 run）：整体按历史处理
            boundary = len(messages)

        for message in messages[:boundary]:
            entry["seen"].add(_message_key(message))

        # 系统提示词属于历史，却是本轮上下文的一部分，单独采集一次（放在最前）。
        for message in messages[:boundary]:
            if _role_of(message) == "system":
                turn.record_system_prompt(_text_of(message), meta={"historical": True})
                break
        entry["system_prompt_done"] = True
        self._turns[key] = entry

    def _consume_messages(self, state: Any, runtime: Any) -> None:
        """after_model：把新增消息增量映射成 step。"""
        key = self._run_key(runtime)
        if key is None:
            return
        entry = self._turns.get(key)
        if entry is None:
            return
        turn: Turn = entry["turn"]
        seen = entry["seen"]

        messages = state.get("messages", []) if hasattr(state, "get") else []
        for message in messages:
            key_of = _message_key(message)
            if key_of in seen:
                continue
            seen.add(key_of)
            self._emit_message(turn, message)

    def _emit_message(self, turn: Turn, message: Any) -> None:
        """单条 LangChain 消息 -> 一个或多个 step。"""
        role = _role_of(message)
        text = _text_of(message)

        if role == "human":
            if not is_genuine_user_message(message):
                # 框架注入的 human 消息（memory 画像、动态上下文提醒、待办提醒等）
                # 不是用户输入：单独记一条带来源标记的 step，不算 user_query。
                label = _injected_label(message) or "injected"
                if text.strip():
                    turn._append(
                        MessageType.REASONING,
                        text,
                        None,
                        meta={"deerflow_injected": label},
                    )
                return
            # 澄清答复与同轮内第 2 条起的用户输入都算追问
            is_follow_up = is_clarification_answer(message) or any(
                step.message_type is MessageType.USER_QUERY for step in turn.steps
            )
            meta = {"clarification_answer": True} if is_clarification_answer(message) else None
            turn.record_user_query(text, follow_up=is_follow_up, meta=meta)
            return

        if role == "system":
            turn.record_system_prompt(text)
            return

        if role == "tool":
            tool_call_id = getattr(message, "tool_call_id", None)
            msg_status = getattr(message, "status", None)
            # LangChain 的 ToolMessage.status: "success" / "error"；缺省视为未知
            if msg_status == "error":
                tool_success = 0
            elif msg_status == "success":
                tool_success = 1
            else:
                tool_success = -1
            turn.record_tool_response(
                tool_call_id=tool_call_id or "",
                content=text,
                # tool_name 由 SDK 按 tool_call_id 自动回填
                tool_name=getattr(message, "name", None),
                tool_success=tool_success,
                completed=False if tool_success == 0 else None,
                error=text if tool_success == 0 else None,
            )
            return

        if role == "ai":
            calls = _tool_calls_of(message)
            prompt_delta, completion_delta = _token_deltas(message)
            if text:
                # 判定规则（与 TerminalResponseMiddleware 的语义对齐）：
                # - 带 tool_calls 的 AI 文本 = 推理/规划 -> reasoning
                # - 不带 tool_calls 的 AI 文本 = 候选最终回答 -> 先记为 reasoning，
                #   若它确实是本轮最后一条 AI 文本，after_agent 会把它升级为 final_answer
                #   （见 _close_turn 的 pending_ai 处理）
                turn.record_reasoning(
                    text,
                    prompt_tokens_delta=prompt_delta,
                    completion_tokens_delta=completion_delta,
                    meta={"pending_final": not calls},
                )
            if calls:
                turn.record_tool_call(
                    [
                        TraceToolCall(
                            id=call.get("id") or "",
                            type=call.get("type", "function"),
                            function=call.get("function") or {},
                        )
                        for call in calls
                    ]
                )
            return

    def _close_turn(self, state: Any, runtime: Any, error: Optional[str] = None) -> None:
        """after_agent：补 final_answer（若缺）并提交。"""
        key = self._run_key(runtime)
        if key is None:
            return
        entry = self._turns.pop(key, None)
        if entry is None:
            return
        turn: Turn = entry["turn"]

        if error:
            turn.completed = False
            turn.error = error

        # 末条 AI 文本的归宿：若本轮最后一条 step 是带 pending_final 标记的
        # reasoning，说明它就是本轮最终回答，改写为 final_answer 而不是新增一步。
        # 末条 AI 文本的归宿：若本轮最后一条 step 是带 pending_final 标记的
        # reasoning（即不带 tool_calls 的 AI 文本），它就是本轮最终回答，
        # 改写为 final_answer 而不是新增一步。
        last = turn.steps[-1] if turn.steps else None
        promoted = False
        if (
            last is not None
            and last.message_type is MessageType.REASONING
            and (last.meta or {}).get("pending_final")
        ):
            last.message_type = MessageType.FINAL_ANSWER
            if error:
                last.completed = False
                last.error = error
            promoted = True

        # pending_final 是内部临时标记，绝不外泄到产出的 trace 里
        for step in turn.steps:
            if step.meta and "pending_final" in step.meta:
                step.meta = {k: v for k, v in step.meta.items() if k != "pending_final"} or None

        # 暂存未提交的澄清轮：其最后一步是 ask_clarification 工具调用，
        # 说明 DeerFlow 正中断等待用户回答，后续 run 会带着澄清答复续跑。
        # 此时不能提交，否则「提问 -> 反问 -> 澄清 -> 完成」被拆成两条 trace。
        if self._awaits_clarification(turn):
            self._last_turn_by_session[turn.session_id] = {"turn": turn, "turn_key": key}
            self._turns.pop(key, None)
            return

        # 本轮结束（未在等澄清）：把同 session 暂存的澄清轮先提交掉，
        # 避免用户放弃澄清、直接重新提问时那部分数据丢失。
        self._flush_parked_turn(turn.session_id)

        if not promoted and (last is None or last.message_type is not MessageType.FINAL_ANSWER):
            # 本轮没有产出可见回答（例如异常中断）：补一条终止 step
            turn.record_final_answer(
                "",
                completed=False if error else None,
                error=error,
            )

        turn.__exit__(None, None, None)

    @staticmethod
    def _awaits_clarification(turn: Turn) -> bool:
        """本轮最后一步是否为 ask_clarification（DeerFlow 借此中断等待用户输入）。"""
        last = turn.steps[-1] if turn.steps else None
        if last is None or last.message_type is not MessageType.TOOL_CALL:
            return False
        for call in last.tool_calls or []:
            function = call.function or {}
            if function.get("name") == "ask_clarification":
                return True
        return False

    def _flush_parked_turn(self, session_id: str) -> None:
        """提交某 session 暂存的澄清轮（用户未续跑时兜底提交）。"""
        parked = self._last_turn_by_session.pop(session_id, None)
        if parked is None:
            return
        turn: Turn = parked["turn"]
        if turn._committed:
            return
        turn.__exit__(None, None, None)

    # ---- AgentMiddleware 钩子（同步） ----

    def before_agent(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        self._open_turn(state, runtime)
        return None

    def after_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        self._consume_messages(state, runtime)
        return None

    def after_agent(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        self._close_turn(state, runtime)
        return None

    # ---- AgentMiddleware 钩子（异步） ----

    async def abefore_agent(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        self._open_turn(state, runtime)
        return None

    async def aafter_model(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        self._consume_messages(state, runtime)
        return None

    async def aafter_agent(self, state: Any, runtime: Any) -> Optional[Dict[str, Any]]:
        self._close_turn(state, runtime)
        return None

# ---- DeerFlow 插件入口 ------------------------------------------------------


def _build_tracer(config: Dict[str, Any]) -> Tracer:
    """按插件 config 构造 Tracer（导出器按 output 配置创建）。"""
    from ..exporters import JsonExporter, JsonlExporter, MemoryExporter

    exporters: List[Any] = []
    output = config.get("output")
    if output:
        path = output
        if str(path).lower().endswith(".jsonl"):
            exporters.append(JsonlExporter(path))
        else:
            exporters.append(JsonExporter(path))
    if config.get("memory", False):
        exporters.append(MemoryExporter())

    return Tracer(
        agent_id=config.get("agent_id"),
        parent_agent_id=config.get("parent_agent_id"),
        app_name=config.get("app_name"),
        app_id=config.get("app_id"),
        system_code=config.get("system_code"),
        user_id=config.get("user_id"),
        source=config.get("source"),
        exporters=exporters,
    )


def install(registry: Any, config: Any) -> None:
    """DeerFlow 插件入口：`use: agent_trace.integrations.deerflow:install`。

    在 config.yaml 里：

        plugins:
          - use: agent_trace.integrations.deerflow:install
            config:
              output: "outputs/agent_traces.json"
              agent_id: "your-agent-id"
              app_name: "deer-flow"

    注册一个覆盖 lead + subagent 的采集中间件。放置位置用 `STANDARD`：
    采集只观察状态，不改变模型的请求/响应，也不改工具返回值，因此对
    执行语义没有要求，只需稳定地挂在栈里。
    """
    from deerflow_extension_api import AgentScope, MiddlewarePlacement, Placement

    options = dict(config or {})
    if options.get("enabled", True) is False:
        return

    tracer = _build_tracer(options)
    middleware = AgentTraceMiddleware(
        tracer,
        agent_id=options.get("agent_id"),
    )

    class _Contributor:
        """把已构造好的中间件实例交给宿主。"""

        def contribute_middlewares(self, app_store: Any, ctx: Any) -> Any:
            return (
                MiddlewarePlacement(
                    middleware=middleware,
                    placement=Placement.STANDARD,
                    scope=AgentScope.BOTH,
                ),
            )

    registry.middlewares(_Contributor())