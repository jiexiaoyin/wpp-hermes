"""AFFECTION（W2）—— ``plugin/inbound/affection.py`` + ``plugin/data/repo_affection.py``。

## 这个文件钉什么

移植类工作的失效形态是「**算错了但看起来很像对的**」：情绪修正用了 ``floor`` 而不是
``trunc``（负向交互每次多扣 1 分）、关键词表顺序换了（「打死你这个傻逼」从「威胁」降级成
「侮辱」，−12 变 −8）、``replace("现在", …)`` 把整句替换了好几遍。这些都不会报错，
只会让好感度慢慢跑偏 —— 而好感度是**给人看**的，跑偏了没人能发现。

所以判据分四组，每组的对象不同：

1. **规则表与系数**：逐条对上 TS ``affection.ts`` 的常量（表要是被"顺手优化"过，
   这里立刻红）。
2. **纯函数的边界**：截断取整、``<= 0.1`` 的不动阈值、情绪门槛、上限只挡正向、
   重分配的 ``max(1, …)``。
3. **配置解析**：``model`` **没有**兜底值（有兜底 = 没配 model 也会去调 LLM，
   而 TS 那边根本不调）；``enabled`` 缺省是 false。
4. **落库层**：``user_id=''`` 哨兵行**不许**被当成用户算进人数/总量（少一个
   ``user_id <> ''``，情绪行就成了一个 level=0 的群成员）；情绪过期要**写回库**。

## 为什么 DB 用例不连库

本机没有 MariaDB（真库用例在 ``.183`` 上跑，那 10 条"大声跳过"的用例就是它们）。
这里用**记录器假连接**验 SQL 与参数（同 ``test_inbound_persist.py`` 的 ``_Recorder``），
再用"换掉 repo 函数"验编排逻辑 —— 两边都不碰 IO，且都能在本地跑。
"""

from __future__ import annotations

import ast
import asyncio
import functools
import math
import re
import time
from pathlib import Path

import pytest

from plugin.data import repo_affection as repo
from plugin.inbound import affection as A

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"


def _sync(coro_fn):
    """本仓**没有** pytest-asyncio（见 ``test_voice.py:19`` 同款说明）⇒ 手工 ``asyncio.run``。

    ``functools.wraps`` 让 pytest 仍按原签名解析夹具（它读 ``__wrapped__``）。
    """
    @functools.wraps(coro_fn)
    def _wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return _wrapper



# ============================================================ 夹具：记录器假连接

class _Recorder:
    """假 connection：记 ``(sql, params)``，并按 ``rows`` 回答查询。

    ``db.Connection`` 的形状（``plugin/data/db.py:132``）：``execute`` / ``query`` /
    ``query_one`` / ``query_value``。
    """

    def __init__(self, rows: object = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.rows = rows

    # -- 写 ---------------------------------------------------------------
    def execute(self, sql: str, params=()) -> int:      # noqa: ANN001
        self.calls.append((sql, tuple(params)))
        return 1

    # -- 读 ---------------------------------------------------------------
    def query(self, sql: str, params=()) -> list[dict]:  # noqa: ANN001
        self.calls.append((sql, tuple(params)))
        rows = self.rows
        if isinstance(rows, list):
            return rows
        return [] if rows is None else [rows]  # type: ignore[list-item]

    def query_one(self, sql: str, params=()) -> dict | None:  # noqa: ANN001
        self.calls.append((sql, tuple(params)))
        rows = self.rows
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows  # type: ignore[return-value]

    def query_value(self, sql: str, params=(), default=None):  # noqa: ANN001
        self.calls.append((sql, tuple(params)))
        rows = self.rows
        if isinstance(rows, list):
            if not rows:
                return default
            r = rows[0]
        else:
            r = rows
        if r is None:
            return default
        if isinstance(r, dict):
            return next(iter(r.values()), default)
        return r

    # -- 断言助手 ---------------------------------------------------------
    def sql_of(self, needle: str) -> str:
        """取含 ``needle`` 的那条 SQL（压掉换行，方便断言子句）。"""
        for sql, _ in self.calls:
            if needle in sql:
                return re.sub(r"\s+", " ", sql)
        raise AssertionError(f"没有一条 SQL 含 {needle!r}；实际有：{[s[:60] for s, _ in self.calls]}")

    def params_of(self, needle: str) -> tuple:
        for sql, params in self.calls:
            if needle in sql:
                return params
        raise AssertionError(f"没有一条 SQL 含 {needle!r}")


# ============================================================ §1 规则表与系数

def test_mood_modifiers_are_the_ten_ts_values():
    """10 个情绪系数逐条对上（``affection.ts:33-44``）。"""
    assert dict(A.MOOD_MODIFIERS) == {
        "happy": 1.2, "excited": 1.3, "playful": 1.1, "calm": 1.0, "curious": 1.05,
        "nostalgic": 0.9, "serious": 0.8, "sad": 0.6, "anxious": 0.7, "angry": 0.4,
    }


def test_mood_modifier_formula_and_clamp():
    """``base * (0.5 + intensity*0.5)``，intensity 夹在 [0,1]。"""
    assert A.mood_modifier("happy", 1.0) == pytest.approx(1.2)
    assert A.mood_modifier("happy", 0.0) == pytest.approx(0.6)
    assert A.mood_modifier("calm", 0.5) == pytest.approx(0.75)
    # 越界一律夹住（负 intensity / >1 都不许把系数拉出轨道）
    assert A.mood_modifier("happy", -3) == pytest.approx(0.6)
    assert A.mood_modifier("happy", 9) == pytest.approx(1.2)
    # 不认识的 mood ⇒ base 兜底 1.0（TS 的 ``?? 1.0``）
    assert A.mood_modifier("bogus", 1.0) == pytest.approx(1.0)


def test_rule_table_is_the_seventeen_ts_rules():
    """17 条规则，键集精确对上（多一条少一条都红）。"""
    assert set(A.AFFECTION_RULES) == {
        "chat", "compliment", "flirt", "comfort", "help", "thanks", "apology",
        "tease", "care", "gift", "insult", "harassment", "abuse", "threat",
        "praise", "encourage", "support",
    }
    assert len(A.AFFECTION_RULES) == 17


@pytest.mark.parametrize("name,base,effect", [
    ("chat", 1, 0.1), ("compliment", 3, 0.2), ("praise", 5, 0.3), ("encourage", 4, 0.25),
    ("support", 4, 0.2), ("flirt", 5, 0.15), ("comfort", 4, 0.3), ("help", 2, 0.1),
    ("thanks", 2, 0.15), ("apology", 1, 0.1), ("tease", 2, 0.1), ("care", 3, 0.2),
    ("gift", 8, 0.4), ("insult", -8, -0.5), ("harassment", -6, -0.4),
    ("abuse", -10, -0.6), ("threat", -12, -0.7),
])
def test_rule_base_change_and_mood_effect(name, base, effect):
    """基础分与情绪影响逐条对上（``affection.ts:69-87``）。"""
    r = A.AFFECTION_RULES[name]
    assert r.base_change == base
    assert r.mood_effect == pytest.approx(effect)


def test_rule_flags():
    """四个 flag 逐条对上 —— 它们决定情绪怎么变、门槛怎么卡。"""
    assert A.AFFECTION_RULES["help"].mood_sensitive is False
    assert all(A.AFFECTION_RULES[k].mood_sensitive for k in A.AFFECTION_RULES if k != "help")
    assert {k for k, r in A.AFFECTION_RULES.items() if r.positive_mood_boost} == \
        {"praise", "encourage", "gift"}
    assert {k for k, r in A.AFFECTION_RULES.items() if r.negative_mood_trigger} == \
        {"insult", "harassment", "abuse", "threat"}
    assert {k for k, r in A.AFFECTION_RULES.items() if r.trigger_fear} == {"threat"}
    assert A.AFFECTION_RULES["flirt"].mood_requirements == ("happy", "playful", "excited")
    assert A.AFFECTION_RULES["comfort"].mood_requirements == ("sad", "anxious")
    assert A.AFFECTION_RULES["apology"].mood_requirements == ("angry", "sad")
    assert A.AFFECTION_RULES["tease"].mood_requirements == ("playful", "happy")
    assert A.AFFECTION_RULES["chat"].mood_requirements == ()


def test_mood_tables_cover_the_ten_moods():
    """两个情绪字典各 10 条，键集与 MOOD_MODIFIERS 完全一致（漏一个就拼不出 prompt）。"""
    assert set(A.MOOD_PROMPTS) == set(A.MOOD_MODIFIERS)
    assert set(A.MOOD_DESCRIPTIONS) == set(A.MOOD_MODIFIERS)
    assert A.MOOD_DESCRIPTIONS["calm"] == "平静"
    assert A.MOOD_PROMPTS["happy"].startswith("你现在心情很好")


def test_keyword_tables_match_ts_exactly():
    """★ 五张表**逐词**对上（``affection.ts:152-172``）。

    表就是功能本身：生产账号没开 ``llmClassify`` ⇒ **关键词表是唯一在跑的判据**，
    少一个词 = 那类交互永远不生效，而"少了一个词"这种差异在运行期完全不可见。
    """
    assert set(A.COMPLIMENT_KEYWORDS) == {
        "好美", "漂亮", "可爱", "美丽", "好看", "厉害", "优秀", "聪明", "温柔", "体贴", "贴心",
        "善良", "完美", "很棒", "真好", "不错", "赞", "给力", "牛逼", "好啊", "好呀", "棒棒",
        "太棒了", "真棒", "真厉害", "哇塞", "厉害了", "太好了", "好厉害", "好强", "好棒", "赞赞",
        "牛牛", "牛b", "nb", "牛批", "牛皮", "好牛", "超棒", "超好", "很好", "很厉害",
        "太厉害了", "好喜欢", "喜欢你", "爱了", "太可爱了", "好可爱", "可爱爆了", "萌萌", "好萌",
    }, "称赞表与 TS 不一致（多了会误判、少了这类交互永远不生效）"
    assert set(A.THANKS_KEYWORDS) == {
        "谢谢", "感谢", "多谢", "thank", "谢", "thx", "谢啦", "谢了"}
    assert set(A.CARE_KEYWORDS) == {
        "你好", "早上好", "晚上好", "怎么样", "最近好吗", "hello", "hi", "嗨", "哈喽", "哈罗",
        "安", "早", "晚安", "午安", "下午好", "你在吗", "在吗", "你在不在", "在不在",
        "你好呀", "你好啊"}
    assert set(A.NEGATIVE_KEYWORDS) == {
        "傻逼", "蠢货", "白痴", "垃圾", "废物", "去死", "妈的", "他妈", "畜生", "王八蛋",
        "神经病", "滚蛋", "混蛋", "欠揍", "找打", "白痴啊"}
    assert set(A.THREAT_KEYWORDS) == {"威胁", "杀", "打死", "弄死", "干掉", "揍", "打你"}


def test_the_p1_single_char_words_stay_out():
    """★ P1（TS 2026-08-23）删掉的那批**单字**词不许回来。

    ``includes`` 是裸子串判定 ⇒ 单字正词 ``好`` 会把「晚上好」判成称赞、单字负向词
    ``死`` 会把「笑死了」判成侮辱（−8 + 情绪 sad 两小时）。反例见
    :func:`test_classify_by_rules` 的 ``笑死了``/``狗粮`` 两条 —— 这里是那条判据的
    "别再从词表这一侧漏回来"的守卫。

    ⚠️ **不是**"表里没有单字词"：TS 自己留着 ``赞``（称赞）与 ``安``/``早``（问候）——
    它们的子串误伤面小。所以只钉被点名删掉的那 14 个字。
    """
    removed = list("美棒牛强好6萌帅") + list("死滚草狗操贱婊")
    for table in (A.COMPLIMENT_KEYWORDS, A.THANKS_KEYWORDS, A.CARE_KEYWORDS,
                  A.NEGATIVE_KEYWORDS, A.THREAT_KEYWORDS):
        bad = [w for w in table if w in removed]
        assert bad == [], f"P1 删掉的单字词又回来了：{bad}"


# ============================================================ §2 关键词分类

def test_threat_is_checked_before_negative():
    """★ 顺序即优先级：同时命中两表时按威胁算（−12 + 恐惧），不是侮辱（−8）。

    「打死你这个傻逼」同时含 THREAT（打死）与 NEGATIVE（傻逼）。反过来排 ⇒ 降级成侮辱。
    """
    assert A.classify_interaction_by_rules("打死你这个傻逼") == "threat"
    assert A.classify_interaction_by_rules("我威胁你") == "threat"
    assert A.classify_interaction_by_rules("去死吧你") == "insult"


@pytest.mark.parametrize("text,expected", [
    ("你好漂亮啊", "compliment"),
    ("谢谢你啦", "thanks"),
    ("晚上好", "care"),          # ★ 不许被单字「好」抓成 compliment
    # ⚠️ 「你好呀」在 TS 里**是** compliment：称赞表里有完整的「好呀」，而称赞表在问候表
    #    之前判 ⇒ P1 那句注释（"把你好呀误判为称赞"）只对了一半 —— 单字「好」确实是元凶、
    #    也确实删了，但「好呀」两字本身还留着。这里按**代码**钉，不按注释钉。
    ("你好呀", "compliment"),
    ("hello", "care"),
    ("笑死了", None),            # ★ 不许被单字「死」抓成 insult
    ("狗粮真多", None),          # ★ 不许被单字「狗」抓成 insult
    ("今天天气", None),
    ("", None),
])
def test_classify_by_rules(text, expected):
    assert A.classify_interaction_by_rules(text) == expected


def test_classify_is_case_insensitive_and_trimmed():
    assert A.classify_interaction_by_rules("  THANKS  ") == "thanks"
    assert A.classify_interaction_by_rules("Nb") == "compliment"


# ============================================================ §3 LLM 分类解析

def test_build_classify_prompt_shape():
    p = A.build_classify_prompt("你好", "小助理")
    assert p.startswith("你是微信机器人 小助理的社交关系分析器。")
    assert '消息: "你好"' in p
    assert '"type": "xxx"' in p
    assert p.rstrip().endswith("不确定时优先 \"chat\"。只输出 JSON。")
    # 无名时不带那半截（不是渲染成「机器人 的分析器」）
    assert A.build_classify_prompt("你好", "").startswith("你是微信机器人的社交关系分析器。")


@pytest.mark.parametrize("raw,expected", [
    ('{"type": "praise"}', "praise"),
    ('```json\n{"type": "praise"}\n```', "praise"),
    ('```\n{"type": "praise"}\n```', "praise"),
    ('好的，结果如下：{"type": "gift"} 完毕', "gift"),
    ('  {"type": "chat"}  ', "chat"),
    ('{"type": "anger"}', None),        # ★ 幻觉类型必须被白名单挡住
    ('{"type": "toString"}', None),     # ★ 精确键判定，不复刻 TS 的原型链 ``in``
    ('{"type": 123}', None),
    ('{"nope": 1}', None),
    ('not json at all', None),
    ("", None),
    ("[1,2,3]", None),
])
def test_parse_classify_response(raw, expected):
    assert A.parse_classify_response(raw) == expected


# ============================================================ §4 好感度计算

def _mood(mood_type: str, intensity: float = 0.5, expires_at: int = 0) -> A.MoodState:
    # ``.get`` 而不是 ``[]``：本文件要能用**不认识的** moodType 造状态（脏库值那条）
    return A.MoodState(mood_type, intensity, A.MOOD_DESCRIPTIONS.get(mood_type, ""), expires_at)


def test_mood_gate_blocks_flirt_in_calm_mood():
    """情绪门槛：平静时撩拨不算数（``can_change=False`` 且理由是那句人话）。"""
    r = A.calculate_affection_change("flirt", 10, _mood("calm"), 100)
    assert r.can_change is False and r.change == 0
    assert "calm" in r.reason and "撩拨调情" in r.reason


def test_mood_gate_passes_when_mood_matches():
    r = A.calculate_affection_change("flirt", 10, _mood("happy"), 100)
    assert r.can_change is True
    # happy 1.2 * (0.5+0.25) = 0.9 ⇒ 5*0.9 = 4.5 ⇒ trunc 4
    assert r.change == 4


def test_no_mood_means_no_gate_and_no_modulation():
    """没有情绪状态时：门槛不生效、也不做情绪修正，直接给基础分。"""
    r = A.calculate_affection_change("flirt", 10, None, 100)
    assert r.can_change is True and r.change == 5


def test_negative_change_truncates_toward_zero():
    """★ 截断取整：``trunc(-2.4) == -2``，不是 ``floor`` 的 ``-3``。

    angry 0.4 * (0.5 + 0.5*0.5) = 0.3 ⇒ -8 * 0.3 = -2.4。
    写成 ``floor`` 每次负向交互就多扣一分 —— 而"骂一句多掉一分"没人看得出来。
    """
    r = A.calculate_affection_change("insult", 50, _mood("angry"), 100)
    assert r.change == -2
    assert r.change == math.trunc(-8 * A.mood_modifier("angry", 0.5))


def test_cap_only_blocks_positive_changes():
    """上限只挡正向：满好感时骂人照样扣（否则骂到 100 就免疫了）。"""
    r = A.calculate_affection_change("praise", 100, _mood("calm"), 100)
    assert r.can_change is False and r.reason == "好感度已达到上限"
    r2 = A.calculate_affection_change("insult", 100, _mood("calm"), 100)
    assert r2.can_change is True and r2.change < 0


def test_help_is_not_mood_sensitive():
    """``help`` 是唯一 ``moodSensitive: false`` 的规则 ⇒ 情绪不参与修正，恒 +2。"""
    assert A.calculate_affection_change("help", 0, _mood("angry"), 100).change == 2
    assert A.calculate_affection_change("help", 0, _mood("happy"), 100).change == 2


def test_unknown_interaction_falls_back_to_chat_rule():
    r = A.calculate_affection_change("bogus", 0, None, 100)
    assert r.can_change is True and r.change == 1 and r.reason == "普通聊天"


@pytest.mark.parametrize("mood_type,intensity,expected", [
    ("happy", 1.0, 6),      # 1.2 * (0.5+0.5*1.0) = 1.2 -> trunc(5*1.2) = 6
    ("angry", 1.0, 2),      # 0.4 -> trunc(5*0.4) = 2
    ("sad", 1.0, 3),        # 0.6 -> 3
])
def test_positive_change_uses_the_mood_factor(mood_type, intensity, expected):
    r = A.calculate_affection_change("praise", 0, _mood(mood_type, intensity), 100)
    assert r.change == expected


# ============================================================ §5 重分配

def test_redistribution_is_empty_when_nobody_else_has_points():
    assert A.plan_redistribution([("t", 5)], "t", 10, 0.3) == {}
    assert A.plan_redistribution([], "t", 10, 0.3) == {}
    assert A.plan_redistribution([("a", 0), ("t", 5)], "t", 10, 0.3) == {}


def test_redistribution_never_targets_the_user_who_gained():
    plan = A.plan_redistribution([("a", 100), ("t", 90)], "t", 10, 0.3)
    assert "t" not in plan
    assert set(plan) == {"a"}


def test_redistribution_takes_at_least_one_point_when_it_can():
    """★ ``max(1, …)``：比例算出来不足 1 也要扣 1。

    少了这个 ``max``，``trunc(20 * 0.2 * 0.3) == trunc(1.2) == 1`` 还凑合，
    但 ``trunc(10 * 0.1667 * 0.3) == 0`` —— 那一轮等于白扣，总量永远降不下来。
    下面这个小例子（两人各 10/20 分、要扣 6）正是"每个都只能扣 1"的情形。
    """
    plan = A.plan_redistribution([("a", 10), ("b", 20), ("t", 5)], "t", 6, 0.3)
    assert plan == {"b": 1, "a": 1}, "比例不足 1 也必须各扣 1（否则重分配空转）"


def test_redistribution_deducts_highest_first():
    """高好感先扣（``sort desc`` 的可见后果：扣量分配不对称）。"""
    plan = A.plan_redistribution([("low", 2), ("high", 200)], "t", 10, 1.0)
    assert plan["high"] > plan["low"]


def test_redistribution_respects_the_level_and_remaining_budget():
    """扣量不许超过该用户的分，也不许超过"还要扣多少"。"""
    plan = A.plan_redistribution([("a", 100), ("b", 100)], "t", 100, 1.0)
    assert plan == {"a": 50, "b": 25}
    assert all(v >= 1 for v in plan.values())


def test_redistribution_is_deterministic_on_ties():
    """同分时按 user_id 稳定排序 —— 两次调用结果必须一致（否则测试与线上都飘）。"""
    first = A.plan_redistribution([("b", 10), ("a", 10)], "t", 4, 0.5)
    second = A.plan_redistribution([("a", 10), ("b", 10)], "t", 4, 0.5)
    assert first == second


# ============================================================ §6 情绪状态机

def test_default_mood_is_calm_and_already_expired():
    m = A.default_mood(1000)
    assert (m.mood_type, m.intensity, m.description) == ("calm", 0.5, "平静")
    assert m.expires_at == 1000, "TS 就是 nowMs（即立刻算过期）"


@pytest.mark.parametrize("interaction,mood_type,intensity", [
    ("threat", "anxious", 0.7),
    ("abuse", "angry", 0.6),
    ("insult", "sad", 0.5),
    ("harassment", "anxious", 0.4),
])
def test_negative_interactions_trigger_moods(interaction, mood_type, intensity):
    now = 10_000
    m = A.apply_mood_response(A.default_mood(now), interaction, now)
    assert m is not None
    assert m.mood_type == mood_type
    assert m.intensity == pytest.approx(intensity)
    assert m.expires_at == now + 2 * 3600, "负面情绪 2 小时"


@pytest.mark.parametrize("interaction,mood_type,intensity", [
    ("gift", "excited", 0.4),
    ("praise", "happy", 0.3),
    ("encourage", "happy", 0.25),
])
def test_positive_interactions_trigger_moods(interaction, mood_type, intensity):
    now = 10_000
    m = A.apply_mood_response(A.default_mood(now), interaction, now)
    assert m is not None
    assert (m.mood_type, m.intensity) == (mood_type, pytest.approx(intensity))
    assert m.expires_at == now + 4 * 3600, "正面情绪 4 小时"


def test_a_barely_visible_effect_is_not_written():
    """★ ``|moodEffect| < 0.1`` 不处理，且一般调整里 ``|Δ| <= 0.1`` 也不算变化。

    ``chat`` 的 effect 恰好 0.1：``0.5 + 0.1 = 0.6``，``|0.6 - 0.5| == 0.1`` ⇒ 不动。
    判据写成 ``< 0.1`` 就会每条普通聊天都写一次库（高频写）。
    """
    now = 10_000
    assert A.apply_mood_response(A.default_mood(now), "chat", now) is None
    assert A.apply_mood_response(A.default_mood(now), "help", now) is None


def test_the_tiny_effect_gate_is_a_forward_guard(monkeypatch):
    """★ ``|moodEffect| < 0.1`` 是**前瞻闸**：现有 17 条规则没有一条的效果小于 0.1
    （最小的 ``chat``/``help``/``apology``/``tease`` 恰好 0.1）⇒ 用真规则**够不到**它。

    够不到不等于可以拆：它挡的是"以后有人加一条 0.05 的规则"。那时若拆掉，
    一条**触发型**规则（``negative_mood_trigger``）会拿着 0.05 的效果写进一条真情绪
    （sad/anxious 两小时），而**一般调整那道的 ``<= 0.1`` 闸拦不住它** —— 那道闸只在
    末尾的 general 分支上。所以这里合成一条规则把它钉住：合成规则本身就是判据
    （真规则碰不到，说明这个闸只对未来的表生效）。
    """
    now = 10_000
    tiny = A.AffectionRule(0, False, -0.05, "合成：微小负向", negative_mood_trigger=True)
    monkeypatch.setitem(A.AFFECTION_RULES, "_test_tiny_negative", tiny)
    assert A.apply_mood_response(A.default_mood(now), "_test_tiny_negative", now) is None, \
        "≤0.05 的效果写了一条真情绪 —— 现场看不出来，只是 bot 莫名变成 sad 两小时"

    # 反向自证：闸之上（effect 恰好 0.1 的反向触发）**必须**写 —— 否则"什么都拦"也能全绿
    edge = A.AffectionRule(-12, True, -0.1, "合成：恰好 0.1", negative_mood_trigger=True)
    monkeypatch.setitem(A.AFFECTION_RULES, "_test_edge_negative", edge)
    m = A.apply_mood_response(A.default_mood(now), "_test_edge_negative", now)
    assert m is not None and m.mood_type == "anxious"


def test_general_adjustment_only_touches_intensity():
    """一般调整保留 moodType / description / expiresAt，只换强度。"""
    now = 10_000
    cur = A.MoodState("calm", 0.5, "平静", now + 999)
    m = A.apply_mood_response(cur, "compliment", now)          # +0.2 ⇒ 0.7
    assert m is not None
    assert m.mood_type == "calm" and m.description == "平静"
    assert m.expires_at == now + 999, "一般调整不重置过期时间"
    assert m.intensity == pytest.approx(0.7)


def test_general_adjustment_clamps_then_may_become_a_noop():
    """★ 顺序是"先夹、再判变化太小"：``0.85 + 0.2 = 1.05`` 夹到 ``0.9``，此时只剩 0.05 ⇒ 不写。

    反过来先判再夹，就会写出一个"没变化"的新状态（每条称赞都触发一次高频写）。
    ⚠️ 下界夹不到：负向的规则（insult/harassment/abuse/threat）**全都**是
    ``negativeMoodTrigger``、走的是"换情绪"那条路，不会以一般调整的形式减强度。
    """
    now = 10_000
    cur = A.MoodState("calm", 0.85, "平静", now)
    assert A.apply_mood_response(cur, "compliment", now) is None


def test_unknown_interaction_leaves_the_mood_alone():
    assert A.apply_mood_response(A.default_mood(0), "bogus", 0) is None


# ============================================================ §7 情绪 prompt

@pytest.mark.parametrize("intensity,adverb", [(0.8, "非常"), (0.5, "有些"), (0.3, "轻微")])
def test_mood_prompt_adverb_by_intensity(intensity, adverb):
    out = A.build_mood_system_prompt("", _mood("happy", intensity))
    assert out == (f"\n\n当前情绪状态：心情愉快 你现在{adverb}心情很好，说话比较活泼开朗，"
                   f"容易表达正面情感。\n\n请根据以上情绪状态调整你的回复风格和语气。")


def test_mood_prompt_replaces_only_the_first_occurrence():
    """★ ``replace(…, count=1)``：JS 的 ``String.replace(串,串)`` 只换第一处。

    今天 10 条模板里"现在"各只出现一次，所以全换与只换第一处等价 —— 但判据钉住
    "每条的输出里"现在"数量与模板一致"，将来改文案时不会静默变成替换多处。
    """
    for mood_type, template in A.MOOD_PROMPTS.items():
        out = A.build_mood_system_prompt("", _mood(mood_type, 0.9))
        assert out.count("现在") == template.count("现在"), f"{mood_type} 被替换了多处"


def test_mood_prompt_is_idempotent_on_a_base_that_already_has_mood_words():
    """★ 幂等去重串：base 里已经有"心情/情绪"就不再追加（否则每轮追加一遍）。"""
    base = "你是一个助手。当前情绪状态：平静"
    assert A.build_mood_system_prompt(base, _mood("happy", 0.9)) == base
    assert A.build_mood_system_prompt("注意他的心情", _mood("happy", 0.9)) == "注意他的心情"
    assert A.build_mood_system_prompt("【增量更新】x", _mood("happy", 0.9)) == "【增量更新】x"


def test_mood_prompt_keeps_the_base_prompt():
    out = A.build_mood_system_prompt("你是小助理。", _mood("sad", 0.9))
    assert out.startswith("你是小助理。\n\n当前情绪状态：")


def test_unknown_mood_yields_no_prompt():
    assert A.build_mood_system_prompt("base", _mood("bogus", 0.9)) == "base"


def test_summary_reports_mood_and_top_ten():
    users = [(f"u{i}", i) for i in range(15)]
    out = A.format_affection_summary(_mood("happy", 0.9), users)
    assert out.startswith("情绪: 心情愉快 (强度 0.9,")
    assert "用户数: 15" in out
    assert "u14: 14" in out and "u5: 5" in out
    assert "u4: 4" not in out, "只报前 10 个"


# ============================================================ §8 配置解析

def test_config_defaults_are_conservative():
    c = A.resolve_affection_cfg(None)
    assert c.enabled is False, "键缺失时 fail-closed（TS 的 default 写 true 是自相矛盾的那一侧）"
    assert c.max_user_affection == 100
    assert c.max_total_affection == 500
    assert c.affection_decay_rate == pytest.approx(0.3)
    assert c.timeout_ms == 5000
    assert c.llm_classify is False


def test_both_fail_closed_layers_agree_on_the_default():
    """★ ``enabled`` 的 fail-closed **编码了两次**：dataclass 字段默认 + ``from_cfg`` 的字面量。

    ``resolve_affection_cfg`` 只走后者 ⇒ 只测它，前者翻了也全绿（真事：变异第一轮
    ``enabled: bool = False`` → ``True`` **SURVIVED**）。而前者是**文档级**的默认 ——
    它翻成 ``true``，下一个照文档直接 ``AffectionConfig(...)`` 的地方就悄悄开了功能
    （TS 的 ``defaultAffectionConfig():111`` 正是这么写出 ``true`` 的，然后和接口注释、
    schema 自相矛盾）。所以两层都要钉，且必须**取值一致**。
    """
    assert A.AffectionConfig().enabled is False, "dataclass 默认翻开了"
    assert A.resolve_affection_cfg({}).enabled is False, "解析器默认翻开了"
    assert A.resolve_affection_cfg(None).enabled is False


def test_config_has_no_model_fallback():
    """★ ``model`` **没有**兜底值。

    给个兜底（比如 "deepseek-flash"）看起来更"能用"，实际是把 TS 明确关着的
    LLM 分类悄悄打开了：没配 model 的账号也会去调 LLM。实测 schema 里根本没有
    这个 default，生产账号也没开 ``llmClassify`` ⇒ 兜底等于凭空造行为。
    """
    assert A.resolve_affection_cfg({}).model == ""
    assert A.resolve_affection_cfg({"enabled": True}).model == ""


def test_config_reads_the_production_shape():
    """照生产账号文件那段（``enabled:true`` + ``model`` + ``timeoutMs``）解析。"""
    c = A.resolve_affection_cfg({
        "enabled": True, "model": "deepseek-flash", "timeoutMs": 5000,
        "_model_note": "注解键", "maxRetries": 2, "minJudgeIntervalSec": 30,
    })
    assert c.enabled is True and c.model == "deepseek-flash"
    assert c.timeout_ms == 5000 and c.llm_classify is False


@pytest.mark.parametrize("raw,expected", [
    ({"enabled": "true"}, True), ({"enabled": "on"}, True), ({"enabled": 1}, True),
    ({"enabled": "false"}, False), ({"enabled": "off"}, False), ({"enabled": 0}, False),
    ({"enabled": "乱七八糟"}, False),
])
def test_config_bool_coercion(raw, expected):
    assert A.resolve_affection_cfg(raw).enabled is expected


@pytest.mark.parametrize("raw", [
    {"maxUserAffection": "abc"}, {"maxUserAffection": None}, {"maxUserAffection": True},
    {"maxUserAffection": float("nan")}, {"maxUserAffection": float("inf")},
])
def test_config_bad_numbers_fall_back_to_default(raw):
    c = A.resolve_affection_cfg(raw)
    assert c.max_user_affection == 100
    assert isinstance(c.max_user_affection, int)


def test_config_tolerates_a_non_mapping():
    assert A.resolve_affection_cfg("nope").enabled is False      # type: ignore[arg-type]
    assert A.resolve_affection_cfg({"affection": 1}).max_total_affection == 500


# ============================================================ §9 落库层（SQL 形状）

def test_level_upsert_is_idempotent_by_key():
    rec = _Recorder()
    repo.set_user_level("acct", "g@chatroom", "u1", 7, 1700, conn=rec)
    sql = rec.sql_of("INSERT INTO wpp_affection")
    assert "ON DUPLICATE KEY UPDATE" in sql, "没有 upsert ⇒ 同一个人第二次写会 1062 撞键"
    assert "level = VALUES(level)" in sql
    assert rec.params_of("INSERT INTO wpp_affection") == ("acct", "g@chatroom", "u1", 7, 1700)


def test_reading_a_level_defaults_to_zero():
    assert repo.get_user_level("a", "g", "u", conn=_Recorder(None)) == 0
    assert repo.get_user_level("a", "g", "u", conn=_Recorder({"level": None})) == 0
    assert repo.get_user_level("a", "g", "u", conn=_Recorder({"level": "脏"})) == 0
    assert repo.get_user_level("a", "g", "u", conn=_Recorder({"level": 9})) == 9


@pytest.mark.parametrize("fn,needle", [
    (repo.list_user_levels, "SELECT user_id, level FROM wpp_affection"),
    (repo.get_group_total, "SUM(GREATEST(level, 0))"),
    (repo.count_users, "COUNT(*) AS n FROM wpp_affection"),
])
def test_user_queries_exclude_the_mood_sentinel_row(fn, needle):
    """★ 每条"按用户"的查询都必须排除 ``user_id = ''`` 的情绪行。

    少这一个条件，情绪行就变成一个 ``level=0`` 的群成员：人数多一个、
    ``list_user_levels`` 里多一个空 wxid（重分配可能去扣它）。
    """
    rec = _Recorder([])
    fn("acct", "g@chatroom", conn=rec)
    assert "user_id <> ''" in rec.sql_of(needle)


def test_count_level_rows_is_account_wide_and_skips_the_sentinel():
    """``/affection status`` 报的那个总数：**跨群**、**不含情绪行**。

    两个条件少一个都会给出一个**看起来合理**的错数：按群过滤 ⇒ 只报一个群的；
    不排哨兵行 ⇒ 每个有过情绪的群偷偷 +1（且它 ``level=0``，从数字上完全看不出）。
    方法名里没有 ``group_id`` 参数本身就是判据的一半（跨群）。
    """
    rec = _Recorder([])
    assert repo.count_level_rows("acct", conn=rec) == 0
    sql = rec.sql_of("COUNT(*) AS n FROM wpp_affection")
    assert "user_id <> ''" in sql
    assert "group_id" not in sql, "状态页要的是全账号总量，按群过滤会让它只报一个群"
    assert rec.params_of("COUNT(*)") == ("acct",)


def test_group_total_counts_negative_levels_as_zero():
    """负分按 0 计：否则一个被骂惨的用户能把群总量拉下来、重分配永不触发。"""
    rec = _Recorder({"total": 42})
    assert repo.get_group_total("a", "g", conn=rec) == 42
    assert "GREATEST(level, 0)" in rec.sql_of("SUM(GREATEST")


def test_mood_defaults_when_there_is_no_row():
    m = repo.get_mood("a", "g", 5000, conn=_Recorder(None))
    assert (m.mood_type, m.intensity) == ("calm", 0.5)
    # 空 mood_type 的行也算"没有"
    m2 = repo.get_mood("a", "g", 5000, conn=_Recorder({"mood_type": ""}))
    assert m2.mood_type == "calm"


def test_mood_resets_and_writes_back_when_expired():
    """★ 过期重置必须**写回库**（只返回默认值 ⇒ 每次读都重判一次过期）。"""
    rec = _Recorder({"mood_type": "angry", "intensity": 0.6,
                     "description": "愤怒", "expires_at": 100})
    m = repo.get_mood("a", "g", 5000, conn=rec)
    assert m.mood_type == "calm"
    assert rec.sql_of("INSERT INTO wpp_affection"), "过期重置没有落库"


def test_mood_returns_the_stored_row_when_still_valid():
    rec = _Recorder({"mood_type": "sad", "intensity": 0.5,
                     "description": "心情低落", "expires_at": 9000})
    m = repo.get_mood("a", "g", 5000, conn=rec)
    assert (m.mood_type, m.intensity, m.expires_at) == ("sad", 0.5, 9000)
    assert not [s for s, _ in rec.calls if s.startswith("INSERT")], "没过期不该写库"


def test_an_unknown_stored_mood_is_treated_as_absent():
    """脏值（手工改库 / 枚举变更）不许按未知情绪拼 prompt。"""
    rec = _Recorder({"mood_type": "在吗", "intensity": 0.5,
                     "description": "x", "expires_at": 9000})
    assert repo.get_mood("a", "g", 5000, conn=rec).mood_type == "calm"


def test_set_mood_stores_the_absolute_expiry():
    rec = _Recorder()
    mood = A.MoodState("happy", 0.3, "心情愉快", 88_000)
    repo.set_mood("acct", "g", mood, conn=rec)
    sql = rec.sql_of("INSERT INTO wpp_affection")
    assert "expires_at" in sql and "ON DUPLICATE KEY UPDATE" in sql
    params = rec.params_of("INSERT INTO wpp_affection")
    assert params[0] == "acct" and params[2] == repo.MOOD_ROW_USER_ID
    assert 88_000 in params, "存的是绝对 epoch 秒（相对秒重启后判不了过期）"
    assert params[-1] != 88_000, \
        "updated_at 不能写 expires_at（旧实现两列同值 ⇒ '最近一次情绪变化'恒等于到期时间）"


def test_set_mood_updated_at_is_the_write_time_not_the_expiry():
    rec = _Recorder()
    before = int(time.time())
    repo.set_mood("acct", "g", A.MoodState("happy", 0.3, "x", 99_999), conn=rec)
    after = int(time.time())
    params = rec.params_of("INSERT INTO wpp_affection")
    assert before <= params[-1] <= after, f"updated_at 必须是写入时刻：{params[-1]}"


# ============================================================ §10 编排（换 repo）

@pytest.fixture
def repo_spy(monkeypatch):
    """把 repo 的读写换成内存实现，记录每一次调用。"""
    state: dict[str, object] = {
        "levels": {}, "mood": None, "calls": [], "mood_writes": [],
    }

    def _key(account_id, group_id, user_id):
        return (account_id, group_id, user_id)

    def get_mood(account_id, group_id, now_sec, *, conn=None):
        state["calls"].append("get_mood")  # type: ignore[union-attr]
        m = state["mood"]
        return A.default_mood(now_sec) if m is None else m

    def set_mood(account_id, group_id, mood, *, conn=None):
        state["calls"].append("set_mood")  # type: ignore[union-attr]
        state["mood"] = mood
        state["mood_writes"].append(mood)  # type: ignore[union-attr]

    def get_user_level(account_id, group_id, user_id, *, conn=None):
        return int(state["levels"].get(_key(account_id, group_id, user_id), 0))  # type: ignore[union-attr]

    def set_user_level(account_id, group_id, user_id, level, updated_at, *, conn=None):
        state["calls"].append(("set_user_level", user_id, level))  # type: ignore[union-attr]
        state["levels"][_key(account_id, group_id, user_id)] = level  # type: ignore[union-attr]

    def list_user_levels(account_id, group_id, *, conn=None):
        return [(u, lv) for (a, g, u), lv in state["levels"].items()  # type: ignore[union-attr]
                if a == account_id and g == group_id]

    def get_group_total(account_id, group_id, *, conn=None):
        return sum(max(0, lv) for (a, g, _u), lv in state["levels"].items()  # type: ignore[union-attr]
                   if a == account_id and g == group_id)

    for name, fn in [("get_mood", get_mood), ("set_mood", set_mood),
                     ("get_user_level", get_user_level), ("set_user_level", set_user_level),
                     ("list_user_levels", list_user_levels), ("get_group_total", get_group_total)]:
        monkeypatch.setattr(repo, name, fn)
    return state


def _cfg(**over) -> A.AffectionConfig:
    base = {"enabled": True}
    base.update(over)
    return A.resolve_affection_cfg(base)


@_sync
async def test_disabled_config_does_nothing(repo_spy):
    """★ 关着就一条库都不碰（fail-closed；开着才写）。"""
    out = await A.process_affection_message(
        "acct", "g", "u1", "你好漂亮", "张三", A.resolve_affection_cfg({"enabled": False}), 1000)
    assert out is None
    assert repo_spy["calls"] == [], "关着的账号不该产生任何读写"


@_sync
async def test_compliment_raises_the_level_and_the_mood(repo_spy):
    out = await A.process_affection_message(
        "acct", "g", "u1", "你好漂亮", "张三", _cfg(), 1000)
    assert out is not None
    assert out.interaction_type == "compliment"
    # 默认平静 0.5：calm 1.0 * 0.75 = 0.75 ⇒ 3*0.75 = 2.25 ⇒ trunc 2
    assert out.change == 2
    assert repo_spy["levels"][("acct", "g", "u1")] == 2
    # 情绪：compliment 是 +0.2 的一般调整 ⇒ calm 0.5 → 0.7（|Δ|>0.1 才写）
    assert repo_spy["mood"].intensity == pytest.approx(0.7)


@_sync
async def test_an_unclassifiable_message_falls_back_to_chat(repo_spy):
    """判不出来 ⇒ 按 ``chat`` 处理（不是"跳过"）。

    ★ 顺带钉一个蓝本里真实存在的怪癖：平静情绪（强度 0.5）下 ``trunc(1 * 0.75) == 0``
    —— **普通闲聊一分不加**，只有心情 >0.5 时才加 1 分。写成 ``round``/``floor+1``
    都会让"没话找话"也涨好感度，而这条差异在累计一个月的数据里才看得出。
    """
    out = await A.process_affection_message(
        "acct", "g", "u1", "今天天气还行", "张三", _cfg(), 1000)
    assert out is not None and out.interaction_type == "chat"
    assert out.change == 0
    assert repo_spy["levels"][("acct", "g", "u1")] == 0


@_sync
async def test_insult_lowers_the_level_and_makes_it_sad(repo_spy):
    repo_spy["levels"][("acct", "g", "u1")] = 20
    out = await A.process_affection_message(
        "acct", "g", "u1", "傻逼", "张三", _cfg(), 1000)
    assert out is not None and out.interaction_type == "insult"
    # 平静 0.5：calm 1.0 * 0.75 = 0.75 ⇒ trunc(-8 * 0.75) = -6
    assert out.change == -6
    assert repo_spy["levels"][("acct", "g", "u1")] == 14
    assert repo_spy["mood"].mood_type == "sad"
    assert repo_spy["mood"].expires_at == 1000 + 2 * 3600


@_sync
async def test_a_level_never_goes_below_zero(repo_spy):
    repo_spy["levels"][("acct", "g", "u1")] = 1
    await A.process_affection_message("acct", "g", "u1", "打死你傻逼", "张三", _cfg(), 1000)
    assert repo_spy["levels"][("acct", "g", "u1")] == 0


@_sync
async def test_overflow_redistributes_from_other_users(repo_spy):
    """★ 群总量超限 ⇒ 从其他用户按比例扣（这是 ④⑤ 两步唯一的可见后果）。"""
    repo_spy["levels"].update({("acct", "g", "u1"): 100, ("acct", "g", "u2"): 100})
    await A.process_affection_message(
        "acct", "g", "u3", "你好漂亮", "张三",
        _cfg(maxTotalAffection=150, maxUserAffection=1000), 1000)
    assert repo_spy["levels"][("acct", "g", "u1")] < 100, "没触发重分配"
    assert repo_spy["levels"][("acct", "g", "u2")] < 100


@_sync
async def test_no_redistribution_for_a_negative_interaction(repo_spy):
    """扣分不触发重分配（``calc.change > 0`` 才补扣）—— 否则骂一句会把别人也扣一遍。"""
    repo_spy["levels"].update({("acct", "g", "u1"): 100, ("acct", "g", "u2"): 100})
    await A.process_affection_message(
        "acct", "g", "u3", "傻逼", "张三",
        _cfg(maxTotalAffection=150, maxUserAffection=1000), 1000)
    assert repo_spy["levels"][("acct", "g", "u1")] == 100


@_sync
async def test_the_group_cap_does_not_block_a_negative_interaction(repo_spy):
    """上限只挡正向 —— 满好感时骂人照样扣（与 §4 的纯函数判据同一条）。"""
    repo_spy["levels"][("acct", "g", "u1")] = 100
    out = await A.process_affection_message(
        "acct", "g", "u1", "傻逼", "张三", _cfg(), 1000)
    assert out is not None and out.can_change is True
    assert repo_spy["levels"][("acct", "g", "u1")] < 100


@_sync
async def test_the_llm_is_not_called_without_a_model(monkeypatch, repo_spy):
    """没配 ``model`` ⇒ 一次都不调 judge（TS 在 :266 抛错后吞掉，效果相同）。"""
    called = []

    class _Boom:
        @staticmethod
        async def call_judge(**kw):
            called.append(kw)
            raise AssertionError("不该被调用")

    monkeypatch.setattr("plugin.llm.judge.call_judge", _Boom.call_judge)
    out = await A.process_affection_message(
        "acct", "g", "u1", "今天天气还行", "张三", _cfg(llmClassify=True), 1000)
    assert called == [] and out is not None and out.interaction_type == "chat"


@_sync
async def test_a_failing_judge_degrades_to_keywords(monkeypatch, repo_spy):
    """★ ``call_judge`` 一律 raise（与 TS 的 catch 不同）⇒ 这里必须自己吞。

    少了这个 try/except，"降级为关键词"这条链就断了：入站链路会直接抛穿。
    """
    class _Err(Exception):
        pass

    async def boom(**kw):
        raise _Err("judge 挂了")

    monkeypatch.setattr("plugin.llm.judge.call_judge", boom)
    out = await A.process_affection_message(
        "acct", "g", "u1", "今天天气还行", "张三", _cfg(llmClassify=True, model="m"), 1000)
    assert out is not None and out.interaction_type == "chat"


@_sync
async def test_a_working_judge_can_classify(monkeypatch, repo_spy):
    """LLM 分类真的能接管关键词判不了的消息。"""
    async def fake(**kw):
        assert kw["model"] == "m" and kw["max_tokens"] == 50
        return '{"type": "gift"}'

    monkeypatch.setattr("plugin.llm.judge.call_judge", fake)
    out = await A.process_affection_message(
        "acct", "g", "u1", "给你个东西", "张三", _cfg(llmClassify=True, model="m"), 1000)
    assert out is not None and out.interaction_type == "gift"
    assert out.change == 6, "gift 基础 8 × 平静修正 0.75 = 6"


@_sync
async def test_keywords_win_over_the_llm(monkeypatch, repo_spy):
    """规则先跑：命中关键词就**不调 LLM**（TS 的 ``规则优先`` 顺序）。"""
    called = []

    async def fake(**kw):
        called.append(kw)
        return '{"type": "gift"}'

    monkeypatch.setattr("plugin.llm.judge.call_judge", fake)
    out = await A.process_affection_message(
        "acct", "g", "u1", "谢谢你", "张三", _cfg(llmClassify=True, model="m"), 1000)
    assert called == [] and out is not None and out.interaction_type == "thanks"
