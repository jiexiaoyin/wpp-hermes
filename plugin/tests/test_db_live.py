"""真库用例（D1，2026-10-02）——**唯一**碰真 MariaDB 的文件。

## 为什么必须有这一层

stub（``test_db.py``）验的是"框架发了什么语句、自己维护的状态对不对"，它**验不了**
真服务端的行为：``autocommit=False`` 下的事务/快照语义、``information_schema`` 的真实行形状、
``INT``→``int(11)`` 这类服务端补的显示宽度。D4/D5 两条修的恰恰是**真服务端语义**上的
静默故障（半套 schema、永远读同一个快照）—— 只在 stub 上验，等于没验。

## 三条护栏（任何一条不满足，这里**一个字节都不会发给生产**）

1. 库名：``conftest._env_guard`` 要求 ``WECHATPRO_DB_NAME`` **显式**设置、非 ``wechatpro``、
   且以 ``_test`` 结尾 —— 名字不对直接 fail。
2. host：``db.assert_host_allowed`` 只放行 127.0.0.1/localhost/::1（R3），要远程得显式
   设 ``WECHATPRO_DB_ALLOW_REMOTE=1``，而 conftest 见到这个 env 就 fail。
3. 只碰**自己建的临时表**（``wpp_selftest_*``）：用例自己 CREATE、结尾自己 DROP，
   绝不 DROP/ALTER 任何 ``wpp_`` 真表（R8：migration 框架也只 ADD）。

本机（dev 树所在机器）没有真测试库（3306/3307 是**别的项目**的 1Panel MariaDB）⇒ 这些用例
会**大声跳过**，那不是通过：真测试库 ``wechatpro_test`` 在 .183 上，用
``WECHATPRO_DB_NAME=wechatpro_test`` 跑它们（``tools/gates.sh`` 里也各挂了一道）。
"""

from __future__ import annotations

import time
import uuid

import pytest

from plugin.data import db
from plugin.data import migrations as M

pytestmark = pytest.mark.db


@pytest.fixture
def scratch():
    """一张属于本用例的临时表（名字带 uuid，用完必删）。"""
    name = "wpp_selftest_" + uuid.uuid4().hex[:10]
    conn = db.get_conn()
    conn.execute(f"CREATE TABLE {name} (id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
                 "v INT NOT NULL DEFAULT 0) ENGINE=InnoDB")
    try:
        yield name
    finally:
        try:
            db.get_conn().execute(f"DROP TABLE IF EXISTS {name}")
        except Exception:            # noqa: BLE001 —— 清理失败不该掩盖用例结论
            pass


# --------------------------------------------------------------- 1. 真库上的 migration


def test_migrate_is_idempotent_on_the_real_server():
    """真服务端上的幂等：第二次零新建/零补齐/零应用，checksum 转 ``ok``。"""
    first = M.migrate(db.get_conn())
    assert first["expected_tables"] == 17
    second = M.migrate(db.get_conn())
    assert second["created_tables"] == []
    assert second["added_columns"] == [] and second["added_indexes"] == []
    assert second["checksum"] == "ok"


def test_the_real_schema_has_no_definition_drift():
    """★ 真 ``information_schema`` 上的体检 —— ``int(11)`` / ``unsigned`` 这类
    服务端补出来的显示宽度就在这里显形（stub 只能验我们**以为**它会补什么）。"""
    result = M.migrate(db.get_conn())
    assert result["drifted"] == [], result["drifted"][:8]


def test_the_migration_framework_never_sends_modify_or_drop():
    """R8：真库上也不许出现 MODIFY / DROP / CHANGE / RENAME（``schema.sql`` 只有 ADD）。

    这条在真库上是**安全的**：migration 是幂等的，跑完不改变任何东西（见上面两条）。
    """
    before = _schema_signature()
    M.migrate(db.get_conn())
    assert _schema_signature() == before, "migration 不该改动已就绪的库"


def _schema_signature() -> str:
    """全部 ``wpp_%`` 表的列定义快照（排序后拼串）—— 用来断言"跑完没变"。"""
    rows = db.get_conn().query(
        "SELECT TABLE_NAME AS t, COLUMN_NAME AS c, COLUMN_TYPE AS ty, IS_NULLABLE AS n "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME LIKE 'wpp\\_%' ORDER BY TABLE_NAME, COLUMN_NAME")
    return "|".join(f"{r['t']}.{r['c']}:{r['ty']}:{r['n']}" for r in rows)


# --------------------------------------------------------------- 2. D5：快照必须收尾


def test_a_read_snapshot_is_released_so_later_writes_are_visible(scratch):
    """★ D5 的真库复现：**读也要提交**，否则这条线程永远读同一个快照。

    现场：``autocommit=False`` + REPEATABLE READ。同一线程的连接先 SELECT（开快照），
    另一个连接写入并提交；若读侧不收尾，第二次 SELECT 仍看见**旧**快照 ——
    症状是"新台账看不见、最后真发言时间不动"，**一行日志都没有**。

    读数不用 ``db.get_conn()``（那是线程内复用的），而是自己开一条独立连接当"别人"。
    """
    import pymysql
    from pymysql.cursors import DictCursor

    cfg = db.config()
    other = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                            password=cfg["password"], database=cfg["database"],
                            charset="utf8mb4", cursorclass=DictCursor, autocommit=True)
    try:
        reader = db.get_conn()
        assert reader.query_value(f"SELECT COUNT(*) AS n FROM {scratch}") == 0

        other.cursor().execute(f"INSERT INTO {scratch} (v) VALUES (7)")

        got = reader.query_value(f"SELECT COUNT(*) AS n FROM {scratch}")
        assert got == 1, ("读侧没有收尾 ⇒ 快照永远不刷新（D5）："
                          f"另一条连接写进去的行看不见（读到 {got}）")
    finally:
        other.close()


def test_a_write_on_the_real_server_is_visible_to_a_second_connection(scratch):
    """写侧收尾（旧有行为）—— 与上面那条配对，防止"其实谁都没提交过"也全绿。"""
    import pymysql
    from pymysql.cursors import DictCursor

    cfg = db.config()
    other = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                            password=cfg["password"], database=cfg["database"],
                            charset="utf8mb4", cursorclass=DictCursor, autocommit=True)
    try:
        db.get_conn().execute(f"INSERT INTO {scratch} (v) VALUES (11)")
        n = other.cursor()
        n.execute(f"SELECT COUNT(*) AS n FROM {scratch}")
        assert n.fetchone()["n"] == 1, "写侧不提交（或没落库）"
    finally:
        other.close()


# --------------------------------------------------------------- 3. D4：事务真的原子


def test_a_rolled_back_write_leaves_nothing_on_the_real_server(scratch):
    """★ D4 的真库复现：``with db.transaction()`` 里炸掉 ⇒ 一条都不落。

    这条是 D4 的**判据**：旧写法（裸 ``START TRANSACTION``）在 ``autocommit=False`` 下
    等于没开事务，中途炸掉留下的就是**半套**改动。
    """
    reader = db.get_conn()

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with db.transaction() as conn:
            conn.execute(f"INSERT INTO {scratch} (v) VALUES (1)")
            conn.execute(f"INSERT INTO {scratch} (v) VALUES (2)")
            raise _Boom("业务在中途炸了")

    assert reader.query_value(f"SELECT COUNT(*) AS n FROM {scratch}") == 0, \
        "回滚没生效 —— 半套改动留在库里（D4 的现场）"


def test_a_committed_transaction_lands_every_row(scratch):
    """配对：正常结束要**全部**落库（否则"回滚太狠"也能让上一条绿）。"""
    with db.transaction() as conn:
        conn.execute(f"INSERT INTO {scratch} (v) VALUES (1)")
        conn.execute(f"INSERT INTO {scratch} (v) VALUES (2)")
    assert db.get_conn().query_value(f"SELECT COUNT(*) AS n FROM {scratch}") == 2


#: 下面三条用例共用的假版本号（真 ``_MIGRATIONS`` 是空的；用完必须删干净）。
_FAKE_VERSION = 9001


def _new_scratch_table(conn) -> str:
    """自建临时表（不动 ``wpp_`` 真表）：migration 拿它当靶子。"""
    name = "wpp_selftest_" + uuid.uuid4().hex[:10]
    conn.execute(f"CREATE TABLE {name} (id INT NOT NULL PRIMARY KEY, v INT NOT NULL DEFAULT 0) "
                 "ENGINE=InnoDB")
    return name


def _columns_of(conn, table: str) -> list[str]:
    return [r["c"] for r in conn.query(
        "SELECT COLUMN_NAME AS c FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))]


def _fake_version_rows() -> int:
    return db.get_conn().query_value(
        "SELECT COUNT(*) AS n FROM wpp_schema_migrations WHERE version = %s", (_FAKE_VERSION,))


def test_a_failed_dml_migration_is_rolled_back(monkeypatch):
    """★ D4 端到端（**DML** 半段）：回填写一半炸掉 ⇒ 一行都不许留下。

    事务真正管用的就是这一类（``UPDATE``/``INSERT`` 回填）—— 也正是
    ``Migration`` 模板里那类动作。
    """
    conn = db.get_conn()
    name = _new_scratch_table(conn)

    def _half(c):
        c.execute(f"INSERT INTO {name} (id, v) VALUES (1, 1), (2, 2), (3, 3)")
        raise RuntimeError("回填炸了")

    monkeypatch.setattr(M, "_MIGRATIONS", (M.Migration(_FAKE_VERSION, "half_dml", _half),))
    try:
        with pytest.raises(RuntimeError, match="回填炸了"):
            M.migrate(conn)
        assert db.get_conn().query_value(f"SELECT COUNT(*) AS n FROM {name}") == 0, \
            "回滚没生效 —— 半套回填留在库里（D4 的现场）"
        assert _fake_version_rows() == 0, "炸掉的 migration 不许留下已应用记录（否则永不重跑）"
    finally:
        db.get_conn().execute(f"DROP TABLE IF EXISTS {name}")
        db.get_conn().execute("DELETE FROM wpp_schema_migrations WHERE version = %s",
                              (_FAKE_VERSION,))


def test_ddl_is_implicitly_committed_so_the_body_must_be_idempotent(monkeypatch):
    """★★ D4 的**真话**：MariaDB 的 ``ALTER`` 隐式提交 ⇒ 事务**兜不住 DDL**。

    这条用例存在的全部意义就是钉住这个事实（2026-10-02 在真库上第一次跑就抓到了：
    旧断言"半套 DDL 不许留下"是**错的**，而它在 stub 里永远是绿的 —— 因为桩的快照
    回滚比真服务端宽容）。真契约因此弱一条但可执行：

    1. DDL 会**留在库里**（隐式提交，回滚不掉）—— 不许假装它回滚了；
    2. 版本**不会被记录** ⇒ 下次 ``migrate()`` 重跑（漏记 = 永不重跑，比半套更要命）；
    3. 所以 body **必须幂等**（``ADD COLUMN IF NOT EXISTS``）⇒ 去掉故障重跑即收敛。

    靶子是自建临时表，恢复动作也在临时表上；``wpp_`` 真表一个字节都不动（R8）。
    """
    conn = db.get_conn()
    name = _new_scratch_table(conn)
    ddl = f"ALTER TABLE {name} ADD COLUMN IF NOT EXISTS half_done INT NULL"

    def _half(c):
        c.execute(ddl)
        raise RuntimeError("migration 炸了")

    monkeypatch.setattr(M, "_MIGRATIONS", (M.Migration(_FAKE_VERSION, "half_ddl", _half),))
    try:
        with pytest.raises(RuntimeError, match="migration 炸了"):
            M.migrate(conn)
        # ① DDL 已经落地：隐式提交，滚不回去（这是事实，不是我们希望的行为）
        assert "half_done" in _columns_of(conn, name), "DDL 没落地？MariaDB 的隐式提交呢"
        # ② 版本没记 ⇒ 会重跑（这一条比"回滚干净"更要紧）
        assert _fake_version_rows() == 0

        # ③ 幂等 ⇒ 故障排除后重跑即收敛，且只有一列 half_done
        monkeypatch.setattr(M, "_MIGRATIONS",
                            (M.Migration(_FAKE_VERSION, "half_ddl", lambda c: c.execute(ddl)),))
        M.migrate(conn)
        assert _columns_of(conn, name).count("half_done") == 1, "重跑没有收敛到一致状态"
        assert _fake_version_rows() == 1, "重跑成功了却没记版本 ⇒ 每次启动都要白跑一遍"
    finally:
        db.get_conn().execute(f"DROP TABLE IF EXISTS {name}")
        db.get_conn().execute("DELETE FROM wpp_schema_migrations WHERE version = %s",
                              (_FAKE_VERSION,))


# --------------------------------------------------------------- 4. 体检状态与真库


def test_health_says_ok_after_a_real_migration():
    db._note_health("unknown", "")
    db.migrate()
    h = db.health()
    assert h["state"] in ("ok", "drift"), h        # drift 应当为空，但那是上面那条的事
    assert h["ok"] is True and h["at"] >= int(time.time()) - 60
