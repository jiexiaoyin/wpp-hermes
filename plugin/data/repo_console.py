"""控制台的群活跃统计（只读）。

## 单表纪律

本插件在 MariaDB 上踩过跨表 JOIN 的 collation 坑（见 ``repo_messages`` 模块
docstring）：群表与消息表的字符集/排序规则不一致时，JOIN 会直接报错。控制台
只需要"群列表 + 每群成员数 + 每群收发条数"，三条**单表**查询在 Python 里合并
即可，没必要 JOIN。

## 时间窗

统一用 ``ts``（`wpp_messages` 的索引列），**不用** ``create_time``（遗留列）。
``repo_messages.list_bot_msg_share`` 已带 ``COALESCE(NULLIF(chat_id,''), peer_id)``
的群 key 与 ``FROM_UNIXTIME(%s)`` 过滤，直接复用。

## 缓存

UI 会轮询；10–15 秒的缓存足够新鲜，也避免每次轮询都打一遍大表。
**失败不缓存** —— 数据库抖动恢复后下一次请求就能拿到数据。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import db, repo_messages

#: 缓存 TTL（秒）。控制台轮询间隔通常 5–10 秒。
CACHE_TTL_SEC = 12.0

_cache: dict[tuple[str, int, int], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _iso(value: Any) -> str | None:
    """TIMESTAMP 在 MySQL 返回 datetime、SQLite 返回字符串 —— 统一成字符串给 JSON。"""
    if value is None:
        return None
    return str(value)


def list_group_activity(
    account_id: str,
    *,
    since_hours: int = 24,
    limit: int = 200,
    now: float | None = None,
) -> dict[str, Any]:
    """返回 ``{db_available, since_hours, groups, truncated, totals}``。

    ``groups`` 每项：``chatroom_id / name / member_count(vendor) / local_members /
    inbound / outbound / last_synced_at``。只有消息、没有群档案的 gid 也会列出
    （``name`` 为空、成员数为 0），避免"活跃但没同步过群名"的群在控制台消失。
    """
    since_hours = max(1, min(int(since_hours or 24), 24 * 30))
    limit = max(1, min(int(limit or 200), 500))
    key = (str(account_id), since_hours, limit)
    stamp = time.time() if now is None else float(now)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and stamp - hit[0] < CACHE_TTL_SEC:
            return hit[1]

    since_sec = int(stamp) - since_hours * 3600
    conn = db.get_conn()
    try:
        rooms = conn.query(
            "SELECT chatroom_id, nickname, remark, member_count, last_synced_at "
            "FROM wpp_chatrooms WHERE account_id = %s ORDER BY id",
            (account_id,),
        )
        member_rows = conn.query(
            "SELECT chatroom_id, COUNT(*) AS n FROM wpp_chatroom_members "
            "WHERE account_id = %s GROUP BY chatroom_id",
            (account_id,),
        )
        share = repo_messages.list_bot_msg_share(account_id, since_sec, conn=conn)
    finally:
        # 统计是纯读；连接属于线程池线程，用完就还（避免 dashboard 长跑漏连接）。
        db.close()

    local_members = {str(r.get("chatroom_id")): int(r.get("n") or 0)
                     for r in member_rows}
    counts: dict[str, dict[str, int]] = {}
    for row in share:
        gid = str(row.get("gid") or "")
        if not gid:
            continue
        direction = str(row.get("direction") or "")
        bucket = counts.setdefault(gid, {"inbound": 0, "outbound": 0})
        if direction in bucket:
            bucket[direction] += int(row.get("n") or 0)

    merged: dict[str, dict[str, Any]] = {}
    for row in rooms:
        gid = str(row.get("chatroom_id") or "")
        if not gid:
            continue
        merged[gid] = {
            "chatroom_id": gid,
            "name": str(row.get("nickname") or row.get("remark") or ""),
            "member_count": (int(row["member_count"])
                             if row.get("member_count") is not None else None),
            "local_members": local_members.get(gid, 0),
            "inbound": 0,
            "outbound": 0,
            "last_synced_at": _iso(row.get("last_synced_at")),
        }
    for gid, bucket in counts.items():
        item = merged.setdefault(gid, {
            "chatroom_id": gid, "name": "", "member_count": None,
            "local_members": local_members.get(gid, 0),
            "inbound": 0, "outbound": 0, "last_synced_at": None,
        })
        item["inbound"] = bucket["inbound"]
        item["outbound"] = bucket["outbound"]

    groups = list(merged.values())
    groups.sort(key=lambda g: (-(g["inbound"] + g["outbound"]), g["chatroom_id"]))
    truncated = len(groups) > limit
    # 总量按**全部**群算，不受 limit 截断影响（overview 的 24h 收发条数用它）。
    totals = {
        "inbound": sum(int(g["inbound"]) for g in groups),
        "outbound": sum(int(g["outbound"]) for g in groups),
    }
    result = {
        "db_available": True,
        "since_hours": since_hours,
        "truncated": truncated,
        "totals": totals,
        "groups": groups[:limit],
    }
    with _cache_lock:
        _cache[key] = (stamp, result)
        # 简单容量控制：控制台只有少数账号/窗口，超过 64 条时清最旧的。
        if len(_cache) > 64:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[:16]
            for old_key, _ in oldest:
                _cache.pop(old_key, None)
    return result


def clear_cache() -> None:
    """测试 / 手动刷新用。"""
    with _cache_lock:
        _cache.clear()
