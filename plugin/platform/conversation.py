"""WPP 对通道无关会话契约的实现（扩展边界 P1，2026-10-19）。

契约本身在 :mod:`plugin.neutral.contracts`；本模块只做三件 WPP 专属的事：

* 把 ``WppMessage`` / 账号配置映射成 :class:`ConversationKey`；
* 用 ``wpp_messages`` 表实现 :class:`ConversationStore`；
* 用 ``WppClient`` / SILK 编码实现 :class:`OutboundSender`。

策略层（主动消息、画像、风格…）只依赖契约，不 import 本模块 —— 换通道时
只需提供另一份实现，策略代码不动。
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import Callable
from typing import Any

from ..neutral.contracts import ChatCapabilities, ConversationKey, MessageRow

log = logging.getLogger(__name__)

#: 本通道在 :class:`ConversationKey.platform` 里的名字。
PLATFORM = "wechatpadpro"

#: WPP 能力声明：群聊/语音/引用/成员齐全，语音容器是 SILK。
WPP_CAPABILITIES = ChatCapabilities(
    groups=True, voice=True, quote=True, members=True, voice_format="silk",
)


def key_from_message(msg: Any) -> ConversationKey:
    """``WppMessage`` → 统一会话身份（只读属性，不依赖具体类）。"""
    return ConversationKey(
        platform=PLATFORM,
        account_id=str(getattr(msg, "account_id", "") or ""),
        chat_type=str(getattr(msg, "peer_kind", "") or "dm"),
        chat_id=str(getattr(msg, "peer_id", "") or ""),
    )


def dm_key(account_id: str, peer_id: str) -> ConversationKey:
    return ConversationKey(PLATFORM, account_id, "dm", peer_id)


def group_key(account_id: str, group_id: str) -> ConversationKey:
    return ConversationKey(PLATFORM, account_id, "group", group_id)


class WppConversationStore:
    """:class:`ConversationStore` 的 WPP 实现（读 ``wpp_messages``）。"""

    platform = PLATFORM

    def __init__(self, account_id: str | None = None) -> None:
        self.account_id = account_id

    def owns(self, conv: ConversationKey) -> bool:
        return (conv.platform == PLATFORM
                and (self.account_id is None or conv.account_id == self.account_id))

    def recent(self, conv: ConversationKey, limit: int = 20) -> list[MessageRow]:
        if not self.owns(conv):
            return []
        from ..data import repo_messages

        rows = repo_messages.list_messages_by_peer(conv.account_id, conv.chat_id, limit)
        out: list[MessageRow] = []
        for row in rows:
            # 2026-10-19 审阅修复：``wpp_messages.direction`` 的 ENUM 是
            # ``inbound/outbound``（persist.db_direction 就这么写），而
            # ``proactive_policy.is_eligible`` 按契约读 ``incoming``。
            # 原样透传 ⇒ 主动消息这个功能在真库上**永远**返回 not eligible，
            # 且是静默的（reason=ok, sent=0）。在这里归一成契约口径。
            direction = str(row.get("direction") or "")
            if direction == "inbound":
                direction = "incoming"
            out.append(MessageRow(
                direction=direction,
                at_sec=_int(row.get("at_sec")),
                content=str(row.get("content") or ""),
                sender_id=str(row.get("from_wxid") or ""),
            ))
        return out

    def count_outbound_since(self, conv: ConversationKey, since_sec: int) -> int:
        if not self.owns(conv):
            return 0
        from ..data import repo_messages

        return repo_messages.count_outbound_since(
            conv.account_id, conv.chat_id, int(since_sec))


class WppOutboundSender:
    """:class:`OutboundSender` 的 WPP 实现。

    ``client_getter`` 必须是**零参可调用**：每次发送时现取连接，避免适配器重连后
    手里握着旧 client。未连接时抛 :class:`RuntimeError` —— 调用方据此跳过本轮。
    """

    platform = PLATFORM
    capabilities = WPP_CAPABILITIES

    def __init__(self, account_id: str,
                 client_getter: Callable[[], Any] | None = None) -> None:
        self.account_id = account_id
        self._client_getter = client_getter

    @classmethod
    def from_adapter(cls, adapter: Any, account_id: str) -> "WppOutboundSender":
        def _getter() -> Any:
            return (getattr(adapter, "_clients", {}) or {}).get(account_id)
        return cls(account_id, _getter)

    # ------------------------------------------------------------------ 内部

    def _client(self, conv: ConversationKey) -> Any:
        if conv.platform != PLATFORM or conv.account_id != self.account_id:
            raise ValueError(f"会话不属于本 sender：{conv!r}")
        client = self._client_getter() if self._client_getter is not None else None
        if client is None:
            raise RuntimeError(f"WPP 账号 {self.account_id} 未连接")
        return client

    @staticmethod
    def _result(resp: Any, action: str) -> str:
        from . import outbound

        if not outbound.send_ok(resp):
            raise RuntimeError(f"{action} 失败: {outbound.send_error_text(resp)}")
        ids = outbound.extract_outbound_msg_ids(resp)
        return ids.new_msg_id or ids.msg_id or ""

    # ------------------------------------------------------------------ 发送

    async def send_text(self, conv: ConversationKey, text: str, *,
                        quote_id: str | None = None) -> str:
        from . import outbound

        client = self._client(conv)
        content = outbound.build_quote_reply_xml(text, quote_id) if quote_id else text
        resp = await client.send_text(conv.chat_id, content)
        return self._result(resp, "send_text")

    async def send_voice(self, conv: ConversationKey, audio_path: str) -> str:
        from ..core import netguard
        from ..core.api_client import WppApiError
        from . import silk_encoder

        client = self._client(conv)
        # 安全闸：这是通道无关接口，调用方不一定是适配器（适配器在 _send_media 里
        # 已经过了一遍）。2026-10-19 审阅发现这里原先直接 open()，可绕过本地读取白名单。
        if not netguard.is_local_read_allowed(audio_path):
            raise ValueError(f"语音文件不在可读白名单: {str(audio_path)[:80]}")
        # 微信单条 ≤60s，且厂商卡 SILK 60 KB 字节上限 ⇒ 超长先切段
        segments = await silk_encoder.split_audio(audio_path)
        last = ""
        try:
            for seg in segments:
                b64 = await silk_encoder.encode_to_silk_fitting(seg)
                if b64 is None:
                    # 本机没有 silk 编码链 ⇒ 与适配器同款降级：发文件（老板 6-12 定的偏好）
                    last = await self.send_file(conv, seg)
                    continue
                # VoiceTime 传 0 会被厂商 Code=-2 拒收（2026-10-19 实测）⇒ 必须带真实毫秒数
                voice_ms = (await silk_encoder.probe_duration_ms(seg)
                            or await silk_encoder.probe_silk_base64_ms(b64))
                if voice_ms <= 0:
                    log.warning("[WPP] 语音段无法确认时长（VoiceTime=0 必被拒）—— 降级发文件")
                    last = await self.send_file(conv, seg)
                    continue
                try:
                    resp = await client.send_voice(conv.chat_id, b64, voice_time=voice_ms)
                except WppApiError as exc:
                    log.warning("[WPP] 语音段被厂商拒收（%s）—— 降级发文件", exc)
                    last = await self.send_file(conv, seg)
                    continue
                last = self._result(resp, "send_voice")
            return last
        finally:
            silk_encoder.cleanup_segments(segments)

    async def send_file(self, conv: ConversationKey, file_path: str) -> str:
        from ..core import netguard

        client = self._client(conv)
        src = os.path.expanduser(file_path)
        if not netguard.is_local_read_allowed(src):
            raise ValueError(f"文件不在可读白名单: {src[:80]}")
        with open(src, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        resp = await client.send_file_v2(conv.chat_id, os.path.basename(src), b64)
        return self._result(resp, "send_file")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "PLATFORM",
    "WPP_CAPABILITIES",
    "WppConversationStore",
    "WppOutboundSender",
    "dm_key",
    "group_key",
    "key_from_message",
]
