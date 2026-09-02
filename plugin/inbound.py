"""WPP 入站消息处理管线（迁移自 wpp-openclaw handler.ts + astrbot wpp_account.py）。

流程：extract → dedup → 过滤 → 白名单/trigger → 归一化 → 交给 adapter.handle_message
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict

from .message_parser import WppMessage, extract_all_msg_src, parse_message
from .triggers import normalize_mention, should_process

log = logging.getLogger(__name__)


class SeenTracker:
    """内存去重（LRU）。配合 DB UNIQUE 实现 at-least-once。"""

    def __init__(self, maxsize: int = 20000) -> None:
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._maxsize = maxsize

    def is_dup(self, key: str) -> bool:
        if key in self._seen:
            return True
        self._seen[key] = time.time()
        if len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return False


class InboundPipeline:
    """入站消息管线。"""

    def __init__(self, account_id: str, cfg: dict) -> None:
        self.account_id = account_id
        self.cfg = cfg
        self.seen = SeenTracker()

    def extract(self, payload: dict) -> list[WppMessage]:
        """从 WS/sync payload 提取并过滤原始消息。"""
        out: list[WppMessage] = []
        for src in extract_all_msg_src(payload):
            try:
                msg = parse_message(self.account_id, src)
            except Exception as e:  # noqa: BLE001
                log.warning("[WPP:%s] 解析消息失败: %s", self.account_id, e)
                continue
            if not msg:
                continue
            # 去重
            if msg.msg_id and self.seen.is_dup(f"{self.account_id}:{msg.msg_id}"):
                log.debug("[WPP:%s] 重复消息: %s", self.account_id, msg.msg_id)
                continue
            out.append(msg)
        return out

    def decide(self, msg: WppMessage) -> tuple[bool, str]:
        """判断是否触发 agent。"""
        ok, reason = should_process(msg, self.cfg)
        return ok, reason

    def prepare_text(self, msg: WppMessage) -> str:
        """生成发给 agent 的文本（去 @、带媒体标记）。"""
        text = msg.content
        text = normalize_mention(text, self.cfg.get("selfWxid", ""), self.cfg.get("nickname", ""))
        # 媒体消息注入标记（2026-09-01 补全：48 位置 / 10002 撤回 / 49 细分接龙·转账·应用）
        if msg.msg_type == 3:      # 图片
            text = f"[图片] {text}".strip()
        elif msg.msg_type == 34:   # 语音
            text = f"[语音] {text}".strip()
        elif msg.msg_type == 43:   # 视频
            text = f"[视频] {text}".strip()
        elif msg.msg_type == 49:   # APP_MSG：细分接龙/转账/应用消息/文件
            if "接龙" in text:
                text = f"[接龙] {text}".strip()
            elif "转账" in text or "收款" in text:
                text = f"[转账] {text}".strip()
            elif text.startswith("[应用消息]") or "应用消息" in text:
                text = f"[应用消息] {text}".strip()
            else:
                text = f"[文件] {text}".strip()
        elif msg.msg_type == 47:   # 表情
            text = f"[表情] {text}".strip()
        elif msg.msg_type == 42:   # 名片
            text = f"[名片] {text}".strip()
        elif msg.msg_type == 48:   # 位置
            text = f"[位置] {text}".strip()
        elif msg.msg_type == 10002:  # 撤回
            text = f"[撤回消息] {text}".strip()
        return text
