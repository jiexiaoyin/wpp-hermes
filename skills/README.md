# skills/ —— 本插件自带的技能（以及**为什么只有 5 个**）

## Hermes 怎么发现它们

`agent_plugins.py:224 _discover_skills(<plugin_root>)` 扫的是 **`skills/<子目录>/SKILL.md`**：

| 情形 | Hermes 的反应 |
|---|---|
| 子目录里**没有** `SKILL.md` | 直接 `continue`，**一行日志都没有** |
| `name` ≠ 目录名 | 丢弃（同上，无声） |
| `name` 不匹配 `_SKILL_NAME_RE`（小写+单连字符） | 丢弃 |
| `description` 为空或 > 1024 | 丢弃 |
| `license`/`compatibility`/`metadata`/`allowed-tools` 形状不对 | 丢弃 |

⇒ **"目录搬过来了" ≠ "技能被采纳了"**。这四处判据全是静默的，所以
`plugin/tests/test_skills.py` 逐条钉住，并由变异套件 `skills` 证明它会红
（4/4 KILLED，见 `tools/mutation_check.py`）。

## 部署到哪：`~/.hermes/skills/`，不是插件目录里

插件自带的技能**不会**进 agent 的 `<available_skills>` —— 它由
`agent/prompt_builder.py` 从**本地技能目录**（`~/.hermes/skills`）、
`skills.external_dirs`（config）和项目目录拼出来，插件 bundle 里的那一份不在其中。
（`PluginContext.register_skill()` 注册是只读、带命名空间的：`skills_list` 能列、
`skill_view()` 能取，**但不会进系统提示的技能索引**。）

所以 `deploy.sh` 的落位目标是 `.183:~/.hermes/skills/<name>/` —— 与旧的 v1 部署一致。

## 目录清单（5 个）

| 技能 | 干什么 |
|---|---|
| `wpp-friendcircle` | 发朋友圈（文字/图文/视频 3 条路径 + 发布铁律） |
| `wpp-friendcircle-stats` | 朋友圈数据统计（发布频率/内容类型/活跃周期） |
| `wpp-friendcircle-view` | 看某人朋友圈并总结 |
| `wpp-history` | `wpp_messages` 表的历史消息查询 |
| `wpp-identity` | 昵称 ↔ wxid 匹配（多源，脱敏判断前置） |

正文里出现的 `` `wpp_*` `` 记号必须能落到**实注册的工具名**或 **`schema.sql` 的表名**
上 —— 技能写错名字照样"加载成功"，错到模型照抄那一刻才炸，所以这条也在门禁里。

## 旧树里还有两个"技能"，为什么这里没有

旧树 `plugin/skills/` 下另外两个目录**不是技能**（没有 `SKILL.md`），是两包 Python 能力：

| 旧目录 | 结论 | 理由 |
|---|---|---|
| `oss_archive/` | **N/A** | 那套代码解决的是"OpenClaw 的入站媒体只有 CDN 凭据、需要自己下载再传对象存储"。Hermes 这条链路不同：入站媒体由适配器落**本地缓存路径**（`plugin/inbound/media.py`），出站由 `outbound.resolve_to_base64` 直接读文件转 base64 交给 vendor —— **整条路不需要对象存储**。照搬过来只会多一份无人调用的上传逻辑，还多一组必须维护的凭据。 |
| `wpp_stt/` | **并入主链路**，不做独立技能 | 语音转写由 `plugin/inbound/media.py` + `prepare_text` 承担（见任务 #17）：优先用 vendor 自带的转写结果，缺了才降级走 SiliconFlow STT。它是**入站解析的一个环节**，不是模型按需调用的技能。 |

> ⇒ `plugin/plugin.yaml` 的 `optional_env` 里那两条 `WECHATPRO_S3_*` 是**没人读的声明**
> （声明了不用 = 骗正在配环境的人），要删；`WECHATPRO_STT_API_KEY` 反过来 —— 声明在、
> 读它的代码还没接上（语音转写那一步接）。
