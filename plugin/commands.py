"""WPP filehelper 命令处理（迁移自 wpp-openclaw index.ts FILEHELPER_COMMANDS）。

filehelper 消息（发给文件传输助手）触发命令，管理功能开关/白名单。
命令动态生效（内存覆盖，不重启）。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 内存配置覆盖（命令修改后写入，config 读取时优先）
_runtime_overrides: dict[str, dict] = {}


def get_override(account_id: str) -> dict:
    return _runtime_overrides.setdefault(account_id, {})


def set_override(account_id: str, key: str, value) -> None:
    _runtime_overrides.setdefault(account_id, {})[key] = value


def clear_override(account_id: str, key: str) -> None:
    _runtime_overrides.setdefault(account_id, {}).pop(key, None)


HELP_TEXT = """可用命令：
/heartflow on|off|status — 心流主动回复开关
/affection on|off|status — 好感度开关
/jargon on|off|status — 黑话挖掘开关
/user add|del|list [wxid] — 私聊白名单
/group add|del|list [群id] — 群白名单
/genpair — 生成配对码
/pairs — 查看配对码
/help — 显示帮助
"""


def parse_args(args: str) -> list[str]:
    return [a.strip() for a in args.split() if a.strip()]


def handle_command(account_id: str, content: str) -> str:
    """处理 filehelper 命令，返回回复文本（空 = 不回复）。"""
    if not content.startswith("/"):
        return ""
    parts = content.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    arg_list = parse_args(args)

    if cmd == "/help":
        return HELP_TEXT

    # 功能开关: /heartflow /affection /jargon
    feature_map = {"heartflow": "heartflow", "affection": "affection", "jargon": "jargon"}
    if cmd.lstrip("/") in feature_map:
        feature = feature_map[cmd.lstrip("/")]
        return _handle_feature_toggle(account_id, feature, arg_list)

    # 白名单: /user /group
    if cmd in ("/user", "/group"):
        return _handle_allowlist(account_id, "dm" if cmd == "/user" else "group", arg_list)

    # 配对码: /genpair /pairs /pair
    if cmd == "/genpair":
        from .pairing import generate_pairing_code
        entry = generate_pairing_code(account_id)
        from datetime import datetime
        exp = datetime.fromtimestamp(entry["expires_at"] / 1000).strftime("%Y-%m-%d %H:%M")
        return f"✅ 新配对码已生成:\n\n配对码: {entry['code']}\n有效期至: {exp}\n\n用法: 发给白名单外用户，用户私聊机器人发 /pair {entry['code']} 自助加入"
    if cmd == "/pairs":
        from .pairing import read_pairing_code
        entry = read_pairing_code(account_id)
        if entry:
            from datetime import datetime
            exp = datetime.fromtimestamp(entry["expires_at"] / 1000).strftime("%Y-%m-%d %H:%M")
            return f"当前配对码:\n\n配对码: {entry['code']}\n有效期至: {exp}"
        return "当前无配对码（未生成或已过期）。用 /genpair 生成。"
    if cmd == "/pair":
        from .pairing import extract_pair_code, redeem_pairing_code
        code = extract_pair_code(content)
        if not code:
            return "用法: /pair <8位配对码>"
        ok, msg = redeem_pairing_code(account_id, code)
        return msg

    return f"未知命令 {cmd}，/help 查看帮助"


def _handle_feature_toggle(account_id: str, feature: str, args: list[str]) -> str:
    override = get_override(account_id)
    # 读当前状态（从 config）
    from .config import resolve_account_config
    base_cfg = resolve_account_config(account_id, {})
    feat_cfg = base_cfg.get(feature) or {}
    current = override.get(feature, {}).get("enabled", feat_cfg.get("enabled", False))

    if not args:
        return f"/{feature} 当前状态: {'on' if current else 'off'}"
    action = args[0].lower()
    if action in ("on", "1", "true", "yes"):
        set_override(account_id, feature, {**feat_cfg, "enabled": True})
        return f"/{feature} 已开启 ✅"
    if action in ("off", "0", "false", "no"):
        set_override(account_id, feature, {**feat_cfg, "enabled": False})
        return f"/{feature} 已关闭 ✅"
    if action == "status":
        return f"/{feature} 状态: {'on' if current else 'off'}"
    return f"/{feature} 参数: on/off/status"


def _handle_allowlist(account_id: str, kind: str, args: list[str]) -> str:
    if not args:
        return "用法: /user|/group add|del|list [wxid]"
    action = args[0].lower()
    if action == "list":
        from .config import resolve_account_config
        base_cfg = resolve_account_config(account_id, {})
        key = "allowFrom" if kind == "dm" else "groupAllowFrom"
        items = base_cfg.get(key) or []
        return f"{'私聊' if kind == 'dm' else '群'}白名单: {', '.join(items) if items else '(空)'}"
    if action in ("add", "del") and len(args) >= 2:
        target = args[1]
        from .config import resolve_account_config
        base_cfg = resolve_account_config(account_id, {})
        key = "allowFrom" if kind == "dm" else "groupAllowFrom"
        items = list(base_cfg.get(key) or [])
        if action == "add" and target not in items:
            items.append(target)
            set_override(account_id, key, items)
            return f"已添加 {target}"
        if action == "del" and target in items:
            items.remove(target)
            set_override(account_id, key, items)
            return f"已删除 {target}"
        return f"{target} 已在列表中"
    return "用法: /user|/group add|del|list [wxid]"
