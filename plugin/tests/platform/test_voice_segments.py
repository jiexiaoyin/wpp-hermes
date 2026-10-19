"""语音 60 秒上限 / SILK 60 KB 字节闸的分段。

2026-10-19：老板明确「微信端语音最长不能超过 60S，超过的就得分段」。此外厂商
``/Msg/SendVoice`` 还卡 SILK 字节上限（60 KB，25 kbps 只够 ~19 秒），所以
真正要保证的是「每段 ≤ 60s 且编码后 ≤ 60 KB」。这里按行为钉住：

* :func:`plan_segments` 纯切点：优先静音、硬切兜底、不产生碎片；
* 适配器 ``_send_voice_media`` 对长音频发多条语音气泡，而不是降级成一个文件；
* 通道契约 sender 同样分段。
"""

from __future__ import annotations

import asyncio
import base64
import os

import pytest

from plugin.platform import silk_encoder


def test_plan_segments_returns_one_segment_when_under_limit() -> None:
    assert silk_encoder.plan_segments(45_000, max_ms=55_000) == [(0.0, 45.0)]


def test_plan_segments_hard_cuts_without_silence() -> None:
    assert silk_encoder.plan_segments(120_000, [], max_ms=55_000) == [
        (0.0, 55.0), (55.0, 110.0), (110.0, 120.0),
    ]


def test_plan_segments_prefers_the_latest_silence_before_the_limit() -> None:
    silences = [(10.0, 10.4), (53.5, 54.2), (100.0, 100.5), (108.0, 108.6)]
    assert silk_encoder.plan_segments(120_000, silences, max_ms=55_000) == [
        (0.0, 54.2), (54.2, 108.6), (108.6, 120.0),
    ]


def test_plan_segments_ignores_silence_too_close_to_the_start() -> None:
    # 0.5s 处就静音：切了只剩 0.5 秒碎片，必须继续用硬切
    assert silk_encoder.plan_segments(80_000, [(0.0, 0.5)], max_ms=55_000) == [
        (0.0, 55.0), (55.0, 80.0),
    ]


class _FakeClient:
    def __init__(self) -> None:
        self.voice_calls: list[tuple] = []
        self.file_calls: list[tuple] = []

    async def send_voice(self, to=None, b64=None, *args, **kwargs):
        call = {"to": to, "b64": b64, **kwargs}
        self.voice_calls.append(call)
        return {"Code": 0, "Data": {"NewMsgId": f"v{len(self.voice_calls)}"}}

    async def send_file_v2(self, **kwargs):
        self.file_calls.append(kwargs)
        return {"Code": 0, "Data": {"NewMsgId": "f1"}}


def test_send_voice_media_sends_each_segment_as_a_bubble(stubbed, monkeypatch) -> None:
    from plugin.platform.adapter_media import MediaMixin

    segments = ["/tmp/wpp-voice-split-x/part-01.wav",
                "/tmp/wpp-voice-split-x/part-02.wav"]
    cleaned: list[list[str]] = []

    async def _split(_ref, max_ms=silk_encoder.MAX_VOICE_SEGMENT_MS):
        return list(segments)

    async def _encode(_ref):
        return "SILK"

    async def _duration(_ref):
        return 50_000

    monkeypatch.setattr(silk_encoder, "split_audio", _split)
    monkeypatch.setattr(silk_encoder, "encode_to_silk_fitting", _encode)
    monkeypatch.setattr(silk_encoder, "probe_duration_ms", _duration)
    monkeypatch.setattr(silk_encoder, "cleanup_segments", cleaned.append)

    client = _FakeClient()
    resp = asyncio.run(MediaMixin._send_voice_media(
        client, "peer", "/tmp/long.mp3", "long.mp3", "QjY0"))

    assert resp["Code"] == 0
    assert len(client.voice_calls) == 2, "长语音必须发两条气泡，而不是降级成一个文件"
    assert client.file_calls == []
    assert [c["voice_time"] for c in client.voice_calls] == [50_000, 50_000]
    assert cleaned == [segments], "临时分段文件必须清理"


def test_send_voice_media_degrades_only_the_failed_segment(stubbed, monkeypatch,
                                                           tmp_path) -> None:
    from plugin.platform.adapter_media import MediaMixin

    segments = [str(tmp_path / "part-01.wav"), str(tmp_path / "part-02.wav")]
    for seg in segments:
        with open(seg, "wb") as fh:
            fh.write(b"RIFF")

    async def _split(_ref, max_ms=silk_encoder.MAX_VOICE_SEGMENT_MS):
        return list(segments)

    calls: list[str] = []

    async def _encode(ref):
        calls.append(ref)
        return None if ref.endswith("02.wav") else "SILK"

    async def _duration(_ref):
        return 10_000

    monkeypatch.setattr(silk_encoder, "split_audio", _split)
    monkeypatch.setattr(silk_encoder, "encode_to_silk_fitting", _encode)
    monkeypatch.setattr(silk_encoder, "probe_duration_ms", _duration)
    monkeypatch.setattr(silk_encoder, "cleanup_segments", lambda _p: None)

    client = _FakeClient()
    asyncio.run(MediaMixin._send_voice_media(
        client, "peer", "/tmp/long.mp3", "long.mp3", "QjY0"))

    assert len(client.voice_calls) == 1
    assert len(client.file_calls) == 1, "编码失败的那一段降级文件，已发段不重发"


def test_send_voice_media_materializes_url_ref_before_splitting(stubbed, monkeypatch,
                                                               tmp_path) -> None:
    """``ref`` 是 URL/data URI/裸 base64 时（live 的 ``send()`` 就这样传），旧实现把
    它直接交给 ``split_audio``，降级分支再 ``open(seg)`` ⇒ ``FileNotFoundError``。
    现在必须先落地成临时文件，并在结束时清掉。"""
    from plugin.platform.adapter_media import MediaMixin

    seg = tmp_path / "decoded.wav"
    seg.write_bytes(b"RIFF")
    seen: dict = {}

    async def _split(ref, max_ms=silk_encoder.MAX_VOICE_SEGMENT_MS):
        seen["ref"] = ref
        seen["exists"] = os.path.isfile(ref)
        with open(ref, "rb") as fh:
            seen["data"] = fh.read()
        return [str(seg)]

    async def _encode(_ref):
        return "SILK"

    async def _duration(_ref):
        return 1_000

    monkeypatch.setattr(silk_encoder, "split_audio", _split)
    monkeypatch.setattr(silk_encoder, "encode_to_silk_fitting", _encode)
    monkeypatch.setattr(silk_encoder, "probe_duration_ms", _duration)
    monkeypatch.setattr(silk_encoder, "cleanup_segments", lambda _p: None)

    payload = b"ID3" + b"x" * 32
    asyncio.run(MediaMixin._send_voice_media(
        _FakeClient(), "peer", "https://oss.example/x.mp3", "x.mp3",
        base64.b64encode(payload).decode()))

    assert seen["exists"], "分段前 ref 必须已是本地文件"
    assert seen["data"] == payload
    assert not os.path.exists(os.path.dirname(seen["ref"])), "落地临时目录必须清理"


def test_send_voice_media_vendor_rejection_degrades_that_segment(stubbed, monkeypatch,
                                                                 tmp_path) -> None:
    """厂商拒收是 **raise**（``WppApiError``），不是返回 ``Code=-2``。旧代码没有
    try/except ⇒ 整条语音发送炸掉，降级分支是死的。现在只把被拒那一段降级文件。"""
    from plugin.core.api_client import WppApiError
    from plugin.platform.adapter_media import MediaMixin

    segments = [str(tmp_path / "part-01.wav"), str(tmp_path / "part-02.wav")]
    for seg in segments:
        with open(seg, "wb") as fh:
            fh.write(b"RIFF")

    class _RejectFirst(_FakeClient):
        async def send_voice(self, to=None, b64=None, *args, **kwargs):
            self.voice_calls.append({"to": to, "b64": b64, **kwargs})
            if len(self.voice_calls) == 1:
                raise WppApiError(-2, "INVALID_CREDENTIAL", "/Msg/SendVoice")
            return {"Code": 0, "Data": {"NewMsgId": "v2"}}

    async def _split(_ref, max_ms=silk_encoder.MAX_VOICE_SEGMENT_MS):
        return list(segments)

    async def _encode(_ref):
        return "SILK"

    async def _duration(_ref):
        return 10_000

    monkeypatch.setattr(silk_encoder, "split_audio", _split)
    monkeypatch.setattr(silk_encoder, "encode_to_silk_fitting", _encode)
    monkeypatch.setattr(silk_encoder, "probe_duration_ms", _duration)
    monkeypatch.setattr(silk_encoder, "cleanup_segments", lambda _p: None)

    client = _RejectFirst()
    resp = asyncio.run(MediaMixin._send_voice_media(
        client, "peer", "/tmp/long.mp3", "long.mp3", "QjY0"))

    assert resp["Code"] == 0, "后一段成功即返回成功响应"
    assert len(client.voice_calls) == 2
    assert len(client.file_calls) == 1, "被拒的那一段降级发文件，成功的段不重发"


def test_send_voice_media_never_sends_zero_voice_time(stubbed, monkeypatch,
                                                      tmp_path) -> None:
    """``VoiceTime=0`` 厂商必拒（误导性地回 INVALID_CREDENTIAL）。时长探测不出时
    必须降级文件，绝不能带着 0 调 ``send_voice``。"""
    from plugin.platform.adapter_media import MediaMixin

    seg = tmp_path / "part-01.wav"
    seg.write_bytes(b"RIFF")

    async def _split(_ref, max_ms=silk_encoder.MAX_VOICE_SEGMENT_MS):
        return [str(seg)]

    async def _encode(_ref):
        return "SILK"

    async def _zero(_ref):
        return 0

    monkeypatch.setattr(silk_encoder, "split_audio", _split)
    monkeypatch.setattr(silk_encoder, "encode_to_silk_fitting", _encode)
    monkeypatch.setattr(silk_encoder, "probe_duration_ms", _zero)
    monkeypatch.setattr(silk_encoder, "probe_silk_base64_ms", _zero)
    monkeypatch.setattr(silk_encoder, "cleanup_segments", lambda _p: None)

    client = _FakeClient()
    asyncio.run(MediaMixin._send_voice_media(
        client, "peer", "/tmp/x.mp3", "x.mp3", "QjY0"))

    assert client.voice_calls == [], "VoiceTime 无法确认时不许调用 SendVoice"
    assert len(client.file_calls) == 1, "必须降级发文件"


def test_send_voice_media_fast_path_never_sends_zero_voice_time(stubbed, monkeypatch,
                                                                tmp_path) -> None:
    """SILK 直发快路径同样要挡 ``VoiceTime=0``。"""
    from plugin.platform.adapter_media import MediaMixin

    payload = b"\x02" + b"SILK" + b"\x00" * 20
    seg = tmp_path / "part-01.silk"
    seg.write_bytes(payload)

    async def _split(_ref, max_ms=silk_encoder.MAX_VOICE_SEGMENT_MS):
        return [str(seg)]

    async def _zero(_ref):
        return 0

    monkeypatch.setattr(silk_encoder, "split_audio", _split)
    monkeypatch.setattr(silk_encoder, "probe_duration_ms", _zero)
    monkeypatch.setattr(silk_encoder, "probe_silk_base64_ms", _zero)
    monkeypatch.setattr(silk_encoder, "cleanup_segments", lambda _p: None)

    client = _FakeClient()
    asyncio.run(MediaMixin._send_voice_media(
        client, "peer", "/tmp/x.silk", "x.silk",
        base64.b64encode(payload).decode()))

    assert client.voice_calls == []
    assert len(client.file_calls) == 1


def test_conversation_sender_splits_long_voice(monkeypatch, tmp_path) -> None:
    from plugin.platform import conversation

    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3")
    segments = ["/tmp/wpp-voice-split-x/part-01.wav",
                "/tmp/wpp-voice-split-x/part-02.wav"]

    async def _split(_ref, max_ms=silk_encoder.MAX_VOICE_SEGMENT_MS):
        return list(segments)

    async def _encode(_ref):
        return "SILK"

    async def _duration(_ref):
        return 30_000

    monkeypatch.setattr(silk_encoder, "split_audio", _split)
    monkeypatch.setattr(silk_encoder, "encode_to_silk_fitting", _encode)
    monkeypatch.setattr(silk_encoder, "probe_duration_ms", _duration)
    monkeypatch.setattr(silk_encoder, "cleanup_segments", lambda _p: None)

    client = _FakeClient()
    sender = conversation.WppOutboundSender("acct", lambda: client)
    result = asyncio.run(sender.send_voice(
        conversation.dm_key("acct", "u1"), str(audio)))

    assert result == "v2"
    assert [c["voice_time"] for c in client.voice_calls] == [30_000, 30_000]
