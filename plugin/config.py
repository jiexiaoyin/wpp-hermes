"""WPP 插件配置（迁移自 wpp-openclaw src/config.ts）。

配置来源优先级：
1. env 变量（WECHATPRO_AUTHCODE 等）
2. Hermes config.yaml platforms.wechatpadpro.extra.accounts.<id>
3. 插件目录 accounts/<id>.json（迁移自 wpp-openclaw 布局）

默认账号 = 益融小助理（authcode 从 WECHATPRO_AUTHCODE env 读取，不硬编码）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
ACCOUNTS_DIR = PLUGIN_DIR / "accounts"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning("[WPP] 读取配置失败 %s: %s", path, e)
        return {}


def resolve_account_config(account_id: str, extra: dict | None = None) -> dict:
    """解析单个账号配置：extra > accounts/<id>.json > env 默认。

    extra 来自 Hermes config.yaml platforms.wechatpadpro.extra.accounts.<id>。
    """
    cfg: dict = {}

    # 1. 插件目录 accounts/<id>.json（wpp 原布局，如 default.json）
    acct_file = ACCOUNTS_DIR / f"{account_id}.json"
    if acct_file.exists():
        cfg.update(_read_json(acct_file))

    # 2. extra 覆盖（Hermes config.yaml）
    if extra:
        cfg.update(extra)

    # 2.5 runtime overrides（filehelper 命令动态修改，优先于静态配置）
    try:
        from .commands import get_override
        ov = get_override(account_id)
        for k, v in ov.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k] = {**cfg[k], **v}
            else:
                cfg[k] = v
    except Exception:  # noqa: BLE001
        pass

    # 3. env 兜底（凭据）— 注意用直接赋值：default.json 里 authcode 可能是空串占位，
    #    setdefault 不会覆盖已存在的空串，必须强制覆盖。
    authcode = (
        cfg.get("authcode")
        or (cfg.get("authcodeEnv") and _env(cfg["authcodeEnv"]))
        or _env("WECHATPRO_AUTHCODE")
    )
    token_key = (
        cfg.get("tokenKey")
        or (cfg.get("tokenKeyEnv") and _env(cfg["tokenKeyEnv"]))
        or _env("WECHATPRO_TOKEN_KEY")
    )

    cfg["authcode"] = authcode
    cfg["tokenKey"] = token_key
    # webhook 兜底：webhookPublicUrl 从 env 读（默认 hermes.juhe.chat）
    webhook_public = (
        cfg.get("webhookPublicUrl")
        or (cfg.get("webhookPublicUrlEnv") and _env(cfg["webhookPublicUrlEnv"]))
        or _env("WECHATPRO_WEBHOOK_PUBLIC_URL", "https://hermes.juhe.chat")
    )
    cfg["webhookPublicUrl"] = webhook_public
    cfg.setdefault("apiBaseUrl", _env("WECHATPRO_API_BASE", "https://wx.juhe.chat"))
    cfg.setdefault("wsUrl", _env("WECHATPRO_WS_URL", "wss://wx.juhe.chat/ws/sync"))
    cfg.setdefault("selfWxid", "")
    cfg.setdefault("nickname", "微信助手")
    cfg.setdefault("allowFrom", [])
    cfg.setdefault("groupPolicy", "open")
    cfg.setdefault("groupAllowFrom", [])
    cfg.setdefault("requireAtMention", False)
    cfg.setdefault("debounceMs", 1500)

    return cfg


def list_account_ids(extra_accounts: dict | None = None) -> list[str]:
    """账号列表：优先 extra.accounts 键；否则扫描 accounts/ 目录 .json。

    排除 .example / .example.json 后缀 (示例/模板文件, 不应被加载).
    排除下划线开头 (e.g. _notes.md → 不是账号配置).
    """
    if extra_accounts:
        return list(extra_accounts.keys())
    if ACCOUNTS_DIR.exists():
        ids = []
        for p in sorted(ACCOUNTS_DIR.glob("*.json")):
            # 过滤: 文件名包含 .example / _example / 以 _ 开头 / .bak 等非账号文件
            name = p.name
            if name.startswith("_"):
                continue
            if ".example" in name or "_example" in name:
                continue
            if name.endswith(".bak") or name.endswith(".tmp") or name.endswith(".swp"):
                continue
            ids.append(p.stem)
        return ids
    return ["default"]


def default_account_id() -> str:
    ids = list_account_ids()
    return ids[0] if ids else "default"


# ===================== 配置热加载（对齐 wpp fs.watch）=====================
# 改 accounts/<id>.json 运行时字段零重启生效：watchdog → 防抖 → 回调刷新 adapter._accounts
_ACCOUNTS_WATCHER = None


def start_config_watcher(on_change) -> None:
    """启动 accounts/ 目录配置监听（wpp watchAccountConfigs 范式）。

    on_change(account_id) 在配置文件变更后调用（adapter 用于刷新账号缓存）。
    幂等：重复调用只保留 1 个 watcher。
    """
    global _ACCOUNTS_WATCHER
    if _ACCOUNTS_WATCHER is not None:
        return
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        class _Handler(FileSystemEventHandler):
            def on_modified(self, event):
                _debounced_refresh(event.src_path, on_change)

            def on_created(self, event):
                _debounced_refresh(event.src_path, on_change)

        def _debounced_refresh(path, cb):
            # 只关心 accounts/*.json
            if not str(path).endswith(".json"):
                return
            import threading
            timer = threading.Timer(0.3, lambda: cb(_account_id_from_path(path)))
            timer.daemon = True
            timer.start()

        def _account_id_from_path(path: str) -> str:
            name = Path(path).stem
            return name if name != "default" else "default"

        observer = Observer()
        observer.schedule(_Handler(), str(ACCOUNTS_DIR), recursive=False)
        observer.start()
        _ACCOUNTS_WATCHER = observer
        log.info("[WPP] 配置热加载已启动: %s", ACCOUNTS_DIR)
    except ImportError:
        log.warning("[WPP] watchdog 未安装，配置热加载不可用（可用 uv pip install watchdog）")
    except Exception as e:  # noqa: BLE001
        log.warning("[WPP] 配置热加载启动失败: %s", e)


def stop_config_watcher() -> None:
    global _ACCOUNTS_WATCHER
    if _ACCOUNTS_WATCHER is not None:
        try:
            _ACCOUNTS_WATCHER.stop()
            _ACCOUNTS_WATCHER.join(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        _ACCOUNTS_WATCHER = None
