"""触发判定 —— legacy ``triggers.py`` 的硬门 + TS ``triggers.ts`` 的四路 OR。

## 为什么合成**一个**入口而不是两个函数

TS 有 ``shouldTrigger``（四路 OR + 策略门），legacy Python 有 ``should_process``
（红包/撤回/公众号/filehelper 硬门 + 群/私策略 + 内置 @与接龙）。
两者**不是替代关系，是叠加关系**：

- legacy 的硬门是**安全静音**（红包/转账/撤回/公众号消息本就不该触发 AI）；
- TS 的四路 OR 是**「凭什么触发」**（@ / msgType / 引用bot / 关键词）。

保留两个函数各写一份策略 = 迟早漂移，而漂移的那一刻恰是"看起来正常但已经不工作了"
（这正是计划里 I4 的精神：单一真源）。所以这里**只有一个** :func:`should_process`，
内部按固定顺序串起来，reason 字符串保留 legacy 的取值（日志与回归测试都依赖它）。

## 判定顺序（**不可随意调换**）

======================================  ==================  ==========================
① 红包/转账（见 :mod:`inbound.hongbao`）  ``red_packet``      **必须最先** —— 09-27 事故
② 撤回 (``msg_type == 10002``)           ``revoke``
③ 公众号 (``gh_`` 前缀)                  ``official_account``
④ filehelper                             ``filehelper``      **必须在 self 之前**
⑤ 自己发的                                ``self``
────────────────────────────────────  群聊 ──────────────
⑥ 群黑名单 / 策略关 / 白名单外             ``group_*``
⑦ @机器人                                ``group_mention``   100% 必回
⑧ 接龙                                   ``group_solitaire`` 100% 必回
⑨ msgType / 引用bot / 关键词（可选配置）    ``group_msg_type`` / ``group_quote_bot`` / ``group_keyword``
⑩ 其余                                   ``heartflow_candidate``（交心流 judge）
────────────────────────────────────  私聊 ──────────────
⑪ 白名单内 / ``allowAllUsers``            ``dm``
⑫ 白名单外 / 空白名单（fail-closed）       ``dm_not_allowlisted`` / ``dm_fail_closed``
======================================  ==================  ==========================

第 ④ 步的**位置**是历史修复（P1-2.3）：filehelper 会话的发送者常被 vendor 标成
**机器人自己**，若排在 self 之后，filehelper 命令**永远被拦截**。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .hongbao import is_red_packet_message
from .parser import MT_REVOKE, WppMessage, is_bot_mentioned_by_text

log = logging.getLogger(__name__)

#: 文件传输助手的会话标识（vendor 用这个字面量当 wxid）。
FILEHELPER = "filehelper"


def is_filehelper_session(msg: WppMessage) -> bool:
    """是不是「发给文件传输助手」的会话 —— filehelper 管理命令的**唯一判据**。

    单独抽出来是因为它有两个消费者：本模块的 ④ 硬门（**判定**）与
    ``platform.adapter._maybe_command``（**处置**，把命令交给
    ``platform.commands.handle_command`` 并就地回复）。两处各写一份
    ``to_wxid == "filehelper" or peer_id == "filehelper"`` 就是 I4 要防的那种漂移。
    """
    return msg.to_wxid == FILEHELPER or msg.peer_id == FILEHELPER


# ------------------------------------------------------------------ 公开入口


def should_process(msg: WppMessage, cfg: dict) -> tuple[bool, str]:
    """判断消息是否应触发 agent。返回 ``(是否处理, 原因)``。

    ``cfg`` 字段：``selfWxid`` / ``nickname`` / ``blacklistGroups`` / ``groupPolicy``
    / ``groupAllowFrom`` / ``allowFrom`` / ``allowAllUsers`` / ``msgTypeTrigger``
    / ``keywordTrigger`` / ``quoteBotTrigger``。

    触发逻辑（2026-09-01 老板拍板·简洁版，**保留**）::

        @机器人      → 100% 必须回复（无需心流判断）
        接龙消息      → 100% 必须回复（无需心流判断）
        其他群消息    → 交给心流（heartflow）判断是否参与

    ⚠️ 2026-09-01 老板**已撤回**原"业务关键词 100% 触发"的 15 个硬编码关键词
    方案（晒单/出单/开张/收米/报单/战报/喜报/成交/破万/破千/开单/接单/出机/
    出货/爆单）—— 误触风险高、表达方式多变、违背"让心流 LLM 理解语义"的原意。
    改由心流 judge + 业务 prompt 上下文判断晒单消息。**别再把那 15 个词加回来。**
    """
    self_wxid = cfg.get("selfWxid") or ""
    self_nickname = cfg.get("nickname") or ""
    content = msg.content or ""

    # ① 红包/转账静默（**最先** —— 09-27 事故：排在接龙前会把接龙吃掉）
    if is_red_packet_message(msg):
        return False, "red_packet"

    # ② 撤回消息静默（2026-09-01：人撤回了不需要 AI 响应）
    try:
        if int(msg.msg_type) == MT_REVOKE:
            return False, "revoke"
    except (TypeError, ValueError):
        pass

    # ③ 公众号
    if msg.from_wxid.startswith("gh_"):
        return False, "official_account"

    # ④ filehelper（**必须在 self 之前** —— 见模块 docstring）
    if is_filehelper_session(msg):
        return True, "filehelper"

    # ⑤ 自己发的不处理（防 vendor 回推 self → 自问自答）
    if self_wxid and msg.from_wxid == self_wxid:
        return False, "self"

    if msg.is_group:
        return _decide_group(msg, cfg, content, self_wxid, self_nickname)
    return _decide_dm(msg, cfg)


# ---------------------------------------------------------------------- 群聊


def _decide_group(msg: WppMessage, cfg: dict, content: str,
                  self_wxid: str, self_nickname: str) -> tuple[bool, str]:
    chatroom_id = msg.chatroom_id

    # ⑥ 黑名单 / 策略 / 白名单
    if chatroom_id and chatroom_id in (cfg.get("blacklistGroups") or []):
        return False, "group_blacklist"

    policy = cfg.get("groupPolicy") or "open"
    if policy == "disabled":
        return False, "group_disabled"
    if policy == "closed":
        return False, "group_closed"
    if policy == "allowlist":
        allow = cfg.get("groupAllowFrom") or []
        # P0-2（2026-08-23）：**空列表 = 拒绝所有群**（fail-closed，与 DM allowFrom 语义对齐）。
        #   旧行为是空列表放行全部 —— 管理员清空白名单"暂时禁用"反而全放行，静默隐患。
        if not allow or chatroom_id not in allow:
            return False, "group_not_allowlisted"

    # ⑦ @机器人 → 100% 必回
    if is_bot_mentioned_by_text(content, self_wxid, self_nickname):
        return True, "group_mention"

    # ⑧ 接龙 → 100% 必回（内置规则，无需配置；老板 2026-09-01 拍板）
    if _is_solitaire(msg):
        return True, "group_solitaire"

    # ⑨ 可选配置的三路（**默认全关**，配了才生效）
    if _matches_msg_type(msg, cfg.get("msgTypeTrigger") or {}, chatroom_id):
        return True, "group_msg_type"
    if is_quoting_bot(msg, self_wxid) and (cfg.get("quoteBotTrigger") or {}).get("enabled"):
        return True, "group_quote_bot"
    if _matches_keyword(content, cfg.get("keywordTrigger") or {}, chatroom_id):
        return True, "group_keyword"

    # ⑩ 其余 → 交心流 judge 决定
    return False, "heartflow_candidate"


def _is_solitaire(msg: WppMessage) -> bool:
    """接龙检测：正文含"接龙"（``[接龙]`` / ``#接龙`` 均命中）。

    ⚠️ **2026-09-01 fix 必须保留：不用 ``msg_type == 49`` 判接龙。**
    华为群实测 49 里含 转账 86 / 应用消息 44 / 接龙 135+ ——
    按类型判会把转账和应用消息**一起**当成接龙触发。只认"接龙"字样，精确命中。
    """
    return "接龙" in (msg.content or "")


# ------------------------------------------------------------------ 私聊


def _decide_dm(msg: WppMessage, cfg: dict) -> tuple[bool, str]:
    if cfg.get("allowAllUsers") is True:
        return True, "dm"
    allow_from = cfg.get("allowFrom") or []
    if allow_from:
        return (True, "dm") if msg.from_wxid in allow_from else (False, "dm_not_allowlisted")
    # 空白名单 = fail-closed（默认拒绝所有 DM，防 P0 联系人 fan-out 重演）
    return False, "dm_fail_closed"


# ------------------------------------------------------- 可选的路由（默认关）


def _matches_msg_type(msg: WppMessage, trig: dict, chatroom_id: str) -> bool:
    """``msgTypeTrigger``：顶层 ``msgType`` 或 ``appMsgTypes`` 命中。

    ``trig``: ``{enabled, msgTypes: [int|str], appMsgTypes: [int], whitelistGroups: [...]}``
    """
    if not trig or not trig.get("enabled"):
        return False
    try:
        msg_type = int(msg.msg_type)
    except (TypeError, ValueError):
        return False

    hit = False
    for key in ("msgTypes", "appMsgTypes"):
        wanted = trig.get(key) or []
        if wanted:
            try:
                if msg_type in {int(x) for x in wanted}:
                    hit = True
                    break
            except (TypeError, ValueError):
                continue
    if not hit:
        return False

    # 群白名单：配了就只在这些群触发（不在白名单 ⇒ 此路不成立，**不影响**其他路）
    groups = trig.get("whitelistGroups") or []
    if groups and chatroom_id not in groups:
        return False
    return True


def _matches_keyword(content: str, trig: dict, group_id: str) -> bool:
    """``keywordTrigger``：白/黑名单 + ``matchMode``(substring/exact/regex)。

    ``trig``: ``{enabled, matchMode, caseSensitive, stripAtMention, keywords,
    whitelistGroups, blacklistGroups}``。群黑名单**一票否决**，优先于白名单。
    """
    if not trig or not trig.get("enabled"):
        return False
    keywords = trig.get("keywords") or []
    if not keywords:
        return False
    if group_id and group_id in (trig.get("blacklistGroups") or []):
        return False
    wl = trig.get("whitelistGroups") or []
    if wl and (not group_id or group_id not in wl):
        return False

    text = content or ""
    if trig.get("stripAtMention", True):
        text = text.replace("@", "")
    if not text:
        return False

    case_sensitive = bool(trig.get("caseSensitive", False))
    if not case_sensitive:
        text = text.lower()
    mode = trig.get("matchMode", "substring")

    for raw_kw in keywords:
        if not raw_kw:
            continue
        kw = raw_kw if case_sensitive else raw_kw.lower()
        if mode == "exact":
            if text.strip() == kw.strip():
                return True
        elif mode == "regex":
            try:
                if re.search(kw, text, 0 if case_sensitive else re.I):
                    return True
            except re.error:
                continue    # 坏正则跳过，不影响其他关键词
        else:
            if kw in text:
                return True
    return False


def is_quoting_bot(msg: WppMessage, self_wxid: str) -> bool:
    """是否引用了 bot 的消息（``reply_to.from_wxid == self_wxid``）。

    公开：它同时是触发判定第 ⑨ 路（``group_quote_bot``）和心流旁路观测
    （``heartflow.note_group_message`` 的 ``quotes_bot`` 标记）的**唯一判据** ——
    两处各写一份"引用"的定义，迟早漂移成"触发了但候选标记是 False"。
    """
    if not self_wxid:
        return False
    return (msg.reply_to or {}).get("from_wxid") == self_wxid


__all__ = ["is_filehelper_session", "is_quoting_bot", "should_process"]
