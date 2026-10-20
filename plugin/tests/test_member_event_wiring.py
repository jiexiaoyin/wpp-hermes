"""``WppAdapter._maybe_member_event`` 的接线契约。

这个方法挂在 ``_handle_one`` 的 ③d（④ 触发判定**之前**），因为系统通知不满足
任何触发路，会被 ④ fail-closed 丢掉 —— 那段路径上没有任何东西会去更新成员表。

钉三件事：

1. 进群/退群 ⇒ **只拉该群**（`["1@chatroom"]`），不是扫全部群；
2. 机器人自己被拉进群（`group_invite`）⇒ 拉**全部**群（新群还没有 id）；
3. 任何失败都不许冒泡 —— 记账坏了不能挡住这条消息的后续处理。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from plugin.platform.adapter import WppAdapter


def _adapter_with_client(client):
    """只构造 `_maybe_member_event` 用到的两个属性，避开整套 adapter 初始化。"""
    adapter = object.__new__(WppAdapter)
    adapter._clients = {"default": client}
    return adapter


def _msg(content, msg_type="10000", chatroom_id="1@chatroom"):
    return SimpleNamespace(content=content, msg_type=msg_type,
                           chatroom_id=chatroom_id, peer_id=chatroom_id)


_ACCT = SimpleNamespace(id="default")


@pytest.mark.asyncio
async def test_join_event_syncs_only_that_group():
    client = AsyncMock()
    adapter = _adapter_with_client(client)

    with patch("plugin.inbound.name_sync.sync_members",
               AsyncMock(return_value=3)) as m:
        await adapter._maybe_member_event(
            _ACCT, _msg('"张三"邀请"李四"加入了群聊'))

    m.assert_awaited_once_with(client, "default", ["1@chatroom"])


@pytest.mark.asyncio
async def test_leave_event_syncs_only_that_group():
    client = AsyncMock()
    adapter = _adapter_with_client(client)

    with patch("plugin.inbound.name_sync.sync_members",
               AsyncMock(return_value=0)) as m:
        await adapter._maybe_member_event(_ACCT, _msg("李四退出了群聊"))

    m.assert_awaited_once_with(client, "default", ["1@chatroom"])


@pytest.mark.asyncio
async def test_self_join_syncs_all_groups():
    """机器人被拉进群时新群还没有 id ⇒ 只能拉全部群（`None` = 全量）。"""
    client = AsyncMock()
    adapter = _adapter_with_client(client)

    with patch("plugin.inbound.name_sync.sync_members",
               AsyncMock(return_value=40)) as m:
        await adapter._maybe_member_event(
            _ACCT, _msg("[群聊邀请] 周州 邀请你加入群聊「某群」",
                        msg_type="group_invite"))

    m.assert_awaited_once_with(client, "default", None)


@pytest.mark.asyncio
async def test_ordinary_message_does_not_sync():
    client = AsyncMock()
    adapter = _adapter_with_client(client)

    with patch("plugin.inbound.name_sync.sync_members",
               AsyncMock(return_value=0)) as m:
        await adapter._maybe_member_event(
            _ACCT, _msg("办理业务：福袋", msg_type="1"))

    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_failure_never_propagates():
    """记账失败只记 warning —— 不能让这条消息的处理链挂掉。"""
    client = AsyncMock()
    adapter = _adapter_with_client(client)

    with patch("plugin.inbound.name_sync.sync_members",
               AsyncMock(side_effect=RuntimeError("vendor down"))):
        await adapter._maybe_member_event(_ACCT, _msg('"张三"邀请"李四"加入了群聊'))


@pytest.mark.asyncio
async def test_missing_client_is_skipped():
    adapter = object.__new__(WppAdapter)
    adapter._clients = {}

    with patch("plugin.inbound.name_sync.sync_members",
               AsyncMock(return_value=0)) as m:
        await adapter._maybe_member_event(_ACCT, _msg('"李四"加入了群聊'))

    m.assert_not_awaited()
