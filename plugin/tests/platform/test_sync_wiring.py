"""W5 装配回归：增量兜底**接得对不对**（2026-10-02）。

## 为什么这一面单独一套门

``plugin/tests/test_sync_fallback.py`` 验的是兜底本体（游标顺序 / in-flight 锁 / 首次
不回放 / 心跳守卫），这一面验的是**它有没有被接上、接得对不对**。``adapter.py`` 本机
**import 不了**（模块级 ``from gateway.config import Platform`` 是 Hermes 独有，
见 ``test_outbound_send_result.py:33-35``）⇒ 适配器与 WS 客户端那两半只能按
**AST 源码形状**验；``api_client`` 那半真跑（本机可导入）。

## 这一面要抓的失效形态，全都是"看着完全正常"

1. **接错通道**：``REASON_OPEN`` 与 WS 那边实际 emit 的字符串不一致 ⇒ 每次"连上"都被
   当成 push ⇒ **AutoHeartBeat 永远不在重连时重开**。现场没有任何异常，只是"跑一阵
   就掉线、重连后更容易掉"（TS v1.9.3-fix 的根因就是这类静默失效）。
2. **回调里 await**：``on_event`` 在 WS 读循环里被调用；在那里 ``await`` 一趟 HTTP
   拉取会把**收帧**一起堵住 —— 快群能把读循环压成"一条一拉"。⇒ 判据下在
   "**只许 spawn、源码里一个 ``await ...emit/on_event`` 都没有**"上。
3. **钩子把连接带崩**：钩子是**旁路**，它自己炸了不许影响收帧。⇒ ``_emit`` 必须自带
   try/except + ``on_event is None`` 早返回。
4. **把 ``connection_ready`` 也当"有新东西"** ⇒ 每次握手 ack 都白打一次厂商。
5. **兜底挂在 WS 成功之后** ⇒ WS 起不来的账号**正好**没有兜底可用（那是它最有用的时候）。
6. **``disconnect()`` 忘了停它** ⇒ 两个定时器继续打已关闭的 client（刷错误日志、
   拖住关闭），且重连后**新旧两个游标互相踩**。
7. **``is_connected`` 被写成一个捕获下来的 bool** ⇒ 断线之后心跳照发（刷日志）或
   连上了却不发（漏保活）；必须每次**现读** WS 对象。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from plugin.core import outbound_guard
from plugin.core.api_client import WppClient
from plugin.core.sync_fallback import REASON_OPEN, REASON_PUSH, REASON_TIMER

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
WS_REL = "plugin/platform/inbound_ws.py"
FALLBACK_REL = "plugin/core/sync_fallback.py"
ADAPTER_CLS = "WppAdapter"


# ============================================================ 源码工具（AST）

def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _cls_node(cls: str = ADAPTER_CLS) -> ast.ClassDef:
    for node in ast.parse(_src(ADAPTER_REL)).body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return node
    raise AssertionError(f"{ADAPTER_REL} 里找不到 class {cls}")


def _methods(cls: str = ADAPTER_CLS) -> dict[str, ast.AST]:
    return {n.name: n for n in _cls_node(cls).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _code(node: ast.AST) -> str:
    """去 docstring 后的源码（docstring 里写满了反例，搜不得 —— 同 ``test_intent_wiring``）。"""
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


def _fn(name: str) -> ast.AST:
    m = _methods()
    assert name in m, f"{ADAPTER_CLS}.{name} 不在了 —— 改名了还是被删了？"
    return m[name]


def _emits(rel: str) -> list[str]:
    """``inbound_ws`` 里所有 ``self._emit("<字面量>")`` 的参数，**按源码顺序**。

    ⚠️ 不能用 ``ast.walk`` 的顺序 —— 它是**广度优先**，拿到的顺序与源码顺序不一定一致
    （这条判据的意义就在"谁先谁后"，顺序错了等于没判）。
    """
    hits: list[tuple[int, str]] = []
    for n in ast.walk(ast.parse(_src(rel))):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_emit" and len(n.args) == 1
                and isinstance(n.args[0], ast.Constant)):
            hits.append((n.lineno, n.args[0].value))
    return [v for _ln, v in sorted(hits)]


# ============================================================ §1 WS 客户端

def test_w1_the_two_event_reasons_match_the_fallback_constants():
    """★ 两边字符串必须**逐字对齐**（``REASON_OPEN`` ↔ ``ws-open``）。

    对不上的后果是**静默**的：``_on_ws_event`` 把"连上"当成 push ⇒ 不再重开
    ``/Login/AutoHeartBeat`` ⇒ 新 ws 会话没告知厂商 ⇒ 跑一阵就掉线、且日志一句错都没有
    （TS v1.9.3-fix 的根因就是这类"看起来只是网络不稳"的失效）。
    """
    reasons = _emits(WS_REL)
    assert reasons == [REASON_OPEN, "sync-update"], \
        f"inbound_ws emit 的事件必须正好是这两条（顺序也别动）：{reasons}"
    # 与常量交叉验证：改任何一边的字符串这个断言就红
    assert REASON_OPEN == "ws-open"
    assert REASON_PUSH == "ws-push" and REASON_TIMER == "fallback-timer"


def test_w2_connection_ready_does_not_trigger_a_pull():
    """``connection_ready`` 是**握手 ack** ⇒ 不 emit（开连那一脚已经拉过一趟了）。

    坏法：把它与 ``sync_update`` 一视同仁 ⇒ 每次握手都白打一次 ``/Msg/Sync``。
    """
    ws_cls = next(n for n in ast.parse(_src(WS_REL)).body
                  if isinstance(n, ast.ClassDef) and n.name == "WppWsClient")
    disp = next(n for n in ws_cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_dispatch")
    # 那个 if 的判据必须是"等于 sync_update"（不是"是这两种之一"）
    tests = [ast.unparse(n.test) for n in ast.walk(disp) if isinstance(n, ast.If)]
    # ⚠️ ``ast.unparse`` 的字符串字面量是**单引号**（`== 'sync_update'`），别按双引号找
    assert any("== 'sync_update'" in t for t in tests), \
        f"_dispatch 里没有「等于 sync_update」的判据（拿 _IGNORED_TYPES 整体判会连握手 ack 一起触发）：{tests}"


def test_w3_the_sync_branch_is_inside_the_ignored_shapes_and_still_drops_the_frame():
    """W5 那条分支**只加在"本来就丢帧"的分支里** —— WS 帧主通道一个字节都没动。

    两条判据：① emit 点必须在 ``data.get("type") in _IGNORED_TYPES`` 的 ``if`` 里面；
    ② 那条分支**仍然 return**（帧本身不派发 —— 它没有消息体，硬塞给 ``on_raw`` 只会
    产生一条解析失败的告警）。
    """
    ws_cls = next(n for n in ast.parse(_src(WS_REL)).body
                  if isinstance(n, ast.ClassDef) and n.name == "WppWsClient")
    disp = next(n for n in ws_cls.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_dispatch")
    outer = [n for n in ast.walk(disp) if isinstance(n, ast.If)
             and "_IGNORED_TYPES" in ast.unparse(n.test)]
    assert len(outer) == 1, "找不到那个 `in _IGNORED_TYPES` 的守卫"
    body = "\n".join(ast.unparse(s) for s in outer[0].body)
    assert "_emit(" in body, "emit 点不在「本来就丢帧」的那个分支里（帧主通道被动过了？）"
    assert "return" in body, "这一支必须照样 return（心跳帧没有消息体，不许派发）"
    # 主通道：非心跳帧照旧走 on_raw（没被这次改动动过）
    assert "await self.on_raw(payload)" in _code(disp)


def test_w4_the_hook_is_a_non_blocking_bypass():
    """★ 钩子**不许被 await**、也不许能把连接带崩。

    ①②：``_run`` 里的 emit 与 ``_dispatch`` 里的 emit 都必须是**裸调用**（前面没有 await）
    —— 那里是 WS 读循环，await 一趟 HTTP 拉取会把收帧一起堵住。
    ③：``_emit`` 自带 ``on_event is None`` 早返回 + try/except + ``log.warning``。
    """
    # ① / ②：全模块不许出现 `await self._emit(` / `await ...on_event(`
    whole = _src(WS_REL)
    for bad in ("await self._emit(", "await self.on_event("):
        assert bad not in whole, f"钩子被 await 了（会堵住 WS 读循环）：{bad}"

    emits = [n for n in ast.walk(ast.parse(whole))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_emit"]
    assert len(emits) == 2, f"emit 点应当只有 2 处（开连 + 通知帧），实际 {len(emits)}"

    # ③ 旁路：钩子抛错只 warn
    ws_cls = next(n for n in ast.parse(whole).body
                  if isinstance(n, ast.ClassDef) and n.name == "WppWsClient")
    emit_fn = next(n for n in ws_cls.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_emit")
    code = _code(emit_fn)
    assert "return" in code and "on_event is None" in code, "没钩子时要早返回"
    assert "try:" in code and "except Exception" in code, "钩子自己炸了会打断收帧"
    assert "log.warning" in code, "钩子失败要出声"
    assert "raise" not in code


def test_w5_the_connected_flag_follows_the_socket():
    """★ ``self.connected`` 是**心跳那道守卫的唯一输入** ⇒ 它必须真的跟着连接走。

    三处缺一处就有一类静默失效：

    * 连上时不置 True ⇒ 心跳**永远不发**（漏保活 ⇒ vendor idle 超时断连）；
    * 断开时（``finally``）不置 False ⇒ 断线后照样发 HTTP 心跳（刷错误日志），
      而 ``finally`` 是**唯一**能覆盖"异常退出/被取消"的出口 —— 放外面漏一半；
    * ``stop()`` 时不置 False ⇒ 停机过程中还在发心跳（那正是 disconnect 要停的事）。
    """
    ws_cls = next(n for n in ast.parse(_src(WS_REL)).body
                  if isinstance(n, ast.ClassDef) and n.name == "WppWsClient")
    m = {n.name: n for n in ws_cls.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    run_src = _code(m["_run"])
    assert "self.connected = True" in run_src, "连上时没置 connected ⇒ 心跳永远不发"
    # 置 True 必须在 emit 之前（emit 那一刻兜底就会去问 is_connected）
    # ⚠️ ``_code`` 是 ``ast.unparse`` 出来的 ⇒ 字面量是**单引号**，别按双引号找
    assert run_src.index("self.connected = True") < run_src.index("_emit("), \
        "connected 要在 emit 之前置好（否则开连那一脚的心跳守卫拿到的是 False）"
    assert "finally:" in run_src and "self.connected = False" in run_src, \
        "断开必须落在 finally 里（放外面覆盖不到异常退出/被取消）"
    fin = [n for n in ast.walk(m["_run"]) if isinstance(n, ast.Try) and n.finalbody]
    assert any("self.connected = False" in "\n".join(ast.unparse(s) for s in t.finalbody)
               for t in fin), "self.connected = False 不在任何 finally 里"

    assert "self.connected = False" in _code(m["stop"]), \
        "stop() 要清连接态（否则停机过程中还在发心跳）"
    assert "self.connected = False" in _code(m["__init__"]), \
        "初值必须是 False（fail-closed：没连上就不发心跳）"


def test_w6_the_native_ws_ping_is_still_on():
    """★ ``ping_interval`` 是**保活主力**，不许关掉（TS v1.9.3-fix 的根因结论）。

    vendor 的 idle timeout 挂在 ``/ws/sync`` 长连接**本身**：HTTP 心跳不产生 ws 流量，
    所以只靠 ``/Login/HeartBeatLong`` 照样每 ~10 分钟被 code=1005 断连 —— 而那种失效
    在日志里只是"又断了"，看不出跟这里有关。``websockets.connect(ping_interval=…)``
    是 py2 这侧**结构性**的原生 ping（TS 需要自己 ``ws.ping()``）。
    """
    whole = _src(WS_REL)
    kw = {k.arg: ast.unparse(k.value) for n in ast.walk(ast.parse(whole))
          if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "connect"
          for k in n.keywords}
    assert "ping_interval" in kw, "原生 ping 被去掉了 ⇒ 只靠 HTTP 心跳，会被 vendor 按 idle 断掉"
    assert kw["ping_interval"] not in ("None", "0"), f"ping_interval 被关了：{kw['ping_interval']}"


# ============================================================ §2 适配器装配

def test_a1_the_fallback_is_built_outside_the_ws_try_and_not_gated_on_it():
    """★ 兜底**不许挂在 WS 的成败上**：WS 挂了的时候正是它最有用的时候。

    两种写法都会让"WS 起不来 ⇒ 兜底一起没了"，而它们都**看着很像对的**：

    * 把 ``SyncFallback(...)`` 塞进 ``WppWsClient`` 那个 ``try`` 里；
    * 在它的 ``except`` 里 ``return``（"WS 失败了就先回去吧"）。

    ⇒ 判据用 AST 定位那个 ``try``，然后要求：①它里面**不许出现** ``SyncFallback(``；
    ②它的 handler 里**一个 return 都没有**。光比"两处字符串的先后"是抓不到第二种的
    （``except`` 的文本照样在 ``SyncFallback(`` 之前）。
    """
    fn = _fn("_start_ws")
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)
             and "WppWsClient(" in ast.unparse(n)]
    assert len(tries) == 1, f"找不到包住 WppWsClient(...) 的那个 try：{len(tries)}"
    ws_try = tries[0]
    inside = "\n".join(ast.unparse(s) for s in ws_try.body + ws_try.handlers
                       + ws_try.orelse + ws_try.finalbody)
    assert "SyncFallback(" not in inside, \
        "兜底被塞进 WS 的 try 里了 —— WS 起不来时它会一起消失（那正是最需要它的时候）"
    assert not [n for h in ws_try.handlers for n in ast.walk(h) if isinstance(n, ast.Return)], \
        "WS 的 except 里 return 了 —— 等于「WS 失败就别起兜底」，与塞进 try 里同罪"
    # ⚠️ 只查 handler 里有没有 return 还不够：把 return 写在 **except 之后、同一层**
    #    （`else:` 块的 try/except 后面）照样是「WS 失败就先回去」—— 它不在 handler
    #    的语法子树里，AST 扫 handler 扫不到。⇒ 连"包住这个 try 的那一层语句块"
    #    一起查：try 之后同一层不许再出现 return。
    block = None
    for node in ast.walk(fn):
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if isinstance(stmts, list) and ws_try in stmts:
                block = stmts
                break
        if block is not None:
            break
    assert block is not None, "找不到包住 WppWsClient try 的那层语句块"
    tail = block[block.index(ws_try) + 1:]
    assert not [n for s in tail for n in ast.walk(s) if isinstance(n, ast.Return)], \
        "WS 的 try 之后同一层 return 了 —— 等于「WS 失败就别起兜底」（兜底恰好这时最有用）"

    src = _code(fn)
    assert "sync.start()" in src, "建了没 start = 定时器一个都没起"
    assert "self._sync[acct.id] = sync" in src, "没登记 ⇒ disconnect 停不到它（孤儿定时器）"


def test_a2_the_fallback_dispatch_and_connection_state_are_wired_correctly():
    """三根线接对：``on_message`` / ``is_connected`` / 那个 api 是**该账号的 client**。

    * ``on_message`` 必须是 ``_on_sync_raw``（**不是** ``_on_raw``）：拉回来的条目与 WS
      帧同形，去重仍靠 msgId；但兜底回调返回后 ``SyncFallback`` 就推进游标，而
      ``_on_raw`` 只入聚合器就返回 ⇒ 聚合窗口内进程退出会永久丢这批消息。
    * ``is_connected`` 必须**现读** ``ws.connected``（存成 bool 会让断线后照样发心跳）。
    ``is_connected=lambda: ws.connected`` 这种写法必须是**默认参数闭包**
    （``w=ws``），否则循环里所有账号会共用最后一个 ws。
    """
    fn = _fn("_start_ws")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "SyncFallback"]
    assert len(calls) == 1, f"SyncFallback 构造点应当只有 1 处，实际 {len(calls)}"
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}

    assert "on_message" in kw, "拉回来的条目没人派发"
    assert "_on_sync_raw(" in kw["on_message"], \
        f"on_message 必须走「处理完才返回」的同步入口：{kw['on_message']}"
    assert "_on_raw(" not in kw["on_message"].replace("_on_sync_raw(", ""), \
        f"不能退回只入聚合器的 _on_raw：{kw['on_message']}"
    assert "item" in kw["on_message"], f"派发的必须是拉回来的那一条：{kw['on_message']}"

    assert "is_connected" in kw, "心跳没接连接态 ⇒ 断线后照样发 HTTP 心跳"
    # unparse 后是 `lambda w=ws: bool(w is not None and w.connected)` ⇒ 读的是 **w**
    assert ".connected" in kw["is_connected"], \
        f"is_connected 要现读 WS 对象的状态：{kw['is_connected']}"
    assert "w=ws" in kw["is_connected"], \
        f"必须用默认参数把 ws 钉住（否则多账号会共用最后一个）：{kw['is_connected']}"

    # 第一个位置参数是账号 id，第二个是那个账号的 client（不是默认账号、不是全局）
    assert ast.unparse(calls[0].args[0]) == "acct.id"
    assert ast.unparse(calls[0].args[1]) == "client", \
        f"第二个位置参数必须是本账号的 vendor client：{ast.unparse(calls[0].args[1])}"


def test_a2b_sync_dispatch_waits_for_the_handler_instead_of_the_debouncer():
    """★ 兜底派发必须**直接 await ``_dispatch_one``**，不能只 ``enqueue``。

    ``SyncFallback.trigger`` 拿到 ``on_message`` 返回就推进游标；聚合器 ``enqueue``
    只是放进 500ms 缓冲。若这里走 ``_on_raw``，停机/崩溃窗口内那批消息游标已过、
    却从未落库 ⇒ 永久丢失。判据：源码里有 ``_dispatch_one(``、没有 ``enqueue(``。
    """
    fn = _fn("_on_sync_raw")
    src = _code(fn)
    assert "_dispatch_one(" in src, "兜底派发没有走到处理出口"
    assert "enqueue(" not in src, \
        "兜底派发又走聚合器了 —— 游标会早于下游处理推进（停机丢消息）"
    assert "await self._dispatch_one(" in src, \
        "必须 await 处理完成再返回，否则 SyncFallback 会提前推进游标"


def test_a3_the_ws_event_hook_is_spawn_only_and_maps_open_to_reason_open():
    """``_on_ws_event`` 只许 ``spawn``（等价 TS 的 ``void this.triggerSync(...)``）。

    判据：①它里面**没有** ``await ...trigger(``；②``REASON_OPEN`` 那个判断真的在
    （连上要重开 AutoHeartBeat）；③``ws-open`` 走 ``REASON_OPEN``、其余走 ``REASON_PUSH``
    —— 两条分支都要在，缺一条就等于把某类事件静默吞掉。
    """
    fn = _fn("_on_ws_event")
    src = _code(fn)
    assert ".spawn(" in src, "触发必须走 spawn（非阻塞）"
    assert "await" not in src, f"钩子在 WS 读循环里被调用，一个 await 都不许有：{src}"
    assert ".trigger(" not in src, "不许直接调 trigger（那是 await 的接口，会堵住读循环）"
    assert "REASON_OPEN" in src and "REASON_PUSH" in src, \
        "两条 reason 分支都要在（缺一条 = 某类事件被静默当另一类处理）"
    assert "reason == REASON_OPEN" in src, "判据必须是'等于 ws-open'（连上才重开自动心跳）"
    # 拿不到兜底对象时安静返回（WS 起得来但兜底没起来的情形）
    assert "if sync is None" in src, "找不到兜底时要早返回（别抛）"


def test_a4_the_duplicate_connect_guard_exists_and_actually_returns():
    """重复 ``connect()`` 不许在同一账号上挂**两个**兜底。

    两个 ``SyncFallback`` = 两条 ``/Msg/Sync`` 轮询 + 两个游标互相踩 ⇒ **静默丢消息**
    —— 正是 in-flight 锁要防的那件事，只是发生在实例之间（锁只管得住自己那一个）。

    ⚠️ 光有 ``if acct.id in self._sync:`` 是**装饰**：body 里不 ``return`` 就照样往下
    建第二个。⇒ 判据是"那个 if 的**最后一句是 return**"。
    """
    fn = _fn("_start_ws")
    guard = [n for n in ast.walk(fn) if isinstance(n, ast.If)
             and ast.unparse(n.test) == "acct.id in self._sync"]
    assert guard, "没有「已存在就别再起一个」的闸"
    body = [ast.unparse(s) for s in guard[0].body]
    assert body[-1] == "return", f"那个闸的 body 末尾必须是 return（否则是装饰）：{body}"
    assert any("log.warning" in b for b in body), "走到这条分支说明调用方漏了 disconnect，要吵"


def test_a5_disconnect_stops_the_fallback_before_closing_the_clients():
    """``disconnect()`` 必须停它，且**早于**关 client。

    它的两个定时器（拉取 / HTTP 心跳）都在打厂商：client 关了之后它们只会刷错误日志，
    还可能拖住关闭；漏停则重连后新旧两个游标互相踩。
    """
    src = _code(_fn("disconnect"))
    assert "self._sync" in src, "disconnect 没停兜底 ⇒ 孤儿定时器"
    assert "await sync.stop()" in src, "要真的 stop（cancel + await），不是 clear 一下就完"
    assert "self._sync.clear()" in src, "停完要清登记"
    # 循环必须真遍历 `_sync`（写成 `for ... in []` 这种"停了个空集合"照样有上面那些字符串）
    assert "self._sync.items()" in src, "停的是个空集合（`list(self._sync.items())` 才是真遍历）"
    assert src.index("self._sync") < src.index("self._clients"), \
        "兜底必须在关 client **之前**停（顺序反了 = 定时器打已关闭的 client）"
    assert src.index("self._sync") < src.index("await ws.stop()"), \
        "兜底要早于 WS 停（它是 WS 的下游）"


def test_a8_the_ws_client_actually_receives_the_event_hook():
    """★ ``WppWsClient(...)`` 必须**真的**收到 ``on_event=`` —— 那条 push 触发链的接口。

    漏了它，兜底就只剩 60s 定时器一个触发源："收到通知帧就拉一趟"整条路径不存在，
    而**所有其它判据照样全绿**（``_on_ws_event`` 还在、emit 点还在、字符串也对得上 ——
    只是没人把这两头接起来）。这正是"看着完全正常"的那类失效。
    """
    fn = _fn("_start_ws")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "WppWsClient"]
    assert len(calls) == 1, f"WppWsClient 构造点应当只有 1 处，实际 {len(calls)}"
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert "on_event" in kw, "WS 客户端没拿到事件钩子 ⇒ push 触发链整条不存在"
    assert "self._on_ws_event(a, reason)" in kw["on_event"], \
        f"on_event 必须把 reason **原样透传**给那个只 spawn 的钩子：{kw['on_event']}"


def test_a6_the_outbound_block_reads_the_latest_account_and_delegates():
    """兜底只**接上**，不重新实现"首次不回放 / 游标顺序"那套判断（唯一家在 sync_fallback）。

    ⇒ 适配器里不许出现 ``synckey``/``KeyBuf``/``CmdList`` 这类协议细节的字面量：
    出现即意味着第二份解析（两份迟早不一致，而这条链的失效是静默的）。
    """
    whole = _src(ADAPTER_REL)
    for needle in ("KeyBuf", "CmdList", "Synckey"):
        assert f'"{needle}"' not in whole, \
            f"适配器里出现了协议细节 {needle} —— 解析只允许在 core/sync_fallback.py 里"


def test_a7_the_w5_invariant_is_marked_in_its_source_home():
    """不变量标记留在**源码的家**里（W1 → ``inbound/persist.py``、W6 → ``account_store.py``、
    W2 → ``inbound/affection.py``、W3 → ``inbound/intent.py``、W5 → ``core/sync_fallback.py``）
    —— 改到这一带的人**必须**先看到这四条。
    """
    src = _src(FALLBACK_REL)
    assert "INVARIANT: W5" in src, "invariant 标记不见了"
    for phrase in ("先派发、后前进游标", "只建立游标", "一次只拉一趟", "任何失败只 warn"):
        assert phrase in src, f"四条判据少了一条：{phrase}"
    assert "为什么首次不敢回放" in src, "那条「与 TS 有意不齐」的理由必须写在源码里"


# ============================================================ §3 api_client（真跑）

@pytest.fixture()
def captured(monkeypatch):
    """把 ``WppClient.call`` 换掉，只记录 (endpoint, body, method)。"""
    seen: list[tuple[str, dict | None, str]] = []

    async def _call(self, endpoint, body=None, method="POST", max_retries=2, *, audit=True):
        seen.append((endpoint, body, method))
        return {"Code": 0, "Success": True, "Data": {}}

    monkeypatch.setattr(WppClient, "call", _call)
    return seen


def _client() -> WppClient:
    return WppClient("https://wx.example.test", "FAKE-AUTHCODE")


def test_c1_sync_message_posts_the_vendor_sync_param_doc(captured):
    """★ ``/Msg/Sync`` 的请求体是 vendor 的 ``Msg.SyncParamDoc``：``{"Scene": 0, "Synckey": …}``。

    形状错的后果：vendor 忽略 ``Synckey`` ⇒ **每次都全量**（重放风暴），而且不会报错。
    """
    import asyncio

    c = _client()
    asyncio.run(c.sync_message("K1"))
    assert captured == [("/Msg/Sync", {"Scene": 0, "Synckey": "K1"}, "POST")]

    captured.clear()
    asyncio.run(c.sync_message())
    assert captured[0][1] == {"Scene": 0, "Synckey": ""}, \
        "缺省必须是空串（不是 None）—— vendor 那边是 Go 的 string 字段"

    # ⚠️ 显式传 None 这一条**不是凑数**：方法体里那道 ``or ""`` 闸，在"不传参"的调用下
    # 与"去掉闸"**完全同形**（形参默认值本来就是 `""`）⇒ 只有显式传假值才看得出来。
    # 而 `None` 正是本链路上的真实输入形状 —— ``_read_key()`` 找不到游标就返 `None`。
    # 今天 ``sync_fallback.trigger`` 替它补了 ``prev or ""``，所以这里钉的是**方法自己的**
    # 契约（"请求体里永远是字符串"），免得"谁补这道闸"变成第二个真相源。
    captured.clear()
    asyncio.run(c.sync_message(None))
    assert captured[0][1] == {"Scene": 0, "Synckey": ""}, \
        "游标是 None 时也必须发空串（JSON null 会被 vendor 的 Go string 字段拒掉）"


def test_c2_the_two_heartbeats_hit_the_right_endpoints(captured):
    """HTTP 心跳两条端点照 TS 接线：``/Login/HeartBeatLong`` 与 ``/Login/AutoHeartBeat``。"""
    import asyncio

    c = _client()
    asyncio.run(c.heartbeat_long())
    asyncio.run(c.auto_heartbeat())
    assert [e for e, _b, _m in captured] == ["/Login/HeartBeatLong", "/Login/AutoHeartBeat"]


@pytest.mark.parametrize("endpoint", ["/Msg/Sync", "/Login/HeartBeatLong", "/Login/AutoHeartBeat"])
def test_c3_the_new_endpoints_pass_the_outbound_guard(endpoint):
    """★ 三个新端点必须被判为**只读**。

    出站闸把非只读端点当"会发消息"的写操作处理 ⇒ 一旦被判成写，拉取与心跳会被闸门拦下，
    而现场表现是"兜底一封告警都不出、就是不生效"（这条链的失效形态本来就安静）。
    """
    assert outbound_guard.is_read_only(endpoint) is True, f"{endpoint} 被判成写操作了"
    outbound_guard.guard(endpoint, {}, "acct")   # 不许抛
