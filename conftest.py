"""pytest 全局夹具 —— **安全默认值优先**（本仓的测试跑在老板的真机器上）。

三条默认值，全部是"默认禁止，要开显式开"：

1. **禁网**：默认把 ``socket.connect`` 打掉。心流的 judge 热路径 / 预算 / 去重
   都不该产生任何网络 IO —— 这条不是为了跑得快，是为了让"某处偷偷发了个请求"
   **在测试里就炸**，而不是等到 .183 上才发现。
2. **禁生产库**：``WECHATPRO_DB_NAME`` 未显式指到非生产库时，任何带 ``db`` 标记的
   用例直接 fail。缺省值就是 ``wechatpro``（生产库名），而"忘了设环境变量"是
   最容易发生、后果最重的一种失误 —— 所以判据是**必须显式声明**，不是"看着像就行"。
3. **禁生产 bot**：任何用例里出现 ``wxid_eezdbu1ytws422``（益融小助理，生产号）
   一律 fail。铁律：**绝不能**拿生产号连调试实例（厂商的客户端实例锁会把生产踢下线
   并劫持它的 webhook）。

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

#: 生产 bot 的 wxid（益融小助理）。出现即 fail。
PROD_SELF_WXID = "wxid_eezdbu1ytws422"


# --------------------------------------------------------------------- 安全护栏


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "network: 允许真实网络（默认禁网）")
    config.addinivalue_line("markers", "db: 需要数据库（默认按生产库名护栏检查环境变量）")


def pytest_collection_modifyitems(config, items) -> None:
    """带 ``network`` 标记的用例不挂禁网夹具（夹具自己按 marker 判断，这里只做提醒）。"""
    for item in items:
        if "network" in item.keywords:
            print(f"  ⚠️ 该用例允许真实网络: {item.nodeid}")


def _env_guard(request) -> None:
    if os.environ.get("WECHATPRO_DB_ALLOW_REMOTE") == "1":
        raise AssertionError(
            "测试环境里设了 WECHATPRO_DB_ALLOW_REMOTE=1 —— 这会放行非本地 DB。"
            "铁律：DB 只允许 localhost。请去掉它。")

    if "db" not in request.keywords:
        return
    name = os.environ.get("WECHATPRO_DB_NAME", "").strip()
    if not name:
        raise AssertionError(
            f"带 db 标记的用例要求**显式**设 WECHATPRO_DB_NAME（不得让缺省值 {PROD_DB_NAME!r} 生效）。"
            f"例: WECHATPRO_DB_NAME=wechatpro_test")
    if name == PROD_DB_NAME:
        raise AssertionError(
            f"WECHATPRO_DB_NAME={name!r} 是**生产库**。钦定红线：生产密钥/生产库永不可用于调试。"
            f"请用 wechatpro_test。")


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
    # 生产 bot 不得出现在测试里（扫已设的环境变量，够用了）
    for k, v in os.environ.items():
        assert PROD_SELF_WXID not in str(v), (
            f"环境变量 {k} 里出现生产 bot {PROD_SELF_WXID} —— 绝不能用生产号连调试实例")

    _env_guard(request)
    if "network" not in request.keywords:
        _forbid_sockets(monkeypatch)

    _reset_hf_caches()
    yield
    _reset_hf_caches()
