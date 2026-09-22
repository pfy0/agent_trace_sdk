"""契约层：枚举、字段模型、校验规则、id 生成。

本模块是整个 SDK 的单点契约。所有字段定义集中在 `Step` / `Trace` 两个
pydantic 模型里，schema 演进时只需改这里。

两层数据模型：
- Trace：一次 agent 运行 = 一个 turn = 一轮交互，主键 `turn_id`。
- Step：trace 中的一步，即 `conversation` 数组里的一条消息。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MessageType(str, Enum):
    """消息类型枚举，取值与 message_id 缩写、role 的映射见 MESSAGE_TYPE_META。"""

    SYSTEM_PROMPT = "system_prompt"
    USER_QUERY = "user_query"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESPONSE = "tool_response"
    FINAL_ANSWER = "final_answer"


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Status(str, Enum):
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


# message_type -> (类型缩写, 对应 role)
MESSAGE_TYPE_META: Dict[MessageType, tuple] = {
    MessageType.SYSTEM_PROMPT: ("sys", Role.SYSTEM),
    MessageType.USER_QUERY: ("usr", Role.USER),
    MessageType.REASONING: ("rsn", Role.ASSISTANT),
    MessageType.TOOL_CALL: ("cal", Role.ASSISTANT),
    MessageType.TOOL_RESPONSE: ("res", Role.TOOL),
    MessageType.FINAL_ANSWER: ("ans", Role.ASSISTANT),
}

MESSAGE_ID_PATTERN = re.compile(r"^msg_(sys|usr|rsn|cal|res|ans)_\d+$")


def utc_now() -> datetime:
    """返回当前 UTC 时间（带时区）。"""
    return datetime.now(timezone.utc)


def iso_timestamp(value: Optional[datetime] = None) -> str:
    """格式化为 ISO 8601 UTC 字符串，毫秒精度，以 Z 结尾。"""
    moment = value or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def generate_id(prefix: str) -> str:
    """生成带前缀的唯一 id，如 tr_xxxx / sess_xxxx。"""
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def generate_message_id(message_type: Union[MessageType, str], sequence: int) -> str:
    """按 `msg_{类型缩写}_{序号}` 生成 message_id。序号为该类型内自增计数。"""
    resolved = MessageType(message_type)
    abbr, _ = MESSAGE_TYPE_META[resolved]
    return "msg_%s_%d" % (abbr, sequence)

class ToolCall(BaseModel):
    """单个工具调用对象。字段刻意贴近 OpenAI 的 tool_calls 结构。"""

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., description="工具调用 ID，用于与 tool_response 关联")
    type: str = "function"
    function: Dict[str, Any] = Field(default_factory=dict)


class Step(BaseModel):
    """轨迹中的一步 = `conversation` 数组里的一个元素。"""

    model_config = ConfigDict(extra="allow")

    message_id: str = Field(..., description="格式 msg_{类型缩写}_{序号}")
    role: Role
    message_type: MessageType
    content: Optional[str] = Field(default=None, description="tool_call 时为 null")
    timestamp: str = Field(..., description="ISO 8601 时间戳")

    # 本项目增量字段
    step_no: Optional[int] = Field(default=None, ge=1, description="从 1 递增；由 SDK 统一分配")
    latency_ms: Optional[int] = Field(default=None, ge=0)

    # 同一 turn_id 下的多条 user_query：第一条是初始输入，其余标记为追问
    is_follow_up: Optional[bool] = Field(
        default=None, description="仅 user_query 有意义；True 表示该轮内的追问/补充"
    )

    # 工具相关
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None

    # 待规范文档确认的增量字段
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    status: Optional[Status] = None
    error: Optional[str] = None
    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    model: Optional[str] = None
    tags: Optional[Dict[str, str]] = None
    meta: Optional[Dict[str, Any]] = None

    @field_validator("message_id")
    @classmethod
    def _validate_message_id_format(cls, value: str) -> str:
        # 只校验格式，不校验序号连续性（历史数据不一致，见 2.2）
        if not MESSAGE_ID_PATTERN.match(value):
            raise ValueError(
                "message_id 格式非法: %r，应为 msg_{sys|usr|rsn|cal|res|ans}_{数字}" % value
            )
        return value

    @model_validator(mode="after")
    def _validate_contract(self) -> "Step":
        expected_role = MESSAGE_TYPE_META[self.message_type][1]
        if self.role != expected_role:
            raise ValueError(
                "role 与 message_type 不一致: message_type=%s 应配 role=%s，实际 role=%s"
                % (self.message_type.value, expected_role.value, self.role.value)
            )
        if self.message_type is MessageType.TOOL_CALL:
            if not self.tool_calls:
                raise ValueError("message_type=tool_call 时 tool_calls 必填且非空")
        if self.message_type is MessageType.TOOL_RESPONSE and not self.tool_call_id:
            raise ValueError("message_type=tool_response 时 tool_call_id 必填")
        if self.is_follow_up and self.message_type is not MessageType.USER_QUERY:
            raise ValueError("is_follow_up 只能出现在 message_type=user_query 的 step 上")
        return self

    def serializable(self) -> Dict[str, Any]:
        """序列化为上报/导出用的字典，丢弃空值字段。"""
        return self.model_dump(mode="json", exclude_none=True, by_alias=True)

class Trace(BaseModel):
    """一次 agent 运行 = 一个 turn = 一轮交互，是采集/校验/上报的基本单位。

    `turn_id` 由业务方传入，是 trace 的唯一标识（不再单独引入 trace_id）。
    `session_id` 同样由业务方传入，用于关联同一会话的多个 turn。
    """

    model_config = ConfigDict(extra="allow")

    turn_id: str = Field(..., min_length=1, description="一轮交互的唯一标识，业务方传入")
    session_id: str = Field(..., min_length=1, description="会话标识，业务方传入")
    agent_name: Optional[str] = None
    agent_version: Optional[str] = None
    env: Optional[str] = None

    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: Optional[int] = Field(default=None, ge=0)
    status: Optional[Status] = None
    error: Optional[str] = None

    model: Optional[str] = None
    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)

    tags: Optional[Dict[str, str]] = None
    meta: Optional[Dict[str, Any]] = None

    conversation: List[Step] = Field(default_factory=list, description="该轮的全部步骤")

    @model_validator(mode="after")
    def _validate_turn(self) -> "Trace":
        if not self.conversation:
            raise ValueError("conversation 不能为空：一个 trace 至少包含一个 step")

        # 边界由 turn_id 决定：同一 turn_id 下的多条 user_query 属于同一轮，
        # 按数组顺序区分——第一条是初始输入，其余是追问/补充，不做切分。
        # 多轮对话的切分只能通过 turn_id（见 ingest.split_by_turn）。
        user_queries = [
            step for step in self.conversation if step.message_type is MessageType.USER_QUERY
        ]
        if not user_queries:
            raise ValueError("一个 trace 至少要包含一个 user_query（该轮的用户输入）")
        if self.conversation[0].message_type not in (
            MessageType.USER_QUERY,
            MessageType.SYSTEM_PROMPT,
        ):
            raise ValueError(
                "trace 的第一步应为 system_prompt 或 user_query，实际是 %s"
                % self.conversation[0].message_type.value
            )

        # step_no 必须从 1 起连续递增
        for index, step in enumerate(self.conversation, start=1):
            if step.step_no is not None and step.step_no != index:
                raise ValueError(
                    "step_no 必须从 1 递增：第 %d 个 step 的 step_no=%s" % (index, step.step_no)
                )
        return self

    @classmethod
    def from_raw(
        cls,
        payload: Dict[str, Any],
        conversation_key: str = "conversation",
    ) -> "Trace":
        """从上报/导入的裸字典构造 Trace；兼容 messages 与 conversation 两种键名。

        若 step 缺少 step_no，按数组顺序自动补 1..N。
        """
        data = dict(payload)
        raw_steps = None
        for key in (conversation_key, "conversation", "messages"):
            if isinstance(data.get(key), list):
                raw_steps = data.pop(key)
                break
        if raw_steps is None:
            raise ValueError("未找到 conversation / messages 数组")

        steps: List[Dict[str, Any]] = []
        for index, raw in enumerate(raw_steps, start=1):
            step = dict(raw)
            step.setdefault("step_no", index)
            steps.append(step)

        data["conversation"] = steps
        return cls.model_validate(data)

    def serializable(self) -> Dict[str, Any]:
        """序列化为上报/导出用的字典，丢弃空值字段。"""
        dumped = self.model_dump(mode="json", exclude_none=True, by_alias=True)
        dumped["conversation"] = [step.serializable() for step in self.conversation]
        return dumped
