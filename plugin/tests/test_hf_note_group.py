"""``_hf_note_group`` 的契约：心流旁路观测的**记账口径**。

这块函数只有 30 行，但它是 judge 判"要不要搭话"的**唯一上下文来源**。口径错了不会
报错，只会让判断悄悄变形：

* **系统通知 / 撤回进缓冲** → judge 的"最近 N 条上下文"里混进
  「xxx 撤回了一条消息」「你已加入群聊」，而且会刷新"有人说话"的沉默时长
  ⇒ 预算以为群里一直很热闹，该搭话的时候不搭；
* **@ / 引用判据与触发判定不同源** → "触发了但候选标记为假"，judge 看不到
  "这条是在叫我"这个事实；
* **旁路抛错挡住收信** → 心流模块没装 / DB 未起时整条消息派发失败。

所以这里钉四件事：系统帧丢弃、正常帧入缓冲且参数正确、判据同源、异常不外溢。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from plugin.inbound.parser import MT_REVOKE, MT_SYSTEM, WppMessage
from plugin.platform.adapter import WppAdapter

SELF = "wxid_bot"


def _adapter(hf) -> WppAdapter:
    a = object.__new__(WppAdapter)
    a._hf_mod = lambda: hf          # 静态方法，实例上覆盖成 lambda 即可
    return a


def _acct() -> MagicMock:
    acct = MagicMock()
    acct.id = "default"
    acct.self_wxid = SELF
    acct.nickname = "机器人"
    return acct


def _msg(**over) -> WppMessage:
    base = dict(account_id="default", msg_id="m1", from_wxid="wxid_user",
                from_nickname="老王", chatroom_id="123@chatroom",
                content="今晚吃什么", timestamp=1700000000)
    base.update(over)
    return WppMessage(**base)


def test_missing_heartflow_module_is_a_silent_noop():
    """心流模块没装时旁路直接跳过 —— 不能因为观测失败挡住收信。"""
    adapter = _adapter(None)
    adapter._hf_note_group(_acct(), _msg(), is_bot=False)   # 不抛即可


def test_system_notice_is_not_buffered():
    """「xxx 撤回了一条消息」不是人说的话 —— 进缓冲会污染 judge 上下文。"""
    hf = MagicMock()
    _adapter(hf)._hf_note_group(_acct(), _msg(msg_type=MT_SYSTEM), is_bot=False)
    hf.note_group_message.assert_not_called()


def test_revoke_notice_is_not_buffered():
    hf = MagicMock()
    _adapter(hf)._hf_note_group(_acct(), _msg(msg_type=MT_REVOKE), is_bot=False)
    hf.note_group_message.assert_not_called()


def test_normal_message_is_buffered_with_the_right_fields():
    hf = MagicMock()
    msg = _msg()
    _adapter(hf)._hf_note_group(_acct(), msg, is_bot=False)

    hf.note_group_message.assert_called_once()
    kw = hf.note_group_message.call_args.kwargs
    assert hf.note_group_message.call_args.args[:2] == ("default", "123@chatroom")
    assert kw["sender_name"] == "老王"
    assert kw["content"] == "今晚吃什么"
    assert kw["ts_sec"] == 1700000000.0
    assert kw["is_bot"] is False


def test_missing_timestamp_falls_back_to_now():
    """``timestamp=0`` 的帧用当前时间 —— 否则这条消息在缓冲里永远是"很久以前"。"""
    hf = MagicMock()
    before = time.time()
    _adapter(hf)._hf_note_group(_acct(), _msg(timestamp=0), is_bot=False)
    ts = hf.note_group_message.call_args.kwargs["ts_sec"]
    assert ts >= before


def test_sender_name_falls_back_to_wxid():
    hf = MagicMock()
    _adapter(hf)._hf_note_group(_acct(), _msg(from_nickname=""), is_bot=False)
    assert hf.note_group_message.call_args.kwargs["sender_name"] == "wxid_user"


def test_mentions_bot_uses_the_same_predicate_as_triggering():
    """@ 判据必须与触发判定同源 —— 否则会出现"触发了但候选标记为假"。"""
    hf = MagicMock()
    _adapter(hf)._hf_note_group(
        _acct(), _msg(content="@机器人 在吗"), is_bot=False)
    assert hf.note_group_message.call_args.kwargs["mentions_bot"] is True


def test_plain_message_does_not_flag_a_mention():
    hf = MagicMock()
    _adapter(hf)._hf_note_group(_acct(), _msg(content="今天天气不错"), is_bot=False)
    assert hf.note_group_message.call_args.kwargs["mentions_bot"] is False


def test_quoting_the_bot_is_flagged():
    hf = MagicMock()
    msg = _msg(reply_to={"from_wxid": SELF, "quote_content": "我说过的话"})
    _adapter(hf)._hf_note_group(_acct(), msg, is_bot=False)
    assert hf.note_group_message.call_args.kwargs["quotes_bot"] is True


def test_buffering_failure_does_not_propagate():
    """观测是旁路 —— 它炸了不许影响消息派发。"""
    hf = MagicMock()
    hf.note_group_message.side_effect = RuntimeError("buffer down")
    _adapter(hf)._hf_note_group(_acct(), _msg(), is_bot=False)   # 不抛即可


def test_bot_own_message_is_marked_is_bot():
    hf = MagicMock()
    _adapter(hf)._hf_note_group(_acct(), _msg(from_wxid=SELF), is_bot=True)
    assert hf.note_group_message.call_args.kwargs["is_bot"] is True
