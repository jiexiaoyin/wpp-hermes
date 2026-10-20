# 配置参考

配置分两层：

1. **环境变量**：`<HERMES_HOME>/.env`（单 profile）或
   `<HERMES_HOME>/profiles/<name>/.env`（多 profile）。凭据、地址、webhook、数据库。
2. **账号 JSON**：`<插件目录>/accounts/<id>.json`。白名单、群策略、模块开关、agent 绑定。

只配环境变量也能启动：插件会合成一个 `default` 账号。需要多账号或细粒度策略时再写账号文件。

---

## 1. 最小配置

```dotenv
WECHATPRO_API_BASE=https://your-vendor.example
WECHATPRO_AUTHCODE=...
WECHATPRO_TOKEN_KEY=...
```

| 变量 | 必填 | 说明 |
|---|---|---|
| `WECHATPRO_API_BASE` | 是 | vendor HTTP 根地址，只写到 host（不带 `/ws/sync`） |
| `WECHATPRO_AUTHCODE` | 是 | vendor 分配的客户端凭据 |
| `WECHATPRO_TOKEN_KEY` | 是 | vendor 分配的客户端 token |
| `WECHATPRO_WS_URL` | 否 | WebSocket 地址；留空按 API base 推导 `wss://host/ws/sync` |

⚠️ **一个客户端密钥同一时间只能被一个客户端使用。** 不要把同一份凭据同时配到两个
profile、两台机器或两个测试进程；后连接的一方会把前一方踢下线。

---

## 2. 环境变量总表

### 连接与触发

| 变量 | 默认 | 说明 |
|---|---|---|
| `WECHATPRO_AGENT` | profile 名 | 账号绑定的 Hermes profile；禁止 `main` |
| `WECHATPRO_ALLOWED_USERS` | 空 | 允许私聊的用户，逗号分隔（`dmPolicy=allowlist` 时使用） |
| `WECHATPRO_ALLOW_ALL_USERS` | `0` | `1` = 放行所有私聊/群消息（真实过滤仍在插件闸门）。**open / blacklist / pairing 模式必须为 `1`**，否则 Hermes 网关会先 default-deny；向导会自动写入 |
| `WECHATPRO_HOME_CHANNEL` | 空 | cron / 通知默认目标，格式 `accountId:peerId` |
| `WECHATPRO_HOME_CHANNEL_NAME` | 空 | 默认目标的显示名 |

### webhook

| 变量 | 默认 | 说明 |
|---|---|---|
| `WECHATPRO_WEBHOOK_PUBLIC_URL` | 空 | vendor 回调的公网基址；**必须显式配置** |
| `WECHATPRO_WEBHOOK_HOST` | `127.0.0.1` | 本地监听地址 |
| `WECHATPRO_WEBHOOK_SECRET` | 空 | HMAC v2 验签 secret；与 vendor 侧一致 |
| `WPP_WEBHOOK_DEBUG` | `0` | 打印签名头，含凭据风险；验完立刻关 |
| `WPP_WEBHOOK_MAX_SKEW_SEC` | 实现默认 | 时间戳允许偏移 |

### 数据库（SQLite 默认，MySQL 可选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `WECHATPRO_DB_BACKEND` | `sqlite` | `sqlite` / `mysql`（也认 `mariadb` / `sqlite3`）；拼错直接报错 |
| `WECHATPRO_SQLITE_PATH` | `<插件目录>/wpp.sqlite3` | SQLite 文件；`:memory:` 仅测试用 |
| `WECHATPRO_DB_HOST` | `127.0.0.1` | 仅 MySQL；只允许本机，远程需显式 `WECHATPRO_DB_ALLOW_REMOTE=1` |
| `WECHATPRO_DB_PORT` | `3306` | 仅 MySQL |
| `WECHATPRO_DB_USER` / `WECHATPRO_DB_PASSWORD` | 空 | 仅 MySQL |
| `WECHATPRO_DB_NAME` | 空 | 仅 MySQL；MySQL 模式下不配则该后端不可用（SQLite 模式忽略此项） |
| `WECHATPRO_DB_CONNECT_TIMEOUT` / `_READ_TIMEOUT` / `_WRITE_TIMEOUT` | 实现默认 | 秒 |

### OSS 媒体转存（可选，默认关闭）

| 变量 | 默认 | 说明 |
|---|---|---|
| `WECHATPRO_OSS_ENABLED` | `0` | `1` 才启用；启用后媒体消息会带公网 URL |
| `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` | 空 | OSS 凭据（只读，不写日志） |
| `OSS_BUCKET` / `OSS_ENDPOINT` | 空 / 实现默认 | 桶名与 endpoint |
| `OSS_CREDENTIALS_PATH` | 兼容回落 | 凭据文件路径（新版建议直接用上面的 env） |

也需要本机安装 `ossutil` 与 `~/.ossutilconfig`（`ossutil` 自己读它鉴权）。
缺任何一项只会让"这条消息不带 URL"，不影响收发。

### LLM / 语音（可选）

| 变量 | 说明 |
|---|---|
| `JUDGE_API_KEY` | 心流 / 好感度 / 黑话 / 风格学习共用的模型 key（也接受 `MINIMAX_CN_API_KEY` / `MINIMAX_API_KEY`） |
| `JUDGE_BASE_URL` | 可选，自定义判定模型的兼容端点 |
| `WECHATPRO_STT_API_KEY` | 语音转写 key（也接受 `SILICONFLOW_API_KEY`） |
| `WPP_STT_BACKEND` | `siliconflow`（默认）或 `dashscope` |
| `WPP_STT_URL` / `WPP_STT_MODEL` | 覆盖 STT 端点与模型 |
| `WPP_SILK_DECODER_PATH` | 入站 silk 解码器，默认 `~/silk_decoder/silk/decoder` |
| `WPP_SILK_ENCODER_PATH` | 出站 silk 编码器，默认 `~/silk_decoder/silk/encoder` |

### 安全护栏

| 变量 | 说明 |
|---|---|
| `WECHATPRO_SEND_ALLOWLIST` | 出站目标白名单；留空不限制 |
| `WECHATPRO_PROTECTED_WXIDS` | 受保护的 bot wxid，逗号分隔；禁止被测试实例连接 |
| `WECHATPRO_PROTECTED_FINGERPRINTS` | 受保护的凭据指纹（`sha256(authcode)[:12]`） |
| `WECHATPRO_ALLOW_PRODUCTION_WXID` | `1` = 显式放行受保护 wxid（仅供生产实例） |
| `WECHATPRO_ALLOW_GLOBAL_CREDENTIALS` | 允许回落到全局 `.env` 凭据 |
| `WECHATPRO_ACCOUNTS_DIR` | 覆盖账号目录 |
| `WECHATPRO_ACCOUNT_BACKUP_DIR` | 覆盖账号备份目录 |

### 其他

| 变量 | 说明 |
|---|---|
| `WECHATPRO_ACCOUNTS_JSON` | 内联账号表：`{"<id>": {"authcode": "...", "tokenKey": "..."}}` |
| `WECHATPRO_MCP_URL` | vendor MCP 地址（可选通道） |
| `WPP_PAIRING_DIR` | 私聊配对状态目录 |

---

## 3. 账号 JSON

最小账号文件（`accounts/default.json`）：

```json
{
  "enabled": true,
  "apiBaseUrl": "https://your-vendor.example",
  "authcodeEnv": "WECHATPRO_AUTHCODE",
  "tokenKeyEnv": "WECHATPRO_TOKEN_KEY",
  "agent": "wpp-wechat",
  "dmPolicy": "open",
  "groupPolicy": "open"
}
```

**不要把明文 authcode/tokenKey 写进账号文件**，用 `authcodeEnv` / `tokenKeyEnv`
指向 `.env` 里的变量。账号文件权限应为 `0600`。

完整范例见 `plugin/accounts/default.example.json` 与
`plugin/accounts/boss2.example.json`。

### 常用字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否连接该账号 |
| `apiBaseUrl` / `wsUrl` | 环境变量 | 账号级覆盖 |
| `authcode` / `authcodeEnv` | — | 明文或环境变量名 |
| `tokenKey` / `tokenKeyEnv` | — | 明文或环境变量名 |
| `selfWxid` | 空 | bot 自己的 wxid；留空由 vendor 返回 |
| `nickname` | `微信助手` | 显示名，用于 @ 识别 |
| `agent` | profile 名 | 绑定的 Hermes profile；禁止 `main` |
| `dmPolicy` | `open` | 私聊准入：`open`（全部放行）/ `allowlist` / `blacklist` / `pairing` |
| `allowFrom` | `[]` | 私聊白名单（`allowlist` 模式）；空列表 = 拒绝所有私聊 |
| `blacklistUsers` | `[]` | 私聊黑名单；**任何模式**下都先拒 |
| `groupPolicy` | `open` | `open` / `allowlist` / `blacklist` / `disabled` / `closed`（未知值 fail-closed） |
| `groupAllowFrom` | `[]` | `allowlist` 模式下的群白名单；空列表 = 拒绝所有群 |
| `adminUsers` | `[]` | 管理命令用户 |
| `blacklistGroups` | `[]` | 永不处理的群（`blacklist` 模式或一票否决） |
| `webhookHost` / `webhookPort` | `127.0.0.1` / `4398` | 每个账号端口必须不同 |
| `webhookPath` / `webhookPathToken` | 实现默认 / 空 | 路径 token 是 webhook 的第一因素 |
| `webhookSecretEnv` | 空 | HMAC secret 的环境变量名 |
| `autoSetWebhook` | `true` | 启动时向 vendor 注册回调 |
| `debounceMs` | `500` | 入站防抖窗口 |
| `employeeGroupIds` | `[]` | 全员群；成员自动同步进 `allowFrom` |
| `mcpEnabled` | `false` | 是否启用 vendor MCP 工具（必须显式 `true`） |

### 准入模式

**默认群聊、私聊全部放行。** 需要收紧时按账号选模式：

| 模式 | 私聊 | 群聊 |
|---|---|---|
| `open`（默认） | 全部放行 | 全部放行 |
| `allowlist` | 只放行 `allowFrom`；空 = 拒所有 | 只放行 `groupAllowFrom`；空 = 拒所有 |
| `blacklist` | 放行除 `blacklistUsers` 外的人 | 放行除 `blacklistGroups` 外的群 |
| `pairing` | 只放行已兑换配对码的用户 | — |
| `disabled` / `closed` | — | 全部静默 |

- 公众号（`gh_` 前缀）、微信团队、微信支付等服务通知**任何模式都不回复**。
- 配对模式：管理员私聊发 `/genpair` 取码，用户发 `/pair <码>` 自助加入；
  已配对名单在 `<插件目录>/pairing/<account>.paired.json`（0600）。
- `open` / `blacklist` / `pairing` 的真实过滤由插件做；Hermes 网关侧仍需
  `WECHATPRO_ALLOW_ALL_USERS=1` 才放行（向导自动写），否则消息到不了插件。

### 可选模块

每个模块都是独立开关，**默认关闭**（模型画像能力是其中唯一默认开启的子项）：

```json
{
  "heartflow": {
    "enabled": false,
    "model": "your-judge-model",
    "profile": {"enabled": true}
  },
  "embeddingModel": "text-embedding-v4",
  "embeddingApiKeyEnv": "DASHSCOPE_API_KEY",
  "affection": {"enabled": false, "model": "your-model"},
  "jargon": {"enabled": false, "model": "your-model"},
  "style": {"enabled": false, "model": "your-model"},
  "tts": {"enabled": false, "provider": "tokenplan"},
  "proactive": {"enabled": false, "allowFrom": []},
  "debounceMerge": {"enabled": false, "maxChars": 2000},
  "spectre": {"quoteHint": false},
  "joinWelcome": {
    "enabled": false,
    "mode": "allowlist",
    "groups": [],
    "template": "欢迎 @{name} 加入{group}！",
    "llmPolish": true,
    "mention": true,
    "quietHours": [],
    "maxBatch": 3
  }
}
```

**`joinWelcome`（进群欢迎语）**：默认关闭；`mode` 取 `allowlist` / `blocklist` /
`all`，未知值回落 `allowlist`。`{name}` 为新人昵称（多人用「、」连接），`{group}`
为群名。`llmPolish` 只在有群画像时生效，失败回落模板原文；`maxBatch` 防止首次
同步 / 清表把全群欢迎一遍。详见 [features.md](features.md#10-成员变动识别与进群欢迎语)。

- **heartflow**：群聊里判断是否主动搭话，遵循白名单、预算、静默段。
  启用时必须配置 `heartflow.model`；判定模型密钥走 `JUDGE_API_KEY`
  （可选 `JUDGE_BASE_URL`，未设置时用实现默认端点）。
- **heartflow.profile**：群画像 / 模型画像能力，**默认开启**；不需要时写
  `"enabled": false`。
- **向量模型**：用于群聊意图识别（挑选相关上下文），配 `embeddingModel` +
  `embeddingApiKeyEnv`（指向 `.env` 里的 key，例如 `DASHSCOPE_API_KEY`），可选
  `embeddingBaseUrl`。未配置时向量层关闭，自动退回 LLM 意图判断；不影响心流。
- **affection**：好感度记账，影响 TTS 情绪路由。
- **jargon**：群黑话（黑化）挖掘，写入数据库。
- **style**：从群聊归纳说话风格，注入回复侧 system prompt。
- **proactive**：私聊主动关心，`allowFrom` 为空时永不发送。
- **debounceMerge**：合并突发私聊 / @ 消息。

未配置模型或 key 时模块不启用，不会阻塞消息。

---

## 4. 多账号隔离

每个账号必须满足：

1. 独立 `accounts/<id>.json`；
2. 独立 authcode（不同 vendor 客户端）；
3. 独立 `selfWxid`；
4. 独立 `webhookPort`；
5. 独立 `agent` profile（不能是 `main`）。

启动后插件会为每个账号建立独立客户端实例。webhook 路径按账号区分，端口冲突会直接报错。

---

## 5. webhook

入站有两条通道：

- **webhook 主路径**：vendor POST → 本地 HTTP server。需要路径 token **和** HMAC secret。
- **WebSocket 兜底**：webhook 不可达时仍能收消息。

公网部署时：

```dotenv
WECHATPRO_WEBHOOK_PUBLIC_URL=https://bot.example.com
WECHATPRO_WEBHOOK_SECRET=<随机长串>
```

反向代理需要把 `/wechatpadpro/...` 路径转发到 `WECHATPRO_WEBHOOK_HOST:webhookPort`。
路径 token 会出现在 URL 里，日志脱敏只保留前缀。

---

## 6. 数据库（SQLite 默认 / MySQL 可选）

**默认使用 SQLite，无需安装数据库服务。** 数据落在
`<插件目录>/wpp.sqlite3`（可用 `WECHATPRO_SQLITE_PATH` 改位置），首次启动自动建
20 张表与索引。WAL 模式 + 5 秒 busy timeout，适合单机单进程的 Hermes 网关。

SQLite 与 MySQL/MariaDB 是**功能等价的两个后端**：历史检索、心流台账、审计、
黑话/风格/好感度持久化在 SQLite 下全部可用。MariaDB 只是“可选替代”，不是这些
能力的前置依赖。

需要 MariaDB/MySQL 时显式切换：

```dotenv
WECHATPRO_DB_BACKEND=mysql
WECHATPRO_DB_HOST=127.0.0.1
WECHATPRO_DB_PORT=3306
WECHATPRO_DB_USER=wechatpro
WECHATPRO_DB_PASSWORD=...
WECHATPRO_DB_NAME=wechatpro
```

```sql
CREATE DATABASE wechatpro CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'wechatpro'@'127.0.0.1' IDENTIFIED BY '...';
GRANT ALL PRIVILEGES ON wechatpro.* TO 'wechatpro'@'127.0.0.1';
```

- SQLite 与 MySQL 共用同一份 `schema.sql`；SQLite 侧在运行时做确定性方言翻译。
- 首次连接自动建表；已有库只补齐缺列/缺索引，并做只读漂移检测，不改列、不删列。
- 数据库连不上时消息收发、工具调用照常；历史检索、心流落库、审计、黑话/风格
  持久化降级，启动日志会写明原因。
- **已有 MySQL 部署升级时必须显式补 `WECHATPRO_DB_BACKEND=mysql`**，否则会切到
  一个空 SQLite。
- 测试请使用以 `_test` 结尾的库名，插件和测试护栏都会拒绝其他库名（SQLite 测试
  只用临时文件）。
