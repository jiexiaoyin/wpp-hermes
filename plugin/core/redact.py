"""日志脱敏 —— TS v1.16.1 ``src/core/redact.ts`` 的 Python 对等实现（**唯一一份**）。

## 为什么需要它（TS CHANGELOG v1.16.1「Security」段，生产事故驱动）

``webhookPathToken`` 是 webhook 路径上**唯一的**鉴权（vendor 不发签名，HMAC 按既定
策略关着）：拿到它就等于拿到「往插件里灌任意消息」的入口。它嵌在
``/wechatpadpro/<token>/<id>/webhook`` 里，而注册/鉴权失败/调度失败这几条日志
天天把**完整路径**打出来 ⇒ 明文 token 长期落在 journald 与日志文件里。

authcode 早就走 ``mask_secret`` 了，token 没有 —— 这个模块补的就是这个洞。

## 按**值**替换，不按路径结构替换

结构式（把 ``/wechatpadpro/<段>/`` 的第二段当 token 打码）在没有配 token 的老配置上
会把字面量 ``webhook`` 当成 token，日志变成 ``/wechatpadpro/webh...ok/`` ——
恰好在排障时把人往沟里带。按值替换时 token 为空 ⇒ 原样返回，语义精确。

## 多账号必须全脱

v1.3.61 起**一个** webhook server 服务多账号（一 server 多 path）。只脱「本条账号」
的 token 时，A 账号的请求日志里会出现 B 账号的 token（TS 侧注入式验证过）。
所以接收器用 :func:`redact_webhook_path_any` 遍历**自己路由表里的全部** token。
"""

from __future__ import annotations

import re
from collections.abc import Iterable


def mask_secret(secret: str) -> str:
    """与 authcode 同一套 mask：留头 4 尾 2，够人工核对"是不是同一条"，又不给可用信息。"""
    if not secret:
        return "(empty)"
    if len(secret) <= 4:
        return "****"
    return f"{secret[:4]}...{secret[-2:]}"


def redact_webhook_path(value: str, token: str | None) -> str:
    """把字符串（URL / 路径）里出现的 webhook token 换成 :func:`mask_secret` 形式。

    纯函数；``token`` 为空时**恒等**返回（无 token 的路径没有秘密可脱）。
    """
    if not token:
        return value
    return value.replace(token, mask_secret(token))


def redact_webhook_path_any(value: str, tokens: Iterable[str | None]) -> str:
    """多 token 版：依次替换；空值项自动跳过（调用方不必过滤）。"""
    out = value
    for token in tokens:
        out = redact_webhook_path(out, token)
    return out


#: webhook token 在派生路径里的固定位置：``/wechatpadpro/<token>/...``。
#: 审计层拿不到明文 token（它只存在于账号配置里），所以按**结构**打码。
_WEBHOOK_TOKEN_SEG = re.compile(r"(?i)(/wechatpadpro/)([^/?#\s]+)")


def redact_webhook_url(value: str) -> str:
    """按路径结构给 URL 里的 webhook token 打码（审计/日志兜底用）。

    ⚠️ 只在**确认**字符串是"我们自己派生的 webhook 地址"时用（``/Webhook/*``、
    ``/Msg/StartAutoSync`` 的请求体）。普通业务字符串不走这里 —— 结构式替换在
    别处可能误伤正常路径。
    """
    if not value:
        return value
    return _WEBHOOK_TOKEN_SEG.sub(
        lambda m: m.group(1) + mask_secret(m.group(2)), value)


#: **键名命中即打码**的凭据键（小写、去下划线后比对）。
#:
#: ★ 2026-10-18 审阅实测：``redact_deep`` 只处理 webhook **路径**，而
#:   ``/Webhook/Set`` / ``/Webhook/Test`` 的请求体里带着明文的 ``authcode``
#:   （``api_client.set_webhook`` 与 ``toolkit/registry.py`` 都会 ``setdefault`` 一份）。
#:   审计表 ``wpp_api_calls`` 是**长期留存**且会进备份 ⇒ 等于把账号唯一凭据复制进 DB，
#:   拿到即可接管该微信账号。所以审计落库前按**键名**兜底打码，与端点无关。
_SECRET_KEYS = frozenset({
    "authcode", "accesstoken", "tokenkey", "secret", "password", "passwd",
    "webhooksecret", "pathToken".lower(), "apikey", "accesskeysecret",
})


def _is_secret_key(key: object) -> bool:
    return str(key).lower().replace("_", "").replace("-", "") in _SECRET_KEYS


def redact_secret_keys(value: object) -> object:
    """只按**键名**打码（``authcode`` / ``tokenKey`` / ``secret`` …），不动其它字符串。

    审计层对**所有**端点都该跑这一支：凭据可能出现在任何请求体里（``/Webhook/Set``
    的 ``authcode`` 是显式一份，别处也可能被模型塞进来），而 URL 结构式打码只对
    webhook 派生地址有意义（见 :func:`redact_webhook_url`），全局跑会误伤。
    """
    if isinstance(value, list):
        return [redact_secret_keys(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_secret_keys(v) for v in value)
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if _is_secret_key(k) and isinstance(v, str) and v:
                out[k] = mask_secret(v)
            else:
                out[k] = redact_secret_keys(v)
        return out
    return value


def redact_deep(value: object) -> object:
    """递归打码：字符串过 :func:`redact_webhook_url`，**凭据键的值**整体打码。"""
    if isinstance(value, str):
        return redact_webhook_url(value)
    if isinstance(value, list):
        return [redact_deep(v) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_deep(v) for v in value)
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if _is_secret_key(k) and isinstance(v, str) and v:
                out[k] = mask_secret(v)
            else:
                out[k] = redact_deep(v)
        return out
    return value
