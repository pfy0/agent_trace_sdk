"""工具返回体的成败判定（跨集成共用，单一事实来源）。

不同框架的集成（DeerFlow / OpenCode / …）都需要回答同一个问题：
一次工具调用到底**成功 / 失败 / 判不出**。判定口径必须一致，否则下游
`tool_stats` 会因集成而异。这里把它收敛成一处，返回三值：

    1  成功
    0  失败
   -1  未知（不猜）

两层判定（与 v0.5.1 起的 DeerFlow 口径一致）：

1. **显式成败**：返回体 JSON **顶层**带 ``success`` / ``ok`` /
   ``is_success`` / ``succeeded`` 布尔值 -> 按它判定；
2. **兜底**：
   - 文本以 ``Error`` 开头（工具失败约定）-> ``0``；
   - 能解析为 JSON 对象、只是没带成败键（结构化结果本身就说明跑通了，
     如搜索结果 ``{"query": ..., "results": [...]}``）-> ``1``；
   - 其余 -> ``-1``（未知），**不猜**。

框架自带的 ``status`` 一律不采信：LangChain 无论成败都显式置
``"success"``，采信它会把失败记成成功；OpenCode 的 tool part 状态语义
也随版本变化，原子返回体才是稳定依据。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

#: 工具返回体里表示成败的顶层布尔键。业务工具常直接返回
#: ``{"success": true/false}`` / ``{"ok": true/false}`` 这类结构化结果。
#:
#: **只看顶层键**：嵌套在 ``data`` 里的 ``ok`` 说的是业务子状态（如
#: ``status: draft``），不能代表这次工具调用本身的成败。
JSON_VERDICT_KEYS = ("success", "ok", "is_success", "succeeded")

TRUE_STRINGS = frozenset({"true", "1", "yes"})
FALSE_STRINGS = frozenset({"false", "0", "no"})

#: 第二层用：文本以 ``Error`` 开头即视为失败。DeerFlow 各工具失败时统一
#: ``return f"Error: {e}"``（全仓 20+ 处一致），业务工具也沿用这个写法。
#: 只认「Error + 分隔符」开头，避免把 ``errors are common`` 这类正常输出误判。
ERROR_PREFIXES = ("error:", "error -", "error —")


def parse_json_object(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """把文本解析成 JSON 对象；不是对象则返回 None。

    沙箱可能在 JSON 之后追加诊断文本，此时退一步只取首个 JSON 对象。
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError):
        end = stripped.rfind("}")
        if end <= 0:
            return None
        try:
            payload = json.loads(stripped[: end + 1])
        except (TypeError, ValueError):
            return None
    return payload if isinstance(payload, dict) else None


def json_verdict(payload: Optional[Dict[str, Any]]) -> Optional[int]:
    """第一层：从 JSON 对象顶层取**显式**成败布尔值，返回 1 / 0 / None。

    只认真正的布尔值（或 ``"true"`` / ``"false"`` 这类字符串写法）；
    没有这些键时返回 None，交由第二层判定。
    """
    if not isinstance(payload, dict):
        return None
    for key in JSON_VERDICT_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in TRUE_STRINGS:
                return 1
            if lowered in FALSE_STRINGS:
                return 0
    return None


def looks_like_error_text(text: Optional[str]) -> bool:
    """文本是否以 ``Error`` 开头（工具失败约定）。"""
    if not text:
        return False
    lowered = text.lstrip().lower()
    return any(lowered.startswith(prefix) for prefix in ERROR_PREFIXES)


def judge_tool_success(text: Optional[str], *, json_payload: Any = None) -> int:
    """判定一次工具调用是否成功，返回 -1（未知）/ 0（失败）/ 1（成功）。

    ``text`` 是工具返回体的文本形态；``json_payload`` 允许调用方直接传
    已经解析好的 JSON 对象（如 OpenCode 的事件里本就带结构化结果），
    传了就以它为准，避免二次解析。判定顺序与文档一致：显式成败 > Error
    文本 > 可解析为对象即成功 > 未知。
    """
    if isinstance(json_payload, dict):
        payload: Optional[Dict[str, Any]] = json_payload
    elif json_payload is not None:
        # 显式给了非对象 JSON（数组 / 标量）：不作为结构化成功的依据
        payload = None
    else:
        payload = parse_json_object(text)
    verdict = json_verdict(payload)
    if verdict is not None:
        return verdict
    if looks_like_error_text(text):
        return 0
    if payload is not None:
        return 1
    return -1
