"""心流原始缓冲（``_hf_note_group``）的**采集面**。

2026-10-19：系统通知（``MT_SYSTEM``：撤回提示 / 入群提示等）与 ``MT_REVOKE``
都曾进缓冲 —— judge 的"最近 N 条上下文"被通知污染，而"有人说话"的沉默时长也被
刷新（预算以为群里一直有人聊）。撤回在正常路径上已被 ``should_process`` 拦掉，
这里再兜一道，防止未来换入口绕过触发判定。

适配器在空壳环境（``conftest.stubbed``）里能真跑，所以这条按**行为**验，不按源码形状。
"""

from __future__ import annotations

import types

import pytest

from plugin.inbound.parser import MT_REVOKE, MT_SYSTEM, MT_TEXT, WppMessage

SELF = "wxid_bot"


def _msg(**kw) -> WppMessage:
    base = dict(account_id="acct", msg_id="m1", from_wxid="wxid_friend",
                to_wxid=SELF, content="在吗", msg_type=MT_TEXT,
                chatroom_id="12345@chatroom")
    base.update(kw)
    return WppMessage(**base)


@pytest.fixture(scope="module")
def adapter(stubbed):  # noqa: F811 —— 空壳环境来自 conftest
    import plugin.platform.adapter as A
    return A


def _install(monkeypatch, adapter, calls: list[dict]):
    hf = types.SimpleNamespace(
        note_group_message=lambda *a, **kw: calls.append({"args": a, **kw}))
    monkeypatch.setattr(adapter.WppAdapter, "_hf_mod", staticmethod(lambda: hf))
    acct = types.SimpleNamespace(id="acct", self_wxid=SELF, nickname="小助理")
    return object.__new__(adapter.WppAdapter), acct


@pytest.mark.parametrize("msg_type,content", [
    (MT_SYSTEM, "你已加入群聊"),
    (MT_SYSTEM, "「张三」撤回了一条消息"),
    (MT_REVOKE, "「李四」撤回了一条消息"),
])
def test_system_and_revoke_messages_never_reach_the_buffer(
        monkeypatch, adapter, msg_type, content) -> None:
    calls: list[dict] = []
    inst, acct = _install(monkeypatch, adapter, calls)
    inst._hf_note_group(acct, _msg(msg_type=msg_type, content=content), is_bot=False)
    assert calls == [], "系统通知/撤回不是人说的话，不许进心流缓冲"


def test_a_real_human_message_is_still_buffered(monkeypatch, adapter) -> None:
    """反向判据：排除法不许把正常群消息一起吃掉。"""
    calls: list[dict] = []
    inst, acct = _install(monkeypatch, adapter, calls)
    inst._hf_note_group(acct, _msg(content="这个多少钱"), is_bot=False)
    assert len(calls) == 1
    assert calls[0]["content"] == "这个多少钱"
    assert calls[0]["mentions_bot"] is False
