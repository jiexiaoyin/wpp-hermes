"""到达层批次合并的**纯函数**（扩展边界 P0-6，2026-10-19）。

从 ``inbound/debounce.py`` 抽出 ``merge_text_batch``。本模块不 import 任何 WPP
消息类型：是否算"可合并的纯文本"由调用方注入 ``is_text_message``，合成新消息由
``clone`` 注入（默认 ``dataclasses.replace``）。

消息对象只需满足 :class:`MergeableMessage` 的字段形状；WPP 的 ``WppMessage``
与任何通道自己的 dataclass 都可以直接传入。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, Protocol, TypeVar

M = TypeVar("M")


class MergeableMessage(Protocol):
    """合并所需的最小字段集（仅文档/类型用途，运行时靠鸭子类型）。"""

    msg_id: Any
    content: str | None
    timestamp: Any
    raw: Mapping[str, Any]


#: 句末标点：上一段以这些结尾 ⇒ 合并时换行（是两句完整的话）；
#: 否则视为被拆断的一句话，直接拼接（"我明天" + "下午三点去" → "我明天下午三点去"）。
_TERMINAL_PUNCT: tuple[str, ...] = ("。", "！", "？", "…", "!", "?", ".", "~", "～")


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith(_TERMINAL_PUNCT)


def merge_text_batch(
    batch: list[M],
    *,
    max_chars: int = 2000,
    is_text_message: Callable[[M], bool],
    clone: Callable[[M, str, dict[str, Any]], M] | None = None,
) -> list[M]:
    """把同 key 的一批**纯文本**消息合并成一条合成消息（轻量合并）。

    ``is_text_message`` 决定一条消息是否算"可合并的纯文本"（无媒体、无引用、
      正文非空）；调用方还要先用业务谓词判过"该不该合"（私聊 / 群里 @ 了机器人）。

    * 批里只要有一条不满足 ``is_text_message`` ⇒ 整批**不合并**（媒体各自有
      富化与落库路径，混进来会丢掉附件）；
    * 以句末标点分段：完整句子之间换行，被拆断的半句直接拼接；
    * 合并后超过 ``max_chars`` ⇒ 不合并（宁可多派发一次，也不截断用户的话）；
    * 合成消息取**最后一条**的元数据（msg_id / 时间 / 引用），原文进
      ``raw["_merged"]`` 便于排查。原消息对象**不改**（``clone`` / ``replace``
      造新对象）。

    返回长度为 1（合并）或原样 ``list(batch)``（不合并）。
    """
    if len(batch) < 2:
        return list(batch)
    texts: list[str] = []
    for msg in batch:
        if not is_text_message(msg):
            return list(batch)
        text = (getattr(msg, "content", "") or "").strip()
        if not text:
            return list(batch)
        texts.append(text)

    merged = texts[0]
    for text in texts[1:]:
        merged += ("\n" if _ends_sentence(merged) else "") + text
    try:
        limit = max(1, int(max_chars))
    except (TypeError, ValueError):
        limit = 2000
    if len(merged) > limit:
        return list(batch)

    last = batch[-1]
    raw = dict(getattr(last, "raw", None) or {})
    raw["_merged"] = [
        {
            "msgId": getattr(m, "msg_id", None),
            "content": getattr(m, "content", None),
            "timestamp": getattr(m, "timestamp", None),
        }
        for m in batch
    ]
    if clone is not None:
        return [clone(last, merged, raw)]
    return [replace(last, content=merged, raw=raw)]


__all__ = ["MergeableMessage", "merge_text_batch"]
