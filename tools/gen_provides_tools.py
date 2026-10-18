#!/usr/bin/env python3
"""从**实际注册**生成 ``plugin.yaml`` 的 ``provides_tools``（R6：防手写漂移）。

## 为什么必须生成而不是手写

Hermes 的 ``plugin_validate`` 会把声明与实注册逐名比对：

- ``actual − declared ≠ ∅``  ⇒ **FAIL 级**
- ``declared − actual ≠ ∅``  ⇒ WARN

手写必然漂移（本插件注册 299 个工具，其中 216 个是厂商端点机械映射）。
所以 deploy.sh 会**重新生成并与仓库里的值 diff，不为空即中止**。

## 必须在 Hermes 运行时里跑

``register()`` 会 import ``platform/adapter.py``，那需要 ``gateway.*`` 可导入。
用 Hermes 自己的隔离解释器 + 它的启动前导：

    P=/root/.hermes/tools/python-3.14.7+20260901-linux-x64/bin/python3
    $P -I -c '
    import os, sys
    os.environ.pop("PYTHONHOME", None); os.environ.pop("PYTHONPATH", None)
    sys.path.insert(0, "/root/.hermes/hermes-agent")
    sys.path.insert(0, "<plugin_dir 的父目录>")
    os.environ.setdefault("HERMES_HOME", "/root/.hermes")
    import hermes_bootstrap, runpy
    sys.argv = ["gen_provides_tools.py", "<plugin_dir>"]
    runpy.run_path("<repo>/tools/gen_provides_tools.py", run_name="__main__")
    '

``hermes plugins doctor`` 走的是同一套加载，所以这里的计数应与
doctor 报的 ``registrations: N tool(s)`` **完全相等**。

## 退出码

- ``0`` 正常
- ``1`` 注册数为 0 —— 视为失败。**绝不能把空列表写回 yaml**（那会让
  ``declared − actual`` 全变成 WARN，且掩盖注册整体崩掉）。
- ``2`` ``--write`` 模式下没找到 ``provides_tools:`` 段
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from pathlib import Path

_MARKER = "provides_tools:"
#: 顶层 key 的行（用于界定 provides_tools 段的结束）
_TOP_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*:")


class _CollectorCtx:
    """最小 PluginContext 替身 —— 只记录 ``register_tool`` 的名字。

    其余属性一律返回 no-op 可调用对象，这样即使 Hermes 将来新增注册 API，
    收集器也不会因为缺方法而崩。
    """

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
        return lambda *a, **k: None


def collect(plugin_dir: Path) -> list[str]:
    """import 插件包并跑 ``register()``，返回去重排序后的工具名。"""
    pkg_parent = str(plugin_dir.resolve().parent)
    if pkg_parent not in sys.path:
        sys.path.insert(0, pkg_parent)

    pkg = importlib.import_module(plugin_dir.resolve().name)
    ctx = _CollectorCtx()
    pkg.register(ctx)
    return sorted(set(ctx.names))


def render(names: list[str]) -> str:
    """渲染 yaml 片段（含 ``provides_tools:`` 行）。"""
    if not names:
        raise ValueError("空工具列表 —— 拒绝渲染（见模块 docstring）")
    body = "\n".join(f"  - {n}" for n in names)
    return f"{_MARKER}\n{body}\n"


def splice(yaml_text: str, names: list[str]) -> str:
    """把 ``provides_tools`` 段替换成新列表，保留其他内容与上方注释。"""
    lines = yaml_text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.startswith(_MARKER)), None)
    if start is None:
        raise LookupError(f"没找到 {_MARKER} 行")

    end = start + 1
    while end < len(lines):
        ln = lines[end]
        # 段内容 = 缩进行或空行；碰到下一个顶层 key 就停
        if ln.strip() and _TOP_KEY.match(ln):
            break
        if ln.strip() and not ln.startswith((" ", "\t")):
            break
        end += 1

    tail = "".join(lines[end:])
    return "".join(lines[:start]) + render(names) + tail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 plugin.yaml 的 provides_tools")
    ap.add_argument("plugin_dir", type=Path, help="插件目录（含 plugin.yaml）")
    ap.add_argument("--write", action="store_true", help="就地改写 plugin.yaml")
    ap.add_argument(
        "--expect", type=int, default=None,
        help="期望的工具数；不符则失败（用于 CI 交叉核对 doctor 的计数）",
    )
    args = ap.parse_args(argv)

    plugin_dir: Path = args.plugin_dir
    names = collect(plugin_dir)

    if not names:
        print("ERROR: 注册结果为 0 个工具 —— 拒绝继续", file=sys.stderr)
        return 1

    if args.expect is not None and len(names) != args.expect:
        print(f"ERROR: 期望 {args.expect} 个工具，实得 {len(names)}", file=sys.stderr)
        return 1

    if not args.write:
        sys.stdout.write(render(names))
        return 0

    yaml_path = plugin_dir / "plugin.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    try:
        new_text = splice(text, names)
    except LookupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if new_text == text:
        print(f"provides_tools 已是最新（{len(names)} 个）")
        return 0

    yaml_path.write_text(new_text, encoding="utf-8")
    print(f"已写入 {yaml_path}: {len(names)} 个工具")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
