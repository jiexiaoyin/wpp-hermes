"""心流闸门 —— TS ``src/inbound/heartflow.ts`` (39KB) + 三个接线点的 pytest 译本。

这是九模块里**唯一带 IO / 副作用**的一个（其余八个是纯逻辑或薄管线），所以本文件的重心
不是"算法对不对"，而是：

============================  ===================================================
**降级契约**                  任何一步坏掉都必须"不回复"而不是"报错 / 乱回复"
**台账三分**                  通过 / 不过 / 没出结果 —— 三者的落库行为**必须两两不同**
**交付缝翻译**                ``platform.SendResult`` → ``learn.HfSendResult``（错了会静默 pending）
**I1 阈值不进 prompt**        源级 + 行为级双钉
**I5 占位不占额度**            重复闸拦下的不得开窗 / 占预算 / 进历史
**F5 加载期纪律**             模块级零第三方依赖
============================  ===================================================

## TS 侧的对应关系

======================  ==========================================================
``heartflow.ts`` §1–§8 本文件 §1–§9（参数 / JSON / 打分 / 精力 / 缓冲 / prompt / 判定）
``handler.ts`` 560–890  §12（``maybe_engage`` 编排）+ §14（旁路 ``note_group_message``）
``dispatcher.ts`` 930–1000  §13（交付缝 ``pre_send`` / ``note_sent``）
``triggers.ts``         §10（``check_gate``）+ §11（``resolve_hf_effective_budget``）
======================  ==========================================================

⚠️ 假数据层一律 ``monkeypatch`` 掉（``persist_hf_*`` / ``_cfg_for`` / ``call_judge``）：
本文件**不碰真 DB**（真 DB 用例归 §15 的源级守卫与 ``-m db`` 的少数几条）。
指标计数器**不随 conftest 重置** ⇒ 断言一律用**增量**。
"""

from __future__ import annotations

import ast
import sys
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from plugin.core import metrics
from plugin.inbound.heartflow import budget as B
from plugin.inbound.heartflow import dedupe as D
from plugin.inbound.heartflow import heartflow as HF
from plugin.inbound.heartflow import learn as L
from plugin.inbound.heartflow import profile as P
from plugin.inbound.heartflow.runtime import CACHE_REGISTRY

HF_SRC = Path(HF.__file__).read_text(encoding="utf-8")

#: 基准时刻：**UTC 20:00**（本机 TZ +8 ⇒ 本地是次日 04:00）。
#:
#: ⚠️ 刻意挑"UTC 与本地**不同一天**"的时刻 —— §5 的时区用例要靠这个差异才钉得住
#: "每日重置用的是 UTC 而不是本地时间"（同一天的时刻会让那条断言恒真、什么也没验）。
NOW_MS = 1_789_070_400_000
NOW_SEC = NOW_MS // 1000


# ===== 0. 工具 =====


def cfg_on(**over: object) -> dict:
    """开着的心流配置（缺省 + 覆盖）。"""
    c = HF.default_hf_config()
    c["enabled"] = True
    c["model"] = "judge-model"
    c.update(over)
    return c


@dataclass
class FakeMsg:
    """入站消息（字段取自 ``plugin.platform.adapter`` 的事件对象）。"""

    chatroom_id: str = "g1@chatroom"
    peer_id: str = "g1@chatroom"
    content: str = "这批货什么时候到"
    msg_id: str = "m100"
    #: ⚠️ **没有** ``new_msg_id`` 字段：真的 ``WppMessage`` 也没有。服务端 id 只能从
    #: 原始 payload 取（``persist.pick_new_msg_id``）。这里以前凭空给 FakeMsg 加了
    #: 一个 ``new_msg_id`` 属性，于是 ``getattr(msg, "new_msg_id", "")`` 在测试里恒有值、
    #: 在线上恒为空 —— 假件的形状漂移把真 bug 盖住了。
    raw: dict = field(default_factory=lambda: {"NewMsgId": "n100"})
    from_wxid: str = "wxid_alice"
    from_nickname: str = "阿丽"
    msg_type: str = "text"
    #: ⚠️ 字段名对齐真的 ``inbound.parser.WppMessage``（那里叫 ``timestamp``，不是 ``ts``）——
    #: 名字写错的话 ``maybe_engage`` 取到 None，``candidate_at_sec`` 静默失效，测试还全绿。
    timestamp: int = NOW_SEC
    reply_to: dict | None = None


@dataclass
class FakeAcct:
    """账号（``plugin.core.accounts.Account`` 的心流相关子集）。"""

    id: str = "acc1"
    self_wxid: str = "wxid_bot"
    nickname: str = "小助理"
    heartflow: dict | None = None

    def __post_init__(self) -> None:
        if self.heartflow is None:
            self.heartflow = cfg_on()


class FakeAdapter:
    """适配器（心流只用它当"身份"透传，不调它的方法）。"""

    def __init__(self) -> None:
        self.sent: list[tuple] = []


def ledger_recorder(monkeypatch) -> list[tuple]:
    """把三个台账写入点换成记录器（本文件不碰真 DB）。"""
    calls: list[tuple] = []
    monkeypatch.setattr(L, "persist_hf_judged",
                        lambda rec: calls.append(("judged", rec)))
    monkeypatch.setattr(L, "persist_hf_judged_below_threshold",
                        lambda rec, at: calls.append(("below", rec, at)))
    monkeypatch.setattr(L, "persist_hf_send_outcome",
                        lambda *a, **kw: calls.append(("send", a, kw)))
    return calls


def creds(**over: object):
    """假 judge 凭证（``api_key`` 非空即"配好了"）。"""
    from plugin.llm import judge as judge_mod

    base = dict(api_key="sk-test", base_url="http://judge.invalid/v1", format="openai")
    base.update(over)
    return judge_mod.JudgeCreds(**base)


def fake_judge(monkeypatch, replies: list, *, recorder: list | None = None) -> list[dict]:
    """把 ``call_judge`` 换成脚本化假件，返回"每次调用的 kwargs"列表。

    顺带把 ``resolve_judge_creds`` 换掉 —— ``maybe_engage`` 不传 creds，走环境变量链；
    本地环境没有那三个 key（也不该有），不换掉的话每条编排用例都会"没配 key ⇒ 不判"。
    """
    from plugin.llm import judge as judge_mod

    got: list[dict] = recorder if recorder is not None else []
    seq = list(replies)

    async def _call(**kw):
        got.append(kw)
        item = seq[min(len(got) - 1, len(seq) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(judge_mod, "call_judge", _call)
    monkeypatch.setattr(judge_mod, "resolve_judge_creds", lambda: creds())
    return got


def good_json(**over: object) -> str:
    """一份满分响应（5 维全 10 ⇒ overall 1.0）。"""
    d = {k: 10 for k in HF.HF_DIMENSIONS}
    d.update(over)
    import json

    return json.dumps(d, ensure_ascii=False)


async def _engage(msg: FakeMsg, acct: FakeAcct | None = None, cfg: dict | None = None,
                  adapter=None):
    """``maybe_engage`` 的薄包装（本仓库无 pytest-asyncio ⇒ 手工 ``asyncio.run``）。"""
    a = acct or FakeAcct()
    if cfg is not None:
        a.heartflow = cfg
    return await HF.maybe_engage(adapter or FakeAdapter(), a, msg)


# ===== 1. 参数与缺省 =====


def test_default_config_is_a_deep_copy():
    """``weights`` 是嵌套 dict —— 共享出去就会被就地改坏（一处改、全局生效）。"""
    a = HF.default_hf_config()
    b = HF.default_hf_config()
    a["weights"]["relevance"] = 9.0
    a["whitelistGroups"].append("g1")
    assert b["weights"]["relevance"] == 0.25
    assert b["whitelistGroups"] == []


def test_defaults_pin_the_boss_decisions():
    """缺省值都是拍板过的数字，改动必须是有意的。"""
    c = HF.default_hf_config()
    assert c["enabled"] is False, "默认关：开机会话里逐账号显式开"
    assert c["timeoutMs"] == 5000
    assert c["maxRetries"] == 1, "5000ms × 2 次 = 最坏 10s（C 方案）"
    assert c["contextMessagesCount"] == 5
    assert c["energyDecayRate"] == 0.1 and c["energyRecoveryRate"] == 0.02
    assert c["includeReasoning"] is False
    assert c["whitelistGroups"] == [], "空 = 全放行"
    assert "model" not in c, "模型名必须由 accounts JSON 提供（v1.4.0 消除 hardcode）"


def test_reply_threshold_default_has_exactly_one_source():
    """阈值基线不许在这里另立一份（09-26 停摆的一半根因就是"三处各算一份"）。"""
    assert HF.HF_DEFAULTS["replyThreshold"] == L.HF_REPLY_THRESHOLD_FALLBACK


def test_weights_defaults_and_dirty_values():
    w = HF.resolve_hf_weights(None)
    assert w == {"relevance": 0.25, "willingness": 0.2, "social": 0.2,
                 "timing": 0.15, "continuity": 0.2}
    assert abs(sum(w.values()) - 1.0) < 1e-9, "权重和应为 1（overall 才是 0–1 尺度）"

    dirty = HF.resolve_hf_weights({"weights": {
        "relevance": float("nan"),   # NaN 会让 overall 变 NaN ⇒ 一条都不回且不报错
        "willingness": True,         # bool 不是数字
        "social": -1.0,              # 负权重无意义
        "timing": "0.5",             # 字符串不隐式转换
    }})
    assert dirty["relevance"] == 0.25 and dirty["willingness"] == 0.2
    assert dirty["social"] == 0.2 and dirty["timing"] == 0.15
    assert dirty["continuity"] == 0.2


def test_vision_cfg_defaults_and_coercion():
    assert HF.HF_VISION_DEFAULTS == {"enabled": True, "maxImages": 1}
    assert HF.resolve_hf_vision_cfg(None) == {"enabled": True, "maxImages": 1}
    v = HF.resolve_hf_vision_cfg({"vision": {"enabled": "yes", "maxImages": 3.9}})
    assert v == {"enabled": True, "maxImages": 3}, "非 bool 回落默认；小数向下取整"
    assert HF.resolve_hf_vision_cfg({"vision": {"maxImages": -2}}) == {
        "enabled": True, "maxImages": 1}, "负数 ⇒ 回落默认（不是 0：0 等于静默关掉看图）"


def test_is_hf_group_allowed_empty_means_all():
    assert HF.is_hf_group_allowed("anything", {}) is True
    assert HF.is_hf_group_allowed("anything", {"whitelistGroups": []}) is True
    c = {"whitelistGroups": ["g1", "g2"]}
    assert HF.is_hf_group_allowed("g1", c) is True
    assert HF.is_hf_group_allowed("g3", c) is False
    assert HF.is_hf_group_allowed("g1", {"whitelistGroups": "g1"}) is True, \
        "脏值（非列表）⇒ 按空处理 = 全放行（不是静默全拦）"


# ===== 2. JSON 提取 =====


def test_extract_json_three_paths():
    assert HF.extract_heartflow_json('{"relevance": 8}') == {"relevance": 8}
    assert HF.extract_heartflow_json('```json\n{"relevance": 8}\n```') == {"relevance": 8}
    assert HF.extract_heartflow_json('```\n{"a": 1}\n```') == {"a": 1}
    assert HF.extract_heartflow_json('好的：{"relevance": 8}\n以上。') == {"relevance": 8}
    # 两段 JSON（贪婪取最外层 = 第一个 { 到最后一个 }）⇒ 合成体解析失败 ⇒ None。
    # ⚠️ 与 TS 一致：``/\{[\s\S]*\}/`` 也是贪婪的，``JSON.parse`` 同样会失败。
    assert HF.extract_heartflow_json('{"a": 1} 噪声 {"b": 2}') is None


@pytest.mark.parametrize("bad", ["", "   ", None, "不确定", "{坏}", "{'a': 1}"])
def test_extract_json_rejects_garbage(bad):
    assert HF.extract_heartflow_json(bad) is None


def test_extract_json_rejects_non_objects():
    """JS 的 ``JSON.parse("[1,2]")`` 会成功，随后 5 维全 undefined ⇒ 一条 0 分的"有效"结果。

    Py 侧刻意收紧成"坏 JSON"（触发重试）。这是**故意的行为分歧**，钉住它以免被"对齐 TS"改回去。
    """
    assert HF.extract_heartflow_json("[1,2]") is None
    assert HF.extract_heartflow_json("42") is None
    assert HF.extract_heartflow_json('"str"') is None
    assert HF.extract_heartflow_json("null") is None
    # 三条路径**各自**都要过类型闸：直接解析能出数组、剥围栏后也能出数组。
    # 只在最后那条（贪婪 ``{...}``）上补一次是白补的 —— 那条正则的产物必是对象。
    assert HF.extract_heartflow_json("```json\n[1,2]\n```") is None
    assert HF.extract_heartflow_json("```\n42\n```") is None


# ===== 3. clamp_score =====


@pytest.mark.parametrize(("v", "want"), [
    (7, 7.0), (7.5, 7.5), ("8.25", 8.25), (-3, 0.0), (99, 10.0),
    (True, 0.0), (False, 0.0), (None, 0.0), ("abc", 0.0), ([1], 0.0),
    (float("nan"), 0.0), (float("inf"), 0.0), ("", 0.0),
])
def test_clamp_score(v, want):
    assert HF.clamp_score(v) == want


def test_clamp_score_keeps_zero_and_ten_at_the_edges():
    assert HF.clamp_score(0) == 0.0 and HF.clamp_score(10) == 10.0


# ===== 4. 看图（v1.11.0）=====


def test_extract_image_urls_basic():
    text = "看看这台\n[图片] https://oss.example/a.jpg\n[图片] https://oss.example/b.png"
    assert HF.extract_hf_image_urls(text, 5) == ["https://oss.example/a.jpg",
                                                "https://oss.example/b.png"]


def test_extract_image_urls_respects_scheme_and_dedup_and_cap():
    text = ("[图片] ftp://x/a.jpg\n"          # 非 http(s) ⇒ 丢
            "[图片] https://x/a.jpg\n"
            "[图片] https://x/a.jpg\n"        # 重复 ⇒ 去重
            "[图片] HTTP://X/C.JPG\n"         # 大小写不敏感
            "[图片] data:image/png;base64,AA")  # 非 http ⇒ 丢
    assert HF.extract_hf_image_urls(text, 5) == ["https://x/a.jpg", "HTTP://X/C.JPG"]
    assert HF.extract_hf_image_urls(text, 1) == ["https://x/a.jpg"], "按出现顺序截取前 N"


def test_extract_image_urls_edge_inputs():
    assert HF.extract_hf_image_urls("", 1) == []
    assert HF.extract_hf_image_urls(None, 1) == []
    assert HF.extract_hf_image_urls("[图片] https://x/a.jpg", 0) == [], "cap=0 ⇒ 不问"
    assert HF.extract_hf_image_urls("[图片]", 1) == [], "没有 URL 的 [图片] 标记不算"
    # vendor 尾部还会跟一句 "(注: ...)" —— 正则按空白截断
    assert HF.extract_hf_image_urls("[图片] https://x/a.jpg (注: 缩略图)", 1) == \
        ["https://x/a.jpg"]


def test_extract_image_urls_does_not_parse_host():
    """⚠️ 边界如实写下：只按 scheme 过滤，**不解析主机** —— ``127.0.0.1`` 也放行。

    来源本身可信（URL 由自家 enrich 从 vendor 媒体数组取出），真要收口该加在抓取侧。
    """
    assert HF.extract_hf_image_urls("[图片] http://127.0.0.1/a.jpg", 1) == \
        ["http://127.0.0.1/a.jpg"]


# ===== 5. 精力状态机 =====


def test_chat_state_starts_full_and_resets_on_utc_day_change():
    st = HF.get_chat_state("g1", cfg_on(), NOW_MS)
    assert (st.energy, st.total_messages, st.total_replies) == (1.0, 0, 0)
    utc_day = re.match(r"^(\d{4}-\d{2}-\d{2})$", st.last_reset_date)
    assert utc_day, "``last_reset_date`` 是 YYYY-MM-DD"

    # 消耗一点，跨 UTC 零点 ⇒ +0.2（把恢复游标归零，隔离出"每日重置"这一项）
    with HF._state_lock:
        st.energy = 0.5
        st.last_energy_recovery_ts = 0
    next_day = NOW_MS + 86400 * 1000
    st2 = HF.get_chat_state("g1", cfg_on(), next_day)
    assert abs(st2.energy - 0.7) < 1e-9
    assert st2 is st, "同一个群是同一个对象（不是每次新建）"


def test_daily_reset_uses_utc_not_local_time():
    """本机 TZ 是 +8：跟本地走会让"每日重置点"偏 8 小时。

    ⚠️ 挑了 UTC 20:xx（本地次日 04:xx）的时刻 —— 两种实现给出**不同**的日期串。
    """
    import time as _t

    ms = NOW_MS
    utc = _t.strftime("%Y-%m-%d", _t.gmtime(ms / 1000.0))
    local = _t.strftime("%Y-%m-%d", _t.localtime(ms / 1000.0))
    if utc == local:
        pytest.skip("该时刻 UTC 与本地同一天，钉不出差异（换个时刻即可）")
    assert HF.get_chat_state("g1", cfg_on(), ms).last_reset_date == utc, \
        "必须按 UTC 重置（对齐 TS 的 toISOString().slice(0,10)）"


def test_daily_reset_caps_at_one():
    st = HF.get_chat_state("g1", cfg_on(), NOW_MS)
    with HF._state_lock:
        st.energy = 0.95
    HF.get_chat_state("g1", cfg_on(), NOW_MS + 86400 * 1000)
    assert st.energy == 1.0


def test_time_recovery_advances_energy_but_not_last_reply_time():
    """P0-5：恢复只推 ``last_energy_recovery_ts``。共用字段 ⇒ ``secondsSinceLastReply`` 恒 0
    ⇒ 冷却闸形同不存在（每个群看起来都"刚刚回过"）。"""
    st = HF.get_chat_state("g1", cfg_on(), NOW_MS)
    with HF._state_lock:
        st.energy = 0.5
        st.last_reply_time = NOW_MS - 300_000
        st.last_energy_recovery_ts = NOW_MS - 60_000        # 1 分钟前
    before = st.last_reply_time
    HF.get_chat_state("g1", cfg_on(), NOW_MS)
    assert abs(st.energy - (0.5 + 1 * 0.02 * 5)) < 1e-9, "每分钟恢复 recovery×5"
    assert st.last_reply_time == before, "恢复不许推进 last_reply_time"
    assert st.last_energy_recovery_ts == NOW_MS, "恢复时刻本身要推进（否则下次重复计）"


def test_time_recovery_ignores_negative_elapsed_and_caps():
    st = HF.get_chat_state("g1", cfg_on(), NOW_MS)
    with HF._state_lock:
        st.energy = 0.9
        st.last_energy_recovery_ts = NOW_MS + 60_000       # 时钟回拨
    HF.get_chat_state("g1", cfg_on(), NOW_MS)
    assert st.energy == 0.9, "回拨不产生负恢复"
    with HF._state_lock:
        st.last_energy_recovery_ts = NOW_MS - 86_400_000
    HF.get_chat_state("g1", cfg_on(), NOW_MS)
    assert st.energy == 1.0, "上限 1.0"


def test_record_active_reply_decays_with_floor_and_counts():
    c = cfg_on()
    st = HF.get_chat_state("g1", c, NOW_MS)
    HF.record_active_reply("g1", c, NOW_MS)
    assert abs(st.energy - 0.9) < 1e-9
    assert (st.total_replies, st.total_messages) == (1, 1)
    assert st.last_reply_time == NOW_MS
    for _ in range(20):
        HF.record_active_reply("g1", c, NOW_MS)
    assert st.energy == 0.1, "地板 0.1（精力耗尽 = 极其不愿说话，不是永不再说）"


def test_record_passive_message_gains_with_cap():
    c = cfg_on()
    st = HF.get_chat_state("g1", c, NOW_MS)
    with HF._state_lock:
        st.energy = 0.5
    HF.record_passive_message("g1", c, NOW_MS)
    assert abs(st.energy - 0.52) < 1e-9
    assert st.total_messages == 1 and st.total_replies == 0
    for _ in range(50):
        HF.record_passive_message("g1", c, NOW_MS)
    assert st.energy == 1.0


def test_seconds_since_last_reply_zero_means_never():
    """``0`` = **从未回复**（不是"刚刚回复过"）—— 冷却闸靠这个语义不误伤新群。"""
    assert HF.seconds_since_last_reply("gNew", NOW_MS) == 0.0
    c = cfg_on()
    HF.record_active_reply("g1", c, NOW_MS)
    assert HF.seconds_since_last_reply("g1", NOW_MS + 90_000) == 90.0


# ===== 6. 原始缓冲 =====


def test_raw_buffer_keeps_order_and_caps_at_200():
    for i in range(250):
        HF.record_raw_message("g1", HF.HfRawMessage("u", "", f"m{i}", float(i)))
    assert HF.HF_RAW_BUFFER_MAX == 200
    got = HF.get_raw_buffer("g1", 10_000)
    assert len(got) == 200, "环形缓冲：丢最旧"
    assert got[0].content == "m50" and got[-1].content == "m249"
    assert HF.get_raw_buffer("g1", 3) == got[-3:]
    assert HF.get_raw_buffer("g1", 0) == []
    assert HF.get_raw_buffer("gNone", 5) == []


def test_format_and_contexts():
    msgs = [HF.HfRawMessage("阿丽", "", "在吗", 1.0),
            HF.HfRawMessage("小助理", "", "在的", 2.0, is_bot=True)]
    assert HF.format_raw_messages(msgs) == "[阿丽]: 在吗\n[机器人]: 在的"
    assert HF.format_raw_messages([]) == "暂无对话历史"
    assert HF.raw_messages_to_contexts(msgs) == [
        {"role": "user", "content": "在吗"}, {"role": "assistant", "content": "在的"}]


def test_last_bot_reply_skips_empty_and_picks_most_recent():
    HF.record_raw_message("g1", HF.HfRawMessage("b", "", "第一句", 1.0, is_bot=True))
    HF.record_raw_message("g1", HF.HfRawMessage("u", "", "群友插话", 2.0))
    assert HF.last_bot_reply("g1") == "第一句"
    HF.record_raw_message("g1", HF.HfRawMessage("b", "", "   ", 3.0, is_bot=True))
    assert HF.last_bot_reply("g1") == "第一句", "空白正文不算一次回复"
    HF.record_raw_message("g1", HF.HfRawMessage("b", "", "第二句", 4.0, is_bot=True))
    assert HF.last_bot_reply("g1") == "第二句"
    assert HF.last_bot_reply("gNone") is None


def test_context_summary_reports_activity_rate_and_engagement():
    c = cfg_on()
    st = HF.get_chat_state("g1", c, NOW_MS)
    with HF._state_lock:
        st.total_messages, st.total_replies = 10, 2
    s = HF.build_chat_context_summary("g1", c, NOW_MS)
    assert "最近活跃度: 低" in s, "≤20 条 = 低"
    assert "历史回复率: 20.0%" in s
    assert re.search(r"当前时间: \d{2}:\d{2}", s)

    with HF._state_lock:
        st.total_messages = 50
    assert "最近活跃度: 中" in HF.build_chat_context_summary("g1", c, NOW_MS)
    with HF._state_lock:
        st.total_messages = 101
    assert "最近活跃度: 高" in HF.build_chat_context_summary("g1", c, NOW_MS)


def test_context_summary_engagement_needs_a_previous_bot_reply():
    c = cfg_on()
    HF.get_chat_state("g1", c, NOW_MS)
    assert "回复效果" not in HF.build_chat_context_summary("g1", c, NOW_MS), \
        "没有过 bot 发言 ⇒ 谈不上「回复效果」"

    HF.record_raw_message("g1", HF.HfRawMessage("b", "", "在的", 1.0, is_bot=True))
    assert "上次回复后无人接话" in HF.build_chat_context_summary("g1", c, NOW_MS)
    for i in range(3):
        HF.record_raw_message("g1", HF.HfRawMessage("u", "", f"接话{i}", 2.0 + i))
    assert "上次回复后群里进行了热烈讨论" in HF.build_chat_context_summary("g1", c, NOW_MS)


def test_mark_judged_is_what_the_interval_gate_reads():
    assert HF.judge_interval_count() == 0
    HF.mark_heartflow_judged("g1", NOW_MS)
    assert HF._last_judge_at["g1"] == NOW_MS


# ===== 7. build_prompt =====


def test_prompt_contains_no_threshold_number():
    """**INVARIANT I1**（行为级）：上游曾在 prompt 里写"回复阈值: X"，裁判会按那个数校准
    自己的打分 ⇒ 阈值一动分数跟着动 ⇒ 09-26 自锁停摆 3 天。"""
    p = HF.build_prompt(HF.HfJudgeInput(chat_id="g1", content="在吗", energy=1.0), cfg_on())
    for banned in ("回复阈值", "阈值", "threshold", "0.6", "60%", "0.60"):
        assert banned not in p, f"prompt 里不得出现 {banned!r}"


def test_prompt_has_the_five_dimensions_and_the_json_reminder():
    p = HF.build_prompt(HF.HfJudgeInput(chat_id="g1", content="在吗"), cfg_on())
    for dim in ("relevance", "willingness", "social", "timing", "continuity"):
        assert f'"{dim}"' in p
    assert HF.HF_JSON_REMINDER in p, "重试锚点必须逐字在 prompt 里（否则重试是空操作）"
    for rule in ("内容相关度", "回复意愿", "社交适宜性", "时机恰当性", "对话连贯性"):
        assert rule in p


def test_prompt_optional_parts():
    c = cfg_on()
    base = HF.HfJudgeInput(chat_id="g1", content="x")
    p = HF.build_prompt(base, c)
    assert "默认角色：智能助手" in p and "reasoning" not in p
    assert "暂无上次回复记录" in p and "从未回复" in p
    assert "本群画像" not in p, "没有画像文本 ⇒ 整段不注入（不是留个空壳）"
    assert "我的精力水平: 1.0/1.0" in p, "精力是 0–1 尺度，展示到小数点后一位"
    assert '"reasoning"' in HF.build_prompt(base, cfg_on(includeReasoning=True)), \
        "includeReasoning=True ⇒ 才要写理由（默认 False 省 token）"

    p2 = HF.build_prompt(HF.HfJudgeInput(
        chat_id="g1", content="x", bot_nickname="小助理", last_bot_reply="在的",
        seconds_since_last_reply=125.0, chat_context="最近活跃度: 高",
        recent_messages="[阿丽]: 在吗", group_profile="手机店售后群"), cfg_on())
    assert "我是 小助理" in p2
    assert "2分钟前" in p2, "125s ⇒ 取整 2 分钟"
    assert "手机店售后群" in p2 and "本群画像" in p2

    p3 = HF.build_prompt(base, cfg_on(includeReasoning=True))
    assert '"reasoning"' in p3


def test_prompt_context_count_comes_from_config():
    p = HF.build_prompt(HF.HfJudgeInput(chat_id="g1", content="x"),
                        cfg_on(contextMessagesCount=12))
    assert "## 最近12条对话历史" in p
    p0 = HF.build_prompt(HF.HfJudgeInput(chat_id="g1", content="x"),
                         cfg_on(contextMessagesCount="bad"))
    assert "## 最近5条对话历史" in p0


def test_prompt_clips_untrusted_chat_text():
    """群聊原文长度不可控：单条长文能把 judge prompt 撑到几万 token。

    输出预算只有 300 token —— prompt 不设上限时，费用随群友的一条消息走。
    """
    p = HF.build_prompt(HF.HfJudgeInput(
        chat_id="g1", content="长" * 5000, sender_name="名" * 500,
        recent_messages="史" * 9000, last_bot_reply="回" * 900,
    ), cfg_on())
    assert "长" * 2000 + "…" in p
    assert "长" * 2001 not in p
    assert "史" * 4000 + "…" in p
    assert "回" * 500 + "…" in p
    assert "名" * 100 + "…" in p


# ===== 8. parse_response =====


def test_parse_response_weighted_overall_and_threshold():
    """``overall`` 是加权分 **÷10** ⇒ 与 ``replyThreshold``（0–1）同尺度（I11）。"""
    r = HF.parse_response(good_json(), cfg_on())
    assert r.overall_score == 1.0 and r.should_reply is True
    assert r.dimensions == {k: 10.0 for k in HF.HF_DIMENSIONS}

    zero = HF.parse_response(good_json(**{k: 0 for k in HF.HF_DIMENSIONS}), cfg_on())
    assert zero.overall_score == 0.0 and zero.should_reply is False

    # 恰好压线（权重和 1 ⇒ 全 6 分 = 0.6）
    six = HF.parse_response(good_json(**{k: 6 for k in HF.HF_DIMENSIONS}), cfg_on())
    assert abs(six.overall_score - 0.6) < 1e-9
    assert six.should_reply is True, ">= 阈值 ⇒ 过（边界含等号）"
    assert HF.parse_response(good_json(**{k: 5 for k in HF.HF_DIMENSIONS}),
                             cfg_on()).should_reply is False


def test_parse_response_weighted_not_arithmetic_mean():
    """5 维权重不同 —— 用算术平均会算出不同的分数（权重是拍板过的）。"""
    d = {"relevance": 10, "willingness": 0, "social": 0, "timing": 0, "continuity": 0}
    r = HF.parse_response(good_json(**d), cfg_on())
    assert abs(r.overall_score - 0.25) < 1e-9, "只有 0.25 权重的 relatedness 满分"
    assert r.should_reply is False


def test_parse_response_uses_the_configured_threshold():
    d = {k: 8 for k in HF.HF_DIMENSIONS}
    assert HF.parse_response(good_json(**d), cfg_on()).should_reply is True
    assert HF.parse_response(good_json(**d),
                             cfg_on(replyThreshold=0.9)).should_reply is False


def test_parse_response_missing_dims_are_zero_not_error():
    """模型漏一个维度 ⇒ 那个维度 0 分（而不是整条作废）—— 与 TS 逐字一致。"""
    r = HF.parse_response('{"relevance": 10}', cfg_on())
    assert r.dimensions["relevance"] == 10.0 and r.dimensions["social"] == 0.0
    assert r.overall_score == 0.25


def test_parse_response_reasoning_only_when_a_string():
    assert HF.parse_response(good_json(reasoning="因为该接"), cfg_on()).reasoning == "因为该接"
    assert HF.parse_response(good_json(reasoning={"a": 1}), cfg_on()).reasoning == ""
    assert HF.parse_response(good_json(), cfg_on()).reasoning == ""


def test_parse_response_bad_json_is_none():
    assert HF.parse_response("我不确定要不要回", cfg_on()) is None
    assert HF.parse_response("", cfg_on()) is None


# ===== 9. judge_heartflow =====


def test_judge_missing_api_key_warns_and_returns_none(monkeypatch, caplog):
    got = fake_judge(monkeypatch, [good_json()])
    with caplog.at_level(logging.WARNING, logger=HF.__name__):
        out = asyncio.run(HF.judge_heartflow(
            HF.HfJudgeInput(chat_id="g1", content="x"), cfg_on(),
            creds=creds(api_key="")))
    assert out is None and got == [], "没配 key 就不该发请求"
    assert any("API key" in r.getMessage() for r in caplog.records)


def test_judge_missing_model_raises_not_falls_back():
    """v1.4.0 项目约定"消除 plugin hardcode"：模型名必须由 accounts JSON 给。

    静默回落某个模型的表现是"换了模型但线上没换"，只在账单与判分分布上看得见。
    """
    c = cfg_on()
    del c["model"]
    with pytest.raises(ValueError, match="heartflow.model"):
        asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g1", content="x"), c,
                                       creds=creds()))
    with pytest.raises(ValueError, match="heartflow.model"):
        asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g1", content="x"),
                                       cfg_on(model=""), creds=creds()))


def test_judge_passes_expected_kwargs(monkeypatch):
    got = fake_judge(monkeypatch, [good_json()])
    out = asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g1", content="x"),
                                         cfg_on(timeoutMs=1234), creds=creds()))
    assert out is not None and out.should_reply is True
    assert len(got) == 1
    kw = got[0]
    assert kw["model"] == "judge-model"
    assert kw["max_tokens"] == HF.HF_JUDGE_MAX_TOKENS == 300, \
        "300 硬编码（对齐 TS heartflow.ts:755；调大没意义，调小会截断 JSON ⇒ 恒不发言）"
    assert kw["timeout_ms"] == 1234
    assert kw["images"] == []
    assert HF.HF_JSON_REMINDER in kw["user_prompt"]
    assert "专业的群聊回复决策系统" in kw["system_prompt"]
    assert kw["creds"].api_key == "sk-test"


def test_judge_retries_and_replaces_only_the_reminder_line(monkeypatch, caplog):
    got = fake_judge(monkeypatch, ["我拒绝输出JSON", good_json()])
    out = asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g1", content="x"),
                                         cfg_on(), creds=creds()))
    assert out is not None, "第一次坏 JSON、第二次成功 ⇒ 必须返回结果"
    assert len(got) == 2
    first, second = got[0]["user_prompt"], got[1]["user_prompt"]
    assert "第2次尝试" in second
    assert second == first.replace(HF.HF_JSON_REMINDER, re.search(
        r"\*\*重要！！！[^*]*\*\*", second).group(0)), "除锚点那一行外逐字不变"


def test_judge_gives_up_after_max_retries_with_a_warning(monkeypatch, caplog):
    got = fake_judge(monkeypatch, ["坏", "也坏"])
    with caplog.at_level(logging.WARNING, logger=HF.__name__):
        out = asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g1", content="x"),
                                             cfg_on(), creds=creds()))
    assert out is None
    assert len(got) == 2, "maxRetries=1 ⇒ 最多 2 次调用"
    assert any("judge failed" in r.getMessage() for r in caplog.records)


def test_judge_max_retries_zero_means_one_call(monkeypatch):
    got = fake_judge(monkeypatch, ["坏"])
    assert asyncio.run(HF.judge_heartflow(
        HF.HfJudgeInput(chat_id="g1", content="x"), cfg_on(maxRetries=0),
        creds=creds())) is None
    assert len(got) == 1


def test_judge_swallows_exceptions(monkeypatch, caplog):
    from plugin.llm.judge import JudgeError

    got = fake_judge(monkeypatch, [JudgeError("timeout"), JudgeError("timeout")])
    with caplog.at_level(logging.WARNING, logger=HF.__name__):
        out = asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g1", content="x"),
                                             cfg_on(), creds=creds()))
    assert out is None and len(got) == 2
    assert any("timeout" in r.getMessage() for r in caplog.records)


def test_judge_sends_images_only_from_the_candidate_message(monkeypatch):
    """I9：只看**待判定消息**的图（上下文里的图不看 —— 成本可控 + 焦点清楚）。"""
    got = fake_judge(monkeypatch, [good_json()])
    HF.record_raw_message("g1", HF.HfRawMessage("u", "", "[图片] https://x/ctx.jpg", 1.0))
    asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(
        chat_id="g1", content="晒单 [图片] https://x/cand.jpg",
        recent_messages="[阿丽]: [图片] https://x/ctx.jpg"), cfg_on(), creds=creds()))
    assert got[0]["images"] == ["https://x/cand.jpg"]


def test_judge_vision_off_or_no_image_keeps_images_empty(monkeypatch):
    got = fake_judge(monkeypatch, [good_json(), good_json()])
    asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(
        chat_id="g1", content="[图片] https://x/a.jpg"),
        cfg_on(vision={"enabled": False}), creds=creds()))
    asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g1", content="纯文字"),
                                   cfg_on(), creds=creds()))
    assert got[0]["images"] == [] and got[1]["images"] == [], \
        "无图 / 关掉 ⇒ 空列表（judge 侧据此保持请求体逐字节不变）"


def test_judge_respects_max_images(monkeypatch):
    got = fake_judge(monkeypatch, [good_json()])
    asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(
        chat_id="g1", content="[图片] https://x/a.jpg [图片] https://x/b.jpg"),
        cfg_on(vision={"enabled": True, "maxImages": 1}), creds=creds()))
    assert got[0]["images"] == ["https://x/a.jpg"]


def test_judge_business_context_prefixes_the_system_prompt(monkeypatch):
    got = fake_judge(monkeypatch, [good_json()])
    asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g1", content="x"),
                                   cfg_on(businessContext="我们是手机店"), creds=creds()))
    assert got[0]["system_prompt"].startswith("我们是手机店\n\n")


def test_a_group_business_context_overrides_the_account_wide_one(monkeypatch):
    """账号级那段写的是全员群口径；别的群必须能用群级值覆盖它（2026-10-18）。"""
    got = fake_judge(monkeypatch, [good_json()])
    cfg = cfg_on(businessContext="全员群晒单口径",
                 groupBusinessContexts={"g1": "移动业务对接群口径"})
    asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g1", content="x"),
                                   cfg, creds=creds()))
    assert got[0]["system_prompt"].startswith("移动业务对接群口径\n\n")
    assert "全员群晒单口径" not in got[0]["system_prompt"]


def test_an_unknown_or_blank_group_context_falls_back_to_the_account_level(monkeypatch):
    got = fake_judge(monkeypatch, [good_json(), good_json()])
    cfg = cfg_on(businessContext="账号口径",
                 groupBusinessContexts={"g1": "   ", "other": "别的群"})
    asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g1", content="x"),
                                   cfg, creds=creds()))
    asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g2", content="x"),
                                   cfg, creds=creds()))
    assert got[0]["system_prompt"].startswith("账号口径\n\n"), "空白群级值不许把背景整段丢掉"
    assert got[1]["system_prompt"].startswith("账号口径\n\n")


def test_resolve_business_context_is_pure_and_trimmed():
    cfg = {"businessContext": " 账号 ", "groupBusinessContexts": {"g1": " 群级 "}}
    assert HF.resolve_business_context("g1", cfg) == "群级"
    assert HF.resolve_business_context("g2", cfg) == "账号"
    assert HF.resolve_business_context("g1", {}) == ""
    assert HF.resolve_business_context("g1", {"groupBusinessContexts": "bad"}) == ""


def test_judge_falls_back_to_env_creds(monkeypatch):
    """``creds=None`` ⇒ 走 ``resolve_judge_creds()``（环境变量链）。"""
    from plugin.llm import judge as judge_mod

    fake_judge(monkeypatch, [good_json()])
    monkeypatch.setattr(judge_mod, "resolve_judge_creds", lambda: creds(api_key="sk-env"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    out = asyncio.run(HF.judge_heartflow(HF.HfJudgeInput(chat_id="g1", content="x"),
                                         cfg_on()))
    assert out is not None


# ===== 10. check_gate =====


def test_gate_order_disabled_whitelist_empty():
    assert HF.check_gate("g1", "在吗", {}, NOW_MS) == HF.HfGateResult(False, "disabled")
    c = cfg_on(whitelistGroups=["gOther"])
    assert HF.check_gate("g1", "在吗", c, NOW_MS).reason == "not-whitelisted"
    assert HF.check_gate("g1", "   ", cfg_on(), NOW_MS).reason == "empty"
    assert HF.check_gate("g1", "", cfg_on(), NOW_MS).reason == "empty"
    assert HF.check_gate("gOther", "在吗", c, NOW_MS).allowed is True


def test_gate_allows_clean_config():
    g = HF.check_gate("g1", "在吗", cfg_on(), NOW_MS)
    assert g.allowed is True and g.reason is None


def test_gate_cooling_does_not_punish_a_group_that_never_replied():
    """``secondsSinceLastReply == 0`` = 从未回复 ⇒ **必须放行**。

    写反的后果：新群的第一条消息被自己的冷却闸吃掉（而"新群从不响应"看起来像
    "心流不工作"）。
    """
    c = cfg_on(minReplyIntervalSec=600)
    assert HF.check_gate("gNew", "第一条", c, NOW_MS).allowed is True
    HF.record_active_reply("gNew", c, NOW_MS)
    r = HF.check_gate("gNew", "第二条", c, NOW_MS + 60_000)
    assert r == HF.HfGateResult(False, "cooling")
    assert HF.check_gate("gNew", "第三条", c, NOW_MS + 600_000).allowed is True, \
        "恰好等于间隔 ⇒ 放行"


def test_gate_judge_cooldown_reads_the_judged_mark():
    c = cfg_on(minJudgeIntervalSec=30)
    assert HF.check_gate("g1", "x", c, NOW_MS).allowed is True
    HF.mark_heartflow_judged("g1", NOW_MS)
    assert HF.check_gate("g1", "x", c, NOW_MS + 10_000).reason == "judge-cooldown"
    assert HF.check_gate("g1", "x", c, NOW_MS + 30_000).allowed is True
    # mark 为 0（哨兵）不算"刚判过"
    HF.mark_heartflow_judged("g2", 0)
    assert HF.check_gate("g2", "x", c, NOW_MS).allowed is True


def test_gate_budget_peeks_without_recording(monkeypatch):
    """预算用 ``peek``：**判了但被下游拦掉的不能占额度**（记账只在真发出去时）。

    调 ``check_budget``（记账版）会让每次被拦的判定都推进计数 ⇒ 越拦越拦，直到整群静音。
    """
    B.reset_budget_cache()
    c = cfg_on(budget={"enabled": True, "minGapSec": 600, "maxPerHour": 8, "maxPerDay": 60})
    B.note_reply_sent("acc1", "g1", NOW_SEC)
    r = HF.check_gate("g1", "x", c, NOW_MS, account_id="acc1")
    assert r.allowed is False and r.reason == B.REASON_GAP
    state_before = B.get_budget_state("acc1", "g1", NOW_SEC)
    HF.check_gate("g1", "x", c, NOW_MS, account_id="acc1")
    assert B.get_budget_state("acc1", "g1", NOW_SEC) == state_before, "peek 不许改状态"


def test_gate_budget_reason_is_passed_through(monkeypatch):
    B.reset_budget_cache()
    c = cfg_on(budget={"enabled": True, "minGapSec": 0, "maxPerHour": 1, "maxPerDay": 60,
                       "noConsecutiveWithoutHuman": False})
    B.note_reply_sent("acc1", "g1", NOW_SEC)
    r = HF.check_gate("g1", "x", c, NOW_MS, account_id="acc1")
    assert r.reason == B.REASON_HOUR


def test_gate_uses_budget_override_when_given():
    """``budget_override``（生效预算）优先于配置预算 —— 画像收紧必须真的挡人。"""
    B.reset_budget_cache()
    c = cfg_on(budget={"enabled": True, "minGapSec": 0, "maxPerHour": 8, "maxPerDay": 60,
                       "noConsecutiveWithoutHuman": False})
    tight = B.resolve_budget({"budget": {"enabled": True, "minGapSec": 999, "maxPerHour": 8,
                                         "maxPerDay": 60, "noConsecutiveWithoutHuman": False}})
    B.note_reply_sent("acc1", "g1", NOW_SEC)
    assert HF.check_gate("g1", "x", c, NOW_MS, account_id="acc1").allowed is True
    r = HF.check_gate("g1", "x", c, NOW_MS, account_id="acc1", budget_override=tight)
    assert r.allowed is False and r.reason == B.REASON_GAP


def test_gate_passes_candidate_at_sec_through():
    """陈旧触发防重放：候选自己的时刻**不晚于**我们上次发言 ⇒ 是同一批消息被重复处理
    （debounce 重放 / 重试 / 补扫），拦住它；比上次发言新的候选才放行。

    ⚠️ ``candidate_at_sec`` 只喂这一条判据 —— 最小间隔问的是"距我们上次说话多久"，
    用的是**现在**（不是候选自己的时刻），所以那条不随它变。
    """
    B.reset_budget_cache()
    c = cfg_on(budget={"enabled": True, "minGapSec": 0, "maxPerHour": 8, "maxPerDay": 60,
                       "noConsecutiveWithoutHuman": True})
    B.note_reply_sent("acc1", "g1", NOW_SEC)
    stale = HF.check_gate("g1", "x", c, NOW_MS, account_id="acc1",
                          candidate_at_sec=NOW_SEC)
    assert stale.allowed is False and stale.reason == B.REASON_CONSECUTIVE
    fresh = HF.check_gate("g1", "x", c, NOW_MS, account_id="acc1",
                          candidate_at_sec=NOW_SEC + 10)
    assert fresh.allowed is True, "候选比上次发言新 ⇒ 真·新消息"


def test_gate_budget_state_cache_reset_isolates_tests():
    """预算缓存必须登记（否则跨用例污染：这条用例依赖的"空状态"可能被上一条污染）。"""
    assert "budget" in CACHE_REGISTRY.names()


# ===== 11. resolve_hf_effective_budget =====


def test_effective_budget_composes_three_layers(monkeypatch):
    base_cfg = cfg_on(budget={"enabled": True, "minGapSec": 180, "maxPerHour": 8,
                              "maxPerDay": 60})
    eff = HF.resolve_hf_effective_budget("acc1", "g1", base_cfg, NOW_SEC)
    assert (eff.min_gap_sec, eff.max_per_hour, eff.max_per_day) == (180, 8, 60)

    monkeypatch.setattr(P, "get_hf_profile_budget",
                        lambda a, g: {"minGapSec": 900, "maxPerHour": 2, "maxPerDay": 20})
    eff2 = HF.resolve_hf_effective_budget("acc1", "g1", base_cfg, NOW_SEC)
    assert (eff2.min_gap_sec, eff2.max_per_hour, eff2.max_per_day) == (900, 2, 20), \
        "画像收紧生效"


def test_effective_budget_only_tightens(monkeypatch):
    """**I2**：三层复合只许更严（09-26 复盘：漂移永远朝"松"的方向）。"""
    base_cfg = cfg_on(budget={"enabled": True, "minGapSec": 180, "maxPerHour": 8,
                              "maxPerDay": 60})
    monkeypatch.setattr(P, "get_hf_profile_budget",
                        lambda a, g: {"minGapSec": 10, "maxPerHour": 999, "maxPerDay": 9999})
    eff = HF.resolve_hf_effective_budget("acc1", "g1", base_cfg, NOW_SEC)
    assert (eff.min_gap_sec, eff.max_per_hour, eff.max_per_day) == (180, 8, 60), \
        "画像给的「更松」必须被忽略（不是断言抛错，是取交集）"


def test_effective_budget_quiet_hours_need_explicit_optin(monkeypatch):
    """账号自己的静默段**始终生效**；**画像建议的**静默段要 ``applyQuietHours: true`` 才并入。"""
    quiet = {"enabled": True, "minGapSec": 180, "maxPerHour": 8, "maxPerDay": 60,
             "quietHours": [[1, 5]]}
    base_cfg = cfg_on(budget={"enabled": True, "minGapSec": 180, "maxPerHour": 8,
                              "maxPerDay": 60})
    monkeypatch.setattr(P, "get_hf_profile_budget", lambda a, g: dict(quiet))
    off = HF.resolve_hf_effective_budget("acc1", "g1", base_cfg, NOW_SEC)
    assert off.quiet_hours == (), "老板 2026-09-26「静默段默认关」—— 画像的一段不许静默生效"

    on_cfg = cfg_on(budget={"enabled": True, "minGapSec": 180, "maxPerHour": 8,
                            "maxPerDay": 60}, profile={"applyQuietHours": True})
    on = HF.resolve_hf_effective_budget("acc1", "g1", on_cfg, NOW_SEC)
    assert [tuple(x) for x in on.quiet_hours] == [(1, 5)], \
        "显式开 ⇒ 画像那段并入（合并函数的返回形状不保证 tuple，故归一化后比）"

    own = cfg_on(budget={"enabled": True, "minGapSec": 180, "maxPerHour": 8, "maxPerDay": 60,
                         "quietHours": [[23, 7]]})
    monkeypatch.setattr(P, "get_hf_profile_budget", lambda a, g: None)
    assert HF.resolve_hf_effective_budget("acc1", "g1", own, NOW_SEC).quiet_hours \
        == ((23, 7),), "账号档的静默段是配置，不是建议"


def test_effective_budget_quiet_hours_come_from_the_profile_cache():
    """★ 主路径回归：开关打开后，静默段必须真的从**画像缓存**并进生效预算。

    上面那条用例手工把 ``quietHours`` 塞进 ``get_hf_profile_budget`` 的返回值里 ——
    而真实缓存从不这么给（``clamp_hf_profile_budget`` 的口径不含静默段）。2026-10-18
    实测正是这个缝：``applyQuietHours=true`` 但每个群生效的 ``quiet_hours`` 全是空。
    这里只灌缓存、不 mock getter，走真实合成路径。
    """
    rows = [{"group_id": "g1@chatroom", "generated_at": NOW_SEC,
             "profile_json": json.dumps({"summary": "零售售后群",
                                         "quietHours": [[1, 5]]})}]
    assert P.load_hf_profiles_into_cache("acc1", rows, 400) == 1
    base_cfg = cfg_on(budget={"enabled": True, "minGapSec": 180, "maxPerHour": 8,
                              "maxPerDay": 60})
    off = HF.resolve_hf_effective_budget("acc1", "g1@chatroom", base_cfg, NOW_SEC)
    assert off.quiet_hours == (), "默认关 —— 缓存里有也不生效"

    on_cfg = cfg_on(budget={"enabled": True, "minGapSec": 180, "maxPerHour": 8,
                            "maxPerDay": 60}, profile={"applyQuietHours": True})
    on = HF.resolve_hf_effective_budget("acc1", "g1@chatroom", on_cfg, NOW_SEC)
    assert [tuple(x) for x in on.quiet_hours] == [(1, 5)], \
        "开了开关，缓存里的静默段必须并进生效预算"


def test_effective_budget_splits_long_profile_quiet_hours():
    """★ 画像给 23–07（8h）时，生效预算应拆成 ≤6h 的段，而不是整段丢弃。

    真实画像常见 23–07 / 23–06；旧的「单段 >6h 整段丢弃」让这些建议全部落空。
    """
    rows = [{"group_id": "g-split@chatroom", "generated_at": NOW_SEC,
             "profile_json": json.dumps({"summary": "夜间订单群",
                                         "quietHours": [[23, 7]]})}]
    assert P.load_hf_profiles_into_cache("acc-split", rows, 400) == 1
    cfg = cfg_on(budget={"enabled": True, "minGapSec": 180, "maxPerHour": 8,
                         "maxPerDay": 60}, profile={"applyQuietHours": True})
    eff = HF.resolve_hf_effective_budget("acc-split", "g-split@chatroom", cfg, NOW_SEC)
    assert [tuple(x) for x in eff.quiet_hours] == [(23, 5), (5, 7)], \
        "23–07 必须拆成 23–05 + 05–07，且总时长仍是 8h"


def test_effective_budget_shares_the_outer_ring(monkeypatch):
    """占比外环（observe）也走同一条合成路径。"""
    from plugin.inbound.heartflow import observe as O

    class FakeTighten:
        min_gap_sec, max_per_hour, max_per_day = 1800, 1, 5
        no_consecutive_without_human, enabled = True, True

        def as_dict(self):
            return {"enabled": True, "minGapSec": 1800, "maxPerHour": 1, "maxPerDay": 5,
                    "noConsecutiveWithoutHuman": True}

    monkeypatch.setattr(P, "get_hf_profile_budget", lambda a, g: None)
    monkeypatch.setattr(O, "get_hf_share_tighten", lambda a, g, b, c, t: FakeTighten())
    eff = HF.resolve_hf_effective_budget(
        "acc1", "g1", cfg_on(budget={"enabled": True, "minGapSec": 180, "maxPerHour": 8,
                                     "maxPerDay": 60}), NOW_SEC)
    assert (eff.min_gap_sec, eff.max_per_hour, eff.max_per_day) == (1800, 1, 5)


def test_effective_budget_is_zero_db_io(monkeypatch):
    """judge 热路径零 DB IO（V7）：三条来源全是内存缓存。"""
    def _boom(*a, **kw):
        raise AssertionError("生效预算不得触 DB")

    monkeypatch.setattr(B, "get_budget_state", _boom)
    eff = HF.resolve_hf_effective_budget("acc1", "g1", cfg_on(), NOW_SEC)
    assert eff.min_gap_sec == B.HF_BUDGET_DEFAULTS["minGapSec"]


# ===== 12. maybe_engage 编排 =====


def test_engage_gate_rejection_never_calls_the_judge(monkeypatch):
    got = fake_judge(monkeypatch, [good_json()])
    calls = ledger_recorder(monkeypatch)
    assert asyncio.run(_engage(FakeMsg(), cfg=cfg_on(enabled=False))) is False
    assert asyncio.run(_engage(FakeMsg(), cfg=cfg_on(whitelistGroups=["gOther"]))) is False
    assert asyncio.run(_engage(FakeMsg(content="  "))) is False
    assert got == [] and calls == [], "闸拒 ⇒ 不调 LLM、不落台账"


def test_engage_judge_failure_returns_false_without_ledger(monkeypatch, caplog):
    """judge 没出结果 ⇒ **不落台账**（与"判定未过"区分开）。

    落一行"没过"会让 09-13 那次静默瘫的故事重演：台账看起来在跑，其实一次都没判出来。
    """
    fake_judge(monkeypatch, ["坏", "也坏"])
    calls = ledger_recorder(monkeypatch)
    before = metrics.get_counter(metrics.COUNTER_HF_JUDGE_BELOW)
    with caplog.at_level(logging.WARNING, logger=HF.__name__):
        out = asyncio.run(_engage(FakeMsg()))
    assert out is False and calls == []
    assert metrics.get_counter(metrics.COUNTER_HF_JUDGE_BELOW) == before, \
        "判不出来既不算「过」也不算「不过」"
    # 「没出结果」是一条**正常早退**，不是异常路径。若实现写成"落到下面再炸"，
    # 结果同样是 False + 不落台账，但线上日志会多一条 judge error —— 真正的异常会被淹没。
    assert not any("judge error" in r.getMessage() for r in caplog.records), \
        "judge 返回 None ⇒ 干净早退，不许走异常兜底那条路"


def test_engage_pass_path(monkeypatch):
    fake_judge(monkeypatch, [good_json(reasoning="该接")])
    calls = ledger_recorder(monkeypatch)
    p_before = metrics.get_counter(metrics.COUNTER_HF_JUDGE_PASSED)

    acct = FakeAcct()
    msg = FakeMsg()
    assert asyncio.run(_engage(msg, acct)) is True
    assert metrics.get_counter(metrics.COUNTER_HF_JUDGE_PASSED) == p_before + 1
    assert len(calls) == 1 and calls[0][0] == "judged"
    rec = calls[0][1]
    assert rec.account_id == "acc1" and rec.group_id == "g1@chatroom"
    assert rec.inbound_msg_id == "m100" and rec.new_msg_id == "n100"
    assert rec.from_wxid == "wxid_alice" and rec.msg_type == "text"
    assert rec.content_head == "这批货什么时候到"
    assert rec.judge_overall == 1.0 and rec.dim_r == 10.0 and rec.dim_c == 10.0
    assert abs(rec.effective_threshold - L.HF_REPLY_THRESHOLD_FALLBACK) < 1e-9
    assert rec.energy == 1.0
    # ★ B8（2026-10-02 修）：**判了 ≠ 发过了** —— 这里只登记待交付，回复状态一律不动。
    #   旧断言 ``total_replies == 1`` 钉的正是那个 bug：话还没进群（还要过生成 + 复读闸 +
    #   发送），却已经按"回过"扣精力、推 ``last_reply_time`` ⇒ 冷却闸接下来的
    #   ``minReplyIntervalSec`` 内一律装死（老板看到的就是"该接的话不接"）。
    st = HF._chat_states["g1@chatroom"]
    assert (st.total_replies, st.last_reply_time) == (0, 0), \
        "只判过还没发出 ⇒ 不许记回复状态（冷却不许在这儿起算）"
    assert HF.pending_send_count() == 1
    assert HF._pending["acc1:g1@chatroom"].inbound_msg_id == "m100"
    assert HF.judge_interval_count() == 1, "judge 频率闸要记上"


def test_engage_below_threshold_path(monkeypatch):
    fake_judge(monkeypatch, [good_json(**{k: 1 for k in HF.HF_DIMENSIONS})])
    calls = ledger_recorder(monkeypatch)
    b_before = metrics.get_counter(metrics.COUNTER_HF_JUDGE_BELOW)
    p_before = metrics.get_counter(metrics.COUNTER_HF_JUDGE_PASSED)

    assert asyncio.run(_engage(FakeMsg())) is False
    assert metrics.get_counter(metrics.COUNTER_HF_JUDGE_BELOW) == b_before + 1
    assert metrics.get_counter(metrics.COUNTER_HF_JUDGE_PASSED) == p_before
    assert len(calls) == 1 and calls[0][0] == "below"
    assert abs(calls[0][2] - time.time()) < 60, "落账时刻用**现在**（不是消息的 ts）"
    assert calls[0][1].judge_overall == pytest.approx(0.1)
    assert HF.pending_send_count() == 0, "没过阈值 ⇒ 没有待交付"
    st = HF._chat_states["g1@chatroom"]
    assert (st.total_replies, st.total_messages) == (0, 1), "走了被动分支（精力恢复）"


def test_engage_collapses_whitespace_and_caps_content_head(monkeypatch):
    fake_judge(monkeypatch, [good_json()])
    calls = ledger_recorder(monkeypatch)
    content = "第一行\n\n  第二行\t第三行" + "很长的尾巴" * 100
    asyncio.run(_engage(FakeMsg(content=content)))
    head = calls[0][1].content_head
    assert "\n" not in head and "  " not in head and "\t" not in head
    assert head.startswith("第一行 第二行 第三行")
    assert len(head) == 256


def test_engage_uses_effective_threshold_for_both_decision_and_record(monkeypatch):
    """生效阈值必须**同时**用于"判"和"记" —— 分叉那一刻就是状态页开始撒谎的时刻。"""
    fake_judge(monkeypatch, [good_json(**{k: 7 for k in HF.HF_DIMENSIONS})])
    calls = ledger_recorder(monkeypatch)
    monkeypatch.setattr(L, "resolve_hf_effective_threshold",
                        lambda a, g, c, now=None: L.HfEffectiveThreshold(
                            threshold=0.9, base=0.6, override=None, band=0.9,
                            band_capped=False, reach_cap=None, reach_info=None))
    assert asyncio.run(_engage(FakeMsg())) is False, "0.7 < 0.9 ⇒ 不该接管"
    rec = calls[0][1]
    assert abs(rec.effective_threshold - 0.9) < 1e-9, "台账记「生效」阈值（不是账号级 0.6）"


def test_engage_judge_receives_the_effective_threshold_config(monkeypatch):
    """生效阈值只进「判定比较」，**不进 prompt**（I1）—— 两条断言一起钉住这件事。"""
    got = fake_judge(monkeypatch, [good_json(**{k: 7 for k in HF.HF_DIMENSIONS})])
    ledger_recorder(monkeypatch)
    monkeypatch.setattr(L, "resolve_hf_effective_threshold",
                        lambda a, g, c, now=None: L.HfEffectiveThreshold(
                            threshold=0.9, base=0.6, override=None, band=0.9,
                            band_capped=False, reach_cap=None, reach_info=None))
    assert asyncio.run(_engage(FakeMsg())) is False, "0.7 分 vs 生效阈值 0.9"
    assert "0.9" not in got[0]["user_prompt"] and "阈值" not in got[0]["user_prompt"]


def test_engage_warns_when_the_profile_band_is_capped(monkeypatch, caplog):
    fake_judge(monkeypatch, [good_json()])
    ledger_recorder(monkeypatch)
    monkeypatch.setattr(L, "resolve_hf_effective_threshold",
                        lambda a, g, c, now=None: L.HfEffectiveThreshold(
                            threshold=0.7, base=0.6, override=None, band=0.9,
                            band_capped=True, reach_cap=None, reach_info=None))
    with caplog.at_level(logging.WARNING, logger=HF.__name__):
        asyncio.run(_engage(FakeMsg()))
    assert any("band capped" in r.getMessage() for r in caplog.records), \
        "画像想要 0.9 只生效 0.7 —— 这件事必须有人看得见"


def test_engage_marks_judged_even_when_the_judge_fails(monkeypatch):
    """频率闸管的是"别把 LLM 打爆"，与判定成败无关：只在通过时标记 ⇒ 一个持续坏 JSON
    的群会**每条消息都调一次**。"""
    fake_judge(monkeypatch, ["坏", "也坏"])
    ledger_recorder(monkeypatch)
    asyncio.run(_engage(FakeMsg()))
    assert HF.judge_interval_count() == 1


def test_engage_exception_is_swallowed_with_a_warning(monkeypatch, caplog):
    """心流坏了不该让这条消息整体失败（外层还会走主 LLM 路径）。"""
    ledger_recorder(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("编排炸了")

    monkeypatch.setattr(HF, "resolve_hf_effective_budget", _boom)
    with caplog.at_level(logging.WARNING, logger=HF.__name__):
        assert asyncio.run(_engage(FakeMsg())) is False
    assert any("judge error" in r.getMessage() for r in caplog.records)


def test_engage_disabled_account_is_a_noop(monkeypatch):
    got = fake_judge(monkeypatch, [good_json()])
    ledger_recorder(monkeypatch)
    assert asyncio.run(_engage(FakeMsg(), cfg={})) is False
    # 账号完全没有 heartflow 字段（不是 {}，是根本没这个 key）
    bare = FakeAcct()
    bare.heartflow = None
    assert asyncio.run(HF.maybe_engage(FakeAdapter(), bare, FakeMsg())) is False
    assert got == [], "关着的心流连 LLM 都不该调"


def test_engage_passes_the_candidates_own_timestamp_to_the_budget_gate(monkeypatch):
    """``candidate_at_sec`` 必须取自**消息自带的时间戳**（不是"现在"）。

    取错字段名（``msg.ts`` vs ``msg.timestamp``）的表现是"永远 None" ⇒ 预算的
    陈旧触发防重放静默失效 —— 而测试如果用的是自己编的假消息，它一样全绿。
    """
    got = fake_judge(monkeypatch, [good_json()])
    ledger_recorder(monkeypatch)
    seen: list = []
    real = HF.check_gate

    def _spy(chat_id, content, cfg, now_ms, account_id=None, candidate_at_sec=None,
             budget_override=None):
        seen.append(candidate_at_sec)
        return real(chat_id, content, cfg, now_ms, account_id, candidate_at_sec,
                    budget_override)

    monkeypatch.setattr(HF, "check_gate", _spy)
    assert asyncio.run(_engage(FakeMsg(timestamp=NOW_SEC - 30))) is True
    assert seen == [NOW_SEC - 30], "传的是消息自己的时刻，不是 now"
    assert got, "闸放行了才谈得上判定"


def test_engage_feeds_prompt_from_state_buffer_and_profile(monkeypatch):
    """编排把四样东西真的接上了：群画像 / 上次回复 / 上下文 / 精力。"""
    got = fake_judge(monkeypatch, [good_json()])
    ledger_recorder(monkeypatch)
    HF.record_raw_message("g1@chatroom",
                          HF.HfRawMessage("小助理", "", "上次我说的", 1.0, is_bot=True))
    HF.record_raw_message("g1@chatroom", HF.HfRawMessage("阿丽", "", "历史消息", 2.0))
    monkeypatch.setattr(P, "get_hf_profile_prompt_text", lambda a, g: "售后群，爱聊价格")
    asyncio.run(_engage(FakeMsg()))
    p = got[0]["user_prompt"]
    assert "售后群，爱聊价格" in p
    assert "上次我说的" in p and "历史消息" in p
    assert "我是 小助理" in p, "账号昵称进 prompt（不是空着）"


# ===== 13. 交付缝 =====


def test_pre_send_is_none_for_non_heartflow_traffic():
    """普通回复（没有待交付登记）⇒ ``None`` = 照常发，一个字都不改。"""
    assert HF.pre_send("acc1", "g1", "随便一句话", NOW_SEC) is None


def test_pre_send_passes_when_not_repeated(monkeypatch):
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    plan = HF.pre_send("acc1", "g1", "今天下午三点到货", NOW_SEC)
    assert plan is not None and plan.placeholder is None
    assert HF.pending_send_count() == 1, "放行 ⇒ 登记保留（等 note_sent 来消费）"


def test_pre_send_suppresses_a_repeat_and_settles_the_ledger(monkeypatch):
    """I5：重复闸拦下的**不能**占预算 / 开窗 / 进历史。

    而且必须落账（``suppressed/repeat``）：否则那行会一直是 ``judged``，被 sweep 当
    "没人接话"（负样本）—— 而它其实**根本没发出去**。
    """
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    calls = ledger_recorder(monkeypatch)
    B.reset_budget_cache()
    D.reset_dedupe_store()
    dcfg = D.resolve_dedupe_cfg({})
    D.note_recent_reply("acc1", "g1", "今天下午三点到货一批新机", NOW_SEC - 600, dcfg)
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)

    plan = HF.pre_send("acc1", "g1", "今天下午三点到货一批新机", NOW_SEC)
    assert plan is not None and plan.placeholder == HF.HF_REPEAT_PLACEHOLDER == "repeat-suppressed"
    assert plan.verdict is not None
    assert HF.pending_send_count() == 0, "拦下 ⇒ 登记被消费（否则过期前会一直挂着）"
    assert len(calls) == 1 and calls[0][0] == "send"
    args, kw = calls[0][1], calls[0][2]
    assert args[0] == "acc1" and args[1] == "m1"
    assert args[2].ok is True and args[2].msg_id == HF.HF_REPEAT_PLACEHOLDER
    assert kw["label_window_sec"] == 0 and kw["observe_sec"] == 0, \
        "占位路径不许开窗（0/0 ⇒ learn 侧判 suppressed，不占额度不开窗）"
    # ★ B8：静默不发 ⇒ 群里一个字都没有 ⇒ 回复状态一个都不许动（适配器拿到占位符就
    #   直接返回了，压根不会调 note_sent；这条钉的是"别有人在交付缝顺手补记一笔"）。
    assert HF.seconds_since_last_reply("g1", NOW_MS + 1_000) == 0.0, \
        "静默不发 ⇒ 冷却不许起算（老板看到的「装死」就是这个）"


def test_pre_send_respects_dedupe_off(monkeypatch):
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on(dedupe={"enabled": False}))
    B.reset_budget_cache()
    D.reset_dedupe_store()
    D.note_recent_reply("acc1", "g1", "今天下午三点到货一批新机", NOW_SEC - 60,
                        D.resolve_dedupe_cfg({}))
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    plan = HF.pre_send("acc1", "g1", "今天下午三点到货一批新机", NOW_SEC)
    assert plan is not None and plan.placeholder is None, "关掉重复闸 ⇒ 放行"


def test_pre_send_ignores_an_expired_registration(monkeypatch):
    """B2：**过期登记不是心流交付** —— 不能继续拿重复闸去卡该群后面每一条出站。

    修前 `pre_send` 只 `.get()` 不看 TTL（只有 `take_pending_send` 看），于是"判定通过
    但 agent 最终没产出回复"的那条登记会一直挂着：老板 @ 之后的正常回复也会被当心流插话
    过闸，命中就静默丢弃（还返回 success=True）—— 日志干净、消息没发。
    """
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    ledger_recorder(monkeypatch)
    B.reset_budget_cache()
    D.reset_dedupe_store()
    dcfg = D.resolve_dedupe_cfg({})
    D.note_recent_reply("acc1", "g1", "今天下午三点到货一批新机", NOW_SEC - 600, dcfg)
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)

    # 对照：同一文案在 TTL 内**会**被拦（证明这条测试有咬合力，不是恒真）
    assert HF.pre_send("acc1", "g1", "今天下午三点到货一批新机", NOW_SEC + 119) is not None

    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    assert HF.pre_send("acc1", "g1", "今天下午三点到货一批新机", NOW_SEC + 121) is None, \
        "过期（>120s）⇒ 按普通发送放行，一个字都不改"
    # B6（2026-10-02）改了这里：闸门过期**不再顺手删登记**。删了就没人给慢模型那次
    # 出站归账（它真发出去了，却被记成"没发生"）。登记留着等 ``note_sent`` 用宽时钟
    # 去认；每群至多一条，不会堆积。
    assert HF.pending_send_count() == 1, "过期只是不再拦人，登记要留给归账（B6）"


def test_pre_send_ignores_a_registration_after_a_bot_mention(monkeypatch):
    """B3：登记之后有人 @/引用机器人 ⇒ 这批出站回的是**那一条**，不是心流插话。

    这条正是"群里 @ 机器人它不回"的直接成因：@ 之后框架产出的回复被当成心流插话，
    命中重复闸 ⇒ 静默丢弃。
    """
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    ledger_recorder(monkeypatch)
    B.reset_budget_cache()
    D.reset_dedupe_store()
    dcfg = D.resolve_dedupe_cfg({})
    D.note_recent_reply("acc1", "g1", "好的老板，马上处理", NOW_SEC - 600, dcfg)
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)

    # 对照：没有 @ 时，同样这句**会**被重复闸拦下
    assert HF.pre_send("acc1", "g1", "好的老板，马上处理", NOW_SEC + 5) is not None

    HF.note_bot_mention("acc1", "g1")
    plan = HF.pre_send("acc1", "g1", "好的老板，马上处理", NOW_SEC + 6)
    assert plan is None, "@ 之后的出站必须放行（重复闸不适用）"
    assert HF.pending_send_count() == 1, "登记**留着**：真正那条心流插话可能还在生成中"


def test_note_bot_mention_is_keyed_by_account_and_group(monkeypatch):
    """标记只作用于**那个群**：多账号 / 多群共享同一进程，串台就是别的群漏闸。"""
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    HF.begin_pending_send("acc1", "g2", "m2", NOW_SEC)
    HF.begin_pending_send("acc2", "g1", "m3", NOW_SEC)
    HF.note_bot_mention("acc1", "g1")
    assert HF._pending["acc1:g1"].newer_mention_at_sec is not None
    assert HF._pending["acc1:g2"].newer_mention_at_sec is None
    assert HF._pending["acc2:g1"].newer_mention_at_sec is None, "账号维度也要隔离"


def test_a_new_registration_clears_a_previous_mention_flag(monkeypatch):
    """@ **自己**触发了心流接管时不能自我否决。

    时序：`_handle_one` 的 ④a 旁路观测（打标记）排在 ⑤ 交给 runner 之前，而
    `begin_pending_send` 在 ④a 之后 ⇒ 标记落在**旧**登记上，随即被整条替换掉。
    """
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    HF.note_bot_mention("acc1", "g1")
    HF.begin_pending_send("acc1", "g1", "m9", NOW_SEC + 1)
    plan = HF.pre_send("acc1", "g1", "一句全新的话", NOW_SEC + 2)
    assert plan is not None and plan.placeholder is None


def test_note_group_message_flags_a_mention_for_the_delivery_seam(monkeypatch):
    """接线：适配器那条旁路（`_hf_note_group`）走的就是 `note_group_message`。

    少了这条，B3 的判据在真机上永远不置位 —— 单元测试全绿而故障照旧。
    """
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    HF.reset_heartflow_cache()
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    HF.note_group_message("acc1", "g1", sender_name="老板", content="@机器人 在吗",
                          ts_sec=NOW_SEC + 3, mentions_bot=True)
    assert HF._pending["acc1:g1"].newer_mention_at_sec is not None

    # 反例：同一群里只是闲聊 ⇒ **不**置位（否则闲聊群里去重闸整体失效，
    # 而那条闸正是老板 2026-09-26 拍板的"别换个日期说同一段话"）
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    HF.note_group_message("acc1", "g1", sender_name="路人", content="今天天气不错",
                          ts_sec=NOW_SEC + 3)
    assert HF._pending["acc1:g1"].newer_mention_at_sec is None

    # 引用机器人同样算"在跟机器人说话"（触发判定第 ⑨ 路 quote_bot 会产出回复）
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    HF.note_group_message("acc1", "g1", sender_name="路人", content="[引用]",
                          ts_sec=NOW_SEC + 3, quotes_bot=True)
    assert HF._pending["acc1:g1"].newer_mention_at_sec is not None


def test_note_sent_translates_platform_send_result(monkeypatch):
    """**翻译**是必需的一步：``learn.classify_hf_send`` 只认 ``ok``/``msg_id``，直接塞
    ``success``/``message_id`` 会被判 pending ⇒ 消息发出去了但账本永远 pending
    （不占预算、不开窗、无日志）。"""
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    calls = ledger_recorder(monkeypatch)
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    HF.note_sent("acc1", "g1", "今天下午三点到货", success=True,
                 message_id="848526155-179", now_sec=NOW_SEC)
    assert len(calls) == 1
    args, kw = calls[0][1], calls[0][2]
    assert args[0] == "acc1" and args[1] == "m1"
    sent = args[2]
    assert isinstance(sent, L.HfSendResult) and sent.ok is True
    assert sent.msg_id == "848526155-179"
    assert L.classify_hf_send(sent) == L.HF_SEND_OUTCOME_SENT, "翻译后必须是 sent（不是 pending）"
    assert kw["group_id"] == "g1" and kw["text"] == "今天下午三点到货"
    assert kw["observe_sec"] == L.HF_LEARNING_DEFAULTS["observeWindowSec"]
    assert kw["label_window_sec"] == L.HF_LEARNING_DEFAULTS["labelWindowSec"]
    assert isinstance(kw["dedupe_cfg"], D.HfDedupeConfig)


def test_note_sent_failure_keeps_the_pending_for_a_retry(monkeypatch, caplog):
    """发送**没成功** ⇒ 账本不动、登记**留着**（``pending`` 的语义就是"等重试"）。

    顺手把登记吃掉的话，重试那次匹配不上登记 ⇒ 台账那行永远停在 ``judged``，
    被 sweep 的呆账收敛当成"没人接话"—— 而这句话群里**根本没出现过**。
    """
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    calls = ledger_recorder(monkeypatch)
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    with caplog.at_level(logging.WARNING, logger=HF.__name__):
        HF.note_sent("acc1", "g1", "文本", success=False, message_id=None, now_sec=NOW_SEC)
    assert calls == [], "pending ⇒ learn 侧本来就什么都不改（不该白跑一趟 DB）"
    assert HF.pending_send_count() == 1, "登记必须留着（否则重试落不了账）"
    assert any("发送失败" in r.getMessage() for r in caplog.records), "要吵一声，不许静默"

    # 重试成功 ⇒ 这一次才真落账
    HF.note_sent("acc1", "g1", "文本", success=True, message_id="v9", now_sec=NOW_SEC + 5)
    assert len(calls) == 1 and calls[0][0] == "send"
    assert HF.pending_send_count() == 0


def test_note_sent_of_an_ordinary_failed_send_stays_silent(monkeypatch, caplog):
    """**没有**待交付登记时，一次失败的主 LLM 回复不该吵（那是日常，不是心流事故）。"""
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    calls = ledger_recorder(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=HF.__name__):
        HF.note_sent("acc1", "g1", "主回复", success=False, message_id=None, now_sec=NOW_SEC)
    assert calls == []
    assert not any("发送失败" in r.getMessage() for r in caplog.records)


def test_note_sent_without_a_pending_is_a_noop(monkeypatch):
    calls = ledger_recorder(monkeypatch)
    HF.note_sent("acc1", "g1", "普通回复", success=True, message_id="x", now_sec=NOW_SEC)
    assert calls == [], "不是心流交付 ⇒ 不落账（否则主 LLM 的每条回复都会污染台账）"


# ===== 13b. 回复状态只认「真发出去」（B8，2026-10-02 修）=====
#
# 旧写法在 judge 通过时就记 ``last_reply_time``（TS ``handler.ts:827`` 同形）。可判过之后
# 这条回复还要过生成与交付缝两道，两道都能让它**进不了群**：复读闸静默不发、发送失败等重试。
# 而冷却闸（``check_gate`` 的 ``cooling``）只认 ``last_reply_time`` ⇒ "没发出去的那次"
# 照样把接下来 ``minReplyIntervalSec`` 的窗口占掉，群里再喊它也不应 ——
# 老板看到的正是"明明该接的话它装死"。


def test_reply_state_is_recorded_only_after_the_send_lands(monkeypatch):
    """★ B8 正题：闭环走一遍 —— judge 通过不动状态，真发出之后才动（冷却这才起算）。"""
    fake_judge(monkeypatch, [good_json(reasoning="该接")])
    ledger_recorder(monkeypatch)
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on(minReplyIntervalSec=300))
    msg = FakeMsg()
    assert asyncio.run(_engage(msg)) is True
    st = HF._chat_states[msg.chatroom_id]
    assert (st.total_replies, st.last_reply_time, st.total_messages) == (0, 0, 0), \
        "判过而已：话还没进群，状态一律不动"

    sent_at = int(time.time())
    HF.note_sent("acc1", msg.chatroom_id, "今天下午三点到货", success=True,
                 message_id="848526155-179", now_sec=sent_at)
    st = HF._chat_states[msg.chatroom_id]
    assert (st.total_replies, st.total_messages) == (1, 1), "一条真发出的回复只算一次"
    assert st.last_reply_time == sent_at * 1000
    assert st.energy < 1.0, "真发出去才算消耗"
    g = HF.check_gate(msg.chatroom_id, "有人吗", cfg_on(minReplyIntervalSec=300),
                      sent_at * 1000 + 5_000)
    assert (g.allowed, g.reason) == (False, "cooling"), "冷却只该在真发出之后起算"


def test_a_failed_send_does_not_start_the_cooldown(monkeypatch):
    """发送失败（等重试）⇒ 状态不动 —— 否则重试成功之前那段窗口里群里喊它也不应。

    这里直接用 :func:`seconds_since_last_reply` 断言：它就是 ``cooling`` 闸读的那个数
    （``0`` = 从未回复）。
    """
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on(minReplyIntervalSec=300))
    ledger_recorder(monkeypatch)
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    HF.note_sent("acc1", "g1", "文本", success=False, message_id=None, now_sec=NOW_SEC)
    assert HF.pending_send_count() == 1, "失败 ⇒ 登记留着等重试"
    assert HF.seconds_since_last_reply("g1", NOW_MS) == 0.0, \
        "没发出去 ⇒ 冷却不许起算（0 = 从未回复）"

    HF.note_sent("acc1", "g1", "文本", success=True, message_id="v9", now_sec=NOW_SEC + 5)
    assert HF.seconds_since_last_reply("g1", NOW_MS + 6_000) == 1.0, \
        "重试真发出去的那次才起算"
    assert HF.pending_send_count() == 0


@pytest.mark.parametrize("dropped", list(L.HF_PLACEHOLDER_MSG_IDS))
def test_a_framework_dropped_send_does_not_start_the_cooldown(monkeypatch, dropped):
    """账本判 ``suppressed`` = 框架把它丢了 ⇒ 群里没有这句话 ⇒ 一样不许起算冷却。

    （``msg_id=""`` 到不了这里：``note_sent`` 的 ``message_id or None`` 会把它归一成
    ``None`` = "vendor 没回 id"，那是 **sent** —— 与"框架丢了"是两回事。）
    """
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on(minReplyIntervalSec=300))
    calls = ledger_recorder(monkeypatch)
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    HF.note_sent("acc1", "g1", "文本", success=True, message_id=dropped, now_sec=NOW_SEC)
    assert calls and calls[0][0] == "send", "账本照样收敛（那行不许停在 judged）"
    assert calls[0][1][2].msg_id == dropped, "落的是 suppressed，占位符原样带上"
    assert HF.seconds_since_last_reply("g1", NOW_MS + 1_000) == 0.0, \
        "框架丢了 ⇒ 没进群 ⇒ 冷却不许起算"


def test_reply_state_is_written_in_exactly_one_place_and_only_when_sent():
    """★ B8 源级：``record_active_reply`` 只许出现在 ``note_sent``，且只在账本判成 sent 时。

    行为级那两条管"这一次对不对"；这条管"有人把 ``handler.ts:827`` 那行搬回来" ——
    群状态是内存里的，下一个用例一清就看不出差别，那种回归会一路溜到生产
    （表现是生产上"该接的话不接"，本地全绿）。
    """
    assert "record_active_reply" not in _fn_code("maybe_engage"), \
        "judge 通过不许记回复状态（B8：判了 ≠ 发过了）"
    tree = ast.parse(HF_SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "note_sent")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "record_active_reply"]
    assert len(calls) == 1, f"记回复状态只该有一处（实际 {len(calls)}）"
    guard = next(ast.unparse(n.test) for n in ast.walk(fn)
                 if isinstance(n, ast.If) and any(c is calls[0] for c in ast.walk(n)))
    assert guard == "outcome == L.HF_SEND_OUTCOME_SENT", \
        f"守卫必须是「账本判成 sent」（实际 {guard}）—— suppressed = 框架把它丢了，一样没进群"


def test_pending_expires_after_ttl(monkeypatch):
    """两个时钟都是**写死的具体数字**，不是"某个常数"。

    写死字面量是刻意的：用 ``HF.HF_PENDING_SEND_TTL_SEC`` 写断言，常数被改大多少
    测试就跟着放水多少 —— 而这两条守卫的全部意义就在于"agent 卡了一下之后，
    下一条无关发言不许被算成心流交付"（闸门）与"卡太久之后的交付不许被算成没发生"
    （归账）。

    B6（2026-10-02）把**拦人**与**归账**分成两个时钟：120s / 600s。混成一个数的
    两个方向都坏 —— 取 120 则慢模型那次真发出的回复不记账（B6 的原始症状）；
    取 600 则 agent 挂掉后 10 分钟内所有无关出站都被重复闸当心流插话去卡（B2 回潮）。
    """
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    assert HF.HF_PENDING_SEND_TTL_SEC == 120
    assert HF.HF_PENDING_SEND_ABSORB_SEC == 600
    calls = ledger_recorder(monkeypatch)
    # ① 闸门时钟：>120s 不再拿重复闸卡该群出站（B2），但登记**留着**
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    assert HF.pre_send("acc1", "g1", "一句会被重复闸盯上的话", NOW_SEC + 121) is None
    assert HF.pending_send_count() == 1, "闸门过期只是不再拦人，登记留给归账（B6）"
    # ② 归账时钟：慢模型 200s 才把回复发出去，仍然**算数**（B6 的正题）
    HF.note_sent("acc1", "g1", "慢吞吞但真发出去了", success=True, message_id="x",
                 now_sec=NOW_SEC + 200)
    assert len(calls) == 1, "真发出去的回复不许因为「那次生成慢」就被记成没发生"
    assert calls[0][1][1] == "m1", "归到那条判定行上"
    assert HF.pending_send_count() == 0, "归账即消费掉"
    # ③ 超过归账时钟 ⇒ 丢弃（agent 挂了以后的下一条无关发言不许被算成心流交付）
    HF.begin_pending_send("acc1", "g1", "m2", NOW_SEC)
    HF.note_sent("acc1", "g1", "太晚了", success=True, message_id="y",
                 now_sec=NOW_SEC + 601)
    assert len(calls) == 1, ">600s 的交付尝试不再归账（对准哪条行已经不可信）"
    assert HF.pending_send_count() == 0
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    assert HF.take_pending_send("acc1", "g1", NOW_SEC + 119) is not None


def test_pending_is_consumed_once(monkeypatch):
    """一次心流触发 = 一次交付（对齐 TS 的 batch 预算守卫，见模块 docstring 的分歧说明）。"""
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    calls = ledger_recorder(monkeypatch)
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    HF.note_sent("acc1", "g1", "第一条", success=True, message_id="a", now_sec=NOW_SEC)
    HF.note_sent("acc1", "g1", "第二条", success=True, message_id="b", now_sec=NOW_SEC)
    assert len(calls) == 1


def test_pending_is_per_group():
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    HF.begin_pending_send("acc1", "g2", "m2", NOW_SEC)
    assert HF.pending_send_count() == 2
    p = HF.take_pending_send("acc1", "g1", NOW_SEC)
    assert p is not None and p.inbound_msg_id == "m1"
    assert HF.pending_send_count() == 1
    assert HF.take_pending_send("acc1", "g2", NOW_SEC).inbound_msg_id == "m2"


def test_pending_replaces_the_previous_one_for_the_same_group():
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    HF.begin_pending_send("acc1", "g1", "m2", NOW_SEC + 10)
    assert HF.pending_send_count() == 1
    assert HF.take_pending_send("acc1", "g1", NOW_SEC + 11).inbound_msg_id == "m2"


# ===== 14. 旁路 note_group_message =====


def test_note_group_message_records_raw_when_enabled(monkeypatch):
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    HF.note_group_message("acc1", "g1", sender_name="阿丽", content="在吗", ts_sec=NOW_SEC)
    buf = HF.get_raw_buffer("g1", 5)
    assert len(buf) == 1 and buf[0].content == "在吗" and buf[0].is_bot is False


def test_note_group_message_skips_disabled_accounts(monkeypatch):
    monkeypatch.setattr(HF, "_cfg_for", lambda a: {})
    HF.note_group_message("acc1", "g1", sender_name="阿丽", content="在吗", ts_sec=NOW_SEC)
    assert HF.raw_buffer_count() == 0, "心流关着 ⇒ 不占内存缓冲"


def test_note_group_message_bot_messages_do_not_count_as_human(monkeypatch):
    """bot 自己那条既不记"有人说话"（否则沉默时长指标被自己刷新）也不记接话。"""
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    hits: list = []
    monkeypatch.setattr(B, "note_human_message",
                        lambda a, g, t: hits.append(("human", a, g, t)))
    monkeypatch.setattr(L, "mark_hf_group_engaged",
                        lambda *a, **kw: hits.append(("engaged", a)))
    HF.note_group_message("acc1", "g1", sender_name="小助理", content="我说", ts_sec=NOW_SEC,
                          is_bot=True)
    assert hits == [] and len(HF.get_raw_buffer("g1", 5)) == 1, "原始缓冲仍然要进（judge 要看）"


def test_note_group_message_notes_human_only_for_whitelisted_groups(monkeypatch):
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on(whitelistGroups=["gIn"]))
    hits: list = []
    monkeypatch.setattr(B, "note_human_message", lambda a, g, t: hits.append((a, g, t)))
    HF.note_group_message("acc1", "gOut", sender_name="阿丽", content="x", ts_sec=NOW_SEC,
                          now_sec=NOW_SEC)
    assert hits == []
    HF.note_group_message("acc1", "gIn", sender_name="阿丽", content="x", ts_sec=NOW_SEC,
                          now_sec=NOW_SEC)
    assert hits == [("acc1", "gIn", NOW_SEC)]


def test_note_group_message_feeds_candidates_only_when_a_window_is_open(monkeypatch):
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    seen: list = []
    monkeypatch.setattr(L, "mark_hf_group_engaged",
                        lambda a, g, t, cands: seen.append((a, g, t, list(cands))))
    monkeypatch.setattr(L, "get_open_hf_window", lambda a, g: None)
    HF.note_group_message("acc1", "g1", sender_name="阿丽", content="x", ts_sec=NOW_SEC,
                          now_sec=NOW_SEC)
    assert seen == [], "没开窗（绝大多数情况）⇒ 零额外开销"

    monkeypatch.setattr(L, "get_open_hf_window", lambda a, g: object())
    HF.note_group_message("acc1", "g1", sender_name="阿丽", content="在吗", ts_sec=NOW_SEC,
                          quotes_bot=True, mentions_bot=True, now_sec=NOW_SEC)
    assert len(seen) == 1
    a, g, t, cands = seen[0]
    assert (a, g, t) == ("acc1", "g1", NOW_SEC)
    assert len(cands) == 1
    # 引用 / @ 是 label 侧判"强信号"的**唯一**依据，丢了就等于把强信号降级成弱信号。
    assert (cands[0].quotes_bot, cands[0].mentions_bot) == (True, True)


def test_note_group_message_candidate_shape(monkeypatch):
    from plugin.inbound.heartflow import label as LABEL

    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    seen: list = []
    monkeypatch.setattr(L, "get_open_hf_window", lambda a, g: object())
    monkeypatch.setattr(L, "mark_hf_group_engaged",
                        lambda a, g, t, cands: seen.append(list(cands)))
    HF.note_group_message("acc1", "g1", sender_name="阿丽", content="在吗",
                          ts_sec=NOW_SEC - 5, quotes_bot=True, mentions_bot=False,
                          now_sec=NOW_SEC)
    assert len(seen) == 1 and len(seen[0]) == 1
    c = seen[0][0]
    assert isinstance(c, LABEL.HfEngageCandidate)
    # 候选的 at_sec 是**消息自己的时刻**（NOW_SEC-5），不是"现在" —— 窗内判优先级要用它
    assert (c.at_sec, c.text, c.quotes_bot, c.mentions_bot) == (NOW_SEC - 5, "在吗", True, False)


def test_note_group_message_empty_sender_gets_a_placeholder(monkeypatch):
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    HF.note_group_message("acc1", "g1", sender_name="", content="x", ts_sec=NOW_SEC)
    assert HF.get_raw_buffer("g1", 1)[0].sender_name == "未知"


# ===== 14b. 出站记录 bot 自己的发言（TS recordHeartflowBotReply）=====
#
# 为什么这条接在**出站**而不是入站的自消息分支：vendor 回推的 self 消息在第 ① 步就被
# 适配器丢了（TS 的 verdict 同样是 ``blocked``）。两边都记 ⇒ 缓冲里同一句话两份。


def test_note_bot_reply_feeds_the_buffer_and_last_bot_reply(monkeypatch):
    """judge 判「接不接话」要看「我上次说了什么」（I6），这份数据只有出站路径能给。"""
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    HF.note_bot_reply("acc1", "g1@chatroom", "要的，明天到", ts_sec=NOW_SEC)
    buf = HF.get_raw_buffer("g1@chatroom", 5)
    assert len(buf) == 1
    assert buf[0].is_bot is True and buf[0].content == "要的，明天到"
    assert HF.last_bot_reply("g1@chatroom") == "要的，明天到"


def test_note_bot_reply_keeps_the_caller_timestamp(monkeypatch):
    """入参时间戳必须原样落缓冲，**不许改取``time.time()``**。

    这一条是给上游留的老账：适配器把 ``msg.timestamp`` 传进来，是为了让
    "我上次回复"在**补记/重放**时仍停在真实时刻。若这里偷懒取"现在"，
    一条五分钟前的旧回复会被判成刚刚说过的话 —— 冷却/静默段据此误判，
    而缓冲条数与文案都对，光看内容断言永远绿。
    """
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    old = NOW_SEC - 600
    HF.note_bot_reply("acc1", "g1@chatroom", "十分钟前那句", ts_sec=old)
    buf = HF.get_raw_buffer("g1@chatroom", 5)
    assert buf[0].timestamp == float(old)
    assert buf[0].timestamp != float(NOW_SEC)


def test_note_bot_reply_ignores_dms(monkeypatch):
    """私聊不进心流缓冲 —— TS 同款判据（``toWxid`` 必须含 ``@chatroom``）。"""
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    HF.note_bot_reply("acc1", "wxid_alice", "私聊里说的话")
    assert HF.raw_buffer_count() == 0


def test_note_bot_reply_skips_disabled_accounts(monkeypatch):
    monkeypatch.setattr(HF, "_cfg_for", lambda a: {})
    HF.note_bot_reply("acc1", "g1@chatroom", "x")
    assert HF.raw_buffer_count() == 0


def test_note_bot_reply_does_not_settle_the_delivery_ledger(monkeypatch):
    """「记缓冲」与「结算交付」是两件事：``note_bot_reply`` 只写缓冲。

    适配器里它排在 ``note_sent`` 前一步 —— 两者若被合并（"反正都要记这一条"），
    待交付登记会在写缓冲时就被消费掉，交付缝从此拿不到真结果（账本永远 pending）。
    """
    monkeypatch.setattr(HF, "_cfg_for", lambda a: cfg_on())
    calls = ledger_recorder(monkeypatch)
    HF.begin_pending_send("acc1", "g1@chatroom", "m1", NOW_SEC)
    HF.note_bot_reply("acc1", "g1@chatroom", "回复内容", ts_sec=NOW_SEC)
    assert calls == [] and HF.pending_send_count() == 1


# ===== 15. 缓存登记 =====


def test_registered_in_the_cache_registry():
    assert "heartflow" in CACHE_REGISTRY.names()


def test_reset_clears_all_four_stores():
    HF.get_chat_state("g1", cfg_on(), NOW_MS)
    HF.record_raw_message("g1", HF.HfRawMessage("u", "", "x", 1.0))
    HF.mark_heartflow_judged("g1", NOW_MS)
    HF.begin_pending_send("acc1", "g1", "m1", NOW_SEC)
    assert (HF.chat_state_count(), HF.raw_buffer_count(), HF.judge_interval_count(),
            HF.pending_send_count()) == (1, 1, 1, 1)

    HF.reset_heartflow_cache()
    assert (HF.chat_state_count(), HF.raw_buffer_count(), HF.judge_interval_count(),
            HF.pending_send_count()) == (0, 0, 0, 0), \
        "四者一起清（只清一半 = 测试间互相污染，看起来像业务 bug）"

    CACHE_REGISTRY.reset_all()
    assert HF.pending_send_count() == 0


def test_reset_is_registered_under_our_own_name_not_someone_elses():
    """登记名必须唯一：重名会让 ``reset_all`` 只跑其中一个（另一半静默不清）。

    完整名单（七处）钉在 ``test_runtime.py::KNOWN_CACHES`` —— 登记表的老家在那里
    （``CACHE_REGISTRY`` 就定义在 ``runtime.py``）。这里只验"我们这一个在、且无重名"：
    同一份名单写两处，就又多了一处会漂的地方。
    """
    names = sorted(CACHE_REGISTRY.names())
    assert len(names) == len(set(names))
    assert "heartflow" in names, "本模块的缓存必须登记（否则它的清空永远不会被 reset_all 调到）"


# ===== 16. 源级守卫 =====


def test_module_level_imports_have_no_third_party_dependency():
    """F5：本模块被 ``adapter`` 延迟导入；模块级第三方依赖会让"缺依赖"变成静默坏行为。"""
    tree = ast.parse(HF_SRC)
    top: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top.append(("." * node.level) + (node.module or ""))
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for name in top:
        assert name.startswith(".") or name.split(".")[0] in stdlib, f"模块级第三方依赖: {name}"
    for lazy in ("from ...llm import judge", "from ...core import metrics",
                 "from ...core import accounts"):
        assert lazy in HF_SRC, f"LLM / 埋点 / 账号仍是延迟导入: {lazy}"


def _fn_code(fname: str) -> str:
    """取函数体源码（去 docstring）—— docstring 里写满了反例，搜不得。"""
    tree = ast.parse(HF_SRC)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fname:
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"源码里找不到函数 {fname}")


def test_build_prompt_has_no_threshold_anywhere_in_its_body():
    """**INVARIANT I1**（源级）：prompt 构造体里不许出现阈值 —— 连"读配置"都不许。

    读一下 ``cfg["replyThreshold"]`` 再格式化进文本，就是 09-26 那次自锁停摆的原样。
    """
    body = _fn_code("build_prompt")
    assert body.strip()
    for banned in ("replyThreshold", "resolve_reply_threshold", "阈值", "0.6",
                   "HF_REPLY_THRESHOLD_FALLBACK"):
        assert banned not in body, f"build_prompt 体内不得出现 {banned!r}"


@pytest.mark.parametrize("fname", ["check_gate", "parse_response", "is_hf_group_allowed",
                                   "resolve_hf_effective_budget", "get_chat_state"])
def test_hot_path_functions_have_no_import_or_sql(fname: str):
    """judge 热路径零 DB IO（V7）。"""
    body = _fn_code(fname)
    assert body.strip(), f"{fname} 的函数体被切空了（这条断言会假绿）"
    for banned in ("import ", "SELECT", "get_conn", "await ", "repo_", "executemany"):
        assert banned not in body, f"{fname} 体内不得出现 {banned!r}"


def test_delivery_seam_builds_a_learn_send_result_explicitly():
    """源级：``note_sent`` 必须显式构造 ``HfSendResult``。

    这是本模块**最容易静默坏**的一处：直接传 ``platform.SendResult`` 不报错、不告警，
    只是账本永远 pending（不占预算 / 不开窗 / 无学习样本）。
    """
    body = _fn_code("note_sent")
    assert "HfSendResult(" in body, "必须显式翻译（不是塞 platform 的 SendResult）"
    assert "message_id" in body, "从 platform 形状里取 message_id（再喂给 HfSendResult.msg_id）"


def test_delivery_seam_translates_in_exactly_two_places():
    """``HfSendResult(`` 只该在交付缝出现：重复闸的占位 + 真发送结果。"""
    n = sum(1 for node in ast.walk(ast.parse(HF_SRC))
            if isinstance(node, ast.Call)
            and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
            == "HfSendResult")
    assert n == 2, f"HfSendResult 构造点应为 2（pre_send 占位 + note_sent 真结果），实际 {n}"


def test_engine_never_constructs_a_platform_send_result():
    """本模块**构造**的只能是 ``HfSendResult``；构造 ``platform.SendResult`` 就是把两种形状
    混在一起（那正是会让账本静默停在 pending 的写法）。

    按 **AST** 判（不看源码文本）：docstring 里为说明问题**引用了**平台形状，
    文本级扫描会把那段说明也算成违规。
    """
    for node in ast.walk(ast.parse(HF_SRC)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            assert name != "SendResult", f"构造了平台的 SendResult（行 {node.lineno}）"


def test_judge_max_tokens_is_hardcoded_not_configurable():
    """300 与 TS 逐字一致（``heartflow.ts:755``）。可配置 ⇒ 有人调成 1000，然后没人知道
    为什么 judge 变慢了。"""
    assert HF.HF_JUDGE_MAX_TOKENS == 300
    body = _fn_code("judge_heartflow")
    assert "max_tokens=HF_JUDGE_MAX_TOKENS" in body
    assert "maxTokens" not in body, "不许从配置读 max_tokens"


def test_judge_is_async_and_the_pipeline_is_sync():
    import inspect

    assert inspect.iscoroutinefunction(HF.judge_heartflow)
    assert inspect.iscoroutinefunction(HF.maybe_engage)
    for fn in (HF.check_gate, HF.parse_response, HF.build_prompt, HF.pre_send, HF.note_sent,
               HF.note_group_message, HF.resolve_hf_effective_budget):
        assert not inspect.iscoroutinefunction(fn), f"{fn.__name__} 不该是 async"


def test_whitelist_rule_has_exactly_one_implementation():
    """白名单语义只有一处（``is_hf_group_allowed``）—— learn 必须委托它，不许自己判。

    这是 #9 落地后才**自证**的一条：``_allowed_filter`` 现在能拿到真实现（不是假模块）。
    """
    f = L._allowed_filter({"enabled": True, "whitelistGroups": ["gYes"]})
    assert f is not None
    assert f("gYes") is True and f("gNo") is False
    assert L._allowed_filter({"enabled": True, "whitelistGroups": []})("any") is True

    body = _fn_code("is_hf_group_allowed")
    for banned in ("budget", "profile", "learn"):
        assert banned not in body, "白名单判定不该顺手做别的事"


def test_check_gate_delegates_the_whitelist_instead_of_inlining_it():
    body = _fn_code("check_gate")
    assert "is_hf_group_allowed" in body, "必须委托（复制必漂移）"
    assert "not-whitelisted" in body, "reason 串是对外契约（/heartflow status 与 TS 对齐）"


def test_engine_never_imports_the_llm_module_at_module_level():
    """``llm.judge`` 拉 aiohttp ⇒ 模块级导入会把注册链变成"看环境"的行为。"""
    tree = ast.parse(HF_SRC)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert "llm" not in (node.module or ""), "llm 只在函数体内导入"


def test_reason_strings_match_the_ts_contract():
    """门禁 reason 串是 TS 的对外契约（日志 / 归因 / 文档都按它 grep）。"""
    assert HF.check_gate("g", "x", {}, 0).reason == "disabled"
    assert HF.check_gate("g", "x", {"enabled": True, "whitelistGroups": ["o"]},
                         0).reason == "not-whitelisted"
    assert HF.check_gate("g", "   ", {"enabled": True}, 0).reason == "empty"


def test_no_eager_heartflow_import_in_the_package_init():
    """包 ``__init__`` 必须**零导入**（可以写 docstring）—— ``verify_tree`` 的
    ``NO_EAGER_INIT_PACKAGES`` 钉着同一件事：``import plugin.inbound.heartflow`` 这个动作
    不许顺带拉起九模块（注册链上任何一环缺依赖都会变成静默不注册）。"""
    init = Path(HF.__file__).parent / "__init__.py"
    tree = ast.parse(init.read_text(encoding="utf-8"))
    for node in tree.body:
        assert not isinstance(node, (ast.Import, ast.ImportFrom)), \
            f"心流包的 __init__ 里出现了导入: {ast.unparse(node)[:60]}"


def test_adapter_imports_heartflow_lazily():
    """适配器只在函数体内 import 心流（加载期纪律；注册失败会被吞成 WARNING）。

    ⚠️ **不 import 适配器**：它顶层依赖 Hermes 的 ``gateway`` 包，本机没有（只在 真机 有）。
    按路径读源码即可 —— 要验的本来就是"源码长什么样"。
    """
    adapter_py = Path(__file__).resolve().parents[2] / "platform" / "adapter.py"
    tree = ast.parse(adapter_py.read_text(encoding="utf-8"))
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "")]
        for n in names:
            assert "heartflow" not in n, "模块级 import 心流"


def test_the_real_module_is_the_one_that_is_loaded():
    """占位守卫：本文件的 monkeypatch 全部走 ``monkeypatch``（自动还原），运行期真模块必须在。"""
    import sys as _sys

    assert _sys.modules["plugin.inbound.heartflow.heartflow"] is HF
    assert hasattr(HF, "maybe_engage")


# ===== 17. 适配器四处的接线（源级：本机装不了 Hermes，装得了的话就该行为级测） =====
#
# 为什么要**源级**钉：适配器顶层 ``from gateway.config import Platform``，本机没有那个包
# （只在 真机 的 Hermes 进程里存在）。而接线的**位置**恰恰是这里最容易烂的地方 ——
# 顺序换一下、挪到判断之前、漏掉一条分支，全是"跑得通但语义变了"，真机验收
# （V7/V8）也未必抓得到：心流照旧"在工作"，只是喂给它的现场记录是伪造的。


def _adapter_tree() -> ast.Module:
    adapter_py = Path(__file__).resolve().parents[2] / "platform" / "adapter.py"
    return ast.parse(adapter_py.read_text(encoding="utf-8"))


def _fn_node(tree: ast.Module, fname: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fname:
            return node
    raise AssertionError(f"adapter.py 里找不到 {fname}")


def _call_lines(fn, attr: str) -> list[int]:
    return sorted(n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == attr)


def test_adapter_send_runs_the_delivery_seam_in_order():
    """``send()``：**发之前**问重复闸 → 发 → **发之后**报结果。

    顺序错了就是两条静默失效：前置挪到发送之后（重复闸永远看不到"即将发出的文本"）、
    后置挪到发送之前（哪怕发失败也记 sent，学习样本造假）。
    """
    fn = _fn_node(_adapter_tree(), "send")
    pre = _call_lines(fn, "_hf_pre_send")
    chunks = _call_lines(fn, "_send_chunk")
    post = _call_lines(fn, "_hf_note_sent")
    bot = _call_lines(fn, "_hf_note_bot_reply")
    assert pre and chunks and post and bot
    assert pre[0] < chunks[0], "重复闸必须在第一次真发送之前"
    # 失败路径上也有一条 ``_hf_note_sent``（在分片循环里），所以取**最后**一条比
    assert post[-1] > chunks[-1], "成功那条交付结果必须在最后一次发送**之后**才记"
    assert bot[0] > chunks[-1], "bot 发言只该在**发成功之后**进缓冲（失败的那句群里没出现）"
    assert bot[0] < post[-1], "先记「我说了什么」（缓冲）再结算交付"


def test_adapter_send_short_circuits_on_a_repeat_placeholder():
    """占位 ⇒ **不发**（I5 的另一半）。真发出去的话，重复闸就只是个装饰。"""
    fn = _fn_node(_adapter_tree(), "send")
    guards = [n for n in ast.walk(fn)
              if isinstance(n, ast.If)
              and isinstance(n.test, ast.BoolOp)
              and "placeholder" in ast.unparse(n.test)]
    assert guards, "send() 里必须有 `plan is not None and plan.placeholder` 这道短路"
    rets = [ast.unparse(n) for g in guards for n in g.body if isinstance(n, ast.Return)]
    assert rets and all("SendResult(success=True" in r for r in rets), \
        "占位要返回**成功**（报失败会让 runner 重试同一条插话直到天亮）"


def test_adapter_partial_shard_failure_is_booked_as_sent():
    """**B5**：多片消息只发出前几片时，要按"说了"记账，不许当"没发"。

    旧写法把失败结果原样交回交付缝 ⇒ ``classify_hf_send`` 判 pending ⇒ 账本不动
    （等框架重试）。可群里**已经有半句了**（红线 3：微信撤不回）：不占预算、不进
    复读历史、不开观察窗，而台账那行停在 ``judged`` 等 sweep 收敛成"没人接话" ——
    一条真说过话的样本被记成负样本。重试还会把第 1 片**再发一遍**。

    一片都没出去时才维持原样（真的没发 ⇒ 上报失败、等重试）。
    """
    fn = _fn_node(_adapter_tree(), "send")
    fails = [n for n in ast.walk(fn)
             if isinstance(n, ast.If) and ast.unparse(n.test) == "not result.success"]
    assert len(fails) == 1, "send() 里应只有分片循环那一处发送失败判定"
    branch = fails[0]
    nested = [n for n in ast.walk(branch)
              if isinstance(n, ast.If) and ast.unparse(n.test) == "last_id is None"]
    assert len(nested) == 1, "失败分支必须区分「一片都没出去」与「已经发出了几片」"

    calls = [n for n in ast.walk(branch)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_hf_note_sent"]
    assert len(calls) == 2, f"失败分支该有两处交付记账（全失败/部分发出），实际 {len(calls)}"
    inner = [c for c in calls if any(c is x for x in ast.walk(nested[0]))]
    outer = [c for c in calls if c not in inner]
    assert len(inner) == 1 and len(outer) == 1
    assert ast.unparse(inner[0].args[-1]) == "result", \
        "一片都没发 ⇒ 照原样上报失败结果（判 pending，等框架重试）"
    assert "SendResult(success=True" in ast.unparse(outer[0]), \
        "已发出几片 ⇒ 必须按「发出去了」上报，否则账本、预算、复读历史三处全丢"
    assert "last_id" in ast.unparse(outer[0]), \
        "部分发出要带上已发出那片的 msgId（否则那条 sent 行永远拿不到 bot_msg_id）"


def test_adapter_records_group_messages_only_after_the_drop_gate():
    """``_handle_one``：旁路观测必须排在丢弃门之后。

    排在前面 = 黑名单群 / 非白名单群的消息也进缓冲（"隐私"那条设计注释直接变成空话）；
    排在触发判断之后更糟 —— 缓冲里只剩 bot 自己的发言，judge 看的是伪造的现场。

    判据本体在 ``inbound.triggers.should_drop``（纯函数，那边有真用例与变异门）；
    这里只钉**位置**与**有没有接线** —— 顺带钉住它带上了 ``ok``：
    只看 reason 不看 ``ok`` 的话，``(True, …)`` 里任何未登记的 reason 会被误丢。
    """
    fn = _fn_node(_adapter_tree(), "_handle_one")
    drop_if = None
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and "should_drop(" in ast.unparse(node.test):
            drop_if = node
            break
    assert drop_if is not None, "找不到 `should_drop(ok, reason)` 那道门（A8 的接线）"
    assert ast.unparse(drop_if.test) == "should_drop(ok, reason)", \
        "判据必须原样吃 (ok, reason) —— 光看 reason、或传空串，" \
        "都会让「明令拒绝的消息」溜到下游（放行类里未登记的 reason 也会被误丢）"
    assert any(isinstance(n, ast.Return) for n in drop_if.body), "那道门必须是「丢完就 return」"

    notes = _call_lines(fn, "_hf_note_group")
    assert notes, "群消息必须喂旁路观测"
    assert all(ln > drop_if.lineno for ln in notes), "旁路观测不得排在被丢弃的消息之前"
    # 且必须**只管群**（私聊不进心流缓冲）
    assert "msg.is_group" in ast.unparse(fn)


def test_adapter_self_messages_are_not_buffered_inbound():
    """自消息**不进**入站缓冲 —— 自己的发言由出站那条路记。

    TS 的判据是"自消息 verdict = blocked"；两边都记 ⇒ 缓冲里同一句话两份，
    "最近 N 条上下文"和 ``last_bot_reply`` 一起掺水。
    """
    fn = _fn_node(_adapter_tree(), "_handle_one")
    self_if = None
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and "self_wxid" in ast.unparse(node.test):
            self_if = node
            break
    assert self_if is not None, "找不到自消息那道门"
    body = ast.unparse(self_if)
    assert "_hf_note_group" not in body, "自消息分支里不许写缓冲（那是出站的活）"
    assert any(isinstance(n, ast.Return) for n in self_if.body)


def test_adapter_seam_helpers_swallow_their_own_errors():
    """四处接缝都必须"心流坏了不影响收发"（try/except + 懒取模块）。

    少了兜底，一个心流 bug 就能让整条消息发不出去 —— 而心流是**旁路**，它没有这个资格。
    """
    tree = _adapter_tree()
    for fname in ("_hf_pre_send", "_hf_note_sent", "_hf_note_bot_reply", "_hf_note_group"):
        fn = _fn_node(tree, fname)
        assert any(isinstance(n, ast.Try) for n in ast.walk(fn)), f"{fname} 没有兜底"
        assert "_hf_mod()" in ast.unparse(fn), f"{fname} 必须懒取心流模块（F5）"


def test_adapter_never_calls_note_group_message_is_bot_true_inbound():
    """入站那条路只记人（``is_bot=False``）—— bot 的话由出站记（见上一条）。"""
    fn = _fn_node(_adapter_tree(), "_hf_note_group")
    assert "is_bot=is_bot" in ast.unparse(fn), "``is_bot`` 由调用方决定（自消息分支已删）"
    calls = [n for n in ast.walk(_fn_node(_adapter_tree(), "_handle_one"))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_hf_note_group"]
    assert calls, "至少还有一处调用"
    for c in calls:
        kw = {k.arg: ast.unparse(k.value) for k in c.keywords}
        assert kw.get("is_bot") == "False", "入站只记人类消息（bot 的走 note_bot_reply）"


def test_adapter_preheats_before_it_starts_the_sweep():
    """账号启动：**先预热三条缓存，再**起 sweep 任务。

    sweep 是"先睡 300s 再跑"（对齐 TS ``setInterval``）⇒ 不预热的后果是重启后头 5 分钟
    阈值回落账号级、预算从 0 起算（= 重启刷额度）、分层统计缺席。而三条加载函数都好好地
    在那儿 —— 缺的只是这一行调用：**实现了但没接线**，本项目最熟的那种静默失效。

    顺序也是判据：预热排在 ``create_task`` 之后的话，起 WS 与预热并行，进来的第一条消息
    仍可能撞上冷缓存（那正是预热要消掉的东西）。
    """
    fn = _fn_node(_adapter_tree(), "_start_sweeps")
    # 2026-10-19 起同一函数里还有风格学习的 preheat/create_task：这里只挑心流那一对，
    # 否则"风格预热排在心流 create_task 之后"会被误判成心流预热顺序错了。
    pre = sorted(n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "preheat_account"
                 and ast.unparse(n.func.value) == "hf_runtime")
    task = sorted(n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "create_task"
                  and "hf_runtime.sweep_loop" in ast.unparse(n))
    assert pre, "_start_sweeps 必须先做心流启动预热"
    assert task, "心流 sweep 任务还得照起"
    assert pre[-1] < task[0], "心流预热必须排在起心流 sweep 之前"
    assert "hf_runtime.sweep_loop" in ast.unparse(fn), "预热不许把 sweep 挤掉"


def test_adapter_preheat_is_wrapped_so_it_cannot_kill_the_sweep():
    """预热失败**不许**连累 sweep：两者同在一个循环里，抛出去就是心流静默停摆。

    ``preheat_account`` 自己已保证不抛（见 ``test_runtime.py``），这里是**第二道网** ——
    防的是"以后有人往预热里加一句会抛的代码"。
    """
    fn = _fn_node(_adapter_tree(), "_start_sweeps")
    # ⚠️ 不能取 ``_call_lines(...)[-1]``：同一函数里还有**风格学习**的
    #    ``style_mod.preheat_account``，它排在后面 —— 取最后一条等于在检查风格那条，
    #    把心流这道网拆掉测试照样绿（2026-10-19 变异体实测存活）。
    hf_pre = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "preheat_account"
              and ast.unparse(n.func.value) == "hf_runtime"]
    assert hf_pre, "心流启动预热不见了"
    line = max(n.lineno for n in hf_pre)
    guarded = [n for n in ast.walk(fn) if isinstance(n, ast.Try)
               and n.lineno < line <= (n.end_lineno or n.lineno)]
    assert guarded, "``preheat_account`` 必须被 try 包住"
