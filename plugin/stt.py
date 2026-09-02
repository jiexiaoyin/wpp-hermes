"""WPP 语音转写 STT（迁移自 wpp-openclaw storage/stt.ts）。

Pipeline: silk → decode → PCM → WAV header → SiliconFlow API → text
缺 key / 解码失败 → 返回 None（caller 决定 fallback）。
"""
from __future__ import annotations

import logging
import os
import struct
from typing import Optional

import aiohttp

from .silk import decode_silk_to_pcm

log = logging.getLogger(__name__)

SILICONFLOW_STT_URL = os.environ.get("SILICONFLOW_STT_URL", "https://api.siliconflow.cn/v1/audio/transcriptions")
STT_MODEL = os.environ.get("SILICONFLOW_STT_MODEL", "FunAudioLLM/SenseVoiceSmall")


def build_wav_buffer(pcm_buffer: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
    """PCM → WAV header（16-bit LE）。"""
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm_buffer)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, 16)
    header += b"data" + struct.pack("<I", len(pcm_buffer))
    return header + pcm_buffer


async def transcribe_silk_buffer(silk_buffer: bytes, api_key: str = "") -> Optional[str]:
    """silk buffer → STT 转写文字。失败返回 None。"""
    api_key = api_key or os.environ.get("SILICONFLOW_API_KEY", "")
    if not api_key:
        log.warning("[STT] missing SILICONFLOW_API_KEY env var")
        return None
    try:
        pcm = decode_silk_to_pcm(silk_buffer)
        if not pcm:
            return None
        wav = build_wav_buffer(pcm)
    except Exception as e:  # noqa: BLE001
        log.warning("[STT] silk decode failed: %s", e)
        return None

    try:
        # 直接 POST multipart 文件字段（SiliconFlow 接受原始 WAV）
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
                    log.warning("[STT] SiliconFlow HTTP %s", resp.status)
                    return None
                data = await resp.json()
                return data.get("text") or ""
    except Exception as e:  # noqa: BLE001
        log.warning("[STT] STT 转写失败: %s", e)
        return None
