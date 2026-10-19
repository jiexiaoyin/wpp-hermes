"""``data/repo_style.py`` 的 SQL 形状与版本语义（2026-10-19）。

这里**不碰真库**：用最小假连接记录 SQL/参数，钉住三件容易"看着对、实际错"的事：

1. **版本只增不减** —— 回滚是把旧内容复制成新版本，不是把版本号改回去；
2. **主表与历史同事务** —— 两条 ``execute`` 必须包在 ``transaction()`` 里；
3. **坏 JSON 不在 SQL 层解析** —— 批量预热要能如实看到"这张卡坏了"。
"""

from __future__ import annotations

import json
from contextlib import contextmanager

from plugin.data import repo_style as R


class FakeConn:
    """只实现 repo_style 用到的那几个口；``current_version`` 模拟已有主表行。"""

    def __init__(self, *, current_version: int | None = None,
                 version_row: dict | None = None,
                 cards: list[dict] | None = None) -> None:
        self.current_version = current_version
        self.version_row = version_row
        self.cards = cards or []
        self.executed: list[tuple[str, tuple]] = []
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield self

    def query(self, sql: str, params=()):
        if "FROM wpp_style_cards" in sql:
            return list(self.cards)
        raise AssertionError(f"未预期的 query: {sql}")

    def query_one(self, sql: str, params=()):
        if "FROM wpp_style_card_versions" in sql:
            return self.version_row
        if "FROM wpp_style_cards" in sql and "FOR UPDATE" in sql:
            if self.current_version is None:
                return None
            return {"version": self.current_version}
        raise AssertionError(f"未预期的 query_one: {sql}")

    def query_value(self, sql: str, params=(), default=None):
        if "COUNT(*)" in sql:
            return len(self.cards)
        raise AssertionError(f"未预期的 query_value: {sql}")

    def execute(self, sql: str, params=()):
        self.executed.append((" ".join(sql.split()), tuple(params)))
        return 1


# ============================================================ save_card


def test_first_save_starts_at_v1_and_writes_both_tables() -> None:
    conn = FakeConn()
    version = R.save_card(
        "a1", "g1", {"tone": "轻松"}, sample_msgs=100, sender_count=4,
        confidence=0.9, model="m", generated_at=123, conn=conn)

    assert version == 1
    assert conn.transactions == 1
    assert len(conn.executed) == 2

    main_sql, main_params = conn.executed[0]
    assert "INSERT INTO wpp_style_cards" in main_sql
    assert json.loads(main_params[2]) == {"tone": "轻松"}
    assert main_params[7] == 1                     # version

    hist_sql, hist_params = conn.executed[1]
    assert "INSERT INTO wpp_style_card_versions" in hist_sql
    assert hist_params[2] == 1                     # version
    assert hist_params[8] == "generated"           # reason


def test_each_save_increments_version_without_resetting() -> None:
    conn = FakeConn(current_version=6)
    assert R.save_card("a1", "g1", {"tone": "x"}, conn=conn) == 7
    assert conn.executed[0][1][7] == 7
    assert conn.executed[1][1][2] == 7


def test_confidence_is_clamped_and_nan_becomes_zero() -> None:
    conn = FakeConn()
    R.save_card("a1", "g1", {}, confidence=1.7, conn=conn)
    assert conn.executed[0][1][5] == 1.0
    conn = FakeConn()
    R.save_card("a1", "g1", {}, confidence=float("nan"), conn=conn)
    assert conn.executed[0][1][5] == 0.0


def test_string_style_is_not_double_encoded() -> None:
    """回滚时从历史行搬来的是**原始 JSON 文本**，不能再套一层引号。"""
    conn = FakeConn()
    R.save_card("a1", "g1", '{"tone":"旧"}', conn=conn)
    assert conn.executed[0][1][2] == '{"tone":"旧"}'


# ============================================================ rollback


def _history_row() -> dict:
    return {
        "account_id": "a1", "group_id": "g1", "version": 3,
        "style_json": '{"tone":"旧卡"}', "sample_msgs": 50, "sender_count": 3,
        "confidence": 0.7, "model": "m", "reason": "generated",
        "generated_at": 100,
    }


def test_rollback_copies_old_content_into_a_new_version() -> None:
    conn = FakeConn(current_version=5, version_row=_history_row())
    new_version = R.rollback_card("a1", "g1", 3, conn=conn)

    assert new_version == 6                          # 不是 3：版本号只增不减
    _, hist_params = conn.executed[1]
    assert hist_params[2] == 6
    assert hist_params[3] == '{"tone":"旧卡"}'
    assert hist_params[8] == "rollback"
    assert hist_params[1] == "g1"


def test_rollback_missing_version_returns_none_and_writes_nothing() -> None:
    conn = FakeConn(current_version=5, version_row=None)
    assert R.rollback_card("a1", "g1", 99, conn=conn) is None
    assert conn.executed == []


# ============================================================ 读路径


def test_list_cards_normalizes_types_but_leaves_json_untouched() -> None:
    conn = FakeConn(cards=[{
        "account_id": "a1", "group_id": "g1", "style_json": "{broken",
        "sample_msgs": "12", "sender_count": None, "confidence": "0.812",
        "model": "m", "version": None, "generated_at": "1700000000",
    }])
    rows = R.list_cards("a1", conn=conn)
    assert rows[0]["style_json"] == "{broken"       # SQL 层不解析/不吞坏行
    assert rows[0]["sample_msgs"] == 12
    assert rows[0]["sender_count"] == 0
    assert rows[0]["confidence"] == 0.812
    assert rows[0]["version"] == 1
    assert rows[0]["generated_at"] == 1700000000


def test_count_cards_returns_zero_on_empty() -> None:
    assert R.count_cards("a1", conn=FakeConn()) == 0
    assert R.count_cards("a1", conn=FakeConn(cards=[{"group_id": "g1"}])) == 1
