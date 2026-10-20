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
HOME_CHANNEL_KEY = "WECHATPRO_HOME_CHANNEL"
AGENT_KEY = "WECHATPRO_AGENT"
DB_HOST_KEY = "WECHATPRO_DB_HOST"
DB_PORT_KEY = "WECHATPRO_DB_PORT"
DB_USER_KEY = "WECHATPRO_DB_USER"
DB_NAME_KEY = "WECHATPRO_DB_NAME"
DB_PASSWORD_KEY = "WECHATPRO_DB_PASSWORD"

_REQUIRED_ENV = (API_BASE_KEY, AUTHCODE_KEY, TOKEN_KEY_KEY)
_OPTIONAL_ENV = (WS_URL_KEY, WEBHOOK_PUBLIC_URL_KEY, ALLOWED_USERS_KEY, HOME_CHANNEL_KEY)

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
        DB_HOST_KEY,
        DB_PORT_KEY,
        DB_USER_KEY,
        DB_NAME_KEY,
        DB_PASSWORD_KEY,
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
        "3. 向导会写入 .env 与 accounts/default.json，并尝试启用插件。",
        f"4. 验证：hermes plugins doctor {status['plugin_dir']}",
        "5. 重启 gateway 后，用允许的微信私聊发一条消息。",
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
    allowed = _prompt("允许私聊的 wxid（逗号分隔，可留空）", env.get(ALLOWED_USERS_KEY, ""))
    home_channel = _prompt("Home channel（格式 accountId:peerId，可留空）",
                           env.get(HOME_CHANNEL_KEY, ""))
    agent = _prompt("绑定 Hermes profile（agent 名，禁止 main）", _agent_default(home, account))
    if agent == "main":
        raise SystemExit("agent 不能是 main（多账号会串号）")

    env_updates = {
        API_BASE_KEY: api_base,
        AUTHCODE_KEY: authcode,
        TOKEN_KEY_KEY: token_key,
    }
    if ws_url:
        env_updates[WS_URL_KEY] = ws_url
    if public_url:
        env_updates[WEBHOOK_PUBLIC_URL_KEY] = public_url
    if allowed:
        env_updates[ALLOWED_USERS_KEY] = allowed
    if home_channel:
        env_updates[HOME_CHANNEL_KEY] = home_channel
    env_updates[AGENT_KEY] = agent

    account_updates = {
        "enabled": True,
        "agent": agent,
        "authcodeEnv": AUTHCODE_KEY,
        "tokenKeyEnv": TOKEN_KEY_KEY,
        "apiBaseUrl": api_base,
    }
    if ws_url:
        account_updates["wsUrl"] = ws_url
    if public_url:
        account_updates["webhookPublicUrl"] = public_url
    if allowed:
        account_updates["allowFrom"] = [x.strip() for x in allowed.split(",") if x.strip()]

    if _ask_yes("配置 MariaDB（可选，用于历史/心流/审计）", False):
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

    print("\n即将写入：")
    print(f"  {home / '.env'}（0600）")
    print(f"  {_account_path(home=home)}（0600）")
    for key in (API_BASE_KEY, WS_URL_KEY, AUTHCODE_KEY, TOKEN_KEY_KEY):
        if key in env_updates:
            shown = "***" if key in (AUTHCODE_KEY, TOKEN_KEY_KEY) else env_updates[key]
            print(f"  {key}={shown}")
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

    env_updates = {
        API_BASE_KEY: api_base,
        AUTHCODE_KEY: authcode,
        TOKEN_KEY_KEY: token_key,
        WS_URL_KEY: ws_url,
        AGENT_KEY: agent,
    }
    for key in (WEBHOOK_PUBLIC_URL_KEY, ALLOWED_USERS_KEY, HOME_CHANNEL_KEY):
        if env.get(key):
            env_updates[key] = env[key]
    for key in (DB_HOST_KEY, DB_PORT_KEY, DB_USER_KEY, DB_NAME_KEY, DB_PASSWORD_KEY):
        if env.get(key):
            env_updates[key] = env[key]

    account_updates = {
        "enabled": True,
        "agent": agent,
        "authcodeEnv": AUTHCODE_KEY,
        "tokenKeyEnv": TOKEN_KEY_KEY,
        "apiBaseUrl": api_base,
        "wsUrl": ws_url,
    }
    if env.get(WEBHOOK_PUBLIC_URL_KEY):
        account_updates["webhookPublicUrl"] = env[WEBHOOK_PUBLIC_URL_KEY]
    if env.get(ALLOWED_USERS_KEY):
        account_updates["allowFrom"] = [
            x.strip() for x in env[ALLOWED_USERS_KEY].split(",") if x.strip()
        ]
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
