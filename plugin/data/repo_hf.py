"""心流（heartflow）五张表的 SQL 原语。

对齐 TS ``src/storage/db/mysql.ts`` 的 ``wpp_hf_*`` 读写。**只写 SQL** ——
阈值怎么调、样本算不算数、预算怎么扣，全在 ``inbound/heartflow/`` 的 9 个模块里。

## 状态机（五条 guard 是这套表最值钱的部分，照搬别改）

```
                  ┌── set_sent（judged 或 suppressed+sent_at IS NULL）
   judged ────────┤
     │            └── set_suppressed（仅 judged）
     │
     └─ expire_stale_judged（judged 挂太久无投递结果 → suppressed/no-deliver-outcome）

   sent ──── mark_engaged(close=True) ────▶ closed
     │      close_expired_windows（到期没信号 → silence / short-window）
     └────── mark_veto（人工否决 → engaged=0, signal='veto', closed）
```

- **只有 ``judged`` 能推进到 ``suppressed``**（防 deliver 双调/竞态）。
- **``set_sent`` 额外放行 ``suppressed`` 且 ``sent_at IS NULL``**（v1.10.0）：
  同一条消息先判"不过"、后被重判为"过"并真发出去，那次真发原本落不了账 ——
  不占预算、不开窗、不进重复闸历史。
- **``mark_engaged(close=False)`` 只记信号、保持 ``sent``**，让晚到的引用/@ 还能升格；
  一旦关窗就再也升不上去。
- **``close=True`` 允许覆盖 ``short-window``（弱→强），但不许覆盖已有的强信号。**

## 尺度（别混）

- ``judge_overall`` / ``effective_threshold``：**0–1**
- ``dim_r/w/s/t/c``：**0–10**
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from . import db

logger = logging.getLogger(__name__)

#: 状态机的四个状态
STATUS_JUDGED = "judged"
STATUS_SENT = "sent"
STATUS_SUPPRESSED = "suppressed"
STATUS_CLOSED = "closed"

#: 弱信号名 —— 可被强信号覆盖（见 mark_engaged）
SIGNAL_SHORT_WINDOW = "short-window"


# --------------------------------------------------------------------------- 台账：写


def insert_judged(
    account_id: str, inbound_msg_id: str, group_id: str, judged_at: int, *,
    new_msg_id: str | None = None, from_wxid: str | None = None, msg_type: str | None = None,
    content_head: str | None = None, judge_overall: float | None = None,
    dim_r: float | None = None, dim_w: float | None = None, dim_s: float | None = None,
    dim_t: float | None = None, dim_c: float | None = None,
    effective_threshold: float | None = None, energy: float | None = None,
    conn: db.Connection | None = None,
) -> int:
    """落一条判定（``INSERT IGNORE``：同一入站消息只记一次）。"""
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT IGNORE INTO wpp_hf_ledger
          (account_id, inbound_msg_id, new_msg_id, group_id, from_wxid, msg_type, content_head,
           judge_overall, dim_r, dim_w, dim_s, dim_t, dim_c, effective_threshold, energy,
           status, judged_at, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'judged', %s, CURRENT_TIMESTAMP)
        """,
        (account_id, inbound_msg_id, new_msg_id, group_id, from_wxid, msg_type,
         (content_head or "")[:512] or None, judge_overall, dim_r, dim_w, dim_s, dim_t,
         dim_c, effective_threshold, energy, judged_at),
    )


def set_sent(
    account_id: str, inbound_msg_id: str, sent_at: int, window_expires_at: int,
    bot_msg_id: str | None = None, conn: db.Connection | None = None,
) -> int:
    """记"真的发出去了"。

    ★ guard 见模块 docstring：``judged`` 或 ``suppressed`` 且**从未发过**。
    已 ``closed`` 的样本不会被改回 ``sent``。
    """
    c = conn or db.get_conn()
    return c.execute(
        """
        UPDATE wpp_hf_ledger
        SET status = 'sent', sent_at = %s, window_expires_at = %s, bot_msg_id = %s,
            suppressed_reason = NULL
        WHERE account_id = %s AND inbound_msg_id = %s
          AND (status = 'judged' OR (status = 'suppressed' AND sent_at IS NULL))
        """,
        (sent_at, window_expires_at, bot_msg_id, account_id, inbound_msg_id),
    )


def set_suppressed(
    account_id: str, inbound_msg_id: str, reason: str, at_sec: int,
    conn: db.Connection | None = None,
) -> int:
    """记"判了但没发"。**只从 ``judged`` 推进**。"""
    c = conn or db.get_conn()
    return c.execute(
        """
        UPDATE wpp_hf_ledger
        SET status = 'suppressed', suppressed_reason = %s, closed_at = %s
        WHERE account_id = %s AND inbound_msg_id = %s AND status = 'judged'
        """,
        ((reason or "")[:96], at_sec, account_id, inbound_msg_id),
    )


def mark_engaged(
    account_id: str, group_id: str, at_sec: int, engaged: int, signal: str,
    close: bool, inbound_msg_id: str | None = None, conn: db.Connection | None = None,
) -> int:
    """按信号收敛。

    ``close=False``：只记 ``engaged=1`` + 信号，**保持 ``sent``**（还能被升格）。
    ``close=True``：落结论并关窗；允许覆盖 ``short-window``，不许覆盖已有强信号。

    ``inbound_msg_id`` 传了就**精确到行**：同群两条 sent 行重叠时，一句引用若
    给两行都记 ``engaged=1``，一条信号就被算了两遍（学习样本被污染）。
    """
    c = conn or db.get_conn()
    row_scope = " AND inbound_msg_id = %s" if inbound_msg_id else ""
    row_arg: Sequence[Any] = (inbound_msg_id,) if inbound_msg_id else ()

    if not close:
        return c.execute(
            f"""
            UPDATE wpp_hf_ledger
            SET engaged = 1, engage_signal = %s
            WHERE account_id = %s AND group_id = %s AND status = 'sent'
              AND engage_signal IS NULL{row_scope}
              AND window_expires_at IS NOT NULL AND window_expires_at > %s
            """,
            (signal, account_id, group_id, *row_arg, at_sec),
        )
    return c.execute(
        f"""
        UPDATE wpp_hf_ledger
        SET engaged = %s, engage_signal = %s, status = 'closed', closed_at = %s
        WHERE account_id = %s AND group_id = %s AND status = 'sent'
          AND (engage_signal IS NULL OR engage_signal = 'short-window'){row_scope}
          AND window_expires_at IS NOT NULL AND window_expires_at > %s
        """,
        (engaged, signal, at_sec, account_id, group_id, *row_arg, at_sec),
    )


def close_expired_windows(
    account_id: str, at_sec: int, conn: db.Connection | None = None,
) -> dict[str, int]:
    """关掉所有到期观察窗。两条语句，别合并。

    (1) 已落弱信号没人升级 → 关窗**但保留 ``engaged=1``**（它确实被弱信号命中过）
    (2) 整窗无信号 → ``engaged=0`` + ``silence``（冷清群里 bot 说话没人理，是负样本）
    """
    c = conn or db.get_conn()
    weak = c.execute(
        """
        UPDATE wpp_hf_ledger SET status = 'closed', closed_at = %s
        WHERE account_id = %s AND status = 'sent' AND engage_signal = 'short-window'
          AND window_expires_at IS NOT NULL AND window_expires_at <= %s
        """,
        (at_sec, account_id, at_sec),
    )
    silent = c.execute(
        """
        UPDATE wpp_hf_ledger
        SET status = 'closed', engaged = 0, engage_signal = 'silence', closed_at = %s
        WHERE account_id = %s AND status = 'sent' AND engaged IS NULL
          AND window_expires_at IS NOT NULL AND window_expires_at <= %s
        """,
        (at_sec, account_id, at_sec),
    )
    return {"short_window": weak, "silence": silent}


def expire_stale_judged(
    account_id: str, at_sec: int, judged_before_sec: int, conn: db.Connection | None = None,
) -> int:
    """判了却一直没投递结果的陈旧行 → ``suppressed/no-deliver-outcome``。"""
    c = conn or db.get_conn()
    return c.execute(
        """
        UPDATE wpp_hf_ledger
        SET status = 'suppressed', suppressed_reason = 'no-deliver-outcome', closed_at = %s
        WHERE account_id = %s AND status = 'judged' AND judged_at <= %s
        """,
        (at_sec, account_id, judged_before_sec),
    )


def mark_veto(
    account_id: str, group_id: str, at_sec: int, conn: db.Connection | None = None,
) -> dict | None:
    """一键否决（``/heartflow veto``）：把该群**最近一条已发出**的行标成"这条不该回"。

    ⚠️ 排序键必须是 ``sent_at`` 而非 ``judged_at``：一条被预算/冷却拦了很久的旧
    ``judged`` 行会盖过刚发出的那条。

    ⚠️ 不能写成 ``WHERE id = (SELECT ... FROM 同一张表)`` —— MariaDB 拒绝同表子查询
    更新，故**分两步**（照搬 TS）。
    """
    c = conn or db.get_conn()
    row = c.query_one(
        """
        SELECT id, content_head, sent_at FROM wpp_hf_ledger
        WHERE account_id = %s AND group_id = %s AND status IN ('sent', 'closed')
          AND sent_at IS NOT NULL
        ORDER BY sent_at DESC, id DESC LIMIT 1
        """,
        (account_id, group_id),
    )
    if not row:
        return None
    row_id = int(row["id"])
    c.execute(
        """
        UPDATE wpp_hf_ledger
        SET engaged = 0, engage_signal = 'veto', status = 'closed', closed_at = %s
        WHERE id = %s AND account_id = %s
        """,
        (at_sec, row_id, account_id),
    )
    return {
        "id": row_id,
        "content_head": row.get("content_head"),
        "sent_at": None if row.get("sent_at") is None else int(row["sent_at"]),
    }


# --------------------------------------------------------------------------- 台账：读


def get_last_sent_at(account_id: str, conn: db.Connection | None = None) -> int | None:
    """★ 账号级「最后一次真发言」时刻 —— 静默金丝雀（**INVARIANT I6**）。

    ⚠️ **只能认 ``MAX(sent_at)``**。写成 ``WHERE status='sent'`` 是错的：
    sweep 的到期关窗会把 sent 行全推成 ``closed``，数出来**恒为 0**
    （「平台停了 3 天而所有指标都正常」就是被这个假 0 掩盖的）。
    """
    c = conn or db.get_conn()
    value = c.query_value(
        "SELECT MAX(sent_at) AS last_sent_at FROM wpp_hf_ledger "
        "WHERE account_id = %s AND sent_at IS NOT NULL",
        (account_id,),
    )
    return None if value is None else int(value)


def list_sent_counts_recent(
    account_id: str, hour_since_sec: int, day_since_sec: int,
    conn: db.Connection | None = None,
) -> list[dict]:
    """发言预算回填素材：每群近 1 小时 / 近 24 小时**已发出**条数 + 最近发出时刻。

    "已发出" = ``sent_at`` 非空（``sent`` 行还在窗内、``closed`` 行已收敛，两种都算），
    与 ``note_reply_sent`` 的记账口径一致：**发送成功即占额度，不因后续收敛退还**。

    ⚠️ ``hour_since_sec`` 必须是**本地整点**（与运行时小时桶键同口径），否则上一小时的
    尾巴会被算进本小时，让新小时一开始就少一条额度。
    """
    c = conn or db.get_conn()
    rows = c.query(
        """
        SELECT group_id,
               SUM(sent_at >= %s) AS hour_count,
               COUNT(*) AS day_count,
               MAX(sent_at) AS last_sent_at
        FROM wpp_hf_ledger
        WHERE account_id = %s AND sent_at IS NOT NULL AND sent_at >= %s
        GROUP BY group_id
        """,
        (hour_since_sec, account_id, day_since_sec),
    )
    for r in rows:
        r["hour_count"] = int(r.get("hour_count") or 0)
        r["day_count"] = int(r.get("day_count") or 0)
        r["last_sent_at"] = None if r.get("last_sent_at") is None else int(r["last_sent_at"])
    return rows


def list_recent_judged_scores(
    account_id: str, since_sec: int, row_cap: int = 2000, conn: db.Connection | None = None,
) -> list[dict]:
    """可达性护栏素材：该群近期**判过的所有分**。

    ⚠️ 必须含 sent/closed 行，不能只取 suppressed：**"发得出去"本身就是"阈值可达"
    的证据**。只取 suppressed 的话最高分必然低于阈值，护栏会把每一轮都判成"不可达"
    而长期误压阈值。
    """
    c = conn or db.get_conn()
    rows = c.query(
        """
        SELECT group_id, judge_overall
        FROM wpp_hf_ledger
        WHERE account_id = %s AND judged_at >= %s AND judge_overall IS NOT NULL
        ORDER BY judged_at DESC, id DESC LIMIT %s
        """,
        (account_id, since_sec, _cap(row_cap, 1, 20000)),
    )
    return [{"group_id": str(r["group_id"]), "judge_overall": float(r["judge_overall"])}
            for r in rows]


def list_closed_since(
    account_id: str, since_sec: int, limit: int = 2000, conn: db.Connection | None = None,
) -> list[dict]:
    """分层统计输入：窗口内**全部群**已收敛行，一次批量取（避免逐群 N+1）。

    ``engaged IS NOT NULL`` 排除 suppressed（没进观察窗的行不是样本）；
    ``engage_signal`` 允许 NULL —— 由 ``aggregate_layer_stats`` 侧丢弃
    （v1.6.8 之前的旧行没有信号列，那些正是旧错误标签）。
    """
    c = conn or db.get_conn()
    return c.query(
        """
        SELECT group_id, engaged, engage_signal, sent_at FROM wpp_hf_ledger
        WHERE account_id = %s AND status = 'closed' AND engaged IS NOT NULL
          AND sent_at IS NOT NULL AND sent_at >= %s
        ORDER BY sent_at DESC LIMIT %s
        """,
        (account_id, since_sec, _cap(limit, 1, 5000)),
    )


def get_closed_recent(
    account_id: str, group_id: str, limit: int = 200, conn: db.Connection | None = None,
) -> list[dict]:
    """该群最近已收敛样本（学习素材，按 ``closed_at`` 倒序）。

    ``sent_at`` 是调阈侧的**必需列**（B9，2026-10-02）：样本横跨 7 天，而"该不该采信
    这条弱信号"取决于**它自己那一小时**群本来有多热闹 —— 少了它就只剩"当前小时"一份
    本底可用（v1.6.8 踩过的坑，见 ``observe.hf_engagement_summary``）。

    v1.16.2 silence-exempt：补 ``judge_overall, effective_threshold`` —— 判定**当时**的快照
    （不是事后重读阈值：那条样本被判定时生效的是哪个阈值，直接决定「它算不算高置信被无视」）。
    ``DECIMAL`` 经 pymysql 回来是 ``Decimal``，这里统一收成 ``float``/``None``，
    与 ``label.is_hf_silence_exempted`` 的「非数字 ⇒ 不豁免」口径对齐（``Decimal`` 不是
    ``Number`` 语义，直接比较会绕开 `_finite` 守卫）。
    """
    c = conn or db.get_conn()
    rows = c.query(
        """
        SELECT engaged, closed_at, engage_signal, sent_at,
               judge_overall, effective_threshold
        FROM wpp_hf_ledger
        WHERE account_id = %s AND group_id = %s AND status = 'closed' AND engaged IS NOT NULL
        ORDER BY closed_at DESC LIMIT %s
        """,
        (account_id, group_id, _cap(limit, 1, 500)),
    )
    for r in rows:
        r["engaged"] = int(r.get("engaged") or 0)
        r["closed_at"] = None if r.get("closed_at") is None else int(r["closed_at"])
        r["sent_at"] = None if r.get("sent_at") is None else int(r["sent_at"])
        for col in ("judge_overall", "effective_threshold"):
            v = r.get(col)
            r[col] = None if v is None else float(v)
    return rows


def get_ledger_last(
    account_id: str, group_id: str, conn: db.Connection | None = None,
) -> dict | None:
    """该群最近一条台账行（``/heartflow why`` 用 —— 判不出原因时运维只能靠猜）。"""
    c = conn or db.get_conn()
    return c.query_one(
        """
        SELECT account_id, inbound_msg_id, new_msg_id, bot_msg_id, group_id, from_wxid,
               msg_type, content_head, judge_overall, dim_r, dim_w, dim_s, dim_t, dim_c,
               effective_threshold, energy, status, suppressed_reason, engaged,
               engage_signal, judged_at, sent_at
        FROM wpp_hf_ledger
        WHERE account_id = %s AND group_id = %s
        ORDER BY judged_at DESC, id DESC LIMIT 1
        """,
        (account_id, group_id),
    )


def find_ledger_by_bot_msg(
    account_id: str, bot_msg_id: str, conn: db.Connection | None = None,
) -> dict | None:
    """按 bot 发出那条的 msg_id 反查台账（v1.6.8 起才有值）。"""
    c = conn or db.get_conn()
    return c.query_one(
        # 同一 bot_msg_id 理论上唯一，但重放/多入口下可能有多行 —— 不带 ORDER BY 的
        # LIMIT 1 由存储引擎决定返回哪行（"引用 bot 回复"会随机对不上号）。
        "SELECT id, group_id, inbound_msg_id FROM wpp_hf_ledger "
        "WHERE account_id = %s AND bot_msg_id = %s ORDER BY id DESC LIMIT 1",
        (account_id, bot_msg_id),
    )


def list_recent_judged(
    account_id: str, limit: int = 50, conn: db.Connection | None = None,
) -> list[dict]:
    """最近 N 条判定（控制台「最近判定」表；单表、只读）。

    比 :func:`get_ledger_last` 多带 ``group_id``（跨群列表用）；数值统一成
    ``float``/``int``，避免 ``Decimal`` 直接进 JSON。
    """
    c = conn or db.get_conn()
    rows = c.query(
        """
        SELECT group_id, inbound_msg_id, from_wxid, content_head, judge_overall,
               effective_threshold, status, suppressed_reason, judged_at, sent_at
        FROM wpp_hf_ledger
        WHERE account_id = %s
        ORDER BY judged_at DESC, id DESC LIMIT %s
        """,
        (account_id, _cap(limit, 1, 200)),
    )
    for r in rows:
        for col in ("judge_overall", "effective_threshold"):
            v = r.get(col)
            r[col] = None if v is None else float(v)
        for col in ("judged_at", "sent_at"):
            v = r.get(col)
            r[col] = None if v is None else int(v)
        r["group_id"] = str(r.get("group_id") or "")
    return rows


def count_by_status(
    account_id: str, since_sec: int, conn: db.Connection | None = None,
) -> dict:
    """近 N 秒台账按 ``status``/``suppressed_reason`` 计数（``/heartflow status``）。

    v1.6.1 的可观测入口：**"judge 跑了但没回"就是被这个盲区掩盖了 3 天**
    （2026-09-11 judge 静默瘫）。
    """
    c = conn or db.get_conn()
    rows = c.query(
        """
        SELECT status, suppressed_reason, COUNT(*) AS n FROM wpp_hf_ledger
        WHERE account_id = %s AND judged_at >= %s
        GROUP BY status, suppressed_reason
        """,
        (account_id, since_sec),
    )
    by_status: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    total = 0
    for r in rows:
        n = int(r.get("n") or 0)
        st = str(r.get("status") or "")
        by_status[st] = by_status.get(st, 0) + n
        total += n
        reason = r.get("suppressed_reason")
        if reason is not None:
            by_reason[str(reason)] = by_reason.get(str(reason), 0) + n
    return {"total": total, "by_status": by_status, "by_suppressed_reason": by_reason}


def count_engage_signals(
    account_id: str, since_sec: int, conn: db.Connection | None = None,
) -> dict[str, int]:
    """近 N 秒已收敛样本按信号分布（让"标签换对了没有"在运维面可见）。"""
    c = conn or db.get_conn()
    rows = c.query(
        """
        SELECT engage_signal, COUNT(*) AS n FROM wpp_hf_ledger
        WHERE account_id = %s AND status = 'closed' AND judged_at >= %s
        GROUP BY engage_signal
        """,
        (account_id, since_sec),
    )
    out: dict[str, int] = {}
    for r in rows:
        key = "legacy" if r.get("engage_signal") is None else str(r["engage_signal"])
        out[key] = out.get(key, 0) + int(r.get("n") or 0)
    return out


def list_distinct_closed_groups(
    account_id: str, since_sec: int, conn: db.Connection | None = None,
) -> list[str]:
    c = conn or db.get_conn()
    rows = c.query(
        """
        SELECT DISTINCT group_id FROM wpp_hf_ledger
        WHERE account_id = %s AND status = 'closed' AND engaged IS NOT NULL
          AND closed_at >= %s
        """,
        (account_id, since_sec),
    )
    return [str(r["group_id"]) for r in rows]


# --------------------------------------------------------------------------- 群状态


def get_group_state(
    account_id: str, group_id: str, conn: db.Connection | None = None,
) -> dict | None:
    """``learned_threshold`` 为 NULL 表示**回落账号级 ``replyThreshold``**。"""
    c = conn or db.get_conn()
    row = c.query_one(
        "SELECT account_id, group_id, learned_threshold, last_change_at, last_change_old, "
        "last_change_new, last_change_reason FROM wpp_hf_group_state "
        "WHERE account_id = %s AND group_id = %s LIMIT 1",
        (account_id, group_id),
    )
    return _num_fields(row, "learned_threshold", "last_change_old", "last_change_new")


def list_group_states(account_id: str, conn: db.Connection | None = None) -> list[dict]:
    c = conn or db.get_conn()
    rows = c.query(
        "SELECT account_id, group_id, learned_threshold, last_change_at, last_change_old, "
        "last_change_new, last_change_reason FROM wpp_hf_group_state "
        "WHERE account_id = %s ORDER BY group_id",
        (account_id,),
    )
    return [_num_fields(r, "learned_threshold", "last_change_old", "last_change_new") for r in rows]


def upsert_group_state(
    account_id: str, group_id: str, *, learned_threshold: float | None = None,
    last_change_at: int | None = None, last_change_old: float | None = None,
    last_change_new: float | None = None, last_change_reason: str | None = None,
    conn: db.Connection | None = None,
) -> int:
    """写每群自适应状态（**高频写库，绝不回写 accounts JSON** —— 防 fs.watch 抖动）。"""
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_hf_group_state
          (account_id, group_id, learned_threshold, last_change_at,
           last_change_old, last_change_new, last_change_reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          learned_threshold = VALUES(learned_threshold),
          last_change_at = VALUES(last_change_at),
          last_change_old = VALUES(last_change_old),
          last_change_new = VALUES(last_change_new),
          last_change_reason = VALUES(last_change_reason)
        """,
        (account_id, group_id, learned_threshold, last_change_at, last_change_old,
         last_change_new, _trunc(last_change_reason, 128)),
    )


def log_threshold_change(
    account_id: str, group_id: str, new_threshold: float, sample_total: int,
    sample_engaged: int, *, old_threshold: float | None = None, reason: str | None = None,
    conn: db.Connection | None = None,
) -> int:
    """阈值变更审计 —— 每次变更插一行（"变更留痕"护栏）。"""
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_hf_threshold_audit
          (account_id, group_id, old_threshold, new_threshold, sample_total, sample_engaged, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (account_id, group_id, old_threshold, new_threshold, int(sample_total),
         int(sample_engaged), _trunc(reason, 255)),
    )


def list_threshold_audit(
    account_id: str, group_id: str, limit: int = 20, conn: db.Connection | None = None,
) -> list[dict]:
    """首调阈判据就是这张表的**首行**。"""
    c = conn or db.get_conn()
    return c.query(
        "SELECT old_threshold, new_threshold, sample_total, sample_engaged, reason, created_at "
        "FROM wpp_hf_threshold_audit WHERE account_id = %s AND group_id = %s "
        "ORDER BY id DESC LIMIT %s",
        (account_id, group_id, _cap(limit, 1, 200)),
    )


# --------------------------------------------------------------------------- 画像


def get_profile(
    account_id: str, group_id: str, conn: db.Connection | None = None,
) -> dict | None:
    """取群画像；``profile_json`` **解析失败即视为无画像**（宁可没有，不可用坏的）。"""
    c = conn or db.get_conn()
    row = c.query_one(
        "SELECT profile_json, stats_json, sample_msgs, model, version, generated_at "
        "FROM wpp_hf_group_profile WHERE account_id = %s AND group_id = %s LIMIT 1",
        (account_id, group_id),
    )
    if not row:
        return None
    row["profile"] = _loads_or_none(row.pop("profile_json", None))
    row["stats"] = _loads_or_none(row.pop("stats_json", None))
    row["version"] = int(row.get("version") or 1)
    row["sample_msgs"] = int(row.get("sample_msgs") or 0)
    row["generated_at"] = None if row.get("generated_at") is None else int(row["generated_at"])
    return row


def list_profiles(account_id: str, conn: db.Connection | None = None) -> list[dict]:
    """取该账号**全部**画像行（含原始 JSON），``ORDER BY group_id``。

    列与 TS ``listHfGroupProfiles`` **逐列一致**：``account_id`` / ``group_id`` /
    ``profile_json`` / ``stats_json`` / ``sample_msgs`` / ``model`` / ``version`` /
    ``generated_at``。

    ⚠️ ``profile_json`` **原样返回不解析** —— 这是 sweep 预热用的批量读，解析与
    "坏 JSON 视为无画像"的判断在调用方（``profile.parse_hf_group_profile_row``）。
    这里替调用方吞掉坏行，等于把"这条画像坏了"这个事实藏起来。
    """
    c = conn or db.get_conn()
    rows = c.query(
        "SELECT account_id, group_id, profile_json, stats_json, sample_msgs, model, "
        "version, generated_at FROM wpp_hf_group_profile "
        "WHERE account_id = %s ORDER BY group_id",
        (account_id,),
    )
    for r in rows:
        r["generated_at"] = None if r.get("generated_at") is None else int(r["generated_at"])
        r["version"] = int(r.get("version") or 1)
        r["sample_msgs"] = int(r.get("sample_msgs") or 0)
    return rows


def upsert_profile(
    account_id: str, group_id: str, profile: Any, *, stats: Any = None,
    sample_msgs: int = 0, model: str | None = None, generated_at: int | None = None,
    conn: db.Connection | None = None,
) -> int:
    """写群画像（每次重生成 ``version + 1``）。

    ⚠️ 写进去的画像**只能收紧约束，不许放开**；注入 prompt 前还要过
    ``profile.strip_mute_phrases``（"别抢话"那类禁言指令曾否决 65% 权重）。
    """
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_hf_group_profile
          (account_id, group_id, profile_json, stats_json, sample_msgs, model, version, generated_at)
        VALUES (%s, %s, %s, %s, %s, %s, 1, %s)
        ON DUPLICATE KEY UPDATE
          profile_json = VALUES(profile_json),
          stats_json = VALUES(stats_json),
          sample_msgs = VALUES(sample_msgs),
          model = VALUES(model),
          version = version + 1,
          generated_at = VALUES(generated_at)
        """,
        (account_id, group_id, _dumps(profile), _dumps(stats),
         int(sample_msgs), model, generated_at),
    )


# --------------------------------------------------------------------- 图片配文


def upsert_hf_image_caption(
    account_id: str, url_hash: str, *, group_id: str | None = None,
    url_sample: str | None = None, caption: str | None = None,
    model: str | None = None, fail_delta: int = 0, created_at: int | None = None,
    conn: db.Connection | None = None,
) -> int:
    """写一行图片配文（v1.16.0，对齐 TS ``upsertHfImageCaption``）。

    语义与 TS **逐条一致**：
    - ``caption`` / ``model`` / ``url_sample`` 只在**非 NULL** 时覆盖（``COALESCE(VALUES(...), 原值)``）
      —— 失败那次传 ``caption=None`` 不许把之前配好的文字抹掉；
    - ``fail_count = fail_count + VALUES(fail_count)`` —— 失败增量在 SQL 侧累加（成功=0，失败=1），
      并发写不会丢计数。

    ``url_hash`` 是主键（sha1 全局唯一）：同一张图跨群只配一次，第二次命中只是 NO-OP 覆盖。
    """
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_hf_image_caption
          (url_hash, account_id, group_id, url_sample, caption, model, fail_count, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          caption = COALESCE(VALUES(caption), caption),
          model = COALESCE(VALUES(model), model),
          url_sample = COALESCE(VALUES(url_sample), url_sample),
          fail_count = fail_count + VALUES(fail_count)
        """,
        (url_hash, account_id, _trunc(group_id, 128), _trunc(url_sample, 255),
         caption, _trunc(model, 64), max(0, int(fail_delta)), created_at),
    )


def get_hf_captions_by_hashes(
    account_id: str, url_hashes: Sequence[str], conn: db.Connection | None = None,
) -> list[dict]:
    """批量查已试配文行（``url_hash / caption / fail_count``），供"跳过已配/止损"判定。

    上限 1000 条占位符 —— 与 TS 同（单次画像最多 150 条消息、每条 ≤4 图，1000 够用且不炸
    MariaDB 的 ``max_allowed_packet``）。空列表直接返回，不发 SQL。
    """
    hashes = [h for h in (url_hashes or []) if h][:1000]
    if not hashes:
        return []
    c = conn or db.get_conn()
    placeholders = ",".join(["%s"] * len(hashes))
    rows = c.query(
        f"SELECT url_hash, caption, fail_count FROM wpp_hf_image_caption "
        f"WHERE account_id = %s AND url_hash IN ({placeholders})",
        (account_id, *hashes),
    )
    out: list[dict] = []
    for r in rows:
        cap = r.get("caption")
        out.append({
            "url_hash": str(r.get("url_hash") or ""),
            "caption": None if cap is None else str(cap),
            "fail_count": int(r.get("fail_count") or 0),
        })
    return out


def list_hf_image_msgs(
    account_id: str, group_ids: Sequence[str], since_sec: int, limit: int = 150,
    conn: db.Connection | None = None,
) -> list[dict]:
    """取近 ``since_sec`` 以来的图片入站消息（``id / group_id / content``，按 id 倒序）。

    对齐 TS ``listHfImageMsgs``：
    - ``msg_type IN ('3','image')`` —— 库里两种形态都有（'3'=微信原始类型，'image'=解析后标记）；
    - 群归属用 ``COALESCE(NULLIF(chat_id,''), peer_id)``（照 v1.9.0 ``listHfBotMsgShare`` 先例）；
    - ``LIMIT`` 夹在 1..500（TS 同）。
    """
    c = conn or db.get_conn()
    safe_limit = _cap(limit if limit is not None else 100, 1, 500)
    where = ["account_id = %s", "direction = 'inbound'",
             "msg_type IN ('3','image')", "ts >= FROM_UNIXTIME(%s)"]
    params: list[Any] = [account_id, since_sec]
    gids = [g for g in (group_ids or []) if g]
    if gids:
        where.append(
            f"COALESCE(NULLIF(chat_id, ''), peer_id) IN ({','.join(['%s'] * len(gids))})")
        params.extend(gids)
    rows = c.query(
        f"SELECT id, COALESCE(NULLIF(chat_id, ''), peer_id) AS group_id, content "
        f"FROM wpp_messages WHERE {' AND '.join(where)} "
        f"ORDER BY id DESC LIMIT {safe_limit}",
        tuple(params),
    )
    out: list[dict] = []
    for r in rows:
        content = r.get("content")
        out.append({
            "id": int(r.get("id") or 0),
            "group_id": str(r.get("group_id") or ""),
            "content": None if content is None else str(content),
        })
    return out


# --------------------------------------------------------------------------- 分层统计


def upsert_layer_stat(
    account_id: str, group_id: str, layer_kind: str, layer_key: str, n: int, engaged: int,
    window_start: int, *, ambient_p: float | None = None, conn: db.Connection | None = None,
) -> int:
    """落一条分层统计。

    ⚠️ **每轮 sweep 全量重算后只写有变化的行**（稳态下每轮 0 写）。
    这张表是**快照不是累加器** —— 累加会在 300s 周期里重复计入且不可自愈（**I3**）。
    ``window_start`` 每次覆盖 = 窗口起点在滚动。
    """
    c = conn or db.get_conn()
    return c.execute(
        """
        INSERT INTO wpp_hf_layer_stat
          (account_id, group_id, layer_kind, layer_key, n, engaged, ambient_p, window_start)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          n = VALUES(n), engaged = VALUES(engaged),
          ambient_p = VALUES(ambient_p), window_start = VALUES(window_start)
        """,
        (account_id, group_id, layer_kind, layer_key, int(n), int(engaged),
         ambient_p, int(window_start)),
    )


def list_layer_stats(
    account_id: str, layer_kind: str | None = None, conn: db.Connection | None = None,
) -> list[dict]:
    """启动预热：账号全部段统计（**判定热路径不读它**）。"""
    c = conn or db.get_conn()
    if layer_kind:
        rows = c.query(
            "SELECT account_id, group_id, layer_kind, layer_key, n, engaged, ambient_p, "
            "window_start FROM wpp_hf_layer_stat WHERE account_id = %s AND layer_kind = %s",
            (account_id, layer_kind),
        )
    else:
        rows = c.query(
            "SELECT account_id, group_id, layer_kind, layer_key, n, engaged, ambient_p, "
            "window_start FROM wpp_hf_layer_stat WHERE account_id = %s",
            (account_id,),
        )
    for r in rows:
        r["n"] = int(r.get("n") or 0)
        r["engaged"] = int(r.get("engaged") or 0)
        r["window_start"] = int(r.get("window_start") or 0)
        r["ambient_p"] = None if r.get("ambient_p") is None else float(r["ambient_p"])
    return rows


# --------------------------------------------------------------------------- 内部


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _loads_or_none(value: Any):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        # 坏了就当没有 —— 宁可无画像，也不要把半个 JSON 喂给 prompt
        logger.warning("[WPP] 群画像 JSON 解析失败，按无画像处理（前 80 字: %.80s）", value)
        return None


def _num_fields(row: dict | None, *fields: str) -> dict | None:
    if row is None:
        return None
    for f in fields:
        if f in row and row[f] is not None:
            row[f] = float(row[f])
    return row


def _cap(value: int, low: int, high: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = low
    return max(low, min(n, high))


def _trunc(text: str | None, limit: int) -> str | None:
    """空串按 NULL 存（避免 '' 与 NULL 两种"没有"在统计里分裂）。"""
    if not text:
        return None
    return text[:limit]
