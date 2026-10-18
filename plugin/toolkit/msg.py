"""msg 域工具（``/Msg/*``）—— 从旧 ``plugin/tools.py`` **逐字搬来**。

## 为什么是逐字搬，而不是重写

``register_msg_tools`` 是 **660 行踩过坑的生产逻辑**：每个 vendor 端点的参数名、
``ret`` 码判定、mmx CLI 调用链、语音/视频的 base64 兜底分支……手抄一遍就是给
「抄错一个参数名」开后门，而抄错的后果**只在运行期暴露**（厂商多半回一个与真因
无关的错误码）。所以这里选择生成 + 比对，而不是重打字。

本文件由 ``tools/port_tools_domain.py`` 生成，``tools/verify_tools_port.py``
用 AST 逐函数与旧实现比对 —— **任何未登记在案的字面改动都会让它红。**

## 与旧实现的全部差异（就这三条）

1. 函数体内 ``from .api_client import WppApiError``
   → ``from ..core.api_client import WppApiError``（包结构变了）
2. 旧的**同文件模块级** ``_get_client`` / ``_mk_handler`` / ``_resolve_to_base64``
   现在由本文件头部提供别名 / 包装：前两个指向 :mod:`.registry`，第三个见下。
3. **W7-5（2026-10-02）**：四处朋友圈守卫的"开关"那半收口到 :mod:`.policy` 的运行期
   门禁（头部因此多一个 ``policy`` 导入）。旧实现在这四处调 ``adapter.get_account(aid)``
   —— **适配器上没有这个方法**（全树只在旧原文这 4 行出现过）⇒ 真机必然
   ``AttributeError`` ⇒ 回话是「调用失败: …」而不是那句"未启用"，且**白名单那一支
   永远走不到**（等于白名单形同没有）。⇒ 这是**第二处"改了语义"的有意偏离**，
   详见 :data:`REWRITES` 里 ``msg.py`` 那条与 ``plugin/toolkit/policy.py`` 的
   :data:`FRIEND_CIRCLE_PUBLISH_GATE`。

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
import time

from . import policy, registry

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


def _generate_image_via_hermes(prompt: str, aspect_ratio: str) -> dict:
    """生图走 Hermes ``image_gen`` 注册表（百炼 qwen-image / wan / z-image）。

    旧实现 fork ``mmx``（MiniMax image-01），与 Hermes 的模型供应商体系完全脱节：
    即使 ``image_gen.provider`` 已经指向百炼，WPP 工具仍会用 MiniMax。这里改为
    调用当前激活的 provider，图片落到 ``$HERMES_HOME/cache/generated/images/``。
    """
    try:
        # 延迟导入：``register()`` 会在隔离 probe 子进程里整体跑一遍，那个解释器
        # 没有 Hermes 的 ``agent`` 包；模块级导入只能依赖标准库 + ``registry``。
        from agent.image_gen_registry import get_active_provider
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"Hermes 生图后端加载失败: {exc}"}
    provider = get_active_provider()
    if provider is None:
        return {"success": False, "error": "未配置生图后端（image_gen.provider）"}
    try:
        return provider.generate(prompt=prompt, aspect_ratio=aspect_ratio)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"{getattr(provider, 'name', 'image_gen')} 生图异常: {exc}"}


def _localize_generated_image(image: str, prefix: str) -> str:
    """把 provider 返回的 URL 兜底下载到 /tmp（缓存失败时 URL 24h 后会过期）。"""
    if not image or not image.startswith(("http://", "https://")):
        return image
    import os as _os
    import shutil as _shutil
    import time as _time
    import urllib.request as _urlreq

    path = f"/tmp/{prefix}-{_os.getpid()}-{int(_time.time() * 1000)}.png"
    with _urlreq.urlopen(image, timeout=60) as src, open(path, "wb") as dst:
        _shutil.copyfileobj(src, dst)
    return path


def _persist_tool_outbound(account_id: str, peer_id: str, content: str, resp,
                           msg_type: str = "text") -> None:
    """落库工具路径的出站记录（``wpp_send_text`` / ``wpp_send_image``）。

    live ``adapter.send()`` 与 cron ``_standalone_send`` 都会落库；工具直发
    原先只有 ``quote_reply`` 落库 ⇒ Stage2 这种"agent 调工具发群"的消息在
    ``wpp_messages`` 里完全隐形。失败只 warn，不影响已完成的发送。
    """
    try:
        from ..data import repo_messages
        from ..inbound.persist import db_chat_id, db_peer_kind
        from ..platform import outbound

        resp_ids = outbound.extract_outbound_msg_ids(resp)
        is_group = outbound.is_group_peer(peer_id)
        repo_messages.save_message(
            account_id,
            direction="outbound",
            peer_kind=db_peer_kind("group" if is_group else "dm"),
            peer_id=peer_id,
            chat_id=db_chat_id(peer_id, is_group=is_group),
            msg_id=resp_ids.new_msg_id or resp_ids.msg_id or None,
            new_msg_id=resp_ids.new_msg_id,
            content=content,
            msg_type=msg_type,
            ts=int(time.time()),
        )
        try:
            from ..core import metrics

            metrics.inc_outbound_persist(True)
        except Exception:  # noqa: BLE001 —— 埋点失败不影响落库结论
            pass
    except Exception as exc:  # noqa: BLE001
        try:
            from ..core import metrics

            metrics.inc_outbound_persist(False)
        except Exception:  # noqa: BLE001 —— 埋点失败绝不能盖住原始错误
            pass
        log.warning("[WPP:%s] 工具出站落库失败: %s", account_id, exc)


def _send_payload(resp, *, msg_id=None, **extra) -> dict:
    """发送类工具的统一返回 —— **成败判据只有一处**：``platform.outbound.send_ok``。

    ⚠️ 旧实现各自判 ``BaseResponse.ret == 0``（或 ``Code == 0``），**不看**
    ``Data.List[].Ret``：厂商回 ``Code=0`` + ``BaseResponse.ret=0``、但逐条结果是
    ``Ret=-2``（被拒收）时，工具报 ``ok:true`` —— 模型以为已经发出去，群里什么也没有
    （v1.5.6 ``SEND-LIST-RET`` 事故的同型复发：适配器侧早就改用 ``send_ok``，
    工具面 15 处漏了）。判据不许在调用点另写一份。
    """
    from ..platform import outbound

    ids = outbound.extract_outbound_msg_ids(resp)
    ok = outbound.send_ok(resp)
    payload: dict = {"ok": ok, "msgId": msg_id or ids.msg_id}
    if ids.new_msg_id:
        payload["newMsgId"] = ids.new_msg_id
    if not ok:
        payload["error"] = outbound.send_error_text(resp)
    payload.update(extra)
    return payload


def register_msg_tools(ctx, adapter_getter) -> None:
    """注册 msg 域工具（迁移自 msg-meta.ts）。adapter_getter() 返回当前 adapter。"""
    from ..core.api_client import WppApiError

    def acct(adapter, params):
        # ``default`` 是 schema 的**占位哨兵**，不是真账号名：单账号部署里账号文件
        # 叫 ``boss2`` 时，按字面量 ``default`` 去查存在性会被判"账号不存在" ⇒ 所有
        # /Msg 工具全拒。先解析成**已连接的那个**（仅单账号 + 请求的就是 default 时回退）。
        aid = registry.resolve_account_id(adapter, params.get("account_id"))
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
        # ★ 2026-10-18：工具路径也必须分片。微信个人号超过约 2000 字符会被
        #   静默截断；agent 直接用 ``wpp_send_text`` 发长文时同样会丢后半段。
        #   ``@`` 只挂在第一片，后续补发不重复打扰。
        from ..platform import outbound
        chunks = outbound.chunk_text(content)
        payload: dict = {}
        sent = 0
        for idx, chunk in enumerate(chunks):
            resp = await client.send_text(to_wxid, chunk, ats if idx == 0 else None)
            data = resp.get("Data") or {}
            br = data.get("BaseResponse") or {}
            msg_id = data.get("Newmsgid") or br.get("NewMsgId") or data.get("Msgid")
            payload = _send_payload(resp, msg_id=msg_id)
            if not payload.get("ok"):
                break
            sent += 1
            _persist_tool_outbound(aid, to_wxid, chunk, resp)
        if len(chunks) > 1:
            payload["chunked"] = True
            payload["chunks"] = len(chunks)
            payload["sentChunks"] = sent
        return payload

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
        payload = _send_payload(resp, msg_id=msg_id)
        if payload.get("ok"):
            _persist_tool_outbound(aid, to_wxid, "[图片]", resp, msg_type="image")
        return payload

    # Phase 5.1: AI 图片生成 + 自动发送（Hermes image_gen 注册表，百炼 qwen-image）
    async def send_ai_image(**params):
        import asyncio as _aio
        import base64 as _b64
        import os as _os
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        prompt = params["prompt"]
        aspect_ratio = params.get("aspect_ratio", "16:9")
        aid, client = acct(adapter, params)
        if client is None:
            return {"error": f"账号 {aid} 未连接"}
        # 1. 走 Hermes image_gen 注册表（默认百炼 qwen-image），不再 fork mmx
        result = await _aio.to_thread(_generate_image_via_hermes, prompt, aspect_ratio)
        if not result.get("success"):
            return {"error": result.get("error") or "图片生成失败"}
        # 2. provider 已落盘；缓存失败时返回的 URL 兜底下载（URL 24h 过期）
        try:
            out_path = _localize_generated_image(str(result.get("image") or ""), "wpp-ai-image")
        except Exception as e:  # noqa: BLE001
            return {"error": f"生图结果下载失败: {e}"}
        if not out_path or not _os.path.isfile(out_path):
            return {"error": f"生图后文件不存在: {out_path}"}
        # 3. 读文件转 base64 调 vendor 发送（vendor API 在 plugin 层）
        with open(out_path, "rb") as f:
            b64 = _b64.b64encode(f.read()).decode()
        resp = await client.send_image(to_wxid, b64)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        msg_id = data.get("Newmsgid") or data.get("Msgid")
        payload = _send_payload(resp, msg_id=msg_id, image_path=out_path)
        if payload.get("ok"):
            _persist_tool_outbound(aid, to_wxid, "[图片]", resp, msg_type="image")
        return payload

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
        # 文件消息走 UploadFile + ShareLink Type=6（旧 /Msg/UploadFile 端点不存在）
        resp = await client.send_file_via_app(to_wxid, fname, b64)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        payload = _send_payload(resp, msg_id=data.get("Msgid"), voice_path=out_path)
        if payload.get("ok"):
            _persist_tool_outbound(aid, to_wxid, f"[文件] {fname}", resp, msg_type="file")
        return payload

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
        payload = _send_payload(resp, msg_id=msg_id)
        if payload.get("ok"):
            _persist_tool_outbound(aid, to_wxid, f"[位置] {label or poi_name}".strip(),
                                   resp, msg_type="location")
        return payload

    # publishImages: 发布图片朋友圈（1-9 张，带 guard 校验）
    async def publish_circle_images(**params):
        adapter = adapter_getter()
        title = params.get("title", params.get("content", ""))
        image_base64_list = params.get("imageBase64List", params.get("images", []))
        aid, client = acct(adapter, params)
        # guard：friendCirclePublishEnabled + 白名单（对齐 OpenClaw v1.3.41）
        # guard：``friendCirclePublishEnabled`` 那条**开关**由 policy 的运行期门禁把关
        #   （所有调用路径都过 ``hermes_contract.wrap_ctx``，收口到 tools/policy.py）。
        #   这里只剩**按调用方**的白名单判定。
        #   ⚠️ 本行原先是 ``adapter.get_account(aid)`` —— 适配器上**没有这个方法**
        #   （全树只在这 4 行出现过）⇒ 真机必然 AttributeError，回话是"调用失败: …"而
        #   不是那句"未启用"，且白名单这一支**永远走不到**。2026-10-02 修。
        acct_cfg = policy.raw_account_cfg(adapter, aid)
        if acct_cfg is None:
            return {"error": "读不到账号配置，拒绝发布朋友圈"}
        allow = acct_cfg.get("friendCirclePublishAllowFrom") or acct_cfg.get("adminUsers") or []
        caller = params.get("callerWxid") or ""
        if caller and allow and caller not in allow:
            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}
        resp = await client.publish_circle_images(title, image_base64_list)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return _send_payload(resp, msg_id=data.get("Newmsgid"), code=resp.get("Code"))

    # publishVideo: 发布视频朋友圈（带 guard）
    async def publish_circle_video(**params):
        adapter = adapter_getter()
        title = params.get("title", params.get("content", ""))
        video_base64 = params.get("videoBase64", params.get("video", ""))
        thumb_base64 = params.get("thumbBase64", params.get("thumb", ""))
        aid, client = acct(adapter, params)
        # guard：``friendCirclePublishEnabled`` 那条**开关**由 policy 的运行期门禁把关
        #   （所有调用路径都过 ``hermes_contract.wrap_ctx``，收口到 tools/policy.py）。
        #   这里只剩**按调用方**的白名单判定。
        #   ⚠️ 本行原先是 ``adapter.get_account(aid)`` —— 适配器上**没有这个方法**
        #   （全树只在这 4 行出现过）⇒ 真机必然 AttributeError，回话是"调用失败: …"而
        #   不是那句"未启用"，且白名单这一支**永远走不到**。2026-10-02 修。
        acct_cfg = policy.raw_account_cfg(adapter, aid)
        if acct_cfg is None:
            return {"error": "读不到账号配置，拒绝发布朋友圈"}
        allow = acct_cfg.get("friendCirclePublishAllowFrom") or acct_cfg.get("adminUsers") or []
        caller = params.get("callerWxid") or ""
        if caller and allow and caller not in allow:
            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}
        resp = await client.publish_circle_video(title, video_base64, thumb_base64)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return _send_payload(resp, msg_id=data.get("Newmsgid"), code=resp.get("Code"))

    # publishText: 发布文字朋友圈（带 guard）
    async def publish_circle_text(**params):
        adapter = adapter_getter()
        title = params.get("title", params.get("content", ""))
        aid, client = acct(adapter, params)
        # guard：``friendCirclePublishEnabled`` 那条**开关**由 policy 的运行期门禁把关
        #   （所有调用路径都过 ``hermes_contract.wrap_ctx``，收口到 tools/policy.py）。
        #   这里只剩**按调用方**的白名单判定。
        #   ⚠️ 本行原先是 ``adapter.get_account(aid)`` —— 适配器上**没有这个方法**
        #   （全树只在这 4 行出现过）⇒ 真机必然 AttributeError，回话是"调用失败: …"而
        #   不是那句"未启用"，且白名单这一支**永远走不到**。2026-10-02 修。
        acct_cfg = policy.raw_account_cfg(adapter, aid)
        if acct_cfg is None:
            return {"error": "读不到账号配置，拒绝发布朋友圈"}
        allow = acct_cfg.get("friendCirclePublishAllowFrom") or acct_cfg.get("adminUsers") or []
        caller = params.get("callerWxid") or ""
        if caller and allow and caller not in allow:
            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}
        resp = await client.publish_circle_text(title)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return _send_payload(resp, msg_id=data.get("Newmsgid"), code=resp.get("Code"))

    # Phase 5.2: AI 配图朋友圈（Hermes image_gen 注册表生成配图 → 自动发布朋友圈）
    async def publish_circle_ai(**params):
        import asyncio as _aio
        import base64 as _b64
        import os as _os
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
        # guard：``friendCirclePublishEnabled`` 那条**开关**由 policy 的运行期门禁把关
        #   （所有调用路径都过 ``hermes_contract.wrap_ctx``，收口到 tools/policy.py）。
        #   这里只剩**按调用方**的白名单判定。
        #   ⚠️ 本行原先是 ``adapter.get_account(aid)`` —— 适配器上**没有这个方法**
        #   （全树只在这 4 行出现过）⇒ 真机必然 AttributeError，回话是"调用失败: …"而
        #   不是那句"未启用"，且白名单这一支**永远走不到**。2026-10-02 修。
        acct_cfg = policy.raw_account_cfg(adapter, aid)
        if acct_cfg is None:
            return {"error": "读不到账号配置，拒绝发布朋友圈"}
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
        # Hermes image_gen 生成每张配图 → base64 数组
        base64_list = []
        for i, p in enumerate(prompts):
            result = await _aio.to_thread(_generate_image_via_hermes, str(p), aspect_ratio)
            if not result.get("success"):
                return {"error": f"配图生成失败（第{i+1}张）: {result.get('error') or '未知错误'}"}
            try:
                out_path = _localize_generated_image(str(result.get("image") or ""), "wpp-fc-image")
            except Exception as e:  # noqa: BLE001
                return {"error": f"配图下载失败（第{i+1}张）: {e}"}
            if not out_path or not _os.path.isfile(out_path):
                return {"error": f"配图文件不存在（第{i+1}张）: {out_path}"}
            with open(out_path, "rb") as f:
                base64_list.append(_b64.b64encode(f.read()).decode())
        # 发布朋友圈
        resp = await client.publish_circle_images(title, base64_list)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        return _send_payload(resp, msg_id=data.get("Newmsgid"), code=resp.get("Code"),
                             image_count=len(base64_list))

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
        payload = _send_payload(resp, msg_id=data.get("Newmsgid"))
        if payload.get("ok"):
            _persist_tool_outbound(aid, to_wxid, f"[表情] {emoji_md5}".strip(),
                                   resp, msg_type="emoji")
        return payload

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
        payload = _send_payload(resp, msg_id=data.get("Newmsgid"))
        if payload.get("ok"):
            _persist_tool_outbound(aid, to_wxid, f"[名片] {card_nickname}".strip(),
                                   resp, msg_type="card")
        return payload

    # sendMiniProgram: 发送小程序（/Msg/SendXCX）
    async def send_mini_program(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        aid, client = acct(adapter, params)
        title = params.get("xcxTitle", params.get("title", ""))
        page_path = params.get("pagePath", "")
        resp = await client.send_xcx(
            to_wxid,
            title,
            params.get("xcxDesc", params.get("desc", "")),
            params.get("xcxUrl", params.get("url", "")),
            params.get("xcxAppId", params.get("appId", "")),
            params.get("thumbUrl", ""),
            page_path,
            params.get("username", params.get("xcxUsername", "")),
        )
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        payload = _send_payload(resp, msg_id=data.get("Newmsgid"))
        if payload.get("ok"):
            _persist_tool_outbound(
                aid, to_wxid,
                f"[小程序] {title}".strip() + (f" (页面 {page_path})" if page_path else ""),
                resp, msg_type="miniprogram")
        return payload

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
        payload = _send_payload(resp, msg_id=data.get("Newmsgid"))
        if payload.get("ok"):
            _persist_tool_outbound(aid, to_wxid, f"[链接] {title}".strip(),
                                   resp, msg_type="link")
        return payload

    # sendGroupMassMsgText: 群发文本到多个群（/Msg/SendGroupMassMsgText）
    async def send_group_mass_text(**params):
        adapter = adapter_getter()
        to_ids = params.get("toIds", params.get("to_ids", []))
        content = params.get("content", "")
        aid, client = acct(adapter, params)
        resp = await client.send_group_mass_text(to_ids, content)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        payload = _send_payload(resp, msg_id=data.get("Newmsgid"))
        if payload.get("ok"):
            # 群发是 N 个目标共用一次响应；逐目标落库（同一 msgId 在不同 peer 下唯一）。
            targets = ([to_ids] if isinstance(to_ids, str)
                       else (to_ids or []))
            for to_id in targets:
                _persist_tool_outbound(aid, str(to_id), content, resp)
        return payload

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
        payload = _send_payload(resp, msg_id=data.get("Newmsgid"))
        if payload.get("ok"):
            _persist_tool_outbound(aid, to_wxid, f"[文件] {file_name}", resp, msg_type="file")
        return payload

    # sendFile: 发送文件（下载 URL → UploadFile）
    async def send_file(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        file_url = params.get("fileUrl") or params.get("fileUrl2") or ""
        file_name = params.get("fileName", "file")
        aid, client = acct(adapter, params)
        # 下载文件 → base64 → UploadFile + ShareLink Type=6（安全：SSRF 防护 + 本地白名单）
        b64 = await _resolve_to_base64(file_url)
        resp = await client.send_file_via_app(to_wxid, file_name, b64)
        data = resp.get("Data") or {}
        br = data.get("BaseResponse") or {}
        payload = _send_payload(resp, msg_id=data.get("Msgid"))
        if payload.get("ok"):
            _persist_tool_outbound(aid, to_wxid, f"[文件] {file_name}", resp, msg_type="file")
        return payload

    # sendVoice: 发送语音
    async def send_voice(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        voice = params.get("voiceUrl") or params.get("voiceBase64") or ""
        duration = params.get("duration", 0)
        aid, client = acct(adapter, params)
        if not voice:
            return {"error": "voiceUrl/voiceBase64 至少给一个（厂商不收空语音）"}
        # 非 base64 尝试下载（安全：SSRF 防护）
        base64_str = await _resolve_to_base64(voice) if (voice.startswith("http") or voice.startswith("/")) else voice
        # 厂商 /Msg/SendVoice **只收 SILK**（Type=4）。mp3 等其它格式没有转码器时
        # 降级成文件消息（TS v1.3.53 VOICE-DEGRADE 同口径），绝不把 mp3 当语音发。
        no_query = voice.split("?")[0].lower()
        is_silk = (voice.startswith("data:audio/silk") or no_query.endswith(".silk")
                   or params.get("formatHint") == "silk")
        if not is_silk:
            fname = voice.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "voice.mp3"
            resp = await client.send_file_via_app(to_wxid, fname, base64_str)
            data = resp.get("Data") or {}
            br = data.get("BaseResponse") or {}
            payload = _send_payload(resp, msg_id=data.get("Msgid"), degraded="file")
            if payload.get("ok"):
                _persist_tool_outbound(aid, to_wxid, f"[文件] {fname}", resp, msg_type="file")
            return payload
        resp = await client.send_voice(to_wxid, base64_str, 4, int(duration))
        payload = _send_payload(resp, code=resp.get("Code"))
        if payload.get("ok"):
            _persist_tool_outbound(aid, to_wxid, "[语音]", resp, msg_type="voice")
        return payload

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
        payload = _send_payload(resp, code=resp.get("Code"))
        if payload.get("ok"):
            _persist_tool_outbound(aid, to_wxid, "[视频]", resp, msg_type="video")
        return payload

    # revokeMsg: 撤回消息
    async def revoke_msg(**params):
        adapter = adapter_getter()
        msg_id = params.get("msgId") or ""
        new_msg_id = params.get("newMsgId") or ""
        to_wxid = params.get("toWxid") or ""
        create_time = params.get("createTime") or 0
        aid, client = acct(adapter, params)
        resp = await client.revoke_msg(msg_id, new_msg_id, int(create_time), to_wxid)
        return _send_payload(resp, code=resp.get("Code"))

    # quoteReply: 引用回复（对齐 TS src/send/quote-reply.ts）
    async def quote_reply(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        content = params["content"]
        msg_id = str(params.get("msgId") or "")
        new_msg_id = str(params.get("newMsgId") or "")
        aid, client = acct(adapter, params)
        if not msg_id and not new_msg_id:
            return {"ok": False, "error": "msgId/newMsgId 至少填一个（被引用消息的 id）"}
        # ★ 2026-10-18：原实现是假引用 —— 只发了一条普通 SendTxt，既没有 type=57
        #   refermsg，也不会 @ 被回复人。这里对齐 TS 的主路径：
        #   ① 回查被引用消息（**不限方向**：被引用的可能是 bot 自己的回复）；
        #   ② 群聊补 ``@昵称 `` 前缀（唯一产出点 inbound.quote_ref.build_mention_prefix）；
        #   ③ 生成 type=57 XML，走 ``/Msg/ShareLink {ToWxid,Type:5,Xml}``。
        from ..data import repo_messages
        from ..inbound import quote_ref
        from ..platform import outbound

        svrid = new_msg_id or msg_id
        ident = quote_ref.QuoteIdentity()
        try:
            row = repo_messages.get_message_by_id_or_new_id(
                aid, msg_id=msg_id or None, new_msg_id=new_msg_id or None)
            ident = quote_ref.resolve_quote_identity(row)
        except Exception as exc:  # noqa: BLE001 —— 查库失败只降级，不挡发送
            log.debug("[WPP:%s] quote_reply 回查降级 %s: %s", aid, svrid, exc)
        name = ident.display_name
        if ident.fromusr and (not name or name == ident.fromusr):
            try:
                name = adapter._contact_name(aid, ident.fromusr) if adapter is not None else ""
            except Exception:  # noqa: BLE001 —— 昵称只是装饰
                name = ""
        prefix = quote_ref.build_mention_prefix(to_wxid, name, ident.fromusr)
        quote_text = f"{prefix}{content}".strip()
        peer_kind = "group" if to_wxid.endswith("@chatroom") else "direct"

        def _persist_sent(msg_ids, text: str, msg_type: str) -> None:
            # 任意渠道发送都要入库（TS v1.3.16 OUTBOUND-PERSIST）：否则之后"引用 bot
            # 这条回复"会查不到锚点。入库失败只留痕，不把发送结果翻成失败。
            try:
                repo_messages.save_message(
                    aid, direction="outbound", peer_kind=peer_kind,
                    peer_id=to_wxid, msg_id=msg_ids.msg_id,
                    new_msg_id=msg_ids.new_msg_id, msg_type=msg_type,
                    content=text)
            except Exception as exc:  # noqa: BLE001 —— 入库是附带动作
                log.warning("[WPP:%s] quote_reply 入库失败（不影响发送）: %s", aid, exc)

        if not outbound.quote_reply_fits(quote_text):
            # ★ 2026-10-18：引用气泡只渲染 500 字（vendor 侧同样 slice）⇒ 长正文走
            #   ShareLink 会被静默砍半。超限改发普通文本，按平台上限分段；宁可多发
            #   几条，也不让调用方拿到"成功"却只发出去半句话。
            log.info("[WPP:%s] quote_reply 正文 %d 字超引用气泡上限 %d ⇒ 改发普通文本",
                     aid, len(quote_text), outbound.QUOTE_REPLY_MAX_CHARS)
            ok = True
            ids = None
            for ch in outbound.chunk_text(quote_text):
                resp = await client.send_text(to_wxid=to_wxid, content=ch)
                chunk_ok = outbound.send_ok(resp)
                ok = ok and chunk_ok
                ids = outbound.extract_outbound_msg_ids(resp)
                if chunk_ok:
                    _persist_sent(ids, ch, "text")
            return {"ok": ok,
                    "msgId": getattr(ids, "msg_id", None),
                    "newMsgId": getattr(ids, "new_msg_id", None),
                    "chunked": True}

        xml = outbound.build_quote_reply_xml(
            reply_content=quote_text, svrid=svrid, fromusr=ident.fromusr)
        resp = await client.call("/Msg/ShareLink",
                                 {"ToWxid": to_wxid, "Type": 5, "Xml": xml})
        ok = outbound.send_ok(resp)
        ids = outbound.extract_outbound_msg_ids(resp)
        if ok:
            _persist_sent(ids, content, "quote")
        return {"ok": ok, "msgId": ids.msg_id, "newMsgId": ids.new_msg_id}

    # syncMessage: 同步消息
    async def sync_message(**params):
        adapter = adapter_getter()
        aid, client = acct(adapter, params)
        resp = await client.sync_message()
        # 读端点：Code 缺失属正常形状（见 api_client._request），别用 Success 单独定成败
        return {"ok": resp.get("Success") is not False and resp.get("Code") in (0, 200, None),
                "count": len((resp.get("Data") or {}).get("messages") or [])}

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
            # TS ``uploadImage`` 要求 imgBase64；漏了它只会得到“拿不到内容”的运行时错误
            "required": ["toWxid", "imgBase64"],
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
                "pagePath": {"type": "string",
                             "description": "小程序内部页面路径（如 pages/index/index?x=1）；给了才发可直达页面的现代卡片"},
                "username": {"type": "string",
                             "description": "小程序 username（gh_xxx@app），现代卡片定位小程序用"},
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
            "required": ["toWxid", "fileUrl", "fileName"],
        }, send_file),
        ("wpp_send_voice", "发送语音到微信", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "voiceUrl": {"type": "string", "description": "语音 URL 或 base64"},
                "duration": {"type": "integer", "description": "时长毫秒"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "voiceUrl"],
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
            "required": ["toWxid", "videoUrl"],
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
        ("wpp_quote_reply", "引用回复微信消息. 自动从 DB 取被引用消息构造完整引用卡片（文本/图片通用），群聊自动 @ 被回复人.", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string", "description": "目标 wxid / 群 id"},
                "content": {"type": "string", "description": "回复内容"},
                "msgId": {"type": "string", "description": "被引用消息的 msgId（DB 回查用）"},
                "newMsgId": {"type": "string", "description": "被引用消息的 newMsgId（可选，svrid 优先用它）"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "content", "msgId"],
        }, quote_reply),
        ("wpp_sync_message", "同步微信消息增量", {
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
        }, sync_message),
        ("wpp_accounts_context", "查询所有微信账号的上下文元信息（账号ID/绑定agent/selfWxid/连接状态/白名单），不含authcode。用于多账号场景下跨账号路由前探查", {
            "type": "object",
            "properties": {},
        }, accounts_context),
        ("wpp_send_ai_image", "AI 生成图片并自动发送到微信（Hermes image_gen → 百炼 qwen-image 生成 → 自动 send_image）。一步完成「生成海报/配图 → 发微信」", {
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
        ("wpp_fc_publish_ai", "AI 配图并发布朋友圈（Hermes image_gen → 百炼 qwen-image 生成 1-9 张配图 → 自动发布图文朋友圈）。一步完成「写文案 + 自动配图 + 发朋友圈」", {
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
