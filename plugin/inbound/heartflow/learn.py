"""心流反馈闭环（自适应调阈）+ **发送结果的唯一记账口** + sweep 主体。

``heartflow-learn.ts`` (1068 行, v1.6.x → v1.10.0) 的移植。

## 三层（照 TS，别混）

======================  ==========================================================
纯算法 / 分类           :func:`eval_hf_threshold` / :func:`hf_cooldown_ok` /
                        :func:`round2` / :func:`clamp_hf_threshold_to_band` /
                        :func:`hf_reachability_cap` / :func:`classify_hf_send`
                        —— 无副作用，单测直接调
内存缓存（**三处**）     :data:`_learned_thresholds`（每群 learned 阈值）/
                        :data:`_open_windows`（该群"正在观察中"的那**一条**）/
                        :data:`_reach_caps`（可达性天花板）
                        —— judge 热路径**零 DB IO** 全靠它们
DB 薄管线 + sweep       :func:`persist_hf_judged` / :func:`persist_hf_send_outcome` /
                        :func:`mark_hf_group_engaged` / :func:`run_sweep`
                        —— **全 catch**，失败不阻断收发
======================  ==========================================================

## sweep 的顺序**是规格的一部分**（不是随手排的）

1. 关过期窗 + 呆账收敛 + 清过期开窗 —— **无论学习开关都跑**（防 pending/超期行无限堆积）；
   失败则本轮直接 ``return``（下一轮补上）；
2. ``cfg.enabled``（**心流总开关**，不是 learning）关着 ⇒ return；
3. 画像 pass / 4. 分层 pass / 5. 占比外环 pass / 6. 可达性 pass —— 四者都在
   ``learning.enabled`` 判定**之前**：它们保的是「观测」与「心流还能不能说话」，
   与"自动调阈开关"无关（老板可能关掉自动调阈但仍要看画像/分层）；
7. 静默金丝雀（距上次真发言秒数）+ 重复闸历史裁剪；
8. ``if not L.enabled: return`` —— 到这里才是自动调阈；
9. 反事实基线 → 逐群取已收敛样本 → 可采信过滤 → :func:`eval_hf_threshold` →
   冷却 → :func:`apply_hf_threshold_change`。

每一步**各自** try/except：单轮单步骤的失败不许连累其余（这是 v1.6.8 起的一贯口径）。

## 与 TS 的**故意**分歧（每条都对应一次真实失效）

1. **``round2`` 用 half-up 而不是 :func:`round`**：JS ``Math.round`` 是"取较大者"，
   Python 的 ``round`` 是银行家舍入（``round(0.5) == 0``）⇒ 照抄会让
   ``round2(0.125)`` 得到 ``0.12``，与 TS 的 ``0.13`` 不同。
2. **``Number.isFinite`` 把 ``bool`` 排除掉**：``Number.isFinite(true) === false``，
   而 ``isinstance(True, int)`` 在 Py 里是 ``True`` —— 不排除的话 ``enabled: true``
   会被当成数字 ``1`` 混进阈值。
3. **脏配置一律回落缺省（本模块最重要的一条）**：JS 的 ``??`` 只在
   ``null``/``undefined`` 时回落，``NaN`` / ``"0.7"`` 会**一路流下去**；心流里
   ``bandMin: NaN`` 的后果是 ``clampHfThresholdToBand`` 返回 ``NaN`` ⇒ 生效阈值
   ``NaN`` ⇒ ``judge_overall >= NaN`` **恒 false** ⇒ **一条都不回**，而且不报错
   （09-26 那次静默停摆的形态）。⇒ 本模块每个数字字段都过 :func:`_num_or` /
   :func:`_int_or` / :func:`_bool_or`，脏值换成缺省并**不抛**。
   （副作用式的第二道防线：Py 的 ``min``/``max`` 遇 ``NaN`` 会返回**另一侧**而不是
   ``NaN``，所以即便有脏值漏进来也不会污染成"零发言" —— 但那是巧合，别依赖它。）
4. **IO 薄管线是同步函数**（TS 侧返回 Promise）：Py 的数据层是 pymysql，同步。
   只有 :func:`run_sweep` 是 ``async`` —— 因为它要 ``await`` 画像生成（LLM 调用）。
5. **:func:`resolve_hf_learning` 返回扁平的 camelCase ``dict``**：``profile._band_limits``
   与 ``runtime._effective_interval`` 已经按这个形状调用（``L["bandMin"]`` /
   ``L["sweepIntervalSec"]``）。**band 区间的唯一真源在这里**，
   ``profile._BAND_FALLBACK`` 必须与 :data:`HF_LEARNING_DEFAULTS` 逐字相等。
6. **白名单判定委托给 :mod:`.heartflow`**（延迟导入）：白名单语义只许有一处
   （``heartflow.is_hf_group_allowed``）。heartflow(#9) 反向 import 本模块 ⇒ 只能
   在函数体内 import。取不到时退化成"不过滤" = 白名单为空 ⇒ 全放行，
   与空白名单**语义完全相同**，所以这里不复制一份判定规则（复制必漂移）。
7. **:func:`get_open_hf_window` 直接返回不可变对象**而不是 TS 那样的"防御性副本"：
   :class:`HfOpenWindow` 是 frozen dataclass，调用方改不动它。
8. **``opts.dedupeCfg`` 的缺省**：TS 传的是未解析的 ``HF_DEDUPE_DEFAULTS`` 常量；
   Py 侧 ``HfDedupeConfig`` 是 dataclass ⇒ 缺省走 :func:`dedupe.resolve_dedupe_cfg`

   （``None``），逐字段与那份常量相等（由 ``test_learn.py`` 钉住）。
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from . import budget, dedupe, label, layer, observe, profile
from .runtime import CACHE_REGISTRY

log = logging.getLogger(__name__)

#: v1.10.0 可达性护栏单轮取数上限（行）。
#:
#: sweep 每 300s 一次，不能把台账整表拉进内存。5000 行 ≈ 生产 5 个群跑 2 周的判定量，
#: 远高于 ``reachabilityMinSample``（20）—— 只为防御脏数据 / 异常刷单。
HF_REACH_DB_ROW_CAP = 5000

#: 占位符 msgId 清单（``ok=True`` 但**没真发**）。
#:
#: 三个来源语义不同但后果相同 —— **预算与观察窗都不得被占用**。
#: v1.9.0 加 ``repeat-suppressed``（重复闸拦下）：不加这一条，它会被判成 **sent**，
#: 从而 ① 消耗发言预算额度 ② 开一个 600s 观察窗 ⇒ 随后人类的正常发言被记成
#: "接了我那句" ⇒ **毒化学习样本**。
HF_PLACEHOLDER_MSG_IDS: tuple[str, ...] = (
    "dedup-suppressed",
    "ack-template-dropped",
    "repeat-suppressed",
)

#: v1.6.x 心流学习参数缺省表 —— **band 区间的唯一真源**（见模块 docstring 分歧 5）。
#:
#: ``enabled: False`` 是刻意的（自动调阈要老板显式开）；``bandMin: 0.5`` 是老板
#: 2026-09-16 拍板的质量底线（起因见 ``heartflow.ts`` 的同名常量注释：接话率指标饱和 ⇒
#: 闭环必然把阈值推到地板）。``labelWindowSec: 60`` 是拿生产账本回放实测选的（不是拍的）。
HF_LEARNING_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "minSample": 10,
    "lowEngageRate": 0.15,
    "highEngageRate": 0.5,
    "step": 0.05,
    "bandMin": 0.5,
    "bandMax": 0.9,
    "sampleWindow": 20,
    "observeWindowSec": 600,
    "labelWindowSec": 60,
    "ambientMax": 0.5,
    "minChangeCooldownSec": 4 * 3600,
    "sweepIntervalSec": 300,
    "staleJudgedMaxSec": 1800,
    "recoverUnreachable": True,
    "reachabilityWindowSec": 7 * 86400,
    "reachabilityMinSample": 20,
    # v1.16.2 silence-exempt 边距（见 :func:`label.is_hf_silence_exempted`）：
    #   0.10 不是拍的 —— 生产实排（10-16/17 全员群 18 条 closed）：margin 分布
    #   {0.005×2, 0.055×2, 0.14+×14}，0.10 正落在「擦边负样本」与「高置信被无视」
    #   之间的空档 —— 豁免 14/18 条真误伤，4 条擦边仍计负。**≤0 = 关闭豁免退回旧行为**。
    "silenceExemptMarginMin": 0.1,
}

#: 账号级阈值回落基线（``cfg.replyThreshold`` 的缺省）。
#:
#: ⚠️ **单一真源**：heartflow(#9) 的闸门也必须调 :func:`resolve_reply_threshold`，
#: 不许各写一份 ``cfg.get("replyThreshold") or 0.6`` —— 阈值有三处各算一份正是
#: 09-26 静默停摆藏 3 天的原因之一（见 :func:`resolve_hf_effective_threshold`）。
HF_REPLY_THRESHOLD_FALLBACK = 0.6


# ================================================================ 配置解析（纯）


def _is_num(v: Any) -> bool:
    """``Number.isFinite`` 的等价：``bool`` **不算数**（见模块 docstring 分歧 2）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _num_or(v: Any, fallback: float, *, lo: float | None = None,
            hi: float | None = None) -> float:
    """有限的 ``float``；脏值 / 越界 ⇒ ``fallback``（见模块 docstring 分歧 3）。"""
    if not _is_num(v):
        return float(fallback)
    f = float(v)
    if lo is not None and f < lo:
        return float(fallback)
    if hi is not None and f > hi:
        return float(fallback)
    return f


def _int_or(v: Any, fallback: int, *, lo: int = 0) -> int:
    """``Math.floor`` 后的整数；脏值 / 小于 ``lo`` ⇒ ``fallback``。

    ``v`` 是小数（``300.7``）照 JS 取整 —— 配额类参数写个小数不该整套回落。
    """
    if not _is_num(v):
        return int(fallback)
    n = int(math.floor(v))
    return n if n >= lo else int(fallback)


def _bool_or(v: Any, fallback: bool) -> bool:
    """真布尔；``"false"`` / ``0`` / ``None`` 这类**不做真值推断** ⇒ ``fallback``。

    ``bool("false") is True`` —— 靠真值判断会把配置里的合法字符串 ``"false"``
    读成"开"，而这类参数（``recoverUnreachable``）反着读就会静默关掉护栏。
    """
    return v if isinstance(v, bool) else bool(fallback)


def resolve_reply_threshold(cfg: Mapping[str, Any] | None) -> float:
    """账号级回复阈值（``replyThreshold``）；脏值 ⇒ :data:`HF_REPLY_THRESHOLD_FALLBACK`。

    ⚠️ 这是**唯一**的取法（见 :data:`HF_REPLY_THRESHOLD_FALLBACK`）。
    """
    return _num_or((cfg or {}).get("replyThreshold"), HF_REPLY_THRESHOLD_FALLBACK,
                   lo=0.0, hi=1.0)


def resolve_hf_learning(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """合并 ``cfg["learning"]`` 与 :data:`HF_LEARNING_DEFAULTS`（**扁平 camelCase**）。

    形状脏（``learning`` 是字符串 / 列表）⇒ 全部回落缺省，**不抛**：这个函数在
    judge 热路径与 300s sweep 上都会被调，在这里抛错的形态是"整条链静默不工作"，
    比"用缺省值"严重得多。

    区间类的两个字段（``bandMin``/``bandMax``）**成对校验**：``bandMin > bandMax``
    说明配置写反了，此时两个都回落缺省 —— 只回落一个会得到一个比原本更松或更紧的
    区间，那不是"修好了"而是"换了个错法"。
    """
    root = cfg if isinstance(cfg, Mapping) else {}
    raw = root.get("learning")
    l = raw if isinstance(raw, Mapping) else {}
    D = HF_LEARNING_DEFAULTS

    lo = _num_or(l.get("bandMin"), D["bandMin"], lo=0.0, hi=1.0)
    hi = _num_or(l.get("bandMax"), D["bandMax"], lo=0.0, hi=1.0)
    if lo > hi:
        lo, hi = float(D["bandMin"]), float(D["bandMax"])

    return {
        "enabled": _bool_or(l.get("enabled"), D["enabled"]),
        "minSample": _int_or(l.get("minSample"), D["minSample"], lo=1),
        "lowEngageRate": _num_or(l.get("lowEngageRate"), D["lowEngageRate"], lo=0.0, hi=1.0),
        "highEngageRate": _num_or(l.get("highEngageRate"), D["highEngageRate"], lo=0.0, hi=1.0),
        "step": _num_or(l.get("step"), D["step"], lo=0.0, hi=1.0),
        "bandMin": lo,
        "bandMax": hi,
        "sampleWindow": _int_or(l.get("sampleWindow"), D["sampleWindow"], lo=1),
        "observeWindowSec": _int_or(l.get("observeWindowSec"), D["observeWindowSec"], lo=1),
        "labelWindowSec": _int_or(l.get("labelWindowSec"), D["labelWindowSec"], lo=1),
        "ambientMax": _num_or(l.get("ambientMax"), D["ambientMax"], lo=0.0, hi=1.0),
        "minChangeCooldownSec": _int_or(l.get("minChangeCooldownSec"),
                                        D["minChangeCooldownSec"], lo=0),
        "sweepIntervalSec": _int_or(l.get("sweepIntervalSec"), D["sweepIntervalSec"], lo=1),
        "staleJudgedMaxSec": _int_or(l.get("staleJudgedMaxSec"), D["staleJudgedMaxSec"], lo=1),
        "recoverUnreachable": _bool_or(l.get("recoverUnreachable"), D["recoverUnreachable"]),
        "reachabilityWindowSec": _int_or(l.get("reachabilityWindowSec"),
                                         D["reachabilityWindowSec"], lo=1),
        "reachabilityMinSample": _int_or(l.get("reachabilityMinSample"),
                                         D["reachabilityMinSample"], lo=1),
        # 边距是 0–1 之差，但**不做夹紧**：TS 保留任意有限值（≤0 = 关，>1 = 永不豁免），
        # 夹紧会让「配成 2 想彻底关掉」变成「配成 1 偶尔豁免」—— 又是一次静默改语义。
        "silenceExemptMarginMin": _num_or(l.get("silenceExemptMarginMin"),
                                          D["silenceExemptMarginMin"]),
    }


# ============================================================ 纯算法（可单测）


def round2(n: float) -> float:
    """保留 2 位小数 —— ``Math.round(n*100)/100`` 的等价（见模块 docstring 分歧 1）。

    ``Math.round(x)`` 的定义是 ``floor(x + 0.5)``（对负数是"往 +∞ 取"），
    恰好就是下面这一行的写法；Python 的 ``round`` 在平局时取偶数，会差一档。
    """
    return math.floor(n * 100 + 0.5) / 100


def _fixed(v: float, digits: int) -> str:
    """``Number.prototype.toFixed`` 的等价（日志/审计文案里的数字）。

    与 ``observe._fixed`` 同一套语义、**故意各留一份**：它只是格式化，
    真正必须共用的口径是 ``ambient_p`` 那一条（那份已经在 ``layer`` 里收口）。
    这里按 JS 语义对**二进制精确值**做 half-up（``Decimal(float)`` 取的正是它）。
    """
    try:
        quant = Decimal(1).scaleb(-digits)
        return str(Decimal(v).quantize(quant, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):        # 位数超出上下文精度 / 非数
        return f"{v:.{digits}f}"


@dataclass(frozen=True)
class HfThresholdEval:
    """:func:`eval_hf_threshold` 的结果（TS 侧是个可辨识联合，这里摊平成字段）。"""

    changed: bool
    #: 不变更 ⇒ ``"insufficient-sample"|"in-dead-zone"|"clamped-noop"``；
    #: 变更 ⇒ ``"rate=… total=… engaged=…"``（进审计 reason）
    reason: str
    new_threshold: float | None = None
    direction: str | None = None
    rate: float | None = None


def eval_hf_threshold(
    stats: Mapping[str, Any],
    learned_threshold: float | None,
    base_threshold: float,
    params: Mapping[str, Any],
) -> HfThresholdEval:
    """双向自适应判定（**纯函数**）。

    方向：接话率 ``≤ lowEngageRate`` ⇒ **上调**（少说精选）；``≥ highEngageRate`` ⇒
    **下调**（更主动）；两者之间是**死区** ⇒ 不变（天然滞回防抖）。
    护栏：区间钳制在函数内；``cur`` 的回落基线是 ``learned ?? base``。

    ⚠️ 钳制**只在 delta ≠ 0 时执行** —— 死区时直接 return，所以库里存量的旧值
    （地板抬高前落的 0.3）在死区期间会绕过地板。读取侧因此另有
    :func:`clamp_hf_threshold_to_band` 兜底（这就是 I8 的"两头都钳"）。
    """
    total = stats.get("total")
    engaged = stats.get("engaged")
    total_i = int(total) if _is_num(total) else 0
    engaged_i = int(engaged) if _is_num(engaged) else 0

    if total_i < _int_or(params.get("minSample"), 0, lo=0):
        return HfThresholdEval(False, "insufficient-sample")

    cur = learned_threshold if _is_num(learned_threshold) else float(base_threshold)
    rate = engaged_i / total_i
    low = _num_or(params.get("lowEngageRate"), 0.15, lo=0.0, hi=1.0)
    high = _num_or(params.get("highEngageRate"), 0.5, lo=0.0, hi=1.0)
    step = _num_or(params.get("step"), 0.05, lo=0.0, hi=1.0)
    band_min = _num_or(params.get("bandMin"), 0.5, lo=0.0, hi=1.0)
    band_max = _num_or(params.get("bandMax"), 0.9, lo=0.0, hi=1.0)

    delta = step if rate <= low else (-step if rate >= high else 0.0)
    if delta == 0:
        return HfThresholdEval(False, "in-dead-zone")

    raw = max(band_min, min(band_max, round2(cur + delta)))
    if raw == round2(cur):
        return HfThresholdEval(False, "clamped-noop")
    return HfThresholdEval(
        True,
        f"rate={_fixed(rate, 3)} total={total_i} engaged={engaged_i}",
        new_threshold=raw,
        direction="up" if delta > 0 else "down",
        rate=rate,
    )


def hf_cooldown_ok(last_change_at_sec: Any, now_sec: int, min_cooldown_sec: int) -> bool:
    """相邻两次阈值变更的冷却是否已过（``None`` ⇒ 从未变过 ⇒ 立即允许）。"""
    if last_change_at_sec is None:
        return True
    if not _is_num(last_change_at_sec):
        return True                      # 脏值当"没有记录"，放行比锁死安全（锁死是静默停摆）
    return now_sec - int(last_change_at_sec) >= int(min_cooldown_sec)


def clamp_hf_threshold_to_band(t: float, band_min: float, band_max: float) -> float:
    """v1.6.6：把阈值钳进 ``[band_min, band_max]``（纯函数）。**读取侧硬地板**。

    为什么光改 ``bandMin`` 不够（这个函数存在的全部理由）：:func:`eval_hf_threshold`
    的钳制只在 ``delta ≠ 0`` 时执行，接话率落死区时它直接 return ⇒ 库里存量的旧 learned
    值（如 0.3）在死区期间会继续生效，**永久绕过新地板**。所以读取侧
    （:func:`resolve_threshold_override`，即 judge 真正用的那个数）必须再钳一次。

    反过来**不**钳 :func:`eval_hf_threshold` 的 ``cur``：那会让库里旧值恒判
    ``clamped-noop`` 永不修复；保持原样，sweep 在非死区时会把 DB 自愈到地板值。
    """
    return round2(max(band_min, min(band_max, t)))


def hf_reachability_cap(
    scores: Sequence[Any],
    threshold: float,
    params: Mapping[str, Any],
) -> dict[str, Any] | None:
    """v1.10.0 可达性天花板（纯函数）：阈值不得高于"该群近期判过的最高分 + step"。

    语义（为什么是这个形状，而不是"什么都没过就调低"）：归因必须**无歧义**。
    "最近 N 条消息没有一条达到阈值" = 阈值落在裁判可给的分域之外 ⇒ 它不是在筛选，
    是在**关闸**。注意判据**不能**用"最近有没有发出"—— 冷清群本来就该一条不发，
    那是健康的；只有**分数够不着**才是坏的。

    上下界：

    * 上界 ``maxScore + step``：只比历史最好那条高一步，保证下一出现同类消息就能过；
    * 下界 ``bandMin``（0.5）：老板 2026-09-16 定的质量底线 —— 护栏绝不许把阈值压进
      垃圾分域（生产垃圾档实测 0.18–0.39，全部低于 0.5）。

    无数据 / 样本不足 / 最高分已经够得着 ⇒ ``None``（不设天花板，调用方保持原值）。
    """
    valid = [float(s) for s in scores if _is_num(s)]
    min_sample = max(1, _int_or(params.get("minSample"), 1, lo=0))
    if len(valid) < min_sample:
        return None
    max_score = max(valid)
    if max_score >= threshold:
        return None                      # 够得着 ⇒ 阈值可达，无需护栏
    band_min = _num_or(params.get("bandMin"), 0.5, lo=0.0, hi=1.0)
    step = _num_or(params.get("step"), 0.05, lo=0.0, hi=1.0)
    cap = max(band_min, round2(max_score + step))
    # 天花板 ≥ 原阈值时它不起作用（例如 maxScore 0.58 + 0.05 = 0.63 > 阈值 0.6 ——
    # 那是"刚好差一点"，属正常滞回）
    if cap >= threshold:
        return None
    return {"cap": cap, "maxScore": max_score, "n": len(valid)}


#: 发送结果三态（``ok=True`` ≠ 真发）。
HF_SEND_OUTCOME_SENT = "sent"
HF_SEND_OUTCOME_SUPPRESSED = "suppressed"
HF_SEND_OUTCOME_PENDING = "pending"


def _send_field(result: Any, name: str) -> Any:
    """从发送结果里取字段：``Mapping``（``"ok"``/``"msg_id"`` 键）或对象属性都行。"""
    if isinstance(result, Mapping):
        return result.get(name)
    return getattr(result, name, None)


@dataclass(frozen=True)
class HfSendResult:
    """发送结果（:func:`classify_hf_send` 的输入形状）。

    ⚠️ **这不是** ``plugin.platform.contracts.SendResult``（那个是
    ``success`` / ``message_id``）—— 两者**故意不通用**，见 :func:`classify_hf_send`
    的"形状不匹配"一段。交付侧（#9）必须显式翻译成这个形状。
    """

    ok: bool
    #: 占位符（见 :data:`HF_PLACEHOLDER_MSG_IDS`）/ vendor 回的 msgId / ``None``（vendor 没回）
    msg_id: str | None = None


def classify_hf_send(result: Any) -> str:
    """真发判别：``ok: true`` **不等于**真发（dedup/ack/重复闸/空文本都是占位符）。

    * ``ok`` 非真 ⇒ ``pending``（等框架重试，**不改任何状态**）；
    * ``msg_id`` 命中 :data:`HF_PLACEHOLDER_MSG_IDS` 或为 ``""`` ⇒ ``suppressed``；
    * 其余（含 ``msg_id`` 为 ``None``＝vendor 没回 id）⇒ ``sent``。

    ⚠️ **形状不匹配要吵，不许静默**：``ok`` 读不到时（典型误用：直接塞
    ``platform.SendResult(success=…, message_id=…)``）本函数返回 ``pending`` —— 而
    pending 的语义是"什么都不改"。若检测到对方其实带了 ``success``/``message_id``，
    打一条 warning 说明**该翻译形状**：否则表现是"消息发出去了但账本永远是 pending"，
    既不占预算也不开窗（学习闭环少一半样本），且**日志里一句错都没有**。
    """
    ok = _send_field(result, "ok")
    if ok is not True:
        if _looks_like_platform_send_result(result):
            log.warning("[WPP HF] 发送结果形状不匹配（缺 ok/msg_id，像 platform.SendResult）"
                        "⇒ 判成 pending（不记账、不开窗）。交付侧必须翻译成 HfSendResult")
        return HF_SEND_OUTCOME_PENDING
    msg_id = _send_field(result, "msg_id")
    if msg_id is None and _looks_like_platform_send_result(result):
        log.warning("[WPP HF] 发送结果形状不匹配：有 success/message_id 但无 msg_id ⇒ "
                    "msgId 会丢失（引用匹配退化为同群+窗内）。交付侧必须翻译成 HfSendResult")
    if msg_id is not None and msg_id in HF_PLACEHOLDER_MSG_IDS:
        return HF_SEND_OUTCOME_SUPPRESSED
    if msg_id == "":
        return HF_SEND_OUTCOME_SUPPRESSED
    return HF_SEND_OUTCOME_SENT


def _looks_like_platform_send_result(result: Any) -> bool:
    """是否是"另一个家族的"发送结果（``success``/``message_id`` 那一套）。

    只用于**判断要不要吵一声**，不参与任何判定分支 —— 判定只认 ``ok``/``msg_id``。
    """
    if _send_field(result, "ok") is not None or _send_field(result, "msg_id") is not None:
        return False
    return (_send_field(result, "success") is not None
            or _send_field(result, "message_id") is not None)


# ============================ 内存缓存（judge 热路径零 DB IO 的三处真源）


def _key(account_id: str, group_id: str) -> str:
    return f"{account_id}:{group_id}"


#: ``"{account_id}:{group_id}"`` → 该群当前 learned 阈值
_learned_thresholds: dict[str, float] = {}


@dataclass(frozen=True)
class HfOpenWindow:
    """每群"正在观察中"的那次心流发言（``onFlush`` 判接话要用的最小状态）。

    为什么必须有：判"有人接了我那句"要知道**我在这个群刚发了什么 / 什么时候发的**。
    旧码不知道（只能退化成"群里有没有人说话"）；改成每条人类消息都查一次 DB，又成了
    群聊热路径上的每消息一次查询。内存表让"没开窗"这个绝大多数情况**零成本直接 return**。

    生命周期：:func:`persist_hf_send_outcome`（真发出）登记 → :func:`mark_hf_group_engaged`
    收敛时删（弱信号保留等升级）→ sweep 到期清理（防重启后残留）。
    进程重启会丢 ⇒ 丢窗口那几行按 ``silence`` 收敛（已知降级：影响面 = 重启前
    ``observeWindowSec`` 内发过心流回复的群，且只损失一次样本）。
    """

    #: v1.10.0：本窗对应的**那条台账行**（``ledger.inbound_msg_id``）。
    #:
    #: 为什么必须记住它：收敛标记过去只按 ``(账号, 群, status='sent')`` 匹配 ⇒ 同群有
    #: **两条** sent 行重叠时（``minGapSec < observeWindowSec`` 即可达，生产
    #: ``minReplyIntervalSec=0`` + ``minGapSec=180`` 就满足），一句"有人引用了我"
    #: 会给**两条**都记 ``engaged=1``、一句"别刷了"会**罚两条** —— 一条信号被算两遍，
    #: 样本量凭空翻倍、接话率被稀释/放大，学习直接被喂脏数据。故收敛必须精确到行。
    inbound_msg_id: str
    #: bot 发出那条的 vendor msgId（``None`` = vendor 未回 id ⇒ 引用匹配退化为
    #: "同群 outbound + 窗内"）
    bot_msg_id: str | None
    sent_at_sec: int
    observe_window_sec: int
    #: 弱信号窗（只有这段时间内的人类消息才算"可能接了我那句"，强信号不受此限）
    label_window_sec: int


#: ``"{account_id}:{group_id}"`` → 开窗
_open_windows: dict[str, HfOpenWindow] = {}


@dataclass(frozen=True)
class HfReachCap:
    """v1.10.0 可达性天花板缓存（每群一份，由 sweep 每轮一条聚合查询刷新）。"""

    cap: float
    max_score: float
    n: int
    #: 刷新时刻（供告警节流：首次出现 / 值变化 / 每小时一次）
    at_sec: int


#: ``"{account_id}:{group_id}"`` → 天花板
_reach_caps: dict[str, HfReachCap] = {}


def get_learned_threshold(account_id: str, group_id: str) -> float | None:
    """读单群 learned 阈值（无 ⇒ ``None``）。**judge 热路径读口，纯内存。**"""
    return _learned_thresholds.get(_key(account_id, group_id))


def clamp_hf_threshold_to_reachability(account_id: str, group_id: str, t: float) -> float:
    """读取侧可达性钳制：返回**不高于**天花板的阈值（无天花板 ⇒ 原值）。

    与 :func:`clamp_hf_threshold_to_band` 同为**读取侧硬保证** —— 天花板来自 sweep
    的观测，这里只做取 ``min``。
    """
    c = _reach_caps.get(_key(account_id, group_id))
    if c is None:
        return t
    return min(t, c.cap)


def get_hf_reachability_cap(account_id: str, group_id: str) -> HfReachCap | None:
    """该群当前的可达性天花板（供 ``/heartflow status`` / ``why`` 展示；无 ⇒ ``None``）。"""
    return _reach_caps.get(_key(account_id, group_id))


def get_open_hf_window(account_id: str, group_id: str) -> HfOpenWindow | None:
    """取该群开窗信息（handler 判"引用的这条是不是 bot 刚发的那条"）。

    返回的是 frozen dataclass（调用方改不动）—— 见模块 docstring 分歧 7。
    """
    return _open_windows.get(_key(account_id, group_id))


def forget_hf_open_window(account_id: str, group_id: str) -> None:
    """v1.8.0 ``/heartflow veto``：删掉该群的开窗内存记录。

    为什么**必须**做（最容易漏的一步）：veto 只作用于已 sent 的行，而 sent 那一刻已经
    登记了开窗。不删窗的话，若随后有人引用了那条 bot 消息，
    :func:`mark_hf_group_engaged` 会用 ``engaged=1, signal='quote'`` **覆盖 veto** ——
    样本从"不该回"翻转成"该回"，比老板不点这一下更糟。
    """
    _open_windows.pop(_key(account_id, group_id), None)


# ==================================================== 阈值决策（judge 热路径）


@dataclass(frozen=True)
class HfLayerStep:
    """分层"一步"的结果。"""

    threshold: float
    changed: bool


@dataclass(frozen=True)
class HfShadow:
    """影子建议：样本够但 ``layered.apply=False`` 时"若生效会是多少"。"""

    threshold: float
    layer_key: str
    n: int
    engaged: int
    rate: float


@dataclass(frozen=True)
class HfThresholdDecision:
    """v1.8.0 阈值决策（**纯内存**：judge 热路径零 DB 读）。

    链路：段内样本够 且 ``layered.apply`` ⇒ **分层值** → 否则群级 learned →
    否则账号级 ``base``。分层值**不是独立自学的标量**，而是"从群级锚点出发、按段内
    接话率走**一步** :func:`eval_hf_threshold`"（±step，被 band 钳死）⇒ 不累积、
    不漂移，所以**不需要冷却**（值本身跑不飞）。段内样本不够 / 接话率落死区 ⇒
    一律回落群级锚点（这就是老板要的"不够就回落先验"）。
    """

    #: 真正应生效的阈值（已过读侧钳制）；``None`` = 无覆盖 ⇒ 调用方沿用账号级 cfg
    applied: float | None
    #: 生效来源
    source: str                                      # "layer" | "group" | "account"
    #: 当前时刻所处的段名（无论该段有无样本；未覆盖 ⇒ ``None``）
    layer_key: str | None
    #: 该段可采信样本数 / 其中被接话数（无数据 ⇒ 0）
    layer_n: int
    layer_engaged: int
    #: 段内接话率（无样本 ⇒ ``None``）
    layer_rate: float | None
    #: 分层是否真正生效（= ``layered.enabled && layered.apply``）
    apply: bool
    #: 影子建议（样本不够 / 死区 ⇒ ``None``）
    shadow: HfShadow | None


def hf_layer_anchor_for(account_id: str, group_id: str,
                        hf_cfg: Mapping[str, Any] | None) -> float:
    """群级锚点（分层每段都从它出发）：群级 learned（无则账号级），**已过读侧钳制**。

    库里存量的旧值（地板抬高前落的 0.3）也必须被钳回来 —— 与 v1.6.6 同一道保证。
    """
    L = resolve_hf_learning(hf_cfg)
    learned = get_learned_threshold(account_id, group_id)
    base = resolve_reply_threshold(hf_cfg) if learned is None else learned
    return clamp_hf_threshold_to_band(base, L["bandMin"], L["bandMax"])


def hf_layer_step(anchor: float, n: int, engaged: int,
                  hf_cfg: Mapping[str, Any] | None) -> HfLayerStep:
    """分层"一步"预览（纯函数，供决策与 ``/heartflow layers`` 共用）。

    从群级锚点出发，按该段接话率走一步 :func:`eval_hf_threshold` —— **复用调阈的
    同一套参数**（死区 / step / band），绝不写第二套判定参数；段内样本不够时
    ``eval_hf_threshold`` 自带 ``insufficient-sample``。
    ``allowLoosen=False``（默认）⇒ 只许**收紧**（阈值只许变高 = 更克制）。
    """
    L = resolve_hf_learning(hf_cfg)
    LAY = layer.resolve_hf_layered_cfg(hf_cfg)
    r = eval_hf_threshold(
        {"total": n, "engaged": engaged},
        anchor,
        resolve_reply_threshold(hf_cfg),
        {
            "minSample": LAY.min_samples,
            "lowEngageRate": L["lowEngageRate"],
            "highEngageRate": L["highEngageRate"],
            "step": L["step"],
            "bandMin": L["bandMin"],
            "bandMax": L["bandMax"],
        },
    )
    if not r.changed or r.new_threshold is None:
        return HfLayerStep(anchor, False)
    t = r.new_threshold if LAY.allow_loosen else max(anchor, r.new_threshold)
    return HfLayerStep(t, t != anchor)


def resolve_hf_threshold_decision(
    account_id: str,
    group_id: str,
    hf_cfg: Mapping[str, Any] | None,
    now_sec: int | None = None,
) -> HfThresholdDecision:
    """v1.8.0 阈值决策（**纯内存**，见 :class:`HfThresholdDecision`）。"""
    ts = int(time.time()) if now_sec is None else int(now_sec)
    LAY = layer.resolve_hf_layered_cfg(hf_cfg)
    learned = get_learned_threshold(account_id, group_id)
    anchor = hf_layer_anchor_for(account_id, group_id, hf_cfg)
    layer_key = (layer.hf_layer_key_for(budget.hf_local_hour(ts), LAY.buckets)
                 if LAY.enabled else None)
    hit = (None if layer_key is None
           else layer.get_hf_layer_stat(account_id, group_id, ts, LAY.buckets))

    rate = None
    if hit is not None and hit.stat.n > 0:
        rate = hit.stat.engaged / hit.stat.n
    enough = hit is not None and hit.stat.n >= LAY.min_samples
    d = (hf_layer_step(anchor, hit.stat.n, hit.stat.engaged, hf_cfg)
         if (LAY.apply and enough) else HfLayerStep(anchor, False))
    layered_on = bool(LAY.apply and LAY.enabled and enough and d.changed)

    shadow = None
    if not LAY.apply and LAY.enabled and enough:
        s = hf_layer_step(anchor, hit.stat.n, hit.stat.engaged, hf_cfg)
        if s.changed:
            shadow = HfShadow(threshold=s.threshold, layer_key=hit.key, n=hit.stat.n,
                              engaged=hit.stat.engaged,
                              rate=0.0 if rate is None else rate)

    key_out = hit.key if hit is not None else layer_key
    n_out = hit.stat.n if hit is not None else 0
    eng_out = hit.stat.engaged if hit is not None else 0

    if layered_on:
        return HfThresholdDecision(applied=d.threshold, source="layer", layer_key=key_out,
                                   layer_n=n_out, layer_engaged=eng_out, layer_rate=rate,
                                   apply=True, shadow=None)
    return HfThresholdDecision(
        applied=None if learned is None else anchor,
        source="account" if learned is None else "group",
        layer_key=key_out, layer_n=n_out, layer_engaged=eng_out, layer_rate=rate,
        apply=bool(LAY.apply and LAY.enabled), shadow=shadow,
    )


def resolve_threshold_override(
    account_id: str,
    group_id: str,
    hf_cfg: Mapping[str, Any] | None,
    now_sec: int | None = None,
) -> float | None:
    """应 override 的阈值（≠账号级时返回；否则 ``None`` ⇒ 调用方沿用原 cfg，不 clone）。

    v1.8.0 加第 4 参（照 :func:`mark_hf_group_engaged` 的先例）—— 分层要按"当前时段"取值。
    """
    d = resolve_hf_threshold_decision(account_id, group_id, hf_cfg, now_sec)
    if d.applied is None:
        return None
    base = resolve_reply_threshold(hf_cfg)
    # v1.6.6 硬地板/上限：读时再钳一次，保证"阈值最低不低于 bandMin"不依赖 DB 里旧值
    # 是否会被 sweep 修好。局部名沿用 learned：分层生效时它已是"从群级锚点走了半步"
    # 的值，但读侧钳制对**两种来源都**是必需的硬保证（库里可能还存着 0.3）。
    L = resolve_hf_learning(hf_cfg)
    eff = clamp_hf_threshold_to_band(d.applied, L["bandMin"], L["bandMax"])
    if abs(eff - base) < 1e-9:
        return None
    return eff


@dataclass(frozen=True)
class HfReachInfo:
    """天花板依据（窗口内判定条数 / 最高分）。"""

    n: int
    max_score: float


@dataclass(frozen=True)
class HfEffectiveThreshold:
    """生效阈值的完整分解（判定用 ``threshold``，其余字段只供 ``/heartflow status``·``why``）。"""

    #: 真正生效的阈值 —— judge 拿它与 ``judge_overall`` 比的就是这个
    threshold: float
    #: 账号级基线（``heartflow.replyThreshold``）
    base: float
    #: 群级 learned / 分层覆盖值（未覆盖 ⇒ ``None``）
    override: float | None
    #: 画像建议的 band（无画像 ⇒ ``None``）
    band: float | None
    #: 画像抬升被上限截断（画像想要得更高，但只给了 ``base + maxRaise``）
    band_capped: bool
    #: 可达性天花板（**未被压 ⇒ ``None``**）
    reach_cap: float | None
    #: 天花板依据（无天花板 ⇒ ``None``）
    reach_info: HfReachInfo | None


def resolve_hf_effective_threshold(
    account_id: str,
    group_id: str,
    hf_cfg: Mapping[str, Any] | None,
    now_sec: int | None = None,
) -> HfEffectiveThreshold:
    """v1.10.0：**唯一**的"生效阈值"计算入口 —— judge 判定与 ``/heartflow status`` 共用。

    为什么必须收口成一个函数：2026-09-26 的静默停摆之所以能藏 3 天，一半原因是
    **各处各算一份** —— judge 按 profile band 抬到 0.85，状态页却打印账号级 0.6
    （看起来一切正常），sweep 的告警又是第三份。只要三处不共用同一段代码，它们迟早
    会漂移，而漂移的那一刻恰好就是"看起来正常但已经死了"。

    链路顺序（与 TS 逐字等价）：学习/分层覆盖 → 画像 band **有界抬升** → 可达性天花板。

    ⚠️ judge 热路径：体内不得出现**导入语句 / SQL / 任何 IO**（见 ``test_learn.py`` 的源级守卫）。
    """
    base = resolve_reply_threshold(hf_cfg)
    override = resolve_threshold_override(account_id, group_id, hf_cfg, now_sec)
    band_eff = profile.resolve_hf_profile_band_effect_for(
        account_id, group_id, override if override is not None else base, hf_cfg)
    cap = get_hf_reachability_cap(account_id, group_id)
    threshold = clamp_hf_threshold_to_reachability(account_id, group_id, band_eff["threshold"])
    bit = cap is not None and threshold < band_eff["threshold"] - 1e-9
    return HfEffectiveThreshold(
        threshold=threshold,
        base=base,
        override=override,
        band=band_eff["band"],
        band_capped=bool(band_eff["capped"]),
        reach_cap=cap.cap if bit else None,
        reach_info=HfReachInfo(n=cap.n, max_score=cap.max_score) if bit else None,
    )


# ==================================================== DB 薄管线（全 catch）


@dataclass(frozen=True)
class HfLedgerRecord:
    """judge 的判定记录 → ``wpp_hf_ledger`` 一行（字段与 DDL 列名一一对应）。"""

    account_id: str
    inbound_msg_id: str
    group_id: str
    judged_at: int
    new_msg_id: str | None = None
    from_wxid: str | None = None
    msg_type: str | None = None
    content_head: str | None = None
    judge_overall: float | None = None
    dim_r: float | None = None
    dim_w: float | None = None
    dim_s: float | None = None
    dim_t: float | None = None
    dim_c: float | None = None
    effective_threshold: float | None = None
    energy: float | None = None


def _insert_judged(record: HfLedgerRecord) -> None:
    from ...data import repo_hf  # 延迟导入：加载期不许拉进第三方依赖（F5）

    repo_hf.insert_judged(
        record.account_id, record.inbound_msg_id, record.group_id, record.judged_at,
        new_msg_id=record.new_msg_id, from_wxid=record.from_wxid, msg_type=record.msg_type,
        content_head=record.content_head, judge_overall=record.judge_overall,
        dim_r=record.dim_r, dim_w=record.dim_w, dim_s=record.dim_s, dim_t=record.dim_t,
        dim_c=record.dim_c, effective_threshold=record.effective_threshold,
        energy=record.energy,
    )


def persist_hf_judged(record: HfLedgerRecord) -> None:
    """judge 通过、落一条 ledger 行（insert 失败仅 warn，**不阻断 dispatch**）。"""
    try:
        _insert_judged(record)
    except Exception as exc:  # noqa: BLE001 —— 台账写失败不该让回复发不出去
        log.warning("[WPP HF] ledger insert failed (non-fatal): %s", exc)


def persist_hf_judged_below_threshold(record: HfLedgerRecord, at_sec: int) -> None:
    """judge 跑了、但判定**不回复** → 也落一行 ledger（judged → 立即 suppressed）。

    ``suppressed_reason='below-threshold'``。为什么要有（2026-09-13）：旧码只在
    ``shouldReply=True`` 时落 ledger ⇒「judge 跑了但没过」在**日志与 DB 里零留痕**，
    与「群里压根没消息」外观完全一致。09-11 静默瘫 3 天正是被这一点掩盖：台账最后
    一行停在 09-10，而真实原因（judge 恒失败）只在那条 warn 里。

    为什么可安全加：suppressed 行**不进阈值学习样本** —— ``get_closed_recent`` 与
    ``list_distinct_closed_groups`` 都要求 ``status='closed' AND engaged IS NOT NULL``，
    故学习闭环与既有行为**零变更**；频率 = 每群每个 judge 冷却周期最多一行
    （``minJudgeIntervalSec`` 闸）。

    注：走 INSERT 后再 UPDATE 两步，复用既有状态机（``INSERT IGNORE`` 幂等 +
    仅 ``judged`` 可推进的 guard），不新增 SQL 分支。
    """
    try:
        _insert_judged(record)
        from ...data import repo_hf
        repo_hf.set_suppressed(record.account_id, record.inbound_msg_id,
                               "below-threshold", at_sec)
    except Exception as exc:  # noqa: BLE001
        log.warning("[WPP HF] ledger below-threshold insert failed (non-fatal): %s", exc)


def persist_hf_send_outcome(
    account_id: str,
    inbound_msg_id: str,
    result: Any,
    at_sec: int,
    *,
    group_id: str,
    observe_sec: int,
    label_window_sec: int,
    text: str | None = None,
    dedupe_cfg: dedupe.HfDedupeConfig | None = None,
) -> None:
    """deliver 之后落发送结果：``sent`` → 开观察窗；``suppressed`` → 收敛；``pending`` → 不动。

    v1.6.8：``sent`` 时额外 ① 把 bot 自己那条的 msgId 落库（判"有人引用了我那条"）
    ② 在内存里登记开窗，供 ``onFlush`` 的接话判定用（零 DB IO）。
    v1.6.9：**真发出去**才占发言预算额度（判了但被下游拦掉的不占）—— 与开窗同一时刻同一条件。
    v1.9.0：同一条件下把文本记进重复闸历史 —— 只有真发出去的文本才有资格"被重复"
    （被预算/闸拦下的、vendor 去重掉的都不能进历史，否则会拿"没说出口的话"去拦下一句）。

    ⚠️ **INVARIANT I5**：占位/被抑制的消息**不得**走到开窗与预算那两行
    （见 :func:`classify_hf_send` 与本函数的 ``outcome == sent`` 分支）。

    ⚠️ B4（2026-10-02 修）：**账本与内存三件套分成互不连累的几段**。旧写法把这几条
    语句塞进**同一个** ``try``，两个方向都会坏：

    ① ``set_sent`` 一炸（DB 抖动 / 连接断了），后面三条**全不执行** —— 消息已经发到
       群里、撤不回来了，却不占额度、不进复读历史、观察窗也不开 ⇒ 学习闭环少一个
       样本，而台账上那行停在 ``judged`` 等 sweep 收敛成"没人接话"（负样本）。
    ② 反过来：账本那次写**已经提交**了，内存里某一步炸掉却让整个分支退出 ⇒ 两边
       **永久不一致**（库里说发了，内存里像没发过）。

    另有一处**静默**：``set_sent`` 的 guard 只在 ``judged``（或从未发过的
    ``suppressed``）上生效，**受影响行数为 0 时不抛异常**。``persist_hf_judged``
    那次 INSERT 没落上时就是这种局面 —— 消息发出去、账本里零痕迹、一行日志都没有。
    故这里把行数 0 也当失败处理（只 warn，不抛）。
    """
    from ...data import repo_hf  # 延迟导入，同上

    outcome = classify_hf_send(result)
    if outcome == HF_SEND_OUTCOME_SENT:
        metrics = _metrics()                 # 延迟导入：埋点也要走 F5 的加载期纪律
        metrics.inc_hf_sent()
        raw_id = _send_field(result, "msg_id")
        bot_msg_id = raw_id if raw_id else None

        # ① 账本（DB）：失败只 warn，**不 return** —— 预算与复读历史活在内存里，
        #    账本坏了不该把它们的记性也一起抹掉（见 docstring B4 ①）。
        with_window = True
        try:
            if not repo_hf.set_sent(account_id, inbound_msg_id, at_sec,
                                    at_sec + int(observe_sec), bot_msg_id):
                with_window = False
                log.warning("[WPP HF] 账本 set_sent 没匹配到行（受影响行数 0，多半是 "
                            "judged 行缺失）⇒ 不开观察窗，仅记预算与复读历史 "
                            "(non-fatal): account=%s msgId=%s", account_id, inbound_msg_id)
        except Exception as exc:  # noqa: BLE001 —— 见 docstring B4 ①
            with_window = False
            log.warning("[WPP HF] 账本 set_sent 失败，本条只记预算与复读历史 "
                        "(non-fatal): account=%s msgId=%s: %s",
                        account_id, inbound_msg_id, exc)

        # ② 观察窗（内存）：**只在账本真进了 ``sent`` 时开**。窗的唯一消费者
        #    ``repo_hf.mark_engaged`` 要求 ``status='sent'``，账本没写成时开窗纯属占内存。
        if with_window:
            k = _key(account_id, group_id)
            prev = _open_windows.get(k)
            # v1.10.0：若覆盖掉一个还没收敛的旧窗，说明该群在 observeWindowSec 内发了第二条
            #   —— 旧窗从此不再收到"接话"信号，会按 silence 收敛（语义正确：它确实没人接）。
            #   留一行 debug 让这种重叠可见（它是样本可信度的一个风险点）。
            if prev is not None and prev.sent_at_sec + prev.observe_window_sec > at_sec:
                log.debug("[WPP HF] window overlap: account=%s group=%s prev=%s (%ds ago) "
                          "⇒ prev 按 silence 收敛", account_id, group_id, prev.inbound_msg_id,
                          at_sec - prev.sent_at_sec)
            _open_windows[k] = HfOpenWindow(
                inbound_msg_id=inbound_msg_id, bot_msg_id=bot_msg_id, sent_at_sec=int(at_sec),
                observe_window_sec=int(observe_sec), label_window_sec=int(label_window_sec))

        # ③ 预算：**与 ①② 各自独立** —— 它和观察窗无关，必须无论如何都记上
        #    （漏记 = 给"重启/抖动刷额度"开后门）。
        try:
            budget.note_reply_sent(account_id, group_id, at_sec)
        except Exception as exc:  # noqa: BLE001 —— 同上
            log.warning("[WPP HF] 预算记账失败 (non-fatal): %s", exc)

        # ④ 复读历史：同上，独立一段
        if text:
            try:
                dedupe.note_recent_reply(account_id, group_id, text, at_sec,
                                         dedupe_cfg or dedupe.resolve_dedupe_cfg(None))
            except Exception as exc:  # noqa: BLE001 —— 同上
                log.warning("[WPP HF] 复读历史记账失败 (non-fatal): %s", exc)
        return

    if outcome == HF_SEND_OUTCOME_SUPPRESSED:
        try:
            msg_id = _send_field(result, "msg_id")
            if msg_id == "ack-template-dropped":
                reason = "ack-template"
            elif msg_id == "dedup-suppressed":
                reason = "dedup"
            elif msg_id == "repeat-suppressed":
                reason = "repeat"
            elif msg_id == "":
                reason = "empty-text"
            else:
                reason = "unknown"
            repo_hf.set_suppressed(account_id, inbound_msg_id, reason, at_sec)
        except Exception as exc:  # noqa: BLE001 —— 记账失败不该影响已经发出去的那条
            log.warning("[WPP HF] ledger send outcome failed (non-fatal): %s", exc)
    # pending: 不动（留给 sweep 的呆账收敛）


def mark_hf_group_engaged(
    account_id: str,
    group_id: str,
    at_sec: int,
    candidates: Sequence[label.HfEngageCandidate],
) -> None:
    """``onFlush`` 人类消息 → 按**信号**判定该群开窗行（v1.6.8 换标签的核心）。

    旧行为（v1.6.x 至 v1.6.7）：只要群里出现人类消息就 ``engaged=1`` + 关窗 ⇒
    活跃群恒饱和 ⇒ 阈值单调降。现行为见 :mod:`.label` —— 引用 bot / @bot / 针对 bot
    的负词 = 强信号；窄窗内有人说话 = 弱信号；全部候选一起判，弱信号**不关窗**留待升级。

    无开窗（绝大多数情况）或本行无信号 ⇒ 立刻 return，**不做任何 DB 写**。
    """
    from ...data import repo_hf  # 延迟导入，同上

    k = _key(account_id, group_id)
    w = _open_windows.get(k)
    if w is None:
        return
    verdict = label.classify_hf_engagement(label.HfEngagementInput(
        sent_at_sec=w.sent_at_sec,
        label_window_sec=w.label_window_sec,
        observe_window_sec=w.observe_window_sec,
        candidates=tuple(candidates),
    ))
    if verdict.signal is None or verdict.engaged is None:
        return
    try:
        # v1.10.0：精确到**这一行**（w.inbound_msg_id）—— 见 HfOpenWindow.inbound_msg_id 注释
        repo_hf.mark_engaged(account_id, group_id, at_sec, verdict.engaged, verdict.signal,
                             verdict.close, w.inbound_msg_id)
        if verdict.close:
            _open_windows.pop(k, None)   # 弱信号仍留在表里等更强信号升级
    except Exception as exc:  # noqa: BLE001 —— 记不上就丢了这一次样本，不影响收发
        log.debug("[WPP HF] mark engaged failed (non-fatal): %s", exc)


def load_learned_thresholds(account_id: str) -> int:
    """账号启动 / 热载后从 DB 加载 learned 阈值进内存缓存。返回加载的行数。

    失败降级：账号以**空缓存**启动（= 全群回落账号级阈值），warn 一次。
    """
    from ...data import repo_hf  # 延迟导入，同上

    n = 0
    try:
        rows = repo_hf.list_group_states(account_id)
        for r in rows:
            v = r.get("learned_threshold")
            if v is not None and _is_num(v):
                _learned_thresholds[_key(account_id, str(r["group_id"]))] = float(v)
                n += 1
    except Exception as exc:  # noqa: BLE001
        log.warning("[WPP HF] loadLearnedThresholds failed (account starts w/o learned): %s", exc)
    return n


def load_hf_budget_seed(account_id: str, now_sec: int | None = None) -> int:
    """v1.6.9 账号启动：从账本回填发言预算的小时/天计数（**重启不清零额度**）。

    为什么必须回填：预算计数在内存里 —— 若只在进程内累加，一次重启就把"今天已发 60 条"
    清零，等于给"重启刷额度"开了后门（插件在部署/热重载时会重启，那不是老板想要的豁免）。

    边界口径：小时边界取**本地整点**、天边界取**本地零点**，与运行时桶键
    （``hf_hour_bucket_key`` / ``hf_day_bucket_key``）完全一致；否则新小时/新的一天
    会带着上一段的余数开局。

    已知降级：``lastHumanAtSec`` 无法从账本回填（账本没有人类消息时刻）⇒ 留空。
    """
    from ...data import repo_hf  # 延迟导入，同上

    ts = int(time.time()) if now_sec is None else int(now_sec)
    try:
        rows = repo_hf.list_sent_counts_recent(
            account_id, budget.hf_hour_start_sec(ts), budget.hf_day_start_sec(ts))
        n = budget.seed_budget_states(account_id, rows, ts)
        if n > 0:
            log.debug("[WPP HF] budget seed loaded: account=%s groups=%d", account_id, n)
        return n
    except Exception as exc:  # noqa: BLE001 —— 回填失败 = 计数从 0 开始（比不回填更松，但不炸）
        log.warning("[WPP HF] budget seed failed (counters start at 0): %s", exc)
        return 0


def reset_learned_threshold_cache() -> None:
    """测试 / 重置：清空 learned + 开窗表 + 天花板 + 分层缓存 + 重复历史 + 占比外环状态。

    ⚠️ 这里**故意**连带清掉另外**三个**模块的内存（TS ``resetLearnedThresholdCache``
    逐字如此：``resetHfLayerCache`` / ``resetHfDedupeStore`` / ``resetHfShareGuard``）：
    它们与调阈共用同一批样本，只清一半的表现是"清完还脏"，比不清更难查。

    画像缓存**不在**这三者里 —— 它由 LLM 单独生成、按 ``generatedAt`` 自己过期，
    与本函数清的那批样本不同源；它有自己的登记名（``profile``），
    ``CACHE_REGISTRY.reset_all()`` 才会一并清掉。
    """
    _learned_thresholds.clear()
    _open_windows.clear()
    _reach_caps.clear()
    layer.reset_hf_layer_cache()
    dedupe.reset_dedupe_store()
    observe.reset_hf_share_guard()


CACHE_REGISTRY.register("learn", reset_learned_threshold_cache)


def _metrics():
    """延迟导入埋点模块（加载期纪律，同 F5）。"""
    from ...core import metrics
    return metrics


# ============================================================ sweep（每 300s）


def _allowed_filter(cfg: Mapping[str, Any] | None) -> Callable[[str], bool] | None:
    """群白名单判定的**唯一来源**：``heartflow.is_hf_group_allowed``（延迟导入）。

    为什么延迟：``heartflow``(#9) 反向 import 本模块 ⇒ 模块级 import 成环。
    为什么**不**写一份兜底实现：白名单语义只许有一处，复制必漂移。取不到 heartflow 时
    退化成"不过滤"，而这与"白名单为空"**语义完全相同**（空 / 缺省 ⇒ 全放行），
    所以没有第二份规则可漂移。退化本身记 debug —— 它在生产上不该发生。
    """
    try:
        from . import heartflow as _hf
    except Exception as exc:  # noqa: BLE001 —— #9 未就位时不该挡住 sweep
        log.debug("[WPP HF] heartflow 未就位 ⇒ 本轮不做白名单过滤（等价白名单为空）: %s", exc)
        return None
    return lambda gid: bool(_hf.is_hf_group_allowed(gid, cfg))


async def run_sweep(adapter: Any, account_id: str, now_sec: int | None = None) -> None:
    """每账号一轮 sweep（顺序见模块 docstring；每步各自 try/except）。

    ``adapter`` 用来取该账号当前的配置（**运行中唯一真源**，``/heartflow on`` 改的就是它）；
    取不到再回落 ``accounts.resolve``（离线 / 测试）。两处都取不到 ⇒ 空配置
    （= 全缺省 + ``enabled=False``）⇒ 本轮只跑关窗与呆账收敛。
    """
    from ...data import repo_hf  # 延迟导入，同上

    ts = int(time.time()) if now_sec is None else int(now_sec)
    cfg = _cfg_for(adapter, account_id)
    L = resolve_hf_learning(cfg)

    # 1) 关窗 + 呆账收敛（**无论学习开关都跑**：防 pending/超期行无限堆积）
    try:
        repo_hf.close_expired_windows(account_id, ts)
        repo_hf.expire_stale_judged(account_id, ts, ts - L["staleJudgedMaxSec"])
        prune_open_windows(account_id, ts)
    except Exception as exc:  # noqa: BLE001 —— 本轮到此为止，下一轮补上
        log.warning("[WPP HF] sweep close/expire failed: %s", exc)
        return

    if not cfg.get("enabled"):
        return

    gf = _allowed_filter(cfg)

    # 2) v1.7.0 群画像：每群每日一次（内部按 generated_at 判新旧 + 单轮配额）。
    #    放在 learning.enabled 判定**之前** —— 画像不依赖调阈开关，且内部全 catch。
    try:
        await profile.maybe_generate_hf_group_profiles(account_id, cfg, ts, group_filter=gf)
    except Exception as exc:  # noqa: BLE001 —— 防御性兜底
        log.warning("[WPP HF] profile pass failed (不影响调阈): %s", exc)

    # 3) v1.8.0 分层统计（群 × 时段）：每轮**全量重算** + 只 upsert 有变化的行（稳态 0 写）。
    #    同样在 learning.enabled 判定之前（分层是"观测 + 影子建议"）。
    #    ⚠️ label_window_sec / ambient_max 必须传**这一份** L —— 两个 pass 口径不同的话，
    #       同一条样本会在调阈侧"可采信"、在分层侧"不可采信"，n 永远对不上。
    try:
        layer.maybe_recompute_hf_layer_stats(
            account_id, cfg, ts, label_window_sec=L["labelWindowSec"],
            ambient_max=L["ambientMax"], group_filter=gf)
    except Exception as exc:  # noqa: BLE001 —— 防御性兜底
        log.warning("[WPP HF] layer pass failed (不影响调阈): %s", exc)

    # 4) v1.9.0 占比外环（老板拍板：bot 发言占比 > 目标 ⇒ 收紧当日预算）：
    #    DB 聚合只在这里做，结果落内存 → judge 读侧零 DB IO。与调阈开关无关。
    try:
        maybe_recompute_hf_share_guard(account_id, cfg, ts)
    except Exception as exc:  # noqa: BLE001 —— 防御性兜底
        log.warning("[WPP HF] share-guard pass failed (不影响调阈): %s", exc)

    # 5) v1.10.0 可达性护栏：阈值高于"近期最高分+step" ⇒ 记天花板（judge 读侧压回）+ 告警。
    #    放在 learning.enabled 之前：它保的是"心流还能不能说话"，与自动调阈开关无关。
    try:
        refresh_hf_reachability_caps(account_id, cfg, ts)
    except Exception as exc:  # noqa: BLE001 —— 防御性兜底
        log.warning("[WPP HF] reachability pass failed (不影响调阈): %s", exc)

    # 6) v1.10.0 静默金丝雀：距上次真发言多久 —— 心流停摆时台账/日志都看不出，
    #    只有这个数能一眼看出（从未发过 ⇒ -1，绝不用 0）。
    try:
        last = repo_hf.get_last_sent_at(account_id)
        _metrics().set_hf_last_send_age_sec(-1 if last is None else max(0, ts - int(last)))
    except Exception:  # noqa: S110, BLE001 —— 观测失败不影响任何功能
        pass

    # 7) v1.9.0 重复闸历史裁剪（防长跑进程里内存无限增长；零 IO）
    dedupe.prune_recent_replies(ts, dedupe.resolve_dedupe_cfg(cfg))

    if not L["enabled"]:
        return

    # 8) 自适应调阈
    try:
        # 反事实基线：**每个「群|小时」**本来有多热闹（近 14 天同小时段的入站人类消息数）。
        #   没数据的时段 = 那时段本来没人说话 ⇒ ambientP=0 ⇒ 弱信号/沉默都算有效信息。
        #   ⚠️ B9（2026-10-02 修）：表是**全时段**的，下面按每条样本自己的小时去查 ——
        #   旧写法只留"当前小时"一份值套给全部样本，7 天窗口里夜里那批会被整片判成
        #   "不可采信"（那时段群本来就冷清），阈值只被白天那批推着走。
        ambient = load_ambient_by_group_hour(account_id, ts, L["labelWindowSec"])

        # 近 7 天有已收敛样本的群 → 逐群滚窗统计 → 判定 →（过冷却才）应用
        since_sec = ts - 7 * 86400
        for group_id in repo_hf.list_distinct_closed_groups(account_id, since_sec):
            # 护栏：只对白名单群调阈
            if gf is not None and not gf(group_id):
                continue
            samples = repo_hf.get_closed_recent(account_id, group_id, L["sampleWindow"])
            if not samples:
                continue
            # v1.6.8：只采信"可鉴别"样本 —— 强信号（引用/@/负词）恒采信；弱信号与沉默
            #   仅在**该样本属于的那个时段**本来不热闹时采信。v1.6.8 之前的行
            #   engage_signal 为 NULL ⇒ 全部排除（= 旧错误标签作废；上线后要重新攒够
            #   minSample 条才会再调阈）。
            #   ``sent_at`` 缺/脏 ⇒ 不采信（与 ``observe.hf_engagement_summary`` 同一条
            #   守卫：查不到它属于哪个时段，就没法判断这条弱信号算不算信息）。
            # v1.16.2 silence-exempt：高置信回复被无视 ≠ 不欢迎 —— 豁免的沉默样本不进统计
            #   （既不计负也不计正）；negative/veto 永不豁免，擦边回复被无视仍计负 ——
            #   9-26 治刷屏的收敛力不动。
            usable: list[Mapping[str, Any]] = []
            ambient_sum = 0.0
            exempted = 0
            for s in samples:
                at = s.get("sent_at")
                if isinstance(at, bool) or not isinstance(at, (int, float)) or at <= 0:
                    continue
                ambient_p = ambient.get(
                    f"{group_id}|{budget.hf_local_hour(int(at))}", 0.0)
                signal = label.as_hf_engage_signal(s.get("engage_signal"))
                if not label.is_hf_sample_informative(signal, ambient_p, L["ambientMax"]):
                    continue
                if L["silenceExemptMarginMin"] > 0 and label.is_hf_silence_exempted(
                        signal, s.get("engaged"), s.get("judge_overall"),
                        s.get("effective_threshold"), L["silenceExemptMarginMin"]):
                    exempted += 1
                    continue
                usable.append(s)
                ambient_sum += ambient_p
            if not usable:
                # （Py 侧没有 TS 那份「群级当前小时本底」单值 —— B9 起按样本自己那一小时取，
                #   所以这里不报 ambientP：报 0 会假装知道一个并不存在的数。）
                log.debug("[WPP HF] no informative sample: group=%s window=%d exempt=%d",
                          group_id, len(samples), exempted)
                continue
            # 审计里报的是**可采信样本的本底均值**（与 ``/heartflow report`` 的
            # ``HfEngagementSummary.ambient_p`` 同口径：那份也是逐样本累加后取均值）。
            ambient_p = ambient_sum / len(usable)
            engaged = sum(1 for s in usable if s.get("engaged"))
            st = repo_hf.get_group_state(account_id, group_id)
            base_threshold = resolve_reply_threshold(cfg)
            res = eval_hf_threshold(
                {"total": len(usable), "engaged": engaged},
                None if st is None else st.get("learned_threshold"),
                base_threshold,
                {
                    "minSample": L["minSample"],
                    "lowEngageRate": L["lowEngageRate"],
                    "highEngageRate": L["highEngageRate"],
                    "step": L["step"],
                    "bandMin": L["bandMin"],
                    "bandMax": L["bandMax"],
                },
            )
            if not res.changed or res.new_threshold is None:
                continue
            # 护栏：变更冷却
            if not hf_cooldown_ok(None if st is None else st.get("last_change_at"), ts,
                                  L["minChangeCooldownSec"]):
                continue
            learned_prev = None if st is None else st.get("learned_threshold")
            old_effective = base_threshold if learned_prev is None else float(learned_prev)
            detail = (f"{res.reason} skipped={len(samples) - len(usable) - exempted} "
                      f"exempt={exempted} ambientP={_fixed(ambient_p, 3)} "
                      f"sig={signal_histogram(usable)}")
            apply_hf_threshold_change(account_id, group_id, old_effective, res.new_threshold,
                                      detail, len(usable), engaged, ts)
            log.info("[WPP HF] threshold adapted: group=%s %.2f → %.2f (%s, %s)",
                     group_id, old_effective, res.new_threshold, res.direction, detail)
    except Exception as exc:  # noqa: BLE001 —— 调阈失败不该影响其它 pass
        log.warning("[WPP HF] sweep adapt failed: %s", exc)


def _cfg_for(adapter: Any, account_id: str) -> Mapping[str, Any]:
    """该账号的心流配置：优先适配器持有的 Account，回落 ``accounts.resolve``。

    两处都取不到 ⇒ ``{}``（= 全缺省 + ``enabled=False``）。**不抛**：sweep 每 300s
    跑一次，"配置一时读不出来"的表现应该是"这一轮什么都不做"，不是每 5 分钟一条
    异常栈。
    """
    acct = None
    try:
        if adapter is not None:
            acct = adapter.account(account_id)
    except Exception:  # noqa: BLE001 —— 见 docstring
        acct = None
    if acct is None:
        try:
            from ...core import accounts as accounts_mod
            acct = accounts_mod.resolve(account_id)
        except Exception:  # noqa: BLE001
            return {}
    raw = getattr(acct, "heartflow", None)
    return raw if isinstance(raw, Mapping) else {}


def load_ambient_by_group_hour(account_id: str, now_sec: int,
                               label_window_sec: int) -> dict[str, float]:
    """**每个 ``群|小时``** 的本底接话概率（近 14 天同小时段入站人类消息数 → 泊松近似）。

    键 = ``f"{group_id}|{hour}"``，**不筛当前小时**（B9，2026-10-02 修）。调用方用
    **每条样本自己那一小时**去查表 —— 样本横跨 7 天，白天/夜里都可能有，拿"当前小时"
    一份值去过滤全部样本，会把夜里那批（那时群本来就冷清 ⇒ ``ambientP`` 高）整片判成
    "不可采信"，于是样本 n 永远对不上，而阈值只被白天那批推着走。这正是 v1.6.8 踩过的
    坑（单一 ambient 口径）—— ``observe.hf_engagement_summary`` 早已按"逐样本查表"
    实现，这里是与它**同源**的取数入口（两处口径必须一致，否则同一批样本在报表里
    采信、在调阈这里不采信）。

    一次聚合查询覆盖所有群，不按群逐个查（sweep 每 5 分钟一次，别把 DB 打热）。

    v1.8.0：取数收口到 ``layer.load_hf_group_hour_counts``（与分层统计共用同一条查询 +
    120s 缓存），公式用同一个 ``ambient_p_from_hour_counts`` —— 两处若各写一份，同一条
    样本会在调阈侧"可采信"、在分层侧"不可采信"，``/heartflow report`` 的 n 与这里的
    样本数就永远对不上。

    取不到基线 ⇒ 返回空表（所有弱信号/沉默都按 ``ambientP=0`` 采信，退回旧行为）。
    明知这会让阈值更容易漂，故留 warn：基线查询长期失败必须看得见。
    """
    out: dict[str, float] = {}
    try:
        all_p = layer.ambient_p_from_hour_counts(
            layer.load_hf_group_hour_counts(account_id, now_sec), label_window_sec)
        for k, p in all_p.items():
            i = k.rfind("|")
            if i < 0:
                continue                     # 脏键（不含 "|"）进不了任何小时段
            try:
                int(k[i + 1:])
            except ValueError:
                continue                     # 与 JS ``Number("x") → NaN ≠ hour`` 同义
            out[k] = p
    except Exception as exc:  # noqa: BLE001 —— 降级路径见 docstring
        log.warning("[WPP HF] ambient baseline query failed (弱信号将不过滤): %s", exc)
    return out


def prune_open_windows(account_id: str, now_sec: int) -> None:
    """清掉该账号已过期的开窗内存（sweep 到期关窗后同步；防长跑进程里表无限增长）。"""
    prefix = f"{account_id}:"
    for k in [k for k, w in _open_windows.items()
              if k.startswith(prefix) and w.sent_at_sec + w.observe_window_sec <= now_sec]:
        _open_windows.pop(k, None)


def maybe_recompute_hf_share_guard(account_id: str, cfg: Mapping[str, Any] | None,
                                   now_sec: int) -> int:
    """v1.9.0 占比外环：重算**今日**每群 bot 发言占比 → 落内存（judge 读侧零 DB IO）。

    为什么用"今日"而不是"近 N 天"：外环是**当日**预算的收紧依据 —— 今天的嘴今天管，
    昨天的超标不该锁今天（跨日本地日翻页时读侧 ``get_hf_share_tighten`` 会自然返回
    ``None``，不需要额外的失效逻辑）。

    **只在占比超标时才 warn**：冷清群每天 4 条发言，每轮都打日志会把 journal 刷满
    （每账号每 300s 一次）。返回命中的群数（测试与诊断用）。
    """
    from ...data import repo_messages  # 延迟导入，同上

    G = observe.resolve_hf_share_guard_cfg(cfg)
    if not G.enabled:
        return 0
    rows = repo_messages.list_bot_msg_share(account_id, budget.hf_day_start_sec(now_sec))
    by_group: dict[str, dict[str, int]] = {}
    for r in rows:
        gid = r.get("gid")                   # ⚠️ 数据层这一列叫 gid（COALESCE 出来的别名）
        if not gid:
            continue                         # 归群失败的脏行（peer_id 也空）直接丢
        cur = by_group.setdefault(str(gid), {"inbound": 0, "outbound": 0})
        if r.get("direction") == "outbound":
            cur["outbound"] += int(r.get("n") or 0)
        else:
            cur["inbound"] += int(r.get("n") or 0)
    hits = 0
    for gid, v in by_group.items():
        share = observe.hf_bot_share(v["inbound"], v["outbound"])
        total = v["inbound"] + v["outbound"]
        observe.note_hf_group_share(
            account_id, gid, now_sec,
            observe.HfGroupVolume(total=total, bot_sends=v["outbound"]), share)
        if share > G.target_share and total >= G.min_msgs and v["outbound"] >= G.min_bot_sends:
            hits += 1
            log.warning("[WPP HF] share-guard: group=%s bot 发言占比 %s%% (%d/%d) > %s%% ⇒ 今日预算收紧",
                        gid, _fixed(share * 100, 1), v["outbound"], total,
                        _fixed(G.target_share * 100, 0))
    return hits


def refresh_hf_reachability_caps(account_id: str, cfg: Mapping[str, Any] | None,
                                 now_sec: int) -> int:
    """v1.10.0 可达性护栏刷新（sweep 侧，每轮一次 DB 聚合）：逐群算"阈值够得着吗"。

    为什么放在 sweep 而不是 judge 热路径：判据需要"该群近期判过的最高分"，是一条聚合
    查询 —— 放进 judge 就是每条群消息一次查询（违反热路径约定）。sweep 每 300s 一次，
    结果落内存，judge 侧只做一次查表（:func:`clamp_hf_threshold_to_reachability`）。

    为什么**无条件跑**（不受 ``learning.enabled`` 影响）：它保护的是**功能可用性**，
    不是学习闭环 —— 老板关掉自动调阈时，阈值仍可能被画像/手工设到够不着的高度。

    返回被压住的群数（测试与指标用）。
    """
    from ...data import repo_hf  # 延迟导入，同上

    L = resolve_hf_learning(cfg)
    if not L["recoverUnreachable"]:
        # 关掉时清空缓存：否则旧的压住值会继续生效（开关要真能关）
        _drop_caps(account_id)
        return 0
    rows = repo_hf.list_recent_judged_scores(
        account_id, now_sec - L["reachabilityWindowSec"], HF_REACH_DB_ROW_CAP)
    by_group: dict[str, list[float]] = {}
    for r in rows:
        gid = r.get("group_id")
        if not gid:
            continue
        by_group.setdefault(str(gid), []).append(float(r["judge_overall"]))
    base = resolve_reply_threshold(cfg)
    gf = _allowed_filter(cfg)
    capped = 0
    reachable = 0
    max_eff = 0.0
    for group_id, scores in by_group.items():
        if gf is not None and not gf(group_id):
            continue
        # 生效阈值必须与 judge 路径算得**一模一样**（学习值 → 读侧钳制 → 画像 band 有界抬升），
        #   否则护栏量错了对象：拿账号级 0.6 去比 0.85 的生效值，就永远发现不了自锁。
        override = resolve_threshold_override(account_id, group_id, cfg, now_sec)
        anchor = base if override is None else override
        eff = profile.resolve_hf_profile_band_effect_for(account_id, group_id, anchor,
                                                        cfg)["threshold"]
        max_eff = max(max_eff, eff)
        r = hf_reachability_cap(scores, eff, {
            "minSample": L["reachabilityMinSample"],
            "step": L["step"],
            "bandMin": L["bandMin"],
        })
        k = _key(account_id, group_id)
        prev = _reach_caps.get(k)
        if r is None:
            if prev is not None:
                _reach_caps.pop(k, None)
            if len(scores) >= L["reachabilityMinSample"]:
                reachable += 1
            continue
        capped += 1
        _reach_caps[k] = HfReachCap(cap=r["cap"], max_score=r["maxScore"], n=r["n"],
                                    at_sec=now_sec)
        # 告警：**只在这一格首次出现 / 压住值变化 / 每小时一次** 时打 —— sweep 每 300s
        #   一轮，不做节流会把 journal 刷满，反而让这条真正重要的告警被淹没。
        if prev is None or prev.cap != r["cap"] or now_sec - prev.at_sec >= 3600:
            _metrics().inc_hf_reach_cap_applied()
            log.warning(
                "[WPP HF] threshold unreachable ⇒ capped: account=%s group=%s eff=%s 但近 %d 天 "
                "%d 条判定最高只 %s ⇒ 生效阈值压到 %s（心流此前等于停摆; 检查画像 band / 学习值 / 手工阈值）",
                account_id, group_id, _fixed(eff, 2),
                int(round(L["reachabilityWindowSec"] / 86400)), r["n"],
                _fixed(r["maxScore"], 2), _fixed(r["cap"], 2))
    m = _metrics()
    m.set_hf_effective_threshold_max(max_eff)
    m.set_hf_threshold_ceiling_groups(capped)
    m.set_hf_reachable_groups(reachable)
    return capped


def _drop_caps(account_id: str) -> None:
    prefix = f"{account_id}:"
    for k in [k for k in _reach_caps if k.startswith(prefix)]:
        _reach_caps.pop(k, None)


def signal_histogram(samples: Sequence[Mapping[str, Any]]) -> str:
    """样本信号分布 ``quote:2,short-window:5``（审计 reason 用）。

    有界：信号种类固定 6 种，样本 ≤ ``sampleWindow``。顺序按**首次出现**（对齐 JS ``Map``）。
    """
    m: dict[str, int] = {}
    for s in samples:
        k = s.get("engage_signal") or "unknown"
        m[str(k)] = m.get(str(k), 0) + 1
    return ",".join(f"{k}:{n}" for k, n in m.items())


def apply_hf_threshold_change(
    account_id: str,
    group_id: str,
    old_threshold: float,
    new_threshold: float,
    reason: str,
    sample_total: int,
    sample_engaged: int,
    now_sec: int,
) -> None:
    """落一次阈值变更：群状态 + 审计行 + **内存缓存同步**（三者必须一起，缺一则读侧读到旧值）。"""
    from ...data import repo_hf  # 延迟导入，同上

    repo_hf.upsert_group_state(
        account_id, group_id, learned_threshold=new_threshold, last_change_at=now_sec,
        last_change_old=old_threshold, last_change_new=new_threshold,
        last_change_reason=reason)
    repo_hf.log_threshold_change(
        account_id, group_id, new_threshold, sample_total, sample_engaged,
        old_threshold=old_threshold, reason=reason)
    _learned_thresholds[_key(account_id, group_id)] = new_threshold


#: 供测试与诊断：当前内存里学了几个群（不导出内部表本身）
def learned_threshold_count() -> int:
    """测试 / 可观测：当前内存里有 learned 阈值的群数。"""
    return len(_learned_thresholds)


def open_window_count() -> int:
    """测试 / 可观测：当前登记的开窗数。"""
    return len(_open_windows)


def reach_cap_count() -> int:
    """测试 / 可观测：当前缓存的天花板数。"""
    return len(_reach_caps)


__all__ = [
    "HF_LEARNING_DEFAULTS",
    "HF_PLACEHOLDER_MSG_IDS",
    "HF_REACH_DB_ROW_CAP",
    "HF_REPLY_THRESHOLD_FALLBACK",
    "HF_SEND_OUTCOME_PENDING",
    "HF_SEND_OUTCOME_SENT",
    "HF_SEND_OUTCOME_SUPPRESSED",
    "HfEffectiveThreshold",
    "HfLayerStep",
    "HfLedgerRecord",
    "HfOpenWindow",
    "HfReachCap",
    "HfReachInfo",
    "HfSendResult",
    "HfShadow",
    "HfThresholdDecision",
    "HfThresholdEval",
    "apply_hf_threshold_change",
    "clamp_hf_threshold_to_band",
    "clamp_hf_threshold_to_reachability",
    "classify_hf_send",
    "eval_hf_threshold",
    "forget_hf_open_window",
    "get_hf_reachability_cap",
    "get_learned_threshold",
    "get_open_hf_window",
    "hf_cooldown_ok",
    "hf_layer_anchor_for",
    "hf_layer_step",
    "hf_reachability_cap",
    "learned_threshold_count",
    "load_ambient_by_group_hour",
    "load_hf_budget_seed",
    "load_learned_thresholds",
    "mark_hf_group_engaged",
    "maybe_recompute_hf_share_guard",
    "open_window_count",
    "persist_hf_judged",
    "persist_hf_judged_below_threshold",
    "persist_hf_send_outcome",
    "prune_open_windows",
    "reach_cap_count",
    "refresh_hf_reachability_caps",
    "reset_learned_threshold_cache",
    "resolve_hf_effective_threshold",
    "resolve_hf_learning",
    "resolve_reply_threshold",
    "resolve_threshold_override",
    "round2",
    "run_sweep",
    "signal_histogram",
]
