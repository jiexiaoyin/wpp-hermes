"""``WppClient`` 的 aiohttp 会话**按 event loop 隔离**（2026-10-18 修）。

## 为什么这条线是 P0

``_acquire()`` 原来只判 ``self._session is None or self._session.closed``。会话是
**loop-bound** 的（aiohttp 自己的约束），而同一个 ``WppClient`` 实例会被两个不同的
loop 用到：

* 平台 adapter 的 loop（WS / webhook / sync 兜底）；
* Hermes 工具调用的 loop（TUI 下与上面**不是同一个**）。

复现（真实厂商端点，同一实例）：loop1 调用成功 → 换 loop2 立即
``RuntimeError: Event loop is closed`` / ``Timeout context manager should be used
inside a task`` / ``got Future attached to a different loop``。工具侧表现为**所有**
打厂商 API 的工具全失败（收消息的 WS 通道不受影响，所以症状很迷惑）。

修法：记录建会话时的 loop，``self._session_loop is asyncio.get_running_loop()``
不成立就重建；旧会话能关就关（同 loop 直接 await；旧 loop 还活着就投递 close），
关不掉就丢弃 —— 清理失败绝不能把请求带崩。

本文件只用真 aiohttp 的**会话对象**，不发任何网络请求。
"""

from __future__ import annotations

import asyncio
import threading

from plugin.core.api_client import WppClient


def _client() -> WppClient:
    return WppClient(base_url="http://127.0.0.1:1", authcode="x", account_id="t", audit=False)


async def _acquire(c: WppClient):
    return await c._acquire()


def test_same_loop_reuses_one_session() -> None:
    """同一 loop 里的连续调用必须复用同一个会话（否则每调一次泄漏一个连接池）。"""

    async def main():
        c = _client()
        try:
            a = await c._acquire()
            b = await c._acquire()
            assert a is b
            return a
        finally:
            await c.close()

    sess = asyncio.run(main())
    assert sess.closed  # close() 之后旧会话真的关了


def test_new_loop_gets_a_new_session() -> None:
    """换 loop 必须重建会话，且**不许抛**（哪怕旧 loop 已经关闭）。"""
    c = _client()
    first = asyncio.run(_acquire(c))
    second = asyncio.run(_acquire(c))
    assert second is not first
    assert not second.closed
    asyncio.run(c.close())


def test_live_session_on_another_loop_is_never_closed() -> None:
    """★ 核心不变量：别的**活** loop 上的会话绝不能被关掉（会掐断在途请求）。"""
    c = _client()
    state: dict[str, object] = {}
    ready = threading.Event()
    stop = threading.Event()

    def worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def run() -> None:
            state["sess"] = await c._acquire()
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.01)

        try:
            loop.run_until_complete(run())
        finally:
            loop.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert ready.wait(5), "worker loop 没能建起会话"
        other = asyncio.run(_acquire(c))  # 主线程一个全新的 loop
        assert other is not state["sess"], "不同 loop 必须各自一个会话"
        assert not state["sess"].closed, "另一个活 loop 的会话被关了 —— 会掐断在途请求"
    finally:
        stop.set()
        thread.join(5)
        asyncio.run(c.close())


def test_close_from_a_different_loop_is_safe() -> None:
    """跨 loop 调 close() 不能把 "Event loop is closed" 抛给调用方。"""
    c = _client()
    asyncio.run(_acquire(c))
    asyncio.run(c.close())
    assert c._sessions == {}


def test_reacquire_after_close_creates_a_fresh_session() -> None:
    c = _client()

    async def main():
        a = await c._acquire()
        await c.close()
        b = await c._acquire()
        assert b is not a
        await c.close()

    asyncio.run(main())
