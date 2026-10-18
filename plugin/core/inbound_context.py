"""入站消息的**可信身份上下文**（contextvar）—— 工具层唯一的调用者真源。

## 为什么需要它

工具参数里的 ``account_id`` / ``callerWxid`` 都是**模型填的**。群成员做一次
prompt injection，就能让模型把 ``account_id`` 写成另一个已连接账号、把
``callerWxid`` 写成白名单里的老板 wxid —— 这不是"权限校验"，是"让被审对象自己
填审查表"。

真实的账号/发送者在入站链路上是**已知的**（``adapter._handle_one`` 手里就有
``acct.id`` 与 ``msg.from_wxid``）。这里用 ``ContextVar`` 把它钉在**当前异步任务**
上：Hermes 的 ``handle_message`` → 工具 dispatch 都在同一个任务（含
``asyncio.create_task`` 派生）里执行，工具 handler 因此能读到它。

## 语义（fail-closed）

* 没有绑定时（CLI/TUI/standalone 调用）：返回空串，行为与旧实现完全一致 ——
  运维在命令行显式指定 ``account_id`` 是合法意图。
* 有绑定时：
  - ``account_id`` 缺省**或为 "default"** ⇒ 自动补成当前账号（"default" 是工具
    schema 里写的字面默认值，不是"切到另一个账号"的意图；真正的跨账号意图必须
    写出另一个账号名）；
  - ``account_id`` 是**另一个具名账号** ⇒ :func:`account_reject` 返回拒绝话术，
    工具**不执行**；
  - ``callerWxid`` 由 :func:`caller` 覆盖为真实发送者，模型填的值一律忽略。
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Iterator

_account_id: ContextVar[str] = ContextVar("wpp_inbound_account", default="")
_sender: ContextVar[str] = ContextVar("wpp_inbound_sender", default="")
_peer: ContextVar[str] = ContextVar("wpp_inbound_peer", default="")


@contextlib.contextmanager
def bind(account_id: str, sender: str = "", peer: str = "") -> Iterator[None]:
    """在 ``with`` 块内把当前入站消息的身份绑定到 contextvar。"""
    tokens = (_account_id.set(str(account_id or "")),
              _sender.set(str(sender or "")),
              _peer.set(str(peer or "")))
    try:
        yield
    finally:
        _peer.reset(tokens[2])
        _sender.reset(tokens[1])
        _account_id.reset(tokens[0])


def account_id() -> str:
    """当前入站消息所属账号；没有绑定（CLI 等）⇒ 空串。"""
    return _account_id.get()


def sender() -> str:
    """当前入站消息的真实发送者 wxid；没有绑定 ⇒ 空串。"""
    return _sender.get()


def peer() -> str:
    """当前会话 id（群 id 或对方 wxid）；没有绑定 ⇒ 空串。"""
    return _peer.get()


def account_reject(requested: str) -> str:
    """跨账号调用判据：绑定存在且 ``requested`` 不一致 ⇒ 拒绝话术。"""
    bound = account_id()
    req = str(requested or "").strip()
    # "default" 是 schema 广告的哨兵值（见模块 docstring），不是具名账号：
    # 绑定时按"未指定"处理，由 apply_account 改写成当前账号。
    if req == "default":
        req = ""
    if bound and req and req != bound:
        return (f"账号不匹配：当前会话绑定账号 {bound}，拒绝跨账号操作 {req}。"
                f"（account_id 不能由模型指定）")
    return ""


def apply_account(args: dict) -> str:
    """按绑定补齐 ``args["account_id"]``；跨账号时返回拒绝话术。"""
    bound = account_id()
    if not bound:
        return ""
    requested = str(args.get("account_id") or "").strip()
    reject = account_reject(requested)
    if reject:
        return reject
    if not requested or requested == "default":
        args["account_id"] = bound
    return ""
