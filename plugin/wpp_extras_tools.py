"""WPP 缺失 skill direct tool 补全: wpp-history + wpp-identity。

老板拍板 (2026-08-31): 所有 openclaw 迁移 skill 必须注册成 hermes direct tool,
model 直接调, 禁止走 terminal 跑 CLI / SQL。

wpp-history  → wpp_history_search: 查微信消息历史 (wpp_messages 表)
wpp-identity → wpp_identity_lookup: 昵称↔wxid 匹配 (wpp_contacts / wpp_chatroom_members)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_PHONEERP_LOOKUP = "/root/.hermes/skills/phoneerp/scripts/lookup_employee.js"


# ------------------------------------------------------------------ 历史消息
def _query_history(account_id: str, peer_id: str, keyword: str, limit: int) -> list[dict]:
    """从 MariaDB wpp_messages 表查历史消息 (直接走 db.py, 不跑 SQL CLI)."""
    from . import db as _db
    if keyword:
        rows = _db.search_messages(account_id, keyword, limit=limit)
    elif peer_id:
        rows = _db.list_messages_by_peer(account_id, peer_id, limit=limit)
    else:
        rows = _db.list_messages(account_id, limit=limit)
    return rows


def _resolve_peer_id(account_id: str, target: str) -> str:
    """把用户说的昵称/群名解析成 peer_id (wxid / @chatroom)。"""
    from . import db as _db
    if target.endswith("@chatroom"):
        # 群: 按 nickname/remark 匹配 wpp_chatrooms
        rooms = _db.list_chatrooms(account_id, limit=200)
        for r in rooms:
            if target in (r.get("chatroom_id") or ""):
                return r["chatroom_id"]
            if target in (r.get("nickname") or "") or target in (r.get("remark") or ""):
                return r["chatroom_id"]
        return target  # 原样
    # 单聊: 按 nickname/remark 匹配 wpp_contacts
    contacts = _db.search_contacts(account_id, target, limit=20)
    for c in contacts:
        if target in (c.get("wxid") or ""):
            return c["wxid"]
        if target in (c.get("nickname") or "") or target in (c.get("remark") or ""):
            return c["wxid"]
    # 找不到就按 wxid 原样
    return target


def register(ctx, adapter_getter) -> int:
    """注册 wpp-history + wpp-identity direct tool."""
    count = 0

    # ========== 1. wpp_history_search ==========
    async def history_search(
        target: str = "",
        keyword: str = "",
        limit: int = 20,
        direction: str = "",
        **kwargs,
    ) -> str:
        """查微信消息历史 (wpp_messages 表)。

        Args:
            target: 对端标识 (昵称 / wxid / 群名 / xxx@chatroom), 留空 = 最近全部
            keyword: 关键词过滤 (可选)
            limit: 返回条数上限 (默认 20)
            direction: 方向过滤 (inbound/outbound, 可选)
        Returns:
            JSON 数组: [{direction, peer_id, msg_type, content, from_wxid, ts}, ...]
        """
        try:
            account_id = "default"
            peer_id = _resolve_peer_id(account_id, target) if target else ""
            rows = _query_history(account_id, peer_id, keyword, min(int(limit), 100))
            if direction:
                rows = [r for r in rows if r.get("direction") == direction]
            # 截断超长 content
            out = []
            for r in rows:
                c = (r.get("content") or "")[:500]
                out.append({
                    "direction": r.get("direction"),
                    "peer_id": r.get("peer_id"),
                    "msg_type": r.get("msg_type"),
                    "content": c,
                    "from_wxid": r.get("from_wxid"),
                    "ts": r.get("ts"),
                })
            return json.dumps({"ok": True, "count": len(out), "messages": out}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] wpp_history_search 失败: %s", e)
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    ctx.register_tool(
        name="wpp_history_search",
        toolset="wechatpadpro",
        description="查微信消息历史 (入站/出站消息)。调用: wpp_history_search(target='张三', keyword='红包', limit=20)。target 可以是昵称/wxid/群名。返回消息列表 JSON。",
        schema={
            "type": "object",
            "description": "查微信消息历史 (入站/出站消息)。调用: wpp_history_search(target='张三', keyword='红包', limit=20)。target 可以是昵称/wxid/群名。返回消息列表 JSON。",
            "properties": {
                "target": {"type": "string", "description": "对端标识: 昵称 / wxid / 群名 / xxx@chatroom, 留空=最近全部"},
                "keyword": {"type": "string", "description": "关键词过滤 (可选)"},
                "limit": {"type": "integer", "description": "返回条数上限, 默认 20, 最大 100"},
                "direction": {"type": "string", "description": "方向过滤: inbound / outbound (可选)"},
            },
        },
        handler=history_search,
        is_async=True,
    )
    count += 1

    # ========== 2. wpp_identity_lookup ==========
    async def identity_lookup(
        name: str = "",
        wxid: str = "",
    ) -> str:
        """匹配昵称↔wxid (多源: 通讯录表 / 群成员表 / phoneerp 员工表)。

        Args:
            name: 昵称 / 姓名 (可选, 和 wxid 至少给一个)
            wxid: wxid (可选)
        Returns:
            JSON: [{source, wxid, nickname, remark, title, store}, ...]
        """
        try:
            from . import db as _db
            results = []
            account_id = "default"
            if name:
                # 1) 本地通讯录表
                contacts = _db.search_contacts(account_id, name, limit=10)
                for c in contacts:
                    results.append({"source": "wpp_contacts", "wxid": c.get("wxid"), "nickname": c.get("nickname"), "remark": c.get("remark")})
                # 2) phoneerp 员工表
                try:
                    r = subprocess.run(
                        ["node", _PHONEERP_LOOKUP, "--name", name, "--json"],
                        capture_output=True, text=True, timeout=10,
                    )
                    if r.returncode == 0:
                        data = json.loads(r.stdout)
                        m = data.get("match") if isinstance(data, dict) else None
                        if m and m.get("record"):
                            rec = m["record"]
                            results.append({"source": "phoneerp", "wxid": rec.get("wppId"), "nickname": rec.get("wechatNickName"), "name": rec.get("name"), "title": rec.get("title"), "store": rec.get("store")})
                except Exception:  # noqa: BLE001
                    pass
            if wxid:
                # 通讯录 + 群成员
                contacts = _db.search_contacts(account_id, wxid, limit=5)
                for c in contacts:
                    if c.get("wxid") == wxid or wxid in (c.get("wxid") or ""):
                        results.append({"source": "wpp_contacts", "wxid": c.get("wxid"), "nickname": c.get("nickname"), "remark": c.get("remark")})
                try:
                    r = subprocess.run(
                        ["node", _PHONEERP_LOOKUP, "--wppId", wxid, "--json"],
                        capture_output=True, text=True, timeout=10,
                    )
                    if r.returncode == 0:
                        data = json.loads(r.stdout)
                        m = data.get("match") if isinstance(data, dict) else None
                        if m and m.get("record"):
                            rec = m["record"]
                            results.append({"source": "phoneerp", "wxid": rec.get("wppId"), "nickname": rec.get("wechatNickName"), "name": rec.get("name"), "title": rec.get("title"), "store": rec.get("store")})
                except Exception:  # noqa: BLE001
                    pass
            return json.dumps({"ok": True, "matches": results}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] wpp_identity_lookup 失败: %s", e)
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    ctx.register_tool(
        name="wpp_identity_lookup",
        toolset="wechatpadpro",
        description="匹配微信昵称↔wxid (多源: 通讯录 / 群成员 / phoneerp 员工表)。调用: wpp_identity_lookup(name='张三') 或 wpp_identity_lookup(wxid='wxid_xxx')。用于识别 '这个wxid是谁' / '张三的wxid是什么'。",
        schema={
            "type": "object",
            "description": "匹配微信昵称↔wxid (多源: 通讯录 / 群成员 / phoneerp 员工表)。调用: wpp_identity_lookup(name='张三') 或 wpp_identity_lookup(wxid='wxid_xxx')。用于识别 '这个wxid是谁' / '张三的wxid是什么'。",
            "properties": {
                "name": {"type": "string", "description": "昵称 / 姓名 (和 wxid 至少给一个)"},
                "wxid": {"type": "string", "description": "wxid (和 name 至少给一个)"},
            },
        },
        handler=identity_lookup,
        is_async=True,
    )
    count += 1

    # ========== 3. wpp_tools_list (2026-09-01 老板拍板 — 防 '找接口痛苦') ==========
    async def tools_list(domain: str = "", keyword: str = "") -> str:
        """列出所有 wpp vendor 工具 / 按域过滤 / 关键词搜索 (本地查 SSOT JSON, 不调 vendor)。

        Args:
            domain: 域过滤 (group/user/friend/.../extra_group/extra_msg), 留空=全部
            keyword: 工具名/描述关键词 (大小写不敏感), 留空=全部
        Returns:
            JSON: {ok, count, tools: [{name, description, endpoint, method, params, domain}], source}
        """
        try:
            map_path = Path(__file__).resolve().parent.parent / "skills" / "wpp_tools_map.json"
            if not map_path.exists():
                # 退到部署版 (deploy)
                map_path = Path("/root/.hermes/profiles/wpp-wechat/skills/wpp_tools_map.json")
            with open(map_path) as f:
                data = json.load(f)
            tools = data.get("tools", [])

            domain_lc = domain.lower().strip()
            kw_lc = keyword.lower().strip()

            if domain_lc:
                # 域模糊匹配 (group 域也匹配 extra_group)
                matched = []
                for t in tools:
                    # tools_data.py 没按域分, 用 endpoint 前缀判断
                    ep = t.get("endpoint", "")
                    if ep.startswith("/" + domain_lc.title().replace("_", "/")) or ("/" + domain_lc.split("_")[0].title() + "/") in ep:
                        matched.append(t)
                tools = matched
            if kw_lc:
                tools = [t for t in tools
                         if kw_lc in t.get("name", "").lower()
                         or kw_lc in t.get("description", "").lower()
                         or kw_lc in t.get("endpoint", "").lower()]

            return json.dumps({
                "ok": True,
                "count": len(tools),
                "tools": tools[:50],  # 上限 50 防过大
                "source": str(map_path),
                "hint": "返回可能截断 (50 条), 用 domain/keyword 缩小范围"
            }, ensure_ascii=False, default=str)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    ctx.register_tool(
        name="wpp_tools_list",
        toolset="wechatpadpro",
        description="列出所有 wpp vendor 工具 (本地查 SSOT JSON, 不调 vendor)。调用: wpp_tools_list(domain='group') 查 group 域工具; wpp_tools_list(keyword='chatroom') 搜名称含 chatroom 的工具。返回工具清单 JSON 含 endpoint/method/params。老板 2026-09-01 拍板 — 解决 '找接口痛苦' 问题 (SSOT: skills/wpp_tools_map.json)。",
        schema={
            "type": "object",
            "description": "列出所有 wpp vendor 工具 (本地查 SSOT JSON, 不调 vendor)。调用: wpp_tools_list(domain='group') 查 group 域工具; wpp_tools_list(keyword='chatroom') 搜名称含 chatroom 的工具。",
            "properties": {
                "domain": {"type": "string", "description": "域过滤 (group/user/friend/msg/.../extra_group), 留空=全部"},
                "keyword": {"type": "string", "description": "工具名/描述/端点关键词 (大小写不敏感), 留空=全部"},
            },
        },
        handler=tools_list,
        is_async=True,
    )
    count += 1

    return count