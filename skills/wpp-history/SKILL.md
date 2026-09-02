---
name: wpp-history
description: "WPP 入站消息历史查询。wechatpadpro 插件把所有消息（文本/图片/语音/视频/文件/引用/事件）持久化到 MySQL wechatpro 库 wpp_messages 表，本技能封装常见查询 SQL 与典型场景。用于「查某人发过的图」、「统计某个群今天的活跃度」、「找一段聊天记录」。⚠️ 仅有插件记录的消息，更早数据不可查。v1.3.x 从 gewe-history 适配 (wpp_messages 表结构, msg_type 用数字)。"
---

# wpp-history — WPP 消息历史查询

## 适用 agent

| Agent | 是否适用 | 原因 |
|-------|---------|------|
| main | ✅ | 适用 |
| wpp-wechat | ✅ | 适用 |
| wecom | ❌ | 不适用 (wpp-history 与该 agent 职责不匹配) |

> **Note**: 微信消息历史查询 — main (查任意会话) + wpp-wechat (微信主通道)


> 个人微信**没有云端历史拉取 API**。wechatpadpro 插件把消息存到 MySQL `wechatpro.wpp_messages` 表，本技能是这套数据的查询封装。
> 查微信消息前先确认 peer_id（对方 wxid 或群 `xxx@chatroom`），再来这里查。

## 数据表结构（wpp_messages）

> 源: `src/storage/db/mysql.ts` (ensureTables) + `src/storage/db/messages.ts`

```sql
-- wechatpro.wpp_messages 实际表结构 (精简)
CREATE TABLE wpp_messages (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    account_id    VARCHAR(64) NOT NULL,          -- 多账号隔离 (默认 'default')
    msg_id        VARCHAR(128),                  -- 微信消息 ID
    new_msg_id    VARCHAR(128),                  -- vendor new_msg_id
    direction     ENUM('inbound','outbound'),    -- 入站/出站
    peer_kind     ENUM('direct','group','room'), -- direct=单聊 group=群 room=房间
    peer_id       VARCHAR(128) NOT NULL,         -- 对端: 单聊=对方wxid, 群=chatroomId(@chatroom结尾)
    chat_id       VARCHAR(128),                  -- 群 ID (群聊)
    msg_type      VARCHAR(32),                   -- 数字: 1=文本 3=图片 34=语音 43=视频 47=表情 48=位置 49=链接/App 42=名片 10002=撤回 quote=引用
    content       LONGTEXT,                      -- 文本内容 / 媒体标记
    raw_payload   LONGTEXT,                      -- 原始 vendor payload
    from_wxid     VARCHAR(128),                  -- 发送者 wxid
    create_time   BIGINT,                        -- 秒级 Unix 时间戳
    ts            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_account_ts (account_id, ts),
    INDEX idx_peer (peer_kind, peer_id),
    INDEX idx_sender (peer_kind, peer_id, from_wxid)
);
```

## msg_type 数字对照 (WPP MsgType 常量)

| msg_type | 含义 | gewe 对应 |
|---|---|---|
| `1` | 文本 TEXT | TEXT |
| `3` | 图片 IMAGE | IMAGE |
| `34` | 语音 VOICE | VOICE |
| `43` | 视频 VIDEO | VIDEO |
| `47` | 表情 EMOJI | — |
| `48` | 位置 LOCATION | LOCATION |
| `49` | 链接/小程序/**接龙 App** | APP_MSG |
| `42` | 名片 CARD | CARD |
| `10002` | 撤回 REVOKE | — |
| `quote` | 引用回复 (outbound) | QUOTE |

> ⚠️ **接龙消息是 msg_type=49**：群里发的接龙（如 `#接龙`）是 App 消息，不是普通文本。查询时必须同时查 `msg_type IN ('1','49')`，否则漏掉所有接龙。

> **图片/视频的媒体 URL 在 `content` 里**：格式 `[图片] <公网URL>` / `[视频] <url>` / `[文件] <url>` / `[语音] <url>` (WPP media-enrich 注入)。

## 快速参考

| 想查什么 | 关键过滤 |
|---|---|
| 某好友最近文本 | `peer_id='wxid_xxx' AND peer_kind='direct' AND msg_type='1'` |
| 某好友发过的图 | `from_wxid='wxid_xxx' AND msg_type='3'` |
| 某个群今天的活跃 | `peer_id='12345@chatroom' AND peer_kind='group' AND ts >= CURDATE()` |
| 群里某人的消息 | `peer_id='xxx@chatroom' AND from_wxid='wxid_xxx'` |
| 找包含关键词的文本（含接龙） | `msg_type IN ('1','49') AND content LIKE '%关键词%'` |
| 查群里接龙 | `msg_type IN ('1','49') AND content LIKE '%接龙%'` |
| 统计某人/某群消息量 | `GROUP BY from_wxid` + `COUNT(*)` |
| 区分群 vs 单聊 | `peer_kind='group'` (群) / `peer_kind='direct'` (单聊) |
| 查自己发过的（出站） | `direction='outbound'` |

> **multi-account 铁律**：所有查询必须带 `account_id = 'default'` (或具体账号 id) 过滤，避免跨账号串数据。

## 常用查询模板（wpp_messages 字段名）

### 某好友最近 10 条文本

```sql
SELECT from_wxid, content, ts
FROM wpp_messages
WHERE peer_id = 'wxid_abc123'
  AND peer_kind = 'direct'
  AND account_id = 'default'
  AND msg_type = '1'
ORDER BY ts DESC
LIMIT 10;
```

### 某好友发过的图片

```sql
SELECT msg_id, from_wxid, content, ts
FROM wpp_messages
WHERE from_wxid = 'wxid_abc123'
  AND account_id = 'default'
  AND msg_type = '3'
ORDER BY ts DESC
LIMIT 20;
```

> ⚠️ **WPP 媒体 URL 两种情况**：
> 1. **enrich 成功** → content 追加 `[图片] <公网URL>`（如 `收到一张图片\n[图片] https://openclaw-a.oss.../xxx.jpg`），直接用 content 里的 URL
> 2. **enrich 未完成/失败** → content 只是"收到一张图片"，媒体 URL 在 `raw_payload` JSON 的 `image.cdn_download_contexts[].file_no`（CDN 下载凭据）
>
> 查询媒体时优先用 content 的 `[图片] URL`；没有再用 JSON_EXTRACT：
>
> ```sql
> -- 提取 raw_payload.image.cdn_download_contexts 的 CDN 凭据 (enrich 失败的兜底)
> SELECT from_wxid, content, ts,
>   JSON_UNQUOTE(JSON_EXTRACT(raw_payload, '$.image.cdn_download_contexts[0].file_no')) AS cdn_file_no
> FROM wpp_messages
> WHERE msg_type = '3' AND raw_payload LIKE '%cdn_download_contexts%'
> ORDER BY ts DESC LIMIT 20;
> ```
>
> 展示时说明这是媒体消息（图/视频/语音），是否发回需先调插件重新下载上传（见「⚠️ 不要自动发回」）。

### 某个群今天的活跃度

```sql
SELECT
  from_wxid,
  COUNT(*) AS msg_count
FROM wpp_messages
WHERE peer_id = '12345@chatroom'
  AND peer_kind = 'group'
  AND account_id = 'default'
  AND ts >= CURDATE()
  AND ts < CURDATE() + INTERVAL 1 DAY
GROUP BY from_wxid
ORDER BY msg_count DESC
LIMIT 20;
```

### 群里包含关键词的文本（含接龙）

```sql
SELECT from_wxid, msg_type, content, ts
FROM wpp_messages
WHERE peer_id = '12345@chatroom'
  AND peer_kind = 'group'
  AND account_id = 'default'
  AND msg_type IN ('1','49')      -- ⚠️ 必须同时查 49，否则漏掉接龙
  AND content LIKE '%价格%'
ORDER BY ts DESC
LIMIT 50;
```

### 查群里接龙（通用模板）

```sql
-- 查某群最近接龙（不限定关键词，直接拉所有 App 消息 + 含接龙关键词的文本）
SELECT from_wxid, msg_type, content, ts
FROM wpp_messages
WHERE peer_id = 'chatroom_demo_4@chatroom'
  AND peer_kind = 'group'
  AND account_id = 'default'
  AND msg_type IN ('1','49')
  AND (
    msg_type = '49'
    OR content LIKE '%接龙%'
  )
ORDER BY ts DESC
LIMIT 30;
```

### 某段时间内某好友的所有消息（多类型）

```sql
SELECT msg_type, content, ts
FROM wpp_messages
WHERE peer_id = 'wxid_abc123'
  AND account_id = 'default'
  AND ts BETWEEN '2026-05-01 00:00:00' AND '2026-06-01 00:00:00'
ORDER BY ts DESC
LIMIT 100;
```

### 找转发/引用的聊天记录

```sql
SELECT from_wxid, content, ts
FROM wpp_messages
WHERE account_id = 'default'
  AND msg_type = 'quote'          -- 引用回复
  OR (msg_type = '49' AND content LIKE '%type=57%')  -- 聊天记录 App
ORDER BY ts DESC
LIMIT 20;
```

## ⚠️ 重要规则

### 1. 展示查询结果时保护隐私

- 多个用户的消息混在一起时，**用 from_wxid 区分**（昵称从 peer_name 或通讯录查）
- 长文本要**截断展示**（前 100 字 + 「...」）
- 涉及金额、身份证、手机号等敏感字段时**打码**

### 2. 媒体不要自动发回

查到的 content 里媒体 URL：
- ✅ **可以**：展示「以下是你查到的图片」+ URL 列表
- ❌ **不可以**：直接把 URL 重新 `sendMessage` 出去（CDN URL 7 天过期）
- 正确做法：如果用户要重新发，先调插件重新上传

### 3. 群消息会重复计数

群里**自己发的消息也会入库**（`from_wxid` 是自己的 wxid，direction='outbound'）。统计活跃时要排除：

```sql
WHERE from_wxid != 'wxid_robot_demo'  -- 排除自己 (WPP 账号 selfWxid)
```

### 4. 时间戳转换

`ts` 是 TIMESTAMP（直接可读），`create_time` 是秒级 Unix。转可读：

```sql
SELECT FROM_UNIXTIME(create_time) AS time
```

### 5. 大结果集要分页

```sql
LIMIT 50 OFFSET 100   -- 第 3 页
```

### 6. 多账号铁律

所有查询必须带 `account_id = 'default'` 过滤，避免跨账号串数据。

## 典型工作流

### 「张三这周给我发过哪些图」

```
1. 确认张三 wxid (通讯录/消息里查)
2. SQL 查:
   SELECT content, ts FROM wpp_messages
   WHERE from_wxid = 'wxid_张三'
     AND account_id = 'default'
     AND msg_type = '3'
     AND ts >= NOW() - INTERVAL 7 DAY
   ORDER BY ts DESC;
3. 展示 content 里的图片 URL 列表
4. ⚠️ 不要自动发回；问用户「要不要把某张图发回去？」
```

### 「3 号店群今天谁最活跃」

```
1. 确认群 chatroomId
2. SQL 查当日发言排行:
   SELECT from_wxid, COUNT(*) AS msg_count
   FROM wpp_messages
   WHERE peer_id = '12345@chatroom'
     AND peer_kind = 'group'
     AND account_id = 'default'
     AND ts >= CURDATE()
   GROUP BY from_wxid
   ORDER BY msg_count DESC
   LIMIT 20;
3. 展示:
   📊 3号店群 今日活跃度：
   1. 张三 - 28 条
   2. 李四 - 15 条
```

### 「找一下那段『价格表』的聊天记录」

```
1. 用 LIKE '%价格%' 查最近 7 天:
   SELECT from_wxid, content, ts FROM wpp_messages
   WHERE account_id = 'default'
     AND msg_type = '1'
     AND content LIKE '%价格%'
     AND ts >= NOW() - INTERVAL 7 DAY
   ORDER BY ts DESC;
2. 展示 from_wxid + 时间 + 内容前 100 字
3. 命中多条 → 展示列表让用户选
```

### DB 连接

```bash
# 凭证在 ~/.hermes/skills/phoneerp/credentials/phoneerp_db.json (host 127.0.0.1 user root db phoneerp)
# WPP 消息在 wechatpro 库 (user wechatpro, password env WECHATPRO_DB_PASSWORD)
mysql -h 127.0.0.1 -u wechatpro -p"$WECHATPRO_DB_PASSWORD" wechatpro -e "SELECT ..."
```

## 与 gewe-history 的差异（适配说明）

| gewe messages | wpp_messages | 说明 |
|---|---|---|
| `chat_id` | `peer_id` | 对端标识 (群=@chatroom) |
| `sender_wxid` | `from_wxid` | 发送者 |
| `msg_type` TEXT/IMAGE | `msg_type` 1/3 | 数字枚举 |
| `timestamp` (秒) | `ts` (TIMESTAMP) | 时间 |
| — | `direction` | 加出入站过滤 |
| — | `peer_kind` | 群/单聊显式判定 |
