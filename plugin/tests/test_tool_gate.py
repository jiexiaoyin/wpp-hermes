"""运行期门禁（W7-5）—— 工具**被调用时**才判的那道开关。

## 这一门守的是什么

``policy.DENY_TOOLS`` / ``PATCH_TOOLS`` / ``ADDED_GROUP`` 三张表作用在**注册期**
（工具在不在 AI 的工具表里）。但 ``/xiaowei on|off`` 要求**当次**改变行为，而注册
一个进程只发生一次 ⇒ 把开关做成注册期筛选，"开着却调不动"与"关了却还能调"就会随
重启时机随机出现。所以门禁挂在 ``hermes_contract.wrap_ctx`` 这个**所有工具、所有
注册路径**都必经的缝上，**每次调用**判一次。

三种失效形态，**全都不报错**：

1. 开关没人读（命令写了一个死键）⇒ 关了照发、开着照拒；
2. 读不到配置时**默认放行**（fail-open）⇒ 账号文件坏掉的那一刻危险工具全开；
3. 门禁读**写死的账号**（而不是调用参数里的那个）⇒ 多账号下 A 号的开关管到 B 号。

⇒ 本文件的判据一律下在"**工具到底跑没跑**"上：假客户端记下每一次厂商调用，
不测"门禁函数被调用了"（"调用了"在这里零信息量 —— 三种失效形态全都"调用了"）。

## 与 test_hermes_tool_contract.py 的分工

那份钉**契约形状**（299 个 handler 都接得住位置 dict、dispatch 上下文键不漏进请求体）；
这份钉**门**：受管工具被挡住的**结果**、不受管工具照旧能跑、以及开关翻转后**同一份
注册**里的行为跟着变（不重注册）—— 最后这条正是 ``/xiaowei on`` 的验收判据。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import re
from typing import Any

import pytest

#: 受管工具（``wpp_xw_*``）与它的真实厂商端点。
GATED_TOOL = "wpp_xw_invite"
GATED_ENDPOINT = "/XiaoWei/Invites"

#: 不受管工具 —— **控制组**：门禁不许漏挡它（漏挡 = 工具面整体哑掉）。
UNGATED_TOOL = "wpp_group_info"
UNGATED_ENDPOINT = "/Group/GetChatRoomInfo"

#: 朋友圈发布（第二条门禁）。开关在**门禁**里，白名单在**工具**里，两半都要在。
FC_TOOL = "wpp_fc_publish_text"

#: dispatch 固定注入的上下文键（``model_tools.py:826``）—— 原样带进来，验门禁不受它们影响。
DISPATCH_KWARGS = {"task_id": "task-1", "session_id": "sess-1", "user_task": "帮我查一下"}

#: 门禁回话里的关键串（照抄 TS 的两句）。
XW_CLOSED = "小微智能体未启用 (xiaoweiEnabled=false), 用 /xiaowei on 开启"
FC_CLOSED = "朋友圈发布未启用 (friendCirclePublishEnabled=false)"


# ------------------------------------------------------------------ 替身


class _RecClient:
    """记下**每一次厂商调用**（方法名 + 参数），并回一个成功信封。

    任何属性都变成"一个异步方法"：工具层两条路都要能走通 ——
    通用端点工具用 ``client.call(endpoint, body)``，``msg`` 域用具名方法
    （``client.publish_circle_text(...)``）。记的是**方法名**，所以两条路都能断言。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        async def fn(*a: Any, **kw: Any) -> dict:
            self.calls.append((name, a, kw))
            return {"Success": True, "Code": 0, "Data": {"BaseResponse": {"ret": 0}}}

        return fn


class _FakeAccount:
    def __init__(self, raw: dict) -> None:
        self.raw = raw


class _GateAdapter:
    """够用的适配器替身：``account()``（门禁读配置）+ ``_clients``（工具发请求）。

    ``account()`` 对**没登记的账号 raise** —— 真适配器就是这个形状
    （``accounts.resolve`` 对缺 ``agent`` 的账号**按设计** raise）。这条本身是判据：
    门禁必须把"读不到"判成**关闭**并把原因说出来，不能当成"没有这个开关键"
    （两者的回话不一样，而回话会经模型转述给老板）。
    """

    def __init__(self, default_id: str = "acct-42") -> None:
        #: ⚠️ 夹具的账号 id **故意不是 "default"**：门禁若把账号硬编码成
        #: ``DEFAULT_TOOL_ACCOUNT``，用 default 当夹具时变异体与原文**同值** ⇒ 测不出来
        #: （W7-1 就在这个坑里丢过一条变异体）。
        self.client = _RecClient()
        self.raws: dict[str, dict] = {default_id: {}}
        #: 真适配器的 ``_accounts`` 是"已加载账号"全集（与是否连上无关）——
        #: 门禁的单账号回退就靠它。保持与 ``raws`` 同步。
        self._accounts: dict[str, _FakeAccount] = {default_id: _FakeAccount({})}
        self._clients = {aid: self.client for aid in self.raws}
        self.getter_calls = 0

    # ---- 门禁侧
    def account(self, account_id: str) -> _FakeAccount:
        if account_id not in self.raws:
            raise KeyError(f"账号 {account_id} 不存在")
        return _FakeAccount(self.raws[account_id])

    def set_raw(self, account_id: str, raw: dict) -> None:
        self.raws[account_id] = raw
        self._accounts.setdefault(account_id, _FakeAccount(raw))
        self._clients.setdefault(account_id, self.client)

    # ---- 命令写回侧（``commands.reload_now`` 会调它）
    def reload_account(self, account_id: str) -> bool:
        from plugin.core import accounts as _accounts

        raw = _accounts.load_account_file(account_id)
        if raw:
            self.set_raw(account_id, raw)
        return True

    # ---- 只是为了让"getter 每次都被问"可观测
    def touch(self) -> "_GateAdapter":
        self.getter_calls += 1
        return self


class _CapCtx:
    """记全量注册 kwargs（名字 / schema / handler）—— 只记，不落任何地方。"""

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def register_tool(self, **kw: Any) -> None:
        self.tools[kw["name"]] = kw

    def __getattr__(self, _item: str):
        return lambda *a, **k: None


def _hermes_dispatch(handler, args: Any, **context: Any) -> str:
    """**逐字模仿** Hermes 的派发（``_kwargs_accepted_by`` + ``handler(args, **kwargs)``）。

    复制自 ``test_hermes_tool_contract.py`` 的同名函数（那边是权威副本，改一处要改两处）：
    两个文件都要能在**变异套件的单文件运行**下独立成立，所以共享夹具留在 conftest、
    这个纯函数各留一份。
    """
    params = inspect.signature(handler).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        accepted = dict(context)
    else:
        kinds = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        accepted = {k: v for k, v in context.items() if k in params and params[k].kind in kinds}
    out = handler(args, **accepted)
    if inspect.isawaitable(out):
        out = asyncio.run(out)
    return out


def _register(monkeypatch, adapter: _GateAdapter) -> _CapCtx:
    """真跑一遍 ``register()``，把 ``_get_adapter`` 换成假的（必须在注册**之前**打补丁）。

    门禁那把 getter 是 ``lambda: _adapter._get_adapter()`` —— **每次调用**重新取模块属性，
    所以补丁落在这个模块属性上就生效；直接传函数对象会让补丁落空（``registration.py``
    的注释与 ``test_hermes_tool_contract`` 的源级判据各钉了一半）。
    """
    from plugin.platform import adapter as _adapter

    monkeypatch.setattr(_adapter, "_get_adapter", lambda: adapter.touch())
    ctx = _CapCtx()
    importlib.import_module("plugin").register(ctx)
    return ctx


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """账号目录指到 tmp（``/xiaowei on`` 会**落盘**，绝不能碰真账号目录）。"""
    d = tmp_path / "accounts"
    d.mkdir()
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_DIR", str(d))
    monkeypatch.setenv("WECHATPRO_ACCOUNT_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.delenv("WECHATPRO_ACCOUNTS_JSON", raising=False)
    from plugin.core import overrides as _ov

    _ov._runtime_overrides.clear()
    yield d
    _ov._runtime_overrides.clear()


def write_account(dirpath, account_id: str, cfg: dict) -> None:
    (dirpath / f"{account_id}.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def _calls(client: _RecClient) -> list[tuple[str, tuple, dict]]:
    return list(client.calls)


# ==================================================== ① 门禁表

def test_every_gated_prefix_in_the_ai_surface_is_claimed(stubbed):
    """★ 前缀里的工具**一个都没漏**：AI 看得见的名字必须全在门禁名单里。

    这条是"将来新增的工具忘不掉"的机制：新加一个 ``wpp_xw_*`` 到厂商表里，
    它立刻出现在注册面里，而门禁名单没有它 ⇒ **本用例变红**（而不是那个新工具
    静悄悄绕过开关）。所以名单扫描对象是**真注册面**（``register()`` 之后的表），
    不是蓝本 —— ``wpp_fc_publish_raw`` 那种被撤注册的工具不在 AI 的能力面里。
    """
    from plugin.toolkit import policy

    ctx = _CapCtx()
    importlib.import_module("plugin").register(ctx)
    prefixes = tuple(p for g in policy.RUNTIME_GATES for p in g.prefixes)
    assert prefixes, "门禁居然一个前缀都没有 —— 这条判据会变成空转"

    missing = sorted(n for n in ctx.tools
                     if n.startswith(prefixes) and policy.gate_for(n) is None)
    assert not missing, (
        f"这些工具落在受管前缀里却没有门禁：{missing}。"
        "要么加进 policy 的名单，要么在 py2 里明确撤注册 —— 不许两者都不做")


def test_the_gate_lists_match_the_tools_that_exist(stubbed):
    """反向：门禁名单里的每个名字都必须是**真存在**的工具（名单不能凭空写）。

    只查"前缀里的都在名单"会漏掉另一种坏法：名单里写了 ``wpp_xw_invite2`` 这种
    拼错的名字 ⇒ 真工具没受管、而名单看着很全（``claims()`` 恒假）。
    """
    from plugin.toolkit import policy

    ctx = _CapCtx()
    importlib.import_module("plugin").register(ctx)
    ghosts = sorted(set(policy.GATED_TOOLS) - set(ctx.tools))
    assert not ghosts, f"门禁名单里这些工具根本不存在（拼错了？）：{ghosts}"


def test_gate_index_is_read_only():
    """门禁表是**安全面**：运行期不许被就地改（改一行 dict 就能放开一个工具）。"""
    from plugin.toolkit import policy

    with pytest.raises(TypeError):
        policy.GATED_TOOLS["wpp_xw_invite"] = policy.FRIEND_CIRCLE_PUBLISH_GATE  # type: ignore[index]


def test_a_tool_claimed_by_two_gates_raises_instead_of_picking_one():
    """两条门禁认领同一个工具 ⇒ **当场抛**。静默取其中一条 = "门禁取决于字典顺序"。"""
    from plugin.toolkit import policy

    a = policy.RuntimeGate(label="甲", key="k1", message="m1", prefixes=("x_",),
                           tools=("t1",), source="test")
    b = policy.RuntimeGate(label="乙", key="k2", message="m2", prefixes=("x_",),
                           tools=("t1",), source="test")
    with pytest.raises(RuntimeError) as ei:
        policy._build_gate_index((a, b))
    assert "甲" in str(ei.value) and "乙" in str(ei.value), \
        f"报错必须点出是哪两条门禁（不然没人知道去删哪条）：{ei.value}"


def test_the_two_real_gates_cover_the_expected_tools():
    """两条真门禁的开关键与工具面 —— 逐条钉住，防止"顺手缩小名单"。

    ⚠️ ``xiaoweiEnabled`` 是 TS 的键名（``src/index.ts:263`` 的 ``setAccountFlag``）；
    写成 snake_case 会**永远取不到值**（门禁恒关、工具全废），而且不报错。
    """
    from plugin.toolkit import policy

    assert policy.XIAOWEI_GATE.key == "xiaoweiEnabled"
    assert policy.FRIEND_CIRCLE_PUBLISH_GATE.key == "friendCirclePublishEnabled"
    assert policy.XIAOWEI_GATE.message == XW_CLOSED, "回话逐字对齐 TS（模型会转述给老板）"
    assert policy.FRIEND_CIRCLE_PUBLISH_GATE.message == FC_CLOSED
    # 2026-10-18：补齐蓝本缺的 10 个小微端点后，TS ``xiaowei-meta.ts`` 的 20 个工具
    # 全在名单里（以前只有 10 个，因为蓝本只生成了 10 个端点）。
    assert len(policy.XIAOWEI_GATE.tools) == 20, \
        f"TS 工具面是 20 个：{sorted(policy.XIAOWEI_GATE.tools)}"
    #: 四个**带守卫**的发布工具（``tools/msg.py``）。⚠️ 它们**没被撤注册** —— TS 侧
    #: 整体撤出了 AI 工具面（``friendcircle-meta.ts:45-47``），py2 仍暴露但带开关。
    #: 这是**有意保留**的差异（撤注册是减少一个能力，要老板拍板），本用例只钉现状。
    assert set(policy.FRIEND_CIRCLE_PUBLISH_GATE.tools) == {
        "wpp_fc_publish_images", "wpp_fc_publish_video",
        "wpp_fc_publish_text", "wpp_fc_publish_ai",
        # 2026-10-19：评论/点赞同样是公开写入
        "wpp_fc_comment",
    }
    #: ``wpp_fc_publish_raw`` 是**撤注册**那条路（原始 XML 发布），不在门禁名单里 ——
    #: 它连 AI 的能力面都不在，不需要门禁。两个机制别混。
    assert policy.gate_for("wpp_fc_publish_raw") is None
    assert "wpp_fc_publish_raw" in policy.DENY_TOOLS


def test_irreversible_write_tools_are_behind_the_admin_gate():
    """2026-10-19：群发/群管理/资金/第三方授权/账号资料类写工具不能再裸奔。

    这些动作要么**撤不回**（群发、加好友、群主转让），要么动钱/动授权（转账确认、
    JSLogin），要么改账号资料（隐私、头像、别名）。它们原先不在 ``adminToolsEnabled``
    门禁里 —— 模型被群消息诱导一句就能调。
    """
    from plugin.toolkit import policy

    gated = {
        "wpp_group_mass_text",
        "wpp_group_transfer_owner",
        "wpp_group_add_member",
        "wpp_group_invite_member",
        "wpp_group_set_name",
        "wpp_group_set_announcement",
        "wpp_group_set_access_verify",
        "wpp_group_del_member",
        "wpp_group_operate_admin",
        "wpp_friend_send_request",
        "wpp_finder_send_dm",
        "wpp_tenpay_create_redpacket",
        "wpp_tenpay_create_pre_transfer",
        "wpp_tenpay_confirm_pre_transfer",
        "wpp_wxapp_js_login",
        "wpp_oa_auth_mp_login",
        "wpp_user_set_privacy",
        "wpp_user_update_profile",
        "wpp_user_set_alias",
    }
    missing = sorted(n for n in gated if policy.gate_for(n) is not policy.ADMIN_TOOLS_GATE)
    assert not missing, f"这些不可逆/高风险写工具没有被管理面门禁认领：{missing}"


def test_ungated_tools_have_no_gate():
    from plugin.toolkit import policy

    assert policy.gate_for(UNGATED_TOOL) is None
    assert policy.gate_for("wpp_tool_set_step_count") is None
    assert policy.gate_for("") is None


# ==================================================== ② 读配置（raw_account_cfg）

def test_no_adapter_means_unreadable_not_closed_by_the_switch():
    """没有适配器 = **读不到**（``None``），不是"读到了、开关是 false"。"""
    from plugin.toolkit import policy

    assert policy.raw_account_cfg(None) is None


def test_an_unknown_account_is_unreadable():
    from plugin.toolkit import policy

    assert policy.raw_account_cfg(_GateAdapter(), "nope") is None


def test_a_non_mapping_raw_is_unreadable():
    """``raw`` 不是 Mapping（适配器换实现 / 半初始化）⇒ 读不到，不是"没开"。"""
    from plugin.toolkit import policy

    adapter = _GateAdapter()
    adapter.raws["acct-42"] = ["不是 dict"]  # type: ignore[assignment]
    assert policy.raw_account_cfg(adapter, "acct-42") is None


def test_missing_account_id_falls_back_to_the_default_account():
    """工具面没给 ``account_id`` 时读哪个账号 —— 与工具层的既有口径同源。"""
    from plugin.toolkit import policy

    adapter = _GateAdapter()
    adapter.set_raw(policy.DEFAULT_TOOL_ACCOUNT, {"xiaoweiEnabled": True})
    cfg = policy.raw_account_cfg(adapter, "")
    assert cfg is not None and cfg["xiaoweiEnabled"] is True


def test_the_default_sentinel_resolves_to_the_sole_account():
    """★★ 单账号部署（账号文件叫 ``boss2``）下，``default`` 必须落到那个唯一账号。

    不解析的后果不是"读不到配置"这么轻：门禁会 **fail-closed** 掉**所有**受管工具
    （朋友圈/管理面/心微全哑），而现场只留一句 debug 日志，看起来像"开关忘了开"。
    """
    from plugin.toolkit import policy

    adapter = _GateAdapter(default_id="boss2")
    adapter.set_raw("boss2", {"xiaoweiEnabled": True})
    cfg = policy.raw_account_cfg(adapter, policy.DEFAULT_TOOL_ACCOUNT)
    assert cfg is not None and cfg["xiaoweiEnabled"] is True


def test_the_default_sentinel_does_not_fall_back_when_two_accounts_exist():
    """多账号时 ``default`` 不再猜 —— 猜错就是 A 号的开关管到 B 号（跨号事故）。"""
    from plugin.toolkit import policy

    adapter = _GateAdapter(default_id="boss2")
    adapter.set_raw("boss2", {"xiaoweiEnabled": True})
    adapter.set_raw("boss3", {"xiaoweiEnabled": True})
    assert policy.raw_account_cfg(adapter, policy.DEFAULT_TOOL_ACCOUNT) is None


def test_raw_account_cfg_hands_back_a_copy():
    """★ 交出去的必须是**副本**：门禁拿到活字典就等于拿到账号配置的写权
    （改一下就绕过开关，而"谁改的"永远查不出来）。"""
    from plugin.toolkit import policy

    adapter = _GateAdapter()
    adapter.set_raw("acct-42", {"xiaoweiEnabled": False})
    cfg = policy.raw_account_cfg(adapter, "acct-42")
    assert cfg is not None
    cfg["xiaoweiEnabled"] = True
    assert adapter.raws["acct-42"]["xiaoweiEnabled"] is False, \
        "门禁改动了账号配置的真身 —— 下一条真话就没人信了"


# ==================================================== ③ 门禁工厂（每次调用判一次）

def test_unmanaged_tools_get_no_wrapper_at_all():
    """不受管 ⇒ 工厂回 ``None`` ⇒ ``_ToolCtx`` **一个包装都不加**（299 个里只 14 个多一层）。

    量的是"少一层间接"：包装多了会让 W0 那套契约判据（首参名 / ``**kwargs`` /
    ``is_async``）多出 247 个无关对象要过。
    """
    from plugin.toolkit import policy

    factory = policy.make_tool_gate(lambda: _GateAdapter())
    assert factory(UNGATED_TOOL) is None
    assert factory("") is None
    assert factory(GATED_TOOL) is not None


def test_a_closed_gate_answers_with_the_ts_message():
    from plugin.toolkit import policy

    pre = policy.make_tool_gate(lambda: _GateAdapter())(GATED_TOOL)
    assert pre is not None
    assert pre({"account_id": "acct-42"}) == XW_CLOSED


def test_an_open_gate_lets_the_call_through():
    from plugin.toolkit import policy

    adapter = _GateAdapter()
    adapter.set_raw("acct-42", {"xiaoweiEnabled": True})
    pre = policy.make_tool_gate(lambda: adapter)(GATED_TOOL)
    assert pre is not None
    assert pre({"account_id": "acct-42"}) is None


def test_an_unreadable_config_is_closed_and_says_why():
    """★ 读不到 ⇒ **关闭**，而且回话必须能让老板分辨"读不出来"与"没开"。

    混为一谈的后果：账号文件坏掉时老板看到的是「你没开这个开关」，于是去
    ``/xiaowei on`` —— 而真正的故障（文件读不了）被这层话术盖住。
    """
    from plugin.toolkit import policy

    pre = policy.make_tool_gate(lambda: _GateAdapter())(GATED_TOOL)
    assert pre is not None
    out = pre({"account_id": "不存在的账号"})
    assert out is not None and out != XW_CLOSED, "读不到必须与'没开'分开说"
    assert "读不到账号配置" in out and "按未启用处理" in out, out


def test_an_adapter_getter_failure_is_closed_too():
    """取不到适配器（启动中 / 已停机）⇒ 关闭。**fail-closed 没有例外**。"""
    from plugin.toolkit import policy

    def boom():
        raise RuntimeError("适配器还没起")

    pre = policy.make_tool_gate(boom)(GATED_TOOL)
    assert pre is not None
    out = pre({"account_id": "acct-42"})
    assert out is not None and "取不到适配器" in out and "按未启用处理" in out, out


def test_the_gate_reads_the_account_named_in_the_call():
    """★ 多账号：门禁必须读**调用参数里那个账号**的开关。

    读错账号的两种表现都很坏：读 default ⇒ 一号的开关管到所有号；
    写死某个号 ⇒ 那个号的开关能放开别的号。这里 A 开 B 关，两个方向都钉。
    """
    from plugin.toolkit import policy

    adapter = _GateAdapter()
    adapter.set_raw("acct-a", {"xiaoweiEnabled": True})
    adapter.set_raw("acct-b", {"xiaoweiEnabled": False})
    pre = policy.make_tool_gate(lambda: adapter)(GATED_TOOL)
    assert pre is not None
    assert pre({"account_id": "acct-a"}) is None, "A 号开着，应当放行"
    assert pre({"account_id": "acct-b"}) == XW_CLOSED, "B 号关着，应当拒绝"


def test_a_call_aimed_at_an_unknown_account_is_closed():
    """★ 指名一个**不认识**的账号 ⇒ 关闭（不许悄悄退回 default 的开关）。

    退回 default 的后果：一个只想发给 X 号的调用，被 default 号（开关开着）放行 ⇒
    跨账号授权。这正是 fail-closed 要挡的形状。
    """
    from plugin.toolkit import policy

    adapter = _GateAdapter()
    adapter.set_raw(policy.DEFAULT_TOOL_ACCOUNT, {"xiaoweiEnabled": True})
    pre = policy.make_tool_gate(lambda: adapter)(GATED_TOOL)
    assert pre is not None
    out = pre({"account_id": "另一个号"})
    assert out is not None and out != XW_CLOSED, "必须拒绝，且说明是读不到配置"


def test_the_gate_is_asked_again_on_every_call():
    """★ 每次调用**重新问** getter —— 这是 ``/xiaowei on`` **当次生效**的机制。

    注册期缓存适配器/配置的实现照样能通过一切"注册得上"的判据，只在
    "改了开关之后行为不变"时才现形（而那时老板已经以为开关没用了）。
    """
    from plugin.toolkit import policy

    adapter = _GateAdapter()
    pre = policy.make_tool_gate(lambda: adapter.touch())(GATED_TOOL)
    assert pre is not None
    for _ in range(3):
        pre({"account_id": "acct-42"})
    assert adapter.getter_calls == 3, \
        f"getter 只被问了 {adapter.getter_calls} 次（3 次调用）—— 配置被缓存了"


def test_a_non_dict_payload_does_not_crash_the_gate():
    """模型给的不是 dict（协议异常）时门禁**不许自己炸** —— 它是最外层那道闸，
    抛出去就是 tool_error，而真正该说话的是 W0 的契约适配层（它有自己的判据）。

    取不到 ``account_id`` 时按 :data:`policy.DEFAULT_TOOL_ACCOUNT` 走 —— 与工具层
    自己的兜底同源（``registry`` / ``tools/msg.py`` 里同一个字面量）。所以两种结果
    都要能出现：默认号开着 ⇒ 放行；默认号读不到 ⇒ 回一句拒绝。**唯一不许的是抛异常。**
    """
    from plugin.toolkit import policy

    adapter = _GateAdapter()
    closed = policy.make_tool_gate(lambda: adapter)(GATED_TOOL)
    assert closed is not None
    for payload in (None, ["不是", "dict"], "也不是"):
        out = closed(payload)
        assert out is not None and out.startswith(XW_CLOSED), \
            f"非 dict 载荷没被拒（或炸了）：{out!r}"

    adapter.set_raw(policy.DEFAULT_TOOL_ACCOUNT, {"xiaoweiEnabled": True})
    opened = policy.make_tool_gate(lambda: adapter)(GATED_TOOL)
    assert opened is not None
    for payload in (None, ["不是", "dict"], "也不是"):
        assert opened(payload) is None, "没有 account_id 时应当按默认账号判（与工具层同源）"


# ==================================================== ④ 真注册 + 真派发

def test_a_closed_gate_really_blocks_the_call(stubbed, monkeypatch):
    """★ 反向判据：**厂商一次都没被调到**，且回话是那句"未启用"。

    "调用被拒绝了"必须看厂商侧 —— 只看返回值的话，一个"先发出去再回话"的实现
    照样绿（真机表现：朋友圈真发出去了，模型说"未启用"）。
    """
    adapter = _GateAdapter()
    ctx = _register(monkeypatch, adapter)
    out = _hermes_dispatch(ctx.tools[GATED_TOOL]["handler"],
                           {"wxids": ["wxid_a"], "account_id": "acct-42"}, **DISPATCH_KWARGS)
    assert out == XW_CLOSED, f"回话必须是门禁那句（模型会照着它说话）：{out!r}"
    assert _calls(adapter.client) == [], \
        f"开关关着却已经打到厂商了：{_calls(adapter.client)}"


def test_an_open_gate_really_lets_the_request_out(stubbed, monkeypatch):
    """★ 反向判据的另一半：开着时**请求真的出去了**，端点是 XiaoWei 那个。"""
    adapter = _GateAdapter()
    adapter.set_raw("acct-42", {"xiaoweiEnabled": True})
    ctx = _register(monkeypatch, adapter)
    out = _hermes_dispatch(ctx.tools[GATED_TOOL]["handler"],
                           {"wxids": ["wxid_a"], "account_id": "acct-42"}, **DISPATCH_KWARGS)
    calls = _calls(adapter.client)
    assert calls, f"开关开着却一次厂商请求都没发：{out!r}"
    name, args, _kw = calls[-1]
    assert name == "call", f"通用工具应当走 client.call：{name}"
    assert args[0] == GATED_ENDPOINT, f"端点错了：{args[0]}"
    assert args[1] == {"wxids": ["wxid_a"]}, \
        f"请求体应为模型给的参数原样（account_id 由 handler 取走）：{args[1]}"
    assert XW_CLOSED not in out, f"开着还被门禁挡了：{out!r}"


def test_flipping_the_switch_changes_the_same_registration(stubbed, monkeypatch):
    """★ ``/xiaowei on`` 的验收判据：**不重注册**，同一份 handler 行为跟着变。

    生产里 ``/xiaowei on`` 走的是"落盘 + ``reload_account``"⇒ 适配器换掉缓存那份
    ``Account``（W6 已验），门禁每次调用重新读 ⇒ 下一个工具调用就生效。
    这里把 reload 那一跳换成直接改 ``raws``（W6 的套件验的是真 reload），
    钉的是门禁这半：**它读的是调用当刻的那份配置**。
    """
    adapter = _GateAdapter()
    ctx = _register(monkeypatch, adapter)
    handler = ctx.tools[GATED_TOOL]["handler"]           # 同一个对象，全程不换
    args = {"wxids": ["wxid_a"], "account_id": "acct-42"}

    assert _hermes_dispatch(handler, args, **DISPATCH_KWARGS) == XW_CLOSED
    assert _calls(adapter.client) == []

    adapter.set_raw("acct-42", {"xiaoweiEnabled": True})   # ⇐ 等价于 /xiaowei on + reload
    out = _hermes_dispatch(handler, args, **DISPATCH_KWARGS)
    assert len(_calls(adapter.client)) == 1, f"开了之后应当放行：{out!r}"

    adapter.set_raw("acct-42", {"xiaoweiEnabled": False})  # ⇐ 等价于 /xiaowei off + reload
    assert _hermes_dispatch(handler, args, **DISPATCH_KWARGS) == XW_CLOSED
    assert len(_calls(adapter.client)) == 1, "关了之后不许再发"


def test_ungated_tools_are_not_touched_by_the_gate(stubbed, monkeypatch):
    """控制组：不受管的工具照旧打到厂商 —— 门禁**不许漏挡**。

    漏挡的表现最像"没事"：整片工具面回一句"未启用"，而注册数、doctor 全绿。
    """
    adapter = _GateAdapter()
    ctx = _register(monkeypatch, adapter)
    out = _hermes_dispatch(ctx.tools[UNGATED_TOOL]["handler"],
                           {"QID": "12345@chatroom", "account_id": "acct-42"},
                           **DISPATCH_KWARGS)
    calls = _calls(adapter.client)
    assert calls and calls[-1][1][0] == UNGATED_ENDPOINT, \
        f"不受管的工具被打到了门禁（或没发请求）：{out!r} / {calls}"


# ==================================================== ⑤ 朋友圈发布（第二条门禁 + 修掉的 bug）

#: 四处守卫里能在单测里**派发**的三个。``wpp_fc_publish_ai`` 不在此列：它会 fork ``mmx``
#: 子进程去真生成配图 ⇒ 本机跑测试会起子进程。它那一处由**同形判据**兜住
#: （``test_tool_gate_wiring`` 断言 msg.py 里恰好 4 处 ``policy.raw_account_cfg`` +
#: 4 处 ``if acct_cfg is None``；把任何一处拆成 ``if False`` 计数就掉到 3）。
FC_TOOLS_SAFE = ("wpp_fc_publish_text", "wpp_fc_publish_images", "wpp_fc_publish_video")

#: 每个工具真正打出去的厂商方法名（"真发了"的那个动作，不是"调用了"）。
FC_CALLS = {"wpp_fc_publish_text": "publish_circle_text",
            "wpp_fc_publish_images": "publish_circle_images",
            "wpp_fc_publish_video": "publish_circle_video"}

#: 各工具走守卫所需的载荷（图片/视频那条路不带载荷会在守卫**之后**的分支上失败）。
FC_PAYLOAD = {"wpp_fc_publish_text": {"title": "文案"},
              "wpp_fc_publish_images": {"title": "文案", "imageBase64List": ["aGk="]},
              "wpp_fc_publish_video": {"title": "文案", "videoBase64": "aGk=",
                                       "thumbBase64": "aGk="}}


def test_the_fc_publish_gate_blocks_before_the_whitelist(stubbed, monkeypatch):
    """★ 开关关着时，即使 ``callerWxid`` **在白名单里**也必须拒 —— 而且是门禁那句。

    两半的边界就在这里：开关在**门禁**（调用前，所有路径都过），白名单在**工具里**。
    谁先说话决定了回话对不对（开关关着却说"无权限"会让老板去查白名单）。
    """
    adapter = _GateAdapter()
    adapter.set_raw("acct-42", {"friendCirclePublishEnabled": False,
                                "friendCirclePublishAllowFrom": ["wxid_boss"]})
    ctx = _register(monkeypatch, adapter)
    for tool in FC_TOOLS_SAFE:
        args = {**FC_PAYLOAD[tool], "callerWxid": "wxid_boss", "account_id": "acct-42"}
        out = _hermes_dispatch(ctx.tools[tool]["handler"], args, **DISPATCH_KWARGS)
        assert out == FC_CLOSED, f"{tool}：开关关着时先说「未启用」，不提白名单：{out!r}"
        assert _calls(adapter.client) == [], f"{tool}：拒绝了却已经发了朋友圈"


def test_the_fc_whitelist_branch_is_reachable_now(stubbed, monkeypatch):
    """★ 这条钉的是那个**真 bug**：白名单分支此前**永远走不到**。

    原先四处守卫写 ``adapter.get_account(aid)`` —— 适配器上**没有这个方法**（全树只在
    那 4 行出现过）⇒ 真机必然 ``AttributeError`` ⇒ 回话是"调用失败: 'WppAdapter'
    object has no attribute 'get_account'"，既不是"未启用"，白名单也从未生效过
    （等于**白名单形同没有**：任何 caller 都能发）。修法是读配置改走
    ``policy.raw_account_cfg``。⇒ 本用例要求：开关开着 + caller 不在白名单 ⇒
    拒绝且**说明是无权限**；caller 在白名单 ⇒ 真发。

    ⚠️ 三个工具**各判一遍**：四处守卫的文本逐字相同，只测一处的话，另外三处写成
    ``if False:`` 测试照样全绿（这是本轮变异体 SURVIVED 抓到的第一个真盲区）。
    """
    adapter = _GateAdapter()
    adapter.set_raw("acct-42", {"friendCirclePublishEnabled": True,
                                "friendCirclePublishAllowFrom": ["wxid_boss"]})
    ctx = _register(monkeypatch, adapter)
    for tool in FC_TOOLS_SAFE:
        handler = ctx.tools[tool]["handler"]
        base = {**FC_PAYLOAD[tool], "account_id": "acct-42"}
        before = len(_calls(adapter.client))

        out = _hermes_dispatch(handler, {**base, "callerWxid": "wxid_陌生人"},
                               **DISPATCH_KWARGS)
        assert "无权限" in out, f"{tool}：白名单外的调用方必须被拒：{out!r}"
        assert len(_calls(adapter.client)) == before, f"{tool}：白名单外的调用方已经发出去了"

        out = _hermes_dispatch(handler, {**base, "callerWxid": "wxid_boss"},
                               **DISPATCH_KWARGS)
        calls = _calls(adapter.client)
        assert len(calls) == before + 1 and calls[-1][0] == FC_CALLS[tool], \
            f"{tool}：白名单内的调用方应当真发出去：{out!r} / {calls}"


@pytest.mark.parametrize("tool", FC_TOOLS_SAFE)
def test_each_fc_handler_checks_the_whitelist_itself(stubbed, monkeypatch, tool):
    """第二道防线：**绕过运行期门禁**直接调 handler，白名单仍要拦。

    端到端派发时 ``policy.make_tool_gate`` 的 ``pre`` 先判白名单 ⇒ ``msg.py`` 里那三处
    **同形**检查永远走不到（2026-10-18 变异套件报「白名单判定拆成摆设」存活就是这个
    原因）。门禁是在 W7-5 才加的，handler 自己那半仍是从 TS 搬来的纵深防御：门禁没
    装上/被绕开时不能变成谁都能发。所以这里从 ``__original_handler__`` 直接打原函数
    （它就是 msg.py 里那个闭包，不再经过门禁包装层）。
    """
    adapter = _GateAdapter()
    adapter.set_raw("acct-42", {"friendCirclePublishEnabled": True,
                                "friendCirclePublishAllowFrom": ["wxid_boss"]})
    ctx = _register(monkeypatch, adapter)
    raw = ctx.tools[tool]["handler"].__original_handler__
    base = {**FC_PAYLOAD[tool], "account_id": "acct-42"}
    before = len(_calls(adapter.client))

    out = asyncio.run(raw(**{**base, "callerWxid": "wxid_陌生人"}))
    assert "无权限" in json.dumps(out, ensure_ascii=False), \
        f"{tool}：绕过门禁后 handler 自己的白名单必须仍然拦：{out!r}"
    assert len(_calls(adapter.client)) == before, \
        f"{tool}：被第二道防线拒绝却已经发出去了"

    out = asyncio.run(raw(**{**base, "callerWxid": "wxid_boss"}))
    calls = _calls(adapter.client)
    assert len(calls) == before + 1 and calls[-1][0] == FC_CALLS[tool], \
        f"{tool}：白名单内的调用方应当真发出去：{out!r} / {calls}"


def _nogate(registered):
    """剥掉门禁那一层，只留 W0 的适配层 —— 用来验证**工具自己的守卫**。

    ⚠️ 为什么必须剥：读不到配置时**门禁先说话**（它排在最前），工具那一层的守卫
    **根本轮不到执行** ⇒ 把工具守卫写成 fail-open 在端到端用例里**完全看不见**
    （本轮 ``tool-gate-msg`` 的两条 fail-open 变异体正是这么活下来的）。
    而门禁不是唯一的调用路径（工具面探针、直调、将来新增的注册入口都绕不过它），
    所以工具层必须自带判据 —— 这一层是"门禁没装上时"的最后一道。
    """
    from plugin.toolkit import hermes_contract as hc

    return hc.adapt_handler(getattr(registered, "__original_handler__", registered))


def test_the_fc_tools_refuse_when_the_config_is_unreadable(stubbed, monkeypatch):
    """工具那半也 fail-closed：读不到配置 ⇒ 拒绝发布（不是"当作没有白名单、放行"）。

    ① 经门禁：门禁先说（那句带「按未启用处理」的原因），工具压根不执行；
    ② 剥掉门禁：**工具自己**那一层也必须是拒绝 + 零外呼。
    三个工具各判一遍 —— fail-open 只要有一处写错，那一处的工具就成了
    "任何 caller 都能发"，而另外两处仍然绿着。
    """
    adapter = _GateAdapter()
    adapter.set_raw("acct-42", {"friendCirclePublishEnabled": True})
    ctx = _register(monkeypatch, adapter)
    for tool in FC_TOOLS_SAFE:
        args = {**FC_PAYLOAD[tool], "account_id": "不存在的账号"}
        out = _hermes_dispatch(ctx.tools[tool]["handler"], args, **DISPATCH_KWARGS)
        assert "读不到账号配置" in out, f"{tool}：{out!r}"
        assert _calls(adapter.client) == [], f"{tool}：读不到配置却已经发了"

        out = _hermes_dispatch(_nogate(ctx.tools[tool]["handler"]), args,
                               **DISPATCH_KWARGS)
        assert "读不到账号配置" in out, f"{tool}（无门禁）：{out!r}"
        assert _calls(adapter.client) == [], f"{tool}（无门禁）：读不到配置却已经发了"


# ==================================================== ⑥ 用户面：命令与门禁是同一个开关

def test_the_xiaowei_command_opens_the_gate_it_just_wrote(stubbed, monkeypatch, sandbox):
    """★★ ``/xiaowei on`` → **门禁当次放行**（端到端：命令 → 落盘 → 重载 → 门禁）。

    生产链路：命令写账号文件（W6）→ ``reload_account`` 换掉缓存那份 ``Account`` →
    门禁每次调用重读 ⇒ 下一个工具调用就放行。这里除了"真 ``reload_account``"那一跳
    用替身之外，其余全是真件（真 ``register()``、真门禁、真命令、真落盘）。
    少了这条，两条线（命令写键 / 门禁读键）各自都"有测试"，合起来却是断的 ——
    那正是"开关在、门不在"的形态。
    """
    from plugin.platform import commands

    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test",
                                       "xiaoweiEnabled": False})
    adapter = _GateAdapter()
    adapter.reload_account("acct-42")                      # 初始状态：读盘
    ctx = _register(monkeypatch, adapter)
    args = {"wxids": ["wxid_a"], "account_id": "acct-42"}

    assert _hermes_dispatch(ctx.tools[GATED_TOOL]["handler"], args, **DISPATCH_KWARGS) \
        == XW_CLOSED
    assert _calls(adapter.client) == []

    reply = commands.handle_command("acct-42", "/xiaowei on", adapter=adapter)
    assert reply.startswith("✅ 小微智能体已开启"), reply
    # ① 盘上真的变了（判据在字节上，不在回复里）
    on_disk = json.loads((sandbox / "acct-42.json").read_text(encoding="utf-8"))
    assert on_disk["xiaoweiEnabled"] is True, f"命令回了 ✅ 却没落盘：{on_disk}"
    # ② 门禁当次放行 —— **同一个 handler 对象**，没有重注册
    out = _hermes_dispatch(ctx.tools[GATED_TOOL]["handler"], args, **DISPATCH_KWARGS)
    assert len(_calls(adapter.client)) == 1, f"开了之后门禁没放行：{out!r}"

    # ③ 关回去同样当次生效
    assert commands.handle_command("acct-42", "/xiaowei off", adapter=adapter) \
        .startswith("✅ 小微智能体已关闭")
    assert _hermes_dispatch(ctx.tools[GATED_TOOL]["handler"], args, **DISPATCH_KWARGS) \
        == XW_CLOSED
    assert len(_calls(adapter.client)) == 1, "关了之后不许再发"


def test_the_xiaowei_command_writes_a_scalar_not_a_dict(stubbed, monkeypatch, sandbox):
    """★ 写成 ``{"enabled": true}`` 会让门禁**恒开**（非空 dict 是真值）。

    这是最容易犯、最难发现的一种：命令、状态页、落盘全都"看着对"，
    而门禁永远读到真值 ⇒ ``/xiaowei off`` 完全无效。
    """
    from plugin.platform import commands

    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test"})
    commands.handle_command("acct-42", "/xiaowei on")
    raw = json.loads((sandbox / "acct-42.json").read_text(encoding="utf-8"))
    assert raw["xiaoweiEnabled"] is True, f"必须是标量 true：{raw.get('xiaoweiEnabled')!r}"

    commands.handle_command("acct-42", "/xiaowei off")
    raw = json.loads((sandbox / "acct-42.json").read_text(encoding="utf-8"))
    assert raw["xiaoweiEnabled"] is False, f"关掉必须是 false：{raw.get('xiaoweiEnabled')!r}"


def test_the_xiaowei_status_reports_what_the_gate_will_do(stubbed, sandbox):
    """状态页与门禁读的是**同一个键**：页面上是 ✅ 时门禁就该放行。

    两边各读一处（页面读内存覆盖、门禁读文件）时，"页面开着、工具却拒绝"会
    一直挂着 —— 老板只能靠试才知道。
    """
    from plugin.platform import commands

    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test",
                                       "xiaoweiEnabled": True})
    assert "✅ 开启" in commands.handle_command("acct-42", "/xiaowei status")
    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test",
                                       "xiaoweiEnabled": False})
    assert "❌ 关闭" in commands.handle_command("acct-42", "/xiaowei status")


def test_the_xiaowei_status_says_unreadable_instead_of_off(stubbed, tmp_path, monkeypatch):
    """账号文件读不出来时状态页必须说"读不到"，**不能**报成 "❌ 关闭"。

    报成"关闭"的后果：老板照着去 ``/xiaowei on``，而那会**写一份新配置**到
    一个读不出来的账号上（真故障被话术盖住）。
    """
    from plugin.platform import commands

    monkeypatch.setenv("WECHATPRO_ACCOUNTS_DIR", str(tmp_path / "空的目录"))
    assert "读不到" in commands.handle_command("acct-42", "/xiaowei status"), \
        "读不到配置时报'关闭'就是撒谎（门禁那边会说'读不到'，两边必须一致）"


def test_the_xiaowei_usage_line_matches_ts():
    from plugin.platform import commands

    out = commands.handle_command("acct-42", "/xiaowei")
    assert out == "用法: /xiaowei on|off|status\n示例: /xiaowei on (开启小微智能体)"
    assert "用法: /xiaowei on|off|status" in commands.handle_command("acct-42", "/xiaowei bogus")


# ==================================================== ⑦ 「读不到」不能说成「没开」

def test_state_label_has_three_values_not_two():
    """★ 「读不到」必须是**第三态**。二值化的 ``✅ 开启/❌ 关闭`` 会把
    "我读不出来" 印成 "关闭" —— 老板于是去做 ``/xiaowei on``（或 ``/heartflow on``），
    而真故障（账号文件坏了）被这句话盖住。

    这是 W7-5 顺手收口的一族：``/xiaowei`` 的门禁回话早就有第三态
    （``RuntimeGate.closed(reason)``），而**同一个开关**的状态页当时还只有两态 ——
    两个面在描述同一个开关却说两套话。
    """
    from plugin.platform import commands

    assert commands.state_label(True) == "✅ 开启"
    assert commands.state_label(False) == "❌ 关闭"
    assert commands.state_label(None) == commands.UNREADABLE_STATE_MSG
    assert "读不到" in commands.state_label(None)
    assert commands.UNREADABLE_STATE_MSG not in (commands.state_label(True),
                                                 commands.state_label(False))


def test_feature_state_distinguishes_unreadable_from_off(stubbed, sandbox):
    """★ 同一个账号：文件里明确写着 ``enabled:false`` ⇒ ``False``；
    文件读不出来 ⇒ ``None``。**两者不许同值**（这正是"谎话"的机制）。"""
    from plugin.platform import commands

    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test",
                                       "affection": {"enabled": False}})
    assert commands._feature_state("acct-42", "affection") is False
    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test",
                                       "affection": {"enabled": True}})
    assert commands._feature_state("acct-42", "affection") is True
    assert commands._feature_state("不存在的账号", "affection") is None, \
        "读不到必须与「配置里写了 false」区分开"


def test_every_feature_status_page_says_unreadable_instead_of_off(stubbed, tmp_path,
                                                                 monkeypatch):
    """★ 三个状态页（``/xiaowei`` ``/affection`` ``/heartflow``）在账号文件读不出来时
    **都要说「读不到」**，一个都不许印「关闭/off」。

    只修 ``/xiaowei`` 那一处最省事，但那样同一个故障在三个页面上有三种说法，
    而老板看到的是"有的开关关了、有的开关开着"。
    """
    from plugin.platform import commands

    monkeypatch.setenv("WECHATPRO_ACCOUNTS_DIR", str(tmp_path / "空的目录"))
    for cmd in ("/xiaowei status", "/affection status", "/affection", "/heartflow status"):
        out = commands.handle_command("acct-42", cmd)
        #: 只认**报这个开关状态的那一行** —— ``/heartflow status`` 里还有别的段落
        #: （自适应调阈 / 重复闸…）本来就该说"关闭"，拿整篇当一个断言会误伤，
        #: 也会让这条判据变成"随便哪里提到读不到就算过"。
        lines = [ln for ln in out.splitlines()
                 if re.match(r"^(小微智能体|.*状态): ", ln or "")]
        assert len(lines) == 1, f"{cmd} 的状态行不是恰好一条：{lines} / {out!r}"
        assert "读不到" in lines[0], f"{cmd} 的状态行把「读不到」说成了别的：{lines[0]!r}"



# ================================ ⑧ 写回的是「整份清单」，不是「这次改的那些」
#
# 这一节盯的是同一件事的**数据面**：W6 把写回做成"读原始 dict → 改一个键 → 整体替换"
# ⇒ 传进来的值必须**已经是最终值**。若某个调用方图省事只把自己新改的那几项传下去
# （``new_items`` → ``changed``），命令照样回 ✅、文件照样写成功，而**原有名单被整段清空**。
# 所以判据一律下在**盘上的字节**上：回话是不是 ✅、有没有异常，对这条故障是零信息量的。

def _disk(dirpath, account_id: str) -> dict:
    return json.loads((dirpath / f"{account_id}.json").read_text(encoding="utf-8"))


def test_allowlist_add_appends_to_the_existing_list(stubbed, sandbox):
    """★ ``/user add`` 必须**追加**。写回传"这次改的那些"会清空现有名单，
    而回话是「✅ 已加入私聊白名单: wxid_new」—— 老板看不出少了一个人。"""
    from plugin.platform import commands

    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test",
                                       "allowFrom": ["wxid_old"]})
    out = commands.handle_command("acct-42", "/user add wxid_new")
    assert "已加入私聊白名单: wxid_new" in out, out
    assert _disk(sandbox, "acct-42")["allowFrom"] == ["wxid_old", "wxid_new"], \
        "盘上少了原有名单 —— 写回传的是增量而不是整份清单"


def test_allowlist_add_takes_several_targets_at_once(stubbed, sandbox):
    """批量：``add a b`` 两个都要落盘（TS ``handleWhitelistCommand`` 是批量的）。"""
    from plugin.platform import commands

    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test", "allowFrom": []})
    commands.handle_command("acct-42", "/user add wxid_a wxid_b")
    assert _disk(sandbox, "acct-42")["allowFrom"] == ["wxid_a", "wxid_b"]


def test_allowlist_del_removes_only_the_named_one(stubbed, sandbox):
    """``/user del`` 只删点名的那几个 —— 同样是"整份清单"的判据。"""
    from plugin.platform import commands

    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test",
                                       "allowFrom": ["wxid_a", "wxid_b", "wxid_c"]})
    out = commands.handle_command("acct-42", "/user del wxid_b")
    assert "已移出私聊白名单: wxid_b" in out, out
    assert _disk(sandbox, "acct-42")["allowFrom"] == ["wxid_a", "wxid_c"]


def test_blacklist_writes_the_blacklist_groups_key(stubbed, sandbox):
    """★ ``/blacklist`` 落的是账号文件里的 ``blacklistGroups``（``triggers.py:203`` 读的
    就是它）。写成 ``blacklist`` 或写进 ``allowFrom`` 都会**回 ✅ 而一票否决永不生效**。"""
    from plugin.platform import commands

    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test"})
    out = commands.handle_command("acct-42", "/blacklist add group_x")
    assert "已加入黑名单群: group_x" in out, out
    raw = _disk(sandbox, "acct-42")
    assert raw["blacklistGroups"] == ["group_x"], f"黑名单没落到 blacklistGroups：{raw}"
    assert "allowFrom" not in raw, "黑名单命令不该动私聊白名单"


def test_blacklist_list_reads_the_same_key_it_writes(stubbed, sandbox):
    """写一处、读一处 —— 两边同一个键名，老板才能用 ``/blacklist list`` 复核自己刚写的。"""
    from plugin.platform import commands

    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test",
                                       "blacklistGroups": ["group_x", "group_y"]})
    out = commands.handle_command("acct-42", "/blacklist list")
    assert out == "📋 黑名单群 (2):\n- group_x\n- group_y", out


def test_a_feature_toggle_keeps_the_sibling_keys(stubbed, sandbox):
    """★ ``/affection off`` 只翻 ``enabled``，**同级的阈值不许丢**。

    写回传 ``{"enabled": false}``（而不是 ``{**base, "enabled": false}``）时，
    ``maxUserAffection`` 等键被整段拍扁 —— 命令回 ✅、文件也写成功，
    而老板调过的阈值**静默回到默认**。
    """
    from plugin.platform import commands

    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test",
                                       "affection": {"enabled": True,
                                                     "maxUserAffection": 88}})
    out = commands.handle_command("acct-42", "/affection off")
    assert "已写入账号配置" in out, out
    feat = _disk(sandbox, "acct-42")["affection"]
    assert feat["enabled"] is False, feat
    assert feat.get("maxUserAffection") == 88, f"同级阈值被写没了：{feat}"


def test_a_write_that_changes_nothing_does_not_touch_the_file(stubbed, sandbox):
    """没变化就**不写**：不落盘、不备份、回话里也不出现那句「已写入账号配置」
    （那句话是"盘上真的变了"的凭据，多印一次它就不再是凭据）。"""
    from plugin.platform import commands

    write_account(sandbox, "acct-42", {"agent": "wpp-wechat-test",
                                       "allowFrom": ["wxid_a"]})
    before = (sandbox / "acct-42.json").read_text(encoding="utf-8")
    out = commands.handle_command("acct-42", "/user add wxid_a")
    assert "已加入私聊白名单: (均已存在)" in out, out
    assert "已写入账号配置" not in out, f"没写却说写了：{out!r}"
    assert (sandbox / "acct-42.json").read_text(encoding="utf-8") == before


def test_an_unreadable_account_refuses_the_write(stubbed, sandbox):
    """★ 底稿读不出来 ⇒ **拒绝写**，而不是拿空名单当底稿整体替换。

    这条是"静默清段"的另一半：名单命令的 ``items`` 为 ``None`` 时若继续走，
    ``new_items`` 就是"只要这几个" ⇒ 一条 ``/user add`` 清空整份白名单。
    """
    from plugin.platform import commands

    out = commands.handle_command("acct-42", "/user add wxid_a")
    assert "读不到账号文件" in out, out
    assert not (sandbox / "acct-42.json").exists(), "读不到底稿却给它造了一份新配置"


def test_an_unreadable_account_refuses_the_feature_toggle(stubbed, sandbox):
    """同一个拒绝路，走特征开关那半（``/affection on`` 也不许凭空造账号文件）。"""
    from plugin.platform import commands

    out = commands.handle_command("acct-42", "/affection on")
    assert "读不到账号文件" in out, out
    assert not (sandbox / "acct-42.json").exists()
