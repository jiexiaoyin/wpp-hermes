# wpp-hermes Framework 补丁记录

> ⚠️ 以下补丁修改了 **Hermes framework 核心文件**（非 wpp 插件源码）。
> **升级 hermes 后必须重新打补丁**（framework 升级会覆盖）。
> **2026-09-01 更新**: PATCH-1 已升级为**插件侧方案**（adapter.py 实现授权契约），不再需要 authz_mixin 补丁。

## PATCH-1: 群消息 chat-scoped 授权（2026-09-01）→ ✅ 已插件化

**原文件**: `/usr/local/lib/hermes-agent/gateway/authz_mixin.py`（**不再需要补丁**）

**问题**: 插件平台（wechatpadpro）群消息被 "Unauthorized user" 拒绝。
- 根因: wpp adapter 声明 `enforces_own_access_policy=True`，但**缺 framework 期望的授权属性**
  （`_group_policy`/`_group_allow_from`/`_dm_policy`/`_allow_from`/`_groups`）→ framework
  read policy 失败 → 走 user 白名单 fallback → 群内非白名单成员（如接龙发起人）被拒

**插件侧方案（当前生效，升级 hermes 不影响）**:
- 文件: `/root/.hermes/plugins/wechatpadpro/adapter.py`
- 位置: `enforces_own_access_policy` 属性后，新增 `_group_policy`/`_group_allow_from`/`_dm_policy`/`_allow_from`/`_groups` 属性
- 逻辑: 从 default 账号配置（accounts/default.json）读 `groupPolicy`/`groupAllowFrom`/`allowFrom`
  - `_group_policy` = `allowlist`（有 groupAllowFrom）→ framework 群消息 line 683 `return True`
  - **fail-closed 加固**: `allowlist` 但无 groupAllowFrom → 返回 `disabled`（拒绝所有群）
  - `_dm_policy` = `allowlist`（有 allowFrom）→ 私聊按用户白名单
- framework 原生 `enforces_own_access_policy` 分支（authz_mixin.py:642-668）直接信任 wpp intake
  （wpp triggers.py:94-96 已做群白名单）

**验证**: 用 Hermes 插件机制实例化 wpp adapter → `_group_policy=allowlist`、`_group_allow_from count=7`、
`enforces_own_access_policy=True` → 模拟 framework 授权 → 接龙群消息授权 True（无需 PATCH-1）

**备份**: `/data/wpp-skill-refactor-2026-09-01/authz_mixin.py.with-patch1-132417`（原 PATCH-1 版）
`/data/wpp-auth-contract-20260901-132623/`（插件侧方案 + 回退后 authz_mixin）

**回退**: `git -C /usr/local/lib/hermes-agent checkout -- gateway/authz_mixin.py`（已执行，PATCH-1 已移除）

## PATCH-2: memos 云端自动记忆（2026-09-01）

**文件**: `/usr/local/lib/hermes-agent/gateway/run.py`

**问题**: memos-cloud MCP 工具可用，但会话不会自动传入（framework 无自动记忆调用）。
老板要求"会话会传入吗？包括 wecom wpp 等"→ 需要自动写记忆。

**修复**: 
1. `_handle_message` 的 `_run_post_turn_hooks` 调用前，新增 `_run_memos_auto_memory(event, source, agent_result)`：
   - 每轮外部会话(user+assistant)写入 memos-cloud openmem API
   - 覆盖所有 gateway 平台（wecom/wpp 等）
   - 跳过 internal / slash 命令 / 空回复 / 无 key
   - 后台 executor 同步 POST（不阻塞 event loop），失败静默
2. 新增 `_memos_post_sync` 静态方法（同步 POST /add/message）
3. gateway 进程不继承 ~/.hermes/.env → 自行读取 MEMOS_API_KEY/MEMOS_BASE_URL

**位置**: run.py 约 19036（调用点）+ 22779-22873（方法定义），搜 "memos auto-memory"

**备份**: `/data/wpp-skill-refactor-2026-09-01/run.py.before-memos`

**验证**: 5/5 单测（正常会话写 / internal 跳过 / slash 跳过 / 空回复跳过 / 无 key 跳过）+ 重启 13:08:02 生效

## PATCH-3: 移动 IM 平台无人值守 (approval.py)（2026-09-01）

**文件**: `/usr/local/lib/hermes-agent/tools/approval.py`

**问题**: hermes 把 wechatpadpro/wecom 视为普通 chat 平台，dangerous command approval notice 会推到 IM。
老板实测：`node -e "..."` 这种常用脚本触发 `⚠️ **Dangerous command requires approval:**` + `/approve` `/deny` 提示推到微信——CLI 风格 notice 对 IM 用户毫无意义，且 `/approve` 在 IM 客户端根本输不进去。

**根因**:
- `_UNATTENDED_APPROVAL_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "api_server"})` —— 写死的 frozenset，不包含微信/企业微信
- hermes 团队注释自己都写 "Messaging approvals arrive as a push notification the user may not see immediately"（config_defaults.py:2536）但默认 `mode: smart` 还是会推 notice
- IM 平台老板不会时刻盯手机，approval timeout (60-300s) 内大概率没回复 → 死锁 + notice 残留

**修复**: 把 `"wechatpadpro"` / `"wecom"` 加入 `_UNATTENDED_APPROVAL_PLATFORMS`，配合 `approvals.unattended_mode: approve`：
- dangerous command 在微信上不再生成 approval notice，直接 approve 通过，notice 不推 IM
- 老板常用命令（node -e / playwright / curl / npm / pip 等）不会被无意义拦截
- 真要拦截的危险操作（rm -rf / / mkfs / 改 system path）由 guardian LLM + smart_policy 兜底

**位置**: tools/approval.py:275-279，搜 `"wechatpadpro"`

**配套配置** (`~/.hermes/config.yaml`):
```yaml
approvals:
  mode: smart
  unattended_mode: approve
  smart_policy: |
    以下命令/工具是运营者日常合法使用，应当 APPROVE：
    - node -e / python -c / playwright / curl / npm / pip 等常用工具链
    仅当命令满足以下之一才 ESCALATE / DENY：
    - 写系统关键路径 (/etc/ /var/ systemd unit)
    - 改 ~/.hermes/config.yaml / .env / ~/.ssh/ / shell rc
    - rm -rf / / dd to /dev/sd* / mkfs / force push --no-verify
```

**备份**: `/data/wpp-patch-backup/approval.py.YYYYMMDD-HHMMSS`

**验证**: `python3 -c "from tools.approval import _UNATTENDED_APPROVAL_PLATFORMS; print('wechatpadpro' in _UNATTENDED_APPROVAL_PLATFORMS)"` → True

**升级注意**: 升级 hermes 后必须 `bash /root/dev/wpp-hermes/reapply-patches.sh` 重打（PATCH-3 已纳入脚本）
