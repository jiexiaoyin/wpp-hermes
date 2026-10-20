"""AFFECTION 好感度 / 情绪状态的 SQL 原语（``wpp_affection`` 表）。

对齐 TS ``src/inbound/affection.ts`` 的**状态层**，但换了载体：TS 把状态放在模块级
``Map``（``affection.ts:489`` 的 ``groupStates``），进程一重启就清零；老板 2026-10-02
拍板**落库持久化**。算法那一半在 :mod:`plugin.inbound.affection`，这里**只写 SQL**。

## 一张表两种粒度（靠 ``user_id`` 分）

```
(account_id, group_id, user_id)   user_id != '' → 该用户好感度分值 (level, updated_at)
(account_id, group_id, user_id='')               → 该群情绪       (mood_type…expires_at)
```

为什么挤在一张表：两者**生命期与访问模式完全一样**（都是"每群一条 / 每人一条、读改写"），
分两张表只会让"这个群现在什么状态"变成两次查询。

为什么**只用哨兵、不再加一个 ``kind`` 列**：哨兵与 ``kind`` 是同一个事实的两种编码，
同时留着就能造出自相矛盾的行（``kind='level'`` 而 ``user_id=''``），还得在每个查询里
决定信哪个。哨兵够用的前提是**wxid / 群 id 都非空** —— vendor 给的就是这个形状，
而 ``user_id`` 全表只有本模块写。

⚠️ 因此**凡是要"所有用户"的地方都必须显式排除哨兵行**（``user_id <> ''``）——
少写这个条件，情绪行会被当成一个 level=0 的用户算进群总量与人数里。

⚠️ **情绪行不能省**：一个群只要有过一次情绪变化就有情绪行；没有行 = 还没发生过，
读的时候返回 :func:`plugin.inbound.affection.default_mood`（平静）。

## 写失败一律抛出去，由调用方吞

本模块**不吞异常**（与 :mod:`repo_hf` 同款）。调用方 ``process_affection_message``
跑在 ``asyncio.create_task`` 的 fire-and-forget 里，异常在那里被记成一行 warn ——
**好感度记账失败绝不许影响入站链路**（TS 是 ``void processAffectionMessage``）。

⚠️ **绝不回写 accounts JSON**：这是高频写（每条群消息一次），照 ``repo_hf.upsert_group_state``
的先例（``repo_hf.py:487`` 注释"高频写库，绝不回写 accounts JSON —— 防 fs.watch 抖动"）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import db

logger = logging.getLogger(__name__)

#: 情绪行用的 ``user_id`` 哨兵（wxid / 群 id 都非空，空串不会撞人）
MOOD_ROW_USER_ID = ""


# --------------------------------------------------------------------------- 分值：读


def get_user_level(account_id: str, group_id: str, user_id: str, *,
                   conn: db.Connection | None = None) -> int:
    """该用户在本群的好感度。没有行 ⇒ 0（不是"未知"）。"""
    c = conn or db.get_conn()
    v = c.query_value(
        "SELECT level FROM wpp_affection "
        "WHERE account_id = %s AND group_id = %s AND user_id = %s",
        (account_id, group_id, user_id),
        default=0,
    )
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def list_user_levels(account_id: str, group_id: str, *,
                     conn: db.Connection | None = None) -> list[tuple[str, int]]:
    """本群所有用户的**分值**（重分配要用全量，**不能**只取 top N）。

    ⚠️ ``user_id <> ''`` 是排除情绪行 —— 它在同一张表里。
    """
    c = conn or db.get_conn()
    rows = c.query(
        "SELECT user_id, level FROM wpp_affection "
        "WHERE account_id = %s AND group_id = %s AND user_id <> '' "
        "ORDER BY level DESC, user_id",
        (account_id, group_id),
    )
    out: list[tuple[str, int]] = []
    for r in rows:
        try:
            out.append((str(r.get("user_id") or ""), int(r.get("level") or 0)))
        except (TypeError, ValueError):
            continue
    return out


def list_top_user_levels(
    account_id: str, group_id: str, limit: int = 100, *,
    conn: db.Connection | None = None,
) -> list[tuple[str, int]]:
    """Top N 用户分值（控制台好感度榜用；**只读**、SQL 侧就截断）。"""
    c = conn or db.get_conn()
    n = max(1, min(int(limit) if isinstance(limit, int) else 100, 500))
    rows = c.query(
        "SELECT user_id, level FROM wpp_affection "
        "WHERE account_id = %s AND group_id = %s AND user_id <> '' "
        "ORDER BY level DESC, user_id LIMIT %s",
        (account_id, group_id, n),
    )
    out: list[tuple[str, int]] = []
    for r in rows:
        try:
            out.append((str(r.get("user_id") or ""), int(r.get("level") or 0)))
        except (TypeError, ValueError):
            continue
    return out


def get_mood_row(
    account_id: str, group_id: str, *, conn: db.Connection | None = None,
) -> dict | None:
    """**只读**当前情绪行（不做过期重置 —— 那会写库，控制台不该改状态）。

    返回 ``mood_type / intensity / description / expires_at / updated_at``；
    没有行 ⇒ ``None``（由调用方决定展示"无"还是平静）。
    """
    c = conn or db.get_conn()
    return c.query_one(
        "SELECT mood_type, intensity, description, expires_at, updated_at "
        "FROM wpp_affection WHERE account_id = %s AND group_id = %s AND user_id = %s",
        (account_id, group_id, MOOD_ROW_USER_ID),
    )


def get_group_total(account_id: str, group_id: str, *,
                    conn: db.Connection | None = None) -> int:
    """群总好感度。**负分按 0 计**（对齐 TS ``getGroupTotalAffection`` 的 ``Math.max(0, l)``）
    —— 否则一个被骂惨的用户能把全群总量拉下来、让重分配永远不触发。"""
    c = conn or db.get_conn()
    v = c.query_value(
        "SELECT COALESCE(SUM(GREATEST(level, 0)), 0) AS total FROM wpp_affection "
        "WHERE account_id = %s AND group_id = %s AND user_id <> ''",
        (account_id, group_id),
        default=0,
    )
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def count_users(account_id: str, group_id: str, *,
                conn: db.Connection | None = None) -> int:
    """本群有分值行的人数（``/affection status`` 报的那个数）。"""
    c = conn or db.get_conn()
    v = c.query_value(
        "SELECT COUNT(*) AS n FROM wpp_affection "
        "WHERE account_id = %s AND group_id = %s AND user_id <> ''",
        (account_id, group_id),
        default=0,
    )
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def count_groups_with_state(account_id: str, *,
                            conn: db.Connection | None = None) -> int:
    """该账号有状态的群数（``/affection status`` 用）。"""
    c = conn or db.get_conn()
    v = c.query_value(
        "SELECT COUNT(DISTINCT group_id) AS n FROM wpp_affection WHERE account_id = %s",
        (account_id,),
        default=0,
    )
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def count_level_rows(account_id: str, *,
                     conn: db.Connection | None = None) -> int:
    """该账号**全账号**的分值行数（``/affection status`` 用；``user_id <> ''`` 排掉情绪行）。

    与 :func:`count_users` 的区别：那个按群数人，这个跨群数行 —— 状态命令问的是
    "落库到底有没有在写"，需要一个与具体群无关的总量。
    """
    c = conn or db.get_conn()
    v = c.query_value(
        "SELECT COUNT(*) AS n FROM wpp_affection "
        "WHERE account_id = %s AND user_id <> ''",
        (account_id,),
        default=0,
    )
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- 分值：写


def set_user_level(account_id: str, group_id: str, user_id: str, level: int,
                   updated_at: int, *, conn: db.Connection | None = None) -> int:
    """upsert 分值。``level`` 由调用方保证已 clamp 到 ``>= 0``。"""
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_affection
          (account_id, group_id, user_id, level, updated_at)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE level = VALUES(level), updated_at = VALUES(updated_at)
        """,
        (account_id, group_id, user_id, int(level), int(updated_at)),
    )


# --------------------------------------------------------------------------- 情绪


def get_mood(account_id: str, group_id: str, now_sec: int, *,
             conn: db.Connection | None = None) -> Any:
    """当前情绪。**过期即重置为平静并落库**（对齐 TS ``getGroupMood`` 的过期重置）。

    ⚠️ 过期重置要**写回库**：只返回默认值不写，下一次读又从头算一次过期，而"刚才已经
    判过它过期了"这件事没人记得。TS 的 ``st.mood = defaultMood(now)`` 就是就地改掉，
    这里等价，只是载体是库。
    """
    from ..inbound.affection import MOOD_DESCRIPTIONS, MoodState, default_mood

    c = conn or db.get_conn()
    row = c.query_one(
        "SELECT mood_type, intensity, description, expires_at FROM wpp_affection "
        "WHERE account_id = %s AND group_id = %s AND user_id = %s",
        (account_id, group_id, MOOD_ROW_USER_ID),
    )
    if not row or not row.get("mood_type"):
        return default_mood(now_sec)

    mood_type = str(row.get("mood_type") or "")
    if mood_type not in MOOD_DESCRIPTIONS:
        # 脏值（手工改库 / 未来版本改了枚举）：当没有，别让下游按未知情绪拼 prompt
        logger.warning("[WPP AFFECTION] 情绪值不认识 %r (account=%s, group=%s)，重置为平静",
                       mood_type, account_id, group_id)
        return default_mood(now_sec)

    expires_at = _as_int(row.get("expires_at"), 0)
    if now_sec > expires_at:
        fresh = default_mood(now_sec)
        set_mood(account_id, group_id, fresh, conn=conn)
        return fresh

    return MoodState(
        mood_type,
        _as_float(row.get("intensity"), 0.5),
        str(row.get("description") or MOOD_DESCRIPTIONS[mood_type]),
        expires_at,
    )


def set_mood(account_id: str, group_id: str, mood: Any, *,
             conn: db.Connection | None = None) -> int:
    """upsert 情绪（``mood.expires_at`` 是**绝对 epoch 秒**）。"""
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_affection
          (account_id, group_id, user_id, mood_type, intensity, description,
           expires_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE mood_type = VALUES(mood_type), intensity = VALUES(intensity),
          description = VALUES(description), expires_at = VALUES(expires_at),
          updated_at = VALUES(updated_at)
        """,
        # ``updated_at`` 是"这次写入的时刻"，不是情绪到期时刻 —— 旧实现两列
        # 写同一个值，于是"最近一次情绪变化"永远等于"到期时间"（读侧按它排序/判新鲜
        # 时全错，而且不报错）。
        (account_id, group_id, MOOD_ROW_USER_ID, str(mood.mood_type),
         float(mood.intensity), str(mood.description), int(mood.expires_at),
         int(time.time())),
    )


# --------------------------------------------------------------------------- 小工具


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
