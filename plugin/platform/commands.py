"""WPP filehelper 命令处理（迁移自 wpp-openclaw index.ts FILEHELPER_COMMANDS）。

filehelper 消息（发给「文件传输助手」）触发命令，管理功能开关 / 白名单。
命令写入 ``core/overrides.py`` 的内存覆盖，**不重启即改**（下一次账号解析生效，
见该模块 docstring 的「生效时机」）。

从旧扁平 ``plugin/commands.py`` 迁入 ``platform/``，装配方式有三处改动：

1. 覆盖存储改从 ``..core.overrides`` 取（旧的是本模块自己定义，导致配置层反向依赖命令层）。
2. 当前状态改读 ``core.accounts.resolve(account_id).raw``（旧的是
   ``config.resolve_account_config``）—— ``raw`` 是**已并入覆盖**的 cfg，
   所以不必再手工叠一层覆盖判断。
3. **删掉 ``/pair`` 分支**（旧的是死码）：``extract_pair_code`` → ``redeem_pairing_code``
   只校验码就回「配对码有效」，**从不真把谁写进白名单**（旧 adapter 也从未调用它）。
   留着一条会骗人的回复，比没有更糟。真正要做「自助加白名单」，得先解决
   「加进哪份白名单」—— 覆盖是内存态、重连就回退到 ``accounts/*.json``，
   拿它当"永久加入"是假的（见 ``platform/pairing.py`` docstring）。
   ``/genpair`` / ``/pairs`` 保留：它们是**管理员自己看码**，本来就是通的。
4. ``/heartflow`` 的**子命令面**搬到 :mod:`.hf_commands`（status 详情 / threshold /
   group / profile / why / layers / veto / report），本模块只留一行分发。理由是那个
   面每一条都要读台账/分层/画像，而本模块是"三功能开关 + 白名单"的通用件 ——
   混在一起会让"改心流展示口径"变成"动一个通用文件"（TS 侧 2026-09-28 同理提取）。
"""
from __future__ import annotations

import logging

from ..core import overrides

log = logging.getLogger(__name__)

HELP_TEXT = """可用命令：
/heartflow on|off|status — 心流主动回复开关（status 含 24h 台账 / 生效阈值 / 预算 / 分层）
/heartflow threshold|group|profile|why|layers|veto|report — 心流运维（见 /heartflow 空参数）
/affection on|off|status — 好感度开关
/jargon on|off|status — 黑话挖掘开关
/user add|del|list [wxid] — 私聊白名单
/group add|del|list [群id] — 群白名单
/genpair — 生成配对码
/pairs — 查看配对码
/help — 显示帮助
"""

#: 覆盖里用的**原始 cfg 键**（驼峰）—— ``core.accounts.resolve()`` 就是按这些名字
#: 把覆盖并进 cfg 的，写成别的名字读不到。
_ALLOWLIST_KEYS = {"dm": "allowFrom", "group": "groupAllowFrom"}


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

    # 心流：开关走通用实现，其余子命令（status 详情 / threshold / group / profile /
    # why / layers / veto / report）在 ``hf_commands`` 里，这里**只做分发**。
    # 导入是**函数级**的：hf_commands 在模块级 import 心流七个子模块，放进加载期
    # 就等于给注册链路加一段心流加载代价（F5 的形态，见该模块 docstring）。
    if cmd == "/heartflow":
        from . import hf_commands

        return hf_commands.handle(account_id, arg_list)

    # 功能开关: /affection /jargon
    if cmd in ("/affection", "/jargon"):
        return _handle_feature_toggle(account_id, cmd.lstrip("/"), arg_list)

    # 白名单: /user /group
    if cmd in ("/user", "/group"):
        return _handle_allowlist(account_id, "dm" if cmd == "/user" else "group", arg_list)

    # 配对码: /genpair /pairs（/pair 兑换已删，见模块 docstring）
    if cmd == "/genpair":
        from .pairing import generate_pairing_code
        entry = generate_pairing_code(account_id)
        return f"✅ 新配对码已生成:\n\n配对码: {entry['code']}\n有效期至: {_fmt_ms(entry['expires_at'])}\n\n用法: 发给白名单外用户，用户私聊机器人发 /pair {entry['code']} 自助加入"
    if cmd == "/pairs":
        from .pairing import read_pairing_code
        entry = read_pairing_code(account_id)
        if entry:
            return f"当前配对码:\n\n配对码: {entry['code']}\n有效期至: {_fmt_ms(entry['expires_at'])}"
        return "当前无配对码（未生成或已过期）。用 /genpair 生成。"

    return f"未知命令 {cmd}，/help 查看帮助"


# --------------------------------------------------------------------------- 内部

def _fmt_ms(ms: int) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _safe_cfg(account_id: str) -> dict:
    """取**已并入覆盖**的原始 cfg；解析失败返回空 dict（命令链路不该因配置瑕疵整体哑掉）。

    典型失败：账号没配 ``agent`` ⇒ ``resolve()`` 按设计 raise（串号是安全事故）。
    这时命令只能按内存覆盖判断状态，够用。
    """
    try:
        from ..core import accounts as _accounts

        return dict(_accounts.resolve(account_id).raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("[WPP] 命令(account=%s) 读不到账号配置: %s —— 仅按内存覆盖判断", account_id, exc)
        return {}


def _feature_state(account_id: str, feature: str) -> bool:
    """当前开关状态。覆盖优先（与旧实现同序），覆盖没有才看配置。"""
    ov = overrides.get_override(account_id).get(feature)
    if isinstance(ov, dict) and "enabled" in ov:
        return bool(ov["enabled"])
    feat = _safe_cfg(account_id).get(feature)
    return bool(isinstance(feat, dict) and feat.get("enabled"))


def _allowlist_items(account_id: str, key: str) -> list[str]:
    """当前白名单。**覆盖优先**，同 :func:`_feature_state` 的次序。

    ``resolve()`` 会把覆盖并进 cfg，所以正常路径下两支等价；``_safe_cfg`` 返空时
    这一支至少还能看见覆盖 —— 否则 ``/user list`` 会显示"(空)"、``/user add``
    会把同一个人反复 append（旧实现就是靠 cfg 单支读的，解析一失败就错乱）。
    """
    ov = overrides.get_override(account_id).get(key)
    if isinstance(ov, list):
        return [str(x) for x in ov]
    return [str(x) for x in (_safe_cfg(account_id).get(key) or [])]


def _feature_cfg(account_id: str, feature: str) -> dict:
    """写覆盖时的**底稿**：保留阈值等同级键，别让开关动作把整段配置拍扁。

    ``resolve()`` 返回的 cfg 已并入现有覆盖，所以正常路径下取它就够了。
    但 ``_safe_cfg`` 返空时（账号配置有瑕疵 / ``resolve()`` raise）**必须**退回读现有覆盖 ——
    否则一次 ``/heartflow off`` 会把之前设过的 ``replyThreshold`` 一起抹掉，
    而"静默丢配置"正是本树要消灭的那类失败（旧实现就有这个洞，只是没人踩到）。
    """
    feat = _safe_cfg(account_id).get(feature)
    if isinstance(feat, dict):
        return dict(feat)
    ov = overrides.get_override(account_id).get(feature)
    return dict(ov) if isinstance(ov, dict) else {}


def _handle_feature_toggle(account_id: str, feature: str, args: list[str]) -> str:
    current = _feature_state(account_id, feature)

    if not args:
        return f"/{feature} 当前状态: {'on' if current else 'off'}"
    action = args[0].lower()
    if action in ("on", "1", "true", "yes"):
        overrides.set_override(account_id, feature, {**_feature_cfg(account_id, feature), "enabled": True})
        return f"/{feature} 已开启 ✅"
    if action in ("off", "0", "false", "no"):
        overrides.set_override(account_id, feature, {**_feature_cfg(account_id, feature), "enabled": False})
        return f"/{feature} 已关闭 ✅"
    if action == "status":
        return f"/{feature} 状态: {'on' if current else 'off'}"
    return f"/{feature} 参数: on/off/status"


def _handle_allowlist(account_id: str, kind: str, args: list[str]) -> str:
    key = _ALLOWLIST_KEYS[kind]
    label = "私聊" if kind == "dm" else "群"
    if not args:
        return "用法: /user|/group add|del|list [wxid]"
    action = args[0].lower()
    if action == "list":
        items = _allowlist_items(account_id, key)
        return f"{label}白名单: {', '.join(items) if items else '(空)'}"
    if action in ("add", "del") and len(args) >= 2:
        target = args[1]
        items = _allowlist_items(account_id, key)
        if action == "add" and target not in items:
            items.append(target)
            overrides.set_override(account_id, key, items)
            return f"已添加 {target}"
        if action == "del" and target in items:
            items.remove(target)
            overrides.set_override(account_id, key, items)
            return f"已删除 {target}"
        return f"{target} 已在列表中"
    return "用法: /user|/group add|del|list [wxid]"
