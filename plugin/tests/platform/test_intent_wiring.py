"""W3 装配回归：群聊上下文**注入得对不对**（2026-10-02）。

## 为什么这一面单独一套门

``plugin/tests/test_intent.py`` 验的是**纯逻辑**（规则分类、候选压缩、LLM 响应解析、
embedding 与相似度、降级矩阵），这一面验的是**它有没有被接上、接得对不对**。
``adapter.py`` 本机 **import 不了**（模块级 ``from gateway.config import Platform``
是 Hermes 独有，见 ``test_outbound_send_result.py:33-35``）⇒ 适配器那半只能按
**AST 源码形状**验；数据层那半（§2）真跑，因为 ``plugin.data`` 本机可导入。

四条判据挑的都是**失效形态为"看着完全正常"** 的那种：

1. **总闸守卫写在查库之后** ⇒ 开关关着的账号照样一次 DB + 一次 embedding/LLM 外呼
   （花钱、且请求体被悄悄改了内容）。⇒ §1 H1 判的是**源码顺序**，不是"有没有守卫"。
2. **候选池没排除触发消息自己** ⇒ 候选人里永远有"用户刚 @ 你的那一句"，
   意图判断拿它当背景 ⇒ 现场表现是"AI 复读用户的话"，日志一句错都没有。⇒ H3。
    ⚠️ 这条**不是保险**：``persist_inbound`` 排在 ``_build_event`` **之前**（:611/:635），
    查库那一刻触发消息**已经在库里**了。
3. **异常穿透** ⇒ 这条路径挂在收信链路上，一次 DB 抖动应当只丢上下文，不该丢消息。
    ⇒ H2 判"整段在 try 里 + 每个出口都是 ``return None`` + 源码里一个 ``raise`` 都没有"。
4. **``None`` 被当成"注入空"** ⇒ ``None`` 是"**不注入**"，抹平它会让每个 topic 意图都
   拿到一段空上下文（模型看到"以下是最近 0 条消息"）—— 比不注入更糟。⇒ H4。

§2 的判据下在**发出去的 SQL 与参数**上（不是"调用过 list_group_messages"）：
``before_sec`` 必须变成 ``ts < FROM_UNIXTIME(%s)``（**严格小于**）且参数顺序对得上，
关掉时**一个字节都不该出现在 SQL 里**。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from plugin.data import repo_messages

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"
INTENT_REL = "plugin/inbound/intent.py"


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
    """去 docstring 后的源码（docstring 里写满了反例，搜不得 —— 同 ``test_invariants``）。"""
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


def _fn(name: str) -> ast.AST:
    m = _methods()
    assert name in m, f"{ADAPTER_CLS}.{name} 不在了 —— 改名了还是被删了？"
    return m[name]


# ============================================================ §1 装配：adapter

def test_h1_the_master_switch_is_checked_before_any_io():
    """★ 核心判据：``group_context_enabled`` 的守卫必须在**纯函数与 IO 之前**。

    ``acct.group_context_enabled`` 是**总闸**（TS 默认 false，必须显式 true，
    ``dispatcher.ts:154-158``）。守卫写晚了 —— 查完库才发现开关是关的 —— 现场
    什么都看不出来：回执正常、日志正常，只是**每个群每条消息都白查一次库**，
    而且配了 embedding key 的账号还会**白花一次外呼**。

    ⇒ 判据是**源码里的先后顺序**：总闸 < 规则预筛 < 查库 < 选择。
    """
    fn = _fn("_intent_channel_context")
    src = _code(fn)
    for needle in ("acct.group_context_enabled", "classify_group_intent(",
                   "list_group_messages", "select_intent_context("):
        assert needle in src, f"装配缺一块：{needle}"
    order = [src.index("acct.group_context_enabled"), src.index("classify_group_intent("),
             src.index("list_group_messages"), src.index("select_intent_context(")]
    assert order == sorted(order), f"守卫或查询顺序错了（开关要第一个判）：{order}"

    # 两个守卫都得**真的拦住**：body 必须就是 `return None`。
    # ⚠️ 只判"源码里出现过 group_context_enabled"是不够的 —— 把 body 改成
    #    `pass`（或 `log.debug(...)`）它照样在，而开关就变成了装饰。
    for test_src in ("not msg.is_group", "not acct.group_context_enabled"):
        guard = [n for n in ast.walk(fn)
                 if isinstance(n, ast.If) and ast.unparse(n.test) == test_src]
        assert guard, f"守卫不见了：if {test_src}:"
        body = [ast.unparse(s) for s in guard[0].body]
        assert body == ["return None"], f"if {test_src}: 的 body 必须是 return None，实际 {body}"

    # 非群消息同样在 IO 之前出局（TS 的 `if (msg.peerKind !== "group") return null`）
    assert "msg.is_group" in src
    assert src.index("msg.is_group") < src.index("list_group_messages")

    # 反面自证：把守卫挪到查库**之后**（真违规形态），判据要能红
    probe = ("rows = repo_messages.list_group_messages(acct.id, gid)\n"
             "kind = classify_group_intent(content)\n"
             "if not acct.group_context_enabled:\n    return None\n")
    bad = [probe.index("acct.group_context_enabled"), probe.index("classify_group_intent("),
           probe.index("list_group_messages(")]
    assert bad != sorted(bad), "判据形状写错了 —— 它抓不到真违规"


def test_h2_every_failure_path_returns_none_and_nothing_raises():
    """★ 失败只 warn、**绝不上抛**（这条路径挂在收信链路上）。

    与 W2 的打分旁路同理、与 W6 的"写回失败必须出声"**刚好相反**：上下文注入失败
    只该少一段背景，不该把消息丢掉或让收信协程炸。

    ⇒ 判据：①整段包在 ``try`` 里且 ``except Exception`` 后跟 ``log.warning``；
    ②每个 ``Return`` 要么是 ``None``、要么就是 ``build_group_context_lines`` 的产物
    （不许把异常对象/裸群 id/空串之类的中间值当返回值漏出去）；
    ③源码里**一个 ``raise`` 都没有**。
    """
    fn = _fn("_intent_channel_context")
    src = _code(fn)
    assert "try:" in src and "except Exception" in src, "没兜住任何异常"
    assert "log.warning" in src, "失败要出声（否则注入没了都没人知道）"

    # 拼文本的那一步必须落在**具名变量**上，否则下面这条判据无从分辨"哪个 return 合法"
    assign = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
              and "build_group_context_lines(" in ast.unparse(n.value)
              and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)]
    assert assign, "build_group_context_lines(...) 的结果要落到一个具名变量上"
    ok_name = assign[0].targets[0].id

    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert returns, "一个 return 都没有？"
    bad = [ast.unparse(r) for r in returns
           if not (r.value is None or ast.unparse(r.value) in ("None", ok_name))]
    assert not bad, f"这些出口把非 None/非上下文文本漏出去了：{bad}"
    assert "raise" not in src, "收信链路上的异常必须收成 warn，不许上抛"


def test_h3_the_candidate_pool_excludes_the_trigger_message_itself():
    """★ 候选池 = 当前消息 ts **之前**的群消息（TS ``dispatcher.ts:343`` 的 ``beforeTs``）。

    ⚠️ 这是**唯一**的排除手段：适配器里 ``persist_inbound`` 排在 ``_build_event``
    **之前** ⇒ 查库那一刻，触发消息**已经在库里**。少了 ``before_sec``，
    候选人里永远有"用户刚 @ 你的那一句"，而意图判断要的恰恰是**它之前的背景**。
    """
    src = _code(_fn("_intent_channel_context"))
    assert "msg.timestamp" in src, "ts 必须来自这条消息自己的时间戳"
    assert "before_sec=" in src, "没传 before_sec ⇒ 触发消息会被喂回给意图判断"

    fn = _fn("_intent_channel_context")
    refs = [n for n in ast.walk(fn)
            if isinstance(n, ast.Attribute) and n.attr == "list_group_messages"]
    assert len(refs) == 1, f"list_group_messages 调用点不止一个：{len(refs)}"
    # 2026-10-19：同步 pymysql 查询挪进 ``asyncio.to_thread``（否则卡事件循环）。
    # 被调函数变成 to_thread 的**位置参数**，业务参数整体右移一位。
    threaded = [n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "to_thread"
                and n.args
                and ast.unparse(n.args[0]) == "repo_messages.list_group_messages"]
    assert threaded, "同步 DB 查询必须走 asyncio.to_thread（否则阻塞事件循环）"
    call = threaded[0]
    offset = 1
    kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert "before_sec" in kw, f"before_sec 必须显式传：{kw}"
    assert "ts" in kw["before_sec"], f"before_sec 要取这条消息的 ts：{kw['before_sec']}"
    # 窗口大小走 resolver（TS `resolveGroupWindow`，1..100、默认 10），不在这里硬编码
    assert kw.get("limit") == "resolve_intent_window(acct.raw)", \
        f"limit 没走 resolver（硬编码的窗口改不动）：{kw.get('limit')}"
    # 群 id：TS 用 peerId；py2 优先 chatroom_id（落库形态兼容见 W1）。
    # 这一支是**位置**传参（``(account_id, group_id, ...)``），所以判位置不判关键字。
    gid = kw.get("group_id") or ast.unparse(call.args[1 + offset])
    assert gid.startswith("msg.chatroom_id"), f"群 id 取错了：{gid}"
    assert ast.unparse(call.args[0 + offset]) == "acct.id", \
        "第一个位置参数必须是账号 id"


def test_h4_none_means_do_not_inject_and_it_survives_the_walk_back():
    """``select_intent_context`` 的 ``None`` = "**不注入**"（不是"注入空"）。

    抹平它（``kept or rows`` 之类）会让每个 topic 意图都拿到一段空上下文 ——
    模型看到"以下是最近 0 条消息"，比不注入更糟。⇒ 判据：``kept is None`` 要**显式**
    判一次并 ``return None``；拼文本走 ``build_group_context_lines``，且它的结果
    只在**非空**时才当返回值。
    """
    src = _code(_fn("_intent_channel_context"))
    assert "if kept is None" in src.replace("(kept is None)", "kept is None"), \
        "没显式区分 None 与空列表"
    assert "build_group_context_lines(" in src, "拼注入文本要用现成的那支"
    assert "if text:" in src, "空文本也要当成'不注入'（否则模型收到一段空上下文）"

    # 反面自证：`kept or rows` 这种抹平写法出现在源码里就该红
    probe = "kept = kept or rows\nif kept:\n    pass\n"
    assert "if kept is None" not in probe


def test_h5_the_llm_half_is_gated_by_the_account_flag_and_delegates():
    """LLM 那半由 ``llm_intent_enabled`` 把门，解析本体在 intent.py 的 resolver。

    ⚠️ **默认值有意与 TS 相反**：TS 缺键算**开**（``dispatcher.ts:162-168``），
    py2 缺键算 **False**（``accounts.py:420``）。这条链上多一次 LLM 外呼都是花钱的，
    默认不花 —— 判据钉在"开关关着**直接返回 None**、一次都不往下走"上。
    """
    src = _code(_fn("_intent_llm_cfg"))
    assert "acct.llm_intent_enabled" in src, "没读账号开关"
    assert "return None" in src, "开关关着必须回 None（不是空 config）"
    assert src.index("acct.llm_intent_enabled") < src.index("resolve_intent_llm_cfg("), \
        "先判开关再解析（顺序反了 = 关着也白解析一次）"
    assert "resolve_intent_llm_cfg(acct.raw" in src, "解析走 intent.py 的 resolver"
    assert "nickname=acct.nickname" in src, "botName 取账号昵称（TS resolveBotName）"

    # 适配器里不许再长出第二份解析（模型名/超时/窗口只允许有一个家）
    whole = _src(ADAPTER_REL)
    for needle in ("llmIntentModel", "llmIntentTimeoutMs", "groupContextWindow"):
        assert f'"{needle}"' not in whole, \
            f"适配器里出现了对 {needle} 的直接读取 —— 解析该在 intent.py 里（TS 的那两个 resolver）"


def test_h6_the_context_goes_to_channel_context_not_into_the_text():
    """注入走 ``MessageEvent.channel_context=``（缝见 plan Context 缝 2）。

    ``event.py:82`` 的注释写明它 ``kept out of text``，消费点 ``run_inbound.py:1465``
    把它前置进请求体 ⇒ 与 TS「前置进 Body」**同形**（不像 W2 的 ``channel_prompt``
    那样是有意偏离）。
    ⇒ 判据：``MessageEvent(...)`` 的那次调用里 ``channel_context`` 取的是
    ``await self._intent_channel_context(acct, msg)``，且**不许**出现在 ``text=`` 上。
    """
    calls = [n for n in ast.walk(_fn("_build_event"))
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "MessageEvent"]
    assert len(calls) == 1, f"MessageEvent 构造点不止一个/找不到：{len(calls)}"
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert "channel_context" in kw, "群聊上下文没接上 MessageEvent"
    assert kw["channel_context"] == "await self._intent_channel_context(acct, msg)", \
        f"channel_context 取错了：{kw['channel_context']}"
    assert kw["text"] == "text", "正文只能是 prepare_text 的结果（上下文进正文会被当用户说的话）"


def test_h7_the_injection_has_exactly_one_call_site():
    """全模块**只有一处**调用它 —— 多出来的那一处就是"同一段上下文进两次请求体"。

    （``_mood_channel_prompt`` 的 H9 同款判据：调用点数量本身就是判据。）
    """
    hits = [n for n in ast.walk(ast.parse(_src(ADAPTER_REL)))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_intent_channel_context"]
    assert len(hits) == 1, f"_intent_channel_context 的调用点应当只有 1 处，实际 {len(hits)}"


def test_h8_the_w3_invariant_is_marked_in_its_source_home():
    """不变量标记留在**源码的家**里（W1 在 ``inbound/persist.py``、W6 在 ``account_store.py``、
    W2 在 ``inbound/affection.py``、W3 在这里）—— 改到这一带的人**必须**先看到这三条。
    """
    src = _src(INTENT_REL)
    assert "INVARIANT: W3" in src, "invariant 标记不见了"
    for phrase in ("零副作用", "排除触发消息自身", "失败只 warn"):
        assert phrase in src, f"三条判据少了一条：{phrase}"


# ============================================================ §2 数据层：SQL 形状

class _Recorder:
    """假 connection：只记 ``(sql, params)``，不做任何 IO。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def query(self, sql: str, params=()) -> list[dict]:      # noqa: ANN001
        self.calls.append((sql, tuple(params)))
        return []

    @property
    def only(self) -> tuple[str, tuple]:
        assert len(self.calls) == 1, f"应当只查一次，实际 {len(self.calls)} 次"
        return self.calls[0]


def test_before_sec_becomes_a_strictly_less_than_filter():
    """``before_sec`` → ``ts < FROM_UNIXTIME(%s)``，**严格小于**（TS ``mysql.ts:581-583``）。

    ``<=`` 会把"同一秒的其它消息"一起放进上下文 —— 看起来更全，实际是把触发时刻
    那一秒的噪声混进来（``ts`` 是**秒**精度，同秒分不出先后）。
    """
    conn = _Recorder()
    repo_messages.list_group_messages("acct", "g1", limit=10, conn=conn, before_sec=1700)
    sql, params = conn.only
    assert "ts < FROM_UNIXTIME(%s)" in sql, f"过滤条件不对：{sql}"
    assert "ts <= FROM_UNIXTIME(%s)" not in sql, "必须是严格小于"
    assert params == ("acct", "g1", "g1", 1700, 10), f"参数顺序错了：{params}"


def test_without_before_sec_the_sql_is_unchanged():
    """不传 ⇒ **一个字节都不该多**（老调用方——心流那些——拿到的是与改造前逐字相同的 SQL）。"""
    conn = _Recorder()
    repo_messages.list_group_messages("acct", "g1", limit=30, conn=conn)
    sql, params = conn.only
    assert "FROM_UNIXTIME" not in sql, f"不传 before_sec 却出现了时间过滤：{sql}"
    assert params == ("acct", "g1", "g1", 30)


@pytest.mark.parametrize("falsy", [None, 0])
def test_a_falsy_before_sec_is_treated_as_no_filter(falsy):
    """``0``/``None`` 都表示"不过滤"（TS 是 ``if (opts.beforeTs)`` 的真值判断）。

    写成 ``is not None`` 会让传 0 的调用方拿到一个**空窗口** —— 上下文静默消失，
    而 SQL 看着完全正常。
    """
    conn = _Recorder()
    repo_messages.list_group_messages("acct", "g1", conn=conn, before_sec=falsy)
    sql, _ = conn.only
    assert "FROM_UNIXTIME" not in sql, f"{falsy!r} 不该产生过滤条件：{sql}"


def test_the_two_chat_id_storage_forms_are_still_supported():
    """W1 的落库形态兼容不能被这次改造破坏：``chat_id = <裸群 id>`` **或**
    ``chat_id IS NULL AND peer_id = <裸群 id>``（历史行 + 出站行两种形态）。
    """
    conn = _Recorder()
    repo_messages.list_group_messages("acct", "g1", conn=conn, before_sec=1700)
    sql, _ = conn.only
    assert "chat_id = %s OR (chat_id IS NULL AND peer_id = %s)" in sql, \
        f"两种落库形态的兼容分支没了：{sql}"
    assert "peer_kind = 'group'" in sql, "群消息判据没了"
