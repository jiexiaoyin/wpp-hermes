"""WPP xiaowei 域 (小微智能体) 工具集 — 占位实现。

xiaowei 是微信小微 AI 智能体 (类似 GPT), v1.3.71 引入, 默认 disabled.

老板目前不需要, 所以这里只把工具元数据补全 (让 hermes tools 列表齐 232→251),
实际调用全部走 placeholder raise (提示用 /xiaowei on 开启).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _placeholder(**kwargs) -> dict:
    """所有 xiaowei 工具的占位实现。"""
    return {
        "_placeholder": True,
        "tool": "xiaowei",
        "msg": "小微智能体未启用 (xiaoweiEnabled=false), 用 /xiaowei on 开启",
        "kwargs": kwargs,
    }


def register_xiaowei_tools(ctx, adapter_getter) -> int:
    """注册 xiaowei 域 19 个工具 (全部占位, 默认 disabled)."""
    tools_def = [
        ("wpp_xw_create_session", "创建小微 AI 会话 (返回 events_url 供订阅 SSE 事件流).",
         {"clientRequestId": {"type": "string"}, "roomId": {"type": "string"}, "welcomeText": {"type": "string"}}),
        ("wpp_xw_get_session", "获取小微会话状态 (当前消息/房间/最后事件序号).",
         {"sessionId": {"type": "string", "required": True}}),
        ("wpp_xw_send_message", "向小微会话发送消息 (text 必填).",
         {"sessionId": {"type": "string", "required": True}, "text": {"type": "string", "required": True},
          "context": {"type": "array"}}),
        ("wpp_xw_cancel", "取消小微当前回答.",
         {"sessionId": {"type": "string", "required": True}}),
        ("wpp_xw_regenerate", "重新生成小微回答.",
         {"sessionId": {"type": "string", "required": True}, "messageId": {"type": "string", "required": True}}),
        ("wpp_xw_switch_room", "切换小微会话房间.",
         {"sessionId": {"type": "string", "required": True}, "roomId": {"type": "string", "required": True}}),
        ("wpp_xw_events", "订阅小微会话 SSE 事件流.",
         {"sessionId": {"type": "string", "required": True}, "afterSequence": {"type": "integer"}}),
        ("wpp_xw_history_list", "读取小微记忆列表.",
         {"scrollType": {"type": "integer"}}),
        ("wpp_xw_history_fill", "补录问答卡片到小微记忆.",
         {"items": {"type": "array", "required": True}, "operationType": {"type": "integer"}}),
        ("wpp_xw_history_delete", "删除小微记忆.",
         {"deleteItemLists": {"type": "array", "required": True}}),
        ("wpp_xw_invite", "邀请小微协作者.",
         {"sessionId": {"type": "string", "required": True}, "userId": {"type": "string", "required": True}}),
        ("wpp_xw_invite_candidates", "查询可邀请的小微协作者候选.",
         {"sessionId": {"type": "string", "required": True}}),
        ("wpp_xw_invite_info", "查询小微会话邀请信息.",
         {"sessionId": {"type": "string", "required": True}, "userId": {"type": "string", "required": True}}),
        ("wpp_xw_reddots_query", "查询小微未读消息红点.",
         {"sessionId": {"type": "string", "required": True}}),
        ("wpp_xw_reddots_read", "标记小微未读消息为已读.",
         {"sessionId": {"type": "string", "required": True}, "reddots": {"type": "array"}}),
        ("wpp_xw_card_users", "查询小微卡片关联用户.",
         {"cardId": {"type": "string", "required": True}}),
        ("wpp_xw_card_screenshot_check", "检查小微卡片截图合规性.",
         {"cardId": {"type": "string", "required": True}, "image": {"type": "string"}}),
        ("wpp_xw_permission", "查询小微权限信息.",
         {"sessionId": {"type": "string", "required": True}}),
        ("wpp_xw_suggestions", "获取小微推荐问题列表.",
         {"sessionId": {"type": "string", "required": True}, "limit": {"type": "integer"}}),
    ]

    count = 0
    for name, desc, params in tools_def:
        async def handler(_name=name, _params=params, **kwargs):
            log.info("[WPP] xiaowei tool %s invoked (placeholder)", _name)
            return _placeholder(**kwargs)

        schema = {
            "type": "object",
            "properties": {k: {"type": v.get("type", "string")} for k, v in _params.items()},
            "required": [k for k, v in _params.items() if v.get("required")],
        }
        try:
            ctx.register_tool(
                name=name,
                description=desc,
                parameters=schema,
                handler=handler,
            )
            count += 1
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] xiaowei tool %s 注册失败: %s", name, e)

    return count