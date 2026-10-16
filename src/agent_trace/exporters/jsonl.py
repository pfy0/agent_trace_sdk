"""JSONL 导出器：一行一个 step 的裸 JSON。

便于用命令行工具（jq、grep）直接处理，也方便后续灌 ClickHouse。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..schemas import Trace
from .base import Exporter


class JsonlExporter(Exporter):
    """一行一个 step 写入 JSONL；每个 step 行带上所属 trace 的元数据。"""

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        ensure_ascii: bool = False,
        include_trace_meta: bool = True,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.ensure_ascii = ensure_ascii
        self.include_trace_meta = include_trace_meta
        self._lines: List[Dict[str, Any]] = []
        self._handle = None
        self._lock = threading.Lock()
        if self.path is not None and self.path.exists():
            self.path.unlink()

    def _rows(self, trace: Trace) -> List[Dict[str, Any]]:
        payload = trace.serializable()
        conversation = payload.pop("conversations", {}) or {}
        rows: List[Dict[str, Any]] = []
        for step in conversation.get("messages", []):
            if self.include_trace_meta:
                row = dict(payload)
                row.update(step)
            else:
                row = dict(step)
            rows.append(row)
        return rows

    def export(self, trace: Trace) -> None:
        rows = self._rows(trace)
        if self.path is None:
            with self._lock:
                self._lines.extend(rows)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._handle is None:
            self._handle = self.path.open("a", encoding="utf-8")
        with self._lock:
            for row in rows:
                self._handle.write(json.dumps(row, ensure_ascii=self.ensure_ascii) + "\n")
            self._handle.flush()

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    @property
    def rows(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._lines)
