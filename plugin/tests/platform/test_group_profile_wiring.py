"""群画像 → 回复侧 ``channel_prompt`` 的装配门（2026-10-18）。

## 为什么单独一套门

``adapter.py`` 本机 import 不了（模块级 ``from gateway.config import Platform`` 是
Hermes 独有），所以只能按 **AST 源码形状**验。这个功能有三种"看着完全正常"的失效：

1. **只接了 judge、没接回复侧**：老板问「群画像怎么完善」时看到的正是这个 ——
   画像在决定"要不要说"，回复正文却还是通用人设。
2. **守卫写在 IO 之后**：非群消息 / 心流关着照样取画像，热路径每条消息多一次缓存查
   （更糟的是将来有人把缓存换成 DB 读，就变成每条消息一次 DB）。
3. **把 mood 那条缝覆盖掉**：``channel_prompt`` 只收一个字符串，新功能如果直接
   ``channel_prompt=profile`` 就会让 W2 的情绪注入静默消失。

判据都下在源码形状上；画像文本本身的拼接与空值语义由
``plugin/tests/heartflow/test_profile.py`` 的纯函数用例钉住。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"


def _src() -> str:
    return (ROOT / ADAPTER_REL).read_text(encoding="utf-8")


def _cls_node() -> ast.ClassDef:
    for node in ast.parse(_src()).body:
        if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLS:
            return node
    raise AssertionError(f"{ADAPTER_REL} 里找不到 class {ADAPTER_CLS}")


def _methods() -> dict[str, ast.AST]:
    return {n.name: n for n in _cls_node().body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _code(name: str) -> str:
    """去掉 docstring 的源码（docstring 里写满了反例，搜不得）。"""
    body = list(_methods()[name].body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return "\n".join(ast.unparse(s) for s in body)


# ============================================================ 1. 接在正确的缝上

def test_build_event_passes_the_combined_channel_prompt():
    code = _code("_build_event")
    assert "channel_prompt=self._channel_prompt(acct, msg)" in code
    assert "channel_prompt=self._mood_channel_prompt(" not in code, \
        "直接传 mood 会把画像挡在回复侧之外（channel_prompt 只收一个字符串）"


def test_channel_prompt_keeps_both_mood_and_profile():
    code = _code("_channel_prompt")
    assert "_mood_channel_prompt" in code, "W2 情绪注入不许被新功能覆盖"
    assert "_group_profile_channel_prompt" in code
    assert '"\\n\\n".join(parts)' in code or "'\\n\\n'.join(parts)" in code, \
        "两段要合成一个字符串"
    assert "if parts else None" in code, \
        "两段都空 ⇒ None（与没开功能时请求体逐字节相同）"


# ============================================================ 2. 三道闸都在 IO 之前

def test_profile_lookup_is_gated_by_group_and_heartflow_enabled():
    code = _code("_group_profile_channel_prompt")
    io_at = code.find("get_hf_profile_prompt_text")
    assert io_at >= 0, "画像文本必须从缓存取"
    assert code.find("is_group") >= 0 and code.find("is_group") < io_at, \
        "非群消息必须在取画像之前返回"
    assert "enabled" in code and code.find("enabled") < io_at, \
        "心流关着必须在取画像之前返回"
    assert "repo_hf" not in code and "list_profiles" not in code, \
        "入站热路径不许查 DB —— 缓存由 sweep / 启动预热负责"


def test_profile_injection_failure_never_blocks_the_message():
    code = _code("_group_profile_channel_prompt")
    assert "except Exception" in code, "画像注入失败必须降级成 None（不能挡住消息）"
    assert "return None" in code
