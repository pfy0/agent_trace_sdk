"""HTTP 导出器：把 trace 非阻塞地送进上报队列。"""
from __future__ import annotations

import atexit
from typing import Any, Dict, Optional

from ..schemas import Trace
from ..exporters.base import Exporter
from .client import HttpTransport
from .queue import BatchQueue
from .spool import SpoolStore

DEFAULT_ENDPOINT = "/api/traces"


class HttpExporter(Exporter):
    """把已校验的 trace 上报到采集服务。

    `export()` 只入队后立即返回，网络 IO 全在后台线程完成，
    因此不会拖慢业务方的 agent。

    参数：
        endpoint: 采集服务地址，如 `http://collector:8080/api/traces`。
        headers: 额外请求头（鉴权 token 等）。
        batch_size / flush_interval: 组批策略。
        spool_dir: 失败落盘目录；None 表示失败即丢弃。
        auto_start: 构造时即启动后台线程（默认 True）。
    """

    def __init__(
        self,
        endpoint: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        batch_size: int = 20,
        flush_interval: float = 5.0,
        max_queue_size: int = 1000,
        max_retries: int = 3,
        timeout: float = 10.0,
        spool_dir: Optional[str] = None,
        auto_start: bool = True,
        register_atexit: bool = True,
        on_error: Any = None,
    ) -> None:
        transport = HttpTransport(
            endpoint,
            headers=headers,
            timeout=timeout,
            max_retries=max_retries,
        )
        spool = SpoolStore(spool_dir) if spool_dir else None
        self.queue = BatchQueue(
            transport,
            batch_size=batch_size,
            flush_interval=flush_interval,
            max_queue_size=max_queue_size,
            spool=spool,
            on_error=on_error,
        )
        self.endpoint = endpoint
        self._closed = False
        if auto_start:
            self.queue.start()
        if register_atexit:
            atexit.register(self.close)

    def export(self, trace: Trace) -> None:
        if self._closed:
            return
        self.queue.put(trace.serializable())

    def flush(self) -> None:
        if self._closed:
            return
        while self.queue.flush_once() > 0:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.queue.stop()

    @property
    def stats(self) -> Dict[str, int]:
        return {
            "sent": self.queue.sent,
            "dropped": self.queue.dropped,
            "spooled": self.queue.spooled,
        }
