"""``toolkit/toolout.py`` 的可解析截断（2026-10-18 修）。

旧写法 ``json.dumps(...)[:4000]`` 在超限时把 JSON **从中间切断** —— 实测
``wpp_group_list`` 的返回以 ``"access_type":`` 结尾，模型拿到的是一段语法非法的
半截文本：既 ``json.loads`` 不了，也看不出"被截断了"。本文件锁住三件事：

1. 超限时返回**合法 JSON**（``json.loads`` 必成功）；
2. 长度 ≤ cap（中文/引号膨胀的情况下也成立）；
3. 不超限时与旧行为**逐字节一致**（避免影响既有调用方与断言）。
"""

from __future__ import annotations

import json

from plugin.toolkit.toolout import DEFAULT_CAP, dump_capped, dump_error


def test_small_payload_is_byte_identical_to_plain_dumps() -> None:
    payload = {"ok": True, "code": 0, "data": {"a": 1, "中文": ["x", "y"]}}
    assert dump_capped(payload) == json.dumps(payload, ensure_ascii=False, default=str)


def test_large_payload_is_valid_json_and_capped() -> None:
    payload = {"ok": True, "code": 0,
               "data": {"GroupList": [f"{i}@chatroom" for i in range(500)]}}
    out = dump_capped(payload)
    assert len(out) <= DEFAULT_CAP
    parsed = json.loads(out)  # 关键：必须可解析
    assert parsed["truncated"] is True
    assert parsed["ok"] is True
    assert parsed["full_chars"] > DEFAULT_CAP
    assert parsed["head"].startswith('{"ok": true')


def test_cap_is_honoured_with_escaping_heavy_content() -> None:
    """内容里全是引号/反斜杠时，head 会转义膨胀 —— 仍然不许超过 cap。"""
    payload = {"ok": True, "data": ['a"b\\c' * 2000]}
    out = dump_capped(payload, cap=1200)
    assert len(out) <= 1200
    assert json.loads(out)["truncated"] is True


def test_pathological_tiny_cap_still_returns_valid_json() -> None:
    """``cap`` 比信封本体还短：两个不变量无法两全，合法 JSON 优先（不许切半截）。"""
    out = dump_capped({"ok": True, "data": ["x" * 500]}, cap=10)
    parsed = json.loads(out)
    assert parsed["truncated"] is True
    assert parsed["head"] == ""


def test_empty_and_none_payloads() -> None:
    assert json.loads(dump_capped(None)) is None
    assert json.loads(dump_capped({})) == {}


def test_dump_error_is_machine_readable() -> None:
    parsed = json.loads(dump_error("账号未连接", account="live-test"))
    assert parsed == {"ok": False, "error": "账号未连接", "account": "live-test"}


def test_datetime_values_are_serialised() -> None:
    """pymysql 的 TIMESTAMP 列是 ``datetime`` —— 没有 ``default=str`` 时整条工具会抛
    "Object of type datetime is not JSON serializable"（库一有数据就挂，空库看不出来）。"""
    from datetime import datetime

    out = dump_capped({"ok": True, "ts": datetime(2026, 10, 18, 11, 30)})
    assert json.loads(out)["ts"].startswith("2026-10-18 11:30")
