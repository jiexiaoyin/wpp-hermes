"""引用回复的"被回复人"身份解析 —— 群聊 ``@被回复人`` 的**唯一**数据来源。

## 为什么有它（2026-10-03 老板报的 bug）

「Hermes 插件，群里 @ 机器人时，触发回复了，但是回复没有和 Openclaw 插件一样，
可以 @ 被回复的人」

代码一直在、链路也是通的：``platform/adapter.py::_send_chunk`` 的引用分支会调
``_quote_prefix`` 生成 ``"@昵称 "`` 前缀，再交给 ``build_quote_reply_xml``。
**但它唯一的输入是死的**：

* ``_quote_prefix`` 拿到的 ``fromusr`` 来自 ``metadata["quote_from_wxid"]``，而这个键
  **全树只有读、没有写**（``command grep -rn "quote_from_wxid" plugin/`` 只有一处）；
* Hermes 那侧也不会给：``gateway/platforms/base.py::_thread_metadata_for_source``
  只为 telegram / slack / feishu 造 metadata 键，wechatpadpro 的 ``send()`` 收到的
  ``metadata`` 恒是 ``None`` / ``{}``。

⇒ ``fromusr`` 恒空 ⇒ ``_quote_prefix`` 第一句 ``return ""`` ⇒ **前缀从来没出现过**，
refermsg 里的 ``<fromusr>`` 也一直是空。

旧树（``/root/dev/wpp-hermes/plugin/adapter.py::_send_quote``）当年**不是**靠 metadata：
它按锚点 id **回查 DB**（``SELECT msg_id, new_msg_id, from_wxid, peer_name FROM
wpp_messages WHERE account_id=%s AND (msg_id=%s OR new_msg_id=%s) LIMIT 1``）拿到
"是谁被回复"，Hermes 移植把这一截丢了。TS 同样回查
（``src/send/quote-reply.ts`` → ``getMessageByMsgIdOrNewId(..., {direction:'any'})``）。
本模块就是那一截的 Python 版，只负责**纯逻辑**：DB 行 → 身份。

## 与 TS 的三处对应

1. **不限方向**：被引用的可能是 bot 自己发的（``direction='outbound'``）⇒ TS 传
   ``direction:'any'``（v1.3.18 P1-核心2），:func:`resolve_quote_identity` 也不看方向。
2. **发送者优先级**（TS ``quote-reply.ts`` 里 ``dbFromWxid`` 那一段）：
   行里的 ``from_wxid`` → ``raw_payload`` 的发送者字段 → 正文前缀 ``wxid_xxx:\\n`` →
   （**非群**记录）``peer_id``。TS 还多一档"参数优先"（``params.fromWxid``，来自
   dispatcher 手上的触发消息）—— py2 对应的那一档是 ``metadata["quote_from_wxid"]``，
   留在适配器入口处（本模块只看 DB 行）。
3. **群 id 不是人**：``xxx@chatroom`` 一律当"拿不到发送者"。少这道闸，@ 的对象会变成
   **群自己** —— business callback 的 ``FromUserName`` 与旧格式群消息里那个字段
   本身就是群 id，这是真会发生的形态，不是理论风险。

   ⚠️ **这一档 py2 比 TS 严，是有意偏离**：TS 只有两道**局部**的闸 ——
   ``paramFromWxidValid``（``quote-reply.ts:163``，只管参数那一档）与 ``resolveDisplayName``
   （``:74``，只管**查昵称**那一步）。``dbFromWxid`` 这一串本身**不设闸**：
   ``rawSenderId``（``:183-187``，群消息里 ``FromUserName`` 就是群 id）与
   ``extractGroupSenderWxid(rec.content)``（``:193``）拿到的群 id 会**原样**流进
   ``fromusr``，再由 ``buildGroupMentionPrefix`` 兜底成 ``@111@chatroom `` 真发出去。
   py2 在**每一档出口**都过 :func:`is_usable_sender`，代价是零（发送者 wxid 恒以
   ``wxid_``/``gh_`` 开头，闸不掉任何一个真发送者），收益是群里不会出现"@ 一个群"。

## 有意不搬的一截

TS 的 ``stripGroupContentPrefix``（把 refermsg 正文里的发送者前缀删掉）**不搬**：
py2 的 ``parser.strip_group_prefix``（``parser.py:160``）在入站那一步已经把
``wxid_xxx:\\n`` 从正文里摘掉了，那函数只需要 ``fromusr``，用不上它。搬过来就是
第二份真相源。而 ``extractGroupSenderWxid`` **要**搬 —— 它读的正是"正文里还留着前缀"
的那种老形态（本模块的 :func:`extract_group_sender_wxid`）。

INVARIANT: W-Q —— 身份解析**永不抛**（昵称只是装饰，DM 或查不到时少个 @ 是正常降级），
且 ``@`` 前缀的形状**只有** :func:`build_mention_prefix` 一个产出点。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: 群聊会话 id 后缀。**与** ``platform/outbound.py::is_group_peer`` / ``data/repo_messages.py``
#: 的 ``GROUP_SUFFIX`` 同值；本模块在 ``inbound/`` 层，import 那两个会成环
#: （``platform`` → ``inbound`` 已是单向依赖），故就地声明并在此交叉引用。
GROUP_SUFFIX = "@chatroom"

#: ``raw_payload`` 里"发送者 wxid"的几种拼法（对齐 TS ``quote-reply.ts`` 的
#: ``rawSenderId`` 那段；``FromUserName`` 是旧格式/v1 的字段名，**群消息里它是群 id**，
#: 靠 :func:`is_usable_sender` 那道闸挡掉）。``go_fields`` 是本树实测的 WS 形态
#: （``parser._extract_msg_type`` 那条注释记的就是它）。
_FLAT_SENDER_KEYS = ("sender_id", "fromUser", "FromWxid", "fromWxid",
                     "from_user_id", "FromUserName")
_GO_FIELDS_KEY = "go_fields"
_GO_SENDER_KEYS = ("sender_id", "fromUser", "FromWxid", "fromWxid",
                   "from_user_id", "FromUserName")

#: 嵌套 ``from_user`` 对象里的 wxid 键（``parser._extract_from_user`` 认的那几种）
_NESTED_SENDER_KEYS = ("wxid", "userName", "UserName", "user_name")

#: 正文前缀形态：``wxid_xxx:\\n`` / ``gh_xxx:\\n`` / ``xxx@chatroom:\\n``
#: —— 逐字移植 TS ``extractGroupSenderWxid`` 的正则（**ASCII** 字符类，别写成 ``\w``）。
_PREFIX_RE = r"^(wxid_[a-zA-Z0-9_]+|gh_[a-zA-Z0-9_]+|[a-zA-Z0-9_]+@chatroom):\n"


@dataclass(frozen=True)
class QuoteIdentity:
    """被引用消息的"是谁发的"。

    ``matched`` = 是否命中了 DB 行（供调用方判"该不该出声"：命中但拿不到发送者要留痕，
    整条查不到只是降级）。
    """

    fromusr: str = ""
    display_name: str = ""
    matched: bool = False


def is_usable_sender(wxid: Any) -> str:
    """这个值能不能当"被回复人" —— 空串与群 id 都不行（返回 ``""``）。"""
    from .parser import _safe_str

    val = _safe_str(wxid)
    if not val or val.endswith(GROUP_SUFFIX):
        return ""
    return val


def extract_group_sender_wxid(content: Any) -> str:
    """从正文前缀 ``wxid_xxx:\\n`` 里抠发送者 wxid（移植 TS ``extractGroupSenderWxid``）。

    这是 business callback / 旧格式群消息的形态：``FromUserName`` 是群 id，
    真正的发送者只在正文第一行。找不到返回 ``""``。
    """
    from .parser import _safe_str

    text = _safe_str(content)
    if not text:
        return ""
    m = re.match(_PREFIX_RE, text)
    return m.group(1) if m else ""


def _raw_payload_dict(raw: Any) -> dict:
    """``raw_payload`` 可能是 dict、也可能是 JSON 字符串（MariaDB 的 JSON 列 =
    LONGTEXT，pymysql 原样回字符串）。解不出来就当空 dict（**不抛**）。"""
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, (str, bytes)):
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return dict(loaded) if isinstance(loaded, Mapping) else {}
    return {}


def _payload_sender(raw: Any) -> str:
    """从原始 payload 里取发送者 wxid（顶层键 → ``go_fields`` → 嵌套 ``from_user``）。"""
    payload = _raw_payload_dict(raw)
    if not payload:
        return ""

    nested = payload.get("from_user") or payload.get("fromUser")
    if isinstance(nested, Mapping):
        for key in _NESTED_SENDER_KEYS:
            found = is_usable_sender(nested.get(key))
            if found:
                return found

    go = payload.get(_GO_FIELDS_KEY)
    for source, keys in ((payload, _FLAT_SENDER_KEYS),
                         (go if isinstance(go, Mapping) else {}, _GO_SENDER_KEYS)):
        for key in keys:
            found = is_usable_sender(source.get(key))
            if found:
                return found
    return ""


def resolve_quote_identity(row: Any) -> QuoteIdentity:
    """DB 行 → :class:`QuoteIdentity`（**纯函数**，永不抛）。

    ``row`` 为空 / 不是 mapping ⇒ 空身份（``matched=False``）。
    """
    if not isinstance(row, Mapping):
        return QuoteIdentity()

    from .parser import _safe_str

    fromusr = is_usable_sender(row.get("from_wxid"))
    if not fromusr:
        fromusr = _payload_sender(row.get("raw_payload"))
    if not fromusr:
        # ⚠️ 正文前缀那一档也要过 :func:`is_usable_sender`（TS 在这条路上**没有**闸，见
        # 模块 docstring 第 3 条）。多这一道不会误伤真发送者：发送者 wxid 恒以
        # ``wxid_``/``gh_`` 开头，正则里唯一会被闸掉的就是 ``xxx@chatroom`` 那一个分支。
        fromusr = is_usable_sender(extract_group_sender_wxid(row.get("content")))
    if not fromusr and str(row.get("peer_kind") or "").strip().lower() != "group":
        # TS：非群记录才能拿 peer_id 当发送者（群记录的 peer_id 就是群本身）。
        fromusr = is_usable_sender(row.get("peer_id"))
    return QuoteIdentity(
        fromusr=fromusr,
        display_name=_safe_str(row.get("peer_name")),
        matched=True,
    )


def build_mention_prefix(peer_id: Any, name: Any, fromusr: Any) -> str:
    """``@被回复人 `` 前缀（**只群聊**）。移植 TS ``buildGroupMentionPrefix``。

    TS 的规则逐字照抄：``atName = displayName && displayName !== fromusr
    ? displayName : (fromusr ?? "")``；``atName`` 为空 ⇒ 不加前缀。
    "名称与 wxid 相同"要当**没有名称**处理 —— 否则会真 @ 出 ``@wxid_xxx``。

    返回值是**前缀本身**（``"@张三 "``），不是 TS 那种"拼好的整句"：本树的调用点
    ``_send_chunk`` 做的是 ``f"{prefix}{chunk}".strip()``，与 TS 的
    ``(`@${atName} ${content}`).trim()`` 同形（分片只切正文，前缀只贴第一片）。
    """
    from .parser import _safe_str

    if not _safe_str(peer_id).endswith(GROUP_SUFFIX):
        return ""
    at_name = _safe_str(name)
    if not at_name or at_name == _safe_str(fromusr):
        at_name = _safe_str(fromusr)
    if not at_name:
        return ""
    return f"@{at_name} "
