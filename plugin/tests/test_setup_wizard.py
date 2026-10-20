"""安装向导回归：写到哪、写了什么、权限、显式 ``--home`` 不被重解析。

向导是外部用户见到的第一条路径；它写错目录会让插件永远读不到账号
（``accounts_dir()`` 在插件目录下），而日志看起来一切正常。
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from plugin import setup_wizard as W


def _env(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_non_interactive_writes_only_inside_the_target_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    _env(monkeypatch,
         WECHATPRO_API_BASE="https://vendor.example",
         WECHATPRO_AUTHCODE="auth-fake",
         WECHATPRO_TOKEN_KEY="token-fake")
    monkeypatch.delenv("WECHATPRO_ACCOUNTS_DIR", raising=False)
    monkeypatch.delenv("WECHATPRO_WS_URL", raising=False)

    assert W.main(["--home", str(home), "--non-interactive", "--skip-verify"]) == 0

    env_file = home / ".env"
    account = home / "plugins" / W.PLUGIN_NAME / "accounts" / "default.json"
    assert env_file.is_file() and account.is_file()
    assert _mode(env_file) == 0o600 and _mode(account) == 0o600

    # 账号文件不落明文密钥，只写 *Env 间接引用
    text = account.read_text(encoding="utf-8")
    assert "auth-fake" not in text and "token-fake" not in text
    data = json.loads(text)
    assert data["authcodeEnv"] == W.AUTHCODE_KEY
    assert data["tokenKeyEnv"] == W.TOKEN_KEY_KEY

    # 关键：账号文件在目标 home 的安装布局下，而不是当前代码所在目录
    assert account.parent == home / "plugins" / W.PLUGIN_NAME / "accounts"


def test_explicit_home_wins_over_profile_name(tmp_path, monkeypatch):
    """``--home /tmp/x --profile default`` 必须写 /tmp/x，不能跳回 ~/.hermes。"""
    monkeypatch.delenv("WECHATPRO_ACCOUNTS_DIR", raising=False)
    home = tmp_path / "explicit"
    resolved, profile = W.resolve_home(str(home), "default")
    assert resolved == home.resolve()
    assert profile == "default"

    status = W.check_status(str(home), "default", _resolved=True)
    assert status["home"] == str(home.resolve())
    assert status["account_file"].startswith(str(home.resolve()))


def test_force_still_keeps_a_backup(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    _env(monkeypatch,
         WECHATPRO_API_BASE="https://vendor.example",
         WECHATPRO_AUTHCODE="auth-fake",
         WECHATPRO_TOKEN_KEY="token-fake")
    monkeypatch.delenv("WECHATPRO_ACCOUNTS_DIR", raising=False)

    assert W.main(["--home", str(home), "--non-interactive", "--skip-verify"]) == 0
    assert W.main(["--home", str(home), "--non-interactive", "--skip-verify", "--force"]) == 0

    account = home / "plugins" / W.PLUGIN_NAME / "accounts" / "default.json"
    backups = list(account.parent.glob("default.json.bak-*"))
    assert backups, "覆盖账号文件必须先备份（--force 只跳过确认，不跳过备份）"
    assert all(_mode(p) == 0o600 for p in backups)


def test_check_reports_missing_without_leaking_values(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    for k in ("WECHATPRO_API_BASE", "WECHATPRO_AUTHCODE", "WECHATPRO_TOKEN_KEY",
              "WECHATPRO_WS_URL", "WECHATPRO_ACCOUNTS_DIR"):
        monkeypatch.delenv(k, raising=False)

    status = W.check_status(str(home), None, _resolved=True)
    assert status["ready"] is False
    assert set(status["missing"]) == {W.API_BASE_KEY, W.AUTHCODE_KEY, W.TOKEN_KEY_KEY}
    dumped = json.dumps(status, ensure_ascii=False)
    assert "fake" not in dumped, "状态输出不得包含任何密钥值"


def test_wizard_rejects_non_http_api_base():
    with pytest.raises(ValueError):
        W._normalize_api_base("ftp://vendor.example")


def test_ws_url_is_derived_without_a_vendor_default():
    assert W.derive_ws_url("https://vendor.example") == "wss://vendor.example/ws/sync"
    assert W.derive_ws_url("http://127.0.0.1:8080") == "ws://127.0.0.1:8080/ws/sync"
