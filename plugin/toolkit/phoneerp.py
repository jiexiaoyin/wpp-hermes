"""WPP phoneerp 专用工具集。

老板拍板: phoneerp 必须作为独立 tool 注册, model 必须真调,不能"打字"调 terminal。

从旧扁平 ``plugin/phoneerp_tools.py`` **原样迁入** —— 只换了位置，
一处内容都没动（本文件零相对导入、零模块级副作用，所以搬动是安全的）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)


def _caller_userid(model_value: str = "") -> str:
    """调用者身份：**只信入站 contextvar**，模型填的 userid 一律不采信。

    模型参数是 prompt injection 可改写的（同 ``account_id``/``callerWxid`` 的教训，
    见 ``core/inbound_context.py``）。绑定存在时以真实发送者 wxid 为准；没有绑定
    （CLI/TUI 直调）才回退到显式传参，保持旧行为。
    """
    from ..core import inbound_context

    real = inbound_context.sender()
    return real or str(model_value or "")


async def _phoneerp_natural_async(query: str, userid: str = "") -> str:
    """``_phoneerp_natural`` 的异步壳（30s 同步子进程 ⇒ 工作线程）。"""
    return await asyncio.to_thread(_phoneerp_natural, query, userid)


def _phoneerp_natural(query: str, userid: str = "") -> str:
    """直接跑 phoneerp CLI natural 模式, 返回 JSON 字符串.

    ``--userid`` 必须真的传给 CLI：旧实现把 `` userid=xxx`` 拼进 query 文本，
    CLI 不解析这种写法 ⇒ 权限过滤从未生效。
    """
    cli = "/root/.hermes/skills/phoneerp/cli.js"
    argv = ["node", cli, "--json", "natural", query]
    if userid:
        argv += ["--userid", userid]
    try:
        result = subprocess.run(
            argv,
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
    async def sales_detail(query: str = "零售明细 今天") -> str:
        """查 phoneerp 销售数据 (零售明细/批发明细/提成明细)。

        Args:
            query: 自然语言查询, 例如 "零售明细 今天" / "批发明细 上周" / "提成明细 5月份"
        Returns:
            phoneerp CLI 返回的 JSON 字符串
        """
        return await _phoneerp_natural_async(query, _caller_userid())

    ctx.register_tool(
        name="phoneerp_query",
        toolset="wechatpadpro",
        schema={
            "type": "object",
            "description": "查 phoneerp 业务数据(销售/批发/提成/库存)。调用示例: phoneerp_query(query='零售明细 今天')。返回 phoneerp CLI 的 JSON 结果,含表格明细和汇总金额。权限按当前微信发送者自动过滤,不需要也不能指定 userid。",
            "properties": {
                "query": {"type": "string", "description": "自然语言查询, 例如 '零售明细 今天' / '批发明细 上周' / '提成明细 5月份'"},
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
        return await _phoneerp_natural_async(f"发定位 {store}", _caller_userid())

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
        return await _phoneerp_natural_async(f"串码查询 {imei}", _caller_userid())

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
        return await _phoneerp_natural_async(query, _caller_userid())

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
