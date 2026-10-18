#!/usr/bin/env python3
"""W-Q 引用身份 **.183 真机**探针（计划第 52 项）。

## 为什么要有它

W-Q1 的回查 SQL 有两个判据**在 stub 上验不出来**，只能落到真 MariaDB 上才算数：

1. **``JSON`` 列回来的到底是字符串还是对象**。本模块的 ``_raw_payload_dict`` 之所以
   要处理"JSON 字符串"那一条分支，依据就是 MariaDB 的 ``JSON`` ≡ ``LONGTEXT``（V2b
   实测过）⇒ pymysql 原样回**字符串**。stub 上我们想让它回什么就回什么，
   **真正的形状只有服务器说了算**。
2. **``ORDER BY ts DESC`` 的决胜键真能定胜负**。stub 是"我们给的行"，真库要在
   *同一条 SQL 里匹配两行* 才会用到它 —— 而两行同时命中需要 ``msg_id`` 与
   ``new_msg_id`` 交叉（A 行的 new = B 行的 msg）。构造得出来，但只有真库能跑。

⇒ 本探针 = **合成数据 + 真库往返 + 真 ``resolve_quote_identity``**：插进去、查回来、
喂给身份链，看它到底认成谁。**不读任何真消息内容、不碰生产库、不连厂商、不发消息。**

## 用法（.183）

    /root/venvs/wpp-test/bin/python3 tools/probe_quote_identity.py \\
        --plugin /root/wpp-py2-check/plugin --env-file /root/.hermes/.env --db wechatpro_test

⚠️ 只对 ``*_test`` 库跑（默认就把 env 的库名改写成 ``_test``；不是 ``_test`` 直接退出 2）。
⚠️ 只动 ``ACCT`` 这一个合成账号的行，跑完在 ``finally`` 里删干净。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_FAILS: list[str] = []

#: 合成账号 —— 真库上不能撞任何在跑账号（探针只删这个 id 的行）
ACCT = "quote_probe"
#: 交叉账号 —— 用来验"OR 的括号"真在起作用（少了括号会跨账号命中）
OTHER = "quote_probe_other"
GROUP = "99999999999@chatroom"      # 明显的假群号
SENDER = "wxid_probe_sender"
NICK = "探针张三"


def _check(cond: object, label: str, detail: object = "") -> bool:
    ok = bool(cond)
    if ok:
        print(f"  ok   {label}")
    else:
        msg = f"{label} —— {detail}" if detail != "" else label
        _FAILS.append(msg)
        print(f"  FAIL {label}" + (f" —— {detail}" if detail != "" else ""))
    return ok


def load_env_file(path: str | None) -> None:
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


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin", default="/root/wpp-py2-check/plugin")
    ap.add_argument("--env-file", default="/root/.hermes/.env")
    ap.add_argument("--db", default=None, help="默认把 env 的库名改成 <name>_test")
    args = ap.parse_args(argv[1:])

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: S110, BLE001 —— 异常不得冒泡打断整批（收尾/探针路径）
        pass

    load_env_file(args.env_file)
    base = args.db or os.environ.get("WECHATPRO_DB_NAME", "wechatpro")
    if not base.endswith("_test"):
        base = f"{base}_test"
    os.environ["WECHATPRO_DB_NAME"] = base

    # ⚠️ **按包**导入（``plugin.*``），与 .183 上 Hermes 装载本插件的真实形态一致：
    #    扁平导入（``from data import …``）会让 ``plugin.data.db`` 与 ``data.db`` 成为
    #    两个模块实例（同一个库、两条连接），本探针跨层要的是**同一份**。这一点在
    #    ``probe_v2b_repos.py`` 里已经用血换过一次教训。
    sys.path.insert(0, str(Path(args.plugin).resolve().parent))
    from plugin.data import db, repo_messages
    from plugin.inbound import quote_ref

    print(f"● 目标 {db.describe()}（**必须**是 _test 库）")
    if not base.endswith("_test"):
        print("  FAIL 拒绝对非测试库跑")
        return 2

    db.migrate()
    conn = db.get_conn()
    now = int(time.time())

    def wipe() -> None:
        for acct in (ACCT, OTHER):
            conn.execute("DELETE FROM wpp_messages WHERE account_id = %s", (acct,))

    wipe()
    try:
        # ─────────────────────────────────────────── ① JSON 列的真实回程形状
        # 这一条是 W-Q2 那条 `_raw_payload_dict` 分支的**存在理由**：如果服务器回的是
        # 字符串而我们只认 dict，身份链的中间两档会**静默全空**（现场就是"@ 不出来"）。
        payload = {"sender_id": SENDER, "go_fields": {"FromUserName": GROUP}}
        repo_messages.save_message(
            ACCT, direction="inbound", peer_kind="group", peer_id=GROUP, chat_id=GROUP,
            msg_id="probe-json-1", new_msg_id="probe-json-1-new", msg_type="1",
            content="[图片]", raw_payload=payload, ts=now - 30,
        )
        row = repo_messages.get_message_by_id_or_new_id(ACCT, msg_id="probe-json-1")
        _check(row is not None, "① 按 msg_id 查回来了一行")
        raw = (row or {}).get("raw_payload")
        print(f"  ·  raw_payload 回程类型 = {type(raw).__name__}"
              f"（len={len(raw) if isinstance(raw, (str, bytes)) else 'n/a'}）")
        _check(isinstance(raw, (str, bytes)),
               "① 真库把 JSON 列回成**字符串**（若不是，`_raw_payload_dict` 的字符串分支就是死码）",
               f"实际 {type(raw).__name__}")
        ident = quote_ref.resolve_quote_identity(row)
        _check(ident.fromusr == SENDER,
               "① 身份链穿过 JSON 字符串仍认得发送者",
               f"fromusr={ident.fromusr!r}（期望 {SENDER!r}）")
        _check(ident.matched is True, "① matched=True")

        # ─────────────────────────────────────────── ② 两个 id 各查一次
        r_mid = repo_messages.get_message_by_id_or_new_id(ACCT, msg_id="probe-json-1")
        r_nid = repo_messages.get_message_by_id_or_new_id(ACCT, new_msg_id="probe-json-1-new")
        r_any = repo_messages.get_message_by_id_or_new_id(
            ACCT, msg_id="probe-json-1", new_msg_id="probe-json-1-new")
        _check(r_mid and r_nid and r_any,
               "② 两半各自都能命中（引用锚点在**服务端 new_msg_id** 那一半也成立）",
               f"mid={bool(r_mid)} nid={bool(r_nid)} any={bool(r_any)}")
        _check([r_mid, r_nid, r_any].count(None) == 0 and len({id(r) for r in (r_mid, r_nid, r_any)}) == 3,
               "② 三次查询各自回一行（不是同一个对象被缓存）")

        miss = repo_messages.get_message_by_id_or_new_id(ACCT, msg_id="probe-nope")
        _check(miss is None, "② 查不到的 id 回 None（调用方据此判 unmatched）", repr(miss))
        blank = repo_messages.get_message_by_id_or_new_id(ACCT, msg_id="   ", new_msg_id="")
        _check(blank is None, "② 空白 id 不发查询、直接 None", repr(blank))

        # ─────────────────────────────────────────── ③ OR 的括号：不许跨账号命中
        repo_messages.save_message(
            OTHER, direction="inbound", peer_kind="group", peer_id=GROUP, chat_id=GROUP,
            msg_id="probe-x-1", new_msg_id="probe-json-1-new",   # ← 同一个 new_msg_id，**不同账号**
            msg_type="1", content="别的账号的行", raw_payload={"sender_id": "wxid_other"},
            ts=now,
        )
        # ⚠️ **必须同时传两个 id**：只传一个时，`account_id = %s AND (x = %s)` 与
        # 去掉括号的 `account_id = %s AND x = %s` 是**同一句**，括号坏了也看不出来。
        # 两个都传才让 `OR` 真正出现在 `AND` 右边：缺括号 ⇒ `(acct AND msg_id) OR new_msg_id`
        # ⇒ 另一账号那行（ts 更新，决胜键还偏袒它）会被当成本账号的结果回出来。
        leaked = repo_messages.get_message_by_id_or_new_id(
            ACCT, msg_id="probe-nopesuch", new_msg_id="probe-json-1-new")
        _check(leaked is not None and leaked.get("content") == "[图片]",
               "③ 括号生效：同 new_msg_id 的另一账号行**不会**被查出来（返回的仍是本账号那行）",
               repr((leaked or {}).get("content")))

        # ─────────────────────────────────────────── ④ 决胜键（同一条 SQL 命中两行）
        # A(ts 早) 的 new_msg_id == B(ts 晚) 的 msg_id ⇒ 用这个 id 查会**同时**命中两行，
        # 靠 `ORDER BY ts DESC, id DESC` 定胜负。TS 那边没有 ORDER BY（存储引擎说了算）。
        repo_messages.save_message(
            ACCT, direction="inbound", peer_kind="group", peer_id=GROUP, chat_id=GROUP,
            msg_id="probe-cross", new_msg_id="probe-cross-new", msg_type="1",
            content="旧的那行", raw_payload={"sender_id": "wxid_old"}, ts=now - 600,
        )
        repo_messages.save_message(
            ACCT, direction="inbound", peer_kind="group", peer_id=GROUP, chat_id=GROUP,
            msg_id="probe-cross-new", new_msg_id="probe-cross-newer", msg_type="1",
            content="新的那行", raw_payload={"sender_id": "wxid_new"}, ts=now - 300,
        )
        seen = set()
        for _ in range(3):
            got = repo_messages.get_message_by_id_or_new_id(ACCT, msg_id="probe-cross-new")
            seen.add((got or {}).get("content"))
        _check(seen == {"新的那行"},
               "④ 交叉命中两行时，三次查询都稳定选中 ts 更晚的那一行",
               f"取到 {seen}")

        # ─────────────────────────────────────────── ⑤ 不限方向：bot 自己发的也要查得到
        repo_messages.save_message(
            ACCT, direction="outbound", peer_kind="group", peer_id=GROUP, chat_id=GROUP,
            msg_id="probe-out-1", new_msg_id="probe-out-1-new", msg_type="1",
            content="bot 自己发的话", raw_payload=None, ts=now - 120,
        )
        out_row = repo_messages.get_message_by_id_or_new_id(ACCT, msg_id="probe-out-1")
        _check(out_row is not None and out_row.get("direction") == "outbound",
               "⑤ 出站行也查得到（`direction='any'`；筛 inbound 会让这一类永远查不到）",
               repr((out_row or {}).get("direction")))
        out_ident = quote_ref.resolve_quote_identity(out_row)
        _check(out_ident.fromusr == "",
               "⑤ 但出站群行没有可 @ 的人（没有 from_wxid / raw_payload）⇒ 空身份，不 @ 任何人",
               f"fromusr={out_ident.fromusr!r}")
        _check(out_ident.matched is True, "⑤ matched=True（「查到了但取不到人」要留痕）")

        # ─────────────────────────────────────────── ⑥ peer_name 走真列回程
        repo_messages.save_message(
            ACCT, direction="inbound", peer_kind="group", peer_id=GROUP, chat_id=GROUP,
            msg_id="probe-name-1", new_msg_id="probe-name-1-new", msg_type="1",
            content="普通正文", peer_name=NICK, from_wxid=SENDER, ts=now - 60,
        )
        named = repo_messages.get_message_by_id_or_new_id(ACCT, msg_id="probe-name-1")
        _check((named or {}).get("peer_name") == NICK,
               "⑥ peer_name 原样回得来（这一级是真机上**唯一**能直接拿到名字的来源）",
               repr((named or {}).get("peer_name")))
        prefix = quote_ref.build_mention_prefix(
            GROUP, quote_ref.resolve_quote_identity(named).display_name,
            quote_ref.resolve_quote_identity(named).fromusr)
        _check(prefix == f"@{NICK} ",
               "⑥ 端到端：真库行 ⇒ 身份 ⇒ 前缀",
               f"prefix={prefix!r}（期望 {('@' + NICK + ' ')!r}）")

        # ─────────────────────────────────────────── ⑦ 群 id 落进 from_wxid 的形态
        repo_messages.save_message(
            ACCT, direction="inbound", peer_kind="group", peer_id=GROUP, chat_id=GROUP,
            msg_id="probe-gid-1", new_msg_id="probe-gid-1-new", msg_type="1",
            content="业务回调形态", from_wxid=GROUP,     # ← business callback 的 FromUserName
            raw_payload={"go_fields": {"sender_id": SENDER}}, ts=now - 10,
        )
        gid = repo_messages.get_message_by_id_or_new_id(ACCT, msg_id="probe-gid-1")
        gid_ident = quote_ref.resolve_quote_identity(gid)
        _check(gid_ident.fromusr == SENDER,
               "⑦ 行里的 from_wxid 是群 id ⇒ 继续往下找，最终认到真发送者（不是群自己）",
               f"fromusr={gid_ident.fromusr!r}")
        _check(quote_ref.build_mention_prefix(GROUP, "", gid_ident.fromusr) == f"@{SENDER} ",
               "⑦ 拿不到昵称时退化成 @wxid（TS 同款兜底）")
    finally:
        wipe()
        print("  ·  已清场（只删本探针这两个合成账号的行）")

    print()
    if _FAILS:
        print(f"❌ FAIL — 失败 {len(_FAILS)} 条:")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print("✅ PASS — 失败 0 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
