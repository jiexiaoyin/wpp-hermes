"""引用回复 ``@被回复人`` 前缀的昵称来源优先级。

2026-10-20 华为群实测：员工 ``chenli5211013`` 在 ``wpp_chatroom_members`` 里
明明有昵称「何必」，但 ``@`` 出去的还是 ``@chenli5211013`` —— 因为
``_quote_prefix`` 的五级兜底**漏了群成员表**，一路退到 wxid。

钉三件事：

1. **群成员表优先于通讯录**：同群群昵称比通讯录备注更贴当下；
2. **成员表查不到才退通讯录**；
3. **全查不到才用 wxid 兜底**（至少还能 @ 到人，不至于丢前缀）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plugin.platform.adapter import WppAdapter

_GROUP = "19908568237@chatroom"
_ACCT = SimpleNamespace(id="default")


def _adapter():
    return object.__new__(WppAdapter)


@pytest.mark.asyncio
async def test_group_member_nickname_wins():
    """**核心回归**：成员表有名字就必须用它，不能退到通讯录或 wxid。"""
    adapter = _adapter()
    with patch.object(WppAdapter, "_group_member_name", return_value="何必"), \
            patch.object(WppAdapter, "_contact_name", return_value="通讯录备注") as contact:
        prefix = await adapter._quote_prefix(
            _ACCT, None, _GROUP, "chenli5211013", {})

    assert prefix == "@何必 "
    contact.assert_not_called()


@pytest.mark.asyncio
async def test_falls_back_to_contact_when_member_missing():
    adapter = _adapter()
    with patch.object(WppAdapter, "_group_member_name", return_value=""), \
            patch.object(WppAdapter, "_contact_name", return_value="通讯录备注"):
        prefix = await adapter._quote_prefix(
            _ACCT, None, _GROUP, "wxid_unknown", {})

    assert prefix == "@通讯录备注 "


@pytest.mark.asyncio
async def test_vendor_lookup_before_wxid_fallback():
    adapter = _adapter()
    with patch.object(WppAdapter, "_group_member_name", return_value=""), \
            patch.object(WppAdapter, "_contact_name", return_value=""), \
            patch.object(WppAdapter, "_resolve_display_name",
                         return_value="厂商查到的名字"):
        prefix = await adapter._quote_prefix(
            _ACCT, None, _GROUP, "wxid_q", {})

    assert prefix == "@厂商查到的名字 "


@pytest.mark.asyncio
async def test_wxid_is_the_last_resort():
    """全查不到时 @wxid —— 至少人还能被 @ 到，前缀不该整个消失。"""
    adapter = _adapter()
    with patch.object(WppAdapter, "_group_member_name", return_value=""), \
            patch.object(WppAdapter, "_contact_name", return_value=""), \
            patch.object(WppAdapter, "_resolve_display_name", return_value=""):
        prefix = await adapter._quote_prefix(
            _ACCT, None, _GROUP, "wxid_q", {})

    assert prefix == "@wxid_q "


@pytest.mark.asyncio
async def test_metadata_display_name_beats_everything():
    """微信引用消息自带的名字最准（连群友都对）—— 有就用它。"""
    adapter = _adapter()
    with patch.object(WppAdapter, "_group_member_name", return_value="群昵称") as member:
        prefix = await adapter._quote_prefix(
            _ACCT, None, _GROUP, "wxid_q",
            {"quote_display_name": "微信自带名"})

    assert prefix == "@微信自带名 "
    member.assert_not_called()


@pytest.mark.asyncio
async def test_direct_message_never_gets_a_prefix():
    """私聊不 @ —— 这是 ``build_mention_prefix`` 的语义，这里只确认没被绕过。"""
    adapter = _adapter()
    prefix = await adapter._quote_prefix(
        _ACCT, None, "wxid_other", "wxid_other", {})
    assert prefix == ""
