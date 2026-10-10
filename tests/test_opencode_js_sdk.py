"""JS SDK 一致性离线测试（collector.js 归一 → 与 Python Trace 契约比对）。

不需要 opencode：喂入真实事件 fixture（tests/fixtures/opencode/*.jsonl），
用 Node 跑 `integrations/opencode/session-collector.js` 归一，再把结果交给
Python 的 `Trace.model_validate`（extra="forbid"）严格校验。
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "opencode"
RUNNER = Path(__file__).parent / "js_run_collector.mjs"
sys.path.insert(0, str(ROOT / "src"))

from agent_trace.schemas import Trace  # noqa: E402


def _needs_node():
    if shutil.which("node") is None:
        pytest.skip("node 不可用")


def run_js(fixture: str) -> list:
    _needs_node()
    proc = subprocess.run(
        ["node", str(RUNNER), str(FIXTURES / fixture)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_tool_turn_matches_python_contract():
    traces = run_js("events_tool.jsonl")
    assert len(traces) == 1
    t = traces[0]
    Trace.model_validate(t)  # 严格校验（多余/缺失字段都会抛）
    types = [s["message_type"] for s in t["conversations"]]
    assert types[0] == "user_query"
    assert "tool_call" in types and "tool_response" in types
    assert types[-1] == "final_answer"
    assert t["tool_stats"] == {"shell": {"total": 1, "success": 1, "failure": 0}}
    assert t["toolsets_used"] == ["shell"]
    assert t["token_usage"]["total_tokens"] > 0
    assert t["completed"] is True
    res = [s for s in t["conversations"] if s["message_type"] == "tool_response"][0]
    call = [s for s in t["conversations"] if s["message_type"] == "tool_call"][0]
    assert res["parent_span_id"] == call["span_id"]
    assert res["tool_call_id"] == call["tool_calls"][0]["id"]
    assert res["tool_success"] == 1


def test_text_turn_matches_python_contract():
    traces = run_js("events_text.jsonl")
    assert len(traces) == 1
    t = traces[0]
    Trace.model_validate(t)
    types = [s["message_type"] for s in t["conversations"]]
    assert types == ["user_query", "reasoning", "final_answer"]
    assert t.get("tool_stats") is None  # prune 丢空值，键不存在
    assert t["token_usage"]["total_tokens"] > 0


def test_turn_and_session_id_shape():
    """契约形态：turn_id 为 turn_<32位hex>（与 Python generate_id("turn") 同形）；
    session_id 保留 OpenCode 原生的 ses_xxx（不伪造，与 DeerFlow 取 thread_id 同理）。"""
    traces = run_js("events_tool.jsonl")
    t = traces[0]
    assert re.fullmatch(r"turn_[0-9a-f]{32}", t["turn_id"]), t["turn_id"]
    assert t["session_id"].startswith("ses_"), t["session_id"]
    # 跨轮不重叠：每轮一个新 turn_id，但共享同一 session_id
    for trace in traces:
        assert re.fullmatch(r"turn_[0-9a-f]{32}", trace["turn_id"])


def test_step_fields_exactly_match_schema():
    """序列化键集合必须与 Python Step.serializable() 完全一致（无多余/缺失）。"""
    traces = run_js("events_tool.jsonl")
    t = traces[0]
    py = Trace.model_validate(t).serializable()
    for js_step, py_step in zip(t["conversations"], py["conversations"]):
        assert set(js_step.keys()) == set(py_step.keys()), js_step["step_no"]
    assert set(t.keys()) == set(py.keys())

