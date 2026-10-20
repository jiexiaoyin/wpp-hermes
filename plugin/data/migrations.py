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

``真机`` 是**全新空库** ⇒ legacy 表建空表即可，**不做 ETL**（计划原文）。
因此 ``_MIGRATIONS`` 为空 —— 能用 DDL 表达的（加列/加索引）一律走
``ensure_schema_complete``，那是幂等且可重建的；``_MIGRATIONS`` 只留
**不可由 DDL 重建的一次性动作**（数据回填、改名搬值）。
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

#: 框架自有的版本表 —— **不属于** schema.sql 的 20 张表
MIGRATION_TABLE = "wpp_schema_migrations"

#: 用 version=0 这一行存 ``schema.sql`` 自身的校验和（不是一次 migration）
SCHEMA_VERSION_ROW = 0


class SchemaError(RuntimeError):
    """``schema.sql`` 本身有问题（读不到 / 切不出语句 / 表数对不上）。"""


def _dialect(conn) -> str:
    """连接方言；测试桩没有 ``dialect`` ⇒ 按 MySQL 处理（旧行为）。"""
    return str(getattr(conn, "dialect", "mysql") or "mysql")


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
    #: 主键列（小写）。**体检要用**：主键列在服务端恒为 ``NOT NULL``，而 schema.sql
    #: 里写 ``PRIMARY KEY (id)`` 时列定义本身往往**不写 NOT NULL** ⇒ 不认这条就会
    #: 报假漂移（2026-10-02 在 真机 真库上实测到 7 条）。
    pk: list[str] = field(default_factory=list)


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
                if upper.startswith("PRIMARY KEY"):
                    spec.pk.extend(_index_cols(item))     # 主键列 ⇒ 体检时视为 NOT NULL
                else:
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
    if _dialect(conn) == "sqlite":
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} ("
            "version INTEGER NOT NULL PRIMARY KEY,"
            "name TEXT NOT NULL DEFAULT '',"
            "checksum TEXT NULL,"
            "note TEXT NULL,"
            "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        have = _existing_columns(conn, MIGRATION_TABLE)
        for col, ddl in (
            ("name", "ADD COLUMN name TEXT NOT NULL DEFAULT ''"),
            ("checksum", "ADD COLUMN checksum TEXT NULL"),
            ("note", "ADD COLUMN note TEXT NULL"),
        ):
            if col not in have:
                conn.execute(f"ALTER TABLE {MIGRATION_TABLE} {ddl}")
        return

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


def _existing_column_specs(conn, table: str) -> dict[str, dict]:
    """列名 → 线上定义（``type`` / ``nullable`` / ``dflt`` / ``extra``）—— 体检用。

    ⚠️ 与 :func:`_existing_columns`（只要名字）**故意分开**：补齐逻辑不需要这些字段，
    而多查一次 information_schema 是有代价的；更重要的是"只要名字"这条判据本身
    不该被体检的口径改动牵连。
    """
    rows = conn.query(
        "SELECT COLUMN_NAME AS c, COLUMN_TYPE AS type, IS_NULLABLE AS nullable, "
        "COLUMN_DEFAULT AS dflt, EXTRA AS extra "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return {str(r.get("c")): dict(r) for r in rows if r.get("c")}


def _existing_columns(conn, table: str) -> set[str]:
    if _dialect(conn) == "sqlite":
        rows = conn.query(f"PRAGMA table_info({table})")
        return {str(r["name"]) for r in rows if r.get("name")}
    rows = conn.query(
        "SELECT COLUMN_NAME AS c FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return {r["c"] for r in rows}


def _existing_indexes(conn, table: str) -> dict[str, str]:
    if _dialect(conn) == "sqlite":
        out: dict[str, list[str]] = {}
        for idx in conn.query(f"PRAGMA index_list({table})"):
            name = str(idx.get("name") or "")
            if not name:
                continue
            cols = [str(r.get("name") or "")
                    for r in conn.query(f"PRAGMA index_info({name})")]
            out[name] = [c for c in cols if c]
        return {k: ",".join(v) for k, v in out.items()}
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


# --- D6：定义漂移体检（**只报不改**，R8）--------------------------------------
#
# ``ensure_schema_complete`` 的判据是**列名在不在**。名字在、定义变了（``VARCHAR(64)``
# → ``VARCHAR(128)``、``NOT NULL`` 被去掉、``DECIMAL(6,4)`` 改了精度、索引换了列），
# 它一律不吭声 —— 而这类漂移的后果全是慢性的：写 100 字符进 ``VARCHAR(64)`` 被截断、
# 空值从"写不进来"变成"写得进来"、热查询索引挂在错的列上退化成 filesort。
# 所以这里补一层**体检**：只报，不改（R8：绝不 MODIFY/DROP）。

#: 这些类型的括号里是服务端**自己补的显示宽度**（``INT`` → ``int(11)``、
#: ``BIGINT`` → ``bigint(20)``），归一化要抹掉 —— 否则每次体检都报一堆假漂移。
_WIDTH_NOISE = frozenset({
    "int", "bigint", "smallint", "tinyint", "mediumint", "float", "double",
    "text", "tinytext", "mediumtext", "longtext", "blob", "json", "date",
})

#: 类型部分到此为止的关键词（后面的都是约束/属性）
_DEFN_STOP_RE = re.compile(
    r"\b(NOT|NULL|DEFAULT|AUTO_INCREMENT|PRIMARY|UNIQUE|COMMENT|COLLATE|CHARACTER|"
    r"ON|REFERENCES|CHECK|GENERATED|VIRTUAL|STORED)\b",
    re.IGNORECASE,
)

#: 时间列**不比默认值**：``explicit_defaults_for_timestamp`` 关着时服务端会给
#: ``TIMESTAMP NOT NULL`` 偷偷补一个 ``DEFAULT CURRENT_TIMESTAMP``，
#: 拿它判漂移只会制造假警报（这个开关不由我们控制）。
_UNRELIABLE_DEFAULT_TYPES = frozenset({"timestamp", "datetime"})

#: 索引列的长度前缀在 ``information_schema.STATISTICS`` 里是 ``SUB_PART``，
#: 本体检**不取**那一列 ⇒ 前缀长度变化不报（见 :func:`_index_cols`）。
_INDEX_COL_SUFFIX_RE = re.compile(r"\(\s*\d+\s*\)$")

#: 类型别名等价表（**服务端说的名字与我们写的不同**，不是漂移）。
#:
#: ``json``：MariaDB 的 ``JSON`` 是 ``LONGTEXT`` 的**别名**（加一条 ``json_valid``
#: CHECK），``information_schema.COLUMNS.COLUMN_TYPE`` 报回来就是 ``longtext``。
#: 2026-10-02 在 ``真机`` 真库上实测：``schema.sql`` 写 ``config_json JSON``，
#: 线上报 ``longtext`` ⇒ 一次体检 4 条假漂移。
#: 代价：真的把 ``json`` 写成 ``text``（丢掉 ``json_valid`` 校验）**报不出来** ——
#: 接受，因为 R8 本来就不许我们 MODIFY，报了也只能看（而且 ``u`` 前缀的变化仍会报）。
_TYPE_ALIASES = {"json": "longtext"}

#: 判定漂移所需的线上字段（:func:`_existing_column_specs` 的 SELECT 别名）。
#: 缺任何一个就没法判定 —— 见 :func:`check_schema_drift` 里那条**显式**分支：
#: 不判 ≠ 没漂移，必须报出来（否则"查询别名写错 ⇒ 体检永远全绿"）。
_LIVE_COL_FIELDS = ("type", "nullable", "dflt", "extra")


def _norm_type(raw: str) -> str | None:
    """把一段列类型归一成签名；看不懂就返回 ``None``（**宁可不报，也不假报**）。

    归一化三件事：抹掉服务端补的显示宽度、抹掉空白与反引号、走一遍
    :data:`_TYPE_ALIASES`（``json`` ≡ ``longtext`` —— MariaDB 的别名）。
    """
    text = " ".join(str(raw or "").split())
    stop = _DEFN_STOP_RE.search(text)
    if stop:
        text = text[:stop.start()]
    text = text.replace("`", "").replace(" ", "").lower()
    if not text:
        return None
    base = text.split("(", 1)[0]
    if base in _WIDTH_NOISE:
        text = re.sub(r"\(\d+\)", "", text)      # int(11) → int；bigint(20) → bigint
    alias = _TYPE_ALIASES.get(text.split("(", 1)[0])
    if alias is not None:
        text = alias + text[len(text.split("(", 1)[0]):]
    return text or None


def _live_has_default(got: dict) -> bool:
    """线上这一列**有没有**默认值 —— 把 MariaDB 的 ``'NULL'`` 也算作"没有"。

    ⚠️ 这是 MariaDB 与 MySQL 的一个真实差异（2026-10-02 在 ``真机`` 实测）：
    列定义写 ``display_name VARCHAR(128)``（即 ``DEFAULT NULL``）时，
    MySQL 的 ``COLUMN_DEFAULT`` 是 SQL ``NULL``，而 **MariaDB 回来的是字符串
    ``"NULL"``** ⇒ ``is not None`` 恒真 ⇒ 一次体检 66 条假"线上有默认值"。
    两者语义相同（默认就是 NULL = 没默认），所以这里一起当"没有"。

    代价：真写 ``DEFAULT 'NULL'``（字符串字面量）也判成"没有" —— 这种列在本仓不存在，
    且真出现了也只是**漏报**（本体检本来就宁漏不假报）。
    """
    raw = got.get("dflt")
    if raw is None:
        return False
    return str(raw).strip().upper() != "NULL"


def _expected_col_parts(defn: str) -> dict[str, Any] | None:
    """从 ``schema.sql`` 的列定义里抠出可比对的四件事；抠不出来返回 ``None``。

    ``defn`` 形如 ``"id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT"``（含列名）。
    """
    parts = str(defn or "").split(None, 1)
    if len(parts) < 2:
        return None
    rest = parts[1]
    type_sig = _norm_type(rest)
    if type_sig is None:
        return None
    up = rest.upper()
    base = re.sub(r"(unsigned|zerofill)$", "", type_sig.split("(", 1)[0])
    return {
        "type": type_sig,
        # 行内主键（``id INT AUTO_INCREMENT PRIMARY KEY``）⇒ 服务端恒报 NOT NULL，
        # 而 defn 里没写 NOT NULL ⇒ 不认这条会报假漂移（表级 PRIMARY KEY 由
        # ``TableSpec.pk`` 在 :func:`check_schema_drift` 里补上）。
        "not_null": bool(re.search(r"\bNOT\s+NULL\b", up)) or "PRIMARY KEY" in up,
        "has_default": bool(re.search(r"\bDEFAULT\s+(?!NULL\b)\S", up)),
        "auto_inc": "AUTO_INCREMENT" in up,
        "on_update": bool(re.search(r"\bON\s+UPDATE\b", up)),
        "default_reliable": base not in _UNRELIABLE_DEFAULT_TYPES,
    }


def _index_cols(defn: str) -> list[str]:
    """从 ``KEY idx (a, b)`` 里抠出列名列表（**丢掉长度前缀**：``a(10)`` → ``a``）。"""
    m = re.search(r"\((.*)\)", str(defn or ""), re.S)
    if not m:
        return []
    out: list[str] = []
    for part in _split_top_level(m.group(1)):
        col = part.strip().strip("`").strip().lower()
        col = _INDEX_COL_SUFFIX_RE.sub("", col).strip()
        if col:
            out.append(col)
    return out


def check_schema_drift(conn, sql_text: str | None = None) -> list[str]:
    """体检：线上定义与 ``schema.sql`` 不一致的地方（``["表.列: 说明", ...]``）。

    **只报不改**（R8）：migration 框架无权改列型 / 删列 / 重建索引，所以这里的作用
    是把"看不见的漂移"变成一行行能被 grep 到的字符串，挂进 ``migrate()`` 的返回值与
    ``db.health()``。四类判据（都是**单向**的保守判据，宁可漏报不假报）：

    1. 类型签名不同（``varchar(64)`` vs ``varchar(128)``，显示宽度已归一）
    2. ``NOT NULL`` 与线上可空性不一致
    3. ``DEFAULT`` 有无不一致（``TIMESTAMP``/``DATETIME`` 不比 —— 见常量注释）
    4. 同名索引的**列清单**不同（挂在错的列上 = 热查询静默退化成 filesort）

    看不懂的列定义一律跳过（:func:`_expected_col_parts` 返回 ``None``）；
    线上行**整块**缺字段（老形状的测试桩 / 别名被改坏）时，该表**一律不判**并报一条
    "无法判定" —— 注意这与"跳过"不同：跳过会被读成"没漂移"，而这一条是**红的**。
    """
    text = SCHEMA_FILE.read_text(encoding="utf-8") if sql_text is None else sql_text
    specs = parse_schema(text)
    live_tables = _existing_tables(conn)
    out: list[str] = []

    for name, spec in sorted(specs.items()):
        if name not in live_tables:
            continue
        live_cols = _existing_column_specs(conn, name)
        pk_cols = {c.strip().strip("`").lower() for c in spec.pk}
        usable = bool(live_cols) and all(
            k in row for row in live_cols.values() for k in _LIVE_COL_FIELDS)
        if not usable:
            # 读回来的行缺字段 ⇒ 判不了。**不许静默跳过**：那正好是"查询别名写错 /
            # 桩形状过时 ⇒ 体检永远全绿"这种最坏的形态（本文件生存的全部理由）。
            out.append(
                f"{name}: 线上列定义字段不全（缺 {'/'.join(_LIVE_COL_FIELDS)}），"
                "本次无法判定漂移 —— 请检查 information_schema 查询的别名")
        for col, defn in spec.columns:
            want = _expected_col_parts(defn)
            got = live_cols.get(col)
            if not usable or want is None or not got:
                continue                       # 抠不出来 / 判不了：上面已报或交给补齐那步
            got_type = _norm_type(got.get("type", ""))
            if got_type is not None and got_type != want["type"]:
                out.append(f"{name}.{col}: 类型 线上={got_type} 期望={want['type']}")
            want_not_null = want["not_null"] or col.strip().strip("`").lower() in pk_cols
            got_null = str(got.get("nullable", "")).upper() == "NO"
            if got_null != want_not_null:
                out.append(
                    f"{name}.{col}: 可空性 线上={'NOT NULL' if got_null else 'NULL'} "
                    f"期望={'NOT NULL' if want_not_null else 'NULL'}")
            if want["default_reliable"]:
                got_default = _live_has_default(got)
                if got_default != want["has_default"]:
                    out.append(
                        f"{name}.{col}: 默认值 线上="
                        f"{'有' if got_default else '无'} 期望={'有' if want['has_default'] else '无'}")
            got_extra = str(got.get("extra", "")).lower()
            if ("auto_increment" in got_extra) != want["auto_inc"]:
                out.append(f"{name}.{col}: AUTO_INCREMENT 线上={got_extra or '无'}")

        live_idx = _existing_indexes(conn, name)
        for idx_name, defn in spec.indexes:
            want_cols = _index_cols(defn)
            got_cols = [c.strip().strip("`").lower()
                        for c in str(live_idx.get(idx_name, "")).split(",") if c.strip()]
            if not got_cols or want_cols == got_cols:
                continue                       # 缺失由补齐那步 ADD；一致就没什么可报
            out.append(f"{name}.{idx_name}: 索引列 线上={','.join(got_cols)} "
                       f"期望={','.join(want_cols)}")
    return out


def _apply_schema_sql(conn, sql_text: str | None = None) -> list[str]:
    """执行 ``schema.sql`` 的每一条语句，返回新建的表名列表。

    ★ 三条 raise 都是**防静默失效**（见模块 docstring 防线 1）。
    """
    text = SCHEMA_FILE.read_text(encoding="utf-8") if sql_text is None else sql_text
    if not text.strip():
        raise SchemaError(f"schema.sql 是空的: {SCHEMA_FILE}")

    if _dialect(conn) == "sqlite":
        from .sqlite_dialect import schema_to_sqlite

        statements = schema_to_sqlite(text)
    else:
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
    if _dialect(conn) == "sqlite":
        rows = conn.query(
            "SELECT name AS t FROM sqlite_master WHERE type = 'table'")
        return {r["t"] for r in rows}
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
    is_sqlite = _dialect(conn) == "sqlite"
    if is_sqlite:
        from .sqlite_dialect import convert_schema_fragment

    for name, spec in specs.items():
        if name not in live_tables:
            continue                      # 新表由 _apply_schema_sql 建，这里不管
        have_cols = _existing_columns(conn, name)
        for col, defn in spec.columns:
            if col in have_cols:
                continue
            ddl = convert_schema_fragment(defn) if is_sqlite else defn
            conn.execute(f"ALTER TABLE {name} ADD COLUMN {ddl}")
            added_cols.append(f"{name}.{col}")
            logger.info("[WPP] schema 补齐列: %s.%s", name, col)

        have_idx = _existing_indexes(conn, name)
        for idx_name, defn in spec.indexes:
            # SQLite 索引名全库唯一 ⇒ 物理名带表前缀（与 schema_to_sqlite 同口径）。
            physical = f"{name}__{idx_name}" if is_sqlite else idx_name
            if physical in have_idx:
                continue
            if is_sqlite:
                # spec 里的 defn 形如 ``INDEX idx (a, b)``；SQLite 没有
                # ``ALTER TABLE ADD INDEX``，必须独立 ``CREATE INDEX``。
                m = re.match(
                    r"(UNIQUE\s+)?(?:KEY|INDEX)\s+[A-Za-z_][A-Za-z0-9_]*\s*\(([^)]*)\)",
                    defn, re.IGNORECASE)
                if not m:
                    logger.warning("[WPP] 无法转换的索引定义，跳过: %s.%s %r",
                                   name, idx_name, defn)
                    continue
                uniq = "UNIQUE " if m.group(1) else ""
                cols = re.sub(r"\s+", "", m.group(2))
                conn.execute(
                    f"CREATE {uniq}INDEX IF NOT EXISTS {physical} ON {name} ({cols})")
            else:
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

    ⚠️ **DDL 不受事务保护** —— 这条在 2026-10-02 被 ``真机`` 真库当场上了一课：

    旧写法（裸 ``START TRANSACTION``）固然是坏的（``execute`` 会把它自己提交掉），
    但换成真事务之后，"中途炸掉 ⇒ 半套 schema 被回滚"**依然不成立**：MariaDB/MySQL 的
    ``ALTER``/``CREATE`` 会**隐式提交**当前事务，回滚命令发出去也追不回那半条 DDL。
    所以 ``conn.transaction()`` 只护得住 **DML**（回填 / 搬值 —— 也就是这个类
    docstring 第一条说的那类动作）。

    真库上验到的契约因此是**弱一条**的、但可执行的：

    1. 炸掉的版本**不会被记录** ⇒ 下次 ``migrate()`` 会重跑（这条更要紧：漏记 = 永不重跑）；
    2. 所以 ``fn`` **必须幂等**（``ADD COLUMN IF NOT EXISTS`` / ``UPDATE ... WHERE``）——
       "没做完"是允许的，"重跑接不上"不是。

    上面两个性质由 ``plugin/tests/test_db_live.py`` 在真库上逐条钉住。
    """

    version: int
    name: str
    fn: Callable[[object], None]


#: 空得有理由：``真机`` 是新库，没有历史数据要回填。
#: 模板（将来要加时照抄，version 从 1 起，禁止复用已用过的号）：
#:
#:     def _backfill_xxx(conn):
#:         conn.execute("UPDATE wpp_messages SET from_wxid = '' WHERE from_wxid IS NULL")
#:
#:     _MIGRATIONS += (Migration(1, "backfill_from_wxid", _backfill_xxx),)
_MIGRATIONS: tuple[Migration, ...] = ()


def migrate(conn) -> dict:
    """建表 / 补齐 / 跑一次性 migration / 校验 checksum / 体检漂移。

    幂等：连跑两次第二次应当**零新建、零补齐、零应用**（V2 的判据）。
    由 ``db.migrate()`` 在 ``adapter.connect()`` 之前调用；调用方负责
    「失败只 warn 不阻断」的降级策略。

    ★ D4（2026-10-02 修）：每条 migration 的整体性改由 ``conn.transaction()`` 兜
    （旧写法发的是裸 ``START TRANSACTION``，在 ``autocommit=False`` 下会被
    ``Connection.execute`` 自己提交掉 ⇒ 中途炸掉留半套，见 ``db.Connection.transaction``）。
    ``conn`` 可能是不带事务语义的测试桩，所以走 :func:`_atomic`。
    ⚠️ **只护 DML** —— DDL 在 MariaDB 上隐式提交，回滚不掉；幂等性由 body 自己保证
    （真库实测见 :class:`Migration` 的 docstring 与 ``tests/test_db_live.py``）。

    ★ D6：返回多一个 ``drifted``（[``"表.列: 说明"``]）—— 定义漂移只报不改（R8）。
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
        with _atomic(conn):
            mig.fn(conn)
            _record(conn, mig.version, mig.name, None)
        applied_now.append(f"{mig.version}:{mig.name}")

    checksum_state = _check_checksum(conn, schema_checksum(text))
    drifted = _check_drift_loudly(conn, text)
    if hasattr(conn, "commit"):
        conn.commit()                              # 收尾（真连接在 depth==0 时已自提交）

    return {
        "created_tables": created,
        "added_columns": ensured["columns"],
        "added_indexes": ensured["indexes"],
        "applied_migrations": applied_now,
        "checksum": checksum_state,
        "expected_tables": len(parse_schema(text)),
        "drifted": drifted,
    }


@contextmanager
def _atomic(conn):
    """多语句 **DML** 写要么全成、要么全不成。

    真 ``db.Connection`` 有 ``transaction()``；测试桩没有（它的 commit/rollback
    本就是空操作）⇒ 退化成"原样执行"，语义上等价于旧写法。

    ⚠️ 名字里的"原子"只管 DML。**DDL 是隐式提交的**（MariaDB/MySQL），
    包在事务里也回滚不掉 —— 别把这里当成"schema 变更的后悔药"（见 :class:`Migration`）。
    """
    tx = getattr(conn, "transaction", None)
    if tx is None:
        yield conn
        return
    with tx():
        yield conn


def _check_drift_loudly(conn, text: str) -> list[str]:
    """跑体检并把结果写进日志 —— **体检自己坏了也要看得见**（不许静默）。

    体检失败不阻断启动（表都在、只是没法判断漂移），但绝不能"假装没漂移"：
    失败会变成 ``drifted`` 里的一条 ``"体检失败: ..."``，于是状态页与日志都会红。
    """
    if _dialect(conn) == "sqlite":
        # SQLite 没有 information_schema；建表/补列/补索引已在上面按 schema.sql
        # 逐项完成。这里不另写一套 PRAGMA 漂移比对 —— 少一套口径就是少一处分叉。
        return []
    try:
        drifted = check_schema_drift(conn, text)
    except Exception as exc:  # noqa: BLE001 —— 见 docstring
        logger.warning("[WPP] schema 漂移体检失败（本次不判漂移）: %s", exc)
        return [f"体检失败: {exc}"]
    if drifted:
        logger.warning(
            "[WPP] schema 漂移 %d 处（**只报不改**，R8 不许 MODIFY/DROP）：%s",
            len(drifted), "; ".join(drifted[:5]))
    return drifted


def expected_table_names(sql_text: str | None = None) -> list[str]:
    """``schema.sql`` 声称要建的表名（供测试断言「文件里写的都真存在」）。"""
    text = SCHEMA_FILE.read_text(encoding="utf-8") if sql_text is None else sql_text
    return [m.group(1) for m in _CREATE_RE.finditer(strip_comments(text))]


def iter_indexes(sql_text: Iterable[str] | None = None) -> list[tuple[str, str]]:
    """``[(表名, 索引名), ...]`` —— 给测试用（TS 曾漏搬 5 个热查询索引）。"""
    text = SCHEMA_FILE.read_text(encoding="utf-8") if sql_text is None else "".join(sql_text)
    return [(name, idx) for name, spec in parse_schema(text).items() for idx, _ in spec.indexes]
