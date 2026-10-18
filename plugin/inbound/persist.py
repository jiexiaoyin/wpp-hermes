"""入站落库 —— 对齐 TS ``src/inbound/enrich.ts`` 的 ``enrichAndSaveMessage``。

## 为什么需要它

本树此前**只记出站**（``platform/adapter.py::_persist_outbound``），入站一条不落。
后果不是"少一张审计表"，而是**下游三个功能全都建在沙子上**：

* 心流 judge 的"最近 N 条上下文"只能看到 bot 自己说的话（``_hf_note_bot_reply`` 那半），
  等于给裁判看了一份伪造的现场记录；
* ``repo_messages.list_group_messages`` / ``list_messages`` 查出来是**空表**；
* 黑话挖掘（``inbound/jargon.py`` 的 ``update_from_message``）、按人查群历史
  （``from_wxid`` 那两列）都没有输入。

## 与 TS 的对应关系（三条都逐字对齐）

1. **永不抛**：TS ``enrichAndSaveMessage`` 整体一个 try/catch，失败返 ``{saved:false}``
   （``enrich.ts:58-63``）。落库失败**不该**影响收信链路。
2. **一次调用一次写入**：TS 成功路径只有一次 ``saveMessage({...})``；
   幂等靠唯一键 ``uk_account_msg(account_id, msg_id, new_msg_id)``。
3. **字段映射**（``enrich.ts:41-54`` → :func:`to_db_record`）：13 列一一对。
   ``create_time`` 不在本映射里：它由 ``repo_messages.save_message`` 统一从 ``ts``
   派生（本地墙钟 ``YYYYMMDDHHMMSS``，2026-10-18 起；见
   ``repo_messages.legacy_create_time``），保证入站/出站走同一条规则。

## 调用方负责"该不该落"——本模块不判

TS 侧 skip 全在调用方：``handler.ts:539``
``persistBatch = batch.filter(m => persistResults.get(m)?.via !== "blocked")``。
本树对应的位置是 ``adapter._handle_one`` 的 ④ ``should_drop`` 之后 ——
那一处的语义**恰好等价**：``should_drop`` 为假 ⟺ 触发了（``ok=True``）或
**白名单内但未硬触发**（``reason='heartflow_candidate'``），而红包/撤回/公众号/黑名单群/
非白名单会话/自消息全在 ``DENY_REASONS`` 里 ⇒ 与 TS 的"白名单内才入库（隐私）"是同一批。
⚠️ 有意比 TS **略严**：红包/撤回/公众号也不落库（TS 那三个 reason 属不属于 ``blocked``
未在蓝本里明确；不落是 fail-closed 的一侧，且它们不是对话内容）。

## 两个真 bug 的修法在这里（W1 顺带）

* ``WppMessage.peer_kind`` 对私聊返回 ``"dm"``，而 ``wpp_messages.peer_kind`` 是
  **ENUM('direct','group','room')** ⇒ 私聊入库**必然报 1265**，被"永不抛"吞成一条 warn。
  修法是**在 DB 边界**映射（:func:`db_peer_kind`）—— **不改属性本身**：
  ``adapter._build_event`` 的 ``chat_type="dm"`` 是 Hermes 侧口径，动它会改事件语义。
* ``_persist_outbound`` 写 ``chat_id = f"{account_id}:{peer_id}"``，而
  ``repo_messages.list_group_messages`` 的查询是 ``chat_id = <裸群 id> OR (chat_id IS NULL
  AND peer_id = <裸群 id>)`` ⇒ 出站群消息**两个分支都不匹配**、对"按群查历史"隐形。
  修法是出站也写**裸 peer_id**（:func:`db_chat_id`），与入站共用同一个函数。

INVARIANT: W1 —— 落库失败只 warn，绝不打断收信；入站/出站的 peer_kind 与 chat_id
必须走本模块的同一对函数（两处各写一份就是漂移的起点）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

#: vendor 侧"服务端消息 id"的几种拼法（大小写/下划线各版都有）。
#: 取不到就写 NULL —— 宁可少一列，也不要把客户端 id 冒充服务端 id
#: （群友引用 bot 消息时带的是 svrid，冒充了**永远匹配不上**）。
_NEW_MSG_ID_KEYS = ("NewMsgId", "newMsgId", "new_msg_id", "newmsgid")

#: ``wpp_messages.direction`` 是 ENUM('inbound','outbound') —— 只许这两个值。
_DIRECTION_OUTBOUND = "outbound"
_DIRECTION_INBOUND = "inbound"

#: ``wpp_messages.peer_kind`` 是 ENUM('direct','group','room')。
_KIND_GROUP = "group"
_KIND_DIRECT = "direct"


def db_peer_kind(peer_kind: str) -> str:
    """把消息层的 ``peer_kind`` 映射到 **DB ENUM 能接受的值**。

    ``WppMessage.peer_kind`` 对私聊给 ``"dm"``（本树旧口径），而 ENUM 里是 ``direct``。
    不映射 ⇒ ``ERROR 1265 Data truncated``，且**被"永不抛"吞成一条 warn**
    （写不进去但链路照跑，是最难发现的一类）。
    """
    return _KIND_GROUP if str(peer_kind or "").strip().lower() == _KIND_GROUP else _KIND_DIRECT


def db_chat_id(peer_id: str, *, is_group: bool) -> str | None:
    """``wpp_messages.chat_id`` 写什么：**群聊写裸群 id**，私聊写 NULL。

    不写 ``accountId:peerId`` 那种编码 —— ``list_group_messages`` 按裸群 id 查
    ``chat_id = %s OR (chat_id IS NULL AND peer_id = %s)``，编码串**两个分支都不命中**
    ⇒ 那条消息对"按群查历史"整个隐形（出站群消息今天就处在这个状态）。
    """
    return (peer_id or None) if is_group else None


def db_direction(raw: str) -> str:
    """消息层的 ``direction`` → ENUM 值。

    ``WppMessage.direction`` 默认 ``"incoming"``（解析器原样透传 vendor 的字段），
    而 ENUM 只认 ``inbound``/``outbound`` ⇒ **必须映射**，否则入站整批 1265。
    非 ``out*`` 一律按 ``inbound``（fail-closed 到"这是别人说的话"）。
    """
    return _DIRECTION_OUTBOUND if str(raw or "").strip().lower().startswith("out") else _DIRECTION_INBOUND


def pick_new_msg_id(raw: Any) -> str | None:
    """从原始 payload 里取服务端消息 id（``msg.raw``）。取不到返回 ``None``。"""
    if not isinstance(raw, dict):
        return None
    for key in _NEW_MSG_ID_KEYS:
        if raw.get(key) not in (None, ""):
            # 复用解析器的解包（business callback 会把每个字段包成 ``{"string": …}``）；
            # 不解包的话写进去的是 **Python dict 字面量**，看着有值、其实永远匹配不上。
            from .parser import _safe_str

            val = _safe_str(raw.get(key))
            return val or None
    return None


def to_db_record(acct_id: str, msg: Any, *, now: int | None = None) -> dict:
    """``WppMessage`` → ``repo_messages.save_message`` 的 kwargs（**纯函数**，可测）。

    逐列对齐 TS ``enrich.ts:41-54``：

    ==========================  ==================================================
    ``account_id``              ``msg.accountId``
    ``msg_id``                  ``msg.msgId``
    ``new_msg_id``              ``msg.newMsgId``（本树从 ``msg.raw`` 取，见
                                :func:`pick_new_msg_id`；``WppMessage`` **没有**这个字段
                                —— ``heartflow.py`` 里那句 ``getattr(msg, "new_msg_id", "")``
                                恒空，是条静默空操作，本模块不重蹈）
    ``direction``               ``msg.direction ?? "inbound"`` → :func:`db_direction`
    ``peer_kind``               ``msg.peerKind`` → :func:`db_peer_kind`
    ``peer_id``                 ``msg.peerId``
    ``peer_name``               ``msg.fromNickname``
    ``chat_id``                 ``msg.chatroomId``（裸群 id；私聊 NULL）
    ``msg_type``                ``String(msg.msgType)``
    ``content``                 ``msg.content``
    ``raw_payload``             ``msg.raw``
    ``from_wxid``               ``msg.fromWxid``
    ``ts``                      ``msg.ts``（0/None ⇒ 用当前时间；TS 侧由 DB 兜
                                ``CURRENT_TIMESTAMP``，这里显式给，便于测试与真库一致）
    ``create_time``             **不在本映射**：``save_message`` 从 ``ts`` 派生
                                （本地墙钟 ``YYYYMMDDHHMMSS``）
    ==========================  ==================================================

    ⚠️ ``msg_id`` 可能是解析器**合成**的（无 id 的 vendor 消息，见 ``parser.py:302-308``）
    ⇒ 这类行**去重不了**（每次合成都不同）。这是本树既有的"宁可重放、不可静默吞"策略，
    落库沿用同一条：看得见的重复行胜过看不见的丢消息。
    """
    is_group = bool(getattr(msg, "is_group", False))
    peer_id = str(getattr(msg, "peer_id", "") or "")
    ts = int(getattr(msg, "timestamp", 0) or 0) or int(now if now is not None else time.time())
    return {
        "account_id": acct_id,
        "msg_id": str(getattr(msg, "msg_id", "") or "") or None,
        "new_msg_id": pick_new_msg_id(getattr(msg, "raw", None)),
        "direction": db_direction(getattr(msg, "direction", "")),
        "peer_kind": db_peer_kind(getattr(msg, "peer_kind", "")),
        "peer_id": peer_id,
        "peer_name": str(getattr(msg, "from_nickname", "") or "") or None,
        "chat_id": db_chat_id(str(getattr(msg, "chatroom_id", "") or ""), is_group=is_group),
        # 对齐 TS ``String(msgType)``：列是 VARCHAR(32)，写 int 由 pymysql 转成 "1"/"49"
        "msg_type": str(getattr(msg, "msg_type", "") or "") or None,
        "content": getattr(msg, "content", None),
        "raw_payload": getattr(msg, "raw", None),
        "from_wxid": str(getattr(msg, "from_wxid", "") or "") or None,
        "ts": ts,
    }


def persist_inbound(acct: Any, msg: Any, conn: Any = None) -> bool:
    """落一条入站消息。**永不抛**（对齐 TS ``enrichAndSaveMessage``）。

    返回 ``True`` = 已入库/已幂等更新；``False`` = 失败（已 warn）。

    ⚠️ 失败只 ``log.warning`` —— 收信链路不能因为"审计写不进去"而断。
    """
    try:
        from ..data import repo_messages

        record = to_db_record(str(getattr(acct, "id", "") or ""), msg)
        repo_messages.save_message(**record, conn=conn)
        return True
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：不许抛
        log.warning("[WPP:%s] 入站落库失败（不影响收信）msg=%s: %s",
                    getattr(acct, "id", "?"), getattr(msg, "msg_id", "?"), exc)
        return False
