"""出站语音 → 微信 SILK 编码（vendor ``/Msg/SendVoice`` 唯一认的格式）。

## 为什么必须有这一层（2026-08-12 生产实证，别删）

厂商 ``/Msg/SendVoice`` **只接受 SILK base64**（24kHz mono，``0x02 + !SILK_V3`` 头）。
把 mp3 base64 直接传过去，厂商**静默拒收**（``ret=-2``）——而外层 wrapper 当时还
报 ``ok=true``（"AI 误判 framework sendMessage ok=true 是真成功"），所以这个坑
表现为「语音发出去没人听见，日志却说成功」。

老板 8-12 11:11–11:18 curl 实测的尺寸矩阵（**写死在这里，别再猜**）::

    5807 B (2s 23kbps)      → Code=0   ✅
    42450 B (15s 23kbps)    → Code=0   ✅
    53631 B (30s 14kbps)    → Code=0   ✅
    67361 B (30s 18kbps)    → 边界（未定）
    85836 B (30s 23kbps)    → Code=-2  ❌
    121809 B (30s 16kHz)    → 拒绝（太大）

⚠️ ``Code=-2`` 的文案是 **INVALID_CREDENTIAL** —— **误导**。真因是 silk 太大/格式不对，
不是凭证问题。排查时别被这个字符串带偏（老板铁律：不许被推测路线带偏）。

## 本机的现实

``.183`` 上 **既没有 ffmpeg 也没有 silk encoder 二进制**（``/root/silk_decoder`` 不存在）
⇒ 这里的 :func:`encode_to_silk` 永远返回 ``None``，出站语音走**降级发文件**
（这正是老板 6-12 16:36 定的偏好："成功发为语音, 失败降级文件"）。
所以本模块在 .183 上的可测路径就是「探测不可用 → None」—— 也就是生产路径，
不是死码。

## 编码链（对齐 TS ``dispatch/silk-encoder.ts`` v1.3.48/1.3.51）

    mp3/wav/… --ffmpeg(-ar 24000 -ac 1)--> PCM --encoder(-tencent -quiet)--> silk

两个二进制缺一不可：只有 encoder、没有 ffmpeg ⇒ 拿不到 24kHz mono PCM ⇒ 一样降级。
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import tempfile

log = logging.getLogger(__name__)

#: silk encoder 二进制。默认值与 TS ``WPP_SILK_ENCODER_PATH`` 一致。
SILK_ENCODER_PATH = os.environ.get("WPP_SILK_ENCODER_PATH", "/root/silk_decoder/silk/encoder")

#: 厂商实际接受的 silk 上限。矩阵见模块 docstring（60000 是实测推断值，留一点余量）。
MAX_SILK_BYTES = 60_000

#: 微信语音时长上限（TS ``MAX_VOICE_DURATION_MS``）
MAX_VOICE_DURATION_MS = 60_000

#: 分段时主动留 5 秒余量（编码/容器取整会让 60.0s 变成 60.1s ⇒ 厂商拒收）。
MAX_VOICE_SEGMENT_MS = MAX_VOICE_DURATION_MS - 5_000

#: 超限时依次尝试的 SILK 码率。默认 25 kbps 下 60 KB 只够 ~19 秒；语音消息
#: 本身是低码率场景，12/8/6 kbps 能把 55 秒的段压进厂商 60 KB 的字节闸，
#: 避免一条 1 分钟的话被切成 4 条气泡。
#:
#: 2026-10-19 实测：纯音 55 秒 @8 kbps = 60,696 B（**仍超** 60 KB，silk 帧头/
#: 容器有固定开销，不是简单的 ``码率×时长``）；@6 kbps = 51,724 B 才留出余量。
#: 所以 6 kbps 这档不能删，否则 55 秒无静音段会整段降级成文件。
_SILK_FALLBACK_RATES = (None, 12_000, 8_000, 6_000)

_ENCODE_TIMEOUT = 60


class SilkTooLarge(Exception):
    """SILK 编码结果超过厂商上限（内部信号，不对外抛出）。"""


class SilkUnavailable(Exception):
    """本机缺少 ffmpeg / encoder 二进制（内部信号，不对外抛出）。"""


def unavailable_reason() -> str | None:
    """为什么编码不可用 —— ``None`` 表示可用。纯探测，**无副作用**。"""
    if not os.path.exists(SILK_ENCODER_PATH):
        return f"silk encoder 不存在: {SILK_ENCODER_PATH}"
    if not os.access(SILK_ENCODER_PATH, os.X_OK):
        return f"silk encoder 不可执行: {SILK_ENCODER_PATH}"
    if not shutil.which("ffmpeg"):
        return "ffmpeg 不在 PATH（拿不到 24kHz mono PCM）"
    return None


def is_available() -> bool:
    return unavailable_reason() is None


def looks_like_silk_bytes(data: bytes) -> bool:
    """SILK 原始字节判定（``0x02#!SILK_V3`` 头）。

    2026-10-19 审阅补的：``wpp_send_voice`` 只看文件名/``data:audio/silk``，
    模型直接传裸 SILK base64 时会被当成 mp3 降级成文件（语音能力"明明有却没用上"）。
    """
    return bool(data) and data[:1] == b"\x02" and b"SILK" in data[:10]


def looks_like_silk_base64(b64: str) -> bool:
    try:
        raw = base64.b64decode((b64 or "").strip(), validate=False)[:16]
    except Exception:  # noqa: BLE001
        return False
    return looks_like_silk_bytes(raw)


async def probe_silk_base64_ms(b64: str) -> int:
    """从 SILK base64 直接算时长（毫秒）。

    ``probe_duration_ms`` 只认本地文件；URL/base64 入参在落地前没有路径可用。
    SILK 解码后是 24kHz/mono/s16 ⇒ 48 字节/毫秒。失败返回 0（调用方必须降级，
    **不能**把 0 传给厂商 —— 实测 ``VoiceTime=0`` 必被 ``Code=-2`` 拒收）。
    """
    try:
        raw = base64.b64decode((b64 or "").strip(), validate=False)
        if not looks_like_silk_bytes(raw):
            return 0
        from ..inbound import stt

        pcm = await stt.decode_silk_to_pcm(raw)
        return int(round(len(pcm) / 48)) if pcm else 0
    except Exception as exc:  # noqa: BLE001
        log.debug("[SILK] base64 时长探测失败: %s", exc)
        return 0


async def probe_duration_ms(ref: str) -> int:
    """本地音频时长（毫秒）。探测失败返回 ``0``。

    ⚠️ ``/Msg/SendVoice`` 的 ``VoiceTime`` **不能传 0**：2026-10-19 实测 2.4 秒的
    SILK 传 0 被厂商以 ``Code=-2`` 拒收（文案仍是误导性的 INVALID_CREDENTIAL），
    传 ``2376`` 立即 ``Code=0 成功``。TS 侧一直传的是 ``voiceDurationMs``。
    """
    src = os.path.expanduser(str(ref or ""))
    if not os.path.isfile(src):
        return 0
    try:
        if src.lower().endswith(".silk"):
            # 解码成 24kHz/mono/16bit PCM：48000 字节/秒 ⇒ 48 字节/毫秒
            from ..inbound import stt

            with open(src, "rb") as fh:
                pcm = await stt.decode_silk_to_pcm(fh.read())
            return int(round(len(pcm) / 48)) if pcm else 0
        import asyncio

        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", src,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return 0
        return int(round(float(out.decode("ascii", "ignore").strip() or 0) * 1000))
    except Exception as exc:  # noqa: BLE001 —— 时长只是展示字段，探测失败不能挡发送
        log.debug("[SILK] 时长探测失败 %s: %s", src, exc)
        return 0


def plan_segments(duration_ms: int, silences: list[tuple[float, float]] | None = None,
                  max_ms: int = MAX_VOICE_SEGMENT_MS) -> list[tuple[float, float]]:
    """把总时长切成 ≤ ``max_ms`` 的若干段（纯函数，便于单测）。

    优先在**静音结束点**切：TTS 的句号/逗号处通常有 0.2–0.8s 静音，切在那里
    听感最自然；找不到合适静音才退化为硬切。返回 ``[(start_s, end_s), …]``。
    """
    duration = max(0.0, float(duration_ms) / 1000.0)
    limit_s = max(1.0, float(max_ms) / 1000.0)
    if duration <= 0:
        return []
    if duration <= limit_s:
        return [(0.0, duration)]

    ends = sorted({float(end) for _start, end in (silences or []) if end and end > 0})
    out: list[tuple[float, float]] = []
    start = 0.0
    while duration - start > limit_s + 0.05:
        hard_limit = start + limit_s
        # 至少留 1 秒，避免静音点过密切出碎片
        candidates = [end for end in ends if start + 1.0 <= end <= hard_limit]
        cut = max(candidates) if candidates else hard_limit
        out.append((start, cut))
        start = cut
    if duration - start > 0.05:
        out.append((start, duration))
    return out


async def _detect_silences(src: str, timeout: float = 60.0) -> list[tuple[float, float]]:
    """``ffmpeg silencedetect`` 找静音区间；失败返回空列表（调用方硬切）。"""
    try:
        import asyncio

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-nostats", "-i", src,
            "-af", "silencedetect=noise=-35dB:d=0.30", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = (err or b"").decode("utf-8", "ignore")
        starts: list[float] = []
        pairs: list[tuple[float, float]] = []
        for line in text.splitlines():
            m = re.search(r"silence_start:\s*(-?\d+(?:\.\d+)?)", line)
            if m:
                starts.append(float(m.group(1)))
                continue
            m = re.search(r"silence_end:\s*(-?\d+(?:\.\d+)?)", line)
            if m and starts:
                pairs.append((starts.pop(0), float(m.group(1))))
        return pairs
    except Exception as exc:  # noqa: BLE001 —— 只是切点优化，失败就硬切
        log.debug("[SILK] silencedetect 失败 %s: %s", src, exc)
        return []


async def _silk_to_wav(src: str, out_path: str) -> bool:
    """``.silk`` → 24kHz mono wav（分段前先解码；失败返回 False）。"""
    try:
        import wave

        from ..inbound import stt

        with open(src, "rb") as fh:
            pcm = await stt.decode_silk_to_pcm(fh.read())
        if not pcm:
            return False
        with wave.open(out_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(pcm)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("[SILK] 解码 .silk 失败，无法分段: %s", exc)
        return False


async def _cut_segment(src: str, start_s: float, end_s: float, out_path: str) -> bool:
    try:
        import asyncio

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", src,
            "-ss", f"{start_s:.3f}", "-t", f"{max(0.05, end_s - start_s):.3f}",
            "-vn", "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", out_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=_ENCODE_TIMEOUT)
        if proc.returncode != 0:
            log.warning("[SILK] 分段失败 %.1f-%.1f: %s",
                        start_s, end_s, (err or b"")[:200])
            return False
        return os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("[SILK] 分段异常 %.1f-%.1f: %s", start_s, end_s, exc)
        return False


async def split_audio(ref: str, max_ms: int = MAX_VOICE_SEGMENT_MS) -> list[str]:
    """把超过微信语音上限的音频切成多段，返回本地路径列表。

    * 未超限 / 探测不到时长 / ffmpeg 不可用 ⇒ 原样返回 ``[ref]``；
    * 切点优先落在静音处（见 :func:`plan_segments`）；
    * 返回的临时文件用 :func:`cleanup_segments` 清理。
    """
    src = os.path.expanduser(str(ref or ""))
    if not os.path.isfile(src):
        return [src]
    duration_ms = await probe_duration_ms(src)
    if duration_ms <= 0 or duration_ms <= max_ms:
        return [src]

    tmpdir = tempfile.mkdtemp(prefix="wpp-voice-split-")
    source = src
    if src.lower().endswith(".silk"):
        converted = os.path.join(tmpdir, "source.wav")
        if not await _silk_to_wav(src, converted):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return [src]
        source = converted

    try:
        silences = await _detect_silences(source)
        plan = plan_segments(duration_ms, silences, max_ms=max_ms)
        if len(plan) <= 1:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return [src]
        out: list[str] = []
        for idx, (start_s, end_s) in enumerate(plan):
            path = os.path.join(tmpdir, f"part-{idx + 1:02d}.wav")
            if not await _cut_segment(source, start_s, end_s, path):
                shutil.rmtree(tmpdir, ignore_errors=True)
                return [src]
            out.append(path)
        log.info("[SILK] 语音 %.1fs 超过 %.1fs，切为 %d 段",
                 duration_ms / 1000.0, max_ms / 1000.0, len(out))
        return out
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def cleanup_segments(paths: list[str] | None) -> None:
    """删除 :func:`split_audio` 产生的临时目录（原始文件不动）。"""
    for path in paths or []:
        folder = os.path.dirname(os.path.abspath(str(path or "")))
        if os.path.basename(folder).startswith("wpp-voice-split-"):
            shutil.rmtree(folder, ignore_errors=True)


async def _encode_silk_bytes(src: str, bitrate: int | None = None) -> bytes:
    """编码成 SILK 原始字节。失败抛异常（由公开包装函数降级成 ``None``）。"""
    reason = unavailable_reason()
    if reason:
        raise SilkUnavailable(reason)

    pcm_path = silk_path = ""
    try:
        import asyncio

        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as fh:
            pcm_path = fh.name
        silk_path = pcm_path + ".silk"

        # 1. 任意音频 → PCM 24kHz mono（TS: -ar 24000 -ac 1）
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", src,
            "-ar", "24000", "-ac", "1", "-f", "s16le", pcm_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=_ENCODE_TIMEOUT)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg 转 PCM 失败: {(err or b'')[:300]!r}")

        # 2. PCM → silk（TS: -tencent -quiet）；超限时用 -rate 降码率再试
        cmd = [SILK_ENCODER_PATH, pcm_path, silk_path, "-tencent", "-quiet"]
        if bitrate:
            cmd += ["-rate", str(int(bitrate))]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=_ENCODE_TIMEOUT)
        if proc.returncode != 0 or not os.path.isfile(silk_path):
            raise RuntimeError(f"encoder 失败: {(err or b'')[:300]!r}")

        with open(silk_path, "rb") as fh:
            data = fh.read()

        # 3. 尺寸闸：超限抛专用信号，调用方换更低码率或分段
        if len(data) > MAX_SILK_BYTES:
            raise SilkTooLarge(f"{len(data)} B > {MAX_SILK_BYTES} B")
        if not data.startswith(b"\x02"):
            raise RuntimeError(f"encoder 输出不是 SILK 头（首字节 {data[:2]!r}）")
        return data
    finally:
        for path in (pcm_path, silk_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


async def encode_to_silk(ref: str, bitrate: int | None = None) -> str | None:
    """本地音频文件 → SILK 的 **base64**；不可用/失败一律 ``None``（调用方降级发文件）。

    ``ref`` 必须是**本地路径** —— URL 在 :func:`plugin.platform.outbound.resolve_to_base64`
    阶段已被 SSRF 校验并落地，这里不再处理远程。
    """
    src = os.path.expanduser(ref)
    if not os.path.isfile(src):
        log.warning("[SILK] 源文件不存在: %s", src)
        return None
    try:
        return base64.b64encode(await _encode_silk_bytes(src, bitrate)).decode()
    except SilkUnavailable as exc:
        log.info("[SILK] 跳过编码（%s）—— 出站语音将降级为文件", exc)
    except SilkTooLarge as exc:
        log.warning("[SILK] silk 过大 %s —— 降级发文件（厂商会以 Code=-2 拒收）", exc)
    except Exception as exc:  # noqa: BLE001 — 编码失败绝不能冒泡成「发送失败」
        log.warning("[SILK] 编码异常: %s", exc)
    return None


async def encode_to_silk_fitting(ref: str) -> str | None:
    """优先 25 kbps；超厂商 60 KB 字节闸时依次降到 12/8/6 kbps。

    60 KB 是**字节**上限，不是 60 秒：25 kbps 下只够 ~19 秒。降码率让
    30–55 秒的段仍能作为一条语音气泡发出，避免过度切碎。
    """
    src = os.path.expanduser(ref)
    if not os.path.isfile(src):
        log.warning("[SILK] 源文件不存在: %s", src)
        return None
    # 2026-10-19 审阅修复：>60 KB 的 .silk 以前直接走 ``ffmpeg -i .silk``，而 ffmpeg
    # 不认 SILK（本机没有 libsilk）⇒ 编码失败 ⇒ 降级成**发一个打不开的 .silk 附件**。
    # 先用自带 decoder 解成 24k mono wav，再走正常的降码率重编码。
    tmpdir = ""
    if src.lower().endswith(".silk"):
        tmpdir = tempfile.mkdtemp(prefix="wpp-voice-silk-")
        wav = os.path.join(tmpdir, "source.wav")
        if not await _silk_to_wav(src, wav):
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None
        src = wav
    try:
        last: Exception | None = None
        for bitrate in _SILK_FALLBACK_RATES:
            try:
                return base64.b64encode(await _encode_silk_bytes(src, bitrate)).decode()
            except SilkUnavailable as exc:
                log.info("[SILK] 跳过编码（%s）—— 出站语音将降级为文件", exc)
                return None
            except SilkTooLarge as exc:
                last = exc
                log.info("[SILK] %s kbps 仍超限（%s），尝试更低码率",
                         (bitrate or 25_000) // 1000, exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("[SILK] 编码异常: %s", exc)
                return None
        log.warning("[SILK] 降码率后仍超过 %d B（%s）", MAX_SILK_BYTES, last)
        return None
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
