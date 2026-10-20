---
name: wpp-setup
description: "WeChatPadPro Hermes 插件的安装与配置引导。用于「怎么安装微信插件」「微信没反应怎么配置」「检查微信连接状态」「authcode 填哪里」等问题。先调用 wpp_setup 工具读取状态，再引导用户在自己的终端运行 setup_wizard.py；绝不在聊天里收集 authcode/tokenKey。"
---

# wpp-setup — 安装与配置引导

## 适用 agent

| Agent | 是否适用 | 原因 |
|-------|---------|------|
| main | ✅ | 安装 / 配置 / 排障 |
| wpp-wechat | ✅ | 运行中配置检查 |
| 其他 | ✅ | 只读检查无副作用 |

## 原则

1. **密钥只走终端**：不通过聊天询问、转述或记录 `authcode` / `tokenKey`；
2. **先读状态再给建议**：不要凭记忆列步骤；
3. **只做只读检查**：写配置由用户运行向导完成；
4. 不确定时看 `docs/install.md`、`docs/configuration.md`、`docs/troubleshooting.md`。

## 步骤

### 1. 读取状态

调用 `wpp_setup(action="check")`。返回字段：

| 字段 | 含义 |
|---|---|
| `ready` | 凭据齐全且插件已启用 |
| `missing` | 缺失的必填变量名 |
| `plugin_enabled` | `config.yaml` 是否启用插件 |
| `account_file_exists` | 是否已有账号 JSON |
| `next_step` | 建议的下一步 |

也可以直接运行（不经模型）：

```bash
python3 "$HERMES_HOME/plugins/wechatpadpro-platform/setup_wizard.py" --check --json
```

### 2. 根据结果引导

| `missing` / 状态 | 引导 |
|---|---|
| 缺 `WECHATPRO_API_BASE` | 让用户准备 vendor 地址（`https://...`） |
| 缺 `WECHATPRO_AUTHCODE` / `WECHATPRO_TOKEN_KEY` | 让用户从 vendor 控制台获取；**不要让他发到聊天里** |
| `plugin_enabled=false` | `hermes plugins enable wechatpadpro-platform` |
| 已就绪但无账号文件 | 可选：运行向导写账号文件（白名单 / 多账号） |
| 全部就绪 | 重启 gateway，然后用白名单微信发一条测试消息 |

### 3. 运行向导

让用户在自己的终端执行：

```bash
python3 "$HERMES_HOME/plugins/wechatpadpro-platform/setup_wizard.py"
```

向导会：

- 询问 API base、authcode、tokenKey（密钥输入不回显）；
- 可选配置 webhook、私聊白名单、MariaDB；
- 写入 `<HERMES_HOME>/.env` 与账号 JSON（权限 0600，先备份）；
- 尝试 `hermes plugins enable` 并跑 `doctor`。

### 4. 验证

```bash
hermes plugins doctor "$HERMES_HOME/plugins/wechatpadpro-platform"
journalctl --user -u hermes-gateway -n 80 --no-pager
```

确认平台注册、账号连接成功后，再让用户发测试消息。

### 5. 常见问题

- **平台不在线**：检查 Hermes 版本 `>= 0.21.5`、依赖是否装进 Hermes 解释器；
- **收到 403**：webhook secret 与 vendor 不一致，或路径 token 不对；
- **私聊没反应**：用户不在 `allowFrom` / `WECHATPRO_ALLOWED_USERS`；
- **群聊没反应**：`groupPolicy` 为 `disabled` / `closed`，或 `allowlist` 未包含该群；
- **凭据被踢**：同一 authcode 被另一个客户端使用。

详细排查见 `docs/troubleshooting.md`。
