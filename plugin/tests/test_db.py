"""数据层回归（schema.sql / 切分 / 补齐 / checksum / 行形状）—— 计划第 7 步的 ``test_db.py``。

## 为什么驱动在 **stub** 上，而不是真库

老板铁律：**测试只走 /tmp stub DB** —— 生产机上 MariaDB 与服务同机，测试连真库
就是拿生产陪练。而且本机（dev 树所在机器）上跑着的 3306/3307 是**别的项目**的
1Panel MariaDB，更不该碰。

所以本文件把 ``migrate()`` 驱动在一个内存 stub 上（``_StubDb``）：它只实现
``query`` / ``execute`` 两个口子，按**真的 DDL 文本**更新自己的 information_schema
视图。断言的是**框架发了什么、发了之后状态对不对**（幂等 / 只 ADD / checksum 生命周期）,
**不是** MySQL 自己会怎么做。真库上的那一段由 ``tools/probe_v2*.py`` 在 真机 跑（V2/V2b）。

## 本文件守的六件事（每条都对应一次真实事故）

1. ``schema.sql`` 是**唯一真源**：20 张表、198 列、37 个索引，且台账三个热索引在文件里
   （TS 把它们只写在 ``mysql.ts`` 的 ensureIndex、**没进 schema.sql** —— 双写陷阱实证）。
2. **行尾注释里的逗号**不许把列定义切碎（2026-10-01：``new_msg_id`` 没被认成列 ⇒
   ``ensure_schema_complete`` 去重复 ADD ⇒ ``ERROR 1060``）。
3. 切分/解析出 0 条语句、括号不配对、重复列名 ⇒ **抛**（TS ``applySchemaSql`` 曾静默失效：
   文件在、路径错、切出空、无报错，"以为建好了"）。
4. ``migrate()`` **幂等**：第二次零新建 / 零补齐 / 零应用，checksum 转 ``ok``。
5. ``ensure_schema_complete`` **只 ADD**，绝不 MODIFY / DROP / CHANGE / RENAME（R8）。
6. checksum 变了**只 warn + 留痕、不阻断**（``schema.sql`` 改动是正常开发行为）。

外加两条与"静默失效"同族的边界：R3 host 白名单（防连上生产库）与
``Connection._run`` 的无参路径（``LIKE 'wpp_hf_%'`` 会被 pymysql 的 ``%`` 插值炸掉）。
"""

from __future__ import annotations

import copy
import re
from contextlib import contextmanager

import pytest

from plugin.data import db
from plugin.data import migrations as M
from plugin.data import repo_messages as MSG

SCHEMA_TEXT = M.SCHEMA_FILE.read_text(encoding="utf-8")

#: 从文件里数出来的期望值（**改 schema 时必须跟着改** —— 这正是它的作用：
#: 让"顺手加一列"这件事在 diff 里显形，而不是悄悄漂进线上）。
EXPECTED_TABLES = 20
EXPECTED_COLUMNS = 198
EXPECTED_INDEXES = 37


# ==================================================== 1. schema.sql 是唯一真源


def test_schema_sql_declares_the_expected_shape():
    specs = M.parse_schema(SCHEMA_TEXT)
    assert len(specs) == EXPECTED_TABLES, sorted(specs)
    assert sum(len(s.columns) for s in specs.values()) == EXPECTED_COLUMNS
    assert len(M.iter_indexes()) == EXPECTED_INDEXES

    names = M.expected_table_names()
    assert len(set(names)) == len(names), "表名不许重复"
    # 心流六张表必须在（plan §6）；漏一张 = 心流整套静默降级
    for t in ("wpp_hf_ledger", "wpp_hf_group_state", "wpp_hf_threshold_audit",
              "wpp_hf_group_profile", "wpp_hf_layer_stat"):
        assert t in names, f"schema.sql 缺表 {t}"


def test_the_three_ledger_hot_indexes_live_in_the_ddl_source():
    """台账三个时间列上的索引必须在 **DDL 真源**里，不能只靠代码 ensureIndex。

    TS 侧 ``idx_hf_judged/sent/closed`` 只写在 ``mysql.ts`` 的 ensureIndex 里、
    **没进** ``db/schema.sql``（2026-09-28 审阅发现：5 处热查询全走 filesort）。
    那份"两处都写、改一处必改另一处"的约定已经漂过一次，Python 侧只留 DDL 一处。
    """
    idx = {i for _, i in M.iter_indexes()}
    for name in ("idx_hf_judged", "idx_hf_sent", "idx_hf_closed"):
        assert name in idx, f"{name} 不在 schema.sql 里（hot query 会退化成 filesort）"

    cols = [c for c, _ in M.parse_schema(SCHEMA_TEXT)["wpp_hf_ledger"].columns]
    for col in ("judged_at", "sent_at", "closed_at"):
        assert col in cols, f"台账缺列 {col}"
    # TS 侧 idx_peer 与 idx_peer_kind_peer_id 列完全相同（重复索引）——Python 只留一份
    assert "idx_peer_kind_peer_id" not in idx, "重复索引（与 idx_peer 同列）不该存在"
    assert "idx_peer" in idx


def test_messages_keep_the_columns_that_the_comment_bug_used_to_eat():
    """``wpp_messages`` 的列数钉住 —— 2026-10-01 那次它就是 14 而不是 15。

    ``new_msg_id VARCHAR(128),  -- wechatpadpro new_msg_id`` 后面那行的列定义
    被行尾注释里的**逗号**切碎，``new_msg_id`` 没被认成列 ⇒ 补齐逻辑去
    ``ADD COLUMN new_msg_id`` ⇒ ``ERROR 1060``。这条用例是那次事故的**形状**断言。
    """
    cols = [c for c, _ in M.parse_schema(SCHEMA_TEXT)["wpp_messages"].columns]
    assert len(cols) == 15, cols
    for col in ("msg_id", "new_msg_id", "create_time", "from_wxid", "ts"):
        assert col in cols, f"wpp_messages 缺列 {col}"


# ==================================================== 2. 切分 / 解析


def test_strip_comments_eats_trailing_comments_but_not_string_literals():
    sql = (
        "CREATE TABLE t (\n"
        "  a INT,                    -- 这里有个逗号, 但它不是顶层的\n"
        "  b VARCHAR(8) DEFAULT 'x--y',   -- '--' 在字符串里\n"
        "  c INT\n"
        ");\n"
    )
    stripped = M.strip_comments(sql)
    assert "逗号" not in stripped, "行尾注释要被整段吃掉（包括注释里的逗号）"
    assert "在字符串里" not in stripped
    assert "'x--y'" in stripped, "字符串里的 -- 不是注释，不能吞（吞了字符串状态就翻了）"

    specs = M.parse_schema(sql)
    assert [c for c, _ in specs["t"].columns] == ["a", "b", "c"]


def test_split_statements_ignores_semicolons_inside_strings():
    sql = "INSERT INTO t VALUES ('a;b', 'c'); INSERT INTO t VALUES ('d')"
    stmts = M.split_statements(sql)
    assert stmts == ["INSERT INTO t VALUES ('a;b', 'c')", "INSERT INTO t VALUES ('d')"], \
        "字符串里的分号不是语句边界；最后一条允许不带分号"


def test_a_broken_schema_is_loud_not_silent():
    """三条 raise 都是**防静默失效**（TS ``applySchemaSql`` 曾切出 0 条还报成功）。"""
    conn = _StubDb()
    with pytest.raises(M.SchemaError, match="切出 0 条语句"):
        M._apply_schema_sql(conn, "-- 只有注释，没有语句\n")
    with pytest.raises(M.SchemaError, match="找不到任何 CREATE TABLE"):
        M._apply_schema_sql(conn, "SELECT 1;\n")
    with pytest.raises(M.SchemaError, match="括号不配对"):
        M.parse_schema("CREATE TABLE t (a INT\n")
    with pytest.raises(M.SchemaError, match="重复列名"):
        M.parse_schema("CREATE TABLE t (a INT, a INT);\n")
    with pytest.raises(M.SchemaError, match="0 列"):
        M.parse_schema("CREATE TABLE t ();\n")
    with pytest.raises(M.SchemaError, match="不是合法标识符"):
        # 注释没剥干净/切分错位时**立刻炸**，别静默漏列（漏列的后果是重复 ADD 1060）
        M.parse_schema("CREATE TABLE t (a INT, 1bad INT);\n")


def test_tables_that_did_not_get_created_are_an_error():
    """语句执行了却没建出表 —— 比"没执行"更危险（会以为建好了）。"""
    class _SwallowAll(_StubDb):
        def execute(self, sql, params=()):        # 记下来，但什么都不建
            self.executed.append(sql)
            return 0

    with pytest.raises(M.SchemaError, match="仍缺表"):
        M._apply_schema_sql(_SwallowAll(), "CREATE TABLE t (a INT);\n")


def test_schema_checksum_ignores_line_ending_style():
    assert M.schema_checksum("a\r\nb\r\n") == M.schema_checksum("a\nb\n")
    assert M.schema_checksum("a\nb") != M.schema_checksum("a\nb\n")


# ==================================================== 3. migrate 生命周期（stub）

def test_migrate_is_idempotent_and_only_adds():
    """首跑建表 + 记 checksum；二跑**零动作**、checksum 转 ``ok``。"""
    conn = _StubDb()
    first = M.migrate(conn)
    assert len(first["created_tables"]) == EXPECTED_TABLES
    assert first["expected_tables"] == EXPECTED_TABLES
    assert first["added_columns"] == [] and first["added_indexes"] == []
    assert first["applied_migrations"] == []
    assert first["checksum"] == "recorded", "首跑要把 schema.sql 的哈希记下来"

    second = M.migrate(conn)
    assert second["created_tables"] == [], "第二次不许再建（CREATE IF NOT EXISTS 也只是不发）"
    assert second["added_columns"] == [] and second["added_indexes"] == []
    assert second["checksum"] == "ok", "第二次哈希应当相符"

    # 全程只允许 ADD（R8）—— stub 把每条 ALTER 都记下来了
    assert not [s for s in conn.alters if not re.search(r"\bADD\b", s)], conn.alters
    assert not [s for s in conn.executed
                if re.search(r"\b(MODIFY|DROP|CHANGE|RENAME)\b", s, re.I)], \
        "migration 框架无权改列型 / 删列 / 改名（R8）"


def test_a_missing_column_is_added_without_touching_the_others():
    conn = _StubDb()
    M.migrate(conn)
    # 模拟线上被手工删掉一列（或老库缺列）
    del conn.tables["wpp_messages"]["columns"]["from_wxid"]

    again = M.migrate(conn)
    assert again["created_tables"] == []
    assert again["added_columns"] == ["wpp_messages.from_wxid"]
    assert again["added_indexes"] == []
    assert "from_wxid" in conn.tables["wpp_messages"]["columns"]
    assert [s for s in conn.alters if "from_wxid" in s], "补齐要走 ALTER TABLE ADD COLUMN"


def test_a_missing_index_is_added_too():
    """TS 曾漏搬 5 个热查询索引 ⇒ 补齐逻辑也必须覆盖索引，不只是列。"""
    conn = _StubDb()
    M.migrate(conn)
    del conn.tables["wpp_hf_ledger"]["indexes"]["idx_hf_sent"]

    again = M.migrate(conn)
    assert again["added_indexes"] == ["wpp_hf_ledger.idx_hf_sent"]
    assert "idx_hf_sent" in conn.tables["wpp_hf_ledger"]["indexes"]


def test_checksum_mismatch_warns_and_records_but_does_not_block():
    """``schema.sql`` 被改是**正常开发行为**：只 warn + 留痕，不阻断启动。"""
    conn = _StubDb()
    M.migrate(conn)
    conn.migrations[M.SCHEMA_VERSION_ROW]["checksum"] = "0" * 64   # 假装是旧版本的哈希

    result = M.migrate(conn)
    assert result["checksum"] == "mismatch"
    row = conn.migrations[M.SCHEMA_VERSION_ROW]
    assert row["checksum"] == M.schema_checksum(SCHEMA_TEXT), "要把新哈希写回去（留痕）"
    assert "checksum changed from" in (row["note"] or ""), "痕迹里要写清从哪一版变来的"


def test_migrate_refuses_a_missing_schema_file(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "SCHEMA_FILE", tmp_path / "nope.sql")
    with pytest.raises(M.SchemaError, match="缺失或为空"):
        M.migrate(_StubDb())


# ==================================================== 4. R3：DB 只许本地


def test_non_local_db_host_is_a_red_line(monkeypatch):
    """R3：默认只连 127.0.0.1/localhost/::1/空 —— 防连上 .182 的生产库。"""
    for host in ("127.0.0.1", "localhost", "::1", ""):
        db.assert_host_allowed({"host": host})            # 不抛

    monkeypatch.delenv("WECHATPRO_DB_ALLOW_REMOTE", raising=False)
    with pytest.raises(db.DbConfigError, match="非本地"):
        db.assert_host_allowed({"host": "192.168.1.9"})

    # 显式豁免才放行（日志里要能看见"你正在连远程"）
    # ⚠️ 这里只是**测豁免分支本身**，没有任何连接动作；conftest 禁的是"跑测试时把
    #    ALLOW_REMOTE=1 留在环境里"（那会让别的用例意外碰到远程库）。
    monkeypatch.setenv("WECHATPRO_DB_ALLOW_REMOTE", "1")
    db.assert_host_allowed({"host": "192.168.1.9"})


def test_bad_port_is_a_config_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("WECHATPRO_DB_PORT", "3306abc")
    with pytest.raises(db.DbConfigError, match="不是整数"):
        db.config()


def test_describe_never_leaks_the_password(monkeypatch):
    monkeypatch.setenv("WECHATPRO_DB_PASSWORD", "hunter2-should-not-appear")
    monkeypatch.setenv("WECHATPRO_DB_NAME", "wechatpro_test")
    line = db.describe()
    assert "wechatpro_test" in line and "hunter2" not in line


# ==================================================== 5. 连接层：无参不插值


class _Cursor:
    """假游标：**记下被怎么调的**。pymysql 的 ``execute(sql, ())`` 仍会做 ``%`` 插值。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.rowcount = 0
        self.lastrowid = 0

    def execute(self, *args):
        self.calls.append(args)
        if len(args) > 1:
            sql = args[0]
            if "wpp_hf_%" in sql:                       # 真 pymysql 会在这里炸
                raise AssertionError(
                    "无参 SQL 被当成带参调用 —— pymysql 会对字面量 % 做插值: " + sql)

    def fetchall(self):
        return [{"ok": 1}]

    def executemany(self, sql, seq):
        self.calls.append((sql, tuple(seq)))

    def close(self):
        pass


class _Raw:
    open = True

    def __init__(self) -> None:
        self.cur = _Cursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.open = False


def test_no_param_sql_avoids_percent_interpolation():
    """2026-10-01 实测：``SHOW TABLES LIKE 'wpp_hf_%'`` 必须走**单参** ``execute``。

    否则报 ``ProgrammingError: not enough arguments for format string`` —— 而这是
    "无参却插值"，不是 SQL 写错，排查时极易走偏。
    """
    raw = _Raw()
    conn = db.Connection(raw)
    conn.query("SHOW TABLES LIKE 'wpp_hf_%'")           # 不抛
    assert raw.cur.calls == [("SHOW TABLES LIKE 'wpp_hf_%'",)], "无参要走单参形式"

    raw.cur.calls.clear()
    conn.query("SELECT 1 FROM t WHERE a = %s", (7,))
    assert raw.cur.calls == [("SELECT 1 FROM t WHERE a = %s", (7,))], "有参才交给 pymysql"


def test_execute_outside_a_transaction_commits():
    raw = _Raw()
    conn = db.Connection(raw)
    conn.execute("DELETE FROM t WHERE a = %s", (1,))
    assert raw.commits == 1, "不在显式事务里时立即提交（否则攥着旧快照）"


# ==================================================== 6. 行形状 / JS 语义对齐


def test_round_half_up_matches_js_math_round():
    """``Math.round`` 是**半值向 +∞**，Python ``round`` 是银行家舍入 —— 别混。"""
    assert MSG._round_half_up(2.5) == 3
    assert MSG._round_half_up(2.4) == 2
    assert MSG._round_half_up(0.5) == 1
    assert MSG._round_half_up(-0.5) == 0                  # JS: Math.round(-0.5) === -0
    assert MSG._round_half_up(None) == 0, "子查询无行 ⇒ NULL ⇒ 0"
    assert MSG._round_half_up("abc") == 0
    assert MSG._round_half_up(float("nan")) == 0
    assert MSG._round_half_up(float("inf")) == 0
    assert round(2.5) == 2, "（这里只是提醒：Python 内建 round 不是我们要的语义）"


def _sql_literals(path) -> list[str]:
    """文件里所有**运行期**字符串常量（**排除 docstring**）。

    排除 docstring 是必须的：本模块的 docstring 恰恰在**反着讲**这件事
    （"直接 ``GROUP BY chat_id`` 会让 bot 侧分子恒 0"），拿全文当判据会被自己的
    说明文字打败。这个坑 TS 那批测试也踩过（注释里出现 "heuristic 1"）。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(
        n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and n.body and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
        and isinstance(n.body[0].value.value, str)}
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def test_group_key_is_coalesce_nullif_chat_id_peer_id():
    """``COALESCE(NULLIF(chat_id,''), peer_id)`` 三处都在（出站行 chat_id 恒 NULL）。

    直接 ``GROUP BY chat_id`` 会让 bot 侧分子恒 0、占比永远 0%，**且不报错**。
    """
    from pathlib import Path

    lits = _sql_literals(Path(MSG.__file__))
    hits = sum(s.count("COALESCE(NULLIF(chat_id, ''), peer_id)") for s in lits)
    assert hits == 3, f"群里分组口径的三处查询都要用它（实际 {hits} 处）"
    # 反面：**运行期** SQL 里不许出现裸 GROUP BY chat_id
    bad = [s[:60] for s in lits if re.search(r"GROUP\s+BY\s+chat_id\b", s, re.I)]
    assert not bad, f"裸 GROUP BY chat_id（bot 侧占比恒 0% 且不报错）: {bad}"


def test_local_offset_follows_the_runtime_not_a_hardcoded_plus_8():
    """TS 的 ``-getTimezoneOffset()*60``：**符号**和**取样时刻**都别顺手改。"""
    import time as _t

    off = MSG._local_offset_sec(int(_t.time()))
    assert off == -_t.timezone, "非夏令时地区应等于 -timezone（东八区 = +28800）"


# ============================================ 按锚点回查一条消息（引用回复）
#
# 老板 2026-10-03 报的 bug（「群里 @ 机器人时回复了，但不会 @ 被回复的人」）的数据源头
# 就是这一个函数：它是"被回复的是谁"的唯一供数点。失效形态全都**不报错**
# （查不到 ⇒ 悄悄不 @；条件写宽 ⇒ @ 错人）⇒ 判据一律下在**发出去的 SQL 与参数**上。


class _Q:
    """假连接：只记 ``query_one`` 被怎么调的（``get_message_by_id_or_new_id`` 唯一走法）。"""

    def __init__(self, row=None) -> None:
        self.calls: list[tuple] = []
        self.row = row

    def query_one(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        return self.row

    def query(self, sql, params=()):
        raise AssertionError("这一条读不该走 query()（它要的是单行）: " + sql)


def _q(**kw):
    """调 ``get_message_by_id_or_new_id``，回 ``(返回值, 假连接)``。"""
    conn = _Q(row=kw.pop("row", None))
    out = MSG.get_message_by_id_or_new_id(kw.pop("account_id", "acc1"), conn=conn, **kw)
    return out, conn


def test_both_ids_empty_never_touches_the_database():
    """两个 id 都空 ⇒ 直接 ``None``，**一次查询都不发**（TS：``if (!msgId && !newMsgId)``）。

    发出去的那条是 ``msg_id = NULL`` —— 恒不匹配，纯粹白跑一次库；而引用回复是**每条
    群消息**都要走的路，白跑一次就是白跑 N 次。
    """
    for kw in ({}, {"msg_id": None, "new_msg_id": None},
               {"msg_id": "", "new_msg_id": ""}, {"msg_id": "   "}):
        out, conn = _q(**kw)
        assert out is None and conn.calls == [], kw


@pytest.mark.parametrize("kw,params", [
    ({"msg_id": "9001"}, ("acc1", "9001")),
    ({"new_msg_id": "9001"}, ("acc1", "9001")),
    ({"msg_id": "9001", "new_msg_id": "9002"}, ("acc1", "9001", "9002")),
    ({"msg_id": 9001}, ("acc1", "9001")),               # 数字 id 也要当字符串用
    ({"msg_id": " 9001 ", "new_msg_id": ""}, ("acc1", "9001")),   # 去空白后只有一个
])
def test_only_the_given_ids_are_bound_and_blank_ones_drop_out(kw, params):
    out, conn = _q(**kw)
    assert len(conn.calls) == 1
    sql, bound = conn.calls[0]
    assert bound == params
    # 占位符个数必须与传进去的值个数**相等**：空的那一半若留下 ``new_msg_id = %s``
    # 却绑不上值，pymysql 会直接报错（而这是"少一个 id 就整条链路挂掉"的形态）。
    assert sql.count("%s") == len(params)
    # 占位符个数与 id 个数**一一对应**：只给一个 id 时不许出现 OR（多出来的那一半
    # 在 SQL 里留着、却没值可绑，正是上一句要防的东西）。
    assert sql.count(" OR ") == (1 if len(params) == 3 else 0), sql


def test_the_two_id_clauses_are_wrapped_in_one_parenthesis():
    """``account_id = %s AND (msg_id = %s OR new_msg_id = %s)`` —— 括号是**语义**不是排版。

    ``AND`` 优先级高于 ``OR``：少了那对括号，条件会变成
    ``(account_id = ? AND msg_id = ?) OR new_msg_id = ?`` ⇒ **任何账号**下 new_msg_id
    命中的行都会回给这个账号（跨账号串号）。TS 的 ``mysql.ts:620`` 就是为这个加的括号。
    """
    _, conn = _q(msg_id="9001", new_msg_id="9002")
    sql, _ = conn.calls[0]
    assert "account_id = %s AND (msg_id = %s OR new_msg_id = %s)" in sql, sql


def test_the_query_is_not_filtered_by_direction():
    """**不限方向**是这一条的全部要点（TS ``{direction:'any'}``，v1.3.18 P1-核心2）。

    被引用的消息完全可能是 **bot 自己发的**（``direction='outbound'``）—— 群里引用
    机器人的话再说话就是这个形态。加上 ``direction='inbound'`` 之后，这一类**永远查不到**，
    表现只是"不 @ 人"，与"这功能没做"长得一模一样。
    """
    _, conn = _q(msg_id="9001")
    sql, _ = conn.calls[0]
    where = sql.split("WHERE", 1)[1]          # ``direction`` 是**选的列**，看的是条件那一半
    assert "direction" not in where, where


def test_it_asks_for_the_columns_the_identity_chain_needs():
    """``raw_payload`` 与 ``content`` 是发送者兜底的**第二、三档**（前两档是 ``from_wxid``）。

    少一列 ⇒ ``quote_ref`` 那一档静默失效（``row.get(...)`` 恒 ``None``），
    而现场只表现为"@ 不出来"。
    """
    _, conn = _q(msg_id="9001")
    sql, _ = conn.calls[0]
    for col in ("msg_id", "new_msg_id", "peer_kind", "peer_id", "chat_id", "peer_name",
                "from_wxid", "msg_type", "content", "raw_payload"):
        assert col in sql, col
    assert "FROM wpp_messages" in sql and "account_id = %s" in sql


def test_it_is_deterministic_even_without_the_ts_row_ids():
    """``LIMIT 1`` 之外还带 ``ORDER BY ts DESC, id DESC``（TS 没有，这是 py2 有意加的）。

    同 id 多行时（重放/重复落库）"选中的是哪一行"必须可复现 —— 否则昵称可能这次有、
    下次没有，看起来像随机行为。
    """
    _, conn = _q(msg_id="9001")
    sql, _ = conn.calls[0]
    assert "ORDER BY ts DESC, id DESC LIMIT 1" in sql, sql


def test_it_returns_whatever_the_connection_returns():
    row = {"msg_id": "9001", "from_wxid": "wxid_a", "peer_name": "张三"}
    out, _ = _q(msg_id="9001", row=row)
    assert out is row
    assert _q(msg_id="9001", row=None)[0] is None


def test_without_a_conn_it_asks_the_pool(monkeypatch):
    """省略 ``conn`` ⇒ 走 ``db.get_conn()``（探针/生产路径）。"""
    conn = _Q(row={"msg_id": "9001"})
    monkeypatch.setattr(db, "get_conn", lambda: conn)
    assert MSG.get_message_by_id_or_new_id("acc1", msg_id="9001") == {"msg_id": "9001"}
    assert len(conn.calls) == 1


# ==================================================== stub（内存库）
#
# ★ D2（2026-10-02 修）：这个桩**自己解析 DDL**，不许再借 ``migrations`` 的解析内部
# （``M._CREATE_RE`` / ``M._match_paren`` / ``M._split_top_level`` / ``M._CONSTRAINT_PREFIXES``
# / ``M._INDEX_NAME_RE``）。借了就成了自证：框架的切分器漏掉一列，桩用**同一个**切分器
# 也漏掉同一列 ⇒ 「框架建了什么」与「桩以为建了什么」永远一致，测试恒绿。
# 那正是 TS ``applySchemaSql`` 静默失效能活很久的原因（文件在、路径错、切出空、无报错）。
# 所以下面这套括号匹配 / 顶层逗号切分 / 约束识别是**照 DDL 语义重写一遍**的独立实现。


_STUB_CREATE_RE = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\(",
    re.IGNORECASE)
_STUB_ADD_RE = re.compile(r"^\s*ALTER\s+TABLE\s+`?(\w+)`?\s+ADD\s+(.*)$", re.IGNORECASE)
_STUB_INDEX_NAME_RE = re.compile(
    r"^(?:UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+)?(?:KEY|INDEX)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
    re.IGNORECASE)
#: 表级约束/索引的开头（不是列定义）——**独立**列一遍，不抄框架那份。
_STUB_CONSTRAINT_HEADS = (
    "PRIMARY KEY", "UNIQUE KEY", "UNIQUE INDEX", "KEY", "INDEX",
    "FULLTEXT", "SPATIAL", "CONSTRAINT", "FOREIGN KEY", "CHECK",
)
#: 列定义里"类型"到此为止的关键词（后面的都是约束/属性）
_STUB_COL_STOP_RE = re.compile(
    r"\b(NOT|NULL|DEFAULT|AUTO_INCREMENT|PRIMARY|UNIQUE|KEY|COMMENT|COLLATE|"
    r"CHARACTER|REFERENCES|CHECK|GENERATED|VIRTUAL|STORED|ON)\b",
    re.IGNORECASE)
_STUB_DEFAULT_RE = re.compile(r"\bDEFAULT\s+('(?:[^']|'')*'|\S+)", re.IGNORECASE)


def _stub_match_paren(text: str, open_idx: int) -> int:
    """``text[open_idx] == '('`` 对应的右括号位置（自己数层数，**不借框架的**）。"""
    depth = 0
    quote = ""
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "\\":
                i += 1                      # 反斜杠转义：跳过下一个字符
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise AssertionError(f"stub 的括号匹配失败（DDL 被切坏了？）: {text[:80]}")


def _stub_split_top_level(body: str) -> list[str]:
    """按**顶层**逗号切分（括号内 / 字符串内的逗号不算）——独立实现。"""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            elif ch == "\\" and i + 1 < len(body):
                buf.append(body[i + 1])
                i += 1
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return [p for p in out if p]


def _stub_index_columns(defn: str) -> list[str]:
    """``KEY idx (a, b)`` / ``UNIQUE KEY uk (a)`` → ``["a", "b"]``。"""
    open_idx = defn.find("(")
    if open_idx < 0:
        return []
    inner = defn[open_idx + 1:_stub_match_paren(defn, open_idx)]
    return [c.strip().strip("`") for c in _stub_split_top_level(inner)]


def _stub_col_row(defn: str, is_pk: bool = False) -> dict:
    """把一条列定义翻译成 information_schema 形状的一行（``type``/``nullable``/``dflt``/``extra``）。

    这是**桩自己的**信息视图：真库里这些值由服务端给出，这里由定义文本推出来。
    与 ``migrations._expected_col_parts`` 是两条独立的路（那条读 schema.sql 的期望态，
    这条读"桩以为库里是什么"）。

    ★ 这里按 **MariaDB** 建模，不是按 SQL 教科书 —— 三条实测差异（2026-10-02 在 真机
    真库上对着 ``wechatpro_test`` 读出来的），少一条就会让本桩**假绿**：

    1. **主键列恒 NOT NULL**：``id INT AUTO_INCREMENT PRIMARY KEY`` 里没写 ``NOT NULL``，
       服务端照样报 ``NO``（``is_pk`` 由调用方从表级/行内主键算出来）。
    2. **可空列的默认值是字符串 ``"NULL"``**（MySQL 回的是 SQL ``NULL``）：写
       ``display_name VARCHAR(128)``（无 DEFAULT 子句）或显式 ``DEFAULT NULL``，
       回来都是字符串 ``"NULL"`` ⇒ "有没有默认值"必须按 :func:`migrations._live_has_default`
       的口径判，不能只看 ``is not None``。
    3. **``JSON`` 是 ``LONGTEXT`` 的别名**：``COLUMN_TYPE`` 报 ``longtext``。

    这三条原来只写在"我以为"里，于是本桩 158 列**全绿**而真库一次报 77 条 —— 正是
    "stub 只能验我们以为它会补什么"那句话的现场。
    """
    parts = defn.split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    stop = _STUB_COL_STOP_RE.search(rest)
    type_sig = (rest[:stop.start()] if stop else rest).strip()
    up = rest.upper()
    dm = _STUB_DEFAULT_RE.search(rest)
    has_not_null = bool(re.search(r"\bNOT\s+NULL\b", up))
    if dm is None:
        # 没有 DEFAULT 子句：MariaDB 把可空列存成 DEFAULT NULL 并报字符串 "NULL"
        dflt = None if has_not_null else "NULL"
    elif dm.group(1).strip().strip("'\"").upper() == "NULL":
        dflt = "NULL"
    else:
        dflt = dm.group(1).strip()
    return {
        "type": "longtext" if type_sig.lower() == "json" else type_sig,
        "nullable": "NO" if (has_not_null or is_pk or "PRIMARY KEY" in up) else "YES",
        "dflt": dflt,
        "extra": "auto_increment" if "AUTO_INCREMENT" in up else "",
    }


class _StubDb:
    """``migrate()`` 的最小驱动面：**只理解本框架会发的三类语句**。

    它**故意不是** SQL 引擎：真库行为（InnoDB、collation、filesort）由 真机 上的
    ``tools/probe_v2*.py`` 验。这里只回答两个问题：框架发了什么？发完之后状态对不对？

    ``transaction()`` 不是摆设：D4 之后 migration 循环走 ``conn.transaction()``，
    而"要么全成要么全不成"这件事只有**真能回滚**的桩才验得了 ⇒ 这里做快照/还原，
    并把 begin/commit/rollback 记进 :attr:`tx_log`。
    """

    def __init__(self) -> None:
        self.tables: dict[str, dict] = {}
        self.migrations: dict[int, dict] = {}
        self.executed: list[str] = []
        self.alters: list[str] = []
        self.tx_log: list[str] = []
        self._depth = 0
        self._snap: list | None = None

    # --------------------------------------------------------------- 写
    def execute(self, sql: str, params=()) -> int:
        s = " ".join(sql.split())
        self.executed.append(s)
        low = s.lower()

        m = _STUB_CREATE_RE.match(s)
        if m:
            name = m.group(1)
            if name in self.tables and "if not exists" in low:
                return 0               # **IF NOT EXISTS 的语义**：已存在就什么都不做
            open_idx = s.index("(")
            close_idx = _stub_match_paren(s, open_idx)
            cols, idx, pk = {}, {}, set()
            for item in _stub_split_top_level(s[open_idx + 1:close_idx]):
                if item.upper().startswith(_STUB_CONSTRAINT_HEADS):
                    if item.upper().startswith("PRIMARY KEY"):
                        pk.update(c.lower() for c in _stub_index_columns(item))
                        continue
                    im = _STUB_INDEX_NAME_RE.match(item)
                    if im:
                        idx[im.group(1)] = item
                    continue
                cols[item.split()[0].strip("`")] = item
            self.tables.setdefault(name, {"columns": {}, "indexes": {}, "pk": set()})
            self.tables[name]["columns"].update(cols)
            self.tables[name]["indexes"].update(idx)
            self.tables[name]["pk"].update(pk)
            return 0

        m = _STUB_ADD_RE.match(s)
        if m:
            self.alters.append(s)
            table, clause = m.group(1), m.group(2)
            im = _STUB_INDEX_NAME_RE.match(clause)
            if im:
                self.tables[table]["indexes"][im.group(1)] = clause
            else:
                col = re.sub(r"^COLUMN\s+", "", clause, flags=re.I)
                self.tables[table]["columns"][col.split()[0].strip("`")] = col
                if "PRIMARY KEY" in col.upper():       # ALTER 里加的行内主键
                    self.tables[table]["pk"].add(col.split()[0].strip("`").lower())
            return 0

        if "wpp_schema_migrations" in low and low.startswith("insert into"):
            version, name, checksum, note = params
            self.migrations[int(version)] = {
                "version": int(version), "name": name, "checksum": checksum, "note": note}
            return 1

        return 0                       # 别的（含裸 START TRANSACTION，见 D4 用例）：忽略

    def executemany(self, sql, seq) -> int:
        return 0

    # --------------------------------------------------------------- 事务（D4）
    @contextmanager
    def transaction(self):
        """快照 / 还原 —— 让「migration 中途炸掉不留半套」这件事**可断言**。

        ⚠️ **本桩比真服务端宽容**：它连 DDL 一起快照回滚，而 MariaDB 的 ``ALTER``
        是**隐式提交**的（真库实测，见 ``Migration`` docstring）。所以这里验的只是
        "框架有没有把 body 包进事务"，**不许**拿桩上的绿去推断"半套 schema 会被回滚"——
        那条真判据在 ``tests/test_db_live.py`` 的真库用例里，且结论是"回滚不掉，只能靠幂等"。
        """
        snap = (copy.deepcopy(self.tables), copy.deepcopy(self.migrations))
        self._depth += 1
        self.tx_log.append("begin")
        try:
            yield self
        except BaseException:
            self.tables, self.migrations = snap
            self.tx_log.append("rollback")
            raise
        finally:
            self._depth -= 1
            if self._depth == 0 and self.tx_log and self.tx_log[-1] != "rollback":
                self.tx_log.append("commit")

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    # --------------------------------------------------------------- 读
    def query(self, sql: str, params=()) -> list[dict]:
        s = " ".join(sql.split())
        if "information_schema.TABLES" in s:
            return [{"t": t} for t in self.tables]
        if "COLUMN_TYPE" in s:                     # 体检那条（要定义，不只名字）
            tab = self.tables[params[0]]
            return [dict({"c": c}, **_stub_col_row(d, c.lower() in tab["pk"]))
                    for c, d in sorted(tab["columns"].items())]
        if "information_schema.COLUMNS" in s:
            return [{"c": c} for c in sorted(self.tables[params[0]]["columns"])]
        if "information_schema.STATISTICS" in s:
            out = []
            for name, defn in sorted(self.tables[params[0]]["indexes"].items()):
                for seq, col in enumerate(_stub_index_columns(defn), start=1):
                    out.append({"n": name, "c": col, "s": seq})
            return out
        if s.lower().startswith("select version, checksum"):
            return [dict(r) for r in self.migrations.values()]
        return []

    def query_one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None


# ==================================================== 7. 事务与快照（D4 / D5）
#
# 这两条坏的形态都是"**不报错、只是数据慢慢变错**"：
#   D4 —— migration 发的是裸 ``START TRANSACTION``，而 ``Connection.execute`` 收尾时
#         （depth==0）把它**自己提交掉了** ⇒ 中途炸掉留半套 schema，调用方却以为原子。
#   D5 —— 只读查询不提交 ⇒ ``autocommit=False`` + REPEATABLE READ 下线程内复用几十年
#         的连接**永远读同一个快照**：新台账看不见、"最后真发言"不动，一行日志都没有。


def test_a_raw_start_transaction_would_self_commit():
    """D4 的**反面证据**：裸 ``START TRANSACTION`` 在 ``execute()`` 里会被自己提交掉。

    所以 migration 循环不能再发它 —— 那条路在 ``autocommit=False`` 下等于**没开事务**。
    """
    raw = _Raw()
    conn = db.Connection(raw)
    conn.execute("START TRANSACTION")
    assert raw.commits == 1, \
        "execute() 在 depth==0 时会提交 ⇒ 裸 START TRANSACTION 立即被提交、事务是假的"


def test_reads_outside_a_transaction_commit_too():
    """★ D5：读也要收尾。挂着不提交 = 线程内**永远**读同一个快照。"""
    raw = _Raw()
    conn = db.Connection(raw)
    conn.query("SELECT 1 AS ok")
    assert raw.commits == 1, "只读查询不在显式事务里时也要提交（否则快照永远不刷新）"
    conn.query_one("SELECT 1 AS ok")
    conn.query_value("SELECT 1 AS ok")
    assert raw.commits == 3, "query_one / query_value 走同一条路"


def test_reads_inside_a_transaction_do_not_commit_early():
    """事务里的读**绝不能**提交 —— 提交掉就等于把外层原子性拆了。"""
    raw = _Raw()
    conn = db.Connection(raw)
    with conn.transaction():
        conn.query("SELECT 1 AS ok")
        conn.execute("UPDATE t SET a = 1")
        assert raw.commits == 0, "事务里的读写都不许提前提交"
    assert raw.commits == 1, "只在最外层提交一次"


def test_a_transaction_rolls_back_and_commits_nothing_on_failure():
    raw = _Raw()
    conn = db.Connection(raw)
    with pytest.raises(RuntimeError, match="业务炸了"):
        with conn.transaction():
            conn.execute("UPDATE t SET a = 1")
            raise RuntimeError("业务炸了")
    assert raw.commits == 0 and raw.rollbacks == 1, "炸了就只许回滚"


def test_nested_transactions_only_the_outermost_decides():
    """可重入：内层退出既不提交也不回滚，一切延到最外层。"""
    raw = _Raw()
    conn = db.Connection(raw)
    with conn.transaction():
        with conn.transaction():
            conn.execute("UPDATE t SET a = 1")
            assert raw.commits == 0 and raw.rollbacks == 0
        assert (raw.commits, raw.rollbacks) == (0, 0), "内层退出不许提交"
    assert (raw.commits, raw.rollbacks) == (1, 0), "最外层才提交"

    # 内层抛 ⇒ 外层看到异常 ⇒ 最外层回滚（内层自己不回滚，否则外层就白回滚了）
    raw2 = _Raw()
    conn2 = db.Connection(raw2)
    with pytest.raises(ValueError):
        with conn2.transaction():
            with conn2.transaction():
                raise ValueError("内层炸")
    assert (raw2.commits, raw2.rollbacks) == (0, 1)


def test_depth_is_restored_even_after_a_failure():
    """depth 留在高位 = 此后的读写**再也不提交** —— 静默、且极难查。"""
    raw = _Raw()
    conn = db.Connection(raw)
    with pytest.raises(RuntimeError):
        with conn.transaction():
            raise RuntimeError("炸")
    conn.execute("UPDATE t SET a = 1")
    assert raw.commits == 1, "事务结束后 depth 必须回 0，后续写照常提交"


def test_module_level_transaction_uses_the_same_implementation(monkeypatch):
    """模块级 ``db.transaction()`` 只是"本线程连接上那份"的转发，不许各写一套。"""
    raw = _Raw()
    monkeypatch.setattr(db, "get_conn", lambda: db.Connection(raw))
    with db.transaction() as conn:
        conn.query("SELECT 1 AS ok")
        assert raw.commits == 0
    assert raw.commits == 1


def test_migrations_run_each_body_inside_a_transaction(monkeypatch):
    """★ D4 的正面证据：migration 循环**逐条**开事务，且不再发裸 START TRANSACTION。"""
    calls: list[str] = []

    def _half(conn):
        conn.execute("ALTER TABLE wpp_messages ADD COLUMN half_done INT NULL")
        calls.append("写了一半")
        raise RuntimeError("migration 炸了")

    monkeypatch.setattr(M, "_MIGRATIONS", (M.Migration(1, "half_done", _half),))
    conn = _StubDb()
    with pytest.raises(RuntimeError, match="migration 炸了"):
        M.migrate(conn)

    assert calls == ["写了一半"], "migration 应当真被执行到（否则这条用例什么都没验）"
    assert conn.tx_log == ["begin", "rollback"], f"必须进事务并回滚: {conn.tx_log}"
    assert 1 not in conn.migrations, "炸掉的 migration **不许**留下已应用记录（否则永不重跑）"
    assert "half_done" not in conn.tables["wpp_messages"]["columns"], \
        "半套改动要跟着回滚（这正是旧写法留下的现场）"
    assert not [s for s in conn.executed if "START TRANSACTION" in s.upper()], \
        "D4：不许再发裸 START TRANSACTION（它会被 execute 自己提交掉）"


def test_a_successful_migration_commits_its_transaction(monkeypatch):
    def _ok(conn):
        conn.execute("ALTER TABLE wpp_messages ADD COLUMN done_flag TINYINT NOT NULL DEFAULT 0")

    monkeypatch.setattr(M, "_MIGRATIONS", (M.Migration(1, "done_flag", _ok),))
    conn = _StubDb()
    result = M.migrate(conn)
    assert result["applied_migrations"] == ["1:done_flag"]
    assert conn.tx_log == ["begin", "commit"]
    assert 1 in conn.migrations and "done_flag" in conn.tables["wpp_messages"]["columns"]


# ==================================================== 8. 定义漂移体检（D6）


class _Live:
    """**手写**的线上形状：只回答 ``check_schema_drift`` 会问的三张视图。

    行是测试**逐字段写死**的，桩的解析器不参与 —— 这条路径要验的正是"线上与期望不一致
    时它报不报"，用会自己推导的桩就等于让被测逻辑给自己出题（D2 那条教训）。
    """

    def __init__(self, tables, columns, indexes=None) -> None:
        self.tables = list(tables)
        self.columns = columns                 # {表: {列: information_schema 行}}
        self.indexes = indexes or {}           # {表: {索引名: "列1,列2"}}

    def query(self, sql: str, params=()) -> list[dict]:
        s = " ".join(sql.split())
        if "information_schema.TABLES" in s:
            return [{"t": t} for t in self.tables]
        if "information_schema.COLUMNS" in s:
            return [dict(r) for r in self.columns.get(params[0], {}).values()]
        if "information_schema.STATISTICS" in s:
            out = []
            for name, cols in self.indexes.get(params[0], {}).items():
                for seq, col in enumerate(str(cols).split(","), start=1):
                    out.append({"n": name, "c": col, "s": seq})
            return out
        return []

    def query_one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None


#: 期望态（``sql_text``）——一条列全乎的表，用来一条条改**线上行**
_DRIFT_SQL = """
CREATE TABLE IF NOT EXISTS t (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name VARCHAR(64) NOT NULL,
  note TEXT NULL,
  n INT NOT NULL DEFAULT 0,
  made_at TIMESTAMP NOT NULL,
  KEY idx_name (name, note)
) ENGINE=InnoDB;
"""


def _live_row(name: str, type_="varchar(64)", nullable="NO", dflt=None, extra="") -> dict:
    return {"c": name, "type": type_, "nullable": nullable, "dflt": dflt, "extra": extra}


def _drift(columns: dict, indexes: dict | None = None) -> list[str]:
    return M.check_schema_drift(_Live(["t"], {"t": columns}, {"t": indexes or {}}), _DRIFT_SQL)


def _matching_columns() -> dict:
    """与 ``_DRIFT_SQL`` **完全一致**的线上形状（基线：必须零漂移）。"""
    return {
        "id": _live_row("id", "bigint(20) unsigned", "NO", None, "auto_increment"),
        "name": _live_row("name", "varchar(64)", "NO"),
        "note": _live_row("note", "text", "YES"),
        "n": _live_row("n", "int(11)", "NO", "0"),
        "made_at": _live_row("made_at", "timestamp", "NO"),
    }


def test_a_freshly_applied_schema_has_no_drift():
    """基线（**零假警报**）：线上与期望一字不差时必须是空的。"""
    assert _drift(_matching_columns(), {"idx_name": "name,note"}) == []


def test_display_widths_and_case_are_not_drift():
    """``INT``↔``int(11)``、``BIGINT``↔``bigint(20)`` 是服务端补的显示宽度，不是漂移。"""
    cols = _matching_columns()
    cols["id"] = _live_row("id", "BIGINT(20) UNSIGNED", "NO", None, "auto_increment")
    cols["n"] = _live_row("n", "INT", "NO", "0")
    assert _drift(cols) == []


#: ★ MariaDB 的三种「服务端说法与我们写法不同」——**都不是漂移**，2026-10-02 在 真机
#: 真库上实测出来的（本桩原来按 SQL 教科书建模，于是 158 列全绿而真库一次报 77 条）：
#: 1. 主键列（行内 / 表级）没写 ``NOT NULL``，服务端恒回 ``NO``；
#: 2. 可空列的默认值回来是**字符串 ``"NULL"``**（MySQL 回 SQL ``NULL``）；
#: 3. ``JSON`` 是 ``LONGTEXT`` 的别名，``COLUMN_TYPE`` 报 ``longtext``。
_MARIADB_SQL = """
CREATE TABLE IF NOT EXISTS t (
  account_id VARCHAR(64) NOT NULL,
  display_name VARCHAR(128),
  config_json JSON,
  pair_id BIGINT UNSIGNED,
  PRIMARY KEY (account_id, pair_id)
) ENGINE=InnoDB;
"""


def _drift_maria(columns: dict) -> list[str]:
    return M.check_schema_drift(_Live(["t"], {"t": columns}, {"t": {}}), _MARIADB_SQL)


def test_mariadb_shapes_are_not_drift() -> None:
    """★ 基线：真 MariaDB 的形状（PK 恒 NOT NULL / 默认值是 ``"NULL"`` / json=longtext）。"""
    assert _drift_maria({
        "account_id": _live_row("account_id", "varchar(64)", "NO"),
        "display_name": _live_row("display_name", "varchar(128)", "YES", "NULL"),
        "config_json": _live_row("config_json", "longtext", "YES", "NULL"),
        "pair_id": _live_row("pair_id", "bigint(20) unsigned", "NO"),
    }) == []


def test_the_primary_key_rule_does_not_swallow_real_nullability_drift() -> None:
    """主键那条归一化**只**加在"期望 NOT NULL"一侧：真可空了照样报。"""
    out = _drift_maria({
        "account_id": _live_row("account_id", "varchar(64)", "NO"),
        "display_name": _live_row("display_name", "varchar(128)", "YES", "NULL"),
        "config_json": _live_row("config_json", "longtext", "YES", "NULL"),
        "pair_id": _live_row("pair_id", "bigint(20) unsigned", "YES"),
    })
    assert len(out) == 1 and "t.pair_id" in out[0] and "可空性" in out[0], out


def test_the_null_string_rule_does_not_swallow_a_real_default() -> None:
    """``"NULL"`` 当作"没有默认值"，但真空串 ``''`` 仍要报（默认值真的变了）。"""
    out = _drift_maria({
        "account_id": _live_row("account_id", "varchar(64)", "NO"),
        "display_name": _live_row("display_name", "varchar(128)", "YES", ""),
        "config_json": _live_row("config_json", "longtext", "YES", "NULL"),
        "pair_id": _live_row("pair_id", "bigint(20) unsigned", "NO"),
    })
    assert len(out) == 1 and "t.display_name" in out[0] and "默认值" in out[0], out


def test_the_json_alias_does_not_swallow_a_real_type_change() -> None:
    """``json`` ≡ ``longtext``，但线上真变成 ``varchar`` 仍要报。"""
    out = _drift_maria({
        "account_id": _live_row("account_id", "varchar(64)", "NO"),
        "display_name": _live_row("display_name", "varchar(128)", "YES", "NULL"),
        "config_json": _live_row("config_json", "varchar(64)", "YES", "NULL"),
        "pair_id": _live_row("pair_id", "bigint(20) unsigned", "NO"),
    })
    assert len(out) == 1 and "t.config_json" in out[0] and "类型" in out[0], out


def test_an_inline_primary_key_counts_as_not_null_too() -> None:
    """行内写法（``id INT AUTO_INCREMENT PRIMARY KEY``，本仓 15 张表里的 8 张）同上。"""
    sql = "CREATE TABLE IF NOT EXISTS t (id INT AUTO_INCREMENT PRIMARY KEY) ENGINE=InnoDB;"
    live = _Live(["t"], {"t": {"id": _live_row("id", "int(11)", "NO", None, "auto_increment")}}, {"t": {}})
    assert M.check_schema_drift(live, sql) == []


def test_a_narrowed_type_is_reported():
    """``VARCHAR(64)`` 被改成 128 ⇒ 写 100 字符不再截断 —— 反向（收窄）必须报。"""
    cols = _matching_columns()
    cols["name"] = _live_row("name", "varchar(32)", "NO")
    out = _drift(cols)
    assert len(out) == 1 and "t.name" in out[0] and "varchar(32)" in out[0] \
        and "varchar(64)" in out[0], out


def test_nullability_drift_is_reported():
    cols = _matching_columns()
    cols["name"] = _live_row("name", "varchar(64)", "YES")
    out = _drift(cols)
    assert len(out) == 1 and "可空性" in out[0] and "NOT NULL" in out[0], out


def test_default_drift_is_reported():
    """期望 ``DEFAULT 0`` 而线上没有默认值 ⇒ 插入不给值时行为变了。"""
    cols = _matching_columns()
    cols["n"] = _live_row("n", "int(11)", "NO", None)
    out = _drift(cols)
    assert len(out) == 1 and "默认值" in out[0], out

    cols = _matching_columns()
    cols["name"] = _live_row("name", "varchar(64)", "NO", "x")   # 线上多出默认值也算漂移
    assert "默认值" in _drift(cols)[0]


def test_timestamp_defaults_are_not_compared():
    """``TIMESTAMP NOT NULL`` 会被服务端偷偷补 ``DEFAULT CURRENT_TIMESTAMP`` —— 不比。"""
    cols = _matching_columns()
    cols["made_at"] = _live_row("made_at", "timestamp", "NO", "current_timestamp()")
    assert _drift(cols) == [], "时间列的默认值不可靠（取决于服务器开关），不许报"


def test_auto_increment_drift_is_reported():
    cols = _matching_columns()
    cols["id"] = _live_row("id", "bigint(20) unsigned", "NO", None, "")
    out = _drift(cols)
    assert len(out) == 1 and "AUTO_INCREMENT" in out[0], out


def test_index_column_drift_is_reported():
    """同名索引挂在**错的列**上 = 热查询静默退化成 filesort。"""
    out = _drift(_matching_columns(), {"idx_name": "name"})
    assert len(out) == 1 and "索引列" in out[0] and "name,note" in out[0], out


def test_a_missing_index_is_not_drift():
    """缺索引由**补齐那步** ADD（R8 允许），体检不重复报。"""
    assert _drift(_matching_columns(), {}) == []


def test_an_unparseable_expected_definition_is_skipped_not_reported():
    """抠不出来的定义（只有列名，没有类型）宁可不报 —— 假警报会让人关掉体检。"""
    sql = "CREATE TABLE IF NOT EXISTS t (\n  weird,\n  ok INT NOT NULL\n) ENGINE=InnoDB;\n"
    live = _Live(["t"], {"t": {"weird": _live_row("int(11)", "NO"),
                               "ok": _live_row("int(11)", "NO")}})
    assert M.check_schema_drift(live, sql) == []


def test_live_rows_missing_fields_are_reported_as_unjudgeable():
    """线上行缺字段（老桩形状 / 查询别名被改坏）⇒ **报一条"无法判定"**，不是"没漂移"。

    这一条守的是最坏的形态：别名写错时所有列都判不了，若静默跳过，体检就**永远全绿**
    —— 正是本文件存在的理由（见模块 docstring）。
    """
    live = _Live(["t"], {"t": {"name": {"c": "name"}}})
    out = M.check_schema_drift(live, _DRIFT_SQL)
    assert len(out) == 1 and "无法判定" in out[0], out


def test_health_reports_drift_but_does_not_block(monkeypatch):
    """漂移只报不改（R8）：``ok`` 仍为 True，但状态页/日志都看得见。"""
    db._note_health("unknown", "")
    monkeypatch.setattr(M, "check_schema_drift", lambda conn, text: ["t.name: 类型 线上=x"])
    monkeypatch.setattr(db, "get_conn", lambda: _StubDb())
    result = db.migrate()
    assert result["drifted"] == ["t.name: 类型 线上=x"]
    assert db.health()["state"] == "drift" and db.health()["ok"] is True
    assert "只报不改" in db.health()["detail"]


def test_a_broken_drift_check_is_loud_not_silent(monkeypatch):
    """体检自己炸了也不许"假装没漂移" —— 变成一条可见的 ``体检失败``。"""
    def _boom(conn, text):
        raise RuntimeError("information_schema 读不动")

    db._note_health("unknown", "")
    monkeypatch.setattr(M, "check_schema_drift", _boom)
    monkeypatch.setattr(db, "get_conn", lambda: _StubDb())
    result = db.migrate()
    assert result["drifted"] and "体检失败" in result["drifted"][0]
    # 顺带钉住：体检失败**不阻断**启动（表都在，只是判不了漂移）
    assert db.health()["ok"] is True


def test_the_real_schema_reports_no_drift_on_a_freshly_applied_database():
    """★ 用**真** schema.sql（20 表 198 列）跑一遍体检 —— 每一列的归一化一次验完。

    假警报的代价不是"多看一眼"：体检一旦每次都报几十条，人就再也不看它了。
    """
    conn = _StubDb()
    result = M.migrate(conn)
    assert result["drifted"] == [], result["drifted"][:5]
    assert result["created_tables"] and result["added_columns"] == []


# ==================================================== 9. DB 体检状态（D7）


def _raise_unavailable(*_a, **_kw):
    raise db.DbUnavailable("连不上")


def test_health_starts_unknown_and_is_a_copy():
    db._note_health("unknown", "")            # 用例之间复位（模块级单例）
    h = db.health()
    assert h["state"] == "unknown" and h["ok"] is False
    h["state"] = "改副本不该影响本体"
    assert db.health()["state"] == "unknown"


def test_is_available_records_unavailable(monkeypatch):
    """``SELECT 1`` 失败要把结局**记下来** —— 否则只剩一行日志，之后无迹可寻。"""
    db._note_health("unknown", "")
    monkeypatch.setattr(db, "get_conn", _raise_unavailable)
    assert db.is_available() is False
    assert db.health()["state"] == "unavailable"
    assert "连不上" in db.health()["detail"]


def test_migrate_records_ok_with_the_checksum(monkeypatch):
    db._note_health("unknown", "")
    monkeypatch.setattr(db, "get_conn", lambda: _StubDb())
    result = db.migrate()
    assert result["checksum"] == "recorded"
    h = db.health()
    assert h["state"] == "ok" and h["ok"] is True and result["checksum"] in h["detail"]


def test_migrate_records_failure_and_still_raises(monkeypatch):
    """失败**必须**继续抛（调用方决定降级），但结局不许只活在日志里。"""
    db._note_health("unknown", "")

    def _boom():
        raise db.DbError("migration 失败: 表建了一半")

    monkeypatch.setattr(db, "get_conn", _boom)
    with pytest.raises(db.DbError):
        db.migrate()
    h = db.health()
    assert h["state"] == "failed" and h["ok"] is False and "一半" in h["detail"]


def test_health_survives_a_migration_that_blows_up_unexpectedly(monkeypatch):
    """非 ``DbError``（比如 pymysql 的 ProgrammingError）也要落成 ``failed``。"""
    db._note_health("unknown", "")

    def _boom():
        raise ValueError("pymysql 抛了个别的")

    monkeypatch.setattr(db, "get_conn", _boom)
    with pytest.raises(db.DbError, match="migration 失败"):
        db.migrate()
    assert db.health()["state"] == "failed"


def test_the_db_ready_gauge_and_failure_counter_exist_and_move():
    """``db_ready`` / ``db_migration_failures_total`` 必须**预声明**（0 与"没接上"要能分）。"""
    from plugin.core import metrics as MT

    assert "db_ready" in {k.removeprefix("wpp_") for k in MT.snapshot()["gauges"]}
    assert "db_migration_failures_total" in \
        {k.removeprefix("wpp_") for k in MT.snapshot()["counters"]}

    MT.set_db_gauge(1)
    assert MT.get_gauge(MT.GAUGE_DB_READY) == 1.0
    MT.inc_db_migration_failure()
    assert MT.get_counter(MT.COUNTER_DB_MIGRATION_FAILURES) >= 1
    MT.reset_all()
    assert MT.get_gauge(MT.GAUGE_DB_READY) == 0.0
    assert MT.get_counter(MT.COUNTER_DB_MIGRATION_FAILURES) == 0


# ==================================================== 连接探活（2026-10-18 审阅）


def test_get_conn_pings_the_reused_connection_first():
    """★ 只查 ``open`` 不够：服务端踢掉空闲连接后客户端仍报 open，
    下一条业务 SQL 才炸 ``server has gone away``（现场像"偶发失败"）。
    """
    class _RawPing:
        open = True

        def __init__(self) -> None:
            self.pings: list[bool] = []

        def ping(self, reconnect: bool = False) -> None:
            self.pings.append(reconnect)

        def cursor(self):  # pragma: no cover - 本用例不查
            raise AssertionError("不该用到 cursor")

    raw = _RawPing()
    old = getattr(db._local, "conn", None)
    db._local.conn = db.Connection(raw)
    try:
        conn = db.get_conn()
        assert conn is db._local.conn, "同一线程复用连接"
        assert raw.pings == [True], "复用前必须带 reconnect 探活一次"
    finally:
        db._local.conn = old


def test_get_conn_replaces_a_dead_connection(monkeypatch):
    class _Dead:
        open = True

        def ping(self, reconnect: bool = False) -> None:
            raise RuntimeError("server has gone away")

        def close(self) -> None:
            self.closed = True

        def __init__(self) -> None:
            self.closed = False

    dead = _Dead()
    fresh = db.Connection(object())
    monkeypatch.setattr(db, "_connect", lambda: fresh)
    old = getattr(db._local, "conn", None)
    db._local.conn = db.Connection(dead)
    try:
        got = db.get_conn()
        assert got is fresh, "探活失败必须换一条新连接（不是把死的再交出去）"
        assert dead.closed is True
    finally:
        db._local.conn = old


# ==================================================== 审计 JSON 合法性（2026-10-18 审阅）


def test_json_column_helper_always_returns_valid_json():
    """审计列是 ``JSON`` 类型 —— 非 JSON 文本 / 截断到一半都写不进去（整条审计消失）。"""
    import json as _json

    from plugin.data.repo_messages import _json_or_none

    assert _json_or_none(None) is None
    # 普通字符串（vendor 错误体常常就是一行文本）必须被包成 JSON 字符串
    assert _json.loads(_json_or_none("internal server error")) == "internal server error"
    # 本来就是 JSON 的字符串原样保留（解析后同形）
    assert _json.loads(_json_or_none('{"Code":-1}')) == {"Code": -1}
    # dict/list 正常序列化
    assert _json.loads(_json_or_none({"a": [1, 2]})) == {"a": [1, 2]}
    # 超长输入：截断后仍是**合法** JSON，且带 truncated 标记
    big = _json_or_none({"body": "x" * 100_000})
    parsed = _json.loads(big)
    assert parsed["truncated"] is True and parsed["length"] > 60000
    assert len(big) <= 60000
