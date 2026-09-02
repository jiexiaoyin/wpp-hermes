# wpp-hermes

**WeChatPadPro (WPP) platform adapter for [Hermes Agent](https://github.com/jiexiaoyin) — Python 3 implementation.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-1.0.0-green)](https://github.com/jiexiaoyin/wpp-hermes/releases)
[![GitHub stars](https://img.shields.io/github/stars/jiexiaoyin/wpp-hermes?style=social)](https://github.com/jiexiaoyin/wpp-hermes/stargazers)
[![Code style](https://img.shields.io/badge/code%20style-PEP8-orange)](https://peps.python.org/pep-0008/)

> Relays WeChat chats between the WPP vendor (`/ws/sync` push + `/Msg/SendTxt` send) and the Hermes agent. Migrated from [wpp-openclaw](https://github.com/jiexiaoyin/wpp-openclaw) **v1.3.80** (TypeScript) to a Hermes-native Python adapter.

---

## ✨ Features

- **247+ tools** covering the full WeChatPadPro vendor API surface (`/Msg`, `/Friend`, `/Group`, `/FriendCircle`, `/Label`, `/Finder`, `/TenPay`, `/Wxapp`, `/Xiaowei`, etc.)
- **Multi-account B scheme** — per-account authcode isolation, dedicated Hermes profile binding, no cross-account fallback
- **Real-time WebSocket channel** (`wss://.../ws/sync`) with exponential backoff + Webhook fallback for legacy vendors
- **AI augmentation modules**:
  - `heartflow` — autonomous group participation (5-dimension scoring, independent trigger)
  - `jargon` — group jargon mining (background LLM extraction + query tool)
  - `affection` — interaction scoring + emotional state injection
  - `intent-embed` — embedding fast-path for context injection
  - `intent-llm` — LLM-based candidate selection
- **Media pipeline**: SILK audio codec, STT transcription (SiliconFlow), OSS archive (Aliyun), thumbnail enrichment
- **Hermes skills** (extracted reusable SKILL.md modules): `wpp-history`, `wpp-identity`, `wpp-friendcircle`, `wpp-friendcircle-stats`, `wpp-friendcircle-view`, `wpp_stt`, `oss_archive`
- **Quote reply, file deterministic reply, group @-mention routing, group debounce**

## 📦 What's included

```
wpp-hermes/
├── plugin/                          # 📦 Hermes platform adapter (31 .py files, ~8.6k LOC)
│   ├── adapter.py                   #   WS inbound + send outbound + account registry
│   ├── tools.py / tools_data*.py    #   Tool registration (247+ tools, 20+ domains)
│   ├── api_client.py                #   WPP vendor HTTP API client
│   ├── heartflow.py / affection.py  #   AI augmentation modules
│   ├── intent_embed.py / intent_llm.py
│   ├── message_parser.py / media.py / silk.py / stt.py
│   ├── phoneerp_tools.py            #   PhoneERP ERP business integration
│   ├── wecom_tools.py               #   WeCom customer management
│   ├── accounts/boss2.example.json  #   Example multi-account config (no secrets)
│   ├── tests/                       #   Multi-account test harness
│   └── plugin.yaml                  #   Hermes platform manifest
├── skills/                          # 🧩 7 reusable SKILL.md modules
│   ├── wpp-history/                 #   Message history SQL templates
│   ├── wpp-identity/                #   wxid ↔ nickname matching (multi-source)
│   ├── wpp-friendcircle/            #   Friend circle publish (text/image/video)
│   ├── wpp-friendcircle-stats/      #   Statistics (frequency / type / timing)
│   ├── wpp-friendcircle-view/       #   View + summarize friend timelines
│   ├── wpp_stt/                     #   Voice → text transcription
│   ├── oss_archive/                 #   Aliyun OSS upload
│   └── wpp_tools_map.json           #   61KB SSOT tool index (221 tools + 8 group maps)
├── README.md
├── DEVELOPMENT_RULES.md             #   Iron rules: dev → deploy order, no shortcuts
├── PATCHES.md                       #   Framework patches (re-apply after Hermes upgrades)
└── .gitignore                       #   Excludes accounts/default.json + deploy scripts
```

## 🚀 Quick Start

### Prerequisites

- Python **3.10+**
- A running [WPP vendor](https://github.com/jiexiaoyin/wpp-openclaw) instance (HTTP API + WebSocket)
- MariaDB or MySQL (for message persistence + contact cache)
- Aliyun OSS bucket (optional, for media archive)
- SiliconFlow API key (optional, for STT)

### Install

```bash
# Clone the repo
git clone https://github.com/jiexiaoyin/wpp-hermes.git
cd wpp-hermes

# Install Python deps (pymysql + aiohttp are typical)
pip install pymysql aiohttp requests PyYAML
```

### Configure

Copy the example account config and fill in your credentials:

```bash
cp plugin/accounts/boss2.example.json plugin/accounts/default.json
# Edit default.json — fill authcode / tokenKey from your WPP vendor, or set them via env vars
```

Required env vars (recommended over hardcoding):

```bash
export WECHATPRO_AUTHCODE="your_vendor_authcode"
export WECHATPRO_TOKEN_KEY="your_vendor_token_key"
export WECHATPRO_ALLOWED_USERS="wxid_boss_demo,wxid_user_a_demo"
export WECHATPRO_DB_PASSWORD="your_mariadb_password"
export WECHATPRO_LLM_API_KEY="sk-..."        # MiniMax / DeepSeek for heartflow/affection/jargon
export WECHATPRO_STT_API_KEY="sk-..."        # SiliconFlow for voice transcription
export WECHATPRO_S3_ACCESS_KEY="..."         # Aliyun OSS
export WECHATPRO_S3_SECRET_KEY="..."
```

### Deploy into Hermes

```bash
# Copy plugin → Hermes plugin directory
cp -r plugin/ /root/.hermes/plugins/wechatpadpro/

# Copy skills → Hermes skills directory
cp -r skills/wpp-history skills/wpp-identity skills/wpp-friendcircle \
      skills/wpp-friendcircle-stats skills/wpp-friendcircle-view \
      skills/wpp_stt skills/oss_archive /root/.hermes/skills/

# Restart Hermes gateway
hermes gateway restart
```

The adapter will auto-register on gateway startup. Check logs for:

```
[WPP] 平台已注册 / 工具注册 / wpp_stt / oss-archive
```

## 🧪 Testing

```bash
# Unit tests for multi-account routing
python3 plugin/tests/run_tests.py

# Syntax check (all .py files)
python3 -m py_compile plugin/*.py
```

## 🏗 Architecture

```
┌─────────────┐    WSS /ws/sync    ┌──────────────┐    inbound     ┌─────────────┐
│ WPP vendor  │◄──────────────────►│  adapter.py  │───────────────►│   Hermes    │
│ (wx.juhe.chat)│   webhook fallback│  (Python)    │                │   agent     │
└─────────────┘                    │              │   outbound     │             │
       ▲                           │              │◄───────────────│             │
       │ HTTP API                  └──────────────┘                └─────────────┘
       │                           ┌──────────────┐
       └──── /api/Msg/SendTxt ─────│ api_client.py│
                                   └──────────────┘
                                          │
                                  ┌───────┴───────┐
                                  ▼               ▼
                          ┌─────────────┐ ┌─────────────┐
                          │   MariaDB   │ │ Aliyun OSS  │
                          │ (contacts,  │ │ (media      │
                          │  messages)  │ │  archive)   │
                          └─────────────┘ └─────────────┘
```

## 📚 Documentation

- **[DEVELOPMENT_RULES.md](DEVELOPMENT_RULES.md)** — Iron rules: dev → deploy order, no shortcuts, deploy.sh is the only sanctioned deployment path
- **[PATCHES.md](PATCHES.md)** — Hermes framework patches (re-apply after Hermes upgrades)
- **[plugin/](plugin/)** — Inline docstrings in every .py module
- **[skills/*/SKILL.md](skills/)** — Per-skill usage documentation

## 🤝 Contributing

Issues and PRs welcome at [github.com/jiexiaoyin/wpp-hermes/issues](https://github.com/jiexiaoyin/wpp-hermes/issues).

Before opening a PR:
1. Read [DEVELOPMENT_RULES.md](DEVELOPMENT_RULES.md)
2. Run `python3 -m py_compile plugin/*.py` to verify syntax
3. Run `python3 plugin/tests/run_tests.py` to verify multi-account routing
4. Update the relevant SKILL.md if your change affects a skill's contract

## 📄 License

[MIT](LICENSE) © 2026 jiexiaoyin

## 🔗 Related projects

- **[wpp-openclaw](https://github.com/jiexiaoyin/wpp-openclaw)** — TypeScript OpenClaw implementation (the origin of this project, still actively maintained for the OpenClaw platform)
- **[Hermes Agent](https://github.com/jiexiaoyin)** — The agent framework this plugin targets

---

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