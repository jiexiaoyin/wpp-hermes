"""兼容层：实现已迁至 :mod:`plugin.neutral.rate_limit`（扩展边界 P0，2026-10-19）。

保留本路径是为了让 ``platform/adapter.py`` 与现有单测**零改动**；新代码请直接
``from ..neutral.rate_limit import ...``。
"""

from ..neutral.rate_limit import (  # noqa: F401
    GUARDED_REASONS,
    MAX_BUCKETS,
    MAX_TRIGGERS,
    MUTE_S,
    WINDOW_S,
    Decision,
    TriggerRateLimiter,
)

__all__ = [
    "GUARDED_REASONS",
    "MAX_BUCKETS",
    "MAX_TRIGGERS",
    "MUTE_S",
    "WINDOW_S",
    "Decision",
    "TriggerRateLimiter",
]
