"""pytest 全局夹具 —— **安全默认值优先**（本仓的测试跑在老板的真机器上）。

三条默认值，全部是"默认禁止，要开显式开"：

1. **禁网**：默认把 ``socket.connect`` 打掉。心流的 judge 热路径 / 预算 / 去重
   都不该产生任何网络 IO —— 这条不是为了跑得快，是为了让"某处偷偷发了个请求"
   **在测试里就炸**，而不是等到 真机 上才发现。
2. **禁生产库**：带 ``db`` 标记的用例（真库用例，见 ``test_db_live.py``）必须显式
   声明 ``WECHATPRO_DB_NAME`` 且**必须以 ``_test`` 结尾**；指到生产库名 ``wechatpro``
   或没设就**当场 fail / skip**，绝不放行。缺省值就是 ``wechatpro``（生产库名），
   而"忘了设环境变量"是最容易发生、后果最重的一种失误 —— 所以判据是**必须显式声明**。
   库连不上（本机没有那个库；真测试库在 真机 上）⇒ **大声跳过**，那不算通过。
3. **禁生产 bot**：配置了 ``WPP_TEST_PROD_WXID``（或未跟踪的 ``conftest_local.py``）时，
   任何用例的环境变量里出现该 wxid 一律 fail。铁律：**绝不能**拿生产号连调试实例
   （厂商的客户端实例锁会把生产踢下线并劫持它的 webhook）。公开仓默认不配置。

外加一条心流专用的：**每个用例前后清空心流内存缓存**（见 :func:`_reset_hf_caches`）。
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

# 让 ``import plugin.*`` 在任何调用姿势下都成立（``pytest plugin/tests`` 与
# ``pytest`` 两种跑法的 rootdir 推断不同，靠 conftest 自己兜住）。
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

#: 生产库名（``plugin/data/db.py`` 的 ``_DEFAULT_NAME``）—— 测试**绝不允许**指到它。
PROD_DB_NAME = "wechatpro"

#: 允许真库用例连的库名后缀（``wechatpro_test``）。**必须显式**，不给"看着像"留口子。
TEST_DB_SUFFIX = "_test"

#: 生产 bot 的 wxid。公开仓**不带**任何生产标识 —— 本机通过
#: ``WPP_TEST_PROD_WXID`` 或未跟踪的 ``conftest_local.py`` 注入；
#: 未配置时这条护栏自动跳过（第三方用户没有生产号需要防）。
def _local_prod_wxid() -> str:
    value = os.environ.get("WPP_TEST_PROD_WXID", "").strip()
    if value:
        return value
    try:
        from conftest_local import PROD_SELF_WXID as local  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — 没有本地覆盖是公开仓的正常状态
        return ""
    return str(local or "").strip()


PROD_SELF_WXID = _local_prod_wxid()


# --------------------------------------------------------------------- 安全护栏


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "network: 允许真实网络（默认禁网）")
    config.addinivalue_line(
        "markers", "db: 需要真库（须显式 WECHATPRO_DB_NAME=<*_test>；连不上则**大声跳过**）")


def pytest_collection_modifyitems(config, items) -> None:
    """允许真实网络/真库的用例，收集时逐个点名（默认是禁网 + 禁库的）。"""
    for item in items:
        if "network" in item.keywords:
            print(f"  ⚠️ 该用例允许真实网络: {item.nodeid}")
        if "db" in item.keywords:
            print(f"  ⚠️ 该用例允许连**本地测试库**: {item.nodeid}")


def _env_guard(request) -> None:
    if os.environ.get("WECHATPRO_DB_ALLOW_REMOTE") == "1":
        raise AssertionError(
            "测试环境里设了 WECHATPRO_DB_ALLOW_REMOTE=1 —— 这会放行非本地 DB。"
            "铁律：DB 只允许 localhost。请去掉它。")

    if "db" not in request.keywords:
        return
    name = os.environ.get("WECHATPRO_DB_NAME", "").strip()
    if not name:
        _skip_or_fail(
            f"⏭  真库用例跳过：未显式设 WECHATPRO_DB_NAME（缺省值 {PROD_DB_NAME!r} 是**生产库**，绝不使用）。"
            f" 这不是通过 —— 真测试库在 真机 上，例: WECHATPRO_DB_NAME=wechatpro_test "
            f"WECHATPRO_DB_HOST=127.0.0.1")
    if name == PROD_DB_NAME:
        raise AssertionError(
            f"WECHATPRO_DB_NAME={name!r} 是**生产库**。钦定红线：生产密钥/生产库永不可用于调试。"
            f"请用 wechatpro_test。")
    if not name.endswith(TEST_DB_SUFFIX):
        raise AssertionError(
            f"WECHATPRO_DB_NAME={name!r} 不以 {TEST_DB_SUFFIX!r} 结尾 —— 真库用例只许连"
            f"**显式命名的测试库**（如 wechatpro{TEST_DB_SUFFIX}）。判据是名字，不是「看着像测试库」。")
    _require_reachable_test_db()


def _skip_or_fail(msg: str) -> None:
    """真库缺席时**跳过**；但门禁在 真机 上会设 ``WPP_REQUIRE_LIVE_DB=1`` ⇒ 那时缺席=**红**。

    为什么要有这个开关：跳过是"本机没有参照物"的诚实表达（照 ``tools/gates.sh`` 里 TS 差分
    那条的口径），但到了**本该有真库**的机器上还跳过，就变成"看起来绿"。两种情形必须分开。
    """
    if os.environ.get("WPP_REQUIRE_LIVE_DB") == "1":
        raise AssertionError(msg + " —— 但 WPP_REQUIRE_LIVE_DB=1 要求真库必须可达（不许跳过）。")
    pytest.skip(msg)


def _require_reachable_test_db() -> None:
    """连得上吗？连不上就**大声跳过** —— 本机（dev 树所在机器）没有真测试库。

    ⚠️ 走到这里检查的库名已经过上面三道闸（非空 / 非生产 / ``*_test``），
    而 ``db.get_conn()`` 自己还有 R3 host 白名单 —— 三处叠加，**不可能**连到生产库。
    """
    from plugin.data import db

    try:
        ok = db.is_available()
    except Exception as exc:  # noqa: BLE001 —— 连库失败只该跳过，不该把收集期炸掉
        _skip_or_fail(f"⏭  真库用例跳过：连不上测试库（{exc}）。这不是通过。")
        return
    if not ok:
        _skip_or_fail(
            f"⏭  真库用例跳过：测试库不可达（{db.health().get('detail') or db.describe()}）。"
            " 这不是通过 —— 真测试库在 真机 上。")


def _release_db() -> None:
    """真库用例收尾：关掉本线程连接（线程内复用 ⇒ 不关就跨用例带着旧快照）。"""
    try:
        from plugin.data import db

        db.close()
    except Exception:  # noqa: BLE001 —— 收尾失败不该让用例变红
        pass


def _forbid_sockets(monkeypatch) -> None:
    """把出站连接打掉；``network`` 标记的用例会跳过它。"""

    def _blocked(*_a, **_kw):
        raise AssertionError(
            "测试默认禁网：出现了真实的 socket 连接。"
            "若这是有意的，给用例加 @pytest.mark.network。")

    monkeypatch.setattr(socket.socket, "connect", _blocked, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked, raising=True)


# ------------------------------------------------------------------ 心流缓存


def _reset_hf_caches() -> None:
    """清空**全部**已登记的心流内存缓存（七处，见 ``runtime.CACHE_REGISTRY``）。

    不清就会**跨用例互相污染**：前一个用例学会的阈值 / 记的预算额度漂到后一个，
    失败长相是业务 bug，极难定位。所以做成 autouse，谁也不用记得调。

    容错：心流包还没建好时不报错（本夹具在九模块全部就位前就要能用）。
    """
    try:
        from plugin.inbound.heartflow.runtime import CACHE_REGISTRY
    except Exception:  # noqa: BLE001 —— 未就位不算错
        return
    CACHE_REGISTRY.reset_all()


# --------------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _test_env(request, monkeypatch):
    """每个用例的默认环境：安全护栏 + 禁网 + 清心流缓存。"""
    # 生产 bot 不得出现在测试里（扫已设的环境变量，够用了）。
    # 未配置生产号（公开仓默认）时跳过这条护栏。
    if PROD_SELF_WXID:
        for k, v in os.environ.items():
            assert PROD_SELF_WXID not in str(v), (
                f"环境变量 {k} 里出现生产 bot {PROD_SELF_WXID} —— 绝不能用生产号连调试实例")

    _env_guard(request)
    # 厂商地址现在是必填项（公开仓库不带默认域名）。测试统一给占位值；
    # 专门测「未配置」的用例自己 delenv 覆盖。受保护身份集合同理默认清空。
    monkeypatch.setenv("WECHATPRO_API_BASE", "https://vendor.test")
    for k in ("WECHATPRO_WS_URL", "WECHATPRO_PROTECTED_WXIDS",
              "WECHATPRO_PROTECTED_FINGERPRINTS", "WECHATPRO_ALLOW_PRODUCTION_WXID"):
        monkeypatch.delenv(k, raising=False)
    if "network" not in request.keywords and "db" not in request.keywords:
        # 真库用例要连 localhost —— 它的 host 由 R3 白名单 + 库名三道闸管着（见 _env_guard）
        _forbid_sockets(monkeypatch)

    _reset_hf_caches()
    yield
    if "db" in request.keywords:
        _release_db()
    _reset_hf_caches()
