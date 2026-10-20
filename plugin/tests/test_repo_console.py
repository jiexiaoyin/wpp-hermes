"""控制台只读查询（``plugin/data/repo_console.py``）—— 用 fake conn，不连库。

重点不是 SQL 语法本身（那是 ``test_sqlite_backend`` 的事），而是：
WHERE 参数是否按过滤条件拼对、COUNT 与列表是否用同一套条件、分页/桶
是否会出现"少一条/多一桶"的差错。
"""
from __future__ import annotations

import time

import pytest

from plugin.data import repo_console


class _FakeConn:
    def __init__(self, rows=None, one=None):
        self.rows = list(rows or [])
        self.one = one
        self.calls: list[tuple[str, tuple]] = []

    def query(self, sql, params=()):
        self.calls.append((sql, params))
        return self.rows.pop(0) if self.rows and isinstance(self.rows[0], list) else self.rows

    def query_one(self, sql, params=()):
        self.calls.append((sql, params))
        return self.one


@pytest.fixture
def fake_db(monkeypatch):
    holder: dict[str, _FakeConn] = {}

    def _install(conn):
        holder["conn"] = conn
        monkeypatch.setattr(repo_console.db, "get_conn", lambda: conn)
        monkeypatch.setattr(repo_console.db, "close", lambda: None)
        return conn

    return _install


def test_member_search_uses_same_where_for_count_and_page(fake_db):
    conn = fake_db(_FakeConn(rows=[[{"wxid": "wxid_a", "nickname": "A", "is_owner": 1}]],
                             one={"n": 3}))
    res = repo_console.list_group_members_page(
        "default", "g1@chatroom", q="A", limit=1, offset=2)
    assert res["total"] == 3 and res["has_more"] is False
    assert [m["wxid"] for m in res["members"]] == ["wxid_a"]
    count_sql, count_params = conn.calls[0]
    list_sql, list_params = conn.calls[1]
    assert "COUNT(*)" in count_sql
    assert "LIKE %s" in count_sql and "LIKE %s" in list_sql
    assert count_params == ("default", "g1@chatroom", "%A%", "%A%")
    assert list_params[:4] == count_params
    assert list_params[-2:] == (1, 2)


def test_search_messages_filters_direction_group_sender_keyword(fake_db):
    conn = fake_db(_FakeConn(
        rows=[[{"id": 7, "direction": "inbound", "peer_kind": "group",
                "peer_id": "g1@chatroom", "chat_id": "g1@chatroom",
                "peer_name": "群一", "from_wxid": "wxid_a", "msg_type": "text",
                "content": "hello", "at_sec": 100}]],
        one={"n": 1}))
    res = repo_console.search_messages_page(
        "default", q="hel", group_id="g1@chatroom", sender="wxid_a",
        direction="inbound", since_hours=24, limit=10, offset=0)
    assert res["total"] == 1 and res["has_more"] is False
    assert res["messages"][0]["content"] == "hello"
    count_sql, count_params = conn.calls[0]
    list_sql, _ = conn.calls[1]
    assert "direction = %s" in count_sql
    assert "COALESCE(NULLIF(chat_id, ''), peer_id) = %s" in count_sql
    assert "from_wxid LIKE %s" in count_sql
    assert "content LIKE %s" in count_sql
    assert count_params[2:5] == ("g1@chatroom", "%hel%", "%wxid_a%")
    assert "COUNT(*)" not in list_sql
    assert "SUBSTR" in list_sql


def test_message_analytics_zero_fills_buckets_and_types(fake_db):
    now = 1_700_000_000
    off = repo_console._local_offset_sec(now)
    bucket = (now + off) - (now + off) % 3600
    fake_db(_FakeConn(
        rows=[[{"bucket": bucket, "direction": "inbound", "n": 2}],
              [{"msg_type": "text", "direction": "inbound", "n": 2}]],
        one={"last_at": now}))
    res = repo_console.message_analytics("default", hours=24, now=now)
    assert res["bucket_sec"] == 3600
    assert len(res["buckets"]) == 24
    assert res["totals"] == {"inbound": 2, "outbound": 0}
    assert res["types"] == [{"msg_type": "text", "direction": "inbound", "count": 2}]
    assert res["last_message_at"] == now


def test_group_detail_merges_direction_counts(fake_db):
    fake_db(_FakeConn(
        rows=[[{"direction": "inbound", "n": 4}, {"direction": "outbound", "n": 1}]],
        one={"chatroom_id": "g1@chatroom", "nickname": "群一", "remark": "",
             "owner_wxid": "wxid_o", "member_count": 10,
             "last_synced_at": "2026-10-20 10:00:00"}))
    # get_group_detail 的 query_one 有多处（room/local/last），这里只断言合并结果形状。
    res = repo_console.get_group_detail("default", "g1@chatroom", since_hours=24)
    assert res["db_available"] is True
    assert res["group"]["inbound"] == 4
    assert res["group"]["outbound"] == 1
    assert res["group"]["nickname"] == "群一"


def test_list_groups_page_search_sort_and_pagination(monkeypatch):
    monkeypatch.setattr(repo_console, "_group_dataset", lambda *a, **k: {
        "db_available": True, "since_hours": 24,
        "totals": {"inbound": 3, "outbound": 1},
        "groups": [
            {"chatroom_id": "g1@chatroom", "name": "Alpha", "member_count": 5,
             "local_members": 4, "inbound": 1, "outbound": 0, "last_synced_at": None},
            {"chatroom_id": "g2@chatroom", "name": "Beta", "member_count": 50,
             "local_members": 40, "inbound": 0, "outbound": 1, "last_synced_at": None},
            {"chatroom_id": "g3@chatroom", "name": "Alpha Two", "member_count": 9,
             "local_members": 8, "inbound": 2, "outbound": 0, "last_synced_at": None},
        ]})
    by_name = repo_console.list_groups_page(
        "default", q="alpha", sort="name", limit=1, offset=0)
    assert by_name["total"] == 2
    assert [g["chatroom_id"] for g in by_name["groups"]] == ["g1@chatroom"]
    assert by_name["has_more"] is True
    page2 = repo_console.list_groups_page(
        "default", q="alpha", sort="name", limit=1, offset=1)
    assert [g["chatroom_id"] for g in page2["groups"]] == ["g3@chatroom"]
    by_members = repo_console.list_groups_page("default", sort="members", limit=3)
    assert [g["chatroom_id"] for g in by_members["groups"]] == [
        "g2@chatroom", "g3@chatroom", "g1@chatroom"]
    by_activity = repo_console.list_groups_page("default", sort="activity", limit=3)
    assert by_activity["groups"][0]["chatroom_id"] == "g3@chatroom"


def test_heartflow_overview_combines_sources(monkeypatch, fake_db):
    fake_db(_FakeConn())
    monkeypatch.setattr(repo_console.repo_hf, "count_by_status",
                        lambda aid, since, conn=None: {"total": 2, "by_status": {}})
    monkeypatch.setattr(repo_console.repo_hf, "list_sent_counts_recent",
                        lambda aid, h, d, conn=None: [{"group_id": "g1@chatroom"}])
    monkeypatch.setattr(repo_console.repo_hf, "list_group_states",
                        lambda aid, conn=None: [{"group_id": "g1@chatroom"}])
    monkeypatch.setattr(repo_console.repo_hf, "list_recent_judged",
                        lambda aid, n, conn=None: [{"group_id": "g1@chatroom"}])
    monkeypatch.setattr(repo_console.repo_hf, "get_last_sent_at",
                        lambda aid, conn=None: 123)
    res = repo_console.heartflow_overview("default", hours=24, now=time.time())
    assert res["stats"]["total"] == 2
    assert res["last_sent_at"] == 123
    assert res["budget"] and res["group_states"] and res["recent"]
