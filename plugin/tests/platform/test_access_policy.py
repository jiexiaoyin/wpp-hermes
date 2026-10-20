"""准入策略上报回归（``core/access_policy.py`` + ``platform/adapter.py`` 的接线）。

## 这条线为什么值得单测

Hermes 网关在插件**之上**还有一道用户授权，**没有环境白名单时默认全拒**。
2026-10-01 真机实测：心流 judge 打了 0.775、台账落了 ``status='judged'``，
回复却被网关拦下，现场只留一行 ``WARNING gateway.run: Unauthorized user: <wxid>``。
插件侧日志（``触发（…）``、``[WPP HF] trigger: …``）一切正常 —— 这是**最贵的一类故障**：
从插件视角看什么都对。

修法就是让适配器声明 ``enforces_own_access_policy`` 并把已有的策略上报给网关
（口径与理由写在 ``core/access_policy.py`` 模块 docstring）。本文件守两段：

1. **纯逻辑**（``core/access_policy.py``，本机可 import）—— 映射表、聚合口径、
   fail-closed 兜底。
2. **接线**（``platform/adapter.py``）—— 该文件模块级 ``from gateway.config import Platform``
   （Hermes 独有本机 **import 不了**），所以这一段按 **AST 源码形状**验：
   属性在不在、是不是 ``@property``、委托给哪个函数、拿的是哪一份账号集。
   变异套件 ``adapter-access-policy`` / ``access-policy``（``tools/mutation_check.py``）
   保证它们**拆掉就红**。

## 不覆盖的边界（别误判）

- **网关那一侧的判据**（``authz_mixin.py`` 怎么用这几个属性）不在本仓，
  由 ``真机`` 上的真机复测覆盖。
- ``_is_group_allowed`` **故意不实现**：中心授权不调它（只有 whatsapp/qqbot 等在
  自己适配器内部用），群 ID 的过滤仍由 ``inbound/triggers.py`` ⑥ 的
  ``groupAllowFrom`` 逐账号把守。见 ``adapter.py`` 那段注释。
- 本文件**不**读 ``plugin/accounts/default.json``（含 authcode，见 .gitignore 红线）：
  真账号那两条用例把 ``WECHATPRO_ACCOUNTS_DIR`` 指到 tmp_path。
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from plugin.core import access_policy as AP
from plugin.core import accounts as A
from plugin.core import overrides as OV

#: 仓根（``plugin/tests/platform/test_access_policy.py`` 往上三层）。
ROOT = Path(__file__).resolve().parents[3]

#: 接线被测文件（**只读源码**，不 import —— 见模块 docstring）。
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _adapter_methods() -> dict[str, ast.AST]:
    """``WppAdapter`` 的方法节点表（找不到类/方法就炸，别让改名变成假绿）。"""
    for node in ast.parse(_src(ADAPTER_REL)).body:
        if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLS:
            return {n.name: n for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    raise AssertionError(f"{ADAPTER_REL} 里找不到 class {ADAPTER_CLS}")


def _method(name: str) -> ast.AST:
    got = _adapter_methods().get(name)
    if got is None:
        raise AssertionError(f"{ADAPTER_CLS} 少了 {name!r} —— 网关那一侧会直接 default-deny")
    return got


def _code(node: ast.AST) -> str:
    """函数体源码（**去 docstring**）—— docstring 里写满了反例，搜不得。"""
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


def _is_property(node: ast.AST) -> bool:
    return any(isinstance(d, ast.Name) and d.id == "property"
               for d in getattr(node, "decorator_list", ()))


def _delegates_to(node: ast.AST, attr: str) -> bool:
    """函数体里有没有 ``access_policy.<attr>(...)``。"""
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            v = n.func.value
            if isinstance(v, ast.Name) and v.id == "access_policy" and n.func.attr == attr:
                return True
    return False


# ============================================================ 假账号（纯逻辑用）

@dataclass
class _Fake:
    """字段名**故意与** ``core.accounts.Account`` 同名 —— 假对象与真对象共用一套读取口径。"""

    id: str = "a"
    group_policy: str = "open"
    allow_from: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)


# ============================================================ 1. 群策略

def test_group_policy_maps_the_plugin_vocabulary():
    """插件侧的 ``allowlist`` 必须原样上报；网关**只认**这一个值。"""
    assert AP.group_policy([_Fake(group_policy="allowlist")]) == AP.ALLOWLIST


def test_disabled_and_closed_both_report_disabled():
    """``closed`` 是插件自己的词，网关不认识 ⇒ 并入 ``disabled``。"""
    assert AP.group_policy([_Fake(group_policy="disabled")]) == AP.DISABLED
    assert AP.group_policy([_Fake(group_policy="closed")]) == AP.DISABLED
    assert AP.group_policy([_Fake(group_policy="CLOSED")]) == AP.DISABLED  # 大小写不敏感


def test_unknown_group_policy_is_open_never_allowlist():
    """★ 未知值**不许**猜成 ``allowlist``：那是拿默认值去替操作者开门。

    ``open`` 会被网关 default-deny（SECURITY.md §2.6 反 fail-open），
    所以"猜错"的方向必须是**更严**而不是更松。
    """
    assert AP.group_policy([_Fake(group_policy="whatever")]) == AP.OPEN
    assert AP.group_policy([_Fake(group_policy="")]) == AP.OPEN


def test_group_policy_any_allowlist_wins():
    """多账号聚合：任一 ``allowlist`` ⇒ 报 ``allowlist``（理由见模块 docstring）。

    这不是放宽：网关对群**不查群 ID**，真实过滤在插件 ⑥ 的 ``groupAllowFrom``。
    """
    accounts = [_Fake(id="a", group_policy="open"), _Fake(id="b", group_policy="allowlist")]
    assert AP.group_policy(accounts) == AP.ALLOWLIST


def test_group_policy_empty_set_is_disabled():
    """★ 一个账号都没连上 ⇒ ``disabled``（fail-closed），不许开门。"""
    assert AP.group_policy([]) == AP.DISABLED


def test_group_policy_all_open_stays_open():
    """全 ``open`` ⇒ ``open``（**不是** ``allowlist``）—— 别顺手"提权"上报。"""
    assert AP.group_policy([_Fake(group_policy="open"), _Fake(group_policy="open")]) == AP.OPEN


# ============================================================ 2. 私聊策略

def test_dm_allow_all_requires_literal_true():
    """★ ``allowAllUsers`` 只认**布尔** ``True`` —— 字符串 ``"true"`` 不算。

    这是与 ``inbound/triggers.py::_decide_dm`` 逐条对齐的口径（原实现如此），
    放宽成真值判断就等于替配置错误开门。
    """
    assert AP.dm_policy([_Fake(raw={"allowAllUsers": True})]) == AP.OPEN
    assert AP.dm_policy([_Fake(raw={"allowAllUsers": "true"})]) == AP.DISABLED
    assert AP.dm_policy([_Fake(raw={"allowAllUsers": 1})]) == AP.DISABLED


def test_dm_allowlist_only_when_a_sender_is_listed():
    assert AP.dm_policy([_Fake(allow_from=("wxid_x",))]) == AP.ALLOWLIST


def test_dm_empty_is_disabled():
    """空名单 = ``disabled``，与改造前"私聊一律不放行"的行为一致。"""
    assert AP.dm_policy([_Fake()]) == AP.DISABLED
    assert AP.dm_policy([_Fake(allow_from=("  ",))]) == AP.DISABLED
    assert AP.dm_policy([]) == AP.DISABLED


def test_dm_open_wins_over_allowlist():
    """任一账号开了 ``allowAllUsers`` ⇒ 报 ``open``（网关据此 default-deny，不会误放）。"""
    accounts = [_Fake(id="a", allow_from=("wxid_x",)), _Fake(id="b", raw={"allowAllUsers": True})]
    assert AP.dm_policy(accounts) == AP.OPEN


def test_dm_sender_check_is_fail_closed():
    """★ 名单是空的 ⇒ 谁都不放行（包括空串/None）。"""
    acct = _Fake(allow_from=("wxid_x",))
    assert AP.is_dm_allowed([acct], "wxid_x") is True
    assert AP.is_dm_allowed([acct], "wxid_other") is False
    assert AP.is_dm_allowed([acct], "") is False
    assert AP.is_dm_allowed([acct], None) is False
    assert AP.is_dm_allowed([_Fake()], "wxid_x") is False  # 空名单


def test_dm_senders_are_the_union_and_trimmed():
    """多账号取并集（网关只给发件人、不给账号，见模块 docstring），并去掉空白。"""
    accounts = [_Fake(id="a", allow_from=("wxid_x", "  wxid_y  ")), _Fake(id="b", allow_from=("wxid_z",))]
    assert AP.allowed_dm_senders(accounts) == frozenset({"wxid_x", "wxid_y", "wxid_z"})


# ============================================================ 3. 只上报已连接账号

def test_reportable_keeps_only_connected_and_sorts():
    """★ ``default.json`` 那种连不上的账号**不许**被上报。

    它的 ``groupAllowFrom`` 里是**真实生产群 id** —— 报上去等于替一个
    根本不存在的连接开门。排序是为了让日志/单测有确定性。
    """
    accounts = [_Fake(id="b"), _Fake(id="a"), _Fake(id="prod-shaped")]
    assert [a.id for a in AP.reportable(accounts, {"b", "a"})] == ["a", "b"]
    assert [a.id for a in AP.reportable(accounts, {"a"})] == ["a"]
    assert AP.reportable(accounts, set()) == ()
    # 比较前先 str() 化：connected_ids 来自 keys()，不保证是 str
    assert [a.id for a in AP.reportable(accounts, {b"nope"})] == []


# ============================================================ 4. 与真 Account 的字段契约

@pytest.fixture
def _isolated_accounts_dir(tmp_path, monkeypatch):
    """把账号目录指到 tmp_path —— 绝不碰 ``plugin/accounts/default.json``（含 authcode）。

    ``overrides._runtime_overrides`` 是模块级可变状态且不在 root conftest 的重置
    名单里 ⇒ 与 ``tests/test_accounts.py`` 采取同一套兜底。
    """
    d = tmp_path / "accounts"
    d.mkdir()
    monkeypatch.setenv("WECHATPRO_ACCOUNTS_DIR", str(d))
    monkeypatch.delenv("WECHATPRO_ACCOUNTS_JSON", raising=False)
    OV._runtime_overrides.clear()
    yield d
    OV._runtime_overrides.clear()


def test_real_account_fields_line_up(_isolated_accounts_dir):
    """★ 契约：``access_policy`` 读的 ``group_policy`` / ``allow_from`` / ``raw``
    必须真的存在（``accounts.py`` 改字段名 ⇒ 这条红）。

    用**真** ``Account`` 而不是假对象跑一遍，就是防"假对象永远绿"。
    """
    cfg = {
        "id": "live-test",
        "agent": "wpp-wechat-livetest",
        "selfWxid": "wxid_owner_demo",
        "groupPolicy": "allowlist",
        "groupAllowFrom": ["123456789@chatroom"],
        "allowFrom": ["wxid_bot_demo"],
    }
    (_isolated_accounts_dir / "live-test.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    acct = A.resolve("live-test")

    assert AP.group_policy([acct]) == AP.ALLOWLIST
    assert AP.dm_policy([acct]) == AP.ALLOWLIST
    assert AP.is_dm_allowed([acct], "wxid_bot_demo") is True
    assert AP.is_dm_allowed([acct], "wxid_stranger") is False


def test_real_account_defaults_stay_closed(_isolated_accounts_dir):
    """★ ``resolve`` 的**默认值**即规格：没配准入策略的账号报出来是 ``open``（群）+
    ``disabled``（私聊）—— 私聊默认关死，群交回网关 default-deny。"""
    (_isolated_accounts_dir / "bare.json").write_text(
        json.dumps({"id": "bare", "agent": "wpp-wechat"}, ensure_ascii=False), encoding="utf-8")
    acct = A.resolve("bare")

    assert AP.group_policy([acct]) == AP.OPEN
    assert AP.dm_policy([acct]) == AP.DISABLED
    assert AP.is_dm_allowed([acct], "wxid_anyone") is False


# ============================================================ 5. 适配器接线（AST 源码形状）

def test_adapter_declares_it_enforces_its_own_policy():
    """★ 没有这个开关，网关**根本不会回头问**下面几个属性 ⇒ 全部 default-deny。

    必须是 ``@property`` 且返回**字面量** ``True``：中心授权是
    ``getattr(adapter, attr)`` 取值 —— 退化成方法对象仍然 truthy，
    故障会从"拒所有消息"变成更难查的形状。
    """
    node = _method("enforces_own_access_policy")
    assert _is_property(node), "enforces_own_access_policy 必须是 @property"
    ret = node.body[-1]
    assert isinstance(ret, ast.Return) and isinstance(ret.value, ast.Constant) \
        and ret.value.value is True, "必须显式 return True"


def test_adapter_gateway_accounts_only_connected():
    """★ 上报集必须是 ``_clients``（已连接），不是 ``_accounts``（全量，含 default.json）。"""
    node = _method("_gateway_accounts")
    assert _is_property(node)
    assert _delegates_to(node, "reportable")
    code = _code(node)
    assert "self._clients" in code, "必须按 _clients 过滤已连接账号"
    assert "self._accounts" in code, "仍应把全量账号交给 reportable 去筛"


def test_adapter_group_policy_delegates():
    node = _method("_group_policy")
    assert _is_property(node), "_group_policy 必须是 @property（网关按属性取值）"
    assert _delegates_to(node, "group_policy")
    assert "_gateway_accounts" in _code(node), "必须传已连接账号集"


def test_adapter_dm_policy_delegates():
    node = _method("_dm_policy")
    assert _is_property(node), "_dm_policy 必须是 @property"
    assert _delegates_to(node, "dm_policy")
    assert "_gateway_accounts" in _code(node)


def test_adapter_is_dm_allowed_delegates_sender_id():
    """``_is_dm_allowed`` 是**方法不是属性** —— 网关要传发件人 id 进来。"""
    node = _method("_is_dm_allowed")
    assert not _is_property(node), "_is_dm_allowed 收 sender_id，不能是 @property"
    assert _delegates_to(node, "is_dm_allowed")
    code = _code(node)
    assert "sender_id" in code and "_gateway_accounts" in code


def test_adapter_does_not_fake_open_as_allowlist():
    """★ 反 fail-open：适配器里不许出现把 ``open`` 硬报成 ``allowlist`` 的写法。

    看的是 **AST 里的字符串常量**而不是原文 —— 注释/docstring 里写 ``"allowlist"``
    讲道理是允许的（而且正该允许），把口径写死在代码里才是问题。
    """
    lits = {n.value for n in ast.walk(ast.parse(_src(ADAPTER_REL)))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "allowlist" not in lits, "适配器不该自己写死 allowlist（口径在 access_policy）"


def test_adapter_imports_the_shared_module():
    """接线的前提：真的 import 了 ``access_policy``（不是同名局部函数）。"""
    tree = ast.parse(_src(ADAPTER_REL))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("core") \
                and any(a.name == "access_policy" for a in node.names):
            return
    raise AssertionError("adapter.py 没有 from ..core import access_policy")
