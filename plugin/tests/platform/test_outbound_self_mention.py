"""出站 @self 闸（2026-10-02）—— 机器人**不许 @ 自己**。

## 现场（``.183`` live-test，2026-10-02 13:19，老板实测抓到）

群里有人 @ 了本账号，agent（qwen3.8-flash）把上下文里那条 ``@<本账号昵称>``
**原样抄进了回复**，于是账号在群里 @ 了自己：

    群成员：@<本账号昵称>\\u2005你好        ← 触发（group_mention）
    本账号：@<本账号昵称> 你好！我是 …      ← 回复里的 @ 指向**自己**

老板当场指出「回复时候 @ 的对象也不对」。判据很直白：那个名字正是
``accounts/*.json`` 里的 ``nickname``（账号自己的名字），而它该回的是**发消息的人**。

## 为什么闸在出站

``inbound/parser.py::normalize_mention`` 的 docstring 写的就是
「去掉内容里的 @bot 提及，**避免 AI 重复回 @**」—— 但它此前只在 ``prepare_text``
（**入站**喂给模型的那份正文）上生效。模型**抄回来的那一份**没人管，于是自家人 @ 自家人。

## 边界

- 只摘**指向自己**的 @（``@<selfWxid>`` / ``@<selfNickname>`` / ``<selfWxid>@``）。
  别人的 @ 一个字符都不动 —— 摘多了会改语义，比不摘更糟。
- 微信 @ 后面跟的是 U+2005（四分空格），Python 视其为空白 ⇒ ``strip()`` 一并吃掉。
- 适配器接线按 **AST 源码形状**验（``platform/adapter.py`` 本机 import 不了：
  模块级 ``from gateway.config import Platform`` 是 Hermes 独有），与本目录
  ``test_outbound_send_result.py`` 同一套路。
"""

from __future__ import annotations

import ast
from pathlib import Path

from plugin.platform import outbound as O

BOT = "wxid_bot"
NICK = "益融小助理"

#: 仓根（``plugin/tests/platform/...`` 往上三层）
ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"

#: 现场那条回复（去掉 @ 前缀后 47 字），逐字照抄。
INCIDENT_REPLY = (
    f"@{NICK} 你好！我是{NICK} 🤖 查销售/提成报表、员工日报、朋友圈、"
    "发消息都能干，有什么需要直接说～"
)


# ============================================================ 1. 判据本体

def test_the_incident_reply_loses_the_self_mention() -> None:
    """★ 现场原样复现：回复以 ``@<本账号昵称>`` 开头 ⇒ 摘掉后以正文开头。"""
    got = O.strip_self_mention(INCIDENT_REPLY, BOT, NICK)
    assert not got.startswith(f"@{NICK}"), f"还在 @ 自己：{got[:20]!r}"
    assert got.startswith("你好！我是"), got[:20]
    assert f"@{NICK}" not in got, "正文里也不许留（模型可能写在中间）"


def test_the_four_per_em_space_is_eaten() -> None:
    """微信 @ 后跟 U+2005 而不是普通空格 —— 不处理就会留下一个前导怪空格。"""
    assert O.strip_self_mention(f"@{NICK} 你好", BOT, NICK) == "你好"


def test_the_wxid_form_is_also_stripped() -> None:
    """两种 wxid 形态都要认（``@wxid`` 与 ``wxid@``）—— 与入站判据同一组。"""
    assert O.strip_self_mention(f"@{BOT} 你好", BOT, NICK) == "你好"
    assert O.strip_self_mention(f"{BOT}@你好", BOT, NICK) == "你好"


def test_a_mid_text_self_mention_also_goes() -> None:
    assert O.strip_self_mention(f"在吗 @{BOT} 帮忙看下", BOT, NICK) == "在吗  帮忙看下"


def test_another_persons_mention_survives() -> None:
    """★ 反向判据：别人的 @ **一个字符都不许动**（摘多了直接改语义）。"""
    for text in ("@wxid_other 你好", "@所有人 开会", "你好"):
        assert O.strip_self_mention(text, BOT, NICK) == text, text


def test_a_name_that_starts_with_ours_is_over_stripped_like_the_inbound_helper() -> None:
    """已知的过度剔除：``@<昵称>`` 走**裸子串替换**（继承 ``normalize_mention``）。

    ``@益融小助理x`` 会被当成"自己的提及"摘掉前缀。**故意保留**：换成带词界的
    ``\\b`` 反而会漏掉 ``@昵称`` 紧贴中文的形态（中文是 ``\\w``，没有词界 —— 见
    ``test_mentions.py`` §3 那条 A7 记录），而两个方向的代价不对称：
    漏掉一次 = 群里出现"自己 @ 自己"（一眼假、老板当场抓到）；多摘一次 = 昵称恰好
    以本账号昵称为前缀的**第三方**少了个 @（本账号昵称是尾缀时不受影响）。
    钉住它，免得后来者当 bug"修"。
    """
    assert O.strip_self_mention(f"@{NICK}x 你好", BOT, NICK) == "x 你好"
    assert O.strip_self_mention(f"@{NICK}2 你好", BOT, NICK) == "2 你好"


def test_empty_and_missing_config_do_not_break() -> None:
    assert O.strip_self_mention("", BOT, NICK) == ""
    assert O.strip_self_mention("你好", "", "") == "你好"
    assert f"@{NICK}" not in O.strip_self_mention(f"@{NICK} 你好", "", NICK)


# ============================================================ 2. 适配器接线（AST）


def _module() -> ast.Module:
    return ast.parse((ROOT / ADAPTER_REL).read_text(encoding="utf-8"))


def _code(node: ast.AST) -> str:
    """函数体源码（**去 docstring** —— docstring 里写满反例，搜不得）。"""
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


def _adapter_method(name: str) -> ast.AST:
    for node in _module().body:
        if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLS:
            for n in node.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                    return n
    raise AssertionError(f"{ADAPTER_REL} 里找不到 {ADAPTER_CLS}.{name}")


def _module_function(name: str) -> ast.AST:
    for node in _module().body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{ADAPTER_REL} 里找不到模块级函数 {name}")


def test_live_send_actually_calls_the_guard() -> None:
    """★ 接线（"0 与没接上分不开"）：函数写得再对，不在路上就等于没有。

    真机那条回复走的就是 ``WppAdapter.send``。
    """
    code = _code(_adapter_method("send"))
    assert "strip_self_mention" in code, "live send 没接闸 —— 现场那条还会 @ 自己"
    assert "acct.nickname" in code and "acct.self_wxid" in code, "判据要拿账号自己的身份"
    # 算了不写回 = 没算（这一条是变异套件 `adapter-outbound-self-mention` 逼出来的：
    # 只断言"调用了"会漏掉"调完把结果扔了"）。
    assert "content = cleaned" in code, "摘完必须写回 content（分片/记账读的是它）"


def test_the_cron_path_calls_it_too() -> None:
    """进程外（cron / standalone）是**另一条出口**，不许绕过。"""
    assert "strip_self_mention" in _code(_module_function("_standalone_send"))
