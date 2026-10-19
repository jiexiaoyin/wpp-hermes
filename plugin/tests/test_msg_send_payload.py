"""发送类工具的成败判据（2026-10-18 修）。

## 修的是什么

``toolkit/msg.py`` 的 15 处发送工具各自判 ``BaseResponse.ret == 0``（或 ``Code == 0``），
**从不看 ``Data.List[].Ret``**。厂商在某些情况下回：

    {"Code": 0, "Data": {"BaseResponse": {"ret": 0}, "List": [{"Ret": -2, "NewMsgId": 5}]}}

看着全对，但消息**被拒收**（v1.5.6 ``SEND-LIST-RET``：某业务晨报 30362 群根本没收到，
调用方一路报成功）。适配器侧早用 ``platform.outbound.send_ok`` 修掉了，工具面漏了 ——
模型拿到 ``ok:true`` 就认为已送达、不会再发。

现在所有发送工具都走 ``_send_payload``，判据只有 ``send_ok`` 一份。
"""

from __future__ import annotations

import asyncio
import json
import types

from plugin.toolkit.msg import _send_payload, register_msg_tools


def test_list_ret_rejection_is_reported_as_failure() -> None:
    """``Code=0`` + ``BaseResponse.ret=0`` 但逐条 ``Ret=-2`` ⇒ 必须 ok:false。"""
    resp = {"Code": 0, "Data": {"BaseResponse": {"ret": 0},
                                "List": [{"Ret": -2, "NewMsgId": 5}]}}
    out = _send_payload(resp)
    assert out["ok"] is False
    assert "error" in out
    assert out.get("newMsgId") == "5"


def test_plain_success_keeps_ok_and_msg_id() -> None:
    resp = {"Code": 0, "Data": {"BaseResponse": {"ret": 0},
                                "List": [{"Ret": 0, "NewMsgId": 42}]}}
    out = _send_payload(resp)
    assert out["ok"] is True
    assert out["msgId"] == "42"
    assert "error" not in out


def test_vendor_code_error_is_failure() -> None:
    out = _send_payload({"Code": -8, "Message": "反序列化失败"})
    assert out["ok"] is False


def test_extra_fields_are_preserved() -> None:
    """``image_path`` / ``degraded`` / ``code`` 这些附加上下文不能丢。"""
    resp = {"Code": 0, "Data": {"BaseResponse": {"ret": 0}}}
    out = _send_payload(resp, code=0, image_path="/tmp/x.png", degraded="file")
    assert out["image_path"] == "/tmp/x.png"
    assert out["degraded"] == "file"
    assert out["code"] == 0


def test_non_dict_response_is_failure() -> None:
    assert _send_payload(None)["ok"] is False


# ------------------------------------------------- 群发目标上限（2026-10-19）


class _MassClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def send_group_mass_text(self, to_ids, content):
        self.calls.append(list(to_ids))
        return {"Code": 0, "Data": {"BaseResponse": {"ret": 0}}}


def _mass_handler():
    client = _MassClient()
    adapter = types.SimpleNamespace(_clients={"acct": client})

    class _Ctx:
        def __init__(self) -> None:
            self.tools: dict[str, dict] = {}

        def register_tool(self, **kw) -> None:
            self.tools[kw["name"]] = kw

    ctx = _Ctx()
    register_msg_tools(ctx, lambda: adapter)
    handler = ctx.tools["wpp_group_mass_text"]["handler"]

    def call(**params):
        return json.loads(asyncio.run(handler(account_id="acct", **params)))

    return client, call


def test_group_mass_rejects_empty_targets() -> None:
    client, call = _mass_handler()
    out = call(toIds=[], content="hi")
    assert out["ok"] is False and "为空" in out["error"]
    assert client.calls == [], "空目标绝不许发给厂商"


def test_group_mass_rejects_more_than_64_targets() -> None:
    client, call = _mass_handler()
    out = call(toIds=[f"g{i}@chatroom" for i in range(65)], content="hi")
    assert out["ok"] is False and "上限 64" in out["error"]
    assert client.calls == []


def test_group_mass_dedupes_and_trims_targets() -> None:
    client, call = _mass_handler()
    out = call(toIds=[" a@chatroom ", "a@chatroom", "b@chatroom", ""], content="hi")
    assert out["ok"] is True
    assert client.calls == [["a@chatroom", "b@chatroom"]]
