"""W2 装配回归：好感度**旁路**必须真的是旁路（2026-10-02）。

## 为什么这一面单独一套门

``plugin/tests/test_affection.py`` 验的是**纯逻辑**（17 条规则、10 种情绪、落库 SQL 形状、
编排），这一面验的是**它有没有被接上、接得对不对**。``adapter.py`` 本机 **import 不了**
（模块级 ``from gateway.config import Platform`` 是 Hermes 独有，见
``test_outbound_send_result.py:33-35``）⇒ 只能按 **AST 源码形状**验。

四条判据挑的都是**失效形态为"看着完全正常"** 的那种（这正是本树反复吃亏的地方）：

1. **``await`` 了它** ⇒ 收信链路被"DB 读改写 + 5s 超时的 LLM"拖住，现场表现只是
   "偶尔回得慢"，日志干净（TS 的 ``void processAffectionMessage`` 就是不许这个）。
   ⇒ §1 H2 不测"有没有调用"，测**没有任何 await 到这条路径上**。
2. **没挂 done-callback** ⇒ 记账异常只在 GC 时冒一句 "Task exception was never
   retrieved"，不说是哪个群、不说为什么。⇒ H4。
3. **任务句柄没存** ⇒ ``asyncio`` 只持**弱引用**，任务可能跑到一半被 GC 掉（本树踩过，
   见 ``adapter.__init__`` 的注释）。⇒ H4/H5。
4. **守卫写在读库之后** ⇒ 开关关着照样查库、甚至调 LLM（老板花钱、指标还好好的）。
   ⇒ H7 判的是**源码顺序**，不是"有没有守卫" —— 守卫写得再全，位置反了就是白写。

命令面（§2）的失败形态是另一种：**回得头头是道但数字是编的**。所以判据一律下在
"回执里那行数字等于桩的返回值"上，而不是"文案里有'数据'两个字"。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from plugin.platform import commands

#: 仓根（``plugin/tests/platform/...`` 往上三层）
ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"

ACC = "acct"


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


def _call_at(src: str, expr: str) -> int:
    """``expr(...)`` 或 ``asyncio.to_thread(expr, ...)`` 在源码里的位置。

    2026-10-18 起同步落库挪进了线程池；两种写法都认，位置判据不因此放宽。
    """
    for needle in (expr + "(", "to_thread(" + expr + ","):
        i = src.find(needle)
        if i >= 0:
            return i
    raise AssertionError(f"{expr} 在这段源码里没有调用点")


def _fn(name: str) -> ast.AST:
    m = _methods()
    assert name in m, f"{ADAPTER_CLS}.{name} 不在了 —— 改名了还是被删了？"
    return m[name]


def _calls_in(node: ast.AST, callee: str) -> list[ast.Call]:
    """``node`` 里所有 ``...callee(...)`` 调用（按 ``attr`` 名匹配）。"""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == callee:
            out.append(n)
    return out


# ============================================================ §1 装配：adapter

def test_h1_the_group_branch_notes_affection_beside_heartflow():
    """打分与心流**并列**挂在 ``if msg.is_group:`` 那个 body 里 —— 两者都与
    "这条消息有没有触发 AI"无关（TS ``handler.ts:620-642`` 的旁路语义）。

    ⇒ 判据：那个 ``if`` 的 body 同时含 ``_hf_note_group`` 与 ``_affection_note_group``。
    接到 ``if not ok:`` 里面（"只在触发时记"）或接到 DM 分支上都会当场红。
    """
    blocks = [n for n in ast.walk(_fn("_handle_one"))
              if isinstance(n, ast.If) and ast.unparse(n.test) == "msg.is_group"]
    assert blocks, "_handle_one 里的 `if msg.is_group:` 分支不在了"
    body = "\n".join(ast.unparse(s) for s in blocks[0].body)
    assert "self._hf_note_group(acct, msg, is_bot=False)" in body, "心流旁路不在这个分支里"
    assert "self._affection_note_group(acct, msg)" in body, \
        "好感度没接在群分支上 —— 开关开了也不会记一分"


def test_h2_nothing_on_that_path_is_awaited():
    """★ 核心判据：``_handle_one`` 里**没有任何** ``await`` 到 affection 的调用。

    ``await self._affection_note_group(...)`` 与 ``await self._affection_run(...)``
    都是致命的：前者把记账塞进收信链路，后者更糟（连 task 都不建）。
    这里连 ``asyncio.gather``/``wait_for`` 兜着算异步的写法一起否掉 ——
    凡是 ``Await`` 节点的源码里出现 affection，就是违规。
    """
    fn = _fn("_handle_one")
    bad = [ast.unparse(n) for n in ast.walk(fn)
           if isinstance(n, ast.Await) and "affection" in ast.unparse(n).lower()]
    assert not bad, f"好感度被 await 了（收信链路会被 DB/LLM 拖住）：{bad}"

    # 反面自证：判据本身要能抓到违规形态（否则"没 await"可能只是没搜对）
    probe = ast.parse("async def f():\n    await self._affection_note_group(acct, msg)\n")
    hits = [n for n in ast.walk(probe)
            if isinstance(n, ast.Await) and "affection" in ast.unparse(n).lower()]
    assert len(hits) == 1, "判据形状写错了 —— 它抓不到真违规"


def test_h3_it_sits_between_the_drop_gate_and_the_runner():
    """顺序：``should_drop`` 闸 → 落库（W1）→ 心流/好感度旁路 → ``handle_message``。

    两个方向都错得很难看：**早于** ``should_drop`` ⇒ 黑名单群/红包/撤回也计分
    （隐私口径破了，且没触发的垃圾消息在给人加分）；**晚于** ``handle_message``
    ⇒ 被 ``return`` 掉的路径（心流未接管）根本走不到，等于随机丢账。
    """
    src = _code(_fn("_handle_one"))
    order = [src.index("should_drop("),
             _call_at(src, "persist_inbound"),
             src.index("self._affection_note_group(acct, msg)"),
             src.index("await self.handle_message(event)")]
    assert order == sorted(order), f"装配顺序错了：{order}"


def test_h4_the_task_is_strongly_referenced_and_has_a_done_callback():
    """``create_task`` 之后必须**两件事都做**：存句柄 + 挂 done-callback。

    少了存句柄 ⇒ 事件循环只持弱引用，任务可能跑到一半被 GC（静默丢账）；
    少了回调 ⇒ 异常变成 GC 时的 "Task exception was never retrieved"。
    """
    src = _code(_fn("_affection_note_group"))
    for needle in ("asyncio.create_task(", "self._affection_tasks.add(task)",
                   "task.add_done_callback(self._affection_done)"):
        assert needle in src, f"装配缺一块：{needle}"

    # 建 task 本身也要在 try 里 —— 起不来（循环已关 / 名字重复）绝不能带崩收信
    assert "try:" in src and "log.warning" in src

    done = _fn("_affection_done")
    dsrc = _code(done)
    assert "self._affection_tasks.discard(task)" in dsrc, "句柄不丢 ⇒ 集合只涨不落"
    assert "task.exception()" in dsrc, "不取异常 ⇒ 失败无声（连日志都没有）"
    assert "log.warning" in dsrc
    assert "raise" not in dsrc, "收尾回调再抛 ⇒ 只能变成循环的 unhandled exception 噪音"


def test_h5_the_handle_holder_is_created_in_init():
    """集合在 ``__init__`` 里建（不是类属性 —— 类属性会被所有实例共享，多账号串账）。"""
    init = _fn("__init__")
    assert any(isinstance(n, ast.AnnAssign) and "affection" in ast.unparse(n).lower()
               for n in init.body), f"__init__ 里没建任务集合：{_code(init)[:200]}"
    src = _code(_fn("_affection_note_group"))
    assert "self._affection_tasks" in src


def test_h6_the_mood_goes_to_channel_prompt_not_into_the_text():
    """情绪注入走 ``MessageEvent.channel_prompt=``（Hermes 的临时 system prompt 缝）。

    ⇒ 判据：``_build_event`` 的 ``MessageEvent(...)`` 调用里有 ``channel_prompt``
    关键字，且取的是合并入口 ``self._channel_prompt(acct, msg)``（2026-10-18 起它把
    情绪与群画像合成一个字符串；直接传 mood 会让画像进不了回复侧）。
    ``_channel_prompt`` 内部必须仍然调用 ``_mood_channel_prompt`` —— 那一条由
    ``test_group_profile_wiring.py`` 钉住。
    **不许**出现在 ``text=`` 上 —— 情绪说明一旦进正文，就成了"用户说过的话"进 transcript。
    """
    calls = [n for n in ast.walk(_fn("_build_event"))
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "MessageEvent"]
    assert len(calls) == 1, f"MessageEvent 构造点不止一个/找不到：{len(calls)}"
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kw.get("channel_prompt") == "self._channel_prompt(acct, msg)", \
        f"channel_prompt 没接上：{kw.get('channel_prompt')!r}"
    assert kw.get("text") == "text", "正文被动了 —— 情绪不许进正文"


def test_h7_the_disabled_guards_run_before_any_io():
    """★ 开关关着 ⇒ **一次 DB、一次 LLM 都不碰**（源码顺序即判据）。

    ``_mood_channel_prompt`` 同理：非群 / 没开开关时必须在 ``get_mood`` **之前**返回
    ``None`` —— 否则每条私聊都要查一次库，而"没这个功能"的请求体也不再逐字节相同。

    光有守卫不算数：守卫写在读库**后面**，功能一样是"关着也在花钱"，而现场看不出来。
    """
    note = _code(_fn("_affection_note_group"))
    assert note.index("if not cfg.enabled") < note.index("asyncio.create_task("), \
        "enabled 守卫在 create_task 之后 ⇒ 关着开关照样建任务（DB/LLM 照跑）"
    for guard in ("msg.msg_type == MT_SYSTEM", "msg.from_wxid == acct.self_wxid",
                  "if not content.strip()"):
        assert guard in note, f"缺守卫：{guard}"

    mood = _code(_fn("_mood_channel_prompt"))
    assert mood.index("if not msg.is_group") < mood.index("resolve_affection_cfg")
    assert mood.index("if not cfg.enabled") < mood.index("get_mood("), \
        "enabled 守卫在 get_mood 之后 ⇒ 没开开关也每条消息查一次库"


def test_h8_the_mood_prompt_only_ever_returns_none_or_our_prefix():
    """回 ``None`` ⇒ 调用方不传关键字参数 ⇒ 请求体与"没这个功能"**逐字节相同**。

    所以这个函数**只许**有两种出口：``None``，或带 ``[系统提示-当前情绪]`` 前缀的串。
    任何第三种返回（比如原样回 base prompt、或抛出去）都会让"没开开关"的请求体变样。
    """
    rets = [n for n in ast.walk(_fn("_mood_channel_prompt")) if isinstance(n, ast.Return)]
    assert rets, "一个 return 都没有？"
    for r in rets:
        if r.value is None or (isinstance(r.value, ast.Constant) and r.value.value is None):
            continue
        un = ast.unparse(r.value)
        assert "系统提示-当前情绪" in un, f"出现了第三种出口：{un[:120]}"

    # 取库那一段必须在 try 里（TS ``dispatcher.ts:866-870`` 同款吞错）
    trys = [n for n in ast.walk(_fn("_mood_channel_prompt")) if isinstance(n, ast.Try)]
    assert trys, "没有 try ⇒ 库一坏，整条收信路径被情绪注入带崩"


def test_h9_the_mood_prompt_has_exactly_one_call_site():
    """``_mood_channel_prompt`` 全模块只许有 1 个调用点（``_build_event``）。

    多出来的调用点几乎一定是"顺手塞进 text/日志"，那正是上面 H6 要挡的形态；
    而且多一个调用点 = 多一次每次收信的 DB 查询。
    """
    tree = ast.parse(_src(ADAPTER_REL))
    sites = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute)
             and n.attr == "_mood_channel_prompt"]
    assert len(sites) == 1, f"调用点/引用点不止一个：{len(sites)}（定义点 1 个属正常）"


def test_h10_the_w2_invariant_is_marked_in_its_source_home():
    """本树约定：每个工作项在自己的老家留一行 ``INVARIANT: Wn``（同 W1/W6）。

    这不是装饰 —— 它让"这一面为什么这样接"能在源码里读到，而不是只存在于测试文件。
    """
    src = _src("plugin/inbound/affection.py")
    assert "INVARIANT: W2" in src
    for word in ("不 await", "只 warn", "零副作用"):
        assert word in src, f"W2 不变量少了一条：{word}"


# ============================================================ §2 命令面：/affection status

@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """账号目录隔离（状态命令虽不写盘，也绝不指向真实账号文件）。"""
    d = tmp_path / "accounts"
    d.mkdir()
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_DIR", str(d))
    monkeypatch.delenv("WECHATPRO_ACCOUNTS_JSON", raising=False)
    yield d


def _stub_counts(monkeypatch, groups: int, rows: int) -> None:
    from plugin.data import repo_affection as repo

    monkeypatch.setattr(repo, "count_groups_with_state", lambda *a, **k: groups)
    monkeypatch.setattr(repo, "count_level_rows", lambda *a, **k: rows)


def test_affection_status_reports_the_real_counts(monkeypatch):
    """数字必须是**桩的返回值**，不是重算出来的 —— 重算会跟着实现一起错。"""
    _stub_counts(monkeypatch, 3, 7)
    reply = commands.handle_command(ACC, "/affection status")
    assert "有状态的群 3 个 / 好感度行 7 条" in reply, reply


def test_affection_status_with_nothing_recorded_says_why(monkeypatch):
    """0 行时不是干巴巴的 "0"：要顺带说清"为什么可能是 0"（没过触发判定的群消息不计分）。

    少了这句，运维看到 0 会以为功能坏了（而它其实只是没说过话）。
    """
    _stub_counts(monkeypatch, 0, 0)
    reply = commands.handle_command(ACC, "/affection status")
    assert "暂无" in reply and "触发判定" in reply, reply


def test_affection_status_survives_a_broken_repo(monkeypatch):
    """状态页在**库坏掉**那一刻最有用 ⇒ 读失败只能降一句人话，绝不抛（TS 同款 8 个 try）。"""
    from plugin.data import repo_affection as repo

    def boom(*a, **k):
        raise RuntimeError("DB down")

    monkeypatch.setattr(repo, "count_groups_with_state", boom)
    monkeypatch.setattr(repo, "count_level_rows", boom)
    reply = commands.handle_command(ACC, "/affection status")
    assert "读取失败，见日志" in reply, reply
    assert reply.startswith("/affection 状态:"), reply


def test_only_affection_status_pays_for_the_counts(monkeypatch):
    """★ 这两次数**只属于** ``/affection status``。

    接到通用分支上会让 ``/jargon status`` 也去查好感度表（白一次查询，
    更糟的是把两个功能的状态页**绑在一起**：好感度表没建时，黑话开关的状态页也报"读取失败"）。
    """
    from plugin.data import repo_affection as repo

    calls: list[str] = []

    def spy(name):
        def _f(*a, **k):
            calls.append(name)
            return 0
        return _f

    monkeypatch.setattr(repo, "count_groups_with_state", spy("groups"))
    monkeypatch.setattr(repo, "count_level_rows", spy("rows"))

    jargon = commands.handle_command(ACC, "/jargon status")
    assert "数据:" not in jargon, f"/jargon status 不该报好感度数据：{jargon}"
    assert calls == [], f"/jargon status 查了好感度表：{calls}"

    aff = commands.handle_command(ACC, "/affection status")
    assert calls == ["groups", "rows"], calls
    assert "数据:" in aff


def test_help_and_plain_toggle_are_untouched(monkeypatch):
    """回归护栏：``/affection`` 无参数 / 非法参数时**不查库**（那是开关文案，不是状态页）。"""
    from plugin.data import repo_affection as repo

    calls: list[str] = []
    monkeypatch.setattr(repo, "count_groups_with_state",
                        lambda *a, **k: calls.append("g") or 0)
    monkeypatch.setattr(repo, "count_level_rows", lambda *a, **k: calls.append("r") or 0)

    assert "当前状态" in commands.handle_command(ACC, "/affection")
    assert "参数: on/off/status" in commands.handle_command(ACC, "/affection bogus")
    assert calls == [], calls
