#!/usr/bin/env python3
"""V5 验证台：平台是否**真的被启用**（F1 的验收项）。

## 它验什么

调用 Hermes 真实的启用链路 —— ``gateway.config_env._enable_plugin_platforms_from_env``
—— 并断言 ``config.platforms[Platform("wechatpadpro")].enabled``。

这条链路是 2026-10-01 那场故障的正中心：插件能加载、日志有
``[WPP] 平台已注册``，但平台**永不启用**（``gateway_state.platforms = {}``、
``No messaging platforms available``）。

## 为什么不碰线上

全程在 ``tempfile`` 草稿 ``HERMES_HOME`` 里跑（与 ``hermes plugins doctor``
同款手法）：拷一份插件进去、写一个**假凭据**的测试账号，**绝不触碰**
``~/.hermes/plugins/wechatpadpro/``。假 authcode 只用于让凭证门通过，
既不联网也不需要真实密钥。

⚠️ 绝不用 ``accounts/default.json`` 验 —— 它引用**生产 bot**
``wxid_eezdbu1ytws422``。

## 用法

    tools/hermes_run.sh tools/probe_v5_enablement.py <plugin_dir> <case>

``case``：

- ``configured``    账号文件带假 authcode       → 期望 **启用**
- ``unconfigured``  只有 .example.json / 空     → 期望 **不启用**
- ``env_only``      无账号文件，只有 env 凭据   → 期望 **启用**

每个 case 必须**单独起进程**（``discover_plugins()`` 进程内缓存）。
退出码 0 = 符合预期。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

#: 假凭据 —— 只为了让凭证门放行。不是任何真实密钥。
_FAKE_AUTHCODE = "V5-PROBE-FAKE-NOT-A-REAL-KEY"

#: 测试账号 id（不用 default，避免与真实账号混淆）
_TEST_ACCOUNT_ID = "v5probe"

#: 平台名（与 plugin.yaml / register_platform 同字面量）
PLATFORM_NAME = "wechatpadpro"

#: 插件的 manifest ``name:``。
#: ⚠️ 启用白名单记的是**这个**，不是目录名 —— ``hermes plugins enable`` 写的
#: 就是它（.183 线上一行实测为 ``enabled: [wechatpadpro-platform]``）。
_PLUGIN_MANIFEST_NAME = "wechatpadpro-platform"


def _write_config(home: Path) -> None:
    """写草稿 config.yaml，把插件放进 ``plugins.enabled`` 白名单。

    ⚠️ 这一步是**整个启用链的最外层前置门**：插件是 opt-in，没有这一行
    ``discover_plugins()`` 就不会调 ``register()``，于是
    ``platform_registry`` 里没有这个平台 ⇒ ``Platform("wechatpadpro")``
    直接 ValueError ⇒ ``_enable_plugin_platform`` 在 try 里静默 return。
    """
    (home / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        f"    - {_PLUGIN_MANIFEST_NAME}\n"
        "  disabled: []\n",
        encoding="utf-8",
    )


def _build_home(plugin_src: Path, case: str) -> tuple[str, Path]:
    """建草稿 HERMES_HOME，把插件拷成 ``plugins/wechatpadpro``。"""
    home = tempfile.mkdtemp(prefix="wpp-v5-")
    _write_config(Path(home))
    plugins_root = Path(home) / "plugins"
    plugins_root.mkdir(parents=True)
    # 目录名必须是平台名：manifest key 由目录名派生
    dst = plugins_root / "wechatpadpro"
    shutil.copytree(
        plugin_src, dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".pytest_cache"),
    )
    accounts = dst / "accounts"
    accounts.mkdir(exist_ok=True)

    if case == "configured":
        (accounts / f"{_TEST_ACCOUNT_ID}.json").write_text(
            json.dumps({"id": _TEST_ACCOUNT_ID, "authcode": _FAKE_AUTHCODE}),
            encoding="utf-8",
        )
    elif case == "unconfigured":
        # 只放一个 .example.json —— 约定永不参与加载
        (accounts / "sample.example.json").write_text(
            json.dumps({"id": "sample", "authcode": _FAKE_AUTHCODE}), encoding="utf-8"
        )
    elif case == "env_only":
        os.environ["WECHATPRO_AUTHCODE"] = _FAKE_AUTHCODE
    else:
        raise SystemExit(f"未知 case: {case!r}")

    return home, dst


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.split("## 用法")[1], file=sys.stderr)
        return 64
    plugin_src = Path(argv[1]).resolve()
    case = argv[2]

    # 别让外部 env 干扰判定
    os.environ.pop("WECHATPRO_ACCOUNTS_JSON", None)
    if case != "env_only":
        os.environ.pop("WECHATPRO_AUTHCODE", None)

    home, dst = _build_home(plugin_src, case)

    # HERMES_HOME 必须在 discover_plugins() 之前设定
    os.environ["HERMES_HOME"] = home
    os.environ.setdefault("HERMES_BUNDLED_PLUGINS", str(Path(home) / "bundled-plugins"))
    Path(os.environ["HERMES_BUNDLED_PLUGINS"]).mkdir(parents=True, exist_ok=True)

    from hermes_cli.plugins import discover_plugins
    discover_plugins()

    from gateway.config import GatewayConfig, Platform
    from gateway.config_env import _enable_plugin_platforms_from_env

    config = GatewayConfig()
    _enable_plugin_platforms_from_env(config)

    # 注册表是 Platform(name) 能成立的前提（见 Platform._missing_）
    from gateway.platform_registry import platform_registry
    in_registry = bool(platform_registry.is_registered(PLATFORM_NAME))

    try:
        platform = Platform(PLATFORM_NAME)
    except ValueError:
        platform = None

    cfg = config.platforms.get(platform) if platform is not None else None
    enabled = bool(cfg is not None and cfg.enabled)

    expect = case != "unconfigured"
    result = {
        "case": case,
        "plugin_dir": str(dst),
        "hermes_home": home,
        "registered_in_registry": in_registry,
        "platform_resolvable": platform is not None,
        "platform_present": cfg is not None,
        "enabled": enabled,
        "expect_enabled": expect,
        "pass": enabled == expect,
    }
    print(json.dumps(result, ensure_ascii=False))

    if result["pass"]:
        # 通过就清理；失败则**保留**草稿 home 供排查
        shutil.rmtree(home, ignore_errors=True)
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
