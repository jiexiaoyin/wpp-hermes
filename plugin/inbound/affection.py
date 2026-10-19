"""AFFECTION 好感度 / 情绪状态机 —— 逐字移植 TS ``src/inbound/affection.ts``（v1.3.77）。

## 它做什么

让 bot 对**每个用户**攒一条好感度，并维护一个**每群**的情绪状态：

* 用户夸它 ⇒ 好感 +5、心情变 happy ⇒ 回复更热情；
* 用户骂它 ⇒ 好感 −8、心情变 sad ⇒ 回复更谨慎；
* 情绪经 ``MessageEvent.channel_prompt`` 注入**当次** system prompt（见
  :func:`build_mood_system_prompt`）。

分三层，**每层的失败都不许影响入站链路**（TS 那边整块是 ``void processAffectionMessage``）：

① **交互分类**：5 张关键词表（零 LLM）；表命中不了且开了 ``llmClassify`` 才问 LLM；
   再问不出来兜底 ``chat``。
② **好感度增减**：17 种交互 × 基础分 × 情绪修正系数（情绪门槛 / 上限 / **截断取整**）。
③ **情绪状态机**：10 种情绪 × 强度；负面交互触发 sad/angry/anxious，正面触发 happy/excited，
   一般的只微调强度。

## INVARIANT: W2 —— 打分是**旁路**，绝不阻塞收信

三条判据（``plugin/tests/platform/test_affection_wiring.py`` 逐条钉住）：

1. **不 await**：``_handle_one`` 里只 ``create_task``（TS 是 ``void processAffectionMessage``）；
   任何一条路径上 ``await`` 它 = 把 DB 读改写 + 5s 超时的 LLM 调用塞进收信链路。
2. **失败只 warn**：记账异常在 done-callback 里记一行，**绝不上抛**（与 W6 的
   「写回失败必须出声」刚好相反 —— 那条不说就等于说谎，这条出声就等于害群）。
3. **开关关着 ⇒ 零副作用**：``affection.enabled`` 为假时**一次 DB、一次 LLM 都不碰**
   （守卫必须在读库**之前**），情绪注入返回 ``None`` ⇒ 请求体与"没这个功能"逐字节相同。

## 与 TS 的**有意偏离**（三处，别当成移植漏项）

1. **状态落库，不落内存**。TS 把 ``groupStates`` 放在模块级 ``Map`` 里（``affection.ts:489``）
   —— 进程一重启好感度清零。老板 2026-10-02 拍板**落库持久化**（比 TS 多做一截）。
   读写走 :mod:`plugin.data.repo_affection`（两张粒度：分值 / 情绪）。
2. **情绪注入走 ``channel_prompt``，不塞正文**。TS 把 moodNote 前置进 **Body**
   （``dispatcher.ts:864-865`` → ``injectedContext``）；Hermes 的
   ``MessageEvent.channel_prompt`` 正是"当次生效、**不进 transcript**"的临时 system prompt
   （``gateway/platforms/events.py``），比塞正文更贴 TS 自己给它的标签
   ``[系统提示-当前情绪]``。语义等价、载体不同。
3. **情绪按 ``(account_id, group_id)`` 存**。TS 只按 ``groupId``（单账号假设）；本树是多账号，
   两个账号的同一个群必须各记一份。

## 逐字保留的四个语义（每一条都对应一次误伤或崩溃）

* **情绪修正是截断取整**（``Math.trunc``，``affection.ts:321``）—— ``trunc(-8 * 0.4) == -3``，
  不是 ``floor``（``floor(-3.2) == -4``，负向交互会被多扣）。Python ``math.trunc`` 同义。
* ``|moodEffect| < 0.1`` 直接返回 ``None``（``:429``）—— 影响太小的不写库。
* ``parse_classify_response`` **必须校验类型在白名单里**（``:239``）—— LLM 幻觉出
  ``"anger"`` 之类的键，不校验就会在规则表查到时炸。
* **关键词判定顺序即优先级**（``:175-193``）：``THREAT`` 排在 ``NEGATIVE`` **之前**。
  顺序一换，「去死」这类同时命中两表的词就从「威胁」降级成「侮辱」（−12 → −8）。

## 关键词表为什么都是多字词（P1，2026-08-23）

TS 注释写得很清楚：单字正词（美/棒/牛/强/好/6/萌/帅）用裸 ``includes`` 会把
「晚上好」「你好呀」判成称赞；单字负向词（死/滚/草/狗/操/贱/婊）会把「笑死了」「狗粮」
判成侮辱（好感 −8 + 情绪 sad 两小时）。**别顺手加回单字词**。
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

log = logging.getLogger(__name__)

MoodType = Literal[
    "happy", "sad", "excited", "calm", "angry",
    "anxious", "playful", "serious", "nostalgic", "curious",
]
InteractionType = Literal[
    "chat", "compliment", "flirt", "comfort", "help",
    "thanks", "apology", "tease", "care", "gift",
    "insult", "harassment", "abuse", "threat",
    "praise", "encourage", "support",
]

#: 情绪有效期：负面 2 小时、正面 4 小时（``affection.ts:443/455``）。
NEGATIVE_MOOD_TTL_SEC = 2 * 3600
POSITIVE_MOOD_TTL_SEC = 4 * 3600

# ===== 情绪修正系数（对齐 BotMood.get_mood_modifier）=====

MOOD_MODIFIERS: dict[str, float] = {
    "happy": 1.2,
    "excited": 1.3,
    "playful": 1.1,
    "calm": 1.0,
    "curious": 1.05,
    "nostalgic": 0.9,
    "serious": 0.8,
    "sad": 0.6,
    "anxious": 0.7,
    "angry": 0.4,
}


def mood_modifier(mood: str, intensity: float) -> float:
    """情绪修正系数 = ``base * (0.5 + clamp(intensity, 0, 1) * 0.5)``（``:47``）。"""
    base = MOOD_MODIFIERS.get(mood, 1.0)
    return base * (0.5 + _clamp(intensity, 0.0, 1.0) * 0.5)


# ===== 交互规则表（对齐 _init_affection_rules）=====


@dataclass(frozen=True)
class AffectionRule:
    base_change: int
    mood_sensitive: bool
    mood_effect: float
    description: str
    #: 情绪门槛：当前情绪必须是这些之一才允许变化
    mood_requirements: tuple[str, ...] = ()
    #: 提升积极情绪
    positive_mood_boost: bool = False
    #: 触发负面情绪
    negative_mood_trigger: bool = False
    #: 触发恐惧情绪
    trigger_fear: bool = False


AFFECTION_RULES: dict[str, AffectionRule] = {
    "chat": AffectionRule(1, True, 0.1, "普通聊天"),
    "compliment": AffectionRule(3, True, 0.2, "称赞鼓励"),
    "praise": AffectionRule(5, True, 0.3, "夸赞表扬", positive_mood_boost=True),
    "encourage": AffectionRule(4, True, 0.25, "鼓励支持", positive_mood_boost=True),
    "support": AffectionRule(4, True, 0.2, "支持认同"),
    "flirt": AffectionRule(5, True, 0.15, "撩拨调情",
                           mood_requirements=("happy", "playful", "excited")),
    "comfort": AffectionRule(4, True, 0.3, "安慰关怀", mood_requirements=("sad", "anxious")),
    "help": AffectionRule(2, False, 0.1, "寻求帮助"),
    "thanks": AffectionRule(2, True, 0.15, "表达感谢"),
    "apology": AffectionRule(1, True, 0.1, "道歉认错", mood_requirements=("angry", "sad")),
    "tease": AffectionRule(2, True, 0.1, "善意调侃", mood_requirements=("playful", "happy")),
    "care": AffectionRule(3, True, 0.2, "关心问候"),
    "gift": AffectionRule(8, True, 0.4, "赠送礼物", positive_mood_boost=True),
    "insult": AffectionRule(-8, True, -0.5, "侮辱攻击", negative_mood_trigger=True),
    "harassment": AffectionRule(-6, True, -0.4, "骚扰行为", negative_mood_trigger=True),
    "abuse": AffectionRule(-10, True, -0.6, "恶意谩骂", negative_mood_trigger=True),
    "threat": AffectionRule(-12, True, -0.7, "威胁恐吓",
                            negative_mood_trigger=True, trigger_fear=True),
}

# ===== 配置 =====


@dataclass(frozen=True)
class AffectionConfig:
    """``accounts/<id>.json`` 的 ``affection`` 段解析结果。

    ⚠️ ``enabled`` 在**键缺席**时是 ``False``（fail-closed），而 TS 的
    ``defaultAffectionConfig()``（``:111``）写着 ``true`` —— 同一个函数上方的接口注释与
    ``openclaw.plugin.json`` 的 schema 都写 false，TS 自相矛盾。取保守一侧。
    **注意**：生产账号文件里显式写着 ``"enabled": true``（实测），所以那台机器上实际是开的 ——
    这里的默认只管"键缺失"。

    ⚠️ ``model`` **没有**兜底值。TS 在 ``:266-274`` 里当场抛错再被外层 catch 吞掉，
    效果等于"没配 model 就不做 LLM 分类"；本实现直接不调用，等价且少一次异常。
    实测 ``openclaw.plugin.json`` 的 ``affection.properties`` **只有 ``enabled``**
    （README 式注释说的"schema default 提供 model"并不存在），生产账号文件里
    ``llmClassify`` 也没写 ⇒ **LLM 分类在生产上从来没跑过**，一直是纯关键词。
    """
    enabled: bool = False
    max_user_affection: int = 100
    max_total_affection: int = 500
    affection_decay_rate: float = 0.3
    model: str = ""
    timeout_ms: int = 5000
    llm_classify: bool = False

    @classmethod
    def from_cfg(cls, cfg: Mapping[str, Any] | None) -> AffectionConfig:
        """从账号 cfg 的 ``affection`` 段解析（坏值一律退回默认，**不抛**）。"""
        d = cfg if isinstance(cfg, Mapping) else {}
        return cls(
            enabled=_as_bool(d.get("enabled"), False),
            max_user_affection=_as_int(d.get("maxUserAffection"), 100),
            max_total_affection=_as_int(d.get("maxTotalAffection"), 500),
            affection_decay_rate=_as_float(d.get("affectionDecayRate"), 0.3),
            model=str(d.get("model") or "").strip(),
            timeout_ms=_as_int(d.get("timeoutMs"), 5000),
            llm_classify=_as_bool(d.get("llmClassify"), False),
        )


def resolve_affection_cfg(raw: Mapping[str, Any] | None) -> AffectionConfig:
    """``Account.affection``（裸 dict，见 ``core/accounts.py:160``）→ :class:`AffectionConfig`。

    默认值在这里给，而不是在 ``Account`` 上加字段：那个 dataclass 是 ``frozen`` 的，
    且 W6 的账号写回特意**不**从它反序列化 —— 让它只当"原始 cfg 的搬运工"更安全。
    """
    return AffectionConfig.from_cfg(raw)


# ===== 关键词规则分类（对齐 _rule_based_interaction_analysis）=====

COMPLIMENT_KEYWORDS = (
    "好美", "漂亮", "可爱", "美丽", "好看", "厉害", "优秀", "聪明", "温柔", "体贴", "贴心", "善良",
    "完美", "很棒", "真好", "不错", "赞", "给力", "牛逼", "好啊", "好呀", "棒棒", "太棒了", "真棒",
    "真厉害", "哇塞", "厉害了", "太好了", "好厉害", "好强", "好棒", "赞赞", "牛牛", "牛b", "nb",
    "牛批", "牛皮", "好牛", "超棒", "超好", "很好", "很棒", "很厉害", "太厉害了", "好喜欢", "喜欢你",
    "爱了", "太可爱了", "好可爱", "可爱爆了", "萌萌", "好萌",
)
THANKS_KEYWORDS = ("谢谢", "感谢", "多谢", "thank", "谢", "thx", "谢啦", "谢了")
CARE_KEYWORDS = (
    "你好", "早上好", "晚上好", "怎么样", "最近好吗", "hello", "hi", "嗨", "哈喽",
    "哈罗", "早安", "晚安", "午安", "下午好", "你在吗", "在吗", "你在不在",
    "在不在", "你好呀", "你好啊",
)
NEGATIVE_KEYWORDS = (
    "傻逼", "蠢货", "白痴", "垃圾", "废物", "去死", "妈的", "他妈", "畜生", "王八蛋", "神经病",
    "滚蛋", "混蛋", "欠揍", "找打", "白痴啊",
)
#: ⚠️ 2026-10-19 审阅修复：**不要**收单字/裸动词。
#: 旧表里的 ``"杀"`` 让 3C 群的高频词「秒杀/秒杀价/限时秒杀」全部判成 threat
#: （-12 好感度 + 触发 fear）；``"安"``/``"早"`` 让「安装/安卓/早教」判成 care。
#: 关键词表是生产上**唯一**在跑的交互分类器（LLM 分类未启用），误判会直接改好感度。
THREAT_KEYWORDS = (
    "威胁", "杀了你", "杀死你", "杀你", "打死", "打死你", "弄死", "弄死你",
    "干掉", "揍你", "打你",
)

#: 判定顺序 = 优先级（``affection.ts:175-193``）。**THREAT 必须在 NEGATIVE 之前**。
_RULE_KEYWORD_TABLE: tuple[tuple[tuple[str, ...], str], ...] = (
    (COMPLIMENT_KEYWORDS, "compliment"),
    (THANKS_KEYWORDS, "thanks"),
    (CARE_KEYWORDS, "care"),
    (THREAT_KEYWORDS, "threat"),
    (NEGATIVE_KEYWORDS, "insult"),
)


def classify_interaction_by_rules(message: str) -> str | None:
    """关键词规则交互分类。返回 ``None`` = 判断不了（交给 LLM 或兜底 ``chat``）。"""
    lower = (message or "").lower().strip()
    for keywords, interaction in _RULE_KEYWORD_TABLE:
        for kw in keywords:
            if kw in lower:
                return interaction
    return None


# ===== LLM 交互分类（可选增强，对齐 analyze_interaction_type）=====


def build_classify_prompt(message: str, sender_name: str) -> str:
    """构造 LLM 交互分类 prompt（``:204-229``，逐字）。"""
    who = f" {sender_name}" if sender_name else ""
    return f"""你是微信机器人{who}的社交关系分析器。请判断用户消息的交互类型。

消息: "{message}"

从以下类型中选择一个 (返回 JSON: {{"type": "xxx"}}):
- chat: 普通聊天/闲聊
- compliment: 称赞/夸外貌/夸表现
- praise: 夸赞表扬 (比 compliment 更强烈)
- encourage: 鼓励支持
- support: 支持认同
- flirt: 撩拨调情
- comfort: 安慰关怀
- help: 寻求帮助
- thanks: 表达感谢
- apology: 道歉认错
- tease: 善意调侃
- care: 关心问候
- gift: 赠送礼物
- insult: 侮辱攻击
- harassment: 骚扰行为
- abuse: 恶意谩骂
- threat: 威胁恐吓

不确定时优先 "chat"。只输出 JSON。"""


_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")
_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def parse_classify_response(text: str) -> str | None:
    """解析 LLM 分类响应 → 交互类型，坏输出一律 ``None``（``:232-254``）。

    两条容错：先剥 ```` ```json ```` 围栏；围栏里不是合法 JSON 时，再退一步抓**第一个
    ``{...}`` 子串**（模型常在 JSON 前后带一句解释）。

    ⚠️ **必须**用 :data:`AFFECTION_RULES` 校验类型。少这一步，模型编出 ``"anger"``
    时调用方会在规则表里查空 —— TS 的 ``key in obj`` 会顺手命中 ``toString`` 之类的
    原型键（那是 TS 自己的一个潜在 bug），本实现用的是精确的键成员判定，**不**复刻它。
    """
    if not text:
        return None
    m = _FENCE_RE.search(text.strip())
    s = (m.group(1) if m else text).strip()
    out = _extract_type(s)
    if out is not None:
        return out
    m2 = _OBJECT_RE.search(s)
    return _extract_type(m2.group(0)) if m2 else None


def _extract_type(s: str) -> str | None:
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    if isinstance(t, str) and t in AFFECTION_RULES:
        return t
    return None


async def classify_interaction_with_llm(
    message: str, sender_name: str, cfg: AffectionConfig,
) -> str | None:
    """调 judge 做交互分类。**任何失败都降级为 ``None``**（调用方再退到 ``chat``）。

    ⚠️ 这是与 TS 的一个必须注意的差异：TS 的 ``callJudge`` 失败会被这一层自己 catch 成
    ``undefined``；而 py2 的 :func:`plugin.llm.judge.call_judge` **一律 raise**
    （``JudgeError``），所以这里的 try/except 是语义的一部分，不是防御性代码 ——
    少了它，"降级为关键词分类"这条链就断了，入站会直接抛穿。
    """
    if not cfg.llm_classify or not cfg.model:
        # 没开 LLM 分类、或没配 model（TS 在 :266 抛错后被吞，效果相同）
        return None
    try:
        from ..llm.judge import call_judge, resolve_judge_creds

        text = await call_judge(
            model=cfg.model,
            user_prompt=build_classify_prompt(message, sender_name),
            max_tokens=50,
            timeout_ms=cfg.timeout_ms,
            creds=resolve_judge_creds(),
        )
        return parse_classify_response(text)
    except Exception as exc:  # noqa: BLE001 —— 分类失败绝不许影响入站
        log.debug("[WPP AFFECTION] LLM 分类失败，降级关键词: %s", exc)
        return None


# ===== 好感度计算（对齐 _calculate_affection_change）=====


@dataclass(frozen=True)
class InteractionResult:
    interaction_type: str
    can_change: bool
    change: int
    reason: str


def calculate_affection_change(
    interaction_type: str, current_level: int, current_mood: MoodState | None,
    max_user_affection: int,
) -> InteractionResult:
    """纯函数：算这次交互让好感度变多少（``:300-340``）。

    三道闸的**先后顺序是语义**：先情绪门槛 → 再算变化 → 最后上限。
    顺序换了，一个"当前心情不适合调情"的交互会先去撞上限，报出来的理由是错的。
    """
    rule = AFFECTION_RULES.get(interaction_type) or AFFECTION_RULES["chat"]

    # 情绪门槛
    if rule.mood_requirements and current_mood is not None:
        if current_mood.mood_type not in rule.mood_requirements:
            return InteractionResult(
                interaction_type, False, 0,
                f"当前心情({current_mood.mood_type})不适合{rule.description}",
            )

    actual_change = rule.base_change

    # 情绪修正 —— **截断取整**（见模块 docstring）
    if rule.mood_sensitive and current_mood is not None:
        mod = mood_modifier(current_mood.mood_type, current_mood.intensity)
        actual_change = math.trunc(rule.base_change * mod)

    # 上限（只挡正向；负向交互在满好感时照样扣）
    if current_level >= max_user_affection and actual_change > 0:
        return InteractionResult(interaction_type, False, 0, "好感度已达到上限")

    return InteractionResult(interaction_type, True, actual_change, rule.description)


# ===== 重分配（对齐 _redistribute_affection）=====


def plan_redistribution(
    all_affections: Sequence[tuple[str, int]], target_user_id: str,
    increase_amount: int, decay_rate: float,
) -> dict[str, int]:
    """群总量超限时按比例从**其他用户**（好感高的先扣）扣减。纯函数，返回应扣映射。

    ⚠️ 三处边界照抄（``:348-374``）：``totalOthersLevel <= 0`` 直接返空；
    单次扣减 ``max(1, trunc(level * ratio * decayRate))`` 再 ``min(扣量, level, 剩余要扣)``
    —— ``max(1, …)`` 保证"只要还在扣就一定扣得动"，否则比例算出来是 0 就死循环式空转。
    """
    result: dict[str, int] = {}
    total_to_decrease = max(0, increase_amount)

    others = sorted(
        (a for a in all_affections if a[0] != target_user_id),
        key=lambda a: a[1], reverse=True,
    )
    total_others_level = sum(max(0, lv) for _, lv in others)
    if total_others_level <= 0:
        return result

    for user_id, level in others:
        if total_to_decrease <= 0:
            break
        if level <= 0:
            continue
        decrease_ratio = min(1.0, total_to_decrease / total_others_level)
        decrease_amount = max(1, math.trunc(level * decrease_ratio * decay_rate))
        decrease_amount = min(decrease_amount, level, total_to_decrease)
        result[user_id] = decrease_amount
        total_to_decrease -= decrease_amount
    return result


# ===== 情绪状态机 =====


@dataclass(frozen=True)
class MoodState:
    mood_type: str
    intensity: float
    description: str
    #: **绝对 epoch 秒**（不是相对秒）—— 相对秒在进程重启后判不了过期。
    expires_at: int


MOOD_PROMPTS: dict[str, str] = {
    "happy": "你现在心情很好，说话比较活泼开朗，容易表达正面情感。",
    "sad": "你现在心情有些低落，说话比较温和，需要更多的理解和安慰。",
    "excited": "你现在很兴奋，说话比较有活力，对很多事情都很感兴趣。",
    "calm": "你现在心情平静，说话比较稳重，给人安全感。",
    "angry": "你现在心情不太好，说话可能比较直接，不太有耐心。",
    "anxious": "你现在有些紧张不安，说话可能比较谨慎，需要更多确认。",
    "playful": "你现在心情很调皮，喜欢开玩笑，说话比较幽默风趣。",
    "serious": "你现在比较严肃认真，说话简洁直接，专注于重要的事情。",
    "nostalgic": "你现在有些怀旧情绪，说话带有回忆色彩，比较感性。",
    "curious": "你现在对很多事情都很好奇，喜欢提问和探索新事物。",
}

MOOD_DESCRIPTIONS: dict[str, str] = {
    "happy": "心情愉快",
    "sad": "心情低落",
    "excited": "兴奋激动",
    "calm": "平静",
    "angry": "愤怒",
    "anxious": "紧张不安",
    "playful": "调皮",
    "serious": "严肃",
    "nostalgic": "怀旧",
    "curious": "好奇",
}


def default_mood(now_sec: int) -> MoodState:
    """默认情绪：平静、强度 0.5。``expires_at`` = **当下**（即立刻算过期，``:413``）。"""
    return MoodState("calm", 0.5, MOOD_DESCRIPTIONS["calm"], now_sec)


def apply_mood_response(
    current_mood: MoodState, interaction_type: str, now_sec: int,
) -> MoodState | None:
    """交互对情绪的影响。返回新情绪，``None`` = **情绪不变**（``:421-464``）。

    三种走向：负面触发（威胁→anxious / 谩骂→angry / 侮辱→sad / 骚扰→anxious）、
    正面触发（送礼→excited / 其余→happy）、一般调整（只动强度，夹在 ``[0.1, 0.9]``）。

    ⚠️ 两个"太小就不动"的闸都是语义不是优化：``|moodEffect| < 0.1`` 直接不处理；
    一般调整里 ``|新 - 旧| <= 0.1`` 也算不动 —— 少了它，每次普通聊天都会写一次库。
    """
    rule = AFFECTION_RULES.get(interaction_type)
    if rule is None:
        return None
    mood_effect = rule.mood_effect
    if abs(mood_effect) < 0.1:
        return None

    if rule.negative_mood_trigger:
        if interaction_type == "threat":
            new_mood = "anxious"
        elif interaction_type == "abuse":
            new_mood = "angry"
        elif interaction_type == "insult":
            new_mood = "sad"
        else:                                   # harassment
            new_mood = "anxious"
        return MoodState(new_mood, min(0.9, abs(mood_effect)),
                         MOOD_DESCRIPTIONS[new_mood],
                         now_sec + NEGATIVE_MOOD_TTL_SEC)

    if rule.positive_mood_boost:
        new_mood = "excited" if interaction_type == "gift" else "happy"
        return MoodState(new_mood, min(0.8, mood_effect),
                         MOOD_DESCRIPTIONS[new_mood],
                         now_sec + POSITIVE_MOOD_TTL_SEC)

    intensity = _clamp(current_mood.intensity + mood_effect, 0.1, 0.9)
    if abs(intensity - current_mood.intensity) <= 0.1:
        return None
    return replace(current_mood, intensity=intensity)


def build_mood_system_prompt(base_prompt: str, mood: MoodState) -> str:
    """情绪版 system prompt（``:466-478``）。

    ⚠️ 三处逐字保留：

    1. ``replace("现在", …, 1)`` —— **只换第一处**。JS 的 ``String.replace(串, 串)``
       只替换第一个匹配，Python 的 ``str.replace`` 默认全换。当前 10 条模板里"现在"
       都只出现一次，所以两者今天等价 —— 但用 ``count=1`` 才与蓝本同义（将来改文案不会踩）。
    2. **幂等去重串**：``base_prompt`` 里已含 心情/情绪 之类字样就直接返回，否则同一段
       基础 prompt 每轮都会被追加一遍情绪说明。
    3. 强度副词：``>0.7`` 非常 / ``>0.4`` 有些 / 其余 轻微。
    """
    mood_prompt = MOOD_PROMPTS.get(mood.mood_type, "")
    if not mood_prompt:
        return base_prompt
    if mood.intensity > 0.7:
        intensity_modifier = "非常"
    elif mood.intensity > 0.4:
        intensity_modifier = "有些"
    else:
        intensity_modifier = "轻微"
    final_mood_prompt = mood_prompt.replace("现在", f"现在{intensity_modifier}", 1)

    mood_keywords = ("当前情绪状态", "心情", "情绪", "【当前情绪状态", "【增量更新")
    if any(kw in base_prompt for kw in mood_keywords):
        return base_prompt

    return (f"{base_prompt}\n\n当前情绪状态：{mood.description} {final_mood_prompt}"
            f"\n\n请根据以上情绪状态调整你的回复风格和语气。")


def format_affection_summary(mood: MoodState, users: Sequence[tuple[str, int]],
                             limit: int = 10) -> str:
    """群状态摘要（对齐 ``getAffectionSummary``，``:614-624``）。供 ``/affection status``。"""
    top = sorted(users, key=lambda u: u[1], reverse=True)[:limit]
    lines = [f"- {uid}: {lv}" for uid, lv in top]
    return "\n".join([
        f"情绪: {mood.description} (强度 {mood.intensity:.1f}, "
        f"{MOOD_PROMPTS.get(mood.mood_type, '')})",
        f"用户数: {len(users)}",
        *lines,
    ])


# ===== 核心入口（有状态）=====


@dataclass(frozen=True)
class AffectionOutcome:
    interaction_type: str
    change: int
    can_change: bool
    reason: str


async def process_affection_message(
    account_id: str, group_id: str, user_id: str, message: str,
    sender_name: str, cfg: AffectionConfig, now_sec: int,
    *, conn: Any = None,
) -> AffectionOutcome | None:
    """处理一条群消息的好感度 + 情绪更新（对齐 ``processAffectionMessage``，``:542-611``）。

    与 TS 的两处差异都是"落库"带来的：

    * 状态从 :mod:`plugin.data.repo_affection` 读写，不是模块级 ``Map``；
    * 读-改-写之间**没有事务**跨这两张表 —— 单条消息的记账不值得为它开事务，而
      ``wpp_hf_*`` 那边的先例也是"高频写库、失败只 warn"（见 ``repo_hf.py:487``）。
      落库失败一律 ``raise`` 出去由调用方吞成一行 warn（**不影响入站**）。

    ⚠️ 顺序照抄 TS：**先算变化、再改情绪、最后重分配**。重分配读的是"改完之后的群总量"，
    顺序换了超限判定就会差一条消息的量。
    """
    from ..data import repo_affection as repo

    if not cfg.enabled:
        return None

    # ① 交互分类（规则优先，LLM 可选增强，最后兜底 chat）
    interaction_type = classify_interaction_by_rules(message)
    if not interaction_type:
        interaction_type = await classify_interaction_with_llm(message, sender_name, cfg)
    if not interaction_type:
        interaction_type = "chat"

    current_mood = repo.get_mood(account_id, group_id, now_sec, conn=conn)
    current_level = repo.get_user_level(account_id, group_id, user_id, conn=conn)

    # ② 计算
    calc = calculate_affection_change(
        interaction_type, current_level, current_mood, cfg.max_user_affection)

    # ③ 应用
    if calc.can_change:
        repo.set_user_level(account_id, group_id, user_id,
                            max(0, current_level + calc.change), now_sec, conn=conn)

    # ④ 情绪响应
    new_mood = apply_mood_response(current_mood, interaction_type, now_sec)
    if new_mood is not None:
        repo.set_mood(account_id, group_id, new_mood, conn=conn)

    # ⑤ 群总量超限 → 重分配
    total = repo.get_group_total(account_id, group_id, conn=conn)
    if total > cfg.max_total_affection:
        excess = total - cfg.max_total_affection
        if calc.can_change and calc.change > 0:
            plan = plan_redistribution(
                repo.list_user_levels(account_id, group_id, conn=conn),
                user_id, excess, cfg.affection_decay_rate,
            )
            for uid, amount in plan.items():
                cur = repo.get_user_level(account_id, group_id, uid, conn=conn)
                repo.set_user_level(account_id, group_id, uid,
                                    max(0, cur - amount), now_sec, conn=conn)

    return AffectionOutcome(
        interaction_type, calc.change if calc.can_change else 0,
        calc.can_change, calc.reason,
    )


# ===== 小工具 =====


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _as_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_int(v: Any, default: int) -> int:
    if isinstance(v, bool):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        # ``OverflowError``：``int(float("inf"))``。JSON 里 ``Infinity`` 是非法字面量，
        # 但 Python 的 ``json.load`` 认它 ⇒ 手改过的账号文件能把这里炸进连接路径。
        return default
    return n if math.isfinite(n) else default


def _as_float(v: Any, default: float) -> float:
    if isinstance(v, bool):
        return default
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return n if math.isfinite(n) else default
