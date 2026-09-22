"""嵌套 span 示例：用 span 树表达子 agent / 子工具链，而不是另开 trace。

运行：
    python examples/nested_spans.py
"""
from __future__ import annotations

from agent_trace import JsonExporter, MemoryExporter, Tracer


def main() -> None:
    memory = MemoryExporter()
    tracer = Tracer(
        agent_name="lead-agent",
        env="local",
        exporters=[memory, JsonExporter("output/nested_spans.json")],
    )

    with tracer.turn(turn_id="turn-span-1", session_id="sess-span-1") as turn:
        turn.record_user_query("调研今日 AI 新闻并写一份简报")
        turn.record_reasoning("拆解：先调研，再写稿，最后汇总")

        # 子 agent：调研
        with turn.span("agent", name="researcher"):
            turn.record_reasoning("开始检索……")
            with turn.span("tool", name="web_search"):
                turn.record_tool_call([("web_search", {"q": "AI 新闻", "count": 8})])
                turn.record_tool_response(tool_call_id="call_1", content='{"results": "..."}')
            turn.record_final_answer("调研结果：共 8 条要闻……")

        # 子 agent：撰稿
        with turn.span("agent", name="writer"):
            turn.record_reasoning("基于调研结果撰写简报……")
            turn.record_final_answer("简报初稿……")

        turn.record_final_answer("最终简报：……")

    trace = memory.traces[0]
    spans = {step.span_id: step for step in trace.conversation}

    def depth_of(step) -> int:
        depth, cursor = 0, step.parent_span_id
        while cursor and cursor in spans:
            depth += 1
            cursor = spans[cursor].parent_span_id
        return depth

    print("turn_id:", trace.turn_id, "| 步数:", len(trace.conversation))
    for step in trace.conversation:
        meta = step.meta or {}
        label = (
            "<%s:%s>" % (meta.get("span_kind"), meta.get("span_name"))
            if meta.get("span_container")
            else step.message_type.value
        )
        print("  %s%d. %-22s %s" % ("  " * depth_of(step), step.step_no, label, step.span_id[:14]))


if __name__ == "__main__":
    main()
