#!/usr/bin/env python3
"""公开树敏感信息扫描。

扫描 Git 跟踪的文件，命中以下任一模式即以非零码退出：

* 真实生产 wxid / 微信号 / 群号；
* 厂商域名与内网地址；
* 生产密钥前缀；
* 本机绝对路径与内部主机名；
* 内部运维文档名。

占位符（``your-vendor.example``、``wxid_replace_me`` 等）不会命中。
CI 与本地共用；只读，不修改文件。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# 允许的文件：扫描器自身、示例账号、env 范例
SELF = Path(__file__).resolve()
SKIP_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".gz"}

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("生产 bot wxid", re.compile(r"wxid_eezdbu1ytws422")),
    ("生产微信号", re.compile(r"q139198824")),
    ("生产群号", re.compile(r"(?:53889526119|57737516566|12345678901)@chatroom")),
    ("厂商域名", re.compile(r"\b(?:wx|oss|adm)\.juhe\.chat\b|juhe\.chat")),
    ("生产密钥前缀", re.compile(r"(?:76db098c|a0da3904|71bed0f5)")),
    ("内网主机", re.compile(r"\b172\.20\.\d+\.\d+\b")),
    ("内部机器名", re.compile(r"\.183\b")),
    ("本机绝对路径", re.compile(r"/root/")),
    ("内部运维文档", re.compile(r"DEVELOPMENT_RULES")),
)


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True, capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"无法读取 git 文件列表：{exc}", file=sys.stderr)
        return []
    return [Path(p) for p in out.decode("utf-8", "replace").split("\0") if p]


def _iter_lines(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for lineno, line in enumerate(text.splitlines(), 1):
        yield lineno, line


def scan(files: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in files:
        if path.resolve() == SELF:
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        for lineno, line in _iter_lines(path):
            for label, pattern in PATTERNS:
                if pattern.search(line):
                    hits.append(f"{path}:{lineno}: [{label}] {line.strip()[:200]}")
    return hits


def main() -> int:
    files = _tracked_files()
    if not files:
        print("没有可扫描的 Git 跟踪文件", file=sys.stderr)
        return 2
    hits = scan(files)
    if hits:
        print(f"发现 {len(hits)} 处疑似敏感信息：\n", file=sys.stderr)
        for hit in hits:
            print(hit, file=sys.stderr)
        return 1
    print(f"公开树扫描通过：{len(files)} 个文件，未发现敏感信息")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
