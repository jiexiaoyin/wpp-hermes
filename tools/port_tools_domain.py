"""把旧扁平 ``plugin/tools.py`` **机械地**切分成 ``plugin/tools/`` 包。

## 为什么用脚本而不是手抄

``register_msg_tools`` 有 660 行**手工调过的生产逻辑**（每个 vendor 端点的参数名、
错误码判定、mmx CLI 调用链都是踩过坑的）。手抄一遍就是给"抄错一个参数名"开后门，
而且抄错的后果是运行期才发现。

所以这里做的是**行区间搬家 + 少量登记在案的导入重定向**，并被
``tools/verify_tools_port.py`` 用 AST 逐函数比对原文 ——
**任何未登记的改动都会被抓住**。

## 搬家地图

========================================  ==========================================
旧位置（``plugin/tools.py``）            新位置
========================================  ==========================================
``_get_client`` … ``_get_noop_adapter_getter``   ``plugin/tools/registry.py``（机器）※
``register_msg_tools``                   ``plugin/tools/msg.py``                 ← 本脚本
``register_mcp_tools``                   ``plugin/tools/mcp.py``                 ※
``register_jargon_tools``                ``plugin/tools/jargon_tool.py``         ← 本脚本
``_LOCAL_READ_DIRS`` / ``_is_local_read_allowed`` / ``_safe_fetch_url`` /
``_resolve_to_base64``                   **不搬** —— 已被 ``core/netguard.py`` +
``platform/outbound.py`` 取代（单一真源）
``register_tools``（973+，死入口）        **不搬** —— F3 实测它从未被调用
========================================  ==========================================

※ ``registry.py`` / ``mcp.py`` 由另一路负责（``mcp.py`` 还要接新的 repo 读函数），
  不由本脚本写盘 —— 但本脚本的 ``SPLITS`` 仍保留它们的原文区间，
  ``tools/verify_tools_port.py`` 拿它做比对基准。

用法::

    python3 tools/port_tools_domain.py            # 生成（幂等）
    python3 tools/port_tools_domain.py --check    # 生成到内存与磁盘比对（deploy 门）
    python3 tools/port_tools_domain.py --range    # 只打印原文区间
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 蓝本 = **冻结的旧树**里的扁平 ``tools.py``，不是本树同名文件。
#: 两个理由：① 本树那份已被 ``plugin/tools/`` 目录**遮蔽**（同名包优先于模块），
#: 且它本身在删除清单上；② 旧树按决策「原样冻结」作参照与回滚蓝本 ⇒ 生成器
#: 永远可复现，不会因为本树清理而失效。
LEGACY = Path("/root/dev/wpp-hermes/plugin/tools.py")
OUT_DIR = ROOT / "plugin" / "tools"

#: 每个目标文件的「锚点区间」——用函数名定位，不写死行号（行号会漂）。
SPLITS: dict[str, tuple[str, str | None]] = {
    # 目标文件名 -> (起锚正则, 止锚正则；None = 到文件尾)
    "registry.py": (r"^def _get_client\(", r"^# -+ msg 域工具"),
    "msg.py": (r"^def register_msg_tools\(", r"^def register_mcp_tools\("),
    "mcp.py": (r"^def register_mcp_tools\(", r"^def register_jargon_tools\("),
    "jargon_tool.py": (r"^def register_jargon_tools\(", r"^# 本地文件读取白名单"),
}

#: **不搬**的区间（必须在原文里出现，否则说明蓝本变了，得重新评估）
MUST_SKIP: tuple[str, ...] = (
    r"^def register_tools\(",          # F3: 死入口，从未被调用
    r"^async def _resolve_to_base64\(",  # 已被 outbound.resolve_to_base64 取代
    r"^async def _safe_fetch_url\(",     # 已被 netguard.safe_fetch_url 取代
    r"^def _is_local_read_allowed\(",    # 已被 netguard.is_local_read_allowed 取代
)

# --------------------------------------------------------------------------- 头部

_MSG_HEADER = '''"""msg 域工具（``/Msg/*``）—— 从旧 ``plugin/tools.py`` **逐字搬来**。

## 为什么是逐字搬，而不是重写

``register_msg_tools`` 是 **660 行踩过坑的生产逻辑**：每个 vendor 端点的参数名、
``ret`` 码判定、mmx CLI 调用链、语音/视频的 base64 兜底分支……手抄一遍就是给
「抄错一个参数名」开后门，而抄错的后果**只在运行期暴露**（厂商多半回一个与真因
无关的错误码）。所以这里选择生成 + 比对，而不是重打字。

本文件由 ``tools/port_tools_domain.py`` 生成，``tools/verify_tools_port.py``
用 AST 逐函数与旧实现比对 —— **任何未登记在案的字面改动都会让它红。**

## 与旧实现的全部差异（就这两条）

1. 函数体内 ``from .api_client import WppApiError``
   → ``from ..core.api_client import WppApiError``（包结构变了）
2. 旧的**同文件模块级** ``_get_client`` / ``_mk_handler`` / ``_resolve_to_base64``
   现在由本文件头部提供别名 / 包装：前两个指向 :mod:`.registry`，第三个见下。

## ``_resolve_to_base64`` 为什么要包一层（别图省事直接引用）

新 ``platform.outbound.resolve_to_base64`` 失败时返回**空串**；旧实现同名函数
失败时 **raise**。msg 域 5 个调用点的历史契约是「要么拿到 base64，要么抛错」。
若直接引用返空串的那个版本，``send_file`` 会把 ``Base64=""`` 交给厂商，厂商回一个
**与真因无关**的错误码 —— 正是老板最恨的那类事故（"日志说成功 / 报错指向错的根因"，
参考出站语音那个 Code=-2 文案为 INVALID_CREDENTIAL 的坑）。

所以这里把「本该拿到内容却拿到空」重新抛成 ``WppApiError``，同时**保留**旧实现的
两条原样返回分支：空串入参、以及既非 URL 也非路径者（当作已经是 base64）。
唯一**有意**的偏离：``data:`` 无逗号的畸形输入，旧实现原样返回，这里抛错。
"""

from __future__ import annotations

import logging

from . import registry

log = logging.getLogger(__name__)

#: 别名 —— 为的是让下面 660 行函数体**一字不改**（见模块 docstring 差异 2）
_get_client = registry.get_client
_mk_handler = registry.mk_handler


async def _resolve_to_base64(ref: str) -> str:
    """旧契约的**抛错版**包装（别换成直接引用 outbound 那个，见模块 docstring）。"""
    # ★ 依赖延迟到调用期：`core/api_client.py` 顶层 `import aiohttp`，
    #   而 `register()` 会被 `plugin_validate` 在**隔离 probe 子进程**里整体跑一遍
    #   （那个解释器没有 aiohttp）⇒ 本模块的**模块级**导入必须只依赖标准库 +
    #   `registry`。旧的 `_resolve_to_base64` 也是这么在函数内 import 的。
    from ..core.api_client import WppApiError
    from ..platform import outbound

    ref = (ref or "").strip()
    if not ref:
        return ""  # 旧实现末支：空串原样返回，不抛（send_file 允许 fileUrl 缺省）
    if not (ref.startswith("data:") or ref.startswith("http") or ref.startswith("/")):
        return ref  # 旧实现末支：既非 URL 也非路径 ⇒ 当作已是 base64
    b64 = await outbound.resolve_to_base64(ref)
    if not b64:
        raise WppApiError(f"媒体解析失败（取不到内容，拒绝发送空媒体）: {ref[:60]}")
    return b64

'''

_JARGON_HEADER = '''"""jargon 黑话查询工具（``wpp_jargon_query``）—— 从旧 ``plugin/tools.py`` 逐字搬来。

本文件由 ``tools/port_tools_domain.py`` 生成。差异只有一条（登记在案）：

    函数体内 ``from .jargon import ...`` → ``from ..inbound.jargon import ...``

因为 ``plugin/jargon.py`` 已迁到 ``plugin/inbound/jargon.py``（该模块零内部依赖，
是纯 stdlib，所以搬动是安全的 —— 见 ``plugin/inbound/__init__.py``）。
"""

from __future__ import annotations

import json
import logging

from . import registry

log = logging.getLogger(__name__)

_mk_handler = registry.mk_handler

'''

HEADERS: dict[str, str] = {"msg.py": _MSG_HEADER, "jargon_tool.py": _JARGON_HEADER}

#: 逐文件的**登记在案的重写**：``(旧串, 新串, 期望出现次数)``。
#: 函数体除这些精确串之外必须逐字相同 —— 次数也钉死，蓝本多出一条同类语句就会报错。
#: ``tools/verify_tools_port.py`` 把同一张表套到旧原文上再比对 —— 单一真源。
REWRITES: dict[str, tuple[tuple[str, str, int], ...]] = {
    "msg.py": ((
        "    from .api_client import WppApiError\n",
        "    from ..core.api_client import WppApiError\n",
        1,
    ),),
    "jargon_tool.py": ((
        "        from .jargon import query_jargon as qj, list_hot_jargon\n",
        "        from ..inbound.jargon import query_jargon as qj, list_hot_jargon\n",
        1,
    ),),
    "mcp.py": (
        # 6 处 handler 内的 DB 取用：旧扁平 ``plugin/db.py`` → 新的 ``data/repo_messages``。
        # 绑定成 ``db`` 这个名字是为了**不动调用点写法**。
        ("        from . import db\n",
         "        from ..data import repo_messages as db\n", 6),
        # 两个调用点改名以复用既有实现（**没有**为它们另写一份 SQL）——
        # 签名逐一核对过：query_contact_by_wxid(account_id, wxid) ↔ get_contact(...)，
        # query_chatroom(account_id, chatroom_id) ↔ get_chatroom(...)
        ("db.query_contact_by_wxid(account_id, cid)",
         "db.get_contact(account_id, cid)", 1),
        ("db.query_chatroom(account_id, gid)",
         "db.get_chatroom(account_id, gid)", 1),
        # 注册循环：旧名带前导下划线，新树在 registry 里已去掉
        ("handler=_mk_handler(fn)",
         "handler=mk_handler(fn)", 1),
    ),
}

#: 逐文件的**函数改名表**（旧名 → 新名）。仅 ``registry.py``：旧机器的 5 个函数
#: 都去掉了前导下划线（``_get_client`` → ``get_client`` 等）。
#: 验算器在 AST 上做改名（不碰字符串/注释），再比对函数体。
RENAMES: dict[str, dict[str, str]] = {
    "registry.py": {
        "_get_client": "get_client",
        "_mk_handler": "mk_handler",
        "_mk_generic_handler": "generic_handler",
        "_register_generic_tools": "register_generic_tools",
        "_get_noop_adapter_getter": "noop_adapter_getter",
    },
}

#: 本脚本负责写盘的目标（其余区间只用作比对基准）
GENERATED: tuple[str, ...] = ("msg.py", "jargon_tool.py")


# --------------------------------------------------------------------------- 切分

def _slice(text: str, start_re: str, end_re: str | None) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(start_re, l)), None)
    if start is None:
        raise SystemExit(f"❌ 找不到起锚 {start_re!r} —— 蓝本 tools.py 变了，先重新评估搬家地图")
    if end_re is None:
        return start, len(lines)
    end = next((i for i, l in enumerate(lines) if i > start and re.match(end_re, l)), None)
    if end is None:
        raise SystemExit(f"❌ 找不到止锚 {end_re!r}")
    return start, end


def legacy_body(name: str, text: str) -> str:
    """旧原文里该目标文件对应的区间（**未套重写**）—— 供生成与比对共用。"""
    a, b = _slice(text, *SPLITS[name])
    return "".join(text.splitlines(keepends=True)[a:b]).strip("\n")


def apply_rewrites(name: str, body: str) -> str:
    for old, new, want in REWRITES.get(name, ()):
        got = body.count(old)
        if got != want:
            raise SystemExit(
                f"❌ {name}: 登记的重写目标出现 {got} 次（期望 {want}）: {old!r}\n"
                "   蓝本变了（语句增删 / 行首缩进变了）—— 先核对，别盲生成"
            )
        body = body.replace(old, new)
    return body


def render(name: str, body: str) -> str:
    return HEADERS[name] + "\n" + body + "\n"


def main() -> int:
    text = LEGACY.read_text(encoding="utf-8")

    for pattern in MUST_SKIP:
        if not re.search(pattern, text, re.M):
            raise SystemExit(f"❌ 预期「不搬」的 {pattern!r} 在原文里不存在 —— 蓝本已变，别盲搬")

    if "--range" in sys.argv:
        for name, (s, e) in SPLITS.items():
            a, b = _slice(text, s, e)
            mark = "生成" if name in GENERATED else "基准"
            print(f"  {mark} {name:18s} 原文 [{a + 1}, {b}] 共 {b - a} 行")
        return 0

    if "--check" in sys.argv:
        bad: list[str] = []
        for name in GENERATED:
            want = render(name, apply_rewrites(name, legacy_body(name, text)))
            path = OUT_DIR / name
            have = path.read_text(encoding="utf-8") if path.exists() else ""
            if have != want:
                bad.append(name)
                print(f"  ❌ {name} 与旧原文生成结果不一致（手改过？或蓝本变了）")
            else:
                print(f"  ✅ {name}")
        return 1 if bad else 0

    OUT_DIR.mkdir(exist_ok=True)
    for name in GENERATED:
        path = OUT_DIR / name
        path.write_text(render(name, apply_rewrites(name, legacy_body(name, text))), encoding="utf-8")
        n = len(path.read_text(encoding="utf-8").splitlines())
        print(f"  ✅ 已生成 plugin/tools/{name:16s} {n:4d} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
