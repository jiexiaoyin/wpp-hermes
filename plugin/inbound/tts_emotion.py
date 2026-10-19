"""TTS 情绪路由（轻量版）—— 把当前情绪映射成语音合成参数。

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
import random
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

log = logging.getLogger(__name__)

#: 情绪 → 中文表演指令。键与 :mod:`plugin.inbound.affection` 的十种情绪一致。
MOOD_INSTRUCTIONS: dict[str, str] = {
    "happy": "用开心、轻快上扬的语气说",
    "sad": "用低落、缓慢而轻柔的语气说",
    "excited": "用兴奋、充满活力的语气说",
    "calm": "用平静、温和自然的语气说",
    "angry": "用生气、压抑着怒火的语气说",
    "anxious": "用紧张、略带不安的语气说",
    "playful": "用俏皮、带笑意的语气说",
    "serious": "用严肃、沉稳、字正腔圆的语气说",
    "nostalgic": "用怀念、温柔而略带感伤的语气说",
    "curious": "用好奇、带疑问感的语气说",
}

#: 情绪 → 语速倍率（端点接受 0.5–2.0）。
MOOD_SPEEDS: dict[str, float] = {
    "happy": 1.08,
    "sad": 0.90,
    "excited": 1.15,
    "calm": 1.0,
    "angry": 1.05,
    "anxious": 1.08,
    "playful": 1.10,
    "serious": 0.98,
    "nostalgic": 0.92,
    "curious": 1.02,
}

#: 情绪 → 音色（默认空 = 用 ``tts.tokenplan.voice``；不轻易换性别）。
MOOD_VOICES: dict[str, str] = {}

@dataclass(frozen=True)
class TtsEmotionConfig:
    enabled: bool = False
    provider: str = ""
    voice: str = ""
    max_chars: int = 300
    auto_voice: float = 0.0
    auto_voice_min_chars: int = 4
    auto_voice_max_chars: int = 120
    whitelist_groups: tuple[str, ...] = ()
    whitelist_users: tuple[str, ...] = ()
    instructions: dict[str, str] = field(default_factory=lambda: dict(MOOD_INSTRUCTIONS))
    speeds: dict[str, float] = field(default_factory=lambda: dict(MOOD_SPEEDS))
    voices: dict[str, str] = field(default_factory=lambda: dict(MOOD_VOICES))
    fallback_mmx: bool = False


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_float(value: Any, default: float, *, lo: float | None = None,
              hi: float | None = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and out < lo:
        out = lo
    if hi is not None and out > hi:
        out = hi
    return out


def _as_int(value: Any, default: int, *, lo: int | None = None,
            hi: int | None = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and out < lo:
        out = lo
    if hi is not None and out > hi:
        out = hi
    return out


def _str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(x).strip() for x in value if x is not None and str(x).strip())


def _mapping(value: Any, fallback: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(fallback)
    if isinstance(value, Mapping):
        for k, v in value.items():
            key = str(k).strip().lower()
            if key:
                out[key] = v
    return out


def resolve_tts_cfg(raw: Any) -> TtsEmotionConfig:
    """账号 JSON ``tts`` 段 → 配置对象。坏值一律回落默认，绝不 raise。"""
    cfg = _as_dict(raw)
    return TtsEmotionConfig(
        enabled=bool(cfg.get("enabled")),
        provider=str(cfg.get("provider") or "").strip(),
        voice=str(cfg.get("voice") or "").strip(),
        max_chars=_as_int(cfg.get("maxChars"), 300, lo=20, hi=2000),
        auto_voice=_as_float(cfg.get("autoVoice"), 0.0, lo=0.0, hi=1.0),
        auto_voice_min_chars=_as_int(cfg.get("autoVoiceMinChars"), 4, lo=1, hi=500),
        auto_voice_max_chars=_as_int(cfg.get("autoVoiceMaxChars"), 120, lo=10, hi=1000),
        whitelist_groups=_str_tuple(cfg.get("whitelistGroups")),
        whitelist_users=_str_tuple(cfg.get("whitelistUsers")),
        # 空值 = "这个情绪不覆盖"，回落到内置指令（不是把这个情绪删掉 ——
        # 否则 ``{"happy": ""}`` 会让 happy 在情绪表里消失，build 时静默变空指令）。
        instructions={
            str(k).strip().lower(): (
                str(v).strip() or MOOD_INSTRUCTIONS.get(str(k).strip().lower(), "")
            )
            for k, v in _mapping(cfg.get("instructionByMood"),
                                 MOOD_INSTRUCTIONS).items()
        },
        speeds={k: _as_float(v, MOOD_SPEEDS.get(k, 1.0), lo=0.5, hi=2.0)
                for k, v in _mapping(cfg.get("speedByMood"), MOOD_SPEEDS).items()},
        voices={k: str(v).strip() for k, v in
                _mapping(cfg.get("voiceByMood"), MOOD_VOICES).items()
                if str(v).strip()},
        fallback_mmx=bool(cfg.get("fallbackMmx")),
    )


def normalize_mood(value: Any) -> str:
    """只接受十种已知情绪；其余一律当 ``calm``（不猜、不让脏值进 instruction）。"""
    mood = str(value or "").strip().lower()
    return mood if mood in MOOD_INSTRUCTIONS else "calm"


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


def build_voice_style(
    cfg: TtsEmotionConfig,
    mood: str,
    *,
    voice: str | None = None,
    speed: float | None = None,
    instruction: str | None = None,
) -> dict[str, Any]:
    """情绪 → ``{voice, speed, instruction}``；显式入参优先于情绪映射。"""
    mood = normalize_mood(mood)
    out_voice = (voice or "").strip() or cfg.voices.get(mood, "") or cfg.voice or None
    out_speed = speed if speed is not None else cfg.speeds.get(mood, 1.0)
    out_instr = (instruction or "").strip() or cfg.instructions.get(mood, "")
    return {
        "voice": out_voice,
        "speed": _as_float(out_speed, 1.0, lo=0.5, hi=2.0),
        "instruction": out_instr or None,
    }


def chat_is_allowed(cfg: TtsEmotionConfig, chat_id: str, *, is_group: bool) -> bool:
    """白名单为空 = 不限制；非空时只放行列出的会话。"""
    if is_group:
        allowed = cfg.whitelist_groups
    else:
        allowed = cfg.whitelist_users
    return not allowed or chat_id in allowed


def should_auto_voice(
    cfg: TtsEmotionConfig, text: str, chat_id: str, *, is_group: bool,
    rng: random.Random | None = None,
) -> bool:
    """自动语音的概率闸（纯函数，便于单测）。``autoVoice=0`` 永远 False。"""
    if not cfg.enabled or cfg.auto_voice <= 0:
        return False
    clean = (text or "").strip()
    if not (cfg.auto_voice_min_chars <= len(clean) <= cfg.auto_voice_max_chars):
        return False
    if not chat_is_allowed(cfg, chat_id, is_group=is_group):
        return False
    # 链接/长数字读出来没有意义，还会让合成变慢
    if "http://" in clean or "https://" in clean:
        return False
    draw = (rng or random).random()
    return draw < cfg.auto_voice


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


def clean_voice_text(text: str, max_chars: int = 300) -> str:
    """去掉 markdown/换行/表情噪声，避免把格式符号读出来。"""
    clean = str(text or "").strip()
    clean = re.sub(r"```.*?```", "", clean, flags=re.S)
    clean = re.sub(r"[*_#>`\[\]`]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:max_chars]


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
