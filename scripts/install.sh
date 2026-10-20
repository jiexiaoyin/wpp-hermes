#!/usr/bin/env bash
# 安装 / 升级 WeChatPadPro Hermes 插件。
#
#   bash scripts/install.sh [--home DIR] [--profile NAME] [--upgrade]
#                           [--no-wizard] [--dry-run]
#
# 行为：
#   1. 复制 plugin/ → <HERMES_HOME>/plugins/wechatpadpro-platform（保留 accounts/）
#   2. 调用 hermes plugins enable
#   3. 调用 setup_wizard.py 完成配置
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$REPO_DIR/plugin"
PLUGIN_NAME="wechatpadpro-platform"

HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
PROFILE=""
UPGRADE=0
RUN_WIZARD=1
DRY_RUN=0

usage() {
  sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home) [[ $# -ge 2 ]] || die "--home 需要参数"; HOME_DIR="$2"; shift 2 ;;
    --profile) [[ $# -ge 2 ]] || die "--profile 需要参数"; PROFILE="$2"; shift 2 ;;
    --upgrade) UPGRADE=1; shift ;;
    --no-wizard) RUN_WIZARD=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
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

[[ -f "$SRC_DIR/plugin.yaml" ]] || die "找不到 $SRC_DIR/plugin.yaml；请在仓库根目录运行"
[[ -d "$SRC_DIR" ]] || die "找不到插件源目录 $SRC_DIR"

DEST="$HOME_DIR/plugins/$PLUGIN_NAME"

info "Hermes home: $HOME_DIR"
[[ -n "$PROFILE" ]] && info "Profile: $PROFILE"
info "插件目标:   $DEST"

if [[ -d "$DEST" && "$UPGRADE" -eq 0 ]]; then
  info "目标已存在，将先备份再覆盖（等同 --upgrade）"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  info "dry-run：不写入任何文件"
  exit 0
fi

mkdir -p "$HOME_DIR/plugins"
mkdir -p "$DEST"

if [[ -d "$DEST" && -n "$(ls -A "$DEST" 2>/dev/null || true)" ]]; then
  BACKUP="$HOME_DIR/plugins/.backup-$PLUGIN_NAME-$(date +%Y%m%d-%H%M%S)"
  info "备份旧目录 → $BACKUP"
  cp -a "$DEST" "$BACKUP"
fi

info "复制代码（保留 accounts/ 与运行期状态）"
(
  cd "$SRC_DIR"
  tar -cf - \
    --exclude='accounts' \
    --exclude='pairing' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.bak-*' \
    . | (cd "$DEST" && tar -xf -)
)
mkdir -p "$DEST/accounts"
chmod 700 "$DEST/accounts"

run_hermes() {
  local limit="${WPP_HERMES_TIMEOUT:-300}"
  if command -v timeout >/dev/null 2>&1; then
    HERMES_HOME="$HOME_DIR" timeout "$limit" hermes "$@" </dev/null
  else
    HERMES_HOME="$HOME_DIR" hermes "$@" </dev/null
  fi
}

if [[ "${WPP_INSTALL_SKIP_HERMES:-0}" == "1" ]]; then
  printf 'ℹ️  WPP_INSTALL_SKIP_HERMES=1，跳过 hermes 命令。\n'
elif command -v hermes >/dev/null 2>&1; then
  info "启用插件"
  run_hermes plugins enable "$PLUGIN_NAME" || {
    printf '⚠️  hermes plugins enable 失败或超时；稍后可手动执行：\n  HERMES_HOME=%q hermes plugins enable %s\n' "$HOME_DIR" "$PLUGIN_NAME" >&2
  }
else
  printf '⚠️  未找到 hermes 命令；跳过启用。安装 Hermes 后执行：\n  HERMES_HOME=%q hermes plugins enable %s\n' "$HOME_DIR" "$PLUGIN_NAME" >&2
fi

if [[ "$RUN_WIZARD" -eq 1 ]]; then
  WIZARD_ARGS=(--home "$HOME_DIR")
  [[ -n "$PROFILE" ]] && WIZARD_ARGS+=(--profile "$PROFILE")
  info "运行配置向导"
  python3 "$DEST/setup_wizard.py" "${WIZARD_ARGS[@]}" || {
    printf '⚠️  向导未完成；可稍后重跑：\n  python3 %q --home %q\n' "$DEST/setup_wizard.py" "$HOME_DIR" >&2
  }
fi

info "验收"
if [[ "${WPP_INSTALL_SKIP_HERMES:-0}" != "1" ]] && command -v hermes >/dev/null 2>&1; then
  run_hermes plugins doctor "$DEST" || true
fi

cat <<EOF

下一步：
  1. 重启 gateway：systemctl --user restart hermes-gateway
  2. 用白名单里的微信私聊机器人发一条消息
  3. 只读检查：python3 "$DEST/setup_wizard.py" --check --json
EOF
