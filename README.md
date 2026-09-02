# wpp-hermes 开发目录

> **微信插件（wechatpadpro）+ wpp skills 统一开发版**。开发只在此目录进行，改完后一键部署到 Hermes。

## 📁 目录结构

```
/root/dev/wpp-hermes/
├── plugin/                    # 📦 插件源码（wechatpadpro，35 个 .py）
│   ├── adapter.py             #   平台适配器（入站/出站/多账号）
│   ├── tools.py               #   工具注册（225+ 工具）
│   ├── tools_data.py          #   通用工具定义
│   ├── tools_data_extra.py    #   扩展工具定义
│   ├── api_client.py          #   vendor API 客户端
│   ├── triggers.py            #   触发判断（@机器人/接龙/心流）
│   ├── ...（其余 .py）...
│   ├── accounts/default.json  #   ⚠️ 含 authcode，勿提交 GitHub
│   ├── tests/                 #   测试
│   └── plugin.yaml
├── skills/                    # 🧩 wpp skills（7 个，2026-09-01 纳入）
│   ├── wpp-history/           #   消息历史查询
│   ├── wpp-identity/          #   昵称↔wxid 匹配
│   ├── wpp-friendcircle/      #   朋友圈发布
│   ├── wpp-friendcircle-stats/#   朋友圈统计
│   ├── wpp-friendcircle-view/ #   查看朋友圈
│   ├── wpp_stt/               #   语音转写（python 模块，插件依赖）
│   └── oss_archive/           #   OSS 归档（python 模块，插件依赖）
├── deploy.sh                  # 🚀 一键部署
├── README.md
└── .gitignore
```

## 🚀 快速部署（核心）

```bash
# 开发：直接编辑本目录（plugin/ 或 skills/）
vim /root/dev/wpp-hermes/plugin/tools.py
vim /root/dev/wpp-hermes/skills/wpp-history/SKILL.md

# 部署：一条命令（自动完成 6 步）
bash /root/dev/wpp-hermes/deploy.sh

# 只检查不部署（语法 + 差异预览）
bash /root/dev/wpp-hermes/deploy.sh --check

# 回滚到某次部署前的备份
bash /root/dev/wpp-hermes/deploy.sh --rollback /data/wpp-deploy-backup/wechatpadpro-20260901-103000
```

## 📋 部署流程（deploy.sh 自动执行）

| 步骤 | 动作 | 说明 |
|------|------|------|
| 1 | 语法检查 | plugin + skills 全部 .py，失败中止 |
| 2 | 备份 | plugin + 7 skills → `/data/wpp-deploy-backup/wechatpadpro-<时间戳>/` |
| 3 | 同步 plugin | → `/root/.hermes/plugins/wechatpadpro/` |
| 4 | 同步 skills | 逐个 → `/root/.hermes/skills/<同名>/`（不影响其他 skills） |
| 5 | 重启 gateway | `hermes gateway restart`（新代码生效） |
| 6 | 验证 | 检查日志 `[WPP] 平台已注册/工具注册/wpp_stt/oss-archive` |

## ⚠️ 安全注意

1. **`plugin/accounts/default.json` 含 authcode（凭证）** — 已在 .gitignore 排除，**禁止提交到 GitHub**
2. 部署自动备份（`/data/wpp-deploy-backup/`），改坏了随时回滚
3. 测试插件功能时**不要真发消息到生产群**（微信是生产环境）

## 📦 涉及范围

- **plugin/**：wechatpadpro 插件核心（35 .py + accounts + tests + plugin.yaml）
- **skills/**：7 个 wpp skills（含插件依赖的 wpp_stt / oss_archive）
- **不在本目录**（如需纳入可扩展 deploy.sh）：
  - 晨报脚本：`/root/.hermes/profiles/wpp-wechat/scripts/run_morning_report.sh`
  - wpp-wechat profile 配置：`/root/.hermes/profiles/wpp-wechat/`
