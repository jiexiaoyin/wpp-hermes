"""进群欢迎语（``inbound/join_welcome.py``）的三模式契约。

三种模式是老板 2026-10-20 明确要求的：allowlist / blocklist / all。
这个模块只做**纯判定 + 渲染**，不发消息、不碰 DB，所以测试能钉死语义。

钉四件事：

1. **未知 ``mode`` 必须回落 allowlist** —— 配错宁可少发，不能全群刷屏；
2. **``enabled`` 缺省 false** —— 升级后不配就一个字都不发；
3. **``maxBatch`` 挡基线重建** —— 一次冒出十几个"新人"多半是首次同步/清表，
   照发就是全员被欢迎一遍；
4. **``quietHours`` 命中就静默**。
"""

from __future__ import annotations

import pytest

from plugin.inbound import join_welcome as jw

_GROUP_A = "111@chatroom"
_GROUP_B = "222@chatroom"


def _cfg(**kw):
    return jw.resolve_cfg({"enabled": True, **kw})


# ---------------------------------------------------------------- 默认值

def test_disabled_by_default():
    """不配就是不发 —— 升级不该让任何群突然冒出欢迎语。"""
    cfg = jw.resolve_cfg({})
    assert cfg["enabled"] is False
    assert jw.should_welcome(cfg, _GROUP_A) is False
    assert jw.resolve_cfg(None)["enabled"] is False


def test_default_mode_is_allowlist():
    assert jw.resolve_cfg({})["mode"] == jw.MODE_ALLOWLIST


def test_unknown_mode_falls_back_to_allowlist():
    """配成 ``whitelist`` / 拼错 ⇒ 按最保守的模式处理，不是"全放行"。"""
    cfg = jw.resolve_cfg({"enabled": True, "mode": "whitelist",
                          "groups": [_GROUP_A]})
    assert cfg["mode"] == jw.MODE_ALLOWLIST
    assert jw.should_welcome(cfg, _GROUP_A) is True
    assert jw.should_welcome(cfg, _GROUP_B) is False


# ---------------------------------------------------------------- 三模式

def test_allowlist_only_lists_configured_groups():
    cfg = _cfg(mode="allowlist", groups=[_GROUP_A])
    assert jw.should_welcome(cfg, _GROUP_A) is True
    assert jw.should_welcome(cfg, _GROUP_B) is False


def test_allowlist_empty_means_nobody():
    cfg = _cfg(mode="allowlist")
    assert jw.should_welcome(cfg, _GROUP_A) is False


def test_blocklist_blocks_configured_groups_only():
    cfg = _cfg(mode="blocklist", groups=[_GROUP_A])
    assert jw.should_welcome(cfg, _GROUP_A) is False
    assert jw.should_welcome(cfg, _GROUP_B) is True


def test_blocklist_empty_means_everybody():
    cfg = _cfg(mode="blocklist")
    assert jw.should_welcome(cfg, _GROUP_A) is True


def test_all_ignores_groups():
    cfg = _cfg(mode="all", groups=[_GROUP_A])
    assert jw.should_welcome(cfg, _GROUP_A) is True
    assert jw.should_welcome(cfg, _GROUP_B) is True


# ---------------------------------------------------------------- 防刷屏

def test_max_batch_blocks_baseline_rebuild():
    """一次 diff 出 4 个"新人"（默认上限 3）⇒ 当作基线重建，不发。"""
    cfg = _cfg(mode="all")
    added = [("wxid_1", "甲"), ("wxid_2", "乙"),
             ("wxid_3", "丙"), ("wxid_4", "丁")]
    assert jw.build(cfg, _GROUP_A, added, hour=10) is None


def test_max_batch_allows_a_real_newcomer():
    cfg = _cfg(mode="all")
    out = jw.build(cfg, _GROUP_A, [("wxid_1", "甲")], hour=10)
    assert out is not None
    assert "甲" in out[0]


def test_empty_added_never_sends():
    cfg = _cfg(mode="all")
    assert jw.build(cfg, _GROUP_A, [], hour=10) is None


def test_quiet_hour_suppresses():
    cfg = _cfg(mode="all", quietHours=[23, 0, 1])
    assert jw.build(cfg, _GROUP_A, [("wxid_1", "甲")], hour=0) is None
    assert jw.build(cfg, _GROUP_A, [("wxid_1", "甲")], hour=10) is not None


# ---------------------------------------------------------------- 渲染

def test_render_substitutes_all_names():
    assert jw.render("欢迎 {name}！", ["甲", "乙"]) == "欢迎 甲、乙！"


def test_render_falls_back_when_name_missing():
    """拿不到昵称时用 wxid，别渲染出「欢迎 ！」。"""
    assert jw.render("欢迎 {name}！", ["wxid_abc"]) == "欢迎 wxid_abc！"


def test_render_uses_placeholder_when_name_empty():
    assert jw.render("欢迎 {name}！", [""]) == "欢迎 新成员！"


def test_template_without_placeholder_gets_one_appended():
    """配了「进群请改备注」这种不带占位符的模板 —— 补上，不至于没人名。"""
    cfg = jw.resolve_cfg({"enabled": True, "mode": "all",
                          "template": "进群请改备注"})
    assert "{name}" in cfg["template"]


def test_mention_controls_ats():
    cfg = _cfg(mode="all", mention=True)
    text, ats = jw.build(cfg, _GROUP_A, [("wxid_1", "甲")], hour=10)
    assert ats == ["wxid_1"]

    cfg_off = _cfg(mode="all", mention=False)
    _, ats_off = jw.build(cfg_off, _GROUP_A, [("wxid_1", "甲")], hour=10)
    assert ats_off == []


def test_build_respects_mode():
    """模式不放行的群，即使有真实新人也不发。"""
    cfg = _cfg(mode="allowlist", groups=[_GROUP_A])
    assert jw.build(cfg, _GROUP_B, [("wxid_1", "甲")], hour=10) is None


# ---------------------------------------------------------------- {group}

def test_render_substitutes_group_name():
    assert jw.render("欢迎 @{name} 加入{group}！", ["甲"], "移动业务对接群") == \
        "欢迎 @甲 加入移动业务对接群！"


def test_render_group_falls_back_when_missing():
    """群名查不到时写「本群」，别渲染成「加入！」。"""
    assert jw.render("欢迎 {name} 加入{group}！", ["甲"], "") == "欢迎 甲 加入本群！"


def test_build_passes_group_name_through():
    cfg = _cfg(mode="all", template="欢迎 @{name} 加入{group}！")
    text, _ = jw.build(cfg, _GROUP_A, [("wxid_1", "甲")], hour=10,
                       group_name="测试群")
    assert text == "欢迎 @甲 加入测试群！"


# ---------------------------------------------------------------- LLM 润色

@pytest.mark.asyncio
async def test_polish_returns_base_without_profile():
    """没有群画像就别硬编 —— 原样返回模板文案。"""
    assert await jw.polish("基线", profile_text="", group_name="g",
                           model="m") == "基线"


@pytest.mark.asyncio
async def test_polish_returns_base_without_model():
    assert await jw.polish("基线", profile_text="画像", group_name="g",
                           model="") == "基线"


@pytest.mark.asyncio
async def test_polish_returns_base_on_llm_error():
    """LLM 抖动不能导致欢迎语消失。"""
    from unittest.mock import AsyncMock, patch
    with patch("plugin.llm.judge.call_judge", AsyncMock(side_effect=RuntimeError("down"))):
        assert await jw.polish("基线", profile_text="画像", group_name="g",
                               model="m") == "基线"


@pytest.mark.asyncio
async def test_polish_uses_llm_text():
    from unittest.mock import AsyncMock, patch
    with patch("plugin.llm.judge.call_judge",
               AsyncMock(return_value="欢迎 @甲 加入群！咱们主打晒单冲量，多分享～")):
        out = await jw.polish("基线", profile_text="画像", group_name="g", model="m")
    assert "晒单" in out


@pytest.mark.asyncio
async def test_polish_rejects_text_without_mention():
    """模型跑题（没 @ 任何人）⇒ 退回基线，别在群里说怪话。"""
    from unittest.mock import AsyncMock, patch
    with patch("plugin.llm.judge.call_judge", AsyncMock(return_value="随便一句话")):
        assert await jw.polish("基线", profile_text="画像", group_name="g",
                               model="m") == "基线"


@pytest.mark.asyncio
async def test_polish_strips_quotes():
    from unittest.mock import AsyncMock, patch
    with patch("plugin.llm.judge.call_judge",
               AsyncMock(return_value='"欢迎 @甲 加入群！"')):
        assert await jw.polish("基线", profile_text="画像", group_name="g",
                               model="m") == "欢迎 @甲 加入群！"
