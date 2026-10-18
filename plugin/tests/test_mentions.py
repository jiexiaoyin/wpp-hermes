"""@ 提及层（A7）—— ``plugin/inbound/parser.py`` 的六个 pattern + 四个函数，此前**零判据**。

## 为什么这一层必须单独钉住

它是"群里到底叫没叫 bot"的**唯一判据**：``inbound/triggers.py`` 的 ⑦ 一旦判真就是
**100% 必回**（``group_mention``，绕开心流阈值）。两个方向判错都是事故 ——

* **漏判** ⇒ bot 在群里彻底哑掉（老板问一句没人应，且没有任何日志会喊）；
* **误判** ⇒ bot 抢话。

而这一层此前**一条测试都没有**：``extract_at_user_list`` / ``strip_at_mentions`` /
``normalize_mention`` 三个导出函数在 ``plugin/tests/`` 里搜不到一次引用。

## 移植期的形态差异全在这一层

pattern 5/6 是给 "selfWxid 是**微信号**（如老板本人的 ``q139198824``）不是
``wxid_``" 补的（TS 注释原文：之前硬要求 ``wxid_`` 前缀漏判）。这类"真实存在、
但只在生产里见过"的输入，没有测试就只能靠回忆 —— 所以下面按**形态**逐条钉。

对齐物 TS ``src/inbound/parser/mention.ts``。**三处有意/必要的差异**各自单列用例：

1. ``extract_at_user_list`` **不做** ``isValidAtUser`` 过滤（见函数 docstring）；
2. ``strip_at_mentions`` 的 ``\\b`` 走 ``re.ASCII`` —— JS 的 ``\\w`` 是 ASCII 的，
   Python 默认 Unicode，不给 flag 就会漏掉 ``你好wxid_bot`` 这种紧贴中文的场景
   （实测 TS 剥掉、不指定 ASCII 的 Python 不剥）；
3. ``is_bot_mentioned_by_text`` 的 ``<atuserlist>`` 昵称兜底是本树**新增**的
   （TS 没有），且它同样遵守"正文进正则前先截 4096"。
"""

from __future__ import annotations

import time

import pytest

from plugin.inbound.parser import (
    _AT_PATTERNS, _RE_MAX_TEXT, InboundPipeline, WppMessage, extract_at_user_list,
    is_bot_mentioned_by_text, normalize_mention, strip_at_mentions,
)

BOT = "wxid_bot"
#: 老板本人微信号形态的 bot（真实存在：TS 那条 pattern 就是为它加的）。
BOT_ALIAS = "q139198824"
NICK = "益融小助理"


# ====================================================== §1 六种形态的提取


@pytest.mark.parametrize("content,want", [
    # ① `wxid_x@nick`（群聊 @ 通知最常见的一种）
    ("wxid_abc@张三 你好", ["wxid_abc"]),
    # ② 中文 @ + wxid
    ("@wxid_abc 在吗", ["wxid_abc"]),
    # ③ XML 风格 `<at user=...>`
    ('<at user="wxid_abc">你好</at>', ["wxid_abc"]),
    # ④ 群 XML `<atuserlist><username>wxid_...`
    ("<atuserlist><item><username>wxid_abc</username></item></atuserlist>", ["wxid_abc"]),
    # ⑤ ★ 微信号形态 —— self_wxid 是微信号（非 wxid_）时的唯一出路
    ("<atuserlist><item><username>q139198824</username></item></atuserlist>", ["q139198824"]),
    # ⑥ ★ 正文里的 `@微信号`
    ("@q139198824 帮我看看这个", ["q139198824"]),
])
def test_each_vendor_mention_shape_is_extracted(content: str, want: list[str]) -> None:
    """六条 pattern 各自的**正例** —— 少一条就有整类群消息叫不动 bot。"""
    assert extract_at_user_list(content) == want


@pytest.mark.parametrize("content", ["", "普通消息，没有任何 @", "@所有人 开会", "wxid", "@wxid"])
def test_shapes_that_must_not_extract_anything(content: str) -> None:
    """反例：空串 / 无 @ / 中文 ``@所有人``（ASCII pattern 不该误吞）都不许出候选。"""
    assert extract_at_user_list(content) == []


def test_duplicates_are_collapsed_and_order_is_kept() -> None:
    """去重**保序**：同一人被多条 pattern 命中（② 与 ⑥ 会同时命中微信号）只留一份。"""
    assert extract_at_user_list("@wxid_b 和 @wxid_a 还有 @wxid_b") == ["wxid_b", "wxid_a"]
    # 微信号形态同时被 ⑤/⑥ 命中也要去重
    got = extract_at_user_list("<atuserlist><username>q139198824</username></atuserlist>@q139198824")
    assert got == ["q139198824"], got


def test_the_alias_form_is_why_the_ts_filter_is_deliberately_dropped() -> None:
    """★ 有意不齐 ①：微信号形态**必须**出现在出参里，否则重演 TS 的"漏判"。

    TS ``extractAtUserList`` 出参前过 ``isValidAtUser``（``^(wxid_|gh_)``），
    于是它的 ⑤/⑥ 两条 pattern 的产出**恒被自己滤掉**。本树不滤 —— 这条用例是
    那个决定的**唯一执行者**：谁哪天"顺手对齐"加上这个过滤，这里必须红。
    """
    assert extract_at_user_list("@q139198824 你好") == ["q139198824"], \
        "微信号形态被滤掉了 —— 老板本人的微信在群里被 @ 会判成没叫（TS 注释里的原话漏判）"
    assert extract_at_user_list("<atuserlist><username>q139198824</username></atuserlist>") \
        == ["q139198824"]
    # 端到端：过滤一旦加回，这个账号在群里就**彻底哑掉**（这条是它的报警器）
    assert is_bot_mentioned_by_text("@q139198824 你好", BOT_ALIAS, NICK) is True


def test_a_junk_at_target_is_returned_but_cannot_be_mistaken_for_the_bot() -> None:
    """不齐的**代价**也要钉住：``@everyone`` 会进列表。

    这不是 bug（去掉过滤的必然结果），但调用方**不许**把它当 wxid 用，而且它
    **不许**造成误判 —— 假阳性要求候选恰好等于 bot 的 wxid，那得正文里真写了它。
    """
    assert extract_at_user_list("@everyone 开会") == ["everyone"]
    assert is_bot_mentioned_by_text("@everyone 开会", BOT, NICK) is False


# ====================================================== §2 4096 截断（防 ReDoS）


def test_a_mention_past_the_cap_is_not_extracted() -> None:
    """4096 是**硬闸**：超过的尾巴不参与匹配（对齐 TS ``safeMatchAll`` 的截断）。"""
    assert extract_at_user_list("x" * (_RE_MAX_TEXT - 10) + "@wxid_late") == ["wxid_late"]
    assert extract_at_user_list("x" * _RE_MAX_TEXT + "@wxid_late") == []


def test_a_pathological_body_does_not_stall_the_scan() -> None:
    """ReDoS 烟感：20 万字符的病态正文必须**秒回**。

    它没有 ``(a+)+`` 那类形状，真正的保护是"进正则前先截 4096" —— 所以这里
    同时验两件事：耗时上界（松到 3s，这是烟感不是基准）与结果正确。
    """
    body = "@" + "a" * 200_000 + " <atuserlist>" + "<username>" * 5_000
    t0 = time.monotonic()
    got = extract_at_user_list(body)
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"截断没生效？20 万字符扫了 {elapsed:.2f}s"
    # 出参必须**只能**来自前 4096 个字符：长串被切到 4095 个 a（`@` 占 1 位）
    assert got == ["a" * (_RE_MAX_TEXT - 1)], got


# ====================================================== §3 strip_at_mentions


def test_at_mentions_of_the_bot_are_stripped() -> None:
    assert strip_at_mentions("@wxid_bot 你好", BOT) == "你好"
    assert strip_at_mentions("wxid_bot 在吗", BOT) == "在吗"
    assert strip_at_mentions("在吗 @wxid_bot", BOT) == "在吗"


def test_a_bot_id_glued_to_chinese_is_still_stripped() -> None:
    """★ 有意差异 ②：``\\b`` 必须按 **ASCII** 判定（JS 语义）。

    Python 默认 Unicode 语义下中文算词字符 ⇒ 没有词边界 ⇒ 整条不剥。生产里
    群消息正文是 ``wxid_x:\\n...`` 这种混排，紧贴中文的提及并不罕见。
    """
    assert strip_at_mentions("你好wxid_bot 在吗", BOT) == "你好在吗"
    assert strip_at_mentions("wxid_bot在吗", BOT) == "在吗"


def test_a_longer_identifier_sharing_the_prefix_is_handled_like_ts() -> None:
    """两条路的边界行为**不同**，且都与 TS 逐字一致（钉住，免得当 bug"顺手改"）。

    * 裸形态 ``wxid_bot2`` 受 ``\\b`` 保护，**原样保留** —— 对；
    * ``@`` 形态的 ``@wxid_bot\\s*`` **没有**词边界（TS 也没有），于是
      ``@wxid_bot2`` 会被吃掉前缀只剩 ``2 在吗`` —— 是 TS 同款的坑。

    在没有生产调用方（见函数 docstring）的前提下，"跟 TS 一致"比"就地修好"更值：
    哪天真接上，两边至少不会各错各的。
    """
    assert strip_at_mentions("wxid_bot2 在吗", BOT) == "wxid_bot2 在吗"
    assert strip_at_mentions("@wxid_bot2 在吗", BOT) == "2 在吗"
    # 真·提及仍然整段剥掉
    assert strip_at_mentions("@wxid_bot 在吗", BOT) == "在吗"


@pytest.mark.parametrize("bot,content", [("", "@wxid_bot 你好"), (BOT, "")])
def test_empty_inputs_pass_through_unchanged(bot: str, content: str) -> None:
    """没有 bot_wxid（配置缺失）时**原样返回** —— 不能把正文清成空。"""
    assert strip_at_mentions(content, bot) == content


def test_the_tail_past_the_cap_is_dropped_not_just_the_mention() -> None:
    """★ 钉住"有损"这个事实：≥4096 的正文**尾巴整段没了**（TS 同）。

    正因为它有损，它只能喂"看正文"的路径、不能落库；本树也因此没有生产调用方
    （@ 判定只需要 :func:`is_bot_mentioned_by_text`，不需要清洗正文）。
    将来真要接上，必须先看这条。
    """
    long = "哈哈 " + "x" * _RE_MAX_TEXT + " 尾巴"
    got = strip_at_mentions(long, BOT)
    assert "尾巴" not in got, "超过 4096 的部分被截掉了（TS 同）—— 接上前先想清楚落库"
    assert len(got) <= _RE_MAX_TEXT


# ====================================================== §4 is_bot_mentioned_by_text


@pytest.mark.parametrize("content,bot,why", [
    ("@wxid_bot 你好", BOT, "① 正文直写 @wxid"),
    (" wxid_bot 请回一下", BOT, "② 空格 + wxid（vendor 只给裸 id 的形态）"),
    ('<at user="wxid_bot">你好</at>', BOT, "③ XML 提及"),
    ("<atuserlist><username>wxid_bot</username></atuserlist>", BOT, "④ 群通知 XML"),
    # ⑤ 的两条：bot 的 self_wxid **就是**微信号（老板本人那种账号）
    ("@q139198824 你好", BOT_ALIAS, "⑤ 微信号形态的 bot"),
    (" q139198824 请回一下", BOT_ALIAS, "⑤b 微信号的裸形态（空格分隔）"),
    ("<atuserlist><username>q139198824</username></atuserlist>", BOT_ALIAS, "⑤c 微信号的群通知 XML"),
    ("@益融小助理 你好", BOT, "⑥ 昵称带 @"),
    ("<atuserlist><item><username>益融小助理</username></item></atuserlist>", BOT,
     "⑦ XML 里只有昵称、没有 wxid（本树的结构性兜底）"),
])
def test_ways_the_bot_counts_as_mentioned(content: str, bot: str, why: str) -> None:
    """九条**都算叫了**。少任何一条 ⇒ 那一类消息在群里永远哑掉（且无日志）。"""
    assert is_bot_mentioned_by_text(content, bot, NICK) is True, why


@pytest.mark.parametrize("content,why", [
    ("", "空正文"),
    ("大家开会吧", "没有提到任何人"),
    ("益融小助理昨天说了什么", "★ 只顺口提昵称、没有 @ —— 旧实现（裸子串）在这里误判"),
    ("<atuserlist><username>张三</username></atuserlist>", "群通知 XML 里是**别人**"),
    ("@everyone 开会", "非 wxid 的 @ 目标不许造成误判"),
])
def test_ways_the_bot_does_not_count_as_mentioned(content: str, why: str) -> None:
    """反例 —— 尤其第三/五条：**误判=抢话**，是老板最不想要的那种毛病。"""
    assert is_bot_mentioned_by_text(content, BOT, NICK) is False, why


def test_bare_nickname_without_at_is_deliberately_not_a_hit() -> None:
    """★ 行为变更的**唯一执行者**：昵称分支必须带 ``@``。

    旧实现末行是 ``self_nickname in content``（裸子串）—— 群里只要有人提到
    "益融小助理"这四个字，bot 就跳出来回话，这是纯噪声。本树跟 TS 走。
    """
    assert is_bot_mentioned_by_text("益融小助理这个功能不错", BOT, NICK) is False
    assert is_bot_mentioned_by_text("@益融小助理 这个功能不错", BOT, NICK) is True


def test_a_longer_nickname_that_starts_with_ours_is_not_a_hit() -> None:
    """★ 昵称匹配要边界：``@益融小助理2`` 是**别人**（前缀撞名的 bot 会互相抢答）。

    裸子串版本对 ``@益融小助理2`` 判真 ⇒ 两个名字互为前缀的 bot 在同一个群里
    谁都以为在叫自己。边界只看 ASCII 字母/数字/下划线（``(?![A-Za-z0-9_])``）——
    紧跟中文仍算命中（``@益融小助理在吗`` 是常见的"@完直接说话"写法）。
    """
    assert is_bot_mentioned_by_text("@益融小助理2 你好", BOT, NICK) is False
    assert is_bot_mentioned_by_text("@益融小助理bot 你好", BOT, NICK) is False
    assert is_bot_mentioned_by_text("@益融小助理 你好", BOT, NICK) is True
    assert is_bot_mentioned_by_text("@益融小助理在吗", BOT, NICK) is True


def test_a_missing_bot_wxid_still_allows_the_nickname_route() -> None:
    """``selfWxid`` 没配时不许**整条短路**：昵称分支还救得回来。"""
    assert is_bot_mentioned_by_text("@益融小助理 你好", "", NICK) is True
    assert is_bot_mentioned_by_text(" wxid_bot 你好", "", NICK) is False


def test_the_xml_nickname_fallback_also_respects_the_cap() -> None:
    """兜底那条正则是**唯一**接触未截断正文的地方，这条钉住它已收口。"""
    xml = "<atuserlist><username>益融小助理</username></atuserlist>"
    assert is_bot_mentioned_by_text("x" * 10 + xml, BOT, NICK) is True
    assert is_bot_mentioned_by_text("x" * _RE_MAX_TEXT + xml, BOT, NICK) is False


def test_all_six_patterns_are_still_wired() -> None:
    """六条 pattern 缺一不可（它们是上面正例的骨架，防止被"精简"掉）。"""
    assert len(_AT_PATTERNS) == 6


# ====================================================== §5 normalize_mention + 接线


def test_normalize_mention_strips_every_bot_form() -> None:
    assert normalize_mention("@wxid_bot 你好", BOT, NICK) == "你好"
    assert normalize_mention("wxid_bot@张三: 你好", BOT, NICK) == "张三: 你好"
    assert normalize_mention("@益融小助理 你好", BOT, NICK) == "你好"


def test_normalize_mention_only_touches_the_bot() -> None:
    """★ 只删"叫 bot"的那一段：别人被 @ 的提及、正文一律不许动。"""
    assert normalize_mention("@wxid_bot 你好 @wxid_other 一起看", BOT, NICK) \
        == "你好 @wxid_other 一起看"


@pytest.mark.parametrize("wxid,nick", [("", ""), (BOT, ""), ("", NICK)])
def test_normalize_mention_tolerates_missing_config(wxid: str, nick: str) -> None:
    """配置缺任何一项都不许炸（也不许把正文清空）。"""
    assert "你好" in normalize_mention("@wxid_bot @益融小助理 你好", wxid, nick)
    assert normalize_mention("", wxid, nick) == ""


def test_prepare_text_actually_calls_normalize_mention() -> None:
    """★ 接线（"0 与没接上分不开"）：函数写得再对，不在路上就等于没有。

    ``prepare_text`` 是喂给 agent 的那份正文的**唯一**出口 —— 这里直接走管线，
    证明去提及真的发生在路上，而不是只存在于孤立的函数里。
    """
    msg = WppMessage(
        account_id="acc", msg_id="m1", from_wxid="wxid_a",
        chatroom_id="123@chatroom", content="@wxid_bot 帮我看下价格",
    )
    pipe = InboundPipeline("acc", {"selfWxid": BOT, "nickname": NICK})
    assert pipe.prepare_text(msg) == "帮我看下价格"
