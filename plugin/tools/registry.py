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

## 约束

- **无重依赖**：只 import stdlib ``json`` / ``logging``。handler 里要用的
  pymysql / node / 网络一律在**函数体内**导入（Hermes ``plugin_validate`` 会在隔离
  子进程里 import 整个插件）。
- **包内相对导入**：本插件禁止裸名导入（``plugin/platform/`` 会遮蔽 stdlib ``platform``）。
- ``register_generic_tools`` 里 ``toolset="wechatpadpro"`` 是**字面量**，不是常量（保持原样）。
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

#: 本插件所有工具注册用的 toolset 名。
#: ⚠️ 只是**常量出口**：注册点仍写 ``toolset="wechatpadpro"`` **字面量**
#: （旧实现如此，别替换成这个常量 —— 换掉就改了注册行为的可读基线）。
TOOLSET = "wechatpadpro"


def get_client(adapter, account_id: str):
    """从 adapter 拿指定账号的 WppClient。"""
    return (adapter._clients or {}).get(account_id)


def mk_handler(fn):
    """包装 handler，把异常转成工具返回字符串。"""
    async def handler(**params):
        try:
            result = await fn(**params)
            return json.dumps(result, ensure_ascii=False, default=str)[:4000]
        except Exception as e:  # noqa: BLE001
            return f"调用失败: {e}"
    return handler


def generic_handler(endpoint: str, method: str = "POST"):
    """通用 vendor 工具 handler：调 client.call(endpoint, body)。"""
    async def handler(adapter_getter, **params):
        adapter = adapter_getter()
        if not adapter:
            return "adapter 未就绪"
        account_id = params.pop("account_id", "default")
        client = (adapter._clients or {}).get(account_id)
        if not client:
            return f"账号 {account_id} 未连接"
        # 过滤空参数
        body = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await client.call(endpoint, body, method=method)
            return {"ok": resp.get("Success", False), "code": resp.get("Code"), "data": resp.get("Data")}
        except Exception as e:  # noqa: BLE001
            return f"调用失败: {e}"
    return handler


def register_generic_tools(ctx, adapter_getter, tool_defs: list[dict]) -> int:
    """批量注册通用工具（基于 tools_data 定义表）。"""
    count = 0
    for td in tool_defs:
        schema_props = {}
        required = []
        for pname, pmeta in td.get("params", {}).items():
            pt = {"type": pmeta.get("type", "string")}
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

        async def _handler(adapter_getter=noop_adapter_getter, endpoint=endpoint, method=method, **params):
            adapter = adapter_getter()
            if not adapter:
                return "adapter 未就绪"
            account_id = params.pop("account_id", "default")
            client = (adapter._clients or {}).get(account_id)
            if not client:
                return f"账号 {account_id} 未连接"
            body = {k: v for k, v in params.items() if v is not None}
            try:
                resp = await client.call(endpoint, body, method=method)
                return json.dumps({"ok": resp.get("Success", False), "code": resp.get("Code"), "data": resp.get("Data")}, ensure_ascii=False, default=str)[:4000]
            except Exception as e:  # noqa: BLE001
                return f"调用失败: {e}"

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
