"""心流发言预算（结构层频率约束）—— ``heartflow-budget.ts`` (v1.6.9) 的移植。

## 为什么要有它（2026-09-26 老板拍板）

老板：「群里回复消息的频率太高了 … 如果我不设定下限，就会一直降低[阈值]」。
根因有两层：① 标签学错了（v1.6.8 修，见 :mod:`.label`）；② **拿一个被自学的标量去控频率**，
它必然漂到边界。阈值是"要不要开口"的**质量**判据，不该同时兼任"多久能开口一次"的**频率**闸。
⇒ 把频率约束上移到**结构层**：有界、可解释、**不依赖学习**。

## 与既有机制的分工（不是替代）

- energy 状态机：保留原样（软性精力衰减，影响 judge 的 willingness 维度）
- ``minReplyIntervalSec`` / ``minJudgeIntervalSec``：保留（既有冷却与 LLM 调用闸）
- **本模块**：每群硬上限（最小间隔 / 每小时 / 每天）+ 静默段 + 陈旧触发防重放

预算在 ``check_gate`` 里、**judge 之前**判定 ⇒ 顺带省掉被拦那次的 LLM 调用。

## 缓存与重启

计数在内存（:data:`_budget`），**judge 热路径零 DB IO**；进程启动时用一条聚合
（``list_sent_counts_recent``）回填小时/天计数，**重启不清零**（否则"重启刷额度"成了后门）。
已知降级：``lastHumanAtSec`` 无法回填 ⇒ 重启后为空（见 :func:`record_budget_human` 的说明）。
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .guards import assert_not_looser
from .runtime import CACHE_REGISTRY

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ 参数


#: 老板 2026-09-26 拍板档位：**中等**（每群 ≤1条/3分钟，≤8条/小时，≤60条/天）
HF_BUDGET_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "minGapSec": 180,
    "maxPerHour": 8,
    "maxPerDay": 60,
    "noConsecutiveWithoutHuman": True,
    "quietHours": [],
}

#: 静默段硬上限：单段 ≤ 6 小时，全天合计 ≤ 8 小时（防一次幻觉把群整天静音）
HF_QUIET_MAX_RANGE_H = 6
HF_QUIET_MAX_TOTAL_H = 8

#: 预算拦截原因（``/heartflow status`` 归因用）
REASON_QUIET_HOURS = "quiet-hours"
REASON_GAP = "budget-gap"
REASON_HOUR = "budget-hour"
REASON_DAY = "budget-day"
REASON_CONSECUTIVE = "budget-consecutive"


@dataclass(frozen=True)
class BudgetConfig:
    """**已解析**的预算参数（缺省已填满）—— 对应 TS 的 ``Required<HfBudgetConfig>``。"""

    enabled: bool
    min_gap_sec: int
    max_per_hour: int
    max_per_day: int
    no_consecutive_without_human: bool
    quiet_hours: tuple[tuple[int, int], ...]

    def as_dict(self) -> dict[str, Any]:
        """给 :func:`guards.assert_not_looser` 用的扁平视图（键名对齐 TS）。"""
        return {
            "enabled": self.enabled,
            "minGapSec": self.min_gap_sec,
            "maxPerHour": self.max_per_hour,
            "maxPerDay": self.max_per_day,
            "quietHours": list(self.quiet_hours),
        }


@dataclass(frozen=True)
class BudgetState:
    """每群一份预算状态。**不可变** —— 修改走 :func:`roll_budget_state` 等纯函数。"""

    hour_key: str
    hour_count: int
    day_key: str
    day_count: int
    #: 本群上次**真正发出**发言的时刻（秒）
    last_reply_at_sec: int | None = None
    #: 本群上次收到人类消息的时刻（秒；重启后为 ``None``）
    #:
    #: ⚠️ **仅用于观测**（``/heartflow status`` / 画像看"群里多久没人说话了"），
    #: **不参与预算判定**：它在 handler 的 debounce flush 时按**墙上时间**记录，
    #: 而门禁在消息到达时按**消息时间**判定 ⇒ 两者存在抖动量级偏差，
    #: 拿它当闸会误拦紧随其后的正常消息（宁可少一条规则，不要一个会误杀的规则）。
    last_human_at_sec: int | None = None


# --------------------------------------------------------------- 时间桶


def hf_day_bucket_key(now_sec: int) -> str:
    """本地日期键 ``YYYY-MM-DD``（本地时区，与看日志/加静默段的直觉一致）。"""
    d = datetime.fromtimestamp(now_sec)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def hf_local_hour(now_sec: int) -> int:
    """本地小时 (0-23)。"""
    return datetime.fromtimestamp(now_sec).hour


def hf_hour_bucket_key(now_sec: int) -> str:
    """本地小时桶键。"""
    return f"{hf_day_bucket_key(now_sec)}#{hf_local_hour(now_sec)}"


def hf_hour_start_sec(now_sec: int) -> int:
    """本地**整点**起点（秒）—— 回填"本小时已发条数"的边界。

    必须与 :func:`hf_hour_bucket_key` **同口径**：用 ``now-3600`` 会把上一小时的尾巴
    算进本小时，新小时一开局就少一条额度。走本地 ``replace(minute=0...)`` 让运行时的
    时区规则（含 DST）决定偏移，不自己算 UTC 偏移。
    """
    d = datetime.fromtimestamp(now_sec).replace(minute=0, second=0, microsecond=0)
    return int(d.timestamp())


def hf_day_start_sec(now_sec: int) -> int:
    """本地**零点**起点（秒）—— 天桶按**日历日**滚动，不是滚动 24h。"""
    d = datetime.fromtimestamp(now_sec).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp())


def is_hf_quiet_hour(hour: float, ranges: Sequence[Sequence[int]] | None) -> bool:
    """是否落在静默段（纯函数）。

    区间按 ``[start, end)`` 半开 —— 相邻段 ``[9,12)`` 与 ``[12,15)`` 不重叠。
    ``start == end`` 视为**空段**（不静默）：否则 ``[0,0]`` 会意外变成全天静默。
    ``start > end`` 表示跨零点（如 ``[23, 7]`` = 23:00–06:59）。
    """
    if not ranges:
        return False
    h = int(math.floor(hour)) % 24
    for pair in ranges:
        start = int(math.floor(pair[0])) % 24
        end = int(math.floor(pair[1])) % 24
        if start == end:
            continue
        if (start < end and start <= h < end) or (start >= end and (h >= start or h < end)):
            return True
    return False


def hf_quiet_range_hours(rng: Sequence[int]) -> int:
    """静默段时长（小时；跨零点按 ``24 - s + e`` 折算）。"""
    s, e = int(rng[0]), int(rng[1])
    if s == e:
        return 0
    return e - s if e > s else 24 - s + e


def merge_hf_quiet_hours(base: Sequence[Sequence[int]],
                         extra: Sequence[Sequence[int]]) -> list[list[int]]:
    """合并静默段（配置 ∪ 画像建议），逐段过硬上限；超限的**整段丢弃**。

    合计超上限时按**短段优先**保留（长的先丢 —— 一段 6h 静音比三段 2h 更危险）。
    超限是**丢弃不是截断**：半段静默的语义说不清楚。
    """
    out: list[list[int]] = [list(r) for r in base]
    total = sum(hf_quiet_range_hours(r) for r in out)
    candidates = [list(r) for r in extra if hf_quiet_range_hours(r) <= HF_QUIET_MAX_RANGE_H]
    candidates.sort(key=hf_quiet_range_hours)          # 稳定排序（与 JS sort 一致）
    for r in candidates:
        h = hf_quiet_range_hours(r)
        if h <= 0 or total + h > HF_QUIET_MAX_TOTAL_H:
            continue
        out.append(r)
        total += h
        if len(out) >= 6:
            break
    return out


# ------------------------------------------------------------------ 参数解析


def _pos(v: Any, fallback: int) -> int:
    """正数取整；非有限数 / ≤0 / 非数 ⇒ ``fallback``（对齐 TS ``pos()``）。"""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return fallback
    if not math.isfinite(v) or v <= 0:
        return fallback
    return int(math.floor(v))


def _pairs(raw: Any) -> tuple[tuple[int, int], ...]:
    if not raw:
        return ()
    out: list[tuple[int, int]] = []
    for item in raw:
        try:
            out.append((int(item[0]), int(item[1])))
        except (TypeError, IndexError, ValueError):
            continue          # 坏段丢弃，不让一行脏配置炸掉整块
    return tuple(out)


#: 脏值告警去重（键 = ``(字段, 值的 repr)``）。
#: ``resolve_budget`` 在**每条触发消息**上被调用，不节流就是日志洪水 —— 而洪水里的
#: 真信号等于没有信号（09-11 那次留痕缺失的教训）。
_dirty_warned: set[tuple[str, str]] = set()


def _cfg_num(field: str, raw: Any, fallback: int) -> int:
    """配置里的数值参数：**有限数原样取整**，其余回落 ``fallback`` 并告警一次（B1）。

    ⚠️ **为什么不直接用 :func:`_pos`**：``_pos`` 把 ``0``/负数也当脏值回落默认档，
    而这三个参数的 ``0`` 在 TS 里是**有意义的** —— ``check_budget`` 的守卫是
    ``cfg.min_gap_sec > 0`` / ``max_per_hour > 0`` / ``max_per_day > 0`` ⇒
    ``0`` = **该闸不生效**。老板在配置里写 ``0``（"不限"），顺手用 ``_pos`` 兜底会把它
    悄悄改成默认 8 ⇒ **背着配置收紧**。这里只兜"说不清是什么"的那一类。

    ⚠️ **修前**这里是裸 ``int(b.get("minGapSec"))``：字符串/``None``/``NaN``/``inf``
    会抛 ``ValueError``/``OverflowError`` —— 而调用点在热路径（每条触发消息一次）
    ⇒ 现象是心流**整条静默**，日志里只有一行 ValueError（比"值算错了"贵得多）。
    TS 那边不会炸纯粹是因为 JS 的比较不抛异常（``"abc" > 0`` 是 ``false``）。
    """
    if raw is None:
        return fallback                      # 没配这个键 = 用默认，**不是**脏值（不告警）
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        got = None
    elif not math.isfinite(raw):
        got = None
    else:
        got = int(math.floor(raw))
    if got is None:
        key = (field, repr(raw))
        if key not in _dirty_warned:
            _dirty_warned.add(key)
            log.warning("[WPP] 心流预算参数 %s 是脏值 %r ⇒ 回落默认 %s"
                        "（配置写错了，别以为它在生效）", field, raw, fallback)
        return fallback
    return got


def resolve_budget(cfg: Mapping[str, Any] | None) -> BudgetConfig:
    """合并参数（缺省走 :data:`HF_BUDGET_DEFAULTS`；不依赖 accounts 的合并语义）。

    数值三项走 :func:`_cfg_num`（脏值兜底 + 告警），不用裸 ``int()`` —— 见那里的说明。
    """
    b = (cfg or {}).get("budget") or {}
    D = HF_BUDGET_DEFAULTS
    return BudgetConfig(
        enabled=bool(b.get("enabled", D["enabled"])),
        min_gap_sec=_cfg_num("minGapSec", b.get("minGapSec"), D["minGapSec"]),
        max_per_hour=_cfg_num("maxPerHour", b.get("maxPerHour"), D["maxPerHour"]),
        max_per_day=_cfg_num("maxPerDay", b.get("maxPerDay"), D["maxPerDay"]),
        no_consecutive_without_human=bool(
            b.get("noConsecutiveWithoutHuman", D["noConsecutiveWithoutHuman"])),
        quiet_hours=_pairs(b.get("quietHours", D["quietHours"])),
    )


def tighten_budget(base: BudgetConfig, tighter: Mapping[str, Any] | None, *,
                   apply_quiet_hours: bool = False) -> BudgetConfig:
    """把画像建议的预算与配置预算取**交集** —— 画像只能让约束更紧，不许放宽（v1.7.0）。

    取"更严的那一侧"：间隔取 ``max``，每小时/每天上限取 ``min``。
    ``enabled=False`` 的画像建议**整条忽略**（画像无权关掉结构约束）。

    静默段默认**不并入**（老板 2026-09-26：「静默段默认关」；且它与其他参数不同 ——
    它是"彻底不吭声"的开关，一次幻觉就能让某个群整天无响应）。要启用须显式传
    ``apply_quiet_hours=True``，且并入时过硬上限（见 :func:`merge_hf_quiet_hours`）。

    为什么不在 :func:`resolve_budget` 里做：那是"配置→参数"的一步，这里是"配置 ∩ 画像"。
    分开才能让 gate 在**不 import 画像模块**的前提下拿到结果（避免 ``heartflow`` ⟷
    ``profile`` 成环）。
    """
    if not tighter or tighter.get("enabled") is False:
        return base

    quiet = (merge_hf_quiet_hours(base.quiet_hours, _pairs(tighter.get("quietHours")))
             if apply_quiet_hours else base.quiet_hours)

    def _tighter_cap(b: int, raw: Any) -> int:
        v = _pos(raw, b)
        # base 为 0/负 = 不设上限 ⇒ 画像给的任何正数都是收紧
        return min(b, v) if b > 0 else v

    out = BudgetConfig(
        enabled=base.enabled,
        min_gap_sec=max(base.min_gap_sec, _pos(tighter.get("minGapSec"), base.min_gap_sec)),
        max_per_hour=_tighter_cap(base.max_per_hour, tighter.get("maxPerHour")),
        max_per_day=_tighter_cap(base.max_per_day, tighter.get("maxPerDay")),
        no_consecutive_without_human=base.no_consecutive_without_human,
        quiet_hours=quiet,
    )
    # INVARIANT: I2 —— 画像/占比/预算只能收紧不能放宽（09-26 复盘：漂移永远朝松的方向）
    assert_not_looser(base.as_dict(), out.as_dict(),
                      higher_is_tighter=("minGapSec",),
                      lower_is_tighter=("maxPerHour", "maxPerDay"),
                      label="budget")
    return out


# ------------------------------------------------------------------ 判定


def new_budget_state(now_sec: int) -> BudgetState:
    """新建状态（计数从 0 起）。"""
    return BudgetState(hour_key=hf_hour_bucket_key(now_sec), hour_count=0,
                       day_key=hf_day_bucket_key(now_sec), day_count=0)


def roll_budget_state(state: BudgetState, now_sec: int) -> BudgetState:
    """桶滚动（纯函数）：跨小时 / 跨天则对应计数归零。"""
    day_key = hf_day_bucket_key(now_sec)
    hour_key = hf_hour_bucket_key(now_sec)
    if state.day_key == day_key and state.hour_key == hour_key:
        return state
    same_day = state.day_key == day_key
    return replace(
        state,
        day_key=day_key,
        day_count=state.day_count if same_day else 0,
        hour_key=hour_key,
        hour_count=state.hour_count if state.hour_key == hour_key else 0,
    )


def check_budget(state: BudgetState, cfg: BudgetConfig, now_sec: int,
                 candidate_at_sec: int | None = None) -> tuple[bool, str | None]:
    """预算判定（**纯函数，不改 state**）。返回 ``(allowed, reason)``。

    判定顺序（从"最绝对"到"最软"）：静默段 → 最小间隔 → 陈旧触发 → 每小时 → 每天。
    只报**第一条**命中的原因（便于 ``/heartflow status`` 归因，不叠加）。

    ⚠️ 计划里 ``noConsecutiveWithoutHuman`` 写的是"无人类插话不得连发第 2 条"，
    但**照字面实现会永远不生效**：心流的每次触发本身就源自一条人类消息，
    故"上次发言后没有人类消息"在门禁处不可能成立。这里落成**陈旧触发防重放**：
    候选消息**不晚于**我们上次发言 ⇒ 是同一批/更旧的消息被重复处理
    （debounce 重放、重试、补扫），拦住它。真频率约束由
    ``min_gap_sec`` / ``max_per_hour`` / ``max_per_day`` 承担。
    """
    if not cfg.enabled:
        return True, None
    rolled = roll_budget_state(state, now_sec)
    cand = now_sec if candidate_at_sec is None else candidate_at_sec

    if is_hf_quiet_hour(hf_local_hour(now_sec), cfg.quiet_hours):
        return False, REASON_QUIET_HOURS
    if (cfg.min_gap_sec > 0 and rolled.last_reply_at_sec is not None
            and now_sec - rolled.last_reply_at_sec < cfg.min_gap_sec):
        return False, REASON_GAP
    if (cfg.no_consecutive_without_human and rolled.last_reply_at_sec is not None
            and cand <= rolled.last_reply_at_sec):
        return False, REASON_CONSECUTIVE
    if cfg.max_per_hour > 0 and rolled.hour_count >= cfg.max_per_hour:
        return False, REASON_HOUR
    if cfg.max_per_day > 0 and rolled.day_count >= cfg.max_per_day:
        return False, REASON_DAY
    return True, None


def record_budget_reply(state: BudgetState, now_sec: int) -> BudgetState:
    """记一次**已确认发出**的发言（纯函数：返回新 state）。"""
    rolled = roll_budget_state(state, now_sec)
    return replace(rolled, hour_count=rolled.hour_count + 1,
                   day_count=rolled.day_count + 1, last_reply_at_sec=now_sec)


def record_budget_human(state: BudgetState, now_sec: int) -> BudgetState:
    """记一条人类消息（纯函数：返回新 state；只前进不后退）。"""
    rolled = roll_budget_state(state, now_sec)
    last = rolled.last_human_at_sec or 0
    if now_sec <= last:
        return rolled
    return replace(rolled, last_human_at_sec=now_sec)


# ------------------------------------------------------- 进程内状态（有缓存）


_lock = threading.RLock()
#: ``f"{account_id}:{group_id}" -> BudgetState``
_budget: dict[str, BudgetState] = {}
#: 进程内发生的预算拦截计数（按原因；供 ``/heartflow status``；**重启归零，显示须标注**）
_blocked: dict[str, int] = {}


def _key(account_id: str, group_id: str) -> str:
    return f"{account_id}:{group_id}"


def get_budget_state(account_id: str, group_id: str, now_sec: int) -> BudgetState:
    """取状态（不存在则新建；顺带滚动桶）。"""
    with _lock:
        k = _key(account_id, group_id)
        cur = _budget.get(k)
        if cur is None:
            fresh = new_budget_state(now_sec)
            _budget[k] = fresh
            return fresh
        rolled = roll_budget_state(cur, now_sec)
        if rolled is not cur:
            _budget[k] = rolled
        return rolled


def peek_budget(account_id: str | None, group_id: str, cfg: BudgetConfig, now_sec: int,
                candidate_at_sec: int | None = None) -> tuple[bool, str | None]:
    """门禁调用：判定 + 记拦截（**不写 DB**）。"""
    if not cfg.enabled:
        return True, None
    acct = account_id or "*"
    allowed, reason = check_budget(get_budget_state(acct, group_id, now_sec), cfg,
                                   now_sec, candidate_at_sec)
    if not allowed and reason:
        with _lock:
            _blocked[reason] = _blocked.get(reason, 0) + 1
        log.debug("[WPP HF] budget blocked: account=%s group=%s reason=%s", acct, group_id, reason)
    return allowed, reason


def note_human_message(account_id: str, group_id: str, now_sec: int) -> None:
    """人类消息 hook（handler 里与接话判定同一处调用；零额外 IO）。"""
    with _lock:
        k = _key(account_id, group_id)
        cur = _budget.get(k) or new_budget_state(now_sec)
        _budget[k] = record_budget_human(cur, now_sec)


def note_reply_sent(account_id: str, group_id: str, now_sec: int) -> None:
    """发言 hook —— **只在发送结果确认为 sent 后**调用（见 ``learn.persist_send_outcome``）。

    ⚠️ INVARIANT: I5 —— 占位/被抑制的消息**不得**走到这里（占位消息不消耗预算、不开启窗口）。
    """
    with _lock:
        k = _key(account_id, group_id)
        cur = _budget.get(k) or new_budget_state(now_sec)
        _budget[k] = record_budget_reply(cur, now_sec)


def seed_budget_states(account_id: str, rows: Iterable[Mapping[str, Any]],
                       now_sec: int) -> int:
    """启动回填（账号启动时一次）。

    重启**不清零**小时/天计数 —— 否则"重启刷额度"成了后门。
    ``lastHumanAtSec`` 无法回填 ⇒ 留 ``None``（见 :class:`BudgetState` 的说明）。
    """
    n = 0
    for r in rows:
        group_id = r.get("group_id")
        if not group_id:
            continue
        fresh = new_budget_state(now_sec)
        with _lock:
            _budget[_key(account_id, str(group_id))] = replace(
                fresh,
                hour_count=_floor_nonneg(r.get("hour_count")),
                day_count=_floor_nonneg(r.get("day_count")),
                last_reply_at_sec=r.get("last_sent_at"),
            )
        n += 1
    return n


def _floor_nonneg(v: Any) -> int:
    """``Math.max(0, Math.floor(v) || 0)`` 的等价：非数/NaN ⇒ 0。"""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    if not math.isfinite(v):
        return 0
    return max(0, int(math.floor(v)))


def hf_budget_blocked_snapshot() -> dict[str, int]:
    """拦截计数快照（**近进程生命周期**；供 ``/heartflow status``，显示时须标注"重启归零"）。"""
    with _lock:
        items = sorted(_blocked.items(), key=lambda kv: -kv[1])
    return dict(items)


def reset_budget_cache() -> None:
    """测试 / 热重载：清空预算状态、拦截计数与脏值告警去重表。"""
    with _lock:
        _budget.clear()
        _blocked.clear()
        _dirty_warned.clear()


def hf_budget_tracked_groups() -> int:
    """测试 / 可观测：当前已跟踪的群数。"""
    with _lock:
        return len(_budget)


CACHE_REGISTRY.register("budget", reset_budget_cache)
