"""platform —— 平台适配器与启用契约。

## ⚠️ 这个包名会遮蔽标准库的 ``platform`` 模块（必须知道）

Python 标准库有个 ``platform`` 模块（``platform.python_version()`` 之类）。
本包**故意**沿用 TS 蓝图的目录名 ``platform/``，代价是：

- **正常路径无害**：Hermes 以 ``plugin.platform`` 的形式导入本包（插件是包，
  不是裸模块），``import platform`` 在进程里仍然解析到**标准库**。
  （已实测：装好本包后 ``import platform`` 拿到的是 stdlib，完好。）
- **危险路径**：一旦有人把 ``plugin/`` 目录**直接塞进 ``sys.path``**
  （即 loader 的裸名映射 ``_BARE_MODULE_SCOPE``：``plugins_loader.py:38``），
  那么 ``import platform`` 会命中**本包**，而 ``platform/__init__.py`` 里
  没有 ``python_version`` 等属性 ⇒ **全进程**任何依赖 stdlib ``platform``
  的库都会炸。

**因此：本插件一律用包内相对导入**（``from .platform import adapter`` /
``from ..core import netguard``），**禁止**依赖裸名导入。这条不是风格偏好，
是上面那个遮蔽风险的对策。

## 子模块

================================  ============================================
:mod:`.adapter`                    ``WppAdapter(BasePlatformAdapter)`` —— 主实现
:mod:`.enablement`                 ``is_connected`` / ``env_enablement_fn``（平台启用的单一真源）
:mod:`.inbound_ws`                 厂商 WS 实时通道（连接内重连）
:mod:`.inbound_webhook`            厂商 webhook 接收器（含鉴权）
:mod:`.outbound`                   出站纯函数（分片 / 引用 XML / 响应判定）
================================  ============================================
"""
