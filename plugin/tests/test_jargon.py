"""群黑话（W7-4）—— 统计层 ``inbound/jargon.py`` + LLM 层 ``inbound/jargon_mine.py``。

## 这个文件钉什么

三层流水线（TS ``jargon.ts`` 729 行）里，**每一层的失效形态都是"静默少做一点"**：

- ① 统计层：分词少了/停了几个 → 候选池变小 → 现象只是"黑话学得慢"；
- ② 批量验证：回包解析的形状搞错（该走兜底时没走、不该走时走了）→ 现象是"LLM 确认了却没存"；
- ③ 三步推断：某一步的"降级回上一步"写成了"返回空串" → 现象是"挖到了但含义是空"。

三者都不报错。所以判据一律下在**可数的产出**上（候选条数、``save_term`` 次数、库里的
meaning、有没有外呼），而不是"调用了哪个函数"。

## 四处 JS/Python 分叉（源码 docstring 里各有一条）

1. ``Math.round`` vs ``round``：``round(2.5) == 2`` 而 ``Math.round(2.5) == 3`` ⇒ 分数在 .5 上分叉；
2. ``\\d``/``\\w`` 是 ASCII 的 ⇒ 纯数字/纯标点两道过滤都写显式 ASCII 类；
3. ``Array.sort``/``sorted`` 都稳定 ⇒ 同分候选保持首次出现顺序，**别加次级键**；
4. ``{}`` 在 JS 是真值、在 Python 是假值 ⇒ ``is_similar === false`` 必须写 ``is False``
   （``0`` 与 ``false`` 在 TS 是同一个值）。

## 两条要如实记下的现实（各有一条用例钉住，且**都没有**变异体）

* **2026-10-19 起中文候选不再被拉黑**（有意偏离 TS ``jargon.ts:323``）：``HARD_BLOCK_RE``
  只拦 @提及/方括号/URL/空白。中文噪声由 ``COMMON_BLOCK``、可选 jieba 词典频率、
  LLM 验证三道控制。§4 与 §12 各有一条判据钉住"候选确实到了 LLM 并落库"。
* ``tokenize`` 的四道过滤里，**纯数字/纯标点两道不可达** —— ``ngram_tokens`` 只会吐出
  汉字段与 ASCII 字母段，两者都过不了那两条正则。留着是为了与 TS 逐字对齐。

## 一处**真 bug**（写这一遍测试时抓到的，已修，见 §12 末尾三条）

``RepoJargonStore.list_terms`` 原先直接把 DB 行的 ``term`` 列交出去，而 store 契约
（``jargon.ts:361``）读的是 ``content`` ⇒ ``exclude`` 恒空集：已确认的词每轮重新送 LLM
并重新 ``upsert``（那条 SQL 是 ``frequency + 1``，词频被反复灌水）。TS 侧**永远看不出**
这个错 —— 它的 ``cfg.store`` 从未被注入过（见 ``jargon_mine.py`` 模块 docstring）。
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import math

import pytest

from plugin.neutral import jargon as J
from plugin.inbound import jargon_mine as M


def _sync(coro_fn):
    """本仓无 pytest-asyncio ⇒ 手工 ``asyncio.run``（同 ``test_intent.py:34``）。"""
    @functools.wraps(coro_fn)
    def _wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return _wrapper


@pytest.fixture(autouse=True)
def _clean_state():
    """两层的内存都在模块级 ⇒ 用例之间必须互不串味。

    ⚠️ 少了它，候选池会带着上一条用例的词 —— 而多数断言只要"含"就不报错，
    于是**顺序相关**的假绿。``reset_stats`` 连 ``_term_meanings`` 一起清（见其实现）。
    """
    J.reset_stats()
    M.reset_all()
    yield
    J.reset_stats()
    M.reset_all()


# ============================================================ §1 分词与 n-gram


def test_ngram_goes_long_to_short_and_that_order_is_load_bearing() -> None:
    """4→3→2 的生成次序**不是**实现细节：候选表按"首次出现"保序，
    取整后同分的候选靠这个顺序决胜负（``Array.sort``/``sorted`` 都稳定）。"""
    assert J.ngram_tokens("测试一下") == [
        "测试一下",                      # n=4
        "测试一", "试一下",              # n=3
        "测试", "试一", "一下",          # n=2
    ]


def test_ascii_segments_are_kept_whole_and_long_ones_dropped() -> None:
    """英文/拼音缩写**整体**当一个候选（``yyds``/``nbcs``）；超过 8 个字母的丢掉
    （长英文串多半是变量名/URL 残渣，不是黑话）。"""
    assert J.ngram_tokens("yyds") == ["yyds"]
    assert J.ngram_tokens("abcdef") == ["abcdef"]
    assert J.ngram_tokens("abcdefghi") == []
    # 两种段按**出现次序**拼接（前一段的全部 n-gram 先出）
    assert J.ngram_tokens("yyds 测试一下")[0] == "yyds"


def test_single_char_segments_are_not_tokens() -> None:
    """``[一-鿿]{2,}`` 那条 ``{2,}`` 是 TS 原样 ⇒ 单字不成段（也就没有 1-gram）。"""
    assert J.ngram_tokens("测") == []
    assert J.ngram_tokens("a") == []


def test_tokenize_cleans_mentions_urls_and_brackets_before_splitting() -> None:
    """@提及 / URL / ``[方括号]`` 三样先换成空格再分词。

    不换掉就会切出 ``wxid``、``https``、``图片`` 这类垃圾段 —— 其中 ``图片`` 恰好在
    停用词表里（双保险），但 URL 与 wxid 不在，会实打实地占候选位。
    """
    base = ["测试一下", "测试一", "试一下", "测试", "试一", "一下"]
    assert J.tokenize("测试一下") == base
    assert J.tokenize("@张三 测试一下") == base
    assert J.tokenize("[图片] 测试一下") == base
    assert J.tokenize("看 https://example.com/a 测试一下") == base


def test_tokenize_applies_the_length_and_stopword_filters() -> None:
    """长度 <2 → 停用词（这两道**可达**）。"""
    assert J.tokenize("a") == []
    assert J.tokenize("哈哈") == []                   # 停用词（也在 COMMON_BLOCK 里）
    assert "真的" not in J.tokenize("真的测试一下")   # 停用词
    assert J.tokenize("") == []
    assert J.tokenize(None) == []


def test_the_digit_and_punctuation_filters_are_unreachable_from_tokenize() -> None:
    """★ 如实钉住：``tokenize`` 的四道过滤里有**两道不可达**。

    ``ngram_tokens`` 只会吐汉字段与 ASCII 字母段（来源正则
    ``[一-鿿]{2,}|[a-zA-Z]{2,}``）⇒ 纯数字段与纯标点段**根本不会产生** ⇒
    ``_PURE_DIGITS_RE`` / ``_PURE_PUNCT_RE`` 在 ``tokenize`` 里永远不命中。
    留着它们是为了与 TS 的书写次序逐字对齐（``jargon.ts:116-136``），**没有**
    专属变异体 —— 任何忠实的变异都语义等价。数字/标点真正的把关在
    ``should_filter_candidate``（§4 有行为判据）。"""
    assert J.ngram_tokens("123") == []          # 数字段压根不产生
    assert J.ngram_tokens("!!!") == []
    assert J.tokenize("123") == []
    assert J.tokenize("!!!") == []
    assert J._PURE_DIGITS_RE.pattern == r"^[0-9\s]+$"


def test_arabic_indic_digits_are_not_ascii_digits_but_this_rule_is_overdetermined() -> None:
    """``^[0-9\\s]+$`` 不含阿拉伯-印度数字（``١٢٣``）—— 显式 ASCII 类，见 docstring ②。

    ⚠️ 如实记下：这条规则在本模块**过定**（``tokenize`` 与 ``should_filter_candidate``
    各写了一次），任一处单独放宽都还有另一处挡着 ⇒ **没有**对应的单次 ``str.replace``
    变异。与 W7-3 的 ``[0-9]`` 同款。
    """
    assert J._PURE_DIGITS_RE.match("١٢٣") is None
    assert J._PURE_DIGITS_RE.match("123") is not None
    assert J.should_filter_candidate("١٢٣") is False      # 不是"纯数字" ⇒ 不被这条拦
    assert J.should_filter_candidate("12٣") is False


# ============================================================ §2 统计表


def test_update_from_message_counts_per_group_per_user() -> None:
    J.update_from_message("yyds", "g1", "u1")
    J.update_from_message("yyds", "g1", "u2")
    J.update_from_message("yyds", "g2", "u1")

    assert J._stats.group_term_freq["g1"]["yyds"] == 2
    assert J._stats.group_term_freq["g2"]["yyds"] == 1
    assert J._stats.global_term_freq["yyds"] == 3
    assert J._stats.user_term_freq["g1"]["yyds"] == {"u1": 1, "u2": 1}
    assert J._stats.dirty_groups == {"g1", "g2"}


def test_update_from_message_no_ops_on_empty_content_or_group() -> None:
    """TS 的两个早退（``jargon.ts:203``）—— 少一个就会把空正文算进"这个群说过话"。"""
    J.update_from_message("", "g1", "u1")
    J.update_from_message("yyds", "", "u1")
    assert J._stats.group_term_freq == {}
    assert J._stats.dirty_groups == set()


def test_first_seen_is_set_once_and_never_moves() -> None:
    """``if (!firstSeen.has(token))`` —— 只有第一次写。写成"每次都覆盖"会让 burst 恒等于
    ``freq/1``（年龄永远是 0 ⇒ 下限 1 天），于是"新爆发的词"这一维彻底失效（不报错）。"""
    J.update_from_message("yyds", "g1", "u1")
    t0 = J._stats.term_first_seen["g1"]["yyds"]
    J.update_from_message("yyds", "g1", "u2")
    assert J._stats.term_first_seen["g1"]["yyds"] == t0


def test_context_examples_are_capped_at_ten() -> None:
    for i in range(15):
        J.update_from_message(f"yyds 第{i}句", "g1", "u1")
    ctx = J._stats.term_contexts["g1"]["yyds"]
    assert len(ctx) == J.MAX_CONTEXT_EXAMPLES == 10
    assert ctx[0] == "yyds 第0句"        # 留的是**最早**的 10 条（TS 同）


def test_updating_a_second_group_does_not_touch_the_first() -> None:
    J.update_from_message("yyds", "g1", "u1")
    J.update_from_message("xswl", "g2", "u1")
    assert set(J._stats.group_term_freq["g1"]) == {"yyds"}
    assert set(J._stats.group_term_freq["g2"]) == {"xswl"}


def test_reset_group_only_clears_that_group() -> None:
    J.update_from_message("yyds", "g1", "u1")
    J.update_from_message("xswl", "g2", "u1")
    J.remember_meaning("g1", "yyds", "永远的神")
    J.reset_group_jargon("g1")
    assert "g1" not in J._stats.group_term_freq
    assert J._stats.group_term_freq["g2"] == {"xswl": 1}
    assert J.known_meaning("g1", "yyds") == ""       # 含义一起清
    assert "g1" not in J._stats.dirty_groups


def test_global_freq_survives_reset_group_because_idf_needs_it() -> None:
    """IDF 按**跨群**频次算 ⇒ ``reset_group_jargon`` 不许动 ``global_term_freq``。

    少了这一条，一个群被重置后其余群的 IDF 会集体漂移（而两边都不报错）。"""
    J.update_from_message("yyds", "g1", "u1")
    J.reset_group_jargon("g1")
    assert J._stats.global_term_freq["yyds"] == 1


def test_prune_drops_low_frequency_terms_when_the_global_table_overflows(monkeypatch) -> None:
    """py2 独有：n-gram 会把每个中文段切成 O(n) 个子串 ⇒ 必须有内存上限。

    上限压低到 3：全局超限 ⇒ 删掉出现 <3 次的词；高频词留下。"""
    monkeypatch.setattr(J, "MAX_GLOBAL_TERMS", 3)
    for _ in range(5):
        for t in ("aaa", "bbb", "ccc"):
            J.update_from_message(t, "g1", "u1")        # 3 个高频词：满，但不超限
    assert len(J._stats.global_term_freq) == 3
    J.update_from_message("ddd", "g1", "u1")            # 第 4 个 ⇒ 超限，低频的被裁
    assert "ddd" not in J._stats.global_term_freq
    assert J._stats.group_term_freq["g1"]["aaa"] == 5
    assert "ddd" not in J._stats.group_term_freq["g1"]


def test_prune_also_cleans_the_per_group_side_tables(monkeypatch) -> None:
    """裁剪漏掉 user_freq/first_seen/contexts ⇒ 三张表与词频表**失去同步**，
    下游按 old key 取出的分数来自一个已经不存在的词（静默）。"""
    monkeypatch.setattr(J, "MAX_GLOBAL_TERMS", 3)
    for _ in range(5):
        for t in ("aaa", "bbb", "ccc"):
            J.update_from_message(t, "g1", "u1")
    J.update_from_message("ddd", "g1", "u1")
    assert "ddd" not in J._stats.user_term_freq["g1"]
    assert "ddd" not in J._stats.term_first_seen["g1"]
    assert "ddd" not in J._stats.term_contexts["g1"]


def test_prune_per_group_limit_is_separate_from_the_global_one(monkeypatch) -> None:
    monkeypatch.setattr(J, "MAX_GLOBAL_TERMS", 10 ** 6)
    monkeypatch.setattr(J, "MAX_GROUP_TERMS", 2)
    for _ in range(5):
        J.update_from_message("aaa", "g1", "u1")
        J.update_from_message("bbb", "g1", "u1")
    assert len(J._stats.group_term_freq["g1"]) == 2
    J.update_from_message("ccc", "g1", "u1")
    assert "ccc" not in J._stats.group_term_freq["g1"]
    assert J._stats.global_term_freq["ccc"] == 1        # 只裁本群那份


# ============================================================ §3 候选打分


def test_js_round_is_half_up_not_bankers() -> None:
    """``Math.round`` 是 ``floor(x+0.5)``。Python 的 ``round(2.5) == 2`` ⇒ 直接用会分叉。"""
    assert J._js_round(0.5) == 1
    assert J._js_round(1.5) == 2
    assert J._js_round(2.5) == 3          # ★ Python round(2.5) 是 2 —— 这一条就是判据
    assert J._js_round(3.5) == 4
    assert J._js_round(0.4999) == 0
    # 负数也一致：JS 的 Math.round(-1.5) === -1、Math.round(-2.5) === -2
    assert J._js_round(-1.5) == -1
    assert J._js_round(-2.5) == -2
    assert J._js_round(-0.5) == 0


def test_burst_score_is_zero_without_a_first_seen_stamp() -> None:
    """``first_seen == 0`` 直接返 0 —— 别让它落进分母。"""
    assert J.calc_burst_score("yyds", "g1") == 0.0
    J.update_from_message("yyds", "g1", "u1")
    assert J.calc_burst_score("nonexistent", "g1") == 0.0


def test_burst_score_is_zero_when_the_term_has_no_stamp_but_does_have_a_count() -> None:
    """★ 守卫的**另一条**输入：词频表里有这个词、首见时间却缺了。

    ⚠️ 这条直接构造内部状态 —— 本模块的写入路径（``update_from_message`` 成对写、
    ``_prune_if_needed`` 成对删）不会产生这个形状，守的是"万一上游只填了词频表"。
    为什么值得单列一条：**两条路的结果差得很远** —— 有守卫 ⇒ 返 ``0.0``；
    没守卫 ⇒ ``first_seen=0`` 落进分母，``(now-0)/86400 ≈ 19000`` 天 ⇒
    ``5 / 19000 ≈ 0.00026``。一个**不为 0** 的分会挤进候选池，而它的 burst 那一维是假的。

    ⚠️ 第一版只测了上面那条（没种子、freq 也是 0）—— 那种输入下 ``0 / 大数`` 同样等于
    ``0.0`` ⇒ 拆掉守卫**一样绿**（变异 SURVIVED）。补这条才把守卫钉住。
    """
    J._stats.group_term_freq.setdefault("g1", {})["yyds"] = 5
    assert not J._stats.term_first_seen.get("g1", {}).get("yyds")
    assert J.calc_burst_score("yyds", "g1") == 0.0


def test_burst_score_age_floor_is_one_day_and_cap_is_fourteen() -> None:
    for _ in range(6):
        J.update_from_message("yyds", "g1", "u1")
    t0 = J._stats.term_first_seen["g1"]["yyds"]

    # 刚出现（年龄 0 天）⇒ 下限 1 天 ⇒ 6/1
    assert J.calc_burst_score("yyds", "g1", now=t0) == pytest.approx(6.0)
    assert J.calc_burst_score("yyds", "g1", now=t0 + 2 * 86400) == pytest.approx(3.0)
    # 超过 14 天 ⇒ 封顶 6/14（burst 不再继续衰减，否则老词的分永远趋近 0）
    assert J.calc_burst_score("yyds", "g1", now=t0 + 30 * 86400) == pytest.approx(6 / 14)


def test_candidates_need_min_frequency() -> None:
    """``MIN_FREQUENCY = 5``：说了 4 次还不够格（这是"有人在用"的最低证据）。"""
    for _ in range(4):
        J.update_from_message("yyds", "g1", "u1")
    assert J.get_jargon_candidates("g1") == []
    J.update_from_message("yyds", "g1", "u1")            # 第 5 次
    assert [c.term for c in J.get_jargon_candidates("g1")] == ["yyds"]


def test_candidate_score_combines_idf_burst_and_concentration() -> None:
    """综合分 = ``idf*0.4 + burst*0.3 + 集中度*0.3``，三项都取整到 4 位。

    单群单用户、年龄 2 天：idf = log(1) = 0、burst = 6/2 = 3、集中度 = 1
    ⇒ 0*0.4 + 3*0.3 + 1*0.3 = 1.2。"""
    for _ in range(6):
        J.update_from_message("yyds", "g1", "u1")
    t0 = J._stats.term_first_seen["g1"]["yyds"]
    (c,) = J.get_jargon_candidates("g1", now=t0 + 2 * 86400)
    assert (c.term, c.frequency, c.unique_users) == ("yyds", 6, 1)
    assert c.idf == 0.0
    assert c.burst_score == 3.0
    assert c.score == 1.2


def test_idf_counts_groups_containing_the_term() -> None:
    """``idf = log(numGroups / groupsContaining)`` —— 只在**一个**群出现的词更可能是
    "这个群的内部话"（分更高）；两个群都在说的多半是网络流行语。"""
    for _ in range(6):
        J.update_from_message("yyds", "g1", "u1")
        J.update_from_message("xswl", "g1", "u1")
        J.update_from_message("xswl", "g2", "u1")
    t0 = J._stats.term_first_seen["g1"]["yyds"]
    got = {c.term: c for c in J.get_jargon_candidates("g1", now=t0)}
    assert got["yyds"].idf == pytest.approx(math.log(2), abs=1e-4)
    assert got["xswl"].idf == 0.0                     # 两个群都有 ⇒ log(2/2) = 0


def test_concentration_rewards_many_speakers() -> None:
    """集中度 = ``1/max(unique_users,1)`` ⇒ "很多人都在说"的分**低于**"一个人反复说"。
    这是黑话与口头禅的分野 —— 反了就变成在挖某人的口癖。

    ⚠️ 两个词的总词频必须**相等**，否则 burst 那一项（``freq/天数``）会盖过集中度。
    第一版把 xswl 也灌成 18 次（6 轮 × 3 个用户），于是 yyds(6 次/1 人) 的 2.1
    反而**低于** xswl(18 次/3 人) 的 5.5 —— 那不是集中度错了，是这条用例没控住变量。
    """
    for _ in range(6):
        J.update_from_message("yyds", "g1", "u1")       # 一个人说 6 次
    for _ in range(2):
        for u in ("u1", "u2", "u3"):
            J.update_from_message("xswl", "g1", u)      # 三个人各说 2 次，共 6 次
    t0 = J._stats.term_first_seen["g1"]["yyds"]
    got = {c.term: c for c in J.get_jargon_candidates("g1", now=t0)}
    assert got["yyds"].frequency == got["xswl"].frequency == 6
    assert got["yyds"].unique_users == 1
    assert got["xswl"].unique_users == 3
    assert got["yyds"].score > got["xswl"].score


def test_candidates_are_sorted_desc_by_score_and_capped() -> None:
    for _ in range(6):
        for t in ("yyds", "xswl", "nbcs"):
            J.update_from_message(t, "g1", "u1")
    out = J.get_jargon_candidates("g1", top_k=2)
    assert len(out) == 2
    assert out[0].score >= out[1].score
    assert len(J.get_jargon_candidates("g1", top_k=3)) == 3


def test_equal_scores_keep_first_seen_order_and_there_is_no_secondary_key() -> None:
    """同分时保序（``sorted`` 稳定 ＝ TS ``Array.sort`` 稳定）。
    加次级键（``key=lambda c: (-c.score, c.term)``）就是分叉 —— 这条钉住不加。"""
    for _ in range(6):
        for t in ("zzz", "aaa", "mmm"):
            J.update_from_message(t, "g1", "u1")
    assert [c.term for c in J.get_jargon_candidates("g1")] == ["zzz", "aaa", "mmm"]


def test_excluded_terms_never_reach_the_candidate_list() -> None:
    """已确认的词条由调用方从**库**里读出来当排除集（``mine_jargon_for_group`` 的 ①）。
    漏了这一步 ⇒ 同一批词每轮都被重新送进 LLM 计费。"""
    for _ in range(6):
        J.update_from_message("yyds", "g1", "u1")
    assert J.get_jargon_candidates("g1", exclude_terms={"yyds"}) == []
    assert [c.term for c in J.get_jargon_candidates("g1", exclude_terms={"other"})] == ["yyds"]


def test_empty_exclude_set_is_not_treated_as_missing() -> None:
    """``exclude_terms or set()``：**空集**与 ``None`` 等价（都不过滤）。
    若反过来把空集当"全都排除"，候选池会恒空（挖掘永久停摆，且不报错）。"""
    for _ in range(6):
        J.update_from_message("yyds", "g1", "u1")
    assert len(J.get_jargon_candidates("g1", exclude_terms=set())) == 1


def test_candidates_from_an_unknown_group_are_empty() -> None:
    assert J.get_jargon_candidates("nope") == []


def test_context_examples_are_truncated_to_five_on_output() -> None:
    """存储上限 10 条、**输出**只带 5 条（``jargon.ts:283`` 的 ``slice(0,5)``）——
    5 条要送给 LLM，多了就是白花 token。"""
    for i in range(12):
        J.update_from_message(f"yyds 第{i}句", "g1", "u1")
    (c,) = J.get_jargon_candidates("g1")
    assert len(c.context_examples) == 5
    assert c.context_examples[0] == "yyds 第0句"


def test_group_stats_counts_terms_above_threshold() -> None:
    for _ in range(5):
        J.update_from_message("yyds", "g1", "u1")
    J.update_from_message("xswl", "g1", "u1")
    assert J.get_group_stats("g1") == {
        "total_unique_terms": 2,
        "total_occurrences": 6,
        "terms_above_threshold": 1,
    }
    assert J.get_group_stats("nope") == {
        "total_unique_terms": 0, "total_occurrences": 0, "terms_above_threshold": 0,
    }


# ============================================================ §4 硬过滤（含中文即拉黑）


def test_only_short_ascii_abbreviations_survive_the_filter() -> None:
    assert J.should_filter_candidate("yyds") is False
    assert J.should_filter_candidate("xswl") is False
    assert J.should_filter_candidate("abcdef") is False       # 6 个字母，勉强收


def test_chinese_terms_survive_the_filter_after_the_2026_10_19_deviation() -> None:
    """★ 中文候选现在**能到 LLM**（2026-10-19 老板拍板，有意偏离 TS）。

    TS 的 ``HARD_BLOCK_RE`` 含 ``[一-鿿]``，中文永远到不了 LLM；py2 原先照搬，
    真机上等于"黑话插件开着，中文黑话永远学不到"。放宽后仍拦 @提及/方括号/URL/空白。
    """
    assert J.HARD_BLOCK_RE.search("测试") is None
    assert J.should_filter_candidate("测试") is False
    assert J.should_filter_candidate("真香") is False
    assert J.should_filter_candidate("yyds@") is True         # @ 提及残渣
    assert J.should_filter_candidate("[yyds]") is True        # 方括号
    assert J.should_filter_candidate("yy ds") is True         # 空白
    assert J.should_filter_candidate("yy　ds") is True        # 全角空格（TS 原文里那一项）


def test_length_bounds_are_two_to_eight() -> None:
    assert J.should_filter_candidate("") is True
    assert J.should_filter_candidate("a") is True
    assert J.should_filter_candidate("ab") is False
    assert J.should_filter_candidate("abc1") is False         # 4 字符、非纯字母
    assert J.should_filter_candidate("abcd1234") is False     # 恰好 8
    assert J.should_filter_candidate("abcd12345") is True     # 9 ⇒ 超长


def test_pure_digits_and_long_ascii_are_rejected() -> None:
    assert J.should_filter_candidate("12345") is True
    assert J.should_filter_candidate("123456") is True
    assert J.should_filter_candidate("abcdefg") is True       # 7 个字母（正则 {7,}）
    assert J.should_filter_candidate("abcdef") is False


def test_common_block_is_reachable_and_blocks_common_words() -> None:
    """中文放宽后 ``COMMON_BLOCK`` 从"不可达死码"变成**真正生效**的一道过滤。

    保留 TS 原文的 22 条 + py2 补充的常用词；每一条都必须被过滤。这条是中文放宽后
    防止"哈哈/今天/价格"这类标准词挤占候选名额的第一道闸。
    """
    assert len(J.COMMON_BLOCK) >= 22
    for t in J.COMMON_BLOCK:
        assert J.should_filter_candidate(t) is True
    # 中文不再被 HARD_BLOCK 拦截 ⇒ 这些必须由 COMMON_BLOCK 拦下
    assert J.HARD_BLOCK_RE.search("哈哈") is None
    assert J.should_filter_candidate("哈哈") is True
    assert J.should_filter_candidate("今天") is True
    assert J.should_filter_candidate("价格") is True


def test_the_common_block_check_sits_after_the_hard_block_check() -> None:
    """顺序判据：``HARD_BLOCK_RE`` 排在 ``COMMON_BLOCK`` 之前（TS 的书写次序）。
    反过来的执行结果**完全相同**（集合包含）⇒ 这条只钉"次序未被无意调换"，
    **没有**变异体（如实记下）。"""
    src = inspect.getsource(J.should_filter_candidate)
    assert src.index("HARD_BLOCK_RE.search") < src.index("COMMON_BLOCK")


# ============================================================ §5 含义缓存与查询


def test_remember_meaning_is_a_bridge_from_the_db_side_to_the_query_tool() -> None:
    """``wpp_jargon_query`` 读的是**内存统计**，而挖掘的产出落在 ``wpp_jargon_terms``
    表里 ⇒ 不搭这座桥，"挖到了但没人读得到"（W7-1 抓到的同一族失败形态）。"""
    J.remember_meaning("g1", "yyds", "永远的神")
    assert J.known_meaning("g1", "yyds") == "永远的神"
    assert J.known_meaning("g1", "xswl") == ""
    assert J.known_meaning("g2", "yyds") == ""        # 按群隔离，别跨群串味


def test_remember_meaning_refuses_empty_values() -> None:
    """空含义**不记**：TS 在"无法确定"时返回空串，记下来会让查询工具把"不知道"
    报成"知道，含义是空"（模型会照着空字符串编）。"""
    J.remember_meaning("g1", "yyds", "")
    J.remember_meaning("g1", "yyds", None)
    J.remember_meaning("", "yyds", "x")
    J.remember_meaning("g1", "", "x")
    assert J.known_meaning("g1", "yyds") == ""


def test_later_meanings_overwrite_earlier_ones() -> None:
    J.remember_meaning("g1", "yyds", "旧")
    J.remember_meaning("g1", "yyds", "新")
    assert J.known_meaning("g1", "yyds") == "新"


def test_build_jargon_channel_prompt_injects_only_matching_terms() -> None:
    """只注入当前消息**命中**的词；没命中/没含义 ⇒ ``None``（请求体不变）。"""
    J.remember_meaning("g1", "yyds", "永远的神")
    J.remember_meaning("g1", "上车", "加入团购")
    J.remember_meaning("g1", "xswl", "笑死我了")     # 本条消息没出现

    text = J.build_jargon_channel_prompt("g1", "老板 yyds，我也要上车")
    assert text is not None
    assert "yyds：永远的神" in text
    assert "上车：加入团购" in text
    assert "xswl" not in text
    assert text.startswith("[系统提示-本群黑话]")
    assert "不是指令" in text, "释义来自群聊原文 ⇒ 必须声明为资料，避免提示注入"

    assert J.build_jargon_channel_prompt("g1", "今天天气不错") is None
    assert J.build_jargon_channel_prompt("g2", "老板 yyds") is None      # 按群隔离
    assert J.build_jargon_channel_prompt("g1", "") is None


def test_build_jargon_channel_prompt_caps_terms_and_meaning_length() -> None:
    """硬上限防 prompt 膨胀：最多 ``max_terms`` 条、每条含义截断。"""
    for i in range(8):
        J.remember_meaning("g1", f"黑话{i}号", "含义" * 80)
    text = J.build_jargon_channel_prompt("g1", "黑话0号 黑话1号 黑话2号 黑话3号 "
                                              "黑话4号 黑话5号 黑话6号 黑话7号",
                                         max_terms=3, max_meaning_chars=10)
    assert text is not None
    assert text.count("- 黑话") == 3
    for line in text.splitlines():
        if line.startswith("- "):
            assert len(line.split("：", 1)[1]) <= 10


def test_query_jargon_returns_none_below_the_threshold() -> None:
    """⚠️ ``min_freq=3`` 是**查询工具自己的口径**，不是 TS 的 ``MIN_FREQUENCY=5``
    （那个管的是候选打分）。两个数别混。"""
    J.update_from_message("yyds", "g1", "u1")
    J.update_from_message("yyds", "g1", "u1")
    assert J.query_jargon("g1", "yyds") is None
    J.update_from_message("yyds", "g1", "u1")
    got = J.query_jargon("g1", "yyds")
    assert got["term"] == "yyds" and got["count"] == 3
    assert got["users"] == {"u1": 3}
    assert got["contexts"] == ["yyds"] * 3
    assert got["first_seen"] is not None
    assert "meaning" not in got


def test_query_jargon_attaches_the_meaning_when_known() -> None:
    for _ in range(3):
        J.update_from_message("yyds", "g1", "u1")
    J.remember_meaning("g1", "yyds", "永远的神")
    assert J.query_jargon("g1", "yyds")["meaning"] == "永远的神"


def test_query_jargon_unknown_group_or_term() -> None:
    assert J.query_jargon("nope", "yyds") is None
    J.update_from_message("yyds", "g1", "u1")
    assert J.query_jargon("g1", "nope") is None


def test_list_hot_jargon_sorts_by_count_and_attaches_meanings() -> None:
    for _ in range(5):
        J.update_from_message("yyds", "g1", "u1")
    for _ in range(3):
        J.update_from_message("xswl", "g1", "u1")
    J.update_from_message("nbcs", "g1", "u1")         # 1 次 ⇒ 低于 min_freq
    J.remember_meaning("g1", "yyds", "永远的神")
    assert J.list_hot_jargon("g1") == [
        {"term": "yyds", "count": 5, "meaning": "永远的神"},
        {"term": "xswl", "count": 3},
    ]


def test_limit_and_frequency_gate_are_both_prefixes_so_their_order_is_unobservable() -> None:
    """如实记下：先 ``[:limit]`` 再判 ``min_freq`` 与反过来**结果恒等**
    （两者都是"降序列表的前缀"）⇒ 这个次序在本函数里**不可观测**，不假装有判据。
    能看见的只有行为本身：低频词被挡掉、条数被 limit 截断。"""
    for _ in range(5):
        J.update_from_message("yyds", "g1", "u1")
    for _ in range(3):
        J.update_from_message("xswl", "g1", "u1")
    J.update_from_message("nbcs", "g1", "u1")
    assert J.list_hot_jargon("g1", limit=1) == [{"term": "yyds", "count": 5}]
    assert J.list_hot_jargon("g1", limit=2) == [
        {"term": "yyds", "count": 5}, {"term": "xswl", "count": 3}]
    assert J.list_hot_jargon("g1", limit=10) == [
        {"term": "yyds", "count": 5}, {"term": "xswl", "count": 3}]   # nbcs 被阈值挡掉


def test_group_stats_summary_has_a_fallback_line() -> None:
    assert J.get_group_stats_summary("nope") == "（暂无黑话数据）"
    for _ in range(3):
        J.update_from_message("yyds", "g1", "u1")
    assert J.get_group_stats_summary("g1") == "群黑话TOP: yyds(3次)"


# ============================================================ §6 配置解析（LLM 层）


def test_cfg_defaults_match_default_jargon_config() -> None:
    """逐条对齐 TS ``defaultJargonConfig()``（``jargon.ts:365-376``）：
    enabled false / timeoutMs 5000 / mineIntervalSec 60 / minMessages 10 /
    maxCandidatesPerGroup 50。``model`` **没有**默认值（见下一条）。"""
    for raw in ({}, None, {"unrelated": 1}, "not a dict", [1]):
        c = M.resolve_jargon_cfg(raw)
        assert c.enabled is False
        assert c.model == ""
        assert (c.timeout_ms, c.mine_interval_sec) == (5000, 60)
        assert (c.min_messages, c.max_candidates_per_group) == (10, 50)


def test_cfg_accepts_the_full_key_set() -> None:
    c = M.resolve_jargon_cfg({
        "enabled": True, "model": "MiniMax-M2.7-highspeed", "timeoutMs": 3000,
        "mineIntervalSec": 120, "minMessages": 30, "maxCandidatesPerGroup": 80,
    })
    assert (c.enabled, c.model) == (True, "MiniMax-M2.7-highspeed")
    assert (c.timeout_ms, c.mine_interval_sec) == (3000, 120)
    assert (c.min_messages, c.max_candidates_per_group) == (30, 80)


def test_cfg_enabled_coercion_is_fail_closed() -> None:
    """``enabled`` 的每个怪值都必须落到 ``False``（fail-closed）—— 这是"旁路要不要跑"
    的总闸，猜错方向就是"没开的开关自己开了"。"""
    for v in ("true", "1", "yes", "on", "TRUE", " On ", 1, 2.5):
        assert M.resolve_jargon_cfg({"enabled": v}).enabled is True, v
    for v in (False, "false", "0", "no", "off", "", "garbage", None, 0, 0.0, [], {}):
        assert M.resolve_jargon_cfg({"enabled": v}).enabled is False, v


def test_cfg_int_keys_reject_bools() -> None:
    """Python 的 ``bool`` 是 ``int`` ⇒ ``timeoutMs: true`` 会被 ``int()`` 成 1
    （"超时 1 毫秒"）。与 W3 的 ``groupContextWindow: true`` 同一个坑。"""
    c = M.resolve_jargon_cfg({"timeoutMs": True, "mineIntervalSec": True,
                              "minMessages": False, "maxCandidatesPerGroup": True})
    assert (c.timeout_ms, c.mine_interval_sec) == (5000, 60)
    assert (c.min_messages, c.max_candidates_per_group) == (10, 50)


def test_cfg_int_keys_accept_numeric_strings_and_reject_junk() -> None:
    c = M.resolve_jargon_cfg({"timeoutMs": "3000", "minMessages": "20"})
    assert (c.timeout_ms, c.min_messages) == (3000, 20)
    c = M.resolve_jargon_cfg({"timeoutMs": "abc", "minMessages": None})
    assert (c.timeout_ms, c.min_messages) == (5000, 10)


def test_cfg_model_is_stripped_and_never_defaulted() -> None:
    """``model`` 没默认值是**故意的**：见 :class:`M.JargonConfigError`。"""
    assert M.resolve_jargon_cfg({"model": "  gpt-4o  "}).model == "gpt-4o"
    assert M.resolve_jargon_cfg({"model": None}).model == ""
    assert M.resolve_jargon_cfg({"model": "   "}).model == ""


def test_cfg_is_frozen() -> None:
    c = M.resolve_jargon_cfg({"enabled": True})
    with pytest.raises(Exception):
        c.enabled = False       # type: ignore[misc]


# ============================================================ §7 消息历史与挖掘节流


def test_record_message_prefixes_the_sender_and_caps_at_two_hundred() -> None:
    for i in range(205):
        M.record_message("g1", "u1", f"消息{i}")
    hist = M.recent_messages("g1", 500)
    assert len(hist) == M.MSG_HISTORY_MAX == 200
    assert hist[0] == "u1: 消息5"          # 留的是**最后** 200 条
    assert hist[-1] == "u1: 消息204"


def test_record_message_skips_blank_content_and_therefore_does_not_count_it() -> None:
    """★ 空正文**不记也不计数**（TS 的早退在计数之前）。

    计数错一位 ⇒ ``minMessages`` 那道闸早/晚一拍；更坏的是：把空消息也计数，
    配上一个只会发系统通知的群，计数会一直涨到触发挖掘 —— 而那个群一句话都没说过。"""
    for bad in ("", "   ", "\n", "\t"):
        M.record_message("g1", "u1", bad)
    assert M.recent_messages("g1", 10) == []
    assert M.group_message_count("g1") == 0


def test_message_count_is_monotonic_and_decoupled_from_history_length() -> None:
    """★ P1（``jargon.ts:382-384``）：**不能用历史长度当"新增消息数"**。

    缓冲满 200 之后长度恒为 200 ⇒ "新增"恒等于 0 ⇒ **挖掘永久停摆**。
    这条用"250 条之后计数还在涨、而历史长度不动"把它钉死。"""
    for i in range(250):
        M.record_message("g1", "u1", f"消息{i}")
    assert M.group_message_count("g1") == 250
    assert len(M.recent_messages("g1", 10 ** 6)) == 200
    assert M.group_message_count("g1") != len(M.recent_messages("g1", 10 ** 6))


def test_recent_messages_keeps_insertion_order() -> None:
    M.record_message("g1", "u1", "第一句")
    M.record_message("g1", "u2", "第二句")
    assert M.recent_messages("g1", 30) == ["u1: 第一句", "u2: 第二句"]
    assert M.recent_messages("g1", 1) == ["u2: 第二句"]
    assert M.recent_messages("nope", 30) == []


def test_should_trigger_mine_needs_both_gates() -> None:
    cfg = M.JargonCfg(enabled=True, mine_interval_sec=60, min_messages=10)
    # 间隔够（1_000_000 - 0 ≥ 60000）、新增够（10 - 0 ≥ 10）⇒ 触发并记账
    assert M.should_trigger_mine("g1", cfg, 1_000_000, 10) is True
    # 间隔不够
    assert M.should_trigger_mine("g1", cfg, 1_030_000, 20) is False
    # 间隔够了但新增不够（15 - 10 = 5 < 10）—— **且不许记账**
    assert M.should_trigger_mine("g1", cfg, 1_100_000, 15) is False
    # 还是用 10 当基线 ⇒ 20 - 10 = 10 ⇒ 这次触发
    assert M.should_trigger_mine("g1", cfg, 1_100_000, 20) is True


def test_the_state_is_written_only_when_both_gates_pass() -> None:
    """★ 反过来的写法（先记账再判）会让"间隔没到"的那次把配额白吃掉 ⇒ 挖掘**永远不触发**
    （每次都在记账，每次都返回 False）。"""
    cfg = M.JargonCfg(enabled=True, mine_interval_sec=60, min_messages=10)
    assert M.should_trigger_mine("g1", cfg, 1_000_000, 10) is True
    assert M.should_trigger_mine("g1", cfg, 1_030_000, 100) is False       # 间隔不够，不记账
    # 若那次记了账（lastMsgCount=100），这里 100-100=0 就永远触发不了
    assert M.should_trigger_mine("g1", cfg, 1_100_000, 100) is True


def test_throttle_state_is_per_group() -> None:
    cfg = M.JargonCfg(enabled=True, mine_interval_sec=60, min_messages=10)
    assert M.should_trigger_mine("g1", cfg, 1_000_000, 10) is True
    assert M.should_trigger_mine("g2", cfg, 1_000_000, 10) is True         # 另一个群不共享节流


def test_reset_mine_states_clears_the_throttle() -> None:
    cfg = M.JargonCfg(enabled=True, mine_interval_sec=600, min_messages=1)
    assert M.should_trigger_mine("g1", cfg, 1_000_000, 1) is True
    assert M.should_trigger_mine("g1", cfg, 1_000_001, 2) is False
    M.reset_mine_states()
    assert M.should_trigger_mine("g1", cfg, 1_000_001, 2) is True


def test_zero_interval_triggers_on_every_qualifying_message() -> None:
    """``mineIntervalSec: 0`` ⇒ 只由 ``minMessages`` 把关（配置允许这么调）。"""
    cfg = M.JargonCfg(enabled=True, mine_interval_sec=0, min_messages=1)
    assert M.should_trigger_mine("g1", cfg, 1_000_000, 1) is True
    assert M.should_trigger_mine("g1", cfg, 1_000_000, 2) is True


# ============================================================ §8 回包解析


def test_extract_string_array_plain_fenced_and_surrounded() -> None:
    assert M.extract_string_array('["yyds","xswl"]') == ["yyds", "xswl"]
    assert M.extract_string_array('```json\n["yyds"]\n```') == ["yyds"]
    assert M.extract_string_array('```\n["yyds"]\n```') == ["yyds"]
    # ⚠️ 前后有闲话 ⇒ ``json.loads`` 抛 ⇒ **这才**走方括号兜底
    assert M.extract_string_array('好的，确认：[ "yyds" ] 以上') == ["yyds"]


def test_extract_string_array_drops_non_string_elements() -> None:
    assert M.extract_string_array('["yyds", 1, {"a":1}, null, "xswl"]') == ["yyds", "xswl"]


def test_a_parseable_non_array_does_not_get_the_bracket_fallback() -> None:
    """★ TS 把 ``[...]`` 兜底写在 **catch** 里 ⇒ 解析成功但不是数组时**直接返回 ``[]``**。

    改成"先试解析、失败或非数组再抠方括号"会让 ``{"a":1}`` 这类回包多走一遍正则 ——
    这是分支形状的分叉，不是等价重写。"""
    assert M.extract_string_array('{"a": 1}') == []
    assert M.extract_string_array('"just a string"') == []
    assert M.extract_string_array("42") == []
    assert M.extract_string_array("true") == []


def test_extract_string_array_returns_empty_on_garbage() -> None:
    assert M.extract_string_array("") == []
    assert M.extract_string_array("完全不是 JSON") == []
    assert M.extract_string_array("[不是 json") == []
    assert M.extract_string_array("[]") == []


def test_extract_json_object_plain_fenced_and_surrounded() -> None:
    assert M.extract_json_object('{"meaning":"永远的神"}') == {"meaning": "永远的神"}
    assert M.extract_json_object('```json\n{"is_similar": false}\n```') == {"is_similar": False}
    assert M.extract_json_object('推断结果：{"is_similar": true} 完毕') == {"is_similar": True}


def test_extract_json_object_returns_non_dicts_as_is_because_ts_lies_about_the_type() -> None:
    """TS 写的是 ``JSON.parse(s) as Record<string, unknown>`` —— 一个**类型谎言**：
    数组/数字/字符串都会原样返回。py2 照样返回，由调用方 ``isinstance(o, dict)`` 判。

    这种"类型断言是假的"最容易被 Python 侧好心改成"不是 dict 就返 None" ——
    那会让 ``o?.meaning`` 与 ``isinstance(o, dict)`` 两条路在**同一个输入**上分歧。
    """
    assert M.extract_json_object("[1, 2]") == [1, 2]
    assert M.extract_json_object("42") == 42
    assert M.extract_json_object('"hi"') == "hi"
    assert M.extract_json_object("null") is None


def test_extract_json_object_returns_none_on_garbage() -> None:
    assert M.extract_json_object("") is None
    assert M.extract_json_object("完全不是 JSON") is None
    assert M.extract_json_object("{不是 json") is None


# ============================================================ §9 五个 prompt


def test_compare_prompt_verbatim() -> None:
    out = M.build_compare_prompt("yyds", "永远的神", "永远的神")
    assert out == (
        "词条：yyds\n\n"
        "有两个推断：\n"
        "推断1（群聊上下文中的含义）：永远的神\n"
        "推断2（字面/常规含义）：永远的神\n\n"
        "如果推断1和推断2含义不同（说明词条在群里被赋予了特殊含义，可能是黑话），"
        '返回 {"is_similar": false}\n'
        "如果两者含义相同或接近（说明是常规用法），返回 {\"is_similar\": true}\n\n"
        "只输出 JSON，不要解释。"
    )


def test_compare_prompt_marks_an_empty_context_meaning() -> None:
    """空上下文含义要印成"（无法确定）"而不是空串 —— 否则 prompt 里出现
    "推断1（...）：" 后面直接换行，模型会以为那一栏缺失。"""
    assert "（无法确定）" in M.build_compare_prompt("yyds", "", "字面")


def test_content_only_prompt_verbatim() -> None:
    assert M.build_infer_content_only_prompt("yyds") == (
        "词条：yyds\n\n"
        "请仅凭这个词条本身，推断它的字面含义（不依赖任何上下文）。\n\n"
        "以 JSON 输出：\n"
        '{"meaning": "按字面/常规理解的解释"}'
    )


def test_infer_with_context_prompt_carries_term_and_context() -> None:
    out = M.build_infer_with_context_prompt("yyds", "u1: 这个 yyds 啊")
    assert out.startswith(
        "以下是一个群聊中出现的词条和它出现的上下文。\n\n词条：yyds\n上下文：\nu1: 这个 yyds 啊")
    assert '"no_info": true或false' in out


def test_validate_prompt_embeds_the_candidates_as_a_json_array() -> None:
    """候选表必须是 ``json.dumps(..., ensure_ascii=False)`` 的形状 ——
    用 ``ensure_ascii=True``（默认）会把中文转成 ``\\uXXXX``，模型仍看得懂，
    但 prompt 与 TS **不再逐字相同**。"""
    raw = ["yyds", "中文候选"]
    out = M.build_validate_prompt("u1: 聊天", raw)
    assert json.dumps(raw, ensure_ascii=False) in out
    assert "中文候选" in out
    assert "\\u" not in out
    assert out.endswith("如果没有，输出 []")


def test_extract_prompt_is_kept_verbatim_even_though_nothing_calls_it() -> None:
    """``build_extract_prompt`` 在 TS 里也**没有调用点**（三步流程实际用的是
    "统计预筛 + 批量验证"）。保留它是为了与 TS 对齐 —— 与 W7-2 的 ``cover_token``
    形参同一个处置：内容留着，将来补那条路时只需接上调用点。"""
    out = M.build_extract_prompt("u1: 聊天")
    assert "yyds、xswl、nbcs、zqsg" in out
    assert '"content": "词条", "raw_content": "包含该词条的完整对话上下文原文"' in out
    assert out.endswith("现在请输出：\nu1: 聊天")


def test_prompt_builders_are_pure() -> None:
    """五个 builder 都只吃参数、不读模块状态（否则挖掘会随"上一个群"漂移）。"""
    assert M.build_validate_prompt("c", ["a"]) == M.build_validate_prompt("c", ["a"])
    assert M.build_infer_content_only_prompt("t") == M.build_infer_content_only_prompt("t")
    assert M.build_compare_prompt("t", "a", "b") == M.build_compare_prompt("t", "a", "b")


# ============================================================ §10 LLM 调用


class _Creds:
    """假 creds（不依赖 judge 模块解析 env）。"""
    def __init__(self, api_key: str = "k", base_url: str = "https://x/v1",
                 format: str = "openai") -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.format = format


def test_jargon_llm_returns_none_without_a_key() -> None:
    """★ 没 key ⇒ **静默返回 None**（TS ``jargon.ts:541``，在 ``model`` 检查**之前**）。

    顺序不能换：换了之后"这台机器压根没接 LLM"会变成**每轮挖一次报错**。"""
    assert _sync(lambda: M.jargon_llm("p", M.JargonCfg(model=""), _Creds(api_key="")))() is None


def test_jargon_llm_raises_when_the_model_is_unset() -> None:
    """★ 有 key、没 model ⇒ **抛**（``jargon.ts:543-548``）。故意不降级：
    静默跳过等于让运维以为在挖。代价是每轮一行 warn（见 ``_jargon_done``）。"""
    with pytest.raises(M.JargonConfigError):
        _sync(lambda: M.jargon_llm("p", M.JargonCfg(model=""), _Creds()))()


def test_jargon_llm_passes_the_cfg_through_to_call_judge(monkeypatch) -> None:
    seen = {}

    async def fake_call_judge(**kw):
        seen.update(kw)
        return '["yyds"]'

    from plugin.llm import judge as judge_mod
    monkeypatch.setattr(judge_mod, "call_judge", fake_call_judge)

    cfg = M.JargonCfg(model="deepseek-v4-flash", timeout_ms=4000)
    got = _sync(lambda: M.jargon_llm("提示词", cfg, _Creds()))()
    assert got == '["yyds"]'
    assert seen["model"] == "deepseek-v4-flash"
    assert seen["user_prompt"] == "提示词"
    assert seen["max_tokens"] == M.JARGON_MAX_TOKENS == 500       # TS 的 maxTokens: 500
    assert seen["timeout_ms"] == 4000
    assert seen["creds"].api_key == "k"
    # 不给 system prompt（判定式写成 `not in` —— 不该由"参数是 None"来假装覆盖）
    assert "system_prompt" not in seen or seen["system_prompt"] is None


def test_jargon_llm_swallows_any_failure(monkeypatch) -> None:
    """TS 的 ``catch { return null }`` —— 单次失败**绝不许**冒泡到挖掘编排之外。"""
    async def boom(**kw):
        raise RuntimeError("网络炸了")

    from plugin.llm import judge as judge_mod
    monkeypatch.setattr(judge_mod, "call_judge", boom)
    assert _sync(lambda: M.jargon_llm("p", M.JargonCfg(model="m"), _Creds()))() is None


# ============================================================ §11 三步推断


def _scripted(replies: list):
    """按调用次序返回预设文本的假 ``jargon_llm``（``None`` 表示这一步失败）。"""
    calls = []

    async def fake(prompt, cfg, creds):
        calls.append(prompt)
        return replies.pop(0) if replies else None
    return fake, calls


def test_infer_returns_the_context_meaning_when_the_two_views_differ(monkeypatch) -> None:
    fake, calls = _scripted([
        '{"meaning":"永远的神","no_info":false}',
        '{"meaning":"永远的神"}',
        '{"is_similar":false}',
    ])
    monkeypatch.setattr(M, "jargon_llm", fake)
    got = _sync(lambda: M.infer_jargon_meaning("yyds", "u1: yyds!", M.JargonCfg(model="m"),
                                               _Creds()))()
    assert got == "永远的神"
    assert len(calls) == 3
    assert all("yyds" in c for c in calls)


def test_the_fallthrough_returns_the_context_meaning_even_when_the_two_views_agree(
        monkeypatch) -> None:
    """★ 如实钉住一个**反直觉但逐字来自 TS** 的落点（``jargon.ts:726-727``）。

    三步走完、``is_similar === true``（"两视图一致 ⇒ 常规用法"）时，返回的是
    ``ctx_meaning`` —— **不是** ``""``。所以"这个不是黑话"在 TS/py2 里的落点不是
    "含义为空"，而是**只有 ``no_info`` 那一条**才返空串。

    第一版我按名字想当然写成"一致 ⇒ 空串"，用例当场变红（真值是"永远的神"）；
    我核了 TS 原文才发现是我的预期错了 —— 这条用例就是那次纠正的留痕。
    """
    fake, _ = _scripted([
        '{"meaning":"永远的神"}', '{"meaning":"永远的神"}', '{"is_similar":true}',
    ])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == "永远的神"


def test_empty_meaning_only_comes_from_step_one_failure_or_no_info(monkeypatch) -> None:
    """空串的两条来路（其余一律回上下文含义）—— 把"什么情况下查不到含义"问清楚，
    免得把上面那条反直觉的落点误读成"py2 忘了返空"。"""
    # 来路一：第一步失败
    fake, _ = _scripted([None])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == ""
    # 来路二：no_info（且没被判成"两视图不同"）
    fake, _ = _scripted(['{"no_info":true}', '{"meaning":"字面"}', '{"is_similar":true}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == ""
    # 反例：有含义、且两视图一致 ⇒ 照旧带回含义
    fake, _ = _scripted(['{"meaning":"永远的神"}', '{"meaning":"永远的神"}',
                         '{"is_similar":true}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == "永远的神"


def test_infer_falls_back_to_a_placeholder_when_the_context_says_nothing(monkeypatch) -> None:
    """``ctxMeaning || `${term}（群内特有用法）```：差异大但上下文推不出含义时，
    宁可存一句"它是群内特有用法"也不存空串（空串等于"不是黑话"）。"""
    fake, _ = _scripted(['{"no_info":true}', '{"meaning":"字面"}', '{"is_similar":false}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == "yyds（群内特有用法）"


def test_no_info_yields_empty_only_when_the_views_did_not_differ(monkeypatch) -> None:
    """``no_info`` 的判决排在 ``is_similar === false`` **之后** —— 顺序反了，
    一个"上下文里啥也看不出但两视图确实不同"的词会被丢掉。"""
    fake, _ = _scripted(['{"no_info":true}', '{"meaning":"字面"}', '{"is_similar":true}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == ""


def test_infer_step_one_failure_yields_empty(monkeypatch) -> None:
    """第一步就失败 ⇒ ``""``（连上下文含义都没有，无从降级）。"""
    fake, _ = _scripted([None])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == ""


def test_infer_step_two_and_three_failures_degrade_to_the_context_meaning(monkeypatch) -> None:
    """★ 降级是**回上一步的结果**，不是"一路回空串"。

    写成"任何一步失败就返回 ``""``"，现象是"挖到了、含义丢了" —— 库里躺着一条
    ``meaning=''`` 的记录，模型查到它只会照着空字符串编。"""
    fake, _ = _scripted(['{"meaning":"永远的神"}', None])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == "永远的神"

    fake, _ = _scripted(['{"meaning":"永远的神"}', '{"meaning":"字面"}', None])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == "永远的神"


def test_infer_tolerates_a_non_dict_reply(monkeypatch) -> None:
    """回包是数组/数字时 ``o?.meaning`` 是 ``undefined``（Python 侧 ``isinstance`` 判）——
    不许在这条路上抛 ``TypeError``（抛了整轮挖掘中止）。"""
    fake, _ = _scripted(["[1,2]", "42", '{"is_similar":false}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == "yyds（群内特有用法）"


def test_a_non_dict_third_reply_falls_through_instead_of_raising(monkeypatch) -> None:
    """★ 第三步回包是**数组**时也不能炸（``isinstance(o3, dict)`` 那道闸的意义）。

    TS 写的是 ``o3?.is_similar === false``：数组的 ``.is_similar`` 是 ``undefined``
    ⇒ 不等于 ``false`` ⇒ 落到末尾 ``return ctxMeaning``。py2 没有可选链，靠
    ``isinstance`` 判类型复现同一个结果。

    ⚠️ 补这条的理由：上面那条用例的第三步回包**是 dict** ⇒ ``isinstance`` 那道闸压根
    没被走到，拆掉它一样绿（变异 SURVIVED）。这里把第三步换成数组才钉得住 ——
    拆掉闸就是 ``AttributeError: 'list' object has no attribute 'get'``，
    整个词（乃至整轮）中止。
    """
    fake, _ = _scripted(['{"meaning":"永远的神"}', '{"meaning":"永远的神"}', "[1,2]"])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == "永远的神"


def test_a_fenced_similar_flag_still_counts(monkeypatch) -> None:
    """``is_similar`` 藏在 ```json 围栏里也要认得（真机上模型十有八九带围栏）。"""
    fake, _ = _scripted(['{"meaning":"永远的神"}', '{"meaning":"永远的神"}',
                         '```json\n{"is_similar": false}\n```'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == "永远的神"


def test_is_similar_zero_does_not_count_as_different(monkeypatch) -> None:
    """★ 严格 ``is False``：JSON 里的 ``0`` 在 TS 是 ``0 === false`` → **false**，
    Python 的 ``0 is False`` 也是 False ⇒ 两边都不算"不同"。

    判据要借 ``no_info: true`` 才看得出分歧：写成 ``not o3.get("is_similar")``
    （把 ``0`` 当假值用）就会走进"含义不同"那支、返回上下文含义而不是空串。"""
    # 严格判：0 不算"不同" ⇒ 落到 no_info ⇒ ""
    fake, _ = _scripted(['{"meaning":"永远的神","no_info":true}',
                         '{"meaning":"永远的神"}', '{"is_similar":0}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == ""
    # 对照：真的 false 才算"不同" ⇒ 返回上下文含义
    fake, _ = _scripted(['{"meaning":"永远的神","no_info":true}',
                         '{"meaning":"永远的神"}', '{"is_similar":false}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    assert _sync(lambda: M.infer_jargon_meaning("yyds", "c", M.JargonCfg(model="m"),
                                                _Creds()))() == "永远的神"


def test_infer_passes_the_no_context_placeholder(monkeypatch) -> None:
    fake, calls = _scripted(['{"meaning":"x"}', '{"meaning":"x"}', '{"is_similar":true}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    _sync(lambda: M.infer_jargon_meaning("yyds", "", M.JargonCfg(model="m"), _Creds()))()
    assert "（无上下文）" in calls[0]


# ============================================================ §12 挖掘编排与 store


class _Store:
    """假 store（``RepoJargonStore`` 那半见本节末尾 —— 那里必须走**真**映射）。"""
    def __init__(self, existing: list[str] | None = None, boom: bool = False) -> None:
        self.existing = [{"content": t, "meaning": ""} for t in (existing or [])]
        self.saved: list[dict] = []
        self.boom = boom

    def list_terms(self, group_id, limit=100):
        return self.existing

    def save_term(self, **kw):
        if self.boom:
            raise RuntimeError("库写失败")
        self.saved.append(kw)


def _seed(group_id: str, term: str, times: int) -> None:
    for _ in range(times):
        J.update_from_message(term, group_id, "u1")


def test_mine_returns_empty_when_there_are_no_candidates(monkeypatch) -> None:
    """候选池空（词频不够）⇒ 零 LLM 调用、零落库。"""
    async def no_llm(*a, **kw):
        raise AssertionError("不该调 LLM")

    monkeypatch.setattr(M, "jargon_llm", no_llm)
    store = _Store()
    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), store))()
    assert got == M.MineResult(extracted=0, confirmed=0)
    assert store.saved == []


def test_mine_sends_chinese_candidates_to_the_llm_and_saves_them(monkeypatch) -> None:
    """★ 编排层判据：纯中文的活跃群，候选**能**到 LLM 且确认后**落库**。

    这是 2026-10-19 放宽后的核心验收点 —— 对照旧行为（候选池被清空、零外呼、库里
    永远没有中文词条）。编排层的 ``should_filter_candidate`` 与 §4 是同一道判据。
    """
    _seed("g1", "测试一下", 6)          # 6 个中文 n-gram 候选，词频都够
    assert [c.term for c in J.get_jargon_candidates("g1")]      # 候选池不为空
    store = _Store()
    fake, calls = _scripted([
        '["测试一下"]',                              # ② 批量验证
        '{"meaning":"群内暗号"}', '{"meaning":"测试一下"}', '{"is_similar":false}',  # ③
    ])
    monkeypatch.setattr(M, "jargon_llm", fake)

    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(),
                                                store))()
    assert got.confirmed == 1
    assert len(calls) == 4                           # 1 次验证 + 3 步推断
    (row,) = store.saved
    assert row["content"] == "测试一下"
    assert row["meaning"] == "群内暗号"
    assert J.known_meaning("g1", "测试一下") == "群内暗号"


def test_standard_vocabulary_is_dropped_before_top_k(monkeypatch) -> None:
    """jieba 词典里的标准词在 top-K **之前**剔除，不能挤占黑话候选名额。

    无 jieba 时 ``is_standard_vocabulary`` 恒 False（降级路径）；这里用 monkeypatch
    钉住"有 jieba 时"的分支，避免测试机是否装了 jieba 让结果漂移。
    """
    _seed("g1", "价格", 6)
    _seed("g1", "真香", 6)
    monkeypatch.setattr(J, "is_standard_vocabulary", lambda t: t == "价格")
    terms = [c.term for c in J.get_jargon_candidates("g1")]
    assert "价格" not in terms
    assert "真香" in terms


def test_mine_writes_confirmed_terms_with_meanings(monkeypatch) -> None:
    _seed("g1", "yyds", 6)
    store = _Store()
    fake, calls = _scripted([
        '["yyds"]',                                  # ② 批量验证
        '{"meaning":"永远的神"}', '{"meaning":"永远的神"}', '{"is_similar":false}',   # ③ 三步
    ])
    monkeypatch.setattr(M, "jargon_llm", fake)

    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), store))()
    assert got == M.MineResult(extracted=1, confirmed=1)
    assert len(store.saved) == 1
    (row,) = store.saved
    assert row["group_id"] == "g1"
    assert row["content"] == "yyds"
    assert row["meaning"] == "永远的神"
    assert row["is_jargon"] is True
    assert row["raw_content"] == "yyds"              # 上下文样例（首条）
    assert len(calls) == 4                           # 1 次验证 + 3 步推断


def test_mine_keeps_the_meaning_cache_in_sync_with_the_db(monkeypatch) -> None:
    """★ ``remember_meaning`` 只在**落库成功之后**调 ⇒ 不变式是"缓存 ⊆ 库"。

    反过来的顺序（先记缓存再落库）在库写失败时会让查询工具**报出库里没有的含义** ——
    而且重启就消失，现场表现为"同一个词时有时无"。"""
    _seed("g1", "yyds", 6)
    store = _Store()
    fake, _ = _scripted(['["yyds"]', '{"meaning":"永远的神"}', '{"meaning":"永远的神"}',
                         '{"is_similar":false}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), store))()
    assert J.known_meaning("g1", "yyds") == "永远的神"


def test_mine_saves_a_row_even_when_the_inferred_meaning_is_empty(monkeypatch) -> None:
    """★ 如实钉住 TS 的一个后果：``meaning`` 为空（``no_info``）**照样落库、照样计数**。

    TS 的落库点只看 ``cfg.store`` 在不在，不看含义是否为空（``jargon.ts:684-691``）
    ⇒ "这个群确实在用、但含义推不出来"会在 ``wpp_jargon_terms`` 留一行
    ``meaning=''`` + ``is_jargon=1``。别顺手改成"空含义就不存"：那是与 TS 分叉，
    而且会让"已知是黑话、只是还不知道啥意思"这件事**从账上消失**。

    与之配套的反面：空含义**不进**含义缓存（``remember_meaning`` 挡空），所以
    "库里有一行、查询工具查不到含义"是本模块的**正常态**，不是丢数据。
    """
    _seed("g1", "yyds", 6)
    store = _Store()
    fake, _ = _scripted(['["yyds"]', '{"no_info":true}', '{"meaning":"字面"}',
                         '{"is_similar":true}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), store))()
    assert got == M.MineResult(extracted=1, confirmed=1)
    assert store.saved[0]["meaning"] == ""
    assert store.saved[0]["is_jargon"] is True
    assert J.known_meaning("g1", "yyds") == ""


def test_mine_does_not_cache_a_meaning_whose_save_failed(monkeypatch) -> None:
    _seed("g1", "yyds", 6)
    store = _Store(boom=True)
    fake, _ = _scripted(['["yyds"]', '{"meaning":"永远的神"}', '{"meaning":"永远的神"}',
                         '{"is_similar":false}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), store))()
    assert got == M.MineResult(extracted=1, confirmed=0)      # saved 计的是**库里真有的**
    assert J.known_meaning("g1", "yyds") == ""


def test_mine_returns_zero_extracted_when_the_validation_call_fails(monkeypatch) -> None:
    """★ 如实钉住一个**容易读错**的返回值：验证调用失败时 TS 返回
    ``{extracted: 0, confirmed: 0}`` —— **不是** ``{extracted: 候选数}``。

    按名字想当然（"提取了几个"）会在这里写错，而两边**都不报错**，只是日志里的
    数字不同 —— 直到有人拿这个数去算"转化率"。"""
    _seed("g1", "yyds", 6)
    fake, _ = _scripted([None])
    monkeypatch.setattr(M, "jargon_llm", fake)
    store = _Store()
    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), store))()
    assert got.extracted == 0 and got.confirmed == 0
    assert store.saved == []


def test_mine_reports_candidates_count_when_the_llm_confirms_nothing(monkeypatch) -> None:
    _seed("g1", "yyds", 6)
    fake, calls = _scripted(["[]"])
    monkeypatch.setattr(M, "jargon_llm", fake)
    store = _Store()
    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), store))()
    assert got == M.MineResult(extracted=1, confirmed=0)
    assert len(calls) == 1                    # 没确认 ⇒ 不再做推断（省 LLM 钱）
    assert store.saved == []


def test_mine_excludes_terms_already_in_the_store(monkeypatch) -> None:
    """① 的排除集来自**库**（``list_terms``）—— 漏了它，同一批词每轮重新计费。"""
    _seed("g1", "yyds", 6)
    fake, calls = _scripted([])
    monkeypatch.setattr(M, "jargon_llm", fake)
    store = _Store(existing=["yyds"])
    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), store))()
    assert got == M.MineResult() and calls == []


def test_mine_continues_when_the_store_cannot_be_read(monkeypatch) -> None:
    """库读不出来**不该让整轮挖掘中止** —— 代价只是"可能重复确认几个词"。"""
    _seed("g1", "yyds", 6)

    class _BadRead(_Store):
        def list_terms(self, group_id, limit=100):
            raise RuntimeError("库读失败")

    store = _BadRead()
    fake, _ = _scripted(['["yyds"]', '{"meaning":"m"}', '{"meaning":"m"}', '{"is_similar":false}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), store))()
    assert got.confirmed == 1


def test_mine_skips_one_term_whose_inference_explodes(monkeypatch) -> None:
    """单个词抛了 ⇒ 只跳过它，其余照存（TS 的 per-term try/catch）。"""
    terms = ["taa", "tbb"]
    for t in terms:
        _seed("g1", t, 5)
    replies = [json.dumps(terms), '{"meaning":"m"}', '{"meaning":"m"}', '{"is_similar":false}']
    fake, _ = _scripted(replies)

    async def flaky(prompt, cfg, creds):
        # 第一个词的"纯字面推断"那一步炸掉 ⇒ 那个词被跳过；第二个词走完三步
        if prompt == M.build_infer_content_only_prompt("taa"):
            raise RuntimeError("推断炸了")
        return await fake(prompt, cfg, creds)

    monkeypatch.setattr(M, "jargon_llm", flaky)
    store = _Store()
    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), store))()
    assert got.confirmed == 1
    assert [r["content"] for r in store.saved] == ["tbb"]


def test_mine_caps_validation_batch_at_fifteen_candidates(monkeypatch) -> None:
    """★ 一次最多送 15 个候选（``jargon.ts:663`` 的 ``.slice(0, 15)``）—— 控成本。
    ``maxCandidatesPerGroup`` 只管**候选池**大小，不是送 LLM 的上限。"""
    terms = [f"t{chr(97 + i)}x" for i in range(20)]        # "tax".."ttx"，都是合法候选
    for t in terms:
        _seed("g1", t, 5)
    seen = {}

    async def fake(prompt, cfg, creds):
        seen["prompt"] = prompt
        return "[]"

    monkeypatch.setattr(M, "jargon_llm", fake)
    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), _Store))()
    sent = json.loads(seen["prompt"].split("候选词列表：\n")[1].split("\n\n")[0])
    assert len(sent) == M.MAX_VALIDATE_TERMS == 15
    assert got.extracted == 15


def test_mine_caps_inference_at_five_terms(monkeypatch) -> None:
    """确认了 8 个，只推断前 5 个（``jargon.ts:676`` 的 ``.slice(0, 5)``，每词 3 次 LLM）。"""
    terms = [f"t{chr(97 + i)}x" for i in range(8)]
    for t in terms:
        _seed("g1", t, 5)
    replies = [json.dumps(terms)] + [
        '{"meaning":"m"}', '{"meaning":"m"}', '{"is_similar":false}'] * 8
    fake, calls = _scripted(replies)
    monkeypatch.setattr(M, "jargon_llm", fake)
    store = _Store()
    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), store))()
    assert len(store.saved) == M.MAX_INFER_TERMS == 5
    assert got.confirmed == 5
    assert len(calls) == 1 + 5 * 3


def test_mine_honours_max_candidates_per_group(monkeypatch) -> None:
    """``maxCandidatesPerGroup`` 从 cfg 流到候选池（不是硬编码 50）。"""
    for t in ("tax", "tbx", "tcx"):
        _seed("g1", t, 5)
    seen = {}

    async def fake(prompt, cfg, creds):
        seen["prompt"] = prompt
        return "[]"

    monkeypatch.setattr(M, "jargon_llm", fake)
    cfg = M.JargonCfg(model="m", max_candidates_per_group=2)
    _sync(lambda: M.mine_jargon_for_group("g1", cfg, _Creds(), _Store))()
    sent = json.loads(seen["prompt"].split("候选词列表：\n")[1].split("\n\n")[0])
    assert len(sent) == 2


def test_mine_works_without_a_store_but_saves_nothing(monkeypatch) -> None:
    """``store=None``（没接线时）⇒ 照常挖、不落库、``confirmed=0``。TS 同款
    （``if (cfg.store)`` 两道）。"""
    _seed("g1", "yyds", 6)
    fake, _ = _scripted(['["yyds"]', '{"meaning":"永远的神"}', '{"meaning":"永远的神"}',
                         '{"is_similar":false}'])
    monkeypatch.setattr(M, "jargon_llm", fake)
    got = _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), None))()
    assert got == M.MineResult(extracted=1, confirmed=0)
    assert J.known_meaning("g1", "yyds") == ""          # 没落库 ⇒ 也不进缓存


def test_mine_sends_the_recent_messages_as_context(monkeypatch) -> None:
    _seed("g1", "yyds", 6)
    M.record_message("g1", "u1", "第一句")
    M.record_message("g1", "u2", "第二句")
    seen = {}

    async def fake(prompt, cfg, creds):
        seen["prompt"] = prompt
        return "[]"

    monkeypatch.setattr(M, "jargon_llm", fake)
    _sync(lambda: M.mine_jargon_for_group("g1", M.JargonCfg(model="m"), _Creds(), _Store))()
    assert "u1: 第一句" in seen["prompt"] and "u2: 第二句" in seen["prompt"]


# ---- store 契约（★ 本项抓到的真 bug：``term`` → ``content`` 的映射）----


def test_repo_store_maps_the_db_term_column_to_the_declared_content_key(monkeypatch) -> None:
    """★★ 这一条钉的就是本项修掉的**真 bug**。

    store 契约（``jargon.ts:361``）声明 ``listTerms`` 返回 ``{content, meaning, isJargon}``，
    而 py2 的驱动函数 ``list_jargon`` 吐的是 DB 行 ``{group_id, term, meaning, frequency}``。
    适配层不映射，``exclude`` 就恒空集 ⇒ 已确认的词每轮重新送 LLM、重新 ``upsert``
    （那条 SQL 是 ``frequency = frequency + 1``，词频被反复灌水）—— 全链路无异常、无日志。

    ⚠️ 这条**必须**用真 ``RepoJargonStore`` + DB 形状的假行，不能用本节上面那个
    已经说了 ``content`` 的假 store —— 那样只会复述我们的假设，测不出契约缝隙。
    """
    from plugin.data import repo_messages
    db_row = {"group_id": "g1", "term": "yyds", "meaning": "永远的神", "frequency": 7}
    monkeypatch.setattr(repo_messages, "list_jargon", lambda *a, **kw: [dict(db_row)])

    out = M.RepoJargonStore("default").list_terms("g1")
    assert out == [{"content": "yyds", "meaning": "永远的神"}]
    assert out[0]["content"], "content 为空 ⇒ 排除集会恒空（就是上一条那个 bug）"


def test_a_row_without_content_or_term_degrades_to_an_empty_string(monkeypatch) -> None:
    """脏行（缺列）不许炸 —— 退化成空串，``exclude`` 里多一个 "" 是无害的。"""
    from plugin.data import repo_messages
    monkeypatch.setattr(repo_messages, "list_jargon", lambda *a, **kw: [{}])
    assert M.RepoJargonStore("default").list_terms("g1") == [{"content": "", "meaning": ""}]


def test_the_store_contract_actually_reaches_the_exclude_step(monkeypatch) -> None:
    """★ 端到端反向判据：真 store + DB 形状的行 ⇒ 候选**真的**被排除掉（零 LLM 调用）。

    这一条把"映射对不对"从"适配层的形状"提到"整轮挖掘发不发外呼"上 —— 契约错的时候
    唯一的现场表现就是"每轮都在重挖同一批词"，没有异常可看。"""
    from plugin.data import repo_messages
    monkeypatch.setattr(repo_messages, "list_jargon",
                        lambda *a, **kw: [{"group_id": "g1", "term": "yyds",
                                           "meaning": "", "frequency": 7}])
    _seed("g1", "yyds", 6)

    async def no_llm(*a, **kw):
        raise AssertionError("已入库的词不该再送 LLM")

    monkeypatch.setattr(M, "jargon_llm", no_llm)
    got = _sync(lambda: M.mine_jargon_for_group(
        "g1", M.JargonCfg(model="m"), _Creds(), M.RepoJargonStore("default")))()
    assert got == M.MineResult()


def test_repo_store_delegates_to_repo_messages(monkeypatch) -> None:
    """``RepoJargonStore`` 只是 ``repo_messages`` 的薄壳 —— 但**必须**把 ``conn`` 透传
    （显式注入那份连接时，``plugin.data.db`` 与探针用的 ``data.db`` 是两个模块实例，
    见 W2 的 ``repo_affection`` 同一个坑）。"""
    from plugin.data import repo_messages
    seen = {}

    def fake_list(account_id, group_id, limit, conn=None):
        seen["list"] = (account_id, group_id, limit, conn)
        return []

    def fake_upsert(account_id, group_id, content, raw, meaning, is_jargon, conn=None):
        seen["save"] = (account_id, group_id, content, raw, meaning, is_jargon, conn)

    monkeypatch.setattr(repo_messages, "list_jargon", fake_list)
    monkeypatch.setattr(repo_messages, "upsert_jargon", fake_upsert)

    sentinel = object()
    store = M.RepoJargonStore("default", conn=sentinel)
    assert store.list_terms("g1") == []
    store.save_term(group_id="g1", content="yyds", raw_content="ctx",
                    meaning="永远的神", is_jargon=True)
    assert seen["list"] == ("default", "g1", M.STORE_LIST_LIMIT, sentinel)
    assert seen["save"] == ("default", "g1", "yyds", "ctx", "永远的神", 1, sentinel)


def test_repo_store_maps_the_boolean_to_an_int(monkeypatch) -> None:
    """``is_jargon`` 进的是 ``tinyint`` 列 ⇒ ``True`` 要显式转 1（别指望驱动替我们转）。"""
    from plugin.data import repo_messages
    seen = {}

    def fake_upsert(account_id, group_id, content, raw, meaning, is_jargon, conn=None):
        seen["is_jargon"] = is_jargon

    monkeypatch.setattr(repo_messages, "upsert_jargon", fake_upsert)
    store = M.RepoJargonStore("a")
    store.save_term(group_id="g", content="t", raw_content="r", meaning="m", is_jargon=True)
    assert seen["is_jargon"] == 1
    store.save_term(group_id="g", content="t", raw_content="r", meaning="m", is_jargon=False)
    assert seen["is_jargon"] == 0


def test_the_store_protocol_and_the_repo_store_agree_on_the_method_names() -> None:
    """``JargonStore`` 是 Protocol（只用于类型），真实现是 ``RepoJargonStore`` ——
    两者方法名错位的话运行期是 ``AttributeError``（在 fire-and-forget 里被收尾回调
    吞成一行 warn）。这条用运行期属性把两者钉在一起。"""
    for name in ("list_terms", "save_term"):
        assert callable(getattr(M.RepoJargonStore("a"), name)), name
        assert hasattr(M.JargonStore, name), f"Protocol 缺 {name}"
