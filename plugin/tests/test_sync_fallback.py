"""W5 · WS 增量兜底 + HTTP 心跳（``plugin/core/sync_fallback.py``）。

## 这一面的失效形态（为什么判据都下在"顺序"和"游标"上）

兜底这条链坏了**一封告警都不出**，只表现为"某些消息没人回" —— 而"没人回"在群里
是无法归因的（用户以为机器人不想理他）。四种坏法尤其安静：

* **游标先前进**：崩溃窗口里那批消息永远拉不回来（TS ``ws-client.ts:229-245`` 的 P1 教训）；
* **首次回放历史**：新进程的内存去重是空的 ⇒ 厂商倒回来的上千条历史当新消息走完
  ``_handle_one`` ⇒ **机器人去回复历史消息**（TS 19:56 那次 1458 条重放就是这个形状，
  差别是它那边有 DB 去重挡着，py2 没有 —— 见 ``sync_fallback`` 模块 docstring）；
* **空 KeyBuf 覆盖游标**：每次都退化成"首次全量"，重启后重放上千条；
* **并发双拉**：两个游标互相踩，拉回来的窗口既可能重叠也可能出现空洞。

所以这里的断言是"**盘上/链上真的发生了什么、按什么顺序**"，不是"调用了没"。
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from plugin.core import sync_fallback as SF
from plugin.core.sync_fallback import (
    REASON_OPEN, REASON_PUSH, REASON_TIMER, SyncFallback,
    extract_sync_items, extract_sync_key,
)

#: 已存在游标的哨兵值 —— 大多数用例要的是"**已经过了首次**"那条增量路。
#: 用常量而不是各处硬写，是为了让"这个用例在测首次还是增量"一眼可辨。
SEED = "SEED"

#: 并发用例里"这一趟本该被锁挡住、立刻返回"的等待上限（秒）。
#: ⚠️ 不是超时容忍，是**防止变异体把门禁吊死**：锁被判成装饰（``if self._in_flight`` →
#: ``if False``）之后，第二次触发会真的去等那个没人 `set()` 的闸门 ⇒ 测试**挂住**而不是变红。
#: 变异跑与门禁都是整批串行跑的，挂一次就等于全停（实测被这台机器上的一次挂起卡了 11 分钟）。
#: 套上 ``wait_for`` 后这种变异体会在这里抛 ``TimeoutError`` ⇒ 正常判 KILLED。
_LOCK_PROBE_S = 2.0


def _sync(coro_fn):
    """本仓没有 pytest-asyncio（见 ``test_affection.py:47`` 同款说明）⇒ 手工 ``asyncio.run``。"""
    @functools.wraps(coro_fn)
    def _wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return _wrapper


def _resp(items: list | None = None, key: str | None = "K1") -> dict:
    """造一个 ``/Msg/Sync`` 响应（形状 = TS ``sync?.Data?.CmdList?.List`` / ``KeyBuf.buffer``）。"""
    data: dict = {"CmdList": {"Count": len(items or []), "List": list(items or [])}}
    if key is not None:
        data["KeyBuf"] = {"buffer": key, "iLen": len(key)}
    return {"Code": 0, "Success": True, "Data": data}


class _Api:
    """假厂商客户端：**记录每次传出去的游标**（判据就在这里 —— 增量 vs 全量）。"""

    def __init__(self, responses: list[dict] | None = None, *, boom: bool = False) -> None:
        self.responses = list(responses or [])
        self.sent: list[str] = []
        self.autos = 0
        self.heartbeats = 0
        self.boom = boom
        self.gate: asyncio.Event | None = None

    async def sync_message(self, prev: str = "") -> dict:
        self.sent.append(prev)
        if self.gate is not None:
            await self.gate.wait()
        if self.boom:
            raise RuntimeError("vendor 502")
        return self.responses.pop(0) if self.responses else _resp([], key=None)

    async def auto_heartbeat(self) -> None:
        self.autos += 1

    async def heartbeat_long(self) -> None:
        self.heartbeats += 1
        if self.boom:
            raise RuntimeError("heartbeat 502")


def _mk(api: _Api, events: list, keys: dict | None = None, *, interval: float = 0.0,
        connected: bool = True, seeded: bool = True) -> SyncFallback:
    """装配一个 SyncFallback：派发与游标读写都记进 ``events``（顺序可断言）。

    ``seeded=True``（默认）预置游标 ``SEED`` ⇒ 走**增量**那条路（大多数断言关心的是它）；
    测"首次"的用例显式传 ``seeded=False``。
    """
    keys = {} if keys is None else keys
    if seeded and "acct" not in keys:
        keys["acct"] = SEED

    async def on_message(item: dict) -> None:
        events.append(("msg", item.get("i")))
        if item.get("boom"):
            raise RuntimeError("单条坏消息")

    return SyncFallback(
        "acct", api, on_message, interval_s=interval, is_connected=lambda: connected,
        get_key=lambda a: keys.get(a), set_key=lambda a, k: (events.append(("key", k)), keys.update({a: k})),
    )


# ============================================================ 1. 首次：只建立基线

@_sync
async def test_the_first_pull_only_bootstraps_the_cursor_and_replays_nothing():
    """★ INVARIANT 第 2 条：没有游标时**只落基线、一条都不派发**。

    坏法：把首次全量倒回来的历史当新消息派发 —— 而这时进程的内存去重是空的 ⇒
    历史消息会走完 ``_handle_one`` ⇒ **机器人回复旧消息**（生产可见的伤害）。
    判据落在"**派发事件为空** 且 **游标确实建立了**"上：只看"拉回来了没"分辨不出区别。
    """
    api = _Api([_resp([{"i": 1}, {"i": 2}], key="K1")])
    events: list = []
    keys: dict = {}
    s = _mk(api, events, keys, seeded=False)
    assert await s.trigger(REASON_OPEN) == 0
    assert [e for e in events if e[0] == "msg"] == [], "首次绝不许派发历史"
    assert keys["acct"] == "K1", "基线必须落下来，否则永远停在'首次'（兜底永不生效）"
    assert api.sent == [""]


@_sync
async def test_before_any_cursor_no_keybuf_means_no_baseline_and_no_dispatch():
    """首次而厂商**没回 KeyBuf** ⇒ 不派发、也不写空游标（下次还按首次处理）。"""
    api = _Api([_resp([{"i": 1}], key=None)])
    events: list = []
    keys: dict = {}
    s = _mk(api, events, keys, seeded=False)
    assert await s.trigger(REASON_OPEN) == 0
    assert events == []
    assert keys == {}


@_sync
async def test_every_later_pull_is_incremental_and_does_dispatch():
    """建立基线之后：每次带上次那把游标（增量），并且**派发**。"""
    api = _Api([_resp([{"i": 1}], key="K1"), _resp([{"i": 2}], key="K2")])
    keys: dict = {}
    s = _mk(api, [], keys, seeded=False)
    assert await s.trigger(REASON_OPEN) == 0, "首次：只落基线"
    assert await s.trigger(REASON_TIMER) == 1, "之后：增量 + 派发"
    assert api.sent == ["", "K1"], "第二次必须是增量（带上次的游标）"
    assert keys["acct"] == "K2"


@_sync
async def test_a_broken_db_read_skips_the_round_and_never_advances_the_cursor():
    """读不到游标（库坏/没建表）⇒ **本轮不动**：不拉、不派发、不写基线，下拍重试。

    ⚠️ 这里与"库里真没游标"必须分开：按首次处理会拿空游标拉全窗、然后把
    ``KeyBuf`` 当基线存下 —— 上一窗的消息**一条都没派发就永久跳过**。
    库抖一下就丢一窗，而现场只表现为"有些人没被回"。所以判据是"厂商一次都没被
    调用，且盘上没有任何写入"。

    坏法：把异常往上抛 ⇒ 同样不该发生（兜底永不抛），由上层的宽 except 兜住也不许
    冒充"跳过"—— 那会连日志都分不清。这里用"一个字节都没写"直接钉死。
    """
    api = _Api([_resp([{"i": 1}], key="K1")])
    written: dict = {}
    broken = {"on": True}

    def _read(_a: str) -> str | None:
        if broken["on"]:
            raise RuntimeError("table missing")
        return "OLD"

    s = SyncFallback("acct", api, lambda m: asyncio.sleep(0), interval_s=0,
                     get_key=_read, set_key=lambda a, k: written.update({a: k}))
    assert await s.trigger(REASON_TIMER) == 0
    assert api.sent == [], "读库失败还去打厂商 = 拿空游标拉全窗（那是首次语义）"
    assert written == {}, "读库失败不许写基线（会把上一窗静默跳过）"
    assert s.in_flight is False, "失败后锁必须放开（下一拍还要重试）"

    # 库恢复之后要能接着跑（不是永久停摆）。上一轮没打厂商 ⇒ 那条预置响应还在
    # 队列里（它是给"没跳过"的错误行为准备的），这里清掉换成恢复后的。
    broken["on"] = False
    api.responses.clear()
    api.responses.append(_resp([{"i": 2}], key="K2"))
    assert await s.trigger(REASON_TIMER) == 1
    assert api.sent == ["OLD"], "恢复后必须用旧游标做增量（不是空游标全量）"
    assert written == {"acct": "K2"}


@_sync
async def test_cursor_io_runs_off_the_event_loop():
    """游标读写是**同步 DB**，必须经 ``asyncio.to_thread`` —— 不许堵事件循环。"""
    import threading

    api = _Api([_resp([{"i": 1}], key="K2")])
    seen: dict = {}

    def _read(_a: str) -> str:
        seen["read"] = threading.get_ident()
        return "OLD"

    def _write(_a: str, _k: str) -> None:
        seen["write"] = threading.get_ident()

    s = SyncFallback("acct", api, lambda m: asyncio.sleep(0), interval_s=0,
                     get_key=_read, set_key=_write)
    await s.trigger(REASON_TIMER)
    assert seen.get("read") != threading.get_ident(), "读游标在主线程/事件循环上跑了"
    assert seen.get("write") != threading.get_ident(), "写游标在主线程/事件循环上跑了"


# ============================================================ 2. 游标：顺序与推进

@_sync
async def test_the_cursor_advances_only_after_every_message_was_dispatched():
    """★ INVARIANT 第 1 条：**先派发、后前进游标**。

    判据是**顺序**（事件流里 ``key`` 必须排在所有 ``msg`` 之后），不是"存了没" ——
    先存后发在正常路径上"看起来也对"，只有崩溃窗口才暴露，那种差别看不出来就白测。
    """
    api = _Api([_resp([{"i": 1}, {"i": 2}, {"i": 3}], key="K9")])
    events: list = []
    s = _mk(api, events)
    await s.trigger(REASON_PUSH)
    assert events == [("msg", 1), ("msg", 2), ("msg", 3), ("key", "K9")]


@_sync
async def test_a_failed_dispatch_does_not_stop_the_rest_nor_block_the_cursor():
    """单条坏消息只 warn：**其余照发、游标照走**（否则一条坏消息把整批堵死 ⇒ 消息积压）。"""
    api = _Api([_resp([{"i": 1}, {"i": 2, "boom": True}, {"i": 3}], key="K3")])
    events: list = []
    keys: dict = {}
    s = _mk(api, events, keys)
    assert await s.trigger(REASON_TIMER) == 3
    assert [e for e in events if e[0] == "msg"] == [("msg", 1), ("msg", 2), ("msg", 3)]
    assert keys["acct"] == "K3", "一条派发失败不该让游标停住（重放靠幂等兜，不靠卡游标）"


@_sync
async def test_no_keybuf_never_overwrites_the_existing_cursor():
    """★ 响应没带 ``KeyBuf`` ⇒ **不写**（写空串 = 下次全量重放）。

    坏法：把"取不到 key"当成空串存下去 —— 判据是"存量压根没变"。
    """
    api = _Api([_resp([{"i": 1}], key=None)])
    events: list = []
    keys: dict = {"acct": "OLD"}
    s = _mk(api, events, keys)
    await s.trigger(REASON_TIMER)
    assert keys["acct"] == "OLD"
    assert ("key", "") not in events and not [e for e in events if e[0] == "key"]


@_sync
async def test_an_empty_cmdlist_is_a_normal_path_and_still_advances_the_cursor():
    """空 CmdList = "没有新消息"（正常路径，不是异常）：返 0 条、但**游标要照样前进**。"""
    api = _Api([_resp([], key="K5")])
    keys: dict = {}
    s = _mk(api, [], keys)
    assert await s.trigger(REASON_TIMER) == 0
    assert keys["acct"] == "K5"


# ============================================================ 3. 失败面：只 warn 不抛

@_sync
async def test_sync_failure_only_warns_and_leaves_the_cursor_alone():
    """厂商失败 ⇒ 返 0、**不抛**、游标不动，且**锁要放开**（否则一次网络抖动 = 永久停止兜底）。"""
    api = _Api(boom=True)
    keys: dict = {"acct": "OLD"}
    s = _mk(api, [], keys)
    assert await s.trigger(REASON_TIMER) == 0
    assert keys["acct"] == "OLD"
    assert s.in_flight is False
    api.responses.append(_resp([{"i": 1}], key="K2"))
    api.boom = False
    assert await s.trigger(REASON_TIMER) == 1, "失败之后必须还能再拉"


@_sync
async def test_a_broken_db_write_only_warns():
    """存游标失败只 warn（代价是下次重放，幂等安全）—— 不许把派发链路带崩。"""
    api = _Api([_resp([{"i": 1}], key="K1")])

    def _boom(_a: str, _k: str) -> None:
        raise RuntimeError("disk full")

    # 已过首次（有游标可读）⇒ 走增量那条路，才谈得上"存游标失败"
    s = SyncFallback("acct", api, lambda m: asyncio.sleep(0), interval_s=0,
                     get_key=lambda a: "OLD", set_key=_boom)
    assert await s.trigger(REASON_TIMER) == 1


@_sync
async def test_heartbeat_failure_only_warns():
    api = _Api(boom=True)
    s = _mk(api, [])
    await s.heartbeat()          # 不许抛
    await s.auto_heartbeat()


# ============================================================ 4. 并发：一趟只拉一次

@_sync
async def test_a_second_trigger_while_one_is_in_flight_is_skipped():
    """★ INVARIANT 第 3 条：in-flight 锁。WS 风暴 + 定时器 + 开连会同时来。

    判据：阻塞住第一趟，再打两次 ⇒ 厂商**只收到一次**请求；第一次放开后照常完成。
    """
    api = _Api([_resp([{"i": 1}], key="K1")])
    api.gate = asyncio.Event()
    s = _mk(api, [])
    first = asyncio.ensure_future(s.trigger(REASON_PUSH))
    await asyncio.sleep(0)                       # 让第一趟进入 in_flight 并卡在 api 上
    assert s.in_flight is True
    # ⚠️ 这两次**必须套 wait_for**：锁若被改没了，它们会卡在同一个闸门上（见 _LOCK_PROBE_S）。
    assert await asyncio.wait_for(s.trigger(REASON_TIMER), _LOCK_PROBE_S) == 0
    assert await asyncio.wait_for(s.trigger(REASON_OPEN), _LOCK_PROBE_S) == 0
    api.gate.set()
    assert await asyncio.wait_for(first, _LOCK_PROBE_S) == 1
    assert api.sent == [SEED], "并发触发必须被锁挡住（否则两个游标互相踩）"


def test_spawn_does_not_block_the_caller_and_still_runs():
    """``spawn`` = TS 的 ``void triggerSync``：**不许**阻塞 WS 读循环。

    判据：spawn 返回后派发还没发生（同步返回），事件循环跑起来之后才有结果。
    """
    async def main() -> None:
        api = _Api([_resp([{"i": 1}], key="K1")])
        events: list = []
        s = _mk(api, events)
        s.spawn(REASON_PUSH)
        assert events == [], "spawn 必须是异步起跑（同步等完就把读循环堵住了）"
        # 游标读写走 ``to_thread`` ⇒ 完成要等线程池真正跑完（纯 sleep(0) 转不够
        # 线程启动的时间）；给足上限，正常路径几毫秒就出集合。
        for _ in range(1000):
            await asyncio.sleep(0.001)
            if events and not s._tasks:     # 任务**跑完**（含 done 回调）才算数
                break
        assert [e for e in events if e[0] == "msg"] == [("msg", 1)]
        assert events[-1] == ("key", "K1"), "派发在前、游标在后"
        assert s._tasks == set(), "任务做完要出集合（否则长时间运行会攒泄漏）"

    asyncio.run(main())


def test_spawn_swallows_an_api_failure_without_an_unretrieved_exception():
    """spawn 出去的任务失败必须**在内部收掉**（否则 asyncio 打 "exception was never retrieved"）。"""
    async def main() -> None:
        api = _Api(boom=True)
        s = _mk(api, [])
        s.spawn(REASON_TIMER)
        for _ in range(10):
            await asyncio.sleep(0)
        assert s._tasks == set()
        assert s.in_flight is False

    asyncio.run(main())


# ============================================================ 5. 开连 / 心跳

@_sync
async def test_ws_open_enables_vendor_auto_heartbeat_and_pulls_once():
    """开连那一脚：**先**开厂商自动心跳，再拉一趟（TS ``:137-142`` 同序）。"""
    api = _Api([_resp([], key="K1")])
    s = _mk(api, [])
    await s._run(REASON_OPEN)
    assert api.autos == 1
    assert api.sent == [SEED]


@_sync
async def test_a_push_notification_does_not_touch_auto_heartbeat():
    """帧通知那条路**不该**去开自动心跳（否则每来一条消息就打一次厂商）。"""
    api = _Api([_resp([], key="K1")])
    s = _mk(api, [])
    await s._run(REASON_PUSH)
    assert api.autos == 0


@_sync
async def test_the_http_heartbeat_is_skipped_while_the_socket_is_down():
    """断线时不许发 HTTP 心跳（TS ``!this.connected`` 同款守卫）——连都没连，发它只会刷错误日志。"""
    api = _Api()
    s = _mk(api, [], connected=False)
    await s.heartbeat()
    assert api.heartbeats == 0

    api2 = _Api()
    s2 = _mk(api2, [], connected=True)
    await s2.heartbeat()
    assert api2.heartbeats == 1


# ============================================================ 6. 定时器

def test_timers_fire_and_stop_cleanly():
    """``interval_s<=0`` 不起定时器；起了的能停干净（停后不再打厂商）。"""
    async def main() -> None:
        api = _Api()
        off = _mk(api, [], interval=0)
        off.start()
        assert off.running is False

        api2 = _Api([_resp([], key=None) for _ in range(10)])
        on = _mk(api2, [], interval=0.01)
        on.start()
        assert on.running is True
        for _ in range(20):
            await asyncio.sleep(0.01)
            if api2.sent and api2.heartbeats:
                break
        assert api2.sent, "兜底定时器要真去拉"
        assert api2.heartbeats, "心跳定时器要真去发"
        await on.stop()
        assert on.running is False
        n = len(api2.sent)
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert len(api2.sent) == n, "停掉之后不许再拉"

    asyncio.run(main())


# ============================================================ 7. 响应形状（纯函数）

@pytest.mark.parametrize("resp,expected", [
    ({}, []),
    (None, []),
    ({"Data": None}, []),
    ({"Data": {"CmdList": {"Count": 0, "List": []}}}, []),
    ({"Data": {"CmdList": {"List": [{"a": 1}, "坏", None]}}}, [{"a": 1}]),
    ({"Data": {"CmdList": [{"a": 1}]}}, [{"a": 1}]),          # vendor 少裹一层
    ({"Data": {"List": [{"b": 2}]}}, [{"b": 2}]),              # 再少裹一层
])
def test_item_extraction_shapes(resp, expected):
    """形状容错只到"三种已知摆法"；非 dict 元素在**提取层**就滤掉（不许扔给派发层）。"""
    assert extract_sync_items(resp) == expected


@pytest.mark.parametrize("resp,expected", [
    ({}, ""),
    (None, ""),
    ({"Data": {"KeyBuf": {"buffer": "K"}}}, "K"),
    ({"Data": {"KeyBuf": "K2"}}, "K2"),            # 少裹一层
    ({"Data": {"KeyBuf": {"buffer": None}}}, ""),
    ({"Data": {"KeyBuf": {"buffer": 123}}}, ""),   # 非串不许硬转（会存下 "123" 这种假游标）
    ({"Data": {"KeyBuf": {"iLen": 3}}}, ""),
])
def test_key_extraction_shapes(resp, expected):
    assert extract_sync_key(resp) == expected


# ============================================================ 8. 默认游标源 = 真表

@_sync
async def test_the_default_cursor_source_is_the_repo_table(monkeypatch):
    """不注入 get/set 时**必须**落到 ``repo_messages`` 的 ``wpp_sync_state`` 读写上。

    判据不是"调用了" —— 而是**取到的游标真的被当成增量发出去了**、**返回的新游标真的存了**。
    """
    from plugin.data import repo_messages

    state: dict = {}
    seen: list = []

    def _get(account_id: str) -> str | None:
        seen.append(("get", account_id))
        return state.get(account_id)

    def _set(account_id: str, synckey: str) -> int:
        seen.append(("set", account_id, synckey))
        state[account_id] = synckey
        return 1

    monkeypatch.setattr(repo_messages, "get_sync_state", _get)
    monkeypatch.setattr(repo_messages, "set_sync_state", _set)

    api = _Api([_resp([{"i": 1}], key="K1"), _resp([], key="K2")])
    s = SyncFallback("acct", api, lambda m: asyncio.sleep(0), interval_s=0)
    assert await s.trigger(REASON_OPEN) == 0, "首次：只落基线（库里还没有游标）"
    assert state == {"acct": "K1"}
    assert await s.trigger(REASON_TIMER) == 0
    assert api.sent == ["", "K1"]
    assert ("set", "acct", "K2") in seen
    assert state == {"acct": "K2"}
