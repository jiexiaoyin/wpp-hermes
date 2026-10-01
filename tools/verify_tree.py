"""结构守卫：``plugin/`` 的**形状**必须一直保持成包，扁平文件不得回流。

这个脚本不验行为，只验形状与**加载期依赖**。它挡的是三类**历史上真发生过**的事故：

## R1 扁平模块回流（历史：``plugin/inbound.py`` 与 ``plugin/inbound/`` 同名共存）

旧树里同时存在 ``plugin/inbound.py``（文件）和 ``plugin/inbound/``（目录，且**没有
``__init__.py``**）。同名目录无 ``__init__.py`` ⇒ 不是包 ⇒ ``from . import inbound``
永远命中那个文件，整层新代码**不可达**。同理 ``plugin/tools.py`` 与
``plugin/tools/`` 并存时，包胜出 ⇒ 生成物生效、扁平文件成了幽灵。

判据：``plugin/`` 顶层只允许 ``__init__.py`` 与 ``registration.py`` 两个 .py 文件。

## R2 ``register()`` 执行时会碰到的第三方运行期依赖（历史：F4 —— 隔离解释器缺 pymysql/aiohttp）

``register()`` 是**真的会执行**的：``registration._register_platform`` 里
``from .platform import adapter`` 是函数级导入，但导入**动作**照样发生。所以这里做的是
**含函数级导入的可达性 BFS**（从 ``plugin/__init__.py`` 出发），而不是只扫模块级。

判定分两层，因为两者的证据强度不同：

- **``plugin/tools/`` 子树内出现第三方模块级依赖 ⇒ FAIL**。这是计划里写死的硬约束
  （``plugin_validate.py:320-355`` 在 scratch 解释器里跑 ``register()``），而且当前
  完全满足 —— 一旦破防就是回归。
- **适配器链路上出现 ⇒ ℹ️ 提示，不 FAIL**。``platform/adapter.py`` 模块级 import
  ``..core.api_client``（→``aiohttp``）与 ``.inbound_ws``（→``websockets``）。.183 实测
  平台能注册上（V4/V5 绿），说明真跑的那个解释器里这两个依赖是在的；但 ``hermes
  plugins validate`` 的 probe 解释器未必有。**根治办法是把这两处 import 挪进函数**
  （一个文件、几行），不是加白名单。第 9 步 V3 必须确认 validate 不报缺依赖。

## R3 ``plugin/tools/__init__.py`` 里 eager import（历史：F5 —— 10 个工具静默不注册）

旧实现把 ``from . import wecom_tools`` 放在模块级，而该模块**加载期**解析外部
``wecom-cli.js`` 并抛 ``FileNotFoundError`` ⇒ 被 ``registration._step`` 的兜底吞成一行
WARNING ⇒ 其后的 wecom 7 + wpp_extras 3 个工具**全部静默不注册**。

判据：``plugin/tools/__init__.py`` 必须**零 import**（子模块由调用方按需 import）。
其他包的 ``__init__.py`` 允许做纯 re-export 门面（它们的加载期依赖由 R2 覆盖）。

用法::

    python3 tools/verify_tree.py          # 默认：本树 /root/dev/wpp-hermes-py2
    python3 tools/verify_tree.py <root>   # 指到别的树（如部署目录）自查
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

#: 允许出现在 ``plugin/`` 顶层的 .py 文件（其余一律是扁平回流）
TOP_LEVEL_ALLOWED = {"__init__.py", "registration.py"}

#: **必须**零 import 的包（F5 的直接对策）：包级导入一挂，整批工具静默消失。
#:
#: 用**点分包名**（相对 ``plugin/``），所以子包也能点名 —— 三个都在注册链上：
#: ``llm``（``adapter`` → 心流 → judge）、``inbound.heartflow``（``adapter._try_heartflow``）。
#: 判据是"这个包的加载期错误**会不会**被 ``registration._step`` 的兜底吞成一行 WARNING"，
#: 而不是"它在哪一层"。
NO_EAGER_INIT_PACKAGES = ("tools", "llm", "inbound.heartflow")

#: 第三方**运行期**依赖 —— 缺了它们的解释器跑 ``register()`` 会直接 ImportError。
#: （Hermes 自有的 ``gateway`` / ``agent`` 等不算：插件必然在 Hermes 里跑。）
RUNTIME_DEPS = frozenset({
    "aiohttp", "websockets", "pymysql", "watchdog", "httpx", "requests",
    "redis", "PIL", "numpy", "cryptography", "pydantic", "yaml",
})

FAILS: list[str] = []


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"  ❌ {msg}")


def ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def info(msg: str) -> None:
    print(f"  ℹ️  {msg}")


def _modname(plugin: Path, path: Path) -> str:
    parts = list(path.relative_to(plugin).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


# --------------------------------------------------------------------- R1 形状

def check_shape(plugin: Path) -> None:
    print("R1 形状：顶层不得有扁平模块；同名不得文件/目录并存")
    flat = sorted(p.name for p in plugin.glob("*.py") if p.name not in TOP_LEVEL_ALLOWED)
    if flat:
        fail(f"plugin/ 顶层出现扁平模块（应只剩 {sorted(TOP_LEVEL_ALLOWED)}）: {flat}")
    else:
        ok(f"plugin/ 顶层干净（{sorted(TOP_LEVEL_ALLOWED)}）")

    pkgs = sorted(p.name for p in plugin.iterdir() if p.is_dir() and not p.name.startswith((".", "__")))
    for name in pkgs:
        sibling = plugin / f"{name}.py"
        if sibling.exists():
            fail(f"同名并存: {sibling} 与 {plugin / name}/ —— 导入结果取决于 Python 的包优先规则")

    # 每个含 .py 的子目录都必须是真包（有 __init__.py）
    bad_pkg = 0
    for d in sorted(p for p in plugin.rglob("*") if p.is_dir()):
        if d.name.startswith((".", "__")):
            continue
        if any(d.glob("*.py")) and not (d / "__init__.py").exists():
            fail(f"{d.relative_to(plugin)}/ 有 .py 却没有 __init__.py ⇒ 不是包（R1 原版事故）")
            bad_pkg += 1
    if not bad_pkg:
        ok(f"{len(pkgs)} 个子目录，无同名 .py 并存，且含 .py 的都带 __init__.py")


# ------------------------------------------- R2 注册执行链路的加载期第三方依赖

def _collect_imports(plugin: Path, path: Path) -> tuple[set[str], list[tuple[str, int, bool]]]:
    """扫**整个文件**的 import（含函数体内）。

    返回 ``(包内相对导入的目标, [(模块名, 行号, 是否模块级)])``。

    含函数体内的导入，是因为 ``register()`` 会**执行**到它们 —— "延迟导入"只是把代价
    从加载期挪到调用期。但两者**必须分开报**：只有**模块级**那批才是"一 import 这个文件
    就得先有依赖"；函数级的是已经处理好的（``core/netguard.py`` 就是函数级，别误伤）。
    """
    name = _modname(plugin, path)                      # "platform.adapter" / "platform"
    pkg = name if path.name == "__init__.py" else (name.rsplit(".", 1)[0] if "." in name else "")
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # 先标出模块级语句（只下钻 try/if/with —— 那几层的东西仍然在加载期跑；**不进函数**）
    top_ids: set[tuple[int, int]] = set()

    def drill(body: list[ast.stmt]) -> None:
        for n in body:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                top_ids.add((n.lineno, n.col_offset))
            elif isinstance(n, (ast.Try, ast.If, ast.With)):
                drill(n.body + n.orelse)

    drill(tree.body)

    inner: set[str] = set()
    outer: list[tuple[str, int, bool]] = []
    for n in ast.walk(tree):
        if not isinstance(n, (ast.Import, ast.ImportFrom)):
            continue
        top = (n.lineno, n.col_offset) in top_ids
        if isinstance(n, ast.Import):
            outer.extend((a.name.split(".")[0], n.lineno, top) for a in n.names)
        else:
            if n.level == 0:
                if n.module:
                    outer.append((n.module.split(".")[0], n.lineno, top))
                continue
            base = pkg.split(".") if pkg else []
            up = n.level - 1
            if up:
                base = base[:len(base) - up]
            target = ".".join(base + ([n.module] if n.module else [])).lstrip(".")
            inner.add(target)
            # ⚠️ ``from .pkg import sub`` 里的 sub **可能是子模块**（如
            #    ``from .platform import adapter``）—— 必须把它当候选加进去，
            #    否则可达性 BFS 会漏掉整棵子树（这里漏过一次，报"零依赖"的假绿）。
            inner.update((target + "." + a.name).lstrip(".") for a in n.names)
    return inner, outer


def check_register_chain(plugin: Path) -> None:
    print("R2 注册执行链路：第三方运行期依赖的位置")
    mods = {_modname(plugin, p): p for p in plugin.rglob("*.py")}

    # 从包根出发的 BFS（含函数级导入 —— register() 会执行到它们）
    seen: set[str] = set()
    load_time: dict[str, set[str]] = {}      # 第三方模块 -> {**模块级**引用它的包内模块}
    call_time: dict[str, set[str]] = {}      # 第三方模块 -> {只在函数体里引用它的}
    stack = [""]
    unresolved: set[str] = set()
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        path = mods.get(m)
        if path is None:
            # 只报**真的找不到父模块**的（``from .pkg import sym`` 会把符号名也塞进来，
            # 那些的父模块都在树里，是候选不是缺失 —— 报出来只会是噪声）
            if m.rsplit(".", 1)[0] not in mods:
                unresolved.add(m)
            continue
        seen.add(m)
        inner, outer = _collect_imports(plugin, path)
        for name, _line, is_top in outer:
            if name not in RUNTIME_DEPS:
                continue
            (load_time if is_top else call_time).setdefault(name, set()).add(m or "<plugin.根>")
        stack.extend(inner)

    print(f"     可达 {len(seen)} 个包内模块；未解析的相导入: {sorted(unresolved) or '无'}")

    # ⛔ 计划硬约束：tools 子树不许有任何第三方运行期依赖（模块级 **或** 函数级都不该有，
    #    因为工具 handler 是给 model 调的，注册期与调用期都在 Hermes 进程里）
    tools_load = {k: v for k, v in load_time.items() if any(x.startswith("tools") for x in v)}
    tools_call = {k: v for k, v in call_time.items() if any(x.startswith("tools") for x in v)}
    if tools_load or tools_call:
        for kind, hits in (("模块级", tools_load), ("函数体", tools_call)):
            for name, where in sorted(hits.items()):
                fail(f"plugin/tools/ 子树{kind}依赖第三方 {name!r}（{sorted(where)}）")
    else:
        ok("plugin/tools/ 子树零第三方运行期依赖（计划硬约束满足）")

    # 适配器链路：模块级 = 一 import adapter 就得有它（**必须修**）；
    #             函数体 = 已经延迟好了（**无需动**）。两者混报会把人带偏。
    #
    # ⚠️ 这条 2026-10-01 从「ℹ️ 提示」升成「❌ FAIL」：原先 platform/adapter.py 会在模块级
    #    拉起 aiohttp（经 core.api_client）与 websockets（经 .inbound_ws），已全部延迟到
    #    调用期。既然现在是零，就不许再有新的 —— 否则 `register()` 在缺依赖的解释器里
    #    （plugin_validate 的 probe）会静默注册不上，而失败长得像"工具数对不上"。
    adapter_load = {k: v for k, v in load_time.items() if k not in tools_load}
    for name, where in sorted(adapter_load.items()):
        fail(f"注册链路**模块级**依赖第三方 {name!r}（{sorted(where)}）⇒ 一 import 就会拉起它。"
             f"请把那处 import 挪进函数（本文件 docstring 有先例）")
    adapter_call = {k: v for k, v in call_time.items() if k not in tools_call}
    for name, where in sorted(adapter_call.items()):
        info(f"函数体内依赖 {name!r}（{sorted(where)}）—— 已延迟到调用期 ✓ 无需处理")
    if not load_time and not call_time:
        ok("整个注册链路零第三方运行期依赖")


def check_no_eager_init(plugin: Path) -> None:
    print("R3 指定包的 __init__.py 必须零 import（F5 的直接对策）")
    bad = 0
    for name in NO_EAGER_INIT_PACKAGES:
        init = plugin.joinpath(*name.split(".")) / "__init__.py"
        if not init.exists():
            fail(f"{name}/__init__.py 不存在（点名了就必须在）")
            bad += 1
            continue
        offenders: list[str] = []
        for node in ast.parse(init.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                offenders.append(f"{name}/__init__.py:{node.lineno}")
        if offenders:
            for o in offenders:
                fail(f"{o} 有包级 import —— 子模块加载期一挂，整批静默消失（F5 同形态）")
            bad += 1
        else:
            ok(f"{name}/__init__.py 零 import")

    # 其余包的 __init__ 允许做门面，只报个数（它们的加载期依赖由 R2 兜）
    locked = set(NO_EAGER_INIT_PACKAGES)
    facades = []
    for init in sorted(plugin.rglob("__init__.py")):
        rel = init.relative_to(plugin)
        dotted = rel.parent.as_posix().replace("/", ".")
        if dotted in locked or rel.as_posix() == "__init__.py":
            continue
        if any(isinstance(n, (ast.Import, ast.ImportFrom))
               for n in ast.parse(init.read_text(encoding="utf-8")).body):
            facades.append(rel.parent.as_posix() or ".")
    if facades:
        info(f"其余做 re-export 门面的包: {sorted(set(facades))}（加载期依赖见 R2）")


# ------------------------------------------------------------------------ 入口

def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    plugin = root / "plugin"
    if not plugin.is_dir():
        print(f"❌ 找不到 {plugin}")
        return 1
    print(f"检查树: {root}\n")
    check_shape(plugin)
    print()
    check_register_chain(plugin)
    print()
    check_no_eager_init(plugin)
    print()
    if FAILS:
        print(f"{len(FAILS)} 项结构问题 —— 形状不对就别往下走")
        return 1
    print("结构守卫：全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
