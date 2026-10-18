"""WS 增量兜底 + 厂商 HTTP 心跳（W5，移植 wpp-openclaw v1.11.0 ``src/ws-client.ts:203-282``）。

## 为什么 py2 只补"兜底"，不照 TS 把传输整体换掉

TS 的 WS **帧只是通知**（``ws-client.ts:165-168``：任何非握手帧都只触发一次
``/Msg/Sync``，真消息一律带游标从 ``/Msg/Sync`` 拉）。py2 反过来 —— 帧里**带着消息体**，
直接解析就真机可用。整体换成"帧只当通知"等于拿一条**正在工作**的入站链路去冒险，
所以本项只补缺口：**Ws 断开的那段窗口**（以及只发通知、不发体的那一类帧）漏掉的消息。

## INVARIANT: W5（四条判据，缺一条这条兜底就是假的）

1. **先派发、后前进游标**（``ws-client.ts:229-245`` 的 P1 教训）：崩溃（SIGKILL/OOM）
   窗口里消息还没派发、游标却已经前进 ⇒ 那批消息**永久丢失**。正确组合是
   **at-least-once + 下游幂等**（``wpp_messages`` 唯一键 / ``SeenTracker`` 按 msgId 去重），
   **不是**"少拉点"。
2. **首次（库里没有游标）只建立游标、不回放历史** —— 见下面「为什么不敢回放」。
3. **一次只拉一趟**（``syncInFlight`` 锁，``:209-213``）：WS 推送风暴 / 兜底定时器 /
   开连三种触发会**同时**来；并发双拉既打厂商，也让两个游标互相踩。
4. **任何失败只 warn，绝不抛**：这是入站链路的旁路 —— 兜底失败不该把收信搞停
   （``:249-250`` 同款 catch）。

## 为什么首次不敢回放（**与 TS 有意不齐**，这条比"补齐能力"重要）

TS 敢把首次全量拉回来的消息**全部派发**，是因为它那条入站链路上有一道
**DB 去重**（``ws-client.ts`` 注释里的 "dedup 每条独立 DB 查询"：重放的消息在
``wpp_messages`` 里已存在 ⇒ 在**决定回不回**之前就被丢掉）。**py2 没有这道门** ——
``InboundPipeline.seen`` 是 ``SeenTracker``（``parser.py:515-523`` 自己写明"**进程内**
缓存"），adapter 的 ``self._dedup`` 是 ``MessageDeduplicator``，两者**都随进程重启清空**。

⇒ py2 上把首次全量倒回来的历史当新消息派发，后果是它们会走完
``_handle_one`` → 心流/触发 → **机器人去回复历史消息**。TS 注释里 19:56 那次
"1458 条重放"就是这个形状；在 py2 上它不是性能问题，是**发错消息**的问题。

所以首次拉取（``prev`` 为空 —— **仅指"库里真没游标"**）**只把返回的 ``KeyBuf``
存下来当基线，一条都不派发**。代价是"进程启动到首次拉取之间"那个窗口不补 ——
而那个窗口由 WS 帧（主通道）与 webhook 覆盖，实际为 0。**收益是：新部署/新账号
第一次跑起来不会翻旧账。**

⚠️ **读库失败不许走这条"首次"路**（``_READ_FAILED`` 哨兵）：库抖一下就把上一窗
当成历史跳过、还把基线覆盖掉 —— 那是一条静默丢消息的路。失败的那一轮直接跳过，
下拍重试。

⚠️ **残留窗口（诚实记下，没关掉）**：incremental 那条路上，若在"派发完但还没存游标"
的几微秒里崩溃，重启后同一批会被**再派发一次** —— 而重启后内存去重是空的 ⇒ 理论上有
"重复回复"的可能。这是 at-least-once 的固有代价，真正的解法是给入站链路加一道
**DB seen 门**（与 W1 的 ``wpp_messages`` 同表，属 W1/W8 范围），不在本项里假装关掉。

## 与 TS 的三处有意不齐（都写在这里，别当成漏移植）

* **触发面**：TS 在**每一帧**上触发拉取；py2 只在 ``sync_update`` 这类**不带消息体**的帧上
  触发（其余帧自己就带体，re-拉纯属重复劳动）。
* **心跳主力**：TS 的 ``sendHeartbeat`` 里 ``ws.ping()`` 是保活主力；py2 的
  ``websockets.connect(ping_interval=20)`` **天生每 20s 发原生 ping** ⇒ 这一半**结构性
  不需要移植**。本模块只做 TS 那半"双保险"的 HTTP 心跳（``/Login/HeartBeatLong``）。
* **定时器归属**：TS 把两个 ``setInterval`` 挂在 WS 客户端上；py2 挂在本模块、由适配器
  随账号起停 —— 这样"拉取/心跳"不必反过来依赖 WS 客户端，纯逻辑也就能在**本地真跑**
  （不必用 AST 形状判据去猜）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)

#: 兜底拉取周期（TS ``fallbackSyncMs`` 默认 60_000）
DEFAULT_INTERVAL_S = 60.0
#: 开连那次触发的原因标签（与 TS ``triggerSync("ws-open")`` 同名）
REASON_OPEN = "ws-open"
#: 带着"有新消息"通知的帧触发（TS ``triggerSync("ws-push")``）
REASON_PUSH = "ws-push"
#: 周期兜底触发（TS ``triggerSync("fallback-timer")``）
REASON_TIMER = "fallback-timer"

#: 读游标失败的哨兵 —— **不能**与"库里真没游标"(None) 混为一谈：
#: 前者跳过本轮（下拍照来），后者才走"首次只落基线"。混在一起时库抖一下
#: 就会把一整窗消息当成"首次"吞掉（只存游标、不派发）。
_READ_FAILED = object()


def extract_sync_items(resp: Mapping[str, Any] | None) -> list[dict]:
    """从 ``/Msg/Sync`` 响应里取消息列表（TS ``sync?.Data?.CmdList?.List ?? []``）。

    ⚠️ **取不到就是空列表，不许抛**：`/Msg/Sync` 在"没有新消息"时就返回空 CmdList，
    那是**正常路径**，不是异常。形状容错只到"三种已知摆法"为止（``List`` / 直接是数组 /
    ``Data.List``）—— vendor 真换了形状要能**看出来**（日志里 items=0 且游标不动），
    不是靠这里猜。
    """
    data = (resp or {}).get("Data")
    if not isinstance(data, Mapping):
        return []
    cmd = data.get("CmdList")
    if isinstance(cmd, Mapping):
        arr = cmd.get("List")
        if isinstance(arr, list):
            return [x for x in arr if isinstance(x, dict)]
        return []
    if isinstance(cmd, list):
        return [x for x in cmd if isinstance(x, dict)]
    arr = data.get("List")
    if isinstance(arr, list):
        return [x for x in arr if isinstance(x, dict)]
    return []


def extract_sync_key(resp: Mapping[str, Any] | None) -> str:
    """从 ``/Msg/Sync`` 响应里取新游标（TS ``sync?.Data?.KeyBuf?.buffer``）。

    ``buffer`` 是 Go ``[]byte`` 的 base64（vendor 侧 ``KeyBuf``）；取不到返空串
    ⇒ **调用方不许用空串覆盖已有游标**（那会把"增量"降级成"每次全量重放"）。
    """
    data = (resp or {}).get("Data")
    if not isinstance(data, Mapping):
        return ""
    kb = data.get("KeyBuf")
    if isinstance(kb, str):
        return kb
    if isinstance(kb, Mapping):
        buf = kb.get("buffer")
        return buf if isinstance(buf, str) else ""
    return ""


class SyncFallback:
    """单账号的增量兜底 + 心跳。**构造即待命，``start()`` 才起两个 60s 定时器。**

    依赖全部注入（``api`` / ``on_message`` / ``get_key`` / ``set_key``），
    所以能在本地用假的 api 与假的库真跑；适配器那边只负责把它们接上。
    """

    def __init__(
        self,
        account_id: str,
        api: Any,
        on_message: Callable[[dict], Awaitable[None]],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        is_connected: Callable[[], bool] | None = None,
        get_key: Callable[[str], str | None] | None = None,
        set_key: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.account_id = account_id
        self.api = api
        self.on_message = on_message
        self.interval_s = float(interval_s)
        #: 心跳只在连着的时候发（TS ``if (this.stopped || !this.connected ...) return``）
        self.is_connected = is_connected or (lambda: True)
        self._get_key = get_key
        self._set_key = set_key
        self._in_flight = False
        self._timers: list[asyncio.Task[None]] = []
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ 观测

    @property
    def in_flight(self) -> bool:
        """是否有一趟拉取正在跑（并发触发的判据，也是测试的观测点）。"""
        return self._in_flight

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._timers)

    # -------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """起两个定时器（幂等）：兜底拉取 + HTTP 心跳。

        ``interval_s <= 0`` ⇒ **不起定时器**（TS ``if (this.fallbackSyncMs > 0)`` 同款）。
        """
        if self.interval_s <= 0 or self.running:
            return
        self._timers = [
            asyncio.create_task(self._tick_loop(self.trigger, REASON_TIMER),
                                name=f"wpp-sync-timer-{self.account_id}"),
            # ⚠️ 心跳不吃 reason（它是"该不该发"由 is_connected 决定，不是"为什么发"）
            asyncio.create_task(self._tick_loop(lambda _r: self.heartbeat(), "heartbeat-timer"),
                                name=f"wpp-heartbeat-{self.account_id}"),
        ]

    async def stop(self) -> None:
        """停定时器 + 掐掉在跑的触发任务（``disconnect()`` 调它）。"""
        timers, self._timers = self._timers, []
        for t in timers:
            t.cancel()
        for t in timers:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: S110, BLE001 —— noqa: BLE001
                pass
        pending, self._tasks = self._tasks, set()
        for t in pending:
            t.cancel()

    async def _tick_loop(self, action: Callable[[str], Awaitable[Any]], reason: str) -> None:
        """每 ``interval_s`` 调一次 ``action(reason)``；失败只 warn（下一拍照来）。"""
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await action(reason)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 定时器不许被一次失败打死
                log.warning("[WPP:%s SYNC] 定时任务 %s 失败: %s", self.account_id, reason, exc)

    # ------------------------------------------------------------------ 事件

    def spawn(self, reason: str) -> None:
        """**非阻塞**触发（等价 TS 的 ``void this.triggerSync(...)``）。

        ⚠️ WS 的读循环是 ``async for raw in ws: await self._dispatch(raw)`` ——
        在那里直接 ``await`` 一趟 HTTP 拉取会把**收帧**也堵住（快群能把读循环压成
        "一条一拉"）。所以触发一律走 ``create_task``，并且挂 done 回调把异常喊出来
        （否则就是 "Task exception was never retrieved" 那种静默）。
        """
        try:
            task = asyncio.create_task(self._run(reason),
                                       name=f"wpp-sync-{self.account_id}-{reason}")
        except RuntimeError as exc:  # 没有运行中的事件循环（同步上下文误调）
            log.warning("[WPP:%s SYNC] 触发失败（无事件循环）: %s", self.account_id, exc)
            return
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # pragma: no cover —— trigger 自己已经吞过一遍
            log.warning("[WPP:%s SYNC] 触发任务异常: %s", self.account_id, exc)

    async def _run(self, reason: str) -> int:
        if reason == REASON_OPEN:
            await self.auto_heartbeat()
        return await self.trigger(reason)

    # ------------------------------------------------------------------ 拉取

    async def trigger(self, reason: str) -> int:
        """拉一趟 ``/Msg/Sync`` 并**逐条派发**，最后才前进游标。返回派发条数。

        首次（没有游标）**只建立游标、一条都不派发** —— 理由见模块 docstring
        「为什么首次不敢回放」。失败一律 warn + 返 0（``INVARIANT: W5`` 第 4 条），**永不抛**。
        """
        if self._in_flight:
            log.debug("[WPP:%s SYNC] 已有拉取在跑，跳过（reason=%s）", self.account_id, reason)
            return 0
        self._in_flight = True
        try:
            prev = await self._read_cursor()
            if prev is _READ_FAILED:
                # ⚠️ 「读库失败」≠「库里没有游标」：前者按首次同步处理会把**上一窗**
                #    消息当成历史静默跳过（游标还会被基线覆盖）。库抖一下就丢一窗，
                #    而这条链本身不出声。⇒ 本轮不动、下拍重试（WS 主通道照常收信）。
                log.warning("[WPP:%s SYNC] 读游标失败（本轮跳过，下拍重试；reason=%s）",
                            self.account_id, reason)
                return 0
            resp = await self.api.sync_message(prev or "")
            items = extract_sync_items(resp)
            new_key = extract_sync_key(resp)
            if not prev:
                # ⚠️ INVARIANT 第 2 条：首次只落基线，**不回放**（py2 的入站去重只在内存里，
                #    倒回来的历史会走完 _handle_one → 触发 → 回复旧消息）。
                if new_key:
                    await self._write_cursor(new_key)
                    log.info("[WPP:%s SYNC] 首次同步：只建立游标（厂商回了 %d 条历史，"
                             "已跳过不回放；reason=%s）", self.account_id, len(items), reason)
                else:
                    log.warning("[WPP:%s SYNC] 首次同步但厂商未返回 KeyBuf ⇒ 游标仍空，"
                                "兜底暂不派发（WS 帧那条路不受影响；reason=%s）",
                                self.account_id, reason)
                return 0
            log.debug("[WPP:%s SYNC] 拉回 %d 条（reason=%s synckey=incremental）",
                      self.account_id, len(items), reason)
            failed: list[str] = []
            for item in items:
                try:
                    await self.on_message(item)
                except Exception as exc:  # noqa: BLE001 —— 单条坏消息不许断整批
                    failed.append(str((item or {}).get("MsgId")
                                      or (item or {}).get("NewMsgId") or "?"))
                    log.warning("[WPP:%s SYNC] 派发失败（继续下一条）: %s", self.account_id, exc)
            # ⚠️ 游标最后动（INVARIANT 第 1 条）：上面每一条都已经派发完才走到这里。
            # ★ 2026-10-18 审阅：**这里是有意的取舍** —— 派发失败也照走游标，宁可丢
            #   这一条也不堵整条兜底通道（`tests/test_sync_fallback.py` 用
            #   ``test_a_failed_dispatch_..._nor_block_the_cursor`` 钉住了它）。
            #   代价是这条消息既没重试、也没进可重放队列 ⇒ 永久丢失，所以日志必须是
            #   ERROR 且带得上 msg_id（旧实现只 warn，现场等于查不到）。
            if failed:
                log.error("[WPP:%s SYNC] %d 条派发失败且游标照走（这批视为丢失，"
                          "请按 msg_id 排查）: %s", self.account_id, len(failed),
                          ",".join(failed[:10]))
            if new_key:
                await self._write_cursor(new_key)
            else:
                log.debug("[WPP:%s SYNC] 未返回 KeyBuf，游标不动（reason=%s）",
                          self.account_id, reason)
            return len(items)
        except Exception as exc:  # noqa: BLE001 —— 兜底失败不许影响收信
            log.warning("[WPP:%s SYNC] /Msg/Sync 失败（reason=%s）: %s", self.account_id, reason, exc)
            return 0
        finally:
            self._in_flight = False

    # ------------------------------------------------------------------ 心跳

    async def auto_heartbeat(self) -> None:
        """``/Login/AutoHeartBeat`` —— 让**厂商**自己维持长连接。

        开连（含每次重连）时都要重开一遍：新 ws 会话需要重新告知厂商
        （``ws-client.ts:138-142``）。失败只 warn。
        """
        await self._quiet(self.api.auto_heartbeat, "AutoHeartBeat 开启失败")

    async def heartbeat(self) -> None:
        """``/Login/HeartBeatLong`` —— HTTP 侧保活（主力是 ws 原生 ping，见模块 docstring）。"""
        if not self.is_connected():
            return
        await self._quiet(self.api.heartbeat_long, "HeartBeatLong 失败")

    async def _quiet(self, fn: Callable[[], Awaitable[Any]], what: str) -> None:
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001 —— 心跳失败只有 warn，重连由 WS 那侧负责
            log.warning("[WPP:%s SYNC] %s: %s", self.account_id, what, exc)

    # ------------------------------------------------------------------ 游标

    async def _read_cursor(self) -> object:
        """把**同步**的库读取挪到线程池（热路径是事件循环，DB 往返不许堵在这里）。"""
        return await asyncio.to_thread(self._read_key)

    async def _write_cursor(self, key: str) -> None:
        """同 ``_read_cursor`` —— 写游标同样不许在事件循环里做同步 IO。"""
        await asyncio.to_thread(self._write_key, key)

    def _read_key(self) -> object:
        """读上次的游标；失败返 ``_READ_FAILED``（**绝不是** None —— 那会退化成首次）。

        「库坏了也要能继续」在这里的含义是**本轮跳过、下拍照来**：WS 仍是主通道，
        而按首次处理会把这窗消息静默吞掉（还有基线覆盖）。两种降级差一条消息都没少发，
        但只有后者是**丢消息**。
        """
        try:
            if self._get_key is None:
                from ..data import repo_messages
                return repo_messages.get_sync_state(self.account_id)
            return self._get_key(self.account_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP:%s SYNC] 读游标失败（本轮跳过，下拍重试）: %s",
                        self.account_id, exc)
            return _READ_FAILED

    def _write_key(self, key: str) -> None:
        try:
            if self._set_key is None:
                from ..data import repo_messages
                repo_messages.set_sync_state(self.account_id, key)
            else:
                self._set_key(self.account_id, key)
        except Exception as exc:  # noqa: BLE001
            # 存不下游标 ⇒ 下次还是全量：代价是重放（幂等，安全），不是丢消息。
            log.warning("[WPP:%s SYNC] 存游标失败（下次退化为全量拉）: %s", self.account_id, exc)
