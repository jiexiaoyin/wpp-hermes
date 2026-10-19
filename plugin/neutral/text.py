"""通道无关的文本小工具（扩展边界 P0，2026-10-19）。

从 ``inbound/parser.py`` 抽出的两个纯函数，语义逐字保持一致：

* :func:`unwrap_vendor_string` —— 解 vendor 的 ``{"string": ...}`` 包装；
* :func:`safe_str` —— 解包后转字符串并去空白；解包后仍是容器则返回空串。

它们不依赖任何 WPP 类型，放在中立层供 ``quote_ref`` 等模块使用；``parser`` 的
同名私有函数保持原样，避免动入站主路径。
"""

from __future__ import annotations

from typing import Any


def unwrap_vendor_string(value: Any) -> Any:
    """递归解开 ``{"string": ...}`` 包装（最多 3 层，容忍嵌套）。"""
    seen = 0
    while isinstance(value, dict):
        if "string" not in value or seen >= 3:
            return value
        value = value["string"]
        seen += 1
    return value


def safe_str(value: Any) -> str:
    """``str`` + 去空白；解包后仍是 ``dict``/``list`` ⇒ 返回空串（不传播字面量）。"""
    value = unwrap_vendor_string(value)
    if isinstance(value, (dict, list)):
        return ""
    return str(value or "").strip()


__all__ = ["safe_str", "unwrap_vendor_string"]
