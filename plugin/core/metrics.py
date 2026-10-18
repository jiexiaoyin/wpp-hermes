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

import json
import threading
import time

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


def render_json() -> str:
    """JSON 快照（对齐 TS ``renderJson``）—— ``curl /metrics | jq`` 直接消费。

    为什么对外默认给 JSON 而不是 Prometheus 文本：本插件没有 Prometheus 抓取端，
    排障时人/脚本看的是 JSON；Prometheus 文本保留给将来真接抓取器的场景。
    两个格式并存（与 TS v1.9.2 同款）。
    """
    with _lock:
        body = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "counters": dict(_counters),
            "gauges": dict(_gauges),
        }
    return json.dumps(body, ensure_ascii=False, indent=2)


# ------------------------------------------------------- 具名指标（有调用方的）


def inc_judge_call() -> None:
    """``call_judge`` 被调用（四机制公共入口，计在这里最省埋点且不会漏）。"""
    inc_counter(COUNTER_JUDGE_CALLS)


def inc_judge_failure() -> None:
    """``call_judge`` **最终**失败（兜底救回来的那次不算）。"""
    inc_counter(COUNTER_JUDGE_FAILURES)


def inc_judge_fallback() -> None:
    """主端点失败、兜底被启用。"""
    inc_counter(COUNTER_JUDGE_FALLBACK)


def inc_judge_fallback_ok() -> None:
    """兜底端点救回来了（直接回答"兜底到底有没有用"）。"""
    inc_counter(COUNTER_JUDGE_FALLBACK_OK)


def inc_intent_call() -> None:
    inc_counter(COUNTER_INTENT_CALLS)


def inc_intent_failure() -> None:
    inc_counter(COUNTER_INTENT_FAILURES)


def inc_intent_fallback() -> None:
    inc_counter(COUNTER_INTENT_FALLBACK)


def inc_intent_fallback_ok() -> None:
    inc_counter(COUNTER_INTENT_FALLBACK_OK)


COUNTER_JUDGE_CALLS = "judge_calls_total"
#: **最终**失败次数（v1.13.0 起）：主端点挂了但兜底救回来的那次不计失败。
COUNTER_JUDGE_FAILURES = "judge_failures_total"
#: 兜底被启用次数 / 兜底救回来次数（v1.13.0）。
COUNTER_JUDGE_FALLBACK = "judge_fallback_total"
COUNTER_JUDGE_FALLBACK_OK = "judge_fallback_ok_total"

#: llmIntent 独立一套（v1.14.2）：它不走 ``call_judge``，复用等于"意图链断了但 judge
#: 计数照涨"。
COUNTER_INTENT_CALLS = "intent_calls_total"
COUNTER_INTENT_FAILURES = "intent_failures_total"
COUNTER_INTENT_FALLBACK = "intent_fallback_total"
COUNTER_INTENT_FALLBACK_OK = "intent_fallback_ok_total"

#: migration 未完成（连不上 / schema 只建了一半 / 漂移体检自己坏了）—— D7（2026-10-02）。
#: 为什么值得一个计数器：这类失败**只在启动那一行 warn 里出现过一次**，此后进程照跑、
#: ``SELECT 1`` 照通，于是运维面上与"一切正常"完全一样。
COUNTER_DB_MIGRATION_FAILURES = "db_migration_failures_total"


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

#: DB 体检结论：1 = ``migrate()`` 跑通（含"只报不改"的漂移），0 = 未就绪 / 未跑 / 失败。
#: **不是**"连得上吗"（``is_available()`` 只问 ``SELECT 1``）—— 半建成的 schema 也连得上。
GAUGE_DB_READY = "db_ready"


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


def inc_db_migration_failure() -> None:
    inc_counter(COUNTER_DB_MIGRATION_FAILURES)


# ------------------------------------------------ webhook / vendor 注册（v1.9.2 对齐）

#: webhook 收到的 POST 总数（**在校验之前**计 —— 与 TS ``incReceived`` 同位置）。
COUNTER_MESSAGES_RECEIVED = "messages_received_total"
#: 路径不认识（404）。
COUNTER_MESSAGES_REJECTED_PATH = "messages_rejected_path_total"
#: 路径/secret 鉴权失败（403）。
COUNTER_MESSAGES_REJECTED_SECRET = "messages_rejected_secret_total"
#: 请求体超限 / Content-Length 非法 / chunked 读失败（400/413）。
COUNTER_MESSAGES_REJECTED_BODY_SIZE = "messages_rejected_body_size_total"
#: 已成功调度进主事件循环。
COUNTER_MESSAGES_PROCESSED = "messages_processed_total"
#: 调度失败（主循环不可用 / run_coroutine_threadsafe 抛错）。
COUNTER_MESSAGES_DISPATCH_FAILED = "messages_dispatch_failed_total"
#: 厂商 webhook 注册（启动重试 + 周期刷新）。
COUNTER_SETWEBHOOK_OK = "setwebhook_ok_total"
COUNTER_SETWEBHOOK_FAIL = "setwebhook_fail_total"
COUNTER_SETWEBHOOK_PERIODIC_OK = "setwebhook_periodic_ok_total"
COUNTER_SETWEBHOOK_PERIODIC_FAIL = "setwebhook_periodic_fail_total"
#: 出站落库（工具/媒体/统一 send_message 路径）。
COUNTER_OUTBOUND_PERSIST_OK = "outbound_persist_ok_total"
COUNTER_OUTBOUND_PERSIST_FAIL = "outbound_persist_fail_total"

# ---------------------------------------------------------- 入站链路（TS 对齐）
#: 进入 ``_handle_one`` 的入站条数（WS 帧 + webhook 通知，与 TS ``messages_in_total`` 同义）。
COUNTER_MESSAGES_IN = "messages_in_total"
#: 去重命中（``_handle_one`` 的 pipeline 去重闸）。
COUNTER_MESSAGES_REJECTED_DEDUPE = "messages_rejected_dedupe_total"
#: 触发判定判丢（``should_drop``）。
COUNTER_MESSAGES_REJECTED_POLICY = "messages_rejected_policy_total"
#: 入站落库成功（``persist_inbound`` 返回 True）。
COUNTER_MESSAGES_SAVED_DB = "messages_saved_db_total"
#: 媒体富化上传失败（``_publish_media`` 拿到空 URL）。
COUNTER_ENRICH_SAVE_FAILED = "enrich_save_failed_total"
#: 单条消息处理抛异常（``_dispatch_one`` 的 except）。
COUNTER_HANDLER_ONERROR = "handler_onerror_total"
#: 交给 agent runner 的条数（``handle_message`` 之前计）。
COUNTER_DISPATCH_DISPATCHED = "dispatch_dispatch_total"
#: webhook 请求超时（读取慢连接）与 JSON 解析失败。
COUNTER_MESSAGES_REJECTED_TIMEOUT = "messages_rejected_timeout_total"
COUNTER_MESSAGES_REJECTED_PARSE = "messages_rejected_parse_total"
#: autoSetWebhook 被跳过的三类原因（TS ``index.ts:791-794``）。
COUNTER_SETWEBHOOK_SKIPPED_NO_PUBLIC_URL = "setwebhook_skipped_no_public_url_total"
COUNTER_SETWEBHOOK_SKIPPED_NO_AUTHCODE = "setwebhook_skipped_no_authcode_total"
COUNTER_SETWEBHOOK_SKIPPED_DISABLED = "setwebhook_skipped_disabled_total"

# ------------------------------------------------------------------------ WS
#: WS 连接断开次数（含 vendor idle / 网络抖动 / 主动 stop，与 TS 同口径）。
COUNTER_WS_DISCONNECTS = "ws_disconnects_total"
#: 安排一次重连（stop 之后的调用不计）。
COUNTER_WS_RECONNECTS = "ws_reconnects_total"
#: 当前连接态（1/0）—— counter 只回答"断过几次"，回答不了"此刻是不是断的"。
GAUGE_WS_CONNECTED = "ws_connected"


def inc_webhook_received() -> None:
    inc_counter(COUNTER_MESSAGES_RECEIVED)


def inc_webhook_rejected_path() -> None:
    inc_counter(COUNTER_MESSAGES_REJECTED_PATH)


def inc_webhook_rejected_secret() -> None:
    inc_counter(COUNTER_MESSAGES_REJECTED_SECRET)


def inc_webhook_rejected_body_size() -> None:
    inc_counter(COUNTER_MESSAGES_REJECTED_BODY_SIZE)


def inc_webhook_processed() -> None:
    inc_counter(COUNTER_MESSAGES_PROCESSED)


def inc_webhook_dispatch_failed() -> None:
    inc_counter(COUNTER_MESSAGES_DISPATCH_FAILED)


def inc_setwebhook_ok(periodic: bool = False) -> None:
    inc_counter(COUNTER_SETWEBHOOK_PERIODIC_OK if periodic else COUNTER_SETWEBHOOK_OK)


def inc_setwebhook_fail(periodic: bool = False) -> None:
    inc_counter(COUNTER_SETWEBHOOK_PERIODIC_FAIL if periodic else COUNTER_SETWEBHOOK_FAIL)


def inc_outbound_persist(ok: bool) -> None:
    inc_counter(COUNTER_OUTBOUND_PERSIST_OK if ok else COUNTER_OUTBOUND_PERSIST_FAIL)


def inc_messages_in() -> None:
    inc_counter(COUNTER_MESSAGES_IN)


def inc_messages_rejected_dedupe() -> None:
    inc_counter(COUNTER_MESSAGES_REJECTED_DEDUPE)


def inc_messages_rejected_policy() -> None:
    inc_counter(COUNTER_MESSAGES_REJECTED_POLICY)


def inc_messages_saved_db() -> None:
    inc_counter(COUNTER_MESSAGES_SAVED_DB)


def inc_enrich_save_failed() -> None:
    inc_counter(COUNTER_ENRICH_SAVE_FAILED)


def inc_handler_onerror() -> None:
    inc_counter(COUNTER_HANDLER_ONERROR)


def inc_dispatch_dispatched() -> None:
    inc_counter(COUNTER_DISPATCH_DISPATCHED)


def inc_webhook_timeout() -> None:
    inc_counter(COUNTER_MESSAGES_REJECTED_TIMEOUT)


def inc_webhook_parse_failed() -> None:
    inc_counter(COUNTER_MESSAGES_REJECTED_PARSE)


def inc_setwebhook_skipped(reason: str) -> None:
    """``reason`` ∈ ``no_public_url`` / ``no_authcode`` / ``disabled``。"""
    key = {
        "no_public_url": COUNTER_SETWEBHOOK_SKIPPED_NO_PUBLIC_URL,
        "no_authcode": COUNTER_SETWEBHOOK_SKIPPED_NO_AUTHCODE,
        "disabled": COUNTER_SETWEBHOOK_SKIPPED_DISABLED,
    }.get(reason)
    if key:
        inc_counter(key)


def inc_ws_disconnect() -> None:
    inc_counter(COUNTER_WS_DISCONNECTS)


def inc_ws_reconnect() -> None:
    inc_counter(COUNTER_WS_RECONNECTS)


def set_ws_connected(connected: bool) -> None:
    set_gauge(GAUGE_WS_CONNECTED, 1.0 if connected else 0.0)


def set_db_gauge(ready: float) -> None:
    """DB 体检结论（1 / 0）—— 名字与 ``db.health()`` 的 ``ok`` 同源，见 ``GAUGE_DB_READY``。"""
    set_gauge(GAUGE_DB_READY, ready)


#: 进程启动即存在的 0 值指标 —— 见模块 docstring。
#: 心流那批在第 6 步（``learn`` / ``heartflow``）落位时登记；**登记 ≠ 已有埋点**，
#: 而是"这批指标从进程启动起就存在"，这样"值为 0"才排除了"埋点没接上"。
PRE_DECLARED_COUNTERS: tuple[str, ...] = (
    COUNTER_JUDGE_CALLS,
    COUNTER_JUDGE_FAILURES,
    COUNTER_JUDGE_FALLBACK,
    COUNTER_JUDGE_FALLBACK_OK,
    COUNTER_INTENT_CALLS,
    COUNTER_INTENT_FAILURES,
    COUNTER_INTENT_FALLBACK,
    COUNTER_INTENT_FALLBACK_OK,
    COUNTER_HF_SENDS,
    COUNTER_HF_REACH_CAP_APPLIED,
    COUNTER_HF_JUDGE_PASSED,
    COUNTER_HF_JUDGE_BELOW,
    COUNTER_DB_MIGRATION_FAILURES,
    COUNTER_MESSAGES_RECEIVED,
    COUNTER_MESSAGES_REJECTED_PATH,
    COUNTER_MESSAGES_REJECTED_SECRET,
    COUNTER_MESSAGES_REJECTED_BODY_SIZE,
    COUNTER_MESSAGES_PROCESSED,
    COUNTER_MESSAGES_DISPATCH_FAILED,
    COUNTER_SETWEBHOOK_OK,
    COUNTER_SETWEBHOOK_FAIL,
    COUNTER_SETWEBHOOK_PERIODIC_OK,
    COUNTER_SETWEBHOOK_PERIODIC_FAIL,
    COUNTER_OUTBOUND_PERSIST_OK,
    COUNTER_OUTBOUND_PERSIST_FAIL,
    COUNTER_MESSAGES_IN,
    COUNTER_MESSAGES_REJECTED_DEDUPE,
    COUNTER_MESSAGES_REJECTED_POLICY,
    COUNTER_MESSAGES_SAVED_DB,
    COUNTER_ENRICH_SAVE_FAILED,
    COUNTER_HANDLER_ONERROR,
    COUNTER_DISPATCH_DISPATCHED,
    COUNTER_MESSAGES_REJECTED_TIMEOUT,
    COUNTER_MESSAGES_REJECTED_PARSE,
    COUNTER_SETWEBHOOK_SKIPPED_NO_PUBLIC_URL,
    COUNTER_SETWEBHOOK_SKIPPED_NO_AUTHCODE,
    COUNTER_SETWEBHOOK_SKIPPED_DISABLED,
    COUNTER_WS_DISCONNECTS,
    COUNTER_WS_RECONNECTS,
)

#: 预声明为 0 的仪表盘（理由同上）。
PRE_DECLARED_GAUGES: tuple[str, ...] = (
    GAUGE_HF_LAST_SEND_AGE_SEC,
    GAUGE_HF_EFFECTIVE_THRESHOLD_MAX,
    GAUGE_HF_THRESHOLD_CEILING_GROUPS,
    GAUGE_HF_REACHABLE_GROUPS,
    GAUGE_DB_READY,
    GAUGE_WS_CONNECTED,
)


def _declare_all() -> None:
    for name in PRE_DECLARED_COUNTERS:
        _counters[_key(name)] = 0
    for name in PRE_DECLARED_GAUGES:
        _gauges[_key(name)] = 0.0


_declare_all()
