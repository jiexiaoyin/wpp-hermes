"""平台启用契约 —— 让平台**真正被 gateway 启动**的关键。

## Hermes 的启用链路（0.21.5 实读源码）

``gateway/config_env.py``::

    _enable_plugin_platforms_from_env(config)        # :441 每次 load_gateway_config 都跑
      └─ _enable_plugin_platform(config, entry)      # :396
           ├─ seed = _plugin_probe_seed(entry)                       # :366 调 env_enablement_fn()
           ├─ if not already_enabled and is_connected is not None \\
           │     and not _plugin_is_configured(...): return          # :410 ← 凭证门
           │        └─ _plugin_is_configured: seed 里**除 home_channel** 的 key
           │           setdefault 进 extra，再调 is_connected(...)   # :377
           ├─ deps_ok = check_fn(); 不过且无 ensure_deps_fn 就 return # :423
           └─ platform_config.enabled = True
              if seed: platform_config.extra.update(seed)            # :430 seed 到这里才 commit

**要让平台启用，三条必须同时成立**：seed 非空、`is_connected` 为真、`check_fn` 为真。

两个反直觉点（都在源码注释里，踩过才知道）：

1. **`home_channel` 是特殊 key** —— 它不会被 ``setdefault`` 进 extra，而是由核心
   ``lift`` 成 ``HomeChannel`` 对象（``config_env.py:432-437``）。
2. **`env_enablement_fn` 只会被调用一次**（``config_env.py:430`` 注释），
   所以它必须一次给全，不能指望二次补齐。

## 历史故障（本模块修的就是它）

旧 ``adapter._is_connected`` 只认 ``config.extra["accounts"]`` 或 env
``WECHATPRO_AUTHCODE``，**从不读插件自管的 ``accounts/*.json``**
⇒ 恒 False ⇒ 平台注册了但永不启用（``gateway_state.platforms = {}``、
``No messaging platforms enabled.``）。

注意：``check_fn`` 不是阻塞点 —— ``adapter.check_wechatpadpro_requirements()``
是硬编码 ``return True``。
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ..core import credentials

#: 平台名。必须与工具注册用的 ``toolset`` 同字面量 ——
#: Hermes 的 ``_platform_default_toolset`` 会派生 ``hermes-<platform>``，
#: 名字对不上工具就挂不到平台上（``hermes_cli/toolsets.py:355-372``）。
PLATFORM_NAME = "wechatpadpro"

_AUTHCODE_ENV = "WECHATPRO_AUTHCODE"
_HOME_ENV = "WECHATPRO_HOME_CHANNEL"


def is_connected(config: Any) -> bool:
    """账号存在性判定 —— 四条路径任一满足即可。

    与 ``env_enablement_fn`` 的口径**必须严格一致**：探针 seed 填了 ``accounts``，
    这里就得按同一规则认它；否则 seed 通过、判定为假，平台依旧不启用。
    """
    extra = getattr(config, "extra", None)
    accounts = extra.get("accounts") if isinstance(extra, dict) else None
    # ① YAML / env_enablement_fn seed 注入的账号表
    if credentials.any_configured(accounts):
        return True
    # ② WECHATPRO_ACCOUNTS_JSON 显式注入
    if credentials.any_configured(credentials.env_accounts()):
        return True
    # ③ 单条 env 凭据（scoped 口径，见 core.env_compat）
    from ..core.env_compat import secret_env

    if secret_env(_AUTHCODE_ENV).strip():
        return True
    # ④ 插件自管 accounts/*.json —— ★ 旧实现漏掉的就是这一路
    return credentials.any_local_account_configured()


def validate_config(config: Any) -> bool:
    """Hermes 在 ``hermes config`` / setup wizard 里展示时用。与 gate 同口径。"""
    return is_connected(config)


def env_enablement_fn() -> Optional[dict]:
    """env 启用探针：返回非空 dict 才 seed，否则平台不启用。

    ``_plugin_probe_seed`` 要求返回**非空 dict**（``config_env.py:374``），
    返回 ``None``/``{}`` 等同「没配置」。

    ⚠️ 只 seed **真配了凭据**的账号。若把随仓库分发的空壳 ``default.json``
    也 seed 进去，``is_connected`` 会因「accounts 非空」判真，平台被启用后
    又连不上厂商，陷入无凭据的重试。
    """
    from ..core.env_compat import gate_env, secret_env

    accounts = credentials.configured_accounts(
        credentials.env_accounts() or credentials.load_local_accounts())

    # ⑤ 只有一条 ``WECHATPRO_AUTHCODE``（没有账号文件）⇒ **合成 default 账号**。
    #    以前这里直接 return None：``is_connected`` 却因单条 env 判真 ⇒ 探针不 seed、
    #    平台不启用，而配置检查说"已配置" —— 两处口径相反。
    if not accounts:
        authcode = secret_env(_AUTHCODE_ENV).strip()
        if not authcode:
            return None
        accounts = {"default": {
            "agent": gate_env("WECHATPRO_AGENT") or "wpp-wechat",
            "authcodeEnv": _AUTHCODE_ENV,
            "enabled": True,
        }}

    seed: dict[str, Any] = {"accounts": accounts}

    home = gate_env(_HOME_ENV)
    if home:
        # 特殊 key：核心会把它 lift 成 HomeChannel（不做 setdefault）
        seed["home_channel"] = {
            "chat_id": home,
            "name": gate_env(f"{_HOME_ENV}_NAME") or "Home",
        }
    return seed
