"""共享夹具：**空壳 Hermes 环境**。

## 为什么要提到 conftest

``stubbed`` 原来住在 ``test_registration.py`` 里，``test_voice.py`` / ``test_skills.py``
靠 pytest「同目录按名字找到它」的副作用跨模块用。那在两件事上会翻车：

1. **单文件跑**（``pytest plugin/tests/test_voice.py``）—— 名字找不到 ⇒ 整文件 ERROR；
2. ``tools/mutation_check.py`` 正是**单文件**跑测试的（``run_tests(test_file)``），
   于是变异体会被判成 BROKEN（"收集期就崩"），而 BROKEN **不计入杀死数** ——
   变异套件会静默失去意义。

夹具是环境，不是某个用例的私产；放 conftest 才是它该在的地方。

## 空壳是什么

``plugin/platform/adapter.py`` 模块级 ``from gateway.config import Platform``
（Hermes 独有，本机没有），以及注册期用不到、真用时才在函数体里 import 的运行期依赖
（aiohttp / websockets / pymysql）。装上之后 ``register()`` 在本机才能真跑一遍。

⚠️ 装的是**进程级** ``sys.modules`` 条目，用完必须**恢复原样**：留在那里会让后续
用例以为真有个 ``gateway`` 包，把"环境不干净"伪装成"代码没问题"。
"""

from __future__ import annotations

import sys

import pytest

from . import _hermes_stub


class _MISSING_TYPE:
    pass


_MISSING = _MISSING_TYPE()


def _probe():
    """返回 Hermes 空壳模块。

    历史上这里按路径加载 ``tools/probe_registration.py``（内部探针）。
    公开仓库不包含 ``tools/``，空壳已迁到 ``plugin/tests/_hermes_stub.py``，
    测试与内部探针共用同一份清单。
    """
    return _hermes_stub


@pytest.fixture(autouse=True)
def _isolated_accounts(tmp_path, monkeypatch):
    """**任何**用例都不许写到真账号目录 / 真备份目录。

    为什么必须是 autouse（而不是"谁用到谁自己指"）：W6 之后一条
    ``/affection on`` 就会**落盘**，而 ``plugin/accounts/`` 里躺的是
    **真实账号（含 authcode）**，落盘路径还会顺手备份一份。少指一次环境变量，
    一次跑测试就等于把生产的账号文件改掉 + 在 /data 下堆备份。

    用例自己的 ``monkeypatch.setenv("WECHATPRO_ACCOUNTS_DIR", …)`` 照旧生效
    （后设的覆盖这里设的），不会打架。
    """
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setenv("WECHATPRO_ACCOUNT_BACKUP_DIR", str(tmp_path / "account-backups"))
    yield


@pytest.fixture(scope="module")
def stubbed():
    """装上 gateway 空壳（+ 缺的运行期依赖），跑完**恢复原样**。"""
    probe = _probe()
    saved = {k: sys.modules.get(k, _MISSING) for k in probe._HERMES_INTERNAL}
    runtime = probe._stub_runtime()          # aiohttp/websockets/pymysql：缺了才顶
    saved_runtime = {k: sys.modules.get(k, _MISSING) for k in runtime}
    probe._stub_internal()
    try:
        yield probe
    finally:
        for k, v in {**saved, **saved_runtime}.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
