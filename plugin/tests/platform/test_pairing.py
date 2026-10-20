"""配对码回归（``platform/pairing.py``）。

2026-10-20 接通 ``/pair`` 兑换链路后，这一段从死码变成真实入口：
命令识别、码校验、已配对名单的持久化都必须守。测试全程用 ``tmp_path``，
不碰 ``plugin/pairing/`` 下的生产名单。
"""

from __future__ import annotations

import json
import stat

import pytest

from plugin.platform import pairing as P


@pytest.fixture(autouse=True)
def _isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PAIRING_DIR", tmp_path / "pairing")
    return tmp_path / "pairing"


# ------------------------------------------------------------------ 命令识别

def test_pair_command_is_exact():
    assert P.is_pair_command("/pair")
    assert P.is_pair_command("/PAIR ABCD2345")
    assert P.is_pair_command("  /pair   abcd2345  ")


def test_pair_command_does_not_swallow_similar_commands():
    # /pairs 是另一条命令；前缀匹配会把它误判成 /pair。
    assert not P.is_pair_command("/pairs")
    assert not P.is_pair_command("/pairing")
    assert not P.is_pair_command("pair ABCD2345")
    assert not P.is_pair_command("")


def test_extract_pair_code_normalizes_and_handles_missing():
    assert P.extract_pair_code("/pair abcd2345") == "ABCD2345"
    assert P.extract_pair_code("/pair") is None
    assert P.extract_pair_code("/pairs ABCD2345") is None


# ------------------------------------------------------------------ 码校验

def test_redeem_valid_code(monkeypatch):
    monkeypatch.setattr(P.time, "time", lambda: 1_000.0)
    entry = P.generate_pairing_code("acct")
    ok, why = P.redeem_pairing_code("acct", entry["code"].lower())
    assert ok and why == "配对码有效"


def test_redeem_rejects_wrong_missing_and_expired(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(P.time, "time", lambda: now[0])

    ok, why = P.redeem_pairing_code("acct", "ABCD2345")
    assert not ok and "无有效配对码" in why

    entry = P.generate_pairing_code("acct")
    code = entry["code"]
    wrong = "AAAA2345" if code != "AAAA2345" else "BBBB2345"
    ok, why = P.redeem_pairing_code("acct", wrong)
    assert not ok and why == "配对码错误"

    now[0] += P.PAIRING_CODE_TTL_MS / 1000 + 1
    ok, why = P.redeem_pairing_code("acct", code)
    assert not ok and "无有效配对码" in why


def test_code_is_account_scoped(monkeypatch):
    monkeypatch.setattr(P.time, "time", lambda: 1_000.0)
    entry = P.generate_pairing_code("acct-a")
    ok, _ = P.redeem_pairing_code("acct-b", entry["code"])
    assert not ok


# ------------------------------------------------------------ 已配对名单

def test_add_paired_is_idempotent_and_persistent(_isolated_dir):
    assert P.load_paired("acct") == []
    assert P.add_paired("acct", "wxid_new") is True
    assert P.add_paired("acct", "wxid_new") is False
    assert P.add_paired("acct", "  ") is False
    assert P.load_paired("acct") == ["wxid_new"]
    assert P.is_paired("acct", "wxid_new")
    assert not P.is_paired("acct", "wxid_other")

    path = P.get_paired_path("acct")
    assert path == _isolated_dir / "acct.paired.json"
    assert json.loads(path.read_text("utf-8")) == {"users": ["wxid_new"]}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_paired_tolerates_missing_and_bad_files(_isolated_dir):
    assert P.load_paired("missing") == []

    _isolated_dir.mkdir(parents=True, exist_ok=True)
    path = P.get_paired_path("bad")
    path.write_text("{not json", "utf-8")
    assert P.load_paired("bad") == []
    path.write_text('{"users": "wxid_not_a_list"}', "utf-8")
    assert P.load_paired("bad") == []
    path.write_text('{"users": ["wxid_ok", "", 3, null]}', "utf-8")
    assert P.load_paired("bad") == ["wxid_ok"]
