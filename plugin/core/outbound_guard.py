"""出站闸的**唯一收口** —— 299 个工具、适配器、standalone 全走这里。

## C1/C5：修的是什么

修前 ``WECHATPRO_SEND_ALLOWLIST``（调试期的"只许发给这个群"保险）**只挡得住三条路**：
``adapter.send_message`` / ``send_media`` / ``_standalone_send`` 各自在发之前调一次
``outbound.assert_outbound_allowed``。而**工具面完全在闸外**：

* ``plugin/toolkit/msg.py`` 的 8 个发送工具 → ``client.send_text/send_image/...``；
* ``plugin/toolkit/registry.py`` 的通用 handler（219 条 vendor 端点表）→ ``client.call(endpoint, body)``。

两条都直接落到 :class:`plugin.core.api_client.WppClient`，**不经过任何判定**。后果不是
"少一层保险"而是"保险是假的"：调试窗口里把白名单设成测试群，模型照样能调
``wpp_msg_send_txt`` 往生产群发消息（``/Finder/FinderSendText``、``/Msg/UploadImg``、
朋友圈上传、``/TenPay/OpenHongBao`` 全在同一张表里），而日志上一切正常。

## 为什么收口在 ``WppClient._raw_url``

``_raw_url`` 是**所有**厂商请求拼 URL 的唯一出处（``_request`` / ``_post_bytes`` /
``_post_json_b64`` 三个 helper 都调它，见 ``api_client`` 的模块 docstring）。把判据挂在这里，
"新增一条发送路径忘了过闸"在结构上不可能发生 —— 不管谁写的新工具，只要发请求就经过它。

## 判据

1. **没设白名单 ⇒ 直接返回**（
   ``netguard.send_allowlist()`` 空 ⇒ no-op）。与 TS 同行为：生产期不限制，这个闸只为
   调试期防误发而存在。
2. **只读端点 ⇒ 返回**。不放行的话调试窗口连 ``/Login/HeartBeat``、``/User/GetOnlineInfo``、
   ``/Msg/Sync`` 都会被拒 —— 那样的闸没人敢开，等于没有。
3. 其余（**写/发送**）端点：从请求体里把**所有**目标取出来，逐个过
   ``netguard.check_outbound_allowed``（白名单外 raise ``GuardError``）。
4. 取不到任何目标 ⇒ **拒绝**（fail-closed）。形如 ``/Friend/SendRequest``、朋友圈上传、
   ``/Webhook/Set``、``/TenPay/OpenHongBao`` 这类"目标不在参数里"的端点，调试期一律不放行
   —— 说不清发给谁的操作，在白名单模式下没有放行的理由。

## 只读/写怎么分（分类表是**数据**，不是猜的）

``READ_WORDS`` / ``WRITE_WORDS`` 作用在端点路径的 CamelCase 词上：**命中写词就是写**
（写优先），否则命中读词才是只读，两个都不命中 ⇒ 写（fail-closed）。

对 218 条端点表的实测结果：**92 只读 / 126 受闸**。分类里有三个必须记住的例子：

* ``/Finder/FinderSendText`` —— 词面以 ``Find`` 开头（像读），但含 ``Send`` ⇒ **写**（受闸）；
  只看"首词是不是读词"的写法会在这里漏一个发送口；
* ``/Msg/StartAutoSync`` —— 含 ``Sync``（像读），但含 ``Start`` ⇒ **写**。它把同步目标指向
  任意 ``TargetURL``，放行等于允许把消息流引到外部地址；
* ``/Login/HeartBeat``、``/Tools/CdnDownloadImage`` —— ``Heart``/``Download`` 命中读词 ⇒ **只读**，
  否则调试期心跳与媒体下载会自己把自己拒掉。

⚠️ 分类**偏保守**：判成"写"而实际是读的端点（例如 ``/Search/Channels/Comments``）在调试期
会被拒。这是有意的方向 —— 误拒只是调试窗口里的一句报错，误放则是一条发出去撤不回的
微信消息（红线 3）。
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from . import netguard

#: 只读词（读端点里的动作词）。**必须比写词先看**：见 :func:`is_read_only`。
READ_WORDS = frozenset({
    "Get", "List", "Search", "Query", "Find", "Check", "Download", "Sync", "Detail",
    # ★ "Test" 已移除（2026-10-18）：/Webhook/Test 是"把任意 TestData POST 到已配置的
    #   回调"——语义是**网络写**，被当读词就绕过了白名单闸。去掉后它落到"两个词都不命中
    #   ⇒ 写（fail-closed）"，正是我们要的方向。
    "Info", "Read", "Status", "Qrcode", "QRCode", "All", "Scan", "Fill", "Pull",
    "History", "Profile", "Heart", "Beat", "Result", "Article", "Markdown", "Preview",
    "Weather", "Translation", "Suggestions", "Users", "Text", "Qrydetail", "Qry",
    "Long", "Short", "Mini",
})

#: 写词（会改状态或**把内容发出去**的动作词）。命中即判写，压过读词。
WRITE_WORDS = frozenset({
    "Send", "Upload", "Share", "Post", "Publish", "Forward", "Revoke", "Set", "Add",
    "Delete", "Del", "Update", "Modify", "Create", "Quit", "Invite", "Operate", "Move",
    "Pass", "Verify", "Blacklist", "Logout", "Bind", "Follow", "Like", "Comment",
    "Comments", "Transfer", "Pay", "Hong", "Bao", "RedPacket", "Enable", "Disable",
    "Join", "Accept", "Reject", "Push", "Recall", "Withdraw", "Remove", "Star", "Emoji",
    "Pat", "Mass", "Mark", "Hello", "Start", "Into",
})

#: 请求体里表示"发给谁/对谁操作"的键。**全部**命中值都要过闸（不是只取第一个）。
#:
#: 故意不收的键：``blackList``（是"不给谁看"，不是收件人）、``SendId``/``SendUserName``
#: （红包**发起人**，不是收件人）、``Id``/``id``（Finder/收藏对象 id，太泛）。
TARGET_KEYS = (
    "ToWxid", "toWxid", "to_wxid", "towxid",
    "ToWxids", "toWxids", "to_wxids",
    "ToIds", "toIds",
    "Wxid", "wxid", "Wxids", "wxids",
    "ToUserName", "toUserName", "to_user_name",
    "UserName", "userName", "Username", "username",
    "ChatRoomName", "chatRoomName", "chat_room_name",
    "RoomId", "roomId", "room_id",
    "QID", "qid",
    "Target",
    # ★ 2026-10-19：以下四个键是厂商真实接收方/操作对象，原先漏在闸外：
    #   /Group/SendTransferGroupOwner 的 NewOwnerUserName（新群主）、
    #   /Finder/FinderSendText 的 FinderUsername（私信对象）、
    #   withUserList / groupUserList（批量操作成员）。
    "NewOwnerUserName", "newOwnerUserName",
    "FinderUsername", "finderUsername",
    "withUserList", "withUserlist", "with_user_list",
    "groupUserList", "groupUserlist", "group_user_list",
)

#: 目标键的**归一化**集合（``lower`` + 去下划线）。
#:
#: ★ 为什么必须是归一化而不是拼写名单（2026-10-18 审阅实测）：厂商是 Go + swagger，
#:   ``encoding/json`` 对结构体字段**大小写不敏感**、且后出现的键覆盖前者。于是
#:   ``{"ToWxid": "ok@chatroom", "ToWXID": "PROD@chatroom"}`` 实际只会发给 PROD，而拼写
#:   名单只认得出 ``ToWxid`` ⇒ 闸"核对通过"，真消息却进了生产群。归一化后两个键都进
#:   targets；再叠加下面的"同键多值 ⇒ 拒绝"，歧义请求直接不放行。
_NORMALIZED_TARGET_KEYS = frozenset(k.lower().replace("_", "") for k in TARGET_KEYS)


def _norm_key(key: Any) -> str:
    return str(key).lower().replace("_", "")


#: 递归取目标的深度上限（``/Msg/SendAppMessage`` 这类是 ``{"Items": [{"ToWxid": …}]}``）。
_MAX_DEPTH = 3

#: 单次请求最多核对多少个目标（防病态载荷把闸变成 CPU 放大器）。
_MAX_TARGETS = 64

#: **无目标上传步骤**的收件人上下文（2026-10-19 修）。
#: ``/Tools/UploadFile`` 这类端点体里只有 base64、天生没有收件人，真发送在紧随其后的
#: ``/Msg/ShareLink`` 上。fail-closed 让它永远被拒 ⇒ 白名单开着时**所有发文件都挂**
#: （07:29 实测）。⇒ 组合调用用 :func:`guard_targets` 把**已校验过的**收件人带进
#: 上传步骤；上下文里的目标同样逐个过白名单，不核对不上就照样拒。
_upload_targets_var: ContextVar[tuple[str, ...]] = ContextVar(
    "wpp_upload_guard_targets", default=())


@contextlib.contextmanager
def guard_targets(*targets: str, allowed: tuple[str, ...] | None = None,
                  account_id: str = "") -> Iterator[None]:
    """把本协程内「无目标端点」的核对收件人临时设为 ``targets``（须全在白名单内）。

    ``allowed``/``account_id`` 与 :func:`guard` 同义：组合调用（UploadFile→ShareLink）
    必须和后续真发送用**同一份**白名单，否则上传步骤会按 ambient env 再判一次。
    """
    cleaned = tuple(t for t in (str(x or "").strip() for x in targets) if t)
    for t in cleaned:
        netguard.check_outbound_allowed(t, account_id, allowed=allowed)
    token = _upload_targets_var.set(cleaned)
    try:
        yield
    finally:
        _upload_targets_var.reset(token)

_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_SEG_RE = re.compile(r"[^0-9A-Za-z]+")


def camel_words(endpoint: str) -> tuple[str, ...]:
    """把端点路径切成 CamelCase 词：``/Finder/FinderSendText`` → (Finder, Send, Text)。"""
    out: list[str] = []
    for seg in _SEG_RE.split(endpoint or ""):
        out.extend(_WORD_RE.findall(seg))
    return tuple(out)


def is_read_only(endpoint: str) -> bool:
    """端点是否**只读**（不把内容发给谁、也不改状态）。

    写词优先：命中写词直接判写 —— 这样 ``FinderSendText``（含 ``Send``）、
    ``StartAutoSync``（含 ``Start``）不会被它们词面里的读词救回来。
    """
    words = set(camel_words(endpoint))
    if words & WRITE_WORDS:
        return False
    return bool(words & READ_WORDS)


def _add_value(value: Any, out: list[str], state: dict[str, bool]) -> None:
    """**已确认是目标**的值：标量收下，列表逐项收，字典继续找。

    ``state["overflow"]`` 在超过 :data:`_MAX_TARGETS` 后置位 —— 绝不能静默截断：
    截断等于「前 64 个在白名单里、第 65 个随便发」，正是闸要防的事。
    """
    if len(out) >= _MAX_TARGETS:
        state["overflow"] = True
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _add_value(item, out, state)
        return
    if isinstance(value, dict):
        _gather(value, out, 0, state)
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (str, int, float)):
        got = str(value).strip()
        if got and got not in out:
            out.append(got)


def _gather(value: Any, out: list[str], depth: int, state: dict[str, bool]) -> None:
    """在**非目标键**下继续找容器：只为了找更深的 dict，**不收裸标量**。

    "不收裸标量"这条是必需的：``{"blackList": ["bystander"]}`` 走的是非目标键，
    若把列表里的字符串也当目标，就会把"不给谁看"的人当成收件人（判反）。
    """
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, dict):
        for key, val in value.items():
            if _norm_key(key) in _NORMALIZED_TARGET_KEYS:
                _add_value(val, out, state)
            elif isinstance(val, (dict, list, tuple)):
                _gather(val, out, depth + 1, state)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (dict, list, tuple)):
                _gather(item, out, depth + 1, state)


def _collect_targets(body: Any) -> tuple[tuple[str, ...], bool]:
    """返回 ``(targets, overflow)``；``overflow`` 表示目标数超过硬上限。"""
    out: list[str] = []
    state = {"overflow": False}
    _gather(body, out, 0, state)
    return tuple(out), state["overflow"]


def targets_in(body: Any) -> tuple[str, ...]:
    """请求体里所有可核对的目标（单值/多值/嵌套列表都算）。"""
    return _collect_targets(body)[0]


def duplicated_target_keys(body: Any) -> tuple[str, ...]:
    """**同一个 dict 内**归一化后出现多次的目标键（大小写/下划线变体视为同一个键）。

    厂商（Go）对字段名大小写不敏感，``{"ToWxid": a, "ToWXID": b}`` 只有一个是有效的；
    闸看不出哪个生效 ⇒ 按 fail-closed 拒绝。返回归一化键名，便于日志定位。

    ⚠️ 判据按**单个 dict** 收敛（2026-10-18 修）：跨对象不算重复。列表里两个
    独立元素各用自己的拼写（``Items[0].ToWxid`` / ``Items[1].toWxid``）是合法载荷，
    而且每个对象内部只有一个有效键、两个值都过了白名单 ⇒ 不存在"闸核对了 A、
    实际发给 B"的歧义；按整棵子树计数会把这类正常请求误拒。
    """
    found: set[str] = set()

    def walk(value: Any, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        if isinstance(value, dict):
            here: dict[str, list[str]] = {}
            for key, val in value.items():
                nk = _norm_key(key)
                if nk in _NORMALIZED_TARGET_KEYS:
                    here.setdefault(nk, []).append(str(key))
                    continue
                if isinstance(val, (dict, list, tuple)):
                    walk(val, depth + 1)
            for nk, keys in here.items():
                if len(keys) > 1:
                    found.add(nk)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, (dict, list, tuple)):
                    walk(item, depth + 1)

    walk(body, 0)
    return tuple(sorted(found))


def guard(endpoint: str, body: Any = None, account_id: str = "",
          *, allowed: tuple[str, ...] | None = None,
          path_targets: tuple[str, ...] = ()) -> None:
    """出站闸判据（**唯一实现**，``WppClient._raw_url`` 调用）。

    未设白名单 ⇒ no-op（生产行为与 TS 一致）；只读端点 ⇒ 放行；其余端点把体里所有目标
    逐个过白名单，取不到目标则 fail-closed。

    ``allowed`` 是**连接期捕获**的白名单（见 ``WppClient``），避免发送任务掉出
    profile scope 后重新读到根 ``.env``；``path_targets`` 是已经被
    ``_bind_endpoint`` 移进 URL 路径的收件人 —— 它们同样必须过闸，否则
    「路径里是任意目标、请求体里放一个白名单诱饵」就能绕过。
    """
    allowed = netguard.send_allowlist() if allowed is None else tuple(allowed)
    if not allowed:
        return
    if is_read_only(endpoint):
        return
    dup = duplicated_target_keys(body)
    if dup:
        raise netguard.GuardError(
            f"出站闸：{endpoint} 的请求体里出现了**指向同一字段的多个键** "
            f"({', '.join(sorted(dup))}) —— 厂商侧只有一个会生效，闸无法确认实际发给谁 ⇒ "
            f"白名单期间拒绝执行。"
        )
    body_targets, overflow = _collect_targets(body)
    if overflow:
        raise netguard.GuardError(
            f"出站闸：{endpoint} 的请求里目标数超过硬上限 {_MAX_TARGETS} —— "
            f"闸不会截断后放行（截断=第 {_MAX_TARGETS + 1} 个目标不受核对）。"
        )
    targets = tuple(dict.fromkeys((*path_targets, *body_targets)))
    if not targets:
        # 体里没有收件人 ≠ 一定拒：组合调用（UploadFile→ShareLink）用 guard_targets()
        # 带进来的**已校验收件人**也算核对（仍逐个过白名单，见模块 docstring）。
        ctx_targets = _upload_targets_var.get()
        if ctx_targets:
            for target in ctx_targets:
                netguard.check_outbound_allowed(target, account_id, allowed=allowed)
            return
        raise netguard.GuardError(
            f"出站闸：{endpoint} 不是只读端点，请求体里又没有任何可核对的目标 ⇒ "
            f"白名单（{netguard.SEND_ALLOWLIST_ENV}）期间**拒绝执行**。"
            f"（说不清对谁操作的一律不放行 —— 微信是生产环境，发出去撤不回）"
        )
    for target in targets:
        netguard.check_outbound_allowed(target, account_id, allowed=allowed)
