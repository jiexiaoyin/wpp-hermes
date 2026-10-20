# 安装、升级与卸载

## 前置条件

| 项 | 要求 |
|---|---|
| Hermes Agent | `>= 0.21.5`（插件使用 `env_enablement_fn` 挂载点） |
| Python | `>= 3.11` |
| vendor | 一个可用的 WeChatPadPro 兼容服务，以及 `authcode` / `tokenKey` |
| 网络 | 插件所在机器能访问 vendor 的 HTTP / WebSocket 地址 |
| 可选 | MariaDB、ffmpeg、silk 编解码器、LLM / STT key（缺失时对应能力降级） |

确认 Hermes 版本：

```bash
hermes --version
```

## 方式 A：Hermes 原生安装

```bash
hermes plugins install <owner>/<repo>#plugin --yes-deps --enable
```

- `#plugin` 表示只取仓库里的 `plugin/` 子目录（Hermes 的插件根目录）。
- `--yes-deps` 自动确认安装 `plugin.yaml` 里声明的 Python 依赖。
- 安装器会逐项询问 `requires_env`：`WECHATPRO_API_BASE`、`WECHATPRO_AUTHCODE`、
  `WECHATPRO_TOKEN_KEY`，写入当前 `HERMES_HOME/.env`。
- 标了 `secret: true` 的项输入不回显。

装完后补跑向导（账号文件、白名单、webhook、profile 绑定）：

```bash
python3 "$HERMES_HOME/plugins/wechatpadpro-platform/setup_wizard.py"
```

## 方式 B：安装脚本

```bash
git clone <repo-url> wpp-hermes-py2
cd wpp-hermes-py2
bash scripts/install.sh
```

常用参数：

```bash
bash scripts/install.sh --home /srv/hermes        # 指定 Hermes home
bash scripts/install.sh --profile wpp-wechat      # 装进某个 profile
bash scripts/install.sh --dry-run                 # 只打印将执行的操作
bash scripts/install.sh --no-wizard               # 只复制并启用
```

脚本行为：

1. 检查 `HERMES_HOME` 与目标插件目录；
2. 把 `plugin/` 复制到 `<home>/plugins/wechatpadpro-platform`（已有目录时先备份）；
3. 调用 `hermes plugins enable wechatpadpro-platform`；
4. 调用 `setup_wizard.py` 完成配置；
5. 打印 `hermes plugins doctor` 与重启命令。

## 方式 C：手动安装

```bash
mkdir -p "$HERMES_HOME/plugins"
cp -r plugin "$HERMES_HOME/plugins/wechatpadpro-platform"
chmod 700 "$HERMES_HOME/plugins/wechatpadpro-platform/accounts" 2>/dev/null || true
python3 "$HERMES_HOME/plugins/wechatpadpro-platform/setup_wizard.py"
hermes plugins enable wechatpadpro-platform
hermes plugins doctor "$HERMES_HOME/plugins/wechatpadpro-platform"
```

如果 Hermes 使用隔离解释器，还需要把依赖装进它：

```bash
<hermes-python> -m pip install 'aiohttp>=3.9,<4' 'websockets>=12,<16' 'pymysql>=1.1,<2'
```

## 多 profile

Hermes 的 profile 是独立的 `HERMES_HOME`（通常位于 `~/.hermes/profiles/<name>`）。
每个账号应绑定自己的 profile：

```bash
hermes profile create wpp-wechat
bash scripts/install.sh --home ~/.hermes/profiles/wpp-wechat
```

账号 JSON 里的 `agent` 字段写 profile 名；**不要写 `main`**，否则多账号会串号。

## 升级

```bash
git pull
bash scripts/install.sh --upgrade
hermes plugins doctor "$HERMES_HOME/plugins/wechatpadpro-platform"
systemctl --user restart hermes-gateway     # 或你实际的启动方式
```

`--upgrade` 会保留 `accounts/` 与 `.env`，只替换代码。升级前脚本会把旧插件目录
备份到 `<home>/plugins/.backup-wechatpadpro-platform-<timestamp>`。

## 卸载

```bash
bash scripts/uninstall.sh                 # 停用并删除插件目录，保留 .env 与账号
bash scripts/uninstall.sh --purge         # 连账号目录一起删
bash scripts/uninstall.sh --home /srv/hermes
```

手动卸载：

```bash
hermes plugins disable wechatpadpro-platform
rm -rf "$HERMES_HOME/plugins/wechatpadpro-platform"
```

`.env` 里的 `WECHATPRO_*` 变量需要自己清理，脚本不会动它（可能和其他插件共用）。

## 离线安装

```bash
git clone <repo-url> wpp-hermes-py2
cd wpp-hermes-py2
bash scripts/install.sh --no-wizard
# 然后按 docs/configuration.md 手工写 .env 或运行向导
```

依赖需要提前放进 Hermes 的解释器；`--no-wizard` 不会联网。
