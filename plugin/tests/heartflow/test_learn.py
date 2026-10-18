"""心流学习闭环回归 —— TS ``tests/unit/heartflow-learn.test.mjs`` (199 行) 的 pytest 译本
**+ layer 侧欠下的三笔债**（``test_layer.py`` §7 末尾点名由本文件归还）：

1. ``run_sweep`` 的**步骤顺序**（关窗收敛 < 总开关 < 画像 < 分层 < 占比外环 < 可达性 <
   金丝雀 < ``learning.enabled`` < 调阈）—— 顺序不是实现细节：把画像/可达性挪到
   ``learning.enabled`` 之后，老板关掉自动调阈时会**连观测一起关掉**（两件无关的事）；
   把可达性挪进调阈分支，则"关掉调阈后阈值被手工设到够不着"这条路无人发现。
2. ``resolve_hf_threshold_decision`` 的**七条决策用例**（分层生效 / 影子 / 样本不足 /
   死区 / allowLoosen / band 钳制 / 账号级回落）。
3. ``persist_hf_send_outcome`` 的 sent/suppressed/pending 三态与 **I5**
   （占位符**不得**占预算、**不得**开窗）。

## 本文件最怕"假绿"的四处，各配一条**行为级**回归门

1. **收敛必须精确到行**（v1.10.0 的 ``w.inbound_msg_id``）：``mark_hf_group_engaged`` 传的
   行号必须是**本窗那一行**。传 ``None`` 也能"跑通"，但同群两条 sent 行重叠时一句引用
   会给两行都记 ``engaged=1`` ⇒ 样本凭空翻倍、接话率被稀释。
2. **I5**：``repeat-suppressed`` 若被漏判成 sent，会同时占预算 + 开 600s 观察窗 ⇒
   随后人类的正常发言被记成"接了我那句" ⇒ 毒化学习样本。断言写成"预算/开窗/重复历史
   三者**都没被碰**"，而不是只断分类函数的返回值。
3. **读侧钳制**（v1.6.6）：接话率落死区时 ``eval_hf_threshold`` 直接 return
   ⇒ 库里存量的 0.3 **永不被修好**。所以断言必须同时覆盖"死区不发生变更"**和**
   "读取侧把它钳回 0.5"——少任何一半，这道防线都只存在于注释里。
4. **可达性护栏量的是生效阈值**（v1.10.0）：拿账号级 0.6 去比 0.85 的生效值 ⇒ 永远
   发现不了自锁。断言里让学习值 + 画像 band 都参与，再验天花板真的压到了 judge 用的数上。

真实日期 / 生产数字一律不进本文件（公开仓红线）；样本全是构造的。

## 与 TS 版的对应

=============  ==================================================================
TS 1–15        本文件 §1（``eval_hf_threshold`` 8 条）、§2（v1.6.6 钳制 2 条）、
               §3（``hf_cooldown_ok`` / ``round2`` / ``classify_hf_send`` 5 条）
TS 之外的      §4 可达性护栏 · §5 决策链 · §6 IO 管线 · §7 sweep 顺序 ·
               §8 刷新写侧 · §9 占比外环 · §10 配置面 · §11 源级守卫 · §12 小工具
=============  ==================================================================
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import math
import re
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from plugin.core import metrics
from plugin.data import repo_hf, repo_messages
import plugin.inbound.heartflow as HF_PKG   # 父包本体（属性顶替用，见 _allowed_filter 两条用例）
from plugin.inbound.heartflow import budget as B
from plugin.inbound.heartflow import dedupe as D
from plugin.inbound.heartflow import label as LBL
from plugin.inbound.heartflow import layer as LAY
from plugin.inbound.heartflow import learn as L
from plugin.inbound.heartflow import observe as O
from plugin.inbound.heartflow import profile as P
from plugin.inbound.heartflow.runtime import CACHE_REGISTRY

LEARN_SRC = Path(L.__file__).read_text(encoding="utf-8")
REPO_HF_SRC = Path(repo_hf.__file__).read_text(encoding="utf-8")
REPO_MSG_SRC = Path(repo_messages.__file__).read_text(encoding="utf-8")

#: 本地 20:30（机器 TZ=+8）—— 落在默认四段里的 ``18-24``。
NOW = int(datetime(2026, 9, 20, 20, 30).timestamp())

#: TS fixture ``P`` 的逐字对应（与 ``HF_LEARNING_DEFAULTS`` 同值 ⇒ 起 guard 作用：
#: 代码默认值悄悄漂了，这里就会红）。⚠️ 不叫 ``P``：``P`` 已被 ``profile`` 占走。
TS_PARAMS = {"minSample": 10, "lowEngageRate": 0.15, "highEngageRate": 0.5, "step": 0.05,
     "bandMin": 0.5, "bandMax": 0.9}


def params(**kw) -> dict:
    """``eval_hf_threshold`` 的 params（camelCase —— 与 TS 的 params 同形）。"""
    out = dict(TS_PARAMS)
    out.update(kw)
    return out


def ev(stats: dict, learned=None, base=0.6, **kw) -> L.HfThresholdEval:
    return L.eval_hf_threshold(stats, learned, base, params(**kw))


def _s(signal, engaged, *, judge_overall=None, effective_threshold=None):
    """一条已收敛样本（列名与 ``get_closed_recent`` 的真查询一致）。

    ``sent_at`` 是 B9 起必需的一列：调阈侧按**样本自己那一小时**查本底。缺了它那条
    守卫会静默跳过全部样本（表现 = 阈值再也不动），所以这里默认给上。

    v1.16.2 起真查询还带 ``judge_overall / effective_threshold``（silence-exempt 用）；
    不传时**不出现**这两个键 —— 模拟「旧行/无分」，豁免必须退回旧行为（宁严勿宽）。
    """
    row = {"engaged": 1 if engaged else 0, "closed_at": NOW - 10,
           "engage_signal": signal, "sent_at": NOW - 10}
    if judge_overall is not None:
        row["judge_overall"] = judge_overall
    if effective_threshold is not None:
        row["effective_threshold"] = effective_threshold
    return row


def _seed_learned(account_id: str, group_id: str, v: float) -> None:
    """直接种 learned 阈值 —— 走 :func:`apply_hf_threshold_change` 会打 DB（另有专测）。"""
    L._learned_thresholds[f"{account_id}:{group_id}"] = v


def _seed_profile(account_id: str, group_id: str, *, band: float, text: str = "画像样本") -> None:
    """直接种画像缓存（绕开 JSON 解析 —— 那部分由 ``profile`` 自己的套件钉住）。"""
    P._profiles[f"{account_id}:{group_id}"] = {
        "text": text, "band": band, "budget": None, "generatedAt": NOW, "version": 1}


def _seed_cap(account_id: str, group_id: str, *, cap: float, max_score: float, n: int,
              at_sec: int) -> None:
    """直接种可达性天花板缓存（它唯一的写入口是 sweep 的 ``refresh``，另有专测覆盖）。"""
    L._reach_caps[f"{account_id}:{group_id}"] = L.HfReachCap(
        cap=cap, max_score=max_score, n=n, at_sec=at_sec)


def sweep_run(adapter, account_id: str, now_sec: int) -> None:
    """驱动 async ``run_sweep``（本仓没有 pytest-asyncio ⇒ 一律 ``asyncio.run``）。"""
    asyncio.run(L.run_sweep(adapter, account_id, now_sec))


def cfg_on(**hf) -> dict:
    """``enabled=True`` 的心流配置（可覆盖）。"""
    base = {"enabled": True, "replyThreshold": 0.6}
    base.update(hf)
    return base


def _allow_all(_gid: str) -> bool:
    """白名单哨兵：``is`` 比较用（默认夹具下"不过滤"）。"""
    return True


# ===== 1. eval_hf_threshold（TS 前 8 条 + 边界）=====


def test_threshold_insufficient_sample_keeps_ts_shape():
    """样本不足 ⇒ 不调阈（自然灰度）。9 < minSample 10。"""
    r = ev({"total": 9, "engaged": 1})
    assert (r.changed, r.reason) == (False, "insufficient-sample")
    assert (r.new_threshold, r.direction, r.rate) == (None, None, None)


def test_threshold_dead_zone_is_natural_hysteresis():
    """接话率 0.30 ∈ (0.15, 0.5) ⇒ 死区，天然滞回不跳。"""
    r = ev({"total": 20, "engaged": 6})
    assert (r.changed, r.reason, r.new_threshold) == (False, "in-dead-zone", None)


def test_threshold_up_when_rate_at_or_below_low():
    """0.10 ≤ 0.15 ⇒ 上调（少说精选），0.6 → 0.65。"""
    r = ev({"total": 20, "engaged": 2})
    assert (r.changed, r.direction, r.new_threshold, r.rate) == (True, "up", 0.65, 0.1)


def test_threshold_down_when_rate_at_or_above_high():
    """0.90 ≥ 0.50 ⇒ 下调（更主动），0.6 → 0.55。"""
    r = ev({"total": 20, "engaged": 18})
    assert (r.changed, r.direction, r.new_threshold) == (True, "down", 0.55)


def test_threshold_round2_normalizes_float_step():
    """0.65 + 0.05 的二进制尾巴必须被 round2 收掉（0.7 而不是 0.7000…1）。"""
    r = ev({"total": 20, "engaged": 2}, base=0.65)
    assert r.new_threshold == 0.7
    assert isinstance(r.new_threshold, float), "必须是 float（不是 Decimal/int）"


def test_threshold_learned_overrides_base_and_hits_band_floor():
    """``cur = learned ?? base``：learned 0.8 → 0.75（**不回落** base 0.6）；0.55 → 撞地板 0.5。"""
    down = ev({"total": 20, "engaged": 18}, learned=0.8)
    assert (down.changed, down.direction, down.new_threshold) == (True, "down", 0.75)

    lo = ev({"total": 20, "engaged": 18}, learned=0.55)
    assert (lo.changed, lo.new_threshold) == (True, 0.5), "撞 bandMin 是**实变**（到达硬下界）"


def test_threshold_double_end_clamp_is_noop():
    """两端都撞界 ⇒ clamped-noop（已到硬界还朝外走，不是变更）。"""
    assert ev({"total": 20, "engaged": 1}, learned=0.9).reason == "clamped-noop"
    assert ev({"total": 20, "engaged": 18}, learned=0.5).reason == "clamped-noop"


@pytest.mark.parametrize("total,engaged,expect", [
    (20, 3, "up"),             # 0.15 == low → up（**含等号**）
    (20, 10, "down"),          # 0.50 == high → down（**含等号**）
    (20, 4, "in-dead-zone"),   # 0.20 —— 刚过 low 一点就进死区
    (20, 9, "in-dead-zone"),   # 0.45
])
def test_threshold_rate_boundaries_are_inclusive_like_ts(total, engaged, expect):
    """``rate <= low`` / ``rate >= high`` 的等号方向必须与 TS 一致。

    把 ``<=`` 写成 ``<`` 的后果不是"差一点"：接话率恰在阈值上的群会**永远不动**
    （既不调也不死区），而生产接话率恰恰常落在 0.15 这种整数档上。
    """
    r = ev({"total": total, "engaged": engaged})
    got = "up" if r.direction == "up" else ("down" if r.direction == "down" else r.reason)
    assert got == expect


def test_threshold_reason_carries_rate_total_engaged_with_tofixed_parity():
    """审计 reason 里的 ``rate`` 用 JS ``toFixed(3)`` 口径（``0.1`` 必须打成 ``0.100``）。"""
    assert ev({"total": 20, "engaged": 2}).reason == "rate=0.100 total=20 engaged=2"


# ===== 2. v1.6.6 硬地板（读取侧钳制）=====


def test_clamp_to_band_five_cases():
    assert L.clamp_hf_threshold_to_band(0.3, 0.5, 0.9) == 0.5, "低于地板 → 抬起"
    assert L.clamp_hf_threshold_to_band(0.95, 0.5, 0.9) == 0.9, "高于上限 → 压下"
    assert L.clamp_hf_threshold_to_band(0.7, 0.5, 0.9) == 0.7, "带内原样"
    assert L.clamp_hf_threshold_to_band(0.5, 0.5, 0.9) == 0.5, "恰在边界 → 不变"
    assert L.clamp_hf_threshold_to_band(0.9, 0.5, 0.9) == 0.9


def test_dead_zone_never_repairs_stored_value_so_read_side_must_clamp():
    """**只改 bandMin 挡不住**：死区 ⇒ 不变更 ⇒ 库里存量的 0.3 永不被修好。

    这就是 :func:`resolve_threshold_override` 必须再钳一次的**全部理由**；
    两半断言缺一，这道防线就只活在注释里。
    """
    r = ev({"total": 20, "engaged": 6}, learned=0.3)          # 0.30 ∈ 死区
    assert (r.changed, r.reason) == (False, "in-dead-zone"), "死区不触发变更"
    assert L.clamp_hf_threshold_to_band(0.3, TS_PARAMS["bandMin"], TS_PARAMS["bandMax"]) == 0.5, "读时钳回地板"


# ===== 3. 冷却 / round2 / 发送分类 =====


def test_cooldown_none_means_allow():
    assert L.hf_cooldown_ok(None, 1000, 100) is True


def test_cooldown_boundary_is_inclusive():
    assert L.hf_cooldown_ok(900, 1000, 100) is True, "差 100 == 冷却 → 允许（临界）"
    assert L.hf_cooldown_ok(950, 1000, 100) is False, "差 50 < 100 → 拒绝"
    assert L.hf_cooldown_ok(800, 1000, 100) is True, "差 200 ≥ 100 → 允许"


def test_cooldown_dirty_value_allows_not_blocks():
    """脏值时**放行**。反着来（当"刚改过"锁死）就是静默停摆，正是心流最怕的形态。"""
    assert L.hf_cooldown_ok("abc", 1000, 100) is True
    assert L.hf_cooldown_ok(float("nan"), 1000, 100) is True


@pytest.mark.parametrize("n,expect", [
    (0.075, 0.08), (0.0749, 0.07), (0.7000000000000001, 0.7), (0.3, 0.3),
    (0.125, 0.13),   # ← 银行家舍入会在这里给出 0.12（JS Math.round 给 0.13）
    (0.105, 0.11),   # ← 同上
    (-0.075, -0.07),  # JS Math.round 对负数是"往 +∞ 取"（floor(-7.5+0.5) = -7）
])
def test_round2_is_half_up_not_bankers(n, expect):
    """``round2`` 必须实现 JS ``Math.round``（half-up）而不是 Python ``round``（半偶）。"""
    assert L.round2(n) == expect


def test_classify_pending_when_not_ok():
    assert L.classify_hf_send({"ok": False, "error": "rate_limit"}) == "pending"


@pytest.mark.parametrize("msg_id", ["dedup-suppressed", "ack-template-dropped",
                                    "repeat-suppressed", ""])
def test_classify_suppressed_placeholders(msg_id):
    """``ok: true`` **不等于**真发：三类占位符 + 空文本都判 suppressed。"""
    assert L.classify_hf_send({"ok": True, "msg_id": msg_id}) == "suppressed"


def test_classify_sent_for_vendor_id_or_missing_id():
    assert L.classify_hf_send({"ok": True, "msg_id": None}) == "sent", "vendor 不回 id 仍是真发"
    assert L.classify_hf_send({"ok": True, "msg_id": "848526155-179"}) == "sent"


def test_classify_accepts_hf_send_result_dataclass():
    """形如 ``HfSendResult`` 的对象（属性访问）与 Mapping 等价。"""
    assert L.classify_hf_send(L.HfSendResult(ok=True, msg_id=None)) == "sent"
    assert L.classify_hf_send(L.HfSendResult(ok=True, msg_id="")) == "suppressed"
    assert L.classify_hf_send(L.HfSendResult(ok=False)) == "pending"


def test_classify_warns_on_platform_send_result_shape_mismatch(caplog):
    """**形状不匹配要吵**：``platform.SendResult`` 的 ``success``/``message_id`` 不是本模块的形状。

    静默的后果：消息发出去了但账本永远是 pending（不占预算、不开窗、学习少一半样本），
    而日志里一句错都没有 —— 这类"看起来正常"正是本仓最怕的。
    """
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        assert L.classify_hf_send({"success": True, "message_id": "848526155-179"}) == "pending"
    assert any("形状不匹配" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        assert L.classify_hf_send({"ok": False, "error": "rate_limit"}) == "pending"
    assert caplog.records == [], "正常的 pending 不该吵 —— 否则真问题会淹在噪声里"


def test_classify_does_not_warn_when_our_own_fields_are_present(caplog):
    """有 ``ok`` 就是**我们的**形状 ⇒ 哪怕夹带 ``success``/``message_id`` 也不吵。

    这条守的是"噪声"这一面：把"看着像 platform.SendResult"当成警告条件（而不是
    "缺我们的字段"）会让每条正常发送都打一条 warning —— 真问题就淹在里面了。
    """
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        assert L.classify_hf_send(L.HfSendResult(ok=True, msg_id="848526155-179")) == "sent"
        assert L.classify_hf_send({"ok": True, "msg_id": "x", "success": True,
                                   "message_id": "y"}) == "sent"
        # 半个我们的形状（有 msg_id、缺 ok）：仍算**我们的家族** ⇒ 不吵，只按 pending 处理。
        #   判"是不是别人的形状"看的是**有没有我们的字段**，不是"有没有别人的字段" ——
        #   反过来判的话，任何夹带 success 的半成品都会刷警告。
        assert L.classify_hf_send({"msg_id": "x", "success": True}) == "pending"
    assert caplog.records == []


def test_placeholder_list_is_exactly_three():
    """值域**一处定义**：加第五态必须同时改这里与原因映射（漏了就是静默判 sent）。"""
    assert L.HF_PLACEHOLDER_MSG_IDS == ("dedup-suppressed", "ack-template-dropped",
                                        "repeat-suppressed")
    for name in ("sent", "suppressed", "pending"):
        assert getattr(L, f"HF_SEND_OUTCOME_{name.upper()}") == name


# ===== 4. v1.10.0 可达性天花板（纯函数）=====


def test_reach_cap_needs_min_sample():
    """有效样本 < max(1, minSample) ⇒ 不设天花板（数据不够就别下结论）。"""
    assert L.hf_reachability_cap([0.4] * 19, 0.7, params(minSample=20)) is None
    assert L.hf_reachability_cap([], 0.7, params(minSample=20)) is None


def test_reach_cap_none_when_already_reachable():
    """最高分够得着 ⇒ 阈值可达 ⇒ 不需要护栏。"""
    assert L.hf_reachability_cap([0.5] * 30 + [0.75], 0.7, params()) is None


def test_reach_cap_none_when_it_would_not_bite():
    """算出的天花板 ≥ 原阈值 ⇒ 不起作用（"刚好差一点"是正常滞回，不是自锁）。"""
    assert L.hf_reachability_cap([0.68] * 30, 0.7, params()) is None      # 0.73 ≥ 0.7


def test_reach_cap_uses_band_min_as_hard_floor():
    """``maxScore + step`` 低于老板定的质量底线 0.5 ⇒ 抬到 0.5（护栏不许把阈值压进垃圾分域）。"""
    r = L.hf_reachability_cap([0.18, 0.39] + [0.2] * 18, 0.8, params())
    assert r is not None and r["cap"] == 0.5 and r["maxScore"] == 0.39


def test_reach_cap_returns_evidence_and_skips_dirty_scores():
    r = L.hf_reachability_cap([0.5] * 25 + [0.61], 0.8, params())
    assert r == {"cap": 0.66, "maxScore": 0.61, "n": 26}
    assert L.hf_reachability_cap(["x", None, 0.6, True] * 10, 0.8, params())["n"] == 10, \
        "非数/布尔不算样本（True 不许当 1）"


# ===== 5. 决策链（分层 / 影子 / 回落）=====


def test_layer_step_preview_up_from_anchor():
    r = L.hf_layer_step(0.6, 100, 5, None)          # rate 0.05 ≤ 0.15 → +step
    assert (r.threshold, r.changed) == (0.65, True)


def test_layer_step_is_tighten_only_by_default():
    """``allowLoosen=False`` ⇒ 只许收紧：接话率很高（想下调）也**不许**放宽。"""
    r = L.hf_layer_step(0.6, 100, 90, None)         # rate 0.9 → 想下调到 0.55
    assert (r.threshold, r.changed) == (0.6, False), "默认只许变高"


def test_layer_step_can_loosen_when_explicitly_allowed():
    r = L.hf_layer_step(0.6, 100, 90, {"layered": {"allowLoosen": True}})
    assert (r.threshold, r.changed) == (0.55, True)


def test_layer_step_holds_anchor_below_min_samples():
    """段内样本不够 ⇒ ``eval_hf_threshold`` 自带 insufficient-sample ⇒ 原样返回锚点。"""
    r = L.hf_layer_step(0.6, 29, 1, None)           # minSamples 默认 30
    assert (r.threshold, r.changed) == (0.6, False)


def test_anchor_prefers_learned_then_clamps_to_band():
    """锚点 = 群级 learned（无则账号级），且**读取侧就钳**（库里可能存着 0.3）。"""
    assert L.hf_layer_anchor_for("acct", "gX", None) == 0.6, "无 learned ⇒ 账号级"
    _seed_learned("acct", "gX", 0.3)
    assert L.hf_layer_anchor_for("acct", "gX", None) == 0.5, "0.3 被钳回地板"


@pytest.fixture
def layer_rows(monkeypatch):
    """把一批段统计灌进 layer 的读侧缓存（走官方预热路径）。"""
    def _put(rows):
        monkeypatch.setattr(repo_hf, "list_layer_stats",
                            lambda account_id, layer_kind=None: rows)
        return LAY.load_hf_layer_stats("acct")
    return _put


def _row(gid="gA", key="18-24", n=40, engaged=2):
    return dict(account_id="acct", group_id=gid, layer_kind="daypart", layer_key=key,
                n=n, engaged=engaged, ambient_p=None, window_start=NOW - 86400)


def test_decision_layer_when_applied(layer_rows):
    assert layer_rows([_row(n=40, engaged=2)]) == 1
    d = L.resolve_hf_threshold_decision("acct", "gA", {"layered": {"apply": True}}, NOW)
    assert (d.applied, d.source, d.apply) == (0.65, "layer", True)
    assert (d.layer_key, d.layer_n, d.layer_engaged) == ("18-24", 40, 2)
    assert d.layer_rate == 0.05 and d.shadow is None


def test_decision_shadow_when_apply_off(layer_rows):
    """``apply=False``（默认）⇒ 照算照记但不生效：applied 回落，影子给出"若生效会是多少"。"""
    layer_rows([_row(n=40, engaged=2)])
    d = L.resolve_hf_threshold_decision("acct", "gA", None, NOW)
    assert (d.applied, d.source, d.apply) == (None, "account", False)
    assert (d.shadow.threshold, d.shadow.layer_key, d.shadow.n, d.shadow.engaged) == \
        (0.65, "18-24", 40, 2)
    assert d.shadow.rate == 0.05


def test_decision_falls_back_to_group_learned(layer_rows):
    """段内样本不足 ⇒ 回落群级 learned（这就是"不够就回落先验"）。"""
    layer_rows([_row(n=5, engaged=0)])
    _seed_learned("acct", "gA", 0.75)
    d = L.resolve_hf_threshold_decision("acct", "gA", {"layered": {"apply": True}}, NOW)
    assert (d.applied, d.source, d.apply) == (0.75, "group", True)
    assert d.shadow is None, "样本不足 ⇒ 连影子都没有"


def test_decision_dead_zone_keeps_group_value(layer_rows):
    """段内落死区 ⇒ ``hf_layer_step`` 不变 ⇒ 不是"分层生效"，回落群级值。"""
    layer_rows([_row(n=40, engaged=12)])            # 0.30 ∈ 死区
    _seed_learned("acct", "gA", 0.7)
    d = L.resolve_hf_threshold_decision("acct", "gA", {"layered": {"apply": True}}, NOW)
    assert (d.applied, d.source) == (0.7, "group")
    assert d.shadow is None, "死区不产生影子（没有「若生效」可言）"


def test_decision_layer_disabled_skips_lookup(layer_rows, monkeypatch):
    layer_rows([_row()])
    monkeypatch.setattr(LAY, "get_hf_layer_stat",
                        lambda *a, **kw: pytest.fail("layered.enabled=false 不该查分段"))
    d = L.resolve_hf_threshold_decision(
        "acct", "gA", {"layered": {"enabled": False, "apply": True}}, NOW)
    assert (d.applied, d.layer_key, d.layer_n) == (None, None, 0)
    assert d.layer_rate is None


def test_decision_reports_layer_key_even_without_hit(layer_rows):
    """没有该群的段统计时，``layer_key`` 仍报"当前时刻该在的段"（``/heartflow why`` 要用）。"""
    layer_rows([])
    d = L.resolve_hf_threshold_decision("acct", "gA", None, NOW)
    assert (d.layer_key, d.layer_n, d.layer_rate) == ("18-24", 0, None)


def test_override_none_when_equal_to_base():
    """与账号级基线相等 ⇒ 返回 ``None``（调用方沿用原 cfg，不做无意义的 clone）。"""
    assert L.resolve_threshold_override("acct", "gA", None, NOW) is None
    _seed_learned("acct", "gA", 0.6)
    assert L.resolve_threshold_override("acct", "gA", None, NOW) is None


def test_override_clamps_stored_value_to_band():
    """v1.6.6 的核心：库里存着地板抬高前落的 0.3 ⇒ 读取侧必须给 0.5，而不是 0.3。"""
    _seed_learned("acct", "gA", 0.3)
    assert L.resolve_threshold_override("acct", "gA", None, NOW) == 0.5


def test_effective_threshold_base_only():
    e = L.resolve_hf_effective_threshold("acct", "gA", None, NOW)
    assert (e.threshold, e.base, e.override, e.band, e.band_capped) == (0.6, 0.6, None, None, False)
    assert (e.reach_cap, e.reach_info) == (None, None)


def test_effective_threshold_profile_raise_is_bounded():
    """画像想说 0.9 ⇒ 只给到 ``anchor + 0.1``，且 ``band_capped=True`` 让"被截住"看得见。"""
    _seed_profile("acct", "gA", band=0.9)
    e = L.resolve_hf_effective_threshold("acct", "gA", None, NOW)
    assert (e.threshold, e.band, e.band_capped) == (0.7, 0.9, True)


def test_effective_threshold_reach_cap_bites_last():
    """**唯一入口**的完整链路：学习值 → 读侧钳制 → 画像有界抬升 → 可达性天花板（最后压）。"""
    _seed_learned("acct", "gA", 0.8)
    _seed_profile("acct", "gA", band=0.95)         # 想要 0.95 → 只给 0.9（仍被截）→ 再钳到 0.9
    _seed_cap("acct", "gA", cap=0.55, max_score=0.5, n=40, at_sec=NOW)
    e = L.resolve_hf_effective_threshold("acct", "gA", None, NOW)
    assert e.override == 0.8 and e.band_capped is True
    assert e.threshold == 0.55, "天花板最后压"
    assert e.reach_cap == 0.55
    assert (e.reach_info.n, e.reach_info.max_score) == (40, 0.5)


def test_effective_threshold_reports_cap_only_when_it_bit():
    """天花板没咬住（生效值本来就低）⇒ ``reach_cap`` 报 ``None``，不要谎报护栏起作用了。"""
    _seed_cap("acct", "gA", cap=0.55, max_score=0.5, n=40, at_sec=NOW)
    _seed_learned("acct", "gA", 0.5)
    e = L.resolve_hf_effective_threshold("acct", "gA", None, NOW)
    assert e.threshold == 0.5 and e.reach_cap is None and e.reach_info is None


def test_clamp_to_reachability_reads_memory_only():
    _seed_cap("acct", "gA", cap=0.55, max_score=0.5, n=40, at_sec=NOW)
    assert L.clamp_hf_threshold_to_reachability("acct", "gA", 0.7) == 0.55
    assert L.clamp_hf_threshold_to_reachability("acct", "gA", 0.4) == 0.4, "只取 min，不抬"
    assert L.clamp_hf_threshold_to_reachability("acct", "gB", 0.7) == 0.7, "无天花板 ⇒ 原值"


# ===== 6. IO 薄管线（假数据层）=====


class FakeRepo:
    """假数据层：记调用、可注入返回值与异常。**签名按位置固定**（错位会立刻炸）。"""

    def __init__(self, trace=None):
        self.trace = trace if trace is not None else []
        self.inserts: list[dict] = []
        self.sent: list[tuple] = []
        self.suppressed: list[tuple] = []
        self.engaged: list[tuple] = []
        self.closed: list[tuple] = []
        self.expired: list[tuple] = []
        self.upserts: list[dict] = []
        self.audits: list[tuple] = []
        self.sent_count_calls: list[tuple] = []
        self.scores_calls: list[tuple] = []
        self.closed_recent_calls: list[tuple] = []
        self.group_states: list[dict] = []
        self.closed_groups: list[str] = []
        self.samples_by_group: dict[str, list[dict]] = {}
        self.scores: list[dict] = []
        self.seed_rows: list[dict] = []
        self.last_sent = None
        self.raise_on: set[str] = set()

    def _hit(self, name):
        self.trace.append(name)
        if name in self.raise_on:
            raise RuntimeError(f"boom-{name}")

    # ---- 管线 ----
    def insert_judged(self, account_id, inbound_msg_id, group_id, judged_at, **kw):
        self._hit("insert_judged")
        self.inserts.append(dict(account_id=account_id, inbound_msg_id=inbound_msg_id,
                                 group_id=group_id, judged_at=judged_at, **kw))
        return 1

    def set_sent(self, account_id, inbound_msg_id, sent_at, window_expires_at, bot_msg_id=None):
        self._hit("set_sent")
        self.sent.append((account_id, inbound_msg_id, sent_at, window_expires_at, bot_msg_id))
        return 1

    def set_suppressed(self, account_id, inbound_msg_id, reason, at_sec):
        self._hit("set_suppressed")
        self.suppressed.append((account_id, inbound_msg_id, reason, at_sec))
        return 1

    def mark_engaged(self, account_id, group_id, at_sec, engaged, signal, close,
                     inbound_msg_id=None):
        self._hit("mark_engaged")
        self.engaged.append((account_id, group_id, at_sec, engaged, signal, close,
                             inbound_msg_id))
        return 1

    def list_group_states(self, account_id):
        self._hit("list_group_states")
        return list(self.group_states)

    def list_sent_counts_recent(self, account_id, hour_since_sec, day_since_sec):
        self._hit("list_sent_counts_recent")
        self.sent_count_calls.append((account_id, hour_since_sec, day_since_sec))
        return list(self.seed_rows)

    def get_group_state(self, account_id, group_id):
        self._hit("get_group_state")
        for r in self.group_states:
            if r.get("group_id") == group_id:
                return r
        return None

    def upsert_group_state(self, account_id, group_id, *, learned_threshold=None,
                           last_change_at=None, last_change_old=None, last_change_new=None,
                           last_change_reason=None):
        self._hit("upsert_group_state")
        self.upserts.append(dict(account_id=account_id, group_id=group_id,
                                 learned_threshold=learned_threshold,
                                 last_change_at=last_change_at, last_change_old=last_change_old,
                                 last_change_new=last_change_new,
                                 last_change_reason=last_change_reason))
        return 1

    def log_threshold_change(self, account_id, group_id, new_threshold, sample_total,
                             sample_engaged, *, old_threshold=None, reason=None):
        self._hit("log_threshold_change")
        self.audits.append((account_id, group_id, new_threshold, sample_total, sample_engaged,
                            old_threshold, reason))
        return 1

    # ---- sweep ----
    def close_expired_windows(self, account_id, at_sec):
        self._hit("close_expired_windows")
        self.closed.append((account_id, at_sec))
        return {"closed": 0}

    def expire_stale_judged(self, account_id, at_sec, judged_before_sec):
        self._hit("expire_stale_judged")
        self.expired.append((account_id, at_sec, judged_before_sec))
        return 0

    def get_last_sent_at(self, account_id):
        self._hit("get_last_sent_at")
        return self.last_sent

    def list_distinct_closed_groups(self, account_id, since_sec):
        self._hit("list_distinct_closed_groups")
        return list(self.closed_groups)

    def get_closed_recent(self, account_id, group_id, limit=200):
        self._hit("get_closed_recent")
        self.closed_recent_calls.append((account_id, group_id, limit))
        return list(self.samples_by_group.get(group_id, []))[:limit]

    def list_recent_judged_scores(self, account_id, since_sec, row_cap=2000):
        self._hit("list_recent_judged_scores")
        self.scores_calls.append((account_id, since_sec, row_cap))
        return list(self.scores)


@pytest.fixture
def repo(monkeypatch):
    f = FakeRepo()
    for name in ("insert_judged", "set_sent", "set_suppressed", "mark_engaged",
                 "close_expired_windows", "expire_stale_judged", "get_last_sent_at",
                 "list_distinct_closed_groups", "get_closed_recent", "list_group_states",
                 "list_sent_counts_recent", "get_group_state", "upsert_group_state",
                 "log_threshold_change", "list_recent_judged_scores"):
        monkeypatch.setattr(repo_hf, name, getattr(f, name))
    return f


def _rec():
    return L.HfLedgerRecord(account_id="acct", inbound_msg_id="m1", group_id="gA",
                            judged_at=NOW, new_msg_id="n1", from_wxid="wxid_x",
                            msg_type="text", content_head="问价", judge_overall=0.81,
                            dim_r=0.8, dim_w=0.8, dim_s=0.8, dim_t=0.8, dim_c=0.8,
                            effective_threshold=0.6, energy=0.5)


def _outcome(result, **kw):
    opts = dict(group_id="gA", observe_sec=600, label_window_sec=60)
    opts.update(kw)
    return L.persist_hf_send_outcome("acct", "m1", result, NOW, **opts)


def test_persist_judged_inserts_with_all_columns(repo):
    L.persist_hf_judged(_rec())
    assert repo.inserts[0]["inbound_msg_id"] == "m1"
    assert repo.inserts[0]["judge_overall"] == 0.81
    assert repo.inserts[0]["effective_threshold"] == 0.6
    assert set(repo.inserts[0]) == {"account_id", "inbound_msg_id", "group_id", "judged_at",
                                    "new_msg_id", "from_wxid", "msg_type", "content_head",
                                    "judge_overall", "dim_r", "dim_w", "dim_s", "dim_t",
                                    "dim_c", "effective_threshold", "energy"}


def test_persist_judged_is_non_fatal(repo, caplog):
    """台账写失败**不许**阻断 dispatch（消息该回还得回）。"""
    repo.raise_on = {"insert_judged"}
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        L.persist_hf_judged(_rec())
    assert any("non-fatal" in r.message for r in caplog.records)


def test_persist_below_threshold_inserts_then_suppresses(repo):
    """judge 跑了但没过 ⇒ **也要留痕**（否则与"群里压根没消息"外观完全一致）。"""
    L.persist_hf_judged_below_threshold(_rec(), NOW)
    assert repo.trace == ["insert_judged", "set_suppressed"]
    assert repo.suppressed == [("acct", "m1", "below-threshold", NOW)]


def test_apply_threshold_change_writes_db_audit_and_cache(repo):
    """一次变更 = 群状态 + 审计行 + **内存缓存**（三件一起，缺一则读侧读到旧值）。"""
    L.apply_hf_threshold_change("acct", "gA", 0.6, 0.65, "rate=0.100 total=20 engaged=2",
                                20, 2, NOW)
    assert repo.upserts == [dict(account_id="acct", group_id="gA", learned_threshold=0.65,
                                 last_change_at=NOW, last_change_old=0.6,
                                 last_change_new=0.65,
                                 last_change_reason="rate=0.100 total=20 engaged=2")]
    assert repo.audits == [("acct", "gA", 0.65, 20, 2, 0.6, "rate=0.100 total=20 engaged=2")]
    assert L.get_learned_threshold("acct", "gA") == 0.65


def test_send_outcome_sent_records_everything_once(repo, monkeypatch):
    """sent = 落库 + 开窗 + 占预算 + 进重复历史，四件事**同一条件同一时刻**。"""
    budget_calls, dedupe_calls = [], []
    monkeypatch.setattr(B, "note_reply_sent", lambda *a: budget_calls.append(a))
    monkeypatch.setattr(D, "note_recent_reply", lambda *a: dedupe_calls.append(a))
    before = metrics.get_counter(metrics.COUNTER_HF_SENDS)

    _outcome({"ok": True, "msg_id": "848526155-179"}, text="回一句长的真心话样本")
    assert repo.sent == [("acct", "m1", NOW, NOW + 600, "848526155-179")]
    assert budget_calls == [("acct", "gA", NOW)]
    assert dedupe_calls[0][:4] == ("acct", "gA", "回一句长的真心话样本", NOW)
    assert isinstance(dedupe_calls[0][4], D.HfDedupeConfig)
    assert L.get_open_hf_window("acct", "gA") == L.HfOpenWindow(
        inbound_msg_id="m1", bot_msg_id="848526155-179", sent_at_sec=NOW,
        observe_window_sec=600, label_window_sec=60)
    assert metrics.get_counter(metrics.COUNTER_HF_SENDS) == before + 1


def test_send_outcome_sent_without_text_skips_dedupe(repo, monkeypatch):
    """没有文本可记（非文本回复）⇒ 不碰重复闸（拿空串进历史会拦掉下一句）。"""
    monkeypatch.setattr(D, "note_recent_reply",
                        lambda *a: pytest.fail("无文本时不该进重复历史"))
    _outcome({"ok": True, "msg_id": None})
    assert L.get_open_hf_window("acct", "gA").bot_msg_id is None


def test_send_outcome_uses_resolved_dedupe_defaults(repo, monkeypatch):
    """不传 ``dedupe_cfg`` ⇒ 用 ``resolve_dedupe_cfg(None)``，逐字段等于原始缺省表。

    TS 传的是**未解析**的 ``HF_DEDUPE_DEFAULTS`` 常量，Py 传的是 dataclass —— 这条钉住
    "两者等价"，否则重复闸的默认档会在移植时悄悄变松。
    """
    got = []
    monkeypatch.setattr(D, "note_recent_reply", lambda *a: got.append(a[4]))
    _outcome({"ok": True, "msg_id": "x"}, text="一段足够长的文本内容")
    assert got[0].as_dict() == D.HF_DEDUPE_DEFAULTS


@pytest.mark.parametrize("msg_id,reason", [
    ("ack-template-dropped", "ack-template"),
    ("dedup-suppressed", "dedup"),
    ("repeat-suppressed", "repeat"),
    ("", "empty-text"),
])
def test_send_outcome_suppressed_reason_map(repo, msg_id, reason):
    """落库原因必须**区分**是哪一类拦下的（都写成 ``repeat`` 就没法归因了）。"""
    _outcome({"ok": True, "msg_id": msg_id})
    assert repo.suppressed == [("acct", "m1", reason, NOW)]


def test_reason_map_covers_the_classifier_s_whole_domain():
    """值域**穷尽性**：``classify_hf_send`` 判 suppressed 的两个入口 ——
    占位符（HF_PLACEHOLDER_MSG_IDS 三条）与空文本 —— 都必须有专属原因，且**逐字**对上。

    这条不是装饰：以后往分类器里加第四类占位符（比如"预算拦下"）却忘了加原因，
    现象是它**落库成 ``unknown``**（数字还在，归因没了）—— 用这段映射直接钉死。
    """
    body = _fn_code("persist_hf_send_outcome")
    for msg_id in L.HF_PLACEHOLDER_MSG_IDS:
        # ``ast.unparse`` 会把字符串统一成单引号 —— 断言按它归一化后的形态写
        assert f"msg_id == '{msg_id}'" in body, f"{msg_id} 没有专属原因分支"
    assert "elif msg_id == ''" in body, "空文本必须与三类占位符分开（合成一类就归不了因）"
    assert "reason = 'unknown'" in body, "兜底必须存在：宁可落 unknown 也不许静默丢弃"

    # ``unknown`` 在**构造上**够不到（非占位符非空 → 判 sent）—— 所以它只能靠上面那条
    # 源级断言守着，而不是靠喂一个"未知占位符"进来（那会走成 sent，断言必红）。
    assert L.classify_hf_send({"ok": True, "msg_id": "未知占位符"}) == "sent"


def test_send_outcome_pending_touches_nothing(repo, monkeypatch):
    """pending = 等框架重试 ⇒ **什么都不改**（改了就等于替重试做了决定）。"""
    monkeypatch.setattr(B, "note_reply_sent", lambda *a: pytest.fail("pending 不占预算"))
    monkeypatch.setattr(D, "note_recent_reply", lambda *a: pytest.fail("pending 不进历史"))
    _outcome({"ok": False, "error": "rate_limit"}, text="一段足够长的文本内容")
    assert repo.sent == [] and repo.suppressed == []
    assert L.get_open_hf_window("acct", "gA") is None


@pytest.mark.parametrize("msg_id", ["repeat-suppressed", "dedup-suppressed",
                                    "ack-template-dropped", ""])
def test_send_outcome_i5_placeholder_consumes_no_budget_and_opens_no_window(
        repo, monkeypatch, msg_id):
    """**I5 的落点**：占位符（``ok=True`` 但没真发）不得占预算、不得开窗、不得进重复历史。

    漏判成 sent 的后果是复合的：① 预算被无言消耗 ② 开 600s 观察窗 ⇒ 随后人类的正常
    发言被记成"接了我那句" ⇒ **毒化学习样本**（闭环学的是没发生过的事）。
    """
    budget_calls, dedupe_calls = [], []
    monkeypatch.setattr(B, "note_reply_sent", lambda *a: budget_calls.append(a))
    monkeypatch.setattr(D, "note_recent_reply", lambda *a: dedupe_calls.append(a))
    before = metrics.get_counter(metrics.COUNTER_HF_SENDS)

    _outcome({"ok": True, "msg_id": msg_id}, text="一段足够长的文本内容")
    assert budget_calls == [] and dedupe_calls == []
    assert L.get_open_hf_window("acct", "gA") is None
    assert metrics.get_counter(metrics.COUNTER_HF_SENDS) == before, "真发计数不许涨"


def test_send_outcome_window_overlap_is_visible_in_debug_log(repo, caplog):
    """同群在观察窗内发了第二条 ⇒ 旧窗按 silence 收敛，**但要留痕**（样本可信度风险点）。"""
    _outcome({"ok": True, "msg_id": "a1"})
    with caplog.at_level(logging.DEBUG, logger=L.__name__):
        L.persist_hf_send_outcome("acct", "m2", {"ok": True, "msg_id": "a2"}, NOW + 60,
                                  group_id="gA", observe_sec=600, label_window_sec=60)
    assert any("window overlap" in r.message for r in caplog.records)
    assert L.get_open_hf_window("acct", "gA").inbound_msg_id == "m2", "新窗顶掉旧窗"


def test_send_outcome_failure_is_non_fatal(repo, caplog):
    """记账炸了也不许影响**已经发出去**的那条（它撤不回来）。"""
    repo.raise_on = {"set_sent"}
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        _outcome({"ok": True, "msg_id": "a1"})
    assert any("non-fatal" in r.message for r in caplog.records)


# ======================================================== B4 记账原子性（2026-10-02）


def test_send_outcome_books_budget_and_history_even_when_the_ledger_explodes(
        repo, monkeypatch, caplog):
    """**B4 的核心**：账本（DB）炸了，预算与复读历史**照记**。

    旧写法把五条语句塞进同一个 ``try`` ⇒ ``set_sent`` 一抛，后面三条全跳过：消息已经
    发到群里（撤不回），却不占额度、不进复读历史 —— 而这两样是**内存里的护栏**，和库
    在不在没有半点关系。表现是"台账看着像没发，实际发了"。
    """
    repo.raise_on = {"set_sent"}
    budget_calls, dedupe_calls = [], []
    monkeypatch.setattr(B, "note_reply_sent", lambda *a: budget_calls.append(a))
    monkeypatch.setattr(D, "note_recent_reply", lambda *a: dedupe_calls.append(a))
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        _outcome({"ok": True, "msg_id": "a1"}, text="一段足够长的文本内容")

    assert len(budget_calls) == 1, "账本失败不该吞掉预算记账（否则等于给刷额度开后门）"
    assert len(dedupe_calls) == 1, "账本失败不该吞掉复读历史（否则下一句照抄没人拦）"
    assert any("non-fatal" in r.message for r in caplog.records)


def test_send_outcome_ledger_guard_miss_is_not_silent(repo, monkeypatch, caplog):
    """``set_sent`` 的 guard 不匹配时**返回 0 而不是抛** —— 那一行必须自己喊出来。

    guard 只认 ``judged``（或从未发过的 ``suppressed``），所以行数为 0 只能说明
    "这行根本不在可推进状态"（多半是 ``persist_hf_judged`` 那次 INSERT 没落上）。
    旧写法不看返回值：消息发出去了、账本里**零痕迹**、日志里**零行**。
    """
    monkeypatch.setattr(repo_hf, "set_sent", lambda *a, **k: 0)
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        _outcome({"ok": True, "msg_id": "a1"})
    assert any("受影响行数 0" in r.message for r in caplog.records), \
        "行数为 0 必须留痕（否则整条消息在账本与日志里同时消失）"
    assert any("non-fatal" in r.message for r in caplog.records)


def test_send_outcome_only_opens_a_window_when_the_ledger_row_is_sent(
        repo, monkeypatch):
    """账本没进 ``sent`` ⇒ **不开观察窗**。

    窗的唯一消费者 ``repo_hf.mark_engaged`` 带 ``status='sent'`` 条件，账本没写成时
    开窗只是个永远不会被收敛的内存条目（占内存 + 让 ``/heartflow status`` 的窗数虚高）。
    """
    monkeypatch.setattr(repo_hf, "set_sent", lambda *a, **k: 0)
    _outcome({"ok": True, "msg_id": "a1"})
    assert L.get_open_hf_window("acct", "gA") is None, "账本没写成就不许开窗"
    assert repo.sent == [] or repo.sent is not None


def test_send_outcome_opens_no_window_when_the_ledger_write_throws(repo):
    """账本那次写**抛了** ⇒ 同样不许开窗（行数 0 的另一种坏法）。

    两者在旧写法里都是"什么都没发生"：只看 ``set_sent`` 有没有抛，就分不清
    "行推进了"与"DB 抖了一下"。窗的唯一消费者 ``mark_engaged`` 带 ``status='sent'``，
    行没写成时这条窗永远收敛不了，只在内存里占个位、让 status 页的窗数虚高。
    """
    repo.raise_on = {"set_sent"}
    _outcome({"ok": True, "msg_id": "a1"})
    assert L.get_open_hf_window("acct", "gA") is None, "账本抛错就不许开窗"


def test_send_outcome_still_counts_when_a_memory_step_explodes(repo, monkeypatch, caplog):
    """内存里某一步炸 ⇒ 账本那次写**照样算数**，且不许把异常抛给调用方。

    反向的坏法：账本已提交、内存某步抛 ⇒ 整个分支退出，调用方（适配器）只看到一条
    warn 之外的静默 —— 库里说发了、内存里像没发过。
    """
    dedupe_calls = []
    monkeypatch.setattr(B, "note_reply_sent",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(D, "note_recent_reply", lambda *a: dedupe_calls.append(a))
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        _outcome({"ok": True, "msg_id": "a1"}, text="一段足够长的文本内容")

    assert repo.sent, "预算那步炸了不许抹掉已经落下的 set_sent"
    assert len(dedupe_calls) == 1, "预算那步炸了不许连累复读历史（各自独立 try）"
    assert any("预算记账失败" in r.message for r in caplog.records)


def test_send_outcome_memory_steps_are_not_in_the_same_try_as_the_ledger():
    """源码形状：``set_sent`` 与内存三件套**不许共用一个 try**（B4 的机制本身）。

    行为用例只能证明"这次没被吞"，证明不了"以后也不会被吞" —— 把五条语句重新塞回
    一个 ``try`` 里，上面那条行为用例仍然会绿（因为它只喂了 ``set_sent`` 这一种坏法）。
    """
    fn = next(n for n in ast.walk(ast.parse(inspect.getsource(L.persist_hf_send_outcome)))
              if isinstance(n, ast.FunctionDef))
    tries = [t for t in ast.walk(fn) if isinstance(t, ast.Try)]
    sent_try = [t for t in tries if "set_sent" in ast.unparse(t)]
    assert len(sent_try) == 1, \
        "set_sent 必须独占一个 try（套一层外层 try 也算共用 —— 那正是 B4 的老毛病）"
    src = ast.unparse(sent_try[0])
    assert "note_reply_sent" not in src and "note_recent_reply" not in src, \
        "内存记账不许写在 set_sent 的 try 里（DB 一炸就会连它们一起跳过）"
    for call in ("note_reply_sent", "note_recent_reply"):
        holders = [t for t in tries if call in ast.unparse(t)]
        assert len(holders) == 1, f"{call} 必须独占一个 try，实际 {len(holders)} 个"


def test_mark_engaged_no_window_writes_nothing(repo):
    """绝大多数消息属于"我没开窗"⇒ 必须**零 DB 写**（群聊热路径）。"""
    L.mark_hf_group_engaged("acct", "gA", NOW,
                            (LBL.HfEngageCandidate(at_sec=NOW, text="在吗"),))
    assert repo.engaged == [] and repo.trace == []


def test_mark_engaged_strong_signal_closes_and_marks_this_row(repo):
    """引用 bot 那条 ⇒ 强信号，立刻收敛，且行号是**本窗那一行**（v1.10.0 精确到行）。"""
    _outcome({"ok": True, "msg_id": "a1"})
    L.mark_hf_group_engaged("acct", "gA", NOW + 30, (
        LBL.HfEngageCandidate(at_sec=NOW + 30, text="谢谢", quotes_bot=True),))
    assert repo.engaged == [("acct", "gA", NOW + 30, 1, "quote", True, "m1")]
    assert L.get_open_hf_window("acct", "gA") is None, "收敛即关窗"


def test_mark_engaged_weak_signal_keeps_window_open(repo):
    """弱信号（窄窗内有人说话）只落标签、**不关窗** —— 留给更强的信号升级。"""
    _outcome({"ok": True, "msg_id": "a1"})
    L.mark_hf_group_engaged("acct", "gA", NOW + 30, (
        LBL.HfEngageCandidate(at_sec=NOW + 30, text="随便说说"),))
    assert repo.engaged == [("acct", "gA", NOW + 30, 1, "short-window", False, "m1")]
    assert L.get_open_hf_window("acct", "gA") is not None


def test_mark_engaged_negative_marks_zero_and_closes(repo):
    """硬负词（"别刷了"）⇒ 负样本，立刻收敛（这条是**评估**用词表，不是触发器）。"""
    _outcome({"ok": True, "msg_id": "a1"})
    L.mark_hf_group_engaged("acct", "gA", NOW + 30, (
        LBL.HfEngageCandidate(at_sec=NOW + 30, text="别刷了"),))
    assert repo.engaged == [("acct", "gA", NOW + 30, 0, "negative", True, "m1")]


def test_mark_engaged_no_signal_no_write(repo):
    """窗外的消息 ⇒ 无结论 ⇒ 不写（晚到的普通闲聊不该被记成接话）。"""
    _outcome({"ok": True, "msg_id": "a1"})
    L.mark_hf_group_engaged("acct", "gA", NOW + 900, (
        LBL.HfEngageCandidate(at_sec=NOW + 900, text="随便说说"),))
    assert repo.engaged == []
    assert L.get_open_hf_window("acct", "gA") is not None


def test_forget_window_makes_later_quote_ignored(repo):
    """``/heartflow veto`` 后必须删窗：否则随后的引用会把 veto 覆盖成 ``engaged=1``。

    那比老板不点这一下更糟 —— 人工标注被自动判据翻掉，学习闭环学的是反面。
    """
    _outcome({"ok": True, "msg_id": "a1"})
    L.forget_hf_open_window("acct", "gA")
    assert L.get_open_hf_window("acct", "gA") is None
    L.mark_hf_group_engaged("acct", "gA", NOW + 30, (
        LBL.HfEngageCandidate(at_sec=NOW + 30, text="谢谢", quotes_bot=True),))
    assert repo.engaged == [], "窗没了 ⇒ 不得再写"


def test_load_learned_thresholds_reads_rows_and_skips_dirty(repo):
    repo.group_states = [{"group_id": "gA", "learned_threshold": 0.72, "last_change_at": NOW - 100},
                         {"group_id": "gB", "learned_threshold": None},
                         {"group_id": "gC", "learned_threshold": "0.65"}]
    assert L.load_learned_thresholds("acct") == 1, "只收**数值**（Bool 与字符串都不算）"
    assert L.get_learned_threshold("acct", "gA") == 0.72
    assert L.get_learned_threshold("acct", "gB") is None
    assert L.get_learned_threshold("acct", "gC") is None, \
        "JS `Number.isFinite(\"0.65\") === false` ⇒ 字符串列一律不收（Py 侧同义）"


def test_load_learned_thresholds_failure_degrades_without_clearing(repo, caplog):
    """失败 ⇒ 账号以**空缓存**启动（全群回落账号级）+ 一条 warn；不抛、不清已有。"""
    repo.group_states = [{"group_id": "gA", "learned_threshold": 0.72}]
    L.load_learned_thresholds("acct")
    repo.raise_on = {"list_group_states"}
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        assert L.load_learned_thresholds("acct") == 0
    assert any("loadLearnedThresholds" in r.message for r in caplog.records)
    assert L.get_learned_threshold("acct", "gA") == 0.72, "失败不清缓存"


def test_load_budget_seed_uses_local_boundaries(repo, monkeypatch):
    """回填边界必须是**本地**整点/零点（与运行时桶键同口径）—— 否则新小时带余数开局。"""
    seen = {}

    def fake_seed(account_id, rows, now_sec):
        seen.update(account_id=account_id, rows=list(rows), now=now_sec)
        return 3

    monkeypatch.setattr(B, "seed_budget_states", fake_seed)
    repo.seed_rows = [{"group_id": "gA", "hour_count": 2, "day_count": 5,
                       "last_sent_at": NOW - 60}]
    assert L.load_hf_budget_seed("acct", NOW) == 3
    assert repo.sent_count_calls == [("acct", B.hf_hour_start_sec(NOW), B.hf_day_start_sec(NOW))]
    assert seen["now"] == NOW and seen["rows"][0]["hour_count"] == 2


def test_load_budget_seed_failure_returns_zero(repo, caplog):
    repo.raise_on = {"list_sent_counts_recent"}
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        assert L.load_hf_budget_seed("acct", NOW) == 0
    assert any("budget seed failed" in r.message for r in caplog.records)


# ===== 7. sweep：顺序 / 开关 / 隔离 =====


class SweepSpies:
    """把 sweep 的各个 pass 换成记录器，与假数据层**共用同一条 trace**。"""

    def __init__(self, repo: FakeRepo):
        self.repo = repo
        self.trace = repo.trace
        self.profile_calls: list[tuple] = []
        self.layer_calls: list[tuple] = []
        self.share_calls: list[tuple] = []
        self.reach_calls: list[tuple] = []

    async def profile(self, account_id, cfg, now_sec, *, group_filter=None):
        self.trace.append("profile")
        self.profile_calls.append((account_id, cfg, now_sec, group_filter))
        return 0

    def layer(self, account_id, cfg, now_sec, *, label_window_sec, ambient_max, group_filter=None):
        self.trace.append("layer")
        self.layer_calls.append((account_id, now_sec, label_window_sec, ambient_max, group_filter))
        return 0

    def share(self, account_id, cfg, now_sec):
        self.trace.append("share")
        self.share_calls.append((account_id, now_sec))
        return 0

    def reach(self, account_id, cfg, now_sec):
        self.trace.append("reach")
        self.reach_calls.append((account_id, now_sec))
        return 0

    def prune(self, account_id, now_sec):
        self.trace.append("prune_open_windows")

    def ambient(self, account_id, now_sec, label_window_sec):
        self.trace.append("ambient")
        return {}


@pytest.fixture
def sweep(monkeypatch, repo):
    s = SweepSpies(repo)
    monkeypatch.setattr(P, "maybe_generate_hf_group_profiles", s.profile)
    monkeypatch.setattr(LAY, "maybe_recompute_hf_layer_stats", s.layer)
    monkeypatch.setattr(L, "maybe_recompute_hf_share_guard", s.share)
    monkeypatch.setattr(L, "refresh_hf_reachability_caps", s.reach)
    monkeypatch.setattr(L, "prune_open_windows", s.prune)
    monkeypatch.setattr(L, "load_ambient_by_group_hour", s.ambient)
    monkeypatch.setattr(D, "prune_recent_replies",
                        lambda now, cfg: s.trace.append("prune_replies"))
    monkeypatch.setattr(L, "_allowed_filter", lambda cfg: _allow_all)   # 白名单单独测
    return s


class FakeAdapter:
    def __init__(self, hf):
        self._hf = hf
        self.seen: list[str] = []

    def account(self, account_id):
        self.seen.append(account_id)
        return types.SimpleNamespace(heartflow=self._hf)


def test_sweep_order_is_pinned(sweep, repo):
    """**顺序就是规格**（见模块 docstring）—— 这条断言是它唯一的守卫。

    · 关窗/收敛/清窗在最前，且**先于**总开关判定（防 pending 行无限堆积）；
    · 画像 < 分层 < 占比外环 < 可达性，**全部在** ``learning.enabled`` 之前
      （老板关掉自动调阈时，观测与"心流还能不能说话"仍必须继续）；
    · 金丝雀 + 重复历史裁剪之后才是调阈；
    · 调阈内部：取反事实基线 → 逐群取样本 → 取群状态 → 写库 → 审计。
    """
    repo.closed_groups = ["gA"]
    repo.samples_by_group = {"gA": [_s("silence", False)] * 20}
    repo.group_states = [{"group_id": "gA", "learned_threshold": 0.6, "last_change_at": None}]
    sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)

    assert repo.trace == [
        "close_expired_windows", "expire_stale_judged", "prune_open_windows",
        "profile", "layer", "share", "reach",
        "get_last_sent_at", "prune_replies",
        "ambient", "list_distinct_closed_groups", "get_closed_recent", "get_group_state",
        "upsert_group_state", "log_threshold_change",
    ]
    assert repo.expired == [("acct", NOW, NOW - 1800)], "呆账收敛用 staleJudgedMaxSec 往前推"
    assert repo.closed_recent_calls == [("acct", "gA", 20)], \
        "滚窗长度 = sampleWindow（默认 20）—— 写死别的数就是「判定口径与声明不符」"
    assert [c[1] for c in repo.sent_count_calls] == [], "调阈路径不碰预算回填"


def test_sweep_disabled_returns_after_step_one(sweep, repo):
    """心流总开关关着 ⇒ 只做步骤 1（关窗/收敛），连画像都不跑。"""
    sweep_run(FakeAdapter({"enabled": False}), "acct", NOW)
    assert repo.trace == ["close_expired_windows", "expire_stale_judged", "prune_open_windows"]


def test_sweep_step_one_failure_returns_early(sweep, repo, caplog):
    """步骤 1 失败 ⇒ **本轮直接 return**（下一轮补上），不带未收敛的行继续往下走。"""
    repo.raise_on = {"expire_stale_judged"}
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)
    assert repo.trace == ["close_expired_windows", "expire_stale_judged"]
    assert any("close/expire failed" in r.message for r in caplog.records)


def test_sweep_learning_disabled_still_runs_observation_passes(sweep, repo):
    """``learning.enabled=false``（默认）⇒ 观测四件套照跑，**只停调阈**。

    把画像/可达性挪进调阈分支，老板关掉自动调阈时会连观测一起关掉 —— 那正是 09-26
    停摆能藏 3 天的原因之一（没人看得见）。
    """
    sweep_run(FakeAdapter(cfg_on()), "acct", NOW)
    assert repo.trace == ["close_expired_windows", "expire_stale_judged",
                          "prune_open_windows", "profile", "layer", "share", "reach",
                          "get_last_sent_at", "prune_replies"]
    assert "list_distinct_closed_groups" not in repo.trace, "不调阈 ⇒ 不取样本"
    assert repo.upserts == []


def test_sweep_passes_the_same_whitelist_filter_to_all_three_passes(sweep, repo):
    """白名单判据**同一个函数对象**传给画像/分层（可达性在 pass 内自取）—— 各建一份必漂移。"""
    sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)
    assert sweep.profile_calls[0][3] is _allow_all
    assert sweep.layer_calls[0][4] is _allow_all
    assert (sweep.layer_calls[0][2], sweep.layer_calls[0][3]) == (60, 0.5), \
        "分层与调阈共用同一份 labelWindowSec/ambientMax（口径不同则 n 永远对不上）"


def test_sweep_adapts_and_logs(sweep, repo, caplog):
    """一轮完整的调阈：20 条里 2 条被接话 ⇒ rate 0.10 ⇒ 0.6 → 0.65 + 审计一行。"""
    detail = ("rate=0.100 total=20 engaged=2 skipped=0 exempt=0 "
              "ambientP=0.000 sig=silence:18,quote:2")
    repo.closed_groups = ["gA"]
    repo.samples_by_group = {"gA": [_s("silence", False)] * 18 + [_s("quote", True)] * 2}
    repo.group_states = [{"group_id": "gA", "learned_threshold": 0.6, "last_change_at": None}]
    with caplog.at_level(logging.INFO, logger=L.__name__):
        sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)

    assert repo.upserts == [dict(account_id="acct", group_id="gA", learned_threshold=0.65,
                                 last_change_at=NOW, last_change_old=0.6, last_change_new=0.65,
                                 last_change_reason=detail)]
    assert repo.audits == [("acct", "gA", 0.65, 20, 2, 0.6, detail)]
    assert L.get_learned_threshold("acct", "gA") == 0.65, "内存缓存同步（否则读侧读到旧值）"
    assert any("threshold adapted" in r.message for r in caplog.records)


def test_sweep_silence_exempt_stops_self_lock_replay(sweep, repo, monkeypatch):
    """v1.16.2 生产实排复现回归门（全员群 10-16/17 两次自锁）。

    17 条 informative silence 的 margin 分布 ``{0.005×2, 0.055×2, ≥0.14×13}``：
    豁免 13 条高置信被无视，只剩 4 条擦边负 < ``minSample``(10) ⇒ **抬阈路径必须死**。
    旧口径（v1.16.1）下这 17 条全负 ⇒ rate 0 ⇒ 每 4h +0.05 单调抬到 0.90 钉死。
    """
    margins = [0.005, 0.005, 0.055, 0.055] + [0.14] * 13
    repo.closed_groups = ["gA"]
    repo.samples_by_group = {"gA": [
        _s("silence", False, judge_overall=0.6 + m, effective_threshold=0.6)
        for m in margins]}
    repo.group_states = [{"group_id": "gA", "learned_threshold": 0.6, "last_change_at": None}]

    seen: dict = {}
    orig = L.eval_hf_threshold

    def spy(stats, *a, **kw):
        seen.update(stats)
        return orig(stats, *a, **kw)

    monkeypatch.setattr(L, "eval_hf_threshold", spy)
    sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)

    assert seen == {"total": 4, "engaged": 0}, \
        "17 条里 13 条高置信被无视已豁免，只剩 4 条擦边负进统计"
    assert repo.upserts == [], "可用样本 4 < minSample 10 ⇒ 不调阈（自锁路径已断）"
    assert repo.audits == []


def test_sweep_exempt_counts_in_audit_and_never_double_counts_as_skipped(sweep, repo, caplog):
    """审计口径：``skipped`` 只数**不可采信**，``exempt`` 单独数 —— 两者不许重叠。"""
    repo.closed_groups = ["gA"]
    repo.samples_by_group = {"gA": (
        [_s("silence", False, judge_overall=0.80, effective_threshold=0.60)] * 5
        + [_s("silence", False, judge_overall=0.62, effective_threshold=0.60)] * 20)}
    repo.group_states = [{"group_id": "gA", "learned_threshold": 0.6, "last_change_at": None}]
    with caplog.at_level(logging.INFO, logger=L.__name__):
        sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)

    assert len(repo.upserts) == 1, "20 条擦边负仍够 minSample ⇒ 照常抬阈（收敛力不动）"
    detail = repo.upserts[0]["last_change_reason"]
    assert "skipped=0" in detail and "exempt=5" in detail
    assert "total=15" in detail, "豁免的 5 条不进统计（样本窗 20 = 5 豁免 + 15 可用）"
    assert L.get_learned_threshold("acct", "gA") == 0.65


def test_sweep_exempt_disabled_restores_old_self_lock(monkeypatch, sweep, repo, caplog):
    """``silenceExemptMarginMin=0`` = 退回 v1.16.1 旧行为（17 条全负照常抬阈）。

    这条是「开关真的关得掉」的证明 —— 否则默认值一改，想回退只能改代码。
    """
    margins = [0.005, 0.005, 0.055, 0.055] + [0.14] * 13
    repo.closed_groups = ["gA"]
    repo.samples_by_group = {"gA": [
        _s("silence", False, judge_overall=0.6 + m, effective_threshold=0.6)
        for m in margins]}
    repo.group_states = [{"group_id": "gA", "learned_threshold": 0.6, "last_change_at": None}]
    with caplog.at_level(logging.INFO, logger=L.__name__):
        sweep_run(FakeAdapter(cfg_on(learning={
            "enabled": True, "silenceExemptMarginMin": 0})), "acct", NOW)

    assert len(repo.upserts) == 1, "旧口径：17 条全负 ⇒ 抬阈（对照实验）"
    assert "exempt=0" in repo.upserts[0]["last_change_reason"]


def test_sweep_skips_when_no_informative_sample(sweep, repo, monkeypatch, caplog):
    """本底太热闹（ambientP ≥ ambientMax）⇒ 弱信号/沉默全部不采信 ⇒ 不调阈。"""
    monkeypatch.setattr(L, "load_ambient_by_group_hour",
                        lambda *a: {f"gA|{h}": 0.9 for h in range(24)})
    repo.closed_groups = ["gA"]
    repo.samples_by_group = {"gA": [_s("short-window", True)] * 20}
    repo.group_states = [{"group_id": "gA", "learned_threshold": 0.6}]
    with caplog.at_level(logging.DEBUG, logger=L.__name__):
        sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)
    assert repo.upserts == []
    assert any("no informative sample" in r.message for r in caplog.records)


def test_ambient_is_read_per_sample_not_once_per_run(sweep, repo, monkeypatch):
    """★ B9 正题：本底按**每条样本自己那一小时**查，不是"当前小时"一份套全群。

    造两批样本落在**不同的本地小时**：冷清时段（本底 0.05 < ambientMax ⇒ 弱信号可采信）
    18 条，热闹时段（本底 0.9 ≥ ambientMax ⇒ 弱信号不可采信）2 条。
    正确口径 = 18 条可采信、``skipped=2``；旧口径两种情形都不对 —— 当前小时恰好是热闹
    那个 ⇒ 20 条全废（本轮不调阈，阈值僵住）；是冷清那个 ⇒ 20 条全收（夜里那批噪声
    当成有效信息混进来，正是 B9 要治的病）。
    """
    cold_at, busy_at = NOW - 3_600, NOW          # 差 3600s = 必然差一个本地小时
    assert B.hf_local_hour(cold_at) != B.hf_local_hour(busy_at), "用例前提：两个不同时段"
    monkeypatch.setattr(L, "load_ambient_by_group_hour", lambda *a: {
        f"gA|{B.hf_local_hour(cold_at)}": 0.05,
        f"gA|{B.hf_local_hour(busy_at)}": 0.9,
    })
    repo.closed_groups = ["gA"]

    def _row(at):
        return {"engaged": 0, "closed_at": at + 600, "engage_signal": "silence", "sent_at": at}

    repo.samples_by_group = {"gA": [_row(cold_at) for _ in range(18)]
                                    + [_row(busy_at) for _ in range(2)]}
    repo.group_states = [{"group_id": "gA", "learned_threshold": 0.6, "last_change_at": None}]
    sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)

    assert len(repo.audits) == 1, "18 条够 minSample ⇒ 该调阈"
    assert repo.audits[0][3] == 18, "只有冷清时段那 18 条可采信"
    assert "skipped=2" in repo.audits[0][6], "热闹时段那 2 条要出现在归因里"


def test_a_sample_without_a_usable_sent_at_is_never_usable(sweep, repo, monkeypatch):
    """★ B9：``sent_at`` 缺/脏 ⇒ 不采信（强信号也救不了）。

    查不到它属于哪个时段，就没法判断这条弱信号算不算信息；拿它当"本底 0"采信更危险 ——
    夜里那批会**全部冒充有效信息**，正是 B9 要治的病。
    """
    monkeypatch.setattr(L, "load_ambient_by_group_hour", lambda *a: {})
    repo.closed_groups = ["gA"]
    good = [{"engaged": 0, "closed_at": NOW - 10, "engage_signal": "silence",
             "sent_at": NOW - 10} for _ in range(10)]
    dirty = [{"engaged": 1, "closed_at": NOW - 10, "engage_signal": "quote", "sent_at": v}
             for v in (None, 0, -5, "1789070400", True)]
    dirty.append({"engaged": 1, "closed_at": NOW - 10, "engage_signal": "quote"})  # 列缺席
    repo.samples_by_group = {"gA": good + dirty}
    repo.group_states = [{"group_id": "gA", "learned_threshold": 0.6, "last_change_at": None}]
    sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)

    assert len(repo.audits) == 1
    assert repo.audits[0][3] == 10, "只有时刻可用的那 10 条算数"
    assert "skipped=6" in repo.audits[0][6], "脏时刻的 6 条一条都不许混进可采信"


def test_sweep_respects_cooldown(sweep, repo):
    """冷却内不许再改（防"每 5 分钟一步"把阈值走飞）。"""
    repo.closed_groups = ["gA"]
    repo.samples_by_group = {"gA": [_s("silence", False)] * 20}
    repo.group_states = [{"group_id": "gA", "learned_threshold": 0.6,
                          "last_change_at": NOW - 10}]
    sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)
    assert repo.upserts == [] and repo.audits == []


def test_sweep_whitelist_is_enforced_before_fetching_samples(sweep, repo, monkeypatch):
    """白名单外**根本不取样本**（省一条查询，也断了把别群样本算进来的可能）。"""
    monkeypatch.setattr(L, "_allowed_filter", lambda cfg: (lambda gid: gid == "gYes"))
    repo.closed_groups = ["gNo"]
    repo.samples_by_group = {"gNo": [_s("silence", False)] * 20}
    sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)
    assert "get_closed_recent" not in repo.trace


def test_sweep_adaptation_failure_is_isolated(sweep, repo, caplog):
    """调阈段炸了 ⇒ 只 warn（前面四件套的结果已经落好了）。"""
    repo.closed_groups = ["gA"]
    repo.samples_by_group = {"gA": [_s("silence", False)] * 20}
    repo.raise_on = {"upsert_group_state"}
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        sweep_run(FakeAdapter(cfg_on(learning={"enabled": True})), "acct", NOW)
    assert any("sweep adapt failed" in r.message for r in caplog.records)


def test_sweep_isolation_between_passes(sweep, repo, monkeypatch, caplog):
    """任一个 pass 抛错都不许连累其余（v1.6.8 起的一贯口径）。"""
    async def boom(*a, **kw):
        raise RuntimeError("boom-profile")

    monkeypatch.setattr(P, "maybe_generate_hf_group_profiles", boom)
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        sweep_run(FakeAdapter(cfg_on()), "acct", NOW)
    assert "layer" in repo.trace and "reach" in repo.trace, "后续 pass 照跑"
    assert any("profile pass failed" in r.message for r in caplog.records)


def test_sweep_canary_sets_minus_one_when_never_sent(sweep, repo):
    """从未发过 ⇒ ``-1``（``0`` 会被读成"刚发过" —— 金丝雀瞬间失明）。"""
    repo.last_sent = None
    sweep_run(FakeAdapter(cfg_on()), "acct", NOW)
    assert metrics.get_gauge(metrics.GAUGE_HF_LAST_SEND_AGE_SEC) == -1.0


def test_sweep_canary_uses_age_on_success(sweep, repo):
    repo.last_sent = NOW - 120
    sweep_run(FakeAdapter(cfg_on()), "acct", NOW)
    assert metrics.get_gauge(metrics.GAUGE_HF_LAST_SEND_AGE_SEC) == 120.0


def test_sweep_canary_clamps_negative_on_clock_skew(sweep, repo):
    """库里 ``sent_at`` 比当前时刻还晚（时钟回拨 / 跨机时钟差）⇒ 记 0 而不是负数。

    负的"距上次发言秒数"在告警规则里（``> N``）恒不触发 ⇒ 恰恰是停摆时最容易出现的
    那种数：发出去那一刻进程时钟还是对的，之后回拨。所以负数必须夹掉。
    """
    repo.last_sent = NOW + 60
    sweep_run(FakeAdapter(cfg_on()), "acct", NOW)
    assert metrics.get_gauge(metrics.GAUGE_HF_LAST_SEND_AGE_SEC) == 0.0


def test_cfg_for_prefers_adapter_then_accounts_then_empty(monkeypatch):
    """配置来源：运行中的适配器 → ``accounts.resolve`` → 空（只跑步骤 1）。"""
    assert L._cfg_for(FakeAdapter({"enabled": True, "replyThreshold": 0.7}), "acct")[
        "replyThreshold"] == 0.7

    class NoAcct:
        def account(self, account_id):
            raise RuntimeError("no such account")

    monkeypatch.setattr("plugin.core.accounts.resolve",
                        lambda aid: types.SimpleNamespace(heartflow={"enabled": True}))
    assert L._cfg_for(NoAcct(), "acct") == {"enabled": True}

    monkeypatch.setattr("plugin.core.accounts.resolve",
                        lambda aid: (_ for _ in ()).throw(RuntimeError("nope")))
    assert L._cfg_for(None, "acct") == {}, "取不到 ⇒ 空配置（= 什么都不做），不抛"


def test_sweep_cfg_falls_back_to_accounts_resolve(sweep, repo, monkeypatch):
    """适配器取不到配置时回落 ``accounts.resolve`` —— 两条路都必须能跑通 sweep。"""
    monkeypatch.setattr("plugin.core.accounts.resolve",
                        lambda aid: types.SimpleNamespace(heartflow={"enabled": True}))
    sweep_run(None, "acct", NOW)
    assert "profile" in repo.trace


# ===== 8. 可达性刷新的写侧（删除 / 节流 / 指标）=====


def test_refresh_caps_and_deletes_on_recovery(repo, caplog):
    """**删除与保留的转换**：一旦该群样本变得够得着，天花板必须被删掉。

    只加不删的实现会让"曾经够不着"永久压住阈值 —— 护栏自己成了新的自锁。
    """
    repo.scores = [{"group_id": "gA", "judge_overall": 0.5}] * 25
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        assert L.refresh_hf_reachability_caps("acct", cfg_on(), NOW) == 1
    cap = L.get_hf_reachability_cap("acct", "gA")
    assert (cap.cap, cap.max_score, cap.n, cap.at_sec) == (0.55, 0.5, 25, NOW)
    assert L.clamp_hf_threshold_to_reachability("acct", "gA", 0.6) == 0.55
    assert any("unreachable ⇒ capped" in r.message for r in caplog.records)
    assert repo.scores_calls[0] == ("acct", NOW - 7 * 86400, L.HF_REACH_DB_ROW_CAP)

    caplog.clear()
    repo.scores = [{"group_id": "gA", "judge_overall": 0.5}] * 24 + [
        {"group_id": "gA", "judge_overall": 0.8}]
    assert L.refresh_hf_reachability_caps("acct", cfg_on(), NOW + 300) == 0
    assert L.get_hf_reachability_cap("acct", "gA") is None, "够得着了 ⇒ 删天花板"
    assert metrics.get_gauge(metrics.GAUGE_HF_REACHABLE_GROUPS) == 1


def test_refresh_warns_on_first_and_after_a_long_gap(repo, caplog):
    """告警节流：每 300s 一轮不许每轮都吵；但**≥1 小时没跑到过**（插件被停过）要再提醒。

    ⚠️ 照 TS 逐字等价：``at_sec`` 每轮都被重写 ⇒ 常态 300s 节奏下"每小时一次"这一支
    **够不到**，实际只有"首次"与"压住值变化"会吵。这条把这个事实钉住 ——
    免得后人以为"它每小时会提醒我一次"而把节流当成了保险。
    """
    repo.scores = [{"group_id": "gA", "judge_overall": 0.5}] * 25
    before = metrics.get_counter(metrics.COUNTER_HF_REACH_CAP_APPLIED)
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        L.refresh_hf_reachability_caps("acct", cfg_on(), NOW)
        n_first = len([r for r in caplog.records if "unreachable" in r.message])
        L.refresh_hf_reachability_caps("acct", cfg_on(), NOW + 300)
        n_again = len([r for r in caplog.records if "unreachable" in r.message])
    assert (n_first, n_again) == (1, 1), "常态 300s 节奏下不重复吵"
    assert metrics.get_counter(metrics.COUNTER_HF_REACH_CAP_APPLIED) == before + 1

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        L.refresh_hf_reachability_caps("acct", cfg_on(), NOW + 4000)
    assert any("unreachable" in r.message for r in caplog.records), "隔了 > 1h ⇒ 再提醒"


def test_refresh_warns_again_when_cap_value_changes(repo, caplog):
    repo.scores = [{"group_id": "gA", "judge_overall": 0.5}] * 25
    L.refresh_hf_reachability_caps("acct", cfg_on(), NOW)
    caplog.clear()
    repo.scores = [{"group_id": "gA", "judge_overall": 0.52}] * 25    # 天花板变成 0.57
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        L.refresh_hf_reachability_caps("acct", cfg_on(), NOW + 300)
    assert L.get_hf_reachability_cap("acct", "gA").cap == 0.57
    assert any("unreachable" in r.message for r in caplog.records)


def test_refresh_disabled_switch_clears_caps_and_skips_query(repo):
    """``recoverUnreachable=false`` ⇒ 清空该账号全部天花板（开关要真能关）。"""
    repo.scores = [{"group_id": "gA", "judge_overall": 0.5}] * 25
    L.refresh_hf_reachability_caps("acct", cfg_on(), NOW)
    assert L.reach_cap_count() == 1
    assert L.refresh_hf_reachability_caps(
        "acct", cfg_on(learning={"recoverUnreachable": False}), NOW + 300) == 0
    assert L.get_hf_reachability_cap("acct", "gA") is None
    assert repo.scores_calls == [("acct", NOW - 7 * 86400, L.HF_REACH_DB_ROW_CAP)], "关掉不查库"


def test_refresh_ignores_groups_without_sample_or_group_id(repo):
    """样本不足的群既不算 capped 也不算 reachable；没有群号的行直接丢。"""
    repo.scores = ([{"group_id": "gB", "judge_overall": 0.8}] * 5
                   + [{"group_id": "", "judge_overall": 0.2}] * 30)
    assert L.refresh_hf_reachability_caps("acct", cfg_on(), NOW) == 0
    assert metrics.get_gauge(metrics.GAUGE_HF_THRESHOLD_CEILING_GROUPS) == 0.0
    assert metrics.get_gauge(metrics.GAUGE_HF_REACHABLE_GROUPS) == 0.0


def test_refresh_measures_the_effective_threshold_not_the_base(repo):
    """护栏必须量**生效阈值**：拿账号级 0.6 去比 0.85 的生效值就永远发现不了自锁。"""
    _seed_profile("acct", "gA", band=0.9)              # 生效 0.7（有界抬升）
    repo.scores = [{"group_id": "gA", "judge_overall": 0.6}] * 25
    assert L.refresh_hf_reachability_caps("acct", cfg_on(), NOW) == 1
    assert L.get_hf_reachability_cap("acct", "gA").cap == 0.65   # max(0.5, 0.6+0.05)
    assert metrics.get_gauge(metrics.GAUGE_HF_EFFECTIVE_THRESHOLD_MAX) == 0.7
    assert metrics.get_gauge(metrics.GAUGE_HF_THRESHOLD_CEILING_GROUPS) == 1.0, \
        "被压住的群数（恒 0 的话护栏在起作用也看不出来）"


def test_refresh_whitelist_is_enforced(repo, monkeypatch):
    monkeypatch.setattr(L, "_allowed_filter", lambda cfg: (lambda gid: gid == "gYes"))
    repo.scores = [{"group_id": "gNo", "judge_overall": 0.5}] * 25
    assert L.refresh_hf_reachability_caps("acct", cfg_on(), NOW) == 0
    assert L.reach_cap_count() == 0


# ===== 9. 占比外环（写侧）=====


def test_share_guard_recomputes_today_and_warns_only_on_exceed(monkeypatch, caplog):
    # gA 故意做成"人类近乎不说话、bot 一直在说"：颠倒分子分母后占比会掉到 4%
    # （低于目标 5%），于是"把 outbound/inbound 传反"这件事能被这条用例抓住。
    rows = [{"gid": "gA", "direction": "inbound", "n": 2},
            {"gid": "gA", "direction": "outbound", "n": 48},
            {"gid": "gB", "direction": "inbound", "n": 100},
            {"gid": "gB", "direction": "outbound", "n": 1},
            {"gid": "", "direction": "outbound", "n": 5}]        # 脏行（归群失败）
    seen = []

    def fake_share(account_id, since_sec):
        seen.append((account_id, since_sec))
        return rows

    monkeypatch.setattr(repo_messages, "list_bot_msg_share", fake_share)
    monkeypatch.setattr(O, "note_hf_group_share", lambda *a: seen.append(a))
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        hits = L.maybe_recompute_hf_share_guard("acct", cfg_on(), NOW)
    assert seen[0] == ("acct", B.hf_day_start_sec(NOW)), "只算今日（本地零点起）"
    assert hits == 1, "只有 gA 超标（96% > 5%，且量本来够）"
    assert not any("gB" in str(r.message) for r in caplog.records), "冷清群不吵"
    assert any("gA" in str(r.message) for r in caplog.records)
    assert any("96.0%" in str(r.message) for r in caplog.records), "占比口径 = bot / 总量"
    assert any(isinstance(a[3], O.HfGroupVolume) and a[3].total == 50 for a in seen[1:]), \
        "落内存的是当日音量（judge 读侧零 DB IO 靠它）"
    assert {a[1] for a in seen[1:]} == {"gA", "gB"}, \
        "无群号的脏行不许进内存（它会成为一个永远查不到的影子群）"


def test_share_guard_disabled_makes_no_query(monkeypatch):
    monkeypatch.setattr(repo_messages, "list_bot_msg_share",
                        lambda *a: pytest.fail("关掉就不该查库"))
    assert L.maybe_recompute_hf_share_guard("acct", cfg_on(shareGuard={"enabled": False}),
                                            NOW) == 0


# ===== 10. 配置面 =====


def test_defaults_match_profile_band_fallback():
    """**band 区间的唯一真源在这里** —— ``profile._BAND_FALLBACK`` 必须逐字相等。

    漂了的表现：这里解析出 (0.5, 0.9) 而 profile 的兜底取别的值 ⇒ 同一个阈值在两处被
    钳成不同的数，且**只在配置缺字段时**才出现（最难复现的一类）。
    """
    assert (L.HF_LEARNING_DEFAULTS["bandMin"], L.HF_LEARNING_DEFAULTS["bandMax"]) == \
        P._BAND_FALLBACK


def test_defaults_are_pinned():
    D_ = L.HF_LEARNING_DEFAULTS
    assert D_["enabled"] is False, "自动调阈默认关（老板要显式开）"
    assert (D_["minSample"], D_["sampleWindow"]) == (10, 20)
    assert (D_["lowEngageRate"], D_["highEngageRate"], D_["step"]) == (0.15, 0.5, 0.05)
    assert (D_["observeWindowSec"], D_["labelWindowSec"]) == (600, 60)
    assert (D_["ambientMax"], D_["minChangeCooldownSec"]) == (0.5, 4 * 3600)
    assert (D_["sweepIntervalSec"], D_["staleJudgedMaxSec"]) == (300, 1800)
    assert D_["recoverUnreachable"] is True
    assert (D_["reachabilityWindowSec"], D_["reachabilityMinSample"]) == (7 * 86400, 20)
    assert D_["silenceExemptMarginMin"] == 0.1, "v1.16.2 生产实排空档 0.10"
    assert L.HF_REACH_DB_ROW_CAP == 5000
    assert L.HF_REPLY_THRESHOLD_FALLBACK == 0.6


def test_resolve_reads_config_and_leaves_untouched_fields_at_default():
    r = L.resolve_hf_learning({"replyThreshold": 0.7, "learning": {
        "enabled": True, "minSample": 12, "bandMin": 0.55, "bandMax": 0.85,
        "step": 0.03, "recoverUnreachable": False, "sweepIntervalSec": 120}})
    assert r["enabled"] is True and r["minSample"] == 12
    assert (r["bandMin"], r["bandMax"]) == (0.55, 0.85)
    assert (r["step"], r["recoverUnreachable"], r["sweepIntervalSec"]) == (0.03, False, 120)
    assert r["ambientMax"] == 0.5, "未提到的字段回落缺省"
    assert r["silenceExemptMarginMin"] == 0.1, "v1.16.2：未配置 ⇒ 缺省边距"
    assert L.resolve_reply_threshold({"replyThreshold": 0.7}) == 0.7
    assert L.resolve_reply_threshold(None) == 0.6
    assert L.resolve_reply_threshold({"replyThreshold": 1.5}) == 0.6, "越界回落（阈值 >1 恒假）"


@pytest.mark.parametrize("dirty", ["abc", None, True, float("nan"), float("inf"), [], {}])
def test_dirty_config_falls_back_not_nan(dirty):
    """**本模块最重要的一条**：脏配置一律回落缺省，绝不产生 ``NaN``。

    ``bandMin: NaN`` 的下场是生效阈值 NaN ⇒ ``judge_overall >= NaN`` 恒假 ⇒ **一条都不回**
    且不报错（09-26 静默停摆的形态）。逐字段过一遍，任何一个漏网都会重演。
    """
    r = L.resolve_hf_learning({"learning": {
        "bandMin": dirty, "bandMax": dirty, "step": dirty, "minSample": dirty,
        "lowEngageRate": dirty, "highEngageRate": dirty, "ambientMax": dirty,
        "sweepIntervalSec": dirty, "observeWindowSec": dirty, "labelWindowSec": dirty,
        "minChangeCooldownSec": dirty, "staleJudgedMaxSec": dirty,
        "reachabilityWindowSec": dirty, "reachabilityMinSample": dirty,
        "sampleWindow": dirty, "silenceExemptMarginMin": dirty}})
    for k, v in r.items():
        if isinstance(v, bool):
            continue
        assert math.isfinite(v), f"{k} 变成非有限数了"
    assert (r["bandMin"], r["bandMax"]) == (0.5, 0.9)
    assert (r["step"], r["minSample"], r["sweepIntervalSec"]) == (0.05, 10, 300)


def test_dirty_learning_section_is_not_fatal():
    """``learning`` 整段形状脏（字符串/数字/列表）⇒ 全缺省，**不抛**。

    这个函数在 judge 热路径与 300s sweep 上都会被调：在这里抛错的形态是"整条链静默不工作"。
    """
    for bad in ("nonsense", 42, ["x"], None):
        r = L.resolve_hf_learning({"learning": bad})
        assert (r["minSample"], r["enabled"]) == (10, False)
    assert L.resolve_hf_learning("not-a-mapping")["minSample"] == 10


@pytest.mark.parametrize("bad", ["false", 0, None, "no"])
def test_string_false_does_not_flip_switches(bad):
    """真值判断会把 ``"false"`` 读成"开" —— ``recoverUnreachable`` 反着读会**静默关掉护栏**。"""
    r = L.resolve_hf_learning({"learning": {"enabled": bad, "recoverUnreachable": bad}})
    assert (r["enabled"], r["recoverUnreachable"]) == (False, True)


def test_inverted_band_falls_back_on_both_ends():
    """``bandMin > bandMax``（配置写反）⇒ 两个都回落缺省：只回落一个等于换了个错法。"""
    r = L.resolve_hf_learning({"learning": {"bandMin": 0.85, "bandMax": 0.6}})
    assert (r["bandMin"], r["bandMax"]) == (0.5, 0.9)


@pytest.mark.parametrize("bad", [-0.5, -1, -3.7])
def test_negative_numbers_fall_back_on_both_ends(bad):
    """**负数**是另一类脏值（``_num_or`` 的 ``lo``/``hi`` 就是为它设的）。

    单测"字符串/NaN 回落"盖不住它：``bandMin: -1`` 会让地板彻底失效（阈值可压到 0），
    ``step: -0.5`` 会让调阈方向整个反过来，``ambientMax: -3`` 会让本底过滤恒不采信
    —— 三种都不是崩溃，而是**静默功能丧失**，所以逐字段断言它们回落缺省。
    """
    r = L.resolve_hf_learning({"learning": {
        "bandMin": bad, "bandMax": bad, "step": bad, "lowEngageRate": bad,
        "highEngageRate": bad, "ambientMax": bad, "minSample": bad, "sampleWindow": bad,
        "reachabilityMinSample": bad}})
    assert (r["bandMin"], r["bandMax"]) == (0.5, 0.9)
    assert (r["step"], r["lowEngageRate"], r["highEngageRate"]) == (0.05, 0.15, 0.5)
    assert (r["ambientMax"], r["minSample"], r["sampleWindow"]) == (0.5, 10, 20)
    assert r["reachabilityMinSample"] == 20


def test_int_fields_floor_but_reject_zero_and_negative():
    r = L.resolve_hf_learning({"learning": {"minSample": 12.9, "sampleWindow": 0,
                                            "reachabilityMinSample": -3}})
    assert r["minSample"] == 12, "小数向下取整"
    assert r["sampleWindow"] == 20, "0 ⇒ 回落（0 条窗口 = 永远没样本）"
    assert r["reachabilityMinSample"] == 20, "负数 ⇒ 回落"


def test_bool_is_not_accepted_as_a_number():
    """``Number.isFinite(true) === false``：``enabled: 1`` 不许被当成"开着"混进数字列。"""
    assert L._is_num(1) is True and L._is_num(True) is False and L._is_num(float("nan")) is False


# ===== 11. 源级接线守卫（learn 侧）=====


def test_module_level_imports_have_no_third_party_dependency():
    """加载期只许 stdlib + 本包相对导入（本模块在**注册链**上：``heartflow.py`` import 它）。

    模块级的 ``pymysql`` 之类会让"注册探针缺依赖"变成**静默不注册**（F5 的形态，
    第 4 步真栽过一次：AST 全绿、真跑少 14 个工具）。数据层一律函数体内延迟导入。
    """
    tree = ast.parse(LEARN_SRC)
    top: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top.append(("." * node.level) + (node.module or ""))
    stdlib = {"__future__", "logging", "math", "time", "dataclasses", "decimal", "typing"}
    for name in top:
        assert name.startswith(".") or name.split(".")[0] in stdlib, f"模块级第三方依赖: {name}"
    for lazy in ("from ...data import repo_hf", "from ...data import repo_messages",
                 "from ...core import metrics"):
        assert lazy in LEARN_SRC, f"数据层/埋点仍是延迟导入: {lazy}"


def test_learn_imports_heartflow_only_inside_a_function():
    """``heartflow``(#9) 反向 import 本模块 ⇒ 只能**函数体内**导入，否则成环。

    成环在注册链上的表现同样是**静默不注册**（异常被注册器吞成 WARNING）。
    """
    tree = ast.parse(LEARN_SRC)
    for node in tree.body:                       # 只看模块顶层
        if isinstance(node, ast.Import):
            assert all(a.name != "heartflow" for a in node.names), "模块级 import heartflow 会成环"
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "") != "heartflow", "模块级 import heartflow 会成环"
    idx = LEARN_SRC.index("from . import heartflow as _hf")
    assert LEARN_SRC[:idx].rindex("def ") == LEARN_SRC[:idx].rindex("def _allowed_filter("), \
        "该 import 必须在 _allowed_filter 体内（前一个 def 就是它自己）"


def _fn_code(fname: str) -> str:
    """取函数体源码（**去掉 docstring**）—— docstring 里写满了反例说明，搜不得。

    同时兼作"函数存在性"断言：函数没了 ``ast`` 会在这里报出来，而不是让断言假绿。
    """
    tree = ast.parse(LEARN_SRC)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fname:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"源码里找不到函数 {fname}")


@pytest.mark.parametrize("fname", [
    "resolve_hf_effective_threshold", "resolve_threshold_override",
    "resolve_hf_threshold_decision", "hf_layer_anchor_for", "hf_layer_step",
    "get_open_hf_window", "get_learned_threshold", "clamp_hf_threshold_to_reachability",
    "forget_hf_open_window", "round2", "clamp_hf_threshold_to_band", "classify_hf_send",
])
def test_hot_path_functions_have_no_import_or_sql(fname: str):
    """judge 热路径零 DB IO（V7）：体内不得出现**导入语句 / SQL / 取连接**。

    这几个函数是"每条群消息都会走"的那条链；函数级 import 虽不炸加载期，但会让热路径
    每次调用都过一次 import 锁。
    """
    body = _fn_code(fname)
    assert body.strip(), f"{fname} 的函数体被切空了（这条断言会假绿）"
    for banned in ("import ", "SELECT", "get_conn", "await ", "repo_", "executemany"):
        assert banned not in body, f"{fname} 体内不得出现 {banned!r}"


def test_whitelist_rule_has_exactly_one_implementation():
    """白名单语义只许有一处（``heartflow.is_hf_group_allowed``）—— 复制必漂移。"""
    body = _fn_code("_allowed_filter")
    assert "is_hf_group_allowed" in body, "必须委托给 heartflow"
    for banned in ("whitelist", "allowedGroups", "groupIds"):
        assert banned not in body, f"不得在本地再实现一份白名单判定: {banned}"


def test_allowed_filter_delegates_when_heartflow_is_present(monkeypatch):
    """``_allowed_filter`` 真委托（用假模块顶替 —— #9 落地前后都跑得动，不是 skip 壳）。

    ⚠️ **两处都要顶替**：``from . import heartflow`` 先看**父包属性**，只有属性缺席时才
    走 ``sys.modules``。跑单文件时属性恰好是空的（``heartflow/__init__.py`` 零 import），
    于是"只 patch sys.modules"看着能过；一旦同文件里别的用例（如
    ``refresh_hf_reachability_caps`` 的白名单过滤）真导入过一次，属性就留住了 ——
    这里静默拿到**真模块**，"gNo" 被全放行，用例变成**看顺序吃饭**。
    """
    fake = types.ModuleType("plugin.inbound.heartflow.heartflow")
    fake.is_hf_group_allowed = lambda gid, cfg: gid == "gYes"
    monkeypatch.setitem(sys.modules, "plugin.inbound.heartflow.heartflow", fake)
    monkeypatch.setattr(HF_PKG, "heartflow", fake, raising=False)
    f = L._allowed_filter({"enabled": True})
    assert f is not None and f("gYes") is True and f("gNo") is False


def test_allowed_filter_degrades_to_no_filter_without_heartflow(monkeypatch, caplog):
    """取不到 heartflow ⇒ 返回 ``None`` = 不过滤，而"白名单为空"**语义相同**（全放行）。

    所以这条降级路径不复制任何规则，也就没有第二份规则可漂移；退化本身记 debug
    （它在生产上不该发生）。

    造"取不到"要**删父包属性 + ``sys.modules`` 置 ``None``**（见上一条的说明）：
    属性若留着（哪怕是 ``None``），``from . import`` 会**直接返回它**、根本不抛，
    于是这条降级路径永远不被走到 —— 用例假绿。
    """
    monkeypatch.delattr(HF_PKG, "heartflow", raising=False)
    monkeypatch.setitem(sys.modules, "plugin.inbound.heartflow.heartflow", None)
    with caplog.at_level(logging.DEBUG, logger=L.__name__):
        assert L._allowed_filter({"enabled": True}) is None


def test_run_sweep_is_async_while_the_pipeline_is_sync():
    """``run_sweep`` 必须 ``async``（要 await 画像生成），其余管线同步（pymysql 同步）。"""
    assert inspect.iscoroutinefunction(L.run_sweep)
    for fn in (L.persist_hf_judged, L.persist_hf_send_outcome, L.mark_hf_group_engaged,
               L.load_learned_thresholds, L.load_hf_budget_seed,
               L.maybe_recompute_hf_share_guard, L.refresh_hf_reachability_caps,
               L.apply_hf_threshold_change):
        assert not inspect.iscoroutinefunction(fn), f"{fn.__name__} 不该是 async"


def test_fake_row_shape_matches_real_queries():
    """假数据层的行形状必须与真 ``SELECT`` 逐列一致（否则**所有**用例一起失真却全绿）。"""
    checks = [
        ("list_recent_judged_scores", {"group_id", "judge_overall"}),
        ("get_closed_recent", {"engaged", "closed_at", "engage_signal", "sent_at"}),
        ("list_group_states", {"account_id", "group_id", "learned_threshold", "last_change_at"}),
        ("list_sent_counts_recent", {"group_id", "hour_count", "day_count", "last_sent_at"}),
    ]
    for fn, cols in checks:
        start = REPO_HF_SRC.index(f"def {fn}(")
        body = REPO_HF_SRC[start:REPO_HF_SRC.index("\ndef ", start + 1)]
        for col in cols:
            assert col in body, f"{fn} 的真查询里没有 {col!r}（假行已与真查询漂移）"

    start = REPO_MSG_SRC.index("def list_bot_msg_share(")
    body = REPO_MSG_SRC[start:REPO_MSG_SRC.index("\ndef ", start + 1)]
    for col in ("gid", "direction", "n"):
        assert col in body, f"list_bot_msg_share 里没有 {col!r}"


def test_no_real_group_ids_in_sources():
    """R2 红线：群 ID 形态可断言；门店/品牌/人名不许写在这里。"""
    pat = re.compile(r"\d{9,}@chatroom")
    for src in (LEARN_SRC, Path(__file__).read_text(encoding="utf-8")):
        assert not pat.search(src)


DEDUPE_TEXT = "今天这家店的报价单我看过了，明天再给你答复"     # ≥ minChars(12)


def test_cache_is_registered_and_reset_clears_siblings_but_not_profile(layer_rows):
    """learn 的重置**连带**清掉共用同一批样本的三个模块（TS 逐字如此），**但不动画像**。

    三个 = ``layer``（分层统计，同样的样本）+ ``dedupe``（回复历史）+ ``observe``（占比外环）。
    画像不同源（LLM 生成、按 ``generatedAt`` 自过期），它有自己的登记名 ``profile`` ——
    第二条断言守着"别顺手把它也清了"：顺手清掉的代价是**每次重置都白花一次 LLM 调用**，
    且只在重置路径上发生（生产看不到，测试里也只是慢）。

    三处都用**可观测**的方式验（而不是数私有 dict）：分层看 ``hf_layer_cache_size``；
    重复闸与占比外环走它们**自己的读口** —— 读口才是生产真正依赖的那一面。
    """
    assert "learn" in CACHE_REGISTRY.names()
    _seed_learned("acct", "gA", 0.7)
    _seed_cap("acct", "gA", cap=0.55, max_score=0.5, n=40, at_sec=NOW)
    _seed_profile("acct", "gA", band=0.7)
    assert layer_rows([_row()]) == 1
    D.note_recent_reply("acct", "gA", DEDUPE_TEXT, NOW, D.resolve_dedupe_cfg(None))
    O.note_hf_group_share("acct", "gA", NOW, O.HfGroupVolume(total=100, bot_sends=90), 0.9)
    assert D.check_hf_repeat("acct", "gA", DEDUPE_TEXT, D.resolve_dedupe_cfg(None),
                             NOW + 60) is not None, "种进去了（兜底：这条读口本身也得是活的）"
    assert O.get_hf_share_tighten("acct", "gA", B.resolve_budget(None), None, NOW + 60) is not None

    CACHE_REGISTRY.reset("learn")
    assert L.learned_threshold_count() == 0 and L.reach_cap_count() == 0
    assert LAY.hf_layer_cache_size() == 0, "分层缓存也要清（同批样本 —— 只清一半最难看）"
    assert D.check_hf_repeat("acct", "gA", DEDUPE_TEXT, D.resolve_dedupe_cfg(None),
                             NOW + 60) is None, "重复历史被清"
    assert O.get_hf_share_tighten("acct", "gA", B.resolve_budget(None), None,
                                  NOW + 60) is None, "占比外环内存被清"

    assert P.hf_profile_cache_size() == 1, "画像不在 learn 的连带清理范围内"
    CACHE_REGISTRY.reset_all()                  # conftest 的口径
    assert P.hf_profile_cache_size() == 0, "全清时画像由自己的登记名负责"


# ===== 12. 无 DB 依赖的小工具 =====


def test_prune_open_windows_only_this_account_and_only_expired(repo):
    _outcome({"ok": True, "msg_id": "a1"})                        # acct/gA（600s）
    L.persist_hf_send_outcome("other", "m9", {"ok": True, "msg_id": "b1"}, NOW,
                              group_id="gA", observe_sec=600, label_window_sec=60)
    L.prune_open_windows("acct", NOW + 599)
    assert L.open_window_count() == 2, "未到期不清"
    L.prune_open_windows("acct", NOW + 600)
    assert L.open_window_count() == 1, "只清本账号的已到期窗"
    assert L.get_open_hf_window("other", "gA") is not None


def test_signal_histogram_order_and_unknown_bucket():
    rows = [_s("quote", True), _s(None, False), _s("quote", True), _s("veto", False)]
    assert L.signal_histogram(rows) == "quote:2,unknown:1,veto:1"


def test_load_ambient_by_group_hour_keeps_every_hour(monkeypatch):
    """★ B9：**全时段**都留着，键是 ``群|小时``（不筛当前小时）。

    旧写法在这里只留当前小时、把键缩成群名 —— 于是调用方只能拿一份"现在"的本底去过滤
    横跨 7 天的全部样本，夜里那批（那时段群本来就冷清）被整片判成"不可采信"。
    """
    counts = {f"gA|{B.hf_local_hour(NOW)}": 0, "gA|3": 5, "gB": 3, "gC|abc": 9}
    monkeypatch.setattr(LAY, "load_hf_group_hour_counts", lambda *a: counts)
    out = L.load_ambient_by_group_hour("acct", NOW, 60)
    assert set(out) == {f"gA|{B.hf_local_hour(NOW)}", "gA|3"}, \
        "每个「群|小时」各留一份；脏键（无 | 或小时非数）跳过"
    assert out[f"gA|{B.hf_local_hour(NOW)}"] == 0.0
    assert out["gA|3"] > 0.0, "别的时段的本底必须还在（调用方要按样本自己的小时去查）"


def test_load_ambient_failure_degrades_to_empty(monkeypatch, caplog):
    """基线查询失败 ⇒ 空表（弱信号全采信，退回旧行为）—— 但必须留 warn 看得见。"""
    def boom(*_a):
        raise RuntimeError("boom")
    monkeypatch.setattr(LAY, "load_hf_group_hour_counts", boom)
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        assert L.load_ambient_by_group_hour("acct", NOW, 60) == {}
    assert any("ambient baseline" in r.message for r in caplog.records)
