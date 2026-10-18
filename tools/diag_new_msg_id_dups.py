"""只读诊断：new_msg_id 的 NULL 与重复分布（不做任何写操作）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fix_new_msg_id_drift import connect  # noqa: E402

T = "wpp_messages"


def main() -> int:
    conn, cfg = connect("/root/.hermes/profiles/wpp-wechat")
    print(f"库: {cfg['database']}")
    total = conn.query_value(f"SELECT COUNT(*) FROM {T}")
    nulls = conn.query_value(f"SELECT COUNT(*) FROM {T} WHERE new_msg_id IS NULL")
    print(f"总行={total} NULL={nulls}")

    dup = conn.query(
        f"SELECT account_id, msg_id, COUNT(*) AS c FROM {T} "
        f"WHERE new_msg_id IS NULL GROUP BY account_id, msg_id HAVING c > 1 "
        f"ORDER BY c DESC LIMIT 10"
    )
    ndup = conn.query_value(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM {T} WHERE new_msg_id IS NULL "
        f"GROUP BY account_id, msg_id HAVING COUNT(*) > 1) x"
    )
    extra = conn.query_value(
        f"SELECT COALESCE(SUM(c-1),0) FROM (SELECT COUNT(*) c FROM {T} WHERE new_msg_id IS NULL "
        f"GROUP BY account_id, msg_id HAVING c > 1) x"
    )
    print(f"重复组(account_id,msg_id)={ndup} 多余行={extra}")
    for r in dup:
        print("  ", dict(r))

    # 这些重复行是否时间/内容完全相同（真重复）还是不同消息撞了 msg_id
    if ndup:
        sample = conn.query(
            f"SELECT id, account_id, msg_id, msg_type, created_at, LEFT(content, 40) AS c "
            f"FROM {T} WHERE new_msg_id IS NULL AND (account_id, msg_id) IN "
            f"(SELECT account_id, msg_id FROM {T} WHERE new_msg_id IS NULL "
            f" GROUP BY account_id, msg_id HAVING COUNT(*) > 1 LIMIT 3) "
            f"ORDER BY msg_id, id"
        )
        print("样例:")
        for r in sample:
            print("  ", dict(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
