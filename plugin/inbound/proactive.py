"""私聊主动关心（轻量版）—— 老板 2026-10-19 拍板引入。

## 分层（扩展边界 P0-5，2026-10-19）

* **纯策略**在 :mod:`plugin.neutral.proactive_policy`：配置解析、静默时段、
  触发判定、提示词、输出清洗、日期键。不读 DB、不调模型、不发消息。
* **执行侧**在本模块：按账号读历史、调 judge 生成、发送、落库、sweep。
  本模块 re-export 纯策略的公开名字，适配器与既有测试无需改动。

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
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping

from ..neutral.proactive_policy import (  # noqa: F401 —— 兼容旧 import
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_SYSTEM,
    ProactiveConfig,
    build_prompt,
    clean_output,
    in_quiet_hours,
    is_eligible,
    resolve_proactive_cfg,
    today_key,
)

log = logging.getLogger(__name__)

#: 默认每轮 sweep 间隔（秒）。主动消息不需要很灵敏，半小时一轮足够。
DEFAULT_SWEEP_INTERVAL = 1800

#: 判断"今天是否已主动发过"的内存记录（account_id, peer_id）→ 本地日期。
_last_sent: dict[tuple[str, str], str] = {}


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
        if _last_sent.get((account_id, peer_id)) == today_key(now_sec):
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
        _last_sent[(account_id, peer_id)] = today_key(now_sec)
        _persist(account_id, peer_id, text, resp, now_sec)
    return {"sent": sent, "reason": "ok"}


async def _generate(rows: list[Mapping[str, Any]], cfg: ProactiveConfig) -> str:
    from ..llm.judge import call_judge

    try:
        raw = await call_judge(
            model=cfg.model,
            system_prompt=DEFAULT_SYSTEM.format(max_chars=cfg.max_chars),
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
