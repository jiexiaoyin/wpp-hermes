"""数据层回归（schema.sql / 切分 / 补齐 / checksum / 行形状）—— 计划第 7 步的 ``test_db.py``。

## 为什么驱动在 **stub** 上，而不是真库

老板铁律：**测试只走 /tmp stub DB** —— 生产机上 MariaDB 与服务同机，测试连真库
就是拿生产陪练。而且本机（dev 树所在机器）上跑着的 3306/3307 是**别的项目**的
1Panel MariaDB，更不该碰。

所以本文件把 ``migrate()`` 驱动在一个内存 stub 上（``_StubDb``）：它只实现
``query`` / ``execute`` 两个口子，按**真的 DDL 文本**更新自己的 information_schema
视图。断言的是**框架发了什么、发了之后状态对不对**（幂等 / 只 ADD / checksum 生命周期）,
**不是** MySQL 自己会怎么做。真库上的那一段由 ``tools/probe_v2*.py`` 在 .183 跑（V2/V2b）。

## 本文件守的六件事（每条都对应一次真实事故）

1. ``schema.sql`` 是**唯一真源**：16 张表、158 列、33 个索引，且台账三个热索引在文件里
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

import re

import pytest

from plugin.data import db
from plugin.data import migrations as M
from plugin.data import repo_messages as MSG

SCHEMA_TEXT = M.SCHEMA_FILE.read_text(encoding="utf-8")

#: 从文件里数出来的期望值（**改 schema 时必须跟着改** —— 这正是它的作用：
#: 让"顺手加一列"这件事在 diff 里显形，而不是悄悄漂进线上）。
EXPECTED_TABLES = 16
EXPECTED_COLUMNS = 158
EXPECTED_INDEXES = 33


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

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

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


# ==================================================== stub（内存库）

class _StubDb:
    """``migrate()`` 的最小驱动面：**只理解本框架会发的三类语句**。

    它**故意不是** SQL 引擎：真库行为（InnoDB、collation、filesort）由 .183 上的
    ``tools/probe_v2*.py`` 验。这里只回答两个问题：框架发了什么？发完之后状态对不对？
    """

    def __init__(self) -> None:
        self.tables: dict[str, dict] = {}
        self.migrations: dict[int, dict] = {}
        self.executed: list[str] = []
        self.alters: list[str] = []

    # --------------------------------------------------------------- 写
    def execute(self, sql: str, params=()) -> int:
        s = " ".join(sql.split())
        self.executed.append(s)
        low = s.lower()

        if low.startswith("create table"):
            m = M._CREATE_RE.search(s)
            assert m, f"stub 看不懂的 CREATE: {s[:80]}"
            name = m.group(1)
            if name in self.tables and "if not exists" in low:
                return 0               # **IF NOT EXISTS 的语义**：已存在就什么都不做
            open_idx = s.index("(")
            close_idx = M._match_paren(s, open_idx)
            cols, idx = {}, {}
            for item in M._split_top_level(s[open_idx + 1:close_idx]):
                up = item.upper()
                if up.startswith(M._CONSTRAINT_PREFIXES):
                    if not up.startswith("PRIMARY KEY"):
                        im = M._INDEX_NAME_RE.match(item)
                        if im:
                            idx[im.group(1)] = item
                    continue
                cols[item.split()[0].strip("`")] = item
            self.tables.setdefault(name, {"columns": {}, "indexes": {}})
            self.tables[name]["columns"].update(cols)
            self.tables[name]["indexes"].update(idx)
            return 0

        if low.startswith("alter table"):
            self.alters.append(s)
            m = re.match(r"alter table `?(\w+)`? add (.*)$", s, re.I)
            assert m, f"stub 只支持 ALTER TABLE ... ADD: {s[:80]}"
            table, clause = m.group(1), m.group(2)
            im = M._INDEX_NAME_RE.match(clause)
            if im:
                self.tables[table]["indexes"][im.group(1)] = clause
            else:
                col = re.sub(r"^COLUMN\s+", "", clause, flags=re.I)
                self.tables[table]["columns"][col.split()[0].strip("`")] = col
            return 0

        if "wpp_schema_migrations" in low and low.startswith("insert into"):
            version, name, checksum, note = params
            self.migrations[int(version)] = {
                "version": int(version), "name": name, "checksum": checksum, "note": note}
            return 1

        return 0                       # START TRANSACTION / COMMIT / 别的：忽略

    def executemany(self, sql, seq) -> int:
        return 0

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    # --------------------------------------------------------------- 读
    def query(self, sql: str, params=()) -> list[dict]:
        s = " ".join(sql.split())
        if "information_schema.TABLES" in s:
            return [{"t": t} for t in self.tables]
        if "information_schema.COLUMNS" in s:
            return [{"c": c} for c in sorted(self.tables[params[0]]["columns"])]
        if "information_schema.STATISTICS" in s:
            out = []
            for name, defn in sorted(self.tables[params[0]]["indexes"].items()):
                for seq, col in enumerate(_index_columns(defn), start=1):
                    out.append({"n": name, "c": col, "s": seq})
            return out
        if s.lower().startswith("select version, checksum"):
            return [dict(r) for r in self.migrations.values()]
        return []

    def query_one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None


def _index_columns(defn: str) -> list[str]:
    """从 ``KEY idx (a, b)`` / ``UNIQUE KEY uk (a)`` 里抠出列名（stub 自用）。"""
    m = re.search(r"\((.*)\)", defn)
    return [c.strip().strip("`") for c in m.group(1).split(",")] if m else []
