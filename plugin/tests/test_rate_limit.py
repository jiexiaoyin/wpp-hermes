"""R1 · 触发限速闸（``plugin/inbound/rate_limit.py``）。

## 这一面的失效形态（为什么判据都下在"还让不让过"上）

这是 2026-10-03 08:39–08:43「两个机器人互相 @ 死循环」的止血闸（4 分钟刷 24 条，
每 10~15 秒一轮，最后靠人工停网关）。它能坏的方向**全都是安静的**：

* **闸没接上 / 判了不丢** ⇒ 回路照旧（而平时看不出任何异常，因为没有回路时它本来就不吭声）；
* **静默期内继续计数** ⇒ 持续来刷 = 无限续期 = "永久封禁"，比死循环更难发现；
* **窗口/上限的搭配让闸空转**（稳态计数恒差一次，见 ``test_a_loop_dies_inside_one_mute_window``）
  ⇒ 这就是本项目最讨厌的形态：装了个闸，看着正常，一次都不拦；
* **拿不到发送者时用空 key 建桶** ⇒ 一个群里**所有人共享一个额度**，静默起来一锅端；
* **桶无界增长** ⇒ 长跑进程的内存缓慢泄漏（现场只表现为"跑久了变慢"）。

⇒ 断言全是"第几次被拒、静默多久、别人的额度有没有被牵连"，不是"调用了没"。
"""

from __future__ import annotations

import logging
import time

import pytest

from plugin.inbound.rate_limit import (
    GUARDED_REASONS, MAX_TRIGGERS, MUTE_S, WINDOW_S, Decision, TriggerRateLimiter,
)
from plugin.inbound.triggers import ALLOW_REASONS

#: 事故现场：回路每 10~15 秒一轮（2026-10-03 08:39–08:43 实测）
_LOOP_ROUND_S = 15.0


class _Clock:
    """假时钟 —— 静默 180 秒的用例不该真 sleep 180 秒。"""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, d: float) -> float:
        self.t += d
        return self.t


def _limiter(**kw) -> tuple[TriggerRateLimiter, _Clock]:
    clock = _Clock()
    kw.setdefault("clock", clock)
    return TriggerRateLimiter(**kw), clock


# ==================================================== 1. 归它管 / 不归它管

def test_only_the_two_hundred_percent_paths_are_guarded():
    """受管的就是那两条「100% 必回」的硬触发（别的路一个都不碰）。

    ⚠️ 这条同时钉住**字符串的真实性**：两个 reason 必须是 ``triggers`` 里登记过的
    ALLOW reason —— 写成 ``group_mentions`` 这种错字，闸就再也锁不上任何东西，
    而现场没有任何异常（退化成"受管集合为空"）。
    """
    assert GUARDED_REASONS == {"group_mention", "group_solitaire"}
    assert GUARDED_REASONS <= ALLOW_REASONS, "受管 reason 必须是触发判定真的会返回的那些"


@pytest.mark.parametrize("reason", ["dm", "group_msg_type", "group_quote_bot",
                                    "group_keyword", "heartflow_candidate", "self",
                                    "red_packet", "没人见过的-reason"])
def test_unguarded_reasons_pass_and_leave_no_state(reason: str):
    """不受管的 reason：**永远放行，且一个桶都不建**（别让无关流量把内存撑起来）。"""
    limiter, _ = _limiter()
    for _ in range(MAX_TRIGGERS + 10):
        assert limiter.guard("a", "g", "wxid_x", reason).allowed is True
    assert limiter.stats()["buckets"] == 0, "不受管的 reason 不许建桶"


def test_a_missing_sender_never_creates_a_shared_bucket():
    """拿不到发送者 ⇒ 放行且不成桶。

    ⚠️ 反面就是两个真事故：拿空串当 key ⇒ 一个群里**所有人共享一个额度**
    （不相干的人被一起静默）；或者干脆拒了它 ⇒ 谁发的不明就静默，比不设闸更坏。
    """
    limiter, _ = _limiter()
    for _ in range(MAX_TRIGGERS + 5):
        d = limiter.guard("a", "g", "", "group_mention")
        assert d.allowed is True and d.reason == "no_sender"
    assert limiter.stats()["buckets"] == 0


# ==================================================== 2. 上限与静默

def test_the_trigger_past_the_limit_is_refused():
    """窗口内第 ``MAX_TRIGGERS + 1`` 次硬触发 ⇒ 拒，并进入静默（前 N 次照常放行）。"""
    limiter, _ = _limiter()
    for i in range(MAX_TRIGGERS):
        d = limiter.guard("a", "g", "wxid_peer", "group_mention")
        assert d.allowed is True, f"第 {i + 1} 次不该被拒"
        assert d.hits == i + 1

    tripped = limiter.guard("a", "g", "wxid_peer", "group_mention")
    assert tripped.allowed is False
    assert tripped.reason == "tripped"
    assert tripped.muted_until == pytest.approx(1000.0 + MUTE_S)


def test_a_loop_dies_inside_one_mute_window():
    """**回路真的会断**：事故里每 10~15 秒一轮 ⇒ 静默期内我们不再回，对面收不到 @。

    这条是参数（``WINDOW_S`` / ``MAX_TRIGGERS`` / ``MUTE_S``）的**唯一行为判据**：
    按 15 秒一轮推进假时钟，回路必须在**一个静默期之内**被摁住。
    """
    limiter, clock = _limiter()
    allowed = refused = 0
    rounds_to_mute = None
    for i in range(1, 41):                      # 最多 40 轮 = 10 分钟
        d = limiter.guard("a", "57737516566@chatroom", "wxid_eezdbu1ytws422",
                          "group_mention")
        if d.allowed:
            allowed += 1
        else:
            refused += 1
            if rounds_to_mute is None:
                rounds_to_mute = i
        clock.tick(_LOOP_ROUND_S)

    assert rounds_to_mute is not None, "回路刷了 40 轮都没被拦 ⇒ 闸没生效"
    assert rounds_to_mute <= MAX_TRIGGERS + 1, \
        f"第 {rounds_to_mute} 轮才拦 —— 上限没起作用"
    # 静默 180s / 15s = 12 轮；这 40 轮里被拒的必须**远多于**放行的（回路被摁住了）
    assert refused > allowed, "被拒的次数没有超过放行次数 ⇒ 静默太短，只是减慢不是掐断"


def test_the_mute_does_not_extend_itself():
    """**静默期内不再计数**（INVARIANT 2）：持续来刷不许把静默期一直续下去。

    反面的现场形态是"永久封禁"：一个被误伤的人只要再发一条，静默就翻倍，
    而且**日志里只会多几行 WARNING**，没人会把它和"这个人为什么永远不被回复"联系起来。
    """
    limiter, clock = _limiter()
    for _ in range(MAX_TRIGGERS):
        limiter.guard("a", "g", "wxid_peer", "group_mention")
    first = limiter.guard("a", "g", "wxid_peer", "group_mention")
    assert first.allowed is False

    # 静默期内来回刷 10 轮，静默的到期时刻**一秒都不许变**
    for _ in range(10):
        clock.tick(5.0)
        d = limiter.guard("a", "g", "wxid_peer", "group_mention")
        assert d.allowed is False and d.reason == "muted"
        assert d.muted_until == pytest.approx(first.muted_until)


def test_the_mute_expires_and_the_window_starts_fresh():
    """静默到点自动放行；且放行之后是**全新窗口**（不是"再一条就又被掐"）。"""
    limiter, clock = _limiter()
    for _ in range(MAX_TRIGGERS):
        limiter.guard("a", "g", "wxid_peer", "group_mention")
    assert limiter.guard("a", "g", "wxid_peer", "group_mention").allowed is False

    clock.tick(MUTE_S + 0.5)
    for i in range(MAX_TRIGGERS):
        d = limiter.guard("a", "g", "wxid_peer", "group_mention")
        assert d.allowed is True and d.hits == i + 1, \
            "静默刚过就被立刻掐回去 ⇒ 计数没清空（等价于静默无限续期）"


# ==================================================== 3. 滑窗

def test_a_slow_talker_never_trips_it():
    """**正常的群聊节奏永远不触发**：每分钟 @ 一次，跑 20 分钟一次都不拦。

    这条是"别把真人当刷屏"的判据（``WINDOW_S / MAX_TRIGGERS = 50`` 秒 ⇒ 比这更慢的
    节奏在窗口里最多只能攒到 5 次，够不到 6）。
    """
    limiter, clock = _limiter()
    for _ in range(20):
        assert limiter.guard("a", "g", "wxid_human", "group_mention").allowed is True
        clock.tick(60.0)
    assert limiter.stats()["muted"] == 0


def test_the_window_is_a_sliding_one_not_a_fixed_bucket():
    """窗口是**滑动**的：不能是"每 60 秒清零一次"的固定桶。

    判据：同一批 5 次计数，若相邻间隔刚好压过窗口（61 秒），一次都不该被拒；
    而把它们挤进 60 秒内，第 6 次要被拒。
    """
    limiter, clock = _limiter()
    for _ in range(6):
        assert limiter.guard("a", "g", "wxid_slow", "group_mention").allowed is True
        clock.tick(WINDOW_S + 1.0)

    for _ in range(MAX_TRIGGERS):
        assert limiter.guard("a", "g", "wxid_fast", "group_mention").allowed is True
        clock.tick(1.0)
    assert limiter.guard("a", "g", "wxid_fast", "group_mention").allowed is False


# ==================================================== 4. 额度互不牵连

def test_each_sender_group_and_account_has_its_own_budget():
    """桶的 key = ``(账号, 群, 发送者)`` —— 三个维度**都要**分开。

    别把这件事当小事：混一个维度就是"张三刷屏、李四被封"。现场只表现为
    "某些人说话机器人不理"，而日志里那一行 WARNING 打的是**别人**的 wxid。
    """
    limiter, _ = _limiter()
    for _ in range(MAX_TRIGGERS + 1):
        limiter.guard("acct-1", "g1", "wxid_spammer", "group_mention")
    assert limiter.guard("acct-1", "g1", "wxid_spammer", "group_mention").allowed is False

    assert limiter.guard("acct-1", "g1", "wxid_other", "group_mention").allowed is True, "别人被牵连"
    assert limiter.guard("acct-1", "g2", "wxid_spammer", "group_mention").allowed is True, "别的群被牵连"
    assert limiter.guard("acct-2", "g1", "wxid_spammer", "group_mention").allowed is True, "别的账号被牵连"


def test_the_two_guarded_reasons_share_one_budget():
    """``group_mention`` 与 ``group_solitaire`` **共用**一个额度。

    它们是同一个回路的两条腿（接龙卡片里也可能带 @）：分开计账 ⇒ 交替用两条路
    就能各刷 5 次，等于把上限翻倍。
    """
    limiter, _ = _limiter()
    # ⚠️ 必须**多一轮**：写成 ``* 3`` 时正好 6 条，而 MAX_TRIGGERS 就是 6
    #    ⇒ ``else`` 那一支永远走不到，"分开计账"的变异体于是活了下来（第一轮实测 SURVIVED）。
    reasons = ["group_mention", "group_solitaire"] * 4
    for i, reason in enumerate(reasons):
        d = limiter.guard("a", "g", "wxid_peer", reason)
        if i < MAX_TRIGGERS:
            assert d.allowed is True, f"第 {i + 1} 次（{reason}）不该被拒"
        else:
            assert d.allowed is False, "两条腿分开计账 ⇒ 上限被翻倍"


# ==================================================== 5. 桶的生命周期

def test_stale_buckets_get_reaped_when_new_ones_arrive():
    """过期桶会在下一个新桶到来时被清掉（``MAX_BUCKETS`` 是**清扫触发线**，不是硬上限）。

    ⚠️ 诚实记一笔：桶数的**真实上界**是"窗口内的活跃发件人数"（真实流量决定），
    清扫只能清**过期**的那些 —— 这是对的（窗口内活跃的人本来就该各占一个桶，
    清了就等于把正在刷屏的人的额度重置掉）。这条用例判的是"闲置的那些不会一直留着"。
    """
    limiter, clock = _limiter(max_buckets=8)
    for i in range(20):
        limiter.guard("a", "g", f"wxid_old_{i}", "group_mention")
    assert limiter.stats()["buckets"] == 20, "窗口内活跃的发件人本来就该各占一个桶"

    clock.tick(WINDOW_S * 2)                     # 全部过期
    limiter.guard("a", "g", "wxid_fresh", "group_mention")
    assert limiter.stats()["buckets"] == 1, "过期桶没被清 ⇒ 长跑进程无界增长"


def test_a_muted_bucket_survives_the_sweep():
    """清扫不许把**正在静默**的桶清掉 —— 那等于"刷得越狠越早被放行"。"""
    limiter, _ = _limiter(max_buckets=4)
    for _ in range(MAX_TRIGGERS + 1):
        limiter.guard("a", "g", "wxid_muted", "group_mention")
    assert limiter.guard("a", "g", "wxid_muted", "group_mention").allowed is False

    for i in range(20):                          # 灌满新桶，逼出若干次清扫
        limiter.guard("a", "g", f"wxid_new_{i}", "group_mention")

    assert limiter.guard("a", "g", "wxid_muted", "group_mention").allowed is False, \
        "静默中的桶被清扫掉了 ⇒ 刷屏的人反而立刻被放行"


def test_reset_clears_one_account_or_everything():
    limiter, _ = _limiter()
    for acct in ("acct-1", "acct-2"):
        for _ in range(MAX_TRIGGERS + 1):
            limiter.guard(acct, "g", "wxid_peer", "group_mention")
    assert limiter.stats()["muted"] == 2

    limiter.reset("acct-1")
    assert limiter.guard("acct-1", "g", "wxid_peer", "group_mention").allowed is True
    assert limiter.guard("acct-2", "g", "wxid_peer", "group_mention").allowed is False

    limiter.reset()
    assert limiter.guard("acct-2", "g", "wxid_peer", "group_mention").allowed is True
    assert limiter.stats() == {"buckets": 1, "muted": 0}


# ==================================================== 6. 参数与观测

def test_the_trip_is_visible_at_warning_level(caplog):
    """掐停必须打 **WARNING**。

    ⚠️ ``.183`` 的 ``journalctl`` **只采 WARNING+**（INFO 只在
    ``/root/.hermes/logs/gateway.log`` 那个文件里）。写 debug/info ⇒ 出事时现场
    什么都看不见，而"看不见"正是这次事故拖了 4 分钟才被发现的原因。
    """
    limiter, _ = _limiter()
    with caplog.at_level(logging.WARNING):
        for _ in range(MAX_TRIGGERS + 1):
            limiter.guard("a", "g", "wxid_peer", "group_mention")
    recs = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert recs, "掐停没有 WARNING ⇒ journald 里看不见"
    assert any("触发限速闸" in r.getMessage() for r in recs)


def test_the_constants_are_self_consistent():
    """参数的**内部一致性**（改常量必须一起想清楚，别只改一个），三条都是有理由的：

    1. **静默 ≫ 回路间隔** —— 掐断靠"我们不再回"，静默短了对面又刷回来；
    2. **事故节奏要留 ≥2 倍余量** —— ``WINDOW_S / 间隔``（窗口里能装多少次）
       必须远大于 ``MAX_TRIGGERS``，否则落进"稳态差一次、永远不拦"的坑（见文件头）；
    3. **真人节奏要够不着** —— ``WINDOW_S / MAX_TRIGGERS``（容忍的最快节奏）
       不许低于一分钟一次。
    """
    assert WINDOW_S > 0 and MAX_TRIGGERS >= 1 and MUTE_S > 0
    assert MUTE_S >= 4 * _LOOP_ROUND_S, \
        "静默必须显著长于回路的轮询间隔，否则静默期里对面又刷回来了"
    assert WINDOW_S / _LOOP_ROUND_S >= 2 * MAX_TRIGGERS, \
        "窗口里装不下 2 倍上限 ⇒ 稳态计数够不到上限，闸会空转（事故那种节奏都拦不住）"
    assert WINDOW_S / MAX_TRIGGERS >= 50.0, \
        "容忍的节奏比每 50 秒一次还快 ⇒ 正常人连着问几句就会被掐"


def test_a_broken_clock_lets_everything_through(caplog):
    """★ 判定出错 ⇒ **放行**（INVARIANT 3），不是丢弃。

    判据用"时钟炸了"来构造（``guard`` 里唯一会抛的一步）。反过来的写法
    （出错就拒）现场形态是：限速器一坏，**所有 @ 全被吞掉**，而只有几行 WARNING ——
    那是比"没有闸"严重得多的故障（没有闸只是回到部署前的状态）。
    """
    def _boom():
        raise RuntimeError("clock exploded")

    limiter = TriggerRateLimiter(clock=_boom)
    with caplog.at_level(logging.WARNING):
        d = limiter.guard("a", "g", "wxid_peer", "group_mention")
    assert d.allowed is True and d.reason == "error"
    assert any("触发限速判定出错" in r.getMessage() for r in caplog.records)


def test_stats_are_counts_only_never_identities():
    """``stats`` 只报数（这是运维面，不许返回 wxid / 群 id）。"""
    limiter, _ = _limiter()
    limiter.guard("a", "g", "wxid_secret", "group_mention")
    limiter.guard("a", "g", "wxid_secret", "group_mention")
    st = limiter.stats()
    assert set(st) == {"buckets", "muted"}
    assert "wxid_secret" not in repr(st)


def test_the_clock_is_injectable_and_defaults_to_monotonic():
    """默认时钟必须是 **monotonic**：```time.time()`` 被 NTP 往回拨会让静默期变长/变短。"""
    limiter = TriggerRateLimiter()
    assert limiter._clock is time.monotonic
    assert isinstance(Decision(True, "ok"), Decision)      # 冻结值对象，可比较
    # 脏 now 也不许抛（INVARIANT 3：出错一律放行）
    bad = TriggerRateLimiter().guard("a", "g", "w", "group_mention", now="nope")
    assert bad.allowed is True and bad.reason == "error"


def test_explicit_now_overrides_the_clock():
    """``now=`` 是给探针/测试用的显式时点（不改时钟状态）。

    ⚠️ 最后那条 ``muted_until`` 的断言不能省：只断言"被拒"的话，**忽略 ``now=``
    去读时钟**的写法照样能通过（时钟停在 1000，六次计数照样够到上限）——
    那是"测试看起来在验 now、其实在验别的"。
    """
    limiter, clock = _limiter()
    for _ in range(MAX_TRIGGERS):
        limiter.guard("a", "g", "w", "group_mention", now=500.0)
    d = limiter.guard("a", "g", "w", "group_mention", now=500.5)
    assert d.allowed is False and d.reason == "tripped"
    assert d.muted_until == pytest.approx(500.5 + MUTE_S), "静默到期的基准必须是 now，不是时钟"
    assert clock.t == 1000.0, "传了 now 就不该去读时钟"
