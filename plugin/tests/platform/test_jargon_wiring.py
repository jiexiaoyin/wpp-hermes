"""W7-4 装配回归：黑话旁路**接在哪儿、开不开火、跑起来的代价会不会压到收信链上**。

## 为什么这一面单独一套门

``plugin/tests/test_jargon.py`` 验的是**逻辑本身**（分词打分、prompt 逐字、三步降级、
store 契约）。这一面验的是**适配器有没有把它接上、接在哪一步、拿的是哪份配置** ——
``adapter.py`` 本机 **import 不了**（模块级 ``from gateway.config import Platform`` 是
Hermes 独有）⇒ 只能按 **AST 源码形状**验。

五条判据挑的都是**失效形态为"看着完全正常"** 的那种：

1. **④b' 的排位** —— 夹在 ④a（心流缓冲）与 ④b（好感度）之间。TS 的批内次序是
   ``recordRawMessage``(570) → **JARGON**(589) → AFFECTION(620)。排到 ④d 之前 ⇒ 采到的是
   原始那坨 ``#接龙 …``；排到 ⑤ 之后 ⇒ 每条消息都晚一步、且"只在触发时采集"会把
   不触发的那批全丢掉（TS 是**每句人话**都算数）。两种错法都不报错。
2. **两个循环必须还是两个循环** —— TS 的采集面（排除系统通知 + 空正文）与触发面
   （**只**排除 blocked 与非群）是**两段**代码。合并成一个循环的后果极轻（晚一条消息才挖），
   但那是**替 TS 改了行为**；更要紧的是合成一处后，"系统通知也吃一次挖掘判据"这个
   TS 特征会静默消失。
3. **挖掘必须 fire-and-forget 且任务被强引用** —— 一轮最多 16 次 LLM 调用（1 次批量验证
   + 5 个候选 × 3 步推断）。``await`` 它 = 把整段网络往返塞进收信链路；不存句柄 =
   事件循环只持弱引用 ⇒ 任务可能被 GC 掉且**不留痕迹**（``add_done_callback`` 之前就没了）。
4. **配置只有一个家**：``resolve_jargon_cfg(acct.jargon)``。jargon 独有的三个键
   （``mineIntervalSec`` / ``minMessages`` / ``maxCandidatesPerGroup``）在 ``adapter.py``
   里**连字符串字面量都不许出现** —— 出现了就是第二处解析，两处一旦不同步，
   现象只是"到点挖的节流不太对"，永远查不出来（W3 的 ``intent-adapter`` 同款判据）。
5. **store 按账号建** —— ``RepoJargonStore(account_id)``，而 ``account_id`` 来自
   ``acct.id``。写成 ``"default"`` 会让多账号共写一个命名空间（表里有 ``account_id`` 列，
   但过滤是假的）：A 号挖的词被 B 号当成"已确认"跳过，A 号也再也挖不到自己的词。

⚠️ 两条 AST 上的注意（与 ``test_relay_wiring.py`` 同一族）：``ast.walk`` 是**广度优先、
不是源码顺序** ⇒ 判"谁在谁前面"一律用 ``lineno``；``ast.unparse`` 把下标/字符串印成
**单引号** ⇒ 期望值别写成双引号。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"
MINE_REL = "plugin/inbound/jargon_mine.py"

#: jargon 独有的配置键 —— 解析只许在 ``jargon_mine.py`` 里（判据 4）
JARGON_ONLY_KEYS = ("mineIntervalSec", "minMessages", "maxCandidatesPerGroup")


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


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    """子树里所有 ``name(...)``（``self._jargon_note_group`` 与模块级
    ``jargon_mine.resolve_jargon_cfg`` 两种写法都要认）。"""
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


def _is_inside(inner: ast.AST, outer: ast.AST) -> bool:
    """``inner`` 是否落在 ``outer`` 的子树里（按对象身份找，``ast.walk`` 广度优先不影响）。"""
    return any(n is inner for n in ast.walk(outer))


# ============================================================ 判据 1：④b' 的排位


def test_the_bypass_runs_once_per_group_message_and_only_for_groups() -> None:
    """一处一行，且**在 ``if msg.is_group:`` 里面**。

    排到那道闸外面 ⇒ 私聊消息也去采黑话（``group_id`` 会退化成 ``peer_id``，
    统计表里多出一堆"假群"），而 ``record_message``/``update_from_message`` 都不报错。
    """
    one = _methods()["_handle_one"]
    calls = _calls(one, "_jargon_note_group")
    assert len(calls) == 1, f"_handle_one 里 _jargon_note_group 出现 {len(calls)} 次"
    guard = _if(one, "msg.is_group")
    assert _is_inside(calls[0], guard), "_jargon_note_group 必须在 `if msg.is_group:` 里面"


def test_the_bypass_sits_between_the_hf_and_affection_bypasses() -> None:
    """★ 夹在 ④a 与 ④b **之间** —— 位置判据对齐 TS ``handler.ts`` 的批内次序
    （570 心流 → 589 黑话 → 620 好感度）。

    排到 ④a 之前 ⇒ 心流缓冲里的正文还没被黑话采过（无害，但次序不符）；
    排到 ④b 之后 ⇒ 与 TS 的批内次序不符，且"黑话只在好感度之后才动"没有任何依据。
    两条旁路互不相干 ⇒ 顺序错了**不报错、不丢数据**，只与 TS 分叉。
    """
    one = _methods()["_handle_one"]
    i_jargon = _line(_calls(one, "_jargon_note_group"))
    assert i_jargon > _line(_calls(one, "_hf_note_group")), \
        "黑话旁路必须排在 ④a 心流缓冲之后（TS 570 → 589）"
    assert i_jargon < _line(_calls(one, "_affection_note_group")), \
        "黑话旁路必须排在 ④b 好感度之前（TS 589 → 620）"


def test_the_bypass_sits_after_drop_persist_and_relay() -> None:
    """必须排在 ④ ``should_drop``、④z ``persist_inbound``、④d ``apply_relay_render``
    **之后**。

    * 早于 ``should_drop`` ⇒ 黑名单群/非白名单群的消息也进统计（"隐私"那条注释成空话）；
    * 早于 ``persist_inbound`` ⇒ 与 TS 的"先落库再旁路"不符；
    * 早于 ``apply_relay_render`` ⇒ 采到的是原始那坨 ``#接龙 …``，而 TS 采的是改写后的
      结构（这条是 W7-4 排位时与 W7-3 一起核过的：TS 的池是进程内缓冲，写的是改写后的正文）。
    """
    one = _methods()["_handle_one"]
    i_jargon = _line(_calls(one, "_jargon_note_group"))
    assert i_jargon > _line(_calls(one, "should_drop"))
    assert i_jargon > _line(_calls(one, "persist_inbound"))
    assert i_jargon > _line(_calls(one, "apply_relay_render"))


# ============================================================ 判据 2：两个循环


def test_the_trigger_check_is_not_inside_the_collect_guard() -> None:
    """★ 采集面与触发面必须是**两段**（TS 两个 ``for`` 循环，``:592-604`` / ``:607-616``）。

    ``should_trigger_mine`` 挪进 ``if msg.msg_type != MT_SYSTEM and content.strip():``
    里面就合成了一处：系统通知与空正文从此**完全**不参与节流判定（TS 是参与——
    它第二个循环只排除 blocked 与非群）。后果极轻（晚一条消息才挖），
    但那是**替 TS 改了行为**，而且这种形状在测试里几乎看不出来：两边都不报错。
    """
    fn = _methods()["_jargon_note_group"]
    guard = _if(fn, "msg.msg_type != MT_SYSTEM and content.strip()")
    trigger = _calls(fn, "should_trigger_mine")
    assert trigger, "找不到 should_trigger_mine 调用"
    assert not _is_inside(trigger[0], guard), \
        "should_trigger_mine 必须在采集面的 if **外面**（TS 是两个循环）"
    assert trigger[0].lineno > guard.lineno


def test_the_collect_guard_excludes_system_notes_and_blank_content() -> None:
    """采集面的闸逐字对齐 TS 第一个循环的两个 ``continue``：
    ``msgType === SYSTEM`` 与 ``!content``（py2 写成 ``content.strip()``，
    比 TS 的 ``!content`` 略严 —— 全空白正文在 TS 会进统计表，这里不进；
    如实记下：这一处**有意略严**，因为 py2 的 ``WppMessage.content`` 可能带换行）。"""
    fn = _methods()["_jargon_note_group"]
    guard = _if(fn, "msg.msg_type != MT_SYSTEM and content.strip()")
    # ⚠️ 只看 ``guard.test`` —— ``ast.walk(guard)`` 会把 ``if`` 的**整段 body**
    #    也走一遍（里面有一堆 ``jargon_mod.update_from_message`` 之类的属性调用）
    calls = {c.func.attr for c in ast.walk(guard.test) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Attribute)}
    assert calls == {"strip"}, ast.unparse(guard)


# ============================================================ 判据 3：fire-and-forget


def test_the_bypass_itself_is_synchronous() -> None:
    """``_jargon_note_group`` 是**同步** ``def``（只做内存采集 + 抛任务），
    ``_jargon_mine_run`` 才是 ``async def``。

    写成 ``async def`` 而调用方不 ``await`` ⇒ 整个旁路**一次都不执行**、只留一条
    "coroutine was never awaited" 的 RuntimeWarning（被日志淹掉），
    现场表现就是"开关开着、词库永远空"。
    """
    assert isinstance(_methods()["_jargon_note_group"], ast.FunctionDef)
    assert not any(isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith))
                   for n in ast.walk(_methods()["_jargon_note_group"])), \
        "同步函数里不该有 await"
    assert isinstance(_methods()["_jargon_mine_run"], ast.AsyncFunctionDef)


def test_the_mine_run_is_never_awaited_from_the_inbound_path() -> None:
    """★ 收信链路上**绝不**出现 ``await ...mine_jargon_for_group`` 或
    ``await self._jargon_mine_run`` —— 那会把最多 16 次 LLM 往返（每次 5s 超时）
    直接塞进 ``_handle_one``：数据库/模型一慢，整个群看着机器人卡住。"""
    one = _methods()["_handle_one"]
    for node in ast.walk(one):
        if isinstance(node, ast.Await) and any(
                name in ast.unparse(node) for name in ("mine_jargon_for_group", "_jargon_mine_run")):
            raise AssertionError(f"收信链路上 await 了挖掘: {ast.unparse(node)}")
    # 旁路内部也只许 create_task，不许直接 await
    fn = _methods()["_jargon_note_group"]
    assert not any(isinstance(n, ast.Await) for n in ast.walk(fn)), \
        "_jargon_note_group 里不许 await（挖掘必须 fire-and-forget）"


def test_the_task_is_created_by_name_and_held_by_a_strong_reference() -> None:
    """★ ``asyncio.create_task(..., name=...)`` + ``self._jargon_tasks.add(task)``
    + ``task.add_done_callback(self._jargon_done)``，三件缺一不可是各有一种**静默**后果：

    * 不存句柄 ⇒ 事件循环只持弱引用，任务可能在跑完前被 GC（"黑话有时挖有时不挖"）；
    * 不挂回调 ⇒ fire-and-forget 的异常只在 GC 时留一句
      "Task exception was never retrieved"，**没有群、没有原因**；
    * 不给 ``name`` ⇒ 上面那行日志认不出是哪个账号（多账号时只剩一个随机 task id）。
    """
    fn = _methods()["_jargon_note_group"]
    created = _calls(fn, "create_task")
    assert len(created) == 1, f"create_task 出现 {len(created)} 次"
    assert any(k.arg == "name" for k in created[0].keywords), "create_task 没给 name="

    adds = _calls(fn, "add")
    assert adds and ast.unparse(adds[0].func) == "self._jargon_tasks.add", ast.unparse(fn)
    assert any(ast.unparse(c.func) == "task.add_done_callback" for c in _calls(fn, "add_done_callback"))

    # 句柄表必须真的在 __init__ 里建出来（否则第一句 add 就 AttributeError，
    # 而它被同一段 try 吞成一行 warning —— 表现为"任务永远不挖"）
    init_src = ast.unparse(_methods()["__init__"])
    assert "_jargon_tasks" in init_src


def test_the_done_callback_discards_first_then_logs_any_exception() -> None:
    """收尾回调：先 ``discard`` 再判异常，且**任何**异常都记一行 warn。

    ⚠️ 这里不许按异常类型分流：``_jargon_done`` 是这条路上**唯一**能出声的地方，
    挑类型就等于把"没预料到的失败"重新变成静默 —— 与本项"绝不造说谎的开关"同一条。
    """
    fn = _methods()["_jargon_done"]
    src = ast.unparse(fn)
    assert "self._jargon_tasks.discard(task)" in src
    assert "task.exception()" in src
    assert src.index("discard(task)") < src.index("task.exception()"), "先丢句柄再取异常"
    warns = [c for c in _calls(fn, "warning")]
    assert warns, "_jargon_done 里必须有一行 log.warning（否则失败无声）"
    assert "task.cancelled()" in src, "被取消的任务不许再取 exception()（会抛 CancelledError）"


def test_the_mine_run_logs_only_real_saves() -> None:
    """记账只报**真的进了库**的条数（``result.confirmed``，见 ``MineResult`` 的 docstring）。

    写成 ``extracted``（候选数）会让日志把"送 LLM 验证了几个词"报成"入库几条"，
    与 ``wpp_jargon_terms`` 的行数对不上 —— 而这两处是运维唯一的观测点。
    """
    fn = _methods()["_jargon_mine_run"]
    src = ast.unparse(fn)
    assert "result.confirmed" in src
    assert "result.extracted" not in src


# ============================================================ 判据 4：配置只有一个家


def test_the_cfg_is_resolved_from_the_account_file() -> None:
    """``resolve_jargon_cfg(acct.jargon)`` —— 账号文件那份裸 dict，**直接**喂进去。

    ⚠️ 不许在这里就地取键（``acct.jargon.get("model")``）：那样配置就有了第二个家，
    而 ``resolve_jargon_cfg`` 的默认值/类型强制（fail-closed 的 ``enabled``、
    ``bool`` 不是 ``int``）会**整份绕过去** —— 开关的语义在两条路上分叉。
    """
    fn = _methods()["_jargon_note_group"]
    calls = [c for c in _calls(fn, "resolve_jargon_cfg")
             if ast.unparse(c.func) == "jargon_mine.resolve_jargon_cfg"]
    assert len(calls) == 1, f"resolve_jargon_cfg 出现 {len(calls)} 次"
    assert ast.unparse(calls[0].args[0]) == "acct.jargon", ast.unparse(calls[0])

    src = ast.unparse(fn)
    for key in JARGON_ONLY_KEYS:
        assert key not in src, f"adapter 里不该出现配置键 {key!r}（第二处解析）"


def test_the_shared_adapter_source_has_no_jargon_config_key_literals() -> None:
    """整个 ``adapter.py`` 里都不许出现 jargon 独有的三个键名 —— 与 W3 的
    ``intent-adapter`` 同款判据：键名一旦在这里出现，就是又开了一处解析。"""
    src = _src(ADAPTER_REL)
    present = [k for k in JARGON_ONLY_KEYS if k in src]
    assert not present, f"adapter.py 里出现了 jargon 配置键 {present}"


def test_the_jargon_keys_are_read_only_in_the_mine_module() -> None:
    """三个键在本仓只有**一个**读取点：``jargon_mine.resolve_jargon_cfg``。

    （``accounts.py`` 只是把 ``jargon`` 整份透传，不读里面的键；落到那儿的
    ``.get("model")`` 就是第二条解析路。）

    ⚠️ 扫描面**排除测试目录**：这条判据的期望值里就写着这三个键名，
    而 ``plugin/tests/**`` 里必然出现它们（否则这条判据自己先红）。
    """
    for key in JARGON_ONLY_KEYS:
        hits = [p for p in (ROOT / "plugin").rglob("*.py")
                if "tests" not in p.parts and key in p.read_text(encoding="utf-8")]
        rels = sorted(str(p.relative_to(ROOT)) for p in hits)
        assert rels == ["plugin/inbound/jargon_mine.py"], f"{key} 出现在 {rels}"


# ============================================================ 判据 5：store 与 creds


def test_the_store_is_scoped_to_the_account_not_a_fixed_namespace() -> None:
    """★ ``RepoJargonStore(account_id)``，而 ``account_id`` 来自 ``acct.id``。

    写成 ``"default"`` 的后果是**跨账号串味**：表里有 ``account_id`` 列，
    A 号挖到的词会让 B 号的 ``exclude`` 命中 ⇒ B 号永远挖不到那几个词，
    而两边都不报错（W7-1 的 ``media-enrich`` 变异体就是同一个坑，那次是夹具的 id 正好叫
    ``default`` 才躲过判据 —— 这里直接把实参名钉死）。
    """
    fn = _methods()["_jargon_mine_run"]
    created = _calls(fn, "RepoJargonStore")
    assert len(created) == 1, f"RepoJargonStore 出现 {len(created)} 次"
    assert ast.unparse(created[0].args[0]) == "account_id", ast.unparse(created[0])

    call = _calls(_methods()["_jargon_note_group"], "_jargon_mine_run")[0]
    assert ast.unparse(call.args[0]) == "acct.id", ast.unparse(call)
    assert ast.unparse(call.args[1]) == "group_id", ast.unparse(call)


def test_the_creds_are_left_to_the_module_to_resolve() -> None:
    """``mine_jargon_for_group(group_id, cfg, None, store)`` —— 第三个实参是 ``None``。

    写死一份 creds 就等于在适配器里造了第二条凭据解析路（house style 是模块内部
    ``resolve_judge_creds()``，见 ``intent.py:388``）。写成 ``None`` 还有个好处：
    没配 key 时 ``jargon_llm`` 第一句就返 ``None`` ⇒ 一轮**零外呼**地空转结束。
    """
    fn = _methods()["_jargon_mine_run"]
    calls = _calls(fn, "mine_jargon_for_group")
    assert len(calls) == 1, f"mine_jargon_for_group 出现 {len(calls)} 次"
    args = calls[0].args
    assert len(args) == 4, ast.unparse(calls[0])
    assert ast.unparse(args[2]) == "None", ast.unparse(calls[0])
    assert ast.unparse(args[3]) == "store", ast.unparse(calls[0])


def test_the_group_id_is_the_chatroom_with_a_peer_fallback() -> None:
    """``group_id = msg.chatroom_id or msg.peer_id`` —— 且这一个值喂给**四处**
    （``update_from_message`` / ``record_message`` / ``should_trigger_mine`` / 挖掘任务）。

    四处各算一遍（比如挖的时候用 ``msg.peer_id``）会让统计与挖掘落在两个桶里：
    词库永远差那一群，而日志上一切正常。
    """
    fn = _methods()["_jargon_note_group"]
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "group_id" for t in n.targets)]
    assert len(assigns) == 1, f"group_id 被赋值 {len(assigns)} 次"
    assert ast.unparse(assigns[0].value) == "msg.chatroom_id or msg.peer_id"
    assert "msg.peer_id" not in ast.unparse(fn).replace("msg.chatroom_id or msg.peer_id", ""), \
        "别处不该再自己拼一次群 id"

    for name in ("update_from_message", "record_message", "should_trigger_mine"):
        call = _calls(fn, name)[0]
        assert any(ast.unparse(a) == "group_id" for a in call.args), ast.unparse(call)
    assert any(ast.unparse(a) == "group_id"
               for a in _calls(fn, "_jargon_mine_run")[0].args)


def test_the_bypass_imports_its_two_modules_from_inbound() -> None:
    """装配点必须是 ``from ..inbound import jargon as jargon_mod`` /
    ``from ..inbound import jargon_mine`` 这两条 —— 断掉 import 会 NameError（好在会响）；
    **更坏的是**从别处 import 同名对象 ⇒ 看起来干净、行为是另一套。

    ⚠️ 写法是 ``ImportFrom(module="..inbound", names=[jargon, jargon_mine])``，
    不是 ``module="..inbound.jargon_mine"`` ⇒ 判据要按 ``module + alias`` 一起看。
    """
    tree = ast.parse(_src(ADAPTER_REL))
    bound = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("inbound"):
            bound |= {(a.name, a.asname) for a in n.names}
    assert ("jargon_mine", None) in bound, bound
    assert ("jargon", "jargon_mod") in bound, bound


def test_the_mine_module_keeps_the_config_error_exported() -> None:
    """``JargonConfigError`` 必须能被适配器/测试 import 到（真机"没配 model"
    那条路的观测点）；同时 ``resolve_jargon_cfg``/``mine_jargon_for_group`` 都在。"""
    tree = ast.parse(_src(MINE_REL))
    names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    assert "JargonConfigError" in names
    funcs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"resolve_jargon_cfg", "mine_jargon_for_group"} <= funcs
    exported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets):
            exported = {e.value for e in n.value.elts if isinstance(e, ast.Constant)}
    assert {"JargonConfigError", "RepoJargonStore", "mine_jargon_for_group"} <= exported, exported


# ============================================================ 判据 6：回复侧注入（2026-10-19）


def test_jargon_meaning_is_injected_into_the_reply_side_channel_prompt() -> None:
    """★ 学到的含义必须进**回复侧**，而不只是 ``wpp_jargon_query`` 工具。

    注入点必须是 ``_channel_prompt`` 的组合列表里（``channel_prompt`` 只收一个字符串）；
    直接覆盖会让 W2 情绪 / 群画像静默消失。
    """
    combined = ast.unparse(_methods()["_channel_prompt"])
    assert "_jargon_channel_prompt" in combined
    assert "_mood_channel_prompt" in combined and "_group_profile_channel_prompt" in combined
    assert "if parts else None" in combined

    fn = _methods()["_jargon_channel_prompt"]
    src = ast.unparse(fn)
    assert "jargon_mod.build_jargon_channel_prompt" in src
    assert "resolve_jargon_cfg(acct.jargon)" in src


def test_the_reply_side_jargon_lookup_never_touches_the_db() -> None:
    """热路径只读进程内缓存 —— 本方法里出现 ``repo_`` / ``list_jargon`` 就是每条消息一次库读。"""
    src = ast.unparse(_methods()["_jargon_channel_prompt"])
    assert "repo_" not in src and "list_jargon" not in src

    # 三道闸都在缓存读之前：非群 / 开关关 / 缓存无命中
    fn = _methods()["_jargon_channel_prompt"]
    gate_group = _if(fn, "not getattr(msg, 'is_group', False)")
    call = _calls(fn, "build_jargon_channel_prompt")[0]
    assert not _is_inside(call, gate_group), "群消息闸必须在缓存读之前返回"
    assert "enabled" in src and src.find("enabled") < src.find("build_jargon_channel_prompt")


def test_the_db_meanings_are_preheated_once_per_group_after_restart() -> None:
    """重启后 ``_term_meanings`` 是空的 ⇒ 必须有一次性预热，否则注入要等下一轮挖掘。"""
    fn = _methods()["_jargon_ensure_meanings"]
    src = ast.unparse(fn)
    assert "self._jargon_loaded" in src
    assert "create_task" in src and "to_thread" in src, "读库必须进线程池，不能卡收信"
    assert any(k.arg == "name" for c in _calls(fn, "create_task") for k in c.keywords)

    loader = _methods()["_jargon_load_meanings"]
    lsrc = ast.unparse(loader)
    assert "list_jargon" in lsrc
    assert "remember_meaning" in lsrc
    init_src = ast.unparse(_methods()["__init__"])
    assert "_jargon_loaded" in init_src
