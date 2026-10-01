"""工具层：旧 ``plugin/tools.py``（1036 行单文件）拆成「机器 + 各域」。

## 拆法（对齐旧文件的物理分段，不是重新设计）

============================  ==========================================================
:mod:`.registry`              注册机器：``get_client`` / ``mk_handler`` /
                              ``register_generic_tools`` / ``noop_adapter_getter``
                              （旧文件 14–103 行，逐字搬）
:mod:`.msg`                   ``/Msg/*`` 域，660 行（**生成物**，见下）
:mod:`.mcp`                   MCP 只读域（联系人 / 群 / 消息 / 搜索）
:mod:`.jargon_tool`           ``wpp_jargon_query``（**生成物**）
:mod:`.vendor_defs`           厂商端点表：``tools_data.py`` 的 **112** 条（**生成物**）
:mod:`.vendor_defs_extra`     厂商端点表：``tools_data_extra.py`` 的 **107** 条（**生成物**）
:mod:`.phoneerp` / ``.wecom`` / ``.extras``
                              独有能力（TS 侧没有、被降级成纯 skill 的那批）
============================  ==========================================================

**生成物**（带生成头，禁止手改；改蓝本后重跑生成器）::

    python3 tools/port_tools_domain.py     # msg.py / jargon_tool.py（+ --check 门）
    python3 tools/gen_vendor_defs.py       # vendor_defs*.py（+ --check 门）

## 两条硬约定

1. **不做包级 eager import。** 本 ``__init__`` 故意只有文档 —— 各子模块由
   ``registration.py`` 在**调用期**按需导入，且每个子域独立 try/except。
   历史事故：旧实现把 import 写在模块顶层，``wecom_tools`` 在**导入期**解析外部
   CLI 抛 ``FileNotFoundError`` ⇒ 被外层 except 吞成一行 WARNING ⇒
   **wecom 7 + wpp_extras 3 共 10 个工具全部静默消失**（见 ``registration.py``）。
2. **模块级导入只能是标准库。** ``plugin_validate`` 会把 ``register()`` 在**隔离
   probe 子进程**里整体跑一遍，那个解释器**没有 aiohttp / pymysql**。
   凡是需要它们的，一律在**函数体内**导入（``core/api_client.py`` 顶层有
   ``import aiohttp``，所以连 ``WppApiError`` 都得在函数里取）。
"""
