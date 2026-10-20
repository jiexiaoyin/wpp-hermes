"""SQLite 方言层：把本仓既有的 MySQL SQL / DDL 翻译成 SQLite 可执行形态。

## 为什么做「运行时翻译」而不是再写一份 ``schema_sqlite.sql``

``schema.sql`` 是**唯一 DDL 真源**（见 ``data/migrations.py`` 的模块 docstring）。
再维护一份 SQLite schema 就是当年的「双写陷阱」：改一处忘另一处，两边定义悄悄分叉。
所以这里只做**确定性的语法翻译**，表/列/索引仍然只有 ``schema.sql`` 一个来源。

## 翻译范围（只覆盖本仓实际用到的语法）

* 占位符：``%s`` → ``?``，``%%`` → ``%``
* UPSERT：``ON DUPLICATE KEY UPDATE`` → ``ON CONFLICT DO UPDATE SET``，
  ``VALUES(col)`` → ``excluded.col``（SQLite 3.24+ 支持无冲突目标的 UPSERT）
* 时间函数：``FROM_UNIXTIME(x)`` → ``datetime(x, 'unixepoch')``，
  ``UNIX_TIMESTAMP(x)`` → ``CAST(strftime('%s', x) AS INTEGER)``
* 行锁：``FOR UPDATE`` → 删除（SQLite 写事务本身就是库级锁）
* ``CHAR_LENGTH`` → ``length``
* DDL：MySQL 类型/``ENGINE``/``CHARSET``/``AUTO_INCREMENT``/``ON UPDATE`` 等
  转成 SQLite 类型；表内 ``KEY``/``INDEX`` 抽成独立 ``CREATE INDEX``。

⚠️ 这里**不是**通用 SQL 翻译器。新增 SQL 若用了别的 MySQL 专有函数，必须同时
在这里补翻译 + 在 ``plugin/tests/test_sqlite_backend.py`` 里加一条真实执行用例。
"""

from __future__ import annotations

import re

#: MySQL 类型 → SQLite 类型（按**最长前缀**优先匹配）。
_TYPE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    # ⚠️ 每个类型都带 ``\b``：不带边界时 ``INT`` 会吃掉 ``endpoint`` 的尾三字母
    #    （实测：``endpoint`` → ``endpoINTEGER``，建索引时才炸）。
    (r"\bBIGINT\s+UNSIGNED\b", "INTEGER"),
    (r"\bBIGINT\b", "INTEGER"),
    (r"\bINT\s+UNSIGNED\b", "INTEGER"),
    (r"\bTINYINT\s*\(\s*1\s*\)", "INTEGER"),
    (r"\bTINYINT\b", "INTEGER"),
    (r"\bINT\b", "INTEGER"),
    (r"\bVARCHAR\s*\(\s*\d+\s*\)", "TEXT"),
    (r"\bCHAR\s*\(\s*\d+\s*\)", "TEXT"),
    (r"\bLONGTEXT\b", "TEXT"),
    (r"\bMEDIUMTEXT\b", "TEXT"),
    (r"\bJSON\b", "TEXT"),
    (r"\bENUM\s*\([^)]*\)", "TEXT"),
    (r"\bDECIMAL\s*\(\s*\d+\s*,\s*\d+\s*\)", "REAL"),
    (r"\bDOUBLE\b", "REAL"),
    (r"\bFLOAT\b", "REAL"),
)

_ENGINE_RE = re.compile(
    r"\)\s*(?:ENGINE\s*=\s*\w+\s*)?(?:DEFAULT\s+)?(?:CHARSET\s*=\s*\w+\s*)?"
    r"(?:COLLATE\s*=\s*\w+\s*)?;?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _split_sql_list(body: str) -> list[str]:
    """按逗号切 DDL 定义，**忽略括号内的逗号**（ENUM/DECIMAL 里都有）。"""
    items: list[str] = []
    depth = 0
    start = 0
    quote: str | None = None
    for i, ch in enumerate(body):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"', "`"):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            items.append(body[start:i].strip())
            start = i + 1
    tail = body[start:].strip()
    if tail:
        items.append(tail)
    return items


def _strip_comments(sql: str) -> str:
    """去掉 ``--`` 行注释（本仓 schema 里没有字符串内含 ``--`` 的情况）。"""
    out: list[str] = []
    for line in sql.splitlines():
        # 只截断第一个 --（DDL 的注释都在行尾）
        idx = line.find("--")
        out.append(line[:idx] if idx >= 0 else line)
    return "\n".join(out)


def _convert_type(defn: str) -> str:
    out = defn
    out = re.sub(r"\bUNSIGNED\b", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\bAUTO_INCREMENT\b", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\bON\s+UPDATE\s+CURRENT_TIMESTAMP\b", "", out, flags=re.IGNORECASE)
    for pattern, repl in _TYPE_REPLACEMENTS:
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    # ``TEXT NOT NULL DEFAULT ''``：SQLite 允许；``REAL NOT NULL DEFAULT 0`` 也允许。
    return re.sub(r"\s+", " ", out).strip()


def _index_columns(defn: str) -> tuple[str, str]:
    """``INDEX/KEY name (cols)`` → ``(name, cols)``。"""
    m = re.match(
        r"(?:UNIQUE\s+)?(?:KEY|INDEX)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
        defn, re.IGNORECASE | re.DOTALL)
    if not m:
        return "", ""
    return m.group(1), re.sub(r"\s+", "", m.group(2))


def _convert_create_table(stmt: str) -> list[str]:
    """一条 MySQL ``CREATE TABLE`` → ``[CREATE TABLE ..., CREATE INDEX ...]``。"""
    m = re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
                  stmt, re.IGNORECASE)
    if not m:
        return []
    table = m.group(1)
    open_paren = stmt.find("(", m.end())
    if open_paren < 0:
        return []
    # 找与 open_paren 匹配的右括号（表定义最后那个）
    depth = 1
    close = open_paren + 1
    while close < len(stmt) and depth:
        if stmt[close] == "(":
            depth += 1
        elif stmt[close] == ")":
            depth -= 1
        close += 1
    body = stmt[open_paren + 1:close - 1]

    columns: list[str] = []
    extras: list[str] = []
    indexes: list[str] = []
    for item in _split_sql_list(body):
        if not item:
            continue
        upper = item.upper()
        if upper.startswith(("UNIQUE KEY", "UNIQUE INDEX", "KEY ", "INDEX ")):
            name, cols = _index_columns(item)
            if name and cols:
                uniq = "UNIQUE " if upper.startswith("UNIQUE") else ""
                # SQLite 的索引名是**全库唯一**的（不像 MySQL 按表命名空间）。
                # ``idx_account_ts`` 同时出现在 wpp_messages 与 wpp_api_calls 上，
                # 不加表名前缀时第二个会被 ``IF NOT EXISTS`` 静默跳过。
                indexes.append(
                    f"CREATE {uniq}INDEX IF NOT EXISTS {table}__{name} "
                    f"ON {table} ({cols})")
            continue
        if upper.startswith(("PRIMARY KEY", "CONSTRAINT", "FOREIGN KEY")):
            extras.append(re.sub(r"\s+", " ", item))
            continue
        # 列定义：``id BIGINT ... AUTO_INCREMENT PRIMARY KEY`` 要变成
        # ``id INTEGER PRIMARY KEY AUTOINCREMENT``（SQLite 的自增语法只认这一种）。
        if re.search(r"\bAUTO_INCREMENT\b", item, re.IGNORECASE) and \
                re.search(r"\bPRIMARY\s+KEY\b", item, re.IGNORECASE):
            col = item.split(None, 1)[0]
            columns.append(f"{col} INTEGER PRIMARY KEY AUTOINCREMENT")
            continue
        columns.append(_convert_type(item))

    create = (f"CREATE TABLE IF NOT EXISTS {table} (\n  "
              + ",\n  ".join(columns + extras) + "\n)")
    return [create, *indexes]


def schema_to_sqlite(schema_sql: str) -> list[str]:
    """``schema.sql`` → SQLite 可执行的语句列表（幂等）。"""
    text = _strip_comments(schema_sql)
    statements = [s.strip() for s in text.split(";") if s.strip()]
    out: list[str] = []
    for stmt in statements:
        if re.match(r"CREATE\s+TABLE", stmt, re.IGNORECASE):
            out.extend(_convert_create_table(stmt))
        else:
            # 本仓 schema.sql 只有 CREATE TABLE；出现别的语句说明真源变了，
            # 直接抛出比静默跳过安全（静默跳过 = 新表永远建不出来）。
            raise ValueError(f"schema.sql 出现未支持的语句: {stmt[:80]!r}")
    return out


# ------------------------------------------------------------------ 查询翻译


def _rewrite_call(sql: str, name: str, render) -> str:
    """把 ``name(arg)`` 替换成 ``render(arg)``，**支持嵌套括号**。"""
    out = sql
    needle = name + "("
    start = 0
    while True:
        i = out.find(needle, start)
        if i < 0:
            return out
        arg_start = i + len(needle)
        depth = 1
        j = arg_start
        while j < len(out) and depth:
            if out[j] == "(":
                depth += 1
            elif out[j] == ")":
                depth -= 1
            j += 1
        if depth:
            return out
        arg = out[arg_start:j - 1]
        out = out[:i] + render(arg) + out[j:]
        start = i + len(render(arg))


def translate_sql(sql: str) -> str:
    """MySQL SQL → SQLite SQL（只用于 SQLite 连接）。"""
    # ``%%`` 是 MySQL 参数化下的字面量百分号；SQLite 直接写 ``%``。
    sql = sql.replace("%%", "%")
    sql = re.sub(r"%s", "?", sql)
    # MySQL 的 ``INSERT IGNORE`` → SQLite ``INSERT OR IGNORE``（心流台账去重）。
    sql = re.sub(r"\bINSERT\s+IGNORE\s+INTO\b", "INSERT OR IGNORE INTO",
                 sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bFOR\s+UPDATE\b", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bCHAR_LENGTH\s*\(", "length(", sql, flags=re.IGNORECASE)
    sql = _rewrite_call(
        sql, "UNIX_TIMESTAMP",
        lambda arg: f"CAST(strftime('%s', {arg}) AS INTEGER)")
    sql = _rewrite_call(
        sql, "FROM_UNIXTIME",
        lambda arg: f"datetime({arg}, 'unixepoch')")
    sql = re.sub(
        r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", "ON CONFLICT DO UPDATE SET",
        sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"\bVALUES\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        r"excluded.\1", sql, flags=re.IGNORECASE)
    return sql


def convert_schema_fragment(defn: str) -> str:
    """把 ``schema.sql`` 里的一段列定义转成 SQLite 可 ``ALTER TABLE ADD COLUMN`` 的形态。"""
    return _convert_type(defn)
