"""Hermes 插件入口（``register(ctx)``）：平台 + 工具面。

## 两条加载路径

1. **eager 路径**（用户目录插件，当前 ``~/.hermes/plugins/wechatpadpro`` 走的
   就是这条）：Hermes 在 discovery 阶段 import 本包并调用 ``register(ctx)``。
2. **deferred 路径**（bundled 平台插件，以及未来可能的用户插件延迟加载）：
   Hermes 先 import ``<plugin>.tools`` 调 ``register_tools(ctx)`` 预注册客户端
   工具，等平台第一次被请求时才 import 本包、调用 ``register(ctx)``。

两条路径都落到 :func:`plugin.tools.register_tools`；那里有进程级幂等标记，
所以重复调用不会把工具表注册两遍。

## 为什么每个子步骤独立 try/except

历史事故（已在 真机 复现）：旧实现把全部工具注册包在**同一个** try 里，而
``from . import wecom_tools`` 会在**模块加载期**解析 ``wecom-cli.js`` 并抛
``FileNotFoundError`` ⇒ 被外层 except 吞成一行 WARNING ⇒ 其后的
**wecom 7 + wpp_extras 3 = 10 个工具全部静默不注册**。

⇒ 现在平台与工具面各自隔离，工具面内部再按子域逐个隔离（见
``toolkit/registration.py``）。

> 排查提示：日志里看似出现「两份」注册记录时，先看有没有
> ``SIGUSR1 received ... graceful gateway restart`` —— 那多半是
> **重启前后两个进程各一份**（旧进程还没退干净），不是重复注册。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Hermes 插件入口。"""
    _step("平台", lambda: _register_platform(ctx))
    _step("工具", lambda: _register_tools(ctx))
    _step("会话标题钩子", lambda: _register_hooks(ctx))
    log.info("[WPP] 注册流程完成")


# --------------------------------------------------------------------------- 内部

def _step(label: str, fn: Callable[[], Any]) -> Any:
    """跑一个注册子步骤，失败只记日志、不向上抛（保证后续步骤继续）。"""
    try:
        return fn()
    except Exception as exc:
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
        # ★ 必须与 outbound.MAX_MESSAGE_LENGTH 同值（2026-10-18 4000→2000）：
        #   微信个人号长文本超过约 2000 字符会被静默截断；cron/standalone 分片
        #   与 live adapter 分片必须同一阈值，否则同一段回复两条路径长度不同。
        max_message_length=_adapter.MAX_MESSAGE_LENGTH,
        # ★ 微信没有状态栏、消息发出撤不回：按 LOW 档（同 weixin/signal/wecom）——
        #   tool_progress=off、interim_assistant_messages=false、long_running=false、
        #   busy_ack_detail=false。老板 2026-10-18 拍板「只发结果，不发过程」。
        #   适配器侧还有 ``_interim_send`` 闸 + ``send_or_update_status`` 空实现兜底。
        display_tier="low",
        emoji="💬",
        allow_update_command=True,
        platform_hint=(
            "You are chatting via WeChatPadPro (personal WeChat). "
            "Group chats: reply with at-mentions (@nickname). Plain text only. "
            "Current account resolves from the session metadata."
        ),
    )
    log.info("[WPP] 平台已注册: %s", enablement.PLATFORM_NAME)


def _register_tools(ctx: Any) -> None:
    """委托给 ``plugin/tools.py``（deferred 预注册与 eager 注册共用同一入口）。"""
    from . import tools as _client_tools

    _client_tools.register_tools(ctx)


def _register_hooks(ctx: Any) -> None:
    """把 WebUI 会话标题接到 Hermes 的 ``pre_llm_call`` 上。

    只对本平台的**首轮**生效：群聊写成「群名 · 发送者」、私聊写成联系人昵称，
    见 ``inbound/session_title.py``。单独一步注册 —— 与平台/工具面互相隔离，
    钩子注册失败不影响收发消息。
    """
    from .inbound import session_title

    ctx.register_hook("pre_llm_call", session_title.on_pre_llm_call)
    log.info("[WPP] 会话标题钩子已注册（pre_llm_call）")
