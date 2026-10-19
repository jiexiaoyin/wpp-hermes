# 架构与数据流

> 给接手人看的。目标是"读完能定位改哪里、出错先查什么"，不是目录说明。
> 最后更新：2026-10-19

---

## 1. 它是什么

Hermes 的一个**平台适配器插件**：把 WeChatPadPro（下称 vendor）的 HTTP API 包成
Hermes 的 `BasePlatformAdapter`，让 agent 能收发微信群/私聊消息。

```
微信客户端 ── vendor（wx.juhe.chat / 127.0.0.1:28062）
                 │ ① 入站 webhook（HMAC v2 签名）
                 │ ② 入站 WebSocket（兜底通道）
                 │ ③ HTTP API（出站发送、群管理、查询）
                 ▼
        ┌──────────────────────────┐
        │  plugin/（本仓）          │
        │  adapter ─ 编排           │
        │  inbound/ ─ 解析/判定/旁路 │
        │  platform/ ─ 协议与传输    │
        │  toolkit/ ─ 302 个工具定义 │
        └──────────────────────────┘
                 ▼
        Hermes gateway（handle_message）→ LLM → send()
```

**两个进程内的事实**（改代码前必须知道）：

1. **一个 adapter 实例管多个账号**（`accounts/*.json` 每个一份配置）。
2. **插件跑在 gateway 进程内**，`os.environ` 装的是 **launch profile** 的 `.env`，
   不是当前 profile 的 —— 读凭据一律走 `core/env_compat.secret_env` / `gate_env`，
   绝不直接 `os.getenv`（multiplex 下会串号，见 §4 不变量 7）。

---

## 2. 入站链路（`_handle_one`，170 行，10 个阶段）

位置：`plugin/platform/adapter.py`。**顺序本身就是契约**，每一步的注释都写了"为什么
必须在这里"，改之前先读。

```
①  自消息/回声过滤         from_wxid == self_wxid  → 丢弃
   ⚠️ filehelper 会话豁免   （vendor 把文件助手发送者标成机器人自己）
②  去重                    key = f"{账号}:{msg_id}"
③  空消息体               无正文 且 无媒体 → 丢弃（vendor 心跳噪声）
③b filehelper 管理命令     `/xxx` 在 ④ 之前截住，否则会被当问题喂给 LLM
③c 临时禁言               群 muted → 整条丢（含 @机器人）
④  触发判定               pipeline.decide() → should_drop()
④b1 硬触发限速            group_mention/solitaire 的止血闸
④c 媒体富化               下载 → OSS，**写回 msg.content**
④z 落库                   persist_inbound（只落白名单内的）
④d 接龙改写               apply_relay_render（DB 留原文，下游看改写后的）
④a 心流缓冲 / ④b' 黑话 / ④b 好感度   （都在 ④ 之后，只记未被丢弃的）
⑤  构造事件 → handle_message 或 _try_heartflow
```

**为什么 ④c 必须在 ④z 之前**：`msg.content` 是这条链路上唯一的口径，三个消费者
（落库、心流缓冲、judge 看图）都读它。放在 ⑤（`_build_event`）里取媒体就只能覆盖
"触发了的"消息，而 ④z/④a 排在它之前 → 提取函数拿不到直链 → **看图能力整段空转**。

**为什么 ④a 对"通过与否"都记**：只在触发时才喂，judge 看到的"最近 N 条上下文"
就只剩 bot 自己那几句，等于给裁判看了一份伪造的现场记录。

---

## 3. 出站链路（`send` → `_send_chunk`）

```
send(chat_id, content, reply_to, metadata)
  ├─ content/metadata 归一化（一次，别在下面重复写 `or ""`）
  ├─ 三道静默丢弃：_interim_send（过程话术）/ 拒绝话术 / 机器通知
  ├─ strip_self_mention（模型会把「@本账号昵称」原样抄进回复）
  ├─ 出站白名单闸 assert_outbound_allowed
  ├─ 心流交付缝 _hf_pre_send（登记过的群才命中）
  ├─ chunk_text 分片 → _send_chunk（每片一次 HTTP）
  └─ 记账 _hf_note_sent / _hf_note_bot_reply
```

**多片中途失败**：第 1 片成功、第 2 片失败 ⇒ 群里**已经有半句**（微信撤不回）。
此时写入 `partial_overflow: True` + `sent_chunks`，让 Hermes 的 `_send_with_retry`
**不重发**已见头部。`last_id is None`（一片都没出去）才是真"没发"。

**超时单独分类**：`error_kind="unknown"`。Hermes 的 `_is_timeout_error` 只认英文
`timed out/readtimeout`，而本客户端的中文「超时」会落进 transient 被自动重试 ——
重试就是重复发送。

---

## 4. 关键不变量（改代码前逐条对一遍）

| # | 不变量 | 违反的后果 |
|---|---|---|
| 1 | 入站必须 `await self.handle_message(event)` | runner 收不到消息 |
| 2 | 去重器必须是 `MessageDeduplicator` 实例且挂**命名属性** | 重连后变空 → vendor 重放被二次回答 |
| 3 | `connect()` 永久失败要 `_set_fatal_error(retryable=False)` | runner 每 300s 重试，fd 泄漏 |
| 4 | webhook 主路径**双因素**（HMAC v2 + 路径 token）；business **只** token | 配错 = 100% 403，消息静默丢失 |
| 5 | 落库只落**白名单内**的消息 | 隐私口径（老板拍板） |
| 6 | 自消息回推**不进心流缓冲** | bot 自己的话被记两份，judge 上下文掺水 |
| 7 | 凭据读 `secret_env`/`gate_env`，不读 `os.environ` | multiplex 下 A profile 用 B 的 key |
| 8 | 阻塞 IO（DB、文件、< 1MB 也要注意）走 `asyncio.to_thread` | 事件循环停摆，WS 心跳和所有账号一起排队 |
| 9 | 工具 schema 只补**高危/高频**描述 | 302 个工具每条多 30 字符 = 每轮多付 token |
| 10 | 生成物（`vendor_defs*.py`）只改源表 `tools/vendor_source/` 再重生成 | `--check` 拒，下次部署被覆盖 |

---

## 5. 安全模型

| 面 | 机制 | 位置 |
|---|---|---|
| 入站 webhook | HMAC-SHA256(`TimestampMs.DeliveryID.rawBody`) + 路径 token | `platform/inbound_webhook.py::_verify_v2_signature` |
| business 回调 | 仅路径 token（vendor 不签 business） | `adapter_webhook.py::_start_webhook` |
| 出站 | 白名单（`allowFrom` ∪ `groupAllowFrom` ∪ `adminUsers` ∪ env） | `outbound.assert_outbound_allowed` |
| 本地读 | `netguard.is_local_read_allowed` + realpath | `core/netguard.py` |
| 凭据文件 | `0o600` | 部署脚本 |
| 日志脱敏 | `redact_webhook_path`（sync_url 含 token） | `core/redact.py` |
| 子进程 | 全部 `create_subprocess_exec`（参数列表，无 `shell=True`） | 8 处，silk/media/stt |

**诊断开关**：`WPP_WEBHOOK_DEBUG=1` 会把签名头打进日志（凭据泄露风险）——
**验完立刻关**。

---

## 6. 目录

```
plugin/
├── platform/
│   ├── adapter.py            编排（2898 行；43% 是 ≤15 行转发方法，别再拆文件）
│   ├── adapter_webhook.py    WebhookMixin：厂商侧注册 + 周期刷新
│   ├── adapter_media.py      MediaMixin：9 个媒体方法
│   ├── inbound_webhook.py    HTTP server + HMAC 验签 + /metrics
│   ├── inbound_ws.py         WebSocket 兜底通道
│   └── conversation.py       通道无关会话契约的 WPP 实现
├── inbound/                  解析 / 判定 / 旁路（心流·黑话·好感度）/ 落库
├── outbound/                 分片、白名单、@self 剥离、拒绝话术识别
├── core/                     账号、客户端、指标、netguard、webhook_paths
├── toolkit/                  302 个工具定义（vendor_defs*.py 是生成物）
└── neutral/                  通道无关契约 + OSS 上传
```

---

## 7. 排障入口（按症状查）

| 症状 | 先看 | 常见原因 |
|---|---|---|
| 消息没反应 | `wpp_messages` 最新 `ts` | webhook 403（验签）→ 看 nginx access.log 该路径的状态码 |
| webhook 403 | 账号 `.env` 的 `WECHATPRO_WEBHOOK_SECRET` | vendor 侧没配 secret（不发签名头）⇒ 两边不一致 |
| 群名不更新 | `name_sync` 日志 | `/Group/List` 返回形状变了（`_group_rows` 不认） |
| 工具选错 | `toolkit/vendor_defs*.py` 的描述 | 描述 < 20 字符，弱模型分不清近义工具 |
| 事件循环卡 | `asyncio.to_thread` 覆盖率 | 新增了同步 IO 没 offload |
| 心流不说话 | `_try_heartflow` 日志 | 预算/冷却/重复内容闸 |
| 重连后重复回答 | 去重器属性名 | 必须是 `MessageDeduplicator` 实例 |

**探针**（不改生产）：

```bash
python tools/probe_registration.py     # 302 工具与 plugin.yaml 逐名对账
python tools/verify_tree.py            # 结构守卫（包形状 / 顶层文件数）
python tools/gen_vendor_defs.py --check # 生成物与源表一致
bash tools/gates.sh --only lint        # 只跑匹配的门禁项
```

**日志位置**：`~/.hermes/profiles/<profile>/logs/gateway.log`（profile 自己的）+
`~/.hermes/logs/gateway.log`（launch profile）。**webhook 线程写的是后者** —— 在
目标 profile 里怎么 grep 都没有，先换文件再怀疑代码。

---

## 8. 部署

```bash
bash sync.sh                    # 备份 → rsync → 语法复检 → md5 逐位
bash sync.sh --check            # 只核对 dev 与生产是否一致
bash sync.sh --rollback <备份>   # 回滚
hermes gateway restart          # 必须在**外部 shell** 跑
```

**为什么不带重启**：网关进程内执行重启会把发起命令一起杀掉（安全策略也拦）。
`sync.sh` 跑到"代码已就位"为止，重启由人做。

**改了 `.env` 不需要重新部署**，重启即可生效。
