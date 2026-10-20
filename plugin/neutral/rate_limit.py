"""触发限速闸（R1，2026-10-03「两个机器人互相 @ 死循环」事故的止血闸）。

## 为什么需要这一层

2026-10-03 08:39–08:43，某个测试群里两个机器人（生产号与 live-test 号）
**每 10~15 秒对刷一轮，4 分钟刷 24 条**，直到人工把网关停掉。
回路是：A @ B → B 回一条 **@A** → A 看见自己被 @ → A 回 **@B** → …… 每一环走的都是
``inbound.triggers`` 第 ⑦/⑧ 条 —— **「@机器人」「接龙」= 100% 必回、直接进 agent、
不过心流 judge**。

**心流的那些闸对这条路一条都不生效**：``minReplyIntervalSec`` / ``minJudgeIntervalSec`` /
``check_gate``（关闭→白名单→空消息→冷却→judge 频率→预算）/ ``hf_cooldown_ok`` 全都长在
``_try_heartflow`` 里面，而这两个 reason 在 ``ok=True`` 那一支就**直通** ``handle_message``
（``adapter.py`` ④ 之后）。⇒ **硬触发这条路在设计上没有任何上限**，两个 bot 一凑齐就无限刷。
（TS 生产版同样没有这道闸 —— 这不是 py2 独有的缺口，只是 py2 先被撞上。）

## 这一层做什么 / 不做什么

**只加限速，不改触发识别**（老板 2026-10-03 拍板「只加限速（最小改动）」）：
同一个 ``(账号, 群, 发送者)`` 在 :data:`WINDOW_S` 秒内**第 7 次**硬触发 ⇒ 静默
:data:`MUTE_S` 秒，静默期内的硬触发**丢掉**并打 **WARNING**（journald 只采 WARNING+，
所以这里不许用 debug —— 出事了必须能在现场看见）。

**掐断回路靠的是"我们不再回"，不是"我们少回"**：静默期内对面机器人收不到 @，
它就不出声 ⇒ 回路在第一个静默期里自己断掉。⇒ 静默时长必须显著长于对面的轮询间隔。

⚠️ **误伤的代价是有界而且很轻的（这一条决定了阈值敢不敢定得低）**：静默只作用于
:data:`GUARDED_REASONS` 那两条**硬触发**路 —— 被静默的人**不 @ 机器人时，心流那一路
照旧在跑**（群消息仍然进 judge、该接话照接）。他损失的是"@ 了必答"这个保证，
而不是"机器人完全不理我"。

**只覆盖「100% 必回、绕过心流」的那两条**（:data:`GUARDED_REASONS`）：``group_mention``
与 ``group_solitaire``。它们是**唯一**"没有任何别的闸"的一类；``dm`` / ``group_msg_type``
/ ``group_quote_bot`` / ``group_keyword`` 虽然也走 ``ok=True``，但都属于**配置开出来的**
路（默认关），拿它们的用户是**人**、且私聊里一分钟发 6 条再正常不过 —— 同一个上限盖上去
会把正常对话掐掉。⚠️ **这是一条已知的边界，不是遗漏**：机器人在私聊里对刷、
或靠关键词对刷，这一版**不拦**（要拦就得单独定参数，属项目约定的事）。

## INVARIANT: R1（四条，改动前先看这里）

1. **纯内存、纯函数式判定、零 IO**：不落库、不联网、不发消息、不读账号文件。
   ⇒ 它可以被本机单测真跑（``plugin/tests/test_rate_limit.py`` 不需要任何替身）。
2. **静默期内不再计数**：被掐的那几条**不延长**静默（否则持续来刷 = 无限续期 =
   变成"永久封禁"，那是一个比死循环更难发现的故障）。静默到点自动放行，窗口重新起算。
3. **判定失败的方向永远是"放行"**：``guard`` 只做算术，任何异常都在调用方那条
   ``try`` 之外 —— 它**不许**成为丢消息的原因。反过来，**触发限速时那条消息是被丢弃的**
   （不是交给心流），理由见 :meth:`TriggerRateLimiter.guard` 的 docstring。
4. **键含账号**：多账号同一群同一个人互不干扰（同 ``_dedup`` 的 key 口径）。

## 参数怎么来的（没有新配置键 —— 这是有意的）

上限**不**做成账号配置键。理由是本工程反复吃亏的那条：**配置键在、实现在不在**
（``debounceMs`` / ``jargon.maxRetries`` 都出现过这个形态）。这是个
**安全网**，安全网的参数不该能被人手改成"关掉"。要调就改这里的常量（一处），
要临时放行就重启进程（内存态）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: 计数窗口（秒）。**为什么是 300 而不是 60 —— 这条是算过的，别拍脑袋改小**：
#: 滑动窗口的稳态计数是 ``W / 间隔``，所以"窗口内 N 次"这种判据在
#: ``间隔 ≈ W/N`` 时**永远差一次**（实测：W=60 / N=5 时，10~15 秒一轮的回路
#: 稳态只有 4 次，**一次都不会被拒** —— 那是个看起来装好了、其实空转的闸）。
#: 取 300 之后，事故那种 10~15 秒/轮的节奏在窗口里能装 **20~30** 次，
#: 对上限 6 有 **3 倍以上**的余量；连 30 秒一轮的慢回路也照样会被掐。
WINDOW_S = 300.0

#: 窗口内允许的硬触发次数。**第 7 次**硬触发即静默。
#: 两头都试过：调到 4~5 会把"在群里连着问几件事"的人掐掉（那是**正经生意**，
#: 而老板自己验收时就常常一口气 @ 好几句）；调到 8 以上则现场这种回路要刷两分钟才停。
#: 6 的含义是 **5 分钟 6 次硬触发**（≈ 每 50 秒一次的持续 @）—— 真人要越过它得
#: 一直在 @ 机器人，那本身就已经是刷屏行为了。
MAX_TRIGGERS = 6

#: 静默时长。**必须显著长于回路的轮询间隔**，因为掐断回路靠的就是"我们不再回，
#: 对面收不到 @ 自然就停了"：事故里 10~15 秒一轮 ⇒ 180 秒 = 12~18 个回合，
#: 对面早就没得回了。误伤的代价也**有界且很轻**：静默只掐「硬触发」那两条路 ——
#: 这个人不 @ 的时候，心流那一路照旧在跑（见 :data:`GUARDED_REASONS` 那一段说明）。
MUTE_S = 180.0

#: **只**管这两条 —— 触发判定里唯一"100% 必回、绕过心流全部预算与冷却"的路。
#: ⚠️ 这两个字符串必须与 ``inbound.triggers.ALLOW_REASONS`` 里的逐字相同
#: （有一条用例钉着这个包含关系，写错一个字母它会变红）。
GUARDED_REASONS = frozenset({"group_mention", "group_solitaire"})

#: **清扫触发线**（不是硬上限，别当成 cap 用）：key 里带发送者 wxid，长跑进程 + 群成员
#: 流动 = 桶只增不减。新建桶时若桶数已超过它就顺手清一遍**过期**的
#: （既不在静默、最近一次计数也早于窗口）。
#: ⚠️ **正在窗口内活跃的桶一个都不清** —— 清了等于把刷屏那个人的额度重置掉。
#: ⇒ 真实上界 = "窗口内的活跃发送者数"（由真实流量决定，不是由这个常量决定）。
MAX_BUCKETS = 2048


@dataclass
class _Bucket:
    """一个 ``(账号, 群, 发送者)`` 的计数状态。"""

    hits: list[float] = field(default_factory=list)
    muted_until: float = 0.0


@dataclass(frozen=True)
class Decision:
    """一次判定的结果。``allowed=False`` ⇒ 调用方**丢弃**这条消息。

    ``reason`` 是给日志/测试看的短标签，取值：``unguarded``（这一类不受管）/
    ``no_sender``（拿不到发送者，不成桶）/ ``muted``（正在静默期）/
    ``tripped``（**本次**触发上限）/ ``ok``。
    """

    allowed: bool
    reason: str
    hits: int = 0
    muted_until: float = 0.0


class TriggerRateLimiter:
    """硬触发的限速闸。实例**进程级**（key 里带账号，见 INVARIANT 4）。

    ``clock`` 可注入（测试传假时钟，不必 sleep）。默认 ``time.monotonic`` ——
    **不用 wall clock**：系统时间被 NTP 拨回时，monotonic 的静默期不会跟着变长。
    """

    def __init__(self, *, window_s: float = WINDOW_S, max_triggers: int = MAX_TRIGGERS,
                 mute_s: float = MUTE_S, max_buckets: int = MAX_BUCKETS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.window_s = float(window_s)
        self.max_triggers = int(max_triggers)
        self.mute_s = float(mute_s)
        self.max_buckets = int(max_buckets)
        self._clock = clock
        self._buckets: dict[tuple[str, str, str], _Bucket] = {}

    # ------------------------------------------------------------- 唯一入口

    def guard(self, account_id: str, group_id: str, sender_id: str, reason: str,
              now: float | None = None) -> Decision:
        """记一次硬触发并回答"这一条还让不让过"。

        **被拒时调用方必须丢弃这条消息**（不是交给心流）：心流接管时同样会产出一条回复，
        而"这个发送者正在刷"这个判断对心流那一路一样成立；更要紧的是，回路里的对面
        机器人等的正是**我们的任何一条发言**（哪怕不带 @，只要带关键词/引用就够）。
        丢是唯一能保证断链的动作。

        返回 ``decision.allowed`` 为假时，调用方打 WARNING 并 ``return``。

        ⚠️ 内部包了 ``try``：**判定出错一律按"放行"处理**（INVARIANT 3）。反过来的写法
        （出错就拒）等于"限速器坏了 = 所有 @ 都被吞掉"，而现场只有几行 WARNING；
        放行那一侧最坏也只是回到"没有闸"这个已知状态。
        """
        try:
            return self._guard(account_id, group_id, sender_id, reason, now)
        except Exception as exc:  # noqa: BLE001
            log.warning("触发限速判定出错（按放行处理，绝不因此丢消息）: %s", exc)
            return Decision(True, "error")

    def _guard(self, account_id: str, group_id: str, sender_id: str, reason: str,
               now: float | None) -> Decision:
        if reason not in GUARDED_REASONS:
            return Decision(True, "unguarded")
        if not sender_id:
            # 拿不到发送者就没法归因 —— 绝不用一个空 key 建桶：那会让**所有人**共享
            # 一个额度，静默起来连不相干的人一起掐（比不设闸更坏）。
            return Decision(True, "no_sender")

        t = self._clock() if now is None else float(now)
        key = (account_id or "", group_id or "", sender_id)
        bucket = self._buckets.get(key)

        if bucket is None:
            self._maybe_sweep(t)
            bucket = _Bucket()
            self._buckets[key] = bucket

        # ② 静默期：直接拒，**且不记账**（INVARIANT 2：静默不许自我续期）。
        if bucket.muted_until > t:
            return Decision(False, "muted", hits=len(bucket.hits),
                            muted_until=bucket.muted_until)

        # 滑窗：只留窗口内的计数
        cutoff = t - self.window_s
        if bucket.hits:
            bucket.hits = [h for h in bucket.hits if h > cutoff]

        # 到上限 ⇒ 静默。**清空计数**：静默结束后按全新窗口起算（而 180s 的静默本来就
        #  长于 60s 的窗口，旧计数到那时也早已过期 —— 清空只是把这件事写明白）。
        if len(bucket.hits) >= self.max_triggers:
            bucket.hits = []
            bucket.muted_until = t + self.mute_s
            log.warning("触发限速闸：%s/%s/%s 在 %.0fs 内第 %d 次硬触发（%s）"
                        "⇒ 静默 %.0fs（至 %.0f）",
                        account_id, group_id, sender_id, self.window_s,
                        self.max_triggers + 1, reason, self.mute_s, bucket.muted_until)
            return Decision(False, "tripped", hits=self.max_triggers,
                            muted_until=bucket.muted_until)

        bucket.hits.append(t)
        return Decision(True, "ok", hits=len(bucket.hits))

    # ------------------------------------------------------------- 观测/清理

    def reset(self, account_id: str | None = None) -> None:
        """清掉限速状态（**全部**或某账号）。给测试与应急用；停机/重连**不**自动清
        （重启进程自然会清，而"重连一次就把安全网清空"正好是回路想要的效果）。"""
        if account_id is None:
            self._buckets.clear()
            return
        for key in [k for k in self._buckets if k[0] == account_id]:
            self._buckets.pop(key, None)

    def stats(self, now: float | None = None) -> dict:
        """``{"buckets": n, "muted": m}`` —— 数出来的运维观测（不读内容、不含 wxid）。"""
        t = self._clock() if now is None else float(now)
        return {
            "buckets": len(self._buckets),
            "muted": sum(1 for b in self._buckets.values() if b.muted_until > t),
        }

    def _maybe_sweep(self, now: float) -> None:
        """桶太多时清一次过期桶（**只清新桶进来时**，热路径上摊薄成 O(1)）。"""
        if len(self._buckets) <= self.max_buckets:
            return
        cutoff = now - self.window_s
        stale = [k for k, b in self._buckets.items()
                 if b.muted_until <= now and (not b.hits or max(b.hits) <= cutoff)]
        for key in stale:
            self._buckets.pop(key, None)


__all__ = [
    "GUARDED_REASONS",
    "MAX_TRIGGERS",
    "MUTE_S",
    "WINDOW_S",
    "Decision",
    "TriggerRateLimiter",
]
