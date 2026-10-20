"""群名同步（``inbound/name_sync.py``）的契约。

这个模块原来覆盖率 **0%**（54 条语句一条都没跑过），但它做的是"把厂商群列表
写进 ``wpp_chatrooms``"——写错了会直接污染 WebUI 的会话标题，而且**失败是静默的**
（只记 debug/warning，不影响收发消息）。所以这里钉四件事：

1. **三种返回形状都要认**（``group_details`` / ``items`` / ``GroupList``）——
   厂商换一版字段名，群名就集体停更，而日志只有一行 warning；
2. **``Code != 0`` 必须抛**，不能被当成"空列表"静默吞掉（否则永远 0 个群）；
3. **单个群写库失败不拖累整批**（事务粒度是行，不是批）；
4. **``CancelledError`` 必须原样抛出**——被 ``except Exception`` 吃掉的话，
   连接关闭时这个任务永远退不出去。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugin.inbound import name_sync

# ---------------------------------------------------------------- _group_rows

@pytest.mark.parametrize("key", ["group_details", "items", "GroupList", "groups"])
def test_group_rows_accepts_every_known_shape(key):
    payload = {"Data": {key: [{"id": "1@chatroom", "name": "群一"}]}}
    rows = name_sync._group_rows(payload)
    assert len(rows) == 1
    assert rows[0]["id"] == "1@chatroom"


def test_group_rows_tolerates_lowercase_data():
    payload = {"data": {"items": [{"id": "2@chatroom"}]}}
    assert name_sync._group_rows(payload) == [{"id": "2@chatroom"}]


@pytest.mark.parametrize("payload", [
    {},
    {"Data": None},
    {"Data": []},                       # data 不是 dict
    {"Data": {"group_details": []}},    # 空列表
    {"Data": {"items": ["not-a-dict"]}},
    {"Data": {"unknown_key": [{"id": "1@chatroom"}]}},
])
def test_group_rows_returns_empty_instead_of_raising(payload):
    """厂商换字段 / 返回异常形状时，同步应该安静返回 0，不是把任务炸掉。"""
    assert name_sync._group_rows(payload) == []


# --------------------------------------------------------- apply_group_rows

def test_apply_group_rows_skips_rows_without_an_id():
    repo = MagicMock()
    with patch("plugin.data.repo_messages.upsert_chatroom", repo.upsert_chatroom):
        written = name_sync.apply_group_rows("default", [{"name": "无 id 的群"}])
    assert written == 0
    repo.upsert_chatroom.assert_not_called()


def test_apply_group_rows_falls_back_to_display_name():
    """``name`` 为空时用 ``display_name``（通常形如「群名(人数)」）。"""
    repo = MagicMock()
    with patch("plugin.data.repo_messages.upsert_chatroom", repo.upsert_chatroom):
        name_sync.apply_group_rows("default", [
            {"id": "1@chatroom", "name": "", "display_name": "技术群(12)"},
        ])
    kwargs = repo.upsert_chatroom.call_args.kwargs
    assert kwargs["nickname"] == "技术群(12)"


def test_apply_group_rows_keeps_writing_after_one_row_fails():
    """单群写库失败只记 debug，不能中断整批 —— 否则一个坏群名让其余全停更。"""
    ok, bad = MagicMock(), MagicMock(side_effect=RuntimeError("boom"))
    calls = {"n": 0}

    def _upsert(account_id, chatroom_id, **kw):
        calls["n"] += 1
        return bad(account_id, chatroom_id) if chatroom_id == "bad@chatroom" else ok()

    with patch("plugin.data.repo_messages.upsert_chatroom", _upsert):
        written = name_sync.apply_group_rows("default", [
            {"id": "bad@chatroom", "name": "坏"},
            {"id": "good@chatroom", "name": "好"},
        ])
    assert calls["n"] == 2, "第二个群必须仍然被尝试"
    assert written == 1, "只有成功的那一行计数"


def test_apply_group_rows_passes_owner_and_member_count():
    repo = MagicMock()
    with patch("plugin.data.repo_messages.upsert_chatroom", repo.upsert_chatroom):
        name_sync.apply_group_rows("default", [
            {"id": "1@chatroom", "name": "群", "owner_id": "wxid_owner", "member_count": 42},
        ])
    kwargs = repo.upsert_chatroom.call_args.kwargs
    assert kwargs["owner_wxid"] == "wxid_owner"
    assert kwargs["member_count"] == 42


# --------------------------------------------------------------- sync_once

@pytest.mark.asyncio
async def test_sync_once_raises_on_nonzero_code():
    """``Code != 0`` 不能当成空列表 —— 否则群名永远停在旧快照且无告警。"""
    client = MagicMock()
    client.call = AsyncMock(return_value={"Code": 40001, "Message": "auth failed"})

    with pytest.raises(RuntimeError, match="Code=40001"):
        await name_sync.sync_once(client, "default")


@pytest.mark.asyncio
async def test_sync_once_returns_zero_when_the_list_is_empty():
    client = MagicMock()
    client.call = AsyncMock(return_value={"Code": 0, "Data": {"group_details": []}})

    assert await name_sync.sync_once(client, "default") == 0


@pytest.mark.asyncio
async def test_sync_once_clears_the_title_cache_after_writing():
    """同步和标题解析同进程 —— 写完必须清缓存，否则新群名要等 5 分钟 TTL。"""
    client = MagicMock()
    client.call = AsyncMock(return_value={
        "Code": 0, "Data": {"group_details": [{"id": "1@chatroom", "name": "群"}]}})

    with patch("plugin.inbound.name_sync.apply_group_rows", return_value=1), \
         patch("plugin.inbound.session_title.clear_name_cache") as clear:
        written = await name_sync.sync_once(client, "default")

    assert written == 1
    clear.assert_called_once()


@pytest.mark.asyncio
async def test_sync_once_does_not_touch_the_cache_when_nothing_was_written():
    client = MagicMock()
    client.call = AsyncMock(return_value={
        "Code": 0, "Data": {"group_details": [{"id": "1@chatroom"}]}})

    with patch("plugin.inbound.name_sync.apply_group_rows", return_value=0), \
         patch("plugin.inbound.session_title.clear_name_cache") as clear:
        await name_sync.sync_once(client, "default")

    clear.assert_not_called()


# ------------------------------------------------------------- sync_forever

@pytest.mark.asyncio
async def test_sync_forever_propagates_cancellation():
    """被 ``except Exception`` 吃掉的话，连接关闭时这个任务永远退不出去。"""
    client = MagicMock()

    async def _boom(*_a, **_k):
        raise asyncio.CancelledError

    with patch("plugin.inbound.name_sync.sync_once", _boom):
        with pytest.raises(asyncio.CancelledError):
            await name_sync.sync_forever(client, "default", interval_sec=0)


@pytest.mark.asyncio
async def test_sync_forever_survives_a_failed_round():
    """同步失败只 warn，不能退出循环 —— 下一轮还要重试。"""
    client = MagicMock()
    calls = {"n": 0}

    async def _fail_then_cancel(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("vendor down")
        raise asyncio.CancelledError

    with patch("plugin.inbound.name_sync.sync_once", _fail_then_cancel):
        with patch("plugin.inbound.name_sync.sync_members", AsyncMock(return_value=0)):
            with pytest.raises(asyncio.CancelledError):
                await name_sync.sync_forever(client, "default", interval_sec=0)

    assert calls["n"] == 2, "第一轮失败后必须继续跑第二轮"


# ------------------------------------------------------------- sync_members

def test_apply_member_rows_writes_each_member():
    """单个成员写库失败不能拖累整批（事务粒度是行）。"""
    from plugin.data import repo_messages

    calls = []

    def _upsert(account_id, chatroom_id, wxid, **kw):
        calls.append((account_id, chatroom_id, wxid, kw.get("nickname")))
        if wxid == "wxid_bad":
            raise RuntimeError("db down")
        return 1

    with patch.object(repo_messages, "upsert_chatroom_member", _upsert), \
            patch.object(repo_messages, "list_chatroom_members", return_value=[]), \
            patch.object(repo_messages, "prune_chatroom_members", return_value=[]):
        res = name_sync.apply_member_rows(
            "default", "1@chatroom",
            [("wxid_a", "张三"), ("wxid_bad", "坏"), ("wxid_b", "李四")])

    assert res["written"] == 2, "失败那条跳过，其余照写"
    assert ("default", "1@chatroom", "wxid_a", "张三") in calls
    assert ("default", "1@chatroom", "wxid_b", "李四") in calls
    assert {w for w, _ in res["added"]} == {"wxid_a", "wxid_b", "wxid_bad"}, \
        "本地表是空的 ⇒ 三个都算新增（written 才是写入成功的计数）"


def test_apply_member_rows_reports_added_and_removed():
    """**核心**：人数不变也要能说出"谁进谁出"（2026-10-20 16:40 实测一进一出）。"""
    from plugin.data import repo_messages

    with patch.object(repo_messages, "upsert_chatroom_member", return_value=1), \
            patch.object(repo_messages, "list_chatroom_members",
                         return_value=[{"wxid": "wxid_old"}, {"wxid": "wxid_keep"}]), \
            patch.object(repo_messages, "prune_chatroom_members",
                         return_value=[("wxid_old", "旧成员")]):
        res = name_sync.apply_member_rows(
            "default", "1@chatroom", [("wxid_keep", "留"), ("wxid_new", "新来的")])

    assert res["added"] == [("wxid_new", "新来的")]
    assert res["removed"] == [("wxid_old", "旧成员")]


@pytest.mark.asyncio
async def test_sync_members_upserts_every_group():
    """群列表 → 逐群拉成员 → 全部入库。"""
    client = MagicMock()

    async def _call(path, params=None, **_kw):
        if path == "/Group/List":
            return {"Code": 0, "Data": {"group_details": [
                {"id": "1@chatroom"}, {"id": "2@chatroom"}]}}
        return {"Code": 0, "Data": {"NewChatroomData": {"ChatRoomMember": [
            {"UserName": "wxid_x", "NickName": "甲"}]}}}

    client.call = _call
    with patch("plugin.inbound.name_sync.apply_member_rows",
                  return_value={"written": 1, "added": [], "removed": []}) as m:
        with patch("plugin.inbound.session_title.clear_name_cache") as clr:
            res = await name_sync.sync_members(client, "default")

    assert res["written"] == 2, "两个群各写一条"
    assert res["groups"] == 2
    assert [c.args[1] for c in m.call_args_list] == ["1@chatroom", "2@chatroom"]
    clr.assert_called_once(), "刷新后必须清名字缓存"


@pytest.mark.asyncio
async def test_sync_members_skips_a_failing_group():
    """一个群拉不到，不该让整轮昵称刷新归零。"""
    client = MagicMock()

    async def _call(path, params=None, **_kw):
        if path == "/Group/List":
            return {"Code": 0, "Data": {"group_details": [
                {"id": "1@chatroom"}, {"id": "2@chatroom"}]}}
        if params["QID"] == "1@chatroom":
            return {"Code": -8}
        return {"Code": 0, "Data": {"NewChatroomData": {"ChatRoomMember": [
            {"UserName": "wxid_y", "NickName": "乙"}]}}}

    client.call = _call
    with patch("plugin.inbound.name_sync.apply_member_rows",
                  return_value={"written": 1, "added": [], "removed": []}) as m:
        with patch("plugin.inbound.session_title.clear_name_cache"):
            res = await name_sync.sync_members(client, "default")

    assert res["written"] == 1
    assert m.call_args.args[1] == "2@chatroom"


@pytest.mark.asyncio
async def test_sync_members_raises_on_group_list_error():
    """``/Group/List`` 的 Code 非 0 必须抛 —— 否则永远 0 条且无人知晓。"""
    client = MagicMock()
    client.call = AsyncMock(return_value={"Code": -1})

    with pytest.raises(RuntimeError):
        await name_sync.sync_members(client, "default")


@pytest.mark.asyncio
async def test_sync_forever_does_not_touch_members():
    """成员昵称是**事件驱动**的（老板 2026-10-20 拍板）—— 群名轮询里不许顺手扫全群。"""
    client = MagicMock()
    rounds = {"n": 0}

    async def _once(*_a, **_k):
        rounds["n"] += 1
        if rounds["n"] > 1:
            raise asyncio.CancelledError

    with patch("plugin.inbound.name_sync.sync_once", _once):
        with patch("plugin.inbound.name_sync.sync_members",
                   AsyncMock(return_value=3)) as mm:
            with pytest.raises(asyncio.CancelledError):
                await name_sync.sync_forever(client, "default", interval_sec=0)

    mm.assert_not_called()

