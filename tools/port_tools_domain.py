"""把旧扁平 ``plugin/tools.py`` **机械地**切分成 ``plugin/toolkit/`` 包。

## 为什么用脚本而不是手抄

``register_msg_tools`` 有 660 行**手工调过的生产逻辑**（每个 vendor 端点的参数名、
错误码判定、mmx CLI 调用链都是踩过坑的）。手抄一遍就是给"抄错一个参数名"开后门，
而且抄错的后果是运行期才发现。

所以这里做的是**行区间搬家 + 少量登记在案的导入重定向**，并被
``tools/verify_tools_port.py`` 用 AST 逐函数比对原文 ——
**任何未登记的改动都会被抓住**。

## 搬家地图

========================================  ==========================================
旧位置（``plugin/tools.py``）            新位置
========================================  ==========================================
``_get_client`` … ``_get_noop_adapter_getter``   ``plugin/toolkit/registry.py``（机器）※
``register_msg_tools``                   ``plugin/toolkit/msg.py``                 ← 本脚本
``register_mcp_tools``                   ``plugin/toolkit/mcp.py``                 ※
``register_jargon_tools``                ``plugin/toolkit/jargon_tool.py``         ← 本脚本
``_LOCAL_READ_DIRS`` / ``_is_local_read_allowed`` / ``_safe_fetch_url`` /
``_resolve_to_base64``                   **不搬** —— 已被 ``core/netguard.py`` +
``platform/outbound.py`` 取代（单一真源）
``register_tools``（973+，死入口）        **不搬** —— F3 实测它从未被调用
========================================  ==========================================

※ ``registry.py`` / ``mcp.py`` 由另一路负责（``mcp.py`` 还要接新的 repo 读函数），
  不由本脚本写盘 —— 但本脚本的 ``SPLITS`` 仍保留它们的原文区间，
  ``tools/verify_tools_port.py`` 拿它做比对基准。

用法::

    python3 tools/port_tools_domain.py            # 生成（幂等；有手工改动时拒写）
    python3 tools/port_tools_domain.py --force    # 覆盖手工改动（危险，先看 diff）
    python3 tools/port_tools_domain.py --check    # deploy 门：AST 等价 + 注册对账
    python3 tools/port_tools_domain.py --range    # 只打印原文区间

⚠️ **2026-10-18 起 ``msg.py`` / ``jargon_tool.py`` 不再是"纯净生成物"**：对齐 TS
的新能力（出站落库、生图后端、jargon ``text`` 匹配）是直接改在这两个文件里的，
逐字节再生成会把这些改动**静默抹掉**。所以 ``--check`` 改为委托
``tools/verify_tools_port.py`` —— 它按 **AST + 注册清单**核对"相对旧实现的差异
是否全部登记在 ``DECLARED_AST_DIFFS`` / ``DECLARED_REGISTRATION_DIFFS``"，
既允许有意改动，又对任何未登记的漂移保持红灯。生成模式遇到磁盘内容与生成结果
不一致时**默认拒写**（``--force`` 才覆盖）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 蓝本 = **冻结的旧树**里的扁平 ``tools.py``，不是本树同名文件。
#: 两个理由：① 本树那份已被 ``plugin/toolkit/`` 目录**遮蔽**（同名包优先于模块），
#: 且它本身在删除清单上；② 旧树按决策「原样冻结」作参照与回滚蓝本 ⇒ 生成器
#: 永远可复现，不会因为本树清理而失效。
#: 旧树（2026-10-18 归档到 _archive，只作参照）。本脚本是**一次性移植工具**，
#: 只有显式跑它时才读这个文件；主链路（注册/生成/deploy）不依赖旧树。
LEGACY = Path("/root/dev/_archive/wpp-hermes-legacy-20261018/plugin/tools.py")
OUT_DIR = ROOT / "plugin" / "toolkit"

#: 每个目标文件的「锚点区间」——用函数名定位，不写死行号（行号会漂）。
SPLITS: dict[str, tuple[str, str | None]] = {
    # 目标文件名 -> (起锚正则, 止锚正则；None = 到文件尾)
    "registry.py": (r"^def _get_client\(", r"^# -+ msg 域工具"),
    "msg.py": (r"^def register_msg_tools\(", r"^def register_mcp_tools\("),
    "mcp.py": (r"^def register_mcp_tools\(", r"^def register_jargon_tools\("),
    "jargon_tool.py": (r"^def register_jargon_tools\(", r"^# 本地文件读取白名单"),
}

#: **不搬**的区间（必须在原文里出现，否则说明蓝本变了，得重新评估）
MUST_SKIP: tuple[str, ...] = (
    r"^def register_tools\(",          # F3: 死入口，从未被调用
    r"^async def _resolve_to_base64\(",  # 已被 outbound.resolve_to_base64 取代
    r"^async def _safe_fetch_url\(",     # 已被 netguard.safe_fetch_url 取代
    r"^def _is_local_read_allowed\(",    # 已被 netguard.is_local_read_allowed 取代
)

# --------------------------------------------------------------------------- 头部

_MSG_HEADER = '''"""msg 域工具（``/Msg/*``）—— 从旧 ``plugin/tools.py`` **逐字搬来**。

## 为什么是逐字搬，而不是重写

``register_msg_tools`` 是 **660 行踩过坑的生产逻辑**：每个 vendor 端点的参数名、
``ret`` 码判定、mmx CLI 调用链、语音/视频的 base64 兜底分支……手抄一遍就是给
「抄错一个参数名」开后门，而抄错的后果**只在运行期暴露**（厂商多半回一个与真因
无关的错误码）。所以这里选择生成 + 比对，而不是重打字。

本文件由 ``tools/port_tools_domain.py`` 生成，``tools/verify_tools_port.py``
用 AST 逐函数与旧实现比对 —— **任何未登记在案的字面改动都会让它红。**

## 与旧实现的全部差异（就这三条）

1. 函数体内 ``from .api_client import WppApiError``
   → ``from ..core.api_client import WppApiError``（包结构变了）
2. 旧的**同文件模块级** ``_get_client`` / ``_mk_handler`` / ``_resolve_to_base64``
   现在由本文件头部提供别名 / 包装：前两个指向 :mod:`.registry`，第三个见下。
3. **W7-5（2026-10-02）**：四处朋友圈守卫的"开关"那半收口到 :mod:`.policy` 的运行期
   门禁（头部因此多一个 ``policy`` 导入）。旧实现在这四处调 ``adapter.get_account(aid)``
   —— **适配器上没有这个方法**（全树只在旧原文这 4 行出现过）⇒ 真机必然
   ``AttributeError`` ⇒ 回话是「调用失败: …」而不是那句"未启用"，且**白名单那一支
   永远走不到**（等于白名单形同没有）。⇒ 这是**第二处"改了语义"的有意偏离**，
   详见 :data:`REWRITES` 里 ``msg.py`` 那条与 ``plugin/toolkit/policy.py`` 的
   :data:`FRIEND_CIRCLE_PUBLISH_GATE`。

## ``_resolve_to_base64`` 为什么要包一层（别图省事直接引用）

新 ``platform.outbound.resolve_to_base64`` 失败时返回**空串**；旧实现同名函数
失败时 **raise**。msg 域 5 个调用点的历史契约是「要么拿到 base64，要么抛错」。
若直接引用返空串的那个版本，``send_file`` 会把 ``Base64=""`` 交给厂商，厂商回一个
**与真因无关**的错误码 —— 正是老板最恨的那类事故（"日志说成功 / 报错指向错的根因"，
参考出站语音那个 Code=-2 文案为 INVALID_CREDENTIAL 的坑）。

所以这里把「本该拿到内容却拿到空」重新抛成 ``WppApiError``，同时**保留**旧实现的
两条原样返回分支：空串入参、以及既非 URL 也非路径者（当作已经是 base64）。
唯一**有意**的偏离：``data:`` 无逗号的畸形输入，旧实现原样返回，这里抛错。
"""

from __future__ import annotations

import logging

from . import policy, registry

log = logging.getLogger(__name__)

#: 别名 —— 为的是让下面 660 行函数体**一字不改**（见模块 docstring 差异 2）
_get_client = registry.get_client
_mk_handler = registry.mk_handler


async def _resolve_to_base64(ref: str) -> str:
    """旧契约的**抛错版**包装（别换成直接引用 outbound 那个，见模块 docstring）。"""
    # ★ 依赖延迟到调用期：`core/api_client.py` 顶层 `import aiohttp`，
    #   而 `register()` 会被 `plugin_validate` 在**隔离 probe 子进程**里整体跑一遍
    #   （那个解释器没有 aiohttp）⇒ 本模块的**模块级**导入必须只依赖标准库 +
    #   `registry`。旧的 `_resolve_to_base64` 也是这么在函数内 import 的。
    from ..core.api_client import WppApiError
    from ..platform import outbound

    ref = (ref or "").strip()
    if not ref:
        return ""  # 旧实现末支：空串原样返回，不抛（send_file 允许 fileUrl 缺省）
    if not (ref.startswith("data:") or ref.startswith("http") or ref.startswith("/")):
        return ref  # 旧实现末支：既非 URL 也非路径 ⇒ 当作已是 base64
    b64 = await outbound.resolve_to_base64(ref)
    if not b64:
        raise WppApiError(f"媒体解析失败（取不到内容，拒绝发送空媒体）: {ref[:60]}")
    return b64

'''

_JARGON_HEADER = '''"""jargon 黑话查询工具（``wpp_jargon_query``）—— 从旧 ``plugin/tools.py`` 逐字搬来。

本文件由 ``tools/port_tools_domain.py`` 生成。差异只有一条（登记在案）：

    函数体内 ``from .jargon import ...`` → ``from ..inbound.jargon import ...``

因为 ``plugin/jargon.py`` 已迁到 ``plugin/inbound/jargon.py``（该模块零内部依赖，
是纯 stdlib，所以搬动是安全的 —— 见 ``plugin/inbound/__init__.py``）。
"""

from __future__ import annotations

import json
import logging

from . import registry

log = logging.getLogger(__name__)

_mk_handler = registry.mk_handler

'''

HEADERS: dict[str, str] = {"msg.py": _MSG_HEADER, "jargon_tool.py": _JARGON_HEADER}

#: W7-5 换掉那半"开关"之后 ``msg.py`` 四处守卫的样子（见下面 ``msg.py`` 那条登记）。
_GUARD_NEW = (
    '        # guard：``friendCirclePublishEnabled`` 那条**开关**由 policy 的运行期门禁把关\n'
    '        #   （所有调用路径都过 ``hermes_contract.wrap_ctx``，收口到 tools/policy.py）。\n'
    '        #   这里只剩**按调用方**的白名单判定。\n'
    '        #   ⚠️ 本行原先是 ``adapter.get_account(aid)`` —— 适配器上**没有这个方法**\n'
    '        #   （全树只在这 4 行出现过）⇒ 真机必然 AttributeError，回话是"调用失败: …"而\n'
    '        #   不是那句"未启用"，且白名单这一支**永远走不到**。2026-10-02 修。\n'
    '        acct_cfg = policy.raw_account_cfg(adapter, aid)\n'
    '        if acct_cfg is None:\n'
    '            return {"error": "读不到账号配置，拒绝发布朋友圈"}\n'
)

#: 2026-10-18：发送类工具的**统一返回**（生成期注入 ``msg.py`` 的模块级 helper）。
#:
#: 蓝本 15 处各判各的（``BaseResponse.ret`` 或 ``Code``），都**不看**
#: ``Data.List[].Ret`` —— 厂商回 ``Code=0`` 但逐条 ``Ret=-2``（被拒收）时工具报
#: ``ok:true``，模型以为发出去了、群里什么都没有（v1.5.6 ``SEND-LIST-RET`` 的同型
#: 复发）。判据只许有一处：``platform.outbound.send_ok``（适配器侧早就这么判）。
_SEND_PAYLOAD_HELPER = '''def _send_payload(resp, *, msg_id=None, **extra) -> dict:
    """发送类工具的统一返回 —— **成败判据只有一处**：``platform.outbound.send_ok``。

    ⚠️ 旧实现各自判 ``BaseResponse.ret == 0``（或 ``Code == 0``），**不看**
    ``Data.List[].Ret``：厂商回 ``Code=0`` + ``BaseResponse.ret=0``、但逐条结果是
    ``Ret=-2``（被拒收）时，工具报 ``ok:true`` —— 模型以为已经发出去，群里什么也没有
    （v1.5.6 ``SEND-LIST-RET`` 事故的同型复发：适配器侧早就改用 ``send_ok``，
    工具面 15 处漏了）。判据不许在调用点另写一份。
    """
    from ..platform import outbound

    ids = outbound.extract_outbound_msg_ids(resp)
    ok = outbound.send_ok(resp)
    payload: dict = {"ok": ok, "msgId": msg_id or ids.msg_id}
    if ids.new_msg_id:
        payload["newMsgId"] = ids.new_msg_id
    if not ok:
        payload["error"] = outbound.send_error_text(resp)
    payload.update(extra)
    return payload


'''

#: 逐文件的**登记在案的重写**：``(旧串, 新串, 期望出现次数)``。
#: 函数体除这些精确串之外必须逐字相同 —— 次数也钉死，蓝本多出一条同类语句就会报错。
#: ``tools/verify_tools_port.py`` 把同一张表套到旧原文上再比对 —— 单一真源。
REWRITES: dict[str, tuple[tuple[str, str, int], ...]] = {
    # ⚠️ 2026-10-02 全模块**唯一**一处"改了语义"的登记（其余都只是改名/改导入路径）：
    #    ``register_generic_tools`` 内联的 ``_handler`` 原来把 ``adapter_getter`` 写成
    #    **同名形参的默认值**，而 Hermes 派发时只传模型给的参数字典 ⇒ 该形参永远取默认值
    #    ``noop_adapter_getter`` ⇒ **219 个通用工具全部回「adapter 未就绪」，一次厂商请求
    #    都不发**，且不抛异常、日志干净（静默）。改法是删掉那个形参、直接闭合外层 getter。
    #    完整理由见 ``plugin/toolkit/registry.py`` 模块 docstring 的「2026-10-02 例外」一节。
    #    ⇒ 这一条是**有意偏离蓝本**：能真调 > 逐字保真。
    "registry.py": (
        (
            "        async def _handler(adapter_getter=_get_noop_adapter_getter, "
            "endpoint=endpoint, method=method, **params):\n",
            "        async def _handler(endpoint=endpoint, method=method,\n"
            "                           transform=td.get(\"transform\"), **params):\n",
            1,
        ),
        # 2026-10-17：请求体整形钩子（tenpay 纠错）。``transform`` 也必须是**默认参数**，
        # 否则在循环结束后被调用时会晚绑定到最后一条 ``td``（每条工具按别人的规则整形）。
        # 生成物与 schema 保持不动，规则全在 ``plugin/toolkit/policy.py`` 的 PATCH_TOOLS。
        (
            "            body = {k: v for k, v in params.items() if v is not None}\n",
            "            body = {k: v for k, v in params.items() if v is not None}\n"
            "            if callable(transform):\n"
            "                body = transform(body)\n",
            1,
        ),
    ),
    "msg.py": (
        (
            "    from .api_client import WppApiError\n",
            "    from ..core.api_client import WppApiError\n",
            1,
        ),
        # ⚠️ 2026-10-02 W7-5：**第二处"改了语义"的登记**（第一处是 registry.py 那条）。
        #    蓝本的四处朋友圈守卫调 ``adapter.get_account(aid)`` —— 适配器上**没有这个方法**
        #    （全树只在旧原文这 4 行出现过）⇒ 真机必然 ``AttributeError`` ⇒ 回话是
        #    「调用失败: …」而不是那句"未启用"，且**白名单那一支永远走不到**（等于白名单
        #    形同没有：任何 caller 都能发）。同时"开关"这半收口到
        #    ``tools/policy.py`` 的运行期门禁（``FRIEND_CIRCLE_PUBLISH_GATE``，所有调用
        #    路径都过 ``hermes_contract.wrap_ctx``）⇒ 工具里只剩**按调用方**的白名单判定，
        #    读不到配置一律 fail-closed（``if acct_cfg is None: return 拒绝``）。
        #    完整理由见 ``plugin/toolkit/msg.py`` 里就地写的那段注释与
        #    ``plugin/toolkit/policy.py`` 的 :data:`FRIEND_CIRCLE_PUBLISH_GATE`。
        #    ⇒ 这一条同样是**有意偏离蓝本**：能拦住 > 逐字保真。
        (
            '        # guard：friendCirclePublishEnabled + 白名单（对齐 OpenClaw v1.3.41）\n'
            '        acct_cfg = adapter.get_account(aid) if adapter else {}\n'
            '        if not acct_cfg.get("friendCirclePublishEnabled"):\n'
            '            return {"error": "朋友圈发布未启用 (friendCirclePublishEnabled=false)"}\n',
            '        # guard：friendCirclePublishEnabled + 白名单（对齐 OpenClaw v1.3.41）\n'
            + _GUARD_NEW,
            1,
        ),
        # 其余三处（video / text / ai）没有那行旧注释；ai 上面那行
        # ``# guard（对齐 publish_circle_images）`` **保留**，所以锚点从 ``acct_cfg`` 起。
        (
            '        acct_cfg = adapter.get_account(aid) if adapter else {}\n'
            '        if not acct_cfg.get("friendCirclePublishEnabled"):\n'
            '            return {"error": "朋友圈发布未启用 (friendCirclePublishEnabled=false)"}\n',
            _GUARD_NEW,
            3,
        ),
        # ⚠️ 2026-10-18：**第三处"改了语义"的登记**。蓝本的 ``quote_reply`` 是个假引用 ——
        #    它只调 ``client.send_text``，既没有 type=57 refermsg，也不会 @ 被回复人
        #    （TS ``send/quote-reply.ts`` 有完整实现：DB 回查 → refermsg → ShareLink）。
        #    这里逐字对齐 TS 的主路径；``policy.py`` 的注释与 ``tests/toolkit`` 里
        #    ``test_quote_reply_*`` 都锁着这段行为。
        (
            """    # quoteReply: 引用回复
    async def quote_reply(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        content = params["content"]
        aid, client = acct(adapter, params)
        # 简化引用：走 SendTxt（完整引用在阶段4 实现）
        resp = await client.send_text(to_wxid, content)
        return {"ok": resp.get("Success", False), "resp": resp.get("Code")}
""",
            """    # quoteReply: 引用回复（对齐 TS src/send/quote-reply.ts）
    async def quote_reply(**params):
        adapter = adapter_getter()
        to_wxid = params["toWxid"]
        content = params["content"]
        msg_id = str(params.get("msgId") or "")
        new_msg_id = str(params.get("newMsgId") or "")
        aid, client = acct(adapter, params)
        if not msg_id and not new_msg_id:
            return {"ok": False, "error": "msgId/newMsgId 至少填一个（被引用消息的 id）"}
        # ★ 2026-10-18：原实现是假引用 —— 只发了一条普通 SendTxt，既没有 type=57
        #   refermsg，也不会 @ 被回复人。这里对齐 TS 的主路径：
        #   ① 回查被引用消息（**不限方向**：被引用的可能是 bot 自己的回复）；
        #   ② 群聊补 ``@昵称 `` 前缀（唯一产出点 inbound.quote_ref.build_mention_prefix）；
        #   ③ 生成 type=57 XML，走 ``/Msg/ShareLink {ToWxid,Type:5,Xml}``。
        from ..data import repo_messages
        from ..inbound import quote_ref
        from ..platform import outbound

        svrid = new_msg_id or msg_id
        ident = quote_ref.QuoteIdentity()
        try:
            row = repo_messages.get_message_by_id_or_new_id(
                aid, msg_id=msg_id or None, new_msg_id=new_msg_id or None)
            ident = quote_ref.resolve_quote_identity(row)
        except Exception as exc:  # noqa: BLE001 —— 查库失败只降级，不挡发送
            log.debug("[WPP:%s] quote_reply 回查降级 %s: %s", aid, svrid, exc)
        name = ident.display_name
        if ident.fromusr and (not name or name == ident.fromusr):
            try:
                name = adapter._contact_name(aid, ident.fromusr) if adapter is not None else ""
            except Exception:  # noqa: BLE001 —— 昵称只是装饰
                name = ""
        prefix = quote_ref.build_mention_prefix(to_wxid, name, ident.fromusr)
        xml = outbound.build_quote_reply_xml(
            reply_content=f"{prefix}{content}".strip(), svrid=svrid, fromusr=ident.fromusr)
        resp = await client.call("/Msg/ShareLink",
                                 {"ToWxid": to_wxid, "Type": 5, "Xml": xml})
        ok = outbound.send_ok(resp)
        ids = outbound.extract_outbound_msg_ids(resp)
        if ok:
            # 任意渠道发送都要入库（TS v1.3.16 OUTBOUND-PERSIST）：否则之后"引用 bot
            # 这条回复"会查不到锚点。入库失败只留痕，不把发送结果翻成失败。
            try:
                repo_messages.save_message(
                    aid, direction="outbound",
                    peer_kind="group" if to_wxid.endswith("@chatroom") else "direct",
                    peer_id=to_wxid, msg_id=ids.msg_id,
                    new_msg_id=ids.new_msg_id, msg_type="quote", content=content)
            except Exception as exc:  # noqa: BLE001 —— 入库是附带动作
                log.warning("[WPP:%s] quote_reply 入库失败（不影响发送）: %s", aid, exc)
        return {"ok": ok, "msgId": ids.msg_id, "newMsgId": ids.new_msg_id}
""",
            1,
        ),
        (
            """        ("wpp_quote_reply", "引用回复微信消息", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string"},
                "content": {"type": "string"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "content"],
        }, quote_reply),
""",
            """        ("wpp_quote_reply", "引用回复微信消息. 自动从 DB 取被引用消息构造完整引用卡片（文本/图片通用），群聊自动 @ 被回复人.", {
            "type": "object",
            "properties": {
                "toWxid": {"type": "string", "description": "目标 wxid / 群 id"},
                "content": {"type": "string", "description": "回复内容"},
                "msgId": {"type": "string", "description": "被引用消息的 msgId（DB 回查用）"},
                "newMsgId": {"type": "string", "description": "被引用消息的 newMsgId（可选，svrid 优先用它）"},
                "account_id": {"type": "string"},
            },
            "required": ["toWxid", "content", "msgId"],
        }, quote_reply),
""",
            1,
        ),
        # ⚠️ 2026-10-18：**第四处"改了语义"的登记**。``aid`` 原来直接取
        #    ``params.get("account_id") or "default"`` —— ``default`` 只是 schema 占位符；
        #    账号文件叫别的名字（单账号部署的常态）时，``authorize_account("default")``
        #    会把**所有 /Msg 工具**判成"账号不存在"。改走 ``registry.resolve_account_id``：
        #    仅"请求的就是 default + 恰好一个账号已连接"时回退，显式别的名字绝不回退。
        (
            '    def acct(adapter, params):\n'
            '        aid = params.get("account_id") or "default"\n',
            '    def acct(adapter, params):\n'
            "        # ``default`` 是 schema 的**占位哨兵**，不是真账号名：单账号部署里账号文件\n"
            '        # 叫 ``boss2`` 时，按字面量 ``default`` 去查存在性会被判"账号不存在" ⇒ 所有\n'
            "        # /Msg 工具全拒。先解析成**已连接的那个**（仅单账号 + 请求的就是 default 时回退）。\n"
            '        aid = registry.resolve_account_id(adapter, params.get("account_id"))\n',
            1,
        ),
        # ⚠️ 2026-10-18：**第五处"改了语义"的登记** —— 发送成败判据收口到
        #    ``_send_payload`` / ``platform.outbound.send_ok``（见常量注释）。
        #    helper 注入在 ``register_msg_tools`` 之前（模块级，只此一份）。
        (
            "def register_msg_tools(",
            _SEND_PAYLOAD_HELPER + "def register_msg_tools(",
            1,
        ),
        (
            '        return {"ok": br.get("ret") == 0 or msg_id is not None, "msgId": msg_id}\n',
            "        return _send_payload(resp, msg_id=msg_id)\n",
            3,
        ),
        (
            '        return {"ok": br.get("ret") == 0 or msg_id is not None, "msgId": msg_id,\n'
            '                "image_path": out_path}\n',
            "        return _send_payload(resp, msg_id=msg_id, image_path=out_path)\n",
            1,
        ),
        (
            '        return {"ok": br.get("ret") == 0, "msgId": data.get("Msgid"), '
            '"voice_path": out_path}\n',
            '        return _send_payload(resp, msg_id=data.get("Msgid"), '
            'voice_path=out_path)\n',
            1,
        ),
        (
            '        return {"ok": resp.get("Code") == 0 or br.get("ret") == 0, '
            '"msgId": data.get("Newmsgid"), "code": resp.get("Code")}\n',
            '        return _send_payload(resp, msg_id=data.get("Newmsgid"), '
            'code=resp.get("Code"))\n',
            3,
        ),
        (
            '        return {"ok": resp.get("Code") == 0 or br.get("ret") == 0,\n'
            '                "msgId": data.get("Newmsgid"), "code": resp.get("Code"),\n'
            '                "image_count": len(base64_list)}\n',
            '        return _send_payload(resp, msg_id=data.get("Newmsgid"), '
            'code=resp.get("Code"),\n'
            '                             image_count=len(base64_list))\n',
            1,
        ),
        (
            '        return {"ok": br.get("ret") == 0, "msgId": data.get("Newmsgid")}\n',
            '        return _send_payload(resp, msg_id=data.get("Newmsgid"))\n',
            6,
        ),
        (
            '        return {"ok": br.get("ret") == 0, "msgId": data.get("Msgid")}\n',
            '        return _send_payload(resp, msg_id=data.get("Msgid"))\n',
            1,
        ),
        (
            '            return {"ok": br.get("ret") == 0, "degraded": "file", '
            '"msgId": data.get("Msgid")}\n',
            '            return _send_payload(resp, msg_id=data.get("Msgid"), '
            'degraded="file")\n',
            1,
        ),
        (
            '        return {"ok": resp.get("Success", False), "resp": resp.get("Code")}\n',
            '        return _send_payload(resp, code=resp.get("Code"))\n',
            3,
        ),
        # 读端点（拉某人的消息历史）：``Code`` 缺失属正常形状（见
        # ``api_client._request``），别拿 ``Success`` 单独定成败。
        (
            '        return {"ok": resp.get("Success", False), "count": '
            'len((resp.get("Data") or {}).get("messages") or [])}\n',
            "        # 读端点：Code 缺失属正常形状（见 api_client._request），"
            "别用 Success 单独定成败\n"
            '        return {"ok": resp.get("Success") is not False and '
            'resp.get("Code") in (0, 200, None),\n'
            '                "count": len((resp.get("Data") or {}).get("messages") or [])}\n',
            1,
        ),
    ),
    "jargon_tool.py": ((
        "        from .jargon import query_jargon as qj, list_hot_jargon\n",
        "        from ..inbound.jargon import query_jargon as qj, list_hot_jargon\n",
        1,
    ),),
    "mcp.py": (
        # 6 处 handler 内的 DB 取用：旧扁平 ``plugin/db.py`` → 新的 ``data/repo_messages``。
        # 绑定成 ``db`` 这个名字是为了**不动调用点写法**。
        ("        from . import db\n",
         "        from ..data import repo_messages as db\n", 6),
        # 两个调用点改名以复用既有实现（**没有**为它们另写一份 SQL）——
        # 签名逐一核对过：query_contact_by_wxid(account_id, wxid) ↔ get_contact(...)，
        # query_chatroom(account_id, chatroom_id) ↔ get_chatroom(...)
        ("db.query_contact_by_wxid(account_id, cid)",
         "db.get_contact(account_id, cid)", 1),
        ("db.query_chatroom(account_id, gid)",
         "db.get_chatroom(account_id, gid)", 1),
        # 注册循环：旧名带前导下划线，新树在 registry 里已去掉
        ("handler=_mk_handler(fn)",
         "handler=mk_handler(fn)", 1),
    ),
}

#: **已申报的有意差异**（AST 层）。键是文件名，值是 ``{函数名: 理由}``。
#:
#: 本表的存在是为了让 ``verify_tools_port.py`` 继续有牙齿：**未登记的差异恒红**，
#: 登记过的差异会以 ``ℹ️ 已申报`` 的形式打印理由。不要把"懒得修"写进来 ——
#: 每一条都必须指向 TS 侧的出处或一个可复现的 bug。
DECLARED_AST_DIFFS: dict[str, dict[str, str]] = {
    "msg.py": {
        "send_ai_image": (
            "2026-10-18 生图后端从 ``mmx`` CLI（MiniMax image-01）改为 Hermes "
            "``image_gen`` 注册表（默认百炼 qwen-image）：旧实现与 Hermes 的模型"
            "供应商体系脱节——即便 ``image_gen.provider`` 已配百炼，工具仍走 MiniMax。"
            "新实现 ``await asyncio.to_thread(provider.generate, ...)``，并保留 provider "
            "缓存失败时下载 URL 的兜底；发送链路（base64 → ``client.send_image``）不变。"
        ),
        "publish_circle_ai": (
            "同上：9 张配图逐张改走 Hermes ``image_gen`` 注册表，不再 fork ``mmx``；"
            "白名单判定、发布链路与入参语义保持不变。"
        ),
        "register_msg_tools": (
            "① 两处工具描述文本随生图后端切换更新：``wpp_send_ai_image`` / "
            "``wpp_fc_publish_ai`` 从「mmx image-01」改为「Hermes image_gen → 百炼 "
            "qwen-image」；② 2026-10-18 超长回复修复：嵌套的 ``send_text`` 先走 "
            "``outbound.chunk_text`` 分片（微信个人号 >2000 字会被静默截断），"
            "``quote_reply`` 在引用气泡装不下（>500 字）时改发普通文本而不是截断。"
            "工具名、schema、toolset 均未变。"
        ),
    },
    "registry.py": {
        "get_client": (
            "2026-10-18 单账号兼容：请求 ``default`` 且恰好一个账号连接时回退到该账号 "
            "（``resolve_account_id``）。否则账号文件叫 boss2 的部署里，schema 默认值 "
            "会让所有工具恒回“账号 default 未连接”。显式传其他名字绝不回退。"
        ),
        "mk_handler": (
            "2026-10-18 工具返回值统一走 ``toolout.dump_capped`` / ``dump_error``："
            "旧的 ``json.dumps(...)[:4000]`` 会把 JSON 从中间切断（实测 "
            "``wpp_group_list`` 以 ``\"access_type\":`` 结尾），模型拿到的是语法非法"
            "的半截文本；失败回话也统一成 ``ok:false`` 的 JSON，超时这类空消息异常"
            "至少还能看出类型。行为只在超限/异常路径变化，正常返回逐字节相同。"
        ),
        "generic_handler": (
            "同上：走 ``resolve_account_id``；账号未连接时的回话列出已连接账号，"
            "便于模型/运维分辨“笔误”与“真没连上”。"
        ),
        "register_generic_tools": (
            "2026-10-18 三处有意改动：① 账号解析走 ``resolve_account_id``；"
            "② schema 透传 items/enum/format/边界/default（只搬 type/desc 会让 "
            "array 丢 items、enum 全丢，模型与厂商 Go 结构体对不上）；"
            "③ ``/Webhook/Set|Test`` 在请求体补 authcode（TS ``webhook.ts`` 契约；"
            "transform 拿不到客户端实例，只能在这个唯一出口补）。"
        ),
    },
    "jargon_tool.py": {
        "register_jargon_tools": (
            "2026-10-18 对齐 TS ``jargon-meta.ts`` 的 ``queryJargon(groupId, text?)``："
            "新增可选 ``text`` —— 用 ``containsJargon`` 语义（纯 ASCII 词条走词边界、"
            "中文走子串）从群热词表中筛出命中项；无命中时退回热词 TOP（供 AI 学习群内"
            "语言）而不是空结果。``term`` 精确查询与无参热词表两条旧路径逐字未动。"
        ),
    },
    "mcp.py": {
        "register_mcp_tools": (
            "2026-10-18 对齐 TS ``mcp-meta.ts``：6 个查询工具改为 **vendor MCP 优先**"
            "（``127.0.0.1:28062/mcp``，vendor ``httpport``），不可用时回退本地缓存表；新增 "
            "``wpp_mcp_account_status``；全部工具按 ``mcpEnabled`` 门控；"
            "``wpp_mcp_search`` 改为 vendor 的通用搜索语义（query/category/offset/limit）。"
            "旧实现的本地 SQL 语义保留在 fallback 分支里，未丢失能力。"
        ),
    },
}

#: **已申报的注册差异**：``{label: {工具名: 理由}}``（label 与 verify 脚本的
#: ``msg`` / ``mcp`` / ``jargon`` 对应）。schema 或 toolset 与旧实现不同是允许的，
#: 但必须在这里说清"为什么不同"。
DECLARED_REGISTRATION_DIFFS: dict[str, dict[str, str]] = {
    "msg": {
        "wpp_send_ai_image": (
            "仅描述变化：生图后端从 mmx（MiniMax image-01）改为 Hermes image_gen "
            "注册表（默认百炼 qwen-image）。schema / toolset 未变。"
        ),
        "wpp_fc_publish_ai": (
            "同上：配图后端改为 Hermes image_gen（百炼 qwen-image）。schema / toolset 未变。"
        ),
        "wpp_quote_reply": (
            "旧实现是假引用（只发普通 SendTxt）。2026-10-18 按 TS "
            "``send/quote-reply.ts`` 重写：DB 回查被引用消息、群聊 @ 被回复人、"
            "type=57 refermsg XML 走 ``/Msg/ShareLink``；schema 因此新增必填 "
            "``msgId`` 与可选 ``newMsgId``。"
        ),
    },
    "jargon": {
        "wpp_jargon_query": (
            "对齐 TS ``jargon-meta.ts``：schema 新增可选 ``text``（消息文本 → 返回命中"
            "黑话，推荐用法），``term`` 降为精确查询备选，二者都不填则返回群热词 TOP。"
            "``groupId`` 仍必填；toolset 未变。"
        ),
    },
    "mcp": {
        "wpp_mcp_list_contacts": "TS ``mcp-meta.ts`` 语义：分页（offset/limit）+ vendor 优先；描述同步。",
        "wpp_mcp_list_groups": "同上：补 ``offset``，描述改为分页语义。",
        "wpp_mcp_recent_messages": "同上：描述对齐 TS（最近 24h、最多 500 条）。",
        "wpp_mcp_get_contact": "同上：``id`` 可来自 list/search 或 wxid，描述对齐。",
        "wpp_mcp_get_group": "同上：描述对齐 TS 的返回字段。",
        "wpp_mcp_search": (
            "TS 的 ``wechat_search`` 是**通用微信搜索**（公众号/文章/小程序/视频号/"
            "朋友圈…），不是本地联系人 LIKE 搜索。schema 改为 query/category/offset/"
            "limit（``query`` 与 ``category`` 均必填，对齐 TS non-Optional）；本地联系人"
            "搜索只保留为 category=all/contact 时的 vendor 不可用 fallback。"
        ),
    },
}

#: **已申报的新增工具**：``{label: {工具名: 理由}}``。
DECLARED_EXTRA_TOOLS: dict[str, dict[str, str]] = {
    "mcp": {
        "wpp_mcp_account_status": (
            "TS ``mcp-meta.ts`` 的第 7 个只读工具（vendor ``wechat_account_status``）；"
            "旧 py2 实现漏了它。没有本地等价物 ⇒ vendor 不可用时明确报错。"
        ),
    },
}

#: 逐文件的**函数改名表**（旧名 → 新名）。仅 ``registry.py``：旧机器的 5 个函数
#: 都去掉了前导下划线（``_get_client`` → ``get_client`` 等）。
#: 验算器在 AST 上做改名（不碰字符串/注释），再比对函数体。
RENAMES: dict[str, dict[str, str]] = {
    "registry.py": {
        "_get_client": "get_client",
        "_mk_handler": "mk_handler",
        "_mk_generic_handler": "generic_handler",
        "_register_generic_tools": "register_generic_tools",
        "_get_noop_adapter_getter": "noop_adapter_getter",
    },
}

#: 本脚本负责写盘的目标（其余区间只用作比对基准）
GENERATED: tuple[str, ...] = ("msg.py", "jargon_tool.py")


# --------------------------------------------------------------------------- 切分

def _slice(text: str, start_re: str, end_re: str | None) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(start_re, l)), None)
    if start is None:
        raise SystemExit(f"❌ 找不到起锚 {start_re!r} —— 蓝本 tools.py 变了，先重新评估搬家地图")
    if end_re is None:
        return start, len(lines)
    end = next((i for i, l in enumerate(lines) if i > start and re.match(end_re, l)), None)
    if end is None:
        raise SystemExit(f"❌ 找不到止锚 {end_re!r}")
    return start, end


def legacy_body(name: str, text: str) -> str:
    """旧原文里该目标文件对应的区间（**未套重写**）—— 供生成与比对共用。"""
    a, b = _slice(text, *SPLITS[name])
    return "".join(text.splitlines(keepends=True)[a:b]).strip("\n")


def apply_rewrites(name: str, body: str) -> str:
    for old, new, want in REWRITES.get(name, ()):
        got = body.count(old)
        if got != want:
            raise SystemExit(
                f"❌ {name}: 登记的重写目标出现 {got} 次（期望 {want}）: {old!r}\n"
                "   蓝本变了（语句增删 / 行首缩进变了）—— 先核对，别盲生成"
            )
        body = body.replace(old, new)
    return body


def render(name: str, body: str) -> str:
    return HEADERS[name] + "\n" + body + "\n"


def main() -> int:
    text = LEGACY.read_text(encoding="utf-8")

    for pattern in MUST_SKIP:
        if not re.search(pattern, text, re.M):
            raise SystemExit(f"❌ 预期「不搬」的 {pattern!r} 在原文里不存在 —— 蓝本已变，别盲搬")

    if "--range" in sys.argv:
        for name, (s, e) in SPLITS.items():
            a, b = _slice(text, s, e)
            mark = "生成" if name in GENERATED else "基准"
            print(f"  {mark} {name:18s} 原文 [{a + 1}, {b}] 共 {b - a} 行")
        return 0

    if "--check" in sys.argv:
        # 逐字节比对已失效（两个生成物里有登记在案的手工改动，见模块 docstring）。
        # 委托 AST + 注册对账：允许申报过的差异，未申报的漂移照样红。
        import subprocess

        print("  ℹ️  生成物含登记在案的手工改动 ⇒ 改用 AST 等价 + 注册对账门禁")
        return subprocess.run(
            [sys.executable, str(ROOT / "tools" / "verify_tools_port.py")],
            cwd=str(ROOT),
        ).returncode

    OUT_DIR.mkdir(exist_ok=True)
    force = "--force" in sys.argv
    drift: list[str] = []
    for name in GENERATED:
        path = OUT_DIR / name
        have = path.read_text(encoding="utf-8") if path.exists() else ""
        want = render(name, apply_rewrites(name, legacy_body(name, text)))
        if have and have != want:
            drift.append(name)
            if not force:
                continue
        path.write_text(want, encoding="utf-8")
        n = len(want.splitlines())
        print(f"  ✅ 已生成 plugin/toolkit/{name:16s} {n:4d} 行")
    if drift and not force:
        print(f"  ❌ 拒绝覆盖手工改动: {', '.join(drift)}")
        print("     这些文件现在由手工维护；语义等价由 tools/verify_tools_port.py 保证。")
        print("     确需按旧蓝本重生成（会丢失手工改动）再跑 --force。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
