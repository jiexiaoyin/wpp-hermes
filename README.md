# wpp-hermes

把**个人微信**（WeChatPadPro 协议）接进 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的平台插件。

装上之后，Hermes 会多出一个 `wechatpadpro` 平台：收发私聊/群聊、下载上传媒体，
并暴露近 300 个 `wpp_*` 工具（好友、群、朋友圈、视频号、公众号、支付、搜索等）。
历史数据默认落 SQLite（零配置），也可选 MariaDB/MySQL。可选能力包括群聊心流、
好感度、意图识别、黑话挖掘、说话风格、语音转写和 OSS 媒体转存；这些能力**默认关闭**，
缺失时都会**明确降级**，不阻塞基本收发。

> 本项目只做协议适配，不附带、不托管任何 WeChatPadPro 服务。你需要自己有一个可用的
> WeChatPadPro 兼容服务（下文称 “vendor”）以及它的 `authcode` / `tokenKey`。

## 核心能力

**基础能力（装好即用）**

- 私聊 / 群聊收发：文本、图片、文件、视频、语音、引用回复、@、链接卡片、位置；
- **292 个 `wpp_*` 工具**：好友、群管理、朋友圈、视频号、公众号、支付、搜索等；
- 媒体下载与上传、历史消息落库（SQLite 零配置，可选 MySQL/MariaDB）；
- 准入策略：`open` / `allowlist` / `blacklist` / `pairing`，公众号与服务号永远静默；
- 单机多账号：同一 vendor 地址按 `authcode` 隔离，每账号独立 WS / 策略 / 数据；
- 文件传输助手命令：运行时开关能力、管理白名单、查状态，不用改文件重启。

**特色能力（按需开启，失败自动降级）**

- **群聊心流 + 群画像**：判断 bot 何时该接话，按群画像控制预算、静默段与语气；
- **成员变动识别**：进群 / 退群 / 被移出事件驱动更新成员表，心流按真实昵称称呼群友；
- **进群欢迎语**：模板欢迎 + 可选群画像润色、@ 新人、静默时段、`maxBatch` 防刷屏；
- **好感度 / 意图识别 / 黑话挖掘 / 说话风格**：让群聊回复更像本群的人；
- 语音转写与情绪化 TTS、OSS 媒体转存、私聊主动关心、MCP 精简工具集；
- 默认全部放行，需要收紧时再按账号切换策略；任何增强能力失效都不阻塞基本收发。

---

## 相关项目

本仓库是 WeChatPadPro 跨平台适配插件生态的一部分。三个仓库共用同一套 vendor API，
按你使用的 Agent 平台选择：

| 项目 | 平台 | 语言 | 说明 |
|---|---|---|---|
| [wpp-hermes](https://github.com/jiexiaoyin/wpp-hermes)（本仓库） | Hermes Agent | Python 3 | 平台插件 + 292 个 `wpp_*` 工具 |
| [wpp-openclaw](https://github.com/jiexiaoyin/wpp-openclaw) | OpenClaw | TypeScript | OpenClaw channel 插件 |
| [astrbot-plugin-wpp](https://github.com/jiexiaoyin/astrbot-plugin-wpp) | AstrBot | Python 3 | AstrBot 平台适配器 |

三者能力方向一致，但配置格式与安装方式不同；不要混用配置文件。第一次接触这个生态时，
可以先在任意一个仓库跑通 vendor 连接，再按所用 Agent 平台迁移配置。

---

## 快速开始

### 方式 A：Hermes 原生安装（推荐）

```bash
hermes plugins install <owner>/<repo>#plugin --yes-deps --enable
```

安装器会读取 `plugin/plugin.yaml` 的 `requires_env`，逐项询问 API 地址、authcode、
tokenKey，并写入当前 `HERMES_HOME/.env`（敏感项不回显）。

然后跑一次补充向导（账号文件、准入策略、数据库、webhook、profile 绑定）：

```bash
python3 "$HERMES_HOME/plugins/wechatpadpro-platform/setup_wizard.py"
```

重启 gateway：

```bash
systemctl --user restart hermes-gateway    # 或你实际的启动方式
```

### 方式 B：脚本安装

```bash
git clone <repo-url> wpp-hermes
cd wpp-hermes
bash scripts/install.sh
```

脚本会复制插件到 `$HERMES_HOME/plugins/wechatpadpro-platform`、启用插件并调用向导。
用 `--home` 指定其他 Hermes home，用 `--dry-run` 只看将做什么。

### 方式 C：手动安装

```bash
cp -r plugin "$HERMES_HOME/plugins/wechatpadpro-platform"
python3 "$HERMES_HOME/plugins/wechatpadpro-platform/setup_wizard.py"
hermes plugins enable wechatpadpro-platform
hermes plugins doctor "$HERMES_HOME/plugins/wechatpadpro-platform"
```

装好后可以在 Hermes 里说“帮我配置微信”，模型会调用 `wpp_setup` 工具读取配置状态
并给出下一步；**密钥始终由你自己在终端输入**，不经过聊天。

---

## 配置

最小配置只有三项：

| 变量 | 说明 |
|---|---|
| `WECHATPRO_API_BASE` | vendor 地址，如 `https://your-vendor.example` |
| `WECHATPRO_AUTHCODE` | vendor 分配的客户端凭据 |
| `WECHATPRO_TOKEN_KEY` | vendor 分配的客户端 token |

其余都有合理默认值。默认行为：

- **准入**：所有群聊、私聊全部放行；可选 `allowlist` / `blacklist` / `pairing`，
  公众号与微信团队/支付等服务通知在任何模式下都不回复。
- **数据库**：SQLite（`<插件目录>/wpp.sqlite3`），可选 MySQL/MariaDB。
  SQLite 是完整后端：历史检索、心流台账、审计、黑话/风格/好感度持久化都可以直接使用，
  **不需要安装 MariaDB**。
- **能力**：OSS 转存、心流、好感度、黑话、风格、语音、主动关心、MCP 等默认关闭；
  群画像（心流子能力）默认开启。

完整变量表、账号 JSON 字段、多账号隔离、webhook 与数据库配置见
[docs/configuration.md](docs/configuration.md)。

⚠️ 一个客户端密钥同一时间只能被一个客户端使用。复制账号文件或 profile 之前，先确认
没有两个进程拿同一份凭据连接 vendor。

---

## 文件传输助手命令

在微信「文件传输助手」里给机器人发命令，可以运行时查看和修改当前账号的策略；
`/pair` 则在私聊里由普通用户发送。常用命令：

| 命令 | 作用 |
|---|---|
| `/help` | 显示完整命令列表与心流运维子命令 |
| `/heartflow on\|off\|status` | 心流开关与状态 |
| `/affection on\|off\|status` | 好感度开关与状态 |
| `/jargon on\|off\|status` | 黑话挖掘开关与状态 |
| `/style on\|off\|status` | 说话风格开关与状态 |
| `/tts on\|off\|status` | 语音合成 / 情绪路由开关 |
| `/proactive on\|off\|status` | 私聊主动关心开关 |
| `/mute <群id> [分钟]` / `/mute off <群id>` / `/mute list` | 临时禁言某个群 |
| `/xiaowei on\|off\|status` | 小微智能体工具开关 |
| `/user add\|del\|list [wxid...]` | 私聊白名单（收发共用） |
| `/user sync` | 从全员群同步员工到私聊白名单 |
| `/group add\|del\|list [群id...]` | 群白名单（收发共用） |
| `/blacklist add\|del\|list [群id...]` | 群黑名单（一票否决） |
| `/whitelist status` / `/whitelist sync` | 查看统一白名单 / 同步全员群 |
| `/genpair` / `/pairs` | 管理员生成 / 查看配对码 |
| `/pair <8位码>` | 普通用户在私聊里自助配对（`dmPolicy=pairing`） |

命令改动会写回账号 JSON 并立即生效；写入失败时会明确回复“本次改动未生效”，
不会假装成功。完整说明见 [docs/features.md](docs/features.md)。

---

## Web 控制台（可选）

插件自带一个 Hermes dashboard 页签（`/wpp-console`），不修改 Hermes 本体代码：

- **账号状态**：真实 WS 在线状态、客户端就绪、最近一次热加载结果与快照新鲜度
  （`live` / `delayed` / `stale` / `unknown`）。
- **准入策略**：私聊/群策略、白/黑名单、管理员、`employeeDenyList`，并提示
  `allowAllUsers`、空白名单等配置陷阱。
- **进群欢迎**：开关、模式、生效群、模板、LLM 润色、@ 新成员、静默时段、批量上限。
- **能力开关**：心流 / 好感度 / 黑话 / 风格 / TTS / 主动发言 / 小微。
- **群活跃**：群名、成员数（厂商/本地）、近 6h–7d 收发出入站、最后同步时间。
- **热加载**：保存后网关约 **3 秒**内重载该账号，不需要重启 Hermes；
  写入前自动备份、原子替换、文件保持 `0600`，凭据字段结构性不可写。

启用方式（每个要显示页签的 profile 各加一次）：把 `wechatpadpro-console` 加进该
profile 的 `plugins.enabled`，然后重启 `hermes-dashboard` 与网关：

```yaml
# $HERMES_HOME/config.yaml（root / default）
plugins:
  enabled:
    - wechatpadpro-console
```

要让次级 profile（如 `wpp-wechat`）也显示页签，同样把名字加进
`$HERMES_HOME/profiles/<name>/config.yaml` 的 `plugins.enabled`。Hermes 会把次级
profile 的插件后端转发到**该 profile 自己的 plugin host** 进程运行；没有启用的
profile 只会返回 404。账号绑定在哪个 profile 不影响控制台管理——它直接读写
`accounts/*.json` 与插件数据库；在线状态快照从 launch home 读取。

控制台**不调用厂商 API、不重连账号、不显示也不写凭据**；`enabled` 与端点变更仍需重启
网关。数据库统计只读，配置缺失时页面降级为“数据库不可用”，策略与开关不受影响。
详见 [docs/configuration.md](docs/configuration.md#7-web-控制台可选)。

---

## 验证

```bash
# 只读检查（不打印任何密钥）
python3 "$HERMES_HOME/plugins/wechatpadpro-platform/setup_wizard.py" --check --json

# Hermes 侧验收
hermes plugins doctor "$HERMES_HOME/plugins/wechatpadpro-platform"
hermes plugins list

# 日志
journalctl --user -u hermes-gateway -n 80
```

看到平台注册成功、账号连接成功，然后用微信给机器人发一条私聊（默认放行）。

---

## 能力与降级

| 能力 | 依赖 | 缺失时 |
|---|---|---|
| 基本收发、媒体 | 无（仅 vendor 凭据） | — |
| 历史检索 / 心流台账 / 审计 / 黑话与风格持久化 | SQLite（默认，零配置）或 MySQL/MariaDB（可选） | 两种后端功能等价；SQLite 无需安装数据库服务；数据库不可用时降级，不影响收发 |
| 群聊心流 / 好感度 / 黑话 / 风格（仅群聊） | 显式开启 + LLM key | 对应模块不启用 |
| 成员变动识别 | 无（事件驱动） | 只少更新成员表，不影响收发 |
| 进群欢迎语 | `joinWelcome.enabled=true` | 不发欢迎语 |
| 欢迎语群画像润色 | `joinWelcome.llmPolish=true` + 心流模型 + 群画像 | 回落为模板原文 |
| 主动关心（仅私聊） | `proactive.enabled=true` + `allowFrom` 白名单 | 不主动发消息 |
| OSS 媒体公网 URL | `WECHATPRO_OSS_ENABLED=1` + 凭据 + ossutil | 消息不带 URL，照常收发 |
| 语音转文字（私聊/群聊） | silk 解码器 + STT key | 第一条语音打一次 WARNING |
| 出站语音（私聊/群聊） | silk 编码器 | 降级为文件发送 |
| 视频缩略图 | ffmpeg | 缩略图留空，视频照发 |

---

## 安全模型

1. **出站收口**：所有 vendor 请求都经过 `core/api_client`，可配 `WECHATPRO_SEND_ALLOWLIST`。
2. **SSRF 防护**：DNS 解析后二次校验 IP，只放行公网地址。
3. **本地读白名单**：`realpath` 规范化，显式排除账号目录与备份目录。
4. **webhook 双因素**：路径 token + secret，缺一 fail-closed。
5. **凭据**：账号文件 `0600`、目录 `0700`；日志中的 token 一律打码；生产 bot 可用
   `WECHATPRO_PROTECTED_WXIDS` 防止测试实例误连。
6. **准入**：默认全部放行是产品要求（2026-10-20）；需要收紧时在账号 JSON 配
   `dmPolicy` / `groupPolicy`。`open` 系模式依赖 `WECHATPRO_ALLOW_ALL_USERS=1`
   先过 Hermes 网关，真实过滤仍由插件按账号逐条完成。

发现安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

---

## 开发

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest plugin/tests -q
.venv/bin/ruff check plugin
bash scripts/check.sh          # pytest + ruff + 公开树敏感信息扫描
```

测试默认**禁网**、**禁生产库**（库名必须以 `_test` 结尾）、可用
`WPP_TEST_PROD_WXID` 增加生产 bot 护栏。贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 文档

- [功能详解](docs/features.md) — 心流、群画像、成员变动、进群欢迎、好感度、意图识别、黑话、风格、语音、历史检索
- [wpp_* 工具清单](docs/tools.md) — 292 个工具的分组说明与必填参数
- [安装与升级](docs/install.md)
- [配置参考](docs/configuration.md)
- [排障](docs/troubleshooting.md)
- [架构与数据流](docs/architecture.md)

## 许可证

MIT，见 [LICENSE](LICENSE)。
