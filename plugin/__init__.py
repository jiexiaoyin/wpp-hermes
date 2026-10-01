"""wechatpadpro-platform —— Hermes 平台插件（v2.0.0 全量重写）。

暴露 Hermes 唯一要求的入口 ``register(ctx)``。实现见 ``registration.py``。
"""

from .registration import register

__all__ = ["register"]
