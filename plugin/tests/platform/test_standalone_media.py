"""``_standalone_send``（cron / send_message 进程外投递）的**附件**投递。

2026-10-19：签名一直收 ``media_files`` / ``force_document``，实现却**完全没消费** ——
模型生成的图片/文件被静默丢弃，调用方还拿到 ``success``。这里按行为钉住：

* 附件按扩展名走 image / voice / video / file 四条链路（与 live ``_send_media`` 同款）；
* ``force_document`` 把图片强制按文件发；
* 文本为空、只有附件时照发（且**不发**空文本气泡）；
* 纯空输入不再回 ``success``（否则调用方记成"已投递"，实际一条都没发）。
"""

from __future__ import annotations

import types

import pytest


class _FakeClient:
    instances: list["_FakeClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls: list[tuple] = []
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _ok(self, kind, **fields):
        self.calls.append((kind, fields))
        return {"Code": 0, "Data": {"NewMsgId": f"id-{len(self.calls)}"}}

    async def send_text(self, to_wxid, content):
        return self._ok("text", to_wxid=to_wxid, content=content)

    async def send_image(self, to_wxid, image_base64):
        return self._ok("image", to_wxid=to_wxid)

    async def send_file_v2(self, to_wxid, file_name, file_base64):
        return self._ok("file", to_wxid=to_wxid, file_name=file_name)

    async def send_video(self, to_wxid, video_base64, thumb_base64=""):
        return self._ok("video", to_wxid=to_wxid)

    async def send_voice(self, to_wxid, voice_base64, voice_type=4):
        return self._ok("voice", to_wxid=to_wxid)


@pytest.fixture()
def mod(stubbed, monkeypatch):  # noqa: F811 —— 空壳环境来自 conftest
    import plugin.platform.adapter as A
    import plugin.platform.outbound as O

    acct = types.SimpleNamespace(id="acct", self_wxid="wxid_bot", nickname="小助理",
                                 api_base_url="http://127.0.0.1:1", authcode="x",
                                 agent="default")
    monkeypatch.setattr(A.accounts_mod, "load_all", lambda _p=None: {"acct": acct})
    monkeypatch.setattr(A.accounts_mod, "refusal_reason", lambda _a: None)
    monkeypatch.setattr(A, "WppClient", _FakeClient)
    monkeypatch.setattr(A.outbound.netguard, "send_allowlist", lambda **_kw: ())
    monkeypatch.setattr(A.outbound, "assert_outbound_allowed", lambda *a, **k: None)
    monkeypatch.setattr(A.outbound, "resolve_to_base64",
                        lambda ref, **_kw: _async("aGVsbG8="))
    monkeypatch.setattr(A, "_persist_outbound_record", lambda *a, **k: _async(None))
    _FakeClient.instances.clear()
    return A


def _async(value):
    async def _inner(*_a, **_k):
        return value
    return _inner()


def _run(coro):
    import asyncio
    return asyncio.run(coro)


CONFIG = types.SimpleNamespace(extra={})


def test_media_is_actually_delivered(mod) -> None:
    """文本 + 一个 PDF：先发文本，再走文件链路 —— 附件不许再被静默吞掉。"""
    out = _run(mod._standalone_send(CONFIG, "acct:12345@chatroom", "报告见附件",
                                    media_files=[("/tmp/report.pdf", False)]))
    assert out["success"] is True
    assert out["media_sent"] == 1
    assert [c[0] for c in _FakeClient.instances[-1].calls] == ["text", "file"]


def test_image_goes_through_send_image(mod) -> None:
    out = _run(mod._standalone_send(CONFIG, "acct:12345@chatroom", "",
                                    media_files=[("/tmp/pic.png", False)]))
    assert out["success"] is True and out["media_sent"] == 1
    calls = _FakeClient.instances[-1].calls
    assert [c[0] for c in calls] == ["image"], "空文本不许产生空气泡，图片正常发"


def test_force_document_sends_images_as_files(mod) -> None:
    out = _run(mod._standalone_send(CONFIG, "acct:12345@chatroom", "",
                                    media_files=[("/tmp/pic.png", False)],
                                    force_document=True))
    assert out["success"] is True
    assert [c[0] for c in _FakeClient.instances[-1].calls] == ["file"]


def test_video_uses_the_video_endpoint(mod, monkeypatch) -> None:
    monkeypatch.setattr(mod.WppAdapter, "_video_thumb",
                        staticmethod(lambda _ref: _async("dGh1bWI=")))
    out = _run(mod._standalone_send(CONFIG, "acct:12345@chatroom", "",
                                    media_files=[("/tmp/clip.mp4", False)]))
    assert out["success"] is True
    assert [c[0] for c in _FakeClient.instances[-1].calls] == ["video"]


def test_empty_input_is_an_error_not_a_silent_success(mod) -> None:
    out = _run(mod._standalone_send(CONFIG, "acct:12345@chatroom", "   "))
    assert "error" in out and "success" not in out, \
        "什么都没发出去时回 success 会让调用方记一笔假账"
