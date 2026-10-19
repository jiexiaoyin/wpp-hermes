# 统一白名单（收发同一份）— 2026-10-19

> 老板拍板：**白名单不区分进出站**。一个人/群在名单里，机器人既能收他的消息，
> 也能主动发给他；全员群里的公司员工自动成为白名单成员。

## 1. 生效集合

出站保险丝（`netguard.check_outbound_allowed`）与入站准入使用同一份来源：

```
allowFrom          （私聊：收 + 发）
groupAllowFrom     （群：收 + 发）
adminUsers         （管理员：额外保底）
WECHATPRO_SEND_ALLOWLIST （可选的历史额外条目，不再是唯一来源）
```

其中 `allowFrom` 由两部分组成：

```
allowFrom = 手工名单（/user add|del）
          + employeeAllowFrom（全员群自动同步）
```

`employeeAllowFrom` 是自动同步台账，用来在下次同步时**只移除"上次自动加、这次已退群"**的人，手工加的人永不被自动删除。

## 2. 全员群同步

账号 JSON 配置：

```json
"employeeGroupIds": ["44971342037@chatroom"]
```

- 生产当前：`44971342037@chatroom` =「✊🏻益融数码全员群」，36 人（含机器人）。
- 拉取端点：`/Group/GetChatRoomMemberDetail`（只读）。
- 排除：机器人自己（`selfWxid`）、群 id、公众号 `gh_*`、系统号。
- 频率：连接后立即一轮，之后每 6 小时；命令 `/user sync` 可立即触发。
- 写回：`allowFrom` 与 `employeeAllowFrom` **一次原子写**，备份在 `/data/wpp-account-backups/`。
- **失败整轮不写**：任一全员群拉取失败，就保留上一轮名单，绝不因厂商抖动误删员工。

## 3. 命令

| 命令 | 作用 |
|---|---|
| `/user add\|del\|list [wxid...]` | 手工私聊白名单；**改完立刻刷新出站** |
| `/user sync` | 从全员群同步员工（异步，走厂商接口） |
| `/group add\|del\|list [群id...]` | 群白名单；同样收发共用 |
| `/whitelist status` | 统一白名单总览（私聊/群/管理员/全员群/env；`/outbound` 是别名） |
| `/whitelist sync` | 同 `/user sync` |

之前的 `WECHATPRO_SEND_ALLOWLIST` 仍保留为**额外补充**，但加人不再需要改 `.env`。

## 4. 热生效

- `commands.write_back` → `adapter.reload_account` → `refresh_outbound_allowlist`：
  改完 `/user` / `/group` 后，所有已连接 client 的 `send_allowlist` 立即更新。
- 全员群同步写回后走同一个 `reload_account`，所以 inbound 管线与 outbound 闸同时更新。
- 只有 `.env` 本身改动仍需要重连/重启（它只在 connect 时读一次）。

## 5. 验证

```bash
# 单测
/usr/local/bin/pytest -q plugin/tests/test_unified_whitelist.py

# 真实链路（只读拉成员 + 写回）
python3 /tmp/run_employee_sync.py

# 纯判定（不发消息）
python3 - <<'PY'
# unified_allowlist(...) 后 check_outbound_allowed('jsnjzhou', ...) 应放行
PY
```
