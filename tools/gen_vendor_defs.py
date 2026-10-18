#!/usr/bin/env python3
"""从旧树的两张「厂商端点表」生成 ``plugin/toolkit/vendor_defs{,_extra}.py``。

## 为什么要有这个生成器（消灭一类「静默丢工具」）

旧实现（``plugin/registration.py: _generic_groups``）靠一份**手写的组名列表**去
``getattr`` 逐个取组：

    for name in ("GROUP", "USER", "FRIEND", "SEARCH", ...):
        defs = getattr(td, f"{name}_TOOLS", None)
        if defs:                      # ← 拼错的名字在这里被静默吞掉
            rows.append((name.lower(), defs))

列表里一旦有个手滑的组名，整组工具就**悄无声息地消失**（历史上 10 个工具正是这么
没的）。本生成器把「组清单」改成**从数据里派生** —— 组名由 ``vars(module)`` 反射
得到，组顺序由源文件里的出现顺序得到。手抄组名这件事从此在结构上不可能再发生。

## 输入 / 输出

输入（本仓蓝本，唯一真源）：
    tools/vendor_source/tools_data.py        （20 组 / 112 条）
    tools/vendor_source/tools_data_extra.py  （15 组 / 107 条）

输出（本树，生成物，**禁止手改**）：
    plugin/toolkit/vendor_defs.py
    plugin/toolkit/vendor_defs_extra.py

## 两种模式

    python3 tools/gen_vendor_defs.py            # 写盘
    python3 tools/gen_vendor_defs.py --check    # 只比对；不一致则退出码 1

``--check`` 供 deploy.sh 当回归门：产物被谁手改了、或源表变了没重生成，都会在这里
被逐字节抓出来。

## 两个必须提防的坑

1. **``ALL_EXTRA_TOOLS`` 必须排除。** 它是 15 个 EXTRA 组拼起来的**聚合元组**
   （``EXTRA_GROUP_TOOLS + EXTRA_FRIEND_TOOLS + ...``，共 107 条），**不是一个独立
   组**。若把它也当组注册，107 个工具会被**重复注册两遍**。这里用两条独立判据排除：
   名字以 ``ALL_`` 开头，**或**内容恰好等于「其它各组按源顺序拼接」——双保险。

2. **聚合项是 ``tuple`` 不是 list。** 源里两种容器都有，取组一律用
   ``isinstance(v, (list, tuple))``，别只判 list。

## 数据保真

description / endpoint / params 的 ``type/required/desc`` **一个字都不改**，key 顺序、
组内条目顺序、中文原样保留。生成纯粹是**搬运**，不是「润色」。往返等价由 ``--check``
里内置的深度比对保证。

## 为什么生成物里没有「生成时间」戳

生成头若写 wall-clock 时间，同一份输入两次生成的字节就不同，``--check`` 永远失败。
所以头部改为记录**源文件的 SHA-256**（输入决定、可复现）作为溯源指纹：既说明
「由谁、从哪一版的源生成」，又保证生成是幂等的。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pprint
import re
import sys
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- 路径

#: 本生成器所在仓库根（``<repo>/tools/gen_vendor_defs.py`` → ``<repo>``）
REPO = Path(__file__).resolve().parent.parent
#: 产物目录
OUT_DIR = REPO / "plugin" / "toolkit"
#: 蓝本源表目录（**本仓内，唯一真源**）。
#:
#: 2026-10-18 变更：原来指向外部旧树 ``/root/dev/wpp-hermes/plugin`` —— 那棵树同时
#: 是"已废弃的扁平实现"和"仍在被本生成器读取的蓝本"，身份冲突：删它会把生成器打断，
#: 留它又让"唯一真源"分裂成两处（有人改旧树、有人改本仓，生成物到底反映谁说不清）。
#: 现在蓝本已复制进本仓（复制时逐文件 SHA-256 与原文件比对一致），旧树降级为历史。
SOURCE_DIR = REPO / "tools" / "vendor_source"

#: 组变量名的正则：顶层赋值的 ``<NAME>_TOOLS =``
_GROUP_DEF = re.compile(r"^([A-Z_]+)_TOOLS\s*=", re.M)

#: ``(源文件, 产物文件, 期望唯一工具名数, 期望组数)``
TARGETS: tuple[tuple[str, str, int, int], ...] = (
    ("tools_data.py", "vendor_defs.py", 112, 20),
    ("tools_data_extra.py", "vendor_defs_extra.py", 107, 15),
)

#: 跨文件重名与单文件重名的硬上限
_MAX_DUP = 0


# --------------------------------------------------------------------------- 加载


def load_module(path: Path, name: str) -> Any:
    """按**真 import** 加载旧表（不是正则解析）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法为 {path} 建立 import spec")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def source_order(src: str) -> list[str]:
    """源文件里 ``*_TOOLS =`` 的出现顺序（组顺序的唯一真源）。

    返回**完整变量名**（含 ``_TOOLS``），以便与 ``vars(mod)`` 的键直接对齐。
    """
    return [m.group(1) + "_TOOLS" for m in _GROUP_DEF.finditer(src)]


def reflected_groups(mod: Any) -> dict[str, Any]:
    """反射取组：``{k: v for k, v in vars(mod).items() if k.endswith("_TOOLS")}``。

    只保留 ``list`` / ``tuple``（坑 2：聚合项是 tuple）。
    """
    return {
        k: v
        for k, v in vars(mod).items()
        if k.endswith("_TOOLS") and isinstance(v, (list, tuple))
    }


# --------------------------------------------------------------------------- 排除


def is_aggregate(name: str, value: Any, all_groups: dict[str, Any]) -> bool:
    """判断是否聚合项（坑 1）。两条独立判据，任一命中即排除。

    - 名字以 ``ALL_`` 开头；
    - 内容恰好 == 其它各组按源顺序拼接（针对 ``ALL_EXTRA_TOOLS`` 这种显式子集拼接；
      即使它被改名，拼接判据也认得出）。
    """
    if name.startswith("ALL_"):
        return True
    others = [v for n, v in all_groups.items() if n != name]
    if len(others) < 2:
        return False
    return list(value) == [t for v in others for t in v]


def label_of(var_name: str) -> str:
    """组变量名 → 日志标签，与旧代码逐字对照。

    旧代码：主表 ``name.lower()``；extra 表 ``f"extra-{name.lower()}"``。
    等价泛化：去掉 ``_TOOLS`` 后缀；``EXTRA_`` 前缀 → ``extra-``；其余小写。
    """
    base = var_name[: -len("_TOOLS")]
    if base.startswith("EXTRA_"):
        return "extra-" + base[len("EXTRA_") :].lower()
    return base.lower()


def collect(path: Path, mod_name: str) -> tuple[list[tuple[str, tuple[dict, ...]]], dict]:
    """返回 ``([(标签, 定义元组), ...], meta)``；组顺序 = 源文件出现顺序。"""
    src = path.read_text(encoding="utf-8")
    mod = load_module(path, mod_name)
    groups = reflected_groups(mod)
    order = source_order(src)

    # 两面夹逼：正则拿到的顺序 & 反射拿到的名字，必须一一对应
    missing = [g for g in order if g not in groups]
    if missing:
        raise SystemExit(f"{path.name}: 正则顺序里有 {missing} 但反射取不到定义")
    extra_reflected = [k for k in groups if k not in order]
    if extra_reflected:
        raise SystemExit(f"{path.name}: 反射有 {extra_reflected} 但正则顺序里没有（非顶层 ?）")

    rows: list[tuple[str, tuple[dict, ...]]] = []
    excluded: list[str] = []
    for var in order:  # ← 源顺序，不是手抄顺序
        value = groups[var]
        if is_aggregate(var, value, groups):
            excluded.append(var)
            continue
        rows.append((label_of(var), tuple(value)))

    meta = {
        "src": str(path),
        "excluded": excluded,
        "sha256": hashlib.sha256(src.encode("utf-8")).hexdigest(),
    }
    return rows, meta


# --------------------------------------------------------------------------- 渲染

_HEADER = '''"""WPP 厂商端点定义表 —— {product}

**本文件由 `tools/gen_vendor_defs.py` 自动生成，请勿手改。**
改动请改源表后重跑生成器：

    python3 tools/gen_vendor_defs.py            # 写盘
    python3 tools/gen_vendor_defs.py --check    # 只校验（deploy.sh 回归门）

源（本仓蓝本）: {src}
源 SHA-256: {sha256}
派生组数: {ngroups}，工具条数: {ntools}{excl}

`GROUPS` 是唯一真源；新 `registration.py` 直接 `for group, defs in GROUPS:` 迭代，
**不再需要任何手写的组名列表**（旧实现那份手抄列表正是「静默丢工具」的温床）。
"""
'''


def _render(product: str, meta: dict, rows: list[tuple[str, tuple[dict, ...]]]) -> str:
    ntools = sum(len(defs) for _label, defs in rows)
    if meta["excluded"]:
        excl = f"（已排除聚合项: {', '.join(meta['excluded'])}）"
    else:
        excl = ""
    out: list[str] = [
        _HEADER.format(
            product=product,
            src=meta["src"],
            sha256=meta["sha256"],
            ngroups=len(rows),
            ntools=ntools,
            excl=excl,
        ),
        "from __future__ import annotations",
        "",
        "#: (组名, 该组工具定义) —— 顺序即注册顺序，与源文件一致",
        "GROUPS: tuple[tuple[str, tuple[dict, ...]], ...] = (",
    ]
    for label, defs in rows:
        out.append(f'    ("{label}", (')
        for d in defs:
            body = pprint.pformat(d, width=100, sort_dicts=False)
            out.extend("        " + ln for ln in body.splitlines())
            out[-1] += ","
        out.append("    )),")
    out.append(")")
    out.append("")
    out.append("TOOLS: tuple[dict, ...] = tuple(t for _g, _ts in GROUPS for t in _ts)")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- 校验


def _normalized(rows: Iterable[tuple[str, Iterable[dict]]]) -> str:
    """把 ``GROUPS`` 归一化成可逐字符比对的形式（组名/组序/组内序/全字段）。"""
    return json.dumps(
        [[label, [list(d.items()) for d in defs]] for label, defs in rows],
        ensure_ascii=False,
        sort_keys=False,
    )


def _tool_names(rows: Iterable[tuple[str, Iterable[dict]]]) -> list[str]:
    return [d["name"] for _label, defs in rows for d in defs]


def _assert_counts(tag: str, names: list[str], want_unique: int, ngroups: int, want_groups: int) -> None:
    uniq = len(set(names))
    dups = sorted({n for n in names if names.count(n) > 1})
    if len(names) != uniq:
        raise SystemExit(f"硬断言失败[{tag}]: 组内/组间重名 {dups}")
    if uniq != want_unique:
        raise SystemExit(f"硬断言失败[{tag}]: 唯一工具名 {uniq} ≠ 期望 {want_unique}")
    if ngroups != want_groups:
        raise SystemExit(f"硬断言失败[{tag}]: 组数 {ngroups} ≠ 期望 {want_groups}")


def roundtrip(product_rows: list[tuple[str, tuple[dict, ...]]], src_rows: list[tuple[str, tuple[dict, ...]]]) -> bool:
    """产物 ``GROUPS`` ↔ 源表 深度相等（含组名集合/组序/组内序/每条全部字段）。"""
    return _normalized(product_rows) == _normalized(src_rows)


def exec_product(text: str, tag: str) -> list[tuple[str, tuple[dict, ...]]]:
    """把生成的源码文本 exec 一遍，取它自己的 ``GROUPS``（不落盘也能往返）。"""
    ns: dict[str, Any] = {}
    exec(compile(text, f"<generated:{tag}>", "exec"), ns)
    groups = ns["GROUPS"]
    # 结构自检：tuple[tuple[str, tuple[dict, ...]], ...]
    assert isinstance(groups, tuple)
    for label, defs in groups:
        assert isinstance(label, str) and isinstance(defs, tuple)
        assert all(isinstance(d, dict) for d in defs)
    return list(groups)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 plugin/toolkit/vendor_defs{,_extra}.py")
    ap.add_argument("--check", action="store_true", help="只比对不写盘；不一致退出码 1")
    args = ap.parse_args(argv)

    sources: dict[str, list[tuple[str, tuple[dict, ...]]]] = {}
    outputs: list[tuple[Path, str]] = []
    all_names: list[str] = []

    for src_name, out_name, want_unique, want_groups in TARGETS:
        rows, meta = collect(SOURCE_DIR / src_name, src_name.replace(".py", ""))
        names = _tool_names(rows)
        _assert_counts(src_name, names, want_unique, len(rows), want_groups)
        sources[src_name] = rows
        all_names.extend(names)
        text = _render(out_name, meta, rows)
        outputs.append((OUT_DIR / out_name, text))

    # 跨文件重名硬断言（0）
    cross = sorted({n for n in all_names if all_names.count(n) > 1})
    if cross:
        raise SystemExit(f"硬断言失败[跨文件]: 重名 {cross}")

    print("[gen] 硬断言通过：")
    for src_name, _out, want_unique, want_groups in TARGETS:
        print(f"  - {src_name}: 组={want_groups} 唯一工具名={want_unique} 组内重名=0")
    print(f"  - 跨文件重名={len(cross)}")

    # 往返等价：把生成的**源码文本** exec 回来，与源表逐条深度比对
    ok = True
    for (out_path, text), (src_name, _o, _wu, _wg) in zip(outputs, TARGETS):
        landed = exec_product(text, out_path.name)
        same = roundtrip(landed, sources[src_name])
        n_landed = sum(len(d) for _l, d in landed)
        print(
            f"[gen] 往返等价 {out_path.name}: {'一致' if same else '不一致'} "
            f"(产物 {len(landed)} 组 / {n_landed} 条)"
        )
        ok = ok and same

    if args.check:
        bad = []
        for out_path, text in outputs:
            disk = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            if disk != text:
                bad.append(out_path.name)
        if bad or not ok:
            print(f"ERROR: 产物与源不一致：{bad or '（往返校验失败）'}", file=sys.stderr)
            print("       跑 `python3 tools/gen_vendor_defs.py` 重生成", file=sys.stderr)
            return 1
        print(f"[gen] --check 一致：{', '.join(p.name for p, _ in outputs)}")
        return 0

    for out_path, text in outputs:
        out_path.write_text(text, encoding="utf-8")
        print(f"[gen] 已写入 {out_path.relative_to(REPO)} ({len(text.splitlines())} 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
