"""WPP vendor HTTP API 客户端（迁移自 astrbot-plugin-wpp wpp_client.py）。

对 wx.juhe.chat vendor (28062) 发起业务 API 调用。
凭证 = authcode，同时注入 X-Access-Token header + ?authcode= query 双保险。
成功判定：Code==0 且 Success 非 false。
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 200 * 1024 * 1024  # 200MB 文件下载上限

# 已知语义特殊的端点（Code != 0 不代表调用失败，豁免检查）
_IGNORE_CODE_ENDPOINTS = frozenset(
    {
        "/Msg/Quote",  # 永久 ret=-2，走别的方式
    }
)

# 可重试的瞬时 HTTP 状态码（vendor 网关/上游故障）
_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})


class WppApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class WppClient:
    """WPP vendor API 客户端（aiohttp，带 retry + 错误语义）。"""

    def __init__(self, base_url: str, auth_token: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def _session_acquire(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def call(self, endpoint: str, body: dict, method: str = "POST", max_retries: int = 2) -> dict:
        """通用 vendor 调用（GET/POST）。工具 handler 的统一入口。

        endpoint 形如 /Group/GetChatroomInfo（无 /api 前缀，_url 自动加）。
        """
        if method.upper() == "GET":
            return await self._get(endpoint, max_retries=max_retries)
        return await self._post(endpoint, body, max_retries=max_retries)

    def _url(self, endpoint: str) -> str:
        """{base}/api{endpoint}?authcode=xxx"""
        ep = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        url = f"{self.base_url}/api{ep}"
        if self.auth_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}authcode={self.auth_token}"
        return url

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.auth_token:
            h["X-Access-Token"] = self.auth_token
        return h

    async def _post(self, endpoint: str, body: dict, max_retries: int = 2) -> dict:
        session = await self._session_acquire()
        url = self._url(endpoint)
        last_err: Exception | None = None
        for attempt in range(1, max_retries + 2):
            try:
                async with session.post(
                    url, json=body, headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        err = WppApiError(f"http_{resp.status}", f"{endpoint} -> HTTP {resp.status}: {text[:200]}")
                        # 5xx 瞬时故障可重试（区别于 4xx 永久错误）
                        if resp.status in _RETRYABLE_HTTP_STATUS:
                            last_err = err
                            if attempt <= max_retries:
                                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                                continue
                        raise err
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        raise WppApiError("bad_json", f"{endpoint} -> 非 JSON 响应: {text[:200]}")
                    code = data.get("Code")
                    if data.get("Success") is False or (code is not None and code != 0 and endpoint not in _IGNORE_CODE_ENDPOINTS):
                        raise WppApiError(code if code is not None else data.get("CodeValue", "?"), f"{endpoint} -> {text[:300]}")
                    return data
            except aiohttp.ClientError as e:
                last_err = e
                if attempt <= max_retries:
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                    continue
        raise WppApiError("network", f"{endpoint} -> {last_err}") from last_err

    async def _get(self, endpoint: str, max_retries: int = 1) -> dict:
        session = await self._session_acquire()
        url = self._url(endpoint)
        last_err: Exception | None = None
        for attempt in range(1, max_retries + 2):
            try:
                async with session.get(
                    url, headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        err = WppApiError(f"http_{resp.status}", f"{endpoint} -> HTTP {resp.status}: {text[:200]}")
                        # 5xx 瞬时故障可重试
                        if resp.status in _RETRYABLE_HTTP_STATUS:
                            last_err = err
                            if attempt <= max_retries:
                                await asyncio.sleep(0.3 * attempt)
                                continue
                        raise err
                    return json.loads(text)
            except (aiohttp.ClientError, json.JSONDecodeError) as e:
                last_err = e
                if attempt <= max_retries:
                    await asyncio.sleep(0.3 * attempt)
                    continue
        raise WppApiError("network", f"{endpoint} -> {last_err}") from last_err

    # ------------------------------------------------------------------ 状态
    async def get_online_info(self) -> dict:
        return await self._get("/User/GetOnlineInfo")

    async def get_long_link_status(self) -> dict:
        return await self._get("/Login/LongLinkStatus")

    async def get_contract_profile(self) -> dict:
        return await self._post("/User/GetContractProfile", {})

    async def heartbeat(self) -> dict:
        return await self._post("/Login/HeartBeat", {})

    # ------------------------------------------------------------------ 收发
    async def send_text(self, to_wxid: str, content: str, ats: list[str] | None = None) -> dict:
        # P0-5.1 修复：对齐 OpenClaw sendTxt — At 逗号串 + Type:1（vendor swagger 定义）
        body: dict = {"ToWxid": to_wxid, "Content": content, "Type": 1}
        if ats:
            body["At"] = ",".join(str(a) for a in ats)
        return await self._post("/Msg/SendTxt", body)

    async def revoke_msg(self, msg_id: str, new_msg_id: str = "", create_time: int = 0) -> dict:
        """撤回消息 (vendor /Msg/Revoke, 需要 msgId/newMsgId/createTime 三元组).

        v1.3.20: createTime 必须用发送返回的 (不能用 now, 否则 vendor 不真撤).
        edit_message 链路无法拿原 createTime, 接受 vendor 容错(失败继续).
        """
        import time as _t
        return await self._post("/Msg/Revoke", {
            "MsgId": msg_id,
            "NewMsgId": new_msg_id or msg_id,
            "CreateTime": create_time or int(_t.time()),
        })

    async def send_image(self, to_wxid: str, image_ref: str) -> dict:
        # UploadImg: {Base64, ToWxid}（上传即发送，ret=0 成功）
        return await self._post("/Msg/UploadImg", {"ToWxid": to_wxid, "Base64": image_ref})

    async def send_cdn_image(self, to_wxid: str, img_url: str) -> dict:
        """/Msg/SendCDNImg — 用 imgUrl 发送图片。"""
        return await self._post("/Msg/SendCDNImg", {"ToWxid": to_wxid, "Content": img_url})

    async def share_location(self, to_wxid: str, latitude: float, longitude: float, label: str = "", poi_name: str = "") -> dict:
        """/Msg/ShareLocation — 发送定位卡片 (Type 48)。

        v1.3.11 对齐 gewe: X=纬度(lat), Y=经度(lng) — 反了会定位卡片缩略图错位。
        """
        return await self._post("/Msg/ShareLocation", {
            "ToWxid": to_wxid,
            "X": float(latitude),
            "Y": float(longitude),
            "Label": label or "",
            "Poiname": poi_name or "",
            "Scale": 16,
            "Infourl": "",
        })

    async def send_cdn_video(self, to_wxid: str, video_url: str) -> dict:
        """/Msg/SendCDNVideo — 用 URL 发送视频。"""
        return await self._post("/Msg/SendCDNVideo", {"ToWxid": to_wxid, "Content": video_url})

    async def send_emoji(self, to_wxid: str, emoji_md5: str, emoji_size: int = 0) -> dict:
        """/Msg/SendEmoji — 发送 Emoji（用 md5）。"""
        return await self._post("/Msg/SendEmoji", {"toWxid": to_wxid, "emojiMd5": emoji_md5, "emojiSize": emoji_size})

    async def share_card(self, to_wxid: str, card_wxid: str, card_nickname: str, card_alias: str = "") -> dict:
        """/Msg/ShareCard — 分享名片。"""
        return await self._post("/Msg/ShareCard", {
            "ToWxid": to_wxid,
            "CardWxId": card_wxid,
            "CardNickName": card_nickname,
            "CardAlias": card_alias or "",
        })

    async def send_xcx(self, to_wxid: str, xcx_title: str, xcx_desc: str, xcx_url: str, xcx_app_id: str, thumb_url: str = "") -> dict:
        """/Msg/SendXCX — 发送小程序消息。"""
        return await self._post("/Msg/SendXCX", {
            "toWxid": to_wxid,
            "xcxTitle": xcx_title,
            "xcxDesc": xcx_desc,
            "xcxUrl": xcx_url,
            "xcxAppId": xcx_app_id,
            "thumbUrl": thumb_url or "",
        })

    async def share_link(self, to_wxid: str, title: str, desc: str, link_url: str, thumb_url: str = "") -> dict:
        """/Msg/ShareLink — 分享链接（构造 type=5 appmsg XML）。"""
        import re
        def esc(s: str) -> str:
            return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        xml = (
            '<appmsg appid="" sdkver="0"><title>{}</title>'
            '<des>{}</des><action>view</action><type>5</type>'
            '<url>{}</url><thumburl>{}</thumburl></appmsg>'
        ).format(esc(title), esc(desc), esc(link_url), esc(thumb_url))
        return await self._post("/Msg/ShareLink", {"ToWxid": to_wxid, "Type": 5, "Xml": xml})

    async def send_group_mass_text(self, to_ids: list[str], content: str) -> dict:
        """/Msg/SendGroupMassMsgText — 群发文本到多个群。"""
        return await self._post("/Msg/SendGroupMassMsgText", {"ToIds": to_ids, "Content": content})

    async def send_file_v2(self, to_wxid: str, file_name: str, base64: str) -> dict:
        """/Msg/SendFile — 发送文件（自动上传+发送）。"""
        return await self._post("/Msg/SendFile", {"ToWxid": to_wxid, "FileName": file_name, "Base64": base64})

    async def send_voice(self, to_wxid: str, base64: str, voice_type: int = 4) -> dict:
        """/Msg/SendVoice — 发送语音（base64 自动上传+发送）。
        voice_type: 0=AMR, 4=SILK（wechat 默认 silk, 上传后会被自动转码）."""
        return await self._post("/Msg/SendVoice", {"ToWxid": to_wxid, "Base64": base64, "Type": voice_type})

    async def send_video(self, to_wxid: str, base64: str, thumb_base64: str = "", play_length: int = 10) -> dict:
        """/Msg/SendVideo — 发送视频（base64 自动上传+发送）。
        thumb_base64: 视频封面缩略图 base64 (必需, 否则 vendor 拒收)。
        play_length: 视频时长（秒）。"""
        return await self._post("/Msg/SendVideo", {
            "ToWxid": to_wxid,
            "Base64": base64,
            "ImageBase64": thumb_base64,
            "PlayLength": play_length,
        })

    async def send_app_message(self, items: list[dict]) -> dict:
        """/Msg/SendAppMessage — 发送结构化应用卡片（单次最多 20 项）。"""
        return await self._post("/Msg/SendAppMessage", {"items": items})

    # ------------------------------------------------------------------ 朋友圈发布（复合：上传→发布）
    async def publish_circle_images(self, title: str, image_base64_list: list[str]) -> dict:
        """发布图片朋友圈（1-9 张）。逐张 UploadImage → /FriendCircle/Messages images 数组。"""
        if not image_base64_list or len(image_base64_list) > 9:
            return {"Code": -1, "Success": False, "CodeValue": f"图片数量必须 1-9 张（实际 {len(image_base64_list)}）"}
        items = []
        for b64 in image_base64_list:
            up = await self._post("/FriendCircle/UploadImage", {"imageData": b64})
            item = (up.get("Data") or {}).get("publishItem")
            if up.get("Code") != 0 or not item:
                return {"Code": up.get("Code", -1), "Success": False, "CodeValue": f"UploadImage 失败 {up.get('CodeValue', '')}"}
            items.append(item)
        return await self._post("/FriendCircle/Messages", {"title": title, "private": 0, "images": items})

    async def publish_circle_video(self, title: str, video_base64: str, thumb_base64: str) -> dict:
        """发布视频朋友圈。UploadVideo → publishItem → /FriendCircle/Messages video 对象。"""
        up = await self._post("/FriendCircle/UploadVideo", {"videoData": video_base64, "thumbData": thumb_base64})
        item = (up.get("Data") or {}).get("publishItem")
        if up.get("Code") != 0 or not item:
            return {"Code": up.get("Code", -1), "Success": False, "CodeValue": f"UploadVideo 失败 {up.get('CodeValue', '')}"}
        return await self._post("/FriendCircle/Messages", {"title": title, "private": 0, "video": item})

    async def publish_circle_text(self, title: str) -> dict:
        """发布文字朋友圈。"""
        return await self._post("/FriendCircle/Messages", {"title": title, "private": 0})

    async def sync_message(self) -> dict:
        """/Msg/Sync — 拉取增量消息（服务端用 authcode 定位账号 + synckey）。"""
        return await self._post("/Msg/Sync", {})

    # ------------------------------------------------------------------ 媒体下载
    async def download_image_cdn(self, file_aes_key: str, file_no: str, variant: str = "",
                                authcode: str = "") -> bytes | None:
        """POST /api/Tools/CdnDownloadImage — CDN 图片完整大图 (swagger 2026-09-01 准确字段).

        vendor Tools.CdnDownloadImageParamDoc:
          required: [file_aes_key, file_no]
          注: vendor 只有这 2 个字段, variant 是我兼容老 schema 加的 (vendor 会忽略)
        resp: Data.Image (base64 JPEG) 或 Data.Data.buffer
        """
        session = await self._session_acquire()
        base = self.base_url.rstrip("/")
        url = f"{base}/api/Tools/CdnDownloadImage?authcode={authcode or ''}"
        headers = self._headers()
        headers["TokenKey"] = self.auth_token
        headers["Content-Type"] = "application/json"
        try:
            async with session.post(
                url, json={"file_aes_key": file_aes_key, "file_no": file_no, **({"variant": variant} if variant else {})},
                headers=headers, timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    log.warning("[WPP] download_image_cdn HTTP %s", resp.status)
                    return None
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return None
                payload = data.get("Data") or {}
                if not isinstance(payload, dict):
                    return None
                b64 = ""
                if isinstance(payload.get("data"), dict):
                    b64 = payload["data"].get("buffer") or payload["data"].get("Buffer") or ""
                if not b64:
                    b64 = (payload.get("Image") or payload.get("image")
                           or payload.get("buffer") or payload.get("Buffer")
                           or payload.get("base64") or "")
                if b64:
                    import base64
                    try:
                        return base64.b64decode(b64)
                    except Exception:  # noqa: BLE001
                        return None
                return None
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] download_image_cdn failed: %s", e)
            return None

    async def download_voice_legacy(self, aes_key: str, file_no: str, authcode: str = "") -> bytes | None:
        """老 schema 语音下载 /Tools/DownloadVoice — body {aesKey, fileId}."""
        session = await self._session_acquire()
        base = self.base_url.rstrip("/")
        url = f"{base}/api/Tools/DownloadVoice?authcode={authcode or ''}"
        headers = self._headers()
        headers["TokenKey"] = self.auth_token
        headers["Content-Type"] = "application/json"
        try:
            async with session.post(
                url, json={"aesKey": aes_key, "fileId": file_no},
                headers=headers, timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    return None
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return None
                payload = data.get("Data") or {}
                if not isinstance(payload, dict):
                    return None
                b64 = (payload.get("Voice") or payload.get("voice")
                       or payload.get("File") or payload.get("file")
                       or payload.get("Buffer") or payload.get("buffer") or "")
                if b64:
                    import base64
                    try:
                        return base64.b64decode(b64)
                    except Exception:  # noqa: BLE001
                        return None
                return None
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] download_voice_legacy failed: %s", e)
            return None

    async def download_voice(self, msg_id: int, from_user_name: str, length: int,
                            bufid: str = "", authcode: str = "") -> bytes | None:
        """POST /api/Tools/DownloadVoice — 语音 JSON 接口 (base64) — swagger 2026-09-01 准确字段.

        vendor Tools.DownloadVoiceParamDoc:
          required: [fromUserName, msgId, length]
          body: {bufid, fromUserName, length, msgId}
        resp: Data.Voice (base64) 或 Data.Buffer/buffer/base64
        """
        session = await self._session_acquire()
        base = self.base_url.rstrip("/")
        url = f"{base}/api/Tools/DownloadVoice?authcode={authcode or ''}"
        headers = self._headers()
        headers["TokenKey"] = self.auth_token
        headers["Content-Type"] = "application/json"
        body = {
            "msgId": msg_id,
            "fromUserName": from_user_name,
            "length": length,
        }
        if bufid:
            body["bufid"] = bufid
        try:
            async with session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    log.warning("[WPP] download_voice HTTP %s", resp.status)
                    return None
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return None
                payload = data.get("Data") or {}
                if not isinstance(payload, dict):
                    return None
                # vendor 返回: Voice/File/Buffer/base64/data.buffer 等
                b64 = (payload.get("Voice") or payload.get("voice")
                       or payload.get("File") or payload.get("file")
                       or payload.get("Buffer") or payload.get("buffer")
                       or payload.get("base64") or "")
                if isinstance(payload.get("data"), dict):
                    b64 = b64 or payload["data"].get("buffer") or payload["data"].get("Buffer") or ""
                if b64:
                    import base64
                    try:
                        return base64.b64decode(b64)
                    except Exception:  # noqa: BLE001
                        return None
                return None
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] download_voice failed: %s", e)
            return None

    async def download_voice_binary(self, msg_id: int, new_msg_id: str = "", client_msg_id: str = "",
                                 master_buf_id: str = "0", format: int = 4, length: int = 0,
                                 chat_room_name: str = "", from_user_name: str = "",
                                 to_user_name: str = "", file_name: str = "voice.silk",
                                 authcode: str = "") -> bytes | None:
        """POST /api/Tools/DownloadVoiceBinary — 语音原始字节 (swagger 2026-09-01 准确字段).

        vendor Tools.BinaryVoiceDownloadParamDoc:
          body: {msg_id, new_msg_id, client_msg_id, master_buf_id, format, length,
                 chat_room_name, from_user_name, to_user_name, file_name}
          required: (none - 但实际 msg_id 必填)
        resp: SILK/AMR/MP3/WAV 原始字节 (非 JSON), 可能 Content-Type: audio/silk
        """
        session = await self._session_acquire()
        base = self.base_url.rstrip("/")
        url = f"{base}/api/Tools/DownloadVoiceBinary?authcode={authcode or ''}"
        headers = self._headers()
        headers["TokenKey"] = self.auth_token
        headers["Content-Type"] = "application/json"
        body = {
            "msg_id": msg_id,
            "new_msg_id": new_msg_id,
            "client_msg_id": client_msg_id,
            "master_buf_id": master_buf_id,
            "format": format,
            "length": length,
            "chat_room_name": chat_room_name,
            "from_user_name": from_user_name,
            "to_user_name": to_user_name,
            "file_name": file_name or "voice.silk",
        }
        try:
            async with session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    log.warning("[WPP] download_voice_binary HTTP %s", resp.status)
                    return None
                content_type = resp.headers.get("Content-Type", "").lower()
                # 如果返回 JSON, 解析 base64; 否则直接读字节
                if "json" in content_type:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        return None
                    payload = data.get("Data") or {}
                    if not isinstance(payload, dict):
                        return None
                    b64 = (payload.get("voice") or payload.get("Voice")
                           or payload.get("buffer") or payload.get("Buffer")
                           or payload.get("file") or payload.get("File")
                           or payload.get("base64") or "")
                    if isinstance(payload.get("data"), dict):
                        b64 = b64 or payload["data"].get("buffer") or payload["data"].get("Buffer") or ""
                    if b64:
                        import base64
                        try:
                            return base64.b64decode(b64)
                        except Exception:  # noqa: BLE001
                            return None
                    return None
                # 二进制直接读
                buf = await resp.read()
                return buf if len(buf) > 10 else None
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] download_voice_binary failed: %s", e)
            return None

    async def download_file_binary(self, attach_id: str, user_name: str = "",
                                   data_len: int = 0, start_pos: int = 0,
                                   section_len: int = 0, app_id: str = "",
                                   file_name: str = "", authcode: str = "") -> bytes | None:
        """POST /api/Tools/DownloadFileBinary — 文件原始字节流 (swagger 2026-09-01 准确字段).

        vendor Tools.BinaryFileDownloadParamDoc:
          required: [attach_id, user_name, data_len]
          body: {attach_id, user_name, data_len, section, app_id, file_name}
          resp: 原始文件字节 (非 JSON), Content-Disposition 附件
        兼容: 如果返回 JSON, 尝试 Data.File/buffer/base64
        """
        if data_len > MAX_FILE_BYTES:
            log.warning("[WPP] download_file_binary 文件过大 %d > %d，拒绝下载", data_len, MAX_FILE_BYTES)
            return None
        session = await self._session_acquire()
        base = self.base_url.rstrip("/")
        url = f"{base}/api/Tools/DownloadFileBinary?authcode={authcode or ''}"
        headers = self._headers()
        headers["TokenKey"] = self.auth_token
        headers["Content-Type"] = "application/json"
        body = {
            "attach_id": attach_id,
            "user_name": user_name,
            "data_len": data_len,
            "section": {"start_pos": start_pos, "data_len": section_len or data_len},
        }
        if app_id:
            body["app_id"] = app_id
        if file_name:
            body["file_name"] = file_name
        try:
            async with session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                if resp.status != 200:
                    log.warning("[WPP] download_file_binary HTTP %s", resp.status)
                    return None
                content_type = resp.headers.get("Content-Type", "").lower()
                # Content-Length 防护（防 vendor 返回超大数据导致 OOM）
                cl = resp.headers.get("Content-Length")
                if cl:
                    try:
                        if int(cl) > MAX_FILE_BYTES:
                            log.warning("[WPP] download_file_binary Content-Length %s 超上限，拒绝", cl)
                            return None
                    except ValueError:
                        pass
                buf = await resp.read()
                if buf and len(buf) > 10:
                    if "json" in content_type:
                        try:
                            data = json.loads(buf.decode("utf-8", errors="ignore"))
                            payload = data.get("Data") or {}
                            if isinstance(payload, dict):
                                b64 = (payload.get("file") or payload.get("File")
                                       or payload.get("buffer") or payload.get("Buffer")
                                       or payload.get("base64") or "")
                                if isinstance(payload.get("data"), dict):
                                    b64 = b64 or payload["data"].get("buffer") or ""
                                if b64:
                                    import base64
                                    try:
                                        return base64.b64decode(b64)
                                    except Exception:  # noqa: BLE001
                                        pass
                        except Exception:  # noqa: BLE001
                            pass
                    return buf
                return None
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] download_file_binary failed: %s", e)
            return None

    async def download_image(self, msg_id: str, to_wxid: str, data_len: int = 0,
                            authcode: str = "") -> bytes | None:
        """POST /api/Tools/DownloadImg — 下载图片 (openclaw v1.3.70 v1 schema 真实字段).

        字段名: msg_id/to_wxid/data_len/section (snake_case, 旧 MsgId/ToWxid 报 INVALID_ARGUMENT)
        返回: Data.data.buffer (base64 JPEG, vendor 64KB cap)
        """
        session = await self._session_acquire()
        base = self.base_url.rstrip("/")
        url = f"{base}/api/Tools/DownloadImg?authcode={authcode or ''}"
        headers = self._headers()
        headers["TokenKey"] = self.auth_token
        headers["Content-Type"] = "application/json"
        body = {
            "msg_id": msg_id,
            "to_wxid": to_wxid,
            "data_len": data_len or 0,
            "compress_type": 0,
        }
        if data_len:
            body["section"] = {"start_pos": 0, "data_len": data_len}
        try:
            async with session.post(
                url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    log.warning("[WPP] download_image HTTP %s", resp.status)
                    return None
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return None
                payload = data.get("Data") or {}
                if not isinstance(payload, dict):
                    return None
                # 兼容 Data.data.buffer / Data.Image / Data.buffer
                b64 = ""
                if isinstance(payload.get("data"), dict):
                    b64 = payload["data"].get("buffer") or payload["data"].get("Buffer") or ""
                if not b64:
                    b64 = (payload.get("Image") or payload.get("image")
                           or payload.get("buffer") or payload.get("Buffer")
                           or payload.get("base64") or "")
                if b64:
                    import base64
                    try:
                        return base64.b64decode(b64)
                    except Exception:  # noqa: BLE001
                        return None
                return None
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] download_image failed: %s", e)
            return None

    async def download_video(self, to_wxid: str, msg_id: int, data_len: int,
                            start_pos: int = 0, chunk_len: int = 1048576,
                            authcode: str = "", compress_type: int = 0) -> bytes | None:
        """POST /api/Tools/DownloadVideo — 视频分片下载（vendor V1 schema, 2026-09-01 修复）.

        openclaw 真实实现 (wpp-openclaw v1.3.8 VIDEO-DOWNLOAD):
        body: {to_wxid, msg_id, data_len, section:{start_pos,data_len}, compress_type}
        resp: {Data: {totalLen?, data?: {buffer: b64}, Video?: b64}}

        完整视频需循环分片 (start_pos += chunk.length) 直到 start_pos >= totalLen。
        """
        session = await self._session_acquire()
        # vendor URL 直接拼 authcode query (不走 _url, 因为 _url 会加 /api)
        base = self.base_url.rstrip("/")
        url = f"{base}/api/Tools/DownloadVideo?authcode={authcode or ''}"
        headers = self._headers()
        headers["TokenKey"] = self.auth_token
        headers["Content-Type"] = "application/json"
        body = {
            "to_wxid": to_wxid,
            "msg_id": msg_id,
            "data_len": data_len,
            "section": {"start_pos": start_pos, "data_len": chunk_len},
            "compress_type": compress_type,
        }
        try:
            async with session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    log.warning("[WPP] download_video HTTP %s body=%s", resp.status, text[:200])
                    return None
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("[WPP] download_video 非JSON响应: %s", text[:200])
                    return None
                # 响应结构: Data: {totalLen?, data?: {buffer: b64}, Video?: b64}
                payload = data.get("Data") or {}
                b64 = ""
                if isinstance(payload, dict):
                    if isinstance(payload.get("data"), dict):
                        b64 = payload["data"].get("buffer") or ""
                    if not b64:
                        b64 = payload.get("Video") or payload.get("buffer") or ""
                if b64:
                    import base64
                    try:
                        return base64.b64decode(b64)
                    except Exception:  # noqa: BLE001
                        return None
                log.warning("[WPP] download_video 无 buffer, Data keys=%s", list(payload.keys()) if isinstance(payload, dict) else type(payload))
                return None
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] download_video failed: %s", e)
            return None

    async def download_video_thumb(self, file_aes_key: str, file_no: str) -> bytes | None:
        """下载视频缩略图 (与 video 同 aes_key/file_no, 用 Image API 解析)."""
        session = await self._session_acquire()
        url = self._url("/Tools/CdnDownloadImage")
        headers = self._headers()
        headers["TokenKey"] = self.auth_token
        body = {
            "FileAESKey": file_aes_key,
            "FileNo": file_no,
            "Section": {"StartPos": 0, "DataLen": 100000},
        }
        try:
            async with session.post(
                url, json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return None
                inner = data.get("Data") or {}
                if isinstance(inner, dict):
                    inner2 = inner.get("Data") or inner
                    buf = (inner2.get("buffer") or inner2.get("Buffer") or inner2.get("Image")
                           or inner2.get("base64") or inner.get("buffer") or inner.get("Data"))
                    if buf:
                        import base64
                        try:
                            return base64.b64decode(buf)
                        except Exception:  # noqa: BLE001
                            return None
                return None
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP] download_video_thumb failed: %s", e)
            return None
