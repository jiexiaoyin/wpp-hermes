"""``plugin/neutral/`` 的依赖边界（P0/P1，2026-10-19）。

中立层的价值全在"它不依赖通道"上；一旦有人图省事在里面 ``from ..platform…``，
所有搬迁就白做了，而且这个退化**没有任何运行时报错**（测试也照样绿）。
所以用 AST 把边界钉死：

* 只允许包内相对导入（``from .text import …``，``level == 1``）；
* 不允许 ``level >= 2`` 的相对导入（那是逃出 ``neutral/`` 去拿通道模块）；
* 不允许绝对导入本插件（``plugin.*`` / ``plugin.…``）。
"""

from __future__ import annotations

import ast
from pathlib import Path

NEUTRAL_DIR = Path(__file__).resolve().parents[1] / "neutral"


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level >= 2:
                bad.append(f"{path.name}:{node.lineno} 相对导入 level={node.level} "
                           f"module={node.module!r}")
            elif node.level == 0 and node.module and node.module.split(".")[0] == "plugin":
                bad.append(f"{path.name}:{node.lineno} 绝对导入 {node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "plugin":
                    bad.append(f"{path.name}:{node.lineno} 绝对导入 {alias.name}")
    return bad


def test_neutral_package_never_imports_channel_modules() -> None:
    files = sorted(NEUTRAL_DIR.glob("*.py"))
    assert files, "neutral 目录为空？"
    bad = [msg for path in files for msg in _violations(path)]
    assert not bad, "中立层越界：\n" + "\n".join(bad)
