"""W7-2 装配回归：小程序卡片的富化**接在哪儿、键名对不对**（2026-10-02）。

## 为什么这一面单独一套门

``plugin/tests/test_app_card.py`` 验的是**纯逻辑**（识别、字段映射、文本化、下载降级）。
这一面验的是**适配器有没有把它接上、接在哪一步、两边读写的键是不是同一个** ——
``adapter.py`` 本机 **import 不了**（模块级 ``from gateway.config import Platform`` 是
Hermes 独有，见 ``test_outbound_send_result.py:33-35``）⇒ 只能按 **AST 源码形状**验。

四条判据挑的都是**失效形态为"看着完全正常"** 的那种：

1. **分支接在 ``if not msg.media`` 那道闸后面 ⇒ 整类消息根本走不到它**。
   小程序卡片的资产挂在 ``app.cover_image`` 上、**不在** ``msg.media`` 里（vendor 的
   媒体字段三代混用，封面算第四种）⇒ 那个 guard 会把 100% 的卡片挡回去，而现场表现
   只是"卡片没图"：没有异常、没有日志、模型那头只是少一行 ``[小程序封面]``。
   ⇒ H1/H2 判的是**源码顺序**（按下标比），不是"有没有这段代码"。
2. **键名两边不一致**（``mp_asset_url`` ↔ ``mpAssetUrl``）⇒ 富化明明成功，正文里
   一个字都不多。这类跨模块的约定**没有任何编译器/类型检查**会管 ⇒ H3 直接把
   写侧（adapter 的字符串常量）与读侧（``app_card`` 里 ``media.get(...)`` 的实参）
   对起来 —— 改一边就红。
3. **异常穿透** ⇒ 这条路径挂在收信链路上，一次 OSS 抖动应当只丢"图"，
   不该丢"消息"。⇒ H4 判"这段里一个 ``raise`` 都没有 + 写 media 只发生在
   ``if result.media_url:`` 的**正**支里 + 降级支只出声（一行 log）"。⚠️ 这条判据
   必须**逐字**比条件表达式：只查"条件里出现过 ``result.media_url``"的话，
   ``and False`` 和 ``not result.media_url``（写反）两个变异体都拦不住 —— 子串都命中。
   另加一条 H4b：没有 ``cover_image`` 的卡片**不该**去打厂商（事前不问，而不是事后诚实）。
4. **早退的元组形状被改坏**（``return [], []`` → ``return []``）⇒ 调用方
   ``media_urls, media_types = await self._fetch_media(...)`` 立刻 ``ValueError``。
   ⇒ H5。⚠️ 这一条不是假想的：写 W7-2 时我自己就把它改成了 ``return []``。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"
APP_CARD_REL = "plugin/inbound/app_card.py"

#: adapter 写、app_card 读的两个键（唯一真源写在 ``app_card.mini_program_block`` 的 docstring 里）
KEY_URL = "mp_asset_url"
KEY_KIND = "mp_asset_kind"


# ============================================================ 源码工具（AST）


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _methods(rel: str = ADAPTER_REL, cls: str = ADAPTER_CLS) -> dict[str, ast.AST]:
    for node in ast.parse(_src(rel)).body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {n.name: n for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    raise AssertionError(f"{rel} 里找不到 class {cls}")


def _code(node: ast.AST) -> str:
    """去 docstring 后的源码（docstring 里写满了反例，搜不得）。"""
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


def _fetch_media() -> ast.AST:
    return _methods()["_fetch_media"]


def _mini_branch() -> ast.If:
    """``_fetch_media`` 里那个"是不是小程序卡片"的 if（按条件里有没有 ``is_mini_program_card`` 找）。"""
    for n in ast.walk(_fetch_media()):
        if isinstance(n, ast.If) and "is_mini_program_card" in ast.unparse(n.test):
            return n
    raise AssertionError("`_fetch_media` 里找不到小程序卡片分支")


# ====================================================== H1 分支存在且条件够窄


def test_the_branch_requires_both_a_card_type_and_the_mini_program_category():
    """只判 ``msgType == 49`` 会把红包/接龙/文件也送去下载封面（每一条都白打一次厂商）。"""
    test = ast.unparse(_mini_branch().test)
    assert "MT_APP" in test, test
    assert "is_mini_program_card" in test, test


def test_the_branch_is_actually_reachable_not_a_dead_condition():
    """夹具：条件里不许有 `False`/`and 0` 这类把整支关掉的东西（"接线拆掉"的常见写法）。"""
    test = ast.unparse(_mini_branch().test)
    assert not test.startswith("False"), test
    assert "False and" not in test, test


# ====================================================== H2 顺序（本项的要害）


def test_the_branch_sits_before_the_empty_media_guard():
    """★ 小程序卡片的 ``msg.media`` 是空的 ⇒ 排在 guard 后面 = 整类消息走不到它。"""
    code = _code(_fetch_media())
    branch_at = code.index(_branch_head())
    guard_at = code.index("if not msg.media:")
    assert branch_at < guard_at, "小程序分支必须排在 `if not msg.media` 那道闸之前"


def _branch_head() -> str:
    """分支首行的去缩进源码（``_code`` 用 ``ast.unparse`` ⇒ 缩进是 4 空格）。"""
    return "if " + ast.unparse(_mini_branch().test) + ":"


# ====================================================== H3 跨模块键名契约


def test_the_keys_written_here_are_exactly_the_keys_read_there():
    """★ adapter 写的键 = ``app_card`` 读的键。改一边就红（没有任何别的东西管这件事）。"""
    written = {n.slice.value for n in ast.walk(_mini_branch())
               if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute)
               and n.value.attr == "media" and isinstance(n.slice, ast.Constant)}
    assert written == {KEY_URL, KEY_KIND}, written

    block = next(n for n in ast.parse(_src(APP_CARD_REL)).body
                 if isinstance(n, ast.FunctionDef) and n.name == "mini_program_block")
    read = {n.args[0].value for n in ast.walk(block)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get" and n.args
            and isinstance(n.args[0], ast.Constant) and str(n.args[0].value).startswith("mp_")}
    assert read == {KEY_URL, KEY_KIND}, read


def test_the_kind_decides_which_line_the_url_lands_on():
    """两个键是**一对**：只有 URL 没有 kind ⇒ 封面/图标谁都不标（宁可不给，别标错）。"""
    code = _src(APP_CARD_REL)
    assert 'cover_url=url if kind == "cover" else ""' in code
    assert 'icon_url=url if kind == "icon" else ""' in code


# ====================================================== H4 失败只出声不上抛


def test_the_branch_never_raises():
    branch = _mini_branch()
    assert not any(isinstance(n, ast.Raise) for n in ast.walk(branch))


def _media_writes(node: ast.AST) -> list[ast.Subscript]:
    """子树里所有 ``*.media[...]`` 的下标访问（写侧与读侧都长这样，调用方自己判）。"""
    return [n for n in ast.walk(node) if isinstance(n, ast.Subscript)
            and isinstance(n.value, ast.Attribute) and n.value.attr == "media"]


def _writing_guard(branch: ast.AST) -> ast.If:
    """「写 media 的那两句」所守着的那个 ``if``（**追父节点**，不是"包含这个词的 if 都算"）。

    后者会把外层那个 `if mtype == MT_APP and …` 也算进来，于是判据变成永远为真。
    """
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(branch):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    writes = _media_writes(branch)
    assert writes, "找不到写 media 的地方"
    guards = set()
    for write in writes:
        assign = parents[id(write)]
        assert isinstance(assign, ast.Assign), ast.unparse(write)
        guard = parents.get(id(assign))
        while guard is not None and not isinstance(guard, ast.If):
            guard = parents.get(id(guard))
        assert guard is not None, "写 media 的那句不在任何 if 里 —— 降级路径会污染 media"
        guards.add(id(guard))
    assert len(guards) == 1, "两句写 media 分属不同的 if"
    return next(g for g in ast.walk(branch) if isinstance(g, ast.If) and id(g) in guards)


def test_the_writes_are_guarded_by_the_url_exactly():
    """★ 判据下在"**哪个分支**写 media"上，而且**逐字**：

    - ``if result.media_url:`` 才对 —— ``and False``（拿了图也白拿）与
      ``if not result.media_url:``（把失败当成功、成功当失败）都必须在**写法上**被拦住；
      只断言"条件里出现了 ``result.media_url`` 这个词"两种都拦不住（子串都命中）。
    """
    guard = _writing_guard(_mini_branch())
    assert ast.unparse(guard.test) == "result.media_url", ast.unparse(guard.test)


def test_the_degraded_branch_only_speaks_never_writes():
    """★ 降级那支（拿不到图）**只许出声，不许往 media 里塞东西**。

    塞进去的后果不是多一行字：``mini_program_block`` 会拿一个空/别的 URL 去标封面 ——
    模型于是"看到"了一张并不存在的图。反过来，"什么都不说"也不算过：这条降级
    **必须留下痕迹**，否则真机上只能看到"卡片没图"而查不出为什么。
    """
    guard = _writing_guard(_mini_branch())
    assert guard.orelse, "降级支没了 —— 拿不到图时连一声都不出"
    assert not [w for stmt in guard.orelse for w in _media_writes(stmt)], ast.unparse(guard)
    logged = [n for stmt in guard.orelse for n in ast.walk(stmt)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr in ("info", "warning", "error", "debug")]
    assert len(logged) == 1, ast.unparse(guard.orelse[0])


def test_the_enrichment_only_runs_when_the_card_has_a_cover():
    """★ 没有 ``cover_image`` 的卡片不该去打一次厂商（每次都是白跑的外呼）。

    这一条与 W7-1 同源：``enrich`` 自己会返 ``no cover url/file_no/icon_url``，
    但那是**事后**的诚实 —— 事前就该不问。
    """
    branch = _mini_branch()
    call = next(n for n in ast.walk(branch)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "enrich_mini_program_asset")
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(branch):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    guard = parents[id(call)]
    while guard is not None and not isinstance(guard, ast.If):
        guard = parents.get(id(guard))
    assert guard is not None, "富化调用不在任何 if 里"
    assert "card.cover is not None" in ast.unparse(guard.test), ast.unparse(guard.test)


def test_the_enrichment_is_called_exactly_once():
    calls = [n for n in ast.walk(_mini_branch())
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "enrich_mini_program_asset"]
    assert len(calls) == 1


# ====================================================== H5 早退的元组形状


def test_every_return_of_fetch_media_is_still_a_pair():
    """``media_urls, media_types = await self._fetch_media(...)`` ⇒ 少一个元素就是
    收信链路上的 ``ValueError``（`return []` 是个**静态形状**错误，不该等到真机才发现）。

    ⚠️ 不是假想的：写 W7-2 这一支时我自己就把早退改成了 ``return []``。
    """
    returns = [n for n in ast.walk(_fetch_media()) if isinstance(n, ast.Return)]
    assert returns, "找不到 return"
    for node in returns:
        assert isinstance(node.value, ast.Tuple), ast.unparse(node.value)
        assert len(node.value.elts) == 2, ast.unparse(node.value)


def test_the_late_guard_is_still_the_pair_form_too():
    """空 ``msg.media`` 那条早退**必须还在**（它是"没有媒体就什么都不下"的闸），
    而且同样得返二元组 —— 上面那条追的是"每个 return"，这条追的是"这道闸还在不在"。"""
    guard = next(n for n in ast.walk(_fetch_media())
                 if isinstance(n, ast.If) and ast.unparse(n.test) == "not msg.media")
    assert len(guard.body) == 1 and isinstance(guard.body[0], ast.Return), ast.unparse(guard)
    value = guard.body[0].value
    assert isinstance(value, ast.Tuple) and len(value.elts) == 2, ast.unparse(value)
