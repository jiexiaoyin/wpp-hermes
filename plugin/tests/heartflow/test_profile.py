"""心流群画像回归 —— TS ``tests/unit/heartflow-profile.test.mjs`` (387 行) 的 pytest 译本。

## 这一批锁住三类东西

1. **纯函数**：参数缺省 / 生成 prompt / 解析校验 / 截断 / 缓存 / 新旧判定。
2. **安全边界**（最重要）：画像只能收紧约束（阈值抬高 / 预算变小 / 静默段默认不生效），
   且**永不写空画像**。
3. **源级接线**：sweep 生成 → 缓存 → judge prompt 注入 + 触发侧预算收紧 + 命令面。

数值断言尽量用**语义**而非魔数：「画像建议 0.2 的阈值必须被抬到 bandMin」，而不是断言 0.5 == 0.5。

## 与 TS 版的对应

=================  ==================================================================
TS 1–8（参数/prompt/解析/ 本文件 §1–§8 一一对应
预算/渲染/缓存）
TS §6 接线 11–14     拆成 §9（**行为级**：假数据层 + 假 judge 真跑编排）+ §10（源级守卫）。
                     TS 那几条断言的主语在 Py 侧搬家了：``handler.ts`` / ``triggers.ts`` /
                     ``filehelper-features.ts`` 的等价物（适配器、filehelper 命令面）
                     **还没移植**，所以命令面只留"还没到"的显式记录。
TS §6 接线 13        循环依赖 + judge prompt 注入：注入点在 ``heartflow.py``（#9），
                     本文件只钉"profile 侧不许反向 import"，注入侧留给 test_heartflow.py。
=================  ==================================================================

真实日期/生产数字一律不进本文件（公开仓红线）；样本全是构造的。
"""

from __future__ import annotations

import ast
import sys
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from plugin.data import repo_hf, repo_messages
from plugin.inbound.heartflow import budget as B
from plugin.inbound.heartflow import profile as P
from plugin.inbound.heartflow.runtime import CACHE_REGISTRY

PROFILE_SRC = Path(P.__file__).read_text(encoding="utf-8")
HF_SRC = Path(repo_hf.__file__).read_text(encoding="utf-8")
MSGS_SRC = Path(repo_messages.__file__).read_text(encoding="utf-8")

NOW = int(datetime(2026, 9, 20, 20, 30).timestamp())

#: 一份"好"的画像响应（LLM 的 snake_case 原样）
GOOD = json.dumps({
    "nature": "手机零售商的客户售后群",
    "style": "短句口语, 爱用表情",
    "bot_role": "答疑的客服",
    "engage": ["售后", "价格", "新品"],
    "avoid": ["政治", "同行业内幕"],
    "active_hours": [9, 10, 14, 20],
    "quiet_hours": [[23, 7]],
    "band": 0.72,
    "budget": {"min_gap_sec": 300, "max_per_hour": 6, "max_per_day": 40},
    "summary": "群里问售后就答, 别抢话",
}, ensure_ascii=False)

#: 统计素材 —— 形状与 ``repo_messages.get_hf_group_message_stats`` 的返回**逐键一致**
STATS = {
    "total": 200,
    "activeDays": 12,
    "avgLen": 18,
    "hourHist": [0] * 9 + [40, 30] + [0] * 3 + [35] + [0] * 5 + [25] + [0, 0, 0, 0],
    "topSenders": [{"wxid": "wxid_a", "n": 40}, {"wxid": "wxid_b", "n": 30}],
    "typeHist": {"text": 180, "image": 20},
}


def msg(direction: str, content: str, at_sec: int, from_wxid: str = "wxid_a") -> dict:
    """给 ``_load_sample_lines`` 的行 —— 形状与 ``repo_messages.list_messages_by_peer``
    的 ``SELECT`` 逐列一致（§10 有断言钉住）。"""
    return dict(direction=direction, peer_kind="group", peer_id="g1@chatroom",
                chat_id="g1@chatroom", msg_id="m", new_msg_id="n", msg_type="text",
                content=content, from_wxid=from_wxid, peer_name="群", at_sec=at_sec)


def prof_row(group_id: str, profile_json: str = GOOD, *, version: int = 3,
             generated_at: int = 111, sample_msgs: int = 40,
             model: str = "m", stats_json: str | None = None) -> dict:
    """给 ``load_hf_profiles_into_cache`` 的行 —— 形状与 ``repo_hf.list_profiles``
    的 ``SELECT`` 逐列一致（§10 有断言钉住）。"""
    return dict(account_id="acc1", group_id=group_id, profile_json=profile_json,
                stats_json=stats_json, sample_msgs=sample_msgs, model=model,
                version=version, generated_at=generated_at)


# ===== 1. 参数 =====


def test_resolve_cfg_defaults():
    c = P.resolve_hf_profile_cfg(None)
    assert c.enabled is True, "画像默认开（老板要的就是它）"
    assert c.min_msgs == 30
    assert c.lookback_days == 14
    assert c.apply_quiet_hours is False, "静默段默认**不自动生效**（老板 2026-09-26 拍板「默认关」）"
    assert c.max_prompt_chars == 400, "注入上限 400 字符（计划值）"
    assert c.max_raise == P.HF_PROFILE_MAX_RAISE == 0.1
    o = P.resolve_hf_profile_cfg({"profile": {"maxTokens": 2000}})
    assert o.max_tokens == 2000, "覆盖生效"
    assert o.min_msgs == c.min_msgs, "未覆盖的键走默认"


def test_profile_max_tokens_never_reuses_judge_300():
    """2026-09-13 那次 judge 静默瘫 3 天：推理模型的思考与正文共享 ``max_tokens``。

    画像要写的是一整份结构化 JSON，比 judge 短 JSON 吃 token 得多 —— 复用 300 就是同一个故障。
    （judge 那处硬编码 300 仍在 ``heartflow.py`` 里，本条只说"不复用它"；那条 pin 归 test_heartflow.py。）
    """
    assert P.HF_PROFILE_DEFAULTS["maxTokens"] != 300
    assert P.HF_PROFILE_DEFAULTS["maxTokens"] >= 800
    assert P.HF_PROFILE_DEFAULTS["maxTokens"] != 300


@pytest.mark.parametrize(("dirty", "field", "default"), [
    ({"minMsgs": "abc"}, "min_msgs", 30),
    ({"minMsgs": 0}, "min_msgs", 30),
    ({"minMsgs": -5}, "min_msgs", 30),
    ({"lookbackDays": None}, "lookback_days", 14),
    ({"refreshHours": "x"}, "refresh_hours", 22),
    ({"maxPerRun": 0}, "max_per_run", 3),
    ({"maxPromptChars": True}, "max_prompt_chars", 400),
    ({"sampleMsgs": float("nan")}, "sample_msgs", 40),
    ({"timeoutMs": float("inf")}, "timeout_ms", 20000),
    ({"maxRaise": "高"}, "max_raise", 0.1),
])
def test_resolve_cfg_dirty_values_fall_back(dirty, field, default):
    """脏配置**回落缺省**，不许 ``NaN`` 流下去（模块 docstring 分歧 3）。

    ``NaN`` 在这些字段上的表现是「画像永不重生成」和「阈值 NaN ⇒ 零发言」，都不报错。
    """
    assert getattr(P.resolve_hf_profile_cfg({"profile": dirty}), field) == default


def test_resolve_cfg_zero_max_raise_is_legal():
    """``maxRaise: 0`` 是**合法值**（= 不许画像抬阈值），不是脏值 —— 别一起回落成 0.1。"""
    assert P.resolve_hf_profile_cfg({"profile": {"maxRaise": 0}}).max_raise == 0
    assert P.resolve_hf_profile_cfg({"profile": {"maxRaise": -1}}).max_raise == 0.1


@pytest.mark.parametrize("dirty", ["nonsense", ["a"], 3, None])
def test_resolve_cfg_tolerates_dirty_shape(dirty):
    """整段 ``profile`` 形状脏（字符串/列表/数字）⇒ 全部走缺省，不抛 ``AttributeError``。

    TS 靠属性访问拿到 ``undefined`` 再走缺省；Py 的 ``.get`` 在非 Mapping 上会抛 ——
    那是"一行脏配置让整个 sweep 挂掉"的形态。
    """
    c = P.resolve_hf_profile_cfg({"profile": dirty})
    assert (c.enabled, c.min_msgs, c.max_raise) == (True, 30, 0.1)


# ===== 2. 生成 prompt =====


def test_prompt_carries_stats_and_samples():
    p = P.build_hf_profile_prompt(msgs=STATS["total"], days=14, stats=STATS,
                                  samples=["wxid_a**: 在吗", "wxid_b**: 多少钱"])
    assert "200" in p, "必须给出消息总数"
    assert "活跃天数: 12" in p
    assert "9时:40" in p, "小时直方图（0 的那几小时不列）"
    assert "0时:0" not in p, "零值小时不该列（直方图长 24 格，列出来就没法看）"
    assert "wxid_a**: 在吗" in p, "必须带样本消息"
    assert '"band"' in p and '"quiet_hours"' in p, "必须要求 band / quiet_hours 字段"
    assert "只输出 JSON" in p, "必须明确「只输出 JSON」（judge 同范式，靠容忍式解析兜底）"
    assert "40条, 30条" in p, "发言最多的成员按条数列出"
    assert "text:180" in p, "类型分布"


def test_prompt_has_the_two_hard_constraints():
    """两条硬约束是**源头**那一半（另一半是 :func:`.profile.strip_hf_mute_phrases`）。

    只靠过滤 = 白丢一条本来有用的信息；只靠措辞 = 一次模型抽风就重新自锁（09-26 就是这么来的）。
    """
    p = P.build_hf_profile_prompt(msgs=1, days=1, stats=STATS, samples=["a: b"])
    assert "绝不要" in p and "不该插话" in p, "硬约束 1：角色定位只讲口吻，不讲要不要说话"
    assert "最多 +0.10" in p and "0.85" in p, "硬约束 2：band 只能小幅上调，并点名反例"
    assert "比画像为空更糟" in p, "要说清写错的后果（读者是模型）"


def test_prompt_never_carries_group_id():
    """隐私：生成 prompt 不需要群 ID（样本已足够）。

    函数签名里没有群 ID，正文里也不许出现群 ID 形态的串 —— 少一处个人/商户数据外发面。
    """
    p = P.build_hf_profile_prompt(msgs=1, days=1, stats=STATS, samples=["a: b"])
    assert "@chatroom" not in p
    decl = PROFILE_SRC[PROFILE_SRC.index("def build_hf_profile_prompt("):]
    sig = decl[:decl.index(") -> str:")]
    assert "group" not in sig.lower(), f"签名里不得有群 ID 参数: {sig}"


# ===== 3. 解析与校验（安全边界）=====


def test_parse_happy_path_keeps_every_field():
    p = P.parse_hf_profile_response(GOOD)
    assert p.nature == "手机零售商的客户售后群"
    assert p.bot_role == "答疑的客服"
    assert p.engage == ("售后", "价格", "新品")
    assert p.active_hours == (9, 10, 14, 20)
    assert p.quiet_hours == ((23, 7),)
    assert p.band == 0.72
    assert p.budget == {"minGapSec": 300, "maxPerHour": 6, "maxPerDay": 40}
    assert p.summary == "群里问售后就答, 别抢话", "summary 必须被解析（否则等于问了模型又丢掉）"


def test_parse_tolerates_fences_and_rejects_garbage():
    assert P.parse_hf_profile_response("好的, 这是结果:\n```json\n" + GOOD + "\n```\n以上。")
    assert P.parse_hf_profile_response("完全不是 JSON") is None
    assert P.parse_hf_profile_response("") is None
    assert P.parse_hf_profile_response("{坏掉的") is None
    assert P.parse_hf_profile_response("[1,2,3]") is None, "数组不是画像"
    assert P.parse_hf_profile_response('"字符串"') is None
    assert P.parse_hf_profile_response(None) is None


def test_parse_empty_object_is_none():
    """**绝不写空画像**：空对象 / 全空字段 ⇒ ``None``（调用方据此保留上一版）。

    空画像比没有画像更坏 —— prompt 里出现「本群画像: (空)」会引导 judge 乱猜。
    """
    assert P.parse_hf_profile_response("{}") is None
    assert P.parse_hf_profile_response('{"nature":"","engage":[],"avoid":[]}') is None
    assert P.parse_hf_profile_response('{"band":null,"budget":null}') is None
    assert P.parse_hf_profile_response('{"summary":"这是个售后答疑群, 语气随意点"}'), \
        "只有 summary 也算有内容（模型对全群的归纳，单看有价值）"


def test_parse_band_bad_values_and_clamping():
    """band 非法值丢弃；越界值**被抬进 band 区间**（画像不许把阈值压低到硬地板以下）。"""

    def band(v):
        return P.parse_hf_profile_response(
            json.dumps({"nature": "x", "band": v})).band

    assert band(1.5) is None, ">1 丢弃"
    assert band(-0.2) is None, "<=0 丢弃"
    assert band("高") is None, "非数字丢弃"
    assert band(None) is None
    assert band(True) is None, "bool 不算数（TS 的 Number(true)===1 会把 true 当 1，见分歧 4）"
    assert band(0.2) == P._BAND_FALLBACK[0], "画像给的 0.2 必须被抬到 bandMin（否则等于绕开硬地板）"
    assert band(0.95) == P._BAND_FALLBACK[1], "超过 bandMax 被压回上限"
    assert band(0.7) == 0.7, "区间内原样保留"


def test_parse_band_explicit_limits_win():
    """调用方显式给区间时以调用方为准（账号级 bandMin/bandMax 覆盖）。"""
    p = P.parse_hf_profile_response(json.dumps({"nature": "x", "band": 0.2}),
                                    band_min=0.3, band_max=0.9)
    assert p.band == 0.3
    q = P.parse_hf_profile_response(json.dumps({"nature": "x", "band": 0.95}),
                                    band_min=0.6, band_max=0.8)
    assert q.band == 0.8


def test_parse_list_limits_dedupe_hour_filter_quiet_ranges():
    p = P.parse_hf_profile_response(json.dumps({
        "nature": "x",
        "engage": ["a", "b", "c", "d", "e", "f", "g"],
        "avoid": ["a", "a", "b"],
        "active_hours": [9, 9, 99, -1, 10, 11, 12, 13, 14, 15, 16],
        "quiet_hours": [[23, 7], [5, 5], [30, 2], [8, 12], [1, 2], [3, 4], [6, 7]],
    }))
    assert len(p.engage) == 5, "宜聊最多 5 条"
    assert p.avoid == ("a", "b"), "去重"
    assert p.active_hours == (9, 10, 11, 12, 13, 14), \
        "越界/重复小时过滤 + 上限 6 个（11 个有效值只留前 6）"
    assert p.quiet_hours == ((23, 7), (8, 12), (1, 2)), "start==end 丢弃 + 越界丢弃 + 最多 3 段"
    assert all(len(s) <= P.LIM["listItem"] for s in p.engage), "单条限长"


def test_parse_field_length_limits():
    """字段限长防 prompt 膨胀（长文本由 LLM 越写越长的本性驱动）。"""
    p = P.parse_hf_profile_response(json.dumps({
        "nature": "长" * 200, "style": "风" * 200, "bot_role": "角" * 200,
        "summary": "调" * 500,
    }))
    assert len(p.nature) == P.LIM["nature"]
    assert len(p.style) == P.LIM["style"]
    assert len(p.bot_role) == P.LIM["botRole"]
    assert len(p.summary) == P.LIM["summary"]


def test_parse_collapses_whitespace_and_null_literal():
    """空白折叠 + 字面量 ``"null"`` 视同空（模型偶尔会把 null 写成字符串）。"""
    p = P.parse_hf_profile_response(json.dumps({"nature": "多  空\n白", "style": "null"}))
    assert p.nature == "多 空 白"
    assert p.style == ""


def test_parse_round_trips_stored_json():
    """**Py 侧分歧 1**：存库的是 camelCase，解析读的是 snake_case —— TS 在这一步**静默丢四块**。

    TS 的 ``parseHfGroupProfileRow`` 拿 ``profile_json`` 喂回 ``parseHfProfileResponse``，
    而后者读 ``bot_role``/``active_hours``/``quiet_hours``/``min_gap_sec`` ⇒ 预热之后画像
    只剩半张脸，且**画像建议的预算整块失效**（要等下一次重生成才回来）。Py 侧两种拼写都读，
    所以「存进去 → 读回来」必须是**恒等**的。
    """
    p = P.parse_hf_profile_response(GOOD)
    again = P.parse_hf_profile_response(json.dumps(p.as_dict(), ensure_ascii=False))
    assert again == p, "as_dict → parse 必须无损（TS 在此丢 botRole/activeHours/quietHours/budget）"
    assert again.bot_role and again.active_hours and again.quiet_hours and again.budget


def test_parse_row_bad_json_is_none():
    assert P.parse_hf_group_profile_row({"profile_json": "{坏"}) is None
    assert P.parse_hf_group_profile_row({"profile_json": ""}) is None
    assert P.parse_hf_group_profile_row({}) is None
    assert P.parse_hf_group_profile_row({"profile_json": GOOD}) is not None


# ===== 4. 预算收紧（画像 ∩ 配置）=====


def test_clamp_budget_only_tightens():
    cfg = B.resolve_budget(None)                    # 中等档：180 / 8 / 60
    c = P.clamp_hf_profile_budget(
        {"min_gap_sec": 60, "max_per_hour": 20, "max_per_day": 10}, cfg)
    assert c["minGapSec"] == cfg.min_gap_sec, "画像想把间隔缩到 60s ⇒ 无效（间隔取更严的）"
    assert c["maxPerHour"] == cfg.max_per_hour, "画像想放宽到 20/小时 ⇒ 无效"
    assert c["maxPerDay"] == 10, "画像收紧到 10/天 ⇒ 生效"

    tighter = P.clamp_hf_profile_budget({"min_gap_sec": 600}, cfg)
    assert tighter["minGapSec"] == 600, "画像要求更长间隔 ⇒ 生效"
    assert "maxPerHour" not in tighter, "未给的字段不填（避免覆盖配置）"
    assert P.clamp_hf_profile_budget({}, cfg) is None, "无有效字段 ⇒ None"
    assert P.clamp_hf_profile_budget({"max_per_hour": -3}, cfg) is None, "负值不当真"
    assert P.clamp_hf_profile_budget({"max_per_hour": 0}, cfg) is None, "0 不当真（0 是「不限」的哨兵）"


def test_clamp_budget_without_cfg_keeps_suggestion():
    """没有配置基准时不取交集（返回值仍只是"建议"，复合在 ``budget.tighten_budget``）。"""
    assert P.clamp_hf_profile_budget({"min_gap_sec": 60}) == {"minGapSec": 60}
    assert P.clamp_hf_profile_budget({"max_per_hour": 3}) == {"maxPerHour": 3}


def test_clamped_budget_composes_and_never_loosens():
    """与配置复合后**只能更严**（I2 的落点是复合那一步，不是在建议那一步）。"""
    base = B.resolve_budget(None)
    sugg = P.clamp_hf_profile_budget(
        {"min_gap_sec": 1, "max_per_hour": 99, "max_per_day": 1}, base)
    out = B.tighten_budget(base, sugg)
    assert out.min_gap_sec >= base.min_gap_sec
    assert out.max_per_hour <= base.max_per_hour
    assert out.max_per_day == 1


def test_clamp_budget_accepts_camel_case_suggestion():
    """库里的画像建议是 camelCase（``as_dict`` 存的）—— 必须能直接再喂进来。"""
    assert P.clamp_hf_profile_budget({"minGapSec": 600}) == {"minGapSec": 600}
    assert P.clamp_hf_profile_budget({"maxPerDay": 10}) == {"maxPerDay": 10}


def test_quiet_hours_never_in_profile_budget_suggestion():
    """画像的**静默段**不走预算建议这条口子。

    静默段是"彻底不说话"的开关，默认关、要开须显式 ``applyQuietHours``；它只在
    ``budget.tighten_budget(..., apply_quiet_hours=...)`` 那一侧并入。画像建议里混进
    ``quietHours`` 会被 ``tighten_budget`` 忽略（不放行）—— 本用例把这条口径钉住。
    """
    cfg = B.resolve_budget(None)
    sugg = P.clamp_hf_profile_budget({"min_gap_sec": 600}, cfg)
    merged = B.tighten_budget(cfg, {"quietHours": [[1, 5]]})
    assert merged.quiet_hours == (), "默认（applyQuietHours 未开）时画像的静默段不得生效"
    assert B.tighten_budget(cfg, {"quietHours": [[1, 5]]},
                            apply_quiet_hours=True).quiet_hours == [[1, 5]], "开了才生效"
    assert "quietHours" not in sugg, "画像预算建议里不该带静默段（走显式开关那条路）"


# ===== 5. 禁言令过滤（v1.10.0 事故的主根因）=====


MUTE_SAMPLES = [
    "基本不该插话的旁观者",
    "不要插话，少说话",
    "群里的围观者",
    "不主动搭话",
    "不闲聊",
    "保持沉默即可",
    "禁言模式",
]


@pytest.mark.parametrize("s", MUTE_SAMPLES)
def test_strip_mute_phrases_kills_every_pattern(s):
    assert P.strip_hf_mute_phrases(s) == "", f"必须被拦下: {s!r}"
    assert len(P.HF_MUTE_PATTERNS) == 6, "模式表是六条（少一条就是一个漏网的否决票）"


@pytest.mark.parametrize("s", [
    "懂手机业务的同事, 说话短、直给结论, 不客套",
    "答疑的客服",
    "偶尔搭话的熟人",
])
def test_strip_mute_phrases_keeps_good_roles(s):
    assert P.strip_hf_mute_phrases(s) == s, "只说「以什么身份、什么口吻」的必须原样保留"


def test_strip_mute_phrases_empty_and_dirty():
    assert P.strip_hf_mute_phrases("") == ""
    assert P.strip_hf_mute_phrases("   ") == ""
    assert P.strip_hf_mute_phrases(None) == ""


def test_guard_filters_only_role_and_summary_and_keeps_original():
    """只过滤 ``bot_role`` / ``summary``：``engage`` / ``avoid`` 是**话题清单**，不扫。

    （"避开政治" 这类"忌接话题"是资产；"少说话" 这类"要不要说话"的元指令才是负债。）
    """
    p = P.parse_hf_profile_response(GOOD)
    dirty = P.replace(p, bot_role="基本不该插话的旁观者", summary="别抢话, 保持沉默")
    g = P.guard_hf_profile_for_prompt(dirty)
    assert g.bot_role == "" and g.summary == ""
    assert dirty.bot_role == "基本不该插话的旁观者", "原对象不许被改（/heartflow profile 要看真相）"
    assert g.nature == p.nature and g.engage == p.engage and g.avoid == p.avoid
    assert g.band == p.band and g.budget == p.budget


def test_mute_phrases_in_topic_lists_are_not_stripped():
    p = P.parse_hf_profile_response(json.dumps({
        "nature": "x", "engage": ["不聊政治"], "avoid": ["少说话的话题"],
    }))
    g = P.guard_hf_profile_for_prompt(p)
    assert g.engage == ("不聊政治",) and g.avoid == ("少说话的话题",)


# ===== 6. band 生效（有界抬升）=====


def test_band_effect_raises_with_cap():
    """只抬不降 + 抬升有上限 —— 09-26 自锁的点火器就是"无上界的硬地板"。"""
    t, capped = P.resolve_hf_profile_band_effect(0.6, 0.9)
    assert t == pytest.approx(0.7), "0.9 只能抬到 0.6+0.10（maxRaise）"
    assert capped is True
    t, capped = P.resolve_hf_profile_band_effect(0.6, 0.62)
    assert t == pytest.approx(0.62) and capped is False
    t, capped = P.resolve_hf_profile_band_effect(0.6, 0.6)
    assert t == pytest.approx(0.6) and capped is False


def test_band_effect_never_lowers():
    """画像无权放宽约束：建议低于锚点 ⇒ 不动。"""
    t, capped = P.resolve_hf_profile_band_effect(0.68, 0.55)
    assert t == pytest.approx(0.68) and capped is False
    t, _ = P.resolve_hf_profile_band_effect(0.68, None)
    assert t == pytest.approx(0.68), "无建议 ⇒ 锚点原样"
    t, _ = P.resolve_hf_profile_band_effect(0.68, "高")
    assert t == pytest.approx(0.68)


def test_band_effect_respects_hard_band():
    """再与硬区间取交 —— 与 ``evalHfThreshold`` / ``clampHfThresholdToBand`` 同一道保证。"""
    assert P.resolve_hf_profile_band_effect(0.6, 0.9, band_max=0.65)[0] == pytest.approx(0.65)
    assert P.resolve_hf_profile_band_effect(0.4, 0.9, band_min=0.5, band_max=0.9)[0] == pytest.approx(0.5)
    assert P.resolve_hf_profile_band_effect(2.0, None)[0] == pytest.approx(P._BAND_FALLBACK[1]), \
        "锚点越界也要被钳回区间"


def test_band_effect_dirty_inputs_never_produce_nan():
    """**分歧 3**：``maxRaise`` 脏值在 TS 会算出 ``threshold = NaN`` ⇒ 所有比较为假 ⇒ **零发言**。"""
    t, _ = P.resolve_hf_profile_band_effect(0.6, 0.9, max_raise=float("nan"))
    assert t == pytest.approx(0.7), "脏 maxRaise 回落 0.10（不是 NaN）"
    t, _ = P.resolve_hf_profile_band_effect(0.6, 0.9, max_raise=-1)
    assert t == pytest.approx(0.7), \
        "负数与其它脏值同例：回落 0.10（要表达「不许抬」的合法拼写是 0，不是 -1 —— " \
        "见 test_resolve_cfg_zero_max_raise_is_legal；单处另立一套「负数=0」的口径， " \
        "只会让配置面与函数面给出两个不同的阈值）"
    t, _ = P.resolve_hf_profile_band_effect(float("nan"), 0.7)
    assert t == pytest.approx(0.7), "锚点脏 ⇒ 用 0.6 起算（不是 NaN）"


def test_band_effect_max_raise_zero():
    t, capped = P.resolve_hf_profile_band_effect(0.6, 0.9, max_raise=0)
    assert t == pytest.approx(0.6) and capped is True


# ===== 7. 渲染 / 推导 / 新旧判定 =====


def test_render_truncates_whole_lines():
    p = P.parse_hf_profile_response(GOOD)
    full = P.render_hf_profile_for_prompt(p, 400)
    assert len(full) <= 400
    assert "群性质: " in full and "我的角色定位: " in full
    assert "说话基调: " in full, "summary 也要渲染（问了模型就得用上）"
    assert full.count("\n") == 6, "七行都有（cap 够）"

    tiny = P.render_hf_profile_for_prompt(p, 20)
    assert len(tiny) <= 20
    assert "忌接话题" not in tiny, "放不下的整行丢弃，不切半句"
    assert "说话基调" not in tiny, "summary 排最后 ⇒ 紧张时最先被丢（它是硬信息的重述）"
    assert tiny == "群性质: 手机零售商的客户售后群"


def test_render_empty_profile_is_empty_string():
    assert P.render_hf_profile_for_prompt(P.HfGroupProfile(), 400) == "", "无内容 ⇒ 空串（调用方据此不注入）"
    assert P.render_hf_profile_for_prompt(P.HfGroupProfile(nature="x"), 0) == ""
    assert P.render_hf_profile_for_prompt(P.HfGroupProfile(nature="x"), -5) == ""
    assert P.render_hf_profile_for_prompt(P.HfGroupProfile(nature="x"), None) == ""


def test_derive_active_hours_is_objective():
    """按均值取活跃小时（客观口径，不靠 LLM）—— 至少一条消息才谈得上活跃。"""
    assert P.derive_active_hours(STATS["hourHist"]) == [9, 10, 14, 20]
    assert P.derive_active_hours([0] * 24) == [], "全 0 ⇒ 无活跃时段"
    assert len(P.derive_active_hours([5] * 24)) == 24, "均匀分布 = 每小时都算活跃"
    assert P.derive_active_hours([0] * 23 + [30], min_msgs=40) == [], "样本不足门槛"
    assert P.derive_active_hours([1] * 24, min_msgs=24) == list(range(24))


def test_is_stale_boundaries():
    now = 1_800_000_000
    assert P.is_hf_profile_stale(None, now, 22) is True
    assert P.is_hf_profile_stale(0, now, 22) is True
    assert P.is_hf_profile_stale(now - 21 * 3600, now, 22) is False, "21h < 22h ⇒ 还新"
    assert P.is_hf_profile_stale(now - 22 * 3600, now, 22) is True, "恰好 22h ⇒ 该重生"
    assert P.is_hf_profile_stale(now - 3 * 86400, now, 22) is True


def test_is_stale_dirty_refresh_hours_does_not_freeze():
    """**分歧 3**：TS 的 ``Math.max(1, NaN) = NaN`` ⇒ ``>=`` 恒假 ⇒ **画像永不重生成**（不报错）。"""
    now = 1_800_000_000
    old = now - 30 * 86400
    assert P.is_hf_profile_stale(old, now, "abc") is True, "脏值回落 22h ⇒ 超期就是超期"
    assert P.is_hf_profile_stale(old, now, 0) is True
    assert P.is_hf_profile_stale(old, now, -3) is True
    assert P.is_hf_profile_stale(old, now, 0.5) is True, "0.5h 也夹到 1h（TS 同：Math.max(1, x)）"


# ===== 8. 缓存（judge 热路径零 DB 读）=====


def test_cache_warm_then_read_and_replace():
    rows = [prof_row("g1@chatroom"), prof_row("g2@chatroom", profile_json="{坏", version=1)]
    n = P.load_hf_profiles_into_cache("acc1", rows, 400)
    assert n == 1, "坏行被跳过（不会以「空画像」进缓存）"
    assert P.hf_profile_cache_size() == 1
    text = P.get_hf_profile_prompt_text("acc1", "g1@chatroom")
    assert text and len(text) <= 400
    assert P.get_hf_profile_band_floor("acc1", "g1@chatroom") == 0.72
    assert P.get_hf_profile_budget("acc1", "g1@chatroom")["minGapSec"] == 300, \
        "预热后预算仍在（TS 在这一步丢 budget，见分歧 1）"
    assert P.get_hf_profile_prompt_text("acc2", "g1@chatroom") is None, "别的账号取不到"
    assert P.get_hf_profile_prompt_text("acc1", "other@chatroom") is None

    n2 = P.load_hf_profiles_into_cache("acc1", [prof_row("g9@chatroom")], 400)
    assert n2 == 1
    assert P.get_hf_profile_prompt_text("acc1", "g1@chatroom") is None, "旧群必须被清掉（否则吃过期画像）"
    P.reset_hf_profile_cache()
    assert P.hf_profile_cache_size() == 0


def test_cache_keeps_quiet_hours_for_the_explicit_switch():
    """★ 主路径：静默段必须进缓存。

    2026-10-18 实测 bug：缓存只存了 ``budget``，而 ``clamp_hf_profile_budget`` 按口径
    **不含** ``quietHours`` ⇒ ``applyQuietHours=true`` 时生效值永远空（开关是摆设）。
    这里钉住原始静默段可被显式开关那条路取到，同时预算建议的口径不变。
    """
    P.load_hf_profiles_into_cache("acc1", [prof_row("g1@chatroom")], 400)
    assert P.get_hf_profile_quiet_hours("acc1", "g1@chatroom") == ((23, 7),)
    assert "quietHours" not in (P.get_hf_profile_budget("acc1", "g1@chatroom") or {}), \
        "预算建议口径不变 —— 静默段只走显式开关"
    assert P.get_hf_profile_quiet_hours("acc1", "nope@chatroom") == ()


def test_cache_truncates_at_warm_time():
    P.load_hf_profiles_into_cache("acc", [prof_row("g")], 30)
    text = P.get_hf_profile_prompt_text("acc", "g")
    assert text and len(text) <= 30, "缓存里的文本必须已截断（注入上限在缓存层就生效）"


def test_cache_warm_strips_mute_phrases_and_logs(caplog):
    """预热时过滤 —— 库里**已有**的坏画像（09-26 那批）立刻失效，不必等下一次重生成。

    原始画像仍留在库里供 ``/heartflow profile`` 查看（只有进 prompt 的文本被过滤）。
    """
    bad = json.dumps({"nature": "x", "bot_role": "基本不该插话的旁观者",
                      "engage": ["售后"], "summary": "不要插话, 少说话"}, ensure_ascii=False)
    with caplog.at_level("WARNING"):
        P.load_hf_profiles_into_cache("acc", [prof_row("g", bad)], 400)
    text = P.get_hf_profile_prompt_text("acc", "g")
    assert "旁观者" not in text, "禁言令不得进缓存文本"
    assert "不要插话" not in text
    assert "群性质: x" in text and "宜接话题: 售后" in text, "话题性内容是资产，必须留"
    assert "mute-phrase stripped" in caplog.text, "被过滤要留痕（否则没人知道画像被削了）"
    assert "field=bot_role" in caplog.text and "field=summary" in caplog.text


def test_mute_pattern_table_has_known_gaps():
    """**把过滤器的边界写成可见的东西** —— 免得后人以为"禁言措辞已被全覆盖"。

    表与 TS ``HF_MUTE_PATTERNS`` 逐条一致（6 条，故意不对齐就是新增分歧，得先有证据）。
    它是一张**否定式黑名单**，所以"没命中"只等于"这批措辞没命中"，不等于"这句话不是禁言令"：

    - ``别抢话`` / ``抢话`` / ``别插嘴`` 都不在表里 —— 语义上同属"要不要说话"的元指令；
    - 任意新措辞（"多看着就行"、"以听为主"…）都能绕过去。

    为什么**不顺手加几条**：每条模式都是一个**误伤面**（命中即整个字段被丢弃 ⇒ 画像白生成），
    而 09-26 那次事故里的真实措辞（旁观者/不该插话）已经在表里了。要加，得先有一条**实际
    漏网的生产措辞**作为证据（与本次重写的分歧纪律一致：只修有实证的 bug）。
    """
    for loose in ("别抢话", "抢话", "别插嘴", "多看着就行"):
        assert P.strip_hf_mute_phrases(loose) == loose, (
            f"{loose!r} 现在被拦住了 —— 好消息，但请把这条从本测试挪进 "
            "test_strip_mute_phrases_kills_every_pattern 并同步 HF_MUTE_PATTERNS 的条数"
        )
    assert len(P.HF_MUTE_PATTERNS) == 6


def test_startup_preload_reads_rows_once_into_cache(monkeypatch):
    """启动预热（2026-10-18）：只读画像行灌缓存，不生成、不查第二次。"""
    calls: list[str] = []

    def fake_list(account_id):
        calls.append(account_id)
        return [prof_row("g1@chatroom"), prof_row("g2@chatroom", profile_json="{坏")]

    monkeypatch.setattr(repo_hf, "list_profiles", fake_list)
    P.reset_hf_profile_cache()
    assert P.load_hf_profiles("acc1") == 1, "坏行跳过（与 sweep 预热同一套解析）"
    assert calls == ["acc1"]
    assert P.get_hf_profile_prompt_text("acc1", "g1@chatroom")
    assert P.get_hf_profile_prompt_text("acc1", "g2@chatroom") is None
    P.reset_hf_profile_cache()


def test_reply_side_prompt_wraps_the_same_text_without_changing_it():
    """回复侧注入 = 前缀 + 画像正文；空画像 ⇒ None（= 与没开功能时逐字节相同）。"""
    assert P.build_hf_profile_channel_prompt(None) is None
    assert P.build_hf_profile_channel_prompt("  \n ") is None
    out = P.build_hf_profile_channel_prompt("群性质: 手机店\n说话基调: 简短")
    assert out.startswith("[系统提示-本群画像]")
    assert out.endswith("群性质: 手机店\n说话基调: 简短")
    assert "不要向群友复述" in out, "必须明确它不是用户消息（channel_prompt 会进 system prompt）"


def test_cache_rows_without_group_id_are_skipped():
    rows = [dict(prof_row("g"), group_id=""), dict(prof_row("g2"), group_id=None)]
    assert P.load_hf_profiles_into_cache("acc", rows, 400) == 0
    assert P.hf_profile_cache_size() == 0


def test_resolve_band_effect_for_reads_cache_without_io():
    """读取侧唯一入口：有界抬升 + 硬区间钳制，**零 DB IO**。"""
    P.load_hf_profiles_into_cache("acc", [prof_row("g")], 400)
    r = P.resolve_hf_profile_band_effect_for("acc", "g", 0.6)
    assert r["band"] == 0.72, "画像原始建议要能看见（诊断用）"
    assert r["threshold"] == pytest.approx(0.7) and r["capped"] is True
    r2 = P.resolve_hf_profile_band_effect_for("acc", "nope", 0.66)
    assert r2 == {"threshold": pytest.approx(0.66), "capped": False, "band": None}


# ===== 9. 生成编排（假数据层 + 假 judge，真跑）=====


class FakeIo:
    """假数据层 + 假 judge：**只替换 IO**，编排逻辑真跑。

    row 形状与真 ``SELECT`` 同（§10 有断言钉住），否则"假替换件一起漂移"会全绿。
    """

    def __init__(self, *, profiles=(), groups=("g1@chatroom",), stats=None, samples=(),
                 text=GOOD, fail_call=(), fail_stats=(), fail_list=False, buckets=None):
        self.profiles = list(profiles)
        self.groups = list(groups)
        self.stats = dict(STATS if stats is None else stats)
        self.samples = list(samples)
        self.text = text
        self.fail_call = set(fail_call)
        self.fail_stats = set(fail_stats)
        self.fail_list = fail_list
        self.buckets = None if buckets is None else list(buckets)
        self.calls: list[dict] = []
        self.writes: list[dict] = []
        self.bucket_args: list[tuple] = []

    # ---- 数据层
    def list_profiles(self, account_id, conn=None):
        if self.fail_list:
            raise RuntimeError("DB 炸了")
        return [dict(r) for r in self.profiles]

    def upsert_profile(self, account_id, group_id, profile, *, stats=None,
                       sample_msgs=0, model=None, generated_at=None, conn=None):
        self.writes.append(dict(account_id=account_id, group_id=group_id,
                                profile=profile, stats=stats,
                                sample_msgs=sample_msgs, model=model,
                                generated_at=generated_at))
        return 1

    def get_hf_group_message_stats(self, account_id, group_id, since_sec, conn=None):
        if group_id in self.fail_stats:
            raise RuntimeError("统计炸了")
        return dict(self.stats)

    def list_group_msg_hour_buckets(self, account_id, since_sec, local_offset_sec, conn=None):
        self.bucket_args.append((account_id, since_sec, local_offset_sec))
        if self.buckets is not None:
            return [dict(b) for b in self.buckets]
        return [{"group_id": g, "h": 9, "n": 5} for g in self.groups]

    def list_messages_by_peer(self, account_id, peer_id, limit=20, conn=None):
        return [dict(m) for m in self.samples]

    # ---- LLM
    async def call_judge(self, **kw):
        self.calls.append(kw)
        if len(self.calls) in self.fail_call:
            raise RuntimeError("LLM 炸了")
        return self.text

    def resolve_judge_creds(self):
        return None

    # ---- 装/卸
    def install(self, monkeypatch):
        from plugin.llm import judge as judge_mod
        monkeypatch.setattr(repo_hf, "list_profiles", self.list_profiles)
        monkeypatch.setattr(repo_hf, "upsert_profile", self.upsert_profile)
        monkeypatch.setattr(repo_messages, "get_hf_group_message_stats",
                            self.get_hf_group_message_stats)
        monkeypatch.setattr(repo_messages, "list_group_msg_hour_buckets",
                            self.list_group_msg_hour_buckets)
        monkeypatch.setattr(repo_messages, "list_messages_by_peer",
                            self.list_messages_by_peer)
        monkeypatch.setattr(judge_mod, "call_judge", self.call_judge)
        monkeypatch.setattr(judge_mod, "resolve_judge_creds", self.resolve_judge_creds)
        return self


def gen(io: FakeIo, cfg=None, *, now: int = NOW, group_filter=None):
    return asyncio.run(P.maybe_generate_hf_group_profiles(
        "acc1", cfg if cfg is not None else {"model": "m1"}, now,
        group_filter=group_filter))


def test_generate_writes_row_and_warms_cache(monkeypatch):
    io = FakeIo(samples=[msg("inbound", "在吗, 这款多少钱", NOW - 60),
                         msg("outbound", "bot 自己说的话", NOW - 30)]).install(monkeypatch)
    r = gen(io)
    assert (r.generated, r.skipped) == (1, 0)
    assert len(io.calls) == 1 and len(io.writes) == 1
    w = io.writes[0]
    assert w["group_id"] == "g1@chatroom" and w["model"] == "m1" and w["generated_at"] == NOW
    assert w["profile"]["botRole"] == "答疑的客服", "落库的是 camelCase（与 TS 同形）"
    assert w["sample_msgs"] == 1, "样本数 = 真正喂进去的条数（出站被排除）"
    assert w["stats"]["derivedActiveHours"] == [9, 10, 14, 20], "落库带客观推导的活跃时段"
    assert w["stats"]["topSenderCount"] == 2
    assert w["stats"]["total"] == 200
    assert P.hf_profile_cache_size() == 1 and P.get_hf_profile_band_floor("acc1", "g1@chatroom") == 0.72
    # judge 入参：单独给的 maxTokens / timeout（绝不复用 judge 的 300）+ 只回 JSON 的系统提示
    call = io.calls[0]
    assert call["max_tokens"] == P.HF_PROFILE_DEFAULTS["maxTokens"] and call["max_tokens"] != 300
    assert call["timeout_ms"] == P.HF_PROFILE_DEFAULTS["timeoutMs"]
    assert "只输出 JSON" in call["system_prompt"]


def test_generate_window_and_local_offset(monkeypatch):
    io = FakeIo().install(monkeypatch)
    gen(io)
    acct, since, offset = io.bucket_args[0]
    assert acct == "acc1"
    assert since == NOW - P.HF_PROFILE_DEFAULTS["lookbackDays"] * 86400, "活跃群窗口 = lookbackDays"
    assert offset == P._local_offset_sec(NOW), "本地偏移必须传（否则小时分桶按 UTC 算）"


def test_generate_warms_cache_even_when_disabled(monkeypatch):
    """关掉生成仍要预热：judge 每次都要用画像，老板也可能"关生成、用旧画像"。"""
    io = FakeIo(profiles=[prof_row("g1@chatroom")]).install(monkeypatch)
    r = gen(io, {"model": "m1", "profile": {"enabled": False}})
    assert (r.generated, r.skipped) == (0, 0)
    assert io.calls == [], "关掉生成不得调 LLM"
    assert P.get_hf_profile_prompt_text("acc1", "g1@chatroom"), "但缓存必须已预热"


def test_generate_skips_without_model(monkeypatch):
    io = FakeIo(profiles=[prof_row("g1@chatroom")]).install(monkeypatch)
    r = gen(io, {})
    assert (r.generated, r.skipped) == (0, 0) and io.calls == []
    assert P.get_hf_profile_prompt_text("acc1", "g1@chatroom"), "缓存照常预热"


def test_generate_warm_failure_does_not_raise(monkeypatch):
    """预热失败 ⇒ judge 无画像可用，但**账号照常跑**（warn 一条就够）。"""
    io = FakeIo(fail_list=True).install(monkeypatch)
    r = gen(io)
    assert (r.generated, r.skipped) == (0, 0)
    assert io.calls == [], "预热都没成功，不要继续打 LLM"


def test_generate_skips_fresh_and_respects_max_per_run(monkeypatch):
    """无画像 / 无生成时刻 / 超期才重生；单轮配额防"20 个群同时到期打 20 次 LLM"。"""
    fresh = prof_row("g1@chatroom", generated_at=NOW - 21 * 3600)      # 21h < 22h ⇒ 还新
    io = FakeIo(profiles=[fresh], groups=("g1@chatroom", "g2@chatroom", "g3@chatroom"),
                stats=STATS).install(monkeypatch)
    r = gen(io)
    assert (r.generated, r.skipped) == (2, 1), "g1 还新（21h<22h）⇒ skipped，g2/g3 都生成"
    assert len(io.calls) == 2

    io2 = FakeIo(groups=("g1@chatroom", "g2@chatroom", "g3@chatroom")).install(monkeypatch)
    r2 = gen(io2, {"model": "m1", "profile": {"maxPerRun": 2}})
    assert (r2.generated, r2.skipped) == (2, 1) and len(io2.calls) == 2
    assert [w["group_id"] for w in io2.writes] == ["g1@chatroom", "g2@chatroom"], \
        "配额按活跃群清单的顺序给（谁先到期谁先拿）"


def test_generate_skips_when_sample_too_small(monkeypatch):
    """几条消息的「画像」是噪声 —— 样本门槛挡住它，且**不花 LLM 调用**。"""
    io = FakeIo(stats={**STATS, "total": 29}).install(monkeypatch)
    r = gen(io)
    assert (r.generated, r.skipped) == (0, 1)
    assert io.calls == [] and io.writes == []
    assert P.hf_profile_cache_size() == 0, "样本不足不得留缓存（也不许写空画像）"


def test_generate_parse_failure_keeps_previous(monkeypatch, caplog):
    """解析失败**绝不写库**（空画像比没有画像更坏）—— 缓存里仍是预热进来的那一版。

    ⚠️ 只断言"没写库"是不够的：把 ``if p is None: continue`` 删掉之后，代码会走到
    ``p.as_dict()`` 上抛 ``AttributeError``，被那条"单群失败就地兜底"的 ``except`` 捞回来 ——
    **观测结果完全一样**（没写库、skipped+1），于是变异存活。所以这里必须再钉**日志措辞**：
    "解析失败"与"生成失败"是两条不同的运维线索（前者说模型/解析器坏了，后者说代码崩了）。
    """
    io = FakeIo(profiles=[prof_row("g1@chatroom", generated_at=0)],
                text="完全不是 JSON").install(monkeypatch)
    with caplog.at_level("WARNING"):
        r = gen(io)
    assert (r.generated, r.skipped) == (0, 1)
    assert io.writes == [], "解析失败不得 upsert"
    assert P.get_hf_profile_prompt_text("acc1", "g1@chatroom"), "上一版画像必须还在缓存里"
    assert P.get_hf_profile_band_floor("acc1", "g1@chatroom") == 0.72
    assert "profile 解析失败 (保留上一版)" in caplog.text, "必须是「解析失败」这条分支走的"
    assert "profile 生成失败" not in caplog.text, \
        "落到兜底 except 里说明是崩了（None.as_dict()），不是解析失败 —— 两者的运维含义不同"


def test_generate_single_group_failure_does_not_stop_the_round(monkeypatch):
    """单群失败就地 catch：既不写库、也不影响别的群，更不许影响 sweep。"""
    io = FakeIo(groups=("g1@chatroom", "g2@chatroom"), fail_call=(1,)).install(monkeypatch)
    r = gen(io)
    assert (r.generated, r.skipped) == (1, 1)
    assert [w["group_id"] for w in io.writes] == ["g2@chatroom"]
    assert P.get_hf_profile_prompt_text("acc1", "g2@chatroom")


def test_generate_stats_failure_is_contained(monkeypatch):
    io = FakeIo(groups=("g1@chatroom", "g2@chatroom"), fail_stats=("g1@chatroom",)).install(monkeypatch)
    r = gen(io)
    assert (r.generated, r.skipped) == (1, 1)
    assert len(io.calls) == 1, "统计炸了就不该继续调 LLM"


def test_generate_group_filter_is_applied(monkeypatch):
    """白名单过滤由调用方注入（成环理由见 ``maybe_generate_hf_group_profiles`` 的 docstring）。"""
    io = FakeIo(groups=("g1@chatroom", "g2@chatroom")).install(monkeypatch)
    r = gen(io, group_filter=lambda g: g == "g2@chatroom")
    assert r.generated == 1 and io.writes[0]["group_id"] == "g2@chatroom"
    io2 = FakeIo(groups=("g1@chatroom", "g2@chatroom")).install(monkeypatch)
    assert gen(io2).generated == 2, "group_filter=None ⇒ 不过滤（等价于白名单为空）"


def test_generate_dedupes_group_list_keeping_order(monkeypatch):
    """分桶查询会按群 × 小时出行 ⇒ 去重必须**保序**（顺序决定配额先给谁）。"""
    io = FakeIo(buckets=[{"group_id": "g2@chatroom", "h": 9, "n": 1},
                         {"group_id": "g1@chatroom", "h": 9, "n": 1},
                         {"group_id": "g2@chatroom", "h": 10, "n": 1}]).install(monkeypatch)
    r = gen(io, {"model": "m1", "profile": {"maxPerRun": 1}})
    assert r.generated == 1 and io.writes[0]["group_id"] == "g2@chatroom", "保序 ⇒ 先到先得"


def test_generate_strips_mute_phrases_from_fresh_profile(monkeypatch, caplog):
    """**Py 侧分歧 2**：TS 只在预热时过滤，**生成路径直接入缓存** ⇒ 一次抽风能绕过过滤器。

    绕过窗口最长 ``refreshHours``（22h），而过滤器本来就是为了「源头措辞不可靠」才存在的。
    """
    bad = json.dumps({"nature": "x", "bot_role": "基本不该插话的旁观者",
                      "engage": ["售后"]}, ensure_ascii=False)
    io = FakeIo(text=bad).install(monkeypatch)
    with caplog.at_level("WARNING"):
        assert gen(io).generated == 1
    text = P.get_hf_profile_prompt_text("acc1", "g1@chatroom")
    assert "旁观者" not in text, "刚生成的禁言令也必须被拦（否则 22h 内都在喂 judge）"
    assert "群性质: x" in text
    assert "mute-phrase stripped (新生成)" in caplog.text, "被削要留痕"
    assert io.writes[0]["profile"]["botRole"] == "基本不该插话的旁观者", \
        "库里存**原始**画像（/heartflow profile 要看真相），只有进 prompt 的那份被过滤"


def test_generate_version_is_prev_plus_one(monkeypatch):
    io = FakeIo(profiles=[prof_row("g1@chatroom", generated_at=0, version=4)],
                ).install(monkeypatch)
    assert gen(io).generated == 1
    c = P._profiles[P._key("acc1", "g1@chatroom")]
    assert c["version"] == 5, "重生成 ⇒ version + 1"


def test_sample_lines_take_most_recent_inbound(monkeypatch):
    """样本 = **最近**的 N 条入站：repo 给升序行，SUT 必须反过来取。

    顺着升序取会把**最老的**几条喂进画像 —— 而画像说的是"这个群现在怎么说话"。
    """
    rows = [msg("inbound", f"第{i}条", NOW - 100 + i) for i in range(5)]   # 升序
    rows.append(msg("outbound", "bot 自己说的话", NOW - 50))                # 最新，但是出站
    io = FakeIo(samples=rows).install(monkeypatch)
    out = P._load_sample_lines("acc1", "g1@chatroom", 3)
    assert [o.split(": ", 1)[1] for o in out] == ["第4条", "第3条", "第2条"], \
        "取最近的 3 条（不是最老的 第0/1/2 条）"
    assert all("bot 自己说的话" not in o for o in out), "出站绝不进样本（否则画像自我强化）"


def test_sample_lines_dedupe_is_by_full_line(monkeypatch):
    """同一条文本由**不同人**说出来不算重复（去重键是整行，含发送者）。

    按内容去重会把"三个人都在问同一款手机的价"压成一条 —— 那正是群氛围的证据。
    """
    rows = [msg("inbound", "这款多少钱", NOW - 30, from_wxid="wxid_aaa"),
            msg("inbound", "这款多少钱", NOW - 20, from_wxid="wxid_bbb"),
            msg("inbound", "这款多少钱", NOW - 10, from_wxid="wxid_bbb")]   # 同一人重复
    io = FakeIo(samples=rows).install(monkeypatch)
    out = P._load_sample_lines("acc1", "g1@chatroom", 10)
    assert len(out) == 2, "同人重复 ⇒ 压成一条；不同人同内容 ⇒ 保留"


def test_sample_lines_skips_short_and_dedupes_and_truncates(monkeypatch):
    long_body = "长" * 200
    # ⚠️ 发送者必须用**够长**的 wxid：真实 wxid 20+ 字符，遮 6 位才有意义；
    #    拿 "wxid_a"（正好 6 位）去测，`[:6]` 与 `[:8]` 的输出**一模一样** ⇒ 变异存活。
    who6 = "wxid_abcdefg"
    rows = [msg("inbound", "", NOW - 10, from_wxid=who6),        # 空 ⇒ 丢
            msg("inbound", "短", NOW - 9, from_wxid=who6),        # <2 字 ⇒ 丢
            msg("inbound", long_body, NOW - 8, from_wxid=who6),
            msg("inbound", long_body, NOW - 7, from_wxid=who6)]   # 同人同内容 ⇒ 去重
    io = FakeIo(samples=rows).install(monkeypatch)
    out = P._load_sample_lines("acc1", "g1@chatroom", 10)
    assert len(out) == 1, "空/过短丢弃 + 同内容去重"
    who, body = out[0].split(": ", 1)
    assert who == "wxid_a**", "发送者只留前 6 位 + **（少一处身份外发；后 9 位必须被遮掉）"
    assert "abcdefg" not in who
    assert len(body) == 80, "单条截断 80 字"


def test_sample_lines_limits_are_clamped(monkeypatch):
    io = FakeIo(samples=[msg("inbound", f"x{i}", NOW - i) for i in range(5)]).install(monkeypatch)
    assert len(P._load_sample_lines("acc1", "g", 0)) == 1, "0/负 ⇒ 至少 1 条（不炸）"
    assert len(P._load_sample_lines("acc1", "g", 999)) == 5, "上限 100（防一次拉爆内存）"


# ===== 10. 源级守卫 =====


def test_profile_imports_no_learn_or_heartflow_at_module_level():
    """成环会把加载期异常吞成 WARNING ⇒ 真实表现是**整批工具静默不注册**（F5 的形态）。

    ``learn`` / ``heartflow`` 都要 import 本模块（值导入），所以本模块只能在**函数体里**
    延迟 import 它们（band 区间那一条）。模块级允许的只有 ``.budget`` / ``.runtime`` /
    ``.caption`` —— 前两个无包内依赖，``caption`` 模块级只用 stdlib（它对本包
    ``heartflow`` 的依赖也在函数体里延迟导入，正是为了避开这条环）。
    """
    tree = ast.parse(PROFILE_SRC)
    top: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top.append("." * node.level + (node.module or ""))
    assert top, "切片自检：模块级导入必须被扫到（否则这条断言是假绿）"
    for name in top:
        if name.startswith("."):
            assert name in (".budget", ".runtime", ".caption"), \
                f"模块级只许 .budget/.runtime/.caption，得到 {name}"
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[-1]
            assert mod != "learn" or node not in tree.body, "learn 只能在函数体里延迟导入"
    assert "from . import learn" in PROFILE_SRC, "band 区间仍要取自 learn（唯一真源）"
    assert "from . import learn" in PROFILE_SRC.split("def _band_limits")[1], \
        "那一次 import 必须在 _band_limits 体内"


def test_module_level_imports_have_no_third_party_dependency():
    """加载期只许 stdlib + 本包相对导入（本模块在注册链上，见 F5）。"""
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    tree = ast.parse(PROFILE_SRC)
    top: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top.append("." * node.level + (node.module or ""))
    for name in top:
        assert name.startswith(".") or name.split(".")[0] in stdlib, f"模块级第三方依赖: {name}"


@pytest.mark.parametrize(("fn", "needle"), [
    ("get_hf_profile_prompt_text", "_profiles"),
    ("get_hf_profile_band_floor", "_profiles"),
    ("get_hf_profile_budget", "_profiles"),
    ("resolve_hf_profile_band_effect_for", "resolve_hf_profile_band_effect"),
])
def test_judge_hot_path_reads_have_no_io(fn, needle):
    """judge 热路径零 DB IO：读取口体内无导入 / 无 SQL / 无 await。"""
    body = _fn_code(PROFILE_SRC, fn)
    assert len(body) > 80, "切片自检：函数体必须被切出来"
    for banned in ("import ", "SELECT", "get_conn", "await ", "repo_"):
        assert banned not in body, f"{fn} 体内不得出现 {banned!r}"
    assert needle in body


def test_generate_path_is_the_only_io_place():
    """IO 只在生成路径：写入用 ``repo_hf.upsert_profile``，读取用两条 repo 里已有的查询。"""
    for fn in ("maybe_generate_hf_group_profiles", "_load_sample_lines"):
        body = _fn_code(PROFILE_SRC, fn)
        assert "from ...data import" in body, f"{fn} 必须延迟导入数据层（F5）"
    assert "upsert_profile(" in _fn_code(PROFILE_SRC, "maybe_generate_hf_group_profiles")
    assert "list_messages_by_peer(" in _fn_code(PROFILE_SRC, "_load_sample_lines")


def test_stats_sql_is_single_table_inbound_local_hour():
    """统计的 4 条聚合：单表 ``wpp_messages``、只算入站、本地小时、**不许用 create_time**。"""
    body = _fn_code(MSGS_SRC, "get_hf_group_message_stats")
    assert "FROM wpp_messages" in body
    assert "JOIN" not in body.upper().replace("JOINED", ""), "不得 JOIN（collation 历史坑）"
    assert body.count("direction = 'inbound'") == 1, "三段查询共用同一个 base 常量"
    assert "peer_kind = 'group'" in body
    assert "UNIX_TIMESTAMP(ts)" in body and "create_time" not in body, \
        "时间走 ts（create_time 是遗留死列，值是 YYYYMMDDHHMMSS）"
    assert "%% 86400" in body, "SQL 里的字面量 % 必须转义（有参数时 pymysql 会插值）"
    assert "CHAR_LENGTH" in body, "按字符计长度（不是字节）"


def test_profiles_listing_selects_raw_json():
    """``list_profiles`` 必须带 ``profile_json``：sweep 预热的**唯一**数据来源。

    只列元数据（当年 Py 侧就是这么写的）⇒ 缓存里永远是空的，而 judge 看不出区别。
    """
    body = _fn_code(HF_SRC, "list_profiles")
    for col in ("account_id", "group_id", "profile_json", "stats_json",
                "sample_msgs", "model", "version", "generated_at"):
        assert col in body, f"list_profiles 必须 SELECT {col}"
    assert "ORDER BY group_id" in body, "顺序稳定（否则预热结果随引擎心情）"


def test_fake_rows_match_real_selects():
    """**假行的列必须与真 ``SELECT`` 逐列一致** —— 否则所有用假行的用例一起漂移却仍然全绿。"""
    row = prof_row("g")
    list_body = _fn_code(HF_SRC, "list_profiles")
    for col in row:
        assert col in list_body, f"假行的列 {col!r} 不在 list_profiles 的 SELECT 里（两边已漂移）"
    m = msg("inbound", "x", 1)
    peer_body = _fn_code(MSGS_SRC, "list_messages_by_peer")
    for col in m:
        assert col in peer_body, f"假行的列 {col!r} 不在 list_messages_by_peer 的 SELECT 里"
    for key in STATS:
        assert key in _fn_code(MSGS_SRC, "get_hf_group_message_stats"), \
            f"假统计的键 {key!r} 不在真查询的返回里"


def test_pinned_constants():
    """跨实现共用的数字**逐个字面钉住** —— 一侧改动、另一侧没跟上就是静默失效。"""
    assert P.HF_PROFILE_DEFAULTS == {
        "enabled": True, "minMsgs": 30, "lookbackDays": 14, "maxTokens": 1200,
        "refreshHours": 22, "maxPerRun": 3, "maxPromptChars": 400, "sampleMsgs": 40,
        "timeoutMs": 20000, "applyQuietHours": False, "maxRaise": 0.1,
    }
    assert P.HF_PROFILE_MAX_RAISE == 0.1
    assert P._BAND_FALLBACK == (0.5, 0.9)
    assert P.LIM == {"nature": 60, "style": 60, "botRole": 60, "summary": 120,
                     "listItem": 30, "listLen": 5}
    assert len(P.HF_MUTE_PATTERNS) == 6


def test_band_fallback_matches_learn_once_it_exists():
    """**自激活**断言：``learn`` 一就位，回落钉子就必须与真源逐位相等。

    （``_BAND_FALLBACK`` 是 learn 缺席时的临时值；两边一旦不一致，
    "画像 band 的硬区间"就有两个版本 —— 一个在判定里，一个在解析里。）
    """
    learn_path = Path(P.__file__).with_name("learn.py")
    if not learn_path.exists():
        pytest.skip("learn.py 还没移植（第 6 步 #8）；就位后本条自动生效")
    from plugin.inbound.heartflow import learn as L

    D = L.HF_LEARNING_DEFAULTS
    assert (D["bandMin"], D["bandMax"]) == P._BAND_FALLBACK, \
        "learn 的 band 区间与 profile 的回落钉子已漂移 —— 一边改了一边没跟上"

    # 钉子相等还不够：**必须真从 learn 取**。两边只是"恰好同值"，而 profile 走的是
    # 自己那份硬编码 —— 那样老板改 bandMin 时判定侧纹丝不动（注释里说的唯一真源就废了）。
    cfg = {"learning": {"bandMin": 0.55, "bandMax": 0.85}}
    assert P._band_limits(cfg) == (0.55, 0.85), "profile 必须真委托 learn（而不是拿钉子凑数）"
    assert P._band_limits(None) == (D["bandMin"], D["bandMax"]), "缺配置时与 learn 缺省一致"
    # learn 取不到/取到脏值 ⇒ 才轮到钉子（这条不 skip：它就是钉子存在的全部理由）
    assert P._band_limits({"learning": {"bandMin": "x", "bandMax": "y"}}) == P._BAND_FALLBACK


def test_cache_is_registered_in_registry():
    assert "profile" in CACHE_REGISTRY.names()


def test_no_real_group_ids_in_sources():
    """R2 红线：群 ID 形态可断言；门店/品牌/人名**不能**写在这里。"""
    pat = re.compile(r"\d{9,}@chatroom")
    for src in (PROFILE_SRC, Path(__file__).read_text(encoding="utf-8")):
        assert not pat.search(src)


def _fn_code(src: str, name: str) -> str:
    """函数体的**纯代码**（剥掉 docstring），供「源码形状」断言用。

    为什么必须剥 docstring：本仓库的 docstring 是**反例说明书** ——
    ``get_hf_group_message_stats`` 的注释里就写着「不得用 ``create_time``」，
    拿整段源码去 ``assert "create_time" not in body`` 断言的是注释而不是代码：
    一个真实违反会**照样绿**，而一条无害的注释文字会让它**假红**。
    """
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            assert body, f"{name} 剥掉 docstring 后没有代码了（切片失败会变成静默假绿）"
            return ast.unparse(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])))
    raise AssertionError(f"找不到函数 {name} —— 切片失败必须是响亮的失败，不许静默假绿")
