"""webhook 接收器：路径派生（TS 同形状）+ 鉴权 fail-closed + 注册侧的第二道网。

## 这条线为什么值得单测

厂商 webhook 是**公网可达**的入站口（nginx 反代之后），而 2026-10-02 审阅发现它
在 py2 里等于**没有凭据**：

* ``webhookPathToken`` 这个字段被 ``accounts.py`` 解析、被模块 docstring 声称"必须出现在
  URL 路径里"，但**谁都没消费** ⇒ 回调地址就是可猜的 ``/wechatpadpro/<id>/webhook``；
* ``_authorized`` 开头是 ``if not secret: return True`` ⇒ 未配 secret 即**放行**。

两条合起来：任何能碰到该端口的人都能伪造微信入站消息。修法是三处同源收口：
派生路径（本文件 §1）、请求侧复核（§2）、注册侧无凭据不注册（§3 + AST 形状）。

## 为什么不真起 HTTP

``conftest.py`` 默认禁网（``socket.connect`` 直接炸），而这条线要验的是**判据**而不是
TCP。所以 §2 直接调生成出来的 handler 方法（用假 connection 对象喂 ``headers`` /
``path`` / ``rfile``），§3 只验注册与调用形状。真正的"厂商推过来收得到"归 .183 真机。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import io
import logging
import threading
import time
from pathlib import Path

import pytest

from plugin.core.api_client import derive_business_url
from plugin.core.webhook_paths import (
    WebhookPaths, derive_webhook_paths, token_is_in_path,
)
from plugin.platform.inbound_webhook import WEBHOOK_BODY_LIMIT, WppWebhookServer

ROOT = Path(__file__).resolve().parents[2]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"

#: 真机上 live-test 账号那组值（形状取自 ``accounts/live-test.json``）。
LIVE_PATH = "/wechatpadpro/livetest/webhook"
LIVE_TOKEN = "aee409d9b64f4b533f2a947d"


def _adapter_method(name: str) -> ast.AST:
    src = (ROOT / ADAPTER_REL).read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLS:
            for fn in node.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == name:
                    return fn
    raise AssertionError(f"{ADAPTER_REL} 里找不到 {ADAPTER_CLS}.{name} —— 改名/搬家了？")


def _adapter_code(name: str) -> str:
    body = list(getattr(_adapter_method(name), "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


# ===== 1. 路径派生（唯一来源，TS 同形状） =====


def test_token_is_inserted_after_the_vendor_prefix():
    """token 段插在 ``/wechatpadpro/`` **之后** —— 位置不是随便定的，TS 一直这么做。"""
    got = derive_webhook_paths(LIVE_PATH, LIVE_TOKEN)
    assert got.webhook == f"/wechatpadpro/{LIVE_TOKEN}/livetest/webhook"
    assert got.business == f"/wechatpadpro/{LIVE_TOKEN}/livetest/webhook/business"


def test_without_a_token_the_configured_paths_are_used_as_is():
    """没配 token ⇒ 配置的 business 路径**原样采信**（故意用了个 ≠ 主路径+``/business`` 的值：
    用默认形状的话，"配置被忽略"和"配置被采信"会算出同一个串，测试就成了空转）。
    """
    got = derive_webhook_paths(LIVE_PATH, "", "/custom/biz")
    assert got.webhook == LIVE_PATH
    assert got.business == "/custom/biz"


def test_business_defaults_to_path_plus_business():
    got = derive_webhook_paths(LIVE_PATH, "")
    assert got.business == f"{LIVE_PATH}/business"


def test_empty_everything_falls_back_to_the_legacy_business_path():
    """全空时的兜底值**与旧 ``derive_business_url`` 一致**（别改，有人按它配过 nginx）。"""
    assert derive_webhook_paths("", "").business == "/wechatpadpro/default/webhook/business"


def test_token_is_appended_when_the_path_has_no_vendor_prefix():
    """有意偏离 TS：TS 那里 ``replace`` 是空操作 ⇒ token **静默不生效**（配了等于没配）。
    这里退化成追加，保证"配了就一定在路径里" —— 安全默认优先于逐字对齐。
    """
    got = derive_webhook_paths("/hook", "T")
    assert got.webhook == "/hook/T"
    assert token_is_in_path(got.webhook, "T")


def test_derived_paths_are_a_named_tuple():
    got = derive_webhook_paths(LIVE_PATH, LIVE_TOKEN)
    assert isinstance(got, WebhookPaths)
    webhook, business = got, got          # 元组解包也要能用（旧调用点是双返回值）
    assert webhook.webhook == business.webhook


@pytest.mark.parametrize("path,token,want", [
    (f"/wechatpadpro/{LIVE_TOKEN}/livetest/webhook", LIVE_TOKEN, True),
    ("/wechatpadpro/livetest/webhook", LIVE_TOKEN, False),
    ("/wechatpadpro/livetest/webhook", "", False),
    (f"/wechatpadpro/{LIVE_TOKEN}/livetest/webhook", "", False),
    # 只在别处出现同样的串不算（判据是路径段，不是子串碰巧）
    (f"/wechatpadpro/livetest/webhook?x={LIVE_TOKEN}", LIVE_TOKEN, False),
])
def test_token_is_in_path(path: str, token: str, want: bool):
    assert token_is_in_path(path, token) is want


def test_business_url_carries_the_token_and_full_credentials():
    """推给 vendor 的两条 URL 必须**都**带 token 且都带凭据段。

    修前 ``/Webhook/Set`` 推的是**不带 token** 的路径，而 business 那条带 —— 同一个账号
    两个回调地址形状不一致，且前者正是可猜的那个（C2）。
    """
    sync_url, logout_url = derive_business_url("https://h.test/wechatpadpro/x/webhook",
                                               LIVE_PATH, LIVE_TOKEN)
    assert sync_url == f"https://h.test/wechatpadpro/{LIVE_TOKEN}/livetest/webhook/business"
    assert logout_url == f"{sync_url}/logout"
    assert token_is_in_path(sync_url, LIVE_TOKEN)


def test_business_url_without_token_keeps_the_old_shape():
    sync_url, _ = derive_business_url("https://h.test", LIVE_PATH, "")
    assert sync_url == f"https://h.test{LIVE_PATH}/business"


# ===== 2. 请求侧鉴权（fail-closed） =====


class _FakeSock:
    def __init__(self) -> None:
        self.timeout: float | None = None

    def settimeout(self, value: float) -> None:
        self.timeout = value


class _FakeConn:
    """喂给生成出来的 handler 当 ``self``：只带 ``_authorized`` / ``_handle`` 用到的东西。"""

    def __init__(self, path: str, headers: dict | None = None, body: bytes = b"{}") -> None:
        self.path = path
        self.headers = dict(headers or {})
        # 真 HTTP 一定带 Content-Length（handler 按它决定读多少字节）；假连接补上，
        # 否则请求体读成空 ⇒ 测出来的是"我没发"而不是"它没读"。
        self.headers.setdefault("Content-Length", str(len(body)))
        self.rfile = io.BytesIO(body)
        self.connection = _FakeSock()
        self.client_address = ("127.0.0.1", 12345)
        self.sent: list[tuple[int, bytes]] = []
        self._handler: type | None = None

    def bind(self, handler: type) -> "_FakeConn":
        """把生成出来的 handler 挂上 —— ``_handle`` 内部会 ``self._authorized(...)``。"""
        self._handler = handler
        return self

    def _authorized(self, path: str, secret: str, path_token: str,
                    raw_body: bytes = b"") -> bool:
        assert self._handler is not None, "conn 没 bind handler"
        return self._handler._authorized(self, path, secret, path_token, raw_body)

    def _read_chunked(self) -> bytes:
        assert self._handler is not None, "conn 没 bind handler"
        return self._handler._read_chunked(self)

    def _log_future(self, future) -> None:
        assert self._handler is not None, "conn 没 bind handler"
        return self._handler._log_future(self, future)

    def _reply(self, code: int, body: bytes = b'{"ok":true}') -> None:
        self.sent.append((code, body))

    @property
    def code(self) -> int:
        assert self.sent, "没有回复"
        return self.sent[-1][0]


def _conn(path: str, headers: dict | None = None, body: bytes = b"{}") -> _FakeConn:
    return _FakeConn(path, headers, body)


def _authorized(path: str, secret: str = "", token: str = "",
                headers: dict | None = None, query: str = "",
                raw_body: bytes = b"") -> bool:
    handler = WppWebhookServer()._make_handler()
    conn = _conn(path + query, headers, body=raw_body or b"{}")
    return handler._authorized(conn, path, secret, token, raw_body)


def test_no_credential_at_all_is_rejected():
    """**C2 的核心回归**：既没 secret 也没路径 token ⇒ 拒绝。

    旧实现 ``if not secret: return True`` ⇒ 放行；配上当时"路径里根本没带 token"，
    这个回调口对公网是完全敞开的。
    """
    assert _authorized("/wechatpadpro/livetest/webhook") is False


def test_path_token_alone_is_enough_when_it_is_really_in_the_path():
    assert _authorized(f"/wechatpadpro/{LIVE_TOKEN}/livetest/webhook", "", LIVE_TOKEN) is True


def test_path_token_must_be_present_in_the_path():
    """配了 token 但这条路径里没有它 ⇒ 拒（不能"配了就当验过了"）。"""
    assert _authorized("/wechatpadpro/livetest/webhook", "", LIVE_TOKEN) is False


def test_secret_from_each_accepted_header():
    for header in ("X-Webhook-Secret", "X-WPP-Secret", "X-Access-Token"):
        assert _authorized("/p", "s3cret", "", {header: "s3cret"}) is True, header
        assert _authorized("/p", "s3cret", "", {header: "nope"}) is False, header


def test_secret_from_query_string():
    assert _authorized("/p", "s3cret", "", None, "?secret=s3cret") is True
    assert _authorized("/p", "s3cret", "", None, "?token=s3cret") is True
    assert _authorized("/p", "s3cret", "", None, "?secret=nope") is False


def test_empty_secret_header_value_never_passes():
    """空串头不能等于"没配 secret 就放行"（``hmac.compare_digest("", "")`` 是 True）。"""
    assert _authorized("/p", "s3cret", "", {"X-Webhook-Secret": ""}) is False


# ===== 2b. v2 HMAC（wechatpadpromax，2026-10-19） =====


def _signed_headers(secret: str, body: bytes, *, ts_ms: int | None = None,
                    delivery: str = "delivery-1", version: str = "v2") -> dict:
    ts = str(int(time.time() * 1000) if ts_ms is None else ts_ms)
    base = f"{ts}.{delivery}.".encode() + body
    sig = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "X-Webhook-Signature": sig,
        "X-Webhook-Timestamp-Ms": ts,
        "X-Webhook-Delivery": delivery,
        "X-Webhook-Signature-Version": version,
    }


_BODY = b'{"MessageType": "sync_message", "Wxid": "wxid_x"}'


def test_v2_signature_over_raw_body_passes_without_plain_secret_header():
    """vendor 配 secret 后只发签名头 —— 必须用 raw body 验，而不是只认明文头。"""
    assert _authorized("/p", "s3cret", "", _signed_headers("s3cret", _BODY),
                       raw_body=_BODY) is True


def test_v2_signature_rejects_tampered_body_and_wrong_secret():
    headers = _signed_headers("s3cret", _BODY)
    assert _authorized("/p", "s3cret", "", headers,
                       raw_body=_BODY + b" ") is False, "体变了签名必须失效"
    assert _authorized("/p", "other-secret", "", headers, raw_body=_BODY) is False
    # 签名按原始 JSON 字节算；用"解析后再序列化"的等价 JSON 不应通过
    canonical = b'{"MessageType":"sync_message","Wxid":"wxid_x"}'
    assert _authorized("/p", "s3cret", "", headers, raw_body=canonical) is False


def test_v2_signature_requires_fresh_timestamp(monkeypatch):
    monkeypatch.setenv("WPP_WEBHOOK_MAX_SKEW_SEC", "60")
    stale = int(time.time() * 1000) - 61_000
    headers = _signed_headers("s3cret", _BODY, ts_ms=stale)
    assert _authorized("/p", "s3cret", "", headers, raw_body=_BODY) is False


def test_v2_signature_rejects_unknown_version_and_missing_headers():
    headers = {
        "X-Webhook-Signature": "00" * 32,
        "X-Webhook-Timestamp-Ms": str(int(time.time() * 1000)),
        "X-Webhook-Delivery": "d",
        "X-Webhook-Signature-Version": "v3",
    }
    assert _authorized("/p", "s3cret", "", headers, raw_body=_BODY) is False
    assert _authorized("/p", "s3cret", "", {
        "X-Webhook-Signature": "00" * 32}, raw_body=_BODY) is False


def test_v2_signed_request_reaches_the_handler_with_raw_body():
    """端到端（仍不涉网）：签名有效的请求必须真的调度，且 payload 是同一份体。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        server, handler, seen = _handler_with_routes(secret="s3cret")
        server.loop = loop
        handler = server._make_handler()
        conn = _conn("/p", _signed_headers("s3cret", _BODY), body=_BODY)
        handler._handle(conn.bind(handler))
        assert conn.code == 200
        deadline = time.time() + 2
        while not seen and time.time() < deadline:
            time.sleep(0.01)
        assert seen == [{"MessageType": "sync_message", "Wxid": "wxid_x"}]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_v2_signed_request_with_wrong_signature_is_403_before_parsing():
    _, handler, seen = _handler_with_routes(secret="s3cret")
    headers = _signed_headers("wrong", _BODY)
    conn = _conn("/p", headers, body=_BODY)
    handler._handle(conn.bind(handler))
    assert conn.code == 403
    assert seen == []


def test_both_credentials_must_hold_when_both_are_configured():
    good = f"/wechatpadpro/{LIVE_TOKEN}/livetest/webhook"
    assert _authorized(good, "s3cret", LIVE_TOKEN, {"X-Webhook-Secret": "s3cret"}) is True
    assert _authorized(good, "s3cret", LIVE_TOKEN, {"X-Webhook-Secret": "wrong"}) is False
    assert _authorized("/wechatpadpro/livetest/webhook", "s3cret", LIVE_TOKEN,
                       {"X-Webhook-Secret": "s3cret"}) is False


# ===== 3. handler 路由与调度（不碰 TCP） =====


def _handler_with_routes(*, secret: str = "", token: str = "", path: str = "/p"):
    server = WppWebhookServer()            # loop=None ⇒ 走"主循环不可用"那条
    seen: list[dict] = []

    async def on_message(payload: dict) -> None:
        seen.append(payload)

    server.add_path(path, on_message, secret=secret, path_token=token)
    return server, server._make_handler(), seen


def test_unknown_path_is_404():
    _, handler, _ = _handler_with_routes()
    conn = _conn("/nope")
    handler._handle(conn.bind(handler))
    assert conn.code == 404


def test_unauthenticated_request_is_403_and_never_dispatches():
    _, handler, seen = _handler_with_routes(path="/p")
    conn = _conn("/p")
    handler._handle(conn.bind(handler))
    assert conn.code == 403, "无凭据的请求必须被挡住"
    assert seen == [], "被拒的请求不许进回调"


def test_authorized_request_is_503_when_the_main_loop_is_unavailable():
    """修复①：拿不到主循环 ⇒ 503 让 vendor 重试，**绝不**在本线程新建事件循环。"""
    _, handler, seen = _handler_with_routes(secret="s3cret")
    conn = _conn("/p", {"X-Webhook-Secret": "s3cret"})
    handler._handle(conn.bind(handler))
    assert conn.code == 503
    assert seen == []


def test_oversized_body_is_413():
    """上限是 **10MB**（TS 同款）—— 2MB 会把带大 base64 媒体字段的通知全挡掉。"""
    assert WEBHOOK_BODY_LIMIT == 10 * 1024 * 1024
    _, handler, _ = _handler_with_routes(secret="s3cret")
    conn = _conn("/p", {"X-Webhook-Secret": "s3cret",
                        "Content-Length": str(WEBHOOK_BODY_LIMIT + 1)})
    handler._handle(conn.bind(handler))
    assert conn.code == 413


def test_a_negative_content_length_is_rejected_not_read_to_eof():
    """``rfile.read(-1)`` = 读到 EOF ⇒ 慢连接能把线程永远占住。负数必须 400。"""
    _, handler, seen = _handler_with_routes(secret="s3cret")
    conn = _conn("/p", {"X-Webhook-Secret": "s3cret", "Content-Length": "-1"})
    handler._handle(conn.bind(handler))
    assert conn.code == 400
    assert seen == []


def test_a_non_numeric_content_length_is_rejected():
    _, handler, _ = _handler_with_routes(secret="s3cret")
    conn = _conn("/p", {"X-Webhook-Secret": "s3cret", "Content-Length": "abc"})
    handler._handle(conn.bind(handler))
    assert conn.code == 400


def test_chunked_body_is_read_with_a_cap():
    """chunked 没有 Content-Length —— 只按头判定等于这道上限不存在。"""
    payload = b'{"MessageType":"sync_message"}'
    wire = (b"%x\r\n" % len(payload)) + payload + b"\r\n0\r\n\r\n"
    _, handler, seen = _handler_with_routes(secret="s3cret")
    conn = _conn("/p", {"X-Webhook-Secret": "s3cret", "Content-Length": "0",
                        "Transfer-Encoding": "chunked"}, body=wire)
    # 主循环不可用 ⇒ 503，但前提是**体已经正确读完**并且 payload 解析出来了
    handler._handle(conn.bind(handler))
    assert conn.code == 503


def test_an_oversized_chunked_body_is_413_without_buffering_it():
    _, handler, _ = _handler_with_routes(secret="s3cret")
    wire = (b"%x\r\n" % (WEBHOOK_BODY_LIMIT + 1))
    conn = _conn("/p", {"X-Webhook-Secret": "s3cret", "Content-Length": "0",
                        "Transfer-Encoding": "chunked"}, body=wire)
    handler._handle(conn.bind(handler))
    assert conn.code == 413


def test_a_malformed_chunked_body_is_400():
    _, handler, _ = _handler_with_routes(secret="s3cret")
    conn = _conn("/p", {"X-Webhook-Secret": "s3cret", "Content-Length": "0",
                        "Transfer-Encoding": "chunked"}, body=b"zz\r\n")
    handler._handle(conn.bind(handler))
    assert conn.code == 400


def test_get_only_answers_healthy_on_registered_paths():
    """可达性探测只对已注册路径回 200，否则等于替扫路径的人确认了这个口开着。"""
    server, handler, _ = _handler_with_routes(secret="s3cret", path="/p")
    conn = _conn("/p")
    handler.do_GET(conn.bind(handler))
    assert conn.code == 200
    conn2 = _conn("/nope")
    handler.do_GET(conn2.bind(handler))
    assert conn2.code == 404


def test_authorized_request_is_dispatched_on_the_main_loop():
    """把主循环真跑起来（不涉网）：payload 必须**真的**走到 ``on_message``，并回 200。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        server, handler, seen = _handler_with_routes(secret="s3cret")
        server.loop = loop
        handler = server._make_handler()          # 重新生成：闭包捕获的是旧 loop
        conn = _conn("/p", {"X-Webhook-Secret": "s3cret"}, body=b'{"MessageType":"sync_message"}')
        handler._handle(conn.bind(handler))
        assert conn.code == 200
        deadline = time.time() + 2
        while not seen and time.time() < deadline:
            time.sleep(0.01)
        assert seen == [{"MessageType": "sync_message"}], "payload 必须原样交给回调"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_malformed_body_is_rejected_with_400_not_dispatched():
    """坏 JSON ⇒ **400 拒绝**，不调度（2026-10-18 对齐 TS ``webhook-receiver.ts:191``）。

    旧契约是"当空 dict 继续调度"—— 于是"vendor 发了畸形负载"与一次正常通知在本地
    看起来完全一样（回调拿到 {}，什么也不做）。TS 明确回 400 并计
    ``messages_rejected_parse_total``，这里照做；关键是**不许抛穿**处理线程。
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        server, handler, seen = _handler_with_routes(secret="s3cret")
        server.loop = loop
        handler = server._make_handler()
        conn = _conn("/p", {"X-Webhook-Secret": "s3cret"}, body=b"not json")
        handler._handle(conn.bind(handler))
        assert conn.code == 400
        time.sleep(0.05)
        assert seen == [], "坏 JSON 不许进回调（否则与正常通知无法区分）"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_add_path_remembers_the_token_and_is_idempotent():
    server = WppWebhookServer()
    async def on_message(payload: dict) -> None:  # noqa: ARG001
        return None

    server.add_path("/a", on_message, secret="", path_token="T")
    server.add_path("/a", on_message, secret="", path_token="T")
    assert server.routes == ("/a",)
    assert server._routes["/a"][2] == "T", "token 必须存着（请求侧还要复核一次）"
    server.remove_path("/a")
    assert server.routes == ()


# ===== 4. 注册侧（fail-closed 的第一道网，AST 形状） =====


def test_start_webhook_derives_paths_from_the_shared_module():
    code = _adapter_code("_start_webhook")
    assert "webhook_paths.derive_webhook_paths(" in code, \
        "路径派生必须走 core.webhook_paths（与推给 vendor 的 URL 同源）"


def test_start_webhook_registers_both_paths_with_the_token():
    code = _adapter_code("_start_webhook")
    assert code.count(".add_path(") == 2, "主路径 + business 路径都要注册（business 曾长期 404）"
    assert "paths.webhook," in code and "paths.business," in code, \
        "两条路径必须各自注册（把 business 换成 webhook 会让计数还是 2 但 business 又 404）"
    assert code.count("path_token=acct.webhook_path_token") == 2, \
        "两条路由都得把 token 交给 server（请求侧要靠它复核一次）"


def test_start_webhook_without_any_credential_skips_the_route():
    """没 token 又没 secret ⇒ **不注册**（并报 error），不是"注册了但放行"。"""
    fn = _adapter_method("_start_webhook")
    guards = [n for n in ast.walk(fn)
              if isinstance(n, ast.If)
              and isinstance(n.test, ast.BoolOp) and isinstance(n.test.op, ast.And)
              and any(isinstance(c, ast.Attribute) and c.attr == "webhook_path_token"
                      for c in ast.walk(n.test))
              and any(isinstance(c, ast.Attribute) and c.attr == "webhook_secret"
                      for c in ast.walk(n.test))]
    assert guards, "找不到「无凭据」的分支"
    assert any(isinstance(s, ast.Continue) for g in guards for s in g.body), \
        "无凭据的账号必须 continue（不注册）—— 注册了就是把它敞给公网"


def test_start_webhook_errors_when_nothing_can_be_registered():
    code = _adapter_code("_start_webhook")
    assert "registered += 1" in code and "registered == 0" in code and "return False" in code, \
        "一个路由都注册不上时不该还起一个空监听（白开端口）"


def test_vendor_webhook_registration_waits_for_the_local_listener():
    """本地 bind 失败时**不许**把 URL 推给厂商（否则消息全推到死端口，日志还显示成功）。

    只对 ``_webhook_accounts`` 里（真正 add_path 成功的）账号推送。
    """
    code = _adapter_code("connect")
    assert "webhook_ok = self._start_webhook(connectable)" in code
    assert "else:" in code and "_start_vendor_webhooks(" in code
    assert "if a.id in self._webhook_accounts" in code, \
        "vendor 注册必须只覆盖本地真的注册了路由的账号"


# ===== 5. 调度出去的 future：异常必须有人看 =====


class _DoneFuture:
    """``add_done_callback`` 关心的只有 ``exception()`` 这一个方法。"""

    def __init__(self, exc: BaseException | None = None, raises: BaseException | None = None):
        self._exc = exc
        self._raises = raises

    def exception(self):
        if self._raises is not None:
            raise self._raises
        return self._exc


def test_a_failed_callback_is_logged_as_an_error(caplog):
    """★ 回调在事件循环里抛异常，``future`` 一丢就只剩 GC 时一条无声警告。

    handler 线程已经回了 200，vendor 不会重试 ⇒ 那条消息**就此消失**，
    运维面上必须留下一条带堆栈的 error。
    """
    handler = WppWebhookServer()._make_handler()
    conn = _conn("/x")
    with caplog.at_level(logging.ERROR):
        handler._log_future(conn, _DoneFuture(RuntimeError("boom")))
    assert any("回调执行失败" in r.getMessage() for r in caplog.records), \
        "回调异常没有留下 error 日志"


def test_a_successful_callback_is_silent(caplog):
    handler = WppWebhookServer()._make_handler()
    with caplog.at_level(logging.ERROR):
        handler._log_future(_conn("/x"), _DoneFuture(None))
    assert not [r for r in caplog.records if "回调执行失败" in r.getMessage()]


def test_a_cancelled_future_does_not_raise(caplog):
    """``exception()`` 在已取消的 future 上会抛 ``CancelledError`` —— 吞掉，不许炸线程。"""
    handler = WppWebhookServer()._make_handler()
    with caplog.at_level(logging.ERROR):
        handler._log_future(_conn("/x"), _DoneFuture(raises=asyncio.CancelledError()))
    assert not [r for r in caplog.records if "回调执行失败" in r.getMessage()]
