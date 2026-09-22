"""HTTP 上报客户端：标准库优先，httpx 可选。"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger("agent_trace.transport")

DEFAULT_TIMEOUT = 10.0


class TransportError(RuntimeError):
    """上报失败。`retryable` 标识是否值得重试（网络类错误为 True）。"""

    def __init__(self, message: str, *, retryable: bool = True, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class UrllibPoster:
    """基于标准库的 POST 实现，无第三方依赖。"""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, headers: Optional[Dict[str, str]] = None) -> None:
        self.timeout = timeout
        self.headers = dict(headers or {})

    def post(self, url: str, payload: Any) -> Tuple[int, str]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        headers.update(self.headers)
        req = urlrequest.Request(url, data=body, headers=headers, method="POST")
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urlerror.HTTPError as exc:
            # 4xx 多为请求本身有问题，重试无意义；5xx / 429 值得重试
            retryable = exc.code >= 500 or exc.code == 429
            raise TransportError(
                "HTTP %s: %s" % (exc.code, exc.reason), retryable=retryable, status=exc.code
            ) from exc
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise TransportError("网络错误: %s" % exc, retryable=True) from exc


class HttpxPoster:
    """基于 httpx 的实现（可选依赖，装了 httpx 时更省资源）。"""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, headers: Optional[Dict[str, str]] = None) -> None:
        import httpx  # noqa: F401  (延迟导入，未安装时不影响其他路径)

        self._client = httpx.Client(timeout=timeout, headers=dict(headers or {}))

    def post(self, url: str, payload: Any) -> Tuple[int, str]:
        import httpx

        try:
            resp = self._client.post(url, json=payload)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise TransportError("网络错误: %s" % exc, retryable=True) from exc
        retryable = resp.status_code >= 500 or resp.status_code == 429
        if resp.status_code >= 400:
            raise TransportError(
                "HTTP %s" % resp.status_code, retryable=retryable, status=resp.status_code
            )
        return resp.status_code, resp.text

    def close(self) -> None:
        self._client.close()


def build_poster(
    timeout: float = DEFAULT_TIMEOUT,
    headers: Optional[Dict[str, str]] = None,
    prefer_httpx: bool = True,
) -> Any:
    """优先用 httpx（若可用），否则退回标准库。"""
    if prefer_httpx:
        try:
            return HttpxPoster(timeout=timeout, headers=headers)
        except ImportError:
            pass
    return UrllibPoster(timeout=timeout, headers=headers)


class HttpTransport:
    """带指数退避重试的上报通道。"""

    def __init__(
        self,
        endpoint: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        prefer_httpx: bool = True,
        sleep: Any = None,
    ) -> None:
        self.endpoint = endpoint
        self.max_retries = max(1, int(max_retries))
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._poster = build_poster(timeout=timeout, headers=headers, prefer_httpx=prefer_httpx)
        self._sleep = sleep or _default_sleep

    def send(self, traces: Sequence[Dict[str, Any]]) -> None:
        """批量上报；重试耗尽后抛出 TransportError。"""
        payload = {"traces": list(traces)}
        last: Optional[TransportError] = None
        for attempt in range(self.max_retries):
            try:
                self._poster.post(self.endpoint, payload)
                return
            except TransportError as exc:
                last = exc
                if not exc.retryable or attempt == self.max_retries - 1:
                    raise
                delay = min(self.backoff_base * (2 ** attempt), self.backoff_max)
                logger.debug("上报失败（第 %d 次），%.1fs 后重试: %s", attempt + 1, delay, exc)
                self._sleep(delay)
        if last is not None:  # pragma: no cover - 循环内已 raise
            raise last

    def close(self) -> None:
        close = getattr(self._poster, "close", None)
        if callable(close):
            close()


def _default_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
