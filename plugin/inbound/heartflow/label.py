"""心流标签判定（纯函数，无 DB / 无 IO）—— ``heartflow-label.ts`` (v1.6.8) 的移植。

## 为什么需要它（2026-09-26 老板拍板改造）

旧闭环的 ``engaged`` 判据是「**该群出现任意人类群消息**」（handler 里
``markHfGroupEngaged``，600s 窗）⇒ 活跃群恒为真 ⇒ 接话率饱和 ⇒ ``eval_hf_threshold``
每轮都下调 ⇒ **阈值单调降到地板**（"不设下限就会一直降低"，老板原话）。
它学的是「群里有没有人说话」，不是「我上一条说得怎么样」。

本模块把标签锚回 **bot 自己发的那条消息**：

====================  ============================================================
``quote``             有人引用了 bot 那条              （强正：最可靠）
``mention``           有人 @ 了 bot                    （强正）
``negative``          有人针对 bot 那条说"别刷了"      （强负）
``veto``              人工一键否决 ``/heartflow veto``  （强负：人工标注）
``short-window``      弱窗（默认 60s）内有人说话        （弱正：可能只是群里本来就热闹
                                                       ⇒ 需反事实基线过滤）
``silence``           观察窗内无人接话                  （负；同样受反事实基线约束）
====================  ============================================================

⚠️ **负信号词只用于给 bot 自己的发言打分，绝不参与"要不要回复"的触发判定**
（老板 2026-09-26 明确：触发必须由心流五维判断，**不许用固定关键词**。
这里是**评估已发出消息的效果**，是反馈闭环的输入，不是触发器）。
本模块的 docstring 里"只用于给 bot 自己的发言打分"这句话被源级守卫断言钉住。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: 命中信号种类（落 ``wpp_hf_ledger.engage_signal``，供 ``/heartflow status`` 分布与审计）
HF_ENGAGE_SIGNALS: tuple[str, ...] = (
    "quote", "mention", "negative", "veto", "short-window", "silence")

#: 强信号（与群是否本来就热闹无关，**永远**算有效学习样本）。
#:
#: v1.8.0 起含 ``veto``（人工一键否决）：它是**人工标注的负样本**，比任何自动判据
#: 都可信 —— 必须进本表，否则会被 ``ambientP`` 本底过滤掉
#: （冷清群里 veto 恰好最需要被记住）。
HF_STRONG_SIGNALS: tuple[str, ...] = ("quote", "mention", "negative", "veto")


def as_hf_engage_signal(s: Any) -> str | None:
    """DB 读回的 ``engage_signal`` → 收窄成已知信号；``NULL``/未知 → ``None``（**不采信**）。

    v1.6.8 之前落的行没有信号列，那些正是**旧错误标签**，必须被排除在学习样本外，
    否则改造白做。

    ⚠️ 加新信号必须**同时**改 :data:`HF_ENGAGE_SIGNALS` 与 DDL 的 ``engage_signal``
    列宽 —— 漏这里的后果是**静默**的：本函数返回 ``None`` ⇒ 该行被当 legacy 丢弃，
    信号完全无效且**无报错**。所以值域是**一处定义**，别在别处再列一遍。
    """
    if s is None:
        return None
    return s if isinstance(s, str) and s in HF_ENGAGE_SIGNALS else None


#: 强负信号词（仅在**引用/提到 bot** 的消息里判定 —— 此时几乎可以肯定在说 bot）。
#:
#: ⚠️ 这是**评估用**词表，**不是触发关键词**（见模块 docstring）。
HF_NEGATIVE_PHRASES: tuple[str, ...] = (
    "别刷了", "别刷屏", "不要刷屏", "别发了", "别说话了", "别说话", "闭嘴",
    "别插话", "别乱说", "别瞎说", "少说两句", "谁让你说话", "别吵", "别烦",
)

#: 硬负信号词：极少数"只能是冲着刷屏者说的"短语，允许在**窗内任意人类消息**上判定。
#:
#: 刻意压到 4 个 —— 窗内裸消息没有上下文，**误报代价是冤枉一次好回复**，宁可少判。
HF_HARD_NEGATIVE_PHRASES: tuple[str, ...] = ("别刷了", "别刷屏", "不要刷屏", "别发了")


def _norm(s: str) -> str:
    """归一化：去空白 + 小写（中文群聊场景够用，不做全角转换）。"""
    return "".join(s.split()).lower()


def is_hf_negative_text(text: Any, strong: bool) -> bool:
    """文本是否命中负信号词（``strong=True`` 用完整词表，``False`` 只用极少数硬词）。"""
    t = _norm(text if isinstance(text, str) else "")
    if not t:
        return False
    phrases = HF_NEGATIVE_PHRASES if strong else HF_HARD_NEGATIVE_PHRASES
    return any(_norm(p) in t for p in phrases)


@dataclass(frozen=True)
class HfEngageCandidate:
    """观察窗内的候选人类消息（handler 已把"引用的正是 bot 那条"解析成 ``quotes_bot``）。"""

    #: 消息时刻（epoch 秒）
    at_sec: int
    #: 文本（用于负信号词判定）
    text: str = ""
    #: 引用的正是 bot 刚发的那条（handler 用 bot_msg_id 或 outbound+同群+窗内判定后传入）
    quotes_bot: bool = False
    #: @ 了 bot
    mentions_bot: bool = False


@dataclass(frozen=True)
class HfEngagementInput:
    """一行账本行的判定输入。"""

    #: 本行 ``sent_at``（epoch 秒）
    sent_at_sec: int
    #: 弱信号窗（秒）：只有这段内说话的才算"可能接了我的话"
    label_window_sec: int
    #: 观察窗上限（秒）：强信号（引用/@/负词）在**整段内**都有效 —— 晚到的引用也是真接话
    observe_window_sec: int
    candidates: Sequence[HfEngageCandidate]


@dataclass(frozen=True)
class HfEngagement:
    """判定结果。"""

    #: ``1`` = 被接话 / ``0`` = 被嫌 / ``None`` = 无结论（无候选）
    engaged: int | None
    signal: str | None
    #: ``True`` = 立即收敛该行；``False`` = 只落弱信号，窗继续开着等更强的信号升级
    close: bool


#: 无结论（窗内没有可用候选）
NO_VERDICT = HfEngagement(engaged=None, signal=None, close=False)


def classify_hf_engagement(inp: HfEngagementInput) -> HfEngagement:
    """判定一条账本行的接话结果（**纯函数**）。

    优先级不是"按信号种类排"，而是**按每条候选消息的意图置信度先定其性质**，
    再聚合成一行一个结论：

    ① 任何一条判为 negative → 整行 negative/0（即便同时有人引用了 bot：
       "别刷了"是针对 bot 那句的）
    ② 否则有 quote → quote/1（quote 比 mention 强：引用带上下文，@ 可能只是叫人）
    ③ 否则有 mention → mention/1
    ④ 否则有短窗内消息 → short-window/1（**不关窗**，留给更强信号升级）
    ⑤ 否则无结论
    """
    sent_at = inp.sent_at_sec
    obs_end = sent_at + inp.observe_window_sec
    label_end = sent_at + inp.label_window_sec

    saw_quote = False
    saw_mention = False
    saw_weak = False

    for c in inp.candidates:
        if c.at_sec < sent_at or c.at_sec > obs_end:
            continue                                    # 窗外：与本行无关
        in_label = c.at_sec <= label_end
        if c.quotes_bot:
            # 引用 bot 的那条：说"别刷了" = 负面；否则 = 强正
            if is_hf_negative_text(c.text, True):
                return HfEngagement(engaged=0, signal="negative", close=True)
            saw_quote = True
            continue
        if c.mentions_bot:
            if is_hf_negative_text(c.text, True):
                return HfEngagement(engaged=0, signal="negative", close=True)
            saw_mention = True
            continue
        # 既没引用也没提到：只有窄窗内的"极少数硬负词"才敢判负（误报代价高）
        if in_label and is_hf_negative_text(c.text, False):
            return HfEngagement(engaged=0, signal="negative", close=True)
        if in_label:
            saw_weak = True

    if saw_quote:
        return HfEngagement(engaged=1, signal="quote", close=True)
    if saw_mention:
        return HfEngagement(engaged=1, signal="mention", close=True)
    if saw_weak:
        return HfEngagement(engaged=1, signal="short-window", close=False)   # 弱：不关窗
    return NO_VERDICT


def hf_ambient_p(count: Any, observed_sec: Any, window_sec: Any) -> float:
    """反事实基线：该群在**同一时段**本来就会有人说话的概率（泊松近似）。

    为什么要它：``short-window``（窗内有人说话）与 ``silence``（窗内没人说话）
    都有信息量**仅当**"这个时段本来就没那么热闹"。刷口号群的高峰时段本来 120s 内
    必有人说话 ⇒ 那行标签其实什么都没证明 ⇒ **直接不采信**，而不是把它当成
    "bot 受欢迎"继续下调阈值。（这正是旧标签跑飞的机制；也是老板说的
    "不设下限就会一直降低"的根因。）

    :param count: 该群**同一小时段**在观察期内的入站人类消息数
    :param observed_sec: 该时段被观察的总秒数（天数 × 3600）
    :param window_sec: 要算的窗长（``label_window_sec``）
    """
    if not _finite(count) or count <= 0:
        return 0.0
    if not _finite(observed_sec) or observed_sec <= 0:
        return 0.0
    rate = count / observed_sec                               # 条 / 秒
    # ⚠️ 窗长非法时**取 0 而不是让它变 NaN**：TS 的 ``Math.max(0, NaN)`` 是 NaN，
    #    而 Python 的 ``max(0.0, nan)`` 会返回 0.0 —— 两边**不一致**。
    #    这里显式收敛成 0（"窗长为 0 ⇒ 概率 0"），使行为与输入无关地确定。
    w = float(window_sec) if _finite(window_sec) else 0.0
    return 1.0 - math.exp(-rate * max(0.0, w))


def is_hf_sample_informative(signal: Any, ambient_p: float, ambient_max: float) -> bool:
    """该样本是否可采信（进自适应调阈的统计）。

    强信号（quote/mention/negative/veto）**永远**可采信 —— 引用与 @ 是有指向的行为，
    群里再热闹也不减损其含义。

    弱信号（short-window）与沉默（silence）只在 ``ambientP < ambientMax`` 时采信。
    ⚠️ 是 **严格小于**：恰等于 ``ambientMax`` 不算采信（边界有单测钉住）。
    """
    if signal is None:
        return False
    if signal in HF_STRONG_SIGNALS:
        return True
    return ambient_p < ambient_max


def is_hf_silence_exempted(
    signal: Any,
    engaged: Any,
    judge_overall: Any,
    threshold_at_judge_time: Any,
    margin_min: Any,
) -> bool:
    """v1.16.2 silence-exempt：高置信回复被「无人接话」时，该沉默样本不得充当抬阈负样本。

    ## 为什么要改口径（2026-10-16/17 生产两次复现的自锁）

    全员群真人不会去 @/引用机器人 —— 修好「看图」后回复内容对味（读出截图里的型号/金额/
    直播数据），但 ``engaged`` 仍全 0 ⇒ 学习闭环把它读成「此群不欢迎我」⇒ 4h/步单调抬阈
    0.60 → 0.80，预计回到 0.90 钉死 —— 与 10-16 事故的链逐字重演。实测账本：全员群
    18 条 closed 里 14 条是 ``margin ≥ 0.10`` 的 silence（judge 明确认为值得回、回完没人接），
    若照样计负 = 惩罚「质量高但不抢镜」。

    ## 判据（只豁免一种组合，其余全部照常）

    ``signal=silence`` 且 ``engaged=0`` 且 ``(judge_overall − 判定时生效阈值) ≥ margin_min``
    ⇒ 不计入调阈样本。

    - ❗ ``negative`` / ``veto`` **永不豁免** —— 那是「主动嫌弃」，才是该收敛的真信号
      （老板 9-26 治刷屏的本意不动）；
    - ❗ 低 margin 的沉默照常计负 —— 擦边回复被无视仍会抬阈，保留机制的收敛能力；
    - 两个分数任一缺失/非数字 ⇒ **不豁免**（退回旧行为，宁严勿宽）。

    ## 1e-9 容差

    ``judge_overall`` / ``effective_threshold`` 是 ``DECIMAL`` 经浮点收口的值，而 margin 是
    两者相减 —— ``0.75-0.65`` 得 ``0.09999999999999998``，恰在边距上的真样本会被错杀
    （2026-10-17 单测当场拓出这个坑）。容差只宽 1e-9，不影响「擦边 0.055/0.005 仍计负」的
    收敛力。

    ``margin_min <= 0``：总开关关，退回旧行为（纯函数自守，不依赖调用方闸门）。
    """
    if not _finite(margin_min) or not (margin_min > 0):
        return False
    # 与 TS 严格相等：``engaged`` 必须是数字 0（NULL/字符串都不算「没人接」）
    if signal != "silence" or isinstance(engaged, bool) or engaged != 0:
        return False
    if not _finite(judge_overall) or not _finite(threshold_at_judge_time):
        return False
    return judge_overall - threshold_at_judge_time >= margin_min - 1e-9


def _finite(v: Any) -> bool:
    """``Number.isFinite`` 的等价：bool 不算数（``True`` 会被当 1 混进来）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
