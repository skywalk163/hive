"""Hive 韧性控制面 v0.5。

独立于旧 HTTP 集群面的一组纯标准库组件：Leader 选举、状态条目单调扩散、
上行单播 / 下行组播、DNA 稳态抑制、租约与人工拉取。

模块边界与线格式见 ``docs/控制面v0.5-实现约定.md``。
"""

from .config import (  # noqa: F401
    DEFAULT_INCARNATION_PATH,
    DEFAULT_SOCKET_PATH,
    ControlConfig,
    Limits,
    default_incarnation_path,
    default_socket_path,
)
from .model import (  # noqa: F401
    STATES,
    TRANSIENT_STATES,
    IncarnationStore,
    StateEntry,
    is_newer,
    version_key,
)

__all__ = [
    "STATES",
    "TRANSIENT_STATES",
    "ControlConfig",
    "DEFAULT_INCARNATION_PATH",
    "DEFAULT_SOCKET_PATH",
    "IncarnationStore",
    "Limits",
    "StateEntry",
    "default_incarnation_path",
    "default_socket_path",
    "is_newer",
    "version_key",
]

CONTROL_VERSION = "0.5.0"
