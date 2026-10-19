"""通道无关层（扩展边界 P0，2026-10-19）。

本包只放**不依赖 WPP 运行时**的逻辑，规则：

1. 禁止 import ``plugin.core`` / ``plugin.data`` / ``plugin.platform`` /
   ``plugin.toolkit`` / ``plugin.inbound`` 的任何模块；
2. 消息、会话、用户一律用 ``Mapping`` 或本包内的小协议表示，不 import
   ``WppMessage``，不假定 ``wxid`` / ``chatroom`` 字段；
3. 允许依赖标准库、第三方纯算法库，以及 Hermes 核心
   （``gateway`` / ``agent`` / ``tools`` / ``hermes_cli``）；
4. 发送、落库、读账号配置等执行动作由调用方注入，不在本包内完成。

迁移期间旧路径（``plugin/inbound/<name>.py``）保留 re-export 兼容层，
新代码请直接 import 本包。目标是等第二个通道接入时，整包平移到 Hermes 核心
或独立插件而不改一行逻辑。

P1（2026-10-19）起，会话身份与出站统一走 :mod:`.contracts` 里的
``ConversationKey`` / ``MessageRow`` / ``ConversationStore`` / ``OutboundSender`` /
``ChatCapabilities``；通道实现放在通道自己的包里（WPP 是
``plugin/platform/conversation.py``）。策略层只认协议，不认平台名。
"""
