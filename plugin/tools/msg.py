"""msg 域工具（``/Msg/*``）—— 从旧 ``plugin/tools.py`` **逐字搬来**。

## 为什么是逐字搬，而不是重写

``register_msg_tools`` 是 **660 行踩过坑的生产逻辑**：每个 vendor 端点的参数名、
``ret`` 码判定、mmx CLI 调用链、语音/视频的 base64 兜底分支……手抄一遍就是给
「抄错一个参数名」开后门，而抄错的后果**只在运行期暴露**（厂商多半回一个与真因
无关的错误码）。所以这里选择生成 + 比对，而不是重打字。

本文件由 ``tools/port_tools_domain.py`` 生成，``tools/verify_tools_port.py``
用 AST 逐函数与旧实现比对 —— **任何未登记在案的字面改动都会让它红。**

## 与旧实现的全部差异（就这两条）

1. 函数体内 ``from .api_client import WppApiError``
   → ``from ..core.api_client import WppApiError``（包结构变了）
2. 旧的**同文件模块级** ``_get_client`` / ``_mk_handler`` / ``_resolve_to_base64``
   现在由本文件头部提供别名 / 包装：前两个指向 :mod:`.registry`，第三个见下。

## ``_resolve_to_base64`` 为什么要包一层（别图省事直接引用）

新 ``platform.outbound.resolve_to_base64`` 失败时返回**空串**；旧实现同名函数
失败时 **raise**。msg 域 5 个调用点的历史契约是「要么拿到 base64，要么抛错」。
若直接引用返空串的那个版本，``send_file`` 会把 ``Base64=""`` 交给厂商，厂商回一个
**与真因无关**的错误码 —— 正是老板最恨的那类事故（"日志说成功 / 报错指向错的根因"，
参考出站语音那个 Code=-2 文案为 INVALID_CREDENTIAL 的坑）。

所以这里把「本该拿到内容却拿到空」重新抛成 ``WppApiError``，同时**保留**旧实现的
两条原样返回分支：空串入参、以及既非 URL 也非路径者（当作已经是 base64）。
唯一**有意**的偏离：``data:`` 无逗号的畸形输入，旧实现原样返回，这里抛错。
"""

from __future__ import annotations

import logging

from . import registry

log = logging.getLogger(__name__)

#: 别名 —— 为的是让下面 660 行函数体**一字不改**（见模块 docstring 差异 2）
_get_client = registry.get_client
_mk_handler = registry.mk_handler


async def _resolve_to_base64(ref: str) -> str:
    """旧契约的**抛错版**包装（别换成直接引用 outbound 那个，见模块 docstring）。"""
    # ★ 依赖延迟到调用期：`core/api_client.py` 顶层 `import aiohttp`，
    #   而 `register()` 会被 `plugin_validate` 在**隔离 probe 子进程**里整体跑一遍
    #   （那个解释器没有 aiohttp）⇒ 本模块的**模块级**导入必须只依赖标准库 +
    #   `registry`。旧的 `_resolve_to_base64` 也是这么在函数内 import 的。
    from ..core.api_client import WppApiError
    from ..platform import outbound

    ref = (ref or "").strip()
    if not ref:
        return ""  # 旧实现末支：空串原样返回，不抛（send_file 允许 fileUrl 缺省）
    if not (ref.startswith("data:") or ref.startswith("http") or ref.startswith("/")):
        return ref  # 旧实现末支：既非 URL 也非路径 ⇒ 当作已是 base64
    b64 = await outbound.resolve_to_base64(ref)
    if not b64:
        raise WppApiError(f"媒体解析失败（取不到内容，拒绝发送空媒体）: {ref[:60]}")
    return b64


def register_msg_tools(ctx, adapter_getter) -> None:
    """注册 msg 域工具（迁移自 msg-meta.ts）。adapter_getter() 返回当前 adapter。"""
    from ..core.api_client import WppApiError

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
