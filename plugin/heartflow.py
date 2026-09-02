"""WPP heartflow 心流主动回复（迁移自 wpp-openclaw inbound/heartflow.ts）。

未 @ 机器人的群消息，用小模型 5 维打分决定是否主动参与。
judge 调 MiniMax Anthropic API，分数 ≥ threshold 则触发。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

# 默认配置（迁移自 defaultHeartflowConfig）
DEFAULT_WEIGHTS = {"relevance": 0.25, "willingness": 0.2, "social": 0.2, "timing": 0.15, "continuity": 0.2}
DEFAULT_THRESHOLD = 6.0
DEFAULT_CONTEXT_COUNT = 5
DEFAULT_TIMEOUT_MS = 5000
DEFAULT_MAX_RETRIES = 1

# 每群冷却状态
_last_judged: dict[str, float] = {}
_last_reply: dict[str, float] = {}


class HeartflowConfig:
    def __init__(self, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.independent_trigger = bool(cfg.get("independentTrigger", cfg.get("independent_trigger", False)))
        self.whitelist_groups = cfg.get("whitelistGroups") or cfg.get("whitelist_groups") or []
        self.model = cfg.get("model") or ""
        # 2026-09-01 fix: base_url 可配（deepseek 兜底时用 DeepSeek 端点，不再硬编码 MiniMax）
        self.base_url = cfg.get("baseUrl") or cfg.get("base_url") or ""
        self.timeout_ms = int(cfg.get("timeoutMs", cfg.get("timeout_ms", DEFAULT_TIMEOUT_MS)))
        self.max_retries = int(cfg.get("maxRetries", cfg.get("max_retries", DEFAULT_MAX_RETRIES)))
        self.threshold = float(cfg.get("threshold", DEFAULT_THRESHOLD))
        self.context_messages_count = int(cfg.get("contextMessagesCount", cfg.get("context_messages_count", DEFAULT_CONTEXT_COUNT)))
        self.min_reply_interval_sec = int(cfg.get("minReplyIntervalSec", cfg.get("min_reply_interval_sec", 0)))
        self.include_reasoning = bool(cfg.get("includeReasoning", cfg.get("include_reasoning", False)))
        w = cfg.get("weights") or {}
        self.weights = {
            "relevance": float(w.get("relevance", DEFAULT_WEIGHTS["relevance"])),
            "willingness": float(w.get("willingness", DEFAULT_WEIGHTS["willingness"])),
            "social": float(w.get("social", DEFAULT_WEIGHTS["social"])),
            "timing": float(w.get("timing", DEFAULT_WEIGHTS["timing"])),
            "continuity": float(w.get("continuity", DEFAULT_WEIGHTS["continuity"])),
        }


class JudgeResult:
    def __init__(self, should_reply: bool, overall_score: float, dimensions: dict, reasoning: str = "") -> None:
        self.should_reply = should_reply
        self.overall_score = overall_score
        self.dimensions = dimensions
        self.reasoning = reasoning


def _clamp_score(v) -> float:
    try:
        return max(0.0, min(10.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _build_prompt(chat_id: str, bot_nickname: str, content: str, sender_name: str,
                  chat_context: str, recent_messages: str, last_bot_reply: str,
                  seconds_since_last_reply: int, energy: float, cfg: HeartflowConfig) -> str:
    reasoning_part = ',\n    "reasoning": "详细分析原因，说明为什么应该或不应该回复，需要结合机器人角色特点进行分析，特别说明与上次回复的关联性，特别说明是否触发业务场景（晒单/出单/成交/突破/纪录等）"' if cfg.include_reasoning else ""
    last_reply = last_bot_reply or "暂无上次回复记录"
    since_min = f"{round(seconds_since_last_reply / 60)} 分钟前" if seconds_since_last_reply > 0 else "从未回复"

    # 2026-09-01 接总立·业务上下文（老板拍板撤回关键词方案 → 用 prompt 让心流理解零售业）
    # 老板的 5 号店群、移动业务对接群、华为群等都是零售业务工作群，晒单/出单/成交消息
    # 在零售业语境下 = 业务胜利 / 庆祝 / 正向情绪，应该主动祝福。LLM 没有业务上下文会误判
    # 这类消息为"普通消息"打低分。加这段让 judge 知道是工作群 + 识别业务胜利场景。
    business_context = """## 业务上下文（2026-09-01 老板拍板）
本群是「益融数码」零售业务工作群（手机数码连锁，江苏盱眙，7 个华为/OPPO 门店）。
群成员包括店长、店员、业务对接方。日常消息涉及：
- **业务胜利消息（应该回复）**：晒单、出单、成交、突破纪录、销售喜报、开张、战报、破万破千、接龙报到
  → 这类消息在零售业 = 业务进展 / 正向事件 / 团队胜利，**bot 应该主动庆祝、加油、互动**
- **普通工作沟通（可适度回复）**：库存、订单、客户、供货、维修、营业状态
- **闲聊/非业务（低相关性）**：中午吃啥、段子、表情包、无关话题

**特别说明**：
- "出3台nova13"= 卖出3台 = 业务胜利
- "晒单啦" / "报单" / "出货" / "成交" / "开张" / "战报" / "喜报" = 业务胜利
- "破万 / 破千" = 销售突破 = 业务胜利（大数字）
- 这些消息在零售业工作群里是**团队正反馈**，应该高分（relevance/social/continuity 给 7-10）
"""

    return f"""你是群聊机器人的决策系统，需要判断是否应该主动回复以下消息。

{business_context}

## 机器人角色设定
{('我是 ' + bot_nickname + '，一个群聊机器人助手。') if bot_nickname else "默认角色：智能助手"}

## 当前群聊情况
- 群聊ID: {chat_id}
- 我的精力水平: {energy:.1f}/1.0
- 上次发言: {since_min}

## 群聊基本信息
{chat_context}

## 最近{cfg.context_messages_count}条对话历史
{recent_messages}

## 待判断消息
发送者: {sender_name}
内容: {content}

## 判断要求
请基于以下五个维度打分（每个维度 0-10 分）：
1. relevance（相关性）：这条消息是否与我相关或值得我回复
   - **业务胜利消息**（晒单/出单/成交/破纪录）→ 7-10 分（必须回复）
   - **普通工作沟通** → 4-7 分
   - **闲聊/无关** → 0-3 分
2. willingness（意愿度）：我是否愿意回复这条消息
   - 业务胜利/团队正反馈 → 8-10 分（bot 应该主动庆祝）
   - 普通工作 → 5-7 分
   - 闲聊/无关 → 0-3 分
3. social（社交性）：回复是否会促进群聊氛围
   - 业务胜利时回复会鼓舞团队 → 8-10 分
   - 普通工作 → 5-7 分
   - 闲聊/无关 → 0-3 分
4. timing（时机）：现在回复是否合适
   - 业务胜利消息**越早回复越好**（趁热打铁）→ 8-10 分
5. continuity（延续性）：是否与之前的对话有延续性
   - 与之前的业务进展相关 → 7-10 分

**重要！！！请严格按照以下JSON格式回复，不要添加任何其他内容：**
{{
    "relevance": 0-10的数字,
    "willingness": 0-10的数字,
    "social": 0-10的数字,
    "timing": 0-10的数字,
    "continuity": 0-10的数字,
    "shouldReply": true或false,
    "overallScore": 0-10的数字{reasoning_part}
}}"""


async def judge_heartflow(chat_id: str, bot_nickname: str, content: str, sender_name: str,
                          chat_context: str, recent_messages: str, last_bot_reply: str,
                          seconds_since_last_reply: int, energy: float, cfg: HeartflowConfig,
                          api_key: str, base_url: str = "https://api.minimaxi.com/anthropic") -> Optional[JudgeResult]:
    """调 MiniMax API 做 5 维打分。返回 JudgeResult 或 None。"""
    if not api_key:
        log.warning("[WPP HEARTFLOW] missing MiniMax API key, skip judge")
        return None
    if not cfg.model:
        log.warning("[WPP HEARTFLOW] cfg.model 未配置，跳过 judge")
        return None

    base_url = base_url.rstrip("/")
    prompt = _build_prompt(chat_id, bot_nickname, content, sender_name, chat_context,
                           recent_messages, last_bot_reply, seconds_since_last_reply, energy, cfg)
    system_prompt = (
        "你是一个专业的群聊回复决策系统，能够准确判断消息价值和回复时机。\n"
        "你必须严格按照JSON格式返回结果，不要包含任何其他内容！请不要进行对话，只返回JSON！"
    )

    last_err = ""
    for attempt in range(cfg.max_retries + 1):
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    f"{base_url}/v1/messages",
                    json={
                        "model": cfg.model,
                        "max_tokens": 300,
                        "temperature": 0,
                        "system": system_prompt,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    headers={
                        "content-type": "application/json",
                        "anthropic-version": "2023-06-01",
                        "x-api-key": api_key,
                    },
                    timeout=aiohttp.ClientTimeout(total=cfg.timeout_ms / 1000),
                ) as resp:
                    if resp.status != 200:
                        err_text = (await resp.text())[:120]
                        last_err = f"HTTP {resp.status}: {err_text}"
                        continue
                    data = await resp.json()
                    content_parts = data.get("content") or []
                    text = ""
                    for part in content_parts:
                        if part.get("type") == "text":
                            text = part.get("text", "")
                            break
                    result = _parse_response(text, cfg)
                    if result:
                        return result
                    last_err = f"unparseable: {text[:80]}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    log.warning("[WPP HEARTFLOW] judge failed after %d attempts: %s", cfg.max_retries + 1, last_err)
    return None


def _parse_response(text: str, cfg: HeartflowConfig) -> Optional[JudgeResult]:
    """解析 LLM 返回的 JSON。"""
    # 提取 JSON（可能有 markdown 围栏）
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        cleaned = cleaned.strip().strip("json").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # 尝试提取花括号 JSON
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                return None
        else:
            return None

    dims = {
        "relevance": _clamp_score(data.get("relevance")),
        "willingness": _clamp_score(data.get("willingness")),
        "social": _clamp_score(data.get("social")),
        "timing": _clamp_score(data.get("timing")),
        "continuity": _clamp_score(data.get("continuity")),
    }
    overall = (
        dims["relevance"] * cfg.weights["relevance"]
        + dims["willingness"] * cfg.weights["willingness"]
        + dims["social"] * cfg.weights["social"]
        + dims["timing"] * cfg.weights["timing"]
        + dims["continuity"] * cfg.weights["continuity"]
    )
    should_reply = data.get("shouldReply")
    if should_reply is None:
        should_reply = overall >= cfg.threshold
    else:
        should_reply = bool(should_reply)
    return JudgeResult(should_reply=should_reply, overall_score=overall, dimensions=dims,
                       reasoning=str(data.get("reasoning", "")))


def check_gate(chat_id: str, content: str, cfg: HeartflowConfig, now_ms: int | None = None) -> tuple[bool, str]:
    """触发门禁（纯同步预筛，不调 LLM）。"""
    if not cfg.enabled:
        return False, "heartflow disabled"
    if cfg.whitelist_groups and chat_id not in cfg.whitelist_groups:
        return False, "not in whitelist"
    if not content:
        return False, "empty content"
    now = now_ms or int(time.time() * 1000)
    # 冷却期（P0-4.1 修复：min_reply_interval_sec=0 时给默认下限 15s，
    #   避免活跃群中心流对连续消息各自 judge+回复 → 刷屏 + API 费失控）
    interval = cfg.min_reply_interval_sec
    if interval <= 0:
        interval = 15
    last = _last_judged.get(chat_id, 0)
    if last and (now - last) < interval * 1000:
        return False, "cooling"
    return True, ""


def mark_judged(chat_id: str) -> None:
    _last_judged[chat_id] = int(time.time() * 1000)


async def try_independent_trigger(chat_id: str, content: str, sender_name: str,
                                  bot_nickname: str, cfg: HeartflowConfig,
                                  api_key: str, base_url: str = "https://api.minimaxi.com/anthropic",
                                  chat_context: str = "", recent_messages: str = "") -> dict:
    """完整心流触发流程（门禁 + judge）。返回 {triggered, reason, judgeResult?}。"""
    if not cfg.independent_trigger:
        return {"triggered": False, "reason": "independentTrigger disabled"}
    if not cfg.enabled:
        return {"triggered": False, "reason": "heartflow disabled"}
    if cfg.whitelist_groups and chat_id not in cfg.whitelist_groups:
        return {"triggered": False, "reason": "not in whitelist"}

    now = int(time.time() * 1000)
    ok, reason = check_gate(chat_id, content, cfg, now)
    if not ok:
        return {"triggered": False, "reason": f"gate:{reason}"}

    try:
        judge = await judge_heartflow(
            chat_id=chat_id, bot_nickname=bot_nickname, content=content, sender_name=sender_name,
            chat_context=chat_context, recent_messages=recent_messages,
            last_bot_reply="", seconds_since_last_reply=0, energy=1.0,
            cfg=cfg, api_key=api_key, base_url=base_url,
        )
        mark_judged(chat_id)
        if not judge:
            return {"triggered": False, "reason": "judge returned null"}
        return {
            "triggered": judge.should_reply,
            "reason": "judge.shouldReply=true" if judge.should_reply else f"score={judge.overall_score:.2f}<threshold",
            "judgeResult": judge,
        }
    except Exception as e:  # noqa: BLE001
        return {"triggered": False, "reason": f"judge threw: {e}"}
