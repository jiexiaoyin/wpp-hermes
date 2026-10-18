"""webhook 路径 token 的日志脱敏 —— TS v1.16.1 ``core/redact.ts`` 的 Python 对等回归。

## 为什么要钉

``webhookPathToken`` 是 webhook 路径上**唯一的**鉴权：看到日志里的明文 token
就等于拿到"往插件里灌任意消息"的入口。本插件此前把完整路径（含 token）原样打进
``注册路由`` / ``鉴权失败`` / ``调度失败`` 三条日志。TS v1.16.1 为此新增了
``core/redact.ts`` 并在生产验收时又抓出第 13 处漏点 —— 说明"人肉列扫描范围"不可靠，
所以这里不测"某个函数被调用了"，而是**对着日志捕获器断言 token 一次都没出现**。
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request

import pytest

from plugin.core.redact import mask_secret, redact_webhook_path, redact_webhook_path_any
from plugin.platform.inbound_webhook import WppWebhookServer

TOKEN_A = "0123456789abcdef0123456789abcdef"   # 32 hex（新生成的形状）
TOKEN_B = "fedcba9876543210"                    # 16 hex（老账号可能更短）


# ------------------------------------------------------------------ 纯函数


def test_mask_secret_shape():
    assert mask_secret("") == "(empty)"
    assert mask_secret("abcd") == "****"
    assert mask_secret(TOKEN_A) == "0123...ef"
    assert TOKEN_A not in mask_secret(TOKEN_A)


def test_redact_is_by_value_and_identity_without_token():
    path = f"/wechatpadpro/{TOKEN_A}/default/webhook"
    masked = redact_webhook_path(path, TOKEN_A)
    assert TOKEN_A not in masked
    assert masked == f"/wechatpadpro/{mask_secret(TOKEN_A)}/default/webhook"
    # 无 token ⇒ 恒等（结构式脱敏会把 "webhook" 打成 "webh...ok"）
    assert redact_webhook_path("/wechatpadpro/default/webhook", None) == \
        "/wechatpadpro/default/webhook"
    assert redact_webhook_path("/wechatpadpro/default/webhook", "") == \
        "/wechatpadpro/default/webhook"


def test_redact_any_masks_every_accounts_token():
    """多账号共享 server：A 账号日志里出现的 B 账号 token 也必须脱。"""
    s = f"/wechatpadpro/{TOKEN_A}/a/webhook, /wechatpadpro/{TOKEN_B}/b/webhook"
    out = redact_webhook_path_any(s, [TOKEN_A, TOKEN_B, None, ""])
    assert TOKEN_A not in out and TOKEN_B not in out


# ------------------------------------------------------------------ 接线


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_add_path_log_never_contains_token(caplog):
    caplog.set_level(logging.INFO, logger="plugin.platform.inbound_webhook")
    srv = WppWebhookServer(host="127.0.0.1", port=4399)
    path = f"/wechatpadpro/{TOKEN_A}/default/webhook"
    srv.add_path(path, _noop, secret="", path_token=TOKEN_A)
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert TOKEN_A not in logs, "注册日志里漏了明文 token"
    assert mask_secret(TOKEN_A) in logs, "日志应保留可核对的 mask 形式"


@pytest.mark.network  # 只连 127.0.0.1 的本进程 server；conftest 默认禁网，这里显式放行
def test_request_logs_are_redacted_end_to_end(caplog):
    """真起一个 server，打一发缺 secret 的请求 ⇒ 鉴权失败日志里不得有 token。"""
    port = _free_port()
    srv = WppWebhookServer(host="127.0.0.1", port=port)
    path = f"/wechatpadpro/{TOKEN_A}/default/webhook"
    srv.add_path(path, _noop, secret="s3cr3t", path_token=TOKEN_A)
    assert srv.start(), "测试用 webhook server 起不来（端口被占？）"
    try:
        caplog.set_level(logging.DEBUG, logger="plugin.platform.inbound_webhook")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps({}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        logs = "\n".join(r.getMessage() for r in caplog.records)
        assert TOKEN_A not in logs, "鉴权失败日志里漏了明文 token"
        assert mask_secret(TOKEN_A) in logs
    finally:
        srv.stop()


def test_redact_path_covers_late_registered_accounts():
    """后注册账号的 token 也要被覆盖（脱敏器每次读最新路由表，不是构造时快照）。"""
    srv = WppWebhookServer()
    srv.add_path(f"/wechatpadpro/{TOKEN_A}/a/webhook", _noop,
                 secret="", path_token=TOKEN_A)
    srv.add_path(f"/wechatpadpro/{TOKEN_B}/b/webhook", _noop,
                 secret="", path_token=TOKEN_B)
    out = srv.redact_path(f"/wechatpadpro/{TOKEN_B}/b/webhook")
    assert TOKEN_B not in out and TOKEN_A not in out


async def _noop(payload: dict) -> None:
    return None
