"""W7-6 装配回归：到达层聚合**接得对不对**（2026-10-02）。

## 为什么这一面单独一套门

``plugin/tests/test_debounce.py`` 验的是聚合器本体（合并 / 窗口重置 / 保序 / 不丢 / 只 warn），
这一面验的是**它有没有被接上、接得对不对**。``plugin/platform/adapter.py`` 本机
**import 不了**（模块级 ``from gateway.config import Platform`` 是 Hermes 独有，见
``test_outbound_send_result.py:33-35``）⇒ 只能按 **AST 源码形状**验。

## 这一面要抓的失效形态，全都是"看着完全正常"

1. **聚合器成了消息的终点**：``enqueue`` 返 ``False``（没循环 / 已停机）却没人管 ⇒
   那一条永远躺在缓冲里。现场只有"这条没人回"，日志一行都不出。
2. **多账号共用一个聚合器**：``on_flush`` 里把账号 id 写成闭包默认参数以外的形状
   （``lambda batch: self._flush_inbound(acct.id, batch)`` —— 循环变量晚绑定）⇒
   所有账号的消息都按**最后一个**账号派发。多账号环境直接串号。
3. **窗口写死**：``interval_ms`` 用字面量而不是 ``acct.debounce_ms`` ⇒ 账号文件里那个键
   又变回死键（正是本项要消灭的"配置在、实现不在"）。
4. **停机不 flush（或排完队就走）**：缓冲区里那批消息随进程消失。
5. **flush 时"只取最后一条"**：那样触发语义就变成批式的了 —— 本项**明确不做**那个
   （见 ``inbound/debounce`` 的模块 docstring）。
6. **窗口逻辑在适配器里又写一遍**：两份判断迟早不一致，而这条链的失效是安静的。
"""

from __future__ import annotations

import ast
from pathlib import Path

from plugin.inbound.debounce import PASSTHROUGH_TYPES

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
DEBOUNCE_REL = "plugin/inbound/debounce.py"
ADAPTER_CLS = "WppAdapter"


# ============================================================ 源码工具（AST）

def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _adapter_tree() -> ast.Module:
    return ast.parse(_src(ADAPTER_REL))


def _cls_node(cls: str = ADAPTER_CLS) -> ast.ClassDef:
    for node in _adapter_tree().body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return node
    raise AssertionError(f"{ADAPTER_REL} 里找不到 class {cls}")


def _methods(cls: str = ADAPTER_CLS) -> dict[str, ast.AST]:
    return {n.name: n for n in _cls_node(cls).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _code(node: ast.AST) -> str:
    """去 docstring 后的源码（docstring 里写满了反例，搜不得 —— 同 ``test_sync_wiring``）。"""
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


def _fn(name: str) -> ast.AST:
    m = _methods()
    assert name in m, f"{ADAPTER_CLS}.{name} 不在了 —— 改名了还是被删了？"
    return m[name]


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    """按源码顺序找 ``name(...)``（**不许**用 ``ast.walk`` 的顺序做生意 —— 它是广度的）。"""
    hits = [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == name]
    return sorted(hits, key=lambda n: n.lineno)


# ============================================================ §1 connect() 建造

def test_c1_one_debouncer_per_account_built_where_the_pipeline_is():
    """★ 聚合器与管线**同一个账号、同一个循环**里建。

    判据用"那个 ``for acct in connectable`` 的 body 里同时有 ``InboundPipeline(`` 与
    ``InboundDebouncer(``" —— 分开建的话，两个循环之间那个窗口里消息会走"有管线没聚合器"
    那条直通路（不至于丢，但会绕开本项要的能力，且没人看得出来）。
    """
    fn = _fn("connect")
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)
             and "connectable" in ast.unparse(n.iter)]
    assert loops, "connect() 里找不到 `for acct in connectable`"
    holders = [n for n in loops if "InboundDebouncer(" in ast.unparse(n)]
    assert len(holders) == 1, f"聚合器的建造点应当恰好 1 处，实际 {len(holders)}"
    body = ast.unparse(holders[0])
    assert "InboundPipeline(" in body, "聚合器与管线不在同一个循环里"
    # 每账号一个（键是 acct.id），不是建一个全局的
    assert "self._debounce[acct.id] = InboundDebouncer(" in _code(fn), \
        "没按账号登记 ⇒ 要么共用一个、要么 disconnect 停不到它"


def test_c2_the_window_comes_from_the_account_file_not_a_literal():
    """★ ``interval_ms`` 必须是 ``acct.debounce_ms`` —— 写死就让账号文件那个键又变成死键。"""
    fn = _fn("connect")
    calls = _calls(fn, "InboundDebouncer")
    assert len(calls) == 1, f"InboundDebouncer 构造点应当只有 1 处，实际 {len(calls)}"
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kw.get("interval_ms") == "acct.debounce_ms", \
        f"窗口必须来自账号配置：{kw.get('interval_ms')}"
    assert ast.unparse(calls[0].args[0]) == "acct.id", \
        f"第一个位置参数应当是账号 id：{ast.unparse(calls[0].args[0])}"


def test_c3_the_flush_callback_pins_the_account_id_and_never_touches_verbatim_msgid():
    """``on_flush`` 必须把**本账号**的 id 钉进闭包（默认参数），并转交 ``_flush_inbound``。

    ⚠️ 晚绑定的坏法（``lambda batch: self._flush_inbound(acct.id, batch)``）在多账号下
    会把所有账号的批都按**最后一个**账号派发 —— 而单账号测试全绿。
    """
    fn = _fn("connect")
    call = _calls(fn, "InboundDebouncer")[0]
    # ⚠️ 回调是**第二个位置参数**（签名 ``(account_id, on_flush, *, …)``）—— 第一版按
    #    关键字找 ``kw["on_flush"]``，拿到的 `KeyError` 长得像"没接回调"，其实只是位置不同。
    assert len(call.args) == 2, f"位置参数应当是 (账号, 回调)，实际 {len(call.args)} 个"
    cb = ast.unparse(call.args[1])
    assert "a=acct.id" in cb, f"账号 id 没被钉进闭包（晚绑定 ⇒ 多账号串号）：{cb}"
    assert "_flush_inbound(" in cb, f"必须转交 _flush_inbound：{cb}"


def test_c4_the_control_hook_is_wired_to_the_filehelper_session():
    """``is_control_command`` 接 ``is_filehelper_session``（py2 唯一的控制面通道）。

    ⚠️ 这是**有意**偏离 TS 的一处（TS 声明了钩子却没人传）—— 见 ``inbound/debounce``
    的「与 TS 有意不齐」。所以判据不是"必须有"，而是"必须**就是它**"：
    传别的谓词等于悄悄改掉"哪些消息不排队"。
    """
    fn = _fn("connect")
    kw = {k.arg: ast.unparse(k.value) for k in _calls(fn, "InboundDebouncer")[0].keywords}
    assert kw.get("is_control_command") == "is_filehelper_session", \
        f"控制面钩子接错了：{kw.get('is_control_command')}"


# ============================================================ §2 _on_raw 入口

def test_r1_the_ingress_enqueues_and_falls_back_to_direct_dispatch():
    """★ ``enqueue`` 返 ``False`` 的那条路**必须真的派发**（不是"记个日志"）。

    坏法有两族，都安静：① ``if db is None or not db.enqueue(msg):`` 整块被写成
    ``if False``（消息只进缓冲，停机/无循环时永远不出来）；② 守卫在、body 里
    ``pass``（等于上一条）。
    """
    fn = _fn("_on_raw")
    guards = [n for n in ast.walk(fn) if isinstance(n, ast.If)
              and "db.enqueue(msg)" in ast.unparse(n.test)]
    assert len(guards) == 1, f"找不到「没接手就直通」的那道守卫：{len(guards)}"
    guard = guards[0]
    assert "db is None" in ast.unparse(guard.test), \
        f"守卫漏了「拿不到聚合器」这一支：{ast.unparse(guard.test)}"
    body = [ast.unparse(s) for s in guard.body]
    assert body and body[-1].startswith("await self._dispatch_one(acct, msg, pipeline)"), \
        f"守卫里没有真的派发（那一条就永远没人送）：{body}"
    # 直通与 flush 走**同一个出口**（单条的 try/except 只写一遍）
    assert "_handle_one(" not in _code(fn), "_on_raw 里直接调 _handle_one 了 —— 绕过 _dispatch_one"


def test_r2_flush_dispatches_every_message_not_just_the_last():
    """★ ``_flush_inbound`` **逐条**派发（批式语义明确不在本项范围）。

    坏法：``for msg in batch:`` 改成 ``for msg in batch[-1:]`` 或只调一次 ——
    那等于把"合并"偷偷升级成"只看最后一条"，触发语义整个变了。
    """
    fn = _fn("_flush_inbound")
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    assert len(loops) == 1, f"找不到那条逐条派发的循环：{len(loops)}"
    assert ast.unparse(loops[0].iter) == "batch", \
        f"循环必须遍历整批：{ast.unparse(loops[0].iter)}"
    assert "await self._dispatch_one(acct, msg, pipeline)" in "\n".join(
        ast.unparse(s) for s in loops[0].body), "循环体里没有派发那一条"


def test_r3_flush_reads_the_latest_account_and_pipeline_and_shouts_when_there_is_none():
    """派发前**现读** ``self._accounts`` / ``self._pipelines``（不是构造期快照）。

    与 W6 那条同源：账号配置写回后 ``self._accounts`` 会被换掉，读旧快照等于命令改过的
    白名单对这批消息不生效。另一个方向：停机窗口里管线已被清 ⇒ **必须吵**
    （安静地丢掉一批是这个模块最坏的失效形态）。
    """
    src = _code(_fn("_flush_inbound"))
    assert "self._accounts.get(account_id)" in src, "没现读账号（拿的是构造期快照）"
    assert "self._pipelines.get(account_id)" in src, "没现读管线"
    assert "log.warning" in src, "无处派发时必须出声（不许安静丢掉一整批）"
    assert "return" in src, "无处派发时要早返回（别拿着 None 往下走）"


def test_r4_one_bad_message_does_not_kill_the_batch():
    """单条的 try/except 收在 ``_dispatch_one`` 里 —— 一批 N 条坏一条只丢那一条。"""
    src = _code(_fn("_dispatch_one"))
    assert "_handle_one(" in src and "try:" in src and "except Exception" in src, \
        "直通那条没有单条保护（坏消息会把整批带崩）"
    assert "log.warning" in src, "吞了异常却不留痕"
    assert "raise" not in src, "单条失败不许往上抛"


# ============================================================ §3 停机 / 热加载

def test_d1_disconnect_flushes_before_tearing_anything_down():
    """★ 停机顺序：**先 flush 聚合器，再**停 WS / 关 client / 清管线。

    ``disconnect`` 的每一步都在拆这批消息要用的东西（client / 管线 / 心流）——
    顺序反了就是停机丢一批，且现场只表现为"有些人说话机器人没理"。
    """
    src = _code(_fn("disconnect"))
    assert "db.stop()" in src, "停机没 flush 聚合器 ⇒ 缓冲区里那批随进程消失"
    assert "self._debounce.clear()" in src, "停完要清登记"
    assert "self._debounce.items()" in src, "停的是个空集合（要真遍历 `list(...items())`）"
    # 遍历必须走 `list(...)`（改 dict 的时候不能边遍历边改）
    assert "list(self._debounce.items())" in src
    assert src.index("self._debounce") < src.index("await ws.stop()"), \
        "聚合器要早于 WS 停（它是 WS/webhook 的下游）"
    assert src.index("self._debounce") < src.index("await client.close()"), \
        "聚合器要早于关 client（批次里的媒体还得靠它下载）"
    assert src.index("self._debounce") < src.index("self._pipelines.clear()"), \
        "聚合器要早于清管线（派发要用 pipeline）"


def test_d2_reload_refreshes_the_window_in_place_and_does_not_rebuild():
    """★ 命令写回后热生效：``interval_ms`` 就地改，**不重建**聚合器。

    重建会把已经缓冲的那批丢掉 —— 那是**真丢消息**（不是"晚一点到"）。
    """
    src = _code(_fn("reload_account"))
    assert "db.interval_ms = " in src, "debounceMs 改了不生效（又成了一个死键）"
    assert "acct.debounce_ms" in src, "窗口要跟着新解析出来的账号走"
    assert "InboundDebouncer(" not in src, \
        "重载时重建了聚合器 ⇒ 已缓冲的那批被丢掉（真丢消息）"
    # 拿不到聚合器（账号没连）时不许炸
    assert "if db is not None" in src


# ============================================================ §4 单一真源 / 不变量

def test_s1_the_adapter_does_not_reimplement_the_window():
    """窗口语义**只许**在 ``inbound/debounce.py`` 里 —— 适配器不许有第二份。

    两份判断迟早不一致，而这条链的失效是安静的（"消息晚了几秒"没人会当成 bug 报）。
    """
    whole = _src(ADAPTER_REL)
    # ⚠️ 判据只取**窗口逻辑特有的**指纹。``_buffers`` / ``_timers`` 这类**不能**用：
    #    本文件里已有 ``heartflow._raw_buffers``（注释）与 ``self._sweep_timers``（W5 的
    #    定时器列表）⇒ 子串命中，判据会变成噪声（第一版就是这么写坏的）。
    for needle in ("call_later", "TimerHandle", "PASSTHROUGH_TYPES",
                   "DEFAULT_DEBOUNCE_MS", "debounce_key"):
        assert needle not in whole, \
            f"适配器里出现了聚合器的内部细节 {needle} —— 那是在这里又写了一遍"
    # 从聚合器模块**只许**拿那一个公开类（连私有名都不许进）
    imports = [n for n in ast.walk(_adapter_tree()) if isinstance(n, ast.ImportFrom)
               and (n.module or "").endswith("inbound.debounce")]
    assert len(imports) == 1 and [a.name for a in imports[0].names] == ["InboundDebouncer"], \
        "从 inbound.debounce 导入的不止 InboundDebouncer —— 内部细节被搬进来了"


def test_s2_the_invariant_marker_lives_in_the_module():
    """不变量标记留在**源码的家**里（W1 ``inbound/persist.py``、W5 ``core/sync_fallback.py``、
    W6 ``core/account_store.py``、W7-6 ``inbound/debounce.py``）—— 改到这一带的人必须先看到四条。"""
    src = _src(DEBOUNCE_REL)
    assert "INVARIANT: W7-6" in src, "invariant 标记不见了"
    for phrase in ("一条都不许丢", "同 key 严格保序", "直通三类逐字照抄", "flush 失败只 warn"):
        assert phrase in src, f"四条判据少了一条：{phrase}"
    assert "与 TS 的三处有意不齐" in src, "那三处有意不齐的理由必须写在源码里"
    assert "保守移植" in src, "「只做到达层聚合、不做批式处理管线」这条边界必须写在源码里"
    assert PASSTHROUGH_TYPES == frozenset({10000, 34, 10002}), "直通三类被改了"


def test_s3_the_adapter_does_not_mark_the_message_as_buffered():
    """反向判据：``_on_raw`` 里**不许**把"已入队"当成"已处理"再自己收尾 ——
    （比如顺手把 msg 从 payload 里 pop 掉、或再调一次 ``_handle_one``）。
    判据：``_on_raw`` 里既没有 ``_handle_one(`` 也没有 ``continue``/``break``
    这种"循环里另起一条路"的痕迹。"""
    src = _code(_fn("_on_raw"))
    assert "_handle_one(" not in src
    assert "continue" not in src and "break" not in src, \
        "循环里出现了 continue/break —— 消息会绕过聚合器或绕过直通"
