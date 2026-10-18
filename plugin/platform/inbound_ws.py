"""厂商 WebSocket 实时通道 —— 旧 ``ws_client.py`` 的移植。

## 这条通道干什么

连 ``wss://<vendor>/ws/sync?authcode=<authcode>``。WS 帧只承载「有新消息」的
推送（``Data.type == "connection_ready" / "sync_update"`` 是心跳类，丢弃），
真正的业务消息体随帧到达后交给 ``on_raw`` 派发。

## 退避策略（**必须原样保留**，与 OpenClaw 对齐）

============  =========================================================
正常断开      指数退避 ``1s → 2s → … → 30s``（上限 30s）
连续 5 次 5xx  进入 **300s** 长退避（vendor 网关整体故障时不要空转）
被拒码         ``1002 / 1008 / 4004`` ⇒ **停止重连**（authcode 无效/被踢）
============  =========================================================

## 与 runner 的分工（**别搞混**）

Hermes 的 ``GatewayRunner._platform_reconnect_watcher`` 负责「**整个平台掉线**」
的重连（30s→300s，自己新建 adapter）。**本条循环只负责「连接活着的时候 WS 断了」**
—— 这正是 ``ADDING_A_PLATFORM.md`` 要求的 *"Implement reconnection with
exponential backoff + jitter for streaming connections"*。

## ⚠️ ``websockets`` 必须在**函数体内**导入（不许搬到模块级）

``platform/adapter.py`` 模块级就 ``from .inbound_ws import WppWsClient``，而
``registration._register_platform`` 会导入那个 adapter ⇒ **本模块的加载期依赖就是
注册期依赖**。``plugin_validate`` 的 probe 在 scratch 解释器里跑 ``register()``，那里
未必装了 websockets ⇒ 模块级 import 会让 probe 里的注册静默失败。

两个用到它的地方都在 :meth:`WppWsClient._run` 里（``connect`` / ``exceptions.ConnectionClosed``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

#: 被拒即停的重连码：1002 协议错、1008 策略拒绝（authcode 无效）、4004 厂商自定义（实例被顶）
_FATAL_CLOSE_CODES = frozenset({1002, 1008, 4004})
#: 连续这么多次 5xx 就进长退避
_LONG_BACKOFF_AFTER_5XX = 5
_LONG_BACKOFF_SECONDS = 300.0
_MAX_BACKOFF_SECONDS = 30.0
#: 心跳类帧（不是业务消息）
_IGNORED_TYPES = frozenset({"connection_ready", "sync_update"})


class WppWsClient:
    """单账号 WS 客户端。``on_raw(payload)`` 收**非心跳**的原始帧。

    ``on_event(reason)``（W5，可选）= **非阻塞**事件钩子，只收到两个 reason：
    ``"ws-open"``（每次连上/重连）与 ``"sync-update"``（收到**不带消息体**的通知帧）。
    适配器把它接到 :class:`core.sync_fallback.SyncFallback` 上 —— 断开那段窗口漏掉的
    消息由``/Msg/Sync``补回来。⚠️ 钩子**必须是同步返回的**（自己 ``create_task``）：
    这里是 WS 读循环里被 ``await`` 的地方，等一趟 HTTP 拉取会把收帧也堵住。
    """

    def __init__(
        self,
        account_id: str,
        ws_url: str,
        authcode: str,
        on_raw: Callable[[dict[str, Any]], Awaitable[None]],
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.account_id = account_id
        self.ws_url = ws_url
        self.authcode = authcode
        self.on_raw = on_raw
        self.on_event = on_event
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        #: 已连上过一次（供 ``connect()`` 判断是否算接入成功）
        self.connected_once = False
        #: **当前**是否连着（心跳只在连着时发；``connected_once`` 是"曾经连上过"，别混）
        self.connected = False

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """起后台重连循环（幂等）。"""
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name=f"wpp-ws-{self.account_id}")

    async def stop(self) -> None:
        self._stop.set()
        self.connected = False
        try:
            from ..core import metrics

            metrics.set_ws_connected(False)
        except Exception:  # noqa: S110, BLE001 —— 埋点失败绝不能挡住停机
            pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("[WPP:%s WS] 停止时忽略异常: %s", self.account_id, exc)
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # -------------------------------------------------------------------- 内部

    def _url(self) -> str:
        if self.authcode and "?" not in self.ws_url:
            return f"{self.ws_url}?authcode={self.authcode}"
        return self.ws_url

    async def _run(self) -> None:
        import websockets  # 延迟导入，见模块 docstring

        delay = 1.0
        consecutive_5xx = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self._url(),
                    ping_interval=20, ping_timeout=20, close_timeout=5,
                    max_size=100 * 1024 * 1024,
                ) as ws:
                    delay = 1.0
                    consecutive_5xx = 0
                    self.connected_once = True
                    self.connected = True
                    self._metric("set_ws_connected", True)
                    log.info("[WPP:%s WS] 已连接", self.account_id)
                    # W5：每次连上/重连都让兜底拉一趟 —— **断开那段窗口**的消息只能这么补
                    #     （TS ``ws-client.ts:137`` 的 ``triggerSync("ws-open")`` 同款）。
                    self._emit("ws-open")
                    try:
                        async for raw in ws:
                            await self._dispatch(raw)
                    finally:
                        self.connected = False
                        self._metric("set_ws_connected", False)
                        self._metric("inc_ws_disconnect")
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 —— 任何断开都要能重连
                if self._stop.is_set():
                    break
                log.warning("[WPP:%s WS] 断开，%.0fs 后重连: %s", self.account_id, delay, exc)

                # 被拒码 ⇒ 停止重连（重试也不会好，还会打爆 vendor）
                if isinstance(exc, websockets.exceptions.ConnectionClosed):
                    code = exc.rcvd.code if exc.rcvd else None
                    if code in _FATAL_CLOSE_CODES:
                        log.error("[WPP:%s WS] 连接被拒 (code=%s)，停止重连", self.account_id, code)
                        break

                # 连续 5xx ⇒ 长退避
                status = getattr(getattr(exc, "response", None), "status_code", None)
                consecutive_5xx = consecutive_5xx + 1 if status in (502, 503, 504) else 0
                if consecutive_5xx >= _LONG_BACKOFF_AFTER_5XX:
                    log.warning("[WPP:%s WS] 连续 %d 次 5xx，进入 %.0fs 长退避",
                                self.account_id, consecutive_5xx, _LONG_BACKOFF_SECONDS)
                    consecutive_5xx = 0
                    delay = 1.0
                    self._metric("inc_ws_reconnect")
                    if await self._sleep_interruptible(_LONG_BACKOFF_SECONDS):
                        break
                    continue

            if self._stop.is_set():
                break
            # TS ``ws-client.ts:286``：只在**真的排期重连**时计（stop 之后不计）。
            self._metric("inc_ws_reconnect")
            if await self._sleep_interruptible(delay):
                break
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)

    def _metric(self, fn: str, *args) -> None:
        """埋点调用一律吞异常 —— 观测失败绝不许影响 WS 重连主链。"""
        try:
            from ..core import metrics

            getattr(metrics, fn)(*args)
        except Exception:  # noqa: S110, BLE001 —— noqa: BLE001
            pass

    async def _sleep_interruptible(self, seconds: float) -> bool:
        """睡 ``seconds``，期间若被要求停止则立刻返回 True。"""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    def _emit(self, reason: str) -> None:
        """调事件钩子（W5）。钩子是**旁路** —— 它自己炸了不许影响连接与收帧。"""
        if self.on_event is None:
            return
        try:
            self.on_event(reason)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP:%s WS] 事件钩子 %s 失败: %s", self.account_id, reason, exc)

    async def _dispatch(self, raw: Any) -> None:
        """解析一帧并交给 ``on_raw``。**坏帧只跳过自己**，不中断连接。"""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        data = payload.get("Data")
        if isinstance(data, dict) and data.get("type") in _IGNORED_TYPES:
            # W5：这两类帧**不带消息体**，所以它们不是"可以当消息解析"的帧 ——
            #   但 `sync_update` 恰恰是"有新东西"的通知 ⇒ 让兜底去 /Msg/Sync 拉一趟
            #   （拉回来的照样走 on_raw，与帧那条路靠 msgId 去重）。
            #   `connection_ready` 是握手 ack，开连时已经拉过一趟了，不重复打厂商。
            if data.get("type") == "sync_update":
                self._emit("sync-update")
            return
        try:
            await self.on_raw(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP:%s WS] on_raw 处理失败: %s", self.account_id, exc)
