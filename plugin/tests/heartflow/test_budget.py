"""心流发言预算回归 —— TS ``tests/unit/heartflow-budget.test.mjs`` (v1.6.9) 的 pytest 译本。

## 为什么有这份测试（2026-09-26 项目约定）

老板：「群里回复消息的频率太高了」+「如果我不设定下限，就会一直降低[阈值]」。
根因两层：① 标签学错了（v1.6.8 修）；② **拿一个被自学的标量去控频率**，它必然漂到
边界。阈值是"要不要开口"的**质量**判据，不该兼任"多久能开口一次"的**频率**闸。
⇒ 频率约束上移到**结构层**：有界、可解释、**不依赖学习**。

本文件锁住这套结构约束的**每条闸与每个边界**。

## 与 TS 版的对应（**逐 use-case 对齐，不逐行对齐**）

===========  ==========================================================
①②③④⑤⑥     本文件 ``# ===== N. =====`` 小节与 TS 第 N 节一一对应
⑦             TS 末节是**源级接线断言**（``fnBody`` 切片 + 正则），
              它锁的是"实现了但没接上"。**这里不译** —— 见文末说明。
===========  ==========================================================

## 基准时刻用**本地时间构造函数**取（不 hardcode epoch）

``at(h, m, day)`` 走 ``datetime(...)`` 的本地语义，与 TS 的
``new Date(2026, 8, day, h, m, 0)`` 一致 ⇒ ``hf_local_hour`` 在任何 TZ 下都可预期。
写死 epoch 的测试在换时区（或 CI 用 UTC）时会**整体错一小时**，且失败长相是
"某条闸莫名多拦一条"，极难归因。
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime

import pytest

from plugin.inbound.heartflow import budget as B

#: 本地 2026-09-26 的某个时刻（秒）
def at(h: int, m: int = 0, day: int = 26) -> int:
    return int(datetime(2026, 9, day, h, m, 0).timestamp())


T = at(12)                                   # 2026-09-26 12:00 本地
CFG = B.resolve_budget(None)                 # 缺省档（老板选的"中等"）
D = B.HF_BUDGET_DEFAULTS


# ===== 1. 参数解析 =====


def test_resolve_defaults_are_the_boss_middle_tier():
    """缺省 = 老板选的「中等」档（3 分钟 / 8 条·小时 / 60 条·天）+ 静默段默认关。"""
    c = B.resolve_budget(None)
    assert c.enabled is True, "结构约束默认生效（老板 2026-09-26 选「中等」）"
    assert c.min_gap_sec == 180, "1 条 / 3 分钟"
    assert c.max_per_hour == 8
    assert c.max_per_day == 60
    assert c.no_consecutive_without_human is True
    assert c.quiet_hours == (), "静默段默认**关**（机制做好，等 P1 画像按群填）"

    # 空 dict 与 None 等价（"没配" 与 "配了个空对象" 不该有两套语义）
    assert B.resolve_budget({}) == c
    assert B.resolve_budget({"budget": {}}) == c


def test_resolve_overrides_are_per_key_others_fall_back():
    """accounts 覆盖逐键生效（未覆盖的键走默认）。"""
    c = B.resolve_budget({"budget": {"minGapSec": 600, "quietHours": [[23, 7]]}})
    assert c.min_gap_sec == 600, "覆盖生效"
    assert c.max_per_hour == D["maxPerHour"], "未覆盖的键走默认"
    assert c.max_per_day == D["maxPerDay"]
    assert c.enabled is True
    assert c.quiet_hours == ((23, 7),)


def test_resolve_enabled_false_is_the_master_switch():
    """``enabled=false`` 是总开关（可整块关掉预算）。"""
    c = B.resolve_budget({"budget": {"enabled": False}})
    assert c.enabled is False
    st = replace(B.new_budget_state(T), hour_count=999, day_count=9999)
    assert B.check_budget(st, c, T) == (True, None), "关了就不拦"


def test_resolve_quiet_hours_drops_dirty_pairs_without_crashing():
    """坏静默段**逐条丢弃**，不让一行脏配置炸掉整块预算。"""
    c = B.resolve_budget({"budget": {"quietHours": [[1, 2], ["x"], [3], [[4, 5]], [9, 10]]}})
    assert c.quiet_hours == ((1, 2), (9, 10))


# ===== 2. 桶键与边界（回填口径必须与运行时一致）=====


@pytest.mark.parametrize("h", [0, 7, 12, 23])
def test_bucket_keys_agree_with_runtime_buckets(h):
    """本地整点 / 零点与运行时桶键**同口径**（回填不带上一段余数）。"""
    ts = at(h, 37)
    assert B.hf_local_hour(ts) == h, f"hf_local_hour({h} 时) 必须等于 {h}"
    assert B.hf_hour_bucket_key(B.hf_hour_start_sec(ts)) == B.hf_hour_bucket_key(ts), (
        "整点起点必须落在同一个小时候桶里（否则回填的小时计数会溢到上一小时）")
    assert B.hf_day_bucket_key(B.hf_day_start_sec(ts)) == B.hf_day_bucket_key(ts), (
        "零点起点必须落在同一个天桶里")
    assert B.hf_hour_start_sec(ts) <= ts and B.hf_day_start_sec(ts) <= ts, "起点不得晚于当前时刻"
    assert ts - B.hf_hour_start_sec(ts) < 3600, "整点起点必须在本小时内"
    assert ts - B.hf_day_start_sec(ts) < 86400, "零点起点必须在今天内"


def test_bucket_keys_roll_across_midnight():
    """跨零点（23:59 → 次日 00:01）天键与小时键都滚动。"""
    late = at(23, 59, 26)
    nxt = at(0, 1, 27)
    assert B.hf_day_bucket_key(late) != B.hf_day_bucket_key(nxt), "跨零点天键必须变"
    assert B.hf_hour_bucket_key(late) != B.hf_hour_bucket_key(nxt), "跨零点小时键必须变"


# ===== 3. 静默段 =====


def test_quiet_hour_is_half_open_start_in_end_out():
    """半开区间 ``[start, end)`` —— 起点静默、终点不静默（相邻段不重叠）。"""
    r = [[9, 12]]
    assert B.is_hf_quiet_hour(8, r) is False
    assert B.is_hf_quiet_hour(9, r) is True, "起点含"
    assert B.is_hf_quiet_hour(11, r) is True
    assert B.is_hf_quiet_hour(12, r) is False, "终点不含（否则与 [12,15) 重叠）"


def test_quiet_hour_wraps_midnight():
    """跨零点 ``[23, 7]`` 覆盖 23/0/6 但不覆盖 7。"""
    r = [[23, 7]]
    for h in (23, 0, 3, 6):
        assert B.is_hf_quiet_hour(h, r) is True, f"{h} 时应在静默段"
    for h in (7, 12, 22):
        assert B.is_hf_quiet_hour(h, r) is False, f"{h} 时不应在静默段"


def test_quiet_hour_empty_and_zero_length_segment():
    """空数组 / ``None`` 不静默；``start == end`` 视为空段（防 ``[0,0]`` 变全天静默）。"""
    assert B.is_hf_quiet_hour(3, []) is False
    assert B.is_hf_quiet_hour(3, None) is False
    assert B.is_hf_quiet_hour(0, [[0, 0]]) is False, "start === end 不得变成全天静默"
    assert B.is_hf_quiet_hour(3, [[0, 0]]) is False


def test_quiet_hours_wire_into_check():
    """静默段接进 :func:`check_budget`：命中即拦（``reason=quiet-hours``）。"""
    st = B.new_budget_state(at(23, 30))
    cfg = B.resolve_budget({"budget": {"quietHours": [[23, 7]]}})
    assert B.check_budget(st, cfg, at(23, 30)) == (False, B.REASON_QUIET_HOURS)
    assert B.check_budget(st, cfg, at(12)) == (True, None), "白天放行（此状态下无其它闸命中）"


# ===== 4. 五条闸 + 边界 =====


def test_gate_min_gap_boundary_179_blocked_180_allowed():
    base = replace(B.new_budget_state(T), last_reply_at_sec=T)
    assert B.check_budget(base, CFG, T + 179) == (False, B.REASON_GAP), "179s < 180s 必须拦"
    assert B.check_budget(base, CFG, T + 180) == (True, None), "恰好 180s 放行"


def test_gate_hourly_boundary_7_allowed_8_blocked():
    st = B.new_budget_state(T)
    assert B.check_budget(replace(st, hour_count=7), CFG, T) == (True, None), "第 8 条不误拦"
    assert B.check_budget(replace(st, hour_count=8), CFG, T) == (False, B.REASON_HOUR), "满 8 条即拦"


def test_gate_daily_boundary_59_allowed_60_blocked():
    st = B.new_budget_state(T)
    assert B.check_budget(replace(st, day_count=59), CFG, T) == (True, None), "第 60 条不误拦"
    assert B.check_budget(replace(st, day_count=60), CFG, T) == (False, B.REASON_DAY), "满 60 条即拦"


def test_gate_stale_candidate_blocks_replays():
    """陈旧触发：候选消息不晚于上次发言 ⇒ 拦（防同批 / 重试被重复处理）。"""
    st = replace(B.new_budget_state(T), last_reply_at_sec=T)
    assert B.check_budget(st, CFG, T + 200, T)[0] is False
    assert B.check_budget(st, CFG, T + 200, T)[1] == B.REASON_CONSECUTIVE, (
        "候选与上次发言同时刻 = 重放，拦")
    assert B.check_budget(st, CFG, T + 200, T - 5) == (False, B.REASON_CONSECUTIVE), (
        "候选比上次发言旧 = 陈旧，拦")
    assert B.check_budget(st, CFG, T + 200, T + 1) == (True, None), (
        "候选比上次发言新 = 真·新消息，放行（真实人类消息不会被误拦）")

    no_reply_yet = B.new_budget_state(T)
    assert B.check_budget(no_reply_yet, CFG, T, T - 100) == (True, None), (
        "还没发过言 ⇒ 无「陈旧」可言")


def test_gate_reason_priority_reports_only_the_first_hit():
    """只报**第一条**命中的原因（静默段 > 间隔 > 陈旧 > 小时 > 天）。

    归因给人看，叠加会变成一锅粥；顺序从「最绝对」到「最软」。
    """
    st = B.BudgetState(hour_key=B.hf_hour_bucket_key(at(23, 30)), hour_count=99,
                       day_key=B.hf_day_bucket_key(at(23, 30)), day_count=999,
                       last_reply_at_sec=at(23, 30))
    cfg = B.resolve_budget({"budget": {"quietHours": [[23, 7]]}})
    assert B.check_budget(st, cfg, at(23, 30))[1] == B.REASON_QUIET_HOURS, (
        "多闸同时命中时报最绝对的那条")
    cfg2 = B.resolve_budget({"budget": {"minGapSec": 180}})
    assert B.check_budget(st, cfg2, at(23, 30))[1] == B.REASON_GAP, "无静默段时报间隔"


def test_gate_disabling_stale_check_leaves_other_gates_intact():
    cfg = B.resolve_budget({"budget": {"noConsecutiveWithoutHuman": False}})
    st = replace(B.new_budget_state(T), last_reply_at_sec=T)
    assert B.check_budget(st, cfg, T + 200, T - 100) == (True, None), "陈旧检查关了就该放行"
    assert B.check_budget(st, cfg, T + 100, T - 100) == (False, B.REASON_GAP), "间隔闸仍在"


def test_gate_zero_caps_mean_unlimited():
    cfg = B.resolve_budget({"budget": {"maxPerHour": 0, "maxPerDay": 0}})
    st = replace(B.new_budget_state(T), hour_count=999, day_count=9999)
    assert B.check_budget(st, cfg, T) == (True, None)


# ===== 5. 滚动 =====


def test_roll_clears_hour_keeps_day_then_clears_both():
    st = replace(B.new_budget_state(T), hour_count=5, day_count=20)

    same_hour = B.roll_budget_state(st, T + 60)
    assert (same_hour.hour_count, same_hour.day_count) == (5, 20), "同小时不动"

    next_hour = B.roll_budget_state(st, at(13))
    assert next_hour.hour_count == 0, "跨小时清小时计数"
    assert next_hour.day_count == 20, "天计数保留"

    next_day = B.roll_budget_state(replace(st, hour_key=B.hf_hour_bucket_key(at(23, 30))),
                                   at(0, 5, 27))
    assert next_day.hour_count == 0
    assert next_day.day_count == 0, "跨天清零"


def test_purity_inputs_are_never_mutated():
    """check / roll / record 都不改入参对象（纯函数 ⇒ 可单测 / 可回放）。"""
    st = replace(B.new_budget_state(T), hour_count=3, day_count=7, last_reply_at_sec=T - 10)
    snap = asdict(st)
    B.check_budget(st, CFG, T + 500, T + 1)
    B.roll_budget_state(st, at(23, 59, 27))
    B.record_budget_reply(st, T + 500)
    B.record_budget_human(st, T + 501)
    assert asdict(st) == snap, "入参必须原样"


def test_record_reply_and_human_touch_only_their_own_fields():
    st = replace(B.new_budget_state(T), hour_count=2, day_count=6)

    sent = B.record_budget_reply(st, T + 300)
    assert sent.hour_count == 3
    assert sent.day_count == 7
    assert sent.last_reply_at_sec == T + 300
    assert sent.last_human_at_sec is None, "发言不改人类消息时刻"

    human = B.record_budget_human(st, T + 100)
    assert human.last_human_at_sec == T + 100
    assert human.hour_count == 2, "人类消息不占额度"

    older = B.record_budget_human(replace(st, last_human_at_sec=T + 200), T + 100)
    assert older.last_human_at_sec == T + 200, "更旧的时刻不得把记录往回拨"


# ===== 6. 进程内状态 =====


def test_peek_counts_blocks_and_does_not_write_db():
    """判定并计入拦截快照（门禁零 DB 写）。"""
    acct, g = "acct-peek", "g-peek@chatroom"

    assert B.peek_budget(acct, g, CFG, T) == (True, None), "首次放行"
    B.note_reply_sent(acct, g, T)

    allowed, reason = B.peek_budget(acct, g, CFG, T + 10)
    assert allowed is False
    assert reason == B.REASON_GAP
    assert B.hf_budget_blocked_snapshot()[B.REASON_GAP] == 1, (
        "拦截要计数（供 /heartflow status 归因）")
    assert B.hf_budget_tracked_groups() == 1

    B.reset_budget_cache()
    assert B.hf_budget_blocked_snapshot() == {}, "重置清空"
    assert B.hf_budget_tracked_groups() == 0


def test_peek_falls_back_to_star_bucket_and_isolates_accounts_and_groups():
    B.note_reply_sent("a1", "g1@chatroom", T)
    assert B.peek_budget("a1", "g1@chatroom", CFG, T + 5)[0] is False, "同账号同群受限"
    assert B.peek_budget("a2", "g1@chatroom", CFG, T + 5)[0] is True, "换账号不串账"
    assert B.peek_budget("a1", "g2@chatroom", CFG, T + 5)[0] is True, "换群不串账"
    assert B.peek_budget(None, "g1@chatroom", CFG, T + 5)[0] is True, (
        "accountId 缺省退化为 * 桶，不与具名账号串账")


def test_seed_restores_counts_and_last_reply_but_not_last_human():
    """回填小时/天计数与最近发言时刻；``lastHumanAtSec`` 留空。"""
    n = B.seed_budget_states("acct-seed", [
        {"group_id": "gA@chatroom", "hour_count": 3, "day_count": 12, "last_sent_at": T - 400},
        {"group_id": "gB@chatroom", "hour_count": 0, "day_count": 0, "last_sent_at": None},
        {"group_id": "", "hour_count": 5, "day_count": 5, "last_sent_at": T},   # 脏数据必须跳过
    ], T)
    assert n == 2, "空 group_id 不计入"
    assert B.hf_budget_tracked_groups() == 2

    a = B.get_budget_state("acct-seed", "gA@chatroom", T + 1)
    assert (a.hour_count, a.day_count) == (3, 12)
    assert a.last_reply_at_sec == T - 400
    assert a.last_human_at_sec is None, "账本没有人类消息时刻 ⇒ 无法回填"

    # 回填后接着算：3 条 + 间隔已过 ⇒ 放行（额度确实被带入）
    assert B.check_budget(a, CFG, T + 1, T + 1) == (True, None)
    assert B.check_budget(replace(a, hour_count=8), CFG, T + 1, T + 1)[1] == B.REASON_HOUR, (
        "回填的小时额度必须真的生效（重启不清零 —— 否则「重启刷额度」成了后门）")


def test_seed_converges_dirty_numeric_values():
    """负数 / 小数脏值收敛为非负整数。"""
    B.seed_budget_states("acct-dirty",
                         [{"group_id": "g@chatroom", "hour_count": -3,
                           "day_count": 2.7, "last_sent_at": None}], T)
    st = B.get_budget_state("acct-dirty", "g@chatroom", T)
    assert st.hour_count == 0
    assert st.day_count == 2


def test_cache_registry_owns_budget_reset():
    """预算缓存必须登记在 :data:`CACHE_REGISTRY` 上。

    否则跨用例污染（前一个用例记的额度漂到后一个）—— 那种失败看起来像业务 bug。
    """
    from plugin.inbound.heartflow.runtime import CACHE_REGISTRY

    assert "budget" in CACHE_REGISTRY.names()
    B.note_reply_sent("a", "g", T)
    assert B.hf_budget_tracked_groups() == 1
    CACHE_REGISTRY.reset("budget")
    assert B.hf_budget_tracked_groups() == 0


# ===== 7. 源级接线 =====
#
# TS 的末节用 ``fnBody`` 切片 + 正则锁住"**实现了但没接上**"：预算闸必须在
# ``heartflow.check_gate`` 内、``minJudgeIntervalSec`` **之后**（否则便宜的
# 结构闸会被贵的 LLM 判定绕过并先花钱）；``noteHfReplySent`` 必须在 sent 分支内、
# ``setHfLedgerSuppressed`` **之前**（被抑制的消息不占额度，I5）；
# handler 必须记人类消息；预算不得回流 UI schema。
#
# **本节故意不译**：那些断言的主语（``heartflow.py`` / ``learn.py`` / handler）
# 在本文件写就时尚未移植。等它们落地后按同一意图补测 —— 在这里放个"永远 skip"
# 的壳只会制造**假绿**（跳过看起来像通过），比没有更糟。


# ===== 8. 配置脏值（B1）=====
#
# 这一节是 **py2 独有**：TS 那边靠 JS 的"比较不抛异常"天然兜住（``"abc" > 0`` 是
# ``false`` ⇒ 该闸不生效，但**不炸**）。py2 修前写的是裸 ``int(b.get("minGapSec"))``，
# 而 :func:`budget.resolve_budget` 是在**每条触发消息**上被调的（``heartflow.check_gate``）
# ⇒ 配置里一个字符串/None/NaN 就让每条消息在预算这一步抛一次 ValueError，
# 现象是心流**整条静默**（比"值算错了"贵得多，且日志只有一行 ValueError）。


@pytest.mark.parametrize("raw", [
    "abc", "", "180s", [], {"v": 180}, True, False,
    float("nan"), float("inf"), -float("inf"),
])
def test_dirty_numeric_config_falls_back_to_the_default(raw):
    """说不清是什么的值 ⇒ 回落**默认档**，且**不抛**。"""
    cfg = B.resolve_budget({"budget": {"minGapSec": raw, "maxPerHour": raw, "maxPerDay": raw}})
    assert cfg.min_gap_sec == D["minGapSec"]
    assert cfg.max_per_hour == D["maxPerHour"]
    assert cfg.max_per_day == D["maxPerDay"]


def test_a_dirty_value_falls_back_to_the_gate_not_to_unlimited():
    """回落的方向必须是"按默认档拦"，不是"不设限"——后者是静默放宽（最贵的方向）。"""
    cfg = B.resolve_budget({"budget": {"minGapSec": "abc"}})
    state = B.record_budget_reply(B.new_budget_state(T), T)
    assert B.check_budget(state, cfg, T + 10) == (False, B.REASON_GAP)
    assert B.check_budget(state, cfg, T + D["minGapSec"]) == (True, None)


def test_an_explicit_zero_still_means_that_gate_is_off():
    """``0`` 是**有意义的值**（TS 的守卫是 ``cfg.minGapSec > 0`` ⇒ 0 = 该闸不生效）。

    这一条挡住"顺手用 :func:`_pos` 统一兜底"的写法：``_pos(0, 180)`` 会把它悄悄改成
    180 —— 老板在配置里写"不限"，插件按 3 分钟拦，等于**背着配置收紧**。
    """
    cfg = B.resolve_budget({"budget": {"minGapSec": 0, "maxPerHour": 0, "maxPerDay": 0}})
    assert (cfg.min_gap_sec, cfg.max_per_hour, cfg.max_per_day) == (0, 0, 0)
    state = B.record_budget_reply(B.new_budget_state(T), T)
    assert B.check_budget(state, cfg, T + 1) == (True, None)


def test_a_dirty_value_says_so_in_the_log(caplog):
    """兜底就是静默降级 ⇒ 必须留一行 warn，否则"配置写错了"永远查不出来。"""
    B.reset_budget_cache()
    with caplog.at_level("WARNING"):
        B.resolve_budget({"budget": {"maxPerHour": "eight"}})
    assert any("maxPerHour" in r.getMessage() and "eight" in r.getMessage()
               for r in caplog.records), "脏值必须点名参数与值"


def test_repeated_dirty_config_warns_only_once(caplog):
    """热路径上每条消息调一次 ⇒ 告警必须去重（否则日志被刷爆，真信号被埋）。"""
    B.reset_budget_cache()
    with caplog.at_level("WARNING"):
        B.resolve_budget({"budget": {"maxPerDay": "x"}})
        B.resolve_budget({"budget": {"maxPerDay": "x"}})
    assert len([r for r in caplog.records if "maxPerDay" in r.getMessage()]) == 1


def test_a_missing_key_is_not_a_dirty_value(caplog):
    """没配这个键 = 用默认，**不是**脏值 —— 否则每个账号都刷一行假告警。"""
    B.reset_budget_cache()
    with caplog.at_level("WARNING"):
        B.resolve_budget({"budget": {"enabled": True}})
    assert not [r for r in caplog.records if "脏值" in r.getMessage()]


def test_reset_lets_the_warning_fire_again(caplog):
    """``reset_budget_cache``（测试隔离 / 配置热重载）也要清掉告警去重表。

    否则"改完配置还是老样子"就查不出来了：值换了、告警却因为**旧键**已报过而不再出声。
    """
    B.reset_budget_cache()
    B.resolve_budget({"budget": {"minGapSec": "zzz"}})     # 先烧掉那个去重键
    caplog.clear()
    B.reset_budget_cache()
    with caplog.at_level("WARNING"):
        B.resolve_budget({"budget": {"minGapSec": "zzz"}})
    assert [r for r in caplog.records if "zzz" in r.getMessage()], \
        "重置后同一个脏值要能再报一次"
