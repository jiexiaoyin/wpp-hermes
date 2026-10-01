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
import shutil
import tempfile
from typing import Optional

log = logging.getLogger(__name__)

#: silk encoder 二进制。默认值与 TS ``WPP_SILK_ENCODER_PATH`` 一致。
SILK_ENCODER_PATH = os.environ.get("WPP_SILK_ENCODER_PATH", "/root/silk_decoder/silk/encoder")

#: 厂商实际接受的 silk 上限。矩阵见模块 docstring（60000 是实测推断值，留一点余量）。
MAX_SILK_BYTES = 60_000

#: 微信语音时长上限（TS ``MAX_VOICE_DURATION_MS``）
MAX_VOICE_DURATION_MS = 60_000

_ENCODE_TIMEOUT = 60


def unavailable_reason() -> Optional[str]:
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


async def encode_to_silk(ref: str) -> Optional[str]:
    """本地音频文件 → SILK 的 **base64**；不可用/失败一律 ``None``（调用方降级发文件）。

    ``ref`` 必须是**本地路径** —— URL 在 :func:`plugin.platform.outbound.resolve_to_base64`
    阶段已被 SSRF 校验并落地，这里不再处理远程。
    """
    reason = unavailable_reason()
    if reason:
        log.info("[SILK] 跳过编码（%s）—— 出站语音将降级为文件", reason)
        return None

    src = os.path.expanduser(ref)
    if not os.path.isfile(src):
        log.warning("[SILK] 源文件不存在: %s", src)
        return None

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
            log.warning("[SILK] ffmpeg 转 PCM 失败: %s", (err or b"")[:300])
            return None

        # 2. PCM → silk（TS: -tencent -quiet）
        proc = await asyncio.create_subprocess_exec(
            SILK_ENCODER_PATH, pcm_path, silk_path, "-tencent", "-quiet",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=_ENCODE_TIMEOUT)
        if proc.returncode != 0 or not os.path.isfile(silk_path):
            log.warning("[SILK] encoder 失败: %s", (err or b"")[:300])
            return None

        with open(silk_path, "rb") as fh:
            data = fh.read()

        # 3. 尺寸闸：超限直接放弃 ⇒ 让调用方发文件，别让厂商用误导性的 -2 拒收
        if len(data) > MAX_SILK_BYTES:
            log.warning("[SILK] silk 过大 %d B > %d B —— 降级发文件（厂商会以 Code=-2 拒收）",
                        len(data), MAX_SILK_BYTES)
            return None
        if not data.startswith(b"\x02"):
            log.warning("[SILK] encoder 输出不是 SILK 头（首字节 %r）", data[:2])
            return None
        return base64.b64encode(data).decode()
    except Exception as exc:  # noqa: BLE001 — 编码失败绝不能冒泡成「发送失败」
        log.warning("[SILK] 编码异常: %s", exc)
        return None
    finally:
        for path in (pcm_path, silk_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
