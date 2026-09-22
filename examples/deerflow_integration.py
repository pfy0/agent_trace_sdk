"""DeerFlow 集成示例：作为插件被加载（推荐），或手动挂中间件。

方式 A（推荐）：在 DeerFlow 的 config.yaml 里启用插件

    plugins:
      - use: agent_trace.integrations.deerflow:install
        config:
          output: "outputs/agent_traces.json"   # 轨迹落盘路径（.jsonl 后缀走 JSONL）
          agent_id: "your-agent-id"
          memory: false                          # true 时同时留一份在内存

方式 B：手动把中间件挂到自己的 agent 上（本示例演示）
"""
from __future__ import annotations

from agent_trace import MemoryExporter, Tracer
from agent_trace.integrations.deerflow import AgentTraceMiddleware


def build_middleware() -> AgentTraceMiddleware:
    """构造采集中间件，供 create_agent(middleware=[...]) 使用。"""
    tracer = Tracer(
        agent_id="agent-demo-1",
        app_name="deer-flow",
        exporters=[MemoryExporter()],
    )
    return AgentTraceMiddleware(tracer)


def main() -> None:
    """无 langchain 环境下也能 import 本模块；有 langchain 时直接可用。"""
    middleware = build_middleware()
    print("中间件已就绪:", type(middleware).__name__)
    print("名称:", middleware.name)

    try:
        from langchain.agents import create_agent  # noqa: F401
    except ImportError:
        print("未安装 langchain，跳过 create_agent 演示。")
        print("在 DeerFlow 环境（已装 langchain）下，插件方式无需本示例。")
        return

    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import tool

    @tool
    def web_search(query: str) -> str:
        """搜索网页。"""
        return '{"results": ["AI 新闻"]}'

    class _FakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):  # type: ignore[override]
            return self

    agent = create_agent(
        model=_FakeModel(responses=[
            AIMessage("我先搜索", tool_calls=[
                {"name": "web_search", "args": {"query": "AI"}, "id": "c1", "type": "tool_call"},
            ]),
            AIMessage("今日要闻：……"),
        ]),
        tools=[web_search],
        middleware=[middleware],
    )

    agent.invoke(
        {"messages": [HumanMessage("查今日 AI 新闻")]},
        config={"configurable": {"thread_id": "thread-demo"}},
        context={"run_id": "run-demo", "thread_id": "thread-demo"},
    )

    trace = middleware.tracer._exporters[0].traces[0].serializable()
    print("\nturn_id:", trace["turn_id"], "| session:", trace["session_id"])
    for step in trace["conversations"]:
        print("  %d. %-14s %s" % (step["step_no"], step["message_type"], (step.get("content") or "")[:28]))


if __name__ == "__main__":
    main()
