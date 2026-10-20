"""Hermes 工具调用契约适配 —— ``handler(args, **kw)``。

## 契约原文（不是猜的，是读出来并在真机验过的）

Hermes 调工具**只有一条路**：

.. code-block:: text

    model_tools.py:839      registry.dispatch(function_name, next_args, **dispatch_kwargs)
    tools/registry.py:907   result = _run_async(entry.handler(args, **kwargs))
    tools/registry.py:909   result = entry.handler(args, **kwargs)

即**第一个参数是模型给的参数字典，位置传参**。树内插件的标准写法见
``plugins/spotify/tools.py:89`` 的 ``def handle(args: dict, **kw) -> str``；
``plugins/AGENTS.md`` 也把 ``handle(args)`` 写作契约。

## 本插件原来错在哪（2026-10-02 在 ``真机`` 真 Hermes 的 registry 上派发实测）

py2 的全部 **299 个 handler** 都是 ``fn(**params)`` 形状 —— 一个位置参数都不收：

* **通用端点表（219 个）**：``registry.register_generic_tools`` 里的 ``_handler`` 首个位置
  参数是**闭包默认值** ``adapter_getter=noop_adapter_getter``，字典被绑到它身上 ⇒
  ``TypeError: 'dict' object is not callable``。（即便字典没绑上去，默认值也是 noop
  ⇒ 返回「adapter 未就绪」。两条路都通不了。）
* **msg / extras / mcp / jargon / wecom / phoneerp（42 个）**：形如 ``async def f(**params)``
  ⇒ ``TypeError: f() takes 0 positional arguments but 1 was given``。

⇒ 模型侧拿到的永远是 ``{"error": "[TOOL_ERROR] Tool execution failed: TypeError..."}``。
而**注册数、``plugin.yaml`` 逐名对账、``hermes plugins doctor`` 全绿** —— 它们只看名字，
不看能不能调。这正是本仓反复吃亏的那种「绿灯下的空转」。

## 另一个被 ``**params`` 顺手掩盖的问题：上下文键漏进厂商请求体

``tools/registry.py:1033`` 的 ``_kwargs_accepted_by``：handler 一旦声明 ``**kwargs``
⇒ **dispatch 传进来的全部关键字都放行**。而 ``_execute_tool``（``model_tools.py:826``）
固定传 ``task_id`` / ``session_id`` / ``user_task``。旧的通用 handler 又用
``body = {k: v for k, v in params.items() if v is not None}`` 直接拼厂商请求体
⇒ 这些字段会**原样发给厂商**（Go 侧静默忽略未知字段，所以表面"没事"）。
适配器的签名**只声明 ``args``、不声明 ``**kw``** ⇒ 上面那个过滤函数什么都不放行，
这些键在**Hermes 那一层**就被丢掉。（将来真有工具需要它们，就在自己的签名里写成
**具名关键字参数** —— ``_kwargs_accepted_by`` 会按名字放行，那是 Hermes 认可的取用方式。）

## 结果契约

``_normalize_handler_result``（``tools/registry.py:877``）只认 **str** 或
``{"_multimodal": True, "content": [...]}`` 信封，其余一律变成 tool_error。
所以适配器把非 str 结果 ``json.dumps``（与 ``registry.mk_handler`` 同口径）。
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)

#: 多模态信封的判别键（``tools/registry.py:883``）。**必须原样透传**：
#: 它是 str 之外唯一合法的返回值形状，编码成 JSON 会把图片/附件弄丢。
MULTIMODAL_KEY = "_multimodal"


def to_tool_result(value: Any) -> Any:
    """按 Hermes 的结果契约收拾返回值：str 原样、多模态信封原样、其余 JSON 编码。"""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and value.get(MULTIMODAL_KEY) is True:
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # 自引用/不可序列化 —— 宁可给个能读的串，也别让工具报错
        return str(value)


class ToolRejected(Exception):
    """入站身份绑定拒绝。文本原样回给模型（不执行 handler）。"""


def _payload(args: Any) -> dict:
    """把 Hermes 的位置参数整理成插件 handler 认的关键字参数。

    ★ ``account_id`` 在这里按**入站上下文**补齐/校验（对所有工具生效，
    包括没装门禁的工具）。没有入站绑定（CLI/TUI/standalone）时是 no-op。
    """
    if args is None:
        return {}
    if not isinstance(args, Mapping):
        raise TypeError(
            f"工具参数必须是 dict（Hermes 契约：handler(args, **kw)），收到 {type(args).__name__}"
        )
    payload = dict(args)
    from ..core import inbound_context

    reject = inbound_context.apply_account(payload)
    if reject:
        raise ToolRejected(reject)
    return payload


def adapt_handler(fn: Callable[..., Any]) -> Callable[..., Any]:
    """把插件的 ``fn(**params)`` 适配成 Hermes 的 ``handler(args)``。

    ⚠️ **签名里故意不写 ``**kw``**：``_kwargs_accepted_by``（``tools/registry.py:1033``）
    对声明了 ``**kwargs`` 的 handler **全部放行** dispatch 的上下文键（``task_id`` /
    ``session_id`` / ``user_task``）。窄签名让 **Hermes 自己**就把它们滤掉 ——
    比"收到再丢"更硬：漏进厂商请求体这条路在结构上不存在。

    ⚠️ **故意不设 ``__wrapped__``**：``inspect.signature`` 默认**穿透** ``__wrapped__``，
    设了它 Hermes 会读到**原函数**的 ``(**params)`` 签名 ⇒ 上面那层过滤立刻失效。
    原函数挂在 ``__original_handler__`` 上（非协议名，不会被穿透）。
    """
    if not callable(fn):
        raise TypeError(f"handler 必须可调用，收到 {type(fn).__name__}")

    if inspect.iscoroutinefunction(fn):

        async def handler(args: Any = None) -> Any:
            try:
                payload = _payload(args)
            except ToolRejected as exc:
                return str(exc)
            return to_tool_result(await fn(**payload))

    else:

        def handler(args: Any = None) -> Any:  # type: ignore[misc]
            try:
                payload = _payload(args)
            except ToolRejected as exc:
                return str(exc)
            return to_tool_result(fn(**payload))

    handler.__name__ = getattr(fn, "__name__", "handler")
    handler.__qualname__ = getattr(fn, "__qualname__", handler.__name__)
    handler.__doc__ = getattr(fn, "__doc__", None)
    handler.__original_handler__ = fn  # type: ignore[attr-defined]
    return handler


def guarded_handler(fn: Callable[..., Any], pre: Callable[[Any], Any]) -> Callable[..., Any]:
    """给已适配的 handler 加一道**调用前**判据：``pre(args)`` 说了话就不执行。

    ``pre`` 是 :func:`plugin.toolkit.policy.make_tool_gate` 造的那个闭包；它返回
    ``None`` 时照常执行，返回字符串时**原样**回给 Hermes（结果是 str ⇒ 契约合法，
    见 :func:`to_tool_result` 的口径）。

    ⚠️ 三条形状约束与 :func:`adapt_handler` **完全一致**，逐条都有判据钉着：
    签名只有 ``args``（不声明 ``**kw``，否则 dispatch 上下文键会被放行）、
    异步形状与真身一致、**不设 ``__wrapped__``**（``inspect.signature`` 会穿透它）。
    ``__original_handler__`` 继续指向**最里层**的原函数（W0 的判据就是按它比的）。
    """
    original = getattr(fn, "__original_handler__", fn)

    if inspect.iscoroutinefunction(fn):

        async def handler(args: Any = None) -> Any:
            reject = pre(args)
            if reject:
                return reject
            return await fn(args)

    else:

        def handler(args: Any = None) -> Any:  # type: ignore[misc]
            reject = pre(args)
            if reject:
                return reject
            return fn(args)

    handler.__name__ = getattr(fn, "__name__", "handler")
    handler.__qualname__ = getattr(fn, "__qualname__", handler.__name__)
    handler.__doc__ = getattr(fn, "__doc__", None)
    handler.__original_handler__ = original  # type: ignore[attr-defined]
    return handler


class _ToolCtx:
    """``ctx`` 代理：**只**改 ``register_tool``，其余属性原样转发。

    为什么包 ctx 而不是改 18 个注册点：收在一处**忘不掉**。工具面还有后面几批要加，
    逐个调用点记得套适配器，迟早漏一个 —— 而漏掉的那个就是"点下去报 TypeError"。
    运行期门禁（``gate``）同理挂在这一层：它是**唯一**的"所有工具都必经"的点。
    """

    __slots__ = ("_inner", "_gate", "_registered", "_names")

    def __init__(self, inner: Any, gate: Any = None) -> None:
        self._inner = inner
        self._gate = gate
        #: 真实成功的 ``register_tool`` 次数。注册层用它对账 —— 旧实现只看
        #: "定义表里有多少条"，某组整体注册失败时数字仍然好看（假绿）。
        self._registered = 0
        #: 成功注册的工具名（``wpp_tools_list`` 用它把非端点表工具也列出来）。
        self._names: list[str] = []

    @property
    def registered_count(self) -> int:
        return self._registered

    @property
    def registered_names(self) -> list[str]:
        return list(self._names)

    def register_tool(self, **kw: Any) -> Any:
        fn = kw.get("handler")
        if fn is not None:
            adapted = adapt_handler(fn)
            gate_pre = None
            if self._gate is not None:
                #: 门禁工厂在**注册期**回答"这个工具受管吗"：不受管就不加门禁层。
                #: 入站身份绑定在 :func:`_payload` 里对所有工具生效，不占这一层。
                gate_pre = self._gate(str(kw.get("name") or ""))
            if gate_pre is not None:
                adapted = guarded_handler(adapted, gate_pre)
            #: ``is_async`` 以**适配后的真实形状**为准，不信调用点的声明：
            #: 声明错了（sync 函数写 is_async=True）会让 Hermes 去 await 一个非协程。
            kw = {**kw, "handler": adapted, "is_async": inspect.iscoroutinefunction(adapted)}
        result = self._inner.register_tool(**kw)
        self._registered += 1
        name = kw.get("name")
        if name:
            self._names.append(str(name))
        return result

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


def wrap_ctx(ctx: Any, gate: Any = None) -> Any:
    """把 ``register(ctx)`` 收到的 ctx 包一层（只影响 ``register_tool``）。

    ``gate`` 是 :func:`plugin.toolkit.policy.make_tool_gate` 的产物（``None`` = 不装门禁，
    单测与工具面探针会这么用）。挂在这里的理由见 :mod:`plugin.toolkit.policy` 的
    「运行期门禁」一节。
    """
    return _ToolCtx(ctx, gate)
