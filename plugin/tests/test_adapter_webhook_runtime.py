"""``WebhookMixin._start_webhook`` 的**运行时**契约（不是 AST 形状断言）。

``test_webhook.py`` 里的形状断言只能证明"代码写成那样"，证明不了"跑起来注册了什么"。
2026-10-19 拆出 ``adapter_webhook.py`` 后该文件运行时覆盖率只有 13% —— 三个方法里
最关键的注册逻辑一次都没被执行过。这里补的是**行为**：

1. 有路径 token 时，主 + business **两条**路由都注册（旧代码只注册主路径，
   vendor 推完整消息的那条 ``/business`` 一直 404）；
2. business 路由的 ``secret`` 必须是空 —— vendor 的 business 回调**不带任何签名头**
   （2026-10-19 18:31 实抓），沿用账号 secret 会让每条都 403、业务消息静默丢失；
3. 既没 token 也没 secret 时**一条都不注册**，并且 ``_webhook`` 要置回 ``None``
   （fail-closed：宁可不起监听，也不开一个谁都能伪造入站的口子）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plugin.platform.adapter_webhook import WebhookMixin

TOKEN = "tok0123456789abcdef012345"


class _FakeAdapter(WebhookMixin):
    """只实现 mixin 依赖的属性，避免构造 38 字段的 Account。"""

    def __init__(self) -> None:
        self._webhook = None
        self._webhook_accounts: set[str] = set()
        self.raw_calls: list[tuple[str, dict]] = []

    def _on_raw(self, acct, payload) -> None:  # noqa: ANN001
        self.raw_calls.append((acct.id, payload))


def _acct(**over: object) -> SimpleNamespace:
    base = {
        "id": "default",
        "webhook_path": "/wechatpadpro/default/webhook",
        "webhook_path_token": TOKEN,
        "webhook_secret": "",
        "webhook_host": "127.0.0.1",
        "webhook_port": 4398,
        "webhook_business_path": "",
    }
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_registers_main_and_business_paths_with_the_token():
    adapter = _FakeAdapter()
    with patch("plugin.platform.adapter_webhook.WppWebhookServer") as server_cls:
        # _start_webhook 的返回值透传 server.start()，mock 要给出真值
        server_cls.return_value.start.return_value = True
        ok = adapter._start_webhook([_acct()])

    assert ok is True
    assert adapter._webhook is server_cls.return_value
    assert adapter._webhook_accounts == {"default"}

    paths = [c.args[0] for c in server_cls.return_value.add_path.call_args_list]
    assert len(paths) == 2, f"主 + business 两条都要注册，实际 {paths}"
    assert all(TOKEN in p for p in paths), f"派生路径必须带 token：{paths}"
    assert paths[0] != paths[1], "主路径与 business 路径不能是同一条"


@pytest.mark.asyncio
async def test_business_route_gets_no_secret_even_when_the_account_has_one():
    adapter = _FakeAdapter()
    with patch("plugin.platform.adapter_webhook.WppWebhookServer") as server_cls:
        adapter._start_webhook([_acct(webhook_secret="s" * 64)])

    calls = server_cls.return_value.add_path.call_args_list
    assert len(calls) == 2
    main, business = calls[0].kwargs, calls[1].kwargs
    assert main["secret"] == "s" * 64, "主 webhook 必须用 v2 HMAC secret"
    assert business["secret"] == "", (
        "business 回调不带签名头（vendor 侧无 secret 字段），沿用账号 secret 会 403 全丢")


@pytest.mark.asyncio
async def test_no_credential_registers_nothing_and_leaves_no_listener():
    adapter = _FakeAdapter()
    with patch("plugin.platform.adapter_webhook.WppWebhookServer") as server_cls:
        ok = adapter._start_webhook([_acct(webhook_path_token="", webhook_secret="")])

    assert ok is False, "无凭据必须 fail-closed，不能报成功"
    server_cls.return_value.add_path.assert_not_called()
    assert adapter._webhook is None, "一条都没注册时不能留下半开的监听"
    assert adapter._webhook_accounts == set()


@pytest.mark.asyncio
async def test_accounts_without_a_webhook_path_are_ignored():
    adapter = _FakeAdapter()
    with patch("plugin.platform.adapter_webhook.WppWebhookServer") as server_cls:
        ok = adapter._start_webhook([_acct(webhook_path="")])

    assert ok is False
    assert not server_cls.called, "没有任何账号配 webhookPath 时不该起 server"
