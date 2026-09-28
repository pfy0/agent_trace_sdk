"""脱敏层：在 trace 落盘/上报前，按可配规则擦除敏感信息。

默认规则覆盖常见凭据与个人标识（API key、密码、手机号、邮箱、身份证、
银行卡号）。业务方可以：

- 用 :func:`default_rules` 取默认规则再增删；
- 传 ``enabled=False`` 整体关闭；
- 通过 ``include_content`` / ``include_arguments`` 分别控制
  ``step.content`` 与 ``tool_calls[].function.arguments``；
- 通过 ``redact_keys=False`` 关闭「按敏感键名整段擦除」；
- 用 ``sensitive_keys`` 追加自定义敏感键名，或自定义 :class:`RedactionRule`。

三层防线（前一层兜住后一层认不出的）：

1. **敏感键名**：``tool_calls[].function.arguments`` 里 key 命中 ``password`` /
   ``token`` / ``api_key`` 等时，其值**整段**替换，不看值的形态。结构化凭据
   （``{"password": "hunter2"}``）不符合任何前缀特征，只有这一层能兜住。
2. **文本正则**：``API_KEY=sk-abc`` / JWT / 邮箱 / 手机号等已知形态。
3. **高熵串**（:class:`EntropyRule`）：没有已知前缀、但字符分布像随机密钥的
   长 token。按字符集分别用 Shannon 熵判定，并要求「同时含字母和数字」，
   避免误伤普通长单词。

命中后只替换敏感片段本身，保留键名等上下文，便于排查「哪一类泄露过」，
又不会把明文带走。例如 ``API_KEY=sk-abc123`` -> ``API_KEY=[REDACTED:api_key]``。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Pattern, Protocol, Tuple

# 占位符前缀；带规则名便于定位被擦除的内容类型
PLACEHOLDER_TEMPLATE = "[REDACTED:%s]"

# 敏感键名命中后，其值整段替换成这个占位符
KEY_PLACEHOLDER = "[REDACTED:secret_key]"


class Rule(Protocol):
    """脱敏规则的最小接口：名字 + 应用。:class:`RedactionRule` /
    :class:`EntropyRule` 都满足它，业务方可只依赖该接口自定义规则。"""

    name: str

    def placeholder(self) -> str:  # pragma: no cover - 协议声明
        ...

    def apply(self, text: str) -> str:  # pragma: no cover - 协议声明
        ...


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


#: 十六进制字符集；用于区分「hex 随机串」（熵上限约 4 bit/char）与
#: 「base64/alnum 随机串」（熵上限约 6 bit/char），两者阈值不同。
_HEX_CHARS = frozenset("0123456789abcdefABCDEF")

#: 高熵候选串的字符集（字母数字 + base64 的 ``+/=``）；刻意不含 ``-`` / ``_``
#: 等连字符，避免把 ``a-very-long-hyphenated-identifier`` 这类整串当成候选。
#: 该字符集写死在 :class:`EntropyRule` 的正则里（长度由 min_length 拼接）。


def _shannon_entropy(text: str) -> float:
    """按字符出现频率算 Shannon 熵（bit/char）。"""
    if not text:
        return 0.0
    length = len(text)
    counts = Counter(text)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


class EntropyRule:
    """高熵串规则：抓「没有已知前缀」的随机密钥。

    与 :class:`RedactionRule` 的区别是判定不能只靠正则——同一串是否敏感取决于
    字符分布。这里按字符集分开设阈值（hex 随机串理论上限约 4 bit/char，
    base64 约 6 bit/char），并要求候选串「同时含字母和数字」，把普通长单词
    挡在外面。
    """

    name = "high_entropy"

    def __init__(
        self,
        min_length: int = 20,
        hex_entropy: float = 3.0,
        generic_entropy: float = 4.5,
        replacement: Optional[str] = None,
    ) -> None:
        self.min_length = max(8, int(min_length))
        self.hex_entropy = float(hex_entropy)
        self.generic_entropy = float(generic_entropy)
        self._replacement = replacement or (PLACEHOLDER_TEMPLATE % self.name)
        self._pattern = re.compile(
            r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{%d,}(?![A-Za-z0-9+/=])" % self.min_length
        )

    def placeholder(self) -> str:
        return self._replacement

    def apply(self, text: str) -> str:
        if not text:
            return text

        def _replace(match: "re.Match[str]") -> str:
            token = match.group(0)
            # 要求同时含字母和数字：纯字母的长串几乎都是普通单词/标识符
            if not (any(char.isdigit() for char in token) and any(char.isalpha() for char in token)):
                return token
            entropy = _shannon_entropy(token)
            threshold = self.hex_entropy if all(char in _HEX_CHARS for char in token) else self.generic_entropy
            return self._replacement if entropy >= threshold else token

        return self._pattern.sub(_replace, text)


#: 单个词即视为敏感的键名（按 ``-`` / ``_`` / ``.`` 等分隔符切词后比较，
#: 所以 ``MYSQL_PASSWORD`` / ``auth_token`` 都能命中）。
_SENSITIVE_KEY_TOKENS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "secret",
        "token",
        "credential",
        "credentials",
        "authorization",
        "bearer",
        "cookie",
    }
)

#: 需要相邻两词合并后才命中的组合键名（``api`` + ``key`` -> ``apikey``）。
_SENSITIVE_KEY_COMBOS = frozenset(
    {
        "apikey",
        "accesskey",
        "privatekey",
        "secretkey",
        "clientsecret",
        "sessionid",
    }
)

#: 计量 / 描述性后缀：``token_count`` / ``token_usage`` / ``password_length`` 这类
#: 键名描述的是**数量或属性**而不是凭据本身，不该整段擦除。仅当键名由多词组成
#: 且以这些词收尾时才豁免（单 ``token`` 键仍然敏感）。
_NON_SECRET_QUALIFIERS = frozenset(
    {
        "count",
        "counts",
        "num",
        "number",
        "limit",
        "size",
        "usage",
        "len",
        "length",
        "total",
        "sum",
        "type",
        "name",
        "id",
        "ids",
        "index",
        "idx",
        "enabled",
        "flag",
        "version",
        "ver",
        "status",
        "mode",
        "time",
        "timestamp",
        "date",
        "max",
        "min",
        "avg",
        "mean",
    }
)


def is_sensitive_key(key: str, extra_tokens: Iterable[str] = ()) -> bool:
    """判断字典键名是否表示敏感字段。

    切词后任一单词命中 ``_SENSITIVE_KEY_TOKENS``（可被 ``extra_tokens`` 扩充），
    或相邻词拼起来命中 ``_SENSITIVE_KEY_COMBOS``，即视为敏感。

    刻意不做子串匹配；多词键名以计量/描述性后缀收尾时豁免（``token_count``、
    ``token_usage`` 记录的是数量而非凭据）。单 ``token`` 这类键仍然敏感。
    """
    # 用 ``\W``（Unicode 语义）切词：中文等非 ASCII 字母保留在词内，
    # 只按 ``_`` / ``-`` / ``.`` 等标点分隔，``工号_no`` -> ["工号", "no"]。
    parts = [part for part in re.split(r"[\W_]+", key.lower()) if part]
    if not parts:
        return False
    # 额外词与配置键名同等归一：``employee_no`` -> {"employee", "no"}，
    # 这样调用方不必关心分隔符写法（``employeeNo`` 同样命中）。
    extra = {
        piece
        for token in extra_tokens
        for piece in re.split(r"[\W_]+", str(token).lower())
        if piece
    }
    tokens = _SENSITIVE_KEY_TOKENS.union(extra)
    if len(parts) > 1 and parts[-1] in _NON_SECRET_QUALIFIERS:
        # 末词是计量/描述词：整键按「非凭据」处理，避免 token_count 误伤；
        # 但组合键（如单 sessionid 不在此分支）不受影响。
        return False
    if any(part in tokens for part in parts):
        return True
    if "".join(parts) in _SENSITIVE_KEY_COMBOS or "".join(parts) in extra:
        return True
    return any(
        parts[index] + parts[index + 1] in _SENSITIVE_KEY_COMBOS
        for index in range(len(parts) - 1)
    )


def default_rules() -> List[Rule]:
    """常见敏感信息规则（凭据 + 个人标识 + 高熵串）。

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
        # 值两侧允许引号：``password='hunter2'`` / ``"token": "abc"`` 也能命中。
        RedactionRule(
            name="secret_assignment",
            pattern=re.compile(
                r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key))"
                r"([\"']?)(\s*[:=]\s*)([\"']?)([^\s\"'`,;]+)([\"']?)"
            ),
            replacement=r"\1\2\3\4[REDACTED:secret_assignment]\6",
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
        # --- 高熵串：没有已知前缀的随机密钥（放最后，避免抢在具名规则前） ---
        EntropyRule(),
    ]


class Redactor:
    """按规则列表擦除敏感信息；可分别控制 content 与 tool_calls.arguments。"""

    def __init__(
        self,
        rules: Optional[Iterable[Rule]] = None,
        *,
        enabled: bool = True,
        include_content: bool = True,
        include_arguments: bool = True,
        redact_keys: bool = True,
        sensitive_keys: Optional[Iterable[str]] = None,
    ) -> None:
        self.rules: List[Rule] = list(rules) if rules is not None else default_rules()
        self.enabled = bool(enabled)
        self.include_content = bool(include_content)
        self.include_arguments = bool(include_arguments)
        # 按敏感键名整段擦除（结构化凭据的最后一道防线）
        self.redact_keys = bool(redact_keys)
        self.sensitive_keys: Tuple[str, ...] = tuple(sensitive_keys or ())

    def redact_text(self, text: Optional[str]) -> Optional[str]:
        """对单个字符串跑全部规则；非字符串原样返回。"""
        if not self.enabled or not isinstance(text, str) or not text:
            return text
        result = text
        for rule in self.rules:
            result = rule.apply(result)
        return result

    def redact_value(self, value: Any, _key: Optional[str] = None) -> Any:
        """递归处理任意结构：字符串走规则，dict/list 逐层深入。

        ``_key`` 是当前值在父字典里的键名；命中敏感键名时**整段**替换，
        不再往下递归（值可能是 dict/list，需连子结构一起擦掉）。
        """
        if not self.enabled:
            return value
        if _key is not None and self.redact_keys and is_sensitive_key(_key, self.sensitive_keys):
            return KEY_PLACEHOLDER
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {key: self.redact_value(item, str(key)) for key, item in value.items()}
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
