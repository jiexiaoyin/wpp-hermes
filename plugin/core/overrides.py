"""filehelper 命令的**运行期配置覆盖**存储（内存，不落盘）。

## 为什么单独成模块

旧树里这三个函数住在 ``plugin/commands.py``（命令处理器）里，于是
``plugin/config.py``（配置层）为了读覆盖，必须 ``from . import commands``——
**配置层反向依赖命令层**。命令层又回头 ``from .config import resolve_account_config``，
两边互相 import。搬到 ``core/`` 之后方向是单向的：

    platform/commands.py     ──→ core/overrides.py（只读：判当前状态）
    core/account_store.py    ──→ core/overrides.py（写回后清同键覆盖）
    core/accounts.py         ──→ core/overrides.py（解析时并入 ③ 层）

## 2026-10-02（W6）：**命令面不再写这里**

在此之前 filehelper 命令写的就是本模块，于是六条命令全是「回了 ✅、当次不生效
（``Account`` 是 frozen 的、``connect()`` 只解析一次并缓存）、重启就丢」。
现在命令走 ``core/account_store.write_account_key`` **落盘**，本模块只剩两个用途：

1. **读**：``core.accounts.resolve()`` 的 ③ 层（优先级见该模块 docstring）；命令面
   判状态时也顺带看它（``commands._feature_state`` 等）。
2. **测试 / 紧急热改的注入点**：``set_override`` 仍在（``test_accounts`` 用它钉住
   分层优先级），但**线上没有调用方**。

⚠️ 正因如此，``account_store.write_account_key`` 写完文件**必须**顺手
``clear_override(account_id, key)``：③ 层压在 ① 层之上，残留的覆盖会把刚写的新值
影子掉 —— 那就成了"文件改了、链路读到的还是旧值"，比不做更坏。

## 语义（照抄旧实现，一处未改）

- **只在内存里**：进程重启即失效，不回写 ``accounts/*.json``。
- ``set_override(account_id, key, value)`` 的 ``key`` 是**原始 cfg 键**
  （``heartflow`` / ``allowFrom`` / ``groupAllowFrom`` …），因为
  ``core.accounts.resolve()`` 是把覆盖项**并入 cfg 字典**再解析成 ``Account`` 的。
  写成驼峰以外的名字不会被读到。
- 值为 dict 且 cfg 同名键也是 dict 时**浅合并**（见 ``resolve()`` ③ 段），
  所以 ``set_override(id, "heartflow", {**现有, "enabled": True})`` 不会抹掉阈值等同级键。
- ``get_override()`` 用 ``setdefault`` ⇒ **会创建空条目**。返回的是**活引用**，
  调用方不该就地改（要改走 ``set_override``）。保留这个行为是为了与旧实现逐位一致。

## 生效时机

``resolve()`` 每次调用都重读覆盖 ⇒ 覆盖只影响**此后**的解析结果。
``Account`` 是 frozen 的，且 ``adapter.connect()`` 只解析一次并缓存 ——
所以覆盖在**下一次账号解析**（``adapter.reload_account`` / 重连 / 进程重启）后
才对已在跑的链路生效。命令面那条路已经改走账号文件 + ``reload_account``（见上）。
"""
from __future__ import annotations

# 内存配置覆盖（命令修改后写入，config 读取时优先）
_runtime_overrides: dict[str, dict] = {}


def get_override(account_id: str) -> dict:
    return _runtime_overrides.setdefault(account_id, {})


def set_override(account_id: str, key: str, value) -> None:
    _runtime_overrides.setdefault(account_id, {})[key] = value


def clear_override(account_id: str, key: str) -> None:
    _runtime_overrides.setdefault(account_id, {}).pop(key, None)
