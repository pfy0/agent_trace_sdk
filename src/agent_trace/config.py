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


class Config(BaseModel):
    """SDK 全局配置。所有字段都有默认值，可只用环境变量启动。"""

    model_config = ConfigDict(extra="allow")

    # trace 级默认元数据（对应规范中的业务标识字段）
    agent_id: Optional[str] = Field(default=None)
    parent_agent_id: Optional[str] = Field(default=None)
    app_name: Optional[str] = Field(default=None)
    app_id: Optional[str] = Field(default=None)
    system_code: Optional[str] = Field(default=None)
    user_id: Optional[str] = Field(default=None)
    source: Optional[str] = Field(default=None)

    # 实时采集默认值
    default_completed: bool = Field(default=True)
    default_partial: bool = Field(default=False)

    # 导出/上报
    exporters: Optional[list] = Field(default=None)

    # 运行期开关
    enabled: bool = Field(default=True)
    debug: bool = Field(default=False)

    @classmethod
    def from_env(cls, **overrides: Any) -> "Config":
        """从环境变量构造配置，显式 overrides 优先级最高。"""
        defaults: Dict[str, Any] = {
            "agent_id": _env("AGENT_ID"),
            "parent_agent_id": _env("PARENT_AGENT_ID"),
            "app_name": _env("APP_NAME"),
            "app_id": _env("APP_ID"),
            "system_code": _env("SYSTEM_CODE"),
            "user_id": _env("USER_ID"),
            "source": _env("SOURCE"),
            "enabled": _env_bool("ENABLED", True),
            "debug": _env_bool("DEBUG", False),
        }
        defaults.update(overrides)
        return cls.model_validate(defaults)
