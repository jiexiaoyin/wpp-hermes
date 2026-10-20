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
    for sub in ("core", "data", "inbound", "llm"):
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
            "group_count": len(res.get("groups") or []),
            "truncated": bool(res.get("truncated")),
        }
    return _no_store({
        "ok": True,
        "version": _version(),
        "accounts": accounts,
        "selected": selected,
        "status": _status_view(status, selected),
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


def _write_and_respond(account_id: str, mutate, *, log_keys: set[str]):
    mods = _mods()
    try:
        _data, mtime_ns = mods.account_store.update_account(
            account_id, mutate, changed_keys=set(log_keys), log_keys=set(log_keys))
    except mods.console_admin.ConsoleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except mods.account_store.AccountWriteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _no_store({"ok": True, "updated_keys": sorted(log_keys), "mtime_ns": mtime_ns})


@router.put("/accounts/{account_id}/policy")
def update_policy(account_id: str, payload: dict[str, Any]):
    _require_account(account_id)
    mods = _mods()
    validated = _validate_or_400(mods.console_admin.validate_policy, payload)
    return _write_and_respond(
        account_id, lambda raw: {**raw, **validated}, log_keys=set(validated))


@router.put("/accounts/{account_id}/join-welcome")
def update_join_welcome(account_id: str, payload: dict[str, Any]):
    _require_account(account_id)
    mods = _mods()
    validated = _validate_or_400(mods.console_admin.validate_join_welcome, payload)

    def _mutate(raw: dict[str, Any]) -> dict[str, Any]:
        data = dict(raw)
        data["joinWelcome"] = mods.console_admin.merge_join_welcome(
            data.get("joinWelcome"), validated)
        return data

    return _write_and_respond(account_id, _mutate, log_keys={"joinWelcome"})


@router.put("/accounts/{account_id}/features")
def update_features(account_id: str, payload: dict[str, Any]):
    _require_account(account_id)
    mods = _mods()
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

    return _write_and_respond(account_id, _mutate, log_keys=set(validated))


@router.get("/accounts/{account_id}/groups")
def account_groups(account_id: str,
                   since_hours: int = Query(24, ge=1, le=24 * 30),
                   limit: int = Query(200, ge=1, le=500)):
    _require_account(account_id)
    mods = _mods()
    try:
        res = mods.repo_console.list_group_activity(
            account_id, since_hours=since_hours, limit=limit)
    except Exception as exc:  # noqa: BLE001 —— 控制台降级，不 500
        res = _db_failure(account_id, exc)
    return _no_store({"ok": True, **res})
