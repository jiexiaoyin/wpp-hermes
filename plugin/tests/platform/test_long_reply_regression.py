"""超长回复被截断（2026-10-18 群里实测）—— 两道独立的闸都要有。

老板原话：「超长回复应该分段」「天高地迥，觉宇宙之无穷；兴尽悲来，识盈 后面就没了」。

根因**不是**模型截断，也不是普通文本上限：那条 977 字回复走的是**引用回复**
（``/Msg/ShareLink`` + type=57 XML），而微信客户端/vendor 只渲染引用气泡前
**500** 字符 —— 发送接口照样回成功。普通 ``/Msg/SendTxt`` 的上限则约 2000 字符，
超过同样会被静默截断。

本文件钉三件事：

1. ``quote_reply_fits`` 的边界（500 含 / 501 不含）；
2. 引用正文超限时 ``_send_chunk`` **改走** ``send_text``，绝不再生成会被 slice 的 XML；
3. ``chunk_text`` 在 2000 字阈值上确实分段，且分段可无损拼回。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from plugin.inbound import quote_ref
from plugin.platform import outbound as O

ROOT_GROUP = "57737516566@chatroom"
SENDER = "wxid_tsyoek04xgd822"
NICK = "戴永刚"
SVRID = "324472740118864204"


# ============================================================ 1. 阈值函数

@pytest.mark.parametrize("text,want", [
    ("", True),
    ("字" * 500, True),
    ("字" * 499 + "  ", True),      # strip 后 499
    ("字" * 501, False),
    ("@戴永刚 " + "字" * 495, True),   # 前缀 5 字也计入 ⇒ 500
    ("@戴永刚 " + "字" * 496, False),  # 501
    (None, True),                    # 防御：None 当空
])
def test_quote_reply_fits_boundary(text, want):
    assert O.quote_reply_fits(text) is want


def test_the_text_chunk_limit_is_2000():
    """4000 是旧值（微信个人号会被静默截断）；上游 weixin 也是 2000。"""
    assert O.MAX_MESSAGE_LENGTH == 2000
    assert O._TEXT_CHUNK_LIMIT == 2000


def test_chunk_text_splits_and_reassembles_losslessly():
    content = "甲" * 1500 + "\n\n" + "乙" * 1500 + "\n\n" + "丙" * 100
    chunks = O.chunk_text(content)
    assert len(chunks) == 2, [len(c) for c in chunks]
    assert all(len(c) <= 2000 for c in chunks)
    assert "\n\n".join(chunks) == content, "分段不许丢字/改字"


def test_a_long_paragraph_breaks_at_sentence_boundaries():
    """老板 2026-10-18：「切分长文本应该自然一些，不要生硬切断」。

    一整段中文没有空行时，旧实现严格每 2000 字切一刀，句子会断在「识盈」这种
    地方；现在应优先落在句号/问号/感叹号/分号之后。
    """
    sentences = [f"这是第{i:03d}句用于验证自然分段的测试文本。" * 2 for i in range(80)]
    content = "".join(sentences)
    assert len(content) > 2000

    chunks = O.chunk_text(content)
    assert len(chunks) >= 2
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == content, "自然分段同样不许丢字"
    assert all(c.endswith(("。", "！", "？", "；")) for c in chunks[:-1]), \
        [c[-12:] for c in chunks]


def test_without_any_punctuation_it_still_falls_back_to_a_hard_cut():
    """没有任何可断之处时不能无限找 —— 按上限硬切，保证每条都发得出去。"""
    chunks = O.chunk_text("字" * 4500)
    assert [len(c) for c in chunks] == [2000, 2000, 500]


# ============================================================ 2. 适配器真跑
#
# adapter.py 模块级 import Hermes 的 gateway.*，本机只能靠 conftest 的空壳；
# 装上空壳后 ``_send_chunk`` 可以真跑。这里造壳，只替换身份/取昵称/落库三处。

ACCT = types.SimpleNamespace(id="acct-42", nickname="益融小助理", self_wxid="wxid_bot")
RESP = {"Code": 0, "Data": {"List": [{"NewMsgId": "9001", "ClientMsgid": "9001"}]}}


class _Client:
    def __init__(self) -> None:
        self.share_links: list[tuple[str, dict]] = []
        self.texts: list[tuple[str, str, object]] = []

    async def call(self, endpoint: str, body: dict):
        self.share_links.append((endpoint, body))
        return RESP

    async def send_text(self, to_wxid: str, content: str, ats=None):
        self.texts.append((to_wxid, content, ats))
        return RESP


@pytest.fixture(scope="module")
def adapter(stubbed):  # noqa: F811 —— 空壳环境来自 conftest（模块级）
    import plugin.platform.adapter as A
    return A


class _SendResult:
    """``gateway.platforms.base.SendResult`` 在本机是空壳 ⇒ 换成一个能收参的等价物。"""

    def __init__(self, success, message_id=None, error=None,
                 error_kind=None, raw_response=None):
        self.success = success
        self.message_id = message_id
        self.error = error
        self.error_kind = error_kind
        self.raw_response = raw_response


def _shell(A, monkeypatch):
    """造一个只够跑 ``_send_chunk`` 的适配器实例。"""
    async def _prefix(acct, client, peer_id, fromusr, metadata, *, db_name=""):
        return f"@{NICK} "

    async def _persist(*_a, **_kw):
        return None

    monkeypatch.setattr(A, "SendResult", _SendResult)
    monkeypatch.setattr(
        A.WppAdapter, "_quote_identity",
        staticmethod(lambda acct, svrid: quote_ref.QuoteIdentity(
            fromusr=SENDER, display_name=NICK, matched=True)))
    monkeypatch.setattr(A.WppAdapter, "_quote_prefix", staticmethod(_prefix))
    monkeypatch.setattr(A.WppAdapter, "_persist_outbound", staticmethod(_persist))
    return A.WppAdapter.__new__(A.WppAdapter)


def test_a_short_quote_still_uses_the_quote_bubble(adapter, monkeypatch):
    ad = _shell(adapter, monkeypatch)
    client = _Client()
    result = asyncio.run(ad._send_chunk(
        ACCT, client, ROOT_GROUP, "短回复", reply_to=SVRID, metadata=None))

    assert result.success is True
    assert [ep for ep, _ in client.share_links] == ["/Msg/ShareLink"]
    assert client.texts == [], "短回复仍应走引用卡片"
    xml = client.share_links[0][1]["Xml"]
    assert f"@{NICK} 短回复" in xml


def test_a_long_quote_falls_back_to_plain_text_instead_of_being_truncated(adapter, monkeypatch):
    """★ 600 字引用回复：旧实现会进 ``build_quote_reply_xml`` 被 ``[:500]`` 砍掉 100 字。

    修复后必须整段走 ``/Msg/SendTxt``：不带引用气泡，但**一个字都不少**。
    """
    ad = _shell(adapter, monkeypatch)
    client = _Client()
    body = "天高地迥，觉宇宙之无穷；兴尽悲来，识盈" + "。" * 570  # 约 600 字

    result = asyncio.run(ad._send_chunk(
        ACCT, client, ROOT_GROUP, body, reply_to=SVRID, metadata=None))

    assert result.success is True
    assert client.share_links == [], "超限不许再走 ShareLink（那里会静默 slice）"
    assert len(client.texts) == 1
    to_wxid, sent, _ats = client.texts[0]
    assert to_wxid == ROOT_GROUP
    assert sent == f"@{NICK} {body}", "前缀 + 正文必须完整发出"
    assert sent.endswith("。" * 570), "后半段不许被吞"


def test_a_full_2000_char_chunk_is_split_again_after_the_prefix(adapter, monkeypatch):
    """前缀会把 2000 字的 chunk 顶到 2005 —— 普通文本同样不能超上限，再切一刀。"""
    ad = _shell(adapter, monkeypatch)
    client = _Client()
    body = "字" * O.MAX_MESSAGE_LENGTH

    result = asyncio.run(ad._send_chunk(
        ACCT, client, ROOT_GROUP, body, reply_to=SVRID, metadata=None))

    assert result.success is True
    assert client.share_links == []
    assert len(client.texts) == 2, [len(t[1]) for t in client.texts]
    assert all(len(t[1]) <= O.MAX_MESSAGE_LENGTH for t in client.texts)
    assert "".join(t[1] for t in client.texts) == f"@{NICK} {body}"


def test_the_boundary_is_inclusive(adapter, monkeypatch):
    """``@戴永刚 `` + 495 字 = 500 ⇒ 仍可引用；496 字 = 501 ⇒ 必须降级。"""
    ad = _shell(adapter, monkeypatch)

    exact = _Client()
    asyncio.run(ad._send_chunk(ACCT, exact, ROOT_GROUP, "字" * 495,
                               reply_to=SVRID, metadata=None))
    assert [ep for ep, _ in exact.share_links] == ["/Msg/ShareLink"]

    over = _Client()
    asyncio.run(ad._send_chunk(ACCT, over, ROOT_GROUP, "字" * 496,
                               reply_to=SVRID, metadata=None))
    assert over.share_links == [] and len(over.texts) == 1
