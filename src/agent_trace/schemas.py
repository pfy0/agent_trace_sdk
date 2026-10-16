"""契约层：枚举、字段模型、校验规则、id 生成。

本模块是整个 SDK 的单点契约。所有字段定义集中在 `Step` / `Trace` 两个
pydantic 模型里，schema 演进时只需改这里。

两层数据模型：
- Trace：一次 agent 运行 = 一个 turn = 一轮交互，主键 `turn_id`。
- Step：trace 中的一步，即 `conversations.messages` 数组里的一条消息。
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

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="工具调用 ID，用于与 tool_response 关联")
    type: str = "function"
    function: Dict[str, Any] = Field(default_factory=dict)


class Step(BaseModel):
    """轨迹中的一步 = `conversations.messages` 数组里的一个元素。"""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(..., description="格式 msg_{类型缩写}_{序号}")
    step_no: Optional[int] = Field(default=None, ge=1, description="从 1 递增；由 SDK 统一分配")
    role: Role
    message_type: MessageType
    content: Optional[str] = Field(default=None, description="tool_call 时为 null")

    # 该步的开始 / 结束时刻（ISO 8601 UTC）。无耗时概念的直接埋点，
    # end_time 等于 start_time（瞬时事件）。
    start_time: str = Field(
        default_factory=iso_timestamp, description="开始的 ISO 8601 时间戳（UTC）"
    )
    end_time: Optional[str] = Field(
        default=None, description="结束的 ISO 8601 时间戳（UTC）；缺省时等于 start_time"
    )
    latency_ms: Optional[int] = Field(default=None, ge=0)

    # span 树：每个 step 一个 span_id；父 span 由 SDK 按关联关系自动填。
    # 规则（B）：tool_call 是父，指向它的 tool_response 是子。
    span_id: str = Field(
        default_factory=lambda: generate_id("span"), description="跨度 ID，SDK 自动生成"
    )
    parent_span_id: Optional[str] = Field(default=None, description="父跨度 ID")

    # 同一 turn_id 下的多条 user_query：第一条是初始输入，其余标记为追问
    is_follow_up: Optional[bool] = Field(
        default=None, description="仅 user_query 有意义；True 表示该轮内的追问/补充"
    )

    # 工具相关
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = Field(default=None, description="工具名称，由 SDK 自动回填")
    tool_success: Optional[int] = Field(
        default=None, description="工具调用结果：-1 未知 / 0 失败 / 1 成功"
    )

    # token 增量（调 LLM 的步骤才有）
    prompt_tokens_delta: Optional[int] = Field(default=None, ge=0)
    completion_tokens_delta: Optional[int] = Field(default=None, ge=0)

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
        # 没有结束时刻的瞬时事件（直接 record_* 埋点），end_time 等于 start_time
        if self.end_time is None:
            self.end_time = self.start_time
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
        if self.tool_success is not None and self.tool_success not in (-1, 0, 1):
            raise ValueError("tool_success 取值必须是 -1（未知）/ 0（失败）/ 1（成功）")
        if self.is_follow_up and self.message_type is not MessageType.USER_QUERY:
            raise ValueError("is_follow_up 只能出现在 message_type=user_query 的 step 上")
        return self

    def serializable(self) -> Dict[str, Any]:
        """序列化为上报/导出用的字典，丢弃空值字段。"""
        return self.model_dump(mode="json", exclude_none=True, by_alias=True)


class Conversations(BaseModel):
    """一轮会话内容：`{messages: [Step, ...]}`。

    对外契约形如 ``"conversations": {"messages": [...]}``；先后顺序由每个
    step 的 ``step_no`` 表达，工具调用与返回用 ``tool_calls[].id`` <->
    ``tool_call_id`` 关联。
    """

    model_config = ConfigDict(extra="forbid")

    messages: List[Step] = Field(default_factory=list, description="该轮的全部步骤")

    def serializable(self) -> Dict[str, Any]:
        """序列化为上报/导出用的字典，丢弃空值字段。"""
        return {"messages": [step.serializable() for step in self.messages]}


class Trace(BaseModel):
    """一次 agent 运行 = 一个 turn = 一轮交互，是采集/校验/上报的基本单位。

    `turn_id` 由业务方传入，是 trace 的唯一标识（不再单独引入 trace_id）。
    `session_id` 同样由业务方传入，用于关联同一会话的多个 turn。

    字段与公司《轨迹数据规范》对齐；`id` / `create_time` 由入库侧生成，
    SDK 只留空位。`start_time` / `end_time` 对应采集的起止时刻。
    """

    model_config = ConfigDict(extra="forbid")

    # 入库侧生成（SDK 留空）
    id: Optional[str] = Field(default=None, description="事件唯一 ID（UUIDV7），入库自动生成")
    create_time: Optional[str] = Field(
        default=None, description="入库时间 ISO 8601（UTC），入库自动生成"
    )

    turn_id: str = Field(..., min_length=1, description="回合唯一标识（turn_UUIDV7），业务方传入")
    turn_no: int = Field(
        default=1, ge=1, description="该会话内的第几轮交互，从 1 递增，业务方传入"
    )
    session_id: str = Field(..., min_length=1, description="会话唯一标识（sess_UUIDV7）")

    start_time: Optional[str] = Field(default=None, description="回合开始时间 ISO 8601（UTC）")
    end_time: Optional[str] = Field(default=None, description="回合结束时间 ISO 8601（UTC）")

    # 业务标识
    # 信任模型（见 identity.py）：下面 4 个身份属性应由服务端从可信凭证
    # （API key / SSO / 网关鉴权）解出并覆盖客户端自报值；source 是请求属性，
    # 凭证解不出，只能客户端上报或服务端按入口推导。每条 trace 的字段来源会
    # 写进 metadata.identity_provenance 供审计。
    app_name: Optional[str] = Field(
        default=None, description="项目 / 产品 / 场景标识（身份属性，建议服务端凭证解析）"
    )
    app_id: Optional[str] = Field(
        default=None, description="应用 ID（身份属性，建议服务端凭证解析）"
    )
    system_code: Optional[str] = Field(
        default=None, description="触发事件的系统（ALM 系统 S 码；身份属性，建议服务端凭证解析）"
    )
    user_id: Optional[str] = Field(
        default=None, description="用户 ID（默认工号；身份属性，应由服务端鉴权解析，不信客户端自报）"
    )
    source: Optional[str] = Field(
        default=None, description="事件来源入口，如接口地址（请求属性，凭证解不出，由客户端上报或服务端推导）"
    )

    # 智能体身份（agent_id 来自公司内部智能体注册市场）
    agent_id: Optional[str] = Field(default=None, description="执行此步骤的智能体唯一 ID")
    parent_agent_id: Optional[str] = Field(default=None, description="子智能体对应的父智能体 ID")

    conversations_type: str = Field(
        default="openai_messages",
        description="会话格式标识：openai messages / sharegpt / 自定义 等",
    )
    conversations: Conversations = Field(
        default_factory=lambda: Conversations(),
        description="该轮会话内容，形如 {messages: [...]}",
    )

    completed: bool = Field(default=True, description="该轮是否真正结束")
    partial: bool = Field(default=False, description="是否因超时等外部条件被截断")

    # 统计类
    api_calls: Optional[str] = Field(default=None, description="本回合 API 调用总次数（含重试）")
    toolsets_used: Optional[List[str]] = Field(default=None, description="调用的工具集名称列表")
    tool_stats: Optional[Dict[str, Any]] = Field(default=None, description="各工具调用统计")
    tool_error_counts: Optional[Dict[str, Any]] = Field(default=None, description="各工具错误汇总")
    token_usage: Optional[Dict[str, Any]] = Field(default=None, description="token 消耗汇总")

    metadata: Optional[Dict[str, Any]] = Field(default=None, description="扩展元数据（自定义标签、版本等）")
    extra_info: Optional[Dict[str, Any]] = Field(default=None, description="扩展业务数据（任意额外字段）")

    @model_validator(mode="after")
    def _validate_turn(self) -> "Trace":
        if not self.conversations.messages:
            raise ValueError("conversations.messages 不能为空：一个 trace 至少包含一个 step")

        # 边界由 turn_id 决定：同一 turn_id 下的多条 user_query 属于同一轮，
        # 按数组顺序区分——第一条是初始输入，其余是追问/补充，不做切分。
        # 多轮对话的切分只能通过 turn_id（由业务方保证一轮一个 turn_id）。
        user_queries = [
            step
            for step in self.conversations.messages
            if step.message_type is MessageType.USER_QUERY
        ]
        if not user_queries:
            raise ValueError("一个 trace 至少要包含一个 user_query（该轮的用户输入）")
        if self.conversations.messages[0].message_type not in (
            MessageType.USER_QUERY,
            MessageType.SYSTEM_PROMPT,
        ):
            raise ValueError(
                "trace 的第一步应为 system_prompt 或 user_query，实际是 %s"
                % self.conversations.messages[0].message_type.value
            )

        # step_no 必须从 1 起连续递增
        for index, step in enumerate(self.conversations.messages, start=1):
            if step.step_no is not None and step.step_no != index:
                raise ValueError(
                    "step_no 必须从 1 递增：第 %d 个 step 的 step_no=%s" % (index, step.step_no)
                )
        return self

    def serializable(self) -> Dict[str, Any]:
        """序列化为上报/导出用的字典，丢弃空值字段。"""
        dumped = self.model_dump(mode="json", exclude_none=True, by_alias=True)
        dumped["conversations"] = self.conversations.serializable()
        return dumped
