"""Web 控制台纯逻辑 + 跨进程写锁（``plugin/core/console_admin.py``）。

判据都下在**产出物**上：安全视图 JSON 里不出现凭据键、写回后的文件内容、
快照字段。控制台最坏的失败形态是"页面显示保存成功，实际写坏/写了凭据"，
所以这里对 ``account_store.update_account`` 也做一次真实落盘验证。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin.core import account_store as S
from plugin.core import console_admin as C
from plugin.core import overrides as OV
from plugin.data import repo_console

SECRETS = {
    "authcode": "SECRET-AUTHCODE",
    "tokenKey": "SECRET-TOKEN",
    "webhookSecret": "SECRET-HOOK",
    "webhookPathToken": "SECRET-PATH",
}


def _raw(**over):
    cfg = {
        "agent": "wpp-wechat-test",
        "enabled": True,
        "nickname": "测试助手",
        "selfWxid": "wxid_bot",
        "dmPolicy": "allowlist",
        "allowFrom": ["wxid_a", "wxid_b"],
        "blacklistUsers": [],
        "groupPolicy": "allowlist",
        "groupAllowFrom": ["g1@chatroom"],
        "blacklistGroups": [],
        "adminUsers": ["wxid_a"],
        "employeeGroupIds": ["g1@chatroom"],
        "employeeAllowFrom": ["wxid_b"],
        "debounceMs": 800,
        "heartflow": {"enabled": False, "model": "m1", "replyThreshold": 0.6},
        "affection": {"enabled": True},
        "jargon": {"enabled": True},
        "style": {"enabled": False},
        "tts": {"enabled": False},
        "proactive": {"enabled": False},
        "xiaoweiEnabled": True,
        "joinWelcome": {"enabled": True, "mode": "allowlist",
                        "groups": ["g1@chatroom"], "template": "欢迎"},
        "unknownKey": {"keep": [1, 2, 3]},
        **SECRETS,
    }
    cfg.update(over)
    return cfg


@pytest.fixture
def adir(tmp_path, monkeypatch):
    d = tmp_path / "accounts"
    d.mkdir()
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_DIR", str(d))
    OV._runtime_overrides.clear()
    yield d
    OV._runtime_overrides.clear()


@pytest.fixture
def bdir(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    monkeypatch.setenv(S.BACKUP_DIR_ENV, str(d))
    return d


# ------------------------------------------------------------------ 安全视图

def test_safe_view_never_contains_credentials():
    view = C.safe_account_view("default", _raw(), join_welcome_effective={"enabled": True})
    blob = json.dumps(view, ensure_ascii=False)
    for key in SECRETS:
        assert key not in blob
        assert SECRETS[key] not in blob
    assert view["id"] == "default"
    assert view["features"]["heartflow"] is False
    assert view["features"]["xiaowei"] is True
    assert view["heartflowModelConfigured"] is True


def test_safe_view_keeps_raw_and_effective_join_welcome_separate():
    view = C.safe_account_view(
        "default", _raw(), join_welcome_effective={"template": "欢迎 {name}"})
    assert view["joinWelcome"]["raw"]["template"] == "欢迎"
    assert view["joinWelcome"]["effective"]["template"] == "欢迎 {name}"


def test_safe_view_warnings_flag_allow_all_users_bypass():
    view = C.safe_account_view("default", _raw(allowAllUsers=True, dmPolicy="allowlist"))
    assert any("allowAllUsers" in w for w in view["warnings"])


# ------------------------------------------------------------------ 策略

def test_policy_rejects_unknown_enum_and_unknown_field():
    with pytest.raises(C.ConsoleValidationError):
        C.validate_policy({"dmPolicy": "everyone"})
    with pytest.raises(C.ConsoleValidationError):
        C.validate_policy({"botPolicy": "open"})


def test_policy_normalizes_empty_dm_policy_to_open():
    assert C.validate_policy({"dmPolicy": ""}) == {"dmPolicy": "open"}


def test_policy_list_parsing_dedupes_and_validates_kind():
    out = C.validate_policy({"allowFrom": "wxid_a, wxid_b\nwxid_a\n"})
    assert out["allowFrom"] == ["wxid_a", "wxid_b"]
    with pytest.raises(C.ConsoleValidationError):
        C.validate_policy({"allowFrom": ["g1@chatroom"]})
    with pytest.raises(C.ConsoleValidationError):
        C.validate_policy({"groupAllowFrom": ["wxid_a"]})


def test_policy_list_limits():
    too_many = [f"wxid_{i}" for i in range(C.MAX_LIST_ITEMS + 1)]
    with pytest.raises(C.ConsoleValidationError):
        C.validate_policy({"allowFrom": too_many})
    with pytest.raises(C.ConsoleValidationError):
        C.validate_policy({"allowFrom": ["x" * (C.MAX_ITEM_LEN + 1)]})


# ------------------------------------------------------------------ 进群欢迎

def test_join_welcome_quiet_hours_must_be_int_array():
    assert C.validate_join_welcome({"quietHours": [23, 0, 7, 7]})["quietHours"] == [0, 7, 23]
    with pytest.raises(C.ConsoleValidationError):
        C.validate_join_welcome({"quietHours": "23-7"})
    with pytest.raises(C.ConsoleValidationError):
        C.validate_join_welcome({"quietHours": [24]})


def test_join_welcome_max_batch_and_template():
    assert C.validate_join_welcome({"maxBatch": 1})["maxBatch"] == 1
    assert C.validate_join_welcome({"maxBatch": "20"})["maxBatch"] == 20
    with pytest.raises(C.ConsoleValidationError):
        C.validate_join_welcome({"maxBatch": 0})
    with pytest.raises(C.ConsoleValidationError):
        C.validate_join_welcome({"maxBatch": 21})
    with pytest.raises(C.ConsoleValidationError):
        C.validate_join_welcome({"template": "  "})
    with pytest.raises(C.ConsoleValidationError):
        C.validate_join_welcome({"template": "x" * (C.MAX_TEMPLATE_LEN + 1)})


def test_join_welcome_merge_keeps_unknown_sibling_keys():
    merged = C.merge_join_welcome(
        {"enabled": True, "unknownSibling": 1}, {"template": "hi"})
    assert merged == {"enabled": True, "unknownSibling": 1, "template": "hi"}
    with pytest.raises(C.ConsoleValidationError):
        C.merge_join_welcome(["not", "a", "dict"], {"template": "hi"})


# ------------------------------------------------------------------ 能力

def test_feature_merge_keeps_section_keys_and_rejects_non_dict():
    merged = C.merge_feature_section({"enabled": False, "replyThreshold": 0.6}, True)
    assert merged == {"enabled": True, "replyThreshold": 0.6}
    with pytest.raises(C.ConsoleValidationError):
        C.merge_feature_section("bad", True)


def test_features_require_bool_and_known_keys():
    assert C.validate_features({"heartflow": True, "xiaoweiEnabled": False}) == {
        "heartflow": True, "xiaoweiEnabled": False}
    with pytest.raises(C.ConsoleValidationError):
        C.validate_features({"heartflow": 1})
    with pytest.raises(C.ConsoleValidationError):
        C.validate_features({"nope": True})


# ------------------------------------------------------------------ 状态

def test_status_classify_thresholds():
    now = 1_000_000
    assert C.status_classify(now - 5, now)["state"] == "live"
    assert C.status_classify(now - 30, now)["state"] == "delayed"
    assert C.status_classify(now - 61, now)["state"] == "stale"
    assert C.status_classify(None, now)["state"] == "unknown"
    assert C.status_classify(now, now, missing=True)["state"] == "unknown"


def test_snapshot_has_no_credentials_or_policy_values():
    snap = C.build_status_snapshot(
        profile="wpp-wechat", pid=123, started_at=1, connected=True, reason="running",
        accounts={"default": {
            "ws_connected": True, "ws_connected_once": True, "client_ready": True,
            "config_mtime_ns": 42, "last_reload_at": 9, "last_reload_ok": True,
            "last_reload_error": None, "authcode": SECRETS["authcode"],
            "allowFrom": ["wxid_a"],
        }}, now=100)
    assert snap["schema"] == 1
    assert snap["accounts"]["default"]["ws_connected"] is True
    blob = json.dumps(snap, ensure_ascii=False)
    assert SECRETS["authcode"] not in blob
    assert "allowFrom" not in blob
    assert "wxid_a" not in blob


# ------------------------------------------------------- 真实落盘（跨进程锁）

def test_update_account_reads_fresh_disk_state(adir, bdir):
    path = adir / "default.json"
    path.write_text(json.dumps(_raw(), ensure_ascii=False), encoding="utf-8")

    # 模拟"另一个写入者"刚改完 allowFrom：mutator 必须看到这次改动。
    S.write_account_key("default", "allowFrom", ["wxid_new"])

    seen = {}

    def _mutate(data):
        seen.update(data)
        data["groupAllowFrom"] = ["g2@chatroom"]
        return data

    updated, mtime_ns = S.update_account("default", _mutate)
    assert seen["allowFrom"] == ["wxid_new"]
    assert updated["groupAllowFrom"] == ["g2@chatroom"]
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["groupAllowFrom"] == ["g2@chatroom"]
    assert on_disk["allowFrom"] == ["wxid_new"]
    assert on_disk["unknownKey"] == {"keep": [1, 2, 3]}
    assert on_disk["authcode"] == SECRETS["authcode"]
    assert mtime_ns == path.stat().st_mtime_ns
    # 锁文件在 accounts/.locks 下（不污染账号文件本身）
    assert (adir / ".locks" / "default.lock").is_file()


def test_update_account_rejects_credential_change(adir, bdir):
    path = adir / "default.json"
    path.write_text(json.dumps(_raw(), ensure_ascii=False), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    def _mutate(data):
        data["authcode"] = "hacked"
        return data

    with pytest.raises(S.AccountWriteError):
        S.update_account("default", _mutate)
    assert path.read_text(encoding="utf-8") == before


def test_update_account_skip_if_unchanged_writes_no_backup(adir, bdir):
    path = adir / "default.json"
    path.write_text(json.dumps(_raw(), ensure_ascii=False), encoding="utf-8")
    S.update_account("default", lambda d: d, skip_if_unchanged=True)
    assert not bdir.exists() or not list(bdir.iterdir())


# ------------------------------------------------------------- 群统计总量

class _FakeConn:
    def __init__(self, rooms, members, share):
        self._rooms = rooms
        self._members = members
        self._share = share
        self.queries = []

    def query(self, sql, params=()):
        self.queries.append(sql)
        if "wpp_chatrooms" in sql:
            return self._rooms
        if "wpp_chatroom_members" in sql:
            return self._members
        return []


def test_group_activity_totals_count_all_groups(monkeypatch):
    repo_console.clear_cache()
    conn = _FakeConn(
        rooms=[{"chatroom_id": "g1@chatroom", "nickname": "群一", "remark": None,
                "member_count": 10, "last_synced_at": None}],
        members=[{"chatroom_id": "g1@chatroom", "n": 9}],
        share=[{"gid": "g1@chatroom", "direction": "inbound", "n": 3},
               {"gid": "g1@chatroom", "direction": "outbound", "n": 2},
               {"gid": "g2@chatroom", "direction": "inbound", "n": 5}],
    )
    monkeypatch.setattr(repo_console.db, "get_conn", lambda: conn)
    monkeypatch.setattr(repo_console.db, "close", lambda: None)
    monkeypatch.setattr(repo_console.repo_messages, "list_bot_msg_share",
                        lambda aid, since, conn=None: conn._share)

    res = repo_console.list_group_activity("default", since_hours=24, limit=10)
    assert res["totals"] == {"inbound": 8, "outbound": 2}
    assert res["truncated"] is False
    # 只有消息、没有群档案的 g2 也要出现（否则活跃群在控制台"消失"）
    g2 = [g for g in res["groups"] if g["chatroom_id"] == "g2@chatroom"]
    assert g2 and g2[0]["inbound"] == 5 and g2[0]["name"] == ""
