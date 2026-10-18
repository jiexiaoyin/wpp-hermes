"""WPP jargon 群黑话 —— **LLM 层**（迁移自 wpp-openclaw ``src/inbound/jargon.ts:340-729``）。

统计层（候选从哪来）在 :mod:`plugin.inbound.jargon`；本文件装 TS 的 ② ③ 两段：

* **② 批量验证**：一次 LLM 调用把普通词筛掉，只留真黑话（``build_validate_prompt``）；
* **③ 三步推断**：上下文推断 → 纯字面推断 → 对比（``infer_jargon_meaning``），
  差异大 ⇒ 是黑话，存含义。

再加两件配套：**消息历史缓冲**（``record_message`` 等，供 ③ 看上下文）与
**触发节流**（``should_trigger_mine``，间隔 + 新增消息数双闸）。

## 为什么 LLM 调用单独一层"薄"

``jargon_llm`` 只有一件事：把 prompt 交给 :func:`plugin.llm.judge.call_judge`
（py2 唯一的 LLM 出口，W2 已收口）。**不新增第二条 HTTP 路径**，也不自己拼请求体。
因此这里没有 TS 的 ``format ?? "anthropic"`` 那半：py2 的
:func:`plugin.llm.judge.resolve_judge_creds` **按"哪个 key 存在"**选端点与格式
（DEEPSEEK ⇒ openai / MINIMAX ⇒ anthropic），比 TS 按调用方给 ``format`` 更不容易配错
（记忆里的 judge 事故就是"按模型名分支，改名就换错端点"）。

## 四处需要记住的判断

1. **``model`` 缺失 ⇒ 抛、不降级**（``jargon.ts:543-548`` 逐字保留其理由）：
   TS 在 v1.4.0 12:28 老板拍板"消除 plugin hardcode"后把这里改成**直接抛错**，
   为的是逼出账面上的模型配置。抛出点**在任何 try 之外** ⇒ 整轮挖掘中止、
   由 fire-and-forget 的收尾回调记一行 warn（**每轮一次**，不是静默）。
   py2 的账号默认值里 ``model`` 是空串 ⇒ 没配就是这条路。
2. **没有 key ⇒ 静默返回 ``None``**（``:541``，在 ``model`` 检查**之前**）——
   与上一条刚好相反：没配 key 不算配置错误，是"这台机器压根没接 LLM"。
   顺序不能换：换了之后"没 key 的机器"会每轮挖一次报错。
3. **``JSON.parse`` 成功但结果不是数组时，**不**走 ``[...]`` 兜底**（``:569-590``）：
   TS 的兜底写在 ``catch`` 里 ⇒ 只有"解析抛了"才尝试抠方括号。照抄这条分支形状，
   别把它改成"先试解析、失败或非数组再抠"（那会让 ``{"a":1}`` 这类回包被抠出一个数组）。
4. **``o?.meaning`` 在非对象上是 ``undefined``** ⇒ py2 逐处 ``isinstance(o, dict)``
   是**同一件事**（可选链的 Python 写法），不是"多一层防御"。

## 没移植的一截（如实记下）

* **``cfg.whitelistGroups``**：TS 的 ``JargonConfig`` 声明了它，但 ``jargon.ts`` 里
  **一次都没读**（连 ``handler.ts`` 的调用点也不看它）⇒ py2 同样不读。
  **不顺手实现一个门**：那会造出"配置里写着的群被过滤了"这种 TS 没有的行为，
  而且方向是"少挖" —— 需要一个真门时应当单独提。
* **``hasTerm``/``has_jargon``**：TS 的 store 接口有 ``hasTerm`` 但挖掘流程只用
  ``listTerms`` 做排除（``:652-660``）⇒ py2 的 :class:`RepoJargonStore` 也只用
  ``list_jargon``；``repo_messages.has_jargon`` 保持零调用（与 TS 对称，不是漏接线）。
* **★ TS 的 store 从来没被注入过 ⇒ 它的"落库那半"是纯花钱的空转**（本项实测，
  记在这里因为它决定了 py2 这一处的行为**有意**与 TS 不同）：``JargonConfig.store``
  是可选字段，``defaultJargonConfig()``（``:365-376``）**没有** ``store``，
  ``resolveAiConfig``（``config-ai.ts:17-29``）只合并 ``model``/``timeoutMs``，
  全仓也没有第二个赋值点（``command grep -rn "listTerms|saveTerm"`` 只命中
  ``jargon.ts`` 与它的三个构建产物）⇒ 真机上 ``cfg.store === undefined``，
  ``if (cfg.store)`` 两道全不过 ⇒ **每一轮挖掘真花 1+15 次 LLM 调用、然后一条都不存**，
  返回值恒 ``{extracted: n, confirmed: 0}``。所以 ``t.content`` 那行在 TS 从未执行 ——
  这正是 store 契约里 ``content`` 与 py2 表列 ``term`` 的名字差**在 TS 侧永远暴露不出来**
  的原因（见 :meth:`RepoJargonStore.list_terms`）。
  py2 **有意**把 store 接上（计划原文："落库用现成 ``upsert_jargon``/``has_jargon``
  —— 现为零调用死码"）⇒ 这是**有意偏离 TS 的一处**，方向是"把 TS 只算不存的半截补完"。
* **``cfg.maxRetries`` / ``cfg.minJudgeIntervalSec``**：TS（``command grep -c`` = 0）与
  py2 **都没有**对应实现（``call_judge`` 自己不重试，也没有 judge 间隔闸）⇒ 死键。
  dev 的 ``boss2.example.json`` 里那两行**已删**（原因写进 ``_jargon_note``）；
  ⚠️ ``.183`` 上**已部署**的账号文件里可能还有（本轮之前的历史状态，无人读、无害）——
  别把它当成"py2 有这个功能"。**没有**在本项里凭空实现重试。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import jargon

log = logging.getLogger(__name__)

#: 每群保留的消息历史条数（``jargon.ts:380``）
MSG_HISTORY_MAX = 200

#: LLM 调用上限（``jargon.ts:555`` 的 ``maxTokens: 500``）
JARGON_MAX_TOKENS = 500

#: 每次最多送 LLM 验证几个候选（``:663`` 的 ``.slice(0, 15)``，控成本）
MAX_VALIDATE_TERMS = 15

#: 每次最多给几个确认候选做含义推断（``:676`` 的 ``.slice(0, 5)``，控成本）
MAX_INFER_TERMS = 5

#: 送 LLM 看上下文的消息条数（``:667``）
RECENT_MESSAGES_FOR_LLM = 30

#: 排除已确认词条时取多少条（``:654`` 的 ``listTerms(groupId, 100)``）
STORE_LIST_LIMIT = 100


class JargonConfigError(RuntimeError):
    """``jargon.model`` 未配置（``jargon.ts:544-548`` 的对等物）。

    **故意抛穿**而不是降级：这条是"配置没到位"，静默跳过等于让运维以为在挖。
    """


# ===== 配置 =====


@dataclass(frozen=True)
class JargonCfg:
    """``accounts/<id>.json`` 的 ``jargon`` 段解析结果。

    ⚠️ ``enabled`` 键缺席时是 ``False``（fail-closed，对齐 TS 的
    ``defaultJargonConfig():365-376`` —— 那里也是 ``enabled: false``，且
    ``openclaw.plugin.json`` 的 schema 描述写着"enabled=false (默认) 关"）。

    ⚠️ ``model`` **没有兜底值** —— 见 :class:`JargonConfigError`。
    ``timeoutMs``/``mineIntervalSec``/``minMessages``/``maxCandidatesPerGroup``
    的默认值与 TS 的 ``defaultJargonConfig`` 逐条相同（5000/60/10/50）。
    """

    enabled: bool = False
    model: str = ""
    timeout_ms: int = 5000
    mine_interval_sec: int = 60
    min_messages: int = 10
    max_candidates_per_group: int = 50

    @classmethod
    def from_raw(cls, cfg: Mapping[str, Any] | None) -> JargonCfg:
        """从账号 cfg 的 ``jargon`` 段解析（坏值一律退回默认，**不抛**）。"""
        d = cfg if isinstance(cfg, Mapping) else {}
        return cls(
            enabled=_as_bool(d.get("enabled"), False),
            model=str(d.get("model") or "").strip(),
            timeout_ms=_as_int(d.get("timeoutMs"), 5000),
            mine_interval_sec=_as_int(d.get("mineIntervalSec"), 60),
            min_messages=_as_int(d.get("minMessages"), 10),
            max_candidates_per_group=_as_int(d.get("maxCandidatesPerGroup"), 50),
        )


def resolve_jargon_cfg(raw: Mapping[str, Any] | None) -> JargonCfg:
    """``Account.jargon``（裸 dict，见 ``core/accounts.py``）→ :class:`JargonCfg`。

    与 W2/W3 同规矩：**这四个键名的解析只有这一处**，``adapter`` 里连字符串字面量
    都不许出现（有变异体钉着）—— 免得又出现"adapter 读 `mineInterval`、这里读
    `mineIntervalSec`"这种两边各自单测都绿的静默分叉。
    """
    return JargonCfg.from_raw(raw)


# ===== 消息历史缓冲（TS ``jargon.ts:379-412``）=====

_msg_history: dict[str, list[str]] = {}
#: **单调递增**消息计数 —— 供 :func:`should_trigger_mine` 用。
#: ⚠️ P1（TS ``:382-384``）：**不能用历史缓冲的长度**当"新增消息数" ——
#: 缓冲满 200 条之后长度恒为 200，"新增"恒等于 0 ⇒ 挖掘**永久停摆**。
_group_msg_counter: dict[str, int] = {}


def record_message(group_id: str, sender_id: str, content: str) -> None:
    """记一条消息（旁路，供 LLM 挖掘看上下文）。

    ⚠️ 空/纯空白正文**不记、也不计数**（TS ``:388`` 的早退在计数之前）——
    计数错一位就会让 ``minMessages`` 那道闸早/晚一拍。
    """
    if not (content or "").strip():
        return
    hist = _msg_history.setdefault(group_id, [])
    hist.append(f"{sender_id}: {content}")
    if len(hist) > MSG_HISTORY_MAX:
        _msg_history[group_id] = hist[-MSG_HISTORY_MAX:]
    _group_msg_counter[group_id] = _group_msg_counter.get(group_id, 0) + 1


def group_message_count(group_id: str) -> int:
    """群累计消息数（**单调递增**，只增不清）。"""
    return _group_msg_counter.get(group_id, 0)


def recent_messages(group_id: str, n: int) -> list[str]:
    """取最近 n 条消息文本。"""
    return list(_msg_history.get(group_id) or [])[-n:]


def reset_history() -> None:
    """测试/热重载：清空历史与计数。"""
    _msg_history.clear()
    _group_msg_counter.clear()


# ===== 触发节流（TS ``jargon.ts:614-639``）=====


@dataclass(frozen=True)
class _MineState:
    last_trigger_ts: int = 0
    last_msg_count: int = 0


_mine_states: dict[str, _MineState] = {}


def reset_mine_states() -> None:
    """测试/热重载：清空挖掘节流状态。"""
    _mine_states.clear()


def should_trigger_mine(group_id: str, cfg: JargonCfg, now_ms: float,
                        current_msg_count: int) -> bool:
    """是否该挖：距上次 ≥ :attr:`JargonCfg.mine_interval_sec` **且**新增 ≥
    :attr:`JargonCfg.min_messages`（``jargon.ts:629-639``）。

    ⚠️ **命中就记账**（写回 ``last_trigger_ts``/``last_msg_count``）—— 所以本函数
    **有副作用**，不是纯判据。调用方判完必须真的去挖，否则这一轮配额被白吃掉。
    ``now_ms`` 由调用方给（TS 也是传进来的），本函数**不读时钟** ⇒ 可确定地测。
    """
    st = _mine_states.get(group_id) or _MineState()
    interval_ms = cfg.mine_interval_sec * 1000
    if now_ms - st.last_trigger_ts < interval_ms:
        return False
    if current_msg_count - st.last_msg_count < cfg.min_messages:
        return False
    _mine_states[group_id] = _MineState(last_trigger_ts=int(now_ms),
                                        last_msg_count=current_msg_count)
    return True


# ===== 五个 prompt（``jargon.ts:420-521`` 逐字）=====


def build_extract_prompt(chat_text: str) -> str:
    """提取候选 prompt（``:420-452``）。**当前流程未调用** —— 保留它是为了与 TS 对齐：
    它的三步流程实际用的是"统计预筛 + 批量验证"，这个 prompt 是更早的"直接让 LLM 提"
    那条路（TS 侧也留着未调用）。"""
    return f"""请从下面这段聊天内容中提取"黑话/俚语/网络缩写"候选项。

**必须满足的条件（全部满足才提取）：**
- 是对话中真实出现过的短词或短语（2-8个字符）
- 是特定圈子/群组才会使用的词语，普通人看不懂的
- 脱离上下文后无法理解其含义

**严格排除以下内容（出现即跳过）：**
- @xxx、@某人 等 at 提及
- 人名、昵称、群名、ID
- 日常用语：吃饭、睡觉、上班、回家、好的、可以、谢谢 等
- 常见名词：手机、电脑、学校、公司、时间 等
- 语气词：哈哈、嗯嗯、啊啊、呵呵 等
- 表情描述：[图片]、[表情]、[语音] 等
- 纯数字、纯标点、URL链接
- 含义清晰明确的词语（即使不常见）

**黑话的典型特征：**
- 拼音首字母缩写：yyds、xswl、nbcs、zqsg
- 特定圈子内的暗语、缩写、谐音梗
- 群内独创的表达方式，外人无法理解

以 JSON 数组输出（严格按结构）：
[
  {{"content": "词条", "raw_content": "包含该词条的完整对话上下文原文"}}
]

如果没有找到符合条件的黑话，输出空数组 []

现在请输出：
{chat_text}"""


def build_validate_prompt(chat_text: str, candidates: Sequence[str]) -> str:
    """批量验证 prompt（``:458-474``）—— 一次调用筛掉普通词。"""
    return f"""下面是某群聊的对话片段和从中提取的候选词列表。

对话片段：
{chat_text}

候选词列表：
{json.dumps(list(candidates), ensure_ascii=False)}

请判断哪些候选词是该群特有的"黑话"（圈内用语/缩写/内部梗，外人看不懂）。
排除：普通日常用语、含义清晰明确的词、明显是常用词的。

只输出确认是黑话的词条数组（JSON），不要任何解释：
["词条1", "词条2", ...]

如果没有，输出 []"""


def build_infer_with_context_prompt(term: str, context: str) -> str:
    """三步推断 ①：上下文推断含义（``:479-492``）。"""
    return f"""以下是一个群聊中出现的词条和它出现的上下文。

词条：{term}
上下文：
{context}

请推断这个词条在该群中的含义。如果是黑话/缩写/圈内用语，给出可能的解释。

以 JSON 输出：
{{"meaning": "含义推断（如果是黑话）；如果不是黑话或无法确定，写空字符串", "no_info": true或false}}

"no_info" 为 true 表示无法从上下文推断出是黑话。"""


def build_infer_content_only_prompt(term: str) -> str:
    """三步推断 ②：仅凭词条推断（``:497-504``）。"""
    return f"""词条：{term}

请仅凭这个词条本身，推断它的字面含义（不依赖任何上下文）。

以 JSON 输出：
{{"meaning": "按字面/常规理解的解释"}}"""


def build_compare_prompt(term: str, ctx_meaning: str, literal_meaning: str) -> str:
    """三步推断 ③：对比两个推断（``:510-521``）—— 差异大 ⇒ 是黑话。"""
    return f"""词条：{term}

有两个推断：
推断1（群聊上下文中的含义）：{ctx_meaning or "（无法确定）"}
推断2（字面/常规含义）：{literal_meaning}

如果推断1和推断2含义不同（说明词条在群里被赋予了特殊含义，可能是黑话），返回 {{"is_similar": false}}
如果两者含义相同或接近（说明是常规用法），返回 {{"is_similar": true}}

只输出 JSON，不要解释。"""


# ===== 回包解析（``jargon.ts:568-610``）=====

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


def _strip_fence(text: str) -> str:
    """剥 ```` ```json … ``` ```` 围栏（``:572``/``:595``）。"""
    m = _FENCE_RE.search(text)
    return (m.group(1) if m else text).strip()


def extract_string_array(text: str) -> list[str]:
    """从 LLM 文本里抠 JSON 字符串数组（``:569-590``）。

    ⚠️ ``[...]`` 兜底**只在解析抛异常时**才走（TS 把它写在 ``catch`` 里）——
    解析成功但不是数组（``{"a":1}``）时 TS 直接返回 ``[]``。见模块 docstring ③。
    """
    if not text:
        return []
    s = _strip_fence(text.strip())
    try:
        arr = json.loads(s)
    except Exception:  # noqa: BLE001 —— 解析不了就走下面的兜底
        m = re.search(r"\[[\s\S]*\]", s)
        if not m:
            return []
        try:
            arr = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return []
    if isinstance(arr, list):
        return [x for x in arr if isinstance(x, str)]
    return []


def extract_json_object(text: str) -> Any:
    """从 LLM 文本里抠 JSON 对象（``:593-610``）。

    返回值**类型不强求是 dict**（TS 的 ``JSON.parse(s) as Record<...>`` 是个谎言式的
    类型断言，实际可能是数组/数字）⇒ 调用方一律用 ``isinstance(o, dict)`` 判，
    这与 TS 的可选链在非对象上得 ``undefined`` 等价（模块 docstring ④）。
    """
    if not text:
        return None
    s = _strip_fence(text.strip())
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return None


# ===== LLM 调用（``jargon.ts:525-566``）=====


async def jargon_llm(prompt: str, cfg: JargonCfg, creds: Any) -> str | None:
    """单次 LLM 调用，返回文本；**失败一律 ``None``**（TS 的 ``catch { return null }``）。

    两条早退的顺序与理由见模块 docstring ①②。

    ``creds`` 为空时在**本函数内**解析（``intent.py:388`` 的 ``creds or
    resolve_judge_creds()`` 是同款 house style）—— 调用方不必自己去碰 judge 模块。
    """
    from ..llm.judge import call_judge, resolve_judge_creds  # 延迟导入：模块级会拖进 aiohttp

    creds = creds or resolve_judge_creds()
    if not getattr(creds, "api_key", ""):
        return None
    if not cfg.model:
        raise JargonConfigError(
            "[WPP JARGON] 账号未配 jargon.model —— 拒绝凭空挑模型。"
            "请在 accounts/<id>.json 的 jargon 段写明 model（对齐 TS v1.4.0 12:28 "
            "老板拍板：消除 plugin hardcode）")

    try:
        return await call_judge(
            model=cfg.model,
            user_prompt=prompt,
            max_tokens=JARGON_MAX_TOKENS,
            timeout_ms=cfg.timeout_ms,
            creds=creds,
        )
    except Exception as exc:  # noqa: BLE001 —— 单次失败绝不许冒泡（TS 同款宽 catch）
        log.debug("[WPP JARGON] LLM 调用失败: %s", exc)
        return None


# ===== 三步推断（``jargon.ts:699-729``）=====


async def infer_jargon_meaning(term: str, context: str, cfg: JargonCfg,
                               creds: Any) -> str:
    """三步推断：上下文 vs 字面，差异大 ⇒ 是黑话（``:699-729``）。

    返回含义；``""`` 表示"不是黑话或无法确定"。三处降级各自回**上一步**的结果
    （``r2`` 失败 ⇒ 用上下文含义、``r3`` 失败 ⇒ 也用上下文含义），**不是**一路回空串。
    """
    # 第一步：上下文推断
    r1 = await jargon_llm(build_infer_with_context_prompt(term, context or "（无上下文）"),
                          cfg, creds)
    if r1 is None:
        return ""
    o1 = extract_json_object(r1)
    ctx_meaning = _obj_str(o1, "meaning")
    no_info = isinstance(o1, dict) and o1.get("no_info") is True

    # 第二步：纯字面推断
    r2 = await jargon_llm(build_infer_content_only_prompt(term), cfg, creds)
    if r2 is None:
        return ctx_meaning
    literal_meaning = _obj_str(extract_json_object(r2), "meaning")

    # 第三步：对比
    r3 = await jargon_llm(build_compare_prompt(term, ctx_meaning, literal_meaning), cfg, creds)
    if r3 is None:
        return ctx_meaning
    o3 = extract_json_object(r3)

    # ⚠️ 严格 `is False`（不是"假值"）：JSON 里的 0 与 false 在 TS 是 `0 === false` → false，
    #    py2 也必须是 `0 is False` → False。写成 `not o3.get(...)` 就会把 0 当"不同"。
    if isinstance(o3, dict) and o3.get("is_similar") is False:
        # 含义不同 → 黑话，用上下文含义（推不出来时给一个占位说明）
        return ctx_meaning or f"{term}（群内特有用法）"
    if no_info:
        return ""
    return ctx_meaning


def _obj_str(obj: Any, key: str) -> str:
    """``typeof o?.key === "string" ? o.key : ""`` 的对等物。"""
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, str):
            return v
    return ""


# ===== 落库（store 协议；``jargon.ts:356-362`` 的 ``cfg.store``）=====


class JargonStore(Protocol):
    """TS ``cfg.store`` 的对等物（只实现挖掘真用到的那两个方法）。"""

    def list_terms(self, group_id: str, limit: int = STORE_LIST_LIMIT) -> list[dict]: ...

    def save_term(self, *, group_id: str, content: str, raw_content: str,
                  meaning: str, is_jargon: bool) -> None: ...


class RepoJargonStore:
    """走 ``repo_messages`` 的 store（表 ``wpp_jargon_terms``）。

    ⚠️ 用**注入的** ``conn`` 时（真库探针那种场景），``plugin.data.db`` 与探针那份
    可能是两个模块实例 ⇒ 调用方显式传 ``conn``（W2 的 ``repo_affection`` 同一个坑）。
    """

    def __init__(self, account_id: str, conn: Any = None) -> None:
        self.account_id = account_id
        self._conn = conn

    def list_terms(self, group_id: str, limit: int = STORE_LIST_LIMIT) -> list[dict]:
        """⚠️ **这一层必须做列名映射**：``list_jargon`` 给的是 DB 行
        ``{group_id, term, meaning, frequency}``，而 store 契约（``jargon.ts:361``）
        声明的是 ``{content, meaning, isJargon}`` ⇒ ``term → content``。

        映射漏掉的后果是**静默的**：``mine_jargon_for_group`` 的 ``exclude`` 恒为空集
        ⇒ 已确认的词每一轮都重新送 LLM 验证、重新 ``upsert_jargon``（而那条 SQL 是
        ``frequency = frequency + 1``，于是词频被反复灌水）。更坏的是这一处**在 TS 侧
        永远看不出错** —— 它的 ``cfg.store`` 从未被注入（见模块 docstring「没移植的
        一截」），``t.content`` 那行在 TS 从来没执行过。py2 是第一个真的接上 store 的，
        所以这个契约缝隙在这里第一次现形。

        ``isJargon`` 不做映射：``list_jargon`` 的 SELECT 不含 ``is_jargon``，而
        **唯一**的消费点（``jargon.ts:654``）只读 ``content`` ⇒ 不为此改动数据层。
        """
        from ..data import repo_messages

        rows = repo_messages.list_jargon(self.account_id, group_id, limit, conn=self._conn)
        return [
            {"content": r.get("term") or "", "meaning": r.get("meaning") or ""}
            for r in rows
        ]

    def save_term(self, *, group_id: str, content: str, raw_content: str,
                  meaning: str, is_jargon: bool) -> None:
        from ..data import repo_messages

        repo_messages.upsert_jargon(
            self.account_id, group_id, content, raw_content, meaning,
            1 if is_jargon else 0, conn=self._conn)


# ===== 挖掘编排（``jargon.ts:645-696``）=====


@dataclass(frozen=True)
class MineResult:
    """``{extracted, confirmed}``（``:649`` 的返回型）。

    ⚠️ ``confirmed`` 是**存进库里的条数**（``saved``），不是"LLM 确认了几条"——
    TS 就是这么返回的（``:695``），而库写失败/没配 store 时它会小于
    ``len(confirmed)``。别按名字想当然。
    """

    extracted: int = 0
    confirmed: int = 0


async def mine_jargon_for_group(
    group_id: str, cfg: JargonCfg, creds: Any = None, store: JargonStore | None = None,
    *, now: float | None = None,
) -> MineResult:
    """挖一轮：统计候选 → LLM 批量验证 → 三步推断 → 落库（``:645-696``）。

    **静默失败**（无 key / 超时 / 坏 JSON / 库不可用）⇒ 不影响主流程；唯一的例外是
    :class:`JargonConfigError`（``model`` 没配），它**故意抛穿**给调用方记 warn。
    """
    # ① 统计候选（零 LLM）。已确认的词条要排除掉 —— 它取自**库**。
    exclude: set[str] = set()
    if store is not None:
        try:
            for t in store.list_terms(group_id, STORE_LIST_LIMIT):
                exclude.add(t.get("content") or "")
        except Exception as exc:  # noqa: BLE001 —— 库不可用不该让挖掘炸掉
            log.debug("[WPP JARGON] 读取已确认词条失败（继续挖）: %s", exc)

    candidates = [
        c for c in jargon.get_jargon_candidates(
            group_id, cfg.max_candidates_per_group, exclude, now=now)
        if not jargon.should_filter_candidate(c.term)
    ][:MAX_VALIDATE_TERMS]
    if not candidates:
        return MineResult()

    recent = "\n".join(recent_messages(group_id, RECENT_MESSAGES_FOR_LLM))

    # ② 批量验证（一次调用筛普通词）
    validate_res = await jargon_llm(
        build_validate_prompt(recent, [c.term for c in candidates]), cfg, creds)
    if validate_res is None:
        return MineResult()
    confirmed = extract_string_array(validate_res)

    by_term = {c.term: c for c in candidates}
    saved = 0
    # ③ 对确认的候选做含义推断（取前 5 个，控成本）
    for term in confirmed[:MAX_INFER_TERMS]:
        cand = by_term.get(term)
        ctx = (cand.context_examples[0] if cand and cand.context_examples else "")
        try:
            meaning = await infer_jargon_meaning(term, ctx, cfg, creds)
            if store is not None:
                store.save_term(group_id=group_id, content=term,
                                raw_content=ctx[:200], meaning=meaning, is_jargon=True)
                saved += 1
                # 缓存只在**落库成功之后**写：保证"缓存 ⊆ 库"（见 jargon.remember_meaning）。
                jargon.remember_meaning(group_id, term, meaning)
        except Exception as exc:  # noqa: BLE001 —— 单个词失败不影响其他（TS 同）
            log.debug("[WPP JARGON] 推断/落库失败 term=%s: %s", term, exc)

    log.info("[WPP JARGON] mined group=%s candidates=%d confirmed=%d saved=%d",
             group_id, len(candidates), len(confirmed), saved)
    return MineResult(extracted=len(candidates), confirmed=saved)


# ===== 值强制（house style：各模块自带一份小的）=====


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
    # ⚠️ Python 的 bool 是 int 的子类 ⇒ `True` 会被 int() 成 1（"真=1 秒间隔"）。
    #    与 W3 的 `groupContextWindow: true` 是同一个坑。
    if isinstance(v, bool):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return n if math.isfinite(n) else default


# 供测试/热重载一次性清空（本模块自己的那份）
def reset_all() -> None:
    reset_history()
    reset_mine_states()


__all__ = [
    "JARGON_MAX_TOKENS",
    "MSG_HISTORY_MAX",
    "JargonCfg",
    "JargonConfigError",
    "JargonStore",
    "MineResult",
    "RepoJargonStore",
    "build_compare_prompt",
    "build_extract_prompt",
    "build_infer_content_only_prompt",
    "build_infer_with_context_prompt",
    "build_validate_prompt",
    "extract_json_object",
    "extract_string_array",
    "group_message_count",
    "infer_jargon_meaning",
    "jargon_llm",
    "mine_jargon_for_group",
    "recent_messages",
    "record_message",
    "reset_all",
    "reset_history",
    "reset_mine_states",
    "resolve_jargon_cfg",
    "should_trigger_mine",
]
