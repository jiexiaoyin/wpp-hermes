"""兼容层：实现已迁至 :mod:`plugin.neutral.jargon`（扩展边界 P0，2026-10-19）。

统计层零 WPP 依赖，整体上提；本路径保留给 ``adapter.py`` / ``jargon_mine.py`` /
``jargon_tool.py`` 与旧 import。新代码请直接 ``from ..neutral import jargon``。

⚠️ 若需要 monkeypatch 模块级常量（例如测试里的 ``MAX_GLOBAL_TERMS``），必须
patch **中立模块** ``plugin.neutral.jargon`` —— 本兼容层里的同名常量只是导入时的
快照，改它不会影响实现函数读取的全局。
"""

from ..neutral.jargon import (  # noqa: F401  # noqa: F401
    _PURE_DIGITS_RE,
    BURST_AGE_CAP_DAYS,
    COMMON_BLOCK,
    HARD_BLOCK_RE,
    JIEBA_STANDARD_FREQ,
    MAX_CONTEXT_EXAMPLES,
    MAX_GLOBAL_TERMS,
    MAX_GROUP_TERMS,
    MIN_FREQUENCY,
    MIN_TERM_LENGTH,
    NGRAM_MAX,
    STOPWORDS,
    WEIGHT_BURST,
    WEIGHT_CONCENTRATION,
    WEIGHT_IDF,
    JargonCandidate,
    JargonStats,
    _js_round,
    _stats,
    build_jargon_channel_prompt,
    calc_burst_score,
    get_group_stats,
    get_group_stats_summary,
    get_jargon_candidates,
    is_standard_vocabulary,
    known_meaning,
    list_hot_jargon,
    ngram_tokens,
    query_jargon,
    remember_meaning,
    reset_group_jargon,
    reset_stats,
    should_filter_candidate,
    tokenize,
    update_from_message,
)
