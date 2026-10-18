# CHANGELOG

本文件记录 Python 侧（本仓）的版本。TS 版历史在 `jiexiaoyin/wpp-openclaw` 的 CHANGELOG。

版本号与 `plugin/plugin.yaml` 的 `version` 保持一致；打 tag 用 `v<version>`。

---

## v2.0.0 — 2026-10-18

Hermes Python 侧重写完成，接管生产（profile `wpp-wechat`，单账号 `default`）。

### 能力

- **平台适配器**：WS + webhook 双通道入站；`autoSetWebhook` 三段式注册 + 周期刷新；
  媒体下载/上传（图片/视频/语音/文件，200MB 上限）；文本分片（>2000 字自动切）。
- **工具面 299 个**：厂商端点（由蓝本生成，`--check` 幂等）+ 注册期裁决层（撤注册/纠错/补注册）
  + 运行期门禁（小微 / 朋友圈发布 / 管理员）。
- **心流**：门槛判定、预算、画像、复读去重、静默段、learn 阈值、台账落库；热路径零 DB。
- **周边**：意图（LLM + embedding）、黑话挖掘、好感度、会话标题、语音转写、OSS 归档。
- **安全**：出站闸收口 `_raw_url`、SSRF 二次校验、本地读白名单排除凭据目录、
  webhook 双因素 fail-closed、日志全账号 token 打码。
- **测试**：2467 用例 / 0 失败 / 10 跳过（约 19s）；变异测试与结构守卫工具链齐备。

### 本次审计后的处置（同日）

- 蓝本源表从外部旧树迁入 `tools/vendor_source/`，生成器输入回到本仓（`--check` 往返等价）。
- 116 条工具描述重写：先补资金/群管理/好友/朋友圈等高危高频组，平均长度 29 → 35.2 字；
  低频运维类保持短描述以控制 tool_search 索引成本。
- `plugin.yaml` 补 `provides_hooks: [pre_llm_call]`，doctor 不再报「hook not listed」。
- `wpp_messages.new_msg_id` 定义漂移修复：5264 行 NULL 回填为 `#<id>` 占位后改为
  `NOT NULL DEFAULT ''`（唯一键 `uk_account_msg` 恢复对 NULL 的去重能力）。
- 旧树 `/root/dev/wpp-hermes` 归档到 `/root/dev/_archive/wpp-hermes-legacy-20261018`。
- 新增 `tools/fix_new_msg_id_drift.py`（人肉执行）与 `tools/diag_new_msg_id_dups.py`（只读诊断）。
- 群聊触发规则定稿：**被 @ 必回**（`group_mention` 直通 agent），**其余消息交心流 judge**
  （`heartflow_candidate`）。据此移除 `requireAtMention` 死键（账号模型/适配器/示例配置/
  死键告警一并删除）——它不是规则载体，留着只会让人以为"配了只回 @"。要收窄出声范围
  仍用 `groupPolicy` / `blacklistGroups` / 心流预算与阈值。

### 已知未做

- `platform/adapter.py` 仍是单文件 2855 行（媒体/入站/出站可继续拆 sibling）。
- 无 lint/type CI（ruff 配置待接入）。
