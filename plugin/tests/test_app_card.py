"""小程序卡片（W7-2）—— ``plugin/inbound/app_card.py``。

## 这一面的失效形态（为什么判据下在这些地方）

移植前 py2 对 49 卡片里的小程序**零解析**，表现是"模型只看到一行标题" —— 于是
2026-09-13 那次，老板转发国补卡片，小助理回了「转发时只带了文字」：**归因错了**，
而链路上**没有任何一行日志是红的**。所以这一面的失效形态是"看起来正常但东西没进去"：

* **误命中同族的 49**：接龙/红包/转账/文件都挂在同一个 ``msgType=49`` 上，
  判宽了会把接龙当小程序卡片**改写**（`[接龙]` 那一层是 09-27 整晚吞接龙的现场）；
* **错标签**：改之前这类消息会掉进 `[文件]` 兜底 —— 模型答"你发了个文件"，
  人会以为"模型笨"，其实是标签错的；
* **标题打两遍 / 标题丢一遍**：`omit_title` 判反了两种都不报错，只是正文难看或信息没了；
* **富化把消息带崩**：缺 OSS 凭据在 `真机` 是**常态** ⇒ 必须降级成"不带图"而不是抛；
* **令牌写进 prompt**：见下。

## 有一条判据是"钉住我们**不**做的事"

TS 的 ``[小程序封面凭据] xcxthumb:…`` 那一行在本树**故意不注入**（py2 没有出站
``appattach`` 消费者，令牌发出去只会变成灰块卡片）—— 所以有一条用例专门断言
**入站正文里永不出现 ``xcxthumb:``**。它是"防止将来有人好心把它接上"的钉子，
不是凑数。
"""

from __future__ import annotations

import asyncio
import base64
import functools

import pytest

from plugin.inbound import app_card as A
from plugin.inbound.parser import InboundPipeline, MT_APP, MT_TEXT, WppMessage

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200


def _sync(coro_fn):
    """本仓没有 pytest-asyncio（见 ``test_sync_fallback.py:36`` 同款说明）⇒ 手工 ``asyncio.run``。"""
    @functools.wraps(coro_fn)
    def _wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return _wrapper


#: 真卡片形状（对齐 TS ``app-card.ts`` 文件头记的那次实测：国补卡片 720×576 封面 + 140×140 图标）
def _card_raw(**app_overrides) -> dict:
    app = {
        "category": "mini_program",
        "title": "快来领取政府家电数码补贴，单件最高补1500元",
        "description": "国家消费品换新家电补贴微信端",
        "icon_url": "https://wx.qlogo.cn/mmhead/icon-140.png",
        "mini_program": {
            "app_id": "wx1234567890abcdef",
            "page_path": "pages/index/index?activity=guobu",
            "username": "gh_1a2b3c4d5e6f@app",
            "source_display_name": "国家消费品换新家电补贴微信端",
            "version": 12,
        },
        "cover_image": {
            "md5": "d41d8cd98f00b204e9800998ecf8427e",
            "width": 720,
            "height": 576,
            "cdn_length": 51234,
            "download_context": {"url": "https://cdn.example.com/cover.jpg",
                                 "file_no": "305f020100044b3049abcdef",
                                 "file_aes_key": "0123456789abcdef0123456789abcdef"},
        },
    }
    app.update(app_overrides)
    return {"app": app}


def _msg(raw: dict, content: str = "") -> WppMessage:
    return WppMessage(account_id="default", msg_id="m1", from_wxid="wxid_boss",
                      content=content or raw["app"].get("title", ""), msg_type=MT_APP,
                      chatroom_id="1234@chatroom", raw=raw)


# ============================================================== 1. 识别


def test_a_real_subsidy_card_is_recognized():
    assert A.is_mini_program_card(_card_raw()) is True


@pytest.mark.parametrize("category", ["app_message", "file", "quote", "payment_notice", "", "  "])
def test_no_other_category_of_the_same_family_is_a_card(category):
    """同族 49 消息的 ``category`` 各不相同 —— 接龙/文件/引用/转账一个都不许命中。"""
    assert A.is_mini_program_card({"app": {"category": category}}) is False


@pytest.mark.parametrize("raw", [{}, None, [], "mini_program", 3, {"app": None}, {"app": []},
                                 {"app": {"category": 1}}, {"app": "mini_program"}])
def test_the_predicate_never_raises_on_arbitrary_shapes(raw):
    assert A.is_mini_program_card(raw) is False


def test_the_category_comparison_trims_but_stays_case_sensitive():
    """TS 是 ``str(x) === "mini_program"``：先 trim 再**逐字**比 —— 大小写不同就是不同。"""
    assert A.is_mini_program_card({"app": {"category": "  mini_program  "}}) is True
    assert A.is_mini_program_card({"app": {"category": "Mini_Program"}}) is False


# ============================================================== 2. 解析


def test_every_field_of_a_real_card_maps_across():
    info = A.parse_mini_program_card(_card_raw())
    assert info is not None
    assert info.title == "快来领取政府家电数码补贴，单件最高补1500元"
    assert info.app_id == "wx1234567890abcdef"
    assert info.page_path == "pages/index/index?activity=guobu"
    assert info.username == "gh_1a2b3c4d5e6f@app"
    assert info.source_display_name == "国家消费品换新家电补贴微信端"
    assert info.version == 12
    assert info.cover is not None
    assert info.cover.url == "https://cdn.example.com/cover.jpg"
    assert info.cover.file_no == "305f020100044b3049abcdef"
    assert info.cover.file_aes_key == "0123456789abcdef0123456789abcdef"
    assert info.cover.md5 == "d41d8cd98f00b204e9800998ecf8427e"
    assert info.cover.width == 720
    assert info.cover.height == 576
    assert info.cover.length == 51234
    assert info.cover.icon_url == "https://wx.qlogo.cn/mmhead/icon-140.png"


def test_a_non_card_parses_to_nothing():
    assert A.parse_mini_program_card({"app": {"category": "file"}}) is None
    assert A.parse_mini_program_card(None) is None


def test_the_download_context_wins_over_the_top_level_cover_fields():
    """``download_context`` 是厂商文档那条"已校验的直链"，同名字段以它为准。"""
    raw = _card_raw()
    raw["app"]["cover_image"]["url"] = "https://old.example.com/x.jpg"
    raw["app"]["cover_image"]["file_no"] = "old-file-no"
    raw["app"]["cover_image"]["aes_key"] = "old-aes-key"
    info = A.parse_mini_program_card(raw)
    assert info.cover.url == "https://cdn.example.com/cover.jpg"
    assert info.cover.file_no == "305f020100044b3049abcdef"
    assert info.cover.file_aes_key == "0123456789abcdef0123456789abcdef"


def test_the_top_level_fields_are_still_read_when_there_is_no_download_context():
    raw = _card_raw()
    raw["app"]["cover_image"] = {"url": "https://cdn.example.com/cover.jpg",
                                 "file_no": "deadbeefdeadbeef",
                                 "aes_key": "fedcba9876543210fedcba9876543210"}
    info = A.parse_mini_program_card(raw)
    assert (info.cover.url, info.cover.file_no, info.cover.file_aes_key) == (
        "https://cdn.example.com/cover.jpg", "deadbeefdeadbeef",
        "fedcba9876543210fedcba9876543210")


def test_the_length_falls_back_to_the_nested_cdn_length():
    raw = _card_raw()
    raw["app"]["cover_image"].pop("cdn_length")
    raw["app"]["cover_image"]["cdn"] = {"length": 4096}
    assert A.parse_mini_program_card(raw).cover.length == 4096


def test_numeric_strings_are_read_as_numbers():
    """vendor 的 Go 结构体在某些路径下把数字序列化成字符串（TS ``num()`` 专门兜这个）。"""
    raw = _card_raw()
    raw["app"]["mini_program"]["version"] = "12"
    raw["app"]["cover_image"]["width"] = "720"
    info = A.parse_mini_program_card(raw)
    assert info.version == 12
    assert info.cover.width == 720


def test_a_boolean_is_not_a_number():
    """⚠️ Python 的 ``bool`` 是 ``int`` 的子类，JS 不是 —— 不挡就会把 ``true`` 当 ``1``。"""
    raw = _card_raw()
    raw["app"]["mini_program"]["version"] = True
    raw["app"]["cover_image"]["width"] = False
    info = A.parse_mini_program_card(raw)
    assert info.version is None
    assert info.cover.width is None


def test_blank_and_missing_strings_become_absent():
    raw = _card_raw(title="   ", description=None, icon_url="")
    raw["app"]["mini_program"]["page_path"] = ""
    info = A.parse_mini_program_card(raw)
    assert info.title == ""
    assert info.description == ""
    assert info.page_path == ""
    assert info.cover.icon_url == ""


def test_strings_are_trimmed():
    raw = _card_raw(title="  标题  ")
    assert A.parse_mini_program_card(raw).title == "标题"


def test_a_card_without_a_cover_block_has_no_cover():
    raw = _card_raw()
    raw["app"].pop("cover_image")
    assert A.parse_mini_program_card(raw).cover is None


def test_an_empty_cover_block_still_yields_a_cover_object():
    """TS 判的是"这个对象在不在"（``{}`` 也是真值）⇒ 空块要给一个对象，不是 ``None``。

    ⚠️ 这一条钉的是 **JS/Python 的真值分叉**：同一份 payload 在 JS 那边 ``{}`` 为真、
    在 Python 这边 ``if cover:`` 为假 —— 写错一次，卡片就静默少掉一路"只有图标"的降级。
    ``icon_url`` 来自 ``app.icon_url``（不在 ``cover_image`` 里），所以它**不是**空的。
    """
    raw = _card_raw(cover_image={})
    cover = A.parse_mini_program_card(raw).cover
    assert cover is not None
    assert (cover.url, cover.file_no, cover.file_aes_key, cover.length) == ("", "", "", None)
    assert cover.icon_url == "https://wx.qlogo.cn/mmhead/icon-140.png"


# ============================================================== 3. 文本化


def _full_block(**kw) -> str:
    return A.format_mini_program_card(A.parse_mini_program_card(_card_raw()), **kw)


def test_the_block_is_exactly_what_the_model_says_ts_prints():
    assert _full_block() == "\n".join([
        "[小程序卡片] 快来领取政府家电数码补贴，单件最高补1500元",
        "[小程序] 国家消费品换新家电补贴微信端 (appid wx1234567890abcdef, gh_1a2b3c4d5e6f@app)",
        "页面路径: pages/index/index?activity=guobu",
    ])


def test_the_title_line_is_dropped_when_the_body_already_carries_it():
    assert _full_block(omit_title=True) == "\n".join([
        "[小程序] 国家消费品换新家电补贴微信端 (appid wx1234567890abcdef, gh_1a2b3c4d5e6f@app)",
        "页面路径: pages/index/index?activity=guobu",
    ])


def test_a_description_equal_to_the_mini_program_name_is_printed_once():
    """国补卡片实测两者一模一样（都是「国家消费品换新家电补贴微信端」）⇒ 只打一行。"""
    assert "说明:" not in _full_block()


def test_a_genuinely_different_description_gets_its_own_line():
    raw = _card_raw(description="单件最高补 1500 元")
    block = A.format_mini_program_card(A.parse_mini_program_card(raw))
    assert "说明: 单件最高补 1500 元" in block


def test_the_cover_and_the_icon_are_labelled_separately():
    """封面（720×576 卡片图）与图标（140×140 logo）混标会让模型以为看到了卡片大图。"""
    block = _full_block(cover_url="https://oss/cover.jpg", icon_url="https://oss/icon.png")
    assert "[小程序封面] https://oss/cover.jpg" in block
    assert "[小程序图标] https://oss/icon.png" in block


def test_the_icon_line_replaces_the_cover_line_when_only_the_icon_was_fetched():
    block = _full_block(icon_url="https://oss/icon.png")
    assert "[小程序封面]" not in block
    assert "[小程序图标] https://oss/icon.png" in block


def test_a_card_with_no_appid_and_no_username_still_says_which_mini_program():
    """``appid`` 与 ``gh_…`` 都没有时括号整块不出现（不是打出「()」）。"""
    raw = _card_raw()
    raw["app"]["mini_program"].pop("app_id")
    raw["app"]["mini_program"].pop("username")
    block = A.format_mini_program_card(A.parse_mini_program_card(raw))
    assert block.splitlines()[1] == "[小程序] 国家消费品换新家电补贴微信端"


def test_a_card_with_nothing_to_name_the_mini_program_still_says_something():
    raw = _card_raw()
    raw["app"]["mini_program"] = {}
    block = A.format_mini_program_card(A.parse_mini_program_card(raw))
    assert block.splitlines()[1] == "[小程序] 小程序"


def test_the_username_is_the_fallback_when_there_is_no_display_name():
    raw = _card_raw()
    raw["app"]["mini_program"].pop("source_display_name")
    block = A.format_mini_program_card(A.parse_mini_program_card(raw))
    assert "[小程序] gh_1a2b3c4d5e6f@app (appid wx1234567890abcdef, gh_1a2b3c4d5e6f@app)" in block


def test_the_token_line_is_rendered_when_one_is_handed_in():
    """形参与 TS 一致（将来接上出站 XML 卡片时只需在调用点补一个实参）。

    ⚠️ 同时钉住"**没有任何调用方会传**"这件事 —— 见 :func:`test_the_inbound_block_never_carries_the_thumb_token`。
    """
    block = _full_block(cover_token="xcxthumb:305f020100044b3049:0123456789abcdef0123456789abcdef")
    assert block.splitlines()[-1] == (
        "[小程序封面凭据] xcxthumb:305f020100044b3049:0123456789abcdef0123456789abcdef"
        " (转发时原样填 thumbUrl)")


def test_a_page_path_less_card_has_no_page_line():
    raw = _card_raw()
    raw["app"]["mini_program"].pop("page_path")
    assert not any(line.startswith("页面路径:") for line in A.format_mini_program_card(
        A.parse_mini_program_card(raw)).splitlines())


# ============================================================== 4. 接线


def _pipeline() -> InboundPipeline:
    return InboundPipeline("default", {"selfWxid": "wxid_bot", "nickname": "小助理"})


def test_a_mini_program_card_is_no_longer_labelled_a_file():
    """★ 这是本项要修的那个**错标签**：改之前它会掉进 ``[文件]`` 兜底。"""
    msg = _msg(_card_raw())
    assert _pipeline()._media_tag(msg, "标题") == ""


def test_the_other_49_subtypes_keep_their_tags():
    """顺序不能动：接龙/红包/转账的判据都还在，且都排在兜底之前。"""
    p = _pipeline()
    assert p._media_tag(_msg({"app": {"category": "app_message"}}, "接龙 1. 张三"), "接龙 1. 张三") == "[接龙]"
    assert p._media_tag(_msg({"app": {"category": "file"}}, "报表.xlsx"), "报表.xlsx") == "[文件]"


def test_prepare_text_appends_the_block_after_the_title():
    msg = _msg(_card_raw())
    lines = _pipeline().prepare_text(msg).splitlines()
    assert lines[0] == "快来领取政府家电数码补贴，单件最高补1500元"
    assert lines[1].startswith("[小程序] 国家消费品换新家电补贴微信端")


def test_the_title_is_not_printed_twice_when_the_vendor_content_already_has_it():
    """``omit_title`` 比的是**厂商 content 原文**（TS 那边比的就是 ``m.content``）。"""
    msg = _msg(_card_raw())
    text = _pipeline().prepare_text(msg)
    assert text.count("快来领取政府家电数码补贴，单件最高补1500元") == 1


def test_a_title_the_body_does_not_carry_is_prepended_in_the_block():
    raw = _card_raw()
    msg = _msg(raw, content="[转发]")
    # content 里没有标题 ⇒ 不 omit ⇒ ``[小程序卡片] 标题`` 那一行要出现
    assert "[小程序卡片] 快来领取政府家电数码补贴，单件最高补1500元" in _pipeline().prepare_text(msg)


def test_the_block_takes_the_cover_url_from_media_when_the_kind_says_cover():
    msg = _msg(_card_raw())
    msg.media = {"mp_asset_url": "https://oss/cover.jpg", "mp_asset_kind": "cover"}
    assert "[小程序封面] https://oss/cover.jpg" in _pipeline().prepare_text(msg)


def test_the_block_takes_the_icon_url_from_media_when_the_kind_says_icon():
    msg = _msg(_card_raw())
    msg.media = {"mp_asset_url": "https://oss/icon.png", "mp_asset_kind": "icon"}
    text = _pipeline().prepare_text(msg)
    assert "[小程序图标] https://oss/icon.png" in text
    assert "[小程序封面]" not in text


def test_an_asset_without_a_kind_is_not_labelled_at_all():
    """``kind`` 是"这图到底是封面还是图标"的唯一判据 —— 缺了就**谁都不标**（宁可不给）。"""
    msg = _msg(_card_raw())
    msg.media = {"mp_asset_url": "https://oss/x.jpg"}
    text = _pipeline().prepare_text(msg)
    assert "https://oss/x.jpg" not in text


def test_the_inbound_block_never_carries_the_thumb_token():
    """★ 有意不注入（见模块 docstring「未移植的一截」）：py2 没有出站 ``appattach`` 消费者。

    令牌只在 TS 的 ``buildMiniProgramCardXml`` 里有用（解进 ``<appattach>`` 再发）；
    py2 的工具面连 ``pagePath``/``username`` 参数都没有，``thumbUrl`` 是**原样**发给厂商的
    ⇒ 把令牌写进 prompt 等于教模型"照做就发出灰块卡片"。这条钉子防的是**将来好心接上**。
    """
    msg = _msg(_card_raw())
    msg.media = {"mp_asset_url": "https://oss/cover.jpg", "mp_asset_kind": "cover"}
    for text in (_pipeline().prepare_text(msg),
                 A.mini_program_block(msg),
                 A.format_mini_program_card(A.parse_mini_program_card(_card_raw()))):
        assert "xcxthumb:" not in text
        assert "封面凭据" not in text


@pytest.mark.parametrize("mtype,content", [(MT_TEXT, "普通一句话"), (MT_APP, "报表.xlsx")])
def test_every_other_message_type_is_byte_identical_to_before(mtype, content):
    """回归：非小程序卡片的正文一个字都不许多出来（这是新加的**追加**，不是改写）。"""
    msg = WppMessage(account_id="default", msg_id="m2", from_wxid="wxid_boss",
                     content=content, msg_type=mtype, raw={"app": {"category": "file"}})
    assert _pipeline().prepare_text(msg) == content or _pipeline().prepare_text(msg) == f"[文件] {content}"


def test_a_message_whose_raw_is_not_a_dict_is_untouched():
    msg = WppMessage(account_id="default", msg_id="m3", from_wxid="w", content="你好",
                     msg_type=MT_APP, raw=None)
    assert A.mini_program_block(msg) == ""
    assert _pipeline().prepare_text(msg) == "[文件] 你好"


# ============================================================== 5. 下载路


class _FakeClient:
    """假厂商：每次 ``call`` 按顺序吐一个预置结果（``Exception`` 表示这一次抛）。"""

    def __init__(self, *responses):
        self.calls: list[tuple[str, dict]] = []
        self._responses = list(responses)

    async def call(self, endpoint, body=None, **kw):
        self.calls.append((endpoint, dict(body or {})))
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _image_response(buf: bytes = PNG) -> dict:
    return {"Code": 0, "Data": {"Image": base64.b64encode(buf).decode()}}


def test_the_cover_url_beats_the_file_credentials():
    cover = A.MiniProgramCover(url="https://cdn/cover.jpg", file_no="ab" * 8, file_aes_key="cd" * 16)
    assert [(a.kind, a.body) for a in A.plan_asset_attempts(cover)] == [
        ("cover", {"url": "https://cdn/cover.jpg"})]


def test_the_file_credentials_are_used_when_there_is_no_url():
    cover = A.MiniProgramCover(file_no="ab" * 8, file_aes_key="cd" * 16)
    assert [(a.kind, a.body) for a in A.plan_asset_attempts(cover)] == [
        ("cover", {"file_no": "ab" * 8, "file_aes_key": "cd" * 16})]


def test_the_icon_is_a_second_attempt_only_when_it_differs_from_the_cover():
    same = A.MiniProgramCover(url="https://cdn/x.jpg", icon_url="https://cdn/x.jpg")
    assert len(A.plan_asset_attempts(same)) == 1
    both = A.MiniProgramCover(url="https://cdn/cover.jpg", icon_url="https://cdn/icon.png")
    assert [(a.kind, a.body) for a in A.plan_asset_attempts(both)] == [
        ("cover", {"url": "https://cdn/cover.jpg"}), ("icon", {"url": "https://cdn/icon.png"})]


def test_a_cover_with_only_an_icon_still_tries_the_icon():
    cover = A.MiniProgramCover(icon_url="https://cdn/icon.png")
    assert [a.kind for a in A.plan_asset_attempts(cover)] == ["icon"]


def test_a_cover_with_nothing_usable_yields_no_attempt():
    assert A.plan_asset_attempts(A.MiniProgramCover(md5="d41d8cd98f00b204e9800998ecf8427e")) == []
    assert A.plan_asset_attempts(None) == []


@pytest.mark.parametrize("buf,want", [
    (JPEG, "jpg"), (PNG, "png"), (b"GIF89a" + b"\x00" * 10, "gif"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"), (b"nonsense" * 20, "jpg"), (b"\x89PN", "jpg"),
])
def test_the_extension_comes_from_the_magic_bytes(buf, want):
    assert A.sniff_image_ext(buf) == want


@_sync
async def test_no_attempt_means_no_vendor_call_at_all():
    client = _FakeClient()
    r = await A.enrich_mini_program_asset(client, "default", None)
    assert (r.media_url, r.kind) == ("", "")
    assert r.error == "no cover url/file_no/icon_url"
    assert client.calls == []


@_sync
async def test_missing_oss_credentials_short_circuits_before_the_vendor_call(monkeypatch):
    """★ OSS 没配好就别去下载（TS 同序）—— 否则每次卡片都白打一次厂商 + 一次失败日志。"""
    monkeypatch.setattr(A, "load_oss_config", lambda: None)
    client = _FakeClient(_image_response())
    r = await A.enrich_mini_program_asset(client, "default", A.MiniProgramCover(url="https://cdn/c.jpg"))
    assert r.error == "oss credentials missing"
    assert client.calls == []


@_sync
async def test_the_cover_is_uploaded_under_the_miniprogram_kind_with_the_md5_name(monkeypatch):
    monkeypatch.setattr(A, "load_oss_config", lambda: object())
    seen: list[tuple] = []

    async def _fake_upload(buf, filename, prefix, *, account_id=None, kind=None):
        seen.append((buf, filename, prefix, account_id, kind))
        return "https://openclaw-a.oss-cn-hangzhou.aliyuncs.com/x/cover.png"

    monkeypatch.setattr(A, "oss_upload_buffer", _fake_upload)
    client = _FakeClient(_image_response(PNG))
    cover = A.MiniProgramCover(url="https://cdn/c.jpg", md5="d41d8cd98f00b204e9800998ecf8427e")
    r = await A.enrich_mini_program_asset(client, "acct1", cover)
    assert r == A.AssetResult(media_url="https://openclaw-a.oss-cn-hangzhou.aliyuncs.com/x/cover.png",
                              media_size=len(PNG), kind="cover", error="")
    assert seen == [(PNG, "d41d8cd98f00b204e9800998ecf8427e.png", "wpp/miniprogram",
                     "acct1", "miniprogram")]
    assert client.calls == [("/Tools/DownloadMiniProgramCover", {"url": "https://cdn/c.jpg"})]


@_sync
async def test_a_failed_cover_falls_back_to_the_icon_and_says_which_one_it_got(monkeypatch):
    """封面这条路在真卡片上**恒失败**（厂商 -5103017）⇒ 降级到图标是常规路径，不是异常。"""
    monkeypatch.setattr(A, "load_oss_config", lambda: object())

    async def _fake_upload(buf, filename, prefix, *, account_id=None, kind=None):
        return "https://oss/icon.png"

    monkeypatch.setattr(A, "oss_upload_buffer", _fake_upload)
    client = _FakeClient({"Code": -8, "CodeValue": "FAIL", "Message": "CDN 返回错误码 -5103017"},
                         _image_response(PNG))
    cover = A.MiniProgramCover(url="https://cdn/cover.jpg", icon_url="https://cdn/icon.png")
    r = await A.enrich_mini_program_asset(client, "default", cover)
    assert r.kind == "icon"
    assert r.media_url == "https://oss/icon.png"
    assert [c[1] for c in client.calls] == [{"url": "https://cdn/cover.jpg"},
                                            {"url": "https://cdn/icon.png"}]


@_sync
async def test_every_attempt_failing_returns_no_url_and_the_last_reason(monkeypatch):
    monkeypatch.setattr(A, "load_oss_config", lambda: object())
    client = _FakeClient({"Code": -8, "Data": None, "Message": "CDN 返回错误码 -5103017"},
                         {"Code": -8, "Data": None, "Message": "CDN 返回错误码 -5103017"})
    cover = A.MiniProgramCover(url="https://cdn/cover.jpg", icon_url="https://cdn/icon.png")
    r = await A.enrich_mini_program_asset(client, "default", cover)
    assert (r.media_url, r.media_size) == ("", None)
    assert r.error.startswith("icon: ")
    assert "-5103017" in r.error


@_sync
async def test_a_missing_image_field_reports_the_vendor_message(monkeypatch):
    """排障关键：只看 ``Code=-8`` 查不出原因，``Message`` 才写着 CDN 错误码。"""
    monkeypatch.setattr(A, "load_oss_config", lambda: object())
    client = _FakeClient({"Code": -8, "CodeValue": "INVALID_ARGUMENT", "Data": {},
                          "Message": "异常：小程序封面下载失败：CDN 返回错误码 -5103017"})
    r = await A.enrich_mini_program_asset(client, "default", A.MiniProgramCover(url="https://c/x"))
    assert "code=-8" in r.error and "value=INVALID_ARGUMENT" in r.error
    assert "-5103017" in r.error


@_sync
async def test_a_too_small_payload_is_refused(monkeypatch):
    monkeypatch.setattr(A, "load_oss_config", lambda: object())
    client = _FakeClient(_image_response(b"\x89PNG" + b"\x00" * 10))
    r = await A.enrich_mini_program_asset(client, "default", A.MiniProgramCover(url="https://c/x"))
    assert "too small" in r.error


@_sync
async def test_an_upload_failure_is_swallowed_not_raised(monkeypatch):
    """上传失败 = 这条消息不带图（``media_oss`` 那条不变量的下游出口）。"""
    monkeypatch.setattr(A, "load_oss_config", lambda: object())

    async def _boom(*a, **k):
        raise RuntimeError("没有 OSS 凭据")

    monkeypatch.setattr(A, "oss_upload_buffer", _boom)
    client = _FakeClient(_image_response())
    r = await A.enrich_mini_program_asset(client, "default", A.MiniProgramCover(url="https://c/x"))
    assert r.media_url == "" and "cover: " in r.error and "没有 OSS 凭据" in r.error


@_sync
async def test_the_vendor_call_raising_is_also_swallowed(monkeypatch):
    monkeypatch.setattr(A, "load_oss_config", lambda: object())
    client = _FakeClient(RuntimeError("网络断了"))
    r = await A.enrich_mini_program_asset(client, "default", A.MiniProgramCover(url="https://c/x"))
    assert r.media_url == "" and "网络断了" in r.error


@_sync
async def test_a_payload_that_is_not_base64_does_not_raise(monkeypatch):
    monkeypatch.setattr(A, "load_oss_config", lambda: object())
    client = _FakeClient({"Code": 0, "Data": {"Image": "!!!not base64!!!"}})
    r = await A.enrich_mini_program_asset(client, "default", A.MiniProgramCover(url="https://c/x"))
    assert r.media_url == ""
    assert r.error.startswith("cover: ")
