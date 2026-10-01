#!/usr/bin/env python3
"""V2b 验证台：repo_* 的 SQL 真能跑，且**状态机 guard 逐条成立**。

## 为什么要有它

V2 只证明「表建起来了」。repo_* 里那些 ``WHERE ... AND status = 'judged'`` 的
guard 才是把历史事故焊死的地方 —— 而它们**只有跑起来才知道对不对**：

- ``set_sent`` 从 ``suppressed``（且从未发过）要能推进（v1.10.0 修的口径）
- ``set_sent`` 从 ``closed`` **不能**推回去
- ``set_suppressed`` 只认 ``judged``
- ``mark_engaged(close=False)`` 保持 ``sent`` 以便升格；``close=True`` 只覆盖 ``short-window``
- ``get_last_sent_at`` 在行被 ``close`` 之后**仍要返回 sent_at**（**I6**：
  写成 ``status='sent'`` 会恒返回 0，正是「停摆 3 天而指标全绿」的成因）
- ``list_bot_msg_share`` 必须靠 ``COALESCE(NULLIF(chat_id,''), peer_id)`` 才数得到
  bot 自己的发言（出站行 ``chat_id`` 恒 NULL）

## 用法

    <venv>/bin/python3 tools/probe_v2b_repos.py --env-file /root/.hermes/.env \\
        --plugin /root/wpp-py2-check/plugin --db wechatpro_test

⚠️ 只对 ``*_test`` 库跑（默认就从 env 的库名改写成 ``_test``）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_FAILS: list[str] = []
ACCT = "probe_acct"
GROUP = "12345678@chatroom"
PEER = "wxid_friend_1"


def _check(cond: bool, label: str, detail: str = "") -> bool:
    if cond:
        print(f"  ok   {label}")
    else:
        msg = f"{label} —— {detail}" if detail else label
        _FAILS.append(msg)
        print(f"  FAIL {label}" + (f" —— {detail}" if detail else ""))
    return cond


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
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--db", default=None, help="默认把 env 的库名改成 <name>_test")
    args = ap.parse_args(argv[1:])

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    load_env_file(args.env_file)
    base = args.db or os.environ.get("WECHATPRO_DB_NAME", "wechatpro")
    if not base.endswith("_test"):
        base = f"{base}_test"
    os.environ["WECHATPRO_DB_NAME"] = base

    sys.path.insert(0, str(Path(args.plugin).resolve()))
    from data import db, repo_hf, repo_messages   # noqa: E402

    print(f"● 目标 {db.describe()}（**必须**是 _test 库）")
    if not base.endswith("_test"):
        print("  FAIL 拒绝对非测试库跑")
        return 2

    db.migrate()
    # 清干净（只动本账号，别 TRUNCATE 整表）
    conn = db.get_conn()
    for tbl in ("wpp_hf_ledger", "wpp_hf_group_state", "wpp_hf_threshold_audit",
                "wpp_hf_group_profile", "wpp_hf_layer_stat", "wpp_messages",
                "wpp_contacts", "wpp_chatrooms", "wpp_chatroom_members",
                "wpp_jargon_terms", "wpp_redpackets", "wpp_api_calls",
                "wpp_svrid_mapping", "wpp_sync_state", "wpp_session_state"):
        conn.execute(f"DELETE FROM {tbl} WHERE account_id = %s", (ACCT,))

    now = int(time.time())

    # ---------------------------------------------------------------- 消息
    print("① wpp_messages")
    repo_messages.save_message(
        ACCT, direction="inbound", peer_kind="group", peer_id=GROUP, chat_id=GROUP,
        msg_id="m1", new_msg_id="n1", msg_type="text", content="人说的话",
        from_wxid="wxid_human", raw_payload={"a": 1}, ts=now - 60,
    )
    repo_messages.save_message(
        ACCT, direction="inbound", peer_kind="group", peer_id=GROUP, chat_id=GROUP,
        msg_id="m1", new_msg_id="n1", msg_type="text", content="人说的话(改)",
        from_wxid="wxid_human", ts=now - 60,
    )
    # ★ 出站行只有 peer_kind + peer_id，chat_id 为 NULL（复刻 TS 落库形态）
    repo_messages.save_message(
        ACCT, direction="outbound", peer_kind="group", peer_id=GROUP,
        msg_id="m2", new_msg_id="n2", msg_type="text", content="bot 说的话", ts=now - 30,
    )
    rows = conn.query("SELECT COUNT(*) AS n FROM wpp_messages WHERE account_id = %s", (ACCT,))
    _check(int(rows[0]["n"]) == 2, "同 (msg_id,new_msg_id) 幂等 → 2 行（非 3）", str(rows[0]))
    same = conn.query_one(
        "SELECT content FROM wpp_messages WHERE account_id = %s AND msg_id = 'm1'", (ACCT,))
    _check(same["content"] == "人说的话(改)", "重复入库更新内容", str(same))

    share = repo_messages.list_bot_msg_share(ACCT, now - 600)
    by_dir = {(r["direction"]): int(r["n"]) for r in share}
    _check(by_dir.get("outbound") == 1,
           "★ 出站发言被 COALESCE(NULLIF(chat_id,''),peer_id) 数到（chat_id 为 NULL）",
           str(share))
    _check(all(r["gid"] == GROUP for r in share), "两侧都归到同一个群 id", str(share))

    buckets = repo_messages.list_group_msg_hour_buckets(ACCT, now - 600, 8 * 3600)
    _check(len(buckets) == 1 and int(buckets[0]["n"]) == 1, "小时分桶只算入站（1 条）",
           str(buckets))
    _check("h" in buckets[0] and 0 <= int(buckets[0]["h"]) < 24, "小时落在 0–23",
           str(buckets[0]))

    texts = repo_messages.list_outbound_texts(ACCT, now - 600)
    _check(len(texts) == 1 and texts[0]["content"] == "bot 说的话",
           "出站文本素材", str(texts))

    groups = repo_messages.list_active_group_ids(ACCT, now - 600)
    _check(groups == [GROUP], "活跃群枚举", str(groups))

    # 群画像素材（4 条单表聚合）：必须只算**入站**，且小时是**本地**小时
    st = repo_messages.get_hf_group_message_stats(ACCT, GROUP, now - 600)
    _check(st["total"] == 1, "★ 画像统计只算入站（排除 bot 自己说的 ⇒ 画像不会自我强化）", str(st))
    _check(len(st["hourHist"]) == 24 and sum(st["hourHist"]) == 1,
           "小时直方图恒 24 格且总数对齐", str(st["hourHist"]))
    _check(st["avgLen"] == len("人说的话(改)") and st["typeHist"] == {"text": 1},
           "平均长度 / 类型直方图", str(st))
    _check([s["wxid"] for s in st["topSenders"]] == ["wxid_human"], "发言最多的成员", str(st))
    local_h = time.localtime(now - 60).tm_hour
    _check(st["hourHist"][local_h] == 1,
           f"★ 小时是**本地**小时（{local_h} 时），不是 UTC", str(st["hourHist"]))

    # ---------------------------------------------------------------- 台账状态机
    print("② wpp_hf_ledger 状态机")
    _check(repo_hf.insert_judged(
        ACCT, "in1", GROUP, now - 300, dim_r=8.0, judge_overall=0.72,
        effective_threshold=0.6, content_head="第一条") == 1, "insert_judged 落一行")
    _check(repo_hf.insert_judged(ACCT, "in1", GROUP, now - 300, judge_overall=0.9) == 0,
           "同 inbound_msg_id 重复 insert 被 IGNORE")

    _check(repo_hf.set_suppressed(ACCT, "in1", "below-threshold", now - 290) == 1,
           "judged → suppressed 允许")
    _check(repo_hf.set_suppressed(ACCT, "in1", "again", now - 280) == 0,
           "★ 已是 suppressed 再 set_suppressed 不生效（只认 judged）")
    _check(repo_hf.set_sent(ACCT, "in1", now - 270, now + 30, "bot1") == 1,
           "★ suppressed 且未发过 → sent 允许（v1.10.0 口径）")
    _check(repo_hf.set_sent(ACCT, "in1", now - 260, now + 40, "bot1b") == 0,
           "已 sent（sent_at 非空）再 set_sent 不生效")

    _check(repo_hf.mark_engaged(ACCT, GROUP, now - 200, 1, "short-window", False, "in1") == 1,
           "mark_engaged(close=False) 记弱信号")
    st = conn.query_one("SELECT status, engaged, engage_signal FROM wpp_hf_ledger "
                        "WHERE account_id = %s AND inbound_msg_id = 'in1'", (ACCT,))
    _check(st["status"] == "sent" and int(st["engaged"]) == 1,
           "★ close=False 后**保持 sent**（留出升格余地）", str(st))
    _check(repo_hf.mark_engaged(ACCT, GROUP, now - 190, 1, "quote", True, "in1") == 1,
           "弱信号可被强信号覆盖（close=True）")
    st = conn.query_one("SELECT status, engage_signal FROM wpp_hf_ledger "
                        "WHERE account_id = %s AND inbound_msg_id = 'in1'", (ACCT,))
    _check(st["status"] == "closed" and st["engage_signal"] == "quote",
           "已关窗且信号为 quote", str(st))
    _check(repo_hf.mark_engaged(ACCT, GROUP, now - 180, 0, "negative", True, "in1") == 0,
           "★ 已关窗后不再被任何信号改动")

    print("③ I6：最后真发言只认 MAX(sent_at)")
    last = repo_hf.get_last_sent_at(ACCT)
    _check(last == now - 270,
           "★ 行已 closed，get_last_sent_at 仍返回 sent_at（写成 status='sent' 会恒为 0）",
           f"got {last}")

    print("④ 到期关窗两条分支")
    repo_hf.insert_judged(ACCT, "in2", GROUP, now - 300)
    repo_hf.set_sent(ACCT, "in2", now - 300, now - 10)            # 已过期、无信号
    repo_hf.insert_judged(ACCT, "in3", GROUP, now - 300)
    repo_hf.set_sent(ACCT, "in3", now - 300, now - 10)
    repo_hf.mark_engaged(ACCT, GROUP, now - 250, 1, "short-window", False, "in3")
    res = repo_hf.close_expired_windows(ACCT, now)
    a = conn.query_one("SELECT engaged, engage_signal, status FROM wpp_hf_ledger "
                       "WHERE account_id = %s AND inbound_msg_id = 'in2'", (ACCT,))
    b = conn.query_one("SELECT engaged, engage_signal, status FROM wpp_hf_ledger "
                       "WHERE account_id = %s AND inbound_msg_id = 'in3'", (ACCT,))
    _check(res["silence"] == 1 and a["engage_signal"] == "silence" and int(a["engaged"]) == 0,
           "整窗无信号 → silence / engaged=0", str(a))
    _check(res["short_window"] == 1 and b["engage_signal"] == "short-window"
           and int(b["engaged"]) == 1,
           "★ 弱信号到期 → 保留 engaged=1（不外推成 silence）", str(b))

    print("⑤ 陈旧 judged 收敛")
    repo_hf.insert_judged(ACCT, "in4", GROUP, now - 4000)
    _check(repo_hf.expire_stale_judged(ACCT, now, now - 3600) == 1,
           "挂太久的 judged → no-deliver-outcome")
    r4 = conn.query_one("SELECT status, suppressed_reason FROM wpp_hf_ledger "
                        "WHERE account_id = %s AND inbound_msg_id = 'in4'", (ACCT,))
    _check(r4["suppressed_reason"] == "no-deliver-outcome", "理由是 no-deliver-outcome", str(r4))

    print("⑥ 读口径")
    scores = repo_hf.list_recent_judged_scores(ACCT, now - 7200)
    _check(len(scores) >= 1 and all(0 <= s["judge_overall"] <= 1 or s["judge_overall"] == 0
                                    for s in scores),
           "可达性素材取到判过的分（0–1）", str(scores[:3]))
    closed = repo_hf.list_closed_since(ACCT, now - 7200)
    _check(all(r["engaged"] is not None for r in closed), "分层素材不含 suppressed 行",
           str(closed))
    _check(all(r["engage_signal"] != "silence" or int(r["engaged"]) == 0 for r in closed),
           "silence 与 engaged 自洽")
    gg = repo_hf.list_distinct_closed_groups(ACCT, now - 7200)
    _check(gg == [GROUP], "已收敛群枚举", str(gg))
    counts = repo_hf.list_sent_counts_recent(ACCT, now - 3600, now - 86400)
    _check(any(c["group_id"] == GROUP and c["last_sent_at"] for c in counts),
           "预算回填素材含 last_sent_at", str(counts))
    cnt = repo_hf.count_by_status(ACCT, now - 7200)
    _check(cnt["total"] >= 4 and "suppressed" in cnt["by_status"], "状态计数", str(cnt))
    sig = repo_hf.count_engage_signals(ACCT, now - 7200)
    _check(sig.get("quote") == 1, "信号分布", str(sig))
    lastrow = repo_hf.get_ledger_last(ACCT, GROUP)
    _check(lastrow is not None and lastrow["inbound_msg_id"], "台账最后一行", str(lastrow)[:120])
    _check(repo_hf.find_ledger_by_bot_msg(ACCT, "bot1") is not None, "按 bot_msg_id 反查")

    print("⑦ 一键否决（两步、按 sent_at 排序）")
    veto = repo_hf.mark_veto(ACCT, GROUP, now)
    _check(veto is not None and veto["id"] > 0, "veto 命中最近发出的那条", str(veto))
    vr = conn.query_one("SELECT engaged, engage_signal, status FROM wpp_hf_ledger "
                        "WHERE id = %s", (veto["id"],))
    _check(int(vr["engaged"]) == 0 and vr["engage_signal"] == "veto"
           and vr["status"] == "closed", "veto 落终态", str(vr))

    print("⑧ 群状态 / 审计 / 画像 / 分层")
    repo_hf.upsert_group_state(ACCT, GROUP, learned_threshold=0.58, last_change_at=now,
                               last_change_old=0.6, last_change_new=0.58,
                               last_change_reason="lift")
    gs = repo_hf.get_group_state(ACCT, GROUP)
    _check(abs((gs or {}).get("learned_threshold", 0) - 0.58) < 1e-6,
           "群状态读回（DECIMAL(6,4) 而非 float 失真）", str(gs))
    repo_hf.upsert_group_state(ACCT, GROUP, learned_threshold=0.57)
    _check(repo_hf.get_group_state(ACCT, GROUP)["learned_threshold"] == 0.57, "群状态可更新")
    _check(len(repo_hf.list_group_states(ACCT)) == 1, "群状态列表")

    repo_hf.log_threshold_change(ACCT, GROUP, 0.58, 30, 12, old_threshold=0.6, reason="test")
    audit = repo_hf.list_threshold_audit(ACCT, GROUP)
    _check(len(audit) == 1 and float(audit[0]["new_threshold"]) == 0.58,
           "阈值审计可读（首调阈判据=首行）", str(audit))

    repo_hf.upsert_profile(ACCT, GROUP, {"tone": "正式"}, stats={"n": 30},
                           sample_msgs=30, model="probe", generated_at=now)
    repo_hf.upsert_profile(ACCT, GROUP, {"tone": "随意"}, sample_msgs=40,
                           model="probe", generated_at=now + 1)
    prof = repo_hf.get_profile(ACCT, GROUP)
    _check(prof["profile"] == {"tone": "随意"} and prof["version"] == 2,
           "画像重生成 version+1 且 JSON 解回 dict", str(prof))
    plist = repo_hf.list_profiles(ACCT)
    _check(len(plist) == 1, "画像列表")
    _check(plist[0].get("profile_json") == '{"tone": "随意"}',
           "★ 画像列表带**原始** JSON（sweep 预热要用它解析，不能只列元数据）", str(plist[0])[:120])
    _check(plist[0]["version"] == 2 and plist[0]["sample_msgs"] == 40,
           "画像列表带版本与样本数", str(plist[0])[:120])
    _check(plist[0].get("stats_json") is None,
           "★ 第二次 upsert 没带 stats ⇒ 快照被覆写成 NULL（不是保留上一版：VALUES(stats_json)）",
           str(plist[0])[:120])

    conn.execute("UPDATE wpp_hf_group_profile SET profile_json = '{坏 JSON' "
                 "WHERE account_id = %s AND group_id = %s", (ACCT, GROUP))
    bad = repo_hf.get_profile(ACCT, GROUP)
    _check(bad is not None and bad["profile"] is None,
           "★ 画像 JSON 坏了 → 按无画像处理（不抛、不喂半个 JSON）", str(bad)[:120])

    repo_hf.upsert_layer_stat(ACCT, GROUP, "daypart", "18-24", 10, 3, now - 86400,
                              ambient_p=0.42)
    repo_hf.upsert_layer_stat(ACCT, GROUP, "daypart", "18-24", 12, 5, now - 86400,
                              ambient_p=0.5)
    ls = repo_hf.list_layer_stats(ACCT)
    _check(len(ls) == 1 and ls[0]["n"] == 12 and ls[0]["engaged"] == 5,
           "★ 分层是快照（覆盖）不是累加器（I3）", str(ls))

    # ---------------------------------------------------------------- 其余仓储
    print("⑨ 联系人 / 群 / 成员 / 黑话 / 红包 / 辅助")
    repo_messages.upsert_contact(ACCT, "wxid_x", "昵称", "备注")
    repo_messages.upsert_contact(ACCT, "wxid_x", "昵称2")
    _check(repo_messages.get_contact(ACCT, "wxid_x")["nickname"] == "昵称2", "联系人 upsert")
    _check(len(repo_messages.list_contacts(ACCT)) == 1, "联系人列表")

    repo_messages.upsert_chatroom(ACCT, GROUP, "测试群", member_count=3)
    _check(repo_messages.get_chatroom(ACCT, GROUP)["member_count"] == 3, "群 upsert")
    repo_messages.upsert_chatroom(ACCT, GROUP, "测试群")
    _check(repo_messages.get_chatroom(ACCT, GROUP)["member_count"] == 3,
           "★ 群 upsert 不清空未提供的字段（COALESCE 保旧）")

    repo_messages.upsert_chatroom_member(ACCT, GROUP, "wxid_x", "昵称", is_owner=1)
    _check(repo_messages.get_chatroom_member(ACCT, GROUP, "wxid_x") is not None, "群成员 upsert")
    _check(len(repo_messages.list_chatroom_members(ACCT, GROUP)) == 1, "群成员列表")

    repo_messages.upsert_jargon(ACCT, GROUP, "黑话", "上下文", "含义")
    repo_messages.upsert_jargon(ACCT, GROUP, "黑话")
    jz = repo_messages.list_jargon(ACCT, GROUP)
    _check(len(jz) == 1 and int(jz[0]["frequency"]) == 2, "黑话词频累加", str(jz))
    _check(repo_messages.has_jargon(ACCT, GROUP, "黑话"), "黑话存在性")

    repo_messages.record_api_call(ACCT, "/Msg/SendTxt", status_code=200, vendor_code=0,
                                 latency_ms=42, request_body={"x": 1}, response_body={"y": 2})
    _check(int(conn.query_one("SELECT COUNT(*) AS n FROM wpp_api_calls WHERE account_id = %s",
                              (ACCT,))["n"]) == 1, "API 调用审计落库")

    repo_messages.record_redpacket(ACCT, GROUP, "wxid_human", "http://hb/1", "k1", {"a": 1})
    repo_messages.record_redpacket(ACCT, GROUP, "wxid_human", "http://hb/1", "k1", {"a": 2})
    rps = repo_messages.list_redpackets(ACCT, GROUP)
    _check(len(rps) == 1, "★ 红包幂等（同 hb_url 不重复）", str(rps))

    repo_messages.save_svrid(ACCT, "100", "md5x", "hashy")
    _check(repo_messages.lookup_svrid(ACCT, "100")["msg_md5"] == "md5x", "svrid 映射")

    repo_messages.set_sync_state(ACCT, "key-1")
    repo_messages.set_sync_state(ACCT, "key-2")
    _check(repo_messages.get_sync_state(ACCT) == "key-2", "sync_state upsert")

    repo_messages.touch_session_state(ACCT, "group", GROUP, "m1")
    repo_messages.touch_session_state(ACCT, "group", GROUP, "m2")
    pend = conn.query_one("SELECT pending_count FROM wpp_session_state "
                          "WHERE account_id = %s AND peer_id = %s", (ACCT, GROUP))
    _check(int(pend["pending_count"]) == 2, "会话状态 pending 累加", str(pend))

    db.close()
    print()
    print(f"{'PASS' if not _FAILS else 'FAIL'} —— 失败 {len(_FAILS)} 条")
    for f in _FAILS:
        print(f"  - {f}")
    return 0 if not _FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
