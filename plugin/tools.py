"""Hermes 客户端工具入口（manifest ``provides_tools`` 的落点）。

Hermes 对 **bundled/deferred** 平台插件会先 import ``<plugin>.tools`` 并调用
``register_tools(ctx)``，只注册"模型能调的工具面"、不加载平台适配器 —— 这样
CLI/TUI 进程里 ``hermes tools`` 也能看到这些工具（见
``hermes_cli/plugins_loader.py::_register_deferred_platform_tools``）。
用户目录插件目前仍是 eager 加载，但两条路径都指向**同一个**入口，行为一致。

## 为什么实现不在本文件

``plugin/toolkit/`` 是工具实现包（注册机器 / 各域 / 厂商端点表）。本文件只留
一个**按 manager 幂等**的壳：``register(ctx)`` 与 ``register_tools(ctx)`` 可能在
同一进程里先后被调用（deferred 预注册 + 平台物化），重复注册会让工具表翻倍。
标记按 ``ctx._manager`` 的对象身份记 —— 同一 PluginManager（同一进程的同一注册
面）只注册一次；测试里那种每次新建的假 ctx 没有 ``_manager``，照常每次注册。

## 依赖约束

模块级只允许标准库导入（``plugin_validate`` 的隔离 probe 解释器没有
aiohttp/pymysql）；重依赖全部在调用期由 ``toolkit`` 内部导入。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: ``id(PluginManager) -> PluginManager``。强引用同时防止 id 被回收后复用。
_REGISTERED_MANAGERS: dict[int, object] = {}


def register_tools(ctx) -> None:
    """Hermes ``provides_tools`` 入口：注册全部客户端工具（同一 manager 幂等）。"""
    manager = getattr(ctx, "_manager", None)
    if manager is not None and id(manager) in _REGISTERED_MANAGERS:
        log.info("[WPP] 工具面已注册（manager=%s），跳过重复注册", type(manager).__name__)
        return

    from .toolkit import registration as _registration

    _registration.register_tools(ctx)
    report = getattr(_registration, "REGISTRATION_REPORT", {}) or {}
    if report.get("ok", True):
        if manager is not None:
            _REGISTERED_MANAGERS[id(manager)] = manager
        log.info("[WPP] 工具面注册完成")
        return
    # ⚠️ 对账没过 = **半套工具面**：不登记 manager，让下一次入口重试；同时把
    #   "注册成功"那句换成 error —— 否则日志里一切正常，模型那边少了一半工具。
    problems = report.get("problems") or []
    log.error("[WPP] 工具面注册不完整（%d 处），不标记为已注册：%s",
              len(problems), "；".join(map(str, problems)))
