"""``_build_event`` 的契约：``WppMessage`` → Hermes ``MessageEvent``。

这是入站链路的**最后一跳**，把本地解析出来的消息交给 Hermes 的 runner。原来
``adapter.py`` 只覆盖 34%，这块函数（80 行）一条用例都没有。

值得测的不是 happy path，而是**几个"错位"**——它们都不会报错，只会让行为悄悄变形：

1. **``media`` 是必填关键字参数**（调用方 ④c 已取过，全链路只允许一个下载点）。
   写成可选会让"忘了传"在运行时悄悄多下一次媒体；
2. **``reply_to_*`` 的五个字段各自独立兜底**：``svrid`` 空但 ``quote_content`` 有值时，
   引用正文要保住（否则模型看到"引用了什么"是空的）；
3. **``reply_to_is_own_message``** —— 引用的是 bot 自己的话时，agent 的回复策略不同
   （不能自己跟自己演对话）；
4. **群名/昵称解析失败不能挡消息** —— 冷启动、DB 未起时应该退回 wxid 照常派发；
5. **``reply_expected`` 三态**：``True``/``None`` 会生成兜底文案，``False`` 是"可以沉默"
   （心流主动搭话专用）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugin.inbound.parser import WppMessage
from plugin.platform.adapter import WppAdapter

SELF = "wxid_bot"


def _adapter() -> WppAdapter:
    a = object.__new__(WppAdapter)          # 跳过 __init__（要 PlatformConfig + 账号表）
    a.platform = MagicMock(name="wechatpadpro")
    a.gateway_runner = None
    a._owner_profile = None
    a._chat_id = lambda account_id, peer_id: f"{account_id}:{peer_id}"
    a._channel_prompt = MagicMock(return_value=None)
    a._intent_channel_context = AsyncMock(return_value=None)
    return a


def _acct() -> MagicMock:
    acct = MagicMock()
    acct.id = "default"
    acct.self_wxid = SELF
    acct.nickname = "机器人"
    return acct


def _msg(**over) -> WppMessage:
    base = dict(account_id="default", msg_id="m1", from_wxid="wxid_user",
                from_nickname="老王", content="你好")
    base.update(over)
    return WppMessage(**base)


def _pipeline(text: str = "你好") -> MagicMock:
    pipe = MagicMock()
    pipe.prepare_text.return_value = text
    return pipe


@pytest.mark.asyncio
async def test_builds_a_group_event_with_resolved_names():
    adapter = _adapter()
    msg = _msg(chatroom_id="123@chatroom", content="在吗")
    with patch("plugin.inbound.session_title.chat_display_name", return_value="示例华为群"), \
         patch("plugin.inbound.session_title.sender_display_name", return_value="老王"):
        event = await adapter._build_event(_acct(), msg, _pipeline("在吗"), media=([], []))

    assert event is not None
    assert event.text == "在吗"
    assert event.source.chat_name == "示例华为群"
    assert event.source.chat_type == "group"
    assert event.user_name == "老王"
    assert event.message_id == "m1"


@pytest.mark.asyncio
async def test_dm_event_has_dm_chat_type():
    adapter = _adapter()
    with patch("plugin.inbound.session_title.chat_display_name", return_value="老王"), \
         patch("plugin.inbound.session_title.sender_display_name", return_value="老王"):
        event = await adapter._build_event(_acct(), _msg(), _pipeline(), media=([], []))
    assert event.source.chat_type == "dm"


@pytest.mark.asyncio
async def test_title_lookup_failure_does_not_block_the_message():
    """冷启动 / DB 未起时退回 wxid —— 名字只影响展示，绝不能挡住派发。"""
    adapter = _adapter()
    msg = _msg(chatroom_id="123@chatroom")
    with patch("plugin.inbound.session_title.chat_display_name",
               side_effect=RuntimeError("db down")):
        event = await adapter._build_event(_acct(), msg, _pipeline(), media=([], []))

    assert event is not None, "名字解析失败不能返回 None"
    assert event.source.chat_name, "必须有一个兜底名字（wxid/群 id）"


@pytest.mark.asyncio
async def test_media_urls_and_types_are_passed_through():
    adapter = _adapter()
    with patch("plugin.inbound.session_title.chat_display_name", return_value="x"), \
         patch("plugin.inbound.session_title.sender_display_name", return_value="y"):
        event = await adapter._build_event(
            _acct(), _msg(media={"type": "image"}), _pipeline(),
            media=(["https://oss/a.jpg"], ["image"]))

    assert event.media_urls == ["https://oss/a.jpg"]
    assert event.media_types == ["image"]


@pytest.mark.asyncio
async def test_quoted_message_fields_are_mapped():
    adapter = _adapter()
    quoted = {"svrid": "q-1", "quote_content": "被引用的那句话",
              "from_wxid": "wxid_other", "display_name": "小李"}
    msg = _msg(reply_to=quoted)
    with patch("plugin.inbound.session_title.chat_display_name", return_value="x"), \
         patch("plugin.inbound.session_title.sender_display_name", return_value="y"):
        event = await adapter._build_event(_acct(), msg, _pipeline(), media=([], []))

    assert event.reply_to_message_id == "q-1"
    assert event.reply_to_text == "被引用的那句话"
    assert event.reply_to_author_id == "wxid_other"
    assert event.reply_to_author_name == "小李"
    assert event.reply_to_is_own_message is False


@pytest.mark.asyncio
async def test_quoting_our_own_message_is_flagged():
    """引用的是 bot 自己的话 —— agent 不能自己跟自己演对话。"""
    adapter = _adapter()
    msg = _msg(reply_to={"svrid": "q-2", "from_wxid": SELF})
    with patch("plugin.inbound.session_title.chat_display_name", return_value="x"), \
         patch("plugin.inbound.session_title.sender_display_name", return_value="y"):
        event = await adapter._build_event(_acct(), msg, _pipeline(), media=([], []))

    assert event.reply_to_is_own_message is True


@pytest.mark.asyncio
async def test_missing_quote_fields_become_none():
    adapter = _adapter()
    with patch("plugin.inbound.session_title.chat_display_name", return_value="x"), \
         patch("plugin.inbound.session_title.sender_display_name", return_value="y"):
        event = await adapter._build_event(_acct(), _msg(), _pipeline(), media=([], []))

    assert event.reply_to_message_id is None
    assert event.reply_to_text is None
    assert event.reply_to_is_own_message is False


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [None, True, False])
async def test_reply_expected_three_states_are_preserved(expected):
    """``False`` = 可以沉默（心流主动搭话）；``None``/``True`` = 会生成兜底文案。"""
    adapter = _adapter()
    with patch("plugin.inbound.session_title.chat_display_name", return_value="x"), \
         patch("plugin.inbound.session_title.sender_display_name", return_value="y"):
        event = await adapter._build_event(
            _acct(), _msg(), _pipeline(), media=([], []), reply_expected=expected)
    assert event.reply_expected is expected


@pytest.mark.asyncio
async def test_channel_prompt_and_context_are_wired():
    adapter = _adapter()
    adapter._channel_prompt = MagicMock(return_value="本群风格：简短")
    adapter._intent_channel_context = AsyncMock(return_value="最近聊天：…")
    with patch("plugin.inbound.session_title.chat_display_name", return_value="x"), \
         patch("plugin.inbound.session_title.sender_display_name", return_value="y"):
        event = await adapter._build_event(_acct(), _msg(), _pipeline(), media=([], []))

    assert event.channel_prompt == "本群风格：简短"
    assert event.channel_context == "最近聊天：…"
