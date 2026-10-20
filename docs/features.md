# 功能详解

本文面向使用者和部署者，说明插件除“收发消息 + 调用 `wpp_*` 工具”之外的增强能力：
它们解决什么问题、怎么工作、默认是否开启、需要哪些依赖，以及怎么配置和验证。

工具清单见 [tools.md](tools.md)，环境变量与账号字段总表见
[configuration.md](configuration.md)。

---

## 0. 能力总览

| 能力 | 默认 | 依赖 | 一句话说明 |
|---|---|---|---|
| 群聊心流 | **关闭** | 判定模型 + `JUDGE_API_KEY` | 群里有消息时判断“bot 现在该不该主动接话” |
| 群画像（心流子能力） | **开启** | 同上 | 自动总结每个群的身份、话题、活跃时段，供心流参考 |
| 好感度与情绪 | **关闭** | 可选 LLM | 按用户互动记账，影响回复语气与 TTS 情绪 |
| 意图识别 / 群上下文 | **关闭** | 可选向量模型、可选 LLM | @ 机器人时只挑相关消息注入上下文，避免被无关聊天带偏 |
| 黑话挖掘 | **关闭** | LLM | 从群聊里发现并解释本群特有的词，回复时参考 |
| 说话风格 | **关闭** | LLM | 学习群友的句长、语气词、称呼、表情，让回复更像群里的人 |
| 入站语音转写 | 配好即用 | silk 解码器 + STT key | 把语音消息变成文字给模型看 |
| 出站 AI 语音 | `tts.enabled=false` | TTS 服务 + silk 编码器 | 按当前情绪合成语音；编码失败降级为文件 |
| 历史检索 | **可用** | SQLite（默认）或 MySQL | 按人/群/关键词查本地保存的收发消息 |
| OSS 媒体转存 | **关闭** | `WECHATPRO_OSS_ENABLED=1` + 凭据 | 媒体消息附带公网 URL |
| MCP 兼容工具 | **关闭** | `mcpEnabled: true` | 给 MCP 调用方的精简工具集 |
| 主动关心 | **关闭** | `proactive.enabled=true` + 白名单 | 私聊场景下按策略主动发消息 |

开启方式统一是：编辑**账号 JSON**（`<插件目录>/accounts/<id>.json`）和/或
`.env`，然后重启 Hermes gateway。所有增强能力失败时都只降级并记日志，
**不会阻塞基本收发**。

> 模型名（`model` 字段）由你自己填写，插件不绑定某一家厂商。判定模型、好感度、
> 黑话、风格共用 `JUDGE_API_KEY`（也接受 `MINIMAX_CN_API_KEY` / `MINIMAX_API_KEY`）；
> 向量模型按账号配置里的 `embeddingApiKeyEnv` 取 key。

---

## 1. 群聊心流

### 1.1 它解决什么

群里消息很多，但绝大多数不需要机器人回应。心流（heartflow）负责判断：
**这条群消息值不值得接、现在接是否合时宜**。它只处理群聊；私聊默认直接进入 Agent。

触发规则：

- **被 @（@wxid 或 @昵称）**：直接回复，不经过心流判定。
- **接龙，或账号配置的 msgType / 引用机器人 / 关键词触发**：同样直接进入 Agent。
- **其他群消息**：交给心流判定。只有开启 `heartflow.enabled` 才会发生；关闭时这些
  消息不会触发回复。

### 1.2 一条消息的五步

1. **同步预筛**：检查开关、群白名单、空消息、冷却时间、判定频率、发言预算。
   预筛不通过就不调用模型，省时省钱。
2. **计算生效预算**：账号配置、群画像建议、外环占比三者取**更严格**的一档，
   画像只能让 bot 更克制，不能让它更激进。
3. **五维打分**：一次小模型调用，输出 0–1 的五个维度：

   | 维度 | 默认权重 | 含义 |
   |---|---|---|
   | `relevance` 相关性 | 0.25 | 这条消息与机器人是否相关 |
   | `willingness` 意愿 | 0.20 | 机器人当前“精力”和角色是否适合开口 |
   | `social` 社交适宜 | 0.20 | 此时插话是否自然、是否打断别人 |
   | `timing` 时机 | 0.15 | 是否正好接得上 |
   | `continuity` 连续性 | 0.20 | 是否延续了正在进行的对话 |

   加权总分达到 `replyThreshold`（默认 `0.6`）才回复。
4. **落台账**：无论过不过阈值都写一行记录（过/不过、分数、原因），便于事后复盘。
   被预算拦下的判定也会留痕。
5. **交付与记账**：发出前做重复内容检查，发出后记录结果，用于学习与预算计数。

### 1.3 发言预算与静默段

预算在调用模型**之前**生效，是独立于阈值的“频率闸”：

| 参数 | 默认 | 说明 |
|---|---|---|
| `budget.enabled` | `true` | 预算总开关 |
| `budget.minGapSec` | `180` | 同一群两条主动发言的最小间隔（秒），`0` = 不限 |
| `budget.maxPerHour` | `8` | 每群每小时上限，`0` = 不限 |
| `budget.maxPerDay` | `60` | 每群每天上限，`0` = 不限 |
| `budget.noConsecutiveWithoutHuman` | `true` | 上一条是 bot 自己说的、且之后没人说话时不再连发 |
| `budget.quietHours` | `[]` | 静默时段，形如 `[[23, 7]]`（23:00–06:59，可跨零点） |

计数保存在进程内存，启动时从台账回填小时/天计数，**重启不会清零额度**。
手动配置的 `quietHours` 按你写的值执行；由**群画像建议**并入的静默段有硬上限
（单段不超过 6 小时、全天合计不超过 8 小时），防止模型幻觉让群整天没有响应。

### 1.4 群画像

画像让机器人“理解这个群是什么地方”，而不是对每个群说同一套话。每个群每天
（默认 22 小时）重新生成一次，内容包括群性质、机器人角色、常见话题、活跃时段、
宜忌与建议静默段。

- 默认开启：`heartflow.profile.enabled`，想关写 `false`。
- 画像文本注入判定提示词前会截断（默认 400 字符），并且会过滤“不要插话”一类
  会导致机器人永久沉默的措辞。
- 画像建议的阈值只能**有界抬升**（默认最多 +0.1），预算只能收紧。
- 画像建议的静默段**不会自动生效**；只有 `profile.applyQuietHours: true` 才并入，
  且仍受 6h/8h 硬上限约束。

### 1.5 配置示例

```json
{
  "heartflow": {
    "enabled": true,
    "model": "your-judge-model",
    "timeoutMs": 5000,
    "replyThreshold": 0.6,
    "contextMessagesCount": 5,
    "whitelistGroups": [],
    "weights": {
      "relevance": 0.25,
      "willingness": 0.2,
      "social": 0.2,
      "timing": 0.15,
      "continuity": 0.2
    },
    "budget": {
      "enabled": true,
      "minGapSec": 180,
      "maxPerHour": 8,
      "maxPerDay": 60,
      "noConsecutiveWithoutHuman": true,
      "quietHours": [[23, 7]]
    },
    "profile": {
      "enabled": true,
      "minMsgs": 30,
      "lookbackDays": 14,
      "refreshHours": 22,
      "maxPromptChars": 400,
      "applyQuietHours": false
    }
  }
}
```

`.env`：

```dotenv
JUDGE_API_KEY=...
# 可选：自定义 OpenAI / Anthropic 兼容端点
# JUDGE_BASE_URL=https://your-llm.example/v1
```

要点：

- `heartflow.model` 必填；没有模型时心流不会启用。
- `whitelistGroups` 为空 = 所有准入通过的群都参与；非空 = 只在这些群生效。
- `replyThreshold` 是唯一阈值来源，不要同时在提示词或别处再写一份。
- 判定模型的输出预算很小，建议选响应快、JSON 稳定的模型。

### 1.6 怎么确认在工作

日志里能看到心流判定记录；数据库 `wpp_hf_ledger` 表会持续写入判定结果。
如果配置了但完全没有记录，先检查：`heartflow.enabled`、`heartflow.model`、
`JUDGE_API_KEY`、群准入策略，以及预算/静默段是否把当前时段全部挡住了。

---

## 2. 好感度与情绪

### 2.1 它做什么

让机器人对**每个用户**累积一条好感度，并维护**每个群**当前的情绪状态：

- 被夸奖、感谢、关心 → 好感上升，情绪变积极，回复更热情。
- 被侮辱、骚扰、威胁 → 好感下降，情绪变消极，回复更谨慎。
- 情绪会作为**当次回复的临时 system prompt** 注入，不写进聊天记录，也不影响后续历史。
- 开启了 TTS 情绪时，情绪还会决定合成语音的语气和语速。

### 2.2 工作方式

1. **交互分类**：先查关键词表，零模型调用；只有开启 `llmClassify: true` 且配置了
   `model` 时，关键词命中不了才问 LLM。
2. **好感度增减**：17 种交互各有基础分，再乘当前情绪修正系数。
3. **情绪状态机**：10 种情绪（`happy` / `sad` / `excited` / `calm` / `angry` /
   `anxious` / `playful` / `serious` / `nostalgic` / `curious`），带强度和有效期：
   负面情绪 2 小时、正面情绪 4 小时，过期回到平静。
4. **持久化**：按 `(账号, 群)` 存库（`wpp_affection`），重启不丢。

| 交互 | 基础分 | 交互 | 基础分 |
|---|---|---|---|
| 夸赞表扬 `praise` | +5 | 侮辱攻击 `insult` | −8 |
| 赠送礼物 `gift` | +8 | 骚扰行为 `harassment` | −6 |
| 撩拨调情 `flirt` | +5 | 恶意谩骂 `abuse` | −10 |
| 鼓励支持 `encourage` | +4 | 威胁恐吓 `threat` | −12 |
| 安慰关怀 `comfort` | +4 | 普通聊天 `chat` | +1 |
| 称赞鼓励 `compliment` | +3 | 寻求帮助 `help` | +2 |
| 关心问候 `care` | +3 | 表达感谢 `thanks` | +2 |
| 支持认同 `support` | +4 | 道歉认错 `apology` | +1 |
| 善意调侃 `tease` | +2 | | |

好感度有单人上限（默认 100）和总上限（默认 500），并有衰减系数（默认 0.3）。
整个记账过程是旁路：不等待、失败只记日志，绝不拖慢或阻断收信。

### 2.3 配置

```json
{
  "affection": {
    "enabled": true,
    "model": "your-model",
    "llmClassify": false,
    "timeoutMs": 5000,
    "maxUserAffection": 100,
    "maxTotalAffection": 500,
    "affectionDecayRate": 0.3
  }
}
```

- 只靠关键词就能工作，`model` 可以不填；`llmClassify` 打开时才需要模型。
- 建议先用关键词模式观察一段时间，再决定是否开启 LLM 分类。
- 好感度本身不直接决定“回不回”，它影响的是**怎么回**；要不要回仍由准入、心流
  和 Agent 决定。

---

## 3. 意图识别与群聊上下文

### 3.1 它解决什么

群里被 @ 时，如果把最近 10 条消息全部塞给模型，模型很容易被无关闲聊带偏。
意图识别只做一件事：**从候选消息里挑出真正相关的几条**，作为本次回复的上下文。

它不是“回复/不回复”的判定器。回复与否由准入、心流和 Agent 决定；意图识别只负责
“带上哪些前文”。

### 3.2 三级降级

```
规则预筛（纯 @ / 命令类）
        │
        ├─ 非命令且有向量配置 → embedding 快路径（毫秒级）
        │        └─ 选不出来 / 调用失败 → LLM 判断
        │
        └─ 命令类 → 直接 LLM 判断
                 └─ LLM 也失败 → 按意图兜底：
                      media 只注入媒体消息；topic 不注入
```

- **规则层**：识别纯 @、命令、媒体等简单意图。
- **向量层**：把触发消息和候选消息都做 embedding，按余弦相似度取相关项。
  默认模型 `text-embedding-v4`，只看相似度 ≥ `0.3` 的前 5 条（其中媒体消息单独保留）。
- **LLM 层**：向量不可用或选不出时，用小模型判断。默认超时 5 秒、输出上限 200 token。
- **兜底**：任何一层失败都只是“本次不注入上下文”，绝不阻塞收信。

### 3.3 注入方式

选中的消息会整理成一段带边界标记的临时上下文，放在
`MessageEvent.channel_context` 里，和触发消息本身分开。触发消息**不会**被重复注入。
这段上下文只在当次请求生效，不写入聊天历史。

### 3.4 配置

```json
{
  "groupContextEnabled": true,
  "groupContextWindow": 10,
  "llmIntentEnabled": true,
  "llmIntentModel": "your-small-model",
  "llmIntentTimeoutMs": 5000,
  "embeddingModel": "text-embedding-v4",
  "embeddingApiKeyEnv": "DASHSCOPE_API_KEY",
  "embeddingBaseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "embedIntentTopN": 5,
  "embedIntentThreshold": 0.3
}
```

`.env`：

```dotenv
DASHSCOPE_API_KEY=...
```

说明：

- `groupContextEnabled` 是总开关，默认 `false`。关闭时**零数据库查询、零外部调用**，
  请求体与没有这个功能完全一致。
- `groupContextWindow` 是候选池大小，取值 1–100，默认 10。
- 不配 `embeddingApiKeyEnv` / `embeddingApiKey` 时，向量层关闭，自动走 LLM 层。
- 不配 `llmIntentModel` 或关闭 `llmIntentEnabled` 时，LLM 层也关闭，只保留规则兜底。
- 命令类消息不会走向量层，直接交给 LLM 或兜底。

---

## 4. 黑话挖掘

### 4.1 它做什么

每个群都可能有外人看不懂的词：缩写、谐音、内部梗。黑话模块从群聊中自动发现
高频、集中、近期突然增多的词，用 LLM 判断它是不是真黑话，再推断含义并落库。
回复时，已知黑话会作为提示注入，帮助模型理解群友在说什么。

### 4.2 四段流水线

1. **统计**：对群消息分词并生成 2–4 元词组，过滤停用词、纯数字、标准词典词。
   只保留出现至少 5 次、长度至少 2 的词。
2. **打分排序**：综合分 = `IDF × 0.4 + 爆发度 × 0.3 + 用户集中度 × 0.3`。
   - IDF：越少群出现的词，越可能是本群内部话。
   - 爆发度：最近突然增多（统计窗口最长 14 天）。
   - 集中度：越多人都在用，越可能是黑话；一个人反复说的口头禅得分更低。
3. **批量验证**：一次 LLM 调用最多验证 15 个候选，把普通词筛掉。
4. **含义推断**：对确认的词做“上下文推断 → 字面推断 → 对比”，最多推断 5 个；
   两种推断差异大才认定为黑话并保存含义。

### 4.3 触发与存储

- 每个群独立统计；每轮挖掘有最小间隔（默认 60 秒）和最小新增消息数（默认 10 条）
  双闸，避免频繁调用模型。
- 词条、含义、上下文样例和频次写入 `wpp_jargon_terms`。
- 回复侧通过 `channel_prompt` 注入本群已知黑话，热路径不查库。
- 可以用工具 `wpp_jargon_query(groupId=..., term=...)` 主动查询某个词的含义。

### 4.4 配置

```json
{
  "jargon": {
    "enabled": true,
    "model": "your-model",
    "timeoutMs": 5000,
    "mineIntervalSec": 60,
    "minMessages": 10,
    "maxCandidatesPerGroup": 50
  }
}
```

- `model` 必填，且需要 `JUDGE_API_KEY`；缺模型时该轮挖掘中止并记一条警告。
- 没有 LLM key 时模块静默不工作，不会反复报错。
- 黑话是从群聊内容学出来的，涉及隐私；不需要时保持关闭。

---

## 5. 说话风格

### 5.1 它做什么

学习**这个群的人怎么说话**：句子长短、语气词、口头禅、表情、称呼、标点习惯，
然后把这些特征注入机器人回复时的临时 system prompt。它只改“怎么说”，不改
“要不要说”；群画像负责后者。

### 5.2 三道安全闸

1. **只学摘要，不存原文**：送给模型的样本会匿名成“用户1/用户2”，落库和注入的
   都是归纳后的短字段，不保存聊天原文。
2. **限长 + 净化**：每个字段有硬上限；含 URL、@、wxid、长数字、提示词注入话术或
   明显辱骂的条目直接丢弃。宁可少学，不把脏内容喂回提示词。
3. **置信度门槛**：模型自评置信度低于 `minConfidence`（默认 0.62）时不落库、
   不注入；生成失败保留上一版，不写空卡。

### 5.3 生成节奏

- 默认看最近 14 天的入站消息。
- 清洗后至少 80 条、至少 3 个不同说话人才生成，避免把一个人的口癖当成群风格。
- 同一群默认 72 小时重生成一次，单轮最多处理 2 个群。
- 生成与刷新在后台 sweep 中完成；回复侧只读进程内缓存，热路径零数据库 IO。

### 5.4 配置

```json
{
  "style": {
    "enabled": true,
    "model": "your-model",
    "lookbackDays": 14,
    "minMsgs": 80,
    "minSenders": 3,
    "sampleMsgs": 120,
    "refreshHours": 72,
    "maxPerRun": 2,
    "maxTokens": 700,
    "timeoutMs": 20000,
    "maxPromptChars": 300,
    "minConfidence": 0.62,
    "whitelistGroups": []
  }
}
```

- `whitelistGroups` 为空 = 所有群；非空 = 只学习这些群。
- 风格卡版本保存在 `wpp_style_cards` / `wpp_style_card_versions`，便于审计和回滚。
- 与心流画像可以同时开启：画像决定“该不该说”，风格决定“说得像不像”。

---

## 6. 语音转写与语音发送

### 6.1 入站语音：厂商转写优先

收到语音消息时：

1. **优先使用厂商自带的转写**（`voice.transcript`）。免费、即时，通常也最准；
   有转写就不下载音频、不调用第三方 STT。
2. 没有厂商转写时，插件自己处理：下载 silk → 用解码器转成 PCM → 套 24kHz /
   单声道 / 16-bit 的 WAV 头 → 调用 STT 端点。
3. 转写结果以 `[语音] 收到一条语音\n[转写] …` 的形状进入模型上下文。

### 6.2 STT 后端

| 后端 | 选择方式 | 默认端点 / 模型 |
|---|---|---|
| SiliconFlow | `WPP_STT_BACKEND=siliconflow`（默认） | `https://api.siliconflow.cn/v1/audio/transcriptions` / `FunAudioLLM/SenseVoiceSmall` |
| Token Plan 原生 ASR | `WPP_STT_BACKEND=dashscope` | 阿里云百炼 `multimodal-generation` / `qwen-audio-3.0-asr-flash` |

相关环境变量：

```dotenv
WECHATPRO_STT_API_KEY=...
# 兼容旧名
# SILICONFLOW_API_KEY=...
WPP_STT_BACKEND=siliconflow
# 可选：覆盖端点、模型
# WPP_STT_URL=...
# WPP_STT_MODEL=...
# 可选：自定义 silk 解码器 / 编码器
# WPP_SILK_DECODER_PATH=~/silk_decoder/silk/decoder
# WPP_SILK_ENCODER_PATH=~/silk_decoder/silk/encoder
```

行为细节：

- 单次 HTTP 超时 60 秒；对 401 / 429 / 5xx 最多尝试 3 次，退避 1s、2s。
- 解码器超时 10 秒，避免原生二进制卡死。
- **缺失 key 或解码器不会报错刷屏**：第一条转不出的语音记一条 WARNING，之后降为
  DEBUG；消息照常以 `[语音]` 形式进入上下文。
- 转写工具也可以手动调用：`wpp_voice_message_transcribe(msg_id=...)`（消息已在库）
  或 `wpp_voice_transcribe(audio_base64=...)`（已有音频字节）。

### 6.3 出站语音

- `wpp_send_voice`：发送已有的 SILK/音频地址或 base64。
- `wpp_send_ai_voice`：用 Hermes TTS 或 Token Plan 合成，再转成微信语音气泡。
- 微信只接受 SILK；编码链是 `ffmpeg（24kHz 单声道）→ silk encoder`。缺任一工具时，
  自动降级为**发送同名音频文件**，不会静默丢失。
- 语音时长上限 60 秒；超长会自动分段，编码后超过厂商大小上限时会尝试更低码率。
- 情绪化 TTS（`tts.enabled=true`）会根据好感度模块的当前情绪选择语气、语速，
  默认关闭；`autoVoice` 是自动转语音的概率，`0` 表示不自动转。

```json
{
  "tts": {
    "enabled": true,
    "provider": "tokenplan",
    "voice": "",
    "maxChars": 300,
    "autoVoice": 0,
    "autoVoiceMinChars": 4,
    "autoVoiceMaxChars": 120,
    "whitelistGroups": [],
    "whitelistUsers": [],
    "fallbackMmx": false
  }
}
```

---

## 7. 历史消息检索（SQLite / MariaDB）

### 7.1 默认：SQLite，零配置

插件默认把消息、联系人、群、心流台账、黑话、风格卡、好感度等写入：

```
<插件目录>/wpp.sqlite3
```

- 首次启动自动建表（20 张）与索引，WAL 模式，5 秒 busy timeout。
- 无需安装数据库服务，适合单机单进程的 Hermes gateway。
- 可用 `WECHATPRO_SQLITE_PATH` 改位置；建议放在持久化磁盘并纳入备份。
- 默认不自动清理历史；长期运行请定期备份或自行归档。

### 7.2 可选：MariaDB / MySQL

需要多实例共享、集中备份或已有数据库运维体系时，切换后端：

```dotenv
WECHATPRO_DB_BACKEND=mysql
WECHATPRO_DB_HOST=127.0.0.1
WECHATPRO_DB_PORT=3306
WECHATPRO_DB_USER=wechatpro
WECHATPRO_DB_PASSWORD=...
WECHATPRO_DB_NAME=wechatpro
```

```sql
CREATE DATABASE wechatpro CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'wechatpro'@'127.0.0.1' IDENTIFIED BY '...';
GRANT ALL PRIVILEGES ON wechatpro.* TO 'wechatpro'@'127.0.0.1';
```

要点：

- SQLite 与 MySQL 共用同一份 `plugin/data/schema.sql`；SQLite 侧在运行时做确定性
  方言翻译，两边字段与索引一致。
- 首次连接自动建表；已有库只补缺列/缺索引，并做只读漂移检测，不改列、不删列。
- 远程数据库默认拒绝，必须显式 `WECHATPRO_DB_ALLOW_REMOTE=1`。
- **从旧 MySQL 部署升级时，必须显式补 `WECHATPRO_DB_BACKEND=mysql`**，否则会切到
  一个新的空 SQLite。
- 数据库连不上时收发照常；历史检索、心流台账、黑话/风格持久化降级，启动日志会写明原因。

### 7.3 用工具查历史

```
wpp_history_search(target="张三", keyword="红包", limit=20)
wpp_history_search(target="某群", direction="inbound", limit=50)
```

参数：

| 参数 | 说明 |
|---|---|
| `target` | 昵称 / 备注 / wxid / 群名 / `xxx@chatroom`；留空 = 最近全部 |
| `keyword` | 关键词过滤；填写时在账号范围内按关键词检索 |
| `limit` | 返回条数，默认 20，最大 100 |
| `direction` | `inbound` / `outbound` |
| `account_id` | 多账号部署时指定账号 |

返回值是 JSON 数组，每条包含方向、对端、消息类型、内容（超长截断到 500 字）、
发送者与时间。`target` 会先在本账号的联系人/群表里做匹配，匹配不到就按原值当
wxid/群 id 使用。

### 7.4 直接查库

排查或做报表时可以直接读库。两张最常用的表：

| 表 | 内容 |
|---|---|
| `wpp_messages` | 收发消息：方向、对端、类型、正文、原始 payload、入库时间 |
| `wpp_contacts` / `wpp_chatrooms` | 联系人与群资料，用于把昵称解析成 wxid/群 id |

时间字段说明：`ts` 是**入库时间**，所有排序和聚合都用它；`create_time` 是历史字段，
可能为空或格式不统一，不要依赖。

SQLite 示例：

```bash
sqlite3 /path/to/wpp.sqlite3 \
  "SELECT ts, direction, peer_id, msg_type, substr(content,1,80)
     FROM wpp_messages
    WHERE account_id='default'
    ORDER BY ts DESC LIMIT 20;"
```

MySQL / MariaDB 示例：

```sql
SELECT ts, direction, peer_id, msg_type, LEFT(content, 80)
  FROM wpp_messages
 WHERE account_id = 'default'
 ORDER BY ts DESC
 LIMIT 20;
```

其他常用表：`wpp_hf_ledger`（心流台账）、`wpp_hf_group_profile`（群画像）、
`wpp_jargon_terms`（黑话）、`wpp_affection`（好感度/情绪）、`wpp_style_cards`
（风格卡）、`wpp_api_calls`（接口审计）。

---

## 8. 建议的开启顺序

1. **先跑通基本收发**：只配 vendor 地址与凭据，保持所有增强能力关闭。
2. **开历史检索**：默认 SQLite 即可；确认 `wpp_history_search` 能查到消息。
3. **按需开心流**：先只开 `heartflow.enabled` + 模型，保持默认预算，观察一周台账。
   确认没有刷屏或长时间沉默后，再考虑调整阈值/预算。
4. **再考虑画像与风格**：画像默认开启可以先观察；风格学习建议积累足够消息
   （≥ 80 条、≥ 3 个说话人）后再开。
5. **黑话、好感度、意图识别**属于锦上添花，按群的实际需求逐个开，每次只改一项，
   便于判断效果。
6. **语音**先确认厂商是否自带转写；没有时再配解码器和 STT key。

---

## 9. 文件传输助手命令

在微信里给机器人的「文件传输助手」发命令，可以查看状态、开关能力、管理白名单，
不必每次改文件再重启。命令只影响当前账号；修改会写回账号 JSON 并立即生效。

| 命令 | 作用 |
|---|---|
| `/help` | 帮助与命令列表 |
| `/heartflow on\|off\|status` | 心流开关与状态 |
| `/heartflow threshold\|group\|profile\|why\|layers\|veto\|report` | 心流阈值、分群、画像、归因与复盘 |
| `/affection on\|off\|status` | 好感度开关与状态 |
| `/jargon on\|off\|status` | 黑话挖掘开关与状态 |
| `/style on\|off\|status` | 说话风格开关与状态 |
| `/style list [群id]` / `/style show <群id>` / `/style rollback <群id> <版本>` | 风格卡查看与回滚 |
| `/tts on\|off\|status` | 语音合成 / 情绪路由开关 |
| `/proactive on\|off\|status` | 私聊主动关心开关 |
| `/mute <群id> [分钟]` / `/mute off <群id>` / `/mute list` | 临时禁言（默认 30 分钟，内存态，重启失效） |
| `/xiaowei on\|off\|status` | 小微智能体工具开关 |
| `/user add\|del\|list [wxid...]` | 私聊白名单（收发共用） |
| `/user sync` | 从全员群同步员工到私聊白名单 |
| `/group add\|del\|list [群id...]` | 群白名单（收发共用） |
| `/blacklist add\|del\|list [群id...]` | 群黑名单（一票否决，优先于群白名单） |
| `/whitelist status` | 查看统一白名单（私聊/群/管理员/全员群/env 额外项） |
| `/whitelist sync` | 同 `/user sync` |
| `/genpair` / `/pairs` | 管理员生成 / 查看配对码 |
| `/pair <8位码>` | 普通用户私聊自助配对（仅在 `dmPolicy=pairing` 时） |

说明：

- `/genpair`、`/pairs` 和 `/pair` 的权限边界不同：前两者是管理员查看/生成，
  后者是普通用户兑换。
- 白名单命令支持批量；`/group` 与 `/blacklist` 是两套名单，黑名单优先级更高。
- 写回失败时会明确回复“本次改动未生效”，不会返回假的成功。

---

## 10. 常见问题

**开了心流但群里毫无反应？**
依次检查：群准入是否放行、`heartflow.model` 是否填写、`JUDGE_API_KEY` 是否有效、
预算/静默段是否把当前时段挡住、`whitelistGroups` 是否写错。台账表没有新增记录，
说明请求根本没到判定步骤。

**心流回复太频繁 / 太沉默？**
优先调预算（`minGapSec` / `maxPerHour` / `maxPerDay`）和静默段，而不是大幅改
`replyThreshold`。阈值同时影响语气与频率，容易越调越偏。

**配置了向量模型但意图识别没变化？**
确认 `groupContextEnabled` 已开，并且账号里写了 `embeddingApiKeyEnv`（或
`embeddingApiKey`），对应的环境变量确实存在。只配 `embeddingModel` 不会启用向量层。

**语音一直只有 `[语音]`？**
厂商没有返回转写，且本地缺 STT key 或 silk 解码器。日志里第一条这样的语音会有
一次 WARNING，写明缺的是哪一项。

**历史查不到，但消息确实收发了？**
先确认查的是正确的 `account_id`；多账号部署下不同账号的数据是分开的。再确认数据库
后端与路径：切过 `WECHATPRO_DB_BACKEND` 但没重启，或切到新的空 SQLite，都会表现为
“历史突然没了”。

**升级后历史不见了？**
旧 MySQL 部署升级到默认 SQLite 版本时，必须补 `WECHATPRO_DB_BACKEND=mysql`。
数据没有丢，只是连到了另一个库。

**黑话/风格/好感度会保存聊天原文吗？**
不会。好感度只存分值与情绪；风格只存归纳后的短字段；黑话存词、含义和少量上下文
样例。需要完全关闭时把对应 `enabled` 设为 `false`。
