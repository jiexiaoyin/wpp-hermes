"""微信支付/红包工具请求体 —— 对齐 TS v1.16.1（含它纠正的 v1.6.0 SWAGGER-323）。

## 这一组测试守的是什么

生成物 ``toolkit/vendor_defs*.py`` 照抄的是**更早的旧树**，其中多条 tenpay 请求体
带的是厂商 swagger 里**不存在**的字段（``wxid`` / ``redPacketId`` / ``url``+``key``）
或类型不对（金额发 number，model 声明的是 string）。厂商 Go 对未知字段**静默忽略**
⇒ 这些调用从不报错，也从未生效。

TS 侧已在 v1.6.0 与 v1.16.1 逐条更正（``src/send/tenpay.ts`` / ``tenpay-meta.ts``）。
本插件把差异收在 ``toolkit/policy.py`` 的 ``PATCH_TOOLS``（生成物保持字节保真），
所以判据必须下在**真正发出去的请求体**上，而不是"策略表里有这一条"。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from plugin.toolkit import policy, registry
from plugin.toolkit import vendor_defs, vendor_defs_extra


def _all_defs() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for _group, defs in policy.iter_groups(
            list(vendor_defs.GROUPS) + list(vendor_defs_extra.GROUPS)):
        for td in defs:
            out[td["name"]] = td
    return out


DEFS = _all_defs()


# ------------------------------------------------------------------ 策略表


def test_patch_covers_every_corrected_tenpay_tool():
    """逐条钉死「哪些工具被纠错、纠成什么形状」。

    ⚠️ 用**字面量**列出期望参数名 —— 从策略表自身推导（``for p in params``）会让
    "少了一个字段"的回归照样全绿。
    """
    expected = {
        "wpp_tenpay_ge_ma_skd_qcode": {"Money", "Name", "Remark"},
        "wpp_tenpay_sj_skd_qcode": {"Money", "Name", "Remark"},
        "wpp_tenpay_generate_pay_qcode": {"money", "name"},
        "wpp_tenpay_open_hongbao": {"contextId", "Xml", "SendUserName"},
        "wpp_tenpay_collect_money": {
            "contextId", "toUserName", "transFerId", "transactionId", "invalidTime"},
        "wpp_tenpay_confirm_pre_transfer": {
            "bankSerial", "bankType", "payPassword", "reqKey"},
        "wpp_tenpay_redpacket_list": {"xml", "offset", "size"},
        "wpp_tenpay_open_redpacket": {
            "Xml", "SendUserName", "TimingIdentifier", "Encrypt_key", "Encrypt_userinfo"},
        "wpp_tenpay_query_hongbao": {"Xml", "Encrypt_key", "Encrypt_userinfo"},
        "wpp_tenpay_receive_hongbao": {"Xml", "InWay", "Encrypt_key", "Encrypt_userinfo"},
    }
    # PATCH_TOOLS 里还有 2026-10-18 新增的 Tools/User/Wxapp/Login 纠错条目；
    # 这里只钉死 tenpay 子集（其余条目各自有测试）。
    assert {n for n in policy.PATCH_TOOLS if "tenpay" in n} == set(expected)
    for name, keys in expected.items():
        assert set(DEFS[name]["params"]) == keys, f"{name} 参数表与 TS 不一致"


def test_dead_swagger_fields_are_gone():
    """线上 model 里不存在的字段一个都不能留（它们是"从不报错的空转"的根因）。"""
    for name in ("wpp_tenpay_ge_ma_skd_qcode", "wpp_tenpay_sj_skd_qcode"):
        assert "Wxid" not in DEFS[name]["params"], f"{name}: 线上 model 无 Wxid"
    for name in ("wpp_tenpay_generate_pay_qcode", "wpp_tenpay_redpacket_list",
                 "wpp_tenpay_collect_money"):
        assert "wxid" not in DEFS[name]["params"], f"{name}: 线上 model 无 wxid"
    for name in ("wpp_tenpay_open_redpacket", "wpp_tenpay_query_hongbao",
                 "wpp_tenpay_receive_hongbao"):
        assert "redPacketId" not in DEFS[name]["params"], f"{name}: 线上 model 无 redPacketId"
    assert "url" not in DEFS["wpp_tenpay_open_hongbao"]["params"]
    assert "key" not in DEFS["wpp_tenpay_open_hongbao"]["params"]


def test_confirm_pre_transfer_four_required():
    """四个字段在线上全必填 —— 少一个就只会发出空串，线上不可能成功。"""
    params = DEFS["wpp_tenpay_confirm_pre_transfer"]["params"]
    assert {k for k, v in params.items() if v.get("required")} == {
        "bankSerial", "bankType", "payPassword", "reqKey"}


# ------------------------------------------------------------ 请求体整形


def test_two_decimal_yuan():
    assert policy._two_decimal_yuan(10) == "10.00"
    assert policy._two_decimal_yuan(9.5) == "9.50"
    assert policy._two_decimal_yuan("12.3") == "12.30"
    assert policy._two_decimal_yuan("abc") == "abc", "非法输入原样透传，别伪装成 0.00"
    assert policy._two_decimal_yuan(True) is True, "bool 不是金额"
    assert policy._two_decimal_yuan(None) is None


def test_open_hongbao_uses_context_id_alone():
    """给 contextId 就**只发**它（TS 推荐用法）；混发没有依据。"""
    assert policy._tenpay_open_hongbao(
        {"contextId": "ctx-1", "Xml": "<x/>", "SendUserName": "wxid_a"}) == {"contextId": "ctx-1"}


def test_open_hongbao_falls_back_to_compat_fields():
    assert policy._tenpay_open_hongbao({"Xml": "<x/>", "SendUserName": "wxid_a"}) == {
        "SendUserName": "wxid_a", "Xml": "<x/>"}
    assert policy._tenpay_open_hongbao({}) == {"SendUserName": "", "Xml": ""}


def test_collect_money_uses_context_id_alone_and_drops_empties():
    assert policy._tenpay_collect_money(
        {"contextId": "c1", "toUserName": "wxid_a", "transFerId": "t1"}) == {"contextId": "c1"}
    assert policy._tenpay_collect_money(
        {"toUserName": "wxid_a", "transactionId": "", "invalidTime": "60"}) == {
        "toUserName": "wxid_a", "invalidTime": "60"}


# --------------------------------------------------- 真正发出去的请求体（端到端）


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, endpoint: str, body: dict, method: str = "POST") -> dict:
        self.calls.append((endpoint, body))
        return {"Success": True, "Code": 0, "Data": {}}


class _FakeAdapter:
    def __init__(self) -> None:
        self._clients = {"default": _FakeClient()}


class _FakeCtx:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register_tool(self, *, name: str, handler: Any, **kw: Any) -> None:
        self.handlers[name] = handler


def _run(tool: str, **params: Any) -> dict:
    adapter = _FakeAdapter()
    ctx = _FakeCtx()
    registry.register_generic_tools(ctx, lambda: adapter, [DEFS[tool]])
    asyncio.run(ctx.handlers[tool](**{"account_id": "default", **params}))
    _endpoint, body = adapter._clients["default"].calls[-1]
    return body


def test_money_is_sent_as_two_decimal_string():
    assert _run("wpp_tenpay_ge_ma_skd_qcode", Money=10, Name="账户全名") == {
        "Money": "10.00", "Name": "账户全名"}
    assert _run("wpp_tenpay_generate_pay_qcode", money=9.5, name="项目") == {
        "money": "9.50", "name": "项目"}


def test_open_hongbao_body_end_to_end():
    assert _run("wpp_tenpay_open_hongbao", contextId="ctx-1", Xml="<x/>") == {"contextId": "ctx-1"}
    assert _run("wpp_tenpay_open_hongbao", Xml="<x/>", SendUserName="wxid_a") == {
        "SendUserName": "wxid_a", "Xml": "<x/>"}


def test_collect_money_body_end_to_end():
    assert _run("wpp_tenpay_collect_money", toUserName="wxid_a") == {"toUserName": "wxid_a"}
    assert _run("wpp_tenpay_collect_money", contextId="c1", toUserName="wxid_a") == {
        "contextId": "c1"}


def test_receive_hongbao_sends_xml_not_red_packet_id():
    assert _run("wpp_tenpay_receive_hongbao", Xml="<x/>") == {"Xml": "<x/>"}


@pytest.mark.parametrize("tool", ["wpp_tenpay_ge_ma_skd_qcode",
                                  "wpp_tenpay_sj_skd_qcode"])
def test_skd_qcode_never_sends_wxid(tool: str):
    body = _run(tool, Money=1, Name="n", Wxid="wxid_dead")
    assert "Wxid" not in body and "wxid" not in body
