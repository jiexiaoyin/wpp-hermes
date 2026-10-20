# CHANGELOG

本文件记录 Python 侧（本仓）的版本。TS 版历史在 `jiexiaoyin/wpp-openclaw` 的 CHANGELOG。

版本号与 `plugin/plugin.yaml` 的 `version` 保持一致；打 tag 用 `v<version>`。

---

## v2.3.0 — 2026-10-20

Web 控制台（不修改 Hermes 本体代码）。

- **Dashboard 页签**：`/wpp-console`，由 Hermes 官方插件机制加载
  （`dashboard/manifest.json` + `dashboard/plugin_api.py` + 纯 IIFE 前端）。
  每个要显示页签的 profile 需在 `plugins.enabled` 里启用 `wechatpadpro-console`；
  次级 profile（如 `wpp-wechat`）的插件后端由 Hermes 转发到该 profile 自己的
  plugin host 运行（PLG-3），未启用的 profile 返回 404。状态快照在此场景下回退到
  launch home 读取。
- **账号状态**：网关每 5 秒写一份运行时快照
  （`$HERMES_HOME/plugin-data/wechatpadpro/console-status.json`，0600），包含真实
  WS 连接、客户端就绪、最近热加载结果；控制台按 15s/60s 区分 live/delayed/stale，
  快照缺失或过期时显示 unknown，不把旧状态当在线。控制台**不调用厂商 API**。
- **准入 / 欢迎语 / 能力开关编辑**：严格枚举与名单校验（`@chatroom` 归属、上限、
  `quietHours` 只收 0–23 整数、`maxBatch` 1–20），section 增量合并保留本树未建模
  的同级键；`heartflow` 未配 model 时开关不可用。
- **3 秒热加载**：网关侧 mtime watcher（3s）发现账号文件变化后
  `clear_overrides` + `reload_account`，无需重启 Hermes；UI 对比快照
  `config_mtime_ns` 显示「已热加载」。
- **跨进程写锁**：`account_store.update_account()` 在进程锁 + `flock` 内完成
  读 → mutate → 备份 → 原子替换 → 清同键覆盖；控制台与全员群同步并发写不再
  互相覆盖。锁文件在 `accounts/.locks/`（已被 sync/check/回滚排除）。
- **群活跃统计**：新增只读 `data/repo_console.py`（三条单表查询 + Python 合并，
  12 秒缓存）；数据库不可用时降级为 `db_available:false`。
- **凭据边界**：控制台视图按白名单拼装，凭据字段结构性不出现；写路径拒绝改动
  `authcode` / `tokenKey` / `webhookSecret` / `webhookPathToken`。
- **主题适配**：页面颜色统一改用 Hermes 主题变量（`--midground` / `--color-*`），
  修复内置主题下正文透明（`--foreground` 的 alpha 为 0）导致整页文字不可见；
  深色与浅色主题下正文对比度均 ≥ 4.5:1。

---

## v2.2.0 — 2026-10-20

群成员变动与进群欢迎语。

- **成员变动识别**：新增 `inbound/join_events.py`，识别三种进群文案（邀请 / 扫码 /
  直接加入）与两种退群文案（主动退出 / 被移出）；**文本判据优先于 `msgType`**，
  厂商改类型或只推占位符时仍能命中。未识别的系统通知会记一条 INFO 留样本。
- **成员表增量维护**：进群/退群事件驱动拉取该群成员，diff 出「谁进谁出」写库；
  机器人自己被拉进群时拉全部群补基线。不做定时轮询。
- **进群欢迎语**：账号 JSON `joinWelcome` 段，三模式（`allowlist` / `blocklist` /
  `all`）；支持 `{name}` / `{group}` 模板、@ 新人、静默时段、`maxBatch` 防刷屏；
  默认关闭。
- **群画像润色（可选）**：`llmPolish=true` 且该群有画像时，用一句话结合群业务方向
  改写欢迎语；LLM 失败/超时/跑题一律回落到模板原文，不会因为润色失败而不发。
- **成员表可读化**：进群事件驱动更新成员昵称，心流提示词按可读名称呼人；新增
  `scripts/sync_members_once.py` 一次性补齐历史昵称（只读端点，物理上发不出消息）。
- **引用回复修正**：@ 前缀解析补一级「群成员表」，不再因为缓存缺昵称退化成 `@wxid`。

---

## v2.1.0 — 2026-10-20

面向公开分享的版本。

- **默认策略重排（2026-10-20）**：
  - 数据库默认 **SQLite**（`<插件目录>/wpp.sqlite3`），MySQL/MariaDB 改为可选；
    `schema.sql` 仍是唯一 DDL 真源，SQLite 侧运行时做确定性方言翻译。
    旧 MySQL 部署升级必须补 `WECHATPRO_DB_BACKEND=mysql`。
  - 私聊 / 群聊默认 **全部放行**；新增 `dmPolicy: blacklist`、`pairing`，
    群策略新增 `blacklist`。公众号、微信团队、微信支付等服务号在任何模式下都不回复。
  - OSS 媒体转存默认关闭，必须显式 `WECHATPRO_OSS_ENABLED=1`（不再“有凭据就开”）。
  - 心流默认关闭；启用时向导引导配置判定模型与向量模型。画像子能力默认开启，
    可单独关闭。
  - MCP、好感度、黑化(黑话)、风格、语音、主动关心等其他能力默认关闭。
- **新增交互式配置向导** `plugin/setup_wizard.py`：预检、逐项询问、写 `.env` 与账号
  JSON（0600、先备份、不回显密钥），并调用 `hermes plugins enable/doctor`。
- **`/pair` 兑换接通**：`dmPolicy=pairing` 下用户可私聊 `/pair <code>` 自助加入；
  已配对名单持久化在 `<插件目录>/pairing/<account>.paired.json`（0600）。
- **新增 `wpp_setup` 工具**：Hermes 里直接查询配置状态、获取分步引导，不在聊天中
  收集凭据。
- **安装体验**：`plugin.yaml` 补 `WECHATPRO_API_BASE` 必填项，敏感项标 `secret: true`；
  新增 `scripts/install.sh` / `uninstall.sh` / `check.sh` 与公开文档。
- **厂商地址不再有默认值**：未配置 `WECHATPRO_API_BASE` 时给出明确错误，而不是
  回落到某个固定域名。
- **测试与生产标识解耦**：生产 wxid / 凭据指纹改由
  `WECHATPRO_PROTECTED_WXIDS` / `WECHATPRO_PROTECTED_FINGERPRINTS` 提供；
  测试夹具与示例统一使用占位值。
- **拆分职责**：`wecom_*` 与 `phoneerp_*` 工具移出本插件，分别回归
  wecom-skill 与 phoneerp skill；本插件工具面只保留 `wpp_*`（292 个）。
- **客户文档**：新增 `docs/features.md`（心流、好感度、意图识别、黑话、风格、
  语音、历史检索的详细说明）与 `docs/tools.md`（292 个 `wpp_*` 工具分组清单）；
  README 补充文件传输助手命令与 wpp-openclaw / astrbot-plugin-wpp 生态链接。
- **文档修正**：明确 SQLite 是完整后端（历史检索/心流台账/审计等无需 MariaDB）；
  LLM key 统一说明为 `JUDGE_API_KEY`，移除未被代码读取的 `WECHATPRO_LLM_API_KEY`。
- 新增 `scripts/check_public_tree.py`，CI 同时跑 lint、测试与敏感信息扫描。
- **静默段拆分（2026-10-20 生产修复）**：画像建议超过 6h 的静默段（如 `23–07`）
  拆成 ≤6h 的连续段后生效，不再整段丢弃；全天合计仍 ≤8h，超过 8h 的仍整段丢弃。
  手动配置的 `quietHours` 不受影响。

---

## v2.0.3 — 2026-10-19

审计后的五项收口（结构 / 噪音 / 可观测性 / 依赖 / 文档）。

- **拆 `adapter.py`**：媒体链路（9 个方法、约 500 行）抽到 `platform/adapter_media.py`
  的 `MediaMixin`。`adapter.py` 3069 → 2573 行。MRO 保持 `WppAdapter(MediaMixin,
  BasePlatformAdapter)`，与原「类内定义覆盖基类」等价，调用点零改动。
- **告警去重**：`webhookSecretEnv` 空变量从"每 5 分钟一条"（一天 288 条）改为
  进程级只告警一次，且「从有变无」时重新告警；心流画像 `mute-phrase stripped`
  降 DEBUG（已处理状态不该每轮吵），「新生成时又被剥掉」仍是 WARNING。
- **可观测性**：`/metrics` 增加 Prometheus 文本格式
  （`?format=prom` 或 `Accept: text/plain`），JSON 快照保持默认；仍只回环可读。
- **依赖声明**：`pyproject.toml` 补 `[project].dependencies`
  （aiohttp / pymysql / websockets / pyyaml，上界按 next-major）与 `dev` 组。
- 测试 2517 通过 / 10 跳过 / 0 失败；ruff、结构守卫、注册探针(299) 全绿。

> 备注：审计报告里「79 个工具无 `required`」经复核**不是缺陷** —— 其中 77 个的参数
> 只有可选的 `account_id` 与分页字段，必填集本就为空；`registry.py:122-141` 的
> required 生成逻辑是正确的。

## v2.0.2 — 2026-10-19

深度审阅后的第二批修复（入站解析 / 出站投递 / 安全加固）：

**入站**
- **AddMsgs 包装解析**：vendor 的业务回调把每个字段裹成 `{"string": …}`（复杂字段还是
  JSON 字符串），旧解析对 `from_user` / `app` / `reply_context` / `voice` 直接当 dict 用 ⇒
  引用消息的「被回复人 / 原文」整段丢失、语音的厂商转写变成 `{'string': …}`。
  `parser` / `media` 统一解包后再取。
- **引用文件不再被当成文件**：`is_file_message` 先剥 `<refermsg>` 再判外层 appmsg ——
  引用一条文件消息的纯文本回复此前会走「文件无法下载」兜底话术。
- **引用接龙不算接龙**：`relay.strip_quoted_content` 统一剥掉被引用的原消息；
  `is_relay_message` 与 `_is_solitaire` 都用它 ⇒ 引用接龙卡片不再 100% 必回。
- **心流缓冲排除系统通知/撤回**：`MT_SYSTEM` / `MT_REVOKE` 不进缓冲，judge 上下文
  与「有人说话」沉默时长不再被污染。
- **语音标记按元数据判**：带语音元数据（`voice_ctx` / `vendor_transcript` / `stt_text`）
  就标 `[语音]`，不再依赖 `msg_type == 34` —— 49 appmsg 里承载的语音不再被兜底成
  `[文件]`、与同一条正文的 `[转写]` 自相矛盾。正常语音的正文形状不变：
  `[语音] 收到一条语音` + `\n[转写] …`。

**出站**
- **cron/standalone 附件真正投递**：`_standalone_send` 此前收了 `media_files` /
  `force_document` 却完全没消费（附件静默丢弃、调用方还收到 success）。现按扩展名与
  标志走 image/voice/video/file 四条链路，与 live `_send_media` 同款（含落库）。
- **空消息不再假成功**：`chunk_text` 对空/纯空白返回零片（此前 `[""]` 会真发空气泡）；
  standalone 在无文本且无附件时回 `error` 而不是 `success(message_id=None)`。
- **过滤判据收窄**：`💾` 必须带自审摘要标题才算机械通知；`无 @提及` 必须构成
  「……不回复」才算出站拒绝话术 —— 正常业务话术不再被静默丢弃。
- **文件消息 XML**：`fileext` 同样过 XML 转义；`totallen` 用 base64 长度估算，
  不再为算一个展示字段同步 `b64decode` 整个文件；超限在**上传前**拒绝。

**安全 / 可靠性**
- 本地读取：`/tmp` 下的凭据类文件（`.env*`、私钥、`credentials` 等）一律拒绝；
  已知 profile scope 时只放行本 profile 的工作目录，不再跨 profile 读。
- SSRF：CGNAT / 基准测试等非全局地址段一并拒绝。
- 厂商响应读取全部加上限（含 chunked / 缺 `Content-Length`）。
- 群发文本目标去重 + 上限 64（schema 与 handler 双判）。
- 管理员门禁新增 23 个不可逆/资金/认证类工具（共 57 个，默认仍关闭）。
- 热路径同步 pymysql 调用改走 `asyncio.to_thread`（含引用身份回查）。

测试：新增 `test_hf_note_wiring.py` / `test_standalone_media.py` 及多处回归；
全量 **2507 通过 / 10 跳过 / 0 失败**。

---

## v2.0.1 — 2026-10-19

出站与本地媒体三处修复（07:24/07:29 实测，老板在场）：

- **netguard**：多 profile 网关不带 `HERMES_PROFILE` env ⇒ profile 自己的
  `workspace/cache/tmp` 全部「本地媒体读取被拒」（wpp_send_image 从 profile 目录发图必挂）。
  env 缺失时改为扫描 `profiles/*/` 的工作目录面补白名单；拒绝名单（accounts 凭据、
  备份目录、插件点文件/JSON）仍**先于**白名单判定，凭据外泄面不变。
- **outbound_guard**：新增 `guard_targets()` 上下文——无收件人的上传步骤
  （`/Tools/UploadFile`）此前在 `WECHATPRO_SEND_ALLOWLIST` 开着时被 fail-closed
  永久拒绝 ⇒ **所有发文件都挂**。现在组合调用（UploadFile→ShareLink）把已校验
  收件人带进上传步骤；上下文目标同样逐个过白名单，白名单外照样拒。
- **toolkit/msg**：`_resolve_to_base64` 的报错用 `WppApiError` 单参构造（签名要求
  `(code, message)`），把「白名单/读取被拒」伪装成 `TypeError`，误导排查。

测试：`plugin/tests/test_security_hardening.py` 新增 ⑦⑧ 三例（上传上下文、
上下文只覆盖上传步骤、profile workspace 可读 + 凭据仍拒）；全量 2006 例通过。

---

## v2.0.0 — 2026-10-18

Hermes Python 侧重写完成，接管生产（profile `wpp-wechat`，单账号 `default`）。

### 能力

- **平台适配器**：WS + webhook 双通道入站；`autoSetWebhook` 三段式注册 + 周期刷新；
  媒体下载/上传（图片/视频/语音/文件，200MB 上限）；文本分片（>2000 字自动切）。
- **工具面 299 个**：厂商端点（由蓝本生成，`--check` 幂等）+ 注册期裁决层（撤注册/纠错/补注册）
  + 运行期门禁（小微 / 朋友圈发布 / 管理员）。
- **心流**：门槛判定、预算、画像、复读去重、静默段、learn 阈值、台账落库；热路径零 DB。
- **周边**：意图（LLM + embedding）、黑话挖掘、好感度、会话标题、语音转写、OSS 归档。
- **安全**：出站闸收口 `_raw_url`、SSRF 二次校验、本地读白名单排除凭据目录、
  webhook 双因素 fail-closed、日志全账号 token 打码。
- **测试**：2467 用例 / 0 失败 / 10 跳过（约 19s）；变异测试与结构守卫工具链齐备。

### 本次审计后的处置（同日）

- 蓝本源表从外部旧树迁入 `tools/vendor_source/`，生成器输入回到本仓（`--check` 往返等价）。
- 116 条工具描述重写：先补资金/群管理/好友/朋友圈等高危高频组，平均长度 29 → 35.2 字；
  低频运维类保持短描述以控制 tool_search 索引成本。
- `plugin.yaml` 补 `provides_hooks: [pre_llm_call]`，doctor 不再报「hook not listed」。
- `wpp_messages.new_msg_id` 定义漂移修复：5264 行 NULL 回填为 `#<id>` 占位后改为
  `NOT NULL DEFAULT ''`（唯一键 `uk_account_msg` 恢复对 NULL 的去重能力）。
- 旧树 `~/dev/wpp-hermes` 归档到 `~/dev/_archive/wpp-hermes-legacy-20261018`。
- 新增 `tools/fix_new_msg_id_drift.py`（人肉执行）与 `tools/diag_new_msg_id_dups.py`（只读诊断）。
- 群聊触发规则定稿：**被 @ 必回**（`group_mention` 直通 agent），**其余消息交心流 judge**
  （`heartflow_candidate`）。据此移除 `requireAtMention` 死键（账号模型/适配器/示例配置/
  死键告警一并删除）——它不是规则载体，留着只会让人以为"配了只回 @"。要收窄出声范围
  仍用 `groupPolicy` / `blacklistGroups` / 心流预算与阈值。

### 已知未做

- `platform/adapter.py` 仍是单文件 2855 行（媒体/入站/出站可继续拆 sibling）。
- 无 lint/type CI（ruff 配置待接入）。
