"""TS v1.16.2 线上契约对齐的回归（2026-10-18）。

这一批修复的共同点是「**工具注册全绿、厂商请求全空转**」：TS 在
v1.6.0 / v1.6.5 / v1.16.1 连续纠正过一批端点的请求体字段，而 py2 的生成物
是从更早的旧树机械生成的 —— 旧键被厂商 Go **静默忽略**，调用照常返回成功码。
所以这里不测「注册了没有」（那是 ``test_registration.py``），只钉：

1. ``PATCH_TOOLS`` 的 transform 把**工具层参数**翻成**线上键**（逐字段）；
2. 走真实 ``registry.register_generic_tools`` 的 handler 时，发出去的
   endpoint/body 就是上面那份线上形状（闭包晚绑定、None 过滤都在这条链上）；
3. ``ADDED_GROUP`` 里补注册的端点真的注册了、方法/路径正确；
4. ``wpp_quote_reply`` 发的是 **type=57 引用卡片**（``/Msg/ShareLink``），
   不是普通文本 —— 旧实现只发了一条 SendTxt，引用关系根本不存在；
5. ``wpp_send_message`` 的 type 路由真的落到对应的 ``WppClient`` 方法。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from plugin.toolkit import policy


# ------------------------------------------------------------------ 替身


class _RecordingClient:
    """记下每次厂商调用；``call`` 与所有 ``send_*`` 方法都返回成功响应。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def call(self, endpoint: str, body: dict, method: str = "POST") -> dict:
        self.calls.append((f"call:{endpoint}", (body,), {"method": method}))
        return {"Success": True, "Code": 0,
                "Data": {"List": [{"NewMsgId": 555, "ClientMsgid": 444, "MsgId": 444}]}}

    def __getattr__(self, name: str):
        async def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"Success": True, "Code": 0,
                    "Data": {"List": [{"NewMsgId": 555, "ClientMsgid": 444, "MsgId": 444}]}}
        return _record


class _FakeAdapter:
    def __init__(self, client: _RecordingClient) -> None:
        self._clients = {"default": client}

    async def _contact_name(self, _aid: str, _wxid: str) -> str:  # pragma: no cover - 本文件不用
        return ""


class _Ctx:
    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def register_tool(self, **kw) -> None:
        assert kw["name"] not in self.tools, f"重复注册: {kw['name']}"
        self.tools[kw["name"]] = kw


def _all_vendor_defs() -> list[dict]:
    from plugin.toolkit import vendor_defs, vendor_defs_extra

    return [td for _group, defs in (*vendor_defs.GROUPS, *vendor_defs_extra.GROUPS)
            for td in defs]


def _dispatch(generic_ctx: _Ctx, name: str, params: dict):
    return asyncio.run(generic_ctx.tools[name]["handler"](**params))


# ------------------------------------------------- ① transform 逐字段


@pytest.mark.parametrize(("func", "body", "expected"), [
    # Tools 域：TS 的 toWxid/msgId/dataLen → 线上 to_wxid/msg_id/data_len/section
    (policy._tools_download_media,
     {"toWxid": "9@chatroom", "msgId": 11, "dataLen": 999, "compressType": 1},
     {"to_wxid": "9@chatroom", "msg_id": 11, "data_len": 999, "compress_type": 1,
      "section": {"start_pos": 0, "data_len": 999}}),
    (policy._tools_download_voice,
     {"fromUserName": "wxid_a", "msgId": 3, "length": 120, "bufid": "b"},
     {"fromUserName": "wxid_a", "msgId": 3, "length": 120, "bufid": "b"}),
    (policy._tools_download_file,
     {"appId": "app", "attachId": "att", "dataLen": 77, "userName": "wxid_a"},
     {"app_id": "app", "attach_id": "att", "data_len": 77,
      "section": {"start_pos": 0, "data_len": 77}, "user_name": "wxid_a"}),
    (policy._tools_download_file_binary,
     {"attachId": "att", "userName": "wxid_a", "dataLen": 77, "fileName": "a.pdf"},
     {"app_id": "", "attach_id": "att", "data_len": 77,
      "section": {"start_pos": 0, "data_len": 77}, "user_name": "wxid_a",
      "file_name": "a.pdf"}),
    (policy._tools_download_voice_binary,
     {"msgId": 5, "newMsgId": "6"},
     {"msg_id": 5, "new_msg_id": "6", "to_user_name": "", "from_user_name": "",
      "length": 0, "format": 4}),
    (policy._tools_cdn_download_image,
     {"fileAesKey": "k", "fileNo": "n"},
     {"file_aes_key": "k", "file_no": "n"}),
    (policy._tools_get_a8key, {"url": "https://mp.weixin.qq.com/x"},
     {"reqUrl": "https://mp.weixin.qq.com/x"}),
    (policy._mini_program_cover, {"url": "https://x/c.png"},
     {"url": "https://x/c.png"}),
    (policy._mini_program_cover, {"fileNo": "n", "fileAesKey": "k"},
     {"file_no": "n", "file_aes_key": "k"}),
    # User 域
    (policy._user_bind_mobile, {"mobile": "138", "code": "1234"},
     {"mobile": "138", "verifycode": "1234"}),
    (policy._user_bind_qq, {"qq": "10001", "password": "p"},
     {"account": 10001, "password": "p"}),
    (policy._user_change_password, {"newPwd": "n", "ticket": "t"},
     {"newPassword": "n", "ticket": "t"}),
    (policy._user_report_motion, {"steps": 5000},
     {"deviceId": "", "deviceType": "ipad", "stepCount": 5000}),
    (policy._user_upload_head, {"imgBase64": "AAA"},
     {"base64": "AAA"}),
    (policy._user_set_alias, {"alias": "new_id"}, {"alisa": "new_id"}),
    # TS user-meta：opt/value 都是 number（厂商函数项枚举 + 整数取值）
    (policy._user_set_privacy, {"opt": 4, "value": 1},
     {"function": 4, "value": 1}),
    # FriendCircle/GetList：厂商拼写 fristpagemd5（少一个 i）+ maxid 翻页游标
    (policy._fc_list, {"firstPageMd5": "md5", "maxid": "cursor"},
     {"fristpagemd5": "md5", "maxid": "cursor"}),
    (policy._fc_list, {}, {"fristpagemd5": "", "maxid": ""}),
    (policy._user_update_profile, {"nickname": "n"},
     {"nickName": "n", "signature": "", "sex": 0, "city": "", "country": "",
      "province": ""}),
    (policy._user_my_profile, {}, {"wxid": ""}),
    (policy._user_add_me_methods, {"phone": True, "qr_code": False},
     {"phone": True, "qr_code": False}),
    # Wxapp / Login 域
    (policy._wxapp_openid, {"toWxId": "wxid_a", "appid": "wx"},
     {"toWxId": "wxid_a", "appid": "wx"}),
    (policy._wxapp_cloud_call, {"appid": "wx", "data": "{}"},
     {"appid": "wx", "data": "{}"}),
    (policy._wxapp_operate, {"appId": "wx", "data": "{}"},
     {"appid": "wx", "data": "{}"}),
    (policy._wxapp_operate, {"appId": "wx", "data": "{}", "opt": 2},
     {"appid": "wx", "data": "{}", "opt": 2}),
    (policy._login_get_qr, {}, {"deviceType": "ipad"}),
    (policy._login_get_status, {}, {"autoLogin": "true"}),
    (policy._login_get_status, {"autoLogin": False}, {"autoLogin": "false"}),
    (policy._login_pad_cloud, {}, {"DeviceName": "我的 iPad", "oversea": False}),
    (policy._login_62data, {"url": "https://x/q"}, {"Url": "https://x/q"}),
    # CDN / 上传
    (policy._send_cdn_image, {"toWxid": "wxid_a", "imgUrl": "https://x/i.jpg"},
     {"ToWxid": "wxid_a", "Content": "https://x/i.jpg"}),
    (policy._send_cdn_video, {"toWxid": "wxid_a", "videoUrl": "https://x/v.mp4",
                              "thumbUrl": "https://x/t.jpg"},
     {"ToWxid": "wxid_a", "Content": "https://x/v.mp4"}),
    (policy._upload_file, {"base64": "AAA"}, {"base64": "AAA"}),
    (policy._upload_image, {"toWxid": "wxid_a", "imgBase64": "AAA"},
     {"ToWxid": "wxid_a", "Base64": "AAA"}),
])
def test_transform_maps_tool_params_to_wire_keys(func, body, expected):
    assert func(dict(body)) == expected


def test_bind_qq_keeps_unparseable_value_for_vendor_to_reject():
    """非数字 QQ 不吞成 0 —— 原样发给厂商报错，别把"填错了"伪装成「绑定 0 号」。"""
    assert policy._user_bind_qq({"qq": "abc", "password": "p"}) == {
        "account": "abc", "password": "p"}


def test_tenpay_money_is_two_decimal_string():
    assert policy._tenpay_money({"Money": 10, "wxid": "dead"}) == {"Money": "10.00"}


# --------------------------------------- ② 真实 handler 发出的线上形状


def test_patched_tool_sends_exact_wire_body(stubbed):  # noqa: F811
    client = _RecordingClient()
    adapter = _FakeAdapter(client)
    ctx = _Ctx()
    from plugin.toolkit import registry

    registry.register_generic_tools(ctx, lambda: adapter,
                                    policy.apply_policy(_all_vendor_defs()))

    _dispatch(ctx, "wpp_tool_download_img",
              {"toWxid": "9@chatroom", "msgId": 11, "dataLen": 999})

    endpoint, args, kwargs = client.calls[-1]
    assert endpoint == "call:/Tools/DownloadImg"
    assert kwargs["method"] == "POST"
    assert args[0] == {
        "to_wxid": "9@chatroom", "msg_id": 11, "data_len": 999,
        "compress_type": 0, "section": {"start_pos": 0, "data_len": 999},
    }

    # schema 是 TS 语义（模型看到 toWxid/msgId/dataLen），不是厂商 wire key
    schema = ctx.tools["wpp_tool_download_img"]["schema"]
    assert set(schema["properties"]) == {"toWxid", "msgId", "dataLen",
                                         "compressType", "account_id"}
    assert set(schema["required"]) == {"toWxid", "msgId", "dataLen"}

    # 朋友圈首页：旧生成物 params={} ⇒ 永远只看第一页；现在发厂商拼写的
    # fristpagemd5 + maxid（两者都可空，首页调用仍合法）。
    _dispatch(ctx, "wpp_fc_list", {"firstPageMd5": "md5", "maxid": "cursor"})
    endpoint, args, _kwargs = client.calls[-1]
    assert endpoint == "call:/FriendCircle/GetList"
    assert args[0] == {"fristpagemd5": "md5", "maxid": "cursor"}
    fc_schema = ctx.tools["wpp_fc_list"]["schema"]
    assert set(fc_schema["properties"]) == {"firstPageMd5", "maxid", "account_id"}
    assert not fc_schema.get("required")


def test_added_tools_register_with_correct_endpoints(stubbed):  # noqa: F811
    client = _RecordingClient()
    adapter = _FakeAdapter(client)
    ctx = _Ctx()
    from plugin.toolkit import registry

    defs = list(policy.ADDED_GROUP[1])
    count = registry.register_generic_tools(ctx, lambda: adapter,
                                            policy.apply_policy(defs))
    assert count == len(defs) == 35

    expected = {
        "wpp_tool_download_mini_program_cover": "/Tools/DownloadMiniProgramCover",
        "wpp_send_cdn_image": "/Msg/SendCDNImg",
        "wpp_send_cdn_video": "/Msg/SendCDNVideo",
        "wpp_send_app_message": "/Msg/SendAppMessage",
        "wpp_upload_image": "/Msg/UploadImg",
    }
    assert set(expected) <= set(ctx.tools)

    _dispatch(ctx, "wpp_send_cdn_image",
              {"toWxid": "wxid_a", "imgUrl": "https://x/i.jpg"})
    assert client.calls[-1][0] == f"call:{expected['wpp_send_cdn_image']}"
    assert client.calls[-1][1][0] == {"ToWxid": "wxid_a",
                                      "Content": "https://x/i.jpg"}

    _dispatch(ctx, "wpp_upload_image",
              {"toWxid": "wxid_a", "imgBase64": "AAA"})
    assert client.calls[-1][0] == f"call:{expected['wpp_upload_image']}"
    assert client.calls[-1][1][0] == {"ToWxid": "wxid_a", "Base64": "AAA"}

    _dispatch(ctx, "wpp_tool_download_mini_program_cover",
              {"fileNo": "n", "fileAesKey": "k"})
    assert client.calls[-1][0] == f"call:{expected['wpp_tool_download_mini_program_cover']}"
    assert client.calls[-1][1][0] == {"file_no": "n", "file_aes_key": "k"}


# ------------------------------------------------------- ③ quote_reply


def _empty_row(**_kw) -> None:
    return None


def test_quote_reply_sends_type57_card_and_mentions(stubbed, monkeypatch):  # noqa: F811
    """群聊引用回复：群聊前缀 @被回复人 + type=57 refermsg，且落 outbound 台账。"""
    from plugin.data import repo_messages

    saved: list[dict] = []

    monkeypatch.setattr(repo_messages, "get_message_by_id_or_new_id",
                        lambda *_a, **_k: {"from_wxid": "wxid_alice",
                                           "peer_name": "张三",
                                           "peer_kind": "group",
                                           "peer_id": "9@chatroom"})
    monkeypatch.setattr(repo_messages, "save_message",
                        lambda *_a, **kw: saved.append(kw))

    client = _RecordingClient()
    adapter = _FakeAdapter(client)
    ctx = _Ctx()
    from plugin.toolkit import msg

    msg.register_msg_tools(ctx, lambda: adapter)
    raw = asyncio.run(ctx.tools["wpp_quote_reply"]["handler"](
        **{"toWxid": "9@chatroom", "content": "收到", "msgId": "888",
           "newMsgId": "999"}))
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["newMsgId"] == "555"
    endpoint, args, _kwargs = client.calls[-1]
    assert endpoint == "call:/Msg/ShareLink"
    body = args[0]
    assert body["ToWxid"] == "9@chatroom"
    assert body["Type"] == 5
    xml = body["Xml"]
    assert "<type>57</type>" in xml
    assert "<svrid>999</svrid>" in xml          # newMsgId 优先
    assert "<fromusr>wxid_alice</fromusr>" in xml
    assert "@张三 收到" in xml

    assert saved and saved[0]["direction"] == "outbound"
    assert saved[0]["peer_kind"] == "group"
    assert saved[0]["msg_type"] == "quote"


def test_quote_reply_direct_chat_has_no_mention_prefix(stubbed, monkeypatch):  # noqa: F811
    from plugin.data import repo_messages

    monkeypatch.setattr(repo_messages, "get_message_by_id_or_new_id", _empty_row)
    monkeypatch.setattr(repo_messages, "save_message", lambda *_a, **_k: None)

    client = _RecordingClient()
    ctx = _Ctx()
    from plugin.toolkit import msg

    msg.register_msg_tools(ctx, lambda: _FakeAdapter(client))
    raw = asyncio.run(ctx.tools["wpp_quote_reply"]["handler"](
        **{"toWxid": "wxid_bob", "content": "好的", "msgId": "1"}))
    assert json.loads(raw)["ok"] is True
    xml = client.calls[-1][1][0]["Xml"]
    assert xml.startswith("<appmsg><title>好的</title>")
    assert "@" not in xml


# --------------------------------------------------- ④ send_message 路由


@pytest.mark.parametrize(("params", "method", "expected_args"), [
    ({"toWxid": "wxid_a", "type": "text", "content": "hi", "ats": ["wxid_b"]},
     "send_text", ("wxid_a", "hi", ["wxid_b"])),
    ({"toWxid": "wxid_a", "type": "image", "content": "https://x/i.jpg"},
     "send_image", ("wxid_a", "B64")),
    ({"toWxid": "wxid_a", "type": "video", "content": "https://x/v.mp4",
      "thumbUrl": "https://x/t.jpg", "durationMs": 15000},
     "send_video", ("wxid_a", "B64", "B64", 15)),
    ({"toWxid": "wxid_a", "type": "voice", "content": "https://x/v.silk"},
     "send_voice", ("wxid_a", "B64", 4, 0)),
    ({"toWxid": "wxid_a", "type": "link", "content": "https://x/p",
      "title": "T", "desc": "D", "thumbUrl": "https://x/t.jpg"},
     "share_link", ("wxid_a", "T", "D", "https://x/p", "https://x/t.jpg")),
    ({"toWxid": "wxid_a", "type": "card", "content": "x",
      "cardWxid": "wxid_b", "cardNickname": "B"},
     "share_card", ("wxid_a", "wxid_b", "B")),
    ({"toWxid": "wxid_a", "type": "location", "content": "x",
      "latitude": 1.5, "longitude": 2.5, "label": "L"},
     "share_location", ("wxid_a", 1.5, 2.5, "L")),
    ({"toWxid": "wxid_a", "type": "miniprogram", "content": "https://x/mp",
      "title": "T", "desc": "D", "appId": "app", "thumbUrl": "https://x/t.jpg"},
     "send_xcx", ("wxid_a", "T", "D", "https://x/mp", "app", "https://x/t.jpg", "", "")),
    ({"toWxid": "wxid_a", "type": "emoji", "content": "https://x/e.gif",
      "size": 10},
     "send_emoji", ("wxid_a", "https://x/e.gif", 10)),
])
def test_send_message_routes_each_type(stubbed, monkeypatch, params,  # noqa: F811
                                      method, expected_args):
    from plugin.toolkit import parity

    async def _fake_resolve(_ref: str) -> str:
        return "B64"

    monkeypatch.setattr("plugin.toolkit.msg._resolve_to_base64", _fake_resolve)

    client = _RecordingClient()
    ctx = _Ctx()
    parity.register_parity_tools(ctx, lambda: _FakeAdapter(client))
    raw = asyncio.run(ctx.tools["wpp_send_message"]["handler"](**params))
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["newMsgId"] == "555"
    name, args, _kwargs = client.calls[-1]
    assert name == method
    assert args == expected_args


def test_send_message_file_uses_upload_file(stubbed, monkeypatch):  # noqa: F811
    """文件消息必须走 ``/Tools/UploadFile`` + ``/Msg/ShareLink Type=6``。

    ⚠️ 以前这条锁的是 ``/Msg/UploadFile`` —— **该端点不存在**（swagger 只有
    ``/Tools/UploadFile``），测试把一个必然 404 的实现锁成了"正确"。
    """
    from plugin.toolkit import parity

    async def _fake_resolve(_ref: str) -> str:
        return "B64"

    monkeypatch.setattr("plugin.toolkit.msg._resolve_to_base64", _fake_resolve)
    client = _RecordingClient()
    ctx = _Ctx()
    parity.register_parity_tools(ctx, lambda: _FakeAdapter(client))

    raw = asyncio.run(ctx.tools["wpp_send_message"]["handler"](
        **{"toWxid": "wxid_a", "type": "file", "content": "https://x/f.pdf",
           "fileName": "f.pdf"}))
    assert json.loads(raw)["ok"] is True
    name, args, _kwargs = client.calls[-1]
    assert name == "send_file_via_app"
    assert args == ("wxid_a", "f.pdf", "B64")


def test_send_message_rejects_unknown_type(stubbed):  # noqa: F811
    from plugin.toolkit import parity

    ctx = _Ctx()
    parity.register_parity_tools(ctx, lambda: _FakeAdapter(_RecordingClient()))
    raw = asyncio.run(ctx.tools["wpp_send_message"]["handler"](
        **{"toWxid": "wxid_a", "type": "nope", "content": "x"}))
    result = json.loads(raw)
    assert result["ok"] is False
    assert "不支持的 type" in result["error"]
