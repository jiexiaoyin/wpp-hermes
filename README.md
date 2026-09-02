# wpp-hermes

**微信 WeChatPadPro (WPP) 协议的 Hermes Agent 平台适配器 — Python 3 实现**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-1.0.0-green)](https://github.com/jiexiaoyin/wpp-hermes/releases)
[![GitHub stars](https://img.shields.io/github/stars/jiexiaoyin/wpp-hermes?style=social)](https://github.com/jiexiaoyin/wpp-hermes/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/jiexiaoyin/wpp-hermes?style=social)](https://github.com/jiexiaoyin/wpp-hermes/network)
[![Open Issues](https://img.shields.io/github/issues/jiexiaoyin/wpp-hermes)](https://github.com/jiexiaoyin/wpp-hermes/issues)
[![Last commit](https://img.shields.io/github/last-commit/jiexiaoyin/wpp-hermes)](https://github.com/jiexiaoyin/wpp-hermes/commits/main)

> 在 WPP vendor (`wss://.../ws/sync` 推送 + `POST /api/Msg/SendTxt` 发送) 和 Hermes agent 之间转发微信消息。
> 从 [wpp-openclaw](https://github.com/jiexiaoyin/wpp-openclaw) **v1.3.80**(TypeScript)移植而来,改为 Hermes 原生 Python 适配器。

---

## 🔗 相关项目

本项目是 WeChatPadPro 跨平台适配插件生态的一部分:

| 项目 | 平台 | 语言 | 状态 |
|---|---|---|---|
| **[wpp-openclaw](https://github.com/jiexiaoyin/wpp-openclaw)** | OpenClaw | TypeScript | ✅ 活跃维护 |
| **[wpp-hermes](https://github.com/jiexiaoyin/wpp-hermes)** (本仓库) | Hermes Agent | Python 3 | ✅ 活跃维护 |
| **[astrbot-plugin-wpp](https://github.com/jiexiaoyin/astrbot-plugin-wpp)** | AstrBot | Python 3 | ✅ 活跃维护 |

三个仓库覆盖同一 vendor API,根据 agent 平台选择:

- 想用 **OpenClaw gateway** 的 LLM → [wpp-openclaw](https://github.com/jiexiaoyin/wpp-openclaw)
- 想用 **Hermes Agent** → 本仓库 (`wpp-hermes`)
- 想用 **AstrBot** 的多平台架构 → [astrbot-plugin-wpp](https://github.com/jiexiaoyin/astrbot-plugin-wpp)

---

## ✨ 功能特性

- **247+ 工具**,覆盖 WPP vendor 完整 API(`/Msg`、`/Friend`、`/Group`、`/FriendCircle`、`/Label`、`/Finder`、`/TenPay`、`/Wxapp`、`/Xiaowei` 等 20+ 域)
- **多账号 B 方案** — 每个账号独立 authcode,绑定专属 Hermes profile,杜绝跨账号串号
- **WebSocket 实时通道** (`wss://.../ws/sync`) + 指数退避重连 + Webhook 兜底(老 vendor)
- **5 大 AI 增强模块**:
  - `heartflow` — 主动参与群聊(5 维打分 + 独立触发)
  - `jargon` — 群黑话挖掘(后台 LLM 提取 + 查询工具)
  - `affection` — 互动打分 + 情绪状态注入
  - `intent-embed` — Embedding 快路径上下文注入
  - `intent-llm` — LLM 智能候选筛选
- **媒体处理全链路**: SILK 语音编解码、STT 语音转写(SiliconFlow)、OSS 归档(阿里云)、缩略图富化
- **7 个独立 Hermes skills**(可复用 SKILL.md 模块): `wpp-history`、`wpp-identity`、`wpp-friendcircle`、`wpp-friendcircle-stats`、`wpp-friendcircle-view`、`wpp_stt`、`oss_archive`
- 引用回复、文件确定性回复、群@触发、群消息防抖

## 📦 仓库内容

```
wpp-hermes/
├── plugin/                          # 📦 Hermes 平台适配器(31 个 .py,~8.6k 行)
│   ├── adapter.py                   #   WS 入站 + 发送出站 + 账号注册表
│   ├── tools.py / tools_data*.py    #   工具注册(247+ 工具,20+ 域)
│   ├── api_client.py                #   WPP vendor HTTP API 客户端
│   ├── heartflow.py / affection.py  #   AI 增强模块
│   ├── intent_embed.py / intent_llm.py
│   ├── message_parser.py / media.py / silk.py / stt.py
│   ├── phoneerp_tools.py            #   PhoneERP 业务集成
│   ├── wecom_tools.py               #   企业微信客户管理
│   ├── accounts/boss2.example.json  #   多账号示例配置(无敏感数据)
│   ├── tests/                       #   多账号测试
│   └── plugin.yaml                  #   Hermes 平台清单
├── skills/                          # 🧩 7 个独立 SKILL.md 模块
│   ├── wpp-history/                 #   消息历史 SQL 模板
│   ├── wpp-identity/                #   wxid ↔ 昵称匹配(多源)
│   ├── wpp-friendcircle/            #   朋友圈发布(文字/图片/视频)
│   ├── wpp-friendcircle-stats/      #   朋友圈统计(频率/类型/时段)
│   ├── wpp-friendcircle-view/       #   朋友圈查看与总结
│   ├── wpp_stt/                     #   语音转文字
│   ├── oss_archive/                 #   阿里云 OSS 上传
│   └── wpp_tools_map.json           #   61KB 工具索引 SSOT(221 工具 + 8 群映射)
├── README.md
├── DEVELOPMENT_RULES.md             #   铁律:dev → deploy 顺序
├── PATCHES.md                       #   Hermes framework 补丁(Hermes 升级后需重打)
└── .gitignore                       #   排除 accounts/default.json + 部署脚本
```

## 🚀 快速开始

### 前置依赖

- Python **3.10+**
- 一个跑着的 WPP vendor 实例(HTTP API + WebSocket)
- MariaDB 或 MySQL(消息持久化 + 通讯录缓存)
- 阿里云 OSS 桶(可选,媒体归档)
- SiliconFlow API Key(可选,STT)

### 安装

```bash
# 克隆仓库
git clone https://github.com/jiexiaoyin/wpp-hermes.git
cd wpp-hermes

# 安装 Python 依赖
pip install pymysql aiohttp requests PyYAML
```

### 配置

复制示例账号配置并填入凭证:

```bash
cp plugin/accounts/boss2.example.json plugin/accounts/default.json
# 编辑 default.json — 填 authcode/tokenKey(从你的 WPP vendor 获取),或通过环境变量注入
```

推荐使用环境变量(不要硬编码):

```bash
export WECHATPRO_AUTHCODE="你的 vendor authcode"
export WECHATPRO_TOKEN_KEY="你的 vendor token_key"
export WECHATPRO_ALLOWED_USERS="wxid_boss_demo,wxid_user_a_demo"
export WECHATPRO_DB_PASSWORD="你的 MariaDB 密码"
export WECHATPRO_LLM_API_KEY="sk-..."        # MiniMax / DeepSeek(心流/好感度/黑话)
export WECHATPRO_STT_API_KEY="sk-..."        # SiliconFlow(语音转写)
export WECHATPRO_S3_ACCESS_KEY="..."         # 阿里云 OSS
export WECHATPRO_S3_SECRET_KEY="..."
```

### 部署到 Hermes

```bash
# 拷贝 plugin 到 Hermes 插件目录
cp -r plugin/ /root/.hermes/plugins/wechatpadpro/

# 拷贝 skills 到 Hermes skills 目录
cp -r skills/wpp-history skills/wpp-identity skills/wpp-friendcircle \
      skills/wpp-friendcircle-stats skills/wpp-friendcircle-view \
      skills/wpp_stt skills/oss_archive /root/.hermes/skills/

# 重启 Hermes gateway
hermes gateway restart
```

适配器会在 gateway 启动时自动注册,检查日志看到下面即成功:

```
[WPP] 平台已注册 / 工具注册 / wpp_stt / oss-archive
```

## 🧪 测试

```bash
# 多账号路由单元测试
python3 plugin/tests/run_tests.py

# 语法检查(全部 .py)
python3 -m py_compile plugin/*.py
```

## 🏗 架构

```
┌─────────────┐    WSS /ws/sync    ┌──────────────┐    入站       ┌─────────────┐
│ WPP vendor  │◄──────────────────►│  adapter.py  │───────────────►│   Hermes    │
│ (wx.juhe.chat)│   webhook 兜底    │  (Python)    │                │   agent     │
└─────────────┘                    │              │   出站       │             │
       ▲                           │              │◄───────────────│             │
       │ HTTP API                  └──────────────┘                └─────────────┘
       │                           ┌──────────────┐
       └──── /api/Msg/SendTxt ─────│ api_client.py│
                                   └──────────────┘
                                          │
                                  ┌───────┴───────┐
                                  ▼               ▼
                          ┌─────────────┐ ┌─────────────┐
                          │   MariaDB   │ │ 阿里云 OSS  │
                          │ (contacts,  │ │ (媒体       │
                          │  messages)  │ │  归档)     │
                          └─────────────┘ └─────────────┘
```

## 📚 文档

- **[DEVELOPMENT_RULES.md](DEVELOPMENT_RULES.md)** — 铁律:dev → deploy 顺序,严禁捷径,deploy.sh 是唯一规范部署路径
- **[PATCHES.md](PATCHES.md)** — Hermes framework 补丁(Hermes 升级后需重打)
- **[plugin/](plugin/)** — 每个 .py 模块都有详细的 docstring
- **[skills/*/SKILL.md](skills/)** — 每个 skill 的使用文档

## 🤝 贡献

欢迎在 [github.com/jiexiaoyin/wpp-hermes/issues](https://github.com/jiexiaoyin/wpp-hermes/issues) 提 Issue 或 PR。

提 PR 之前:
1. 读 [DEVELOPMENT_RULES.md](DEVELOPMENT_RULES.md)
2. 跑 `python3 -m py_compile plugin/*.py` 验证语法
3. 跑 `python3 plugin/tests/run_tests.py` 验证多账号路由
4. 如果改动影响 skill 契约,更新对应 SKILL.md

## 📄 许可证

[MIT](LICENSE) © 2026 jiexiaoyin

## 🔗 相关项目

- **[wpp-openclaw](https://github.com/jiexiaoyin/wpp-openclaw)** — TypeScript OpenClaw 实现(本项目的源,OpenClaw 平台仍在维护)
- **[Hermes Agent](https://github.com/jiexiaoyin)** — 本插件目标框架

---

# wpp-hermes 开发目录

> **微信插件(wechatpadpro)+ wpp skills 统一开发版**。开发只在此目录进行,改完后一键部署到 Hermes。

## 📁 目录结构

```
/root/dev/wpp-hermes/
├── plugin/                    # 📦 插件源码(wechatpadpro,35 个 .py)
│   ├── adapter.py             #   平台适配器(入站/出站/多账号)
│   ├── tools.py               #   工具注册(225+ 工具)
│   ├── tools_data.py          #   通用工具定义
│   ├── tools_data_extra.py    #   扩展工具定义
│   ├── api_client.py          #   vendor API 客户端
│   ├── triggers.py            #   触发判断(@机器人/接龙/心流)
│   ├── ...(其余 .py)...
│   ├── accounts/default.json  #   ⚠️ 含 authcode,勿提交 GitHub
│   ├── tests/                 #   测试
│   └── plugin.yaml
├── skills/                    # 🧩 wpp skills(7 个,2026-09-01 纳入)
│   ├── wpp-history/           #   消息历史查询
│   ├── wpp-identity/          #   昵称↔wxid 匹配
│   ├── wpp-friendcircle/      #   朋友圈发布
│   ├── wpp-friendcircle-stats/#   朋友圈统计
│   ├── wpp-friendcircle-view/ #   查看朋友圈
│   ├── wpp_stt/               #   语音转写(python 模块,插件依赖)
│   └── oss_archive/           #   OSS 归档(python 模块,插件依赖)
├── deploy.sh                  # 🚀 一键部署
├── README.md
└── .gitignore
```

## 🚀 快速部署(核心)

```bash
# 开发:直接编辑本目录(plugin/ 或 skills/)
vim /root/dev/wpp-hermes/plugin/tools.py
vim /root/dev/wpp-hermes/skills/wpp-history/SKILL.md

# 部署:一条命令(自动完成 6 步)
bash /root/dev/wpp-hermes/deploy.sh

# 只检查不部署(语法 + 差异预览)
bash /root/dev/wpp-hermes/deploy.sh --check

# 回滚到某次部署前的备份
bash /root/dev/wpp-hermes/deploy.sh --rollback /data/wpp-deploy-backup/wechatpadpro-20260901-103000
```

## 📋 部署流程(deploy.sh 自动执行)

| 步骤 | 动作 | 说明 |
|------|------|------|
| 1 | 语法检查 | plugin + skills 全部 .py,失败中止 |
| 2 | 备份 | plugin + 7 skills → `/data/wpp-deploy-backup/wechatpadpro-<时间戳>/` |
| 3 | 同步 plugin | → `/root/.hermes/plugins/wechatpadpro/` |
| 4 | 同步 skills | 逐个 → `/root/.hermes/skills/<同名>/`(不影响其他 skills) |
| 5 | 重启 gateway | `hermes gateway restart`(新代码生效) |
| 6 | 验证 | 检查日志 `[WPP] 平台已注册/工具注册/wpp_stt/oss-archive` |