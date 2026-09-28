"""Agent 运行轨迹采集 SDK。

最小用法：

    from agent_trace import Tracer, JsonExporter

    tracer = Tracer(agent_id="agent-demo-1", app_name="news-daily",
                    exporters=[JsonExporter("traces.json")])

    with tracer.turn(turn_id="t-1", session_id="s-1") as turn:
        turn.record_user_query("查看今日ai新闻热点")
        turn.record_reasoning("先搜索今日 AI 新闻……")
        turn.record_tool_call([("web_search", {"count": 8})])
        turn.record_tool_response(tool_call_id="call_1", content="{...}")
        turn.record_final_answer("今日要闻……")
"""
from __future__ import annotations

from .config import Config
from .context import get_current_turn
from .exporters import (
    Exporter,
    HttpExporter,
    JsonExporter,
    JsonlExporter,
    MemoryExporter,
    XlsxExporter,
)
from .redact import EntropyRule, RedactionRule, Redactor, default_rules, is_sensitive_key
from .schemas import (
    MESSAGE_TYPE_META,
    MessageType,
    Role,
    Step,
    ToolCall,
    Trace,
    generate_id,
    generate_message_id,
    iso_timestamp,
)
from .tracer import Tracer, Turn
from .transport import TransportError

__version__ = "0.4.4"

__all__ = [
    "__version__",
    "Tracer",
    "Turn",
    "Config",
    "Trace",
    "Step",
    "ToolCall",
    "MessageType",
    "Role",
    "MESSAGE_TYPE_META",
    "Exporter",
    "MemoryExporter",
    "JsonExporter",
    "JsonlExporter",
    "XlsxExporter",
    "HttpExporter",
    "TransportError",
    "Redactor",
    "RedactionRule",
    "EntropyRule",
    "default_rules",
    "is_sensitive_key",
    "get_current_turn",
    "generate_id",
    "generate_message_id",
    "iso_timestamp",
]
