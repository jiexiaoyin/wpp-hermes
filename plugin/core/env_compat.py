"""多 profile（``gateway.multiplex_profiles``）下的 env 读取口径。

## 为什么不能直接 ``os.environ.get``

Hermes 多 profile 共用一个进程时，进程 env 是 **default profile 的**；配置里的
YAML→env 桥接是**先写先得**（first-writer-wins），所以：

* 凭据（authcode / tokenKey / DB 密码 …）直接读 env ⇒ **可能读到另一个 profile
  的值**，表现为串号；
* 门禁/白名单（``*_ALLOWLIST``、``*_ENABLED`` …）直接读 env ⇒ 另一个 profile 的
  白名单会作用到本 profile（Hermes #72348）。

Hermes 自己在 ``gateway/platforms/_shared.py`` 给了两个正确口径：

* :func:`get_scoped_secret` —— 凭据。装了 secret scope 时，**scoped miss 不回退
  到 os.environ**（只有 unscoped 的 default profile 才用 env）。
* :func:`platform_gate_env` —— 开关/白名单。scope + multiplex 生效时只查 scope。

本模块只做**导入失败时的降级**：插件在 Hermes 进程外被 import（单测、探针、
工具脚本）时那两个函数不存在，此时 ``os.environ`` 就是唯一来源，与旧行为一致。
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def secret_env(name: str, default: str = "") -> str:
    """按 Hermes 的 scoped-secret 口径读一个**凭据**型 env。

    ⚠️ 只有 **import 失败**（Hermes 进程外）才回退 ``os.environ``。函数存在但
    调用抛错时返回 ``default`` —— 回退 os.environ 恰恰会读到**另一个 profile**
    的值，那正是本模块要消灭的串号路径（fail-closed，不 fail-open）。
    """
    if not name:
        return default
    try:
        from gateway.platforms._shared import get_scoped_secret
    except ImportError:
        return str(os.environ.get(name) or default)
    try:
        val: Any = get_scoped_secret(name, None)
    except Exception:
        log.warning("[WPP] scoped secret 读取失败: %s", name, exc_info=True)
        return default
    return default if val is None else str(val)


def gate_env(name: str, default: str = "") -> str:
    """按 Hermes 的 per-profile 口径读一个**开关/白名单**型 env（已 strip）。

    与 :func:`secret_env` 同一原则：仅 import 失败时回退 ``os.environ``。
    """
    if not name:
        return default
    try:
        from gateway.platforms._shared import platform_gate_env
    except ImportError:
        return str(os.environ.get(name) or default).strip()
    try:
        return str(platform_gate_env(name, default) or default)
    except Exception:
        log.warning("[WPP] per-profile gate env 读取失败: %s", name, exc_info=True)
        return default


def plain_env(name: str, default: str = "") -> str:
    """非凭据、非门禁的普通配置（URL、端口 …）。多 profile 下共用是预期行为。"""
    return str(os.environ.get(name) or default)
