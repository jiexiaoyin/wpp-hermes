"""群成员变动事件（``inbound/join_events.py``）的契约。

这模块补的是**迁移漏掉的一段**：OpenClaw 时期进群通知能入库（DB 里还有一条
2026-05-30 的 ``friend_confirm``），py2 里 ``friend_confirm`` 零命中 —— 于是
"有人进群"在 Hermes 版**永远识别不到**，成员表停在 8-11，新人昵称查不到，
心流回复只能看到 ``wxid_xxx``。

钉四件事：

1. **微信三种进群文案都要认**（邀请 / 扫码 / 直接加入）；
2. **退群两种都要认**（主动退出 / 被移出）；
3. **文本判据优先于 msgType** —— 厂商改类型时不能静默失效（这是本模块存在的主因）；
4. **普通消息必须返回 ``None``** —— 误判会让每条聊天都去拉一次成员列表。
"""

from __future__ import annotations

import pytest

from plugin.inbound import join_events


class _Msg:
    """最小消息桩：只带本模块读的三个字段。"""

    def __init__(self, content: str, msg_type="10000", chatroom_id="1@chatroom"):
        self.content = content
        self.msg_type = msg_type
        self.chatroom_id = chatroom_id
        self.peer_id = chatroom_id


# ---------------------------------------------------------------- 进群

@pytest.mark.parametrize("text,member,inviter", [
    ('"张三"邀请"李四"加入了群聊', "李四", "张三"),
    ('张三邀请李四加入了群聊', "李四", "张三"),
    ('"李四"通过扫描"张三"分享的二维码加入群聊', "李四", "张三"),
    ('"李四"加入了群聊', "李四", ""),
    ('李四加入了群聊', "李四", ""),
])
def test_join_texts(text, member, inviter):
    ev = join_events.parse(_Msg(text))
    assert ev is not None
    assert ev.kind == "join"
    assert ev.member_name == member
    assert ev.inviter == inviter


def test_join_works_when_msg_type_is_unexpected():
    """**核心契约**：厂商换了 msgType，文本判据仍要命中（否则又静默失效）。"""
    ev = join_events.parse(_Msg('"张三"邀请"李四"加入了群聊', msg_type="1"))
    assert ev is not None and ev.kind == "join"


def test_join_keeps_chatroom_id():
    ev = join_events.parse(_Msg('"李四"加入了群聊', chatroom_id="21354014635@chatroom"))
    assert ev is not None
    assert ev.chatroom_id == "21354014635@chatroom"


# ---------------------------------------------------------------- 退群

@pytest.mark.parametrize("text,member", [
    ('"李四"退出了群聊', "李四"),
    ('李四退出了群聊', "李四"),
    ('"张三"将"李四"移出了群聊', "李四"),
])
def test_leave_texts(text, member):
    ev = join_events.parse(_Msg(text))
    assert ev is not None
    assert ev.kind == "leave"
    assert ev.member_name == member


# ---------------------------------------------------------------- 机器人自己进群

def test_group_invite_means_self_join():
    """``group_invite`` = 机器人自己被拉进群 ⇒ 调用方要拉**全部**群。"""
    ev = join_events.parse(_Msg(
        "[群聊邀请] 周州 邀请你加入群聊「东方商城苏宁合作厅联通对接群」",
        msg_type="group_invite"))
    assert ev is not None
    assert ev.kind == "self_join"


def test_self_join_wins_over_join_pattern():
    """「邀请你加入群聊」不能被当成"别人进群" —— 两种处置（单群 vs 全部群）不能搞反。"""
    ev = join_events.parse(_Msg("张三 邀请你加入群聊「某某群」"))
    assert ev is not None
    assert ev.kind == "self_join"


# ---------------------------------------------------------------- 不误判

@pytest.mark.parametrize("text", [
    "办理业务：福袋 办理号码：13852495588",
    "又下一单福袋，不错[强]",
    "收到一张图片",
    "",
])
def test_ordinary_messages_are_not_events(text):
    """普通聊天必须返回 ``None`` —— 误判 = 每条消息都拉一次成员列表。"""
    assert join_events.parse(_Msg(text, msg_type="1")) is None


def test_known_system_type_with_unknown_text_is_reported():
    """已知系统类型但文案不认识 ⇒ 返回 ``unknown_system`` 供日志留痕，不能静默吞。"""
    ev = join_events.parse(_Msg("某某操作了某某", msg_type="10000"))
    assert ev is not None
    assert ev.kind == "unknown_system"


def test_unknown_type_with_unknown_text_is_none():
    assert join_events.parse(_Msg("随便一句话", msg_type="12345")) is None


def test_parse_never_raises_on_none_content():
    ev = join_events.parse(_Msg(None))
    assert ev is None
