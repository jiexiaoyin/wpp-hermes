"""留痕 / 可观测回归 —— TS ``tests/unit/heartflow-trace.test.mjs`` (9 例) 的 pytest 译本。

这一套问的不是"判定对不对"，而是 **"判了之后，运维面上看不看得见"**。
09-11 judge 静默瘫 3 天之所以能瘫满 3 天，根因不是代码写错，是**留痕缺失**：
旧码只在 ``shouldReply=True`` 时落台账 ⇒「judge 跑了但没过」在日志与 DB 里零留痕，
与「群里压根没消息」外观**完全一致**；台账最后一行停在 09-10，人却看不出异常。

## 与 TS 的对应

================  ==========================================================
TS 1              已由 ``test_learn.py::test_persist_below_threshold_inserts_then_suppresses``
                  覆盖（INSERT → 立即 suppress，顺序固定）—— 本文件不重复。
TS 2              本文件 §1：三个结论的词必须互不相同，且各只有一个出处。
TS 3              本文件 §2：INSERT 抛错 ⇒ 不 reject，**且不去 suppress 一个不存在的行**。
TS 4              本文件 §3：UPDATE 抛错 ⇒ 不 reject，行留在 ``judged`` 交给 sweep 呆账。
TS 5              本文件 §4：两条学习样本查询都要求 ``status='closed' AND engaged IS NOT NULL``。
TS 6              本文件 §5：``count_by_status`` 的聚合形状（含空集与 NULL 理由）。
TS 7              ``/heartflow status`` 展示近 24h 台账 —— 见 §6 的说明。
TS 8/9            dev 侧调用点与 deploy 产物形状 —— N/A（py2 无 dist 产物；调用点由
                  ``test_learn.py`` 的 sweep 用例覆盖）。
TS B9（v1.10.0）  本文件 §6：指标**预声明** —— 零值序列从进程启动就存在。
================  ==========================================================

## 本文件最怕的"假绿"

1. **只测了失败路径**：把 ``insert_judged`` 换成"啥也不干"的空壳，失败用例照样全绿。
   所以 §2/§3 每条都**先跑一遍成功对照组**（证明桩真的会被调用），再注入异常。
2. **SQL 用源码 grep 断言**：字符串在文件里、但函数走的是另一条分支，一样绿。
   §4/§5 一律**通过真函数 + 记账式 conn** 拿到"实际执行的那条 SQL"再断言。
3. **预声明写成"常量表里有个名字"**：名字在表里而埋点指到别的名字，等于没接。
   §6 走 AST：把每个 ``inc_*`` / ``set_*`` 出口真正传的常量名挖出来，逐一核对。

真实日期 / 生产数字一律不进本文件（公开仓红线）；样本全是构造的。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from plugin.core import metrics
from plugin.data import repo_hf
from plugin.inbound.heartflow import learn as L

ROOT = Path(__file__).resolve().parents[3]
NOW = 1_700_000_000


# ============================================================ 假数据层（本文件自用）

class _LedgerStub:
    """台账写侧的最小桩：记调用 + 可注入异常。签名与真 ``repo_hf`` **按位置固定**。"""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on or set()

    def _hit(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail_on:
            raise RuntimeError(f"boom-{name}")

    def insert_judged(self, account_id, inbound_msg_id, group_id, judged_at, **kw) -> int:
        self._hit("insert_judged")
        self.last_insert = dict(account_id=account_id, inbound_msg_id=inbound_msg_id,
                                group_id=group_id, judged_at=judged_at, **kw)
        return 1

    def set_suppressed(self, account_id, inbound_msg_id, reason, at_sec) -> int:
        self._hit("set_suppressed")
        self.suppressed = (account_id, inbound_msg_id, reason, at_sec)
        return 1


class _SqlRecorder:
    """只读记账 conn：把 ``query`` 真正收到的 SQL 与参数记下来，返回预设行。"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.seen: list[tuple[str, tuple]] = []

    def query(self, sql: str, params=()) -> list[dict]:
        self.seen.append((sql, params))
        return [dict(r) for r in self.rows]

    def execute(self, sql: str, params=()):        # pragma: no cover —— 只读查询不许走这条
        self.seen.append((sql, params))
        raise AssertionError(f"只读查询里出现了写操作: {sql[:60]}")


class _ExecRecorder(_SqlRecorder):
    """写侧记账（``execute`` 记下 SQL 并返回 0 行）—— 用来验证**状态机收敛语句**的形状。"""

    def execute(self, sql: str, params=()) -> int:
        self.seen.append((sql, params))
        return 0

@pytest.fixture
def stub(monkeypatch):
    s = _LedgerStub()
    monkeypatch.setattr(repo_hf, "insert_judged", s.insert_judged)
    monkeypatch.setattr(repo_hf, "set_suppressed", s.set_suppressed)
    return s


def _rec() -> L.HfLedgerRecord:
    return L.HfLedgerRecord(account_id="acct", inbound_msg_id="m1", group_id="gA",
                            judged_at=NOW, content_head="问价", judge_overall=0.41,
                            effective_threshold=0.6)


# ============================================================ §1 三个结论三个词

def test_the_three_ledger_verdicts_use_three_distinct_reason_words(stub):
    """**TS 2**：``below-threshold`` / ``no-deliver-outcome`` / 发送期抑制 —— 三个词必须互不相同。

    为什么不是"随便记个原因就行"：这三个词在 ``/heartflow status`` 里是**三件不同的事** ——
    「judge 判了没过」（调阈要看它）/「判了但投递结果永远没回来」（vendor 或进程的问题）/
    「发送期被闸拦下」（预算/重复/占位符）。混成一个词，运维面对着的就是一锅汤：
    v1.6.1 之前正是这样，所以"judge 静默瘫"与"群里没人说话"分不开。

    词本身还各自只许有一个出处 —— 到处手写字符串迟早漂移成三个词变五个。
    """
    # ① 判了没过：真跑一次，拿**实际写下去**的那个词
    L.persist_hf_judged_below_threshold(_rec(), NOW)
    below = stub.suppressed[2]
    assert below == "below-threshold"

    # ② 发送期抑制：占位符 → 各自的词（与上面那类必须不相交）
    send_words = set()
    for placeholder, expected in (("ack-template-dropped", "ack-template"),
                                  ("dedup-suppressed", "dedup"),
                                  ("repeat-suppressed", "repeat"),
                                  ("", "empty-text")):
        s = _LedgerStub()
        import plugin.data.repo_hf as rh
        orig = rh.set_suppressed
        rh.set_suppressed = s.set_suppressed
        try:
            L.persist_hf_send_outcome("acct", "m1", {"ok": True, "msg_id": placeholder},
                                      NOW, group_id="gA", observe_sec=600, label_window_sec=60)
        finally:
            rh.set_suppressed = orig
        send_words.add(s.suppressed[2])
        assert s.suppressed[2] == expected, f"{placeholder!r} 的抑制理由应是 {expected!r}"

    assert "below-threshold" not in send_words, "judge 判了没过 ≠ 发送期被拦（两件事）"

    # ③ 呆账收敛的词（sweep 侧，见 §3 的 SQL）；只要求与上面两类都不同
    assert "no-deliver-outcome" not in send_words | {"below-threshold"}

    # ④ 单出处：每个词的**运行期字符串**只出现在一个源码文件里（文档串里的交叉引用不算 ——
    #    ``heartflow.py`` 的模块 docstring 就要点出 ``suppressed/below-threshold`` 这个名字，
    #    那不是"第二处定义"）。
    for word, home in (("below-threshold", "plugin/inbound/heartflow/learn.py"),
                       ("no-deliver-outcome", "plugin/data/repo_hf.py")):
        homes = _runtime_string_homes(word)
        assert homes == {home}, f"{word!r} 应只在 {home} 里定义，实际出现在 {sorted(homes)}"


def _runtime_string_homes(needle: str) -> set[str]:
    """含该词的**运行期**字符串常量分布在哪些文件（docstring / 注释一律不算）。"""
    out: set[str] = set()
    for p in (ROOT / "plugin").rglob("*.py"):
        if "/tests/" in str(p):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        doc_ids = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    doc_ids.add(id(body[0].value))
        if any(isinstance(n, ast.Constant) and isinstance(n.value, str)
               and id(n) not in doc_ids and needle in n.value
               for n in ast.walk(tree)):
            out.add(str(p.relative_to(ROOT)))
    return out



# ============================================================ §2 INSERT 抛错

def test_below_threshold_insert_failure_leaves_no_suppressed_ghost(stub, caplog):
    """**TS 3**：INSERT 抛错 ⇒ **不 reject**，且**不再执行 suppress**（半成品不留）。

    "不留半成品"在这里是硬要求：``set_suppressed`` 的 guard 是 ``status='judged'``，
    行压根没插进去时它 UPDATE 0 行 —— 看起来无害，但它会**吃掉一次 warn**
    （出错的那条日志会被"抑制成功"的下一条盖过去），而这条 warn 正是 09-11 唯一
    能救命的线索。

    先跑成功对照组：不然把 ``insert_judged`` 换成空壳，这条用例照样绿。
    """
    # 对照组：正常路径两个调用都在
    L.persist_hf_judged_below_threshold(_rec(), NOW)
    assert stub.calls == ["insert_judged", "set_suppressed"], "对照组：两步都要跑"

    # 失败路径
    stub.calls.clear()
    stub.fail_on = {"insert_judged"}
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        L.persist_hf_judged_below_threshold(_rec(), NOW)      # 不许抛
    assert stub.calls == ["insert_judged"], "INSERT 失败后不许再去 suppress"
    assert any("non-fatal" in r.message for r in caplog.records), "失败必须留一行 warn"


# ============================================================ §3 UPDATE 抛错

def test_below_threshold_suppress_failure_leaves_the_row_to_the_sweep(stub, caplog):
    """**TS 4**：suppress 抛错 ⇒ **不 reject**；行留在 ``judged``，由 sweep 呆账收敛。

    这条不是"顺手容错"，是**状态机的一环**：``judged`` 挂太久本就有一条收敛路径
    （``expire_stale_judged`` → ``suppressed/no-deliver-outcome``）。所以这里只要
    保证"不抛 + 行还是 judged"，两者就能接上 —— 下面把接上的那一端也断言掉，
    否则"交给 sweep"只是注释里的一句话。
    """
    stub.fail_on = {"set_suppressed"}
    with caplog.at_level(logging.WARNING, logger=L.__name__):
        L.persist_hf_judged_below_threshold(_rec(), NOW)      # 不许抛
    assert stub.calls == ["insert_judged", "set_suppressed"]
    assert any("non-fatal" in r.message for r in caplog.records)

    # 接得上的那一端：真的有一条只吃 ``judged`` 的收敛语句
    with _conn_capture() as seen:
        repo_hf.expire_stale_judged("acct", NOW, NOW - 86_400)
    sql, _params = seen[0]
    assert "status = 'judged'" in sql, "呆账收敛只该吃 judged 行（已发/已收敛的不许被动）"
    assert "'no-deliver-outcome'" in sql


class _conn_capture:
    """把真函数执行的 SQL 截下来（用完还原 ``db.get_conn``）。"""

    def __init__(self) -> None:
        self.rec = _ExecRecorder()
    def __enter__(self):
        from plugin.data import db
        self.db = db
        self.orig = db.get_conn
        db.get_conn = lambda *a, **kw: self.rec
        return self.rec.seen

    def __exit__(self, *exc):
        self.db.get_conn = self.orig
        return False


# ============================================================ §4 学习样本的筛选

def test_learning_samples_can_never_include_a_suppressed_row():
    """**TS 5**：两条学习样本查询都要求 ``status='closed' AND engaged IS NOT NULL``。

    为什么这条必须焊死：``persist_hf_judged_below_threshold`` 每群每个 judge 冷却周期
    就会写一条 ``suppressed`` 行。它一旦进了学习样本，"判了没过"的样本会**指数增长**
    （判得越多、样本越多），接话率被稀释到 0 ⇒ 闭环必然把阈值推上地板 ⇒ 再也发不出话。
    换句话说：留痕（§1）与学习（这条）是**一对**，只做留痕不做隔离就是把 09-11 换成 09-26。
    """
    for fn, args in ((repo_hf.get_closed_recent, ("acct", "gA")),
                     (repo_hf.list_distinct_closed_groups, ("acct", NOW - 86_400))):
        rec = _SqlRecorder()
        fn(*args, conn=rec)                      # 真函数，不是 grep
        assert len(rec.seen) == 1, f"{fn.__name__} 应只查一次"
        sql, _params = rec.seen[0]
        assert sql.lstrip().upper().startswith("SELECT"), f"{fn.__name__} 必须只读"
        assert "status = 'closed'" in sql, f"{fn.__name__} 漏了 status='closed'"
        assert "engaged IS NOT NULL" in sql, f"{fn.__name__} 漏了 engaged IS NOT NULL"
        for other in ("'suppressed'", "'sent'", "'judged'", "'pending'"):
            assert other not in sql, f"{fn.__name__} 数进了非收敛行（{other}）"

    # 另一半：写 suppressed 时**不许碰 engaged**（保持 NULL ⇒ 上面两条过滤天然排除它）。
    # 碰了就前功尽弃 —— 过滤条件还在，但行已经能穿过它了。
    rec = _ExecRecorder()
    repo_hf.set_suppressed("acct", "m1", "below-threshold", NOW, conn=rec)
    sup_sql, _ = rec.seen[0]
    assert "SET status = 'suppressed'" in sup_sql
    assert "engaged" not in sup_sql, "suppressed 写入不得碰 engaged（保持 NULL ⇒ 被学习样本排除）"
    assert "status = 'judged'" in sup_sql, "guard：只许从 judged 推进"

    # ``count_engage_signals`` **故意不**带 engaged IS NOT NULL —— 它是"标签分布"而不是学习样本，
    # 要对**全部** closed 行计数（``engage_signal IS NULL`` 单列成 legacy 桶，
    # 那正是"老标签还剩多少"这个可观测信号本身）。带上过滤反而会把 legacy 桶清空。
    rec = _SqlRecorder([{"engage_signal": None, "n": 2}, {"engage_signal": "quote", "n": 3}])
    out = repo_hf.count_engage_signals("acct", NOW - 86_400, conn=rec)
    assert out == {"legacy": 2, "quote": 3}
    assert "engaged IS NOT NULL" not in rec.seen[0][0]


def test_get_closed_recent_returns_an_int_sent_at():
    """★ B9（2026-10-02）：调阈侧按**每条样本自己那一小时**查本底 ⇒ ``sent_at`` 必需且是整型秒。

    两个方向都要钉：查询里真取这一列（缺了整批判成"不可采信"，阈值静默僵住），
    以及回来的是 ``int`` —— ``UNIX_TIMESTAMP`` 那边可能是 ``Decimal``，
    不归一的话本底查表的键会变成 ``gA|Decimal('1789070300')`` 而表里只有 ``gA|8``，
    查不到就按 ``ambientP=0`` 采信 ⇒ **夜里那批弱信号全被当成有效信息**（B9 要治的病照旧犯）。
    """
    from decimal import Decimal

    rows = [{"engaged": 1, "closed_at": Decimal("1789071000"),
             "engage_signal": "quote", "sent_at": Decimal("1789070300")}]
    rec = _SqlRecorder(rows)
    out = repo_hf.get_closed_recent("acct", "gA", conn=rec)

    assert "sent_at" in rec.seen[0][0], "查询里必须真取这一列"
    assert out[0]["sent_at"] == 1789070300 and isinstance(out[0]["sent_at"], int)
    assert isinstance(out[0]["closed_at"], int), "顺手确认归一没漏（同一条路径）"


# ============================================================ §5 台账计数

def test_count_by_status_aggregates_status_and_reason():
    """**TS 6**：``count_by_status`` 按 ``status`` + ``suppressed_reason`` 聚合，带时间窗、只读。

    这是 ``/heartflow status`` 里"近 24h 台账"那一行的唯一数据源 ——
    没有它，"judge 跑了但没回"就只能靠翻库。
    """
    rows = [
        {"status": "suppressed", "suppressed_reason": "below-threshold", "n": 3},
        {"status": "suppressed", "suppressed_reason": "no-deliver-outcome", "n": 1},
        {"status": "closed", "suppressed_reason": None, "n": 5},
    ]
    rec = _SqlRecorder(rows)
    out = repo_hf.count_by_status("acct", NOW - 86_400, conn=rec)

    assert out["total"] == 9
    assert out["by_status"] == {"suppressed": 4, "closed": 5}
    assert out["by_suppressed_reason"] == {"below-threshold": 3, "no-deliver-outcome": 1}, \
        "没有理由的行（closed/sent）不许落进 by_suppressed_reason"

    sql, params = rec.seen[0]
    assert "COUNT(*)" in sql and "GROUP BY status, suppressed_reason" in sql
    assert "judged_at >= %s" in sql, "必须带时间窗（不带上限就是全表扫）"
    assert params == ("acct", NOW - 86_400)
    for kw in ("UPDATE", "DELETE", "INSERT"):
        assert kw not in sql.upper()

    # 空集：形状要稳定（前端拿它直接渲染）
    assert repo_hf.count_by_status("acct", 0, conn=_SqlRecorder([])) == \
        {"total": 0, "by_status": {}, "by_suppressed_reason": {}}


# ============================================================ §6 指标预声明

def test_every_emitter_targets_a_predeclared_metric():
    """**TS B9**：零值序列从进程启动就存在 —— 且每个出口指向的**就是**被声明的那个名字。

    "值为 0"必须能与"埋点根本没接上"区分开，否则停摆看起来一切正常（v1.9.2 的教训，
    也是 09-26 停摆 3 天无人发现的原因之一）。

    光断言"常量表里有这几个名字"不够 —— 出口传的字符串写错了，表里照样齐全。
    所以这里从 ``metrics.py`` 的 AST 里把每个 ``inc_*`` / ``set_*`` 出口真正传的常量
    挖出来，与预声明表逐一核对。
    """
    # ⚠️ 断言的是"**存在**（=被预声明），而不是"值为 0" —— 同进程里别的用例可能已经自增过，
    #    而这条要守的正是"没自增过也存在"（0 与"埋点没接上"的分界线）。
    #    ``metrics._key`` 会补 ``wpp_`` 前缀，快照里存的是补过前缀的名字。
    snap = metrics.snapshot()
    for name in metrics.PRE_DECLARED_COUNTERS:
        assert metrics._key(name) in snap["counters"], \
            f"{name} 必须从进程启动起就存在（否则「值为 0」与「埋点没接上」分不开）"
    for name in metrics.PRE_DECLARED_GAUGES:
        assert metrics._key(name) in snap["gauges"], f"{name} 必须从进程启动起就存在"

    # 心流四条金丝雀必须都在（少一条 = 某一类停摆不可见）
    required = {
        metrics.COUNTER_HF_SENDS,
        metrics.COUNTER_HF_REACH_CAP_APPLIED,
        metrics.COUNTER_HF_JUDGE_PASSED,
        metrics.COUNTER_HF_JUDGE_BELOW,
        metrics.GAUGE_HF_LAST_SEND_AGE_SEC,
        metrics.GAUGE_HF_EFFECTIVE_THRESHOLD_MAX,
        metrics.GAUGE_HF_THRESHOLD_CEILING_GROUPS,
        metrics.GAUGE_HF_REACHABLE_GROUPS,
    }
    declared = set(metrics.PRE_DECLARED_COUNTERS) | set(metrics.PRE_DECLARED_GAUGES)
    assert required <= declared, f"未预声明的心流指标: {sorted(required - declared)}"

    # 出口 → 常量名：逐一核对
    src = (ROOT / "plugin/core/metrics.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = {t.id: n.value.value
              for n in tree.body
              if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
              for t in n.targets if isinstance(t, ast.Name)}
    consts = {k: v for k, v in consts.items() if isinstance(v, str)}

    emitted: dict[str, str] = {}
    for n in tree.body:
        if not isinstance(n, ast.FunctionDef) or not (
                n.name.startswith("inc_") or n.name.startswith("set_hf_")):
            continue
        for call in ast.walk(n):
            if isinstance(call, ast.Call) and call.args:
                arg = call.args[0]
                if isinstance(arg, ast.Name) and arg.id in consts:
                    emitted[n.name] = consts[arg.id]
    assert emitted, "没扫到任何出口 —— AST 口径写错了，别让这条用例变成空转"
    for fn_name, metric in emitted.items():
        assert metric in declared, f"{fn_name}() 埋的 {metric!r} 没预声明 ⇒ 静默 0 与没接上分不开"
