#!/usr/bin/env python3
"""一次性：把各群成员昵称补进 ``wpp_chatroom_members``。

正常路径是**事件驱动**的（进群通知 → 拉该群，见 ``inbound/join_events.py``）。
但历史数据没有事件可触发 —— 成员表停在 2026-08-11，之后入群的人查不到昵称，
心流 prompt 里只剩 ``wxid_xxx``。这个脚本把欠的账一次性补上。

**只跑一次**。之后靠进群事件增量维护，别再把它塞进 cron。

用法（生产，网关同款运行时 + profile 环境变量）::

    set -a; . /root/.hermes/profiles/wpp-wechat/.env; set +a
    PYTHONPATH=/root/.hermes/plugins \\
      "$(readlink -f /proc/$(systemctl show -p MainPID --value hermes-gateway.service)/exe)" \\
      tools/sync_members_once.py [account_id]

安全性：只调两个**只读**端点（``/Group/List``、``/Group/GetChatRoomMemberDetail``）
+ upsert 本地表；``send_allowlist`` 传空元组，物理上发不出任何消息。
"""

from __future__ import annotations

import asyncio
import sys


async def main(account_id: str = "default") -> int:
    from wechatpadpro.core import accounts as accounts_mod
    from wechatpadpro.core.api_client import WppClient
    from wechatpadpro.inbound import name_sync

    accts = accounts_mod.load_all()
    acct = accts.get(account_id)
    if acct is None:
        print(f"[FAIL] 账号 {account_id!r} 不存在（现有：{sorted(accts)}）")
        return 1

    client = WppClient(
        base_url=acct.api_base_url,
        authcode=acct.authcode,
        account_id=acct.id,
        timeout=60,
        audit=False,
        send_allowlist=(),          # 只读脚本：物理上发不出消息
    )
    try:
        res = await name_sync.sync_members(client, acct.id)
    finally:
        await client.close()

    print(f"[OK] {account_id}: 写入 {res['written']} 条"
          f"（新增 {len(res['added'])} / 移出 {len(res['removed'])}）")
    for wxid, nick in res["added"]:
        print(f"  + {nick or '?':20s} {wxid}")
    for wxid, nick in res["removed"]:
        print(f"  - {nick or '?':20s} {wxid}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(*(sys.argv[1:] or ["default"]))))
