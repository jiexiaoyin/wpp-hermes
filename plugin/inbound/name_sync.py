"""把厂商的群列表同步进 ``wpp_chatrooms``，供 WebUI 会话标题/群名使用。

为什么需要单独同步：v1 入站帧只带 ``xxx@chatroom``，群名在厂商侧；本地
``wpp_chatrooms.nickname`` 是 8 月导入时的一份快照，群改名后不会自己更新。
适配器连上后跑一轮 ``/Group/List``，之后每 :data:`SYNC_INTERVAL_SEC` 再跑，
进程内 5 分钟缓存（``session_title``）自然过期，标题和 ``chat_name`` 都不用
每条消息去问厂商。

只写**群**。好友昵称走 ``wpp_contacts``（导入时已有），厂商的
``/Friend/GetContractList`` 只回 wxid 列表、不带昵称，逐个
``/Friend/GetContractDetail`` 太贵，不值得在连接期做。

同步是纯读 + 本地 upsert：失败只记 warning，绝不影响收发消息。
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

SYNC_INTERVAL_SEC = 6 * 3600.0

#: 群**成员昵称**同步：**事件驱动**，不做定时轮询（老板 2026-10-20 拍板）。
#: 触发点只有两个：① 机器人被拉进群（``group_invite``）拉该群全量；
#: ② 群里有进群/退群系统消息（``join_events``）拉该群一次。
#: 定时轮询是错的 —— 10 个群每轮 10 次外呼换来的只是"绝大多数轮次没有变化"。


def _group_rows(payload: dict) -> list[dict]:
    """兼容 ``GroupList``（id 列表）/ ``group_details`` / ``items`` 三种形状。"""
    data = payload.get("Data") or payload.get("data") or {}
    if not isinstance(data, dict):
        return []
    for key in ("group_details", "items", "GroupList", "groups"):
        rows = data.get(key)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return [r for r in rows if isinstance(r, dict)]
    return []


def apply_group_rows(account_id: str, rows: list[dict]) -> int:
    """同步 upsert（阻塞 DB ⇒ 由调用方 ``asyncio.to_thread``）。返回写入行数。"""
    from ..data import repo_messages

    written = 0
    for row in rows:
        chatroom_id = str(row.get("id") or row.get("chatroom_id") or "").strip()
        if not chatroom_id:
            continue
        name = str(row.get("name") or row.get("nickname") or "").strip()
        display = str(row.get("display_name") or "").strip()
        # ``name`` 是真实群名；``display_name`` 有时是"群名(人数)"，仅作兜底。
        if not name and display:
            name = display
        try:
            repo_messages.upsert_chatroom(
                account_id, chatroom_id, nickname=name,
                owner_wxid=str(row.get("owner_id") or "") or None,
                member_count=row.get("member_count"),
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 —— 单群失败不拖累整批
            log.debug("[WPP:%s] 群名入库失败 %s: %s", account_id, chatroom_id, exc)
    return written


async def sync_once(client, account_id: str) -> int:
    payload = await client.call("/Group/List", method="GET", audit=False)
    code = payload.get("Code")
    if code is not None and code != 0:
        raise RuntimeError(f"/Group/List Code={code}")
    rows = _group_rows(payload)
    if not rows:
        return 0
    written = await asyncio.to_thread(apply_group_rows, account_id, rows)
    if written:
        # 同步和标题解析在同一进程：让刚写入的群名立刻生效，不用等 5 分钟 TTL。
        from . import session_title
        session_title.clear_name_cache()
        log.info("[WPP:%s] 群名同步完成：%d 个群", account_id, written)
    return written


def apply_member_rows(account_id: str, chatroom_id: str,
                      members: list[tuple[str, str]]) -> int:
    """把 ``(wxid, 群昵称)`` 写进 ``wpp_chatroom_members``（阻塞 DB ⇒ 调用方 to_thread）。"""
    from ..data import repo_messages

    written = 0
    for wxid, nick in members:
        try:
            repo_messages.upsert_chatroom_member(
                account_id, chatroom_id, wxid, nickname=nick or "")
            written += 1
        except Exception as exc:  # noqa: BLE001 —— 单个成员失败不拖累整批
            log.debug("[WPP:%s] 成员昵称入库失败 %s/%s: %s",
                      account_id, chatroom_id, wxid, exc)
    return written


async def sync_members(client, account_id: str,
                       chatroom_ids: list[str] | None = None) -> int:
    """拉群成员昵称并 upsert 进 ``wpp_chatroom_members``。返回写入行数。

    ``chatroom_ids`` 给了就只拉这几个群（**进群事件 / 新人加入时用**）；
    不给则拉全部群（首次补历史数据 / 机器人被拉进群时用）。

    单群失败只记 debug，不影响其它群：一个群拉不到，不该让整轮昵称刷新归零。
    """
    from . import employee_sync

    if chatroom_ids is None:
        payload = await client.call("/Group/List", method="GET", audit=False)
        code = payload.get("Code")
        if code is not None and code != 0:
            raise RuntimeError(f"/Group/List Code={code}")
        chatroom_ids = [
            str(r.get("id") or r.get("chatroom_id") or "").strip()
            for r in _group_rows(payload)
        ]
    written = 0
    for chatroom_id in chatroom_ids:
        if not chatroom_id.endswith("@chatroom"):
            continue
        try:
            detail = await client.call(
                employee_sync.MEMBER_ENDPOINT, {"QID": chatroom_id}, audit=False)
            dcode = detail.get("Code")
            if dcode is not None and dcode != 0:
                raise RuntimeError(f"Code={dcode}")
            members = employee_sync.parse_members(detail)
            if members:
                written += await asyncio.to_thread(
                    apply_member_rows, account_id, chatroom_id, members)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单群失败不拖累整轮
            log.debug("[WPP:%s] 群成员同步失败 %s: %s", account_id, chatroom_id, exc)
    if written:
        # 昵称刚刷新，让候选名字立刻生效（否则 5 分钟 TTL 内仍显示旧值）。
        from . import session_title
        session_title.clear_name_cache()
        log.info("[WPP:%s] 群成员昵称同步完成：%d 条（%d 个群）",
                 account_id, written, len(chatroom_ids))
    return written


async def sync_forever(client, account_id: str,
                       interval_sec: float = SYNC_INTERVAL_SEC) -> None:
    """连接期常驻任务：先同步一轮群名，之后定期刷新；``CancelledError`` 原样抛出。

    成员昵称**不在这里**跑 —— 它是事件驱动的（见模块顶部注释）。
    """
    while True:
        try:
            await sync_once(client, account_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 同步失败不致命
            log.warning("[WPP:%s] 群名同步失败（不影响收发）: %s", account_id, exc)
        await asyncio.sleep(interval_sec)
