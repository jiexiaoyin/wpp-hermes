"""WPP jargon 群黑话挖掘（迁移自 wpp-openclaw inbound/jargon.ts）。

自主学习：统计群内高频词 → 提供查询（AI 可查群黑话含义）。
词频统计纯规则；含义推断可选 LLM 增强。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

log = logging.getLogger(__name__)

MAX_CONTEXT_EXAMPLES = 10

# 网络常用但含义明确的词（不是黑话，跳过统计）
_COMMON_WORDS = {
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "这", "那", "就", "都", "也", "还", "很", "有", "没", "不", "好", "吗", "呢",
    "啊", "吧", "嗯", "哦", "哈", "哈哈", "呵呵", "嘿嘿",
    "hello", "hi", "ok", "好的", "可以", "谢谢", "感谢", "微信", "群", "消息",
}

_TOKEN_RE = re.compile(r"[一-鿿]{2,}|[a-zA-Z0-9_]{2,}")


def tokenize(text: str) -> list[str]:
    """提取候选词（中文 2 字以上 + 英文数字 2 字符以上）。"""
    if not text:
        return []
    # 去掉 @提及
    text = re.sub(r"@\S+", "", text)
    tokens = _TOKEN_RE.findall(text)
    return [t for t in tokens if t not in _COMMON_WORDS]


# P1-7.2 修复：内存上限（防 token 集合无限膨胀 OOM）
MAX_GLOBAL_TERMS = 20000      # 全局 token 数上限（跨群）
MAX_GROUP_TERMS = 5000        # 单群 token 数上限
_PRUNE_THRESHOLD = 3          # 裁剪时删除 <3 次的低频词


class JargonStats:
    def __init__(self) -> None:
        self.group_term_freq: dict[str, dict[str, int]] = {}      # group → term → count
        self.global_term_freq: dict[str, int] = {}                # term → count
        self.user_term_freq: dict[str, dict[str, dict[str, int]]] = {}  # group → term → user → count
        self.term_first_seen: dict[str, dict[str, float]] = {}    # group → term → ts
        self.term_contexts: dict[str, dict[str, list[str]]] = {}  # group → term → samples

    def _prune_if_needed(self) -> None:
        """容量超限时裁剪低频词（保留高频黑话，控制内存）。"""
        if len(self.global_term_freq) > MAX_GLOBAL_TERMS:
            # 裁剪 global：删除低频词
            low = [t for t, c in self.global_term_freq.items() if c < _PRUNE_THRESHOLD]
            for t in low:
                del self.global_term_freq[t]
            # 同步裁剪各群
            for g, gf in self.group_term_freq.items():
                for t in list(gf):
                    if t in low:
                        del gf[t]
                        self.user_term_freq.get(g, {}).pop(t, None)
                        self.term_first_seen.get(g, {}).pop(t, None)
                        self.term_contexts.get(g, {}).pop(t, None)
        for g, gf in self.group_term_freq.items():
            if len(gf) > MAX_GROUP_TERMS:
                low = [t for t, c in gf.items() if c < _PRUNE_THRESHOLD]
                for t in low:
                    del gf[t]
                    self.user_term_freq.get(g, {}).pop(t, None)
                    self.term_first_seen.get(g, {}).pop(t, None)
                    self.term_contexts.get(g, {}).pop(t, None)


_stats = JargonStats()


def reset_stats() -> None:
    global _stats
    _stats = JargonStats()


def update_from_message(content: str, group_id: str, sender_id: str) -> None:
    """统计一条群消息的词频。"""
    if not content or not group_id:
        return
    tokens = tokenize(content)
    if not tokens:
        return
    now = time.time()
    for token in tokens:
        gf = _stats.group_term_freq.setdefault(group_id, {})
        gf[token] = gf.get(token, 0) + 1
        _stats.global_term_freq[token] = _stats.global_term_freq.get(token, 0) + 1
        uf = _stats.user_term_freq.setdefault(group_id, {}).setdefault(token, {})
        uf[sender_id] = uf.get(sender_id, 0) + 1
        fs = _stats.term_first_seen.setdefault(group_id, {})
        if token not in fs:
            fs[token] = now
        ctx = _stats.term_contexts.setdefault(group_id, {}).setdefault(token, [])
        if len(ctx) < MAX_CONTEXT_EXAMPLES:
            ctx.append(content)
    _stats._prune_if_needed()  # P1-7.2：容量超限裁剪


def query_jargon(group_id: str, term: str, min_freq: int = 3) -> Optional[dict]:
    """查询某群某词的黑话信息。返回 {term, count, users, contexts, first_seen} 或 None。"""
    gf = _stats.group_term_freq.get(group_id, {})
    count = gf.get(term, 0)
    if count < min_freq:
        return None
    users = _stats.user_term_freq.get(group_id, {}).get(term, {})
    contexts = _stats.term_contexts.get(group_id, {}).get(term, [])
    return {
        "term": term,
        "count": count,
        "users": {uid: c for uid, c in users.items()},
        "contexts": contexts,
        "first_seen": _stats.term_first_seen.get(group_id, {}).get(term),
    }


def list_hot_jargon(group_id: str, limit: int = 10, min_freq: int = 3) -> list[dict]:
    """列出某群高频词（黑话候选）。"""
    gf = _stats.group_term_freq.get(group_id, {})
    hot = sorted(gf.items(), key=lambda x: -x[1])[:limit]
    out = []
    for term, count in hot:
        if count >= min_freq:
            out.append({"term": term, "count": count})
    return out


def get_group_stats_summary(group_id: str) -> str:
    """生成群黑话摘要（供 agent 参考）。"""
    hot = list_hot_jargon(group_id, 5)
    if not hot:
        return "（暂无黑话数据）"
    return "群黑话TOP: " + ", ".join(f"{t['term']}({t['count']}次)" for t in hot)
