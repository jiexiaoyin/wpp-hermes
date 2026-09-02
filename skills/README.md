# WPP Skills (老板 2026-09-01 立 — 解决"找接口痛苦")

## 🎯 这是什么

为 wpp-wechat agent / main agent 提供**两份持久化文档**,
避免每次找 vendor 接口都重新猜/查 openapi/swagger/源码。

## 📚 文件清单

### 1. `wpp_tools_map.json` (61KB)
- **vendor API 工具清单** (221 个工具, 35 个域)
- **群映射表** (8 个群含 nickname / member_count / owner_id)
- **来源**: 自动从 `tools_data.py` + `tools_data_extra.py` + vendor `/Group/List` 生成
- **谁该读**:
  - main agent (找接口时, 直接 read_file)
  - wpp-wechat agent (运行时也能调 `wpp_tools_list` 工具查)

### 2. `wpp_chatrooms_map.json` (3KB) — 在 wpp_tools_map.json 里已合并
- 仅 chatroom 映射 (vendor `/Group/List` 同步)
- 老板手工校对用, 可独立编辑

## 🚀 老板使用 SOP

### 场景 1: 我(老板) 想确认某个群 ID / 群昵称
```
直接读 /root/.hermes/profiles/wpp-wechat/skills/wpp_tools_map.json
看 chatrooms 字段 (8 个群, key 是 ID, value 包含 name/member_count)
```

### 场景 2: 我想加新群映射 (手工)
```bash
# 1. 编辑 chatrooms 字段
vim /root/.hermes/profiles/wpp-wechat/skills/wpp_chatrooms_map.json

# 2. 也写入 DB (可选)
mysql wechatpro -e "
INSERT INTO wpp_chatrooms (chatroom_id, nickname, member_count, source)
VALUES ('xxx@chatroom', '群名', N, 'manual 2026-09-01')
ON DUPLICATE KEY UPDATE nickname='群名';
"
```

### 场景 3: vendor 加了新接口, 我要更新
```bash
# 1. 编辑 /root/dev/wpp-hermes/plugin/tools_data.py
# 2. 重生成 wpp_tools_map.json:
python3 -c "
import sys
sys.path.insert(0, '/root/dev/wpp-hermes/plugin')
# 用 tools_data.py + tools_data_extra.py 自动解析
"
# 3. 同步到 wpp-wechat profile:
cp /root/dev/wpp-hermes/skills/wpp_tools_map.json \
   /root/.hermes/profiles/wpp-wechat/skills/
# 4. 重启 gateway
hermes gateway restart
```

## 🤖 main agent 使用 SOP (铁律!)

### ✅ 应该做的:
- 找 wpp 接口时, **第一动作** = `read_file /root/.hermes/profiles/wpp-wechat/skills/wpp_tools_map.json`
- 找群昵称时, 同上文件查 `chatrooms` 字段
- 验证接口存在性: grep `endpoint` 字段

### ❌ 不应该做的 (老板9/1 立铁律):
- ❌ 重新猜 vendor 接口名 (`GetGroupList` / `GroupList` 都试了 30 分钟 = 浪费时间)
- ❌ 重新读 `tools_data.py` 源文件 — 应直接读 JSON SSOT
- ❌ vendor openapi (`/data/wechatpad-openapi.json` 316 路径) — 太大, 用 wpp_tools_map.json 已够
- ❌ 实时调 `wpp_tools_list` 工具 — 那是 wpp-wechat agent 的能力

## 🎯 老板 9/1 拍板总结

> **"这个相关的接口工具, 你为什么没部署进来? 你每次找接口都这么痛苦!"**

### 3 个响应:
1. **已部署** (MD5 一致证明): `tools_data.py` 含 `wpp_group_list` (vendor `/Group/List`)
2. **我调不到的原因**: main agent 没 wpp 工具集 (越权铁律)
3. **永久解决** (本次):
   - 静态 JSON: `/root/.hermes/profiles/wpp-wechat/skills/wpp_tools_map.json`
   - 运行时工具: `wpp_tools_list` (部署到 `wpp_extras_tools.py`)
   - README: 本文件 (SOP)

## 📅 维护记录

| 日期 | 操作 | 谁 |
|---|---|---|
| 2026-09-01 17:48 | 创建 wpp_tools_map.json + wpp_chatrooms_map.json | main |
| 2026-09-01 17:48 | 同步到 /root/dev/wpp-hermes/skills/ | main |
| 2026-09-01 17:48 | 加 wpp_tools_list 工具 + deploy + gateway restart | main |
| 2026-09-01 17:48 | 写 README.md (本文件) | main |

## 🛠️ 自动重新生成脚本 (TODO)

未来可加 cron 任务: 每周日 02:00 自动从 `tools_data.py` 重生成 wpp_tools_map.json,
保证 SSOT 永远同步。老板拍板后可加。

---
*老板 2026-09-01 拍板立. 任何疑问找 main agent 读这份 README.*