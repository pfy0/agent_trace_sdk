"""导出语言无关的 JSON Schema。

Java / 其他语言版本以这份文件为准，保证跨语言字段一致。
用法：
    python tools/export_schema.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from agent_trace.schemas import MESSAGE_TYPE_META, MessageType, Role, Status, Trace  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "schema")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)

    trace_schema = Trace.model_json_schema()
    trace_schema["$id"] = "https://github.com/pfy0/agent_trace_sdk/schema/trace.schema.json"
    trace_schema["title"] = "AgentTrace"

    out = os.path.join(OUT_DIR, "trace.schema.json")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(trace_schema, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    print("wrote", out)

    enums = {
        "message_type": {
            "values": [m.value for m in MessageType],
            "abbr": {m.value: MESSAGE_TYPE_META[m][0] for m in MessageType},
            "role": {m.value: MESSAGE_TYPE_META[m][1].value for m in MessageType},
        },
        "role": [r.value for r in Role],
        "status": [s.value for s in Status],
    }
    out2 = os.path.join(OUT_DIR, "enums.json")
    with open(out2, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(enums, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    print("wrote", out2)


if __name__ == "__main__":
    main()
