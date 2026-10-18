"""心流回合的 ``reply_expected=False`` 接线（2026-10-18 生产群事故）。

## 事故形态

心流（未 @ 的群消息）放行后，模型先用发送工具把回复发进群，再按"不要重复发"
输出 ``[SILENT]``。网关的默认语义是：一条**冲机器人来的**消息若只回静默标记，
就替换成 ``⚠️ The model returned only a silence marker...`` 兜底文案 ——
于是群里多出一条英文内部提示（移动业务对接群 21354014635，18:01）。

Hermes 为"这条消息不是冲着机器人来的"留了 ``MessageEvent.reply_expected=False``：
``gateway/response_filters.silence_allowed`` 允许它静默。心流主动搭话正是这种
回合；@ 提及与私聊仍传 ``None``（未知 ⇒ 保留可见兜底，不能把真人提问吞掉）。

``adapter.py`` 本机 import 不了（模块级 ``from gateway.config import Platform``），
判据按 AST 源码形状下（与 ``test_affection_wiring`` 同一套工具）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"


def _src() -> str:
    return (ROOT / ADAPTER_REL).read_text(encoding="utf-8")


def _methods() -> dict[str, ast.AST]:
    for node in ast.parse(_src()).body:
        if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLS:
            return {n.name: n for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    raise AssertionError(f"{ADAPTER_REL} 里找不到 class {ADAPTER_CLS}")


def _code(node: ast.AST) -> str:
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


def test_build_event_accepts_and_forwards_reply_expected():
    """``_build_event`` 必须收下 ``reply_expected`` 并原样交给 ``MessageEvent``。"""
    build = _methods()["_build_event"]
    args = [a.arg for a in build.args.kwonlyargs]
    assert "reply_expected" in args, f"_build_event 没接 reply_expected：{args}"

    calls = [n for n in ast.walk(build)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "MessageEvent"]
    assert len(calls) == 1, f"MessageEvent 构造点不止一个/找不到：{len(calls)}"
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kw.get("reply_expected") == "reply_expected", \
        f"reply_expected 没接进 MessageEvent：{kw.get('reply_expected')!r}"


def test_heartflow_turn_marks_itself_before_build_event():
    """心流分支置 ``hf_engaged = True``，且 ``_build_event`` 调用按它取值。

    顺序也要对：标记必须出现在调用**之前**（写在后面就是每轮都 False）。
    """
    src = _code(_methods()["_handle_one"])
    assert "hf_engaged = False" in src, "没有初值 ⇒ 复用上一轮变量 / NameError"
    assert "hf_engaged = True" in src, "心流放行后没标记"

    call = src[src.index("_build_event("):]
    assert "reply_expected=False if hf_engaged else None" in call[:400], \
        f"_build_event 没按心流标记传 reply_expected：{call[:240]}"


def test_only_the_heartflow_branch_sets_the_flag():
    """@ 提及 / 私聊分支不许置 True（否则真人提问的静默会被合法吞掉）。"""
    src = _code(_methods()["_handle_one"])
    assert src.count("hf_engaged = True") == 1, "置 True 的地方不止/不到一处"
    i_flag = src.index("hf_engaged = True")
    i_not_ok = src.index("if not ok:")
    i_else = src.index("else:", i_not_ok)
    assert i_not_ok < i_flag < i_else, \
        "hf_engaged 不在 `if not ok:`（心流）分支里 —— reply_expected 会误传 False"
