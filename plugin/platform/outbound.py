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
import os
import re
from dataclasses import dataclass
from typing import Any

from ..core import netguard

log = logging.getLogger(__name__)

#: 单条消息上限。**必须与 ``register_platform(max_message_length=...)`` 同值** ——
#: 否则 live adapter 与进程外（cron/standalone）分片阈值不一致。
#: 2026-10-18 修正：4000 → 2000。微信个人号长文本超过约 2000 字符会被**静默截断**
#: （群里实测把 977 字回复的最后半段吞掉；Hermes 上游 weixin 平台也改用 2000）。
#: 留余量宁可多发一条，也不要让用户看到半句话。
MAX_MESSAGE_LENGTH = 2000

#: 纯文本还走 SendTxt；**这条阈值只是分片线**，与 vendor 无关
_TEXT_CHUNK_LIMIT = MAX_MESSAGE_LENGTH

#: 引用回复 XML 的 ``<title>``/``<des>`` 单字段上限。
#: 微信客户端对 type=57 引用气泡只渲染前 500 字符（vendor 侧同样按 500 截断，
#: 见 ``src/send/quote-xml.ts``）。**超过这个长度不能再走引用回复** ——
#: 否则整条正文会被砍到 500 字，且发送仍返回成功（2026-10-18 群内实测）。
QUOTE_REPLY_MAX_CHARS = 500


def quote_reply_fits(text: str) -> bool:
    """引用气泡能否装下这段正文（含 ``@被回复人 `` 前缀）。

    返回 ``False`` 时调用方必须改走普通 ``/Msg/SendTxt``；不要依赖
    :func:`build_quote_reply_xml` 的 ``[:500]`` —— 那是**截断**，不是分片。
    """
    return len((text or "").strip()) <= QUOTE_REPLY_MAX_CHARS

#: 2026-09-01 修复：过滤「无 @提及，按群规则不回复」类拒绝话术（老板在益融管理群看到 2 次）
#:
#: ⚠️ 2026-10-19 收窄：裸 ``无 @提及`` **不再**单独命中 —— 它可能是正常业务话术的一部分
#: （「这个客户无 @提及 需求」），而命中 = **静默丢弃**，用户永远不知道为什么没回。
#: 保留的两种是"整句拒绝声明"：``无 @提及…不回复`` 与行首 ``不回复：``。
REJECTION_PHRASE_RE = re.compile(
    r"无\s*@\s*提及[，,、\s]*(?:按群规则)?不回复"
    r"|按群规则不回复"
    r"|^\s*不回复\s*[:：]",
    re.MULTILINE,
)

#: 2026-10-18 事故修复：网关**内部机械通知**严禁进生产微信群。
#: 现场：移动业务对接群里漏出两条——① 网关"静默兜底"英文报错
#: ``⚠️ The model returned only a silence marker...``（zh locale 为
#: 「模型只返回了静默标记」），② 后台自审摘要 ``💾 Self-improvement review: ...``
#: （zh locale「💾 自我改进回顾」）。命中 ⇒ 与拒绝话术同款：**静默丢弃，只落日志**。
#: 判据刻意只匹配 locale 文案里的固定短语，不可能命中正常业务话术。
#:
#: 2026-10-18 补：记忆召回提示 ``🧠 MemOS — recalled 9 memories``（Hindsight 同款）
#: 也是 Hermes 的**生命周期状态**，网关对 WeiXin 这类"没有状态栏"的平台会把它
#: 当普通消息 ``adapter.send`` 出去 —— 群里就会出现一行英文内部提示。
#: 形状固定（glyph + provider + em dash + recalled N memor(y|ies)），按行首锚定。
MACHINERY_NOTICE_RE = re.compile(
    r"only a silence marker|只返回了静默标记|只回傳了靜默標記"
    r"|Self-improvement review|自我改进回顾|自我改進檢閱"
    # ⚠️ 2026-10-19：💾 **必须带标题**。旧写法是裸 ``|💾`` ⇒ 任何正文里出现这个
    #    表情（「记得备份💾」）都会被整条静默丢弃。它是自审摘要的**行首标记**，
    #    不是关键词。
    r"|💾\s*(?:Self-improvement review|自我改进回顾|自我改進檢閱)"
    r"|^\s*(?:🧠|👁️)\s+\S.*?—\s*recalled\s+\d+\s+memor(?:y|ies)\s*$"
    # 「忙碌回执」同属过程话术：网关在处理上一轮时又收到消息，会回一行
    # 「⏳ 已排队至下一回合 / ⚡ 正在中断当前任务 / ⏳ 网关正在重启…」。
    # 它不走 ``_interim_send`` 标记（``run_busy._send_busy_reply`` 只是普通
    # ``_send_with_retry``），只能按 locale 固定文案拦。锚行首 + 固定句式，
    # 不会误伤以这些词开头的正常业务话术。
    r"|^\s*(?:⏳|⚡|⏩|↪)\s*(?:已排队|子代理正在工作|正在压缩上下文"
    r"|正在中断当前任务|已引导至当前运行|已重定向当前运行|网关正在)"
    r"|^\s*(?:⏳|⚡|⏩|↪)\s*(?:Queued for the next turn|Subagent working"
    r"|Compressing context|Interrupting current task|Steered into current run"
    r"|Redirected current run|Gateway .*?—\s*queued for the next turn"
    r"|Gateway is .*?and is not accepting)",
    re.MULTILINE,
)

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
        saw_ret = False
        for item in lst:
            # 非 dict 的条目没有 Ret 可判 —— 跳过（不因形状怪异把成功判成失败）
            ret = item.get("Ret") if isinstance(item, dict) else None
            if ret is None:
                continue
            saw_ret = True
            if not _ret_ok(ret):
                return False
        if saw_ret:
            return True
        # ★ 2026-10-19：List 非空但**所有条目都缺 Ret** 时不能直接判成功。
        #   旧写法 == 只要 List 非空就 True，连 BaseResponse.ret=101 都不看 ⇒
        #   vendor 明确拒绝也报成功（幽灵出站记录）。没有逐条结果就退回
        #   BaseResponse；两边都没有可用判据时才按老 vendor 兼容判成功。
        br = data.get("BaseResponse")
        if not isinstance(br, dict):
            return True
        return _ret_ok(br.get("ret"))
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


# ---------------------------------------------------- 出站端点 → 落库元数据


#: 发送类端点 → 消息类型（**逐字对齐** TS ``send/msg.ts:16 OUTBOUND_MSG_TYPES``）。
#: 通用工具表（``toolkit/registry.py``）与 ``wpp_send_message`` 都经过这里落库 ——
#: 否则"agent 调工具发群"在 ``wpp_messages`` 里完全隐形（引用/历史都查不到锚点）。
OUTBOUND_MSG_TYPES: dict[str, str] = {
    "/Msg/SendTxt": "text",
    "/Msg/SendCDNImg": "image",
    "/Msg/UploadImg": "image",
    "/Msg/SendCDNVideo": "video",
    "/Msg/SendVideo": "video",
    "/Msg/ShareVideo": "video",
    "/Msg/SendVoice": "voice",
    "/Msg/SendCDNFile": "file",
    "/Msg/SendEmoji": "emoji",
    "/Msg/ShareCard": "card",
    "/Msg/ShareLink": "link",
    "/Msg/ShareLocation": "location",
    "/Msg/SendXCX": "miniprogram",
}

_XML_TITLE_RE = re.compile(r"<title>([\s\S]*?)</title>")
_XML_PAGEPATH_RE = re.compile(r"<pagepath>([\s\S]*?)</pagepath>")


def _xml_title(xml: Any) -> str:
    if not isinstance(xml, str):
        return ""
    m = _XML_TITLE_RE.search(xml)
    return m.group(1).strip() if m else ""


def outbound_content_for(endpoint: str, body: dict) -> str:
    """从端点 + 请求体提取**可读**正文（base64 只留标记，绝不入库整段）。

    逐字对齐 TS ``send/msg.ts:40 outboundContentFor``：这是"按群查历史"里
    媒体消息唯一能读的一行，也是引用回复的锚点摘要。
    """
    if not isinstance(body, dict):
        return ""
    if endpoint == "/Msg/SendTxt":
        return str(body.get("Content") or "")
    if endpoint == "/Msg/SendCDNImg":
        return f"[图片] {body.get('Content') or ''}"
    if endpoint == "/Msg/UploadImg":
        return "[图片]"
    if endpoint == "/Msg/SendCDNVideo":
        return f"[视频] {body.get('Content') or ''}"
    if endpoint in ("/Msg/SendVideo", "/Msg/ShareVideo", "/Msg/SendVoice"):
        return "[视频]" if "Video" in endpoint else "[语音]"
    if endpoint == "/Msg/SendCDNFile":
        return f"[文件] {body.get('Content') or ''}"
    if endpoint == "/Msg/SendEmoji":
        return f"[表情] {body.get('Md5') or ''}"
    if endpoint == "/Msg/ShareCard":
        return f"[名片] {body.get('CardNickName') or ''}"
    if endpoint == "/Msg/ShareLink":
        return f"[链接] {_xml_title(body.get('Xml'))}"
    if endpoint == "/Msg/ShareLocation":
        return f"[位置] {body.get('Label') or body.get('Poiname') or ''}"
    if endpoint == "/Msg/SendXCX":
        page = ""
        m = _XML_PAGEPATH_RE.search(str(body.get("Content") or ""))
        if m:
            page = m.group(1).strip()
        return f"[小程序] {_xml_title(body.get('Content'))}" + (f" (页面 {page})" if page else "")
    return ""


def outbound_meta_for(endpoint: str, body: dict) -> dict | None:
    """发送端点 → ``{toWxid, msgType, content}``；非发送端点 / 缺目标 ⇒ ``None``。

    对齐 TS ``send/msg.ts:139 outboundMetaFor``。
    """
    msg_type = OUTBOUND_MSG_TYPES.get(endpoint)
    if not msg_type or not isinstance(body, dict):
        return None
    to_wxid = str(body.get("ToWxid") or body.get("toWxid") or "")
    if not to_wxid:
        return None
    return {"toWxid": to_wxid, "msgType": msg_type,
            "content": outbound_content_for(endpoint, body)}


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
    3. 段落 > limit：交给 :func:`_chunk_long_paragraph`；正文优先在句末/逗号处
       断开（2026-10-18 老板要求"自然一些"），代码块与无标点长串才按长度硬切。

    ⚠️ 与"旧 Python 实现"的两处**故意不同**（旧实现是错的，别改回去）：

    - 旧实现**不做硬切**，单行超限就整条发出去 —— 蓝本在 v1.3.63 明确加了
      ``hardSplitLine`` 硬切 + ``CODE_BLOCK_HARD_CAP`` 强制切，理由是单块超限
      会被 vendor 静默截断。旧实现那句"硬切会破坏 markdown/URL"的注释只描述
      了它自己的取舍，不是权威；
    - 旧实现按"行"拼，本实现按"段"拼（空行分隔），对齐蓝本。
    """
    content = content or ""
    if not content.strip():
        # ★ 空串/纯空白 ⇒ **零片**，不是 ``[""]``：``[""]`` 会让调用方真去发一条空文本
        #   （cron 只投附件时尤其常见 —— 文本为空但媒体列表非空），vendor 要么报错
        #   要么推一条空气泡。去掉它之后"没文本"自然退化成"不发文本"。
        return []
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

        # 单行超限 → 切。正文优先在句末/逗号处断开（别把一句话拦腰砍断）；
        # 代码块里保持旧行为按长度硬切（在代码中间找中文标点没有意义）。
        if len(line) > limit:
            if current:
                out.append(current)
                current = ""
            out.extend(_hard_split_line(line, limit, natural=not in_code_block))
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


#: 长段落的**自然断点**（从强到弱）。正文切分优先落在这些字符**之后**，
#: 避免「识盈」这种把半句话切给下一条的观感；代码块不参与（见 ``natural=``）。
_BREAK_STRONG = "。！？!?；;\n"
_BREAK_SOFT = "，,、：:）】」』…"


def _hard_split_line(line: str, limit: int, *, natural: bool = True) -> list[str]:
    """切超限单行。

    2026-10-18 老板反馈「切分长文本应该自然一些，不要生硬切断」：旧实现严格按
    ``limit`` 切，中文长段落会在句子中间断开（真机那条 977 字回复就是被砍在
    「识盈」后面）。现在正文优先在**句末标点**处断，其次在逗号/顿号处断，
    都找不到才按长度切；代码块仍按长度硬切（``natural=False``）。

    ⚠️ TS 要按 ``codePointAt`` 对齐，是因为 JS 字符串是 **UTF-16 码元**，
    切点可能落在星面字符（emoji）的前半个代理对里。Python 字符串是**码点
    序列**，切片不可能切出半个字符 ⇒ 代理对保护在本移植里是**结构性不需要**，
    不是漏写。副作用方向是安全的：同一个 emoji 在 JS 里算 2、在 Python 里算 1，
    所以同样 ``limit`` 下 Python 切出的片段在 JS 口径下**更短**。

    断点搜索范围：

    * 强断点（。！？；）—— 只要不早于 ``limit // 3`` 就接受；
    * 弱断点（，、：）—— 要求不早于 ``limit * 2 // 3``，免得一条消息太短；
    * 都没有 —— 才退化成按 ``limit`` 切。

    断点处的空白（含换行）会从下一片开头剥掉，避免新消息以空格/空行开场。
    """
    if limit <= 0:
        return [line]
    if not natural:
        return [line[i:i + limit] for i in range(0, len(line), limit)]

    out: list[str] = []
    rest = line
    strong_min = max(1, limit // 3)
    soft_min = max(1, limit * 2 // 3)
    while len(rest) > limit:
        window = rest[:limit]
        cut = 0
        for i in range(len(window) - 1, strong_min - 1, -1):
            if window[i] in _BREAK_STRONG:
                cut = i + 1
                break
        if not cut:
            for i in range(len(window) - 1, soft_min - 1, -1):
                if window[i] in _BREAK_SOFT:
                    cut = i + 1
                    break
        if not cut:
            cut = limit
        piece = rest[:cut].rstrip()
        if not piece:
            piece, cut = rest[:limit], limit
        out.append(piece)
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def is_rejection_phrase(content: str) -> bool:
    """是不是「拒绝话术」。命中 ⇒ 出站直接丢弃（**不报错，静默丢**）。"""
    return bool(content and REJECTION_PHRASE_RE.search(content))


def is_machinery_notice(content: str) -> bool:
    """是不是网关**内部机械通知**（静默兜底报错 / 💾 自审摘要）。
    命中 ⇒ 出站静默丢弃（见 :data:`MACHINERY_NOTICE_RE`，2026-10-18 事故）。"""
    return bool(content and MACHINERY_NOTICE_RE.search(content))


def strip_self_mention(content: str, self_wxid: str, self_nickname: str) -> str:
    """摘掉 agent 回复里**指向机器人自己**的 @（防「机器人 @ 自己」）。

    **2026-10-02 真机现场**（``.183`` live-test）：群里有人 @ 了本账号
    （正文 ``@接晓银\\u2005你好``），模型把上下文里那条 ``@接晓银`` 原样抄进回复 ——
    于是账号在群里 **@ 了自己**，而它明明是在回别人的话（老板当场指出
    「回复时候 @ 的对象也不对」）。群里的表现是「接晓银 @ 接晓银 你好！…」，
    一眼假。

    判据：``@<selfWxid>`` / ``@<selfNickname>`` / ``<selfWxid>@`` 三种形态
    （微信 @ 后面跟的是 U+2005 四分之一空格，``normalize_mention`` 收尾的
    ``strip()`` 一并吃掉 —— U+2005 在 Python 里算空白）。判据复用**入站同款**
    :func:`inbound.parser.normalize_mention`（那里的 docstring 就写着
    「去掉内容里的 @bot 提及，避免 AI 重复回 @」—— 本函数把它接到**出站**这一侧：
    此前它只在 ``prepare_text`` 单向生效，模型抄回来的那条没人管）。

    ⚠️ **与 TS 有意不齐**：TS 没有这一道（模型 @ 自己就照发）。保留它的理由与
    本模块其它出站守卫一致 —— 它**只会**摘掉指向自己的 @，不可能改变一条正常回复
    的语义（`.strip()` 之外不动任何字符），而代价是群里出现"自己 @ 自己"。
    行为钉在 ``plugin/tests/test_mentions.py`` 的 §6。
    """
    if not content:
        return content
    # 延迟导入：``platform → inbound`` 只在真发消息时用到这一条边，
    # 不把它抬到模块级（本模块的纪律是"只放无状态纯函数"，导入面越窄越好）。
    from ..inbound.parser import normalize_mention

    return normalize_mention(content, self_wxid or "", self_nickname or "")


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

    ⚠️ 这里仍保留 ``[:QUOTE_REPLY_MAX_CHARS]`` 只是兜底：微信只渲染引用气泡前 500
    字符，超长正文**必须在** :meth:`WppAdapter._send_chunk` 改走普通文本
    （:func:`quote_reply_fits` 是那道判据）。把它当分片用 = 静默丢字。
    """
    reply = (reply_content or "引用回复").strip()[:QUOTE_REPLY_MAX_CHARS]
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

#: 走 ``/Msg/SendImg`` 的图片扩展名（与 Hermes ``send_message`` 的图片集合对齐）。
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"})

#: 走 ``/Msg/SendVideo`` 的视频扩展名。
VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".3gp"})


def media_kind(path: str, *, is_voice: bool = False,
               force_document: bool = False) -> str:
    """Hermes ``media_files`` 的一项 → ``image`` / ``voice`` / ``video`` / ``file``。

    ``force_document`` 优先级最高（Hermes 用它把图片强制按文件发），其次 ``is_voice``
    （发送方已判定这是语音，具体能否进 ``/Msg/SendVoice`` 由 SILK 转码那一步决定）。
    """
    if force_document:
        return "file"
    if is_voice:
        return "voice"
    ext = os.path.splitext(path or "")[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return "file"


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
        # ``~`` 必须先展开：白名单判据与 open() 用的都应是同一个真实路径，
        # 否则辨识与读取分家（旧实现直接 open("~...") 必失败）。
        # ``expanduser`` 是纯字符串处理（读 HOME 环境变量），不碰文件系统 ——
        # 真正的 IO 在下面的 open()，那一步已被 per-file-ignores 的 ASYNC230 覆盖。
        local_ref = os.path.expanduser(ref)  # noqa: ASYNC240 —— 无 IO，仅字符串展开
        if not netguard.is_local_read_allowed(local_ref):
            log.warning("[WPP] 本地媒体读取被拒（不在白名单）: %s", local_ref[:60])
            return ""
        try:
            with open(local_ref, "rb") as fh:
                return base64.b64encode(fh.read()).decode()
        except OSError as exc:
            log.warning("[WPP] 本地媒体读取失败: %s", exc)
            return ""

    # 既不是 URL 也不是路径 ⇒ 当作已经是 base64
    return ref


def assert_outbound_allowed(peer_id: str, account_id: str = "",
                            *, allowed: tuple[str, ...] | None = None) -> None:
    """出站白名单闸（调试保险）。未设 ``WECHATPRO_SEND_ALLOWLIST`` 时是 no-op。"""
    netguard.check_outbound_allowed(peer_id, account_id, allowed=allowed)
