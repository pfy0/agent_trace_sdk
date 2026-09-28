"""Trace 级统计字段自动聚合的回归测试。

覆盖 `as_trace()` 里由 SDK 自动填充的 4 个字段：
`toolsets_used` / `tool_stats` / `tool_error_counts` / `token_usage`。

运行：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agent_trace import MemoryExporter, Tracer  # noqa: E402


class StatsAggregationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = MemoryExporter()
        self.tracer = Tracer(agent_id="a", exporters=[self.memory])

    def _trace(self, **turn_kwargs):
        with self.tracer.turn(turn_id="t", session_id="s", **turn_kwargs) as turn:
            yield turn
        return self.memory.traces[-1]

    def test_toolsets_dedup_in_first_seen_order(self) -> None:
        with self.tracer.turn(turn_id="t", session_id="s") as turn:
            turn.record_user_query("q")
            turn.record_tool_call([{"name": "b"}, {"name": "a"}])
            ids = [c.id for c in turn.steps[-1].tool_calls]
            for cid in ids:
                turn.record_tool_response(tool_call_id=cid, content="ok", tool_success=1)
            turn.record_tool_call([{"name": "a"}])
            turn.record_tool_response(
                tool_call_id=turn.steps[-1].tool_calls[0].id, content="ok", tool_success=1
            )
            turn.record_final_answer("a")
        payload = self.memory.traces[0].serializable()
        self.assertEqual(payload["toolsets_used"], ["b", "a"])

    def test_tool_stats_counts_success_failure_unknown(self) -> None:
        with self.tracer.turn(turn_id="t", session_id="s") as turn:
            turn.record_user_query("q")
            outcomes = [1, 0, -1, 1]
            for outcome in outcomes:
                turn.record_tool_call([{"name": "web_search"}])
                turn.record_tool_response(
                    tool_call_id=turn.steps[-1].tool_calls[0].id,
                    content="x",
                    tool_success=outcome,
                )
            turn.record_final_answer("a")
        stats = self.memory.traces[0].serializable()["tool_stats"]
        self.assertEqual(
            stats["web_search"],
            {"total": 4, "success": 2, "failure": 1, "unknown": 1},
        )
        self.assertEqual(
            stats["web_search"]["total"],
            stats["web_search"]["success"] + stats["web_search"]["failure"]
            + stats["web_search"]["unknown"],
        )

    def test_tool_error_counts_keeps_only_failures(self) -> None:
        with self.tracer.turn(turn_id="t", session_id="s") as turn:
            turn.record_user_query("q")
            turn.record_tool_call([{"name": "ok_tool"}])
            turn.record_tool_response(
                tool_call_id=turn.steps[-1].tool_calls[0].id, content="ok", tool_success=1
            )
            turn.record_tool_call([{"name": "bad_tool"}])
            turn.record_tool_response(
                tool_call_id=turn.steps[-1].tool_calls[0].id, content="bad", tool_success=0
            )
            turn.record_final_answer("a")
        payload = self.memory.traces[0].serializable()
        self.assertEqual(payload["tool_error_counts"], {"bad_tool": 1})

    def test_token_usage_sums_deltas(self) -> None:
        with self.tracer.turn(turn_id="t", session_id="s") as turn:
            turn.record_user_query("q")
            turn.record_reasoning("r1", prompt_tokens_delta=10, completion_tokens_delta=5)
            turn.record_reasoning("r2", prompt_tokens_delta=3, completion_tokens_delta=2)
            turn.record_final_answer("a")
        self.assertEqual(
            self.memory.traces[0].serializable()["token_usage"],
            {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20},
        )

    def test_stats_omitted_when_no_tools_or_tokens(self) -> None:
        with self.tracer.turn(turn_id="t", session_id="s") as turn:
            turn.record_user_query("q")
            turn.record_final_answer("a")
        payload = self.memory.traces[0].serializable()
        for key in ("toolsets_used", "tool_stats", "tool_error_counts", "token_usage"):
            self.assertNotIn(key, payload)

    def test_tool_name_falls_back_to_response_step(self) -> None:
        # tool_response 的 tool_name 在拿不到对应 tool_call 时也应计入
        with self.tracer.turn(turn_id="t", session_id="s") as turn:
            turn.record_user_query("q")
            turn.record_tool_response(
                tool_call_id="ghost", content="ok", tool_name="orphan_tool", tool_success=1
            )
            turn.record_final_answer("a")
        payload = self.memory.traces[0].serializable()
        self.assertEqual(payload["toolsets_used"], ["orphan_tool"])
        self.assertEqual(payload["tool_stats"]["orphan_tool"]["success"], 1)


if __name__ == "__main__":
    unittest.main()
