"""语音链路回归：**转写进正文**（vendor 优先）+ 自己转（silk→WAV→SiliconFlow）。

## 这一套守的失效形态：语音"处理过了"但等于没处理

语音这条链上有三处**不报错**的断点，任一处断了，现场都是同一副样子：
消息收下了、音频也落了、日志正常，**只有模型看到的是 ``[语音]`` 一行**（老板 @ 机器人
发语音＝没说话）：

1. **正文注入**（:func:`plugin.inbound.parser.InboundPipeline.prepare_text`）没接转写 ——
   转写算出来了，却没人写进正文；
2. **顺序反了**（``adapter._build_event`` 先算正文再取媒体）—— 转写永远赶不上这班车；
3. **来源反了 / 丢了**（vendor 已有转写却还是去调 STT，或 STT 结果没写回 ``media``）。

所以下面既测**行为**（WAV 头逐字节、重试分类、两个来源的取舍），也钉**接线形状**
（顺序、写回）—— 后者在本机没有真机可比时是唯一能红的地方。

## 本机怎么测异步与网络

本仓无 pytest-asyncio ⇒ 手工 ``asyncio.run``；根 conftest 默认**禁网** ⇒
HTTP 那一层被收在 :func:`plugin.inbound.stt._post_wav` 一个函数里，测试 monkeypatch 它，
于是"重试/分类/解析"这些真会错的地方在**本机**就能测（aiohttp 本机根本没装 ——
这也是为什么它必须在函数体内 import）。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import struct
import sys
import textwrap

import pytest

from plugin.inbound import stt
from plugin.inbound.media import voice_transcript
from plugin.inbound.parser import MT_APP, MT_TEXT, MT_VOICE, InboundPipeline, WppMessage

#: 复用注册面那套空壳环境（``gateway.*`` 只在 Hermes 运行时里存在），**不另写一份**。
#: ⚠️ ``stubbed`` 住在 ``conftest.py``（2026-10-02 提上来的）⇒ pytest 按名字解析，
#: **不需要 import**；从 test_registration 导入会在收集期 ImportError。

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: 便宜一点的退避：测试里不该真的 sleep 1+2 秒（断言的是"重试了几次"不是"等了多久"）。
_FAST = 0.0


@pytest.fixture(autouse=True)
def _fast_retry_sleep(monkeypatch):
    monkeypatch.setattr(stt, "RETRY_SLEEP_S", _FAST)
    stt.reset_degradation_latch()


def _msg(msg_type: int = MT_VOICE, content: str = "收到一条语音", media: dict | None = None,
         is_group: bool = False) -> WppMessage:
    return WppMessage(
        account_id="acc", msg_id="m1", from_wxid="wxid_a", from_nickname="张三",
        chatroom_id="123@chatroom" if is_group else "",
        content=content, msg_type=msg_type, media=dict(media or {}),
    )


def _pipeline() -> InboundPipeline:
    return InboundPipeline("acc", {"selfWxid": "wxid_bot", "nickname": "益融小助理"})


# ============================================================ ① 正文注入（parser）


def test_voice_with_vendor_transcript_lands_in_body() -> None:
    """vendor 官方转写 ⇒ 正文带 ``[转写] <text>``（记号逐字对齐 TS v1.3.22）。"""
    msg = _msg(media={"vendor_transcript": "今天下午三点开会"})
    assert _pipeline().prepare_text(msg) == "[语音] 收到一条语音\n[转写] 今天下午三点开会"


def test_voice_with_local_stt_text_lands_in_body() -> None:
    """插件自己转的（``stt_text``）同样要进正文 —— 只在 vendor 缺席时才有的那条来源。"""
    msg = _msg(media={"stt_text": "帮我看下这个价格"})
    assert _pipeline().prepare_text(msg) == "[语音] 收到一条语音\n[转写] 帮我看下这个价格"


def test_vendor_transcript_wins_over_local_stt() -> None:
    """两个来源都在时**以 vendor 为准**（微信官方出的那个）。"""
    msg = _msg(media={"vendor_transcript": "官方的", "stt_text": "自己转的"})
    out = _pipeline().prepare_text(msg)
    assert "官方的" in out and "自己转的" not in out


def test_no_transcript_means_no_empty_marker() -> None:
    """没有转写 ⇒ **一行都不加**。写个空的 ``[转写] `` 会让模型以为"语音内容为空"。"""
    out = _pipeline().prepare_text(_msg(media={}))
    assert out == "[语音] 收到一条语音"
    assert "[转写]" not in out


def test_blank_transcript_is_treated_as_absent() -> None:
    """转写是空白串（vendor 给了个空格）⇒ 当没有，不写进正文。"""
    out = _pipeline().prepare_text(_msg(media={"vendor_transcript": "   "}))
    assert "[转写]" not in out


def test_transcript_is_not_gated_on_msg_type() -> None:
    """**有意不按 ``msg_type`` 判**（钉住这个决定，别顺手加个 34 的门）。

    判据是"转写键在不在"而不是"类型是不是 34"：这两个键只有语音解析（
    :func:`plugin.inbound.media._extract_voice`）会写，按类型再判一次是**死码**；
    而 TS 那边语音本来就有两路（``MsgType.VOICE`` 与 49 appmsg 里的 ``<voicemsg>``，
    见 ``handler.ts:357``），py2 目前只实现了前一路 —— 真加了类型门，
    等哪天后一路补上时这里会**静默吞掉**那批语音的转写。
    """
    msg = _msg(msg_type=MT_APP, content="x", media={"vendor_transcript": "appmsg 里的语音"})
    assert _pipeline().prepare_text(msg).endswith("[转写] appmsg 里的语音")


def test_text_message_without_media_untouched() -> None:
    """普通文字消息一个字符都不变（转写只挂在语音上，靠 media 键自身区分）。"""
    msg = _msg(msg_type=MT_TEXT, content="你好")
    assert _pipeline().prepare_text(msg) == "你好"


def test_voice_transcript_reader_prefers_vendor() -> None:
    assert voice_transcript({"vendor_transcript": " a ", "stt_text": "b"}) == "a"
    assert voice_transcript({"stt_text": "b"}) == "b"
    assert voice_transcript({}) == ""
    assert voice_transcript(None) == ""


# =============================================================== ② STT 策略与降级


def test_should_attempt_stt_skips_when_vendor_gave_one() -> None:
    assert stt.should_attempt_stt({"vendor_transcript": "官方转写"}) is False
    assert stt.should_attempt_stt({"vendor_transcript": "   "}) is True
    assert stt.should_attempt_stt({}) is True
    assert stt.should_attempt_stt(None) is True


def test_unavailable_reason_names_what_is_missing(monkeypatch, tmp_path) -> None:
    """降级理由必须**说出缺哪样**（缺二进制 / 不可执行 / 缺 key 三种）。"""
    missing = tmp_path / "nope"
    monkeypatch.setattr(stt, "SILK_DECODER_PATH", str(missing))
    assert "不存在" in (stt.unavailable_reason() or "")

    noexec = tmp_path / "decoder"
    noexec.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(noexec, 0o644)
    monkeypatch.setattr(stt, "SILK_DECODER_PATH", str(noexec))
    assert "不可执行" in (stt.unavailable_reason() or "")

    os.chmod(noexec, 0o755)
    monkeypatch.delenv("WECHATPRO_STT_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    assert "STT key" in (stt.unavailable_reason() or "")

    monkeypatch.setenv("WECHATPRO_STT_API_KEY", "sk-x")
    assert stt.unavailable_reason() is None


def test_api_key_prefers_plugin_yaml_name(monkeypatch) -> None:
    """``WECHATPRO_STT_API_KEY`` 优先（plugin.yaml 声明的就是它），旧名兜底。"""
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-old")
    monkeypatch.setenv("WECHATPRO_STT_API_KEY", "sk-new")
    assert stt.siliconflow_api_key() == "sk-new"
    monkeypatch.delenv("WECHATPRO_STT_API_KEY")
    assert stt.siliconflow_api_key() == "sk-old"


def test_stt_backend_explicit_and_auto_detect(monkeypatch) -> None:
    """显式 ``WPP_STT_BACKEND`` 优先；不写时只在 URL 含 token-plan 时自动切换。"""
    monkeypatch.delenv("WPP_STT_BACKEND", raising=False)
    monkeypatch.delenv("SILICONFLOW_STT_URL", raising=False)
    assert stt.stt_backend() == "siliconflow"

    monkeypatch.setenv("SILICONFLOW_STT_URL", "https://token-plan.cn-beijing.maas.aliyuncs.com/x")
    assert stt.stt_backend() == "dashscope"

    monkeypatch.setenv("WPP_STT_BACKEND", "siliconflow")
    assert stt.stt_backend() == "siliconflow"
    monkeypatch.setenv("WPP_STT_BACKEND", "tokenplan")
    assert stt.stt_backend() == "dashscope"


def test_dashscope_key_falls_back_to_judge_key(monkeypatch) -> None:
    """Token Plan 后端用 JUDGE_API_KEY 兜底，不会拿 SiliconFlow key 去发。"""
    monkeypatch.setenv("WPP_STT_BACKEND", "dashscope")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-silicon")
    monkeypatch.delenv("WECHATPRO_STT_API_KEY", raising=False)
    monkeypatch.setenv("JUDGE_API_KEY", "tp-key")
    assert stt.stt_api_key() == "tp-key"
    monkeypatch.delenv("JUDGE_API_KEY")
    assert stt.stt_api_key() == ""


def test_dashscope_payload_inlines_wav_as_data_url() -> None:
    """请求体必须是 ``input_audio.data`` + ``data:audio/wav;base64,``，且采样率与 WAV 头一致。"""
    wav = stt.build_wav_buffer(b"\x01\x02" * 10)
    payload = stt.build_dashscope_payload(wav)
    assert payload["model"] == stt.DASHSCOPE_STT_MODEL
    part = payload["input"]["messages"][0]["content"][0]
    assert part["type"] == "input_audio"
    data = part["input_audio"]["data"]
    assert data.startswith("data:audio/wav;base64,")
    assert base64.b64decode(data.split(",", 1)[1]) == wav
    assert payload["parameters"] == {"format": "wav", "sample_rate": str(stt.WAV_SAMPLE_RATE)}


def test_transcribe_uses_dashscope_post_when_selected(monkeypatch, tmp_path) -> None:
    """选 dashscope 后端时：走 ``_post_dashscope``、用 Token Plan key、URL 是原生端点。"""
    monkeypatch.setenv("WPP_STT_BACKEND", "dashscope")
    monkeypatch.setenv("JUDGE_API_KEY", "tp-key")
    monkeypatch.delenv("WECHATPRO_STT_API_KEY", raising=False)
    _patch_stt_ready(monkeypatch, tmp_path)
    seen = {}

    async def fake_post(url, key, wav, timeout_s):
        seen.update(url=url, key=key, wav=wav)
        return 200, '{"text":"语音识别结果"}'

    monkeypatch.setattr(stt, "_post_dashscope", fake_post)
    assert asyncio.run(stt.transcribe_silk_buffer(b"\x02#x")) == "语音识别结果"
    assert seen["url"] == stt.DASHSCOPE_STT_URL
    assert seen["key"] == "tp-key"
    assert seen["wav"].startswith(b"RIFF")


def test_degradation_warns_loudly_once(caplog) -> None:
    """降级日志：**第一条 WARNING，之后 DEBUG**。

    「一条都不喊」= 静默丢能力（本仓最怕的）；「每条都喊」= 把真异常淹掉。
    """
    with caplog.at_level(logging.DEBUG, logger="plugin.inbound.stt"):
        stt.note_degradation_once("silk 解码器不存在: /x")
        stt.note_degradation_once("silk 解码器不存在: /x")
        stt.note_degradation_once("silk 解码器不存在: /x")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"WARNING 应当只有一条，实际 {len(warnings)} 条"
    assert "silk 解码器不存在" in warnings[0].getMessage()


# ===================================================================== ③ WAV 头


def test_build_wav_buffer_header_is_byte_exact() -> None:
    """44 字节头逐字段核对（采样率/块对齐/两处长度错一个 ⇒ 对方 400 或识别成噪声）。"""
    pcm = b"\x01\x02" * 100          # 200 字节
    wav = stt.build_wav_buffer(pcm)
    assert len(wav) == 44 + len(pcm)
    assert wav[:4] == b"RIFF"
    assert struct.unpack("<I", wav[4:8])[0] == 36 + len(pcm)     # RIFF 段长度 = 总长 - 8
    assert wav[8:12] == b"WAVE"
    assert wav[12:16] == b"fmt "
    fmt_size, audio_format, channels, rate, byte_rate, align, bits = struct.unpack(
        "<IHHIIHH", wav[16:36])
    assert (fmt_size, audio_format) == (16, 1)                    # PCM
    assert (channels, rate, bits) == (1, 24_000, 16)
    assert byte_rate == 24_000 * 1 * 2                            # 采样率 × 声道 × 2
    assert align == 1 * 2                                          # 块对齐
    assert wav[36:40] == b"data"
    assert struct.unpack("<I", wav[40:44])[0] == len(pcm)
    assert wav[44:] == pcm


def test_build_wav_buffer_empty_pcm() -> None:
    wav = stt.build_wav_buffer(b"")
    assert len(wav) == 44
    assert struct.unpack("<I", wav[40:44])[0] == 0


# ===================================================================== ④ 解码


@pytest.fixture
def fake_decoder(tmp_path):
    """假解码器：把输入原样当 PCM 写出去（本机没有真的 SKP_SILK 二进制）。"""
    path = tmp_path / "decoder"
    path.write_text(textwrap.dedent("""\
        #!/bin/sh
        cp "$1" "$2"
        """))
    os.chmod(path, 0o755)
    return str(path)


def test_decode_silk_to_pcm_ok(monkeypatch, fake_decoder) -> None:
    monkeypatch.setattr(stt, "SILK_DECODER_PATH", fake_decoder)
    pcm = asyncio.run(stt.decode_silk_to_pcm(b"\x02#!SILK_V3pcmdata"))
    assert pcm == b"\x02#!SILK_V3pcmdata"


def test_decode_silk_missing_binary_is_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(stt, "SILK_DECODER_PATH", str(tmp_path / "nope"))
    assert asyncio.run(stt.decode_silk_to_pcm(b"\x02#x")) is None


def test_decode_silk_nonzero_exit_is_none(monkeypatch, tmp_path) -> None:
    bad = tmp_path / "decoder"
    bad.write_text("#!/bin/sh\nexit 3\n")
    os.chmod(bad, 0o755)
    monkeypatch.setattr(stt, "SILK_DECODER_PATH", str(bad))
    assert asyncio.run(stt.decode_silk_to_pcm(b"\x02#x")) is None


def test_decode_silk_empty_output_is_none(monkeypatch, tmp_path) -> None:
    """解码器"成功"但输出 0 字节 ⇒ 当失败（否则会套出 0 字节数据的 WAV，对方回 400）。"""
    empty = tmp_path / "decoder"
    empty.write_text("#!/bin/sh\n: > \"$2\"\n")
    os.chmod(empty, 0o755)
    monkeypatch.setattr(stt, "SILK_DECODER_PATH", str(empty))
    assert asyncio.run(stt.decode_silk_to_pcm(b"\x02#x")) is None


def test_decode_silk_leaves_no_temp_files(monkeypatch, fake_decoder) -> None:
    """临时文件必须清干净（解码器每来一条语音跑一次，漏一个就是长跑泄漏）。"""
    before = set(os.listdir("/tmp"))
    monkeypatch.setattr(stt, "SILK_DECODER_PATH", fake_decoder)
    assert asyncio.run(stt.decode_silk_to_pcm(b"\x02#x")) is not None
    leaked = {n for n in set(os.listdir("/tmp")) - before if n.endswith((".silk", ".pcm"))}
    assert not leaked, f"临时文件没清: {leaked}"


# =================================================================⑤ 重试与解析


def _patch_stt_ready(monkeypatch, tmp_path) -> None:
    """让"解码"这一步在测试里必定成功（只测 HTTP 那一层的策略）。"""
    monkeypatch.setattr(stt, "SILK_DECODER_PATH", _ok_decoder(tmp_path))


def _ok_decoder(tmp_path) -> str:
    path = tmp_path / "decoder"
    path.write_text('#!/bin/sh\nprintf "pcm" > "$2"\n')
    os.chmod(path, 0o755)
    return str(path)


def test_extract_text_accepts_both_field_names() -> None:
    assert stt._extract_text('{"text": "你好"}') == "你好"
    assert stt._extract_text('{"transcript": "也行"}') == "也行"     # TS: text || transcript
    assert stt._extract_text('{"text": ""}') == ""
    assert stt._extract_text("not json") == ""
    assert stt._extract_text('["array"]') == ""


@pytest.mark.parametrize("status,retryable", [
    (401, True),    # TS 的 isRetryable（这个 vendor 上 401 抖过 —— 原样照搬）
    (429, True),
    (500, True),
    (503, True),
    (400, False),   # 音频/参数不对，重发同一个只会再错一次
    (403, False),
    (404, False),
])
def test_attempt_retry_classification(monkeypatch, status, retryable) -> None:
    """重试分类**逐个状态码**核对（分类错的两个方向都贵：该重的不重=语音丢，
    不该重的重=白等 3 秒 + 每条语音两行噪声日志）。"""
    async def fake_post(url, api_key, wav, timeout_s):
        return status, "body"

    monkeypatch.setattr(stt, "_post_wav", fake_post)
    text, ret, detail = asyncio.run(stt._attempt("u", "k", b"wav"))
    assert text is None
    assert ret is retryable
    assert f"HTTP {status}" in detail


def test_transcribe_retries_then_succeeds(monkeypatch, tmp_path) -> None:
    """429 → 500 → 200 的序列必须**重试到底并拿到文字**（TS 的 3 次）。"""
    _patch_stt_ready(monkeypatch, tmp_path)
    monkeypatch.setenv("WECHATPRO_STT_API_KEY", "sk-x")
    seen: list[int] = []

    async def fake_post(url, api_key, wav, timeout_s):
        seen.append(1)
        if len(seen) == 1:
            return 429, "rate limited"
        if len(seen) == 2:
            return 500, "boom"
        return 200, '{"text": "第三次成功"}'

    monkeypatch.setattr(stt, "_post_wav", fake_post)
    assert asyncio.run(stt.transcribe_silk_buffer(b"\x02#x")) == "第三次成功"
    assert len(seen) == 3


def test_transcribe_gives_up_after_three_attempts(monkeypatch, tmp_path) -> None:
    """一路 5xx ⇒ 3 次就停（第 4 次不该发生），返回 None。"""
    _patch_stt_ready(monkeypatch, tmp_path)
    monkeypatch.setenv("WECHATPRO_STT_API_KEY", "sk-x")
    calls: list[int] = []

    async def fake_post(url, api_key, wav, timeout_s):
        calls.append(1)
        return 503, "unavailable"

    monkeypatch.setattr(stt, "_post_wav", fake_post)
    assert asyncio.run(stt.transcribe_silk_buffer(b"\x02#x")) is None
    assert len(calls) == stt.MAX_ATTEMPTS == 3


def test_transcribe_does_not_retry_4xx(monkeypatch, tmp_path) -> None:
    """400/403/404 这类**重试没意义**的错当场放弃（TS: 只有 401/429/5xx 值得重试）。"""
    _patch_stt_ready(monkeypatch, tmp_path)
    monkeypatch.setenv("WECHATPRO_STT_API_KEY", "sk-x")
    calls: list[int] = []

    async def fake_post(url, api_key, wav, timeout_s):
        calls.append(1)
        return 400, "bad audio"

    monkeypatch.setattr(stt, "_post_wav", fake_post)
    assert asyncio.run(stt.transcribe_silk_buffer(b"\x02#x")) is None
    assert len(calls) == 1


def test_transcribe_retries_network_exception(monkeypatch, tmp_path) -> None:
    """网络异常/超时也按可重试处理（TS 的 catch 分支），最终返回 None 而不是抛出。"""
    _patch_stt_ready(monkeypatch, tmp_path)
    monkeypatch.setenv("WECHATPRO_STT_API_KEY", "sk-x")
    calls: list[int] = []

    async def boom(url, api_key, wav, timeout_s):
        calls.append(1)
        raise TimeoutError("read timeout")

    monkeypatch.setattr(stt, "_post_wav", boom)
    assert asyncio.run(stt.transcribe_silk_buffer(b"\x02#x")) is None
    assert len(calls) == 3


def test_transcribe_empty_200_is_not_retried(monkeypatch, tmp_path) -> None:
    """200 但正文没有 text ⇒ 返回 None 且**不重试**（对方已经收下了，重发同一个文件没意义）。"""
    _patch_stt_ready(monkeypatch, tmp_path)
    monkeypatch.setenv("WECHATPRO_STT_API_KEY", "sk-x")
    calls: list[int] = []

    async def fake_post(url, api_key, wav, timeout_s):
        calls.append(1)
        return 200, "{}"

    monkeypatch.setattr(stt, "_post_wav", fake_post)
    assert asyncio.run(stt.transcribe_silk_buffer(b"\x02#x")) is None
    assert len(calls) == 1


def test_transcribe_without_key_does_not_call_api(monkeypatch, tmp_path) -> None:
    """没配 key ⇒ 一次外呼都不发（也别拿空 Bearer 去撞 401）。"""
    _patch_stt_ready(monkeypatch, tmp_path)
    monkeypatch.delenv("WECHATPRO_STT_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    calls: list[int] = []

    async def fake_post(*a, **kw):
        calls.append(1)
        return 200, '{"text": "不该走到这里"}'

    monkeypatch.setattr(stt, "_post_wav", fake_post)
    assert asyncio.run(stt.transcribe_silk_buffer(b"\x02#x")) is None
    assert calls == []


def test_transcribe_decode_failure_skips_api(monkeypatch, tmp_path) -> None:
    """解码失败就不该再去调 API（白花一次外呼 + 一条 400 日志）。"""
    monkeypatch.setattr(stt, "SILK_DECODER_PATH", str(tmp_path / "nope"))
    monkeypatch.setenv("WECHATPRO_STT_API_KEY", "sk-x")
    calls: list[int] = []

    async def fake_post(*a, **kw):
        calls.append(1)
        return 200, '{"text": "x"}'

    monkeypatch.setattr(stt, "_post_wav", fake_post)
    assert asyncio.run(stt.transcribe_silk_buffer(b"\x02#x")) is None
    assert calls == []


def test_transcribe_uses_wav_sized_payload(monkeypatch, tmp_path) -> None:
    """送出去的是 **WAV**（44 字节头 + PCM），不是裸 PCM —— 对方只收容器格式。"""
    _patch_stt_ready(monkeypatch, tmp_path)          # 解码器输出 b"pcm"（3 字节）
    monkeypatch.setenv("WECHATPRO_STT_API_KEY", "sk-x")
    got: list[bytes] = []

    async def fake_post(url, api_key, wav, timeout_s):
        got.append(wav)
        return 200, '{"text": "ok"}'

    monkeypatch.setattr(stt, "_post_wav", fake_post)
    assert asyncio.run(stt.transcribe_silk_buffer(b"\x02#x")) == "ok"
    assert len(got) == 1
    assert got[0][:4] == b"RIFF" and len(got[0]) == 44 + 3


# ============================================================== ⑥ adapter 接线


@pytest.fixture(scope="module")
def adapter(stubbed):  # noqa: F811 —— stubbed 来自 test_registration（同一套空壳环境）
    """装上 gateway 空壳后再导适配器（真机才有 gateway.*）。"""
    import plugin.platform.adapter as A
    return A


def test_adapter_fill_voice_transcript_writes_stt_text(monkeypatch, adapter) -> None:
    """自己转出来的文字必须**写回 ``media``** —— 不写回＝白转（正文读不到）。"""
    async def fake_transcribe(silk, api_key=""):
        return "帮我看下这个价格"

    monkeypatch.setattr(stt, "should_attempt_stt", lambda m: True)
    monkeypatch.setattr(stt, "unavailable_reason", lambda: None)
    monkeypatch.setattr(stt, "transcribe_silk_buffer", fake_transcribe)
    media: dict = {}
    asyncio.run(adapter.WppAdapter._fill_voice_transcript("acc", media, b"\x02#x"))
    assert media["stt_text"] == "帮我看下这个价格"


def test_adapter_skips_stt_when_vendor_transcript_present(monkeypatch, adapter) -> None:
    """vendor 已有转写 ⇒ 一次 STT 都不调（省一次下载 + 一次外呼，且官方那个更准）。

    ⚠️ 这条必须把 ``unavailable_reason`` 也改成"依赖齐全"：否则在这台机器上
    （dev 有解码器但没配 key）**第二道门**会替第一道门把 STT 挡住，
    测试照样绿 —— 而"vendor 优先"那条短路其实已经被摘掉了（变异存活）。
    """
    called: list[int] = []

    async def boom(*a, **kw):
        called.append(1)
        raise AssertionError("vendor 有转写时不该再调 STT")

    monkeypatch.setattr(stt, "transcribe_silk_buffer", boom)
    monkeypatch.setattr(stt, "unavailable_reason", lambda: None)
    media = {"vendor_transcript": "官方转写"}
    asyncio.run(adapter.WppAdapter._fill_voice_transcript("acc", media, b"\x02#x"))
    assert called == []
    assert "stt_text" not in media


def test_adapter_degrades_without_deps(monkeypatch, adapter) -> None:
    """缺依赖 ⇒ 不调 STT、只喊一声降级（且**不抛**：一条语音绝不能挡住整条消息）。"""
    async def boom(*a, **kw):
        raise AssertionError("依赖不全时不该调 STT")

    warned: list[str] = []
    monkeypatch.setattr(stt, "unavailable_reason", lambda: "silk 解码器不存在: /x")
    monkeypatch.setattr(stt, "transcribe_silk_buffer", boom)
    monkeypatch.setattr(stt, "note_degradation_once", warned.append)
    media: dict = {}
    asyncio.run(adapter.WppAdapter._fill_voice_transcript("acc", media, b"\x02#x"))
    assert warned == ["silk 解码器不存在: /x"]
    assert "stt_text" not in media


def test_adapter_stt_failure_keeps_message_alive(monkeypatch, adapter) -> None:
    """STT 返回 None（重试到底也失败）⇒ 不写 ``stt_text``，且**不抛异常**。"""
    async def none_stt(silk, api_key=""):
        return None

    monkeypatch.setattr(stt, "should_attempt_stt", lambda m: True)
    monkeypatch.setattr(stt, "unavailable_reason", lambda: None)
    monkeypatch.setattr(stt, "transcribe_silk_buffer", none_stt)
    media: dict = {}
    asyncio.run(adapter.WppAdapter._fill_voice_transcript("acc", media, b"\x02#x"))
    assert "stt_text" not in media


# ------------------------------------------------------- 接线形状（本机唯一能红处）


def _adapter_src(adapter) -> str:
    return open(adapter.__file__, encoding="utf-8").read()


def test_media_is_fetched_before_text_persist_and_heartflow(adapter) -> None:
    """媒体必须在**正文之前**取（否则语音转写进不了正文）—— 且要在**更上游**取（W7-1）。

    这条原来判的是 ``_build_event`` 内部那句顺序（先取媒体、再 ``prepare_text``）。
    W7-1 把取媒体提到了 ``_handle_one`` 的 ④c，于是那个位置**不够了**：
    ④z（落库）与 ④a（心流缓冲）都排在 ⑤（``_build_event``）**之前**，而心流 judge
    看图（v1.11.0）抠的是 ``msg.content`` 里 ``[图片] <直链>`` 那一行 ⇒ 在 ⑤ 里取媒体，
    judge 与 DB 永远拿不到 URL。判据因此换成"全链路只有 ④c 一个取媒体点，
    且它排在**所有**消费者之前"——比原来那条更强，也不是放宽。
    """
    src = _adapter_src(adapter)
    one = src[src.index("async def _handle_one"):src.index("async def _maybe_command")]
    i_media = one.index("await self._fetch_media(acct, msg)")
    # 2026-10-18 起落库挪进了线程池：``await asyncio.to_thread(persist_inbound, ...)``。
    i_persist = min(one.index(s) for s in ("persist_inbound(acct, msg)",
                                           "to_thread(persist_inbound, acct, msg)")
                    if s in one)
    assert i_media < i_persist, \
        "取媒体必须在落库之前（DB 里那行正文要带 OSS URL，对齐 TS enrichAndSave）"
    assert i_media < one.index("self._hf_note_group(acct, msg"), \
        "取媒体必须在心流缓冲之前（judge 看图要从 content 里抠直链）"
    assert i_media < one.index("await self._build_event("), "取媒体必须在构造事件之前"
    # 2026-10-18：心流回合追加了 keyword-only 的 ``reply_expected``，调用摊成多行；
    # 不再按整句字面量匹配，改判「调用窗口里确实带上了 media」。
    _i_build = one.index("await self._build_event(")
    assert "media=media" in one[_i_build:_i_build + 240], \
        "取到的 (media_urls, media_types) 必须交给 _build_event（否则附件丢了）"

    build = src[src.index("async def _build_event"):src.index("async def _fetch_media")]
    assert "await self._fetch_media(" not in build, \
        "_build_event 不许自己再取一次 —— 取两次＝多下一次、多传一次（白花钱）"


def test_fetch_media_wires_voice_transcript(adapter) -> None:
    """``_fetch_media`` 的语音分支必须调 ``_fill_voice_transcript``（接线断了就静默丢）。"""
    src = _adapter_src(adapter)
    head = src.index("async def _fetch_media")
    body = src[head:src.index("async def _download_voice", head)]
    branch = body[body.index("if mtype == MT_VOICE"):]
    branch = branch[:branch.index("if mtype == MT_VIDEO")]
    assert "_fill_voice_transcript" in branch, "语音分支没接转写"
    assert "self._download_voice(client, msg)" in branch, "语音分支不再下载了？"
