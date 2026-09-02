# 开发规范铁律 (老板 2026-09-01 拍板)

## 🚨 铁律 0: dev → deploy 顺序 (严禁搞反)

**正确顺序**:
1. ✏️  修改 `/root/dev/wpp-hermes/plugin/*.py` (开发版)
2. ✅  `python3 -m py_compile` 语法校验
3. ✅  跑 `bash /root/dev/wpp-hermes/deploy.sh` (或手动 cp)
4. 🚀  `hermes gateway restart` 让 plugin 重新加载
5. ✔️  md5sum 验证 dev == deploy

**错误顺序 (禁止!)**:
- ❌ 直接编辑 `/root/.hermes/plugins/wechatpadpro/*.py`
- ❌ 从 deploy 目录 git commit
- ❌ 在 deploy 改完不回到 dev

## 🎯 为什么

| 风险 | 说明 |
|---|---|
| 丢失改动 | dev 是 SSOT, deploy 是产物. 直接改 deploy → 重部署被覆盖 |
| git 污染 | deploy 目录没有 git 或 git 配置不一致 |
| 重装回滚困难 | deploy 改完没 dev 备份, 升级/迁移会丢 |
| 流程混乱 | 多人协作时 deploy 各自改 → 合并冲突 |

## 🛡️ 自动化防护

### 部署脚本 (deploy.sh 已实现)
```bash
# 语法检查 → 备份 → rsync → restart → 验证
bash /root/dev/wpp-hermes/deploy.sh

# 只检查不部署
bash /root/dev/wpp-hermes/deploy.sh --check

# 回滚
bash /root/dev/wpp-hermes/deploy.sh --rollback /data/wpp-deploy-backup/<ts>
```

### 手动校验 (任何变更后必须跑)
```bash
md5sum /root/dev/wpp-hermes/plugin/*.py
md5sum /root/.hermes/plugins/wechatpadpro/*.py
# 所有对应文件 MD5 必须一致
```

## 📋 例外情况

| 场景 | 怎么办 |
|---|---|
| 紧急修复 (生产挂) | deploy 可临时改, **但事后必须立即同步到 dev** |
| Config/secrets 调整 | deploy 改 (因为是配置), 但 dev 留 schema 范例 |
| 数据库结构变更 | deploy 跑 DDL, dev 留 migration SQL |

## ⚠️ 历史教训

| 日期 | 教训 |
|---|---|
| 2026-09-01 | 老板训诫: "每次应该在dev中修改，再部署到生产目录！你直接搞反了" |
| 2026-08-12 | (历史) heartflow 配置曾直接改 deploy, 后来同步 dev |

---
*任何疑问找 main agent 读这份铁律.*