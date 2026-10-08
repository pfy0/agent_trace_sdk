"""可信身份解析：把「服务端鉴权解出的身份」与「客户端自报的值」分开。

背景：公司轨迹规范里 `user_id` / `app_id` / `app_name` / `system_code` 属于
**身份属性**——同一把凭证下它们不变，应由服务端从可信凭证（API key / SSO token /
网关鉴权）解出；而 `source` 属于**请求属性**——每次请求都可能不同，只能客户端上报，
服务端最多做值域校验或按入口推导。

本模块提供统一的解析入口，让调用方按「越可信越优先」的顺序取值，并记录每个字段
最终来自哪一层，便于服务端审计「这条 trace 的身份到底可不可信」。

优先级（越靠前越可信）：

    1. trusted   —— 服务端鉴权 / 网关注入 / 已解出的凭证，**可信**
    2. explicit  —— 业务方在代码里显式传入（如 `tracer.turn(user_id=...)`）
    3. reported  —— 客户端 / 前端上报的原始值，**不可信，仅作兜底**
    4. default   —— 环境变量 / Tracer 级默认值

`source` 不走可信层：它只有 explicit / reported / default，因为凭证解不出它。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

# 身份属性：可由服务端凭证解析，可信层有意义
IDENTITY_FIELDS: tuple = ("app_name", "app_id", "system_code", "user_id")
# 请求属性：凭证解不出，只能上报 / 服务端按入口推导
REQUEST_FIELDS: tuple = ("source",)
# 全部参与「可信解析」的业务字段
RESOLVABLE_FIELDS: tuple = IDENTITY_FIELDS + REQUEST_FIELDS

_FIELD_LABELS: Dict[str, str] = {
    "app_name": "应用名称",
    "app_id": "应用 ID",
    "system_code": "系统编码",
    "user_id": "用户 ID",
    "source": "事件来源",
}


def is_identity_field(field: str) -> bool:
    """该字段是否属于「身份属性」（可被服务端鉴权解析）。"""
    return field in IDENTITY_FIELDS


def is_trusted_field(field: str) -> bool:
    """该字段是否允许来自可信层（凭证）。`source` 这类请求属性为 False。"""
    return field in IDENTITY_FIELDS


def field_label(field: str) -> str:
    """字段的中文名，用于日志/文档。"""
    return _FIELD_LABELS.get(field, field)


def _clean(value: Any) -> Optional[str]:
    """规范化取值：None / 空白 / 非字符串一律按「没提供」处理。"""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text or None


class ResolvedField:
    """单个字段的解析结果：值 + 来源。"""

    __slots__ = ("field", "value", "origin", "trusted")

    def __init__(self, field: str, value: str, origin: str, trusted: bool) -> None:
        self.field = field
        self.value = value
        self.origin = origin  # trusted / explicit / reported / default
        self.trusted = trusted

    def as_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "origin": self.origin, "trusted": self.trusted}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "ResolvedField(%s=%r, origin=%r, trusted=%r)" % (
            self.field,
            self.value,
            self.origin,
            self.trusted,
        )


class ResolvedIdentity:
    """一组业务字段的解析结果。

    - `values`：字段 -> 最终值（未取到的字段不出现）。
    - `provenance`：字段 -> 来源与是否可信，可写入 trace.metadata 供服务端审计。
    """

    def __init__(self, fields: Mapping[str, ResolvedField]) -> None:
        self._fields: Dict[str, ResolvedField] = dict(fields)

    @property
    def values(self) -> Dict[str, str]:
        return {name: item.value for name, item in self._fields.items()}

    def get(self, field: str) -> Optional[str]:
        item = self._fields.get(field)
        return item.value if item else None

    def origin(self, field: str) -> Optional[str]:
        item = self._fields.get(field)
        return item.origin if item else None

    def is_trusted(self, field: str) -> bool:
        item = self._fields.get(field)
        return bool(item and item.trusted)

    def provenance(self) -> Dict[str, Dict[str, Any]]:
        return {name: item.as_dict() for name, item in self._fields.items()}

    def untrusted_fields(self, candidates: Iterable[str] = IDENTITY_FIELDS) -> list:
        """身份属性里「有值但不可信」的字段——服务端应重点关注这些。"""
        return [
            name
            for name in candidates
            if self._fields.get(name) is not None and not self._fields[name].trusted
        ]

    def __contains__(self, field: str) -> bool:
        return field in self._fields

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "ResolvedIdentity(%r)" % (self.values,)


def resolve_identity(
    *,
    trusted: Optional[Mapping[str, Any]] = None,
    explicit: Optional[Mapping[str, Any]] = None,
    reported: Optional[Mapping[str, Any]] = None,
    default: Optional[Mapping[str, Any]] = None,
    fields: Iterable[str] = RESOLVABLE_FIELDS,
) -> ResolvedIdentity:
    """按信任优先级解析一组业务字段。

    典型用法（服务端接收入口）：

        resolved = resolve_identity(
            trusted=credential_claims,   # {user_id, app_id, ...} 服务端解出
            reported=client_payload,     # 客户端上报的原始字段
        )
        tracer.turn(session_id=..., **resolved.values)

    身份属性：trusted > explicit > reported > default。
    请求属性（source）：explicit > reported > default（trusted 层被忽略）。
    """
    trusted = trusted or {}
    explicit = explicit or {}
    reported = reported or {}
    default = default or {}

    resolved: Dict[str, ResolvedField] = {}
    for field in fields:
        candidates = []
        if is_trusted_field(field):
            candidates.append(("trusted", trusted.get(field)))
        candidates.append(("explicit", explicit.get(field)))
        candidates.append(("reported", reported.get(field)))
        candidates.append(("default", default.get(field)))

        for origin, raw in candidates:
            value = _clean(raw)
            if value is not None:
                resolved[field] = ResolvedField(
                    field, value, origin, trusted=(origin == "trusted")
                )
                break
    return ResolvedIdentity(resolved)


__all__ = [
    "IDENTITY_FIELDS",
    "REQUEST_FIELDS",
    "RESOLVABLE_FIELDS",
    "ResolvedField",
    "ResolvedIdentity",
    "resolve_identity",
    "is_identity_field",
    "is_trusted_field",
    "field_label",
]
