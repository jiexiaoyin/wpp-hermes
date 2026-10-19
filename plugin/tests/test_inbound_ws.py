"""WS 通道（``inbound_ws.py``）的契约。

这条通道是 **webhook 断了之后唯一的退路**（2026-10-19 就真断过一次：secret 配错
让主 webhook 每条 403，全靠 WS 撑着）。它原来覆盖率 18%，缺口全在 ``_dispatch``
和退避判定上 —— 而那两个地方坏掉的共同症状是**静默**：连接还在、日志干净、
消息就是不进来。

所以这里钉的是"坏情况下必须继续跑"的语义，而不是 happy path：

1. **坏帧只跳过自己**——一条畸形 JSON 不能把整条连接打断（vendor 换版本时常见）；
2. **心跳帧不是消息**——``connection_ready`` / ``sync_update`` 不带消息体，当成
   消息喂给下游会让 agent 去"回答"一条通知；其中 ``sync_update`` 要转成
   ``on_event("sync-update")`` 让兜底去 ``/Msg/Sync`` 补拉；
3. **``on_raw`` 抛错不能中断读循环**——否则一条处理失败的消息让整个账号失联；
4. **被拒码停止重连**（1002/1008/4004）——重试不会好，还会打爆 vendor；
5. **``stop()`` 期间 sleep 必须立刻返回**——否则停机要等满 30 秒退避。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugin.platform.inbound_ws import WppWsClient

WS_URL = "wss://vendor.example/ws/sync"
AUTH = "auth-123"


def _client(on_raw=None, on_event=None) -> WppWsClient:
    return WppWsClient(
        account_id="default",
        ws_url=WS_URL,
        authcode=AUTH,
        on_raw=on_raw or AsyncMock(),
        on_event=on_event,
    )


# ------------------------------------------------------------------ _url

def test_url_appends_the_authcode():
    assert _client()._url() == f"{WS_URL}?authcode={AUTH}"


def test_url_keeps_existing_query_string_untouched():
    """URL 里已经有 ``?`` 时不能再拼一个 —— 会让 vendor 收到两个 query 串。"""
    c = WppWsClient("default", f"{WS_URL}?foo=1", AUTH, AsyncMock())
    assert c._url() == f"{WS_URL}?foo=1"


def test_url_without_authcode_is_returned_as_is():
    c = WppWsClient("default", WS_URL, "", AsyncMock())
    assert c._url() == WS_URL


# -------------------------------------------------------------- _dispatch

@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    "not json at all",
    b"{broken",
    None,
    12345,
    "[1, 2, 3]",          # 合法 JSON 但不是 dict
    '"a string"',
])
async def test_malformed_frames_are_skipped_without_raising(raw):
    on_raw = AsyncMock()
    await _client(on_raw=on_raw)._dispatch(raw)
    on_raw.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["connection_ready", "sync_update"])
async def test_heartbeat_frames_are_not_treated_as_messages(kind):
    """这两类帧不带消息体 —— 当消息喂下去，agent 会去"回答"一条通知。"""
    on_raw = AsyncMock()
    await _client(on_raw=on_raw)._dispatch(json.dumps({"Data": {"type": kind}}))
    on_raw.assert_not_called()


@pytest.mark.asyncio
async def test_sync_update_triggers_the_fallback_pull():
    """sync_update = "有新东西"，让兜底去 /Msg/Sync 拉一趟。"""
    on_event = MagicMock()
    on_raw = AsyncMock()
    await _client(on_raw=on_raw, on_event=on_event)._dispatch(
        json.dumps({"Data": {"type": "sync_update"}}))
    on_event.assert_called_once_with("sync-update")
    on_raw.assert_not_called()


@pytest.mark.asyncio
async def test_connection_ready_does_not_trigger_the_fallback():
    """握手 ack —— 开连时已经拉过一趟了，再拉就是白打厂商。"""
    on_event = MagicMock()
    await _client(on_event=on_event)._dispatch(json.dumps({"Data": {"type": "connection_ready"}}))
    on_event.assert_not_called()


@pytest.mark.asyncio
async def test_real_frames_are_passed_to_on_raw():
    on_raw = AsyncMock()
    payload = {"Data": {"MsgId": "1", "type": "recvMsg"}}
    await _client(on_raw=on_raw)._dispatch(json.dumps(payload))
    on_raw.assert_awaited_once_with(payload)


@pytest.mark.asyncio
async def test_on_raw_failure_does_not_break_the_read_loop():
    """一条消息处理失败，不能让整个账号失联。"""
    on_raw = AsyncMock(side_effect=RuntimeError("boom"))
    await _client(on_raw=on_raw)._dispatch(json.dumps({"Data": {"MsgId": "1"}}))
    on_raw.assert_awaited()          # 必须真的试过，且异常被吞掉


@pytest.mark.asyncio
async def test_data_not_a_dict_falls_through_to_on_raw():
    """``Data`` 是数组/字符串时不是心跳帧，按普通帧处理（下游自己去解析）。"""
    on_raw = AsyncMock()
    await _client(on_raw=on_raw)._dispatch(json.dumps({"Data": [1, 2]}))
    on_raw.assert_awaited()


# ------------------------------------------------------------------- _emit

def test_event_hook_failure_does_not_propagate():
    """钩子是旁路 —— 它炸了不许影响连接与收帧。"""
    on_event = MagicMock(side_effect=RuntimeError("hook down"))
    _client(on_event=on_event)._emit("ws-open")     # 不抛
    on_event.assert_called_once_with("ws-open")


def test_no_hook_is_a_noop():
    _client()._emit("ws-open")                      # 不抛即可


# --------------------------------------------------- _sleep_interruptible

@pytest.mark.asyncio
async def test_sleep_returns_true_immediately_when_stopped():
    """停机不该等满退避 —— 否则 stop() 要卡 30 秒。"""
    c = _client()
    c._stop.set()
    assert await c._sleep_interruptible(30.0) is True


@pytest.mark.asyncio
async def test_sleep_returns_false_after_timeout():
    c = _client()
    assert await c._sleep_interruptible(0.01) is False


# --------------------------------------------------------------- 生命周期

@pytest.mark.asyncio
async def test_start_is_idempotent():
    c = _client()
    fake_task = MagicMock()
    fake_task.done.return_value = False      # 真 Task 在跑时 done() 是 False
    with pytest.MonkeyPatch.context() as mp:
        started = []
        mp.setattr(asyncio, "create_task",
                   lambda coro, **kw: started.append(coro) or fake_task)
        c.start()
        c.start()
    assert len(started) == 1, "重复 start 不能起两个重连循环"
    for coro in started:
        coro.close()


@pytest.mark.asyncio
async def test_stop_clears_state_and_marks_not_connected():
    c = _client()
    c.connected = True
    await c.stop()
    assert c.connected is False
    assert c._task is None
    assert c._stop.is_set()


def test_running_reflects_the_task_state():
    c = _client()
    assert c.running is False
    fake = MagicMock()
    fake.done.return_value = False
    c._task = fake
    assert c.running is True
    fake.done.return_value = True
    assert c.running is False


# ------------------------------------------------------------ _run 退避策略
#
# 这一段是整个模块最贵的地方：退避算错不会报错，只会表现为"vendor 网关被打爆"
# 或"断了以后再也连不回来"。用假 websockets 模块驱动真实循环。

import sys
import types


def _fake_websockets(*, exc_factory=None, status=None, code=None):
    """造一个 websockets 替身：``connect()`` 直接抛指定异常。"""
    mod = types.ModuleType("websockets")

    class ConnectionClosed(Exception):
        def __init__(self, close_code):
            super().__init__(f"closed {close_code}")
            self.rcvd = types.SimpleNamespace(code=close_code)

    class ServerError(Exception):
        def __init__(self, status_code):
            super().__init__(f"{status_code}")
            self.response = types.SimpleNamespace(status_code=status_code)

    def _connect(*_a, **_k):
        # 同步抛：真实 connect() 也是同步函数（返回 ctx manager），异常在求值时就抛
        if exc_factory is not None:
            raise exc_factory()
        raise ConnectionClosed(code)

    mod.connect = _connect
    mod.exceptions = types.SimpleNamespace(ConnectionClosed=ConnectionClosed)
    mod.ServerError = ServerError
    return mod


@pytest.mark.asyncio
async def test_fatal_close_code_stops_the_loop(monkeypatch):
    """1008 = 策略拒绝（authcode 无效）—— 重试不会好，还会打爆 vendor。"""
    from plugin.platform.inbound_ws import _FATAL_CLOSE_CODES

    for code in _FATAL_CLOSE_CODES:
        fake = _fake_websockets(code=code)
        monkeypatch.setitem(sys.modules, "websockets", fake)
        c = _client()
        # 致命码必须让 _run 直接返回，而不是睡 1s 再试
        await asyncio.wait_for(c._run(), timeout=1.0)
        assert c.connected is False


@pytest.mark.asyncio
async def test_non_fatal_close_keeps_retrying_with_backoff(monkeypatch):
    """普通断开要一直重试（直到 stop），退避不能是"放弃"。"""
    fake = _fake_websockets(code=1006)
    monkeypatch.setitem(sys.modules, "websockets", fake)

    sleeps: list[float] = []
    c = _client()

    async def _record_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 4:
            c._stop.set()
        return c._stop.is_set()

    monkeypatch.setattr(c, "_sleep_interruptible", _record_sleep)
    await asyncio.wait_for(c._run(), timeout=2.0)

    assert sleeps[:4] == [1.0, 2.0, 4.0, 8.0], f"指数退避序列错了：{sleeps}"


@pytest.mark.asyncio
async def test_backoff_is_capped_at_30_seconds(monkeypatch):
    fake = _fake_websockets(code=1006)
    monkeypatch.setitem(sys.modules, "websockets", fake)

    sleeps: list[float] = []
    c = _client()

    async def _record_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 8:
            c._stop.set()
        return c._stop.is_set()

    monkeypatch.setattr(c, "_sleep_interruptible", _record_sleep)
    await asyncio.wait_for(c._run(), timeout=2.0)

    assert max(sleeps) <= 30.0, f"退避超过上限 30s：{sleeps}"
    assert sleeps[-1] == 30.0


@pytest.mark.asyncio
async def test_five_consecutive_5xx_enters_long_backoff(monkeypatch):
    """vendor 网关整体故障时不要每秒空转 —— 连续 5 次 502/503/504 退到 300s。"""
    from plugin.platform.inbound_ws import _LONG_BACKOFF_SECONDS

    sleeps: list[float] = []
    c = _client()

    async def _record_sleep(seconds):
        sleeps.append(seconds)
        # 不管有没有进长退避，记录够 8 次就停 —— 否则断言失败时会挂死
        if len(sleeps) >= 8:
            c._stop.set()
        return c._stop.is_set()

    state = {"n": 0}

    class _Flaky(Exception):
        def __init__(self):
            state["n"] += 1
            self.response = types.SimpleNamespace(status_code=503)

    fake = _fake_websockets(exc_factory=_Flaky)
    monkeypatch.setitem(sys.modules, "websockets", fake)
    monkeypatch.setattr(c, "_sleep_interruptible", _record_sleep)

    await asyncio.wait_for(c._run(), timeout=2.0)

    assert _LONG_BACKOFF_SECONDS in sleeps, f"连续 5xx 没有进入长退避：{sleeps}"


@pytest.mark.asyncio
async def test_stop_interrupts_the_backoff_sleep(monkeypatch):
    """停机不能被 30 秒退避卡住 —— stop() 要能立刻生效。"""
    fake = _fake_websockets(code=1006)
    monkeypatch.setitem(sys.modules, "websockets", fake)

    c = _client()

    async def _stop_during_sleep(_seconds):
        c._stop.set()
        return True

    monkeypatch.setattr(c, "_sleep_interruptible", _stop_during_sleep)
    await asyncio.wait_for(c._run(), timeout=1.0)
    assert c._stop.is_set()


@pytest.mark.asyncio
async def test_successful_connect_resets_the_backoff(monkeypatch):
    """连上过一次之后再断，退避要回到 1s（否则一次长断之后永远慢）。"""
    sleeps: list[float] = []
    c = _client()

    async def _record_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            c._stop.set()
        return c._stop.is_set()

    monkeypatch.setattr(c, "_sleep_interruptible", _record_sleep)

    state = {"n": 0}

    class _ClosableWs:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            async def _gen():
                if False:
                    yield  # 立刻结束，模拟"连上后马上断"
            return _gen()

    mod = types.ModuleType("websockets")

    def _connect(*_a, **_k):
        state["n"] += 1
        return _ClosableWs()

    mod.connect = _connect
    mod.exceptions = types.SimpleNamespace(ConnectionClosed=type("CC", (Exception,), {}))
    monkeypatch.setitem(sys.modules, "websockets", mod)

    await asyncio.wait_for(c._run(), timeout=2.0)

    assert state["n"] >= 2, "应该重连过"
    assert all(s == 1.0 for s in sleeps), f"每次连上后都应重置为 1s，实际 {sleeps}"
