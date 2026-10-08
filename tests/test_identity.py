"""可信身份解析（identity）的回归测试。

覆盖：
- `resolve_identity` 的信任优先级（trusted > explicit > reported > default）；
- `source` 这类请求属性不采纳 trusted 层；
- Tracer/Turn 集成后，可信身份能覆盖客户端自报值与 config 默认值；
- provenance 写入 metadata，且标出「有值但不可信」的身份字段；
- `user_resolver` 钩子按请求自动注入可信身份；
- `record_identity_provenance=False` 时不写 metadata。

运行：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agent_trace import (  # noqa: E402
    IDENTITY_FIELDS,
    MemoryExporter,
    Tracer,
    is_identity_field,
    is_trusted_field,
    resolve_identity,
)


class ResolveIdentityUnitTest(unittest.TestCase):
    def test_trusted_beats_explicit_reported_default(self) -> None:
        r = resolve_identity(
            trusted={"user_id": "trusted-user"},
            explicit={"user_id": "explicit-user"},
            reported={"user_id": "reported-user"},
            default={"user_id": "default-user"},
        )
        self.assertEqual(r.get("user_id"), "trusted-user")
        self.assertEqual(r.origin("user_id"), "trusted")
        self.assertTrue(r.is_trusted("user_id"))

    def test_explicit_beats_reported_and_default(self) -> None:
        r = resolve_identity(
            explicit={"user_id": "explicit-user"},
            reported={"user_id": "reported-user"},
            default={"user_id": "default-user"},
        )
        self.assertEqual(r.get("user_id"), "explicit-user")
        self.assertEqual(r.origin("user_id"), "explicit")
        self.assertFalse(r.is_trusted("user_id"))

    def test_reported_beats_default(self) -> None:
        r = resolve_identity(
            reported={"user_id": "reported-user"},
            default={"user_id": "default-user"},
        )
        self.assertEqual(r.get("user_id"), "reported-user")
        self.assertEqual(r.origin("user_id"), "reported")

    def test_default_used_as_last_resort(self) -> None:
        r = resolve_identity(default={"user_id": "default-user"})
        self.assertEqual(r.get("user_id"), "default-user")
        self.assertEqual(r.origin("user_id"), "default")
        self.assertFalse(r.is_trusted("user_id"))

    def test_source_ignores_trusted_layer(self) -> None:
        # source 是请求属性，凭证解不出，trusted 层必须被忽略
        r = resolve_identity(
            trusted={"source": "trusted-source"},
            reported={"source": "web"},
        )
        self.assertEqual(r.get("source"), "web")
        self.assertEqual(r.origin("source"), "reported")
        self.assertFalse(r.is_trusted("source"))

    def test_source_falls_back_to_explicit_then_default(self) -> None:
        self.assertEqual(resolve_identity(explicit={"source": "cli"}).get("source"), "cli")
        self.assertEqual(resolve_identity(default={"source": "api"}).get("source"), "api")

    def test_blank_values_are_ignored(self) -> None:
        r = resolve_identity(
            trusted={"user_id": "   "},
            reported={"user_id": "26037050"},
        )
        self.assertEqual(r.get("user_id"), "26037050")
        self.assertEqual(r.origin("user_id"), "reported")

    def test_missing_field_is_absent(self) -> None:
        r = resolve_identity(reported={})
        self.assertNotIn("user_id", r)
        self.assertIsNone(r.get("user_id"))
        self.assertIsNone(r.origin("user_id"))

    def test_provenance_and_untrusted_report(self) -> None:
        r = resolve_identity(
            trusted={"user_id": "26037050"},
            reported={"app_name": "自报应用"},
        )
        prov = r.provenance()
        self.assertEqual(prov["user_id"]["origin"], "trusted")
        self.assertTrue(prov["user_id"]["trusted"])
        self.assertEqual(prov["app_name"]["origin"], "reported")
        self.assertFalse(prov["app_name"]["trusted"])
        self.assertEqual(r.untrusted_fields(), ["app_name"])

    def test_field_classification_helpers(self) -> None:
        for name in ("user_id", "app_id", "app_name", "system_code"):
            self.assertTrue(is_identity_field(name))
            self.assertTrue(is_trusted_field(name))
        self.assertIn("user_id", IDENTITY_FIELDS)
        self.assertFalse(is_identity_field("source"))
        self.assertFalse(is_trusted_field("source"))


class TracerIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = MemoryExporter()

    def _tracer(self, **kwargs):
        return Tracer(agent_id="a", exporters=[self.memory], **kwargs)

    def _run(self, tracer, **turn_kwargs):
        with tracer.turn(turn_id="t", session_id="s", **turn_kwargs) as turn:
            turn.record_user_query("hi")
        return self.memory.traces[-1]

    def test_trusted_identity_overrides_reported_and_config(self) -> None:
        tracer = self._tracer(app_name="env-app", user_id="env-user", app_id="env-app-id")
        trace = self._run(
            tracer,
            user_id="client-claimed",  # 客户端自报，应被可信层覆盖
            trusted_identity={
                "user_id": "26037050",
                "app_id": "App_X",
                "system_code": "IDM",
            },
        )
        self.assertEqual(trace.user_id, "26037050")
        self.assertEqual(trace.app_id, "App_X")
        self.assertEqual(trace.system_code, "IDM")
        # 未在可信层提供的 app_name 回落到 config 默认
        self.assertEqual(trace.app_name, "env-app")

    def test_reported_is_dropped_when_trusted_present(self) -> None:
        tracer = self._tracer()
        trace = self._run(
            tracer,
            trusted_identity={"user_id": "26037050"},
            user_id="attacker-supplied",
        )
        self.assertEqual(trace.user_id, "26037050")

    def test_source_stays_request_level(self) -> None:
        tracer = self._tracer(source="env-source")
        trace = self._run(
            tracer,
            source="web",
            trusted_identity={"user_id": "26037050", "source": "should-be-ignored"},
        )
        self.assertEqual(trace.source, "web")
        self.assertEqual(trace.user_id, "26037050")

    def test_provenance_written_to_metadata(self) -> None:
        tracer = self._tracer()
        trace = self._run(
            tracer,
            user_id="client-claimed",
            trusted_identity={"user_id": "26037050"},
        )
        prov = trace.metadata["identity_provenance"]
        self.assertEqual(prov["user_id"]["origin"], "trusted")
        self.assertTrue(prov["user_id"]["trusted"])

    def test_untrusted_identity_field_flagged(self) -> None:
        tracer = self._tracer()
        trace = self._run(tracer, user_id="client-claimed")  # 只有自报，没有可信层
        self.assertFalse(trace.metadata["identity_provenance"]["user_id"]["trusted"])
        self.assertIn("user_id", trace.metadata["untrusted_identity_fields"])

    def test_provenance_can_be_disabled(self) -> None:
        tracer = self._tracer()
        trace = self._run(
            tracer,
            trusted_identity={"user_id": "26037050"},
            record_identity_provenance=False,
        )
        self.assertIsNone(trace.metadata)

    def test_user_resolver_injects_trusted_identity(self) -> None:
        tracer = self._tracer(
            user_resolver=lambda: {"user_id": "from-request", "system_code": "IDM"}
        )
        trace = self._run(tracer, user_id="env-user")
        self.assertEqual(trace.user_id, "from-request")
        self.assertEqual(trace.system_code, "IDM")

    def test_user_resolver_failure_is_silent(self) -> None:
        def boom():
            raise RuntimeError("后端取用户失败")

        tracer = self._tracer(user_resolver=boom)
        trace = self._run(tracer, user_id="env-user")
        # 钩子出错不影响采集，回落到显式值
        self.assertEqual(trace.user_id, "env-user")

    def test_tracer_level_trusted_identity(self) -> None:
        tracer = self._tracer(trusted_identity={"user_id": "svc-user", "app_id": "App_S"})
        trace = self._run(tracer)
        self.assertEqual(trace.user_id, "svc-user")
        self.assertEqual(trace.app_id, "App_S")


if __name__ == "__main__":
    unittest.main()
