"""工具调用契约回归 —— 299 个工具必须**真能调**，不是"注册上了"。

## 这一门守的是什么（2026-10-02 在 ``真机`` 真机抓到的 P0）

Hermes 调工具只有一条路（``model_tools.py:839`` → ``tools/registry.py:907/909``）：

    entry.handler(args, **kwargs)      # 第一个参数是**模型给的参数字典**，位置传参

而 py2 的 299 个 handler 全是 ``fn(**params)`` —— 一个位置参数都不收。后果在真
Hermes 的 registry 上实测：

* 219 个通用端点工具 ⇒ ``TypeError: 'dict' object is not callable``
  （字典绑到了闭包默认值 ``adapter_getter=noop_adapter_getter`` 上）；
* 42 个 msg/extras/mcp/jargon/wecom/phoneerp ⇒ ``takes 0 positional arguments but 1 was given``。

⇒ 模型侧拿到的永远是 ``{"error": "[TOOL_ERROR] ..."}``。**而注册数、``plugin.yaml``
逐名对账、``doctor`` 全绿** —— 它们只看名字。所以本文件不做"名字对账"（那是
``test_registration.py`` 的活），只钉**可调用性**：

1. 每个 handler 都能接住一个位置 dict，且**不声明 ``**kwargs``**
   （声明了 Hermes 就会把 dispatch 上下文键全部放行 —— 见下）；
2. ``is_async`` 与真实形状一致（声明错了 Hermes 会去 ``await`` 一个非协程）；
3. ★ **dispatch 上下文键不许漏进厂商请求体**；
4. ★ **厂商调用真的发生了**（不是"调了 handler 就完事"）——
   见 :func:`test_the_step_tool_really_calls_setstep`。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from typing import Any

import pytest

#: 通用端点表里的一个普通工具，用来验"参数原样到了请求体"。
PROBE_TOOL = "wpp_group_info"
PROBE_TOOL_ARGS = {"QID": "12345@chatroom"}
PROBE_TOOL_ENDPOINT = "/Group/GetChatRoomInfo"

#: dispatch 固定注入的上下文关键字（``model_tools.py:826``）—— 一个都不许进请求体。
DISPATCH_KWARGS = {"task_id": "task-1", "session_id": "sess-1", "user_task": "帮我查一下"}

#: Hermes 认可的"首参名"（``plugins/AGENTS.md`` 的 ``handle(args)`` 契约）。
_ARGS_NAMES = frozenset({"args", "params", "arguments"})


class _CapCtx:
    """记全量注册 kwargs（名字、schema、handler、is_async）—— 探针的 ctx 只记名字。"""

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}
        self.platforms: list[str] = []

    def register_tool(self, **kw: Any) -> None:
        self.tools[kw["name"]] = kw

    def register_platform(self, **kw: Any) -> None:
        self.platforms.append(str(kw.get("name")))

    def __getattr__(self, _item: str):
        return lambda *a, **k: None


class _FakeClient:
    """记下每一次厂商调用（endpoint / body / method），不联网。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    async def call(self, endpoint: str, body: dict, method: str = "POST") -> dict:
        self.calls.append((endpoint, dict(body), method))
        return {"Success": True, "Code": 0, "Data": {"echo": dict(body)}}


class _FakeAccount:
    def __init__(self, raw: dict | None = None) -> None:
        self.raw = dict(raw or {})


class _FakeAdapter:
    """最小适配器替身：工具面用 ``_clients``，工具门禁读 ``account().raw``。

    ``adminToolsEnabled`` 默认**不开**（与真账号一致）—— 管理面工具默认关闭；
    要验"打开时真打到厂商"，显式传 ``raw={"adminToolsEnabled": True}``。
    """

    def __init__(self, raw: dict | None = None) -> None:
        self.client = _FakeClient()
        self._clients = {"default": self.client}
        self._accounts = {"default": _FakeAccount(raw)}
        self._raw = raw or {}

    def account(self, _account_id: str = "default") -> _FakeAccount:
        return next(iter(self._accounts.values())) if self._accounts else _FakeAccount(self._raw)


def _hermes_dispatch(handler, args: dict, **context: Any) -> str:
    """**逐字模仿** Hermes 的派发：``_kwargs_accepted_by`` + ``handler(args, **kwargs)``。

    为什么复制这两行而不是"直接 ``handler(args)``"：契约的关键之一**正是**
    "``_kwargs_accepted_by`` 会不会把上下文键放行进来"。直接调等于跳过被测的那一步，
    测试就会自证（把 ``**kw`` 加回适配器也照样绿）。
    """
    params = inspect.signature(handler).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        accepted = dict(context)
    else:
        kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        accepted = {k: v for k, v in context.items() if k in params and params[k].kind in kinds}
    out = handler(args, **accepted)
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    return out


def _register_with_adapter(monkeypatch, adapter: _FakeAdapter) -> _CapCtx:
    """在**注册期**就把 ``_get_adapter`` 换成假的（闭包在注册时取这个函数）。

    所以必须在 ``register()`` **之前**打补丁 —— 注册后再换不影响已注册的闭包。
    """
    from plugin.platform import adapter as _adapter

    monkeypatch.setattr(_adapter, "_get_adapter", lambda: adapter)
    ctx = _CapCtx()
    importlib.import_module("plugin").register(ctx)
    return ctx


@pytest.fixture(scope="module")
def registered(stubbed):  # noqa: F811 —— stubbed 由 conftest 提供
    """真跑一遍 ``register()``，保留 schema/handler 供签名检查。"""
    ctx = _CapCtx()
    importlib.import_module("plugin").register(ctx)
    return ctx


# ==================================================== ① 可调用性（形状）

def test_every_handler_takes_a_positional_args_dict(registered):
    """★ 299 个 handler 的首个参数必须能接住位置 dict —— 这正是 P0 的逆命题。

    旧形态两种：``(**params)``（首参是 VAR_KEYWORD）与
    ``(adapter_getter=noop, endpoint=..., **params)``（首参虽是位置参数，但**名字不是 args**
    —— 字典会绑到那个闭包默认值上，正是 ``'dict' object is not callable`` 的来源）。
    """
    bad: list[str] = []
    for name, kw in registered.tools.items():
        ps = list(inspect.signature(kw["handler"]).parameters.values())
        if not ps:
            bad.append(f"{name}: 无参数")
            continue
        first = ps[0]
        if first.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD:
            bad.append(f"{name}: 首参 kind={first.kind.name}（{first.name}）")
        elif first.name not in _ARGS_NAMES:
            bad.append(f"{name}: 首参名为 {first.name!r}，像是被闭包默认值占了位")
    assert not bad, f"这些 handler 接不住 Hermes 的位置 dict：{bad[:10]}"


def test_no_handler_opts_into_the_dispatch_context_kwargs(registered):
    """★ 不许声明 ``**kwargs`` —— 那等于让 Hermes 把上下文键**全部**交给工具。

    ``_kwargs_accepted_by``（``tools/registry.py:1033``）见 ``**kwargs`` 就返回**整个**
    dispatch 关键字集，而 ``_execute_tool``（``model_tools.py:826``）固定传
    ``task_id`` / ``session_id`` / ``user_task``。旧实现是 ``(**params)``，通用 handler
    又拿 params 直接拼厂商请求体 ⇒ 这三个键会**原样发给厂商**（Go 静默忽略未知字段，
    所以既不报错也不告警）。窄签名让 Hermes 在它那一层就滤掉。

    将来真有工具需要上下文信息，请在签名里写成**具名关键字参数**（那是 Hermes 认可的
    取用方式，``_kwargs_accepted_by`` 会按名字放行）。
    """
    bad = [n for n, kw in registered.tools.items()
           if any(p.kind is inspect.Parameter.VAR_KEYWORD
                  for p in inspect.signature(kw["handler"]).parameters.values())]
    assert not bad, f"这些 handler 声明了 **kwargs（上下文键会漏进厂商请求体）：{bad[:10]}"


def test_is_async_matches_the_real_handler_shape(registered):
    """``is_async`` 必须与真实形状一致。

    声明成 True 而函数是同步的 ⇒ Hermes 会 ``_run_async(entry.handler(...))``
    去 await 一个**非协程**；声明成 False 而函数是协程 ⇒ 拿到一个从没被 await 的
    协程对象 ⇒ ``_normalize_handler_result`` 判"unsupported result type"。
    两种都是"注册得上、调不通"。
    """
    bad = [n for n, kw in registered.tools.items()
           if bool(kw.get("is_async")) != inspect.iscoroutinefunction(kw["handler"])]
    assert not bad, f"is_async 与真实形状不符：{bad[:10]}"


def test_every_tool_survives_a_schema_shaped_dispatch(stubbed, monkeypatch):
    """按 schema 造参数派发**全部**工具：不许抛绑定类异常，且返回值必须是 str。

    用假适配器 + ``account_id='default'``，通用工具走到 ``client.call`` 即返回，不联网。
    **只判"调得动"**；"厂商到底收到什么"由下面几条反向判据盯。
    """
    adapter = _FakeAdapter()
    ctx = _register_with_adapter(monkeypatch, adapter)

    def _dummy(t: str):
        return {"string": "x", "integer": 1, "number": 1, "boolean": True,
                "array": [], "object": {}}.get(t, "x")

    bad: list[str] = []
    for name, kw in ctx.tools.items():
        props = (kw.get("schema") or {}).get("properties") or {}
        args = {k: _dummy(v.get("type")) for k, v in props.items()}
        # ⚠️ ``account_id`` 是**路由键**，照 schema 造出来的是乱值 ⇒ 通用工具会走
        #    「账号 x 未连接」分支 ⇒ 一条厂商调用都不会发生，本用例就空转。
        #    但也**只能给声明了它的工具**塞：wecom/phoneerp 那 11 个的 schema 里没有它
        #    （它们是纯 skill、不按账号路由），硬塞会被 handler 判成非法关键字参数。
        if "account_id" in props:
            args["account_id"] = "default"
        try:
            out = _hermes_dispatch(kw["handler"], args, **DISPATCH_KWARGS)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(out, str):
            bad.append(f"{name}: 返回 {type(out).__name__}，Hermes 只认 str/多模态信封")
    assert not bad, f"这些工具派发不过：{bad[:10]}"
    assert adapter.client.calls, "一次厂商调用都没有 ⇒ 假适配器没接上，本断言空转"


# ==================================================== ② 反向判据：厂商真收到了什么

def test_dispatch_context_kwargs_do_not_leak(stubbed, monkeypatch):
    """★ ``task_id`` / ``session_id`` / ``user_task`` 不许出现在厂商请求体里。"""
    adapter = _FakeAdapter()
    ctx = _register_with_adapter(monkeypatch, adapter)
    _hermes_dispatch(ctx.tools[PROBE_TOOL]["handler"],
                     {**PROBE_TOOL_ARGS, "account_id": "default"}, **DISPATCH_KWARGS)

    assert adapter.client.calls, "厂商一次都没被调到 —— 断言会空转"
    endpoint, body, _method = adapter.client.calls[-1]
    assert endpoint == PROBE_TOOL_ENDPOINT, f"端点错了：{endpoint}"
    leaked = sorted(set(body) & set(DISPATCH_KWARGS))
    assert not leaked, f"dispatch 上下文键漏进了厂商请求体：{leaked}（体：{body}）"
    assert body == PROBE_TOOL_ARGS, f"请求体应为模型给的参数原样：{body}"
    assert "account_id" not in body, "account_id 由 handler 自己取走，不该发给厂商"


def test_the_step_tool_really_calls_setstep(stubbed, monkeypatch):
    """★ W4 的纠错必须**真的落到请求上**：``/Tools/SetStep`` + ``{step}``。

    蓝本把它打在 ``/Tools/setproxy`` 且发 ``{steps}`` —— 而 ``setproxy`` 只读 ``proxy``，
    未知字段被 Go 静默忽略 ⇒ ``proxy`` 取零值空串 ⇒ **恢复直连**。所以这条同时钉三件事：
    端点对、键名是 ``step``（不是 ``steps``）、且请求体里没有 ``proxy``。

    ⚠️ 参数**从工具自己的 schema 反推**，不手写：schema 是模型唯一看得见的东西 ——
    手写 ``{"step": …}`` 会把"schema 写成 ``steps``"这类缺陷**绕过去**（handler 收
    ``**params``，照样原样发出去，本用例就空转了）。手写等于只测了后半截。
    """
    adapter = _FakeAdapter()
    ctx = _register_with_adapter(monkeypatch, adapter)
    tool = ctx.tools["wpp_tool_set_step_count"]
    props = tool["schema"]["properties"]
    assert "step" in props, f"schema 必须声明 step（模型只看得见 schema）：{sorted(props)}"
    assert "steps" not in props, "不能是 steps —— Go 会静默忽略这个未知名"
    assert "step" in tool["schema"].get("required", []), "step 是必填（缺了厂商会当 0）"

    args = {k: 4096 for k in props if k != "account_id"}   # 模型照 schema 填
    args["account_id"] = "default"
    _hermes_dispatch(tool["handler"], args, **DISPATCH_KWARGS)

    endpoint, body, _ = adapter.client.calls[-1]
    assert endpoint == "/Tools/SetStep", f"端点必须是 SetStep，实际 {endpoint}"
    assert body == {"step": 4096}, f"请求体必须恰好是 {{'step': 4096}}，实际 {body}"


def test_the_added_proxy_tool_really_calls_setproxy(stubbed, monkeypatch):
    """★ 补注册的 ``wpp_tool_set_proxy`` 真打到 ``/Tools/setproxy`` 且键名是 ``proxy``。

    与步数那条同理**从 schema 反推参数**：``setproxy`` 传空串 = 恢复直连，所以
    "端点对了但键名不对"的失效形态比报错更贵 —— 它会**把账号的出口代理清掉**。
    """
    # 2026-10-18：setproxy 现在挂在 ADMIN_TOOLS_GATE 下（默认关闭）——
    # 这条验的是"打开时请求真的打到厂商"，所以显式把开关打开。
    adapter = _FakeAdapter(raw={"adminToolsEnabled": True})
    ctx = _register_with_adapter(monkeypatch, adapter)
    tool = ctx.tools["wpp_tool_set_proxy"]
    props = tool["schema"]["properties"]
    assert "proxy" in props, f"schema 必须声明 proxy：{sorted(props)}"

    args = {k: "10.0.0.9:8080" for k in props if k != "account_id"}
    args["account_id"] = "default"
    _hermes_dispatch(tool["handler"], args, **DISPATCH_KWARGS)

    endpoint, body, _ = adapter.client.calls[-1]
    assert endpoint == "/Tools/setproxy"
    assert body == {"proxy": "10.0.0.9:8080"}, body


def test_the_proxy_tool_is_closed_unless_the_account_opts_in(stubbed, monkeypatch):
    """★ 默认关闭：账号没写 ``adminToolsEnabled: true`` ⇒ 一次厂商调用都不许发生。

    这是上面那条的**反面**。只测"打开时能调"的话，把门禁整个删掉也照样全绿 ——
    而 ``setproxy`` 传空串就是恢复直连，管理面能力不该默认落到模型手里。
    """
    adapter = _FakeAdapter()          # raw 里没有 adminToolsEnabled
    ctx = _register_with_adapter(monkeypatch, adapter)
    tool = ctx.tools["wpp_tool_set_proxy"]
    props = tool["schema"]["properties"]
    args = {k: "10.0.0.9:8080" for k in props if k != "account_id"}
    args["account_id"] = "default"

    out = _hermes_dispatch(tool["handler"], args, **DISPATCH_KWARGS)

    assert not adapter.client.calls, f"门禁没生效，厂商收到了 {adapter.client.calls}"
    assert "adminToolsEnabled" in out, f"回话必须说清是开关没开：{out}"


def test_the_denied_tools_are_really_gone(registered):
    """撤注册 = **注册表里根本没有**（不是"声明里没有"）。

    对 AI 来说，看不见的名字等于不存在的能力 —— 这是与 TS 裁决对齐的那两条。

    ⚠️ 必须**钉死集合内容**，不能只查"表里的都消失了"：那样把某一条从 ``DENY_TOOLS``
    里删掉时测试照样全绿 —— 而**撤注册名单缩水**正是最该被拦下的那种改动
    （AI 会重新拿到原始 XML 发布能力，注册面一切正常）。
    """
    from plugin.toolkit import policy
    assert policy.DENY_TOOLS == {
        "wpp_fc_publish_raw", "wpp_send_app_msg",
        # 2026-10-18：蓝本里的幽灵重复项（与 wpp_tool_download_video 同端点，
        # 且发旧树的 aesKey/fileId），TS 工具表里没有第三个下载视频入口。
        "wpp_fc_download_video",
    }, f"撤注册面 = 对齐 TS 的裁决，改动它等于改 AI 看得见的危险能力面：{sorted(policy.DENY_TOOLS)}"
    present = sorted(policy.DENY_TOOLS & set(registered.tools))
    assert not present, f"这些危险工具仍在注册表里：{present}"
    # 反向也要成立：它们本来在冻结蓝本里 ⇒ 证明"消失"是策略层干的，不是蓝本就没有。
    from plugin.toolkit import vendor_defs, vendor_defs_extra
    blueprint = {d["name"] for _g, ds in [*vendor_defs.GROUPS, *vendor_defs_extra.GROUPS]
                 for d in ds}
    assert policy.DENY_TOOLS <= blueprint, \
        f"这些不在蓝本里，撤注册是空转：{sorted(policy.DENY_TOOLS - blueprint)}"


# ==================================================== ③ 结果契约（单元）

def test_dict_and_list_results_become_json_strings():
    """非 str 结果必须编码成 str —— ``_normalize_handler_result`` 只认 str/多模态。"""
    from plugin.toolkit.hermes_contract import to_tool_result
    assert to_tool_result({"ok": True}) == json.dumps({"ok": True}, ensure_ascii=False)
    assert to_tool_result([1, 2]) == "[1, 2]"
    assert to_tool_result("已经是字符串") == "已经是字符串"


def test_the_multimodal_envelope_passes_through_untouched():
    """多模态信封是 str 之外**唯一**合法形状，编码成 JSON 会把图片弄丢。"""
    from plugin.toolkit.hermes_contract import to_tool_result
    envelope = {"_multimodal": True, "content": [{"type": "text", "text": "hi"}]}
    assert to_tool_result(envelope) is envelope


def test_the_adapter_does_not_expose_wrapped():
    """★ **绝不能设 ``__wrapped__``**：``inspect.signature`` 会穿透它。

    Hermes 正是用 ``inspect.signature`` 决定"要不要把 ``task_id`` 等上下文键交给这个
    handler"（``_kwargs_accepted_by``）。设了 ``__wrapped__``，Hermes 读到的是**原函数**
    的 ``(**params)`` 签名 ⇒ 上下文键又被放行 ⇒ 上面那条防线的意义就没了。
    原函数挂 ``__original_handler__``（非协议名）。
    """
    from plugin.toolkit.hermes_contract import adapt_handler

    async def original(**params):
        return params

    adapted = adapt_handler(original)
    assert not hasattr(adapted, "__wrapped__"), "不许设 __wrapped__（inspect.signature 会穿透）"
    assert adapted.__original_handler__ is original
    assert "args" in inspect.signature(adapted).parameters, \
        "inspect.signature 必须看到适配后的窄签名"


def test_rejects_a_non_dict_payload():
    """模型给的不是 dict（协议异常）时**明确报错**，不要静默当成空参数。"""
    from plugin.toolkit.hermes_contract import adapt_handler

    async def original(**params):
        return params

    h = adapt_handler(original)
    with pytest.raises(TypeError):
        out = h(["not", "a", "dict"])  # type: ignore[arg-type]
        if inspect.isawaitable(out):
            asyncio.run(out)


# ==================================================== ④ 收口处不许被绕过

def test_registration_wraps_the_ctx_before_any_tool_is_registered():
    """源级门：``register()`` 必须走 ``hermes_contract.wrap_ctx``（并带上运行期门禁）。

    收口就这一处 —— 删掉它，299 个工具全部退回 TypeError，而**所有别的测试**
    （注册数、逐名对账、``doctor``）照样全绿。所以这里既查源码也查行为：
    下面那条行为断言在 :func:`test_every_handler_takes_a_positional_args_dict`。

    ⚠️ W7-5 起这个锚点**必须连门禁参数一起写**：``wrap_ctx`` 是「所有工具都必经」
    的那一层，运行期门禁（``policy.make_tool_gate``）挂在同一个缝上。只留
    ``wrap_ctx(ctx)`` 空调用而把门禁丢掉 ⇒ ``/xiaowei`` 又变谎开关（命令写了一个
    没人读的键），而**注册面、doctor、本文件其余 12 条**全绿。
    门禁那把 getter 写成 ``lambda`` 也是要钉的：每次调用重新取模块属性，
    否则 register() 之后打的补丁（单测 / 工具面探针）一律落空。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "plugin" / "toolkit" / "registration.py").read_text(
        encoding="utf-8")
    assert "_contract.wrap_ctx(ctx, _policy.make_tool_gate(lambda: _adapter._get_adapter()))" in src, \
        "register_tools() 必须把 ctx 包成「工具契约适配 + 运行期门禁」版（漏了任何一半都是绿灯空转）"
