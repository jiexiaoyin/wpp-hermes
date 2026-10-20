"""控制台的只读查询（群 / 消息 / 心流 / 风格 / 好感度）。

## 单表纪律

本插件在 MariaDB 上踩过跨表 JOIN 的 collation 坑（见 ``repo_messages`` 模块
docstring）：群表与消息表的字符集/排序规则不一致时，JOIN 会直接报错。控制台
只需要"群列表 + 每群成员数 + 每群收发条数"，几条**单表**查询在 Python 里合并
即可，没必要 JOIN。

## 时间窗

统一用 ``ts``（`wpp_messages` 的索引列），**不用** ``create_time``（遗留列）。
群归属 key 统一 ``COALESCE(NULLIF(chat_id,''), peer_id)``（出站行只写 peer_id）。

## 缓存

UI 会轮询；10–15 秒的缓存足够新鲜，也避免每次轮询都打一遍大表。
**失败不缓存** —— 数据库抖动恢复后下一次请求就能拿到数据。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any

from . import db, repo_affection, repo_hf, repo_messages, repo_style

#: 缓存 TTL（秒）。控制台轮询间隔通常 5–10 秒。
CACHE_TTL_SEC = 12.0

#: 群活跃缓存：``(account_id, since_hours)`` → ``(stamp, dataset)``。
#: q/sort/page **不进缓存键** —— 同一份全量结果上做过滤/排序/切片。
_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()

#: 消息分析缓存（短 TTL，独立一份，避免和群列表互相挤）。
_analytics_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_analytics_lock = threading.Lock()


def _iso(value: Any) -> str | None:
    """TIMESTAMP 在 MySQL 返回 datetime、SQLite 返回字符串 —— 统一成字符串给 JSON。"""
    if value is None:
        return None
    return str(value)


def _local_offset_sec(ts_sec: int) -> int:
    """本地 UTC 偏移（秒），与 ``repo_messages._local_offset_sec`` 同义。"""
    off = datetime.fromtimestamp(ts_sec).astimezone().utcoffset()
    return int(off.total_seconds()) if off else 0


def _local_hour_start(now: int) -> int:
    dt = datetime.fromtimestamp(now).replace(minute=0, second=0, microsecond=0)
    return int(dt.timestamp())


def _local_day_start(now: int) -> int:
    dt = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(dt.timestamp())


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- 群列表


def _group_dataset(
    account_id: str, *, since_hours: int = 24, now: float | None = None,
) -> dict[str, Any]:
    """全量群活跃数据（缓存）。返回 ``{db_available, since_hours, groups, totals}``。"""
    since_hours = max(1, min(_int(since_hours, 24), 24 * 30))
    key = (str(account_id), since_hours)
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

    local_members = {str(r.get("chatroom_id")): _int(r.get("n"))
                     for r in member_rows}
    counts: dict[str, dict[str, int]] = {}
    for row in share:
        gid = str(row.get("gid") or "")
        if not gid:
            continue
        direction = str(row.get("direction") or "")
        bucket = counts.setdefault(gid, {"inbound": 0, "outbound": 0})
        if direction in bucket:
            bucket[direction] += _int(row.get("n"))

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
    totals = {
        "inbound": sum(int(g["inbound"]) for g in groups),
        "outbound": sum(int(g["outbound"]) for g in groups),
    }
    result = {
        "db_available": True,
        "since_hours": since_hours,
        "totals": totals,
        "groups": groups,
    }
    with _cache_lock:
        _cache[key] = (stamp, result)
        # 简单容量控制：控制台只有少数账号/窗口，超过 64 条时清最旧的。
        if len(_cache) > 64:
            oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[:16]
            for old_key, _ in oldest:
                _cache.pop(old_key, None)
    return result


def list_group_activity(
    account_id: str,
    *,
    since_hours: int = 24,
    limit: int = 200,
    now: float | None = None,
) -> dict[str, Any]:
    """返回 ``{db_available, since_hours, groups, total, truncated, totals}``。

    ``groups`` 每项：``chatroom_id / name / member_count(vendor) / local_members /
    inbound / outbound / last_synced_at``。只有消息、没有群档案的 gid 也会列出
    （``name`` 为空、成员数为 0），避免"活跃但没同步过群名"的群在控制台消失。
    """
    data = _group_dataset(account_id, since_hours=since_hours, now=now)
    limit = max(1, min(_int(limit, 200), 500))
    groups = data["groups"]
    return {
        "db_available": True,
        "since_hours": data["since_hours"],
        "total": len(groups),
        "truncated": len(groups) > limit,
        "totals": data["totals"],
        "groups": groups[:limit],
    }


def list_groups_page(
    account_id: str,
    *,
    q: str = "",
    sort: str = "activity",
    since_hours: int = 24,
    limit: int = 50,
    offset: int = 0,
    now: float | None = None,
) -> dict[str, Any]:
    """群列表搜索/排序/分页（在缓存的全量结果上做）。"""
    data = _group_dataset(account_id, since_hours=since_hours, now=now)
    groups = list(data["groups"])
    needle = str(q or "").strip().casefold()
    if needle:
        groups = [
            g for g in groups
            if needle in str(g.get("name") or "").casefold()
            or needle in str(g.get("chatroom_id") or "").casefold()
        ]
    sort = str(sort or "activity").strip().lower()
    if sort == "name":
        groups.sort(key=lambda g: (
            str(g.get("name") or g.get("chatroom_id") or "").casefold(),
            str(g.get("chatroom_id") or "")))
    elif sort == "members":
        groups.sort(key=lambda g: (
            -max(_int(g.get("member_count")), _int(g.get("local_members"))),
            str(g.get("name") or "").casefold(),
            str(g.get("chatroom_id") or "")))
    else:
        sort = "activity"
        groups.sort(key=lambda g: (
            -(_int(g.get("inbound")) + _int(g.get("outbound"))),
            str(g.get("chatroom_id") or "")))

    limit = max(1, min(_int(limit, 50), 200))
    offset = max(0, min(_int(offset, 0), 5000))
    total = len(groups)
    page = groups[offset:offset + limit]
    return {
        "db_available": True,
        "since_hours": data["since_hours"],
        "q": str(q or ""),
        "sort": sort,
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_more": offset + len(page) < total,
        "truncated": offset + len(page) < total,
        "totals": data["totals"],
        "groups": page,
    }


def get_group_detail(
    account_id: str, group_id: str, *, since_hours: int = 24,
    now: float | None = None,
) -> dict[str, Any]:
    """单群详情：档案 + 本地成员数 + 窗口内收发 + 最后消息时间。"""
    gid = str(group_id or "").strip()
    since_hours = max(1, min(_int(since_hours, 24), 24 * 30))
    since_sec = int(time.time() if now is None else now) - since_hours * 3600
    conn = db.get_conn()
    try:
        row = conn.query_one(
            "SELECT chatroom_id, nickname, remark, owner_wxid, member_count, "
            "last_synced_at FROM wpp_chatrooms "
            "WHERE account_id = %s AND chatroom_id = %s LIMIT 1",
            (account_id, gid),
        ) or {}
        local = conn.query_one(
            "SELECT COUNT(*) AS n FROM wpp_chatroom_members "
            "WHERE account_id = %s AND chatroom_id = %s",
            (account_id, gid),
        ) or {}
        share = conn.query(
            "SELECT direction, COUNT(*) AS n FROM wpp_messages "
            "WHERE account_id = %s AND peer_kind = 'group' "
            "AND COALESCE(NULLIF(chat_id, ''), peer_id) = %s "
            "AND ts >= FROM_UNIXTIME(%s) GROUP BY direction",
            (account_id, gid, since_sec),
        )
        last = conn.query_one(
            "SELECT UNIX_TIMESTAMP(MAX(ts)) AS last_at FROM wpp_messages "
            "WHERE account_id = %s AND peer_kind = 'group' "
            "AND COALESCE(NULLIF(chat_id, ''), peer_id) = %s",
            (account_id, gid),
        ) or {}
    finally:
        db.close()

    inbound = outbound = 0
    for item in share:
        if str(item.get("direction")) == "inbound":
            inbound = _int(item.get("n"))
        elif str(item.get("direction")) == "outbound":
            outbound = _int(item.get("n"))
    last_at = last.get("last_at")
    return {
        "db_available": True,
        "since_hours": since_hours,
        "found": bool(row),
        "group": {
            "chatroom_id": gid,
            "name": str(row.get("nickname") or row.get("remark") or ""),
            "nickname": str(row.get("nickname") or ""),
            "remark": str(row.get("remark") or ""),
            "owner_wxid": str(row.get("owner_wxid") or ""),
            "member_count": (int(row["member_count"])
                             if row.get("member_count") is not None else None),
            "local_members": _int(local.get("n")),
            "inbound": inbound,
            "outbound": outbound,
            "last_synced_at": _iso(row.get("last_synced_at")),
            "last_message_at": None if last_at is None else int(last_at),
        },
    }


def list_group_members_page(
    account_id: str, group_id: str, *, q: str = "", limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """群成员搜索 + 分页（``wpp_chatroom_members`` 单表）。"""
    limit = max(1, min(_int(limit, 50), 100))
    offset = max(0, min(_int(offset, 0), 5000))
    needle = str(q or "").strip()
    where = "account_id = %s AND chatroom_id = %s"
    params: list[Any] = [account_id, group_id]
    if needle:
        where += " AND (wxid LIKE %s OR nickname LIKE %s)"
        like = f"%{needle}%"
        params.extend([like, like])
    conn = db.get_conn()
    try:
        total_row = conn.query_one(
            f"SELECT COUNT(*) AS n FROM wpp_chatroom_members WHERE {where}",
            tuple(params),
        ) or {}
        rows = conn.query(
            "SELECT wxid, nickname, is_owner FROM wpp_chatroom_members "
            f"WHERE {where} ORDER BY is_owner DESC, nickname, wxid LIMIT %s OFFSET %s",
            (*params, limit, offset),
        )
    finally:
        db.close()
    total = _int(total_row.get("n"))
    members = [{
        "wxid": str(r.get("wxid") or ""),
        "nickname": str(r.get("nickname") or ""),
        "is_owner": bool(r.get("is_owner")),
    } for r in rows]
    return {
        "db_available": True,
        "group_id": str(group_id),
        "q": needle,
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_more": offset + len(members) < total,
        "members": members,
    }


# --------------------------------------------------------------------------- 消息


def search_messages_page(
    account_id: str,
    *,
    q: str = "",
    group_id: str = "",
    sender: str = "",
    direction: str = "",
    since_hours: int = 168,
    limit: int = 50,
    offset: int = 0,
    now: float | None = None,
) -> dict[str, Any]:
    """消息正文检索（两条同 WHERE 单表查询：COUNT + 列表）。"""
    limit = max(1, min(_int(limit, 50), 50))
    offset = max(0, min(_int(offset, 0), 5000))
    since_hours = max(1, min(_int(since_hours, 168), 720))
    since_sec = int(time.time() if now is None else now) - since_hours * 3600

    where = ["account_id = %s", "ts >= FROM_UNIXTIME(%s)"]
    params: list[Any] = [account_id, since_sec]
    gid = str(group_id or "").strip()
    if gid:
        where.append("peer_kind = 'group'")
        where.append("COALESCE(NULLIF(chat_id, ''), peer_id) = %s")
        params.append(gid)
    keyword = str(q or "").strip()
    if keyword:
        where.append("content LIKE %s")
        params.append(f"%{keyword}%")
    sender_text = str(sender or "").strip()
    if sender_text:
        where.append("from_wxid LIKE %s")
        params.append(f"%{sender_text}%")
    direction = str(direction or "").strip().lower()
    if direction in ("inbound", "outbound"):
        where.append("direction = %s")
        params.append(direction)
    clause = " AND ".join(where)

    conn = db.get_conn()
    try:
        total_row = conn.query_one(
            f"SELECT COUNT(*) AS n FROM wpp_messages WHERE {clause}",
            tuple(params),
        ) or {}
        rows = conn.query(
            "SELECT id, msg_id, new_msg_id, direction, peer_kind, peer_id, chat_id, "
            "peer_name, from_wxid, msg_type, "
            "SUBSTR(COALESCE(content, ''), 1, 4000) AS content, "
            "UNIX_TIMESTAMP(ts) AS at_sec "
            f"FROM wpp_messages WHERE {clause} "
            "ORDER BY ts DESC, id DESC LIMIT %s OFFSET %s",
            (*params, limit, offset),
        )
    finally:
        db.close()

    total = _int(total_row.get("n"))
    messages = [{
        "id": _int(r.get("id")),
        "msg_id": str(r.get("msg_id") or ""),
        "new_msg_id": str(r.get("new_msg_id") or ""),
        "direction": str(r.get("direction") or ""),
        "peer_kind": str(r.get("peer_kind") or ""),
        "peer_id": str(r.get("peer_id") or ""),
        "chat_id": str(r.get("chat_id") or ""),
        "peer_name": str(r.get("peer_name") or ""),
        "from_wxid": str(r.get("from_wxid") or ""),
        "msg_type": "" if r.get("msg_type") is None else str(r.get("msg_type")),
        "content": str(r.get("content") or ""),
        "at_sec": None if r.get("at_sec") is None else int(r["at_sec"]),
    } for r in rows]
    return {
        "db_available": True,
        "since_hours": since_hours,
        "q": keyword,
        "group_id": gid,
        "sender": sender_text,
        "direction": direction,
        "limit": limit,
        "offset": offset,
        "total": total,
        "has_more": offset + len(messages) < total,
        "messages": messages,
    }


def message_analytics(
    account_id: str, *, hours: int = 24, now: float | None = None,
) -> dict[str, Any]:
    """收发趋势 + 类型分布（小时桶 ≤48h，否则天桶）。"""
    hours = max(1, min(_int(hours, 24), 168))
    stamp = int(time.time() if now is None else now)
    key = (str(account_id), hours)
    with _analytics_lock:
        hit = _analytics_cache.get(key)
        if hit is not None and stamp - hit[0] < 10:
            return hit[1]

    since_sec = stamp - hours * 3600
    offset = _local_offset_sec(since_sec)
    bucket_sec = 3600 if hours <= 48 else 86400
    conn = db.get_conn()
    try:
        rows = conn.query(
            "SELECT (UNIX_TIMESTAMP(ts) + %s) - "
            "((UNIX_TIMESTAMP(ts) + %s) %% %s) AS bucket, direction, COUNT(*) AS n "
            "FROM wpp_messages WHERE account_id = %s AND ts >= FROM_UNIXTIME(%s) "
            "GROUP BY bucket, direction",
            (offset, offset, bucket_sec, account_id, since_sec),
        )
        type_rows = conn.query(
            "SELECT msg_type, direction, COUNT(*) AS n FROM wpp_messages "
            "WHERE account_id = %s AND ts >= FROM_UNIXTIME(%s) "
            "GROUP BY msg_type, direction",
            (account_id, since_sec),
        )
        last = conn.query_one(
            "SELECT UNIX_TIMESTAMP(MAX(ts)) AS last_at FROM wpp_messages "
            "WHERE account_id = %s",
            (account_id,),
        ) or {}
    finally:
        db.close()

    bucket_counts: dict[int, dict[str, int]] = {}
    totals = {"inbound": 0, "outbound": 0}
    for r in rows:
        bucket = _int(r.get("bucket"))
        direction = str(r.get("direction") or "")
        if direction not in totals:
            continue
        bucket_counts.setdefault(bucket, {"inbound": 0, "outbound": 0})[direction] += _int(r.get("n"))
        totals[direction] += _int(r.get("n"))

    count = hours if bucket_sec == 3600 else max(1, (hours + 23) // 24)
    now_shift = stamp + offset
    end_shift = now_shift - now_shift % bucket_sec
    start_shift = end_shift - (count - 1) * bucket_sec
    buckets: list[dict[str, Any]] = []
    for i in range(count):
        shifted = start_shift + i * bucket_sec
        item = bucket_counts.get(shifted, {"inbound": 0, "outbound": 0})
        buckets.append({
            "start_sec": shifted - offset,
            "inbound": item["inbound"],
            "outbound": item["outbound"],
        })

    types: dict[tuple[str, str], int] = {}
    for r in type_rows:
        msg_type = "unknown" if r.get("msg_type") is None else str(r.get("msg_type"))
        direction = str(r.get("direction") or "unknown")
        types[(msg_type, direction)] = types.get((msg_type, direction), 0) + _int(r.get("n"))
    type_list = [
        {"msg_type": mt, "direction": d, "count": n}
        for (mt, d), n in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    last_at = last.get("last_at")
    result = {
        "db_available": True,
        "hours": hours,
        "bucket_sec": bucket_sec,
        "buckets": buckets,
        "types": type_list,
        "totals": totals,
        "last_message_at": None if last_at is None else int(last_at),
    }
    with _analytics_lock:
        _analytics_cache[key] = (stamp, result)
        if len(_analytics_cache) > 32:
            oldest = sorted(_analytics_cache.items(), key=lambda kv: kv[1][0])[:8]
            for old_key, _ in oldest:
                _analytics_cache.pop(old_key, None)
    return result


# --------------------------------------------------------------------------- 心流


def heartflow_overview(
    account_id: str, *, hours: int = 24, now: float | None = None,
) -> dict[str, Any]:
    """心流配置之外的全部运营只读数据（统计/预算/群状态/最近判定）。"""
    hours = max(1, min(_int(hours, 24), 168))
    stamp = int(time.time() if now is None else now)
    since_sec = stamp - hours * 3600
    conn = db.get_conn()
    try:
        stats = repo_hf.count_by_status(account_id, since_sec, conn=conn)
        budget = repo_hf.list_sent_counts_recent(
            account_id, _local_hour_start(stamp), _local_day_start(stamp), conn=conn)
        states = repo_hf.list_group_states(account_id, conn=conn)
        recent = repo_hf.list_recent_judged(account_id, 50, conn=conn)
        last_sent = repo_hf.get_last_sent_at(account_id, conn=conn)
    finally:
        db.close()
    return {
        "db_available": True,
        "hours": hours,
        "stats": stats,
        "last_sent_at": last_sent,
        "budget": budget,
        "group_states": states,
        "recent": recent,
    }


# --------------------------------------------------------------------------- 风格卡


def _parse_style(text: Any) -> tuple[Any, bool]:
    """坏 JSON ⇒ ``(None, False)``；对象/数组原样返回。"""
    if text is None or text == "":
        return None, False
    if not isinstance(text, str):
        return text, True
    try:
        return json.loads(text), True
    except (ValueError, TypeError):
        return None, False


def _style_summary(row: dict[str, Any]) -> dict[str, Any]:
    style, valid = _parse_style(row.get("style_json"))
    keys = sorted(style) if isinstance(style, dict) else []
    return {
        "group_id": str(row.get("group_id") or ""),
        "version": _int(row.get("version"), 1),
        "confidence": _num(row.get("confidence")),
        "sample_msgs": _int(row.get("sample_msgs")),
        "sender_count": _int(row.get("sender_count")),
        "model": str(row.get("model") or ""),
        "generated_at": (None if row.get("generated_at") is None
                         else int(row["generated_at"])),
        "style_valid": valid,
        "style_keys": keys,
    }


def style_cards_view(
    account_id: str, *, group_id: str = "",
) -> dict[str, Any]:
    """风格卡列表（摘要；完整 JSON 走版本详情接口）。"""
    conn = db.get_conn()
    try:
        cards = repo_style.list_cards(account_id, conn=conn)
    finally:
        db.close()
    gid = str(group_id or "").strip()
    items = [_style_summary(c) for c in cards if not gid or str(c.get("group_id")) == gid]
    return {"db_available": True, "group_id": gid, "cards": items}


def style_card_versions(
    account_id: str, group_id: str, *, limit: int = 20,
) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        versions = repo_style.list_versions(
            account_id, group_id, max(1, min(_int(limit, 20), 100)), conn=conn)
    finally:
        db.close()
    return {"db_available": True, "group_id": str(group_id), "versions": versions}


def style_card_version(
    account_id: str, group_id: str, version: int,
) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        row = repo_style.get_version(account_id, group_id, int(version), conn=conn)
    finally:
        db.close()
    if row is None:
        return {"db_available": True, "found": False, "version": int(version)}
    raw = row.get("style_json")
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    truncated = len(text) > 32768
    style, valid = _parse_style(raw)
    return {
        "db_available": True,
        "found": True,
        "group_id": str(row.get("group_id") or ""),
        "version": _int(row.get("version")),
        "confidence": _num(row.get("confidence")),
        "sample_msgs": _int(row.get("sample_msgs")),
        "sender_count": _int(row.get("sender_count")),
        "model": str(row.get("model") or ""),
        "reason": str(row.get("reason") or ""),
        "generated_at": (None if row.get("generated_at") is None
                         else int(row["generated_at"])),
        "style_valid": valid,
        "style_truncated": truncated,
        "style": None if truncated else style,
        "style_head": text[:32000] if truncated else None,
    }


# --------------------------------------------------------------------------- 好感度


def _affection_payload(
    account_id: str, group_id: str, *, conn: Any, limit: int = 100,
    now: int | None = None,
) -> dict[str, Any]:
    stamp = int(time.time() if now is None else now)
    users = repo_affection.list_top_user_levels(
        account_id, group_id, limit, conn=conn)
    members = conn.query(
        "SELECT wxid, nickname FROM wpp_chatroom_members "
        "WHERE account_id = %s AND chatroom_id = %s LIMIT 2000",
        (account_id, group_id),
    )
    nick = {str(r.get("wxid") or ""): str(r.get("nickname") or "") for r in members}
    mood = repo_affection.get_mood_row(account_id, group_id, conn=conn)
    mood_view = None
    if mood and mood.get("mood_type"):
        expires_at = _int(mood.get("expires_at"))
        mood_view = {
            "mood_type": str(mood.get("mood_type") or ""),
            "intensity": _num(mood.get("intensity"), 0.5),
            "description": str(mood.get("description") or ""),
            "expires_at": expires_at,
            "expired": bool(expires_at and stamp > expires_at),
        }
    return {
        "group_id": str(group_id),
        "total": repo_affection.get_group_total(account_id, group_id, conn=conn),
        "user_count": repo_affection.count_users(account_id, group_id, conn=conn),
        "users": [{
            "wxid": wxid,
            "nickname": nick.get(wxid, ""),
            "level": level,
        } for wxid, level in users],
        "mood": mood_view,
    }


def affection_view(
    account_id: str, group_id: str, *, limit: int = 100, now: int | None = None,
) -> dict[str, Any]:
    """群好感度榜 + 情绪行（**只读**，不触发过期重置写库）。"""
    limit = max(1, min(_int(limit, 100), 100))
    conn = db.get_conn()
    try:
        payload = _affection_payload(
            account_id, group_id, conn=conn, limit=limit, now=now)
    finally:
        db.close()
    return {"db_available": True, **payload}


# --------------------------------------------------------------------------- 群洞察


def group_insights(
    account_id: str, group_id: str, *, now: int | None = None,
) -> dict[str, Any]:
    """群详情抽屉的只读摘要：心流状态/最近台账 + 风格卡 + 好感度 Top 10。"""
    stamp = int(time.time() if now is None else now)
    gid = str(group_id or "").strip()
    conn = db.get_conn()
    try:
        state = repo_hf.get_group_state(account_id, gid, conn=conn)
        ledger = repo_hf.get_ledger_last(account_id, gid, conn=conn)
        audit = repo_hf.list_threshold_audit(account_id, gid, 10, conn=conn)
        card = repo_style.get_card(account_id, gid, conn=conn)
        affection = _affection_payload(account_id, gid, conn=conn, limit=10, now=stamp)
    finally:
        db.close()
    if ledger is not None:
        ledger = {
            "status": str(ledger.get("status") or ""),
            "suppressed_reason": ledger.get("suppressed_reason"),
            "judge_overall": (None if ledger.get("judge_overall") is None
                              else float(ledger["judge_overall"])),
            "effective_threshold": (None if ledger.get("effective_threshold") is None
                                    else float(ledger["effective_threshold"])),
            "content_head": str(ledger.get("content_head") or ""),
            "judged_at": (None if ledger.get("judged_at") is None
                          else int(ledger["judged_at"])),
            "sent_at": (None if ledger.get("sent_at") is None
                        else int(ledger["sent_at"])),
        }
    return {
        "db_available": True,
        "group_id": gid,
        "heartflow": {
            "state": state,
            "last_ledger": ledger,
            "threshold_audit": audit,
        },
        "style": _style_summary(card) if card else None,
        "affection": affection,
    }


def clear_cache() -> None:
    """测试 / 手动刷新用。"""
    with _cache_lock:
        _cache.clear()
    with _analytics_lock:
        _analytics_cache.clear()
