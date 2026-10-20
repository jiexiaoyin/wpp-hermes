"""微信通道「只发结果，不发过程」（老板 2026-10-18 拍板）。

## 为什么要在适配器这层再拦一遍

Hermes 的网关会把大量**回合过程**当普通消息推给"没有状态栏"的平台：

* 流式中间稿 / 后台自审摘要 / 「⏳ Working — N min」心跳 / 卡住告警 / 关停提示
  —— 都带 ``_interim_send`` 标记（:func:`gateway.run._interim_metadata`）；
* 生命周期状态（🧠 记忆召回、上下文压缩、模型切换…）
  —— 走 ``adapter.send_or_update_status``，没有就退回普通 ``send``；
* 运营/告警通知 —— ``_deliver_platform_notice`` 在 ``notice_delivery: private``
  时走 ``send_private_notice``，基类默认仍是"发到原会话"。

微信**发出即不可撤回**，所以本文件钉死适配器侧的三道闸：过程消息丢弃、
状态回调吞掉、群通知改投管理员私聊；同时审批提示必须放行（否则回合卡死）。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def bare(monkeypatch, stubbed):
    """同 ``test_machinery_notice``：绕过 Base 构造，只装发送早退路径要的属性。"""
    from plugin.platform import adapter as adapter_mod

    class _SendResult:
        def __init__(self, success=True, error=None, error_kind=None,
                     message_id=None, **rest):
            self.success = success
            self.error = error
            self.error_kind = error_kind
            self.message_id = message_id
            for k, v in rest.items():
                setattr(self, k, v)

    monkeypatch.setattr(adapter_mod, "SendResult", _SendResult)
    inst = object.__new__(adapter_mod.WppAdapter)
    inst._accounts = {
        adapter_mod.DEFAULT_ACCOUNT: SimpleNamespace(
            id=adapter_mod.DEFAULT_ACCOUNT,
            self_wxid="wxid_self",
            nickname="示例小助理",
            admin_users=("wxid_owner_demo", "wxid_self"),
        ),
    }
    inst._clients = {}
    return inst


def test_interim_process_message_is_dropped(bare):
    """``_interim_send`` = 回合过程（中间稿/心跳/自审/关停提示），微信一律不发。"""
    result = asyncio.run(bare.send(
        "default:room_test@chatroom", "⏳ Working — 3 min",
        metadata={"_interim_send": True}))
    assert result.success is True
    assert result.message_id is None


def test_approval_prompt_is_not_dropped(bare):
    """审批提示虽带 ``_interim_send``，但它是必要交互；要放行到真实发送路径。"""
    result = asyncio.run(bare.send(
        "default:wxid_owner_demo", "允许执行 rm -rf /tmp/x 吗？",
        metadata={"_interim_send": True, "is_approval_prompt": True}))
    # 没连接 ⇒ 走到“账号未连接”，证明不是被过程闸提前吞掉。
    assert result.success is False
    assert "未连接" in (result.error or "")


def test_status_callback_is_a_noop(bare):
    """生命周期状态（记忆召回等）不应落到聊天：声明为无操作并返回成功。"""
    result = asyncio.run(bare.send_or_update_status(
        "default:room_test@chatroom", "lifecycle", "🧠 MemOS — recalled 9 memories"))
    assert result.success is True
    assert result.message_id is None


def test_group_notice_is_routed_to_admin_dm(bare, monkeypatch):
    """群里的运营通知改投管理员私聊，别刷到客户面前。"""
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id, content, reply_to=None, metadata=None):
        sent.append((chat_id, content))
        return SimpleNamespace(success=True, message_id="m1")

    monkeypatch.setattr(bare, "send", fake_send)
    result = asyncio.run(bare.send_private_notice(
        "default:room_test@chatroom", "someone", "⚠️ 网关告警"))
    assert result.success is True
    assert sent == [("default:wxid_owner_demo", "⚠️ 网关告警")]


def test_dm_notice_stays_in_place(bare, monkeypatch):
    """私聊本来就是安全面：通知维持原会话，不改投。"""
    sent: list[str] = []

    async def fake_send(chat_id, content, reply_to=None, metadata=None):
        sent.append(chat_id)
        return SimpleNamespace(success=True, message_id="m1")

    monkeypatch.setattr(bare, "send", fake_send)
    asyncio.run(bare.send_private_notice(
        "default:wxid_owner_demo", "wxid_owner_demo", "⚠️ 网关告警"))
    assert sent == ["default:wxid_owner_demo"]


def test_platform_registers_low_display_tier():
    """注册面必须声明 LOW 档：关掉工具进度/中间稿/长任务心跳/忙碌回执详情。"""
    src = (ROOT / "plugin/registration.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "register_platform":
            kw = {k.arg: k.value for k in node.keywords}
            tier = kw.get("display_tier")
            assert isinstance(tier, ast.Constant) and tier.value == "low", \
                f"register_platform display_tier={(ast.unparse(tier) if tier else None)!r}"
            return
    raise AssertionError("registration.py 里找不到 register_platform(...) 调用")
