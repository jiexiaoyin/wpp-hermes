"""Hermes 运行时空壳 —— 测试与本地探针共用。

``plugin/platform/adapter.py`` 在模块级 ``from gateway.config import Platform``
（Hermes 独有，本机没有），注册期还会遇到只在真运行时才装的
``aiohttp`` / ``websockets`` / ``pymysql``。这里把它们换成**最小空壳**，
让 ``register()`` 能在开发机上真跑一遍。

本模块只依赖标准库，且**不 import 插件包** —— 探针可以在装空壳之前按路径加载它。

⚠️ 空壳是进程级 ``sys.modules`` 条目：调用方必须自己保存/恢复原状
（见 ``plugin/tests/conftest.py`` 的 ``stubbed`` 夹具）。
"""

from __future__ import annotations

import importlib
import re
import sys
import types
from pathlib import Path

#: 期望的注册工具总数（wpp + wecom + phoneerp + extras）。
#: 数量变了必须**显式**改这里 —— 它是注册面回归的锚点。
EXPECTED_TOTAL = 292

#: 只在 Hermes 运行时里存在的内部包 —— 空壳冒充成包，够 ``register()`` import 就行。
_HERMES_INTERNAL: dict[str, list[str]] = {
    "gateway": [],
    "gateway.config": ["Platform"],
    "gateway.platforms": [],
    "gateway.platforms.base": ["BasePlatformAdapter", "SendResult"],
    "gateway.platforms.event": ["MessageEvent", "MessageType"],
    "gateway.platforms.helpers": ["MessageDeduplicator"],
}

#: 注册期用不到的运行期依赖（真用时才在函数体里 import）——缺了就用空壳顶上。
_RUNTIME_STUBS = ("aiohttp", "websockets", "pymysql")


class _CollectorCtx:
    """最小 PluginContext 替身：**只记名字，不去重**（重名是必须看见的信号）。"""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.platforms: list[str] = []

    def register_tool(self, **kwargs) -> None:
        name = kwargs.get("name")
        if name:
            self.names.append(str(name))

    def register_platform(self, **kwargs) -> None:
        name = kwargs.get("name")
        if name:
            self.platforms.append(str(name))

    def __getattr__(self, _item: str):
        # Hermes 将来新增任何注册 API 都不该让探针崩掉
        return lambda *a, **k: None


def _stub_internal() -> None:
    """装上 Hermes 内部包的空壳；已存在的模块不覆盖。"""
    for name, attrs in _HERMES_INTERNAL.items():
        if name in sys.modules:
            continue
        mod = types.ModuleType(name)
        if not attrs:                      # 无属性的当包，有属性的当模块
            mod.__path__ = []              # type: ignore[attr-defined]
        for attr in attrs:
            setattr(mod, attr, type(attr, (), {}))
        sys.modules[name] = mod


def _stub_runtime() -> list[str]:
    """缺哪个运行期依赖就顶哪个；返回**被顶替**的模块名，供调用方恢复。"""
    stubbed = []
    for name in _RUNTIME_STUBS:
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except ImportError:
            mod = types.ModuleType(name)
            mod.__path__ = []              # type: ignore[attr-defined]
            sys.modules[name] = mod
            stubbed.append(name)
    return stubbed


def _declared(yaml_path: Path) -> list[str]:
    """从 plugin.yaml 抠出 ``provides_tools:`` 段的名字（不引 yaml 依赖）。"""
    text = yaml_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("provides_tools:")), None)
    if start is None:
        raise LookupError("plugin.yaml 里没有 provides_tools: 段")
    out: list[str] = []
    for ln in lines[start + 1:]:
        if ln.strip() and not ln.startswith((" ", "\t")):
            break                          # 下一个顶层 key
        m = re.match(r"^\s+-\s+(\S+)\s*$", ln)
        if m:
            out.append(m.group(1))
    return out
