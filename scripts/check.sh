#!/usr/bin/env bash
# 本地 / CI 统一门禁：lint + 测试 + 公开树敏感信息扫描。
#
#   bash scripts/check.sh              # 全部
#   bash scripts/check.sh --lint       # 只跑 ruff
#   bash scripts/check.sh --tests      # 只跑 pytest
#   bash scripts/check.sh --public     # 只跑敏感信息扫描
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

RUN_LINT=1
RUN_TESTS=1
RUN_PUBLIC=1

case "${1:-}" in
  --lint) RUN_TESTS=0; RUN_PUBLIC=0 ;;
  --tests) RUN_LINT=0; RUN_PUBLIC=0 ;;
  --public) RUN_LINT=0; RUN_TESTS=0 ;;
  -h|--help)
    sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  "") ;;
  *) printf '未知参数：%s（--help 查看用法）\n' "$1" >&2; exit 2 ;;
esac

PYTHON="${PYTHON:-python3}"

if [[ "$RUN_LINT" -eq 1 ]]; then
  printf '==> ruff check plugin\n'
  "$PYTHON" -m ruff check plugin
fi

if [[ "$RUN_TESTS" -eq 1 ]]; then
  printf '==> pytest plugin/tests\n'
  "$PYTHON" -m pytest plugin/tests -q
fi

if [[ "$RUN_PUBLIC" -eq 1 ]]; then
  printf '==> 公开树扫描\n'
  "$PYTHON" scripts/check_public_tree.py
fi

printf '\n全部通过。\n'
