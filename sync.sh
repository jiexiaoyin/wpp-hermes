#!/bin/bash
# ============================================================
# 只做「dev → 生产」的同步与校验，**不碰服务生命周期**。
#
# 为什么单独一个脚本：`deploy.sh` 的 9/10 步会重启网关，而网关进程内部
# 不允许执行重启类命令（安全策略会先杀掉发起命令的子进程）。于是 agent /
# CI 在网关内只能跑到「代码已就位、校验全过」这一层，重启交由人在外部 shell 做。
#
# 铁律不变：**代码只在 dev 改**（/root/dev/wpp-hermes-py2），生产目录是产物。
# 本脚本只允许 dev → 生产这一个方向。
#
# 用法:
#   bash sync.sh              # 同步 + 全部门禁 + md5 校验
#   bash sync.sh --check      # 只校验 dev 与生产是否一致（不写任何东西）
#   bash sync.sh --rollback <备份目录>
#   DEPLOY_HOST=root@x bash sync.sh      # 远程目标（默认本机生产）
# ============================================================
set -euo pipefail

DEV="/root/dev/wpp-hermes-py2"
H="/root/.hermes"
PLUGIN_DEPLOY="$H/plugins/wechatpadpro"

# 本机有 /data 就按老板规矩落 /data；远程/无 /data 退回 $H/backups
if [ -d /data ]; then BAK_ROOT="/data/wpp-deploy-backup"; else BAK_ROOT="$H/backups"; fi

MODE="sync"; BK=""
case "${1:-}" in
    --check) MODE="check" ;;
    --rollback) MODE="rollback"; BK="${2:?用法: sync.sh --rollback <备份目录>}" ;;
    "") ;;
    *) echo "未知参数: $1"; exit 64 ;;
esac

cd "$DEV" || exit 1
PY="${PY:-python3}"
TS=$(date +%Y%m%d-%H%M%S)

echo "=============================================="
echo " dev → 生产 同步（$MODE）"
echo " dev    : $DEV/plugin"
echo " 生产   : $PLUGIN_DEPLOY"
echo " 备份根 : $BAK_ROOT"
echo "=============================================="

# ---------------------------------------------------------------- 回滚
if [ "$MODE" = "rollback" ]; then
    [ -d "$BK/plugin" ] || { echo "❌ 备份里没有 plugin/: $BK"; exit 1; }
    echo "回滚 plugin ← $BK/plugin"
    # ⚠️ ``accounts/`` 是**生产运行数据**（账号配置/凭据），不是代码产物：
    #   备份里的那份可能过期，直接 --delete 会把上线后新增的账号删掉/把新配置回退。
    #   回滚只回代码；账号配置永远保留现场。
    rsync -a --delete --exclude 'accounts/' --exclude '__pycache__/' \
          --exclude '*.pyc' --exclude '*.log' \
          "$BK/plugin/" "$PLUGIN_DEPLOY/"
    echo "✅ 已回滚（accounts/ 保留生产现状）。重启网关后才生效（命令见 DEVELOPMENT_RULES §部署）。"
    exit 0
fi

# ---------------------------------------------------------------- 一致性检查
same() {
    diff -rq --exclude='__pycache__' --exclude='accounts' --exclude='*.pyc' \
             --exclude='*.log' plugin "$PLUGIN_DEPLOY" >/dev/null 2>&1
}

if [ "$MODE" = "check" ]; then
    if same; then echo "✅ dev 与生产一致（排除 accounts/缓存/日志）"; exit 0; fi
    echo "⚠️  存在差异："
    diff -rq --exclude='__pycache__' --exclude='accounts' --exclude='*.pyc' \
             --exclude='*.log' plugin "$PLUGIN_DEPLOY" | head -30
    exit 1
fi

# ---------------------------------------------------------------- 门禁（本机）
echo
echo "── 1/5 语法检查（dev，全部 .py）"
find plugin -name '*.py' -not -path '*__pycache__*' -print0 | xargs -0 -n50 "$PY" -m py_compile

echo "── 2/5 生成物幂等（禁止手改生成物）"
"$PY" tools/gen_vendor_defs.py --check >/dev/null
"$PY" tools/port_tools_domain.py --check >/dev/null 2>&1 || true   # 一次性移植工具，缺席不挡
echo "   ✅ vendor_defs 与蓝本一致"

echo "── 3/5 注册对账（真跑 register()，比 provides_tools）"
"$PY" tools/probe_registration.py | tail -2

echo "── 4/5 结构守卫"
"$PY" tools/verify_tree.py | tail -1

# ---------------------------------------------------------------- 备份 + 同步
echo
echo "── 备份生产现状"
mkdir -p "$BAK_ROOT/$TS"
cp -a "$PLUGIN_DEPLOY" "$BAK_ROOT/$TS/plugin"
echo "   ✅ $BAK_ROOT/$TS/plugin"

echo "── 同步 plugin/（排除 accounts / 缓存 / 日志 / 版本库）"
rsync -a --delete \
      --exclude '/accounts/' --exclude '__pycache__/' --exclude '*.pyc' \
      --exclude '.git/' --exclude '*.log' \
      plugin/ "$PLUGIN_DEPLOY/"

echo "── 5/5 目标机语法复检 + md5 逐位校验"
"$PY" -m compileall -q "$PLUGIN_DEPLOY" >/dev/null
fail=0
while IFS= read -r -d '' f; do
    rel="${f#plugin/}"
    case "$rel" in accounts/*|*__pycache__*|*.pyc|*.log) continue ;; esac
    a=$(md5sum "$f" | awk '{print $1}')
    b=$(md5sum "$PLUGIN_DEPLOY/$rel" 2>/dev/null | awk '{print $1}')
    [ "$a" = "$b" ] || { echo "   ❌ $rel"; fail=$((fail+1)); }
done < <(find plugin \( -name '*.py' -o -name '*.yaml' -o -name '*.sql' \) -print0)

if [ "$fail" -ne 0 ]; then
    echo
    echo "❌ $fail 个文件不一致 —— 回滚：bash sync.sh --rollback $BAK_ROOT/$TS"
    exit 1
fi

echo "   ✅ md5 全部一致"
echo
echo "=============================================="
echo " 代码已就位。**重启网关后生效**（命令见 DEVELOPMENT_RULES §部署）。"
echo " 回滚：bash sync.sh --rollback $BAK_ROOT/$TS"
echo "=============================================="
