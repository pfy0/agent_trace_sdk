"""可插拔导出器集合。

XLSX 导出器的 openpyxl 依赖是懒加载的：模块导入本身不需要 openpyxl，
只有在真正调用 `write_xlsx` 时才检查依赖。
"""
from __future__ import annotations

from .base import Exporter, MemoryExporter
from .json import JsonExporter
from .jsonl import JsonlExporter
from .xlsx import XlsxExporter, trace_to_rows, write_xlsx

__all__ = [
    "Exporter",
    "MemoryExporter",
    "JsonExporter",
    "JsonlExporter",
    "XlsxExporter",
    "trace_to_rows",
    "write_xlsx",
]
