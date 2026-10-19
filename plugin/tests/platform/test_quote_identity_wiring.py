"""W-Q 装配回归：群里引用回复的"被回复人"是怎么被查出来的、前缀贴在哪儿。

## 这一面单独一套门的理由

`test_quote_ref.py` 验的是**纯逻辑**（DB 行 → 是谁 + 前缀形状）。这一面验的是
**适配器有没有把它接上、接在哪一步、拿到了之后塞进哪一句话**。

``adapter.py`` 本机默认 **import 不了**（模块级 ``from gateway.config import Platform``
是 Hermes 独有）⇒ **跨语句的顺序、调用形状**只能按 AST 源码形状验（前三节）。
但 ``conftest.stubbed`` 那个空壳装上之后适配器是**能真跑**的，而这一项的要害恰好是
几个**纯取数函数**（``_vendor_nick`` / ``_resolve_display_name`` / ``_quote_prefix`` /
``_quote_identity``）—— 它们的失效形态（形状认不出、名字取成 ``"None"``、@ 错人）
**长得全都一样**，只靠形状判据分不出来 ⇒ 第四节一律**真跑**（造壳、真调用、看返回值）。

老板 2026-10-03 报的 bug（「群里 @ 机器人时触发回复了，但回复没有 @ 被回复的人」）
在这条链上的根因是**输入死了**：``fromusr`` 恒空 ⇒ ``_quote_prefix`` 第一句就返回 ``""``。
这一类失效**全都不报错、日志干净、发送成功**（发出去的只是一条普通引用回复），
所以判据必须下在"哪一步读了谁、值从哪儿来、最终那段文本有没有进 XML"上。

⚠️ 两条 AST 注意（与 ``test_relay_wiring.py`` / ``test_media_enrich_wiring.py`` 同族）：
``ast.walk`` 是**广度优先、不是源码顺序** ⇒ 判"谁在谁前面"一律用 ``lineno``；
``ast.unparse`` 把字符串印成**单引号**、把下标里的字符串也印成单引号 ⇒ 期望值别写成双引号。

⚠️ **一条如实记下的过定（over-determined）**：``_quote_prefix`` 里那四道
``if not name or name == fromusr`` 是**同一条判断**在每一级出口重复一遍，
拆掉其中任意一道的结果是**同一级的取值仍会被下一级覆盖** ⇒ 行为上只多一次查询
（联系人表 / 厂商接口），前缀本身一模一样。所以这里钉的是**四道都在**（级数与兜底口径），
而不是"每一道各自都不可少" —— 后者在这里不成立，写成断言就是自欺。
"""

from __future__ import annotations

import ast
import asyncio
import logging
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"
QUOTE_REF_REL = "plugin/inbound/quote_ref.py"


# ============================================================ 源码工具（AST）


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _methods(rel: str = ADAPTER_REL, cls: str = ADAPTER_CLS) -> dict[str, ast.AST]:
    for node in ast.parse(_src(rel)).body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {n.name: n for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    raise AssertionError(f"{rel} 里找不到 class {cls}")


def _fn(rel: str, name: str) -> ast.AST:
    for node in ast.parse(_src(rel)).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{rel} 里找不到 {name}")


def _body(fn: ast.AST) -> list[ast.stmt]:
    """去掉 docstring 之后的**顶层**语句表。"""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Attribute) and n.func.attr == name)
                 or (isinstance(n.func, ast.Name) and n.func.id == name))]


def _one(node: ast.AST, name: str) -> ast.Call:
    found = _calls(node, name)
    assert len(found) == 1, f"{name} 出现 {len(found)} 次（应为 1）"
    return found[0]


def _line(calls: list[ast.Call]) -> int:
    assert calls, "一个调用点都没有"
    return min(c.lineno for c in calls)


def _kv(call: ast.Call) -> dict[str, str]:
    """调用的关键字实参 → ``{名字: unparse 后的源码}``。"""
    return {k.arg: ast.unparse(k.value) for k in call.keywords if k.arg}


def _py_files(*, skip_tests: bool = True):
    for p in sorted((ROOT / "plugin").rglob("*.py")):
        if skip_tests and "/tests/" in str(p):
            continue
        yield p


# ============================================ 一、引用分支：谁先谁后、值从哪儿来


def test_the_identity_lookup_runs_before_the_name_search() -> None:
    """★ ``_quote_identity``（回查 DB）必须排在 ``_quote_prefix``（取名）**之前**。

    ``_quote_prefix`` 的第 2 级吃的是 ``db_name=ident.display_name``（被引用消息那一行
    自带的 ``peer_name``）。顺序反了 ⇒ 第 2 级永远是空串，昵称只剩"联系人表 → 厂商"
    两级 —— 而真机上这两级**都是空的**（``wpp_contacts`` 0 行、厂商在 py2 从来没被调过）
    ⇒ 结果就是"@ 出 ``wxid_xxx``"或干脆不 @，**且不报错**。
    """
    chunk = _methods()["_send_chunk"]
    assert _line(_calls(chunk, "_quote_identity")) < _line(_calls(chunk, "_quote_prefix"))


def test_both_run_before_the_share_link_call() -> None:
    """名字算出来之后才发 —— 排在发送之后等于白算（发出去的 XML 里没有那个前缀）。"""
    chunk = _methods()["_send_chunk"]
    sends = [c for c in _calls(chunk, "call")
             if c.args and isinstance(c.args[0], ast.Constant)
             and c.args[0].value == "/Msg/ShareLink"]
    assert len(sends) == 1, f"引用分支该走 /Msg/ShareLink 一次（实际 {len(sends)}）"
    assert max(_line(_calls(chunk, "_quote_identity")), _line(_calls(chunk, "_quote_prefix"))) \
        < sends[0].lineno


def test_metadata_outranks_the_database_identity() -> None:
    """``fromusr = metadata["quote_from_wxid"] or ident.fromusr`` —— **参数优先**。

    这一档是 TS ``params.fromWxid``（``quote-reply.ts:163``，dispatcher 手上现成的
    触发消息身份）的对应物。优先级反了不会报错：只会在"metadata 有值"时改用 DB 那一行
    （多数情况同一个人，某些转发/撤回场景不是）。
    """
    chunk = _methods()["_send_chunk"]
    vals = [ast.unparse(n.value) for n in ast.walk(chunk) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name) and t.id == "fromusr"]
    assert vals == ["(metadata or {}).get('quote_from_wxid') or ident.fromusr"], vals


def test_the_row_nickname_is_handed_over_as_the_second_level() -> None:
    """调用形状逐字：位置参数是"被回复人 + 会话 + 输入"，昵称走 ``db_name=``。"""
    call = _one(_methods()["_send_chunk"], "_quote_prefix")
    assert [ast.unparse(a) for a in call.args] == ["acct", "client", "peer_id",
                                                  "fromusr", "metadata"], ast.unparse(call)
    assert _kv(call) == {"db_name": "ident.display_name"}, _kv(call)


def test_the_prefix_reaches_the_xml_and_nowhere_else() -> None:
    """前缀只并进发给厂商的那段正文（``quote_text = f"{prefix}{chunk}".strip()``），
    且 ``svrid`` / ``fromusr`` 一起进 ``build_quote_reply_xml``。

    2026-10-18：正文先落到 ``quote_text`` 是为了**发之前**能判引用气泡装不装得下
    （``outbound.quote_reply_fits``）；超限改走普通文本，不截断（见
    ``test_long_reply_regression.py``）。
    """
    chunk = _methods()["_send_chunk"]
    assigned = [ast.unparse(n.value) for n in ast.walk(chunk) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name) and t.id == "quote_text"]
    assert assigned == ["f'{prefix}{chunk}'.strip()"], assigned
    xml = _one(chunk, "build_quote_reply_xml")
    assert _kv(xml) == {"reply_content": "quote_text",
                        "svrid": "svrid", "fromusr": "fromusr"}, _kv(xml)


def test_the_bookkeeping_records_the_unprefixed_text() -> None:
    """★ 记账那一侧收的是**不带前缀**的 ``chunk``。

    与 TS 同形（``persistQuoteReply`` / ``recordHeartflowBotReply`` 都收原始 ``content``）。
    把 ``chunk`` 换成 ``f"{prefix}{chunk}"`` ⇒ 库里每条群回复都多一个 ``@张三``，
    复读判据与引用回填都会跟着错位，而**发送本身完全正常**。
    """
    chunk = _methods()["_send_chunk"]
    persist = _one(chunk, "_persist_outbound")
    assert [ast.unparse(a) for a in persist.args] == ["acct", "peer_id", "chunk", "ids"], \
        ast.unparse(persist)
    # 心流记账（``_hf_note_*``）在 ``send`` 里、拿的是同一个未加前缀的 content ⇒ 这里为空
    assert not [n for n in ast.walk(chunk) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("_hf_note")]


def test_there_is_exactly_one_place_that_builds_the_at_prefix() -> None:
    """★★ 全树只有 ``quote_ref.build_mention_prefix`` 一个 ``"@X "`` 产出点。

    第二处产出点（顺手写一句 ``f"@{name} "``）平时看不出问题，直到两处的口径分叉
    （比如那一处忘了"名称 == wxid 就当没名称"）⇒ 群里开始出现 ``@wxid_xxx``。
    判据分两半：① ``adapter.py`` 里**没有任何**含 ``@`` 的 f-string；
    ② ``build_mention_prefix`` 全树定义一次、被调一次。
    """
    tree = ast.parse(_src(ADAPTER_REL))
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            parts = [p.value for p in n.values
                     if isinstance(p, ast.Constant) and isinstance(p.value, str)]
            if any("@" in p for p in parts):
                bad.append((n.lineno, ast.unparse(n)[:60]))
    assert not bad, f"adapter.py 里出现了自己拼 @ 的 f-string: {bad}"

    defs, called = [], []
    for p in _py_files():
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, ast.FunctionDef) and n.name == "build_mention_prefix":
                defs.append(str(p.relative_to(ROOT)))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "build_mention_prefix":
                called.append(str(p.relative_to(ROOT)))
    assert defs == [QUOTE_REF_REL], f"build_mention_prefix 的定义处: {defs}"
    # 两处调用点：平台出站链路（adapter._send_chunk）与工具面引用回复
    # （toolkit/msg.py::quote_reply，2026-10-18 对齐 TS 后新增）。**定义**仍然只有一处。
    assert set(called) == {ADAPTER_REL, "plugin/toolkit/msg.py"}, \
        f"build_mention_prefix 的调用处: {called}"


# ============================================ 二、取名链：五级、闸、兜底


def test_the_cost_gate_comes_first_and_needs_both_conditions() -> None:
    """非群 **或** 拿不到人 ⇒ 一次查询都不发。

    这道闸是**成本**闸（后面两级会打联系人表与厂商接口，而引用回复在群里是每条消息都走的），
    ``build_mention_prefix`` 里那道是**形状**闸。两道都要在：拆了这道只是变慢，
    拆了那道会在私聊里 @ 人。
    """
    first = _body(_methods()["_quote_prefix"])[0]
    assert isinstance(first, ast.If), ast.unparse(first)
    assert ast.unparse(first.test) == "not outbound.is_group_peer(peer_id) or not fromusr"
    assert [ast.unparse(s) for s in first.body] == ["return ''"]


def test_the_four_name_sources_are_tried_in_order() -> None:
    """五级链的**顺序**：metadata 昵称 → 行内 ``peer_name`` → 联系人表 → 厂商 → wxid。

    顺序只有"谁先被问"这一个可观测面（每一级都是"取到就用"），所以只能按 ``lineno`` 钉。
    换序的失效：把联系人表提到行内 ``peer_name`` 之前 ⇒ 库里那条消息自带的**当下**昵称
    被通讯录里的**旧**昵称（或改过备注的名字）顶掉 —— 不报错，只是 @ 的名字不对。
    """
    qp = _methods()["_quote_prefix"]
    meta = [c.lineno for c in _calls(qp, "get")
            if c.args and isinstance(c.args[0], ast.Constant)
            and c.args[0].value == "quote_display_name"]
    db = [n.lineno for n in ast.walk(qp) if isinstance(n, ast.Assign)
          for t in n.targets if isinstance(t, ast.Name) and t.id == "name"
          and ast.unparse(n.value) == "str(db_name or '').strip()"]
    # ``_contact_name`` 走 ``asyncio.to_thread(self._contact_name, …)`` —— 它是
    # 作为**参数**传进去的，不再是 Call 节点，按属性引用找。
    contacts = min(n.lineno for n in ast.walk(qp)
                   if isinstance(n, ast.Attribute) and n.attr == "_contact_name")
    vendor = _line(_calls(qp, "_resolve_display_name"))
    final = _line(_calls(qp, "build_mention_prefix"))
    assert meta and db, (meta, db)
    assert meta[0] < db[0] < contacts < vendor < final, \
        f"顺序: metadata={meta} 行内={db} 联系人={contacts} 厂商={vendor} 最终={final}"


def test_the_wxid_fallback_is_last_and_is_announced() -> None:
    """★ 到最后一级才用 wxid 兜底，且**出声一次**。

    这条日志是"@ 出来的是 ``wxid_xxx`` 而不是名字"的**唯一**观测点：没有它，
    联系人表没同步过 + 厂商查不到 这件事在现场和"功能没做"长得一模一样。
    """
    qp = _methods()["_quote_prefix"]
    body = _body(qp)
    fallback = body[-2]
    assert isinstance(fallback, ast.If)
    assert [ast.unparse(s) for s in fallback.body][-1] == "name = fromusr"
    assert len(_calls(fallback, "info")) == 1, "兜底那一支要有且只有一条 info 日志"
    assert ast.unparse(body[-1].value) == \
        "quote_ref.build_mention_prefix(peer_id, name, fromusr)"


def test_the_fall_through_check_is_repeated_at_every_level() -> None:
    """四道 ``if not name or name == fromusr`` —— 钉**级数与兜底口径**（见模块 docstring）。

    少一道不等于坏（下一级会覆盖），但"某一级没有兜底口径"意味着它的取值会**越过**
    "名称等于 wxid 就当没有名称"这条规则直接进前缀 ⇒ ``@wxid_xxx``。
    """
    qp = _methods()["_quote_prefix"]
    guards = [n for n in ast.walk(qp) if isinstance(n, ast.If)
              and ast.unparse(n.test) == "not name or name == fromusr"]
    assert len(guards) == 4, f"兜底判据出现 {len(guards)} 次（应为 4，即四级各自一道）"


# ============================================ 三、昵称来源：联系人表 + 厂商


def test_the_vendor_extraction_prefers_the_contact_list() -> None:
    """``Data.ContactList[0].NickName`` **优先于**扁平键 —— 这是旧树与 TS 共同给出的形状。

    先查扁平 ``Data.NickName`` 的失效形态：真实回包里顶层没有这个键 ⇒ 永远取不到；
    而"取到了但取错"更坏（Go 把字段包在 ``{"string": …}`` 里，解错包就是
    ``"{'string': '张三'}"`` 这种**不是名字的名字**，会被原样 @ 进群）。
    """
    vn = _methods()["_vendor_nick"]
    sub = [n.lineno for n in ast.walk(vn) if isinstance(n, ast.Subscript)
           and ast.unparse(n) == "contact_list[0]"]
    flat = [n.lineno for n in ast.walk(vn) if isinstance(n, ast.For)
            and ast.unparse(n.iter).startswith("(")]
    assert sub, "没有 ContactList[0] 这一档"
    assert flat, "没有扁平键那一档"
    assert min(sub) < min(flat), f"ContactList[0]@{sub} 必须排在扁平键@{flat} 之前"


def test_the_vendor_extraction_always_unwraps_and_never_stringifies_a_dict() -> None:
    """每个取到的值都必须过 ``_safe_str``（= ``_unwrap`` + 去空白）。

    旧写法 ``str(data.get("NickName"))`` 的洞是**双向**的：值真在扁平键上时得到
    ``"{'string': '张三'}"``，值不在时得到 ``"None"`` —— 两个都会被 @ 进群。
    """
    vn = _methods()["_vendor_nick"]
    assigns = [n for n in ast.walk(vn) if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name) and t.id == "found"]
    assert assigns, "没有 found = … 这一形状"
    for a in assigns:
        assert ast.unparse(a.value).startswith("_safe_str("), ast.unparse(a)
    assert not _calls(vn, "str"), "取昵称那一层不许出现裸 str(...)"


def test_the_vendor_lookup_is_gated_against_group_ids() -> None:
    """群 id 不查厂商 —— 与 ``quote_ref.is_usable_sender`` 同一口径（两道闸，同一条规则）。"""
    rd = _methods()["_resolve_display_name"]
    first = _body(rd)[0]
    assert isinstance(first, ast.If), ast.unparse(first)
    assert ast.unparse(first.test) == "not wxid or not quote_ref.is_usable_sender(wxid)"
    assert [ast.unparse(s) for s in first.body] == ["return ''"]


def test_the_vendor_endpoint_is_called_with_the_wxid() -> None:
    """走 ``/Friend/GetContractDetail``（``api_client.get_contract_detail``），
    并且只从 ``Data`` 里抠（回包外层是 ``{code, Data}``）。"""
    rd = _methods()["_resolve_display_name"]
    call = _one(rd, "get_contract_detail")
    assert [ast.unparse(a) for a in call.args] == ["wxid"]
    data = [ast.unparse(n.value) for n in ast.walk(rd) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name) and t.id == "data"]
    assert data == ["detail.get('Data') if isinstance(detail, dict) else None"], data


def test_the_nickname_cache_is_per_account_and_negative_results_are_cached_too():
    """30 分钟缓存、键带账号，且**取不到也要缓存**。

    这条链在群里每条引用回复都走一遍 ⇒ 不缓存就是每句话多打一次厂商接口；
    而"只缓存成功"等于把最常发生的那一种（查不到）永远留在缓存外。
    """
    rd = _methods()["_resolve_display_name"]
    key = "f'{acct.id}:{wxid}'"
    # 读：``self._name_cache.get(<key>)`` —— 写：``self._name_cache[<key>] = …``
    read = [ast.unparse(c.args[0]) for c in _calls(rd, "get")
            if isinstance(c.func, ast.Attribute)
            and ast.unparse(c.func.value) == "self._name_cache" and c.args]
    written = [ast.unparse(n.slice) for n in ast.walk(rd) if isinstance(n, ast.Subscript)
               and ast.unparse(n.value) == "self._name_cache"]
    assert read == [key] and written == [key], (read, written)
    ttl = [ast.unparse(n.comparators[0]) for n in ast.walk(rd) if isinstance(n, ast.Compare)
           and ast.unparse(n.left) == "time.time() - cached[1]"]
    assert ttl == ["1800"], ttl
    store = [n for n in ast.walk(rd) if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Subscript)
             and ast.unparse(t.value) == "self._name_cache"]
    assert len(store) == 1, "缓存写入点有且只有一处"
    tries = [n for n in ast.walk(rd) if isinstance(n, ast.Try)]
    for n in tries:
        inside = {id(x) for x in ast.walk(n)}
        assert id(store[0]) not in inside, "缓存写入必须在 try 之外 —— 否则失败那一路不缓存"


# ============================================ 四、树级不变量


def test_the_anchor_lookup_asks_by_both_ids_in_one_query() -> None:
    """回查用 ``get_message_by_id_or_new_id(acct.id, msg_id=svrid, new_msg_id=svrid)``。

    Hermes 给的锚点是 ``event.message_id``（= 入站那行的 ``msg_id``），而厂商回包里
    两个 id 都可能出现、落库时按 ``new_msg_id`` 另存一列 —— 只按 ``msg_id`` 查会漏掉
    "锚点其实是服务端 id"的那一半，而**漏掉就是静默不 @**。
    """
    qi = _methods()["_quote_identity"]
    # 同步 pymysql 调用必须包在 ``asyncio.to_thread(...)`` 里（否则慢库会卡住事件
    # 循环）；被包住的函数是**参数**，不是 Call 节点。
    threaded = [c for c in _calls(qi, "to_thread")
                if c.args and isinstance(c.args[0], ast.Attribute)
                and c.args[0].attr == "get_message_by_id_or_new_id"]
    assert len(threaded) == 1, "回查必须走 to_thread 且只有一个调用点"
    call = threaded[0]
    assert [ast.unparse(a) for a in call.args[1:]] == ["acct.id"]
    assert _kv(call) == {"msg_id": "svrid", "new_msg_id": "svrid"}, _kv(call)


def test_the_identity_lookup_never_lets_a_failure_escape() -> None:
    """昵称只是装饰：查不到 / 库读不了都只降级（返空身份），**不许抛**。

    ``_send_chunk`` 的外层 ``except`` 会把异常变成"发送失败"—— 于是"查不到昵称"
    升级成"这条回复根本没发出去"。这是最坏的一种降级。
    """
    qi = _methods()["_quote_identity"]
    assert not [n for n in ast.walk(qi) if isinstance(n, ast.Raise)], "不该有 raise"
    tries = [n for n in ast.walk(qi) if isinstance(n, ast.Try)]
    assert len(tries) == 1, "回查那一段要包在 try 里"
    handlers = [h for t in tries for h in t.handlers]
    assert handlers, "try 必须有 except"
    assert any("quote_ref.QuoteIdentity()" in ast.unparse(h.body) for h in handlers), \
        "except 分支要回一个空身份"


def test_quote_from_wxid_has_exactly_one_reader_and_no_writer() -> None:
    """★ 这个键此前是**死输入**（全树只有读、没有写，而读的那处拿到的永远是 ``None``）。

    现在它仍是"只有读"—— 但读的那一处**有了 OR 兜底**（DB 身份）。这条判据钉的是：
    ① 它没有悄悄多出第二个读点（多一处就可能有一处还依赖它而恒空）；
    ② 没有任何地方写它（写了就等于把"谁被回复了"交给调用方，而 py2 的调用方——
    Hermes —— 结构性给不出这个键）。
    """
    readers, writers = [], []
    for p in _py_files():
        rel = str(p.relative_to(ROOT))
        tree = ast.parse(p.read_text(encoding="utf-8"))
        parent: dict[int, ast.AST] = {}
        for n in ast.walk(tree):
            for child in ast.iter_child_nodes(n):
                parent[id(child)] = n
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Constant) and n.value == "quote_from_wxid"):
                continue
            up = parent.get(id(n))
            # ``d["quote_from_wxid"] = …`` ⇒ 这个常量是赋值目标的切片（写）
            target = (isinstance(up, ast.Subscript)
                      and isinstance(parent.get(id(up)), (ast.Assign, ast.AugAssign)))
            (writers if target else readers).append((rel, n.lineno))
    assert writers == [], f"写点: {writers}"
    # ⚠️ **判据不写死行号**（原来钉的是 ``(ADAPTER_REL, 1669)``）：行号会被**无关的**装配改动
    #    推走 —— R1 的限速闸往 ``_handle_one`` 里加了一段注释 + 一个判定，这个读点就从
    #    1669 变成 1695，门禁红在一条**源码一个字没改**的判据上。改成"唯一读点 + 它落在
    #    ``_send_chunk`` 里"：比行号**更紧**（行号说不出它在哪个函数里），也不会被行漂移打红。
    assert len(readers) == 1, f"读点: {readers}"
    assert readers[0][0] == ADAPTER_REL, f"读点: {readers}"
    assert _enclosing_method(readers[0][1]) == "_send_chunk", f"读点: {readers}"


def _enclosing_method(lineno: int) -> str:
    """这个行号落在 ``WppAdapter`` 的哪个方法里（按 ``lineno``/``end_lineno`` 区间判）。"""
    for name, node in _methods().items():
        end = node.end_lineno or node.lineno
        if node.lineno <= lineno <= end:
            return name
    raise AssertionError(f"{ADAPTER_REL}:{lineno} 不在任何方法里（模块级？）")


def test_the_adapter_imports_the_identity_module_once() -> None:
    """装配面的入口：``from ..inbound import quote_ref``（一处即可，函数内不再重复 import）。"""
    tree = ast.parse(_src(ADAPTER_REL))
    # ⚠️ ``from ..inbound import x`` 在 AST 里是 ``level=2, module='inbound'``
    #    （前导点在 ``level`` 里，不在 ``module`` 字符串里）—— 按 ``module="..inbound"``
    #    找会得到空列表，看起来像"没导入"。
    imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
               and n.level == 2 and n.module == "inbound"]
    names = [a.name for n in imports for a in n.names]
    assert names.count("quote_ref") == 1, names


# ============================================ 五、真跑：取昵称那三个函数
#
# 造壳（``__new__`` 免掉 Hermes 基类与 ``__init__`` 里的连接逻辑），只补这几个方法用到的
# 实例属性。**不是**在测"调用了没有"——下面每一条都在看**返回值**。


ACCT = types.SimpleNamespace(id="acct-42", nickname="益融小助理")
GROUP = "57737516566@chatroom"
SENDER = "wxid_sender"


@pytest.fixture(scope="module")
def adapter(stubbed):  # noqa: F811 —— 空壳环境来自 conftest（模块级）
    import plugin.platform.adapter as A
    return A


def _shell(A, *, names: bool = True):
    ad = A.WppAdapter.__new__(A.WppAdapter)
    if names:
        ad._name_cache = {}
    return ad


class _Client:
    """假厂商客户端：只实现 ``get_contract_detail``，并**记下被问过谁**。"""

    def __init__(self, detail=None, boom: bool = False) -> None:
        self.detail = detail
        self.boom = boom
        self.calls: list[str] = []

    async def get_contract_detail(self, user_name: str):
        self.calls.append(user_name)
        if self.boom:
            raise RuntimeError("厂商炸了")
        return self.detail


def _boom(*_a, **_kw):
    raise AssertionError("这一档不该被问到（说明前面那一档没生效）")


# ---- 前缀链


def test_a_row_nickname_needs_no_lookup_at_all(adapter, monkeypatch) -> None:
    """第 2 级（被引用消息那一行的 ``peer_name``）命中 ⇒ **一次都不再往后问**。

    真机形态：``peer_name`` 有值而通讯录空、厂商也没被调过 —— 这一档是**唯一**能让
    ``@`` 落在真名字上的那一级，它没生效的话整条链就只剩 wxid 兜底。
    """
    monkeypatch.setattr(adapter.WppAdapter, "_contact_name", staticmethod(_boom))
    monkeypatch.setattr(adapter.WppAdapter, "_resolve_display_name", _boom)
    ad = _shell(adapter)
    got = asyncio.run(ad._quote_prefix(ACCT, _Client(), GROUP, SENDER, None, db_name="张三"))
    assert got == "@张三 "


def test_a_metadata_name_beats_the_row(adapter, monkeypatch) -> None:
    monkeypatch.setattr(adapter.WppAdapter, "_contact_name", staticmethod(_boom))
    monkeypatch.setattr(adapter.WppAdapter, "_resolve_display_name", _boom)
    ad = _shell(adapter)
    got = asyncio.run(ad._quote_prefix(ACCT, _Client(), GROUP, SENDER,
                                       {"quote_display_name": "李四"}, db_name="张三"))
    assert got == "@李四 "


def test_without_a_replied_person_there_is_no_prefix_and_no_lookup(adapter, monkeypatch) -> None:
    """``fromusr`` 空 ⇒ 直接返回 —— 而且**不许**去问通讯录/厂商（那是白跑一次外呼）。"""
    monkeypatch.setattr(adapter.WppAdapter, "_contact_name", staticmethod(_boom))
    monkeypatch.setattr(adapter.WppAdapter, "_resolve_display_name", _boom)
    ad = _shell(adapter)
    assert asyncio.run(ad._quote_prefix(ACCT, _Client(), GROUP, "", None, db_name="张三")) == ""


def test_a_dm_never_gets_a_prefix(adapter, monkeypatch) -> None:
    """私聊里 @ 人没有意义（TS 的 ``buildGroupMentionPrefix`` 同样只对群生效）。"""
    monkeypatch.setattr(adapter.WppAdapter, "_contact_name", staticmethod(_boom))
    monkeypatch.setattr(adapter.WppAdapter, "_resolve_display_name", _boom)
    ad = _shell(adapter)
    got = asyncio.run(ad._quote_prefix(ACCT, _Client(), "wxid_friend", SENDER, None,
                                       db_name="张三"))
    assert got == ""


def test_the_wxid_is_the_fallback_and_the_fall_is_announced(adapter, monkeypatch, caplog) -> None:
    """四级都取不到 ⇒ ``@wxid_xxx``，并且**留一条 INFO**。

    这条日志是现场唯一能把"这功能坏了"与"这功能没做"分开的东西：两者发出去的正文
    都只是一个光秃秃的 ``@wxid_xxx``。
    """
    monkeypatch.setattr(adapter.WppAdapter, "_contact_name", staticmethod(lambda *a: ""))
    monkeypatch.setattr(adapter.WppAdapter, "_resolve_display_name",
                        lambda *a, **kw: _empty())
    ad = _shell(adapter)
    with caplog.at_level(logging.INFO, logger="plugin.platform.adapter"):
        got = asyncio.run(ad._quote_prefix(ACCT, _Client(), GROUP, SENDER, None))
    assert got == f"@{SENDER} "
    assert any("昵称" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO), \
        [r.getMessage() for r in caplog.records]


async def _empty():
    return ""


# ---- 厂商回包形状（这一条钉的正是本次修的 bug）


@pytest.mark.parametrize("data,expect", [
    ({"ContactList": [{"NickName": {"string": "张三"}}]}, "张三"),   # ★ 真实形状
    ({"ContactList": [{"NickName": "张三"}]}, "张三"),               # 没包 string
    ({"ContactList": [{"NickName": {"string": " 张三 "}}]}, "张三"),  # 带空白
    ({"ContactList": [{"Remark": {"string": "备注名"}}]}, "备注名"),   # 昵称缺 → 备注
    ({"ContactList": [{"NickName": {"string": ""}}, {"NickName": {"string": "第二"}}]}, ""),
    ({"NickName": {"string": "扁平"}}, "扁平"),                      # 扁平兜底
    ({"NickName": "扁平"}, "扁平"),
    ({}, ""),
    (None, ""),
    ("不是 dict", ""),
    ({"ContactList": []}, ""),
    ({"ContactList": [{"NickName": {"other": "x"}}]}, ""),           # 包的是别的键 ⇒ 不是名字
])
def test_the_vendor_payload_shapes(adapter, data, expect) -> None:
    """★ 旧写法 ``data.get("NickName")`` 在**第一行**那种形状上永远取不到（多一层
    ``ContactList``）—— 现场表现就是"@ 出来的是 wxid"，而不是报错。

    反面同样要钉：取不到时回**空串**，绝不许回 ``"{'string': '张三'}"`` 或 ``"None"``
    —— 那两个会被原样 @ 进群，比不 @ 更糟。
    """
    got = adapter.WppAdapter._vendor_nick(data)
    assert got == expect
    if expect == "":
        # 取不到时**只能是空串**：``str(dict)`` 的 ``"{'string': '张三'}"`` 与 ``str(None)``
        # 的 ``"None"`` 都是"不是名字的名字"，@ 进群比不 @ 更糟
        assert got == "" and "{" not in got and got != "None"


# ---- 厂商查询（缓存 / 降级 / 群 id 闸）


def test_a_vendor_nickname_is_returned_and_then_served_from_cache(adapter) -> None:
    client = _Client({"Data": {"ContactList": [{"NickName": {"string": "张三"}}]}})
    ad = _shell(adapter)
    assert asyncio.run(ad._resolve_display_name(ACCT, client, SENDER)) == "张三"
    assert asyncio.run(ad._resolve_display_name(ACCT, client, SENDER)) == "张三"
    assert client.calls == [SENDER], "第二次必须吃缓存（引用回复在群里每条都走这条链）"


def test_a_miss_returns_the_wxid_and_is_cached_too(adapter) -> None:
    """取不到也缓存（负缓存）：不然每条引用回复都要为"查不到"再打一次厂商接口。"""
    client = _Client({"Data": {"ContactList": []}})
    ad = _shell(adapter)
    assert asyncio.run(ad._resolve_display_name(ACCT, client, SENDER)) == SENDER
    assert asyncio.run(ad._resolve_display_name(ACCT, client, SENDER)) == SENDER
    assert client.calls == [SENDER]


def test_the_cache_is_per_account(adapter) -> None:
    """同一个 wxid 在两个账号下可能是两个昵称 —— 串号会把别人的昵称 @ 进群。"""
    client = _Client({"Data": {"ContactList": [{"NickName": {"string": "张三"}}]}})
    ad = _shell(adapter)
    asyncio.run(ad._resolve_display_name(ACCT, client, SENDER))
    other = types.SimpleNamespace(id="acct-43")
    asyncio.run(ad._resolve_display_name(other, client, SENDER))
    assert client.calls == [SENDER, SENDER], "换账号必须重新查"


def test_a_group_id_never_reaches_the_vendor(adapter) -> None:
    client = _Client({"Data": {}})
    ad = _shell(adapter)
    assert asyncio.run(ad._resolve_display_name(ACCT, client, GROUP)) == ""
    assert client.calls == []


def test_a_vendor_failure_degrades_instead_of_raising(adapter) -> None:
    """厂商炸了 ⇒ 至少回 wxid（发送照常），**不许把异常冒到发送那层** ——
    冒上去就会被 ``_send_chunk`` 的兜底变成"这条回复没发出去"。"""
    client = _Client(boom=True)
    ad = _shell(adapter)
    assert asyncio.run(ad._resolve_display_name(ACCT, client, SENDER)) == SENDER


# ---- 身份回查（真跑一次）


def test_the_identity_comes_from_the_message_row(adapter, monkeypatch) -> None:
    from plugin.data import repo_messages

    seen: list[tuple] = []

    def fake(account_id, msg_id=None, new_msg_id=None, conn=None):
        seen.append((account_id, msg_id, new_msg_id))
        return {"msg_id": "9001", "peer_kind": "group", "peer_id": GROUP,
                "peer_name": "张三", "from_wxid": SENDER, "content": "@bot 有货吗",
                "raw_payload": None}

    monkeypatch.setattr(repo_messages, "get_message_by_id_or_new_id", fake)
    ad = _shell(adapter, names=False)
    ident = asyncio.run(ad._quote_identity(ACCT, "9001"))
    assert (ident.fromusr, ident.display_name, ident.matched) == (SENDER, "张三", True)
    assert seen == [("acct-42", "9001", "9001")], "两个 id 都要问（厂商回的可能是任一个）"


def test_a_broken_database_only_costs_the_mention(adapter, monkeypatch) -> None:
    from plugin.data import repo_messages

    def boom(*_a, **_kw):
        raise RuntimeError("库连不上")

    monkeypatch.setattr(repo_messages, "get_message_by_id_or_new_id", boom)
    ad = _shell(adapter, names=False)
    ident = asyncio.run(ad._quote_identity(ACCT, "9001"))
    assert (ident.fromusr, ident.matched) == ("", False), "降级成空身份，但不许抛"
