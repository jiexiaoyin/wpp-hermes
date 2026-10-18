"""工具返回值序列化的**唯一出口** —— 带\"可解析截断\"的 ``json.dumps``。

## 为什么需要（2026-10-18 修）

旧写法在 7 处直接 ``json.dumps(payload, ...)[:4000]``。超出 4000 字符时这不是
"压缩"，而是**把 JSON 从中间切断**：模型收到的是一段语法非法的半截文本
（实测 ``wpp_group_list`` 返回以 ``"access_type":`` 结尾 —— 键后面什么都没有），
既解析不了、也不知道自己被截了，只能干瞪眼或猜。而这类端点恰恰就是大结果：
群列表 / 群成员 / 朋友圈列表 / 文章正文 / 通讯录。

所以截断必须**结构化**：超限时换成一个合法 JSON 信封

    {"ok": ..., "truncated": true, "full_chars": 52341, "cap": 4000,
     "hint": "结果超出工具返回上限已截断…", "head": "{\"ok\": true, \"code\": 0, ..."}

``head`` 里放原文前缀（**字符串**，不是裸 JSON，所以整体永远合法），模型至少能读到
开头那部分结构，并知道该用 ``limit``/``offset``/更精确的参数把范围缩小。

## 不变量

1. 返回值**永远**是合法 JSON（``json.loads`` 必成功）；
2. 长度**≤ ``cap``**（对中文/引号等会膨胀的字符也会重算，见末尾循环）；
   ``cap`` 小于信封本体最小长度时以"合法 JSON"优先（病态 cap 无法两全）；
3. 不超限时**行为完全不变**（逐字节等于旧的 ``json.dumps``）—— 这是为了让既有
   调用方与断言不受影响。
"""

from __future__ import annotations

import json
from typing import Any

#: 与旧实现保持一致的默认上限。
DEFAULT_CAP = 4000

_HINT = ("结果超出工具返回上限已截断，head 是原文前缀；"
         "请用 limit/offset/更精确的筛选参数缩小范围后重试")


def dump_capped(payload: Any, cap: int = DEFAULT_CAP) -> str:
    """序列化 ``payload``；超 ``cap`` 时返回合法 JSON 信封而不是半截文本。"""
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= cap:
        return text

    ok = payload.get("ok") if isinstance(payload, dict) else True
    envelope: dict[str, Any] = {
        "ok": ok,
        "truncated": True,
        "full_chars": len(text),
        "cap": cap,
        "hint": _HINT,
        "head": "",
    }
    # 先扣掉信封本体的长度，再把剩余预算给 head；head 里若含引号/反斜杠会被转义
    # 而膨胀，所以逐个收紧直到装得下（最多十几轮，正常 1~2 轮就够）。
    overhead = len(json.dumps(envelope, ensure_ascii=False))
    # cap 比"空 head 的信封"还短时，两个不变量无法同时成立；截断的意义是让模型
    # 看见结构，所以宁可超一点也必须给合法 JSON（调用方传的是病态 cap）。
    if overhead > cap:
        return json.dumps(envelope, ensure_ascii=False)
    budget = max(0, cap - overhead - 2)
    while True:
        envelope["head"] = text[:budget]
        out = json.dumps(envelope, ensure_ascii=False)
        if len(out) <= cap or budget <= 0:
            return out
        budget = int(budget * 0.85)


def dump_error(message: str, **extra: Any) -> str:
    """统一的失败返回：**JSON**（``ok:false``），而不是裸字符串。

    旧实现的失败路径有两种形态 —— 一部分返回 ``调用失败: xxx`` 纯文本、一部分返回
    ``{"ok": false}``。模型对前者只能当自由文本读，没法可靠判断成败。这里统一成
    JSON，同时保留原消息。
    """
    payload: dict[str, Any] = {"ok": False, "error": str(message)}
    payload.update(extra)
    return dump_capped(payload)
