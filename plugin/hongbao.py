"""WPP hongbao (红包) 事件处理。

迁移自 wpp-openclaw inbound/hongbao.ts。

微信红包消息特征:
- type=2002 (appMsg.type) 或内容含"红包/redpacket"
- hongbaoInfo / raw.hongbao 含 url + key
- shouldOpen=False (老板要红包不自动拆, 只记录 + 通知)

老板目前选择: 红包只记录不发自动拆。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

log = logging.getLogger(__name__)

_HB_PATTERN = re.compile(r"红包|red.?packet", re.IGNORECASE)
_HB_TYPE_IDS = {2002, "2002"}
_PAY_CATEGORIES = {"payment_notice", "transfer", "pay"}
_PAY_DESC_PATTERN = re.compile(r"转账|收款|transfer", re.IGNORECASE)


def is_redpacket_message(msg) -> bool:
    """检测消息是否为红包。"""
    try:
        content = getattr(msg, "content", None) or ""
        if isinstance(content, str) and _HB_PATTERN.search(content):
            return True
        raw = getattr(msg, "raw", None) or {}
        if not isinstance(raw, dict):
            return False
        app_msg = raw.get("appMsg") or {}
        if app_msg.get("type") in _HB_TYPE_IDS:
            return True
        rt = raw.get("type")
        if isinstance(rt, str) and _HB_PATTERN.search(rt):
            return True
        app = raw.get("app") or {}
        cat = app.get("category")
        if isinstance(cat, str) and cat in _PAY_CATEGORIES:
            return True
        desc = app.get("description")
        if isinstance(desc, str) and _PAY_DESC_PATTERN.search(desc):
            return True
        return False
    except Exception as e:  # noqa: BLE001
        log.debug("[WPP] hongbao 检测异常: %s", e)
        return False


def extract_redpacket_info(msg) -> dict:
    """提取红包 url/key (vendor 字段, 用于拆包)."""
    raw = getattr(msg, "raw", None) or {}
    if not isinstance(raw, dict):
        return {"shouldOpen": False}
    hb = raw.get("hongbao") or {}
    if isinstance(hb, dict) and hb.get("url") and hb.get("key"):
        return {"url": hb["url"], "key": hb["key"], "shouldOpen": False}
    app_msg = raw.get("appMsg") or {}
    if isinstance(app_msg, dict):
        info = app_msg.get("hongbaoInfo") or {}
        if isinstance(info, dict) and info.get("url") and info.get("key"):
            return {"url": info["url"], "key": info["key"], "shouldOpen": False}
    return {"shouldOpen": False}


def record_redpacket(account_id: str, peer_id: str, from_wxid: str, msg) -> Optional[int]:
    """红包入库 (wpp_redpackets 表, 供后续分析).

    Returns: insert id, or None on failure.
    """
    try:
        from . import db as _db
        info = extract_redpacket_info(msg)
        import json
        return _db.record_redpacket(
            account_id=account_id,
            peer_id=peer_id,
            from_wxid=from_wxid,
            hb_url=info.get("url", ""),
            hb_key=info.get("key", ""),
            raw_json=json.dumps(getattr(msg, "raw", {}) or {}, ensure_ascii=False)[:4000],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] record_redpacket 失败: %s", e)
        return None