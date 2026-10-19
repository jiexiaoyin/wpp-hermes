"""通道无关的语音风格策略（扩展边界 P0-4，2026-10-19）。

从 ``inbound/tts_emotion.py`` 拆出的纯逻辑：情绪 → ``instruction`` / ``speed`` /
``voice`` 的映射、配置解析、自动语音概率闸、文本清洗。这里不读 DB、不调 TTS、
不 import 任何 WPP 模块。

执行侧（读当前会话情绪、调用 Hermes TTS provider、失败回退 mmx）仍在
``plugin/inbound/tts_emotion.py``；它 import 本模块并保持原公开 API 不变。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

#: 情绪 → 中文表演指令。键与好感度模块的十种情绪一致。
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

#: 情绪 → 音色（默认空 = 用 provider 默认音色；不轻易换性别）。
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
    "MOOD_VOICES",
    "TtsEmotionConfig",
    "build_voice_style",
    "chat_is_allowed",
    "clean_voice_text",
    "normalize_mood",
    "resolve_tts_cfg",
    "should_auto_voice",
]
