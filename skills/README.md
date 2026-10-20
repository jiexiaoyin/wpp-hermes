# skills/ —— 随插件提供的 Hermes 技能

## 技能如何被发现

Hermes 扫描 `skills/<子目录>/SKILL.md`。以下任一条件不满足都会**静默丢弃**：

- 子目录里存在 `SKILL.md`；
- frontmatter 的 `name` 等于目录名；
- `name` 只含小写字母、数字和单个连字符；
- `description` 非空且不超过 1024 字符；
- `license` / `compatibility` / `metadata` / `allowed-tools` 形状正确。

`plugin/tests/test_skills.py` 对这些规则做契约测试。

## 安装位置

Hermes 从本地技能目录读取技能，**不会**把插件包里的 `skills/` 自动加入系统提示。
需要手动复制到技能目录：

```bash
mkdir -p "$HERMES_HOME/skills"
cp -r skills/wpp-setup "$HERMES_HOME/skills/"
```

或复制全部：

```bash
cp -r skills/wpp-* "$HERMES_HOME/skills/"
```

## 目录清单

| 技能 | 用途 |
|---|---|
| `wpp-setup` | 安装与配置引导：检查状态、给出下一步、验证 |
| `wpp-friendcircle` | 发朋友圈（文字 / 图文 / 视频） |
| `wpp-friendcircle-stats` | 朋友圈发布统计 |
| `wpp-friendcircle-view` | 查看并总结某人的朋友圈 |
| `wpp-history` | 查询 `wpp_messages` 历史消息 |
| `wpp-identity` | 昵称 ↔ wxid 匹配（本地联系人） |

技能正文里引用的 `wpp_*` 名字必须对应插件实际注册的工具或数据库表名。
