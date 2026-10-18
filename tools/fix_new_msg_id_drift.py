#!/usr/bin/env python3
"""修复 ``wpp_messages.new_msg_id`` 的定义漂移（**人肉执行，不自动跑**）。

## 为什么不做成 ``_MIGRATIONS`` 自动迁移

``data/migrations.py`` 的 R8 明确写着「只 ADD，不 MODIFY/DROP —— 改列型/删列要人肉决策，
脚本无权」。这个漂移恰好是 MODIFY，所以按设计它**不该**在启动时自动改生产表。

## 漂移是什么、为什么要修

``schema.sql`` 期望：

    new_msg_id VARCHAR(128) NOT NULL DEFAULT ''   -- 注释：NULL 会绕过唯一键

线上是 ``NULL`` 可空 + 无默认值，而表上有：

    UNIQUE KEY uk_account_msg (account_id, msg_id, new_msg_id)

MySQL 的唯一索引对 NULL **不去重**（多行 NULL 互不冲突）⇒ 一旦某条消息的
new_msg_id 为 NULL，同一个 (account_id, msg_id) 就能重复入库，去重失效。

## 凭据来源

这是**独立脚本**，不在 gateway 的 profile scope 里，`plugin.core.env_compat.secret_env`
按设计会返回空（fail-closed，不回退 os.environ）。所以这里显式用
`agent.secret_scope.build_profile_secret_scope(<profile home>)` 读该 profile 的 .env，
只取 DB 两项，**不打印、不落盘**。

## 用法

    # 只诊断（默认，纯读）
    tools/hermes_run.sh tools/fix_new_msg_id_drift.py

    # 真改（必须先看过诊断输出；DDL 隐式提交，MySQL 8 会锁写）
    tools/hermes_run.sh tools/fix_new_msg_id_drift.py --apply --yes
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugin.data import db as DB

TABLE = "wpp_messages"
COLUMN = "new_msg_id"
WANT_TYPE = "varchar(128)"
WANT_NULLABLE = "NO"
DEFAULT_PROFILE_HOME = "/root/.hermes/profiles/wpp-wechat"


def connect(profile_home: str):
    """按 profile 的 .env 连库（凭据只进内存，不打印）。"""
    import pymysql
    from agent.secret_scope import build_profile_secret_scope
    from pymysql.cursors import DictCursor

    cfg = DB.config()
    secrets = build_profile_secret_scope(Path(profile_home))
    cfg["user"] = secrets.get("WECHATPRO_DB_USER") or cfg["user"]
    cfg["password"] = secrets.get("WECHATPRO_DB_PASSWORD") or cfg["password"]
    for key, env_name in (("host", "WECHATPRO_DB_HOST"), ("database", "WECHATPRO_DB_NAME")):
        val = secrets.get(env_name)
        if val:
            cfg[key] = val
    if not cfg.get("password"):
        raise SystemExit(f"❌ 在 {profile_home} 的 .env 里没读到 WECHATPRO_DB_PASSWORD")
    DB.assert_host_allowed(cfg)
    raw = pymysql.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
        database=cfg["database"], charset="utf8mb4", cursorclass=DictCursor,
        autocommit=False, connect_timeout=int(cfg["connect_timeout"]),
        read_timeout=int(cfg["read_timeout"]), write_timeout=int(cfg["write_timeout"]),
    )
    return DB.Connection(raw), cfg


def describe_column(conn) -> dict:
    row = conn.query_one(
        "SELECT COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_NAME "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (TABLE, COLUMN),
    )
    return dict(row or {})


def is_ok(col: dict) -> bool:
    return (
        str(col.get("COLUMN_TYPE", "")).lower() == WANT_TYPE
        and str(col.get("IS_NULLABLE", "")).upper() == WANT_NULLABLE
        and str(col.get("COLUMN_DEFAULT") or "") in ("", "''")
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的执行回填 + ALTER")
    ap.add_argument("--yes", action="store_true", help="--apply 的二次确认")
    ap.add_argument("--profile-home", default=os.environ.get("WPP_SECRET_HOME") or DEFAULT_PROFILE_HOME,
                    help="从哪个 profile 的 .env 读 DB 凭据")
    ap.add_argument("--strategy", choices=("placeholder", "empty"), default="placeholder",
                    help="NULL 回填策略：placeholder=#<id>（默认，避免撞唯一键），empty=''")
    a = ap.parse_args(argv)

    conn, cfg = connect(a.profile_home)
    print(f"目标库: {cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}")

    col = describe_column(conn)
    if not col:
        print(f"❌ 表 {TABLE}.{COLUMN} 不存在 —— 先让插件跑一遍 ensure_schema_complete")
        return 1
    print(f"当前列: {col.get('COLUMN_NAME')} {col.get('COLUMN_TYPE')} "
          f"NULLABLE={col.get('IS_NULLABLE')} DEFAULT={col.get('COLUMN_DEFAULT')!r}")

    if is_ok(col):
        print("✅ 已符合 schema.sql 期望，无需修复")
        return 0

    total = conn.query_value(f"SELECT COUNT(*) FROM {TABLE}") or 0
    nulls = conn.query_value(f"SELECT COUNT(*) FROM {TABLE} WHERE {COLUMN} IS NULL") or 0
    print(f"行数: {total}（其中 NULL {nulls} 行需要回填成 ''）")

    if not a.apply:
        print("\n（诊断模式，未改动任何数据。确认后加 --apply --yes 执行）")
        return 0
    if not a.yes:
        print("\n❌ --apply 必须配 --yes（这是会改生产表结构的操作）")
        return 2

    if nulls:
        # 为什么要占位而不是回填成 ''：线上存在 197 行历史重复（同一
        # account_id+msg_id 多条，其中 178 行的 msg_id 本身是 NULL）——NULL
        # 绕过唯一键正是它们的来路。回填 '' 会立刻撞 uk_account_msg。
        # 占位符 `#<id>` 保证唯一、可辨识（看到 # 就知道是"当年没拿到 vendor id"），
        # 且不删任何一行历史数据。新写入的消息照常有真实 new_msg_id。
        if a.strategy == "empty":
            sql = f"UPDATE {TABLE} SET {COLUMN} = '' WHERE {COLUMN} IS NULL"
        else:
            sql = f"UPDATE {TABLE} SET {COLUMN} = CONCAT('#', id) WHERE {COLUMN} IS NULL"
        with conn.transaction():
            n = conn.execute(sql)
        print(f"✅ 回填 {n} 行（strategy={a.strategy}）")
    else:
        print("· 无 NULL 行，跳过回填")

    print("执行 ALTER …（会锁写，请勿中断）")
    conn.execute(
        f"ALTER TABLE {TABLE} MODIFY {COLUMN} VARCHAR(128) NOT NULL DEFAULT '' "
        f"COMMENT 'wechatpadpro new_msg_id（NULL 会绕过唯一键）'"
    )
    col2 = describe_column(conn)
    print(f"复验: {col2.get('COLUMN_TYPE')} NULLABLE={col2.get('IS_NULLABLE')} "
          f"DEFAULT={col2.get('COLUMN_DEFAULT')!r}")
    print("✅ 完成" if is_ok(col2) else "⚠️ 复验仍不符，请人工检查")
    return 0 if is_ok(col2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
