"""接龙（群接龙 / chat-history）识别与解析。

迁移自 TS ``inbound/relay.ts``。**这是红包误判事故的第一道闸** ——
:mod:`inbound.hongbao` 的 ``is_red_packet_message`` 第一句就是
``if is_relay_message(msg): return False``。

## 历史（三段，别只读一段）

- **v1.3.37**：只判 ``msgType === 53``（假设 chat-history）⇒ 真实 vendor 数据不符
  ⇒ 接龙**从不解析、从不触发**（集成 bug，测试用的是假设数据）。
- **v1.3.54**（2026-08-12 接总立）：真实接龙是 ``type=49``（app, category=app_message），
  title/content 以 ``#接龙`` 开头。改为 53 **或** 49+文本特征。
- **v1.9.1**（2026-09-27）：接龙文案**自带"红包"字样**（如"提升业绩, 领取红包🧧"），
  被关键词启发式误判成红包 ⇒ 整条静默丢弃且无报错。结构性识别必须优先于关键词。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: ``MT_APP``（**不写死 49**）—— 卡片类型只在 :mod:`inbound.parser` 定义一次，
#: 别处一律引用符号。写死数字迟早与 parser 漂移，而漂移的那一刻就是"两处对 49
#: 的理解不一致"（hongbao/handler 之间那类事故的通用形态）。
from .parser import MT_APP

#: 接龙条目的编号行（``1. xxx``）
_NUMBERED_ITEM_RE = re.compile(r"(?:^|\n)\s*\d+\.\s")


@dataclass
class RelayItem:
    """接龙列表里的一项。"""

    index: int                      # 1-based
    wxid: str = ""
    nickname: str = ""
    text: str = ""
    ts: int = 0


def is_relay_message(msg: Any) -> bool:
    """是不是接龙消息。

    判据（对齐 TS ``isRelayMessage``）：

    - ``msg_type == 53`` ⇒ 是（旧 chat-history，历史兼容）
    - ``msg_type != 49`` ⇒ 否（非 appmsg 卡片不可能是接龙）
    - ``msg_type == 49`` 且（正文/``raw.app.title`` 含 ``#接龙``，或含 `"接龙"` +
      编号条目 ``N. xxx``）⇒ 是

    ⚠️ **绝不用 ``msg_type == 49`` 单独判接龙** —— 实测华为群 49 里含
    转账 86 / 应用消息 44 / 接龙 135+，只看类型会把转账和应用消息也当接龙。
    """
    msg_type = getattr(msg, "msg_type", None)
    try:
        msg_type = int(msg_type)
    except (TypeError, ValueError):
        return False

    if msg_type == 53:
        return True
    if msg_type != MT_APP:
        return False

    raw = getattr(msg, "raw", None)
    app = raw.get("app") if isinstance(raw, dict) and isinstance(raw.get("app"), dict) else {}
    title = app.get("title") if isinstance(app.get("title"), str) else ""

    text = f"{getattr(msg, 'content', '') or ''} {title}"
    if "#接龙" in text:
        return True
    return "接龙" in text and bool(_NUMBERED_ITEM_RE.search(text))


__all__ = ["RelayItem", "is_relay_message"]
