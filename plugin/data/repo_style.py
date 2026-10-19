"""说话风格卡（``wpp_style_cards`` / ``wpp_style_card_versions``）的 SQL 原语。

**只写 SQL** —— 怎么采样、怎么问 LLM、怎么校验/截断、怎么注入 prompt，全在
``inbound/style_learn.py``。

## 版本语义（只增不减）

- 每次生成成功：``version + 1``，同时往 ``wpp_style_card_versions`` 插一版。
- 每次回滚：把旧版内容**复制成新版本**（``version`` 仍是当前 +1），历史留
  ``reason='rollback'``。
- 版本号绝不回退：回滚后 ``/style list`` 看到的是「v7 = 回滚到 v3 的内容」，
  而不是把当前版号改回 3 —— 后者会让审计里两条不同内容都叫 v3。

## 坏 JSON 的处理

``style_json`` 原样返回、不在这里解析：批量预热要的是"把坏行如实交给调用方"，
在 SQL 层吞掉坏行等于把"这张卡坏了"这个事实藏起来（与 ``repo_hf.list_profiles``
同一条纪律）。解析在 ``style_learn.parse_style_row``。
"""

from __future__ import annotations

import json
from typing import Any

from . import db


def _json_text(value: Any) -> str:
    """``str`` 原样返回（回滚时从历史行搬过来，不该二次编码）；其余按 JSON 编码。"""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _confidence(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    return max(0.0, min(1.0, f))


def list_cards(account_id: str, conn: db.Connection | None = None) -> list[dict]:
    """该账号全部风格卡（含原始 JSON），``ORDER BY group_id``。预热/巡检用。"""
    c = conn or db.get_conn()
    rows = c.query(
        "SELECT account_id, group_id, style_json, sample_msgs, sender_count, "
        "confidence, model, version, generated_at FROM wpp_style_cards "
        "WHERE account_id = %s ORDER BY group_id",
        (account_id,),
    )
    for r in rows:
        r["version"] = int(r.get("version") or 1)
        r["sample_msgs"] = int(r.get("sample_msgs") or 0)
        r["sender_count"] = int(r.get("sender_count") or 0)
        r["confidence"] = _confidence(r.get("confidence"))
        r["generated_at"] = (None if r.get("generated_at") is None
                             else int(r["generated_at"]))
    return rows


def get_card(account_id: str, group_id: str,
             conn: db.Connection | None = None) -> dict | None:
    c = conn or db.get_conn()
    row = c.query_one(
        "SELECT account_id, group_id, style_json, sample_msgs, sender_count, "
        "confidence, model, version, generated_at FROM wpp_style_cards "
        "WHERE account_id = %s AND group_id = %s",
        (account_id, group_id),
    )
    if row is None:
        return None
    row["version"] = int(row.get("version") or 1)
    row["sample_msgs"] = int(row.get("sample_msgs") or 0)
    row["sender_count"] = int(row.get("sender_count") or 0)
    row["confidence"] = _confidence(row.get("confidence"))
    row["generated_at"] = (None if row.get("generated_at") is None
                           else int(row["generated_at"]))
    return row


def save_card(
    account_id: str, group_id: str, style: Any, *,
    sample_msgs: int = 0,
    sender_count: int = 0,
    confidence: float = 0.0,
    model: str | None = None,
    generated_at: int | None = None,
    reason: str = "generated",
    conn: db.Connection | None = None,
) -> int:
    """写当前卡 + 版本历史，返回**新版本号**。

    单事务完成两表写：先 ``SELECT ... FOR UPDATE`` 取当前版本，再 upsert 主表、
    insert 历史。并发调用（两个 sweep 任务同时跑到同一个群）时后到者会在行锁上
    等待，拿到的是前者提交后的版本，不会出现两版同号。
    """
    c = conn or db.get_conn()
    text = _json_text(style)
    conf = _confidence(confidence)
    with c.transaction():
        row = c.query_one(
            "SELECT version FROM wpp_style_cards "
            "WHERE account_id = %s AND group_id = %s FOR UPDATE",
            (account_id, group_id),
        )
        version = int((row or {}).get("version") or 0) + 1
        c.execute(
            """
            INSERT INTO wpp_style_cards
              (account_id, group_id, style_json, sample_msgs, sender_count,
               confidence, model, version, generated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              style_json = VALUES(style_json),
              sample_msgs = VALUES(sample_msgs),
              sender_count = VALUES(sender_count),
              confidence = VALUES(confidence),
              model = VALUES(model),
              version = VALUES(version),
              generated_at = VALUES(generated_at)
            """,
            (account_id, group_id, text, int(sample_msgs), int(sender_count),
             conf, model, version, generated_at),
        )
        c.execute(
            """
            INSERT INTO wpp_style_card_versions
              (account_id, group_id, version, style_json, sample_msgs,
               sender_count, confidence, model, reason, generated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              style_json = VALUES(style_json),
              sample_msgs = VALUES(sample_msgs),
              sender_count = VALUES(sender_count),
              confidence = VALUES(confidence),
              model = VALUES(model),
              reason = VALUES(reason),
              generated_at = VALUES(generated_at)
            """,
            (account_id, group_id, version, text, int(sample_msgs),
             int(sender_count), conf, model, str(reason)[:16], generated_at),
        )
    return version


def list_versions(
    account_id: str, group_id: str, limit: int = 10,
    conn: db.Connection | None = None,
) -> list[dict]:
    """版本历史（新→旧），不含 ``style_json`` —— 列表只给人看元数据。"""
    c = conn or db.get_conn()
    n = max(1, min(int(limit) if isinstance(limit, int) else 10, 100))
    rows = c.query(
        "SELECT version, sample_msgs, sender_count, confidence, model, reason, "
        "generated_at, created_at FROM wpp_style_card_versions "
        "WHERE account_id = %s AND group_id = %s "
        "ORDER BY version DESC LIMIT %s",
        (account_id, group_id, n),
    )
    for r in rows:
        r["version"] = int(r.get("version") or 0)
        r["confidence"] = _confidence(r.get("confidence"))
        r["sample_msgs"] = int(r.get("sample_msgs") or 0)
        r["sender_count"] = int(r.get("sender_count") or 0)
    return rows


def get_version(
    account_id: str, group_id: str, version: int,
    conn: db.Connection | None = None,
) -> dict | None:
    c = conn or db.get_conn()
    row = c.query_one(
        "SELECT account_id, group_id, version, style_json, sample_msgs, "
        "sender_count, confidence, model, reason, generated_at "
        "FROM wpp_style_card_versions "
        "WHERE account_id = %s AND group_id = %s AND version = %s",
        (account_id, group_id, int(version)),
    )
    if row is None:
        return None
    row["version"] = int(row.get("version") or 0)
    row["confidence"] = _confidence(row.get("confidence"))
    row["sample_msgs"] = int(row.get("sample_msgs") or 0)
    row["sender_count"] = int(row.get("sender_count") or 0)
    return row


def rollback_card(
    account_id: str, group_id: str, version: int,
    conn: db.Connection | None = None,
) -> int | None:
    """把历史 ``version`` 的内容复制成新版本；目标不存在返回 ``None``。"""
    target = get_version(account_id, group_id, version, conn=conn)
    if target is None:
        return None
    return save_card(
        account_id, group_id, target.get("style_json") or "{}",
        sample_msgs=target.get("sample_msgs") or 0,
        sender_count=target.get("sender_count") or 0,
        confidence=target.get("confidence") or 0.0,
        model=target.get("model"),
        generated_at=target.get("generated_at"),
        reason="rollback",
        conn=conn,
    )


def count_cards(account_id: str, conn: db.Connection | None = None) -> int:
    c = conn or db.get_conn()
    return int(c.query_value(
        "SELECT COUNT(*) FROM wpp_style_cards WHERE account_id = %s",
        (account_id,), default=0,
    ) or 0)
