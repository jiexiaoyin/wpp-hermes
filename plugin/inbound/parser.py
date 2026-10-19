"""vendor 消息解析 —— 旧 ``message_parser.py`` + ``inbound.py`` 的合并迁址。

## 这一层干什么

把 vendor 三种形态的推送**归一化**成 :class:`WppMessage`：

1. WS 形态 ``Data.data.messages`` / ``items``
2. v1 形态 ``Data.messages`` / ``items``（含 ``is_group`` 标记）
3. ``EventType=sync_message`` 下的 ``Data.Data.AddMsgs``

**是纯函数层**（除 ``SeenTracker`` / ``InboundPipeline``）—— 不碰 DB、不网络。

## 相对旧实现的三处修正

### ① ``MT_FILE = 49`` 是错的（收口为 ``MT_APP``）

旧的常量表写着 ``MT_FILE = 49`` —— 但 49 是 **appmsg 卡片**（红包/转账/接龙/小程序/
链接/文件**全在里面**），不是"文件"。这个错名字是后面所有"用 49 判文件/判接龙"
误判的温床（实测华为群 49 里 转账86 / 应用消息44 / 接龙135+）。
现在只留 ``MT_APP = 49`` 一个名字，且**卡片判定只在** :mod:`inbound.hongbao`
的 ``is_card_message()`` 里定义一次。

### ② ``_is_mentioned`` 的裸昵称子串匹配被收紧

旧实现最后一行是 ``self_nickname in content`` —— **不带 ``@`` 也命中** ⇒ 任何一句
顺口提到"益融小助理"的废话都会触发回复。TS 蓝本（``parser/mention.ts
isBotMentionedByText``）要求昵称必须带 ``@`` 前缀。这里跟 TS 走，**并补上**结构性
信号 ``<atuserlist>`` XML（比文本启发式可靠）。⚠️ 这是**有意收紧**，见 ``_is_mentioned``
的 docstring —— 若日后发现真的漏 @（尤其非 ``wxid_`` 形态的微信号），排查入口在那。

### ③ ``prepare_text`` 的媒体标记补了 3 类

旧实现漏了 ``42`` 名片之外的 ``48`` 位置（有）、``10002`` 撤回（有）；但 ``49`` 的
细分优先级"接龙 > 转账 > 应用消息 > 文件"是**猜的顺序**且把**红包**漏进"文件"分支。
现在红包/转账先过（与 :mod:`inbound.hongbao` 同源），再接龙，再其余。

### ④ 语音转写接进正文（TS v1.3.22 VENDOR-TRANSCRIPT）

``media["vendor_transcript"]``（微信官方转写）或 ``media["stt_text"]``（插件自己转的，
见 :mod:`inbound.stt`）存在时，正文追加 ``\n[转写] <text>``。
**这是与 TS 的一处有意不齐**：TS 那段注入写在整个 enrich 分支里、以 ``vR.mediaUrl``
为闸（``handler.ts:373``），而 vendor 转写那条路返回的 ``mediaUrl`` 是 ``null``
⇒ **TS 会把已经拿到的官方转写丢掉**。py2 没有 OSS 那一段，照抄等于这个能力永远不生效，
所以这里只认"有没有转写文字"，不认 URL。
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# --------------------------------------------------------------------- 消息类型

MT_TEXT = 1
MT_IMAGE = 3
MT_VOICE = 34
MT_CARD = 42
MT_VIDEO = 43
MT_EMOJI = 47
MT_LOCATION = 48
#: **appmsg 卡片**：红包 / 转账 / 接龙 / 小程序 / 链接 / 文件 全由它承载。
#: 旧代码里叫 ``MT_FILE``，是错的 —— 已收口（见模块 docstring ①）。
MT_APP = 49
MT_CHAT_HISTORY = 53
#: **系统通知**（"xxx 撤回了一条消息" / "你已加入群聊" 之类）。对齐 TS
#: ``core/constants.ts:34 MsgType.SYSTEM = 10000``。
#: ⚠️ 这类消息 vendor 也会当**真人群消息**推上来 ⇒ 凡"按人说话算账"的地方
#: （好感度 W2、接话判定、debounce 直通）都要显式排除它，否则系统提示会当成人发言。
MT_SYSTEM = 10000
MT_REVOKE = 10002


@dataclass
class WppMessage:
    """归一化后的入站消息。"""

    account_id: str
    msg_id: str                                  # msgId / svr_id
    from_wxid: str                               # 发送者
    from_nickname: str = ""
    to_wxid: str = ""                            # 接收者（自己 wxid 或 filehelper）
    chatroom_id: str = ""                        # 群聊 @chatroom，空 = 私聊
    content: str = ""
    msg_type: int = MT_TEXT
    timestamp: int = 0                           # unix 秒
    direction: str = "incoming"
    raw: dict = field(default_factory=dict)
    #: 图片/文件/语音/视频元数据，见 :mod:`inbound.media`
    media: dict = field(default_factory=dict)
    #: 引用消息 ``{svrid, from_wxid, quote_content, display_name}``
    reply_to: dict = field(default_factory=dict)

    @property
    def is_group(self) -> bool:
        return bool(self.chatroom_id)

    @property
    def peer_id(self) -> str:
        """对话对象：群聊用 ``chatroom_id``，私聊用 ``from_wxid``。"""
        return self.chatroom_id or self.from_wxid

    @property
    def peer_kind(self) -> str:
        """``"group"`` / ``"dm"``（TS 的 ``peerKind`` 里叫 ``direct``，这里跟旧 Python 叫 ``dm``）。"""
        return "group" if self.is_group else "dm"

    @property
    def is_card(self) -> bool:
        """是不是 appmsg 卡片（``msg_type == 49``）。

        定义跳 :mod:`inbound.hongbao` 的 ``is_card_message`` —— 那里是**唯一真源**。
        """
        from .hongbao import is_card_message

        return is_card_message(self)


def _unwrap(v: Any) -> Any:
    """解 vendor 的 ``{"string": …}`` 包装（business callback 的旧格式）。

    对齐 TS ``parseBusinessCallbackMsg`` 的 ``unwrap``：``Data.Data.AddMsgs[]`` 里
    **每个字段**都被包成 ``{"string": "值"}``（vendor 内部 JSON 序列化的产物）。
    不解包的话下面的 ``str(v)`` 会把它变成 **Python 的 dict 字面量**
    （``"{'string': 'wxid_x'}"``）—— 它不抛、不报错，只是变成一个**不是 wxid 的 wxid**：
    发件人认不出 ⇒ 早退（整条消息静默消失），群 id 认不出 ⇒ 群消息当私聊。

    递归一层就够（实测只有一层），但写成循环以容忍 ``{"string": {"string": …}}``。
    """
    seen = 0
    while isinstance(v, dict):
        if "string" not in v or seen >= 3:
            return v
        v = v["string"]
        seen += 1
    return v


def _safe_str(v: Any) -> str:
    """``str`` + 去空白。**先解 ``{"string": …}`` 包装**（见 :func:`_unwrap`）。"""
    v = _unwrap(v)
    if isinstance(v, (dict, list)):
        # 解包后还不是标量 ⇒ 这个字段我们不认识，别把 dict 字面量当成值传播下去
        return ""
    return str(v or "").strip()


def _as_obj(v: Any) -> dict | None:
    """把 ``{"string": "{...json...}"}`` 解成 dict（AddMsgs 的复杂字段常这么裹）。

    解不出来返回 ``None``（**不抛**）：引用解析是装饰，坏一个字段不该让整条消息丢。
    """
    v = _unwrap(v)
    if isinstance(v, str):
        s = v.strip()
        if s[:1] not in "{[":
            return None
        try:
            v = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return None
    return v if isinstance(v, dict) else None


# ------------------------------------------------------------------ 群消息清洗

#: vendor 群消息正文前的**两段**前缀：``<group/room>:\n<sender>:\n<content>``。
#: ⚠️ 与 TS ``parser/content.ts stripGroupPrefix`` **逐字对齐**：必须是两段
#: ``[^:\n]+:\n``。旧实现只认一段 ``wxid_xxx:\s*\n?`` ⇒ 普通消息里一句
#: ``hello: 在吗`` 会被当成前缀吃掉开头（"hello" 长度 ≥5 就命中）。
_GROUP_PREFIX_RE = re.compile(r"^[^:\n]+:\n[^:\n]+:\n([\s\S]*)\Z")


def strip_group_prefix(content: str) -> str:
    """去掉群消息的 ``xxx:\\nsender:\\n`` 两段前缀（对齐 TS ``stripGroupPrefix``）。

    TS 的判据是"**匹配才剥**，否则原样返回"；只有 ``group1`` 非空才算匹配
    （``a:\\nb:\\n`` 这种空正文两边都不剥）。
    """
    if not content:
        return content
    m = _GROUP_PREFIX_RE.match(content)
    if m and m.group(1):
        return m.group(1).strip()
    return content


# --------------------------------------------------------------------- 提取层


def extract_all_msg_src(payload: dict) -> list[dict]:
    """从 WS payload / sync 响应提取消息字典列表。"""
    if not isinstance(payload, dict):
        return []
    out: list[dict] = []
    data = payload.get("Data")

    if isinstance(data, dict):
        # WS 形态: Data.data.messages / items
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ("messages", "items"):
                arr = inner.get(key)
                if isinstance(arr, list):
                    out += [x for x in arr if isinstance(x, dict)]
        # v1 形态: Data.messages / items
        for key in ("messages", "items"):
            arr = data.get(key)
            if isinstance(arr, list):
                out += [x for x in arr if isinstance(x, dict)]

        # AddMsgs 形态（只在上面都没捞到时才走，避免重复）
        if not out and payload.get("EventType") == "sync_message":
            deep = data.get("Data")
            if isinstance(deep, dict):
                add_msgs = deep.get("AddMsgs")
                if isinstance(add_msgs, list):
                    out += [x for x in add_msgs if isinstance(x, dict)]

    if not out:
        single = extract_msg_src(payload)
        if single:
            out.append(single)
    return out


#: 判定"这个 dict 像不像一条消息"的候选键
_MSG_HINT_KEYS = ("msgId", "MsgId", "fromUser", "fromWxid", "content", "Content")


def extract_msg_src(payload: dict) -> dict | None:
    """从单条 payload 提取消息字典。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("Data")
    if isinstance(data, dict):
        for key in ("message", "msg", "data"):
            v = data.get(key)
            if isinstance(v, dict) and any(k in v for k in _MSG_HINT_KEYS):
                return v
    if any(k in payload for k in _MSG_HINT_KEYS):
        return payload
    return None


_GROUP_PUSH_NAME_RE = re.compile(
    r"^\s*(.+?)\s*(?:在群聊中|在群里|在群聊里)\s*@\s*了你"
)

#: vendor 普通消息的推送文案是 ``昵称 : 正文``（含私聊和群聊里没 @ 的消息）。
#: 例子：``接晓银 : 你好``、``戴永刚 : 收到``。
_PUSH_NAME_COLON_RE = re.compile(r"^\s*([^:：\n]{1,32}?)\s*[:：]\s+")


def _nickname_from_push_content(src: dict) -> str:
    """从推送文案里取发送者昵称。

    两种已知形状：

    * 群 @：``戴永刚在群聊中@了你`` → ``戴永刚``
    * 普通消息：``接晓银 : 你好`` → ``接晓银``（私聊与群聊都会出现）

    为什么必须补这一级：vendor 的 v1 消息常常**只给 wxid 不给昵称**
    （``sender_id`` 有值、``fromNickname``/``senderName`` 全空），但
    ``push_content`` 里带着微信客户端生成的发送者前缀。缺了昵称，agent 只能
    拿 wxid 猜人；2026-10-18 群内实测它把戴永刚叫成了"接总"，私聊会话在
    WebUI 里也只能显示 ``q139198824`` 而不是"接晓银"。

    **只从 push 取**（正文 ``content`` 是用户自己打的字，不能拿来猜昵称）。
    名片消息的 ``[名片]xxx`` 不带"在群聊中@了你"，也不会匹配 ``昵称 :``
    （冒号后必须跟空白）。
    """
    push = _safe_str(src.get("push_content") or src.get("PushContent")
                     or src.get("pushContent"))
    if not push:
        return ""
    m = _GROUP_PUSH_NAME_RE.match(push)
    if not m:
        m = _PUSH_NAME_COLON_RE.match(push)
    if not m:
        return ""
    return m.group(1).strip().strip("「」『』\"'“”‘’ ")


def _extract_from_user(src: dict) -> tuple[str, str]:
    """从消息提取发送者 ``(wxid, nickname)``（支持嵌套 ``from_user`` 对象）。"""
    fu = _as_obj(src.get("from_user") or src.get("fromUser"))
    if isinstance(fu, dict):
        wxid = fu.get("wxid") or fu.get("userName") or fu.get("UserName") or ""
        nick = fu.get("nickname") or fu.get("nickName") or fu.get("NickName") or ""
        return _safe_str(wxid), _safe_str(nick or _nickname_from_push_content(src))
    wxid = (src.get("fromWxid") or src.get("FromWxid") or src.get("sender_id")
            or src.get("fromUser") or src.get("FromUserName"))
    nick = src.get("fromNickname") or src.get("FromNickName") or src.get("senderName")
    return _safe_str(wxid), _safe_str(nick or _nickname_from_push_content(src))


def _extract_msg_type(src: dict) -> int:
    """取消息类型。

    ⚠️ **2026-08-31 修复必须保留**：vendor WS 推送把 ``MsgType`` 放在 ``go_fields`` 里
    （顶层没有）⇒ 只读顶层会把图片/语音/文件**全误判成文本**。

    ⚠️ **2026-10-02 补 v1 的 ``type``**（A1）：v1（``wechatpad.message.v1``）那条推送
    用的字段名是 ``type``（TS ``parseV1Message``：``num(msg.type, 1)``）。不认它 ⇒
    v1 那一路的消息类型**恒为文本**，于是媒体标记、接龙、红包、撤回这些"按类型分岔"
    的判据跟着一起错 —— 而且不报错、日志看着完全正常。

    老字段（``msgType`` / ``MsgType`` / ``msg_type``）**优先**，``type`` 只当兜底：
    别的 payload 里另有含义的 ``type`` 不许压过它自己的 ``MsgType``。
    """
    gf = src.get("go_fields") if isinstance(src.get("go_fields"), dict) else {}
    raw = (src.get("msgType") or src.get("MsgType") or src.get("msg_type")
           or gf.get("msgType") or gf.get("MsgType")
           or src.get("type") or gf.get("type") or MT_TEXT)
    try:
        return int(_unwrap(raw))      # business callback 的 {"string": "49"} 也要认
    except (TypeError, ValueError):
        return MT_TEXT


def parse_message(account_id: str, src: dict) -> WppMessage | None:
    """把 vendor 消息 dict 归一化为 :class:`WppMessage`。"""
    from .media import extract_media_meta

    from_wxid, from_nickname = _extract_from_user(src)

    chatroom_id = _safe_str(src.get("chatroomId") or src.get("ChatroomId")
                            or src.get("FromChatroomId"))
    conversation_id = _safe_str(src.get("conversation_id"))
    recipient_id = _safe_str(src.get("recipient_id") or src.get("toWxid")
                             or src.get("ToWxid"))
    # ── 群 id 兜底（A2 + A5，对齐 TS 两段实现）─────────────────────────────
    # TS ``parseV1Message``：candidates = [conversation_id, recipient_id, sender_id]，
    #   取**第一个以 ``@chatroom`` 结尾**的当群 id —— **不看 ``is_group`` 标记**；
    # TS ``parseBusinessCallbackMsg``：同一判据，但候选字段是 ``FromUserName`` /
    #   ``ToUserName``（旧格式群里那个字段本身就是群 id）。
    # 少了这一段，v1/旧格式的群消息一旦 ``is_group`` 缺失就被**当成私聊**：
    #   peer 变成某个人 ⇒ 心流按人建会话、回复发成私聊，群白名单/群预算/群策略全绕开。
    v1_is_group = _unwrap(src.get("is_group"))
    is_group_flag = v1_is_group is True or v1_is_group == "true" or v1_is_group == 1
    if not chatroom_id:
        for cand in (conversation_id, recipient_id,
                     _safe_str(src.get("FromUserName")), _safe_str(src.get("ToUserName")),
                     from_wxid):
            if cand.endswith("@chatroom"):
                chatroom_id = cand
                break
    if not chatroom_id and is_group_flag:
        chatroom_id = conversation_id

    content = _safe_str(src.get("content") or src.get("Content") or src.get("text"))
    to_wxid = recipient_id or _safe_str(src.get("ToUserName")) or conversation_id

    raw_msg_id = _safe_str(src.get("msgId") or src.get("MsgId") or src.get("svr_id")
                           or src.get("id") or src.get("NewMsgId") or src.get("msg_id"))

    timestamp = src.get("ts") or src.get("Timestamp") or src.get("CreateTime") or src.get("created_at") or 0
    try:
        timestamp = int(_unwrap(timestamp))
    except (TypeError, ValueError):
        timestamp = 0

    if not from_wxid and not raw_msg_id:
        return None

    # A3：没有 id ⇒ 合成一个**每次不同**的（对齐 TS ``rawMsgId || `${ts}-${random}``）。
    #   ⚠️ **不能**留空串：去重键是 ``f"{acct}:{msg_id}"``（platform/adapter.py），
    #   空串 ⇒ 第一条之后**所有**无 id 的消息共用一个键 ⇒ 全被判重复、**静默丢弃**
    #   （09-27 接龙整晚消失那一类失效的另一种形态）。
    #   合成 id 每次都不同 ⇒ 无 id 的消息**不去重**：宁可重放（看得见、有心流预算兜底），
    #   不可静默吞；有 id 的消息照旧去重。
    msg_id = raw_msg_id or f"{timestamp}-{secrets.token_hex(4)}"

    # ⚠️ **已知分歧（A9，2026-10-02 审阅）—— 有意不实现，别照抄半截**：
    #
    # TS 在 ``parseV1Message`` 里**故意放行** outgoing 图片（``src/inbound/parser.ts``
    # 的 ``isOutgoingImage``，v1.3.21 REVOKE-FIX）：插件自己发的图片被 vendor 推回来
    # 时，靠它把**真实 server id** 落进 ``wpp_messages``（``enrich.ts`` 按
    # ``msg.direction`` 写 ``direction='outbound'``），图片撤回要用它 ——
    # ``UploadImg`` 返回的 ``Newmsgid`` 是上传凭证，不是 server 消息 ID。
    #
    # 本树**仍然不放行**，但理由已随 W1 变化（**别再照抄旧理由**）：
    #
    # ① 旧理由「没有消费者」**已过期**：入站落库在 W1 落地了
    #    （``plugin/inbound/persist.py``，调用点 ``adapter._handle_one`` 的 ④z）。
    # ② 但那个消费者**在 self 门之后**：``_handle_one`` 的 ① 按
    #    ``from_wxid == selfWxid`` 早退 —— 而 outgoing 图片的 ``from_wxid`` 正是机器人
    #    自己 ⇒ 放行进来也**照样落不了库**。
    #    实测 TS 侧为什么能落（``src/inbound/parser.ts:170-177`` +
    #    ``triggers.ts:115-208``）：TS 在 **parser** 里放行 outgoing 图片，而
    #    ``shouldTrigger`` 对它给的是 ``via`` ∈ {``msgType``/``quoteBot``/``null``}，
    #    **不是 ``blocked``** ⇒ 它和普通白名单消息一起进 ``persistBatch``，
    #    靠 ``direction=outbound`` 记成出站 —— 没有第二条专用写入路径。
    #    所以本树要做等价的事，前置是"把 self 门拆成两半"：**只落库不喂 LLM**
    #    的那一半放在 ④z，喂 LLM 的那一半留在原处（⑤ 之前）。这条**独立于 W1**，
    #    没做之前放行只是换个地方丢，且会顺带把 bot 自己的说话内容喂进 ⑤ 之后的链路。
    # ③ 另一条独立事实：图片撤回要用的是 ``UploadImg`` 的真实 server id
    #    （``core/api_client.py::revoke_msg`` 需要 ``(MsgId, NewMsgId, CreateTime)``
    #    三元组）—— 真要做先验证 vendor 回推的那条 outgoing 图片确实带得回它。
    #
    # 判据钉在 ``plugin/tests/test_parser_v1.py::test_an_outgoing_image_is_not_admitted_here``。
    return WppMessage(
        account_id=account_id,
        msg_id=msg_id,
        from_wxid=from_wxid,
        from_nickname=from_nickname,
        to_wxid=to_wxid,
        chatroom_id=chatroom_id,
        content=content,
        msg_type=_extract_msg_type(src),
        timestamp=timestamp,
        direction=_safe_str(src.get("direction")),
        raw=src,
        media=extract_media_meta(src),
        reply_to=extract_quote_ref(src),
    )


def extract_quote_ref(src: dict) -> dict:
    """提取被引用消息信息（引用回复用）。

    三种来源（对齐 TS ``inbound/parser/quote.ts``）：

    1. ``app.reference``（category=quote，新版 vendor，含 ``display_name``）
    2. ``reply_context``（新版 vendor）
    3. ``content`` 里的 ``<refermsg>`` XML（旧版）

    ``display_name`` 优先用微信给的（群友非联系人也能拿到昵称）。

    ⚠️ **AddMsgs 形态同样要认**（2026-10-19 修）：business callback 的
    ``Data.Data.AddMsgs[]`` 把每个字段包成 ``{"string": …}``，复杂对象（``app`` /
    ``reply_context``）还常被序列化成**字符串**。旧实现只认原生 dict ⇒ 这一路的
    引用全部丢成 ``{}``，``@被回复人`` 与引用卡片都失效（且不报错）。
    """
    app = _as_obj(src.get("app"))
    if app and app.get("category") == "quote" and isinstance(app.get("reference"), dict):
        ar = app["reference"]
        svrid = _safe_str(ar.get("svr_id") or ar.get("new_msg_id"))
        if svrid:
            return {"svrid": svrid, "from_wxid": _safe_str(ar.get("from_user_id")),
                    "quote_content": "", "display_name": _safe_str(ar.get("display_name"))}

    rc = _as_obj(src.get("reply_context") or src.get("replyContext"))
    if isinstance(rc, dict):
        svrid = _safe_str(rc.get("svr_id") or rc.get("msg_id") or rc.get("new_msg_id"))
        if svrid:
            return {"svrid": svrid, "from_wxid": _safe_str(rc.get("from_user_id")),
                    "quote_content": _safe_str(rc.get("quote_content")),
                    "display_name": _safe_str(rc.get("display_name"))}

    content = _safe_str(src.get("content") or src.get("Content"))
    if isinstance(content, str) and "<refermsg" in content:
        m = re.search(r"<refermsg\b[^>]*>([\s\S]*?)</refermsg>", content)
        if m:
            inner = m.group(1)

            def _tag(name: str) -> str:
                mm = re.search(rf"<{name}>([\s\S]*?)</{name}>", inner)
                return mm.group(1).strip() if mm else ""

            svrid = _tag("svrid")
            if svrid:
                return {"svrid": svrid, "from_wxid": _tag("fromusr"),
                        "quote_content": "", "display_name": _tag("displayname")}
    return {}


# ------------------------------------------------------------------- @ 提及层

#: @ 提取的候选 pattern（对齐 TS ``AT_MENTION_PATTERNS``，用 ``re`` 的等价写法）。
#: 覆盖 6 种 vendor 形态：``wxid_x@nick`` / ``@wxid_x`` / ``<at user=>`` /
#: ``<atuserlist><username>`` / 微信号形态 / ``@昵称``。
_AT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(wxid_[a-zA-Z0-9]+)@[^,\s]*"),
    re.compile(r"@(wxid_[a-zA-Z0-9]+)"),
    re.compile(r'<at\s+user="(wxid_[a-zA-Z0-9]+)"'),
    re.compile(r"<atuserlist>[\s\S]*?<username>(wxid_[a-zA-Z0-9]+)</username>"),
    re.compile(r"<atuserlist>[\s\S]*?<username>([a-zA-Z][a-zA-Z0-9_-]{5,})</username>"),
    re.compile(r"@([a-zA-Z][a-zA-Z0-9_-]{5,})[^\s@]*"),
)

#: 文本处理前的截断长度（防 ReDoS —— gewe A1 教训）
_RE_MAX_TEXT = 4096


def extract_at_user_list(content: str) -> list[str]:
    """提取群消息里被 @ 的候选标识（去重保序）。

    ⚠️ **与 TS 的一处有意不齐**：TS ``extractAtUserList`` 出参前还过一道
    ``isValidAtUser``（``^(wxid_|gh_)``），于是它自己那条"微信号形态"pattern
    **恒被自己的格式闸滤掉** —— 而那 pattern 正是为 "selfWxid 是**微信号**不是
    ``wxid_``" 补的（TS 注释原文：之前硬要求 ``wxid_`` 前缀漏判）。本树**保留**
    这些候选：调用方 ``is_bot_mentioned_by_text`` 只拿它做**成员判断**，多看几个
    候选不会造假阳性（要假阳性得候选恰好等于 bot 的 wxid/微信号，那必须正文里
    真写了它），反过来滤掉才会重演 "微信号 self_wxid 在群里被 @ 却判成没叫"。
    代价：列表里可能出现 ``everyone`` 这类非 wxid 的 @ 目标 —— 所以调用方
    **不许**把它当 wxid 直接使用（本树只有成员判断这一个调用方）。
    """
    if not content:
        return []
    text = content[:_RE_MAX_TEXT]
    out: list[str] = []
    for pat in _AT_PATTERNS:
        for m in pat.finditer(text):
            wxid = m.group(1)
            if wxid and wxid not in out:
                out.append(wxid)
    return out


def strip_at_mentions(content: str, bot_wxid: str) -> str:
    """去掉 ``@bot`` 文本片段（对齐 TS ``stripAtMentions``）。

    ⚠️ 两处必须知道的行为：

    1. **``\\b`` 走 ``re.ASCII``**（A7）：JS 的 ``\\w`` 只有 ASCII，所以
       ``"你好wxid_bot"`` 在 TS 侧有词边界、会被剥掉；Python 的 ``re`` 默认
       **Unicode** 语义 —— 中文也算词字符，于是**不剥**。加了 ``re.ASCII`` 才与
       TS 逐字一致（实测：不加时 ``你好wxid_bot 在吗`` 原样返回，加了得 ``你好在吗``）。
    2. **超过 4096 的尾巴会被**截掉**（TS 同）**：返回的是处理后的**截断文本**，
       不是"原文减去提及"。所以它只能喂给"看正文"的路径，绝不能拿它当正文落库
       —— 本树也因此**没有生产调用方**（@ 判定只需要
       :func:`is_bot_mentioned_by_text`，不需要清洗正文；TS 侧的调用者
       ``cleanGroupMessage`` 也只存在于零调用者的 ``group-policy.ts`` 路径里）。
       它作为对外 helper 保留并在此钉住行为，是为了将来真接上时不用重新推一遍。
    """
    if not bot_wxid or not content:
        return content
    text = content[:_RE_MAX_TEXT]
    escaped = re.escape(bot_wxid)
    text = re.sub(rf"@{escaped}\s*", "", text)
    text = re.sub(rf"\b{escaped}\b\s*", "", text, flags=re.ASCII)
    return text.strip()


def _nickname_mentioned(content: str, nickname: str) -> bool:
    """``@昵称`` 命中，但**后面紧跟英文/数字/下划线**时不算另一个昵称的前缀。

    旧判据是裸子串 ``@小助理 in content`` ⇒ 群里 @小助理2 也会叫醒小助理
    （两个 bot 同名前缀时会互相抢答）。TS 没有昵称分支（只认 wxid），这条是
    py2 的扩展，边界规则也由 py2 自己定：CJK 后紧跟中文无法消歧，仍按命中处理。
    """
    pattern = re.compile(rf"@{re.escape(nickname)}(?![A-Za-z0-9_])")
    return bool(pattern.search(content))


def is_bot_mentioned_by_text(content: str, bot_wxid: str, bot_nickname: str = "") -> bool:
    """正文里是否 @ 了 bot（对齐 TS ``isBotMentionedByText`` + 昵称分支）。

    ⚠️ **与旧 Python 实现的差异**：旧的 ``_is_mentioned`` 末行是
    ``self_nickname in content`` —— 裸子串，不带 ``@`` 也命中。这里跟 TS 走：
    昵称分支**必须带 ``@``**（``@益融小助理`` 命中，句子里顺口提到昵称不命中），
    另外补上 TS 的结构性信号 ``<atuserlist>`` XML 兜底。
    """
    if not content:
        return False
    if bot_wxid:
        if f"@{bot_wxid}" in content:
            return True
        if f" {bot_wxid}" in content:
            return True
        if bot_wxid in extract_at_user_list(content):
            return True
    if bot_nickname and _nickname_mentioned(content, bot_nickname):
        return True
    # 结构性兜底：群 @ 通知的 XML 里可能只有昵称没有 wxid。
    # ⚠️ 只截断**喂给正则**的那一份：本模块的纪律是"正文进正则前先截 4096"
    # （防 ReDoS），上面两条 ``in`` 子串判断不需要（它们是线性扫描）。
    if bot_nickname and "<atuserlist>" in content:
        m = re.search(r"<atuserlist>[\s\S]*?<username>([\s\S]*?)</username>",
                      content[:_RE_MAX_TEXT])
        if m and m.group(1).strip() == bot_nickname:
            return True
    return False


def normalize_mention(content: str, self_wxid: str, self_nickname: str) -> str:
    """去掉内容里的 @bot 提及，避免 AI 重复回 @。"""
    c = content or ""
    if self_wxid:
        c = c.replace(f"@{self_wxid}", "").replace(f"{self_wxid}@", "")
    if self_nickname:
        c = c.replace(f"@{self_nickname}", "")
    return c.strip()


# ----------------------------------------------------------------- 去重 / 管线


class SeenTracker:
    """内存去重（LRU）。配合 DB UNIQUE 实现 at-least-once。

    ⚠️ 这是**进程内**缓存。适配器**不要**把它当唯一去重手段：
    Hermes runner 的 ``carry_inbound_dedup`` 只认
    ``gateway.platforms.helpers.MessageDeduplicator`` 实例（见
    ``platform/adapter.py`` 里持有的 ``self._dedup``）。本类保留给
    非 runner 路径（standalone / 测试）用。
    """

    def __init__(self, maxsize: int = 20000) -> None:
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._maxsize = maxsize

    def is_dup(self, key: str) -> bool:
        if key in self._seen:
            return True
        self._seen[key] = time.time()
        if len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)   # FIFO 淘汰最老的（OrderedDict，插序即时间序）
        return False

    def clear(self) -> None:
        self._seen.clear()


#: ``msg_type`` → 注入 agent 正文的媒体标记（顺序即优先级）
_MEDIA_TAGS: dict[int, str] = {
    MT_IMAGE: "[图片]",
    MT_VOICE: "[语音]",
    MT_VIDEO: "[视频]",
    MT_EMOJI: "[表情]",
    MT_CARD: "[名片]",
    MT_LOCATION: "[位置]",
    MT_REVOKE: "[撤回消息]",
    MT_CHAT_HISTORY: "[接龙]",
}


_CARD_NAME_RE = re.compile(r"\[名片\]\s*([^\s:：]+)")


def _card_name_from_push(raw: Any) -> str:
    """从 ``push_content`` 提取名片名（对齐 TS ``handler.ts:322``）。

    vendor 字段名在不同版本里是 ``push_content`` / ``PushContent`` / ``pushContent``；
    取不到就不注入（**不要**从 ``content`` 猜 —— 那里面是用户自己打的字）。
    """
    if not isinstance(raw, dict):
        return ""
    push = (raw.get("push_content") or raw.get("PushContent")
            or raw.get("pushContent"))
    if not isinstance(push, str):
        return ""
    m = _CARD_NAME_RE.search(push)
    return m.group(1).strip() if m else ""


class InboundPipeline:
    """入站消息管线：extract（含去重）→ decide → prepare_text。"""

    def __init__(self, account_id: str, cfg: dict) -> None:
        self.account_id = account_id
        self.cfg = cfg
        self.seen = SeenTracker()

    # ------------------------------------------------------------------ 提取

    def extract(self, payload: dict) -> list[WppMessage]:
        """从 WS/sync payload 提取并过滤原始消息（含进程内去重）。"""
        out: list[WppMessage] = []
        for src in extract_all_msg_src(payload):
            try:
                msg = parse_message(self.account_id, src)
            except Exception as exc:  # noqa: BLE001 —— 单条坏消息不该打断整批
                log.warning("[WPP:%s] 解析消息失败: %s", self.account_id, exc)
                continue
            if msg is None:
                continue
            if msg.msg_id and self.seen.is_dup(f"{self.account_id}:{msg.msg_id}"):
                from ..core import metrics

                metrics.inc_messages_rejected_dedupe()
                log.debug("[WPP:%s] 重复消息: %s", self.account_id, msg.msg_id)
                continue
            out.append(msg)
        return out

    # ------------------------------------------------------------------ 判定

    def decide(self, msg: WppMessage) -> tuple[bool, str]:
        """判断是否触发 agent（:mod:`inbound.triggers` 的薄封装）。"""
        from .triggers import should_process

        return should_process(msg, self.cfg)

    # -------------------------------------------------------------- 正文准备

    def prepare_text(self, msg: WppMessage) -> str:
        """生成发给 agent 的文本（去 @、去群前缀、带媒体标记 + 语音转写 + 小程序卡片块）。"""
        from .app_card import mini_program_block
        from .media import voice_transcript

        text = strip_group_prefix(msg.content or "")
        text = normalize_mention(text, self.cfg.get("selfWxid", ""), self.cfg.get("nickname", ""))

        tag = self._media_tag(msg, text)
        if tag:
            text = f"{tag} {text}".strip()

        # 名片（42）：vendor 把名片名放在 ``push_content``（如 ``[名片]龙脉``），
        # 正文里往往只有一个 ``[名片]`` 标记 ⇒ AI 不知道是谁的名片。
        # 逐字对齐 TS ``handler.ts:322-335``（V-CARD）。
        if int(msg.msg_type or 0) == MT_CARD:
            card_name = _card_name_from_push(msg.raw)
            if card_name:
                text = f"{text}\n[名片] {card_name}"

        # W7-2 小程序卡片：49 里**唯一**"内容比一行标记多得多"的一类，单独追加结构块
        # （``[小程序卡片] 标题 / [小程序] 谁 (appid …, gh_…@app) / 页面路径 / 说明 / 封面``）。
        # 位置与 TS 一致：接在正文之后、别的注入之前（``handler.ts:466-496``）。
        # 非小程序卡片返回空串 ⇒ 与旧正文逐字节相同。
        block = mini_program_block(msg)
        if block:
            text = f"{text}\n{block}"

        # 语音转写接在标记之后，记号 ``\n[转写] `` **逐字对齐 TS v1.3.22**——
        # 技能/提示词按这个记号认（见 skills/wpp-history），改它等于改对外协议。
        # ⚠️ 转写为空**不写这一行**：``[转写] `` 后面跟空串，模型会当成"语音内容是空的"。
        transcript = voice_transcript(msg.media)
        if transcript:
            text = f"{text}\n[转写] {transcript}"
        return text

    @staticmethod
    def _media_tag(msg: WppMessage, text: str) -> str:
        """取媒体标记。``MT_APP``(49) 要再细分 —— **顺序有讲究**（见模块 docstring ③）。

        W7-1：``oss_url`` 已写进 ``msg.media`` ⇒ 正文里**已经有** enrich 追加的那一行
        （``\\n[图片] https://…``，自带标记）⇒ 这里返回空串，否则正文里会出现两个
        ``[图片]``。⚠️ TS 那边没有这个函数（enrich 直接改正文、也没有前缀标记这一层），
        所以**没有**对应分支 —— 这是 py2 的适配，判据只能是"我们自己写进去的那个键"。

        W7-3：同理，接龙在 ④d 已被 :func:`inbound.relay.apply_relay_render` **整条改写成**
        ``[接龙] title\\n1. …`` ⇒ 这里再打一个 ``[接龙]`` 前缀就成了"接龙 接龙"。
        两道抑制都放在**最前面**（排在 49 细分之前）—— 放在细分之后会让某一类漏过去
        （W7-2 的教训：顺序错了，测试还全绿）。
        """
        if msg.media.get("oss_url"):
            return ""
        if msg.media.get("relay_rendered"):
            return ""

        # ★ 语音判据是"带不带语音元数据"，**不是** ``msg_type == 34``。
        #   TS 语音本来就有两路（``MsgType.VOICE`` 与 49 appmsg 里的 ``<voicemsg>``，
        #   ``handler.ts:357/363``）；py2 目前只解析前一路，但 vendor 转写
        #   （``voice.transcript``）**与类型无关**——真出现 49/文本形态的语音时，
        #   下面 49 的兜底会给 ``[文件]``、文本类型给空串 ⇒ 模型看到「你发了个文件」，
        #   而 ``[转写]`` 行又暴露了这是句话（自相矛盾）。带语音元数据一律 ``[语音]``。
        from .media import voice_transcript
        if msg.media.get("voice_ctx") or voice_transcript(msg.media):
            return "[语音]"

        if msg.msg_type != MT_APP:
            return _MEDIA_TAGS.get(int(msg.msg_type or 0), "")

        # 49 是 appmsg 卡片，里面什么都有：红包/转账/接龙/小程序/链接/文件
        from .app_card import is_mini_program_card
        from .hongbao import is_red_packet_message

        # ★ 引用回复（49 里的 appmsg type=57）**不是文件**：正文就是回复的那句话，
        #   被引用内容走 ``reply_to``（→ Hermes 的 reply_to_text / reply_to_message_id）。
        #   .183 真机 2026-10-18：纯文本的引用回复被标成 ``[文件]``，模型据此回
        #   「你发的是卡片/文件类消息，我这边只读到文字」—— 一个正常回复被说成附件。
        #   判据复用 ``extract_quote_ref`` 的三条来源（``app.reference`` /
        #   ``reply_context`` / ``<refermsg>`` XML），与 TS ``quote.ts`` 同口径。
        #   ⚠️ 必须排在下面那些细分**之前**：quote 的 ``content`` 是回复文本，
        #   不含「转账/接龙」等词时就会掉进最后那道 ``[文件]`` 兜底。
        raw = msg.raw if isinstance(msg.raw, dict) else {}
        if msg.reply_to or extract_quote_ref(raw):
            return ""
        if is_mini_program_card(msg.raw):
            # W7-2：小程序卡片**不带一行标记**（TS 那边也没有 —— 卡片信息走
            # ``mini_program_block`` 的追加块，首行就是 ``[小程序卡片] 标题``）。
            # ⚠️ 以前它会掉进最后那道 ``[文件]`` 兜底 —— 一个**错标签**：
            #    模型看到 ``[文件] 快来领取政府家电数码补贴`` 只会答"你发了个文件"。
            return ""
        if is_red_packet_message(msg):
            return "[红包]"
        if "转账" in text or "收款" in text:
            return "[转账]"
        if "接龙" in text:
            return "[接龙]"
        if "应用消息" in text:
            return "[应用消息]"
        return "[文件]"


__all__ = [
    "MT_APP",
    "MT_CARD",
    "MT_CHAT_HISTORY",
    "MT_EMOJI",
    "MT_IMAGE",
    "MT_LOCATION",
    "MT_REVOKE",
    "MT_SYSTEM",
    "MT_TEXT",
    "MT_VIDEO",
    "MT_VOICE",
    "InboundPipeline",
    "SeenTracker",
    "WppMessage",
    "extract_all_msg_src",
    "extract_at_user_list",
    "extract_msg_src",
    "extract_quote_ref",
    "is_bot_mentioned_by_text",
    "normalize_mention",
    "parse_message",
    "strip_at_mentions",
    "strip_group_prefix",
]
