"""真库用例的**护栏**回归（D1，2026-10-02）—— ``conftest._env_guard``。

## 为什么给护栏本身写测试

护栏坏了不会红：它会**绿着**把真库用例放到本机的 3306 上（那是**别的项目**的
1Panel MariaDB），或者更糟 —— 放到生产库上。老板的钦定红线是「生产密钥/生产库
永不可用于调试」，这条线的唯一执行者就是 ``conftest.py`` 的几十行判定，
而它此前**一条判据都没有**（真库用例本身也是这次才有的）。

## ⚠️ 判结局要看**类型**，不能让 ``pytest.skip`` 逃出去

第一版这里写的是 ``pytest.raises(AssertionError)``：把某道闸拆掉之后，用例走到
``pytest.skip`` 就**变成绿色的 skip** —— pytest 退出码仍是 0，于是变异"存活"，
测试形同没写。所以下面统一用 :func:`_outcome` 把三种结局（放行 / 跳过 / 抛错）
**抓在手里**再断言，跳过与放行都算"这道闸没起作用"。
"""

from __future__ import annotations

import pytest
from _pytest.outcomes import Skipped

import conftest
from plugin.data import db


class _Req:
    """最小 request 替身：``_env_guard`` 只看 ``keywords``。"""

    def __init__(self, *marks: str) -> None:
        self.keywords = {m: True for m in marks}


def _outcome(fn) -> tuple[str, BaseException | str]:
    """``("ok"|"skip"|"raise", ...)`` —— 三种结局都抓住，一个都不许逃。"""
    try:
        fn()
    except Skipped as exc:
        return ("skip", str(exc))
    except BaseException as exc:                      # noqa: BLE001 —— 断言类型在下面各条里查
        return ("raise", exc)
    return ("ok", "")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("WECHATPRO_DB_NAME", "WECHATPRO_DB_ALLOW_REMOTE", "WPP_REQUIRE_LIVE_DB"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def _no_reach_check(monkeypatch):
    """名字闸之后那一关打成**显式的红**：走到它 = 名字闸没拦住。"""
    def _fail():
        raise AssertionError("名字闸没拦下 —— 已经走到可达性检查了")

    monkeypatch.setattr(conftest, "_require_reachable_test_db", _fail)


def test_a_non_db_test_is_never_gated(monkeypatch):
    monkeypatch.setenv("WECHATPRO_DB_NAME", "wechatpro")      # 生产库名也不行……
    assert _outcome(lambda: conftest._env_guard(_Req("network")))[0] == "ok"


def test_remote_db_is_a_red_line_even_without_the_marker(monkeypatch):
    """R3：``WECHATPRO_DB_ALLOW_REMOTE=1`` 出现在测试进程里就该炸 —— 无一例外。"""
    monkeypatch.setenv("WECHATPRO_DB_ALLOW_REMOTE", "1")
    kind, exc = _outcome(lambda: conftest._env_guard(_Req()))
    assert kind == "raise" and "ALLOW_REMOTE" in str(exc)


def test_a_db_test_without_explicit_name_skips_loudly(_no_reach_check):
    """缺省名就是生产库 ⇒ 必须**显式**声明。跳过，且理由要写在脸上。"""
    kind, exc = _outcome(lambda: conftest._env_guard(_Req("db")))
    assert kind == "skip" and "这不是通过" in str(exc), (kind, exc)


def test_naming_the_production_db_is_a_hard_failure(monkeypatch, _no_reach_check):
    """钦定红线：**跳过也不许** —— 有人把生产库名填进来，就该红给他看。"""
    monkeypatch.setenv("WECHATPRO_DB_NAME", "wechatpro")
    kind, exc = _outcome(lambda: conftest._env_guard(_Req("db")))
    assert kind == "raise" and "生产库" in str(exc), (kind, exc)


def test_a_name_that_just_looks_like_a_test_db_is_rejected(monkeypatch, _no_reach_check):
    """``wechatpro_staging`` 这种"差一点"的名字也要挡住 —— 判据是名字，不是感觉。"""
    monkeypatch.setenv("WECHATPRO_DB_NAME", "wechatpro_staging")
    kind, exc = _outcome(lambda: conftest._env_guard(_Req("db")))
    assert kind == "raise" and "_test" in str(exc), (kind, exc)


def test_a_proper_test_db_name_passes_the_name_gate(monkeypatch):
    """名字对了就往下走（可达性那一关由下面几条单验）。"""
    monkeypatch.setenv("WECHATPRO_DB_NAME", "wechatpro_test")
    reached: list[bool] = []
    monkeypatch.setattr(conftest, "_require_reachable_test_db",
                        lambda: reached.append(True))
    assert _outcome(lambda: conftest._env_guard(_Req("db")))[0] == "ok"
    assert reached == [True], "过了名字闸就该去查可达性（否则真库用例永远不跑）"


def test_an_unreachable_test_db_skips(monkeypatch):
    monkeypatch.setenv("WECHATPRO_DB_NAME", "wechatpro_test")
    monkeypatch.setattr(db, "is_available", lambda: False)
    monkeypatch.setattr(db, "health", lambda: {"detail": "连不上"})
    kind, exc = _outcome(conftest._require_reachable_test_db)
    assert kind == "skip" and "这不是通过" in str(exc), (kind, exc)


def test_a_broken_connection_also_skips(monkeypatch):
    monkeypatch.setenv("WECHATPRO_DB_NAME", "wechatpro_test")

    def _boom():
        raise db.DbUnavailable("pymysql 未安装")

    monkeypatch.setattr(db, "is_available", _boom)
    kind, exc = _outcome(conftest._require_reachable_test_db)
    assert kind == "skip" and "pymysql 未安装" in str(exc), (kind, exc)


def test_require_live_db_turns_the_skip_into_a_failure(monkeypatch):
    """★ 真机 上的口径：本该有真库的机器上，缺席=**红**（跳过是诚实，不是通过）。"""
    monkeypatch.setenv("WECHATPRO_DB_NAME", "wechatpro_test")
    monkeypatch.setenv("WPP_REQUIRE_LIVE_DB", "1")
    monkeypatch.setattr(db, "is_available", lambda: False)
    monkeypatch.setattr(db, "health", lambda: {"detail": "连不上"})
    kind, exc = _outcome(conftest._require_reachable_test_db)
    assert kind == "raise" and "WPP_REQUIRE_LIVE_DB" in str(exc), (kind, exc)


def test_require_live_db_also_covers_the_missing_name(monkeypatch):
    monkeypatch.setenv("WPP_REQUIRE_LIVE_DB", "1")
    kind, exc = _outcome(lambda: conftest._env_guard(_Req("db")))
    assert kind == "raise" and "WPP_REQUIRE_LIVE_DB" in str(exc), (kind, exc)
