"""WPP MariaDB 存储（迁移自 wpp-openclaw storage/db/）。

连接 wechatpro 库（1Panel-mariadb-RlbK），复用现有 9 张表。
消息落库幂等（UNIQUE + ON DUPLICATE KEY UPDATE）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import threading

import pymysql

log = logging.getLogger(__name__)

_pool = None  # pymysql connection（轻量单连接 + 自动重连）
# P2-5 修复：threading.RLock 覆盖 webhook 线程 + 主 loop 的并发 DB 访问
#   （pymysql 连接非协程/线程安全，单连接并发 cursor 会协议交错）
_db_lock = threading.RLock()


def _get_conn():
    """获取 pymysql 连接（懒加载，断线自动重连）。"""
    global _pool
    with _db_lock:
        try:
            if _pool and _pool.open:
                return _pool
        except Exception:  # noqa: BLE001
            pass
        host = os.environ.get("WECHATPRO_DB_HOST", "127.0.0.1")
        port = int(os.environ.get("WECHATPRO_DB_PORT", "3306"))
        user = os.environ.get("WECHATPRO_DB_USER", "wechatpro")
        password = os.environ.get("WECHATPRO_DB_PASSWORD", "")
        database = os.environ.get("WECHATPRO_DB_NAME", "wechatpro")
        try:
            _pool = pymysql.connect(
                host=host, port=port, user=user, password=password, database=database,
                charset="utf8mb4", autocommit=True, connect_timeout=5,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] DB 连接失败: %s", e)
            return None
        return _pool


def _execute(sql: str, params: tuple) -> None:
    global _pool
    with _db_lock:
        conn = _get_conn()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            cur.close()
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] DB 执行失败: %s", e)
            _reset_conn()


def _query(sql: str, params: tuple) -> list[dict]:
    with _db_lock:
        conn = _get_conn()
        if not conn:
            return []
        try:
            cur = conn.cursor(pymysql.cursors.DictCursor)
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return rows
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] DB 查询失败: %s", e)
            _reset_conn()  # P2-5 修复：查询失败也重置连接（原来只 log 不重置 → 坏连接复用）
            return []


def _reset_conn() -> None:
    """关闭并重置全局连接（带锁，防并发重复 close）。"""
    global _pool
    with _db_lock:
        try:
            if _pool is not None:
                _pool.close()
        except Exception:  # noqa: BLE001
            pass
        _pool = None


# ------------------------------------------------------------------ 消息
def save_message(account_id: str, msg_id: Optional[str], new_msg_id: Optional[str],
                 direction: str, peer_kind: str, peer_id: str, peer_name: Optional[str],
                 chat_id: Optional[str], msg_type: Optional[str], content: Optional[str],
                 raw_payload=None, from_wxid: Optional[str] = None, ts: Optional[int] = None,
                 delivery_status: Optional[str] = None,
                 delivery_error: Optional[str] = None,
                 delivery_message_id: Optional[str] = None) -> None:
    """保存消息（幂等）。

    Phase 4.1 (2026-09-01): 加 delivery_status / delivery_error / delivery_message_id 字段
    让老板能查 cron 出站投递状态:
      SELECT msg_id, peer_id, content, delivery_status, delivery_error, delivery_message_id
      FROM wpp_messages WHERE direction='outbound' ORDER BY id DESC LIMIT 20;
    """
    # Phase 4.1: 懒加列 (IF NOT EXISTS 是 MySQL 8.0+, 兼容老版本用 try/except)
    _ensure_delivery_columns()

    sql = """
        INSERT INTO wpp_messages
        (account_id, msg_id, new_msg_id, direction, peer_kind, peer_id, peer_name,
         chat_id, msg_type, content, raw_payload, from_wxid, ts,
         delivery_status, delivery_error, delivery_message_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(FROM_UNIXTIME(%s), CURRENT_TIMESTAMP),
         %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          direction = VALUES(direction),
          peer_kind = VALUES(peer_kind),
          peer_id = VALUES(peer_id),
          msg_type = VALUES(msg_type),
          content = VALUES(content),
          raw_payload = VALUES(raw_payload),
          from_wxid = VALUES(from_wxid),
          delivery_status = VALUES(delivery_status),
          delivery_error = VALUES(delivery_error),
          delivery_message_id = VALUES(delivery_message_id)
    """
    raw_json = json.dumps(raw_payload, ensure_ascii=False, default=str) if raw_payload is not None else None
    _execute(sql, (
        account_id, msg_id, new_msg_id, direction, peer_kind, peer_id, peer_name,
        chat_id, msg_type, content, raw_json, from_wxid, ts,
        delivery_status, delivery_error, delivery_message_id,
    ))


_delivery_columns_checked = False

def _ensure_delivery_columns() -> None:
    """幂等地加 delivery_status / delivery_error / delivery_message_id 列 (Phase 4.1).

    用 SHOW COLUMNS 检查避免每次 INSERT 都跑 ALTER (昂贵).
    失败时 silently 忽略 — 老 MySQL 没 IF NOT EXISTS 加列会报 Duplicate column.
    """
    global _delivery_columns_checked
    if _delivery_columns_checked:
        return
    try:
        sql = """
            SELECT COUNT(*) AS cnt FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'wpp_messages'
              AND column_name = 'delivery_status'
        """
        rows = _query(sql, ())
        if rows and rows[0].get("cnt", 0) == 0:
            # 一次性加 3 列
            conn = _get_conn()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("ALTER TABLE wpp_messages ADD COLUMN delivery_status VARCHAR(32) NULL DEFAULT NULL")
                    cur.execute("ALTER TABLE wpp_messages ADD COLUMN delivery_error TEXT NULL DEFAULT NULL")
                    cur.execute("ALTER TABLE wpp_messages ADD COLUMN delivery_message_id VARCHAR(128) NULL DEFAULT NULL")
                    cur.execute("ALTER TABLE wpp_messages ADD INDEX idx_delivery_status (delivery_status)")
                    log.info("[WPP] delivery_status columns added to wpp_messages")
        _delivery_columns_checked = True
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] ensure_delivery_columns 失败 (非致命): %s", e)
        _delivery_columns_checked = True  # 避免反复尝试


def update_delivery_status(account_id: str, msg_id: str, status: str,
                            error: Optional[str] = None,
                            delivery_message_id: Optional[str] = None) -> None:
    """Phase 4.1: 出站投递完成后回调 — 更新 delivery_status.

    Args:
        account_id: 账号 ID
        msg_id: vendor 返回的 msg_id (vendor 标识)
        status: "success" / "skipped" / "failed" / "pending"
        error: 失败时的 error message (可选)
        delivery_message_id: vendor 返回的 message_id (可选, 有些平台 msg_id == message_id)

    用于 cron 投递场景 (standalone_send / out-of-process send):
      1. _standalone_send 调 vendor API
      2. 拿到 vendor response
      3. 调 update_delivery_status(...) 把结果写回 DB
      4. 老板查 SELECT ... WHERE msg_id=X 看 delivery_status
    """
    sql = """
        UPDATE wpp_messages
        SET delivery_status = %s,
            delivery_error = %s,
            delivery_message_id = COALESCE(NULLIF(%s, ''), delivery_message_id)
        WHERE account_id = %s AND msg_id = %s
    """
    try:
        _execute(sql, (status, error, delivery_message_id, account_id, msg_id))
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] update_delivery_status 失败: %s", e)


def list_outbound_with_delivery(account_id: str = "", limit: int = 20) -> list[dict]:
    """Phase 4.1: 列出最近的出站消息 + delivery 状态 (老板可观测性).

    Args:
        account_id: 可选, 只看某个账号
        limit: 最多返回 N 条

    Returns:
        list of dict {msg_id, peer_id, content, ts, delivery_status,
                       delivery_error, delivery_message_id}
    """
    if account_id:
        sql = """
            SELECT msg_id, peer_id, content, ts, delivery_status,
                   delivery_error, delivery_message_id
            FROM wpp_messages
            WHERE direction = 'outbound' AND account_id = %s
            ORDER BY id DESC
            LIMIT %s
        """
        return _query(sql, (account_id, limit))
    else:
        sql = """
            SELECT account_id, msg_id, peer_id, content, ts, delivery_status,
                   delivery_error, delivery_message_id
            FROM wpp_messages
            WHERE direction = 'outbound'
            ORDER BY id DESC
            LIMIT %s
        """
        return _query(sql, (limit,))


def query_messages(account_id: str, peer_id: str, limit: int = 20) -> list[dict]:
    """查询与某人的最近消息（群上下文/引用回复用）。"""
    sql = """
        SELECT * FROM wpp_messages
        WHERE account_id=%s AND peer_id=%s
        ORDER BY ts DESC LIMIT %s
    """
    return _query(sql, (account_id, peer_id, int(limit)))


def query_contact_by_wxid(account_id: str, wxid: str) -> Optional[dict]:
    rows = _query("SELECT * FROM wpp_contacts WHERE account_id=%s AND wxid=%s", (account_id, wxid))
    return rows[0] if rows else None


def upsert_contact(account_id: str, wxid: str, nickname: str = "", remark: str = "", avatar_url: str = "") -> None:
    sql = """
        INSERT INTO wpp_contacts (account_id, wxid, nickname, remark, avatar_url)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE nickname=VALUES(nickname), remark=VALUES(remark), avatar_url=VALUES(avatar_url)
    """
    _execute(sql, (account_id, wxid, nickname, remark, avatar_url))


def query_chatroom(account_id: str, chatroom_id: str) -> Optional[dict]:
    rows = _query("SELECT * FROM wpp_chatrooms WHERE account_id=%s AND chatroom_id=%s", (account_id, chatroom_id))
    return rows[0] if rows else None


def upsert_chatroom(account_id: str, chatroom_id: str, nickname: str = "", remark: str = "") -> None:
    sql = """
        INSERT INTO wpp_chatrooms (account_id, chatroom_id, nickname, remark)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE nickname=VALUES(nickname), remark=VALUES(remark)
    """
    _execute(sql, (account_id, chatroom_id, nickname, remark))


def list_contacts(account_id: str, limit: int = 20, offset: int = 0) -> list[dict]:
    """联系人列表（MCP mcpListContacts）。"""
    return _query(
        "SELECT wxid, nickname, remark FROM wpp_contacts WHERE account_id=%s ORDER BY id LIMIT %s OFFSET %s",
        (account_id, int(limit), int(offset)),
    )


def search_contacts(account_id: str, keyword: str, limit: int = 20) -> list[dict]:
    """搜索联系人（MCP mcpSearch）。"""
    like = f"%{keyword}%"
    return _query(
        "SELECT wxid, nickname, remark FROM wpp_contacts WHERE account_id=%s AND (wxid LIKE %s OR nickname LIKE %s) LIMIT %s",
        (account_id, like, like, int(limit)),
    )


def list_chatrooms(account_id: str, limit: int = 20, offset: int = 0) -> list[dict]:
    """群列表（MCP mcpListGroups）。"""
    return _query(
        "SELECT chatroom_id, nickname, remark FROM wpp_chatrooms WHERE account_id=%s ORDER BY id LIMIT %s OFFSET %s",
        (account_id, int(limit), int(offset)),
    )


def list_messages(account_id: str, limit: int = 50) -> list[dict]:
    """最近消息（MCP mcpRecentMessages）。"""
    return _query(
        "SELECT direction, peer_id, peer_name, msg_type, content, from_wxid, ts FROM wpp_messages WHERE account_id=%s ORDER BY ts DESC LIMIT %s",
        (account_id, int(limit)),
    )


def search_messages(account_id: str, keyword: str, limit: int = 20) -> list[dict]:
    """按关键词搜历史消息（wpp-history skill direct tool 用）。"""
    like = f"%{keyword}%"
    return _query(
        "SELECT direction, peer_id, peer_name, msg_type, content, from_wxid, ts FROM wpp_messages "
        "WHERE account_id=%s AND content LIKE %s ORDER BY ts DESC LIMIT %s",
        (account_id, like, int(limit)),
    )


def list_messages_by_peer(account_id: str, peer_id: str, limit: int = 20) -> list[dict]:
    """按对端(单聊 wxid / 群 @chatroom)查历史消息。"""
    return _query(
        "SELECT direction, peer_id, peer_name, msg_type, content, from_wxid, ts FROM wpp_messages "
        "WHERE account_id=%s AND peer_id=%s ORDER BY ts DESC LIMIT %s",
        (account_id, peer_id, int(limit)),
    )


def query_chatroom_member(account_id: str, chatroom_id: str, wxid: str) -> Optional[dict]:
    """查询群成员（wpp-identity 技能脱敏判断用）。"""
    rows = _query(
        "SELECT wxid, nickname, is_owner FROM wpp_chatroom_members WHERE account_id=%s AND chatroom_id=%s AND wxid=%s",
        (account_id, chatroom_id, wxid),
    )
    return rows[0] if rows else None


def list_chatroom_members(account_id: str, chatroom_id: str, limit: int = 200) -> list[dict]:
    """查询群成员列表。"""
    return _query(
        "SELECT wxid, nickname, is_owner FROM wpp_chatroom_members WHERE account_id=%s AND chatroom_id=%s LIMIT %s",
        (account_id, chatroom_id, int(limit)),
    )


def upsert_chatroom_member(account_id: str, chatroom_id: str, wxid: str, nickname: str = "", is_owner: int = 0) -> None:
    """同步群成员。"""
    sql = """
        INSERT INTO wpp_chatroom_members (account_id, chatroom_id, wxid, nickname, is_owner)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE nickname=VALUES(nickname), is_owner=VALUES(is_owner)
    """
    _execute(sql, (account_id, chatroom_id, wxid, nickname, int(is_owner)))


# ------------------------------------------------------------------ 红包
def record_redpacket(account_id: str, peer_id: str, from_wxid: str,
                     hb_url: str = "", hb_key: str = "",
                     raw_json: str = "") -> Optional[int]:
    """记录红包 (wpp_redpackets 表, 2026-08-31 完整对照 P1-3 补全).

    表 schema (MariaDB):
      id, account_id, peer_id, from_wxid, hb_url, hb_key, raw_json, opened (0/1), created_at
    """
    # 幂等: 同一 (account, peer, from, hb_url) 唯一
    try:
        with _db_lock:
            conn = _get_conn()
            if not conn:
                return None
            cur = conn.cursor(pymysql.cursors.DictCursor)
            cur.execute("""
                INSERT INTO wpp_redpackets
                    (account_id, peer_id, from_wxid, hb_url, hb_key, raw_json, opened, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 0, UNIX_TIMESTAMP())
                ON DUPLICATE KEY UPDATE raw_json=VALUES(raw_json)
            """, (account_id, peer_id, from_wxid, hb_url[:512], hb_key[:128], raw_json[:4000]))
            insert_id = cur.lastrowid or 0
            cur.close()
            return int(insert_id) if insert_id else None
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] record_redpacket 失败: %s", e)
        return None


def list_redpackets(account_id: str, peer_id: str = "", limit: int = 50) -> list[dict]:
    """查询红包记录。"""
    if peer_id:
        return _query(
            "SELECT id, account_id, peer_id, from_wxid, hb_url, opened, created_at FROM wpp_redpackets "
            "WHERE account_id=%s AND peer_id=%s ORDER BY id DESC LIMIT %s",
            (account_id, peer_id, int(limit)),
        )
    return _query(
        "SELECT id, account_id, peer_id, from_wxid, hb_url, opened, created_at FROM wpp_redpackets "
        "WHERE account_id=%s ORDER BY id DESC LIMIT %s",
        (account_id, int(limit)),
    )
