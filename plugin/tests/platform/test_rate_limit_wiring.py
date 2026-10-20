"""R1 装配回归：限速闸**接得对不对**（2026-10-03）。

## 为什么这一面单独一套门

``plugin/tests/test_rate_limit.py`` 验的是闸本体（上限 / 静默 / 滑窗 / 归因 / 出错放行），
这一面验的是**它有没有被接上、接在哪一步**。``plugin/platform/adapter.py`` 本机
**import 不了**（模块级 ``from gateway.config import Platform`` 是 Hermes 独有，见
``test_outbound_send_result.py:33-35``）⇒ 只能按 **AST 源码形状**验。

## 这一面要抓的失效形态，全都是"看着完全正常"

1. **闸建在``_handle_one`` 里**（每条消息 new 一个）：状态永远只有这一次计数 ⇒
   **一次都拦不住**，而且没有任何异常、没有日志、内存也不涨 —— 装了个空转的闸。
2. **闸建在 ``connect()`` 里**：重连一次就把额度清空 ⇒ 回路只要撑过一次重连就自由了。
3. **判定点排得太晚**（在 ④z 落库 / ④a 心流缓冲 / ④c 媒体富化之后）：被掐的那几条
   照样进台账、进裁判的现场记录、照样下载上传媒体 —— 闸拦住了回复，却拦不住污染与花费。
4. **判了不丢**（只 log 不 return）：日志里 WARNING 一行行地打，回路照样刷。
5. **WARNING 写成 debug/info**：``真机`` 的 journald 只采 WARNING+ ⇒ 现场看不见
   （这次事故拖了 4 分钟才发现，根因之一就是"看不见"）。
6. **参数在适配器里又写一遍**：两份阈值迟早不一致，而失效方向是"闸永远拦不住"。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"


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
    """去 docstring 后的源码（docstring 里全是反例说明，搜不得）。"""
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


def _calls(node: ast.AST, full_name: str) -> list[ast.Call]:
    """按源码顺序找 ``full_name(...)``（``self.x.y`` 这种也认）。

    ⚠️ 不用 ``ast.walk`` 的顺序做生意 —— 它是广度的；一律按 ``lineno`` 排。
    """
    hits = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        if ast.unparse(n.func) == full_name:
            hits.append(n)
        elif n.args and ast.unparse(n.func) in ("asyncio.to_thread", "to_thread") \
                and ast.unparse(n.args[0]) == full_name:
            # 2026-10-18 起同步落库挪进了线程池：``to_thread(fn, ...)`` 也
            # 算一次 ``fn`` 调用，否则位置守卫会看不见落库点。
            hits.append(n)
    return sorted(hits, key=lambda n: n.lineno)


def _one_call(node: ast.AST, full_name: str) -> ast.Call:
    hits = _calls(node, full_name)
    assert len(hits) == 1, f"{full_name} 应当恰好 1 处，实际 {len(hits)} 处"
    return hits[0]


# ============================================================ §1 闸的建造点

def test_c1_exactly_one_limiter_is_built_for_the_whole_process():
    """★ **进程级一个**，建在 ``__init__`` 里（不是每条消息建、也不是每账号建）。

    这条是这一面最要紧的判据：搬到 ``_handle_one`` 里 ⇒ 状态只有本次计数 ⇒
    闸**一次都拦不住**，而现场没有任何异常、日志、内存变化 —— 一个空转的闸。
    """
    hits = _calls(_adapter_tree(), "TriggerRateLimiter")
    assert len(hits) == 1, f"限速闸构造点应当恰好 1 处，实际 {len(hits)}"
    init = _methods()["__init__"]
    assert hits[0].lineno >= init.lineno, "构造点不在 __init__ 里"
    assert "self._trigger_limiter = TriggerRateLimiter()" in _code(init), \
        "闸必须挂在实例上（挂局部变量 ⇒ 下一条消息就没了）"


def test_c2_the_limiter_is_not_rebuilt_on_reconnect():
    """``connect()`` / ``disconnect()`` / ``reload_account()`` **都不许**碰它。

    重连清空额度 = 回路只要撑过一次重连就自由了；而现场看这件事是"重置了一下，
    看着挺正常"。
    """
    for name in ("connect", "disconnect", "reload_account", "_on_raw", "_handle_one"):
        code = _code(_methods()[name])
        assert "TriggerRateLimiter(" not in code, f"{name} 里在重建限速闸"
        assert "_trigger_limiter = " not in code, f"{name} 里在重置限速闸"


# ============================================================ §2 判定点

def test_c3_the_guard_runs_once_per_message_with_the_right_key():
    """判定的 key 必须是 ``(账号, 群, 发送者, reason)`` —— 少一个维度就是"张三刷屏李四被封"。

    ⚠️ 群 id 用 ``msg.peer_id``：群里前者有值，而 vendor 偶尔只给
    ``peer_id``；写成裸 ``msg.peer_id`` 在多账号/群聊混排时会与别的会话撞桶。
    """
    call = _one_call(_methods()["_handle_one"], "self._trigger_limiter.guard")
    args = [ast.unparse(a) for a in call.args]
    assert args == ["acct.id", "msg.peer_id", "msg.from_wxid", "reason"], args


def test_c4_the_guard_sits_between_the_drop_gate_and_everything_else():
    """★ **位置**：``should_drop`` 之后、④c/④z/④a 与心流/派发**之前**。

    排到后面去的后果（每一条都不会报错）：
    * ④c 之后 ⇒ 被掐的消息照样下载 + 上传一次媒体（白花钱，百万 token 级的浪费）；
    * ④z 之后 ⇒ 对刷的消息照样进台账（事故里就是 41 行）；
    * ④a 之后 ⇒ 照样进心流原始缓冲 ⇒ **裁判的"最近 N 条"里塞满机器人自己的对刷**；
    * ``_try_heartflow`` 之后 ⇒ 心流已经替它回过了，闸拦了个寂寞。
    """
    fn = _methods()["_handle_one"]
    guard = _one_call(fn, "self._trigger_limiter.guard")
    after = _one_call(fn, "should_drop")
    for later in ("self._fetch_media", "persist_inbound", "self._hf_note_group",
                  "self._affection_note_group", "self._jargon_note_group",
                  "self._try_heartflow", "self.handle_message"):
        hits = _calls(fn, later)
        assert hits, f"{later} 不在 _handle_one 里了（改名了？）"
        assert guard.lineno < hits[0].lineno, \
            f"限速闸排在 {later} 之后 ⇒ 拦住了回复却拦不住污染/花费"


# ============================================================ §3 判了要丢

def test_c5_a_refused_message_is_dropped_with_a_warning():
    """判了必须**丢**（``return``），且必须打 **WARNING**。

    ``真机`` 的 ``journalctl`` 只采 WARNING+（INFO 只在 ``~/.hermes/logs/gateway.log``
    那个文件里）⇒ 写 debug 就等于"出事时现场什么也看不见"。只 log 不 return 更坏：
    日志里一行行 WARNING，回路照样刷。
    """
    fn = _methods()["_handle_one"]
    guard = _one_call(fn, "self._trigger_limiter.guard")
    branches = [n for n in ast.walk(fn)
                if isinstance(n, ast.If) and ".allowed" in ast.unparse(n.test)
                and n.lineno >= guard.lineno]
    assert branches, "找不到「判定结果」那道 if"
    body = branches[0].body
    assert any(isinstance(s, ast.Return) for s in body), "判了不丢 ⇒ 闸等于没有"
    assert any(isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
               and ast.unparse(s.value.func) in ("log.warning", "log.error")
               for s in body), "被掐必须打 WARNING（journald 只采 WARNING+）"


def test_c6_the_thresholds_are_not_duplicated_in_the_adapter():
    """阈值只有一个家（``inbound/rate_limit.py``）：适配器里不许出现那几个常量名。

    两份阈值迟早不一致，而失效方向是安静的 —— 闸按 A 判、日志按 B 说。
    """
    src = _src(ADAPTER_REL)
    for name in ("WINDOW_S", "MAX_TRIGGERS", "MUTE_S", "GUARDED_REASONS"):
        assert name not in src, f"{name} 在适配器里被重新写了一遍 ⇒ 阈值分叉"


def test_c7_the_guarded_reason_set_is_owned_by_the_module():
    """适配器**不**判"哪些 reason 受管" —— 一律交给闸自己判（单一真源）。

    判据：``_handle_one`` 里不许出现那两个 reason 字符串（只在
    ``inbound/rate_limit.py`` 的 :data:`GUARDED_REASONS` 里出现）。
    """
    code = _code(_methods()["_handle_one"])
    for reason in ("group_mention", "group_solitaire"):
        assert f'"{reason}"' not in code and f"'{reason}'" not in code, \
            f"{reason} 在适配器里被重新判了一遍 ⇒ 受管集合分叉"
