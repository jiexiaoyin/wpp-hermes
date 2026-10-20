"""微信助手控制台 —— dashboard 后端（挂载在 ``/api/plugins/wechatpadpro-console/``）。

## 为什么不是 ``import plugin...``

Hermes 的 dashboard 以**顶层模块**加载本文件（``__package__`` 为空），插件目录也
不在 ``sys.path`` 上。所以这里用 :func:`_mods` 造一个**合成包** ``_hermes_wpp_console``
（``__path__`` 指向 ``plugin/``），再 ``importlib`` 叶子模块：``data.repo_*`` 的
``from . import db``、``inbound.join_welcome`` 的惰性 ``..llm`` 都能正常解析，
同时**不会**执行 ``plugin/__init__.py`` 等带有网关副作用的模块。

## 三条边界

1. **只读凭据、绝不返回**：账号视图走 :func:`core.console_admin.safe_account_view`
   的白名单；``authcode`` / ``tokenKey`` / ``webhookSecret`` / ``webhookPathToken``
   结构性不出现。写路径走 ``core.account_store.update_account``，凭证键被结构性拒绝。
2. **不调用厂商 API、不重连账号**：在线状态读网关写的快照文件
   ``$HERMES_HOME/plugin-data/wechatpadpro/console-status.json``。
3. **DB 只读**：``data.db`` 建连不建表；本文件只做 ``SELECT``，且每个 handler
   ``finally: db.close()``（线程池线程长期存活，不关会漏连接）。

写盘之后由**网关侧** watcher（3 秒 mtime 轮询）热加载，本进程不 reload 任何东西。
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import threading
import time
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

router = APIRouter()

#: 插件根目录（本文件在 ``plugin/dashboard/`` 下）
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST = Path(__file__).resolve().parent / "manifest.json"

#: 合成包名 —— 不能与真实 ``plugin`` 包重名（避免与网关内的已加载模块互踩）。
_PKG = "_hermes_wpp_console"

#: 预加载给 dashboard 进程的 env 白名单（只键名，**绝不含凭据**）。
#: dashboard 进程没有网关的 ``WECHATPRO_*``；DB 配置必须从 ``.env`` 补齐。
_ENV_KEYS = (
    "WECHATPRO_DB_BACKEND", "WECHATPRO_DB_HOST", "WECHATPRO_DB_PORT",
    "WECHATPRO_DB_NAME", "WECHATPRO_DB_USER", "WECHATPRO_DB_PASSWORD",
    "WECHATPRO_DB_CONNECT_TIMEOUT", "WECHATPRO_DB_READ_TIMEOUT",
    "WECHATPRO_DB_WRITE_TIMEOUT", "WECHATPRO_DB_ALLOW_REMOTE",
    "WECHATPRO_SQLITE_PATH", "WECHATPRO_ACCOUNTS_DIR",
    "WECHATPRO_ACCOUNT_BACKUP_DIR",
)

_mods_lock = threading.Lock()
_mods_obj: Any = None


class _Mods:
    """已 import 的叶子模块集合（避免 handler 每次都走 importlib）。"""

    account_store: Any
    accounts: Any
    console_admin: Any
    join_welcome: Any
    db: Any
    repo_console: Any
    repo_messages: Any
    repo_hf: Any
    repo_style: Any
    repo_affection: Any
    pairing: Any


# ---------------------------------------------------------------- 模块加载

def _env_files() -> list[Path]:
    home = Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()
    files = [home / ".env"]
    profiles = home / "profiles"
    if profiles.is_dir():
        try:
            files.extend(sorted(p / ".env" for p in profiles.iterdir() if p.is_dir()))
        except OSError:
            pass
    return files


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 ``.env`` 解析（``KEY=VALUE`` / ``export`` / 引号 / ``#`` 注释）。

    只取白名单键；**不执行**任何内容，也不打印值。
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if key not in _ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out.setdefault(key, value)
    return out


def _preload_env() -> None:
    """把 DB 相关 env 从 ``.env`` 补进 **尚未设置** 的键（setdefault）。"""
    for path in _env_files():
        for key, value in _parse_env_file(path).items():
            os.environ.setdefault(key, value)


def _install_synthetic_package() -> None:
    """注册 ``_hermes_wpp_console`` 及其子包（幂等）。

    ⚠️ 必须先写 ``sys.modules`` 再挂到父包 —— ``importlib`` 解析相对导入时
    只认 ``sys.modules``；只挂属性会在 ``from . import db`` 处失败。
    """
    if _PKG in sys.modules:
        return
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
    pkg.__package__ = _PKG
    sys.modules[_PKG] = pkg
    for sub in ("core", "data", "inbound", "llm", "platform"):
        name = f"{_PKG}.{sub}"
        mod = types.ModuleType(name)
        mod.__path__ = [str(PLUGIN_ROOT / sub)]  # type: ignore[attr-defined]
        mod.__package__ = name
        sys.modules[name] = mod
        setattr(pkg, sub, mod)


def _mods() -> _Mods:
    global _mods_obj
    if _mods_obj is not None:
        return _mods_obj
    with _mods_lock:
        if _mods_obj is not None:
            return _mods_obj
        _preload_env()
        _install_synthetic_package()
        obj = _Mods()
        obj.account_store = importlib.import_module(f"{_PKG}.core.account_store")
        obj.accounts = importlib.import_module(f"{_PKG}.core.accounts")
        obj.console_admin = importlib.import_module(f"{_PKG}.core.console_admin")
        obj.join_welcome = importlib.import_module(f"{_PKG}.inbound.join_welcome")
        obj.db = importlib.import_module(f"{_PKG}.data.db")
        obj.repo_console = importlib.import_module(f"{_PKG}.data.repo_console")
        obj.repo_messages = importlib.import_module(f"{_PKG}.data.repo_messages")
        obj.repo_hf = importlib.import_module(f"{_PKG}.data.repo_hf")
        obj.repo_style = importlib.import_module(f"{_PKG}.data.repo_style")
        obj.repo_affection = importlib.import_module(f"{_PKG}.data.repo_affection")
        obj.pairing = importlib.import_module(f"{_PKG}.platform.pairing")
        _mods_obj = obj
        return obj


# ---------------------------------------------------------------- 小工具

def _version() -> str:
    try:
        return str(json.loads(_MANIFEST.read_text(encoding="utf-8")).get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


def _status_candidates() -> list[Path]:
    """快照候选路径，按优先级排列。

    次级 profile 的插件后端跑在该 profile 的 plugin host 里，``HERMES_HOME`` 指向
    ``profiles/<name>``；而网关进程的 ``HERMES_HOME`` 是 launch home，快照写在
    ``<launch>/plugin-data/...``。所以先看当前 home，再回退到 launch home / ``~/.hermes``。
    """
    home = Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()
    homes = [home]
    if home.parent.name == "profiles":
        homes.append(home.parent.parent)
    default = Path("~/.hermes").expanduser()
    if default not in homes:
        homes.append(default)
    return [h / "plugin-data" / "wechatpadpro" / "console-status.json" for h in homes]


def _status_path() -> Path:
    candidates = _status_candidates()
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def _read_status() -> dict[str, Any] | None:
    try:
        data = json.loads(_status_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 缺失/坏文件都按 unknown
        return None
    return data if isinstance(data, dict) else None


def _status_view(status: Mapping[str, Any] | None, account_id: str) -> dict[str, Any]:
    mods = _mods()
    if not status:
        return {"state": "unknown", "age_sec": None, "written_at": None,
                "connected": False, "reason": "snapshot_missing",
                "profile": "", "account": {}}
    classified = mods.console_admin.status_classify(status.get("written_at"))
    acct = (status.get("accounts") or {}).get(account_id) or {}
    return {
        **classified,
        "written_at": status.get("written_at"),
        "connected": bool(status.get("connected")),
        "reason": str(status.get("reason") or ""),
        "profile": str(status.get("profile") or ""),
        "pid": status.get("pid"),
        "account": dict(acct) if isinstance(acct, Mapping) else {},
    }


def _account_ids() -> list[str]:
    mods = _mods()
    return mods.accounts.list_account_ids()


def _require_account(account_id: str) -> None:
    if account_id not in _account_ids():
        raise HTTPException(status_code=404, detail=f"账号 {account_id!r} 不存在")


def _read_raw(account_id: str) -> dict[str, Any]:
    mods = _mods()
    try:
        return mods.account_store.read_account_json(account_id)
    except mods.account_store.AccountWriteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _summary(account_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": account_id,
        "enabled": bool(raw.get("enabled", True)),
        "agent": str(raw.get("agent") or ""),
        "nickname": str(raw.get("nickname") or ""),
        "selfWxid": str(raw.get("selfWxid") or ""),
    }


def _jsonable(value: Any) -> Any:
    """把 ``resolve_cfg`` 的 frozenset 等转成 JSON 类型（set 排好序，便于展示）。"""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(v) for v in value), key=lambda x: str(x))
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _no_store(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=_jsonable(payload), status_code=status_code,
                        headers={"Cache-Control": "no-store"})


def _db_failure(account_id: str, exc: BaseException) -> dict[str, Any]:
    # 不回传 DSN/user/异常原文 —— 只记类型名，足够定位"库连不上"。
    log.warning("[WPP console] 群统计不可用（%s）: %s", account_id, type(exc).__name__)
    return {"db_available": False, "since_hours": 24, "truncated": False,
            "totals": {"inbound": 0, "outbound": 0}, "groups": []}


def _db_unavailable(account_id: str, exc: BaseException, **shape: Any) -> dict[str, Any]:
    """按端点返回各自的空结构（前端据此显示"库不可用"，而不是 500）。"""
    log.warning("[WPP console] 查询降级（%s）: %s", account_id, type(exc).__name__)
    return {"db_available": False, **shape}


def _pop_expected(payload: dict[str, Any]) -> int | None:
    """取出并移除 ``expected_mtime_ns``（校验器不认识这个键）。

    缺省/``None`` = 不做并发校验；bool / 负数 / 非整数一律 400。
    """
    if "expected_mtime_ns" not in payload:
        return None
    value = payload.pop("expected_mtime_ns")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HTTPException(status_code=400, detail="expected_mtime_ns 必须是非负整数")
    return int(value)


def _env_key_names() -> set[str]:
    """所有 ``.env`` 文件 + 进程 env 里**出现过的键名**（只收键名，不读值）。"""
    names = set(os.environ)
    for path in _env_files():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.partition("=")[0].strip()
            if key.startswith("export "):
                key = key[7:].strip()
            if key:
                names.add(key)
    return names


#: 诊断页 env 存在的友好标签 → 候选键名（只返回 bool）。
_ENV_PRESENCE = {
    "api_base": ("WECHATPRO_API_BASE", "WECHATPRO_WS_URL"),
    "authcode": ("WECHATPRO_AUTHCODE", "WECHATPRO_AUTH_CODE"),
    "token_key": ("WECHATPRO_TOKEN_KEY",),
    "db_backend": ("WECHATPRO_DB_BACKEND",),
    "db_host": ("WECHATPRO_DB_HOST",),
    "db_name": ("WECHATPRO_DB_NAME",),
    "sqlite_path": ("WECHATPRO_SQLITE_PATH",),
    "webhook_secret": ("WECHATPRO_WEBHOOK_SECRET",),
}


def _env_presence() -> dict[str, bool]:
    names = _env_key_names()
    return {label: any(key in names for key in keys)
            for label, keys in _ENV_PRESENCE.items()}


def _id_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(x).strip() for x in value if str(x).strip()}


def _heartflow_warnings(raw: Mapping[str, Any], hf_view: Mapping[str, Any],
                        group_policy: str) -> list[str]:
    """心流配置的显式告警（只读计算）。"""
    out: list[str] = []
    if not hf_view.get("enabled"):
        return out
    whitelist = [str(g) for g in hf_view.get("whitelistGroups") or ()]
    if not whitelist:
        out.append("心流白名单为空：所有群都允许心流")
    elif group_policy == "allowlist":
        allow = _id_set(raw.get("groupAllowFrom"))
        missing = [g for g in whitelist if g not in allow]
        if missing:
            out.append(f"{len(missing)} 个心流白名单群不在 groupAllowFrom 里（群策略=allowlist 时不会处理）")
    if not hf_view.get("modelConfigured"):
        out.append("心流已启用但没有配置 heartflow.model，判定会直接失败")
    return out


def _write_and_respond(account_id: str, mutate, *, log_keys: set[str],
                       expected_mtime_ns: int | None = None,
                       skip_if_unchanged: bool = False,
                       extra: Any = None):
    mods = _mods()
    try:
        data, mtime_ns = mods.account_store.update_account(
            account_id, mutate, changed_keys=set(log_keys), log_keys=set(log_keys),
            skip_if_unchanged=skip_if_unchanged,
            expected_mtime_ns=expected_mtime_ns)
    except mods.console_admin.ConsoleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except mods.account_store.AccountConflictError as exc:
        # 结构化 detail：前端要从 body.detail.current_mtime_ns 读出当前版本。
        raise HTTPException(status_code=409, detail={
            "code": "mtime_conflict",
            "message": str(exc),
            "current_mtime_ns": exc.current_mtime_ns,
        }) from exc
    except mods.account_store.AccountWriteError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "write_failed",
            "message": str(exc),
        }) from exc
    payload: dict[str, Any] = {
        "ok": True, "updated_keys": sorted(log_keys), "mtime_ns": mtime_ns}
    if callable(extra):
        payload.update(extra(data))
    elif extra:
        payload.update(extra)
    return _no_store(payload)


# ---------------------------------------------------------------- 路由

@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": _version()}


@router.get("/overview")
def overview(account: str = Query("", description="要统计的账号 id（默认第一个）")):
    try:
        ids = _account_ids()
    except Exception as exc:  # noqa: BLE001 —— 账号目录坏了不该 500
        log.warning("[WPP console] 读取账号列表失败: %s", type(exc).__name__)
        return _no_store({"ok": False, "accounts": [], "error": "账号目录不可读"})

    accounts = []
    raws: dict[str, dict[str, Any]] = {}
    for aid in ids:
        try:
            raw = _read_raw(aid)
        except HTTPException:
            continue
        raws[aid] = raw
        accounts.append(_summary(aid, raw))

    selected = account if account in raws else (ids[0] if ids else "")
    status = _read_status()
    stats: dict[str, Any] = {}
    if selected:
        mods = _mods()
        try:
            res = mods.repo_console.list_group_activity(selected, since_hours=24)
        except Exception as exc:  # noqa: BLE001
            res = _db_failure(selected, exc)
        stats = {
            "db_available": bool(res.get("db_available")),
            "since_hours": res.get("since_hours", 24),
            "totals": res.get("totals", {"inbound": 0, "outbound": 0}),
            "group_count": int(res.get("total") or len(res.get("groups") or [])),
            "truncated": bool(res.get("truncated")),
        }
    return _no_store({
        "ok": True,
        "version": _version(),
        "accounts": accounts,
        "selected": selected,
        "status": _status_view(status, selected),
        # 全账号状态表：每项只含白名单字段（WS/热加载/快照新鲜度）。
        "account_statuses": {
            aid: _mods().console_admin.account_status_view(status, aid) for aid in ids
        },
        "stats": stats,
        "warnings": _status_warnings(status, selected),
    })


def _status_warnings(status: Mapping[str, Any] | None, account_id: str) -> list[str]:
    out: list[str] = []
    view = _status_view(status, account_id)
    if view["state"] == "unknown":
        out.append("读不到网关状态快照：网关可能未运行，或控制台与网关不在同一 HERMES_HOME")
    elif view["state"] == "stale":
        out.append("状态快照已过期：网关可能卡住或控制台指向了旧快照")
    if status and status.get("reason") == "no_connectable_accounts":
        out.append("网关没有可连接的账号（检查 authcode / enabled）")
    if account_id and view.get("account") and not view["account"].get("ws_connected"):
        out.append("该账号当前未连上 WS（配置仍可编辑，保存后由网关热加载）")
    return out


@router.get("/accounts/{account_id}")
def account_detail(account_id: str):
    _require_account(account_id)
    mods = _mods()
    raw = _read_raw(account_id)
    try:
        mtime_ns = int(mods.account_store.account_path(account_id).stat().st_mtime_ns)
    except OSError:
        mtime_ns = 0
    try:
        effective = mods.join_welcome.resolve_cfg(raw.get("joinWelcome"))
    except Exception:  # noqa: BLE001 —— 坏配置不该让整页打不开
        effective = {}
    view = mods.console_admin.safe_account_view(
        account_id, raw, join_welcome_effective=effective, mtime_ns=mtime_ns)
    view["heartflowWarnings"] = _heartflow_warnings(
        raw, view.get("heartflow") or {}, str(view.get("groupPolicy") or ""))
    return _no_store({
        "ok": True,
        "account": view,
        "status": _status_view(_read_status(), account_id),
    })


def _validate_or_400(fn, payload: dict[str, Any]) -> Any:
    mods = _mods()
    try:
        return fn(payload)
    except mods.console_admin.ConsoleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/accounts/{account_id}/policy")
def update_policy(account_id: str, payload: dict[str, Any]):
    _require_account(account_id)
    mods = _mods()
    expected = _pop_expected(payload)
    validated = _validate_or_400(mods.console_admin.validate_policy, payload)
    return _write_and_respond(
        account_id, lambda raw: {**raw, **validated}, log_keys=set(validated),
        expected_mtime_ns=expected)


@router.put("/accounts/{account_id}/join-welcome")
def update_join_welcome(account_id: str, payload: dict[str, Any]):
    _require_account(account_id)
    mods = _mods()
    expected = _pop_expected(payload)
    validated = _validate_or_400(mods.console_admin.validate_join_welcome, payload)

    def _mutate(raw: dict[str, Any]) -> dict[str, Any]:
        data = dict(raw)
        data["joinWelcome"] = mods.console_admin.merge_join_welcome(
            data.get("joinWelcome"), validated)
        return data

    return _write_and_respond(account_id, _mutate, log_keys={"joinWelcome"},
                              expected_mtime_ns=expected)


@router.put("/accounts/{account_id}/features")
def update_features(account_id: str, payload: dict[str, Any]):
    _require_account(account_id)
    mods = _mods()
    expected = _pop_expected(payload)
    validated = _validate_or_400(mods.console_admin.validate_features, payload)

    def _mutate(raw: dict[str, Any]) -> dict[str, Any]:
        data = dict(raw)
        for key, enabled in validated.items():
            if key == "xiaoweiEnabled":
                data["xiaoweiEnabled"] = enabled
            else:
                data[key] = mods.console_admin.merge_feature_section(
                    data.get(key), enabled)
        return data

    return _write_and_respond(account_id, _mutate, log_keys=set(validated),
                              expected_mtime_ns=expected)


# ---------------------------------------------------------------- 群与成员

def _require_group_id(group_id: str) -> str:
    gid = str(group_id or "").strip()
    if not gid or not gid.endswith("@chatroom"):
        raise HTTPException(status_code=400, detail=f"群 id 必须以 @chatroom 结尾：{gid!r}")
    return gid


def _group_flags(raw: Mapping[str, Any], gid: str) -> dict[str, bool]:
    allow = _id_set(raw.get("groupAllowFrom"))
    black = _id_set(raw.get("blacklistGroups"))
    hf = raw.get("heartflow")
    whitelist = _id_set(hf.get("whitelistGroups")) if isinstance(hf, Mapping) else set()
    return {
        "allowlisted": gid in allow,
        "blacklisted": gid in black,
        "heartflow_whitelisted": gid in whitelist,
    }


@router.get("/accounts/{account_id}/groups")
def account_groups(account_id: str,
                   since_hours: int = Query(24, ge=1, le=720),
                   q: str = Query("", max_length=100),
                   sort: str = Query("activity"),
                   limit: int = Query(50, ge=1, le=500),
                   offset: int = Query(0, ge=0, le=100000)):
    _require_account(account_id)
    mods = _mods()
    sort_key = str(sort or "activity").strip().lower()
    if sort_key not in ("activity", "name", "members"):
        raise HTTPException(status_code=400, detail="sort 只能是 activity / name / members")
    try:
        res = mods.repo_console.list_groups_page(
            account_id, q=q, sort=sort_key, since_hours=since_hours,
            limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001 —— 控制台降级，不 500
        res = _db_failure(account_id, exc)
        res.update({"q": q, "sort": sort_key, "limit": limit, "offset": offset,
                    "total": 0, "has_more": False})
    raw = _read_raw(account_id)
    # 复制每项再挂 policy 标记 —— 缓存里的 dict 不能被就地改。
    res["groups"] = [{**g, "policy": _group_flags(raw, str(g.get("chatroom_id") or ""))}
                     for g in res.get("groups") or []]
    res["sort"] = sort_key
    return _no_store({"ok": True, **res})


@router.get("/accounts/{account_id}/groups/{group_id}")
def group_detail_route(account_id: str, group_id: str,
                       since_hours: int = Query(24, ge=1, le=720)):
    _require_account(account_id)
    gid = _require_group_id(group_id)
    mods = _mods()
    try:
        res = mods.repo_console.get_group_detail(
            account_id, gid, since_hours=since_hours)
    except Exception as exc:  # noqa: BLE001
        res = _db_unavailable(account_id, exc, since_hours=since_hours, found=False,
                              group={"chatroom_id": gid})
    raw = _read_raw(account_id)
    group_view = res.get("group") or {}
    if res.get("db_available") and not res.get("found") \
            and not group_view.get("inbound") and not group_view.get("outbound"):
        raise HTTPException(status_code=404, detail=f"群 {gid!r} 没有本地记录")
    group = dict(res.get("group") or {})
    group["policy"] = _group_flags(raw, gid)
    res["group"] = group
    return _no_store({"ok": True, **res})


@router.get("/accounts/{account_id}/groups/{group_id}/members")
def group_members(account_id: str, group_id: str,
                  q: str = Query("", max_length=100),
                  limit: int = Query(50, ge=1, le=100),
                  offset: int = Query(0, ge=0, le=5000)):
    _require_account(account_id)
    gid = _require_group_id(group_id)
    mods = _mods()
    try:
        res = mods.repo_console.list_group_members_page(
            account_id, gid, q=q, limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001
        res = _db_unavailable(account_id, exc, group_id=gid, q=q, limit=limit,
                              offset=offset, total=0, has_more=False, members=[])
    return _no_store({"ok": True, **res})


@router.post("/accounts/{account_id}/groups/{group_id}/policy")
def group_policy_action(account_id: str, group_id: str, payload: dict[str, Any]):
    _require_account(account_id)
    gid = _require_group_id(group_id)
    mods = _mods()
    expected = _pop_expected(payload)
    action = _validate_or_400(mods.console_admin.validate_group_action,
                              payload.get("action"))
    result: dict[str, Any] = {"changed": False}

    def _mutate(raw: dict[str, Any]) -> dict[str, Any]:
        allow, black = mods.console_admin.merge_group_policy(
            raw.get("groupAllowFrom"), raw.get("blacklistGroups"), gid, action)
        data = dict(raw)
        data["groupAllowFrom"] = allow
        data["blacklistGroups"] = black
        result.update({
            "changed": allow != list(raw.get("groupAllowFrom") or [])
            or black != list(raw.get("blacklistGroups") or []),
            "action": action,
            "group_id": gid,
            "groupAllowFrom": allow,
            "blacklistGroups": black,
        })
        return data

    return _write_and_respond(
        account_id, _mutate, log_keys={"groupAllowFrom", "blacklistGroups"},
        expected_mtime_ns=expected, skip_if_unchanged=True,
        extra=lambda _data: dict(result))


@router.get("/accounts/{account_id}/groups/{group_id}/insights")
def group_insights_route(account_id: str, group_id: str):
    _require_account(account_id)
    gid = _require_group_id(group_id)
    mods = _mods()
    try:
        res = mods.repo_console.group_insights(account_id, gid)
    except Exception as exc:  # noqa: BLE001
        res = _db_unavailable(account_id, exc, group_id=gid, heartflow={},
                              style=None, affection=None)
    return _no_store({"ok": True, **res})


# ---------------------------------------------------------------- 消息与分析

@router.get("/accounts/{account_id}/messages")
def search_messages(account_id: str,
                    q: str = Query("", max_length=100),
                    group_id: str = Query("", max_length=128),
                    sender: str = Query("", max_length=128),
                    direction: str = Query(""),
                    since_hours: int = Query(168, ge=1, le=720),
                    limit: int = Query(50, ge=1, le=50),
                    offset: int = Query(0, ge=0, le=5000)):
    _require_account(account_id)
    mods = _mods()
    gid = str(group_id or "").strip()
    if gid and not gid.endswith("@chatroom"):
        raise HTTPException(status_code=400, detail="group_id 必须以 @chatroom 结尾")
    if not str(q or "").strip() and not gid and not str(sender or "").strip():
        raise HTTPException(status_code=400, detail="至少提供关键词/群/发送者之一")
    direction_value = _validate_or_400(mods.console_admin.validate_direction, direction)
    try:
        res = mods.repo_console.search_messages_page(
            account_id, q=q, group_id=gid, sender=sender, direction=direction_value,
            since_hours=since_hours, limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001
        res = _db_unavailable(account_id, exc, since_hours=since_hours, limit=limit,
                              offset=offset, total=0, has_more=False, messages=[])
    return _no_store({"ok": True, **res})


@router.get("/accounts/{account_id}/analytics")
def analytics(account_id: str, hours: int = Query(24, ge=1, le=168)):
    _require_account(account_id)
    mods = _mods()
    try:
        res = mods.repo_console.message_analytics(account_id, hours=hours)
    except Exception as exc:  # noqa: BLE001
        res = _db_unavailable(
            account_id, exc, hours=hours, bucket_sec=3600, buckets=[], types=[],
            totals={"inbound": 0, "outbound": 0}, last_message_at=None)
    return _no_store({"ok": True, **res})


# ---------------------------------------------------------------- 心流

@router.get("/accounts/{account_id}/heartflow")
def heartflow_detail(account_id: str, hours: int = Query(24, ge=1, le=168)):
    _require_account(account_id)
    mods = _mods()
    raw = _read_raw(account_id)
    config = mods.console_admin.heartflow_config_view(raw)
    try:
        res = mods.repo_console.heartflow_overview(account_id, hours=hours)
    except Exception as exc:  # noqa: BLE001
        res = _db_unavailable(
            account_id, exc, hours=hours, stats={"total": 0, "by_status": {},
                                                 "by_suppressed_reason": {}},
            budget=[], group_states=[], recent=[], last_sent_at=None)
    return _no_store({
        "ok": True,
        "config": config,
        "warnings": _heartflow_warnings(raw, config,
                                        str(raw.get("groupPolicy") or "open")),
        **res,
    })


@router.put("/accounts/{account_id}/heartflow")
def update_heartflow(account_id: str, payload: dict[str, Any]):
    _require_account(account_id)
    mods = _mods()
    expected = _pop_expected(payload)
    validated = _validate_or_400(mods.console_admin.validate_heartflow, payload)

    def _mutate(raw: dict[str, Any]) -> dict[str, Any]:
        data = dict(raw)
        data["heartflow"] = mods.console_admin.merge_heartflow(
            data.get("heartflow"), validated)
        return data

    def _extra(data: dict[str, Any]) -> dict[str, Any]:
        config = mods.console_admin.heartflow_config_view(data)
        return {"warnings": _heartflow_warnings(
            data, config, str(data.get("groupPolicy") or "open"))}

    return _write_and_respond(account_id, _mutate, log_keys={"heartflow"},
                              expected_mtime_ns=expected, extra=_extra)


# ---------------------------------------------------------------- 风格 / 好感度

@router.get("/accounts/{account_id}/style-cards")
def style_cards(account_id: str,
                group_id: str = Query("", max_length=128)):
    _require_account(account_id)
    mods = _mods()
    gid = str(group_id or "").strip()
    if gid and not gid.endswith("@chatroom"):
        raise HTTPException(status_code=400, detail="group_id 必须以 @chatroom 结尾")
    try:
        res = mods.repo_console.style_cards_view(account_id, group_id=gid)
        versions = (mods.repo_console.style_card_versions(account_id, gid)["versions"]
                    if gid else [])
    except Exception as exc:  # noqa: BLE001
        res = _db_unavailable(account_id, exc, group_id=gid, cards=[])
        versions = []
    return _no_store({"ok": True, "versions": versions, **res})


@router.get("/accounts/{account_id}/style-cards/{group_id}/versions")
def style_versions(account_id: str, group_id: str,
                   limit: int = Query(20, ge=1, le=100)):
    _require_account(account_id)
    gid = _require_group_id(group_id)
    mods = _mods()
    try:
        res = mods.repo_console.style_card_versions(account_id, gid, limit=limit)
    except Exception as exc:  # noqa: BLE001
        res = _db_unavailable(account_id, exc, group_id=gid, versions=[])
    return _no_store({"ok": True, **res})


@router.get("/accounts/{account_id}/style-cards/{group_id}/versions/{version}")
def style_version_detail(account_id: str, group_id: str, version: int):
    _require_account(account_id)
    gid = _require_group_id(group_id)
    mods = _mods()
    try:
        res = mods.repo_console.style_card_version(account_id, gid, version)
    except Exception as exc:  # noqa: BLE001
        res = _db_unavailable(account_id, exc, found=False, version=version)
    if not res.get("found"):
        raise HTTPException(status_code=404, detail="风格卡版本不存在")
    return _no_store({"ok": True, **res})


@router.get("/accounts/{account_id}/affection")
def affection_detail(account_id: str,
                     group_id: str = Query("", max_length=128),
                     limit: int = Query(100, ge=1, le=100)):
    _require_account(account_id)
    gid = _require_group_id(group_id)
    mods = _mods()
    try:
        res = mods.repo_console.affection_view(account_id, gid, limit=limit)
    except Exception as exc:  # noqa: BLE001
        res = _db_unavailable(account_id, exc, group_id=gid, total=0,
                              user_count=0, users=[], mood=None)
    return _no_store({"ok": True, **res})


# ---------------------------------------------------------------- 运维诊断

@router.get("/accounts/{account_id}/diagnostics")
def diagnostics(account_id: str):
    _require_account(account_id)
    mods = _mods()
    raw = _read_raw(account_id)
    status = _read_status()
    status_view = mods.console_admin.account_status_view(status, account_id)

    try:
        backend = str(mods.db.backend())
    except Exception:  # noqa: BLE001
        backend = "unknown"
    try:
        db_ok = bool(mods.db.is_available())
        db_error = ""
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_error = type(exc).__name__
    finally:
        try:
            mods.db.close()
        except Exception as exc:  # noqa: BLE001 —— 关连接失败不影响诊断结论
            log.debug("[WPP console] db.close 失败: %s", type(exc).__name__)
    health = mods.db.health()

    env_names = _env_key_names()
    authcode_present = bool(str(raw.get("authcode") or "").strip()) \
        or "WECHATPRO_AUTHCODE" in env_names
    token_key_present = bool(str(raw.get("tokenKey") or "").strip()) \
        or "WECHATPRO_TOKEN_KEY" in env_names
    heartflow = mods.console_admin.heartflow_config_view(raw)
    checks = mods.console_admin.diagnostics_checks(
        db_ok=db_ok, db_error=db_error,
        snapshot_state=str(status_view.get("state") or "unknown"),
        ws_connected=bool(status_view.get("ws_connected")),
        authcode_present=authcode_present,
        token_key_present=token_key_present,
        allow_all_users=raw.get("allowAllUsers") is True,
        dm_policy=str(raw.get("dmPolicy") or ""),
        heartflow_enabled=bool(heartflow.get("enabled")),
        heartflow_model_configured=bool(heartflow.get("modelConfigured")),
    )
    return _no_store({
        "ok": True,
        "version": _version(),
        "generated_at": int(time.time()),
        "db": {
            "backend": backend,
            "available": db_ok,
            "process_health_state": str(health.get("state") or "unknown"),
            "process_health_at": int(health.get("at") or 0),
            "note": "dashboard 进程的 health 是进程内状态；迁移结论由网关侧决定",
        },
        "snapshot": status_view,
        "env": {"presence": _env_presence()},
        "credential_presence": {
            "authcode": authcode_present,
            "tokenKey": token_key_present,
            "webhookSecret": "WECHATPRO_WEBHOOK_SECRET" in env_names,
        },
        "checks": checks,
    })


# ---------------------------------------------------------------- 热加载 / 配对

def _touch_account(account_id: str) -> dict[str, Any]:
    """只抬高 mtime（改内容会备份+清覆盖；热加载 watcher 认的就是 mtime）。"""
    mods = _mods()
    path = mods.account_store.account_path(account_id)
    lock_factory = getattr(mods.account_store, "_cross_process_lock", None)
    lock_cm = lock_factory(account_id) if lock_factory else _nullcontext()
    with lock_cm:
        st = path.stat()
        new_ns = max(time.time_ns(), st.st_mtime_ns + 1_000_000_000)
        os.utime(path, ns=(st.st_atime_ns, new_ns))
    return {"previous_mtime_ns": int(st.st_mtime_ns), "mtime_ns": int(new_ns),
            "bumped_ns": int(new_ns - st.st_mtime_ns)}


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *_exc: Any) -> bool:
        return False


@router.post("/accounts/{account_id}/reload")
def reload_account(account_id: str):
    _require_account(account_id)
    try:
        result = _touch_account(account_id)
    except OSError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "reload_failed", "message": f"账号文件不可写：{type(exc).__name__}"}) from exc
    view = _status_view(_read_status(), account_id)
    warning = None
    if view.get("state") not in ("live", "delayed"):
        warning = "mtime 已更新，但状态快照不新鲜：网关可能未运行，热加载未确认"
    elif not (view.get("account") or {}).get("ws_connected"):
        warning = "该账号当前未连接 WS；watcher 只扫已连接账号，热加载可能未发生"
    return _no_store({"ok": True, **result,
                      "snapshot_age_sec": view.get("age_sec"),
                      "warning": warning})


@router.post("/ops/reload-all")
def reload_all():
    results: list[dict[str, Any]] = []
    for aid in _account_ids():
        try:
            item = _touch_account(aid)
            results.append({"account_id": aid, "ok": True, **item})
        except Exception as exc:  # noqa: BLE001 —— 单账号失败不影响整批
            results.append({"account_id": aid, "ok": False,
                            "error": type(exc).__name__})
    return _no_store({"ok": True, "results": results})


def _pairing_summary(account_id: str) -> dict[str, Any]:
    mods = _mods()
    entry = mods.pairing.read_pairing_code(account_id)
    paired = mods.pairing.load_paired(account_id)
    now_ms = int(time.time() * 1000)
    expires_at = int(entry["expires_at"]) if entry else None
    return {
        "has_code": bool(entry),
        "expires_at": expires_at,
        "expires_in_sec": (max(0, (expires_at - now_ms) // 1000)
                           if expires_at else 0),
        "paired_count": len(paired),
        "paired_users": paired[:500],
        "users_truncated": len(paired) > 500,
    }


@router.get("/accounts/{account_id}/pairing")
def pairing_state(account_id: str):
    _require_account(account_id)
    return _no_store({"ok": True, **_pairing_summary(account_id)})


@router.post("/accounts/{account_id}/pairing")
def pairing_generate(account_id: str):
    _require_account(account_id)
    mods = _mods()
    try:
        entry = mods.pairing.generate_pairing_code(account_id)
    except OSError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "pairing_write_failed",
            "message": f"配对文件写入失败：{type(exc).__name__}"}) from exc
    summary = _pairing_summary(account_id)
    now_ms = int(time.time() * 1000)
    return _no_store({
        "ok": True,
        # 配对码**只在这里返回一次**；之后的 GET 永远不带 code。
        "code": str(entry.get("code") or ""),
        "expires_at": int(entry.get("expires_at") or 0),
        "expires_in_sec": max(0, (int(entry.get("expires_at") or 0) - now_ms) // 1000),
        "paired_count": summary["paired_count"],
    })


@router.delete("/accounts/{account_id}/pairing/{wxid}")
def pairing_remove(account_id: str, wxid: str):
    _require_account(account_id)
    mods = _mods()
    target = str(wxid or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="wxid 不能为空")
    try:
        removed = bool(mods.pairing.remove_paired(account_id, target))
    except OSError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "pairing_write_failed",
            "message": f"配对文件写入失败：{type(exc).__name__}"}) from exc
    return _no_store({"ok": True, "removed": removed, "wxid": target,
                      **_pairing_summary(account_id)})
