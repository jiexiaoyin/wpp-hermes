"""工具调用契约探针 —— 在**真 Hermes** 上证明工具**真能调**（W0 的验收点）。

**只读**：不连账号（``account_id`` 一律 ``nope``）、不联网、不发消息、不写任何文件。
唯一的进程外副作用是"把插件加载进本进程的 registry"，跑完进程就退。

用法（.183）::

    sh <root>/tools/hermes_run.sh <root>/tools/probe_tool_dispatch.py <root>

（``WPP_TOOLS_ONLY=1`` 由本文件**自己设**，不用外部传 —— 见下。）

## 为什么必须用**真** registry / 真 dispatch

这是 2026-10-02 抓到的 P0 的验收点，而它的整个特征就是"**假环境验不出来**"：

* 注册数、``plugin.yaml`` 逐名对账、``hermes plugins doctor`` **全绿** —— 它们只看名字；
* 本机（无 Hermes）的测试也只能证明"我们的适配层自洽"，证明不了**Hermes 真的会这么调**。

所以本探针走 Hermes 自己的装载路径（``PluginManager.discover_and_load``）把
``~/.hermes/plugins/wechatpadpro`` 装进**真 registry**，再用**真的**
``registry.dispatch()`` 调 —— 与线上模型点工具时走的**同一条路**（``model_tools`` →
``registry.dispatch`` → ``_kwargs_accepted_by`` → ``entry.handler(args, **kw)``）。

## 三类红（都曾是静默的）

1. ``Tool execution failed: TypeError: 'dict' object is not callable`` /
   ``takes 0 positional arguments but 1 was given`` —— 契约错（handler 收不了位置 dict）。
2. ``adapter 未就绪`` —— getter 被同名参数默认值挡住，**一条厂商请求都不发**。
   （近亲：本探针是独立进程、没有 gateway 平台 ⇒ 不设 ``WPP_TOOLS_ONLY=1`` 时
   适配器根本不会被构造，全部工具回「适配器尚未构造」。那**不是**被测代码的问题，
   是探针自己少设了一个变量 —— 现在由本文件自动设上，堵掉这个误诊入口。）
3. 上下文键（``task_id``/``session_id``/``user_task``）被放行给工具 ⇒ 会被拼进厂商请求体
   （Go 静默忽略未知字段，所以无声无息）。

⚠️ 第 ③ 步的**期望值是两条**（W7-5 之后新增第二条）：不受运行期门禁管的工具回
「账号 nope 未连接」；受管的（10 个小微）在门禁那一层就被拒，回的是门禁那句
（含"按未启用处理"这个 fail-closed 标记）。两条都是**业务**回答，都不是上面三类红。

第 3 条在这里用**真 Hermes 的** ``_kwargs_accepted_by`` 直接验：它对适配后的 handler
必须**一个都不放行**。
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
from pathlib import Path

HERMES_AGENT = "/root/.hermes/hermes-agent"
ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path("/tmp/wpp-py2-verify")
for p in (HERMES_AGENT, str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# ★ 纯工具模式（2026-10-03 修）：本探针是**独立进程**，没有 gateway 平台 ⇒ 适配器从来
#   不会被构造 ⇒ ``_get_adapter()`` 抛 RuntimeError ⇒ **217 个工具全部**回
#   「适配器尚未构造」而不是那条业务回答（第一轮实测就是这个形状，看起来像"契约又坏了"，
#   其实是探针自己少设了一个环境变量 —— 那句报错文本里就写着要设它）。
#   设成 tools-only **正是本探针声明的契约**（"不连账号、不联网、不发消息"）：
#   ``adapter.py`` 在这个模式下构造一个**不连接**的适配器，工具于是能走到账号路由那一步。
os.environ.setdefault("WPP_TOOLS_ONLY", "1")

# 「这个工具受运行期门禁管吗」—— 受管的工具（W7-5：10 个小微 + 4 个朋友圈发布）
# 在陌生账号下回的是**门禁那句**，不是账号那句（见下面第 ③ 步的判据）。
from plugin.toolkit.policy import gate_for  # noqa: E402  （纯 stdlib 依赖，可裸导入）

#: 与 ``tools/probe_registration.py`` 的 ``EXPECTED_TOTAL`` 同源（对不上就说明部署树不同版本）。
EXPECTED_TOTAL = 299
TOOLSET = "wechatpadpro"
#: 撤注册（对齐 TS 裁决）—— 对 AI 来说"看不见"才算撤掉。
DENIED = {"wpp_fc_publish_raw", "wpp_send_app_msg"}
#: 补注册（TS 有、蓝本没有）。
ADDED = {"wpp_tool_set_proxy"}
#: 陌生账号名：通用 handler 会在**任何厂商调用之前**返回「账号 nope 未连接」。
UNKNOWN = "nope"
#: dispatch 固定注入的上下文键（``model_tools._execute_tool``）。
CTX = {"task_id": "probe-task", "session_id": "probe-session", "user_task": "探针，不是真消息"}

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, cond: object, detail: object = "") -> None:
    RESULTS.append((label, bool(cond), str(detail)))


def _props(schema: dict | None) -> dict:
    return ((schema or {}).get("properties") or {})


def _args_from_schema(schema: dict | None) -> dict:
    """按 schema 造参数（模型就是这么填的）；``account_id`` 钉成陌生账号。

    用 schema 而不是手写键名：schema 是模型唯一看得见的东西，"schema 写错"这类缺陷
    只有走过 schema 才拦得住。
    """
    dummy = {"string": "x", "integer": 1, "number": 1, "boolean": True,
             "array": [], "object": {}}
    args = {k: dummy.get((v or {}).get("type"), "x") for k, v in _props(schema).items()}
    if "account_id" in args:
        args["account_id"] = UNKNOWN
    return args


# ───────────────────────────────────────────── ① 真装载（Hermes 自己的路）
from hermes_cli.plugins import get_plugin_manager          # noqa: E402
from tools.registry import _kwargs_accepted_by, registry   # noqa: E402

get_plugin_manager().discover_and_load()
names = registry.get_tool_names_for_toolset(TOOLSET)

check(f"插件在**真 registry** 里注册了 {EXPECTED_TOTAL} 个工具",
      len(names) == EXPECTED_TOTAL,
      f"实际 {len(names)} —— 插件没启用（先跑 `hermes plugins doctor ~/.hermes/plugins/wechatpadpro`）"
      f"或部署树版本不同")
if not names:
    print("  ❌ 插件一个工具都没注册上，后面的派发无从谈起（先修这个）")
    for label, cond, detail in RESULTS:
        print(f"  {'✅' if cond else '❌'} {label}" + (f"  ← {detail}" if detail and not cond else ""))
    sys.exit(1)

check("撤注册的两个不在 registry 里（AI 根本看不到 = 撤掉）",
      not (DENIED & set(names)), sorted(DENIED & set(names)))
check("补注册的 setProxy 在 registry 里", ADDED <= set(names), sorted(ADDED - set(names)))


# ──────────────────────────────── ② 形状（含不声明 **kwargs 这条硬约束）
def _shape_bad(name: str) -> str:
    entry = registry.get_entry(name)
    if entry is None:
        return "registry 里取不到"
    sig = inspect.signature(entry.handler).parameters
    ps = list(sig.values())
    if not ps:
        return "handler 无参数"
    first = ps[0]
    if first.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD:
        return f"首参 kind={first.kind.name}（{first.name}）"
    if first.name not in {"args", "params", "arguments"}:
        return f"首参名 {first.name!r}（像是被闭包默认值占了位）"
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in ps):
        return "声明了 **kwargs（dispatch 上下文键会被全部放行）"
    if bool(entry.is_async) != inspect.iscoroutinefunction(entry.handler):
        return f"is_async={entry.is_async} 与真实形状不符"
    return ""


bad_shape = {n: _shape_bad(n) for n in names}
bad_shape = {n: why for n, why in bad_shape.items() if why}
check("每个 handler 的首参都能接住位置 dict、不声明 **kwargs、is_async 与真身一致",
      not bad_shape, f"{len(bad_shape)} 个不符：{list(bad_shape.items())[:3]}")

# 【第 3 类红】用**真 Hermes 的过滤函数**验：上下文键一个都不许放行。
leaks = sorted(n for n in names if _kwargs_accepted_by(registry.get_entry(n).handler, dict(CTX)))
check("★ 真 Hermes 的 _kwargs_accepted_by 对适配后的 handler **一个上下文键都不放行**",
      not leaks, f"{len(leaks)} 个会收到上下文键（task_id/session_id/user_task 会被拼进厂商请求体）：{leaks[:5]}")


# ───────────── ③ 真派发：**通用端点表**（它们带 account_id 路由 ⇒ 陌生账号在任何
#                厂商调用**之前**就返回 ⇒ 全程零外呼、零 DB、零消息）
#
# 其余独有能力（wecom / phoneerp / extras / mcp / jargon）**故意不派发**：它们不按账号
# 路由，会去跑真实副作用（拉企微 CLI、查库、甚至搜消息）。它们的形状由第 ② 步覆盖 ——
# 那一层已经能拦住"接不住位置 dict"这类问题。
blueprint: set[str] | None
try:
    vendor = importlib.import_module("plugin.toolkit.vendor_defs")
    vendor_x = importlib.import_module("plugin.toolkit.vendor_defs_extra")
    blueprint = {d["name"] for _g, defs in [*vendor.GROUPS, *vendor_x.GROUPS] for d in defs}
except Exception as exc:  # noqa: BLE001
    blueprint = None
    check("能从部署树加载冻结蓝本（判定哪些是通用端点工具）", False, f"{type(exc).__name__}: {exc}")

if blueprint is not None:
    generic = sorted(blueprint & set(names))
    # 蓝本 219 条 → 撤注册 3 ⇒ **216**（2026-10-18 增加 ``wpp_fc_download_video``：
    # 它是 ``/Tools/DownloadVideo`` 的幽灵重复项，见 policy.DENY_TOOLS）。
    # ⚠️ 这里**不含**补注册的 35 个（``ADDED_GROUP``）：它们**不在蓝本里**，
    #    所以永远不会出现在 ``blueprint & names`` 这个交集里。
    check("通用端点工具数量为 216（蓝本 219 − 撤注册 3；补注册组不在蓝本里）",
          len(generic) == 216, f"实际 {len(generic)}")

    failures: list[str] = []
    notstr: list[str] = []
    gated_n = 0
    for name in generic:
        try:
            out = registry.dispatch(name, _args_from_schema(registry.get_schema(name)), **CTX)
        except Exception as exc:  # noqa: BLE001 —— dispatch 本该把异常兜成 tool_error
            failures.append(f"{name}: 抛了 {type(exc).__name__}: {exc}")
            continue
        if not isinstance(out, str):
            notstr.append(f"{name}: 返回 {type(out).__name__}")
            continue
        # ★ 这一条同时钉住两个缺陷：契约错 ⇒ 这里是 "[TOOL_ERROR] ... TypeError"；
        #   getter 被挡 ⇒ 这里是 "adapter 未就绪"。只有**两个都修好**才会是业务回答。
        #
        # ⚠️ W7-5 之后有**两条**合法业务回答，不是一个：运行期门禁排在工具体**之前**
        #    （``wrap_ctx`` 那一层），受管工具在陌生账号下说的是门禁那句。
        #    第一轮实测：10 个小微工具全部回门禁那句 ⇒ 看起来像"契约又坏了"，其实是
        #    这份期望值停在了 W7-5 之前。判据按**受不受管**分两路，仍然严格：
        #    受管 ⇒ 必须是**门禁的那句原话**（含"按未启用处理"这个 fail-closed 标记）；
        #    不受管 ⇒ 必须逐字等于「账号 nope 未连接」。
        gate = gate_for(name)
        if gate is None:
            if out != f"账号 {UNKNOWN} 未连接":
                failures.append(f"{name}: {out[:120]}")
        else:
            gated_n += 1
            if not out.startswith(gate.message) or "按未启用处理" not in out:
                failures.append(f"{name}: 受门禁管却回了 {out[:120]}")

    check(f"★ {len(generic)} 个通用工具**真派发**都拿到业务回答"
          f"（不受管的逐字「账号 {UNKNOWN} 未连接」，受管的回门禁那句 —— 都不是 TypeError / "
          f"「adapter 未就绪」）",
          not failures and not notstr,
          f"{len(failures) + len(notstr)} 个不对：{(failures + notstr)[:5]}")

    # 反向判据：这两支**各自**都不许空转 —— 否则"分两路"这件事本身没被验到。
    check("派发确实发生了（不是空集空转）", bool(generic))
    check("两条路都真的有样本（受管 / 不受管各 ≥1）", 0 < gated_n < len(generic),
          f"受管 {gated_n} / 共 {len(generic)}")


# ─────────────────────────────────────────────────────────── ④ 输出
ok_n = sum(1 for _, c, _ in RESULTS if c)
for label, cond, detail in RESULTS:
    mark = "✅" if cond else "❌"
    print(f"  {mark} {label}" + (f"\n       ← {detail}" if detail and not cond else ""))
print(f"\n{ok_n}/{len(RESULTS)} 通过")
sys.exit(0 if ok_n == len(RESULTS) else 1)
