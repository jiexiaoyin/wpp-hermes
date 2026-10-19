"""私聊主动关心（``plugin/inbound/proactive.py``，2026-10-19）。

最坏的失效形态是**在不该发的时候发了**：给陌生人发、半夜发、对方刚说完就追着发、
一天发十条。所以判据全下在 ``is_eligible`` / ``in_quiet_hours`` 的边界与
``maybe_send_once`` 的实际发送次数上。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from plugin.inbound import proactive as pa


@pytest.fixture(autouse=True)
def _clear_memory() -> None:
    """``_last_sent`` 是进程级内存记录（生产里就是要它跨 sweep 存活）——测试间必须清。"""
    pa._last_sent.clear()


# ============================================================ 配置

def test_defaults_are_disabled_with_an_empty_allowlist() -> None:
    cfg = pa.resolve_proactive_cfg(None)
    assert cfg.enabled is False
    assert cfg.allow_from == ()
    assert cfg.max_per_day == 1
    assert cfg.min_idle_hours == 20.0
    assert cfg.quiet_hours == (23, 8)


def test_quiet_hours_accept_range_and_object_forms() -> None:
    assert pa.resolve_proactive_cfg({"quietHours": ["22:30", "07:00"]}).quiet_hours == (22, 7)
    assert pa.resolve_proactive_cfg(
        {"quietHours": {"start": 0, "end": 6}}).quiet_hours == (0, 6)


def test_model_falls_back_to_ai_judge_model() -> None:
    cfg = pa.resolve_proactive_cfg({}, ai={"judgeModel": "qwen-plus"})
    assert cfg.model == "qwen-plus"
    assert pa.resolve_proactive_cfg({"model": "x"}, ai={"judgeModel": "y"}).model == "x"


# ============================================================ 静默时段

def test_quiet_hours_wrapping_midnight() -> None:
    cfg = pa.ProactiveConfig(enabled=True, quiet_hours=(23, 8))

    def at(hour: int, minute: int = 0) -> float:
        return time.mktime((2026, 10, 19, hour, minute, 0, 0, 0, -1))

    assert pa.in_quiet_hours(cfg, at(23)) is True
    assert pa.in_quiet_hours(cfg, at(2)) is True
    assert pa.in_quiet_hours(cfg, at(7, 59)) is True
    assert pa.in_quiet_hours(cfg, at(8)) is False
    assert pa.in_quiet_hours(cfg, at(15)) is False


def test_non_wrapping_quiet_hours() -> None:
    cfg = pa.ProactiveConfig(enabled=True, quiet_hours=(1, 6))

    def at(hour: int) -> float:
        return time.mktime((2026, 10, 19, hour, 0, 0, 0, 0, -1))

    assert pa.in_quiet_hours(cfg, at(1)) is True
    assert pa.in_quiet_hours(cfg, at(5)) is True
    assert pa.in_quiet_hours(cfg, at(6)) is False
    assert pa.in_quiet_hours(cfg, at(0)) is False


def test_equal_bounds_mean_no_quiet_hours() -> None:
    assert pa.in_quiet_hours(pa.ProactiveConfig(quiet_hours=(0, 0))) is False


# ============================================================ 触发判定

NOW = 1_800_000_000


def _row(direction: str, hours_ago: float, content: str = "x") -> dict:
    return {"direction": direction, "at_sec": NOW - int(hours_ago * 3600),
            "content": content}


def _cfg(**kw) -> pa.ProactiveConfig:
    return pa.ProactiveConfig(enabled=True, allow_from=("u1",), **kw)


def test_eligible_when_last_message_is_incoming_and_old_enough() -> None:
    assert pa.is_eligible(_cfg(), [_row("incoming", 30)], NOW) is True


def test_not_eligible_when_conversation_stopped_on_us() -> None:
    rows = [_row("incoming", 40), _row("outbound", 30)]
    assert pa.is_eligible(_cfg(), rows, NOW) is False


def test_not_eligible_when_too_recent() -> None:
    assert pa.is_eligible(_cfg(), [_row("incoming", 5)], NOW) is False


def test_not_eligible_when_already_messaged_today() -> None:
    rows = [_row("incoming", 40), _row("outbound", 2), _row("incoming", 1)]
    assert pa.is_eligible(_cfg(), rows, NOW) is False


def test_empty_allowlist_or_disabled_is_never_eligible() -> None:
    rows = [_row("incoming", 30)]
    assert pa.is_eligible(pa.ProactiveConfig(enabled=True), rows, NOW) is False
    assert pa.is_eligible(pa.ProactiveConfig(enabled=False, allow_from=("u1",)),
                          rows, NOW) is False
    assert pa.is_eligible(_cfg(), [], NOW) is False


# ============================================================ 输出清洗

def test_clean_output_unwraps_quotes_and_prefixes() -> None:
    assert pa.clean_output("消息：\"最近忙完了吧？\"") == "最近忙完了吧？"
    assert pa.clean_output("在吗") == "在吗"
    assert pa.clean_output("（无）") == ""
    assert pa.clean_output("") == ""


def test_clean_output_truncates_on_a_sentence_boundary() -> None:
    out = pa.clean_output("第一句话讲完了。第二句话还没讲完", max_chars=12)
    assert out == "第一句话讲完了。"


# ============================================================ 一轮 sweep

class _Client:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, peer_id: str, text: str):
        self.sent.append((peer_id, text))
        return {"Data": {"Msgid": 1, "Newmsgid": 2}}


class _Adapter:
    def __init__(self, raw: dict) -> None:
        # 账号对象持有的是**原始 cfg 段**（生产路径就是这样，解析在 maybe_send_once 里做）
        self.account_obj = type("A", (), {"proactive": raw, "raw": {}})()
        self._clients = {"acct": _Client()}

    def account(self, account_id: str):
        return self.account_obj


def _run(monkeypatch, adapter, rows, *, generated="最近忙完了吧？", now=NOW):
    from plugin.data import repo_messages

    monkeypatch.setattr(repo_messages, "list_messages_by_peer", lambda *a, **k: rows)
    # ``_generate`` 的真实职责 = 调模型 + clean_output ⇒ 桩也走同一条清洗
    monkeypatch.setattr(pa, "_generate", _async_return(pa.clean_output(generated)))
    monkeypatch.setattr(pa, "_persist", lambda *a, **k: None)
    return asyncio.run(pa.maybe_send_once(adapter, "acct", now))


def _async_return(value):
    async def _fn(*a, **k):
        return value
    return _fn


def test_sweep_sends_once_when_eligible(monkeypatch) -> None:
    adapter = _Adapter({
        "enabled": True, "allowFrom": ["u1"], "minIdleHours": 20,
        "quietHours": [0, 0],
    })
    result = _run(monkeypatch, adapter, [_row("incoming", 30)])
    assert result == {"sent": 1, "reason": "ok"}
    assert adapter._clients["acct"].sent == [("u1", "最近忙完了吧？")]


def test_sweep_does_not_repeat_on_the_same_day(monkeypatch) -> None:
    adapter = _Adapter({
        "enabled": True, "allowFrom": ["u1"], "quietHours": [0, 0],
    })
    _run(monkeypatch, adapter, [_row("incoming", 30)])
    # 第二轮：历史仍是"对方 30 小时前说的"（落库失败也没关系）——内存记录必须拦住
    _run(monkeypatch, adapter, [_row("incoming", 30)])
    assert len(adapter._clients["acct"].sent) == 1


def test_sweep_skips_when_disabled_or_allowlist_empty(monkeypatch) -> None:
    for raw, reason in (({"enabled": False, "allowFrom": ["u1"]}, "disabled"),
                        ({"enabled": True, "allowFrom": []}, "allowFrom_empty")):
        adapter = _Adapter(raw)
        assert _run(monkeypatch, adapter, [_row("incoming", 30)]) == \
            {"sent": 0, "reason": reason}
        assert adapter._clients["acct"].sent == []


def test_sweep_respects_quiet_hours(monkeypatch) -> None:
    adapter = _Adapter({
        "enabled": True, "allowFrom": ["u1"], "quietHours": [0, 23],
    })
    # NOW 对应的本地小时不确定 ⇒ 直接钉死一个判定
    monkeypatch.setattr(pa, "in_quiet_hours", lambda cfg, now=None: True)
    assert _run(monkeypatch, adapter, [_row("incoming", 30)]) == \
        {"sent": 0, "reason": "quiet_hours"}


def test_sweep_skips_unusable_model_output(monkeypatch) -> None:
    adapter = _Adapter({
        "enabled": True, "allowFrom": ["u1"], "quietHours": [0, 0],
    })
    result = _run(monkeypatch, adapter, [_row("incoming", 30)], generated="（无）")
    assert result["sent"] == 0
    assert adapter._clients["acct"].sent == []


def test_sweep_survives_db_failure(monkeypatch) -> None:
    from plugin.data import repo_messages

    adapter = _Adapter({
        "enabled": True, "allowFrom": ["u1"], "quietHours": [0, 0],
    })

    def _boom(*a, **k):
        raise RuntimeError("DB down")
    monkeypatch.setattr(repo_messages, "list_messages_by_peer", _boom)
    assert asyncio.run(pa.maybe_send_once(adapter, "acct", NOW))["sent"] == 0


def test_sweep_daily_cap_uses_full_count_not_the_truncated_window() -> None:
    """历史窗口里看不到 outbound，不代表今天没聊过。

    窗口只有 ``historyLimit`` 条：对方连续刷屏把今天的出站挤出窗口后，
    ``is_eligible`` 会误判"可以主动关心"。执行侧必须以全量计数兜底。
    """
    rows = [_row("incoming", 30)]

    class _Store:
        platform = "wechatpadpro"

        def recent(self, conv, limit=20):
            return rows

        def count_outbound_since(self, conv, since_sec):
            return 3          # 今天已经发过 3 条，只是都不在窗口里

    class _Sender:
        platform = "wechatpadpro"

        async def send_text(self, conv, text, *, quote_id=None):
            raise AssertionError("日预算已满，不该再发")

    cfg = pa.ProactiveConfig(
        enabled=True, allow_from=("u1",), quiet_hours=(0, 0),
        min_idle_hours=20, max_per_day=1,
    )
    result = asyncio.run(pa.maybe_send_once(
        account_id="acct", now=NOW, cfg=cfg, store=_Store(), sender=_Sender(),
        key_for=lambda peer: pa.ConversationKey("wechatpadpro", "acct", "dm", peer),
    ))
    assert result == {"sent": 0, "reason": "ok"}
