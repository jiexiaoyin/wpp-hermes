"""全员群成员 → 统一白名单（2026-10-19 项目约定）。

## 规则

白名单**不区分进出站**：在 ``allowFrom`` 里的人可以私聊机器人，机器人也可以主动
发给他；在 ``groupAllowFrom`` 里的群同理。公司员工都在「全员群」里，所以把该群
成员同步进 ``allowFrom`` 就同时解决了「员工能私聊」和「机器人能发给员工」。

## 三个键

* ``allowFrom`` —— **真正生效**的私聊白名单（收发共用）。
* ``employeeAllowFrom`` —— 自动同步的台账。下一次同步时用它区分：

  - 手工加的人（``allowFrom - employeeAllowFrom``）**永不自动移除**；
  - 上次自动加、这次已退群的人才会被移除。

* ``employeeDenyList`` —— **永不自动加入**的 wxid 列表。用于「老板手工把人移出
  白名单后，30 天后又被全员群同步加回来」：把人写进这里，同步会跳过；若此人
  已在自动台账里，本轮会一并移除。⚠️ 它只约束**自动同步**：手工写进 ``allowFrom``
  的人仍然保留（手工意图优先），要彻底禁止请把人从两个名单里都拿掉。

  ``allowFrom`` / ``employeeAllowFrom`` 在**同一次**原子写里落盘
  （:func:`account_store.write_account_keys`），不会出现「名单变了台账没变」的中间态。

## 失败处理

任一全员群拉取失败 ⇒ **整轮不写**。宁可这一轮不同步，也不能因为厂商抖动把
员工从白名单里误删（删名单 = 那人突然收不到、也发不出去）。

## 配置

账号 JSON 里写::

    "employeeGroupIds": [],
    "employeeDenyList": ["wxid_never_add"]

可配多个群（并集）。``employeeDenyList`` 可选（字符串或列表）。没配群 ⇒ 不跑同步，
行为与之前完全一致。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any

log = logging.getLogger(__name__)

#: 厂商查群成员详情（只读）。返回 ``Data.NewChatroomData.ChatRoomMember``。
MEMBER_ENDPOINT = "/Group/GetChatRoomMemberDetail"

#: 老板 2026-10-19 定：每 30 天同步一轮（人员变动不频繁；启动时仍会先跑一轮，
#: 需要立刻刷新用 ``/user sync``）。注意别和群名同步（6h）共用一个频率。
SYNC_INTERVAL_SEC = 30 * 24 * 3600.0

#: 微信的系统号 / 机器人，不是员工。
_NON_PERSON = frozenset({
    "filehelper", "weixin", "fmessage", "medianote", "newsapp",
    "notifymessage", "qqmail", "floatbottle", "brandsessionholder",
    "brandservicesessionholder", "weixinreminder",
})


def parse_members(payload: Mapping[str, Any], *, exclude: Iterable[str] = ()) -> list[tuple[str, str]]:
    """从厂商响应里取 ``(wxid, 群昵称)``，去掉机器人自己和系统号。

    只看 ``Data.NewChatroomData.ChatRoomMember``（2026-10-19 实测形状）；
    同时兼容 camelCase，避免厂商大小写漂移时整轮同步静默归零。
    """
    data = payload.get("Data") or payload.get("data") or {}
    if not isinstance(data, Mapping):
        return []
    new_data = data.get("NewChatroomData") or data.get("newChatroomData") or {}
    if not isinstance(new_data, Mapping):
        return []
    rows = new_data.get("ChatRoomMember") or new_data.get("chatRoomMember") or []
    if not isinstance(rows, list):
        return []

    skip = {str(x or "").strip() for x in exclude}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        wxid = str(row.get("UserName") or row.get("userName") or row.get("wxid") or "").strip()
        if not wxid or wxid in skip or wxid in seen:
            continue
        # 群 id / 公众号 / 系统号都不是"员工"
        if wxid.endswith("@chatroom") or wxid.startswith("gh_"):
            continue
        if wxid.lower() in _NON_PERSON:
            continue
        seen.add(wxid)
        nick = str(row.get("NickName") or row.get("nickName") or "").strip()
        out.append((wxid, nick))
    return out


def plan_sync(
    current_allow: Iterable[str],
    previous_auto: Iterable[str],
    members: Iterable[str],
    deny: Iterable[str] = (),
) -> tuple[list[str], list[str]]:
    """算出新的 ``(allowFrom, employeeAllowFrom)``。

    * 手工项 = 当前 ``allowFrom`` − 上次自动项（保序）；
    * 新自动项 = 本次群成员 − 手工项 − ``deny``（避免把手工项"吞"进自动台账，
      也避免被手工移除的人下一轮又被加回来）；
    * ``allowFrom`` = 手工项 + 新自动项。

    ``deny`` 只约束自动同步：同时出现在手工项里的人仍然保留（手工意图优先）。
    """
    current = list(dict.fromkeys(_clean(current_allow)))
    prev = set(_clean(previous_auto))
    manual = [x for x in current if x not in prev]
    manual_set = set(manual)
    denied = set(_clean(deny))
    new_auto = [x for x in dict.fromkeys(_clean(members))
                if x not in manual_set and x not in denied]
    return [*manual, *new_auto], new_auto


def _clean(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items or ():
        val = str(item or "").strip()
        if val:
            out.append(val)
    return out


def _string_list(raw: Mapping[str, Any], key: str) -> list[str]:
    """读字符串或字符串列表（去空、去重、保序）。"""
    vals = raw.get(key)
    if isinstance(vals, str):
        vals = [vals]
    if not isinstance(vals, (list, tuple)):
        return []
    return list(dict.fromkeys(
        str(x).strip() for x in vals if str(x).strip()))


def group_ids_from_raw(raw: Mapping[str, Any]) -> list[str]:
    """读 ``employeeGroupIds``（复数优先，兼容单数 ``employeeGroupId``）。"""
    out = _string_list(raw, "employeeGroupIds")
    single = str(raw.get("employeeGroupId") or "").strip()
    if single and single not in out:
        out.append(single)
    return list(dict.fromkeys(out))


def deny_list_from_raw(raw: Mapping[str, Any]) -> list[str]:
    """读 ``employeeDenyList``（字符串或列表）—— 永不自动加入的 wxid。"""
    return _string_list(raw, "employeeDenyList")


async def sync_once(
    client: Any,
    account_id: str,
    group_ids: Iterable[str] | None = None,
    *,
    adapter: Any = None,
    deny_list: Iterable[str] | None = None,
) -> dict[str, Any]:
    """拉取全员群成员并写回 ``allowFrom`` / ``employeeAllowFrom``。

    ``deny_list`` 不传时现读账号配置 ``employeeDenyList``。
    返回 ``{ok, groups, members, added, removed, changed, error}``。
    **任一 group 失败 ⇒ 不写**（见模块 docstring）。
    """
    from ..core import account_store

    raw = account_store.read_account_json(account_id)
    groups = [str(g).strip() for g in (group_ids if group_ids is not None
                                       else group_ids_from_raw(raw)) if str(g).strip()]
    deny = (list(deny_list) if deny_list is not None
            else deny_list_from_raw(raw))
    if not groups:
        return {"ok": False, "error": "未配置 employeeGroupIds", "groups": [],
                "members": 0, "added": 0, "removed": 0, "changed": False}
    self_wxid = ""
    if adapter is not None:
        try:
            self_wxid = str(adapter.account(account_id).self_wxid or "").strip()
        except Exception as exc:  # noqa: BLE001 —— 拿不到就只按 raw 里的
            log.debug("[WPP:%s] 取 self_wxid 失败，退回 raw.selfWxid: %s", account_id, exc)
    self_wxid = self_wxid or str(raw.get("selfWxid") or "").strip()

    found: list[str] = []
    errors: list[str] = []
    for group_id in groups:
        try:
            payload = await client.call(MEMBER_ENDPOINT, {"QID": group_id}, audit=False)
            code = payload.get("Code")
            if code is not None and code != 0:
                raise RuntimeError(f"Code={code}")
            rows = parse_members(payload, exclude=(self_wxid,))
            if not rows:
                raise RuntimeError("响应里没有成员（形状不对或群为空）")
            found.extend(wxid for wxid, _ in rows)
        except Exception as exc:  # noqa: BLE001 —— 任一失败整轮不写
            errors.append(f"{group_id}: {exc}")
            log.warning("[WPP:%s] 全员群成员拉取失败 %s: %s", account_id, group_id, exc)

    if errors:
        return {"ok": False, "error": "; ".join(errors), "groups": groups,
                "members": 0, "added": 0, "removed": 0, "changed": False}

    # ⚠️ 读-规划-写必须在同一个跨进程锁里完成：拉成员是网络动作（可能几秒），
    #    期间控制台可能刚改过 allowFrom；拿旧 raw 规划会把那次改动覆盖掉。
    applied: dict[str, Any] = {
        "changed": False, "added": [], "removed": [], "members": 0,
    }

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        current = [str(x) for x in (data.get("allowFrom") or [])]
        previous_auto = [str(x) for x in (data.get("employeeAllowFrom") or [])]
        new_allow, new_auto = plan_sync(current, previous_auto, found, deny)
        applied.update(
            changed=new_allow != current or new_auto != previous_auto,
            added=[x for x in new_allow if x not in current],
            removed=[x for x in previous_auto if x not in new_allow],
            members=len(new_auto),
        )
        data["allowFrom"] = new_allow
        data["employeeAllowFrom"] = new_auto
        return data

    _data, _mtime = account_store.update_account(
        account_id, _mutate,
        changed_keys={"allowFrom", "employeeAllowFrom"},
        skip_if_unchanged=True)
    changed = bool(applied["changed"])
    added = list(applied["added"])
    removed = list(applied["removed"])
    if changed and adapter is not None:
        try:
            adapter.reload_account(account_id)
        except Exception as exc:  # noqa: BLE001 —— 已落盘，失败只报
            log.warning("[WPP:%s] 全员群同步后热重载失败（重启后生效）: %s",
                        account_id, exc)
    # 无变化也留一行：否则「任务到底跑没跑」只能靠猜（启动日志里看不出来）。
    log.info("[WPP:%s] 全员群同步：成员 %d 人，新增 %d，移除 %d%s",
             account_id, int(applied["members"]), len(added), len(removed),
             "" if changed else "（无变化）")
    return {"ok": True, "error": "", "groups": groups,
            "members": int(applied["members"]),
            "added": len(added), "removed": len(removed), "changed": changed}


async def sync_forever(
    client: Any,
    account_id: str,
    get_group_ids: Any,
    *,
    adapter: Any = None,
    interval_sec: float = SYNC_INTERVAL_SEC,
) -> None:
    """连接期常驻任务：立即同步一轮，之后定期刷新；``CancelledError`` 原样抛出。

    ``get_group_ids()`` 每轮现读账号配置 ⇒ 命令改了名单不用重连。
    """
    while True:
        try:
            group_ids = get_group_ids() if callable(get_group_ids) else get_group_ids
            if group_ids:
                res = await sync_once(client, account_id, group_ids, adapter=adapter)
                if adapter is not None:
                    try:
                        adapter._employee_sync_state[account_id] = {
                            "at": int(time.time()),
                            "ok": bool(res.get("ok")),
                            "members": int(res.get("members") or 0),
                            "error": str(res.get("error") or ""),
                        }
                    except Exception as exc:  # noqa: BLE001
                        log.debug("[WPP:%s] 写全员群同步状态失败（不影响同步结果）: %s",
                                  account_id, exc)
                if not res.get("ok") and res.get("error"):
                    log.warning("[WPP:%s] 全员群同步未生效: %s", account_id, res["error"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 下一轮继续
            log.warning("[WPP:%s] 全员群同步 tick 失败（下一轮继续）: %s", account_id, exc)
        await asyncio.sleep(interval_sec)
