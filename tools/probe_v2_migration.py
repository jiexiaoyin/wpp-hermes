#!/usr/bin/env python3
"""V2 验证台：数据层能不能建起来、能不能幂等、坏输入会不会**炸**。

## 它验什么

| 步骤 | 判据 |
|---|---|
| ① 首跑 ``migrate()`` | 建出 ``schema.sql`` 声明的**全部**表（16）；checksum 记为 ``recorded`` |
| ② 再跑 ``migrate()`` | **零新建 / 零补齐 / 零应用**（幂等），checksum ``ok`` |
| ③ 心流表 | `SHOW TABLES LIKE 'wpp_hf_%'` == **5** |
| ④ 索引 | 5 个热查询索引 + 3 个 ledger 索引**真的在库里**（TS 曾漏搬） |
| ⑤ ★ 负控 | `_apply_schema_sql("")` 与「非空但切出 0 条」**都必须 raise** —— 这是 TS v1.6.1 静默失效的那道门 |
| ⑥ ★ 正控 | 塞一个 schema.sql 里多的列 → ``ensure_schema_complete`` **只 ADD** 把它补齐，然后自己删掉收尾 |

第 ⑤ 条是本验证台的**灵魂**：只验「能建表」是不够的 —— TS 的翻车恰恰是
「以为建好了」。所以这里必须证明**坏输入会炸**。

## 用法

    # 在 .183 上（那里才有 pymysql + MariaDB）
    <venv>/bin/python3 tools/probe_v2_migration.py --env-file /root/.hermes/.env \\
        --plugin /root/wpp-py2-check/plugin [--db wechatpro]

退出码 0 = 全绿。失败时打印 `FAIL:` 行说明是哪一条。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

#: 第 ⑥ 条用的临时列名（验完删掉）
_PROBE_COL = "v2_probe_col"

#: 注入点：wpp_redpackets 里这一行的后面
_INJECT_AFTER = "  hb_key VARCHAR(128) NOT NULL DEFAULT '',\n"

_FAILS: list[str] = []


def _check(cond: bool, label: str, detail: str = "") -> bool:
    if cond:
        print(f"  ok   {label}")
    else:
        msg = f"{label} —— {detail}" if detail else label
        _FAILS.append(msg)
        print(f"  FAIL {label}" + (f" —— {detail}" if detail else ""))
    return cond


def load_env_file(path: str | None) -> None:
    """把 ``KEY=VALUE`` 形式的 .env 读进 ``os.environ``（已存在的**不覆盖**）。"""
    if not path:
        return
    p = Path(path)
    if not p.exists():
        print(f"  warn .env 不存在: {p}")
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def perturb_schema(text: str) -> str:
    """在 schema.sql 文本里给 ``wpp_redpackets`` 塞一个多出来的列。"""
    if _INJECT_AFTER not in text:
        raise SystemExit(f"注不进去：schema.sql 里找不到锚点 {_INJECT_AFTER!r}")
    add = f"  {_PROBE_COL} TINYINT(1) NOT NULL DEFAULT 0,\n"
    return text.replace(_INJECT_AFTER, _INJECT_AFTER + add, 1)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin", default="/root/wpp-py2-check/plugin")
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--db", default=None, help="覆盖 WECHATPRO_DB_NAME")
    args = ap.parse_args(argv[1:])

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    load_env_file(args.env_file)
    if args.db:
        os.environ["WECHATPRO_DB_NAME"] = args.db

    sys.path.insert(0, str(Path(args.plugin).resolve()))
    from data import db, migrations            # noqa: E402
    from data.migrations import SchemaError    # noqa: E402

    print(f"● 目标 {db.describe()}")

    # ---- ① 首跑 ----------------------------------------------------------
    print("① 首跑 migrate()")
    first = db.migrate()
    expected = migrations.expected_table_names()
    _check(
        sorted(first["created_tables"]) == sorted(expected),
        "首跑建出 schema.sql 声明的全部表",
        f"建了 {len(first['created_tables'])} 张，声明 {len(expected)} 张",
    )
    _check(first["checksum"] == "recorded", "checksum 首次登记", first["checksum"])
    _check(len(expected) == 16, "schema.sql 声明 16 张表", f"实际 {len(expected)}")

    # ---- ② 幂等 ---------------------------------------------------------
    print("② 再跑 migrate()")
    second = db.migrate()
    _check(second["created_tables"] == [], "二次零新建", str(second["created_tables"]))
    _check(second["added_columns"] == [], "二次零补列", str(second["added_columns"]))
    _check(second["added_indexes"] == [], "二次零补索引", str(second["added_indexes"]))
    _check(second["checksum"] == "ok", "checksum 未变", second["checksum"])

    conn = db.get_conn()

    # ---- ③④ 表与索引 ----------------------------------------------------
    print("③ 心流表")
    hf = conn.query("SHOW TABLES LIKE 'wpp_hf_%'")
    _check(len(hf) == 5, "wpp_hf_* 五张齐全", f"实际 {len(hf)}: {sorted(v for r in hf for v in r.values())}")

    all_tables = {r["t"] for r in conn.query(
        "SELECT TABLE_NAME AS t FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
    )}
    _check(set(expected) <= all_tables, "声明的表全部真实存在",
           f"缺 {sorted(set(expected) - all_tables)}")
    _check(migrations.MIGRATION_TABLE in all_tables, "版本表存在")

    print("④ 索引（含 TS 只写在 mysql.ts、漏在 schema.sql 的那 8 个）")
    wanted = {
        # wpp_messages：schema.sql 原有 5 个
        ("wpp_messages", "idx_account_ts"), ("wpp_messages", "idx_peer"),
        ("wpp_messages", "idx_sender"), ("wpp_messages", "idx_msg_id"),
        ("wpp_messages", "idx_new_msg_id"), ("wpp_messages", "uk_account_msg"),
        # wpp_messages：TS 只在 ensureIndex 里写的 3 个
        ("wpp_messages", "idx_account_chat_ts"), ("wpp_messages", "idx_account_peer_ts"),
        ("wpp_messages", "idx_account_msgtype_ts"),
        # wpp_hf_ledger：schema.sql 原有 3 个
        ("wpp_hf_ledger", "uk_hf_acct_msg"), ("wpp_hf_ledger", "idx_hf_group_status"),
        ("wpp_hf_ledger", "idx_hf_open"),
        # wpp_hf_ledger：2026-09-28 审阅发现、TS 只在 ensureIndex 里写的 3 个
        ("wpp_hf_ledger", "idx_hf_judged"), ("wpp_hf_ledger", "idx_hf_sent"),
        ("wpp_hf_ledger", "idx_hf_closed"),
    }
    present = {(t, i) for t, i in migrations.iter_indexes() if t in
               {"wpp_messages", "wpp_hf_ledger"}}
    _check(wanted <= present, "schema.sql 声明了这些索引",
           f"缺 {sorted(wanted - present)}")
    live = set()
    for tbl in ("wpp_messages", "wpp_hf_ledger"):
        for r in conn.query(
            "SELECT DISTINCT INDEX_NAME AS n FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (tbl,),
        ):
            live.add((tbl, r["n"]))
    _check(wanted <= live, "索引真的建进库里", f"库里缺 {sorted(wanted - live)}")

    # ---- ⑤ 负控：静默失效必须炸 ------------------------------------------
    print("⑤ 负控（防静默失效）")
    try:
        migrations._apply_schema_sql(conn, "")
        _check(False, "空 schema 文本必须 raise", "居然没抛")
    except SchemaError:
        _check(True, "空 schema 文本必须 raise")
    except Exception as exc:                                   # noqa: BLE001
        _check(False, "空 schema 文本必须 raise", f"抛了别的异常 {type(exc).__name__}: {exc}")

    try:
        migrations._apply_schema_sql(conn, "-- 只有注释，没有语句\n")
        _check(False, "非空但切出 0 条必须 raise", "居然没抛")
    except SchemaError:
        _check(True, "非空但切出 0 条必须 raise")
    except Exception as exc:                                   # noqa: BLE001
        _check(False, "非空但切出 0 条必须 raise", f"抛了别的异常 {type(exc).__name__}: {exc}")

    # 切分器本身：16 条 CREATE 必须切出 16 条表语句
    text = migrations.SCHEMA_FILE.read_text(encoding="utf-8")
    stmts = migrations.split_statements(text)
    _check(len(stmts) == len(expected), "切分器切出 16 条语句",
           f"切出 {len(stmts)} 条")

    # ---- ⑥ 正控：反推补齐（只 ADD） --------------------------------------
    print("⑥ 正控（从 schema.sql 反推补列）")
    if _PROBE_COL in {c for c, _ in migrations.parse_schema(text)["wpp_redpackets"].columns}:
        _check(False, "临时列名不该出现在真 schema.sql 里")
    else:
        added = migrations.ensure_schema_complete(conn, perturb_schema(text))
        _check(added["columns"] == [f"wpp_redpackets.{_PROBE_COL}"],
               "多出来的列被补上", str(added["columns"]))
        _check(_PROBE_COL in migrations._existing_columns(conn, "wpp_redpackets"),
               "补齐后的列真的在库里")
        _check(migrations.ensure_schema_complete(conn, perturb_schema(text))["columns"] == [],
               "补齐动作幂等（再来一次不加）")
        conn.execute(f"ALTER TABLE wpp_redpackets DROP COLUMN {_PROBE_COL}")
        _check(_PROBE_COL not in migrations._existing_columns(conn, "wpp_redpackets"),
               "临时列已清理")

    result = {
        "db": db.describe(),
        "tables": len(all_tables),
        "declared": len(expected),
        "hf_tables": len(hf),
        "failures": _FAILS,
        "pass": not _FAILS,
    }
    print(json.dumps(result, ensure_ascii=False))
    db.close()
    return 0 if not _FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
