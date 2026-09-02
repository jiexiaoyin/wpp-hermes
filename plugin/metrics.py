"""WPP metrics — 关键运营指标监控。

迁移自 wpp-openclaw monitor/metrics.ts (简化版).

记录:
- inbound_msg_count (by peer)
- outbound_msg_count
- tool_call_count
- error_count
- api_latency_ms (p50/p95/p99 sliding window,  最近 100 次)

写入 stderr (被 systemd/journald 收), 也写 /tmp/wpp-metrics.log (供 dashboard 拉取).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)

_METRICS_LOG_PATH = "/tmp/wpp-metrics.log"
_metrics_lock = threading.RLock()

# 滑动窗口: 最近 100 个 latencies
_latencies = deque(maxlen=100)
_counts = {
    "inbound_msg": 0,
    "outbound_msg": 0,
    "tool_call": 0,
    "error": 0,
    "inbound_filtered": 0,
    "heartflow_trigger": 0,
}
_by_peer: dict[str, int] = {}
_last_flush_at: float = time.time()


def incr(name: str, by: int = 1, peer: Optional[str] = None) -> None:
    """通用计数器."""
    with _metrics_lock:
        _counts[name] = _counts.get(name, 0) + by
        if peer:
            _by_peer[peer] = _by_peer.get(peer, 0) + by


def record_latency(ms: int) -> None:
    with _metrics_lock:
        _latencies.append(max(0, int(ms)))


def _percentile(p: float) -> Optional[int]:
    if not _latencies:
        return None
    data = sorted(_latencies)
    idx = int(len(data) * p)
    idx = max(0, min(len(data) - 1, idx))
    return data[idx]


def flush(force: bool = False) -> None:
    """周期 (60s) 或显式调用时 dump metrics."""
    global _last_flush_at
    with _metrics_lock:
        if not force and time.time() - _last_flush_at < 60:
            return
        _last_flush_at = time.time()
        snapshot = {
            "ts": int(time.time()),
            "counts": dict(_counts),
            "top_peers": dict(sorted(_by_peer.items(), key=lambda x: -x[1])[:10]),
            "latency_p50": _percentile(0.5),
            "latency_p95": _percentile(0.95),
            "latency_p99": _percentile(0.99),
        }
        try:
            os.makedirs(os.path.dirname(_METRICS_LOG_PATH), exist_ok=True)
            with open(_METRICS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
            log.info("[WPP-METRICS] %s", json.dumps(snapshot, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            log.debug("[WPP] metrics flush failed: %s", e)


def get_summary() -> dict:
    """供 health check / dashboard 拉取."""
    with _metrics_lock:
        return {
            "counts": dict(_counts),
            "top_peers": dict(sorted(_by_peer.items(), key=lambda x: -x[1])[:10]),
            "latency_p50": _percentile(0.5),
            "latency_p95": _percentile(0.95),
            "latency_p99": _percentile(0.99),
            "window_size": len(_latencies),
        }