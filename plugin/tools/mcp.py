"""MCP 只读查询工具 —— 从旧扁平 ``plugin/tools.py`` 第 768–874 行**整段**搬来。

即旧文件里的 ``def register_mcp_tools(ctx)`` （``def register_jargon_tools(`` 之前的那一段）。
体是**逐字保留**的，只动了三处导入相关的东西：

1. 6 处 handler 内的 ``from . import db`` → ``from ..data import repo_messages as db``
   （旧扁平模块名 ``db`` 在新树里是 ``plugin/data/db.py``，而 MCP 的后端 SQL 现在住在
   ``plugin/data/repo_messages.py``；绑定成 ``db`` 这个名字是为了**不动调用点写法**）。
2. 注册循环里的 ``_mk_handler(fn)`` → ``mk_handler(fn)``（从 :mod:`.registry` 取，
   旧名带前导下划线，新树已去掉）。
3. 两个调用点改名以对齐既有实现（**没有**为它们另写一份 SQL）：
   ``db.query_contact_by_wxid`` → ``db.get_contact``、
   ``db.query_chatroom`` → ``db.get_chatroom``。

## 这个文件注册什么

6 个只读工具，全部直查 MariaDB 的**本地缓存表**（不调厂商 API）：

============================  ==================  ================================
工具名                        查哪张表            语义
============================  ==================  ================================
``wpp_mcp_list_contacts``     ``wpp_contacts``    联系人列表（分页）
``wpp_mcp_list_groups``       ``wpp_chatrooms``   群列表
``wpp_mcp_recent_messages``   ``wpp_messages``    全账号最近消息（``ts DESC``）
``wpp_mcp_get_contact``       ``wpp_contacts``    单联系人整行（miss → ``found:false``）
``wpp_mcp_get_group``         ``wpp_chatrooms``   单群整行（miss → ``found:false``）
``wpp_mcp_search``            ``wpp_contacts``    关键词搜联系人
============================  ==================  ================================

## 导入约定（硬性）

**包内相对导入**：``plugin/platform/`` 这个包名会遮蔽 stdlib ``platform`` 模块，
任何依赖裸名导入的地方都有风险（见 ``plugin/platform/__init__.py``）。禁止
``import adapter`` / ``from tools import x``。

**延迟导入**：handler 里那条 ``from ..data import repo_messages as db`` 必须在**函数体内** ——
Hermes 的 ``plugin_validate`` 会在隔离 probe 子进程里 import 整个插件，
模块级的重依赖（pymysql / node / 网络）会直接炸掉验证。本文件的模块级 import 只有
stdlib ``json`` / ``logging`` 和同包的 :mod:`.registry`。

**工具注册用 ``toolset="wechatpadpro"`` 字面量**（不替换成常量，保持原样）。
"""

from __future__ import annotations

import json
import logging

from .registry import mk_handler

log = logging.getLogger(__name__)


def register_mcp_tools(ctx) -> None:
    """注册 MCP 只读查询工具（迁移自 wpp mcp-meta.ts，用 MariaDB 查询）。"""
    async def mcp_list_contacts(**params):
        from ..data import repo_messages as db
        account_id = params.get("account_id") or "default"
        limit = params.get("limit") or 20
        offset = params.get("offset") or 0
        rows = db.list_contacts(account_id, limit, offset)
        return json.dumps(rows, ensure_ascii=False, default=str)[:4000]

    async def mcp_list_groups(**params):
        from ..data import repo_messages as db
        account_id = params.get("account_id") or "default"
        limit = params.get("limit") or 20
        rows = db.list_chatrooms(account_id, limit)
        return json.dumps(rows, ensure_ascii=False, default=str)[:4000]

    async def mcp_recent_messages(**params):
        from ..data import repo_messages as db
        account_id = params.get("account_id") or "default"
        limit = params.get("limit") or 50
        rows = db.list_messages(account_id, limit)
        return json.dumps(rows, ensure_ascii=False, default=str)[:4000]

    async def mcp_get_contact(**params):
        from ..data import repo_messages as db
        account_id = params.get("account_id") or "default"
        cid = params.get("id") or params.get("wxid") or ""
        row = db.get_contact(account_id, cid)
        if not row:
            return json.dumps({"found": False, "note": "联系人不存在于本地缓存"}, ensure_ascii=False)
        return json.dumps(row, ensure_ascii=False, default=str)

    async def mcp_get_group(**params):
        from ..data import repo_messages as db
        account_id = params.get("account_id") or "default"
        gid = params.get("id") or ""
        row = db.get_chatroom(account_id, gid)
        if not row:
            return json.dumps({"found": False, "note": "群不存在于本地缓存"}, ensure_ascii=False)
        return json.dumps(row, ensure_ascii=False, default=str)

    async def mcp_search(**params):
        from ..data import repo_messages as db
        account_id = params.get("account_id") or "default"
        keyword = params.get("keyword") or params.get("query") or ""
        if not keyword:
            return "请提供搜索关键词"
        rows = db.search_contacts(account_id, keyword)
        return json.dumps(rows, ensure_ascii=False, default=str)[:4000]

    tools = [
        ("wpp_mcp_list_contacts", "获取联系人列表（本地缓存）", {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "每页数量 1-100 默认20"},
                "offset": {"type": "integer", "description": "起始偏移"},
                "account_id": {"type": "string"},
            },
        }, mcp_list_contacts),
        ("wpp_mcp_list_groups", "获取群列表（本地缓存）", {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "account_id": {"type": "string"},
            },
        }, mcp_list_groups),
        ("wpp_mcp_recent_messages", "获取最近消息", {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "条数 1-500 默认50"},
                "account_id": {"type": "string"},
            },
        }, mcp_recent_messages),
        ("wpp_mcp_get_contact", "获取联系人详情", {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "联系人 wxid"},
                "account_id": {"type": "string"},
            },
            "required": ["id"],
        }, mcp_get_contact),
        ("wpp_mcp_get_group", "获取群详情", {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "群 @chatroom"},
                "account_id": {"type": "string"},
            },
            "required": ["id"],
        }, mcp_get_group),
        ("wpp_mcp_search", "搜索联系人或群", {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "account_id": {"type": "string"},
            },
            "required": ["keyword"],
        }, mcp_search),
    ]

    for name, desc, schema, fn in tools:
        ctx.register_tool(
            name=name, toolset="wechatpadpro", schema={**schema, "description": desc},
            handler=mk_handler(fn), is_async=True, description=desc, emoji="🔍",
        )
    log.info("[WPP] 已注册 %d 个 MCP 工具", len(tools))
