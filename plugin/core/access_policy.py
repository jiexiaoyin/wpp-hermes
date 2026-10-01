"""把插件自己的准入策略**上报给 Hermes 网关**（纯函数，只用标准库）。

## 为什么需要这一层

Hermes 网关在插件**之上**还有一道用户授权
（``gateway/authz_mixin.py::_principal_authorized``）。它**没有环境白名单时默认全拒** ——
于是出现"插件判过了、网关把它拒了"：现场只留一行
``WARNING gateway.run: Unauthorized user: <wxid>``，而插件侧日志一切正常
（连 ``触发（group_mention）`` 都打出来了）。2026-10-01 真机实测踩的就是这个：
心流 judge 打了 0.775、台账落了 ``status='judged'``，回复却被网关拦下。

插件手里**本来就有**这套策略（``core.accounts.Account`` 的 ``groupPolicy`` /
``groupAllowFrom`` / ``allowFrom``，见 ``inbound/triggers.py`` ⑥⑦ 的实际判据），
只是从没往上报。Hermes 给适配器留了官方接入点：适配器声明
``enforces_own_access_policy`` 之后，网关**反过来问适配器**
（``authz_mixin.py::_adapter_setting`` 优先 ``getattr(adapter, attr)``，再退到
``config.platforms[<p>].extra[<key>]``）。

## 上报的形状（网关认三个值：``allowlist`` / ``disabled`` / ``open``）

| 上报点 | 网关怎么用 |
|---|---|
| ``_group_policy`` | ``== "allowlist"`` ⇒ 放行该群的发送者（``authz_mixin.py:470``） |
| ``_dm_policy`` | ``== "allowlist"`` ⇒ 再问 ``_is_dm_allowed(sender)``；``open`` ⇒ **default-deny** |
| ``_is_dm_allowed(sender)`` | 私聊按发件人逐个判 |

## ⚠️ 两条必须记住的口径

**1. 网关只认 ``allowlist``，``open`` 一律回到 default-deny。**
这是 SECURITY.md §2.6 的反 fail-open 设计（"open 默认转发每一条"是网络暴露面）。
所以插件侧配 ``groupPolicy: open`` / ``allowAllUsers: true`` 的操作者，
**还必须**显式 ``WECHATPRO_ALLOW_ALL_USERS=1``。
本模块**不**把 ``open`` 谎报成 ``allowlist`` 去绕开那道闸 —— 那是把安全边界偷偷拆掉。

**2. 只上报「已连接」的账号。**
适配器的 ``self._accounts`` 含**未连接**的账号（例如被红线 2 拒掉的生产形状
``default.json``：7 个真实生产群在它的 ``groupAllowFrom`` 里）。
把它们报给网关 = 替一个**根本不存在的连接**开门。过滤由调用方按
``_clients`` 做，:func:`reportable` 只负责那一步的纯逻辑。

（本模块独立成文件的**另一个**理由：``plugin.platform.adapter`` 在开发机上
import 不了 —— 模块级 ``from gateway.config import Platform`` 是 Hermes 独有。
逻辑放这儿才能单测，见 ``tests/platform/test_access_policy.py``。）
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

#: 网关只认这三个（见模块 docstring 的口径 1）
ALLOWLIST = "allowlist"
DISABLED = "disabled"
OPEN = "open"

#: 插件侧 ``groupPolicy`` → 网关口径。
#: ``closed`` 是插件自己的值（``inbound/triggers.py`` ⑥ 与 ``disabled`` 同样拒绝），
#: 网关不认识，所以并入 ``disabled``。
_GROUP_POLICY_MAP: Mapping[str, str] = {
    "allowlist": ALLOWLIST,
    "disabled": DISABLED,
    "closed": DISABLED,
}


def reportable(accounts: Iterable[Any], connected_ids: Iterable[str]) -> tuple[Any, ...]:
    """只留**已连接**的账号，按 ``id`` 排序（确定性 —— 单测与日志都靠它）。

    ``connected_ids`` 传适配器的 ``self._clients.keys()``。
    """
    live = {str(i) for i in connected_ids}
    picked = [a for a in accounts if str(getattr(a, "id", "")) in live]
    return tuple(sorted(picked, key=lambda a: str(getattr(a, "id", ""))))


# ------------------------------------------------------------------ 群


def _one_group_policy(acct: Any) -> str:
    """单账号的 ``groupPolicy`` → 网关口径。**未知/缺省一律 ``open``**（不猜成 allowlist）。"""
    raw = str(getattr(acct, "group_policy", "") or "").strip().lower()
    return _GROUP_POLICY_MAP.get(raw, OPEN)


def group_policy(accounts: Iterable[Any]) -> str:
    """聚合多个已连接账号的群策略，给网关的 ``_group_policy``。

    口径 = **真 allowlist 优先**：

    * 只要有**任一**账号是 ``allowlist`` ⇒ ``allowlist``
    * 否则全是 ``disabled``/``closed`` ⇒ ``disabled``
    * 否则 ⇒ ``open``（网关会 default-deny，需操作者显式开 ALLOW_ALL_USERS）

    为什么"任一 allowlist"就够：网关对群的判据**不查群 ID**
    （``authz_mixin.py:470`` 在 ``_adapter_policy == "allowlist"`` 时直接 ``return True``），
    群 ID 的过滤仍然由插件自己的闸门（``inbound/triggers.py`` ⑥ 的 ``groupAllowFrom``）
    **逐账号**把守。所以这里报宽一点不会放宽实际准入面 —— 网关说"进"、
    插件说"这个群不在你的名单里"，消息照样被丢。

    **空集合 ⇒ ``disabled``**（fail-closed）：还没连上任何账号时不该开任何门。
    """
    got = {_one_group_policy(a) for a in accounts}
    if not got:
        return DISABLED
    if ALLOWLIST in got:
        return ALLOWLIST
    if got == {DISABLED}:
        return DISABLED
    return OPEN


# ------------------------------------------------------------------ 私聊


def _one_dm_policy(acct: Any) -> str:
    """单账号的私聊策略。

    插件侧**没有** ``dmPolicy`` 字段 —— 私聊语义写在
    ``inbound/triggers.py::_decide_dm`` 里，这里必须与它**逐条对齐**：

    * ``allowAllUsers is True`` ⇒ ``open``（注意是 ``is True``，字符串 ``"true"`` 不算 —— 原实现如此）
    * 否则 ``allowFrom`` 非空 ⇒ ``allowlist``
    * 否则 ⇒ ``disabled``（原实现的 fail-closed 兜底）
    """
    raw = getattr(acct, "raw", None)
    if isinstance(raw, Mapping) and raw.get("allowAllUsers") is True:
        return OPEN
    # 判据必须与 :func:`is_dm_allowed` **同一套**（都过 trim、都丢空项）：
    # 否则 ``allowFrom: ["  "]`` 会报成 ``allowlist``，网关挨个问下去全是 False ——
    # 结果一样，但报出去的话是假的（"有白名单"其实一个都没有）。
    return ALLOWLIST if allowed_dm_senders((acct,)) else DISABLED


def dm_policy(accounts: Iterable[Any]) -> str:
    """聚合私聊策略，给网关的 ``_dm_policy``。``open`` 优先上报（网关据此 default-deny）。"""
    got = {_one_dm_policy(a) for a in accounts}
    if not got:
        return DISABLED
    if OPEN in got:
        return OPEN
    if ALLOWLIST in got:
        return ALLOWLIST
    return DISABLED


def allowed_dm_senders(accounts: Iterable[Any]) -> frozenset[str]:
    """已连接账号 ``allowFrom`` 的**并集**。

    ⚠️ 多账号时这是并集而不是"按收信账号各自判"：Hermes 的
    ``_is_dm_allowed(sender_id)`` **只给发件人 id、不给账号**，适配器没有钩子能分辨
    这条私聊落在哪个账号上。并集仍然是**操作者显式列出的** wxid，不是猜的。
    """
    out: set[str] = set()
    for a in accounts:
        for item in getattr(a, "allow_from", ()) or ():
            s = str(item).strip()
            if s:
                out.add(s)
    return frozenset(out)


def is_dm_allowed(accounts: Iterable[Any], sender_id: str) -> bool:
    """私聊发件人是否放行。**fail-closed**：名单空 ⇒ 一律 ``False``（与改造前行为一致）。"""
    sid = str(sender_id or "").strip()
    if not sid:
        return False
    return sid in allowed_dm_senders(accounts)
