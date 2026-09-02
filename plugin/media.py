"""WPP 媒体处理（迁移自 astrbot-plugin-wpp 下载逻辑 + Hermes feishu/line 适配器模式）。

入站媒体：下载到本地临时文件 → media_urls 供 Hermes vision 工具访问。
出站媒体：send_image / send_document / send_voice 调 vendor API。
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 200 * 1024 * 1024  # 200MB 上限


async def download_image_to_file(client, media: dict, authcode: str = "") -> tuple[str | None, str | None]:
    """下载图片到本地临时文件。返回 (本地路径, 媒体类型)。"""
    cdn = media.get("cdn") or {}
    try:
        img_bytes = None
        # 优先 CDN 下载 (openclaw v1.2.5 IMAGE-CDN-DOWNLOAD: 完整大图, file_aes_key + file_no)
        if cdn.get("file_aes_key") and cdn.get("file_no"):
            img_bytes = await client.download_image_cdn(
                cdn["file_aes_key"], cdn["file_no"],
                cdn.get("variant", "standard"), authcode=authcode,
            )
        # 降级: v1 schema DownloadImg (msg_id + to_wxid + data_len)
        if not img_bytes and media.get("msg_id"):
            img_bytes = await client.download_image(
                media.get("msg_id", ""),
                media.get("to_wxid", ""),
                media.get("data_len", 0),
                authcode=authcode,
            )
        if img_bytes and len(img_bytes) > 10:
            path = _write_temp(img_bytes, "img", ".jpg")
            return path, "image"
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] 图片下载失败: %s", e)
    return None, None


async def download_file_to_file(client, media: dict, authcode: str = "") -> tuple[str | None, str | None, str | None]:
    """下载文件到本地临时文件。返回 (本地路径, 媒体类型, 文件名)。"""
    # 优先 file_ctx (v1 schema: file.download_context)
    fctx = media.get("file_ctx") or {}
    attach_id = fctx.get("attach_id") or media.get("attach_id", "")
    if not attach_id:
        return None, None, None
    try:
        file_bytes = await client.download_file_binary(
            attach_id=attach_id,
            user_name=fctx.get("user_name") or media.get("user_name", "") or media.get("to_wxid", ""),
            data_len=fctx.get("data_len") or media.get("data_len", 0),
            start_pos=fctx.get("start_pos", 0),
            section_len=fctx.get("section_len") or fctx.get("data_len") or media.get("data_len", 0),
            app_id=fctx.get("app_id", ""),
            file_name=fctx.get("file_name") or media.get("filename", ""),
            authcode=authcode,
        )
        if file_bytes and len(file_bytes) > 10:
            fname = fctx.get("file_name") or media.get("filename", "file")
            base, ext = os.path.splitext(fname)
            path = _write_temp(file_bytes, "wpp_file", ext or ".bin")
            return path, "document", fname
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] 文件下载失败: %s", e)
    return None, None, None


def _write_temp(data: bytes, prefix: str, suffix: str) -> str:
    """写入临时文件，返回路径。"""
    fd, path = tempfile.mkstemp(prefix=f"wpp_{prefix}_", suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


async def download_voice_to_file(client, media: dict, authcode: str = "") -> tuple[str | None, str | None]:
    """下载语音（silk）到本地. swagger 2026-09-01 准确接口:
      - /Tools/DownloadVoice (JSON base64): fromUserName + msgId + length + bufid
      - /Tools/DownloadVoiceBinary (二进制原始字节): msg_id + new_msg_id + ... + file_name

    优先 v1 binary (openclaw v1.2.6) → 降级 v1 voice (Tools.DownloadVoiceParamDoc).
    """
    vctx = media.get("voice_ctx") or {}
    legacy = media.get("legacy_cdn") or {}
    try:
        silk_bytes = None
        # 优先 v1 schema DownloadVoiceBinary
        if vctx.get("msg_id"):
            silk_bytes = await client.download_voice_binary(
                msg_id=vctx["msg_id"],
                new_msg_id=vctx.get("new_msg_id", ""),
                client_msg_id=vctx.get("client_msg_id", ""),
                master_buf_id=vctx.get("master_buf_id", "0"),
                format=vctx.get("format", 4),
                length=vctx.get("length", 0),
                chat_room_name=vctx.get("chat_room_name", ""),
                from_user_name=vctx.get("from_user_name", ""),
                to_user_name=vctx.get("to_user_name", ""),
                file_name=vctx.get("file_name", ""),
                authcode=authcode,
            )
        # 降级老 schema (aes_key + file_no) - 调 download_voice_legacy
        if not silk_bytes and legacy.get("aes_key") and legacy.get("file_no"):
            silk_bytes = await client.download_voice_legacy(
                legacy["aes_key"], legacy["file_no"], authcode,
            )
        # 降级到 /Tools/DownloadVoice (Tools.DownloadVoiceParamDoc)
        if not silk_bytes and media.get("msg_id"):
            silk_bytes = await client.download_voice(
                msg_id=media["msg_id"],
                from_user_name=media.get("fromUserName") or media.get("from_user_name") or media.get("to_wxid", ""),
                length=int(media.get("length") or media.get("data_len") or 0),
                bufid=media.get("bufid") or media.get("master_buf_id", ""),
                authcode=authcode,
            )
        if silk_bytes and len(silk_bytes) > 10:
            path = _write_temp(silk_bytes, "voice", ".silk")
            return path, "voice"
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] 语音下载失败: %s", e)
    return None, None


async def transcribe_voice(client, media: dict) -> str:
    """下载语音 → STT 转写 → 文字。失败返回空串。"""
    path, _ = await download_voice_to_file(client, media)
    if not path:
        return ""
    try:
        import os
        with open(path, "rb") as f:
            silk_data = f.read()
        from .stt import transcribe_silk_buffer
        text = await transcribe_silk_buffer(silk_data)
        return text or ""
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] 语音转写失败: %s", e)
        return ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def download_video_to_file(client, media: dict, authcode: str = "") -> tuple[str | None, str | None, str | None]:
    """下载视频（分片循环, vendor V1 schema）到本地 + 缩略图. 返回 (video_path, thumb_path, media_type='video').

    media.video_ctx: {to_wxid, msg_id, data_len, start_pos, chunk_len}
    """
    vctx = media.get("video_ctx") or {}
    to_wxid = vctx.get("to_wxid", "")
    msg_id = vctx.get("msg_id", 0)
    total = int(vctx.get("data_len") or 0)
    if not (to_wxid and msg_id and total):
        log.warning("[WPP] 视频下载跳过: video_ctx 不完整 (to_wxid=%s msg_id=%s data_len=%s)", to_wxid, msg_id, total)
        return None, None, None
    pos = int(vctx.get("start_pos") or 0)
    chunk = int(vctx.get("chunk_len") or 1048576)
    chunks: list[bytes] = []
    try:
        while pos < total:
            remaining = total - pos
            this_chunk = min(chunk, remaining)
            buf = await client.download_video(
                to_wxid=to_wxid, msg_id=msg_id, data_len=total,
                start_pos=pos, chunk_len=this_chunk, authcode=authcode,
            )
            if not buf or len(buf) < 10:
                log.warning("[WPP] 视频分片下载失败 (pos=%s, len=%s)", pos, this_chunk)
                break
            chunks.append(buf)
            pos += len(buf)
            # 防卡死: 累计字节 > 200MB 终止
            if sum(len(c) for c in chunks) > 200 * 1024 * 1024:
                log.warning("[WPP] 视频下载超过 200MB 限制, 终止")
                break
        if not chunks:
            return None, None, None
        full = b"".join(chunks)
        vpath = _write_temp(full, "video", ".mp4")
        # 缩略图 (走 CdnDownloadImage + variant=thumbnail)
        thumb_cdn = media.get("thumb_cdn") or {}
        tpath = None
        if thumb_cdn.get("file_aes_key") and thumb_cdn.get("file_no"):
            thumb_bytes = await client.download_image_cdn(
                thumb_cdn["file_aes_key"], thumb_cdn["file_no"], "thumbnail"
            )
            if thumb_bytes and len(thumb_bytes) > 10:
                tpath = _write_temp(thumb_bytes, "video_thumb", ".jpg")
        return vpath, tpath, "video"
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] 视频下载失败: %s", e)
        return None, None, None
