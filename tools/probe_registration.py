#!/usr/bin/env python3
"""本地注册探针：**真跑** ``register()``，数工具、抓重名、与 ``plugin.yaml`` 对账。

## 它补的是哪一段

``tools/gen_provides_tools.py`` 才是权威生成器，但它要求 ``gateway.*`` 可导入 ——
只能在 ``.183`` 的 Hermes 运行时里跑。本探针是**开发机上的对照**：把 Hermes 内部包与
缺失的运行期依赖换成空壳，再走同一条 ``register()``，于是：

- 注册**数量**对不对（应为 262 = wpp 251 + wecom 7 + phoneerp 4）；
- 有没有**重名**（``sorted(set(...))`` 会把重名吃掉 —— 所以这里用**原始列表**数；
  历史陷阱：把聚合项 ``ALL_EXTRA_TOOLS`` 当组注册，107 个工具会被注册两遍）；
- ``plugin.yaml`` 里的 ``provides_tools`` 与实注册是否**逐名一致**
  （Hermes 的 ``plugin_validate`` 拿这个字段比对：多一个 ⇒ FAIL，少一个 ⇒ WARN）。

## 它**不能**替代的

不能替代 ``.183`` 上那条 V3/V6 真机验收：
``hermes plugins doctor`` 报的 ``registrations: N tool(s)`` 才是终审，
本探针的空壳可能与真 Hermes 的 API 形状有出入（所以只数名字，不校验 schema）。
用法::

    python3 tools/probe_registration.py            # 默认本树
    python3 tools/probe_registration.py <root>
"""

from __future__ import annotations

import importlib
import re
import sys
import types
from collections import Counter
from pathlib import Path

ROOT_DEFAULT = Path(__file__).resolve().parent.parent

#: 期望总数（wpp 251 + wecom 7 + phoneerp 4）。与 plugin.yaml 注释里那句同年同日。
EXPECTED_TOTAL = 262

#: 只在 Hermes 运行时里存在的内部包 —— 空壳冒充成包，够 ``register()`` import 就行。
_HERMES_INTERNAL = {
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


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[1]).resolve() if argv and len(argv) > 1 else ROOT_DEFAULT
    plugin_dir = root / "plugin"
    if not (plugin_dir / "plugin.yaml").is_file():
        print(f"❌ 找不到 {plugin_dir}/plugin.yaml")
        return 1

    print(f"插件树: {plugin_dir}\n")

    stubbed_runtime = _stub_runtime()
    _stub_internal()
    if stubbed_runtime:
        print(f"ℹ️  本机缺 {'/'.join(stubbed_runtime)} —— 已用空壳顶上（注册期用不到）")
    print("ℹ️  gateway.* 用空壳冒充（真身只在 Hermes 运行时里）\n")

    sys.path.insert(0, str(root))
    pkg = importlib.import_module(plugin_dir.name)
    ctx = _CollectorCtx()
    pkg.register(ctx)

    raw = ctx.names
    uniq = sorted(set(raw))
    dupes = {n: c for n, c in Counter(raw).items() if c > 1}
    declared = _declared(plugin_dir / "plugin.yaml")

    fails: list[str] = []

    # ① 平台注册（顺带确认 _register_platform 没被 try/except 吞掉）
    if ctx.platforms:
        print(f"✅ 平台已注册: {ctx.platforms}")
    else:
        fails.append("平台一个都没注册上 —— _register_platform 被兜底吞了？")
        print("❌ 平台一个都没注册上")

    # ② 重名 —— 必须用原始列表，set 会把证据吃掉
    print(f"\n② 重名检查（原始注册 {len(raw)} 次）")
    if dupes:
        for n, c in sorted(dupes.items()):
            fails.append(f"工具 {n!r} 被注册 {c} 次")
        print(f"  ❌ {len(dupes)} 个工具重复注册 —— 最可能的原因是把聚合项"
              f"（如 ALL_EXTRA_TOOLS）当组注册了")
    else:
        print(f"  ✅ 无重复（原始 {len(raw)} 次 == 去重 {len(uniq)} 个）")

    # ③ 总数
    print("\n③ 总数")
    if len(uniq) == EXPECTED_TOTAL:
        print(f"  ✅ {len(uniq)} == 期望 {EXPECTED_TOTAL}")
    else:
        fails.append(f"工具总数 {len(uniq)} != 期望 {EXPECTED_TOTAL}")
        print(f"  ❌ {len(uniq)} != 期望 {EXPECTED_TOTAL}")

    # ④ 与 plugin.yaml 对账（Hermes plugin_validate 的判据）
    print("\n④ 与 plugin.yaml 的 provides_tools 对账")
    actual_s, declared_s = set(uniq), set(declared)
    extra = sorted(actual_s - declared_s)      # 实注册多出来 ⇒ plugin_validate **FAIL**
    missing = sorted(declared_s - actual_s)    # 声明了但没注册 ⇒ **WARN**
    if extra or missing:
        if extra:
            fails.append(f"实注册多出 {len(extra)} 个未声明（plugin_validate 判 FAIL）: {extra[:10]}")
        if missing:
            fails.append(f"声明了但未注册 {len(missing)} 个（plugin_validate 判 WARN）: {missing[:10]}")
    else:
        print(f"  ✅ declared == actual（{len(declared)} 条逐名一致）")

    print()
    if fails:
        for f in fails:
            print(f"❌ {f}")
        return 1
    print(f"注册探针：全绿 —— {len(uniq)} 个工具，与 plugin.yaml 逐名一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
