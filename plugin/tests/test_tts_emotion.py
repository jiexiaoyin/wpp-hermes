"""TTS 情绪路由（``plugin/inbound/tts_emotion.py``，2026-10-19）。

这一层最容易坏在"看着有声音、其实情绪没生效"：instruction 传丢、未知情绪混进
prompt、自动语音概率算错（半夜给群里发语音）。判据下在**送给 provider 的参数**
与**纯函数的边界值**上。
"""

from __future__ import annotations

import asyncio

from plugin.inbound import tts_emotion as te


# ============================================================ 配置解析

def test_defaults_are_disabled_and_conservative() -> None:
    cfg = te.resolve_tts_cfg(None)
    assert cfg.enabled is False
    assert cfg.auto_voice == 0.0
    assert cfg.provider == ""
    assert cfg.max_chars == 300


def test_bad_values_fall_back_instead_of_raising() -> None:
    cfg = te.resolve_tts_cfg({
        "enabled": True, "maxChars": "abc", "autoVoice": 9,
        "autoVoiceMinChars": -5, "instructionByMood": {"happy": "  "},
    })
    assert cfg.enabled is True
    assert cfg.max_chars == 300          # 坏值 → 默认
    assert cfg.auto_voice == 1.0         # 夹到 [0,1]
    assert cfg.auto_voice_min_chars == 1
    assert cfg.instructions["happy"] == te.MOOD_INSTRUCTIONS["happy"]  # 空串不覆盖


# ============================================================ 情绪映射

def test_known_mood_maps_to_instruction_and_speed() -> None:
    style = te.build_voice_style(te.resolve_tts_cfg({"enabled": True}), "angry")
    assert style["instruction"] == te.MOOD_INSTRUCTIONS["angry"]
    assert style["speed"] == te.MOOD_SPEEDS["angry"]


def test_unknown_mood_is_calm_not_echoed() -> None:
    assert te.normalize_mood("IGNORED; rm -rf") == "calm"
    style = te.build_voice_style(te.resolve_tts_cfg({}), "nonsense")
    assert style["instruction"] == te.MOOD_INSTRUCTIONS["calm"]


def test_explicit_arguments_beat_mood_mapping() -> None:
    cfg = te.resolve_tts_cfg({"enabled": True})
    style = te.build_voice_style(cfg, "sad", voice="v1", speed=1.4,
                                 instruction="用念诗的语调说")
    assert style == {"voice": "v1", "speed": 1.4, "instruction": "用念诗的语调说"}


def test_speed_is_clamped_to_endpoint_range() -> None:
    style = te.build_voice_style(te.resolve_tts_cfg({}), "calm", speed=9)
    assert style["speed"] == 2.0


# ============================================================ 自动语音闸

def _cfg(**kw):
    return te.resolve_tts_cfg({"enabled": True, "autoVoice": 1.0, **kw})


class _Rng:
    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


def test_auto_voice_off_means_never() -> None:
    cfg = te.resolve_tts_cfg({"enabled": True, "autoVoice": 0})
    assert te.should_auto_voice(cfg, "今天天气不错", "u1", is_group=False) is False


def test_auto_voice_respects_length_window_and_links() -> None:
    cfg = _cfg(autoVoiceMinChars=4, autoVoiceMaxChars=10)
    assert te.should_auto_voice(cfg, "好", "u1", is_group=False) is False
    assert te.should_auto_voice(cfg, "今天天气真不错啊哈哈哈", "u1", is_group=False) is False
    assert te.should_auto_voice(cfg, "看 http://x.cn", "u1", is_group=False) is False
    assert te.should_auto_voice(cfg, "今天天气不错", "u1", is_group=False) is True


def test_auto_voice_whitelist_is_per_chat_kind() -> None:
    cfg = _cfg(whitelistUsers=["u1"], whitelistGroups=["g1@chatroom"])
    assert te.should_auto_voice(cfg, "今天天气不错", "u1", is_group=False) is True
    assert te.should_auto_voice(cfg, "今天天气不错", "u2", is_group=False) is False
    assert te.should_auto_voice(cfg, "今天天气不错", "g1@chatroom", is_group=True) is True
    assert te.should_auto_voice(cfg, "今天天气不错", "g2@chatroom", is_group=True) is False


def test_probability_draw_is_strictly_below() -> None:
    cfg = _cfg(autoVoice=0.5)
    assert te.should_auto_voice(cfg, "今天天气不错", "u1", is_group=False,
                                rng=_Rng(0.49)) is True
    assert te.should_auto_voice(cfg, "今天天气不错", "u1", is_group=False,
                                rng=_Rng(0.5)) is False


# ============================================================ 合成接线

def test_synthesize_passes_emotion_to_the_provider(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []

    class _Provider:
        name = "tokenplan"

        def is_available(self) -> bool:
            return True

        def synthesize(self, text, out_path, **kw):
            calls.append({"text": text, **kw})
            with open(out_path, "wb") as fh:
                fh.write(b"ID3fake")
            return out_path

    monkeypatch.setattr(te, "_hermes_provider", lambda name: _Provider())
    monkeypatch.setattr(te, "_configured_provider_name", lambda: "tokenplan")
    out = tmp_path / "v.mp3"
    got = te.synthesize_to_file("你好", str(out), _cfg(), mood="happy")
    assert got == str(out)
    assert calls[0]["text"] == "你好"
    assert calls[0]["instruction"] == te.MOOD_INSTRUCTIONS["happy"]
    assert calls[0]["speed"] == te.MOOD_SPEEDS["happy"]
    assert out.read_bytes() == b"ID3fake"


def test_synthesize_raises_when_no_provider_is_registered(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(te, "_hermes_provider", lambda name: None)
    monkeypatch.setattr(te, "_configured_provider_name", lambda: "tokenplan")
    try:
        te.synthesize_to_file("你好", str(tmp_path / "v.mp3"), _cfg())
    except RuntimeError as exc:
        assert "语音合成失败" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("provider 缺失时必须 raise（工具层要把它变成 error）")


def test_async_wrapper_runs_in_a_thread(tmp_path, monkeypatch) -> None:
    class _Provider:
        name = "tokenplan"

        def is_available(self) -> bool:
            return True

        def synthesize(self, text, out_path, **kw):
            with open(out_path, "wb") as fh:
                fh.write(b"x")
            return out_path

    monkeypatch.setattr(te, "_hermes_provider", lambda name: _Provider())
    out = tmp_path / "v.mp3"
    got = asyncio.run(te.synthesize_to_file_async("你好", str(out), _cfg()))
    assert got == str(out)


# ============================================================ 文本清洗

def test_clean_voice_text_strips_markdown_and_collapses_whitespace() -> None:
    assert te.clean_voice_text("**你好**\n\n[图片] `代码`  \n世界") == "你好 图片 代码 世界"


def test_clean_voice_text_truncates_to_max_chars() -> None:
    assert te.clean_voice_text("一二三四五", 3) == "一二三"
