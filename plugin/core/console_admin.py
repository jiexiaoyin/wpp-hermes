"""Web 控制台的纯逻辑：校验、合并、安全视图、状态快照。

## 为什么单独一个模块

``dashboard/plugin_api.py`` 以**顶层模块**加载（``__package__`` 为空、插件目录不在
``sys.path``），而且 FastAPI / 线程池不该和校验逻辑绑在一起。本模块**只用标准库**，
可以在网关、dashboard、单测三种环境里直接 import。

## 三条硬约束

1. **永不产出凭据**：控制台的账号视图是**白名单**拼出来的，不是"从 raw 删几个键"。
   raw 里有 ``authcode`` / ``tokenKey`` / ``webhookSecret`` / ``webhookPathToken``，
   任何"先 dump 再过滤"的写法都可能漏掉新键。
2. **只做增量合并**：``joinWelcome`` / 能力 section 都是
   ``{**existing, **validated}``，保留本树没建模的同级键（阈值、model、allowFrom …）。
   整段替换会静默删配置 —— 与 ``core/account_store`` 的约束同源。
3. **拒绝未知值**：准入枚举、静默小时、``maxBatch`` 都在这里 fail-closed；
   运行时对未知值大多有兜底，但控制台不该把"看起来生效了"写进文件。
"""
from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

#: 私聊准入（与 ``core/accounts.py`` / ``inbound/triggers.py`` 的运行时口径一致）。
DM_POLICIES = ("open", "allowlist", "blacklist", "pairing")
#: 群聊准入。``disabled`` / ``closed`` 都是"全静默"，保留两个历史名字。
GROUP_POLICIES = ("open", "allowlist", "blacklist", "disabled", "closed")
#: 控制台可切换的能力 section（都写成 ``{**existing, "enabled": bool}``）。
FEATURE_KEYS = ("heartflow", "affection", "jargon", "style", "tts", "proactive")

#: 准入名单上限。真实账号是几十到几百人；500 足够，同时挡住误贴一整份通讯录。
MAX_LIST_ITEMS = 500
MAX_ITEM_LEN = 128
MAX_TEMPLATE_LEN = 500
MAX_BATCH = 20
MAX_BATCH_FLOOR = 1

#: 准入 7 键（``employeeDenyList`` 是额外的可写键，单独处理）。
POLICY_KEYS = (
    "dmPolicy", "allowFrom", "blacklistUsers",
    "groupPolicy", "groupAllowFrom", "blacklistGroups", "adminUsers",
)

#: 快照新鲜度阈值（秒）。
LIVE_SEC = 15
DELAYED_SEC = 60


class ConsoleValidationError(ValueError):
    """请求体不合法 —— dashboard 直接回 400，**不落盘**。"""


# ------------------------------------------------------------------ 小工具

def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iter_items(value: Any) -> list[Any]:
    if isinstance(value, str):
        return [part for line in value.splitlines() for part in line.split(",")]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        return list(value)
    raise ConsoleValidationError("名单必须是数组或逗号/换行分隔的字符串")


def parse_id_list(value: Any, *, kind: str = "user") -> list[str]:
    """把 textarea / 数组解析成去重后的 id 列表。

    ``kind="group"``：每项必须以 ``@chatroom`` 结尾；``kind="user"``：不得包含
    ``@chatroom``（防止把群 id 贴进私聊白名单）。空字符串表示"清空名单"。
    """
    if value is None:
        return []
    items = []
    for raw in _iter_items(value):
        item = str(raw or "").strip()
        if not item:
            continue
        if len(item) > MAX_ITEM_LEN:
            raise ConsoleValidationError(f"名单项过长（>{MAX_ITEM_LEN} 字符）")
        if kind == "group" and not item.endswith("@chatroom"):
            raise ConsoleValidationError(f"群 id 必须以 @chatroom 结尾：{item!r}")
        if kind == "user" and "@chatroom" in item:
            raise ConsoleValidationError(f"私聊名单里不能出现群 id：{item!r}")
        if item not in items:
            items.append(item)
    if len(items) > MAX_LIST_ITEMS:
        raise ConsoleValidationError(f"名单最多 {MAX_LIST_ITEMS} 项")
    return items


def _normalize_enum(value: Any, allowed: tuple[str, ...], field: str,
                    *, legacy_open: bool = False) -> str:
    text = str(value or "").strip().lower()
    if legacy_open and not text:
        # 旧配置里 dmPolicy 可能为空，运行时按 open 处理；控制台写回时显式化。
        return "open"
    if text not in allowed:
        raise ConsoleValidationError(f"{field} 不认识：{text!r}（可选 {', '.join(allowed)}）")
    return text


# ------------------------------------------------------------------ 准入策略

def validate_policy(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验 ``PUT /accounts/{id}/policy`` 的请求体，返回**只含已提供键**的干净 dict。

    允许部分更新（UI 正常会提交全部 7 键）；未提供的键由调用方在 mutator 里保留。
    """
    if not isinstance(payload, Mapping):
        raise ConsoleValidationError("请求体必须是 JSON 对象")
    out: dict[str, Any] = {}
    if "dmPolicy" in payload:
        out["dmPolicy"] = _normalize_enum(
            payload["dmPolicy"], DM_POLICIES, "dmPolicy", legacy_open=True)
    if "groupPolicy" in payload:
        out["groupPolicy"] = _normalize_enum(
            payload["groupPolicy"], GROUP_POLICIES, "groupPolicy")
    for key, kind in (("allowFrom", "user"), ("blacklistUsers", "user"),
                      ("groupAllowFrom", "group"), ("blacklistGroups", "group"),
                      ("adminUsers", "user")):
        if key in payload:
            out[key] = parse_id_list(payload[key], kind=kind)
    if "employeeDenyList" in payload:
        out["employeeDenyList"] = parse_id_list(payload["employeeDenyList"], kind="user")
    unknown = set(payload) - set(POLICY_KEYS) - {"employeeDenyList"}
    if unknown:
        raise ConsoleValidationError(f"不认识的策略字段：{', '.join(sorted(unknown))}")
    if not out:
        raise ConsoleValidationError("没有可写的策略字段")
    return out


# ------------------------------------------------------------------ 进群欢迎

def validate_join_welcome(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验 ``PUT /accounts/{id}/join-welcome``，返回**只含已提供键**的干净 dict。"""
    if not isinstance(payload, Mapping):
        raise ConsoleValidationError("请求体必须是 JSON 对象")
    out: dict[str, Any] = {}
    known = {"enabled", "mode", "groups", "template", "llmPolish",
             "mention", "quietHours", "maxBatch"}
    unknown = set(payload) - known
    if unknown:
        raise ConsoleValidationError(f"不认识的欢迎语字段：{', '.join(sorted(unknown))}")
    if "enabled" in payload:
        out["enabled"] = _as_bool(payload["enabled"])
    if "mode" in payload:
        out["mode"] = _normalize_enum(payload["mode"], ("allowlist", "blocklist", "all"), "mode")
    if "groups" in payload:
        out["groups"] = parse_id_list(payload["groups"], kind="group")
    if "template" in payload:
        template = str(payload["template"] or "").strip()
        if not template:
            raise ConsoleValidationError("欢迎语模板不能为空")
        if len(template) > MAX_TEMPLATE_LEN:
            raise ConsoleValidationError(f"欢迎语模板过长（>{MAX_TEMPLATE_LEN} 字符）")
        # ``{name}`` 由运行时 resolve_cfg 自动补；这里**不写回**补齐后的值。
        out["template"] = template
    for key in ("llmPolish", "mention"):
        if key in payload:
            out[key] = _as_bool(payload[key])
    if "quietHours" in payload:
        hours = payload["quietHours"]
        if isinstance(hours, str):
            raise ConsoleValidationError(
                "quietHours 必须是 0–23 的整数数组（区间请在页面上展开）")
        if not isinstance(hours, Iterable) or isinstance(hours, (bytes, bytearray, Mapping)):
            raise ConsoleValidationError("quietHours 必须是 0–23 的整数数组")
        clean: list[int] = []
        for item in hours:
            hour = _as_int(item, -1)
            if hour < 0 or hour > 23:
                raise ConsoleValidationError(f"quietHours 只接受 0–23：{item!r}")
            if hour not in clean:
                clean.append(hour)
        out["quietHours"] = sorted(clean)
    if "maxBatch" in payload:
        batch = _as_int(payload["maxBatch"], -1)
        if batch < MAX_BATCH_FLOOR or batch > MAX_BATCH:
            raise ConsoleValidationError(
                f"maxBatch 必须在 {MAX_BATCH_FLOOR}–{MAX_BATCH} 之间")
        out["maxBatch"] = batch
    if not out:
        raise ConsoleValidationError("没有可写的欢迎语字段")
    return out


def merge_join_welcome(existing: Any, validated: Mapping[str, Any]) -> dict[str, Any]:
    """``{**existing, **validated}``；existing 不是对象时拒绝，避免整段覆盖。"""
    if existing is not None and not isinstance(existing, Mapping):
        raise ConsoleValidationError("账号里的 joinWelcome 不是对象，拒绝覆盖")
    return {**_as_dict(existing), **dict(validated)}


# ------------------------------------------------------------------ 能力开关

def validate_features(payload: Mapping[str, Any]) -> dict[str, bool]:
    """校验 ``PUT /accounts/{id}/features``：值为 bool，``xiaoweiEnabled`` 是顶层键。"""
    if not isinstance(payload, Mapping):
        raise ConsoleValidationError("请求体必须是 JSON 对象")
    allowed = set(FEATURE_KEYS) | {"xiaoweiEnabled"}
    unknown = set(payload) - allowed
    if unknown:
        raise ConsoleValidationError(f"不认识的能力字段：{', '.join(sorted(unknown))}")
    out: dict[str, bool] = {}
    for key, value in payload.items():
        if not isinstance(value, bool):
            raise ConsoleValidationError(f"{key} 必须是 true/false")
        out[key] = value
    if not out:
        raise ConsoleValidationError("没有可写的能力字段")
    return out


def merge_feature_section(existing: Any, enabled: bool) -> dict[str, Any]:
    """``{**existing, "enabled": bool}``；existing 不是对象时拒绝。"""
    if existing is not None and not isinstance(existing, Mapping):
        raise ConsoleValidationError("账号里的能力 section 不是对象，拒绝覆盖")
    return {**_as_dict(existing), "enabled": bool(enabled)}


# ------------------------------------------------------------------ 安全视图

def safe_account_view(
    account_id: str,
    raw: Mapping[str, Any],
    *,
    effective: Any = None,
    join_welcome_effective: Mapping[str, Any] | None = None,
    mtime_ns: int = 0,
) -> dict[str, Any]:
    """账号的**白名单**视图（凭据结构性不出现）。

    ``effective`` 可传 ``core.accounts.Account``（已解析），只取 agent/nickname/
    selfWxid/enabled 四个安全字段；解析失败就退回 raw。
    """
    raw = _as_dict(raw)
    view: dict[str, Any] = {
        "id": str(account_id),
        "enabled": bool(getattr(effective, "enabled", _as_bool(raw.get("enabled"), True))),
        "agent": str(getattr(effective, "agent", "") or raw.get("agent") or ""),
        "nickname": str(getattr(effective, "nickname", "") or raw.get("nickname") or ""),
        "selfWxid": str(getattr(effective, "self_wxid", "") or raw.get("selfWxid") or ""),
        "config_mtime_ns": int(mtime_ns or 0),
        # 历史兼容键：true 时运行时**绕过** dmPolicy 全放行（triggers.py）。
        "allowAllUsers": raw.get("allowAllUsers") is True,
        "employeeGroupIds": parse_id_list(raw.get("employeeGroupIds"), kind="group"),
        "employeeDenyList": parse_id_list(raw.get("employeeDenyList"), kind="user"),
        "employeeAllowFrom": [str(x) for x in (raw.get("employeeAllowFrom") or ())
                              if str(x).strip()][:MAX_LIST_ITEMS],
        "debounceMs": _as_int(raw.get("debounceMs"), 500),
        #: 只暴露"是否配了模型"，不暴露模型名（UI 用它决定心流开关是否可点）。
        "heartflowModelConfigured": bool(
            str(_as_dict(raw.get("heartflow")).get("model") or "").strip()),
        "features": feature_view(raw),
        "joinWelcome": {
            "raw": _as_dict(raw.get("joinWelcome")),
            "effective": dict(join_welcome_effective or {}),
        },
    }
    for key in POLICY_KEYS:
        value = raw.get(key)
        if key.endswith("Policy"):
            view[key] = str(value or "open").strip().lower()
        else:
            view[key] = [str(x) for x in (value or ()) if str(x).strip()][:MAX_LIST_ITEMS]
    view["warnings"] = policy_warnings(view)
    return view


def feature_view(raw: Mapping[str, Any]) -> dict[str, bool]:
    """各能力当前 ``enabled``（``xiaoweiEnabled`` 是顶层 bool）。"""
    out = {key: _as_bool(_as_dict(raw.get(key)).get("enabled")) for key in FEATURE_KEYS}
    out["xiaowei"] = raw.get("xiaoweiEnabled") is True
    return out


def policy_warnings(view: Mapping[str, Any]) -> list[str]:
    """UI 需要显式提示的配置陷阱（只读计算，不落盘）。"""
    warnings: list[str] = []
    dm = str(view.get("dmPolicy") or "")
    group = str(view.get("groupPolicy") or "")
    if view.get("allowAllUsers") and dm != "open":
        warnings.append("allowAllUsers=true 会绕过 dmPolicy，私聊仍是全放行")
    if dm == "allowlist" and not view.get("allowFrom"):
        warnings.append("dmPolicy=allowlist 且 allowFrom 为空：所有私聊都会被拒绝")
    if group == "allowlist" and not view.get("groupAllowFrom"):
        warnings.append("groupPolicy=allowlist 且 groupAllowFrom 为空：所有群都会被拒绝")
    if dm in ("open", "blacklist", "pairing") or group in ("open", "blacklist"):
        warnings.append("open/blacklist/pairing 仍需网关 WECHATPRO_ALLOW_ALL_USERS=1，否则消息到不了插件")
    return warnings


# ------------------------------------------------------------------ 状态快照

def status_classify(written_at: Any, now: float | None = None, *,
                    missing: bool = False) -> dict[str, Any]:
    """快照新鲜度：``live`` ≤15s、``delayed`` ≤60s、否则 ``stale``；缺失 ``unknown``。"""
    if missing or not written_at:
        return {"state": "unknown", "age_sec": None}
    try:
        written = float(written_at)
    except (TypeError, ValueError):
        return {"state": "unknown", "age_sec": None}
    age = max(0, int((time.time() if now is None else now) - written))
    if age <= LIVE_SEC:
        state = "live"
    elif age <= DELAYED_SEC:
        state = "delayed"
    else:
        state = "stale"
    return {"state": state, "age_sec": age}


def build_status_snapshot(
    *,
    profile: str,
    pid: int,
    started_at: int,
    connected: bool,
    reason: str,
    accounts: Mapping[str, Mapping[str, Any]],
    now: float | None = None,
) -> dict[str, Any]:
    """拼一份**不含凭据/策略值**的运行时快照（网关侧写盘用）。"""
    clean: dict[str, dict[str, Any]] = {}
    for aid, item in (accounts or {}).items():
        item = _as_dict(item)
        clean[str(aid)] = {
            "ws_connected": bool(item.get("ws_connected")),
            "ws_connected_once": bool(item.get("ws_connected_once")),
            "client_ready": bool(item.get("client_ready")),
            "config_mtime_ns": _as_int(item.get("config_mtime_ns"), 0),
            "last_reload_at": _as_int(item.get("last_reload_at"), 0),
            "last_reload_ok": item.get("last_reload_ok"),
            "last_reload_error": (str(item.get("last_reload_error") or "")[:300] or None),
        }
    return {
        "schema": 1,
        "profile": str(profile or ""),
        "pid": int(pid or 0),
        "started_at": int(started_at or 0),
        "written_at": int(time.time() if now is None else now),
        "connected": bool(connected),
        "reason": str(reason or ""),
        "accounts": clean,
    }
