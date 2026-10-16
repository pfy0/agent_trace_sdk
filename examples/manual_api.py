"""手动埋点最小闭环示例：采集一个 turn 并导出 JSON / JSONL / XLSX。

运行：
    python examples/manual_api.py
"""
from __future__ import annotations

import os
from pathlib import Path

from agent_trace import (
    JsonExporter,
    JsonlExporter,
    MemoryExporter,
    Tracer,
    XlsxExporter,
    get_current_turn,
)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    memory = MemoryExporter()

    tracer = Tracer(
        agent_id="agent-demo-1",
        app_name="news-daily",
        exporters=[
            memory,
            JsonExporter(OUTPUT_DIR / "traces.json"),
            JsonlExporter(OUTPUT_DIR / "steps.jsonl"),
            XlsxExporter(OUTPUT_DIR / "steps.xlsx"),
        ],
    )

    with tracer.turn(turn_id="turn-demo-1", session_id="sess-demo-1", turn_no=1) as turn:
        turn.record_user_query("查看今日 ai 新闻热点")
        turn.record_reasoning("先调用搜索引擎拿到今日 AI 新闻……")

        tool_calls = [
            ("web_search", {"count": 8, "query": "人工智能 AI 新闻 头条", "timeRange": "OneDay"}),
            ("web_search", {"count": 6, "query": "AI 大模型 融资 发布 今日", "timeRange": "OneDay"}),
        ]
        turn.record_tool_call(tool_calls)

        # 模拟并发工具陆续返回：先从上下文取回当前 turn，再逐个回填
        current = get_current_turn()
        assert current is turn
        for call in turn.steps[-1].tool_calls:
            turn.record_tool_response(tool_call_id=call.id, content='{"success": true}')

        with turn.step("final_answer") as step:
            step.set_content("今日 AI 要闻速递：……")

    trace = memory.traces[0].serializable()
    print("turn_id    :", trace["turn_id"])
    print("turn_no    :", trace["turn_no"])
    print("completed  :", trace["completed"], "| partial:", trace["partial"])
    print("steps      :", len(trace["conversations"]["messages"]))
    for step in trace["conversations"]["messages"]:
        print("  %d. %-14s %-14s latency_ms=%s"
              % (step["step_no"], step["message_id"], step["message_type"], step.get("latency_ms")))
    print("导出文件   :", ", ".join(sorted(os.listdir(OUTPUT_DIR))))


if __name__ == "__main__":
    main()
