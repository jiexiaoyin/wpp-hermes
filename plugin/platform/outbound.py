"""出站纯函数与安全闸 —— 旧 ``adapter.py`` 出站段(950–1520) 的抽出。

**这里只放无状态的东西**（分片、XML、响应判定、媒体解析）。真正"谁发给谁"的
编排留在 ``adapter.py``，保持可测。

## 抽出来的动机

旧实现里同一套「判定发送是否成功 + 取 msgId」在 ``send()`` / ``_send_quote()``
/ ``_standalone_send()`` 里**各写一遍**，三份的字段名还不完全一致
（``Newmsgid`` / ``NewMsgId`` / ``Msgid`` / ``BaseResponse.NewMsgId``）。
收敛到 ``send_ok()`` / ``extract_outbound_msg_ids()`` 一处，避免「某条路径漏判 ret≠0」。

**收敛本身不够 —— 判据得跟蓝本对齐。** 2026-10-01 补上两条从蓝本逐行核出来的缺口
（两条本树都曾经"全绿但取不到/误报"）：逐条结果 ``Data.List[].Ret``（v1.5.6
SEND-LIST-RET，拒收被当成功）与 ``Data.List[0].NewMsgId``（v1.3.57 P1-2，
顶层取不到 id 导致 ``bot_msg_id`` 恒 NULL）。详见各自 docstring。

## 必须保留的行为

- **拒绝话术过滤**：agent 偶发输出「无 @提及，按群规则不回复」这类话术，
  插件层直接丢弃，绝不发进群（2026-09-01 老板在益融管理群看到 2 次）。
- **分片**：超限按段落切，**逐片发送 + 逐片入库**（群友引用 bot 消息需要 DB 有记录）。
- **引用回复**：``type=57`` 的 ``refermsg`` XML，群聊还要加 ``@被回复人`` 前缀。
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Any

from ..core import netguard

log = logging.getLogger(__name__)

#: 单条消息上限。**必须与 ``register_platform(max_message_length=...)`` 同值** ——
#: 否则 live adapter 与进程外（cron/standalone）分片阈值不一致。
#: 旧实现是 adapter 6000 / registry 4000 两个数，已统一取**较小值**（更安全：
#: 微信长消息会被截断）。
MAX_MESSAGE_LENGTH = 4000

#: 纯文本还走 SendTxt；**这条阈值只是分片线**，与 vendor 无关
_TEXT_CHUNK_LIMIT = MAX_MESSAGE_LENGTH

#: 2026-09-01 修复：过滤「无 @提及，按群规则不回复」类拒绝话术（老板在益融管理群看到 2 次）
REJECTION_PHRASE_RE = re.compile(r"无\s*@\s*提及|按群规则不回复|不回复\s*[:：]")

#: 群聊标识后缀
GROUP_SUFFIX = "@chatroom"


# ------------------------------------------------------------------ 响应判定


def _ret_ok(value: Any) -> bool:
    """``ret`` 值是否算成功。``None`` = 端点根本没回这个字段 ⇒ 视为成功（兼容老 vendor）。

    ``"0"`` 也算成功：本树**一直**是这么判的（抽 ``send_ok`` 之前的老实现就是
    ``ret in (0, "0", None)``），与蓝本 TS 的 ``!== 0`` 有意不同 —— 字符串 ``"0"``
    的字面意思就是成功，按 ``=== 0`` 判会把**真成功**当失败报。
    """
    return value in (0, "0", None)


def send_ok(resp: Any) -> bool:
    """发送是否成功（对齐 OpenClaw ``isSendOk``，含 v1.5.6 的逐条 ``Ret`` 判据）。

    两道，缺一不可：

    1. ``Code`` ∈ {0, 200, None}；
    2. **逐条结果优先**：``Data.List`` 非空时只看 ``List[].Ret`` ——
       **任一条 ``Ret`` 非 0 ⇒ 判失败**（此时**不再看** ``BaseResponse.ret``，
       与蓝本一致）；``List`` 不存在/为空时退回 ``Data.BaseResponse.ret``。

    ## 为什么第 2 条不是多余（2026-09-09 的真实故障）

    某些 vendor 端点回 ``Code=0`` + ``BaseResponse.ret=0``，看着全对，但**逐条结果**
    在 ``Data.List[].Ret`` 里是 ``-2`` ⇒ 消息实际被拒收（蓝本
    ``src/dispatch/outbound.ts:40 isSendOk`` 的 ``v1.5.6 SEND-LIST-RET``：
    某业务晨报 30362 群**根本没收到**，而调用方一路报成功）。
    只看 ``BaseResponse.ret`` 就会把"被拒收"当成功 —— 后果不是漏一条日志，是
    **幽灵出站记录 + 台账写 ``sent`` + 白消费一次发言预算**。

    ⚠️ 本函数是**唯一**判据：``extract_msg_id`` 的前提、``_persist_outbound`` 的前提、
    心流 ``note_sent`` 的 ``success`` 都从它来。别在调用点另写一份。

    ## 第 1 条与蓝本有意不同（``Code`` 缺失算成功）

    蓝本 ``isSendOk`` 是 ``Code !== 0 && Code !== 200 → false``，即**缺 ``Code`` 也算失败**。
    本树不缺 —— 因为**传输层已经先判过一次**：``core/api_client.py`` 的 POST 路径在
    ``code is not None and code != 0`` 时直接 ``raise WppApiError``（唯一豁免
    ``_IGNORE_CODE_ENDPOINTS = {"/Msg/Quote"}``，而出站走的是 ``/Msg/SendTxt`` /
    ``/Msg/ShareLink``，**不在豁免里**）。也就是说 ``send_ok`` 收到的东西里 ``Code``
    只可能是 0 或**真的没这个字段**（老 vendor 不回），照蓝本判会把真成功报成失败
    ⇒ 台账不落 ``sent`` ⇒ 重试**重复发进群**。这里保留 ``None`` 是有意的容错，
    不是漏移植。
    """
    if not isinstance(resp, dict):
        return False
    code = resp.get("Code")
    if code not in (0, 200, None):
        return False
    data = resp.get("Data")
    if not isinstance(data, dict):
        return True
    lst = data.get("List")
    if isinstance(lst, (list, tuple)) and lst:
        for item in lst:
            # 非 dict 的条目没有 Ret 可判 —— 跳过（不因形状怪异把成功判成失败）
            ret = item.get("Ret") if isinstance(item, dict) else None
            if ret is None:
                continue
            if not _ret_ok(ret):
                return False
        return True
    br = data.get("BaseResponse")
    if not isinstance(br, dict):
        return True
    return _ret_ok(br.get("ret"))


def _first_str(*candidates: Any) -> str | None:
    """第一个"有内容"的候选，统一成 ``str``。

    ⚠️ 判据是**真值**（``if not c: continue``）而不是 ``is not None``：``0`` / ``""``
    都不成其为消息 id，且旧实现（``if candidate:``）就是照真值取的 ——
    改成 ``is not None`` 会让 ``NewMsgId: 0`` 顶掉后面那个真候选。
    （与蓝本 ``??`` 的差别仅在 "``0`` 算不算数" 这一处，且方向是**更不容易取错**。）
    """
    for c in candidates:
        if not c:
            continue
        return str(c)
    return None


@dataclass(frozen=True)
class OutboundMsgIds:
    """一次发送响应里的 id 三元组 —— 蓝本 ``extractOutboundMsgIds`` 的移植。

    * ``new_msg_id`` —— 服务端消息 id（≙ 入站引用里的 ``svrid``）
    * ``client_msg_id`` —— 客户端消息 id
    * ``msg_id`` —— 蓝本口径 ``clientMsgId ?? newMsgId``
    """

    msg_id: str | None
    new_msg_id: str | None
    client_msg_id: str | None


def extract_outbound_msg_ids(resp: Any) -> OutboundMsgIds:
    """从发送响应里取全部 id（对齐 OpenClaw ``src/send/msg.ts:76 extractOutboundMsgIds``）。

    ## 为什么必须读 ``Data.List[0]``

    蓝本 ``src/dispatch/outbound.ts:261`` 的 ``v1.3.57 P1-2`` 写得直白：
    **「vendor 响应在 ``Data.List[0].NewMsgId``，顶层 msgId 是 undefined」**。
    旧实现只试 ``Data.NewMsgId`` / ``Data.Msgid`` / ``BaseResponse.*``，
    **从不看 ``List``** ⇒ 真机上取到的永远是 ``None``（2026-10-01 两次真机发送，
    台账 ``bot_msg_id`` 与 ``wpp_messages.msg_id`` 都是 NULL —— 静默降级，一句错都没有）。

    取 id 链（蓝本链 + 旧实现那两个猜测留作**尾部兜底**，保证老响应不会反而取不到）：

    * ``new_msg_id``   : ``List[0].NewMsgId`` → ``Data.NewMsgId``(旧) → ``Data.Newmsgid``
      → ``Data.newMsgId`` → ``Data.msgId`` → ``BaseResponse.NewMsgId``(旧) → ``BaseResponse.Newmsgid``(旧)
    * ``client_msg_id``: ``List[0].ClientMsgid`` → ``List[0].MsgId`` → ``Data.Msgid`` → ``Data.msgId``
    * ``msg_id``       : ``client_msg_id or new_msg_id``（★ 客户端 id 优先，同蓝本）

    ⚠️ 蓝本还取 ``createTime``（``List[0].Createtime`` / ``Data.CreateTime``，撤回要用）。
    本树**没有**撤回路径（``wpp_revoke_msg`` 的 id 来自调用方参数，不来自发送响应）
    ⇒ 不搬 —— 搬了就是没人读的死字段。
    """
    data = resp.get("Data") if isinstance(resp, dict) else None
    if not isinstance(data, dict):
        return OutboundMsgIds(None, None, None)
    lst = data.get("List")
    head = lst[0] if isinstance(lst, (list, tuple)) and lst and isinstance(lst[0], dict) else {}
    br = data.get("BaseResponse")
    br = br if isinstance(br, dict) else {}

    new_id = _first_str(head.get("NewMsgId"), data.get("NewMsgId"), data.get("Newmsgid"),
                        data.get("newMsgId"), data.get("msgId"),
                        br.get("NewMsgId"), br.get("Newmsgid"))
    client_id = _first_str(head.get("ClientMsgid"), head.get("MsgId"),
                           data.get("Msgid"), data.get("msgId"))
    return OutboundMsgIds(msg_id=client_id or new_id, new_msg_id=new_id, client_msg_id=client_id)


def extract_msg_id(resp: Any) -> str | None:
    """取 ``msg_id``（= ``clientMsgId ?? newMsgId``）—— :func:`extract_outbound_msg_ids` 的薄封装。

    要 ``new_msg_id``（落库/引用关联用）就直接调 :func:`extract_outbound_msg_ids`，
    别拿这个再拼一遍。
    """
    return extract_outbound_msg_ids(resp).msg_id


def send_error_text(resp: Any) -> str:
    """从失败响应里取可读错误（vendor ``BaseResponse.errMsg`` + Code）。"""
    if not isinstance(resp, dict):
        return "unknown"
    data = resp.get("Data")
    br = (data or {}).get("BaseResponse") if isinstance(data, dict) else None
    ret = br.get("ret") if isinstance(br, dict) else None
    err = br.get("errMsg") if isinstance(br, dict) else None
    return f"vendor ret={ret} err={err} Code={resp.get('Code')}"


# ---------------------------------------------------------------------- 分片

#: 代码块允许超过 ``limit`` 的倍数；超过则强制切分。
#: 对齐 TS ``CODE_BLOCK_HARD_CAP``（v1.3.63 P1-2）：单个巨型代码块若整块不切，
#: 会超到 vendor 的隐藏上限被**静默截断**。
CODE_BLOCK_HARD_CAP = 2

#: 段落切分时扫围栏用（允许前导空白，与 TS 外层一致）
_FENCE_RE = re.compile(r"^\s*```")
#: 长段落内切分时扫围栏用（**不允许**前导空白，与 TS 内层一致）
#: ⚠️ 这处内外层不一致是**照搬蓝本**的结果，不是笔误 —— 改动它会改变分片边界。
_FENCE_TIGHT_RE = re.compile(r"^```")
_BLANK_RE = re.compile(r"^\s*$")


def chunk_text(content: str, limit: int = _TEXT_CHUNK_LIMIT) -> list[str]:
    """分片（``limit`` 以内原样返回单元素）。

    逐字对齐 OpenClaw ``chunkMarkdown``（``src/dispatch/outbound.ts:341``）：

    1. 先扫全文跟踪 ```` ``` ```` 围栏，按**空行**切段 —— 代码块内的空行**不算**
       段落边界（v1.3.63 P2-1：否则围栏会被段级切分拆断）；
    2. 段落 ≤ limit：尽量与相邻段落拼成一段（留 2 字符给 ``\\n\\n``）；
    3. 段落 > limit：交给 :func:`_chunk_long_paragraph`，**含硬切**。

    ⚠️ 与"旧 Python 实现"的两处**故意不同**（旧实现是错的，别改回去）：

    - 旧实现**不做硬切**，单行超限就整条发出去 —— 蓝本在 v1.3.63 明确加了
      ``hardSplitLine`` 硬切 + ``CODE_BLOCK_HARD_CAP`` 强制切，理由是单块超限
      会被 vendor 静默截断。旧实现那句"硬切会破坏 markdown/URL"的注释只描述
      了它自己的取舍，不是权威；
    - 旧实现按"行"拼，本实现按"段"拼（空行分隔），对齐蓝本。
    """
    content = content or ""
    if len(content) <= limit:
        return [content]

    out: list[str] = []
    current = ""
    in_code_block = False

    # ── 第一遍：跟踪围栏，按空行切段（块内空行不作边界）──
    blocks: list[str] = []
    buf = ""
    for line in content.split("\n"):
        if _FENCE_RE.match(line):
            in_code_block = not in_code_block
        if _BLANK_RE.match(line) and not in_code_block:
            blocks.append(buf)
            buf = ""
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        blocks.append(buf)

    # ── 第二遍：装箱 ──
    for para in (b.strip() for b in blocks):
        if not para:
            continue
        if len(para) <= limit:
            # 留 2 字符给 `\n\n` 分隔
            if len(current) + len(para) + 2 > limit:
                if current:
                    out.append(current)
                current = para
            else:
                current = f"{current}\n\n{para}" if current else para
            continue

        # 单段就超限 → 先 flush，再切这一段
        if current:
            out.append(current)
            current = ""
        out.extend(_chunk_long_paragraph(para, limit))

    if current:
        out.append(current)
    return out


def _chunk_long_paragraph(text: str, limit: int) -> list[str]:
    """切超限长段落（对齐 TS ``chunkLongParagraph``，v1.3.63 P1-2）。

    - 代码块内：加一行会超 ``limit * CODE_BLOCK_HARD_CAP`` 才强制切（优先保围栏完整）；
    - 普通行：超一行就切；
    - 单行本身 > limit：硬切（:func:`_hard_split_line`）。
    """
    out: list[str] = []
    current = ""
    in_code_block = False

    for line in text.split("\n"):
        if _FENCE_TIGHT_RE.match(line):
            in_code_block = not in_code_block

        # 单行超限 → 硬切
        if len(line) > limit:
            if current:
                out.append(current)
                current = ""
            out.extend(_hard_split_line(line, limit))
            continue

        sep = "\n" if current else ""
        would_exceed = len(current) + len(sep) + len(line) > limit

        # 代码块内：未达硬 cap 就先攒着（优先保持代码块完整）
        if in_code_block and len(current) + len(sep) + len(line) > limit * CODE_BLOCK_HARD_CAP:
            out.append(current)
            current = line
            continue
        if would_exceed and in_code_block:
            current += f"{sep}{line}"
            continue
        if would_exceed:
            out.append(current)
            current = line
        else:
            current = f"{current}{sep}{line}" if current else line

    if current:
        out.append(current)
    return out


def _hard_split_line(line: str, limit: int) -> list[str]:
    """硬切超限单行（对齐 TS ``hardSplitLine``）。

    ⚠️ TS 要按 ``codePointAt`` 对齐，是因为 JS 字符串是 **UTF-16 码元**，
    切点可能落在星面字符（emoji）的前半个代理对里。Python 字符串是**码点
    序列**，切片不可能切出半个字符 ⇒ 代理对保护在本移植里是**结构性不需要**，
    不是漏写。副作用方向是安全的：同一个 emoji 在 JS 里算 2、在 Python 里算 1，
    所以同样 ``limit`` 下 Python 切出的片段在 JS 口径下**更短**。
    """
    if limit <= 0:
        return [line]
    return [line[i:i + limit] for i in range(0, len(line), limit)]


def is_rejection_phrase(content: str) -> bool:
    """是不是「拒绝话术」。命中 ⇒ 出站直接丢弃（**不报错，静默丢**）。"""
    return bool(content and REJECTION_PHRASE_RE.search(content))


def is_group_peer(peer_id: str) -> bool:
    return bool(peer_id and peer_id.endswith(GROUP_SUFFIX))


# ------------------------------------------------------------------ 引用 XML


def escape_xml(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def build_quote_reply_xml(reply_content: str, svrid: str, fromusr: str = "") -> str:
    """引用回复 XML（迁移自 wpp ``quote-xml.ts buildQuoteReplyXml``）。

    形状::

        <appmsg><title>回复</title><des>回复</des><type>57</type>
          <refermsg><svrid>..</svrid><fromusr>..</fromusr></refermsg></appmsg>

    ``svrid`` 为空时**不带 refermsg** —— 没有可引用的目标，带了反而让 vendor 拒。
    """
    reply = (reply_content or "引用回复").strip()[:500]
    title = escape_xml(reply)
    svrid_e = escape_xml((svrid or "").strip())
    refermsg = ""
    if svrid_e:
        refermsg = f"<refermsg><svrid>{svrid_e}</svrid>"
        if fromusr:
            refermsg += f"<fromusr>{escape_xml(fromusr)}</fromusr>"
        refermsg += "</refermsg>"
    return f"<appmsg><title>{title}</title><des>{title}</des><type>57</type>{refermsg}</appmsg>"


# ------------------------------------------------------------------ 媒体解析


async def resolve_to_base64(ref: str, *, timeout: int = 15) -> str:
    """URL / 本地路径 / base64 / ``data:`` → 纯 base64。

    安全闸全走 ``core.netguard``：本地路径过白名单（防读 ``/etc/passwd`` 外泄），
    URL 过 SSRF 校验（防 prompt injection 打内网）。失败返回 **空串**（调用方判空），
    与旧实现一致 —— 媒体取不到不该让工具整体抛错。
    """
    ref = (ref or "").strip()
    if not ref:
        return ""
    if ref.startswith("data:"):
        return ref.split(",", 1)[1] if "," in ref else ""

    if ref.startswith("http"):
        try:
            data = await netguard.safe_fetch_url(ref, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP] 媒体 URL 获取失败: %s", exc)
            return ""
        return base64.b64encode(data).decode()

    if ref.startswith("/") or ref.startswith("~"):
        if not netguard.is_local_read_allowed(ref):
            log.warning("[WPP] 本地媒体读取被拒（不在白名单）: %s", ref[:60])
            return ""
        try:
            with open(ref, "rb") as fh:
                return base64.b64encode(fh.read()).decode()
        except OSError as exc:
            log.warning("[WPP] 本地媒体读取失败: %s", exc)
            return ""

    # 既不是 URL 也不是路径 ⇒ 当作已经是 base64
    return ref


def assert_outbound_allowed(peer_id: str, account_id: str = "") -> None:
    """出站白名单闸（调试保险）。未设 ``WECHATPRO_SEND_ALLOWLIST`` 时是 no-op。"""
    netguard.check_outbound_allowed(peer_id, account_id)
