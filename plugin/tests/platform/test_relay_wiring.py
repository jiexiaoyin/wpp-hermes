"""W7-3 装配回归：接龙改写**接在哪儿、钥匙和锁是不是同一把、正文还会不会被重复打标记**。

## 为什么这一面单独一套门

``plugin/tests/test_relay.py`` 验的是**解析本身**（切分、编号、渲染形状、永不抛）。
这一面验的是**适配器有没有把它接上、接在哪一步、与 ``_media_tag`` 是不是同一套口径** ——
``adapter.py`` 本机 **import 不了**（模块级 ``from gateway.config import Platform`` 是 Hermes
独有）⇒ 只能按 **AST 源码形状**验。

四条判据挑的都是**失效形态为"看着完全正常"** 的那种：

1. **④d 的位置** —— 排到 ④z（落库）之前 ⇒ **DB 里存的是改写后的 ``[接龙] title``**，
   TS 存的是原始 ``#接龙 …``；排到 ④a/④b（心流缓冲 / 好感度）**之后** ⇒ 那两条旁路
   看到的是原始那坨，TS 看到的却是改写后的（它把 relay 改写排在 ``recordRawMessage``
   与 affection 之前，见 ``handler.ts`` 539→541→570→589→620）；排到 ④e/⑤ 之后 ⇒
   judge 与 LLM **看不到条目**（等于这一项白做）。三种错法都**不报错**，现象只是
   "库里的接龙长得不一样"或"模型还是自己数人"。
2. **抑制键与写入键必须是同一个字符串** —— 解析侧写 ``relay_rendered``、管线侧读
   ``relay_render``（少一个 ``ed``），两处各自单测都绿，真机上正文变成
   ``[接龙] [接龙] title …``。
3. **抑制必须排在 49 细分之前** —— 排在后面会让"接龙"那一条分支先返回 ``[接龙]``
   （W7-2 的教训：顺序错了，测试还全绿）。
4. **传给 ``apply_relay_render`` 的必须是消息对象** —— 传 ``msg.content``（一个 ``str``）
   会被 ``is_relay_message`` 的宽判据拒掉、被 ``apply_relay_render`` 的宽兜底吞掉，
   返回 ``False`` 而**不留任何痕迹**。

⚠️ 两条 AST 上的注意（与 ``test_media_enrich_wiring.py`` 同一族）：``ast.walk`` 是
**广度优先、不是源码顺序** ⇒ 判"谁在谁前面"一律用 ``lineno``；``ast.unparse`` 把下标里的
字符串印成**单引号** ⇒ 期望值别写成双引号。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"
PARSER_REL = "plugin/inbound/parser.py"
RELAY_REL = "plugin/inbound/relay.py"

#: 标记键 —— 解析侧写、管线侧读的**同一个**字符串（这条判据存在的理由见 docstring ②）
MARK_KEY = "relay_rendered"


# ============================================================ 源码工具（AST）


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _methods(rel: str = ADAPTER_REL, cls: str = ADAPTER_CLS) -> dict[str, ast.AST]:
    for node in ast.parse(_src(rel)).body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {n.name: n for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    raise AssertionError(f"{rel} 里找不到 class {cls}")


def _nested(rel: str, outer: str, inner: str) -> ast.AST:
    """``<outer>.<inner>``（``InboundPipeline._media_tag``）。"""
    for node in ast.parse(_src(rel)).body:
        if isinstance(node, ast.ClassDef) and node.name == outer:
            for n in node.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == inner:
                    return n
    raise AssertionError(f"{rel} 里找不到 {outer}.{inner}")


def _fn(rel: str, name: str) -> ast.AST:
    for node in ast.parse(_src(rel)).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{rel} 里找不到 {name}")


def _body(fn: ast.AST) -> list[ast.stmt]:
    """去掉 docstring 之后的语句表（``ast.walk`` 会把 docstring 也算进去）。"""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    """子树里所有 ``name(...)`` 调用（方法 ``self._hf_note_group`` 与模块级
    ``persist_inbound``/``apply_relay_render`` 两种写法都要认）。"""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if (isinstance(f, ast.Attribute) and f.attr == name) \
                or (isinstance(f, ast.Name) and f.id == name):
            out.append(n)
            continue
        # 2026-10-18 起同步落库挪进了线程池：``asyncio.to_thread(fn, ...)`` 也
        # 算一次 ``fn`` 调用，否则位置守卫会看不见落库点。
        fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if fname == "to_thread" and n.args:
            a0 = n.args[0]
            if (isinstance(a0, ast.Attribute) and a0.attr == name) \
                    or (isinstance(a0, ast.Name) and a0.id == name):
                out.append(n)
    return out


def _line(calls: list[ast.Call]) -> int:
    assert calls, "一个调用点都没有"
    return min(c.lineno for c in calls)


def _if(node: ast.AST, test: str) -> ast.If:
    """条件**逐字**等于 ``test`` 的那个 ``if``。"""
    for n in ast.walk(node):
        if isinstance(n, ast.If) and ast.unparse(n.test) == test:
            return n
    raise AssertionError(f"找不到 `if {test}:`")


# ============================================================ W1 ④d 的位置（三前两后）


def test_the_rewrite_sits_after_persist_and_before_every_bypass() -> None:
    """★ 改写必须排在 ④z（落库）**之后**、④a/④b（心流缓冲 / 好感度）与 ④e（judge）、
    ⑤（构造事件）**之前** —— 位置判据逐条对齐 TS ``handler.ts`` 的批内先后
    （539 落库 → 541 改写 → 570 心流 → 589 黑话 → 620 好感度 → dispatch）。

    ⚠️ 这条断言在 2026-10-02 做 W7-4 排位时**翻过一次**：首版把「心流/好感度」也写成
    ``>``（即改写排在那两条旁路之后），依据是"TS 的池来自 DB 行"—— **那句是错的**，
    TS 的 ``recordRawMessage`` 写的是进程内缓冲、拿的是改写后的 ``m.content``。
    ⇒ 现在钉的是 ``<``：下游旁路看到的必须是**改写后**的结构。
    """
    one = _methods()["_handle_one"]
    i_render = _line(_calls(one, "apply_relay_render"))

    assert i_render > _line(_calls(one, "persist_inbound")), \
        "改写必须在落库之后 —— 否则 DB 里存的不是 TS 那份原始 #接龙 正文"
    assert i_render < _line(_calls(one, "_hf_note_group")), \
        "改写必须在心流缓冲之前 —— TS 的池是进程内缓冲，写的是改写后的正文"
    assert i_render < _line(_calls(one, "_affection_note_group")), \
        "改写必须在好感度旁路之前（TS 的 AFFECTION 排在 relay 之后）"
    assert i_render < _line(_calls(one, "_try_heartflow")), \
        "改写必须在心流 judge 之前 —— judge 要看到编号条目，不然 v1.11.0 看图那类判断全落空"
    assert i_render < _line(_calls(one, "_build_event")), \
        "改写必须在构造事件之前 —— 否则喂给 LLM 的还是那坨原始正文"


def test_the_rewrite_never_runs_twice() -> None:
    """一处一行。两次改写会把 ``[接龙]`` 叠成两层（第二次解析的是已经改写过的正文）。"""
    calls = _calls(_methods()["_handle_one"], "apply_relay_render")
    assert len(calls) == 1, f"_handle_one 里 apply_relay_render 出现 {len(calls)} 次"


def test_the_message_object_is_what_gets_rewritten() -> None:
    """★ 必须传**消息对象**。传 ``msg.content``（``str``）会被两层的宽兜底吞掉：
    ``is_relay_message`` 拒掉它（没有 ``msg_type``）、``apply_relay_render`` 的 ``except``
    再兜一层 ⇒ 返回 ``False``、日志干净、正文一个字没变。"""
    call = _calls(_methods()["_handle_one"], "apply_relay_render")[0]
    assert ast.unparse(call.args[0]) == "msg", ast.unparse(call)


# ============================================================ W2 钥匙与锁是同一把


def test_the_suppression_key_matches_the_written_key() -> None:
    """★ 解析侧写的键与管线侧读的键必须是**同一个字符串**。

    写成 ``relay_render`` / ``relayRendered`` 这种差一个字母的版本，两边的单测都绿，
    而真机上正文变成 ``[接龙] [接龙] #接龙 …`` —— 跨模块的串，只有把两边的 AST 抠出来
    对一下才看得见。"""
    written = {ast.unparse(t.slice) for node in ast.walk(_fn(RELAY_REL, "apply_relay_render"))
               if isinstance(node, ast.Assign) for t in node.targets
               if isinstance(t, ast.Subscript) and ast.unparse(t.value) == "msg.media"}
    assert written == {f"'{MARK_KEY}'"}, f"relay.py 写的键是 {written}"

    read = {ast.unparse(c.args[0]) for c in _calls(_nested(PARSER_REL, "InboundPipeline",
                                                          "_media_tag"), "get")
            if c.args and ast.unparse(c.func) == "msg.media.get"}
    assert f"'{MARK_KEY}'" in read, f"_media_tag 读的键是 {read}"


# ============================================================ W3 两道抑制的位置


def test_both_suppressions_come_first_and_before_the_app_subdivision() -> None:
    """★ 两道抑制（``oss_url`` / ``relay_rendered``）必须是**头两条**语句，
    且都排在 49 细分（``if msg.msg_type != MT_APP``）**之前**。

    排在细分之后 ⇒ 接龙会先走到 ``if "接龙" in text: return "[接龙]"`` 那一条
    （W7-2 的教训：顺序错了、测试还全绿），正文里就多一个标记。"""
    body = _body(_nested(PARSER_REL, "InboundPipeline", "_media_tag"))
    guards = []
    for stmt in body:
        if not isinstance(stmt, ast.If):
            break
        guards.append(ast.unparse(stmt.test))
    # ``ast.unparse`` 把空串印成 ``''``（单引号）—— 期望值跟着它写，别写成双引号
    assert guards[:2] == ["msg.media.get('oss_url')", f"msg.media.get('{MARK_KEY}')"], guards

    subdivision = _if(_nested(PARSER_REL, "InboundPipeline", "_media_tag"),
                      "msg.msg_type != MT_APP").lineno
    assert max(g.lineno for g in body[:2]) < subdivision, "两道抑制必须排在 49 细分之前"


def test_both_suppressions_return_nothing_so_no_marker_is_prefixed() -> None:
    """抑制的返回值必须是**空**（不是 ``None``、更不是某种"占位标记"）——
    ``prepare_text`` 的 ``if tag:`` 对空串不动作；返回 ``None`` 会拼出 "None 正文"。"""
    body = _body(_nested(PARSER_REL, "InboundPipeline", "_media_tag"))
    for stmt in body[:2]:
        assert isinstance(stmt, ast.If)
        assert ast.unparse(stmt.body[0]) == "return ''", ast.unparse(stmt)


# ============================================================ W4 复用而不是重写


def test_relay_reuses_the_parser_helpers_instead_of_reimplementing_them() -> None:
    """``strip_group_prefix`` / ``MT_APP`` 必须**从 parser 借**，不许在本模块里再写一份。

    重写一份就是第二个真相源：两边的 ``_SENDER_PREFIX_RE`` 一旦不同步，
    现象只是"有些接龙的标题多带个 ``wxid_xxx:``"。"""
    imported = set()
    for node in ast.walk(ast.parse(_src(RELAY_REL))):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("parser"):
            imported |= {a.name for a in node.names}
    assert {"MT_APP", "strip_group_prefix"} <= imported, imported

    defined = {n.name for n in ast.parse(_src(RELAY_REL)).body
               if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert "strip_group_prefix" not in defined, "relay.py 里不该有第二份 strip_group_prefix"


def test_the_adapter_imports_the_rewriter_from_the_relay_module() -> None:
    """装配点必须是 ``..inbound.relay.apply_relay_render`` —— 断掉这条 import
    就会 NameError（好在会响）；**更坏的是**从别处 import 一个同名函数 ⇒ 看起来干净、
    行为是另一套。"""
    tree = ast.parse(_src(ADAPTER_REL))
    hit = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
           and (n.module or "").endswith("inbound.relay")
           and any(a.name == "apply_relay_render" for a in n.names)]
    assert hit, f"{ADAPTER_REL} 没有从 ..inbound.relay 导入 apply_relay_render"


def test_the_rewriter_never_raises() -> None:
    """TS 那个 ``try/catch`` 的对等物：``apply_relay_render`` 里必须有一个
    ``except Exception`` 兜底 —— 接龙解析坏了不该让整条消息从入站链路上消失。"""
    handlers = [h for n in ast.walk(_fn(RELAY_REL, "apply_relay_render"))
                if isinstance(n, ast.Try) for h in n.handlers]
    assert handlers, "apply_relay_render 没有 try/except"
    assert any(isinstance(h.type, ast.Name) and h.type.id == "Exception" for h in handlers
               if h.type is not None), "兜底的 except 必须是 Exception（宽兜底）"
