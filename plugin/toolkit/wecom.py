"""WPP wecom-skill 专用工具集。

老板拍板: wecom-skill 必须作为独立 tool 注册, model 必须真调, 不能"打字"。

覆盖老板常用的 wecom 查询:
- 客户列表 / 客户详情
- 员工档案 / 客户标签
- 日报 / 客户变化
- 行为统计

从旧扁平 ``plugin/wecom_tools.py`` **原样迁入**（只换位置）。本文件零相对导入、
零模块级副作用 —— 后者是 2026-10-01 特意修的（见下方 ``_WECOM_CLI_CANDIDATES``
那段注释：旧实现在模块级解析 CLI ⇒ .183 上 import 就炸 ⇒ 10 个工具静默消失）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)

# 2026-08-31: 强制解析绝对路径, 不依赖 hermes_home/profile
#
# ⚠️ 2026-10-01 重写：解析**绝不能**放在模块级。
#    旧实现在这里写 `_WECOM_CLI = _resolve_wecom_cli()`，而 .183 上没有
#    wecom-skill ⇒ import 期抛 FileNotFoundError ⇒ 被 registration._step 吞掉
#    ⇒ **wecom 7 个工具全部静默不注册**（F5）。
#    现在：解析延迟到调用期；找不到就返回 None，工具照常注册、调用时给出明确错误。
#    这样 `provides_tools` 的声明数恒等于实注册数（R6 的稳定性前提）。
_WECOM_CLI_CANDIDATES = (
    "/root/.hermes/skills/wecom-skill/bin/wecom-cli.js",
    "~/.hermes/skills/wecom-skill/bin/wecom-cli.js",
)

_wecom_cli_cache: str | None = None
_wecom_cli_warned = False


def _resolve_wecom_cli() -> str | None:
    """解析 wecom-cli.js 路径；找不到返回 None（**不抛**，见上方注释）。"""
    global _wecom_cli_cache
    if _wecom_cli_cache is not None:
        return _wecom_cli_cache
    for raw in _WECOM_CLI_CANDIDATES:
        c = os.path.expanduser(raw)
        if os.path.exists(c):
            _wecom_cli_cache = c
            return c
    return None


def _wecom_cli_or_warn() -> str | None:
    """取 CLI 路径；缺失时只告警一次，避免每次调用刷屏。"""
    global _wecom_cli_warned
    path = _resolve_wecom_cli()
    if path is None and not _wecom_cli_warned:
        _wecom_cli_warned = True
        log.warning(
            "[WPP] wecom-cli.js 未找到（试过 %s）—— wecom 工具已注册但调用会失败。"
            " 部署时请把 wecom-skill 放进 skills/。",
            list(_WECOM_CLI_CANDIDATES),
        )
    return path


async def _call_async(domain: str, action: str, args: dict | None = None) -> str:
    """``_wecom_call`` 的异步壳：把 30s 的同步子进程挪到工作线程。"""
    return await asyncio.to_thread(_wecom_call, domain, action, args)


def _wecom_call(domain: str, action: str, args: dict | None = None) -> str:
    """调用 wecom-cli, 返回 JSON 字符串."""
    args = args or {}
    cli = _wecom_cli_or_warn()
    if cli is None:
        return json.dumps({
            "ok": False,
            "error": "wecom-cli.js 未安装（需要 node + wecom-skill）",
        }, ensure_ascii=False)
    # ⚠️ 同步子进程（最长 30s）。本文件的所有 handler 都以 ``is_async=True`` 注册，
    #   直接跑会把**整个事件循环**（WS 心跳、其它工具、定时器）堵住 —— 调用方必须走
    #   ``asyncio.to_thread``，见各 handler 里的 ``_call_async``。
    try:
        result = subprocess.run(
            ["node", cli, domain, action, "--args", json.dumps(args, ensure_ascii=False), "--json"],
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
        return await _call_async("customers", "get_customer_list", args)

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
        return await _call_async("customers", "get_customer_detail", {"userid": userid, "external_userid": external_userid})

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
        return await _call_async("customers", "weekly_behavior", {"userid": userid})

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
        return await _call_async("customers", "daily_user_changes", args)

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
        return await _call_async("customers", "daily_report", args)

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
        return await _call_async("customers", "get_all_customers", {"limit": limit})

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
        return await _call_async("customers", "get_corp_tags", {})

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