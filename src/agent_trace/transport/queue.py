"""内存队列 + 后台 flush 线程：采集路径完全非阻塞。"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Sequence

from .client import HttpTransport, TransportError
from .spool import SpoolStore

logger = logging.getLogger("agent_trace.transport")


class BatchQueue:
    """非阻塞批次队列。

    `put()` 只入队立即返回；后台线程按 batch_size / flush_interval 组批发送。
    队列满时丢弃最旧数据并计数（不阻塞业务，对齐计划文档 M3）。
    """

    def __init__(
        self,
        transport: HttpTransport,
        *,
        batch_size: int = 20,
        flush_interval: float = 5.0,
        max_queue_size: int = 1000,
        spool: Optional[SpoolStore] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self.transport = transport
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.1, float(flush_interval))
        self.max_queue_size = max(1, int(max_queue_size))
        self.spool = spool
        self.on_error = on_error

        self._queue: deque = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 统计
        self.dropped = 0
        self.sent = 0
        self.spooled = 0

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="agent-trace-flush", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None

    # ---------- 入队 ----------

    def put(self, trace: Dict[str, Any]) -> None:
        """入队一条 trace。永不阻塞、永不抛异常。"""
        with self._lock:
            if len(self._queue) >= self.max_queue_size:
                self._queue.popleft()
                self.dropped += 1
            self._queue.append(trace)
            full = len(self._queue) >= self.batch_size
        if full:
            self._wake.set()

    # ---------- 后台线程 ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.flush_interval)
            self._wake.clear()
            try:
                self.flush_once()
            except Exception as exc:  # 后台线程绝不能因异常退出
                logger.exception("flush 线程异常: %s", exc)
                if self.on_error:
                    self.on_error(exc)

    def flush_once(self) -> int:
        """取一批发送；返回本批条数。"""
        batch = self._take()
        if not batch:
            return 0
        try:
            self.transport.send(batch)
            self.sent += len(batch)
            return len(batch)
        except TransportError as exc:
            # 发送失败：落盘等待恢复后补发，绝不阻塞或抛出到业务
            if self.spool is not None and self.spool.save(batch) is not None:
                self.spooled += len(batch)
            logger.warning("上报失败，已落盘 %d 条: %s", len(batch), exc)
            if self.on_error:
                self.on_error(exc)
            return 0

    def _take(self) -> List[Dict[str, Any]]:
        with self._lock:
            count = min(len(self._queue), self.batch_size)
            batch = [self._queue.popleft() for _ in range(count)]
        return batch

    # ---------- spool 补发 ----------

    def resend_spooled(self) -> int:
        """把 spool 里积压的批次补发出去；返回成功补发的条数。"""
        if self.spool is None:
            return 0
        resent = 0
        for path in self.spool.pending():
            try:
                traces = self.spool.read(path)
            except (OSError, ValueError) as exc:
                logger.warning("spool 文件损坏，丢弃 %s: %s", path, exc)
                self.spool.discard(path)
                continue
            try:
                self.transport.send(traces)
            except TransportError:
                break  # 仍未恢复，保持顺序，下次再补
            self.spool.discard(path)
            resent += len(traces)
        self.sent += resent
        return resent
