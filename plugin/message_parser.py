"""WPP 消息解析（迁移自 astrbot-plugin-wpp _extract_all_msg_src + wpp-openclaw parser.ts）。

支持三种形态：
1. WS Data.data.messages / items
2. v1 messages / items（含 is_group 标记）
3. AddMsgs（EventType=sync_message）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# 微信消息类型（与 vendor MsgType 对齐）
MT_TEXT = 1
MT_IMAGE = 3
MT_VOICE = 34
MT_VIDEO = 43
MT_EMOJI = 47
MT_CARD = 42
MT_FILE = 49


@dataclass
class WppMessage:
    account_id: str
    msg_id: str                    # msgId / svr_id
    from_wxid: str                 # 发送者
    from_nickname: str = ""
    to_wxid: str = ""              # 接收者（自己 wxid 或 filehelper）
    chatroom_id: str = ""          # 群聊 @chatroom，空=私聊
    content: str = ""
    msg_type: int = MT_TEXT
    timestamp: int = 0             # unix 秒
    direction: str = "incoming"
    raw: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)   # 图片/文件元数据 {cdn, msg_id, to_wxid, data_len, attach_id, filename}
    reply_to: dict = field(default_factory=dict)  # 引用消息 {svrid, from_wxid, quote_content}

    @property
    def is_group(self) -> bool:
        return bool(self.chatroom_id)

    @property
    def peer_id(self) -> str:
        """对话对象：群聊用 chatroom_id，私聊用 from_wxid。"""
        return self.chatroom_id or self.from_wxid

    @property
    def peer_kind(self) -> str:
        return "group" if self.is_group else "dm"


def _safe_str(v) -> str:
    return str(v or "").strip()


def extract_all_msg_src(payload: dict) -> list[dict]:
    """从 WS payload / sync 响应提取消息字典列表。"""
    if not isinstance(payload, dict):
        return []
    out: list[dict] = []
    data = payload.get("Data")

    # WS 形态 (Data.data.messages)
    if isinstance(data, dict):
        data_inner = data.get("data")
        if isinstance(data_inner, dict):
            for kk in ("messages", "items"):
                arr = data_inner.get(kk)
                if isinstance(arr, list):
                    for item in arr:
                        if isinstance(item, dict):
                            out.append(item)

    # 形态 A: v1 messages / items
    if isinstance(data, dict):
        for kk in ("messages", "items"):
            arr = data.get(kk)
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict):
                        out.append(item)

    # 形态 B: AddMsgs
    if not out and payload.get("EventType") == "sync_message":
        if isinstance(data, dict):
            data_inner = data.get("Data")
            if isinstance(data_inner, dict):
                add_msgs = data_inner.get("AddMsgs")
                if isinstance(add_msgs, list):
                    for item in add_msgs:
                        if isinstance(item, dict):
                            out.append(item)

    # 单条
    if not out:
        single = extract_msg_src(payload)
        if single:
            out.append(single)
    return out


def extract_msg_src(payload: dict) -> Optional[dict]:
    """从单条 payload 提取消息字典。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("Data")
    if isinstance(data, dict):
        for kk in ("message", "msg", "data"):
            v = data.get(kk)
            if isinstance(v, dict) and any(x in v for x in ("msgId", "MsgId", "fromUser", "fromWxid", "content", "Content")):
                return v
    # payload 本身可能就是消息
    if any(x in payload for x in ("msgId", "MsgId", "fromUser", "fromWxid", "content", "Content")):
        return payload
    return None


def _extract_from_user(src: dict) -> tuple[str, str]:
    """从消息提取发送者 wxid + nickname（支持嵌套 from_user 对象）。"""
    fu = src.get("from_user") or src.get("fromUser")
    if isinstance(fu, dict):
        wxid = fu.get("wxid") or fu.get("userName") or fu.get("UserName") or ""
        nick = fu.get("nickname") or fu.get("nickName") or fu.get("NickName") or ""
        return _safe_str(wxid), _safe_str(nick)
    wxid = src.get("fromWxid") or src.get("FromWxid") or src.get("sender_id") or src.get("fromUser") or src.get("FromUserName")
    nick = src.get("fromNickname") or src.get("FromNickName") or src.get("senderName")
    return _safe_str(wxid), _safe_str(nick)


def parse_message(account_id: str, src: dict) -> Optional[WppMessage]:
    """把 vendor 消息 dict 归一化为 WppMessage。"""
    from_wxid, from_nickname = _extract_from_user(src)
    chatroom_id = _safe_str(src.get("chatroomId") or src.get("ChatroomId") or src.get("FromChatroomId"))
    v1_is_group = src.get("is_group")
    if not chatroom_id and (v1_is_group is True or v1_is_group == "true" or v1_is_group == 1):
        chatroom_id = _safe_str(src.get("conversation_id"))
    content = _safe_str(src.get("content") or src.get("Content") or src.get("text"))
    to_wxid = _safe_str(src.get("recipient_id") or src.get("toWxid") or src.get("ToWxid") or src.get("ToUserName"))
    conversation_id = _safe_str(src.get("conversation_id"))
    if not to_wxid:
        to_wxid = conversation_id
    msg_id = _safe_str(src.get("msgId") or src.get("MsgId") or src.get("svr_id") or src.get("id") or src.get("NewMsgId") or src.get("msg_id"))
    # 2026-08-31 fix: vendor WS 推送的 MsgType 在 go_fields 里(顶层没有) → 图片/语音/文件被误判为文本
    _gf = src.get("go_fields") if isinstance(src.get("go_fields"), dict) else {}
    msg_type = (src.get("msgType") or src.get("MsgType") or src.get("msg_type")
                or _gf.get("msgType") or _gf.get("MsgType") or MT_TEXT)
    try:
        msg_type = int(msg_type)
    except (TypeError, ValueError):
        msg_type = MT_TEXT
    direction = _safe_str(src.get("direction"))
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
        msg_type=msg_type,
        timestamp=timestamp,
        direction=direction,
        raw=src,
        media=extract_media_meta(src),
        reply_to=extract_quote_ref(src),
    )


def extract_quote_ref(src: dict) -> dict:
    """提取被引用消息信息（引用回复用）。

    三种来源（对齐 wpp quote.ts）：
    1. app.reference（category=quote，新版 vendor，含 display_name 被回复人昵称）
    2. reply_context（新版 vendor：msg_id/new_msg_id/svr_id/quote_content/from_user_id）
    3. content 里的 <refermsg> XML（旧版：svrid/fromusr/displayname）
    昵称 display_name 优先用微信提供的（群友非联系人也能拿到昵称）。
    """
    ref: dict = {}
    # 1. app.reference（新版，category=quote，含 display_name）
    app = src.get("app") if isinstance(src.get("app"), dict) else None
    if app and app.get("category") == "quote" and isinstance(app.get("reference"), dict):
        ar = app["reference"]
        svrid = str(ar.get("svr_id") or ar.get("new_msg_id") or "")
        ref = {
            "svrid": svrid,
            "from_wxid": str(ar.get("from_user_id") or ""),
            "quote_content": "",
            "display_name": str(ar.get("display_name") or ""),
        }
        if svrid:
            return ref
    # 2. reply_context（新版）
    rc = src.get("reply_context") or src.get("replyContext")
    if isinstance(rc, dict):
        svrid = str(rc.get("svr_id") or rc.get("msg_id") or rc.get("new_msg_id") or "")
        ref = {
            "svrid": svrid,
            "from_wxid": str(rc.get("from_user_id") or ""),
            "quote_content": str(rc.get("quote_content") or ""),
            "display_name": str(rc.get("display_name") or ""),
        }
        if svrid:
            return ref
    # 3. <refermsg> XML（旧版，含 displayname 被回复人昵称）
    content = src.get("content") or src.get("Content") or ""
    if isinstance(content, str) and "<refermsg" in content:
        import re
        m = re.search(r"<refermsg\b[^>]*>([\s\S]*?)</refermsg>", content)
        if m:
            inner = m.group(1)
            svrid = ""
            m2 = re.search(r"<svrid>([\s\S]*?)</svrid>", inner)
            if m2:
                svrid = m2.group(1).strip()
            fromusr = ""
            m3 = re.search(r"<fromusr>([\s\S]*?)</fromusr>", inner)
            if m3:
                fromusr = m3.group(1).strip()
            display_name = ""
            m4 = re.search(r"<displayname>([\s\S]*?)</displayname>", inner)
            if m4:
                display_name = m4.group(1).strip()
            if svrid:
                return {
                    "svrid": svrid,
                    "from_wxid": fromusr,
                    "quote_content": "",
                    "display_name": display_name,
                }
    return {}


def extract_media_meta(src: dict) -> dict:
    """提取图片/文件/语音元数据（用于下载）。"""
    media: dict = {}
    # 图片 CDN
    cdn = src.get("cdn") or src.get("image") or src.get("img")
    if isinstance(cdn, dict):
        # vendor 真实字段: aes_key / standard_file_no / cdn_download_contexts[0].file_aes_key
        cdn_dl = cdn.get("cdn_download_contexts")
        ctx0 = cdn_dl[0] if isinstance(cdn_dl, list) and cdn_dl and isinstance(cdn_dl[0], dict) else {}
        media["cdn"] = {
            "file_aes_key": (cdn.get("file_aes_key") or cdn.get("FileAesKey")
                             or cdn.get("aes_key") or cdn.get("AesKey")
                             or ctx0.get("file_aes_key") or ctx0.get("FileAesKey") or ""),
            "file_no": (cdn.get("file_no") or cdn.get("FileNo")
                        or cdn.get("standard_file_no") or cdn.get("StandardFileNo")
                        or ctx0.get("file_no") or ctx0.get("FileNo")
                        or cdn.get("md5") or ""),
            "variant": cdn.get("variant") or ctx0.get("variant") or "standard",
        }
        # image 内部也可能直接带 data_len
        if cdn.get("data_len"):
            media["data_len"] = cdn["data_len"]
    # 直接字段
    for k in ("file_aes_key", "FileAesKey"):
        if src.get(k):
            media.setdefault("cdn", {})["file_aes_key"] = src[k]
    for k in ("file_no", "FileNo"):
        if src.get(k):
            media.setdefault("cdn", {})["file_no"] = src[k]
    # 文件
    for k in ("attach_id", "AttachId"):
        if src.get(k):
            media["attach_id"] = src[k]
    for k in ("data_len", "DataLen"):
        if src.get(k):
            media["data_len"] = src[k]
    for k in ("filename", "FileName"):
        if src.get(k):
            media["filename"] = src[k]
    # 图片通用
    for k in ("msg_id", "MsgId"):
        if src.get(k) and not media.get("msg_id"):
            media["msg_id"] = src[k]
    if src.get("to_wxid") or src.get("ToWxid"):
        media["to_wxid"] = src.get("to_wxid") or src.get("ToWxid")
    # 语音 (vendor V1 schema: voice.download_context.{msg_id, new_msg_id, client_msg_id, master_buf_id, format, length})
    # openclaw v1.2.6 VOICE-DOWNLOAD-BINARY
    voice = src.get("voice")
    if isinstance(voice, dict):
        ctx = voice.get("download_context") or {}
        media["voice_ctx"] = {
            "msg_id": ctx.get("msg_id") or 0,
            "new_msg_id": ctx.get("new_msg_id") or "",
            "client_msg_id": ctx.get("client_msg_id") or "",
            "master_buf_id": ctx.get("master_buf_id") or "0",
            "format": ctx.get("format") if isinstance(ctx.get("format"), int) else 4,
            "length": ctx.get("length") or voice.get("data_len") or 0,
            "chat_room_name": ctx.get("chat_room_name") or "",
            "from_user_name": ctx.get("from_user_name") or "",
            "to_user_name": ctx.get("to_user_name") or "",
        }
        # vendor 自带 transcript (v1.3.22 VENDOR-TRANSCRIPT) — 直接给 agent 不用 STT
        if voice.get("transcript"):
            media["vendor_transcript"] = voice["transcript"]
        # 兼容老 schema aes_key/file_no
        if voice.get("aes_key") or voice.get("file_no") or voice.get("cdn_voice_file_no"):
            media["legacy_cdn"] = {
                "aes_key": voice.get("aes_key") or voice.get("cdn_voice_file_no", ""),
                "file_no": voice.get("file_no") or "",
            }
    # 文件 (vendor V1 schema: file.download_context.{attach_id, user_name, data_len, section})
    # openclaw v1.2.5 FILE-DOWNLOAD-BINARY (返回原始字节, 非 base64)
    file_obj = src.get("file")
    if isinstance(file_obj, dict):
        ctx = file_obj.get("download_context") or {}
        section = ctx.get("section") or {}
        media["file_ctx"] = {
            "attach_id": ctx.get("attach_id") or "",
            "user_name": ctx.get("user_name") or "",
            "data_len": ctx.get("data_len") or file_obj.get("data_len") or 0,
            "start_pos": section.get("start_pos", 0),
            "section_len": section.get("data_len", 0),
            "app_id": ctx.get("app_id") or file_obj.get("app_id") or "",
            "file_name": ctx.get("file_name") or file_obj.get("file_name")
                        or file_obj.get("filename") or "",
        }
        # 兼容老字段
        if file_obj.get("attach_id") and not media.get("attach_id"):
            media["attach_id"] = file_obj["attach_id"]
        if file_obj.get("filename") and not media.get("filename"):
            media["filename"] = file_obj["filename"]
        if file_obj.get("data_len"):
            media["data_len"] = file_obj["data_len"]
    # 视频 (vendor V1 schema: video.download_context.{msg_id, to_wxid, data_len, section})
    # 老 schema aes_key/file_no 已废弃 (2026-09-01 修复, 参考 wpp-openclaw v1.3.8 VIDEO-DOWNLOAD)
    video = src.get("video")
    if isinstance(video, dict):
        ctx = video.get("download_context") or {}
        section = ctx.get("section") or {}
        media["video_ctx"] = {
            "to_wxid": ctx.get("to_wxid") or "",
            "msg_id": ctx.get("msg_id") or 0,
            "data_len": ctx.get("data_len") or video.get("data_len") or 0,
            "start_pos": section.get("start_pos", 0),
            "chunk_len": section.get("data_len", 1048576),
        }
        if video.get("duration_seconds"):
            media["duration"] = video["duration_seconds"]
        # 保留 aes_key/file_no 字段供老 schema / 兼容 (老 schema 直接调 CdnDownloadImage)
        if video.get("aes_key") or video.get("cdn_video_file_no"):
            media["legacy_cdn"] = {
                "aes_key": video.get("aes_key", ""),
                "file_no": video.get("cdn_video_file_no", ""),
            }
        thumb = video.get("thumbnail")
        if isinstance(thumb, dict) and (thumb.get("aes_key") or thumb.get("file_no")):
            media["thumb_cdn"] = {
                "file_aes_key": thumb.get("aes_key") or thumb.get("file_aes_key"),
                "file_no": thumb.get("file_no") or thumb.get("FileNo"),
                "width": thumb.get("width"),
                "height": thumb.get("height"),
            }
    return media
