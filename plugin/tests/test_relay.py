"""接龙**解析**（W7-3）—— ``relay.ts`` 的后一半。

## 这一面的失效形态：**识别对了，但喂给模型的是原样那坨**

``is_relay_message``（老）只回答"是不是接龙"，然后 py2 给正文打个 ``[接龙]`` 前缀就交给
模型 —— 而 vendor 推来的正文是**一整坨** ``#接龙 💥抓住新机 … 红包100元 1. 门店＋型号
2. A店 watch6pro``：title、规则、条目全在一行里，中间那串"红包100元"还是接龙规则的一部分。
模型要自己从这坨里数出有几个人接了、分别是谁。

TS 的做法是**整条改写**（``handler.ts:547-549``）：``[接龙] <title>`` + 每行一条
``N. <条目>``。本项移植的就是这一段。

## 四个"看着一样、其实差一个字"的点（都写成判据）

1. **字面 ``\\n``**：vendor 会把 ``\\`` + ``n`` 两个字面写进 title 块。不先 replace 回真换行，
   按行切的正则只 match 第一行 ⇒ ``items`` 恒 1 条（v1 的真 bug，不是理论风险）。
2. **``\\d`` vs ``[0-9]``**：JS 的 ``\\d`` 是 ASCII 的。``"١٦. 张三"``（阿拉伯-印度数字）
   在 Python 里 ``int()`` 得通、在 JS 里是 ``NaN`` ⇒ 一写成 ``\\d`` 就会把**不是编号行**的
   东西当条目。
3. **``trim()`` 里的 BOM**：JS 从 ES5 起把 ``\\ufeff`` 算空白，Python 不算 ⇒
   ``"\\ufeff#接龙".strip()`` 在 Python 里前面那个 BOM **还在**，而"看起来完全一样"。
4. **空列表仍带尾换行**：TS 是 ``\\`[接龙] ${title}\\n\\` + items.join("\\n")`` ⇒ 一条都没接时
   结果是 ``"[接龙] title\\n"``。别"顺手 rstrip"——那是 TS 的原样。
"""

from __future__ import annotations

from pathlib import Path

from plugin.inbound import relay as R
from plugin.inbound.parser import (
    MT_APP, MT_CHAT_HISTORY, MT_TEXT, InboundPipeline, WppMessage,
)

ROOT = Path(__file__).resolve().parents[2]

#: 真实形状（打码）：**单行无换行**，title + 规则 + 条目全挤在一行里。
#: 这是老板 2026-08-14 亲自报上来的那条（"接龙内容单行无换行"）。
SINGLE_LINE = ("#接龙 🎯🎯🎯 ㊗某商户8月大卖，业绩长虹🥳 1. 门店＋型号 "
               "2. 金源北路GT7蓝 倪彩霞")
MULTI_LINE = "#接龙 周年庆\n1. 门店＋型号\n2. A店 GT7蓝\n3. B店 watch6pro"


def _msg(content: str = "", msg_type: int = MT_APP, raw: dict | None = None) -> WppMessage:
    return WppMessage(account_id="acct", msg_id="m1", from_wxid="wxid_friend",
                      to_wxid="wxid_self0000000000001", chatroom_id="123@chatroom",
                      content=content, msg_type=msg_type, raw=raw if raw is not None else {})


def _pipeline() -> InboundPipeline:
    return InboundPipeline("acct", {"selfWxid": "wxid_self0000000000001", "nickname": "小助理"})


# ============================================================ ① 编号与空白（两个 JS/Python 分叉）


def test_arabic_indic_digits_are_not_an_item_number():
    """★ ``\\d`` 是 JS 的 ASCII 集合，Python 的 ``\\d`` **不是**。

    ``int("١٦")`` 在 Python 里等于 16，而 JS 的 ``Number("١٦")`` 是 ``NaN`` ⇒
    写成 ``\\d`` 就会把这一行当成"第 16 条"，正文里冒出一条谁也没接过的条目。
    """
    assert R.parse_plain_list("١. 张三") == []
    assert R.parse_relay_text("接龙\n١. 张三").items == []


def test_a_bom_is_whitespace_like_js_trim_says_it_is():
    """``\\ufeff``（BOM）在 JS 里是空白、在 Python 里不是 —— 见模块 docstring ③。

    vendor 的 title/正文块实测常带 BOM；漏掉它，标题前面会挂一个**看不见**的字符，
    并且按行切时第一行会因为"不是以编号开头"被当成前导描述丢掉。
    """
    # ⚠️ 用 ``\ufeff`` 转义而不是**字面** BOM：字面 U+FEFF 在编辑器/工具链里会被静默吃掉，
    # 那时这三条断言会变成"看不出差别的绿"。
    assert R._js_trim("\ufeff" + "#接龙 x") == "#接龙 x"
    assert R.parse_plain_list("\ufeff" + "1. 张三")[0].text == "张三"
    assert R.parse_relay_text("\ufeff" + "#接龙 1. 张三").title == "#接龙 1. 张三"


def test_the_literal_backslash_n_becomes_a_real_newline():
    """★ v1 的真 bug：vendor 把字面 ``\\n``（``\\`` + ``n``）写进正文，不先换回来就整条塌成一行。

    判据在两处同时变：**title**（不换 ⇒ title 变成整坨 ``#接龙 周年庆\\n1. …``）和
    **条目正文**（不换 ⇒ 两个字面字符粘在 ``张三`` 后面，正则 ``\\s*`` 剥不掉它们）。
    """
    parsed = R.parse_relay_text("#接龙 周年庆\\n1. 张三\\n2. 李四")
    assert parsed.title == "#接龙 周年庆"
    assert [it.text for it in parsed.items] == ["张三", "李四"]


def test_a_trailing_title_tag_leaks_into_the_last_item_just_like_ts():
    """条目**长在 ``<title>`` 块里面**时，最后一个条目会带上 ``</title>``。

    ⚠️ 这不是本实现的 bug：``parseRelayText`` 把**整段**（含标签）交给 ``parsePlainList``
    （``relay.ts:66-72`` 逐字如此）⇒ TS 生产上渲染出的也是 ``2. 李四</title>``。
    钉住它是为了**别顺手修** —— 一旦在这里 ``strip("</title>")``，py2 就与 TS 分叉了，
    而分叉的那一侧恰好是"我们看着更干净"的那侧，最容易被当成改进。
    """
    parsed = R.parse_relay_text("<title>#接龙 周年庆\\n1. 张三\\n2. 李四</title>")
    assert [it.text for it in parsed.items] == ["张三", "李四</title>"]


def test_crlf_is_normalised_before_splitting():
    assert [it.text for it in R.parse_relay_text("#接龙 x\r\n1. A\r\n2. B").items] == ["A", "B"]


# ============================================================ ② title


def test_the_title_comes_from_the_title_block_with_the_group_prefix_stripped():
    """群前缀是**两段**（``wxid:\\n昵称:\\n正文``）—— 与 TS ``content.ts:7`` 同一条正则。

    ⚠️ 一条用例曾经拿**一段** ``wxid_abc:\\n#接龙 周年庆`` 当"前缀"来断言被剥掉，
    那是 py2 旧实现的宽松行为；TS 的正则要求两段，一段会原样返回。按 TS 对齐。
    """
    parsed = R.parse_relay_text("<title>wxid_abc:\n阿丽:\n#接龙 周年庆</title>\n1. A")
    assert parsed.title == "#接龙 周年庆"


def test_a_single_colon_segment_is_not_a_group_prefix():
    """一段不是前缀（TS 同）—— 剥了就是把昵称当正文吃掉。"""
    parsed = R.parse_relay_text("<title>wxid_abc:\n#接龙 周年庆</title>\n1. A")
    assert parsed.title == "wxid_abc:\n#接龙 周年庆"


def test_the_title_falls_back_to_the_first_line_when_there_is_no_title_block():
    """v1.3.57：真实接龙 type=49 常没有 ``<title>``。

    没有这个兜底，**所有**接龙的 title 都是空 ⇒ 节流 key 相同 ⇒ 不同的接龙互相节流
    （现象是"后来的接龙不回复"，而日志里看不出被谁挡了）。
    """
    assert R.parse_relay_text(MULTI_LINE).title == "#接龙 周年庆"
    assert R.parse_relay_text(SINGLE_LINE).title == SINGLE_LINE


def test_an_empty_body_yields_an_empty_title():
    assert R.parse_relay_text("").title == ""
    assert R.parse_relay_text("").items == []


# ============================================================ ③ 条目切分


def test_a_single_line_relay_still_splits_into_items():
    """★ 老板 2026-08-14 报上来的那条：vendor 整条接龙**没有换行**。

    按 ``\\n`` 分行只会得到 1 行、又不以编号开头 ⇒ 旧实现 ``items=[]``（一条都看不到）。
    """
    parsed = R.parse_relay_text(SINGLE_LINE)
    assert [it.index for it in parsed.items] == [1, 2]
    assert parsed.items[1].text == "金源北路GT7蓝 倪彩霞"


def test_the_number_in_the_title_never_becomes_an_item():
    """``#接龙 3. 周年庆 1. 门店＋型号 2. …`` —— 条目**总是从 ``1. `` 起**。

    不剥前缀就会把 title 里的 ``3.`` 当成一条接龙，还排在第一批（序号乱）。
    """
    parsed = R.parse_relay_text("#接龙 3. 周年庆 1. 门店＋型号 2. 倪彩霞")
    assert [it.index for it in parsed.items] == [1, 2]
    assert [it.text for it in parsed.items] == ["门店＋型号", "倪彩霞"]


def test_a_continuation_line_joins_its_item_with_a_space():
    """条目内换行（``2. 倪彩霞`` / ``gt7``）算**同一条**，用空格接。

    用 ``\\n`` 接会让整段正则（``.`` 不跨行）匹配不到，那条直接被丢掉。
    """
    parsed = R.parse_relay_text("#接龙 周年庆\n1. 门店＋型号\n2. 倪彩霞\ngt7 蓝色")
    assert [it.text for it in parsed.items] == ["门店＋型号", "倪彩霞 gt7 蓝色"]


def test_a_leading_description_is_not_an_item():
    parsed = R.parse_relay_text("#接龙 周年庆 门店＋型号 1. 张三 2. 李四")
    assert [it.text for it in parsed.items] == ["张三", "李四"]


def test_the_item_keeps_the_whole_remaining_text():
    """保守解析（老板 2026-08-11 指正）：**不假设首词=昵称** —— 昵称可改可删，
    整段留给 AI 自己判断。这条钉住"别顺手再切一刀"。"""
    parsed = R.parse_relay_text("#接龙 x\n1. 金源北路GT7蓝 倪彩霞 13800000000")
    assert parsed.items[0].text == "金源北路GT7蓝 倪彩霞 13800000000"


def test_strip_title_prefix_keeps_the_text_when_the_prefix_is_at_index_zero():
    """TS 写的是 ``idx > 0 ? slice : text``。``idx == 0`` 与原串相同是**巧合等价**，
    但形状要留着 —— 这道边界只有真去改 ``>= 0`` 的人才看得出来。"""
    assert R.strip_title_prefix("1. 张三") == "1. 张三"
    assert R.strip_title_prefix("#接龙 x 1. 张三") == "1. 张三"


# ============================================================ ④ <recorditem> 路径


def test_record_items_take_their_index_from_position_not_from_content():
    parsed = R.parse_relay_text(
        "<title>#接龙 周年庆</title>"
        "<recorditem><username>wxid_a</username><nickname>张三</nickname><text>门店＋型号</text></recorditem>"
        "<recorditem><username>wxid_b</username><nickname>李四</nickname><text>GT7蓝</text></recorditem>")
    assert [(it.index, it.wxid, it.nickname, it.text) for it in parsed.items] == [
        (1, "wxid_a", "张三", "门店＋型号"),
        (2, "wxid_b", "李四", "GT7蓝"),
    ]


def test_a_record_item_missing_fields_yields_empty_strings_not_none():
    """TS 里缺字段是 ``undefined`` ⇒ 落到 Python 是 ``""``（**不是 None**）——
    正文渲染会 ``f"{it.text}"``，写 None 就会在群里印一个 "None"。"""
    parsed = R.parse_relay_text("<recorditem><text>只有正文</text></recorditem>")
    assert (parsed.items[0].wxid, parsed.items[0].nickname) == ("", "")


def test_record_items_win_over_the_plain_list_path():
    """两条路**只走一条**：有 ``<recorditem>`` 时不该再去按行数编号。"""
    parsed = R.parse_relay_text(
        "#接龙 x\n1. 不该出现\n<recorditem><text>真正的条目</text></recorditem>")
    assert [it.text for it in parsed.items] == ["真正的条目"]


# ============================================================ ⑤ 渲染 + 副作用


def test_the_rendered_text_is_the_ts_shape():
    assert R.render_relay_text(R.parse_relay_text(MULTI_LINE)) == (
        "[接龙] #接龙 周年庆\n1. 门店＋型号\n2. A店 GT7蓝\n3. B店 watch6pro")


def test_an_empty_item_list_still_renders_the_trailing_newline():
    """TS ``\\`[接龙] ${title}\\n\\` + [].join("\\n")`` ⇒ 尾换行是原样，别 rstrip。"""
    assert R.render_relay_text(R.RelayParseResult(title="周年庆", items=[])) == "[接龙] 周年庆\n"


def test_apply_relay_render_rewrites_the_body_and_marks_it():
    msg = _msg(MULTI_LINE)
    assert R.apply_relay_render(msg) is True
    assert msg.content == "[接龙] #接龙 周年庆\n1. 门店＋型号\n2. A店 GT7蓝\n3. B店 watch6pro"
    assert msg.media["relay_rendered"] is True


def test_apply_relay_render_leaves_non_relay_messages_alone():
    msg = _msg("你好啊", msg_type=MT_TEXT)
    assert R.apply_relay_render(msg) is False
    assert msg.content == "你好啊"
    assert "relay_rendered" not in msg.media


def test_apply_relay_render_never_raises_and_keeps_the_body(monkeypatch):
    """★ TS 的 ``try/catch``：解析炸了只 warn、正文保持原样。

    接龙解析坏了不该让整条消息消失 —— 而若"先写键、再改正文"，异常发生在中间时
    那条消息会**连标记都没有**（``_media_tag`` 已被抑制）。所以顺序是正文在前。
    """
    def boom(_raw):
        raise ValueError("坏数据")

    monkeypatch.setattr(R, "parse_relay_text", boom)
    msg = _msg(MULTI_LINE)
    assert R.apply_relay_render(msg) is False
    assert msg.content == MULTI_LINE, "解析失败时正文必须逐字不变"
    assert "relay_rendered" not in msg.media


def test_a_legacy_chat_history_message_is_rendered_too():
    """53（旧 chat-history）是 TS 里的历史兼容分支 —— 它同样要改写。"""
    msg = _msg(MULTI_LINE, msg_type=MT_CHAT_HISTORY)
    assert R.apply_relay_render(msg) is True
    assert msg.content.startswith("[接龙] ")


# ============================================================ ⑥ 与正文管线接起来（不重复打标记）


def test_the_rendered_relay_reaches_the_prompt_with_exactly_one_marker():
    """改写后的正文进 ``prepare_text`` 时**不许**再被加一个 ``[接龙]`` 前缀。"""
    msg = _msg(MULTI_LINE)
    R.apply_relay_render(msg)
    text = _pipeline().prepare_text(msg)
    assert text == "[接龙] #接龙 周年庆\n1. 门店＋型号\n2. A店 GT7蓝\n3. B店 watch6pro", text
    assert text.count("[接龙]") == 1, text


def test_without_the_render_the_old_prefix_behaviour_is_untouched():
    """★ 回归判据：没改写过的接龙（老路径）正文与加这一项之前**逐字相同**。"""
    text = _pipeline().prepare_text(_msg(MULTI_LINE))
    assert text == f"[接龙] {MULTI_LINE}", text


def test_a_legacy_chat_history_relay_also_gets_exactly_one_marker():
    """★ 53（旧 chat-history）**也**要抑制。

    ``_MEDIA_TAGS[53]`` **就是** ``"[接龙]"``（``parser.py:550``）⇒ 只对 49 抑制的话，
    53 这条会拿到 ``[接龙] [接龙] title …``。把判据写成"检查对象是 49"是最自然的那种
    写错法，而它只在 53 那条路上现形。"""
    msg = _msg(MULTI_LINE, msg_type=MT_CHAT_HISTORY)
    R.apply_relay_render(msg)
    text = _pipeline().prepare_text(msg)
    assert text.count("[接龙]") == 1, text


def test_the_suppression_answers_with_a_string_not_none():
    """★ 抑制的返回**必须是 ``str``**，不是 None。

    ``prepare_text`` 的 ``if tag:`` 对这两个同款（都是假值）⇒ 从正文上看不出区别，
    所以这条只能**直接问那个函数**。写成 None 之后，下一个消费者（``tag.strip()`` /
    ``f"{tag} …"``，或将来某处判 ``isinstance(tag, str)``）就会炸在别人的代码里。
    """
    msg = _msg(MULTI_LINE)
    R.apply_relay_render(msg)
    tag = _pipeline()._media_tag(msg, msg.content)
    assert tag == "" and isinstance(tag, str), repr(tag)
