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
