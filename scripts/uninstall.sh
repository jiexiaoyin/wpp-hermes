#!/usr/bin/env bash
# 卸载 WeChatPadPro Hermes 插件。
#
#   bash scripts/uninstall.sh [--home DIR] [--profile NAME] [--purge]
#
# 默认保留 <插件目录>/accounts（账号配置）与 <home>/.env；--purge 连账号一起删。
# .env 里的 WECHATPRO_* 变量始终保留（可能与其他组件共用）。
set -euo pipefail

PLUGIN_NAME="wechatpadpro-platform"
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
PROFILE=""
PURGE=0

usage() {
  sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}
die() { printf '错误：%s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) [[ $# -ge 2 ]] || die "--home 需要参数"; HOME_DIR="$2"; shift 2 ;;
    --profile) [[ $# -ge 2 ]] || die "--profile 需要参数"; PROFILE="$2"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "未知参数：$1（--help 查看用法）" ;;
  esac
done

if [[ -n "$PROFILE" && "$HOME_DIR" == "${HERMES_HOME:-$HOME/.hermes}" ]]; then
  BASE="${HERMES_HOME:-$HOME/.hermes}"
  [[ "$(basename "$(dirname "$BASE")")" == "profiles" ]] && BASE="$(dirname "$(dirname "$BASE")")"
  HOME_DIR="$BASE/profiles/$PROFILE"
fi
HOME_DIR="${HOME_DIR/#\~/$HOME}"
DEST="$HOME_DIR/plugins/$PLUGIN_NAME"

if [[ ! -d "$DEST" ]]; then
  info "插件目录不存在：$DEST"
  exit 0
fi

if [[ "${WPP_INSTALL_SKIP_HERMES:-0}" == "1" ]]; then
  printf 'ℹ️  WPP_INSTALL_SKIP_HERMES=1，跳过 hermes 命令。\n'
elif command -v hermes >/dev/null 2>&1; then
  info "停用插件"
  if command -v timeout >/dev/null 2>&1; then
    HERMES_HOME="$HOME_DIR" timeout "${WPP_HERMES_TIMEOUT:-300}" \
      hermes plugins disable "$PLUGIN_NAME" </dev/null || true
  else
    HERMES_HOME="$HOME_DIR" hermes plugins disable "$PLUGIN_NAME" </dev/null || true
  fi
fi

if [[ "$PURGE" -eq 1 ]]; then
  info "删除插件目录（含 accounts）：$DEST"
  rm -rf "$DEST"
else
  info "删除代码，保留 accounts/：$DEST"
  find "$DEST" -mindepth 1 -maxdepth 1 ! -name accounts -exec rm -rf {} +
  printf '\n账号配置保留在：%s/accounts\n' "$DEST"
  printf '需要一起删除时运行：bash scripts/uninstall.sh --purge\n'
fi

cat <<EOF

已卸载。以下内容未被修改：
  - $HOME_DIR/.env 里的 WECHATPRO_* 变量
  - 数据库中的数据（如已配置 MariaDB）
EOF
