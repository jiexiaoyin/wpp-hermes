"""验证 ``plugin/toolkit/`` 是旧扁平 ``plugin/tools.py`` 的**无损**搬迁。

检查两层，缺一不可：

## ① AST 逐函数等价

对每个目标文件，把旧原文的区间（套上 ``port_tools_domain.REWRITES`` 里**登记在案**
的重写）与新文件都 parse 成 AST，逐个**顶层函数**比对 ``ast.dump``。

- 旧区间里有、新文件里没有 → ❌（函数搬丢了）
- 两边都有但 AST 不同 → ❌（函数体被改过，且没登记）
- 新文件里多出来的 → ℹ️（允许：头部会新增 ``_resolve_to_base64`` 这类包装/别名）

## ② 注册清单**差异**测试（真跑，不是静态推断）

同一份 ``register_*`` 逻辑在**旧模块**与**新模块**上各跑一遍假 ``ctx``，把
``register_tool`` 收到的 (name, toolset, schema) 全量录下来，逐条比。

这一层防的是 AST 看不出的事：注册循环里的 **toolset 字面量**、列表元素被删、
``register_tool`` 的 kwarg 名漏了。旧模块靠 ``__package__`` 造一个假父包来满足
它函数体内那句 ``from .api_client import WppApiError``（见 ``_import_legacy``）。

退出码 0 = 全绿。用法::

    python3 tools/verify_tools_port.py            # 全部
    python3 tools/verify_tools_port.py --ast      # 只跑 ①
    python3 tools/verify_tools_port.py --diff     # 只跑 ②
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from port_tools_domain import (  # noqa: E402
    DECLARED_AST_DIFFS,
    DECLARED_EXTRA_TOOLS,
    DECLARED_REGISTRATION_DIFFS,
    GENERATED,
    LEGACY,
    RENAMES,
    SPLITS,
    apply_rewrites,
    legacy_body,
)

FAILS: list[str] = []
NOTES: list[str] = []


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"  ❌ {msg}")


def note(msg: str) -> None:
    NOTES.append(msg)
    print(f"  ℹ️  {msg}")


# --------------------------------------------------------------------------- ① AST

def _rename_in_ast(node: ast.AST, mapping: dict[str, str]) -> None:
    """按改名表在 AST 上改名（``Name.id`` / 函数名 / 形参名 / 关键字名）。

    在 **AST** 上而不是字符串上做，是为了不误伤字符串字面量与注释里出现的同名文本。
    """
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in mapping:
            n.id = mapping[n.id]
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in mapping:
            n.name = mapping[n.name]
        elif isinstance(n, ast.arg) and n.arg in mapping:
            n.arg = mapping[n.arg]
        elif isinstance(n, ast.keyword) and n.arg in mapping and n.arg is not None:
            n.arg = mapping[n.arg]


def _top_funcs(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def check_ast(legacy_text: str) -> None:
    print("① AST 逐函数等价（旧区间 → 新文件，套登记在案的重写 + 改名）")
    for name in SPLITS:
        path = ROOT / "plugin" / "toolkit" / name
        if not path.exists():
            fail(f"{name}: 新文件不存在")
            continue

        old_tree = ast.parse(apply_rewrites(name, legacy_body(name, legacy_text)))
        _rename_in_ast(old_tree, RENAMES.get(name, {}))
        old, new = _top_funcs(old_tree), _top_funcs(ast.parse(path.read_text(encoding="utf-8")))

        if not old:
            fail(f"{name}: 旧区间里没切出任何顶层函数 —— 锚点可能失效")
            continue

        bad = 0
        declared = DECLARED_AST_DIFFS.get(name, {})
        for fn, node in old.items():
            if fn not in new:
                fail(f"{name}: 旧函数 {fn}() 在新文件里找不到")
                bad += 1
            elif ast.dump(node) != ast.dump(new[fn]):
                if fn in declared:
                    note(f"{name}: {fn}() 有意偏离蓝本 —— {declared[fn]}")
                else:
                    fail(f"{name}: {fn}() 的函数体与旧实现不一致（且未登记在 REWRITES/RENAMES/DECLARED_AST_DIFFS）")
                    bad += 1
        extra = sorted(set(new) - set(old))
        if not bad:
            tag = "生成物" if name in GENERATED else "人工"
            print(f"  ✅ {name:18s} [{tag}] {len(old)} 个函数逐一等价"
                  + (f"（新增 {', '.join(extra)}）" if extra else ""))


# --------------------------------------------------------------------------- ② 差异

class _RecordingCtx:
    """录下 ``register_tool`` 的全部调用（只关心 name / toolset / schema）。"""

    def __init__(self) -> None:
        self.tools: dict[str, tuple[str, object]] = {}
        self.dupes: list[str] = []

    def register_tool(self, **kw: object) -> None:
        name = str(kw.get("name"))
        if name in self.tools:
            self.dupes.append(name)
        self.tools[name] = (str(kw.get("toolset")), kw.get("schema"))


def _import_legacy() -> types.ModuleType:
    """把旧扁平 ``plugin/tools.py`` 当**独立模块**加载。

    它函数体内有 ``from .api_client import WppApiError`` —— 独立加载时没有父包，
    相对导入会炸。所以先造一个假父包 ``legacyplugin``（带 ``api_client`` 桩），
    再把模块的 ``__package__`` 指过去。这是**唯一**为了跑通旧代码而做的手脚，
    不影响被验对象。
    """
    import importlib.util

    pkg = types.ModuleType("legacyplugin")
    pkg.__path__ = []  # type: ignore[attr-defined]
    stub = types.ModuleType("legacyplugin.api_client")

    class _WppApiError(Exception):
        pass

    stub.WppApiError = _WppApiError  # type: ignore[attr-defined]
    sys.modules["legacyplugin"] = pkg
    sys.modules["legacyplugin.api_client"] = stub

    spec = importlib.util.spec_from_file_location("legacyplugin.toolkit", LEGACY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["legacyplugin.toolkit"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(mod: types.ModuleType, fn_names: tuple[str, ...]) -> _RecordingCtx:
    ctx = _RecordingCtx()
    getter = lambda *_a, **_kw: None  # noqa: E731 — 注册期不会真取 adapter
    for fn in fn_names:
        func = getattr(mod, fn)
        try:
            func(ctx, getter)
        except TypeError:
            # mcp / jargon 的签名是 (ctx)
            func(ctx)
    return ctx


def _stub_missing_runtime_deps() -> list[str]:
    """给**本机**缺的第三方库塞最小替身，好让 ``register_*`` 跑得起来。

    为什么这是正当的：``register_msg_tools`` 的**函数体**里有
    ``from ..core.api_client import WppApiError``（旧实现也这样），而
    ``core/api_client.py`` 顶层 ``import aiohttp``。注册过程**根本不用** aiohttp
    （它只在 handler 真调厂商时用），所以在没有 aiohttp 的机器上（本机 /
    ``plugin_validate`` 的隔离解释器）用一个空壳替身，不改变被验对象的任何行为。

    ⚠️ 这**不等于**允许生产代码在模块级依赖 aiohttp —— 插件真跑在环境 venv 里，
    那边装了真的。这里只为「本地也能跑校验」。

    返回被替身的模块名（空列表 = 本机依赖齐备，没动手脚）。
    """
    stubbed: list[str] = []
    for name in ("aiohttp", "websockets"):
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except ImportError:
            mod = types.ModuleType(name)
            mod.__getattr__ = lambda _n: None  # type: ignore[attr-defined]
            sys.modules[name] = mod
            stubbed.append(name)
    return stubbed


def check_diff(legacy_text: str) -> None:
    print("② 注册清单差异测试（旧模块 vs 新模块，真跑）")
    stubbed = _stub_missing_runtime_deps()
    if stubbed:
        note(f"本机缺 {'/'.join(stubbed)} —— 已用空壳替身（注册期用不到它们）")
    legacy = _import_legacy()

    from plugin.toolkit import jargon_tool, mcp, msg

    pairs = (
        ("msg", legacy, msg, ("register_msg_tools",)),
        ("mcp", legacy, mcp, ("register_mcp_tools",)),
        ("jargon", legacy, jargon_tool, ("register_jargon_tools",)),
    )
    for label, old_mod, new_mod, fns in pairs:
        old_ctx = _run(old_mod, fns)
        new_ctx = _run(new_mod, fns)

        if old_ctx.dupes or new_ctx.dupes:
            fail(f"{label}: 注册了重名工具 {sorted(set(old_ctx.dupes + new_ctx.dupes))}")

        declared_reg = DECLARED_REGISTRATION_DIFFS.get(label, {})
        declared_extra = DECLARED_EXTRA_TOOLS.get(label, {})
        only_old = sorted(set(old_ctx.tools) - set(new_ctx.tools))
        only_new = sorted(set(new_ctx.tools) - set(old_ctx.tools))
        if only_old:
            fail(f"{label}: 旧有 {len(only_old)} 个工具在新实现里丢了: {only_old[:8]}")
        undeclared_new = [n for n in only_new if n not in declared_extra]
        if undeclared_new:
            fail(f"{label}: 新实现多出未申报的工具: {undeclared_new[:8]}")
        for n in only_new:
            if n in declared_extra:
                note(f"{label}: 新增工具 {n} —— {declared_extra[n]}")

        drifted = [
            n for n in sorted(set(old_ctx.tools) & set(new_ctx.tools))
            if old_ctx.tools[n] != new_ctx.tools[n]
        ]
        undeclared_drift = [n for n in drifted if n not in declared_reg]
        if undeclared_drift:
            fail(f"{label}: {len(undeclared_drift)} 个工具的 (toolset, schema) 与旧实现不同: {undeclared_drift[:5]}")
        for n in drifted:
            if n in declared_reg:
                note(f"{label}: {n} 有意偏离蓝本 —— {declared_reg[n]}")

        bad_toolset = [n for n, (ts, _s) in new_ctx.tools.items() if ts != "wechatpadpro"]
        if bad_toolset:
            fail(f"{label}: toolset 不是 'wechatpadpro' 的: {bad_toolset[:5]}")

        if not (only_old or undeclared_new or undeclared_drift or bad_toolset):
            suffix = "（含已申报差异）" if (only_new or drifted) else ""
            print(f"  ✅ {label:8s} {len(new_ctx.tools):3d} 个工具：名称 / toolset / schema 一致{suffix}")


# --------------------------------------------------------------------------- 入口

def main() -> int:
    if not LEGACY.exists():
        print(f"❌ 蓝本不存在: {LEGACY}")
        return 1
    legacy_text = LEGACY.read_text(encoding="utf-8")

    argv = sys.argv[1:]
    if not argv or "--ast" in argv:
        check_ast(legacy_text)
    if not argv or "--diff" in argv:
        check_diff(legacy_text)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项不一致 —— 搬迁不是无损的，别往下走")
        return 1
    print("工具层搬迁校验：全绿" + (f"（{len(NOTES)} 条提示）" if NOTES else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
