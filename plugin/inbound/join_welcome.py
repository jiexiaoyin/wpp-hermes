"""新人进群欢迎语：三模式（白名单 / 黑名单 / 全放行）。

## 配置（账号 JSON 的 ``joinWelcome`` 段）

```json
"joinWelcome": {
  "enabled": true,
  "mode": "allowlist",              // allowlist | blocklist | all
  "groups": ["53977339882@chatroom"],
  "template": "欢迎 @{name} 加入{group}！",
  "llmPolish": true,                // 结合群画像写一句应景的
  "mention": true,
  "quietHours": [23, 0, 1, 2, 3, 4, 5, 6, 7],
  "maxBatch": 3
}

``{name}`` 是新人昵称（多人用「、」连接），``{group}`` 是群名（``wpp_chatrooms``
里同步来的）。
```

| 模式 | 语义 |
|---|---|
| ``allowlist`` | **只有** ``groups`` 里的群发（缺省模式，最保守） |
| ``blocklist`` | ``groups`` 里的群**不发**，其余都发 |
| ``all`` | 所有群都发（``groups`` 忽略） |

未知 ``mode`` 一律**回落 allowlist** —— 配错宁可少发，不能刷屏。

## 三道防刷屏

1. ``enabled`` 缺省 **false**：升级后不配就不发；
2. ``maxBatch``（默认 3）：一次 diff 出超过 3 个新人 ⇒ 判定为**基线重建**
   （首次同步 / 清表 / 迁移），**不发** —— 否则全员被欢迎一遍；
3. ``quietHours``：命中的小时静默（默认空 = 不静默）。

## 为什么只能靠 diff

vendor 对进群通知只推占位符（「收到一条微信系统通知」），**没有「谁进群」的原文**
（2026-10-20 实测 payload 全量确认）。所以欢迎语只能建立在
``wpp_chatroom_members`` 的前后差异上 —— 见 ``adapter._maybe_member_event``。

**代价**：vendor 漏推进群通知时，那次进群不会有欢迎语（实测会漏）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

log = logging.getLogger(__name__)

MODE_ALLOWLIST = "allowlist"
MODE_BLOCKLIST = "blocklist"
MODE_ALL = "all"
MODES = (MODE_ALLOWLIST, MODE_BLOCKLIST, MODE_ALL)

DEFAULT_TEMPLATE = "欢迎 @{name} 加入{group}！"

#: 一次最多欢迎几个人。超过就判定为"基线重建"而非真实进群 —— 见模块 docstring。
DEFAULT_MAX_BATCH = 3

_NAME_RE = re.compile(r"\{name\}")
_GROUP_RE = re.compile(r"\{group\}")


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_groups(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


def _as_hours(value: Any) -> frozenset[int]:
    out: set[int] = set()
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        for item in value:
            try:
                hour = int(item)
            except (TypeError, ValueError):
                continue
            if 0 <= hour <= 23:
                out.add(hour)
    return frozenset(out)


def resolve_cfg(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """把账号 JSON 的 ``joinWelcome`` 段解析成生效配置（缺省值全部填好）。"""
    cfg = dict(raw or {})
    mode = str(cfg.get("mode") or MODE_ALLOWLIST).strip().lower()
    if mode not in MODES:
        # 配错宁可少发：未知模式按最保守的 allowlist 处理。
        mode = MODE_ALLOWLIST
    template = str(cfg.get("template") or DEFAULT_TEMPLATE)
    if "{name}" not in template:
        # 没有占位符就等于"不点名"，允许，但补上更好用。
        template = f"{template} {{name}}"
    try:
        max_batch = int(cfg.get("maxBatch", DEFAULT_MAX_BATCH))
    except (TypeError, ValueError):
        max_batch = DEFAULT_MAX_BATCH
    return {
        "enabled": _as_bool(cfg.get("enabled"), False),
        "mode": mode,
        "groups": _as_groups(cfg.get("groups")),
        "template": template,
        "mention": _as_bool(cfg.get("mention"), True),
        #: 有群画像时让 LLM 写一句应景的（老板 2026-10-20 要求）。
        #: 失败一律回落模板文本 —— 欢迎语不能因为 LLM 抖动就不发。
        "llmPolish": _as_bool(cfg.get("llmPolish"), True),
        "quietHours": _as_hours(cfg.get("quietHours")),
        "maxBatch": max(1, max_batch),
    }


def should_welcome(cfg: Mapping[str, Any], chatroom_id: str) -> bool:
    """三模式判定：这个群该不该发欢迎语。"""
    if not cfg.get("enabled"):
        return False
    chatroom_id = str(chatroom_id or "").strip()
    if not chatroom_id:
        return False
    groups = set(cfg.get("groups") or ())
    mode = cfg.get("mode")
    if mode == MODE_ALL:
        return True
    if mode == MODE_BLOCKLIST:
        return chatroom_id not in groups
    return chatroom_id in groups          # allowlist（含未知模式回落）


def is_quiet_hour(cfg: Mapping[str, Any], hour: int) -> bool:
    return int(hour) in (cfg.get("quietHours") or frozenset())


def render(template: str, names: Iterable[str], group_name: str = "") -> str:
    """填 ``{name}`` / ``{group}``。多人用 ``、`` 连接（一条消息，不刷屏）。"""
    joined = "、".join(str(n).strip() for n in names if str(n).strip())
    text = _NAME_RE.sub(joined or "新成员", template)
    return _GROUP_RE.sub(str(group_name or "").strip() or "本群", text)


def build(
    cfg: Mapping[str, Any], chatroom_id: str,
    added: Iterable[tuple[str, str]], *, hour: int,
    group_name: str = "",
) -> tuple[str, list[str]] | None:
    """算出要发的内容与 @ 列表；不该发 ⇒ ``None``。

    ``added`` 来自 ``name_sync.sync_members`` 的 diff（``[(wxid, 昵称)]``）。
    """
    added = list(added or ())
    if not added:
        return None
    if not should_welcome(cfg, chatroom_id):
        return None
    if is_quiet_hour(cfg, hour):
        return None
    if len(added) > int(cfg.get("maxBatch") or DEFAULT_MAX_BATCH):
        # 一次冒出太多新人 ⇒ 多半是首次同步 / 清表后的基线重建，不是真进群。
        return None
    names = [nick or wxid for wxid, nick in added]
    text = render(str(cfg.get("template") or DEFAULT_TEMPLATE), names, group_name)
    ats = [wxid for wxid, _ in added if wxid] if cfg.get("mention", True) else []
    return text, ats


# ---------------------------------------------------------------- LLM 润色

_WELCOME_SYSTEM = (
    "你在给一个微信群写新人进群欢迎语。要求：一句话，45 字以内；"
    "必须以基础文案开头（「欢迎 @新人 加入群名」那一段照抄，不要改写名字和群名）；"
    "后半句结合群画像里**真实**的业务方向和聊天氛围来写；"
    "不浮夸、不堆 emoji、不用「热烈欢迎/蓬荜生辉」这类套话，"
    "也不要编造画像里没有的信息。只输出这一句话，不要解释、不要引号。"
)


async def polish(
    base_text: str, *, profile_text: str, group_name: str,
    model: str, timeout_ms: int = 8000,
) -> str:
    """结合群画像把欢迎语改写得应景一点。

    **任何失败都原样返回 ``base_text``** —— 欢迎语不能因为 LLM 抖动就不发。
    没有画像（``profile_text`` 为空）也直接返回基线：无为而治，别硬编。
    """
    if not profile_text:
        return base_text
    if not model:
        log.debug("[WPP WELCOME] 未配置模型，跳过润色")
        return base_text
    from ..llm.judge import call_judge

    prompt = (
        f"群名：{group_name or '本群'}\n"
        f"基础文案：{base_text}\n"
        f"群画像：\n{profile_text[:1200]}\n"
    )
    try:
        out = await call_judge(
            model=model, user_prompt=prompt, system_prompt=_WELCOME_SYSTEM,
            max_tokens=200, timeout_ms=timeout_ms,
        )
    except Exception as exc:  # noqa: BLE001 —— 润色失败不是发送失败
        log.warning("[WPP WELCOME] 群画像润色失败，用基线文案: %s", exc)
        return base_text
    text = str(out or "").strip().strip('"“”\'`').strip()
    # 太短/太长/明显跑题都退回基线：机器人宁可用模板，也别在群里说怪话。
    if not text or len(text) > 200 or "@" not in text:
        return base_text
    return text
