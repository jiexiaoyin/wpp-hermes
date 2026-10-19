# WPP 扩展边界审计（2026-10-19）

> 目的：回答「OSS / 心流 / 黑话 / 画像 / 情绪路由 / 主动消息 / 防抖，哪些应该成为
> 通道无关的扩展，哪些必须留在 wechatpadpro 通道插件里」。
>
> 方法：对 `plugin/` 下候选模块做 AST 分析，分别统计**模块顶层 import**与
> **函数内懒加载 import**，再结合反向依赖和 WPP 专有标识符（`wxid` / `chatroom` /
> `wpp_*` 表 / `client.send_*`）人工判级。行数为当前工作树实测。

## 0. 结论先行

1. **方向正确，且代码比看起来更接近目标**：11 个候选模块里，只有 `debounce.py`
   和 `media_oss.py` 在顶层 import 了 WPP 模块；其余 9 个的 WPP 依赖全部是函数内
   懒加载。耦合主要不在 import 图，而在**消息数据形状与会话身份**。
2. **不需要大爆炸式重构**。真正通道绑定的代码集中在
   `parser.py` / `triggers.py` / `persist.py` / `toolkit/msg.py` / `platform/adapter.py`
   这几处；把它们之外的东西按 A/B/C 三级逐步上提即可。
3. **上提的前提是统一会话身份**，不是先搬文件。没有 `ConversationKey` 和
   `OutboundSender`，搬出去的模块仍然只能服务 WPP。
4. **心流、触发识别、消息持久化不要整体上提**。它们编码的是微信群语义
   （@、引用 XML、红包、接龙、成员名单、silk 语音），不是通用回复策略。

## 1. 分级定义

- **A：纯逻辑，零 WPP 运行时依赖**。只依赖标准库（第三方 `jieba` 可选），
  入参是字符串/普通 mapping。可以搬进中立包，旧路径留 re-export 即可，风险接近零。
- **B：策略纯、执行不纯**。判定/解析/渲染是纯函数，但模块内某几个函数懒加载
  WPP DB / LLM / 发送客户端。做法是**拆成 policy + runner**，policy 上提，
  runner 留 WPP 并注入依赖。
- **C：通道语义 / 存储 / 发送**。依赖 WPP 消息类型、聊天室、引用 XML、silk、
  WPP 表结构或 API client。现阶段留在 `wechatpadpro`。

## 2. 模块审计表

| 模块 | 行数 | 顶层 WPP import | 懒加载 WPP 依赖 | 级别 | 建议归宿 |
|---|---:|---|---|---|---|
| `inbound/rate_limit.py` | 244 | 无 | 无 | **A** | 直接搬；`TriggerRateLimiter` 只认 `(account, chat, sender)` 三元组 |
| `inbound/jargon.py` | 591 | 无 | 仅第三方 `jieba` | **A** | 整体可搬；统计/查询缓存是纯算法，DB 读取在 `toolkit/jargon_tool.py` |
| `inbound/quote_ref.py` | 219 | 无 | `parser._safe_str` | **A-** | 本地替换 `_safe_str` 后搬；身份解析只吃 mapping |
| `inbound/tts_emotion.py` | 366 | 无 | `data.repo_affection` | **B+** | `MOOD_*` / `resolve_tts_cfg` / `build_voice_style` / `should_auto_voice` / `clean_voice_text` 是 A；`get_chat_mood` 与合成执行留 WPP |
| `inbound/proactive.py` | 343 | 无 | `repo_messages` / `llm.judge` / `persist` / `platform.outbound` | **B** | `resolve_proactive_cfg` / `in_quiet_hours` / `is_eligible` / `build_prompt` / `clean_output` 上提；`maybe_send_once` / `sweep_loop` 留 WPP |
| `inbound/debounce.py` | 377 | `parser` 的常量与 `WppMessage` | 无 | **B** | `merge_text_batch` 去类型化后可上提；`InboundDebouncer` 的到达时序留适配器 |
| `inbound/media_oss.py` | 380 | `core.env_compat.secret_env` | 无 | **A-** | 注入 env 读取与 key 构造后搬；上传/降级逻辑本身与微信无关 |
| `inbound/intent.py` | 826 | 无 | `core.metrics` / `llm.judge` | **B** | 候选筛选、提示词、结果解析是 A；LLM/embedding 调用用回调注入 |
| `inbound/affection.py` | 670 | 无 | `repo_affection` / `llm.judge` | **B** | 分类规则、分数计算、情绪状态机是 A；持久化留 WPP |
| `inbound/style_learn.py` | 842 | 无 | `repo_style` / `core.accounts` / `repo_messages` / `llm.judge` | **B** | `resolve_style_cfg` / 样本净化 / prompt 构造 / 卡片解析渲染是 A；生成 sweep 与缓存留 WPP |
| `inbound/jargon_mine.py` | 659 | 无 | `repo_messages` / `llm.judge` | **B/C** | LLM 流水线可随 `jargon` 一起上提，但落库执行留 WPP，先不动 |
| `data/repo_*.py` | 219–838 | `. db` | `repo_affection` ↔ `affection` 反向依赖 | **C-** | 先定义 `ConversationStore`，不要直接搬表 |
| `inbound/triggers.py` | 385 | `hongbao` / `parser` / `relay` | — | **C** | 微信 @/引用/红包/接龙语义，留 WPP |
| `inbound/parser.py` | 820 | `app_card` / `hongbao` / `media` / `triggers` / `core` | `client` | **C** | 微信消息类型与 XML 解析，核心资产，不搬 |
| `inbound/persist.py` | 186 | `parser` / `data` | — | **C** | WPP 落库形状 |
| `toolkit/msg.py` | 1073 | 多层 | `client.send_*` / `persist` | **C** | WPP outbound 工具 |
| `platform/adapter.py` | 2883 | 多层 | WPP 全栈 | **C** | 通道适配器本体 |
| `inbound/name_sync.py`、`session_title.py` | 91 / 319 | `data` / `db` | — | **C** | WPP 联系人、会话标题 |

## 3. 审计发现

### 3.1 懒加载已经是事实上的缝

`tts_emotion`、`proactive`、`affection`、`style_learn`、`intent` 在模块顶层都
只 import 标准库；WPP 依赖全部写在函数体里。这意味着：

- 单测可以直接加载这些模块（现有测试已经这么做）；
- 拆分时不需要处理复杂的 import 环；
- 剩余工作主要是**把"懒加载的全局依赖"改成"显式注入的依赖"**。

### 3.2 真正的通道耦合在数据形状

即使模块不 import WPP，它仍默认消息长这样：

```python
msg.msg_type == MT_TEXT
msg.media / msg.reply_to / msg.raw
msg.account_id / msg.peer_kind / msg.peer_id / msg.from_wxid
```

因此 A 级模块能零风险搬；B 级模块必须先把这些字段换成中立协议或 mapping。

### 3.3 反向依赖很轻

- `rate_limit`、`intent` 只被 `adapter.py` 使用；`quote_ref` 只被 `toolkit/msg.py` 使用；
  `media_oss` 被 `adapter.py`、`adapter_media.py`、`app_card.py` 使用（都是 WPP 侧）；
- `tts_emotion` 只被 `toolkit/msg.py` 使用；
- `proactive`、`style_learn` 只被 `adapter.py` 的 sweep 和 `commands.py` 使用；
- `jargon` 被 `adapter.py`、`jargon_tool.py`、`jargon_mine.py` 使用。

搬动时在旧路径保留一行 re-export（`from ..neutral.x import *` 或显式名单），
调用方可以完全不动。

## 4. P0 实施顺序（按风险从低到高）

| 顺序 | 动作 | 预计改动 | 风险 |
|---|---|---|---|
| 1 | `rate_limit` 搬到中立包，旧路径 re-export | 新增 1 文件 + 1 行 shim | 极低 |
| 2 | `jargon` 统计层搬到中立包，DB 查询工具不动 | 新增 1 文件 + 1 行 shim | 极低 |
| 3 | `quote_ref` 用本地 `_safe_str` 替换 `parser` 依赖后搬 | 2 处小改 | 低 |
| 4 | 拆 `tts_emotion`：`voice_style.py`（纯）+ 原文件保留合成/情绪读取 | 1 新文件，测试补 import | 低 |
| 5 | 拆 `proactive`：`proactive_policy.py`（纯）+ 原文件保留执行 | 1 新文件，测试补 import | 低 |
| 6 | `debounce.merge_text_batch` 去 `WppMessage` 类型依赖 | 1 个函数 + 端口校验 | 中（有接线测试） |
| 7 | `media_oss` 注入 env/key builder | 2 个函数签名 | 低 |
| 8 | `intent` / `affection` / `style_learn` 的纯子集上提 | 较大，随 P1 做 | 中 |

前 7 步都不改运行行为，只改 import 位置；每步都可以单独跑全量 pytest 和
`tools/verify_tools_port.py`。

## 5. P1 最小接口（接第二个通道前再落地）

```python
@dataclass(frozen=True)
class ConversationKey:
    platform: str          # "wechatpadpro" / "wecom" / ...
    account_id: str
    chat_type: str         # "dm" / "group"
    chat_id: str

@dataclass(frozen=True)
class MessageRow:          # 与 WppMessage 解耦
    direction: str         # "incoming" / "outbound"
    at_sec: int
    content: str
    sender_id: str = ""

class ConversationStore(Protocol):
    def recent(self, conv: ConversationKey, limit: int) -> list[MessageRow]: ...
    def count_outbound_since(self, conv: ConversationKey, since_sec: int) -> int: ...

class OutboundSender(Protocol):
    async def send_text(self, conv: ConversationKey, text: str, *, quote_id: str | None = None) -> str: ...
    async def send_voice(self, conv: ConversationKey, audio_path: str) -> str: ...
    async def send_file(self, conv: ConversationKey, file_path: str) -> str: ...

@dataclass(frozen=True)
class ChatCapabilities:
    groups: bool
    voice: bool
    quote: bool
    members: bool
    voice_format: str      # "silk" / "mp3" / "ogg"
```

WPP 实现这三个协议；WeCom / Telegram 各自实现。主动消息、画像、风格、禁言、
OSS 都只依赖协议，不再 import `wpp_*`。

## 6. 明确不做的三件事

1. **不整体上提心流**。`triggers.py` 的 @/引用/红包/接龙判定是微信群参与策略，
   换通道后语义不成立。将来最多抽出「是否出声」的纯策略接口。
2. **不把 `WppMessage` 提升为核心类型**。核心只认 mapping / 小协议，避免
   所有通道被迫实现 WPP 的字段。
3. **不预造万能扩展总线**。Hermes 已有 `register_tool` / `register_hook` /
   `pre_gateway_dispatch` / cron；等第二个通道真的需要某功能时，再把它打包成
   独立插件，而不是现在设计一套跨通道框架。

## 7. 第二个通道的建议

生产上已有 `wecom`（智能机器人）。它适合作为第二个消费者，因为：

- 文本/文件出站已具备，`OutboundSender` 最容易落地；
- 没有微信群那套 @/引用/红包语义，正好验证「通道无关层」是否真的无关；
- 能先验证 OSS、TTS、画像、风格、主动消息五个能力，而不触碰心流。

如果目标是 Telegram，则还要额外处理 media group、topic、语音容器格式，
防抖与媒体语义差异更大，建议顺序放在 WeCom 之后。
