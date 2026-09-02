"""WPP vendor WebSocket 客户端（迁移自 astrbot-plugin-wpp wpp_account.py + wpp-openclaw ws-client.ts）。

连 vendor /ws/sync?authcode= 实时推送通道。
WS 帧是"有新消息"的推送信号，收到后调 /Msg/Sync 拉增量（各消费端独立 synckey）。
内置指数退避重连（1s→30s），5 次 502/503/504 触发 5min 长退避。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import websockets

log = logging.getLogger(__name__)


class WppWsClient:
    """单账号 WS 客户端。"""

    def __init__(
        self,
        account_id: str,
        ws_url: str,
        authcode: str,
        on_raw: Callable[[dict], Awaitable[None]],
    ) -> None:
        self.account_id = account_id
        self.ws_url = ws_url
        self.authcode = authcode
        self.on_raw = on_raw  # 收到原始 WS 推送（不含 connection_ready/sync_update）
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def _ws_url(self) -> str:
        url = self.ws_url
        if self.authcode and "?" not in url:
            url = f"{url}?authcode={self.authcode}"
        return url

    async def _run(self) -> None:
        delay = 1.0
        consecutive_5xx = 0
        while not self._stop.is_set():
            try:
                url = self._ws_url()
                log.info("[WPP:%s WS] connecting: %s", self.account_id, url.split("?")[0])
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=100 * 1024 * 1024,
                ) as ws:
                    delay = 1.0
                    consecutive_5xx = 0
                    log.info("[WPP:%s WS] connected", self.account_id)
                    async for raw in ws:
                        try:
                            payload = json.loads(raw)
                        except Exception:  # noqa: BLE001
                            continue
                        dtype = (payload.get("Data") or {}).get("type")
                        if dtype in ("connection_ready", "sync_update"):
                            continue
                        try:
                            await self.on_raw(payload)
                        except Exception as e:  # noqa: BLE001
                            log.warning("[WPP:%s WS] on_raw failed: %s", self.account_id, e)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                log.warning("[WPP:%s WS] 连接断开/异常, %ss 后重连: %s", self.account_id, delay, e)
                # 检测 5xx（websockets 15: InvalidStatus.response.status_code）
                status = None
                resp = getattr(e, "response", None)
                if resp is not None:
                    status = getattr(resp, "status_code", None)
                if status in (502, 503, 504):
                    consecutive_5xx += 1
                else:
                    consecutive_5xx = 0
                if isinstance(e, websockets.exceptions.ConnectionClosed):
                    code = e.rcvd.code if e.rcvd else None
                    if code in (1002, 1008, 4004):
                        log.error("[WPP:%s WS] 连接被拒 (code=%s)，停止重连", self.account_id, code)
                        break
                # 连续 5 次 5xx → 300s 长退避（对齐 docstring）
                if consecutive_5xx >= 5:
                    log.warning("[WPP:%s WS] 连续 5 次 5xx，进入 300s 长退避", self.account_id)
                    consecutive_5xx = 0
                    if self._stop.is_set():
                        break
                    await asyncio.sleep(300)
                    delay = 1.0
                    continue
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
