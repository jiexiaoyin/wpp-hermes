"""WPP 消息触发判断（迁移自 astrbot _is_allowed + wpp-openclaw triggers.ts）。

白名单 + 群策略 + @触发。
"""
from __future__ import annotations

import logging
import re

from .message_parser import WppMessage

log = logging.getLogger(__name__)


def is_red_packet_message(msg: WppMessage) -> bool:
    """检测红包/转账消息（迁移自 wpp hongbao.ts isRedPacketMessage）。

    红包/转账不触发 AI 回复（静默处理）。
    """
    content = msg.content or ""
    if "红包" in content or "red" in content.lower() or "redpacket" in content.lower():
        return True
    raw = msg.raw or {}
    # 微信原生红包 appmsg.type=2002
    app_msg = raw.get("appMsg") if isinstance(raw.get("appMsg"), dict) else None
    if app_msg and str(app_msg.get("type")) == "2002":
        return True
    # vendor 私有 type
    if isinstance(raw.get("type"), str) and "hongbao" in raw["type"].lower():
        return True
    # 转账/支付通知
    app = raw.get("app") if isinstance(raw.get("app"), dict) else None
    if app:
        category = str(app.get("category") or "")
        desc = str(app.get("description") or "")
        if "payment" in category.lower() or "transfer" in category.lower() or "转账" in desc or "收款" in desc:
            return True
    return False


def should_process(msg: WppMessage, cfg: dict) -> tuple[bool, str]:
    """判断消息是否应触发 agent。返回 (是否处理, 原因/标记)。

    cfg 字段: allowFrom, groupPolicy, groupAllowFrom, requireAtMention, blacklistGroups

    触发逻辑（2026-09-01 老板拍板·简洁版）:
      @机器人 → 100% 必须回复（无需心流判断）
      接龙消息（APP_MSG=49 或含"接龙"）→ 100% 必须回复（无需心流判断）
      其他群消息 → 交给心流（heartflow）判断是否参与
      2026-09-01 老板拍板撤回：原"业务关键词 100% 触发"硬编码 15 个关键词方案
      （晒单/出单/开张/收米/报单/战报/喜报/成交/破万/破千/开单/接单/出机/出货/爆单）
      误触风险高、表达方式多变、违背"让心流 LLM 理解语义"的原意。
      改用方案 A：关键词全删，由心流 judge + 业务 prompt 上下文判断晒单消息。
    """
    self_wxid = cfg.get("selfWxid") or ""
    self_nickname = cfg.get("nickname") or ""
    from_wxid = msg.from_wxid
    content = msg.content
    chatroom_id = msg.chatroom_id

    # 红包/转账静默（不触发 AI）
    if is_red_packet_message(msg):
        return False, "red_packet"

    # 撤回消息静默（2026-09-01：人撤回了不需要 AI 响应）
    try:
        if int(msg.msg_type) == 10002:
            return False, "revoke"
    except (TypeError, ValueError):
        pass

    # 公众号
    if from_wxid.startswith("gh_"):
        return False, "official_account"

    # filehelper 命令单独处理（P1-2.3 修复：提到 self 之前。
    #   filehelper 会话的发送者可能是机器人自己（vendor 把文件传输助手消息标为 self），
    #   若在 self 判定之后，命令永远被拦截。对齐 OpenClaw 顺序。）
    if msg.to_wxid == "filehelper" or msg.peer_id == "filehelper":
        return True, "filehelper"

    # 自己发的不处理
    if self_wxid and from_wxid == self_wxid:
        return False, "self"

    if msg.is_group:
        # 群黑名单
        blacklist = cfg.get("blacklistGroups") or []
        if chatroom_id and chatroom_id in blacklist:
            return False, "group_blacklist"

        group_policy = cfg.get("groupPolicy") or "open"
        if group_policy == "disabled":
            return False, "group_disabled"
        if group_policy == "closed":
            return False, "group_closed"

        group_allow = cfg.get("groupAllowFrom") or []
        if group_policy == "allowlist" and group_allow:
            if chatroom_id and chatroom_id not in group_allow:
                return False, "group_not_allowlisted"

        # 1) @机器人 → 100% 必须回复
        if _is_mentioned(content, self_wxid, self_nickname):
            return True, "group_mention"

        # 2) 接龙消息 → 100% 必须回复（内置规则，无需配置；老板 2026-09-01 拍板）
        if _is_solitaire(msg):
            return True, "group_solitaire"

        # 注: 2026-09-01 老板撤回"业务关键词 100% 触发"硬编码方案 — 误触高、违背心流原意。
        # 晒单类业务消息走心流 judge + 业务 prompt 上下文判断（heartflow.py JUDGE_SYSTEM）。

        # 3) 其他 → 交给心流判断（heartflow judge 决定是否参与）
        return False, "heartflow_candidate"

    # 私聊
    allow_from = cfg.get("allowFrom") or []
    allow_all = cfg.get("allowAllUsers") or False
    if allow_all:
        return True, "dm"
    if allow_from:
        if from_wxid in allow_from:
            return True, "dm"
        return False, "dm_not_allowlisted"
    # 空白名单 = fail-closed（默认拒绝所有 DM）
    return False, "dm_fail_closed"


def _is_mentioned(content: str, self_wxid: str, self_nickname: str) -> bool:
    """判断内容是否 @ 了机器人（wxid 或昵称）。"""
    if not content:
        return False
    if "@" not in content:
        return False
    if self_wxid and (f"@{self_wxid}" in content or f"{self_wxid}@" in content):
        return True
    if self_nickname and (f"@{self_nickname}" in content or self_nickname in content):
        return True
    return False


# 2026-09-01 接总立·简洁版：接龙消息 100% 必须回复（内置规则，无需配置）
def _is_solitaire(msg: WppMessage) -> bool:
    """接龙消息检测：content 含"接龙"（[接龙] 或 #接龙 均命中）。

    2026-09-01 fix: 不用 msg_type==49 判断 — 华为群实测 49 含 转账86/应用消息44/接龙135+，
    msg_type==49 会过度触发（转账/应用消息也触发）。只按"接龙"字样，精确命中真接龙。
    """
    return "接龙" in (msg.content or "")


# 2026-09-01 迁移自 gewe triggers.ts（能力保留，should_process 暂用内置 _is_solitaire/@mention；
# 未来要"可配置消息类型/关键词/引用触发"时，把下面函数接回 should_process 即可）
def matches_msg_type(msg: WppMessage, cfg: dict) -> bool:
    """消息类型触发：顶层 msgType 或 appMsgTypes 命中（如接龙 APP_MSG=49）。

    cfg: {enabled, msgTypes: [int|str], appMsgTypes: [int]}
    wpp 的接龙/链接/小程序是 msg_type=49（APP_MSG），content 已是纯文本。
    """
    if not cfg or not cfg.get("enabled"):
        return False
    msg_types = cfg.get("msgTypes") or []
    app_msg_types = cfg.get("appMsgTypes") or []
    if msg_types and int(msg.msg_type) in {int(x) for x in msg_types}:
        return True
    if app_msg_types and int(msg.msg_type) in {int(x) for x in app_msg_types}:
        return True
    return False


def matches_keyword(content: str, cfg: dict, group_id: str) -> bool:
    """关键词触发：白/黑名单 + matchMode(substring/exact/regex)。

    cfg: {enabled, matchMode, caseSensitive, stripAtMention, keywords, whitelistGroups, blacklistGroups}
    群黑名单一票否决，优先于白名单。
    """
    if not cfg or not cfg.get("enabled"):
        return False
    keywords = cfg.get("keywords") or []
    if not keywords:
        return False
    if group_id and group_id in (cfg.get("blacklistGroups") or []):
        return False
    wl = cfg.get("whitelistGroups") or []
    if wl and (not group_id or group_id not in wl):
        return False
    text = content or ""
    if cfg.get("stripAtMention", True):
        text = text.replace("@", "")
    if not text:
        return False
    case_sensitive = bool(cfg.get("caseSensitive", False))
    if not case_sensitive:
        text = text.lower()
    match_mode = cfg.get("matchMode", "substring")
    for raw_kw in keywords:
        if not raw_kw:
            continue
        kw = raw_kw if case_sensitive else raw_kw.lower()
        if match_mode == "exact":
            if text.strip() == kw.strip():
                return True
        elif match_mode == "regex":
            try:
                if re.search(kw, text, 0 if case_sensitive else re.I):
                    return True
            except re.error:
                continue
        else:
            if kw in text:
                return True
    return False


def _is_quoting_bot(msg: WppMessage, self_wxid: str) -> bool:
    """是否引用了 bot 的消息（reply_to.from_wxid == self_wxid）。"""
    if not self_wxid:
        return False
    rt = msg.reply_to or {}
    return rt.get("from_wxid") == self_wxid


def normalize_mention(content: str, self_wxid: str, self_nickname: str) -> str:
    """去掉内容里的 @机器人 前缀，避免 AI 重复回 @。"""
    c = content
    if self_wxid:
        c = c.replace(f"@{self_wxid}", "").replace(f"{self_wxid}@", "")
    if self_nickname:
        c = c.replace(f"@{self_nickname}", "")
    return c.strip()
