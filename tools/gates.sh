#!/bin/sh
# 全部门禁的**唯一入口** —— 本地能跑的都在这。deploy.sh 会先调它。
#
# 为什么要有这个文件：门禁散成六个脚本时，改完代码"记得跑哪个"全靠人脑，
# 而漏跑的后果不是报错、是**静默少注册**（F5 就是这么活了很久的）。
#
# 它**不**替代 .183 上的真机验收（V3/V6：`hermes plugins doctor` 的
# registrations 计数、`hermes plugins validate` 的依赖检查）—— 那些必须在
# Hermes 运行时里跑，见 tools/hermes_run.sh。
#
# 用法:
#   tools/gates.sh            # 在仓库根跑全部
#   tools/gates.sh <root>     # 指到别的树（如部署目录）自查
set -u

# 剥掉**生产 profile 的环境变量**：门禁跑在运维 shell 里，而这些变量是给 gateway 进程用的。
# 不清会把门禁变成假红（2026-10-18 实测：JUDGE_BASE_URL 让 verify_judge 期望默认端点失败；
# WECHATPRO_SEND_ALLOWLIST 让 2 个 pytest 用例假红）。清理是幂等的，没设也无副作用。
unset JUDGE_API_KEY JUDGE_BASE_URL JUDGE_FALLBACK_API_KEY JUDGE_FALLBACK_BASE_URL \
      JUDGE_FALLBACK_MODEL WECHATPRO_SEND_ALLOWLIST WECHATPRO_AUTHCODE_LIVETEST \
      WECHATPRO_ALLOW_PRODUCTION_WXID 2>/dev/null || true

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# 参数解析（必须在 cd 之前：第 1 个位置参数可能是树路径，也可能是 --only）
#
#   tools/gates.sh                     全量（10 分钟以上）
#   tools/gates.sh /path/to/tree       指定门禁树
#   tools/gates.sh --only 变异测试·预算  只跑名字含该关键词的项
#   tools/gates.sh --only lint
ONLY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --only) ONLY="${2:-}"; shift 2 || shift ;;
        --only=*) ONLY="${1#--only=}"; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) ROOT="$1"; shift ;;
    esac
done
cd "$ROOT" || exit 1
[ -n "$ONLY" ] && printf '\033[33m（--only %s：不匹配的项会标记跳过）\033[0m\n' "$ONLY"
# 选一个**带 pytest** 的 python3。
#
# 为什么不能直接写 `PY=${PY:-python3}`：在 Hermes 的工具进程里，PATH 上的 python3 是
# Hermes 自己的 venv（`~/.hermes/installs/<hash>/environments/<hash>/venv/bin/python3`），
# 那里**没有 pytest**。于是 `gates.sh` 每一项都报 "基线就没过：No module named pytest"，
# 三个热路径守卫全变假红 —— 2026-10-19 实测。
#
# 规则：$PY 显式给了就完全信任它（CI 可以钉死）；否则按顺序找第一个能 import pytest 的。
pick_python() {
    for cand in python3 /usr/bin/python3 /usr/local/bin/python3 python; do
        command -v "$cand" >/dev/null 2>&1 || continue
        "$cand" -c 'import pytest' >/dev/null 2>&1 && { echo "$cand"; return 0; }
    done
    return 1
}

if [ -n "${PY:-}" ]; then
    if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
        echo "❌ PY=$PY 里没有 pytest（显式指定时不做回退）" >&2
        exit 1
    fi
else
    PY=$(pick_python) || {
        echo "❌ 找不到带 pytest 的 python3；用 PY=/path/to/python3 指定" >&2
        exit 1
    }
fi

# 测试里 import plugin.platform.adapter 需要 Hermes 的 gateway 包（框架契约）。
# 不设这条，test_handle_one_gates / test_adapter_webhook_runtime 会在收集阶段
# 报 ModuleNotFoundError，看起来像"测试文件坏了"。
if [ -d /root/.hermes/hermes-agent ]; then
    export PYTHONPATH="/root/.hermes/hermes-agent${PYTHONPATH:+:$PYTHONPATH}"
fi

FAILED=""
NPASS=0
NSKIP=0
NSKIP=0

# run <名字> <命令...>
run() {
    name=$1
    shift
    if [ -n "$ONLY" ]; then
        case "$name" in
            *"$ONLY"*) ;;
            *)
                NSKIP=$((NSKIP + 1))
                printf '  \033[2m⏭  跳过（--only %s）: %s\033[0m\n' "$ONLY" "$name"
                return 0 ;;
        esac
    fi
    printf '\n\033[1m── %s\033[0m\n' "$name"
    if "$@" 2>&1; then
        NPASS=$((NPASS + 1))
    else
        printf '\033[31m   ↑ 上面这个失败了\033[0m\n'
        FAILED="$FAILED
  - $name"
    fi
}

echo "门禁树: $ROOT"

run "结构守卫（形状 / 加载期依赖 / F5 对策）" "$PY" tools/verify_tree.py "$ROOT"
run "工具层无损移植（AST 等价 + 真跑差异）"   "$PY" tools/verify_tools_port.py
run "生成物幂等 port_tools_domain（--check）" "$PY" tools/port_tools_domain.py --check
run "生成物幂等 gen_vendor_defs（--check）"   "$PY" tools/gen_vendor_defs.py --check
run "注册探针（真跑 register()，对账 provides_tools）" "$PY" tools/probe_registration.py "$ROOT"
run "LLM 层 judge（双格式/凭证链/关思考/空正文抛错/看图）" "$PY" tools/verify_judge.py

# lint：配置在 pyproject.toml（规则集与豁免理由都写在那里）。
# **硬门禁**：缺席不再静默跳过 —— 旧写法在没装 ruff 的机器上直接放行，
# lint 可以悄悄失守而全部门禁仍报绿。现在两种调用方式都认：
#   ① 当前解释器带 ruff 模块（仓库 dev 依赖的常规姿势）；
#   ② PATH 上的 ruff 二进制（系统 Python 常被 PEP 668 管理，装不进 site-packages，
#      但 `uv tool install ruff` / pipx 的二进制可用）。
# 两者都没有 ⇒ 记失败，并打印可照抄的安装命令。
RUFF_MOD=0
RUFF_BIN=""
if "$PY" -m ruff --version >/dev/null 2>&1; then
    RUFF_MOD=1
elif command -v ruff >/dev/null 2>&1; then
    RUFF_BIN=$(command -v ruff)
fi
if [ "$RUFF_MOD" = "1" ]; then
    run "lint（ruff，规则集见 pyproject.toml）" "$PY" -m ruff check plugin tools --no-cache
elif [ -n "$RUFF_BIN" ]; then
    run "lint（ruff，规则集见 pyproject.toml）" "$RUFF_BIN" check plugin tools --no-cache
else
    printf '\n\033[1m── lint（ruff）\033[0m\n'
    printf '\033[31m   ❌ 找不到 ruff：lint 是硬门禁，不再静默跳过\033[0m\n'
    printf '      安装（二选一）：\n'
    printf '        %s -m pip install "ruff>=0.15,<1"\n' "$PY"
    printf '        uv tool install ruff        # 系统 Python 受 PEP 668 管理时\n'
    FAILED="$FAILED
  - lint（ruff 缺失）"
fi

# TS 差分要 node + 部署目录的 dist。**缺席就跳过**（本地没参照物不该挡住干活），
# 但**在就位却不一致时必须 fail** —— 那才是真信号。
TS_DIST=${TS_DIST:-/root/.openclaw/extensions/wechatpadpro/dist/llm-judge.js}
if [ -f "$TS_DIST" ] && command -v node >/dev/null 2>&1; then
    run "judge 差分（与 TS 真产物逐字节对照）" "$PY" tools/verify_judge_diff.py "$TS_DIST"
else
    printf '\n\033[1m── judge 差分\033[0m\n'
    printf '  \033[33m⏭  跳过：%s\n' "缺 node 或缺 TS 产物 $TS_DIST"
    printf '     这不是通过 —— 只是本地没有参照物；发布前应在有该产物的机器上跑一次\033[0m\n'
fi

run "回归测试（pytest，禁网 + 禁生产库 + 清心流缓存）" "$PY" -m pytest

# 变异测试不是"再跑一遍测试"，是**证明测试会红**。绿色的套件本身不是证据：
# 断言写松了、分支没跑到，它一样全绿 —— F5 就是这么骗过 AST 等价门与差分门的。
# 它比上面那项慢（每条变异起一次 pytest），所以只覆盖**已定型**的模块。
run "变异测试·预算（18 条变异必须全被杀死）" "$PY" tools/mutation_check.py budget

run "变异测试·去重（22 条变异必须全被杀死）" "$PY" tools/mutation_check.py dedupe

run "变异测试·标签（13 条变异必须全被杀死）" "$PY" tools/mutation_check.py label

run "变异测试·分层（28 条变异必须全被杀死）" "$PY" tools/mutation_check.py layer

run "变异测试·观测（44 条变异必须全被杀死）" "$PY" tools/mutation_check.py observe
run "变异测试·画像（70 条变异必须全被杀死）" "$PY" tools/mutation_check.py profile
run "变异测试·学习（103 条变异必须全被杀死）" "$PY" tools/mutation_check.py learn

run "变异测试·心流闸门（116 条变异必须全被杀死）" "$PY" tools/mutation_check.py heartflow

# 心流运维命令面（``/heartflow status|threshold|group|profile|why|layers|veto|report``）
# 单列一套：这一面的失效形态是"**回答得头头是道、内容却是错的**" ——
# 09-26 停摆 3 天时状态页照样报 0.60（judge 按生效值判），照样全绿。
# ⚠️ 这一套的判据在 W6（2026-10-02）随实现一起改了：原先命令写的是**内存覆盖**，
# 所以有一条变异钉的是"回复里必须说明重启会回退"；现在写的是账号文件，那句话反了过来
# （「已写入账号配置，当次生效、重启不回退」），继续钉旧文案会把**已经正确的实现**
# 判成红 —— 于是那条变异改成钉新文案。同理 `group del` 那条变异原先的判据集看不见问题
# （删的群本来就在群聊白名单里 ⇒ "该补哪些"是空集），已换成删一个**不在**里面的群。
run "变异测试·心流运维命令面（28 条变异必须全被杀死）" "$PY" tools/mutation_check.py hf-commands

# 适配器上的四处接缝（收信旁路 / 发送前置 / 出站记录 / 交付结算）单列一套：
# 它们的"正确"只能靠**调用点源码形状**验（``plugin.platform.adapter`` 在本机 import 不了，
# 模块级 `from gateway.config import Platform` 是 Hermes 独有）—— 所以这一套是本地唯一的防线。
run "变异测试·适配器接缝（14 条变异必须全被杀死）" "$PY" tools/mutation_check.py adapter-seams

# 准入策略上报（Hermes 授权面）单列两套：纯逻辑 + 适配器接线。
# 失效形态是 **2026-10-01 真机实测的那一条**：插件判过了、网关默认全拒把回复拦下，
# 而插件侧日志全绿（`触发（…）` 照打、台账照落 `status='judged'`），
# 只有网关那行 `Unauthorized user: <wxid>` —— 所以"上报被拆掉"必须当场红。
run "变异测试·准入策略·逻辑（9 条变异必须全被杀死）" "$PY" tools/mutation_check.py access-policy
run "变异测试·准入策略·适配器接线（7 条变异必须全被杀死）" \
    "$PY" tools/mutation_check.py adapter-access-policy

# 发送结果解析（Code / BaseResponse.ret / **逐条 List[].Ret** / 取 id）。
# 本树此前**一条判据都没有**，而蓝本两条都是真实故障复盘换来的（v1.5.6 SEND-LIST-RET
# 「Code=0+ret=0 但 List[0].Ret=-2 ⇒ 拒收」、v1.3.57 P1-2「id 只在 Data.List[0]」）。
# 失效形态全是最贵的"全绿但不对"：拒收当成功 ⇒ 幽灵出站记录 + 台账写 sent + 白消费预算；
# 取不到 id ⇒ bot_msg_id 恒 NULL 而一句日志都没有。
run "变异测试·发送结果解析（8 条变异必须全被杀死）" \
    "$PY" tools/mutation_check.py outbound-send-result
run "变异测试·出站 id 落库接线（5 条变异必须全被杀死）" \
    "$PY" tools/mutation_check.py adapter-outbound-ids

# 账号启动预热（learned 阈值 / 预算种子 / 分层统计）+ 缓存登记表："没接线"与"没登记"
# 是这一块仅有的两种失效形态，而它们在生产上都完全看不出来（插件照跑、只是头 5 分钟
# 判断变了个样 / 测试之间互相污染），必须靠门钉住。
run "变异测试·运行时（12 条：预热接线 / 只写不读的配置单例 / 缓存登记表）" \
    "$PY" tools/mutation_check.py runtime

# 不变量（I1–I11）单列一门：上面那几套验的是"**这段**逻辑对不对"，
# 这一套验的是"**跨模块**的那条线有没有被谁剪断" —— 阈值不进 prompt、占位符不占预算、
# 空正文必抛、最后发言只认 MAX(sent_at) 之类，每条都对应一次真实事故。
# 它们天然要跨文件读源码（AST），所以混在普通回归里容易被当成"重"而删掉，钉在门禁里。
run "不变量守卫 I1–I11（每条都对应一次真实事故）" \
    "$PY" -m pytest plugin/tests/invariants -q

# 下面六套是**给不变量守卫自己做变异**：断言写松了（例如"缓存行数为 0"而对方塞了个空桶）
# 它会假装全绿 —— 只有变异能证明它会红。
run "变异·不变量/心流（3 条：阈值进 prompt / 上下文抠图 / 尺度不除 10）" \
    "$PY" tools/mutation_check.py inv-heartflow
run "变异·不变量/学习（3 条：pending 走 sent / 阈值去上界 / 真发不记历史）" \
    "$PY" tools/mutation_check.py inv-learn
run "变异·不变量/分层（2 条：顺手写缓存 / 旧标签当弱信号）" \
    "$PY" tools/mutation_check.py inv-layer
run "变异·不变量/仓（1 条：最后发言改用 status='sent'）" \
    "$PY" tools/mutation_check.py inv-repo
run "变异·不变量/画像（2 条：band 去上界 / 禁言令少一条）" \
    "$PY" tools/mutation_check.py inv-profile
run "变异·不变量/judge（1 条：空正文静默返回空串）" \
    "$PY" tools/mutation_check.py inv-judge

# 留痕 / 可观测（09-11 静默瘫 3 天的真正根因是留痕缺失，不是判定错）：
# 学习样本混进 suppressed 行、台账计数丢掉沉默原因、judge 判了没过与"投递结果没回来"混成一个词。
run "变异·留痕（5 条：样本混入 suppressed / 计数丢原因 / 半台账）" \
    "$PY" tools/mutation_check.py trace-db trace-learn

# 入站判定（接龙/红包/闸门顺序）：本仓**最贵的一类失效**是"静默吞消息" ——
# 不报错、日志看着完全合理（只留一行 url=missing），09-27 那晚整晚的接龙就是这么没的。
# 门禁里单列，是因为它跨 hongbao/relay/triggers 三个文件，混进别的套件就被稀释了。
run "变异·入站判定（23 条：接龙被吞 / 裸关键词 / 静默面放宽 / 闸门顺序 / reason 三分类 / @必回·非@心流）" \
    "$PY" tools/mutation_check.py hongbao relay triggers

# 入站**解析**（字段名兜底）：与上面那条是两回事 —— 上面判"该不该回"，这里判"这条消息
# 到底长什么样"。失效形态更隐蔽：字段名对不上不报错、日志全绿，只是**当成别的东西** ——
# v1 的 type 不认 ⇒ 图片/语音/文件全判成文本；群 id 认不出 ⇒ 群消息当私聊；
# 无 id 的消息共用空去重键 ⇒ 第一条之后**全静默丢弃**（A3，与 09-27 吞接龙同一类症状）。
run "变异·入站解析·v1/旧格式字段兜底（16 条：type 不认 / 群当私聊 / 空 id 合并 / 不解包 / envelope）" \
    "$PY" tools/mutation_check.py parser-v1

# @ 提及层（A7，2026-10-02）：这一层是"群里到底叫没叫 bot"的**唯一判据** —— 判真就是
# **100% 必回**（绕开心流阈值）。两个方向判错都是事故：漏判 ⇒ bot 在群里哑掉且**无任何
# 日志**；误判 ⇒ 抢话（老板明令"不要固定触发关键词"）。整层此前零测试，现在连
# 三条 4096 截断（防 ReDoS）与"昵称必须带 @"这次行为变更一起钉住。
run "变异·@ 提及层（15 条：六个 pattern / 微信号形态 / 昵称带 @ / 两条截断 / 去提及接线）" \
    "$PY" tools/mutation_check.py mentions

# 入站**落库**（W1，2026-10-02）：本树此前只记出站，入站一条不落 —— 后果不是"少一张
# 审计表"，而是心流 judge 的上下文只剩 bot 自己说的话（给裁判看伪造的现场记录）、
# 按群查历史查出空表、黑话挖掘没有输入。这一类失效的形态是**写不进去但链路照跑**
# （异常被吞成一行 warn），所以判据落在"写下去的那 13 列"上，并单列一档
# （另一半在 adapter 的接线：落库的位置 + 出站那两个真 bug 的收口）。
run "入站落库（13 列映射 / ENUM 合法值 / 永不抛 / ④z 接线顺序）" \
    "$PY" -m pytest plugin/tests/test_inbound_persist.py -q
run "变异·入站落库（8 条：peer_kind/dm · chat_id 分岔 · direction · 假 new_msg_id · 不解包 / 失败不再吞）" \
    "$PY" tools/mutation_check.py inbound-persist
run "变异·落库接线（3 条：整条拆掉 / 出站 peer_kind 绕过映射 / chat_id 退回编码串）" \
    "$PY" tools/mutation_check.py adapter-inbound-persist

# 账号写回（W6，2026-10-02）：命令面此前写的是**内存覆盖**，而 ``Account`` 是 frozen 的、
# ``connect()`` 只解析一次并缓存 ⇒ 六条命令（/user /group /heartflow /affection /jargon
# 以及 W2 的 /affection）全是「回了 ✅、当次不生效、重启就丢」。改成写回账号文件之后，
# 失效形态换了一批，而且**每一种都回 ✅**：凭证键被写坏（收信立刻停）、读不出来照写
# （半份配置覆盖掉能救回来的那份）、从 dataclass 反序列化（本树不建模的键 + 凭证静默全删）、
# 不清同键覆盖（文件改了链路读到旧值）、不备份/备份半途失败（改了没得回退）、权限退回 644
# （文件里有 authcode）、写失败仍回 ✅、写回后不重载（当次不生效却宣称生效）。
# ⇒ 判据全下在"盘上的字节"与"resolve() 的生效值"上，不测"调用了 write"。
run "账号写回（只改一个键 / 凭证逐字节不动 / 读不出来就不写 / 600 / 清影子覆盖）" \
    "$PY" -m pytest plugin/tests/test_account_store.py -q
run "变异·账号写回（8 条：凭证键不拦 · 读不出来照写 · 从零拼一份 · 不清覆盖 · 不备份 · 644 · 路径穿越 · 备份半途失败）" \
    "$PY" tools/mutation_check.py account-writeback
run "变异·写回命令面（3 条：写失败回成功 · 未知异常原文外发 · 写回后不重载）" \
    "$PY" tools/mutation_check.py account-writeback-cmd
run "变异·写回接线（3 条：入站不取最新 Account · 重载不刷管线 cfg · 命令调用点不传 adapter）" \
    "$PY" tools/mutation_check.py account-writeback-adapter

# 好感度（W2，2026-10-02）：移植 ``affection.ts`` 624 行 + 按老板拍的**落库持久化**
# （TS 放在进程内 Map，重启清零；本树多记一个 ``wpp_affection`` 表）。这一面的失效形态
# **全都不报错**：多扣一分、骂不动的 bot、每次闲聊都写一次库、开关关着照查库、
# 状态页报一个看着合理的错数 —— 累计一个月的数据才看得出来。
# 所以三类判据分开：**算法**（纯函数逐条对齐）· **落库**（SQL 形状：哨兵行/负分/过期写回）
# · **接线**（``adapter.py`` 本机 import 不了 ⇒ AST 形状：旁路不许 await、句柄+回调、
#   情绪走 channel_prompt、守卫必须在 I/O 之前）。
run "好感度（17 规则 / 10 情绪 / 截断取整 / 落库 SQL 形状 / 编排降级）" \
    "$PY" -m pytest plugin/tests/test_affection.py -q
run "好感度接线 + 状态页（不 await / 句柄与回调 / channel_prompt / 计数只属 /affection）" \
    "$PY" -m pytest plugin/tests/platform/test_affection_wiring.py -q
run "变异·好感度算法（9 条：fail-open 两层 · THREAT 次序 · floor · 微效果闸 · 大小写 · 幻觉类型 · 重分配 · 上限挡负向）" \
    "$PY" tools/mutation_check.py affection-core
run "变异·好感度落库（6 条：哨兵行 · 负分总量 · 计数按群 · 过期不写回 · 过期失效 · 裸 INSERT）" \
    "$PY" tools/mutation_check.py affection-repo
run "变异·好感度接线（8 条：await · 挪出群分支 · 不存句柄 · 不挂回调 · 不回收 · 丢 channel_prompt · 系统通知 · 守卫在 I/O 之后）" \
    "$PY" tools/mutation_check.py affection-adapter
run "变异·好感度状态页（4 条：每次都数 · 读失败抛 · 两个数对调 · 0 行只回 0）" \
    "$PY" tools/mutation_check.py affection-status

# 群聊意图 / 上下文（W3，2026-10-02）：移植 intent-llm.ts + intent-embed.ts + dispatcher
# 里"选哪些消息进上下文"那段编排。这一面的失效形态同样**不报错**，只是回复质量悄悄变差：
# 上下文里混进"用户刚 @ 你的那一句"、media 意图兜底不生效、topic 意图被塞进一堆无关文本、
# 图片不裁导致正文被挤掉、阈值差一个等号丢候选、开关关着照样查库/外呼。
# 三类判据分开：**算法**（分类/归一化/解析/摘要）· **选择与降级矩阵**（含 embedding 快路径）
# · **接线**（adapter 本机 import 不了 ⇒ AST 形状：总闸守卫在 I/O 之前且真拦住、
#   候选池排除触发消息自身、失败只 warn 不收信链路抛、注入走 channel_context）。
run "群聊意图（规则分类 / 归一化 / 候选压缩 / LLM 响应解析 / prompt 字节 / 配置 resolver）" \
    "$PY" -m pytest plugin/tests/test_intent.py -q
run "群聊意图接线（守卫顺序与真拦 / before_sec 排除自身 / 失败只 warn / channel_context）" \
    "$PY" -m pytest plugin/tests/platform/test_intent_wiring.py -q
run "变异·意图核心（10 条：空 @ · 媒体词 · 转写正则 · 语音前缀 · msgId ?? · inject 类型 · 非串 id · json 围栏 · bot 名 · 50 字上限）" \
    "$PY" tools/mutation_check.py intent-core
run "变异·意图选择（12 条：topic 兜底 · 不分类 · inject 过滤 · new_msg_id · media_only · 图片裁剪 · 媒体保底 · 名额 · 阈值边界 · 空候选）" \
    "$PY" tools/mutation_check.py intent-select
run "变异·意图配置（9 条：布尔窗口 · 上界 · 默认值 · 取整 · 超时脏值 · 模型 strip · bot 名 · 隐式 env 兜底 · topN）" \
    "$PY" tools/mutation_check.py intent-cfg
run "变异·意图接线（14 条：守卫变装饰 · before_sec 丢 · 窗口硬编码 · 群 id · None 抹平 · 不接 channel_context · 失败上抛 · 开关变装饰 · 第二个解析家）" \
    "$PY" tools/mutation_check.py intent-adapter
run "变异·群消息查询（6 条：严格小于变 <= · 0 当过滤 · 过滤失效 · 参数错位 · 群 id 参数 · chat_id 兼容分支）" \
    "$PY" tools/mutation_check.py intent-repo

# WS 增量兜底 + 心跳（W5，2026-10-02）：移植 ws-client.ts 的 ``triggerSync`` 与两处心跳。
# 这一面的失效形态**一封告警都不出**：兜底坏了只表现为"某些消息没人回"，群里无法归因；
# 而反方向更糟 —— **首次把厂商历史倒回来派发**，新进程的内存去重是空的 ⇒ 机器人去
# 回复旧消息（TS 注释里 19:56 那次"1458 条重放"就是这个形状）。
# 所以判据一律下在"盘上真发生了什么、按什么顺序"（游标在派发**之后**才动、首次只落基线），
# 不是"调用了 trigger"。
run "增量兜底（首次只建游标 / 先派发后游标 / 单飞锁 / 失败只 warn / 心跳看连接态）" \
    "$PY" -m pytest plugin/tests/test_sync_fallback.py -q
run "增量兜底接线（WS 事件钩子 / 适配器装配 / 三个新端点走只读闸）" \
    "$PY" -m pytest plugin/tests/platform/test_sync_wiring.py -q
run "变异·兜底核心（15 条：首次回放 · 不落基线 · 游标先行 · 空 KeyBuf 回写 · 锁变装饰 · 失败上抛 · 心跳瞎发 · 开连不重开自动心跳）" \
    "$PY" tools/mutation_check.py sync-core
run "变异·WS 客户端（11 条：reason 串错 · 心跳帧不 return · 钩子被 await · 钩子异常穿透 · connected 不清 · 原生 ping 关掉）" \
    "$PY" tools/mutation_check.py sync-ws
run "变异·兜底接线（11 条：不传 on_event · 不透传 reason · 连接态变捕获 bool · 不 start · 不登记 · 重复起两个 · WS 失败就 return）" \
    "$PY" tools/mutation_check.py sync-adapter
run "变异·兜底端点（6 条：Synckey 拼错 · 丢 Scene · 空串缺省 · GET · 两个心跳打错端点）" \
    "$PY" tools/mutation_check.py sync-api

# 小程序卡片（W7-2，2026-10-02）：移植 ``inbound/app-card.ts`` 267 行，并把它接进
# ``_media_tag`` 对 49 的细分。失效形态与入站其余各类**同族** —— 不报错、日志干净，
# 只是模型看到的东西少了或错了：
#   ① 卡片掉进最后那道 ``[文件]`` 兜底（模型答"你发了个文件"）—— W7-2 修的就是这个
#      （v1.6.1 那次"转发只带了文字"的归因错误也是同一类误判）；
#   ② 追加块没接上、或接上了键名对不上（``mp_asset_url`` ↔ ``mpAssetUrl``）⇒ 正文一个字不多；
#   ③ 分支排到 ``if not msg.media`` 那道闸**之后** ⇒ 整类卡片走不到它（卡片的资产挂在
#      ``app.cover_image`` 上、不在 ``msg.media`` 里），现场表现只有"卡片没图"；
#   ④ 富化把异常冒泡出去 ⇒ 一次 OSS 抖动从"丢一张图"变成"丢一条消息"。
# ⇒ 判据分三层：**纯逻辑**（识别/字段映射/文本化/下载降级/49 标签）· **卡片装配**
#   （``adapter.py`` 本机 import 不了 ⇒ AST 形状：分支顺序、跨模块键名、写侧条件、元组形状）。
run "小程序卡片（识别 / 字段映射 / 文本化 / 49 不再掉 [文件] / 封面对标 / 降级 / 永不带 xcxthumb）" \
    "$PY" -m pytest plugin/tests/test_app_card.py -q
run "小程序卡片装配（分支在 media 闸之前 / 跨模块键名 / 写侧条件逐字 / 降级只出声 / 元组形状）" \
    "$PY" -m pytest plugin/tests/platform/test_app_card_wiring.py -q
run "变异·卡片逻辑（15 条：类别放宽 · {} 真值分叉 · bool 当数字 · 封面来源 · 标题两遍 · 图标混标 · 白跑外呼 · 异常冒泡）" \
    "$PY" tools/mutation_check.py app-card
run "变异·卡片标签（4 条：掉回 [文件] · 不追加 · 块前置 · 空块也追加）" \
    "$PY" tools/mutation_check.py app-card-tag
run "变异·卡片装配（10 条：排到闸后 · 少类型闸 · 键名两类 · 写侧条件两种 · 降级不吭声 · 无封面也打 · 元组形状 · 不调富化）" \
    "$PY" tools/mutation_check.py app-card-adapter

# 媒体富化（W7-1，2026-10-02）：把 OSS 直链写回 ``msg.content``。上传那半
# （``inbound/media_oss.py``）在 W7-1 前半段就做完了，可**没有一个人调它** ——
# ``_fetch_media`` 只把二进制下到 Hermes 本地缓存，而本地路径进不了任何下游：
# 心流抠图（``heartflow.extract_hf_image_urls``）只认 ``^https?://``、judge 的
# ``filter_image_urls`` 只认 ``^https?://|data:image/`` ⇒ v1.11.0 的"看图"在 py2 是
# **一整段空转**（正文那行 ``[图片]`` 是 ``_media_tag`` 打的**前缀**、后面永远没有 URL），
# 而现场表现只是"bot 看不懂图"，没有一行日志是红的。
# ⇒ 本项的判据全部下在**正文的字节**上，且失效形态每一种都"看着完全正常"：
#   ① 取媒体的位置太靠下（退回 ⑤ ``_build_event``）⇒ ④z（落库）与 ④a（心流缓冲）都排在
#      它之前，judge 抠的又是 ``content`` ⇒ URL 永远到不了 judge 与 DB；
#   ② 四个分支的 ``kind`` 传串了 ⇒ OSS 目录与正文标记双双错位，而**上传本身成功**、日志干净；
#   ③ 降级没有提前 return ⇒ 拿不到 URL 也注入 ``[图片] None``，正文被污染，且"有凭据/没凭据"
#      两种环境行为分叉（``.183`` 当时就是没凭据那条路 —— 老板二次确认「先不放」。
#      ⚠️ 2026-10-03 更新：老板改判「放 OSS 凭据」⇒ ``.183`` 上那三样已**手工**落位
#      （ossutil + ``~/.openclaw/credentials/oss-credentials.json`` + ``~/.ossutilconfig``），
#      这条路现在是"真上传"；**代码面一字未变**，降级判据照旧是门）；
#   ④ 注入写成前缀 ⇒ 与 ``_media_tag`` 那套混血，正文里两个 ``[图片]``。
# ⇒ 判据分三层：**纯逻辑**（四个串逐字对 TS · 注入的字节 · 降级逐字节不变 ·
#   ``_media_tag`` 让路）· **装配**（``adapter.py`` 本机 import 不了 ⇒ AST 形状：
#   ④c 的行号顺序、分支与 kind 一一对应、``if not url: return`` 在注入之前、f-string 组成）
#   · **变异**（三套 24 条，含"退回 ⑤"与"四个目录各传串一次"）。
run "媒体富化（四个串逐字对 TS / 追加不前置 / 幂等 / 降级逐字节不变 / _media_tag 让路）" \
    "$PY" -m pytest plugin/tests/test_media_enrich.py -q
run "媒体富化装配（④c 在落库与心流之前 / 四分支 kind 一一对应 / if not url 在前 / f-string 组成 / 跨模块键名）" \
    "$PY" -m pytest plugin/tests/platform/test_media_enrich_wiring.py -q
run "变异·媒体富化（11 条：四个串形状 · 文件分支判宽 · 丢 bytes · 前缀注入 · 幂等闸拆掉 · 键写串 · 降级闸拆掉 · 上传不带账号 · 大小写死 0 · 标记表缺目录）" \
    "$PY" tools/mutation_check.py media-enrich
run "变异·媒体标记让路（4 条：抑制拆掉 · 读错键 · 判据写反 · 只管 49）" \
    "$PY" tools/mutation_check.py media-enrich-tag
run "变异·媒体富化装配（9 条：图片分支不富化 · 四分支各传串一次 · 文件名分家 · 退回 ⑤ · 不交给 _build_event · 解包写死空表）" \
    "$PY" tools/mutation_check.py media-enrich-adapter
# OSS 上传器本体（R2 补的门，2026-10-03）：``media_oss.py`` 此前**没有任何变异套件**
# （上面三套打的是 ``adapter.py`` / ``parser.py``）⇒ 它「失败必须安静降级」的四条不变量
# 只有正面判据、没有反面判据。R2 把凭据落到 ``.183`` 之后上传**真会跑**，这四条才第一次
# 有成本，所以同一轮把反面判据补齐。
# ★ 这一套里最要紧的一条是**密钥不许上命令行**：``.183`` 实测发现本模块把 AK/SK 读进来
# 却一个字节都不往下传（与 TS 同款）⇒ 真机上能上传成功全靠 ``~/.ossutilconfig``；
# 于是"顺手加个 ``-i`` 让它自洽"是最自然的走法，而那会把凭据发到 ``ps`` 可见的地方。
run "OSS 上传器（凭据读取/半份作废/缓存当次生效/临时文件必删/3 次退避/脱敏/密钥不进命令行/降级只 warn）" \
    "$PY" -m pytest plugin/tests/test_media_oss.py -q
run "变异·OSS 上传器（7 条：密钥上命令行 · 脱敏拆掉 · 降级变抛 · 空字节也传 · 非 ASCII 进 key · 缓存永不命中 · endpoint 不回默认值）" \
    "$PY" tools/mutation_check.py media-oss

# 接龙解析（W7-3，2026-10-02）：``relay.ts`` 的后一半。py2 此前只有 ``is_relay_message``
# （识别），拿到"是接龙"之后给正文打个 ``[接龙]`` 前缀就交给模型 —— 而 vendor 推来的正文
# 是**一整坨**（title + 规则 + 条目全挤在一行），模型得自己从里面数人。
# ⇒ 本项补的是**整条改写**（``[接龙] title\n1. …``，TS ``handler.ts:547-549``）。
# 失效形态同样"看着完全正常"：
#   ① 字面 ``\n`` 不换回来（v1 的真 bug）⇒ title 塌成整坨、条目正文粘着两个字面字符；
#   ② 单行无换行那条路拆掉 ⇒ vendor 真格式一条都切不出来（老板 2026-08-14 亲报的那条）；
#   ③ 改写排在 ④z/④a 之前或 ④e/⑤ 之后 ⇒ DB 与心流池里的原文变了、或 judge/LLM 压根
#      看不到条目（三种排错法都不报错，只是"库里的接龙长得不一样"）；
#   ④ 抑制键与写入键差一个字母（``relay_render`` ↔ ``relay_rendered``）⇒ 正文两个 ``[接龙]``，
#      而两边的单测各自都绿。
# ⇒ 判据分三层：**纯逻辑**（切分/编号/title 兜底/渲染字节/永不抛）· **装配**
#   （``adapter.py`` 本机 import 不了 ⇒ AST 形状：④d 的行号顺序、传的是消息对象、
#   跨模块键名、两道抑制在 49 细分之前且返回空串）· **变异**（三套 23 条）。
run "接龙解析（单行/多行切分 · 序号从 1. 起 · title 兜底 · recorditem · 渲染逐字对 TS · 永不抛）" \
    "$PY" -m pytest plugin/tests/test_relay.py -q
run "接龙装配（④d 在落库/心流之后、judge/事件之前 · 传消息对象 · 跨模块键名 · 抑制在 49 细分前）" \
    "$PY" -m pytest plugin/tests/platform/test_relay_wiring.py -q
run "变异·接龙解析（14 条：字面 \\n · BOM · title 兜底 · 单行路 · 起点 1. · 续行空格 · 前导描述 · recorditem 两处 · 缺字段 None · 渲染尾换行与序号 · 非接龙改写 · except 收窄）" \
    "$PY" tools/mutation_check.py relay-parse
run "变异·接龙标签（5 条：抑制拆掉 · 读错键 · 判据写反 · 只管 49 · 返回 None）" \
    "$PY" tools/mutation_check.py relay-tag
run "变异·接龙装配（4 条：接线成摆设 · 传正文 · 跑两遍 · 从别的模块导入同名函数）" \
    "$PY" tools/mutation_check.py relay-adapter

# 群黑话（W7-4，2026-10-02）：``jargon.ts`` 的后一半（统计层 py2 早就有、**LLM 那半没有**）。
# 三层流水线里**每一层的失效形态都是"静默少做一点"**：
#   ① 统计层：分词少了/裁剪漏了副表 ⇒ 候选池变小 ⇒ 现象只是"黑话学得慢"；
#   ② 批量验证与三步推断：该走兜底时没走（或反过来）⇒ 现象是"LLM 确认了却没存""含义是空"；
#   ③ store 契约：``list_jargon`` 给的是 DB 行 ``{term,…}``，而契约读的是 ``content``
#      ⇒ 排除集**恒空**、已确认的词每轮重新送 LLM 并重新 ``upsert``
#      （那条 SQL 是 ``frequency = frequency + 1`` ⇒ 词频被反复灌水）。全链路无异常、无日志，
#      而且**在 TS 侧永远暴露不出来**（它的 ``cfg.store`` 从未被注入 ⇒ 落库那半是纯花钱的空转）。
# ⚠️ 两条如实记下的现实（都没有变异体，理由写在用例 docstring 里）：
#   · ``shouldFilterCandidate`` 含中文即拉黑 ⇒ **中文黑话到不了 LLM**（TS 原样，不放宽）；
#     连带 ``COMMON_BLOCK`` 那条分支不可达。编排层有一条用例钉住这个后果。
#   · ``tokenize`` 的纯数字/纯标点两道过滤不可达（上游只吐汉字段与 ASCII 字母段）。
run "群黑话（分词 n-gram · 打分 IDF/burst/集中度 · 硬过滤 · 含义缓存 · 配置 fail-closed · 节流记账 · 回包解析 · 五个 prompt · 三步降级 · 编排与 store）" \
    "$PY" -m pytest plugin/tests/test_jargon.py -q
run "黑话装配（④b' 在 ④a/④b 之间且在 ④z/④d 之后 · 两个循环 · fire-and-forget 与强引用 · 配置只有一个家 · store 按账号）" \
    "$PY" -m pytest plugin/tests/platform/test_jargon_wiring.py -q
run "变异·黑话统计层（12 条：Math.round · 词频闸 · 首见时间 · 样例上限 · 输出截断 · 中文拉黑 · 长度界 · 裁剪副表 · 集中度 · burst 上限 · 守卫 · 排除集）" \
    "$PY" tools/mutation_check.py jargon-core
run "变异·黑话编排（10 条：enabled 默认 · bool 当 int · 两个上限 · 降级 · is_similar 严格判 · 非 dict · no_info · 排除闸 · 落库闸）" \
    "$PY" tools/mutation_check.py jargon-mine
run "变异·黑话 store 契约（3 条：★列名映射 term→content · conn 透传 · 位置参数对调）" \
    "$PY" tools/mutation_check.py jargon-store
run "变异·黑话装配（13 条：旁路摆设 · 排到好感度之后 · store 写死 default · 不带 store · 群 id · 配置来源 · 系统通知 · 强引用 · done 回调 · create_task · async 形状 · 日志口径）" \
    "$PY" tools/mutation_check.py jargon-adapter

# 运行期门禁与两条命令（W7-5，2026-10-02）：``/blacklist`` 与 ``/xiaowei``。
# 这一项的形状是「**开关在、门不在**」—— 命令能改键、而没有任何下游消费者：
#   ① 门禁：读不到配置时 fail-open ⇒ 最该拦的那一类全过；账号路由写死默认账号 ⇒
#      B 号的开关跟着 A 号走；判定改成"键在不在"⇒ 清掉默认值就放行；
#   ② seam：门禁工厂的结果被丢掉（注册期装了门、调用期一次都不判）、不受管的工具也包一层、
#      签名放宽成 ``**kw``（dispatch 上下文键会被放行并**原样发给厂商**）、设 ``__wrapped__``
#      （``inspect.signature`` 穿透它 ⇒ 窄签名那道过滤失效）；
#   ③ 命令：写回传**增量**而不是整份清单（``/user add x`` 清空现有白名单**而回话是 ✅**）、
#      开关写回拍扁同级键（``/affection off`` 把 ``maxUserAffection`` 一起抹掉）、
#      ``/xiaowei`` 写成 dict（非空 dict 恒真 ⇒ ``off`` 完全无效）、黑名单落错键名；
#   ④ 状态面：账号文件读不出来时报「❌ 关闭」而不是「读不到」（老板会照着去打开它）。
# 判据一律下**在行为上**（门到底拦没拦、盘上的字节到底变没变）：这条链上"回话"恰恰是最会骗人的。
run "工具门禁（开关三态 · 按账号路由 · fail-closed · 副本/双认领/只读 · 端到端真拦真放 · 命令与门同一个开关）" \
    "$PY" -m pytest plugin/tests/test_tool_gate.py -q
run "门禁装配（唯一 seam + 惰性 getter · 形状三约束 · 只有受管工具多一层 · 全树无 get_account · 四处守卫 · 写回传整份清单）" \
    "$PY" -m pytest plugin/tests/platform/test_tool_gate_wiring.py -q
run "变异·门禁策略层（12 条：fail-open · 交出活 dict · 路由写死默认账号 · 不受管也包 · 丢掉 reason · 空回话 · 双认领不报错 · 表可写 · 键名蛇形 · in 判据 · 前缀漏项 · 幽灵条目）" \
    "$PY" tools/mutation_check.py tool-gate
run "变异·门禁 seam（6 条：pre=None · 不受管也包一层 · 设 __wrapped__ · 签名放宽 **kw · 判据装了不判 · __original_handler__ 指向包装层）" \
    "$PY" tools/mutation_check.py tool-gate-seam
run "变异·门禁注册（2 条：注册期不装门 · getter 传函数对象而非 lambda）" \
    "$PY" tools/mutation_check.py tool-gate-registration
run "变异·命令写回语义（9 条：清单传增量 · del 传点名项 · 拍扁同级键 · xiaowei 写 dict · 黑名单键名 · 状态二值化 · 改回 _safe_cfg · 空底稿继续写）" \
    "$PY" tools/mutation_check.py tool-gate-cmd
run "变异·朋友圈守卫（5 条：三处白名单拆成摆设 · 文字/视频 fail-open）" \
    "$PY" tools/mutation_check.py tool-gate-msg

# 入站到达层聚合（W7-6，2026-10-02）：同 key 消息在 ``debounce_ms`` 窗口内并成一批。
# **保守移植** —— flush 之后仍**逐条**走现有 ``_handle_one``，处理语义一字未动
# （TS 那种批式处理管线**不在本项范围**，见 ``plugin/inbound/debounce.py`` 的模块 docstring）。
# 这一层的坏法**每一种都是安静的**：缓冲器一旦成了消息的终点，现场只有"有些人说话机器人
# 永远不理"—— 没有异常、没有告警、日志一行不出（入站路径本来"没触发就不出声"）。
#   ① 丢：拿不到事件循环/已停机却返 True（消息躺进一个再也不会 flush 的缓冲）、
#      直通消息把同 key 已缓冲的那批从缓冲里取走了却不带出去、停机不 flush（或 flush 但丢内容）；
#   ② 序：同 key 两批并发跑 ⇒ 派发顺序 ≠ 到达顺序（``/affection on`` 紧跟 ``off`` 就有语义）；
#   ③ 窗口：不取消旧定时器（旧窗口到点把**新**缓冲整批发出去）、毫秒→秒漏 ``/1000``、
#      只在第一条上挂定时器；④ 接线：窗口写死成 500（``debounceMs`` 又变回死键）、
#      账号 id 晚绑定（多账号下全按最后一个账号派发 —— 单账号测试全绿）、
#      直通兜底成摆设、``for msg in batch`` 改成只看最后一条（偷换成批式语义）。
run "入站聚合（聚合键 · 窗口重置 · 三类直通 + 控制面钩子 · 同 key 保序 · 一条都不许丢 · 停机 flush · 失败只 warn · 观测面）" \
    "$PY" -m pytest plugin/tests/test_debounce.py -q
run "聚合装配（按账号建在同处 · 窗口取账号文件 · 闭包钉住账号 id · 控制面钩子 · 直通兜底的守卫与真派发 · 逐条派发 · 停机顺序 · 热加载就地改 · 单一真源）" \
    "$PY" -m pytest plugin/tests/platform/test_debounce_wiring.py -q
run "变异·聚合器本体（18 条：窗口只在第一条挂 · 毫秒换算 · 直通不带走缓冲 · 保序断链 · _take 不取消定时器 · 裸 await 吞取消 · 钩子摆设 · 类型判反 · 键少字段 · 键改 direct · 停机不等链 · stop 不 flush · 停机仍收 · 无循环仍收 · _fire 不走 _take · 不叫 on_error · 失败上抛 · 负数不夹）" \
    "$PY" tools/mutation_check.py debounce-core
run "变异·聚合装配（13 条：直通兜底成摆设 · 守卫恒不成立 · 窗口写死 · 账号 id 晚绑定 · 钩子不接 · 不按账号登记 · 只看最后一条 · 无声丢弃 · 单条不保护 · 停机不 flush · flush 丢内容 · 热加载写死 · 搬进第二份真源）" \
    "$PY" tools/mutation_check.py debounce-adapter

# 群聊 @被回复人（W-Q，2026-10-03）：老板报的「Hermes 回复没有像 Openclaw 那样 @ 被回复的人」。
# 代码一直在（``_send_chunk`` 的引用分支会算 ``"@昵称 "`` 前缀），**死的是它唯一的输入**：
# ``fromusr`` 全树只有读、没有写 ⇒ ``_quote_prefix`` 第一句就 ``return ""`` ⇒ 前缀从没出现过。
# 这一条链的坏法**每一种都安静**：算错人 = 群里 @ 了别人；算不到 = 前缀整条消失（看起来
# 就只是"没 @ 而已"）；拿群 id 当人 = @ 了一个群。三种都**不抛异常、不写日志**。
#   W-Q1 数据层：按锚点 id 回查被引用消息（**不限方向** —— 被引用的可能是 bot 自己发的）。
#   W-Q2 纯逻辑：身份链四档 + 群 id 那道闸 + ``@`` 前缀形状（与 TS ``buildGroupMentionPrefix`` 逐字对齐）。
#   W-Q3 装配：真机的昵称只在厂商 ``/Friend/GetContractDetail`` 的回包里（``Data.ContactList[0].NickName.string``），
#              旧写法读的是 ``Data.NickName`` ⇒ 恒取不到。
run "引用身份（群 id 不是人 · 身份链四档 · 正文前缀兜底 · 前缀形状与 TS 对齐 · 永不抛）" \
    "$PY" -m pytest plugin/tests/test_quote_ref.py -q
run "引用回查（两个 id 的 OR 括号 · 不限方向 · 空 id 不发查询 · 决胜键 · raw_payload 那一列）" \
    "$PY" -m pytest plugin/tests/test_db.py -q
run "引用装配（回查先于查名字 · fromusr 的来源优先级 · 前缀并进 XML · 记账记裸正文 · 昵称四级链 · 厂商回包形状 · 缓存 · 全树只有一处拼 ``@``）" \
    "$PY" -m pytest plugin/tests/platform/test_quote_identity_wiring.py -q
run "变异·引用身份（9 条：群 id 那道闸 · 身份链两档不过闸 · matched 恒假 · JSON 串不解包 · 前缀不做群判定 · 无名字不回退 wxid · 空 @ 也发）" \
    "$PY" tools/mutation_check.py quote-ref
run "变异·引用回查（6 条：OR 括号 · 加回 direction 过滤 · 空 id 仍查 · 空白不 strip · 决胜键丢失 · raw_payload 不取）" \
    "$PY" tools/mutation_check.py quote-repo
run "变异·引用装配（11 条：不回查 · 只用一半 id · 参数与 DB 优先级反转 · 行内昵称不传 · 前缀不并进 XML · 记账带前缀 · 成本闸拆掉 · 联系人那一级跳过 · 厂商只认扁平键 · 取昵称不挡群 id · 缓存不写）" \
    "$PY" tools/mutation_check.py quote-adapter

# 触发限速闸（R1，2026-10-03）：两个机器人互相 @ 死循环的止血闸（08:39–08:43 刷 24 条）。
# 病根不是"修错了"，是**硬触发这条路在设计上没有任何上限**：``inbound/triggers`` 第 ⑦/⑧ 条
# （``group_mention`` / ``group_solitaire`` = 100% 必回）在 ``ok=True`` 那一支**直通**
# ``handle_message``，心流的冷却/预算/``check_gate``/``hf_cooldown_ok`` 全长在 ``_try_heartflow``
# 里面 ⇒ 一条都不生效。本闸**只加限速、不改触发识别**（老板拍板「最小改动」）。
# 这一层两种坏法**都安静**：① 判据写松（60 秒 5 次那种档位，10~15 秒的回路稳态只到 4 次
# ⇒ 一次都不拦 —— 一个装好了却空转的闸）；② 判定出错时**拒**而不是放行 ⇒ 限速器一坏，
# 所有 @ 都被吞掉，而现场只有几行 WARNING。
# 另外三处**必须落在装配上**（本机 import 不了 ``adapter.py``，按 AST 源码形状验）：
# 闸建在 ``_handle_one`` 里（每条消息 new 一个 ⇒ 状态只剩本次计数 ⇒ 一次都拦不住）、
# 建在 ``connect()`` 里（重连一次就清空额度）、判定点排到 ④z/④a/④c 之后
# （拦住了回复却拦不住台账污染、裁判现场与媒体花费）。
run "触发限速（只掐必回的两条硬触发 · 第 7 次即静默 · 滑窗不是定桶 · 静默不自我续期 · 出错一律放行 · 阈值自洽 · 回路真在静默期内断掉）" \
    "$PY" -m pytest plugin/tests/test_rate_limit.py -q
run "触发限速装配（进程级一个 · 重连不重建 · key 含账号/群/发送者 · 排在 should_drop 之后且早于落库/缓冲/媒体/心流 · 判了必丢必 WARNING · 阈值不在适配器里分叉）" \
    "$PY" -m pytest plugin/tests/platform/test_rate_limit_wiring.py -q
run "变异·限速本体（17 条：闸不设 · 定桶代滑窗 · 静默自我续期 · 空发送者共用桶 · 出错即拒 · 静默期仍计数 · 上限差一 · 两条腿分开计账 · 键丢账号 · 静默太短 · 窗口取 60 · 不清理过期桶 …）" \
    "$PY" tools/mutation_check.py rate-limit-core
run "变异·限速装配（10 条：每条消息新建 · 重连重建 · key 少维度 · 排到落库/媒体之后 · 判了不丢 · WARNING 降成 debug · 阈值在适配器里再写一遍）" \
    "$PY" tools/mutation_check.py rate-limit-adapter

# 数据层（17 张表 / 反推式 migration / 本地绑定）：失效形态同样静默 ——
# 重复 ADD 报的是 1060（像 SQL 手滑）、裸 GROUP BY chat_id 让占比恒 0%（像还没数据）、
# 无参 SQL 被插值报的是 format string 错（像 SQL 写错）。三类都会让人查错方向。
run "变异·数据层（25 条：schema 漂移 / 重复 ADD / 非本地库 / 无参插值 / 事务与快照 / 漂移体检）" \
    "$PY" tools/mutation_check.py db-migrations db-conn db-shape db-tx db-drift

# 真库用例的**护栏**（D1，2026-10-02）：``conftest._env_guard`` 是老板那条
# 「生产密钥/生产库永不可用于调试」红线的**唯一执行者**，而它此前一条判据都没有。
# 它坏了不会红 —— 会绿着把真库用例放到别的项目（本机 3306 是 1Panel 的）或生产库上。
run "变异·真库护栏（6 条：生产库名 / *_test 后缀 / ALLOW_REMOTE / 可达性 / 强制开关）" \
    "$PY" tools/mutation_check.py db-live-guard

# DB 体检接线（D7，2026-10-02）：``adapter._run_migrations`` 那一段的失效形态是
# "**看起来一切正常**" —— 失败只留一行 warn、gauge 忘了归零、计数器不增、异常吞成 pass。
# 心流每条都写不进台账，而指标里 0 条发言看起来就像"群里没人说话"。
run "变异·DB 体检接线（6 条：gauge 不置 0 / 不计数 / 吞异常 / warn 当 error）" \
    "$PY" tools/mutation_check.py adapter-db-health

# ★ 真库用例（D1，2026-10-02）：stub 验的是"框架发了什么语句"，**真服务端语义**
# （``autocommit=False`` 下的事务/快照、真 information_schema 行形状、服务端补的显示宽度）
# 只能在真库上验。真测试库 ``wechatpro_test`` 在 .183 上，**本机没有** ⇒ 缺席就跳过，
# 但**大声**说明"这不是通过"（照抄上面 TS 差分那条的口径）。
# ⚠️ 在 .183 上跑时本树由 ``WPP_REQUIRE_LIVE_DB=1`` 把"跳过"变成**红** ——
#    本机跳过是诚实，到了本该有真库的机器上还跳过就是假绿。
if [ -n "${WECHATPRO_DB_NAME:-}" ]; then
    run "真库用例（真 MariaDB：幂等 / 漂移 / 事务原子 / 快照收尾）" \
        env WPP_REQUIRE_LIVE_DB=1 "$PY" -m pytest plugin/tests/test_db_live.py -q
else
    printf '\n\033[1m── 真库用例（真 MariaDB）\033[0m\n'
    printf '  \033[33m⏭  跳过：未设 WECHATPRO_DB_NAME（缺省 %s 是**生产库**，绝不用）\n' "wechatpro"
    printf '     这不是通过 —— 只是本机没有真测试库；发布前应在 .183 上跑:\n'
    printf '       WECHATPRO_DB_NAME=wechatpro_test tools/gates.sh\n'
    printf '     （也可单独跑 tools/probe_v2_migration.py / tools/probe_v2b_repos.py）\n'
    printf '     .183 上的可照抄步骤（本机没有真库，所以这段只能在那台机上跑）:\033[0m\n'
    printf '       # 1) 同步 dev 树**不含凭据**（plugin/accounts 必须排除）\n'
    printf '       rsync -a --delete --exclude .git --exclude __pycache__ \\\n'
    printf '             --exclude .pytest_cache --exclude plugin/accounts \\\n'
    printf '             /root/dev/wpp-hermes-py2/ root@172.20.109.183:/root/dev/wpp-hermes-py2/\n'
    printf '       # 2) 在 .183 上（ssh root@172.20.109.183），测试库 env 用**进程替换**读，\n'
    printf '       #    别落盘 —— 凭据不留任何文件（临时文件这条路已经删过一次了）:\n'
    printf '       cd /root/dev/wpp-hermes-py2\n'
    printf '       set -a; . <(command grep "^WECHATPRO_DB_" /root/.hermes/.env); set +a\n'
    printf '       WECHATPRO_DB_NAME=wechatpro_test WPP_REQUIRE_LIVE_DB=1 \\\n'
    printf '         /root/venvs/wpp-test/bin/python3 -m pytest plugin/tests/test_db_live.py -q\n'
    printf '     说明：venv 是 /root/venvs/wpp-test（pymysql + pytest + **pyyaml** ——\n'
    printf '           plugin/tests/test_skills.py 读技能 frontmatter 要用；缺了会在\n'
    printf '           收集阶段整个 ERROR，看起来像代码坏了）。\n'
    printf '           以上只覆盖 test_db_live.py；全量要加 -m "" 与 plugin/tests。\033[0m\n'
fi

# 心流台账读侧（B9，2026-10-02）：调阈要按**每条样本自己那一小时**查本底，
# 而"哪一小时"只能从 ``sent_at`` 得到 —— 少取一列 / 不归一，两条路都在
# "阈值静默僵住"与"夜里那批噪声被当有效信息"之间，且**都不报错**。
run "变异·心流台账读侧（2 条：样本缺 sent_at / 不归一）" \
    "$PY" tools/mutation_check.py repo-hf

# 启用 / 账号 / 注册（F1 + F5）：这一类失效是**悄悄什么都不干** ——
# 平台没启用 ⇒ 一条消息都收不到；工具少注册 ⇒ 模型看不见那些能力。
# 两边都不报错、doctor 也全绿，所以它们是本地唯一能当场红的地方。
run "变异·启用与账号（22 条：F1 四路 / 空壳账号 / 生产身份·wxid+凭据 / repr 泄凭据）" \
    "$PY" tools/mutation_check.py enablement accounts

# 生产身份红线的**接线**（2026-10-02 审阅补，C4）：判据（上面那套）与"挂没挂上"是两件事。
# 失效形态：`refusal_reason` 把"没配好"和"红线"混成同一个出口 ⇒ 红线只被 warn 一句就
# 跳过去，插件照常起来、照常连别的账号，**日志里那行还不一定有人看见**。
run "变异·红线接线（3 条：不接线 / 判空集 / 挪到过滤之后）" \
    "$PY" tools/mutation_check.py adapter-account-guard
run "变异·注册面（4 条：F5 导入前缀 / 吞错不出声 / 独有能力截断 / 端点表半份）" \
    "$PY" tools/mutation_check.py registration

# 平台面两道闸（2026-10-02 审阅补，P0-B/P0-C）：失效形态是**不该进的进来了**，
# 跟上面几类正相反 —— 不缺消息、不报错、日志看着合理，只是凭据判据是恒真。
#   ① filehelper 管理门：空 adminUsers 曾 = 放行、空发件人曾 = 管理员（C3）；
#   ② 厂商 webhook 入站口：未配 secret 曾 = 放行 + webhookPathToken 是死字段
#      ⇒ 回调地址可猜（C2/A6），business 路径还没注册（A5）。
run "变异·filehelper 管理门（9 条：空 adminUsers / 空发件人 / 白名单表 / 判词改名）" \
    "$PY" tools/mutation_check.py filehelper-auth adapter-filehelper-auth
run "变异·webhook 鉴权与路径（23 条：fail-open / token 死字段 / business 404 / 无凭据注册）" \
    "$PY" tools/mutation_check.py webhook-paths webhook-auth adapter-webhook

# 出站闸（2026-10-02 审阅补，P0-D）：`WECHATPRO_SEND_ALLOWLIST` 此前只挡住适配器那三条
# 发送路，299 个工具（含 216 条 vendor 端点表）**一条都不判定** ⇒ 调试期的"保险"是假的。
#   收口面没变 —— 仍是"全部工具 + 适配器 + standalone 都过同一个判据"。
# 失效形态与上面两类又不同：**不报错、不少消息、日志全绿**，只是白名单没起作用 ——
# 消息已经发到生产群了（红线 3：发出去撤不回）。所以判据 + 收口接线各一套。
run "变异·出站闸·判据（14 条：写词优先 / fail-open / blackList 误收 / 词表漏 Send）" \
    "$PY" tools/mutation_check.py outbound-guard
run "变异·出站闸·收口接线（6 条：判据没挂上 / body 没传 / 三个 helper 漏传）" \
    "$PY" tools/mutation_check.py client-outbound-guard
# 写端点不重试（2026-10-18 审阅修）：超时/502 时"没发出去"与"发出去了但响应丢了"
# 无法区分 —— 重试的代价是给真实联系人发第二条，而微信撤不回。
run "变异·发送不重试（3 条：写端点收口被摘 / 放宽成 1 / 分类反了）" \
    "$PY" tools/mutation_check.py client-retry

# 技能落位（``skills/*/SKILL.md``）：**静默丢失**的又一种形态 —— Hermes 的
# ``_discover_skills`` 对"目录在但 SKILL.md 缺失/frontmatter 不合法"一律 continue，
# 一行日志都不打；正文里的工具名写错更是连加载都成功。
run "技能落位守卫（SKILL.md 合法性 + 正文工具名能落到实注册/真表）" \
    "$PY" -m pytest plugin/tests/test_skills.py -q
run "变异·技能落位（4 条：缺 frontmatter / name 不符 / 描述空 / 工具名写错）" \
    "$PY" tools/mutation_check.py skills

# 语音转写链路（TS ``inbound/handler.ts`` + ``storage/stt.ts`` 的移植）：
# 这条链上每一环坏了都**不出声** —— WAV 头错对方 400、转写算出来没接进正文、
# 缺解码器/key 时静默丢能力。所以四段分开变异，别混成一套。
run "语音守卫（注入记号/STT 策略/WAV 头/重试分类/适配器接线）" \
    "$PY" -m pytest plugin/tests/test_voice.py -q
run "变异·语音（13 条：采样率 / RIFF 长度 / 重试分类 / 空 PCM / key 优先级 / 降级静默）" \
    "$PY" tools/mutation_check.py voice-stt
run "变异·语音读回（2 条：两个转写来源反了 / 空白串当成有转写）" \
    "$PY" tools/mutation_check.py voice-media
run "变异·语音注入（3 条：没接进正文 / 记号改字 / 没转写也写空行）" \
    "$PY" tools/mutation_check.py voice-inject
run "变异·语音接线（5 条：不接转写 / 不写回 media / vendor 短路被摘 / 缺依赖硬撞 / 取媒体晚于正文）" \
    "$PY" tools/mutation_check.py adapter-voice

# 工具面：**注册得上 ≠ 调得动**（W0，2026-10-02 在 .183 真机抓到的 P0）。
# 这一族的失效形态是本仓最贵的一种：注册数、plugin.yaml 逐名对账、doctor **三项全绿**，
# 而模型点任何工具都只拿到 tool_error。所以判据必须落在"调用"上，不是"名字"上。
# ⚠️ 本地这一层只证"我们的适配层自洽"；"Hermes 真的会这么调"由 .183 上的
#    tools/probe_tool_dispatch.py 走**真 registry + 真 dispatch** 验（见文件末尾提示）。
run "工具调用契约（位置 dict / 窄签名 / is_async / 厂商请求体 / 端点绑定）" \
    "$PY" -m pytest plugin/tests/test_hermes_tool_contract.py -q
run "变异·工具契约（6 条：适配器收 **kw / 设 __wrapped__ / 收口不包 / 信封 / JSON / 非 dict）" \
    "$PY" tools/mutation_check.py tool-contract
run "变异·契约收口点（1 条：register() 绕过 wrap_ctx）" \
    "$PY" tools/mutation_check.py tool-contract-seam
run "变异·通用端点表（2 条：getter 被同名参数挡住 / account_id 不 pop 漏进请求体）" \
    "$PY" tools/mutation_check.py tool-registry
run "变异·工具策略层（4 条：撤注册缩水 / 步数端点退回 setproxy / 参数名退回 / 补注册端点写错）" \
    "$PY" tools/mutation_check.py tool-policy

# V7：心流**热路径零 DB IO**。这条退化不报错、不少回复、行为测试全绿 ——
# 只是每条群消息多一次库往返。只能用"数"的：db_call_count() 是仪器，
# 所以三条仪器变异必须和热路径用例一起跑（哑掉的计数器让 0 变得不可伪证）。
run "V7 热路径零 DB 守卫（闸门 / judge / 编排 + 仪器自证）" \
    "$PY" -m pytest plugin/tests/heartflow/test_hot_path_no_db.py -q
run "变异·热路径（2 条：画像缓存冷了回库 / 画像预算冷了回库）" \
    "$PY" tools/mutation_check.py hot-path-db
run "变异·热路径闸门（1 条：预算 peek 里塞库查询）" \
    "$PY" tools/mutation_check.py hot-path-budget
run "变异·热路径仪器（3 条：计数器哑 / 读数恒 0 / 归零不归零）" \
    "$PY" tools/mutation_check.py hot-path-instrument

run "全树语法" "$PY" -m compileall -q plugin tools

printf '\n\033[1m════════════════════════════════════\033[0m\n'
if [ -n "$FAILED" ]; then
    printf '\033[31m门禁未过 —— %d 项失败:%s\033[0m\n' "$(printf '%s' "$FAILED" | grep -c '  - ')" "$FAILED"
    exit 1
fi
printf '\033[32m门禁全绿（%d 项）\033[0m' "$NPASS"
[ "$NSKIP" -gt 0 ] && printf '\033[2m｜跳过 %d 项（--only %s）\033[0m' "$NSKIP" "$ONLY"
printf '\n'
printf '\n⚠️  本地门禁 ≠ 真机验收。发布前还必须在 .183 上跑：\n'
printf '     hermes plugins validate   # 依赖不缺（V3）\n'
printf '     hermes plugins doctor     # registrations: 302 tool(s)（V6）\n'
printf '     sh /tmp/wpp-py2-verify/tools/hermes_run.sh \\\n'
printf '        /tmp/wpp-py2-verify/tools/probe_tool_dispatch.py /tmp/wpp-py2-verify\n'
printf '                               # 工具**真能调**（W0：真 registry + 真 dispatch；只读）\n'
printf '     WECHATPRO_DB_NAME=wechatpro_test 跑真库用例（D1；上面那段有可照抄的命令）\n'
