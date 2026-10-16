"""上下文传播与 step 装饰器降级行为的回归测试。

重点固化两条契约：
1. contextvars 在同一线程内可 set/get/reset（asyncio 子任务继承）。
2. `Tracer.step` 在拿不到 turn 时**照常执行业务函数**（采集是旁路），
   且只有 debug=True 时才打告警，避免「线程里埋点全丢」无人察觉。

运行：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agent_trace import MemoryExporter, Tracer  # noqa: E402
from agent_trace.context import (  # noqa: E402
    get_current_turn,
    reset_current_turn,
    set_current_turn,
)


class ContextVarBehaviourTest(unittest.TestCase):
    def test_set_get_reset_in_same_thread(self) -> None:
        token = set_current_turn("sentinel")
        self.assertEqual(get_current_turn(), "sentinel")
        reset_current_turn(token)
        self.assertIsNone(get_current_turn())

    def test_not_propagated_to_child_thread(self) -> None:
        # 固化「跨线程不传播」这一事实：子线程看到的是空 Context
        seen = {}
        with Tracer(agent_id="a", exporters=[MemoryExporter()]).turn(turn_id="t", session_id="s"):
            def worker() -> None:
                seen["turn"] = get_current_turn()

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
        self.assertIsNone(seen["turn"])

    def test_propagated_to_asyncio_task(self) -> None:
        # asyncio 子任务创建时自动复制 Context，能拿到当前 turn
        seen = {}
        with Tracer(agent_id="a", exporters=[MemoryExporter()]).turn(turn_id="t", session_id="s"):
            async def child() -> None:
                seen["turn"] = get_current_turn()

            asyncio.run(asyncio.wait_for(child(), timeout=5))
        self.assertIsNotNone(seen["turn"])


class StepDecoratorDegradationTest(unittest.TestCase):
    def _capture(self, tracer: Tracer):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.WARNING)
        logger = logging.getLogger("agent_trace")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        return stream

    def test_executes_without_turn_and_skips_capture(self) -> None:
        memory = MemoryExporter()
        tracer = Tracer(agent_id="a", exporters=[memory])

        @tracer.step("reasoning")
        def work(value: str) -> str:
            return "r:" + value

        # 直接在主线程调用（无 turn）：业务照常返回，但不产生 trace
        self.assertEqual(work("x"), "r:x")
        self.assertEqual(len(memory.traces), 0)

    def test_no_warning_when_debug_disabled(self) -> None:
        memory = MemoryExporter()
        tracer = Tracer(agent_id="a", exporters=[memory], debug=False)
        stream = self._capture(tracer)

        @tracer.step("reasoning")
        def work(value: str) -> str:
            return value

        work("x")
        self.assertEqual(stream.getvalue(), "")

    def test_warning_when_debug_enabled(self) -> None:
        memory = MemoryExporter()
        tracer = Tracer(agent_id="a", exporters=[memory], debug=True)
        stream = self._capture(tracer)

        @tracer.step("reasoning")
        def work(value: str) -> str:
            return value

        work("x")
        self.assertIn("埋点被跳过", stream.getvalue())

    def test_no_warning_on_normal_path(self) -> None:
        memory = MemoryExporter()
        tracer = Tracer(agent_id="a", exporters=[memory], debug=True)
        stream = self._capture(tracer)

        @tracer.step("reasoning")
        def work(value: str) -> str:
            return "r:" + value

        with tracer.turn(turn_id="t", session_id="s") as turn:
            turn.record_user_query("Q")
            work("main")
            turn.record_final_answer("A")
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(
            [step.message_type.value for step in memory.traces[0].conversations.messages],
            ["user_query", "reasoning", "final_answer"],
        )


if __name__ == "__main__":
    unittest.main()
