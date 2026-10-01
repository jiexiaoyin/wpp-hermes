"""MariaDB 连接 + 事务 + 调用计数（自管，Hermes 无 DB 抽象）。

## 为什么自管

Hermes 没有通用 DB/ORM 抽象；``ctx.state`` 只有 **10 MiB** 配额
（``~/.hermes/plugin-data/<ns>/state.json``）⇒ **心流状态绝不能放那儿**。
所以走 pymysql 直连 MariaDB（计划 §6）。

## 三条设计约束

1. **pymysql 延迟导入到 ``_connect()`` 里** —— ``plugin_validate`` 会在隔离子进程
   里跑 ``register()``，那时既没装 pymysql 也不该连库（计划 §4）。
2. **host 白名单** —— 默认只允许 ``127.0.0.1``/``localhost``/``::1``（R3：防连上
   ``.182`` 的生产库）。要连远程必须显式 ``WECHATPRO_DB_ALLOW_REMOTE=1``。
3. **``db_call_count()`` 计数器** —— V7「心流热路径零 DB IO」的验收仪器。
   ``_get_conn()`` 是**所有** DB 访问的必经之地，计数放这里才拦得住。

## 线程模型

WS 收 / webhook 收 / 300s sweep 可能跑在**不同线程**，而 pymysql 连接不是
线程安全的 ⇒ 连接挂 ``threading.local()``。同一线程内复用，跨线程隔离。
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

#: 允许直连的 host（R3）。空字符串按 localhost 处理。
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 3306
_DEFAULT_USER = "wechatpro"
_DEFAULT_NAME = "wechatpro"

_local = threading.local()

# --- V7 仪器 ---------------------------------------------------------------
_count_lock = threading.Lock()
_db_calls = 0


def db_call_count() -> int:
    """本进程内 ``_get_conn()`` 被调用次数（**跨线程累计**）。"""
    with _count_lock:
        return _db_calls


def reset_db_call_count() -> None:
    """归零 —— 热路径用例前后各调一次（V7）。"""
    global _db_calls
    with _count_lock:
        _db_calls = 0


def _note_call() -> None:
    global _db_calls
    with _count_lock:
        _db_calls += 1


# --- 异常 ------------------------------------------------------------------


class DbError(RuntimeError):
    """所有 DB 层异常的基类。"""


class DbUnavailable(DbError):
    """连不上 / 依赖缺失 / 配置越界。调用方可据此优雅降级（启动期只 warn）。"""


class DbConfigError(DbError):
    """配置本身越界（例如 host 不在白名单）—— 这是**红线**，不降级。"""


# --- 配置 ------------------------------------------------------------------


def config() -> dict[str, Any]:
    """从 env 读连接参数。每次现读（便于测试用 monkeypatch 改 env）。"""
    host = os.environ.get("WECHATPRO_DB_HOST", _DEFAULT_HOST).strip()
    port_raw = os.environ.get("WECHATPRO_DB_PORT", str(_DEFAULT_PORT)).strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise DbConfigError(f"WECHATPRO_DB_PORT 不是整数: {port_raw!r}") from exc
    return {
        "host": host,
        "port": port,
        "user": os.environ.get("WECHATPRO_DB_USER", _DEFAULT_USER),
        "password": os.environ.get("WECHATPRO_DB_PASSWORD", ""),
        "database": os.environ.get("WECHATPRO_DB_NAME", _DEFAULT_NAME),
        "charset": "utf8mb4",
        "connect_timeout": int(os.environ.get("WECHATPRO_DB_CONNECT_TIMEOUT", "5")),
        "read_timeout": int(os.environ.get("WECHATPRO_DB_READ_TIMEOUT", "30")),
        "write_timeout": int(os.environ.get("WECHATPRO_DB_WRITE_TIMEOUT", "30")),
    }


def assert_host_allowed(cfg: Mapping[str, Any] | None = None) -> None:
    """R3：拒连非本地 host，除非显式豁免。"""
    c = dict(cfg or config())
    host = str(c.get("host", ""))
    if host in _ALLOWED_HOSTS:
        return
    if os.environ.get("WECHATPRO_DB_ALLOW_REMOTE") == "1":
        logger.warning("[WPP] DB 连的是非本地 host %s（WECHATPRO_DB_ALLOW_REMOTE=1 已显式放行）", host)
        return
    raise DbConfigError(
        f"WECHATPRO_DB_HOST={host!r} 非本地 —— 本插件只允许 127.0.0.1/localhost/::1。"
        " 确需远程请显式设 WECHATPRO_DB_ALLOW_REMOTE=1"
    )


def describe() -> str:
    """一行连接描述（**不含密码**），供日志/错误信息用。"""
    c = config()
    return f"{c['user']}@{c['host']}:{c['port']}/{c['database']}"


# --- 连接 ------------------------------------------------------------------


class Connection:
    """pymysql 连接的薄包装：统一 ``query`` / ``execute`` 两个口子。

    包一层不是为了好看，是为了让 ``_get_conn()`` 成为**唯一**的 DB 出入口 ——
    V7 的计数器才有意义。
    """

    __slots__ = ("_conn",)

    def __init__(self, raw) -> None:
        self._conn = raw

    @staticmethod
    def _run(cur, sql: str, params) -> None:
        """有参数才交给 pymysql 插值。

        ⚠️ **无参数时必须走单参形式**：pymysql 的 ``execute(sql, ())`` 仍会做
        ``query % args`` 插值，于是 SQL 里任何字面量 ``%`` 都会炸 ——
        ``SHOW TABLES LIKE 'wpp_hf_%'`` 报
        ``ProgrammingError: not enough arguments for format string``。
        这是 2026-10-01 实测踩到的，不是假设。
        """
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)

    # 只读
    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[dict]:
        cur = self._conn.cursor()
        try:
            self._run(cur, sql, params)
            return list(cur.fetchall())
        finally:
            cur.close()

    def query_one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def query_value(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = (), default=None):
        row = self.query_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()), default)

    # 写
    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> int:
        """执行一条语句，返回受影响行数。不在显式事务里时**立即提交**。"""
        cur = self._conn.cursor()
        try:
            self._run(cur, sql, params)
            affected = cur.rowcount
        finally:
            cur.close()
        if getattr(_local, "depth", 0) == 0:
            # 读也要收尾：InnoDB REPEATABLE READ 下挂着不提交会一直攥着旧快照
            self._conn.commit()
        return affected

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> int:
        cur = self._conn.cursor()
        try:
            cur.executemany(sql, list(seq))
            affected = cur.rowcount
        finally:
            cur.close()
        if getattr(_local, "depth", 0) == 0:
            self._conn.commit()
        return affected

    def insert_id(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> int:
        cur = self._conn.cursor()
        try:
            self._run(cur, sql, params)
            rid = cur.lastrowid
        finally:
            cur.close()
        if getattr(_local, "depth", 0) == 0:
            self._conn.commit()
        return int(rid or 0)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:                      # 关闭失败不该炸调用方
            pass

    @property
    def open(self) -> bool:
        return bool(getattr(self._conn, "open", False))


def _connect() -> Connection:
    cfg = config()
    assert_host_allowed(cfg)                   # 红线先查，避免白连一次
    try:
        import pymysql                         # 延迟导入（见模块 docstring 约束 1）
    except ImportError as exc:
        raise DbUnavailable(
            "pymysql 未安装 —— 注意要装在**环境 venv**里"
            "（~/.hermes/installs/<h>/environments/<h>/venv），不是隔离解释器"
        ) from exc
    from pymysql.cursors import DictCursor

    try:
        raw = pymysql.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"],
            password=cfg["password"], database=cfg["database"], charset=cfg["charset"],
            cursorclass=DictCursor, autocommit=False,
            connect_timeout=cfg["connect_timeout"],
            read_timeout=cfg["read_timeout"], write_timeout=cfg["write_timeout"],
        )
    except Exception as exc:
        raise DbUnavailable(f"连库失败 {describe()}: {exc}") from exc
    return Connection(raw)


def get_conn() -> Connection:
    """取本线程的连接（不存在则连）。**所有** DB 访问的唯一入口。"""
    _note_call()
    conn = getattr(_local, "conn", None)
    if conn is not None and conn.open:
        return conn
    if conn is not None:
        conn.close()
    conn = _connect()
    _local.conn = conn
    return conn


#: 兼容旧树命名（旧 ``db.py`` 是 ``_get_conn()``，迁移期两边都在）
_get_conn = get_conn


def close() -> None:
    """关掉本线程的连接（sweep 任务收尾、测试 teardown 用）。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
    _local.depth = 0


@contextmanager
def transaction():
    """显式事务。**可重入**：内层 commit/rollback 延到最外层。

    事务里调 ``execute()`` 不会提前提交（``_local.depth`` 起作用），
    这是「先写 ledger 再写 group_state」这类多表写能保住原子性的前提。
    """
    conn = get_conn()
    depth = getattr(_local, "depth", 0)
    _local.depth = depth + 1
    try:
        yield conn
        if _local.depth == 1:
            conn.commit()
    except Exception:
        if _local.depth == 1:
            conn.rollback()
        raise
    finally:
        _local.depth = depth


def is_available() -> bool:
    """连得上吗？—— 不抛，给启动期做降级判断用。"""
    try:
        get_conn().query("SELECT 1 AS ok")
        return True
    except DbError:
        return False
    except Exception:
        return False


def migrate() -> dict:
    """跑 migration（``data/migrations.py``）。失败抛 ``DbError``，由调用方决定是否降级。"""
    from . import migrations

    try:
        return migrations.migrate(get_conn())
    except DbError:
        raise
    except Exception as exc:
        raise DbError(f"migration 失败 ({describe()}): {exc}") from exc
