"""webhook 路径派生（**唯一来源**）—— 与 TS ``deriveWebhookPaths`` 同形状。

## 为什么要有这个模块

同一条派生有**两个消费方**，必须同源，否则"我们监听哪条路径"与
"我们告诉 vendor 往哪条路径推"会各自漂 —— 漂了的表现是**入站静默全丢**（404），
而两边的日志都干净：

* ``platform/adapter.py::_start_webhook`` —— 注册路由（我们监听哪条）；
* ``core/api_client.py::setup_webhook`` —— 把 URL 推给 vendor（让它往哪条推）。

2026-10-02 审阅（C2 / A6）就是漂出来的：适配器只注册了**不带 token** 的
``webhookPath``，而 ``webhookPathToken`` 这个字段**从来没被消费过**
（``accounts.py:371`` 解析了它、``webhook.py`` 的 docstring 声称"路径 token 必须出现在
URL 路径里"，实际哪都没用）⇒ 回调地址就是可猜的 ``/wechatpadpro/<id>/webhook``，
配上"未配 secret 即放行"（旧 ``_authorized``）＝**任何能碰到该端口的人都能伪造微信入站消息**。

## 形状（照抄 TS ``src/index.ts`` 的 ``deriveWebhookPaths``）

给定 ``webhookPath = "/wechatpadpro/<id>/webhook"``、``webhookPathToken = "T"``：

    webhook  = "/wechatpadpro/T/<id>/webhook"
    business = "/wechatpadpro/T/<id>/webhook/business"

未配 token 时**原样返回**配置里的两条路径（business 为空则补 ``/business``）。
token 段插在 ``/wechatpadpro/`` **之后**是 vendor 侧的约定（TS 一直这么做），
不是随便加个前缀 —— 改位置等于让 vendor 那边的旧回调 URL 全部对不上。
"""

from __future__ import annotations

from typing import NamedTuple

#: vendor 路径的固定前缀 —— token 段插在它后面（TS 同款硬编码）。
_PREFIX = "/wechatpadpro/"

#: 路径全空时的兜底 business 段（对齐旧 ``derive_business_url`` 的行为，别改）。
_FALLBACK_BUSINESS = "/wechatpadpro/default/webhook/business"


class WebhookPaths(NamedTuple):
    """派生的两条路径。``NamedTuple`` 是为了同时兼容 ``p.webhook`` 与元组解包。"""

    webhook: str
    business: str


def derive_webhook_paths(webhook_path: str = "", webhook_path_token: str = "",
                         webhook_business_path: str = "") -> WebhookPaths:
    """派生 ``(webhook, business)`` 两条路径；token 为空时保持配置原样。

    ``webhook_business_path`` 只在**没配 token** 时被采信（配了 token 就按 token 路径
    补 ``/business``）—— 这条优先级是 TS 的行为，不是笔误。

    ⚠️ 与 TS 唯一的有意偏离：TS 在路径里**没有** ``/wechatpadpro/`` 前缀时，
    ``replace`` 是**空操作** ⇒ token 静默不生效（配了等于没配）；这里退化成把 token
    追加到路径尾部，**保证"配了就一定在路径里"** —— 安全默认优先于逐字对齐。
    """
    path = (webhook_path or "").strip()
    token = (webhook_path_token or "").strip()
    if token and path:
        path = path.replace(_PREFIX, f"{_PREFIX}{token}/", 1) if _PREFIX in path \
            else f"{path.rstrip('/')}/{token}"
    business = (webhook_business_path or "").strip()
    if token and path:
        business = f"{path}/business"
    elif not business:
        business = f"{path}/business" if path else _FALLBACK_BUSINESS
    return WebhookPaths(webhook=path, business=business)


def token_is_in_path(path: str, token: str) -> bool:
    """路径里是否**真的**带着 token 段（注册与鉴权两侧共用同一判据）。

    "带着"= token 是**完整的一段**，不是子串碰巧出现在 query 里。所以要求前后都有
    分隔斜杠，或者它正好是**最后一段** —— 后者是 :func:`derive_webhook_paths` 在
    "路径没有 ``/wechatpadpro/`` 前缀"时把 token 追加到尾部的结果，判据必须认它，
    否则注册侧派生出 ``/hook/T`` 而鉴权侧判它"没有 token" ⇒ **自相矛盾地把自己锁死**。
    """
    tok = (token or "").strip()
    if not tok:
        return False
    where = path or ""
    return where.endswith(f"/{tok}") or f"/{tok}/" in where
