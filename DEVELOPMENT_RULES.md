# 开发规范铁律（老板 2026-09-01 拍板；2026-10-01 本树生效版）

> **本树 = `/root/dev/wpp-hermes-py2/`**，是 wpp-hermes Python 侧的**唯一事实来源（SSOT）**。
> 旧树 `/root/dev/wpp-hermes/` 已于 2026-10-18 **归档**到
> `/root/dev/_archive/wpp-hermes-legacy-20261018/`：既是历史实现，也是当初的端点蓝本源。
> 它的蓝本职责已由本仓 `tools/vendor_source/` 接管（复制时 SHA-256 逐文件一致），
> 归档树只作历史追溯，**不再被任何主链路读取**（`tools/port_tools_domain.py` 是唯一引用者，
> 且它是一次性移植工具）。

## 🚨 铁律 0：dev → deploy 顺序（严禁搞反）

**正确顺序**：
1. ✏️  改 `/root/dev/wpp-hermes-py2/plugin/**`（开发版）
2. ✅  语法校验：`python3 -m py_compile`（部署脚本会做，用**目标机的 python** 复检）
3. ✅  跑 `bash /root/dev/wpp-hermes-py2/sync.sh`（备份 → 生成物校验 → 注册对账 →
   结构守卫 → rsync → 目标机语法复检 → md5 逐位；**不重启**）
   或 `bash deploy.sh`（全流程，含重启 —— 只能在**人**的外部 shell 里跑）
4. 🚀  `hermes gateway restart` 让插件重新加载（在网关进程外执行）
5. ✔️  md5sum 验证 dev == deploy（**逐文件**，不是抽样）

> **这条对 agent 同样成立**：任何一次改动插件行为的动作（改代码、改 `plugin.yaml`、
> 改生成物蓝本、加工具描述）都必须**先落在 dev 树**，再由 `sync.sh` 同步过去。
> 生产目录 `/root/.hermes/plugins/wechatpadpro` 是**只读产物**，不允许直接编辑 ——
> 直接改它会在下次同步时被覆盖，并且破坏「代码从哪来」的可追溯性。

**错误顺序（禁止）**：
- ❌ 直接编辑 `.183:/root/.hermes/plugins/wechatpadpro/*.py`
- ❌ 从 deploy 目录 git commit
- ❌ 在 deploy 改完不回到 dev

## 🎯 为什么

| 风险 | 说明 |
|---|---|
| 丢失改动 | dev 是 SSOT，deploy 是**确定性产物**；直接改 deploy → 下次部署被覆盖 |
| git 污染 | deploy 目录没有 git 或配置不一致 |
| 重装回滚困难 | deploy 改完没 dev 备份，升级/迁移会丢 |
| 流程混乱 | 多人协作时各自改 deploy → 合并冲突 |

## 🛡️ 自动化防护

```bash
bash /root/dev/wpp-hermes-py2/deploy.sh              # 语法检查 → 备份 → 部署 → md5 校验
bash /root/dev/wpp-hermes-py2/deploy.sh --check      # 只检查不部署
bash /root/dev/wpp-hermes-py2/deploy.sh --rollback <备份目录>
```

**备份目录**：按目标机选择 ——

- 本机生产（`.182`，有 `/data`）：`/data/wpp-deploy-backup/<YYYYmmdd-HHMMSS>/`
- 远程 `.183`（**没有 `/data`**）：`/root/.hermes/backups/wpp-deploy-<epoch>/`

只备份 **deploy 侧**（dev 是 SSOT，不需要备份自己的产物）。

> ⚠️ `hermes gateway restart` **不能在 gateway 进程内部执行**（安全策略会拦，且会先杀掉
> 发起命令的子进程）。部署脚本同步完代码后，重启这一步要么由人在外部 shell 跑，
> 要么脚本自己以 detached 方式在独立会话里跑。当前约定：**agent 只做同步 + 校验，
> 重启交给人**。

### 手动校验（任何变更后必须跑）

```bash
md5sum /root/dev/wpp-hermes-py2/plugin/**/*.py
ssh root@172.20.109.183 'md5sum /root/.hermes/plugins/wechatpadpro/**/*.py'
# 所有对应文件 MD5 必须一致
```

⚠️ 脚本里 `ssh`/`bash` 会**吞掉调用方 `while read` 的 stdin** ⇒ md5 只校验到第一个文件就停。
凡在 `while read` 循环里调外部命令，必须 `< /dev/null`（2026-10-01 踩过）。

## 📋 例外情况

| 场景 | 怎么办 |
|---|---|
| 紧急修复（生产挂） | deploy 可临时改，**但事后必须立即同步回 dev** |
| Config / secrets 调整 | deploy 改（因为是配置），但 dev 留 **schema 范例**（`*.example.json`） |
| 数据库结构变更 | 改 `plugin/data/schema.sql`（**唯一 DDL 真源**）后跑 `migrate()`；**不要手写第二份 DDL** |

## 🔴 红线（违反即可能伤生产）

1. **生产密钥绝不可用于调试**。钦定不可动的 3 条在用生产密钥：
   `76db098c…`（super admin）／`a0da3904…`（接晓银 iPad）／`71bed0f5…`（益融）。
2. **`selfWxid = wxid_eezdbu1ytws422` 是生产 bot「益融小助理」**
   —— 任何以它为身份的 `.183` 账号**绝不能用来连接厂商**（客户端实例锁会把生产踢下线
   + webhook 被劫持）。测试代码里断言 `selfWxid != wxid_eezdbu1ytws422`。
3. **微信是生产环境，消息发出去撤不回**。调试只用自建测试群 + 测试账号；
   `outbound.send` 在白名单外直接 raise。
4. **DB 只连本机**：`WECHATPRO_DB_HOST` 只允许 `127.0.0.1` / `localhost` / `::1`
   （要远程必须显式 `WECHATPRO_DB_ALLOW_REMOTE=1`）。测试只跑 `wechatpro_test` 库。
5. **含 authcode 的文件禁止提交**（见 `.gitignore`：`accounts/*.json` 仅放行 `*.example.json`）。

## ⚠️ 历史教训

| 日期 | 教训 |
|---|---|
| 2026-09-01 | 老板训诫：「每次应该在 dev 中修改，再部署到生产目录！你直接搞反了」 |
| 2026-08-12 | heartflow 配置曾直接改 deploy，后来同步 dev |
| 2026-10-01 | 旧树根因复盘：插件能加载但平台**永不启用**（缺 `env_enablement_fn` + `_is_connected` 死锁）；`wecom_tools.py` **模块级**抛异常导致 10 个工具静默不注册 |

---
*任何疑问找 main agent 读这份铁律。*
