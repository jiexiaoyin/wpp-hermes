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

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
[ $# -ge 1 ] && ROOT=$1
cd "$ROOT" || exit 1
PY=${PY:-python3}

FAILED=""
NPASS=0

# run <名字> <命令...>
run() {
    name=$1
    shift
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
run "变异测试·预算（12 条变异必须全被杀死）" "$PY" tools/mutation_check.py budget

run "变异测试·去重（17 条变异必须全被杀死）" "$PY" tools/mutation_check.py dedupe

run "变异测试·标签（13 条变异必须全被杀死）" "$PY" tools/mutation_check.py label

run "变异测试·分层（28 条变异必须全被杀死）" "$PY" tools/mutation_check.py layer

run "变异测试·观测（44 条变异必须全被杀死）" "$PY" tools/mutation_check.py observe
run "变异测试·画像（70 条变异必须全被杀死）" "$PY" tools/mutation_check.py profile
run "变异测试·学习（99 条变异必须全被杀死）" "$PY" tools/mutation_check.py learn

run "变异测试·心流闸门（108 条变异必须全被杀死）" "$PY" tools/mutation_check.py heartflow

# 心流运维命令面（``/heartflow status|threshold|group|profile|why|layers|veto|report``）
# 单列一套：这一面的失效形态是"**回答得头头是道、内容却是错的**" ——
# 09-26 停摆 3 天时状态页照样报 0.60（judge 按生效值判），照样全绿。
run "变异测试·心流运维命令面（25 条变异必须全被杀死）" "$PY" tools/mutation_check.py hf-commands

# 适配器上的四处接缝（收信旁路 / 发送前置 / 出站记录 / 交付结算）单列一套：
# 它们的"正确"只能靠**调用点源码形状**验（``plugin.platform.adapter`` 在本机 import 不了，
# 模块级 `from gateway.config import Platform` 是 Hermes 独有）—— 所以这一套是本地唯一的防线。
run "变异测试·适配器接缝（12 条变异必须全被杀死）" "$PY" tools/mutation_check.py adapter-seams

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
run "变异·入站判定（16 条：接龙被吞 / 裸关键词 / 静默面放宽 / 闸门顺序 / fail-closed）" \
    "$PY" tools/mutation_check.py hongbao relay triggers

# 数据层（16 张表 / 反推式 migration / 本地绑定）：失效形态同样静默 ——
# 重复 ADD 报的是 1060（像 SQL 手滑）、裸 GROUP BY chat_id 让占比恒 0%（像还没数据）、
# 无参 SQL 被插值报的是 format string 错（像 SQL 写错）。三类都会让人查错方向。
run "变异·数据层（8 条：schema 漂移 / 重复 ADD / 非本地库 / 无参插值）" \
    "$PY" tools/mutation_check.py db-migrations db-conn db-shape

# 启用 / 账号 / 注册（F1 + F5）：这一类失效是**悄悄什么都不干** ——
# 平台没启用 ⇒ 一条消息都收不到；工具少注册 ⇒ 模型看不见那些能力。
# 两边都不报错、doctor 也全绿，所以它们是本地唯一能当场红的地方。
run "变异·启用与账号（14 条：F1 四路 / 空壳账号 / 生产身份 / repr 泄凭据）" \
    "$PY" tools/mutation_check.py enablement accounts
run "变异·注册面（4 条：F5 导入前缀 / 吞错不出声 / 独有能力截断 / 端点表半份）" \
    "$PY" tools/mutation_check.py registration

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
printf '\033[32m门禁全绿（%d 项）\033[0m\n' "$NPASS"
printf '\n⚠️  本地门禁 ≠ 真机验收。发布前还必须在 .183 上跑：\n'
printf '     hermes plugins validate   # 依赖不缺（V3）\n'
printf '     hermes plugins doctor     # registrations: 262 tool(s)（V6）\n'
