"""vendor MCP 只读客户端（对齐 TS ``src/vendor-mcp-client.ts``）。

vendor 在 **127.0.0.1:28062/mcp** 暴露只读工具（账号状态/联系人/群/最近消息/搜索）。
鉴权是 ``Authorization: Bearer <authcode>``（不是 TokenKey）。

⚠️ 端口以 vendor 实际监听为准：``app.conf`` 里 ``httpport = 28062``（同端口还有
``websocketport = 28089``）。TS 注释里的 ``8062`` 是 vendor **容器内**端口 / 旧部署，
本机 ``ss -ltnp`` 上根本没有 8062；照抄会让 MCP 永远连不上（表现为静默 fallback）。

## 为什么手写 JSON-RPC，而不是引 SDK

Hermes 的 ``plugin_validate`` 会在隔离 probe 子进程里 import 整个插件，那个解释器
**没有** MCP SDK。TS 用 ``@modelcontextprotocol/sdk``，py2 这里只用 stdlib，
保持「模块级零第三方依赖」的既有约束。

⚠️ 2026-10-18 实测：Hermes 的运行时解释器（``~/.hermes/tools/python-...``）
**连 aiohttp / httpx / requests 都没有**。早先这里用 ``import aiohttp``，结果每次
调用都抛 ``ModuleNotFoundError``，又被调用方的宽 ``except`` 吞掉 ⇒ vendor MCP
永远静默回退到本地缓存，看起来像「MCP 没启用」。现改为 stdlib
``urllib.request`` + ``asyncio.to_thread``（阻塞 IO 放线程池，不卡事件循环）。

## 失败口径

任何失败都返回 ``None``，**不抛** —— MCP 是增强通道，vendor 不可用时调用方回退到
本地缓存表（见 ``toolkit/mcp.py``）。连接超时 5s，与 TS ``MCP_TIMEOUT_MS`` 同值。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_MCP_URL = "http://127.0.0.1:28062/mcp"
TIMEOUT_MS = 5000

#: MCP 协议版本（按新到旧尝试；vendor 只认其中一个）
_PROTOCOL_VERSIONS = ("2025-03-26", "2024-11-05")


def mcp_url() -> str:
    return os.environ.get("WECHATPRO_MCP_URL") or DEFAULT_MCP_URL


def _parse_body(text: str, content_type: str) -> dict | None:
    """解析 JSON 或 SSE 响应（StreamableHTTP 两种都会出现）。"""
    if "event-stream" in (content_type or "").lower():
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            try:
                data = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and ("result" in data or "error" in data):
                return data
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _post_sync(payload: dict, authcode: str, session_id: str = "",
               protocol_version: str = "") -> tuple[dict | None, str]:
    """阻塞版 JSON-RPC POST（stdlib，仅在线程里跑）。"""
    import urllib.error
    import urllib.request

    headers = {
        "Authorization": f"Bearer {authcode}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if protocol_version:
        # MCP 2025-03-26：初始化之后的请求必须带协商出的协议版本。
        # 旧实现漏了这个头 —— 对宽松 vendor 无害，对严格实现会 400。
        headers["MCP-Protocol-Version"] = protocol_version
    req = urllib.request.Request(
        mcp_url(), data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_MS / 1000) as resp:
            text = resp.read().decode("utf-8", "replace")
            sid = resp.headers.get("Mcp-Session-Id") or session_id
            return _parse_body(text, resp.headers.get("Content-Type") or ""), sid
    except urllib.error.HTTPError as exc:
        sid = exc.headers.get("Mcp-Session-Id") or session_id
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        if exc.code >= 400:
            log.warning("[WPP] vendor MCP HTTP %s: %s", exc.code, text[:200])
            return None, sid
        return _parse_body(text, exc.headers.get("Content-Type") or ""), sid
    except Exception as exc:  # noqa: BLE001 —— MCP 不可用不该影响主链路
        log.debug("[WPP] vendor MCP 请求失败: %s", exc)
        return None, session_id


async def _rpc(payload: dict, authcode: str, session_id: str = "",
               protocol_version: str = "") -> tuple[dict | None, str]:
    """发一条 JSON-RPC 消息，返回 ``(解析后的响应, Mcp-Session-Id)``。"""
    import asyncio
    return await asyncio.to_thread(
        _post_sync, payload, authcode, session_id, protocol_version)


async def call_tool(name: str, arguments: dict[str, Any], authcode: str) -> dict | None:
    """调一个 vendor MCP 工具，返回原始 ``result``；不可用返回 ``None``。"""
    if not authcode:
        return None

    session_id = ""
    negotiated = ""
    for version in _PROTOCOL_VERSIONS:
        data, session_id = await _rpc({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "wechatpadpro", "version": "1.16.2"},
            },
        }, authcode)
        if data and "result" in data:
            result = data.get("result") or {}
            negotiated = str(result.get("protocolVersion") or version)
            break
    if not negotiated:
        return None

    # 通知没有 id、也没有响应体；失败不影响后续 tools/call（部分实现直接 202）
    await _rpc({"jsonrpc": "2.0", "method": "notifications/initialized"},
               authcode, session_id, negotiated)

    data, _ = await _rpc({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, authcode, session_id, negotiated)
    if not data:
        return None
    if "error" in data:
        err = data.get("error") or {}
        return {"isError": True,
                "content": [{"type": "text",
                             "text": str(err.get("message") or err)[:500]}]}
    result = data.get("result")
    return result if isinstance(result, dict) else None


def result_text(result: dict | None) -> str:
    """把 MCP ``{content:[{type,text}]}`` 归一化成一段文本（TS ``readMcp`` 同款）。"""
    if not result:
        return ""
    if result.get("isError"):
        blocks = result.get("content") or []
        first = blocks[0].get("text") if blocks and isinstance(blocks[0], dict) else ""
        return f"返回错误: {first or 'unknown'}"
    texts = [b.get("text", "") for b in (result.get("content") or [])
             if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "\n".join(texts)
