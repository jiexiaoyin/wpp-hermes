"""WPP intent-llm: LLM 智能判断要注入哪些候选上下文。

迁移自 wpp-openclaw dispatch/intent-llm.ts。

功能:
- 给 LLM 一个简短的 prompt,让它从候选消息中选 top-N 最相关的
- 节省 model context, 提升 relevance

实现: 用 minimax-cn API (deepseek-v4-flash / M2.7-highspeed) 做 judgment。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

log = logging.getLogger(__name__)


_JUDGE_PROMPT = """你是 intent 路由器。从下面的 {n} 条候选消息里,选出与当前用户 query 最相关的 {k} 条。

【用户 query】
{query}

【候选消息】
{candidates}

只返回一个 JSON 数组,包含选中的 candidate_id (从 1 开始),不要其他文字:
[1, 3, 5]
"""


def _build_prompt(query: str, candidates: list[dict], k: int) -> str:
    lines = []
    for i, c in enumerate(candidates, start=1):
        text = (c.get("text") or "")[:200]
        sender = c.get("from_name", "?")
        lines.append(f"{i}. [{sender}] {text}")
    return _JUDGE_PROMPT.format(
        n=len(candidates),
        k=min(k, len(candidates)),
        query=query[:200],
        candidates="\n".join(lines),
    )


async def judge_intent_async(
    query: str,
    candidates: list[dict],
    top_n: int = 5,
    timeout_s: float = 5.0,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> list[int]:
    """调用 LLM 判断哪些候选最相关,返回候选 idx 列表(1-based)。

    失败时降级到 embed 相似度结果(由 caller 处理)。
    """
    if not query or not candidates:
        return []

    model = model or "deepseek-v4-flash"
    api_key = api_key or os.environ.get("MINIMAX_CN_API_KEY") or os.environ.get("MINIMAX_API_KEY") or ""
    if not api_key:
        log.debug("intent-llm: 无 LLM key, 跳过 LLM 判断")
        return []

    prompt = _build_prompt(query, candidates, top_n)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                "https://api.minimaxi.chat/v1/text/chatcompletion_v2",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 50,
                    "temperature": 0.0,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        log.warning("intent-llm LLM call failed: %s", e)
        return []

    # 解析 [1, 3, 5]
    m = re.search(r"\[[\d,\s]+\]", text)
    if not m:
        return []
    try:
        ids = json.loads(m.group(0))
        return [int(i) for i in ids if 1 <= int(i) <= len(candidates)]
    except Exception:
        return []


def is_llm_intent_enabled(acct_cfg: dict) -> bool:
    """检查 account 配置是否启用 LLM intent。"""
    if acct_cfg is None:
        return False
    return bool(acct_cfg.get("llmIntentEnabled", True))


def get_llm_intent_config(acct_cfg: dict) -> tuple[str, float]:
    """返回 (model, timeout_s)。"""
    return (
        str(acct_cfg.get("llmIntentModel", "deepseek-v4-flash")),
        float(acct_cfg.get("llmIntentTimeoutMs", 5000)) / 1000.0,
    )