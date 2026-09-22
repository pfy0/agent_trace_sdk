"""传输层：把采集好的 trace 可靠上报到采集服务（Collector）。

设计要点（对齐计划文档 M3）：
- 采集路径完全非阻塞：`export()` 只入队，立即返回，绝不等待网络。
- 后台线程批量 flush：按 batch size / flush interval 触发。
- 指数退避重试；失败落盘（spool），恢复后补发。
- 队列满即丢并计数，宁可丢数据也不拖慢业务。
- 传输依赖优先标准库（urllib），`httpx` 可用时自动启用。
"""
from __future__ import annotations

from .client import HttpTransport, TransportError, UrllibPoster
from .queue import BatchQueue
from .spool import SpoolStore
from .exporter import HttpExporter

__all__ = [
    "HttpExporter",
    "HttpTransport",
    "TransportError",
    "UrllibPoster",
    "BatchQueue",
    "SpoolStore",
]
