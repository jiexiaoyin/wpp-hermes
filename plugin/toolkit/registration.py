"""工具面注册实现（客户端工具，不含平台适配器）。

入口有两个，最终都落到 :func:`register_tools`：

* 根 ``plugin/tools.py`` 的 ``register_tools(ctx)`` —— Hermes ``provides_tools``
  的 deferred 预注册路径；
* 根 ``plugin/registration.py`` 的 ``register(ctx)`` —— eager 加载路径
  （用户目录插件当前就走这条；deferred 插件物化时也会经过这里）。

重复调用由 ``plugin/tools.py`` 的模块级标记挡住，本模块不重复做幂等判断。

## 为什么每个子步骤独立 try/except

历史事故（已在 .183 复现）：旧实现把全部工具注册包在**同一个** try 里，而
``from . import wecom_tools`` 会在**模块加载期**解析 ``wecom-cli.js`` 并抛
``FileNotFoundError`` ⇒ 被外层 except 吞成一行 WARNING ⇒ 其后的
**wecom 7 + wpp_extras 3 = 10 个工具全部静默不注册**。

⇒ 现在每个子域各自 try/except：任何单点失败不得中断后续，且失败必须在日志里可见。
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

#: 「独有能力」模块（TS 侧没有、被降级成纯 skill 的那批）。
#: ⚠️ 名字是 ``plugin/toolkit/`` **包内**的模块名（见 :func:`_register_extra` 的导入前缀）。
#: ⚠️ 这些模块历史上在**模块导入期**解析外部 CLI（``wecom-cli.js``），
#: 加载期抛错会被外层 except 吞掉 ⇒ 整批工具消失（F5, 10 个工具）。所以
#: import 必须发生在**调用期**且各自隔离。模块名 → 日志标签。
_EXTRA_TOOL_MODULES: tuple[tuple[str, str], ...] = (
    ("phoneerp", "phoneerp"),
    ("wecom", "wecom"),
    ("extras", "wpp_extras"),
)

#: 手写域的工具**应有条数**。这些模块的注册函数不返回计数（有一部分是生成物，
#: 不能改），所以用常量对账。数量变了必须**显式**改这里 —— 对不上就报 ERROR：
#: 旧实现只数"定义表里有多少条"，一组整体没注册也能全绿。
_EXPECTED_DOMAIN_COUNTS: dict[str, int] = {
    "msg": 22,
    "jargon": 1,
    "mcp": 10,   # 2026-10-19: +translate / group_members / send_pat（vendor MCP 真实能力）
    "parity": 4,
}

#: 最近一次注册的对账结果（探针/测试可读）：``{"ok": bool, "problems": [...]}``。
REGISTRATION_REPORT: dict[str, Any] = {"ok": True, "problems": []}

#: 最近一次注册**真实成功**的全部工具名（含非端点表的具名工具）。
#: ``wpp_tools_list`` 读它 —— 以前只读 vendor_defs 的 GROUPS，于是 msg/parity/
#: extras 等 ~30 个工具在"列出所有工具"里**根本不存在**（模型自然会以为没有）。
REGISTERED_TOOL_NAMES: list[str] = []


def register_tools(ctx: Any) -> None:
    """注册全部客户端工具（平台适配器不在本函数范围内）。"""
    # ★ 调用契约收口：Hermes 是 ``handler(args, **kw)``（位置 dict），本插件全是
    #   ``fn(**params)`` ⇒ 收在这里包一层，299 个工具一次修好、且将来新增的忘不掉。
    #   证据与两个失败形态见 toolkit/hermes_contract.py 的模块 docstring。
    from ..platform import adapter as _adapter
    from . import hermes_contract as _contract
    from . import policy as _policy

    # ★ 运行期门禁（W7-5）挂在这同一个 seam 上：它是"所有工具、所有注册路径"必经的一层。
    #   getter 写成 lambda 是为了**每次调用**都重新取模块属性 —— 单测与工具面探针
    #   是在 register() 之前 monkeypatch ``_get_adapter`` 的，直接传函数对象会让补丁落空。
    ctx = _contract.wrap_ctx(ctx, _policy.make_tool_gate(lambda: _adapter._get_adapter()))
    _register_tools(ctx)
    global REGISTERED_TOOL_NAMES
    REGISTERED_TOOL_NAMES = getattr(ctx, "registered_names", []) or []


# --------------------------------------------------------------------------- 内部

def _step(label: str, fn: Callable[[], Any]) -> Any:
    """跑一个注册子步骤，失败只记日志、不向上抛（保证后续步骤继续）。"""
    try:
        return fn()
    except Exception as exc:
        log.warning("[WPP] %s 注册失败: %s", label, exc, exc_info=True)
        return None


def _register_tools(ctx: Any) -> None:
    from ..platform import adapter as _adapter
    get_adapter = _adapter._get_adapter

    # 具名子域。⚠️ 全部在**函数内**导入：`msg` 的模块级只有标准库，但 `mcp` 等
    #    会在函数体里碰 DB —— import 期任何重依赖都会炸掉 plugin_validate 的隔离 probe。
    from . import jargon_tool, mcp, msg, parity, vendor_defs, vendor_defs_extra
    from . import policy as _policy
    from . import registry as _registry

    problems: list[str] = []
    REGISTRATION_REPORT.update(ok=True, problems=problems)

    def _run(label: str, fn: Callable[[], Any], expected: int) -> Any:
        """跑一个注册子步骤，并用**真实注册次数增量**对账。

        ⚠️ 不用 :func:`_step` 的返回值判成败：jargon/mcp 的注册函数本来就不返回
        计数（``None`` 是正常值），拿 ``None`` 当异常会把好路径误报成失败。
        """
        before = getattr(ctx, "registered_count", 0)
        try:
            result = fn()
        except Exception as exc:
            log.warning("[WPP] %s 注册失败: %s", label, exc, exc_info=True)
            problems.append(f"{label}: 注册抛错（{exc}）")
            return None
        got = getattr(ctx, "registered_count", before) - before
        if got != expected:
            problems.append(f"{label}: 期望 {expected} 个，实际注册 {got} 个")
        return result

    _run("工具:msg", lambda: _count("msg", msg.register_msg_tools(ctx, get_adapter)),
         _EXPECTED_DOMAIN_COUNTS["msg"])
    _run("工具:jargon", lambda: jargon_tool.register_jargon_tools(ctx),
         _EXPECTED_DOMAIN_COUNTS["jargon"])
    _run("工具:mcp", lambda: mcp.register_mcp_tools(ctx, get_adapter),
         _EXPECTED_DOMAIN_COUNTS["mcp"])
    _run("工具:parity", lambda: parity.register_parity_tools(ctx, get_adapter),
         _EXPECTED_DOMAIN_COUNTS["parity"])

    # 厂商端点表（112 + 107 = 219 条生成物，DENY 撤 3 条后实注册 216 条）：
    #   **组清单从生成物派生**，不再手写。
    #   ⚠️ 历史包袱：旧实现手写了一张组名元组，靠 getattr 逐个取 —— 组名拼错就
    #   **静默跳过整组**（同类 bug 见模块 docstring 的 F5）。现在组名与顺序都在
    #   vendor_defs*.GROUPS 里，由 tools/gen_vendor_defs.py 从旧表机械生成。
    # 策略层：撤注册／纠错。**必须在注册前套**，且只作用在这一层 ——
    # 生成物（vendor_defs*.py）有「数据保真」契约（gen_vendor_defs.py --check 逐字节比对），
    # 一个字都不能动。差异与出处全在 toolkit/policy.py。
    if _policy.DENY_TOOLS:
        log.info(
            "[WPP] 工具策略：撤注册 %d 个（%s）—— 对齐 TS 裁决，见 toolkit/policy.py",
            len(_policy.DENY_TOOLS), "、".join(sorted(_policy.DENY_TOOLS)),
        )

    total = 0
    for group, defs in _policy.iter_groups(_all_generic_groups(vendor_defs, vendor_defs_extra)):
        _run(
            f"工具:{group}",
            lambda d=defs, g=group: _count(g, _registry.register_generic_tools(ctx, get_adapter, d)),
            len(defs),
        )
        total += len(defs)

    # 策略层补注册（TS 有、蓝本没有的工具）。形状与 GROUPS 的元素一致。
    _added_group, _added_defs = _policy.ADDED_GROUP
    _run(
        f"工具:{_added_group}",
        lambda d=_added_defs, g=_added_group: _count(
            g, _registry.register_generic_tools(ctx, get_adapter, d)
        ),
        len(_added_defs),
    )
    total += len(_added_defs)

    if not total:
        # 空表 = 生成物坏了／没接线。这条**必须吵**：静默的空注册正是要消灭的模式。
        # ⚠️ 这里**不写具体条数** —— 条数会随策略层变动，写死就等着腐烂。
        log.error("[WPP] 厂商端点表为空（vendor_defs*.GROUPS 没有内容）—— 全部通用工具都没注册！")

    # 独有能力：import 在调用期、且逐个隔离（见模块 docstring 的历史事故）
    for mod_name, label in _EXTRA_TOOL_MODULES:
        before = getattr(ctx, "registered_count", 0)
        n = _step(
            f"工具:{label}",
            lambda m=mod_name, l=label: _register_extra(ctx, m, l, get_adapter),
        )
        got = getattr(ctx, "registered_count", before) - before
        if n is None:
            problems.append(f"工具:{label}: 注册抛错（0 个）")
        elif isinstance(n, int) and got != n:
            problems.append(f"工具:{label}: 声称 {n} 个，实际注册 {got} 个")

    if problems:
        REGISTRATION_REPORT["ok"] = False
        log.error("[WPP] 工具注册**不完整**（%d 处）：%s —— 工具面已降级，"
                  "用 tools/probe_registration.py 对账", len(problems), "；".join(problems))
    else:
        log.info("[WPP] 工具注册对账通过：实际注册 %s 个",
                 getattr(ctx, "registered_count", "?"))


def _all_generic_groups(vendor_defs: Any, vendor_defs_extra: Any) -> list[tuple[str, Any]]:
    """``vendor_defs*.GROUPS`` 的顺序拼接 —— 顺序即注册顺序（旧日志按此对照）。

    组清单**只**来自生成物（见 :mod:`plugin.toolkit` 的说明），本函数不做任何筛选、
    不写第二份名单：生成器已经把聚合项 ``ALL_EXTRA_TOOLS`` 排除掉了
    （它是全部 EXTRA 组的拼接，当组注册会让 107 个工具重复注册）。
    """
    return [*vendor_defs.GROUPS, *vendor_defs_extra.GROUPS]


def _register_extra(ctx: Any, mod_name: str, label: str, get_adapter: Callable[[], Any]) -> int:
    """注册一个「独有能力」模块。

    ⚠️ import 在函数内（调用期）—— 这些模块历史上在 import 时就解析外部 CLI，
    加载期抛错会连带整批工具消失（见模块 docstring）。
    """
    # ⚠️ 必须带 ``.toolkit.`` 前缀：这三个模块住在 ``plugin/toolkit/`` 里（第 4 步随工具层
    #    一起搬进来的），不是 ``plugin/`` 顶层。写成 ``f".{mod_name}"`` 会解析成
    #    ``plugin.phoneerp`` ⇒ ModuleNotFoundError ⇒ 被下面 _step 的兜底吞成一行
    #    WARNING ⇒ **14 个"独有能力"工具静默不注册**（wecom 7 + phoneerp 4 +
    #    wpp_extras 3）。这正是 F5 的同一种失败形态，2026-10-01 由
    #    ``tools/probe_registration.py`` 在真跑 register() 时抓到。
    mod = importlib.import_module(f".{mod_name}", __package__)
    n = mod.register(ctx, lambda account_id="default": get_adapter())
    log.info("[WPP] %s 工具注册: %d 个", label, n)
    return n


def _count(label: str, n: Any) -> Any:
    log.info("[WPP] %s 工具注册: %s 个", label, n)
    return n
