-- WeChatPadPro Hermes Plugin - MariaDB Schema (Python 侧 DDL 唯一真源)
--
-- ## 出处与「双写陷阱」
--
-- 前 15 张表的 DDL **逐字搬自** TS 侧 `/root/dev/wechatpadpro-openclaw/db/schema.sql`
-- （v1.11.0）；16 号表 `wpp_hf_image_caption` 搬自 TS v1.16.0（`src/storage/db/mysql.ts`
-- 行内 DDL，TS 的 db/schema.sql 尚未同步该表）。TS 自己在 240/253 行写着「⚠️ 与 src/storage/db/mysql.ts 的行内 DDL
-- 必须同步（改一处必改另一处）」—— 那是个**双写陷阱**：它已经把
-- `wpp_svrid_mapping` 写歪过一次。
--
-- Python 侧**不重复这个错误**：本文件是唯一 DDL 真源，
-- `migrations.py` 的 `ensure_column` / `ensure_index` 全部**从本文件反推**，
-- 不手写第二份 DDL。
--
-- ## 表数（20）
--
--   8  legacy       : wpp_accounts / wpp_messages / wpp_contacts / wpp_chatrooms /
--                     wpp_session_state / wpp_api_calls / wpp_chatroom_members /
--                     wpp_jargon_terms
--   6  心流          : wpp_hf_ledger / wpp_hf_group_state / wpp_hf_threshold_audit /
--                     wpp_hf_group_profile / wpp_hf_layer_stat / wpp_hf_image_caption
--   2  辅助          : wpp_svrid_mapping / wpp_sync_state
--   4  Python 独有   : wpp_redpackets   （TS 无对应；从旧树 db.py:357 record_redpacket 反推）
--                     wpp_affection    （TS 无对应；TS 把好感度/情绪放在进程内 Map，重启清零 ——
--                                        老板 2026-10-02 拍板落库，见文件末尾那张表的注释）
--                     wpp_style_cards  （TS 无对应；2026-10-19 自主学习「说话风格」当前版本）
--                     wpp_style_card_versions（同上；每次生成/回滚留一版，供回滚审计）
--
-- ⚠️ 计划里写的「14 张表 / 9 张 legacy」是错的 —— 亲数 TS DDL（v1.16.0）为
--    **16 个 CREATE TABLE，其中 legacy 8 个**。以本文件为准。
--
-- ## 两条必须记住的语义
--
-- 1. **`ts` 与 `create_time` 是两回事**
--    - `create_time` BIGINT = 消息**实际时间戳（秒）**，2026-08-11 起全是 NULL
--      （更早写的是 `YYYYMMDDHHMMSS` 这种非 epoch 值）
--    - `ts` TIMESTAMP = **入库时间**
--    ⇒ **所有时间聚合/排序必须走 `ts`**，用 `create_time` 会得到 NULL/错序。
--
-- 2. **`effective_threshold` 是 0–1；`dim_*` 是 0–10**（尺度不同，别混）

-- ============================================================ legacy

-- 账号元数据 (单账号 demo 阶段 1 行, 多账号阶段每账号 1 行)
CREATE TABLE IF NOT EXISTS wpp_accounts (
  id INT AUTO_INCREMENT PRIMARY KEY,
  account_id VARCHAR(64) NOT NULL UNIQUE,
  display_name VARCHAR(128),
  self_wxid VARCHAR(128),
  nickname VARCHAR(128),
  enabled TINYINT(1) DEFAULT 1,
  config_json JSON,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_self_wxid (self_wxid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 消息持久化 (inbound + outbound 全量存档)
CREATE TABLE IF NOT EXISTS wpp_messages (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  account_id VARCHAR(64) NOT NULL,
  msg_id VARCHAR(128),                  -- wechatpadpro msg id (uuid)
  new_msg_id VARCHAR(128) NOT NULL DEFAULT '',  -- wechatpadpro new_msg_id（NULL 会绕过唯一键）
  direction ENUM('inbound', 'outbound') NOT NULL,
  peer_kind ENUM('direct', 'group', 'room') NOT NULL,
  peer_id VARCHAR(128) NOT NULL,        -- sender (inbound) / target (outbound)
  peer_name VARCHAR(256),
  chat_id VARCHAR(128),                 -- 群 ID (peer_kind=group)
  msg_type VARCHAR(32),                 -- text / image / video / voice / ...
  content LONGTEXT,                     -- 文本或 JSON
  raw_payload JSON,                     -- 原始 vendor payload (审计 + 回放)
  from_wxid VARCHAR(128),               -- v1.2.4: inbound 发送者 wxid (群聊按人查历史)
  create_time BIGINT NULL,              -- v1.1.27 撤回用: 消息实际时间戳 (秒), 入库时同步 ts→floor(ts/1000)
  ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  -- 时间聚合/排序**只走 ts**（create_time 是遗留死列，见文件头）
  INDEX idx_account_ts (account_id, ts),
  INDEX idx_account_chat_ts (account_id, chat_id, ts),       -- 按群查历史
  INDEX idx_account_peer_ts (account_id, peer_id, ts),       -- get-history-from-peer
  INDEX idx_account_msgtype_ts (account_id, msg_type, ts),   -- 按消息类型查
  -- ⚠️ TS 侧同时有 idx_peer 与 idx_peer_kind_peer_id，**列完全相同**（重复索引）。
  --    Python 只留这一个，名字用 schema.sql 里的 idx_peer。
  INDEX idx_peer (peer_kind, peer_id),
  INDEX idx_sender (peer_kind, peer_id, from_wxid),  -- v1.2.4: 按人查群聊历史
  INDEX idx_msg_id (msg_id),
  INDEX idx_new_msg_id (new_msg_id),
  -- v1.1.17 FULL-FIX (P0-G): UNIQUE 去重约束 (三通道重复入库防护)
  UNIQUE KEY uk_account_msg (account_id, msg_id, new_msg_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 联系人 (轻量, 仅缓存常用字段)
CREATE TABLE IF NOT EXISTS wpp_contacts (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  account_id VARCHAR(64) NOT NULL,
  wxid VARCHAR(128) NOT NULL,
  nickname VARCHAR(256),
  remark VARCHAR(256),
  avatar_url VARCHAR(512),
  gender TINYINT,
  signature VARCHAR(512),
  last_synced_at TIMESTAMP NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_account_wxid (account_id, wxid),
  INDEX idx_nickname (nickname)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 群 (轻量缓存)
CREATE TABLE IF NOT EXISTS wpp_chatrooms (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  account_id VARCHAR(64) NOT NULL,
  chatroom_id VARCHAR(128) NOT NULL,
  nickname VARCHAR(256),
  remark VARCHAR(256),
  owner_wxid VARCHAR(128),
  member_count INT,
  last_synced_at TIMESTAMP NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_account_chatroom (account_id, chatroom_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 会话状态 (用于 inbound 防抖 / 触发器状态)
CREATE TABLE IF NOT EXISTS wpp_session_state (
  account_id VARCHAR(64) NOT NULL,
  peer_kind ENUM('direct', 'group', 'room') NOT NULL,
  peer_id VARCHAR(128) NOT NULL,
  last_msg_id VARCHAR(128),
  last_msg_ts TIMESTAMP NULL,
  pending_count INT DEFAULT 0,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (account_id, peer_kind, peer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- API 调用审计 (vendor 鉴权 + 限流排查用)
CREATE TABLE IF NOT EXISTS wpp_api_calls (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  account_id VARCHAR(64) NOT NULL,
  endpoint VARCHAR(255) NOT NULL,
  method VARCHAR(8) DEFAULT 'POST',
  status_code INT,
  vendor_code INT,                     -- vendor 业务 Code 字段 (-1/-2/-8/0)
  latency_ms INT,
  request_body JSON,
  response_body JSON,
  ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_account_ts (account_id, ts),
  INDEX idx_endpoint (endpoint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 群成员表 (v1.3.34 2026-08-11 新增: 三表同步 — 脱敏判断/群活跃分析用)
CREATE TABLE IF NOT EXISTS wpp_chatroom_members (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  account_id VARCHAR(64) NOT NULL,
  chatroom_id VARCHAR(128) NOT NULL,   -- 群 ID (@chatroom 结尾)
  wxid VARCHAR(128) NOT NULL,          -- 成员 wxid
  nickname VARCHAR(256),               -- 成员昵称
  avatar_url VARCHAR(512),             -- 头像
  is_owner TINYINT DEFAULT 0,          -- 是否群主
  last_synced_at TIMESTAMP NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_room_wxid (account_id, chatroom_id, wxid),
  INDEX idx_wxid (wxid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 群黑话表 (v1.3.76 2026-08-22 新增: 自主学习黑话挖掘)
CREATE TABLE IF NOT EXISTS wpp_jargon_terms (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  account_id VARCHAR(64) NOT NULL,
  group_id VARCHAR(128) NOT NULL,      -- 群 ID (@chatroom 结尾)
  term VARCHAR(64) NOT NULL,           -- 黑话词条
  raw_content VARCHAR(512),            -- 出现该词条的上下文样例
  meaning VARCHAR(512),                -- LLM 推断含义
  is_jargon TINYINT DEFAULT 1,         -- 是否确认黑话
  frequency INT DEFAULT 1,             -- 词频
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_group_term (account_id, group_id, term),
  INDEX idx_group (account_id, group_id),
  INDEX idx_term (term)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================ 心流 (5)

-- 心流反馈闭环表 (v1.6.x 2026-09-07 新增: 决策 + 发送结果 + 接话观察窗)
-- 时间全用 epoch 秒 (INT UNSIGNED), 对齐 wpp_messages.create_time 语义
CREATE TABLE IF NOT EXISTS wpp_hf_ledger (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id VARCHAR(64) NOT NULL,
  inbound_msg_id VARCHAR(128) NOT NULL,        -- msg.msgId (parser 保证非空; 跨通道稳定)
  new_msg_id VARCHAR(128) NULL,                -- 审计用 (可为 '')
  bot_msg_id VARCHAR(128) NULL,                -- v1.6.8: bot 发出那条的 msgId; vendor 不回 id 时 NULL
  group_id VARCHAR(128) NOT NULL,              -- chatId (xxx@chatroom)
  from_wxid VARCHAR(128) NULL,
  msg_type VARCHAR(32) NULL,
  content_head VARCHAR(512) NULL,              -- content 归一化后前 256 字符
  judge_overall DECIMAL(6,4) NULL,             -- 0..1
  dim_r DECIMAL(5,2) NULL,                     -- 0..10 (relevance)
  dim_w DECIMAL(5,2) NULL,                     -- willingness
  dim_s DECIMAL(5,2) NULL,                     -- social
  dim_t DECIMAL(5,2) NULL,                     -- timing
  dim_c DECIMAL(5,2) NULL,                     -- continuity
  effective_threshold DECIMAL(6,4) NULL,       -- judge 时实际阈值 (0..1)
  energy DECIMAL(5,3) NULL,                    -- judge 前精力
  status ENUM('judged','sent','suppressed','closed') NOT NULL DEFAULT 'judged',
  suppressed_reason VARCHAR(96) NULL,          -- dedup/ack-template/empty/no-deliver-outcome/send-failed
  engaged TINYINT(1) NULL,                     -- NULL=suppressed 排除样本外; 1=engaged; 0=ignored
  engage_signal VARCHAR(24) NULL,              -- v1.6.8 命中信号: quote/mention/negative/short-window/silence
  judged_at INT UNSIGNED NOT NULL,
  sent_at INT UNSIGNED NULL,                   -- ★ 判「最后真发言」只认 MAX(sent_at)
  window_expires_at INT UNSIGNED NULL,         -- = sent_at + observeWindowSec
  closed_at INT UNSIGNED NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_hf_acct_msg (account_id, inbound_msg_id),
  KEY idx_hf_group_status (account_id, group_id, status),
  KEY idx_hf_open (account_id, status, window_expires_at),
  -- 2026-09-28 审阅发现: 5 处热查询全按 (account_id + 某个时间列) 过滤/排序, 而
  -- judged_at/sent_at/closed_at 上**没有任何索引** ⇒ 每条都走 filesort。
  -- ⚠️ TS 侧这 3 个只写在 mysql.ts 的 ensureIndex 里、**没进 db/schema.sql**
  --    （双写陷阱的实证）。Python 侧补进 DDL 真源。
  KEY idx_hf_judged (account_id, judged_at),
  KEY idx_hf_sent (account_id, sent_at),
  KEY idx_hf_closed (account_id, closed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 心流每群自适应状态: 1 行/群, learned 阈值落这里 (高频写 DB, 不回写 accounts JSON 防 fs.watch 抖动)
CREATE TABLE IF NOT EXISTS wpp_hf_group_state (
  account_id VARCHAR(64) NOT NULL,
  group_id VARCHAR(128) NOT NULL,
  learned_threshold DECIMAL(6,4) NULL,         -- NULL=回落账号级 replyThreshold
  last_change_at INT UNSIGNED NULL,
  last_change_old DECIMAL(6,4) NULL,
  last_change_new DECIMAL(6,4) NULL,
  last_change_reason VARCHAR(128) NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (account_id, group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 心流阈值变更审计: 每次变更插一行 (满足「变更留痕」护栏)
CREATE TABLE IF NOT EXISTS wpp_hf_threshold_audit (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id VARCHAR(64) NOT NULL,
  group_id VARCHAR(128) NOT NULL,
  old_threshold DECIMAL(6,4) NULL,
  new_threshold DECIMAL(6,4) NOT NULL,
  sample_total INT UNSIGNED NOT NULL,          -- 触发本次变更的样本基数
  sample_engaged INT UNSIGNED NOT NULL,
  reason VARCHAR(255) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_hf_audit (account_id, group_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 心流群画像 (v1.7.0 2026-09-26 新增: 每群每日一份 LLM 画像 + 统计快照)
-- ⚠️ 只能收紧约束, 不许放开
CREATE TABLE IF NOT EXISTS wpp_hf_group_profile (
  account_id VARCHAR(64) NOT NULL,
  group_id VARCHAR(128) NOT NULL,
  profile_json TEXT NULL,                      -- 结构化画像 JSON (解析失败即视为无画像)
  stats_json TEXT NULL,                        -- 生成时的统计快照 (审计)
  sample_msgs INT UNSIGNED NOT NULL DEFAULT 0, -- 参与生成的样本消息数
  model VARCHAR(64) NULL,
  version INT UNSIGNED NOT NULL DEFAULT 1,     -- 每次重生成 +1
  generated_at INT UNSIGNED NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (account_id, group_id),
  KEY idx_hf_profile_gen (account_id, generated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 心流分层统计 (v1.8.0 2026-09-26 新增: 每群 × 每时段一份**快照**, 不是累加器)
-- ⚠️ 每轮 sweep **全量重算**后只 upsert 有变化的行
--    (累加会在 300s 周期里重复计入, 不可自愈 —— 见 INVARIANT I3)
CREATE TABLE IF NOT EXISTS wpp_hf_layer_stat (
  account_id VARCHAR(64) NOT NULL,
  group_id VARCHAR(128) NOT NULL,
  layer_kind VARCHAR(16) NOT NULL DEFAULT 'daypart',  -- daypart=日时段 (topic 槽位预留未启用)
  layer_key VARCHAR(32) NOT NULL,                     -- "18-24" 起-止, 自定义段也自解释
  n INT UNSIGNED NOT NULL DEFAULT 0,                  -- 窗口内可采信样本数 (重算覆盖, 非累加)
  engaged INT UNSIGNED NOT NULL DEFAULT 0,
  ambient_p DECIMAL(6,4) NULL,                        -- 段内本底 (展示用, 不参与判定)
  window_start INT UNSIGNED NOT NULL DEFAULT 0,       -- 本次重算的窗口起点 (滚动)
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (account_id, group_id, layer_kind, layer_key),
  KEY idx_hf_layer_acct (account_id, layer_kind, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 心流图片配文 (v1.16.0 2026-10-16 新增: 群画像样本"看得见图")
-- PK=url_hash (sha1(OSS URL) 全局唯一 —— 同一张图跨群只配一次)。
-- caption NULL 且 fail_count<上限 = 可重试; 从不删行 (止损靠 fail_count, 重启/新 sweep 幂等)。
-- ⚠️ 逐字搬自 TS `src/storage/db/mysql.ts` v1.16.0 (TS 的 db/schema.sql 尚未同步)。
CREATE TABLE IF NOT EXISTS wpp_hf_image_caption (
  url_hash CHAR(40) NOT NULL,
  account_id VARCHAR(64) NOT NULL,
  group_id VARCHAR(128) NULL,
  url_sample VARCHAR(255) NULL,
  caption TEXT NULL,
  model VARCHAR(64) NULL,
  fail_count INT UNSIGNED NOT NULL DEFAULT 0,
  created_at INT UNSIGNED NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (url_hash),
  KEY idx_hf_cap_acct (account_id, fail_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================ 辅助 (2)

CREATE TABLE IF NOT EXISTS wpp_svrid_mapping (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id VARCHAR(64) NOT NULL,
  svrid VARCHAR(32) NOT NULL,
  msg_md5 VARCHAR(32) NULL,
  quoted_content_hash VARCHAR(64) NULL,
  captured_at INT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uk_svrid_acct (account_id, svrid),
  KEY idx_md5 (msg_md5)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS wpp_sync_state (
  account_id VARCHAR(64) NOT NULL,
  synckey VARCHAR(1024) NOT NULL,
  updated_at INT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================ Python 独有 (2)

-- 红包记录 (TS 侧无此表)
-- 出处: 旧树 plugin/db.py:357 record_redpacket + hongbao.py:72
-- 幂等: (account_id, peer_id, from_wxid, hb_url) 唯一 —— 同一红包重复入库只更新 raw_json
-- ⚠️ 与「接龙被误判红包」事故相关 (2026-09-27): 入库前必须先过 hongbao.is_red_packet_message,
--    而那把闸曾在 v1.9.2 前把接龙整条丢掉 —— 见 INVARIANT / tests/test_triggers.py
CREATE TABLE IF NOT EXISTS wpp_redpackets (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id VARCHAR(64) NOT NULL,
  peer_id VARCHAR(128) NOT NULL,       -- 群 ID 或会话 ID
  from_wxid VARCHAR(128) NOT NULL,
  hb_url VARCHAR(512) NOT NULL DEFAULT '',
  hb_key VARCHAR(128) NOT NULL DEFAULT '',
  raw_json TEXT NULL,
  opened TINYINT(1) NOT NULL DEFAULT 0,
  created_at BIGINT UNSIGNED NOT NULL DEFAULT 0,   -- epoch 秒 (沿用旧树 UNIX_TIMESTAMP())
  PRIMARY KEY (id),
  UNIQUE KEY uk_redpacket (account_id, peer_id, from_wxid, hb_url),
  KEY idx_redpacket_acct (account_id, peer_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 好感度 / 情绪状态 (TS 侧无此表)
-- 出处: TS src/inbound/affection.ts (v1.11.0) 的模块级 `groupStates` Map —— 进程一重启
--       就清零。老板 2026-10-02 拍板**落库持久化**，故 Python 侧多这一张表。
-- ⚠️ **一张表两种粒度**，靠 `user_id` 哨兵分（见 plugin/data/repo_affection.py:1-36）:
--        user_id <> ''  → 该用户在群里的好感度分值 (level / updated_at)
--        user_id =  ''  → 该群的情绪            (mood_type / intensity / description / expires_at)
--    ⇒ 凡"所有用户"的查询**必须**带 `user_id <> ''`：漏了它，情绪行会被当成一个
--      level=0 的用户，算进群总量（重分配阈值）与人数（/affection status）。
-- ⚠️ 高频写（每条群消息一次），**绝不回写 accounts JSON** —— 同 wpp_hf_group_state
--    的先例（防 fs.watch 抖动）。
-- ⚠️ `expires_at` 是**绝对 epoch 秒**，不是相对秒 —— 相对秒在进程重启后判不了过期。
--    ``default_mood()`` 把 expires_at 设成"当下"⇒「平静」等于"不过期"，靠 read 侧
--    `now > expires_at` 对非平静情绪生效。
CREATE TABLE IF NOT EXISTS wpp_affection (
  account_id VARCHAR(64) NOT NULL,
  group_id VARCHAR(128) NOT NULL,
  user_id VARCHAR(128) NOT NULL DEFAULT '',        -- '' = 情绪行（哨兵，见上）
  level INT NOT NULL DEFAULT 0,                    -- 好感度分值（仅 user_id <> ''）
  mood_type VARCHAR(32) NOT NULL DEFAULT '',       -- 仅情绪行：happy/sad/…（应用层枚举）
  intensity DECIMAL(4,3) NOT NULL DEFAULT 0,       -- 仅情绪行：0..1
  description VARCHAR(255) NOT NULL DEFAULT '',    -- 仅情绪行：展示给人看的短句
  expires_at INT UNSIGNED NOT NULL DEFAULT 0,      -- 仅情绪行：绝对 epoch 秒
  updated_at INT UNSIGNED NOT NULL DEFAULT 0,      -- epoch 秒（= 写这条时的 now，非 TIMESTAMP）
  PRIMARY KEY (account_id, group_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
-- 索引：主键前缀就够 —— 三处查询全落在 (account_id[, group_id]) 上：
--   · 单人分值      → PK 全列精确匹配
--   · 本群全量/总量/人数 → PK 前缀 (account_id, group_id)
--   · 该账号有状态的群数 → PK 前缀 (account_id)
-- 主键总长 64+128+128 = 320 字符，与既有 uk_account_msg 同宽（真库已验证可用）。


-- ============================================================ 说话风格学习 (2, Python 独有)

-- 群说话风格卡（2026-10-19 老板拍板：学群友的说话风格，用到 bot 自己身上）
-- 与心流群画像的分工：画像是「群是什么、bot 该不该说」，风格卡是「话怎么说」
-- （句长 / 语气词 / 口头禅 / 表情 / 称呼 / 标点）。存摘要，**不存原文**。
-- 注入回复侧走 Hermes channel_prompt（临时 system prompt，不进 transcript）。
CREATE TABLE IF NOT EXISTS wpp_style_cards (
  account_id VARCHAR(64) NOT NULL,
  group_id VARCHAR(128) NOT NULL,
  style_json TEXT NULL,                        -- 结构化风格卡 JSON（解析失败即视为无卡）
  sample_msgs INT UNSIGNED NOT NULL DEFAULT 0, -- 参与生成的清洗后样本数
  sender_count INT UNSIGNED NOT NULL DEFAULT 0,-- 参与样本的匿名说话人数
  confidence DECIMAL(4,3) NOT NULL DEFAULT 0,  -- 0..1（低于 style.minConfidence 不注入）
  model VARCHAR(64) NULL,
  version INT UNSIGNED NOT NULL DEFAULT 1,     -- 每次生成/回滚 +1（只增不减）
  generated_at INT UNSIGNED NULL,              -- 绝对 epoch 秒
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (account_id, group_id),
  KEY idx_style_gen (account_id, generated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 风格卡版本历史（生成/回滚各留一版；回滚 = 把旧版复制成新版本，版本号仍只增）
-- 为什么不把旧版塞进主表 JSON：主表要的是"当前生效版"，历史要能逐版查看/回滚且
-- 不被一次坏写入连带覆盖。样本数/置信度/模型一起留，回滚后审计口径不变。
CREATE TABLE IF NOT EXISTS wpp_style_card_versions (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id VARCHAR(64) NOT NULL,
  group_id VARCHAR(128) NOT NULL,
  version INT UNSIGNED NOT NULL,
  style_json TEXT NULL,
  sample_msgs INT UNSIGNED NOT NULL DEFAULT 0,
  sender_count INT UNSIGNED NOT NULL DEFAULT 0,
  confidence DECIMAL(4,3) NOT NULL DEFAULT 0,
  model VARCHAR(64) NULL,
  reason VARCHAR(16) NOT NULL DEFAULT 'generated',  -- generated | rollback
  generated_at INT UNSIGNED NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_style_version (account_id, group_id, version),
  KEY idx_style_versions (account_id, group_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
