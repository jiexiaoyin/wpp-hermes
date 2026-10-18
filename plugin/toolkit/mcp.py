"""MCP 只读查询工具 —— 7 个，对齐 TS ``mcp-meta.ts``。

## 与 TS 的关系

TS 走 vendor MCP（本机实际监听 ``127.0.0.1:28062/mcp``；TS 注释里的 8062 是
vendor 容器内端口，宿主机上不存在）。py2 早先的 6 个工具直接
查本地 MariaDB 缓存表，**没有** ``account_status``、也没有 vendor 的
``wechat_search`` 语义。2026-10-18 起改为：

1. **先调 vendor MCP**（:mod:`plugin.core.vendor_mcp`），成功就用它的结果；
2. vendor 不可用（Hermes 不在厂商同机 / 未开 MCP）⇒ 6 个查询工具**回退**到本地
   缓存表，行为与旧实现一致；``wpp_mcp_account_status`` 没有本地等价物，直接报
   「vendor MCP 不可用」；
3. 全部 7 个工具按账号 ``mcpEnabled`` 门控（TS 语义：只有显式 ``false`` 才关；
   缺省视为开启）。读不到账号配置时 **fail-closed**。

## 导入约定（硬性）

**包内相对导入**；handler 里的 DB / 网络依赖一律在**函数体内**导入 —— Hermes 的
``plugin_validate`` 会在隔离 probe 子进程里 import 整个插件，模块级重依赖会直接
炸掉验证。本文件模块级只 import stdlib + 同包模块。
"""

from __future__ import annotations

import asyncio
import json
import logging

from . import policy
from .registry import get_client, mk_handler, resolve_account_id
from .toolout import dump_capped

log = logging.getLogger(__name__)

#: vendor MCP 单次调用总超时（初始化 + 通知 + tools/call 三段）。
_VENDOR_TIMEOUT_S = 8.0


def _gate(adapter, account_id: str, tool_name: str) -> str:
    """返回拒绝话术；空串 = 放行（TS ``mcpEnabled !== false`` 同款）。"""
    acct_cfg = policy.raw_account_cfg(adapter, account_id)
    if acct_cfg is None:
        return (f"MCP 未启用 (账号 {account_id} 配置读不出来), "
                f"无法调用 {tool_name}")
    if acct_cfg.get("mcpEnabled") is False:
        return (f"MCP 未启用 (账号 {account_id} mcpEnabled=false), "
                f"无法调用 {tool_name}")
    return ""


def register_mcp_tools(ctx, adapter_getter) -> None:
    """注册 MCP 只读工具（vendor 优先，本地缓存兜底）。"""
    from ..core import vendor_mcp

    async def _account(params):
        adapter = adapter_getter()
        account_id = params.pop("account_id", "default")
        if adapter is None:
            return None, account_id, None, "adapter 未就绪"
        # ``default`` 是 schema 哨兵：单账号部署（如 boss2）要落到唯一账号，
        # 否则这里永远返回"未连接"而工具面全哑。
        account_id = resolve_account_id(adapter, account_id)
        client = get_client(adapter, account_id)
        if client is None:
            return adapter, account_id, None, f"账号 {account_id} 未连接"
        return adapter, account_id, client, ""

    async def _vendor(name, arguments, client, fallback):
        """vendor MCP 优先；返回 ``(文本, 是否 vendor)``。"""
        try:
            result = await asyncio.wait_for(
                vendor_mcp.call_tool(name, arguments, client.authcode),
                timeout=_VENDOR_TIMEOUT_S,
            )
        except (TimeoutError, Exception):  # noqa: BLE001
            result = None
        if result is None:
            # ``fallback()`` 是同步 DB 查询（pymysql）—— 必须在工作线程里跑，
            # 否则本地库慢/重连时会卡住整个事件循环（旧实现直接在 async 里调用）。
            return await asyncio.to_thread(fallback), False
        text = vendor_mcp.result_text(result)
        return (text or f"MCP {name} 返回空"), True

    # ------------------------------------------------------------------ 工具

    def _local_contacts(account_id, limit, offset):
        from ..data import repo_messages as db
        return dump_capped(db.list_contacts(account_id, limit, offset))

    async def mcp_account_status(**params):
        adapter, account_id, client, error = await _account(params)
        if error:
            return error
        gate = _gate(adapter, account_id, "wpp_mcp_account_status")
        if gate:
            return gate
        try:
            result = await asyncio.wait_for(
                vendor_mcp.call_tool("wechat_account_status", {}, client.authcode),
                timeout=_VENDOR_TIMEOUT_S)
        except (TimeoutError, Exception):  # noqa: BLE001
            result = None
        if result is None:
            return "MCP wechat_account_status 调用失败 (vendor MCP 不可用)"
        return vendor_mcp.result_text(result) or "MCP wechat_account_status 返回空"

    async def mcp_list_contacts(**params):
        adapter, account_id, client, error = await _account(params)
        if error:
            return error
        gate = _gate(adapter, account_id, "wpp_mcp_list_contacts")
        if gate:
            return gate
        limit = params.get("limit") or 20
        offset = params.get("offset") or 0
        text, _ = await _vendor(
            "wechat_list_contacts", {"offset": offset, "limit": limit}, client,
            lambda: _local_contacts(account_id, limit, offset))
        return text

    async def mcp_list_groups(**params):
        adapter, account_id, client, error = await _account(params)
        if error:
            return error
        gate = _gate(adapter, account_id, "wpp_mcp_list_groups")
        if gate:
            return gate
        limit = params.get("limit") or 20
        offset = params.get("offset") or 0

        def local():
            from ..data import repo_messages as db
            # 分页参数必须一起传 —— 只传 limit 会让本地兜底永远停在第一页。
            return dump_capped(db.list_chatrooms(account_id, limit, offset))

        text, _ = await _vendor(
            "wechat_list_groups", {"offset": offset, "limit": limit}, client, local)
        return text

    async def mcp_recent_messages(**params):
        adapter, account_id, client, error = await _account(params)
        if error:
            return error
        gate = _gate(adapter, account_id, "wpp_mcp_recent_messages")
        if gate:
            return gate
        limit = params.get("limit") or 50

        def local():
            from ..data import repo_messages as db
            return dump_capped(db.list_messages(account_id, limit))

        text, _ = await _vendor(
            "wechat_get_recent_messages", {"limit": limit}, client, local)
        return text

    async def mcp_get_contact(**params):
        adapter, account_id, client, error = await _account(params)
        if error:
            return error
        gate = _gate(adapter, account_id, "wpp_mcp_get_contact")
        if gate:
            return gate
        cid = params.get("id") or params.get("wxid") or ""

        def local():
            from ..data import repo_messages as db
            row = db.get_contact(account_id, cid)
            if not row:
                return json.dumps({"found": False, "note": "联系人不存在于本地缓存"},
                                  ensure_ascii=False)
            return json.dumps(row, ensure_ascii=False, default=str)

        text, _ = await _vendor("wechat_get_contact", {"id": cid}, client, local)
        return text

    async def mcp_get_group(**params):
        adapter, account_id, client, error = await _account(params)
        if error:
            return error
        gate = _gate(adapter, account_id, "wpp_mcp_get_group")
        if gate:
            return gate
        gid = params.get("id") or ""

        def local():
            from ..data import repo_messages as db
            row = db.get_chatroom(account_id, gid)
            if not row:
                return json.dumps({"found": False, "note": "群不存在于本地缓存"},
                                  ensure_ascii=False)
            return json.dumps(row, ensure_ascii=False, default=str)

        text, _ = await _vendor("wechat_get_group", {"id": gid}, client, local)
        return text

    async def mcp_search(**params):
        adapter, account_id, client, error = await _account(params)
        if error:
            return error
        gate = _gate(adapter, account_id, "wpp_mcp_search")
        if gate:
            return gate
        query = params.get("query") or params.get("keyword") or ""
        if not query:
            return "请提供搜索关键词"
        category = params.get("category") or "all"
        offset = params.get("offset") or 0
        limit = params.get("limit") or 10

        def local():
            from ..data import repo_messages as db
            # 本地缓存只有联系人，**没有**公众号/文章/小程序…；category 不是
            # all/contact 时硬返回联系人是撒谎，不如明说这一路搜不了。
            if category not in ("all", "contact"):
                return (f"vendor MCP 不可用: 本地缓存无法搜索 category={category}"
                        f"（只有联系人）；请恢复 vendor MCP 或改用 category=all")
            return dump_capped(db.search_contacts(account_id, query))

        text, _ = await _vendor(
            "wechat_search",
            {"query": query, "category": category, "offset": offset, "limit": limit},
            client, local)
        return text

    tools = [
        ("wpp_mcp_account_status",
         "查询当前微信账号状态（在线/设备模式/昵称）. 无需参数.", {
             "type": "object",
             "properties": {"account_id": {"type": "string"}},
         }, mcp_account_status),
        ("wpp_mcp_list_contacts", "分页列出微信联系人. 用 next_offset 翻页.", {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "每页 1-100, 默认 20"},
                "offset": {"type": "integer", "description": "起始偏移, 用上次返回的 next_offset"},
                "account_id": {"type": "string"},
            },
        }, mcp_list_contacts),
        ("wpp_mcp_list_groups", "分页列出微信群. 用 next_offset 翻页.", {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "每页 1-100, 默认 20"},
                "offset": {"type": "integer", "description": "起始偏移, 用上次返回的 next_offset"},
                "account_id": {"type": "string"},
            },
        }, mcp_list_groups),
        ("wpp_mcp_recent_messages", "查询最近 24 小时收到的消息（最多 500 条）.", {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "条数 1-500, 默认 50"},
                "account_id": {"type": "string"},
            },
        }, mcp_recent_messages),
        ("wpp_mcp_get_contact", "查询单个微信联系人详情（需 list/search 返回的 id）.", {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "联系人 id（list/search 返回）或 wxid"},
                "account_id": {"type": "string"},
            },
            "required": ["id"],
        }, mcp_get_contact),
        ("wpp_mcp_get_group", "查询单个微信群详情（id, 群名, 群主, 成员数, 公告）.", {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "群 id (@chatroom)"},
                "account_id": {"type": "string"},
            },
            "required": ["id"],
        }, mcp_get_group),
        ("wpp_mcp_search",
         "搜索微信（公众号/文章/小程序/视频号/朋友圈等）. category 选类别, query 关键词.", {
             "type": "object",
             "properties": {
                 "query": {"type": "string", "description": "搜索词（2-100 字符）"},
                 "category": {"type": "string",
                              "description": "类别: all/article/official_account/channels/"
                                             "mini_program/moments/ai/live/image/read/listen/"
                                             "emoji/baike/news/wechat_index/mini_game/sticker/underline"},
                 "offset": {"type": "integer", "description": "翻页偏移"},
                 "limit": {"type": "integer", "description": "每页 1-100, 默认 10"},
                 "account_id": {"type": "string"},
             },
             # TS ``mcpSearch`` 的 category 是**必填**（不是 Optional）：不给类别
             # vendor 的 wechat_search 语义不完整。
             "required": ["query", "category"],
         }, mcp_search),
    ]

    for name, desc, schema, fn in tools:
        ctx.register_tool(
            name=name, toolset="wechatpadpro", schema={**schema, "description": desc},
            handler=mk_handler(fn), is_async=True, description=desc, emoji="🔍",
        )
    log.info("[WPP] 已注册 %d 个 MCP 工具", len(tools))
