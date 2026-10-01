"""逐模块导入探针 —— 在真环境 venv 里把 ``plugin/`` 下每个模块都 import 一遍。

**为什么单独做这个**：旧实现最贵的一次事故是 ``wecom_tools.py`` 在**模块导入期**
解析外部 CLI 抛 ``FileNotFoundError``，被外层 except 吞掉 ⇒ **10 个工具静默不注册**。
Hermes 的 ``plugin_validate`` 又是在**隔离 probe 子进程**里 import 的
（``plugin_validate.py:320-355``）⇒ 任何「导入期副作用」都会在没人看的地方炸。

所以：**每个模块都必须能被单独 import 且无副作用**。本探针把这条变成可执行检查。

**只读**：不连网、不连 DB、不发消息。用法::

    P=<环境 venv>/bin/python
    $P /tmp/wpp-py2-verify/tools/probe_import_all.py

退出码 0 = 全部模块可导入。非 0 时会逐条打印失败模块 + 真因（截断层，不是吞掉的）。
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

ROOT = Path("/tmp/wpp-py2-verify")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PKG = ROOT / "plugin"


def _module_names() -> list[str]:
    """把 ``plugin/**.py`` 映射成包名；跳过 ``tests``/``accounts``/``__pycache__``。"""
    names: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in {"__pycache__", "tests", "accounts"} for part in rel.parts):
            continue
        parts = list(rel.parts[:-1]) + [rel.stem]
        if parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(parts))
    return sorted(set(names))


ok: list[str] = []
bad: list[tuple[str, str]] = []

for name in _module_names():
    try:
        importlib.import_module(name)
        ok.append(name)
    except BaseException:  # noqa: BLE001 — 连 SystemExit 也要抓：导入期 sys.exit 同样致命
        bad.append((name, traceback.format_exc(limit=6)))

for name in ok:
    print(f"  ✅ {name}")
for name, tb in bad:
    print(f"  ❌ {name}")
    for line in tb.rstrip().splitlines()[-4:]:
        print(f"       {line}")

print(f"\n可导入 {len(ok)}/{len(ok) + len(bad)}")
if bad:
    print("\n失败模块:")
    for name, _ in bad:
        print(f"  - {name}")
sys.exit(0 if not bad else 1)
