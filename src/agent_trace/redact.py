"""脱敏层：在 trace 落盘/上报前，按可配规则擦除敏感信息。

默认规则覆盖常见凭据与个人标识（API key、密码、手机号、邮箱、身份证、
银行卡号）。业务方可以：

- 用 :func:`default_rules` 取默认规则再增删；
- 传 ``enabled=False`` 整体关闭；
- 通过 ``include_content`` / ``include_arguments`` 分别控制
  ``step.content`` 与 ``tool_calls[].function.arguments``；
- 自定义 :class:`RedactionRule`（正则 + 替换串）。

命中后只替换敏感片段本身，保留键名等上下文，便于排查「哪一类泄露过」，
又不会把明文带走。例如 ``API_KEY=sk-abc123`` -> ``API_KEY=[REDACTED:api_key]``。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Pattern

# 占位符前缀；带规则名便于定位被擦除的内容类型
PLACEHOLDER_TEMPLATE = "[REDACTED:%s]"


@dataclass(frozen=True)
class RedactionRule:
    """一条脱敏规则：正则 + 占位符名。

    ``pattern`` 用 ``re.compile`` 预编译；``name`` 会出现在替换串
    ``[REDACTED:{name}]`` 里，便于统计「哪类敏感信息出现过」。
    """

    name: str
    pattern: Pattern[str]
    replacement: Optional[str] = None

    @classmethod
    def build(cls, name: str, regex: str, flags: int = 0) -> "RedactionRule":
        return cls(name=name, pattern=re.compile(regex, flags))

    def placeholder(self) -> str:
        return self.replacement or (PLACEHOLDER_TEMPLATE % self.name)

    def apply(self, text: str) -> str:
        return self.pattern.sub(self.placeholder(), text)


def default_rules() -> List[RedactionRule]:
    """常见敏感信息规则（凭据 + 个人标识）。

    刻意保持保守：只在「看起来确实像凭据/身份号」的位置替换，避免误伤
    正常业务文本。
    """
    return [
        # --- 凭据类 ---
        # sk- / apikey- / rk- 等常见 token 前缀，后接足够长的随机串
        RedactionRule.build("api_key", r"\b(?:sk|rk|pk|apikey|api-key)[-_][A-Za-z0-9_\-]{16,}\b", re.IGNORECASE),
        # Langfuse 风格 key：lsv2_pt_<hex>_<hex>
        RedactionRule.build("langfuse_key", r"\b(?:ls|pk|sk)_(?:v\d+_)?[A-Za-z0-9_]{20,}\b"),
        # KEY=value / TOKEN=value / SECRET=value / PASSWORD=value 形式（.env 常见）。
        # 键名允许带前缀（MYSQL_PASSWORD / DEEPSEEK_API_KEY），替换时保留键名与分隔符。
        RedactionRule(
            name="secret_assignment",
            pattern=re.compile(
                r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key))"
                r"(\s*[:=]\s*)([^\s\"'`,;]+)"
            ),
            replacement=r"\1\2[REDACTED:secret_assignment]",
        ),
        # JWT
        RedactionRule.build("jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
        # --- 个人标识 ---
        RedactionRule.build("email", r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        RedactionRule.build("id_card", r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]\b"),
        RedactionRule.build("phone_cn", r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        # 银行卡：仅匹配常见卡号前缀（62/4/5[1-5]/3[47]/9 开头），避免误伤
        # 订单号、金额这类 16-19 位的普通数字。
        RedactionRule.build(
            "bank_card",
            r"(?<!\d)(?:62|4\d|5[1-5]\d|3[47]\d|9\d)\d{13,17}(?!\d)",
        ),
        # URL 内嵌凭据：任意 scheme://user:pass@host（含 postgres / mysql 等）
        RedactionRule(
            name="url_credentials",
            pattern=re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@"),
            replacement=r"\1[REDACTED:url_credentials]@",
        ),
    ]


class Redactor:
    """按规则列表擦除敏感信息；可分别控制 content 与 tool_calls.arguments。"""

    def __init__(
        self,
        rules: Optional[Iterable[RedactionRule]] = None,
        *,
        enabled: bool = True,
        include_content: bool = True,
        include_arguments: bool = True,
    ) -> None:
        self.rules: List[RedactionRule] = list(rules) if rules is not None else default_rules()
        self.enabled = bool(enabled)
        self.include_content = bool(include_content)
        self.include_arguments = bool(include_arguments)

    def redact_text(self, text: Optional[str]) -> Optional[str]:
        """对单个字符串跑全部规则；非字符串原样返回。"""
        if not self.enabled or not isinstance(text, str) or not text:
            return text
        result = text
        for rule in self.rules:
            result = rule.apply(result)
        return result

    def redact_value(self, value: Any) -> Any:
        """递归处理任意结构：字符串走规则，dict/list 逐层深入。"""
        if not self.enabled:
            return value
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {key: self.redact_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_value(item) for item in value]
        return value

    def redact_step(self, step: Any) -> None:
        """就地擦除一个 step 的 content 与 tool_calls.arguments。

        只改这两处：其余字段（message_id / 时间 / span 等）不承载自由文本，
        无需处理。
        """
        if not self.enabled:
            return
        if self.include_content:
            step.content = self.redact_text(step.content)
        if self.include_arguments and step.tool_calls:
            for call in step.tool_calls:
                function = call.function or {}
                if "arguments" in function:
                    function["arguments"] = self.redact_value(function["arguments"])

    def redact_trace(self, trace: Any) -> None:
        """就地擦除整条 trace 的所有 step。"""
        if not self.enabled:
            return
        for step in trace.conversations:
            self.redact_step(step)
