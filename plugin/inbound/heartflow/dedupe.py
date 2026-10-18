"""心流重复内容闸（P3）—— ``heartflow-dedupe.ts`` (v1.9.0) 的移植。

## 为什么要有它

只读回放真实账本发现：bot 的一部分发言与更早一条**高度雷同**（归一化后前若干字符
完全相同）。这是"bot 不像真人"最刺眼的症状 —— 群友看到第二遍就出戏。

根因不是模型笨，是**没有任何机制记得自己刚说过什么**：judge 只看最近几条群消息
上下文，看不到自己两小时前那句；而心流插话是"应景"的，场景相似 ⇒ 话也相似。

设计（老板 2026-09-26 拍板）：**同群 6 小时内高度相似（≥0.85）就不发**，
只对心流**主动插话**生效。

## 三条硬约束

① 判定与观测**必须共用同一个函数**（:func:`hf_repeat_verdict`）：日报的"重复率"
   与闸的"拦不拦"若各写一套，就会出现"日报说重复率很高、闸却一条都没拦"这种
   自相矛盾 —— 观测驱动收口就成了假的。见 ``# INVARIANT: I4``。
② 只比对**真发出**的文本（:func:`note_recent_reply` 只在 :func:`learn.persist_send_outcome`
   确认为 sent 后调）：被预算/闸拦下的、被 vendor 去重掉的不能进历史，
   否则会拿"没说出口的话"去拦下一句。**另一个写入口是启动预热**
   :func:`load_hf_dedupe_history`（B7）—— 它回填的也是"已经真发出去"的文本。
③ 短句豁免（``minChars=12``）："收到 / 好的 / 👌"这类回复天然高频且无害，
   归一化后不足 12 字不进相似判定（前缀判定用的是同一个阈值）——
   否则 bot 连"好的"都不敢说，比重复更不像人。

## 为什么自带 norm + 字符 3-gram 而不用 ``jargon.tokenize``

``jargon`` 的 ``ngramTokens`` 未导出，且 ``tokenize`` 会过滤标准词表 ⇒ 短句/口语
被滤成空 token，相似度恒 0 而**静默失效**。这里只要"像不像"，不需要分词，
零 jieba 依赖。

⚠️ **绝不** import ``.learn``（它 import 本模块 ⇒ 成环；有源级守卫）。
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .runtime import CACHE_REGISTRY

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ 参数


#: 老板档位：6 小时窗 / 0.85 / 12 字豁免 / 每群记 50 条。
#:
#: 为什么是 **6 小时而不是 24 小时**：群聊话题半天就换一轮，24 小时窗会把
#: "早上问价、晚上又问价"这种正常重访也拦掉（那是**应该**回的）。
#: 6 小时 ≈ 一个活跃时段，窗内重复才是真·复读。
#:
#: 50 条 × 6 小时：心流每天每群只发个位数条 ⇒ 窗内最多几条，50 是给异常情况留的余量。
HF_DEDUPE_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "proactiveOnly": True,
    "windowSec": 21600,          # 6 小时
    "simThreshold": 0.85,
    "minChars": 12,
    "historyMax": 50,
}

#: ``historyMax`` 的硬上限（配置写再大也不许越过 —— 这是内存边界的最后一道）
HF_HISTORY_MAX_CAP = 500

#: 历史条目里保存的原文长度上限（只为日志/回执回显，判定用归一化文本）
HF_HISTORY_TEXT_CAP = 200

#: 启动预热回填的条数上限（B7）。与 ``historyMax``（每群 50 条）是两回事 ——
#: 这是"一次查询最多取回多少条**跨群**历史"，防窗口内出站极多时把启动拖住。
#: 取得比"群数 × 50"宽出一截：宁可多取几条，也别让某个群的历史空着。
HF_DEDUPE_PRELOAD_LIMIT = 500


@dataclass(frozen=True)
class HfDedupeConfig:
    """**已解析**的重复闸参数（缺省已填满）—— 对应 TS 的 ``Required<HfDedupeConfig>``。"""

    enabled: bool
    proactive_only: bool
    window_sec: int
    sim_threshold: float
    min_chars: int
    history_max: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "proactiveOnly": self.proactive_only,
            "windowSec": self.window_sec,
            "simThreshold": self.sim_threshold,
            "minChars": self.min_chars,
            "historyMax": self.history_max,
        }


def _pos(v: Any, fallback: int) -> int:
    """正数取整；非有限数 / ≤0 / 非数 ⇒ ``fallback``（对齐 TS ``pos()``）。"""
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return fallback
    if not math.isfinite(v) or v <= 0:
        return fallback
    return int(math.floor(v))


def resolve_dedupe_cfg(cfg: Mapping[str, Any] | None = None) -> HfDedupeConfig:
    """合并参数（缺省走 :data:`HF_DEDUPE_DEFAULTS`）。

    相似度**单独判**：它必须落在 ``(0, 1]``，越界一律回落默认 —— 别让
    ``simThreshold: 1.5`` 这种笔误把闸变成"永远不拦"（那正好是最坏的失效方向：
    看起来配了闸，实际一条都不拦）。
    """
    d = (cfg or {}).get("dedupe") or {}
    D = HF_DEDUPE_DEFAULTS
    sim_raw = d.get("simThreshold")
    sim = (float(sim_raw) if isinstance(sim_raw, (int, float)) and not isinstance(sim_raw, bool)
           and math.isfinite(sim_raw) and 0 < sim_raw <= 1 else float(D["simThreshold"]))
    return HfDedupeConfig(
        enabled=d.get("enabled") is not False,
        proactive_only=d.get("proactiveOnly") is not False,
        window_sec=_pos(d.get("windowSec"), D["windowSec"]),
        sim_threshold=sim,
        min_chars=_pos(d.get("minChars"), D["minChars"]),
        history_max=min(_pos(d.get("historyMax"), D["historyMax"]), HF_HISTORY_MAX_CAP),
    )


# ------------------------------------------------- 纯文本归一化 + 相似度


#: emoji / 变体选择符 / 零宽连接符 / 箭头 / 制表符号 …… 归一化时一律剥掉。
#:
#: ⚠️ TS 用的是 ``/u`` 正则跑 **UTF-16 码元**；Python 字符串是**码点**，
#: 所以这里把码点区间**逐段写成转义**（语义一致，且少了代理对那一层 ——
#: 见第 3 步 ``splitter`` 的笔记：JS 的"代理对保护"在 Python 里结构性不需要）。
#:
#: ⚠️ **别把这些字符直接打进源码**：这段里有零宽连接符（U+200D）与变体选择符
#: （U+FE00-FE0F），它们**看不见**，编辑一次就可能被吃掉而**无人察觉** ——
#: 症状是 emoji 偶尔没剥干净、相似度算低、闸静默少拦。全部写成 ``\uXXXX``，
#: 并由 ``tools/verify_dedupe_regex.py`` 把码点逐个钉死。
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"    # 表情 / 象形 / 交通 …… 主体区
    "\U0001F1E6-\U0001F1FF"     # 区域指示符（国旗）
    "\u2190-\u21FF"             # 箭头
    "\u2300-\u27BF"             # 制表符号 / dingbats
    "\u2B00-\u2BFF"             # 杂项符号与箭头
    "\uFE00-\uFE0F"             # 变体选择符（VS1-VS16）
    "\u200D"                     # 零宽连接符（组合 emoji 的粘合剂）
    "]")

_AT_RE = re.compile(r"@[^\s@]{1,64}")
#: 全角 ASCII（U+FF01-FF5E）。
#: ⚠️ 同样**别打字面量** —— 全角感叹号与全角波浪线在编辑器里看着像半角，
#: 差一个码点就是"归一化静默失效"（`！` 没转成 `!` ⇒ 没被滤掉 ⇒ 相似度偏低）。
_FULLWIDTH_RE = re.compile("[\uFF01-\uFF5E]")
_IDEOGRAPHIC_SPACE_RE = re.compile("\u3000")


def norm_hf_text(s: str) -> str:
    """归一化（纯函数）：去 emoji / @ 提及 / 全角转半角 / 只留字母数字与汉字 / 小写。

    - **为什么保留数字**："iPhone 15 现在多少钱"与"iPhone 16 现在多少钱"是**不同问题**，
      只差一个数字 —— 滤掉数字会让这两句的相似度飙到极高而被误判成重复。
      保留数字后用 3-gram，差异会体现出来（测试第 6 条钉死）。
    - **为什么滤标点与空白**：同一句话换个标点/换行不该算两句；而老板与群友的
      书写习惯差异极大（换行、全角、emoji）。
    """
    if not s:
        return ""
    s = _EMOJI_RE.sub("", s)
    s = _AT_RE.sub("", s)
    s = _FULLWIDTH_RE.sub(lambda m: chr(ord(m.group()) - 0xFEE0), s)
    s = _IDEOGRAPHIC_SPACE_RE.sub(" ", s)
    # ``\p{L}\p{N}`` 的等价写法：`str.isalnum()` 恰好就是 Lu/Ll/Lt/Lm/Lo ∪ Nd/Nl/No。
    # 不用 ``\w``：它把下划线也算进来，与 TS 不同。
    s = "".join(ch for ch in s if ch.isalnum())
    return s.lower()


def hf_char_grams(s: str, n: int = 3) -> set[str]:
    """字符 n-gram 集合（默认 3）。

    中文短句无空格，3-gram 在"口语近重复"上比分词稳（见模块 docstring）。
    """
    out: set[str] = set()
    if not s:
        return out
    if len(s) < n:
        out.add(s)
        return out
    for i in range(len(s) - n + 1):
        out.add(s[i:i + n])
    return out


def hf_jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> float:
    """Jaccard 相似度（纯函数）：交集/并集；任一为空 ⇒ 0（**不凭空相似**）。"""
    if not a or not b:
        return 0.0
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    inter = sum(1 for g in small if g in big)
    union = len(a) + len(b) - inter
    return 0.0 if union <= 0 else inter / union


@dataclass(frozen=True)
class HfRecentReply:
    """历史发言（``norm`` 可预存；省略则现算 —— DB 回放的素材没有 norm 字段）。"""

    text: str
    at_sec: int
    #: 归一化结果缓存（内存里存的都带，避免每次判定重复归一化）
    norm: str | None = None


@dataclass(frozen=True)
class HfRepeatVerdict:
    """重复判定结果（``kind='prefix'`` 归一化前缀命中；``'similar'`` 3-gram 命中）。"""

    kind: str
    #: 相似度（prefix 记 1）
    sim: float
    #: 命中的那条历史发言时刻（unix 秒）
    prev_at: int
    #: 命中的那条历史发言开头（回执/日志用，让老板确认拦的是哪句）
    prev_head: str


def _prev_is_newer(h: HfRecentReply, best: HfRepeatVerdict) -> bool:
    return h.at_sec > best.prev_at


def hf_repeat_verdict(text: str, history: Sequence[HfRecentReply], cfg: HfDedupeConfig,
                      now_sec: int) -> HfRepeatVerdict | None:
    """重复判定（纯函数，**闸与日报共用** —— INVARIANT: I4）。

    同群（由调用方保证 ``history`` 是同一个群的）+ 窗口内 + 归一化后 ≥ ``min_chars`` ⇒

    ① 归一化**前缀**（前 ``min_chars`` 字）完全相同 ⇒ ``prefix``（更准更便宜，优先级高）
    ② 否则字符 3-gram Jaccard ≥ ``simThreshold`` ⇒ ``similar``

    多条命中时：``prefix`` 优先于 ``similar``；同为 ``similar`` 取相似度最高的那条
    （并列取更近的）。返回 ``None`` = 放行。

    ⚠️ **不要**为了"更准"在这里加别的相似度算法：日报走同一个函数，加了就等于
    观测口径也变了（而那正是 I4 要防的）。
    """
    a = norm_hf_text(text)
    if len(a) < cfg.min_chars:
        return None                                     # 短句豁免
    a_gram = hf_char_grams(a)
    prefix = a[:cfg.min_chars]

    best: HfRepeatVerdict | None = None
    for h in history:
        if now_sec - h.at_sec > cfg.window_sec:
            continue                                    # 窗口外：bot 半年前的旧话不算复读
        b = h.norm if h.norm is not None else norm_hf_text(h.text)
        if len(b) < cfg.min_chars:
            continue
        head = h.text[:24]
        if b[:cfg.min_chars] == prefix:
            # prefix 是更强证据（开头一字不差），直接覆盖任何 similar 候选；同 prefix 取更近的
            if best is None or best.kind == "similar" or _prev_is_newer(h, best):
                best = HfRepeatVerdict("prefix", 1.0, h.at_sec, head)
            continue
        sim = hf_jaccard(a_gram, hf_char_grams(b))
        if sim < cfg.sim_threshold:
            continue
        if best is None or (best.kind == "similar"
                            and (sim > best.sim or (sim == best.sim and _prev_is_newer(h, best)))):
            best = HfRepeatVerdict("similar", sim, h.at_sec, head)
    return best


# ------------------------------------------------- 内存历史（per 账号 × 群）


_lock = threading.RLock()
#: ``f"{account_id}:{group_id}" -> list[HfRecentReply]``（**旧 → 新**）
_recent: dict[str, list[HfRecentReply]] = {}


def _key(account_id: str, group_id: str) -> str:
    return f"{account_id}:{group_id}"


def check_hf_repeat(account_id: str, group_id: str, text: str, cfg: HfDedupeConfig,
                    now_sec: int, *, proactive: bool | None = None) -> HfRepeatVerdict | None:
    """闸判定（**只读，不改内存**）：调用方 = dispatcher 的 deliver 回调，**发之前**问一句。

    ``enabled=False`` / 非心流主动插话（``proactiveOnly``）⇒ 一律 ``None``（放行），
    连归一化都不做。
    """
    if not cfg.enabled:
        return None
    if cfg.proactive_only and proactive is False:
        return None
    with _lock:
        hist = _recent.get(_key(account_id, group_id))
        if not hist:
            return None
        snapshot = list(hist)
    return hf_repeat_verdict(text, snapshot, cfg, now_sec)


def note_recent_reply(account_id: str, group_id: str, text: str, at_sec: int,
                      cfg: HfDedupeConfig) -> None:
    """记一条**真发出**的发言。

    调用方 = :func:`learn.persist_send_outcome` 的 sent 分支；与开窗/预算**同一条件
    同一时刻**。

    与 :func:`budget.peek_budget` / :func:`budget.note_reply_sent` 的分割同构：
    判定（check）与记账（note）分开，**判了但没发出去的不记账**。

    只读侧不清理过期项（群里可能几小时没发言）⇒ 这里顺手按窗口裁掉过期的，
    顺带把长度压到 ``historyMax``。
    """
    if not cfg.enabled:
        return
    norm = norm_hf_text(text)
    if len(norm) < cfg.min_chars:
        return                                          # 短句既不进历史，也不参与判定（两侧对称）
    with _lock:
        k = _key(account_id, group_id)
        hist = list(_recent.get(k) or [])
        hist.append(HfRecentReply(text[:HF_HISTORY_TEXT_CAP], at_sec, norm))
        kept = [h for h in hist if at_sec - h.at_sec <= cfg.window_sec]
        if len(kept) > cfg.history_max:
            kept = kept[len(kept) - cfg.history_max:]   # 丢最旧的（对齐 JS 的 while/shift）
        _recent[k] = kept


def load_hf_dedupe_history(account_id: str, now_sec: int | None = None) -> int:
    """启动预热：把库里近期**群聊出站文本**灌回内存历史（B7，2026-10-02 修）。

    为什么必须预热：``_recent`` 只在内存里 ⇒ 每次重启后 ``windowSec``（缺省 6 小时）
    内复读闸**等于不存在** —— 老板最在意的"不像真人"里最刺眼的那条症状（换个日期的
    同一段话）会在这段时间里照发不误，而台账 / 日志 / ``/heartflow status`` 一律全绿
    （它们只看"有没有发出去"，看不到"这句话说过没有"）。

    素材取 ``repo_messages.list_outbound_texts``（该账号窗口内的**全部**群聊出站文本，
    不只心流那几句）—— 这是刻意的**超集**：闸要拦的是"这句我说过"，与当初为什么说
    无关（回一句 @ 和主动插话用的是同一张嘴）。唯一副作用是"重启后比稳态多拦几句
    重复话"，落在安全的那一侧；反过来**少拦**才是要治的症状。

    用**缺省策略**决定窗口与长度：预热发生在账号启动，此时真配置还没进 sweep 的手里。
    真配置与缺省不一致时，下一轮 sweep 的 :func:`prune_recent_replies` 会按真配置
    重裁一遍（只可能裁短，不会把丢掉的补回来）。

    返回灌进去的条数；**不抛**（预热失败 = 复读闸从空开始，比不预热松，但不该挡住接入）。
    """
    from ...data import repo_messages       # 延迟导入：加载期不许拉第三方依赖（F5）

    ts = int(time.time()) if now_sec is None else int(now_sec)
    cfg = resolve_dedupe_cfg(None)
    if not cfg.enabled:
        return 0
    try:
        rows = repo_messages.list_outbound_texts(
            account_id, ts - cfg.window_sec, limit=HF_DEDUPE_PRELOAD_LIMIT)
    except Exception as exc:  # noqa: BLE001 —— 见 docstring
        log.warning("[WPP HF] 复读历史预热失败（复读闸从空开始）: account=%s: %s",
                    account_id, exc)
        return 0
    n = 0
    for r in rows:
        text = str(r.get("content") or "")
        gid = str(r.get("gid") or "")
        if not text or not gid:
            continue                        # 脏行（缺群 ⇒ 归不到任何一个历史桶）
        try:
            at = int(r.get("at_sec"))       # ``UNIX_TIMESTAMP`` 回来可能是 Decimal
        except (TypeError, ValueError):
            continue                        # 缺时刻 ⇒ 归档不了（``prune`` 会即时清掉它）
        if at <= 0:
            continue
        note_recent_reply(account_id, gid, text, at, cfg)
        n += 1
    if n > 0:
        log.info("[WPP HF] 复读历史已预热: account=%s n=%d（窗口 %ds）",
                 account_id, n, cfg.window_sec)
    return n


def prune_recent_replies(now_sec: int, cfg: HfDedupeConfig) -> None:
    """sweep：按窗口裁掉过期历史（防长跑进程里内存无限增长；与 ``prune_open_windows`` 同级）。"""
    with _lock:
        for k in list(_recent):
            hist = _recent[k]
            kept = [h for h in hist if now_sec - h.at_sec <= cfg.window_sec]
            if not kept:
                del _recent[k]
            elif len(kept) != len(hist):
                _recent[k] = kept


def hf_recent_count(account_id: str, group_id: str) -> int:
    """测试/诊断：某群当前历史条数。"""
    with _lock:
        return len(_recent.get(_key(account_id, group_id)) or [])


def reset_dedupe_store() -> None:
    """清空内存历史（测试 / 热重载；生产不经此路径）。"""
    with _lock:
        _recent.clear()


def log_hf_repeat_skip(group_id: str, v: HfRepeatVerdict, now_sec: int) -> None:
    """记一条**未发出**的判定（日志用；**不写历史**）。"""
    log.warning("[WPP HF] repeat-suppressed: group=%s kind=%s sim=%.3f prev=%ds ago head=%r",
                group_id, v.kind, v.sim, now_sec - v.prev_at, v.prev_head)


CACHE_REGISTRY.register("dedupe", reset_dedupe_store)


def tracked_groups() -> int:
    """已跟踪的群数（测试 / 可观测）。"""
    with _lock:
        return len(_recent)


__all__ = [
    "HF_DEDUPE_DEFAULTS",
    "HF_DEDUPE_PRELOAD_LIMIT",
    "HF_HISTORY_MAX_CAP",
    "HfDedupeConfig",
    "HfRecentReply",
    "HfRepeatVerdict",
    "check_hf_repeat",
    "hf_char_grams",
    "hf_jaccard",
    "hf_recent_count",
    "hf_repeat_verdict",
    "load_hf_dedupe_history",
    "log_hf_repeat_skip",
    "norm_hf_text",
    "note_recent_reply",
    "prune_recent_replies",
    "reset_dedupe_store",
    "resolve_dedupe_cfg",
    "tracked_groups",
]
