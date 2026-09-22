"""导入现成数据：多轮 messages 按 turn_id 切分成多条 trace。"""
from __future__ import annotations

import json
from pathlib import Path

from agent_trace import MemoryExporter, Tracer, split_by_turn

SAMPLE = [
    {"turn_id": "t1", "message_id": "msg_usr_1", "role": "user",
     "message_type": "user_query", "content": "第一轮问题", "timestamp": "2026-08-06T01:00:00.000Z"},
    {"turn_id": "t1", "message_id": "msg_ans_1", "role": "assistant",
     "message_type": "final_answer", "content": "第一轮回答", "timestamp": "2026-08-06T01:00:05.000Z"},
    {"turn_id": "t2", "message_id": "msg_usr_1", "role": "user",
     "message_type": "user_query", "content": "第二轮问题", "timestamp": "2026-08-06T01:05:00.000Z"},
    {"turn_id": "t2", "message_id": "msg_ans_1", "role": "assistant",
     "message_type": "final_answer", "content": "第二轮回答", "timestamp": "2026-08-06T01:05:03.000Z"},
]


def main() -> None:
    memory = MemoryExporter()
    tracer = Tracer(agent_id="agent-importer", exporters=[memory])

    traces = split_by_turn({"messages": SAMPLE}, session_id="sess-import", agent_id="agent-importer")
    for trace in traces:
        tracer._emit(trace)

    for trace in memory.traces:
        payload = trace.serializable()
        print("turn_id:", payload["turn_id"], "| turn_no:", payload["turn_no"],
              "| steps:", len(payload["conversations"]))
        print(json.dumps(payload["conversations"], ensure_ascii=False)[:160])

    assert Path(__file__).exists()


if __name__ == "__main__":
    main()
