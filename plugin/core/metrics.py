"""计数器 / 仪表盘 —— 移植自 TS ``src/monitor/metrics.ts`` 的核心部分。

## 相对旧 ``plugin/metrics.py`` 砍掉了什么（计划里写明的"只留被引用的"）

旧模块有 ``flush()`` + ``_percentile()`` + ``_by_peer`` + 写 ``/tmp/wpp-metrics.log``
一整套，**但全树只有一个调用点**（``adapter`` 里 ``incr("inbound_msg")``）。
那套"每 60s 往 /tmp 追加一行 JSON"没有任何消费方，且多进程下互相踩文件。
⇒ 本模块只保留**计数/仪表盘**这一层，导出走 :func:`render_prometheus`（有消费方时再接）。

## 两个"预声明"为什么必须存在（TS v1.9.2 的教训，照搬）

未自增过的指标不会出现在导出里 ⇒ **"值为 0" 与 "埋点根本没接上/进程没走到"无法区分**，
而后者恰恰是最该报警的情形。所以进了 :data:`PRE_DECLARED_COUNTERS` 的指标从进程启动起
就是 0，而不是"不存在"。

心流的有效性指标尤其依赖这个区分（v1.10.0 复盘：09-26 心流**彻底停摆 3 天**无人
发现，因为埋点只覆盖了"judge 有没有在跑"，覆盖不到"跑了但一条都发不出去"）。

## 线程安全

WS 收 / webhook 收 / 300s sweep **可能在不同线程**（见计划第 6 步），所以计数器读写
一律走 ``RLock``。旧实现有锁，这里保留 —— 不是保险起见，是真会被并发碰。
"""

from __future__ import annotations

import threading

#: 指标名前缀（对齐 TS 的 ``ns = "wpp"``，导出的名字形如 ``wpp_judge_calls_total``）
NS = "wpp"

_lock = threading.RLock()
_counters: dict[str, int] = {}
#: 仪表盘**保留浮点**（见 :func:`set_gauge`）—— 心流的阈值类指标是 0.5–0.9 的小数。
_gauges: dict[str, float] = {}


def _key(name: str) -> str:
    return name if name.startswith(f"{NS}_") else f"{NS}_{name}"


# --------------------------------------------------------------------- counter


def inc_counter(name: str, n: int = 1) -> None:
    """计数器 +n（默认 +1）。"""
    k = _key(name)
    with _lock:
        _counters[k] = _counters.get(k, 0) + n


def declare_counter(name: str) -> None:
    """预声明为 0（幂等）—— 见模块 docstring，这是"0 与没接上"的分界线。"""
    k = _key(name)
    with _lock:
        _counters.setdefault(k, 0)


def get_counter(name: str) -> int:
    """读计数器（**未预声明过 ⇒ 0**，与 TS 一致）。"""
    with _lock:
        return _counters.get(_key(name), 0)


# ----------------------------------------------------------------------- gauge


def set_gauge(name: str, value: float) -> None:
    """设仪表盘为绝对值（瞬时量，如"距上次真发言多少秒"）。

    ⚠️ **存原值，不做 ``int()``** —— 心流有两个阈值类指标（``hf_effective_threshold_max``
    / 天花板群数之外的同类）量的是 0.5–0.9 的小数，取整后全体变成 **0**：
    "阈值自锁"这个 v1.10.0 才补上的核心金丝雀就会**永远看起来正常**，
    而它存在的全部理由正是"别再有一次看起来正常的停摆"。
    """
    k = _key(name)
    with _lock:
        _gauges[k] = float(value)


def declare_gauge(name: str) -> None:
    """预声明 gauge 为 0（幂等）。理由同 :func:`declare_counter`。"""
    k = _key(name)
    with _lock:
        _gauges.setdefault(k, 0.0)


def get_gauge(name: str) -> float:
    with _lock:
        return _gauges.get(_key(name), 0.0)


# ----------------------------------------------------------------- 测试 / 导出


def reset_all() -> None:
    """清空全部指标 **并把预声明的重新种回去**（测试 teardown 用）。

    ⚠️ 不是 ``clear()``：清空后预声明的那批就"不存在"了，正好破坏本模块存在的理由。
    """
    with _lock:
        _counters.clear()
        _gauges.clear()
        _declare_all()


def snapshot() -> dict[str, dict[str, float]]:
    """全部指标的浅拷贝（测试 / health check 用）。"""
    with _lock:
        return {"counters": dict(_counters), "gauges": dict(_gauges)}


def _fmt_num(v: float) -> str:
    """对齐 JS ``String(number)``：整数值不打印 ``.0``（``5`` 而不是 ``5.0``）。"""
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def render_prometheus() -> str:
    """Prometheus 0.0.4 text format。"""
    with _lock:
        lines: list[str] = []
        for k, v in _counters.items():
            # ⚠️ TYPE 行不放 ``_total`` 后缀（key 里已经带完整前缀，别再去拆一次再拼 ——
            #    TS 那边因为这个 bug 产出过双重前缀 ``wpp_wpp_...`` 且样本值恒 0）
            lines.append(f"# TYPE {k.removesuffix('_total')} counter")
            lines.append(f"{k} {_fmt_num(v)}")
        for k, v in _gauges.items():
            lines.append(f"# TYPE {k} gauge")
            lines.append(f"{k} {_fmt_num(v)}")
        return "".join(ln + "\n" for ln in lines)


# ------------------------------------------------------- 具名指标（有调用方的）


def inc_judge_call() -> None:
    """``call_judge`` 被调用（四机制公共入口，计在这里最省埋点且不会漏）。"""
    inc_counter(COUNTER_JUDGE_CALLS)


def inc_judge_failure() -> None:
    """``call_judge`` 抛错（无 apiKey / HTTP 非 2xx / 空正文 / 超时）。"""
    inc_counter(COUNTER_JUDGE_FAILURES)


COUNTER_JUDGE_CALLS = "judge_calls_total"
COUNTER_JUDGE_FAILURES = "judge_failures_total"


# --------------------------------------------------- 心流（learn / heartflow 埋点）

#: 真发出（**不含**被 dedup/预算/重复闸拦下的占位符）—— ``classify_hf_send`` 判成 sent 才 +1。
COUNTER_HF_SENDS = "hf_sends_total"
#: 可达性护栏生效次数。**非 0 即说明曾出现"阈值够不着"的自锁**。
COUNTER_HF_REACH_CAP_APPLIED = "hf_reach_cap_applied_total"
#: judge 判定通过 / 不过（#9 ``heartflow.py`` 埋点；先在这里登记，见模块 docstring 的"0 与没接上"）
COUNTER_HF_JUDGE_PASSED = "hf_judge_passed_total"
COUNTER_HF_JUDGE_BELOW = "hf_judge_below_total"

#: 距上次真发言的秒数（**心流停摆的核心金丝雀**）。从未发过 ⇒ ``-1``（``0`` 会被误读成"刚发过"）。
GAUGE_HF_LAST_SEND_AGE_SEC = "hf_last_send_age_sec"
#: 各群生效阈值中的最大值（与上一个一起看：高阈值 + 高年龄 = 典型的阈值自锁）
GAUGE_HF_EFFECTIVE_THRESHOLD_MAX = "hf_effective_threshold_max"
#: 被可达性天花板压住的群数
GAUGE_HF_THRESHOLD_CEILING_GROUPS = "hf_threshold_ceiling_groups"
#: 本轮有判定样本、且阈值可达（最高分够得着）的群数
GAUGE_HF_REACHABLE_GROUPS = "hf_reachable_groups"


def inc_hf_sent() -> None:
    inc_counter(COUNTER_HF_SENDS)


def inc_hf_reach_cap_applied() -> None:
    inc_counter(COUNTER_HF_REACH_CAP_APPLIED)


def inc_hf_judge_passed() -> None:
    inc_counter(COUNTER_HF_JUDGE_PASSED)


def inc_hf_judge_below() -> None:
    inc_counter(COUNTER_HF_JUDGE_BELOW)


def set_hf_last_send_age_sec(sec: float) -> None:
    set_gauge(GAUGE_HF_LAST_SEND_AGE_SEC, sec)


def set_hf_effective_threshold_max(t: float) -> None:
    set_gauge(GAUGE_HF_EFFECTIVE_THRESHOLD_MAX, t)


def set_hf_threshold_ceiling_groups(n: float) -> None:
    set_gauge(GAUGE_HF_THRESHOLD_CEILING_GROUPS, n)


def set_hf_reachable_groups(n: float) -> None:
    set_gauge(GAUGE_HF_REACHABLE_GROUPS, n)


#: 进程启动即存在的 0 值指标 —— 见模块 docstring。
#: 心流那批在第 6 步（``learn`` / ``heartflow``）落位时登记；**登记 ≠ 已有埋点**，
#: 而是"这批指标从进程启动起就存在"，这样"值为 0"才排除了"埋点没接上"。
PRE_DECLARED_COUNTERS: tuple[str, ...] = (
    COUNTER_JUDGE_CALLS,
    COUNTER_JUDGE_FAILURES,
    COUNTER_HF_SENDS,
    COUNTER_HF_REACH_CAP_APPLIED,
    COUNTER_HF_JUDGE_PASSED,
    COUNTER_HF_JUDGE_BELOW,
)

#: 预声明为 0 的仪表盘（理由同上）。
PRE_DECLARED_GAUGES: tuple[str, ...] = (
    GAUGE_HF_LAST_SEND_AGE_SEC,
    GAUGE_HF_EFFECTIVE_THRESHOLD_MAX,
    GAUGE_HF_THRESHOLD_CEILING_GROUPS,
    GAUGE_HF_REACHABLE_GROUPS,
)


def _declare_all() -> None:
    for name in PRE_DECLARED_COUNTERS:
        _counters[_key(name)] = 0
    for name in PRE_DECLARED_GAUGES:
        _gauges[_key(name)] = 0.0


_declare_all()
