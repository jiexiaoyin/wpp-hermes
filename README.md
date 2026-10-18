# wpp-hermes-py2 — WeChatPadPro 的 Hermes 平台插件

个人微信（WeChatPadPro 协议）接进 Hermes Agent 的**平台适配器 + 工具面**。一个进程里同时提供：

- **平台**：收消息（WS + webhook 双通道）、发消息、媒体下载/上传、群/好友/朋友圈/视频号/公众号/支付等 299 个工具；
- **心流（heartflow）**：群聊里判断"要不要主动搭话"，含预算、画像、复读去重、静默段；
- **周边**：意图识别（LLM + embedding）、黑话挖掘、好感度记账、会话标题、语音转写。

本仓库是 **Python 侧唯一事实来源（SSOT）**。TS 版（`jiexiaoyin/wpp-openclaw`）是历史实现，
两者在工具面上由 `plugin/toolkit/parity.py` 与 `tests/test_ts_wire_alignment.py` 对齐。

---

## 架构

```
                    ┌──────────── Hermes gateway ────────────┐
  微信服务器 ──WS──▶ │ platform/inbound_ws.py                 │
             ──HTTP─▶ │ platform/inbound_webhook.py（路径token鉴权）│
                    │            │                            │
                    │            ▼                            │
                    │  inbound/debounce.py（合并突发）          │
                    │            │                            │
                    │            ▼                            │
                    │  platform/adapter.py::_handle_one       │
                    │    ├─ inbound/parser.py   解析成 WppMessage
                    │    ├─ inbound/triggers.py 触发判定（@/关键词/白名单）
                    │    ├─ inbound/intent.py   群聊上下文与意图
                    │    ├─ inbound/heartflow/  是否主动搭话（热路径零 DB）
                    │    └─ data/repo_*.py      落库（MySQL/MariaDB）
                    │            │
                    │            ▼
                    │  core/api_client.py ──▶ vendor HTTP API
                    └────────────────────────────────────────┘
```

六个包，依赖单向（`toolkit → platform → inbound → data → core`，无环）：

| 目录 | 职责 |
|---|---|
| `core/` | 厂商 HTTP 客户端、出站闸、netguard（SSRF/白名单）、脱敏、账号解析、metrics |
| `data/` | MySQL 连接、schema.sql、迁移/漂移检测、各域仓储（消息/心流/好感度） |
| `inbound/` | 消息解析、触发判定、debounce、意图、心流全套、语音/媒体处理 |
| `platform/` | 平台适配器、双通道入站、出站发送、媒体、命令、配对、webhook 路由 |
| `toolkit/` | 299 个工具的定义与注册（端点表由生成器产出，裁决层在 `policy.py`） |
| `llm/` | judge（心流判定）与 embedding 客户端 |

---

## 快速开始

```bash
# 测试（不需要 Hermes 运行时；conftest 会 stub 掉 gateway.*）
python3 -m venv /tmp/wpvenv && /tmp/wpvenv/bin/pip install pytest pyyaml aiohttp
env -u WECHATPRO_SEND_ALLOWLIST /tmp/wpvenv/bin/python -m pytest -q   # 2467 用例，约 20s

# 全部门禁（结构 / 生成物幂等 / 注册对账 / LLM judge）
tools/gates.sh

# 部署（dev → 生产；自动备份 + md5 逐位校验）
bash deploy.sh --local          # 本机生产（.182）
bash deploy.sh                  # 远程（默认 .183）
bash deploy.sh --check          # 只看差异，不落盘
```

> ⚠️ **改代码只在 dev 树改**，deploy 目录是产物。铁律与红线见 `DEVELOPMENT_RULES.md`。

---

## 工具面

- **299 个工具**：`wpp_*`（厂商端点）+ `wecom_*`（企微）+ `phoneerp_*`（ERP）。
- 端点定义由 `tools/gen_vendor_defs.py` 从 `tools/vendor_source/` 蓝本生成 →
  `plugin/toolkit/vendor_defs{,_extra}.py`（**禁止手改**，`--check` 是部署门禁）。
- 注册期的撤注册/纠错/补注册统一在 `toolkit/policy.py`（每条都带 TS 侧 `file:line` 出处）。
- 运行期门禁：`XIAOWEI_GATE` / `FRIEND_CIRCLE_PUBLISH_GATE` / `ADMIN_TOOLS_GATE`，
  统一挂在 `toolkit/hermes_contract.wrap_ctx` 上——新增工具忘不掉。
- 另有一路 vendor MCP（40 个工具，`mcp_servers.wechatpad`），由 Hermes 侧直连；
  插件内 `core/vendor_mcp.py` 是**独立**的 stdlib 通道，用于本机直连与降级。

---

## 安全模型（5 条，都是踩过坑才有的）

1. **出站闸收口在 `core/api_client._raw_url`** —— 所有厂商请求拼 URL 的唯一出处，
   99% 的新增发送路径在结构上绕不过去。`WECHATPRO_SEND_ALLOWLIST` 设了之后白名单外直接 raise。
2. **SSRF**：解析 DNS 后**二次校验 IP**（防 DNS rebinding），只放行公网地址。
3. **本地读白名单**：`realpath` 规范化（挡符号链接），并**显式排除** `plugin/accounts/`
   与备份目录——"允许读插件目录"不能等于"允许读凭据"。
4. **webhook 双因素**：路径 token + secret，两者都没有直接 fail-closed。
5. **凭据**：`accounts/*.json` 只放 `*.example.json`；文件 `0o600`、目录 `0o700`；
   日志里所有账号的 token 一律打码。

---

## 关键不变量（改代码前先读）

- **热路径零 DB**：心流判定只看内存缓存（sweep 预热）。`tests/heartflow/test_hot_path_no_db.py`
  用 `db_call_count()` 仪器锁死这一点。
- **注册期不 import 第三方**：`toolkit/` 子树模块级只允许 stdlib + 同包——Hermes 的
  `plugin_validate` 在隔离解释器里跑 `register()`，模块级 `import aiohttp` 会让工具**静默不注册**。
- **所有 handler 是 `handle(args, **kw)`**：`**params` 和位置参数默认值都会在真机派发时炸
  （`toolkit/hermes_contract.py` 有完整取证）。
- **单点失败不得中断整批注册**：平台/工具/hook 三段各自 try，工具面再按子域隔离。

---

## 测试

| 类型 | 位置 | 说明 |
|---|---|---|
| 单元/契约 | `plugin/tests/` | 2467 用例，18–20s |
| 结构守卫 | `tools/verify_tree.py` | 包形状、扁平模块回流、加载期第三方依赖 |
| 注册对账 | `tools/probe_registration.py` | 真跑 `register()`，与 `plugin.yaml` 逐名比对 |
| 生成物幂等 | `gen_vendor_defs.py --check`、`port_tools_domain.py --check` | 防手改生成物 |
| 变异测试 | `tools/mutation_check.py` | 按测试能否杀死变异体评估用例质量 |
| 真机 E2E | `tools/verify_e2e_183.py` | 需要目标机的 Hermes 运行时 |

测试默认**禁网**、**禁生产库**（库名必须以 `_test` 结尾）、**禁生产 wxid**——
三条护栏在根 `conftest.py`，违反直接 fail。

---

## 目录速查

```
plugin/            插件本体（Hermes 从这里加载）
  accounts/        账号配置（*.json 不提交，*.example.json 是 schema 范例）
  core/ data/ inbound/ platform/ toolkit/ llm/
  tests/
tools/             门禁、生成器、探针、变异测试
tools/vendor_source/  端点蓝本（生成器的输入，唯一真源）
deploy.sh          部署（备份 → rsync → 语法复检 → md5 逐位 → 重启 → doctor 验收）
DEVELOPMENT_RULES.md  铁律与红线（先读这个）
CHANGELOG.md       版本记录
```

---

## 排障入口

```bash
# 插件是否真的注册了
HOME=/root HERMES_HOME=<profile> hermes plugins doctor /root/.hermes/plugins/wechatpadpro

# 生产日志里的插件行
grep hermes_plugins.wechatpadpro <profile>/logs/errors.log | tail

# schema 漂移（只报不改）
# 启动日志里的「schema 漂移 N 处」；修列型要人肉跑 tools/fix_new_msg_id_drift.py

# vendor MCP 是否通
HOME=/root HERMES_HOME=<profile> hermes mcp test wechatpad
```
