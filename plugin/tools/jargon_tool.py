"""jargon 黑话查询工具（``wpp_jargon_query``）—— 从旧 ``plugin/tools.py`` 逐字搬来。

本文件由 ``tools/port_tools_domain.py`` 生成。差异只有一条（登记在案）：

    函数体内 ``from .jargon import ...`` → ``from ..inbound.jargon import ...``

因为 ``plugin/jargon.py`` 已迁到 ``plugin/inbound/jargon.py``（该模块零内部依赖，
是纯 stdlib，所以搬动是安全的 —— 见 ``plugin/inbound/__init__.py``）。
"""

from __future__ import annotations

import json
import logging

from . import registry

log = logging.getLogger(__name__)

_mk_handler = registry.mk_handler


def register_jargon_tools(ctx) -> None:
    """注册 jargon 黑话查询工具。"""
    async def query_jargon(**params):
        from ..inbound.jargon import query_jargon as qj, list_hot_jargon
        group_id = params.get("groupId") or ""
        term = params.get("term") or ""
        if term:
            r = qj(group_id, term)
            return json.dumps(r or {"found": False, "note": "未找到该词的黑话信息"}, ensure_ascii=False)
        hot = list_hot_jargon(group_id)
        return json.dumps(hot, ensure_ascii=False)

    _jargon_desc = "查询群黑话含义（自主学习统计的词频/上下文）。"
    ctx.register_tool(
        name="wpp_jargon_query",
        toolset="wechatpadpro",
        schema={
            "type": "object",
            "description": _jargon_desc,
            "properties": {
                "groupId": {"type": "string", "description": "群 @chatroom 标识"},
                "term": {"type": "string", "description": "要查询的词（不填则返回群热词TOP）"},
            },
            "required": ["groupId"],
        },
        handler=_mk_handler(query_jargon),
        is_async=True,
        description=_jargon_desc,
        emoji="🗣️",
    )
    log.info("[WPP] 已注册 jargon 工具")
