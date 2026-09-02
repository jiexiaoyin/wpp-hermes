"""WPP STT skill (Phase 2.3).

从 wechatpadro plugin 抽出: 语音转文字.

调用方提供 silk_bytes (原始 silk) + 可选 vendor_transcript.
Skill 优先级:
  1. vendor_transcript (vendor 自带, 免费)
  2. SiliconFlow STT (降级, 消耗 token)
"""
from __future__ import annotations

import logging
import os
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# 配置 (可通过 env 覆盖)
SILICONFLOW_STT_URL = os.environ.get(
    "SILICONFLOW_STT_URL", "https://api.siliconflow.cn/v1/audio/transcriptions"
)
STT_MODEL = os.environ.get(
    "SILICONFLOW_STT_MODEL", "FunAudioLLM/SenseVoiceSmall"
)
DEFAULT_SILK_DECODER = os.environ.get(
    "WPP_SILK_DECODER_PATH", "/root/silk_decoder/silk/decoder"
)


@dataclass
class STTResult:
    """STT 转写结果."""
    text: str                    # 转写文字
    source: str                  # "vendor" | "siliconflow" | "none"
    duration_ms: Optional[int] = None  # 语音时长 (ms)


# ---------- silk → PCM ----------
def build_wav_buffer(pcm_buffer: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
    """PCM → WAV header (16-bit LE)."""
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm_buffer)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, 16)
    header += b"data" + struct.pack("<I", len(pcm_buffer))
    return header + pcm_buffer


def decode_silk_to_pcm(silk_buffer: bytes, decoder_path: str = DEFAULT_SILK_DECODER) -> Optional[bytes]:
    """silk → PCM (调 silk decoder 二进制)."""
    if not os.path.exists(decoder_path):
        log.warning("[wpp-stt] silk decoder 不存在: %s", decoder_path)
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".silk", delete=False) as f:
            f.write(silk_buffer)
            silk_path = f.name
        pcm_path = silk_path + ".pcm"
        result = subprocess.run(
            [decoder_path, silk_path, pcm_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and os.path.exists(pcm_path):
            with open(pcm_path, "rb") as f:
                data = f.read()
            os.remove(silk_path)
            os.remove(pcm_path)
            return data
        os.remove(silk_path)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("[wpp-stt] silk 解码失败: %s", e)
        return None


# ---------- SiliconFlow STT ----------
async def _siliconflow_stt(silk_buffer: bytes, api_key: str = "") -> Optional[str]:
    """silk → SiliconFlow STT API → text. 失败返回 None."""
    api_key = api_key or os.environ.get("SILICONFLOW_API_KEY", "")
    if not api_key:
        log.warning("[wpp-stt] missing SILICONFLOW_API_KEY env var")
        return None
    try:
        import aiohttp
        pcm = decode_silk_to_pcm(silk_buffer)
        if not pcm:
            return None
        wav = build_wav_buffer(pcm)
        form = aiohttp.FormData()
        form.add_field("model", STT_MODEL)
        form.add_field("file", wav, filename="voice.wav", content_type="audio/wav")
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                SILICONFLOW_STT_URL,
                data=form,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.warning("[wpp-stt] SiliconFlow HTTP %s", resp.status)
                    return None
                data = await resp.json()
                return data.get("text") or ""
    except Exception as e:  # noqa: BLE001
        log.warning("[wpp-stt] STT 转写失败: %s", e)
        return None


# ---------- 统一入口 ----------
async def transcribe(
    silk_bytes: Optional[bytes] = None,
    vendor_transcript: str = "",
    api_key: str = "",
) -> STTResult:
    """统一 STT 转写入口.

    Args:
        silk_bytes: silk 原始字节 (download_voice 后)
        vendor_transcript: vendor 自带 transcript (msg.media.vendor_transcript), 优先用
        api_key: SiliconFlow API key (可选, 默认从 env SILICONFLOW_API_KEY 读)

    Returns:
        STTResult(text, source, duration_ms)
        - source: "vendor" (vendor transcript), "siliconflow" (STT), "none" (失败)
    """
    # 1. 优先 vendor transcript
    vendor_text = (vendor_transcript or "").strip()
    if vendor_text:
        log.info("[wpp-stt] 用 vendor transcript (%d 字)", len(vendor_text))
        return STTResult(text=vendor_text, source="vendor")

    # 2. 降级 SiliconFlow STT
    if not silk_bytes:
        log.warning("[wpp-stt] 无 silk_bytes 也无 vendor_transcript, 返回空")
        return STTResult(text="", source="none")

    text = await _siliconflow_stt(silk_bytes, api_key)
    if text:
        log.info("[wpp-stt] SiliconFlow STT 成功 (%d 字)", len(text))
        return STTResult(text=text, source="siliconflow")

    log.warning("[wpp-stt] STT 全部失败, 返回空")
    return STTResult(text="", source="none")