"""WPP vendor webhook 接收器（迁移自 wpp-openclaw webhook-receiver.ts）。

vendor 推送 webhook sync_message 通知（MessageType=sync_message，不带消息体）→
收到后调 /Msg/Sync 拉增量消息（与 WS triggerSync 相同），作为 WS 断线的消息兜底。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

WEBHOOK_BODY_LIMIT = 2 * 1024 * 1024  # 2MB


class WppWebhookServer:
    """HTTP webhook 接收 server（单端口多 path，多账号共享）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 4398, loop=None) -> None:
        self.host = host
        self.port = port
        self.loop = loop  # 主事件循环（adapter connect 时注入，用于调度异步回调）
        self._paths: dict[str, callable] = {}   # path -> on_message(payload)
        self._server = None
        self._thread = None

    def add_path(self, path: str, on_message: callable) -> None:
        """注册 path 处理器（幂等）。"""
        self._paths[path] = on_message
        log.info("[WPP webhook] addPath: %s (total %d)", path, len(self._paths))

    def remove_path(self, path: str) -> None:
        self._paths.pop(path, None)

    def start(self) -> bool:
        """启动 HTTP server（后台线程）。"""
        if self._server:
            return True
        try:
            self._server = ThreadingHTTPServer(
                (self.host, self.port), self._make_handler_factory()
            )
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            log.info("[WPP webhook] 已启动: %s:%d", self.host, self.port)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("[WPP webhook] 启动失败: %s", e)
            return False

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            log.info("[WPP webhook] 已停止")

    def _make_handler_factory(self):
        paths = self._paths
        server_loop = self.loop

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # 静默 access log
                pass

            def _handle(self):
                path = self.path.split("?")[0]
                on_message = paths.get(path)
                if not on_message:
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length > WEBHOOK_BODY_LIMIT:
                    self.send_response(413)
                    self.end_headers()
                    return
                body = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = {}
                # 异步处理（不阻塞 vendor 5s deadline）
                loop = server_loop or _get_loop()
                if loop and loop.is_running():
                    try:
                        asyncio.run_coroutine_threadsafe(on_message(payload), loop)
                    except Exception as e:  # noqa: BLE001
                        # run_coroutine_threadsafe 只在 loop 已关闭时抛错；
                        # 此处绝不能回退到 loop.create_task（跨线程非线程安全）。
                        log.warning("[WPP webhook] 调度回调失败: %s", e)
                else:
                    # 无主循环：同步跑（降级，不丢消息）
                    try:
                        asyncio.run(on_message(payload))
                    except Exception:  # noqa: BLE001
                        pass
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def do_POST(self):
                self._handle()

            def do_GET(self):
                # URL 验证（vendor 可能发 GET 验证）
                self.send_response(200)
                self.end_headers()

        return Handler


def _get_loop():
    """获取可用的主事件循环（webhook 回调丢进主循环）。

    P3-3 修复：不再用模块级全局 `_loop`（multiplex 多 profile 共享会串号）。
    优先当前线程 running loop；无则用注入的实例 loop（由 adapter 传入 server_loop）。
    仅作为兜底，正常路径 server_loop（self.loop）已由 adapter 注入。
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass
    try:
        loop = asyncio.get_event_loop()
        if loop is not None and not loop.is_closed():
            return loop
    except RuntimeError:
        pass
    return None
