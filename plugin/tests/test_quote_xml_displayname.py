"""引用回复 XML 的 ``displayname`` —— 微信渲染引用气泡发送者名的唯一来源。

2026-10-20 移动群/华为群实测：``refermsg`` 里只给 ``fromusr``（wxid）时，
**微信客户端就把 wxid 当显示名**，引用气泡上写 ``@wxid_xxx``。看日志/DB 都
挑不出毛病（``fromusr`` 是对的），但群里看到的就是"没识别出被回复人"。

钉三件事：

1. 给了 ``displayname`` 就必须进 XML；
2. 不给就**不新增这个标签**（向后兼容：Hermes 的通用 ``send_message`` 路径拿不到昵称）；
3. XML 特殊字符要转义 —— 昵称里有 ``&`` / ``<`` 不能把 XML 写坏。
"""

from __future__ import annotations

from plugin.platform import outbound


def test_displayname_is_emitted():
    xml = outbound.build_quote_reply_xml(
        "收到", "123", fromusr="wxid_abc", displayname="何必")
    assert "<displayname>何必</displayname>" in xml
    assert "<fromusr>wxid_abc</fromusr>" in xml


def test_displayname_omitted_when_empty():
    """拿不到昵称时不写空标签 —— 让微信退回它自己的兜底，别塞个空字符串。"""
    xml = outbound.build_quote_reply_xml("收到", "123", fromusr="wxid_abc")
    assert "displayname" not in xml


def test_displayname_is_xml_escaped():
    xml = outbound.build_quote_reply_xml(
        "收到", "123", fromusr="wxid_abc", displayname="A&B <店>")
    assert "&amp;" in xml and "&lt;" in xml
    assert "A&B <店>" not in xml


def test_no_refermsg_without_svrid():
    """没有可引用的目标 ⇒ 整段 refermsg 都不带（带了 vendor 会拒）。"""
    xml = outbound.build_quote_reply_xml(
        "收到", "", fromusr="wxid_abc", displayname="何必")
    assert "refermsg" not in xml
