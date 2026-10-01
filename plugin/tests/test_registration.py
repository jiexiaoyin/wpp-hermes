"""注册面回归 —— **注册数 == ``plugin.yaml provides_tools``**（计划第 7 步点名的那条）。

## 这一门守的是什么

Hermes ``plugin_validate`` 的判据是**逐名比对**：实注册比声明**多**一个 ⇒ FAIL，
少一个 ⇒ 只 **WARN**。"少"只是 WARN，所以**静默缺工具**能一路绿灯过发布 ——
这正是 F5 的历史形态：

    旧实现把全部工具注册包在同一个 try 里，而 ``import plugin.tools.wecom``
    会在**模块加载期**解析 ``wecom-cli.js`` 并抛 FileNotFoundError
    ⇒ 被外层 except 吞成一行 WARNING ⇒ 其后的 **10 个工具（wecom 7 + extras 3）
    全部静默不注册**，日志看着完全正常。

⇒ 本文件钉四件事：**总数**、**无重名**、**与声明逐名一致**、**单点失败不连坐**。

## 与 ``tools/probe_registration.py`` 的分工

同一个空壳注册环境，但两边的作用不同、都要留：

- 探针是**脚本**（``gates.sh`` 里跑，发布前给人看「✅ 262 == 期望 262」那几行）；
- 本文件是**套件**（跟着 ``pytest`` 一起绿/红，且能做探针做不了的：
  **故意毒掉一个模块**看其余是否幸存、按域拆开数、AST 验"import 不在模块级"）。

所以这里**复用**探针的 ``_CollectorCtx`` / ``_declared`` / 空壳清单，
不另写一份（写两份迟早分叉 —— 那正是本仓反复吃亏的模式）。

## 本机为什么要空壳

``plugin/platform/adapter.py`` 模块级 ``from gateway.config import Platform``
（Hermes 独有）。真机验收在 ``.183`` 上另跑（V3/V6），本地这份的职责是
**在改注册代码的当下就红**，而不是等发布。
"""

from __future__ import annotations

import ast
import functools
import importlib
import importlib.util
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 独有能力各域的工具数（F5 丢的就是这一批）。改数=改能力面，必须是有意的。
EXTRA_COUNTS = {"wecom": 7, "phoneerp": 4, "extras": 3}


@functools.lru_cache(maxsize=1)
def _probe():
    """按路径加载 ``tools/probe_registration.py``（它不是包，不能 ``import``）。"""
    path = ROOT / "tools/probe_registration.py"
    spec = importlib.util.spec_from_file_location("_probe_registration", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def stubbed():
    """装上 gateway 空壳（+ 缺的运行期依赖），跑完**恢复原样**。

    必须恢复：这些是**进程级** ``sys.modules`` 条目，留在那里会让后续用例
    以为真的有个 ``gateway`` 包（本机没有），把"环境不干净"伪装成"代码没问题"。
    """
    probe = _probe()
    saved = {k: sys.modules.get(k, _MISSING) for k in probe._HERMES_INTERNAL}
    runtime = probe._stub_runtime()          # aiohttp/websockets/pymysql：缺了才顶
    saved_runtime = {k: sys.modules.get(k, _MISSING) for k in runtime}
    probe._stub_internal()
    try:
        yield probe
    finally:
        for k, v in {**saved, **saved_runtime}.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


class _MISSING_TYPE:
    pass


_MISSING = _MISSING_TYPE()


def _register() -> tuple[list[str], list[str]]:
    """真跑一遍 ``register(ctx)``，回 ``(工具名原始列表, 平台名)``。"""
    probe = _probe()
    ctx = probe._CollectorCtx()
    pkg = importlib.import_module("plugin")
    pkg.register(ctx)
    return ctx.names, ctx.platforms


# ==================================================== 平台 + 总数

def test_the_platform_registers(stubbed):
    """平台一条必须注册上（被兜底吞掉的表现是"工具有、平台没有"，静默）。"""
    _names, platforms = _register()
    assert platforms == ["wechatpadpro"], "平台名 = toolset 名 = config.yaml 的键"


def test_the_tool_count_is_exactly_the_declared_one(stubbed):
    """注册数 == 期望总数（262 = wpp 251 + wecom 7 + phoneerp 4）。"""
    names, _ = _register()
    assert len(set(names)) == stubbed.EXPECTED_TOTAL, \
        f"注册 {len(set(names))} 个 != 期望 {stubbed.EXPECTED_TOTAL}"


def test_no_tool_is_registered_twice(stubbed):
    """无重名 —— 必须数**原始列表**（``set`` 会把重命证据吃掉）。

    历史陷阱：把聚合项 ``ALL_EXTRA_TOOLS`` 当组注册 ⇒ 107 个工具注册两遍，
    而 Hermes 侧只会看到一份名字。
    """
    names, _ = _register()
    seen: dict[str, int] = {}
    for n in names:
        seen[n] = seen.get(n, 0) + 1
    dupes = {n: c for n, c in seen.items() if c > 1}
    assert not dupes, f"这些工具被注册多次（最可能是把聚合组当组注册了）: {sorted(dupes)[:10]}"
    assert len(names) == len(set(names))


def test_registered_names_match_provides_tools_exactly(stubbed):
    """★ 与 ``plugin.yaml`` 逐名一致 —— Hermes ``plugin_validate`` 的判据。

    多 ⇒ FAIL；少 ⇒ 只是 WARN（所以"少"必须在这里被挡下，否则能一路绿灯发布）。
    """
    names, _ = _register()
    actual = set(names)
    declared = set(stubbed._declared(ROOT / "plugin/plugin.yaml"))
    assert not (actual - declared), \
        f"实注册多出未声明的（plugin_validate 判 FAIL）: {sorted(actual - declared)[:10]}"
    assert not (declared - actual), \
        f"声明了但没注册（plugin_validate 只 WARN，容易漏过）: {sorted(declared - actual)[:10]}"


def test_the_unique_capabilities_are_all_there(stubbed):
    """独有能力（TS 侧被降级成纯 skill 的那批）按域逐个计数。

    总数对不代表这批对：它们**恰好是** F5 里被静默丢掉的那 14 个 ——
    漏掉它们时总数只差 14，而"少"在 Hermes 侧只是 WARN。
    """
    get_adapter = lambda account_id="default": None  # noqa: E731 — 注册期用不到
    for mod_name, expected in EXTRA_COUNTS.items():
        mod = importlib.import_module(f"plugin.tools.{mod_name}")
        ctx = _probe()._CollectorCtx()
        n = mod.register(ctx, get_adapter)
        assert n == expected, f"{mod_name} 注册 {n} 个，期望 {expected}"
        assert len(set(ctx.names)) == expected, f"{mod_name} 有重名"


# ==================================================== F5：单点失败不连坐

def test_one_broken_extra_module_does_not_take_the_others_down(stubbed, monkeypatch, caplog):
    """★ F5 的直接回归：毒掉 ``wecom``，其余独有能力**照常注册**且**日志可见**。

    ``sys.modules["..."] = None`` 是让 import 抛 ImportError 的标准手法
    （比替换 ``importlib.import_module`` 干净：只动这一个模块，不碰全局函数）。
    """
    names_before, _ = _register()
    assert "plugin.tools.wecom" in sys.modules, "前提：它平时是能导入的"

    with monkeypatch.context() as m:
        m.setitem(sys.modules, "plugin.tools.wecom", None)
        with caplog.at_level(logging.WARNING):
            names_broken, platforms = _register()

    # ① 失败必须**出声**（旧实现吞成一行 WARNING 还吞掉了后续 10 个工具；
    #    注意：_step 是 WARNING，不是 ERROR —— 所以这里按 WARNING 断言）
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("wecom" in w for w in warnings), \
        f"wecom 注册失败必须留痕（否则表现成「工具莫名少了 7 个」）: {warnings}"
    assert any("注册失败" in w for w in warnings)

    # ② 平台不受影响
    assert platforms == ["wechatpadpro"]

    # ③ 其余独有能力幸存（这就是 F5 修的那一条）
    assert len(names_broken) == len(names_before) - EXTRA_COUNTS["wecom"], \
        "只应少了 wecom 那 7 个 —— 多丢几个说明隔离失效（F5 复发）"
    from plugin.tools import extras, phoneerp        # 能被 import 到即"注册过"
    assert extras and phoneerp

    # ④ 恢复后一切照旧（失败不是永久状态）
    names_after, _ = _register()
    assert sorted(names_after) == sorted(names_before)


def test_the_tool_modules_are_never_imported_at_module_level():
    """★ 源级门（AST）：``registration.py`` 的**模块级**不得 import 任何 ``.tools.*``。

    F5 的根因不是"没写 try/except"，而是**import 发生在模块加载期** ——
    那时它在任何 try 之外，一炸就整批消失。所以这条不是风格要求：
    把 import 挪回模块级 = 把 F5 原样复活（哪怕 try/except 还留着）。
    """
    tree = ast.parse((ROOT / "plugin/registration.py").read_text(encoding="utf-8"))
    top_imports = [
        (node.module or "") for node in tree.body if isinstance(node, ast.ImportFrom)
    ] + [
        alias.name for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names
    ]
    offenders = [m for m in top_imports if "tools" in m.split(".")]
    assert not offenders, f"模块级不许 import 工具模块（F5 的形态）: {offenders}"

    src = (ROOT / "plugin/registration.py").read_text(encoding="utf-8")
    assert 'importlib.import_module(f".tools.{mod_name}", __package__)' in src, \
        "独有能力模块必须在**调用期**按 ``.tools.<name>`` 导入" \
        "（写成 f\".{mod_name}\" 会解析成 plugin.<name> ⇒ 14 个工具静默消失，实测踩过）"
