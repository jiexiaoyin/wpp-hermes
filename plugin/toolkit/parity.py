"""TS 工具面里**不能用通用 handler 表达**的少数端点（2026-10-18 补齐）。

通用端点表（``registry.register_generic_tools``）能覆盖 POST/GET + JSON 的绝大多数
工具。剩下三类必须单独写：

* ``wpp_fc_export_download`` —— ``GET /FriendCircle/BatchDownloadFile`` 返回**原始
  文件流**，走 ``WppClient.get_bytes``（JSON 解析会毁掉二进制；TS 也只回报字节数/
  文件名，不把内容塞进 agent 上下文）；
* ``wpp_fc_auto_forward`` —— 开启自动跟发 = 开启自动发布，TS 在 ``enabled=true`` 时
  调 ``assertFriendCirclePublishAllowed``。这是**条件**门禁，不能整条工具拉黑
  （否则连"关掉跟发"都做不到），所以放这里按参数判；
* ``wpp_xw_events`` —— SSE 长连接，普通 JSON 客户端会挂住。用
  ``WppClient.read_sse`` 最多等 10 秒返回已收到的事件。

其余新增端点全部在 :data:`plugin.toolkit.policy.ADDED_GROUP`（数据表）。
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from . import policy
from .registry import get_client, resolve_account_id
from .toolout import dump_capped, dump_error

log = logging.getLogger(__name__)


def _client_for(adapter_getter, params):
    adapter = adapter_getter()
    account_id = params.pop("account_id", "default")
    if not adapter:
        return None, None, account_id, "adapter 未就绪"
    # 单账号部署：``default`` 落到唯一账号（否则所有 parity 工具都报未连接）
    account_id = resolve_account_id(adapter, account_id)
    client = get_client(adapter, account_id)
    if not client:
        return adapter, None, account_id, f"账号 {account_id} 未连接"
    return adapter, client, account_id, ""


def _resp_ok(resp) -> bool:
    """与 ``toolkit.registry._resp_ok`` 同一判据（``platform.outbound.send_ok``）。"""
    from ..platform import outbound

    return outbound.send_ok(resp)


def _dump(payload) -> str:
    """所有 parity 工具的统一出口（截断也必须是合法 JSON，见 ``toolout``）。"""
    return dump_capped(payload)


def _readable_outbound_content(mtype: str, params: dict, content: str) -> str:
    """``wpp_send_message`` 的统一入库正文（媒体只留标记/短 URL，绝不塞 base64）。"""
    if mtype == "text":
        return content
    if mtype == "image":
        return f"[图片] {content}" if content.startswith(("http://", "https://")) and len(content) <= 500 else "[图片]"
    if mtype == "video":
        return "[视频]"
    if mtype == "voice":
        return "[语音]"
    if mtype == "file":
        return f"[文件] {params.get('fileName') or ''}".strip()
    if mtype == "link":
        return f"[链接] {params.get('title') or ''}".strip()
    if mtype == "card":
        return f"[名片] {params.get('cardNickname') or ''}".strip()
    if mtype == "location":
        return f"[位置] {params.get('label') or ''}".strip()
    if mtype == "miniprogram":
        page = str(params.get("pagePath") or "").strip()
        return f"[小程序] {params.get('title') or ''}".strip() + (f" (页面 {page})" if page else "")
    if mtype == "emoji":
        return f"[表情] {content}".strip()
    return content


async def _send_message_router(client, params: dict,
                               adapter=None, account_id: str = "") -> dict:
    """TS ``dispatch/send-message.ts::sendMessage`` 的 Python 版（按 type 路由）。

    这里只做「路由 + 调 WppClient 现成方法」，媒体解析复用
    ``toolkit.msg._resolve_to_base64``（它保持旧契约：拿不到内容就抛错，绝不发空媒体）。
    返回统一 ``{"ok", "msgId", "newMsgId", "error"}``（与 TS 同形）。
    """
    from ..platform import outbound
    from .msg import _resolve_to_base64

    to_wxid = str(params.get("toWxid") or "")
    mtype = str(params.get("type") or "").strip().lower()
    content = str(params.get("content") or "")
    if not to_wxid or not mtype:
        return {"ok": False, "error": "toWxid/type 必填"}
    try:
        if mtype == "text":
            resp = await client.send_text(to_wxid, content, params.get("ats"))
        elif mtype == "image":
            resp = await client.send_image(to_wxid, await _resolve_to_base64(content))
        elif mtype == "video":
            thumb = str(params.get("thumbUrl") or "")
            resp = await client.send_video(
                to_wxid, await _resolve_to_base64(content),
                await _resolve_to_base64(thumb) if thumb else "",
                int((params.get("durationMs") or 0) / 1000) or 10)
        elif mtype == "voice":
            # 厂商只收 SILK（Type=4）；非 silk 降级为文件（TS v1.3.53 同口径）
            b64 = await _resolve_to_base64(content)
            no_query = content.split("?")[0].lower()
            if not (content.startswith("data:audio/silk") or no_query.endswith(".silk")):
                fname = content.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "voice.mp3"
                resp = await client.send_file_via_app(to_wxid, fname, b64)
            else:
                resp = await client.send_voice(
                    to_wxid, b64, 4, int((params.get("durationMs") or 0) / 1000))
        elif mtype == "file":
            # TS sendFile = 下载 → /Tools/UploadFile → /Msg/ShareLink Type=6
            resp = await client.send_file_via_app(
                to_wxid, params.get("fileName") or "file",
                await _resolve_to_base64(content))
        elif mtype == "link":
            resp = await client.share_link(
                to_wxid, params.get("title") or "", params.get("desc") or "",
                content, params.get("thumbUrl") or "")
        elif mtype == "card":
            resp = await client.share_card(
                to_wxid, params.get("cardWxid") or "",
                params.get("cardNickname") or "")
        elif mtype == "location":
            resp = await client.share_location(
                to_wxid, params.get("latitude") or 0,
                params.get("longitude") or 0, params.get("label") or "")
        elif mtype == "miniprogram":
            resp = await client.send_xcx(
                to_wxid, params.get("title") or "", params.get("desc") or "",
                content, params.get("appId") or "", params.get("thumbUrl") or "",
                params.get("pagePath") or "",
                params.get("username") or params.get("xcxUsername") or "")
        elif mtype == "emoji":
            resp = await client.send_emoji(
                to_wxid, content, int(params.get("size") or 0))
        else:
            return {"ok": False, "error": f"不支持的 type: {mtype}"}
    except Exception as exc:  # noqa: BLE001 —— 与 TS 一致：路由错误回给调用方
        return {"ok": False, "error": str(exc)}
    ids = outbound.extract_outbound_msg_ids(resp)
    ok = outbound.send_ok(resp)
    if ok and adapter is not None and account_id:
        # 统一发送工具也必须落库（TS v1.3.16 OUTBOUND-PERSIST）。旧实现只发不记,
        # agent 经 ``wpp_send_message`` 发的群消息在历史里完全隐形。
        try:
            await adapter.persist_chat_outbound(
                account_id, to_wxid, mtype,
                _readable_outbound_content(mtype, params, content), resp)
        except Exception as exc:  # noqa: BLE001 —— 审计是附带动作
            log.warning("[WPP] wpp_send_message 出站落库失败: %s", exc)
    return {"ok": ok, "msgId": ids.msg_id, "newMsgId": ids.new_msg_id,
            "error": "" if ok else outbound.send_error_text(resp)}


def register_parity_tools(ctx, adapter_getter) -> int:
    """注册 4 个需要专用请求形状的工具。返回注册数。"""

    async def fc_export_download(**params):
        _, client, _, error = _client_for(adapter_getter, params)
        if error:
            return error
        task_id = params.get("task_id") or ""
        if not task_id:
            return "缺少 task_id"
        endpoint = f"/FriendCircle/BatchDownloadFile?task_id={quote(str(task_id), safe='')}"
        name = params.get("name") or ""
        if name:
            endpoint += f"&name={quote(str(name), safe='')}"
        result = await client.get_bytes(endpoint)
        if not result:
            return _dump({"ok": False, "error": "下载失败（任务未完成或产物不存在）"})
        return _dump({
            "ok": True,
            "fileName": result.get("file_name") or name or "朋友圈.zip",
            "contentType": result.get("content_type") or "",
            "sizeBytes": result.get("size_bytes") or 0,
        })

    async def fc_auto_forward(**params):
        adapter, client, account_id, error = _client_for(adapter_getter, params)
        if error:
            return error
        enabled = bool(params.get("enabled"))
        if enabled:
            # TS ``friendcircle.ts:301``：开启跟发 = 开启自动发布 ⇒ 过发布开关。
            acct_cfg = policy.raw_account_cfg(adapter, account_id)
            if acct_cfg is None:
                return policy.FRIEND_CIRCLE_PUBLISH_GATE.closed("读不到账号配置")
            if not acct_cfg.get("friendCirclePublishEnabled"):
                return policy.FRIEND_CIRCLE_PUBLISH_GATE.closed()
        body = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await client.call("/FriendCircle/AutoForward", body)
        except Exception as exc:  # noqa: BLE001
            return dump_error(f"{type(exc).__name__}: {exc}")
        return _dump({"ok": _resp_ok(resp), "code": resp.get("Code"),
                      "data": resp.get("Data")})

    async def xw_events(**params):
        _, client, _, error = _client_for(adapter_getter, params)
        if error:
            return error
        session_id = params.get("session_id") or ""
        if not session_id:
            return "缺少 session_id"
        endpoint = f"/XiaoWei/Chat/Sessions/{quote(str(session_id), safe='')}/Events"
        after = params.get("after_sequence")
        if after is not None:
            endpoint += f"?after_sequence={quote(str(after), safe='')}"
        return _dump(await client.read_sse(endpoint, timeout=10, max_events=50))

    async def send_message(**params):
        adapter, client, account_id, error = _client_for(adapter_getter, params)
        if error:
            return error
        return _dump(await _send_message_router(
            client, params, adapter=adapter, account_id=account_id))

    defs = [
        ("wpp_send_message",
         "统一发送消息. toWxid 目标(群id或对方wxid), type 类型(text/image/video/voice/"
         "file/link/card/location/miniprogram/emoji), content 内容或URL. 文本用 content "
         "正文; 图片/视频/语音/文件用 content 填 OSS/公网URL; 链接用 title+desc+content"
         "(URL); 名片用 cardWxid+cardNickname; 位置用 latitude+longitude+label; 小程序用 "
         "appId+title+desc, 要直达内部页面再加 pagePath+username. 推荐优先用此工具.",
         {
             "type": "object",
             "properties": {
                 "toWxid": {"type": "string", "description": "目标 wxid / 群 id"},
                 "type": {"type": "string",
                          "description": "text/image/video/voice/file/link/card/location/miniprogram/emoji"},
                 "content": {"type": "string",
                             "description": "正文(文本) 或 URL(媒体/文件/链接)"},
                 "fileName": {"type": "string", "description": "file: 文件名（含扩展名）"},
                 "title": {"type": "string", "description": "link/miniprogram: 标题"},
                 "desc": {"type": "string", "description": "link/miniprogram: 描述"},
                 "thumbUrl": {"type": "string",
                              "description": "image/video/link: 缩略图 URL"},
                 "appId": {"type": "string", "description": "miniprogram: 小程序 appId"},
                 "latitude": {"type": "number", "description": "location: 纬度"},
                 "longitude": {"type": "number", "description": "location: 经度"},
                 "label": {"type": "string", "description": "location: 位置标签"},
                 "cardWxid": {"type": "string", "description": "card: 名片 wxid"},
                 "cardNickname": {"type": "string", "description": "card: 名片昵称"},
                 "durationMs": {"type": "integer", "description": "voice/video: 时长毫秒"},
                 "size": {"type": "integer", "description": "emoji: 大小字节"},
                 "ats": {"type": "array", "items": {"type": "string"},
                         "description": "text: 群 @ wxid 列表"},
                 "pagePath": {"type": "string",
                              "description": "miniprogram: 内部页面路径（py2 暂按 legacy 卡片发送）"},
                 "username": {"type": "string",
                              "description": "miniprogram: 小程序 username（py2 暂按 legacy 卡片发送）"},
                 "account_id": {"type": "string", "description": "账号ID（默认 default）"},
             },
             "required": ["toWxid", "type", "content"],
         },
         send_message),
        ("wpp_fc_export_download",
         "下载朋友圈批量导出的产物文件（任务完成后才可下载）. name 可选: 朋友圈.zip（默认）"
         " / 朋友圈.md / 导出信息.json. 返回文件字节数与文件名.",
         {
             "type": "object",
             "properties": {
                 "task_id": {"type": "string", "description": "wpp_fc_export 返回的 task_id"},
                 "name": {"type": "string", "description": "产物名, 默认 朋友圈.zip"},
                 "account_id": {"type": "string", "description": "账号ID（默认 default）"},
             },
             "required": ["task_id"],
         },
         fc_export_download),
        ("wpp_fc_auto_forward",
         "配置朋友圈自动跟发（把 source_wxid 的新公开动态自动同步发到自己朋友圈）. "
         "enabled=true 时必须传 content_owner_authorized=true 确认已获授权, "
         "且账号级 friendCirclePublishEnabled 开关须已打开.",
         {
             "type": "object",
             "properties": {
                 "enabled": {"type": "boolean", "description": "是否开启自动跟发"},
                 "source_wxid": {"type": "string", "description": "被跟发的个人 wxid（开启时必填）"},
                 "delay_seconds": {"type": "integer", "description": "发现新动态后延迟发布秒数, 0-300"},
                 "location_mode": {"type": "integer", "description": "0=保留原位置, 1=移除位置"},
                 "black_list": {"type": "array", "items": {"type": "string"},
                                "description": "不允许查看跟发动态的 wxid 列表"},
                 "content_owner_authorized": {"type": "boolean",
                                              "description": "确认已获源内容所有者同步发布授权"},
                 "account_id": {"type": "string", "description": "账号ID（默认 default）"},
             },
             "required": ["enabled"],
         },
         fc_auto_forward),
        ("wpp_xw_events",
         "读取小微会话 SSE 事件流（最多等 10 秒）. after_sequence=断线续传用.",
         {
             "type": "object",
             "properties": {
                 "session_id": {"type": "string", "description": "会话 ID"},
                 "after_sequence": {"type": "integer", "description": "从该事件序号之后续传"},
                 "account_id": {"type": "string", "description": "账号ID（默认 default）"},
             },
             "required": ["session_id"],
         },
         xw_events),
    ]

    for name, description, schema, fn in defs:
        async def handler(_fn=fn, **params):
            try:
                return await _fn(**params)
            except Exception as exc:  # noqa: BLE001
                return dump_error(f"{type(exc).__name__}: {exc}")

        ctx.register_tool(
            name=name, toolset="wechatpadpro",
            schema={**schema, "description": description},
            handler=handler, is_async=True, description=description, emoji="💬",
        )
    log.info("[WPP] 已注册 %d 个专用形状工具", len(defs))
    return len(defs)
