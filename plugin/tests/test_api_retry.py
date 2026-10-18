"""发送类端点**不重试**（2026-10-18 审阅修）。

## 为什么这条线是 P1

``_request`` 原来对所有端点默认 ``max_retries=2``。超时/连接断这两类失败**无法区分**
"请求没到厂商"和"厂商已经发出、响应丢了"。对读取闻端点重试是自愈；对发送端点重试
就是**给真实联系人再发一条** —— 微信消息撤不回（红线 3）。

修法：在唯一入口 ``WppClient.call`` 里按 ``outbound_guard.is_read_only`` 分类，
写端点一律把 ``max_retries`` 压成 0。这样通用端点表里那 216 个端点也自动覆盖，
不依赖每个调用点自己记得传参。

本文件用假 ``aiohttp`` 数**真实发出的请求次数**：写端点必须恰好 1 次（即使调用方
显式要求重试），读端点保留重试。
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from plugin.core import api_client as api_mod
from plugin.core.api_client import WppApiError, WppClient


class _ClientError(Exception):
    pass


def _install_fake_aiohttp(monkeypatch, script: list[tuple[int, str]]):
    """装一个假 aiohttp：``script`` 是按序返回的 ``(status, body)``，返回假 session。"""

    class _Resp:
        def __init__(self, status: int, text: str) -> None:
            self.status = status
            self._text = text

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def text(self) -> str:
            return self._text

    class _Sess:
        def __init__(self) -> None:
            self.calls = 0
            self.closed = False

        def request(self, method, url, **kw):        # noqa: ANN001
            idx = min(self.calls, len(script) - 1)
            self.calls += 1
            return _Resp(*script[idx])

        async def close(self):
            self.closed = True

    sess = _Sess()
    fake = types.SimpleNamespace(
        ClientSession=lambda: sess,
        ClientTimeout=lambda **kw: None,
        ClientError=_ClientError,
    )
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    return sess


@pytest.fixture
def no_sleep(monkeypatch):
    """退避等待归零（只影响进程内 asyncio.sleep，monkeypatch 会自动还原）。"""
    async def _noop(_s):
        return None

    monkeypatch.setattr(api_mod.asyncio, "sleep", _noop)


def test_a_send_is_attempted_exactly_once(monkeypatch):
    """★ 503 是"上游抖动"，读端点会重试；**发送端点必须只试一次**。"""
    sess = _install_fake_aiohttp(monkeypatch, [(503, "busy")])
    client = WppClient(base_url="https://vendor.test", authcode="fake", audit=False)
    with pytest.raises(WppApiError) as ei:
        asyncio.run(client.send_text("wxid_a", "hi"))
    assert ei.value.code == "http_503"
    assert sess.calls == 1, "发送端点重试了 —— 可能给对方发了两条"


def test_even_explicit_retries_are_suppressed_for_writes(monkeypatch, no_sleep):
    """★ 通用端点表 handler 走 ``client.call(..., max_retries=2)`` 的默认值；
    收口必须压过调用方显式传的值（否则"记得别重试"又要靠每个调用点自觉）。"""
    sess = _install_fake_aiohttp(monkeypatch, [(502, "bad gateway")])
    client = WppClient(base_url="https://vendor.test", authcode="fake", audit=False)
    with pytest.raises(WppApiError):
        asyncio.run(client.call("/Msg/SendTxt", {"ToWxid": "wxid_a", "Content": "hi"},
                                max_retries=5))
    assert sess.calls == 1


def test_a_read_endpoint_still_retries(monkeypatch, no_sleep):
    """读端点保持原来的自愈能力：503 一次后成功，应发出第 2 次请求。"""
    sess = _install_fake_aiohttp(
        monkeypatch, [(503, "busy"), (200, '{"Code":0,"Success":true,"Data":{}}')])
    client = WppClient(base_url="https://vendor.test", authcode="fake", audit=False)
    out = asyncio.run(client.call("/User/GetContractProfile", {}))
    assert out.get("Code") == 0
    assert sess.calls == 2, "读端点应当重试一次"


def test_the_write_rule_is_in_the_single_entrypoint():
    """判据必须挂在 ``call``（所有业务端点的唯一入口），不是散在各 typed 方法里。"""
    import ast

    src = (api_mod.__file__ or "")
    tree = ast.parse(open(src, encoding="utf-8").read())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "WppClient")
    fn = next(n for n in cls.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "call")
    code = "\n".join(ast.unparse(s) for s in fn.body)
    assert "is_read_only(endpoint)" in code, \
        "call() 里必须按端点分类压掉写端点的重试"
