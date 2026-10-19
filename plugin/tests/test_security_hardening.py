"""2026-10-18 安全审阅的回归：入站身份绑定 / 凭据 DLP / SSRF 逐跳校验 / scoped env。

这一批缺陷的共同点是「**注册全绿、调用照常返回成功**」——它们不在工具表里，
也不在请求形状里，而在"谁有资格调、能从哪读、读到的是不是本 profile 的值"。
所以本文件只钉**安全判据本身**，不重复 ``test_tool_gate.py`` 的门禁开关覆盖：

1. ``inbound_context`` —— 模型填的 ``account_id`` 不能跨账号；``callerWxid`` 被
   真实发送者覆盖（prompt injection 的中心路径）；
2. ``netguard.is_local_read_allowed`` —— 插件目录可读 ≠ 凭据目录可读（符号链接
   也要挡住）；
3. ``netguard.safe_fetch_url`` —— 重定向**每一跳**都重新过 SSRF 校验（旧实现
   只校验初始 URL，302 到 ``127.0.0.1`` 就能把内网内容发进微信群）；
4. ``env_compat`` —— Hermes 的 scoped 读取抛错时**不许回退**进程 env（那正是
   读到另一个 profile 凭据的路径）；
5. 入站视频的**字节上限**（旧实现只限片数 ≈4GB）；
6. 2026-10-18 对齐 TS 的请求体（文件上传、撤回、emoji、语音、小程序）。
"""

from __future__ import annotations

import asyncio
import socket
import sys
import types

import pytest

from plugin.core import inbound_context


# --------------------------------------------------------------- ① 入站身份


def test_bind_fills_the_account_and_exposes_the_real_sender():
    with inbound_context.bind("acct-a", sender="wxid_boss", peer="123@chatroom"):
        args: dict = {}
        assert inbound_context.apply_account(args) == ""
        assert args["account_id"] == "acct-a"
        assert inbound_context.sender() == "wxid_boss"
        assert inbound_context.peer() == "123@chatroom"
    # 出了 with 必须复原 —— 否则下一条无关消息会继承上一条的身份
    assert inbound_context.account_id() == ""
    assert inbound_context.sender() == ""


def test_default_is_a_sentinel_not_a_cross_account_request():
    """``account_id="default"`` 是工具 schema 广告的默认值，不是"切到 default 号"。"""
    with inbound_context.bind("acct-a"):
        args = {"account_id": "default"}
        assert inbound_context.apply_account(args) == ""
        assert args["account_id"] == "acct-a"


def test_a_different_named_account_is_rejected():
    with inbound_context.bind("acct-a"):
        args = {"account_id": "acct-b"}
        reject = inbound_context.apply_account(args)
        assert "账号不匹配" in reject and "acct-b" in reject
        assert args["account_id"] == "acct-b", "拒绝时不得改写参数"


def test_no_binding_keeps_the_cli_behaviour():
    """CLI/TUI/standalone 没有入站身份 —— 显式指定账号是合法意图。"""
    assert inbound_context.account_id() == ""
    args = {"account_id": "acct-b"}
    assert inbound_context.apply_account(args) == ""
    assert args["account_id"] == "acct-b"


def test_adapt_handler_rejects_before_the_tool_runs():
    from plugin.toolkit.hermes_contract import adapt_handler

    calls: list[dict] = []

    async def fn(**params):
        calls.append(params)
        return "ok"

    handler = adapt_handler(fn)
    with inbound_context.bind("acct-a"):
        out = asyncio.run(handler({"account_id": "acct-b"}))
    assert "账号不匹配" in out
    assert calls == [], "被拒绝的工具一次都不许执行（更不许发厂商请求）"


def test_adapt_handler_injects_the_bound_account_for_ungated_tools():
    from plugin.toolkit.hermes_contract import adapt_handler

    calls: list[dict] = []

    async def fn(**params):
        calls.append(params)
        return "ok"

    handler = adapt_handler(fn)
    with inbound_context.bind("acct-a"):
        assert asyncio.run(handler({})) == "ok"
    assert calls == [{"account_id": "acct-a"}]


# --------------------------------------------------------------- ② 凭据 DLP


def test_accounts_dir_is_never_readable():
    from plugin.core import netguard

    plugin = netguard._plugin_dir()
    assert netguard.is_local_read_allowed(str(plugin / "accounts" / "default.json")) is False


def test_backup_dir_from_env_is_denied_even_under_tmp(tmp_path, monkeypatch):
    from plugin.core import netguard

    backup = tmp_path / "account-backups"
    backup.mkdir()
    secret = backup / "default.json"
    secret.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("WECHATPRO_ACCOUNT_BACKUP_DIR", str(backup))
    assert netguard.is_local_read_allowed(str(secret)) is False


def test_plugin_dotfiles_and_json_are_denied():
    from plugin.core import netguard

    plugin = netguard._plugin_dir()
    assert netguard.is_local_read_allowed(str(plugin / ".env")) is False
    assert netguard.is_local_read_allowed(str(plugin / "anything.json")) is False


def test_symlink_to_etc_passwd_is_denied(tmp_path):
    """``abspath`` 挡不住符号链接 —— 必须 ``realpath``。"""
    from plugin.core import netguard

    link = tmp_path / "passwd-link"
    link.symlink_to("/etc/passwd")
    assert netguard.is_local_read_allowed(str(link)) is False


def test_plain_tmp_files_are_allowed():
    from plugin.core import netguard

    assert netguard.is_local_read_allowed("/tmp/some-image.png") is True


def test_sensitive_files_are_denied_even_under_tmp(tmp_path):
    """``/tmp`` 无条件可读不等于"什么都能发" —— 凭据文件名全局拒绝。"""
    from plugin.core import netguard

    for name in (".env", ".env.local", "id_rsa", "server.pem", "credentials",
                 "client.key", ".netrc"):
        p = tmp_path / name
        p.write_text("x", encoding="utf-8")
        assert netguard.is_local_read_allowed(str(p)) is False, name
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    key = ssh / "config"
    key.write_text("x", encoding="utf-8")
    assert netguard.is_local_read_allowed(str(key)) is False
    # 普通文件仍然可发（别把闸口收得没人敢用）
    assert netguard.is_local_read_allowed(str(tmp_path / "report.pdf")) is True


def test_scope_home_prevents_cross_profile_reads(tmp_path, monkeypatch):
    """scope 明确时只放行**当前** profile；别的 profile 的 workspace 不读。"""
    from plugin.core import netguard

    monkeypatch.setattr(netguard, "_ALWAYS_READABLE", ())
    home = tmp_path / "hermes"
    for name in ("wpp-wechat", "wecom"):
        (home / "profiles" / name / "workspace").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setattr(netguard, "_current_profile_home",
                        lambda: home / "profiles" / "wpp-wechat")
    mine = home / "profiles" / "wpp-wechat" / "workspace" / "a.png"
    other = home / "profiles" / "wecom" / "workspace" / "secret.png"
    assert netguard.is_local_read_allowed(str(mine)) is True
    assert netguard.is_local_read_allowed(str(other)) is False


# ---------------------------------------------------------------- ③ SSRF


def _fake_getaddrinfo(host, port, *a, **k):
    ip = "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


def test_endpoint_guard_rejects_literals_but_allows_domains():
    from plugin.core import netguard

    for url in ("http://127.0.0.1:8062/mcp", "http://169.254.169.254/",
                "http://localhost:8080/", "file:///etc/passwd"):
        with pytest.raises(netguard.GuardError):
            netguard.assert_endpoint_public(url)
    assert netguard.assert_endpoint_public("https://api.example.com/v1") == \
        "https://api.example.com/v1"


@pytest.mark.parametrize("url", [
    "http://100.64.0.1/",        # CGNAT：is_private=False 但能路由到内网设备
    "http://198.18.0.1/",        # 基准测试网段
])
def test_ssrf_blocks_non_global_literal_ranges(url):
    from plugin.core import netguard

    with pytest.raises(netguard.GuardError):
        netguard.assert_endpoint_public(url)


def test_public_url_guard_resolves_dns(monkeypatch):
    from plugin.core import netguard

    monkeypatch.setattr(netguard.socket, "getaddrinfo", _fake_getaddrinfo)
    assert netguard.assert_public_url("http://example.com/x") == "http://example.com/x"
    with pytest.raises(netguard.GuardError):
        netguard.assert_public_url("http://127.0.0.1/x")


def test_the_resolver_pins_the_validated_ip_and_refuses_other_hosts(monkeypatch):
    """校验与连接必须看到同一批 IP（不然 DNS rebinding 仍能换到 127.0.0.1）。

    ``resolve`` 只回放已校验的 IP；换成另一个主机名必须拒 —— 那是重定向/绕过的信号。
    """
    from plugin.core import netguard

    monkeypatch.setattr(netguard.socket, "getaddrinfo", _fake_getaddrinfo)
    host, port, targets = netguard._resolve_public_targets("https://例子.中国/x")
    assert (host, port) == ("例子.中国", 443)
    assert targets == [(socket.AF_INET, "93.184.216.34")]

    resolver = netguard._PinnedResolver(host, port, targets)
    puny = "例子.中国".encode("idna").decode("ascii")
    got = asyncio.run(resolver.resolve(puny, 443))
    assert got[0]["host"] == "93.184.216.34", "连接目标必须是校验过的 IP"
    assert got[0]["hostname"] == puny, "SNI / 证书校验仍用原域名"
    with pytest.raises(OSError):
        asyncio.run(resolver.resolve("evil.example", 443))


def test_redirect_to_loopback_is_revalidated(monkeypatch):
    """★ 旧实现用 ``allow_redirects=True``：初始 URL 合法，302 到内网就绕过了。"""
    from plugin.core import netguard

    monkeypatch.setattr(netguard.socket, "getaddrinfo", _fake_getaddrinfo)

    class _Resp:
        def __init__(self, status, headers=None):
            self.status = status
            self.headers = headers or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def read(self):
            return b"payload"

    fetched: list[str] = []

    class _Sess:
        def __init__(self, connector=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, **_kw):
            fetched.append(url)
            return _Resp(302, {"Location": "http://127.0.0.1:8062/mcp"})

    fake = types.SimpleNamespace(ClientSession=_Sess,
                                 ClientTimeout=lambda **kw: None,
                                 TCPConnector=lambda **kw: None)
    monkeypatch.setitem(sys.modules, "aiohttp", fake)

    with pytest.raises(netguard.GuardError) as ei:
        asyncio.run(netguard.safe_fetch_url("http://example.com/image.png"))
    assert "SSRF" in str(ei.value)
    assert fetched == ["http://example.com/image.png"], \
        "第二跳必须在**发请求前**被拦下"


def test_declared_content_length_over_cap_is_rejected(monkeypatch):
    from plugin.core import netguard

    monkeypatch.setattr(netguard.socket, "getaddrinfo", _fake_getaddrinfo)

    class _Resp:
        status = 200
        headers = {"Content-Length": "9999"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def read(self):  # pragma: no cover - 应在 read 之前就拒
            raise AssertionError("声明超限还在读 body")

    class _Sess:
        def __init__(self, connector=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, **_kw):
            return _Resp()

    monkeypatch.setitem(sys.modules, "aiohttp",
                        types.SimpleNamespace(ClientSession=_Sess,
                                              ClientTimeout=lambda **kw: None,
                                              TCPConnector=lambda **kw: None))
    with pytest.raises(netguard.GuardError):
        asyncio.run(netguard.safe_fetch_url("http://example.com/x", max_bytes=100))


# ------------------------------------------------------------- ④ scoped env


def _fake_shared(monkeypatch, **fns):
    """把 ``gateway.platforms._shared`` 换成一个只有给定函数的假模块。"""
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []
    platforms = types.ModuleType("gateway.platforms")
    platforms.__path__ = []
    shared = types.ModuleType("gateway.platforms._shared")
    for name, fn in fns.items():
        setattr(shared, name, fn)
    gateway.platforms = platforms
    platforms._shared = shared
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.platforms", platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms._shared", shared)


def test_secret_env_uses_the_scoped_reader_not_the_process_env(monkeypatch):
    from plugin.core import env_compat

    _fake_shared(monkeypatch, get_scoped_secret=lambda name, default=None: "scoped-" + name)
    monkeypatch.setenv("WPP_TEST_SECRET", "process-value")
    assert env_compat.secret_env("WPP_TEST_SECRET") == "scoped-WPP_TEST_SECRET"


def test_secret_env_does_not_fall_back_to_process_env_on_error(monkeypatch):
    """★ 旧实现把 ``import`` 和调用包在同一个 ``suppress(Exception)`` 里 ⇒
    读取一抛错就落到 ``os.environ``，恰好读回**另一个 profile** 的凭据。"""
    from plugin.core import env_compat

    def boom(name, default=None):
        raise RuntimeError("scope missing")

    _fake_shared(monkeypatch, get_scoped_secret=boom)
    monkeypatch.setenv("WPP_TEST_SECRET", "process-value")
    assert env_compat.secret_env("WPP_TEST_SECRET", "dflt") == "dflt"


def test_gate_env_prefers_the_per_profile_reader(monkeypatch):
    from plugin.core import env_compat

    _fake_shared(monkeypatch, platform_gate_env=lambda name, default="": "scoped")
    monkeypatch.setenv("WPP_TEST_GATE", "process")
    assert env_compat.gate_env("WPP_TEST_GATE") == "scoped"


# --------------------------------------------------------- ⑤ 视频字节上限


def test_video_download_stops_at_the_byte_cap(stubbed, monkeypatch):
    from plugin.core import netguard
    from plugin.platform import adapter as adapter_mod

    monkeypatch.setattr(netguard, "DEFAULT_MAX_BYTES", 1000)

    class _Client:
        def __init__(self):
            self.calls = 0

        async def download_video(self, **_kw):
            self.calls += 1
            return b"x" * 400

    msg = types.SimpleNamespace(
        msg_id="m1",
        media={"video_ctx": {"data_len": 100_000, "msg_id": 1, "to_wxid": "wxid_a"}},
    )
    client = _Client()
    out = asyncio.run(adapter_mod.WppAdapter._download_video_chunks(None, client, msg))
    assert out is None, "超过 200MB 时必须放弃，不能把整段视频读进内存"
    assert client.calls == 3, f"应在第 3 片触发上限，实际拉了 {client.calls} 片"


def test_small_video_is_downloaded_completely(stubbed):
    from plugin.platform import adapter as adapter_mod

    class _Client:
        async def download_video(self, **kw):
            remaining = kw["data_len"] - kw["start_pos"]
            return b"y" * min(400, remaining)

    msg = types.SimpleNamespace(
        msg_id="m2",
        media={"video_ctx": {"data_len": 800, "msg_id": 2, "to_wxid": "wxid_a"}},
    )
    out = asyncio.run(adapter_mod.WppAdapter._download_video_chunks(None, _Client(), msg))
    assert out == b"y" * 800


# ----------------------------------------------------- ⑥ 线上请求体（TS 对齐）


class _RecordingClient:
    """只记录 ``call(endpoint, body)``，按预置响应依次返回。"""

    def __init__(self, responses=None):
        self.sent: list[tuple[str, dict]] = []
        self.responses = list(responses or [])
        #: ``set_webhook`` 直接把 authcode 写进请求体（TS 契约）—— 假值即可
        self.authcode = "AC"
        #: 真 ``WppClient`` 在构造期捕获白名单；鸭子类型替身补同样的属性
        self.send_allowlist: tuple[str, ...] = ()
        self.account_id = "acct"

    async def call(self, endpoint, body=None, method="POST", *,
                   audit=True, **_kw):
        self.sent.append((endpoint, dict(body or {})))
        return self.responses.pop(0) if self.responses else {
            "Success": True, "Code": 0, "Data": {}}


def _wire(method_name, *args, responses=None):
    """用 ``WppClient`` 的真方法跑一遍，返回记录下来的端点/请求体。"""
    from plugin.core.api_client import WppClient

    client = _RecordingClient(responses)
    fn = getattr(WppClient, method_name)
    asyncio.run(fn(client, *args))
    return client.sent


def test_revoke_uses_the_swagger_keys_and_int_values():
    sent = _wire("revoke_msg", "444", "555", 1700000000, "wxid_a")
    endpoint, body = sent[0]
    assert endpoint == "/Msg/Revoke"
    assert body == {"ClientMsgId": 444, "NewMsgId": 555,
                    "CreateTime": 1700000000, "ToUserName": "wxid_a"}
    assert "MsgId" not in body and "ToWxid" not in body


def test_emoji_uses_md5_and_total_len():
    sent = _wire("send_emoji", "wxid_a", "abc123", 321)
    assert sent[0] == ("/Msg/SendEmoji",
                       {"ToWxid": "wxid_a", "Md5": "abc123", "TotalLen": 321})


def test_voice_sends_type_and_voice_time():
    sent = _wire("send_voice", "wxid_a", "B64", 4, 7)
    assert sent[0] == ("/Msg/SendVoice",
                       {"ToWxid": "wxid_a", "Base64": "B64",
                        "Type": 4, "VoiceTime": 7})


def test_mini_program_modern_xml_when_page_path_is_given():
    sent = _wire("send_xcx", "wxid_a", "标题", "描述", "https://x/mp",
                 "wx123", "https://x/t.jpg", "pages/a/b", "gh_abc")
    endpoint, body = sent[0]
    assert endpoint == "/Msg/SendXCX"
    assert set(body) == {"ToWxid", "Content"}, "厂商只认 ToWxid + Content"
    xml = body["Content"]
    assert "<type>33</type>" in xml
    assert "<pagepath>pages/a/b</pagepath>" in xml
    assert "<username>gh_abc</username>" in xml


def test_mini_program_legacy_xml_without_page_path():
    sent = _wire("send_xcx", "wxid_a", "标题", "描述", "https://x/mp", "wx123")
    xml = sent[0][1]["Content"]
    assert "<type>2001</type>" in xml and "<mmapp>" in xml


def test_file_upload_then_share_link_type_6():
    sent = _wire(
        "send_file_via_app", "wxid_a", "报表.pdf", "QUJD", 0,
        responses=[{"Success": True, "Code": 0, "Data": {"mediaId": "MID-1"}},
                   {"Success": True, "Code": 0, "Data": {}}],
    )
    assert sent[0] == ("/Tools/UploadFile", {"base64": "QUJD"}), \
        "旧实现调的 /Msg/UploadFile 端点不存在"
    endpoint, body = sent[1]
    assert endpoint == "/Msg/ShareLink"
    assert body["Type"] == 6
    xml = body["Xml"]
    assert "attachid=MID-1" in xml and "totallen=3" in xml and "fileext=pdf" in xml


def test_file_upload_without_media_id_fails_closed():
    sent = _wire(
        "send_file_via_app", "wxid_a", "报表.pdf", "QUJD",
        responses=[{"Success": True, "Code": 0, "Data": {}}],
    )
    assert len(sent) == 1, "没拿到 mediaId 就不许发 ShareLink（否则发的是空文件）"


def test_set_webhook_sends_the_ts_body():
    """v1.1.17 P0-E：漏 ``enabled:true`` ⇒ Go 零值 false ⇒ webhook 设了等于没设。"""
    sent = _wire("set_webhook", "https://example.com/hook")
    assert sent[0] == ("/Webhook/Set", {
        "url": "https://example.com/hook",
        "authcode": "AC",
        "enabled": True,
        "retryCount": 3,
        "timeout": 10,
        "messageTypes": ["all"],
    })


def test_set_webhook_includes_secret_only_when_configured():
    """vendor 用 ``secret`` 对回调做 v2 HMAC；空串绝不下发（否则会清掉已有 secret）。"""
    assert "secret" not in _wire("set_webhook", "https://example.com/hook")[0][1]
    sent = _wire("set_webhook", "https://example.com/hook", "s3cret")
    assert sent[0][1]["secret"] == "s3cret"
    assert sent[0][1]["enabled"] is True


def test_webhook_tool_transform_and_authcode_injection():
    """通用工具路：transform 补默认值，唯一出口补请求体 authcode（TS 契约）。"""
    from plugin.toolkit import policy
    from plugin.toolkit.registry import register_generic_tools

    assert policy._webhook_set({"url": "u"}) == {
        "url": "u", "enabled": True, "retryCount": 3, "timeout": 10,
        "messageTypes": ["all"],
    }

    client = _RecordingClient()
    adapter = types.SimpleNamespace(_clients={"acct": client})

    class _Ctx:
        def __init__(self):
            self.tools = {}

        def register_tool(self, **kw):
            self.tools[kw["name"]] = kw

    ctx = _Ctx()
    register_generic_tools(ctx, lambda: adapter, [{
        "name": "wpp_webhook_set", "endpoint": "/Webhook/Set",
        "description": "d", "params": {"url": {"type": "string"}},
        "transform": policy._webhook_set,
    }])
    asyncio.run(ctx.tools["wpp_webhook_set"]["handler"](
        account_id="acct", url="https://example.com/hook"))
    endpoint, body = client.sent[0]
    assert endpoint == "/Webhook/Set"
    assert body["enabled"] is True and body["authcode"] == "AC"


# ------------------------------------------- ⑦ 出站闸：无目标上传步骤（2026-10-19）


def test_targetless_upload_needs_guard_targets(tmp_path, monkeypatch):
    """白名单开着时：裸 ``/Tools/UploadFile`` 依旧 fail-closed；带 guard_targets
    （已校验收件人）才放行；上下文里塞白名单外的收件人同样拒。"""
    from plugin.core import netguard, outbound_guard

    monkeypatch.setattr(netguard, "send_allowlist", lambda: ("wxid_boss",))
    with pytest.raises(netguard.GuardError):
        outbound_guard.guard("/Tools/UploadFile", {"base64": "QUJD"}, "acct")
    with outbound_guard.guard_targets("wxid_boss"):
        outbound_guard.guard("/Tools/UploadFile", {"base64": "QUJD"}, "acct")
    with pytest.raises(netguard.GuardError), outbound_guard.guard_targets("wxid_victim"):
        outbound_guard.guard("/Tools/UploadFile", {"base64": "QUJD"}, "acct")
    # 上下文随 with 退出复位，不会泄漏给后续请求
    assert outbound_guard._upload_targets_var.get() == ()


def test_file_upload_carries_guard_context_only_over_upload_step():
    """send_file_via_app 的真行为：上传步骤（体里没收件人）期间闸上下文里是**收件人**，
    ShareLink 步骤自己带 ToWxid ⇒ 上下文已复位。没有上下文的裸上传在白名单模式下
    仍会被 fail-closed 拒（上一条测试钉住）。"""
    import asyncio as _aio
    from plugin.core import netguard, outbound_guard
    from plugin.core.api_client import WppClient

    monkey_calls: list = []

    class _CtxProbe(_RecordingClient):
        async def call(self, endpoint, body=None, method="POST", max_retries=2, *, audit=True):
            monkey_calls.append((endpoint, outbound_guard._upload_targets_var.get()))
            return await super().call(endpoint, body, method=method, max_retries=max_retries, audit=audit)

    net_send_allowlist = netguard.send_allowlist
    try:
        netguard.send_allowlist = lambda: ("wxid_boss",)
        client = _CtxProbe([{"Success": True, "Code": 0, "Data": {"mediaId": "MID-1"}},
                            {"Success": True, "Code": 0, "Data": {}}])
        # 真客户端在构造期捕获白名单；替身手动补上（发送任务掉 scope 后也不重读 env）
        client.send_allowlist = ("wxid_boss",)
        _aio.run(WppClient.send_file_via_app(client, "wxid_boss", "报表.pdf", "QUJD", 0))
    finally:
        netguard.send_allowlist = net_send_allowlist
    assert monkey_calls == [("/Tools/UploadFile", ("wxid_boss",)),
                            ("/Msg/ShareLink", ())], monkey_calls


def test_file_xml_escapes_ext_and_sizes_without_decoding():
    """``fileext`` 来自**文件名**，同样要过 XML 转义；``totallen`` 用长度算，不 decode。"""
    from plugin.core.api_client import WppClient

    client = _RecordingClient([{"Code": 0, "Data": {"mediaId": "MID-1"}},
                               {"Code": 0, "Data": {}}])
    asyncio.run(WppClient.send_file_via_app(client, "wxid_a", "坏.<x&y>", "QUJD"))
    xml = client.sent[1][1]["Xml"]
    assert "fileext=&lt;x&amp;y&gt;" in xml
    assert "<fileext>&lt;x&amp;y&gt;</fileext>" in xml
    assert "<totallen>3</totallen>" in xml, "QUJD 解码后 3 字节，且不许为算它去 b64decode"


def test_oversized_file_is_rejected_before_upload(monkeypatch):
    """超限必须在**上传前**拒 —— 否则 267MB base64 已经发到 vendor 了才发现。"""
    import plugin.core.api_client as api

    monkeypatch.setattr(api, "_MAX_B64_JSON_BYTES", 8)
    client = _RecordingClient()
    with pytest.raises(api.WppApiError) as exc:
        asyncio.run(api.WppClient.send_file_via_app(
            client, "wxid_a", "a.bin", "QUJDQUJDQUJD"))
    assert exc.value.code == "file_too_large"
    assert client.sent == [], "一个请求都不该发出去"


# ------------------------------------------- ⑧ profile workspace 可读（HERMES_PROFILE 缺失）


def test_profile_workspace_readable_without_profile_env(tmp_path, monkeypatch):
    """多 profile 网关不带 HERMES_PROFILE ⇒ 旧白名单只放行 ~/.hermes/workspace，
    profile 自己的 workspace/cache 全被拒（wpp_send_image 从 profile 目录发图必挂）。
    修复后：profiles/*/（workspace|cache|tmp）可读；凭据目录照旧永远拒。"""
    from plugin.core import netguard

    home = tmp_path / ".hermes"
    ws = home / "profiles" / "wpp-wechat" / "workspace" / "media"
    ws.mkdir(parents=True)
    img = ws / "x.jpg"
    img.write_bytes(b"x")
    cred = home / "profiles" / "wpp-wechat" / ".env"
    cred.write_text("SECRET=1")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    # ⚠️ ``tmp_path`` 在 /tmp 下，而 ``_ALWAYS_READABLE`` 无条件放行 /tmp —— 不剥掉它，
    #    「profile 根目录的 .env 被拒」这条断言会因测试目录本身在 /tmp 而假红
    #    （实现没毛病：生产路径 /root/.hermes/profiles/*/.env 不在 /tmp，实测为 False）。
    monkeypatch.setattr(netguard, "_ALWAYS_READABLE", ())
    assert netguard.is_local_read_allowed(str(img)) is True
    assert netguard.is_local_read_allowed(str(cred)) is False


# --------------------------------- ⑨ phoneerp 权限：身份只认入站 contextvar（2026-10-19）


def _phoneerp_handlers():
    """注册 phoneerp 四件套并取回 handler（真表 + 假 ctx）。"""
    from plugin.toolkit import phoneerp

    class _Ctx:
        def __init__(self):
            self.tools = {}

        def register_tool(self, **kwargs):
            self.tools[kwargs["name"]] = kwargs

    ctx = _Ctx()
    assert phoneerp.register(ctx, lambda: None) == 4
    return ctx.tools


def test_phoneerp_caller_userid_prefers_bound_sender():
    """模型填的 userid 是 injection 可改写的；绑定存在时一律以真实发送者为准。"""
    from plugin.toolkit.phoneerp import _caller_userid

    with inbound_context.bind("acct-a", sender="wxid_real"):
        assert _caller_userid("wxid_boss") == "wxid_real"
    # 没有绑定（CLI/TUI 直调）才回退到显式传参 —— 保持旧行为
    assert _caller_userid("wxid_explicit") == "wxid_explicit"


@pytest.mark.parametrize("tool,params", [
    ("phoneerp_query", {"query": "零售明细 今天"}),
    ("phoneerp_location", {"store": "3号店"}),
    ("phoneerp_imei", {"imei": "860430084378288"}),
    ("phoneerp_natural", {"query": "库存汇总"}),
])
def test_phoneerp_tools_pass_real_sender_as_userid(tool, params, monkeypatch):
    """四个工具都必须把**真实发送者 wxid** 作为 ``--userid`` 传给 CLI。

    旧实现把 `` userid=xxx`` 拼进 query 文本，而 CLI 只认 ``--userid`` 开关
    ⇒ 权限过滤从未生效；且模型可以把 userid 留空拿到老板权限。
    """
    calls = []

    class _R:
        returncode = 0
        stdout = '{"ok": true}'
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _R()

    monkeypatch.setattr("plugin.toolkit.phoneerp.subprocess.run", fake_run)
    handler = _phoneerp_handlers()[tool]["handler"]
    with inbound_context.bind("acct-a", sender="wxid_real"):
        asyncio.run(handler(**params))

    argv = calls[0]
    assert argv[argv.index("--userid") + 1] == "wxid_real"
    assert not any("userid=" in str(a) for a in argv), \
        "userid 必须走开关，不能拼进 query 文本（CLI 不解析）"


def test_phoneerp_tools_without_binding_stay_boss_mode(monkeypatch):
    """没有入站绑定（CLI/TUI）⇒ 不传 --userid，与旧行为一致。"""
    calls = []

    class _R:
        returncode = 0
        stdout = '{"ok": true}'
        stderr = ""

    monkeypatch.setattr("plugin.toolkit.phoneerp.subprocess.run",
                        lambda argv, **kw: calls.append(argv) or _R())
    handler = _phoneerp_handlers()["phoneerp_query"]["handler"]
    asyncio.run(handler(query="零售明细 今天"))
    assert "--userid" not in calls[0]


def test_phoneerp_query_schema_does_not_offer_userid():
    """schema 里不能再出现 userid —— 否则模型会以为权限由它决定。"""
    props = _phoneerp_handlers()["phoneerp_query"]["schema"]["properties"]
    assert "userid" not in props


def test_group_message_identity_is_the_member_not_the_room():
    """群聊权限必须按**发消息的员工**，不能拿群 id 当身份。

    两种真实 payload 形态都要认：
    * 新格式：``FromUserName`` 是发送者，``ChatroomId`` 是群；
    * 旧格式：``FromUserName`` 是群，``sender_id`` 才是发送者。
    """
    from plugin.inbound.parser import parse_message

    new_fmt = parse_message("acct-a", {
        "FromUserName": "wxid_staff", "ChatroomId": "123@chatroom",
        "content": "零售明细 昨天", "msgId": "m1", "MsgType": 1,
    })
    assert new_fmt.from_wxid == "wxid_staff"
    assert new_fmt.chatroom_id == "123@chatroom"

    old_fmt = parse_message("acct-a", {
        "FromUserName": "123@chatroom", "sender_id": "wxid_staff",
        "is_group": True, "content": "零售明细 昨天", "msgId": "m2", "MsgType": 1,
    })
    assert old_fmt.from_wxid == "wxid_staff", "群成员身份不能被群 id 覆盖"
    assert old_fmt.chatroom_id == "123@chatroom"


def test_group_query_passes_the_member_wxid_not_the_room(monkeypatch):
    """按 adapter 的绑法（sender=成员，peer=群）调工具 ⇒ --userid 必须是成员。"""
    from plugin.inbound.parser import parse_message

    msg = parse_message("acct-a", {
        "FromUserName": "wxid_staff", "ChatroomId": "123@chatroom",
        "content": "零售明细 昨天", "msgId": "m3", "MsgType": 1,
    })
    calls = []

    class _R:
        returncode = 0
        stdout = '{"ok": true}'
        stderr = ""

    monkeypatch.setattr("plugin.toolkit.phoneerp.subprocess.run",
                        lambda argv, **kw: calls.append(argv) or _R())
    handler = _phoneerp_handlers()["phoneerp_query"]["handler"]
    with inbound_context.bind("acct-a", msg.from_wxid, msg.chatroom_id):
        asyncio.run(handler(query="零售明细 昨天"))

    argv = calls[0]
    assert argv[argv.index("--userid") + 1] == "wxid_staff"
    assert argv[argv.index("--userid") + 1] != "123@chatroom"
