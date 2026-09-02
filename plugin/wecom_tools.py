"""WPP wecom-skill 专用工具集。

老板拍板: wecom-skill 必须作为独立 tool 注册, model 必须真调, 不能"打字"。

覆盖老板常用的 wecom 查询:
- 客户列表 / 客户详情
- 员工档案 / 客户标签
- 日报 / 客户变化
- 行为统计
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)

# 2026-08-31: 强制解析绝对路径, 不依赖 hermes_home/profile
def _resolve_wecom_cli() -> str:
    candidates = [
        "/root/.hermes/skills/wecom-skill/bin/wecom-cli.js",
        os.path.expanduser("~/.hermes/skills/wecom-skill/bin/wecom-cli.js"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError(f"wecom-cli.js 找不到, 试过: {candidates}")


_WECOM_CLI = _resolve_wecom_cli()


def _wecom_call(domain: str, action: str, args: dict | None = None) -> str:
    """调用 wecom-cli, 返回 JSON 字符串."""
    args = args or {}
    try:
        result = subprocess.run(
            ["node", _WECOM_CLI, domain, action, "--args", json.dumps(args, ensure_ascii=False), "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return json.dumps({
                "ok": False,
                "error": f"wecom CLI 退出码 {result.returncode}",
                "stderr": result.stderr[:500],
                "stdout": result.stdout[:500],
            }, ensure_ascii=False)
        return result.stdout.strip() or json.dumps({"ok": False, "error": "empty stdout"})
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "wecom CLI 超时(30s)"}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


def register(ctx, adapter_getter) -> int:
    """注册 wecom-skill 工具到 hermes ctx."""
    count = 0

    # 工具 1: 客户列表
    async def list_customers(userid: str = "", limit: int = 100) -> str:
        """查 wecom 客户列表(企微外部联系人)。

        Args:
            userid: 员工 userid (留空 = 老板权限, 看全部)
            limit: 返回数量上限
        """
        args = {"limit": limit}
        if userid:
            args["userid"] = userid
        return _wecom_call("customers", "get_customer_list", args)

    ctx.register_tool(
        name="wecom_list_customers",
        toolset="wechatpadpro",
        description="查企微客户列表 (员工管理的外部联系人)。调用: wecom_list_customers(userid='xxx', limit=100)。返回 JSON 含 customer 数组。",
        schema={
            "type": "object",
            "description": "查企微客户列表 (员工管理的外部联系人)。调用: wecom_list_customers(userid='xxx', limit=100)。返回 JSON 含 customer 数组。",
            "properties": {
                "userid": {"type": "string", "description": "员工 userid, 留空=老板权限看全部"},
                "limit": {"type": "integer", "description": "返回数量上限, 默认 100"},
            },
        },
        handler=list_customers,
        is_async=True,
    )
    count += 1

    # 工具 2: 客户详情
    async def customer_detail(userid: str, external_userid: str) -> str:
        """查企微客户详情。

        Args:
            userid: 员工 userid (e.g. 'JieXiaoYin')
            external_userid: 客户 external_userid
        """
        return _wecom_call("customers", "get_customer_detail", {"userid": userid, "external_userid": external_userid})

    ctx.register_tool(
        name="wecom_customer_detail",
        toolset="wechatpadpro",
        description="查企微客户详情 (员工 + 客户的 external_userid)。调用: wecom_customer_detail(userid='JieXiaoYin', external_userid='xxx')",
        schema={
            "type": "object",
            "description": "查企微客户详情 (员工 + 客户的 external_userid)。调用: wecom_customer_detail(userid='JieXiaoYin', external_userid='xxx')",
            "properties": {
                "userid": {"type": "string", "description": "员工 userid"},
                "external_userid": {"type": "string", "description": "客户 external_userid"},
            },
            "required": ["userid", "external_userid"],
        },
        handler=customer_detail,
        is_async=True,
    )
    count += 1

    # 工具 3: 周行为
    async def weekly_behavior(userid: str) -> str:
        """查员工周行为统计 (客户数/消息数等)。"""
        return _wecom_call("customers", "weekly_behavior", {"userid": userid})

    ctx.register_tool(
        name="wecom_weekly_behavior",
        toolset="wechatpadpro",
        description="查员工周行为统计 (客户数/消息数/客户群数等)。调用: wecom_weekly_behavior(userid='JieXiaoYin')",
        schema={
            "type": "object",
            "description": "查员工周行为统计 (客户数/消息数/客户群数等)。调用: wecom_weekly_behavior(userid='JieXiaoYin')",
            "properties": {"userid": {"type": "string", "description": "员工 userid"}},
            "required": ["userid"],
        },
        handler=weekly_behavior,
        is_async=True,
    )
    count += 1

    # 工具 4: 日客户变化
    async def daily_user_changes(userid: str, begin_date: str = "", end_date: str = "") -> str:
        """查员工日客户变化 (新增/流失)。"""
        args = {"userid": userid}
        if begin_date:
            args["begin_date"] = begin_date
        if end_date:
            args["end_date"] = end_date
        return _wecom_call("customers", "daily_user_changes", args)

    ctx.register_tool(
        name="wecom_daily_user_changes",
        toolset="wechatpadpro",
        description="查员工日客户变化 (新增/流失客户数)。调用: wecom_daily_user_changes(userid='JieXiaoYin', begin_date='2026-08-01', end_date='2026-08-31')",
        schema={
            "type": "object",
            "description": "查员工日客户变化 (新增/流失客户数)。调用: wecom_daily_user_changes(userid='JieXiaoYin', begin_date='2026-08-01', end_date='2026-08-31')",
            "properties": {
                "userid": {"type": "string", "description": "员工 userid"},
                "begin_date": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
            },
            "required": ["userid"],
        },
        handler=daily_user_changes,
        is_async=True,
    )
    count += 1

    # 工具 5: 日报
    async def daily_report(userid: str, begin_date: str = "", end_date: str = "") -> str:
        """查员工日报数据。"""
        args = {"userid": userid}
        if begin_date:
            args["begin_date"] = begin_date
        if end_date:
            args["end_date"] = end_date
        return _wecom_call("customers", "daily_report", args)

    ctx.register_tool(
        name="wecom_daily_report",
        toolset="wechatpadpro",
        description="查员工日报数据。调用: wecom_daily_report(userid='JieXiaoYin', begin_date='2026-08-30')",
        schema={
            "type": "object",
            "description": "查员工日报数据。调用: wecom_daily_report(userid='JieXiaoYin', begin_date='2026-08-30')",
            "properties": {
                "userid": {"type": "string", "description": "员工 userid"},
                "begin_date": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
            },
            "required": ["userid"],
        },
        handler=daily_report,
        is_async=True,
    )
    count += 1

    # 工具 6: 全客户列表 (老板视图)
    async def all_customers(limit: int = 1000) -> str:
        """查全部客户 (老板权限)。"""
        return _wecom_call("customers", "get_all_customers", {"limit": limit})

    ctx.register_tool(
        name="wecom_all_customers",
        toolset="wechatpadpro",
        description="查企微全部客户 (老板权限, 全公司)。调用: wecom_all_customers(limit=500)",
        schema={
            "type": "object",
            "description": '查企微全部客户 (老板权限, 全公司)。调用: wecom_all_customers(limit=500)',
            "properties": {"limit": {"type": "integer", "description": "返回数量上限, 默认 1000"}},
        },
        handler=all_customers,
        is_async=True,
    )
    count += 1

    # 工具 7: 客户标签
    async def corp_tags() -> str:
        """查公司标签列表 (企微标签管理)。"""
        return _wecom_call("customers", "get_corp_tags", {})

    ctx.register_tool(
        name="wecom_corp_tags",
        toolset="wechatpadpro",
        description="查企微公司标签列表 (老板/HR 用)。",
        schema={"type": "object", "description": "查企微公司标签列表 (老板/HR 用)。", "properties": {}},
        handler=corp_tags,
        is_async=True,
    )
    count += 1

    return count