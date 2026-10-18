"""把 WebUI 会话标题改成「群名」/「私聊昵称」。

老板 2026-10-18 的诉求：``wpp-wechat`` 下的会话在 WebUI 里按微信来源命名，
便于快速定位 —— 群聊用群名称，私聊用联系人昵称。

Hermes 默认会用小模型从**首条消息正文**生成标题（3-7 个词），所以同一个群
里不同成员、不同话题会得到一堆互不相关的标题。这里在**首轮 LLM 调用前**
（``pre_llm_call``，此时 session 行已创建、自动标题尚未写）用 ``user``
provenance 写标题；Hermes 的标题优先级是 ``derived < llm < user``，因此
随后启动的自动标题线程不会覆盖它。

两个名字来源：

* 群名：``wpp_chatrooms.nickname``（适配器连接后由 ``/Group/List`` 回填），
  其次 ``remark``，最后退回群 ID（``xxx@chatroom``）。
* 人：群成员表 ``wpp_chatroom_members.nickname`` → 通讯录 remark/nickname →
  vendor 推送文案里解析出的昵称 → wxid。

群会话只保留**一条**（配置 ``group_sessions_per_user: false``，2026-10-18
老板要求），所以群标题直接就是群名；发送者身份由 Hermes 的共享会话模式在
每条消息前加 ``[发送者昵称]`` 保留。私聊标题就是联系人昵称，重名/重建会话
时由 :func:`write_title` 自动加 ``#2``。

本模块**不在 import 时连库**（``repo_messages`` 的 pymysql 是懒加载的），
这样 ``register()`` 的隔离探针仍可加载它。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PLATFORM = "wechatpadpro"
MAX_TITLE_CHARS = 100  # 与 hermes_state.SessionDB.MAX_TITLE_LENGTH 一致
_CACHE_TTL_SEC = 300.0
_NAME_CACHE: dict[tuple[str, ...], tuple[float, str]] = {}

def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _name_or_empty(candidate: Any, identity: str) -> str:
    """``candidate`` 可当显示名就返回，否则空串。

    适配器在没有昵称时会把 ``user_name``/``chat_name`` 填成 wxid/群 ID，所以
    "和身份字段完全相同"就是"这不是名字"的可靠判据；不靠正则猜（``Michael``
    这种英文昵称不该被误判成 wxid）。
    """
    candidate = _clean(candidate)
    return "" if not candidate or candidate == _clean(identity) else candidate


def _cached(key: tuple[str, ...], loader) -> str:
    """短 TTL 进程内缓存 —— 入站热路径每条消息都会问名字，不能每次都打 DB。"""
    now = time.time()
    hit = _NAME_CACHE.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        value = _clean(loader())
    except Exception as exc:  # noqa: BLE001 —— 名字取不到只影响标题，不能影响消息
        log.debug("[WPP] 名称解析失败 %s: %s", key, exc)
        value = ""
    _NAME_CACHE[key] = (now + _CACHE_TTL_SEC, value)
    return value


def clear_name_cache() -> None:
    """测试/群名同步后清缓存用。"""
    _NAME_CACHE.clear()


def _repo():
    from ..data import repo_messages
    return repo_messages


# ------------------------------------------------------------------ 名称查询

def group_name(account_id: str, chatroom_id: str) -> str:
    """群名称：``nickname`` 优先（真实群名），``remark`` 次之（自己的备注）。"""
    if not chatroom_id:
        return ""

    def load() -> str:
        row = _repo().get_chatroom(account_id, chatroom_id) or {}
        return _clean(row.get("nickname")) or _clean(row.get("remark"))

    return _cached(("group", account_id, chatroom_id), load)


def contact_name(account_id: str, wxid: str) -> str:
    """联系人显示名：备注优先（老板自己起的名字），其次微信昵称。"""
    if not wxid:
        return ""

    def load() -> str:
        row = _repo().get_contact(account_id, wxid) or {}
        return _clean(row.get("remark")) or _clean(row.get("nickname"))

    return _cached(("contact", account_id, wxid), load)


def group_member_name(account_id: str, chatroom_id: str, wxid: str) -> str:
    """群成员在**该群里的**昵称（群昵称可能和通讯录昵称不同）。"""
    if not (chatroom_id and wxid):
        return ""

    def load() -> str:
        row = _repo().get_chatroom_member(account_id, chatroom_id, wxid) or {}
        return _clean(row.get("nickname"))

    return _cached(("member", account_id, chatroom_id, wxid), load)


def _account_and_peer(origin: dict, session_key: str = "") -> tuple[str, str]:
    """从 ``origin_json.chat_id``（``account:peer``）拆账号与对端。

    兜底解析 session_key：``agent:<profile>:wechatpadpro:<type>:<account>:<peer>:<user>``。
    """
    chat_id = _clean(origin.get("chat_id"))
    account_id, sep, peer_id = chat_id.partition(":")
    if sep and peer_id:
        return account_id or "default", peer_id
    parts = str(session_key or "").split(":")
    if len(parts) >= 7 and parts[2] == PLATFORM:
        return parts[4] or "default", parts[5]
    return "default", chat_id


def chat_display_name(
    account_id: str, peer_kind: str, peer_id: str, user_id: str = "", fallback: str = "",
) -> str:
    """适配器 ``SessionSource.chat_name`` 用：群=群名，私聊=联系人昵称。"""
    if peer_kind == "group":
        return group_name(account_id, peer_id) or peer_id
    identity = user_id or peer_id
    name = contact_name(account_id, identity)
    if not name:
        name = _name_or_empty(fallback, identity)
    return name or identity


def sender_display_name(
    account_id: str, peer_id: str, *, is_group: bool, user_id: str = "", fallback: str = "",
) -> str:
    """适配器 ``SessionSource.user_name`` 用：群成员昵称 / 私聊联系人昵称。"""
    pushed = _name_or_empty(fallback, user_id)
    if is_group:
        name = group_member_name(account_id, peer_id, user_id)
        if not name:
            name = pushed
        if not name:
            name = contact_name(account_id, user_id)
    else:
        name = contact_name(account_id, user_id)
        if not name:
            name = pushed
    return name or user_id


# ------------------------------------------------------------------ 标题

def build_session_title(origin: dict, session_key: str = "") -> str:
    """``origin_json`` → 会话标题（不含唯一化后缀）。"""
    origin = origin if isinstance(origin, dict) else {}
    account_id, peer_id = _account_and_peer(origin, session_key)
    chat_type = _clean(origin.get("chat_type")).lower()
    if chat_type in ("group", "channel"):
        # 一个群一条会话 ⇒ 标题就是群名，不带发送者。
        return group_name(account_id, peer_id) or _name_or_empty(
            origin.get("chat_name"), peer_id) or peer_id
    return chat_display_name(
        account_id, "dm", peer_id,
        user_id=_clean(origin.get("user_id")), fallback=_clean(origin.get("user_name")),
    )


def title_for_session(row: dict) -> str:
    """session 行 → 标题；``origin_json`` 不是本平台时返回空串。"""
    try:
        origin = json.loads(row.get("origin_json") or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(origin, dict):
        return ""
    if _clean(origin.get("platform")).lower() != PLATFORM:
        return ""
    return _clean(build_session_title(origin, row.get("session_key") or ""))


def _fit(title: str, suffix: str = "") -> str:
    title = _clean(title)
    room = MAX_TITLE_CHARS - len(suffix)
    return (title[:room].rstrip() + suffix) if room > 0 else title[:MAX_TITLE_CHARS]


def write_title(db, session_id: str, base: str) -> str:
    """写 ``user`` provenance 标题；重名时自动 ``#2``、``#3``…。

    Hermes 的标题全局唯一，同一联系人/同一群同一发送者新建会话时会撞名。
    ``set_session_title`` 撞名会抛 ``ValueError``（而不是静默覆盖），这里就
    按 Hermes 自己的 ``<title> #N`` 约定往后排。
    """
    base = _clean(base)
    if not base:
        return ""
    for n in range(1, 20):
        suffix = "" if n == 1 else f" #{n}"
        candidate = _fit(base, suffix)
        try:
            if db.set_session_title(session_id, candidate):
                return candidate
        except ValueError:
            continue
        except Exception as exc:  # noqa: BLE001
            log.debug("[WPP] 写会话标题失败 session=%s: %s", session_id, exc)
            return ""
    return ""


# ------------------------------------------------------------------ Hermes hook

def _open_session_db(session_id: str):
    """找到包含该 session 的 Hermes state.db（多 profile 时逐个探）。"""
    try:
        from hermes_state import SessionDB
    except Exception as exc:  # noqa: BLE001 —— 极早期/探针环境
        log.debug("[WPP] 无法导入 hermes_state: %s", exc)
        return None

    def _matches(db) -> bool:
        try:
            return db.get_session(session_id) is not None
        except Exception:  # noqa: BLE001
            return False

    # 当前 profile 的默认库（gateway 在 profile context 里调 hook）。
    try:
        db = SessionDB()
        if _matches(db):
            return db
        db.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("[WPP] 默认 state.db 打开失败: %s", exc)

    # 多路复用下 context 可能没绑到本 profile：扫 profiles/*/state.db。
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
        paths = [home / "state.db", *sorted((home / "profiles").glob("*/state.db"))]
    except Exception:  # noqa: BLE001
        return None
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        try:
            probe = SessionDB(resolved, read_only=True)
            found = _matches(probe)
            probe.close()
            if found:
                return SessionDB(resolved)
        except Exception:  # noqa: BLE001
            continue
    return None


def on_pre_llm_call(**kwargs: Any) -> None:
    """Hermes ``pre_llm_call`` hook —— 只在新会话首轮动手。

    此时 ``_ensure_session_row`` 已建行、自动标题还没跑；写 ``user`` 标题后
    自动标题线程（derived→llm）不会再覆盖。后续轮次直接返回，避免热路径 DB 读，
    也保证用户手动 ``/title`` 之后不会被插件改回去。
    """
    if not kwargs.get("is_first_turn"):
        return
    if _clean(kwargs.get("platform")).lower() != PLATFORM:
        return
    session_id = _clean(kwargs.get("session_id"))
    if not session_id:
        return
    try:
        db = _open_session_db(session_id)
    except Exception as exc:  # noqa: BLE001 —— hook 契约：任何失败都不许打断对话
        log.debug("[WPP] 打开会话库失败 session=%s: %s", session_id, exc)
        return
    if db is None:
        return
    try:
        row = db.get_session(session_id)
        if not row:
            return
        # user 标题 = 用户手动改名，或本插件已写过；都不该再动。
        if _clean(row.get("title_source")).lower() == "user":
            return
        title = title_for_session(row)
        if not title:
            return
        written = write_title(db, session_id, title)
        if written:
            log.info("[WPP] 会话标题已按微信来源设置为 %r (session=%s)", written, session_id)
    except Exception as exc:  # noqa: BLE001 —— 标题失败不能影响对话
        log.debug("[WPP] 设置会话标题失败 session=%s: %s", session_id, exc)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
