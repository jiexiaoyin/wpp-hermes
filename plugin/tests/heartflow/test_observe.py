"""心流观测复盘 + 占比外环回归 —— TS ``tests/unit/heartflow-observe.test.mjs`` (305 行) 的 pytest 译本。

## 这一批最容易"假绿"的四处，各配一条**行为级**回归门

1. **占比的分子必须数得到 bot**：出站行的 ``chat_id`` 为 NULL，归群靠
   ``COALESCE(NULLIF(chat_id,''), peer_id)``。写错的话分子恒 0、占比永远显示 0%，
   **且不报任何错**（外环永不触发）⇒ 有一条 SQL 源级断言钉住它。
2. **日报的重复率必须与闸同源**（I4）：日报报出的每个百分点，换到今天都得是"闸会拦下"。
   两套口径 = 假收口 ⇒ 喂同一个素材给两侧，条数必须**相等**。
3. **占比逐样本用它自己那一小时的本底**（不是"当前小时"，也不是该群最大值）——
   v1.6.8 踩过的坑，见 :mod:`.label`。
4. **外环只能收紧**：与画像收紧复合后必须仍 ≤ 各自单独收紧的结果
   （历史教训：v1.6.6 正反馈飞轮）。

真实日期/生产数字一律不进本文件（公开仓红线）；样本全是构造的。

## 与 TS 版的对应

=======  ====================================================================
1–10     本文件 §1–§8 一一对应（指标 / 区间口径 / 接话率 / 重复率 / 外环 /
         复合 / 状态 / 复盘正文）；TS §2 与 §15（SQL 形状、``create_time`` 死列）
         归入本文件 §9 的**源级守卫**（Py 侧的主语是 :mod:`plugin.data.repo_messages`）。
11–14    出口与命令面的断言（``filehelper-features.ts`` / ``index.ts``）——
         主语是 ````plugin/inbound/filehelper*.py`` 与适配器，那两块还没移植。
15       见 §9。
=======  ====================================================================
"""

from __future__ import annotations

import ast
import sys
import re
from datetime import datetime
from pathlib import Path

import pytest

from plugin.data import repo_hf, repo_messages
from plugin.inbound.heartflow import budget as B
from plugin.inbound.heartflow import dedupe as D
from plugin.inbound.heartflow import layer as L
from plugin.inbound.heartflow import observe as O
from plugin.inbound.heartflow.runtime import CACHE_REGISTRY

OBSERVE_SRC = Path(O.__file__).read_text(encoding="utf-8")
MSGS_SRC = Path(repo_messages.__file__).read_text(encoding="utf-8")
HF_SRC = Path(repo_hf.__file__).read_text(encoding="utf-8")

#: 固定时刻（测试不依赖真实时钟）
NOW = int(datetime(2026, 9, 20, 20, 30).timestamp())

SG = O.resolve_hf_share_guard_cfg(None)
BASE = B.BudgetConfig(enabled=True, min_gap_sec=180, max_per_hour=8, max_per_day=60,
                      no_consecutive_without_human=True, quiet_hours=())


def at(h: int, m: int = 0, day: int = 5) -> int:
    """本地时刻 → unix 秒（用本地字段构造，与 ``hf_local_hour`` 同语义）。"""
    return int(datetime(2026, 1, day, h, m, 0).timestamp())


def vol(total: int, bot: int) -> O.HfGroupVolume:
    return O.HfGroupVolume(total=total, bot_sends=bot)


def as_budget(t: O.HfShareTighten) -> B.BudgetConfig:
    """收紧档 → 预算结构体（TS 里档位本身就是 ``HfBudgetConfig``，结构上可当 base 用）。"""
    return B.BudgetConfig(enabled=t.enabled, min_gap_sec=t.min_gap_sec,
                          max_per_hour=t.max_per_hour, max_per_day=t.max_per_day,
                          no_consecutive_without_human=t.no_consecutive_without_human,
                          quiet_hours=())


def sample(gid: str, signal: str | None, at_sec: int | None, engaged: int = 0) -> dict:
    """给 :func:`observe.hf_engagement_summary` 的行 —— 形状与
    ``repo_hf.list_closed_since`` 的 ``SELECT`` **逐列一致**（§9 有断言钉住）。"""
    return dict(group_id=gid, engage_signal=signal, engaged=engaged, sent_at=at_sec)


@pytest.fixture(autouse=True)
def _clean_state():
    """两处内存态都是模块级的：漏清的表现是"看起来像业务 bug"的跨用例污染。"""
    O.reset_hf_share_guard()
    D.reset_dedupe_store()
    yield
    O.reset_hf_share_guard()
    D.reset_dedupe_store()


# ===== 1. 纯指标 =====


def test_bot_share_denominator_is_all_group_messages():
    assert O.hf_bot_share(210, 30) == 30 / 240
    assert O.hf_bot_share(0, 0) == 0, "空群 ⇒ 0（不是 NaN / ∞）"
    assert O.hf_bot_share(0, 3) == 1, "群里只有 bot 说话 = 100% 占比（外环该管这种）"
    assert O.hf_bot_share(10, 0) == 0
    assert O.fmt_hf_share(210, 30) == "12.5% (30/240)", "格式必须带原始条数，口径不能藏起来"


def test_bot_share_never_divides_bot_by_human():
    """口径守卫：分母必须含 bot 自己。

    用 bot/人类 口径时 ``hf_bot_share(0, 3)`` 会除零，``hf_bot_share(210, 30)`` 会算出
    ``30/210``（放大 1.14 倍）—— 这个断言就是那道线。
    """
    assert O.hf_bot_share(10, 5) == pytest.approx(5 / 15)
    assert O.hf_bot_share(10, 5) != pytest.approx(5 / 10)


def test_fixed_follows_js_tofixed_not_python_format():
    """``toFixed`` 是 half-up on 二进制精确值；Python 的 ``format`` 在精确平局走银行家舍入。

    报告里的百分比 / lift 全从这里出，两条语义混用会让"和 TS 对不上"变成无谓的排查。
    """
    assert O._fixed(0.5, 0) == "1", 'JS (0.5).toFixed(0) === "1"；Python f"{0.5:.0f}" 是 "0"'
    assert f"{0.5:.0f}" == "0", "（对照组：Python 自己确实是银行家舍入）"
    assert O._fixed(2.675, 2) == "2.67", "2.675 的二进制精确值略小于 2.675"
    assert O._fixed(1.005, 2) == "1.00"
    assert O._fixed(12.5, 1) == "12.5"
    assert O._fixed(6.0, 1) == "6.0"
    assert O._fixed(300.0, 0) == "300"
    assert O._fixed(float("nan"), 1) == "NaN"
    assert O._fixed(float("inf"), 1) == "Infinity"


def test_engagement_lift_and_veto_rate_edges():
    assert O.hf_engagement_lift(0.3, 0.009) is None, "本底 <0.01 ⇒ 不算 lift（不吹成 33×）"
    assert O.hf_engagement_lift(0.3, 0.05) == pytest.approx(6), "0.3 / 0.05 = 6×"
    assert O.hf_engagement_lift(0.3, 0.01) == pytest.approx(30), "边界：恰好 0.01 仍算"
    assert O.hf_engagement_lift(0.3, 0) is None
    assert O.hf_engagement_lift(float("nan"), 0.5) is None
    assert O.hf_engagement_lift(0.3, "0.05") is None, "字符串不是数（TS 侧 Number.isFinite 同样拒绝）"
    assert O.hf_veto_rate(0, 0) is None, "无样本 ⇒ None（不是 0）"
    assert O.hf_veto_rate(2, 10) == pytest.approx(0.2)


# ===== 2. 接话率摘要（逐样本本底）=====


def test_engagement_summary_filters_by_each_sample_own_hour():
    """两条样本：夜里那条（本底 0，沉默可信）、白天那条（本底 0.9，沉默不可信）。

    拿"当前小时"或"该群最大本底"去过滤全部样本，会把夜里那批也判成不可采信
    —— 样本在不同时段分布时（7 天窗口必然如此）这等于把统计关上。
    """
    samples = [
        sample("g1", "silence", at(3, 0), 0),      # 夜里：本底 0 ⇒ 可采信
        sample("g1", "silence", at(15, 0), 0),     # 白天：本底 0.9 ⇒ 被过滤
    ]
    ambient = {"g1|3": 0.0, "g1|15": 0.9}
    s = O.hf_engagement_summary(samples, ambient, 0.5)
    assert s.usable == 1, "夜里那条可采信，白天那条被本底过滤"
    assert s.skipped == 1
    assert s.engaged == 0
    assert s.rate == 0, "可采信样本里 0 条被接话"
    assert s.ambient_p == 0, "本底均值只取可采信样本（夜里 = 0）"
    assert O.hf_engagement_summary([], ambient, 0.5).rate is None, "无样本 ⇒ None"


def test_engagement_summary_strong_signal_ignores_ambient():
    """强信号（quote/mention/negative/veto）在饱和群也算可采信 —— 有指向的行为不打折。"""
    s = O.hf_engagement_summary([sample("g1", "veto", at(15), 0)], {"g1|15": 5.0}, 0.5)
    assert s.usable == 1
    assert s.rate == 0, "veto = 强负 ⇒ engaged 0，但它是**有效样本**"
    assert s.ambient_p == 5.0, "强信号的本底照样进均值（lift 的分母是「可采信样本的本底均值」）"


def test_engagement_summary_drops_dirty_and_zero_timestamp():
    """``sent_at`` 为 0 / None / 脏值一律不算样本（与 :mod:`.layer` 同一取向）。

    ⚠️ 这里埋着本地时区陷阱：``sent_at = 0`` 在本机（CST）是 **08:00**，不是 00:00 ——
    只看"usable 少一条"是看不出来的（它会落进某个小时桶并拿到该桶的本底），
    所以下面直接断言它**没有**被算进任何统计。
    """
    samples = [
        sample("g1", "quote", None, 1),
        sample("g1", "quote", 0, 1),
        sample("g1", "quote", "123", 1),
        sample("g1", "quote", at(10), 1),
    ]
    s = O.hf_engagement_summary(samples, {"g1|10": 0.0}, 0.5)
    assert s.usable == 1
    assert s.skipped == 3
    assert s.engaged == 1, "只有 at(10) 那条进了统计"


def test_engagement_summary_engaged_must_be_strict_one():
    """``s.engaged === 1`` 是**严格**判据：Py 里 ``True == 1``，不挡住就会把布尔混进来。"""
    samples = [
        sample("g1", "quote", at(10), True),        # TS: true === 1 为 false
        sample("g1", "quote", at(11), 1),
        sample("g1", "quote", at(12), 2),           # TS: 2 === 1 为 false
    ]
    s = O.hf_engagement_summary(samples, {"g1|10": 0.0, "g1|11": 0.0, "g1|12": 0.0}, 0.5)
    assert s.usable == 3
    assert s.engaged == 1


# ===== 3. 重复率：与闸同源（I4）=====


def test_repeat_rate_counts_exactly_what_the_gate_would_block():
    """日报数出来的重复条数 **必须等于** 闸会拦下的条数。

    两套口径（日报一套、闸一套）= **假收口**：日报说"重复率降了"，实际闸一条没拦。
    所以这里把同一批素材按时间喂给闸，逐条比对。
    """
    cfg = D.resolve_dedupe_cfg(None)
    long_text = "这款手机现在活动价是3999元，支持分期免息，可以到店自提也可以发顺丰"
    items = [
        O.HfRepeatItem("g1", long_text, 1000),
        O.HfRepeatItem("g1", "另外提醒一下周末门店营业到晚上九点哦", 1100),   # 不同内容
        O.HfRepeatItem("g1", long_text, 1200),                                # 与第一条重复
        O.HfRepeatItem("g2", long_text, 1300),                                # 不同群 ⇒ 不算
        O.HfRepeatItem("g1", "好的", 1400),                                   # 短句豁免
    ]
    r = O.hf_repeat_rate(items, cfg)
    assert r.total == 5, "分母是素材总量（含被豁免的短句）"
    assert r.repeats == 1, "只有第 3 条算重复（同群 + 窗口内 + 相似）"
    assert r.prefix == 1
    assert r.similar == 0
    assert r.rate == pytest.approx(1 / 5)

    # 闸视角：按时间顺序喂给闸，应当恰好只在第 3 条命中
    blocked = 0
    for it in sorted(items, key=lambda i: i.at_sec):
        if D.check_hf_repeat("acct", it.group_id, it.text, cfg, it.at_sec):
            blocked += 1
        else:
            D.note_recent_reply("acct", it.group_id, it.text, it.at_sec, cfg)
    assert blocked == r.repeats, "闸拦下的条数必须等于日报的重复条数（两套口径 = 假收口）"


def test_repeat_rate_is_order_independent_of_input_order():
    """素材顺序由 DB 决定（``list_outbound_texts`` 返回升序）—— 但**别依赖**调用方的顺序。

    ⚠️ 这一条不能只用"相隔 100 秒的两条相同文本"：那种情况下**不排序也数得出来**
    （``now_sec - h.at_sec`` 是负数，窗口判定照样放行"未来"的那条）。要暴露"没排序"，
    必须用**跨窗口**的一对：先看新的再看旧的，旧的那条会被当成"未来"而错误放行 ⇒
    每次比对都算命中，日报的重复率于是**高于**闸真正会拦的条数（I4 破）。
    """
    cfg = D.resolve_dedupe_cfg(None)
    a = "这款手机现在活动价是3999元，支持分期免息，可以到店自提也可以发顺丰"
    near = [O.HfRepeatItem("g1", a, 100), O.HfRepeatItem("g1", a, 200)]
    assert O.hf_repeat_rate(near, cfg).repeats == 1
    assert O.hf_repeat_rate(list(reversed(near)), cfg).repeats == 1, "窗内：正反序都该数出来"

    far = [O.HfRepeatItem("g1", a, 0), O.HfRepeatItem("g1", a, 100_000)]     # 跨 6h 窗
    assert O.hf_repeat_rate(far, cfg).repeats == 0, "窗口外 ⇒ 不算重复（闸也不会拦）"
    assert O.hf_repeat_rate(list(reversed(far)), cfg).repeats == 0, "反序同样不算（必须先按时刻排序）"


def test_repeat_rate_groups_do_not_mix():
    """按群分组：同一个句子在两个群各说一次**不是**重复。"""
    cfg = D.resolve_dedupe_cfg(None)
    t = "这款手机现在活动价是3999元，支持分期免息，可以到店自提也可以发顺丰"
    r = O.hf_repeat_rate([O.HfRepeatItem("g1", t, 100), O.HfRepeatItem("g2", t, 200)], cfg)
    assert r.repeats == 0


def test_repeat_rate_rejects_non_numeric_timestamp():
    """脏 ``at_sec`` 当场炸，**不静默**（TS 那边 ``a.atSec - b.atSec`` 得到 NaN ⇒ 排序不确定）。

    两条起才比得上（单条不触发比较 —— 所以这里刻意放两条，否则这条断言是假绿）。
    """
    cfg = D.resolve_dedupe_cfg(None)
    t = "这款手机现在活动价是3999元，支持分期免息，可以到店自提也可以发顺丰"
    with pytest.raises(TypeError):
        O.hf_repeat_rate([O.HfRepeatItem("g1", t, 100),               # type: ignore[list-item]
                          O.HfRepeatItem("g1", t, None)], cfg)


# ===== 4. 外环判定 =====


def test_share_tighten_boundaries():
    assert O.hf_share_tighten(BASE, SG.target_share, vol(100, 20), SG) is None, "恰好 5% 不收紧"
    assert O.hf_share_tighten(BASE, 0.051, vol(39, 20), SG) is None, "消息数不够不收紧"
    assert O.hf_share_tighten(BASE, 0.051, vol(100, 4), SG) is None, "bot 条数不够不收紧"
    assert O.hf_share_tighten(BASE, 0.051, vol(100, 20),
                              O.resolve_hf_share_guard_cfg({"shareGuard": {"enabled": False}})) is None
    # 样本刚好够（不是"超过"）⇒ 有资格谈占比
    assert O.hf_share_tighten(BASE, 0.051, vol(40, 5), SG) is not None
    assert O.hf_share_tighten(BASE, float("nan"), vol(400, 60), SG) is None, "NaN 占比不收紧"
    assert O.hf_share_tighten(BASE, "0.5", vol(400, 60), SG) is None, "字符串占比不收紧"


def test_share_tighten_halves_with_caps():
    t = O.hf_share_tighten(BASE, 0.15, vol(240, 36), SG)
    assert t is not None
    assert (t.min_gap_sec, t.max_per_hour, t.max_per_day) == (360, 4, 30), "180→360 / 8→4 / 60→30"
    assert t.enabled is True


def test_share_tighten_respects_hard_caps_and_floors():
    huge = B.BudgetConfig(enabled=True, min_gap_sec=1200, max_per_hour=1, max_per_day=5,
                          no_consecutive_without_human=True, quiet_hours=())
    t = O.hf_share_tighten(huge, 0.5, vol(500, 200), SG)
    assert t.min_gap_sec == O.HF_SHARE_MIN_GAP_CAP_SEC, "间隔封顶 900s（不能收紧到半小时不说话）"
    assert t.max_per_hour == O.HF_SHARE_MAX_PER_HOUR_FLOOR, "每小时 ≥2 条"
    assert t.max_per_day == O.HF_SHARE_MAX_PER_DAY_FLOOR, "每天 ≥10 条"


def test_share_tighten_unlimited_base_still_tightens():
    """``0`` = 不限：必须用 floor，**不能** ``0÷2=0``。

    否则 :func:`budget.tighten_budget` 会把 0 当成"未指定"回落成原值 ——
    外环**静默失效**，而不限额的账号恰恰最需要这个外环。
    """
    unlimited = B.BudgetConfig(enabled=True, min_gap_sec=0, max_per_hour=0, max_per_day=0,
                               no_consecutive_without_human=True, quiet_hours=())
    t = O.hf_share_tighten(unlimited, 0.2, vol(500, 100), SG)
    assert t.max_per_hour == O.HF_SHARE_MAX_PER_HOUR_FLOOR
    assert t.max_per_day == O.HF_SHARE_MAX_PER_DAY_FLOOR
    assert t.min_gap_sec == 0, "原本不限间隔 ⇒ 0×2 = 0（仍不限，但每小时/每天已封住）"


def test_share_tighten_carries_base_consecutive_flag():
    """``noConsecutiveWithoutHuman`` 不是收紧旋钮，是**继承** base 的（漏继承会改变触发形状）。"""
    off = B.BudgetConfig(enabled=True, min_gap_sec=180, max_per_hour=8, max_per_day=60,
                         no_consecutive_without_human=False, quiet_hours=())
    assert O.hf_share_tighten(off, 0.5, vol(500, 200), SG).no_consecutive_without_human is False
    assert O.hf_share_tighten(BASE, 0.5, vol(500, 200), SG).no_consecutive_without_human is True


# ===== 5. 复合：外环 ∩ 画像（只收紧）=====


def test_compose_share_and_profile_tighten_is_order_independent():
    share = O.hf_share_tighten(BASE, 0.15, vol(400, 60), SG)
    profile = {"enabled": True, "minGapSec": 300, "maxPerHour": 3, "maxPerDay": 40}
    a = B.tighten_budget(BASE, share.as_dict())            # 外环
    b = B.tighten_budget(BASE, profile)                    # 画像
    both = B.tighten_budget(a, profile)                    # 外环 ∩ 画像
    both2 = B.tighten_budget(b, share.as_dict())           # 顺序无关
    assert both == both2, "两层收紧与顺序无关"
    assert both.min_gap_sec >= max(a.min_gap_sec, b.min_gap_sec), "间隔取更严"
    assert both.max_per_hour <= min(a.max_per_hour, b.max_per_hour), "上限取更小"
    assert both.max_per_day <= min(a.max_per_day, b.max_per_day), "上限取更小"
    assert both.max_per_hour <= BASE.max_per_hour, "永不放宽"
    assert both.quiet_hours == BASE.quiet_hours, "静默段默认不并入（见 tighten_budget）"


def test_compose_never_lets_a_looser_layer_win():
    """反向：外环档位绝不能让预算变松（即便画像档更松）。"""
    share = O.hf_share_tighten(BASE, 0.15, vol(400, 60), SG)
    loose = {"enabled": True, "minGapSec": 10, "maxPerHour": 99, "maxPerDay": 999}
    composed = B.tighten_budget(as_budget(share), loose)
    assert composed.min_gap_sec == 360, "更松的画像档不生效"
    assert composed.max_per_hour == 4
    assert composed.max_per_day == 30


def test_share_tighten_may_exceed_base_floor_but_compose_clamps():
    """**为什么 I2 断言不能写在 :func:`observe.hf_share_tighten` 里**。

    floor 是硬边界：``base.max_per_hour=1`` 时档位是 ``2`` —— 比 base **大**。
    哨兵式的 not-looser 断言会把这条**合法**档位判成违规；真正的"只收紧"发生在复合点
    （``budget.tighten_budget`` 取更严的一侧并内置 I2 断言）。
    """
    strict = B.BudgetConfig(enabled=True, min_gap_sec=180, max_per_hour=1, max_per_day=5,
                            no_consecutive_without_human=True, quiet_hours=())
    t = O.hf_share_tighten(strict, 0.5, vol(500, 200), SG)
    assert t.max_per_hour == 2, "档位比 base 松（floor 兜底）"
    composed = B.tighten_budget(strict, t.as_dict())
    assert composed.max_per_hour == 1, "复合后仍取更严的一侧 ⇒ 净效果只收紧"
    assert composed.max_per_day == 5


# ===== 6. 外环状态与快照 =====


def test_share_state_expires_at_local_day_rollover():
    t0 = int(datetime(2026, 5, 10, 14, 0).timestamp())
    t1 = int(datetime(2026, 5, 11, 9, 0).timestamp())      # 次日
    O.note_hf_group_share("acct", "g1", t0, vol(400, 60), 0.15)
    hit = O.get_hf_share_tighten("acct", "g1", BASE, None, t0)
    assert hit is not None, "当日必须收紧"
    assert hit.max_per_hour == 4
    assert O.get_hf_share_tighten("acct", "g1", BASE, None, t1) is None, "跨日 ⇒ 无数据，不收紧"
    assert O.get_hf_share_tighten("acct", "g2", BASE, None, t0) is None, "没数据的群不收紧"
    assert O.get_hf_share_tighten("other", "g1", BASE, None, t0) is None, "别的账号不串"


def test_share_snapshot_lists_only_hits_sorted_by_share():
    t0 = int(datetime(2026, 5, 10, 14, 0).timestamp())
    O.note_hf_group_share("acct", "low", t0, vol(400, 30), 0.075)
    O.note_hf_group_share("acct", "high", t0, vol(400, 120), 0.30)
    O.note_hf_group_share("acct", "ok", t0, vol(400, 12), 0.03)       # 未超目标
    O.note_hf_group_share("other", "high", t0, vol(400, 120), 0.30)   # 别的账号
    snap = O.hf_share_guard_snapshot("acct", BASE, None, t0)
    assert [h.group_id for h in snap] == ["high", "low"], "按占比降序，且不含未命中的群"
    assert snap[0].to.max_per_hour == 4, "0.3 ⇒ 8÷2 = 4（比 floor 严，取更严的一侧）"
    assert snap[1].to.max_per_hour == 4, "0.075 ⇒ 同样 8÷2 = 4"
    assert snap[0].from_ is BASE, "快照要带上收紧前的档（报告显示 180→360s 用）"
    assert O.hf_share_guard_snapshot("acct", BASE, None, t0 + 86400 * 2) == [], "跨日快照为空"


def test_share_reset_clears_state():
    t0 = int(datetime(2026, 5, 10, 14, 0).timestamp())
    O.note_hf_group_share("acct", "g1", t0, vol(400, 60), 0.15)
    O.reset_hf_share_guard()
    assert O.hf_share_guard_snapshot("acct", BASE, None, t0) == []


def test_share_window_start_is_local_midnight():
    t0 = int(datetime(2026, 5, 10, 14, 37).timestamp())
    assert O.hf_share_guard_window_start(t0) == B.hf_day_start_sec(t0)
    assert O.hf_share_guard_window_start(t0) == int(datetime(2026, 5, 10, 0, 0).timestamp())


# ===== 7. 配置面 =====


def test_share_guard_defaults_and_resolution():
    assert O.HF_SHARE_GUARD_DEFAULTS == {"enabled": True, "targetShare": 0.05,
                                         "minMsgs": 40, "minBotSends": 5}
    assert SG == O.HfShareGuardConfig(enabled=True, target_share=0.05,
                                      min_msgs=40, min_bot_sends=5)
    r = O.resolve_hf_share_guard_cfg({"shareGuard": {"targetShare": 0.08, "minMsgs": 100,
                                                     "minBotSends": 9, "enabled": False}})
    assert (r.target_share, r.min_msgs, r.min_bot_sends, r.enabled) == (0.08, 100, 9, False)
    assert O.resolve_hf_share_guard_cfg({"shareGuard": {"targetShare": 1}}).target_share == 1, \
        "上界 1 合法（1 = 100% 才收紧）"


@pytest.mark.parametrize("bad", [0, -0.1, 1.5, "0.05", None, True, float("nan")])
def test_share_guard_bad_target_falls_back(bad):
    """越界/脏值一律回落缺省 —— 别让 ``targetShare: 1.5`` 变成"永不收紧"（那是最坏的失效方向）。"""
    assert O.resolve_hf_share_guard_cfg({"shareGuard": {"targetShare": bad}}).target_share == 0.05


@pytest.mark.parametrize("bad", [-1, "40", None, float("inf"), True])
def test_share_guard_bad_volume_floor_falls_back(bad):
    r = O.resolve_hf_share_guard_cfg({"shareGuard": {"minMsgs": bad, "minBotSends": bad}})
    assert (r.min_msgs, r.min_bot_sends) == (40, 5)


def test_share_guard_accepts_missing_section_and_dirty_shape():
    assert O.resolve_hf_share_guard_cfg(None).enabled is True
    assert O.resolve_hf_share_guard_cfg({}).enabled is True
    assert O.resolve_hf_share_guard_cfg({"shareGuard": None}).enabled is True, "None ⇒ 与没配同义"
    assert O.resolve_hf_share_guard_cfg({"shareGuard": "nonsense"}).min_msgs == 40, \
        "整段是脏类型 ⇒ 全部回落缺省（不抛错，报告/门禁都不该因此崩）"


# ===== 8. 复盘正文 =====


def digest_input(**over) -> O.HfDigestInput:
    base = dict(
        now_sec=NOW,
        days=7,
        shares=[O.HfDigestShare("groupA", 210, 30)],
        engagement=O.HfEngagementSummary(usable=40, skipped=10, engaged=12, rate=0.3,
                                         ambient_p=0.05),
        stopped=O.HfStoppedStat(negative=1, veto=2, total=20),
        repeat=O.HfRepeatStat(total=30, repeats=6, rate=6 / 30, prefix=4, similar=2),
        repeat_sample_total=30,
        layers=[O.HfDigestLayer("groupA", "18-24", 40, 4, 0.1, 0.7, False)],
        budget={"budget-gap": 12, "budget-hour": 3},
        share_guard=[O.HfShareGuardHit("groupA", 30 / 240, 240, 30, BASE,
                                       O.HfShareTighten(360, 4, 30, True))],
    )
    base.update(over)
    return O.HfDigestInput(**base)


def test_digest_has_all_sections_with_caliber_labels():
    text = O.build_hf_digest(digest_input())
    for sec in ("【发言占比】", "【接话率】", "【被制止率】", "【重复率】",
                "【分层 群×时段】", "【预算拦截】", "【占比外环】"):
        assert sec in text, f"缺小节: {sec}"
    assert re.search(r"ts 口径", text), "占比/重复率必须标 ts 口径"
    assert re.search(r"judged_at 口径", text), "被制止率必须标 judged_at 口径（与上两节不同窗）"
    assert re.search(r"lift 6\.0×", text), "本底 0.05 ⇒ lift = 0.3/0.05 = 6.0×"
    assert re.search(r"未生效", text), "影子分层必须标「未生效」"
    assert re.search(r"gap 12", text), "预算拦截计数要显示"
    assert "30/240" in text, "占比必须带原始条数"
    assert "本底 0.050" in text, "本底固定 3 位小数"
    assert "veto 占比 10.0%" in text, "veto 占比是百分比（2/20 ⇒ 10.0%）"
    assert "rate 0.30" in text, "接话率固定 2 位小数"
    assert "rate=0.100" in text and "建议 0.70" in text, "分层行的小数位固定"
    assert "20% (6/30)" in text, "重复率按百分之整数显示"
    assert "间隔 180→360s" in text and "8→4 条/小时" in text, "外环必须显示收紧前后"
    assert len(text) <= O.HF_DIGEST_MAX_CHARS


def test_digest_soft_branches_when_everything_is_empty():
    text = O.build_hf_digest(digest_input(
        shares=[], layers=[], share_guard=[],
        engagement=O.HfEngagementSummary(0, 0, 0, None, 0.0),
        stopped=O.HfStoppedStat(0, 0, 0),
        repeat=O.HfRepeatStat(0, 0, 0.0, 0, 0),
        budget={},
    ))
    assert "(窗口内无群消息)" in text
    assert "(当前时段无分层样本)" in text
    assert "【预算拦截】无 " in text
    assert "【占比外环】未触发 (无群超目标占比)" in text
    assert "lift -(本底过冷, 不计算)" in text, "无可采信样本 ⇒ lift 不算（而不是 0×）"
    assert "rate -" in text
    assert "veto 占比" not in text, "无样本 ⇒ 不显示占比（hf_veto_rate 返回 None）"


def test_digest_collapses_extra_groups_and_is_bounded():
    """列群的上限必须**分节**钉住。

    早先这里只用一条 ``…其余 192 群`` 做断言，看着挺具体，其实**匹配到的是分层那一节**
    的折叠行（两节文案一模一样）—— 于是"占比那一节的上限失效、200 个群全列"这种变异
    照样过。教训：**同一段文案出现在两处时，正则不是在断言，是在撞运气**。所以这里让
    两节用不同的群名前缀 + 不同的条数，再直接断言"第 9 个群名不得出现在正文里"。
    """
    big = digest_input(
        shares=[O.HfDigestShare(f"s{i}@chatroom", 100, 30) for i in range(200)],
        layers=[O.HfDigestLayer(f"l{i}@chatroom", "18-24", 90, 9, 0.1, 0.7, True)
                for i in range(50)],
        share_guard=[O.HfShareGuardHit(f"g{i}@chatroom", 0.2, 400, 80, BASE,
                                       O.HfShareTighten(360, 4, 30, True)) for i in range(50)],
    )
    text = O.build_hf_digest(big)
    assert len(text) <= O.HF_DIGEST_MAX_CHARS, f"硬 cap 必须生效（实测 {len(text)}）"
    assert re.search(r"…其余 192 群", text), "占比节：最多列 8 个群（200-8），其余折叠"
    assert re.search(r"…其余 42 群", text), "分层节：最多列 8 个群（50-8），其余折叠"
    assert "s7@chatroom" in text and "s8@chatroom" not in text, "第 9 个群名不得列出"
    assert "l7@chatroom" in text and "l8@chatroom" not in text, "第 9 个群名不得列出"


@pytest.mark.parametrize("cap", [200, 260, 330, 400, 500, 640, 800, 1000, 1300, 1600, 2000, 2400])
def test_digest_global_cap_truncates_whole_lines(monkeypatch, cap):
    """全局硬 cap 的分支**在正常输入下走不到**（逐节 700 已把总长钉在 ~2500），
    所以把 cap 调小来真跑它 —— 否则那几行是死代码，写错了也没人知道。

    截断必须**整行丢**（留下半句话会被误读），且结果长度不得越过 cap。
    """
    big = digest_input(
        shares=[O.HfDigestShare(f"group{i}@chatroom", 100, 30) for i in range(60)],
        layers=[O.HfDigestLayer(f"group{i}@chatroom", "18-24", 90, 9, 0.1, 0.7, True)
                for i in range(60)],
    )
    monkeypatch.setattr(O, "HF_DIGEST_MAX_CHARS", 10 ** 6)
    full = O.build_hf_digest(big)
    monkeypatch.setattr(O, "HF_DIGEST_MAX_CHARS", cap)
    text = O.build_hf_digest(big)
    assert len(text) <= cap, f"截断后仍越界（{len(text)} > {cap}）"
    if len(full) <= cap:
        assert text == full, f"没超 cap 就不该截断（cap={cap}）"
        return
    assert text.endswith("…(已截断)"), "被截断必须留标记（否则读者以为报告就这么短）"
    kept = text.splitlines()[:-1]
    assert kept == full.splitlines()[:len(kept)], "截断只能是「正文的一个前缀」——不许切半行"


# ===== 9. 源级接线守卫（observe 侧）=====


def test_observe_never_imports_learn_layer_or_profile():
    """成环会把加载期异常吞成 WARNING ⇒ 真实表现是**整批工具静默不注册**（F5 的形态）。

    ``observe`` 站在最下游：它只吃现成数据（DB 素材由 sweep 填，判定只读内存），
    没有任何反向依赖的理由。
    """
    forbidden = {"learn", "layer", "profile", "heartflow"}
    tree = ast.parse(OBSERVE_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[-1] not in forbidden, f"不许 import {a.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert mod.split(".")[-1] not in forbidden, f"不许 from {mod} import ..."
            for a in node.names:
                assert a.name not in forbidden, f"不许 from . import {a.name}"


def test_module_level_imports_have_no_third_party_dependency():
    """加载期只许 stdlib + 本包相对导入（本模块在注册链上，见 F5）。"""
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    tree = ast.parse(OBSERVE_SRC)
    top: list[str] = []
    for node in tree.body:                          # 只看模块顶层，不看函数体
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top.append("." * node.level + (node.module or ""))
    assert top, "切片自检：模块级导入必须被扫到（否则这条断言是假绿）"
    for name in top:
        assert name.startswith(".") or name.split(".")[0] in stdlib, f"模块级第三方依赖: {name}"


def test_judge_hot_path_get_share_tighten_has_no_io():
    """判定读口体内无导入 / 无 SQL / 无 await（judge 热路径零 DB IO）。"""
    start = OBSERVE_SRC.index("def get_hf_share_tighten(")
    rest = OBSERVE_SRC[start + 10:]
    body = rest[:rest.index("\ndef ")]
    assert len(body) > 200, "切片自检：函数体必须被切出来（否则这条断言是假绿）"
    for banned in ("import ", "SELECT", "get_conn", "await ", "repo_"):
        assert banned not in body, f"判定体内不得出现 {banned!r}"
    assert "_share" in body, "判定必须读内存状态"
    assert "hf_share_tighten(" in body, "判定必须走同一个纯函数"


def _fn_body(src: str, name: str) -> str:
    rest = src[src.index(f"def {name}("):]
    return rest[:rest.index("\ndef ", 1)]


def test_bot_share_sql_counts_bot_via_coalesce():
    """占比分子必须数得到 bot：出站行 ``chat_id`` 恒 NULL，归群靠 COALESCE 兜底。

    写错的表现是**分子恒 0、占比永远 0%**，而且**不报任何错**（外环永不触发）。
    """
    body = _fn_body(MSGS_SRC, "list_bot_msg_share")
    assert "COALESCE(NULLIF(chat_id, ''), peer_id)" in body, "必须先按 chat_id 再回落 peer_id 归群"
    assert "GROUP BY gid, direction" in body, "必须按群 × 方向分组（分子 = outbound）"
    assert "peer_kind = 'group'" in body, "只统计群聊（私聊会污染分母）"
    assert "JOIN" not in body.upper(), "单表（跨表 collation 冲突）"
    assert "create_time" not in body


def test_repeat_material_sql_is_outbound_only_and_grouped():
    body = _fn_body(MSGS_SRC, "list_outbound_texts")
    assert "COALESCE(NULLIF(chat_id, ''), peer_id)" in body, "重复率素材也必须 COALESCE 归群"
    assert "direction = 'outbound'" in body, "素材只取出站（bot 自己说的话）"
    assert "UNIX_TIMESTAMP(ts) AS at_sec" in body, "报的必须是**绝对 epoch**"
    assert "JOIN" not in body.upper(), "单表"


@pytest.mark.parametrize("fn", ["list_group_msg_hour_buckets", "list_outbound_texts"])
def test_time_aggregates_use_ts_not_legacy_create_time(fn):
    """``create_time`` 是遗留死列：2026-08-11 前的值是 ``YYYYMMDDHHMMSS``（**不是** epoch），
    之后全 NULL。拿它当秒用会把那些行算成公元 643000 年 ⇒ 分桶/排序/窗口全错且**不报错**。
    """
    body = _fn_body(MSGS_SRC, fn)
    assert "UNIX_TIMESTAMP(ts)" in body, f"{fn} 必须用 UNIX_TIMESTAMP(ts) 取绝对 epoch"
    assert "create_time" not in body, f"{fn} 不得再引用 create_time"


def test_row_shape_matches_the_real_query():
    """假行的列必须与 ``list_closed_since`` 的 ``SELECT`` **逐列一致**。

    少一列会被静默补成默认值（``engage_signal=None`` ⇒ 整行被当 legacy 丢掉），
    于是**所有用假行的用例一起失真**却仍然全绿 —— 那是本仓最怕的"绿色但不是证据"。
    """
    body = _fn_body(HF_SRC, "list_closed_since")
    assert set(sample("g", "quote", 1)) == {"group_id", "engage_signal", "engaged", "sent_at"}
    for col in set(sample("g", "quote", 1)):
        assert col in body, f"假行的列 {col!r} 不在真查询里（两边已漂移）"


def test_pinned_shared_constants():
    """跨模块/跨实现共用的数字**逐个字面钉住** —— 一侧改动、另一侧没跟上就是静默失效。"""
    assert O.HF_SHARE_MIN_GAP_CAP_SEC == 900
    assert O.HF_SHARE_MAX_PER_HOUR_FLOOR == 2
    assert O.HF_SHARE_MAX_PER_DAY_FLOOR == 10
    assert O.HF_DIGEST_MAX_CHARS == 3500
    assert O.HF_DIGEST_MAX_GROUPS == 8
    assert O._SECTION_CAP == 700
    assert O._BUDGET_REASONS == (B.REASON_GAP, B.REASON_HOUR, B.REASON_DAY,
                                 B.REASON_CONSECUTIVE, B.REASON_QUIET_HOURS)


def test_budget_reason_keys_match_budget_module():
    """报告里那五个键就是 :mod:`.budget` 的 ``REASON_*``，**不许另抄一份字面量**。

    （所以除了比数值，还断言引用的是常量名 —— 抄一份字面量在数值上完全等价，
    只在"哪天改常量"时才炸，而那正是这条要挡的。）

    ⚠️ 断言必须落在 ``_BUDGET_REASONS`` **那一行**上：如果只查"这五个名字在源码里
    出现过"，``from .budget import (REASON_GAP, …)`` 那一段就把变异放过去了 ——
    名字确实还在文件里，只是元组换成了字面量。
    """
    assert B.hf_budget_blocked_snapshot() == {}
    assert len(set(O._BUDGET_REASONS)) == 5
    assign = next(line for line in OBSERVE_SRC.splitlines()
                  if line.startswith("_BUDGET_REASONS"))
    for name in ("REASON_GAP", "REASON_HOUR", "REASON_DAY", "REASON_CONSECUTIVE",
                 "REASON_QUIET_HOURS"):
        assert name in assign, (
            f"必须引用 budget 的 {name} 而不是另抄字面量；实测该行为 {assign.strip()!r}")


def test_no_real_group_ids_in_sources():
    """R2 红线：群 ID 形态可断言；门店/品牌/人名**不能**写在这里。

    （真群 ID 由发布链的脱敏规则门负责；这里的用例一律 ``g1`` / ``groupA``。）
    """
    pat = re.compile(r"\d{9,}@chatroom")
    for src in (OBSERVE_SRC, Path(__file__).read_text(encoding="utf-8")):
        assert not pat.search(src)


def test_cache_is_registered_in_registry():
    assert "observe" in CACHE_REGISTRY.names()
    CACHE_REGISTRY.reset("observe")
    assert "layer" in CACHE_REGISTRY.names(), "regression: 登记表是七处共用的，别覆盖掉别人"


def test_observe_does_not_duplicate_the_layer_module_lookback():
    """观测拿的是**现成数据**（sweep 写好、判定只读内存），不该自己去取数 ——
    否则同一份口径会出现两处实现（``HF_AMBIENT_LOOKBACK_DAYS`` 只该住在 :mod:`.layer`）。
    """
    assert not hasattr(O, "HF_AMBIENT_LOOKBACK_DAYS")
    assert L.HF_AMBIENT_LOOKBACK_DAYS == 14


# ===== 10. 未译部分（等 ``learn.py`` / ``heartflow.py`` / 命令面落地）
#
# TS §11–§14 的主语都还没到：
#   · ``/heartflow report`` 的出口（filehelper 的注入 ``send``、不走 ``sendAiReply``、
#     天数钳到 1..30、素材上限 2000 条）—— 命令面（``inbound/filehelper*.py``）
#   · ``triggers`` 侧把外环收紧档复合进预算（``tighten_hf_budget(budget_base, share)``）
#     且必须在 ``check_heartflow_gate`` **之前**算完 —— gate/triggers 移植时
#   · ``learn.ts`` 的 sweep 里 ``maybe_recompute_hf_share_guard``（当日窗口 = 本地零点、
#     裁剪重复历史、顺序在 ``learning.enabled`` 判定之前）—— ``learn.py``
#   · ``heartflow.ts`` 的 ``shareGuard?:`` 只做类型引用（不进 ``defaultHeartflowConfig``）
#     —— ``heartflow.py``
#
# 另外记一条**跨模块**的差异（本轮只修了 observe 这一处）：
#   ``{"shareGuard": "nonsense"}`` 这种**整段脏类型**，TS 取属性拿到 ``undefined`` ⇒
#  回落默认；Py 侧 ``(cfg or {}).get("x") or {}`` 之后直接 ``.get`` 会 ``AttributeError``。
#   :mod:`.observe` 已按"回落默认"处理（见 ``resolve_hf_share_guard_cfg`` 的 docstring），
#   但 :mod:`.budget` / :mod:`.dedupe` / :mod:`.layer` 的同名解析函数仍是直接 ``.get``。
#   触发条件是"手写脏配置"，影响是**入站消息判定路径上抛异常**（比用默认值严重）。
#   计划在第 7 步（配置面回归）统一收口，别在这里悄悄改三个模块然后忘了它们的变异门。
#
# **故意不放"永远 skip"的壳**：跳过看起来像通过，比没有更糟。
