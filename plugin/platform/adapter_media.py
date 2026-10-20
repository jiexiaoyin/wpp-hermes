"""媒体链路 mixin —— 下载 / 上传 / 发送。

从 ``platform/adapter.py`` 抽出来的第一块（那次拆分的第一刀，2026-10-19）：
``adapter.py`` 单文件已到 3000+ 行，而媒体这 9 个方法内聚度最高、与入站/出站解耦。

## 为什么用 mixin 而不是独立类

这些方法大量依赖适配器实例状态（``self._clients`` / ``self._name_cache`` /
``self.accounts``…）与其它未抽走的方法。mixin 保持 ``self`` 语义不变，调用点一个字都不用改；
``WppAdapter(MediaMixin, BasePlatformAdapter)`` 的 MRO 让 mixin 方法优先于基类同名方法，
与原「类内定义覆盖基类」完全等价。

## 依赖约定

模块级只 import **真正跨方法共享**的名字；单方法要用的第三方（``stt`` / ``vendor_mcp`` /
``file_name_of`` …）继续在函数体内延迟导入 —— 注册期不得有第三方依赖
（``plugin/tests/heartflow/test_*_module_level_imports`` 会检查）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

from gateway.platforms.base import SendResult

from ..core import accounts as accounts_mod
from ..core import metrics
from ..core.api_client import WppApiError, WppClient
from ..inbound import WppMessage
from ..inbound.media_oss import (
    KIND_FILES,
    KIND_IMAGES,
    KIND_VIDEOS,
    KIND_VOICES,
    upload_media_bytes,
)
from . import outbound, silk_encoder

log = logging.getLogger(__name__)


class MediaMixin:

    # ------------------------------------------------------------------ _fetch_media

    async def _fetch_media(self, acct: accounts_mod.Account,
                           msg: WppMessage) -> tuple[list[str], list[str]]:
        """把消息里的媒体下到本地缓存，返回 ``(media_urls, media_types)``。

        **best-effort**：下不下来都不该挡住这条消息（旧实现里下载超时会拖死整批）。

        ⚠️ **别名纠正（W7-1 顺手改的错话）**：这里原来写着"图片必须下是因为心流 judge
        看图要的是**本地路径**"—— 那是错的。judge 那条路是
        ``msg.content`` → ``extract_hf_image_urls``（只认 ``^https?://``）→
        ``judge.filter_image_urls``（``^https?://|data:image/``）⇒ **本地文件路径会被
        两道闸直接丢掉**，"看图"要的是**公网直链**。所以本方法现在做两件事：下到本地
        （给 Hermes 的 ``media_urls`` / 多模态附件）+ 传上 OSS 并把直链写回 ``msg.content``
        （给 judge / LLM / 落库 —— 见 :meth:`_publish_media`）。
        这条错话留在这里是因为它恰好说明了"文档说做到了、链路里其实没有"这种失效形态。
        """
        from gateway.platforms.base import (
            cache_audio_from_bytes,
            cache_document_from_bytes,
            cache_image_from_bytes,
            cache_video_from_bytes,
        )

        from ..inbound.app_card import enrich_mini_program_asset, is_mini_program_card, parse_mini_program_card
        from ..inbound.parser import MT_APP, MT_IMAGE, MT_VIDEO, MT_VOICE

        client = self._clients.get(acct.id)
        if client is None:
            return [], []

        mtype = int(msg.msg_type or 0)

        # W7-2 小程序卡片封面：49 卡片的资产挂在 ``app.cover_image`` 上，**不进** ``msg.media``
        # （vendor 的媒体字段三代混用，小程序封面算第四种）⇒ 这一支必须排在下面
        # ``not msg.media`` 那道闸**之前**，否则整类消息在这里就被挡回去，
        # 而现场表现只是"卡片没图"（最难查的那种）。
        if mtype == MT_APP and is_mini_program_card(msg.raw):
            card = parse_mini_program_card(msg.raw)
            if card is not None and card.cover is not None:
                result = await enrich_mini_program_asset(client, acct.id, card.cover)
                if result.media_url:
                    # 两个键由 :func:`inbound.app_card.mini_program_block` 读
                    msg.media["mp_asset_url"] = result.media_url
                    msg.media["mp_asset_kind"] = result.kind
                else:
                    # 与 TS 同款降级：正文里就没有 ``[小程序封面]`` 行，不算错误。
                    log.info("[WPP:%s] 小程序卡片封面未拿到（非致命）: %s", acct.id, result.error)

        # 文件消息：v1 download_context → DownloadFileBinary；失败/缺失 ⇒ vendor MCP
        # 只读兜底（``wechat_get_recent_messages`` 找 CDN URL）⇒ 最终确定性话术。
        # 对齐 TS ``handler.ts:382-455``（FILE-DOWNLOAD-BINARY → VENDOR-MCP → NO-PATH-GUESS）。
        # ⚠️ 必须排在 ``not msg.media`` 之前：没有 download_context 的文件消息
        #   ``media`` 是空 dict，排队后面就整类消息都走不到兜底。
        from ..inbound.media import file_name_of, is_file_message

        if mtype == MT_APP and is_file_message(msg.raw):
            fname = file_name_of(msg.raw, msg.media) or "(未知文件名)"
            data = None
            ctx = msg.media.get("file_ctx")
            if ctx:
                try:
                    data = await client.download_file_binary(
                        ctx.get("attach_id", ""),
                        user_name=ctx.get("user_name", ""),
                        data_len=int(ctx.get("data_len") or 0),
                        start_pos=int(ctx.get("start_pos") or 0),
                        section_len=int(ctx.get("section_len") or 0),
                        app_id=ctx.get("app_id", ""),
                        file_name=ctx.get("file_name", ""),
                    )
                except (WppApiError, NotImplementedError, OSError) as exc:
                    log.warning("[WPP:%s] 文件下载失败（尝试 MCP 兜底）: %s", acct.id, exc)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[WPP:%s] 文件下载异常（尝试 MCP 兜底）: %s", acct.id, exc)
            if not data and acct.mcp_enabled:
                got = await self._fetch_file_via_mcp(client, msg, fname)
                if got:
                    data, fname = got
                    log.info("[WPP:%s] 文件经 vendor MCP 兜底下载成功: %s (%d 字节)",
                             acct.id, fname, len(data))
            if data:
                await self._publish_media(acct, msg, kind=KIND_FILES,
                                          filename=fname, data=data)
                return [cache_document_from_bytes(data, fname)], ["application/octet-stream"]
            # 确定性兜底（TS v1.2.0 NO-PATH-GUESS）：**禁止** AI 去猜路径/翻旧文件。
            msg.content = (
                f"{msg.content}\n[系统提示-文件限制] 此文件消息仅有文件名元数据, "
                f"vendor 当前不提供文件内容下载 (MCP 增强未命中), 你无法读取文件内容。\n"
                f"禁止: 用 find/ls 搜索 *.pdf 或任何文件、猜测/拼接文件路径、"
                f"读取系统里任何现有文件 (可能是旧文件误导)。\n"
                f"只需: 基于文件名回复用户 (例如\"收到文件 {fname}, "
                f"但当前平台无法读取文件内容, 需要内容请换图片或文本发送\"), "
                f"或询问用户是否改用文本/图片发送。"
            )
            log.info("[WPP:%s] 文件兜底话术已注入: msg=%s name=%s", acct.id, msg.msg_id, fname)
            return [], []

        if not msg.media:
            return [], []

        try:
            if mtype == MT_IMAGE and msg.media.get("cdn"):
                cdn = msg.media["cdn"]
                data = await client.download_image_cdn(
                    cdn.get("file_aes_key", ""), cdn.get("file_no", ""),
                    variant=cdn.get("variant", "") or "")
                if data:
                    await self._publish_media(acct, msg, kind=KIND_IMAGES,
                                              filename=f"{msg.msg_id or 'image'}.jpg",
                                              data=data)
                    return [cache_image_from_bytes(data, ext=".jpg")], ["image/jpeg"]

            if mtype == MT_VOICE:
                data = await self._download_voice(client, msg)
                if data:
                    # 转写要在返回**之前**写进 msg.media：正文由 prepare_text 稍后读它
                    await self._fill_voice_transcript(acct.id, msg.media, data)
                    # 2026-10-19 审阅修复：转写只写进 media 还不够 —— 落库（④z）、
                    # 心流缓冲（④a）、judge 与 intent 都在 prepare_text 之前/之外读
                    # ``msg.content``，于是未 @ 的语音在这些消费者眼里只剩
                    # ``[语音] <oss-url>``，等于"说了话但裁判从没看见"。
                    # 在这里把转写并进正文（早于 ④z/④a），并让 prepare_text 去重。
                    from ..inbound.media import voice_transcript

                    transcript = voice_transcript(msg.media)
                    if transcript:
                        line = f"[转写] {transcript}"
                        if line not in (msg.content or ""):
                            msg.content = (f"{msg.content}\n{line}"
                                           if (msg.content or "").strip() else line)
                    # SILK 头是 b"\x02#"；其余当 AMR（vendor 只出这两种）
                    ext = ".silk" if data[:2] == b"\x02#" else ".amr"
                    # ⚠️ 传上去的是**原始 silk/amr**，不是 TS 那种 mp3：TS 的 ``voice.ts``
                    # 先 ffmpeg 转码再传，而 py2 侧无 ffmpeg（``真机`` 上也没有）。
                    # 后果仅限"点开那个 URL 播不出声"——模型看到的是 ``[语音] <url>`` 一行
                    # 加下面的 ``[转写]``，语义由转写承载 ⇒ 如实降级，不假称是 mp3。
                    await self._publish_media(acct, msg, kind=KIND_VOICES,
                                              filename=f"{msg.msg_id or 'voice'}{ext}",
                                              data=data)
                    return [cache_audio_from_bytes(data, ext=ext)], ["audio/silk"]

            if mtype == MT_VIDEO and msg.media.get("video_ctx"):
                data = await self._download_video_chunks(client, msg)
                if data:
                    await self._publish_media(acct, msg, kind=KIND_VIDEOS,
                                              filename=f"{msg.msg_id or 'video'}.mp4",
                                              data=data)
                    return [cache_video_from_bytes(data, ext=".mp4")], ["video/mp4"]

        except (WppApiError, NotImplementedError, OSError) as exc:
            log.warning("[WPP:%s] 媒体下载失败（不阻塞消息）: %s", acct.id, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP:%s] 媒体下载异常（不阻塞消息）: %s", acct.id, exc)
        return [], []

    # ------------------------------------------------------------------ _fetch_file_via_mcp

    async def _fetch_file_via_mcp(self, client: WppClient, msg: WppMessage,
                                  filename: str) -> tuple[bytes, str] | None:
        """vendor MCP 只读兜底：从 ``wechat_get_recent_messages`` 找文件的 CDN URL。

        对齐 TS ``enrichFileMessageViaMcp``（v1.2.0 VENDOR-MCP）：只在 DownloadFileBinary
        没拿到字节时调用；MCP 不可用 / 找不到消息 / 没有 URL / 下载失败都返回 ``None``，
        由调用方走确定性话术。**只调只读工具**，不碰任何写接口。
        """
        from ..core import vendor_mcp

        raw = msg.raw if isinstance(msg.raw, dict) else {}
        local_id = raw.get("local_id") or raw.get("localId") or raw.get("LocalId")
        try:
            result = await asyncio.wait_for(
                vendor_mcp.call_tool("wechat_get_recent_messages", {"limit": 500},
                                     client.authcode),
                timeout=12)
        except Exception as exc:  # noqa: BLE001 —— MCP 是增强，失败不阻塞消息
            log.debug("[WPP] 文件 MCP 兜底不可用: %s", exc)
            return None
        if not isinstance(result, dict) or result.get("isError"):
            return None

        messages: list[dict] = []
        for block in result.get("content") or []:
            text = block.get("text") if isinstance(block, dict) else ""
            if not isinstance(text, str) or not text:
                continue
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, list):
                messages.extend(m for m in parsed if isinstance(m, dict))
            elif isinstance(parsed, dict) and isinstance(parsed.get("messages"), list):
                messages.extend(m for m in parsed["messages"] if isinstance(m, dict))

        target = None
        for m in messages:
            if local_id is not None and str(m.get("local_id")) == str(local_id):
                target = m
                break
            if filename and filename in str(m.get("content") or ""):
                target = m
                break
        if target is None:
            log.debug("[WPP] 文件 MCP 兜底未命中（local_id=%s name=%s）", local_id, filename)
            return None

        url = ""
        for key in ("cdnUrl", "cdn_url", "fileUrl", "file_url"):
            if isinstance(target.get(key), str) and target[key]:
                url = target[key]
                break
        if not url:
            import re

            m = re.search(r"https?://[^\s\"'<>]+", str(target.get("content") or ""))
            url = m.group(0) if m else ""
        if not url:
            return None
        try:
            data = await outbound.netguard.safe_fetch_url(
                url, timeout=30, max_bytes=100 * 1024 * 1024)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP] 文件 MCP 兜底下载失败: %s", exc)
            return None
        return data, filename

    # ------------------------------------------------------------------ _publish_media

    async def _publish_media(self, acct: accounts_mod.Account, msg: WppMessage, *,
                             kind: str, filename: str, data: bytes) -> None:
        """W7-1：把刚下到的字节传上 OSS，成功就把那一行 URL **追加**进 ``msg.content``。

        这是 TS ``enrichImageMessage`` / ``enrichVideoMessage`` / ``enrichVoiceMessage`` /
        ``enrichFileMessage`` 六个入口的最后一段（``handler.ts:232/315/372/392``），
        注入格式**逐字对齐** —— 那四行是下游的解析口径（``extract_hf_image_urls`` 认
        ``[图片] <http(s) 直链>``、``[转写]`` 与语音行相邻、``_media_tag``、引用）。

        **降级与 TS 同款**：缺凭据 / 缺 ossutil / 上传失败 / 空字节 ⇒
        ``upload_media_bytes`` 返 ``None`` ⇒ 这里**只出声、正文一个字不加**，
        调用方照旧返回本地缓存路径。⚠️ 这条降级路径**必须**保持"与加富化之前逐字节
        相同"（``真机`` 上既没 ossutil 也没凭据，现状就是它）。
        "写 URL、写 ``oss_url`` 键、注入正文"三件事在 :func:`_inject_media_enrich_line`
        里**一起**发生（那里也解释了为什么不能只做一半）。
        """
        url = await upload_media_bytes(data, filename, account_id=acct.id, kind=kind)
        if not url:
            metrics.inc_enrich_save_failed()
            return
        from .adapter import _inject_media_enrich_line  # 循环导入：延迟到调用期
        _inject_media_enrich_line(msg, kind, url, filename=filename, size=len(data))
        log.debug("[WPP:%s] 媒体已富化 %s: %s", acct.id, msg.msg_id, url)

    # ------------------------------------------------------------------ _fill_voice_transcript

    @staticmethod
    async def _fill_voice_transcript(account_id: str, media: dict, silk_bytes: bytes) -> None:
        """把语音的文字写进 ``media["stt_text"]``（vendor 转写优先，缺了才自己转）。

        **没有它，语音在模型眼里只剩 ``[语音]`` 一行** —— 老板 @ 机器人发语音＝没说话，
        而现场日志一切正常（消息进来了、音频文件也落了），是最难查的那种失效。

        两条来源的顺序与取舍见 :func:`plugin.inbound.stt.should_attempt_stt`：
        vendor 已经转过就不再下载/调 API（省一次外呼，也更准）。
        降级（缺解码器 / 缺 key）**只喊一次**，见 :func:`plugin.inbound.stt.note_degradation_once`。
        """
        from ..inbound import stt

        if not stt.should_attempt_stt(media):
            log.debug("[WPP:%s] 语音用 vendor 转写（免 STT）", account_id)
            return

        reason = stt.unavailable_reason()
        if reason:
            stt.note_degradation_once(reason)
            return

        text = await stt.transcribe_silk_buffer(silk_bytes)
        if text:
            media["stt_text"] = text
            log.info("[WPP:%s] 语音转写成功（%d 字）", account_id, len(text))
        else:
            # 走到这说明依赖齐备但对方没给文字（空音频 / 5xx 到底）—— 留痕，别静默
            log.info("[WPP:%s] 语音转写未拿到文字 ⇒ 正文只有 [语音]", account_id)

    # ------------------------------------------------------------------ _download_voice

    async def _download_voice(self, client: WppClient, msg: WppMessage) -> bytes | None:
        """下语音。新 schema（``voice_ctx``）优先，老 schema（``legacy_cdn``）兜底。"""
        ctx = msg.media.get("voice_ctx")
        if ctx:
            return await client.download_voice_binary(
                int(ctx.get("msg_id") or 0),
                new_msg_id=str(ctx.get("new_msg_id") or ""),
                client_msg_id=str(ctx.get("client_msg_id") or ""),
                master_buf_id=str(ctx.get("master_buf_id") or "0"),
                fmt=int(ctx.get("format") or 4),
                length=int(ctx.get("length") or 0),
                chat_room_name=str(ctx.get("chat_room_name") or ""),
                from_user_name=str(ctx.get("from_user_name") or ""),
                to_user_name=str(ctx.get("to_user_name") or ""),
            )
        legacy = msg.media.get("legacy_cdn")
        if legacy:
            return await client.download_voice_legacy(
                legacy.get("aes_key", ""), legacy.get("file_no", ""))
        return None

    # ------------------------------------------------------------------ _download_video_chunks

    async def _download_video_chunks(self, client: WppClient, msg: WppMessage) -> bytes | None:
        """**分片**下完整视频。

        ``/Tools/DownloadVideo`` 一次只给一片（默认 1MB），要 ``start_pos += len(chunk)``
        循环到 ``start_pos >= data_len``（旧 ``api_client`` 的 docstring 明确写了这点，
        但**没有任何调用方真的循环** —— 于是视频永远只拿到第一片）。
        """
        ctx = msg.media["video_ctx"]
        total = int(ctx.get("data_len") or 0)
        chunk_len = int(ctx.get("chunk_len") or 1048576) or 1048576
        if total <= 0:
            return None

        buf = bytearray()
        start = int(ctx.get("start_pos") or 0)
        # 安全上限：字节 200MB + 最多 4096 片。旧实现只限片数（≈4GB）——
        # vendor 把 data_len 报大时会把整段视频读进内存，一条入站消息就能打爆进程。
        from ..core.netguard import DEFAULT_MAX_BYTES

        max_bytes = DEFAULT_MAX_BYTES
        for _ in range(4096):
            piece = await client.download_video(
                to_wxid=ctx.get("to_wxid", ""), msg_id=int(ctx.get("msg_id") or 0),
                data_len=total, start_pos=start, chunk_len=chunk_len,
            )
            if not piece:
                break
            if len(buf) + len(piece) > max_bytes:
                log.warning("[WPP] 视频超过 %d 字节上限，截断下载（msg=%s）",
                            max_bytes, msg.msg_id)
                return None
            buf += piece
            start += len(piece)
            if start >= total:
                break
        if not buf:
            return None
        if len(buf) < total:
            log.warning("[WPP] 视频分片不完整 (%d/%d 字节)，仍使用已下载部分", len(buf), total)
        return bytes(buf)

    # ------------------------------------------------------------------ _send_media

    async def _send_media(self, chat_id: str, ref: str, kind: str,
                          caption: str | None, metadata: dict | None,
                          file_name: str | None = None,
                          reply_to: str | None = None) -> SendResult:
        """媒体发送的统一实现。

        ``ref`` → base64 的解析走 :func:`outbound.resolve_to_base64`（**内建安全闸**：
        本地路径过白名单、URL 过 SSRF 校验）。

        ⚠️ **视频要缩略图**：``send_video`` 的 ``ImageBase64`` 是 **必填**
        （vendor 侧拒收空值）。这里用 ffmpeg 抽一帧；抽不到就照发并把
        vendor 的报错**如实**返回 —— 不假装成功。
        """
        try:
            account_id, peer_id = self._parse_chat_id(chat_id)
            acct = self.account(account_id)
            client = self._clients.get(acct.id)
            if client is None:
                return SendResult(success=False, error=f"账号 {acct.id} 未连接",
                                  error_kind="transient")
            outbound.assert_outbound_allowed(peer_id, acct.id,
                                             allowed=self._send_allowlist)

            b64 = await outbound.resolve_to_base64(ref)
            if not b64:
                return SendResult(success=False, error=f"媒体内容不可用: {ref[:60]}",
                                  error_kind="bad_format")

            name = file_name or os.path.basename(ref) or f"{kind}.bin"
            voice_segments_persisted = False
            if kind == "image":
                resp = await client.send_image(to_wxid=peer_id, image_base64=b64)
            elif kind == "voice":
                # 多段语音每段都是独立气泡、独立 msg_id：逐段落库，否则历史里只剩
                # 最后一段（2026-10-19 审阅发现）。回调用 **本账号的 acct** 闭包钉住。
                async def _persist_voice_segment(resp, *, index, total, kind, name):
                    marker = ("[语音]" if kind == "voice" else f"[文件] {name}")
                    if total > 1:
                        marker += f" ({index + 1}/{total})"
                    from .adapter import _persist_outbound_record
                    await _persist_outbound_record(
                        acct, peer_id, marker, outbound.extract_outbound_msg_ids(resp),
                        msg_type=kind if kind in ("image", "voice", "video", "file") else "file",
                        raw_payload=resp)

                voice_segments_persisted = True
                resp = await self._send_voice_media(
                    client, peer_id, ref, name, b64,
                    on_segment=_persist_voice_segment)
            elif kind == "video":
                thumb = await self._video_thumb(ref)
                if not thumb:
                    log.warning("[WPP] 视频无缩略图（ffmpeg 不可用或抽帧失败）—— vendor 可能拒收")
                resp = await client.send_video(to_wxid=peer_id, video_base64=b64,
                                               thumb_base64=thumb)
            else:
                resp = await client.send_file_v2(to_wxid=peer_id, file_name=name,
                                                 file_base64=b64)

            if not outbound.send_ok(resp):
                return SendResult(success=False, error=outbound.send_error_text(resp),
                                  raw_response=resp)
            # 与 live send() 同口径：媒体也要落库（TS ``dispatch/outbound.ts`` 的
            # persistOutbound 覆盖 image/file/voice/video）。这里原先只发不记，
            # "按群查历史"看不到任何媒体出站。
            if not voice_segments_persisted:
                marker = {"image": "[图片]", "voice": "[语音]", "video": "[视频]"}.get(
                    kind, f"[文件] {name}")
                from .adapter import _persist_outbound_record  # 循环导入：延迟到调用期
                await _persist_outbound_record(
                    acct, peer_id, marker, outbound.extract_outbound_msg_ids(resp),
                    msg_type=kind if kind in ("image", "voice", "video", "file") else "file",
                    raw_payload=resp)
            media_id = outbound.extract_msg_id(resp)
            # ★ 2026-10-19：caption 原先被完全丢掉。Hermes 的
            #   ``_media_caption_split`` 会把「一张图 + 短文案」合成一次媒体发送，
            #   ``_send_live_adapter_media`` 已把 caption 传进来 —— 这里不消费，
            #   用户就只能看到图、看不到说明。媒体已发出后文案单独发；文案失败
            #   不能返回 failure（否则框架会把整张图再发一遍），但必须留 ERROR。
            cap = (caption or "").strip()
            if cap:
                cap_result = await self.send(chat_id, cap, reply_to=reply_to,
                                             metadata=metadata)
                if not cap_result.success:
                    log.error("[WPP] 媒体已发出但文案发送失败（不重发媒体）: %s",
                              cap_result.error)
                    raw = dict(resp) if isinstance(resp, dict) else {"media": resp}
                    raw["caption_error"] = cap_result.error
                    return SendResult(success=True, message_id=media_id,
                                      raw_response=raw)
                media_id = cap_result.message_id or media_id
            return SendResult(success=True, message_id=media_id, raw_response=resp)
        except (ValueError, outbound.netguard.GuardError) as exc:
            log.error("[WPP] 媒体发送被拒: %s", exc)
            return SendResult(success=False, error=str(exc), error_kind="forbidden")
        except Exception as exc:
            log.error("[WPP] 媒体发送异常: %s", exc, exc_info=True)
            return SendResult(success=False, error=str(exc), error_kind="unknown")

    # ------------------------------------------------------------------ _send_voice_media

    @staticmethod
    async def _send_voice_media(client: WppClient, peer_id: str, ref: str, name: str,
                                b64: str, *, on_segment=None) -> dict:
        """发语音。**只有 SILK 能进 ``/Msg/SendVoice``**，其余一律降级发文件。

        为什么不能「原样塞 voice_type=4」：厂商对非 SILK / 超限 silk 会**静默拒收**
        并回一个文案为 ``INVALID_CREDENTIAL`` 的 ``Code=-2``（误导性错误码）。
        实证矩阵与根因见 :mod:`plugin.platform.silk_encoder` 的模块 docstring。

        顺序（对齐 TS ``dispatch/outbound.ts`` v1.3.48 的 sendVoice 链路 + 其降级）：

        1. 入参已是 ``.silk`` 且不超 :data:`silk_encoder.MAX_SILK_BYTES` ⇒ 直接发；
        2. 否则试 :func:`silk_encoder.encode_to_silk`（需要 ffmpeg + encoder 二进制，
           ``真机`` 上两者都没有 ⇒ 直接返回 ``None``）；
        3. 还不行，或厂商仍拒收 ⇒ **发文件**（老板 6-12 定的偏好：
           "成功发为语音, 失败降级文件"）。

        返回的是最终那次响应的原样 dict，交给调用方统一判 :func:`outbound.send_ok`。
        """
        import shutil
        import tempfile

        ext = os.path.splitext(name)[1].lower()
        src = os.path.expanduser(str(ref or ""))
        temp_dir = ""
        segments: list[str] = []
        last_resp: dict = {}

        # 2026-10-19 审阅修复：``ref`` 可能是 URL / data URI / 裸 base64（live 的
        # ``send()`` 只把 b64 解析出来，ref 保持原样）。旧实现把它们直接交给
        # ``split_audio``（不是文件 ⇒ 原样返回），再在降级分支 ``open(seg)`` ⇒
        # FileNotFoundError，整条语音发送失败。这里先把 b64 落地成临时文件，
        # 后续分段/探测/降级都只面对本地路径。
        try:
            data_bytes = base64.b64decode(b64 or "", validate=False)
        except Exception:  # noqa: BLE001
            data_bytes = b""
        is_silk = ext == ".silk" or silk_encoder.looks_like_silk_bytes(data_bytes)
        if is_silk:
            ext = ".silk"
        send_name = name
        if is_silk and not send_name.lower().endswith(".silk"):
            send_name = f"{os.path.splitext(send_name)[0] or 'voice'}.silk"

        def _seg_b64(path: str) -> str:
            """优先读本地分段；极端情况下（临时文件被清）退回原始 b64。"""
            try:
                with open(path, "rb") as fh:
                    return base64.b64encode(fh.read()).decode()
            except OSError:
                return b64

        async def _note(resp: dict, idx: int, total: int, seg_kind: str,
                        seg_name: str) -> None:
            """每成功发出一条，交给调用方落库（多段各一条记录）。

            回调失败只 warn —— 消息已经在微信里了，不能因为记账失败再把发送判成失败。
            """
            if on_segment is None or not outbound.send_ok(resp):
                return
            try:
                await on_segment(resp, index=idx, total=total,
                                 kind=seg_kind, name=seg_name)
            except Exception as exc:  # noqa: BLE001
                log.warning("[WPP] 语音分段落库失败（消息已发出）: %s", exc)

        try:
            if not os.path.isfile(src):
                temp_dir = tempfile.mkdtemp(prefix="wpp-voice-split-")
                src = os.path.join(
                    temp_dir, f"{os.path.splitext(name)[0] or 'voice'}{ext or '.bin'}")
                with open(src, "wb") as fh:
                    fh.write(data_bytes)
            if not os.path.isfile(src) or os.path.getsize(src) <= 0:
                log.warning("[WPP] 语音内容为空/不可落地 —— 降级发文件")
                return await client.send_file_v2(
                    to_wxid=peer_id, file_name=send_name, file_base64=b64)

            # 微信单条语音上限 60s；厂商实际还卡 SILK **字节**上限（60 KB），
            # 所以先按 55s 切段，再对每段做「25→12→8→6 kbps」码率自适应。
            segments = await silk_encoder.split_audio(src)
            if (is_silk and len(segments) == 1
                    and len(data_bytes) <= silk_encoder.MAX_SILK_BYTES):
                # Type=4 = SILK（微信默认；0=AMR 是**入站**才见到的格式，厂商出站不收）
                # VoiceTime 必须是**毫秒且非 0**：2026-10-19 实测传 0 会被 Code=-2 拒收
                # （错误文案是误导性的 INVALID_CREDENTIAL），传真实时长立即成功。
                voice_ms = (await silk_encoder.probe_duration_ms(segments[0])
                            or await silk_encoder.probe_silk_base64_ms(b64))
                if voice_ms > 0:
                    try:
                        resp = await client.send_voice(
                            to_wxid=peer_id, voice_base64=b64,
                            voice_type=4, voice_time=voice_ms)
                    except WppApiError as exc:  # 厂商拒收是 raise，不是返回
                        log.warning("[WPP] 语音发送被拒（%s）—— 降级发文件", exc)
                        resp = {"Code": -2, "Message": str(exc)}
                    if outbound.send_ok(resp):
                        await _note(resp, 0, 1, "voice", send_name)
                        return resp
                    log.warning("[WPP] 语音发送被拒（%s）—— 降级发文件",
                                outbound.send_error_text(resp))
                else:
                    log.warning("[WPP] 无法确认 SILK 时长（VoiceTime=0 必被拒）—— 降级发文件")
                resp = await client.send_file_v2(
                    to_wxid=peer_id, file_name=send_name, file_base64=b64)
                await _note(resp, 0, 1, "file", send_name)
                return resp

            stem = os.path.splitext(send_name)[0] or "voice"
            for idx, seg in enumerate(segments):
                silk_b64 = await silk_encoder.encode_to_silk_fitting(seg)
                seg_name = send_name if len(segments) == 1 else \
                    f"{stem}-{idx + 1}{os.path.splitext(seg)[1] or '.wav'}"
                if not silk_b64:
                    # 该段无法转 SILK ⇒ 只降级这一段，别把已发出去的前几段重发
                    log.info("[WPP] 语音段非 SILK 且无法转码 —— 该段降级发文件: %s", seg_name)
                    last_resp = await client.send_file_v2(
                        to_wxid=peer_id, file_name=seg_name,
                        file_base64=_seg_b64(seg))
                    await _note(last_resp, idx, len(segments), "file", seg_name)
                    continue

                voice_ms = (await silk_encoder.probe_duration_ms(seg)
                            or await silk_encoder.probe_silk_base64_ms(silk_b64))
                if voice_ms <= 0:
                    log.warning("[WPP] 第 %d/%d 段无法确认时长（VoiceTime=0 必被拒）"
                                "—— 该段降级发文件", idx + 1, len(segments))
                    last_resp = await client.send_file_v2(
                        to_wxid=peer_id, file_name=seg_name,
                        file_base64=_seg_b64(seg))
                    await _note(last_resp, idx, len(segments), "file", seg_name)
                    continue
                try:
                    resp = await client.send_voice(
                        to_wxid=peer_id, voice_base64=silk_b64,
                        voice_type=4, voice_time=voice_ms)
                except WppApiError as exc:  # 厂商拒收是 raise，不是返回
                    log.warning("[WPP] 第 %d/%d 段语音被拒（%s）—— 降级发文件",
                                idx + 1, len(segments), exc)
                    resp = {"Code": -2, "Message": str(exc)}
                last_resp = resp
                if outbound.send_ok(resp):
                    await _note(resp, idx, len(segments), "voice", seg_name)
                    continue
                log.warning("[WPP] 第 %d/%d 段语音被拒（%s）—— 降级发文件",
                            idx + 1, len(segments), outbound.send_error_text(resp))
                last_resp = await client.send_file_v2(
                    to_wxid=peer_id, file_name=seg_name,
                    file_base64=_seg_b64(seg))
                await _note(last_resp, idx, len(segments), "file", seg_name)
            return last_resp or {"Code": -1, "Message": "语音无可发送内容"}
        finally:
            silk_encoder.cleanup_segments(segments)
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ _video_thumb

    @staticmethod
    async def _video_thumb(ref: str) -> str:
        """用 ffmpeg 抽首帧做缩略图，返回 base64（失败返 ``""``）。

        ``ref`` 是本地路径（URL 已经被 :func:`outbound.resolve_to_base64` 校验过）；
        远程 URL 不在这里处理 —— 抽帧只在本地文件上做。
        """
        if not (ref.startswith("/") or ref.startswith("~")):
            return ""
        path = os.path.expanduser(ref)
        if not os.path.isfile(path):
            return ""
        tmp = ""
        try:
            import shutil
            import tempfile

            if not shutil.which("ffmpeg"):
                log.debug("[WPP] 无 ffmpeg，跳过视频抽帧")
                return ""
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fh:
                tmp = fh.name
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "error", "-i", path,
                "-frames:v", "1", "-f", "image2", tmp,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            except TimeoutError:
                proc.kill()
                log.warning("[WPP] ffmpeg 抽帧超时")
                return ""
            if proc.returncode != 0:
                log.debug("[WPP] ffmpeg 抽帧失败: %s", (stderr or b"")[:200])
                return ""
            with open(tmp, "rb") as fh:
                return base64.b64encode(fh.read()).decode()
        except (OSError, asyncio.SubprocessError) as exc:
            log.debug("[WPP] 视频抽帧异常: %s", exc)
            return ""
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
