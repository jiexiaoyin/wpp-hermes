"""Migration 框架 —— 对齐 TS 的 ``applySchemaSql`` + ``applyMigrations``，但消除双写陷阱。

## 为什么要重写

TS 侧把 DDL 写了**两份**：``db/schema.sql`` 与 ``src/storage/db/mysql.ts`` 的行内
DDL，并在注释里互相提醒「改一处必改另一处」。这是个陷阱 —— 它已经把
``wpp_svrid_mapping`` 写歪过一次（两边列不一致，谁先跑谁的）。

Python 侧改成**单向推导**：

    schema.sql  ──解析──▶  TableSpec  ──diff──▶  ALTER TABLE ADD ...
        （唯一真源）          （期望态）          （只加不改，见 R8）

于是「线上表结构」和「schema.sql」不可能漂移 —— 每次启动都对一遍。

## 三条防线（都对应真实事故）

1. **`_apply_schema_sql` 切出 0 条语句就 raise** —— 对应 TS v1.6.1–1.6.5 的
   `applySchemaSql` **静默失效**（文件在、路径错、切出空、无报错、以为建好了）。
   宁可启动时报错，也不要「以为建好了」。
2. **`ensure_schema_complete` 只 ADD 不 MODIFY/DROP** —— 对应 R8「migration 破坏数据」。
   少列能补，改列型/删列要人肉决策，脚本无权。
3. **checksum 不符只 warn + 留痕，不阻断** —— ``schema.sql`` 被改动是**正常的开发行为**，
   不该阻止启动；但必须留下痕迹，否则「线上表是谁建的」又变成悬案。

## 适用范围

``.183`` 是**全新空库** ⇒ legacy 表建空表即可，**不做 ETL**（计划原文）。
因此 ``_MIGRATIONS`` 为空 —— 能用 DDL 表达的（加列/加索引）一律走
``ensure_schema_complete``，那是幂等且可重建的；``_MIGRATIONS`` 只留
**不可由 DDL 重建的一次性动作**（数据回填、改名搬值）。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

logger = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

#: 框架自有的版本表 —— **不属于** schema.sql 的 16 张表
MIGRATION_TABLE = "wpp_schema_migrations"

#: 用 version=0 这一行存 ``schema.sql`` 自身的校验和（不是一次 migration）
SCHEMA_VERSION_ROW = 0


class SchemaError(RuntimeError):
    """``schema.sql`` 本身有问题（读不到 / 切不出语句 / 表数对不上）。"""


# --------------------------------------------------------------------------- 切分


def strip_comments(sql_text: str) -> str:
    """剥掉 ``--`` 注释（整行与行尾），保留换行。

    ★ **必须在切分/解析之前做**。踩过的坑（2026-10-01）：``parse_schema`` 曾直接在
    原文上按逗号切分，而**行尾注释里的逗号也算顶层逗号** —— 于是

        msg_id VARCHAR(128),              -- wechatpadpro msg id (uuid)
        new_msg_id VARCHAR(128),

    被切成 ``"msg_id VARCHAR(128)"`` + ``"-- wechatpadpro msg id (uuid)\\n new_msg_id
    VARCHAR(128)"``。后一项 ``split()[0]`` 得到 ``"--"``，``new_msg_id`` **没被认成
    列** ⇒ ``ensure_schema_complete`` 便去 ``ADD COLUMN new_msg_id`` ⇒
    ``ERROR 1060 Duplicate column name``。切分器与解析器**必须共用**这一层。

    注释剥除与字符串识别在**同一个状态机**里 —— 反过来做（先剥注释再找字符串）
    会让注释里的单引号翻转字符串状态、把后面整段代码吃光。
    """
    out: list[str] = []
    i = 0
    n = len(sql_text)
    in_str = False

    while i < n:
        ch = sql_text[i]

        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:               # 反斜杠转义
                out.append(sql_text[i + 1])
                i += 2
                continue
            if ch == "'":
                if i + 1 < n and sql_text[i + 1] == "'":   # '' 转义
                    out.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue

        if ch == "'":
            in_str = True
            out.append(ch)
            i += 1
            continue

        if ch == "-" and i + 1 < n and sql_text[i + 1] == "-":
            while i < n and sql_text[i] != "\n":       # 吞到行尾，\n 留给下一轮
                i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def split_statements(sql_text: str) -> list[str]:
    """把 SQL 脚本切成单条语句（已剥注释）。"""
    text = strip_comments(sql_text)
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(text)
    in_str = False

    while i < n:
        ch = text[i]
        if in_str:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:                       # 允许最后一条不带分号
        out.append(tail)
    return out


# --------------------------------------------------------------------------- 解析


@dataclass
class TableSpec:
    """schema.sql 里一张表的**期望态**。"""

    name: str
    columns: list[tuple[str, str]] = field(default_factory=list)   # (列名, 完整定义)
    indexes: list[tuple[str, str]] = field(default_factory=list)   # (索引名, 完整定义)


_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?\s*\(",
    re.IGNORECASE,
)

#: 这些开头是**表级约束/索引**，不是列定义
_CONSTRAINT_PREFIXES = (
    "PRIMARY KEY", "UNIQUE KEY", "UNIQUE INDEX", "KEY", "INDEX",
    "FULLTEXT", "SPATIAL", "CONSTRAINT", "FOREIGN KEY", "CHECK",
)

_INDEX_NAME_RE = re.compile(
    r"^(?:UNIQUE\s+)?(?:KEY|INDEX)\s+`?(\w+)`?",
    re.IGNORECASE,
)

#: 合法标识符 —— 用来把「切分错位」当场变成报错，而不是静默漏掉一列
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _split_top_level(body: str) -> list[str]:
    """按顶层逗号切分（忽略括号内的逗号：``DECIMAL(6,4)`` / ``ENUM('a','b')``）。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    i = 0
    while i < len(body):
        ch = body[i]
        if in_str:
            buf.append(ch)
            if ch == "'":
                if i + 1 < len(body) and body[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _match_paren(text: str, open_idx: int) -> int:
    """返回与 ``text[open_idx] == '('`` 配对的 ``)`` 下标；找不到返回 -1。"""
    depth = 0
    in_str = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def parse_schema(sql_text: str) -> dict[str, TableSpec]:
    """从 ``schema.sql`` 文本反推每张表的期望列与索引。

    ⚠️ 先 ``strip_comments`` —— 见 ``strip_comments`` 的 docstring（行尾注释里的
    逗号会把列定义切碎，导致漏识别 + 重复 ADD COLUMN）。
    """
    text = strip_comments(sql_text)
    tables: dict[str, TableSpec] = {}
    for m in _CREATE_RE.finditer(text):
        name = m.group(1)
        open_idx = text.index("(", m.start())
        close_idx = _match_paren(text, open_idx)
        if close_idx < 0:
            raise SchemaError(f"表 {name}: 括号不配对（schema.sql 语法坏了）")
        body = text[open_idx + 1:close_idx]
        spec = TableSpec(name=name)
        for item in _split_top_level(body):
            upper = item.upper()
            if upper.startswith(_CONSTRAINT_PREFIXES):
                if not upper.startswith("PRIMARY KEY"):
                    im = _INDEX_NAME_RE.match(item)
                    if im:
                        spec.indexes.append((im.group(1), item))
                continue
            col_name = item.split()[0].strip("`")
            if not _IDENT_RE.match(col_name):
                # 走到这儿说明注释没剥干净 / 切分错位 —— 立刻炸，别静默漏列
                raise SchemaError(
                    f"表 {name}: 解析出的列名 {col_name!r} 不是合法标识符"
                    f"（schema.sql 的注释剥除或逗号切分坏了）: {item[:80]!r}"
                )
            spec.columns.append((col_name, item))
        if not spec.columns:
            raise SchemaError(f"表 {name}: 解析出 0 列（schema.sql 语法坏了）")
        names = [c for c, _ in spec.columns]
        if len(set(names)) != len(names):
            dupes = sorted({c for c in names if names.count(c) > 1})
            raise SchemaError(f"表 {name}: 解析出重复列名 {dupes}（切分错位）")
        tables[name] = spec
    return tables


def schema_checksum(sql_text: str) -> str:
    """``schema.sql`` 的内容哈希（换行统一，避免 CRLF 误报）。"""
    norm = sql_text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- 步骤


def _ensure_migration_table(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
          version INT NOT NULL PRIMARY KEY,
          name VARCHAR(128) NOT NULL DEFAULT '',
          checksum CHAR(64) NULL,
          note VARCHAR(255) NULL,
          applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    # 老库可能缺列 —— 只加不改（R8）
    have = _existing_columns(conn, MIGRATION_TABLE)
    for col, ddl in (
        ("name", "ADD COLUMN name VARCHAR(128) NOT NULL DEFAULT ''"),
        ("checksum", "ADD COLUMN checksum CHAR(64) NULL"),
        ("note", "ADD COLUMN note VARCHAR(255) NULL"),
    ):
        if col not in have:
            conn.execute(f"ALTER TABLE {MIGRATION_TABLE} {ddl}")


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.query(
        "SELECT COLUMN_NAME AS c FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return {r["c"] for r in rows}


def _existing_indexes(conn, table: str) -> dict[str, str]:
    rows = conn.query(
        "SELECT INDEX_NAME AS n, COLUMN_NAME AS c, SEQ_IN_INDEX AS s "
        "FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
        "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
        (table,),
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["n"], []).append(r["c"])
    return {k: ",".join(v) for k, v in out.items()}


def _apply_schema_sql(conn, sql_text: str | None = None) -> list[str]:
    """执行 ``schema.sql`` 的每一条语句，返回新建的表名列表。

    ★ 三条 raise 都是**防静默失效**（见模块 docstring 防线 1）。
    """
    text = SCHEMA_FILE.read_text(encoding="utf-8") if sql_text is None else sql_text
    if not text.strip():
        raise SchemaError(f"schema.sql 是空的: {SCHEMA_FILE}")

    statements = split_statements(text)
    if not statements:
        raise SchemaError(
            f"schema.sql 非空却切出 0 条语句 —— 切分器或注释剥除坏了: {SCHEMA_FILE}"
        )

    expected = [m.group(1) for m in _CREATE_RE.finditer(text)]
    if not expected:
        raise SchemaError(f"schema.sql 里找不到任何 CREATE TABLE: {SCHEMA_FILE}")

    before = _existing_tables(conn)
    for stmt in statements:
        conn.execute(stmt)
    after = _existing_tables(conn)

    missing = [t for t in expected if t not in after]
    if missing:
        # 语句跑了却没建出来 —— 比「没跑」更危险，必须炸
        raise SchemaError(f"schema.sql 执行后仍缺表: {missing}")
    return [t for t in expected if t not in before]


def _existing_tables(conn) -> set[str]:
    rows = conn.query(
        "SELECT TABLE_NAME AS t FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE()"
    )
    return {r["t"] for r in rows}


def ensure_schema_complete(conn, sql_text: str | None = None) -> dict[str, list[str]]:
    """把线上表结构补齐到 ``schema.sql`` 的期望态。

    **只 ADD，绝不 MODIFY / DROP / 改型**（R8）。返回 ``{"columns": [...], "indexes": [...]}``
    —— 两个列表都形如 ``"wpp_messages.delivery_status"``，便于测试断言与日志审计。
    """
    text = SCHEMA_FILE.read_text(encoding="utf-8") if sql_text is None else sql_text
    specs = parse_schema(text)
    live_tables = _existing_tables(conn)
    added_cols: list[str] = []
    added_idx: list[str] = []

    for name, spec in specs.items():
        if name not in live_tables:
            continue                      # 新表由 _apply_schema_sql 建，这里不管
        have_cols = _existing_columns(conn, name)
        for col, defn in spec.columns:
            if col in have_cols:
                continue
            conn.execute(f"ALTER TABLE {name} ADD COLUMN {defn}")
            added_cols.append(f"{name}.{col}")
            logger.info("[WPP] schema 补齐列: %s.%s", name, col)

        have_idx = _existing_indexes(conn, name)
        for idx_name, defn in spec.indexes:
            if idx_name in have_idx:
                continue
            conn.execute(f"ALTER TABLE {name} ADD {defn}")
            added_idx.append(f"{name}.{idx_name}")
            logger.info("[WPP] schema 补齐索引: %s.%s", name, idx_name)

    return {"columns": added_cols, "indexes": added_idx}


def _applied_versions(conn) -> dict[int, str | None]:
    rows = conn.query(f"SELECT version, checksum FROM {MIGRATION_TABLE}")
    return {int(r["version"]): r["checksum"] for r in rows}


def _record(conn, version: int, name: str, checksum: str | None, note: str | None = None) -> None:
    conn.execute(
        f"INSERT INTO {MIGRATION_TABLE} (version, name, checksum, note) VALUES (%s, %s, %s, %s) "
        f"ON DUPLICATE KEY UPDATE name = VALUES(name), checksum = VALUES(checksum), "
        f"note = VALUES(note), applied_at = CURRENT_TIMESTAMP",
        (version, name, checksum, note),
    )


def _check_checksum(conn, actual: str) -> str:
    """比对 ``schema.sql`` 的哈希；不符只 warn + 留痕，**不阻断**（防线 3）。"""
    applied = _applied_versions(conn)
    recorded = applied.get(SCHEMA_VERSION_ROW)
    if recorded is None:
        _record(conn, SCHEMA_VERSION_ROW, "schema.sql", actual)
        return "recorded"
    if recorded != actual:
        logger.warning(
            "[WPP] schema.sql 变了（recorded=%s actual=%s）—— 表结构已按新文件补齐，"
            "若曾手工改库请核对",
            (recorded or "")[:12], actual[:12],
        )
        _record(
            conn, SCHEMA_VERSION_ROW, "schema.sql", actual,
            note=f"checksum changed from {(recorded or '')[:12]}",
        )
        return "mismatch"
    return "ok"


@dataclass(frozen=True)
class Migration:
    """一次性动作。

    只在**无法用 DDL 表达**时才写这里（数据回填 / 改名搬值）；能写成
    ``CREATE/ALTER`` 的一律进 ``schema.sql``（那里是幂等且可重建的）。
    ``fn(conn)`` 必须自己保证可重复执行。
    """

    version: int
    name: str
    fn: Callable[[object], None]


#: 空得有理由：``.183`` 是新库，没有历史数据要回填。
#: 模板（将来要加时照抄，version 从 1 起，禁止复用已用过的号）：
#:
#:     def _backfill_xxx(conn):
#:         conn.execute("UPDATE wpp_messages SET from_wxid = '' WHERE from_wxid IS NULL")
#:
#:     _MIGRATIONS += (Migration(1, "backfill_from_wxid", _backfill_xxx),)
_MIGRATIONS: tuple[Migration, ...] = ()


def migrate(conn) -> dict:
    """建表 / 补齐 / 跑一次性 migration / 校验 checksum。

    幂等：连跑两次第二次应当**零新建、零补齐、零应用**（V2 的判据）。
    由 ``db.migrate()`` 在 ``adapter.connect()`` 之前调用；调用方负责
    「失败只 warn 不阻断」的降级策略。
    """
    text = SCHEMA_FILE.read_text(encoding="utf-8") if SCHEMA_FILE.exists() else ""
    if not text.strip():
        raise SchemaError(f"schema.sql 缺失或为空: {SCHEMA_FILE}")

    _ensure_migration_table(conn)
    created = _apply_schema_sql(conn, text)
    ensured = ensure_schema_complete(conn, text)

    applied_before = _applied_versions(conn)
    applied_now: list[str] = []
    for mig in sorted(_MIGRATIONS, key=lambda m: m.version):
        if mig.version in applied_before:
            continue
        conn.execute("START TRANSACTION")
        try:
            mig.fn(conn)
            _record(conn, mig.version, mig.name, None)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        applied_now.append(f"{mig.version}:{mig.name}")

    checksum_state = _check_checksum(conn, schema_checksum(text))
    conn.commit()

    return {
        "created_tables": created,
        "added_columns": ensured["columns"],
        "added_indexes": ensured["indexes"],
        "applied_migrations": applied_now,
        "checksum": checksum_state,
        "expected_tables": len(parse_schema(text)),
    }


def expected_table_names(sql_text: str | None = None) -> list[str]:
    """``schema.sql`` 声称要建的表名（供测试断言「文件里写的都真存在」）。"""
    text = SCHEMA_FILE.read_text(encoding="utf-8") if sql_text is None else sql_text
    return [m.group(1) for m in _CREATE_RE.finditer(strip_comments(text))]


def iter_indexes(sql_text: Iterable[str] | None = None) -> list[tuple[str, str]]:
    """``[(表名, 索引名), ...]`` —— 给测试用（TS 曾漏搬 5 个热查询索引）。"""
    text = SCHEMA_FILE.read_text(encoding="utf-8") if sql_text is None else "".join(sql_text)
    return [(name, idx) for name, spec in parse_schema(text).items() for idx, _ in spec.indexes]
