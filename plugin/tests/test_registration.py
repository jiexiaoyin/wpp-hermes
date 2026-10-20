"""注册面回归 —— **注册数 == ``plugin.yaml provides_tools``**（计划第 7 步点名的那条）。

⚠️ 本文件**只管"注册得上/名字对"**。"注册上但调不动"是另一回事（那正是 2026-10-02
在 ``真机`` 真机抓到的 P0）—— 调用契约回归在 ``test_hermes_tool_contract.py``。
两件事分开，是因为它们**互相不覆盖**：注册数、逐名对账、``doctor`` 在工具全不可调用时
照样全绿。

## 这一门守的是什么

Hermes ``plugin_validate`` 的判据是**逐名比对**：实注册比声明**多**一个 ⇒ FAIL，
少一个 ⇒ 只 **WARN**。"少"只是 WARN，所以**静默缺工具**能一路绿灯过发布 ——
这正是 F5 的历史形态：

    旧实现把全部工具注册包在同一个 try 里，而 ``import plugin.toolkit.wecom``
    会在**模块加载期**解析 ``wecom-cli.js`` 并抛 FileNotFoundError
    ⇒ 被外层 except 吞成一行 WARNING ⇒ 其后的工具全部静默不注册，
    日志看着完全正常。（wecom/phoneerp 包装已迁出本插件，隔离要求不变。）

⇒ 本文件钉四件事：**总数**、**无重名**、**与声明逐名一致**、**单点失败不连坐**。

## 与 ``tools/probe_registration.py`` 的分工

同一个空壳注册环境，但两边的作用不同、都要留：

- 探针是**脚本**（``gates.sh`` 里跑，发布前给人看「✅ 261 == 期望 261」那几行）；
- 本文件是**套件**（跟着 ``pytest`` 一起绿/红，且能做探针做不了的：
  **故意毒掉一个模块**看其余是否幸存、按域拆开数、AST 验"import 不在模块级"）。

所以这里**复用**探针的 ``_CollectorCtx`` / ``_declared`` / 空壳清单，
不另写一份（写两份迟早分叉 —— 那正是本仓反复吃亏的模式）。

## 本机为什么要空壳

``plugin/platform/adapter.py`` 模块级 ``from gateway.config import Platform``
（Hermes 独有）。真机验收在 ``真机`` 上另跑（V3/V6），本地这份的职责是
**在改注册代码的当下就红**，而不是等发布。

⚠️ 空壳夹具（``stubbed`` / ``_probe``）**住在 ``conftest.py``** —— 它原来在本文件里，
别的测试模块靠 pytest"按名字找"的副作用跨模块用。那样在**单文件跑**时会整文件 ERROR，
而 ``tools/mutation_check.py`` 正是单文件跑的（会把变异体误判成 BROKEN ⇒ 不计入杀死数）。
"""

from __future__ import annotations

import ast
import importlib
import logging
import sys
from pathlib import Path

from .conftest import _probe

ROOT = Path(__file__).resolve().parents[2]

#: 独有能力各域的工具数（F5 丢的就是这一批）。改数=改能力面，必须是有意的。
#: wecom_* / phoneerp_* 已迁回各自 skill，不再计入本插件。
EXTRA_COUNTS = {"extras": 3, "setup_guide": 1}


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
    """注册数 == 期望总数（WPP vendor 端点 + extras + setup_guide）。"""
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

    总数对不代表这批对：漏掉它们时总数只差几个，而"少"在 Hermes 侧只是 WARN。
    """
    get_adapter = lambda account_id="default": None  # noqa: E731 — 注册期用不到
    for mod_name, expected in EXTRA_COUNTS.items():
        mod = importlib.import_module(f"plugin.toolkit.{mod_name}")
        ctx = _probe()._CollectorCtx()
        n = mod.register(ctx, get_adapter)
        assert n == expected, f"{mod_name} 注册 {n} 个，期望 {expected}"
        assert len(set(ctx.names)) == expected, f"{mod_name} 有重名"


# ==================================================== F5：单点失败不连坐

def test_one_broken_extra_module_does_not_take_the_others_down(stubbed, monkeypatch, caplog):
    """★ F5 的直接回归：毒掉 ``setup_guide``，其余独有能力**照常注册**且**日志可见**。

    ``sys.modules["..."] = None`` 是让 import 抛 ImportError 的标准手法
    （比替换 ``importlib.import_module`` 干净：只动这一个模块，不碰全局函数）。
    """
    names_before, _ = _register()
    assert "plugin.toolkit.setup_guide" in sys.modules, "前提：它平时是能导入的"

    with monkeypatch.context() as m:
        m.setitem(sys.modules, "plugin.toolkit.setup_guide", None)
        with caplog.at_level(logging.WARNING):
            names_broken, platforms = _register()

    # ① 失败必须**出声**（旧实现吞成一行 WARNING 还吞掉了后续工具；
    #    注意：_step 是 WARNING，不是 ERROR —— 所以这里按 WARNING 断言）
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("wpp_setup" in w or "setup_guide" in w for w in warnings), \
        f"setup_guide 注册失败必须留痕（否则表现成「工具莫名少了 1 个」）: {warnings}"
    assert any("注册失败" in w for w in warnings)

    # ② 平台不受影响
    assert platforms == ["wechatpadpro"]

    # ③ 其余独有能力幸存（这就是 F5 修的那一条）
    assert len(names_broken) == len(names_before) - EXTRA_COUNTS["setup_guide"], \
        "只应少了 wpp_setup 那 1 个 —— 多丢几个说明隔离失效（F5 复发）"
    from plugin.toolkit import extras        # 能被 import 到即"注册过"
    assert extras

    # ④ 恢复后一切照旧（失败不是永久状态）
    names_after, _ = _register()
    assert sorted(names_after) == sorted(names_before)


def test_the_tool_modules_are_never_imported_at_module_level():
    """★ 源级门（AST）：``registration.py`` 的**模块级**不得 import 任何 ``.toolkit.*``。

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

    src = (ROOT / "plugin/toolkit/registration.py").read_text(encoding="utf-8")
    assert 'importlib.import_module(f".{mod_name}", __package__)' in src, \
        "独有能力模块必须在**调用期**按包内相对名导入" \
        "（写成 f\".{mod_name}\" 且 __package__ 不对 ⇒ 14 个工具静默消失，实测踩过）"


def test_hermes_manifest_entrypoints_exist():
    """★ Hermes 规范门：``plugin.yaml`` 声明 ``provides_tools`` ⇒ 根目录必须有
    ``tools.py``，且暴露 ``register_tools(ctx)``（见 ``plugins_loader._register_deferred_platform_tools``）。

    用户目录插件目前是 eager 加载，不会被走这条；但 manifest 一旦声明就必须能兑现，
    否则在 deferred 场景下 Hermes 只打一行 WARNING、工具全部缺席（#78050 的形态）。
    """
    import yaml

    manifest = yaml.safe_load((ROOT / "plugin/plugin.yaml").read_text(encoding="utf-8"))
    assert manifest.get("provides_tools"), "前提：manifest 声明了 provides_tools"
    tools_py = ROOT / "plugin/tools.py"
    assert tools_py.is_file(), "声明 provides_tools 却没有根级工具入口文件"
    mod = importlib.import_module("plugin.tools")
    assert callable(getattr(mod, "register_tools", None)), \
        "plugin/tools.py 必须暴露 register_tools(ctx)"


def test_register_tools_only_marks_the_manager_when_the_report_is_ok(monkeypatch):
    """★ 对账失败时**不许**把 manager 标成"已注册"。

    标了就等于：半套工具面 + 后续入口全部短路 + 日志里只有一句"完成"——
    模型那边少了一半工具，而人看日志以为一切正常。
    """
    from plugin import tools
    from plugin.toolkit import registration

    class _Mgr:
        pass

    class _Ctx:
        _manager = _Mgr()

    ctx = _Ctx()
    monkeypatch.setattr(registration, "register_tools", lambda _c: None)
    monkeypatch.setattr(registration, "REGISTRATION_REPORT",
                        {"ok": False, "problems": ["工具:msg: 期望 22 个，实际注册 0 个"]})
    tools._REGISTERED_MANAGERS.clear()
    tools.register_tools(ctx)
    assert id(ctx._manager) not in tools._REGISTERED_MANAGERS, \
        "对账没过的注册不许被标记（否则再也不会重试）"

    monkeypatch.setattr(registration, "REGISTRATION_REPORT", {"ok": True, "problems": []})
    tools.register_tools(ctx)
    assert tools._REGISTERED_MANAGERS.get(id(ctx._manager)) is ctx._manager
    tools._REGISTERED_MANAGERS.clear()
