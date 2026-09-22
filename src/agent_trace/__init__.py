"""Agent 运行轨迹采集 SDK。

最小用法：

    from agent_trace import Tracer, JsonExporter

    tracer = Tracer(agent_name="news-daily", env="prod",
                    exporters=[JsonExporter("traces.json")])

    with tracer.turn(turn_id="t-1", session_id="s-1") as turn:
        turn.record_user_query("查看今日ai新闻热点")
        turn.record_reasoning("先搜索今日 AI 新闻……")
        turn.record_tool_call(name="web_search", arguments={"count": 8})
        turn.record_tool_response(tool_call_id="call_1", content="{...}")
        turn.record_final_answer("今日要闻……")
"""
from __future__ import annotations

from .config import Config
from .context import (
    get_baggage,
    get_current_span_id,
    get_current_turn,
    set_baggage,
)
from .exporters import (
    Exporter,
    JsonExporter,
    JsonlExporter,
    MemoryExporter,
    XlsxExporter,
)
from .ingest import import_payload, split_by_turn
from .schemas import (
    MESSAGE_TYPE_META,
    MessageType,
    Role,
    Status,
    Step,
    ToolCall,
    Trace,
    generate_id,
    generate_message_id,
    iso_timestamp,
)
from .tracer import Tracer, Turn
from .utils import stringify

__version__ = "0.1.0"

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
    "Status",
    "MESSAGE_TYPE_META",
    "Exporter",
    "MemoryExporter",
    "JsonExporter",
    "JsonlExporter",
    "XlsxExporter",
    "get_current_turn",
    "get_current_span_id",
    "get_baggage",
    "set_baggage",
    "generate_id",
    "generate_message_id",
    "iso_timestamp",
    "stringify",
    "import_payload",
    "split_by_turn",
]
