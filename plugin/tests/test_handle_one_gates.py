"""``_handle_one`` 入站链路的前置门禁契约。

``adapter.py`` 的覆盖率只有 32%，缺口主要在 ``_handle_one`` 这条 170 行的编排链上。
这里只钉**最早返回的三个门**（①自消息 ②去重 ③空体）——它们的共同点是
**一旦命中就不该碰到任何下游副作用**（不落库、不下载媒体、不进心流缓冲），
所以可以用真实方法 + 假依赖来测，不需要 DB / 网络 / LLM。

为什么这三个门值得单测：它们全是**静默丢弃**（debug 日志），线上表现是"消息没反应"，
靠日志很难区分是"门拦了"还是"链路断了"。改坏任何一条的代价都很高：

* ①坏了 ⇒ vendor 回推自己的消息 → 自问自答；
* ① 的 filehelper 豁免丢了 ⇒ 管理命令（``/heartflow on``）永远进不来；
* ②坏了 ⇒ vendor 重放的同一条消息被二次回答（微信里撤不回）；
* ③坏了 ⇒ 心跳噪声进 LLM，每条都白花一次推理。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin.inbound.parser import WppMessage
from plugin.platform.adapter import WppAdapter

SELF = "wxid_bot"
PEER = "wxid_user"


def _adapter() -> WppAdapter:
    """跳过 ``__init__``（它要 PlatformConfig + 账号表），只装 ``_handle_one`` 读到的两个属性。"""
    a = object.__new__(WppAdapter)
    a._dedup = MagicMock()
    a._dedup.is_duplicate.return_value = False
    a._trigger_limiter = MagicMock()
    a._trigger_limiter.guard.return_value = MagicMock(allowed=True, reason="", hits=0, muted_until=0.0)
    # 下游一律打成哨兵：门命中时它们必须一次都不被调用
    a._maybe_command = AsyncMock(return_value=False)
    a.group_muted = MagicMock(return_value=False)
    a._fetch_media = AsyncMock(return_value=None)
    a._hf_note_group = MagicMock()
    a._jargon_note_group = MagicMock()
    a._affection_note_group = MagicMock()
    a._try_heartflow = AsyncMock(return_value=False)
    a.handle_message = AsyncMock()
    return a


def _acct() -> MagicMock:
    acct = MagicMock()
    acct.id = "default"
    acct.self_wxid = SELF
    acct.nickname = "机器人"
    return acct


def _msg(**over) -> WppMessage:
    base = dict(account_id="default", msg_id="m1", from_wxid=PEER, content="你好")
    base.update(over)
    return WppMessage(**base)


def _pipeline() -> MagicMock:
    pipe = MagicMock()
    pipe.decide.return_value = (False, "not_for_me")
    return pipe


@pytest.mark.asyncio
async def test_self_echo_is_dropped_before_any_downstream_side_effect():
    adapter, acct, pipe = _adapter(), _acct(), _pipeline()

    await adapter._handle_one(acct, _msg(from_wxid=SELF), pipe)

    adapter._dedup.is_duplicate.assert_not_called()  # 自消息应在①就返回，不该走到去重
    adapter._maybe_command.assert_not_called()
    adapter._fetch_media.assert_not_called()
    adapter._hf_note_group.assert_not_called()
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_filehelper_session_is_exempt_from_the_self_echo_gate():
    """vendor 把文件传输助手的发送者标成机器人自己 —— 不豁免则管理命令永远进不来。"""
    adapter, acct, pipe = _adapter(), _acct(), _pipeline()
    # filehelper 会话的特征：to_wxid == "filehelper"
    msg = _msg(from_wxid=SELF, to_wxid="filehelper", content="/heartflow on")

    await adapter._handle_one(acct, msg, pipe)

    adapter._maybe_command.assert_awaited()  # filehelper 会话必须越过①，交给③b 的 `/` 命令处理


@pytest.mark.asyncio
async def test_duplicate_message_is_dropped_and_never_reaches_the_pipeline():
    adapter, acct, pipe = _adapter(), _acct(), _pipeline()
    adapter._dedup.is_duplicate.return_value = True

    await adapter._handle_one(acct, _msg(), pipe)

    pipe.decide.assert_not_called()  # 重复消息应在②返回，不该跑触发判定
    adapter._fetch_media.assert_not_called()
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_dedupe_key_includes_the_account_id():
    """多账号可能撞 msg_id —— key 不带账号就会把一个账号的消息当成另一个的重复。"""
    adapter, pipe = _adapter(), _pipeline()

    await adapter._handle_one(_acct(), _msg(msg_id="same-id"), pipe)

    key = adapter._dedup.is_duplicate.call_args.args[0]
    assert key == "default:same-id", f"去重 key 必须带账号前缀，实际 {key!r}"


@pytest.mark.asyncio
async def test_empty_body_without_media_is_dropped_as_vendor_noise():
    adapter, acct, pipe = _adapter(), _acct(), _pipeline()

    await adapter._handle_one(acct, _msg(content="   "), pipe)

    adapter._maybe_command.assert_not_called()  # 空体应在③返回，不该走命令解析
    pipe.decide.assert_not_called()
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_empty_text_with_media_is_kept():
    """只有媒体没有文字的图片/语音是正常消息，不能被③当成心跳噪声丢掉。"""
    adapter, acct, pipe = _adapter(), _acct(), _pipeline()
    msg = _msg(content="", media={"type": "image"})

    await adapter._handle_one(acct, msg, pipe)

    pipe.decide.assert_called_once()  # 带媒体的空体必须继续走触发判定


@pytest.mark.asyncio
async def test_muted_group_is_dropped_even_when_mentioned():
    """③c：禁言期间整条丢弃 —— 包括 @机器人，否则一 @ 就破防，开关等于没有。"""
    adapter, acct, pipe = _adapter(), _acct(), _pipeline()
    adapter.group_muted.return_value = True
    msg = _msg(chatroom_id="123@chatroom", content="@机器人 在吗")

    await adapter._handle_one(acct, msg, pipe)

    adapter.group_muted.assert_called_once_with("default", "123@chatroom")
    pipe.decide.assert_not_called()
    adapter.handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_policy_denial_is_dropped_before_media_fetch():
    """④ should_drop：被策略拒的消息不能先下载+上传媒体（白花钱）。"""
    adapter, acct, pipe = _adapter(), _acct(), _pipeline()
    pipe.decide.return_value = (False, "blacklisted_group")

    await adapter._handle_one(acct, _msg(chatroom_id="1@chatroom"), pipe)

    adapter._fetch_media.assert_not_called()  # 策略拒的消息必须在媒体富化之前返回
    adapter.handle_message.assert_not_called()
