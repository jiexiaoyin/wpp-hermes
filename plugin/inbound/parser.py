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

import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

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


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


# ------------------------------------------------------------------ 群消息清洗

#: vendor 群消息正文前的 ``wxid_xxx:\n`` 发送者前缀
_SENDER_PREFIX_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{4,}:\s*\n?")


def strip_group_prefix(content: str) -> str:
    """去掉群消息的 ``wxid_xxx:\\n`` 发送者前缀（对齐 TS ``parser/content.ts stripGroupPrefix``）。"""
    if not content:
        return content
    return _SENDER_PREFIX_RE.sub("", content, count=1).strip()


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


def extract_msg_src(payload: dict) -> Optional[dict]:
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


def _extract_from_user(src: dict) -> tuple[str, str]:
    """从消息提取发送者 ``(wxid, nickname)``（支持嵌套 ``from_user`` 对象）。"""
    fu = src.get("from_user") or src.get("fromUser")
    if isinstance(fu, dict):
        wxid = fu.get("wxid") or fu.get("userName") or fu.get("UserName") or ""
        nick = fu.get("nickname") or fu.get("nickName") or fu.get("NickName") or ""
        return _safe_str(wxid), _safe_str(nick)
    wxid = (src.get("fromWxid") or src.get("FromWxid") or src.get("sender_id")
            or src.get("fromUser") or src.get("FromUserName"))
    nick = src.get("fromNickname") or src.get("FromNickName") or src.get("senderName")
    return _safe_str(wxid), _safe_str(nick)


def _extract_msg_type(src: dict) -> int:
    """取消息类型。

    ⚠️ **2026-08-31 修复必须保留**：vendor WS 推送把 ``MsgType`` 放在 ``go_fields`` 里
    （顶层没有）⇒ 只读顶层会把图片/语音/文件**全误判成文本**。
    """
    gf = src.get("go_fields") if isinstance(src.get("go_fields"), dict) else {}
    raw = (src.get("msgType") or src.get("MsgType") or src.get("msg_type")
           or gf.get("msgType") or gf.get("MsgType") or MT_TEXT)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return MT_TEXT


def parse_message(account_id: str, src: dict) -> Optional[WppMessage]:
    """把 vendor 消息 dict 归一化为 :class:`WppMessage`。"""
    from .media import extract_media_meta

    from_wxid, from_nickname = _extract_from_user(src)

    chatroom_id = _safe_str(src.get("chatroomId") or src.get("ChatroomId")
                            or src.get("FromChatroomId"))
    v1_is_group = src.get("is_group")
    if not chatroom_id and (v1_is_group is True or v1_is_group == "true" or v1_is_group == 1):
        chatroom_id = _safe_str(src.get("conversation_id"))

    content = _safe_str(src.get("content") or src.get("Content") or src.get("text"))
    conversation_id = _safe_str(src.get("conversation_id"))
    to_wxid = _safe_str(src.get("recipient_id") or src.get("toWxid")
                        or src.get("ToWxid") or src.get("ToUserName")) or conversation_id

    msg_id = _safe_str(src.get("msgId") or src.get("MsgId") or src.get("svr_id")
                       or src.get("id") or src.get("NewMsgId") or src.get("msg_id"))

    timestamp = src.get("ts") or src.get("Timestamp") or src.get("CreateTime") or src.get("created_at") or 0
    try:
        timestamp = int(timestamp)
    except (TypeError, ValueError):
        timestamp = 0

    if not from_wxid and not msg_id:
        return None

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
    """
    app = src.get("app") if isinstance(src.get("app"), dict) else None
    if app and app.get("category") == "quote" and isinstance(app.get("reference"), dict):
        ar = app["reference"]
        svrid = str(ar.get("svr_id") or ar.get("new_msg_id") or "")
        if svrid:
            return {"svrid": svrid, "from_wxid": str(ar.get("from_user_id") or ""),
                    "quote_content": "", "display_name": str(ar.get("display_name") or "")}

    rc = src.get("reply_context") or src.get("replyContext")
    if isinstance(rc, dict):
        svrid = str(rc.get("svr_id") or rc.get("msg_id") or rc.get("new_msg_id") or "")
        if svrid:
            return {"svrid": svrid, "from_wxid": str(rc.get("from_user_id") or ""),
                    "quote_content": str(rc.get("quote_content") or ""),
                    "display_name": str(rc.get("display_name") or "")}

    content = src.get("content") or src.get("Content") or ""
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
    """提取群消息里被 @ 的 wxid 列表（去重保序）。"""
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
    """去掉 ``@bot`` 文本片段（对齐 TS ``stripAtMentions``）。"""
    if not bot_wxid or not content:
        return content
    text = content[:_RE_MAX_TEXT]
    escaped = re.escape(bot_wxid)
    text = re.sub(rf"@{escaped}\s*", "", text)
    text = re.sub(rf"\b{escaped}\b\s*", "", text)
    return text.strip()


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
    if bot_nickname and f"@{bot_nickname}" in content:
        return True
    # 结构性兜底：群 @ 通知的 XML 里可能只有昵称没有 wxid
    if bot_nickname and "<atuserlist>" in content:
        m = re.search(r"<atuserlist>[\s\S]*?<username>([\s\S]*?)</username>", content)
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
        """生成发给 agent 的文本（去 @、去群前缀、带媒体标记 + 语音转写）。"""
        from .hongbao import is_red_packet_message
        from .media import voice_transcript

        text = strip_group_prefix(msg.content or "")
        text = normalize_mention(text, self.cfg.get("selfWxid", ""), self.cfg.get("nickname", ""))

        tag = self._media_tag(msg, text)
        if tag:
            text = f"{tag} {text}".strip()

        # 语音转写接在标记之后，记号 ``\n[转写] `` **逐字对齐 TS v1.3.22**——
        # 技能/提示词按这个记号认（见 skills/wpp-history），改它等于改对外协议。
        # ⚠️ 转写为空**不写这一行**：``[转写] `` 后面跟空串，模型会当成"语音内容是空的"。
        transcript = voice_transcript(msg.media)
        if transcript:
            text = f"{text}\n[转写] {transcript}"
        return text

    @staticmethod
    def _media_tag(msg: WppMessage, text: str) -> str:
        """取媒体标记。``MT_APP``(49) 要再细分 —— **顺序有讲究**（见模块 docstring ③）。"""
        if msg.msg_type != MT_APP:
            return _MEDIA_TAGS.get(int(msg.msg_type or 0), "")

        # 49 是 appmsg 卡片，里面什么都有：红包/转账/接龙/小程序/链接/文件
        from .hongbao import is_red_packet_message

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
