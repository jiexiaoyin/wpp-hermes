---
name: wpp-setup
description: 安装、配置与排障 WeChatPadPro Hermes 插件。
version: 1.0.0
author: wpp-hermes-py2 contributors
license: MIT
platforms: [linux, macos]
metadata:
  tags: "wechatpadpro,setup,configuration,troubleshooting"
  category: productivity
  related_skills: "wpp-history,wpp-identity,wpp-plugin-dev"
---

# wpp-setup — 安装与配置引导

用户问“怎么安装微信插件”“微信没反应”“authcode 填哪里”“检查连接状态”时使用本技能。
先调用 `wpp_setup` 读取状态，再引导用户在自己的终端运行向导；**绝不在聊天里收集
authcode / tokenKey**。

## When to Use

- 首次安装或升级 `wechatpadpro-platform` 插件。
- 插件已安装但平台不在线、账号未连接、收不到消息。
- 需要确认 API base、authcode、tokenKey、webhook、白名单是否配置齐全。
- 用户询问配置文件写到哪里、如何换 profile、如何卸载或回滚。

不适用：修改插件代码、厂商侧封号申诉、与 Hermes 无关的微信客户端问题。

## Prerequisites

- Hermes ≥ 0.21.5，插件 `wechatpadpro-platform` 已复制到
  `<home>/plugins/` 并启用。
- 用户能在服务器终端执行命令（向导是交互式的，密钥输入不回显）。
- 已从 WeChatPadPro 厂商控制台取得 API base、authcode、tokenKey。

## How to Run

1. 调用 `wpp_setup(action="check")`，读取 `home`、`profile`、`plugin_dir`、
   `missing`、`ready`。
2. 根据 `missing` 给用户逐项说明；密钥只让用户在终端输入。
3. 让用户运行向导（用状态里的真实路径，不要猜）：

   ```bash
   python3 <plugin_dir>/setup_wizard.py --home <home>
   ```

   如果 `home` 是 profile，也可以只传 profile：

   ```bash
   python3 <plugin_dir>/setup_wizard.py --profile <profile>
   ```

4. 向导完成后重跑 `wpp_setup(action="check")`，确认 `ready=true`。
5. 重启 gateway，让用户用白名单微信发一条测试消息。

## Quick Reference

| 场景 | 命令 / 动作 |
|---|---|
| 只读检查（agent 用） | `wpp_setup(action="check")` |
| 分步引导（agent 用） | `wpp_setup(action="guide")` |
| 交互式配置 | `python3 <plugin_dir>/setup_wizard.py --home <home>` |
| 无终端/自动化 | 设好 `WECHATPRO_API_BASE` / `WECHATPRO_AUTHCODE` / `WECHATPRO_TOKEN_KEY` 后加 `--non-interactive` |
| 覆盖已有配置 | 追加 `--force`（仍会先备份） |
| 跳过 doctor | 追加 `--skip-verify` |
| 安装/升级脚本 | `bash scripts/install.sh [--home DIR] [--profile NAME] [--upgrade]` |
| 卸载（保留账号/env） | `bash scripts/uninstall.sh` |

`wpp_setup(action="check")` 返回字段：

| 字段 | 含义 |
|---|---|
| `ready` | 凭据齐全且插件已启用 |
| `missing` | 缺失的必填变量名 |
| `plugin_enabled` | `config.yaml` 的 `plugins.enabled` 是否包含插件 |
| `account_file_exists` | 实际运行期账号 JSON 是否存在 |
| `api_base` / `ws_url` | 生效的地址（不含密钥） |
| `next_step` | 建议的下一步 |

## Procedure

### 1. 读取状态

调用 `wpp_setup(action="check")`。不要凭记忆列步骤，也不要把进程环境里的密钥值
转述给用户。字段为空时按 `missing` 引导，不要猜测配置位置。

### 2. 定位真实的配置目录

向导与运行期使用**同一个账号目录**：`<plugin_dir>/accounts/`。profile 的
`plugins/` 通常是指向全局插件目录的符号链接，所以不要按
`<profile>/plugins/<manifest-name>/accounts` 去找。`wpp_setup` 返回的
`plugin_dir` / `accounts_dir` / `env_file` 就是实际路径。

### 3. 让用户运行向导

在用户自己的终端执行：

```bash
python3 <plugin_dir>/setup_wizard.py --home <home>
```

向导会：

- 询问 API base、authcode、tokenKey（密钥输入不回显）；
- 可选配置 WebSocket、webhook、私聊白名单、MariaDB；
- 写入 `<home>/.env` 和账号 JSON（0600，先备份，不落明文密钥）；
- 尝试 `hermes plugins enable` 并运行 `doctor`。

`--home` 与 `--profile` 二选一即可；`--home` 优先。显式 `--home` 不会被重新解析回
`~/.hermes/profiles/<name>`。

### 4. 验证

```bash
hermes plugins doctor <plugin_dir>
hermes plugins validate <plugin_dir>
```

查看 gateway 日志（按服务管理器选择）：

```bash
journalctl -u hermes-gateway -n 80 --no-pager       # system service
journalctl --user -u hermes-gateway -n 80 --no-pager # user service
```

确认出现 `[WPP] 平台已注册`、`工具注册对账通过`，并且日志显示对应 profile 的
`wechatpadpro connected`。最后再让用户发测试消息。

## Pitfalls

- **不要在聊天里收集 authcode/tokenKey**；也不要让用户把密钥贴进群聊。
- **不要用 `<profile>/plugins/<name>/accounts` 判断是否配置**：运行期看的是
  `plugin_dir/accounts`。
- **multiplex gateway 的 `HERMES_HOME` 是宿主 home**，不是 profile；以
  `wpp_setup` 返回的 `home`/`profile` 为准。
- **同一 authcode 只能一个客户端使用**；被另一个客户端占用会把生产踢下线。
- **群聊没反应**先查 `groupPolicy` / `allowlist`，不要直接改代码。
- **向导失败不要重试写生产库**；没有终端时用 `--non-interactive`，不要伪造 TTY。
- **升级不会覆盖账号**：`install.sh` / `sync.sh` 都排除 `accounts/`。

## Verification

1. `wpp_setup(action="check")` 返回 `ready=true` 且 `missing=[]`。
2. `account_file_exists=true`，路径等于 `accounts_dir/default.json`。
3. `hermes plugins doctor <plugin_dir>` 显示注册数与 `plugin.yaml` 一致。
4. 日志出现 `wechatpadpro connected (profile: <name>)`。
5. 用户用白名单微信发一条私聊，能得到回复（或按预期触发心流）。
