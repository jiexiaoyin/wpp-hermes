"""WPP affection 好感度/社交关系系统（迁移自 wpp-openclaw inbound/affection.ts）。

移植自 AstrBot astrbot_plugin_self_learning 的 affection_manager。
纯规则核心：17 交互类型 × 分值 × 情绪修正 + 情绪状态机 + 情绪注入 system prompt。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# 情绪类型
MOOD_TYPES = ["normal", "happy", "playful", "excited", "sad", "anxious", "angry", "fear", "tired", "cold"]

# 情绪修正系数
MOOD_MODIFIERS = {
    "normal": 1.0, "happy": 1.2, "playful": 1.15, "excited": 1.3,
    "sad": 0.85, "anxious": 0.9, "angry": 0.8, "fear": 0.7, "tired": 0.75, "cold": 0.9,
}

# 交互规则表（17 类型）
AFFECTION_RULES = {
    "chat": {"baseChange": 1, "moodSensitive": True, "moodEffect": 0.1, "description": "普通聊天"},
    "compliment": {"baseChange": 3, "moodSensitive": True, "moodEffect": 0.2, "description": "称赞鼓励"},
    "praise": {"baseChange": 5, "moodSensitive": True, "moodEffect": 0.3, "positiveMoodBoost": True, "description": "夸赞表扬"},
    "encourage": {"baseChange": 4, "moodSensitive": True, "moodEffect": 0.25, "positiveMoodBoost": True, "description": "鼓励支持"},
    "support": {"baseChange": 4, "moodSensitive": True, "moodEffect": 0.2, "description": "支持认同"},
    "flirt": {"baseChange": 5, "moodSensitive": True, "moodEffect": 0.15, "moodRequirements": ["happy", "playful", "excited"], "description": "撩拨调情"},
    "comfort": {"baseChange": 4, "moodSensitive": True, "moodEffect": 0.3, "moodRequirements": ["sad", "anxious"], "description": "安慰关怀"},
    "help": {"baseChange": 2, "moodSensitive": False, "moodEffect": 0.1, "description": "寻求帮助"},
    "thanks": {"baseChange": 2, "moodSensitive": True, "moodEffect": 0.15, "description": "表达感谢"},
    "apology": {"baseChange": 1, "moodSensitive": True, "moodEffect": 0.1, "moodRequirements": ["angry", "sad"], "description": "道歉认错"},
    "tease": {"baseChange": 2, "moodSensitive": True, "moodEffect": 0.1, "moodRequirements": ["playful", "happy"], "description": "善意调侃"},
    "care": {"baseChange": 3, "moodSensitive": True, "moodEffect": 0.2, "description": "关心问候"},
    "gift": {"baseChange": 8, "moodSensitive": True, "moodEffect": 0.4, "positiveMoodBoost": True, "description": "赠送礼物"},
    "insult": {"baseChange": -8, "moodSensitive": True, "moodEffect": -0.5, "negativeMoodTrigger": True, "description": "侮辱攻击"},
    "harassment": {"baseChange": -6, "moodSensitive": True, "moodEffect": -0.4, "negativeMoodTrigger": True, "description": "骚扰行为"},
    "abuse": {"baseChange": -10, "moodSensitive": True, "moodEffect": -0.6, "negativeMoodTrigger": True, "description": "恶意谩骂"},
    "threat": {"baseChange": -12, "moodSensitive": True, "moodEffect": -0.7, "negativeMoodTrigger": True, "triggerFear": True, "description": "威胁恐吓"},
}

# 每群状态: {groupId: {users: {userId: affection}, mood, intensity, last_ts}}
_groups: dict[str, dict] = {}


def mood_modifier(mood: str, intensity: float) -> float:
    base = MOOD_MODIFIERS.get(mood, 1.0)
    return base * (0.5 + intensity * 0.5)


def _get_group(group_id: str) -> dict:
    if group_id not in _groups:
        _groups[group_id] = {
            "users": {},
            "mood": "normal",
            "intensity": 0.0,
            "last_ts": int(time.time() * 1000),
        }
    return _groups[group_id]


def get_user_affection(group_id: str, user_id: str) -> int:
    g = _get_group(group_id)
    return int(g["users"].get(user_id, 0))


def get_group_mood(group_id: str) -> str:
    g = _get_group(group_id)
    return g["mood"]


def _classify_by_rules(message: str) -> Optional[str]:
    """关键词规则分类（降级路径，LLM 不可用时用）。"""
    msg = message.lower()
    # 负面
    if any(k in msg for k in ["骂", "废物", "傻逼", "蠢", "滚", "去死"]):
        return "abuse"
    if any(k in msg for k in ["威胁", "杀了你", "小心点", "等着瞧"]):
        return "threat"
    if any(k in msg for k in ["骚扰", "变态", "恶心"]):
        return "harassment"
    if any(k in msg for k in ["蠢货", "白痴", "笨蛋"]):
        return "insult"
    # 正面
    if any(k in msg for k in ["太棒", "厉害", "优秀", "真棒", "赞"]):
        return "praise"
    if any(k in msg for k in ["谢谢", "感谢", "多谢"]):
        return "thanks"
    if any(k in msg for k in ["加油", "支持", "挺你", "相信你"]):
        return "support"
    if any(k in msg for k in ["爱你", "喜欢你", "么么"]):
        return "flirt"
    if any(k in msg for k in ["早安", "晚安", "在吗", "忙吗", "关心"]):
        return "care"
    if any(k in msg for k in ["帮我", "请问", "怎么", "如何", "求助"]):
        return "help"
    if any(k in msg for k in ["对不起", "抱歉", "道歉"]):
        return "apology"
    return "chat"


def calculate_affection_change(group_id: str, user_id: str, interaction_type: str) -> int:
    """计算好感度变化（含情绪修正）。返回变化值。"""
    rule = AFFECTION_RULES.get(interaction_type)
    if not rule:
        return 0
    g = _get_group(group_id)
    # P1-7.1 修复：moodRequirements 未实现（flirt/comfort 在任何 mood 都生效）
    #   对齐 OpenClaw affection.js：当前 mood 不在要求列表 → 该互动类型不生效（变化 0）
    mood_req = rule.get("moodRequirements")
    if mood_req and g.get("mood") and g["mood"] not in mood_req:
        return 0
    base = rule["baseChange"]
    # 情绪修正
    if rule["moodSensitive"]:
        modifier = mood_modifier(g["mood"], g["intensity"])
        base = round(base * modifier)
    # 应用变化（限幅 0..max）
    current = g["users"].get(user_id, 0)
    new_val = max(0, min(100, current + base))
    g["users"][user_id] = new_val
    # 情绪变化
    if rule.get("positiveMoodBoost"):
        g["mood"] = "happy"
        g["intensity"] = min(1.0, g["intensity"] + rule["moodEffect"])
    elif rule.get("negativeMoodTrigger"):
        g["mood"] = "angry" if not rule.get("triggerFear") else "fear"
        g["intensity"] = min(1.0, g["intensity"] + abs(rule["moodEffect"]))
    g["last_ts"] = int(time.time() * 1000)
    return base


def apply_mood_response(base_prompt: str, mood: str, intensity: float) -> str:
    """把情绪注入 system prompt，影响回复风格。"""
    style_map = {
        "happy": "你现在心情很好，回复要热情活泼，多用感叹号和开心的语气。",
        "playful": "你现在心情轻松愉快，回复可以俏皮、幽默、带点小玩笑。",
        "excited": "你现在很兴奋，回复要充满热情和活力。",
        "sad": "你现在心情有些低落，回复要温和体贴，不要太跳脱。",
        "anxious": "你现在有些焦虑，回复要简洁稳重，避免过度承诺。",
        "angry": "你现在有点生气，回复要克制，避免情绪化，保持专业。",
        "fear": "你现在感到不安，回复要谨慎小心，避免冲突。",
        "tired": "你现在有点疲惫，回复要简洁，避免长篇大论。",
        "cold": "你现在态度冷淡，回复要简洁疏离。",
    }
    if intensity < 0.2:
        return base_prompt
    style = style_map.get(mood, "")
    if style:
        return f"{base_prompt}\n[情绪状态] {style}（情绪强度 {intensity:.0%}）"
    return base_prompt


def process_affection_message(group_id: str, user_id: str, message: str,
                              use_llm: bool = False, classify: Optional[str] = None) -> int:
    """处理一条消息，更新好感度。返回变化值。classify 可外部传入 LLM 分类结果。"""
    if not group_id or not user_id:
        return 0
    itype = classify or _classify_by_rules(message)
    return calculate_affection_change(group_id, user_id, itype)


def get_affection_summary(group_id: str) -> str:
    """生成好感度摘要（供 agent 参考）。"""
    g = _get_group(group_id)
    if not g["users"]:
        return "（暂无社交关系数据）"
    top = sorted(g["users"].items(), key=lambda x: -x[1])[:5]
    parts = [f"{uid}:{lvl}" for uid, lvl in top]
    return f"当前情绪:{g['mood']}({g['intensity']:.0%}) 好感度TOP:{', '.join(parts)}"


def reset_group(group_id: str) -> None:
    _groups.pop(group_id, None)
