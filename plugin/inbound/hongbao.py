"""红包 / 转账检测 —— 2026-09-27 生产事故的修复点（TS v1.9.1 + v1.9.2）。

迁移自 TS ``inbound/hongbao.ts``。**这是本移植里最需要逐字对齐的一个文件** ——
它焊死了一次"整条消息被静默丢掉且无任何报错"的事故。

## 事故回顾（2026-09-27，老板报"昨晚接龙没回复"）

链路是三件事叠在一起才炸的：

1. 群接龙文案**自带"红包"二字**（如「提升业绩, 领取红包🧧」/「红包100元」）；
2. 旧 ``is_red_packet_message`` 的启发式 ① 是**内容关键词**判定 ⇒ 接龙被认成红包；
3. ``should_process`` 里红包拦截排在**接龙之前**，且命中即 ``return (False, "red_packet")``
   —— 静默、无日志、无报错。

**实测（只读回放生产账本）**：接龙文案加上"红包"二字之后的那批接龙**条条零回复**，
此前不带"红包"的接龙**条条有回复** —— 相关性完美对应。而日志只留一行
``red packet detected: … url=missing``，**看着像"来了个没 url 的红包"**，
极易误诊成 vendor 侧问题（这就是它潜伏的原因）。

## 两处修复（缺一不可）

- **v1.9.1 接龙优先**：``is_relay_message(msg)`` 命中即返 ``False``（结构性识别
  比关键词可靠得多）。
- **v1.9.2 关键词只在卡片上采信**：真红包在本系统里是 **App 卡片（``msg_type=49``）**，
  正文由厂商译成"微信红包"四字；而**纯文本**里出现"红包"绝大多数是人在聊天
  （如"@某某 群收红包"/"抓紧领大红包"），拿关键词静默它们 = **把群友的话吞了**。

  **实测（只读回放生产账本）**：真红包**全部**是卡片形态（49），纯文本形态
  **一条没有** ⇒ 收窄 = **零漏判**，同时救回一批被误吞的群聊。

  ⚠️ 若将来厂商改用**纯文本**推红包卡片，这条注释就是排查入口 —— 症状会是
  "红包不静默了，bot 开始对着红包说话"。

## 启发式 2/3/4 照搬 TS

这三条是**结构性**信号（不依赖正文关键词），所以**不收窄**。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .parser import MT_APP
from .relay import is_relay_message

log = logging.getLogger(__name__)

#: 正文关键词（heuristic ①）—— **只在卡片上采信**
_HB_KEYWORD_RE = re.compile(r"红包|red.?packet", re.I)
#: 厂商私有 ``raw.type`` 形态
_HB_RAW_TYPE_RE = re.compile(r"hongbao|redpacket", re.I)
#: ``app.category`` 支付类（v1.3.72 转账/支付通知，老板 2026-08-20）
_PAY_CATEGORY_RE = re.compile(r"payment_notice|transfer|pay", re.I)
#: ``app.description`` 文案兜底
_PAY_DESC_RE = re.compile(r"转账|收款|transfer", re.I)

#: 微信原生红包 appmsg type（heuristic ②）
WX_RED_PACKET_APPMSG_TYPE = 2002


def is_card_message(msg: Any) -> bool:
    """是不是"卡片类" App 消息（``msg_type == 49``）。

    红包/转账/接龙/小程序/文件/链接 这些**被包装过**的消息都由 49 承载
    （正文里含 XML/摘要）；纯文本(1)/图片(3)/表情(47) 等不是卡片。

    **这个判定只在这里定义一次**（单一真源）—— 散在各处就会出现
    "两处对 49 的理解不一致"的漂移，而漂移的那一刻恰是事故。
    """
    try:
        return int(getattr(msg, "msg_type", 0)) == MT_APP
    except (TypeError, ValueError):
        return False


def is_red_packet_message(msg: Any) -> bool:
    """是不是红包/转账（**命中即静默，不触发 AI**）。

    判定顺序**不可调换**：接龙必须排在最前（v1.9.1），
    否则文案自带的"红包"二字会把接龙吃掉。
    """
    # ── v1.9.1 接龙优先 ────────────────────────────────────────────────
    if is_relay_message(msg):
        return False

    raw = getattr(msg, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    content = getattr(msg, "content", "") or ""

    # ── heuristic ①: 正文含"红包" —— v1.9.2 起**只在卡片上采信** ────────
    if _HB_KEYWORD_RE.search(content) and is_card_message(msg):
        return True

    # ── heuristic ②: raw.appMsg.type == 2002（微信原生红包）────────────
    app_msg = raw.get("appMsg") if isinstance(raw.get("appMsg"), dict) else {}
    if str(app_msg.get("type") or "") == str(WX_RED_PACKET_APPMSG_TYPE):
        return True

    # ── heuristic ③: raw.type 含 "hongbao"/"redpacket"（厂商私有）──────
    raw_type = raw.get("type")
    if isinstance(raw_type, str) and _HB_RAW_TYPE_RE.search(raw_type):
        return True

    # ── heuristic ④: 转账/支付通知（v1.3.72，老板 2026-08-20）──────────
    #   转账与红包同属支付类 app 消息，推送简化无 transFerId/transactionId ⇒ 不触发 AI
    #   ⚠️ ``description`` 兜底**嵌在 category 判断内**（与 TS 逐字一致）：只有
    #   ``app.category`` 是字符串时才可能命中 —— 无 category 的转账**照旧不被静默**。
    #   这是 TS 上已被记录的**既有缺口**（``hongbao-relay-priority.test.mjs`` 用例 2
    #   把它如实钉住），移植**照搬、不顺手扩大**：这里的每个 True 都在静默一条消息，
    #   多静默一条 = 吞群友的话 —— 与本模块历史上每一次事故同一类。宁可留着旧缺口，
    #   也不要悄悄扩大静默面（要补就该单独提、单独验，而不是混在一次移植里）。
    app = raw.get("app") if isinstance(raw.get("app"), dict) else {}
    category = app.get("category")
    if isinstance(category, str):
        if _PAY_CATEGORY_RE.search(category):
            return True
        desc = app.get("description")
        if isinstance(desc, str) and _PAY_DESC_RE.search(desc):
            return True

    return False


def extract_red_packet_info(msg: Any) -> dict:
    """从 raw 提取红包 ``{url, key}``（供 ``/Msg/OpenHongBao`` 用，**不自动拆**）。

    两条 vendor 路径都试；取不到就返空 dict（调用方只 log）。
    """
    raw = getattr(msg, "raw", None)
    raw = raw if isinstance(raw, dict) else {}

    hb = raw.get("hongbao")
    if isinstance(hb, dict) and isinstance(hb.get("url"), str) and isinstance(hb.get("key"), str):
        return {"url": hb["url"], "key": hb["key"]}

    app_msg = raw.get("appMsg")
    if isinstance(app_msg, dict):
        info = app_msg.get("hongbaoInfo")
        if isinstance(info, dict) and isinstance(info.get("url"), str) and isinstance(info.get("key"), str):
            return {"url": info["url"], "key": info["key"]}
    return {}


def process_red_packet(msg: Any) -> dict:
    """红包处理入口：**只 log**（默认不自动拆）。

    日志**必须区分"有 url"和"没 url"** —— 事故期间那行
    ``red packet detected: … url=missing`` 长得像 vendor 问题，
    实际是接龙被误判。现在补上 ``card=`` 与 ``type=`` 两个字段
    （卡片形态 + 原始 msgType），下次再出同类问题一眼能分辨。
    """
    if not is_red_packet_message(msg):
        return {}
    info = extract_red_packet_info(msg)
    log.info("[WPP] 红包/转账识别: account=%s peer=%s url=%s card=%s type=%s",
             getattr(msg, "account_id", ""), getattr(msg, "peer_id", ""),
             "present" if info.get("url") else "missing",
             is_card_message(msg), getattr(msg, "msg_type", ""))
    return info


__all__ = [
    "WX_RED_PACKET_APPMSG_TYPE",
    "extract_red_packet_info",
    "is_card_message",
    "is_red_packet_message",
    "process_red_packet",
]
