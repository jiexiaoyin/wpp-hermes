"""WPP agent 工具（迁移自 wpp-openclaw dispatch/agent-tools/*-meta.ts）。

通过 ctx.register_tool 注册到 Hermes。handler 调 vendor API。
schema 用 JSON-schema dict（等价 TypeBox）。
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


def _get_client(adapter, account_id: str):
    """从 adapter 拿指定账号的 WppClient。"""
    return (adapter._clients or {}).get(account_id)


def _mk_handler(fn):
    """包装 handler，把异常转成工具返回字符串。"""
    async def handler(**params):
        try:
            result = await fn(**params)
            return json.dumps(result, ensure_ascii=False, default=str)[:4000]
        except Exception as e:  # noqa: BLE001
            return f"调用失败: {e}"
    return handler


def _mk_generic_handler(endpoint: str, method: str = "POST"):
    """通用 vendor 工具 handler：调 client.call(endpoint, body)。"""
    async def handler(adapter_getter, **params):
        adapter = adapter_getter()
        if not adapter:
            return "adapter 未就绪"
        account_id = params.pop("account_id", "default")
        client = (adapter._clients or {}).get(account_id)
        if not client:
            return f"账号 {account_id} 未连接"
        # 过滤空参数
        body = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await client.call(endpoint, body, method=method)
            return {"ok": resp.get("Success", False), "code": resp.get("Code"), "data": resp.get("Data")}
        except Exception as e:  # noqa: BLE001
            return f"调用失败: {e}"
    return handler


def _register_generic_tools(ctx, adapter_getter, tool_defs: list[dict]) -> int:
    """批量注册通用工具（基于 tools_data 定义表）。"""
    count = 0
    for td in tool_defs:
        schema_props = {}
        required = []
        for pname, pmeta in td.get("params", {}).items():
            pt = {"type": pmeta.get("type", "string")}
            if pmeta.get("desc"):
                pt["description"] = pmeta["desc"]
            schema_props[pname] = pt
            if pmeta.get("required"):
                required.append(pname)
        schema_props["account_id"] = {"type": "string", "description": "账号ID（默认 default）"}
        # 2026-09-01: description 注入 schema 顶层 — Hermes get_definitions() 只透传 schema，
        # 不合并 entry.description，不写顶层模型就看不到工具说明。
        schema = {"type": "object", "description": td.get("description", ""), "properties": schema_props}
        if required:
            schema["required"] = required

        endpoint = td["endpoint"]
        method = td.get("method", "POST")

        async def _handler(adapter_getter=_get_noop_adapter_getter, endpoint=endpoint, method=method, **params):
            adapter = adapter_getter()
            if not adapter:
                return "adapter 未就绪"
            account_id = params.pop("account_id", "default")
            client = (adapter._clients or {}).get(account_id)
            if not client:
                return f"账号 {account_id} 未连接"
            body = {k: v for k, v in params.items() if v is not None}
            try:
                resp = await client.call(endpoint, body, method=method)
                return json.dumps({"ok": resp.get("Success", False), "code": resp.get("Code"), "data": resp.get("Data")}, ensure_ascii=False, default=str)[:4000]
            except Exception as e:  # noqa: BLE001
                return f"调用失败: {e}"

        ctx.register_tool(
            name=td["name"],
            toolset="wechatpadpro",
            schema=schema,
            handler=_handler,
            is_async=True,
            description=td["description"],
            emoji="💬",
        )
        count += 1
    return count


def _get_noop_adapter_getter():
    return None


# ------------------------------------------------------------------ msg 域工具
def register_msg_tools(ctx, adapter_getter) -> None:
    """注册 msg 域工具（迁移自 msg-meta.ts）。adapter_getter() 返回当前 adapter。"""
    from .api_client import WppApiError

    def acct(adapter, params):
        aid = params.get("account_id") or "default"
        # Phase 4.3: 账号存在性 + 跨账号权限校验（账号互相隔离防线）
        if adapter is not None and hasattr(adapter, "authorize_account"):
            caller = params.get("callerWxid")
            allowed, reason = adapter.authorize_account(aid, caller)
            if not allowed:
                raise PermissionError(reason)
        return aid, _get_client(adapter, aid)

    # Phase 4.3: 跨账号上下文探查（只读，不含 authcode，符合 R1 铁律）
    async def accounts_context(**params):
        adapter = adapter_getter()
        if adapter is None:
            return {"error": "adapter 未就绪"}
        ctx = adapter.get_cross_account_context()
        return {"accounts": ctx, "count": len(ctx)}

    # sendText: 发送文本消息
    async def send_text(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        content = params["content"]
        ats = params.get("ats")
        aid, client = acct(adapter, params)
        resp = await client.send_text(to_wxid, content, ats)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        msg_id = data.get("Newmsgid") or br.get("NewMsgId") or data.get("Msgid")
        return {"ok": br.get("ret") == 0 or msg_id is not None, "msgId": msg_id}

    # sendImage: 发送图片（UploadImg 上传即发送）
    async def send_image(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        img = params.get("imgBase64") or params.get("imageUrl") or params.get("imgUrl") or ""
        aid, client = acct(adapter, params)
        # base64 处理（本地路径/URL 转 base64）
        base64_str = img
        if img.startswith("http") or img.startswith("/"):
            base64_str = await _resolve_to_base64(img)
        resp = await client.send_image(to_wxid, base64_str)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        msg_id = data.get("Newmsgid") or data.get("Msgid")
        return {"ok": br.get("ret") == 0 or msg_id is not None, "msgId": msg_id}

    # Phase 5.1: AI 图片生成 + 自动发送（mmx-cli 标准化）
    async def send_ai_image(**params):
        import base64 as _b64
        import os as _os
        import subprocess as _sp
        import time as _time
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        prompt = params["prompt"]
        aspect_ratio = params.get("aspect_ratio", "16:9")
        aid, client = acct(adapter, params)
        if client is None:
            return {"error": f"账号 {aid} 未连接"}
        # 1. 调 mmx CLI 生成图片到 /tmp（纯数据生成，vendor authcode 不参与）
        out_path = f"/tmp/mmx-ai-{aid}-{int(_time.time())}.png"
        try:
            proc = _sp.run(
                ["mmx", "image", "generate", "--prompt", prompt,
                 "--aspect-ratio", aspect_ratio, "--out", out_path, "--quiet"],
                capture_output=True, text=True, timeout=180,
            )
            if proc.returncode != 0:
                return {"error": f"mmx 图片生成失败: {(proc.stderr or proc.stdout).strip()[:300]}"}
        except FileNotFoundError:
            return {"error": "mmx CLI 不可用（未安装 /usr/bin/mmx）"}
        except _sp.TimeoutExpired:
            return {"error": "mmx 图片生成超时（180s）"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"mmx 图片生成异常: {e}"}
        # 2. 确认文件存在（--out 可能带后缀，兜底扫 /tmp 最新 mmx-ai 图）
        if not _os.path.isfile(out_path):
            import glob as _glob
            cands = sorted(_glob.glob(f"/tmp/mmx-ai-{aid}-*.png"), key=_os.path.getmtime, reverse=True)
            if not cands:
                return {"error": f"mmx 生成后文件不存在: {out_path}"}
            out_path = cands[0]
        # 3. 读文件转 base64 调 vendor 发送（vendor API 在 plugin 层）
        with open(out_path, "rb") as f:
            b64 = _b64.b64encode(f.read()).decode()
        resp = await client.send_image(to_wxid, b64)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        msg_id = data.get("Newmsgid") or data.get("Msgid")
        return {"ok": br.get("ret") == 0 or msg_id is not None, "msgId": msg_id,
                "image_path": out_path}

    # Phase 5.1: AI 语音合成 + 自动发送（mmx TTS → 发文件，微信语音走 SILK 专有格式）
    async def send_ai_voice(**params):
        import subprocess as _sp
        import time as _time
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        text = params["text"]
        voice = params.get("voice", "")
        aid, client = acct(adapter, params)
        if client is None:
            return {"error": f"账号 {aid} 未连接"}
        # 1. mmx 合成语音到 MP3
        out_path = f"/tmp/mmx-voice-{aid}-{int(_time.time())}.mp3"
        cmd = ["mmx", "speech", "synthesize", "--text", text, "--out", out_path, "--quiet"]
        if voice:
            cmd += ["--voice", voice]
        try:
            proc = _sp.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                return {"error": f"mmx 语音合成失败: {(proc.stderr or proc.stdout).strip()[:300]}"}
        except FileNotFoundError:
            return {"error": "mmx CLI 不可用（未安装 /usr/bin/mmx）"}
        except _sp.TimeoutExpired:
            return {"error": "mmx 语音合成超时（120s）"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"mmx 语音合成异常: {e}"}
        # 2. 发文件（微信语音消息是 SILK，MP3 走文件形式，用户点开系统播放器听）
        #    对齐 SOUL.md「音频走文件形式」约定
        import base64 as _b64
        import os as _os
        if not _os.path.isfile(out_path):
            return {"error": f"mmx 语音文件不存在: {out_path}"}
        with open(out_path, "rb") as f:
            b64 = _b64.b64encode(f.read()).decode()
        fname = f"语音_{int(_time.time())}.mp3"
        body = {"ToWxid": to_wxid, "FileName": fname, "Base64": b64}
        resp = await client.call("/Msg/UploadFile", body)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": br.get("ret") == 0, "msgId": data.get("Msgid"), "voice_path": out_path}

    # sendLocation: 发送定位卡片（/Msg/ShareLocation, Type 48）
    # 对齐原 wpp sendLocation: X=纬度, Y=经度, Label/Poiname/Scale/Infourl
    async def send_location(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        latitude = params.get("latitude", params.get("lat", 0))
        longitude = params.get("longitude", params.get("lng", 0))
        label = params.get("label", params.get("poi_name", ""))
        poi_name = params.get("poiName", params.get("poi_name", ""))
        aid, client = acct(adapter, params)
        resp = await client.share_location(to_wxid, latitude, longitude, label, poi_name)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        msg_id = data.get("Newmsgid") or br.get("NewMsgId") or data.get("Msgid")
        return {"ok": br.get("ret") == 0 or msg_id is not None, "msgId": msg_id}

    # publishImages: 发布图片朋友圈（1-9 张，带 guard 校验）
    async def publish_circle_images(**params):
        adapter = adapter_getter()
        title = params.get("title", params.get("content", ""))
        image_base64_list = params.get("imageBase64List", params.get("images", []))
        aid, client = acct(adapter, params)
        # guard：friendCirclePublishEnabled + 白名单（对齐 OpenClaw v1.3.41）
        acct_cfg = adapter.get_account(aid) if adapter else {}
        if not acct_cfg.get("friendCirclePublishEnabled"):
            return {"error": "朋友圈发布未启用 (friendCirclePublishEnabled=false)"}
        allow = acct_cfg.get("friendCirclePublishAllowFrom") or acct_cfg.get("adminUsers") or []
        caller = params.get("callerWxid") or ""
        if caller and allow and caller not in allow:
            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}
        resp = await client.publish_circle_images(title, image_base64_list)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": resp.get("Code") == 0 or br.get("ret") == 0, "msgId": data.get("Newmsgid"), "code": resp.get("Code")}

    # publishVideo: 发布视频朋友圈（带 guard）
    async def publish_circle_video(**params):
        adapter = adapter_getter()
        title = params.get("title", params.get("content", ""))
        video_base64 = params.get("videoBase64", params.get("video", ""))
        thumb_base64 = params.get("thumbBase64", params.get("thumb", ""))
        aid, client = acct(adapter, params)
        acct_cfg = adapter.get_account(aid) if adapter else {}
        if not acct_cfg.get("friendCirclePublishEnabled"):
            return {"error": "朋友圈发布未启用 (friendCirclePublishEnabled=false)"}
        allow = acct_cfg.get("friendCirclePublishAllowFrom") or acct_cfg.get("adminUsers") or []
        caller = params.get("callerWxid") or ""
        if caller and allow and caller not in allow:
            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}
        resp = await client.publish_circle_video(title, video_base64, thumb_base64)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": resp.get("Code") == 0 or br.get("ret") == 0, "msgId": data.get("Newmsgid"), "code": resp.get("Code")}

    # publishText: 发布文字朋友圈（带 guard）
    async def publish_circle_text(**params):
        adapter = adapter_getter()
        title = params.get("title", params.get("content", ""))
        aid, client = acct(adapter, params)
        acct_cfg = adapter.get_account(aid) if adapter else {}
        if not acct_cfg.get("friendCirclePublishEnabled"):
            return {"error": "朋友圈发布未启用 (friendCirclePublishEnabled=false)"}
        allow = acct_cfg.get("friendCirclePublishAllowFrom") or acct_cfg.get("adminUsers") or []
        caller = params.get("callerWxid") or ""
        if caller and allow and caller not in allow:
            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}
        resp = await client.publish_circle_text(title)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": resp.get("Code") == 0 or br.get("ret") == 0, "msgId": data.get("Newmsgid"), "code": resp.get("Code")}

    # Phase 5.2: AI 配图朋友圈（mmx 生成配图 → 自动发布朋友圈）
    async def publish_circle_ai(**params):
        import base64 as _b64
        import os as _os
        import subprocess as _sp
        import time as _time
        adapter = adapter_getter()
        title = params.get("title", params.get("content", ""))
        image_prompts = params.get("imagePrompts") or []
        single_prompt = params.get("prompt") or ""
        n = int(params.get("n", 1))
        aspect_ratio = params.get("aspect_ratio", "1:1")
        aid, client = acct(adapter, params)
        if client is None:
            return {"error": f"账号 {aid} 未连接"}
        # guard（对齐 publish_circle_images）
        acct_cfg = adapter.get_account(aid) if adapter else {}
        if not acct_cfg.get("friendCirclePublishEnabled"):
            return {"error": "朋友圈发布未启用 (friendCirclePublishEnabled=false)"}
        allow = acct_cfg.get("friendCirclePublishAllowFrom") or acct_cfg.get("adminUsers") or []
        caller = params.get("callerWxid") or ""
        if caller and allow and caller not in allow:
            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}
        # 确定配图描述列表（imagePrompts 数组优先；否则 prompt × n）
        prompts = list(image_prompts) if image_prompts else ([single_prompt] * n if single_prompt else [])
        if not prompts:
            return {"error": "缺少配图描述（传 imagePrompts 数组，或 prompt + n）"}
        if len(prompts) > 9:
            prompts = prompts[:9]  # 微信朋友圈最多 9 张图
        # mmx 生成每张配图 → base64 数组
        base64_list = []
        for i, p in enumerate(prompts):
            out_path = f"/tmp/mmx-fc-{aid}-{int(_time.time())}-{i}.png"
            try:
                proc = _sp.run(
                    ["mmx", "image", "generate", "--prompt", str(p),
                     "--aspect-ratio", aspect_ratio, "--out", out_path, "--quiet"],
                    capture_output=True, text=True, timeout=180,
                )
                if proc.returncode != 0:
                    return {"error": f"mmx 配图生成失败（第{i+1}张）: {(proc.stderr or proc.stdout).strip()[:200]}"}
            except FileNotFoundError:
                return {"error": "mmx CLI 不可用（未安装 /usr/bin/mmx）"}
            except Exception as e:  # noqa: BLE001
                return {"error": f"mmx 配图生成异常（第{i+1}张）: {e}"}
            if not _os.path.isfile(out_path):
                return {"error": f"mmx 配图文件不存在: {out_path}"}
            with open(out_path, "rb") as f:
                base64_list.append(_b64.b64encode(f.read()).decode())
        # 发布朋友圈
        resp = await client.publish_circle_images(title, base64_list)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": resp.get("Code") == 0 or br.get("ret") == 0,
                "msgId": data.get("Newmsgid"), "code": resp.get("Code"),
                "image_count": len(base64_list)}

    # sendEmoji: 发送 Emoji（md5）
    async def send_emoji(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        emoji_md5 = params.get("emojiMd5", params.get("emoji_md5", ""))
        emoji_size = params.get("emojiSize", params.get("emoji_size", 0))
        aid, client = acct(adapter, params)
        resp = await client.send_emoji(to_wxid, emoji_md5, emoji_size)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": br.get("ret") == 0, "msgId": data.get("Newmsgid")}

    # sendContactCard: 分享名片（/Msg/ShareCard）
    async def send_contact_card(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        card_wxid = params.get("cardWxid", "")
        card_nickname = params.get("cardNickname", "")
        card_alias = params.get("cardAlias", "")
        aid, client = acct(adapter, params)
        resp = await client.share_card(to_wxid, card_wxid, card_nickname, card_alias)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": br.get("ret") == 0, "msgId": data.get("Newmsgid")}

    # sendMiniProgram: 发送小程序（/Msg/SendXCX）
    async def send_mini_program(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        aid, client = acct(adapter, params)
        resp = await client.send_xcx(
            to_wxid,
            params.get("xcxTitle", params.get("title", "")),
            params.get("xcxDesc", params.get("desc", "")),
            params.get("xcxUrl", params.get("url", "")),
            params.get("xcxAppId", params.get("appId", "")),
            params.get("thumbUrl", ""),
        )
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": br.get("ret") == 0, "msgId": data.get("Newmsgid")}

    # sendLink: 分享链接（/Msg/ShareLink, type=5 appmsg）
    async def send_link(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        title = params.get("title", "")
        desc = params.get("desc", "")
        link_url = params.get("linkUrl", params.get("url", ""))
        thumb_url = params.get("thumbUrl", "")
        aid, client = acct(adapter, params)
        resp = await client.share_link(to_wxid, title, desc, link_url, thumb_url)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": br.get("ret") == 0, "msgId": data.get("Newmsgid")}

    # sendGroupMassMsgText: 群发文本到多个群（/Msg/SendGroupMassMsgText）
    async def send_group_mass_text(**params):
        adapter = adapter_getter()
        to_ids = params.get("toIds", params.get("to_ids", []))
        content = params.get("content", "")
        aid, client = acct(adapter, params)
        resp = await client.send_group_mass_text(to_ids, content)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": br.get("ret") == 0, "msgId": data.get("Newmsgid")}

    # sendFileV2: 发送文件（/Msg/SendFile, 自动上传）
    async def send_file_v2(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        file_name = params.get("fileName", "file")
        base64 = params.get("base64", "")
        aid, client = acct(adapter, params)
        resp = await client.send_file_v2(to_wxid, file_name, base64)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": br.get("ret") == 0, "msgId": data.get("Newmsgid")}

    # sendFile: 发送文件（下载 URL → UploadFile）
    async def send_file(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        file_url = params.get("fileUrl") or params.get("fileUrl2") or ""
        file_name = params.get("fileName", "file")
        aid, client = acct(adapter, params)
        # 下载文件 → base64 → UploadFile（安全：SSRF 防护 + 本地白名单目录）
        b64 = await _resolve_to_base64(file_url)
        body = {"ToWxid": to_wxid, "FileName": file_name, "Base64": b64}
        resp = await client.call("/Msg/UploadFile", body)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return {"ok": br.get("ret") == 0, "msgId": data.get("Msgid")}

    # sendVoice: 发送语音
    async def send_voice(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        voice = params.get("voiceUrl") or params.get("voiceBase64") or ""
        duration = params.get("duration", 0)
        aid, client = acct(adapter, params)
        # 简化：假设 voice 已是 base64/silk；非 base64 尝试下载（安全：SSRF 防护）
        base64_str = await _resolve_to_base64(voice) if (voice.startswith("http") or voice.startswith("/")) else voice
        body = {"ToWxid": to_wxid, "Base64": base64_str, "VoiceTime": int(duration)}
        resp = await client.call("/Msg/SendVoice", body)
        return {"ok": resp.get("Success", False), "resp": resp.get("Code")}

    # sendVideo: 发送视频
    async def send_video(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        video = params.get("videoUrl") or params.get("videoBase64") or ""
        thumb = params.get("thumbUrl", "")
        duration = params.get("videoDuration", 0)
        aid, client = acct(adapter, params)
        # 安全下载（SSRF 防护 + 本地白名单）
        base64_str = await _resolve_to_base64(video) if (video.startswith("http") or video.startswith("/")) else video
        thumb_b64 = ""
        if thumb.startswith("http") or thumb.startswith("/"):
            thumb_b64 = await _resolve_to_base64(thumb)
        body = {"ToWxid": to_wxid, "Base64": base64_str, "ImageBase64": thumb_b64, "PlayLength": int(duration)}
        resp = await client.call("/Msg/SendVideo", body)
        return {"ok": resp.get("Success", False), "resp": resp.get("Code")}

    # revokeMsg: 撤回消息
    async def revoke_msg(**params):
        adapter = adapter_getter()
        msg_id = params.get("msgId") or ""
        new_msg_id = params.get("newMsgId") or ""
        to_wxid = params.get("toWxid") or ""
        create_time = params.get("createTime") or 0
        aid, client = acct(adapter, params)
        body = {"MsgId": msg_id, "NewMsgId": new_msg_id, "ToWxid": to_wxid, "CreateTime": int(create_time)}
        resp = await client.call("/Msg/Revoke", body)
        return {"ok": resp.get("Success", False), "resp": resp.get("Code")}

    # quoteReply: 引用回复
    async def quote_reply(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        content = params["content"]
        aid, client = acct(adapter, params)
        # 简化引用：走 SendTxt（完整引用在阶段4 实现）
        resp = await client.send_text(to_wxid, content)
        return {"ok": resp.get("Success", False), "resp": resp.get("Code")}

    # syncMessage: 同步消息
    async def sync_message(**params):
        adapter = adapter_getter()
        aid, client = acct(adapter, params)
        resp = await client.sync_message()
        return {"ok": resp.get("Success", False), "count": len((resp.get("Data") or {}).get("messages") or [])}

    tools = [
        ("wpp_send_text", "发送文本消息到微信", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string", "description": "接收者 wxid（私聊用户或 @chatroom 群）"},
                "content": {"type": "string", "description": "消息内容，≥4000字自动切片"},
                "ats": {"type": "array", "items": {"type": "string"}, "description": "群聊 @ 的用户 wxid 列表"},
                "account_id": {"type": "string", "description": "账号ID（默认 default）"},
            },
            "required": ["toWxid", "content"],
        }, send_text),
        ("wpp_send_image", "发送图片到微信", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "imgBase64": {"type": "string", "description": "图片 base64 或 URL 或本地路径"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid"],
        }, send_image),
        ("wpp_send_location", "发送定位卡片到微信 (Type 48 位置消息)", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string", "description": "接收者 wxid（私聊用户或 @chatroom 群）"},
                "latitude": {"type": "number", "description": "纬度 lat"},
                "longitude": {"type": "number", "description": "经度 lng"},
                "label": {"type": "string", "description": "位置标签/店名"},
                "poiName": {"type": "string", "description": "POI 名称"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "latitude", "longitude"],
        }, send_location),
        ("wpp_fc_publish_images", "发布图片朋友圈 (1-9张，带权限校验)", {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "朋友圈文案"},
                "content": {"type": "string", "description": "朋友圈文案（别名）"},
                "imageBase64List": {"type": "array", "items": {"type": "string"}, "description": "图片 base64 数组 (1-9张)"},
                "images": {"type": "array", "items": {"type": "string"}, "description": "图片 base64 数组（别名）"},
                "callerWxid": {"type": "string", "description": "发起者 wxid（白名单校验）"},
                "account_id": {"type": "string"},
            },
            "required": ["title", "imageBase64List"],
        }, publish_circle_images),
        ("wpp_fc_publish_video", "发布视频朋友圈 (带权限校验)", {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "朋友圈文案"},
                "content": {"type": "string", "description": "朋友圈文案（别名）"},
                "videoBase64": {"type": "string", "description": "视频 base64"},
                "video": {"type": "string", "description": "视频 base64（别名）"},
                "thumbBase64": {"type": "string", "description": "缩略图 base64"},
                "thumb": {"type": "string", "description": "缩略图 base64（别名）"},
                "callerWxid": {"type": "string", "description": "发起者 wxid（白名单校验）"},
                "account_id": {"type": "string"},
            },
            "required": ["title", "videoBase64", "thumbBase64"],
        }, publish_circle_video),
        ("wpp_fc_publish_text", "发布文字朋友圈 (带权限校验)", {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "朋友圈文案"},
                "content": {"type": "string", "description": "朋友圈文案（别名）"},
                "callerWxid": {"type": "string", "description": "发起者 wxid（白名单校验）"},
                "account_id": {"type": "string"},
            },
            "required": ["title"],
        }, publish_circle_text),
        ("wpp_send_emoji", "发送 Emoji 表情到微信 (用 md5)", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "emojiMd5": {"type": "string", "description": "Emoji 的 md5"},
                "emojiSize": {"type": "integer"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "emojiMd5"],
        }, send_emoji),
        ("wpp_send_card", "发送微信名片到微信 (分享联系人)", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "cardWxid": {"type": "string", "description": "名片对象的 wxid"},
                "cardNickname": {"type": "string", "description": "名片昵称"},
                "cardAlias": {"type": "string"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "cardWxid"],
        }, send_contact_card),
        ("wpp_send_mini_program", "发送小程序消息到微信", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "title": {"type": "string"},
                "desc": {"type": "string"},
                "url": {"type": "string", "description": "小程序页面 URL"},
                "appId": {"type": "string"},
                "thumbUrl": {"type": "string"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "title", "url", "appId"],
        }, send_mini_program),
        ("wpp_send_link", "分享链接卡片到微信", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "title": {"type": "string"},
                "desc": {"type": "string"},
                "linkUrl": {"type": "string", "description": "链接 URL"},
                "thumbUrl": {"type": "string"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "title", "linkUrl"],
        }, send_link),
        ("wpp_group_mass_text", "群发文本到多个群", {
            "type": "object",
            "properties": {
                "toIds": {"type": "array", "items": {"type": "string"}, "description": "群 @chatroom id 数组"},
                "content": {"type": "string"},
                "account_id": {"type": "string"},
            },
            "required": ["toIds", "content"],
        }, send_group_mass_text),
        ("wpp_send_file_v2", "发送文件到微信 (自动上传)", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "fileName": {"type": "string", "description": "文件名含扩展名"},
                "base64": {"type": "string", "description": "文件内容 base64"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "fileName", "base64"],
        }, send_file_v2),
        ("wpp_send_file", "发送文件到微信", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "fileUrl": {"type": "string", "description": "文件可下载 URL"},
                "fileName": {"type": "string", "description": "文件名含扩展名"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "fileUrl"],
        }, send_file),
        ("wpp_send_voice", "发送语音到微信", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "voiceUrl": {"type": "string", "description": "语音 URL 或 base64"},
                "duration": {"type": "integer", "description": "时长毫秒"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid"],
        }, send_voice),
        ("wpp_send_video", "发送视频到微信", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "videoUrl": {"type": "string", "description": "视频 URL 或 base64"},
                "thumbUrl": {"type": "string"},
                "videoDuration": {"type": "integer"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid"],
        }, send_video),
        ("wpp_revoke_msg", "撤回微信消息", {
            "type": "object",
            "properties": {
                "msgId": {"type": "string"},
                "newMsgId": {"type": "string"},
                "toWxid": {"type": "string"},
                "createTime": {"type": "integer", "description": "发送返回的 server 创建时间"},
                "account_id": {"type": "string"},
            },
            "required": ["msgId", "toWxid"],
        }, revoke_msg),
        ("wpp_quote_reply", "引用回复微信消息", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "content": {"type": "string"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "content"],
        }, quote_reply),
        ("wpp_sync_message", "同步微信消息增量", {
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
        }, sync_message),
        ("wpp_accounts_context", "查询所有微信账号的上下文元信息（账号ID/绑定agent/selfWxid/连接状态/白名单），不含authcode。用于多账号场景下跨账号路由前探查", {
            "type": "object",
            "properties": {},
        }, accounts_context),
        ("wpp_send_ai_image", "AI 生成图片并自动发送到微信（mmx image-01 生成 → 自动 send_image）。一步完成「生成海报/配图 → 发微信」", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string", "description": "接收者 wxid（私聊用户或 @chatroom 群）"},
                "prompt": {"type": "string", "description": "图片描述（中文）"},
                "aspect_ratio": {"type": "string", "description": "比例 16:9/1:1/9:16，默认 16:9"},
                "account_id": {"type": "string", "description": "账号ID（默认 default）"},
            },
            "required": ["toWxid", "prompt"],
        }, send_ai_image),
        ("wpp_send_ai_voice", "AI 合成语音并自动发送到微信（mmx TTS → 自动发文件）。微信语音是 SILK，MP3 以文件形式发送", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string", "description": "接收者 wxid"},
                "text": {"type": "string", "description": "要合成的文本"},
                "voice": {"type": "string", "description": "音色（可选）"},
                "account_id": {"type": "string", "description": "账号ID（默认 default）"},
            },
            "required": ["toWxid", "text"],
        }, send_ai_voice),
        ("wpp_fc_publish_ai", "AI 配图并发布朋友圈（mmx 生成 1-9 张配图 → 自动发布图文朋友圈）。一步完成「写文案 + 自动配图 + 发朋友圈」", {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "朋友圈文案"},
                "content": {"type": "string", "description": "朋友圈文案（别名）"},
                "imagePrompts": {"type": "array", "items": {"type": "string"}, "description": "配图描述数组（每张图一个描述，最多9个）"},
                "prompt": {"type": "string", "description": "单个配图描述（配合 n 生成多张同主题图）"},
                "n": {"type": "integer", "description": "生成几张（默认1，仅当用 prompt 时生效）"},
                "aspect_ratio": {"type": "string", "description": "配图比例，默认 1:1（朋友圈推荐 1:1 或 3:4）"},
                "callerWxid": {"type": "string", "description": "发起者 wxid（白名单校验）"},
                "account_id": {"type": "string", "description": "账号ID（默认 default）"},
            },
            "required": ["title"],
        }, publish_circle_ai),
    ]

    for name, desc, schema, fn in tools:
        ctx.register_tool(
            name=name,
            toolset="wechatpadpro",
            # 2026-09-01: description 注入 schema 顶层（Hermes get_definitions 不合并 entry.description）
            schema={**schema, "description": desc},
            handler=_mk_handler(fn),
            is_async=True,
            description=desc,
            emoji="💬",
        )
    log.info("[WPP] 已注册 %d 个 msg 域工具", len(tools))
    return len(tools)


def register_mcp_tools(ctx) -> None:
    """注册 MCP 只读查询工具（迁移自 wpp mcp-meta.ts，用 MariaDB 查询）。"""
    async def mcp_list_contacts(**params):
        from . import db
        account_id = params.get("account_id") or "default"
        limit = params.get("limit") or 20
        offset = params.get("offset") or 0
        rows = db.list_contacts(account_id, limit, offset)
        return json.dumps(rows, ensure_ascii=False, default=str)[:4000]

    async def mcp_list_groups(**params):
        from . import db
        account_id = params.get("account_id") or "default"
        limit = params.get("limit") or 20
        rows = db.list_chatrooms(account_id, limit)
        return json.dumps(rows, ensure_ascii=False, default=str)[:4000]

    async def mcp_recent_messages(**params):
        from . import db
        account_id = params.get("account_id") or "default"
        limit = params.get("limit") or 50
        rows = db.list_messages(account_id, limit)
        return json.dumps(rows, ensure_ascii=False, default=str)[:4000]

    async def mcp_get_contact(**params):
        from . import db
        account_id = params.get("account_id") or "default"
        cid = params.get("id") or params.get("wxid") or ""
        row = db.query_contact_by_wxid(account_id, cid)
        if not row:
            return json.dumps({"found": False, "note": "联系人不存在于本地缓存"}, ensure_ascii=False)
        return json.dumps(row, ensure_ascii=False, default=str)

    async def mcp_get_group(**params):
        from . import db
        account_id = params.get("account_id") or "default"
        gid = params.get("id") or ""
        row = db.query_chatroom(account_id, gid)
        if not row:
            return json.dumps({"found": False, "note": "群不存在于本地缓存"}, ensure_ascii=False)
        return json.dumps(row, ensure_ascii=False, default=str)

    async def mcp_search(**params):
        from . import db
        account_id = params.get("account_id") or "default"
        keyword = params.get("keyword") or params.get("query") or ""
        if not keyword:
            return "请提供搜索关键词"
        rows = db.search_contacts(account_id, keyword)
        return json.dumps(rows, ensure_ascii=False, default=str)[:4000]

    tools = [
        ("wpp_mcp_list_contacts", "获取联系人列表（本地缓存）", {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "每页数量 1-100 默认20"},
                "offset": {"type": "integer", "description": "起始偏移"},
                "account_id": {"type": "string"},
            },
        }, mcp_list_contacts),
        ("wpp_mcp_list_groups", "获取群列表（本地缓存）", {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "account_id": {"type": "string"},
            },
        }, mcp_list_groups),
        ("wpp_mcp_recent_messages", "获取最近消息", {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "条数 1-500 默认50"},
                "account_id": {"type": "string"},
            },
        }, mcp_recent_messages),
        ("wpp_mcp_get_contact", "获取联系人详情", {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "联系人 wxid"},
                "account_id": {"type": "string"},
            },
            "required": ["id"],
        }, mcp_get_contact),
        ("wpp_mcp_get_group", "获取群详情", {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "群 @chatroom"},
                "account_id": {"type": "string"},
            },
            "required": ["id"],
        }, mcp_get_group),
        ("wpp_mcp_search", "搜索联系人或群", {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "account_id": {"type": "string"},
            },
            "required": ["keyword"],
        }, mcp_search),
    ]

    for name, desc, schema, fn in tools:
        ctx.register_tool(
            name=name, toolset="wechatpadpro", schema={**schema, "description": desc},
            handler=_mk_handler(fn), is_async=True, description=desc, emoji="🔍",
        )
    log.info("[WPP] 已注册 %d 个 MCP 工具", len(tools))


def register_jargon_tools(ctx) -> None:
    """注册 jargon 黑话查询工具。"""
    async def query_jargon(**params):
        from .jargon import query_jargon as qj, list_hot_jargon
        group_id = params.get("groupId") or ""
        term = params.get("term") or ""
        if term:
            r = qj(group_id, term)
            return json.dumps(r or {"found": False, "note": "未找到该词的黑话信息"}, ensure_ascii=False)
        hot = list_hot_jargon(group_id)
        return json.dumps(hot, ensure_ascii=False)

    _jargon_desc = "查询群黑话含义（自主学习统计的词频/上下文）。"
    ctx.register_tool(
        name="wpp_jargon_query",
        toolset="wechatpadpro",
        schema={
            "type": "object",
            "description": _jargon_desc,
            "properties": {
                "groupId": {"type": "string", "description": "群 @chatroom 标识"},
                "term": {"type": "string", "description": "要查询的词（不填则返回群热词TOP）"},
            },
            "required": ["groupId"],
        },
        handler=_mk_handler(query_jargon),
        is_async=True,
        description=_jargon_desc,
        emoji="🗣️",
    )
    log.info("[WPP] 已注册 jargon 工具")


# 本地文件读取白名单（安全：禁止读任意本地文件外泄，只允许 /tmp 和 OSS 缓存）
_LOCAL_READ_DIRS = ("/tmp/", "/var/tmp/", "/root/.hermes/profiles/wpp-wechat/workspace/", "/root/.hermes/plugins/wechatpadpro/")


def _is_local_read_allowed(path: str) -> bool:
    """本地路径只允许白名单目录（防读 /etc/passwd、/root/.ssh 外泄）。"""
    import os
    p = os.path.abspath(os.path.expanduser(path))
    return any(p.startswith(d) for d in _LOCAL_READ_DIRS)


async def _safe_fetch_url(url: str, timeout: int = 30, max_bytes: int = 200 * 1024 * 1024) -> bytes:
    """SSRF 防护的 URL 下载：拒绝 loopback/私网/link-local IP + 限大小。

    修复：send_file/send_voice/send_video 等工具曾无防护直接下载任意 URL，
    LLM 被 prompt injection 诱导可打 127.0.0.1 / 内网（SSRF）。DNS 解析后二次校验。
    """
    from urllib.parse import urlparse
    import ipaddress
    import socket

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"只允许 http/https URL: {url[:50]}")
    host = parsed.hostname or ""
    # 解析 DNS → 校验 IP（防 DNS rebinding / 内网域名）
    try:
        infos = socket.getaddrinfo(host, None)
        ips = {i[4][0] for i in infos}
    except socket.gaierror as e:
        raise ValueError(f"URL 域名无法解析: {host}") from e
    for ip in ips:
        try:
            ip_obj = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                or ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified):
            raise ValueError(f"SSRF 拦截: 拒绝访问内网/保留 IP {ip}")
    import aiohttp
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            data = await resp.read()
            if len(data) > max_bytes:
                raise ValueError(f"下载内容过大 ({len(data)} bytes > {max_bytes})")
            return data


async def _resolve_to_base64(ref: str) -> str:
    """URL/本地路径 → base64（安全版：SSRF 防护 + 本地白名单目录）。"""
    import base64
    if ref.startswith("data:"):
        return ref.split(",", 1)[1] if "," in ref else ref
    if ref.startswith("http"):
        data = await _safe_fetch_url(ref)
        return base64.b64encode(data).decode()
    if ref.startswith("/"):
        if not _is_local_read_allowed(ref):
            raise ValueError(f"本地文件读取被拒（不在白名单目录）: {ref[:50]}")
        with open(ref, "rb") as f:
            return base64.b64encode(f.read()).decode()
    return ref


def register_tools(ctx) -> int:
    """Deferred discovery 入口：注册 wechatpadpro 平台全部工具（含 phoneerp/wecom/wpp-extras）。

    2026-08-31 老板拍板修复：wechatpadpro 是 deferred platform，gateway 启动时
    discovery 阶段只调 tools.py 的 register_tools(ctx)，**不调** adapter.register()。
    之前工具只写在 adapter.register() 里 → 一个都没注册 → model 只能用 terminal
    跑 CLI（打字循环的根因之一）。

    handler 通过 adapter._get_adapter() 延迟拿 adapter 实例（multiplex profile 隔离），
    discovery 阶段 adapter 尚未实例化也没关系——调用时已就绪。
    """
    import adapter as _adapter_mod
    from . import tools_data as _td
    from . import tools_data_extra as _tde

    count = 0
    get_adapter = _adapter_mod._get_adapter

    # 1) msg 域（sendText/sendImage/sendLocation/...）
    register_msg_tools(ctx, get_adapter)
    count += 1

    # 2) mcp 域（recentMessages/contacts/groups/...）
    register_mcp_tools(ctx)
    count += 1

    # 3) jargon 黑话
    register_jargon_tools(ctx)
    count += 1

    # 4) 通用工具（group/user/friend/search/label/translate/tools/tenpay/friendcircle/finder/oa/qwc/favorites/xw/wxapp/voice/webhook/sayhello/login/customized）
    for defs in (
        _td.GROUP_TOOLS, _td.USER_TOOLS, _td.FRIEND_TOOLS, _td.SEARCH_TOOLS,
        _td.LABEL_TOOLS, _td.TRANSLATE_TOOLS, _td.TOOLS_TOOLS, _td.TENPAY_TOOLS,
        _td.FRIENDCIRCLE_TOOLS, _td.FINDER_TOOLS, _td.OA_TOOLS, _td.QWC_TOOLS,
        _td.FAVORITES_TOOLS, _td.XIAOWEI_TOOLS, _td.WXAPP_TOOLS, _td.VOICE_TOOLS,
        _td.WEBHOOK_TOOLS, _td.SAYHELLO_TOOLS, _td.LOGIN_TOOLS, _td.CUSTOMIZED_TOOLS,
    ):
        count += _register_generic_tools(ctx, get_adapter, defs)

    # 4b) extra 域（OpenClaw agent-tools 缺失补全）
    for defs in (
        _tde.EXTRA_MSG_TOOLS, _tde.EXTRA_GROUP_TOOLS, _tde.EXTRA_USER_TOOLS,
        _tde.EXTRA_FRIEND_TOOLS, _tde.EXTRA_FRIENDCIRCLE_TOOLS, _tde.EXTRA_SEARCH_TOOLS,
        _tde.EXTRA_TENPAY_TOOLS, _tde.EXTRA_TOOLS_TOOLS,
        _tde.EXTRA_FINDER_TOOLS, _tde.EXTRA_OA_TOOLS,
        _tde.EXTRA_QW_TOOLS, _tde.EXTRA_XIAOWEI_TOOLS,
        _tde.EXTRA_WXAPP_TOOLS, _tde.EXTRA_VOICE_TOOLS, _tde.EXTRA_LOGIN_TOOLS,
    ):
        count += _register_generic_tools(ctx, get_adapter, defs)

    # 5) phoneerp 业务工具（销售/定位/串码）
    from . import phoneerp_tools as _pet
    count += _pet.register(ctx, lambda account_id="default": get_adapter())

    # 6) wecom 工具（客户/行为/日报）
    from . import wecom_tools as _wct
    count += _wct.register(ctx, lambda account_id="default": get_adapter())

    # 7) wpp-history / wpp-identity
    from . import wpp_extras_tools as _wpe
    count += _wpe.register(ctx, lambda account_id="default": get_adapter())

    return count
