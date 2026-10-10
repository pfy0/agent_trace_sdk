"""框架适配层：DeerFlow / LangGraph 中间件（进程内采集）。

OpenCode 采用独立的 JS SDK（`integrations/opencode/`），订阅事件流后在
OpenCode 进程内归一，直接写本地 JSON，不经本 Python 包。
"""

__all__ = ["deerflow"]
