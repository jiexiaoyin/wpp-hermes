"""vendor MCP 端点 —— 默认端口必须与 vendor 实际监听一致。

2026-10-18 现场核对（不是照抄 TS 注释）：

* vendor ``/config/app.conf``：``httpport = 28062``、``mcp_enabled = true``；
* 宿主机 ``ss -ltnp``：``wechatpadpromax`` 监听 ``*:28062``（HTTP）与 ``*:28089``（WS），
  **没有任何进程监听 8062**；
* ``POST http://127.0.0.1:28062/mcp`` 有响应（无 token 时 401，路由存在）。

TS ``constants.ts`` 写的 ``127.0.0.1:8062`` 是 vendor **容器内**端口 / 旧部署口径，
宿主机侧直接照抄会永远连不上，而调用方是"失败即静默 fallback"⇒ 表现为 MCP 能力
凭空消失、日志只有 debug 级一行。这里把端口钉死，防止再被注释带回 8062。
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plugin.core import vendor_mcp


def test_default_url_matches_vendor_httpport(monkeypatch):
    monkeypatch.delenv("WECHATPRO_MCP_URL", raising=False)
    assert vendor_mcp.mcp_url() == "http://127.0.0.1:28062/mcp"


def test_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("WECHATPRO_MCP_URL", "http://127.0.0.1:9999/mcp")
    assert vendor_mcp.mcp_url() == "http://127.0.0.1:9999/mcp"


class _StubMCP(BaseHTTPRequestHandler):
    """最小 StreamableHTTP 桩：initialize → tools/call，记录每个请求。"""

    requests: list[dict] = []

    def log_message(self, *args):  # noqa: D401 —— 测试里不要 access log
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.requests.append({"headers": dict(self.headers), "body": body})
        method = body.get("method")
        if method == "initialize":
            payload = {"jsonrpc": "2.0", "id": body.get("id"),
                       "result": {"protocolVersion": "2025-03-26",
                                  "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "stub", "version": "1"}}}
            self._send(200, payload)
        elif method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
        elif method == "tools/call":
            payload = {"jsonrpc": "2.0", "id": body.get("id"),
                       "result": {"content": [{"type": "text", "text": "ok"}]}}
            # SSE 形态：vendor 实际就回 event-stream
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(("event: message\ndata: " + json.dumps(payload)
                              + "\n\n").encode())
        else:
            self._send(404, {"error": "unknown method"})

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.mark.network  # 仅回环桩；conftest 默认禁所有 connect
def test_call_tool_roundtrip_uses_stdlib_only(monkeypatch):
    """Hermes 运行时没有 aiohttp/httpx —— 客户端必须靠 stdlib 也能跑通。

    2026-10-18 线上实测：旧实现 ``import aiohttp`` 每次都 ModuleNotFoundError，
    被调用方宽 ``except`` 吞掉 ⇒ MCP 永远静默回退、看起来像没启用。
    """
    _StubMCP.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubMCP)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(
            "WECHATPRO_MCP_URL",
            f"http://127.0.0.1:{server.server_address[1]}/mcp")
        result = asyncio.run(
            vendor_mcp.call_tool("wechat_account_status", {}, "tok-123"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert vendor_mcp.result_text(result) == "ok"
    assert [r["body"].get("method") for r in _StubMCP.requests] == [
        "initialize", "notifications/initialized", "tools/call"]
    for req in _StubMCP.requests:
        # urllib 会把头名 capitalize ⇒ 查的时候必须按大小写不敏感比
        headers = {k.lower(): v for k, v in req["headers"].items()}
        assert headers["authorization"] == "Bearer tok-123"
    # 初始化之后的请求必须带协商出的协议版本
    last = {k.lower(): v for k, v in _StubMCP.requests[-1]["headers"].items()}
    assert last["mcp-protocol-version"] == "2025-03-26"


@pytest.mark.network  # 仅回环桩
def test_http_error_returns_none_without_raising(monkeypatch):
    class _Unauthorized(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            raw = b"invalid token\n"
            self.send_response(401)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Unauthorized)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(
            "WECHATPRO_MCP_URL",
            f"http://127.0.0.1:{server.server_address[1]}/mcp")
        assert asyncio.run(
            vendor_mcp.call_tool("wechat_account_status", {}, "bad")) is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
