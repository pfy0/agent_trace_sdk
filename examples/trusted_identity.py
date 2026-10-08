"""可信身份注入示例：多用户共用一个 agent 时怎么正确采集 user_id。

场景：一个 agent 服务被多个企业用户通过前端使用。user_id / app_id /
system_code 这类「身份属性」必须由**服务端**在请求入口用凭证解出，再注入采集，
而不是靠环境变量（那是进程级、所有人共用）。

本示例演示三种注入方式，并用 MemoryExporter 打印最终落库字段与来源。
不依赖 DeerFlow / LangGraph，纯 SDK 可跑。
"""
from __future__ import annotations

from agent_trace import MemoryExporter, Tracer


# ---- 模拟：服务端用 API key / SSO token 解出的身份 ----------------------------

def resolve_from_credential(api_key: str) -> dict:
    """真实实现里应查库 / 验证 SSO token；这里用假表演示。"""
    fake_db = {
        "key-alice": {"user_id": "26037050", "app_id": "App_X", "system_code": "IDM"},
        "key-bob": {"user_id": "26037888", "app_id": "App_X", "system_code": "IDM"},
    }
    return fake_db.get(api_key, {})


# ---- 方式一：请求入口解出身份，按轮注入（推荐） -------------------------------

def serve_request(api_key: str, question: str) -> None:
    memory = MemoryExporter()
    tracer = Tracer(
        agent_id="agent-1",
        app_name="小海经典版",       # app 维度可以走 config（同一 agent 固定）
        exporters=[memory],
    )
    trusted = resolve_from_credential(api_key)

    with tracer.turn(
        turn_id="turn-1",
        session_id="sess-1",
        trusted_identity=trusted,     # 可信层：覆盖客户端自报 / 环境变量
        source="web",                 # 请求属性：客户端上报，凭证解不出
    ) as turn:
        turn.record_user_query(question)
        turn.record_final_answer("……")

    trace = memory.traces[0]
    print("user_id   =", trace.user_id)
    print("app_id    =", trace.app_id)
    print("source    =", trace.source)
    print("provenance=", trace.metadata["identity_provenance"])


# ---- 方式二：user_resolver 钩子，按请求自动解析 --------------------------------

def serve_with_resolver(api_key: str, question: str) -> None:
    memory = MemoryExporter()
    tracer = Tracer(
        agent_id="agent-1",
        user_resolver=lambda: resolve_from_credential(api_key),
        exporters=[memory],
    )
    with tracer.turn(turn_id="turn-2", session_id="sess-2", source="web") as turn:
        turn.record_user_query(question)
        turn.record_final_answer("……")

    trace = memory.traces[0]
    print("user_id   =", trace.user_id, "(via user_resolver)")
    print("untrusted =", trace.metadata.get("untrusted_identity_fields"))


if __name__ == "__main__":
    print("== 方式一：按轮注入 ==")
    serve_request("key-alice", "我的假期余额还剩多少？")
    print("\n== 方式二：user_resolver ==")
    serve_with_resolver("key-bob", "帮我查一下报销进度")
