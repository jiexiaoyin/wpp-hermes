"""``wpp_tools_list`` 必须列出**全部**已注册工具，而不只是 vendor 端点表。

旧实现只读 ``vendor_defs{,_extra}.py`` 的 ``GROUPS``（216 条端点），于是 msg 域、
parity、mcp、jargon、extras 这些具名工具在"列出所有工具"里**根本不存在** ——
模型按它找能力，会得出"插件没有发消息工具"的错误结论，而且不报错。
"""

from __future__ import annotations

import asyncio
import json


class _Ctx:
    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def register_tool(self, **kw) -> None:
        self.tools[kw["name"]] = kw

    def __getattr__(self, _item):
        return lambda *a, **k: None


def test_tools_list_includes_non_endpoint_tools(stubbed):
    import plugin

    ctx = _Ctx()
    plugin.register(ctx)

    handler = ctx.tools["wpp_tools_list"]["handler"]

    def names_for(**kw) -> set[str]:
        data = json.loads(asyncio.run(handler(kw)))
        assert data["ok"] is True, data
        return {t["name"] for t in data["tools"]}

    # 端点表工具照旧在
    assert "wpp_group_info" in names_for(keyword="wpp_group_info")
    # 非端点表工具也必须在（以前这三类都缺）
    assert "wpp_send_message" in names_for(keyword="wpp_send_message")   # parity 路由
    assert "wpp_send_text" in names_for(keyword="wpp_send_text")         # msg 具名域
    assert "wpp_tools_list" in names_for(keyword="wpp_tools_list")       # 自己

    # 筛选用的是同一份数据，不能被截断掩盖
    only_special = json.loads(asyncio.run(handler({"domain": "special"})))
    assert only_special["count"] >= 30, only_special["count"]
