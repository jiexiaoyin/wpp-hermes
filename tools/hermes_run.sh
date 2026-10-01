#!/bin/sh
# 在 **Hermes 自己的运行时**里跑一个 python 脚本。
#
# 为什么需要它：Hermes 的模块（gateway.*、hermes_bootstrap）只存在于
#   隔离解释器 <P> = /root/.hermes/tools/python-*/bin/python3
#   + sys.path 注入 /root/.hermes/hermes-agent
# 系统 python 直接 import gateway 会 ModuleNotFoundError。
#
# 前导与 /root/.hermes/hermes-agent/.hermes/bin/hermes 保持一致
# （含 pop PYTHONPATH、-I 隔离），这样脚本看到的运行时与 gateway 启动时相同。
#
# 用法:
#   tools/hermes_run.sh <script.py> [args...]
# 覆盖 HERMES_HOME / HERMES_ROOT 可用同名环境变量。
set -eu

HERMES_ROOT="${HERMES_ROOT:-/root/.hermes/hermes-agent}"

if [ -n "${HERMES_PY:-}" ]; then
    PY="$HERMES_PY"
else
    PY="$(ls -d /root/.hermes/tools/python-*/bin/python3 2>/dev/null | head -1 || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
    echo "hermes_run.sh: 找不到 Hermes 隔离解释器（设 HERMES_PY 覆盖）" >&2
    exit 1
fi

[ "$#" -ge 1 ] || { echo "用法: $0 <script.py> [args...]" >&2; exit 64; }

SCRIPT="$1"
shift

HERMES_ROOT="$HERMES_ROOT" exec "$PY" -I -c '
import os, sys, runpy

os.environ.pop("PYTHONHOME", None)
os.environ.pop("PYTHONPATH", None)
sys.path.insert(0, os.environ.get("HERMES_ROOT", "/root/.hermes/hermes-agent"))
os.environ.setdefault("HERMES_HOME", "/root/.hermes")

import hermes_bootstrap  # noqa: F401 — 与官方 launcher 同样的初始化

argv = sys.argv[1:]
if not argv:
    sys.exit("hermes_run.sh: 缺少脚本路径")
script = argv[0]
sys.argv = argv
runpy.run_path(script, run_name="__main__")
' "$SCRIPT" "$@"
