"""通道无关的最小会话契约（扩展边界 P1，2026-10-19）。

四个类型是全插件里**唯一**允许被通道无关模块依赖的会话抽象：

* :class:`ConversationKey` —— 统一会话身份（平台 + 账号 + 会话类型 + 会话 id）。
* :class:`MessageRow` —— 读取历史时的最小行形状（不再暴露 ``WppMessage``）。
* :class:`ConversationStore` —— 读历史 / 数出站条数（同步，调用方按需 ``to_thread``）。
* :class:`OutboundSender` —— 发文本 / 语音 / 文件（异步，返回消息 id）。
* :class:`ChatCapabilities` —— 通道能力声明（群里发不发、语音格式等）。

**本模块不 import 任何通道实现**，也不规定 transport。WPP 的实现在
:mod:`plugin.platform.conversation`；第二个通道只需实现这两个 Protocol 并注入，
主动消息、画像、风格等策略层不需要知道它是微信还是企微。

约定：

* ``chat_type`` 用 ``"dm"`` / ``"group"``（其余值由通道自行扩展，策略层不解读）。
* ``MessageRow.direction`` 用 ``"incoming"`` / ``"outbound"``。
* ``send_*`` 返回**非空消息 id**；拿不到 id 时由实现决定返回空串还是抛错 ——
  调用方不得拿空串当"没发出去"（发送成功但 vendor 不回 id 是常见情况）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ConversationKey:
    """统一会话身份。**平台字段必须由通道实现填自己的名字**（不要填 ``wechat``）。"""

    platform: str
    account_id: str
    chat_type: str          # "dm" / "group"
    chat_id: str

    def __post_init__(self) -> None:
        if not self.platform or not self.account_id or not self.chat_id:
            raise ValueError("ConversationKey 的 platform/account_id/chat_id 都不能为空")
        if not self.chat_type:
            raise ValueError("ConversationKey.chat_type 不能为空")

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.platform, self.account_id, self.chat_type, self.chat_id)


@dataclass(frozen=True)
class MessageRow:
    """历史消息的最小形状（读取侧）。

    ``get()`` 让既有策略代码（以 ``Mapping`` 写的）不必为每个通道改一遍；
    新代码直接取属性即可。
    """

    direction: str          # "incoming" / "outbound"
    at_sec: int
    content: str
    sender_id: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


@runtime_checkable
class ConversationStore(Protocol):
    """会话历史读取。实现方负责把平台存储映射到 :class:`MessageRow`。"""

    def recent(self, conv: ConversationKey, limit: int = 20) -> list[MessageRow]:
        """按时间升序返回最近 ``limit`` 条（旧 → 新）。"""
        ...

    def count_outbound_since(self, conv: ConversationKey, since_sec: int) -> int:
        """统计 ``since_sec`` 之后的出站条数（含普通回复）。"""
        ...


@runtime_checkable
class OutboundSender(Protocol):
    """会话出站。实现方负责平台鉴权、分片、媒体格式。"""

    async def send_text(self, conv: ConversationKey, text: str, *,
                        quote_id: str | None = None) -> str: ...

    async def send_voice(self, conv: ConversationKey, audio_path: str) -> str: ...

    async def send_file(self, conv: ConversationKey, file_path: str) -> str: ...


@dataclass(frozen=True)
class ChatCapabilities:
    """通道能力声明。策略层用它决定"能不能做"，而不是猜平台名。"""

    groups: bool = False
    voice: bool = False
    quote: bool = False
    members: bool = False
    voice_format: str = ""      # "silk" / "mp3" / "ogg" / ""（不会发语音）

    def can_send(self, chat_type: str) -> bool:
        return chat_type != "group" or self.groups


__all__ = [
    "ChatCapabilities",
    "ConversationKey",
    "ConversationStore",
    "MessageRow",
    "OutboundSender",
]
