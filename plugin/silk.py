"""WPP silk 语音编解码（迁移自 wpp-openclaw storage/silk.ts）。

编码: MP3/PCM → silk（出站语音发送）
解码: silk → PCM（入站 STT 转写前置）
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_SILK_ENCODER = os.environ.get("WPP_SILK_ENCODER_PATH", "/root/silk_decoder/silk/encoder")
DEFAULT_SILK_DECODER = os.environ.get("WPP_SILK_DECODER_PATH", "/root/silk_decoder/silk/decoder")


def _cleanup(*paths: str) -> None:
    """尽力清理临时文件（P3-1：错误路径也清理，防 /tmp 泄漏）。"""
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def decode_silk_to_pcm(silk_buffer: bytes) -> Optional[bytes]:
    """silk → PCM。"""
    decoder = DEFAULT_SILK_DECODER
    if not os.path.exists(decoder):
        log.warning("[SILK] decoder 不存在: %s", decoder)
        return None
    silk_path = pcm_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".silk", delete=False) as f:
            f.write(silk_buffer)
            silk_path = f.name
        pcm_path = silk_path + ".pcm"
        result = subprocess.run([decoder, silk_path, pcm_path], capture_output=True, timeout=30)
        if result.returncode == 0 and os.path.exists(pcm_path):
            with open(pcm_path, "rb") as f:
                data = f.read()
            return data
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("[SILK] 解码失败: %s", e)
        return None
    finally:
        _cleanup(silk_path, pcm_path)


def encode_pcm_to_silk(pcm_buffer: bytes, sample_rate: int = 24000) -> Optional[bytes]:
    """PCM → silk。"""
    encoder = DEFAULT_SILK_ENCODER
    if not os.path.exists(encoder):
        log.warning("[SILK] encoder 不存在: %s", encoder)
        return None
    pcm_path = silk_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
            f.write(pcm_buffer)
            pcm_path = f.name
        silk_path = pcm_path + ".silk"
        result = subprocess.run([encoder, pcm_path, silk_path, str(sample_rate)], capture_output=True, timeout=30)
        if result.returncode == 0 and os.path.exists(silk_path):
            with open(silk_path, "rb") as f:
                data = f.read()
            return data
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("[SILK] 编码失败: %s", e)
        return None
    finally:
        _cleanup(pcm_path, silk_path)
