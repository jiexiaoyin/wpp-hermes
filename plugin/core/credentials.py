"""账号存在性判定 —— 单一真源。

## 为什么需要这个模块

历史故障（2026-10-01 在 真机 实测定位）：插件能加载、日志有
``[WPP] 平台已注册``，但 ``gateway_state.platforms = {}``、``No messaging
platforms enabled.`` —— 平台**永远不会被启动**。

根因是**判定口径分裂**：

- ``adapter._build_accounts()``（真正加载账号的函数）**能**读
  ``config.extra["accounts"]`` **和** 插件自管的 ``accounts/<id>.json``；
- 但 ``adapter._is_connected()``（Hermes 的凭证门用的判定）**只认前者和 env
  ``WECHATPRO_AUTHCODE``**，从不读 ``accounts/*.json`` ⇒ 恒返回 False
  ⇒ ``gateway/config_env.py:410`` 的门直接 return。

⇒ **即使把 authcode 填进 ``accounts/default.json``，平台照样起不来。**

本模块把「账号存在性」收敛成单一判定，供两处**共用同一口径**：

1. ``platform.enablement.is_connected``      —— Hermes 的凭证门
2. ``platform.enablement.env_enablement_fn`` —— Hermes 的 env 启用探针

二者口径不一致是最隐蔽的坑：探针 seed 填了 ``accounts``，但 ``is_connected``
若不按同一规则判定，仍然返 False。
"""

from __future__ import annotations

import json
from typing import Any

# ⚠️ 目录与文件筛选**必须复用账号层的实现**，不许在这里再写一遍。
#    2026-10-01 实测发现的分叉（同一类 F1 故障，只是触发条件不同）：
#    这里原本写死 ``PLUGIN_DIR / "accounts"``，而 ``accounts.accounts_dir()``
#    认 ``WECHATPRO_ACCOUNTS_DIR`` 覆盖 ⇒ 运维把账号目录挪走后，
#    **adapter 从新目录读得到账号，本模块的探针却只看旧目录** ⇒ seed/判定为假
#    ⇒ 平台照旧永不启用（``gateway_state.platforms = {}``）。
#    这正是本模块存在的理由（"判定口径分裂"），所以就地收口成单一真源。
from .accounts import accounts_dir, is_account_file


def _has_credential(cfg: dict[str, Any]) -> bool:
    """账号是否真的配了凭据。

    两条路：字面 ``authcode``，或 ``authcodeEnv`` 指向的环境变量有值。
    （对齐 ``accounts/default.json`` 的 schema：``authcode`` 常留空，
    凭据由 ``authcodeEnv`` 从 env 取。）
    """
    if not isinstance(cfg, dict):
        return False
    if str(cfg.get("authcode") or "").strip():
        return True
    env_name = str(cfg.get("authcodeEnv") or "").strip()
    if env_name:
        from .env_compat import secret_env

        if secret_env(env_name).strip():
            return True
    return False


def load_local_accounts() -> dict[str, dict[str, Any]]:
    """读 ``accounts/*.json`` → ``{account_id: cfg}``。

    结构与 ``config.extra["accounts"]`` 同构（``adapter._build_accounts()``
    期望的就是 ``{acct_id: cfg}``），因此 seed 进 extra 后 adapter 可直接用。
    """
    out: dict[str, dict[str, Any]] = {}
    directory = accounts_dir()
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        if not is_account_file(path):
            continue
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 单个坏文件不应拖垮加载
            continue
        if not isinstance(cfg, dict):
            continue
        # 键 = **文件名**（不是 ``cfg["id"]``）：账号的身份在账号层就是文件名
        # （``resolve(id)`` 读 ``accounts/<id>.json``，``list_account_ids()`` 回 stem）。
        # 用 cfg["id"] 会让某个文件以「resolve() 加载不到的名字」算作已配置。
        out[path.stem] = cfg
    return out


def configured_accounts(accounts: Any) -> dict[str, dict[str, Any]]:
    """过滤出**真配了凭据**的账号（空壳 ``default.json`` 不 seed）。"""
    if not isinstance(accounts, dict):
        return {}
    return {str(k): v for k, v in accounts.items() if _has_credential(v)}


def any_configured(accounts: Any) -> bool:
    """给定的账号表里是否**至少有一个**真配了凭据。"""
    return bool(configured_accounts(accounts))


def any_local_account_configured() -> bool:
    """插件自管的 ``accounts/*.json`` 里是否有配好凭据的账号。

    ⚠️ 只看**凭据齐不齐**，不看文件在不在 —— 因为 ``default.json`` 随仓库
    分发（``authcode: ""``），若「文件存在」就算已配置，平台会在无凭据时
    被启用，然后连不上厂商并反复重试。
    """
    return any_configured(load_local_accounts())


def env_accounts() -> dict[str, dict[str, Any]] | None:
    """``WECHATPRO_ACCOUNTS_JSON`` 显式注入的账号表（可选路径）。

    用途：不想把凭据落盘时，用一条 env 直接给账号表。
    """
    from .env_compat import secret_env

    raw = secret_env("WECHATPRO_ACCOUNTS_JSON").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or not data:
        return None
    return data
