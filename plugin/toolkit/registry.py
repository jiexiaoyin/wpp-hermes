"""工具注册原语 —— 从旧扁平 ``plugin/tools.py`` 第 14–103 行**逐字**搬来，只去掉前导下划线。

| 旧名                        | 新名                     |
|-----------------------------|--------------------------|
| ``_get_client``             | ``get_client``           |
| ``_mk_handler``             | ``mk_handler``           |
| ``_mk_generic_handler``     | ``generic_handler``      |
| ``_register_generic_tools`` | ``register_generic_tools`` |
| ``_get_noop_adapter_getter``| ``noop_adapter_getter``  |

**只改了名字与导入**，函数体一个字没动（``register_generic_tools`` 里
``adapter_getter=_get_noop_adapter_getter`` 的默认值同步改名）。

## ⚠️ 2026-10-02 例外：``register_generic_tools`` 的 ``_handler`` 改过（唯一一处）

原来它是 ``_handler(adapter_getter=noop_adapter_getter, endpoint=…, **params)`` ——
参数名与**外层** ``register_generic_tools(ctx, adapter_getter, …)`` 撞名，而调用方从不传它
（handler 只收到模型给的参数字典）⇒ 永远取默认值 noop ⇒ **219 个通用工具全部返回
「adapter 未就绪」，一次厂商请求都不发**，且不抛异常、日志干净。
这与 :mod:`plugin.toolkit.hermes_contract` 的调用契约是**两个独立的**静默缺陷，
所以"蓝本保真"在这里让位于"能真调"：改法见下面的行内注释。
``noop_adapter_getter`` 仍作为公开出口保留（但已不再是任何 handler 的默认值）。

## 约束

- **无重依赖**：只 import stdlib ``json`` / ``logging``。handler 里要用的
  pymysql / node / 网络一律在**函数体内**导入（Hermes ``plugin_validate`` 会在隔离
  子进程里 import 整个插件）。
- **包内相对导入**：本插件禁止裸名导入（``plugin/platform/`` 会遮蔽 stdlib ``platform``）。
- ``register_generic_tools`` 里 ``toolset="wechatpadpro"`` 是**字面量**，不是常量（保持原样）。
"""

from __future__ import annotations

import logging

from .toolout import dump_capped, dump_error

log = logging.getLogger(__name__)

#: 本插件所有工具注册用的 toolset 名。
#: ⚠️ 只是**常量出口**：注册点仍写 ``toolset="wechatpadpro"`` **字面量**
#: （旧实现如此，别替换成这个常量 —— 换掉就改了注册行为的可读基线）。
TOOLSET = "wechatpadpro"


def resolve_account_id(adapter, account_id: str | None) -> str:
    """把请求的账号 id 解析成**已连接**的那个。

    单账号部署里没人会去填 ``account_id``（schema 默认 ``default``），而账号文件
    可能叫 ``boss2`` —— 直接按字面量取 client 会恒返回"账号 default 未连接"。
    只在**请求的就是 default**且**恰好只有一个账号连接**时回退；显式传了别的
    名字绝不回退（那会把一次笔误变成跨账号误发）。
    """
    clients = getattr(adapter, "_clients", None) or {}
    aid = str(account_id or "default")
    if aid in clients:
        return aid
    if aid == "default" and len(clients) == 1:
        return next(iter(clients))
    return aid


def get_client(adapter, account_id: str):
    """从 adapter 拿指定账号的 WppClient（单账号下 ``default`` 自动落到唯一账号）。"""
    return (adapter._clients or {}).get(resolve_account_id(adapter, account_id))


def _resp_ok(resp) -> bool:
    """厂商响应的成败判据 —— **只此一份**。

    ⚠️ 旧写法是 ``resp.get("Success", False)``：厂商回 ``{"Code": 0, "Data": {...}}``
    （**没有** ``Success`` 键，这是 ``api_client._request`` 明确容忍的正常形状）时，
    219 个通用工具一律报 ``ok:false`` 而 ``code`` 是 0 —— 模型看到"失败"却拿着 0，
    只能瞎猜甚至重发。判据改成 ``platform.outbound.send_ok``：它看 ``Code`` ∈
    {0,200,None}、并且**逐条**看 ``Data.List[].Ret``（被拒收 ⇒ 失败）。
    """
    from ..platform import outbound  # 延迟导入：避免 platform 包与 toolkit 互相 import

    return outbound.send_ok(resp)


def mk_handler(fn):
    """包装 handler，把异常转成工具返回字符串。"""
    async def handler(**params):
        try:
            result = await fn(**params)
            return dump_capped(result)
        except Exception as e:  # noqa: BLE001
            # 失败必须是**机器可读**的 JSON：`str(TimeoutError())` 是空串，旧写法
            # 返回的 "调用失败: " 让模型连"超时还是参数错"都分不出来。
            return dump_error(f"{type(e).__name__}: {e}")
    return handler


def generic_handler(endpoint: str, method: str = "POST"):
    """通用 vendor 工具 handler：调 client.call(endpoint, body)。"""
    async def handler(adapter_getter, **params):
        adapter = adapter_getter()
        if not adapter:
            return "adapter 未就绪"
        account_id = resolve_account_id(adapter, params.pop("account_id", "default"))
        client = (adapter._clients or {}).get(account_id)
        if not client:
            return (f"账号 {account_id} 未连接"
                    f"（已连接: {sorted(adapter._clients or {})}）")
        # 过滤空参数
        body = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await client.call(endpoint, body, method=method)
            return {"ok": _resp_ok(resp), "code": resp.get("Code"), "data": resp.get("Data")}
        except Exception as e:  # noqa: BLE001
            return dump_error(f"{type(e).__name__}: {e}", endpoint=endpoint)
    return handler


def register_generic_tools(ctx, adapter_getter, tool_defs: list[dict]) -> int:
    """批量注册通用工具（基于 tools_data 定义表）。"""
    count = 0
    for td in tool_defs:
        schema_props = {}
        required = []
        for pname, pmeta in td.get("params", {}).items():
            pt = {"type": pmeta.get("type", "string")}
            # ★ 契约字段必须透传：只搬 type/desc 会让 array 丢 items、enum/边界全丢，
            #   模型看到的 schema 与厂商 Go 结构体对不上（[]int 收到字符串数组 ⇒ Code=-8）。
            for key in ("items", "enum", "format", "minimum", "maximum",
                        "minLength", "maxLength", "default"):
                if key in pmeta:
                    pt[key] = pmeta[key]
            if pmeta.get("desc"):
                pt["description"] = pmeta["desc"]
            schema_props[pname] = pt
            if pmeta.get("required"):
                required.append(pname)
        schema_props["account_id"] = {"type": "string", "description": "账号ID（默认 default）"}
        # 2026-09-01: description 注入 schema 顶层 — Hermes get_definitions() 只透传 schema，
        # 不合并 entry.description，不写顶层模型就看不到工具说明。
        schema = {"type": "object", "description": td.get("description", ""), "properties": schema_props}
        if required:
            schema["required"] = required

        endpoint = td["endpoint"]
        method = td.get("method", "POST")

        # ⚠️ 2026-10-02 修掉第二个**静默**缺陷（与 hermes_contract 的调用契约同因）：
        #    原来签名是 ``_handler(adapter_getter=noop_adapter_getter, endpoint=…, …)``
        #    —— 参数名与**外层** ``register_generic_tools(ctx, adapter_getter, …)`` 撞名，
        #    而调用方从不传它（handler 只收到模型给的参数字典）⇒ 永远取默认值 noop ⇒
        #    **每个通用工具都返回「adapter 未就绪」，一次厂商请求都不发**。
        #    它不抛异常、日志干净，所以在真机上长得像"工具正常、只是账号没连上"。
        #    现在直接闭合外层那个 getter（endpoint/method 仍用默认值防晚绑定）。
        # ⚠️ ``transform`` 必须用**默认参数**绑定到本次循环这一条定义上（与 endpoint/method
        #    同理）：handler 在循环结束后才被调用，直接引用循环变量 ``td`` 会闭包晚绑定到
        #    **最后一条**定义上 —— 那就是"每条工具都按别人的规则整形"的静默错绑。
        async def _handler(endpoint=endpoint, method=method,
                           transform=td.get("transform"), **params):
            adapter = adapter_getter()
            if not adapter:
                return "adapter 未就绪"
            account_id = resolve_account_id(adapter, params.pop("account_id", "default"))
            client = (adapter._clients or {}).get(account_id)
            if not client:
                return (f"账号 {account_id} 未连接"
                        f"（已连接: {sorted(adapter._clients or {})}）")
            body = {k: v for k, v in params.items() if v is not None}
            # 可选「请求体整形」：个别厂商端点的真实契约 schema 表达不了（二选一字段、
            # 金额 number→两位小数字符串、剔除线上 model 不存在的死字段）。规则由策略层
            # ``toolkit/policy.py`` 以纯函数挂上，生成物与 schema 都保持不动。
            if callable(transform):
                body = transform(body)
            # TS ``webhook.ts`` 的 Set/Test 在**请求体**里也带 authcode（不只是
            # header/query）。这是厂商契约的一部分，transform 拿不到客户端实例，
            # 所以在唯一出口这里补。审计侧对 ``/Webhook/`` 端点整体脱敏。
            if endpoint in ("/Webhook/Set", "/Webhook/Test"):
                body.setdefault("authcode", getattr(client, "authcode", "") or "")
            try:
                resp = await client.call(endpoint, body, method=method)
                # ★ 出站落库（TS v1.3.16 OUTBOUND-PERSIST）：13 个发送类端点成功即入库。
                #   旧实现只有 ``wpp_send_text`` / ``wpp_send_image`` 两个具名工具落库，
                #   其余发送端点（含通用表里的 SendVideo/SendXCX/ShareCard…）在
                #   ``wpp_messages`` 里完全隐形 ⇒ 引用/按群查历史都找不到锚点。
                #   落库失败只 warn，绝不把已发出的消息翻成失败。
                if hasattr(adapter, "persist_tool_outbound"):
                    try:
                        await adapter.persist_tool_outbound(account_id, endpoint, body, resp)
                    except Exception as exc:  # noqa: BLE001 —— 审计是附带动作
                        log.warning("[WPP] 工具出站落库失败 %s: %s", endpoint, exc)
                return dump_capped({"ok": _resp_ok(resp), "code": resp.get("Code"), "data": resp.get("Data")})
            except Exception as e:  # noqa: BLE001
                return dump_error(f"{type(e).__name__}: {e}", endpoint=endpoint)

        ctx.register_tool(
            name=td["name"],
            toolset="wechatpadpro",
            schema=schema,
            handler=_handler,
            is_async=True,
            description=td["description"],
            emoji="💬",
        )
        count += 1
    return count


def noop_adapter_getter():
    return None
