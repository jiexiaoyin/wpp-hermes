"""不变量守卫（**Py 侧新增，TS 无对应文件**）。

计划里 I2 要求：「budget / profile / share **只能收紧不能放宽**」，
并明确「出口统一过 ``_assert_not_looser(before, after)``，内部 raise；随机化属性测试」。

三个模块（:mod:`.budget` / :mod:`.profile` / :mod:`.observe`）都要用同一条判据，
所以提到这里 —— 各写各的必漂移，而这条一旦漂移就是**静默放宽**：
09-26 心流自锁停摆 3 天的复盘里，"阈值/带宽只往松的方向走"正是特征之一。

⚠️ 判据的**落点**是"复合"那一步（:func:`.budget.tighten_budget` 一类的取更严一侧），
**不是**产生建议的那一步：:mod:`.observe` 的收紧档里，``maxPerHour`` 的 floor 允许
比 base 还大（``base=1`` ⇒ 档位 ``2``，见 ``test_observe.py`` 的边界用例）—— 那是**硬
边界**，不是放宽。把断言加在建议侧会把合法档位判成违规。

为什么不写注释而写成 raise：注释不会在回归时拦人。放进 :mod:`tests.invariants`。
"""

from __future__ import annotations

import math


class InvariantError(AssertionError):
    """不变量被破坏。继承 ``AssertionError``：它是**断言**，不是业务错误。"""


def _num(v) -> float | None:
    """把 ``v`` 变成有限数；非数/NaN/∞/bool ⇒ ``None``。"""
    if v is None or isinstance(v, bool):
        return None
    if not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def assert_not_looser(
    before,
    after,
    *,
    higher_is_tighter: tuple[str, ...] = (),
    lower_is_tighter: tuple[str, ...] = (),
    label: str = "",
) -> None:
    """**I2**：``after`` 不得比 ``before`` 松。违反 ⇒ :class:`InvariantError`。

    - ``higher_is_tighter``（如最小间隔）：``after < before`` ⇒ 放宽，raise。
    - ``lower_is_tighter``（如每小时上限）：``before`` 有限而 ``after`` 更大 ⇒ 放宽，raise。
      哨兵 ``0`` / ``None`` 表示"不设上限"⇒``before`` 是哨兵时**任何** ``after`` 都算收紧。

    两边都取不到数（键不存在）时跳过该键 —— 守卫只管"能比的"，不替调用方判缺省。
    """
    who = f"{label} " if label else ""
    for key in higher_is_tighter:
        b, a = _num(before.get(key)), _num(after.get(key))
        if b is None or a is None:
            continue
        if a < b:
            raise InvariantError(
                f"{who}{key}: 被放宽了（{b:g} → {a:g}）；预算/画像/占比只能收紧")
    for key in lower_is_tighter:
        b, a = _num(before.get(key)), _num(after.get(key))
        if b is None or a is None:
            continue
        if b <= 0:
            continue                      # before 是"不设上限"⇒ 收紧成有限值，允许
        if a <= 0 or a > b:
            raise InvariantError(
                f"{who}{key}: 被放宽了（{b:g} → {a:g}，0/None = 不设上限）；只能收紧")


# 曾有一个 ``assert_clamped``（I8 读取侧钳制的通用版）与 ``UNLIMITED`` 哨兵常量住在这里，
# 2026-10-01 删掉：**没有任何调用方**。I8 的读取侧钳制由
# :func:`.learn.clamp_hf_threshold_to_band` 实现并被 ``invariants`` 的 I8 用例正面钉住
# —— 同一个规则留两份实现，正是本模块 docstring 开头说的"各写各的必漂移"。
# 要再用请连同调用点一起加，别只把函数放回来。
