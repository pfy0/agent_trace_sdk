"""DeerFlow 集成的 token 归属回归测试。

覆盖 `AgentTraceMiddleware._emit_message` 对「一次 LLM 调用」的 token 挂载：
一次调用必有一处携带 token，绝不丢失，也不产生空白噪声 step。

场景：
1. 有文本 + tool_calls -> reasoning 带 token（原行为）
2. 纯文本（无 tool_calls）-> reasoning 带 token
3. 文本为空白（如 "\n\n"）+ tool_calls -> 不落空 reasoning，token 挂到 tool_call
4. 文本为空、无 tool_calls -> 不落任何 step，token 自然丢弃（无归属）

运行：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agent_trace import MemoryExporter, Tracer  # noqa: E402
from agent_trace.integrations.deerflow import AgentTraceMiddleware  # noqa: E402
from agent_trace.schemas import MessageType  # noqa: E402


class _AIMessage:
    """最小 AIMessage 替身：只带集成读取的属性。"""

    type = "ai"

    def __init__(self, content, usage=None, tool_calls=None) -> None:
        self.content = content
        self.usage_metadata = usage
        self.tool_calls = tool_calls or []


class _Turn:
    def __init__(self, session_id: str = "s", turn_id: str = "turn-x") -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.turn_no = 1


class _Runtime:
    def __init__(self, session_id: str = "s", turn_id: str = "turn-x") -> None:
        self.turn = _Turn(session_id, turn_id)
        self.context = {"run_id": turn_id, "thread_id": session_id}


def _usage(pt, ct):
    return {"input_tokens": pt, "output_tokens": ct}


class TokenAttachmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = MemoryExporter()
        self.tracer = Tracer(agent_id="a", exporters=[self.memory])
        self.middleware = AgentTraceMiddleware(self.tracer)

    def _run_ai(self, message, tool_calls=None, session_id="s", run_id="turn-x"):
        """开一轮 -> 喂一条 AI 消息 -> 取该轮已落 step（不提交）。"""
        runtime = _Runtime(session_id, run_id)
        state = {"messages": []}
        self.middleware.before_agent(state, runtime)
        entry = self.middleware._turns[runtime.turn.turn_id]
        turn = entry["turn"]
        self.middleware._emit_message(turn, message, entry.setdefault("pending_final_ids", set()))
        return turn

    def test_text_with_tool_calls_attaches_token_to_reasoning(self) -> None:
        msg = _AIMessage(
            "我先搜索",
            usage=_usage(10, 5),
            tool_calls=[{"id": "c1", "name": "web_search", "args": {"q": "x"}}],
        )
        turn = self._run_ai(msg)
        reasoning = turn.steps[-2]
        tool_call = turn.steps[-1]
        self.assertIs(reasoning.message_type, MessageType.REASONING)
        self.assertEqual((reasoning.prompt_tokens_delta, reasoning.completion_tokens_delta), (10, 5))
        self.assertIs(tool_call.message_type, MessageType.TOOL_CALL)
        self.assertIsNone(tool_call.prompt_tokens_delta)

    def test_plain_text_attaches_token_to_reasoning(self) -> None:
        turn = self._run_ai(_AIMessage("答案", usage=_usage(7, 3)))
        self.assertEqual(len(turn.steps), 1)
        step = turn.steps[0]
        self.assertIs(step.message_type, MessageType.REASONING)
        self.assertEqual((step.prompt_tokens_delta, step.completion_tokens_delta), (7, 3))

    def test_whitespace_text_with_tool_calls_folds_token_into_tool_call(self) -> None:
        # 真实场景：模型只吐 "\n\n" 再发起 4 个 tool_calls
        msg = _AIMessage(
            "\n\n",
            usage=_usage(16424, 252),
            tool_calls=[{"id": "c1", "name": "web_search", "args": {"q": "x"}}],
        )
        turn = self._run_ai(msg)
        # 不应出现空白 reasoning；只剩一条 tool_call 且带着 token
        self.assertEqual([s.message_type for s in turn.steps], [MessageType.TOOL_CALL])
        step = turn.steps[0]
        self.assertEqual((step.prompt_tokens_delta, step.completion_tokens_delta), (16424, 252))

    def test_empty_text_without_tool_calls_produces_no_step(self) -> None:
        turn = self._run_ai(_AIMessage("", usage=_usage(1, 1)))
        self.assertEqual(turn.steps, [])


if __name__ == "__main__":
    unittest.main()
