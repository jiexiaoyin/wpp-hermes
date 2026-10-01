"""V7（★）—— 心流**热路径零 DB IO**。

## 为什么这条要单独一个文件

心流热路径（每一条群消息都过）由三段落组成：

    check_gate()  →  judge_heartflow()  →  maybe_engage() 的编排段

三段都必须**只吃内存缓存**：画像、预算、重复历史、生效阈值全由 sweep 每轮用一条查询
预热，判定时一个人都不许回库。

## 它防的是哪一种坏

"缓存冷掉了顺手查一下库补齐" —— 这种退化**不会报错、不会少回复、测试全绿**，
只会让每条群消息多一次 DB 往返。等发现时 QPS 已经在暗处掉了。
所以这里不比行为，只**数**：``plugin/data/db.py`` 是全部 DB 访问的必经之地，
``db_call_count()`` 就是给这一刻准备的仪器（见该模块「V7 仪器」一节）。

## 仪器的自证（别省这一步）

一个只会返回 0 的计数器是**不可伪证**的 —— 热路径真的查了库、计数器坏了，
两者在断言层面长得一模一样。所以 §0 先证明仪器**会响**：手动调一次 ``get_conn()``
必须让计数 +1，然后才允许它替热路径作证。

## ⚠️ 什么不算"热路径"

台账三写入（``persist_hf_judged`` / ``_below_threshold`` / ``_send_outcome``）与
sweep 预热**本来就该写库/读库**，它们是**计数器外面**的调用者：
本文件把台账写成 stub（``ledger_recorder``），不假装它们不存在。
"""

from __future__ import annotations

import asyncio

import pytest

from plugin.data import db as DB
from plugin.inbound.heartflow import budget as B
from plugin.inbound.heartflow import heartflow as HF
from plugin.inbound.heartflow import learn as L
from plugin.inbound.heartflow import profile as P

from .test_heartflow import (FakeAcct, FakeAdapter, FakeMsg, NOW_MS, NOW_SEC,  # noqa: F401
                             cfg_on, creds, fake_judge, good_json, ledger_recorder)


@pytest.fixture(autouse=True)
def _cold_caches():
    """**冷缓存**开局 —— 热路径"顺手查库补齐"只可能发生在缓存为空的时候。

    缓存热着跑，这条测试就永远是绿的：查库分支根本走不到。
    """
    for reset in (P.reset_hf_profile_cache, L.reset_learned_threshold_cache,
                  B.reset_budget_cache, HF.reset_heartflow_cache):
        reset()
    yield


def _count(fn):
    """跑一段热路径，返回它消耗的 DB 调用次数。"""
    DB.reset_db_call_count()
    fn()
    return DB.db_call_count()


# ===== 0. 仪器自证 =====


def test_the_counter_actually_counts():
    """仪器会响吗？——**先证明这一点**，否则后面三条断言全是空的。

    这一条也是 ``db._note_call()`` 被改成空函数的唯一杀手：
    计数器一哑，热路径那三条会**假装全绿**。
    """
    DB.reset_db_call_count()
    assert DB.db_call_count() == 0, "归零失败 ⇒ 后面的断言没有基线"
    try:
        DB.get_conn()
    except Exception:  # noqa: BLE001 —— 本地没装 pymysql / 连不上库都算"碰了一下库"
        pass
    assert DB.db_call_count() == 1, "调了一次 get_conn 却不计数 ⇒ 仪器是坏的"
    DB.reset_db_call_count()


def test_the_counter_survives_reset():
    DB.reset_db_call_count()
    DB._note_call()
    DB._note_call()
    assert DB.db_call_count() == 2
    DB.reset_db_call_count()
    assert DB.db_call_count() == 0


# ===== 1. 闸门（同步预筛）=====


def test_i_v7_check_gate_makes_no_db_calls():
    """``check_gate`` 是每条群消息的第一道门 —— 它里面一次库都不许碰。

    预算判定用的是内存 ``peek``（真计数落在发送成功时），画像/阈值走缓存。
    """
    cfg = cfg_on(whitelistGroups=["g1@chatroom"])
    n = _count(lambda: HF.check_gate("g1@chatroom", "这批货什么时候到", cfg, NOW_MS,
                                     account_id="acc1", candidate_at_sec=NOW_SEC))
    assert n == 0, f"check_gate 碰了 {n} 次库 —— 闸门必须纯内存"


@pytest.mark.parametrize("content,cfg_over", [
    ("这批货什么时候到", {}),                     # 放行路径
    ("", {}),                                      # 空消息（拦）
    ("在吗", {"enabled": False}),                  # 关闭（拦）
    ("在吗", {"whitelistGroups": ["other"]}),      # 白名单外（拦）
])
def test_i_v7_check_gate_no_db_on_every_branch(content, cfg_over):
    """**每条分支**都不许碰库 —— 只验放行那条的话，"拦下顺手记一笔"就漏了。"""
    over = {"whitelistGroups": ["g1@chatroom"]}
    over.update(cfg_over)
    cfg = cfg_on(**over)
    assert _count(lambda: HF.check_gate("g1@chatroom", content, cfg, NOW_MS,
                                       account_id="acc1")) == 0


# ===== 2. judge =====


def test_i_v7_judge_makes_no_db_calls(monkeypatch):
    """``judge_heartflow`` 全程零库（LLM 用假件）：prompt 只由 ``inp`` + ``cfg`` 拼。"""
    fake_judge(monkeypatch, [good_json()])
    inp = HF.HfJudgeInput(chat_id="g1@chatroom", content="这批货什么时候到",
                          sender_name="阿丽", chat_context="[文本] 阿丽: 在吗")
    n = _count(lambda: asyncio.run(HF.judge_heartflow(inp, cfg_on(), creds())))
    assert n == 0, f"judge 碰了 {n} 次库"


def test_i_v7_judge_with_cold_profile_cache_makes_no_db_calls(monkeypatch):
    """★ 冷画像缓存 + 判定的组合：这正是"顺手查库补齐画像"会发作的场合。

    画像预热由 sweep 负责；judge 读不到就**不注入**，绝不允许自己回库拿
    （``profile.get_hf_profile_prompt_text`` 的契约就是"不查 DB"）。
    """
    fake_judge(monkeypatch, [good_json()])
    P.reset_hf_profile_cache()
    assert P.get_hf_profile_prompt_text("acc1", "g1@chatroom") is None, "前提：缓存是冷的"
    inp = HF.HfJudgeInput(chat_id="g1@chatroom", content="这批货什么时候到")
    assert _count(lambda: asyncio.run(HF.judge_heartflow(inp, cfg_on(), creds()))) == 0


# ===== 3. 整条编排 =====


def test_i_v7_maybe_engage_makes_no_db_calls(monkeypatch):
    """整条编排（闸门 → 调画像/预算/阈值缓存 → judge → 交付）一个库调用都不许有。

    台账三写入被换成 stub（它们**本来就该**写库，不属于热路径）。
    """
    called = fake_judge(monkeypatch, [good_json()])
    ledger_recorder(monkeypatch)                      # 台账写入 → 记录器
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())

    async def _no_send(*a, **kw):
        return None

    monkeypatch.setattr(HF, "deliver_hf_reply", _no_send, raising=False)
    n = _count(lambda: asyncio.run(HF.maybe_engage(FakeAdapter(), FakeAcct(), FakeMsg())))
    assert called, "编排连 judge 都没调到 ⇒ 这条断言没验到东西（先修用例）"
    assert n == 0, f"maybe_engage 碰了 {n} 次库"


def test_i_v7_the_ledger_write_really_touches_the_db():
    """反面钉：台账写入**确实**碰库 —— 它就在热路径之外的界上。

    这条钉两件事：

    1. ``persist_hf_judged`` 没被改成"静默丢弃"（本地无 pymysql ⇒ 真写会抛，
       但 ``get_conn()`` **先计数后连接**，所以计数照样 +1）；
    2. 计数器证明"库调用在本进程里是可观测的" —— 有了这一条，
       :func:`test_i_v7_maybe_engage_makes_no_db_calls` 的 0 才**有意义**
       （否则 0 与"仪器坏了"无法区分，见 §0）。
    """
    rec = L.HfLedgerRecord(account_id="acc1", inbound_msg_id="m100",
                           group_id="g1@chatroom", judged_at=NOW_SEC, judge_overall=0.8)
    DB.reset_db_call_count()
    L.persist_hf_judged(rec)      # 本机没 pymysql ⇒ 内部 try 吞掉并 warn，不影响断言
    assert DB.db_call_count() >= 1, \
        "台账写入没碰库 ⇒ 要么被改成静默丢弃，要么边界被挪进热路径了"
