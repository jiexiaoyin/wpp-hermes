"""TTS 情绪路由（轻量版）—— 把当前情绪映射成语音合成参数。

## 分层（扩展边界 P0-4，2026-10-19）

* **纯策略**在 :mod:`plugin.neutral.voice_style`：情绪表、配置解析、概率闸、
  文本清洗、``build_voice_style``。它不读 DB、不调 TTS、不 import WPP。
* **执行侧**留在本模块：读当前会话情绪（好感度模块）、取 Hermes TTS provider、
  合成到文件、可选回退 ``mmx``。

本模块仍 re-export 纯策略的公开名字，``toolkit/msg.py`` 与既有测试无需改动。

## 为什么不是 AstrBot 插件的移植

``astrbot_plugin_tts_emotion_router`` 做的是四类自动语音输出 + 18 种情绪 +
多服务商路由。Hermes/WPP 这边已经有自己的 TTS 后端（``tts.provider``，
当前是阿里 Token Plan 的 ``qwen-audio-3.0-tts-plus``）和 ``wpp_send_ai_voice``
工具，所以这里只补**最薄的一层**：

* 读当前群/会话情绪（好感度模块的 ``wpp_affection``）；
* 把情绪映射成 ``instruction``（自然语言表演指令）+ ``speed``（语速）+ 可选音色；
* 交给 Hermes 已注册的 TTS provider 合成，**不再 fork ``mmx``**。

自动语音（``autoVoice`` 概率）是可选旋钮，默认 0（关）。工具路径永远可用，
只要 ``tts.enabled``。

## 阿里接口的实测结论（2026-10-19）

Token Plan 原生端点
``POST /api/v1/services/audio/tts/SpeechSynthesizer``：

* ``input.instruction`` **生效**（同样文本换指令 ⇒ 音频字节不同）；
* ``parameters.instruction`` 也生效（服务端归一化），顶层 ``instruction`` 被忽略；
* ``parameters.rate`` 仍是语速；与 ``instruction`` 可同时使用；
* 内联标签（``[angry]``）也生效，但可能被读出来，故本模块**只用 instruction**。

## 默认值

``enabled=False``。开关、情绪映射、音色/语速覆盖全部由账号 JSON 的
``tts`` 段提供；未配置时**不改变任何现有行为**。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from typing import Any

from ..neutral.voice_style import (  # noqa: F401 —— 兼容旧 import（te.MOOD_* / build_*）
    MOOD_INSTRUCTIONS,
    MOOD_SPEEDS,
    MOOD_VOICES,
    TtsEmotionConfig,
    build_voice_style,
    chat_is_allowed,
    clean_voice_text,
    normalize_mood,
    resolve_tts_cfg,
    should_auto_voice,
)

log = logging.getLogger(__name__)


def get_chat_mood(account_id: str, chat_id: str, now_sec: int | None = None) -> str:
    """读当前会话情绪；任何失败都回落 ``calm``（语音不该因为 DB 抖动失败）。"""
    try:
        from ..data import repo_affection

        mood = repo_affection.get_mood(
            account_id, chat_id or "", int(now_sec if now_sec is not None else time.time()))
        return normalize_mood(getattr(mood, "mood_type", ""))
    except Exception as exc:  # noqa: BLE001 —— 情绪是装饰，不挡语音
        log.debug("[WPP TTS] 读取情绪失败（按平静处理）: %s", exc)
        return "calm"


def _hermes_provider(name: str):
    """取 Hermes 已注册的 TTS provider（懒导入：注册 probe 里没有 ``agent`` 包）。"""
    if not name:
        return None
    try:
        from agent.tts_registry import get_provider
    except Exception as exc:  # noqa: BLE001
        log.debug("[WPP TTS] Hermes TTS 注册表不可用: %s", exc)
        return None
    provider = get_provider(name)
    if provider is not None:
        return provider
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        return get_provider(name)
    except Exception as exc:  # noqa: BLE001
        log.debug("[WPP TTS] TTS provider %s 发现失败: %s", name, exc)
        return None


def _configured_provider_name() -> str:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        tts = cfg.get("tts") if isinstance(cfg, dict) else None
        return str((tts or {}).get("provider") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _mmx_synthesize(text: str, out_path: str, voice: str | None) -> None:
    cmd = ["mmx", "speech", "synthesize", "--text", text, "--out", out_path, "--quiet"]
    if voice:
        cmd += ["--voice", voice]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "mmx failed").strip()[:300])


def synthesize_to_file(
    text: str,
    out_path: str,
    cfg: TtsEmotionConfig,
    *,
    mood: str = "calm",
    voice: str | None = None,
    speed: float | None = None,
    instruction: str | None = None,
) -> str:
    """同步合成（调用方用 ``asyncio.to_thread`` 包住）。返回实际文件路径。

    优先走 Hermes 当前配置的 TTS provider（阿里 Token Plan）；provider 不可用
    且 ``fallbackMmx=True`` 时才尝试 ``mmx``。两者都失败 ⇒ raise（由工具层转成
    ``{error: ...}``，不静默发一个空文件）。
    """
    clean = (text or "").strip()
    if not clean:
        raise ValueError("text is required")
    if len(clean) > cfg.max_chars:
        clean = clean[:cfg.max_chars]

    style = build_voice_style(cfg, mood, voice=voice, speed=speed,
                              instruction=instruction)
    provider_name = cfg.provider or _configured_provider_name()
    provider = _hermes_provider(provider_name)
    last_err: Exception | None = None
    if provider is not None:
        try:
            if not provider.is_available():
                raise RuntimeError(f"TTS provider {provider.name!r} 不可用（缺 key？）")
            provider.synthesize(
                clean, out_path,
                voice=style["voice"],
                speed=style["speed"],
                instruction=style["instruction"],
            )
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return out_path
            raise RuntimeError("TTS provider 未产出音频文件")
        except Exception as exc:  # noqa: BLE001 —— 再给 mmx 一次机会（可关）
            last_err = exc
            log.warning("[WPP TTS] provider %s 合成失败: %s", provider_name or "?", exc)
    elif not provider_name:
        last_err = RuntimeError("未配置 tts.provider")
    else:
        last_err = RuntimeError(f"TTS provider {provider_name!r} 未注册")

    if cfg.fallback_mmx:
        try:
            _mmx_synthesize(clean, out_path, style.get("voice"))
            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return out_path
            raise RuntimeError("mmx 未产出音频文件")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"语音合成失败: {last_err}")


async def synthesize_to_file_async(*args: Any, **kwargs: Any) -> str:
    return await asyncio.to_thread(synthesize_to_file, *args, **kwargs)


__all__ = [
    "MOOD_INSTRUCTIONS",
    "MOOD_SPEEDS",
    "TtsEmotionConfig",
    "build_voice_style",
    "chat_is_allowed",
    "clean_voice_text",
    "get_chat_mood",
    "normalize_mood",
    "resolve_tts_cfg",
    "should_auto_voice",
    "synthesize_to_file",
    "synthesize_to_file_async",
]
