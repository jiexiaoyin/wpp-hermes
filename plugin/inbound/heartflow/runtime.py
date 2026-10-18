"""心流运行时：**内存缓存登记表** + **启动预热** + **300s sweep 循环**。

## 为什么这里**没有**配置单例（TS 2026-09-27 的教训，反着用）

TS 有一份 ``account_id -> hf 配置`` 的 Map，曾被 ``index.ts`` 与新拆出的命令处理器
**各复制一份**，于是"启动账号时写 A、命令处理器读 B" —— 实测直接导致
**7 个测试失败**。TS 的答案是"提取成一个单例"。

本侧重写时的实践是：配置的**唯一来源**是 ``core.accounts.resolve(account_id).heartflow``
（见 :func:`_effective_interval`），运行期改动走 ``core/overrides.py``（显式、可清、
可枚举）。于是那份单例只被写、**没有任何读者** —— 与其留着一个"随时会被谁写脏"的
第二真源，不如删掉：只写不读的全局配置表迟早被某处顺手当缓存写进去，然后与 Account
分叉，分叉的形状就是上面那个 bug。

## 七处内存缓存为什么要登记（本步的 Python 侧设计）

心流把热路径数据全放内存（learned 阈值 / 预算计数 / 画像 / 层统计 / 占比 / 去重窗 /
标签 / 观察 / 五维评分缓存），**judge 热路径零 DB IO** 全靠它们。代价是"重启即空、
必须由 sweep 重建"，以及"测试要能一处清干净"。

⇒ 每处在 :data:`CACHE_REGISTRY` 登记自己的 ``reset()``。不许各写各的 ``clear()``：
漏掉一处的表现是**测试间互相污染**（前一个用例的阈值漂到后一个用例），
那种失败看起来像业务 bug，极难定位。

## 线程安全

WS 收 / webhook 收 / 300s sweep **可能在不同线程**（sweep 跑在事件循环里，
WS 与 webhook 各有自己的任务），所以登记表与各缓存自身都过 ``RLock``。

## sweep 的形态

``asyncio.create_task`` 每账号一个，**句柄由适配器持有**（``disconnect()`` 逐个 cancel，
防 reload 累积孤儿任务，见 R9）。本模块额外做一层**幂等**：同账号重复起时，
新任务先取消前任 —— 对应 TS 的 clear-then-reschedule。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

#: 默认 sweep 间隔（秒）。运行时以 ``learning.sweepIntervalSec`` 为准，取不到时用这个。
DEFAULT_SWEEP_INTERVAL_SEC = 300


# ------------------------------------------------------------ 内存缓存登记表


class HfCacheRegistry:
    """九个内存缓存的**唯一**重置入口（见模块 docstring）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._resets: dict[str, Callable[[], None]] = {}

    def register(self, name: str, reset: Callable[[], None]) -> None:
        """登记一处缓存的清空函数。同名重复登记视为覆盖（模块重载时会发生）。"""
        with self._lock:
            self._resets[name] = reset

    def reset(self, name: str) -> None:
        """清空指定缓存。名字没登记过 ⇒ ``KeyError``（**吵**，别静默放过）。"""
        with self._lock:
            fn = self._resets.get(name)
        if fn is None:
            raise KeyError(f"未登记的心流缓存: {name!r}（已登记: {sorted(self._resets)}）")
        fn()

    def reset_all(self) -> None:
        """清空全部（测试 teardown / 热重载用）。单处异常不阻断其余。"""
        with self._lock:
            items = list(self._resets.items())
        for name, fn in items:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 —— 一处清不掉不该连累其余
                log.warning("[WPP HF] 清空缓存 %s 失败: %s", name, exc)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._resets)


#: 全局唯一登记表。
CACHE_REGISTRY = HfCacheRegistry()


# ---------------------------------------------------------------- 启动预热


def _preload_steps() -> list[tuple[str, str, Callable[[str], int]]]:
    """启动预热的 ``(键, 中文名, 加载函数)``。取不到模块 ⇒ 空表（见 :func:`preheat_account`）。

    导入放在**函数体内**：九模块未就位时（F5 的注册链纪律）不许在加载期炸。

    前三条对齐 TS ``startAccount`` 的三条 fire-and-forget 预热；**第四条（``dedupe``，
    2026-10-02 / B7）是本侧补的** —— 复读闸历史同样只活在内存里，不预热就等于"每次
    重启后 6 小时内没有复读闸"，而那种失效在日志与状态页上完全看不出来。
    **第五条（``profiles``，2026-10-18）** 是回复侧开始消费群画像后补的：缓存原先
    只在 sweep 第 1 步预热，重启后前 300s judge/回复侧都拿不到画像。
    """
    try:
        from . import dedupe as hf_dedupe
        from . import layer as hf_layer
        from . import learn as hf_learn
        from . import profile as hf_profile
        return [
            ("learned", "learned 阈值", hf_learn.load_learned_thresholds),
            ("budget", "发言预算计数", hf_learn.load_hf_budget_seed),
            ("layer", "分层统计", hf_layer.load_hf_layer_stats),
            ("dedupe", "复读历史", hf_dedupe.load_hf_dedupe_history),
            ("profiles", "群画像", hf_profile.load_hf_profiles),
        ]
    except Exception as exc:  # noqa: BLE001 —— 未就位 / 少了函数都算"没有可预热的"
        log.debug("[WPP HF] 启动预热取不到加载函数（心流未就位？）: %s", exc)
        return []


def preheat_account(account_id: str) -> dict[str, int]:
    """账号启动预热：把"judge 读得到、DB 里存着"的缓存灌满，返回各条加载条数。

    前三条对应 TS ``startAccount`` 里的三条 fire-and-forget 预热
    （``loadLearnedThresholds`` / ``loadHfBudgetSeed`` / ``loadHfLayerStats``）。
    **为什么非预热不可**：judge 热路径零 DB IO（V7），读的全是内存 —— 冷启动到首个
    sweep 之间隔着 300s，这段时间里阈值回落账号级、预算从 0 起算（= 给"重启刷额度"
    开后门）、分层统计整个缺席。

    第四条（``dedupe`` 复读历史，B7 / 2026-10-02）是本侧补的，同样出于"内存缓存冷启动"
    这个理由，只是它的失效更隐蔽：复读闸空转 6 小时，日志与状态页一切正常，只有群友
    能看到 bot 又把同一段话说了第二遍。

    与 TS 的**一处刻意不同**：那边是 ``void load().then()``，不阻塞启动（第一条消息可能
    撞上冷缓存）；本侧 DB 是同步的，于是**同步跑完再起 WS** —— 进来的第一条消息就是热的。
    这是更强的那一侧，不构成行为差异（TS 也没保证预热早于首条消息）。

    每条**各自独立**：一条炸了不影响其余（被调模块里还有自己那层降级）。
    本函数**不抛** —— 预热失败不是账号接入的阻塞点。

    第五条（``profiles`` 群画像，2026-10-18）只**读**画像行灌缓存，不生成：
    生成仍在 sweep 第 1 步（``profile.maybe_generate_hf_group_profiles``）。
    原先画像缓存要等首个 sweep 才热，回复侧开始消费画像后，那 300s 会直接表现为
    "重启后回复风格退化成通用人设"。
    """
    out: dict[str, int] = {}
    for key, label, fn in _preload_steps():
        try:
            n = int(fn(account_id))
        except Exception as exc:  # noqa: BLE001 —— 单条失败降级，见 docstring
            n = 0
            log.warning("[WPP HF] 启动预热失败（%s）: account=%s %s", label, account_id, exc)
        out[key] = n
        if n > 0:
            log.info("[WPP HF] %s 已预热: account=%s n=%d", label, account_id, n)
    return out


# ---------------------------------------------------------------- sweep


#: ``account_id -> Task``（幂等用；**不是**缓存，所以不进 CACHE_REGISTRY）
_sweep_tasks: dict[str, asyncio.Task] = {}


def _effective_interval(adapter: Any, account_id: str, fallback: int) -> int:
    """取 ``learning.sweepIntervalSec``；拿不到就用 ``fallback``。"""
    try:
        from . import learn  # 延迟导入：learn 尚未就位时不该炸
        from ...core import accounts as accounts_mod

        cfg = accounts_mod.resolve(account_id).heartflow or {}
        learning = learn.resolve_hf_learning(cfg)
        sec = int(learning.get("sweepIntervalSec") or 0)
        if sec > 0:
            return sec
    except Exception as exc:  # noqa: BLE001 —— 配不出来不算错，用默认
        log.debug("[WPP HF] sweep 间隔取配置失败，用默认 %ss: %s", fallback, exc)
    return fallback


async def sweep_loop(adapter: Any, account_id: str, interval: int = DEFAULT_SWEEP_INTERVAL_SEC) -> None:
    """每账号的周期 sweep。**句柄由适配器持有**（它在 disconnect 时逐个 cancel）。

    ⚠️ **幂等**：同账号起第二个任务时，先取消前任 —— 否则 ``connect()`` 被重入
    （Hermes runner 重连时会）就会累积多个 sweep 同时打 DB。
    """
    try:
        from . import learn as _learn  # 只在真能跑时才继续
    except Exception as exc:  # noqa: BLE001 —— 九模块未全部就位
        log.debug("[WPP HF] 心流 learn 未就位，sweep 不启动: %s", exc)
        return

    cur = asyncio.current_task()
    prev = _sweep_tasks.get(account_id)
    if prev is not None and prev is not cur and not prev.done():
        log.info("[WPP HF] 账号 %s 重复起 sweep ⇒ 取消前任（幂等）", account_id)
        prev.cancel()
    _sweep_tasks[account_id] = cur

    try:
        while True:
            # 先睡后跑 —— 对齐 TS ``setInterval``（首次触发在 interval 之后），
            # 启动时的那次预热由各模块自己的 ``seed_*`` 负责，不靠 sweep。
            await asyncio.sleep(interval)
            interval = _effective_interval(adapter, account_id, interval)
            try:
                await _learn.run_sweep(adapter, account_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 单轮失败不该终结整条循环
                log.warning("[WPP HF] sweep tick 失败（下一轮继续）: %s", exc, exc_info=True)
    except asyncio.CancelledError:
        raise
    finally:
        if _sweep_tasks.get(account_id) is cur:
            _sweep_tasks.pop(account_id, None)


async def stop_all_sweeps() -> None:
    """取消全部 sweep 任务（停账号 / 测试收尾）。"""
    tasks = [t for t in _sweep_tasks.values() if not t.done()]
    _sweep_tasks.clear()
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
