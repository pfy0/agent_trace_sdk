"""DeerFlow `tool_success` 判定回归测试。

判定依据已收敛为**只看工具返回体 JSON 顶层的成败布尔值**：
`success` / `ok` / `is_success` / `succeeded` 为真 -> 1，为假 -> 0；
其余（没有工具调用、不是 JSON、顶层无成败标记）一律 -1（未知）。

不再采信框架的 `ToolMessage.status`（LangChain 无论成败都显式置 `"success"`），
也不再做文本特征匹配（`Exit Code:` / `Error:` / `timeout:` 等已移除）。

运行：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agent_trace.integrations.deerflow import _tool_success  # noqa: E402


class _Message:
    """最小 ToolMessage 替身：status 仍带着，用于证明它**不**参与判定。"""

    def __init__(self, status: str = "success", explicit: bool = True) -> None:
        self.status = status
        self.model_fields_set = {"status"} if explicit else set()


class JsonVerdictTest(unittest.TestCase):
    def _msg(self, status: str = "success") -> _Message:
        return _Message(status)

    def test_real_draft_course_sample_is_success(self) -> None:
        # 真实样本：draft_course 返回 {"success": true, "data": {...}}
        payload = {
            "success": True,
            "data": {"ok": True, "status": "draft", "summary": {"pra_name": "房地产销售实战陪练"}},
        }
        self.assertEqual(_tool_success(self._msg(), json.dumps(payload, ensure_ascii=False)), 1)

    def test_json_success_false_is_failure(self) -> None:
        payload = {"success": False, "error": "课程创建失败"}
        self.assertEqual(_tool_success(self._msg("success"), json.dumps(payload, ensure_ascii=False)), 0)

    def test_json_ok_false_is_failure(self) -> None:
        self.assertEqual(_tool_success(self._msg(), json.dumps({"ok": False})), 0)

    def test_alternate_verdict_keys(self) -> None:
        self.assertEqual(_tool_success(self._msg(), json.dumps({"is_success": True})), 1)
        self.assertEqual(_tool_success(self._msg(), json.dumps({"succeeded": False})), 0)

    def test_string_boolean_is_recognised(self) -> None:
        self.assertEqual(_tool_success(self._msg(), json.dumps({"success": "false"})), 0)
        self.assertEqual(_tool_success(self._msg(), json.dumps({"success": "true"})), 1)

    def test_nested_data_ok_does_not_override_top_level(self) -> None:
        # data.ok=false 说的是业务子状态，不代表本次调用失败
        payload = {"success": True, "data": {"ok": False}}
        self.assertEqual(_tool_success(self._msg(), json.dumps(payload)), 1)

    def test_json_with_trailing_diagnostic_still_parsed(self) -> None:
        text = json.dumps({"success": False}) + "\nExit Code: 1\n"
        self.assertEqual(_tool_success(self._msg(), text), 0)


class UnknownFallbackTest(unittest.TestCase):
    """判不出就记 -1：既不看框架 status，也不做文本特征匹配。"""

    def test_plain_text_is_unknown_even_when_status_success(self) -> None:
        self.assertEqual(_tool_success(_Message("success"), "all good\n"), -1)

    def test_json_without_verdict_key_is_unknown(self) -> None:
        self.assertEqual(_tool_success(_Message(), json.dumps({"courses": ["a", "b"]})), -1)

    def test_non_object_json_is_unknown(self) -> None:
        self.assertEqual(_tool_success(_Message(), json.dumps([1, 2, 3])), -1)

    def test_empty_text_is_unknown(self) -> None:
        self.assertEqual(_tool_success(_Message(), ""), -1)

    def test_error_text_is_no_longer_special_cased(self) -> None:
        # 文本特征已移除：这些过去会判 0 的输入现在记 -1
        for text in (
            "Error: boom",
            "out\nExit Code: 1\n",
            "Command timed out after 30s",
            "timeout: failed to run command '.venv/bin/python': No such file or directory\n",
        ):
            self.assertEqual(_tool_success(_Message("success"), text), -1, text)

    def test_framework_status_error_is_no_longer_special_cased(self) -> None:
        # 框架 status="error" 也不再看：没有 JSON 判定就是 -1
        self.assertEqual(_tool_success(_Message("error"), "whatever"), -1)


if __name__ == "__main__":
    unittest.main()