"""接龙（群接龙 / chat-history）识别与解析。

迁移自 TS ``inbound/relay.ts``。**这是红包误判事故的第一道闸** ——
:mod:`inbound.hongbao` 的 ``is_red_packet_message`` 第一句就是
``if is_relay_message(msg): return False``。

## 历史（三段，别只读一段）

- **v1.3.37**：只判 ``msgType === 53``（假设 chat-history）⇒ 真实 vendor 数据不符
  ⇒ 接龙**从不解析、从不触发**（集成 bug，测试用的是假设数据）。
- **v1.3.54**（2026-08-12 接总立）：真实接龙是 ``type=49``（app, category=app_message），
  title/content 以 ``#接龙`` 开头。改为 53 **或** 49+文本特征。
- **v1.9.1**（2026-09-27）：接龙文案**自带"红包"字样**（如"提升业绩, 领取红包🧧"），
  被关键词启发式误判成红包 ⇒ 整条静默丢弃且无报错。结构性识别必须优先于关键词。

## 两半（W7-3 补齐了后一半）

- **识别**（``is_relay_message``，老）：只回答"是不是接龙"。
- **解析**（``parse_relay_text`` + 三个 helper，W7-3 移植）：把 vendor 那坨
  ``#接龙 …\\n1. xx 2. yy`` 变成 ``title`` + 编号条目，再由
  :func:`render_relay_text` 整条改写成 ``[接龙] title\\n1. …``。
  ⚠️ **改写是"整体替换"不是"追加"**（TS ``handler.ts:545-552`` 就是
  ``m.content = …``）—— 追加会让正文里同时留着原始那坨和渲染结果。

⚠️ **三处 JS/Python 语义分叉，移植时全部显式对齐**（都不是口味问题）：

1. **``\\d``**：JS 的 ``\\d`` 恒为 ASCII ``[0-9]``，Python 的 ``\\d`` 是 **Unicode 数字**
   （``int("١٢")`` = 12）⇒ 本模块一律写 ``[0-9]``。同族：W7-1 的 ``re.ASCII``。
2. **``trim()``**：JS 从 ES5 起把 ``\\ufeff``（BOM）算作空白，而 Python 的
   ``str.isspace()`` **不认**它 ⇒ ``"\\ufeff#接龙".strip()`` 在 Python 里 BOM 还在。
   vendor 的 title/正文块实测常带 BOM，而现象只是"标题前面多个看不见的字符"
   ⇒ 用 :func:`_js_trim` 逐字对齐 JS 的空白集合。
3. **``Number()`` / ``undefined``**：TS 里缺失字段是 ``undefined``，落到
   :class:`RelayItem` 的默认值 ``""``（Python 侧没有 undefined，别写成 ``None``）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

#: ``MT_APP``（**不写死 49**）—— 卡片类型只在 :mod:`inbound.parser` 定义一次，
#: 别处一律引用符号。写死数字迟早与 parser 漂移，而漂移的那一刻就是"两处对 49
#: 的理解不一致"（hongbao/handler 之间那类事故的通用形态）。
from .parser import MT_APP, strip_group_prefix

log = logging.getLogger(__name__)

#: JS 正则里的 ``\s``（**含 ``\ufeff``**）在 Python 的样子。Python 的 ``\s`` 不认 BOM
#: ⇒ 凡是"JS 原文写 ``\s``"的地方都用这个类，**不写裸 ``\s``**（见模块 docstring ②）。
_JS_WS = "\\s\ufeff"
#: 同一集合的「字符类」写法，供 ``[...]`` 之外的位置用。
_JS_S = f"[{_JS_WS}]"

#: 接龙条目的编号行（``1. xxx``）。⚠️ ``[0-9]`` 不是 ``\d`` —— 见模块 docstring ①。
_NUMBERED_ITEM_RE = re.compile(rf"(?:^|\n){_JS_S}*[0-9]+\.{_JS_S}")

_JS_TRIM_RE = re.compile(f"^[{_JS_WS}]+|[{_JS_WS}]+$")

#: 条目行的开头（合并多行时判"这行是不是新条目"）
_ITEM_LINE_RE = re.compile(rf"^[0-9]+\.{_JS_S}")
#: 单行接龙的起点：**第一个 ``1. ``**（title 里的 ``3. 周年庆`` 必须被弃掉）
_ONE_ITEM_RE = re.compile(rf"(?:^|{_JS_S})1\.{_JS_S}")
#: 条目本体：``N. <text>``。``\s*`` 也必须是 JS 那个集合（``1.␅张三``）。
_ITEM_RE = re.compile(rf"^([0-9]+)\.{_JS_S}*(.*)$")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_RECORD_RE = re.compile(r"<recorditem>.*?</recorditem>", re.S)


def _js_trim(s: str) -> str:
    """JS ``String.prototype.trim()`` 的对等物（差别只有 ``\\ufeff``，见 docstring ②）。"""
    return _JS_TRIM_RE.sub("", s)


@dataclass
class RelayItem:
    """接龙列表里的一项。"""

    index: int                      # 1-based
    wxid: str = ""
    nickname: str = ""
    text: str = ""
    ts: int = 0


@dataclass
class RelayParseResult:
    """``parse_relay_text`` 的产物。TS 里是 ``{title, items}`` 两个字段的对象。"""

    title: str
    items: list[RelayItem] = field(default_factory=list)


def is_relay_message(msg: Any) -> bool:
    """是不是接龙消息。

    判据（对齐 TS ``isRelayMessage``）：

    - ``msg_type == 53`` ⇒ 是（旧 chat-history，历史兼容）
    - ``msg_type != 49`` ⇒ 否（非 appmsg 卡片不可能是接龙）
    - ``msg_type == 49`` 且（正文/``raw.app.title`` 含 ``#接龙``，或含 `"接龙"` +
      编号条目 ``N. xxx``）⇒ 是

    ⚠️ **绝不用 ``msg_type == 49`` 单独判接龙** —— 实测华为群 49 里含
    转账 86 / 应用消息 44 / 接龙 135+，只看类型会把转账和应用消息也当接龙。
    """
    msg_type = getattr(msg, "msg_type", None)
    try:
        msg_type = int(msg_type)
    except (TypeError, ValueError):
        return False

    if msg_type == 53:
        return True
    if msg_type != MT_APP:
        return False

    raw = getattr(msg, "raw", None)
    app = raw.get("app") if isinstance(raw, dict) and isinstance(raw.get("app"), dict) else {}
    title = app.get("title") if isinstance(app.get("title"), str) else ""

    text = f"{getattr(msg, 'content', '') or ''} {title}"
    if "#接龙" in text:
        return True
    return "接龙" in text and bool(_NUMBERED_ITEM_RE.search(text))


# --------------------------------------------------------------------- 解析层（W7-3）


def parse_record_item(block: str, idx: int) -> RelayItem:
    """``<recorditem>`` 块 → :class:`RelayItem`（``index`` 是 1-based，来自**位置**）。"""
    def field_of(name: str) -> str:
        m = re.search(rf"<{name}>(.*?)</{name}>", block, re.S)
        return _js_trim(m.group(1)) if m else ""

    return RelayItem(index=idx + 1, wxid=field_of("username"),
                     nickname=field_of("nickname"), text=field_of("text"))


def strip_title_prefix(text: str) -> str:
    """剥掉首个 ``N.`` 之前的接龙前缀（``#接龙 xxx 3. 周年庆 ...`` ⇒ 从首个 ``N.`` 起）。

    ⚠️ TS 写的是 ``idx > 0 ? slice : 原样`` —— ``idx == 0`` 时返回原串（等价于从头切）。
    **保持这个形状**：``idx > 0`` 是 TS 的原文，别"化简"成 ``>= 0``（读起来一样、边界差一个）。
    """
    m = re.search(r"[0-9]+\.\s", text)
    return text[m.start():] if m and m.start() > 0 else text


def parse_plain_list_single_line(text: str) -> list[RelayItem]:
    """**单行无换行**的接龙（vendor push 的真实格式）。

    实测：vendor 把 title + 规则 + 接龙者拼成一行，如
    ``#接龙 🎯 … 1. 门店＋型号 2. 某型号 某姓名``。

    策略（v1.3.63 P2-3 审阅）：条目**总是从 ``1. `` 开始** ⇒ 从第一个 ``1. `` 处切起
    （title 里即使有 ``3. 周年庆`` 也在 ``1. `` 之前被弃）；没有 ``1. `` 时回退成
    :func:`strip_title_prefix`。
    """
    one = _ONE_ITEM_RE.search(text)
    if one:
        body = re.sub(f"^[{_JS_WS}]+", "", text[one.start():])
    else:
        body = strip_title_prefix(text)
    return _split_items(body)


def parse_plain_list(text: str) -> list[RelayItem]:
    """按行/按 ``N.`` 切分的通用路径（多行 + 单行混合都走这里）。"""
    lines = [_js_trim(line) for line in re.split(r"\n+", text)]
    lines = [line for line in lines if line]          # TS ``.filter(Boolean)``
    if len(lines) == 1:
        return parse_plain_list_single_line(lines[0])

    # 不以 ``N.`` 开头的行 = 上一条的**延续**（条目内换行），合并时用**空格**接
    # （TS ``merged[last] += " " + line``）—— 用 ``\n`` 接会让下面的整段正则匹配不到。
    merged: list[str] = []
    for line in lines:
        if _ITEM_LINE_RE.match(line):
            merged.append(line)
        elif merged:
            merged[-1] += " " + line
        # else：前导描述（接龙标题/规则），忽略
    return _split_items("\n".join(merged))


def _split_items(body: str) -> list[RelayItem]:
    """把 ``1. a 2. b`` / ``1. a\\n2. b`` 切成条目（编号由**行内数字**给出，不重排）。"""
    items: list[RelayItem] = []
    for chunk in re.split(r"(?=[0-9]+\.\s)", body):
        if not _ITEM_LINE_RE.match(_js_trim(chunk)):
            continue
        m = _ITEM_RE.match(_js_trim(chunk))
        if not m:
            continue
        items.append(RelayItem(index=int(m.group(1)), text=_js_trim(m.group(2))))
    return items


def parse_relay_text(raw: str) -> RelayParseResult:
    """解析 vendor 推来的接龙正文（``chat-history`` / ``type=49 app``）。

    ⚠️ **title 里的字面 ``\\n``（``\\`` + ``n``）不是真换行**：vendor 会把它按字面写进
    title 块，不先换回来的话 ``[^\\n]`` 那类正则在真数据上只 match 第一行 ⇒
    ``items`` 恒 1 条（v1 的真 bug）。所以第一句就是这两个 replace。
    """
    text = (raw or "").replace("\\n", "\n").replace("\r\n", "\n")

    m = _TITLE_RE.search(text)
    title = strip_group_prefix(_js_trim(m.group(1))) if m else ""
    # v1.3.57：真实接龙 type=49 常没有 ``<title>`` ⇒ 退化成首行。
    # 没有这个兜底，所有接龙的 title 都是空 ⇒ 节流 key 相同 ⇒ 不同接龙互相节流。
    title = title or _js_trim(text.split("\n")[0] if text else "")

    blocks = _RECORD_RE.findall(text)
    items = ([parse_record_item(b, i) for i, b in enumerate(blocks)] if blocks
             else parse_plain_list(text))
    return RelayParseResult(title=title, items=items)


def render_relay_text(parsed: RelayParseResult) -> str:
    """渲染成注入正文的那一坨（TS ``handler.ts:547-549`` 逐字）。

    ``items`` 为空时结果是 ``"[接龙] title\\n"``（**末尾那个换行是 TS 的原样**，
    别顺手 ``.rstrip()``——它是"一个条目都还没接"的真实形态）。
    """
    joined = "\n".join(f"{it.index}. {it.text}" for it in parsed.items)
    return f"[接龙] {parsed.title}\n{joined}"


def apply_relay_render(msg: Any) -> bool:
    """**整条改写**接龙消息的正文（TS ``handler.ts:541-553``）。返回是否改写了。

    这里是 TS 那个 ``try/catch`` 的对等物：**永不抛**（解析失败只 warn、正文保持原样）
    —— 接龙解析坏了不该让整条消息消失，而现场表现只会是"bot 没看懂这条接龙"。

    两个副作用是一件事：**改正文** + **写 ``relay_rendered`` 键**（
    :meth:`inbound.parser.InboundPipeline._media_tag` 靠它不再打 ``[接龙]`` 前缀，
    否则正文里是"接龙 接龙"）。先改正文再写键：键写了而正文没改，那条消息会
    **连标记都没有**；正文改了而键没写，最多是重复一个标记 —— 后者更显眼。
    """
    if not is_relay_message(msg):
        return False
    try:
        parsed = parse_relay_text(getattr(msg, "content", "") or "")
        log.info("[WPP RELAY] 接龙已解析: title=%r items=%d msgType=%s",
                 parsed.title[:30], len(parsed.items), getattr(msg, "msg_type", ""))
        msg.content = render_relay_text(parsed)
        msg.media["relay_rendered"] = True
        return True
    except Exception as exc:  # noqa: BLE001 —— 与 TS 的 catch 同款：非致命
        log.warning("[WPP RELAY] 接龙解析失败（非致命，正文保持原样）: %s", exc)
        return False


__all__ = [
    "RelayItem",
    "RelayParseResult",
    "apply_relay_render",
    "is_relay_message",
    "parse_plain_list",
    "parse_plain_list_single_line",
    "parse_record_item",
    "parse_relay_text",
    "render_relay_text",
    "strip_title_prefix",
]
