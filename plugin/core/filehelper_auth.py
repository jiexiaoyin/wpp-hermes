"""filehelper 管理命令的准入判据（纯函数，**零第三方依赖**，本机可 import）。

## 为什么单独一个模块

``platform/adapter.py`` 模块级 import Hermes（本机装不了 ⇒ 那边只能靠 AST 形状验），
把**判据**搬到 ``core/`` 才能用真实用例把两条边界钉死。判据与处置分开，也与
``core/access_policy.py`` / ``platform/enablement.py`` 的做法一致。

## 2026-10-02 修的是什么（C3）

旧实现在 ``_maybe_command`` 里写的是：

    if not admin:
        log.warning("……未配 adminUsers ⇒ 放行（管理面裸露）")
    elif sender and sender not in admin:
        return True   # 非 admin ⇒ 拒

两处都往**放行**的方向漏：

1. ``adminUsers`` 未配 ⇒ 直接执行。管理面命令（``/genpair`` 发配对码、改群白名单、
   开关心流）对任何能往 filehelper 会话里写字的人敞开。真机上 ``live-test.json``
   就是 ``"adminUsers": []``。
2. ``elif sender and …`` 里那个 **``sender`` 判空短路** —— 空发件人进不了拒绝分支，
   落到下面**照常执行**。而 vendor 在 filehelper 会话里恰恰常把 sender 标成
   **机器人自己**甚至留空（见 ``inbound.triggers.is_filehelper_session`` 的说明、
   ``adapter._handle_one`` ① 给 filehelper 的豁免注释）。两条合起来 =
   **"空发件人即管理员"**。

现在两条一律 **fail-closed**：拿不到"这就是管理员"的证据就不执行。
要在某账号上启用管理命令，就在账号配置里写 ``"adminUsers": ["<你的 wxid>"]`` ——
历史配置还会把**机器人自己的 wxid** 一起写进这个列表（``accounts/default.json``
就是 ``["q139198824", "wxid_eezdbu1ytws422"]``），因为 vendor 常那样标 sender。
"""

from __future__ import annotations

from collections.abc import Iterable

#: 允许执行 —— 发件人确实在 ``adminUsers`` 里。
ALLOW = "allow"
#: 账号没配 ``adminUsers`` ⇒ 谁都别执行（**不是**"放行"）。
NO_ADMIN_CONFIGURED = "no-admin"
#: 配了，但这条不是管理员发的（**含空发件人**）。
NOT_ADMIN = "not-admin"


def verdict(admin_users: Iterable[object] | None, sender: object) -> str:
    """:data:`ALLOW` / :data:`NO_ADMIN_CONFIGURED` / :data:`NOT_ADMIN` 三者之一。

    比较前 ``strip``（vendor 偶尔带空白）；空串**永远**不是管理员 —— 这一条就是修 C3
    的那一行：旧判据 ``sender and sender not in admin`` 对空串短路放行。
    """
    admins = {str(a).strip() for a in (admin_users or ()) if str(a).strip()}
    if not admins:
        return NO_ADMIN_CONFIGURED
    who = str(sender or "").strip()
    return ALLOW if who in admins else NOT_ADMIN
