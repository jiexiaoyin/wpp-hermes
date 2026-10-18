"""W7-6 · 入站到达层聚合（``plugin/inbound/debounce.py``）。

## 这一面的失效形态（为什么判据都下在"盘上/链上真的发生了什么"）

聚合器是**消息的中转站**：它一旦变成**终点**，现场表现就只有"有些人说话机器人永远
不理" —— 没有异常、没有告警、日志里也不会有一行（今天的入站路径本来就是"没触发就不
出声"）。四种坏法尤其安静：

* **静默吞掉**：``enqueue`` 拿不到事件循环却照样返 True ⇒ 那一条永远躺在缓冲里；
* **停机丢批**：``disconnect`` 不 flush（或只"排上队"不等跑完）⇒ 缓冲区里那 500ms 内
  刚到的一批消息随进程消失；
* **窗口不重置**：连发三条被切成三批 ⇒ "合并"这个能力等于没有（而所有单条用例照样绿）；
* **并发乱序**：直通消息与缓冲批各起各的 task ⇒ **派发顺序 ≠ 到达顺序**，而 ORDER 在
  这条链路上是有语义的（`/affection on` 之后紧跟 `/affection off`）。

⇒ 断言全是"哪几条、按什么顺序、在第几拍到的"，不是"调用了没"。
"""

from __future__ import annotations

import asyncio
import functools
import time

import pytest

from plugin.inbound.debounce import (
    DEFAULT_DEBOUNCE_MS, PASSTHROUGH_TYPES, InboundDebouncer, debounce_key,
)
from plugin.inbound.parser import (MT_REVOKE, MT_SYSTEM, MT_TEXT, MT_VOICE, WppMessage)

#: 窗口用例的等待上限（秒）—— **不是超时容忍**，是防止变异体把门禁吊死
#: （同 ``test_sync_fallback._LOCK_PROBE_S`` 的理由：整批串行跑，挂一次全停）。
_PROBE_S = 3.0


def _sync(coro_fn):
    """本仓没有 pytest-asyncio（见 ``test_affection.py`` 同款说明）⇒ 手工 ``asyncio.run``。"""
    @functools.wraps(coro_fn)
    def _wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return _wrapper


def _msg(mid: str, *, account: str = "acct-1", peer: str = "room@chatroom",
         frm: str = "u1", mtype: int = MT_TEXT, content: str = "hi",
         peer_kind: str = "group") -> WppMessage:
    """造一条入站消息。``peer_kind="dm"`` 时不带群 id（私聊的 ``peer_id`` 就是发送者）。"""
    return WppMessage(
        account_id=account, msg_id=mid, from_wxid=frm,
        chatroom_id=peer if peer_kind == "group" else "",
        content=content, msg_type=mtype, timestamp=0,
    )


class _Sink:
    """假回调：记录**每一批的 msgId 序列**与"什么时候跑完"（顺序判据就看它）。"""

    def __init__(self, *, gate: asyncio.Event | None = None, boom: bool = False) -> None:
        self.batches: list[list[str]] = []
        self.order: list[str] = []          # 全部 msgId 的**派发完成顺序**
        self.gate = gate
        self.boom = boom
        self.live = 0                        # 当前并发在跑的批数
        self.max_live = 0

    async def __call__(self, batch: list[WppMessage]) -> None:
        self.batches.append([m.msg_id for m in batch])
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.boom:
                raise RuntimeError("派发炸了")
            self.order.extend(m.msg_id for m in batch)
        finally:
            self.live -= 1


# ============================================================ §1 聚合键

def test_the_key_carries_all_four_fields():
    """四个字段少一个都会把两段不同的对话合成一批（``debouncer.ts:43-45``）。"""
    base = _msg("m1")
    assert debounce_key(base) == "acct-1:group:room@chatroom:u1"

    for changed in (_msg("m1", account="acct-2"), _msg("m1", peer="other@chatroom"),
                    _msg("m1", frm="u2"), _msg("m1", peer_kind="dm")):
        assert debounce_key(changed) != debounce_key(base), \
            f"改一个字段就该换一个 key：{debounce_key(changed)} != {debounce_key(base)}"


def test_the_key_keeps_py2_s_own_peer_kind_wording():
    """py2 的 ``peer_kind`` 是 ``dm``（TS 叫 ``direct``）—— key 里**不许**改口径。

    key 只用于自己跟自己比，两种写法都"能跑"；但改了它，这个 key 就与树里其它地方
    的 peer 口径分叉（W1 的 ``db_peer_kind`` 才有权把 ``dm`` 映射成 ``direct``）。
    """
    assert "dm" in debounce_key(_msg("m1", peer_kind="dm"))
    assert "direct" not in debounce_key(_msg("m1", peer_kind="dm"))


def test_the_passthrough_set_is_exactly_the_three_types_from_ts():
    """直通三类**逐字**对齐 ``debouncer.ts:50-55``（多一个少一个都是行为分叉）。"""
    assert PASSTHROUGH_TYPES == frozenset({10000, 34, 10002})
    assert (MT_SYSTEM, MT_VOICE, MT_REVOKE) == (10000, 34, 10002)
    assert DEFAULT_DEBOUNCE_MS == 500


# ============================================================ §2 合并 / 窗口

@_sync
async def test_three_messages_in_one_window_come_out_as_one_batch_in_order():
    """同一 key 的三条 → **一批**，且顺序 = 到达顺序（这是本项的核心能力）。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=0)
    for mid in ("m1", "m2", "m3"):
        assert db.enqueue(_msg(mid)) is True

    assert db.size == 3 and db.pending_keys == ["acct-1:group:room@chatroom:u1"]
    await asyncio.sleep(0.02)
    assert sink.batches == [["m1", "m2", "m3"]]
    assert db.size == 0, "flush 之后缓冲必须空（不然下一批会把它再发一次）"


@_sync
async def test_the_window_restarts_on_every_enqueue():
    """★ 窗口是"**最后一条**之后的 interval"，不是"第一条之后的"。

    坏法（不重置定时器）让"连发三条"退化成三批 —— 而**每一条**单发用例照样全绿，
    只有这条判据能看出来。
    """
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=200)
    db.enqueue(_msg("m1"))
    await asyncio.sleep(0.12)
    db.enqueue(_msg("m2"))           # 到点时间应推到 0.32；不重置的话 0.20 就发了
    await asyncio.sleep(0.12)        # t≈0.24：过了旧到点、未到新到点
    assert sink.batches == [], "定时器没被重置：0.20 那一拍就已经派发出去了"
    assert db.size == 2

    await asyncio.sleep(0.25)        # t≈0.49 > 0.32
    assert sink.batches == [["m1", "m2"]], "窗口到点后必须**一批**出（不是两条各一批）"


@_sync
async def test_different_keys_never_merge():
    """两个不同的对话各自成批 —— 合在一起等于把两个人的话拼给同一个人。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=0)
    db.enqueue(_msg("a", frm="u1"))
    db.enqueue(_msg("b", frm="u2"))            # 同群不同人 ⇒ 不同 key
    db.enqueue(_msg("c", peer="other@chatroom"))
    db.enqueue(_msg("d", account="acct-2"))
    await asyncio.sleep(0.03)
    assert sorted(sink.batches) == [["a"], ["b"], ["c"], ["d"]], \
        f"四条各属不同 key，必须四批：{sink.batches}"


@_sync
async def test_two_keys_flush_independently_in_the_same_window():
    """同窗口内的两个 key：各自到点各自出，谁也别等谁。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=0)
    db.enqueue(_msg("a", frm="u1"))
    db.enqueue(_msg("b", frm="u2"))
    await asyncio.sleep(0.03)
    assert sorted(sink.batches) == [["a"], ["b"]]


@_sync
async def test_a_zero_window_still_goes_through_the_buffer():
    """窗口 0 也走完整条路（定时器 → 链），不是"直接派发"——否则测试测的不是真路径。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=0)
    db.enqueue(_msg("m1"))
    assert sink.batches == [], "窗口 0 也要等一拍事件循环（写死成同步派发就测不到缓冲）"
    await asyncio.sleep(0.02)
    assert sink.batches == [["m1"]]


# ============================================================ §3 直通

@pytest.mark.parametrize("mtype", [MT_SYSTEM, MT_VOICE, MT_REVOKE])
@_sync
async def test_the_three_types_bypass_the_window(mtype):
    """系统 / 语音 / 撤回**立刻**派发（``debouncer.ts:50-55``），一分钟都不等。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=60_000)
    assert db.enqueue(_msg("m1", mtype=mtype)) is True
    await asyncio.sleep(0.02)
    assert sink.batches == [["m1"]], f"msgType={mtype} 没有直通"
    assert db.size == 0


@_sync
async def test_a_plain_text_message_does_not_bypass():
    """反例：普通文本（1）**必须**进缓冲 —— 否则"合并"这个能力在同一条用例里被绕过。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=60_000)
    db.enqueue(_msg("m1", mtype=MT_TEXT))
    await asyncio.sleep(0.02)
    assert sink.batches == [] and db.size == 1


@_sync
async def test_the_control_command_hook_bypasses_and_receives_the_message():
    """控制面（py2 = filehelper 管理命令）走第三条例外。

    ⚠️ TS 声明了这个钩子却**没人传**（死参数）。py2 接上了 filehelper —— 收益是
    ``/heartflow status`` 这类命令恢复零延迟。判据：谓词**真的拿到了这条消息**，
    而且它的答复直接决定走哪条路（不是"只看类型"）。
    """
    sink = _Sink()
    seen: list[str] = []

    def is_ctl(msg: WppMessage) -> bool:
        seen.append(msg.msg_id)
        return msg.msg_id == "cmd"

    db = InboundDebouncer("acct-1", sink, interval_ms=60_000, is_control_command=is_ctl)
    db.enqueue(_msg("chat"))
    db.enqueue(_msg("cmd"))
    await asyncio.sleep(0.02)
    assert seen == ["chat", "cmd"], "谓词必须每条都问一次（问一次就缓存 = 后面全走同一条路）"
    assert sink.batches == [["chat", "cmd"]], \
        "直通那条要把同 key 已缓冲的一起带走（顺序仍是先来先派发）"
    assert db.size == 0, "缓冲必须被取走 —— 留着会被定时器再发一次"


@_sync
async def test_no_control_hook_means_type_only():
    """没给钩子时只有类型那三类直通（默认值不许把"什么都是命令"当默认）。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=60_000)
    db.enqueue(_msg("m1"))
    await asyncio.sleep(0.02)
    assert sink.batches == [] and db.size == 1


# ============================================================ §4 保序（INVARIANT 2）

@_sync
async def test_a_passthrough_waits_for_the_batch_already_in_flight():
    """★ 同 key 的两批**不许并发**：后一批要等前一批派发完。

    坏法（各起各的 task）在"派发本身很快"的用例里看不出来 —— 只有让回调**卡住**
    （闸门）才现形。这里断言：第一批发完之前，第二条压根没进来。
    """
    sink = _Sink(gate=asyncio.Event())
    db = InboundDebouncer("acct-1", sink, interval_ms=0)
    db.enqueue(_msg("slow"))                  # 这一批会卡在闸门上
    await asyncio.sleep(0.02)
    db.enqueue(_msg("fast", mtype=MT_VOICE))  # 直通，但不许插队
    await asyncio.sleep(0.02)
    assert sink.batches == [["slow"]], f"直通那条插队了：{sink.batches}"
    assert sink.max_live == 1, "同 key 的两批在并发跑 —— 顺序不再等于到达顺序"

    sink.gate.set()
    await asyncio.wait_for(asyncio.sleep(0.05), _PROBE_S)
    assert sink.batches == [["slow"], ["fast"]]
    assert sink.order == ["slow", "fast"]


@_sync
async def test_two_passthroughs_keep_their_arrival_order():
    """连着两条直通（比如连发两条语音）也按到达顺序派发。"""
    sink = _Sink(gate=asyncio.Event())
    db = InboundDebouncer("acct-1", sink, interval_ms=60_000)
    db.enqueue(_msg("v1", mtype=MT_VOICE))
    db.enqueue(_msg("v2", mtype=MT_VOICE))
    await asyncio.sleep(0.02)
    assert sink.batches == [["v1"]] and sink.max_live == 1
    sink.gate.set()
    await asyncio.wait_for(asyncio.sleep(0.05), _PROBE_S)
    assert sink.order == ["v1", "v2"]


@_sync
async def test_a_cancelled_predecessor_does_not_swallow_the_next_batch():
    """★ 前一条链**被取消**了，后一批也必须照跑（``_run_chain`` 用 ``gather`` 的理由）。

    坏法是裸 ``await prev``：上一条的 ``CancelledError`` 会冒到这一条上 ⇒ **这一批被
    静静跳过**（现场 = 那几条消息没人理，日志干净）。取消这条链的人可能是停机流程、
    也可能是将来某个超时包装 —— 谁取消的不重要，重要的是**消息不能跟着陪葬**。
    """
    sink = _Sink(gate=asyncio.Event())
    db = InboundDebouncer("acct-1", sink, interval_ms=0)
    db.enqueue(_msg("slow"))
    await asyncio.sleep(0.02)                       # slow 那一批已卡在闸门上
    key = debounce_key(_msg("slow"))
    chain = db._chains[key]
    chain.cancel()                                  # 停掉在跑的那条链
    db.enqueue(_msg("next"))                        # 同 key 的下一批
    await asyncio.sleep(0.02)
    assert sink.batches == [["slow"], ["next"]], \
        f"前一条链被取消，后一批被它带没了：{sink.batches}"

    sink.gate.set()
    await asyncio.wait_for(asyncio.sleep(0.05), _PROBE_S)
    assert "next" in sink.order, "后一批根本没跑完（被 CancelledError 吞了）"


# ============================================================ §5 一条都不许丢（INVARIANT 1）

def test_enqueue_outside_a_running_loop_says_so_instead_of_swallowing():
    """★ 没有事件循环时**返 False**（= 交回调用方直通）。

    坏法是"照样返 True"：那条消息躺在一个永远不会被 flush 的缓冲里 —— 现场只是
    "这条没人回"。同步上下文（脚本/工具面/将来某个非 async 入口）就会撞上这条。
    """
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=0)
    assert db.enqueue(_msg("m1")) is False, "没有循环还敢接 —— 这条消息就没人派发了"
    assert db.size == 0, "没接手就不许进缓冲"


@_sync
async def test_after_stop_enqueue_hands_the_message_back():
    """停机之后 ``enqueue`` 一律返 False：**不塞进一个再也不会 flush 的缓冲**。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=0)
    await db.stop()
    assert db.enqueue(_msg("m1")) is False
    assert db.size == 0 and db.stopped is True
    await asyncio.sleep(0.02)
    assert sink.batches == []


@_sync
async def test_every_message_is_dispatched_exactly_once():
    """端到端一条不重不漏：混合缓冲与直通，跑完之后**每条恰好出现一次**。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=0)
    for mid in ("m1", "m2", "m3"):
        db.enqueue(_msg(mid))
    db.enqueue(_msg("v1", mtype=MT_VOICE))
    await asyncio.sleep(0.05)
    flat = [m for b in sink.batches for m in b]
    assert sorted(flat) == ["m1", "m2", "m3", "v1"], f"重了或漏了：{sink.batches}"


# ============================================================ §6 停机 / flush_all

@_sync
async def test_flush_all_dispatches_and_waits_for_completion():
    """★ ``flush_all`` 必须**等派发跑完**（不是"排上队"就返回）。

    ``disconnect()`` 紧接着就要关 client / 清管线；只排队的话那批消息会拿着
    已经拆掉的依赖去跑（或者干脆永远跑不起来）。

    两件事一起判：①缓冲里剩的那些要派出去；②**在跑的那条链也要等**
    （它是先前直通消息排下的，``flush_all`` 只"排自己这一批"就会把它落在后面）。
    """
    sink = _Sink(gate=asyncio.Event())
    db = InboundDebouncer("acct-1", sink, interval_ms=60_000)
    db.enqueue(_msg("m1"))                    # 进缓冲
    db.enqueue(_msg("v1", mtype=MT_VOICE))    # 直通 ⇒ 立刻把 [m1, v1] 排上链（卡在闸门）
    db.enqueue(_msg("m2"))                    # 窗口 60s ⇒ 还在缓冲里
    task = asyncio.ensure_future(db.flush_all())
    await asyncio.sleep(0.02)
    assert not task.done(), "flush_all 在派发还没跑完时就返回了"
    assert sink.batches == [["m1", "v1"]], f"在跑的那批不该被 flush_all 挤掉：{sink.batches}"
    sink.gate.set()
    n = await asyncio.wait_for(task, _PROBE_S)
    assert n == 1, "返的应当是**这一次派出**的条数（在跑的那批不算）"
    assert sink.batches == [["m1", "v1"], ["m2"]]
    assert sink.order == ["m1", "v1", "m2"], "链是串行的：先前排的那批先跑完"


@_sync
async def test_flush_all_on_an_empty_engine_is_a_noop():
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=0)
    assert await db.flush_all() == 0
    assert sink.batches == []


@_sync
async def test_stop_flushes_by_default_and_stop_without_flush_drops():
    """``stop()`` 默认先 flush；显式 ``flush=False`` 才是"丢掉"（测试用）。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=60_000)
    db.enqueue(_msg("m1"))
    assert await db.stop() == 1
    assert sink.batches == [["m1"]]

    sink2 = _Sink()
    db2 = InboundDebouncer("acct-1", sink2, interval_ms=60_000)
    db2.enqueue(_msg("m1"))
    assert await db2.stop(flush=False) == 0
    assert sink2.batches == []


@_sync
async def test_stop_cancels_the_timer_so_nothing_fires_twice():
    """★ 停机后那个窗口到点**不许**再派发一次（那就是重复回复）。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=20)
    db.enqueue(_msg("m1"))
    await db.stop()                                 # flush 一次
    await asyncio.sleep(0.06)                       # 越过原窗口
    assert sink.batches == [["m1"]], f"定时器没被取消，重复派发了：{sink.batches}"


def test_clear_drops_everything_and_cancels_timers():
    """``clear()`` 是**测试用**的丢弃口（生产停机走 ``stop``）—— 它也必须取消定时器。"""
    async def _main():
        sink = _Sink()
        db = InboundDebouncer("acct-1", sink, interval_ms=10)
        db.enqueue(_msg("m1"))
        db.clear()
        assert db.size == 0 and not db.stopped
        await asyncio.sleep(0.05)
        assert sink.batches == []

    asyncio.run(_main())


# ============================================================ §7 失败只 warn（INVARIANT 4）

@_sync
async def test_a_failing_flush_only_warns_and_calls_on_error():
    """回调炸了：**不抛**、`on_error` 拿到 (异常, 批)、后面的批照跑。"""
    sink = _Sink(boom=True)
    errors: list[tuple[str, list[str]]] = []
    db = InboundDebouncer("acct-1", sink, interval_ms=0,
                          on_error=lambda e, b: errors.append((str(e), [m.msg_id for m in b])))
    db.enqueue(_msg("m1"))
    await asyncio.sleep(0.03)
    assert sink.batches == [["m1"]]
    assert errors == [("派发炸了", ["m1"])]

    db.enqueue(_msg("m2"))                          # 炸过之后链路还得活着
    await asyncio.sleep(0.03)
    assert sink.batches == [["m1"], ["m2"]]


@_sync
async def test_a_failing_on_error_callback_is_swallowed_too():
    """``on_error`` 自己炸了也不许冒到读帧循环上（它只是个旁路）。"""
    sink = _Sink(boom=True)

    def bad(_e, _b):
        raise RuntimeError("on_error 也炸了")

    db = InboundDebouncer("acct-1", sink, interval_ms=0, on_error=bad)
    db.enqueue(_msg("m1"))
    await asyncio.sleep(0.03)
    assert sink.batches == [["m1"]]                 # 到这儿就说明没抛出去


@_sync
async def test_a_failing_flush_does_not_break_the_chain_of_the_next_batch():
    """一批炸了，同 key 的下一批**照样**派发（链不能因为前一批抛错就断）。"""
    sink = _Sink(gate=asyncio.Event(), boom=True)
    db = InboundDebouncer("acct-1", sink, interval_ms=0)
    db.enqueue(_msg("m1"))
    await asyncio.sleep(0.02)
    sink.gate.set()
    await asyncio.sleep(0.02)
    sink.boom = False                               # 下一批不炸
    db.enqueue(_msg("m2"))
    await asyncio.sleep(0.03)
    assert sink.batches == [["m1"], ["m2"]], f"前一批抛错把后一批带没了：{sink.batches}"


# ============================================================ §8 观测面

@_sync
async def test_size_and_pending_keys_track_the_buffers():
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=60_000)
    assert db.size == 0 and db.pending_keys == []
    db.enqueue(_msg("m1"))
    db.enqueue(_msg("m2"))
    db.enqueue(_msg("x1", frm="u2"))
    assert db.size == 3
    assert db.pending_keys == ["acct-1:group:room@chatroom:u1", "acct-1:group:room@chatroom:u2"]
    db.clear()
    assert db.size == 0 and db.pending_keys == []


@_sync
async def test_flush_key_only_touches_that_key():
    """``flush_key`` 只动一个 key（其余照旧等窗口）。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=60_000)
    db.enqueue(_msg("m1", frm="u1"))
    db.enqueue(_msg("m2", frm="u2"))
    key = debounce_key(_msg("m1"))
    assert await db.flush_key(key) == 1
    await asyncio.sleep(0.02)
    assert sink.batches == [["m1"]]
    # 另一个 key 必须原样留在缓冲里（连**期望值**都要按它自己的发送者算 ——
    # 用默认 frm 拼出来的 key 是另一个 key，这条判据就变成同义反复了）
    assert db.size == 1 and db.pending_keys == [debounce_key(_msg("m2", frm="u2"))]
    assert await db.flush_key("nope") == 0


@_sync
async def test_the_interval_is_clamped_and_measures_wall_clock():
    """窗口按**墙上时间**走（``call_later`` 秒数 = ``interval_ms/1000``）。

    判据：60ms 的窗口在 ~60ms 内不该出，过了就该出 —— 顺便钉住"毫秒→秒"那次换算
    （漏了 `/1000` 会变成 60 秒，现场是"消息在 60 秒后才被处理"）。
    """
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=60)
    t0 = time.monotonic()
    db.enqueue(_msg("m1"))
    await asyncio.sleep(0.02)
    assert sink.batches == [], "60ms 的窗口在 20ms 就放行了（换算写错了？）"
    await asyncio.sleep(0.12)
    assert sink.batches == [["m1"]]
    assert time.monotonic() - t0 < 2.0, "窗口被当成秒了"


@_sync
async def test_a_negative_interval_is_clamped_to_zero():
    """脏配置不许让 ``call_later`` 抛（负数在 asyncio 里是 ValueError）。"""
    sink = _Sink()
    db = InboundDebouncer("acct-1", sink, interval_ms=-5)
    assert db.interval_ms == 0
    db.enqueue(_msg("m1"))
    await asyncio.sleep(0.02)
    assert sink.batches == [["m1"]]
