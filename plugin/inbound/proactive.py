"""私聊主动关心（轻量版）—— 老板 2026-10-19 拍板引入。

## 为什么是"轻量版"

参考的 ``astrbot_plugin_proactive_chat`` 是一套完整的主动对话系统（多触发源、
话题池、定时投递、上下文管理，AGPL-3.0）。Hermes/WPP 这边只取**最保守的一小块**：

* **只做私聊**，且**必须**在 ``proactive.allowFrom`` 名单里（空名单 = 功能关闭，
  fail-closed —— 绝不主动给陌生人发消息）；
* 只在「对方最后一条消息已经过去 ``minIdleHours`` 小时、且那条是**对方发的**」
  时触发（对话停在对方那里，才轮得到我们补一句；停在机器人这里就绝不追发）；
* 每天最多 ``maxPerDay`` 条、``quietHours`` 内不发；
* 文案由 judge 模型生成（同 Hermes 的 judge 凭据），失败只 warn；
* 发送后落库（``outbound``），下一次 sweep 看到最后一条是 outbound 自然不再触发。

默认 ``enabled=False``。代码是独立实现，没有复制参考插件的源码。

## 触发条件（全部满足）

1. ``enabled`` 且 ``allowFrom`` 非空、当前会话在名单里；
2. 当前时间不在 ``quietHours``（支持跨午夜，如 23→8）；
3. 该会话有历史消息，且**最后一条是 incoming**；
4. 距最后一条 incoming ≥ ``minIdleHours``；
5. 最近 24h 的 outbound 条数 < ``maxPerDay``；
6. 该会话今天还没被本功能主动发过（用内存 ``_last_sent`` 兜底，防落库失败重发）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping

log = logging.getLogger(__name__)

#: 默认每轮 sweep 间隔（秒）。主动消息不需要很灵敏，半小时一轮足够。
DEFAULT_SWEEP_INTERVAL = 1800

#: 判断"今天是否已主动发过"的内存记录（account_id, peer_id）→ 本地日期。
_last_sent: dict[tuple[str, str], str] = {}

#: 每条会话最多取多少条历史给模型看。
_HISTORY_LIMIT = 20

_DEFAULT_SYSTEM = (
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
    history_limit: int = _HISTORY_LIMIT
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
        history_limit=_as_int(cfg.get("historyLimit"), _HISTORY_LIMIT, lo=2, hi=100),
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


def _today_key(now: float) -> str:
    lt = time.localtime(now)
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


async def maybe_send_once(adapter: Any, account_id: str,
                          now: float | None = None) -> dict[str, Any]:
    """跑一轮：满足条件就生成并发送一条主动消息。返回结构化结果（便于测试/日志）。"""
    acct = adapter.account(account_id)
    cfg = resolve_proactive_cfg(getattr(acct, "proactive", {}),
                                ai=getattr(acct, "raw", {}).get("ai"))
    if not cfg.enabled:
        return {"sent": 0, "reason": "disabled"}
    if not cfg.allow_from:
        return {"sent": 0, "reason": "allowFrom_empty"}
    now_sec = int(now if now is not None else time.time())
    if in_quiet_hours(cfg, now_sec):
        return {"sent": 0, "reason": "quiet_hours"}
    client = (getattr(adapter, "_clients", {}) or {}).get(account_id)
    if client is None:
        return {"sent": 0, "reason": "not_connected"}

    from ..data import repo_messages

    sent = 0
    for peer_id in cfg.allow_from:
        if _last_sent.get((account_id, peer_id)) == _today_key(now_sec):
            continue
        try:
            rows = await asyncio.to_thread(
                repo_messages.list_messages_by_peer,
                account_id, peer_id, cfg.history_limit)
        except Exception as exc:  # noqa: BLE001 —— DB 抖动只跳过本轮
            log.warning("[WPP PROACTIVE] 读取 %s 历史失败（跳过）: %s", peer_id, exc)
            continue
        if not is_eligible(cfg, rows, now_sec):
            continue

        text = await _generate(rows, cfg)
        if not text:
            continue
        try:
            resp = await client.send_text(peer_id, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WPP PROACTIVE] 主动消息发送失败（%s）: %s", peer_id, exc)
            continue
        log.info("[WPP PROACTIVE] 已主动发送 → %s（%d 字）", peer_id, len(text))
        sent += 1
        _last_sent[(account_id, peer_id)] = _today_key(now_sec)
        _persist(account_id, peer_id, text, resp, now_sec)
    return {"sent": sent, "reason": "ok"}


async def _generate(rows: list[Mapping[str, Any]], cfg: ProactiveConfig) -> str:
    from ..llm.judge import call_judge

    try:
        raw = await call_judge(
            model=cfg.model,
            system_prompt=_DEFAULT_SYSTEM.format(max_chars=cfg.max_chars),
            user_prompt=build_prompt(rows, cfg),
            max_tokens=300,
            timeout_ms=cfg.timeout_ms,
        )
    except Exception as exc:  # noqa: BLE001 —— 模型失败只跳过，不重试轰炸
        log.warning("[WPP PROACTIVE] 生成失败: %s", exc)
        return ""
    text = clean_output(raw, cfg.max_chars)
    if not text:
        log.warning("[WPP PROACTIVE] 模型输出不可用（已跳过）: %r", str(raw)[:120])
    return text


def _persist(account_id: str, peer_id: str, text: str, resp: Any, now_sec: int) -> None:
    """落库出站记录（失败只 warn —— 消息已经发出去了）。"""
    try:
        from ..data import repo_messages
        from ..inbound.persist import db_chat_id, db_peer_kind
        from ..platform import outbound

        ids = outbound.extract_outbound_msg_ids(resp)
        repo_messages.save_message(
            account_id,
            direction="outbound",
            peer_kind=db_peer_kind("dm"),
            peer_id=peer_id,
            chat_id=db_chat_id(peer_id, is_group=False),
            msg_id=ids.new_msg_id or ids.msg_id or None,
            new_msg_id=ids.new_msg_id,
            content=text,
            msg_type="text",
            raw_payload={"proactive": True},
            ts=now_sec,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[WPP PROACTIVE] 落库失败（消息已发出）: %s", exc)


async def sweep_loop(adapter: Any, account_id: str,
                     interval: int = DEFAULT_SWEEP_INTERVAL) -> None:
    """每账号一个 sweep（适配器持有句柄并在 disconnect 时 cancel）。"""
    while True:
        await asyncio.sleep(max(60, int(interval)))
        try:
            result = await maybe_send_once(adapter, account_id)
            if result.get("sent"):
                log.info("[WPP PROACTIVE] sweep：%s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 下一轮继续
            log.warning("[WPP PROACTIVE] sweep tick 失败（下一轮继续）: %s", exc)


__all__ = [
    "DEFAULT_SWEEP_INTERVAL",
    "ProactiveConfig",
    "build_prompt",
    "clean_output",
    "in_quiet_hours",
    "is_eligible",
    "maybe_send_once",
    "resolve_proactive_cfg",
    "sweep_loop",
]
