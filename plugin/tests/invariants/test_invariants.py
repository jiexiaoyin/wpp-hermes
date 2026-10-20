"""不变量守卫 I1–I11：把"心流为什么不能这么改"写成**会红的断言**。

## 为什么是断言而不是注释

这些条款每一条都对应一次**真实生产事故**，而每一次的特征都是
「**测试全绿、日志干净、指标正常**」：

===================  ==========================================================
I1 / I7 / I8         09-26 心流自锁停摆 **3 天**（阈值数字进 prompt ⇒ 裁判按它校准打分；
                     画像里的"别插话"成了 65% 权重的否决票；画像 band 无上界当硬地板）
I2                   漂移永远朝**松**的方向走（收紧逻辑一旦有一处取反，谁也不报错）
I4                   闸与日报各算一份判据 ⇒ 观测驱动的收口是假的
I5                   占位符（vendor 去重 / 重复闸 / 空文本）也占预算、开窗、进重复历史
I6                   用 ``status='sent'`` 数"最后发言"⇒ sweep 关窗后**恒为 0**
                     （09-11 静默瘫 3 天就是被这个假 0 掩盖的）
I10                  空正文静默返回 ``""`` ⇒ 5 维全 0 ⇒ 一条不回，**且不报错**
===================  ==========================================================

注释不会在回归时拦人，而且这些条款的失效形态恰恰是"没人注意到"。
所以每条写成断言，并且：

* 能**行为级**验的一律行为级（喂进真实输入、看输出）；
* 只剩"接线/形态"可验的（热路径不许读 DB、算法只许一个出处、SQL 只许一种写法）
  用 **AST/源码级**断言 —— 这类没有别的观察面，但它们的回归同样是静默的。

## 两条完整性守卫（第 12、13 条）

1. :func:`test_i12_every_invariant_id_has_a_test_here` —— **1..11 每一条在本文件里
   都必须有用例**。没有它，删掉一条不变量测试是**无声**的（文件仍然全绿）。
2. :func:`test_i13_every_invariant_is_marked_in_its_source_home` —— 每条在本文件声明的
   **源码老家**（:data:`HOMES`）里必须有一行 ``INVARIANT: I<n>`` 标记。没有它，
   源码侧的标记会在重构里被顺手删掉，而下一次读代码的人就没有线索了。

## 与各模块自有用例的关系

本文件是**汇总名册**（一 id 一条，说清"这条不变量是什么"），
``tests/heartflow/test_*.py`` 是细节（边界值、脏输入、日志文案）。
两处**有意重复**：那 147 条里任何一条被误删时，本文件仍会红。

跑不过**不许 deploy**（计划第 7 步）。
"""

from __future__ import annotations

import ast
import json
import logging
import math
import random
import re
import time
from pathlib import Path
from typing import Any

import pytest

from plugin.data import repo_hf
from plugin.inbound.heartflow import (
    budget,
    dedupe,
    guards,
    heartflow as HF,
    layer as LAY,
    learn as L,
    observe,
    profile,
    runtime,
)
from plugin.llm import judge as J

#: 仓根（``plugin/tests/invariants/test_invariants.py`` 往上三层）。
ROOT = Path(__file__).resolve().parents[3]

#: 每条不变量的**老家**：源码里必须留下 ``INVARIANT: I<n>`` 的文件。
#: 写成表而不是散在各处，是为了让"移动/重命名源码文件"这件事**当场红**。
HOMES: dict[int, tuple[str, ...]] = {
    1: ("plugin/inbound/heartflow/heartflow.py",),
    2: ("plugin/inbound/heartflow/budget.py", "plugin/inbound/heartflow/guards.py"),
    3: ("plugin/inbound/heartflow/layer.py",),
    4: ("plugin/inbound/heartflow/dedupe.py", "plugin/inbound/heartflow/observe.py"),
    5: ("plugin/inbound/heartflow/learn.py",
        "plugin/inbound/heartflow/heartflow.py",
        "plugin/inbound/heartflow/budget.py",
        "plugin/platform/adapter.py"),
    6: ("plugin/data/repo_hf.py",),
    7: ("plugin/inbound/heartflow/profile.py",),
    8: ("plugin/inbound/heartflow/profile.py",),
    9: ("plugin/inbound/heartflow/heartflow.py",),
    10: ("plugin/llm/judge.py",),
    11: ("plugin/inbound/heartflow/heartflow.py",),
}

#: ``INVARIANT: I3`` / ``**INVARIANT I7**`` / ``（INVARIANT I11，…`` 都算标记。
#: ``\b`` 是关键：``I1\b`` **不会**命中 ``I11``。
_MARK_RE = re.compile(r"INVARIANT:?\s*\**\s*I(\d+)\b")


# ============================================================ 源码工具（AST / 文本）


_SRC_CACHE: dict[str, str] = {}


def _src(rel: str) -> str:
    if rel not in _SRC_CACHE:
        _SRC_CACHE[rel] = (ROOT / rel).read_text(encoding="utf-8")
    return _SRC_CACHE[rel]


def _fn_node(rel: str, fname: str) -> ast.AST:
    """取顶层函数的 AST 节点（找不到就炸 —— 别让"函数改名了"变成假绿）。"""
    for node in ast.parse(_src(rel)).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fname:
            return node
    raise AssertionError(f"{rel} 里找不到顶层函数 {fname}")


def _fn_code(rel: str, fname: str) -> str:
    """函数体源码（**去 docstring**）—— docstring 里写满了反例，搜不得。"""
    node = _fn_node(rel, fname)
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def _callers_of(rel: str, callee: str) -> set[str]:
    """哪些函数体内调用了 ``callee``（模块顶层调用记作 ``"<module>"``）。"""
    out: set[str] = set()

    def walk(node: ast.AST, inside: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if isinstance(child, ast.Call):
                f = child.func
                nm = f.id if isinstance(f, ast.Name) else (
                    f.attr if isinstance(f, ast.Attribute) else None)
                if nm == callee:
                    out.add(inside or "<module>")
            walk(child, inside)

    walk(ast.parse(_src(rel)), "")
    return out


def _module_level_names(rel: str) -> set[str]:
    """模块顶层被赋值的名字（纯函数不许写这些 —— 那就是副作用）。"""
    out: set[str] = set()
    for node in ast.parse(_src(rel)).body:
        if isinstance(node, ast.Assign):
            out |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


# ============================================================ I1：阈值数字绝不进 prompt


class _FrozenClock:
    """固定时钟替身。

    prompt 正文里有一行 ``时间: %H:%M:%S``（每次都不同）⇒ 不冻住就没法做
    "逐字节相同"的差分断言。冻住时钟不是绕过，而是把变量逐个消掉：
    这样"prompt 变了"只可能来自**配置**，也就是我们要验的那一项。
    """

    def __init__(self, sec: float) -> None:
        self._t = time.localtime(sec)

    def localtime(self, *_a: Any) -> time.struct_time:      # noqa: D102
        return self._t

    def strftime(self, fmt: str, t: Any) -> str:            # noqa: D102
        return time.strftime(fmt, t)


def test_i1_the_prompt_is_byte_identical_under_any_threshold(monkeypatch):
    """**I1**：阈值（以及一切与阈值相关的配置）改动，prompt 必须**一个字节都不变**。

    09-26 那次自锁：prompt 里写着"回复阈值: 0.6"，裁判于是拿这个数**校准自己的打分** ——
    阈值一动，分数跟着动，闭环失效，最后阈值 0.85 + 分数被压低 → 连明显该回的消息都判不回，
    停摆 3 天。判定当然还是按数值阈值做，但那个数**只在代码里**。
    """
    monkeypatch.setattr(HF, "time", _FrozenClock(1_700_000_000))
    inp = HF.HfJudgeInput(
        chat_id="g1@chatroom", bot_nickname="小助", content="这个多少钱？",
        sender_name="张三", chat_context="门店群，主要聊报价", recent_messages="张三: 在吗",
        last_bot_reply="在的", seconds_since_last_reply=600.0, energy=0.8)
    base: dict[str, Any] = {"model": "m", "replyThreshold": HF.HF_DEFAULTS["replyThreshold"]}
    ref = HF.build_prompt(inp, base)

    variants = [
        {**base, "replyThreshold": 0.01},
        {**base, "replyThreshold": 0.99},
        {**base, "learning": {"enabled": True, "bandMin": 0.5, "bandMax": 0.9,
                              "maxRaise": 0.1, "step": 0.05}},
        {**base, "learning": {"enabled": True, "ambientMax": 0.3}},
        {**base, "weights": {"relevance": 1.0, "willingness": 0.0, "social": 0.0,
                             "timing": 0.0, "continuity": 0.0}},
    ]
    for cfg in variants:
        assert HF.build_prompt(inp, cfg) == ref, \
            f"这套配置不许改到 prompt：{sorted(k for k in cfg if k not in base) or 'replyThreshold'}"

    body = _fn_code("plugin/inbound/heartflow/heartflow.py", "build_prompt")
    assert body.strip(), "函数体被切空了 —— 下面的断言会假绿"
    for banned in ("replyThreshold", "resolve_reply_threshold", "HF_REPLY_THRESHOLD_FALLBACK",
                   "bandMin", "bandMax", "resolve_hf_learning", "阈值", "threshold"):
        assert banned not in body, f"build_prompt 体内不得出现 {banned!r}（**连读都不许读**）"


# ============================================================ I2：只能收紧不能放宽


def test_i2_the_guard_itself_catches_loosening():
    """**I2**（守卫自证）：``assert_not_looser`` 对"放宽"必须 raise。

    先验守卫本身 —— 否则下面那条随机化用例即使全绿，也只是"没抽到反例"。
    """
    with pytest.raises(guards.InvariantError):
        guards.assert_not_looser({"minGapSec": 60}, {"minGapSec": 10},
                                 higher_is_tighter=("minGapSec",), label="t")
    for after in ({"maxPerHour": 9}, {"maxPerHour": 0}):     # 变大 / 变"不设上限"
        with pytest.raises(guards.InvariantError):
            guards.assert_not_looser({"maxPerHour": 5}, after, lower_is_tighter=("maxPerHour",))
    # 合法方向：更严 / 从"不设上限"收紧成有限值
    guards.assert_not_looser({"minGapSec": 10}, {"minGapSec": 60}, higher_is_tighter=("minGapSec",))
    guards.assert_not_looser({"maxPerHour": 5}, {"maxPerHour": 4}, lower_is_tighter=("maxPerHour",))
    guards.assert_not_looser({"maxPerHour": 0}, {"maxPerHour": 5}, lower_is_tighter=("maxPerHour",))
    # 缺键跳过：守卫只管"能比的"，不替调用方判缺省
    guards.assert_not_looser({}, {}, higher_is_tighter=("minGapSec",), lower_is_tighter=("maxPerHour",))


def test_i2_budget_only_tightens_over_random_inputs():
    """**I2**（随机化属性测试）：400 组随机 (base, 画像建议) → 结果永不放宽。

    09-26 复盘里"阈值/带宽只往松的方向走"是特征之一。这条不变量没有"正确值"可断言
    （收紧多少是配置自由），只有**方向**可以断言 —— 所以用随机化覆盖方向。
    """
    rnd = random.Random(20261001)
    for _ in range(400):
        base = budget.BudgetConfig(
            enabled=True,
            min_gap_sec=rnd.choice([0, 1, 30, 60, 300]),
            max_per_hour=rnd.choice([0, 1, 3, 10]),
            max_per_day=rnd.choice([0, 5, 20, 100]),
            no_consecutive_without_human=rnd.choice([True, False]),
            quiet_hours=(),
        )
        tighter = {
            "minGapSec": rnd.choice([0, 5, 60, 600]),
            "maxPerHour": rnd.choice([0, 1, 3, 10, 99]),
            "maxPerDay": rnd.choice([0, 2, 50, 999]),
            "quietHours": [[0, 7]],
        }
        out = budget.tighten_budget(base, tighter)
        assert out.min_gap_sec >= base.min_gap_sec, "最小间隔不许变小（变小 = 更能刷屏）"
        if base.max_per_hour > 0:
            assert 0 < out.max_per_hour <= base.max_per_hour
        else:
            assert out.max_per_hour >= 0                 # base 不设上限 ⇒ 任何有限值都是收紧
        if base.max_per_day > 0:
            assert 0 < out.max_per_day <= base.max_per_day
        assert out.enabled == base.enabled, "画像无权关掉结构约束"
        assert out.no_consecutive_without_human == base.no_consecutive_without_human
        assert out.quiet_hours == base.quiet_hours, "静默段默认不并入（一次幻觉能整天不吭声）"

    # 画像建议 enabled=False ⇒ 整条忽略（不是"用缺省收紧"）
    base = budget.BudgetConfig(enabled=True, min_gap_sec=60, max_per_hour=5, max_per_day=20,
                               no_consecutive_without_human=True, quiet_hours=())
    assert budget.tighten_budget(base, {"enabled": False, "maxPerHour": 99}) == base

    body = _fn_code("plugin/inbound/heartflow/budget.py", "tighten_budget")
    assert "assert_not_looser" in body, \
        "守卫必须长在函数体里：随机用例只覆盖抽到的那几组，守卫才覆盖全部输入"


# ============================================================ I3：分层统计是纯函数


def test_i3_the_layer_aggregate_is_pure_idempotent_and_cache_free():
    """**I3**：``aggregate_hf_layer_stats`` 是**纯函数** —— 同输入同输出、不改输入、不写缓存。

    为什么这条要单独立：它是"从零重算"这一步，和 sweep 的增量刷新共用同一份落库数据。
    一旦有人在里面顺手写全局缓存或改样本对象，"重算"与"增量"两条路就给出不同的段统计，
    而两者的产物长得一模一样（都是 ``HfLayerStatRow``）—— 只能靠这条断言分开。
    """
    buckets = ((0, 6), (6, 12), (12, 18), (18, 24))
    samples = [
        LAY.HfLayerSample(group_id="g1", engaged=1, engage_signal="quote", sent_at=1_700_000_000),
        LAY.HfLayerSample(group_id="g1", engaged=0, engage_signal="silence",
                          sent_at=1_700_000_000 + 3 * 3600),
        LAY.HfLayerSample(group_id="g2", engaged=1, engage_signal="mention",
                          sent_at=1_700_000_000 + 20 * 3600),
        # 旧错误标签（v1.6.8 之前没有信号列）⇒ 必须丢
        LAY.HfLayerSample(group_id="g3", engaged=1, engage_signal=None, sent_at=1_700_000_000),
        # 没有归段依据 ⇒ 必须丢
        LAY.HfLayerSample(group_id="g4", engaged=1, engage_signal="quote", sent_at=None),
    ]
    before = [(s.group_id, s.engaged, s.engage_signal, s.sent_at) for s in samples]
    opts = LAY.HfLayerAggregateOpts(buckets=buckets, ambient_p_by_group_hour={},
                                    ambient_max=0.3, window_start=0, also_groups=("g9",))

    a = LAY.aggregate_hf_layer_stats(samples, opts)
    b = LAY.aggregate_hf_layer_stats(samples, opts)
    assert a == b, "同一输入两次必须给同一结果（纯函数）"
    assert [(s.group_id, s.engaged, s.engage_signal, s.sent_at) for s in samples] == before, \
        "不许就地改输入样本（改了就成「增量」与「重算」两套语义）"

    assert sum(r.n for r in a) == 3, "有信号 + 有 sent_at 的三条才进统计"
    gids = {r.group_id for r in a}
    assert "g3" not in gids and "g4" not in gids, "脏样本不许凭空造出一个群"
    assert "g9" in gids and all(r.n == 0 for r in a if r.group_id == "g9"), \
        "also_groups 要给空桶（前端进度条靠它）"
    # ⚠️ 这里**不能**只断言 ``hf_layer_cache_size() == 0`` —— 它数的是"行"
    #    （``sum(len(m) for m in _layer_stats.values())``），有人塞一个**空**的群桶
    #    （``_layer_stats["__mutant__"] = {}``，同样的 0 行）它就看不出来，
    #    而"顺手写缓存"正是要防的那个副作用。所以直接比**缓存字典本身**。
    assert LAY.hf_layer_cache_size() == 0, "聚合不得顺手写模块级缓存（行数）"
    assert not LAY._layer_stats, \
        "聚合不得顺手写模块级缓存 —— 空的群桶也算写（缓存字典必须原封不动）"

    fn = _fn_node("plugin/inbound/heartflow/layer.py", "aggregate_hf_layer_stats")
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Global)], "纯函数不许 global"
    assigned = {t.id
                for n in ast.walk(fn)
                if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                for t in ([*n.targets] if isinstance(n, ast.Assign) else [n.target])
                if isinstance(t, ast.Name)}
    overlap = assigned & _module_level_names("plugin/inbound/heartflow/layer.py")
    assert not overlap, f"纯函数写了模块级名字（副作用）: {sorted(overlap)}"

    # 上面只抓 ``NAME = ...``；``_layer_stats["k"] = v`` 是 Subscript 目标，会从缝里溜过去
    # （正是那个"塞个空群桶"的变异）。这里把"给模块级名字做下标赋值/增删"也一并禁掉。
    mod_names = _module_level_names("plugin/inbound/heartflow/layer.py")
    sub_writes = [
        ast.unparse(t)
        for n in ast.walk(fn)
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete))
        for t in (n.targets if isinstance(n, (ast.Assign, ast.Delete)) else [n.target])
        if isinstance(t, (ast.Subscript, ast.Attribute))
        and ast.unparse(t.value).split("[")[0] in mod_names
    ]
    assert not sub_writes, f"纯函数改了模块级容器（副作用）: {sub_writes}"


# ============================================================ I4：闸与日报同源


def test_i4_the_gate_and_the_daily_report_share_one_verdict_function():
    """**I4**：重复判定只有一个出处 —— 闸（拦）与日报（统计）必须调同一个函数。

    两边各写一份的后果：日报报出的"重复率 12%"**不等于**"这 12% 换到今天会被闸拦"，
    于是"看日报调参"这件事整体失效，而日报看起来一切正常。
    """
    # ① 算法唯一：相似度只许在 verdict 里被调用
    callers = _callers_of("plugin/inbound/heartflow/dedupe.py", "hf_jaccard")
    assert callers == {"hf_repeat_verdict"}, \
        f"hf_jaccard 只许被 hf_repeat_verdict 调用，实际调用方: {sorted(callers)}"
    for rel, fname in (("plugin/inbound/heartflow/dedupe.py", "check_hf_repeat"),
                       ("plugin/inbound/heartflow/observe.py", "hf_repeat_rate")):
        assert "hf_repeat_verdict(" in _fn_code(rel, fname), \
            f"{fname} 必须走同一个 verdict 函数（别自己再算一遍相似度）"

    # ② 行为同源：同一对句子，闸判"重复"，日报就计一次
    text = "这台机器的报价我下午发你，你先别急着下单，等我核一下库存再回你"
    cfg = dedupe.resolve_dedupe_cfg(None)
    hist = [dedupe.HfRecentReply(text=text, at_sec=1_700_000_000,
                                 norm=dedupe.norm_hf_text(text))]
    verdict = dedupe.hf_repeat_verdict(text, hist, cfg, 1_700_000_001)
    assert verdict is not None, "逐字相同的句子在窗口内必须判重复（这是闸的依据）"

    stat = observe.hf_repeat_rate(
        [observe.HfRepeatItem(group_id="g1", text=text, at_sec=1_700_000_000),
         observe.HfRepeatItem(group_id="g1", text=text, at_sec=1_700_000_001)],
        cfg)
    assert (stat.total, stat.repeats) == (2, 1), "日报必须逐条给出与闸一致的结论"
    assert stat.rate == 0.5


# ============================================================ I5：占位符不占预算 / 不开窗


def test_i5_a_placeholder_consumes_no_budget_and_opens_no_window(monkeypatch, caplog):
    """**I5**：``ok: true`` **不等于**真发出去 —— 占位符不许占预算、开窗、进重复历史。

    vendor 去重 / 重复闸 / ack 模板 / 空文本都是"回了个 ok 但一个字没发出去"。
    把它们当 sent 的后果是三重的，且都不报错：预算被白吃（真该回时回不了）、
    观察窗白开（拿没人接的窗去算接话率）、下一句被自己的"没说出口的话"拦掉。
    """
    sent_calls: list[tuple] = []
    suppressed_calls: list[tuple] = []

    def _set_sent(*a, **k):
        # ⚠️ 真 ``repo_hf.set_sent`` 的返回值是**受影响行数**；B4（2026-10-02）起它被当成
        #    成败信号（0 ⇒ 账本没写成 ⇒ 不开观察窗）。桩写成 ``list.append``（返回 None）
        #    就等于"每次都没写成"—— 那是**桩失真**，不是被测行为。
        sent_calls.append((a, k))
        return 1

    monkeypatch.setattr(repo_hf, "set_sent", _set_sent)
    monkeypatch.setattr(repo_hf, "set_suppressed",
                        lambda *a, **k: suppressed_calls.append((a, k)))

    acct, gid, now = "acct1", "g1@chatroom", 1_700_000_000
    # ⚠️ 文本必须**长过** ``minChars``（缺省 12）：短句两侧（判定/记账）都豁免，
    # 用短句验"没进历史"是假绿 —— 它本来就不会进。
    text = "在的，我这就帮你查一下库存，稍等几分钟给你准确答复"

    # ---- 占位符：四种来源，一种都不能漏 ----
    for placeholder in ("dedup-suppressed", "repeat-suppressed", "ack-template-dropped", ""):
        L.persist_hf_send_outcome(
            acct, f"in-{placeholder}", L.HfSendResult(ok=True, msg_id=placeholder), now,
            group_id=gid, observe_sec=60, label_window_sec=120, text=text)
        assert L.get_open_hf_window(acct, gid) is None, f"占位符 {placeholder!r} 不许开窗"
        assert dedupe.hf_recent_count(acct, gid) == 0, "没说出口的话不许进重复历史"
        assert budget.get_budget_state(acct, gid, now).last_reply_at_sec is None, \
            f"占位符 {placeholder!r} 不许消耗预算"
    assert not sent_calls, "占位符一次 set_sent 都不许有"
    assert len(suppressed_calls) == 4, "四条占位都要在台账里落 suppressed（不是不留痕）"

    # ---- 发送失败（ok=False）⇒ pending：什么都不改，等框架重试 ----
    L.persist_hf_send_outcome(acct, "in-fail", L.HfSendResult(ok=False), now,
                              group_id=gid, observe_sec=60, label_window_sec=120, text="x")
    assert L.get_open_hf_window(acct, gid) is None
    assert budget.get_budget_state(acct, gid, now).last_reply_at_sec is None
    assert len(suppressed_calls) == 4, "pending 不落任何状态（留给 sweep 的呆账收敛）"

    # ---- 真发（vendor 回了 msgId）⇒ 三件事一起发生，一个都不能少 ----
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="plugin.inbound.heartflow.learn"):
        L.persist_hf_send_outcome(
            acct, "in-ok", L.HfSendResult(ok=True, msg_id="vendor-1"), now,
            group_id=gid, observe_sec=60, label_window_sec=120, text=text)
    win = L.get_open_hf_window(acct, gid)
    assert win is not None and win.bot_msg_id == "vendor-1", "真发必须开窗并记住自己的 msgId"
    assert dedupe.hf_recent_count(acct, gid) == 1, "真发出去的文本才进重复历史"
    assert budget.get_budget_state(acct, gid, now).last_reply_at_sec == now, "真发才占预算"
    assert sent_calls and sent_calls[0][0][1] == "in-ok", "真发必须落 set_sent"
    assert sent_calls[0][0][3] == now + 60, "观察窗到期时刻 = 发送时刻 + observeWindowSec"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], \
        "整条 sent 路径不许有被吞掉的异常（那条 except 会静默放过记账失败）"

    # ---- 源级：三处副作用必须在 ``if outcome == sent`` 体内（不是"顺序凑巧"）----
    rel = "plugin/inbound/heartflow/learn.py"
    fn = _fn_node(rel, "persist_hf_send_outcome")
    sent_if = None
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and "HF_SEND_OUTCOME_SENT" in ast.unparse(node.test):
            sent_if = node
            break
    assert sent_if is not None, "找不到 sent 分支 —— 断言目标搬家了"
    sent_body = ast.unparse(sent_if)
    for must in ("set_sent(", "_open_windows[", "note_reply_sent(", "note_recent_reply("):
        assert must in sent_body, f"{must} 必须长在 sent 分支里"

    # 反向：这几个名字在函数里**只许**出现在 sent 分支内（别处冒一个出来就是漏口径）
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def _inside_sent(node: ast.AST) -> bool:
        cur: ast.AST | None = node
        while cur is not None and id(cur) in parents:
            cur = parents[id(cur)]
            if cur is sent_if:
                return True
        return False

    hits = 0
    for node in ast.walk(fn):
        # 只挑"叶子级"节点：包含 sent_if 的外层语句（try/if）会把它的正文一起 unparse 出来
        if not isinstance(node, (ast.Call, ast.Subscript, ast.Name, ast.Attribute)):
            continue
        src = ast.unparse(node)
        if not any(t in src for t in ("_open_windows", "note_reply_sent",
                                      "note_recent_reply", "set_sent")):
            continue
        hits += 1
        assert _inside_sent(node), f"{src} 出现在 sent 分支之外"
    assert hits >= 4, "一个引用都没找到 —— 断言目标搬家了（这条会假绿）"


# ============================================================ I6：最后一句话只认 MAX(sent_at)


class _SqlRecorder:
    """只记账不连库的连接替身（见 ``repo_hf.get_last_sent_at`` 的 ``conn`` 参数）。"""

    def __init__(self, value: Any = None) -> None:
        self.value = value
        self.calls: list[tuple[str, Any]] = []

    def query_value(self, sql: str, params: Any = None) -> Any:
        self.calls.append((sql, params))
        return self.value


def test_i6_last_sent_at_is_max_sent_at_never_a_status_filter():
    """**I6**：账号级"最后一次真发言" = ``MAX(sent_at)``，**不得**用 ``status='sent'`` 数。

    09-11 静默瘫 **3 天**：sweep 的到期关窗把 sent 行全推成 closed，于是"最后发言"数出来
    恒为 0，而**所有指标都正常**（这条查询的结果只喂给告警与状态页，不参与判定）。
    金丝雀一旦这么坏，就再也叫不动了。
    """
    conn = _SqlRecorder(value=None)
    assert repo_hf.get_last_sent_at("acct1", conn=conn) is None
    sql, params = conn.calls[0]
    assert "MAX(sent_at)" in sql, "必须取 MAX(sent_at)"
    assert "sent_at IS NOT NULL" in sql
    assert "status" not in sql, "不许带 status 过滤（关窗后 sent 全变 closed ⇒ 假 0）"
    assert params == ("acct1",), "账号参数要真的传下去（否则查的是全表）"

    conn2 = _SqlRecorder(value=1_700_000_000)
    assert repo_hf.get_last_sent_at("acct1", conn=conn2) == 1_700_000_000

    body = _fn_code("plugin/data/repo_hf.py", "get_last_sent_at")
    assert "status" not in body, "连函数体里都不许出现 status"
    assert "MAX(sent_at)" in body


# ============================================================ I7：禁言令过滤


#: 每个禁言令家族至少一个样本（下面还会断言"每条模式都被某个样本命中"）。
_MUTE_SAMPLES = (
    "基本不该插话的旁观者",
    "这个群里不要说话",
    "我是围观者",
    "不主动搭话",
    "群里不闲聊",
    "保持沉默即可",
    "尽量少说话",
)

#: 必须原样留下的：群性质 / 语言风格 / 话题清单（这些是资产）。
_MUTE_KEEPERS = (
    "示例数码门店群，主要聊报价与售后",
    "语气亲切、口语化，偶尔用表情",
    "宜接: 询价、以旧换新；忌接: 时事政治、医疗建议",
    "群里习惯用「老板」称呼店主",
)


def test_i7_every_mute_phrase_family_strips_but_the_topics_survive():
    """**I7**：画像里的"禁言令"必须整条丢弃，而话题性内容必须原样保留。

    09-26 的**主**根因不是洁癖：judge prompt 的三个维度都写着"结合机器人角色特点" ——
    画像的 ``我的角色定位: 基本不该插话的旁观者`` 于是成了压在 relevance + willingness +
    social（合计权重 0.65）上的**否决票**。实测同一批消息：带这两行均分 0.322（2 条过线）、
    删掉 0.581（15 条）、换成只讲身份的文本 0.669（20 条）。
    说还是不说由代码侧的阈值与预算决定，**不是画像能投票的事**。
    """
    for s in _MUTE_SAMPLES:
        assert profile.strip_hf_mute_phrases(s) == "", f"禁言令必须整条丢弃: {s!r}"
    for s in _MUTE_KEEPERS:
        assert profile.strip_hf_mute_phrases(s) == s, f"话题性内容是资产，不许误杀: {s!r}"
    assert profile.strip_hf_mute_phrases(None) == ""
    assert profile.strip_hf_mute_phrases("") == ""

    # 每条模式都得"活的"：留在表里却谁也命中不了的模式 = 以后没人敢删它
    for pat in profile.HF_MUTE_PATTERNS:
        assert any(pat.search(s) for s in _MUTE_SAMPLES), f"这条模式没有样本: {pat.pattern}"

    # 只过滤 bot_role / summary；话题清单（engage/avoid）不动，原对象也不动
    p = profile.HfGroupProfile(
        nature="门店群", style="口语化", bot_role="不该插话的旁观者",
        summary="群里主要聊报价，别插话", engage=("询价",), avoid=("政治",),
        active_hours=(9, 10))
    g = profile.guard_hf_profile_for_prompt(p)
    assert g.bot_role == "" and g.summary == ""
    assert g.engage == ("询价",) and g.avoid == ("政治",) and g.nature == "门店群"
    assert g.style == "口语化", "只过滤两个字段，其余原样"
    assert p.bot_role == "不该插话的旁观者", "原对象要原样留着（/heartflow profile 看真相）"


# ============================================================ I8：band 只抬不降且有界


def test_i8_the_profile_band_can_only_raise_and_only_within_the_cap():
    """**I8**：画像 band 是"这个群我想更克制一点"的**微调**，不是"我重新定义及格线"。

    旧码 ``max(baseThreshold, profileFloor)`` 让画像建议的 0.85/0.90 直接成为**无上界的硬
    地板** —— 而画像 prompt 只教模型"越谨慎越大"，模型没有任何裁判打分刻度的信息
    （实测"该回"档 0.61–0.82），于是连裁判认为明显该回的消息都永远越不过 ⇒ 零发言。
    """
    lo, hi = 0.5, 0.9
    for anchor in (0.5, 0.6, 0.82, 0.95):
        for band in (None, 0.0, 0.1, 0.85, 0.99, float("nan"), float("inf"), "高", -1, True):
            t, capped = profile.resolve_hf_profile_band_effect(
                anchor, band, band_min=lo, band_max=hi)
            assert math.isfinite(t), f"阈值不许变成 NaN（NaN >= 任何数恒假 ⇒ 一条都不回）"
            assert lo <= t <= hi, f"必须落在硬区间: anchor={anchor} band={band!r} → {t}"
            assert t <= anchor + profile.HF_PROFILE_MAX_RAISE + 1e-9, "抬升有上界"
            assert isinstance(capped, bool)
            if isinstance(band, (int, float)) and not isinstance(band, bool) \
                    and math.isfinite(band) and band > anchor and anchor >= lo:
                # 抬升先发生、再与硬区间取交 ⇒ 下限是 min(anchor, band_max)（不是 anchor）
                assert t >= min(anchor, hi) - 1e-9, "画像建议更高时必须抬（只是抬得有限）"

    # 脏 maxRaise 不许把阈值算成 NaN（TS 那一版会，表现是零发言）
    for bad in (float("nan"), float("inf"), -1, "x", None):
        t, _ = profile.resolve_hf_profile_band_effect(0.6, 0.9, max_raise=bad)
        assert math.isfinite(t) and lo <= t <= hi

    # 读取侧再钳一次：库里的行可能来自旧版本 / 手工修数（只信写侧 = 假设库不会被改）
    assert L.clamp_hf_threshold_to_band(0.3, lo, hi) == lo
    assert L.clamp_hf_threshold_to_band(0.99, lo, hi) == hi
    assert L.clamp_hf_threshold_to_band(0.7, lo, hi) == 0.7
    assert lo <= L.clamp_hf_threshold_to_band(float("nan"), lo, hi) <= hi, "NaN 也不许越界"


# ============================================================ I9：看图白名单


def test_i9_images_only_from_the_marked_body_and_only_http_urls(monkeypatch):
    """**I9**：只从**待判定消息**里抠 ``[图片]`` 直链、只认 ``http(s)``、去重截断。

    两个方向都危险：把本机路径/``data:`` 之外的 scheme 交给厂商 = 把内部信息当图片发出去；
    把上下文里的图也算进来 = 焦点糊掉 + 成本与超时同时上去（judge 只有 5s / 300 token 输出）。
    另有"无图时请求体逐字节不变"的一条，靠 ``tools/verify_judge.py`` 门禁守着（见其
    ``case_openai_body``）：那边比的是**真发出去的字节**，这里只管抠图规则。
    """
    txt = ("看这个\n[图片] https://oss.example.com/a.jpg\n"
           "[图片] https://oss.example.com/a.jpg\n"        # 重复
           "[图片] ftp://oss.juhe.ch/x.jpg\n"            # scheme 不对
           "[图片] ~/.hermes/.env\n"                 # 本机路径
           "[图片] data:image/png;base64,AAAA\n"         # 内联数据不算直链
           "[图片] HTTP://OSS.JUHE.CHAT/b.JPG\n")        # scheme 大小写
    assert HF.extract_hf_image_urls(txt, 5) == ["https://oss.example.com/a.jpg",
                                                "HTTP://OSS.JUHE.CHAT/b.JPG"]
    assert HF.extract_hf_image_urls(txt, 1) == ["https://oss.example.com/a.jpg"]
    assert HF.extract_hf_image_urls(txt, 0) == []
    assert HF.extract_hf_image_urls("@张三 https://oss.example.com/c.jpg", 5) == [], \
        "没有 [图片] 标记就不算图（裸链不是图）"
    assert HF.extract_hf_image_urls("", 5) == [] and HF.extract_hf_image_urls(None, 5) == []
    assert HF.HF_VISION_DEFAULTS["maxImages"] == 1, "缺省只送 1 张（成本/超时）"

    body = _fn_code("plugin/inbound/heartflow/heartflow.py", "judge_heartflow")
    assert "extract_hf_image_urls(inp.content" in body, "只从待判定消息正文取图"
    for banned in ("inp.chat_context", "inp.recent_messages", "inp.last_bot_reply"):
        assert f"extract_hf_image_urls({banned}" not in body, f"{banned} 里的图不许抠"
    assert 'vision["enabled"]' in body or "vision['enabled']" in body, \
        "关掉看图必须真的关（不是空跑一遍）"
    assert "else []" in body, "没有图 ⇒ 空列表（请求体逐字节不变，见 verify_judge 门禁）"


# ============================================================ I10：空正文必须报错


def test_i10_an_empty_body_raises_instead_of_scoring_zero():
    """**I10**：模型回了**空正文**必须显式抛错，绝不静默返回 ``""``。

    09-11 静默瘫 3 天就是这一条：reasoning 与正文共享 ``max_tokens`` ⇒ 正文恒空 ⇒
    5 维全 0 ⇒ 一条都不回。若这里静默返回 ``""``，上层看到的是"一条 0 分的有效判定"，
    台账、日志、指标全都正常。
    """
    openai_cases = (
        {},
        {"choices": []},
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
        {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
         "usage": {"completion_tokens_details": {"reasoning_tokens": 300}}},
    )
    for data in openai_cases:
        with pytest.raises(J.JudgeError) as ei:
            J._extract_openai_text(data)
        assert "empty content" in str(ei.value), "报错要说清是「空正文」（不是泛泛的失败）"
    # 截断要指明方向：思考没关 ⇒ 正文被 reasoning 吃光（09-11 的真因）
    with pytest.raises(J.JudgeError) as ei:
        J._extract_openai_text({"choices": [{"message": {"content": ""},
                                             "finish_reason": "length"}],
                                "usage": {"completion_tokens_details": {"reasoning_tokens": 300}}})
    assert "reasoning_tokens=300" in str(ei.value) and "max_tokens" in str(ei.value)

    anthropic_cases = (
        {},
        {"content": []},
        {"content": [{"type": "text", "text": ""}], "stop_reason": "max_tokens"},
        {"content": [{"type": "tool_use", "id": "x"}]},
    )
    for data in anthropic_cases:
        with pytest.raises(J.JudgeError):
            J._extract_anthropic_text(data)

    # 反向：非空照常返回（防"一律 raise"这种过度修正 —— 那会让 judge 永远不可用）
    assert J._extract_openai_text({"choices": [{"message": {"content": "{}"}}]}) == "{}"
    assert J._extract_anthropic_text(
        {"content": [{"type": "text", "text": "hi"}, {"type": "text", "text": "no"}]}) == "hi"

    # 上层：坏/空响应 ⇒ 降级为"不回复"（None），不许降级成"0 分判定"
    assert HF.parse_response("", {}) is None
    assert HF.parse_response("不是 JSON", {}) is None
    assert HF.parse_response("[1,2,3]", {}) is None, "数组不是我们要的形状（走重试）"


# ============================================================ I11：0–1 尺度


def test_i11_the_default_weights_sum_to_one_and_overall_stays_in_unit_scale():
    """**I11**：``overall`` 与账号级 ``replyThreshold`` **同一个尺度**（0–1）。

    尺度错位的表现是"阈值看起来配了但永远/从不触发"，且没有任何报错。
    缺省权重**逐键回落**（不做归一化 —— 那是 TS 的行为，见 ``resolve_hf_weights``）：
    所以可断言的是"缺省权重之和为 1"，而不是"任意配置都归一化"。
    """
    assert abs(sum(HF.resolve_hf_weights(None).values()) - 1.0) < 1e-9
    w = HF.resolve_hf_weights({"weights": {"relevance": float("nan"), "willingness": -3,
                                           "social": "x", "timing": True}})
    assert all(math.isfinite(v) and v >= 0 for v in w.values()), \
        "脏权重不许变成 NaN/负数（NaN 让 overall 恒 NaN ⇒ 一条都不回且不报错）"

    for raw in ({k: 10 for k in HF.HF_DIMENSIONS},
                {k: 0 for k in HF.HF_DIMENSIONS},
                {k: 999 for k in HF.HF_DIMENSIONS},
                {k: -5 for k in HF.HF_DIMENSIONS},
                {"relevance": "8", "willingness": True, "social": None}):
        r = HF.parse_response(json.dumps(raw, ensure_ascii=False), {})
        assert r is not None, f"这是有效 JSON 对象，必须解析出来: {raw}"
        assert 0.0 <= r.overall_score <= 1.0, f"overall 越界: {r.overall_score}"
        assert all(0.0 <= v <= 10.0 for v in r.dimensions.values()), "维度分钳到 0–10"

    full = HF.parse_response(json.dumps({k: 10 for k in HF.HF_DIMENSIONS}), {})
    assert full is not None and full.overall_score == pytest.approx(1.0, abs=1e-9)
    assert full.should_reply is True, "满分必须过线"
    zero = HF.parse_response(json.dumps({k: 0 for k in HF.HF_DIMENSIONS}), {})
    assert zero is not None and zero.overall_score == 0.0 and zero.should_reply is False

    # 阈值唯一取法 + 读取侧 0–1（-0.2 会让"恒真"、2.0 会让"恒假"，都不报错）
    for bad in (-1, 2, 3.5, "0.7", None, float("nan"), True):
        t = L.resolve_reply_threshold({"replyThreshold": bad})
        assert 0.0 <= t <= 1.0, f"阈值越界: {bad!r} → {t}"
    assert L.resolve_reply_threshold({"replyThreshold": 0.42}) == 0.42


# ============================================================ 完整性守卫（第 12、13 条）


def _covered_ids() -> set[int]:
    out: set[int] = set()
    for name, obj in list(globals().items()):
        if not name.startswith("test_i") or not callable(obj):
            continue
        m = re.match(r"test_i(\d+)_", name)
        if m:
            out.add(int(m.group(1)))
    return out


def test_i12_every_invariant_id_has_a_test_here():
    """**完整性 ①**：1..11 每一条都必须在本文件里有用例。

    没有这条，"某条不变量测试被删掉"是无声的 —— 文件照样全绿，而那条不变量
    从此只活在 docstring 里（docstring 不会在回归时拦人）。
    """
    expected = set(range(1, 12))
    assert set(HOMES) == expected, "老家表本身必须齐全（它是这条守卫的真源）"
    covered = _covered_ids() - {12, 13}      # 12/13 就是本节这两条守卫自己
    assert covered <= expected, f"编号越界（写错了 id？）: {sorted(covered - expected)}"
    missing = sorted(expected - covered)
    assert not missing, f"这些不变量没有可执行断言: {missing}"


def test_i13_every_invariant_is_marked_in_its_source_home():
    """**完整性 ②**：每条在源码老家（:data:`HOMES`）里必须留下 ``INVARIANT: I<n>`` 标记。

    读代码的人不该只有"跑 589 个测试"这一条路知道这里有约束。标记被重构顺手删掉，
    下一次改动就不会有人想起查这里 —— 所以标记本身也上闸。
    """
    missing: list[str] = []
    for n, homes in sorted(HOMES.items()):
        pat = re.compile(rf"INVARIANT:?\s*\**\s*I{n}\b")
        if not any(pat.search(_src(h)) for h in homes):
            missing.append(f"I{n} ← {list(homes)}")
    assert not missing, "这些不变量在源码里没有标记: " + "; ".join(missing)

    # 反向：标记不该漫天飞（每条不变量最多出现在它声明的老家 + 模块 docstring 的引用）
    found: dict[int, list[str]] = {}
    for rel in (_all_py_sources()):
        for m in _MARK_RE.finditer(_src(rel)):
            found.setdefault(int(m.group(1)), []).append(rel)
    stray = sorted(n for n in found if n not in HOMES)
    assert not stray, f"源码里出现了没登记的不变量编号: {stray}（要么补进 HOMES，要么改掉）"


def _all_py_sources() -> list[str]:
    """仓内全部 ``plugin/**.py``（相对路径）—— 用来做"标记满天飞"的反向扫描。"""
    return sorted(str(p.relative_to(ROOT))
                  for p in (ROOT / "plugin").rglob("*.py")
                  if "__pycache__" not in p.parts)


def test_lingering_legacy_vars_are_scoped_not_process_env(monkeypatch):
    """旧名检测必须走 **scoped 凭据面**，不能读 ``os.environ``。

    multiplex 下 ``os.environ`` 装的是 launch profile（default）那一份：wpp-wechat
    已经删掉 ``DEEPSEEK_API_KEY``，用 os.environ 判断却会**永远**报残留（2026-10-19
    实测）。反过来同样危险 —— 真残留时若读到 default 的空值就会漏报。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "process-level-leak")
    monkeypatch.setattr(J, "secret_env", lambda name, default="": "")
    assert J.lingering_legacy_judge_vars() == [], "进程 env 有值不算残留（串号）"

    monkeypatch.setattr(
        J, "secret_env",
        lambda name, default="": "k" if name == "DEEPSEEK_API_KEY" else "")
    assert J.lingering_legacy_judge_vars() == ["DEEPSEEK_API_KEY"], "scoped 面有值必须点名"
