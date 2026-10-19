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
（nginx 反代后**公网可达**）。现在两条凭据都校验：

- **路径 token**（``webhookPathToken``）：路由**本身就是**带 token 的路径，
  注册侧由 :func:`core.webhook_paths.derive_webhook_paths` 派生（与推给 vendor 的
  URL **同源**），请求侧再核一遍"token 真的在路径里"；
- **``webhookSecret``**：配了就强制校验 —— 先认明文 secret 头/query（兼容老
  vendor），再认 wechatpadpromax 的 **v2 HMAC**（2026-10-19 起，见下）。

### ③ v2 HMAC（wechatpadpromax，2026-10-19 实抓）

vendor 配 ``secret`` 后**不发明文 secret 头**，而是通过
``X-Webhook-Signature`` / ``X-Webhook-Timestamp-Ms`` / ``X-Webhook-Delivery`` /
``X-Webhook-Signature-Version: v2`` 发送签名：

    hex(HMAC-SHA256(secret, f"{TimestampMs}.{DeliveryID}." + raw_body))

所以签名校验必须在**读完原始请求体之后**做（且要用原始字节，不能用解析后的
JSON）。同时校验时间戳偏差（默认 ±300s，``WPP_WEBHOOK_MAX_SKEW_SEC`` 可调，
0 = 关闭）防重放。旧版 vendor 的明文 secret 头仍然接受。

⚠️ **2026-10-02（C2）改 fail-closed**：旧实现在这里写的是
``if not secret: return True`` —— **未配 secret 即放行**，而当时路径里**根本没带
token**（``webhookPathToken`` 这个字段谁都没消费）⇒ 等于把"可猜的 URL"当成凭据，
管理面/消息面全裸。现在两样都没有 ⇒ **拒绝**（第二道网；第一道网是注册侧根本不注册，
见 ``adapter._start_webhook``）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..core import metrics
from ..core.redact import redact_webhook_path_any
from ..core.webhook_paths import token_is_in_path

log = logging.getLogger(__name__)

#: 请求体上限（10MB，与 TS ``webhook.ts`` 对齐）—— 超了直接 413，不读进内存。
#: 2MB 会把带大 base64 媒体字段的通知挡在门外（vendor 重试到放弃）。
WEBHOOK_BODY_LIMIT = 10 * 1024 * 1024

#: 读取请求体的超时（秒）—— 慢速/半开连接不许把处理线程永远占住
WEBHOOK_READ_TIMEOUT_S = 30.0

#: chunked 编码时单行（chunk-size / trailer）的上限
_CHUNK_LINE_LIMIT = 64 * 1024


class _BodyTooLarge(Exception):
    """chunked 读入超限（不能事先拿 Content-Length 判，只能边读边数）。"""

#: 明文 secret 的候选头（不同 vendor 版本用的名字不一样，全试）。
#:
#: ⚠️ **wechatpadpromax（本项目在用的 vendor）不发明文 secret 头**：它配 secret 后发的是
#: ``X-Webhook-Signature``（64 位 hex）+ ``X-Webhook-Timestamp`` / ``-Timestamp-Ms``
#: / ``-Delivery`` / ``-Signature-Version: v2`` 的 **v2 HMAC 签名**（2026-10-19 实抓，
#: 见 :func:`_verify_v2_signature`）。明文头保留给别的 vendor 版本。
_SECRET_HEADERS = ("X-Webhook-Secret", "X-WPP-Secret", "X-Access-Token")

#: 允许的签名时间戳偏差（秒）。默认 300s：vendor ``retryCount=3`` 的重试窗口远小于它；
#: 设 0 关闭时间戳检查（只在排查时钟问题时用）。
_DEFAULT_MAX_SKEW_S = 300


def _max_skew_seconds() -> int:
    """读 ``WPP_WEBHOOK_MAX_SKEW_SEC``（``0`` = 关闭时间戳检查，非法值回落默认）。"""
    raw = os.environ.get("WPP_WEBHOOK_MAX_SKEW_SEC", "").strip()
    if not raw:
        try:
            from ..core.env_compat import gate_env

            raw = gate_env("WPP_WEBHOOK_MAX_SKEW_SEC").strip()
        except Exception:  # noqa: BLE001 —— 取不到配置就走默认
            raw = ""
    try:
        return max(0, int(raw)) if raw else _DEFAULT_MAX_SKEW_S
    except ValueError:
        return _DEFAULT_MAX_SKEW_S


def _verify_v2_signature(headers: Any, raw_body: bytes, secret: str) -> bool:
    """校验 wechatpadpromax 的 v2 回调签名。

    算法（vendor ``/Webhook/Set`` 文档 + 二进制
    ``wechatdll/srv/webhook.ComputePayloadSignatureV2`` 一致）::

        sig = hex(HMAC-SHA256(secret, f"{TimestampMs}.{DeliveryID}." + raw_body))

    要求 ``X-Webhook-Signature-Version`` 为空或 ``v2``（为将来的 v3 留出拒绝路径）；
    时间戳必须是毫秒且落在 ±``WPP_WEBHOOK_MAX_SKEW_SEC`` 内。
    """
    got = str(headers.get("X-Webhook-Signature") or "").strip()
    ts_raw = str(headers.get("X-Webhook-Timestamp-Ms") or "").strip()
    delivery = str(headers.get("X-Webhook-Delivery") or "").strip()
    version = str(headers.get("X-Webhook-Signature-Version") or "").strip().lower()
    if not got or not ts_raw or not delivery:
        return False
    if version and version != "v2":
        return False
    try:
        ts_ms = int(ts_raw)
    except ValueError:
        return False
    skew = _max_skew_seconds()
    if skew and abs(int(time.time() * 1000) - ts_ms) > skew * 1000:
        return False
    base = f"{ts_raw}.{delivery}.".encode("utf-8") + raw_body
    expect = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(got.lower(), expect)

#: 诊断开关：``WPP_WEBHOOK_DEBUG=1`` 时把回调的鉴权头原样打出来。
#: **只在排查时开**（签名值会进日志），默认关。
#: ⚠️ 必须走 :func:`gate_env`（per-profile 口径）：multiplex 下 ``os.environ`` 装的是
#: launch profile（default）的 ``.env``，读不到 wpp-wechat 里的开关。
def _debug_headers_enabled() -> bool:
    # webhook server 跑在独立线程、没有 profile scope ⇒ gate_env 拿不到当前 profile
    # 的 .env；先看进程 env（launch profile 的 .env 会注入这里），再看 scoped 口径。
    if os.environ.get("WPP_WEBHOOK_DEBUG", "").strip() == "1":
        return True
    from ..core.env_compat import gate_env

    return gate_env("WPP_WEBHOOK_DEBUG").strip() == "1"


def _dump_headers(handler: BaseHTTPRequestHandler) -> None:
    """把 vendor 回调的签名头打出来（诊断用）。

    为什么需要它：vendor（wechatpadpromax）配 secret 后**不是**发明文
    ``X-Webhook-Secret``，而是 ``X-Webhook-Signature`` + ``X-Webhook-Timestamp``
    的 HMAC 签名 —— 与这里 :data:`_SECRET_HEADERS` 的明文比对口径不兼容（配了反而
    100% 403，2026-10-19 实测）。要接这个签名口径，先得看到真实样本。
    """
    interesting: dict[str, str] = {}
    for key, val in handler.headers.items():
        lower = key.lower()
        if lower.startswith(("x-webhook", "x-wpp", "x-hub")):
            interesting[key] = val
        elif lower.startswith("x-access"):
            # 这个头可能是 authcode，只记长度
            interesting[key] = f"<{len(val)} chars>"
    # 只留 tail 两段（``default/webhook``）——路径前缀里有 path token，不进日志
    safe_path = "/".join(handler.path.split("?")[0].split("/")[-2:])
    log.info("[WPP webhook][debug] from=%s path=…/%s body_len=%s headers=%s",
             _client_ip(handler), safe_path,
             handler.headers.get("Content-Length"), interesting)


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    return str(handler.client_address[0]) if handler.client_address else "?"


def _is_loopback(addr: str | None) -> bool:
    """只认回环来源（``127.0.0.0/8`` / ``::1`` / IPv4-mapped）。

    对齐 TS ``isLoopbackAddr``：``/metrics`` 复用 webhook 的监听端口、不另开端口，
    但**只对回环来源**响应；其余一律 404（与"未知路径"同响应，不泄露端点存在）。
    """
    import ipaddress

    raw = str(addr or "").strip()
    if raw.startswith("::ffff:"):
        raw = raw.split("::ffff:", 1)[1]
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


class WppWebhookServer:
    """HTTP webhook 接收 server（**单端口多 path**，多账号共享一个监听）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 4398,
                 loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.host = host
        self.port = port
        #: 主事件循环 —— 由 ``adapter.connect()`` 注入，回调靠它调度
        self.loop = loop
        self._routes: dict[str, tuple[Callable[[dict], Awaitable[None]], str, str]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 路由

    def add_path(self, path: str, on_message: Callable[[dict], Awaitable[None]],
                 secret: str = "", path_token: str = "") -> None:
        """注册 ``path → 处理器``（幂等，重复注册覆盖）。

        ``path_token`` 是**凭据**（``webhookPathToken``）：注册侧应当已把它派生进
        ``path``（``core.webhook_paths``），这里收着是为了在请求侧复核一次，
        并让"这条路由到底有没有凭据"在启动日志里一眼可见。
        """
        if path in self._routes:
            # 同一 path 上换处理器 = 后注册的账号把先注册的**静默顶掉**（消息进错账号的
            # 管线）。派生路径撞车属于配置错误，要吵出来；行为仍保持"幂等覆盖"。
            log.error("[WPP webhook] 路由重复注册 %s —— 后注册的处理器会覆盖前一个",
                      self.redact_path(path))
        self._routes[path] = (on_message, secret or "", path_token or "")
        log.info("[WPP webhook] 注册路由 %s（secret=%s，路径 token=%s，共 %d 条）",
                 self.redact_path(path), "有" if secret else "无",
                 "有" if path_token else "无", len(self._routes))

    def remove_path(self, path: str) -> None:
        self._routes.pop(path, None)

    @property
    def routes(self) -> tuple[str, ...]:
        return tuple(self._routes)

    def redact_path(self, path: str) -> str:
        """**日志专用**：把本 server 上**所有账号**的路径 token 都打码（TS v1.16.1）。

        v1.3.61 起一个 server 服务多账号 ⇒ 只脱"本条账号"的 token 时，A 账号的日志
        里会漏出 B 账号的 token。这里直接遍历路由表里注册过的全部 token。
        """
        return redact_webhook_path_any(
            path, (tok for _on_message, _secret, tok in self._routes.values()))

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
        # 日志专用脱敏器：闭包捕获**绑定方法**（每次调用都读最新路由表 ⇒ 后注册账号的
        # token 也覆盖得到）。请求路径本身含 token，所有打 path 的日志一律过它。
        redact = self.redact_path

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                pass  # 静默 access log（有需要时用 debug 自行打开）

            # ---------------------------------------------------------- 校验

            def _reply(self, code: int, body: bytes = b'{"ok":true}',
                       ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self, path: str, secret: str, path_token: str,
                            raw_body: bytes = b"") -> bool:
                """凭据判定：**路径 token**（配了就必须真在路径里）+ **secret**（配了就必须匹配）。

                fail-closed（C2，2026-10-02）：两样**都没有** ⇒ 直接 ``False``。
                旧实现是 ``if not secret: return True``（未配 secret 即放行），配上当时
                路径里不带 token ⇒ 回调口完全裸奔。路由注册侧已经拒绝注册这种账号
                （见 ``adapter._start_webhook``），这里是**第二道网**：即便有人手工
                ``add_path``，也开不出一个无凭据的回调口。

                secret 支持两种口径（2026-10-19）：老 vendor 的明文 secret 头/query；
                wechatpadpromax 的 v2 HMAC（``raw_body`` 必须是原始请求体字节）。
                """
                if not secret and not path_token:
                    return False
                if path_token and not token_is_in_path(path, path_token):
                    return False
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
                return _verify_v2_signature(self.headers, raw_body, secret)

            # ---------------------------------------------------------- 处理

            def _read_chunked(self) -> bytes:
                """读 ``Transfer-Encoding: chunked`` 的体，**边读边计数**（超限即停）。"""
                chunks: list[bytes] = []
                total = 0
                while True:
                    line = self.rfile.readline(_CHUNK_LINE_LIMIT + 1)
                    if not line or len(line) > _CHUNK_LINE_LIMIT:
                        raise ValueError("bad chunk header")
                    try:
                        size = int(line.split(b";", 1)[0].strip(), 16)
                    except ValueError as exc:
                        raise ValueError("bad chunk size") from exc
                    if size < 0:
                        raise ValueError("negative chunk size")
                    if size == 0:
                        # 吃掉 trailer 直到空行（有些客户端会带）
                        while True:
                            trailer = self.rfile.readline(_CHUNK_LINE_LIMIT + 1)
                            if not trailer or trailer in (b"\r\n", b"\n"):
                                break
                        break
                    total += size
                    if total > WEBHOOK_BODY_LIMIT:
                        raise _BodyTooLarge
                    chunk = self.rfile.read(size)
                    if len(chunk) != size:
                        raise ValueError("short chunk")
                    chunks.append(chunk)
                    self.rfile.read(2)  # 每块末尾的 CRLF
                return b"".join(chunks)

            def _log_future(self, future: Any) -> None:
                """调度出去的回调**异常要有人看**（否则只在 GC 时打一条无声警告）。"""
                try:
                    exc = future.exception()
                except asyncio.CancelledError:
                    # ⚠️ Python 3.8+ 的 CancelledError 是 BaseException，下面那个
                    #   ``except Exception`` **接不住**：被取消的 future 会把异常漏进
                    #   回调机制（asyncio 打一条 "Exception in callback"），明明是
                    #   正常退出却看起来像故障。
                    return
                except Exception:  # noqa: BLE001 —— 已取过异常等边角情形
                    return
                if exc is not None:
                    log.error("[WPP webhook] 回调执行失败: %s", exc, exc_info=exc)

            def _handle(self) -> None:
                # ★ 与 TS v1.9.2 同位置：/metrics 已在 do_GET 拦截，这里才是"收到一条推送"。
                metrics.inc_webhook_received()
                path = self.path.split("?")[0]
                route = routes.get(path)
                if route is None:
                    metrics.inc_webhook_rejected_path()
                    self._reply(404, b'{"error":"unknown path"}')
                    return
                on_message, secret, path_token = route
                if _debug_headers_enabled():
                    _dump_headers(self)
                # 便宜的凭据先挡在读体之前（v2 签名必须等原始体读完才能校验）。
                if not secret and not path_token:
                    metrics.inc_webhook_rejected_secret()
                    log.warning("[WPP webhook] 路由无任何凭据 path=%s from=%s",
                                redact(path), _client_ip(self))
                    self._reply(403, b'{"error":"forbidden"}')
                    return
                if path_token and not token_is_in_path(path, path_token):
                    metrics.inc_webhook_rejected_secret()
                    log.warning("[WPP webhook] 路径 token 不匹配 path=%s from=%s",
                                redact(path), _client_ip(self))
                    self._reply(403, b'{"error":"forbidden"}')
                    return

                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    metrics.inc_webhook_rejected_body_size()
                    self._reply(400, b'{"error":"bad content-length"}')
                    return
                if length < 0:
                    # 负数会让 ``rfile.read(-1)`` 变成"读到 EOF" ⇒ 无限等待/内存。
                    metrics.inc_webhook_rejected_body_size()
                    self._reply(400, b'{"error":"bad content-length"}')
                    return
                if length > WEBHOOK_BODY_LIMIT:
                    metrics.inc_webhook_rejected_body_size()
                    self._reply(413, b'{"error":"payload too large"}')
                    return

                chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
                try:
                    self.connection.settimeout(WEBHOOK_READ_TIMEOUT_S)
                except OSError:
                    pass
                try:
                    if chunked:
                        raw_body = self._read_chunked()
                    else:
                        raw_body = self.rfile.read(length) if length else b""
                    # 解析用的体：空体按 ``{}`` 处理（保持旧行为）；签名用 raw_body。
                    body = raw_body or b"{}"
                except _BodyTooLarge:
                    metrics.inc_webhook_rejected_body_size()
                    self._reply(413, b'{"error":"payload too large"}')
                    return
                except TimeoutError:
                    # 读超时（慢连接/半开）—— TS ``webhook-receiver.ts:100`` 回 408。
                    metrics.inc_webhook_timeout()
                    log.warning("[WPP webhook] 读请求体超时 path=%s from=%s",
                                redact(path), _client_ip(self))
                    self._reply(408, b'{"error":"request timeout"}')
                    return
                except (ValueError, OSError) as exc:
                    metrics.inc_webhook_rejected_body_size()
                    log.warning("[WPP webhook] 读请求体失败 path=%s from=%s: %s",
                                redact(path), _client_ip(self), exc)
                    self._reply(400, b'{"error":"bad body"}')
                    return
                # ★ secret 校验必须在**读完原始体之后**：v2 签名覆盖 raw body。
                #   老 vendor 的明文 secret 头/query 在这里同样走一遍（无体也能过）。
                if not self._authorized(path, secret, path_token, raw_body):
                    metrics.inc_webhook_rejected_secret()
                    log.warning("[WPP webhook] 鉴权失败 path=%s from=%s",
                                redact(path), _client_ip(self))
                    self._reply(403, b'{"error":"forbidden"}')
                    return

                # TS ``webhook-receiver.ts:191-200``：解析失败 **400 拒绝**（不是当空对象
                # 继续调度）—— 否则"vendor 发了畸形负载"在本地看起来与一次正常通知完全
                # 一样，只有计数能看出来。
                try:
                    if _debug_headers_enabled():
                        log.info("[WPP webhook][debug] body=%s",
                                 body[:800].decode("utf-8", "replace"))
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    metrics.inc_webhook_parse_failed()
                    log.warning("[WPP webhook] 请求体 JSON 解析失败 path=%s: %s",
                                redact(path), exc)
                    self._reply(400, b'{"error":"bad request"}')
                    return
                if not isinstance(payload, dict):
                    metrics.inc_webhook_parse_failed()
                    log.warning("[WPP webhook] 请求体不是 JSON 对象 path=%s", redact(path))
                    self._reply(400, b'{"error":"bad request"}')
                    return

                # ★ 修复①：只走注入的主循环调度，绝不在本线程新建事件循环
                if server_loop is None or not server_loop.is_running():
                    metrics.inc_webhook_dispatch_failed()
                    log.error("[WPP webhook] 主事件循环不可用，拒绝处理 path=%s（vendor 会重试）",
                              redact(path))
                    self._reply(503, b'{"error":"loop unavailable"}')
                    return
                try:
                    future = asyncio.run_coroutine_threadsafe(on_message(payload), server_loop)
                except Exception as exc:  # noqa: BLE001 —— 只在 loop 已关闭时发生
                    metrics.inc_webhook_dispatch_failed()
                    log.error("[WPP webhook] 调度回调失败 path=%s: %s", redact(path), exc)
                    self._reply(503, b'{"error":"dispatch failed"}')
                    return
                # ⚠️ future 不能丢：回调在事件循环里抛的异常否则没人看（静默失败）。
                future.add_done_callback(self._log_future)
                metrics.inc_webhook_processed()
                self._reply(200)

            def do_POST(self) -> None:
                try:
                    self._handle()
                except Exception as exc:
                    log.warning("[WPP webhook] 处理异常: %s", exc, exc_info=True)

            def do_GET(self) -> None:
                # v1.9.2 只读观测端点（对齐 TS）：复用监听端口、不开新端口，
                # **只对回环来源**响应；非回环一律 404（不泄露端点存在）。
                # 必须在任何计数之前拦截 —— 运维抓一次 /metrics 不该让"收到消息"+1。
                if self.path.split("?")[0] == "/metrics":
                    if not _is_loopback(_client_ip(self)):
                        self._reply(404, b'{"error":"unknown path"}')
                        return
                    # 两种格式，同一端点：
                    #   默认/无参数      → JSON 快照（`curl /metrics | jq`）
                    #   ?format=prom     → Prometheus 文本（Grafana/node_exporter 直接抓）
                    # 也认 `Accept: text/plain`，方便抓取器不带 query。
                    want_prom = ("format=prom" in self.path
                                 or "text/plain" in (self.headers.get("Accept") or ""))
                    if want_prom:
                        self._reply(200, metrics.render_prometheus().encode("utf-8"),
                                    ctype="text/plain; version=0.0.4; charset=utf-8")
                    else:
                        self._reply(200, metrics.render_json().encode("utf-8"))
                    return
                # vendor 可能用 GET 做 URL 可达性验证 —— 只对**已注册**的路径回 200，
                # 否则 404（别把"这个口开着"告诉扫路径的人）。
                if self.path.split("?")[0] in routes:
                    self._reply(200)
                else:
                    self._reply(404, b'{"error":"unknown path"}')

        return _Handler
