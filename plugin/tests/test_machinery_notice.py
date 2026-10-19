"""出站「内部机械通知」闸（2026-10-18 生产事故）。

现场：移动业务对接群里漏出两条网关内部文案 ——
① 静默兜底的英文报错 ``⚠️ The model returned only a silence marker...``，
② 后台自审摘要 ``💾 Self-improvement review: ...``。
两条都不是 agent 对客户说的话，**发出去撤不回**。

本文件钉三件事：
1. 判据命中各种 locale 形态；
2. 判据**不误伤**正常业务话术；
3. ``WppAdapter.send`` 两条路径（原文 / 摘掉指向自己的 @ 之后）都真的拦得住，
   且正常文本照旧往下走。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from plugin.platform import outbound


@pytest.mark.parametrize("text", [
    "⚠️ The model returned only a silence marker. Nothing to send.",
    "模型只返回了静默标记",
    "模型只回傳了靜默標記",
    "💾 Self-improvement review: 3 notes",
    "💾 自我改进回顾：本轮无事",
    "自我改進檢閱",
    "🧠 MemOS — recalled 9 memories",      # 记忆召回生命周期状态（非模型输出）
    "👁️ Hindsight — recalled 2 memories",
    "前缀\n🧠 MemOS — recalled 9 memories\n后缀",  # 多行块里的独立一行
    "⏳ 已排队至下一回合。当前任务完成后我会回复。",   # 忙碌回执（无 _interim_send 标记）
    "⚡ 正在中断当前任务。我很快就会回复你的消息。",
    "⏳ 网关正在重启——已排队，恢复后的下一回合处理。",
    "⏳ Queued for the next turn. I'll respond once the current task finishes.",
    "⏩ Steered into current run. Your message arrives after the next tool call.",
])
def test_machinery_notice_matches_known_forms(text):
    assert outbound.is_machinery_notice(text)


@pytest.mark.parametrize("text", [
    "空调今天到货，下午可以来拿",
    "无 @提及，按群规则不回复",          # 这是拒绝话术，不是机械通知
    "我 recalled 9 memories 里的那条记录", # 句中出现不能误伤（必须整行形状）
    "已排队至下一回合前，请先把上一单出库",   # 不带回执 glyph 的正常业务句
    "正在中断当前任务的客户，请稍等",         # 同上：没有行首 glyph 不匹配
    "",                                      # ⚠️ 空输入
    None,
    # ★ 2026-10-19：💾 是自审摘要的**行首标记**，不是关键词 —— 旧判据里裸 ``|💾``
    #   会把下面这些正常业务话术整条静默丢弃（用户永远等不到回复）。
    "记得备份💾",
    "今天出单 12 台💾 记录一下",
])
def test_machinery_notice_does_not_match_business_text(text):
    assert not outbound.is_machinery_notice(text)


@pytest.mark.parametrize("text", [
    "无 @提及，按群规则不回复",
    "无@提及,按群规则不回复",
    "不回复：没有需要回应的内容",
    "前缀\n  不回复：本轮无事",
])
def test_rejection_phrase_matches_the_meta_statements(text):
    assert outbound.is_rejection_phrase(text)


@pytest.mark.parametrize("text", [
    # ★ 2026-10-19：裸「无 @提及」不再单独命中 —— 它可能只是业务描述的一部分，
    #   而命中 = 静默丢弃（用户永远不知道为什么没回）。
    "这个客户无 @提及 需求，我们先不回复",
    "群里没人 @ 你，这条不用管",
    "",
    None,
])
def test_rejection_phrase_does_not_match_business_text(text):
    assert not outbound.is_rejection_phrase(text)


@pytest.fixture
def bare(monkeypatch, stubbed):
    """绕过 Base 的重构造，只装 ``send`` 早退路径需要的属性。

    延迟 import：``adapter`` 模块级 ``from gateway.config import Platform``，
    必须等 ``stubbed`` 空壳装上（见 conftest）之后才 import 得动。
    空壳里的 ``SendResult`` 是"不接受参数"的占位类，这里换成记录字段的真工厂，
    否则任何 ``return SendResult(...)`` 都会 TypeError，测的就不是闸而是桩。
    """
    from plugin.platform import adapter as adapter_mod

    class _SendResult:
        def __init__(self, success=True, error=None, error_kind=None,
                     message_id=None, **rest):
            self.success = success
            self.error = error
            self.error_kind = error_kind
            self.message_id = message_id
            for k, v in rest.items():
                setattr(self, k, v)

    monkeypatch.setattr(adapter_mod, "SendResult", _SendResult)
    inst = object.__new__(adapter_mod.WppAdapter)
    inst._accounts = {
        adapter_mod.DEFAULT_ACCOUNT: SimpleNamespace(
            id=adapter_mod.DEFAULT_ACCOUNT,
            self_wxid="wxid_self",
            nickname="益融小助理",
        ),
    }
    inst._clients = {}
    return inst


def test_send_drops_machinery_notice_on_raw_text(bare):
    result = asyncio.run(bare.send(
        "room_test@chatroom", "⚠️ The model returned only a silence marker."))
    assert result.success is True
    assert result.message_id is None


def test_send_drops_machinery_notice_after_stripping_self_mention(bare):
    """``@自己 ⚠️ ...`` —— 原文不命中，摘掉 @ 前缀后必须补拦。"""
    result = asyncio.run(bare.send(
        "room_test@chatroom",
        "@益融小助理 💾 Self-improvement review: x"))
    assert result.success is True
    assert result.message_id is None


def test_send_lets_normal_text_through_the_guards(bare):
    """不误伤：正常文本应继续走到「账号未连接」，而不是被静默丢弃。"""
    result = asyncio.run(bare.send("room_test@chatroom", "空调今天到货，下午来拿"))
    assert result.success is False
    assert "未连接" in (result.error or "")
