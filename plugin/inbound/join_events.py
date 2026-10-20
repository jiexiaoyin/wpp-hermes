"""群成员变动事件（进群 / 退群）的识别与解析。

微信把「谁进群了」推成**系统通知**，不是人说的话。这类消息：

* ``msgType=10000``（``parser.MT_SYSTEM``），或字符串类型
  ``friend_confirm`` / ``group_invite``（旧版/Ts 时期见过）；
* **不满足任何触发路**（不是 @、不是引用、不含关键词）⇒ 在
  ``adapter._handle_one`` 的 ④ 被 ``should_drop`` fail-closed 丢弃；
* 于是 ``wpp_chatroom_members`` 永远不更新 —— 2026-10-20 实测移动群
  有人进群，DB 里零记录，成员表停在 8-11，新人在心流 prompt 里只剩
  ``wxid_xxx``，回复也就不称呼人。

## 为什么解析完还要拉一次成员列表

系统通知是**纯文本**，只带昵称、**不带 wxid**：:

    "张三"邀请"李四"加入了群聊
    "李四"通过扫描"张三"分享的二维码加入群聊
    "李四"加入了群聊

昵称可能重名、可能是群昵称，**不能**当 wxid 用。所以本模块只回答
「**这个群有人变了**」，调用方据此对该群调一次
``name_sync.sync_members(client, account_id, [chatroom_id])`` —— 这就是
老板要的「有新人加入的时候看一次增量」，不是定时轮询。

## 判据顺序

1. 类型不在已知集合里、且文本也不像系统通知 ⇒ 返回 ``None``（绝大多数消息）；
2. 文本命中进群/退群模式 ⇒ 返回事件（**不管类型**，防厂商改 msgType 后静默失效）；
3. 类型是 ``group_invite`` ⇒ 返回 ``kind="self_join"``：机器人**自己**被拉进群，
   调用方应拉全部群（新群还没 id，``/Group/List`` 会带上它）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 已知的系统通知类型（数字 + 旧版字符串）。
SYSTEM_TYPES: frozenset[str] = frozenset({
    "10000", "10002", "friend_confirm", "group_invite", "system",
})

_QUOTED = r'"?(?P<{name}>[^"「」]{1,64}?)"?'


def _q(name: str) -> str:
    """把 ``_QUOTED`` 里的占位符换成命名组名（``str.format`` 会吃掉 ``{1,64}``）。"""
    return _QUOTED.replace("{name}", name)


#: 进群三种文案（顺序即优先级：带邀请人的先匹配，避免被最宽松那条吃掉）。
_JOIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(_q("inviter") + r"\s*邀请\s*" + _q("member") + r"\s*加入了?群聊"),
    re.compile(_q("member") + r"\s*通过扫描\s*" + _q("inviter") + r"\s*分享的二维码加入"),
    re.compile(_q("member") + r"\s*加入了?群聊"),
)

#: 退群两种文案。
_LEAVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(_q("kicker") + r"\s*将\s*" + _q("member") + r"\s*移出了?群聊"),
    re.compile(_q("member") + r"\s*退出了?群聊"),
)

#: ``group_invite`` 的文案：「[群聊邀请] 周州 邀请你加入群聊「某某群」」
_SELF_JOIN_RE = re.compile(r"邀请你加入群聊")


@dataclass(frozen=True)
class MemberEvent:
    """一次群成员变动。

    ``kind``：

    * ``join`` / ``leave`` —— 群里**别人**变了，调用方拉该群成员增量；
    * ``self_join`` —— 机器人**自己**被拉进群，调用方拉全部群成员。

    ``member_name`` 是文本里的昵称（可能为空：老版本只给「有人加入了群聊」），
    **不是** wxid —— 不要拿它当 id 用。
    """

    kind: str
    chatroom_id: str
    member_name: str = ""
    inviter: str = ""
    raw: str = ""


def _msg_type_str(msg) -> str:
    raw = getattr(msg, "msg_type", "") or ""
    return str(raw).strip().lower()


def parse(msg) -> MemberEvent | None:
    """识别一条消息是不是成员变动事件。不是 ⇒ ``None``（不抛异常）。"""
    text = str(getattr(msg, "content", "") or "").strip()
    type_str = _msg_type_str(msg)
    chatroom_id = str(
        getattr(msg, "chatroom_id", "") or getattr(msg, "peer_id", "") or "").strip()

    # ① 机器人自己被拉进群：新群还没 id，只能让调用方拉全部群。
    if type_str == "group_invite" or _SELF_JOIN_RE.search(text):
        return MemberEvent(kind="self_join", chatroom_id=chatroom_id, raw=text[:200])

    # ② 文本判据**优先于类型**：厂商改 msgType 时这条仍能命中。
    head = text[:200]
    for pattern in _LEAVE_PATTERNS:
        m = pattern.search(head)
        if m:
            return MemberEvent(
                kind="leave", chatroom_id=chatroom_id,
                member_name=(m.groupdict().get("member") or "").strip(),
                inviter=(m.groupdict().get("kicker") or "").strip(),
                raw=head)
    for pattern in _JOIN_PATTERNS:
        m = pattern.search(head)
        if m:
            return MemberEvent(
                kind="join", chatroom_id=chatroom_id,
                member_name=(m.groupdict().get("member") or "").strip(),
                inviter=(m.groupdict().get("inviter") or "").strip(),
                raw=head)

    # ③ 已知系统类型但文本不认识 —— 留痕，等下一次样本再补正则。
    #    （这是唯一能发现「厂商又换了文案」的观测点，别静默吞掉。）
    if type_str in SYSTEM_TYPES and text:
        return MemberEvent(kind="unknown_system", chatroom_id=chatroom_id, raw=head)
    return None
