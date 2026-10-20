"""SQLite 后端回归（2026-10-20）—— ``python3 plugin/...`` 默认就能跑，不再依赖 MariaDB。

## 守的四件事

1. **默认后端是 SQLite**，MySQL 只是可选配置；拼错的后端名直接报错（不静默回落）。
2. ``schema.sql`` 是**唯一 DDL 真源**：SQLite 侧靠 :mod:`plugin.data.sqlite_dialect`
   做确定性翻译（类型、``%s``、``ON DUPLICATE KEY``、时间函数、索引全局重名）。
3. migration **幂等**：第一次建 20 张表 + 索引，第二次零新建、零补齐。
4. 真实 repo 语句在 SQLite 上能跑：消息 upsert、时间聚合、风格卡回滚、心流台账
   ``INSERT IGNORE``。

测试只碰 ``tmp_path`` 下的 SQLite 文件，**不连任何网络/生产库**。
"""

from __future__ import annotations

import sqlite3

import pytest

from plugin.data import db
from plugin.data import repo_hf, repo_messages, repo_style
from plugin.data.sqlite_dialect import schema_to_sqlite, translate_sql


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    """把 ``db`` 指到临时 SQLite，并跑一次 migration。"""
    db.close()
    path = tmp_path / "wpp.sqlite3"
    monkeypatch.setenv("WECHATPRO_DB_BACKEND", "sqlite")
    monkeypatch.setenv("WECHATPRO_SQLITE_PATH", str(path))
    monkeypatch.delenv("WECHATPRO_DB_HOST", raising=False)
    db.migrate()
    yield path
    db.close()


def _query(path, sql, params=()):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ============================================================ 1. 后端选择

def test_sqlite_is_the_default_backend(monkeypatch):
    monkeypatch.delenv("WECHATPRO_DB_BACKEND", raising=False)
    assert db.backend() == "sqlite"
    cfg = db.config()
    assert cfg["backend"] == "sqlite"
    assert cfg["path"].endswith("wpp.sqlite3")


def test_backend_aliases_and_typos(monkeypatch):
    monkeypatch.setenv("WECHATPRO_DB_BACKEND", "mariadb")
    assert db.backend() == "mysql"
    monkeypatch.setenv("WECHATPRO_DB_BACKEND", "sqlite3")
    assert db.backend() == "sqlite"
    monkeypatch.setenv("WECHATPRO_DB_BACKEND", "postgres")
    with pytest.raises(db.DbConfigError, match="不认识"):
        db.backend()


def test_sqlite_does_not_require_a_local_host_check(monkeypatch):
    """SQLite 没有 host，远程库红线不该误伤它。"""
    monkeypatch.setenv("WECHATPRO_DB_BACKEND", "sqlite")
    db.assert_host_allowed(db.config())  # 不抛


# ============================================================ 2. 方言翻译

def test_translate_sql_rewrites_mysql_isms():
    sql = translate_sql(
        "INSERT IGNORE INTO t (a) VALUES (%s) "
        "ON DUPLICATE KEY UPDATE a = VALUES(a) "
        "WHERE ts >= FROM_UNIXTIME(%s) AND UNIX_TIMESTAMP(ts) > 0 "
        "AND CHAR_LENGTH(a) > 1 %% 1 = 0 FOR UPDATE")
    assert "INSERT OR IGNORE INTO" in sql
    # 注意 ``strftime('%s', …)`` 里的 ``%s`` 是 SQLite 的格式串，不是参数占位符。
    assert "VALUES (?)" in sql and "FROM_UNIXTIME" not in sql
    assert "ON CONFLICT DO UPDATE SET" in sql
    assert "excluded.a" in sql
    assert "datetime(?, 'unixepoch')" in sql
    assert "CAST(strftime('%s', ts) AS INTEGER)" in sql
    assert "length(a)" in sql
    assert "FOR UPDATE" not in sql


def test_schema_conversion_does_not_corrupt_words_containing_int():
    """历史 bug：``\\bINT\\b`` 少了词边界会把 ``endpoint`` 改成 ``endpoINTEGER``。"""
    out = "\n".join(schema_to_sqlite(
        "CREATE TABLE t (endpoint TEXT, print_count INT, id BIGINT UNSIGNED);"))
    assert "endpoint" in out and "print_count" in out
    assert "endpoINTEGER" not in out
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in out or "id INTEGER" in out


def test_schema_index_names_are_table_scoped():
    """SQLite 索引名是**全库唯一**：两张表都有 ``idx_account_ts`` 时必须加表前缀。"""
    out = "\n".join(schema_to_sqlite(
        "CREATE TABLE a (x INT, INDEX idx_account_ts (x));"
        "CREATE TABLE b (x INT, INDEX idx_account_ts (x));"))
    assert "a__idx_account_ts" in out
    assert "b__idx_account_ts" in out


# ============================================================ 3. migration

def test_migration_creates_the_whole_schema_and_is_idempotent(sqlite_env):
    tables = {r["name"] for r in _query(
        sqlite_env, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert len({t for t in tables if t.startswith("wpp_")}) >= 20
    assert "wpp_messages" in tables and "wpp_hf_ledger" in tables
    indexes = {r["name"] for r in _query(
        sqlite_env, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "wpp_messages__idx_account_ts" in indexes

    again = db.migrate()
    assert again["created_tables"] == []
    assert again["added_columns"] == [] and again["added_indexes"] == []
    assert again["applied_migrations"] == []


# ============================================================ 4. 真实 repo 语句

def test_message_upsert_and_time_bucket_queries(sqlite_env):
    ts = 1_700_000_000
    repo_messages.save_message(
        "acct", direction="inbound", peer_kind="group",
        peer_id="g@chatroom", chat_id="g@chatroom", msg_id="m1",
        content="hello", ts=ts)
    # 幂等键相同 ⇒ 更新内容，不新增行、不改 ts
    repo_messages.save_message(
        "acct", direction="inbound", peer_kind="group",
        peer_id="g@chatroom", chat_id="g@chatroom", msg_id="m1",
        content="hello v2", ts=ts + 999)
    rows = repo_messages.list_messages_in_window("acct", ts - 10)
    assert [r["content"] for r in rows] == ["hello v2"]

    buckets = repo_messages.list_group_msg_hour_buckets("acct", ts - 10, 0)
    assert buckets and buckets[0]["n"] == 1


def test_contact_and_chatroom_upserts(sqlite_env):
    repo_messages.upsert_contact("acct", "wxid_a", nickname="A")
    repo_messages.upsert_contact("acct", "wxid_a", nickname="A2")
    assert repo_messages.get_contact("acct", "wxid_a")["nickname"] == "A2"

    repo_messages.upsert_chatroom("acct", "g@chatroom", nickname="G", member_count=3)
    repo_messages.upsert_chatroom("acct", "g@chatroom", nickname="G2", owner_wxid="wxid_o")
    room = repo_messages.get_chatroom("acct", "g@chatroom")
    assert room["nickname"] == "G2" and room["owner_wxid"] == "wxid_o"


def test_style_card_versions_and_rollback(sqlite_env):
    v1 = repo_style.save_card("acct", "g@chatroom", {"tone": "a"}, confidence=0.5)
    v2 = repo_style.save_card("acct", "g@chatroom", {"tone": "b"}, confidence=0.6)
    assert (v1, v2) == (1, 2)

    v3 = repo_style.rollback_card("acct", "g@chatroom", v1)
    assert v3 == 3
    card = repo_style.get_card("acct", "g@chatroom")
    assert card["version"] == 3
    assert '"a"' in card["style_json"]
    assert repo_style.rollback_card("acct", "g@chatroom", 99) is None


def test_hf_ledger_insert_ignore(sqlite_env):
    ts = 1_700_000_100
    repo_hf.insert_judged("acct", "m1", "g@chatroom", ts, judge_overall=0.7)
    repo_hf.insert_judged("acct", "m1", "g@chatroom", ts + 1, judge_overall=0.9)
    rows = _query(sqlite_env,
                  "SELECT judge_overall FROM wpp_hf_ledger "
                  "WHERE account_id=? AND inbound_msg_id=?", ("acct", "m1"))
    assert len(rows) == 1 and rows[0]["judge_overall"] == 0.7
    assert repo_hf.get_ledger_last("acct", "g@chatroom")["inbound_msg_id"] == "m1"
