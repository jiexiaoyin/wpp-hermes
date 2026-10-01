"""心流标签判定回归 —— TS ``tests/unit/heartflow-label.test.mjs`` (v1.6.8) 的 pytest 译本。

## 为什么有这份测试（2026-09-26 老板拍板）

旧 ``engaged`` 判据 = 「该群 600s 内出现过任意人类群消息」⇒ 活跃群恒为真 ⇒ 接话率饱和
⇒ 每轮 sweep 都下调阈值 ⇒ **单调降到地板**（"不设下限就会一直降低"）。
新判据锚在 **bot 自己发的那条**：``quote``/``mention``/``negative``/``veto`` 为强信号
（**永远**可采信）· ``short-window``/``silence`` 为弱信号（需反事实基线过滤）。

## 与 TS 版的对应

=======  ====================================================================
1–4      本文件 §1–§4 一一对应（classify / 负词表 / 信号值域 / ambientP）
5        TS 的 §5 是**回归门**（饱和群不再下调阈值）与**源级守卫**
         （``bot_msg_id``/``engage_signal`` 的 DDL 落点、handler 接线）。
         前者的主语 ``eval_hf_threshold`` 在 ``learn.py``，尚未移植 ⇒ **不译**，
         等 ``learn.py`` 落地时按同一意图补测（见文末说明）。
=======  ====================================================================
"""

from __future__ import annotations

from plugin.inbound.heartflow import label as L

SENT = 1_000_000          # bot 发言时刻
LABEL = 60                # label_window_sec
OBS = 600                 # observe_window_sec


def cand(**kw) -> L.HfEngageCandidate:
    return L.HfEngageCandidate(at_sec=kw.pop("at_sec", SENT), **kw)


def classify(candidates, *, sent=SENT, label_win=LABEL, obs_win=OBS):
    return L.classify_hf_engagement(L.HfEngagementInput(
        sent_at_sec=sent, label_window_sec=label_win,
        observe_window_sec=obs_win, candidates=list(candidates)))


# ===== 1. classify：优先级与窗边界 =====


def test_quote_counts_as_engaged_and_closes():
    r = classify([cand(at_sec=SENT + 10, quotes_bot=True, text="这个价格还有吗")])
    assert (r.engaged, r.signal, r.close) == (1, "quote", True)


def test_mention_counts_as_engaged_and_closes():
    r = classify([cand(at_sec=SENT + 30, mentions_bot=True, text="@小助理 在吗")])
    assert (r.engaged, r.signal, r.close) == (1, "mention", True)


def test_quote_outranks_mention():
    """引用与 @ 同时存在 → quote 优先（引用带上下文，@ 可能只是叫人）。"""
    r = classify([
        cand(at_sec=SENT + 5, mentions_bot=True, text="@小助理"),
        cand(at_sec=SENT + 20, quotes_bot=True, text="同问"),
    ])
    assert r.signal == "quote", "quote 必须压过 mention"


def test_negative_outranks_everything():
    """负信号优先级最高 —— 即便同时有人引用，"别刷了"就是针对 bot 那条。"""
    r = classify([
        cand(at_sec=SENT + 5, quotes_bot=True, text="这个有货"),
        cand(at_sec=SENT + 40, quotes_bot=True, text="别刷了"),
    ])
    assert (r.engaged, r.signal, r.close) == (0, "negative", True), "negative 必须压过 quote"


def test_mention_with_negative_text_is_negative():
    r = classify([cand(at_sec=SENT + 8, mentions_bot=True, text="@小助理 别说话了")])
    assert (r.engaged, r.signal, r.close) == (0, "negative", True)


def test_bare_message_in_label_window_is_weak_and_keeps_window_open():
    """短窗内裸消息 → ``short-window``/1 / **不关窗**（等更强信号升级）。"""
    r = classify([cand(at_sec=SENT + 60, text="哈哈")])
    assert (r.engaged, r.signal, r.close) == (1, "short-window", False), "弱信号必须 close=False"


def test_late_quote_still_strong():
    """晚到的引用（超出弱窗但在观察窗内）仍是强正 —— **晚回也是真接话**。"""
    r = classify([cand(at_sec=SENT + 400, quotes_bot=True, text="刚看到")])
    assert (r.engaged, r.signal, r.close) == (1, "quote", True)


def test_bare_message_outside_label_window_is_no_verdict():
    """超出弱窗的裸消息不算弱信号（无结论，窗保持开着）。"""
    r = classify([cand(at_sec=SENT + 200, text="在的")])
    assert r == L.NO_VERDICT, "弱窗外的裸消息不得算接话"


def test_messages_outside_observe_window_are_ignored():
    """窗外消息一律忽略（早于 sentAt 或晚于 observeWindowSec）。"""
    early = classify([cand(at_sec=SENT - 1, quotes_bot=True, text="别刷了")])
    assert early.engaged is None, "早于 sentAt 的消息与本行无关"

    late = classify([cand(at_sec=SENT + OBS + 1, quotes_bot=True, text="别刷了")])
    assert late.engaged is None, "超出观察窗的消息与本行无关"


def test_window_boundaries_are_inclusive():
    """窗边界**含端点**（``atSec == labelEnd`` 算弱信号；``atSec == obsEnd`` 仍有效）。"""
    at_label_end = classify([cand(at_sec=SENT + LABEL, text="嗯")])
    assert at_label_end.signal == "short-window", "atSec == sentAt+labelWindowSec 必须算窗内"

    at_obs_end = classify([cand(at_sec=SENT + OBS, quotes_bot=True, text="回了")])
    assert at_obs_end.signal == "quote", "atSec == sentAt+observeWindowSec 必须算窗内"


def test_no_candidates_is_no_verdict():
    """无候选 → 无结论（``engaged=None``，不关窗，交给到期 silence）。"""
    assert classify([]) == L.NO_VERDICT


# ===== 2. 负词表（评估用，不是触发关键词）=====


def test_bare_messages_only_recognise_hard_phrases():
    """裸消息只认极少数硬词 —— 软词（"别说话了"）在裸消息上**不**判负（误报代价高）。"""
    assert L.is_hf_negative_text("别刷屏", False) is True, "硬词在裸消息上判负"
    assert L.is_hf_negative_text("别说话了", False) is False, "软词在裸消息上**不**判负"
    assert L.is_hf_negative_text("别说话了", True) is True, "软词在引用/@ 消息上判负"

    soft = classify([cand(at_sec=SENT + 30, text="别说话了")])
    assert soft.signal == "short-window", "软负词在裸消息上退化为弱正，不得判负"


def test_negative_phrases_are_normalised():
    """归一化忽略空白与大小写。"""
    assert L.is_hf_negative_text(" 别 刷 屏 ", False) is True, "空白必须被归一化掉"
    assert L.is_hf_negative_text("别 刷 屏", True) is True


def test_empty_or_unmatched_text_is_not_negative():
    assert L.is_hf_negative_text("", True) is False
    assert L.is_hf_negative_text("这个多少钱", True) is False
    assert L.is_hf_negative_text(None, True) is False, "None 不得炸（DB 里 text 可为 NULL）"


def test_hard_phrases_are_a_subset_of_the_soft_list():
    """硬词必须是全表的子集 —— 否则"裸消息判负"会比"引用消息判负"更宽松（方向反了）。"""
    assert set(L.HF_HARD_NEGATIVE_PHRASES) <= set(L.HF_NEGATIVE_PHRASES)


# ===== 3. 信号值域：旧行必须被排除 =====


def test_engage_signal_value_domain_is_pinned_literally():
    """值域必须**逐个字面写死**，不许 ``for s in HF_ENGAGE_SIGNALS`` 地自证。

    ⚠️ 这条是被变异测试逼出来的：原来写成"遍历 :data:`HF_ENGAGE_SIGNALS` 逐个校验"，
    看着很全，其实**期望值来自被测代码自己** —— 从元组里删掉 ``silence``，
    循环就少跑一圈，测试照样全绿。这正是"信号静默失效"那一类事故的形状
    （值域少一项 ⇒ 读回来是 ``None`` ⇒ 整行样本被当 legacy 丢掉，**无报错**）。
    """
    assert set(L.HF_ENGAGE_SIGNALS) == {
        "quote", "mention", "negative", "veto", "short-window", "silence"}


def test_as_engage_signal_rejects_legacy_and_unknown():
    """``NULL``/未知 → ``None``（v1.6.8 前的**旧错误标签**不得进学习样本）。"""
    assert L.as_hf_engage_signal(None) is None, "NULL 旧行 → None"
    assert L.as_hf_engage_signal("legacy") is None, "未知值 → None"
    assert L.as_hf_engage_signal("") is None
    assert L.as_hf_engage_signal(123) is None
    # 逐个**字面**断言（不是遍历常量 —— 见上一条 docstring）
    assert L.as_hf_engage_signal("quote") == "quote"
    assert L.as_hf_engage_signal("mention") == "mention"
    assert L.as_hf_engage_signal("negative") == "negative"
    assert L.as_hf_engage_signal("veto") == "veto"
    assert L.as_hf_engage_signal("short-window") == "short-window"
    assert L.as_hf_engage_signal("silence") == "silence"


def test_veto_is_a_strong_signal():
    """v1.8.0 一键否决：``veto`` 必须能落库读回，且**永远**可采信。

    漏了值域它会返回 ``None`` ⇒ 行被当 legacy 丢弃，veto 完全无效**且无报错**
    （静默失效，故单列一条断言）。
    """
    assert L.as_hf_engage_signal("veto") == "veto", "veto 必须进 HF_ENGAGE_SIGNALS 值域"
    assert sorted(L.HF_STRONG_SIGNALS) == ["mention", "negative", "quote", "veto"]
    assert L.is_hf_sample_informative("veto", 0.9, 0.5) is True, "veto 不受本底过滤"


def test_classify_never_emits_a_signal_outside_the_value_domain():
    """``classify`` 产出的每个 signal 都必须是 :data:`HF_ENGAGE_SIGNALS` 的成员。

    这条是防"加了新分支但忘了扩值域"：那种漏法**没有报错**，只是信号读回来变成
    ``None``、样本被静默丢掉（与 veto 那条同一个坑）。
    """
    seen: set[str] = set()
    cases = [
        [cand(at_sec=SENT + 1, quotes_bot=True, text="同问")],
        [cand(at_sec=SENT + 1, mentions_bot=True, text="@小助理")],
        [cand(at_sec=SENT + 1, quotes_bot=True, text="别刷了")],
        [cand(at_sec=SENT + 1, text="哈哈")],
    ]
    for cs in cases:
        sig = classify(cs).signal
        assert sig is not None
        seen.add(sig)
        assert L.as_hf_engage_signal(sig) == sig, f"{sig} 不在值域内"
    assert seen == {"quote", "mention", "negative", "short-window"}


# ===== 4. hfAmbientP + 可采信判定 =====


def test_ambient_p_poisson_monotonic_and_degenerate_inputs():
    assert L.hf_ambient_p(0, 14 * 3600, 120) == 0, "该时段无消息 → 0"
    assert L.hf_ambient_p(-3, 14 * 3600, 120) == 0, "负数 → 0"
    assert L.hf_ambient_p(10, 0, 120) == 0, "观察时长 0 → 0（防除零）"
    assert L.hf_ambient_p(float("nan"), 14 * 3600, 120) == 0, "NaN → 0"
    assert L.hf_ambient_p(10, float("inf"), 120) == 0, "∞ 时长 → 0"
    assert L.hf_ambient_p("10", 14 * 3600, 120) == 0, "非数 → 0"
    assert L.hf_ambient_p(True, 14 * 3600, 120) == 0, "bool 不算数（True 会被当 1 混进来）"

    # 口径：count = 该群**同一小时段**在 14 天里累计的入站人类消息数 ⇒ 每小时均条数 = count/14
    lo = L.hf_ambient_p(2 * 14, 14 * 3600, 120)       # 该时段 ~2 条/小时：冷清
    hi = L.hf_ambient_p(600, 14 * 3600, 120)          # 该时段 ~43 条/小时：话痨
    assert lo < hi, "消息越多 → ambientP 越大"
    assert 0.5 < hi < 1, f"话痨时段 ambientP 应 >0.5 且 <1（实测 {hi}）"
    assert lo < 0.5, f"冷清时段 ambientP 应 <0.5（实测 {lo}）"

    # 钉住 ambientMax=0.5 的运行含义（调这个旋钮时看这里）：约 21 条/小时 是该时段的分水岭
    assert L.hf_ambient_p(300, 14 * 3600, 120) > 0.5, "该时段 ~21 条/小时 已过线"
    assert L.hf_ambient_p(200, 14 * 3600, 120) < 0.5, "该时段 ~14 条/小时 未过线"


def test_ambient_p_non_finite_window_is_deterministic():
    """窗长非法时收敛成 0（**不**跟着变 NaN）。

    这是 Py 侧与 TS 的**已知分歧点**：``Math.max(0, NaN)`` 在 JS 里是 ``NaN``，
    而 ``max(0.0, nan)`` 在 Python 里返回 0.0。两边都"不会崩"，但结果不同，
    所以这里显式定义成"窗长无效 ⇒ 概率 0"，免得日后差分对不上时找不到原因。
    """
    assert L.hf_ambient_p(10, 14 * 3600, float("nan")) == 0.0
    assert L.hf_ambient_p(10, 14 * 3600, -5) == 0.0, "负窗长 → 0（等价于不观察）"
    assert L.hf_ambient_p(10, 14 * 3600, None) == 0.0


def test_is_sample_informative():
    MAX = 0.5
    for s in ("quote", "mention", "negative"):
        assert L.is_hf_sample_informative(s, 0.999, MAX) is True, f"{s} 在饱和群也必须采信"
    assert L.is_hf_sample_informative("short-window", 0.99, MAX) is False, "饱和群 short-window 不可采信"
    assert L.is_hf_sample_informative("silence", 0.99, MAX) is False, "饱和群 silence 不可采信"
    assert L.is_hf_sample_informative("short-window", 0.1, MAX) is True, "冷清群 short-window 可采信"
    assert L.is_hf_sample_informative("silence", 0.1, MAX) is True, "冷清群 silence 可采信"
    assert L.is_hf_sample_informative(None, 0.1, MAX) is False, "无信号（旧行）不可采信"
    assert L.is_hf_sample_informative("short-window", MAX, MAX) is False, (
        "恰等于 ambientMax 不算小于 → 不可采信（严格小于）")


# ===== 5. 回归门 / 源级守卫 =====
#
# TS 的 §5 有三条，主语都还没落地：
#   · ``evalHfThreshold`` 在饱和群上不再下调 —— 属于 ``learn.py``（第 6 步后段）
#   · ``bot_msg_id`` / ``engage_signal`` 的 DDL 落点 —— 属于 ``data/schema``（第 2 步已建表，
#     断言要改成对着 Py 侧的建表/补列代码，等 ``learn.py`` 定稿后一起做）
#   · handler 必须取开窗再判定、负词表不得进触发路径 —— 属于 ``platform/adapter.py``
#     与 ``heartflow.py``
#
# **故意不放"永远 skip"的壳**：跳过看起来像通过，比没有更糟。
# 负词表那条可以现在就钉（label 模块自身已就位），见下。
