"""心流观测复盘 + 占比外环（P3）—— ``heartflow-observe.ts`` (v1.9.0) 的移植。

## 为什么要有它

老板问过「bot 到底说多了没有」，而此前**没有任何观测面**能回答 —— 调阈 / 预算 / 画像
三套机制都在**改行为**，却没有人看**结果**。本模块补上"看结果"，并把看的结果接回一个
**有界**的控制环：**该群 bot 发言占比 > 目标值 ⇒ 收紧当日预算**（老板 2026-09-26 拍板）。

## 与调阈的关系（关键，别混）

v1.6.6 的教训是「拿被自学的标量去控频率」必然漂到边界，见 :mod:`.budget` 的模块
docstring。本模块的外环**不动阈值**，只动预算 —— 预算是有界参数（间隔 ×2 有 cap，
上限 ÷2 有 floor），收紧了不会自我强化：占比降下来 ⇒ 下一轮就不再收紧；占比还是高
⇒ 也只停在 floor，**不会把 bot 静音**。

## 为什么是"占比"而不是"绝对条数"

冷清群 bot 说 3 条就可能是 30% 占比，活跃群说 30 条才 5%。绝对条数**没法跨群比较**，
占比可以。

## 口径（逐项标注，不标注会得出错误结论）

- **占比 / 重复率**：``wpp_messages.ts`` 口径（消息实际时间），窗口 = 近 N 天。
- **被制止率 / 接话率**：``wpp_hf_ledger.judged_at`` 口径（判断时刻）。
  **两者窗口不同，不可相除**。
- 已知偏差（写死在注释里，报告里也点一句）：
  ① 长回复被分片器切成多条 ⇒ bot 条数被放大；
  ② 分母是"已入库的群消息"，不是群里真实消息量 ⇒ **占比只看趋势，不判绝对超标**；
  ③ 出站行含"老板自己用 iPad 手打的消息"，插件发送与人工发送在 ``wpp_messages`` 里
     无法区分。

## 结构约束

- ⚠️ **绝不 import** :mod:`.learn`（成环）；也不依赖 :mod:`.layer`（职责单一：观测只吃现成数据）。
- 纯函数为主，唯一的内存写在**占比外环**的状态上（:data:`_share`）；**judge 读侧零 DB IO**。
- 本模块登记第 5 处内存缓存（``CACHE_REGISTRY.register("observe", ...)``）。

## 与 TS 的四处**故意**分歧

1. ``toFixed`` 语义（:func:`_fixed`）—— Python 的 ``f"{v:.1f}"`` 在精确平局上走银行家
   舍入（``0.5`` → ``"0"``，JS 是 ``"1"``），报告里全是给人看的百分比/lift，两边不一致
   会把"差分对不上"变成无谓的排查。
2. **脏 ``sent_at`` 一律不算可采信样本**（TS 的 ``s.sent_at ?? 0`` 后面跟着
   ``hfLocalHour("abc")`` 会算出 ``NaN`` 小时键，本底取 0，强信号于是照算）。
   与 :mod:`.layer` 的 ``sent_at`` 处理**同一取向**：宁可不采信，也不要静默 NaN。
3. :class:`HfShareTighten` 的单项**可以**比 base 松（``base.maxPerHour=1`` 时档位是 floor
   ``2``：那是硬边界，见 §7 的用例）。**"只能收紧"由复合点保证** ——
   ``budget.tighten_budget`` 取更严的一侧并内置 I2 断言，**别把断言加在这里**，
   那会把合法档位判成违规。
4. TS 的 ``shares[].share`` 字段在正文生成里**从未被读**（只用 ``inbound``/``outbound``
   现算）⇒ Py 侧不再要求调用方先算一遍（少一个能算错的地方）。
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .budget import (
    REASON_CONSECUTIVE,
    REASON_DAY,
    REASON_GAP,
    REASON_HOUR,
    REASON_QUIET_HOURS,
    BudgetConfig,
    hf_day_bucket_key,
    hf_day_start_sec,
    hf_local_hour,
)
from .dedupe import HfDedupeConfig, HfRecentReply, hf_repeat_verdict, norm_hf_text
from .label import as_hf_engage_signal, is_hf_sample_informative
from .runtime import CACHE_REGISTRY

log = logging.getLogger(__name__)


# ================================================================ 数字格式化


def _is_num(v: Any) -> bool:
    """``Number.isFinite`` 的等价：``bool`` **不算数**（``True`` 会被当 1 混进来）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_one(v: Any) -> bool:
    """``v === 1`` 的严格等价：``True == 1`` 在 Python 里成立，在 JS 里不成立。"""
    return _is_num(v) and v == 1


def _fixed(v: Any, digits: int) -> str:
    """``Number.prototype.toFixed`` 的等价（**不是** Python 的 ``f"{v:.1f}"``）。

    JS 的 toFixed 定义是"取与 x 最接近的 ``n/10^d``；恰有两个同样近时取**较大**的 n"，
    即对**二进制精确值**做 half-up；Python 的 ``format`` 在精确平局上用银行家舍入
    （``f"{0.5:.0f}"`` 是 ``"0"``，JS 是 ``"1"``）。``Decimal(float)`` 取的正是那个
    二进制精确值，所以这里按 JS 语义走。

    非有限数照 JS 的字符串化返回（``"NaN"`` / ``"Infinity"``）。
    """
    if not _is_num(v):
        if isinstance(v, float) and math.isnan(v):
            return "NaN"
        if isinstance(v, float):
            return "-Infinity" if v < 0 else "Infinity"
        return "NaN"
    try:
        quant = Decimal(1).scaleb(-digits)
        return str(Decimal(v).quantize(quant, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):          # 位数超出 Decimal 上下文精度
        return f"{v:.{digits}f}"


# ================================================================ 纯指标


def hf_bot_share(inbound: Any, outbound: Any) -> float:
    """发言占比（纯函数）：bot 条数 / 群消息总数。

    用**有界**分母（人类 + bot）而不是 bot/人类：后者在"群里没人说话但 bot 说了 3 条"
    时趋于无穷，会让外环对着一个无意义的数做决策。两种口径的差别在分母：同一个分子在
    bot/人类口径下**必然更大**（分母少了 bot 自己）。报告里一律**同时显示原始条数**
    （``12.5% (30/240)``），避免口径本身成为误读来源。
    """
    total = inbound + outbound
    if total <= 0:
        return 0.0
    return outbound / total


def fmt_hf_share(inbound: Any, outbound: Any) -> str:
    """占比的可读格式（报告与日志共用）：``12.5% (30/240)``。"""
    return f"{_fixed(hf_bot_share(inbound, outbound) * 100, 1)}% ({outbound}/{inbound + outbound})"


def hf_engagement_lift(rate: Any, ambient_p: Any) -> float | None:
    """接话提升倍数（纯函数）：实测接话率 / 本底接话率。本底 <0.01 ⇒ ``None``（不除零，也不吹成 100×）。"""
    if not _is_num(rate) or not _is_num(ambient_p):
        return None
    if ambient_p < 0.01:
        return None
    return rate / ambient_p


def hf_veto_rate(veto: Any, total: Any) -> float | None:
    """被制止率（纯函数）：(负词 + veto) / 已收敛样本。无样本 ⇒ ``None``（不是 0）。"""
    if not _is_num(total) or total <= 0:
        return None
    return veto / total


@dataclass(frozen=True)
class HfRepeatItem:
    """重复率素材（每群一条；由数据层 ``list_outbound_texts`` 提供）。"""

    group_id: str
    text: str
    at_sec: int


@dataclass(frozen=True)
class HfRepeatStat:
    """重复率结果。"""

    total: int
    repeats: int
    rate: float
    prefix: int
    similar: int


def hf_repeat_rate(items: Sequence[HfRepeatItem], cfg: HfDedupeConfig) -> HfRepeatStat:
    """重复率（纯函数，**与闸共用** :func:`dedupe.hf_repeat_verdict`）。

    按群分组、按时刻升序逐条比对"它之前同群窗口内的发言"，命中即计一次重复。
    与闸**严格同源** ⇒ 日报报出的每一个重复百分点，都等于"这个百分点换到今天会被闸拦" ——
    否则观测驱动的收口是假的（INVARIANT: I4）。

    裁剪历史与闸一致（``history_max``）：只差"闸只看当下内存，日报看 DB 素材"。若两侧
    裁剪策略不同，日报会把闸永远不会看到的老句子算成重复，**数字虚高**。

    ⚠️ ``at_sec`` 必须是数（``None``/字符串会当场 ``TypeError``，**不静默**）——
    TS 那边 ``a.atSec - b.atSec`` 遇到 ``undefined`` 得到 ``NaN``，排序结果是不确定的，
    这里选择"吵"而不是"猜"。
    """
    ordered = sorted(items, key=lambda it: it.at_sec)
    hist: dict[str, list[HfRecentReply]] = {}
    repeats = 0
    prefix = 0
    similar = 0
    for it in ordered:
        norm = norm_hf_text(it.text)
        if len(norm) < cfg.min_chars:
            continue                                   # 短句豁免（与闸同一门槛）
        h = hist.get(it.group_id) or []
        v = hf_repeat_verdict(it.text, h, cfg, it.at_sec)
        if v:
            repeats += 1
            if v.kind == "prefix":
                prefix += 1
            else:
                similar += 1
        h = h + [HfRecentReply(text=it.text, at_sec=it.at_sec, norm=norm)]
        while len(h) > cfg.history_max:
            h = h[1:]
        hist[it.group_id] = h
    total = len(ordered)
    return HfRepeatStat(
        total=total,
        repeats=repeats,
        rate=(repeats / total) if total > 0 else 0.0,
        prefix=prefix,
        similar=similar,
    )


@dataclass(frozen=True)
class HfEngagementSummary:
    """接话率摘要。"""

    usable: int
    skipped: int
    engaged: int
    #: 可采信样本的接话率（无可采信样本 ⇒ ``None``）
    rate: float | None
    #: 可采信样本的本底均值（lift 的分母；无 ⇒ 0）
    ambient_p: float


def hf_engagement_summary(
    samples: Sequence[Mapping[str, Any]],
    ambient_by_group_hour: Mapping[str, float],
    ambient_max: float,
) -> HfEngagementSummary:
    """从已收敛样本算接话率（纯函数）。

    为什么必须逐样本用它**自己那一小时**的本底：样本可能落在不同小时段（7 天窗口里
    白天/夜里的都有），拿"当前小时"的本底去过滤全部样本，会把夜里那批全判成"不可采信"
    —— 这正是 v1.6.8 踩过的坑（标签锚错 + 单一 ambient 口径），见 :mod:`.label`。

    行形状 = ``repo_hf.list_closed_since`` 的列（``group_id`` / ``engaged`` /
    ``engage_signal`` / ``sent_at``）—— 测试里有一条断言把两边钉在一起。
    """
    usable = 0
    engaged = 0
    skipped = 0
    ambient_sum = 0.0
    for s in samples:
        at = s.get("sent_at")
        if not _is_num(at) or at <= 0:
            skipped += 1                               # 脏/缺时刻 ⇒ 不采信（见模块分歧 2）
            continue
        gid = str(s.get("group_id") or "")
        p = ambient_by_group_hour.get(f"{gid}|{hf_local_hour(int(at))}") or 0
        if is_hf_sample_informative(as_hf_engage_signal(s.get("engage_signal")), p, ambient_max):
            usable += 1
            ambient_sum += p
            if _is_one(s.get("engaged")):
                engaged += 1
        else:
            skipped += 1
    return HfEngagementSummary(
        usable=usable,
        skipped=skipped,
        engaged=engaged,
        rate=(engaged / usable) if usable > 0 else None,
        ambient_p=(ambient_sum / usable) if usable > 0 else 0.0,
    )


# ============================================== 占比外环（老板拍板：>5% ⇒ 收紧当日预算）


#: 默认档（挂 ``HeartflowConfig.shareGuard``；不进 UI schema）。
#:
#: ``minMsgs`` / ``minBotSends`` 是**防噪声闸**：早上 6 条消息里 bot 说了 1 条 = 16.7% 占比，
#: 样本太小，据此收紧会让新群/冷清群开局就被上锁。样本够了才有资格谈占比。
HF_SHARE_GUARD_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    #: 目标占比（默认 0.05 = 5%）；**严格大于**才收紧（恰好 5% 不动）
    "targetShare": 0.05,
    #: 当日该群消息总数下限
    "minMsgs": 40,
    #: 当日该群 bot 发言数下限
    "minBotSends": 5,
}

#: 收紧档硬边界：间隔 ≤15 分钟 / 每小时 ≥2 条 / 每天 ≥10 条
#: （外环只能收紧，不能把 bot 锁死）。
HF_SHARE_MIN_GAP_CAP_SEC = 900
HF_SHARE_MAX_PER_HOUR_FLOOR = 2
HF_SHARE_MAX_PER_DAY_FLOOR = 10


@dataclass(frozen=True)
class HfShareGuardConfig:
    """占比外环参数（已解析、已钳制）。"""

    enabled: bool
    target_share: float
    min_msgs: int
    min_bot_sends: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "targetShare": self.target_share,
            "minMsgs": self.min_msgs,
            "minBotSends": self.min_bot_sends,
        }


@dataclass(frozen=True)
class HfGroupVolume:
    """该群当日音量（占比的分母与分子）。"""

    total: int
    bot_sends: int


@dataclass(frozen=True)
class HfShareTighten:
    """外环给出的**收紧档**（TS 的 ``HfBudgetConfig`` 结构体）。

    不是算好的预算 —— 交给既有的 :func:`budget.tighten_budget` 复合（``.as_dict()``
    直接就是它要的 ``tighter`` 映射），与画像收紧走**同一条**"只能更紧"的合成路径
    （复合安全：两次收紧取更严的一侧）。

    ⚠️ 单个字段**可能**比 base 松（如 ``max_per_hour`` 的 floor），见模块 docstring 分歧 3。
    """

    min_gap_sec: int
    max_per_hour: int
    max_per_day: int
    no_consecutive_without_human: bool
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        """给 :func:`budget.tighten_budget` 的 ``tighter`` 映射（键名对齐 TS）。"""
        return {
            "enabled": self.enabled,
            "minGapSec": self.min_gap_sec,
            "maxPerHour": self.max_per_hour,
            "maxPerDay": self.max_per_day,
            "noConsecutiveWithoutHuman": self.no_consecutive_without_human,
        }


def _pos_int(v: Any, fallback: int) -> int:
    """``v != null && Number.isFinite(v) && v >= 0 ? Math.floor(v) : fallback``。

    ``bool`` 在 JS 里 ``Number.isFinite(true)`` 是 ``false`` ⇒ 走 fallback；Py 侧同样拒绝。
    """
    if not _is_num(v) or v < 0:
        return fallback
    return int(math.floor(v))


def resolve_hf_share_guard_cfg(cfg: Mapping[str, Any] | None) -> HfShareGuardConfig:
    """合并 ``cfg["shareGuard"]`` 与 :data:`HF_SHARE_GUARD_DEFAULTS`。

    形状脏（整段是字符串/列表）⇒ **全部回落缺省**，不抛错：TS 那边
    ``"nonsense"?.enabled`` 取到的是 ``undefined``（字符串上取属性不报错），Py 侧若直接
    ``.get`` 会在**入站消息的判定路径**上炸掉 —— 那是比"用默认值"严重得多的失效形态。
    """
    root = cfg if isinstance(cfg, Mapping) else {}
    raw = root.get("shareGuard")
    g = raw if isinstance(raw, Mapping) else {}
    D = HF_SHARE_GUARD_DEFAULTS
    ts = g.get("targetShare")
    target = ts if (_is_num(ts) and ts > 0 and ts <= 1) else D["targetShare"]
    return HfShareGuardConfig(
        enabled=bool(g.get("enabled", D["enabled"])),
        target_share=target,
        min_msgs=_pos_int(g.get("minMsgs"), D["minMsgs"]),
        min_bot_sends=_pos_int(g.get("minBotSends"), D["minBotSends"]),
    )


def hf_share_tighten(
    base: BudgetConfig,
    share: Any,
    volume: HfGroupVolume,
    cfg: HfShareGuardConfig,
) -> HfShareTighten | None:
    """外环判定（纯函数）：该群当日占比 > 目标 **且**样本够 ⇒ 返回收紧档；否则 ``None``。

    ``base.max_per_hour <= 0`` 表示"不限"：此时收紧档直接用 floor（否则 ``0÷2=0`` 会被
    :func:`budget.tighten_budget` 当成"未指定"而回落成原值，**外环静默失效** ——
    不限额的账号恰恰最需要这个外环）。
    """
    if not cfg.enabled:
        return None
    # 恰好等于目标 ⇒ 不收紧（边界有单测钉住）
    if not _is_num(share) or share <= cfg.target_share:
        return None
    if volume.total < cfg.min_msgs or volume.bot_sends < cfg.min_bot_sends:
        return None

    def _half_or_floor(v: int, floor: int) -> int:
        return max(int(math.floor(v / 2)), floor) if v > 0 else floor

    return HfShareTighten(
        min_gap_sec=min(base.min_gap_sec * 2, HF_SHARE_MIN_GAP_CAP_SEC),
        max_per_hour=_half_or_floor(base.max_per_hour, HF_SHARE_MAX_PER_HOUR_FLOOR),
        max_per_day=_half_or_floor(base.max_per_day, HF_SHARE_MAX_PER_DAY_FLOOR),
        no_consecutive_without_human=base.no_consecutive_without_human,
    )


@dataclass(frozen=True)
class HfShareState:
    """外环状态（内存；由 sweep 的 DB 聚合填充，judge 读侧零 DB IO）。"""

    day_key: str
    share: float
    total: int
    bot_sends: int


# ------------------------------------------------ 内存状态（judge 读侧的唯一依赖）


#: ``"{account_id}:{group_id}"`` → 状态
_share: dict[str, HfShareState] = {}
_lock = threading.RLock()


def _share_key(account_id: str, group_id: str) -> str:
    return f"{account_id}:{group_id}"


def note_hf_group_share(
    account_id: str,
    group_id: str,
    now_sec: int,
    volume: HfGroupVolume,
    share: float,
) -> None:
    """sweep 侧写入（每轮重算当日占比；随本地日翻页自然失效 —— 读侧比对 ``day_key``）。"""
    with _lock:
        _share[_share_key(account_id, group_id)] = HfShareState(
            day_key=hf_day_bucket_key(now_sec),
            share=share,
            total=volume.total,
            bot_sends=volume.bot_sends,
        )


def get_hf_share_tighten(
    account_id: str,
    group_id: str,
    base: BudgetConfig,
    hf_cfg: Mapping[str, Any] | None,
    now_sec: int,
) -> HfShareTighten | None:
    """judge 读侧（**零 DB IO**）：该群当前是否命中占比外环 ⇒ 返回收紧档。

    跨日（``state.day_key`` ≠ 今日）视为无数据 ⇒ ``None``
    （新的一天从零开始，不带着昨天的结论锁今天的嘴）。

    ⚠️ 判定体内不得出现**导入语句 / SQL / 任何 IO**（见 ``test_observe.py`` 的源级守卫）。
    """
    with _lock:
        st = _share.get(_share_key(account_id, group_id))
    if st is None or st.day_key != hf_day_bucket_key(now_sec):
        return None
    return hf_share_tighten(
        base, st.share, HfGroupVolume(total=st.total, bot_sends=st.bot_sends),
        resolve_hf_share_guard_cfg(hf_cfg),
    )


@dataclass(frozen=True)
class HfShareGuardHit:
    """报告用快照：命中外环的群 + 收紧前后（供 ``/heartflow report`` 显示"已收紧"）。"""

    group_id: str
    share: float
    total: int
    bot_sends: int
    #: TS 的 ``from``（``from`` 是 Py 关键字）
    from_: BudgetConfig
    to: HfShareTighten


def hf_share_guard_snapshot(
    account_id: str,
    base: BudgetConfig,
    hf_cfg: Mapping[str, Any] | None,
    now_sec: int,
) -> list[HfShareGuardHit]:
    out: list[HfShareGuardHit] = []
    prefix = f"{account_id}:"
    g = resolve_hf_share_guard_cfg(hf_cfg)
    day_key = hf_day_bucket_key(now_sec)
    with _lock:
        items = list(_share.items())
    for k, st in items:
        if not k.startswith(prefix):
            continue
        if st.day_key != day_key:
            continue
        to = hf_share_tighten(
            base, st.share, HfGroupVolume(total=st.total, bot_sends=st.bot_sends), g)
        if to is None:
            continue
        out.append(HfShareGuardHit(
            group_id=k[len(prefix):], share=st.share, total=st.total,
            bot_sends=st.bot_sends, from_=base, to=to,
        ))
    return sorted(out, key=lambda h: -h.share)


def reset_hf_share_guard() -> None:
    """测试/诊断：清空外环状态。"""
    with _lock:
        _share.clear()


def hf_share_guard_window_start(now_sec: int) -> int:
    """今日起点（报告与 sweep 共用的窗口边界；只是 :func:`budget.hf_day_start_sec` 的一层语义化包装）。"""
    return hf_day_start_sec(now_sec)


# ============================================== 复盘文本（``/heartflow report`` 的正文）


#: 硬上限：单条 filehelper 消息的阅读上限（超了老板也懒得看，且 vendor 侧消息过长体验差）
HF_DIGEST_MAX_CHARS = 3500
#: 报告里最多列几个群（其余折叠）
HF_DIGEST_MAX_GROUPS = 8
#: 单节字符预算（逐节限流，再叠加全局硬 cap）
_SECTION_CAP = 700


@dataclass(frozen=True)
class HfStoppedStat:
    """被制止素材（``judged_at`` 口径）。"""

    negative: int
    veto: int
    total: int


@dataclass(frozen=True)
class HfDigestShare:
    """报告里的每群占比素材。"""

    group_id: str
    inbound: int
    outbound: int


@dataclass(frozen=True)
class HfDigestLayer:
    """报告里的分层条目（影子/生效）。"""

    group_id: str
    layer_key: str
    n: int
    engaged: int = 0
    rate: float | None = None
    suggestion: float | None = None
    applied: bool = False


@dataclass(frozen=True)
class HfDigestInput:
    """复盘正文的全部素材。

    ``shares`` 由调用方按占比降序排好；``stopped`` / ``budget`` / ``share_guard``
    的口径分别见模块 docstring。
    """

    now_sec: int
    days: int
    shares: Sequence[HfDigestShare]
    engagement: HfEngagementSummary
    stopped: HfStoppedStat
    repeat: HfRepeatStat
    #: 全部重复样本条数（报告里区分"素材总量"与"重复数"）
    repeat_sample_total: int
    layers: Sequence[HfDigestLayer]
    #: 预算拦截（进程内累计计数，见 ``budget.hf_budget_blocked_snapshot``）
    budget: Mapping[str, int]
    share_guard: Sequence[HfShareGuardHit]


#: 预算拦截小节的展示顺序（与 :mod:`.budget` 的 ``REASON_*`` 常量同源，别另抄一份字面量）
_BUDGET_REASONS = (REASON_GAP, REASON_HOUR, REASON_DAY, REASON_CONSECUTIVE, REASON_QUIET_HOURS)


class _Sec:
    """单节字符预算的累加器（TS 里是个 ``{n: number}`` 字面量）。"""

    __slots__ = ("n",)

    def __init__(self) -> None:
        self.n = 0


def _push_line(lines: list[str], line: str, used: _Sec) -> None:
    """逐节累加，超节预算就停（**不抛错**，报告宁可少一节也不能发不出去）。"""
    if used.n + len(line) > _SECTION_CAP:
        return
    used.n += len(line)
    lines.append(line)


def _fmt_date(now_sec: int) -> str:
    d = datetime.fromtimestamp(now_sec)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def build_hf_digest(inp: HfDigestInput) -> str:
    """复盘正文（纯函数，硬 cap :data:`HF_DIGEST_MAX_CHARS` 字符）。

    版式固定 7 节，每节**自带口径标注**（占比/重复率 = ts 口径；被制止率 = judged_at 口径）
    —— 不标注就会有人把两节的数字相除，得出错误结论。

    超限策略：逐节限流 → 仍超全局 cap ⇒ 从尾部丢行并加 ``…(已截断)``
    （宁可少信息，不可发不出去）。

    ⚠️ ``len()`` 在这里是**码点**数，JS 的 ``.length`` 是 **UTF-16 码元**数；本正文全是
    BMP 字符（中英文 + ``…``），两者相等。若将来往报告里放 emoji，这条就不成立了 ——
    那时要改成 ``len(s.encode("utf-16-le")) // 2``。

    ⚠️ 全局 cap 是**第二道**防线：逐节 ``_SECTION_CAP``（三个可长大的节 × 700）已经把总长
    在实际输入上钉在 ~2500 以内，够不到 3500（TS 同样如此 ⇒ 那条分支在 TS 里也是死代码）。
    留着它是为了"将来加节"时不至于发不出去；测试用 monkeypatch 把这个 cap 调小，
    才**真的**走到截断分支（截断循环读的是模块级常量，patch 立即生效）。
    已知继承自 TS 的边角：单行就超过节预算时，该节会退化出 ``(窗口内无群消息)`` 这种
    占位文案 —— 要触发它得有一个 700 字符长的群 ID，现实中到不了。
    """
    lines: list[str] = []
    lines.append(f"心流复盘 (近 {inp.days} 天)  {_fmt_date(inp.now_sec)}")

    # 【发言占比】
    lines.append(
        f"【发言占比】目标 ≤{_fixed(HF_SHARE_GUARD_DEFAULTS['targetShare'] * 100, 0)}%"
        " · ts 口径 · 分母=已入库群消息")
    sec = _Sec()
    shown = list(inp.shares[:HF_DIGEST_MAX_GROUPS])
    sec_lines: list[str] = []
    for s in shown:
        _push_line(sec_lines, f"  {s.group_id} {fmt_hf_share(s.inbound, s.outbound)}", sec)
    if len(inp.shares) > len(shown):
        _push_line(sec_lines, f"  …其余 {len(inp.shares) - len(shown)} 群", sec)
    if not sec_lines:
        sec_lines.append("  (窗口内无群消息)")
    lines.extend(sec_lines)

    # 【接话率】
    eng = inp.engagement
    rate = eng.rate
    lift = None if rate is None else hf_engagement_lift(rate, eng.ambient_p)
    lines.append(
        f"【接话率】可用 {eng.usable} 跳过 {eng.skipped}"
        f" · rate {'-' if rate is None else _fixed(rate, 2)}"
        f" · 本底 {_fixed(eng.ambient_p, 3)}"
        + (" · lift -(本底过冷, 不计算)" if lift is None else f" · lift {_fixed(lift, 1)}×"))

    # 【被制止率】
    vr = hf_veto_rate(inp.stopped.veto, inp.stopped.total)
    lines.append(
        f"【被制止率】负词 {inp.stopped.negative} / veto {inp.stopped.veto}"
        f" / 样本 {inp.stopped.total}"
        + ("" if vr is None else f" · veto 占比 {_fixed(vr * 100, 1)}%")
        + " (judged_at 口径, 与上两节不同窗)")

    # 【重复率】
    r = inp.repeat
    lines.append(
        f"【重复率】{_fixed(r.rate * 100, 0)}% ({r.repeats}/{inp.repeat_sample_total})"
        f" · 前缀 {r.prefix} / 相似 {r.similar} (同群 6h 窗, 与闸同源)")

    # 【分层 群×时段】（影子/生效）
    lines.append("【分层 群×时段】当前时段各群样本与建议:")
    sec = _Sec()
    shown_l = list(inp.layers[:HF_DIGEST_MAX_GROUPS])
    sec_lines = []
    for l in shown_l:
        _push_line(
            sec_lines,
            f"  {l.group_id} [{l.layer_key}] n={l.n}"
            f" rate={'-' if l.rate is None else _fixed(l.rate, 3)}"
            + (" ⇒ 无建议" if l.suggestion is None
               else f" ⇒ 建议 {_fixed(l.suggestion, 2)}"
                    + (" (生效)" if l.applied else " (未生效)")),
            sec)
    if len(inp.layers) > len(shown_l):
        _push_line(sec_lines, f"  …其余 {len(inp.layers) - len(shown_l)} 群", sec)
    if not sec_lines:
        sec_lines.append("  (当前时段无分层样本)")
    lines.extend(sec_lines)

    # 【预算拦截】
    b = inp.budget
    parts = [f"{k.replace('budget-', '')} {b[k]}" for k in _BUDGET_REASONS if (b.get(k) or 0) > 0]
    lines.append(f"【预算拦截】{' / '.join(parts) if parts else '无'} (进程内累计, 重启清零)")

    # 【占比外环】
    if not inp.share_guard:
        lines.append("【占比外环】未触发 (无群超目标占比)")
    else:
        lines.append("【占比外环】占比超标 ⇒ 当日预算收紧 (只能收紧, 有 floor/cap):")
        sec = _Sec()
        sec_lines = []
        for h in list(inp.share_guard[:4]):
            _push_line(
                sec_lines,
                f"  {h.group_id} {_fixed(h.share * 100, 1)}% > 目标 ⇒ 今日预算已收紧"
                f" (间隔 {h.from_.min_gap_sec}→{h.to.min_gap_sec}s,"
                f" {h.from_.max_per_hour}→{h.to.max_per_hour} 条/小时;"
                " 运行时还叠加画像收紧, 只会更严)",
                sec)
        lines.extend(sec_lines)

    text = "\n".join(lines)
    if len(text) <= HF_DIGEST_MAX_CHARS:
        return text
    # 超硬 cap：从尾部整行丢弃（不切半行，免得留下半句话被误读）
    kept: list[str] = []
    n = 0
    tail = "…(已截断)"
    for l in lines:
        if n + len(l) + 1 + len(tail) > HF_DIGEST_MAX_CHARS:
            break
        kept.append(l)
        n += len(l) + 1
    return "\n".join(kept) + "\n" + tail


CACHE_REGISTRY.register("observe", reset_hf_share_guard)
