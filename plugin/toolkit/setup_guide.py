"""``wpp_setup`` —— 安装/配置引导工具。

装好插件后，用户可以直接对 Hermes 说“帮我配置微信”，模型调用本工具读取当前
配置状态并给出下一步；**不通过聊天收集 authcode**，只引导用户在自己的终端运行
``setup_wizard.py``。

两个动作：

* ``check``：返回 home/profile、插件是否启用、已配置/缺失项（不含密钥值）；
* ``guide``：返回分步 Markdown 指令。
"""

from __future__ import annotations

import json
import logging

from .registry import mk_handler

log = logging.getLogger(__name__)

_DESC = (
    "检查 WeChatPadPro 插件配置状态并给出安装/配置引导。"
    "不读取、不回传 authcode/tokenKey 等密钥。"
)


async def wpp_setup(**params) -> str:
    from .. import setup_wizard

    action = str(params.get("action") or "check").strip().lower()
    if action == "guide":
        return setup_wizard.guide_text()
    status = setup_wizard.check_status()
    return json.dumps(status, ensure_ascii=False, indent=2)


def register(ctx, adapter_getter) -> int:
    """注册 ``wpp_setup``（不需要 adapter）。"""
    ctx.register_tool(
        name="wpp_setup",
        toolset="wechatpadpro",
        schema={
            "type": "object",
            "description": _DESC,
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["check", "guide"],
                    "description": "check=读取状态；guide=分步配置引导",
                    "default": "check",
                },
            },
        },
        handler=mk_handler(wpp_setup),
        is_async=True,
        description=_DESC,
        emoji="🧭",
    )
    log.info("[WPP] 已注册 wpp_setup 工具")
    return 1
