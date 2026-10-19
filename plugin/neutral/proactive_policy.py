"""私聊主动关心的**纯策略**（扩展边界 P0-5，2026-10-19）。

从 ``inbound/proactive.py`` 拆出：配置解析、静默时段、触发判定、提示词拼接、
模型输出清洗、本地日期键。这里不读 DB、不调模型、不发消息、不 import WPP。

执行侧（``maybe_send_once`` / ``sweep_loop`` / 生成与落库）仍在
``plugin/inbound/proactive.py``。

## 触发条件（全部满足，判定见 :func:`is_eligible`）

1. ``enabled`` 且 ``allowFrom`` 非空；
2. 当前时间不在 ``quietHours``（支持跨午夜，如 23→8）；
3. 该会话有历史消息，且**最后一条是 incoming**；
4. 距最后一条 incoming ≥ ``minIdleHours``；
5. 最近 24h 的 outbound 条数 < ``maxPerDay``。

第 6 条「今天还没被本功能主动发过」是进程级内存兜底，属于执行侧，不在这里。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Mapping

#: 每条会话最多取多少条历史给模型看。
DEFAULT_HISTORY_LIMIT = 20

#: 生成主动消息的 system prompt（执行侧调用模型时使用）。
DEFAULT_SYSTEM = (
    "你是微信里的一个朋友，不是客服、不是助手。根据最近的聊天记录，"
    "主动发一条自然、简短、口语化的关心消息。要求：\n"
    "- 只输出要发送的消息正文，不要引号、不要解释、不要任何前缀；\n"
    "- 一到两句话，不超过 {max_chars} 个字；\n"
    "- 提到聊天里出现过的具体事情，不要泛泛地问候；\n"
    "- 不要提 AI、机器人、助手、系统；不要连续追问，不要有推销感。"
)


@dataclass(frozen=True)
class ProactiveConfig:
    enabled: bool = False
    allow_from: tuple[str, ...] = ()
    max_per_day: int = 1
    min_idle_hours: float = 20.0
    quiet_hours: tuple[int, int] = (23, 8)
    model: str = ""
    max_chars: int = 120
    history_limit: int = DEFAULT_HISTORY_LIMIT
    timeout_ms: int = 8000


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_int(value: Any, default: int, *, lo: int | None = None,
            hi: int | None = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and out < lo:
        out = lo
    if hi is not None and out > hi:
        out = hi
    return out


def _as_float(value: Any, default: float, *, lo: float | None = None,
              hi: float | None = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if lo is not None and out < lo:
        out = lo
    if hi is not None and out > hi:
        out = hi
    return out


def _str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(x).strip() for x in value if x is not None and str(x).strip())


def _parse_hour(value: Any, default: int) -> int:
    """``"23:30"`` / ``23`` → 小时（0–23）。"""
    if isinstance(value, str) and ":" in value:
        value = value.split(":", 1)[0]
    return _as_int(value, default, lo=0, hi=23)


def resolve_proactive_cfg(raw: Any, *, ai: Any = None) -> ProactiveConfig:
    """账号 JSON ``proactive`` 段 → 配置。坏值回落默认，绝不 raise。"""
    cfg = _as_dict(raw)
    ai_cfg = _as_dict(ai)
    quiet = cfg.get("quietHours")
    if isinstance(quiet, (list, tuple)) and len(quiet) >= 2:
        start_h = _parse_hour(quiet[0], 23)
        end_h = _parse_hour(quiet[1], 8)
    else:
        q = _as_dict(quiet)
        start_h = _parse_hour(q.get("start"), 23)
        end_h = _parse_hour(q.get("end"), 8)
    model = str(cfg.get("model") or "").strip() or str(
        ai_cfg.get("judgeModel") or "").strip()
    return ProactiveConfig(
        enabled=bool(cfg.get("enabled", False)),
        allow_from=_str_tuple(cfg.get("allowFrom")),
        max_per_day=_as_int(cfg.get("maxPerDay"), 1, lo=0, hi=10),
        min_idle_hours=_as_float(cfg.get("minIdleHours"), 20.0, lo=1.0, hi=720.0),
        quiet_hours=(start_h, end_h),
        model=model,
        max_chars=_as_int(cfg.get("maxChars"), 120, lo=10, hi=500),
        history_limit=_as_int(cfg.get("historyLimit"), DEFAULT_HISTORY_LIMIT,
                              lo=2, hi=100),
        timeout_ms=_as_int(cfg.get("timeoutMs"), 8000, lo=1000, hi=60000),
    )


def in_quiet_hours(cfg: ProactiveConfig, now: float | None = None) -> bool:
    """``quietHours`` 内返回 True；支持跨午夜（start > end，如 23→8）。"""
    start, end = cfg.quiet_hours
    if start == end:
        return False
    hour = time.localtime(now if now is not None else time.time()).tm_hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _row_at(row: Mapping[str, Any]) -> int:
    try:
        return int(row.get("at_sec") or 0)
    except (TypeError, ValueError):
        return 0


def is_eligible(cfg: ProactiveConfig, rows: list[Mapping[str, Any]],
                now: float | None = None) -> bool:
    """纯判定（便于单测）：最后一条是对方发的、且已经静默够久。"""
    if not cfg.enabled or not cfg.allow_from:
        return False
    if not rows:
        return False
    now_sec = int(now if now is not None else time.time())
    last = rows[-1]
    if str(last.get("direction") or "") != "incoming":
        return False
    last_at = _row_at(last)
    if last_at <= 0:
        return False
    if now_sec - last_at < cfg.min_idle_hours * 3600:
        return False
    # 最近 24h 的出站条数（含普通回复）—— 今天已经聊过就别再主动打扰
    out_24h = sum(1 for r in rows
                  if str(r.get("direction") or "") == "outbound"
                  and _row_at(r) >= now_sec - 86400)
    return out_24h < cfg.max_per_day


def build_prompt(rows: list[Mapping[str, Any]], cfg: ProactiveConfig) -> str:
    """把最近历史拼成用户提示（升序：旧 → 新）。"""
    lines: list[str] = []
    for row in rows[-cfg.history_limit:]:
        text = str(row.get("content") or "").strip().replace("\n", " ")
        if not text:
            continue
        who = "我" if str(row.get("direction") or "") == "outbound" else "对方"
        lines.append(f"{who}: {text[:200]}")
    return (
        "最近的聊天记录（旧 → 新）：\n"
        + "\n".join(lines)
        + "\n\n请写一条现在主动发过去的关心消息。只输出消息正文。"
    )


def clean_output(text: str, max_chars: int = 120) -> str:
    """模型输出 → 可直接发送的一行文本；不合格返回空串（调用方跳过）。"""
    out = str(text or "").strip()
    out = re.sub(r"```.*?```", "", out, flags=re.S)
    out = out.strip().strip("\"'“”‘’「」")
    out = out.splitlines()[0].strip() if out else ""
    out = re.sub(r"^(消息|回复|回复内容)[:：]\s*", "", out)
    out = out.strip().strip("\"'“”‘’「」")
    out = re.sub(r"\s+", " ", out).strip()
    if not out or out in {"空", "无", "（无）", "(无)"}:
        return ""
    if len(out) > max_chars:
        cut = out[:max_chars]
        for mark in ("。", "！", "？", "!", "?", "，", ",", " "):
            pos = cut.rfind(mark)
            if pos >= max_chars // 2:
                cut = cut[:pos + 1]
                break
        out = cut.strip()
    return out


def today_key(now: float) -> str:
    """本地日期键（``YYYY-MM-DD``），用于「今天是否已主动发过」的内存兜底。"""
    lt = time.localtime(now)
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_SYSTEM",
    "ProactiveConfig",
    "build_prompt",
    "clean_output",
    "in_quiet_hours",
    "is_eligible",
    "resolve_proactive_cfg",
    "today_key",
]
