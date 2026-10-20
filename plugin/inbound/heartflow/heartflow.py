"""心流闸门与打分 —— TS ``src/inbound/heartflow.ts`` (v1.3.75 → v1.11.0) 的 Python 侧。

九个模块的**第 9 个**，也是唯一带 IO 与副作用的那个：前八个（``runtime`` / ``budget``
/ ``dedupe`` / ``label`` / ``layer`` / ``learn`` / ``observe`` / ``profile``）都是纯逻辑
或薄管线，真正的"什么时候判、判什么、判完干什么"全在这里。

## 一条消息的五步

===================================  ==================================================
① :func:`check_gate`                 **同步预筛**（开关 / 白名单 / 空 / 冷却 / judge 频率 / 预算）
② :func:`resolve_hf_effective_budget` 生效预算（账号档 ∩ 画像 ∩ 占比外环，只许收紧）
③ :func:`judge_heartflow`            小模型 5 维打分（唯一一次 LLM 调用）
④ :func:`persist_hf_judged`          台账留痕（过 / 不过**都落一行**，见下）
⑤ :func:`pre_send` / :func:`note_sent` 交付缝：重复闸 + 翻译发送结果 → 开窗/占额度
===================================  ==================================================

## 为什么"不过阈值"也要落一行台账（2026-09-13 的教训）

旧实现只在 ``shouldReply=True`` 时写台账 ⇒「judge 跑了但没过」在日志与 DB 里**零留痕**，
与「群里压根没消息」外观完全一致。09-11 的 judge 静默瘫 3 天正是被这一点掩盖的：
台账最后一行停在 09-10，而真实原因只在那条 warn 里。⇒ 不过阈值的走
:func:`learn.persist_hf_judged_below_threshold`（``suppressed/below-threshold``）；
那是 suppressed 行，**不进学习样本**，闭环行为零变更。

## 阈值数字绝不进 prompt（INVARIANT I1）

上游 prompt 里原本有一行 ``**回复阈值**: X (综合评分达到此分数才回复)``。生产实测：
裁判会**按这个数字校准自己的打分** ⇒ 阈值一动、分数跟着动，闭环失效。09-26 画像把
阈值抬到 0.85/0.90 后，台账里"该回"档（0.61–0.82）整体消失、38 次判定最高 0.39，
阈值再也越不过去（**自锁停摆**）。所以 :func:`build_prompt` 里**没有**任何阈值数字 ——
判定仍然按数值阈值做，但那个数只在代码里（见 :func:`parse_response`）。

## 交付缝为什么长这样（Python 侧新增，TS 无对应）

TS 的 deliver 回调（``dispatch/dispatcher.ts``）天然知道"这条回复属于哪条入站消息"
（``msg.trigger === "heartflow"`` 就在手上）。Py 侧没有这一层：Hermes 走
``adapter.handle_message(event)`` → agent → 回复原路回到 ``adapter.send()``，
**send 不知道自己在替哪条心流判定说话**。

⇒ 用一张极小的**待交付登记表**把两头接上：判定通过时登记 ``(账号, 群) → 入站 msgId``
（:func:`begin_pending_send`），``adapter.send`` 发之前问 :func:`pre_send`（重复闸）、
发之后报 :func:`note_sent`（把 ``platform.SendResult`` **显式翻译**成
:class:`learn.HfSendResult`）。

登记有**两个时钟**（B6，2026-10-02）：**拦人**用 :data:`HF_PENDING_SEND_TTL_SEC`（从紧，
拦错了是消息发不出去），**归账**用 :data:`HF_PENDING_SEND_ABSORB_SEC`（从宽，归错了
只是一条样本标签错位）—— 混成一个数的后果见 :func:`take_pending_send`。

⚠️ 翻译这件事必须显式做：``learn.classify_hf_send`` 只认 ``ok``/``msg_id``，直接塞
``platform.SendResult(success=…, message_id=…)`` 会被判 **pending**（= 什么都不改）——
表现是"消息发出去了但账本永远是 pending"：不占预算、不开观察窗，学习闭环少一半样本，
而**日志里一句错都没有**。

## 内存缓存（登记名 ``heartflow``）

群状态（精力/计数）· 原始消息环形缓冲 · judge 频率 · 待交付登记表 —— 四者都在
:func:`reset_heartflow_cache` 一处清空并登记进 :data:`runtime.CACHE_REGISTRY`，
理由见 :mod:`runtime` 的模块 docstring（漏登记 = 测试间互相污染）。

## 加载期纪律（F5）

本模块被 ``platform/adapter.py`` **在函数体内**延迟导入（``_try_heartflow`` /
``_start_sweeps``），所以严格说不在注册链上；但九模块保持同一条纪律：
**模块级不许有第三方 import**（``aiohttp`` 在 :mod:`plugin.llm.judge` 里已经延迟，
这里连它都不 import —— :func:`judge_heartflow` 体内才取）。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..persist import pick_new_msg_id
from . import budget, dedupe, label, observe, profile
from . import learn as L
from .runtime import CACHE_REGISTRY

log = logging.getLogger(__name__)


# ================================================================ 缺省配置


#: 账号级心流参数缺省（对齐 TS ``defaultHeartflowConfig``）。
#:
#: ⚠️ ``enabled: False`` + **没有 model 缺省**都是刻意的：v1.4.0 项目约定"消除 plugin
#: hardcode" —— 模型名必须由 accounts JSON（``heartflow.model``）提供，缺失时
#: :func:`judge_heartflow` **抛错**（不是静默回落某个模型）。静默回落的表现是
#: "换了模型但线上没换"，而它只在账单和判分分布上看得见。
#:
#: ``maxRetries: 1``（不是 2）：5000ms 超时 × 2 次尝试 = 最坏 10s 阻塞，老板
#: 2026-08-30 14:55 定的 C 方案。
HF_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "timeoutMs": 5000,
    # 阈值基线**不在**这里另立一份：真源是 learn.HF_REPLY_THRESHOLD_FALLBACK
    # （09-26 停摆一半的原因是"阈值有三处各算一份"，见 learn 的同名常量注释）。
    "replyThreshold": L.HF_REPLY_THRESHOLD_FALLBACK,
    "energyDecayRate": 0.1,
    "energyRecoveryRate": 0.02,
    "contextMessagesCount": 5,
    "minReplyIntervalSec": 0,
    "minJudgeIntervalSec": 0,
    "whitelistGroups": [],
    "weights": {
        "relevance": 0.25,
        "willingness": 0.2,
        "social": 0.2,
        "timing": 0.15,
        "continuity": 0.2,
    },
    "includeReasoning": False,
    "maxRetries": 1,
}

#: 5 维名字（顺序即 prompt 里的顺序，也是 :class:`HfJudgeResult` 的键顺序）。
HF_DIMENSIONS: tuple[str, ...] = ("relevance", "willingness", "social", "timing", "continuity")

#: 看图缺省（v1.11.0，老板 2026-09-29 拍板）。见 :func:`extract_hf_image_urls`。
HF_VISION_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # 只送 1 张：A/B 里翻盘的两条都只有 1 张图；一张原图已约 +1000 prompt tokens，
    # 群里连发多图会把 5s judge 超时和成本同时顶上去。
    "maxImages": 1,
}

#: 原始消息环形缓冲上限（条/群）。TS ``RAW_BUFFER_MAX``。
HF_RAW_BUFFER_MAX = 200

#: judge 的输出预算（token）。**硬编码，不进配置** —— 与 TS 逐字一致（``heartflow.ts:755``）。
#:
#: 300 是"5 维分数 + 可选 reason"的量级；调大没有意义（judge 不写长文），调小会截断 JSON
#: ⇒ 解析失败 ⇒ 每次都判 null（静默不发言）。09-11 那次静默瘫就是 token 预算被思考吃掉，
#: 所以：**推理必须关**（见 :class:`plugin.llm.judge.JudgeCreds.no_think`）+ 预算只给这么多。
HF_JUDGE_MAX_TOKENS = 300

#: judge 的系统提示（TS 逐字）。
HF_JUDGE_SYSTEM_PROMPT = (
    "你是一个专业的群聊回复决策系统，能够准确判断消息价值和回复时机。\n"
    "你必须严格按照JSON格式返回结果，不要包含任何其他内容！请不要进行对话，只返回JSON！"
)

#: prompt 里的 JSON 格式提醒行 —— 也是**重试**时要替换的那一行（见 :func:`judge_heartflow`）。
#: 单独提成常量是因为它必须与 prompt 正文**逐字相同**：差一个字符，重试就成了空操作，
#: 而"重试没生效"的外观是"模型偶尔坏 JSON"，看不出是代码没改到。
HF_JSON_REMINDER = "**重要！！！请严格按照以下JSON格式回复，不要添加任何其他内容：**"

#: 待交付登记的**拦人**时长（秒）—— 只有它管 :func:`pre_send` 那道重复内容闸。
#:
#: judge 通过 → agent 生成回复 → ``adapter.send``，中间隔着主 LLM 的一次完整生成
#: （长回复 + 工具调用可能十几秒）。120s 足够覆盖，又短到"agent 挂了 ⇒ 登记表过期"
#: 不会把下一条无关发言误算成心流交付。
HF_PENDING_SEND_TTL_SEC = 120

#: 待交付登记的**归账**时长（秒）—— 只影响 :func:`note_sent` 认不认这条登记（B6，2026-10-02）。
#:
#: 为什么两个时钟（细节见 :func:`take_pending_send`）：拦人拦错了的代价是**消息发不出去**
#: （静默丢），归账归错了的代价是**一条样本标签错位** —— 前者的止损要快，后者可以宽。
#: 取 600s 与 ``observeWindowSec`` 同量级：交付尝试比一个观察窗还旧时，把它算进这条
#: 台账已经没有意义（窗都该关完了）。
HF_PENDING_SEND_ABSORB_SEC = 600


def _num_or(v: Any, fallback: float, *, lo: float | None = None,
            hi: float | None = None) -> float:
    """有限数取用，否则 ``fallback``；``bool`` **不算数**（``True`` 会被当 1 混进来）。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return fallback
    f = float(v)
    if lo is not None and f < lo:
        return fallback
    if hi is not None and f > hi:
        return fallback
    return f


def _int_or(v: Any, fallback: int, *, lo: int = 0) -> int:
    """非负整数取用（向下取整），否则 ``fallback``。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return fallback
    if v < lo:
        return fallback
    return int(math.floor(v))


def _bool_or(v: Any, fallback: bool) -> bool:
    """``bool`` 取用；其余类型回落 ``fallback``（不做 JS 式真值转换）。"""
    return v if isinstance(v, bool) else fallback


def default_hf_config() -> dict[str, Any]:
    """缺省配置的**深拷贝**（``weights`` 是嵌套 dict，共享会被就地改坏）。"""
    out = dict(HF_DEFAULTS)
    out["whitelistGroups"] = list(HF_DEFAULTS["whitelistGroups"])
    out["weights"] = dict(HF_DEFAULTS["weights"])
    return out


def resolve_hf_weights(cfg: Mapping[str, Any] | None) -> dict[str, float]:
    """5 维权重（缺省 :data:`HF_DEFAULTS`；脏值逐键回落，不许变成 ``NaN``）。

    ``NaN`` 权重会让 ``overall`` 变成 ``NaN`` ⇒ ``NaN >= threshold`` 恒假 ⇒ **一条都不回**，
    且不报错。与 profile 的"每个数字字段都过钳制"是同一条理由。
    """
    raw = (cfg or {}).get("weights")
    w = raw if isinstance(raw, Mapping) else {}
    D = HF_DEFAULTS["weights"]
    return {k: _num_or(w.get(k), float(D[k]), lo=0.0) for k in HF_DIMENSIONS}


def resolve_hf_vision_cfg(cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """看图参数（缺省 :data:`HF_VISION_DEFAULTS`；``maxImages`` 向下取整且不为负）。"""
    raw = (cfg or {}).get("vision")
    v = raw if isinstance(raw, Mapping) else {}
    return {
        "enabled": _bool_or(v.get("enabled"), HF_VISION_DEFAULTS["enabled"]),
        "maxImages": max(0, _int_or(v.get("maxImages"), HF_VISION_DEFAULTS["maxImages"])),
    }


def is_hf_group_allowed(chat_id: str, cfg: Mapping[str, Any] | None) -> bool:
    """群是否在心流白名单（**空数组 = 全放行**）。

    与 :func:`check_gate` 的白名单子句**同语义**，供 sweep 侧过滤用。两条判据必须同源：
    一份算法两处调用，各写一份迟早漂移（漂移的那一刻 = sweep 在算一个群里根本不判的样本）。
    """
    wl = (cfg or {}).get("whitelistGroups")
    if not isinstance(wl, (list, tuple)) or len(wl) == 0:
        return True
    return chat_id in wl


def resolve_hf_effective_budget(
    account_id: str,
    group_id: str,
    hf_cfg: Mapping[str, Any] | None,
    now_sec: int | None = None,
) -> budget.BudgetConfig:
    """该群**实际生效**的发言预算：账号档 → 画像收紧 → 占比外环收紧，**三层只许变严**。

    为什么收口成一个函数（而不是散在 gate 里）：``/heartflow status`` 必须显示"生效值"。
    否则运维看到的永远是账号档（比如 600s/3 条），而实际生效的是画像压过后的
    （1800s/1 条）—— "为什么这个群一天只回一条"在运维面上无解。与阈值同理：
    一份算法、两处调用。

    纯内存查（三个来源都是缓存）⇒ **judge 热路径零 DB IO**。
    """
    ts = int(now_sec if now_sec is not None else time.time())
    base = budget.resolve_budget(hf_cfg)
    # 画像建议只能让约束**更紧**；静默段默认**不**并入（老板 2026-09-26：「静默段默认关」，
    # 一次幻觉就能让某个群整天无响应 ⇒ 要开须显式 applyQuietHours=true）。
    apply_quiet = profile.resolve_hf_profile_cfg(hf_cfg).apply_quiet_hours
    suggestion = profile.get_hf_profile_budget(account_id, group_id)
    if apply_quiet:
        quiet = profile.get_hf_profile_quiet_hours(account_id, group_id)
        if quiet:
            # ``get_hf_profile_budget`` 按口径不含静默段（见其 docstring），所以显式
            # 开关这条路必须自己把缓存里的原始建议并进来。2026-10-18 前漏了这一步：
            # applyQuietHours=true 却永远拿到空段 —— 开关是个摆设。
            suggestion = {**(suggestion or {}), "quietHours": [list(r) for r in quiet]}
    base = budget.tighten_budget(base, suggestion, apply_quiet_hours=apply_quiet)
    share = observe.get_hf_share_tighten(account_id, group_id, base, hf_cfg, ts)
    return budget.tighten_budget(base, share.as_dict()) if share is not None else base


# ================================================================ 纯逻辑：解析


def extract_heartflow_json(text: Any) -> dict[str, Any] | None:
    """从模型返回文本里稳健提取 JSON **对象**（依次：直接解析 → 剥 markdown 围栏 → 取最外层 ``{...}``）。

    ⚠️ 只认 ``dict``。JS 的 ``JSON.parse("[1,2]")`` 会**成功**返回一个数组，随后
    ``data.relevance`` 是 ``undefined`` ⇒ 5 维全 0 ⇒ 一条 0 分的"有效"结果；Python 侧
    把它当**坏 JSON**（返回 ``None`` ⇒ 触发重试）。这是**故意的收紧**：数组/裸数不是
    我们要求的形状，让它走重试比让它变成一条 0 分记录更接近语义。
    """
    s = str(text if text is not None else "").strip()
    if not s:
        return None
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        return data
    # 剥 markdown 代码块围栏
    cleaned = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        return data
    # 取最外层 {...}（贪婪到最后一个 ``}``）
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def clamp_score(v: Any) -> float:
    """分数钉到 ``[0, 10]``（对齐 TS ``clampScore``）。

    数字/数字字符串取用（越界钳制），其余（含 ``bool``、``None``、坏字符串）⇒ ``0``。
    ``bool`` 必须排掉：Python 的 ``True`` 是 ``int`` 的实例，不排的话"模型回了 true"
    会被当成 10 分。
    """
    if isinstance(v, bool) or v is None:
        return 0.0
    if isinstance(v, (int, float)):
        n = float(v)
    elif isinstance(v, str):
        try:
            n = float(v.strip())
        except ValueError:
            return 0.0
    else:
        return 0.0
    if not math.isfinite(n):
        return 0.0
    return max(0.0, min(10.0, n))


# ============================================================ 纯逻辑：看图（v1.11.0）


def extract_hf_image_urls(text: Any, max_images: int = HF_VISION_DEFAULTS["maxImages"]) -> list[str]:
    """从消息文本里抠图片直链（**纯函数**）。

    形态来自 enrich：``${content}\\n[图片] ${mediaUrl}``（vendor v1 schema 分支尾部还可能
    跟一句 "(注: …)" —— 正则按空白截断，不会被它带歪）。只认 ``http(s)://``
    （生产是 OSS 直链），去重后按出现顺序截取 ``max_images`` 张。

    为什么要看图（2026-09-29 项目约定）：群里的图在 judge 眼里只是一行
    ``[图片] https://…jpg`` 文本 ⇒ 模型只能瞎猜，5 条真实图片消息纯文本判分全部 ~0.25
    （含"零售单晒单"这种明显该接的）。把图交给视觉模型后同一 prompt 只多一个 image_url 块：
    晒单 0.25→0.66~0.705、下单指引 0.31→0.705~0.72（过线），而白酒广告/街景/自拍仍 0.25
    （挡下）—— 是"由瞎猜变实读"，不是无脑抬分。

    ⚠️ 边界（不假装做到了，对齐 TS）：这里按 **scheme** 过滤，**不解析主机** ——
    ``http://127.0.0.1/…`` 也放行。来源本身可信（URL 是自家 enrich 从 vendor 媒体数组里
    取出来的），真要收口该加在**远端抓取侧**；加在这里只是心理安慰。
    """
    if not text or max_images <= 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\[图片\]\s*(\S+)", str(text)):
        url = m.group(1)
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= max_images:
            break
    return out


# ================================================== 内存状态：精力 / 缓冲 / 频率


@dataclass
class HfChatState:
    """每群状态（精力状态机）—— TS ``HeartflowChatState``。"""

    energy: float = 1.0
    last_reply_time: int = 0
    last_reset_date: str = ""
    total_messages: int = 0
    total_replies: int = 0
    #: 上次**时间恢复**的时刻（独立于 ``last_reply_time``，避免恢复推进冷却）。
    #: P0-5 修复：两者共用一个字段时，每次 judge 都推进 ``lastReplyTime`` ⇒
    #: ``secondsSinceLastReply`` 恒 ≈ 0 ⇒ 冷却闸形同不存在。
    last_energy_recovery_ts: int = 0


@dataclass(frozen=True)
class HfRawMessage:
    """一条原始消息（judge 的上下文来源）。"""

    sender_name: str
    sender_id: str
    content: str
    timestamp: float
    is_bot: bool = False


_state_lock = threading.RLock()
_chat_states: dict[str, HfChatState] = {}
_raw_buffers: dict[str, list[HfRawMessage]] = {}
_last_judge_at: dict[str, int] = {}


def _today_utc(now_ms: int) -> str:
    """``YYYY-MM-DD``（**UTC**）。

    ⚠️ 刻意用 UTC 而不是本地时区：TS 是 ``new Date(ms).toISOString().slice(0,10)``。
    本机 TZ 是 +8，若跟着本地走，"每日重置点"会比线上偏 8 小时 —— 而每日重置是
    "精力 +0.2"，偏 8 小时意味着**某个时段多恢复一次**，且只在跨零点前后可见。
    """
    return time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000.0))


def get_chat_state(chat_id: str, cfg: Mapping[str, Any] | None, now_ms: int) -> HfChatState:
    """取（或建）群状态，并应用**每日重置** + **时间自然恢复**。

    P0-5：时间恢复只推 ``last_energy_recovery_ts``，**不碰** ``last_reply_time``
    （原因见 :class:`HfChatState` 的字段注释）。
    """
    with _state_lock:
        st = _chat_states.get(chat_id)
        if st is None:
            st = HfChatState()
            _chat_states[chat_id] = st
        today = _today_utc(now_ms)
        if st.last_reset_date != today:
            st.last_reset_date = today
            st.energy = min(1.0, st.energy + 0.2)       # 每日重置恢复 20%
        if st.last_energy_recovery_ts > 0:
            elapsed_ms = now_ms - st.last_energy_recovery_ts
            if elapsed_ms > 0:
                recovery = _num_or((cfg or {}).get("energyRecoveryRate"),
                                   float(HF_DEFAULTS["energyRecoveryRate"]), lo=0.0)
                # 每 5 分钟恢复 recovery × 5（与 TS 逐字一致）
                st.energy = min(1.0, st.energy + (elapsed_ms / 60000.0) * (recovery * 5))
        st.last_energy_recovery_ts = now_ms
        return st


def seconds_since_last_reply(chat_id: str, now_ms: int) -> float:
    """距上次回复的秒数（``0`` = **从未回复**）。"""
    with _state_lock:
        st = _chat_states.get(chat_id)
        if st is None or st.last_reply_time == 0:
            return 0.0
        return max(0.0, (now_ms - st.last_reply_time) / 1000.0)


def record_active_reply(chat_id: str, cfg: Mapping[str, Any] | None, now_ms: int) -> None:
    """回复后更新状态（精力消耗 + 计数）。"""
    st = get_chat_state(chat_id, cfg, now_ms)
    with _state_lock:
        st.last_reply_time = now_ms
        st.total_replies += 1
        st.total_messages += 1
        decay = _num_or((cfg or {}).get("energyDecayRate"),
                        float(HF_DEFAULTS["energyDecayRate"]), lo=0.0)
        st.energy = max(0.1, st.energy - decay)


def record_passive_message(chat_id: str, cfg: Mapping[str, Any] | None, now_ms: int) -> None:
    """未回复时更新状态（精力恢复 + 消息计数）。"""
    st = get_chat_state(chat_id, cfg, now_ms)
    with _state_lock:
        st.total_messages += 1
        gain = _num_or((cfg or {}).get("energyRecoveryRate"),
                       float(HF_DEFAULTS["energyRecoveryRate"]), lo=0.0)
        st.energy = min(1.0, st.energy + gain)


def record_raw_message(chat_id: str, msg: HfRawMessage) -> None:
    """写入一条原始消息（无论是否触发，含 bot 自己的回复）。"""
    with _state_lock:
        buf = _raw_buffers.get(chat_id)
        if buf is None:
            buf = []
            _raw_buffers[chat_id] = buf
        buf.append(msg)
        if len(buf) > HF_RAW_BUFFER_MAX:
            del buf[:len(buf) - HF_RAW_BUFFER_MAX]


def get_raw_buffer(chat_id: str, n: int) -> list[HfRawMessage]:
    """取最近 ``n`` 条（时间顺序）。"""
    with _state_lock:
        buf = _raw_buffers.get(chat_id) or []
        return list(buf[-n:]) if n > 0 else []


def format_raw_messages(msgs: Sequence[HfRawMessage]) -> str:
    """最近消息 → 文本行（供 prompt）。"""
    if not msgs:
        return "暂无对话历史"
    return "\n".join(
        f"{'[机器人]' if m.is_bot else f'[{m.sender_name}]'}: {m.content}" for m in msgs)


def raw_messages_to_contexts(msgs: Sequence[HfRawMessage]) -> list[dict[str, str]]:
    """最近消息 → 对话上下文（供多模态/多轮消息用）。"""
    return [{"role": "assistant" if m.is_bot else "user", "content": m.content} for m in msgs]


def last_bot_reply(chat_id: str) -> str | None:
    """上次 bot 回复的原文（取**最近一条有正文**的 bot 消息）。"""
    with _state_lock:
        buf = _raw_buffers.get(chat_id) or []
        snapshot = list(buf)
    for m in reversed(snapshot):
        if m.is_bot and (m.content or "").strip():
            return m.content
    return None


def build_chat_context_summary(chat_id: str, cfg: Mapping[str, Any] | None, now_ms: int) -> str:
    """群聊上下文摘要（活跃度 / 历史回复率 / 当前时间 / 上次回复效果）。"""
    st = get_chat_state(chat_id, cfg, now_ms)
    with _state_lock:
        msgs = list(_raw_buffers.get(chat_id) or [])

    # 上次 bot 发言之后群里说了几句（倒着扫到第一条 bot 消息为止）
    post_reply_engagement = ""
    found_bot = False
    user_msgs_after = 0
    for m in reversed(msgs):
        if m.is_bot:
            found_bot = True
            break
        user_msgs_after += 1
    if found_bot:
        if user_msgs_after >= 3:
            post_reply_engagement = "（上次回复后群里进行了热烈讨论）"
        elif user_msgs_after == 0:
            post_reply_engagement = "（上次回复后无人接话）"

    activity = "高" if st.total_messages > 100 else "中" if st.total_messages > 20 else "低"
    reply_rate = (st.total_replies / max(1, st.total_messages)) * 100.0
    hhmm = time.strftime("%H:%M", time.localtime(now_ms / 1000.0))

    info = f"最近活跃度: {activity}\n历史回复率: {reply_rate:.1f}%\n当前时间: {hhmm}"
    if post_reply_engagement:
        info += f"\n回复效果: {post_reply_engagement}"
    return info


def mark_heartflow_judged(chat_id: str, now_ms: int) -> None:
    """标记该群刚 judge 过（``minJudgeIntervalSec`` 频率闸的依据）。

    ⚠️ **无论判定结果都调**（含 judge 返回 ``None``）：频率闸管的是"别把 LLM 打爆"，
    与判定成功与否无关。只在通过时标记的话，一个持续坏 JSON 的群会**每条消息都调一次**。
    """
    with _state_lock:
        _last_judge_at[chat_id] = now_ms


# ================================================================ prompt / 判定


@dataclass(frozen=True)
class HfJudgeInput:
    """5 维判定的输入（字段与 TS ``HeartflowJudgeInput`` 一一对应）。"""

    chat_id: str
    bot_nickname: str = ""
    content: str = ""
    sender_name: str = ""
    chat_context: str = ""
    recent_messages: str = ""
    last_bot_reply: str = ""
    seconds_since_last_reply: float = 0.0
    energy: float = 1.0
    #: v1.7.0 群画像文本（由 profile 生成并**已截断**到上限；无 ⇒ 不注入）。
    #: ⚠️ 必须是**已截断**的成品（``profile.render_hf_profile_for_prompt``）：
    #: judge 的输出预算只有 300 token，未截断的长画像会挤掉 judge 自己的输出空间
    #: （2026-09-13 静默瘫的同类事故）。
    group_profile: str = ""


@dataclass(frozen=True)
class HfJudgeResult:
    """判定结果。``overall_score`` 与 ``dimensions`` 的**尺度不同**（0–1 / 0–10）。"""

    should_reply: bool
    overall_score: float
    dimensions: dict[str, float]
    reasoning: str = ""


def build_prompt(inp: HfJudgeInput, cfg: Mapping[str, Any] | None) -> str:
    """构造 5 维判定 prompt（**user prompt**）。

    ⚠️ **INVARIANT I1**：这里**不许**出现任何形式的阈值数字（原样 / ``%.2f`` / 百分数）。
    上游 prompt 曾有一行"回复阈值: X"，实测裁判会按那个数字校准打分 ⇒ 阈值一动分数跟着动，
    闭环失效 ⇒ 09-26 自锁停摆 3 天（详见模块 docstring）。判定仍按数值阈值做，
    但那个数**只在代码里**（:func:`parse_response`），模型不需要知道。
    """
    c = cfg or {}
    reasoning_part = (
        ',\n    "reasoning": "详细分析原因，说明为什么应该或不应该回复，'
        '需要结合机器人角色特点进行分析，特别说明与上次回复的关联性"'
        if _bool_or(c.get("includeReasoning"), HF_DEFAULTS["includeReasoning"]) else ""
    )
    # 输入全部来自群聊原文 ⇒ 长度不可控（一条长文/链接可以有几万字）。
    # 不设上限时 prompt 费用与延迟随单条消息走，还会把 judge 自己的输出预算挤掉
    # （输出只有 300 token）。画像文本已由 profile 层按 max_prompt_chars 截断，
    # 这里只钳其余四个字段。
    def _clip(value: str, limit: int) -> str:
        text = str(value or "")
        return text if len(text) <= limit else text[:limit] + "…"

    last_reply_str = _clip(inp.last_bot_reply, 500) or "暂无上次回复记录"
    since_min: Any = (round(inp.seconds_since_last_reply / 60.0)
                      if inp.seconds_since_last_reply > 0 else "从未回复")
    profile_part = (f"\n\n## 本群画像 (系统按历史消息自动归纳，供你应景参考)\n{inp.group_profile}"
                    if inp.group_profile else "")
    ctx_count = _int_or(c.get("contextMessagesCount"), HF_DEFAULTS["contextMessagesCount"])

    return f"""你是群聊机器人的决策系统，需要判断是否应该主动回复以下消息。

## 机器人角色设定
{ f"我是 {inp.bot_nickname}，一个群聊机器人助手。" if inp.bot_nickname else "默认角色：智能助手" }

## 当前群聊情况
- 群聊ID: {inp.chat_id}
- 我的精力水平: {inp.energy:.1f}/1.0
- 上次发言: {since_min}{"分钟前" if isinstance(since_min, int) else ""}

## 群聊基本信息
{inp.chat_context}{profile_part}

## 最近{ctx_count}条对话历史
{_clip(inp.recent_messages, 4000)}

## 上次机器人回复
{last_reply_str}

## 待判断消息
发送者: {_clip(inp.sender_name, 100)}
内容: {_clip(inp.content, 2000)}
时间: {time.strftime("%H:%M:%S", time.localtime())}

## 评估要求
请从以下5个维度评估（0-10分），**基于机器人角色设定来判断是否适合回复**：

1. **内容相关度**(0-10)：消息是否有趣、有价值、适合我回复
   - 考虑消息的质量、话题性、是否需要回应
   - 识别并过滤垃圾消息、无意义内容
   - **结合机器人角色特点，判断是否符合角色定位**

2. **回复意愿**(0-10)：基于当前状态，我回复此消息的意愿
   - 考虑当前精力水平和心情状态
   - 考虑今日回复频率控制
   - **基于机器人角色设定，判断是否应该主动参与此话题**

3. **社交适宜性**(0-10)：在当前群聊氛围下回复是否合适
   - 考虑群聊活跃度和讨论氛围
   - **考虑机器人角色在群中的定位和表现方式**

4. **时机恰当性**(0-10)：回复时机是否恰当
   - 考虑距离上次回复的时间间隔
   - 考虑消息的紧急性和时效性

5. **对话连贯性**(0-10)：当前消息与上次机器人回复的关联程度
   - 如果当前消息是对上次回复的回应或延续，应给高分
   - 如果当前消息与上次回复完全无关，给中等分数
   - 如果没有上次回复记录，给默认分数5分

{HF_JSON_REMINDER}

{{
    "relevance": 分数,
    "willingness": 分数,
    "social": 分数,
    "timing": 分数,
    "continuity": 分数{reasoning_part}
}}
"""


def parse_response(text: str, cfg: Mapping[str, Any] | None) -> HfJudgeResult | None:
    """解析 5 维打分响应 → :class:`HfJudgeResult`；坏 JSON ⇒ ``None``（调用方降级为不回复）。

    ``overall`` 是**加权分 ÷ 10** ⇒ 落在 ``[0, 1]``（INVARIANT I11，与账号级
    ``replyThreshold`` 同尺度）。阈值取 :func:`learn.resolve_reply_threshold`
    （单一真源：heartflow 不许自己写 ``cfg.get("replyThreshold") or 0.6``）。
    """
    data = extract_heartflow_json(text)
    if data is None:
        return None

    dims = {k: clamp_score(data.get(k)) for k in HF_DIMENSIONS}
    w = resolve_hf_weights(cfg)
    overall = sum(dims[k] * w[k] for k in HF_DIMENSIONS) / 10.0
    raw_reason = data.get("reasoning")
    return HfJudgeResult(
        should_reply=overall >= L.resolve_reply_threshold(cfg),
        overall_score=overall,
        dimensions=dims,
        reasoning=raw_reason if isinstance(raw_reason, str) else "",
    )


def resolve_business_context(chat_id: str, cfg: Mapping[str, Any] | None) -> str:
    """该群 judge 用的业务背景：``groupBusinessContexts[群ID]`` 优先，否则账号级。

    账号级 ``businessContext`` 是 2026-10-16 为**全员群**写的那段（晒单/喜报口径），
    但它拼在**每个群**的 judge system prompt 最前面 —— 移动业务对接群、管理群读到的
    是错的语境。2026-10-18 起按群覆盖：群级值非空白 ⇒ 用群级；否则回落到账号级
    （空串/空白**不**当作"本群无背景"，避免一个笔误把背景整段丢掉）。
    """
    c = cfg or {}
    per = c.get("groupBusinessContexts")
    if isinstance(per, Mapping):
        value = per.get(chat_id)
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = c.get("businessContext")
    return value.strip() if isinstance(value, str) else ""


async def judge_heartflow(inp: HfJudgeInput, cfg: Mapping[str, Any] | None,
                          creds: Any = None) -> HfJudgeResult | None:
    """调小模型做 5 维打分。任何失败（无 key / 超时 / HTTP / 坏 JSON）⇒ ``None``（降级不回复）。

    带重试（``maxRetries``，缺省 1 ⇒ 最多 2 次调用）。重试时把 JSON 提醒行换成
    "这是第 N 次尝试"版本 —— 换的是**同一行**（:data:`HF_JSON_REMINDER`），
    prompt 其余部分逐字不变。

    ⚠️ ``model`` 缺失 ⇒ **抛错**（不是回落）：v1.4.0 项目约定"消除 plugin hardcode"，
    模型名必须由 accounts JSON 提供。静默回落的表现是"改了配置但线上没改"。
    """
    from ...llm import judge as judge_mod  # 延迟导入：保持模块级零第三方依赖

    creds = creds if creds is not None else judge_mod.resolve_judge_creds()
    if not getattr(creds, "api_key", ""):
        log.warning("[WPP HF] missing judge API key (JUDGE_API_KEY / MINIMAX_API_KEY), "
                    "skip heartflow judge")
        return None

    model = (cfg or {}).get("model")
    if not model or not isinstance(model, str):
        raise ValueError(
            "[WPP HF] heartflow.model 未配置。v1.4.0 项目约定：模型名必须由 "
            "accounts/<id>.json 的 heartflow.model 提供，插件**不再有** hardcode 回落。")

    timeout_ms = _int_or((cfg or {}).get("timeoutMs"), HF_DEFAULTS["timeoutMs"], lo=1)
    max_retries = max(0, _int_or((cfg or {}).get("maxRetries"), HF_DEFAULTS["maxRetries"]))

    prompt = build_prompt(inp, cfg)
    # v1.11.0 看图：只从**待判定消息**里取图（不取上下文图：成本可控 + 焦点清楚）。
    # 无图 / 关掉 / 端点不支持多模态（非 openai 格式）时 images 为空 ⇒ 请求体与本版之前
    # **逐字节一致**（INVARIANT I9，由 llm/judge.py 保证：无图时 content 保持纯字符串）。
    vision = resolve_hf_vision_cfg(cfg)
    images = extract_hf_image_urls(inp.content, vision["maxImages"]) if vision["enabled"] else []
    # 业务背景按群取：``groupBusinessContexts[chat_id]`` 优先，缺省才是账号级
    # （账号级那段写的是全员群的晒单/喜报口径，见 :func:`resolve_business_context`）。
    business = resolve_business_context(inp.chat_id, cfg)
    system_prompt = ((business + "\n\n") if business else "") + HF_JUDGE_SYSTEM_PROMPT

    last_err = ""
    for attempt in range(max_retries + 1):
        user_prompt = prompt if attempt == 0 else prompt.replace(
            HF_JSON_REMINDER,
            f"**重要！！！请严格按照以下JSON格式回复，不要添加任何其他内容！"
            f"这是第{attempt + 1}次尝试，请确保返回有效的JSON格式！**")
        try:
            text = await judge_mod.call_judge(
                model=model, user_prompt=user_prompt, system_prompt=system_prompt,
                max_tokens=HF_JUDGE_MAX_TOKENS, timeout_ms=timeout_ms, creds=creds,
                images=images)
            result = parse_response(text, cfg)
            if result is not None:
                return result
            last_err = f"unparseable: {str(text)[:80]}"
        except Exception as exc:  # noqa: BLE001 —— 无 key/超时/HTTP/坏响应都走这里
            last_err = str(exc)
    log.warning("[WPP HF] judge failed after %d attempts: %s", max_retries + 1, last_err)
    return None


# ================================================================ 闸门（同步预筛）


@dataclass(frozen=True)
class HfGateResult:
    """闸门结论。``allowed=False`` 时 ``reason`` 是**第一条**命中的原因（便于归因）。"""

    allowed: bool
    reason: str | None = None


def check_gate(chat_id: str, content: str, cfg: Mapping[str, Any] | None, now_ms: int,
               account_id: str | None = None, candidate_at_sec: int | None = None,
               budget_override: budget.BudgetConfig | None = None) -> HfGateResult:
    """心流触发门禁（**纯同步预筛，不调 LLM**）。

    顺序（不可调换，与 TS 逐字一致）：关闭 → 白名单 → 空消息 → 冷却 → judge 频率 → 预算。
    预算放**最后**：被拦的消息连 LLM 都不调（预算判定用 ``peek``，只判不记 ——
    真正的计数落账在发送成功时，见 :func:`learn.persist_hf_send_outcome`，
    这样"判了但被下游拦掉"的不会占用额度）。

    :param account_id: 预算计数按账号分桶；省略时退化为 ``*`` 桶
        （group id 本身全局唯一，不会串群）
    :param candidate_at_sec: 本次候选消息的**自己的时刻**（秒）；省略 = 现在。
        用于预算的"陈旧触发防重放"（debounce 重放/重试/补扫的同一批消息）。
    :param budget_override: 生效预算（:func:`resolve_hf_effective_budget` 的产物）。
        **用参数而不是在函数内算**：算它要 import 画像与占比模块，而 ``profile`` 反向
        import 本模块（拿 ``is_hf_group_allowed``）⇒ 函数内联就会成环。
    """
    c = cfg or {}
    if not c.get("enabled"):
        return HfGateResult(False, "disabled")
    if not is_hf_group_allowed(chat_id, c):
        return HfGateResult(False, "not-whitelisted")
    if not (content or "").strip():
        return HfGateResult(False, "empty")

    min_interval = _int_or(c.get("minReplyIntervalSec"), HF_DEFAULTS["minReplyIntervalSec"])
    if min_interval > 0:
        since = seconds_since_last_reply(chat_id, now_ms)
        # ⚠️ ``since > 0`` 不能省：``0`` 的语义是**从未回复**（不是"刚刚回复过"），
        #    否则新群的第一条消息会被自己的冷却闸吃掉。
        if 0 < since < min_interval:
            return HfGateResult(False, "cooling")

    judge_interval = _int_or(c.get("minJudgeIntervalSec"), HF_DEFAULTS["minJudgeIntervalSec"])
    if judge_interval > 0:
        with _state_lock:
            last = _last_judge_at.get(chat_id, 0)
        if last > 0 and now_ms - last < judge_interval * 1000:
            return HfGateResult(False, "judge-cooldown")

    now_sec = now_ms // 1000
    allowed, reason = budget.peek_budget(
        account_id, chat_id,
        budget_override if budget_override is not None else budget.resolve_budget(c),
        now_sec, candidate_at_sec if candidate_at_sec is not None else now_sec)
    if not allowed:
        return HfGateResult(False, reason or "budget")
    return HfGateResult(True)


# ==================================================== 旁路观测（非 blocked 群消息）


def _cfg_for(account_id: str) -> Mapping[str, Any]:
    """该账号的心流配置（取不到 ⇒ ``{}`` = 全缺省 + ``enabled=False``，**不抛**）。"""
    try:
        from ...core import accounts as accounts_mod

        raw = getattr(accounts_mod.resolve(account_id), "heartflow", None)
    except Exception as exc:  # noqa: BLE001 —— 配置一时读不出来不该打断收信
        log.debug("[WPP HF] 取账号 %s 心流配置失败: %s", account_id, exc)
        return {}
    return raw if isinstance(raw, Mapping) else {}


def note_group_message(account_id: str, group_id: str, *, sender_name: str, content: str,
                       ts_sec: float, is_bot: bool = False, quotes_bot: bool = False,
                       mentions_bot: bool = False, now_sec: int | None = None) -> None:
    """非 blocked 群消息的**统一旁路入口**（对应 TS handler 的 batch 步骤 1:1）。

    一次调用做三件事（TS 里分散在三个循环里，Py 侧没有 batch 层，故合成一处）：

    ① 进原始缓冲（judge 的上下文；**含 bot 自己发的**）；
    ② 记"该群有人类消息"（预算模块的观测字段，供画像参考，**不是闸**）；
    ③ 开窗时构造**接话候选**（:func:`learn.mark_hf_group_engaged`）。

    ③ 的取数门是"该群**有开窗**"（内存 Map 查，零 DB IO）—— 绝大多数群消息在这里
    直接跳过。同一批消息的候选在 TS 里一次性喂给 ``markHfGroupEngaged`` 统一判优先级；
    Py 侧逐条喂，等价（判定是"窗口内候选集合"上的聚合，逐条喂只是把同一集合拆开）。
    """
    cfg = _cfg_for(account_id)
    if not cfg.get("enabled"):
        return
    record_raw_message(group_id, HfRawMessage(
        sender_name=sender_name or "未知", sender_id="",
        content=content or "", timestamp=float(ts_sec), is_bot=is_bot))
    if is_bot:
        # ⚠️ bot 自己不算接话、也不记"有人类消息"（否则预算的沉默时长指标被自己刷新）。
        return
    if mentions_bot or quotes_bot:
        # 有人在跟机器人说话 —— 若此刻有在心流登记，给它打上"随后的出站不是它"（B3）。
        note_bot_mention(account_id, group_id)
    ts = int(now_sec if now_sec is not None else time.time())
    if is_hf_group_allowed(group_id, cfg):
        budget.note_human_message(account_id, group_id, ts)
        if L.get_open_hf_window(account_id, group_id) is not None:
            L.mark_hf_group_engaged(account_id, group_id, ts, (
                label.HfEngageCandidate(at_sec=int(ts_sec), text=content or "",
                                        quotes_bot=quotes_bot, mentions_bot=mentions_bot),))


def note_bot_reply(account_id: str, group_id: str, text: str,
                   ts_sec: float | None = None) -> None:
    """**出站**记录 bot 自己的群发言（对应 TS ``recordHeartflowBotReply``）。

    为什么走**出站**而不是等 vendor 回推：回推的 sender 会被 vendor 标成 bot 自己，
    适配器第 ① 步就把它丢了（TS 那边同样是 ``blocked``）。两边都记的后果不是"更全"，
    而是缓冲里同一句话出现两份 ⇒ "最近 N 条上下文"和 ``last_bot_reply`` 都掺水。

    ⚠️ 只在**发送成功**后调（适配器保证）：失败的那句群里根本没出现，
    记进去会让 judge 以为"我上次已经说过了"。

    只记群：私聊不进心流缓冲（TS 同款判据 ``toWxid`` 含 ``@chatroom``）。
    """
    if "@chatroom" not in (group_id or ""):
        return
    cfg = _cfg_for(account_id)
    if not cfg.get("enabled"):
        return
    record_raw_message(group_id, HfRawMessage(
        sender_name="bot", sender_id="bot", content=text or "",
        timestamp=float(ts_sec if ts_sec is not None else time.time()), is_bot=True))


# ================================================================ 交付缝


@dataclass(frozen=True)
class HfPendingSend:
    """一条"判定已通过、等 agent 产出回复"的登记（见模块 docstring 的交付缝一段）。"""

    account_id: str
    group_id: str
    inbound_msg_id: str
    at_sec: int
    expires_at_sec: int
    #: 登记**之后**该群里有人 @ / 引用机器人（B3）。置上 ⇒ 随后的出站回的是**那一条**，
    #: 不是这条心流插话（见 :func:`note_bot_mention`）。
    newer_mention_at_sec: int | None = None


@dataclass(frozen=True)
class HfDeliveryPlan:
    """``adapter.send`` 发之前的结论。

    ``placeholder`` 非空 ⇒ **不要真发**，把它当发送结果返回（``classify_hf_send``
    会判 suppressed ⇒ 不占预算、不开窗、不进重复历史，INVARIANT I5）。
    """

    placeholder: str | None = None
    verdict: Any = None


_pending: dict[str, HfPendingSend] = {}
#: 重复闸拦下的那条占位符（与 :data:`learn.HF_PLACEHOLDER_MSG_IDS` 同族）。
HF_REPEAT_PLACEHOLDER = "repeat-suppressed"


def _pending_key(account_id: str, group_id: str) -> str:
    return f"{account_id}:{group_id}"


def begin_pending_send(account_id: str, group_id: str, inbound_msg_id: str,
                       now_sec: int | None = None) -> None:
    """登记"这条群即将产出一条心流回复"（判定通过时调）。"""
    ts = int(now_sec if now_sec is not None else time.time())
    with _state_lock:
        _pending[_pending_key(account_id, group_id)] = HfPendingSend(
            account_id=account_id, group_id=group_id, inbound_msg_id=inbound_msg_id,
            at_sec=ts, expires_at_sec=ts + HF_PENDING_SEND_TTL_SEC)


def note_bot_mention(account_id: str, group_id: str) -> None:
    """旁路：该群有人 **@ / 引用机器人** —— 给登记中的那条打上"这批出站不是它"的标记。

    B3（2026-10-02 修）。登记只按 ``(账号, 群)`` 单槽 + 时长近似归属 —— 框架**不给**
    回执关联 id：WeChat 平台上 ``reply_to`` 与 thread metadata 恒为空（Hermes
    ``gateway/run_turn.py`` 的 ``_progress_reply_to`` / ``_thread_metadata_for_progress``
    只对 FEISHU / MATTERMOST / BUZZ / relay 取值），所以"这条出站是不是刚才那条心流
    插话"只能靠时间近似。少了这道判据，老板 @ 机器人之后的那条正常回复会被当成心流
    插话去过重复内容闸，命中就**静默丢弃**（还返回 ``success=True``）——
    表现正是"群里 @ 它，它不回，日志一句错都没有"。

    判据故意是"这之后有人 @/引用过"而**不是**"有人说过话"：闲聊不该让重复闸失效 ——
    老板 2026-09-26 拍板的去重，治的就是闲聊群里"换个日期的同一段话"。

    时序：适配器 ``_handle_one`` 的 ④a 旁路观测（本函数的调用点）排在 ⑤ 交给 runner
    **之前**，而 ``begin_pending_send`` 在 ④a 之后 —— 于是"这条 @ 自己触发了心流接管"
    时，标记落在**旧**登记上，随即被那次 ``begin_pending_send`` 整条替换 ⇒ 不会自我否决。
    """
    key = _pending_key(account_id, group_id)
    with _state_lock:
        p = _pending.get(key)
        if p is None or p.newer_mention_at_sec is not None:
            return
        _pending[key] = HfPendingSend(
            account_id=p.account_id, group_id=p.group_id, inbound_msg_id=p.inbound_msg_id,
            at_sec=p.at_sec, expires_at_sec=p.expires_at_sec,
            newer_mention_at_sec=int(time.time()))


def take_pending_send(account_id: str, group_id: str,
                      now_sec: int | None = None,
                      *, ttl_sec: int | None = None) -> HfPendingSend | None:
    """取出并**删除**登记（过期 ⇒ 丢弃并返回 ``None``）。

    ``ttl_sec`` 是**判过期的那个时钟**，两条调用路各有各的（B6，2026-10-02 修）：

    * ``pre_send`` 用缺省 :data:`HF_PENDING_SEND_TTL_SEC`（120s）—— 这是**拦人**的闸。
      拦错了的代价是消息发不出去（静默丢），所以从紧。
    * ``note_sent`` 传 :data:`HF_PENDING_SEND_ABSORB_SEC`（600s）—— 这是**归账**。
      旧写法两处共用 120s ⇒ 慢模型（agent 一次生成 >120s，工具调用多时会）发出的
      那条真回复，``pre_send`` 已按"不是心流交付"放行、``note_sent`` 也跟着当没这回事
      ⇒ 不占预算、不开观察窗、不进复读历史，台账那行停在 ``judged`` 被 sweep 收敛成
      "没人接话"（负样本）。**消息明明发出去了**。
    """
    ts = int(now_sec if now_sec is not None else time.time())
    with _state_lock:
        p = _pending.pop(_pending_key(account_id, group_id), None)
    if p is None:
        return None
    ttl = int(HF_PENDING_SEND_TTL_SEC if ttl_sec is None else ttl_sec)
    if ts > p.at_sec + ttl:
        log.debug("[WPP HF] pending send 过期（%ds > %ds）⇒ 不记账: account=%s group=%s",
                  ts - p.at_sec, ttl, account_id, group_id)
        return None
    return p


def pending_send_count() -> int:
    with _state_lock:
        return len(_pending)


def pre_send(account_id: str, group_id: str, text: str,
             now_sec: int | None = None) -> HfDeliveryPlan | None:
    """交付**前置**：``None`` = 不是心流交付（照常发）；否则按 :class:`HfDeliveryPlan` 处置。

    重复内容闸在这里（**发之前**）：同群 6 小时内高度相似的心流主动插话不发 ——
    老板 2026-09-26 拍板。只读回放真实账本显示 bot 一部分发言是"换个日期的同一段话"，
    这是"不像真人"最刺眼的症状。

    ⚠️ 被拦下时**也要落账**（``suppressed/repeat``）：否则台账里那条行会一直是
    ``judged``，被 sweep 的呆账收敛当成"没人接话"（负样本）—— 而它其实**根本没发**。
    """
    ts = int(now_sec if now_sec is not None else time.time())
    with _state_lock:
        p = _pending.get(_pending_key(account_id, group_id))
    if p is None:
        return None
    # ★ B2（2026-10-02 修）：**过期登记 ≠ 心流交付**。
    #   旧写法这里只 ``.get()``、不看 TTL（只有 ``take_pending_send`` 看），于是"判定通过、
    #   但 agent 最终没产出回复"的那条登记会**一直挂着**：此后该群**每一条**出站（含老板
    #   @ 之后的正常回复）都得先过重复内容闸，命中就静默丢弃并返回 ``success=True``。
    #   表现 = "群里 @ 机器人，它不回，日志一句错都没有"。TTL 与 ``take_pending_send`` 同源。
    #
    # ★ B6（2026-10-02 修）：这一支**不再把登记删掉**。删了就没人给慢模型那次出站归账了
    #   —— "闸门过期"的语义是"这条出站不该被拦"，不是"这条出站不是心流交付"。
    #   登记留给 ``note_sent``（宽时钟 absorb）去认；没人来认的话，它会被该群下一次
    #   ``take_pending_send`` 或 ``begin_pending_send`` 顶掉（每群至多一条，不会堆积）。
    if ts > p.expires_at_sec:
        log.info("[WPP HF] 待交付登记已过期（判过 %ds 前）⇒ 本条按普通发送放行，"
                 "登记留给归账: account=%s group=%s msgId=%s",
                 ts - p.at_sec, account_id, group_id, p.inbound_msg_id)
        return None
    # ★ B3（2026-10-02 修）：登记之后群里有人 @/引用机器人 ⇒ 这批出站回的是**那一条**，
    #   不是这条心流插话。判据与理由见 :func:`note_bot_mention`（框架不给关联 id，只能这样收口）。
    if p.newer_mention_at_sec is not None:
        log.info("[WPP HF] 登记后有更新的 @/引用（%ds 前）⇒ 本条不是心流插话，放行: "
                 "account=%s group=%s", ts - p.newer_mention_at_sec, account_id, group_id)
        return None
    cfg = _cfg_for(account_id)
    dcfg = dedupe.resolve_dedupe_cfg(cfg)
    verdict = dedupe.check_hf_repeat(account_id, group_id, text, dcfg, ts, proactive=True)
    if verdict is None:
        return HfDeliveryPlan()
    log.info("[WPP HF] 重复内容闸拦下: account=%s group=%s sim=%.2f",
             account_id, group_id, getattr(verdict, "sim", 0.0))
    take_pending_send(account_id, group_id, ts)
    L.persist_hf_send_outcome(
        account_id, p.inbound_msg_id,
        L.HfSendResult(ok=True, msg_id=HF_REPEAT_PLACEHOLDER), ts,
        group_id=group_id, observe_sec=0, label_window_sec=0, text=None)
    return HfDeliveryPlan(placeholder=HF_REPEAT_PLACEHOLDER, verdict=verdict)


def note_sent(account_id: str, group_id: str, text: str, *, success: bool,
              message_id: str | None, now_sec: int | None = None) -> None:
    """交付**后置**：把 ``platform.SendResult`` 翻译成 :class:`learn.HfSendResult` 并落账。

    ⚠️ 翻译是**必需**的（不是美化）：``learn.classify_hf_send`` 只认 ``ok``/``msg_id``，
    直接塞 ``success``/``message_id`` 会被判 pending ⇒ 消息发出去了但账本永远是 pending
    （不占预算、不开窗、无日志）。这条形状不匹配在 learn 侧还有一道 warn 兜底。

    ⚠️ **发送没成功时不消费登记**：``pending`` 的语义就是"等框架重试"（见
    :func:`learn.classify_hf_send`）。这里若顺手把登记吃掉，重试那次就再也匹配不上登记
    ⇒ 台账那行永远停在 ``judged``，被 sweep 的呆账收敛当成"没人接话"—— 而消息**根本没发出去**。

    ★ **B8（2026-10-02 修）：回复状态（冷却/精力/计数）只在这里记**。
    旧写法在 judge 通过时就调 :func:`record_active_reply`（TS ``handler.ts`` 同形），
    于是"判了但没发出去"的两条路都会把 ``last_reply_time`` 推到当下：
    ① 复读闸静默不发（交付缝返回占位符，群里一个字都没有）；
    ② 生成/发送失败（``pending``，这条函数直接 return）。
    代价是 :func:`check_gate` 的 ``cooling`` 闸接下来 ``minReplyIntervalSec`` 内
    一律装死 —— 老板看到的正是"明明该接的话它不说话"；顺带白扣一格精力、
    ``total_replies`` 平白多一条（喂给 judge 的"历史回复率"因此偏高）。
    放到这里是因为**只有这条路 = 话真进了群**：占位符那条根本不经过本函数。
    """
    ts = int(now_sec if now_sec is not None else time.time())
    result = L.HfSendResult(ok=bool(success), msg_id=(message_id or None))
    outcome = L.classify_hf_send(result)
    if outcome == L.HF_SEND_OUTCOME_PENDING:
        with _state_lock:
            inflight = _pending.get(_pending_key(account_id, group_id))
        if inflight is not None:
            log.warning("[WPP HF] 心流回复发送失败，账本不动（登记留着等重试）: "
                        "account=%s group=%s msgId=%s", account_id, group_id,
                        inflight.inbound_msg_id)
        return
    # ★ B6（2026-10-02 修）：归账用**宽时钟**。这条出站已经真的发出去了（``note_sent``
    #   只在 ``classify_hf_send != pending`` 之后走到这里），慢一点不该让它变成"没发生"。
    p = take_pending_send(account_id, group_id, ts, ttl_sec=HF_PENDING_SEND_ABSORB_SEC)
    if p is None:
        return
    cfg = _cfg_for(account_id)
    learning = L.resolve_hf_learning(cfg)
    L.persist_hf_send_outcome(
        account_id, p.inbound_msg_id, result,
        ts, group_id=group_id,
        observe_sec=learning["observeWindowSec"], label_window_sec=learning["labelWindowSec"],
        text=text, dedupe_cfg=dedupe.resolve_dedupe_cfg(cfg))
    # ★ B8：账本判成 sent（``suppressed`` 是"框架把它丢了"，也算没进群）才记回复状态。
    if outcome == L.HF_SEND_OUTCOME_SENT:
        record_active_reply(group_id, cfg, ts * 1000)


# ================================================================ 接入口


def _sender_display_name(account_id: str, group_id: str, msg: Any) -> str:
    """发送者可读名：vendor 昵称 → 群成员昵称 → 通讯录 → wxid 兜底。

    心流 prompt 里的 ``发送者:`` 直接决定模型会不会称呼人。入站帧常只带 wxid
    （``from_nickname`` 空），单靠它模型看到的就是一串 ``wxid_xxx``，于是回复里
    不点名（2026-10-20 移动群实测：晒单被夸但没称呼）。

    ``session_title.sender_display_name`` 本来就查群成员表 + 通讯录，且 300s
    进程内缓存 —— 直接用那条链，别在这里重写一份昵称解析。
    """
    wxid = getattr(msg, "from_wxid", "") or ""
    pushed = getattr(msg, "from_nickname", "") or ""
    try:
        from .. import session_title as _titles
        name = _titles.sender_display_name(
            account_id, group_id, is_group=True, user_id=wxid, fallback=pushed)
    except Exception as exc:  # noqa: BLE001 —— 名字只影响措辞，不能让心流整条失败
        log.debug("[WPP HF] 发送者昵称解析失败，沿用 wxid: %s", exc)
        name = pushed or wxid
    return name or "未知"


async def maybe_engage(adapter: Any, acct: Any, msg: Any, pipeline: Any = None) -> bool:
    """心流接管入口（``adapter._try_heartflow`` 调）—— 返回 ``True`` = 放行给主 LLM。

    流程即模块 docstring 的五步；``False`` 覆盖所有"不接管"的情形（闸拒 / judge 不过 /
    judge 没出结果 / 异常）。异常**吞掉并记账**：心流判不出来不该让这条消息整体失败
    （适配器外层还有一道 try，但那里只能记一条 warning，分不清是哪一步坏的）。
    """
    cfg = getattr(acct, "heartflow", None)
    if not isinstance(cfg, Mapping) or not cfg.get("enabled"):
        return False

    account_id = str(getattr(acct, "id", "") or getattr(acct, "account_id", ""))
    group_id = getattr(msg, "chatroom_id", None) or getattr(msg, "peer_id", "")
    content = getattr(msg, "content", "") or ""
    getattr(acct, "self_wxid", "") or ""
    nickname = getattr(acct, "nickname", "") or ""
    candidate_at = getattr(msg, "timestamp", None)
    now_ms = int(time.time() * 1000)
    now_sec = now_ms // 1000

    try:
        budget_eff = resolve_hf_effective_budget(account_id, group_id, cfg, now_sec)
        gate = check_gate(group_id, content, cfg, now_ms, account_id,
                          int(candidate_at) if candidate_at else None, budget_eff)
        if not gate.allowed:
            log.debug("[WPP HF] gate 拒绝 (%s): account=%s group=%s",
                      gate.reason, account_id, group_id)
            return False

        eff = L.resolve_hf_effective_threshold(account_id, group_id, cfg, now_sec)
        if eff.band_capped:
            log.warning(
                "[WPP HF] profile band capped: group=%s 画像建议 %.2f → 生效 %.2f "
                "（上限 = 基线 +%.2f；画像不是重新定义及格线的地方）",
                group_id, eff.band or 0.0, eff.threshold,
                profile.resolve_hf_profile_cfg(cfg).max_raise)
        base = L.resolve_reply_threshold(cfg)
        eff_cfg: Mapping[str, Any] = cfg if abs(eff.threshold - base) < 1e-9 else {
            **cfg, "replyThreshold": eff.threshold}

        st = get_chat_state(group_id, cfg, now_ms)
        result = await judge_heartflow(HfJudgeInput(
            chat_id=group_id,
            bot_nickname=nickname,
            content=content,
            sender_name=_sender_display_name(account_id, group_id, msg),
            chat_context=build_chat_context_summary(group_id, cfg, now_ms),
            recent_messages=format_raw_messages(
                get_raw_buffer(group_id, _int_or(cfg.get("contextMessagesCount"),
                                                 HF_DEFAULTS["contextMessagesCount"]))),
            last_bot_reply=last_bot_reply(group_id) or "",
            seconds_since_last_reply=seconds_since_last_reply(group_id, now_ms),
            energy=st.energy,
            group_profile=profile.get_hf_profile_prompt_text(account_id, group_id) or "",
        ), eff_cfg)
        # ⚠️ 无论结果都标记（含 result=None）—— 频率闸管的是"别把 LLM 打爆"。
        mark_heartflow_judged(group_id, now_ms)
        if result is None:
            # judge 没出结果 ⇒ **不落台账**（否则与"判定未过"混淆，见模块 docstring）
            return False

        record = L.HfLedgerRecord(
            account_id=account_id,
            inbound_msg_id=str(getattr(msg, "msg_id", "") or ""),
            group_id=group_id,
            judged_at=now_sec,
            # ★ ``WppMessage`` **没有** ``new_msg_id`` 字段 ⇒ ``getattr`` 恒空（静默
            #   空操作）。服务端 id 只能从原始 payload 取，否则台账此列永远是 NULL，
            #   "按 svrid 回查被引用的消息"这条路永远查不到心流判定过的那条。
            new_msg_id=pick_new_msg_id(getattr(msg, "raw", None)),
            from_wxid=(str(getattr(msg, "from_wxid", "") or "") or None),
            msg_type=(str(getattr(msg, "msg_type", "")) if getattr(msg, "msg_type", None) is not None else None),
            content_head=(re.sub(r"\s+", " ", content).strip()[:256] or None),
            judge_overall=result.overall_score,
            dim_r=result.dimensions["relevance"],
            dim_w=result.dimensions["willingness"],
            dim_s=result.dimensions["social"],
            dim_t=result.dimensions["timing"],
            dim_c=result.dimensions["continuity"],
            effective_threshold=eff.threshold,
            energy=st.energy,
        )
        metrics = _metrics()
        if result.should_reply:
            metrics.inc_hf_judge_passed()
            L.persist_hf_judged(record)
            # ★ B8（2026-10-02 修）：这里**不再**记回复状态。judge 通过 ≠ 话进了群 ——
            #   这条回复还要过生成（主 LLM）与交付缝（复读闸可能静默不发 / 发送可能失败）。
            #   在这里记 ``last_reply_time`` 会让冷却闸把"没发出去的那次"当成发过了，
            #   接下来 ``minReplyIntervalSec`` 内群里再喊它也不应。挪到 :func:`note_sent`
            #   （真发出之后） —— 顺带 ``total_messages`` 也跟着挪：一条没发出去的回复
            #   不该既算"回复数"又不算"消息数"（那会让回复率凭空偏高）。
            begin_pending_send(account_id, group_id, record.inbound_msg_id, now_sec)
            log.info("[WPP HF] trigger: account=%s group=%s msgId=%s score=%.2f",
                     account_id, group_id, record.inbound_msg_id, result.overall_score)
            return True
        metrics.inc_hf_judge_below()
        L.persist_hf_judged_below_threshold(record, now_sec)
        record_passive_message(group_id, cfg, now_ms)
        log.debug("[WPP HF] skip (score=%.2f, threshold=%.2f): group=%s",
                  result.overall_score, eff.threshold, group_id)
        return False
    except Exception as exc:
        log.warning("[WPP HF] judge error (skip): account=%s group=%s: %s",
                    account_id, group_id, exc, exc_info=True)
        return False


def _metrics() -> Any:
    """延迟导入埋点模块（加载期纪律，同 F5）。"""
    from ...core import metrics

    return metrics


# ================================================================ 缓存登记


def reset_heartflow_cache() -> None:
    """测试 / 重置：清空群状态 + 原始缓冲 + judge 频率 + 待交付登记表。

    ⚠️ 四者**一起**清是刻意的：它们都是"这条消息/这次判定的上下文"，只清一半的表现是
    **测试间互相污染**（上一个用例的精力/缓冲漂到下一个用例），那种失败看起来像业务 bug。
    其余六处缓存各有自己的登记名（``budget`` / ``dedupe`` / ``layer`` /
    ``observe`` / ``profile`` / ``learn``），``CACHE_REGISTRY.reset_all()`` 才全部清掉。
    名单与总数钉在 ``plugin/tests/heartflow/test_runtime.py::KNOWN_CACHES``。
    """
    with _state_lock:
        _chat_states.clear()
        _raw_buffers.clear()
        _last_judge_at.clear()
        _pending.clear()


CACHE_REGISTRY.register("heartflow", reset_heartflow_cache)


# --------------------------------------------------------------- 测试可观测


def chat_state_count() -> int:
    with _state_lock:
        return len(_chat_states)


def raw_buffer_count() -> int:
    with _state_lock:
        return len(_raw_buffers)


def judge_interval_count() -> int:
    with _state_lock:
        return len(_last_judge_at)


__all__ = [
    "HF_DEFAULTS",
    "HF_DIMENSIONS",
    "HF_JSON_REMINDER",
    "HF_JUDGE_MAX_TOKENS",
    "HF_JUDGE_SYSTEM_PROMPT",
    "HF_PENDING_SEND_ABSORB_SEC",
    "HF_PENDING_SEND_TTL_SEC",
    "HF_RAW_BUFFER_MAX",
    "HF_REPEAT_PLACEHOLDER",
    "HF_VISION_DEFAULTS",
    "HfChatState",
    "HfDeliveryPlan",
    "HfGateResult",
    "HfJudgeInput",
    "HfJudgeResult",
    "HfPendingSend",
    "HfRawMessage",
    "begin_pending_send",
    "build_chat_context_summary",
    "build_prompt",
    "chat_state_count",
    "check_gate",
    "clamp_score",
    "default_hf_config",
    "extract_heartflow_json",
    "extract_hf_image_urls",
    "format_raw_messages",
    "get_chat_state",
    "get_raw_buffer",
    "is_hf_group_allowed",
    "judge_heartflow",
    "judge_interval_count",
    "last_bot_reply",
    "mark_heartflow_judged",
    "maybe_engage",
    "note_bot_reply",
    "note_group_message",
    "note_sent",
    "parse_response",
    "pending_send_count",
    "pre_send",
    "raw_buffer_count",
    "raw_messages_to_contexts",
    "record_active_reply",
    "record_passive_message",
    "record_raw_message",
    "reset_heartflow_cache",
    "resolve_hf_effective_budget",
    "resolve_hf_vision_cfg",
    "resolve_hf_weights",
    "seconds_since_last_reply",
    "take_pending_send",
]
