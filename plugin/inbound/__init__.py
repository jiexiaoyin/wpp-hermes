"""入站消息层：解析 → 触发判定 → 正文准备。

## 为什么这个包曾经**整个不可达**（务必记住这个坑）

旧树上 ``plugin/inbound.py``（**文件**）与 ``plugin/inbound/``（**目录**）**同名共存**，
而目录里没有 ``__init__.py``。Python 的导入顺序里，**同名目录若无 ``__init__.py``
就不是包**，于是 ``import inbound`` 解析到的是那个**文件** —— 目录下的
``heartflow/`` 等模块全都取不到。

本包已按规则加 ``__init__.py``（常规包优先于同名的普通模块），
并**删掉了 ``plugin/inbound.py``**（内容已并入 :mod:`.parser`）。

## 子模块

============================  ==================================================
:mod:`.parser`                消息模型 / 提取 / 管线 / @提及
:mod:`.persist`               入站落库（对齐 TS ``enrich.ts``；入站/出站共用同一对
                              ``peer_kind``/``chat_id`` 映射，防两处各写一份漂移）
:mod:`.media`                 vendor 媒体字段考古（下载元数据）
:mod:`.relay`                 接龙识别（红包事故的第一道闸）
:mod:`.hongbao`               红包/转账识别（**v1.9.1 + v1.9.2 修复点**）
:mod:`.triggers`              触发判定（硬门 + 四路 OR）
:mod:`.stt`                   语音转写（vendor 转写优先，缺了才 silk→WAV→SiliconFlow）
:mod:`.jargon`                黑话词频统计（纯 stdlib，从旧 ``plugin/jargon.py`` 原样迁入）
:mod:`.heartflow`             心流九模块（见其 ``__init__``）
============================  ==================================================
"""

from .hongbao import (
    extract_red_packet_info,
    is_card_message,
    is_red_packet_message,
    process_red_packet,
)
from .jargon import (
    get_group_stats_summary,
    list_hot_jargon,
    query_jargon,
    reset_stats,
    tokenize,
    update_from_message,
)
from .media import extract_media_meta
from .persist import (
    db_chat_id,
    db_direction,
    db_peer_kind,
    persist_inbound,
    pick_new_msg_id,
    to_db_record,
)
from .parser import (
    MT_APP,
    MT_REVOKE,
    MT_SYSTEM,
    InboundPipeline,
    SeenTracker,
    WppMessage,
    extract_all_msg_src,
    extract_at_user_list,
    extract_msg_src,
    extract_quote_ref,
    is_bot_mentioned_by_text,
    normalize_mention,
    parse_message,
    strip_at_mentions,
    strip_group_prefix,
)
from .relay import is_relay_message
from .triggers import (FILEHELPER, is_filehelper_session, is_quoting_bot,
                       should_drop, should_process)

__all__ = [
    "FILEHELPER",
    "MT_APP",
    "MT_REVOKE",
    "MT_SYSTEM",
    "InboundPipeline",
    "SeenTracker",
    "WppMessage",
    "db_chat_id",
    "db_direction",
    "db_peer_kind",
    "extract_all_msg_src",
    "extract_at_user_list",
    "extract_media_meta",
    "extract_msg_src",
    "extract_quote_ref",
    "extract_red_packet_info",
    "get_group_stats_summary",
    "is_bot_mentioned_by_text",
    "is_card_message",
    "is_filehelper_session",
    "is_red_packet_message",
    "is_quoting_bot",
    "is_relay_message",
    "list_hot_jargon",
    "normalize_mention",
    "parse_message",
    "persist_inbound",
    "pick_new_msg_id",
    "process_red_packet",
    "query_jargon",
    "reset_stats",
    "should_drop",
    "should_process",
    "strip_at_mentions",
    "strip_group_prefix",
    "to_db_record",
    "tokenize",
    "update_from_message",
]
