#!/usr/bin/env python3
"""WeChatPadPro Hermes 插件配置向导（仅标准库）。

两种用法：

* 交互式：``python3 setup_wizard.py`` —— 逐项询问，写入当前 Hermes home 的
  ``.env`` 与插件目录下的 ``accounts/default.json``；
* 只读检查：``python3 setup_wizard.py --check --json`` —— 给 Hermes agent /
  安装脚本读取，不写任何文件，不输出密钥。

设计约束：

* 只写 ``<HERMES_HOME>/.env`` 与插件目录内的 ``accounts/*.json``；
* 密钥用 ``getpass`` 读取，不回显、不落日志、不出现在 argv 示例里；
* 账号文件只写 ``authcodeEnv`` / ``tokenKeyEnv``，不写明文凭据；
* 所有写入先备份，权限 0600。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

PLUGIN_NAME = "wechatpadpro-platform"
DEFAULT_ACCOUNT = "default"

API_BASE_KEY = "WECHATPRO_API_BASE"
WS_URL_KEY = "WECHATPRO_WS_URL"
AUTHCODE_KEY = "WECHATPRO_AUTHCODE"
TOKEN_KEY_KEY = "WECHATPRO_TOKEN_KEY"
WEBHOOK_PUBLIC_URL_KEY = "WECHATPRO_WEBHOOK_PUBLIC_URL"
ALLOWED_USERS_KEY = "WECHATPRO_ALLOWED_USERS"
ALLOW_ALL_USERS_KEY = "WECHATPRO_ALLOW_ALL_USERS"
HOME_CHANNEL_KEY = "WECHATPRO_HOME_CHANNEL"
AGENT_KEY = "WECHATPRO_AGENT"
DB_BACKEND_KEY = "WECHATPRO_DB_BACKEND"
SQLITE_PATH_KEY = "WECHATPRO_SQLITE_PATH"
DB_HOST_KEY = "WECHATPRO_DB_HOST"
DB_PORT_KEY = "WECHATPRO_DB_PORT"
DB_USER_KEY = "WECHATPRO_DB_USER"
DB_NAME_KEY = "WECHATPRO_DB_NAME"
DB_PASSWORD_KEY = "WECHATPRO_DB_PASSWORD"
OSS_ENABLED_KEY = "WECHATPRO_OSS_ENABLED"
OSS_ACCESS_KEY_ID_KEY = "OSS_ACCESS_KEY_ID"
OSS_ACCESS_KEY_SECRET_KEY = "OSS_ACCESS_KEY_SECRET"
OSS_BUCKET_KEY = "OSS_BUCKET"
OSS_ENDPOINT_KEY = "OSS_ENDPOINT"
OSS_CREDENTIALS_PATH_KEY = "OSS_CREDENTIALS_PATH"
JUDGE_API_KEY_KEY = "JUDGE_API_KEY"
JUDGE_BASE_URL_KEY = "JUDGE_BASE_URL"
EMBEDDING_API_KEY_ENV_KEY = "WECHATPRO_EMBEDDING_API_KEY_ENV"

#: 私聊/群准入模式（写进账号 JSON，不是运行时 env；向导从 env 读非交互输入）。
DM_POLICY_KEY = "WECHATPRO_DM_POLICY"
GROUP_POLICY_KEY = "WECHATPRO_GROUP_POLICY"
DM_ALLOW_FROM_KEY = "WECHATPRO_DM_ALLOW_FROM"
DM_BLACKLIST_KEY = "WECHATPRO_DM_BLACKLIST"
GROUP_ALLOW_FROM_KEY = "WECHATPRO_GROUP_ALLOW_FROM"
GROUP_BLACKLIST_KEY = "WECHATPRO_GROUP_BLACKLIST"
HEARTFLOW_ENABLED_KEY = "WECHATPRO_HEARTFLOW_ENABLED"
HEARTFLOW_MODEL_KEY = "WECHATPRO_HEARTFLOW_MODEL"
HEARTFLOW_PROFILE_ENABLED_KEY = "WECHATPRO_HEARTFLOW_PROFILE_ENABLED"
EMBEDDING_MODEL_KEY = "WECHATPRO_EMBEDDING_MODEL"
EMBEDDING_BASE_URL_KEY = "WECHATPRO_EMBEDDING_BASE_URL"
MCP_ENABLED_KEY = "WECHATPRO_MCP_ENABLED"

DM_POLICIES = ("open", "allowlist", "blacklist", "pairing")
GROUP_POLICIES = ("open", "allowlist", "blacklist", "disabled")

_REQUIRED_ENV = (API_BASE_KEY, AUTHCODE_KEY, TOKEN_KEY_KEY)
_OPTIONAL_ENV = (WS_URL_KEY, WEBHOOK_PUBLIC_URL_KEY, ALLOWED_USERS_KEY, HOME_CHANNEL_KEY,
                 ALLOW_ALL_USERS_KEY)

_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def plugin_dir() -> Path:
    """插件目录（本文件所在目录）。"""
    return Path(__file__).resolve().parent


def _serves_home(plugin: Path, home: Path) -> bool:
    """插件目录是否就是 ``home``（或其 profile）运行时会加载的那一份。

    运行期账号目录是 ``PLUGIN_DIR/accounts``（``core/accounts.py``），与 home 无关；
    但只有插件确实属于该 home 时才能写到插件目录，否则会写出目标 home 之外。
    共享布局：插件在 ``<base>/plugins/<dir>``，profile 在 ``<base>/profiles/<name>``。
    """
    if plugin.parent.name != "plugins":
        return False
    base = plugin.parent.parent.resolve()
    if base == home:
        return True
    return home.parent.name == "profiles" and home.parent.parent.resolve() == base


def accounts_dir(home: Path | None = None) -> Path:
    """账号目录；支持 ``WECHATPRO_ACCOUNTS_DIR`` 覆盖（与插件运行期同口径）。

    插件已安装时（本文件在 ``<base>/plugins/<plugin>/`` 下），运行期读取的就是
    ``plugin/accounts`` —— **包括 profile home**（profile 的 ``plugins/`` 通常只是
    指向全局插件目录的符号链接）。只有 ``--home`` 指向与当前插件无关的目标
    （离线准备 / 测试）时，才按目标安装布局写到
    ``<home>/plugins/<PLUGIN_NAME>/accounts``。
    """
    override = os.environ.get("WECHATPRO_ACCOUNTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    here = plugin_dir()
    if home is not None:
        target = Path(home).expanduser().resolve()
        if _serves_home(here, target):
            return here / "accounts"
        return target / "plugins" / PLUGIN_NAME / "accounts"
    return here / "accounts"


def _profile_home(base: Path, profile: str) -> Path | None:
    candidate = (base / "profiles" / profile).resolve()
    return candidate if candidate.is_dir() else None


def _bound_hermes_home() -> Path | None:
    """当前 Hermes 运行上下文绑定的 home；独立运行向导时返回 ``None``。

    multiplex gateway 的 ``os.environ['HERMES_HOME']`` 是**宿主** home（如
    ``~/.hermes``），真实 profile 由 Hermes 的 context override 绑定。
    直接读环境变量会把 profile 配置误判成 default 未配置。
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore
    except Exception:  # noqa: BLE001 —— 独立运行向导时没有 Hermes 模块
        return None
    try:
        return Path(get_hermes_home()).expanduser().resolve()
    except Exception:  # noqa: BLE001 —— 上下文异常时退回环境变量
        return None


def detect_home(explicit: str | None = None) -> Path:
    """定位 Hermes home。

    优先级：``--home`` → Hermes 运行上下文 → ``HERMES_HOME``（再叠加
    ``HERMES_PROFILE``）→ 插件所在 home → ``~/.hermes``。
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_profile = os.environ.get("HERMES_PROFILE", "").strip()
    bound = _bound_hermes_home()
    if bound is not None:
        if env_profile:
            # 上下文 home 可能仍是宿主 home；此时配置在 profiles/<name> 下。
            return _profile_home(bound, env_profile) or bound
        return bound
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        home = Path(env_home).expanduser().resolve()
        if env_profile:
            return _profile_home(home, env_profile) or home
        return home
    p = plugin_dir()
    if p.parent.name == "plugins":
        base = p.parent.parent.resolve()
        if env_profile:
            return _profile_home(base, env_profile) or base
        return base
    return (Path.home() / ".hermes").resolve()


def detect_profile(home: Path) -> str:
    """当前 profile 名：``HERMES_PROFILE`` → ``profiles/<name>`` → ``default``。"""
    env_profile = os.environ.get("HERMES_PROFILE", "").strip()
    if env_profile:
        return env_profile
    if home.parent.name == "profiles":
        return home.name
    return "default"


def resolve_home(home_arg: str | None, profile: str | None) -> tuple[Path, str]:
    """把 ``--home`` / ``--profile`` 归一成 ``(home, profile_name)``。

    ``--home`` 显式给出时**优先**：``--profile`` 只用来标注/推导 agent 名，
    不再把 home 重新解析回 ``profiles/<name>``（否则离线准备/测试会写错目标，
    实测踩过）。
    """
    if home_arg:
        home = detect_home(home_arg)
        name = profile or detect_profile(home)
        if "/" in name or name.startswith("~"):
            name = Path(name).expanduser().resolve().name
        return home, name
    if profile:
        base = detect_home(None)
        if base.parent.name == "profiles":
            base = base.parent.parent
        # 允许传路径；纯名字则落到 <base>/profiles/<name>
        if "/" in profile or profile.startswith("~"):
            home = Path(profile).expanduser().resolve()
        else:
            home = (base / "profiles" / profile).resolve()
        return home, (profile if "/" not in profile else home.name)
    home = detect_home(None)
    return home, detect_profile(home)


def _read_env_file(path: Path) -> dict[str, str]:
    """解析 ``.env``（忽略注释/空行，支持 ``export`` 与单双引号）。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def _effective_env(home: Path) -> dict[str, str]:
    """文件值 + 进程值；进程值优先（Hermes 启动时也是这个口径）。"""
    values = _read_env_file(home / ".env")
    known = {
        *_REQUIRED_ENV,
        *_OPTIONAL_ENV,
        AGENT_KEY,
        DB_BACKEND_KEY,
        SQLITE_PATH_KEY,
        DB_HOST_KEY,
        DB_PORT_KEY,
        DB_USER_KEY,
        DB_NAME_KEY,
        DB_PASSWORD_KEY,
        OSS_ENABLED_KEY,
        OSS_ACCESS_KEY_ID_KEY,
        OSS_ACCESS_KEY_SECRET_KEY,
        OSS_BUCKET_KEY,
        OSS_ENDPOINT_KEY,
        OSS_CREDENTIALS_PATH_KEY,
        JUDGE_API_KEY_KEY,
        JUDGE_BASE_URL_KEY,
        EMBEDDING_API_KEY_ENV_KEY,
        DM_POLICY_KEY,
        GROUP_POLICY_KEY,
        DM_ALLOW_FROM_KEY,
        DM_BLACKLIST_KEY,
        GROUP_ALLOW_FROM_KEY,
        GROUP_BLACKLIST_KEY,
        HEARTFLOW_ENABLED_KEY,
        HEARTFLOW_MODEL_KEY,
        HEARTFLOW_PROFILE_ENABLED_KEY,
        EMBEDDING_MODEL_KEY,
        EMBEDDING_BASE_URL_KEY,
        MCP_ENABLED_KEY,
        "WECHATPRO_WEBHOOK_HOST",
        "WECHATPRO_DB_ALLOW_REMOTE",
    }
    for key in known:
        if key in os.environ and os.environ[key].strip():
            values[key] = os.environ[key].strip()
    return values


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    try:
        os.chmod(backup, 0o600)
    except OSError:
        pass
    return backup


def write_env(home: Path, updates: dict[str, str]) -> Path:
    """幂等更新 ``<home>/.env``：保留注释/未知键，替换已知键，追加新键。"""
    path = home / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup(path)

    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()

    done: set[str] = set()
    for i, raw in enumerate(lines):
        m = _ENV_LINE_RE.match(raw.strip())
        if not m:
            continue
        key = m.group(1)
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            done.add(key)

    for key, val in updates.items():
        if key not in done:
            lines.append(f"{key}={val}")

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def _load_account(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_account(account_path: Path, updates: dict, force: bool = False) -> Path:
    """更新账号 JSON（合并未知键），权限 0600，先备份。

    ``force`` 只跳过**交互确认**（那一步在调用方），不跳过备份 ——
    覆盖账号文件永远先留一份 ``.bak-<时间戳>``。
    """
    existing = _load_account(account_path)
    if account_path.is_file():
        _backup(account_path)
    merged = {**existing, **updates}
    account_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = account_path.with_name(account_path.name + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, account_path)
    return account_path


def derive_ws_url(api_base: str) -> str:
    """由 API base 推导默认 WS 地址：``https://host`` → ``wss://host/ws/sync``。"""
    parsed = urlparse(api_base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    host = parsed.netloc
    return f"{scheme}://{host}/ws/sync" if host else ""


def _normalize_api_base(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("API base 必须是 http(s)://host[:port] 形式")
    return value


def _agent_default(home: Path, account: dict) -> str:
    agent = str(account.get("agent") or "").strip()
    if agent:
        return agent
    return detect_profile(home)


def _account_path(account_id: str = DEFAULT_ACCOUNT, home: Path | None = None) -> Path:
    return accounts_dir(home) / f"{account_id}.json"


def _enabled_plugins(config_text: str) -> set[str]:
    """解析 ``config.yaml`` 里 ``plugins.enabled`` 的条目（不引入 YAML 依赖）。

    只认 ``plugins:`` 段下的 ``enabled:`` 列表；``platform_toolsets`` 等其它段里的
    同名条目不算启用，注释行也不算。
    """
    names: set[str] = set()
    plugins_indent: int | None = None
    enabled_indent: int | None = None
    for raw in config_text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()
        if indent == 0:
            plugins_indent = 0 if text == "plugins:" else None
            enabled_indent = None
            continue
        if plugins_indent is None:
            continue
        if text.startswith("enabled:"):
            value = text[len("enabled:"):].strip()
            if value.startswith("[") and value.endswith("]"):
                names.update(
                    item.strip().strip("'\"")
                    for item in value[1:-1].split(",")
                    if item.strip()
                )
                enabled_indent = None
            else:
                enabled_indent = indent
            continue
        if enabled_indent is not None:
            if text.startswith("- "):
                names.add(text[2:].strip().strip("'\""))
            elif indent <= enabled_indent:
                enabled_indent = None
    return names


def _plugin_enabled(home: Path) -> bool:
    """插件是否在 home 的 ``plugins.enabled`` 里。

    兼容两种写法：清单名 ``wechatpadpro-platform``（``hermes plugins enable``
    写入的）与安装目录名 ``wechatpadpro``（手工配置/旧版）。
    """
    config_path = home / "config.yaml"
    if not config_path.is_file():
        return False
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return False
    wanted = {PLUGIN_NAME, plugin_dir().name}
    return bool(_enabled_plugins(text) & wanted)


def check_status(home_arg: str | None = None, profile: str | None = None,
                 *, _resolved: bool = False) -> dict:
    """只读检查，返回可 JSON 序列化的状态；绝不包含密钥值。

    ``_resolved=True`` 表示调用方已经把 ``(home, profile)`` 归一过（``main`` 的
    路径）—— 这时不能再走 :func:`resolve_home`，否则 ``--home /tmp/x --profile
    default`` 会被重新解析回 ``~/.hermes/profiles/default``（实测踩过）。
    """
    if _resolved:
        home = Path(str(home_arg)).expanduser().resolve()
        profile_name = profile or detect_profile(home)
    else:
        home, profile_name = resolve_home(home_arg, profile)
    env = _effective_env(home)
    account_path = _account_path(home=home)
    account = _load_account(account_path)

    authcode = env.get(AUTHCODE_KEY, "")
    if not authcode:
        indirect = str(account.get("authcodeEnv") or "").strip()
        if indirect:
            authcode = os.environ.get(indirect, "") or _read_env_file(home / ".env").get(indirect, "")
    token_key = env.get(TOKEN_KEY_KEY, "")
    if not token_key:
        indirect = str(account.get("tokenKeyEnv") or "").strip()
        if indirect:
            token_key = os.environ.get(indirect, "") or _read_env_file(home / ".env").get(indirect, "")

    api_base = (
        env.get(API_BASE_KEY)
        or str(account.get("apiBaseUrl") or "").strip()
    )
    ws_url = (
        env.get(WS_URL_KEY)
        or str(account.get("wsUrl") or "").strip()
        or (derive_ws_url(api_base) if api_base else "")
    )
    agent = _agent_default(home, account)

    db_backend = str(env.get(DB_BACKEND_KEY) or "").strip().lower()
    if db_backend not in ("sqlite", "mysql"):
        db_backend = "mysql" if env.get(DB_HOST_KEY) else "sqlite"
    dm_policy = str(account.get("dmPolicy") or "").strip().lower() or (
        "pairing" if account.get("dmPairingEnabled") is True
        else "allowlist" if _account_list(account, "allowFrom") else "open")
    group_policy = str(account.get("groupPolicy") or "").strip().lower() or "open"
    hf = _heartflow(account)
    heartflow_enabled = hf.get("enabled") is True
    oss_enabled = _env_truthy(env.get(OSS_ENABLED_KEY, ""))
    warnings: list[str] = []
    if heartflow_enabled and not str(hf.get("model") or "").strip():
        warnings.append("心流已启用但未配置 heartflow.model")
    if oss_enabled and not (env.get(OSS_ACCESS_KEY_ID_KEY)
                            and env.get(OSS_ACCESS_KEY_SECRET_KEY)
                            and env.get(OSS_BUCKET_KEY)):
        warnings.append("OSS 已启用但凭据不完整（媒体消息将不带公网 URL）")

    missing: list[str] = []
    if not api_base:
        missing.append(API_BASE_KEY)
    if not authcode:
        missing.append(AUTHCODE_KEY)
    if not token_key:
        missing.append(TOKEN_KEY_KEY)

    plugin_enabled = _plugin_enabled(home)

    # 账号文件不是必需的：只配 env 凭据时插件会合成 default 账号
    # （见 platform/enablement.py）。ready 只反映"凭据齐 + 插件已启用"。
    ready = not missing and plugin_enabled
    if missing:
        next_step = "运行 setup_wizard.py 填写缺失项"
    elif not plugin_enabled:
        next_step = f"hermes plugins enable {PLUGIN_NAME}"
    elif not account_path.is_file():
        next_step = "凭据已就绪（env 模式）；如需多账号/白名单，运行 setup_wizard.py 写账号文件"
    else:
        next_step = "重启 Hermes gateway，然后发一条私聊测试消息"

    return {
        "ok": True,
        "plugin_dir": str(plugin_dir()),
        "home": str(home),
        "profile": profile_name,
        "env_file": str(home / ".env"),
        "accounts_dir": str(accounts_dir(home)),
        "account_file": str(account_path),
        "account_file_exists": account_path.is_file(),
        "plugin_enabled": plugin_enabled,
        "agent": agent,
        "api_base": api_base,
        "ws_url": ws_url,
        "db_backend": db_backend,
        "access": {
            "dm_policy": dm_policy,
            "group_policy": group_policy,
            "dm_allowlist_size": len(_account_list(account, "allowFrom")),
            "dm_blacklist_size": len(_account_list(account, "blacklistUsers")),
            "group_allowlist_size": len(_account_list(account, "groupAllowFrom")),
            "group_blacklist_size": len(_account_list(account, "blacklistGroups")),
            # 网关对 open/blacklist/pairing 默认全拒；缺这个 env 时消息到不了插件。
            "gateway_allow_all": _env_truthy(env.get(ALLOW_ALL_USERS_KEY, "")),
        },
        "heartflow": {
            "enabled": heartflow_enabled,
            "model_configured": bool(str(hf.get("model") or "").strip()),
            "profile_enabled": (hf.get("profile") or {}).get("enabled", True)
            if isinstance(hf.get("profile"), dict) else True,
            "embedding_model": str(account.get("embeddingModel") or ""),
        },
        "oss_enabled": oss_enabled,
        "warnings": warnings,
        "configured": {
            API_BASE_KEY: bool(api_base),
            AUTHCODE_KEY: bool(authcode),
            TOKEN_KEY_KEY: bool(token_key),
        },
        "missing": missing,
        "ready": ready,
        "next_step": next_step,
    }


def guide_text(home_arg: str | None = None, profile: str | None = None,
               *, _resolved: bool = False) -> str:
    """给 Hermes agent 的分步引导（不含密钥）。"""
    status = check_status(home_arg, profile, _resolved=_resolved)
    wizard = Path(status["plugin_dir"]) / "setup_wizard.py"
    py = shutil.which("python3") or "python3"
    lines = [
        "WeChatPadPro 配置引导：",
        f"1. 在服务器终端运行：{py} {wizard} --home {status['home']}",
        "2. 按提示填写 API base、authcode、tokenKey（输入不回显）。",
        "3. 准入默认全部放行；可选 allowlist/blacklist/pairing（群与私聊分别设置）。",
        "4. 数据库默认 SQLite；心流、OSS 默认关闭，需要时在向导里开启。",
        "5. 向导会写入 .env 与 accounts/default.json，并尝试启用插件。",
        f"6. 验证：hermes plugins doctor {status['plugin_dir']}",
        "7. 重启 gateway 后，用微信私聊发一条消息。",
    ]
    if status["missing"]:
        lines.insert(0, f"当前缺失：{', '.join(status['missing'])}")
    else:
        lines.insert(0, "配置看起来已齐全。")
    return "\n".join(lines)


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    if secret and default:
        suffix = " [已配置，回车保留]"
    try:
        if secret:
            value = getpass.getpass(f"{label}{suffix}: ")
        else:
            value = input(f"{label}{suffix}: ")
    except EOFError:
        raise SystemExit("输入结束；非交互环境请使用 --non-interactive") from None
    value = value.strip()
    return value or default


def _ask_yes(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{label} [{hint}]: ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes", "1", "true")


def _ask_choice(label: str, choices: tuple[str, ...], default: str) -> str:
    """从固定选项里选一个；非法输入重问（不静默落到默认值）。"""
    shown = "/".join(choices)
    while True:
        value = input(f"{label} ({shown}) [{default}]: ").strip().lower() or default
        if value in choices:
            return value
        print(f"⚠️ 无效选项 {value!r}，请从 {shown} 中选。")


def _split_csv(value: str) -> list[str]:
    return [x.strip() for x in str(value or "").split(",") if x.strip()]


def _ask_list(label: str, current: list[str], *, clear_hint: bool = True) -> list[str]:
    """询问逗号分隔列表。回车保留 ``current``；输入 ``-`` 清空。"""
    suffix = "（输入 - 清空）" if clear_hint else ""
    value = _prompt(f"{label}{suffix}", ",".join(current))
    if value.strip() == "-":
        return []
    return _split_csv(value)


def _account_list(account: dict, key: str) -> list[str]:
    raw = account.get(key)
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _dm_default(account: dict) -> str:
    """向导里私聊模式的默认值：显式 ``dmPolicy`` 优先，其次按旧配置推断。"""
    policy = str(account.get("dmPolicy") or "").strip().lower()
    if policy in DM_POLICIES:
        return policy
    if account.get("dmPairingEnabled") is True:
        return "pairing"
    return "allowlist" if _account_list(account, "allowFrom") else "open"


def _group_default(account: dict) -> str:
    policy = str(account.get("groupPolicy") or "").strip().lower()
    return policy if policy in GROUP_POLICIES else "open"


def _heartflow(account: dict) -> dict:
    value = account.get("heartflow")
    return dict(value) if isinstance(value, dict) else {}


def _merge_section(existing: object, updates: dict) -> dict:
    base = dict(existing) if isinstance(existing, dict) else {}
    base.update(updates)
    return base


def _permissive(access: str) -> bool:
    """该模式是否需要网关 ``WECHATPRO_ALLOW_ALL_USERS=1`` 才放得进来。"""
    return access not in ("allowlist", "disabled", "closed")


def _env_truthy(value: str) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


def _env_value(env: dict[str, str], name: str) -> str:
    """``.env``/已知键之外的动态 env 名（如 embedding key）也允许从进程环境取。"""
    return env.get(name) or os.environ.get(name, "") or ""


def _collect_interactive(home: Path, env: dict[str, str], account: dict, force: bool) -> tuple[dict, dict]:
    print(f"\n配置写入目标：{home}")
    print(f"账号文件：{_account_path(home=home)}\n")
    if not sys.stdin.isatty():
        raise SystemExit("当前不是交互终端；请使用 --non-interactive（值从环境变量读取）")

    api_base = _prompt("WeChatPadPro API base（如 https://your-vendor.example）",
                       env.get(API_BASE_KEY) or str(account.get("apiBaseUrl") or ""))
    api_base = _normalize_api_base(api_base)

    ws_default = env.get(WS_URL_KEY) or str(account.get("wsUrl") or "") or derive_ws_url(api_base)
    ws_url = _prompt("WebSocket 地址（留空使用推导值）", ws_default)

    old_authcode = env.get(AUTHCODE_KEY, "")
    authcode = _prompt("authcode（输入不回显）", old_authcode, secret=True)
    if not authcode:
        raise SystemExit("authcode 不能为空")

    old_token = env.get(TOKEN_KEY_KEY, "")
    token_key = _prompt("tokenKey（输入不回显）", old_token, secret=True)
    if not token_key:
        raise SystemExit("tokenKey 不能为空")

    public_url = _prompt("Webhook 公网地址（可留空，vendor 回调用）",
                         env.get(WEBHOOK_PUBLIC_URL_KEY, ""))
    home_channel = _prompt("Home channel（格式 accountId:peerId，可留空）",
                           env.get(HOME_CHANNEL_KEY, ""))
    agent = _prompt("绑定 Hermes profile（agent 名，禁止 main）", _agent_default(home, account))
    if agent == "main":
        raise SystemExit("agent 不能是 main（多账号会串号）")

    # ------------------------------------------------------------ 准入策略
    print("\n准入策略（默认全部放行；按需收紧）")
    dm_policy = _ask_choice("私聊准入模式", DM_POLICIES, _dm_default(account))
    allow_from = _account_list(account, "allowFrom")
    blacklist_users = _account_list(account, "blacklistUsers")
    if dm_policy == "allowlist":
        allow_from = _ask_list(
            "私聊白名单 wxid（逗号分隔；空列表 = 拒绝所有私聊）",
            allow_from or _split_csv(env.get(ALLOWED_USERS_KEY, "")))
        if not allow_from:
            print("⚠️ 白名单为空 ⇒ 所有私聊都会被拒（fail-closed）。")
    elif dm_policy == "blacklist":
        allow_from = []
        blacklist_users = _ask_list("私聊黑名单 wxid（逗号分隔）", blacklist_users)
    elif dm_policy == "pairing":
        allow_from = []
        blacklist_users = []
        print("ℹ️ 配对模式：管理员私聊发 /genpair 取码，用户发 /pair <码> 自助加入。")
    else:  # open：清掉遗留名单，避免“选全部放行却仍被黑名单拦”
        allow_from = []
        blacklist_users = []

    group_policy = _ask_choice("群聊准入模式", GROUP_POLICIES, _group_default(account))
    group_allow = _account_list(account, "groupAllowFrom")
    blacklist_groups = _account_list(account, "blacklistGroups")
    if group_policy == "allowlist":
        group_allow = _ask_list(
            "群白名单（@chatroom，逗号分隔；空列表 = 拒绝所有群）", group_allow)
        if not group_allow:
            print("⚠️ 群白名单为空 ⇒ 所有群消息都会被拒（fail-closed）。")
    elif group_policy == "blacklist":
        group_allow = []
        blacklist_groups = _ask_list("群黑名单（@chatroom，逗号分隔）", blacklist_groups)
    else:  # open / disabled
        group_allow = []
        blacklist_groups = []

    env_updates = {
        API_BASE_KEY: api_base,
        AUTHCODE_KEY: authcode,
        TOKEN_KEY_KEY: token_key,
    }
    if ws_url:
        env_updates[WS_URL_KEY] = ws_url
    if public_url:
        env_updates[WEBHOOK_PUBLIC_URL_KEY] = public_url
    if home_channel:
        env_updates[HOME_CHANNEL_KEY] = home_channel
    env_updates[AGENT_KEY] = agent
    # 网关（Hermes 中心授权）对 open/blacklist/pairing 一律 default-deny，
    # 必须显式开 ALLOW_ALL 才会把消息交给插件自己的闸门。
    env_updates[ALLOW_ALL_USERS_KEY] = (
        "1" if (_permissive(dm_policy) or _permissive(group_policy)) else "0")
    env_updates[ALLOWED_USERS_KEY] = (
        ",".join(allow_from) if dm_policy == "allowlist" else "")

    account_updates = {
        "enabled": True,
        "agent": agent,
        "authcodeEnv": AUTHCODE_KEY,
        "tokenKeyEnv": TOKEN_KEY_KEY,
        "apiBaseUrl": api_base,
        "dmPolicy": dm_policy,
        "allowFrom": allow_from,
        "blacklistUsers": blacklist_users,
        "groupPolicy": group_policy,
        "groupAllowFrom": group_allow,
        "blacklistGroups": blacklist_groups,
    }
    if ws_url:
        account_updates["wsUrl"] = ws_url
    if public_url:
        account_updates["webhookPublicUrl"] = public_url

    # ------------------------------------------------------------ 数据库
    # 默认 SQLite；已有 MySQL 配置（只有 DB_HOST、没有 backend 键）的旧部署
    # 推断为 mysql，避免向导把生产库悄悄切到一个空 SQLite。
    backend = str(env.get(DB_BACKEND_KEY) or "").strip().lower()
    if backend not in ("sqlite", "mysql"):
        backend = "mysql" if env.get(DB_HOST_KEY) else "sqlite"
    backend = _ask_choice("数据库后端", ("sqlite", "mysql"), backend)
    env_updates[DB_BACKEND_KEY] = backend
    if backend == "sqlite":
        sqlite_path = _prompt("SQLite 文件路径（留空 = 插件目录下 wpp.sqlite3）",
                              env.get(SQLITE_PATH_KEY, ""))
        if sqlite_path:
            env_updates[SQLITE_PATH_KEY] = sqlite_path
    else:
        db_host = _prompt("MariaDB host", env.get(DB_HOST_KEY, "127.0.0.1"))
        if db_host not in ("localhost", "127.0.0.1", "::1"):
            print("⚠️ 非本机 host：请确认已显式允许远程连接（WECHATPRO_DB_ALLOW_REMOTE=1）")
        db_port = _prompt("MariaDB port", env.get(DB_PORT_KEY, "3306"))
        db_user = _prompt("MariaDB user", env.get(DB_USER_KEY, "wechatpro"))
        db_name = _prompt("MariaDB database", env.get(DB_NAME_KEY, "wechatpro"))
        db_password = getpass.getpass("MariaDB password（输入不回显）: ")
        env_updates.update({
            DB_HOST_KEY: db_host,
            DB_PORT_KEY: db_port,
            DB_USER_KEY: db_user,
            DB_NAME_KEY: db_name,
        })
        if db_password:
            env_updates[DB_PASSWORD_KEY] = db_password

    # ------------------------------------------------------------ 心流
    hf = _heartflow(account)
    hf_on = _ask_yes("启用心流模式（可选，默认关闭；需要向量模型 + 判定模型）",
                     hf.get("enabled") is True)
    hf_updates: dict = {"enabled": hf_on}
    if hf_on:
        hf_model = _prompt("心流判定模型（如 deepseek-chat）", str(hf.get("model") or ""))
        if not hf_model:
            raise SystemExit("启用心流必须配置心流判定模型（heartflow.model）")
        hf_updates["model"] = hf_model
        profile_cfg = hf.get("profile")
        profile_on = _ask_yes(
            "启用群画像 / 模型画像能力（默认开启）",
            profile_cfg.get("enabled", True) is not False if isinstance(profile_cfg, dict) else True)
        hf_updates["profile"] = _merge_section(profile_cfg, {"enabled": profile_on})

        embedding_model = _prompt("向量模型（embedding）",
                                  str(account.get("embeddingModel") or "text-embedding-v4"))
        embedding_key_env = _prompt("向量模型 API Key 的环境变量名",
                                    str(account.get("embeddingApiKeyEnv") or "DASHSCOPE_API_KEY"))
        embedding_base = _prompt("向量模型 base URL（留空 = 默认 dashscope 兼容地址）",
                                 str(account.get("embeddingBaseUrl") or ""))
        account_updates["embeddingModel"] = embedding_model
        account_updates["embeddingApiKeyEnv"] = embedding_key_env
        if embedding_base:
            account_updates["embeddingBaseUrl"] = embedding_base
        key_val = _prompt(f"{embedding_key_env}（输入不回显）",
                          env.get(embedding_key_env, ""), secret=True)
        if key_val:
            env_updates[embedding_key_env] = key_val
        judge_key = _prompt(f"{JUDGE_API_KEY_KEY}（心流判定模型密钥，输入不回显）",
                            env.get(JUDGE_API_KEY_KEY, ""), secret=True)
        if judge_key:
            env_updates[JUDGE_API_KEY_KEY] = judge_key
        judge_base = _prompt(f"{JUDGE_BASE_URL_KEY}（留空 = 默认端点）",
                             env.get(JUDGE_BASE_URL_KEY, ""))
        if judge_base:
            env_updates[JUDGE_BASE_URL_KEY] = judge_base
    account_updates["heartflow"] = _merge_section(account.get("heartflow"), hf_updates)
    env_updates[HEARTFLOW_ENABLED_KEY] = "1" if hf_on else "0"

    # ------------------------------------------------------------ OSS
    oss_on = _ask_yes("启用 OSS/S3 媒体转存（可选，默认关闭）", False)
    env_updates[OSS_ENABLED_KEY] = "1" if oss_on else "0"
    if oss_on:
        ak = _prompt(f"{OSS_ACCESS_KEY_ID_KEY}（输入不回显）",
                     env.get(OSS_ACCESS_KEY_ID_KEY, ""), secret=True)
        sk = _prompt(f"{OSS_ACCESS_KEY_SECRET_KEY}（输入不回显）",
                     env.get(OSS_ACCESS_KEY_SECRET_KEY, ""), secret=True)
        bucket = _prompt(f"{OSS_BUCKET_KEY}", env.get(OSS_BUCKET_KEY, ""))
        endpoint = _prompt(f"{OSS_ENDPOINT_KEY}（留空 = 默认 endpoint）",
                           env.get(OSS_ENDPOINT_KEY, ""))
        if ak:
            env_updates[OSS_ACCESS_KEY_ID_KEY] = ak
        if sk:
            env_updates[OSS_ACCESS_KEY_SECRET_KEY] = sk
        if bucket:
            env_updates[OSS_BUCKET_KEY] = bucket
        if endpoint:
            env_updates[OSS_ENDPOINT_KEY] = endpoint
        if not (ak and sk and bucket):
            print("⚠️ OSS 凭据不完整：媒体消息将不带公网 URL（不影响收发）。")

    # ------------------------------------------------------------ 其他能力
    mcp_on = _ask_yes("启用 MCP 只读查询工具（可选，默认关闭）",
                      account.get("mcpEnabled") is True)
    if mcp_on or "mcpEnabled" not in account:
        account_updates["mcpEnabled"] = mcp_on
    print("ℹ️ 其余能力（情感/黑化(黑话)/风格学习/语音/主动关心/合并发送）默认关闭；"
          "需要时在账号 JSON 对应段写 \"enabled\": true。")

    print("\n即将写入：")
    print(f"  {home / '.env'}（0600）")
    print(f"  {_account_path(home=home)}（0600）")
    for key in (API_BASE_KEY, WS_URL_KEY, AUTHCODE_KEY, TOKEN_KEY_KEY, DB_BACKEND_KEY,
                ALLOW_ALL_USERS_KEY, HEARTFLOW_ENABLED_KEY, OSS_ENABLED_KEY):
        if key in env_updates:
            shown = "***" if key in (AUTHCODE_KEY, TOKEN_KEY_KEY) else env_updates[key]
            print(f"  {key}={shown}")
    print(f"  私聊={dm_policy} 群聊={group_policy}")
    if not force and not _ask_yes("确认写入", True):
        raise SystemExit("已取消")
    return env_updates, account_updates


def _collect_non_interactive(env: dict[str, str], account: dict, home: Path) -> tuple[dict, dict]:
    api_base = _normalize_api_base(env.get(API_BASE_KEY, "") or str(account.get("apiBaseUrl") or ""))
    authcode = env.get(AUTHCODE_KEY, "")
    token_key = env.get(TOKEN_KEY_KEY, "")
    missing = [k for k, v in ((API_BASE_KEY, api_base), (AUTHCODE_KEY, authcode),
                              (TOKEN_KEY_KEY, token_key)) if not v]
    if missing:
        raise SystemExit(f"非交互模式缺少环境变量：{', '.join(missing)}")

    ws_url = env.get(WS_URL_KEY) or str(account.get("wsUrl") or "") or derive_ws_url(api_base)
    agent = env.get(AGENT_KEY) or _agent_default(home, account)
    if agent == "main":
        raise SystemExit("agent 不能是 main（多账号会串号）")

    dm_policy = str(env.get(DM_POLICY_KEY) or _dm_default(account)).strip().lower()
    if dm_policy not in DM_POLICIES:
        raise SystemExit(f"{DM_POLICY_KEY} 必须是 {'/'.join(DM_POLICIES)} 之一")
    group_policy = str(env.get(GROUP_POLICY_KEY) or _group_default(account)).strip().lower()
    if group_policy not in GROUP_POLICIES:
        raise SystemExit(f"{GROUP_POLICY_KEY} 必须是 {'/'.join(GROUP_POLICIES)} 之一")

    allow_from = (_split_csv(env[DM_ALLOW_FROM_KEY]) if DM_ALLOW_FROM_KEY in env
                  else _account_list(account, "allowFrom"))
    blacklist_users = (_split_csv(env[DM_BLACKLIST_KEY]) if DM_BLACKLIST_KEY in env
                       else _account_list(account, "blacklistUsers"))
    group_allow = (_split_csv(env[GROUP_ALLOW_FROM_KEY]) if GROUP_ALLOW_FROM_KEY in env
                   else _account_list(account, "groupAllowFrom"))
    blacklist_groups = (_split_csv(env[GROUP_BLACKLIST_KEY]) if GROUP_BLACKLIST_KEY in env
                        else _account_list(account, "blacklistGroups"))
    if dm_policy != "allowlist":
        allow_from = []
    if group_policy != "allowlist":
        group_allow = []

    env_updates = {
        API_BASE_KEY: api_base,
        AUTHCODE_KEY: authcode,
        TOKEN_KEY_KEY: token_key,
        WS_URL_KEY: ws_url,
        AGENT_KEY: agent,
        ALLOW_ALL_USERS_KEY: (
            "1" if (_permissive(dm_policy) or _permissive(group_policy)) else "0"),
        ALLOWED_USERS_KEY: ",".join(allow_from) if dm_policy == "allowlist" else "",
    }
    for key in (WEBHOOK_PUBLIC_URL_KEY, HOME_CHANNEL_KEY):
        if env.get(key):
            env_updates[key] = env[key]

    account_updates = {
        "enabled": True,
        "agent": agent,
        "authcodeEnv": AUTHCODE_KEY,
        "tokenKeyEnv": TOKEN_KEY_KEY,
        "apiBaseUrl": api_base,
        "wsUrl": ws_url,
        "dmPolicy": dm_policy,
        "allowFrom": allow_from,
        "blacklistUsers": blacklist_users,
        "groupPolicy": group_policy,
        "groupAllowFrom": group_allow,
        "blacklistGroups": blacklist_groups,
    }
    if env.get(WEBHOOK_PUBLIC_URL_KEY):
        account_updates["webhookPublicUrl"] = env[WEBHOOK_PUBLIC_URL_KEY]

    # ------------------------------------------------------------ 数据库
    backend = str(env.get(DB_BACKEND_KEY) or "").strip().lower()
    if backend not in ("sqlite", "mysql"):
        backend = "mysql" if env.get(DB_HOST_KEY) else "sqlite"
    env_updates[DB_BACKEND_KEY] = backend
    if backend == "sqlite":
        if env.get(SQLITE_PATH_KEY):
            env_updates[SQLITE_PATH_KEY] = env[SQLITE_PATH_KEY]
    else:
        for key in (DB_HOST_KEY, DB_PORT_KEY, DB_USER_KEY, DB_NAME_KEY, DB_PASSWORD_KEY):
            if env.get(key):
                env_updates[key] = env[key]

    # ------------------------------------------------------------ 心流
    hf = _heartflow(account)
    hf_on = (_env_truthy(env[HEARTFLOW_ENABLED_KEY]) if HEARTFLOW_ENABLED_KEY in env
             else hf.get("enabled") is True)
    hf_updates: dict = {"enabled": hf_on}
    if hf_on:
        hf_model = env.get(HEARTFLOW_MODEL_KEY) or str(hf.get("model") or "")
        if not hf_model:
            raise SystemExit(f"心流已启用但缺少模型：设置 {HEARTFLOW_MODEL_KEY}")
        hf_updates["model"] = hf_model
        profile_cfg = hf.get("profile")
        profile_on = (_env_truthy(env[HEARTFLOW_PROFILE_ENABLED_KEY])
                      if HEARTFLOW_PROFILE_ENABLED_KEY in env
                      else (profile_cfg.get("enabled", True) is not False
                            if isinstance(profile_cfg, dict) else True))
        hf_updates["profile"] = _merge_section(profile_cfg, {"enabled": profile_on})

        if env.get(EMBEDDING_MODEL_KEY):
            account_updates["embeddingModel"] = env[EMBEDDING_MODEL_KEY]
        if env.get(EMBEDDING_BASE_URL_KEY):
            account_updates["embeddingBaseUrl"] = env[EMBEDDING_BASE_URL_KEY]
        embedding_key_env = (env.get(EMBEDDING_API_KEY_ENV_KEY)
                             or str(account.get("embeddingApiKeyEnv") or "")).strip()
        if embedding_key_env:
            account_updates["embeddingApiKeyEnv"] = embedding_key_env
            key_val = _env_value(env, embedding_key_env)
            if key_val:
                env_updates[embedding_key_env] = key_val
        for key in (JUDGE_API_KEY_KEY, JUDGE_BASE_URL_KEY):
            if env.get(key):
                env_updates[key] = env[key]
    account_updates["heartflow"] = _merge_section(account.get("heartflow"), hf_updates)
    env_updates[HEARTFLOW_ENABLED_KEY] = "1" if hf_on else "0"

    # ------------------------------------------------------------ OSS
    oss_on = _env_truthy(env.get(OSS_ENABLED_KEY, ""))
    env_updates[OSS_ENABLED_KEY] = "1" if oss_on else "0"
    if oss_on:
        for key in (OSS_ACCESS_KEY_ID_KEY, OSS_ACCESS_KEY_SECRET_KEY, OSS_BUCKET_KEY,
                    OSS_ENDPOINT_KEY, OSS_CREDENTIALS_PATH_KEY):
            if env.get(key):
                env_updates[key] = env[key]

    # ------------------------------------------------------------ 其他能力
    mcp_on = (_env_truthy(env[MCP_ENABLED_KEY]) if MCP_ENABLED_KEY in env
              else account.get("mcpEnabled") is True)
    if mcp_on or "mcpEnabled" not in account:
        account_updates["mcpEnabled"] = mcp_on

    return env_updates, account_updates


def _find_hermes() -> str | None:
    for name in ("hermes",):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (Path.home() / ".local/bin/hermes", Path("/usr/local/bin/hermes")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run_hermes(home: Path, args: list[str]) -> subprocess.CompletedProcess | None:
    if os.environ.get("WPP_INSTALL_SKIP_HERMES", "") == "1":
        return None
    exe = _find_hermes()
    if not exe:
        return None
    env = {**os.environ, "HERMES_HOME": str(home)}
    try:
        return subprocess.run([exe, *args], env=env, capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _post_checks(home: Path, skip: bool) -> None:
    if skip:
        return
    enabled = _run_hermes(home, ["plugins", "enable", PLUGIN_NAME])
    if enabled is None:
        print("ℹ️  未找到 hermes 命令；请手动执行：")
        print(f"   HERMES_HOME={home} hermes plugins enable {PLUGIN_NAME}")
    elif enabled.returncode != 0:
        print("⚠️  自动启用失败（可手动执行）：")
        print(f"   HERMES_HOME={home} hermes plugins enable {PLUGIN_NAME}")
        if enabled.stderr.strip():
            print(f"   {enabled.stderr.strip()[:300]}")

    doctor = _run_hermes(home, ["plugins", "doctor", str(plugin_dir())])
    if doctor is None:
        print("ℹ️  未找到 hermes 命令；跳过 doctor。")
    else:
        output = (doctor.stdout or "") + (doctor.stderr or "")
        print("\n--- hermes plugins doctor ---")
        print(output.strip() or "(无输出)")
        if doctor.returncode != 0:
            print("⚠️  doctor 未通过；按上面的错误逐项修复后重试。")


def _print_next(home: Path) -> None:
    print("\n下一步：")
    print("  1. 重启 Hermes gateway（或 systemd: systemctl --user restart hermes-gateway）")
    print("  2. 用允许的 wxid 私聊机器人发一条消息")
    print("  3. 查看日志：journalctl --user -u hermes-gateway -n 80")
    print("  4. 随时检查：python3 setup_wizard.py --check --json")
    print(f"\n配置文件：{home / '.env'}")
    print(f"账号文件：{_account_path(home=home)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WeChatPadPro Hermes 插件配置向导")
    parser.add_argument("--home", help="Hermes home（默认 HERMES_HOME 或插件所在 home）")
    parser.add_argument("--profile", help="Hermes profile 名（默认自动检测）")
    parser.add_argument("--check", action="store_true", help="只读检查，不写文件")
    parser.add_argument("--json", action="store_true", help="与 --check 一起用，输出 JSON")
    parser.add_argument("--non-interactive", action="store_true", help="从环境变量读值，不询问")
    parser.add_argument("--force", action="store_true", help="跳过覆盖确认（仍然备份）")
    parser.add_argument("--skip-verify", action="store_true", help="跳过 enable/doctor")
    args = parser.parse_args(argv)

    home, profile_name = resolve_home(args.home, args.profile)

    if args.check:
        status = check_status(str(home), profile_name, _resolved=True)
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print(guide_text(str(home), profile_name, _resolved=True))
        return 0 if status["ready"] else 1

    env = _effective_env(home)
    account_path = _account_path(home=home)
    account = _load_account(account_path)
    if args.non_interactive:
        env_updates, account_updates = _collect_non_interactive(env, account, home)
    else:
        env_updates, account_updates = _collect_interactive(home, env, account, args.force)

    write_env(home, env_updates)
    write_account(account_path, account_updates, force=args.force)
    os.chmod(home / ".env", 0o600)
    os.chmod(account_path, 0o600)

    print("\n✅ 配置已写入。")
    _post_checks(home, args.skip_verify)
    _print_next(home)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
