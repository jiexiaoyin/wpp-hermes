"""v1（``wechatpad.message.v1``）形态的解析兜底 —— 三处错了都**不出声**。

vendor 的 business 回调那条路推的是**另一套字段名**（TS ``parseV1Message`` 的注释里
写着 v1 schema：``{content, conversation_id, created_at, direction, id, is_group, kind,
local_id, recipient_id, sender_id, status, type}``）。字段名对不上不会报错 —— 只会
"当成别的东西"，这正是本文件要钉住的三条：

1. **``type``（A1）**：不认它 ⇒ 消息类型恒为 ``MT_TEXT`` ⇒ 图片/语音/文件**全被判成
   文本**，后面所有按类型分岔的判据（媒体标记、卡片、接龙、红包）跟着一起错；
2. **群 id 只在 ``is_group`` 为真时才取（A2）**：v1 推群里某个人发的消息而 ``is_group``
   缺失时，**群消息被当成私聊** —— peer 变成某个人（心流按人建会话、回复发成私聊），
   群策略/群白名单/群预算整条绕开；
3. **没有 id 的消息共用空 msg_id（A3）**：去重键是 ``f"{acct}:{msg_id}"``（见
   ``platform/adapter.py`` 的 ``self._dedup.is_duplicate(...)``），空 id ⇒ 第一条之后
   **所有**无 id 消息都被当成重复而**静默丢弃**（09-27 接龙整晚消失那一类失效的反面：
   一类是吞接龙，一类是吞一切没 id 的）。

对齐物是 TS ``src/inbound/parser.ts`` 的 ``parseV1Message``：``num(msg.type, 1)``、
``groupCandidates = [conversationId, recipientId, senderId].filter(endsWith("@chatroom"))``、
``msgId: rawMsgId || `${ts}-${random}``。
"""

from __future__ import annotations

import json

import pytest

from plugin.inbound.media import is_file_message
from plugin.inbound.parser import (
    MT_APP, MT_IMAGE, MT_TEXT, InboundPipeline, _extract_msg_type, parse_message,
)

ACCT = "test-acct"


def _v1(**kw) -> dict:
    """一条最小 v1 私聊消息（``id`` 给死值，免得 A3 的合成 id 干扰别的用例）。"""
    base = {
        "content": "hi",
        "sender_id": "wxid_sender",
        "conversation_id": "wxid_sender",
        "created_at": 1700000000,
        "id": "m1",
    }
    base.update(kw)
    return base


# ===== A1：v1 的 ``type`` =====


@pytest.mark.parametrize("src,want", [
    ({"type": 3}, MT_IMAGE),              # 【A1】v1 就靠这个字段
    ({"type": "3"}, MT_IMAGE),
    ({"type": 49}, MT_APP),
    ({"type": 3, "MsgType": 1}, MT_TEXT),  # 老字段优先：别让 v1 的 type 压过 MsgType
    ({"type": 3, "msgType": 1}, MT_TEXT),
    ({"type": "abc"}, MT_TEXT),            # 脏值 ⇒ 文本（不抛）
    ({"type": None}, MT_TEXT),
    ({"go_fields": {"MsgType": 3}}, MT_IMAGE),   # WS 形态的老路径不许被碰坏
])
def test_v1_type_is_recognized(src: dict, want: int):
    assert _extract_msg_type(src) == want


def test_v1_image_is_not_reported_as_text():
    """修前这一条恒为 ``MT_TEXT`` —— 图片被当文本，媒体链路整条不触发。"""
    assert parse_message(ACCT, _v1(type=3)).msg_type == MT_IMAGE


# ===== A2：群 id 的兜底 =====


def test_group_id_comes_from_conversation_id_without_the_is_group_flag():
    """【A2】v1 群里某人发言、``is_group`` 缺失 ⇒ 修前被当私聊。"""
    m = parse_message(ACCT, _v1(conversation_id="room_abc@chatroom", id="g1"))
    assert m.chatroom_id == "room_abc@chatroom"
    assert m.is_group is True
    assert m.peer_id == "room_abc@chatroom"
    assert m.peer_kind == "group"


def test_group_id_falls_back_to_recipient_id():
    """``conversation_id`` 为空时按 ``recipient_id`` 兜（v1 群里收件人就是群）。"""
    m = parse_message(ACCT, _v1(conversation_id="", recipient_id="room_x@chatroom", id="g2"))
    assert m.chatroom_id == "room_x@chatroom"


def test_is_group_flag_still_wins_when_the_room_id_is_oddly_shaped():
    """``is_group`` 显式为真时保持旧行为（群 id 不是 ``@chatroom`` 形态也认）。

    这条是"别把它修没了"的护栏：v1 真有非标准群 id 的部署，靠旧分支活着。
    """
    m = parse_message(ACCT, _v1(is_group=True, conversation_id="weird_group_key", id="g3"))
    assert m.chatroom_id == "weird_group_key"


def test_a_dm_is_still_a_dm():
    m = parse_message(ACCT, _v1(id="d1"))
    assert m.chatroom_id == ""
    assert m.is_group is False
    assert m.peer_kind == "dm"


@pytest.mark.parametrize("flag", ["false", "0", 0, False, None, ""])
def test_a_falsy_is_group_stays_a_dm(flag):
    """``is_group`` 的**假值形态**不许被当成真 —— JSON 里它常是字符串。

    ``bool("false")`` 是 ``True``：只要把判据写成裸真值测试，一个字符串 ``"false"``
    就足以把**私聊升级成群**（peer 从某人变成他自己、"群策略"套到私聊头上）。
    """
    m = parse_message(ACCT, _v1(is_group=flag, id="d2"))
    assert m is not None
    assert m.is_group is False, f"is_group={flag!r} 是假值"
    assert m.chatroom_id == ""
    assert m.peer_kind == "dm"


# ===== A3：没有 id 的消息不许共用一个去重键 =====


def test_a_message_without_an_id_gets_a_unique_synthesized_one():
    body = {"sender_id": "wxid_a", "content": "no id here"}
    a = parse_message(ACCT, dict(body))
    b = parse_message(ACCT, dict(body))
    assert a.msg_id and b.msg_id
    assert a.msg_id != b.msg_id, \
        "合成 id 必须每次不同 —— 相同就等于把所有无 id 消息并成一条"


@pytest.mark.parametrize("key", ["msgId", "MsgId", "id", "svr_id", "msg_id", "NewMsgId"])
def test_real_ids_are_left_untouched(key: str):
    assert parse_message(ACCT, {"sender_id": "wxid_a", key: "REAL-1"}).msg_id == "REAL-1"


def test_a_payload_with_neither_sender_nor_id_is_still_dropped():
    """合成 id **不许**把"垃圾 payload 过滤"这条兜没了（返回 ``None``）。"""
    assert parse_message(ACCT, {"content": "status ping"}) is None


def test_two_id_less_pushes_both_survive_the_pipeline():
    """【A3】端到端：无 id 的消息**都要**留下来（修前第二条判重复、静默吞掉）。"""
    pipe = InboundPipeline(ACCT, {})
    payload = {"Data": {"messages": [
        {"sender_id": "wxid_a", "conversation_id": "wxid_a", "content": "one"},
        {"sender_id": "wxid_a", "conversation_id": "wxid_a", "content": "two"},
    ]}}
    assert [m.content for m in pipe.extract(payload)] == ["one", "two"], \
        "同一批里两条无 id 的消息不该互相去重"
    # vendor 重推同一条 payload：宁可重放（可见、有心流预算兜底），不可静默吞
    assert len(pipe.extract(payload)) == 2


def test_duplicate_real_ids_are_still_deduped():
    """去重本身不许被 A3 的修法弄没（有 id 就该去）。"""
    pipe = InboundPipeline(ACCT, {})
    payload = {"Data": {"messages": [
        {"sender_id": "wxid_a", "conversation_id": "wxid_a", "content": "x", "id": "DUP"},
    ]}}
    assert len(pipe.extract(payload)) == 1
    assert pipe.extract(payload) == []


# ===== A5：business callback 的 ``{"string": …}`` 包装 =====
#
# 旧格式（``Data.Data.AddMsgs[]``）**每个字段**都被包成 ``{"string": "值"}``
# （对齐 TS ``parseBusinessCallbackMsg`` 的 ``unwrap``）。不解包不会报错 —— 只会把
# ``str(dict)``（``"{'string': 'wxid_x'}"``）当成 wxid/群 id/正文：发件人认不出 ⇒
# 整条消息静默消失；群 id 认不出 ⇒ 群消息当私聊；正文变成一串 dict 字面量喂给模型。


def test_business_callback_fields_are_unwrapped():
    src = {
        "FromUserName": {"string": "wxid_a"},
        "ToUserName": {"string": "room_1@chatroom"},
        "Content": {"string": "hello"},
        "MsgType": {"string": "3"},
        "CreateTime": {"string": "1700000000"},
        "MsgId": {"string": "bc-1"},
    }
    m = parse_message(ACCT, src)
    assert m is not None
    assert m.from_wxid == "wxid_a"
    assert m.content == "hello"
    assert m.msg_type == MT_IMAGE, "包装里的 MsgType 也要解"
    assert m.timestamp == 1700000000
    assert m.msg_id == "bc-1"
    assert m.chatroom_id == "room_1@chatroom", "群 id 在 ToUserName 上 ⇒ 必须是群"
    assert m.peer_kind == "group"


def test_a_group_message_whose_room_is_in_from_user_name():
    """旧格式群里 ``FromUserName`` 本身就是群 id（TS ``parseBusinessCallbackMsg``）。"""
    m = parse_message(ACCT, {"FromUserName": {"string": "room_2@chatroom"},
                             "Content": {"string": "x"}, "MsgId": "g9"})
    assert m is not None
    assert m.chatroom_id == "room_2@chatroom"


def test_a_non_string_dict_is_not_turned_into_a_wxid():
    """``{"string": …}`` **之外**的 dict 是我们不认识的形态 ⇒ 当空，不许把 dict
    字面量当值传下去（那会造出一个永远匹配不上的"wxid"）。"""
    m = parse_message(ACCT, {"FromUserName": {"userName": "wxid_a"},
                             "Content": "x", "MsgId": "d1"})
    assert m is not None and m.from_wxid == "", "不认识的 dict 不许变成 dict 字面量"


def test_the_whole_business_callback_envelope_reaches_the_pipeline():
    """★ A5 **端到端**：完整 envelope 穿过 ``InboundPipeline.extract``。

    上面几条测的是 ``parse_message`` 这个单点；这条测的是**能不能从厂商推来的那个
    payload 里把消息捞出来** —— ``extract_all_msg_src`` 的 AddMsgs 分支与逐字段
    unwrap 两段都得成立。少了任何一段，症状都一样：**整条消息静默消失**。

    envelope 逐字对齐 TS ``payloadToAllInboundMessages``／``parseBusinessCallbackMsg``
    （``src/inbound/parser.ts``）：``EventType=sync_message`` +
    ``Data.Data.AddMsgs[]``，且 **``Data.messages`` 缺席** —— TS 那边只要
    ``Array.isArray(messages)`` 为真就 ``return out``，会**直接跳过** AddMsgs 分支，
    所以这里不写它，测的才是真的那条路。
    """
    payload = {
        "Wxid": "wxid_bot",
        "EventType": "sync_message",
        "Timestamp": 1700000000,
        "Data": {
            "count": 1,
            "Data": {"AddMsgs": [{
                "FromUserName": {"string": "room_9@chatroom"},
                "ToUserName": {"string": "wxid_bot"},
                "Content": {"string": "早上好"},
                "MsgType": {"string": "3"},
                "CreateTime": {"string": "1700000000"},
                "MsgId": {"string": "bc-9"},
            }]},
        },
    }
    got = InboundPipeline(ACCT, {"selfWxid": "wxid_bot"}).extract(payload)

    assert len(got) == 1, "envelope 里那一条必须被捞出来（捞不出来 = 生产上那种静默丢弃）"
    m = got[0]
    assert m.from_wxid == "room_9@chatroom"
    assert m.content == "早上好"
    assert m.msg_type == MT_IMAGE, "包装里的 MsgType 要走类型判据，不许退化成文本"
    assert m.timestamp == 1700000000
    assert m.msg_id == "bc-9"
    assert m.chatroom_id == "room_9@chatroom", "群 id 在 FromUserName 上 ⇒ 必须是群"
    assert m.peer_kind == "group"
    assert "{" not in m.from_wxid and "{" not in m.content, \
        "不许把 ``{'string': …}`` 的 dict 字面量当值传下去"


def test_business_callback_without_any_message_is_not_invented():
    """AddMsgs 空 / 缺失 ⇒ **一条都不许造**（宁可不认，也不许凭空产生消息）。"""
    pipe = InboundPipeline(ACCT, {"selfWxid": "wxid_bot"})
    assert pipe.extract({"EventType": "sync_message", "Data": {"Data": {"AddMsgs": []}}}) == []
    assert pipe.extract({"EventType": "sync_message", "Data": {"Data": {}}}) == []


def test_business_callback_junk_items_are_filtered_before_the_parser(caplog):
    """``AddMsgs`` 里混进非 dict 的项 ⇒ **在提取层滤掉**，不许扔给 parse 层。

    扔过去的后果不是崩溃（``extract`` 的 per-item try 会把 ``AttributeError`` 吞掉、
    只留一行 warning，消息**照旧只出那一条**），所以这条只能靠**日志**验：
    真正的差别是"每个垃圾项留一行错"，那会把"厂商推了新形态"的信号淹掉。
    """
    payload = {"EventType": "sync_message", "Data": {"Data": {"AddMsgs": [
        "not-a-dict",
        {"FromUserName": {"string": "wxid_ok"}, "Content": {"string": "fine"}, "MsgId": "ok-1"},
    ]}}}
    with caplog.at_level("WARNING"):
        got = InboundPipeline(ACCT, {"selfWxid": "wxid_bot"}).extract(payload)

    assert [m.msg_id for m in got] == ["ok-1"], "好的那条要出，垃圾那条不许出"
    assert not [r for r in caplog.records if "解析消息失败" in r.getMessage()], \
        "垃圾项必须在提取层被滤掉 —— 扔给 parse 层等于每个垃圾项留一行错"


def test_the_envelope_event_type_is_required():
    """★ 不是 ``sync_message`` 的推送**不许**往 ``AddMsgs`` 里掏（与 TS 同口径）。

    TS ``payloadToAllInboundMessages`` 的第一道判据就是
    ``obj.EventType === "sync_message"``。少了它，任何带 ``Data.Data.AddMsgs``
    的推送（含厂商将来新增的事件类型）都会被当成入站消息 —— 那类"多进来的消息"
    会直接触发回复（红线 3：发出去撤不回）。
    """
    add_msgs = [{"FromUserName": {"string": "wxid_a"}, "Content": {"string": "hi"},
                 "MsgId": "x-1"}]
    pipe = InboundPipeline(ACCT, {"selfWxid": "wxid_bot"})
    assert pipe.extract({"Data": {"Data": {"AddMsgs": add_msgs}}}) == [], "没有 EventType"
    assert pipe.extract({"EventType": "group_change",
                         "Data": {"Data": {"AddMsgs": add_msgs}}}) == [], "别的事件类型"


# ===== A9：outgoing 图片的分歧（**有意不实现**，钉在这里免得被"顺手补上"）=====


def test_an_outgoing_image_is_not_admitted_here():
    """★ A9：TS 放行 outgoing 图片，本树**不放行** —— 这是**记录在案的分歧**，不是漏做。

    TS 那边（``src/inbound/parser.ts`` 的 ``isOutgoingImage``，v1.3.21 REVOKE-FIX）
    放行 ``direction != incoming`` 的图片，为的是让插件自己发的图片被 vendor 推回来时，
    把**真实 server id** 记进 ``wpp_messages``（图片撤回要用它）。

    本树不做，因为**没有消费者**：本树不落入站消息 —— ``repo_messages.save_message``
    只在 ``platform/adapter.py`` 的**出站**那条路上被调用。就算放行，也没有任何地方
    会把那个 id 记下来；而且它进来之后会在 ``_handle_one`` ⑤ 被 self 门丢掉。

    真要做图片撤回，先做"入站落库"（对齐 TS ``enrich.ts``），再谈放行。
    这条用例的作用是：**改行为前必须先看到这段理由**。
    """
    m = parse_message(ACCT, _v1(kind="image", type=3, direction="outgoing", id="og-1"))
    assert m is not None, "本树不按 direction 过滤（分歧点就在这里）"
    assert m.direction == "outgoing", "direction 照实透传（TS 侧靠它决定落库方向）"

    # 而它**走不到下游**：自己发的消息一律被 self 门丢掉（adapter ⑤ 与 triggers ⑤ 同口径）。
    from plugin.inbound.triggers import should_process
    ok, reason = should_process(m, {"selfWxid": "wxid_sender", "nickname": "测试小助理"})
    assert (ok, reason) == (False, "self"), "outgoing 图片到不了心流 —— 分歧无实际影响"


# ===== 群前缀 / 昵称提及（2026-10-18 审阅：两处过宽匹配） =====


def test_group_prefix_requires_two_segments():
    """TS 的判据是 ``^[^:\\n]+:\\n[^:\\n]+:\\n(...)`` —— **两段**才剥。

    旧实现只认一段 ``wxid_xxx:\\s*\\n?`` ⇒ 正文里一句 ``hello: 在吗`` 被当成前缀，
    ``hello`` 直接消失（"长度 ≥5 的英文单词 + 冒号"就会中）。
    """
    from plugin.inbound.parser import strip_group_prefix

    assert strip_group_prefix("123@chatroom:\nwxid_sender:\n在吗") == "在吗"
    assert strip_group_prefix("hello: 在吗") == "hello: 在吗"
    assert strip_group_prefix("hello:\n在吗") == "hello:\n在吗", "一段不是前缀"
    assert strip_group_prefix("a:\nb:\n") == "a:\nb:\n", "空正文两边都不剥（TS 同）"
    assert strip_group_prefix("普通消息") == "普通消息"


def test_nickname_mention_needs_a_boundary():
    """``@小助理2`` 不许叫醒名叫 ``小助理`` 的 bot（前缀撞名会互相抢答）。"""
    from plugin.inbound.parser import is_bot_mentioned_by_text

    assert is_bot_mentioned_by_text("@小助理 在吗", "", "小助理") is True
    assert is_bot_mentioned_by_text("@小助理2 在吗", "", "小助理") is False
    assert is_bot_mentioned_by_text("@小助理abc 在吗", "", "小助理") is False
    # 中文紧跟无法消歧（微信 @ 后面通常直接跟正文），仍按命中
    assert is_bot_mentioned_by_text("@小助理在吗", "", "小助理") is True


# ===== 49 引用回复不许被标成 [文件]（2026-10-18 真机） =====


def test_a_quote_reply_is_not_tagged_as_a_file():
    """★ 真机 真机：一条纯文本的引用回复被加上 ``[文件]`` 前缀。

    现场形态（``wpp_messages`` 2026-10-18）：厂商把这条件推成 ``msg_type=49``，
    ``app.category == "quote"``，``content`` 是回复正文。正文里没有「转账/接龙/
    应用消息」这些词 ⇒ 掉进 ``_media_tag`` 最后的 ``[文件]`` 兜底，模型据此回
    「你发的是卡片/文件类消息，我这边只读到文字」。

    判据：quote 是 49 的一个**独立细分**，必须排在兜底之前；被引用内容走
    ``reply_to``（Hermes 的 ``reply_to_text`` / ``reply_to_message_id``），
    正文不加任何媒体标记。
    """
    src = _v1(type=49, content="我刚才那条是纯文本，没有附件", app={
        "category": "quote",
        "reference": {
            "new_msg_id": "4174361559566713352",
            "svr_id": "4174361559566713352",
            "from_user_id": "wxid_owner_demo",
            "display_name": "示例联系人",
        },
    })
    msg = parse_message(ACCT, src)
    assert msg is not None and msg.msg_type == MT_APP
    assert msg.reply_to.get("svrid") == "4174361559566713352"
    assert InboundPipeline(ACCT, {}).prepare_text(msg) == "我刚才那条是纯文本，没有附件"


def test_a_legacy_refermsg_quote_is_not_tagged_as_a_file():
    """旧版 vendor 把引用信息塞在 ``content`` 的 ``<refermsg>`` XML 里，同样不是文件。"""
    xml = ("<msg><appmsg><title>回复正文</title><type>57</type>"
           "<refermsg><svrid>4174</svrid><fromusr>wxid_boss</fromusr>"
           "<displayname>示例联系人</displayname><content>旧消息</content></refermsg>"
           "</appmsg></msg>")
    msg = parse_message(ACCT, _v1(type=49, content=xml))
    assert msg is not None and msg.reply_to.get("svrid") == "4174"
    text = InboundPipeline(ACCT, {}).prepare_text(msg)
    assert not text.startswith("[文件]")


def test_addmsgs_wrapped_quote_is_extracted():
    """business callback 的 ``AddMsgs`` 把字段包成 ``{"string": …}``，复杂对象还是
    JSON 字符串 —— 引用解析必须解包，否则这一路 ``reply_to`` 恒空（且不报错）。"""
    app = {"category": "quote",
           "reference": {"svr_id": "9001", "from_user_id": "wxid_q",
                         "display_name": "示例联系人"}}
    src = {
        "MsgId": {"string": "msg-1"},
        "FromUserName": {"string": "wxid_boss"},
        "ToUserName": {"string": "room@chatroom"},
        "MsgType": {"string": "49"},
        "Content": {"string": "回复正文"},
        "app": {"string": json.dumps(app)},
    }
    msg = parse_message(ACCT, src)
    assert msg is not None
    assert msg.reply_to == {"svrid": "9001", "from_wxid": "wxid_q",
                            "quote_content": "", "display_name": "示例联系人"}


def test_addmsgs_wrapped_refermsg_xml_is_extracted():
    xml = ("<msg><appmsg><title>回复正文</title><type>57</type>"
           "<refermsg><svrid>4174</svrid><fromusr>wxid_boss</fromusr>"
           "<displayname>示例联系人</displayname></refermsg></appmsg></msg>")
    src = {
        "MsgId": {"string": "msg-2"},
        "FromUserName": {"string": "wxid_boss"},
        "ToUserName": {"string": "room@chatroom"},
        "MsgType": {"string": "49"},
        "Content": {"string": xml},
    }
    msg = parse_message(ACCT, src)
    assert msg is not None and msg.reply_to.get("svrid") == "4174"


def test_quoting_a_file_is_not_itself_a_file_message():
    """引用一条**文件**消息 ≠ 这条是文件（refermsg 里嵌的是原文件的 appmsg）。"""
    quoted_file = ("<msg><appmsg><title>报表.pdf</title><type>6</type>"
                   "<filename>报表.pdf</filename></appmsg></msg>")
    src = _v1(type=49, content=(
        "<msg><appmsg><title>这条是纯文本回复</title><type>57</type>"
        f"<refermsg><svrid>555</svrid><fromusr>wxid_q</fromusr>"
        f"<content>{quoted_file}</content></refermsg></appmsg></msg>"))
    msg = parse_message(ACCT, src)
    assert msg is not None and msg.reply_to.get("svrid") == "555"
    assert is_file_message(src) is False, "引用文件的消息不许走路文件兜底话术"


def test_a_real_file_appmsg_is_still_detected():
    outer = ("<msg><appmsg><title>报表.pdf</title><type>6</type>"
             "<filename>报表.pdf</filename></appmsg></msg>")
    assert is_file_message({"content": outer}) is True
    assert is_file_message({"app": {"category": "file", "title": "x.pdf"}}) is True


@pytest.mark.parametrize("category", ["file", "app_message", None])
def test_a_non_quote_49_still_falls_back_to_the_file_tag(category):
    """这条修的是**引用**的误标，不是把 49 的兜底整个撤掉。"""
    app = {} if category is None else {"category": category}
    msg = parse_message(ACCT, _v1(type=49, content="报表.xlsx", app=app))
    assert msg is not None
    assert InboundPipeline(ACCT, {}).prepare_text(msg) == "[文件] 报表.xlsx"


# ===== 群 @ 推送文案里的发送者昵称（2026-10-18 真机） =====


def test_group_at_push_content_supplies_the_sender_nickname():
    """★ v1 群消息只给 ``sender_id``、不给昵称，但 ``push_content`` 里带着
    「戴永刚在群聊中@了你」。

    修前 ``from_nickname`` 恒空 ⇒ Hermes 只看到 wxid，群里把戴永刚叫成了"接总"
    （老板 2026-10-18 指出）。昵称**只从 push 文案取**，绝不拿正文猜。
    """
    m = parse_message(ACCT, _v1(
        conversation_id="21354014635@chatroom", id="pn-1",
        push_content="戴永刚在群聊中@了你",
    ))
    assert m is not None
    assert m.from_wxid == "wxid_sender"
    assert m.from_nickname == "戴永刚"


@pytest.mark.parametrize("push", [
    "张三在群里@了你",
    "李四在群聊里@了你",
    "  王五 在群聊中 @ 了你",
])
def test_other_push_content_variants_are_recognized(push):
    assert parse_message(ACCT, _v1(push_content=push)).from_nickname != ""


def test_an_explicit_nickname_still_wins_over_the_push_text():
    """vendor 将来把昵称直接放进消息体时，以消息体为准（push 只是兜底）。"""
    m = parse_message(ACCT, _v1(fromNickname="真名", push_content="替身在群聊中@了你"))
    assert m.from_nickname == "真名"


def test_a_card_or_plain_push_is_not_mistaken_for_a_nickname():
    """名片 ``[名片]xxx`` / 普通推送文案不带「在群聊中@了你」，不许被当成昵称。"""
    assert parse_message(ACCT, _v1(push_content="[名片]某某")).from_nickname == ""
    assert parse_message(ACCT, _v1(push_content="你收到一条新消息")).from_nickname == ""


def test_the_nested_from_user_shape_also_gets_the_push_fallback():
    src = _v1(push_content="赵六在群聊中@了你")
    src["from_user"] = {"wxid": "wxid_zhao"}
    m = parse_message(ACCT, src)
    assert (m.from_wxid, m.from_nickname) == ("wxid_zhao", "赵六")


# ===== 普通消息推送文案「昵称 : 正文」（2026-10-18 私聊标题依赖它） =====


@pytest.mark.parametrize("push,want", [
    ("示例联系人 : 你好", "示例联系人"),          # 私聊
    ("戴永刚 : 收到", "戴永刚"),          # 群里没 @ 的消息
    ("示例联系人：你好", ""),                 # 冒号后无空白 ⇒ 不猜（可能是正文）
])
def test_plain_push_content_prefix_is_used_as_the_sender_nickname(push, want):
    """v1 私聊常常也只带 wxid；``push_content`` 里的「昵称 : 正文」是微信客户端
    生成的发送者前缀，可以安全当昵称（正文在 ``content``，不参与猜测）。"""
    assert parse_message(ACCT, _v1(push_content=push)).from_nickname == want


def test_the_colon_rule_never_eats_the_body_as_a_nickname():
    """正文自己带冒号（``注意: 明天``）不能被当成昵称 —— 只认 push，不看 content。"""
    m = parse_message(ACCT, _v1(content="注意: 明天", push_content=""))
    assert m.from_nickname == ""
