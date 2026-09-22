"""失败落盘（spool）：网络失败时把批次写到本地文件，恢复后补发。"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union

logger = logging.getLogger("agent_trace.transport")


class SpoolStore:
    """把待重发的批次以文件形式暂存，进程重启后可继续补发。

    每个批次一个文件，文件名为 `{时间戳}-{uuid}.json`，天然按时间有序。
    """

    def __init__(self, directory: Union[str, Path], max_files: int = 1000) -> None:
        self.directory = Path(directory)
        self.max_files = max_files

    def ensure_dir(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, traces: Sequence[Dict[str, Any]]) -> Optional[Path]:
        """写入一个批次；超过上限时丢弃最旧的，避免无限膨胀。"""
        try:
            self.ensure_dir()
            name = "%019d-%s.json" % (int(time.time() * 1000), uuid.uuid4().hex)
            path = self.directory / name
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"traces": list(traces)}, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(path)
            self._trim()
            return path
        except OSError as exc:  # 落盘失败也不能影响业务
            logger.warning("spool 写入失败: %s", exc)
            return None

    def pending(self) -> List[Path]:
        """按时间顺序返回待补发的批次文件。"""
        if not self.directory.is_dir():
            return []
        return sorted(p for p in self.directory.glob("*.json"))

    def read(self, path: Path) -> List[Dict[str, Any]]:
        data = json.loads(path.read_text(encoding="utf-8"))
        traces = data.get("traces") if isinstance(data, dict) else None
        return list(traces or [])

    def discard(self, path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def _trim(self) -> None:
        files = sorted(self.directory.glob("*.json"))
        excess = len(files) - self.max_files
        for path in files[:excess] if excess > 0 else []:
            self.discard(path)
