---
name: wpp-identity
description: "昵称↔wxid 快速匹配。多源查询：本地通讯录/群成员表 + 微信通讯录(wpp_friend_contract_detail) + USER.md 已知用户表，统一入口 wpp_identity_lookup；员工身份可再用 phoneerp skill 的 lookup_employee 查。用于「这个wxid是谁」「张三的wxid是什么」「刚发消息的人是谁」。⚠️ 群消息脱敏/身份识别前必先查身份，不推断不凭记忆。"
---

# wpp-identity — 昵称 ↔ wxid 匹配

## 适用 agent

| Agent | 是否适用 | 原因 |
|-------|---------|------|
| main | ✅ | 适用 |
| wpp-wechat | ✅ | 适用 |
| wecom | ❌ | 不适用 (wpp-identity 与该 agent 职责不匹配) |

> **Note**: 微信身份匹配 (脱敏判断) — main + wpp-wechat


> 用于群消息**脱敏判断**（识别发言者身份）+ 日常「这人是谁」查询。**跨多数据源**，优先级从高到低。

## 数据源（查询顺序）

| 优先级 | 数据源 | 查询方式 | 用途 |
|---|---|---|---|
| 0 | **统一入口（首选）** | `wpp_identity_lookup(name=...)` 或 `wpp_identity_lookup(wxid=...)` | 本地通讯录结果（员工信息另查 phoneerp skill）|
| 1 | **本地通讯录表** `wpp_contacts` | `SELECT wxid,nickname,remark FROM wpp_contacts WHERE ...` | 毫秒级双向匹配 |
| 2 | **本地群成员表** `wpp_chatroom_members` | `SELECT wxid,nickname FROM wpp_chatroom_members WHERE chatroom_id=? AND wxid=?` | 群成员身份（脱敏判断）|
| 3 | 微信通讯录 | `wpp_friend_contract_detail({userName: "<wxid>"})` | 实时兜底（表没命中）|
| 4 | phoneerp 员工表 | `lookup_employee --wppId <wxid>` / `--name <昵称>`（phoneerp skill 的脚本，`node .../scripts/lookup_employee.js`） | 员工姓名/职位/门店 |
| 5 | USER.md 已知用户表 | 读文件 | 高频联系人映射 |
| 6 | 历史消息 | wpp-history 查 from_wxid | 兜底 |

> ⚠️ 工具名以**本插件实际注册的名字**为准（`wpp_*`）。`getContactDetail` / `searchContact` 是 OpenClaw 旧名，已废弃 —— 对应 `wpp_friend_contract_detail` / `wpp_friend_search`。

> ⚠️ **优先查本地表**（`wpp_contacts` / `wpp_chatroom_members`，秒级 SQL），表没命中再实时调 vendor；
> 员工身份/门店信息属于 phoneerp skill 的职责，用它的 `lookup_employee` 查，不在本插件里重复实现。
> 表由 `scripts/sync-contacts.ts` 同步（启动/手动触发）。

## 群脱敏判断（isInternalGroup）

用 `wpp_chatroom_members` 快速判断群是否内部群（无需实时调 vendor）：

```sql
-- 某群成员列表（先看本地群成员表）
SELECT m.wxid, m.nickname
FROM wpp_chatroom_members m
WHERE m.chatroom_id = '<chatroom_id>'
LIMIT 20;
```

> 白名单群（`internalGroups`）直接内部群；非白名单群再用 phoneerp skill 查成员是否全在员工表
> （跨库查询属于 phoneerp 的部署细节，本插件不假设 `phoneerp` 库存在）。

## 双向查询

### wxid → 昵称
```javascript
// 0. 统一入口 (推荐: 一次拿全)
wpp_identity_lookup({ wxid: "wxid_abc123" })
// 返回 [{source: wpp_contacts, wxid, nickname, remark}, ...]

// 1. 通讯录 (微信昵称/备注)
wpp_friend_contract_detail({ userName: "wxid_abc123" })
// 返回 NickName / Remark

// 2. 员工表 (正式姓名)
lookup_employee --wppId wxid_abc123
// 返回 name / wechatNickName / title / store

// 3. 兜底: USER.md 已知用户表
```

### 昵称 → wxid
```javascript
// 0. 统一入口 (推荐)
wpp_identity_lookup({ name: "张三" })

// 1. 员工表 (正式姓名/微信昵称)
lookup_employee --name "张三"
// 返回 wppId

// 2. 搜索通讯录
wpp_friend_search({ keyword: "张三" })
// 返回匹配好友

// 3. 兜底: USER.md 已知用户表
```

## 铁律

1. **不推断 / 不凭记忆**：身份识别必查数据源，找不到标记 "unmapped wxid"（不可乱猜是谁）
2. **员工判定**：wxid 在 phoneerp 员工表（有 wppId）→ 是员工；否则外部联系人
3. **群脱敏前置**：群消息通报 phoneerp 数据前，先查发言者身份判断是否内部群（结合 wpp-friendcircle 脱敏规则）
4. **优先通讯录**：微信昵称 vs 员工正式名，以通讯录为准（日常称呼）
5. **机器人自己**：当前账号的 `selfWxid`（见 `wpp_accounts_context`）是 WPP 账号自己，不是员工

## 工作流

### 「刚在群里发消息的人是谁」
```
1. 拿发言者 wxid (from_wxid)
2. wpp_identity_lookup({wxid}) → 本地表
3. 表没命中 → wpp_friend_contract_detail({userName: wxid}) → 微信昵称
4. 需要员工职位/门店 → phoneerp skill 的 lookup_employee
5. 综合返回: "张三 (3号店店员, 微信昵称 阿三)"
```

### 「张三的 wxid 是什么」
```
1. wpp_identity_lookup({name:"张三"}) → 本地表
2. 若无 → wpp_friend_search({keyword:"张三"}) → 通讯录匹配
3. 仍无且确定是员工 → phoneerp skill 的 lookup_employee --name "张三"
4. 返回 wxid
```

## 相关
- phoneerp skill：`lookup_employee`（员工身份 + 脱敏）
- wpp-friendcircle skill：群通报脱敏规则
- wpp-history skill：历史消息查 from_wxid 兜底
