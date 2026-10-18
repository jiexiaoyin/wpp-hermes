"""消息 / 联系人 / 群 / 群成员 / 会话状态 / 供应商调用审计 的 SQL 原语。

对齐 TS ``src/storage/db/mysql.ts`` 的读写语义。**只写 SQL，不写业务规则** ——
业务判定（去重闸、红包判据、心流决策）都在 ``inbound/`` 与 ``inbound/heartflow/``。

## 四条从 TS 搬过来的硬约束

1. **时间聚合/排序一律走 ``ts``**。``create_time`` **不是 epoch**：它是本地墙钟
   ``YYYYMMDDHHMMSS``（2026-08-11 前写入的行），2026-10-18 起新行也按同一格式写入
   （由 ``ts`` 换算，见 :func:`legacy_create_time`），历史 NULL 行已回填。即便如此，
   仍**只许**用 ``ts`` 做窗口/排序 —— 墙钟串没有时区，也不能拿来做整数时间运算。
2. **单表查询是刻意的**。TS 曾因跨表 JOIN 撞 ``Illegal mix of collations``
   （MariaDB 11 默认 ``utf8mb4_uca1400_ai_ci`` vs 本插件表的 ``utf8mb4_unicode_ci``）。
   Python 侧所有表都硬编码 ``COLLATE utf8mb4_unicode_ci``，但**仍保持单表习惯**。
3. **``COALESCE(NULLIF(chat_id,''), peer_id)`` 是必须的**。出站行只写
   ``peer_kind='group'`` + ``peer_id=目标群``，``chat_id`` 恒 NULL —— 直接
   ``GROUP BY chat_id`` 会让 bot 侧分子恒 0、占比永远 0%，**且不报任何错**。
4. **``ts`` 入库用 epoch 换算**（``FROM_UNIXTIME(?)``），别写裸字符串。
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime
from typing import Any

from . import db

logger = logging.getLogger(__name__)

#: 群聊 id 后缀（判 peer_kind 用）
GROUP_SUFFIX = "@chatroom"


# --------------------------------------------------------------------------- 消息


def legacy_create_time(ts: int | float | None) -> int:
    """``ts``（epoch 秒）→ ``create_time`` 的本地墙钟格式 ``YYYYMMDDHHMMSS``。

    ``create_time`` **不是 epoch**：2026-08-11 前的历史行是 ``YYYYMMDDHHMMSS``
    （2026-10-18 实测 ``20260811094227`` ↔ ``ts=2026-08-11 09:42:27 +08:00``），
    2026-10-18 起新行也写同一形状 —— 老板要求补上"之后全 NULL"这个数据质量问题。
    时区跟随进程本地时区（生产 = CST +08:00，与 DB session 一致）。

    取不到/越界 ⇒ 用当前时刻：一个坏时间戳绝不许挡住落库（本模块的"失败只 warn"契约）。
    """
    try:
        when = int(ts) if ts else int(time.time())
        return int(time.strftime("%Y%m%d%H%M%S", time.localtime(when)))
    except (TypeError, ValueError, OverflowError, OSError):
        return int(time.strftime("%Y%m%d%H%M%S"))


def save_message(
    account_id: str,
    *,
    direction: str,
    peer_kind: str,
    peer_id: str,
    msg_id: str | None = None,
    new_msg_id: str | None = None,
    peer_name: str | None = None,
    chat_id: str | None = None,
    msg_type: str | None = None,
    content: str | None = None,
    raw_payload: Any = None,
    from_wxid: str | None = None,
    ts: int | None = None,
    conn: db.Connection | None = None,
) -> int:
    """落一条消息。幂等键 = ``uk_account_msg(account_id, msg_id, new_msg_id)``。

    重复入库时**更新内容但不动 ts / create_time**（对齐 TS：两者都是"首次入库时刻"）。
    ``create_time`` 由 ``ts`` 派生（:func:`legacy_create_time`），与历史行的
    ``YYYYMMDDHHMMSS`` 形状一致；时间聚合仍只走 ``ts``。
    """
    c = conn or db.get_conn()
    payload = None
    if raw_payload is not None:
        payload = raw_payload if isinstance(raw_payload, str) else json.dumps(
            raw_payload, ensure_ascii=False,
        )
    # ★ NULL 会**绕过**唯一键：MySQL 的 UNIQUE 允许多行含 NULL，于是同一个
    #   (account, msg_id, NULL) 可以无限重复入库（三通道重放时去重整个失效）。
    #   取不到服务端 id 就写空串 —— 空串是"这个键上没有值"，但键本身仍参与比较。
    nid = str(new_msg_id).strip() if new_msg_id is not None else ""
    return c.execute(
        """
        INSERT INTO wpp_messages
          (account_id, msg_id, new_msg_id, direction, peer_kind, peer_id, peer_name,
           chat_id, msg_type, content, raw_payload, from_wxid, ts, create_time)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                COALESCE(FROM_UNIXTIME(%s), CURRENT_TIMESTAMP), %s)
        ON DUPLICATE KEY UPDATE
          direction = VALUES(direction),
          peer_kind = VALUES(peer_kind),
          peer_id = VALUES(peer_id),
          msg_type = VALUES(msg_type),
          content = VALUES(content),
          raw_payload = VALUES(raw_payload),
          from_wxid = VALUES(from_wxid)
        """,
        (account_id, msg_id, nid, direction, peer_kind, peer_id, peer_name,
         chat_id, msg_type, content, payload, from_wxid, ts, legacy_create_time(ts)),
    )


def get_message_by_id_or_new_id(
    account_id: str,
    msg_id: str | None = None,
    new_msg_id: str | None = None,
    conn: db.Connection | None = None,
) -> dict | None:
    """按 ``msg_id`` / ``new_msg_id`` 查**一条**消息（**不限方向**）。

    逐字对齐 TS ``mysql.ts::getMessageByMsgIdOrNewId(…, {direction:'any'})``：引用回复
    那条路（``send/quote-reply.ts``）用它回查被引用消息的 ``from_wxid`` / ``peer_name``，
    而 **v1.3.18 P1-核心2** 那次修的就是"被引用的可能是 bot 自己发的"
    （``direction='outbound'``）⇒ **不许**按 ``direction='inbound'`` 筛，筛了就是
    "群友引用 bot 的消息"这一类**永远查不到**（且不报错，只是 @ 不出来 / refermsg 空）。

    TS 的运算符陷阱照抄在案（``mysql.ts:620``）：必须写
    ``account_id = ? AND (msg_id = ? OR new_msg_id = ?)`` —— 少了那对括号，``AND``
    优先级更高会让 ``account_id`` 恒真的那半边把整条条件变成"几乎全部行"。

    两个 id 都空 ⇒ 直接 ``None``（TS：``if (!msgId && !newMsgId) return null``），
    别去发一条 ``msg_id = NULL`` 的查询 —— 那个恒不匹配，是白跑一次库。

    ⚠️ **一处有意比 TS 严**：TS 是 ``LIMIT 1`` 且**不带 ORDER BY**，同 id 多行时由存储
    引擎决定（本文件其余读也一样，见 :func:`list_messages` 的注释）；这里补
    ``ORDER BY ts DESC, id DESC`` 的确定性决胜键。id 本应唯一，这一条只是让
    "选中的是哪一行"在重放场景下可复现。

    回 12 列（含 ``raw_payload`` 与 ``content``，供
    :func:`plugin.inbound.quote_ref.resolve_quote_identity` 做发送者兜底解析）。
    """
    mid = str(msg_id or "").strip()
    nid = str(new_msg_id or "").strip()
    if not mid and not nid:
        return None
    clauses: list[str] = []
    params: list[Any] = [account_id]
    if mid:
        clauses.append("msg_id = %s")
        params.append(mid)
    if nid:
        clauses.append("new_msg_id = %s")
        params.append(nid)
    c = conn or db.get_conn()
    return c.query_one(
        "SELECT msg_id, new_msg_id, direction, peer_kind, peer_id, chat_id, peer_name, "
        "from_wxid, msg_type, content, raw_payload, UNIX_TIMESTAMP(ts) AS at_sec "
        "FROM wpp_messages WHERE account_id = %s AND ("
        + " OR ".join(clauses) + ") ORDER BY ts DESC, id DESC LIMIT 1",
        tuple(params),
    )


def list_messages_by_peer(
    account_id: str, peer_id: str, limit: int = 20, conn: db.Connection | None = None,
) -> list[dict]:
    """按会话取最近的消息（时间升序返回，便于直接拼 prompt）。"""
    c = conn or db.get_conn()
    rows = c.query(
        """
        SELECT direction, peer_kind, peer_id, chat_id, msg_id, new_msg_id, msg_type,
               content, from_wxid, peer_name, UNIX_TIMESTAMP(ts) AS at_sec
        FROM wpp_messages
        WHERE account_id = %s AND peer_id = %s
        ORDER BY ts DESC, id DESC LIMIT %s
        """,
        (account_id, peer_id, _cap(limit, 1, 500)),
    )
    rows.reverse()
    return rows


def list_messages(
    account_id: str, limit: int = 50, conn: db.Connection | None = None,
) -> list[dict]:
    """查 ``wpp_messages`` —— 该账号**跨全部会话**的最近消息（MCP ``mcpRecentMessages``）。

    旧 ``plugin/db.py`` 的 ``list_messages`` 就是这句：**单表、按 ``ts DESC``、不限 peer**
    （不是 ``list_messages_by_peer`` 那种单会话查询）。``ts`` 是对的排序键 ——
    ``create_time`` 是死列（见文件头约束 1），拿它排会把旧行排到公元 643000 年。

    回 7 列：``direction`` / ``peer_id`` / ``peer_name`` / ``msg_type`` / ``content`` /
    ``from_wxid`` / ``ts``（TIMESTAMP，pymysql 解成 ``datetime``；调用方用
    ``json.dumps(..., default=str)`` 兜底）。

    比旧实现只多一个 ``, id DESC`` 决胜键：``ts`` 是**秒**精度，同秒多行时旧 SQL 的顺序
    由存储引擎决定（不稳定）；本文件其余时间排序也一律 ``ts DESC, id DESC``。
    """
    c = conn or db.get_conn()
    return c.query(
        "SELECT direction, peer_id, peer_name, msg_type, content, from_wxid, ts "
        "FROM wpp_messages WHERE account_id = %s ORDER BY ts DESC, id DESC LIMIT %s",
        (account_id, _cap(limit, 1, 500)),
    )


def list_group_messages(
    account_id: str, group_id: str, limit: int = 30, conn: db.Connection | None = None,
    before_sec: int | None = None,
) -> list[dict]:
    """群聊最近 N 条（时间升序）。``chat_id`` 与 ``peer_id`` 都试，兼容两种落库形态。

    ``before_sec``（可选）= **只看这个时刻之前**的消息，逐字对齐 TS
    （``mysql.ts:581-583``：``ts < FROM_UNIXTIME(?)``，**严格小于**）。
    群聊上下文注入靠它**把触发消息自己排除在候选池之外** —— 不排除的话，候选人里
    永远有"用户刚 @ 你的那一句"，而意图判断要的恰恰是**它之前的背景**
    （``dispatcher.ts:343`` v1.2.8 注释：``beforeTs 排除触发消息自身``）。

    ⚠️ **严格小于 ⇒ 同一秒的其它消息也一并排除**。这不是瑕疵：``ts`` 是**秒**精度，
    同秒无法区分先后，宁可少给一条，也不能把触发消息本身喂回给意图判断。
    ⚠️ ``0``/``None`` 都表示"不过滤"（TS 那边是 ``if (opts.beforeTs)`` 的真值判断，
    同样的语义）—— 别写成 ``is not None``，那会让传 0 的调用方拿到一个空窗口。
    """
    c = conn or db.get_conn()
    sql = (
        """
        SELECT direction, peer_id, chat_id, msg_id, msg_type, content, from_wxid,
               peer_name, UNIX_TIMESTAMP(ts) AS at_sec
        FROM wpp_messages
        WHERE account_id = %s AND peer_kind = 'group'
          AND (chat_id = %s OR (chat_id IS NULL AND peer_id = %s))
        """
    )
    params: list = [account_id, group_id, group_id]
    if before_sec:
        sql += "  AND ts < FROM_UNIXTIME(%s)\n"
        params.append(int(before_sec))
    sql += "ORDER BY ts DESC, id DESC LIMIT %s"
    params.append(_cap(limit, 1, 500))
    rows = c.query(sql, tuple(params))
    rows.reverse()
    return rows


def search_messages(
    account_id: str, keyword: str, limit: int = 20, conn: db.Connection | None = None,
) -> list[dict]:
    c = conn or db.get_conn()
    return c.query(
        """
        SELECT direction, peer_kind, peer_id, chat_id, msg_id, msg_type, content,
               from_wxid, UNIX_TIMESTAMP(ts) AS at_sec
        FROM wpp_messages
        WHERE account_id = %s AND content LIKE %s
        ORDER BY ts DESC, id DESC LIMIT %s
        """,
        (account_id, f"%{keyword}%", _cap(limit, 1, 200)),
    )


def list_messages_in_window(
    account_id: str, since_sec: int, limit: int = 200, conn: db.Connection | None = None,
) -> list[dict]:
    """窗口内消息（心跳画像/黑话挖掘用）。"""
    c = conn or db.get_conn()
    return c.query(
        """
        SELECT direction, peer_kind, peer_id, chat_id, msg_type, content, from_wxid,
               UNIX_TIMESTAMP(ts) AS at_sec
        FROM wpp_messages
        WHERE account_id = %s AND ts >= FROM_UNIXTIME(%s)
        ORDER BY ts DESC, id DESC LIMIT %s
        """,
        (account_id, since_sec, _cap(limit, 1, 5000)),
    )


def list_group_msg_hour_buckets(
    account_id: str, since_sec: int, local_offset_sec: int, conn: db.Connection | None = None,
) -> list[dict]:
    """每群 × 每小时段的**入站人类消息数**（心流反事实基线 ``ambient_p`` 的素材）。

    时间口径 ``UNIX_TIMESTAMP(ts)`` 是**绝对 epoch**（TIMESTAMP 内部存 UTC，
    反解回来与 session 时区无关）⇒ 加 ``local_offset_sec`` 取模即**本地**小时，
    与运行时按本地时钟分桶一致。
    """
    c = conn or db.get_conn()
    return c.query(
        """
        SELECT chat_id AS group_id,
               FLOOR((((UNIX_TIMESTAMP(ts) + %s) %% 86400) + 86400) %% 86400 / 3600) AS h,
               COUNT(*) AS n
        FROM wpp_messages
        WHERE account_id = %s AND peer_kind = 'group' AND direction = 'inbound'
          AND chat_id IS NOT NULL AND chat_id <> ''
          AND ts >= FROM_UNIXTIME(%s)
        GROUP BY group_id, h
        """,
        (local_offset_sec, account_id, since_sec),
    )


def get_hf_group_message_stats(
    account_id: str, group_id: str, since_sec: int, conn: db.Connection | None = None,
) -> dict:
    """群画像素材：窗口内该群的**入站人类**消息统计（4 条单表聚合）。

    对应 TS ``getHfGroupMessageStats``。**每群每天只跑一次**（生成画像时），不在热路径。

    口径与 :func:`list_group_msg_hour_buckets` 一致：``peer_kind='group'`` +
    ``direction='inbound'``（排除 bot 自己的出站，否则画像自我强化），
    时间走 ``UNIX_TIMESTAMP(ts)``（绝对 epoch）加 ``local_offset_sec`` 偏移后取**本地**小时/日
    ——**不得用 ``create_time``**（遗留死列，见模块 docstring 约束 1）。

    ``local_offset_sec`` 由**窗口起点** ``since_sec`` 推导（与 TS 逐字一致：那边也是
    ``-new Date(sinceSec*1000).getTimezoneOffset()*60``，不是 ``now``）。

    返回 ``{"total", "activeDays", "avgLen", "hourHist", "topSenders", "typeHist"}``：
    ``hourHist`` 恒为 24 个整数（缺的小时补 0），``typeHist`` 的键是**字符串**化的
    ``msg_type``（``NULL`` ⇒ ``"unknown"``，与 TS ``String(r.msg_type)`` 同形），
    ``avgLen`` 按 JS ``Math.round`` 语义取整（**半值向上**，不是 Python 的银行家舍入）。
    """
    c = conn or db.get_conn()
    local = _local_offset_sec(since_sec)
    base = (
        "FROM wpp_messages "
        "WHERE account_id = %s AND peer_kind = 'group' AND direction = 'inbound' "
        "AND chat_id = %s AND chat_id <> '' AND ts >= FROM_UNIXTIME(%s)"
    )
    args = (account_id, group_id, since_sec)
    totals = c.query_one(
        "SELECT COUNT(*) AS n, "
        "AVG(CHAR_LENGTH(COALESCE(content, ''))) AS avg_len, "
        "COUNT(DISTINCT DATE(FROM_UNIXTIME(UNIX_TIMESTAMP(ts) + %s))) AS active_days "
        + base,
        (local,) + args,
    ) or {}
    hours = c.query(
        "SELECT FLOOR((((UNIX_TIMESTAMP(ts) + %s) %% 86400) + 86400) %% 86400 / 3600) AS h, "
        "COUNT(*) AS n " + base + " GROUP BY h",
        (local,) + args,
    )
    senders = c.query(
        "SELECT from_wxid, COUNT(*) AS n " + base
        + " GROUP BY from_wxid ORDER BY n DESC LIMIT 5",
        args,
    )
    types = c.query("SELECT msg_type, COUNT(*) AS n " + base + " GROUP BY msg_type", args)

    hour_hist = [0] * 24
    for r in hours:
        h = int(r.get("h") or 0)
        if 0 <= h < 24:
            hour_hist[h] = int(r.get("n") or 0)
    type_hist: dict[str, int] = {}
    for r in types:
        key = "unknown" if r.get("msg_type") is None else str(r["msg_type"])
        type_hist[key] = type_hist.get(key, 0) + int(r.get("n") or 0)

    return {
        "total": int(totals.get("n") or 0),
        "activeDays": int(totals.get("active_days") or 0),
        "avgLen": _round_half_up(totals.get("avg_len")),
        "hourHist": hour_hist,
        "topSenders": [
            {"wxid": str(r.get("from_wxid") or ""), "n": int(r.get("n") or 0)}
            for r in senders
        ],
        "typeHist": type_hist,
    }


def list_bot_msg_share(
    account_id: str, since_sec: int, conn: db.Connection | None = None,
) -> list[dict]:
    """每群发言占比素材（人 vs bot）。

    ★ ``COALESCE(NULLIF(chat_id,''), peer_id)`` 见模块 docstring 约束 3。
    """
    c = conn or db.get_conn()
    return c.query(
        """
        SELECT COALESCE(NULLIF(chat_id, ''), peer_id) AS gid, direction, COUNT(*) AS n
        FROM wpp_messages
        WHERE account_id = %s AND peer_kind = 'group' AND ts >= FROM_UNIXTIME(%s)
        GROUP BY gid, direction
        """,
        (account_id, since_sec),
    )


def list_outbound_texts(
    account_id: str, since_sec: int, limit: int = 200, conn: db.Connection | None = None,
) -> list[dict]:
    """群聊出站**文本**（重复率素材）。返回按时间**升序**（DB 侧取最近 N 条后反转）。"""
    c = conn or db.get_conn()
    rows = c.query(
        """
        SELECT COALESCE(NULLIF(chat_id, ''), peer_id) AS gid,
               content, UNIX_TIMESTAMP(ts) AS at_sec
        FROM wpp_messages
        WHERE account_id = %s AND peer_kind = 'group' AND direction = 'outbound'
          AND content IS NOT NULL AND content <> ''
          AND ts >= FROM_UNIXTIME(%s)
        ORDER BY at_sec DESC LIMIT %s
        """,
        (account_id, since_sec, _cap(limit, 1, 2000)),
    )
    rows.reverse()
    return rows


def list_active_group_ids(
    account_id: str, since_sec: int, conn: db.Connection | None = None,
) -> list[str]:
    """窗口内有入站消息的群 id（sweep 遍历用，单表）。"""
    c = conn or db.get_conn()
    rows = c.query(
        """
        SELECT DISTINCT COALESCE(NULLIF(chat_id, ''), peer_id) AS gid
        FROM wpp_messages
        WHERE account_id = %s AND peer_kind = 'group' AND direction = 'inbound'
          AND ts >= FROM_UNIXTIME(%s)
        """,
        (account_id, since_sec),
    )
    return [str(r["gid"]) for r in rows if r.get("gid")]


# --------------------------------------------------------------------------- 联系人 / 群


def upsert_contact(
    account_id: str, wxid: str, nickname: str = "", remark: str = "",
    avatar_url: str = "", conn: db.Connection | None = None,
) -> int:
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_contacts
          (account_id, wxid, nickname, remark, avatar_url, last_synced_at)
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON DUPLICATE KEY UPDATE
          nickname = VALUES(nickname), remark = VALUES(remark),
          avatar_url = VALUES(avatar_url), last_synced_at = CURRENT_TIMESTAMP
        """,
        (account_id, wxid, nickname, remark, avatar_url),
    )


def get_contact(account_id: str, wxid: str, conn: db.Connection | None = None) -> dict | None:
    c = conn or db.get_conn()
    return c.query_one(
        "SELECT wxid, nickname, remark, avatar_url FROM wpp_contacts "
        "WHERE account_id = %s AND wxid = %s LIMIT 1",
        (account_id, wxid),
    )


def list_contacts(
    account_id: str, limit: int = 50, offset: int = 0, conn: db.Connection | None = None,
) -> list[dict]:
    """查 ``wpp_contacts`` —— 某账号的联系人列表（MCP ``mcpListContacts`` 的后端）。

    按 ``id`` 升序分页（``id`` = 入库顺序）。⚠️ 这里**刻意**从 ``ORDER BY wxid``
    改回 ``id``：旧 ``plugin/db.py:list_contacts`` 就是 ``ORDER BY id``，
    而 MCP 工具的可见输出要**逐位对齐**旧实现（``tools/verify_tools_port.py``
    的差异测试只比工具名/toolset/schema，比不出 SQL 语义，所以在此显式记一笔）。
    与 :func:`list_chatrooms` 的排序键保持一致。
    """
    c = conn or db.get_conn()
    return c.query(
        "SELECT wxid, nickname, remark, avatar_url FROM wpp_contacts "
        "WHERE account_id = %s ORDER BY id LIMIT %s OFFSET %s",
        (account_id, _cap(limit, 1, 500), max(offset, 0)),
    )


def search_contacts(
    account_id: str, keyword: str, limit: int = 20, conn: db.Connection | None = None,
) -> list[dict]:
    """查 ``wpp_contacts`` —— 按关键词搜联系人（MCP ``mcpSearch`` 的后端）。

    只在 ``wxid`` / ``nickname`` 两列上 ``LIKE '%kw%'``（与旧 ``plugin/db.py`` 一致，
    **不搜** ``remark``）。``keyword`` 原样进 LIKE、不额外转义 ``%``/``_``（保持旧语义）。
    LIKE 的 ``%`` 走**参数**传递 ⇒ SQL 文本里没有裸 ``%``，不触发 ``%`` 插值陷阱。
    """
    c = conn or db.get_conn()
    like = f"%{keyword}%"
    return c.query(
        "SELECT wxid, nickname, remark FROM wpp_contacts "
        "WHERE account_id = %s AND (wxid LIKE %s OR nickname LIKE %s) LIMIT %s",
        (account_id, like, like, _cap(limit, 1, 200)),
    )


def upsert_chatroom(
    account_id: str, chatroom_id: str, nickname: str = "", remark: str = "",
    owner_wxid: str | None = None, member_count: int | None = None,
    conn: db.Connection | None = None,
) -> int:
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_chatrooms
          (account_id, chatroom_id, nickname, remark, owner_wxid, member_count, last_synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON DUPLICATE KEY UPDATE
          nickname = VALUES(nickname), remark = VALUES(remark),
          owner_wxid = COALESCE(VALUES(owner_wxid), owner_wxid),
          member_count = COALESCE(VALUES(member_count), member_count),
          last_synced_at = CURRENT_TIMESTAMP
        """,
        (account_id, chatroom_id, nickname, remark, owner_wxid, member_count),
    )


def get_chatroom(
    account_id: str, chatroom_id: str, conn: db.Connection | None = None,
) -> dict | None:
    c = conn or db.get_conn()
    return c.query_one(
        "SELECT chatroom_id, nickname, remark, owner_wxid, member_count FROM wpp_chatrooms "
        "WHERE account_id = %s AND chatroom_id = %s LIMIT 1",
        (account_id, chatroom_id),
    )


def list_chatrooms(
    account_id: str, limit: int = 20, offset: int = 0, conn: db.Connection | None = None,
) -> list[dict]:
    """查 ``wpp_chatrooms`` —— 某账号的群列表（MCP ``mcpListGroups`` 的后端）。

    只回 ``chatroom_id`` / ``nickname`` / ``remark``，按 ``id`` 升序分页（``id`` = 入库顺序，
    与旧 ``plugin/db.py`` 一致）。与 :func:`list_contacts` 的排序键一致。
    """
    c = conn or db.get_conn()
    return c.query(
        "SELECT chatroom_id, nickname, remark FROM wpp_chatrooms "
        "WHERE account_id = %s ORDER BY id LIMIT %s OFFSET %s",
        (account_id, _cap(limit, 1, 500), max(offset, 0)),
    )


def upsert_chatroom_member(
    account_id: str, chatroom_id: str, wxid: str, nickname: str = "",
    avatar_url: str = "", is_owner: int = 0, conn: db.Connection | None = None,
) -> int:
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_chatroom_members
          (account_id, chatroom_id, wxid, nickname, avatar_url, is_owner, last_synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        ON DUPLICATE KEY UPDATE
          nickname = VALUES(nickname), avatar_url = VALUES(avatar_url),
          is_owner = VALUES(is_owner), last_synced_at = CURRENT_TIMESTAMP
        """,
        (account_id, chatroom_id, wxid, nickname, avatar_url, int(is_owner)),
    )


def get_chatroom_member(
    account_id: str, chatroom_id: str, wxid: str, conn: db.Connection | None = None,
) -> dict | None:
    c = conn or db.get_conn()
    return c.query_one(
        "SELECT wxid, nickname, is_owner FROM wpp_chatroom_members "
        "WHERE account_id = %s AND chatroom_id = %s AND wxid = %s LIMIT 1",
        (account_id, chatroom_id, wxid),
    )


def list_chatroom_members(
    account_id: str, chatroom_id: str, limit: int = 500, conn: db.Connection | None = None,
) -> list[dict]:
    c = conn or db.get_conn()
    return c.query(
        "SELECT wxid, nickname, is_owner FROM wpp_chatroom_members "
        "WHERE account_id = %s AND chatroom_id = %s LIMIT %s",
        (account_id, chatroom_id, _cap(limit, 1, 2000)),
    )


# --------------------------------------------------------------------------- 黑话


def upsert_jargon(
    account_id: str, group_id: str, term: str, raw_content: str = "",
    meaning: str = "", is_jargon: int = 1, conn: db.Connection | None = None,
) -> int:
    """记一条黑话候选；重复出现只加词频。"""
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_jargon_terms
          (account_id, group_id, term, raw_content, meaning, is_jargon, frequency)
        VALUES (%s, %s, %s, %s, %s, %s, 1)
        ON DUPLICATE KEY UPDATE
          frequency = frequency + 1,
          raw_content = COALESCE(NULLIF(VALUES(raw_content), ''), raw_content),
          meaning = COALESCE(NULLIF(VALUES(meaning), ''), meaning)
        """,
        (account_id, group_id, term[:64], raw_content[:512], meaning[:512], int(is_jargon)),
    )


def list_jargon(
    account_id: str, group_id: str | None = None, limit: int = 100,
    conn: db.Connection | None = None,
) -> list[dict]:
    c = conn or db.get_conn()
    if group_id:
        return c.query(
            "SELECT group_id, term, meaning, frequency FROM wpp_jargon_terms "
            "WHERE account_id = %s AND group_id = %s ORDER BY frequency DESC LIMIT %s",
            (account_id, group_id, _cap(limit, 1, 1000)),
        )
    return c.query(
        "SELECT group_id, term, meaning, frequency FROM wpp_jargon_terms "
        "WHERE account_id = %s ORDER BY frequency DESC LIMIT %s",
        (account_id, _cap(limit, 1, 1000)),
    )


def has_jargon(
    account_id: str, group_id: str, term: str, conn: db.Connection | None = None,
) -> bool:
    c = conn or db.get_conn()
    return bool(c.query_one(
        "SELECT 1 AS x FROM wpp_jargon_terms "
        "WHERE account_id = %s AND group_id = %s AND term = %s LIMIT 1",
        (account_id, group_id, term),
    ))


# --------------------------------------------------------------------------- 审计


def record_api_call(
    account_id: str, endpoint: str, *, method: str = "POST", status_code: int | None = None,
    vendor_code: int | None = None, latency_ms: int | None = None,
    request_body: Any = None, response_body: Any = None,
    conn: db.Connection | None = None,
) -> int:
    """记一次供应商调用（**失败不该影响主流程** —— 调用方自行 try）。"""
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_api_calls
          (account_id, endpoint, method, status_code, vendor_code, latency_ms,
           request_body, response_body)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (account_id, endpoint, method, status_code, vendor_code, latency_ms,
         _json_or_none(request_body), _json_or_none(response_body)),
    )


# --------------------------------------------------------------------------- 红包


def record_redpacket(
    account_id: str, peer_id: str, from_wxid: str, hb_url: str = "", hb_key: str = "",
    raw_json: Any = None, conn: db.Connection | None = None,
) -> int:
    """记一个红包（幂等键 ``(account, peer, from, hb_url)``）。

    ⚠️ 调用前**必须先过** ``inbound.hongbao.is_red_packet_message`` ——
    2026-09-27 生产事故就是接龙文案自带「红包」字样被这把闸整条丢掉。
    """
    c = conn or db.get_conn()
    payload = None
    if raw_json is not None:
        payload = raw_json if isinstance(raw_json, str) else json.dumps(raw_json, ensure_ascii=False)
    return c.execute(
        """
        INSERT INTO wpp_redpackets
          (account_id, peer_id, from_wxid, hb_url, hb_key, raw_json, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE raw_json = VALUES(raw_json)
        """,
        (account_id, peer_id, from_wxid, (hb_url or "")[:512], (hb_key or "")[:128],
         (payload or "")[:4000], int(time.time())),
    )


def list_redpackets(
    account_id: str, peer_id: str = "", limit: int = 50, conn: db.Connection | None = None,
) -> list[dict]:
    c = conn or db.get_conn()
    if peer_id:
        return c.query(
            "SELECT peer_id, from_wxid, hb_url, hb_key, opened, created_at FROM wpp_redpackets "
            "WHERE account_id = %s AND peer_id = %s ORDER BY id DESC LIMIT %s",
            (account_id, peer_id, _cap(limit, 1, 500)),
        )
    return c.query(
        "SELECT peer_id, from_wxid, hb_url, hb_key, opened, created_at FROM wpp_redpackets "
        "WHERE account_id = %s ORDER BY id DESC LIMIT %s",
        (account_id, _cap(limit, 1, 500)),
    )


# --------------------------------------------------------------------------- 辅助


def save_svrid(
    account_id: str, svrid: str, msg_md5: str = "", quoted_content_hash: str = "",
    conn: db.Connection | None = None,
) -> int:
    """svrid ↔ msg 映射（引用解析用）。"""
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_svrid_mapping
          (account_id, svrid, msg_md5, quoted_content_hash, captured_at)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          msg_md5 = COALESCE(NULLIF(VALUES(msg_md5), ''), msg_md5),
          quoted_content_hash = COALESCE(NULLIF(VALUES(quoted_content_hash), ''), quoted_content_hash)
        """,
        (account_id, svrid, msg_md5, quoted_content_hash, int(time.time())),
    )


def lookup_svrid(account_id: str, svrid: str, conn: db.Connection | None = None) -> dict | None:
    c = conn or db.get_conn()
    return c.query_one(
        "SELECT svrid, msg_md5, quoted_content_hash, captured_at FROM wpp_svrid_mapping "
        "WHERE account_id = %s AND svrid = %s LIMIT 1",
        (account_id, svrid),
    )


def get_sync_state(account_id: str, conn: db.Connection | None = None) -> str | None:
    c = conn or db.get_conn()
    row = c.query_one(
        "SELECT synckey FROM wpp_sync_state WHERE account_id = %s LIMIT 1", (account_id,),
    )
    return None if row is None else str(row.get("synckey") or "")


def set_sync_state(account_id: str, synckey: str, conn: db.Connection | None = None) -> int:
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_sync_state (account_id, synckey, updated_at)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE synckey = VALUES(synckey), updated_at = VALUES(updated_at)
        """,
        (account_id, (synckey or "")[:1024], int(time.time())),
    )


def touch_session_state(
    account_id: str, peer_kind: str, peer_id: str, last_msg_id: str | None = None,
    conn: db.Connection | None = None,
) -> int:
    """刷新会话状态（防抖/触发器用）；``pending_count`` 由调用方决定语义。"""
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_session_state
          (account_id, peer_kind, peer_id, last_msg_id, last_msg_ts, pending_count)
        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, 1)
        ON DUPLICATE KEY UPDATE
          last_msg_id = COALESCE(VALUES(last_msg_id), last_msg_id),
          last_msg_ts = CURRENT_TIMESTAMP,
          pending_count = pending_count + 1
        """,
        (account_id, peer_kind, peer_id, last_msg_id),
    )


# --------------------------------------------------------------------------- 内部


def _json_or_none(value: Any) -> str | None:
    """把一个值变成**合法 JSON 文本**（列类型是 JSON，不是 TEXT）。

    ⚠️ 旧实现直接 ``text[:60000]``：截断点落在 JSON 中间 ⇒ MariaDB 的 JSON 列拒收，
    整条审计写不进去；更隐蔽的是调用方传**非 JSON 的普通字符串**（很多 vendor 错误体
    就是一行文本）时，原文塞进 JSON 列同样非法。两种都只表现为"审计少了一条"。
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value
        try:
            json.loads(text)
        except (ValueError, TypeError):
            text = json.dumps(text, ensure_ascii=False)
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > 60000:
        # 截断**外壳**、保留合法 JSON：给 head + 原始长度，够定位问题又不撑爆列
        text = json.dumps(
            {"truncated": True, "length": len(text), "head": text[:59000]},
            ensure_ascii=False,
        )
    return text


def _cap(value: int, low: int, high: int) -> int:
    """LIMIT 一律夹紧 —— 防调用方传 0/负数/天文数字。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = low
    return max(low, min(n, high))


def _local_offset_sec(ts_sec: int) -> int:
    """本地 UTC 偏移（秒）—— 与 JS ``-new Date(ts*1000).getTimezoneOffset() * 60`` 同义。

    两种写法差在两个细节上，别顺手改：**符号**（JS 的 ``getTimezoneOffset`` 西正东负）
    和**取样时刻**（走 ``astimezone()`` 让运行时按**那个时刻**的夏令时规则给偏移，
    不写死 +8h）。
    """
    off = datetime.fromtimestamp(ts_sec).astimezone().utcoffset()
    return int(off.total_seconds()) if off else 0


def _round_half_up(value: Any) -> int:
    """JS ``Math.round`` 的等价（**半值向 +∞**，不是 Python 的银行家舍入）。

    ``Math.round(2.5) === 3`` 而 ``round(2.5) == 2``；``avg_len`` 只可能非负，
    所以"半值向 +∞"用 ``floor(x + 0.5)`` 就够了。取不到数（子查询无行 ⇒ ``NULL``）⇒ 0。
    """
    if value is None:
        return 0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(f):
        return 0
    return int(math.floor(f + 0.5))
