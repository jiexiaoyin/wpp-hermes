"""媒体富化（W7-1）—— OSS 直链写回 ``msg.content`` 的那一截。

## 这一面的失效形态：**上传写完了，但没有一个人调它**

``plugin/inbound/media_oss.py``（上传那半）在 W7-1 的前半段就做完了，可 ``_fetch_media``
只把二进制下到 Hermes 本地缓存 —— 而**本地路径进不了任何下游**：心流抠图
（``heartflow.extract_hf_image_urls``）只认 ``^https?://``，judge 的
``filter_image_urls`` 只认 ``^https?://|data:image/``。于是 v1.11.0 的"看图"在 py2 是
**一整段空转**：正文里那行 ``[图片]`` 是 ``_media_tag`` 打的**前缀**，后面永远没有 URL，
而现场表现只是"bot 看不懂图" —— 没有任何一行日志是红的。

所以这一套的判据全部下在**正文的字节**上（真写进去了吗？写在哪一侧？降级时有没有
偷偷写半个？），而不是"调用了 ``upload_media_bytes``"。

## 两处"钉住我们**不**做的事"

1. **降级路径必须让正文逐字节不变**：``真机`` 上既没有 ossutil 也没有 OSS 凭据
   （老板 2026-10-02 二次确认「先不放」）⇒ "没拿到 URL"就是**现状那条路**，
   动它等于偷偷改了生产行为。
2. **注入是追加、且带前导 ``\\n``**（TS ``m.content = \\`${m.content}\\n[图片] ${url}\\```），
   而 ``_media_tag`` 那套是**前缀** —— 两条路混了正文里就会出现两个 ``[图片]``。
"""

from __future__ import annotations

import asyncio

import pytest

from plugin.inbound.parser import MT_APP, MT_IMAGE, MT_TEXT, MT_VOICE, InboundPipeline, WppMessage


@pytest.fixture(scope="module")
def A(stubbed):
    """装上 gateway 空壳之后再导适配器（那几个纯函数住在 ``platform/adapter.py`` 里）。"""
    import plugin.platform.adapter as m
    return m


@pytest.fixture(scope="module")
def AM(stubbed):
    """媒体方法 2026-10-19 起住在 ``platform/adapter_media.py``（MediaMixin）。

    patch 必须打在**生产实际读取的模块**上：mixin 方法体内读的是
    ``adapter_media.upload_media_bytes``，打在 ``adapter`` 上无效。
    """
    import plugin.platform.adapter_media as mm
    return mm


def _msg(content: str = "", msg_type: int = MT_IMAGE, media: dict | None = None) -> WppMessage:
    return WppMessage(
        account_id="default", msg_id="m1", from_wxid="wxid_a",
        chatroom_id="123@chatroom", content=content, msg_type=msg_type,
        media=dict(media or {}),
    )


def _pipeline() -> InboundPipeline:
    return InboundPipeline("default", {"selfWxid": "wxid_bot", "nickname": "小助理"})


class _Acct:
    """只要 ``id``：``_publish_media`` 只用它拼 OSS 路径。

    ⚠️ 这里**不能**写 ``"default"`` —— 变异体正是把 ``account_id=acct.id`` 写死成
    ``"default"``（多账号共用一个 OSS 目录），同值就分不出来了（第一轮 SURVIVED）。
    """
    id = "acct-42"


def _bare(A):
    """不跑 ``__init__`` 造个壳 —— ``_publish_media`` 不碰任何实例状态，
    而基类构造要 Hermes 的运行期配置（本机没有）。"""
    return object.__new__(A.WppAdapter)


# ============================================================ ① 四个串逐字（TS 口径）


def test_the_image_line_is_the_ts_one(A):
    """``handler.ts:232`` ``m.content = \\`${m.content}\\n[图片] ${url}\\```。"""
    assert A._media_enrich_line(A.KIND_IMAGES, "https://oss/a.jpg") == "[图片] https://oss/a.jpg"


def test_the_video_line_is_the_ts_one(A):
    """``handler.ts:315``。"""
    assert A._media_enrich_line(A.KIND_VIDEOS, "https://oss/v.mp4") == "[视频] https://oss/v.mp4"


def test_the_voice_line_is_the_ts_one(A):
    """``handler.ts:372`` —— ``[转写]`` 那一截由 ``prepare_text`` 另接（见 ③）。"""
    assert A._media_enrich_line(A.KIND_VOICES, "https://oss/v.silk") == "[语音] https://oss/v.silk"


def test_the_file_line_carries_name_and_size(A):
    """``handler.ts:392``：``[文件] ${filename} (${size ?? "?"} bytes) ${url}``。"""
    line = A._media_enrich_line(A.KIND_FILES, "https://oss/f.xlsx",
                                filename="报价单.xlsx", size=1234)
    assert line == "[文件] 报价单.xlsx (1234 bytes) https://oss/f.xlsx"


def test_only_the_file_line_carries_name_and_size(A):
    """非文件的三行**不带**名字/大小 —— 分支写宽了会漏出 ``[图片] a.jpg (12 bytes) u``。"""
    for kind, tag in ((A.KIND_IMAGES, "图片"), (A.KIND_VIDEOS, "视频"), (A.KIND_VOICES, "语音")):
        assert A._media_enrich_line(kind, "u", filename="a.jpg", size=12) == f"[{tag}] u"


def test_every_upload_kind_has_a_marker(A):
    """四个 OSS 目录**每个**都得有中文标记 —— 少一个就是 KeyError，而且只在"上传成功"
    那一刻炸（本地无凭据时永远发现不了）。"""
    assert set(A._OSS_KIND_TAG) == {A.KIND_IMAGES, A.KIND_VIDEOS, A.KIND_VOICES, A.KIND_FILES}


# ============================================================ ② 注入正文的字节


def test_the_line_is_appended_after_the_text_not_prefixed(A):
    msg = _msg(content="看这个")
    A._inject_media_enrich_line(msg, A.KIND_IMAGES, "https://oss/a.jpg")
    assert msg.content == "看这个\n[图片] https://oss/a.jpg"


def test_an_empty_content_yields_the_ts_leading_newline(A):
    """TS 是 ``\\`${m.content}\\n[图片] ${url}\\``` ⇒ 正文为空时**就是** ``"\\n[图片] url"``。
    下游 ``strip_group_prefix`` 会把这个前导换行 strip 掉（见 ③），但**写出来**的形状
    与 TS 逐字一致 —— 别"顺手优化"成不带 ``\\n`` 的版本。"""
    msg = _msg(content="")
    A._inject_media_enrich_line(msg, A.KIND_IMAGES, "https://oss/a.jpg")
    assert msg.content == "\n[图片] https://oss/a.jpg"


def test_the_two_keys_are_written_together_with_the_line(A):
    """``oss_url`` / ``oss_kind`` 是 ``_media_tag`` 与 ``prepare_text`` 的判据 ——
    见 :func:`_inject_media_enrich_line` 的 docstring（写半个比不写更坏）。"""
    msg = _msg()
    A._inject_media_enrich_line(msg, A.KIND_VIDEOS, "https://oss/v.mp4")
    assert msg.media["oss_url"] == "https://oss/v.mp4"
    assert msg.media["oss_kind"] == A.KIND_VIDEOS


def test_injecting_twice_keeps_one_line(A):
    """幂等：同一条消息被取两次媒体时不许追加第二次（模型会以为发了两张图）。"""
    msg = _msg(content="看这个")
    A._inject_media_enrich_line(msg, A.KIND_IMAGES, "https://oss/a.jpg")
    A._inject_media_enrich_line(msg, A.KIND_IMAGES, "https://oss/a.jpg")
    assert msg.content == "看这个\n[图片] https://oss/a.jpg"


def test_a_file_line_reaches_the_content_with_its_name_and_size(A):
    msg = _msg(msg_type=MT_APP)
    A._inject_media_enrich_line(msg, A.KIND_FILES, "https://oss/f.xlsx",
                                filename="报价单.xlsx", size=1234)
    assert msg.content == "\n[文件] 报价单.xlsx (1234 bytes) https://oss/f.xlsx"


# ============================================================ ③ _publish_media：上传 + 降级


def test_publish_forwards_account_kind_bytes_and_filename(A, AM, monkeypatch):
    seen: dict = {}

    async def fake_upload(data, filename, *, account_id, kind):
        seen.update(data=data, filename=filename, account_id=account_id, kind=kind)
        return "https://oss/a.jpg"

    monkeypatch.setattr(AM, "upload_media_bytes", fake_upload)
    msg = _msg(content="看这个")
    asyncio.run(A.WppAdapter._publish_media(
        _bare(A), _Acct(), msg, kind=A.KIND_IMAGES, filename="m1.jpg", data=b"PNG"))
    assert seen == {"data": b"PNG", "filename": "m1.jpg",
                    "account_id": "acct-42", "kind": "images"}
    assert msg.content == "看这个\n[图片] https://oss/a.jpg"


def test_a_failed_upload_leaves_the_content_byte_identical(A, AM, monkeypatch):
    """★ 降级判据：没拿到 URL ⇒ 正文**一个字都不能变**、两个键一个都不许写。

    ``真机`` 上既没 ossutil 也没凭据 ⇒ 这条就是**现状那条路**；
    而"写了 URL 却没写 ``oss_url``"或者反过来，都会让正文里的 ``[图片]``
    要么一个没有、要么两个（见 ``_media_tag``）。"""
    async def no_url(*_a, **_k):
        return None

    monkeypatch.setattr(AM, "upload_media_bytes", no_url)
    msg = _msg(content="看这个")
    asyncio.run(A.WppAdapter._publish_media(
        _bare(A), _Acct(), msg, kind=A.KIND_IMAGES, filename="m1.jpg", data=b"PNG"))
    assert msg.content == "看这个"
    assert "oss_url" not in msg.media
    assert "oss_kind" not in msg.media


def test_the_file_size_is_what_we_actually_uploaded(A, AM, monkeypatch):
    """文件行的字节数取 ``len(data)``，**不是**厂商声明的 ``data_len``：
    下载被截断时声明值会说谎，而这一行就是给模型看的"这文件多大"。"""
    async def fake_upload(*_a, **_k):
        return "https://oss/f.xlsx"

    monkeypatch.setattr(AM, "upload_media_bytes", fake_upload)
    msg = _msg(msg_type=MT_APP)
    asyncio.run(A.WppAdapter._publish_media(
        _bare(A), _Acct(), msg, kind=A.KIND_FILES, filename="报价单.xlsx", data=b"123"))
    assert msg.content == "\n[文件] 报价单.xlsx (3 bytes) https://oss/f.xlsx"


# ============================================================ ④ 正文口径：不重复打标记


def test_an_enriched_image_has_exactly_one_image_marker(A):
    """正文里**只有** enrich 那一行 ``[图片]`` —— ``_media_tag`` 的前缀那条必须让路。

    ``strip_group_prefix`` / ``normalize_mention`` 都会 ``.strip()``（parser.py:164/509）
    ⇒ TS 那个前导 ``\\n`` 到这里已经没了；判据是"正文里恰好一个 ``[图片]``"。"""
    msg = _msg(content="看这个")
    A._inject_media_enrich_line(msg, A.KIND_IMAGES, "https://oss/a.jpg")
    text = _pipeline().prepare_text(msg)
    assert text == "看这个\n[图片] https://oss/a.jpg", text
    assert text.count("[图片]") == 1


def test_the_enriched_voice_text_matches_ts_byte_for_byte(A):
    """TS：``\\`${content}\\n[语音] ${url}${sttSuffix}\\```，``sttSuffix = "\\n[转写] stt"``。

    py2 的 ``[转写]`` 由 ``prepare_text`` 追加（那条注释自证是 v1.3.22 的对外记号）
    ⇒ 两截接起来必须与 TS 一个形状。"""
    msg = _msg(msg_type=MT_VOICE, media={"stt_text": "今天下午三点开会"})
    A._inject_media_enrich_line(msg, A.KIND_VOICES, "https://oss/v.silk")
    assert _pipeline().prepare_text(msg) == "[语音] https://oss/v.silk\n[转写] 今天下午三点开会"


def test_prepare_text_does_not_duplicate_an_enriched_transcript(A):
    """适配器已把 ``[转写]`` 并进 ``msg.content``（落库/心流要能看到），
    ``prepare_text`` 再补时必须去重，否则正文里出现两行转写。"""
    msg = _msg(msg_type=MT_VOICE,
               content="[语音] https://oss/v.silk\n[转写] 今天下午三点开会",
               media={"oss_url": "https://oss/v.silk",
                      "stt_text": "今天下午三点开会"})
    text = _pipeline().prepare_text(msg)
    assert text.count("[转写]") == 1, text
    assert text.endswith("[转写] 今天下午三点开会")


def test_the_enriched_file_line_does_not_get_a_file_prefix(A):
    msg = _msg(msg_type=MT_APP)
    A._inject_media_enrich_line(msg, A.KIND_FILES, "https://oss/f.xlsx",
                                filename="报价单.xlsx", size=1234)
    text = _pipeline().prepare_text(msg)
    assert text == "[文件] 报价单.xlsx (1234 bytes) https://oss/f.xlsx", text
    assert text.count("[文件]") == 1, text


def test_without_a_url_the_old_marker_behaviour_is_untouched():
    """★ 无凭据时的**回归判据**（本机不装空壳，parser 自成一体）：没有 ``oss_url``
    ⇒ 与加富化之前**逐字节相同**（前缀标记那条路照旧）。"""
    assert _pipeline().prepare_text(_msg(content="看这个")) == "[图片] 看这个"
    assert _pipeline().prepare_text(
        _msg(msg_type=MT_VOICE, content="收到一条语音",
             media={"stt_text": "今天下午三点开会"})
    ) == "[语音] 收到一条语音\n[转写] 今天下午三点开会"
    assert _pipeline().prepare_text(_msg(msg_type=MT_TEXT, content="你好")) == "你好"


def test_the_suppression_is_keyed_on_oss_url_not_on_the_kind(A):
    """抑制的判据是**那个键**，不是消息类型：只有 ``oss_url`` 才让标记让路
    （``mp_asset_url`` 是小程序封面那个键，与这一层无关）。"""
    msg = _msg(content="看这个", media={"mp_asset_url": "https://oss/cover.jpg"})
    assert _pipeline()._media_tag(msg, "看这个") == "[图片]"
