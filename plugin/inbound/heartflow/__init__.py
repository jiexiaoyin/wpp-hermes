"""心流（Heartflow）：nine 模块的包边界。

============================  ==========================================================
:mod:`.runtime`               配置单例 + 内存缓存登记表 + 300s sweep 循环
:mod:`.guards`                不变量守卫（I2 只收紧 / I8 读取侧钳制）——**Py 侧新增**
:mod:`.budget`                预算五闸（间隔 / 每小时 / 每天 / 连续无人类 / 静默段）
:mod:`.dedupe`                消息去重窗 + 摘要（**同一函数**，见 I4）
:mod:`.label`                 群标签（锚 bot 自己那条 + 反事实 lift）
:mod:`.layer`                 层统计（**只许全量重算**，见 I3）
:mod:`.observe`               影子观察（占比）
:mod:`.profile`               群画像
:mod:`.learn`                 反馈闭环 + 自适应调阈 + sweep 主体
:mod:`.heartflow`             闸门与打分（``maybe_engage`` 入口）
============================  ==========================================================

## 为什么这个 ``__init__`` **一个 import 都不写**

心流整条链在 ``platform/adapter.py`` → ``_try_heartflow`` / ``_start_sweeps`` 上，
而那条链**在 ``register()`` 的执行路径里**（``registration._register_platform`` →
``from .platform import adapter``）。

一旦这里做 re-export 门面，**九个子模块的加载期代价就全挪到注册期**：
任意一个子模块在加载时抛错（缺依赖、解析外部文件、写坏的表常量），
都会被 ``registration._step`` 的兜底 try/except 吞成一行 WARNING ——
表现是"**平台没注册上**"或"**工具数对不上**"，而不是"心流挂了"。
这正是 F5 的形态（见 ``tools/verify_tree.py`` 的 R3），**只是换个位置复发**。

所以：``plugin/tools/`` 与 ``plugin/llm/`` 之后，本包是**第三处**零 import 的
``__init__``，并已在 R3 的 :data:`NO_EAGER_INIT_PACKAGES` 里上锁。

调用方一律**显式导入子模块**（``adapter.py`` 已经是这么写的）::

    from ..inbound.heartflow import heartflow as hf      # 而不是 from ..inbound import heartflow
    from ..inbound.heartflow import runtime as hf_runtime

这样"加载哪个子模块"就在调用点看得见，出问题也定位得到。
"""
