"""配置：环境变量 + 代码初始化。

优先级：显式传入的配置 > 环境变量 > 默认值。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

ENV_PREFIX = "AGENT_TRACE_"


def _env(name: str) -> Optional[str]:
    return os.environ.get(ENV_PREFIX + name)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Config(BaseModel):
    """SDK 全局配置。所有字段都有默认值，可只用环境变量启动。"""

    model_config = ConfigDict(extra="allow")

    # trace 级默认元数据
    agent_name: Optional[str] = Field(default=None)
    agent_version: Optional[str] = Field(default=None)
    env: Optional[str] = Field(default=None)

    # 实时采集默认值
    default_status: str = "ok"

    # 导出/上报
    exporters: Optional[list] = Field(default=None)

    # 加工层（M4 预留）
    sanitize_enabled: bool = Field(default=True)
    max_field_bytes: int = Field(default=64 * 1024)
    truncate_enabled: bool = Field(default=True)

    # 运行期开关
    enabled: bool = Field(default=True)
    debug: bool = Field(default=False)

    @classmethod
    def from_env(cls, **overrides: Any) -> "Config":
        """从环境变量构造配置，显式 overrides 优先级最高。"""
        defaults: Dict[str, Any] = {
            "agent_name": _env("AGENT_NAME"),
            "agent_version": _env("AGENT_VERSION"),
            "env": _env("ENV"),
            "enabled": _env_bool("ENABLED", True),
            "debug": _env_bool("DEBUG", False),
            "sanitize_enabled": _env_bool("SANITIZE", True),
            "truncate_enabled": _env_bool("TRUNCATE", True),
            "max_field_bytes": _env_int("MAX_FIELD_BYTES", 64 * 1024),
        }
        defaults.update(overrides)
        return cls.model_validate(defaults)
