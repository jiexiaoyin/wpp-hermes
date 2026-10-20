"""W7-1 装配回归：媒体富化**接在哪儿、键名对不对、降级有没有提前 return**（2026-10-02）。

## 为什么这一面单独一套门

``plugin/tests/test_media_enrich.py`` 验的是**纯逻辑**（四个串的形状、注入的字节、
降级不写键、``_media_tag`` 让路）。这一面验的是**适配器有没有把它接上、接在哪一步、
四个分支各自传的 kind 是不是对的** —— ``adapter.py`` 本机 **import 不了**
（模块级 ``from gateway.config import Platform`` 是 Hermes 独有）⇒ 只能按 **AST 源码形状**验。

判据挑的都是**失效形态为"看着完全正常"** 的那种：

1. **取媒体的位置太靠下** ⇒ ``_fetch_media`` 原来只在 ⑤ ``_build_event`` 里被调，
   而 ④z（落库）与 ④a（心流缓冲）都排在它**之前**、judge（v1.11.0 看图）抠的又是
   ``msg.content`` ⇒ 那条 URL 永远到不了 judge 与 DB，现象只是"bot 看不懂图"。
   ⇒ W1 判 ``_handle_one`` 里的**行号顺序**，不是"有没有这段代码"。
2. **四个分支的 ``kind`` 传串了**（复制粘贴时把图片传成 ``KIND_VIDEOS``）⇒
   OSS 目录错、标记也错（``[视频] https://…jpg``），而上传本身**成功**、日志干净。
   ⇒ W2 判分支与 kind 的**一一对应**。
3. **降级没有提前 return** ⇒ 没拿到 URL 也往里注入 ``[图片] None``，正文被污染，
   且"有凭据/没凭据"两种环境行为分叉（``真机`` 现在就是没凭据那条路）。
   ⇒ W3 判 ``if not url: return`` 在注入调用**之前**。
4. **注入写成前缀**（``f"{line}\\n{msg.content}"``）⇒ 与 ``_media_tag`` 那套混了血，
   正文里出现两个 ``[图片]``/``[文件]``。⇒ W4 逐字比那条 f-string 的**组成**。

⚠️ 两条 AST 上的注意（都踩过）：``ast.walk`` 是**广度优先、不是源码顺序** ⇒ 判"谁在谁
前面"一律用 ``lineno``；``ast.unparse`` 把下标里的字符串印成**单引号** ⇒ 期望值别写成双引号。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_MAIN = "plugin/platform/adapter.py"          # 类骨架：_handle_one / _build_event / 模块级函数
ADAPTER_MEDIA = "plugin/platform/adapter_media.py"   # 媒体方法（2026-10-19 拆分）
ADAPTER_REL = ADAPTER_MEDIA
ADAPTER_CLS = "MediaMixin"  # 类名随文件迁移
PARSER_REL = "plugin/inbound/parser.py"

#: 分支 → 该分支必须传的 kind（唯一真源是 ``adapter._OSS_KIND_TAG`` 的四个键）
BRANCH_KIND = {
    "MT_IMAGE": "KIND_IMAGES",
    "MT_VOICE": "KIND_VOICES",
    "MT_VIDEO": "KIND_VIDEOS",
    "MT_APP": "KIND_FILES",
}


# ============================================================ 源码工具（AST）


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _methods(rel: str | None = None, cls: str | None = None) -> dict[str, ast.AST]:
    """**两个文件合起来**的方法表。

    2026-10-19 把媒体方法拆到 ``adapter_media.MediaMixin``：_handle_one / _build_event
    还在 adapter.py，_fetch_media / _publish_media 已搬走。接线测试要跨两个文件看，
    否则会按"方法不存在"误报（而不是真的接线断了）。
    """
    out: dict[str, ast.AST] = {}
    for r, c in ((ADAPTER_MAIN, "WppAdapter"), (ADAPTER_MEDIA, "MediaMixin")):
        for node in ast.parse(_src(r)).body:
            if isinstance(node, ast.ClassDef) and node.name == c:
                out.update({n.name: n for n in node.body
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))})
    return out


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
    """子树里所有 ``name(...)`` 调用 —— 方法（``self._hf_note_group``）与模块级函数
    （``persist_inbound``）都要认：这一层里两种写法混着用。"""
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


def _branch(body: ast.AST, marker: str) -> ast.If:
    """``_fetch_media`` 里以 ``mtype == <marker>`` **起头**的那条下载分支。

    ⚠️ 起头 + 排除小程序卡片那条旁路 —— 后者的条件（``mtype == MT_APP and
    is_mini_program_card(msg.raw)``）与文件分支**同头**，子串匹配会抓到它。"""
    for node in ast.walk(body):
        if not isinstance(node, ast.If):
            continue
        text = ast.unparse(node.test)
        if text.startswith(f"mtype == {marker}") and "is_mini_program_card" not in text:
            return node
    raise AssertionError(f"找不到 `mtype == {marker}` 那条分支")


def _exact_if(node: ast.AST, test: str) -> ast.If:
    """条件**逐字**等于 ``test`` 的那个 ``if``（``_publish_media` 里的 ``if data:``）。"""
    for n in ast.walk(node):
        if isinstance(n, ast.If) and ast.unparse(n.test) == test:
            return n
    raise AssertionError(f"找不到 `if {test}:`")


def _kw(call: ast.Call, name: str) -> ast.AST:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    raise AssertionError(f"{ast.unparse(call)} 没有 {name}=")


# ============================================================ W1 位置：④c 在三个消费者之前


def test_the_fetch_happens_before_persist_heartflow_and_event() -> None:
    """★ 取媒体必须排在 ④z（落库）/ ④a（心流缓冲）/ ⑤（构造事件）**之前**。

    放在 ⑤ 里（W7-1 之前的样子）只覆盖"触发了的"消息，而 judge 看图抠的是
    ``msg.content`` ⇒ 那条 URL 永远到不了 judge，现象只是"bot 看不懂图"。"""
    one = _methods()["_handle_one"]
    i_fetch = _line(_calls(one, "_fetch_media"))
    assert i_fetch < _line(_calls(one, "persist_inbound")), \
        "取媒体必须在落库之前（DB 里那行正文要带 OSS URL）"
    assert i_fetch < _line(_calls(one, "_hf_note_group")), \
        "取媒体必须在心流缓冲之前（judge 看图要从 content 抠直链）"
    assert i_fetch < _line(_calls(one, "_build_event")), "取媒体必须在构造事件之前"


def test_the_fetch_result_is_handed_to_build_event() -> None:
    """取到的 ``(media_urls, media_types)`` 必须交给 ``_build_event`` ——
    否则附件（Hermes 那侧的多模态）悄悄丢了，而正文里的 URL 还在。"""
    call = _calls(_methods()["_handle_one"], "_build_event")[0]
    assert ast.unparse(_kw(call, "media")) == "media", ast.unparse(call)


def test_build_event_never_fetches_by_itself() -> None:
    """全链路只许有**一个**取媒体点：取两次＝多下一次、多传一次（白花钱）。"""
    build = _methods()["_build_event"]
    assert not _calls(build, "_fetch_media"), "_build_event 不许自己取媒体（结果来自 ④c）"
    unpack = next(n for n in ast.walk(build) if isinstance(n, ast.Assign)
                  and ast.unparse(n.targets[0]) == "(media_urls, media_types)")
    assert ast.unparse(unpack.value) == "media", ast.unparse(unpack)


# ============================================================ W2 四个分支各自传对 kind


def test_every_download_branch_publishes_with_its_own_kind() -> None:
    """★ 每个下载分支都要把字节传上去，且 ``kind`` 与分支**一一对应**。

    传串了不会报错：上传成功、日志干净，只是 OSS 目录与正文标记双双错位
    （``[视频] https://…jpg``）。"""
    fetch = _methods()["_fetch_media"]
    for marker, kind in BRANCH_KIND.items():
        branch = _branch(fetch, marker)
        calls = _calls(branch, "_publish_media")
        assert len(calls) == 1, f"{marker} 分支里 _publish_media 有 {len(calls)} 次"
        assert ast.unparse(_kw(calls[0], "kind")) == kind, ast.unparse(calls[0])


def test_the_publish_happens_inside_the_download_guard() -> None:
    """``_publish_media`` 必须在 ``if data:`` 的**正**支里 —— 没下到字节就没有 URL
    （上传空字节只会白跑一次 ossutil）。"""
    fetch = _methods()["_fetch_media"]
    for marker in BRANCH_KIND:
        branch = _branch(fetch, marker)
        guard = _exact_if(branch, "data")
        assert _calls(guard, "_publish_media"), \
            f"{marker} 分支的 _publish_media 不在 `if data:` 里"


def test_the_file_branch_publishes_under_the_same_name_it_gives_hermes() -> None:
    """文件分支的 ``filename=`` 必须与 ``cache_document_from_bytes`` 用的是**同一个**
    变量 —— 分家之后模型看到的文件名与附件里的文件名会不一样。"""
    branch = _branch(_methods()["_fetch_media"], "MT_APP")
    publish = _calls(branch, "_publish_media")[0]
    cache = _calls(branch, "cache_document_from_bytes")[0]
    assert ast.unparse(_kw(publish, "filename")) == ast.unparse(cache.args[1]), ast.unparse(publish)


# ============================================================ W3/W4 _publish_media 本体


def test_publish_returns_before_injecting_when_there_is_no_url() -> None:
    """★ 降级：``if not url: return`` 必须**排在**注入调用之前。

    没这道闸，拿不到 URL 也会执行 ``_inject_media_enrich_line(msg, kind, None, …)``
    ⇒ 正文里冒出 ``[图片] None``，而没凭据的机器（``真机``）与有凭据的机器行为分叉。"""
    pub = _methods()["_publish_media"]
    guard = next(n for n in _body(pub) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "not url")
    assert any(isinstance(s, ast.Return) for s in guard.body), "`if not url:` 里没有 return"
    assert guard.lineno < _line(_calls(pub, "_inject_media_enrich_line")), \
        "注入排在 `if not url` 之前 —— 没拿到 URL 也往正文里写"


def test_the_injection_appends_and_keeps_the_ts_leading_newline() -> None:
    """★ 逐字比那条 f-string 的组成：``{msg.content}`` 在前、``"\\n"`` 在中、``{line}`` 在后。

    写成 ``f"{line}\\n{msg.content}"`` 就是**前缀**注入 —— 与 ``_media_tag`` 那套撞车，
    正文里两个 ``[图片]``。只查"含有 msg.content"是拦不住写反的。"""
    inj = _fn(ADAPTER_MAIN, "_inject_media_enrich_line")
    assigns = [n for n in ast.walk(inj) if isinstance(n, ast.Assign)
               and ast.unparse(n.targets[0]) == "msg.content"]
    assert len(assigns) == 1, f"msg.content 被写了 {len(assigns)} 次"
    value = assigns[0].value
    assert isinstance(value, ast.JoinedStr), ast.unparse(value)
    parts = value.values
    assert len(parts) == 3, ast.unparse(value)
    assert ast.unparse(parts[0]) == "{msg.content}", ast.unparse(value)
    assert isinstance(parts[1], ast.Constant) and parts[1].value == "\n", ast.unparse(value)
    assert ast.unparse(parts[2]) == "{line}", ast.unparse(value)


def test_the_injection_is_idempotent_and_writes_both_keys() -> None:
    """两个键都必须由**参数**赋值（不是字面量），且幂等闸排在赋值之前。"""
    inj = _fn(ADAPTER_MAIN, "_inject_media_enrich_line")
    written = {ast.unparse(n.targets[0]): ast.unparse(n.value)
               for n in ast.walk(inj) if isinstance(n, ast.Assign)}
    assert written.get("msg.media['oss_url']") == "url", written
    assert written.get("msg.media['oss_kind']") == "kind", written
    guard = next(n for n in _body(inj) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "line in msg.content")
    assert any(isinstance(s, ast.Return) for s in guard.body), "幂等闸里没有 return"
    write = next(n for n in _body(inj) if isinstance(n, ast.Assign)
                 and ast.unparse(n.targets[0]) == "msg.content")
    assert guard.lineno < write.lineno, "幂等闸排在注入之后 —— 同一行会追加两次"


# ============================================================ W5 parser 的抑制在最前面


def _parser_method(name: str) -> ast.AST:
    parser = ast.parse(_src(PARSER_REL))
    cls = next(n for n in parser.body
               if isinstance(n, ast.ClassDef) and n.name == "InboundPipeline")
    return next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)


def test_the_marker_suppression_is_the_first_thing_media_tag_does() -> None:
    """★ ``oss_url`` 那道抑制必须是 ``_media_tag`` 的**第一条**语句。

    排在 ``MT_APP`` 细分之后 ⇒ 49 的文件卡片（``[文件] name (size) url`` 已在正文里）
    还会被再打一个 ``[文件]`` 前缀；排在非 49 的早退之后 ⇒ 图片/视频/语音全都漏。"""
    first = _body(_parser_method("_media_tag"))[0]
    assert isinstance(first, ast.If), ast.dump(first)[:200]
    assert ast.unparse(first.test) == "msg.media.get('oss_url')", ast.unparse(first)
    assert [ast.unparse(s) for s in first.body] == ["return ''"], ast.unparse(first)


def test_the_suppression_reads_the_same_key_the_adapter_writes() -> None:
    """写侧（adapter）与读侧（parser）必须是**同一个**键名 —— 这类跨模块约定
    没有任何编译器会管，改一边就是"URL 到了但标记也还在"。"""
    written = {n.targets[0].slice.value for n in ast.walk(_fn(ADAPTER_MAIN, "_inject_media_enrich_line"))
               if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Subscript)
               and isinstance(n.targets[0].value, ast.Attribute)
               and n.targets[0].value.attr == "media"}
    read = _body(_parser_method("_media_tag"))[0].test
    assert isinstance(read, ast.Call) and isinstance(read.func, ast.Attribute)
    assert read.func.attr == "get", ast.unparse(read)
    assert read.args[0].value in written, f"读的是 {ast.unparse(read.args[0])}，写的是 {written}"
    assert read.func.value.attr == "media", ast.unparse(read)
