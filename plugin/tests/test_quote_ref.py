"""引用回复的"被回复人"身份解析（``plugin/inbound/quote_ref.py``）。

## 这个文件钉的是什么失效

老板 2026-10-03 报的 bug：「Hermes 插件，群里 @ 机器人时，触发回复了，但是回复没有和
Openclaw 插件一样，可以 @ 被回复的人」。代码面根因是一整条**死的输入**：
``fromusr`` 恒空 ⇒ ``_quote_prefix`` 第一句就返回 ``""`` ⇒ 前缀从来没出现过。
本文件钉的就是把它接活的那三件事：

1. **谁是发送者**（``resolve_quote_identity``）—— 换一行、少一档都会让 ``@`` 落在
   错的人身上，或者落在**群自己**身上（business callback 的 ``FromUserName`` 就是群 id，
   这不是理论风险）；
2. **群 id 不是人**（``is_usable_sender``）—— 少这道闸的失效形态是"@ 了一个群"；
3. **前缀形状**（``build_mention_prefix``）—— 与 TS ``buildGroupMentionPrefix`` 的
   ``atName`` 规则逐字对齐；"名称与 wxid 相同要当没有名称" 这一条错了就会真 @ 出
   ``@wxid_xxx``。

⚠️ 全部判据都是**行为**判据（不是在树上找字符串）：这一条链的失效形态全都"看着正常、
不报错"，唯一能分辨的就是"给一份真形状的输入，看它返回谁"。
"""

from __future__ import annotations

import json

import pytest

from plugin.inbound import quote_ref as Q


# ------------------------------------------------------------------ 夹具

def _row(**over) -> dict:
    """一条 ``wpp_messages`` 行（形状照 ``repo_messages.get_message_by_id_or_new_id``）。"""
    base = {
        "msg_id": "m-1",
        "new_msg_id": "9001",
        "direction": "inbound",
        "peer_kind": "group",
        "peer_id": "111@chatroom",
        "chat_id": "111@chatroom",
        "peer_name": None,
        "from_wxid": None,
        "msg_type": "1",
        "content": "@益融小助理 有现货吗",
        "raw_payload": None,
        "at_sec": 1700000000,
    }
    base.update(over)
    return base


#: 真机（``.183``）实测的 v1 / WS 帧形状：发送者在**顶层** ``sender_id``，
#: ``go_fields`` 里的 ``FromUserName`` 对群消息就是**群 id**，且整帧没有昵称字段。
LIVE_FRAME = {
    "sender_id": "wxid_sender",
    "go_fields": {
        "Content": "@益融小助理 有现货吗",
        "FromUserName": "111@chatroom",
        "MsgType": 1,
        "NewMsgId": "9001",
        "PushContent": "某某: @益融小助理 有现货吗",
    },
}


# ============================================================ 1. 群 id 不是人

def test_a_group_id_is_never_a_sender():
    """``xxx@chatroom`` 一律不认 —— 认了就会 @ 一个群（真会发生的形态）。"""
    assert Q.is_usable_sender("111@chatroom") == ""
    assert Q.is_usable_sender("wxid_alice") == "wxid_alice"


@pytest.mark.parametrize("bad", [None, "", "   ", "  \n "])
def test_blank_is_not_a_sender(bad):
    assert Q.is_usable_sender(bad) == ""


def test_a_wrapped_string_is_unwrapped_before_use():
    """business callback 把每个字段包成 ``{"string": …}``（``parser._unwrap`` 那一族）。

    不解包就会把 ``"{'string': 'wxid_alice'}"`` 当成 wxid —— 一个**不是 wxid 的 wxid**。
    """
    assert Q.is_usable_sender({"string": "wxid_alice"}) == "wxid_alice"


# ======================================================== 2. 正文前缀（老形态）

def test_the_body_prefix_shape_is_extracted():
    assert Q.extract_group_sender_wxid("wxid_alice:\n@bot 你好") == "wxid_alice"
    assert Q.extract_group_sender_wxid("gh_abc:\n你好") == "gh_abc"
    # 群 id 也能抠出来 —— **认不认它**是 is_usable_sender 的事，两步分开（对齐 TS）
    assert Q.extract_group_sender_wxid("111@chatroom:\n你好") == "111@chatroom"


@pytest.mark.parametrize("content", [
    None,
    "",
    12345,
    "@bot 你好",                      # 没前缀
    "wxid_alice: @bot 你好",          # TS 的正则要求冒号后是**换行**
    "张三:\n@bot 你好",               # 昵称前缀（TS 也只认 wxid/gh_/@chatroom 三种）
])
def test_the_body_prefix_rejects_everything_else(content):
    assert Q.extract_group_sender_wxid(content) == ""


# ============================================================ 3. 身份链

def test_from_wxid_wins_when_present():
    ident = Q.resolve_quote_identity(_row(
        from_wxid="wxid_row", raw_payload=LIVE_FRAME, peer_name="张三"))
    assert ident.fromusr == "wxid_row"
    assert ident.display_name == "张三"
    assert ident.matched is True


def test_a_group_id_in_the_row_falls_through_to_the_payload():
    """行里的 ``from_wxid`` 是群 id ⇒ 它**不是**发送者，继续往下找。

    这正是 business callback 的形态（``FromUserName`` = 群 id 被落进 ``from_wxid``）：
    少了这道闸，@ 的对象会变成群本身。
    """
    ident = Q.resolve_quote_identity(_row(
        from_wxid="111@chatroom", raw_payload=LIVE_FRAME))
    assert ident.fromusr == "wxid_sender"


def test_the_live_frame_shape_prefers_the_top_level_sender():
    """真机帧：``sender_id``（人）与 ``go_fields.FromUserName``（群）并存 ⇒ 取人。"""
    ident = Q.resolve_quote_identity(_row(raw_payload=LIVE_FRAME))
    assert ident.fromusr == "wxid_sender"


def test_the_payload_may_be_a_json_string():
    """MariaDB 的 ``JSON`` 列 = LONGTEXT，pymysql 原样回**字符串** —— 真实形态。"""
    ident = Q.resolve_quote_identity(_row(raw_payload=json.dumps(LIVE_FRAME)))
    assert ident.fromusr == "wxid_sender"


def test_a_broken_json_payload_is_not_fatal():
    ident = Q.resolve_quote_identity(_row(raw_payload="{不是 json", content="普通正文"))
    assert ident.fromusr == ""


def test_the_flat_sender_keys_are_accepted():
    for key in ("sender_id", "fromUser", "FromWxid", "fromWxid", "FromUserName"):
        ident = Q.resolve_quote_identity(_row(raw_payload={key: "wxid_x"}))
        assert ident.fromusr == "wxid_x", key


def test_a_nested_from_user_object_is_accepted():
    ident = Q.resolve_quote_identity(_row(raw_payload={"from_user": {"wxid": "wxid_n"}}))
    assert ident.fromusr == "wxid_n"


def test_the_go_fields_block_is_accepted():
    ident = Q.resolve_quote_identity(_row(raw_payload={"go_fields": {"sender_id": "wxid_g"}}))
    assert ident.fromusr == "wxid_g"


def test_the_content_prefix_is_the_last_resort():
    ident = Q.resolve_quote_identity(_row(content="wxid_old:\n@bot 在吗"))
    assert ident.fromusr == "wxid_old"


def test_a_group_content_prefix_is_rejected_too():
    """TS 在**这条路**上不设闸（它的闸只管参数档与查昵称档）⇒ 群 id 会流进 ``fromusr``
    再由前缀兜底成 ``@111@chatroom `` 真发出去。py2 每一档出口都过闸，理由与代价见
    ``quote_ref`` 模块 docstring 第 3 条。
    """
    ident = Q.resolve_quote_identity(_row(content="111@chatroom:\n@bot 在吗"))
    assert ident.fromusr == ""


def test_a_rejected_prefix_falls_through_to_the_next_level_instead_of_stopping():
    """被闸掉 ≠ 到此为止：DM 记录里再往下还有 ``peer_id`` 那一档（对齐 TS 的 ``??`` 链）。"""
    ident = Q.resolve_quote_identity(_row(peer_kind="direct", peer_id="wxid_friend",
                                          content="111@chatroom:\n你好"))
    assert ident.fromusr == "wxid_friend"


def test_a_direct_row_may_use_its_peer_id():
    """DM：被引用的对方就是 ``peer_id``（TS 的 ``isGroupRec ? undefined : peer_id``）。"""
    ident = Q.resolve_quote_identity(_row(peer_kind="direct", peer_id="wxid_friend",
                                          content="你好"))
    assert ident.fromusr == "wxid_friend"


def test_a_group_row_never_uses_its_peer_id():
    ident = Q.resolve_quote_identity(_row(peer_kind="group", peer_id="111@chatroom",
                                          content="你好"))
    assert ident.fromusr == ""


def test_a_mislabelled_group_row_still_never_uses_its_peer_id():
    """两个信号打架时以 **``peer_kind``** 为准（TS：``isGroupRec ? undefined : peer_id``）。

    形态：``peer_kind='group'`` 而 ``peer_id`` 是个**普通 wxid**（脏行，或口径改过）。
    ``is_usable_sender`` 那道**值形状**的闸在这里挡不住它（它长得完全像个真发送者）
    ⇒ 只有 ``peer_kind`` 这道闸能挡。

    ⚠️ 这条用例是**补出来的**：没有它时，"群记录不用 peer_id" 这条规则整个可以被删掉
    而现有用例全绿 —— 因为群记录的 ``peer_id`` 恰好都是 ``@chatroom``，被上一道闸
    先拦住了（变异体实测 SURVIVED）。规则本身没错，是**判据被另一道闸遮住了**。
    """
    ident = Q.resolve_quote_identity(_row(peer_kind="group", peer_id="wxid_looks_real",
                                          content="你好"))
    assert ident.fromusr == ""


def test_a_missing_row_is_an_unmatched_identity():
    ident = Q.resolve_quote_identity(None)
    assert (ident.fromusr, ident.display_name, ident.matched) == ("", "", False)
    assert Q.resolve_quote_identity("不是行").matched is False


def test_the_row_peer_name_is_passed_through_as_the_name_hint():
    """行内 ``peer_name``（= 消息到达时的昵称）由调用方决定"要不要用"。

    本函数**不**替调用方把它和 ``fromusr`` 比 —— 那一步在 ``_quote_prefix`` 的
    五级链里（"名称 == wxid 就当没有名称"）。
    """
    ident = Q.resolve_quote_identity(_row(from_wxid="wxid_a", peer_name="wxid_a"))
    assert ident.display_name == "wxid_a"


def test_a_null_peer_name_is_an_empty_hint_not_the_string_none():
    """真机上 ``peer_name`` 实测**恒为 NULL**（帧里没有昵称字段）⇒ 这里必须是 ``""``。"""
    assert Q.resolve_quote_identity(_row(from_wxid="wxid_a", peer_name=None)).display_name == ""


def test_identity_never_raises_on_garbage():
    """库里什么都可能：坏 JSON、不是 dict 的列值、数字……**一个都不许抛**。"""
    for row in (_row(raw_payload=[1, 2, 3], content=42),
                _row(raw_payload=None, content=None, peer_id=None, from_wxid=None),
                _row(raw_payload=b"\xff\xfe", content={"a": 1})):
        Q.resolve_quote_identity(row)          # 不抛就算过


# ============================================================ 4. 前缀形状

def test_the_prefix_is_only_for_groups():
    assert Q.build_mention_prefix("111@chatroom", "张三", "wxid_a") == "@张三 "
    assert Q.build_mention_prefix("wxid_friend", "张三", "wxid_a") == ""


def test_a_missing_name_falls_back_to_the_wxid():
    assert Q.build_mention_prefix("111@chatroom", "", "wxid_a") == "@wxid_a "
    assert Q.build_mention_prefix("111@chatroom", None, "wxid_a") == "@wxid_a "


def test_a_name_equal_to_the_wxid_counts_as_no_name():
    """TS：``displayName && displayName !== fromusr ? displayName : fromusr``。

    这一条错了（照抄那个等于 wxid 的名字）与对了**长得一模一样**（都是 ``@wxid_a ``），
    所以判据要下在"名字确实不同"的那一侧 —— 见下一条。
    """
    assert Q.build_mention_prefix("111@chatroom", "wxid_a", "wxid_a") == "@wxid_a "


def test_a_real_name_beats_the_wxid():
    assert Q.build_mention_prefix("111@chatroom", "张三", "wxid_a") == "@张三 "


def test_without_both_there_is_no_prefix():
    """两样都没有 ⇒ 不加前缀（**不许**返回 ``"@ "`` —— 那是条发出去会被看的空 @）。"""
    assert Q.build_mention_prefix("111@chatroom", "", "") == ""
    assert Q.build_mention_prefix("111@chatroom", None, None) == ""


def test_the_prefix_carries_exactly_one_trailing_space():
    """``_send_chunk`` 做的是 ``f"{prefix}{chunk}".strip()`` ⇒ 前缀自带一个空格。"""
    assert Q.build_mention_prefix("111@chatroom", " 张三 ", "wxid_a") == "@张三 "
