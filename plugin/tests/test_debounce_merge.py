"""到达层批次合并（``merge_text_batch`` + ``InboundDebouncer.merge``，2026-10-19）。

失效形态同样安静：**合并把不该合的合了**（几个群友的话拼成一段、带图的消息丢了附件、
长文被截断）或**该合的不合**（拆成三条碎片各回一次）。所以判据全下在"合成后的正文
与消息形状"上，而不是"merge 参数传了没"。
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass

from plugin.inbound.debounce import InboundDebouncer, merge_text_batch
from plugin.inbound.parser import MT_IMAGE, MT_TEXT, WppMessage
from plugin.neutral.inbound_merge import merge_text_batch as neutral_merge


def _sync(coro_fn):
    @functools.wraps(coro_fn)
    def _wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return _wrapper


def _msg(mid: str, content: str, *, mtype: int = MT_TEXT,
         media: dict | None = None, reply_to: dict | None = None,
         peer: str = "room@chatroom", frm: str = "u1") -> WppMessage:
    return WppMessage(
        account_id="acct", msg_id=mid, from_wxid=frm,
        chatroom_id=peer if peer.endswith("@chatroom") else "",
        content=content, msg_type=mtype, timestamp=int(mid.strip("m") or 0),
        media=media or {}, reply_to=reply_to or {},
    )


class _Sink:
    def __init__(self) -> None:
        self.batches: list[list[WppMessage]] = []

    async def __call__(self, batch: list[WppMessage]) -> None:
        self.batches.append(list(batch))


# ============================================================ 纯函数

def test_fragments_without_terminal_punctuation_are_glued() -> None:
    merged = merge_text_batch([
        _msg("m1", "我明天"),
        _msg("m2", "下午三点"),
        _msg("m3", "去北京"),
    ])
    assert len(merged) == 1
    assert merged[0].content == "我明天下午三点去北京"


def test_complete_sentences_are_joined_with_newlines() -> None:
    merged = merge_text_batch([
        _msg("m1", "在吗？"),
        _msg("m2", "问你个事。"),
    ])
    assert merged[0].content == "在吗？\n问你个事。"


def test_any_non_text_message_disables_merging() -> None:
    batch = [_msg("m1", "看这个"), _msg("m2", "[图片]", mtype=MT_IMAGE,
                                        media={"url": "http://x"})]
    assert merge_text_batch(batch) == batch


def test_reply_or_media_metadata_disables_merging() -> None:
    batch = [_msg("m1", "你看"), _msg("m2", "这个", reply_to={"svrid": "9"})]
    assert merge_text_batch(batch) == batch


def test_over_limit_is_not_merged_rather_than_truncated() -> None:
    batch = [_msg("m1", "啊" * 15), _msg("m2", "哦" * 15)]
    assert merge_text_batch(batch, max_chars=20) == batch


def test_merged_message_keeps_last_metadata_and_records_originals() -> None:
    batch = [_msg("m1", "第一句。"), _msg("m2", "第二句。")]
    merged = merge_text_batch(batch)
    assert merged[0].msg_id == "m2"                     # 最后一条（时间/回复锚点）
    assert [m["msgId"] for m in merged[0].raw["_merged"]] == ["m1", "m2"]
    assert batch[0].content == "第一句。"               # 原对象不被改


def test_single_message_is_returned_as_a_copy() -> None:
    batch = [_msg("m1", "只有一条")]
    out = merge_text_batch(batch)
    assert len(out) == 1 and out[0] is batch[0]


# ============================================================ 中立层（非 WPP 类型）

@dataclass
class _PlainMsg:
    """与 WPP 无关的通道消息：只靠字段形状被中立合并接受。"""
    msg_id: str
    content: str
    timestamp: int
    raw: dict


def test_neutral_merge_accepts_a_non_wpp_dataclass() -> None:
    batch = [_PlainMsg("m1", "我明天", 1, {}), _PlainMsg("m2", "下午三点", 2, {})]
    out = neutral_merge(batch, is_text_message=lambda m: True)
    assert len(out) == 1
    assert out[0].content == "我明天下午三点"
    assert out[0].msg_id == "m2"
    assert [m["msgId"] for m in out[0].raw["_merged"]] == ["m1", "m2"]
    assert batch[1].raw == {}                          # 原对象不被改


def test_neutral_merge_predicate_rejection_disables_merging() -> None:
    batch = [_PlainMsg("m1", "看这个", 1, {}), _PlainMsg("m2", "[图]", 2, {})]
    out = neutral_merge(batch, is_text_message=lambda m: m.msg_id != "m2")
    assert out == batch


# ============================================================ 聚合器接线

@_sync
async def test_debouncer_merges_a_flushed_batch_when_allowed() -> None:
    sink = _Sink()
    db = InboundDebouncer("acct", sink, interval_ms=10, merge=True,
                          merge_allowed=lambda batch: True)
    db.enqueue(_msg("m1", "我明天"))
    db.enqueue(_msg("m2", "下午三点去"))
    await asyncio.sleep(0.05)
    await db.flush_all()
    assert len(sink.batches) == 1
    assert [m.content for m in sink.batches[0]] == ["我明天下午三点去"]


@_sync
async def test_debouncer_keeps_batch_when_predicate_says_no() -> None:
    sink = _Sink()
    db = InboundDebouncer("acct", sink, interval_ms=10, merge=True,
                          merge_allowed=lambda batch: False)
    db.enqueue(_msg("m1", "一"))
    db.enqueue(_msg("m2", "二"))
    await asyncio.sleep(0.05)
    await db.flush_all()
    assert [m.msg_id for m in sink.batches[0]] == ["m1", "m2"]


@_sync
async def test_merge_off_is_byte_identical_to_old_behaviour() -> None:
    sink = _Sink()
    db = InboundDebouncer("acct", sink, interval_ms=10, merge=False,
                          merge_allowed=lambda batch: True)
    db.enqueue(_msg("m1", "一"))
    db.enqueue(_msg("m2", "二"))
    await asyncio.sleep(0.05)
    await db.flush_all()
    assert [m.msg_id for m in sink.batches[0]] == ["m1", "m2"]
