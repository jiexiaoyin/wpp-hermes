"""WPP phoneerp 专用工具集。

老板拍板: phoneerp 必须作为独立 tool 注册, model 必须真调,不能"打字"调 terminal。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)


def _phoneerp_natural(query: str) -> str:
    """直接跑 phoneerp CLI natural 模式, 返回 JSON 字符串."""
    cli = "/root/.hermes/skills/phoneerp/cli.js"
    try:
        result = subprocess.run(
            ["node", cli, "--json", "natural", query],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": "/root/.cache/ms-playwright"},
        )
        if result.returncode != 0:
            return json.dumps({
                "ok": False,
                "error": f"phoneerp CLI 退出码 {result.returncode}",
                "stderr": result.stderr[:500],
                "stdout": result.stdout[:500],
            }, ensure_ascii=False)
        return result.stdout.strip() or json.dumps({"ok": False, "error": "empty stdout"})
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "phoneerp CLI 超时(30s)"}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def register(ctx, adapter_getter) -> int:
    """注册 phoneerp 工具到 hermes ctx."""
    count = 0

    # 工具 1: 销售明细
    async def sales_detail(query: str = "零售明细 今天", userid: str = "") -> str:
        """查 phoneerp 销售数据 (零售明细/批发明细/提成明细)。

        Args:
            query: 自然语言查询, 例如 "零售明细 今天" / "批发明细 上周" / "提成明细 5月份"
            userid: 店员/店长 userid (默认空, 老板权限)
        Returns:
            phoneerp CLI 返回的 JSON 字符串
        """
        full_q = query + (f" userid={userid}" if userid else "")
        return _phoneerp_natural(full_q)

    ctx.register_tool(
        name="phoneerp_query",
        toolset="wechatpadpro",
        schema={
            "type": "object",
            "description": "查 phoneerp 业务数据(销售/批发/提成/库存)。调用示例: phoneerp_query(query='零售明细 今天')。返回 phoneerp CLI 的 JSON 结果,含表格明细和汇总金额。",
            "properties": {
                "query": {"type": "string", "description": "自然语言查询, 例如 '零售明细 今天' / '批发明细 上周' / '提成明细 5月份'"},
                "userid": {"type": "string", "description": "可选: 店员/店长 userid (留空 = 老板权限)"},
            },
            "required": ["query"],
        },
        handler=sales_detail,
        is_async=True,
        description="查 phoneerp 业务数据(销售/批发/提成/库存)。调用示例: phoneerp_query(query='零售明细 今天')。返回 phoneerp CLI 的 JSON 结果,含表格明细和汇总金额。",
        emoji="📊",
    )
    count += 1

    # 工具 2: 发门店定位
    async def location(store: str) -> str:
        """查 phoneerp 门店定位坐标。

        Args:
            store: 店名或店号, 例如 '3号店' / '5号店' / '1号店华为香江'
        Returns:
            含 latitude/longitude/label/poi_name 的 JSON
        """
        return _phoneerp_natural(f"发定位 {store}")

    ctx.register_tool(
        name="phoneerp_location",
        toolset="wechatpadpro",
        schema={
            "type": "object",
            "description": "查 phoneerp 门店定位 (返回经纬度, 用于 wpp_send_location 发微信定位卡片)。调用: phoneerp_location(store='3号店')",
            "properties": {"store": {"type": "string", "description": "店名或店号, 例如 '3号店'"}},
            "required": ["store"],
        },
        handler=location,
        is_async=True,
        description="查 phoneerp 门店定位 (返回经纬度, 用于 wpp_send_location 发微信定位卡片)。调用: phoneerp_location(store='3号店')",
        emoji="📍",
    )
    count += 1

    # 工具 3: 串码查询
    async def imei_query(imei: str) -> str:
        """查 phoneerp 串码/IMEI 归属。

        Args:
            imei: IMEI 串号, 例如 '860430084378288'
        Returns:
            phoneerp CLI JSON, 含商品/门店/价格
        """
        return _phoneerp_natural(f"串码查询 {imei}")

    ctx.register_tool(
        name="phoneerp_imei",
        toolset="wechatpadpro",
        schema={
            "type": "object",
            "description": "查 phoneerp 串码/IMEI 归属信息。调用: phoneerp_imei(imei='860430084378288')",
            "properties": {"imei": {"type": "string", "description": "IMEI 串号"}},
            "required": ["imei"],
        },
        handler=imei_query,
        is_async=True,
        description="查 phoneerp 串码/IMEI 归属信息。调用: phoneerp_imei(imei='860430084378288')",
        emoji="🔍",
    )
    count += 1

    # 工具 4: 任意自然语言查询 (兜底)
    async def nl_query(query: str) -> str:
        """phoneerp 任意自然语言查询 (兜底入口, 当 query 不知道用哪个具体工具时)。"""
        return _phoneerp_natural(query)

    ctx.register_tool(
        name="phoneerp_natural",
        toolset="wechatpadpro",
        schema={
            "type": "object",
            "description": "phoneerp 任意自然语言查询 (兜底)。直接调 CLI natural 模式。调用: phoneerp_natural(query='...')",
            "properties": {"query": {"type": "string", "description": "自然语言查询字符串"}},
            "required": ["query"],
        },
        handler=nl_query,
        is_async=True,
        description="phoneerp 任意自然语言查询 (兜底)。直接调 CLI natural 模式。调用: phoneerp_natural(query='...')",
        emoji="🔧",
    )
    count += 1

    return count