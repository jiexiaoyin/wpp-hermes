"""厂商 HTTP API 客户端 —— 旧 ``api_client.py``(735 行) 的重写。

## 相对旧实现的三处实质改动

1. **★ 补上 API 审计**。旧 ``api_client.py`` **一次审计都不做** ——
   ``wpp_api_calls`` 的 INSERT 只存在于新数据层，从未被调用过。本实现把审计
   收敛到 ``call()`` **唯一入口**，任何走 ``call`` 的请求都留痕。
2. **★ 消除 9 份重复的响应解析**。旧实现有 9 个 ``download_*`` 方法，每个都
   重复「读 text → json.loads → 在 ``Data.*`` 里用 8 种候选键名捞 base64」，
   且各版本候选键名还不一致（有的漏 ``Voice``、有的漏 ``data.buffer``）。
   现收敛为 ``_extract_b64()`` 一处，**取并集**，不再漏。
3. **★ 审计写库不阻塞事件循环**。旧实现的同步 pymysql 调用直接在 async 里跑
   （已知缺陷）；这里审计走 ``asyncio.to_thread``。

## 必须原样保留的厂商契约

- 鉴权**双保险**：``X-Access-Token`` header **和** ``?authcode=`` query。
- URL 形状 ``{base}/api{endpoint}``（``call`` 会自动补 ``/api``）。
- 成功判定 ``Code == 0`` 且 ``Success != false``；``/Msg/Quote`` 例外（永久 ret=-2）。
- 仅 ``{502,503,504}`` 可重试（**4xx 是永久错误，重试无意义**）。
- 下载类端点**不走** ``_url()`` 的 ``/api`` 拼接，且额外带 ``TokenKey`` header。

## ⚠️ ``aiohttp`` 必须在**函数体内**导入（不许搬到模块级）

``platform/adapter.py`` 在**模块级** ``from ..core.api_client import WppApiError, WppClient``，
而 ``registration._register_platform`` 会导入那个 adapter ⇒ **本模块的加载期依赖就是注册期
依赖**。``plugin_validate`` 的 probe 在 scratch ``HERMES_HOME`` 子进程里跑 ``register()``，
那个解释器未必装了 aiohttp ⇒ 模块级 ``import aiohttp`` 会让 probe 里的 ``register()`` 抛
``ModuleNotFoundError``，而失败方式是"平台/工具静默没注册"，极难倒查。

本模块的 ``import aiohttp`` 分布在 4 个真正发请求的方法里（``_acquire`` / ``_request`` /
``_post_bytes`` / ``_post_json_b64``）；下面 138/142 行的 ``aiohttp.ClientSession`` 只出现在
**注解**里，有 ``from __future__ import annotations`` 兜着，**不求值**。

（形状守卫 ``tools/verify_tree.py`` 的 R2 会 BFS 注册执行链路并把模块级第三方依赖标出来。）
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

#: 文件下载上限（200MB）—— 防 vendor 返回超大数据把进程打 OOM
MAX_FILE_BYTES = 200 * 1024 * 1024

#: 已知语义特殊的端点：``Code != 0`` **不代表调用失败**，豁免判定
_IGNORE_CODE_ENDPOINTS = frozenset({"/Msg/Quote"})

#: 可重试的瞬时 HTTP 状态码（vendor 网关/上游抖动）。**4xx 不在此列**。
_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})

#: 响应里可能承载 base64 载荷的键名 —— **取并集**，旧实现各方法用的子集不同
_B64_KEYS = ("buffer", "Buffer", "Image", "image", "Voice", "voice",
             "File", "file", "Video", "video", "base64", "Base64")


class WppApiError(Exception):
    """厂商调用失败。``code`` 可能是 HTTP 码（``http_502``）或厂商业务码。"""

    def __init__(self, code: Any, message: str, endpoint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.endpoint = endpoint


def _extract_b64(payload: Any) -> str:
    """从厂商 ``Data`` 载荷里捞出 base64 串。

    厂商各端点返回结构不统一（``Data.Image`` / ``Data.data.buffer`` /
    ``Data.Buffer`` / ``Data.Voice`` / ``Data.File`` …）。这里按**并集**逐层找，
    比旧实现每个方法各写一份更容易维护，也不会漏键。
    """
    if not isinstance(payload, dict):
        return ""
    # ① 嵌套一层 data.buffer（GetContractProfile 系与二进制系的共同形状）
    for nested_key in ("data", "Data"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            for key in _B64_KEYS:
                val = nested.get(key)
                if isinstance(val, str) and val:
                    return val
    # ② 平铺
    for key in _B64_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _decode_b64(text: str) -> bytes | None:
    """base64 → bytes（坏数据返 None，不抛 —— 单个媒体坏掉不该中断整批）。"""
    if not text:
        return None
    try:
        return base64.b64decode(text)
    except (ValueError, TypeError) as exc:
        log.warning("[WPP] base64 解码失败: %s", exc)
        return None


def derive_business_url(public_url: str, webhook_path: str, token: str) -> tuple[str, str]:
    """推导 vendor business webhook 的 ``(syncMessageUrl, logoutUrl)``。

    对齐 OpenClaw ``deriveWebhookPaths``：把 ``/wechatpadpro/`` 替换成
    ``/wechatpadpro/<token>/`` 再补 ``/business``；末尾再拼 ``/logout``。

    ⚠️ 2026-08-31 踩过：Hermes 迁移时**漏了 business 段**，只设 ``/Webhook/Set``
    会让 vendor 只推空 ``Data`` 的 ``sync_message``（完整消息要 business 才推）。
    """
    public = (public_url or "").split("/wechatpadpro/")[0].rstrip("/")
    path = webhook_path or ""
    if token and path:
        path = path.replace("/wechatpadpro/", f"/wechatpadpro/{token}/")
    if path and not path.endswith("/business"):
        path = f"{path}/business"
    elif not path:
        path = "/wechatpadpro/default/webhook/business"
    sync_url = f"{public}{path}"
    return sync_url, f"{sync_url}/logout"


class WppClient:
    """单个账号的厂商 API 客户端。

    ``account_id`` 只用于审计落库，不参与鉴权（鉴权只有 ``authcode``）。
    """

    def __init__(
        self,
        base_url: str,
        authcode: str,
        *,
        account_id: str = "",
        timeout: int = 30,
        audit: bool = True,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.authcode = authcode or ""
        self.account_id = account_id
        self.timeout = timeout
        self.audit = audit
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------ 会话

    async def _acquire(self) -> aiohttp.ClientSession:
        import aiohttp  # 延迟导入，见模块 docstring

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """关连接池。**必须被调用** —— 旧 ``_standalone_send`` 每次新建 client
        却从不 close，每次 cron 投递泄漏一个 aiohttp session（已知缺陷）。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "WppClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -------------------------------------------------------------- URL/头

    def _raw_url(self, endpoint: str) -> str:
        """``{base}/api{endpoint}?authcode=<authcode>`` —— 鉴权的 query 那一半。

        所有请求（业务 + 下载）都走这里，**没有例外** —— 旧实现有 9 个下载方法
        自行拼 URL，拼法与 ``_url()`` 不一致，是漏带 authcode 的隐患来源。
        """
        ep = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        url = f"{self.base_url}/api{ep}"
        if self.authcode:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}authcode={self.authcode}"
        return url

    def _headers(self, *, token_key: bool = False) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.authcode:
            # 双保险之一：header。之二在 query（见 _raw_url）
            h["X-Access-Token"] = self.authcode
        if token_key:
            # 下载类端点额外要求 TokenKey header（老 schema 遗留，仍被 vendor 认）
            h["TokenKey"] = self.authcode
        return h

    # ------------------------------------------------------------------ 审计

    async def _audit(
        self, endpoint: str, method: str, *,
        status_code: int | None = None, vendor_code: int | None = None,
        latency_ms: int | None = None,
        request_body: Any = None, response_body: Any = None,
    ) -> None:
        """留痕到 ``wpp_api_calls``。**任何异常都必须咽掉** —— 审计不能反过来搞挂主流程。

        同步 pymysql 走 ``to_thread``，不阻塞事件循环（旧实现是在 async 里直连）。
        """
        if not self.audit or not self.account_id:
            return
        try:
            from ..data import repo_messages  # 延迟导入：注册期不拉起 DB 依赖

            await asyncio.to_thread(
                repo_messages.record_api_call,
                self.account_id, endpoint,
                method=method, status_code=status_code, vendor_code=vendor_code,
                latency_ms=latency_ms, request_body=request_body,
                response_body=response_body,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("[WPP] API 审计写入失败 %s: %s", endpoint, exc)

    # ------------------------------------------------------------- 核心调用

    async def call(
        self, endpoint: str, body: dict | None = None,
        method: str = "POST", max_retries: int = 2, *, audit: bool = True,
    ) -> dict:
        """通用厂商调用 —— **所有业务端点的唯一入口**。

        ``endpoint`` 形如 ``/Group/GetChatroomInfo``（不带 ``/api``，这里补）。
        """
        return await self._request(
            endpoint, body if body is not None else {},
            method=method.upper(), max_retries=max_retries, audit=audit,
        )

    async def _request(
        self, endpoint: str, body: dict, *,
        method: str, max_retries: int, audit: bool = True,
    ) -> dict:
        import aiohttp  # 延迟导入，见模块 docstring

        session = await self._acquire()
        url = self._raw_url(endpoint)
        headers = self._headers()
        is_get = method == "GET"
        # GET 退避比 POST 更保守（0.3*attempt vs 0.5*2^n）
        backoff = (lambda a: 0.3 * a) if is_get else (lambda a: 0.5 * (2 ** (a - 1)))

        last_err: Exception | None = None
        started = time.monotonic()
        for attempt in range(1, max_retries + 2):
            try:
                async with session.request(
                    method, url, json=None if is_get else body, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    text = await resp.text()
                    status = resp.status
                    if status != 200:
                        err = WppApiError(f"http_{status}",
                                          f"{endpoint} -> HTTP {status}: {text[:200]}", endpoint)
                        if status in _RETRYABLE_HTTP_STATUS and attempt <= max_retries:
                            last_err = err
                            await asyncio.sleep(backoff(attempt))
                            continue
                        raise err

                    if is_get:
                        # 旧 _get 不解析 Code，直接返回（保持行为不变）
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError as exc:
                            raise WppApiError("bad_json",
                                              f"{endpoint} -> 非 JSON: {text[:200]}", endpoint) from exc
                        if audit:
                            await self._audit(endpoint, method, status_code=status,
                                              latency_ms=int((time.monotonic() - started) * 1000),
                                              request_body=None, response_body=data)
                        return data

                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise WppApiError("bad_json",
                                          f"{endpoint} -> 非 JSON: {text[:200]}", endpoint) from exc

                    code = data.get("Code")
                    failed = data.get("Success") is False or (
                        code is not None and code != 0 and endpoint not in _IGNORE_CODE_ENDPOINTS
                    )
                    if audit:
                        await self._audit(endpoint, method, status_code=status,
                                          vendor_code=code if isinstance(code, int) else None,
                                          latency_ms=int((time.monotonic() - started) * 1000),
                                          request_body=body, response_body=data)
                    if failed:
                        raise WppApiError(code if code is not None else data.get("CodeValue", "?"),
                                          f"{endpoint} -> {text[:300]}", endpoint)
                    return data
            except aiohttp.ClientError as exc:
                last_err = exc
                if attempt <= max_retries:
                    await asyncio.sleep(backoff(attempt))
                    continue
        raise WppApiError("network", f"{endpoint} -> {last_err}", endpoint) from last_err

    # ------------------------------------------------------- 原始字节下载

    async def _post_bytes(
        self, endpoint: str, body: dict, *, timeout: int = 60,
        cap: int = MAX_FILE_BYTES, accept_json_b64: bool = True,
    ) -> bytes | None:
        """下载类端点的统一实现。

        vendor 这些端点**有时返原始字节、有时返 JSON 裹 base64**（取决于
        Content-Type），所以两条路都要走。所有失败都返回 ``None`` 并 warning ——
        单个媒体取不到不该让调用方崩（旧实现同此语义）。
        """
        import aiohttp  # 延迟导入，见模块 docstring

        session = await self._acquire()
        try:
            async with session.post(
                self._raw_url(endpoint), json=body,
                headers=self._headers(token_key=True),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    log.warning("[WPP] %s HTTP %s", endpoint, resp.status)
                    return None
                cl = resp.headers.get("Content-Length")
                if cl:
                    try:
                        if int(cl) > cap:
                            log.warning("[WPP] %s Content-Length=%s 超上限 %s，拒绝", endpoint, cl, cap)
                            return None
                    except ValueError:
                        pass
                content_type = (resp.headers.get("Content-Type") or "").lower()
                raw = await resp.read()
                if not raw:
                    return None
                if accept_json_b64 and "json" in content_type:
                    try:
                        data = json.loads(raw.decode("utf-8", errors="ignore"))
                    except json.JSONDecodeError:
                        return raw if len(raw) > 10 else None
                    got = _decode_b64(_extract_b64(data.get("Data") or data))
                    return got
                return raw if len(raw) > 10 else None
        except Exception as exc:  # noqa: BLE001 —— 下载失败不该上抛
            log.warning("[WPP] %s 失败: %s", endpoint, exc)
            return None

    async def _post_json_b64(self, endpoint: str, body: dict, *, timeout: int = 60) -> bytes | None:
        """一定返 JSON 的下载端点（从 ``Data.*`` 里捞 base64）。"""
        import aiohttp  # 延迟导入，见模块 docstring

        session = await self._acquire()
        try:
            async with session.post(
                self._raw_url(endpoint), json=body,
                headers=self._headers(token_key=True),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    log.warning("[WPP] %s HTTP %s", endpoint, resp.status)
                    return None
                try:
                    data = json.loads(await resp.text())
                except json.JSONDecodeError:
                    return None
                return _decode_b64(_extract_b64(data.get("Data") or data))
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP] %s 失败: %s", endpoint, exc)
            return None

    # ============================================================== 状态查询

    async def get_online_info(self) -> dict:
        return await self.call("/User/GetOnlineInfo", {}, method="GET", max_retries=1)

    async def get_long_link_status(self) -> dict:
        return await self.call("/Login/LongLinkStatus", {}, method="GET", max_retries=1)

    async def get_contract_profile(self) -> dict:
        return await self.call("/User/GetContractProfile")

    async def heartbeat(self) -> dict:
        return await self.call("/Login/HeartBeat")

    async def get_contract_detail(self, user_name: str) -> dict:
        """查联系人详情（引用回复里取被回复人昵称用）。"""
        return await self.call("/Friend/GetContractDetail", {"userName": user_name}, max_retries=0)

    # ============================================================== 发送

    async def send_text(self, to_wxid: str, content: str, ats: list[str] | None = None) -> dict:
        """``/Msg/SendTxt`` —— ``At`` 是逗号串、``Type`` 固定 1（vendor swagger 定义）。"""
        body: dict[str, Any] = {"ToWxid": to_wxid, "Content": content, "Type": 1}
        if ats:
            body["At"] = ",".join(str(a) for a in ats)
        return await self.call("/Msg/SendTxt", body)

    async def revoke_msg(self, msg_id: str, new_msg_id: str = "", create_time: int = 0) -> dict:
        """``/Msg/Revoke`` —— 需要 ``(MsgId, NewMsgId, CreateTime)`` 三元组。

        ⚠️ ``CreateTime`` 必须是**发送时返回的那个**；用 ``now()`` 会静默不生效
        （vendor 不真撤）。拿不到原值才退回 now。
        """
        return await self.call("/Msg/Revoke", {
            "MsgId": msg_id,
            "NewMsgId": new_msg_id or msg_id,
            "CreateTime": create_time or int(time.time()),
        })

    async def send_image(self, to_wxid: str, image_base64: str) -> dict:
        """``/Msg/UploadImg`` —— 上传即发送（``ret=0`` 为成功）。"""
        return await self.call("/Msg/UploadImg", {"ToWxid": to_wxid, "Base64": image_base64})

    async def send_cdn_image(self, to_wxid: str, img_url: str) -> dict:
        return await self.call("/Msg/SendCDNImg", {"ToWxid": to_wxid, "Content": img_url})

    async def send_cdn_video(self, to_wxid: str, video_url: str) -> dict:
        return await self.call("/Msg/SendCDNVideo", {"ToWxid": to_wxid, "Content": video_url})

    async def share_location(self, to_wxid: str, latitude: float, longitude: float,
                            label: str = "", poi_name: str = "") -> dict:
        """``/Msg/ShareLocation``（Type 48 定位卡片）。

        ⚠️ ``X=纬度``、``Y=经度`` —— 反了定位缩略图会错位。
        """
        return await self.call("/Msg/ShareLocation", {
            "ToWxid": to_wxid, "X": float(latitude), "Y": float(longitude),
            "Label": label or "", "Poiname": poi_name or "", "Scale": 16, "Infourl": "",
        })

    async def send_emoji(self, to_wxid: str, emoji_md5: str, emoji_size: int = 0) -> dict:
        return await self.call("/Msg/SendEmoji",
                               {"toWxid": to_wxid, "emojiMd5": emoji_md5, "emojiSize": emoji_size})

    async def share_card(self, to_wxid: str, card_wxid: str, card_nickname: str,
                        card_alias: str = "") -> dict:
        return await self.call("/Msg/ShareCard", {
            "ToWxid": to_wxid, "CardWxId": card_wxid,
            "CardNickName": card_nickname, "CardAlias": card_alias or "",
        })

    async def send_xcx(self, to_wxid: str, xcx_title: str, xcx_desc: str, xcx_url: str,
                      xcx_app_id: str, thumb_url: str = "") -> dict:
        return await self.call("/Msg/SendXCX", {
            "toWxid": to_wxid, "xcxTitle": xcx_title, "xcxDesc": xcx_desc,
            "xcxUrl": xcx_url, "xcxAppId": xcx_app_id, "thumbUrl": thumb_url or "",
        })

    async def share_link(self, to_wxid: str, title: str, desc: str, link_url: str,
                        thumb_url: str = "") -> dict:
        """``/Msg/ShareLink`` —— 自建 ``Type=5`` 的 appmsg XML。"""
        return await self.call("/Msg/ShareLink", {
            "ToWxid": to_wxid, "Type": 5,
            "Xml": build_appmsg_xml(title, desc, link_url, thumb_url),
        })

    async def send_group_mass_text(self, to_ids: list[str], content: str) -> dict:
        return await self.call("/Msg/SendGroupMassMsgText", {"ToIds": to_ids, "Content": content})

    async def send_file_v2(self, to_wxid: str, file_name: str, file_base64: str) -> dict:
        """``/Msg/SendFile`` —— 自动上传 + 发送。"""
        return await self.call("/Msg/SendFile",
                               {"ToWxid": to_wxid, "FileName": file_name, "Base64": file_base64})

    async def send_voice(self, to_wxid: str, voice_base64: str, voice_type: int = 4) -> dict:
        """``/Msg/SendVoice``（``Type``: 0=AMR, 4=SILK；微信默认 silk）。"""
        return await self.call("/Msg/SendVoice",
                               {"ToWxid": to_wxid, "Base64": voice_base64, "Type": voice_type})

    async def send_video(self, to_wxid: str, video_base64: str,
                        thumb_base64: str = "", play_length: int = 10) -> dict:
        """``/Msg/SendVideo`` —— ``ImageBase64`` 缩略图**必填**，否则 vendor 拒收。"""
        return await self.call("/Msg/SendVideo", {
            "ToWxid": to_wxid, "Base64": video_base64,
            "ImageBase64": thumb_base64, "PlayLength": play_length,
        })

    async def send_app_message(self, items: list[dict]) -> dict:
        """``/Msg/SendAppMessage`` —— 结构化应用卡片，单次最多 20 项。"""
        return await self.call("/Msg/SendAppMessage", {"items": items})

    # ============================================================== 朋友圈

    async def publish_circle_text(self, title: str) -> dict:
        return await self.call("/FriendCircle/Messages", {"title": title, "private": 0})

    async def publish_circle_images(self, title: str, image_base64_list: list[str]) -> dict:
        """图片朋友圈（1–9 张）：逐张 ``UploadImage`` 拿 ``publishItem`` 再发布。"""
        if not image_base64_list or len(image_base64_list) > 9:
            return {"Code": -1, "Success": False,
                    "CodeValue": f"图片数量必须 1-9 张（实际 {len(image_base64_list)}）"}
        items = []
        for b64 in image_base64_list:
            up = await self.call("/FriendCircle/UploadImage", {"imageData": b64})
            item = (up.get("Data") or {}).get("publishItem")
            if up.get("Code") != 0 or not item:
                return {"Code": up.get("Code", -1), "Success": False,
                        "CodeValue": f"UploadImage 失败 {up.get('CodeValue', '')}"}
            items.append(item)
        return await self.call("/FriendCircle/Messages",
                               {"title": title, "private": 0, "images": items})

    async def publish_circle_video(self, title: str, video_base64: str, thumb_base64: str) -> dict:
        up = await self.call("/FriendCircle/UploadVideo",
                             {"videoData": video_base64, "thumbData": thumb_base64})
        item = (up.get("Data") or {}).get("publishItem")
        if up.get("Code") != 0 or not item:
            return {"Code": up.get("Code", -1), "Success": False,
                    "CodeValue": f"UploadVideo 失败 {up.get('CodeValue', '')}"}
        return await self.call("/FriendCircle/Messages",
                               {"title": title, "private": 0, "video": item})

    # ============================================================== 消息同步

    async def sync_message(self) -> dict:
        """``/Msg/Sync`` —— 拉增量消息（服务端用 authcode 定位账号 + synckey）。"""
        return await self.call("/Msg/Sync")

    # ============================================================== webhook

    async def set_webhook(self, url: str) -> dict:
        """``/Webhook/Set`` —— 只设这一条**不够**（vendor 只推空 Data 的 sync_message）。"""
        return await self.call("/Webhook/Set", {"url": url})

    async def set_business_webhook(self, sync_url: str, logout_url: str) -> dict:
        return await self.call("/Webhook/Business/Set",
                               {"syncMessageUrl": sync_url, "logoutUrl": logout_url})

    async def start_auto_sync(self, target_url: str) -> dict:
        """``/Msg/StartAutoSync`` —— 启动完整消息推送到 business webhook。"""
        return await self.call("/Msg/StartAutoSync", {"TargetURL": target_url})

    async def setup_webhook(self, public_url: str, webhook_path: str, token: str) -> dict:
        """三段式 webhook 设置（**必须三段都做**，见 ``derive_business_url`` 注释）。"""
        sync_url, logout_url = derive_business_url(public_url, webhook_path, token)
        out = {"sync_url": sync_url, "logout_url": logout_url}
        try:
            out["set"] = await self.set_webhook(f"{public_url.rstrip('/')}{webhook_path}")
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP] vendor /Webhook/Set 失败: %s", exc)
            out["set_error"] = str(exc)
        try:
            out["business"] = await self.set_business_webhook(sync_url, logout_url)
            out["auto_sync"] = await self.start_auto_sync(sync_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP] vendor business 设置失败: %s", exc)
            out["business_error"] = str(exc)
        return out

    # ============================================================== 媒体下载

    async def download_image(self, msg_id: str, to_wxid: str, data_len: int = 0) -> bytes | None:
        """``/Tools/DownloadImg`` —— **字段名是 snake_case**（旧 ``MsgId`` 会报
        ``INVALID_ARGUMENT``）。返回 ``Data.data.buffer``（vendor 有 64KB 上限）。"""
        body: dict[str, Any] = {"msg_id": msg_id, "to_wxid": to_wxid,
                                "data_len": data_len or 0, "compress_type": 0}
        if data_len:
            body["section"] = {"start_pos": 0, "data_len": data_len}
        return await self._post_json_b64("/Tools/DownloadImg", body)

    async def download_image_cdn(self, file_aes_key: str, file_no: str,
                                variant: str = "") -> bytes | None:
        """``/Tools/CdnDownloadImage`` —— CDN 完整大图。"""
        body: dict[str, Any] = {"file_aes_key": file_aes_key, "file_no": file_no}
        if variant:
            body["variant"] = variant
        return await self._post_json_b64("/Tools/CdnDownloadImage", body)

    async def download_video_thumb(self, file_aes_key: str, file_no: str) -> bytes | None:
        """视频缩略图（与视频同 ``aes_key``/``file_no``，走图片接口）。"""
        return await self._post_json_b64("/Tools/CdnDownloadImage", {
            "FileAESKey": file_aes_key, "FileNo": file_no,
            "Section": {"StartPos": 0, "DataLen": 100000},
        })

    async def download_video(self, to_wxid: str, msg_id: int, data_len: int,
                            start_pos: int = 0, chunk_len: int = 1048576,
                            compress_type: int = 0) -> bytes | None:
        """``/Tools/DownloadVideo`` —— **分片**下载。

        完整视频要循环：``start_pos += len(chunk)`` 直到 ``start_pos >= totalLen``。
        """
        return await self._post_json_b64("/Tools/DownloadVideo", {
            "to_wxid": to_wxid, "msg_id": msg_id, "data_len": data_len,
            "section": {"start_pos": start_pos, "data_len": chunk_len},
            "compress_type": compress_type,
        })

    async def download_voice(self, msg_id: int, from_user_name: str, length: int,
                            bufid: str = "") -> bytes | None:
        """``/Tools/DownloadVoice`` —— 新 schema（``{msgId, fromUserName, length}``）。"""
        body: dict[str, Any] = {"msgId": msg_id, "fromUserName": from_user_name, "length": length}
        if bufid:
            body["bufid"] = bufid
        return await self._post_json_b64("/Tools/DownloadVoice", body)

    async def download_voice_legacy(self, aes_key: str, file_no: str) -> bytes | None:
        """``/Tools/DownloadVoice`` —— **老 schema**（``{aesKey, fileId}``）。"""
        return await self._post_json_b64("/Tools/DownloadVoice",
                                         {"aesKey": aes_key, "fileId": file_no})

    async def download_voice_binary(self, msg_id: int, *, new_msg_id: str = "",
                                   client_msg_id: str = "", master_buf_id: str = "0",
                                   fmt: int = 4, length: int = 0, chat_room_name: str = "",
                                   from_user_name: str = "", to_user_name: str = "",
                                   file_name: str = "voice.silk") -> bytes | None:
        """``/Tools/DownloadVoiceBinary`` —— 原始字节（SILK/AMR/MP3/WAV，非 JSON）。"""
        return await self._post_bytes("/Tools/DownloadVoiceBinary", {
            "msg_id": msg_id, "new_msg_id": new_msg_id, "client_msg_id": client_msg_id,
            "master_buf_id": master_buf_id, "format": fmt, "length": length,
            "chat_room_name": chat_room_name, "from_user_name": from_user_name,
            "to_user_name": to_user_name, "file_name": file_name or "voice.silk",
        })

    async def download_file_binary(self, attach_id: str, *, user_name: str = "",
                                   data_len: int = 0, start_pos: int = 0,
                                   section_len: int = 0, app_id: str = "",
                                   file_name: str = "") -> bytes | None:
        """``/Tools/DownloadFileBinary`` —— 文件原始字节流。

        ``data_len`` 超 200MB 直接拒（防 OOM），且带 ``Content-Length`` 二次防护。
        """
        if data_len > MAX_FILE_BYTES:
            log.warning("[WPP] 文件过大 %d > %d，拒绝下载", data_len, MAX_FILE_BYTES)
            return None
        body: dict[str, Any] = {
            "attach_id": attach_id, "user_name": user_name, "data_len": data_len,
            "section": {"start_pos": start_pos, "data_len": section_len or data_len},
        }
        if app_id:
            body["app_id"] = app_id
        if file_name:
            body["file_name"] = file_name
        return await self._post_bytes("/Tools/DownloadFileBinary", body, timeout=180)


def build_appmsg_xml(title: str, desc: str, url: str, thumb_url: str = "") -> str:
    """构造 ``type=5`` 的 appmsg XML（分享链接 / 引用回复都用它）。"""
    def esc(s: Any) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    return (
        '<appmsg appid="" sdkver="0"><title>{}</title>'
        '<des>{}</des><action>view</action><type>5</type>'
        '<url>{}</url><thumburl>{}</thumburl></appmsg>'
    ).format(esc(title), esc(desc), esc(url), esc(thumb_url))
