"""数据层：DDL 真源 + migration 框架 + 连接池 + repo_*。

分层（严格单向，禁止反向 import）：

    schema.sql          ← 唯一 DDL 真源（16 张表）
        ↑ 反向推导
    migrations.py       ← 建表 / ensure_column / ensure_index / checksum
        ↑
    db.py               ← 连接、事务、计数器（V7 用）
        ↑
    repo_*.py           ← 只写 SQL，不写业务规则

⚠️ 本包**不得**在 import 期连库。`plugin_validate` 会在隔离子进程里跑
`register()`，那时既没有 DB 也没有 pymysql。
"""

from . import db, migrations

__all__ = ["db", "migrations"]
