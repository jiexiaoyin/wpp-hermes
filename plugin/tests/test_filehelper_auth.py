"""filehelper 管理命令准入（``core/filehelper_auth.py`` + ``platform/adapter.py`` 的接线）。

## 这条线为什么值得单独钉

filehelper 是**管理面**：``/genpair`` 发配对码、改群白名单、开关主动插话。
它同时又是最容易**误判为已鉴权**的一条 —— 2026-10-02 审阅抓到旧实现两处都朝"放行"
漏（C3）：``adminUsers`` 未配时**放行**；判据写成 ``sender and sender not in admin``
时**空发件人短路放行**。而 vendor 在 filehelper 会话里恰恰常把 sender 标成机器人
自己或留空（见 ``adapter._handle_one`` ① 的豁免注释）—— 两条合起来 =
**"空发件人即管理员"**，且**日志一句错都没有**（那两处本来就只 warn 或不响）。

本文件守两段，与 ``test_access_policy.py`` 同款分法：

1. **纯逻辑**（``core/filehelper_auth.py``，本机可 import）—— 三条判词与两条边界。
2. **接线**（``platform/adapter.py``）—— 该文件模块级 ``from gateway.config import Platform``
   （本机**import 不了**），所以按 **AST 源码形状**验：判据在不在、是不是排在
   ``commands.handle_command`` **之前**、非 allow 分支有没有 ``return True`` 把命令挡下。
   变异套件 ``filehelper-auth`` / ``adapter-filehelper-auth``（``tools/mutation_check.py``）
   保证它们**拆掉就红**。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from plugin.core import filehelper_auth as FA

#: 仓根（``plugin/tests/test_filehelper_auth.py`` 往上两层）。
ROOT = Path(__file__).resolve().parents[2]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"


def _method(name: str) -> ast.AST:
    """``WppAdapter.<name>`` 的 AST 节点（找不到就炸，别让改名变假绿）。

    ⚠️ 必须吃 ``AsyncFunctionDef`` —— ``_maybe_command`` 是 async，
    只认 ``FunctionDef`` 的话这里会报"改名了"，而代码其实一个字没动。
    """
    src = (ROOT / ADAPTER_REL).read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLS:
            for fn in node.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == name:
                    return fn
    raise AssertionError(f"{ADAPTER_REL} 里找不到 {ADAPTER_CLS}.{name} —— 改名/搬家了？")


def _code(name: str) -> str:
    """函数体源码，**去掉 docstring**。

    去 docstring 不是洁癖：本文件的 docstring 里写满了"旧判据长什么样"的反例，
    拿整段源码搜字符串会**搜到注释里的旧形状** ⇒ 断言恒假红。
    """
    body = list(getattr(_method(name), "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


# ===== 1. 纯判据 =====


def test_no_admin_users_means_nobody_executes():
    """账号没配 ``adminUsers`` ⇒ ``no-admin``，**不是**放行。

    真机上的 ``accounts/live-test.json`` 就是 ``"adminUsers": []`` —— 旧实现在这种账号上
    对任何能往 filehelper 写字的人执行管理命令。
    """
    assert FA.verdict([], "q139198824") == FA.NO_ADMIN_CONFIGURED
    assert FA.verdict(None, "q139198824") == FA.NO_ADMIN_CONFIGURED
    assert FA.verdict((), "") == FA.NO_ADMIN_CONFIGURED


def test_blank_admin_entries_do_not_count_as_configuration():
    """只写了空白项 = 没配（否则 ``["  "]`` 会变成"配了但谁都不匹配"，看着像配好了）。"""
    assert FA.verdict(["", "   "], "q139198824") == FA.NO_ADMIN_CONFIGURED


def test_empty_sender_is_never_an_admin():
    """**这条就是 C3 的核心**：空发件人必须落到 ``not-admin``。

    旧判据 ``sender and sender not in admin`` 对空串短路 ⇒ 不进拒绝分支 ⇒ 照常执行。
    """
    assert FA.verdict(["q139198824"], "") == FA.NOT_ADMIN
    assert FA.verdict(["q139198824"], None) == FA.NOT_ADMIN
    assert FA.verdict(["q139198824"], "   ") == FA.NOT_ADMIN, "纯空白等同空"


def test_unknown_sender_is_rejected():
    assert FA.verdict(["q139198824"], "wxid_stranger") == FA.NOT_ADMIN


def test_configured_admin_is_allowed_with_surrounding_whitespace():
    assert FA.verdict(["q139198824"], "q139198824") == FA.ALLOW
    assert FA.verdict([" q139198824 "], "q139198824") == FA.ALLOW
    assert FA.verdict(["q139198824"], " q139198824 ") == FA.ALLOW


def test_the_bots_own_wxid_can_be_an_admin():
    """历史配置把**机器人自己的 wxid** 也写进 ``adminUsers``
    （``accounts/default.json`` = ``["q139198824", "wxid_eezdbu1ytws422"]``）——
    vendor 在 filehelper 会话里常把 sender 标成机器人自己，所以这条是**在用**的路径，
    不能因为"看着别扭"被清掉。
    """
    assert FA.verdict(["q139198824", "wxid_eezdbu1ytws422"], "wxid_eezdbu1ytws422") == FA.ALLOW


def test_verdicts_are_stable_strings():
    """判词是对外契约（日志/审计要能按词聚合），改名等于把历史告警线切断。"""
    assert (FA.ALLOW, FA.NO_ADMIN_CONFIGURED, FA.NOT_ADMIN) == ("allow", "no-admin", "not-admin")
    assert len({FA.ALLOW, FA.NO_ADMIN_CONFIGURED, FA.NOT_ADMIN}) == 3


def test_verdict_accepts_any_iterable_of_scalars():
    """配置来自 JSON：元组/列表/生成器都得吃（``accounts.py`` 存的是 ``tuple[str, ...]``）。"""
    assert FA.verdict(iter(["q139198824"]), "q139198824") == FA.ALLOW
    assert FA.verdict(("q139198824",), "q139198824") == FA.ALLOW


# ===== 2. 接线（AST 形状） =====


def _verdict_call() -> ast.Call:
    for n in ast.walk(_method("_maybe_command")):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "verdict":
            return n
    raise AssertionError("_maybe_command 里没有 filehelper_auth.verdict 调用")


def test_maybe_command_delegates_to_the_pure_verdict():
    code = _code("_maybe_command")
    assert "filehelper_auth.verdict(" in code, \
        "准入判据必须走 core/filehelper_auth（自带用例与变异门），不要在这里现写"


def test_the_gate_runs_before_the_command_is_executed():
    """判词必须排在实际执行**之前** —— 顺序反了就是"先执行后鉴权"，等于没鉴权。"""
    fn = _method("_maybe_command")
    verdict_line = _verdict_call().lineno
    exec_line = min(n.lineno for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "handle_command")
    assert verdict_line < exec_line, "先判后执行（顺序是这条修复的全部意义）"


def test_a_non_allow_verdict_returns_before_executing():
    """非 ``ALLOW`` 的分支必须在那行之前 ``return True``（**接管并终止**，不是继续往下跑）。"""
    fn = _method("_maybe_command")
    exec_line = min(n.lineno for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "handle_command")
    guards = [n for n in ast.walk(fn)
              if isinstance(n, ast.If) and n.lineno < exec_line
              and isinstance(n.test, ast.Compare)
              and any(isinstance(c, ast.Name) and c.id == "why" for c in ast.walk(n.test))]
    assert guards, "找不到对判词的分支 —— 判据被当成建议而不是闸"
    assert any(isinstance(s, ast.Return) and isinstance(s.value, ast.Constant)
               and s.value.value is True
               for g in guards for s in g.body), "非 allow 分支没有 return True（命令会继续执行）"


def test_the_gate_reads_the_accounts_own_admin_list():
    """adminUsers 必须取自**当前账号**（多账号各配各的），不能是全局/写死名单。"""
    call = _verdict_call()
    assert len(call.args) == 2
    first = call.args[0]
    assert isinstance(first, ast.Attribute) and first.attr == "admin_users", \
        f"判据第一个入参应是账号自己的 admin_users，实际是 {ast.dump(first)[:80]}"


def test_the_sender_comes_from_the_message_not_a_constant():
    call = _verdict_call()
    assert isinstance(call.args[1], ast.Attribute) and call.args[1].attr == "from_wxid"


def test_the_old_truthiness_shortcut_is_gone():
    """``sender and sender not in admin`` 这个形状不许再出现（它就是 C3 的第 2 条）。"""
    flat = " ".join(_code("_maybe_command").split())
    assert "sender and sender not in" not in flat
    assert "and sender not in admin" not in flat


@pytest.mark.parametrize("forbidden", [
    "⇒ 放行（管理面裸露）",       # 未配 adminUsers 时放行的那条日志
])
def test_the_fail_open_log_line_is_gone(forbidden: str):
    assert forbidden not in _code("_maybe_command"), \
        "fail-open 的日志连同它的行为一起删掉，别只改措辞"


def test_the_reply_log_does_not_claim_success_when_send_failed():
    """★ 2026-10-18 真机冒烟：出站闸拦下回复时 ``send`` 返回 ``success=False``（不抛），
    旧写法照样打「命令已回复」—— 命令没发出去，日志却说发了。

    本文件按 AST 验接线，这里同样只钉形状：必须拿到 ``send`` 的返回值、且成功/失败
    分两条日志。``send`` 内把 ``GuardError`` 收敛成 ``SendResult`` 的契约在
    ``adapter.send`` 的 ``except (ValueError, outbound.netguard.GuardError)``。
    """
    code = _code("_maybe_command")
    assert "= await self.send(" in code, "send 的返回值没接住 ⇒ 无法判断是否真的发出"
    assert "getattr(result, 'success', False)" in code
    assert "未回复" in code, "失败分支必须单独留一条日志（否则又变成假『已回复』）"
