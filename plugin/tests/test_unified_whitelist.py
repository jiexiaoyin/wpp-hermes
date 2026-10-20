"""统一白名单（2026-10-19）：**收发同一份** + 全员群成员同步。

判据不测"函数被调用了"，而是测三件会真正失效的事：

1. 出站放行集合确实 = 账号 ``allowFrom`` ∪ ``groupAllowFrom`` ∪ ``adminUsers`` ∪ env；
   否则「联系人甲能私聊机器人、机器人一转发就被拦」的割裂还在。
2. 全员群同步是**两键一次原子写**，且厂商失败时**一个字节都不写** ——
   网络抖动不能把员工误删出名单。
3. ``/user sync`` 与 ``/whitelist status`` 走的是同一条聚合逻辑，回执说人话。
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from plugin.core import netguard
from plugin.inbound import employee_sync
from plugin.platform import commands


def _sync(coro_fn):
    @functools.wraps(coro_fn)
    def _wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return _wrapper


class _Acct:
    def __init__(self, allow=(), groups=(), admins=()):
        self.allow_from = tuple(allow)
        self.group_allow_from = tuple(groups)
        self.admin_users = tuple(admins)


# ============================================================ 聚合口径

def test_unified_allowlist_unions_account_sources_and_env(monkeypatch):
    monkeypatch.setattr(netguard, "env_allowlist", lambda *_a, **_k: ("env_only", "u1"))
    got = netguard.unified_allowlist(
        profile_home="/x",
        accounts=[
            _Acct(allow=("u1", "wxid_contact_demo"), groups=("g1@chatroom",),
                  admins=("admin",)),
            _Acct(allow=("u2",), groups=("g2@chatroom",)),
        ],
    )
    assert got == ("env_only", "u1", "wxid_contact_demo", "g1@chatroom", "admin",
                   "u2", "g2@chatroom")


def test_unified_allowlist_degrades_to_env_when_accounts_cannot_load(monkeypatch):
    monkeypatch.setattr(netguard, "env_allowlist", lambda *_a, **_k: ("env_only",))
    from plugin.core import accounts as accounts_mod

    def _boom(*_a, **_k):
        raise RuntimeError("账号目录坏了")

    monkeypatch.setattr(accounts_mod, "load_all", _boom)
    assert netguard.unified_allowlist() == ("env_only",)


def test_guard_error_points_to_the_command_not_the_env_file(monkeypatch):
    monkeypatch.setattr(netguard, "unified_allowlist", lambda *_a, **_k: ("u1",))
    netguard.check_outbound_allowed("u1")
    with pytest.raises(netguard.GuardError) as exc:
        netguard.check_outbound_allowed("wxid_contact_demo")
    msg = str(exc.value)
    assert "/user add" in msg and "/user sync" in msg
    assert "WECHATPRO_SEND_ALLOWLIST" not in msg, \
        "报错不该再把运维引去改 .env（那正是老板要消灭的摩擦）"


# ============================================================ 成员解析 / 同步计划

def test_parse_members_filters_self_rooms_official_and_system_accounts():
    payload = {
        "Code": 0,
        "Data": {"NewChatroomData": {"ChatRoomMember": [
            {"UserName": "wxid_bot", "NickName": "机器人"},
            {"UserName": "wxid_contact_demo", "NickName": "联系人甲"},
            {"UserName": "wxid_owner_demo", "NickName": "示例联系人"},
            {"UserName": "gh_123", "NickName": "公众号"},
            {"UserName": "123@chatroom", "NickName": "群"},
            {"UserName": "filehelper", "NickName": "文件传输助手"},
            {"UserName": "", "NickName": "空"},
        ]}},
    }
    assert employee_sync.parse_members(payload, exclude=("wxid_bot",)) == [
        ("wxid_contact_demo", "联系人甲"), ("wxid_owner_demo", "示例联系人")]


def test_plan_sync_keeps_manual_entries_and_removes_only_stale_auto_entries():
    allow, auto = employee_sync.plan_sync(
        current_allow=["manual", "a", "b"],
        previous_auto=["a", "b"],
        members=["a", "c"],
    )
    assert allow == ["manual", "a", "c"]
    assert auto == ["a", "c"]


def test_plan_sync_first_run_treats_every_existing_entry_as_manual():
    allow, auto = employee_sync.plan_sync(
        current_allow=["boss"], previous_auto=[], members=["boss", "staff"])
    assert allow == ["boss", "staff"]
    assert auto == ["staff"]


def test_plan_sync_deny_list_blocks_re_adding_and_drops_stale_auto_entries():
    """手工移除的人写进 employeeDenyList ⇒ 下一轮不许再被自动加回来。"""
    allow, auto = employee_sync.plan_sync(
        current_allow=["boss", "a"],
        previous_auto=["a", "gone"],
        members=["a", "gone", "staff"],
        deny=["gone"],
    )
    assert allow == ["boss", "a", "staff"]
    assert auto == ["a", "staff"]


def test_plan_sync_deny_list_does_not_override_a_manual_entry():
    """deny 只约束自动同步 —— 老板手工加进来的人仍然保留（手工意图优先）。"""
    allow, auto = employee_sync.plan_sync(
        current_allow=["boss", "manual_denied"],
        previous_auto=[],
        members=["manual_denied", "staff"],
        deny=["manual_denied"],
    )
    assert allow == ["boss", "manual_denied", "staff"]
    assert auto == ["staff"]


def test_deny_list_accepts_string_or_list():
    assert employee_sync.deny_list_from_raw(
        {"employeeDenyList": "wxid_one"}) == ["wxid_one"]
    assert employee_sync.deny_list_from_raw(
        {"employeeDenyList": ["wxid_one", " wxid_two ", ""]}) == [
            "wxid_one", "wxid_two"]
    assert employee_sync.deny_list_from_raw({}) == []


# ============================================================ sync_once

PAYLOAD = {"Code": 0, "Data": {"NewChatroomData": {"ChatRoomMember": [
    {"UserName": "wxid_bot", "NickName": "机器人"},
    {"UserName": "wxid_contact_demo", "NickName": "联系人甲"},
    {"UserName": "wxid_staff", "NickName": "员工"},
]}}}


class _FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else PAYLOAD
        self.error = error
        self.calls = []

    async def call(self, endpoint, body=None, **kwargs):
        self.calls.append((endpoint, body))
        if self.error:
            raise self.error
        return self.payload


class _FakeAdapter:
    def __init__(self, raw=None):
        self.raw = dict(raw or {"selfWxid": "wxid_bot"})
        self.reloaded = []

    def account(self, _account_id):
        return type("A", (), {"self_wxid": self.raw.get("selfWxid", "")})()

    def reload_account(self, account_id):
        self.reloaded.append(account_id)
        return True


def _patch_account_store(monkeypatch, raw):
    from plugin.core import account_store

    writes = []

    monkeypatch.setattr(account_store, "read_account_json",
                        lambda _aid: dict(raw))
    monkeypatch.setattr(account_store, "write_account_keys",
                        lambda aid, updates: writes.append((aid, updates)) or {})
    return writes


@_sync
async def test_sync_once_writes_both_keys_atomically_and_reloads(monkeypatch):
    raw = {"selfWxid": "wxid_bot", "allowFrom": ["boss", "wxid_contact_demo"],
           "employeeAllowFrom": []}
    writes = _patch_account_store(monkeypatch, raw)
    adapter = _FakeAdapter(raw)
    res = await employee_sync.sync_once(
        _FakeClient(), "acct", ["44971342037@chatroom"], adapter=adapter)

    assert res["ok"] is True
    assert res["members"] == 1 and res["added"] == 1
    # 首次同步没有台账 ⇒ 现有 allowFrom 一律按"手工项"保护，只把新成员记成自动项。
    assert writes == [("acct", {
        "allowFrom": ["boss", "wxid_contact_demo", "wxid_staff"],
        "employeeAllowFrom": ["wxid_staff"],
    })]
    assert adapter.reloaded == ["acct"]


@_sync
async def test_sync_once_reads_the_account_deny_list(monkeypatch):
    """配置里的 employeeDenyList 真的进了 plan_sync（而不是只解析没人消费）。"""
    raw = {"selfWxid": "wxid_bot", "allowFrom": ["boss"],
           "employeeAllowFrom": ["wxid_staff"],
           "employeeDenyList": ["wxid_staff"]}
    writes = _patch_account_store(monkeypatch, raw)
    res = await employee_sync.sync_once(
        _FakeClient(), "acct", ["44971342037@chatroom"],
        adapter=_FakeAdapter(raw))

    assert res["ok"] is True
    assert res["removed"] == 1
    assert writes == [("acct", {
        "allowFrom": ["boss", "wxid_contact_demo"],
        "employeeAllowFrom": ["wxid_contact_demo"],
    })]


@_sync
async def test_sync_once_never_writes_when_the_vendor_call_fails(monkeypatch):
    raw = {"selfWxid": "wxid_bot", "allowFrom": ["boss", "old_employee"],
           "employeeAllowFrom": ["old_employee"]}
    writes = _patch_account_store(monkeypatch, raw)
    res = await employee_sync.sync_once(
        _FakeClient(error=RuntimeError("timeout")), "acct",
        ["449713420037@chatroom"], adapter=_FakeAdapter(raw))
    assert res["ok"] is False
    assert writes == [], "厂商抖动绝不能把员工误删出白名单"


# ============================================================ 命令面

class _CmdAdapter(_FakeAdapter):
    def __init__(self, raw=None):
        super().__init__(raw)
        self._clients = {"acct": _FakeClient()}
        self._profile_home = None
        self._employee_sync_state = {}
        self.reloaded = []

    def _employee_group_ids(self, _account_id):
        return ["44971342037@chatroom"]


@_sync
async def test_user_sync_command_reports_the_change(monkeypatch):
    raw = {"selfWxid": "wxid_bot", "allowFrom": ["boss"]}
    writes = _patch_account_store(monkeypatch, raw)
    adapter = _CmdAdapter(raw)
    out = await commands.handle_command_async("acct", "/user sync", adapter=adapter)
    assert "✅" in out and "新增 2" in out
    assert writes and writes[0][1]["allowFrom"] == ["boss", "wxid_contact_demo", "wxid_staff"]


def test_whitelist_status_reports_every_source(monkeypatch):
    monkeypatch.setattr(commands, "_allowlist_items",
                        lambda _aid, key: {
                            "allowFrom": ["boss", "wxid_contact_demo"],
                            "groupAllowFrom": ["g1@chatroom", "g2@chatroom"],
                        }[key])
    monkeypatch.setattr(commands, "_account_file_cfg",
                        lambda _aid: {"adminUsers": ["boss"],
                                      "employeeAllowFrom": ["wxid_contact_demo"]})
    monkeypatch.setattr(netguard, "env_allowlist", lambda *_a, **_k: ("env_only",))
    adapter = _CmdAdapter({"adminUsers": ["boss"]})
    out = commands.handle_command("acct", "/whitelist status", adapter=adapter)
    assert "统一白名单" in out
    assert "私聊 allowFrom: 2" in out
    assert "群 groupAllowFrom: 2" in out
    assert "管理员 adminUsers: 1" in out
    assert "44971342037@chatroom" in out
    assert "出站生效目标合计: 5" in out


def test_outbound_is_an_alias_of_whitelist_status(monkeypatch):
    monkeypatch.setattr(commands, "_handle_whitelist",
                        lambda _aid, _args, adapter=None: "ALIAS")
    assert commands.handle_command("acct", "/outbound", adapter=None) == "ALIAS"


def test_employee_sync_interval_is_30_days():
    """老板 2026-10-19 定：全员群每 30 天同步一轮。

    启动时仍会立即跑第一轮；要立刻刷新用 ``/user sync``。
    """
    assert employee_sync.SYNC_INTERVAL_SEC == 30 * 24 * 3600


# ============================================================ 按账号隔离（出站）

class _Client:
    def __init__(self):
        self.send_allowlist = ()


def _bare_adapter(accounts):
    from plugin.platform.adapter import WppAdapter

    ad = WppAdapter.__new__(WppAdapter)
    for aid, acct in accounts.items():
        acct.id = aid
    ad._accounts = dict(accounts)
    ad._clients = {aid: _Client() for aid in accounts}
    ad._profile_home = None
    ad._send_allowlist = ()
    ad._send_allowlists = {}
    return ad


def test_refresh_outbound_allowlist_injects_each_accounts_own_list(monkeypatch, stubbed):
    """A 账号的 allowFrom 不能替 B 账号开门（以前全局并集注入每个 client）。"""
    accounts = {"a": _Acct(allow=("a1",)), "b": _Acct(allow=("b1",))}
    ad = _bare_adapter(accounts)
    monkeypatch.setattr(
        netguard, "unified_allowlist",
        lambda **kw: tuple(f"u:{a.id}" for a in kw["accounts"]))

    union = ad.refresh_outbound_allowlist()

    assert ad._send_allowlists == {"a": ("u:a",), "b": ("u:b",)}
    assert ad._clients["a"].send_allowlist == ("u:a",)
    assert ad._clients["b"].send_allowlist == ("u:b",)
    assert union == ("u:a", "u:b"), "返回值保留并集（日志/兼容调用方）"


def test_allowlist_for_never_falls_back_to_the_global_union(monkeypatch, stubbed):
    """缓存缺失时按本账号现算；拿全局并集兜底等于把隔离又拆了。"""
    accounts = {"a": _Acct(allow=("a1",)), "b": _Acct(allow=("b1",))}
    ad = _bare_adapter(accounts)
    ad._send_allowlist = ("a1", "b1")
    seen = []

    def _fake(**kw):
        seen.extend(a.id for a in kw["accounts"])
        return tuple(f"u:{a.id}" for a in kw["accounts"])

    monkeypatch.setattr(netguard, "unified_allowlist", _fake)
    assert ad._allowlist_for(accounts["b"]) == ("u:b",)
    assert seen == ["b"], "只允许按该账号现算"
