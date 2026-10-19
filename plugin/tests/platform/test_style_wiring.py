"""风格学习装配回归（2026-10-19）。

``adapter.py`` 本机 import 不了（模块级 ``from gateway.config import Platform``），
所以按 AST 源码形状验三件"接错了也不会报错"的事：

1. **注入守卫在缓存读之前** —— 非群/开关关着时不该碰风格缓存；
2. **风格 sweep 独立于心流** —— heartflow import 失败不能把风格一起带走；
3. **配置只有一个家** —— ``style`` 独有的键只许在 ``style_learn.py`` 里解析。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ADAPTER_REL = "plugin/platform/adapter.py"
ADAPTER_CLS = "WppAdapter"

#: style 独有的配置键（adapter 只许用解析后的属性，不许出现这些字符串）
STYLE_ONLY_KEYS = (
    "lookbackDays", "minMsgs", "minSenders", "sampleMsgs",
    "refreshHours", "maxPerRun", "maxPromptChars", "minConfidence",
    "whitelistGroups",
)


def _src(rel: str = ADAPTER_REL) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _cls() -> ast.ClassDef:
    for node in ast.parse(_src()).body:
        if isinstance(node, ast.ClassDef) and node.name == ADAPTER_CLS:
            return node
    raise AssertionError(f"{ADAPTER_REL} 里找不到 {ADAPTER_CLS}")


def _methods() -> dict[str, ast.AST]:
    return {n.name: n for n in _cls().body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr == name) \
                    or (isinstance(f, ast.Name) and f.id == name):
                out.append(n)
    return out


def _if(node: ast.AST, test: str) -> ast.If:
    for n in ast.walk(node):
        if isinstance(n, ast.If) and ast.unparse(n.test) == test:
            return n
    raise AssertionError(f"找不到 `if {test}:`")


def _is_inside(inner: ast.AST, outer: ast.AST) -> bool:
    return any(n is inner for n in ast.walk(outer))


# ============================================================ 回复侧注入


def test_style_injection_guards_sit_before_the_cache_read() -> None:
    fn = _methods()["_style_channel_prompt"]
    is_group = _if(fn, "not getattr(msg, 'is_group', False)")
    enabled = _if(fn, "not cfg.enabled")
    build = _calls(fn, "build_style_channel_prompt")
    assert len(build) == 1
    assert _is_inside(build[0], is_group) is False  # build 在 guard 之后，不在其子树里
    assert build[0].lineno > is_group.lineno
    assert build[0].lineno > enabled.lineno


def test_channel_prompt_appends_style_after_the_other_parts() -> None:
    fn = _methods()["_channel_prompt"]
    lines = {
        "mood": min(c.lineno for c in _calls(fn, "_mood_channel_prompt")),
        "profile": min(c.lineno for c in _calls(fn, "_group_profile_channel_prompt")),
        "jargon": min(c.lineno for c in _calls(fn, "_jargon_channel_prompt")),
        "style": min(c.lineno for c in _calls(fn, "_style_channel_prompt")),
    }
    assert lines["mood"] < lines["profile"] < lines["jargon"] < lines["style"]


# ============================================================ sweep


def test_style_sweep_is_started_independently_of_heartflow() -> None:
    """两个 import 必须各自一个 ``try``：心流模块坏掉时风格 sweep 仍要起来。"""
    fn = _methods()["_start_sweeps"]
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert len(tries) >= 2
    # 有且仅有一个 try 里出现 style_learn（另一个是 heartflow）
    style_tries = [n for n in tries if "style_learn" in ast.unparse(n)]
    hf_tries = [n for n in tries if "heartflow" in ast.unparse(n)]
    assert len(style_tries) == 1, "style_learn 必须只在一个 try 里 import"
    assert len(hf_tries) == 1, "heartflow 必须只在一个 try 里 import"
    assert style_tries[0] is not hf_tries[0]


def test_style_preheat_and_sweep_only_run_when_module_is_available() -> None:
    fn = _methods()["_start_sweeps"]
    guard = _if(fn, "style_mod is not None")
    preheat = _calls(fn, "preheat_account")
    sweep = _calls(fn, "sweep_loop")
    guarded_preheat = [c for c in preheat if _is_inside(c, guard)]
    assert guarded_preheat, "style preheat 必须在 style_mod 可用时执行"
    assert any("style_mod.preheat_account" in ast.unparse(c) for c in guarded_preheat)
    style_sweeps = [c for c in sweep if _is_inside(c, guard)]
    assert style_sweeps, "style sweep 必须在 style_mod 可用时启动"
    # 任务名写在包裹它的 asyncio.create_task(...) 上
    creates = [c for c in _calls(fn, "create_task") if _is_inside(c, guard)]
    assert any(
        "style-sweep" in ast.unparse(c) and "style_mod.sweep_loop" in ast.unparse(c)
        for c in creates
    )


# ============================================================ 配置单一来源


def test_style_keys_are_not_parsed_a_second_time_in_adapter() -> None:
    src = _src()
    for key in STYLE_ONLY_KEYS:
        assert f'"{key}"' not in src, f"adapter 里出现了 style 键 {key} 的字面量"
        assert f"'{key}'" not in src, f"adapter 里出现了 style 键 {key} 的字面量"


def test_disabled_style_returns_none_without_reading_the_cache() -> None:
    """源码顺序：``not cfg.enabled`` 的 return 必须在 ``build_style_channel_prompt`` 之前。"""
    fn = _methods()["_style_channel_prompt"]
    enabled = _if(fn, "not cfg.enabled")
    build_line = min(c.lineno for c in _calls(fn, "build_style_channel_prompt"))
    assert enabled.lineno < build_line
    # 该 if 体内必须 return None
    assert any(isinstance(n, ast.Return) for n in enabled.body)
