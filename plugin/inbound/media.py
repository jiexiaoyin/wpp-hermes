"""vendor 媒体元数据提取 —— 旧 ``message_parser.py:extract_media_meta`` 的迁址。

## 为什么这个文件全是"考古"

vendor 的媒体字段**三代混用**，同一份 payload 里可能同时出现：

- **v1 schema**（现行）：``image.cdn_download_contexts[0].file_aes_key``、
  ``voice.download_context.{msg_id,new_msg_id,master_buf_id,...}``、
  ``file.download_context.{attach_id,section}``、``video.download_context.{msg_id,section}``
- **老 schema**（已废弃但仍兜底）：``aes_key`` / ``file_no`` / ``cdn_video_file_no``
- **裸字段**：``file_aes_key`` / ``attach_id`` / ``data_len`` / ``filename``

这些字段名**不是猜的**，是 2026-08/09 逐次对真实推送抓包对齐出来的（见 TS
``media-enrich.ts`` 的历次修复：v1.2.5 FILE-DOWNLOAD-BINARY / v1.2.6
VOICE-DOWNLOAD-BINARY / v1.3.8 VIDEO-DOWNLOAD / v1.3.22 VENDOR-TRANSCRIPT）。
**删任何一个兜底分支之前先确认对应老账号已迁移**，否则会静默拿不到媒体。

## 产出形状

统一挂在 :attr:`WppMessage.media` 上，键名与下游 ``api_client`` 的下载方法一一对应::

    {"cdn": {file_aes_key, file_no, variant},   # 图片
     "voice_ctx": {...}, "file_ctx": {...}, "video_ctx": {...},
     "thumb_cdn": {...}, "legacy_cdn": {...},
     "attach_id", "data_len", "filename", "msg_id", "to_wxid",
     "vendor_transcript", "duration"}
"""

from __future__ import annotations

import json
import re
from typing import Any


def _unwrap_scalar(v: Any) -> str:
    """解 AddMsgs 的 ``{"string": …}`` 包装，标量原样转字符串。"""
    while isinstance(v, dict) and "string" in v:
        v = v["string"]
    return v if isinstance(v, str) else ""


def _as_dict(v: Any) -> dict | None:
    """解 ``{"string": "{...json...}"}`` 包装（AddMsgs 的复杂字段常这么裹）。"""
    while isinstance(v, dict) and "string" in v:
        v = v["string"]
    if isinstance(v, str):
        s = v.strip()
        if s[:1] != "{":
            return None
        try:
            v = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return None
    return v if isinstance(v, dict) else None


def _first(d: dict, *keys: str) -> Any:
    """取第一个非空值（对齐旧实现的 ``a or b or c`` 链，但可读）。"""
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return ""


def extract_media_meta(src: dict) -> dict:
    """提取图片/文件/语音/视频元数据（供 :class:`core.api_client.WppClient` 下载）。"""
    media: dict = {}
    _extract_image(src, media)
    _extract_image_v0_xml(src, media)
    _extract_flat_fields(src, media)
    _extract_voice(src, media)
    _extract_file(src, media)
    _extract_video(src, media)
    return media


# ---------------------------------------------------------------------- 图片


def _extract_image(src: dict, media: dict) -> None:
    cdn = src.get("cdn") or src.get("image") or src.get("img")
    if not isinstance(cdn, dict):
        return
    # vendor 真实字段: aes_key / standard_file_no / cdn_download_contexts[0].file_aes_key
    dl = cdn.get("cdn_download_contexts")
    ctx0 = dl[0] if isinstance(dl, list) and dl and isinstance(dl[0], dict) else {}
    media["cdn"] = {
        "file_aes_key": _first(cdn, "file_aes_key", "FileAesKey", "aes_key", "AesKey")
                        or _first(ctx0, "file_aes_key", "FileAesKey"),
        "file_no": (_first(cdn, "file_no", "FileNo", "standard_file_no", "StandardFileNo", "md5")
                    or _first(ctx0, "file_no", "FileNo")),
        "variant": cdn.get("variant") or ctx0.get("variant") or "standard",
    }
    if cdn.get("data_len"):
        media["data_len"] = cdn["data_len"]


def _extract_image_v0_xml(src: dict, media: dict) -> None:
    """v0 ``<img ...>`` XML 兜底（对齐 TS ``parseImageXml``）。

    新 vendor 的图片优先走结构化字段；这里只在结构化字段**完全没有**时补，
    老账号/老推送仍可能只给 XML（``cdndownload`` 链路对 v0 图片仍可用，
    与 file/video/voice 不同 —— 那三类 OpenClaw 也明确标注"新 vendor 不支持"）。
    """
    if (media.get("cdn") or {}).get("file_aes_key"):
        return
    content = src.get("content")
    if not isinstance(content, str) or "cdnbigimgurl" not in content and "cdnthumburl" not in content:
        return
    aes = re.search(r'aeskey="([^"]+)"', content)
    file_no = (re.search(r'cdnbigimgurl="([^"]+)"', content)
               or re.search(r'cdnmidimgurl="([^"]+)"', content)
               or re.search(r'cdnthumburl="([^"]+)"', content))
    if not aes or not file_no:
        return
    md5 = re.search(r'md5="([^"]+)"', content)
    media["cdn"] = {
        "file_aes_key": aes.group(1),
        "file_no": file_no.group(1),
        "variant": "standard",
    }
    if md5:
        media["md5"] = md5.group(1)


# ---------------------------------------------------------------- 裸字段兜底


def _extract_flat_fields(src: dict, media: dict) -> None:
    """顶层裸字段。**只 setdefault** —— 结构化路径拿到的值优先。"""
    for key, target in (("file_aes_key", "file_aes_key"), ("FileAesKey", "file_aes_key"),
                        ("file_no", "file_no"), ("FileNo", "file_no")):
        if src.get(key):
            media.setdefault("cdn", {})[target] = src[key]

    for key, target in (("attach_id", "attach_id"), ("AttachId", "attach_id"),
                        ("data_len", "data_len"), ("DataLen", "data_len"),
                        ("filename", "filename"), ("FileName", "filename")):
        if src.get(key):
            media[target] = src[key]

    for key in ("msg_id", "MsgId"):
        if src.get(key) and not media.get("msg_id"):
            media["msg_id"] = src[key]

    to_wxid = src.get("to_wxid") or src.get("ToWxid")
    if to_wxid:
        media["to_wxid"] = to_wxid


# ---------------------------------------------------------------------- 语音


def _extract_voice(src: dict, media: dict) -> None:
    """voice 形态（v1.2.6 VOICE-DOWNLOAD-BINARY）。

    ⚠️ 2026-10-19：AddMsgs 业务回调把 ``voice`` / ``download_context`` /
    ``transcript`` 都裹成 ``{"string": …}``（复杂对象是 JSON 字符串）。
    旧实现直接 ``voice.get("transcript")`` ⇒ 拿到的是 ``{"string": …}``
    （或整条 voice 认不出）⇒ 正文里要么没有转写，要么出现 ``{'string': …}``
    这种不是话的"话"。与引用解析同一类包装，统一解包。
    """
    voice = _as_dict(src.get("voice"))
    if not isinstance(voice, dict):
        return
    ctx = _as_dict(voice.get("download_context")) or {}
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
    # vendor 自带 transcript（v1.3.22 VENDOR-TRANSCRIPT）—— 有就直接给 agent，免 STT。
    # 必须是**字符串**：包在 ``{"string": …}`` 里时先解包（AddMsgs），解不出来就当没有
    # —— 把 dict 交给正文会让模型看到 ``{'string': '…'}``。
    transcript = _unwrap_scalar(voice.get("transcript"))
    if transcript:
        media["vendor_transcript"] = transcript
    # 老 schema 兜底
    if voice.get("aes_key") or voice.get("file_no") or voice.get("cdn_voice_file_no"):
        media["legacy_cdn"] = {
            "aes_key": voice.get("aes_key") or voice.get("cdn_voice_file_no", ""),
            "file_no": voice.get("file_no") or "",
        }


# ---------------------------------------------------------------------- 文件


def _extract_file(src: dict, media: dict) -> None:
    """file 形态（v1.2.5 FILE-DOWNLOAD-BINARY，返回原始字节而非 base64）。"""
    file_obj = _as_dict(src.get("file"))
    if not isinstance(file_obj, dict):
        return
    ctx = file_obj.get("download_context") or {}
    section = ctx.get("section") or {}
    media["file_ctx"] = {
        "attach_id": ctx.get("attach_id") or "",
        "user_name": ctx.get("user_name") or "",
        "data_len": ctx.get("data_len") or file_obj.get("data_len") or 0,
        "start_pos": section.get("start_pos", 0),
        "section_len": section.get("data_len", 0),
        "app_id": ctx.get("app_id") or file_obj.get("app_id") or "",
        "file_name": (ctx.get("file_name") or file_obj.get("file_name")
                      or file_obj.get("filename") or ""),
    }
    # 老字段兜底
    if file_obj.get("attach_id") and not media.get("attach_id"):
        media["attach_id"] = file_obj["attach_id"]
    if file_obj.get("filename") and not media.get("filename"):
        media["filename"] = file_obj["filename"]
    if file_obj.get("data_len"):
        media["data_len"] = file_obj["data_len"]


# ---------------------------------------------------------------------- 视频


def _extract_video(src: dict, media: dict) -> None:
    """video 形态（v1.3.8 VIDEO-DOWNLOAD）。

    老 schema 的 ``aes_key`` / ``file_no`` **已废弃**（2026-09-01 修复），
    但仍在下面留兜底分支供老账号用。
    """
    video = src.get("video")
    if not isinstance(video, dict):
        return
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


# ----------------------------------------------------------------- 读回（转写）


def is_file_message(src: dict) -> bool:
    """是不是**文件**消息（v1 ``kind=app`` + ``app.category=file``，或 v0 appmsg type 6/8）。

    对齐 TS ``isV1SchemaFile`` + ``handler.ts:382`` 的 v0 判据。单独成函数是因为
    "没有 download_context 的文件也要走兜底话术"，而 :func:`extract_media_meta`
    在那种消息上产出的是空 dict —— 不能拿 ``not media`` 当"不是文件"。

    ⚠️ **被引用的文件不算**（2026-10-19 修）：引用一条文件消息时，vendor 会把原文件
    的 appmsg XML 嵌进 ``<refermsg>`` 里。旧实现直接扫整段 ``content``，于是**纯文本
    的引用回复**被判成文件消息 ⇒ 适配器走"文件无法下载"兜底话术，模型答非所问。
    现在先剥掉 ``<refermsg>`` 再看**外层** appmsg 的 type。
    """
    if not isinstance(src, dict):
        return False
    if _as_dict(src.get("file")) is not None:
        return True
    app = _as_dict(src.get("app"))
    if isinstance(app, dict) and str(app.get("category") or "") == "file":
        return True
    content = _unwrap_scalar(src.get("content") or src.get("Content"))
    # 只看外层：被引用消息（refermsg）里的 appmsg 是**原消息**的文件，不是这条
    outer = re.sub(r"<refermsg\b[\s\S]*?</refermsg>", "", content)
    return ("<appmsg" in outer
            and ("<type>6</type>" in outer or "<type>8</type>" in outer))


def file_name_of(src: dict, media: dict | None = None) -> str:
    """从 v1 ``app.title`` / ``file_ctx.file_name`` / v0 XML ``<filename>`` 取文件名。"""
    media = media or {}
    ctx = media.get("file_ctx") or {}
    app = _as_dict(src.get("app")) or {}
    for cand in (ctx.get("file_name"), media.get("filename"),
                 app.get("title"), _unwrap_scalar(src.get("filename")),
                 _unwrap_scalar(src.get("FileName"))):
        if cand:
            return str(cand)
    m = re.search(r'<filename(?:\s+name="[^"]*")?>([^<]+)</filename>',
                  _unwrap_scalar(src.get("content") or src.get("Content")))
    return m.group(1).strip() if m else ""


def voice_transcript(media: dict) -> str:
    """语音的转写文字；没有就 ``""``。

    两个来源，**vendor 优先**（对齐 TS ``voice.ts:100`` VENDOR-TRANSCRIPT）：

    - ``vendor_transcript`` —— 微信官方随推送给的（``voice.transcript``），免费且更准；
    - ``stt_text`` —— 插件自己转的（:mod:`plugin.inbound.stt`），只在上面那个缺席时才有。

    **不看 ``msg_type``**：这两个键只有 :func:`_extract_voice` / STT 链路会写，
    再按类型判一次是死码（真出现别的类型带语音、也该把转写给模型看）。
    """
    media = media or {}
    return str(media.get("vendor_transcript") or media.get("stt_text") or "").strip()


__all__ = ["extract_media_meta", "file_name_of", "is_file_message", "voice_transcript"]
