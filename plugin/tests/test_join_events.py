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
    """最小消息桩：只带本模块读的字段。"""

    def __init__(self, content, msg_type="10000", chatroom_id="1@chatroom", raw=None):
        self.content = content
        self.msg_type = msg_type
        self.chatroom_id = chatroom_id
        self.peer_id = chatroom_id
        self.raw = raw or {}


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


def test_10000_placeholder_means_members_changed():
    """**实测常态**：vendor 对 10000 只推占位符、raw 里也没有原文。

    这种时候不能报"不认识"就完事 —— 它是"该群成员可能变了"的唯一信号，
    必须返回 ``members_changed`` 让调用方去拉一次名单。
    """
    ev = join_events.parse(_Msg("收到一条微信系统通知", msg_type="10000"))
    assert ev is not None
    assert ev.kind == "members_changed"


def test_unknown_type_with_unknown_text_is_none():
    assert join_events.parse(_Msg("随便一句话", msg_type="12345")) is None


def test_parse_never_raises_on_none_content():
    ev = join_events.parse(_Msg(None))
    assert ev is None


# ---------------------------------------------------------------- raw 兜底

def test_normalized_content_falls_back_to_raw():
    """**2026-10-20 实测**：vendor 对 msgType=10000 把 content 归一化成
    「收到一条微信系统通知」，原文只在 raw 里。只读 content 会永远判成 unknown。"""
    msg = _Msg("收到一条微信系统通知", msg_type="10000",
               raw={"Data": {"Content": '"张三"邀请"李四"加入了群聊'}})
    ev = join_events.parse(msg)
    assert ev is not None
    assert ev.kind == "join"
    assert ev.member_name == "李四"


def test_raw_leave_is_detected():
    msg = _Msg("收到一条微信系统通知", msg_type="10000",
               raw={"Data": {"Msg": '"李四"退出了群聊'}})
    ev = join_events.parse(msg)
    assert ev is not None
    assert ev.kind == "leave"


def test_raw_text_nested_in_lists():
    """字段形状可能深到 list-of-dict —— 深度上限内都要挖到。"""
    msg = _Msg("收到一条微信系统通知", msg_type="10000",
               raw={"Data": {"List": [{"Content": '"李四"加入了群聊'}]}})
    ev = join_events.parse(msg)
    assert ev is not None and ev.kind == "join"


def test_raw_scan_is_depth_limited():
    """别为了找文本把整个 payload 走穿（大 payload 会拖慢热路径）。"""
    deep = {"a": {"b": {"c": {"d": {"e": {"f": '"李四"加入了群聊'}}}}}}
    ev = join_events.parse(_Msg("收到一条微信系统通知", msg_type="10000", raw=deep))
    assert ev is not None and ev.kind == "members_changed"


def test_members_changed_carries_candidates_for_diagnosis():
    """候选文本必须进日志 —— 厂商哪天给了原文，就靠它发现并补正则。"""
    msg = _Msg("收到一条微信系统通知", msg_type="10000",
               raw={"Data": {"Content": "某某 操作了 某某"}})
    ev = join_events.parse(msg)
    assert ev is not None and ev.kind == "members_changed"
    assert "某某 操作了 某某" in ev.raw


def test_string_system_type_means_members_changed():
    """**2026-10-20 16:31 实测**：vendor 把类型发成字符串 ``system``（不是 10000）。

    只判 ``== "10000"`` 会让它永远走 unknown_system —— 这次就是踩这个。
    """
    ev = join_events.parse(_Msg("收到一条微信系统通知", msg_type="system"))
    assert ev is not None
    assert ev.kind == "members_changed"


def test_revoke_type_also_means_members_changed():
    """**实测教训**：同一件事（拉人进群）vendor 两次给了不同类型
    （``system`` 和 ``10002``）。按类型挑必漏，所以已知系统类型一律当成员变动。"""
    ev = join_events.parse(_Msg("收到一条微信系统通知", msg_type="10002"))
    assert ev is not None and ev.kind == "members_changed"


def test_truly_unknown_type_stays_unknown():
    """不在已知集合里的类型才报 unknown（那是唯一需要人看的情况）。"""
    ev = join_events.parse(_Msg("收到一条微信系统通知", msg_type="system_x"))
    assert ev is None or ev.kind == "unknown_system"
