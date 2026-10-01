"""厂商 webhook 接收器 —— 旧 ``webhook.py`` 的移植 + 两处修复。

vendor 推 ``MessageType=sync_message`` 通知（**不带消息体**）⇒ 收到后调
``/Msg/Sync`` 拉增量。它是 WS 断线时的消息兜底通道。

## 相对旧实现的两处修复

### ① 不再在 webhook 线程里新建事件循环

旧实现在拿不到主循环时回退 ``asyncio.run(on_message(payload))`` —— 那会在
**HTTP 处理线程里新建一个事件循环**，而回调链上的 ``aiohttp.ClientSession``
是绑在**主循环**上的 ⇒ 跨循环使用 aiohttp 会抛 ``RuntimeError`` 或更糟：
静默把请求发到别的循环上。现在拿不到主循环就返回 **503**（vendor 会重试），
并记 error —— 宁可让 vendor 重试，也不要跨循环。

### ② 补上鉴权（旧实现完全没有）

旧 server 对任何 POST 都照单全收 ⇒ 任何能碰到这个端口的人都能伪造微信消息
（nginx 反代后**公网可达**）。现在两条都校验：

- **路径 token**：``webhookPathToken`` 必须出现在 URL 路径里；
- **``webhookSecret``**：配了就强制比对（``hmac.compare_digest`` 防时序侧信道）。

未配 secret 的账号仍只靠路径 token —— 与旧行为兼容，但会**warning 提示**。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

#: 请求体上限（2MB）—— 超了直接 413，不读进内存
WEBHOOK_BODY_LIMIT = 2 * 1024 * 1024

#: 校验 secret 时的候选头（不同 vendor 版本用的名字不一样，全试）
_SECRET_HEADERS = ("X-Webhook-Secret", "X-WPP-Secret", "X-Access-Token")


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    return str(handler.client_address[0]) if handler.client_address else "?"


class WppWebhookServer:
    """HTTP webhook 接收 server（**单端口多 path**，多账号共享一个监听）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 4398,
                 loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.host = host
        self.port = port
        #: 主事件循环 —— 由 ``adapter.connect()`` 注入，回调靠它调度
        self.loop = loop
        self._routes: dict[str, tuple[Callable[[dict], Awaitable[None]], str]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 路由

    def add_path(self, path: str, on_message: Callable[[dict], Awaitable[None]],
                 secret: str = "") -> None:
        """注册 ``path → 处理器``（幂等，重复注册覆盖）。"""
        self._routes[path] = (on_message, secret or "")
        log.info("[WPP webhook] 注册路由 %s（secret=%s，共 %d 条）",
                 path, "有" if secret else "无", len(self._routes))

    def remove_path(self, path: str) -> None:
        self._routes.pop(path, None)

    @property
    def routes(self) -> tuple[str, ...]:
        return tuple(self._routes)

    # -------------------------------------------------------------- 生命周期

    def start(self) -> bool:
        if self._server is not None:
            return True
        try:
            self._server = ThreadingHTTPServer((self.host, self.port),
                                               self._make_handler())
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="wpp-webhook")
            self._thread.start()
            log.info("[WPP webhook] 已启动: %s:%d", self.host, self.port)
            return True
        except Exception as exc:  # noqa: BLE001 —— 端口被占不该让 connect 整体失败
            log.error("[WPP webhook] 启动失败 %s:%d: %s", self.host, self.port, exc)
            self._server = None
            return False

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception as exc:  # noqa: BLE001
                log.debug("[WPP webhook] 停止时忽略异常: %s", exc)
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)

    # ------------------------------------------------------------------ 内部

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        routes = self._routes          # 闭包捕获引用（add_path 后新路由立即可见）
        server_loop = self.loop

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
                pass  # 静默 access log（有需要时用 debug 自行打开）

            # ---------------------------------------------------------- 校验

            def _reply(self, code: int, body: bytes = b'{"ok":true}') -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self, path: str, secret: str) -> bool:
                """路径 token 已在 URL 里体现（路由匹配即通过）；这里校验 secret。"""
                if not secret:
                    return True
                for header in _SECRET_HEADERS:
                    got = self.headers.get(header) or ""
                    if got and hmac.compare_digest(got, secret):
                        return True
                # 也接受 query 里的 secret（部分 vendor 版本这么传）
                _, _, query = self.path.partition("?")
                for pair in query.split("&"):
                    key, _, val = pair.partition("=")
                    if key in ("secret", "token") and val and hmac.compare_digest(val, secret):
                        return True
                return False

            # ---------------------------------------------------------- 处理

            def _handle(self) -> None:
                path = self.path.split("?")[0]
                route = routes.get(path)
                if route is None:
                    self._reply(404, b'{"error":"unknown path"}')
                    return
                on_message, secret = route
                if not self._authorized(path, secret):
                    log.warning("[WPP webhook] 鉴权失败 path=%s from=%s", path, _client_ip(self))
                    self._reply(403, b'{"error":"forbidden"}')
                    return

                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length > WEBHOOK_BODY_LIMIT:
                    self._reply(413, b'{"error":"payload too large"}')
                    return

                body = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}

                # ★ 修复①：只走注入的主循环调度，绝不在本线程新建事件循环
                if server_loop is None or not server_loop.is_running():
                    log.error("[WPP webhook] 主事件循环不可用，拒绝处理 path=%s（vendor 会重试）", path)
                    self._reply(503, b'{"error":"loop unavailable"}')
                    return
                try:
                    asyncio.run_coroutine_threadsafe(on_message(payload), server_loop)
                except Exception as exc:  # noqa: BLE001 —— 只在 loop 已关闭时发生
                    log.error("[WPP webhook] 调度回调失败 path=%s: %s", path, exc)
                    self._reply(503, b'{"error":"dispatch failed"}')
                    return
                self._reply(200)

            def do_POST(self) -> None:  # noqa: N802
                try:
                    self._handle()
                except Exception as exc:  # noqa: BLE001 —— 兜底：不让线程挂掉
                    log.warning("[WPP webhook] 处理异常: %s", exc, exc_info=True)

            def do_GET(self) -> None:  # noqa: N802
                # vendor 可能用 GET 做 URL 可达性验证
                self._reply(200)

        return _Handler
