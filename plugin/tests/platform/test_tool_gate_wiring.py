"""W7-5 装配回归：运行期门禁**接在哪儿**、``/xiaowei`` **写的是不是门禁读的那把钥匙**、
以及那两个"开关在、门不在"的老毛病（白名单读不到分支 / 读不到说成没开）。

## 为什么这一面单独一套门

``plugin/tests/test_tool_gate.py`` 验的是**行为**：真 ``register()``、真派发、厂商调用记数、
开关翻转后同一份注册的行为跟着变。这一面验的是**源码形状**，它们的失效形态全都是
"测试照样绿"：

1. **门禁接错层** —— ``wrap_ctx(ctx)``（丢掉第二个实参）能编译、能注册、299 个工具照旧
   可调，只是 ``/xiaowei off`` 从此刻起**是一个谎开关**。⚠️ 这条**行为上**已被
   ``test_tool_gate.py`` 覆盖（真派发会被挡）；这里再钉一次**形状**，因为它是
   "谎开关"最直接的那一根线。
2. **钥匙写错** —— ``/xiaowei`` 写 ``xiaowei_enabled``（snake_case）而门禁读
   ``xiaoweiEnabled`` ⇒ 命令回 ✅、工具永远拒绝、**两边各自的单测都绿**。
3. **`adapter.get_account`** —— 适配器上**没有这个方法**（全树只在那 4 行出现过）
   ⇒ 真机 ``AttributeError`` ⇒ 白名单那一支**永远走不到**，等于"任何调用方都能发朋友圈"。
   这条必须按 AST 扫**全树**（连注释一起看着像"有"）。
4. **白名单域与触发器读的键必须同值** —— 命令写 ``blacklist``、触发器读
   ``blacklistGroups``，各测各的都绿，真机上黑名单**一条也不生效**。

⚠️ 两条 AST 上的注意（同 ``test_relay_wiring.py``）：``ast.unparse`` 把字符串印成
**单引号**；判"谁在谁前面"一律用 ``lineno``。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REGISTRATION_REL = "plugin/registration.py"
TOOL_REGISTRATION_REL = "plugin/toolkit/registration.py"
CONTRACT_REL = "plugin/toolkit/hermes_contract.py"
POLICY_REL = "plugin/toolkit/policy.py"
MSG_REL = "plugin/toolkit/msg.py"
COMMANDS_REL = "plugin/platform/commands.py"
TRIGGERS_REL = "plugin/inbound/triggers.py"
ACCOUNTS_REL = "plugin/core/accounts.py"


# ============================================================ 源码工具（AST）


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _tree(rel: str) -> ast.Module:
    return ast.parse(_src(rel))


def _fn(rel: str, name: str) -> ast.AST:
    for node in _tree(rel).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{rel} 里找不到 {name}")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Attribute) and n.func.attr == name)
                 or (isinstance(n.func, ast.Name) and n.func.id == name))]


def _const_strs(node: ast.AST) -> set[str]:
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _all_py(exclude_tests: bool = True) -> list[Path]:
    out = []
    for p in sorted((ROOT / "plugin").rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if exclude_tests and "/tests/" in rel:
            continue
        out.append(p)
    return out


# ============================================================ ① 门禁挂在唯一的 seam 上


def test_the_gate_is_installed_at_the_one_seam_every_tool_passes() -> None:
    """★ ``register_tools()`` 必须把 ``make_tool_gate(...)`` 交给 ``wrap_ctx``。

    ``wrap_ctx`` 是 W0 为调用契约收的口 —— **所有工具、所有注册路径**都过它。
    门禁挂在这里，将来新增的工具忘不掉；挂在别处（比如某个注册子步骤里）就会
    漏掉别的路径，而漏掉的那条路**没有任何症状**。
    """
    calls = _calls(_fn(TOOL_REGISTRATION_REL, "register_tools"), "wrap_ctx")
    assert len(calls) == 1, f"wrap_ctx 在 register_tools() 里出现 {len(calls)} 次"
    call = calls[0]
    assert len(call.args) == 2, f"wrap_ctx 少了一个实参（门禁被丢了）：{ast.unparse(call)}"
    assert ast.unparse(call.args[1]).startswith("_policy.make_tool_gate("), ast.unparse(call)


def test_the_gate_getter_is_lazy_so_it_can_still_be_patched() -> None:
    """★ getter 必须是 ``lambda: _adapter._get_adapter()``，**不能**传函数对象。

    传 ``_adapter._get_adapter`` 会把这个**当时**的函数对象绑进闭包里 ⇒ 单测/探针在
    ``register()`` 之前 monkeypatch 模块属性就落空了（补丁打了、测的还是真适配器）。
    W0 的契约判据也依赖同一件事：晚绑定。
    """
    src = _src(TOOL_REGISTRATION_REL)
    assert "lambda: _adapter._get_adapter()" in src, \
        "门禁 getter 必须每次调用重新取模块属性（写死函数对象会让 monkeypatch 落空）"


def test_there_is_exactly_one_place_that_wraps_the_registration_ctx() -> None:
    """★ 全树只有 ``toolkit.registration.register_tools`` 一处调 ``wrap_ctx``。

    第二个调用点（或某个子注册函数自己包一层）会让"包里层的那个 ctx"漏掉门禁或
    漏掉契约适配 —— 而漏掉的那批工具只是"点下去报 TypeError"或"开关关着还能调"。
    """
    hits = []
    for path in _all_py():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            # 两种写法都要认：``wrap_ctx(ctx, …)`` 与 ``_contract.wrap_ctx(ctx, …)``
            # （只认裸名会让这条判据恒真 —— 真调用点恰恰是带前缀的那种）。
            if isinstance(node, ast.Call) and (
                    getattr(node.func, "id", "") == "wrap_ctx"
                    or getattr(node.func, "attr", "") == "wrap_ctx"):
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert len(hits) == 1 and hits[0].startswith(TOOL_REGISTRATION_REL + ":"), \
        f"wrap_ctx 的调用点不止一处：{hits}"


# ============================================================ ② guarded_handler 的形状


def test_guarded_handler_keeps_the_w0_contract_shape() -> None:
    """★ 门禁包装后的 handler 必须**仍然满足 W0 的契约三条**。

    这条最阴的失效形态是"顺手设个 ``functools.wraps`` / ``__wrapped__``" ——
    ``inspect.signature`` 默认**穿透** ``__wrapped__``，Hermes 会读到**原函数**的
    ``(**params)`` 签名 ⇒ dispatch 上下文键（``task_id``/``session_id``/``user_task``）
    被放行、原样进厂商请求体（见 :mod:`plugin.toolkit.hermes_contract` 的 docstring）。
    """
    import asyncio

    from plugin.toolkit import hermes_contract as hc

    ran: list[str] = []

    async def orig(**params):
        ran.append("orig")
        return {"ok": True}

    def reject(_args):
        return "拒绝"

    for pre, expected in ((reject, "拒绝"), (lambda _a: None, '{"ok": true}')):
        wrapped = hc.guarded_handler(hc.adapt_handler(orig), pre)
        # ① 签名只有 args、**不声明 **kw
        params = inspect.signature(wrapped).parameters
        assert list(params) == ["args"], f"包装后签名变了：{list(params)}"
        assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        # ② 不设 __wrapped__（设了 signature 会穿透）；原函数挂在非协议名上
        assert not hasattr(wrapped, "__wrapped__"), "__wrapped__ 会让 signature 穿透"
        assert inspect.iscoroutinefunction(wrapped) is inspect.iscoroutinefunction(orig)
        # ③ __original_handler__ 指向**最里层**的原函数（W0 的判据按它比）
        assert getattr(wrapped, "__original_handler__", None) is orig
        # ④ 行为：pre 说了话就**不执行**（判据在原函数**没跑**上，不是"返回了拒绝"），
        #    否则照常执行、结果走 Hermes 的结果契约（dict ⇒ JSON 串）
        before = len(ran)
        assert asyncio.run(wrapped({})) == expected
        assert len(ran) == (before if expected == "拒绝" else before + 1), \
            "pre 已经在调用前说话了，原函数**一次都不该执行**"


def test_the_gate_only_wraps_the_tools_it_claims() -> None:
    """不受管的工具**一个包装都不加** —— 299 个里只有 14 个多一层。

    ``_ToolCtx.register_tool`` 只有在门禁工厂回了非 ``None`` 时才包 —— 这条钉的是
    "工厂回 None 就不包"（回一个恒 ``None`` 的 pre 会白白多 247 层间接，
    并且让 W0 那套"handler 形状"判据多出 247 个对象要过）。
    """
    from plugin.toolkit import hermes_contract as hc

    class _Ctx:
        def __init__(self):
            self.tools: dict[str, dict] = {}

        def register_tool(self, **kw):
            self.tools[kw["name"]] = kw

    def _run(name: str, gate) -> dict:
        inner = _Ctx()
        hc.wrap_ctx(inner, gate).register_tool(
            name=name, handler=lambda **_p: "真的跑了", is_async=False)
        return inner.tools[name]

    gate = lambda n: (lambda _a: None) if n == "受管" else None  # noqa: E731
    #: 判"多没多一层"用**代码对象**：``adapt_handler`` 与 ``guarded_handler`` 各自的
    #: 内层 ``def handler`` 是**模块编译期**定下来的两个不同 code object ⇒ 同一个 code
    #: 就是同一层，换了一个就是多包了一层。比"看有没有某个属性"实在（两层的属性一样）。
    plain = {n: _run(n, None) for n in ("受管", "不受管")}
    gated = {n: _run(n, gate) for n in ("受管", "不受管")}
    assert gated["不受管"]["handler"].__code__ is plain["不受管"]["handler"].__code__, \
        "不受管的工具被多包了一层（工厂回 None 就该原样）"
    assert gated["受管"]["handler"].__code__ is not plain["受管"]["handler"].__code__, \
        "受管的工具没有被包装 —— 门禁挂了等于没挂"
    #: 名字/异步形状由两层各自照抄真身，包装不该改变它们（Hermes 按 ``is_async`` 决定 await）
    for n in ("受管", "不受管"):
        assert gated[n]["is_async"] is False, n
        assert gated[n]["handler"].__name__ == plain[n]["handler"].__name__ == "<lambda>", n


# ============================================================ ③ msg.py 的四处守卫


def test_no_one_calls_the_nonexistent_adapter_get_account() -> None:
    """★ 全树（排除测试）**一处都不许**再出现 ``…get_account(…)`` **调用**。

    改回 ``adapter.get_account(aid)`` 的后果不是报错，而是**白名单静默失效**：
    真机抛 ``AttributeError`` → 工具回「调用失败: 'WppAdapter' object has no
    attribute 'get_account'」→ 那一支（无权限判定）**永远走不到** ⇒ 任何调用方都能发朋友圈。
    注释里提到这个名字是允许的（那是记录），所以按 **AST 的 Call** 扫，不按文本扫。
    """
    hits = []
    for path in _all_py():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get_account":
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not hits, f"还在调不存在的适配器方法：{hits}"


def test_all_four_publish_guards_read_the_config_through_policy() -> None:
    """四处发布守卫必须都走 ``policy.raw_account_cfg``，且**每处都 fail-closed**。

    两件事一起钉：① 读配置的路只有一条（``policy``）—— 少一处就是"有一个工具能绕过"；
    ② 读不到时**拒绝**（``if acct_cfg is None: return …``）—— 少一处就是 fail-open。
    """
    from plugin.toolkit import policy

    reads = [c for c in _calls(_tree(MSG_REL), "raw_account_cfg")
             if ast.unparse(c.func) == "policy.raw_account_cfg"]
    assert len(reads) == 4, f"发布守卫读配置的点有 {len(reads)} 个（应为 4）"
    guards = [n for n in ast.walk(_tree(MSG_REL))
              if isinstance(n, ast.If) and ast.unparse(n.test) == "acct_cfg is None"
              and any(isinstance(s, ast.Return) for s in n.body)]
    assert len(guards) == 4, f"读不到配置时**拒绝**的分支有 {len(guards)} 个（应为 4）"
    #: 门禁那条**开关**在 policy（运行期门禁），工具里只剩"按调用方"的白名单 ⇒
    #: 工具里不许再自己判 ``friendCirclePublishEnabled``（两处判同一个开关 = 两套口径）。
    assert "friendCirclePublishEnabled" not in _const_strs(_tree(MSG_REL)), \
        "开关判定应收口到 policy 的运行期门禁，工具里只留白名单"


def test_the_gate_key_is_the_camel_case_one_the_account_files_use() -> None:
    """门禁读的键名必须与账号文件里的写法**逐字**一致（``xiaoweiEnabled``）。"""
    from plugin.toolkit import policy

    assert policy.XIAOWEI_GATE.key == "xiaoweiEnabled"
    assert policy.FRIEND_CIRCLE_PUBLISH_GATE.key == "friendCirclePublishEnabled"


# ============================================================ ④ 命令面接线


def test_both_new_commands_are_routed() -> None:
    """``/blacklist`` 与 ``/xiaowei`` 必须真的接进 ``handle_command``。

    ``/blacklist`` 走**三个白名单域共用的那条**（域名与命令名同形），``/xiaowei``
    走自己的分支。少任何一条，命令就落进兜底分支（回一句 HELP 或空串）——
    老板看到的是"这命令不存在"。
    """
    body = _fn(COMMANDS_REL, "handle_command")
    tests = {ast.unparse(n.test) for n in ast.walk(body) if isinstance(n, ast.If)}
    # ``ast.unparse`` 把这里的字符串印成**单引号** —— 期望值跟着它写
    assert "cmd.lstrip('/') in _ALLOWLIST_KEYS" in tests, tests
    assert "cmd == '/xiaowei'" in tests, tests


def test_all_three_allowlist_domains_share_one_implementation() -> None:
    """★ 三个域**共用**一份批量实现（TS 也是收成一个 ``handleWhitelistCommand``）。

    分成三份的坏处不是代码多，而是**口径会分叉**：``/user`` 修好了批量、
    ``/group`` 还是单条静默丢弃 —— 而两条命令的回话长得一样。
    """
    tree = _tree(COMMANDS_REL)
    defined = [n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_handle_allowlist"]
    assert len(defined) == 1, "只该有一份 _handle_allowlist"
    called = _calls(_fn(COMMANDS_REL, "handle_command"), "_handle_allowlist")
    assert len(called) == 1, f"handle_command 里调了 {len(called)} 次（三个域共用一次）"
    assert ast.unparse(called[0].args[1]) == "cmd.lstrip('/')", ast.unparse(called[0])

    keys = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_ALLOWLIST_KEYS":
            keys = ast.literal_eval(node.value)
    assert keys == {"user": "allowFrom", "group": "groupAllowFrom",
                    "blacklist": "blacklistGroups"}, keys


def test_the_blacklist_key_is_the_same_string_the_triggers_read() -> None:
    """★ 命令写的键 == 触发器读的键。

    ``commands`` 写 ``blacklist``、触发器读 ``blacklistGroups`` 时，两边各自的单测
    都绿（一边测"写进 cfg 了"、一边测"cfg 里有就拦"），而真机上黑名单
    **一条也不生效** —— 这正是"配置键在、消费者看不到"的同族。"""
    tree = _tree(COMMANDS_REL)
    keys = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_ALLOWLIST_KEYS":
            keys = ast.literal_eval(node.value)
    written = keys["blacklist"]
    assert written in _const_strs(_tree(TRIGGERS_REL)), \
        f"命令写 {written!r}，而 triggers.py 里根本没有这个常量"
    assert written in _const_strs(_tree(ACCOUNTS_REL)), \
        f"命令写 {written!r}，而 accounts.py 不把它解析进 Account"


def test_the_three_write_bases_are_whole_values_not_deltas() -> None:
    """★ 写回是**整体替换那个键** ⇒ 交出去的必须是"合并后的完整值"。

    写成增量（``write_back(…, key, changed)`` / ``{…, "enabled": want}`` 少一个
    ``**base``）会**清掉同级内容**：``/affection off`` 把整个 ``affection`` 段
    （含阈值、白名单）换成 ``{"enabled": false}``，而回话是「✅ 已关闭」。
    这是 W7-5 顺手关掉的一个**真·数据丢失**隐患（底稿原先来自会 raise 的
    ``resolve()``），所以三处都逐字钉住。
    """
    tree = _tree(COMMANDS_REL)
    calls = {ast.unparse(c.args[1]): ast.unparse(c.args[2])
             for c in _calls(tree, "write_back")}
    assert calls.get("key") == "new_items", calls
    assert calls.get("feature") == "{**base, 'enabled': want}", calls
    assert calls.get("'xiaoweiEnabled'") == "want", calls


def test_the_write_bases_never_come_from_resolve() -> None:
    """★ 三个"底稿"读函数**不许**经 ``resolve()``（那条路失败时返回空 dict）。

    空底稿 + 整体替换 = 一条命令清空现有内容（详见 :mod:`plugin.platform.commands`
    的 W7-5 一节）。按 AST 扫这三个函数的**调用**，不扫文本 —— 注释里正解释着这件事。
    """
    for name in ("_account_file_cfg", "_allowlist_items", "_feature_cfg"):
        names = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                 for n in _calls(_fn(COMMANDS_REL, name), "resolve")}
        assert not names, f"{name} 里出现了 resolve(…) —— 底稿会退化成空 dict"


def test_the_xiaowei_command_writes_the_key_the_gate_reads() -> None:
    """★ 钥匙与锁是同一把：命令写的字面量 == ``policy.XIAOWEI_GATE.key``。

    这是本条最值得用**跨模块**判据的一条：两个模块各自的单测都只钉自己那半
    （命令写了个字符串、门禁读了个字符串），差一个字母时**两边都绿**，
    而现象是"``/xiaowei on`` 回 ✅、工具永远拒绝"。
    """
    from plugin.toolkit import policy

    tree = _tree(COMMANDS_REL)
    written = set()
    for node in ast.walk(_fn(COMMANDS_REL, "_handle_xiaowei")):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "write_back":
            written.add(ast.literal_eval(node.args[1]))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_flag_state":
            written.add(ast.literal_eval(node.args[1]))
    assert written == {policy.XIAOWEI_GATE.key}, \
        f"命令读/写的键 {written} 与门禁读的 {policy.XIAOWEI_GATE.key!r} 不一致"


def test_the_help_text_lists_both_new_commands() -> None:
    """帮助文本必须列出来 —— 命令的**唯一**发现途径就是它。"""
    src = _src(COMMANDS_REL)
    assert "/xiaowei on|off|status — 小微智能体工具开关（关着时相关工具直接拒绝执行）" in src
    assert "/blacklist add|del|list [群id...] — 黑名单群（一票否决，优先于群白名单）" in src


def test_status_pages_have_a_third_state_for_unreadable() -> None:
    """★ ``state_label`` 必须是**三态**（``None`` ⇒ 读不到），且两个显示族都过它。

    ``✅ 开启/❌ 关闭`` 二值化会把"账号文件坏了"印成"关闭"，老板于是去做
    ``/heartflow on``（写一份新配置到读不出来的账号上），真故障被这句话盖住。
    """
    from plugin.platform import commands

    assert commands.state_label(None) == commands.UNREADABLE_STATE_MSG
    assert commands._state_word(None) == commands.UNREADABLE_STATE_MSG
    body = _fn(COMMANDS_REL, "_handle_feature_toggle")
    names = {ast.unparse(n.func) for n in ast.walk(body) if isinstance(n, ast.Call)}
    assert "_state_word" in names, "``/affection`` 那两条显示还自己拼 on/off"
