"""装饰器与异常路径示例。

运行：
    python examples/decorator.py
"""
from __future__ import annotations

from agent_trace import MemoryExporter, Tracer


def main() -> None:
    memory = MemoryExporter()
    tracer = Tracer(agent_name="demo-agent", env="local", exporters=[memory])

    @tracer.step("reasoning")
    def plan(question: str) -> str:
        return "拆解问题：%s" % question

    @tracer.step("tool_call")
    def search(keyword: str) -> dict:
        # tool_call 步骤的返回值需是工具调用规格：name + arguments
        return {"name": "web_search", "arguments": {"query": keyword, "count": 5}}

    @tracer.trace
    def answer(question: str) -> dict:
        plan(question)
        search(question)
        return {"answer": "这是最终回答"}

    answer("2026 世界人工智能大会的主题是什么", turn_id="turn-demo-2", session_id="sess-demo-1")

    trace = memory.traces[0].serializable()
    print("status :", trace["status"], "| steps:", len(trace["conversation"]))
    for step in trace["conversation"]:
        preview = step.get("content") or step.get("tool_calls")
        print("  %d. %-14s %s" % (step["step_no"], step["message_type"], str(preview)[:50]))

    # 异常路径：异常向外抛出，同时落一条 error 终止 step
    try:
        with tracer.turn(turn_id="turn-demo-3", session_id="sess-demo-1") as turn:
            turn.record_user_query("触发一个错误")
            raise RuntimeError("工具调用超时")
    except RuntimeError as exc:
        print("\n异常已捕获:", exc)

    failed = memory.traces[1].serializable()
    print("status :", failed["status"], "| error:", failed["error"])
    print("最后一步:", failed["conversation"][-1]["message_type"], failed["conversation"][-1].get("status"))


if __name__ == "__main__":
    main()
