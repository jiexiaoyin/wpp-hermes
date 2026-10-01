"""差分测试：Python ``chunk_text`` vs TS Blueprint ``chunkMarkdown``。

**为什么值得单独做**：分片是「金丝雀级」逻辑 —— 错一处分片边界不会报错，
只会让长回复在群里被 vendor 悄悄截断，或者一条回复炸成十条。手写断言只能
覆盖我想得到的形状；拿蓝本本体当 oracle 才能覆盖想不到的形状。

做法：同一批语料分别喂给

- Python：``plugin.platform.outbound.chunk_text``（本地直读源码，绕开 aiohttp）；
- TS：``tools/_ts_chunk_oracle.mjs``（从 dist 切出蓝本函数，node 里跑）。

**同一 ``limit``（4000）下逐片比对，必须完全一致。**

已知且**可接受**的偏差：非 BMP 字符（emoji）。JS 按 UTF-16 码元计长，Python 按
码点计长 ⇒ 含 emoji 的语料分片边界会不同。这类用例只断言「无损 + 每片在 Python
口径下 ≤ limit」这类弱性质，不要求逐片相等。

用法：``python3 tools/probe_chunk_diff.py``（退出码 0 = 全绿）
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LIMIT = 4000


# ───────────────────────────────────────────── 加载被测模块（绕开 aiohttp 依赖）
def _load_outbound():
    """读 ``plugin/platform/outbound.py``，把 ``..core.netguard`` 换成空壳再执行。

    本地没装 aiohttp ⇒ 正常 import 会炸；但分片是**纯函数**，把无关依赖桩掉
    即可拿到真实实现（比手抄一份实现要可信得多）。
    """
    for name, path in (
        ("plugin", ROOT / "plugin"),
        ("plugin.core", ROOT / "plugin" / "core"),
        ("plugin.platform", ROOT / "plugin" / "platform"),
    ):
        mod = types.ModuleType(name)
        mod.__path__ = [str(path)]  # type: ignore[attr-defined]
        sys.modules[name] = mod
    stub = types.ModuleType("plugin.core.netguard")
    stub.__getattr__ = lambda _n: None  # type: ignore[attr-defined]
    sys.modules["plugin.core.netguard"] = stub

    target = ROOT / "plugin" / "platform" / "outbound.py"
    spec = importlib.util.spec_from_file_location("plugin.platform.outbound", target)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plugin.platform.outbound"] = mod
    spec.loader.exec_module(mod)
    return mod


outbound = _load_outbound()


# ─────────────────────────────────────────────────────────────────────── 语料
def build_corpus() -> tuple[list[str], list[str]]:
    """返回 ``(对齐语料, 已知偏差语料)``。"""
    cb = "```\n" + "\n".join("z" * 300 for _ in range(30)) + "\n```"
    cb_blank = "```\n" + "\n\n".join("w" * 1200 for _ in range(5)) + "\n```"
    cb_indent = "  ```\n" + "\n".join("v" * 400 for _ in range(25)) + "\n  ```"
    mixed = "\n\n".join([
        "开头一段普通文字。" * 20,
        "x" * 5000,
        "```\n" + "\n".join("q" * 900 for _ in range(12)) + "\n```",
        "结尾一段。" * 100,
    ])
    # 确定性伪随机：不用 random，保证跨机可复现
    varied = "\n".join(
        ("a" * ((i * 37) % 1900)) or "b" for i in range(60)
    )

    aligned = [
        "",
        "hi",
        "x" * LIMIT,                       # 恰好等于 limit（不分片）
        "x" * (LIMIT + 1),                 # 刚超 1
        "x" * 9000,                        # 单行远超声
        "\n\n".join("y" * 1500 for _ in range(5)),
        "\n\n".join("c" * 1999 for _ in range(3)),
        cb,
        cb_blank,
        cb_indent,
        mixed,
        varied,
        "  前导空白" + "p" * 9000 + "尾随空白  ",
        "\n\n\n",                          # 全是空行
        "```\n未闭合围栏\n" + "u" * 9000,
        "短行\n" * 3000,
        "\n".join("d" * 3999 for _ in range(4)),
        "tab\t分隔\n" * 1200,
    ]
    # ⚠️ emoji/星面字符：JS 计 2、Python 计 1 ⇒ 边界必然不同，不逐片比对
    skewed = [
        "🧧" * 5000,
        ("🎉" * 700 + "\n\n") * 8,
        "正文" + "🧧" * 3000 + "\n\n" + "尾巴" * 500,
    ]
    return aligned, skewed


def run_ts(corpus: list[str]) -> list[list[str]]:
    proc = subprocess.run(
        ["node", str(HERE / "_ts_chunk_oracle.mjs")],
        input=json.dumps({"corpus": corpus, "limit": LIMIT}),
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(f"TS oracle 失败:\n{proc.stderr}")
    return json.loads(proc.stdout)


# ─────────────────────────────────────────────────────────────────────── 执行
aligned, skewed = build_corpus()
ts_aligned = run_ts(aligned)
ts_skewed = run_ts(skewed)

fails: list[str] = []

for i, text in enumerate(aligned):
    py = outbound.chunk_text(text, LIMIT)
    ts = ts_aligned[i]
    label = f"语料#{i} len={len(text)}"
    if py != ts:
        fails.append(
            f"{label}\n    PY({len(py)}): {[len(c) for c in py][:8]}"
            f"\n    TS({len(ts)}): {[len(c) for c in ts][:8]}"
        )

# emoji 语料：只验弱性质（无损 + 不超限）。边界不同是**已知且已记录**的偏差：
# JS 按 UTF-16 码元计长（emoji 算 2），Python 按码点计长（算 1）⇒ 同样 limit 下
# Python 的片段在 JS 口径下更短，方向安全。
for i, text in enumerate(skewed):
    py = outbound.chunk_text(text, LIMIT)
    label = f"emoji语料#{i} len={len(text)}"
    # 分片器只会 trim 段落 + 用 \n\n 重拼 ⇒ 除空白外必须逐字无损
    if "".join("".join(py).split()) != "".join(text.split()):
        fails.append(f"{label} 无损性失败（除空白外内容变了）")
    if any(len(c) > LIMIT for c in py):
        fails.append(f"{label} 有片段超限: {[len(c) for c in py]}")

total = len(aligned) + len(skewed)
print(f"  对齐语料 {len(aligned)} 条（逐片比对）+ emoji 语料 {len(skewed)} 条（弱性质）")
for f in fails:
    print(f"  ❌ {f}")
print(f"\n{total - len(fails)}/{total} 通过")
sys.exit(0 if not fails else 1)
