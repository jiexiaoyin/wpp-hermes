"""``/style`` 运维命令（2026-10-19）。

失效形态是"回得头头是道但数字/版本是编的"：状态页把读失败说成 0 张、
show 把坏 JSON 渲染成空卡、rollback 说成功但版本号没增。判据一律下在
**回执里的数字等于桩的返回值**上，而不是"文案里有'风格卡'三个字"。
"""

from __future__ import annotations

import json

from plugin.platform import commands

ACC = "acct"
GID = "123@chatroom"


def test_help_lists_style_commands() -> None:
    text = commands.handle_command(ACC, "/help")
    assert "/style on|off|status" in text
    assert "/style list" in text and "/style show" in text and "/style rollback" in text


def test_status_reports_real_card_count(monkeypatch) -> None:
    from plugin.data import repo_style

    monkeypatch.setattr(commands, "_feature_state", lambda *a, **k: True)
    monkeypatch.setattr(repo_style, "count_cards", lambda account_id: 7)
    text = commands.handle_command(ACC, "/style status")
    assert text.startswith("/style 状态: on")
    assert "数据: 风格卡 7 张" in text


def test_status_survives_a_broken_repo(monkeypatch) -> None:
    """状态页在库坏掉时最有用 ⇒ 读失败只降一句人话，绝不抛。"""
    from plugin.data import repo_style

    monkeypatch.setattr(commands, "_feature_state", lambda *a, **k: False)

    def _boom(*a, **k):
        raise RuntimeError("DB down")
    monkeypatch.setattr(repo_style, "count_cards", _boom)
    text = commands.handle_command(ACC, "/style status")
    assert text.startswith("/style 状态: off")
    assert "读取失败，见日志" in text


def test_list_all_cards(monkeypatch) -> None:
    from plugin.data import repo_style

    monkeypatch.setattr(repo_style, "list_cards", lambda account_id: [{
        "group_id": GID, "version": 4, "confidence": 0.83,
        "sample_msgs": 120, "sender_count": 6, "generated_at": 1700000000,
    }])
    text = commands.handle_command(ACC, "/style list")
    assert "风格卡 (1)" in text
    assert f"- {GID}: v4 conf=0.83 samples=120 senders=6" in text


def test_list_versions_for_one_group(monkeypatch) -> None:
    from plugin.data import repo_style

    monkeypatch.setattr(repo_style, "list_versions", lambda account_id, gid, limit=10: [
        {"version": 3, "reason": "rollback", "confidence": 0.7,
         "sample_msgs": 50, "generated_at": 1700000000},
        {"version": 2, "reason": "generated", "confidence": 0.9,
         "sample_msgs": 100, "generated_at": 1690000000},
    ])
    text = commands.handle_command(ACC, f"/style list {GID}")
    assert f"风格卡版本 ({GID})" in text
    assert "- v3 rollback conf=0.70 samples=50" in text
    assert "- v2 generated conf=0.90 samples=100" in text


def test_list_reports_read_failure(monkeypatch) -> None:
    from plugin.data import repo_style

    def _boom(*a, **k):
        raise RuntimeError("DB down")
    monkeypatch.setattr(repo_style, "list_cards", _boom)
    text = commands.handle_command(ACC, "/style list")
    assert text.startswith("❌") and "读取失败" in text


def test_show_renders_card_body(monkeypatch) -> None:
    from plugin.data import repo_style
    from plugin.inbound import style_learn

    monkeypatch.setattr(commands, "_safe_cfg", lambda account_id: {"style": {}})
    monkeypatch.setattr(repo_style, "get_card", lambda account_id, gid: {
        "group_id": gid, "version": 2, "sample_msgs": 120, "sender_count": 5,
        "generated_at": 1700000000, "confidence": 0.8,
        "style_json": json.dumps({"tone": "轻松随意", "sentence_length": "short"},
                                 ensure_ascii=False),
    })
    monkeypatch.setattr(style_learn, "load_style_cards", lambda *a, **k: 0)
    text = commands.handle_command(ACC, f"/style show {GID}")
    assert f"群 {GID} 风格卡 v2" in text
    assert "- 语气：轻松随意" in text and "- 句长：短句为主" in text


def test_show_distinguishes_missing_from_corrupt(monkeypatch) -> None:
    from plugin.data import repo_style

    monkeypatch.setattr(commands, "_safe_cfg", lambda account_id: {"style": {}})
    monkeypatch.setattr(repo_style, "get_card",
                        lambda account_id, gid: None if gid == "none" else {
                            "group_id": gid, "version": 1, "style_json": "{broken",
                        })
    assert "没有群 none 的风格卡" in commands.handle_command(ACC, "/style show none")
    assert "JSON 已损坏" in commands.handle_command(ACC, f"/style show {GID}")


def test_show_requires_group_id() -> None:
    assert "用法: /style show <群id>" in commands.handle_command(ACC, "/style show")


def test_rollback_reports_new_version_and_refreshes_cache(monkeypatch) -> None:
    from plugin.data import repo_style
    from plugin.inbound import style_learn

    refreshed: list[tuple] = []
    monkeypatch.setattr(commands, "_safe_cfg", lambda account_id: {"style": {}})
    monkeypatch.setattr(repo_style, "rollback_card",
                        lambda account_id, gid, version: 8)
    monkeypatch.setattr(style_learn, "load_style_cards",
                        lambda account_id, max_chars=300: refreshed.append(
                            (account_id, max_chars)))
    text = commands.handle_command(ACC, f"/style rollback {GID} 3")
    assert f"回滚到 v3 的内容，新版本 v8" in text
    assert refreshed == [(ACC, 300)]


def test_rollback_rejects_bad_version_and_missing_target(monkeypatch) -> None:
    from plugin.data import repo_style

    monkeypatch.setattr(commands, "_safe_cfg", lambda account_id: {"style": {}})
    assert "版本号不是整数" in commands.handle_command(ACC, f"/style rollback {GID} abc")
    monkeypatch.setattr(repo_style, "rollback_card",
                        lambda account_id, gid, version: None)
    text = commands.handle_command(ACC, f"/style rollback {GID} 99")
    assert "找不到" in text and "v99" in text


def test_rollback_usage_when_args_missing() -> None:
    assert "用法: /style rollback <群id> <版本>" in \
        commands.handle_command(ACC, f"/style rollback {GID}")
