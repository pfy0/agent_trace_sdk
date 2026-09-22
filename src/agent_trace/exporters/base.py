"""导出层抽象。所有导出器实现 `export(trace)`，可选 `flush()` / `close()`。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List

from ..schemas import Trace


class Exporter(ABC):
    """导出器基类。一个 Tracer 可挂多个导出器。"""

    @abstractmethod
    def export(self, trace: Trace) -> None:
        """消费一条已校验的 trace。实现里应自行处理异常，避免影响业务。"""
        raise NotImplementedError

    def flush(self) -> None:
        """把缓冲数据落盘/发送。默认无缓冲，空实现。"""

    def close(self) -> None:
        """释放资源，默认调用 flush。"""
        self.flush()


class MemoryExporter(Exporter):
    """内存导出器：把 trace 留在列表里，便于测试与本地查看。"""

    def __init__(self) -> None:
        self.traces: List[Trace] = []

    def export(self, trace: Trace) -> None:
        self.traces.append(trace)

    def clear(self) -> None:
        self.traces.clear()

    def serialized(self) -> List[Any]:
        return [trace.serializable() for trace in self.traces]
