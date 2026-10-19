"""兼容层：实现已迁至 :mod:`plugin.neutral.quote_ref`（扩展边界 P0，2026-10-19）。

本路径保留给 ``adapter.py`` / ``toolkit/msg.py`` / ``repo_messages.py`` 与旧 import；
新代码请直接 ``from ..neutral import quote_ref``。
"""

from ..neutral.quote_ref import (
    GROUP_SUFFIX,
    QuoteIdentity,
    build_mention_prefix,
    extract_group_sender_wxid,
    is_usable_sender,
    resolve_quote_identity,
)

__all__ = [
    "GROUP_SUFFIX",
    "QuoteIdentity",
    "build_mention_prefix",
    "extract_group_sender_wxid",
    "is_usable_sender",
    "resolve_quote_identity",
]
