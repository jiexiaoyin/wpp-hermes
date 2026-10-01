"""唯一注册入口（Hermes plugin entry point）。

## 为什么只有一个入口

Hermes 0.21.5 对 **user 目录插件**是 eager 加载 —— ``plugins_discovery.gate_manifest``
里只有 ``source == "bundled"`` 且 ``kind == "platform"`` 才返回 ``defer``；user 插件
一律走 ``ManifestGate("load")``，立即 import 并调用 ``register(ctx)``。

因此历史实现里那条注释「声明 provides_tools → deferred discovery 会调
tools.py:register_tools()」**是错的**。在 .183 实测确认：每次 gateway 启动只出现
一份注册日志，且伴随只有 ``adapter.register()`` 才打的 ``[WPP] 平台已注册``；
``register_tools()`` 从未被调用。

⇒ 本模块是**唯一**入口。不要再新增第二条并行路径。

> 排查提示：日志里看似出现「两份」注册记录时，先看有没有
> ``SIGUSR1 received ... graceful gateway restart`` —— 那多半是
> **重启前后两个进程各一份**（旧进程还没退干净），不是重复注册。

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
from typing import Any, Callable

log = logging.getLogger(__name__)

#: 「独有能力」模块（TS 侧没有、被降级成纯 skill 的那批）。
#: ⚠️ 名字是 ``plugin/tools/`` **包内**的模块名（见 :func:`_register_extra` 的导入前缀）。
#: ⚠️ 这些模块历史上在**模块导入期**解析外部 CLI（``wecom-cli.js``），
#: 加载期抛错会被外层 except 吞掉 ⇒ 整批工具消失（F5, 10 个工具）。所以
#: import 必须发生在**调用期**且各自隔离。模块名 → 日志标签。
_EXTRA_TOOL_MODULES: tuple[tuple[str, str], ...] = (
    ("phoneerp", "phoneerp"),
    ("wecom", "wecom"),
    ("extras", "wpp_extras"),
)


def register(ctx: Any) -> None:
    """Hermes 唯一注册入口。"""
    _step("平台", lambda: _register_platform(ctx))
    _step("工具", lambda: _register_tools(ctx))
    log.info("[WPP] 注册流程完成")


# --------------------------------------------------------------------------- 内部

def _step(label: str, fn: Callable[[], Any]) -> Any:
    """跑一个注册子步骤，失败只记日志、不向上抛（保证后续步骤继续）。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — 故意兜底：单点失败不得中断整批注册
        log.warning("[WPP] %s 注册失败: %s", label, exc, exc_info=True)
        return None


def _register_platform(ctx: Any) -> None:
    # ★ 走包内相对导入（不依赖 loader 的裸名映射 ``_BARE_MODULE_SCOPE``）。
    #   旧扁平 ``plugin/adapter.py`` 已由 ``plugin/platform/adapter.py`` 取代。
    from .platform import adapter as _adapter
    from .platform import enablement

    ctx.register_platform(
        name=enablement.PLATFORM_NAME,
        label="WeChatPadPro (Personal WeChat)",
        adapter_factory=_adapter._build_adapter,
        check_fn=_adapter.check_wechatpadpro_requirements,
        # 三个一起换掉（原来是 adapter 里的旧实现）：
        # is_connected 的死锁是平台起不来的根因，见 platform/enablement.py
        is_connected=enablement.is_connected,
        validate_config=enablement.validate_config,
        # ★ 新增：没有它，平台永远进不了 config.platforms
        env_enablement_fn=enablement.env_enablement_fn,
        required_env=["WECHATPRO_AUTHCODE"],
        install_hint=(
            "Run `hermes setup`, or set WECHATPRO_AUTHCODE in ~/.hermes/.env, "
            "or write platforms.wechatpadpro.enabled: true in config.yaml."
        ),
        allowed_users_env="WECHATPRO_ALLOWED_USERS",
        allow_all_env="WECHATPRO_ALLOW_ALL_USERS",
        cron_deliver_env_var="WECHATPRO_HOME_CHANNEL",
        standalone_sender_fn=_adapter._standalone_send,
        max_message_length=4000,
        emoji="💬",
        allow_update_command=True,
        platform_hint=(
            "You are chatting via WeChatPadPro (personal WeChat, vendor wx.juhe.chat). "
            "Group chats: reply with at-mentions (@nickname). Plain text only. "
            "Current account resolves from the session metadata."
        ),
    )
    log.info("[WPP] 平台已注册: %s", enablement.PLATFORM_NAME)


def _register_tools(ctx: Any) -> None:
    from .platform import adapter as _adapter
    get_adapter = _adapter._get_adapter

    # 具名子域。⚠️ 全部在**函数内**导入：`msg` 的模块级只有标准库，但 `mcp` 等
    #    会在函数体里碰 DB —— import 期任何重依赖都会炸掉 plugin_validate 的隔离 probe。
    from .tools import jargon_tool, mcp, msg
    from .tools import registry as _registry
    from .tools import vendor_defs, vendor_defs_extra

    _step("工具:msg", lambda: _count("msg", msg.register_msg_tools(ctx, get_adapter)))
    _step("工具:jargon", lambda: jargon_tool.register_jargon_tools(ctx))
    _step("工具:mcp", lambda: mcp.register_mcp_tools(ctx))

    # 厂商端点表（112 + 107 = 219 条）：**组清单从生成物派生**，不再手写。
    #   ⚠️ 历史包袱：旧实现手写了一张组名元组，靠 getattr 逐个取 —— 组名拼错就
    #   **静默跳过整组**（同类 bug 见模块 docstring 的 F5）。现在组名与顺序都在
    #   vendor_defs*.GROUPS 里，由 tools/gen_vendor_defs.py 从旧表机械生成。
    total = 0
    for group, defs in _all_generic_groups(vendor_defs, vendor_defs_extra):
        _step(
            f"工具:{group}",
            lambda d=defs, g=group: _count(g, _registry.register_generic_tools(ctx, get_adapter, d)),
        )
        total += len(defs)
    if not total:
        # 空表 = 生成物坏了／没接线。这条**必须吵**：静默的空注册正是要消灭的模式。
        log.error("[WPP] 厂商端点表为空（vendor_defs*.GROUPS 没有内容）—— 219 个工具都没注册！")

    # 独有能力：import 在调用期、且逐个隔离（见模块 docstring 的历史事故）
    for mod_name, label in _EXTRA_TOOL_MODULES:
        _step(
            f"工具:{label}",
            lambda m=mod_name, l=label: _register_extra(ctx, m, l, get_adapter),
        )


def _all_generic_groups(vendor_defs: Any, vendor_defs_extra: Any) -> list[tuple[str, Any]]:
    """``vendor_defs*.GROUPS`` 的顺序拼接 —— 顺序即注册顺序（旧日志按此对照）。

    组清单**只**来自生成物（见 :mod:`plugin.tools` 的说明），本函数不做任何筛选、
    不写第二份名单：生成器已经把聚合项 ``ALL_EXTRA_TOOLS`` 排除掉了
    （它是全部 EXTRA 组的拼接，当组注册会让 107 个工具重复注册）。
    """
    return [*vendor_defs.GROUPS, *vendor_defs_extra.GROUPS]


def _register_extra(ctx: Any, mod_name: str, label: str, get_adapter: Callable[[], Any]) -> int:
    """注册一个「独有能力」模块。

    ⚠️ import 在函数内（调用期）—— 这些模块历史上在 import 时就解析外部 CLI，
    加载期抛错会连带整批工具消失（见模块 docstring）。
    """
    # ⚠️ 必须带 ``.tools.`` 前缀：这三个模块住在 ``plugin/tools/`` 里（第 4 步随工具层
    #    一起搬进来的），不是 ``plugin/`` 顶层。写成 ``f".{mod_name}"`` 会解析成
    #    ``plugin.phoneerp`` ⇒ ModuleNotFoundError ⇒ 被下面 _step 的兜底吞成一行
    #    WARNING ⇒ **14 个"独有能力"工具静默不注册**（wecom 7 + phoneerp 4 +
    #    wpp_extras 3）。这正是 F5 的同一种失败形态，2026-10-01 由
    #    ``tools/probe_registration.py`` 在真跑 register() 时抓到。
    mod = importlib.import_module(f".tools.{mod_name}", __package__)
    n = mod.register(ctx, lambda account_id="default": get_adapter())
    log.info("[WPP] %s 工具注册: %d 个", label, n)
    return n


def _count(label: str, n: Any) -> Any:
    log.info("[WPP] %s 工具注册: %s 个", label, n)
    return n
