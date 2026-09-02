#!/usr/bin/env python3
"""wpp-wechat plugin CLI - 多账号 + 健康检查 (Phase 4.2, 2026-09-01).

设计原则 (接老板拍板):
  1. 零依赖 — 只 import plugin 自己的 db.py + config.py
  2. 不真发 vendor — 只调 vendor 轻量 API (GetContractList / Sync)
  3. 不污染生产 DB schema — 加列靠 db._ensure_delivery_columns 幂等触发
  4. 老板可读输出 — emoji + 表格 + 简短摘要

用法:
  python3 -m plugins.wechatpadpro.cli list-accounts
  python3 -m plugins.wechatpadpro.cli check-auth [account_id]
  python3 -m plugins.wechatpadpro.cli health [account_id]
  python3 -m plugins.wechatpadpro.cli delivery-status [account_id] [--limit N]
  python3 -m plugins.wechatpadpro.cli --help
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# 让此脚本可单独执行 + 可作为模块 import
_PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_PLUGIN_DIR.parent.parent))  # ~/.hermes
sys.path.insert(0, str(_PLUGIN_DIR))  # 当前 plugin dir (绝对导入)

# 设 env var 占位 (避免 _get_conn 报错)
os.environ.setdefault("WECHATPRO_DB_PASSWORD", "")

# 预先 import plugin 内的模块 (避免函数内重复 sys.path 调整)
import config as cfg_mod  # type: ignore
import db  # type: ignore

log = logging.getLogger("wpp.cli")


# -------- 命令: list-accounts --------
def cmd_list_accounts(_args) -> int:
    """列出所有 accounts/*.json + agent 路由 (Phase 2.5)."""
    accounts = cfg_mod.list_account_ids({})
    if not accounts:
        print("⚠️ 没有 accounts/*.json 文件")
        return 0
    print(f"\n📋 WeChatPadPro 账号列表 ({len(accounts)} 个):\n")
    print(f"{'账号 ID':<20} {'enabled':<10} {'agent':<25} {'authcode':<20}")
    print("─" * 80)
    for acct_id in accounts:
        cfg = cfg_mod.resolve_account_config(acct_id, {})
        enabled = cfg.get("enabled", True)
        agent = cfg.get("agent", "(missing!)")
        ac = cfg.get("authcode", "")
        ac_display = f"{ac[:4]}...{ac[-4:]}" if len(ac) >= 8 else ("(empty)" if not ac else ac)
        en_str = "✅" if enabled else "❌"
        warn = " ⚠️" if agent == "(missing!)" or agent == "main" or not ac else ""
        print(f"{acct_id:<20} {en_str:<10} {agent:<25} {ac_display:<20}{warn}")
    return 0


# -------- 命令: check-auth --------
async def _check_auth_one(acct_id: str, cfg: dict) -> dict:
    """调 vendor /Friend/GetContractList 验证 authcode (不真发, 仅读)."""
    from api_client import WppClient  # type: ignore
    api_base = cfg.get("apiBaseUrl") or "https://wx.juhe.chat"
    authcode = cfg.get("authcode") or ""
    if not authcode:
        return {"account_id": acct_id, "ok": False, "error": "no authcode"}
    client = WppClient(api_base, authcode)
    try:
        resp = await client.get_contract_list(currentChatRoomContactSeq=0, currentWxcontactSeq=0)
        code = resp.get("Code")
        if code == 0:
            data = resp.get("Data") or {}
            friends = data.get("ContactList", []) if isinstance(data, dict) else []
            return {"account_id": acct_id, "ok": True, "code": 0, "friends_count": len(friends)}
        return {"account_id": acct_id, "ok": False, "code": code, "error": resp.get("Message") or str(resp)}
    except Exception as e:  # noqa: BLE001
        return {"account_id": acct_id, "ok": False, "error": str(e)}


async def _check_auth_all(args) -> int:
    accounts = cfg_mod.list_account_ids({})
    if args.account_id:
        accounts = [args.account_id]
    results = []
    for acct_id in accounts:
        cfg = cfg_mod.resolve_account_config(acct_id, {})
        r = await _check_auth_one(acct_id, cfg)
        results.append(r)
    print(f"\n🔐 Check-auth 结果 ({len(results)} 个账号):\n")
    print(f"{'账号 ID':<20} {'状态':<10} {'code':<8} {'friends':<10} {'message'}")
    print("─" * 80)
    for r in results:
        status = "✅ OK" if r.get("ok") else "❌ FAIL"
        code = r.get("code", "-")
        fc = r.get("friends_count", "-")
        msg = r.get("error") or r.get("message") or ""
        print(f"{r['account_id']:<20} {status:<10} {str(code):<8} {str(fc):<10} {msg[:50]}")
    return 0


def cmd_check_auth(args) -> int:
    return asyncio.run(_check_auth_all(args))


# -------- 命令: health --------
def cmd_health(args) -> int:
    """检查账号健康 (auth + DB + OSS 白名单). 不真发 vendor."""
    # OSS 白名单需要 plugin 启动时 register_accounts 调用, 这里手动触发保证 cli 独立跑也能看
    try:
        from oss_archive import register_accounts  # type: ignore
        register_accounts(set(cfg_mod.list_account_ids({})))
    except Exception:  # noqa: BLE001
        pass

    accounts = cfg_mod.list_account_ids({})
    if args.account_id:
        accounts = [args.account_id]
    print(f"\n🏥 健康检查 ({len(accounts)} 个账号):\n")
    print(f"{'账号 ID':<20} {'authcode':<14} {'DB':<8} {'列已加':<10} {'OSS 白名单':<12}")
    print("─" * 80)
    for acct_id in accounts:
        cfg = cfg_mod.resolve_account_config(acct_id, {})
        ac = cfg.get("authcode", "")
        ac_display = f"{ac[:4]}...{ac[-4:]}" if len(ac) >= 8 else "(empty)"
        # DB 连通性
        try:
            conn = db._get_conn()
            db_ok = "✅" if conn else "❌"
        except Exception:  # noqa: BLE001
            db_ok = "❌"
        # delivery_status 列是否已加
        try:
            rows = db._query(
                "SELECT COUNT(*) AS cnt FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'wpp_messages' AND column_name = 'delivery_status'",
                (),
            )
            col_ok = "✅" if rows and rows[0].get("cnt", 0) > 0 else "❌"
        except Exception:  # noqa: BLE001
            col_ok = "?"
        # OSS 白名单
        try:
            from oss_archive import ALLOWED_ACCOUNTS  # type: ignore
            oss_ok = "✅" if acct_id in ALLOWED_ACCOUNTS else "❌"
        except Exception:  # noqa: BLE001
            oss_ok = "?"
        print(f"{acct_id:<20} {ac_display:<14} {db_ok:<8} {col_ok:<10} {oss_ok:<12}")
    return 0


# -------- 命令: delivery-status --------
def cmd_delivery_status(args) -> int:
    """列出最近出站消息的 delivery 状态 (老板可观测性 — Phase 4.1)."""
    rows = db.list_outbound_with_delivery(args.account_id, args.limit)
    if not rows:
        print("⚠️ 没有出站记录")
        return 0
    print(f"\n📬 出站 delivery 状态 (最近 {len(rows)} 条, account_id={args.account_id or '(all)'}):\n")
    cols = list(rows[0].keys())
    # 计算列宽
    widths = {c: max(len(c), max((len(str(r.get(c, ""))[:30]) for r in rows))) for c in cols}
    header = "  ".join(c.ljust(widths[c])[:widths[c]] for c in cols)
    print(header)
    print("─" * (sum(widths.values()) + 2 * len(cols)))
    for r in rows:
        line = "  ".join(
            str(r.get(c, "") or "")[:30].ljust(widths[c]) for c in cols
        )
        print(line)
    return 0


# -------- 主入口 --------
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="wpp-cli",
        description="wpp-wechat plugin CLI (Phase 4.2, 2026-09-01)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-accounts", help="列出所有 accounts/*.json + agent 路由")

    p_check = sub.add_parser("check-auth", help="调 vendor /Friend/GetContractList 验证 authcode (只读, 不发消息)")
    p_check.add_argument("account_id", nargs="?", help="账号 ID (默认: 全部)")

    p_health = sub.add_parser("health", help="检查账号健康 (auth + DB + OSS 白名单)")
    p_health.add_argument("account_id", nargs="?", help="账号 ID (默认: 全部)")

    p_ds = sub.add_parser("delivery-status", help="列出最近出站消息的 delivery 状态")
    p_ds.add_argument("account_id", nargs="?", help="账号 ID (默认: 全部)")
    p_ds.add_argument("--limit", type=int, default=20, help="最多返回 N 条 (默认 20)")

    args = parser.parse_args()
    handlers = {
        "list-accounts": cmd_list_accounts,
        "check-auth": cmd_check_auth,
        "health": cmd_health,
        "delivery-status": cmd_delivery_status,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())