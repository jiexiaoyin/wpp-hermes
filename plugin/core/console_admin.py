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

import math
import time
from collections.abc import Iterable, Mapping
from typing import Any

#: 私聊准入（与 ``core/accounts.py`` / ``inbound/triggers.py`` 的运行时口径一致）。
DM_POLICIES = ("open", "allowlist", "blacklist", "pairing")
#: 群聊准入。``disabled`` / ``closed`` 都是"全静默"，保留两个历史名字。
GROUP_POLICIES = ("open", "allowlist", "blacklist", "disabled", "closed")
#: 控制台可切换的能力 section（都写成 ``{**existing, "enabled": bool}``）。
FEATURE_KEYS = ("heartflow", "affection", "jargon", "style", "tts", "proactive")

#: 顶层高级开关。缺省一律 false（与 ``core/accounts.py`` 的解析口径一致）。
ADVANCED_FEATURE_KEYS = (
    "llmIntentEnabled", "groupContextEnabled", "mcpEnabled",
    "friendCirclePublishEnabled", "adminToolsEnabled", "dmPairingEnabled",
)

#: 防抖上限（毫秒）。运行时是 ``debounceMs``，0 表示不等待。
MAX_DEBOUNCE_MS = 10_000

#: 心流阈值展示回退（真源 ``inbound.heartflow.learn.HF_REPLY_THRESHOLD_FALLBACK``；
#: 本模块只用标准库，不 import 重型运行时，故复制一个常量并标 ``thresholdExplicit``）。
HF_THRESHOLD_FALLBACK = 0.6


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None

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

def validate_features(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验 ``PUT /accounts/{id}/features``。

    - 六个能力 section + ``xiaoweiEnabled`` + 六个高级顶层开关：只收 bool；
    - ``debounceMs``：0–10000 的整数（bool 不接受）。
    """
    if not isinstance(payload, Mapping):
        raise ConsoleValidationError("请求体必须是 JSON 对象")
    allowed = set(FEATURE_KEYS) | set(ADVANCED_FEATURE_KEYS) | {"xiaoweiEnabled", "debounceMs"}
    unknown = set(payload) - allowed
    if unknown:
        raise ConsoleValidationError(f"不认识的能力字段：{', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "debounceMs":
            if isinstance(value, bool):
                raise ConsoleValidationError("debounceMs 必须是整数（毫秒）")
            number = _as_int(value, -1)
            if number < 0 or number > MAX_DEBOUNCE_MS:
                raise ConsoleValidationError(
                    f"debounceMs 必须在 0–{MAX_DEBOUNCE_MS} 之间")
            out[key] = number
            continue
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


def advanced_view(raw: Mapping[str, Any]) -> dict[str, bool]:
    """高级顶层开关的**生效值**（缺省 false，与运行时 ``core/accounts.py`` 同口径）。"""
    return {key: _as_bool(raw.get(key), False) for key in ADVANCED_FEATURE_KEYS}


# ------------------------------------------------------------------ 心流配置

def heartflow_config_view(raw: Mapping[str, Any]) -> dict[str, Any]:
    """心流可编辑配置 + 只读状态（**不返回模型名**）。"""
    hf = _as_dict(raw.get("heartflow"))
    profile = _as_dict(hf.get("profile"))
    threshold = _as_float(hf.get("replyThreshold"))
    explicit = threshold is not None
    if threshold is None:
        threshold = HF_THRESHOLD_FALLBACK
    threshold = max(0.0, min(1.0, threshold))
    groups = hf.get("whitelistGroups")
    wl: list[str] = []
    if isinstance(groups, (list, tuple)):
        for item in groups:
            text = str(item or "").strip()
            if text and text not in wl:
                wl.append(text)
    return {
        "enabled": _as_bool(hf.get("enabled"), False),
        "modelConfigured": bool(str(hf.get("model") or "").strip()),
        "replyThreshold": threshold,
        "thresholdExplicit": explicit,
        "whitelistGroups": wl,
        "profileEnabled": _as_bool(profile.get("enabled"), True),
    }


def validate_heartflow(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验 ``PUT /accounts/{id}/heartflow``（只开放三个可编辑键）。"""
    if not isinstance(payload, Mapping):
        raise ConsoleValidationError("请求体必须是 JSON 对象")
    allowed = {"replyThreshold", "whitelistGroups", "profileEnabled"}
    unknown = set(payload) - allowed
    if unknown:
        raise ConsoleValidationError(f"不认识的心流字段：{', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    if "replyThreshold" in payload:
        threshold = _as_float(payload["replyThreshold"])
        if threshold is None or not 0.0 <= threshold <= 1.0:
            raise ConsoleValidationError("replyThreshold 必须是 0–1 之间的数字")
        out["replyThreshold"] = threshold
    if "whitelistGroups" in payload:
        out["whitelistGroups"] = parse_id_list(payload["whitelistGroups"], kind="group")
    if "profileEnabled" in payload:
        if not isinstance(payload["profileEnabled"], bool):
            raise ConsoleValidationError("profileEnabled 必须是 true/false")
        out["profileEnabled"] = payload["profileEnabled"]
    if not out:
        raise ConsoleValidationError("没有可写的心流字段")
    return out


def merge_heartflow(existing: Any, validated: Mapping[str, Any]) -> dict[str, Any]:
    """心流段增量合并：保留 ``model`` / ``weights`` / ``profile.caption`` 等同级键。"""
    if existing is not None and not isinstance(existing, Mapping):
        raise ConsoleValidationError("账号里的 heartflow 不是对象，拒绝覆盖")
    out = dict(_as_dict(existing))
    if "replyThreshold" in validated:
        out["replyThreshold"] = validated["replyThreshold"]
    if "whitelistGroups" in validated:
        out["whitelistGroups"] = list(validated["whitelistGroups"])
    if "profileEnabled" in validated:
        profile = out.get("profile")
        if profile is not None and not isinstance(profile, Mapping):
            raise ConsoleValidationError("账号里的 heartflow.profile 不是对象，拒绝覆盖")
        out["profile"] = {**_as_dict(profile), "enabled": bool(validated["profileEnabled"])}
    return out


# ------------------------------------------------------------------ 群策略动作

GROUP_ACTIONS = ("allow", "deny", "clear")


def validate_group_action(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in GROUP_ACTIONS:
        raise ConsoleValidationError(
            f"action 不认识：{text!r}（可选 {', '.join(GROUP_ACTIONS)}）")
    return text


def _clean_group_ids(value: Any) -> list[str]:
    """宽松清洗**盘上已有**名单：坏项直接丢掉，不因为历史脏数据拒绝本次操作。"""
    out: list[str] = []
    if not isinstance(value, (list, tuple)):
        return out
    for item in value:
        text = str(item or "").strip()
        if not text or len(text) > MAX_ITEM_LEN:
            continue
        if not text.endswith("@chatroom"):
            continue
        if text not in out:
            out.append(text)
    return out


def merge_group_policy(
    existing_allow: Any, existing_black: Any, group_id: str, action: Any,
) -> tuple[list[str], list[str]]:
    """返回 ``(groupAllowFrom, blacklistGroups)`` 新值（去重、保序）。"""
    gid = str(group_id or "").strip()
    if not gid or len(gid) > MAX_ITEM_LEN or not gid.endswith("@chatroom"):
        raise ConsoleValidationError(f"群 id 必须以 @chatroom 结尾：{gid!r}")
    act = validate_group_action(action)
    allow = _clean_group_ids(existing_allow)
    black = _clean_group_ids(existing_black)
    if act == "allow":
        if gid not in allow:
            allow.append(gid)
        black = [g for g in black if g != gid]
    elif act == "deny":
        if gid not in black:
            black.append(gid)
        allow = [g for g in allow if g != gid]
    else:
        allow = [g for g in allow if g != gid]
        black = [g for g in black if g != gid]
    return allow, black


# ------------------------------------------------------------------ 查询参数

def validate_page(limit: Any, offset: Any, *, max_limit: int = 200,
                  max_offset: int = 5000) -> tuple[int, int]:
    n = max(1, min(_as_int(limit, 50), int(max_limit)))
    off = max(0, min(_as_int(offset, 0), int(max_offset)))
    return n, off


def validate_hours(hours: Any, *, max_hours: int = 720) -> int:
    return max(1, min(_as_int(hours, 24), int(max_hours)))


def validate_direction(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in ("", "inbound", "outbound"):
        raise ConsoleValidationError("direction 只能是 inbound / outbound / 空")
    return text


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
        "advanced": advanced_view(raw),
        "heartflow": heartflow_config_view(raw),
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


def account_status_view(
    snapshot: Mapping[str, Any] | None, account_id: str, now: float | None = None,
) -> dict[str, Any]:
    """全账号表用：快照新鲜度 + 该账号的 WS/热加载字段（不含凭据/策略值）。"""
    if not isinstance(snapshot, Mapping):
        return {
            "state": "unknown", "age_sec": None, "written_at": None,
            "ws_connected": False, "ws_connected_once": False, "client_ready": False,
            "config_mtime_ns": 0, "last_reload_at": 0, "last_reload_ok": None,
            "last_reload_error": None,
        }
    classified = status_classify(snapshot.get("written_at"), now)
    accounts = snapshot.get("accounts")
    acct = (accounts or {}).get(account_id) if isinstance(accounts, Mapping) else None
    acct = _as_dict(acct)
    return {
        **classified,
        "written_at": snapshot.get("written_at"),
        "ws_connected": bool(acct.get("ws_connected")),
        "ws_connected_once": bool(acct.get("ws_connected_once")),
        "client_ready": bool(acct.get("client_ready")),
        "config_mtime_ns": _as_int(acct.get("config_mtime_ns"), 0),
        "last_reload_at": _as_int(acct.get("last_reload_at"), 0),
        "last_reload_ok": acct.get("last_reload_ok"),
        "last_reload_error": (str(acct.get("last_reload_error") or "")[:300] or None),
    }


def diagnostics_checks(
    *,
    db_ok: bool,
    db_error: str = "",
    snapshot_state: str = "unknown",
    ws_connected: bool = False,
    authcode_present: bool = True,
    token_key_present: bool = True,
    allow_all_users: bool = False,
    dm_policy: str = "",
    heartflow_enabled: bool = False,
    heartflow_model_configured: bool = True,
) -> list[dict[str, Any]]:
    """生成体检项（纯函数；IO 结果由调用方传入）。``level`` = ok/warn/error。"""
    out: list[dict[str, Any]] = []

    def add(check_id: str, ok: bool, message: str, *, warn: bool = False) -> None:
        out.append({
            "id": check_id,
            "ok": bool(ok),
            # ``level`` 是**展示严重度**：warn=True 的项即使不算致命也标黄。
            "level": "warn" if warn else ("ok" if ok else "error"),
            "message": message,
        })

    if db_ok:
        add("db", True, "数据库连接正常")
    else:
        add("db", False, f"数据库不可用{('：' + db_error) if db_error else ''}")

    if snapshot_state in ("live", "delayed"):
        add("snapshot", True, f"状态快照新鲜（{snapshot_state}）")
    elif snapshot_state == "stale":
        add("snapshot", True, "状态快照已过期，网关可能卡住", warn=True)
    else:
        add("snapshot", True, "读不到网关状态快照（网关未运行或 HERMES_HOME 不一致）", warn=True)

    if ws_connected:
        add("ws", True, "账号 WS 已连接")
    else:
        add("ws", True, "账号当前未连接 WS（配置仍可保存，由网关热加载）", warn=True)

    add("authcode", authcode_present, "缺少 authcode（账号文件/.env）—— 无法收信")
    add("tokenKey", token_key_present, "缺少 tokenKey（账号文件/.env）—— 无法收信")
    if allow_all_users:
        add("allowAllUsers", True, "allowAllUsers=true 会绕过 dmPolicy（私聊全放行）", warn=True)
    elif dm_policy in ("open", "blacklist", "pairing"):
        add("allowAllUsers", True,
            f"dmPolicy={dm_policy} 仍需网关 WECHATPRO_ALLOW_ALL_USERS=1，否则消息到不了插件",
            warn=True)
    if heartflow_enabled and not heartflow_model_configured:
        add("heartflowModel", False, "心流已启用但没有配置 heartflow.model，判定会直接失败")
    return out


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
