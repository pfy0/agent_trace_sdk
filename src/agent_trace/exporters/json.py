"""JSON 导出器：把所有 trace 汇总写到一个 JSON 文件。

顶层是 trace 数组，每个元素是一个 trace 信封（含 conversation）。
默认在 export 时增量落盘，保证进程异常退出也不丢已采集数据。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..schemas import Trace
from .base import Exporter


class JsonExporter(Exporter):
    """把 trace 写进单个 JSON 文件（顶层数组）。

    参数：
        path: 目标文件；为 None 时只缓存在内存。
        indent: 格式化缩进，None 表示紧凑输出。
        flush_every: 每采集多少条 trace 落盘一次；1 表示每条都落盘。
    """

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        indent: Optional[int] = 2,
        flush_every: int = 1,
        ensure_ascii: bool = False,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.indent = indent
        self.flush_every = max(1, int(flush_every))
        self.ensure_ascii = ensure_ascii
        self._traces: List[Dict[str, Any]] = []
        self._dirty = 0
        self._lock = threading.Lock()

    def export(self, trace: Trace) -> None:
        payload = trace.serializable()
        with self._lock:
            self._traces.append(payload)
            self._dirty += 1
            should_flush = self.path is not None and self._dirty >= self.flush_every
        if should_flush:
            self.flush()

    def flush(self) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = json.dumps(self._traces, ensure_ascii=self.ensure_ascii, indent=self.indent)
            self._dirty = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    @property
    def traces(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._traces)

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()
            self._dirty = 0
