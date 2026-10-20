"""入站聚合（debounce）—— **到达层**，W7-6（移植 wpp-openclaw v1.11.0 ``src/inbound/debouncer.ts``，119 行）。

## 这一层解决的问题

同一个人在 500ms 里连发三条，今天 py2 会把三条当成三段**独立**的对话依次送进
``_handle_one``（``adapter.py`` 的 ``for msg in pipeline.extract(payload)``）——
每条都各自过一遍触发判定、心流打分、媒体下载。TS 那边同一场景是**合并成一批**
再处理（``debouncer.ts:60-73``：同 key 入队、每条重置定时器、到点一次性 flush）。

## ★ 本项是**保守移植**：只做"到达层聚合"，不做批式处理管线

TS 的 flush 产出一"批"交给 ``onFlush``，而那个回调里是**批量富化 + affection 循环 +
逐条 dispatch**（``handler.ts:222``）。py2 **没有**批处理管线 —— 把管线改成批式是另一个
量级的改动（要动 ``InboundPipeline``、富化、心流、派发全部接口），**不在本项范围**。
所以这里只做前半截：

* 同一 key 的消息在窗口内**合并成一个 batch**；
* flush 时把这个 batch 交给回调，**回调仍逐条走现有的 ``_handle_one``**（语义一字不动）。

⇒ 现场可观测的差别只有两条：① 派发最多晚 ``debounce_ms``；② 短时间内的多条会**连着**
派发（而不是每条各自等到自己那一刻）。**语义没有变成"批式"** —— 谁要 TS 那种批处理
语义（比如一次只看最后一条、或整批一起判触发），那是**另一项工作**，别在这里偷偷改。

## INVARIANT: W7-6（四条判据，缺一条这层聚合就是有害的）

1. **一条都不许丢**：拿不到事件循环 ⇒ ``enqueue`` 返 ``False``，**调用方必须自己直通**
   （不是"记个日志就算派发了"）；停机时先 ``flush_all`` 再停通道。缓冲器一旦成为
   消息的**终点**（而不是中转），表现就是"有些人说话机器人永远不理" —— 无法归因。
2. **同 key 严格保序**：缓冲批、直通消息、不同触发源（定时器 / 停机 flush）都排在
   **同一条链**上（``_chains``）。TS 那边没有这道保险（``flushBatch([msg])`` 是
   ``void`` 出去的，直通消息与缓冲批会**并发**跑）—— py2 今天 ``_on_raw`` 是
   ``await`` 逐条的 ⇒ 严格有序是**现有契约**，不能因为加了一层聚合就丢掉。
3. **直通三类逐字照抄**：``msgType`` 10000（系统）/ 34（语音 silk）/ 10002（撤回）
   （``debouncer.ts:50-55``）。它们不进缓冲 —— 撤回要立刻生效，语音的转写不该被窗口拖住。
4. **flush 失败只 warn，绝不抛**（``debouncer.ts:88-95``）：这是入站链路的旁路，
   一批派发失败不该把读帧循环带崩；失败由 ``on_error`` 回调（可选）另记。

## 与 TS 的三处有意不齐（都写在这里，别当成漏移植）

* **``isControlCommand`` 那个钩子**：TS 声明了它（``debouncer.ts:14``）却**没有任何调用方**
  （``handler.ts:222`` 只传 ``onFlush``）⇒ TS 侧那是**死参数**。py2 保留同一形状，并且
  **真的接上了一个**：``is_filehelper_session``（本树唯一的控制面通道 —— filehelper 管理
  命令；它们在 ``_maybe_command`` 里就被接管、**从不进 LLM**）。接上它的收益是
  ``/heartflow status`` 这类命令**恢复今天的零延迟**，代价是多一条直通例外。
* **key 里的 peer 口径**：TS 用 ``msg.peerKind``（``direct``/``group``）；py2 的
  ``WppMessage.peer_kind`` 是 ``dm``/``group``（W1 的映射表在 DB 边界做，见
  ``inbound/persist.py``）。key 只用于**自己跟自己比**，两种口径都行 ——
  但**不许**在这里改成 ``direct``：那会让 key 与 py2 自己的 peer 口径分叉。
* **直通消息会先"带上"同 key 已缓冲的那批**（合并成一次 flush，顺序仍是先来先派发）。
  TS 是并发跑（直通那条可能反超缓冲批）。py2 取"保序"一侧 —— 见 INVARIANT 第 2 条。

## 可选的批内合并（2026-10-19，默认关）

账号 JSON 的 ``debounceMerge.enabled`` 打开后，flush 前把同 key 的**纯文本**连续消息
合成一条（``merge_text_batch``）：私聊总是可合；群里**只在一批里有人 @/引用机器人时**
才合（其余群聊保持逐条，免得把互不相干的群友发言拼成一段）。非文本消息一律不合。
这不改变本模块"到达层聚合"的定位 —— 只是把"一整段话被拆成三条"的情况收成一条。

## 没做的一截（如实记下）

* **``clear()`` / ``size()``** 只服务测试与观测，适配器不用它们。
* **``timer.unref?.()``**（``debouncer.ts:72``，"不保活"）在 py2 结构性不需要：
  ``call_later`` 挂的是**事件循环自己的**定时器队列，不会阻止进程退出；停机由
  ``stop()`` 显式取消（TS 那行是 Node 的进程保活语义）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..neutral.inbound_merge import merge_text_batch as _merge_text_batch_neutral
from .parser import MT_REVOKE, MT_SYSTEM, MT_TEXT, MT_VOICE, WppMessage

log = logging.getLogger(__name__)

#: 聚合窗口（TS ``core/constants.ts:62`` ``DEFAULT_DEBOUNCE_MS`` —— 2026-08-22 项目约定
#: 由 1500ms 降到 500ms：webhook 到达延迟中位数 < 200ms，等 1.5s 纯属浪费）。
DEFAULT_DEBOUNCE_MS = 500

#: 直通（不进缓冲）的消息类型 —— 逐字对齐 ``debouncer.ts:50-55``。
PASSTHROUGH_TYPES: frozenset[int] = frozenset({MT_SYSTEM, MT_VOICE, MT_REVOKE})

#: ``on_flush`` / ``on_error`` 的签名。``on_flush`` 收一"批"（≥1 条，同 key、按到达顺序）。
FlushFn = Callable[[list[WppMessage]], Awaitable[Any]]
ErrorFn = Callable[[BaseException, list[WppMessage]], Any]

__all__ = [
    "DEFAULT_DEBOUNCE_MS",
    "PASSTHROUGH_TYPES",
    "InboundDebouncer",
    "debounce_key",
    "merge_text_batch",
]

def _is_mergeable_text(msg: WppMessage) -> bool:
    """WPP 口径的"纯文本"：正文类型 + 无媒体 + 无引用（合并的硬前提）。"""
    return msg.msg_type == MT_TEXT and not msg.media and not msg.reply_to


def merge_text_batch(batch: list[WppMessage], *, max_chars: int = 2000) -> list[WppMessage]:
    """WPP 适配层：把 WPP 的"纯文本"口径注入中立合并函数。

    纯逻辑（拼接、句末标点、超长不合并、``_merged`` 元数据）在
    :mod:`plugin.neutral.inbound_merge`；本函数只负责判定哪些消息算纯文本。
    """
    return _merge_text_batch_neutral(
        batch, max_chars=max_chars, is_text_message=_is_mergeable_text,
    )


def debounce_key(msg: WppMessage) -> str:
    """聚合键：``accountId:peerKind:peerId:fromWxid``（``debouncer.ts:43-45``）。

    **四个字段都要**，少一个都会把两段不同的对话合成一批：

    * 去掉 ``accountId`` ⇒ 多账号同群消息互串；
    * 去掉 ``peerId`` ⇒ 同一个人的私聊与群聊合并；
    * 去掉 ``fromWxid`` ⇒ 群里两个人连着说话变成"一批"（那正是要分开的两种语境）。
    """
    return f"{msg.account_id}:{msg.peer_kind}:{msg.peer_id}:{msg.from_wxid}"


class InboundDebouncer:
    """单账号的到达层聚合器。**构造即待命**，``enqueue`` 入队、``stop`` 停机。

    依赖全部注入（``on_flush`` / ``is_control_command`` / ``on_error``）⇒ 纯逻辑能在本地
    真跑；适配器那边只负责接线与起停（与 ``core/sync_fallback.SyncFallback`` 同款）。
    """

    def __init__(
        self,
        account_id: str,
        on_flush: FlushFn,
        *,
        interval_ms: int = DEFAULT_DEBOUNCE_MS,
        is_control_command: Callable[[WppMessage], bool] | None = None,
        on_error: ErrorFn | None = None,
        merge: bool = False,
        merge_allowed: Callable[[list[WppMessage]], bool] | None = None,
        merge_max_chars: int = 2000,
    ) -> None:
        self.account_id = account_id
        self.on_flush = on_flush
        #: 聚合窗口。``0`` 是**合法的**（下一个事件循环轮回就 flush）—— 那是给测试用的，
        #: 不是配置语义（账号文件里的 ``debounceMs`` 由 ``core.accounts`` 解析）。
        self.interval_ms = max(0, int(interval_ms))
        self.is_control_command = is_control_command
        self.on_error = on_error
        #: 批次合并（2026-10-19）：默认关；``merge_allowed`` 由适配器按账号配置 +
        #: 会话类型（私聊 / 群里 @ 了机器人）判定，本模块只做文本拼接。
        self.merge = bool(merge)
        self.merge_allowed = merge_allowed
        try:
            self.merge_max_chars = max(1, int(merge_max_chars))
        except (TypeError, ValueError):
            self.merge_max_chars = 2000
        self._buffers: dict[str, list[WppMessage]] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._chains: dict[str, asyncio.Task[None]] = {}
        self._stopped = False

    # ------------------------------------------------------------------ 观测

    @property
    def size(self) -> int:
        """当前缓冲的条数（测试 + 监控）。"""
        return sum(len(v) for v in self._buffers.values())

    @property
    def pending_keys(self) -> list[str]:
        return sorted(self._buffers)

    @property
    def stopped(self) -> bool:
        return self._stopped

    # ------------------------------------------------------------------ 入队

    def enqueue(self, msg: WppMessage) -> bool:
        """入队。**返回"这一条被谁接手"**：``True`` = 聚合器负责派发；``False`` = 调用方**必须**自己直通。

        返 ``False`` 的两种情形（都不许当成"已经派发了"）：

        * 没有运行中的事件循环（同步上下文误调 —— 定时器根本排不上）；
        * 已经 ``stop()``（通道本该也停了，但真收到就只能直通，**绝不静默丢掉**）。
        """
        if self._stopped:
            log.warning("[WPP:%s DEBOUNCE] 已停机还收到消息（%s）⇒ 交回调用方直通",
                        self.account_id, msg.msg_id)
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("[WPP:%s DEBOUNCE] 无运行中的事件循环 ⇒ 交回调用方直通（%s）",
                        self.account_id, msg.msg_id)
            return False

        key = debounce_key(msg)
        # 先取出同 key 已缓冲的那批（若有）：直通消息要**排在它们后面**，不然后到的
        # 反而先派发（INVARIANT 第 2 条）。
        pending = self._take(key)
        if self._is_passthrough(msg):
            self._schedule(key, pending + [msg])
            return True

        pending.append(msg)
        self._buffers[key] = pending
        # 每条入队都**重置**定时器（``debouncer.ts:65-70``）：窗口是"最后一条之后的
        # ``interval_ms``"，不是"第一条之后的" —— 否则连发三条会被切成 3 批。
        self._timers[key] = loop.call_later(self.interval_ms / 1000.0, self._fire, key)
        return True

    def _is_passthrough(self, msg: WppMessage) -> bool:
        if msg.msg_type in PASSTHROUGH_TYPES:
            return True
        return bool(self.is_control_command is not None and self.is_control_command(msg))

    # ------------------------------------------------------------------ 出队

    def _take(self, key: str) -> list[WppMessage]:
        """取走该 key 的缓冲并**取消它的定时器**（取走 = 这批已归调用方）。"""
        handle = self._timers.pop(key, None)
        if handle is not None:
            handle.cancel()
        return self._buffers.pop(key, [])

    def _fire(self, key: str) -> None:
        """定时器到点（事件循环回调，**同步**）：取批 → 排链。"""
        batch = self._take(key)
        if batch:
            self._schedule(key, batch)

    def _schedule(self, key: str, batch: list[WppMessage]) -> None:
        """把一批挂到该 key 的**串行链**上（INVARIANT 第 2 条）。

        ⚠️ 不能直接 ``await`` 也不能各起各的 task：同 key 的两批并发跑 ⇒ 派发顺序
        不再等于到达顺序（今天的 ``_on_raw`` 是 ``await`` 逐条的，那是有序的）。

        ⚠️ ``prev`` 可能是**已取消**的任务（``stop()`` 之后又被派了一批）—— 那也要接着跑，
        所以用 ``gather(..., return_exceptions=True)`` 而不是裸 ``await``：裸 ``await``
        会把上一条的 ``CancelledError`` 冒到这一条上，**这一批就被静静跳过了**。
        """
        prev = self._chains.get(key)
        task = asyncio.create_task(self._run_chain(prev, key, batch),
                                   name=f"wpp-debounce-{self.account_id}-{key}")
        self._chains[key] = task
        task.add_done_callback(lambda t, k=key: self._forget(k, t))

    def _forget(self, key: str, task: asyncio.Task[None]) -> None:
        if self._chains.get(key) is task:      # 只有当它还是"链尾"时才摘
            self._chains.pop(key, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # pragma: no cover —— ``_flush_batch`` 自己已经吞过一遍
            log.warning("[WPP:%s DEBOUNCE] 派发链异常: %s", self.account_id, exc)

    async def _run_chain(self, prev: asyncio.Task[None] | None, key: str,
                         batch: list[WppMessage]) -> None:
        if prev is not None:
            await asyncio.gather(prev, return_exceptions=True)
        await self._flush_batch(batch)

    async def _flush_batch(self, batch: list[WppMessage]) -> None:
        """真的把一批交出去。**只 warn 不抛**（INVARIANT 第 4 条）。

        合并（可选，默认关）在这里做：``_flush_inbound`` 仍然逐条派发，只是批里
        可能只剩一条合成消息 —— 逐条派发的形状不变（见 ``test_debounce_wiring``）。
        """
        if not batch:
            return
        try:
            if self.merge and len(batch) > 1 and (
                self.merge_allowed is None or self.merge_allowed(batch)
            ):
                batch = merge_text_batch(batch, max_chars=self.merge_max_chars)
            await self.on_flush(batch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 旁路不许把读帧循环带崩
            log.warning("[WPP:%s DEBOUNCE] 一批 %d 条派发失败: %s",
                        self.account_id, len(batch), exc)
            if self.on_error is not None:
                try:
                    self.on_error(exc, batch)
                except Exception as exc2:  # noqa: BLE001
                    log.warning("[WPP:%s DEBOUNCE] on_error 回调自己也炸了: %s",
                                self.account_id, exc2)

    # ------------------------------------------------------------------ 停机

    async def flush_key(self, key: str) -> int:
        """立刻派发该 key 的缓冲（取消定时器）。返派发条数。"""
        batch = self._take(key)
        if not batch:
            return 0
        self._schedule(key, batch)
        return len(batch)

    async def flush_all(self) -> int:
        """把**所有** key 的缓冲排出去，并**等它们跑完**（``debouncer.ts:98-104``）。

        ⚠️ 必须 ``await`` 到链跑完：调用方（``disconnect()``）紧接着就要停 WS / 关
        client / 清管线，而这批消息的派发要用到它们 —— 只"排上队"就等于停机时丢消息。
        返派发条数。
        """
        total = 0
        for key in list(self._buffers):
            total += await self.flush_key(key)
        # 等在跑的整条链（包括先前直通消息排下的）跑完
        pending = list(self._chains.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        log.debug("[WPP:%s DEBOUNCE] flush_all：派出 %d 条", self.account_id, total)
        return total

    async def stop(self, *, flush: bool = True) -> int:
        """停机：**先 flush 再清**（顺序不能反 —— 见 ``flush_all`` 的说明）。

        之后 ``enqueue`` 一律返 ``False``（把消息交回调用方，而不是塞进一个再也不会
        被 flush 的缓冲里）。

        ⚠️ ``_stopped`` 必须在 ``await flush_all()`` **之前**置位：flush 是异步的，
        等待期间到达的消息若还被接收入缓冲，会落在 ``flush_all`` 的快照之外，随后
        被下面的 ``clear()`` 静默丢掉。提前置位后它们走 ``enqueue → False`` 的直通
        路径，由调用方自己派发（与"已停机还收到消息"同一契约）。
        """
        self._stopped = True
        total = await self.flush_all() if flush else 0
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        self._buffers.clear()
        return total

    def clear(self) -> None:
        """丢掉全部缓冲与定时器（**测试用**；生产停机走 :meth:`stop`）。"""
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        self._buffers.clear()
