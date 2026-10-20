"""DB 体检的**接线**回归（D7，2026-10-02）—— ``adapter._run_migrations`` 那一段。

## 为什么单列一套

``migrate()`` 自己把结局记进 ``db.health()`` 与日志（见 ``test_db.py`` §9），但**接没接上**
是另一件事：接线断了的话，体检逻辑再对也白搭 —— 而它的失效形态恰恰是"**看起来一切正常**"：

1. 失败只写 ``warning``、且只有启动那一次 ⇒ 之后进程照跑、``SELECT 1`` 照通，
   "schema 只建了一半"与"一切正常"在运维面上完全一样（心流每条都写不进台账，
   0 条发言看起来就像"群里没人说话"）。
2. ``wpp_db_ready`` gauge 忘了置 0 ⇒ 金丝雀永远报"就绪"。
3. ``db_migration_failures_total`` 不增 ⇒ "跑过但没成"与"根本没跑"无法区分。
4. 异常被吞成 ``pass`` ⇒ 连日志都没有。

## 怎么测（adapter 本机 import 不了）

``adapter.py`` 头部 ``from gateway.config import Platform`` —— gateway 是 Hermes 运行时，
本地没有（与 ``test_hf_commands`` 同一处境）。所以这里把 ``_run_migrations`` 的**函数体**
从 AST 里掏出来、在一个假命名空间里**真跑**：``db`` 走真的 ``plugin.data.db``（monkeypatch
它的三个口子），``metrics`` 走真计数器，``log`` 换成记账的假 logger。

比"对源码做字符串断言"强的地方：注释里写着、代码里删掉，字符串断言照样绿；这里会红。
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from plugin.core import metrics
from plugin.data import db

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"


def _method(tree: ast.Module, cls: str, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == name:
                    return sub
    raise AssertionError(f"{ADAPTER_REL} 里找不到 {cls}.{name} —— 改名/搬家了？")


class _Log:
    """假 logger：把**级别**也记下来（D7 的要点之一就是 warn → error）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _rec(self, level):
        def _f(msg, *args, **_kw):
            self.calls.append((level, (msg % args) if args else str(msg)))
        return _f

    def __getattr__(self, level: str):
        return self._rec(level)

    def text(self) -> str:
        return "\n".join(f"{lv}: {m}" for lv, m in self.calls)

    def levels(self) -> list[str]:
        return [lv for lv, _ in self.calls]


def run_migrations(log: _Log) -> None:
    """真跑 ``_run_migrations`` 的函数体（``db`` 用真模块，便于 monkeypatch）。"""
    tree = ast.parse((ROOT / ADAPTER_REL).read_text(encoding="utf-8"))
    fn = _method(tree, "WppAdapter", "_run_migrations")
    body = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))
    ns: dict = {
        "__name__": "plugin.platform.adapter",
        "__package__": "plugin.platform",      # ``from ..data import db`` 靠它解析
        "metrics": metrics,
        "log": log,
    }
    exec("def _f():\n" + textwrap.indent(body, "    "), ns)   # noqa: S102 —— 见模块 docstring
    ns["_f"]()


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.reset_all()
    yield
    metrics.reset_all()


# --------------------------------------------------------------- 1. DB 连不上


def test_unavailable_db_sets_the_gauge_to_zero_and_says_so(monkeypatch):
    """连不上 ⇒ ``db_ready=0`` + **error** 级日志（不是「静默跳过 migration」）。

    ⚠️ 先把 gauge 置 1（模拟"上一次是好的"）再验它被**扳回 0**：``db_ready`` 是预声明的，
    不扳也能读到 0 —— 那样这条断言会连"压根没接线"一起放过去（0 与没接上分不开，
    正是 ``metrics`` 模块 docstring 里那条教训）。
    """
    log = _Log()
    metrics.set_db_gauge(1)
    monkeypatch.setattr(db, "is_available", lambda: False)
    monkeypatch.setattr(db, "migrate", lambda: pytest.fail("连不上就不该去 migrate"))
    run_migrations(log)

    assert metrics.get_gauge(metrics.GAUGE_DB_READY) == 0.0
    assert "error" in log.levels(), log.text()
    assert "schema 状态未知" in log.text(), log.text()


def test_unavailable_db_log_names_which_database(monkeypatch):
    """日志要能回答"连的是哪个库"，否则排查第一步就得再问一次。"""
    log = _Log()
    monkeypatch.setenv("WECHATPRO_DB_BACKEND", "mysql")
    monkeypatch.setenv("WECHATPRO_DB_NAME", "wechatpro_test")
    monkeypatch.setattr(db, "is_available", lambda: False)
    run_migrations(log)
    assert "wechatpro_test" in log.text(), log.text()


# --------------------------------------------------------------- 2. 成功


def test_a_successful_migration_sets_the_gauge_to_one(monkeypatch):
    log = _Log()
    monkeypatch.setattr(db, "is_available", lambda: True)
    monkeypatch.setattr(db, "migrate", lambda: {"drifted": [], "checksum": "ok"})
    run_migrations(log)

    assert metrics.get_gauge(metrics.GAUGE_DB_READY) == 1.0
    assert metrics.get_counter(metrics.COUNTER_DB_MIGRATION_FAILURES) == 0
    assert "error" not in log.levels(), log.text()


def test_drift_is_reported_but_does_not_unset_ready(monkeypatch):
    """漂移只报不改（R8）：就绪仍为 1，但日志里要有得数。"""
    log = _Log()
    monkeypatch.setattr(db, "is_available", lambda: True)
    monkeypatch.setattr(db, "migrate", lambda: {
        "drifted": ["wpp_hf_ledger.status: 类型 线上=varchar(8) 期望=varchar(16)"]})
    run_migrations(log)

    assert metrics.get_gauge(metrics.GAUGE_DB_READY) == 1.0
    assert "warning" in log.levels() and "漂移" in log.text(), log.text()
    assert "只报不改" in log.text()


# --------------------------------------------------------------- 3. 失败


def test_a_failed_migration_counts_a_failure_and_unsets_ready(monkeypatch):
    """migration 炸了：计数器 +1、gauge 归 0、日志 error —— 三处缺一不可。"""
    log = _Log()
    metrics.set_db_gauge(1)              # 同上：不先置 1 就验不出"被扳回 0"
    monkeypatch.setattr(db, "is_available", lambda: True)

    def _boom():
        raise db.DbError("migration 失败: 半套 schema")

    monkeypatch.setattr(db, "migrate", _boom)
    run_migrations(log)

    assert metrics.get_counter(metrics.COUNTER_DB_MIGRATION_FAILURES) == 1
    assert metrics.get_gauge(metrics.GAUGE_DB_READY) == 0.0
    assert "error" in log.levels() and "DB migration 未完成" in log.text(), log.text()


def test_a_failed_migration_does_not_block_the_plugin(monkeypatch):
    """**不阻断接入**是刻意的（DB 坏了不该连微信都上不去）—— 但必须吵。"""
    log = _Log()
    monkeypatch.setattr(db, "is_available", lambda: True)
    monkeypatch.setattr(db, "migrate", lambda: (_ for _ in ()).throw(RuntimeError("炸")))
    run_migrations(log)                      # 不抛就是通过
    assert metrics.get_counter(metrics.COUNTER_DB_MIGRATION_FAILURES) == 1


def test_an_unexpected_exception_is_also_counted(monkeypatch):
    """非 ``DbError``（pymysql 自己抛的）同样要计数 —— 不然"另一种坏法"看不见。"""
    log = _Log()
    monkeypatch.setattr(db, "is_available", lambda: True)
    monkeypatch.setattr(db, "migrate", lambda: (_ for _ in ()).throw(ValueError("奇怪的坏法")))
    run_migrations(log)
    assert metrics.get_counter(metrics.COUNTER_DB_MIGRATION_FAILURES) == 1
    assert "奇怪的坏法" in log.text(), "异常正文要进日志（否则只剩「未完成」三个字）"


# --------------------------------------------------------------- 4. 接线本身


def test_run_migrations_talks_to_the_real_health_instrument():
    """``metrics`` 必须是**模块级**导入的那份，不能是函数内临时造的对象。

    接线错了会 AttributeError（不阻断、只留日志），于是 gauge 永远不更新而没人发现。
    """
    tree = ast.parse((ROOT / ADAPTER_REL).read_text(encoding="utf-8"))
    imports = [(n.level, n.module, [a.name for a in n.names])
               for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    # 只要求「metrics 出现在模块级 from ..core import 的名单里」——
    # 不锁定它是否独占一行：import 合并/拆分是 ruff 的正常整理，
    # 断言精确的 names 列表会把风格调整当成回归（2026-10-18 踩过）。
    assert any(level == 2 and module == "core" and "metrics" in names
               for level, module, names in imports), imports
