"""通道无关会话契约（P1，2026-10-19）。

判据下在**协议真的能跨通道工作**上：同一段主动消息逻辑，换掉 store/sender 的实现
（WPP 与一个"企微形状"的假通道）必须产出同样的行为。只测 WPP 自己等于没验证抽象。
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from plugin.inbound import proactive as pa
from plugin.neutral.contracts import (
    ChatCapabilities,
    ConversationKey,
    ConversationStore,
    MessageRow,
    OutboundSender,
)
from plugin.platform.conversation import (
    WPP_CAPABILITIES,
    WppConversationStore,
    WppOutboundSender,
    dm_key,
    key_from_message,
)


@pytest.fixture(autouse=True)
def _clear_memory() -> None:
    pa._last_sent.clear()


def _sync(coro_fn):
    @functools.wraps(coro_fn)
    def _wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return _wrapper


# ============================================================ 契约本身

def test_conversation_key_accepts_the_four_fields_and_is_hashable() -> None:
    key = ConversationKey("wechatpadpro", "acct", "group", "123@chatroom")
    assert key.as_tuple() == ("wechatpadpro", "acct", "group", "123@chatroom")
    assert key.is_group is True
    assert key in {key}


@pytest.mark.parametrize("field", ["platform", "account_id", "chat_type", "chat_id"])
def test_conversation_key_rejects_empty_identity(field) -> None:
    values = {"platform": "p", "account_id": "a", "chat_type": "dm", "chat_id": "c"}
    values[field] = ""
    with pytest.raises(ValueError):
        ConversationKey(**values)


def test_message_row_is_mapping_like_for_legacy_policy_code() -> None:
    row = MessageRow("incoming", 123, "在吗", "u1")
    assert row.get("direction") == "incoming"
    assert row.get("at_sec") == 123
    assert row.get("nope", "fallback") == "fallback"


def test_protocols_are_structural() -> None:
    assert isinstance(WppConversationStore("acct"), ConversationStore)
    assert isinstance(WppOutboundSender("acct"), OutboundSender)


def test_capabilities_express_group_and_voice_support() -> None:
    assert WPP_CAPABILITIES.groups and WPP_CAPABILITIES.voice
    wecom = ChatCapabilities(groups=True, voice=False, quote=False, members=False)
    assert wecom.can_send("group") is True
    assert wecom.can_send("dm") is True
    assert ChatCapabilities(groups=False).can_send("group") is False


# ============================================================ WPP store

def _row(direction: str, at_sec: int, content: str = "x") -> dict:
    return {"direction": direction, "at_sec": at_sec, "content": content,
            "from_wxid": "u1"}


def test_wpp_store_maps_repo_rows_to_message_rows(monkeypatch) -> None:
    from plugin.data import repo_messages

    seen: list[tuple] = []

    def _list(account_id, peer_id, limit):
        seen.append((account_id, peer_id, limit))
        return [_row("incoming", 10, "你好"), _row("outbound", 20, "在")]

    monkeypatch.setattr(repo_messages, "list_messages_by_peer", _list)
    store = WppConversationStore("acct")
    rows = store.recent(dm_key("acct", "u1"), 5)
    assert seen == [("acct", "u1", 5)]
    assert rows == [MessageRow("incoming", 10, "你好", "u1"),
                    MessageRow("outbound", 20, "在", "u1")]


def test_wpp_store_normalizes_db_inbound_to_contract_incoming(monkeypatch) -> None:
    """``wpp_messages.direction`` 是 ``inbound/outbound``（见 ``persist.db_direction``），
    而主动消息策略按契约读 ``incoming``。原样透传 ⇒ 真库上永远 ``not eligible``，
    且完全静默（reason=ok, sent=0）。缓存/读库两侧都必须归一。"""
    from plugin.data import repo_messages

    monkeypatch.setattr(repo_messages, "list_messages_by_peer",
                        lambda *a, **k: [_row("inbound", 10, "在吗"),
                                         _row("outbound", 20, "在")])
    rows = WppConversationStore("acct").recent(dm_key("acct", "u1"), 5)
    assert [r.direction for r in rows] == ["incoming", "outbound"]


def test_wpp_store_ignores_other_platforms_or_accounts(monkeypatch) -> None:
    from plugin.data import repo_messages

    monkeypatch.setattr(repo_messages, "list_messages_by_peer",
                        lambda *a, **k: pytest.fail("不该读别的通道的历史"))
    store = WppConversationStore("acct")
    assert store.recent(ConversationKey("wecom", "acct", "dm", "u1")) == []
    assert store.recent(dm_key("other", "u1")) == []


def test_wpp_store_counts_outbound_since(monkeypatch) -> None:
    from plugin.data import repo_messages

    monkeypatch.setattr(repo_messages, "count_outbound_since",
                        lambda account_id, peer_id, since: 3)
    assert WppConversationStore("acct").count_outbound_since(dm_key("acct", "u1"), 0) == 3


# ============================================================ WPP sender

class _FakeClient:
    def __init__(self, resp=None) -> None:
        self.resp = resp or {"Code": 0, "Data": {"List": [{"NewMsgId": 42, "Ret": 0}]}}
        self.calls: list[tuple[str, ...]] = []

    async def send_text(self, to, content):
        self.calls.append(("text", to, content))
        return self.resp

    async def send_voice(self, to, b64, **kwargs):
        self.calls.append(("voice", to, b64))
        self.last_voice_kwargs = kwargs
        return self.resp

    async def send_file_v2(self, to, name, b64):
        self.calls.append(("file", to, name, b64))
        return self.resp


def test_sender_reads_the_client_at_send_time() -> None:
    holder: dict = {}

    def _getter():
        return holder.get("client")

    sender = WppOutboundSender("acct", _getter)
    with pytest.raises(RuntimeError):
        asyncio.run(sender.send_text(dm_key("acct", "u1"), "hi"))

    holder["client"] = _FakeClient()
    assert asyncio.run(sender.send_text(dm_key("acct", "u1"), "hi")) == "42"


def test_sender_refuses_a_foreign_conversation() -> None:
    sender = WppOutboundSender("acct", lambda: _FakeClient())
    with pytest.raises(ValueError):
        asyncio.run(sender.send_text(ConversationKey("wecom", "acct", "dm", "u1"), "hi"))


@_sync
async def test_sender_quotes_when_asked() -> None:
    client = _FakeClient()
    sender = WppOutboundSender("acct", lambda: client)
    await sender.send_text(dm_key("acct", "u1"), "收到", quote_id="987654")
    content = client.calls[0][2]
    assert "收到" in content and "987654" in content and "<refermsg>" in content


@_sync
async def test_sender_passes_real_voice_duration(monkeypatch, tmp_path) -> None:
    """VoiceTime=0 会被厂商 Code=-2 拒收（2026-10-19 实测）⇒ 必须带真实毫秒数。"""
    from plugin.platform import silk_encoder

    async def _b64(_path):
        return "SILK"

    async def _duration(_path):
        return 2376

    monkeypatch.setattr(silk_encoder, "encode_to_silk_fitting", _b64)
    monkeypatch.setattr(silk_encoder, "probe_duration_ms", _duration)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3")
    client = _FakeClient()
    sender = WppOutboundSender("acct", lambda: client)
    await sender.send_voice(dm_key("acct", "u1"), str(audio))
    assert client.calls[0][0] == "voice"
    assert client.last_voice_kwargs == {"voice_time": 2376}


@_sync
async def test_sender_degrades_voice_to_file_without_silk(monkeypatch, tmp_path) -> None:
    from plugin.platform import silk_encoder

    async def _none(_path):
        return None

    monkeypatch.setattr(silk_encoder, "encode_to_silk_fitting", _none)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3")
    client = _FakeClient()
    sender = WppOutboundSender("acct", lambda: client)
    await sender.send_voice(dm_key("acct", "u1"), str(audio))
    assert client.calls[0][0] == "file"
    assert client.calls[0][2] == "a.mp3"


@_sync
async def test_sender_raises_on_vendor_rejection() -> None:
    client = _FakeClient({"Code": -1, "CodeValue": "INVALID_CREDENTIAL"})
    sender = WppOutboundSender("acct", lambda: client)
    with pytest.raises(RuntimeError):
        await sender.send_text(dm_key("acct", "u1"), "hi")


def test_key_from_message_maps_wpp_peer_fields() -> None:
    msg = type("M", (), {"account_id": "acct", "peer_kind": "group",
                         "peer_id": "room@chatroom"})()
    assert key_from_message(msg) == ConversationKey(
        "wechatpadpro", "acct", "group", "room@chatroom")


# ============================================================ 第二个通道（企微形状）

class _WeComStore:
    platform = "wecom"

    def __init__(self, rows):
        self.rows = rows

    def recent(self, conv, limit=20):
        assert conv.platform == "wecom"
        return list(self.rows)[-limit:]

    def count_outbound_since(self, conv, since_sec):
        return 0


class _WeComSender:
    platform = "wecom"
    capabilities = ChatCapabilities(groups=True)

    def __init__(self):
        self.sent = []

    async def send_text(self, conv, text, *, quote_id=None):
        self.sent.append((conv.chat_id, text, quote_id))
        return "wc-1"

    async def send_voice(self, conv, audio_path):
        raise AssertionError("企微这个通道没有语音")

    async def send_file(self, conv, file_path):
        raise AssertionError("本用例不发文件")


def test_proactive_runs_on_a_second_channel_without_wpp(monkeypatch) -> None:
    """同一段策略代码：只换 store/sender/key_for，WPP 的 adapter 完全不出现。"""
    now = 1_800_000_000
    rows = [MessageRow("incoming", now - 30 * 3600, "最近怎么样")]
    store = _WeComStore(rows)
    sender = _WeComSender()
    stored: list[tuple] = []

    async def _gen(_rows, _cfg):
        return "忙完啦，你呢？"

    monkeypatch.setattr(pa, "_generate", _gen)
    cfg = pa.ProactiveConfig(
        enabled=True, allow_from=("user@wecom",), quiet_hours=(0, 0), min_idle_hours=20,
    )
    result = asyncio.run(pa.maybe_send_once(
        account_id="wacct", cfg=cfg, now=now, store=store, sender=sender,
        persist=lambda conv, text, msg_id, ts: stored.append(
            (conv.as_tuple(), text, msg_id, ts)),
    ))
    assert result == {"sent": 1, "reason": "ok"}
    assert sender.sent == [("user@wecom", "忙完啦，你呢？", None)]
    assert stored == [(("wecom", "wacct", "dm", "user@wecom"), "忙完啦，你呢？", "wc-1", now)]
