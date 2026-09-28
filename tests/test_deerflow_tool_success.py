"""DeerFlow `tool_success` 判定回归测试。

两层判定：

1. **显式成败**：返回体 JSON 顶层带 `success` / `ok` / `is_success` / `succeeded`
   （布尔值或 `"true"`/`"false"` 字符串）-> 按它判定，为真 `1`、为假 `0`；
2. **兜底**：
   - 文本以 `Error` 开头（DeerFlow / 业务工具的失败约定）-> `0`；
   - 能解析为 JSON 对象、只是没带成败键 -> `1`；
   - 其余 -> `-1`（未知）。

不采信框架的 `ToolMessage.status`（LangChain 无论成败都显式置 `"success"`）。

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


class FirstLayerExplicitVerdictTest(unittest.TestCase):
    """第一层：JSON 顶层显式的成败布尔值。"""

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

    def test_explicit_verdict_wins_over_error_text(self) -> None:
        # 第一层优先于第二层
        text = json.dumps({"success": True}) + "\nError: trailing noise"
        self.assertEqual(_tool_success(self._msg(), text), 1)

    def test_json_with_trailing_diagnostic_still_parsed(self) -> None:
        text = json.dumps({"success": False}) + "\nExit Code: 1\n"
        self.assertEqual(_tool_success(self._msg(), text), 0)


class SecondLayerFallbackTest(unittest.TestCase):
    """第二层：没有显式成败键时的兜底。"""

    def _msg(self, status: str = "success") -> _Message:
        return _Message(status)

    def test_json_without_verdict_key_is_success(self) -> None:
        # 搜索结果这类结构化结果：没带 success，但能解析成对象即说明跑通了
        payload = {
            "query": "中国代表团 名古屋亚运会 半程",
            "total_results": 5,
            "results": [{"title": "中国军团一枝独秀", "url": "https://m.gmw.cn/x.htm"}],
        }
        self.assertEqual(_tool_success(self._msg(), json.dumps(payload, ensure_ascii=False)), 1)

    def test_error_prefixed_text_is_failure(self) -> None:
        # 真实样本：Jina 抓取超时
        text = "Error: Request to Jina API failed: ConnectTimeout: "
        self.assertEqual(_tool_success(self._msg(), text), 0)

    def test_error_prefix_is_case_insensitive_and_lstrip(self) -> None:
        self.assertEqual(_tool_success(self._msg(), "  error: boom"), 0)
        self.assertEqual(_tool_success(self._msg(), "ERROR: boom"), 0)

    def test_error_substring_in_body_is_not_failure(self) -> None:
        # 只有开头才算：正文里出现 error 不算失败
        self.assertEqual(_tool_success(self._msg(), "the errors are common\n"), -1)

    def test_error_dash_forms_are_failure(self) -> None:
        self.assertEqual(_tool_success(self._msg(), "Error - boom"), 0)

    def test_plain_text_is_unknown(self) -> None:
        self.assertEqual(_tool_success(self._msg(), "just some output\n"), -1)

    def test_non_object_json_is_unknown(self) -> None:
        # 数组不是对象，不算结构化结果
        self.assertEqual(_tool_success(self._msg(), json.dumps([1, 2, 3])), -1)

    def test_empty_text_is_unknown(self) -> None:
        self.assertEqual(_tool_success(self._msg(), ""), -1)

    def test_framework_status_error_is_not_trusted(self) -> None:
        # 框架 status="error" 不参与判定：纯文本仍是 -1
        self.assertEqual(_tool_success(_Message("error"), "whatever"), -1)


if __name__ == "__main__":
    unittest.main()