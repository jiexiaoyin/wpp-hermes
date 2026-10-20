"""配对码文件（``plugin/platform/pairing.py``）—— 权限、原子性、日志卫生。

配对码等于一次性准入凭据：文件必须 0600、目录 0700、**不进日志**；
账号 id 直接进文件名，必须挡住路径穿越。
"""
from __future__ import annotations

import json
import logging
import stat
import time

import pytest

from plugin.platform import pairing


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    d = tmp_path / "pairing"
    monkeypatch.setattr(pairing, "PAIRING_DIR", d)
    yield d


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_generate_writes_private_atomic_files(pdir, caplog):
    caplog.set_level(logging.INFO, logger=pairing.log.name)
    entry = pairing.generate_pairing_code("default")
    path = pdir / "default.json"

    assert path.is_file()
    assert _mode(path) == 0o600
    assert _mode(pdir) == 0o700
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["code"] == entry["code"]
    assert len(entry["code"]) == pairing.PAIRING_CODE_LENGTH
    assert on_disk["expires_at"] > int(time.time() * 1000)
    # 没有临时文件残留
    assert sorted(p.name for p in pdir.iterdir()) == ["default.json"]
    # 日志只记账号/过期时间，不记配对码
    assert entry["code"] not in caplog.text


def test_redeem_checks_code_and_expiry(pdir):
    entry = pairing.generate_pairing_code("default")
    ok, _msg = pairing.redeem_pairing_code("default", entry["code"].lower())
    assert ok is True
    bad, msg = pairing.redeem_pairing_code("default", "WRONG123")
    assert bad is False and "错误" in msg

    path = pdir / "default.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["expires_at"] = int(time.time() * 1000) - 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert pairing.read_pairing_code("default") is None
    expired, msg = pairing.redeem_pairing_code("default", entry["code"])
    assert expired is False and "无有效配对码" in msg


def test_add_and_remove_paired_are_idempotent(pdir):
    assert pairing.add_paired("default", "wxid_a") is True
    assert pairing.add_paired("default", "wxid_a") is False
    assert pairing.add_paired("default", "  ") is False
    assert pairing.load_paired("default") == ["wxid_a"]

    path = pdir / "default.paired.json"
    assert _mode(path) == 0o600

    assert pairing.remove_paired("default", "wxid_a") is True
    assert pairing.remove_paired("default", "wxid_a") is False
    assert pairing.load_paired("default") == []


def test_remove_missing_file_does_not_create_it(pdir):
    assert pairing.remove_paired("absent", "wxid_a") is False
    assert not (pdir / "absent.paired.json").exists()


def test_account_id_path_traversal_rejected(pdir):
    for bad in ("../evil", "a/b", "a\\b", "..", ""):
        with pytest.raises(ValueError):
            pairing.get_pairing_path(bad)
        with pytest.raises(ValueError):
            pairing.get_paired_path(bad)
        with pytest.raises(ValueError):
            pairing.remove_paired(bad, "wxid_a")


def test_corrupt_paired_file_reads_as_empty(pdir):
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "default.paired.json").write_text("{not json", encoding="utf-8")
    assert pairing.load_paired("default") == []
