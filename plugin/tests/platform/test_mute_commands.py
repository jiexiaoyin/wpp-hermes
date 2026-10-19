"""``/mute`` 临时禁言（SpectreCore 轻量项，2026-10-19）。

失效形态：命令回了 ✅ 但入站链路没看这个标志（等于没禁）；或者过期时间算错，
"30 分钟"变成永久/立刻失效。``adapter.py`` 本机 import 不了 ⇒ 行为用桩验命令
文案，接线用 AST 验位置。
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

from plugin.platform import commands

ROOT = Path(__file__).resolve().parents[3]
ADAPTER = ROOT / "plugin/platform/adapter.py"


class _Adapter:
    def __init__(self) -> None:
        self.muted: dict[str, float] = {}

    def mute_group(self, account_id: str, chatroom_id: str, minutes: int = 30) -> float:
        until = time.time() + max(1, min(int(minutes), 7 * 24 * 60)) * 60
        self.muted[chatroom_id] = until
        return until

    def unmute_group(self, account_id: str, chatroom_id: str) -> bool:
        return self.muted.pop(chatroom_id, None) is not None

    def muted_groups(self, account_id: str):
        now = time.time()
        return sorted((k, v) for k, v in self.muted.items() if v > now)


class _Broken:
    def mute_group(self, *a, **k):
        raise RuntimeError("boom")

    def unmute_group(self, *a, **k):
        raise RuntimeError("boom")

    def muted_groups(self, *a, **k):
        raise RuntimeError("boom")


def test_help_lists_mute_and_new_toggles() -> None:
    text = commands.handle_command("acct", "/help")
    assert "/mute" in text and "/tts on|off|status" in text
    assert "/proactive on|off|status" in text


def test_mute_defaults_to_thirty_minutes() -> None:
    adapter = _Adapter()
    text = commands.handle_command("acct", "/mute 123@chatroom", adapter=adapter)
    assert "已禁言 123@chatroom 30 分钟" in text
    assert "123@chatroom" in adapter.muted


def test_mute_accepts_minutes_and_caps_at_seven_days() -> None:
    adapter = _Adapter()
    assert "10 分钟" in commands.handle_command(
        "acct", "/mute 1@chatroom 10", adapter=adapter)
    text = commands.handle_command("acct", "/mute 2@chatroom 999999", adapter=adapter)
    assert "10080 分钟" in text


def test_mute_off_and_list() -> None:
    adapter = _Adapter()
    assert commands.handle_command("acct", "/mute list", adapter=adapter) == \
        "当前没有禁言中的群"
    commands.handle_command("acct", "/mute 1@chatroom 5", adapter=adapter)
    listing = commands.handle_command("acct", "/mute list", adapter=adapter)
    assert "1@chatroom" in listing and "剩余" in listing
    assert "已解除" in commands.handle_command(
        "acct", "/mute off 1@chatroom", adapter=adapter)
    assert "本来就不在" in commands.handle_command(
        "acct", "/mute off 1@chatroom", adapter=adapter)


def test_mute_usage_and_bad_values() -> None:
    adapter = _Adapter()
    assert "用法" in commands.handle_command("acct", "/mute off", adapter=adapter)
    assert "用法" in commands.handle_command(
        "acct", "/mute 1@chatroom abc", adapter=adapter)
    assert "不支持" in commands.handle_command("acct", "/mute 1@chatroom")


def test_mute_never_raises_from_adapter_errors() -> None:
    text = commands.handle_command("acct", "/mute 1@chatroom", adapter=_Broken())
    assert text.startswith("❌")
    assert "失败" in commands.handle_command("acct", "/mute list", adapter=_Broken())


# ------------------------------------------------------------ 接线（AST）

def _adapter_body(name: str) -> ast.AST | None:
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "WppAdapter":
            for fn in node.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == name:
                    return fn
    return None


def test_inbound_checks_mute_right_after_the_command_hook() -> None:
    fn = _adapter_body("_handle_one")
    assert fn is not None
    src = ast.unparse(fn)
    assert "self.group_muted(acct.id, msg.chatroom_id)" in src, \
        "入站没有检查禁言 ⇒ /mute 只是个话术"
    assert src.index("_maybe_command") < src.index("group_muted"), \
        "禁言检查必须排在命令处理之后（filehelper 命令不能被别的东西挡）"
    assert src.index("group_muted") < src.index("pipeline.decide"), \
        "禁言检查要在触发判定之前（禁言期间 @ 也不该进 LLM）"


def test_adapter_owns_the_mute_state_and_helpers() -> None:
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "WppAdapter")
    methods = {n.name for n in cls.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"mute_group", "unmute_group", "group_muted", "muted_groups"} <= methods
    init = next(n for n in cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "__init__")
    assert "_muted_groups" in ast.unparse(init), "禁言状态必须有内存容器"
