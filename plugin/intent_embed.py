"""WPP intent-embed: 群/DM 消息 embedding 快路径定位相关历史候选。

迁移自 wpp-openclaw dispatch/intent-embed.ts。

功能:
- 用 embedding 算当前消息和历史消息的相似度
- 选 top-N (默认 5) 最相关的历史消息作为上下文注入
- 阈值过滤(默认 0.3)

实现: 用 hermes 内置 web_embedding proxy (如果有), 或 fallback 到简单的
Jaccard token 相似度(本地 TF-IDF)。
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Optional

log = logging.getLogger(__name__)


def _tokenize(text: str) -> list[str]:
    """简单分词: 中文按字, 英文按词。"""
    text = text.lower().strip()
    # 中文字符 + 英文/数字单词
    tokens = re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]', text)
    return [t for t in tokens if len(t) > 0]


def _jaccard(a: list[str], b: list[str]) -> float:
    """Jaccard 相似度。"""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def _tfidf_cosine(a: list[str], b: list[str], corpus_tokens: list[list[str]]) -> float:
    """简易 TF-IDF cosine 相似度。corpus_tokens 给全局 IDF 估计。"""
    if not a or not b:
        return 0.0
    # 构建 IDF
    N = len(corpus_tokens) + 1
    df = Counter()
    for doc in corpus_tokens:
        for w in set(doc):
            df[w] += 1
    # 当前 query 和 doc 的 TF
    def tfidf_vec(tokens: list[str]) -> dict[str, float]:
        tf = Counter(tokens)
        vec = {}
        for w, c in tf.items():
            idf = math.log((N + 1) / (df.get(w, 0) + 1)) + 1
            vec[w] = c * idf
        return vec

    va, vb = tfidf_vec(a), tfidf_vec(b)
    dot = sum(va.get(k, 0) * vb.get(k, 0) for k in set(va) | set(vb))
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    return dot / (na * nb) if na and nb else 0.0


def find_relevant_candidates(
    query: str,
    candidates: list[dict],
    top_n: int = 5,
    threshold: float = 0.3,
    use_tfidf: bool = True,
) -> list[dict]:
    """从 candidates 里挑出和 query 最相关的 top_n 条。

    candidates: list of dict, 至少含 'text' 字段
    返回: list of (candidate, score), 按 score 倒序
    """
    if not query or not candidates:
        return []

    q_tokens = _tokenize(query)
    corpus_tokens = [_tokenize(c.get("text", "")) for c in candidates]

    scored = []
    for i, cand in enumerate(candidates):
        text_tokens = corpus_tokens[i]
        if use_tfidf:
            score = _tfidf_cosine(q_tokens, text_tokens, corpus_tokens)
        else:
            score = _jaccard(q_tokens, text_tokens)
        if score >= threshold:
            scored.append((cand, score))

    scored.sort(key=lambda x: -x[1])
    return [c for c, _ in scored[:top_n]]


def is_embed_intent_enabled(acct_cfg: dict) -> bool:
    """检查 account 配置是否启用 embed intent。"""
    if acct_cfg is None:
        return False
    return bool(acct_cfg.get("embedIntentEnabled", True))


def get_embed_config(acct_cfg: dict) -> tuple[int, float]:
    """返回 (top_n, threshold)。"""
    return (
        int(acct_cfg.get("embedIntentTopN", 5)),
        float(acct_cfg.get("embedIntentThreshold", 0.3)),
    )