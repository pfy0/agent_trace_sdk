"""脱敏层回归测试：三层防线（敏感键名 / 文本正则 / 高熵串）。

运行：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agent_trace import (  # noqa: E402
    EntropyRule,
    MemoryExporter,
    RedactionRule,
    Redactor,
    Tracer,
    default_rules,
    is_sensitive_key,
)


class SensitiveKeyTest(unittest.TestCase):
    def test_single_token_keys(self) -> None:
        for key in ("password", "passwd", "pwd", "secret", "token", "authorization", "cookie"):
            self.assertTrue(is_sensitive_key(key), key)

    def test_prefixed_and_camel_case_keys(self) -> None:
        for key in ("MYSQL_PASSWORD", "auth_token", "api_key", "apiKey", "accessKey",
                    "private_key", "client_secret", "sessionId"):
            self.assertTrue(is_sensitive_key(key), key)

    def test_non_secret_keys_are_not_flagged(self) -> None:
        for key in ("max_tokens", "token_count", "token_usage", "password_length",
                    "keyword", "user", "username", "passport", "api_version"):
            self.assertFalse(is_sensitive_key(key), key)

    def test_extra_tokens_extend_defaults(self) -> None:
        self.assertFalse(is_sensitive_key("employee_no"))
        self.assertTrue(is_sensitive_key("employee_no", extra_tokens=("employee_no",)))

    def test_cjk_keys_are_supported(self) -> None:
        # 中文键名不应被切词丢掉；``工号`` / ``身份证`` 可命中
        redactor = Redactor(sensitive_keys=["工号", "身份证"])
        self.assertEqual(redactor.redact_value({"工号": "E12345"}), {"工号": "[REDACTED:secret_key]"})
        self.assertEqual(
            redactor.redact_value({"身份证": "110101199001011234"}),
            {"身份证": "[REDACTED:secret_key]"},
        )
        self.assertEqual(redactor.redact_value({"name": "alice"}), {"name": "alice"})


class StructuredValueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = Redactor()

    def test_password_key_value_is_fully_replaced(self) -> None:
        result = self.redactor.redact_value({"password": "hunter2"})
        self.assertEqual(result["password"], "[REDACTED:secret_key]")

    def test_nested_secret_dict_is_replaced_as_a_whole(self) -> None:
        # 值是 dict 时也要整段擦掉（不能只擦字符串叶子）
        result = self.redactor.redact_value({"credentials": {"user": "a", "pass": "b"}})
        self.assertEqual(result["credentials"], "[REDACTED:secret_key]")

    def test_list_of_dicts_is_walked(self) -> None:
        result = self.redactor.redact_value([{"token": "abc"}, {"safe": "value"}])
        self.assertEqual(result[0]["token"], "[REDACTED:secret_key]")
        self.assertEqual(result[1]["safe"], "value")

    def test_non_secret_keys_are_preserved(self) -> None:
        result = self.redactor.redact_value({"max_tokens": 512, "user": "alice"})
        self.assertEqual(result, {"max_tokens": 512, "user": "alice"})

    def test_redact_keys_can_be_disabled(self) -> None:
        redactor = Redactor(redact_keys=False)
        result = redactor.redact_value({"password": "hunter2"})
        self.assertEqual(result["password"], "hunter2")


class TextRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = Redactor()

    def test_assignment_forms(self) -> None:
        cases = {
            "password=hunter2": "password=[REDACTED:secret_assignment]",
            "password='hunter2'": "password='[REDACTED:secret_assignment]'",
            '{"password": "hunter2"}': '{"password": "[REDACTED:secret_assignment]"}',
            "token: abc123": "token: [REDACTED:secret_assignment]",
            "MYSQL_PASSWORD=Sup3rS3cret!": "MYSQL_PASSWORD=[REDACTED:secret_assignment]",
        }
        for text, expected in cases.items():
            self.assertEqual(self.redactor.redact_text(text), expected, text)

    def test_max_tokens_is_not_redacted(self) -> None:
        self.assertEqual(self.redactor.redact_text("max_tokens=512"), "max_tokens=512")

    def test_known_prefixed_keys(self) -> None:
        self.assertEqual(
            self.redactor.redact_text("key=sk-abcdef1234567890abcdef"),
            "key=[REDACTED:api_key]",
        )

    def test_pii_rules(self) -> None:
        self.assertIn("[REDACTED:phone_cn]", self.redactor.redact_text("手机 13812345678"))
        self.assertIn("[REDACTED:email]", self.redactor.redact_text("a@b.com 联系"))
        self.assertIn(
            "[REDACTED:url_credentials]",
            self.redactor.redact_text("postgres://u:p@host/db"),
        )


class EntropyRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = EntropyRule()

    def test_hex_random_string_is_redacted(self) -> None:
        self.assertEqual(self.rule.apply("c9f4a1b77e2d4c88a1b2c3d4e5f60718"), "[REDACTED:high_entropy]")

    def test_alnum_random_string_is_redacted(self) -> None:
        self.assertEqual(self.rule.apply("Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8"), "[REDACTED:high_entropy]")

    def test_long_english_word_is_kept(self) -> None:
        # 长单词熵不足 / 不含数字，不能误伤
        self.assertEqual(self.rule.apply("aVeryLongNormalEnglishWordHere"), "aVeryLongNormalEnglishWordHere")

    def test_hyphenated_identifier_is_kept(self) -> None:
        self.assertEqual(
            self.rule.apply("some-long-hyphenated-identifier-123456"),
            "some-long-hyphenated-identifier-123456",
        )


class DefaultRulesTest(unittest.TestCase):
    def test_entropy_rule_is_last(self) -> None:
        rules = default_rules()
        self.assertIsInstance(rules[-1], EntropyRule)

    def test_custom_rule_can_be_appended(self) -> None:
        rules = default_rules() + [RedactionRule.build("employee_no", r"EMP-\d{6}")]
        redactor = Redactor(rules)
        self.assertEqual(redactor.redact_text("id EMP-123456"), "id [REDACTED:employee_no]")


class TracerIntegrationTest(unittest.TestCase):
    def test_tool_arguments_are_redacted_before_export(self) -> None:
        memory = MemoryExporter()
        tracer = Tracer(agent_id="a", exporters=[memory])
        with tracer.turn(turn_id="t1", session_id="s") as turn:
            turn.record_user_query("q")
            turn.record_tool_call([("http", {"password": "hunter2", "url": "postgres://u:p@h/db"})])
            turn.record_final_answer("a")
        payload = memory.traces[0].serializable()
        arguments = payload["conversations"][1]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(arguments["password"], "[REDACTED:secret_key]")
        self.assertIn("[REDACTED:url_credentials]", arguments["url"])

    def test_redaction_can_be_disabled(self) -> None:
        memory = MemoryExporter()
        tracer = Tracer(agent_id="a", exporters=[memory], redact=False)
        with tracer.turn(turn_id="t1", session_id="s") as turn:
            turn.record_user_query("手机 13812345678")
            turn.record_final_answer("a")
        payload = memory.traces[0].serializable()
        self.assertIn("13812345678", payload["conversations"][0]["content"])


if __name__ == "__main__":
    unittest.main()
