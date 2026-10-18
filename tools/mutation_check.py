"""变异测试：证明心流各模块的 pytest 套件**真的会红**。

## 为什么需要它（这一步已经吃过一次亏）

绿色的测试套件**本身不是证据**：断言写松了、分支根本没跑到，它一样全绿。
2026-10-01 的 F5 就是同一种形态 —— 注册探针报 248 而不是 262，而 AST 等价门与
差分门**都放行了**。所以本仓的规矩是：**新写的回归测试必须能被变异杀死**。

## 判据（三种结果，只有第一种算通过）

- ``KILLED``   变异后测试变红 ⇒ 测试抓到了这条行为。**通过**
- ``SURVIVED`` 变异后测试仍全绿 ⇒ 测试**没覆盖**这条行为。**不通过**
- ``BROKEN``   变异后连收集都过不去（语法/签名错）⇒ **不计入**。它只说明变异本身
  写坏了，**不能**当成"抓到了" —— 第一版脚本在这里记过一次假通过。

## 用法

    python3 tools/mutation_check.py            # 全部
    python3 tools/mutation_check.py dedupe     # 只跑某个模块
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
HF = ROOT / "plugin" / "inbound" / "heartflow"
ADAPTER = ROOT / "plugin" / "platform" / "adapter.py"
PARSER = ROOT / "plugin" / "inbound" / "parser.py"
RELAY = ROOT / "plugin" / "inbound" / "relay.py"
MEDIA_OSS = ROOT / "plugin" / "inbound" / "media_oss.py"

#: 不变量守卫的测试文件（下面 ``inv-*`` 六套共用它当"唯一裁判"）。
INVARIANTS_TEST = "plugin/tests/invariants/test_invariants.py"

#: ``模块名 -> (被测文件, 测试文件, [(说明, 原文, 改成)])``
#:
#: 每条 ``原文`` **必须恰好命中一次**，否则脚本自曝锚点失效（那也是假绿的一种）。
SUITES: dict[str, tuple[Path, str, list[tuple[str, str, str]]]] = {
    "budget": (HF / "budget.py", "plugin/tests/heartflow/test_budget.py", [
        ("默认档 180s → 60s", 'minGapSec": 180', 'minGapSec": 60'),
        ("静默段半开 → 闭区间（12 点被误静默）",
         "if (start < end and start <= h < end)", "if (start < end and start <= h <= end)"),
        ("闸优先级：静默段降级成 budget-gap",
         "return False, REASON_QUIET_HOURS", "return False, REASON_GAP"),
        ("脏值收敛丢掉下界（-3 变 -3）",
         "return max(0, int(math.floor(v)))", "return int(math.floor(v))"),
        ("每小时闸 >= 改 >（第 9 条会放行）",
         "rolled.hour_count >= cfg.max_per_hour", "rolled.hour_count > cfg.max_per_hour"),
        ("陈旧闸 <= 改 <（同刻重放被放行）",
         "cand <= rolled.last_reply_at_sec", "cand < rolled.last_reply_at_sec"),
        ("跨天不清天计数（额度跨天累积）",
         "day_count=state.day_count if same_day else 0,", "day_count=state.day_count,"),
        ("seed 跳过空 group_id 失效",
         "        if not group_id:\n            continue\n",
         "        if group_id is None:\n            continue\n"),
        ("note_reply_sent 不计小时（额度永不消耗）",
         "hour_count=rolled.hour_count + 1,", "hour_count=rolled.hour_count,"),
        ("roll 跨小时不清小时计数",
         "hour_count=state.hour_count if state.hour_key == hour_key else 0,",
         "hour_count=state.hour_count,"),
        ("预算缓存不登记（跨用例污染）",
         'CACHE_REGISTRY.register("budget", reset_budget_cache)',
         'CACHE_REGISTRY.register("budgetNOPE", reset_budget_cache)'),
        ("时间桶用 UTC（与本地整点不同口径）",
         "d = datetime.fromtimestamp(now_sec).replace(minute=0, second=0, microsecond=0)",
         "d = datetime.utcfromtimestamp(now_sec).replace(minute=0, second=0, microsecond=0)"),
        # ── B1（2026-10-02）：配置脏值不许在热路径上炸 ──────────────────────
        # resolve_budget 在**每条触发消息**上被调（heartflow.check_gate）⇒ 这几条的
        # 失效形态比"值算错"贵得多：一个字符串就让心流**整条静默**，日志只有一行
        # ValueError。反方向（静默放宽）同样要挡住 —— 脏值必须回落到**默认档**的闸。
        ("脏值兜底删掉（裸 int()：配置里一个字符串 ⇒ 每条消息在预算这步抛异常，心流整条静默）",
         '        min_gap_sec=_cfg_num("minGapSec", b.get("minGapSec"), D["minGapSec"]),',
         '        min_gap_sec=int(b.get("minGapSec", D["minGapSec"])),'),
        ("布尔当数字（JSON 里的 true/false 被当成 1/0 悄悄改小闸门）",
         "    if isinstance(raw, bool) or not isinstance(raw, (int, float)):",
         "    if not isinstance(raw, (int, float)):"),
        ("非有限数不滤（NaN/inf 直接进 int() ⇒ OverflowError，同样是热路径炸）",
         "    elif not math.isfinite(raw):",
         "    elif False:"),
        ("脏值告警删掉（静默降级：配置写错了永远查不出来）",
         '            log.warning("[WPP] 心流预算参数 %s 是脏值 %r ⇒ 回落默认 %s"\n'
         '                        "（配置写错了，别以为它在生效）", field, raw, fallback)',
         "            pass"),
        ("脏值告警不去重（热路径每条消息一行，真信号被刷没）",
         "        if key not in _dirty_warned:",
         "        if True:"),
        ("重置不清告警去重表（改完配置仍不再出声 ⇒ 「改了没反应」）",
         "        _dirty_warned.clear()",
         "        pass"),
    ]),
    "dedupe": (HF / "dedupe.py", "plugin/tests/heartflow/test_dedupe.py", [
        ("emoji 区右端点 1FAFF → 1F5FF（大片 emoji 漏剥）",
         '"[\\U0001F000-\\U0001FAFF"', '"[\\U0001F000-\\U0001F5FF"'),
        ("丢掉零宽连接符 U+200D（组合 emoji 残留）",
         '    "\\u200D"                     # 零宽连接符（组合 emoji 的粘合剂）\n', ""),
        ("全角区间端点 FF5E → FF5D（`～` 不转半角）",
         '"[\\uFF01-\\uFF5E]"', '"[\\uFF01-\\uFF5D]"'),
        ("短句豁免失效（文本侧 minChars 判定去掉）",
         "    if len(a) < cfg.min_chars:\n        return None",
         "    if len(a) <= 0:\n        return None"),
        ("窗口判定 >= 而不是 >（边界外仍命中）",
         "if now_sec - h.at_sec > cfg.window_sec:", "if now_sec - h.at_sec >= cfg.window_sec:"),
        ("相似阈值判定 >= 改 >（恰好等于阈值的漏放行）",
         "if sim < cfg.sim_threshold:", "if sim <= cfg.sim_threshold:"),
        ("Jaccard 分母漏减交集（相似度恒偏低）",
         "union = len(a) + len(b) - inter", "union = len(a) + len(b)"),
        # ⚠️ 曾试过把 ``if not a or not b`` 与 ``union <= 0`` 当变异点 —— 两条都是**等价变异**
        #    （空集走哪条路都返回 0，且两者非空时 union ≥ 1 恒成立）。等价变异杀不掉，
        #    留着只会训练出"看到 SURVIVED 就去改测试"的坏习惯。见 test_jaccard 的注释。
        ("记账不按窗口裁剪（内存无限增长 + 陈旧历史误杀）",
         "kept = [h for h in hist if at_sec - h.at_sec <= cfg.window_sec]",
         "kept = list(hist)"),
        ("超容量丢最新而不是最旧",
         "kept = kept[len(kept) - cfg.history_max:]", "kept = kept[:cfg.history_max]"),
        ("短句也进历史（两侧不对称 ⇒ 「好的」互相判重）",
         "    if len(norm) < cfg.min_chars:\n        return", "    if len(norm) <= 0:\n        return"),
        ("proactiveOnly 开关失效（被点名也叫装死）",
         "if cfg.proactive_only and proactive is False:", "if False:"),
        ("historyMax 不钳上限（配置可写到爆内存）",
         "min(_pos(d.get(\"historyMax\"), D[\"historyMax\"]), HF_HISTORY_MAX_CAP)",
         "_pos(d.get(\"historyMax\"), D[\"historyMax\"])"),
        ("simThreshold 越界不回落（1.5 ⇒ 闸永远不拦）",
         "and math.isfinite(sim_raw) and 0 < sim_raw <= 1", "and math.isfinite(sim_raw)"),
        ("归一化保留标点（同一句话两种排版算两句）",
         's = "".join(ch for ch in s if ch.isalnum())', "pass"),
        ("归一化丢掉数字（iPhone 15/16 被当重复）",
         's = "".join(ch for ch in s if ch.isalnum())',
         's = "".join(ch for ch in s if ch.isalpha())'),
        ("prefix 分支不覆盖 similar（更近的旧句抢走）",
         "if best is None or best.kind == \"similar\" or _prev_is_newer(h, best):",
         "if best is None:"),
        ("缓存清理留下空壳键（tracked_groups 永不清零）",
         "            if not kept:\n                del _recent[k]", "            if not kept:\n                pass"),
        # --- 启动预热（B7，2026-10-02）----
        # 这一段的失效形态是**全绿但不对**：不预热 ⇒ 每次重启后 windowSec 内复读闸
        # 等于不存在，而台账 / 日志 / status 只看"发没发出去"，全都看不出来。
        ("预热不看闸开关（闸关掉了也灌历史）",
         "    if not cfg.enabled:\n        return 0\n", ""),
        ("预热拿 now 当窗口下界（一条都灌不进来 / B7 复发）",
         "            account_id, ts - cfg.window_sec, limit=HF_DEDUPE_PRELOAD_LIMIT)",
         "            account_id, ts, limit=HF_DEDUPE_PRELOAD_LIMIT)"),
        ("预热不设条数上限（拉全表）",
         "limit=HF_DEDUPE_PRELOAD_LIMIT", "limit=10 ** 9"),
        ("预热把缺群的行也归档（凭空建出无名桶）",
         "        if not text or not gid:\n            continue", "        if not text:\n            continue"),
        ("预热把时刻 0 的行也灌进去（计数虚高 + prune 前的瞬时误杀）",
         "        if at <= 0:\n            continue\n", ""),
    ]),
    "label": (HF / "label.py", "plugin/tests/heartflow/test_label.py", [
        ("强负词表丢掉「别刷了」（引用消息上不再判负）",
         '"别刷了", "别刷屏", "不要刷屏", "别发了", "别说话了", "别说话", "闭嘴",',
         '"别刷屏", "不要刷屏", "别发了", "别说话了", "别说话", "闭嘴",'),
        ("硬词表混入软词（裸消息也敢判负 ⇒ 冤枉好回复）",
         'HF_HARD_NEGATIVE_PHRASES: tuple[str, ...] = ("别刷了", "别刷屏", "不要刷屏", "别发了")',
         'HF_HARD_NEGATIVE_PHRASES: tuple[str, ...] = ("别刷了", "别刷屏", "不要刷屏", "别发了", "别说话了")'),
        ("归一化不去空白（「别 刷 屏」漏判）",
         '    return "".join(s.split()).lower()', "    return s.lower()"),
        ("信号值域丢 silence（该行静默变 legacy）",
         'HF_ENGAGE_SIGNALS: tuple[str, ...] = (\n    "quote", "mention", "negative", "veto", "short-window", "silence")',
         'HF_ENGAGE_SIGNALS: tuple[str, ...] = (\n    "quote", "mention", "negative", "veto", "short-window")'),
        ("as_hf_engage_signal 不校验（旧错误标签进学习样本）",
         "    return s if isinstance(s, str) and s in HF_ENGAGE_SIGNALS else None",
         "    return s"),
        ("强信号表丢 veto（冷清群里 veto 被本底过滤掉）",
         'HF_STRONG_SIGNALS: tuple[str, ...] = ("quote", "mention", "negative", "veto")',
         'HF_STRONG_SIGNALS: tuple[str, ...] = ("quote", "mention", "negative")'),
        ("观察窗上端点改闭（== obsEnd 的晚到引用被丢）",
         "        if c.at_sec < sent_at or c.at_sec > obs_end:", "        if c.at_sec < sent_at or c.at_sec >= obs_end:"),
        ("弱窗上端点改开（== labelEnd 的弱信号被丢）",
         "        in_label = c.at_sec <= label_end", "        in_label = c.at_sec < label_end"),
        ("quote 分支整个失效（引用不再算强正）",
         "        if c.quotes_bot:\n            # 引用 bot 的那条：说\"别刷了\" = 负面；否则 = 强正",
         "        if False:\n            # 引用 bot 的那条：说\"别刷了\" = 负面；否则 = 强正"),
        ("弱信号也关窗（再没机会被更强信号升级）",
         '        return HfEngagement(engaged=1, signal="short-window", close=False)   # 弱：不关窗',
         '        return HfEngagement(engaged=1, signal="short-window", close=True)   # 弱：不关窗'),
        ("ambientP 丢掉退化输入守卫（NaN 传染到全群统计）",
         "    if not _finite(count) or count <= 0:\n        return 0.0\n", ""),
        ("ambientP 不是泊松（退化成线性 · 概率越界）",
         "    return 1.0 - math.exp(-rate * max(0.0, w))", "    return rate * max(0.0, w)"),
        ("可采信判定改 <=（恰等于 ambientMax 的边缘样本混进来）",
         "    return ambient_p < ambient_max", "    return ambient_p <= ambient_max"),
    ]),
    "layer": (HF / "layer.py", "plugin/tests/heartflow/test_layer.py", [
        ("段边界右开改闭（7 点被算进上一段）",
         "        if s <= h < e:", "        if s <= h <= e:"),
        ("段边界左闭改开（0 点不再归 0-7）",
         "        if s <= h < e:", "        if s < h < e:"),
        ("分段重叠不再拦（半套分段生效）",
         "        if out[i][0] < out[i - 1][1]:", "        if False:"),
        ("分段上界 24 误写成 23（默认四段自己判非法）",
         "        if s < 0 or e > 24 or s >= e:", "        if s < 0 or e > 23 or s >= e:"),
        ("分段整数校验放松（1.5 被截断成 1）",
         "        return int(v) if math.isfinite(v) and v.is_integer() else None",
         "        return int(v) if math.isfinite(v) else None"),
        ("分段数上限失效（可配到 24 段撑爆表）",
         "or len(raw) > HF_LAYER_MAX_BUCKETS:", "or len(raw) > 99:"),
        # ⚠️ 这里有过一次**双重守卫互相掩盖**：聚合里有两道闸挡未知信号 ——
        #    ``as_hf_engage_signal`` 的收窄，和 ``is_hf_sample_informative(None) -> False``。
        #    单独删任何一道都是**等价变异**（另一道照样挡住"bogus"），要删掉**收窄本身**
        #    才观察得到差别。记在这里，免得下次又绕一圈。
        ("信号收窄取消（未知值/旧错误标签进统计）",
         "        signal = as_hf_engage_signal(s.engage_signal)",
         "        signal = s.engage_signal"),
        ("聚合不再丢无时刻行（无法归段的样本也计入）",
         "        if not _is_number(at) or at <= 0:\n            continue\n",
         "        if not _is_number(at):\n            continue\n"),
        ("本底过滤改用该群最大值而不是逐样本那一小时",
         '        ap = opts.ambient_p_by_group_hour.get(f"{gid}|{hour}") or 0',
         '        ap = max([v for k, v in opts.ambient_p_by_group_hour.items()'
         ' if k.startswith(f"{gid}|")] or [0])'),
        ("本底过滤失效（弱信号在饱和群也采信）",
         "        if not is_hf_sample_informative(signal, ap, opts.ambient_max):",
         "        if not is_hf_sample_informative(signal, 0.0, opts.ambient_max):"),
        ("engaged 判定改 is not None（engaged=0 也算接话）",
         "        if s.engaged == 1:", "        if s.engaged is not None:"),
        ("写库签名去掉变化判定（每轮全量重写 ⇒ 累加桶不可察觉）",
         "            if _written.get(k) == sig:\n                continue",
         "            if False:\n                continue"),
        ("写库签名不含 window_start（窗口滚动后行永不重写）",
         '    return f"{r.n}|{r.engaged}|{_fmt_ambient(r.ambient_p)}|{r.window_start}"',
         '    return f"{r.n}|{r.engaged}|{_fmt_ambient(r.ambient_p)}"'),
        ("layered.enabled 开关失效（关掉仍照打 DB）",
         "    if not LAY.enabled:\n        return 0", "    if False:\n        return 0"),
        ("窗口天数写死 60（配置的 windowDays 不生效）",
         "    window_start = now_sec - LAY.window_days * 86400",
         "    window_start = now_sec - 60 * 86400"),
        ("入站量缓存 TTL 判定改 <= 5（同轮两次查询）",
         "if hit is not None and now_sec - hit[0] < HF_HOUR_COUNTS_TTL_SEC:",
         "if hit is not None and now_sec - hit[0] <= 5:"),
        ("入站量缓存 TTL 判定改 <=（第 120 秒仍算命中）",
         "if hit is not None and now_sec - hit[0] < HF_HOUR_COUNTS_TTL_SEC:",
         "if hit is not None and now_sec - hit[0] <= HF_HOUR_COUNTS_TTL_SEC:"),
        ("入站量缓存永不过期（配置改了也拿旧数）",
         "if hit is not None and now_sec - hit[0] < HF_HOUR_COUNTS_TTL_SEC:",
         "if hit is not None:"),
        ("入站量桶键丢了分隔符（group 与 hour 串味）",
         '        counts[f"{r[\'group_id\']}|{int(r[\'h\'])}"] = int(r["n"])',
         '        counts[f"{r[\'group_id\']}{int(r[\'h\'])}"] = int(r["n"])'),
        ("本底回看天数 14 改成 7（与调阈侧不再同源）",
         "        now_sec - HF_AMBIENT_LOOKBACK_DAYS * 86400,\n        _local_offset_sec(now_sec),",
         "        now_sec - 7 * 86400,\n        _local_offset_sec(now_sec),"),
        ("ambientP 的两个时长参数对调（概率彻底算错）",
         "        k: hf_ambient_p(n, HF_AMBIENT_LOOKBACK_DAYS * 3600, label_window_sec)",
         "        k: hf_ambient_p(n, label_window_sec, HF_AMBIENT_LOOKBACK_DAYS * 3600)"),
        ("本地偏移写死 0（整天归段错 8 小时）",
         "    off = datetime.fromtimestamp(now_sec).astimezone().utcoffset()",
         "    off = None"),
        ("reset 漏清写库签名（清完缓存后该写的不写）",
         "        _layer_stats.clear()\n        _written.clear()\n        _hour_counts.clear()",
         "        _layer_stats.clear()\n        _hour_counts.clear()"),
        ("预热不清该账号的陈旧行（库里删掉的段永远留在内存）",
         "        for k in [k for k in _layer_stats if k.startswith(prefix)]:\n            del _layer_stats[k]\n",
         ""),
        ("预热失败改为向上抛（一个坏库把账号启动拖死）",
         "    except Exception as exc:  # noqa: BLE001 —— 预热失败降级，见 docstring",
         "    except ValueError as exc:"),
        ("本底取数失败不再降级（整轮统计跟着炸）",
         "    except Exception as exc:  # noqa: BLE001 —— 降级路径见上\n"
         "        log.warning(\"[WPP HF] 分层本底取数失败 ⇒ 本轮不做本底过滤: %s\", exc)",
         "    except ValueError as exc:\n"
         "        log.warning(\"[WPP HF] 分层本底取数失败 ⇒ 本轮不做本底过滤: %s\", exc)"),
        ("样本触顶不告警（统计偏高无人知）",
         "    if len(rows_in) >= HF_LAYER_DB_ROW_CAP:", "    if len(rows_in) > HF_LAYER_DB_ROW_CAP:"),
        ("缓存不登记（跨用例污染 + 测试清不干净）",
         'CACHE_REGISTRY.register("layer", reset_hf_layer_cache)',
         'CACHE_REGISTRY.register("layerNOPE", reset_hf_layer_cache)'),
    ]),
    "observe": (HF / "observe.py", "plugin/tests/heartflow/test_observe.py", [
        ("占比分母漏掉 bot（改回 bot/人类 口径 ⇒ 数字虚高）",
         "    total = inbound + outbound", "    total = inbound"),
        ("空群不再挡除零（0/0 直接炸在判定路径上）",
         "    if total <= 0:\n        return 0.0", "    if total < 0:\n        return 0.0"),
        ("toFixed 改成银行家舍入（与 TS 报告对不上）",
         "        return str(Decimal(v).quantize(quant, rounding=ROUND_HALF_UP))",
         "        return str(Decimal(v).quantize(quant, rounding=ROUND_HALF_EVEN))"),
        ("lift 本底门槛 < 0.01 改 < 0（0.009 吹成 33×）",
         "    if ambient_p < 0.01:\n        return None", "    if ambient_p < 0.0:\n        return None"),
        ("lift 本底门槛改 <=（恰好 0.01 不算 lift）",
         "    if ambient_p < 0.01:\n        return None", "    if ambient_p <= 0.01:\n        return None"),
        ("veto 无样本返回 0 而不是 None（0/0 炸）",
         "    if not _is_num(total) or total <= 0:\n        return None",
         "    if not _is_num(total):\n        return None"),
        ("重复率素材不排序（按传入顺序比对 ⇒ 重复率恒 0）",
         "    ordered = sorted(items, key=lambda it: it.at_sec)", "    ordered = list(items)"),
        ("重复率不按群分组（同句跨群也算重复）",
         "        hist[it.group_id] = h", '        hist[""] = h'),
        ("重复率恒 0（观测面永远报「没有重复」）",
         "        rate=(repeats / total) if total > 0 else 0.0,", "        rate=0.0,"),
        ("prefix/similar 计数对调（归因错但总数对）",
         '            if v.kind == "prefix":\n                prefix += 1\n            else:\n'
         "                similar += 1",
         '            if v.kind == "prefix":\n                similar += 1\n            else:\n'
         "                prefix += 1"),
        ("接话率本底改成固定那一小时（不是逐样本自己那一小时）",
         '        p = ambient_by_group_hour.get(f"{gid}|{hf_local_hour(int(at))}") or 0',
         '        p = ambient_by_group_hour.get(f"{gid}|15") or 0'),
        ("脏/零时刻不再跳过（sent_at=0 也进统计）",
         "        if not _is_num(at) or at <= 0:", "        if not _is_num(at):"),
        ("被过滤样本不再计数（日报看不出样本被丢了多少）",
         "        else:\n            skipped += 1\n    return HfEngagementSummary(",
         "        else:\n            pass\n    return HfEngagementSummary("),
        ("engaged 严格判定放松（bool True 混进来）",
         '            if _is_one(s.get("engaged")):', '            if s.get("engaged"):'),
        ("_is_one 退化成 == 1（Py 里 True == 1）",
         "    return _is_num(v) and v == 1", "    return v == 1"),
        ("接话率分母门槛错（可用 1 条时不给 rate）",
         "        rate=(engaged / usable) if usable > 0 else None,",
         "        rate=(engaged / usable) if usable > 1 else None,"),
        ("本底均值分子不计（lift 的分母恒 0）",
         "            ambient_sum += p", "            ambient_sum += 0.0"),
        ("外环：恰好目标值也收紧",
         "    if not _is_num(share) or share <= cfg.target_share:",
         "    if not _is_num(share) or share < cfg.target_share:"),
        ("外环：样本门槛 or 改 and（一半样本不足也放行）",
         "    if volume.total < cfg.min_msgs or volume.bot_sends < cfg.min_bot_sends:",
         "    if volume.total < cfg.min_msgs and volume.bot_sends < cfg.min_bot_sends:"),
        ("外环：样本门槛改 <=（刚好够也算不够）",
         "    if volume.total < cfg.min_msgs or volume.bot_sends < cfg.min_bot_sends:",
         "    if volume.total <= cfg.min_msgs or volume.bot_sends <= cfg.min_bot_sends:"),
        ("外环：开关失效（关掉仍收紧）",
         "    if not cfg.enabled:\n        return None\n    # 恰好等于目标",
         "    if False:\n        return None\n    # 恰好等于目标"),
        ("外环：floor 不再兜底（0÷2=0 被当未指定 ⇒ 静默失效）",
         "        return max(int(math.floor(v / 2)), floor) if v > 0 else floor",
         "        return int(math.floor(v / 2)) if v > 0 else 0"),
        ("外环：间隔的 cap 去掉（可收紧到半小时不说话）",
         "        min_gap_sec=min(base.min_gap_sec * 2, HF_SHARE_MIN_GAP_CAP_SEC),",
         "        min_gap_sec=base.min_gap_sec * 2,"),
        ("外环：consecutive 标志不继承 base（改成取反）",
         "        no_consecutive_without_human=base.no_consecutive_without_human,",
         "        no_consecutive_without_human=not base.no_consecutive_without_human,"),
        ("外环：targetShare 上界放开（1.5 生效 ⇒ 永不收紧）",
         '    target = ts if (_is_num(ts) and ts > 0 and ts <= 1) else D["targetShare"]',
         '    target = ts if (_is_num(ts) and ts > 0) else D["targetShare"]'),
        ("外环：音量下限的负数不再回落默认",
         "    if not _is_num(v) or v < 0:\n        return fallback",
         "    if not _is_num(v):\n        return fallback"),
        ("外环状态：跨日不失效（昨天的超标锁今天的嘴）",
         "    if st is None or st.day_key != hf_day_bucket_key(now_sec):", "    if st is None:"),
        ("外环状态：键丢掉账号（两个账号串味）",
         '    return f"{account_id}:{group_id}"', '    return f"{group_id}"'),
        ("外环状态：写入丢掉 bot 条数（样本门槛恒不过）",
         "            total=volume.total,\n            bot_sends=volume.bot_sends,",
         "            total=volume.total,\n            bot_sends=0,"),
        ("外环状态：day_key 写死（永远算当日）",
         "            day_key=hf_day_bucket_key(now_sec),",
         "            day_key=hf_day_bucket_key(0),"),
        ("快照：不按账号过滤（别人的群混进报告）",
         "        if not k.startswith(prefix):\n            continue\n        if st.day_key != day_key:",
         "        if False:\n            continue\n        if st.day_key != day_key:"),
        ("快照：排序反向（报告先列占比最低的群）",
         "    return sorted(out, key=lambda h: -h.share)", "    return sorted(out, key=lambda h: h.share)"),
        ("快照：群 id 带着账号前缀（报告里出现 acct:g1）",
         "            group_id=k[len(prefix):], share=st.share, total=st.total,",
         "            group_id=k, share=st.share, total=st.total,"),
        ("快照：reset 不清状态（跨用例污染）",
         "    with _lock:\n        _share.clear()", "    with _lock:\n        pass"),
        ("正文：本底位数 3 → 2",
         '        f" · 本底 {_fixed(eng.ambient_p, 3)}"', '        f" · 本底 {_fixed(eng.ambient_p, 2)}"'),
        ("正文：接话率位数 2 → 1",
         "        f\" · rate {'-' if rate is None else _fixed(rate, 2)}\"",
         "        f\" · rate {'-' if rate is None else _fixed(rate, 1)}\""),
        ("正文：本底过冷的说明消失（读者以为 lift=0×）",
         '        + (" · lift -(本底过冷, 不计算)" if lift is None else f" · lift {_fixed(lift, 1)}×"))',
         '        + ("" if lift is None else f" · lift {_fixed(lift, 1)}×"))'),
        ("正文：veto 占比忘了乘 100（0.1% 而不是 10.0%）",
         '        + ("" if vr is None else f" · veto 占比 {_fixed(vr * 100, 1)}%")',
         '        + ("" if vr is None else f" · veto 占比 {_fixed(vr, 1)}%")'),
        ("正文：群列表上限失效（200 个群全列）",
         "    shown = list(inp.shares[:HF_DIGEST_MAX_GROUPS])", "    shown = list(inp.shares)"),
        ("正文：节预算失效（单节可撑爆整条消息）",
         "_SECTION_CAP = 700", "_SECTION_CAP = 7"),
        ("正文：全局硬 cap 翻倍（截断永不触发）",
         "        if n + len(l) + 1 + len(tail) > HF_DIGEST_MAX_CHARS:",
         "        if n + len(l) + 1 + len(tail) > HF_DIGEST_MAX_CHARS * 2:"),
        ("正文：截断不留标记（读者以为报告就这么短）",
         '    return "\\n".join(kept) + "\\n" + tail', '    return "\\n".join(kept)'),
        ("预算拦截键不再引用 budget 的常量（另抄一份字面量）",
         "REASON_GAP, REASON_HOUR, REASON_DAY, REASON_CONSECUTIVE, REASON_QUIET_HOURS",
         '"budget-gap", "budget-hour", "budget-day", "budget-consecutive", "quiet-hours"'),
        ("缓存不登记（跨用例污染 + 测试清不干净）",
         'CACHE_REGISTRY.register("observe", reset_hf_share_guard)',
         'CACHE_REGISTRY.register("observeNOPE", reset_hf_share_guard)'),
    ]),
    "profile": (HF / "profile.py", "plugin/tests/heartflow/test_profile.py", [
        # --- 参数与缺省
        ("默认 maxTokens 复用 judge 的 300（同一场「静默瘫 3 天」）",
         '"maxTokens": 1200,', '"maxTokens": 300,'),
        ("默认 refreshHours 22 → 2200（画像永不重生）",
         '"refreshHours": 22,', '"refreshHours": 2200,'),
        ("默认 maxPerRun 3 → 100（20 个群同时到期打 20 次 LLM）",
         '"maxPerRun": 3,', '"maxPerRun": 100,'),
        ("默认样本门槛 30 → 3（几条消息也当画像）",
         '"minMsgs": 30,', '"minMsgs": 3,'),
        ("默认 maxRaise 0.1 → 0.9（画像可无上界抬阈值）",
         '"maxRaise": 0.1,', '"maxRaise": 0.9,'),
        ("_pos_int 放下界（0/负数流下去 ⇒ 门槛归零）",
         "    if not _is_num(v) or v <= 0:", "    if not _is_num(v):"),
        ("_nonneg 放下界（负 maxRaise 变成负抬升）",
         "    if not _is_num(v) or v < 0:", "    if not _is_num(v):"),
        ("_to_num 把 bool 当数（band:true 变 1 ⇒ 顶到硬上限）",
         "    if isinstance(v, bool) or v is None:", "    if v is None:"),
        # --- 解析校验
        ("_clamp_band 丢上界（0.95 原样进判定）",
         "    return min(band_max, max(band_min, t))", "    return max(band_min, t)"),
        ("band >1 也收（1.5 被钳成 0.9 而不是丢弃）",
         "            if band_raw is not None and 0 < band_raw <= 1",
         "            if band_raw is not None and 0 < band_raw"),
        ("band ≤0 也收（-0.2 被抬成 bandMin 而不是丢弃）",
         "            if band_raw is not None and 0 < band_raw <= 1",
         "            if band_raw is not None and band_raw <= 1"),
        ("字段不限长（LLM 越写越长 ⇒ prompt 膨胀）",
         "    return s[:max_len]", "    return s"),
        ("字面量 \"null\" 不再视同空",
         '    if not s or s == "null":', "    if not s:"),
        ("字段不折叠空白（换行/多空格原样进 prompt）",
         '    s = re.sub(r"\\s+", " ", v).strip()', "    s = v.strip()"),
        ("话题清单不去重",
         "        if s and s not in out:", "        if s:"),
        ("话题清单不限 5 条",
         '        if len(out) >= LIM["listLen"]:', "        if len(out) >= 99:"),
        ("活跃小时不过 0–23（99/-1 混进去）",
         "        if h < 0 or h > 23 or h in out:", "        if h in out:"),
        ("活跃小时不限 6 个",
         "        if len(out) >= 6:", "        if len(out) >= 99:"),
        ("静默段 start==end 也收（空段进判定）",
         "        if s < 0 or s > 23 or e < 0 or e > 23 or s == e:",
         "        if s < 0 or s > 23 or e < 0 or e > 23:"),
        ("静默段不限 3 段",
         "        if len(out) >= 3:", "        if len(out) >= 99:"),
        ("空画像不再是 None（**写空覆盖上一版**）",
         "    return None if empty else p", "    return p"),
        ("空判定把「有 summary」也算空（只有 summary 的画像被丢掉）",
         "        and not p.summary\n", ""),
        # --- 预算收紧（只能更严）
        ("预算间隔取 min 而不是 max（画像把间隔缩短）",
         '        out["minGapSec"] = max(cfg.min_gap_sec, gap) if cfg is not None else gap',
         '        out["minGapSec"] = min(cfg.min_gap_sec, gap) if cfg is not None else gap'),
        ("预算每小时取 max 而不是 min（画像放宽频率）",
         "            min(cfg.max_per_hour, hour) if cfg is not None and cfg.max_per_hour > 0 else hour",
         "            max(cfg.max_per_hour, hour) if cfg is not None and cfg.max_per_hour > 0 else hour"),
        ("预算建议收下 0/负数（0 被当「不限」的真值）",
         "        return int(math.floor(n)) if n is not None and n > 0 else None",
         "        return int(math.floor(n)) if n is not None else None"),
        # --- 禁言令过滤（v1.10.0 事故主根因）
        ("禁言模式漏掉「不主动搭话」（最常用的那一种写法）",
         '    re.compile(r"不主动(说|发|聊|参|插|搭|回)"),',
         '    re.compile(r"不主动说"),'),
        ("命中禁言模式仍返回原文（过滤器成了装饰）",
         '        if pat.search(s):\n            return ""', '        if pat.search(s):\n            return s'),
        ("注入前只过滤 bot_role 不过滤 summary（否决票从 summary 进来）",
         "        summary=strip_hf_mute_phrases(p.summary),", "        summary=p.summary,"),
        # --- band 生效规则
        ("band 取建议值而不是与锚点取大（画像可以把阈值压低）",
         "    want = max(a, b)", "    want = b"),
        ("抬升不设上限（0.9 直接生效 ⇒ 零发言）",
         "    limited = min(want, a + m)", "    limited = want"),
        ("capped 恒 False（画像想抬到天上也没人知道）",
         "    return _clamp_band(limited, lo, hi), want - limited > 1e-9",
         "    return _clamp_band(limited, lo, hi), False"),
        ("锚点脏值回落成 NaN（所有比较为假 ⇒ 零发言）",
         "    a = float(anchor) if _is_num(anchor) else 0.6",
         '    a = float(anchor) if _is_num(anchor) else float("nan")'),
        ("maxRaise 缺省变 NaN（同上：静默零发言）",
         "    m = _nonneg(max_raise, HF_PROFILE_MAX_RAISE) if max_raise is not None else HF_PROFILE_MAX_RAISE",
         '    m = _nonneg(max_raise, HF_PROFILE_MAX_RAISE) if max_raise is not None else float("nan")'),
        # --- 渲染 / 推导 / 新旧
        ("截断预算翻倍（半句话也能挤进去）",
         "        if used + add > cap:", "        if used + add > cap * 2:"),
        ("渲染标签改名（下游按标签找字段的会全空）",
         '        lines.append(f"说话基调: {p.summary}")', '        lines.append(f"总结: {p.summary}")'),
        ("max_prompt_chars 脏值改兜底成无限（注入上限失效）",
         "    cap = max(0, int(max_chars)) if _is_num(max_chars) else 0",
         "    cap = max(0, int(max_chars)) if _is_num(max_chars) else 9999"),
        ("活跃小时不看样本门槛（30 条以下也算活跃）",
         "    if total < min_msgs:", "    if total <= 0:"),
        ("活跃小时均值写死 12 小时（口径错但仍有输出）",
         "    avg = total / 24", "    avg = total / 12"),
        ("陈旧判定 >= 改 >（恰好到点不算到期）",
         "    return now_sec - generated_at_sec >= max(1, hours) * 3600",
         "    return now_sec - generated_at_sec > max(1, hours) * 3600"),
        ("陈旧判定脏 refreshHours 兜底成天文数字（永不重生）",
         '    hours = _pos_int(refresh_hours, HF_PROFILE_DEFAULTS["refreshHours"])',
         "    hours = _pos_int(refresh_hours, 10 ** 6)"),
        # 等价变异（**故意不收，不是漏了**）：``generated_at_sec <= 0`` → ``< 0`` 只在
        # ``now_sec < 22h``（1970-01-02 之前）才与原文不同；所有真实调用点的 ``now_sec``
        # 都是当前时刻，两种写法对 ``generated_at=0`` 都返回 stale。要"杀掉"它只能造一个
        # ``now_sec=100`` 的假场景 —— 那是在为不存在的情况写测试，会把「0 是哨兵不是时间戳」
        # 这条**真实语义**记成一条假断言。故此处不收该变异。
        # --- 缓存
        ("预热不清旧群（吃到已删除/已退白名单的画像）",
         "        for k in [k for k in _profiles if k.startswith(prefix)]:", "        for k in []:"),
        ("缓存键丢掉账号（两个账号串味）",
         '    return f"{account_id}:{group_id}"', '    return f"{group_id}"'),
        ("预热时不截断（注入上限在缓存层失效）",
         '                "text": render_hf_profile_for_prompt(p, max_prompt_chars),',
         '                "text": render_hf_profile_for_prompt(p, 10 ** 6),'),
        ("坏行不再跳过（以「空画像」进缓存顶掉好画像）",
         "        if raw is None:\n            continue", "        if raw is None:\n            raw = HfGroupProfile()"),
        ("缓存 version 与落库 version 不同步",
         '            "generatedAt": generated_at, "version": version,',
         '            "generatedAt": generated_at, "version": version + 1,'),
        ("缓存不登记（跨用例污染 + 测试清不干净）",
         'CACHE_REGISTRY.register("profile", reset_hf_profile_cache)',
         'CACHE_REGISTRY.register("profileNOPE", reset_hf_profile_cache)'),
        # --- 样本
        ("样本按升序取（喂进画像的是**最老**的几条）",
         "    for m in reversed(rows):", "    for m in rows:"),
        ("样本不过滤出站（bot 自己的话进画像 ⇒ 自我强化）",
         '        if m.get("direction") != "inbound":\n            continue',
         "        if False:\n            continue"),
        ("样本单条不截断（一条长消息撑爆 prompt）",
         '        line = f"{who}: {body[:80]}"', '        line = f"{who}: {body}"'),
        ("样本发送者不遮身份（完整 wxid 外发）",
         '        who = (m.get("from_wxid") or "")[:6] + "**"',
         '        who = (m.get("from_wxid") or "")[:8] + "**"'),
        ("样本上限写死 3（长样本被腰斩）",
         "    mx = max(1, min(int(limit) if _is_num(limit) else 1, 100))",
         "    mx = max(1, min(int(limit) if _is_num(limit) else 1, 3))"),
        ("样本不去重（同一句刷屏撑满样本）",
         "        if line in seen:\n            continue", "        if False:\n            continue"),
        ("样本收下单字消息（噪声进画像素材）",
         "        if not body or len(body) < 2:", "        if not body:"),
        # --- 生成编排
        ("单轮配额失效（20 个群同时到期打 20 次 LLM）",
         "        if out.generated >= max(1, P.max_per_run):",
         "        if out.generated >= 10 ** 6:"),
        ("不再跳过还新的群（每轮都重生成）",
         "        if not is_hf_profile_stale(\n"
         '            (prev or {}).get("generated_at"), now_sec, P.refresh_hours,\n'
         "        ):",
         "        if False:"),
        ("样本门槛失效（几条消息也生成画像）",
         '            if int(stats.get("total") or 0) < P.min_msgs:',
         '            if int(stats.get("total") or 0) < 0:'),
        ("解析失败也落库（**用空/坏画像覆盖上一版**）",
         "            if p is None:", "            if p is None and False:"),
        ("落库不带样本条数（日报看不出画像基于多少样本）",
         "                sample_msgs=len(samples), model=str(model), generated_at=now_sec,",
         "                sample_msgs=0, model=str(model), generated_at=now_sec,"),
        ("落库不带生成时刻（下一轮必定又判超期）",
         "                sample_msgs=len(samples), model=str(model), generated_at=now_sec,",
         "                sample_msgs=len(samples), model=str(model), generated_at=0,"),
        ("「关掉生成」不再生效（关掉了照样打 LLM）",
         "    if not P.enabled:\n        return out", "    if False:\n        return out"),
        ("白名单过滤失效（非白名单群也生成画像）",
         "            groups = [g for g in groups if group_filter(g)]",
         "            groups = list(groups)"),
        ("活跃群不去重保序（配额给谁变成随机）",
         '        groups = list(dict.fromkeys(str(b["group_id"]) for b in buckets if b.get("group_id")))',
         '        groups = sorted({str(b["group_id"]) for b in buckets if b.get("group_id")})'),
        ("活跃群窗口按 UTC 分桶（小时口径与报告不一致）",
         "            account_id, now_sec - P.lookback_days * 86400, _local_offset_sec(now_sec),",
         "            account_id, now_sec - P.lookback_days * 86400, 0,"),
        ("活跃群窗口不是 lookbackDays（画像素材窗口与声明不符）",
         "            account_id, now_sec - P.lookback_days * 86400, _local_offset_sec(now_sec),",
         "            account_id, now_sec - 86400, _local_offset_sec(now_sec),"),
        ("生成路径不过禁言令过滤（一次抽风绕过过滤器 22h）",
         "            safe = guard_hf_profile_for_prompt(p)", "            safe = p"),
        ("预热失败也继续跑（无画像硬着头皮打 LLM）",
         '        log.warning("[WPP HF] profile cache warm failed (judge 将无画像可用): %s", exc)\n'
         "        return out",
         '        log.warning("[WPP HF] profile cache warm failed (judge 将无画像可用): %s", exc)\n'
         "        pass"),
        # --- 生成 prompt
        ("小时直方图连 0 值小时一起列（prompt 里 24 格全是噪声）",
         '        f"{h}时:{n}" for h, n in enumerate(hour_hist) if _to_num(n) and n > 0',
         '        f"{h}时:{n}" for h, n in enumerate(hour_hist)'),
        ("不再明说「只输出 JSON」（靠容忍式解析兜底变纯赌博）",
         "**只输出 JSON, 不要任何其他内容** (不要 markdown 代码块, 不要解释):",
         "**输出 JSON** (不要 markdown 代码块, 不要解释):"),
        ("硬约束 2 的抬升上限与实现不一致（+0.30 vs +0.10）",
         "(最多 +0.10)", "(最多 +0.30)"),
        ("发言最多的成员列成 wxid 而不是条数（prompt 里没有量级）",
         '    senders = ", ".join(f"{s.get(\'n\')}条" for s in senders_raw) if senders_raw else "(无)"',
         '    senders = ", ".join(f"{s.get(\'wxid\')}条" for s in senders_raw) if senders_raw else "(无)"'),
    ]),

    "learn": (HF / "learn.py", "plugin/tests/heartflow/test_learn.py", [
        # --- 缺省参数（每一个都能把闭环推向"跑飞"或"悄悄停摆"）
        ("默认自动调阈改默认开（老板没让开）", '"enabled": False,', '"enabled": True,'),
        ("默认 minSample 10 → 1（一条样本就调阈）", '"minSample": 10,', '"minSample": 1,'),
        ("默认 bandMin 0.5 → 0.2（质量底线失效）", '"bandMin": 0.5,', '"bandMin": 0.2,'),
        ("默认 bandMax 0.9 → 1.0（上限消失）", '"bandMax": 0.9,', '"bandMax": 1.0,'),
        ("默认 step 0.05 → 0.5（一步走飞）", '"step": 0.05,', '"step": 0.5,'),
        ("默认 lowEngageRate 0.15 → 0.05（几乎不判上调）",
         '"lowEngageRate": 0.15,', '"lowEngageRate": 0.05,'),
        ("默认 highEngageRate 0.5 → 0.95（几乎不判下调）",
         '"highEngageRate": 0.5,', '"highEngageRate": 0.95,'),
        ("默认冷却 4h → 0（每 5 分钟就能改一次）",
         '"minChangeCooldownSec": 4 * 3600,', '"minChangeCooldownSec": 0,'),
        ("默认呆账收敛 1800s → 0（刚判完就作废）",
         '"staleJudgedMaxSec": 1800,', '"staleJudgedMaxSec": 0,'),
        ("默认可达性最小样本 20 → 1（一条判定就下结论）",
         '"reachabilityMinSample": 20,', '"reachabilityMinSample": 1,'),
        ("默认可达性窗口 7 天 → 1 天", '"reachabilityWindowSec": 7 * 86400,',
         '"reachabilityWindowSec": 86400,'),
        ("默认可达性自愈开关默认关（护栏不自愈）",
         '"recoverUnreachable": True,', '"recoverUnreachable": False,'),
        ("默认观察窗 600s → 60s（样本窗口过窄）",
         '"observeWindowSec": 600,', '"observeWindowSec": 60,'),
        ("默认标签窗 60s → 600s", '"labelWindowSec": 60,', '"labelWindowSec": 600,'),
        ("默认本底上限 0.5 → 0（所有弱信号都不采信）",
         '"ambientMax": 0.5,', '"ambientMax": 0.0,'),
        ("默认滚窗 20 → 200 条", '"sampleWindow": 20,', '"sampleWindow": 200,'),
        ("默认 sweep 间隔 300s → 8 小时（金丝雀失明）",
         '"sweepIntervalSec": 300,', '"sweepIntervalSec": 30000,'),
        ("账号级阈值回落 0.6 → 0.9", "HF_REPLY_THRESHOLD_FALLBACK = 0.6",
         "HF_REPLY_THRESHOLD_FALLBACK = 0.9"),
        ("可达性取数上限 5000 → 50 行",
         "HF_REACH_DB_ROW_CAP = 5000", "HF_REACH_DB_ROW_CAP = 50"),
        ("占位符表少一类（漏判成真发 ⇒ 占预算+开窗）",
         '    "repeat-suppressed",\n)', ')'),
        # --- 数值助手（JS 语义的四处）
        ("round2 改成 Python round（银行家舍入：0.125→0.12）",
         "return math.floor(n * 100 + 0.5) / 100", "return round(n, 2)"),
        ("_is_num 把 bool 当数（True 混进分数）",
         "return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)",
         "return isinstance(v, (int, float)) and math.isfinite(v)"),
        ("_int_or 用 ceil（小数向上取整，门槛变松）",
         "    n = int(math.floor(v))", "    n = int(math.ceil(v))"),
        ("_int_or 丢下界（负数/0 混进配额）",
         "    return n if n >= lo else int(fallback)", "    return n"),
        ("_num_or 丢下界（负数 band 进判定）",
         "    if lo is not None and f < lo:\n        return float(fallback)\n", ""),
        ("_bool_or 用真值判断（配置文件里 \"false\" 变开）",
         "    return v if isinstance(v, bool) else bool(fallback)",
         "    return bool(v) if v is not None else bool(fallback)"),
        # --- eval_hf_threshold
        ("rate ≤ low 写成 <（恰好 0.15 的群永不动）",
         "delta = step if rate <= low else (-step if rate >= high else 0.0)",
         "delta = step if rate < low else (-step if rate >= high else 0.0)"),
        ("rate ≥ high 写成 >（恰好 0.5 的群永不动）",
         "delta = step if rate <= low else (-step if rate >= high else 0.0)",
         "delta = step if rate <= low else (-step if rate > high else 0.0)"),
        ("样本不足不判定（1 条也调阈）",
         '    if total_i < _int_or(params.get("minSample"), 0, lo=0):\n'
         '        return HfThresholdEval(False, "insufficient-sample")\n', ""),
        ("漏掉 clamped-noop（到硬界还报变更）",
         '    if raw == round2(cur):\n        return HfThresholdEval(False, "clamped-noop")\n',
         ""),
        ("cur 不收 learned（群级学习值被忽略）",
         "cur = learned_threshold if _is_num(learned_threshold) else float(base_threshold)",
         "cur = float(base_threshold)"),
        ("direction 反了（上调记成下调）",
         'direction="up" if delta > 0 else "down"', 'direction="down" if delta > 0 else "up"'),
        ("审计 reason 用 str(rate) 而不是 toFixed（0.1 打成 0.1）",
         'f"rate={_fixed(rate, 3)} total={total_i} engaged={engaged_i}"',
         'f"rate={rate} total={total_i} engaged={engaged_i}"'),
        # --- 冷却 / 钳制
        ("冷却 > 而不是 >=（恰好到点仍拒绝）",
         "return now_sec - int(last_change_at_sec) >= int(min_cooldown_sec)",
         "return now_sec - int(last_change_at_sec) > int(min_cooldown_sec)"),
        ("冷却把脏值当\"刚改过\"（静默停摆）",
         "    if not _is_num(last_change_at_sec):", "    if False:"),
        ("读侧钳制丢上界（阈值可任意高）",
         "return round2(max(band_min, min(band_max, t)))", "return round2(max(band_min, t))"),
        # --- 可达性天花板（纯）
        ("天花板丢 bandMin 下界（压进垃圾分域）",
         "cap = max(band_min, round2(max_score + step))", "cap = round2(max_score + step)"),
        ("天花板漏掉 +step（下一同类消息永远过不去）",
         "cap = max(band_min, round2(max_score + step))",
         "cap = max(band_min, round2(max_score))"),
        # ⚠️ 曾把 `if max_score >= threshold: return None` 当变异点 —— 它是**等价变异**：
        #    `cap = max(band_min, round2(max_score + step))` 满足 cap ≥ max_score ≥ threshold
        #    （step ≥ 0），所以紧随其后的 `if cap >= threshold: return None` **必然**兜住，
        #    去掉前一条守卫不改变任何可观测行为。等价变异按本仓规矩不入表（这里留档说明）。
        ("审计行不带样本量（变更无从复核）",
         "        account_id, group_id, new_threshold, sample_total, sample_engaged,\n"
         "        old_threshold=old_threshold, reason=reason)",
         "        account_id, group_id, new_threshold, 0, sample_engaged,\n"
         "        old_threshold=old_threshold, reason=reason)"),
        ("样本不足也下结论（拿 1 条判定设天花板）",
         "    if len(valid) < min_sample:\n        return None\n", ""),
        ("天花板没起作用也报告（虚假护栏）",
         "    if cap >= threshold:\n        return None\n", ""),
        # --- 发送三态
        ("占位符不再判 suppressed（漏判成真发）",
         "    if msg_id is not None and msg_id in HF_PLACEHOLDER_MSG_IDS:\n"
         "        return HF_SEND_OUTCOME_SUPPRESSED\n", ""),
        ("空 msg_id 判成 sent",
         '    if msg_id == "":\n        return HF_SEND_OUTCOME_SUPPRESSED\n', ""),
        ("ok 非真也继续（pending 变 sent）",
         "    if ok is not True:", "    if ok is not True and False:"),
        ("形状检查失效（别的发送结果不再报警）",
         '    if _send_field(result, "ok") is not None or _send_field(result, "msg_id") is not None:\n'
         "        return False",
         "    if False:\n        return False"),
        ("形状探测恒假（永远不吵）",
         '    return (_send_field(result, "success") is not None\n'
         '            or _send_field(result, "message_id") is not None)',
         "    return False"),
        # --- DB 薄管线
        ("sent 不占预算（额度永不消耗）",
         "budget.note_reply_sent(account_id, group_id, at_sec)", "pass"),
        ("sent 不开观察窗（学习样本全丢）",
         "        if with_window:\n            k = _key(account_id, group_id)",
         "        if False:\n            k = _key(account_id, group_id)"),
        ("账本行数 0 也当成功（观察窗开在空行上 / B4 静默复发）",
         '                with_window = False\n'
         '                log.warning("[WPP HF] 账本 set_sent 没匹配到行（受影响行数 0，多半是 "',
         '                log.warning("[WPP HF] 账本 set_sent 没匹配到行（受影响行数 0，多半是 "'),
        ("账本抛错也照开窗（窗永远收敛不了 / B4）",
         '            with_window = False\n'
         '            log.warning("[WPP HF] 账本 set_sent 失败，本条只记预算与复读历史 "',
         '            log.warning("[WPP HF] 账本 set_sent 失败，本条只记预算与复读历史 "'),
        ("观察窗不按 observeWindowSec（收窗时刻与声明不符）",
         "at_sec + int(observe_sec)", "at_sec + 3600"),
        ("无文本也进重复历史（拿没说的话拦下一句）",
         "        # ④ 复读历史：同上，独立一段\n        if text:\n",
         "        # ④ 复读历史：同上，独立一段\n        if True:\n"),
        ("sent 不记真发计数（金丝雀无源）", "metrics.inc_hf_sent()", "pass"),
        ("reason 映射：重复闸记成 dedup",
         '            reason = "repeat"', '            reason = "dedup"'),
        ("reason 映射：空文本记成 repeat",
         '            reason = "empty-text"', '            reason = "repeat"'),
        ("pending 也当 suppressed 落库（重试语义被吃掉）",
         "    if outcome == HF_SEND_OUTCOME_SUPPRESSED:",
         "    if outcome != HF_SEND_OUTCOME_SENT:"),
        ("收敛不传行号（重叠窗时样本翻倍）",
         "verdict.close, w.inbound_msg_id)", "verdict.close, None)"),
        ("弱信号也关窗（等不到升级）",
         "        if verdict.close:\n            _open_windows.pop(k, None)",
         "        if True:\n            _open_windows.pop(k, None)"),
        ("veto 不清窗（随后引用覆盖人工标注）",
         "    _open_windows.pop(_key(account_id, group_id), None)", "    pass"),
        ("learned 加载把字符串当数（脏列混进缓存）",
         "            if v is not None and _is_num(v):", "            if v is not None:"),
        ("预算回填把天边界当小时边界（新小时带余数开局）",
         "account_id, budget.hf_hour_start_sec(ts), budget.hf_day_start_sec(ts))",
         "account_id, budget.hf_day_start_sec(ts), budget.hf_day_start_sec(ts))"),
        ("变更不同步内存缓存（读侧读到旧值）",
         "    _learned_thresholds[_key(account_id, group_id)] = new_threshold", "    pass"),
        # --- sweep 编排
        ("步骤 1 失败后继续（未收敛的行带病往下走）",
         '        log.warning("[WPP HF] sweep close/expire failed: %s", exc)\n        return',
         '        log.warning("[WPP HF] sweep close/expire failed: %s", exc)'),
        ("心流总开关不判（关掉了照样跑全链）",
         '    if not cfg.get("enabled"):\n        return\n', ""),
        ("learning 开关不判（关了自动调阈仍调阈）",
         '    if not L["enabled"]:\n        return\n', ""),
        ("滚窗长度写死（判定口径与声明不符）",
         "repo_hf.get_closed_recent(account_id, group_id, L[\"sampleWindow\"])",
         "repo_hf.get_closed_recent(account_id, group_id, 200)"),
        ("调阈不看白名单（别群样本算进来）",
         "            if gf is not None and not gf(group_id):\n                continue\n"
         "            samples = repo_hf.get_closed_recent(",
         "            samples = repo_hf.get_closed_recent("),
        ("样本不看可鉴别性（旧标签/噪声全采信）",
         "                if not label.is_hf_sample_informative(signal, ambient_p, L[\"ambientMax\"]):",
         "                if False:"),
        ("调阈不判冷却（每轮都改）",
         '            if not hf_cooldown_ok(None if st is None else st.get("last_change_at"), ts,\n'
         '                                  L["minChangeCooldownSec"]):\n                continue\n', ""),
        ("审计 reason 漏掉 skipped/ambientP（归因没了）",
         'detail = (f"{res.reason} skipped={len(samples) - len(usable) - exempted} "',
         'detail = (f"{res.reason} "'),
        ("金丝雀从未发过报 0（停摆看起来正常）",
         "set_hf_last_send_age_sec(-1 if last is None else max(0, ts - int(last)))",
         "set_hf_last_send_age_sec(0 if last is None else max(0, ts - int(last)))"),
        ("金丝雀允许负数（时钟回拨掩盖停摆）",
         "max(0, ts - int(last))", "ts - int(last)"),
        ("本底查询失败不吭声（阈值悄悄漂）",
         'log.warning("[WPP HF] ambient baseline query failed (弱信号将不过滤): %s", exc)',
         'log.debug("[WPP HF] ambient baseline query failed: %s", exc)'),
        ("本底只留当前小时（夜里那批样本被整片判成不可采信 / B9）",
         "            out[k] = p", "            out[k[:i]] = p"),
        ("本底拿「当前小时」套全部样本（跨时段样本判错 / B9）",
         '                ambient_p = ambient.get(\n'
         '                    f"{group_id}|{budget.hf_local_hour(int(at))}", 0.0)',
         '                ambient_p = ambient.get(\n'
         '                    f"{group_id}|{budget.hf_local_hour(ts)}", 0.0)'),
        ("样本时刻缺/脏也照采信（本底查了个空键 / B9）",
         "                if isinstance(at, bool) or not isinstance(at, (int, float)) or at <= 0:\n"
         "                    continue",
         "                if False:\n                    continue"),
        # --- 占比外环
        ("占比外环算整段历史而不是今日",
         "rows = repo_messages.list_bot_msg_share(account_id, budget.hf_day_start_sec(now_sec))",
         "rows = repo_messages.list_bot_msg_share(account_id, 0)"),
        ("外环关掉了照样查库", "    if not G.enabled:\n        return 0\n", ""),
        ("超标判定恒真（冷清群也收紧）",
         '        if share > G.target_share and total >= G.min_msgs and v["outbound"] >= G.min_bot_sends:',
         "        if True:"),
        ("占比分子分母颠倒（对着人类发言量收紧）",
         'share = observe.hf_bot_share(v["inbound"], v["outbound"])',
         'share = observe.hf_bot_share(v["outbound"], v["inbound"])'),
        ("无群号的脏行进内存（影子群）",
         "        gid = r.get(\"gid\")                   # ⚠️ 数据层这一列叫 gid（COALESCE 出来的别名）\n"
         "        if not gid:\n"
         "            continue                         # 归群失败的脏行（peer_id 也空）直接丢\n",
         "        gid = r.get(\"gid\")\n"),
        # --- 可达性刷新的写侧
        ("护栏量账号级基线而不是生效阈值（永远发现不了自锁）",
         '        eff = profile.resolve_hf_profile_band_effect_for(account_id, group_id, anchor,\n'
         "                                                        cfg)[\"threshold\"]",
         "        eff = base"),
        ("够得着了也不删天花板（护栏自己成为新自锁）",
         "            if prev is not None:\n                _reach_caps.pop(k, None)\n", ""),
        ("recoverUnreachable=false 不清缓存（开关关不掉）",
         "        _drop_caps(account_id)\n        return 0", "        return 0"),
        ("告警每轮都打（journal 被淹没）",
         '        if prev is None or prev.cap != r["cap"] or now_sec - prev.at_sec >= 3600:',
         "        if True:"),
        ("天花板不落缓存（judge 读侧拿不到）",
         "        _reach_caps[k] = HfReachCap(", "        HfReachCap("),
        ("白名单外也刷天花板", "    gf = _allowed_filter(cfg)\n    capped = 0",
         "    gf = None\n    capped = 0"),
        ("样本不够也算 reachable（指标虚高）",
         '            if len(scores) >= L["reachabilityMinSample"]:', "            if True:"),
        ("无群号的判定行参与（判分组漂移）",
         '        gid = r.get("group_id")\n        if not gid:\n            continue\n',
         '        gid = r.get("group_id") or "unknown"\n'),
        ("生效阈值指标不设（自锁无金丝雀）",
         "m.set_hf_effective_threshold_max(max_eff)", "pass"),
        ("生效阈值指标用账号级算（指标撒谎）",
         "max_eff = max(max_eff, eff)", "max_eff = max(max_eff, base)"),
        ("天花板群数恒报 0（看不出护栏在起作用）",
         "m.set_hf_threshold_ceiling_groups(capped)", "m.set_hf_threshold_ceiling_groups(0)"),
        ("可达群数不设", "m.set_hf_reachable_groups(reachable)", "pass"),
        # --- 小工具
        ("信号直方图按字典序（审计口径与 TS 不一致）",
         'return ",".join(f"{k}:{n}" for k, n in m.items())',
         'return ",".join(f"{k}:{n}" for k, n in sorted(m.items()))'),
        ("信号直方图丢 unknown 桶（None 进审计文案）",
         'k = s.get("engage_signal") or "unknown"', 'k = s.get("engage_signal")'),
        ("开窗清理不按账号（别的账号的窗被清）",
         "k.startswith(prefix) and w.sent_at_sec + w.observe_window_sec <= now_sec",
         "w.sent_at_sec + w.observe_window_sec <= now_sec"),
        ("开窗清理把未到期的也清",
         "w.sent_at_sec + w.observe_window_sec <= now_sec",
         "w.sent_at_sec + w.observe_window_sec < now_sec"),
        # --- 注册 / 接线
        ("learn 缓存不登记（跨用例污染）",
         'CACHE_REGISTRY.register("learn", reset_learned_threshold_cache)',
         'CACHE_REGISTRY.register("learnNOPE", reset_learned_threshold_cache)'),
        ("重置不清分层缓存（清完还脏）", "    layer.reset_hf_layer_cache()\n", ""),
        ("重置不清重复历史（清完还脏）", "    dedupe.reset_dedupe_store()\n", ""),
        ("重置不清占比外环（清完还脏）", "    observe.reset_hf_share_guard()\n", ""),
        ("白名单降级成\"全部过滤\"（心流彻底静默）",
         "        log.debug(\"[WPP HF] heartflow 未就位 ⇒ 本轮不做白名单过滤（等价白名单为空）: %s\", exc)\n"
         "        return None",
         "        log.debug(\"[WPP HF] heartflow 未就位: %s\", exc)\n"
         "        return lambda gid: False"),
        ("_cfg_for 不取适配器（改配置要重启才生效）",
         "            acct = adapter.account(account_id)", "            acct = None"),
    ]),
    "heartflow": (HF / "heartflow.py", "plugin/tests/heartflow/test_heartflow.py", [
        # ---- 缺省参数（每个都是老板拍过板的数字） ----
        ("judge 超时 5000 → 500ms（大面积误判为「不回」）", '"timeoutMs": 5000,', '"timeoutMs": 500,'),
        ("重试次数 1 → 0（坏 JSON 直接放弃）", '"maxRetries": 1,', '"maxRetries": 0,'),
        ("上下文条数 5 → 30（prompt 挤爆 judge 输出预算）", '"contextMessagesCount": 5,',
         '"contextMessagesCount": 30,'),
        ("精力衰减 0.1 → 0.01（从不疲惫 ⇒ 越说越多）", '"energyDecayRate": 0.1,',
         '"energyDecayRate": 0.01,'),
        ("精力恢复 0.02 → 0.2（10 倍恢复 ⇒ 冷却形同虚设）", '"energyRecoveryRate": 0.02,',
         '"energyRecoveryRate": 0.2,'),
        ("相关度权重 0.25 → 0.5（维度失衡）", '"relevance": 0.25,', '"relevance": 0.5,'),
        ("看图默认关（v1.11.0 的能力被静默关掉）", 'HF_VISION_DEFAULTS: dict[str, Any] = {\n    "enabled": True,',
         'HF_VISION_DEFAULTS: dict[str, Any] = {\n    "enabled": False,'),
        ("看图张数上限 1 → 5（成本/超时失控）", '"maxImages": 1,', '"maxImages": 5,'),
        ("原始缓冲 200 → 20（上下文被截短）", "HF_RAW_BUFFER_MAX = 200", "HF_RAW_BUFFER_MAX = 20"),
        ("judge 输出预算 300 → 4096（与 TS 不一致）", "HF_JUDGE_MAX_TOKENS = 300", "HF_JUDGE_MAX_TOKENS = 4096"),
        ("待交付存活 120s → 999999s（过期永远不生效）", "HF_PENDING_SEND_TTL_SEC = 120",
         "HF_PENDING_SEND_TTL_SEC = 999999"),

        # ---- 闸门（顺序与原因串都是对外契约） ----
        ("闸门无视总开关（关着也判）", 'if not c.get("enabled"):', 'if False:'),
        ("闸门无视白名单（不在表里也判）", "if not is_hf_group_allowed(chat_id, c):", "if False:"),
        ("白名单拒绝的原因串改成 disabled（运维误判）",
         'return HfGateResult(False, "not-whitelisted")', 'return HfGateResult(False, "disabled")'),
        ("空消息判定不 strip（三个空格也去判）", 'if not (content or "").strip():', "if not content:"),
        ("冷却闸忘了「从未回复」（新群永远不响应）", "if 0 < since < min_interval:",
         "if since < min_interval:"),
        ("冷却原因串与 judge 频率对调（归因错）", 'return HfGateResult(False, "cooling")',
         'return HfGateResult(False, "judge-cooldown")'),
        ("judge 频率闸单位错（秒当毫秒 ⇒ 变 1000 倍）",
         "if last > 0 and now_ms - last < judge_interval * 1000:",
         "if last > 0 and now_ms - last < judge_interval:"),
        ("预算原因串丢弃（拿不到 budget-gap 归因）", 'reason or "budget"', '"budget"'),
        ("生效预算参数被忽略（画像收紧挡不住人）",
         "budget_override if budget_override is not None else budget.resolve_budget(c)",
         "budget.resolve_budget(c)"),
        ("候选时刻不透传（debounce 重放被当新消息）",
         "candidate_at_sec if candidate_at_sec is not None else now_sec", "now_sec"),

        # ---- 解析纯函数 ----
        ("分数只钳上界（负分进台账）", "return max(0.0, min(10.0, n))", "return min(10.0, n)"),
        ("bool 被当分数（True → 1.0 分）", "if isinstance(v, bool) or v is None:\n        return 0.0",
         "if v is None:\n        return 0.0"),
        ("坏数字串变满分（「abc」→ 10）", "except ValueError:\n            return 0.0",
         "except ValueError:\n            return 10.0"),
        ("最外层 JSON 取非贪婪（多段 JSON 里取第一个）", 're.search(r"\\{[\\s\\S]*\\}", s)',
         're.search(r"\\{[\\s\\S]*?\\}", s)'),
        ("直接解析后不校验是对象（数组直接进打分）",
         '    if isinstance(data, dict):\n        return data\n    # 剥 markdown 代码块围栏',
         '    if True:\n        return data\n    # 剥 markdown 代码块围栏'),
        ("剥围栏后不校验是对象（```[1,2]``` 变有效结果）",
         '    if isinstance(data, dict):\n        return data\n    # 取最外层',
         '    if True:\n        return data\n    # 取最外层'),

        # ---- 看图 ----
        ("只认 http（https 图全丢）", 'r"^https?://", url, flags=re.IGNORECASE',
         'r"^http://", url, flags=re.IGNORECASE'),
        ("scheme 大小写敏感（HTTP:// 被丢）", 'r"^https?://", url, flags=re.IGNORECASE',
         'r"^https?://", url'),
        ("图片不去重（同图重复计费）", "if url in seen:\n            continue", "if False:\n            continue"),
        ("图片上限 >= 改 >（多送一张）", "if len(out) >= max_images:\n            break",
         "if len(out) > max_images:\n            break"),
        ("上限为 0 不拦（关不掉看图）", "if not text or max_images <= 0:", "if not text:"),
        ("[图片] 后不跳空白（一个图都取不到）", 're.finditer(r"\\[图片\\]\\s*(\\S+)", str(text))',
         're.finditer(r"\\[图片\\](\\S+)", str(text))'),

        # ---- 精力状态机 ----
        ("每日重置 +0.2 改 +0.3（恢复速度漂移）", "st.energy = min(1.0, st.energy + 0.2)",
         "st.energy = min(1.0, st.energy + 0.3)"),
        ("每日重置不判日期（每次调用都加）", "if st.last_reset_date != today:", "if False:"),
        ("自然恢复公式丢 ×5（恢复慢 5 倍）",
         "st.energy = min(1.0, st.energy + (elapsed_ms / 60000.0) * (recovery * 5))",
         "st.energy = min(1.0, st.energy + (elapsed_ms / 60000.0) * recovery)"),
        ("P0-5 复现：恢复顺手推进上次回复时刻（冷却闸失效）", "st.last_energy_recovery_ts = now_ms",
         "st.last_reply_time = now_ms"),
        ("主动回复地板 0.1 → 0（精力可以耗尽）", "st.energy = max(0.1, st.energy - decay)",
         "st.energy = max(0.0, st.energy - decay)"),
        ("被动恢复不设上限（精力 >1）", "st.energy = min(1.0, st.energy + gain)",
         "st.energy = st.energy + gain"),
        ("恢复游标 0 也当作有效（冷启动瞬间回满）", "if st.last_energy_recovery_ts > 0:",
         "if st.last_energy_recovery_ts >= 0:"),
        ("每日重置用本地时区（重置点偏 8 小时）", "time.gmtime(now_ms / 1000.0)",
         "time.localtime(now_ms / 1000.0)"),

        # ---- prompt（I1 的另一半：格式开关） ----
        ("没有画像也注入画像段", 'if inp.group_profile else ""', 'if True else ""'),
        ("昵称空也当有（「我是 ，一个助手」）", 'if inp.bot_nickname else "默认角色：智能助手"',
         'if True else "默认角色：智能助手"'),
        ("上次回复缺失时留空（prompt 里出现空行）", 'inp.last_bot_reply or "暂无上次回复记录"',
         "inp.last_bot_reply"),
        ("从未回复被判成 0 分钟前", "if inp.seconds_since_last_reply > 0 else \"从未回复\"",
         "if inp.seconds_since_last_reply >= 0 else \"从未回复\""),
        ("精力展示取整（0.9 与 1.0 看不出差别）", "{inp.energy:.1f}/1.0", "{inp.energy:.0f}/1.0"),
        ("上下文条数硬编码 10（配置失效）",
         '_int_or(c.get("contextMessagesCount"), HF_DEFAULTS["contextMessagesCount"])', "10"),

        # ---- 打分与阈值（I11） ----
        ("缺失维度给 5 分（模型漏项被当成中等）", "dims = {k: clamp_score(data.get(k)) for k in HF_DIMENSIONS}",
         "dims = {k: clamp_score(data.get(k, 5)) for k in HF_DIMENSIONS}"),
        ("overall 用算术平均（丢掉权重）", "overall = sum(dims[k] * w[k] for k in HF_DIMENSIONS) / 10.0",
         "overall = sum(dims.values()) / len(HF_DIMENSIONS) / 10.0"),
        ("overall 不除 10（尺度与阈值差 10 倍 ⇒ 永远过）",
         "overall = sum(dims[k] * w[k] for k in HF_DIMENSIONS) / 10.0",
         "overall = sum(dims[k] * w[k] for k in HF_DIMENSIONS)"),
        ("阈值比较用严格大于（压线被拒）",
         "should_reply=overall >= L.resolve_reply_threshold(cfg)",
         "should_reply=overall > L.resolve_reply_threshold(cfg)"),
        ("reasoning 非字符串也强转（脏值进台账）",
         'raw_reason if isinstance(raw_reason, str) else ""', 'str(raw_reason or "")'),

        # ---- judge 调用 ----
        ("没配 API key 也发请求（必然 401）", 'if not getattr(creds, "api_key", ""):', "if False:"),
        ("模型缺失静默回落（v1.4.0 明令禁止的行为）",
         "if not model or not isinstance(model, str):", "if False:"),
        ("调用参数硬编码超时（账号配置失效）",
         "max_tokens=HF_JUDGE_MAX_TOKENS, timeout_ms=timeout_ms",
         "max_tokens=HF_JUDGE_MAX_TOKENS, timeout_ms=5000"),
        ("重试循环少跑一轮（maxRetries 名不副实）", "for attempt in range(max_retries + 1):",
         "for attempt in range(max_retries):"),
        ("重试序号从 0 起（文案与次数不符）", 'f"这是第{attempt + 1}次尝试',
         'f"这是第{attempt}次尝试'),
        ("首次也替换锚点（重试标记没了）", "if attempt == 0 else prompt.replace(",
         "if False else prompt.replace("),
        ("解析失败也当成功返回（把 None 当结果）",
         "if result is not None:\n                return result", "return result"),
        ("最终放弃不记警告（静默不发言）",
         'log.warning("[WPP HF] judge failed after %d attempts: %s", max_retries + 1, last_err)', ""),
        ("看图开关被忽略（关了也带图）",
         'extract_hf_image_urls(inp.content, vision["maxImages"]) if vision["enabled"] else []',
         'extract_hf_image_urls(inp.content, vision["maxImages"]) if True else []'),
        ("图片从上下文里取（看错消息）",
         'extract_hf_image_urls(inp.content, vision["maxImages"])',
         'extract_hf_image_urls(inp.recent_messages, vision["maxImages"])'),

        # ---- 编排 ----
        ("闸拒后照旧去判（预算/冷却全白设）", "if not gate.allowed:", "if False:"),
        ("judge 没出结果也落台账（与「判定未过」混淆）",
         "        if result is None:\n", "        if False:\n"),
        ("通过时不记指标（金丝雀失效）", "metrics.inc_hf_judge_passed()", "pass"),
        ("不过阈值时不记指标（金丝雀失效）", "metrics.inc_hf_judge_below()", "pass"),
        ("通过也落「未过」台账（学习样本反了）", "L.persist_hf_judged(record)",
         "L.persist_hf_judged_below_threshold(record, now_sec)"),
        ("通过不登记待交付（交付缝永远等不到）",
         "begin_pending_send(account_id, group_id, record.inbound_msg_id, now_sec)", "pass"),
        ("未过阈值也记主动回复（精力白扣）", "record_passive_message(group_id, cfg, now_ms)",
         "record_active_reply(group_id, cfg, now_ms)"),
        ("judge 没出结果就不记频率（坏 JSON 群被打爆）", "mark_heartflow_judged(group_id, now_ms)",
         "if result is not None: mark_heartflow_judged(group_id, now_ms)"),
        ("异常不吞（心流坏了整条消息失败）",
         "except Exception as exc:  # noqa: BLE001 —— 心流坏了不该让这条消息整体失败", "except SystemExit:"),
        ("content_head 截到 64 字（台账看不出说的啥）",
         're.sub(r"\\s+", " ", content).strip()[:256]', 're.sub(r"\\s+", " ", content).strip()[:64]'),
        ("昵称不进 prompt", "bot_nickname=nickname,", 'bot_nickname="",'),
        ("台账记账号级阈值而不是生效阈值（状态页开始撒谎）", "effective_threshold=eff.threshold,",
         "effective_threshold=L.resolve_reply_threshold(cfg),"),
        # ★ WppMessage **没有** new_msg_id 字段；台账此列只能从 msg.raw 取
        #   （getattr 版恒空 ⇒ 按 svrid 回查永远查不到判定过的那条）。
        ("台账 new_msg_id 退回 getattr（恒空、静默）",
         'new_msg_id=pick_new_msg_id(getattr(msg, "raw", None)),', "new_msg_id=None,"),
        ("生效阈值不喂给判定（画像/学习抬的阈值白算）",
         "        eff_cfg: Mapping[str, Any] = cfg if abs(eff.threshold - base) < 1e-9 else {\n"
         "            **cfg, \"replyThreshold\": eff.threshold}",
         "        eff_cfg: Mapping[str, Any] = cfg"),

        # ---- 生效预算 ----
        ("生效预算不并画像（画像收紧失效）",
         "        profile.get_hf_profile_budget(account_id, group_id),", "        None,"),
        ("画像静默段无视开关（默认静默生效）",
         "apply_quiet_hours=profile.resolve_hf_profile_cfg(hf_cfg).apply_quiet_hours,",
         "apply_quiet_hours=True,"),
        ("占比外环不并入（外环收紧失效）",
         "budget.tighten_budget(base, share.as_dict()) if share is not None else base", "base"),

        # ---- 交付缝 ----
        ("TTL 不生效（挂着的待交付永不过期）",
         "expires_at_sec=ts + HF_PENDING_SEND_TTL_SEC)", "expires_at_sec=ts + 99999999)"),
        ("取出时缺登记也继续（AttributeError 或错记）",
         "        p = _pending.pop(_pending_key(account_id, group_id), None)\n"
         "    if p is None:\n        return None",
         "        p = _pending.pop(_pending_key(account_id, group_id), None)\n"
         "    if False:\n        return None"),
        ("发送前缺登记也继续（AttributeError 或错记）",
         "        p = _pending.get(_pending_key(account_id, group_id))\n"
         "    if p is None:\n        return None",
         "        p = _pending.get(_pending_key(account_id, group_id))\n"
         "    if False:\n        return None"),
        ("重复闸不当「主动插话」（proactiveOnly 下永不生效）",
         "dedupe.check_hf_repeat(account_id, group_id, text, dcfg, ts, proactive=True)",
         "dedupe.check_hf_repeat(account_id, group_id, text, dcfg, ts, proactive=False)"),
        ("重复拦下时不落账（那行永远停在 judged）",
         "    take_pending_send(account_id, group_id, ts)\n    L.persist_hf_send_outcome(",
         "    L.persist_hf_send_outcome("),
        ("占位路径也开窗占额度（I5 被破坏）",
         "group_id=group_id, observe_sec=0, label_window_sec=0, text=None)",
         "group_id=group_id, observe_sec=600, label_window_sec=60, text=None)"),
        ("失败也记 sent（学习样本造假）",
         "L.HfSendResult(ok=bool(success), msg_id=(message_id or None))",
         "L.HfSendResult(ok=True, msg_id=(message_id or None))"),
        ("msg_id 不翻译（引用判定永远失败）",
         "L.HfSendResult(ok=bool(success), msg_id=(message_id or None))",
         "L.HfSendResult(ok=bool(success), msg_id=None)"),
        ("观测/标签窗不传学习参数（样本落不进窗）",
         'observe_sec=learning["observeWindowSec"], label_window_sec=learning["labelWindowSec"],',
         "observe_sec=0, label_window_sec=0,"),
        ("文本不进重复历史（重复闸永远没素材）",
         "text=text, dedupe_cfg=dedupe.resolve_dedupe_cfg(cfg))", "text=None, dedupe_cfg=None)"),
        # ---- 回复状态只认真发出（B8，2026-10-02）----
        ("judge 通过就记回复状态（没发出去也当发过 ⇒ 冷却误伤 / B8）",
         "            begin_pending_send(account_id, group_id, record.inbound_msg_id, now_sec)",
         "            record_active_reply(group_id, eff_cfg, now_ms)\n"
         "            begin_pending_send(account_id, group_id, record.inbound_msg_id, now_sec)"),
        ("真发出也不记回复状态（冷却闸永不生效 / B8）",
         "    if outcome == L.HF_SEND_OUTCOME_SENT:\n"
         "        record_active_reply(group_id, cfg, ts * 1000)",
         "    if False:\n        record_active_reply(group_id, cfg, ts * 1000)"),
        ("占位/suppressed 也记回复状态（B8 复发）",
         "    if outcome == L.HF_SEND_OUTCOME_SENT:\n        record_active_reply(",
         "    if outcome != L.HF_SEND_OUTCOME_PENDING:\n        record_active_reply("),
        # ---- 交付缝归属（B2 / B3，2026-10-02）----
        # ★ B6（2026-10-02）起这一支**不再删登记**（只放行），锚点只留判定那一行。
        ("过期登记仍当心流插话（该群此后每条出站都被重复闸卡 / B2）",
         "    if ts > p.expires_at_sec:\n", "    if False:\n"),
        ("登记后有人 @ 仍当心流插话（@ 的回复被静默丢弃 / B3）",
         "    if p.newer_mention_at_sec is not None:\n        log.info(\"[WPP HF] 登记后有更新的",
         "    if False:\n        log.info(\"[WPP HF] 登记后有更新的"),

        # ---- 旁路观测 ----
        ("心流关着也缓冲（白占内存）",
         'if not cfg.get("enabled"):\n        return\n    record_raw_message(group_id, HfRawMessage(\n        sender_name=sender_name or "未知"',
         'if False:\n        return\n    record_raw_message(group_id, HfRawMessage(\n        sender_name=sender_name or "未知"'),
        ("bot 自己的消息也当人类消息（沉默指标被自己刷新）", "    if is_bot:", "    if False:"),
        ("白名单外也记人类消息", "if is_hf_group_allowed(group_id, cfg):", "if True:"),
        ("没开窗也喂候选（每条群消息都付代价）",
         "if L.get_open_hf_window(account_id, group_id) is not None:", "if True:"),
        ("候选丢掉引用标记（强信号降级成弱信号）",
         "quotes_bot=quotes_bot, mentions_bot=mentions_bot),))",
         "quotes_bot=False, mentions_bot=mentions_bot),))"),
        ("候选丢掉 @ 标记", "quotes_bot=quotes_bot, mentions_bot=mentions_bot),))",
         "quotes_bot=quotes_bot, mentions_bot=False),))"),
        ("@/引用不打「这批出站不是它」标记（B3 判据永不置位）",
         "    if mentions_bot or quotes_bot:", "    if False:"),
        ("标记打给了错的群（别的群漏闸）",
         "        note_bot_mention(account_id, group_id)", "        pass"),
        ("空发送者不留占位（prompt 出现「[]: x」）", 'sender_name=sender_name or "未知", sender_id=""',
         'sender_name=sender_name, sender_id=""'),
        ("候选时刻用「现在」（窗内优先级判错）", 'at_sec=int(ts_sec), text=content or "",',
         'at_sec=int(now_sec), text=content or "",'),

        # ---- 缓存登记 ----
        ("缓存不登记（跨用例污染）", 'CACHE_REGISTRY.register("heartflow", reset_heartflow_cache)',
         'CACHE_REGISTRY.register("heartflowNOPE", reset_heartflow_cache)'),
        ("重置不清待交付（清完还脏）", "        _pending.clear()\n", ""),
        ("重置不清群状态（清完还脏）", "        _chat_states.clear()\n", ""),
        ("重置不清 judge 频率（清完还脏）", "        _last_judge_at.clear()\n", ""),
        ("重置不清原始缓冲（清完还脏）", "        _raw_buffers.clear()\n", ""),

        # ---- 出站记录 bot 发言（TS recordHeartflowBotReply） ----
        ("私聊也进心流缓冲", 'if "@chatroom" not in (group_id or ""):', "if False:"),
        ("心流关着也记 bot 发言（白占内存）",
         'cfg = _cfg_for(account_id)\n    if not cfg.get("enabled"):\n        return\n    record_raw_message(group_id, HfRawMessage(\n        sender_name="bot"',
         'cfg = _cfg_for(account_id)\n    if False:\n        return\n    record_raw_message(group_id, HfRawMessage(\n        sender_name="bot"'),
        ("bot 发言被当成人类发言（is_bot 丢）", 'sender_name="bot", sender_id="bot", content=text or "",\n        timestamp=float(ts_sec if ts_sec is not None else time.time()), is_bot=True))',
         'sender_name="bot", sender_id="bot", content=text or "",\n        timestamp=float(ts_sec if ts_sec is not None else time.time()), is_bot=False))'),
        ("bot 发言时间戳不取传入值（「上次回复」永远算成刚刚）", "timestamp=float(ts_sec if ts_sec is not None else time.time())",
         "timestamp=float(time.time())"),

        # ---- 交付缝：失败不消费登记 ----
        ("发送失败也把登记吃掉（重试落不了账）",
         "    if outcome == L.HF_SEND_OUTCOME_PENDING:\n", "    if False:\n"),
        ("待交付缺失不吵（事故静默）",
         "        if inflight is not None:\n            log.warning(", "        if False:\n            log.warning("),

        # ---- 候选时刻取自消息自身 ----
        ("候选时刻改取「现在」（防重放静默失效）", 'candidate_at = getattr(msg, "timestamp", None)',
         "candidate_at = None"),
    ]),
    # 适配器的四处接缝。**本机 import 不了适配器**（顶层就要 Hermes 的 ``gateway``），
    # 所以它的测试是源级的（§17）—— 但源级测试同样会烂，得用变异证明它们真的会红。
    "runtime": (HF / "runtime.py", "plugin/tests/heartflow/test_runtime.py", [
        ("预热少调一条（分层统计永远缺席）",
         '            ("layer", "分层统计", hf_layer.load_hf_layer_stats),\n', ""),
        ("预热顺序调换（与 TS 认位对不上）",
         '            ("learned", "learned 阈值", hf_learn.load_learned_thresholds),\n'
         '            ("budget", "发言预算计数", hf_learn.load_hf_budget_seed),',
         '            ("budget", "发言预算计数", hf_learn.load_hf_budget_seed),\n'
         '            ("learned", "learned 阈值", hf_learn.load_learned_thresholds),'),
        ("预热不传账号（把 A 的缓存灌成 B 的）",
         "            n = int(fn(account_id))", '            n = int(fn(""))'),
        ("预热返回值丢掉（看不出到底灌没灌进东西）",
         "        out[key] = n\n        if n > 0:", "        if n > 0:"),
        ("0 条也吵（重启常态刷屏）",
         "        if n > 0:\n            log.info(", "        if True:\n            log.info("),
        ("单条失败直接抛出（预热反过来挡住账号接入）",
         '        except Exception as exc:  # noqa: BLE001 —— 单条失败降级，见 docstring\n'
         "            n = 0\n"
         '            log.warning("[WPP HF] 启动预热失败（%s）: account=%s %s", label, account_id, exc)',
         "        except Exception as exc:  # noqa: BLE001\n            raise"),
        ("单条失败不记警（静默少一份缓存）",
         '            log.warning("[WPP HF] 启动预热失败（%s）: account=%s %s", label, account_id, exc)',
         "            pass"),
        ("心流未就位就崩（F5：预热成了接入阻塞点）",
         '    except Exception as exc:  # noqa: BLE001 —— 未就位 / 少了函数都算"没有可预热的"\n'
         '        log.debug("[WPP HF] 启动预热取不到加载函数（心流未就位？）: %s", exc)\n'
         "        return []",
         "    except ImportError:\n        raise\n    return []"),
        ("预热路径里混进画像（画像缓存出现第二个持有者）",
         "    out: dict[str, int] = {}\n    for key, label, fn in _preload_steps():",
         "    from . import profile as _unused_profile\n"
         "    out: dict[str, int] = {}\n    for key, label, fn in _preload_steps():"),
        # 缓存登记表三处（测试的第五块）。第三条是为了证明"集合相等"这条断言
        # **不是空对空** —— 两份名单任一变化它都会红。
        ("只写不读的全局配置单例又回来（配置有了第二真源，TS 那次 7 个测试失败的形状）",
         "CACHE_REGISTRY = HfCacheRegistry()",
         "CACHE_REGISTRY = HfCacheRegistry()\n\n_runtime: dict = {}\n\n\n"
         "def get_runtime():\n    return _runtime"),
        ("清一个未登记的缓存名静默放过（「清干净了」变成假象）",
         '        if fn is None:\n            raise KeyError(f"未登记的心流缓存: {name!r}（已登记: {sorted(self._resets)}）")',
         "        if fn is None:\n            return"),
        ("登记表少报一个名字（账本与实现不符，跨用例污染看不出是谁）",
         "            return sorted(self._resets)",
         "            return sorted(self._resets)[1:]"),
    ]),

    "adapter-seams": (ADAPTER, "plugin/tests/heartflow/test_heartflow.py", [
        ("发送前置被摘掉（重复闸永不生效）",
         "plan = self._hf_pre_send(account_id, peer_id, content or \"\")", "plan = None"),
        ("占位短路被摘掉（拦下了照样真发，I5 破）",
         "if plan is not None and plan.placeholder:", "if False:"),
        ("占位返回失败（runner 会重试同一条插话直到天亮）",
         "return SendResult(success=True, message_id=None,\n"
         "                                  raw_response={\"hfSuppressed\": plan.placeholder})",
         "return SendResult(success=False, error=\"hf-suppressed\")"),
        ("发成功也不记交付结果（账本永远 pending）",
         "self._hf_note_sent(account_id, peer_id, content or \"\", sent)", "pass"),
        ("bot 发言不进缓冲（judge 看不到「我上次说了什么」）",
         "self._hf_note_bot_reply(account_id, peer_id, content or \"\")", "pass"),
        ("旁路观测整个被摘掉（judge 的上下文只剩自己）",
         "if msg.is_group:\n            self._hf_note_group(acct, msg, is_bot=False)\n", ""),
        ("旁路观测把人类消息标成 bot（预算的沉默指标被自己刷新）",
         "is_bot=is_bot,", "is_bot=True,"),
        # 兜底那条只能靠「整块摘掉」来表达：单纯把 ``try:`` 换成 ``if True:``
        # 会留下悬空的 ``except`` ⇒ 收集期 SyntaxError，变异自己就崩了（BROKEN 不算杀）。
        ("旁路观测没有兜底（心流一坏就收不了信）",
         '        try:\n            hf.note_bot_reply(account_id, peer_id, text)\n'
         '        except Exception as exc:  # noqa: BLE001\n'
         '            log.debug("[WPP:%s] 心流 bot 发言记录失败（不影响发送）: %s", account_id, exc)',
         "        hf.note_bot_reply(account_id, peer_id, text)"),
        ("自消息也入站进缓冲（同一句在缓冲里两份）",
         'log.debug("[WPP:%s] 自家消息，丢弃 %s", acct.id, msg.msg_id)',
         "self._hf_note_group(acct, msg, is_bot=True)"),
        # 丢弃门（A8）：判据本体在 inbound.triggers（那边有真用例），
        # 这里守**接线** —— 摘掉/写错参数，判据再对也是空的。
        ("★ 丢弃门没接线（deny 全落到「交心流」那条路上 ⇒ 撤回/黑名单/公众号消息进 LLM）",
         "        if should_drop(ok, reason):",
         "        if False:"),
        ("丢弃门参数漂了（传空 reason ⇒ 什么都判成「不丢」）",
         "        if should_drop(ok, reason):",
         '        if should_drop(ok, ""):'),
        # 启动预热三条（见 runtime.py 的 preheat_account）：实现了却没接线的那种静默失效。
        ("启动预热被摘掉（重启后 300s 内缓存全是冷的）",
         "            try:\n                hf_runtime.preheat_account(acct.id)\n"
         '            except Exception as exc:  # noqa: BLE001\n'
         '                log.warning("[WPP:%s] 心流启动预热失败（不影响接入）: %s", acct.id, exc)\n',
         "            pass\n"),
        ("预热没有第二道网（它一抛，sweep 任务就一起没了）",
         "            try:\n                hf_runtime.preheat_account(acct.id)\n"
         '            except Exception as exc:  # noqa: BLE001\n'
         '                log.warning("[WPP:%s] 心流启动预热失败（不影响接入）: %s", acct.id, exc)',
         "            hf_runtime.preheat_account(acct.id)"),
        ("预热挪到起 sweep 之后（首条消息仍可能撞上冷缓存）",
         "            try:\n                hf_runtime.preheat_account(acct.id)\n"
         '            except Exception as exc:  # noqa: BLE001\n'
         '                log.warning("[WPP:%s] 心流启动预热失败（不影响接入）: %s", acct.id, exc)\n'
         "            try:\n                task = asyncio.create_task(\n"
         "                    hf_runtime.sweep_loop(self, acct.id, interval=300),\n"
         '                    name=f"wpp-hf-sweep-{acct.id}")\n'
         "                self._sweep_timers.append(task)\n"
         '            except Exception as exc:  # noqa: BLE001\n'
         '                log.warning("[WPP:%s] sweep 启动失败: %s", acct.id, exc)',
         "            try:\n                task = asyncio.create_task(\n"
         "                    hf_runtime.sweep_loop(self, acct.id, interval=300),\n"
         '                    name=f"wpp-hf-sweep-{acct.id}")\n'
         "                self._sweep_timers.append(task)\n"
         '            except Exception as exc:  # noqa: BLE001\n'
         '                log.warning("[WPP:%s] sweep 启动失败: %s", acct.id, exc)\n'
         "            try:\n                hf_runtime.preheat_account(acct.id)\n"
         '            except Exception as exc:  # noqa: BLE001\n'
         '                log.warning("[WPP:%s] 心流启动预热失败（不影响接入）: %s", acct.id, exc)'),
    ]),

    # ------------------------------------------------- 准入策略上报（Hermes 授权面）
    #
    # 2026-10-01 真机实测的故障形态：插件判过了、网关**默认全拒**把回复拦下，
    # 插件侧日志全绿（`触发（…）` 照打、台账照落 `status='judged'`），
    # 现场只有一行 `WARNING gateway.run: Unauthorized user: <wxid>`。
    # 所以这两套守的不是"判得对不对"，而是**上报有没有被拆掉**——
    # 拆掉的后果是"所有微信用户被静默拒绝"，且从插件视角看不出来。
    "access-policy": (ROOT / "plugin" / "core" / "access_policy.py",
                      "plugin/tests/platform/test_access_policy.py", [
        ("插件侧 allowlist 被映射成 open（真白名单上报成「没配」⇒ 网关 default-deny 全拒）",
         '"allowlist": ALLOWLIST,', '"allowlist": OPEN,'),
        ("上报集不再按已连接过滤（把 default.json 的生产群白名单一起报上去）",
         'picked = [a for a in accounts if str(getattr(a, "id", "")) in live]',
         'picked = list(accounts)'),
        ("排序被摘掉（多账号下上报顺序随 dict 插入序抖动）",
         'return tuple(sorted(picked, key=lambda a: str(getattr(a, "id", ""))))',
         'return tuple(picked)'),
        ("空账号集报 allowlist（还没连上任何账号就先开门）",
         'got = {_one_group_policy(a) for a in accounts}\n    if not got:\n        return DISABLED',
         'got = {_one_group_policy(a) for a in accounts}\n    if not got:\n        return ALLOWLIST'),
        ("群 allowlist 被降成 open（真白名单不往上报）",
         '    if ALLOWLIST in got:\n        return ALLOWLIST\n    if got == {DISABLED}:',
         '    if ALLOWLIST in got:\n        return OPEN\n    if got == {DISABLED}:'),
        ("私聊：白名单为空也报 allowlist（网关挨个问全是 False，报出去的话是假的）",
         'return ALLOWLIST if allowed_dm_senders((acct,)) else DISABLED',
         'return ALLOWLIST\n    return DISABLED'),
        ("allowAllUsers 改真值判断（字符串 true 也算开 ⇒ 替配置错误开门）",
         'raw.get("allowAllUsers") is True', 'raw.get("allowAllUsers")'),
        ("私聊 open 不再优先上报（网关失去 default-deny 的判据）",
         '    if OPEN in got:\n        return OPEN', '    if OPEN in got:\n        return DISABLED'),
        ("私聊发件人判定恒真（fail-closed 兜底被拆）",
         'return sid in allowed_dm_senders(accounts)', 'return True'),
    ]),

    "adapter-access-policy": (ADAPTER, "plugin/tests/platform/test_access_policy.py", [
        ("★ 整个 enforces_own_access_policy 被删（网关不再回头问插件 ⇒ 默认全拒）",
         '    @property\n'
         '    def enforces_own_access_policy(self) -> bool:\n'
         '        """声明"准入由插件按各账号配置自己把关" —— 网关据此才会回头问下面几个。"""\n'
         '        return True\n\n', ''),
        ("开关报 False（网关当插件不把关，回到 default-deny）",
         '        """声明"准入由插件按各账号配置自己把关" —— 网关据此才会回头问下面几个。"""\n'
         '        return True',
         '        """声明"准入由插件按各账号配置自己把关" —— 网关据此才会回头问下面几个。"""\n'
         '        return False'),
        ("@property 被摘（中心授权 getattr 到方法对象 ⇒ 既不是 allowlist 也不是 open）",
         '    @property\n    def _group_policy(self) -> str:', '    def _group_policy(self) -> str:'),
        ("群策略错接成私聊策略（口径串线）",
         '        return access_policy.group_policy(self._gateway_accounts)',
         '        return access_policy.dm_policy(self._gateway_accounts)'),
        ("私聊策略错接成群策略（口径串线）",
         '        return access_policy.dm_policy(self._gateway_accounts)',
         '        return access_policy.group_policy(self._gateway_accounts)'),
        ("上报集改回全量 _accounts（连不上的生产形状账号也一起上报）",
         'return access_policy.reportable(self._accounts.values(), self._clients.keys())',
         'return access_policy.reportable(self._accounts.values(), self._accounts.keys())'),
        ("私聊发件人判定恒真（谁都能私聊机器人）",
         '        return access_policy.is_dm_allowed(self._gateway_accounts, sender_id)',
         '        return True'),
    ]),

    # ------------------------------------------------- 发送结果解析（Code/ret/逐条 Ret/取 id）
    #
    # 这两套守的是**发送判据**。它们此前在本树**一行都没有**，而失效形态是
    # 最贵的那种"全绿但不对"：把 vendor 的**拒收**当成功（→ 幽灵出站记录 +
    # 台账写 sent + 白消费一次发言预算），以及取不到 id 导致 bot_msg_id 恒 NULL
    # （静默降级，一句日志都没有）。蓝本对应 v1.5.6 SEND-LIST-RET / v1.3.57 P1-2。
    "outbound-send-result": (ROOT / "plugin" / "platform" / "outbound.py",
                             "plugin/tests/platform/test_outbound_send_result.py", [
        ("ret 判据恒真（Code=0 + ret≠0 也算成功 —— 又回到只看 HTTP 层）",
         '    return value in (0, "0", None)', '    return True'),
        ("逐条 Ret 闸被摘掉（v1.5.6 的原样故障：List[0].Ret=-2 判成功）",
         '    lst = data.get("List")\n    if isinstance(lst, (list, tuple)) and lst:',
         '    lst = None\n    if False:'),
        ("有 List 时仍去看 BaseResponse（两条判据互相打架 = 等于没有判据）",
         '        return True\n    br = data.get("BaseResponse")',
         '        pass\n    br = data.get("BaseResponse")'),
        ("不看 List[0]（v1.3.57 的原样故障：id 只在 List 里 ⇒ 恒取不到）",
         'head = lst[0] if isinstance(lst, (list, tuple)) and lst and isinstance(lst[0], dict) else {}',
         'head = {}'),
        ("clientMsgId 与 newMsgId 优先级写反（蓝本是 msgId = clientMsgId ?? newMsgId）",
         'return OutboundMsgIds(msg_id=client_id or new_id, new_msg_id=new_id, client_msg_id=client_id)',
         'return OutboundMsgIds(msg_id=new_id or client_id, new_msg_id=new_id, client_msg_id=client_id)'),
        ("clientMsgId 那一整条链断掉（只剩 List[0].NewMsgId）",
         'client_id = _first_str(head.get("ClientMsgid"), head.get("MsgId"),',
         'client_id = _first_str('),
        ("取候选改 is not None（NewMsgId: 0 会顶掉后面那个真候选）",
         '        if not c:', '        if c is None:'),
        ("丢掉旧实现认的 Newmsgid 小写 m（老响应取不到 id）",
         'data.get("NewMsgId"), data.get("Newmsgid"),', 'data.get("NewMsgId"),'),
    ]),

    "adapter-outbound-ids": (ADAPTER, "plugin/tests/platform/test_outbound_send_result.py", [
        ("退回只取单值的 extract_msg_id（new_msg_id 永远丢）",
         'ids = outbound.extract_outbound_msg_ids(resp)',
         'ids = outbound.OutboundMsgIds(outbound.extract_msg_id(resp), None, None)'),
        ("message_id 改成报 new_msg_id（心流台账 bot_msg_id 的取值口径变了）",
         'return SendResult(success=True, message_id=ids.msg_id, raw_response=resp)',
         'return SendResult(success=True, message_id=ids.new_msg_id, raw_response=resp)'),
        ("落库不写 new_msg_id（群友引用 bot 消息的关联断掉）",
         '                new_msg_id=ids.new_msg_id,\n', ''),
        ("落库的 msg_id 改成客户端 id 优先（蓝本是 newMsgId ?? msgId）",
         'msg_id=ids.new_msg_id or ids.msg_id or None,', 'msg_id=ids.msg_id or None,'),
        ("只把 msg_id 传给落库（new_msg_id 丢在门外）",
         'await self._persist_outbound(acct, peer_id, chunk, ids)',
         'await self._persist_outbound(acct, peer_id, chunk, ids.msg_id)'),
    ]),

    # 出站 @self 闸（2026-10-02 真机现场：模型把上下文里的「@本账号昵称」抄进回复
    # ⇒ 账号在群里 @ 了自己）。失效形态是"群里出现自己 @ 自己"，日志干净、没有异常。
    "outbound-self-mention": (ROOT / "plugin" / "platform" / "outbound.py",
                              "plugin/tests/platform/test_outbound_self_mention.py", [
        ("判据整个空转（原样返回 ⇒ 现场那条照发）",
         '    from ..inbound.parser import normalize_mention\n\n'
         '    return normalize_mention(content, self_wxid or "", self_nickname or "")',
         '    return content'),
        ("不拿身份去摘（传空身份 = 谁也不摘，等于没接）",
         'normalize_mention(content, self_wxid or "", self_nickname or "")',
         'normalize_mention(content, "", "")'),
    ]),

    "adapter-outbound-self-mention": (ADAPTER,
                                      "plugin/tests/platform/test_outbound_self_mention.py", [
        ("live send 没接闸（真机那条回复还会 @ 自己）",
         'cleaned = outbound.strip_self_mention(content or "", acct.self_wxid, acct.nickname)',
         'cleaned = content or ""'),
        ("摘掉 @ 之后不写回 content（算了但没生效）",
         '                content = cleaned\n', ''),
        ("cron/standalone 那条出口绕过闸",
         'message = outbound.strip_self_mention(message or "", acct.self_wxid, acct.nickname)',
         'message = message or ""'),
    ]),

    # ------------------------------------------------------------------ 不变量守卫
    #
    # 这一组的目标不是"行为"，而是**证明不变量测试真的会红**。
    # 不变量条款的失效形态全是静默的（测试全绿、日志干净、指标正常），所以它们的测试
    # 本身最需要被证明"咬得住" —— 一条写松了的不变量断言，等于没有这条不变量。
    # 六个模块各取最要命的一条：I1/I9/I11 · I5/I11 · I3 · I6 · I7/I8 · I10。
    "inv-heartflow": (HF / "heartflow.py", INVARIANTS_TEST, [
        ("prompt 里塞进阈值数字（09-26 自锁停摆的原样）",
         "你是群聊机器人的决策系统，需要判断是否应该主动回复以下消息。",
         "你是群聊机器人的决策系统，当前回复阈值 {resolve_reply_threshold(c)}，"
         "需要判断是否应该主动回复以下消息。"),
        ("看图改成从上下文里抠（焦点糊掉 + 上下文图也发给厂商）",
         'extract_hf_image_urls(inp.content, vision["maxImages"])',
         'extract_hf_image_urls(inp.chat_context, vision["maxImages"])'),
        ("overall 不再除以 10（尺度变 0–10 ⇒ 阈值恒过）",
         "overall = sum(dims[k] * w[k] for k in HF_DIMENSIONS) / 10.0",
         "overall = sum(dims[k] * w[k] for k in HF_DIMENSIONS)"),
    ]),
    "inv-learn": (HF / "learn.py", INVARIANTS_TEST, [
        ("pending 也走 sent 分支（没发出去也占预算/开窗/进历史）",
         "if outcome == HF_SEND_OUTCOME_SENT:",
         "if outcome != HF_SEND_OUTCOME_SUPPRESSED:"),
        ("阈值读取侧去掉上界（2.0 ⇒ 恒不过线，静默零发言）",
         'return _num_or((cfg or {}).get("replyThreshold"), HF_REPLY_THRESHOLD_FALLBACK,\n'
         '                   lo=0.0, hi=1.0)',
         'return _num_or((cfg or {}).get("replyThreshold"), HF_REPLY_THRESHOLD_FALLBACK,\n'
         '                   lo=0.0)'),
        ("真发不记重复历史（下一句会被「没说出口的话」拦）",
         "        # ④ 复读历史：同上，独立一段\n        if text:\n",
         "        # ④ 复读历史：同上，独立一段\n        if False:\n"),
    ]),
    "inv-layer": (HF / "layer.py", INVARIANTS_TEST, [
        ("聚合顺手写模块级缓存（「重算」与「增量」给出两套段统计）",
         "    acc: dict[str, dict[str, list[int]]] = {}",
         '    acc: dict[str, dict[str, list[int]]] = {}\n    _layer_stats["__mutant__"] = {}'),
        ("旧错误标签（无信号列）当弱信号灌进统计",
         "        if signal is None:\n            continue",
         '        if signal is None:\n            signal = "silence"'),
    ]),
    "inv-repo": (HF.parent.parent / "data" / "repo_hf.py", INVARIANTS_TEST, [
        ("最后发言改用 status='sent'（sweep 关窗后恒 0 —— 09-11 假 0）",
         '"WHERE account_id = %s AND sent_at IS NOT NULL",',
         '"WHERE account_id = %s AND status = \'sent\'",'),
    ]),
    "inv-profile": (HF / "profile.py", INVARIANTS_TEST, [
        ("band 抬升去掉上界（画像想说 0.85 就是硬地板）",
         "    limited = min(want, a + m)",
         "    limited = want"),
        ("禁言令模式表少一条（「保持沉默」重新变成否决票）",
         '    re.compile(r"禁言|保持沉默|尽量沉默|闭嘴"),\n',
         ""),
    ]),
    "inv-judge": (HF.parent.parent / "llm" / "judge.py", INVARIANTS_TEST, [
        ("空正文静默返回空串（09-11 静默瘫 3 天的形态）",
         'if reason == "length":\n'
         '            msg += " — 输出被 max_tokens 截断，思考未关？见 JudgeCreds.no_think"\n'
         "        raise JudgeError(msg)",
         'if reason == "length":\n'
         '            msg += " — 输出被 max_tokens 截断，思考未关？见 JudgeCreds.no_think"\n'
         '        return ""'),
    ]),

    # ------------------------------------------------- 留痕 / 可观测（test_trace.py）
    #
    # 这一组守的是**"判了之后运维面上看不看得见"**（09-11 静默瘫 3 天的真正根因是留痕，
    # 不是判定）。它们的失效形态同样是静默的：台账照写、日志干净，只是"judge 跑了但没过"
    # 与"群里没人说话"又变成一回事。
    "trace-db": (HF.parent.parent / "data" / "repo_hf.py",
                 "plugin/tests/heartflow/test_trace.py", [
        ("学习样本漏掉 engaged IS NOT NULL（suppressed 行涌进样本 ⇒ 接话率被稀释到 0）",
         "AND group_id = %s AND status = 'closed' AND engaged IS NOT NULL",
         "AND group_id = %s AND status = 'closed'"),
        ("suppress 的 guard 去掉 status='judged'（已发出去的行会被改写成「没发」）",
         "WHERE account_id = %s AND inbound_msg_id = %s AND status = 'judged'",
         "WHERE account_id = %s AND inbound_msg_id = %s"),
        ("台账计数只按 status 聚合（分不清沉默原因 ⇒ 运维面一锅汤）",
         "GROUP BY status, suppressed_reason",
         "GROUP BY status"),
    ]),
    # ------------------------------------------------- 入站判定（test_hongbao_relay.py / test_triggers.py）
    #
    # 这一组守着本仓**历次事故里最贵的一类失效**：静默吞消息。它的形态特别难查 ——
    # 不报错、不写日志标签，唯一痕迹是一行看着完全合理的 "url=missing"，09-27 的接龙
    # 就是这么被吞了整整一晚上。所以这里的每条"口径"都要能被变异杀出来。
    "hongbao": (ROOT / "plugin" / "inbound" / "hongbao.py",
                "plugin/tests/test_hongbao_relay.py", [
        ("接龙豁免整条删掉（文案自带「红包」的接龙又被吞回红包判定）",
         "    if is_relay_message(msg):",
         "    if False:"),
        ("关键词判定退回裸关键词（纯文本里出现「红包」= 吞掉群友的话）",
         "if _HB_KEYWORD_RE.search(content) and is_card_message(msg):",
         "if _HB_KEYWORD_RE.search(content):"),
        ("微信原生红包 appMsg.type=2002 分支删掉（真红包漏判）",
         '    if str(app_msg.get("type") or "") == str(WX_RED_PACKET_APPMSG_TYPE):',
         "    if False:"),
        ("description 兜底移出 category 判断（**静默面被悄悄放宽** —— 无 category 的转账也被吞）",
         "    if isinstance(category, str):\n"
         "        if _PAY_CATEGORY_RE.search(category):\n"
         "            return True\n"
         "        desc = app.get(\"description\")\n"
         "        if isinstance(desc, str) and _PAY_DESC_RE.search(desc):\n"
         "            return True",
         "    desc = app.get(\"description\")\n"
         "    if isinstance(desc, str) and _PAY_DESC_RE.search(desc):\n"
         "        return True\n"
         "    if isinstance(category, str):\n"
         "        if _PAY_CATEGORY_RE.search(category):\n"
         "            return True"),
    ]),
    "relay": (ROOT / "plugin" / "inbound" / "relay.py",
              "plugin/tests/test_hongbao_relay.py", [
        ("卡片类型改回写死的 49（符号收口被拆掉 ⇒ 两处对 49 的理解迟早分叉）",
         "if msg_type != MT_APP:",
         "if msg_type != 49:"),
        ("旧 chat-history(53) 兼容分支删掉（历史接龙全漏）",
         "    if msg_type == 53:\n        return True",
         "    if False:\n        return True"),
        ("编号条目判据放宽成「只要含接龙」（任何提到接龙的话都成接龙卡片）",
         '    return "接龙" in text and bool(_NUMBERED_ITEM_RE.search(text))',
         '    return "接龙" in text'),
    ]),

    # v1 / business-callback 两种推送形态的字段名兜底（test_parser_v1.py，A1/A2/A3/A5）。
    #
    # 这一套守的是**"字段名对不上"这一类静默失效**：不会抛异常，只会"当成别的东西"，
    # 日志看着完全正常 —— 类型恒文本（媒体/卡片/接龙/红包的判据跟着一起错）、
    # 群消息当私聊（群策略/群白名单/群预算整条绕开）、无 id 的消息共用空去重键
    # （第一条之后全被判重复、静默吞掉）。三条的共同点是"**只在真 vendor 推送到来时**
    # 才现形"，所以每条判据都必须有变异门钉住，否则修完就会在下一次重构里悄悄掉。
    "parser-v1": (ROOT / "plugin" / "inbound" / "parser.py",
                  "plugin/tests/test_parser_v1.py", [
        ("v1 的 type 字段不认（A1 回归：v1 图片/语音/文件全判成文本）",
         '           or src.get("type") or gf.get("type") or MT_TEXT)',
         '           or MT_TEXT)'),
        ("v1 的 type 压过老字段（别的 payload 里含义不同的 type 抢了 MsgType）",
         '    raw = (src.get("msgType") or src.get("MsgType") or src.get("msg_type")\n'
         '           or gf.get("msgType") or gf.get("MsgType")\n'
         '           or src.get("type") or gf.get("type") or MT_TEXT)',
         '    raw = (src.get("type") or gf.get("type")\n'
         '           or src.get("msgType") or src.get("MsgType") or src.get("msg_type")\n'
         '           or gf.get("msgType") or gf.get("MsgType") or MT_TEXT)'),
        ("消息类型不解 {\"string\": …} 包装（旧格式的 MsgType 恒判文本）",
         '        return int(_unwrap(raw))      # business callback 的 {"string": "49"} 也要认',
         '        return int(raw)'),
        ("不加 {\"string\": …} 包装的 dict 当值传下去（造出不是 wxid 的 wxid）",
         "    if isinstance(v, (dict, list)):",
         "    if False:"),
        ("_safe_str 不解包装（旧格式的 FromUserName 变成 dict 字面量 ⇒ 整条静默消失）",
         "    v = _unwrap(v)",
         "    v = v"),
        ("群 id 不看 @chatroom 后缀（A2 回归：v1 群消息当私聊，群策略整条绕开）",
         '            if cand.endswith("@chatroom"):',
         "            if False:"),
        ("旧格式的群 id 候选（FromUserName/ToUserName）不看（business 回调的群消息当私聊）",
         '        for cand in (conversation_id, recipient_id,\n'
         '                     _safe_str(src.get("FromUserName")), _safe_str(src.get("ToUserName")),\n'
         '                     from_wxid):',
         '        for cand in (conversation_id, recipient_id,\n'
         '                     from_wxid):'),
        ("is_group 的兜底分支删掉（非 @chatroom 形态的群 id 丢了）",
         "    if not chatroom_id and is_group_flag:",
         "    if False:"),
        ("is_group 用裸真值判（字符串 \"false\" 把私聊升级成群）",
         '    is_group_flag = v1_is_group is True or v1_is_group == "true" or v1_is_group == 1',
         "    is_group_flag = bool(v1_is_group)"),
        ("无 id 不合成（A3 回归：第一条之后所有无 id 消息共用一个去重键、静默丢弃）",
         '    msg_id = raw_msg_id or f"{timestamp}-{secrets.token_hex(4)}"',
         "    msg_id = raw_msg_id"),
        ("合成 id 每次相同（同一秒内的无 id 消息互相去重）",
         'f"{timestamp}-{secrets.token_hex(4)}"',
         'f"{timestamp}-c0ffee"'),
        ("垃圾 payload 的早退删掉（没有发件人也没有 id 的推送也进管线）",
         "    if not from_wxid and not raw_msg_id:",
         "    if False:"),
        # 2026-10-18 真机：49 的引用回复被标成 ``[文件]``，模型回「你发的是卡片/文件类
        # 消息，我这边只读到文字」。这条变异把引用细分摘掉，正文就会回到兜底。
        ("★ 引用回复又掉回 [文件] 兜底（一句正常回复被模型当成附件）",
         '        if msg.reply_to or extract_quote_ref(raw):\n            return ""',
         '        if False:\n            return ""'),
        # -- envelope 那一层（A5 端到端）：单点 parse 全对，也架不住消息**根本捞不出来**。
        ("★ 旧格式 business 回调的 AddMsgs 分支不看（整条 envelope 捞不出消息 ⇒ 静默零入站）",
         '        if not out and payload.get("EventType") == "sync_message":',
         "        if False:"),
        ("AddMsgs 里非 dict 的项也塞进去（parse 层拿到垃圾 ⇒ 抛异常被吞、消息没了）",
         "                    out += [x for x in add_msgs if isinstance(x, dict)]",
         "                    out += list(add_msgs)"),
        ("EventType 不看（任何推送都往 AddMsgs 里掏）",
         '        if not out and payload.get("EventType") == "sync_message":',
         "        if not out:"),
        # A9：TS 放行 outgoing 图片（为了让**真实 server id** 落进 wpp_messages），
        # 本树不落入站 ⇒ **没有消费者**，所以这里只钉"方向必须照实透传"这一半。
        # （不去变异"照抄 TS 的放行"：那在本树里不可达、伤害为零，为它造一条测试
        #   就是给假分叉盖合格章。理由写在 ``parser.parse_message`` 的 A9 注释里。）
        ("direction 写死成 incoming（TS 靠它定落库方向；写死 = 把少数正确的那半截也拆了）",
         '        direction=_safe_str(src.get("direction")),',
         '        direction="incoming",'),
    ]),

    # 入站判定的"闸门"部分（test_triggers.py）：每条失效形态都是**静默**的 ——
    # 要么吞了该回的（filehelper 命令没反应 / 接龙零回复），要么放行了不该回的
    # （空白名单放行全部群 ⇒ P0 那次 fan-out）。它们不会报错，只能靠门钉住。
    "triggers": (ROOT / "plugin" / "inbound" / "triggers.py",
                 "plugin/tests/test_triggers.py", [
        ("红包闸门删掉（真红包会进 AI，且接龙测试同时变红 ⇒ 两道都失控）",
         '    if is_red_packet_message(msg):\n        return False, "red_packet"',
         '    if False:\n        return False, "red_packet"'),
        ("filehelper 判定删掉（P1-2.3：命令永远被 self 门拦死，症状是「发命令没反应」）",
         '    if is_filehelper_session(msg):\n        return True, "filehelper"',
         '    if False:\n        return True, "filehelper"'),
        ("群 allowlist 整个不生效（P0-2：白名单外照样触发）",
         "        if not allow or chatroom_id not in allow:",
         "        if False:"),
        ("接龙判据退回裸子串「接龙」（A4 回归：群里顺口聊接龙也 100% 必回，还吃预算）",
         '    if "接龙" not in content and "[接龙]" not in content:\n        return False\n',
         '    return "接龙" in content\n'),
        ("接龙判据改成恒 False（100% 必回这一路静默失效）",
         "    if is_relay_message(msg):",
         "    if False:"),
        ("接龙硬标记 #接龙 那条路删掉（纯文本形态的真接龙不再回复）",
         '    if "#接龙" in content:\n        return True\n',
         ""),
        ("接龙卡片判据退化成「提到接龙就算」（09-01 教训：按类型单判会把转账当接龙）",
         "        return int(msg.msg_type) in (MT_APP, MT_CHAT_HISTORY)",
         "        return True"),
        ("私聊非白名单也放行（P0 联系人 fan-out 的形态）",
         '        return (True, "dm") if msg.from_wxid in allow_from else (False, "dm_not_allowlisted")',
         '        return True, "dm"'),
        ("关键词路恒不命中（配了关键词也不触发，日志上只看到「没命中」）",
         '    if _matches_keyword(content, cfg.get("keywordTrigger") or {}, chatroom_id):',
         "    if False:"),
        # -- reason 三分类（A8）：失效形态是"**明令拒绝的消息被喂给 LLM**"，
        #    以及它的反面"新增一条触发路被静默吞掉"。两条都不报错。
        ("★ 漏登记一条 deny reason（老毛病：枚举白名单时代它就漏到 LLM 了）",
         '    "self",                   # ⑤ 自己发的（防 vendor 回推自问自答）\n',
         ""),
        ("漏登记一条放行 reason（那条触发路被静默吞掉：消息进来、零日志）",
         '    "dm",                 # ⑪ 私聊白名单内\n',
         ""),
        ("心流漏口被关（heartflow_candidate 也丢 ⇒ 群里再没有主动插话）",
         'HEARTFLOW_REASONS = frozenset({\n    "heartflow_candidate",\n})',
         "HEARTFLOW_REASONS = frozenset()"),
        ("fail-closed 兜底改成 fail-open（不认识的 reason 一律放行给 LLM）",
         "    if reason in HEARTFLOW_REASONS:\n        return CLASS_HEARTFLOW\n    return CLASS_DENY",
         "    if reason in HEARTFLOW_REASONS:\n        return CLASS_HEARTFLOW\n    return CLASS_ALLOW"),
        ("ok 判据写反（放行类全被丢：谁都触发不了）",
         "    if ok:\n        return False\n    return reason_class(reason) != CLASS_HEARTFLOW",
         "    if ok:\n        return True\n    return reason_class(reason) != CLASS_HEARTFLOW"),
        ("拒绝理由永不丢（deny 全落到下游「交心流」那条路上 ⇒ 撤回/黑名单消息进 LLM）",
         "    return reason_class(reason) != CLASS_HEARTFLOW",
         "    return False"),
        ("★ requireAtMention 被实现成硬门（与 TS 分叉：群里非 @ 消息静默不触发、零日志）",
         '    # ⑦ @机器人 → 100% 必回\n'
         "    if is_bot_mentioned_by_text(content, self_wxid, self_nickname):",
         '    if cfg.get("requireAtMention") and not is_bot_mentioned_by_text(\n'
         '            content, self_wxid, self_nickname):\n'
         '        return False, "group_mention"\n'
         "    # ⑦ @机器人 → 100% 必回\n"
         "    if is_bot_mentioned_by_text(content, self_wxid, self_nickname):"),
    ]),

    # ------------------------------------------------- 数据层（test_db.py）
    #
    # 这一组守的是"**schema 与线上不可能漂移**"这条承诺。它的失效形态同样静默：
    # 重复 ADD COLUMN 报的是 1060（像 SQL 手滑）、裸 GROUP BY chat_id 让占比恒 0%
    # （像"还没数据"）、无参 SQL 被插值报的是 format string 错（像 SQL 写错）。
    "db-migrations": (ROOT / "plugin" / "data" / "migrations.py",
                      "plugin/tests/test_db.py", [
        ("解析时不再剥注释（行尾注释里的逗号切碎列定义 ⇒ 重复 ADD ⇒ 1060）",
         "    text = strip_comments(sql_text)\n    tables: dict[str, TableSpec] = {}",
         "    text = sql_text\n    tables: dict[str, TableSpec] = {}"),
        ("已存在的列也照样 ADD（每次启动把整表再补一遍）",
         "        for col, defn in spec.columns:\n            if col in have_cols:\n                continue",
         "        for col, defn in spec.columns:\n            if False:\n                continue"),
        ("切换/陈旧 checksum 不再阻断……但它本来就不阻断 —— 这里改的是**不再留痕**（线上表是谁建的成悬案）",
         "    if recorded != actual:",
         "    if False:"),
        ("「切出 0 条语句」防线拆掉（TS applySchemaSql 静默失效的形态）",
         "    if not statements:",
         "    if False:"),
    ]),
    "db-conn": (ROOT / "plugin" / "data" / "db.py", "plugin/tests/test_db.py", [
        ("无参 SQL 也走带参形式（pymysql 会对字面量 % 插值 ⇒ LIKE 'wpp_hf_%' 炸）",
         "        if params:\n            cur.execute(sql, params)",
         "        if True:\n            cur.execute(sql, params)"),
        ("host 白名单不生效（R3 红线：可以连上非本地库）",
         "    if host in _ALLOWED_HOSTS:\n        return",
         "    if False:\n        return"),
    ]),
    "db-shape": (ROOT / "plugin" / "data" / "repo_messages.py",
                 "plugin/tests/test_db.py", [
        ("Math.round 换成 Python 的银行家舍入（2.5 → 2，与 TS 不一致）",
         "    return int(math.floor(f + 0.5))",
         "    return int(round(f))"),
        ("群分组键去掉 COALESCE(NULLIF(...))（bot 侧占比恒 0% 且不报错）",
         '        SELECT COALESCE(NULLIF(chat_id, \'\'), peer_id) AS gid, direction, COUNT(*) AS n',
         '        SELECT chat_id AS gid, direction, COUNT(*) AS n'),
    ]),
    # --- 事务与快照（D4/D5，2026-10-02）----------------------------------------
    #
    # 这两条坏的形态是"**不报错、只是数据慢慢变错**"：裸 ``START TRANSACTION`` 被
    # ``execute`` 自己提交掉 ⇒ migration 中途炸掉留**半套 schema**；只读查询不收尾 ⇒
    # ``autocommit=False`` + REPEATABLE READ 下线程内复用的连接**永远读同一个快照**
    # （新台账看不见、"最后真发言时间"不动，一行日志都没有）。所以判据必须两套：
    # 事务语义（进没进、回不回滚、depth 还原没有）+ 读侧收尾。
    "db-tx": (ROOT / "plugin" / "data" / "db.py", "plugin/tests/test_db.py", [
        ("读侧不再收尾（D5：线程内永远读同一个快照 ⇒ 新台账看不见）",
         "            # 事务里提交会把外层 atomicity 拆掉，所以必须看 depth。\n"
         "            if getattr(_local, \"depth\", 0) == 0:\n"
         "                self._conn.commit()",
         "            # 事务里提交会把外层 atomicity 拆掉，所以必须看 depth。\n"
         "            if False:\n"
         "                self._conn.commit()"),
        ("事务里也提前提交（把外层原子性拆掉 ⇒ 半套改动落库）",
         "            yield self\n            if _local.depth == 1:\n                self.commit()",
         "            yield self\n            if True:\n                self.commit()"),
        ("事务炸了不回滚（D4 的现场：半套 schema 留在库里）",
         "            if _local.depth == 1:\n                self.rollback()",
         "            if False:\n                self.rollback()"),
        ("depth 不还原（此后该线程的读写**再也不提交**，静默且极难查）",
         "        finally:\n            _local.depth = depth",
         "        finally:\n            pass"),
        ("模块级 transaction 绕开 Connection.transaction（自己裸提交）",
         "    with get_conn().transaction() as conn:\n        yield conn",
         "    conn = get_conn()\n    yield conn\n    conn.commit()"),
    ]),
    # --- 定义漂移体检 + migration 原子性（D4/D6，2026-10-02）---------------------
    #
    # ``ensure_schema_complete`` 的判据只是"列名在不在"：名字在、定义变了（VARCHAR 变宽、
    # NOT NULL 掉了、默认值没了、索引换了列）它一律不吭声，而后果全是慢性的。这里的每条
    # 变异都是"**让它不吭声**"或"**让它假警报**"（假警报的代价是没人再看体检）。
    # 前两条是 D4：migration 循环的整体性 —— 走回裸语句那条路就是"半套 schema"。
    "db-drift": (ROOT / "plugin" / "data" / "migrations.py",
                 "plugin/tests/test_db.py", [
        ("migration 不再进事务（走回裸语句那条路 ⇒ 半套 schema）",
         "        with _atomic(conn):\n            mig.fn(conn)\n"
         "            _record(conn, mig.version, mig.name, None)",
         "        mig.fn(conn)\n        _record(conn, mig.version, mig.name, None)"),
        ("_atomic 视而不见（桩有 transaction 也不进）",
         "    with tx():\n        yield conn",
         "    yield conn"),
        ("体检整个不跑（永远报「无漂移」）",
         "        drifted = check_schema_drift(conn, text)",
         "        drifted = []"),
        ("体检自己坏了也算「无漂移」（永远全绿）",
         "        return [f\"体检失败: {exc}\"]",
         "        return []"),
        ("类型归一不抹显示宽度（INT vs int(11) 每次报一堆假漂移）",
         "    if base in _WIDTH_NOISE:",
         "    if False:"),
        ("列型漂移不报（写超长被截断 / 精度变小，都不出声）",
         "            if got_type is not None and got_type != want[\"type\"]:",
         "            if False:"),
        ("可空性漂移不报（从「写不进去」变成「写得进来」）",
         "            if got_null != want_not_null:",
         "            if False:"),
        ("默认值漂移不报（不给值时行为变了）",
         "                if got_default != want[\"has_default\"]:",
         "                if False:"),
        ("时间列默认值也拿去比（服务端自己会补 ⇒ 假警报）",
         "            if want[\"default_reliable\"]:",
         "            if True:"),
        ("AUTO_INCREMENT 漂移不报（主键不自增 ⇒ 插入报错才发现）",
         "            if (\"auto_increment\" in got_extra) != want[\"auto_inc\"]:",
         "            if False:"),
        ("索引列漂移不报（同名索引挂在错的列上 ⇒ 热查询静默退化成 filesort）",
         "            if not got_cols or want_cols == got_cols:",
         "            if True:"),
        ("线上行字段不全时静默跳过（查询别名写坏 ⇒ 体检永远全绿）",
         "        usable = bool(live_cols) and all(",
         "        usable = True or all("),
        # --- 四条 MariaDB 归一（2026-10-02 在 .183 真库上抓出来的形状差异）------------
        #
        # 这四条改的是**体检的方向**：前两条让体检把真差异当"服务端习惯"放过（假绿），
        # 后两条让体检把服务端习惯当真差异报出来（一次 66/7 条假警报 ⇒ 没人再看体检）。
        # 它们的共同点：**stub 永远绿** —— 桩的行是我们按"以为服务端会怎么回"造的，
        # 所以这四条只有真库用例（``test_db_live.py`` + ``test_db.py`` §8）能杀。
        ("MariaDB 的 'NULL' 默认值当真有默认（真库上 66 条假警报）",
         "    raw = got.get(\"dflt\")\n    if raw is None:\n        return False\n"
         "    return str(raw).strip().upper() != \"NULL\"",
         "    raw = got.get(\"dflt\")\n    return raw is not None"),
        ("json 别名不认识（JSON 列在 MariaDB 上回 longtext ⇒ 4 条假漂移）",
         "_TYPE_ALIASES = {\"json\": \"longtext\"}",
         "_TYPE_ALIASES = {}"),
        ("行内 PRIMARY KEY 不算 NOT NULL（7 条假漂移）",
         "        \"not_null\": bool(re.search(r\"\\bNOT\\s+NULL\\b\", up)) or \"PRIMARY KEY\" in up,",
         "        \"not_null\": bool(re.search(r\"\\bNOT\\s+NULL\\b\", up)),"),
        ("表级 PRIMARY KEY 不登记（同上，但走的是 parse_schema 这条线）",
         "                    spec.pk.extend(_index_cols(item))",
         "                    _index_cols(item)"),
    ]),
    # --- DB 体检接线（D7，2026-10-02）--------------------------------------------
    #
    # ``adapter._run_migrations`` 的失效形态是"**看起来一切正常**"：失败只留一行 warn、
    # gauge 忘了归零、计数器不增、异常吞成 pass —— 于是"schema 只建了一半"在运维面上
    # 与"一切正常"完全一样（心流每条都写不进台账，0 条发言看起来就像群里没人说话）。
    "adapter-db-health": (ADAPTER, "plugin/tests/platform/test_db_health.py", [
        ("DB 不可用时不置 gauge（金丝雀永远报「就绪」）",
         "            if not db.is_available():\n                metrics.set_db_gauge(0)",
         "            if not db.is_available():\n                pass"),
        ("migration 成功后不置 1（永远显示未就绪）",
         "            result = db.migrate()\n            metrics.set_db_gauge(1)",
         "            result = db.migrate()\n            pass"),
        ("失败不计数（「跑过但没成」与「根本没跑」分不开）",
         "            metrics.inc_db_migration_failure()\n            metrics.set_db_gauge(0)",
         "            metrics.set_db_gauge(1)"),
        ("失败只 warn 不 error（09-26 那种「看起来正常」再回来一次）",
         "            log.error(\"[WPP] DB migration 未完成（不影响收信，**心流/审计降级**；\"",
         "            log.warning(\"[WPP] DB migration 未完成（不影响收信，**心流/审计降级**；\""),
        ("异常吞成 pass（连日志都没有）",
         "        except Exception as exc:  # noqa: BLE001 —— 不阻断接入，但要留下不会消失的痕迹\n"
         "            metrics.inc_db_migration_failure()\n"
         "            metrics.set_db_gauge(0)\n"
         "            log.error(\"[WPP] DB migration 未完成（不影响收信，**心流/审计降级**；\"\n"
         "                      \"schema 可能是**半套**）: %s\", exc, exc_info=True)",
         "        except Exception as exc:  # noqa: BLE001\n            pass"),
        ("漂移不报（有漂移也当一切正常）",
         "            if result.get(\"drifted\"):",
         "            if False:"),
    ]),
    # @ 提及层单独一套（A7，2026-10-02）：这一层是"群里到底叫没叫 bot"的唯一判据，
    # 判真就是**100% 必回**（绕开心流阈值）。两个方向判错都是事故：漏判 ⇒ bot 在群里
    # 哑掉且无日志；误判 ⇒ 抢话。整层此前零测试，所以这里逐条钉死六个 pattern、
    # 三条截断（防 ReDoS）与"昵称必须带 @"这次行为变更。
    "mentions": (ROOT / "plugin" / "inbound" / "parser.py",
                 "plugin/tests/test_mentions.py", [
        ("微信号形态 pattern（⑤）失效 ⇒ self_wxid 是微信号的 bot 群里哑掉",
         '    re.compile(r"<atuserlist>[\\s\\S]*?<username>([a-zA-Z][a-zA-Z0-9_-]{5,})</username>"),',
         '    re.compile(r"(?!)"),'),
        ("正文里的 @微信号 pattern（⑥）失效（同上，另一条路）",
         '    re.compile(r"@([a-zA-Z][a-zA-Z0-9_-]{5,})[^\\s@]*"),',
         '    re.compile(r"(?!)"),'),
        ("`<at user=...>` pattern（③）整条删掉（少一类群 @ 通知）",
         '    re.compile(r\'<at\\s+user="(wxid_[a-zA-Z0-9]+)"\'),\n', ""),
        ("提取侧 4096 截断拆掉（病态正文直接进正则 ⇒ ReDoS）",
         "    if not content:\n        return []\n    text = content[:_RE_MAX_TEXT]",
         "    if not content:\n        return []\n    text = content"),
        ("去重拆掉（同一人按 pattern 数重复出现）",
         "            if wxid and wxid not in out:\n                out.append(wxid)",
         "            if wxid:\n                out.append(wxid)"),
        ("清洗侧 4096 截断拆掉（同上，另一条路）",
         "    text = content[:_RE_MAX_TEXT]\n    escaped = re.escape(bot_wxid)",
         "    text = content\n    escaped = re.escape(bot_wxid)"),
        ("\\b 不再按 ASCII 判定（紧贴中文的提及剥不掉 —— Python 默认 Unicode）",
         '    text = re.sub(rf"\\b{escaped}\\b\\s*", "", text, flags=re.ASCII)',
         '    text = re.sub(rf"\\b{escaped}\\b\\s*", "", text)'),
        ("XML 提取那条路拆掉（④ 群通知 XML 里只有 wxid ⇒ 判成没叫）",
         "        if bot_wxid in extract_at_user_list(content):\n            return True",
         "        if False:\n            return True"),
        ("昵称兜底拆掉（⑦ 群 XML 里只有昵称 ⇒ 判成没叫）",
         '    if bot_nickname and "<atuserlist>" in content:',
         "    if False:"),
        ("昵称分支退回旧语义（裸子串 ⇒ 群里顺口提一句昵称就抢话）",
         '    if bot_nickname and _nickname_mentioned(content, bot_nickname):',
         "    if bot_nickname and bot_nickname in content:"),
        ("昵称匹配不做边界判定（@小助理2 也叫醒小助理 ⇒ 前缀撞名的 bot 互相抢答）",
         '    pattern = re.compile(rf"@{re.escape(nickname)}(?![A-Za-z0-9_])")',
         '    pattern = re.compile(rf"@{re.escape(nickname)}")'),
        ("XML 兜底不再核对昵称（别人的 @ 也算叫了 bot ⇒ 抢话）",
         "        if m and m.group(1).strip() == bot_nickname:",
         "        if m:"),
        ("XML 兜底绕过 4096 截断（未截断正文进正则）",
         '                      content[:_RE_MAX_TEXT])\n        if m and m.group(1).strip() == bot_nickname:',
         "                      content)\n        if m and m.group(1).strip() == bot_nickname:"),
        ("normalize_mention 不再去 @wxid（喂给 agent 的正文仍带着 @bot）",
         '    if self_wxid:\n        c = c.replace(f"@{self_wxid}", "").replace(f"{self_wxid}@", "")',
         '    if False:\n        c = c.replace(f"@{self_wxid}", "").replace(f"{self_wxid}@", "")'),
        ("normalize_mention 不再去 @昵称",
         '    if self_nickname:\n        c = c.replace(f"@{self_nickname}", "")',
         '    if False:\n        c = c.replace(f"@{self_nickname}", "")'),
        ("★ prepare_text 不再调 normalize_mention（函数写得再对，不在路上等于没有）",
         '        text = normalize_mention(text, self.cfg.get("selfWxid", ""), self.cfg.get("nickname", ""))',
         "        text = text"),
    ]),

    # ``repo_hf`` 单独一套（B9，2026-10-02）：调阈侧要按**每条样本自己那一小时**查本底，
    # 而"哪一小时"只能从 ``sent_at`` 得到 —— 少取一列 / 不归一，表现都是阈值**静默僵住**
    # 或把夜里那批噪声当成有效信息（两条路都不报错，也都只在几天后看得出来）。
    "repo-hf": (ROOT / "plugin" / "data" / "repo_hf.py",
                "plugin/tests/heartflow/test_trace.py", [
        ("调阈样本不再取 sent_at（本底无从查时段 ⇒ 阈值静默僵住 / B9）",
         "        SELECT engaged, closed_at, engage_signal, sent_at,\n",
         "        SELECT engaged, closed_at, engage_signal,\n"),
        ("sent_at 不归一（Decimal 键永远查不到本底 ⇒ 夜里那批全采信 / B9）",
         '        r["sent_at"] = None if r.get("sent_at") is None else int(r["sent_at"])',
         "        pass"),
    ]),

    # ------------------------------------------------- 启用/账号/注册（F1 + F5）
    #
    # 这三套守的是"**插件起不来**"这一类失效：它和入站判定正好相反 ——
    # 判定类失效是悄悄吞消息，这一类是悄悄**什么都不干**（平台没启用 ⇒ 收不到任何
    # 消息；工具少注册 ⇒ 模型看不见那些能力）。两者都不报错，都只能靠门钉住。
    "enablement": (ROOT / "plugin" / "platform" / "enablement.py",
                   "plugin/tests/test_enablement.py", [
        ("★ 第 ④ 路（插件自管 accounts/*.json）删掉 —— 这就是 F1：按文档配好也永不启用",
         "    return credentials.any_local_account_configured()",
         "    return False"),
        ("第 ② 路（WECHATPRO_ACCOUNTS_JSON）删掉（显式注入的部署起不来）",
         "    if credentials.any_configured(credentials.env_accounts()):\n        return True",
         "    if False:\n        return True"),
        ("seed 不再看凭据（随仓库分发的空壳账号也让平台启用 ⇒ 无凭据的重试循环）",
         "    accounts = credentials.configured_accounts(\n"
         "        credentials.env_accounts() or credentials.load_local_accounts())",
         "    accounts = (credentials.env_accounts()\n"
         "                or credentials.load_local_accounts())"),
        ("home_channel 的名字读错变量（``_NAME`` 后缀丢了 ⇒ 群名恒为 Home）",
         'gate_env(f"{_HOME_ENV}_NAME") or "Home"',
         'gate_env(_HOME_ENV) or "Home"'),
        ("validate_config 恒真（`hermes config` 说配好了、凭证门却说不）",
         "    return is_connected(config)",
         "    return True"),
        ("平台名漂一个字（工具挂不到平台上，而注册计数全对 ⇒ 两边都不报错）",
         'PLATFORM_NAME = "wechatpadpro"',
         'PLATFORM_NAME = "wechatpadprox"'),
    ]),
    "filehelper-auth": (ROOT / "plugin" / "core" / "filehelper_auth.py",
                        "plugin/tests/test_filehelper_auth.py", [
        ("未配 adminUsers 当放行（管理面裸奔 / C3 第 1 条）",
         "        return NO_ADMIN_CONFIGURED", "        return ALLOW"),
        ("空发件人当管理员（C3 第 2 条：vendor 常把 filehelper 的 sender 留空）",
         "    return ALLOW if who in admins else NOT_ADMIN",
         "    return ALLOW if who in admins or not who else NOT_ADMIN"),
        ("空白项也算配好了（空白表变成「谁都匹配不上」的假配置）",
         "    admins = {str(a).strip() for a in (admin_users or ()) if str(a).strip()}",
         "    admins = {str(a) for a in (admin_users or ())}"),
        ("比对前不 strip（发件人带空白就拒 —— 管理员自己反而用不了）",
         '    who = str(sender or "").strip()', '    who = str(sender or "")'),
        ("判词改名（审计/告警线按词聚合，改名等于断线）",
         'ALLOW = "allow"', 'ALLOW = "ok"'),
    ]),
    "adapter-filehelper-auth": (ADAPTER, "plugin/tests/test_filehelper_auth.py", [
        ("非 allow 也往下执行（鉴权退化成装饰）",
         "        if why != filehelper_auth.ALLOW:", "        if False:"),
        ("拒绝后不终止（管理命令漏给 LLM 当闲聊回一句）",
         'acct.id, msg.from_wxid or "")\n            return True',
         'acct.id, msg.from_wxid or "")\n            pass'),
        ("判据换恒真（闸失效）",
         "why = filehelper_auth.verdict(acct.admin_users, msg.from_wxid)",
         "why = filehelper_auth.ALLOW"),
        ("adminUsers 取错来源（不再按账号隔离）",
         "filehelper_auth.verdict(acct.admin_users, msg.from_wxid)",
         "filehelper_auth.verdict((), msg.from_wxid)"),
    ]),
    "adapter-account-guard": (ADAPTER, "plugin/tests/test_accounts.py", [
        ("★ 红线守卫没接线（判据再对，connect() 里不调它也白搭 / C4）",
         '            accounts_mod.assert_no_red_line(self._accounts.values())',
         '            pass'),
        ("守卫判空集（等于这道门不存在，而源码形状看着还在）",
         "            accounts_mod.assert_no_red_line(self._accounts.values())",
         "            accounts_mod.assert_no_red_line([])"),
        ("守卫挪到「可连接」过滤之后（红线又退回被当「没配好」跳过）",
         "            accounts_mod.assert_no_red_line(self._accounts.values())\n"
         "\n"
         "            connectable = [a for a in self._accounts.values()\n"
         "                           if accounts_mod.refusal_reason(a) is None]",
         "            connectable = [a for a in self._accounts.values()\n"
         "                           if accounts_mod.refusal_reason(a) is None]\n"
         "\n"
         "            accounts_mod.assert_no_red_line(self._accounts.values())"),
    ]),
    "webhook-paths": (ROOT / "plugin" / "core" / "webhook_paths.py",
                      "plugin/tests/test_webhook.py", [
        ("token 段不插进路径（回到「配了等于没配」，回调地址又可猜 / C2+A6）",
         '    if token and path:\n        path = path.replace(_PREFIX, f"{_PREFIX}{token}/", 1)',
         '    if False:\n        path = path.replace(_PREFIX, f"{_PREFIX}{token}/", 1)'),
        ("token 段插错位置（vendor 侧旧回调 URL 全部对不上 ⇒ 入站静默全丢）",
         'f"{_PREFIX}{token}/"', 'f"{token}{_PREFIX}"'),
        ("配了 token 时不再派生 business 路径（vendor 推完整消息那条又 404 / A5）",
         '    business = (webhook_business_path or "").strip()',
         '    business = ""'),
        ("路径全空时的兜底段漂了（按旧值配过 nginx 的部署会 404）",
         '_FALLBACK_BUSINESS = "/wechatpadpro/default/webhook/business"',
         '_FALLBACK_BUSINESS = "/wechatpadpro/default/webhook"'),
        ("没有 vendor 前缀时不再退化成追加（token 被静默丢弃 ⇒ 路径里没凭据）",
         'else f"{path.rstrip(\'/\')}/{token}"', 'else path'),
        ("token 判据不再认尾段（注册侧派生 /hook/T，鉴权侧判它没 token ⇒ 自锁死）",
         '    return where.endswith(f"/{tok}") or f"/{tok}/" in where',
         '    return f"/{tok}/" in where'),
        ("token 判据退成子串包含（query 里带上 token 就能过）",
         '    return where.endswith(f"/{tok}") or f"/{tok}/" in where',
         '    return tok in where'),
        ("空 token 也算「验过了」（无凭据路由被放行）",
         "    if not tok:\n        return False", "    if not tok:\n        return True"),
    ]),
    "webhook-auth": (ROOT / "plugin" / "platform" / "inbound_webhook.py",
                     "plugin/tests/test_webhook.py", [
        ("两样凭据都没有也放行（**C2 本体**：公网可 POST 伪造微信消息）",
         "                if not secret and not path_token:\n                    return False",
         "                if not secret and not path_token:\n                    return True"),
        ("配了路径 token 却不核对它在不在路径里（配了就当验过）",
         "                if path_token and not token_is_in_path(path, path_token):\n"
         "                    return False",
         "                if False:\n                    return False"),
        ("secret 一个头都不试（配了 secret 也进不来 ⇒ 运维会去关鉴权）",
         "                for header in _SECRET_HEADERS:", "                for header in ():"),
        ("query 里的 secret 不认（部分 vendor 版本只走 query ⇒ 全 403）",
         '                    if key in ("secret", "token") and val'
         " and hmac.compare_digest(val, secret):",
         "                    if False:"),
        ("handler 不再复核鉴权（路由注册侧一旦漏了就是裸奔 / 第二道网失效）",
         "                if not self._authorized(path, secret, path_token):",
         "                if False:"),
        ("403 之后不 return（被拒的载荷照样进回调）",
         '                    self._reply(403, b\'{"error":"forbidden"}\')\n                    return',
         '                    self._reply(403, b\'{"error":"forbidden"}\')'),
        ("拿不到主循环时回 200（旧行为：静默丢消息，vendor 不重试）",
         '                    self._reply(503, b\'{"error":"loop unavailable"}\')',
         '                    self._reply(200)'),
        ("请求体上限失效（10MB 直接读进内存）",
         "                if length > WEBHOOK_BODY_LIMIT:", "                if False:"),
        ("路由不存 path_token（请求侧复核恒空 ⇒ 只剩 secret 一路）",
         '        self._routes[path] = (on_message, secret or "", path_token or "")',
         '        self._routes[path] = (on_message, secret or "", "")'),
        ("回调异常不打日志（消息已 200 ⇒ 静默消失）",
         'log.error("[WPP webhook] 回调执行失败: %s", exc, exc_info=exc)', "pass"),
        ("CancelledError 不单独接（BaseException 漏进回调机制，正常取消像故障）",
         "                except asyncio.CancelledError:", "                except ZeroDivisionError:"),
    ]),
    "adapter-webhook": (ADAPTER, "plugin/tests/test_webhook.py", [
        ("无凭据也注册路由（fail-closed 第一道网失效）",
         "            if not acct.webhook_path_token and not acct.webhook_secret:",
         "            if False:"),
        ("无凭据只报 error 不 continue（报了错照样把口子敞开）",
         "                continue\n            self._webhook.add_path(\n"
         "                paths.webhook,",
         "                pass\n            self._webhook.add_path(\n"
         "                paths.webhook,"),
        ("business 路径不注册（count 仍是 2，但那条路又 404 / A5）",
         "                paths.business,", "                paths.webhook,"),
        ("注册时不传路径 token（请求侧复核拿不到凭据）",
         "                secret=acct.webhook_secret, path_token=acct.webhook_path_token,\n"
         "            )\n            registered += 1",
         '                secret=acct.webhook_secret, path_token="",\n'
         "            )\n            registered += 1"),
        ("派生改回配置原文（与推给 vendor 的 URL 各自漂 / A6）",
         "            paths = webhook_paths.derive_webhook_paths(\n"
         "                acct.webhook_path, acct.webhook_path_token, acct.webhook_business_path)",
         '            paths = webhook_paths.WebhookPaths(\n'
         '                acct.webhook_path, f"{acct.webhook_path}/business")'),
        ("注册计数不涨（一个路由都没成也照样起监听）",
         "            registered += 1", "            pass"),
    ]),
    # 出站闸（C1/C5）：失效形态是**调试期的"保险"其实是假的** —— 白名单设了、日志全绿、
    # 消息照样发到生产群。判据本身 + 收口的接线各一套，因为"判据对但没挂上"和
    # "挂上了但判据是摆设"在运行期长得一模一样（都只有一句日志）。
    "outbound-guard": (ROOT / "plugin" / "core" / "outbound_guard.py",
                       "plugin/tests/test_outbound_guard.py", [
        ("写词优先失效（FinderSendText / StartAutoSync 被词面里的读词救回只读 / C1）",
         "    if words & WRITE_WORDS:\n        return False", "    if False:\n        return False"),
        ("认不出的端点判成只读（fail-closed 变 fail-open）",
         "    return bool(words & READ_WORDS)", "    return True"),
        ("词表删掉 Send（交付类动词全靠「认不出⇒受闸」兜底，兜不住就漏）",
         '    "Send", "Upload", "Share", "Post", "Publish", "Forward", "Revoke", "Set", "Add",',
         '    "Upload", "Share", "Post", "Publish", "Forward", "Revoke", "Set", "Add",'),
        ("只读端点不再豁免（调试期心跳/同步全被自己拒掉 ⇒ 闸没人敢开）",
         "    if is_read_only(endpoint):\n        return", "    if False:\n        return"),
        ("取不到目标也放行（说不清发给谁的写操作直接过 / fail-open）",
         "    if not targets:\n        raise netguard.GuardError(",
         "    if False:\n        raise netguard.GuardError("),
        ("只核对第一个目标（多目标里混一个白名单外的就漏）",
         "    for target in targets:\n        netguard.check_outbound_allowed(target, account_id)",
         "    for target in targets[:1]:\n        netguard.check_outbound_allowed(target, account_id)"),
        ("未设白名单不再是 no-op（生产期全部发送被拒 = 生产事故）",
         "    if not netguard.send_allowlist():\n        return", "    if False:\n        return"),
        ("非目标键下的裸标量也当目标（blackList「不给谁看」被当成收件人 ⇒ 判反）",
         "            if _norm_key(key) in _NORMALIZED_TARGET_KEYS:\n                _add_value(val, out)",
         "            if True:\n                _add_value(val, out)"),
        ("多目标键改名（ToIds 系端点取不到目标 ⇒ 一律被拒或漏判）",
         '    "ToIds", "toIds",', '    "ToIdz", "toIdz",'),
        ("群目标键改名（QID 系端点取不到目标）",
         '    "QID", "qid",', '    "QIDx", "qidx",'),
        ("递归深度上限归零（嵌套 Items 里的目标全取不到）",
         "_MAX_DEPTH = 3", "_MAX_DEPTH = 0"),
        ("目标数上限取消（病态载荷把闸变成 CPU 放大器）",
         "_MAX_TARGETS = 64", "_MAX_TARGETS = 1000000"),
        ("目标不去重（同一目标核对 N 次，白名单形同放大）",
         "        if got and got not in out:", "        if got:"),
        ("bool 也当目标（{\"ToWxid\": true} 这种占位值被拉去核对）",
         "    if isinstance(value, bool) or value is None:\n        return", "    if value is None:\n        return"),
    ]),
    "client-outbound-guard": (ROOT / "plugin" / "core" / "api_client.py",
                              "plugin/tests/test_outbound_guard.py", [
        ("_raw_url 上不再调判据（收口被摘 ⇒ 工具面全在闸外 / C1）",
         "        outbound_guard.guard(endpoint, body, self.account_id)", "        pass"),
        ("判据收不到请求体（写端点全部变成「没有目标⇒拒绝」）",
         "        outbound_guard.guard(endpoint, body, self.account_id)",
         "        outbound_guard.guard(endpoint, None, self.account_id)"),
        ("判据收不到 account_id（`acct:peer` 形式的白名单永远对不上）",
         "        outbound_guard.guard(endpoint, body, self.account_id)",
         '        outbound_guard.guard(endpoint, body, "")'),
        ("主请求 helper 不把 body 传下去（最常用的那条路取不到目标）",
         "        url = self._raw_url(endpoint, body)", "        url = self._raw_url(endpoint)"),
        ("字节下载 helper 不把 body 传下去（媒体下载走的那条路）",
         "                self._raw_url(endpoint, body), json=body,\n"
         "                headers=self._headers(token_key=True),\n"
         "                timeout=aiohttp.ClientTimeout(total=timeout),\n"
         "            ) as resp:\n"
         "                if resp.status != 200:\n"
         '                    log.warning("[WPP] %s HTTP %s", endpoint, resp.status)\n'
         "                    return None\n"
         '                cl = resp.headers.get("Content-Length")',
         "                self._raw_url(endpoint), json=body,\n"
         "                headers=self._headers(token_key=True),\n"
         "                timeout=aiohttp.ClientTimeout(total=timeout),\n"
         "            ) as resp:\n"
         "                if resp.status != 200:\n"
         '                    log.warning("[WPP] %s HTTP %s", endpoint, resp.status)\n'
         "                    return None\n"
         '                cl = resp.headers.get("Content-Length")'),
        ("JSON 下载 helper 不把 body 传下去",
         "                self._raw_url(endpoint, body), json=body,\n"
         "                headers=self._headers(token_key=True),\n"
         "                timeout=aiohttp.ClientTimeout(total=timeout),\n"
         "            ) as resp:\n"
         "                if resp.status != 200:\n"
         '                    log.warning("[WPP] %s HTTP %s", endpoint, resp.status)\n'
         "                    return None\n"
         "                try:\n"
         "                    data = json.loads(await resp.text())",
         "                self._raw_url(endpoint), json=body,\n"
         "                headers=self._headers(token_key=True),\n"
         "                timeout=aiohttp.ClientTimeout(total=timeout),\n"
         "            ) as resp:\n"
         "                if resp.status != 200:\n"
         '                    log.warning("[WPP] %s HTTP %s", endpoint, resp.status)\n'
         "                    return None\n"
         "                try:\n"
         "                    data = json.loads(await resp.text())"),
    ]),
    # ★ 2026-10-18 审阅修：写端点不重试。失效形态是"偶发超时 ⇒ 给真实联系人
    #   发了第二条"（微信撤不回）。判据在 call() 收口；typed 方法自己也传 0，
    #   所以变异要打在收口那一行才算真正的防线。
    "client-retry": (ROOT / "plugin" / "core" / "api_client.py",
                     "plugin/tests/test_api_retry.py", [
        ("写端点重试收口被摘（超时/502 会给对方发第二条）",
         "        if not outbound_guard.is_read_only(endpoint):\n            max_retries = 0",
         "        if False:\n            max_retries = 0"),
        ("写端点仍重试一次（收口放宽成 1）",
         "        if not outbound_guard.is_read_only(endpoint):\n            max_retries = 0",
         "        if False:\n            max_retries = 1"),
        ("分类反了（读端点被禁重试、写端点照旧重试）",
         "        if not outbound_guard.is_read_only(endpoint):",
         "        if outbound_guard.is_read_only(endpoint):"),
    ]),
    "accounts": (ROOT / "plugin" / "core" / "accounts.py",
                 "plugin/tests/test_accounts.py", [
        ("authcode 回到默认 repr（凭据随任意一行日志进 journald / /tmp）",
         "    authcode: str = field(repr=False)",
         "    authcode: str"),
        ("生产 bot 身份守卫删掉（红线 2：能拿生产号去连调试实例）",
         "    if not acct.is_production_identity or _env_bool(ALLOW_PRODUCTION_ENV):\n"
         "        return None",
         "    if False:\n"
         "        return None"),
        ("身份判据恒真（红线变成「谁都拦」⇒ 正常账号也起不来）",
         "    if not acct.is_production_identity or _env_bool(ALLOW_PRODUCTION_ENV):",
         "    if False:\n        pass\n    if True:"),
        ("拒因一律怪 selfWxid（凭据命中的那条被报成「你 wxid 写错了」）",
         "    if acct.self_wxid == PRODUCTION_WXID:\n"
         "        which = (f\"selfWxid 就是生产 bot「益融小助理」（{PRODUCTION_WXID}）\")",
         "    if True:\n"
         "        which = (f\"selfWxid 就是生产 bot「益融小助理」（{PRODUCTION_WXID}）\")"),
        ("★ 凭据那道判据（C4）被摘掉：只看 selfWxid，写错/不写就能拿生产密钥去连",
         '        return ft != "-" and ft in PRODUCTION_CREDENTIAL_FINGERPRINTS',
         "        return False"),
        ("凭据判据口径写反（`== \"-\"`：空壳账号全变红线）",
         '        return ft != "-" and ft in PRODUCTION_CREDENTIAL_FINGERPRINTS',
         '        return ft == "-" or ft in PRODUCTION_CREDENTIAL_FINGERPRINTS'),
        ("selfWxid 那道判据被摘（配了生产号但 authcode 是别把 ⇒ 放行）",
         "        if self.self_wxid == PRODUCTION_WXID:\n            return True",
         "        if False:\n            return True"),
        ("★ 红线退回「warn 一句就跳过」（C4 的原样故障：守卫是过滤不是 raise）",
         "        reason = red_line_reason(acct)\n"
         "        if reason:\n"
         "            raise AccountError(reason)",
         '        reason = red_line_reason(acct)\n'
         "        if reason:\n"
         '            log.warning("[WPP] 跳过账号: %s", reason)'),
        ("红线判据不看「本来会不会连」（enabled=false 的生产号也拦 ⇒ .183 起不来）",
         "    if not acct.enabled or not acct.has_credentials:\n"
         "        return None\n"
         "    return _production_reason(acct)",
         "    if False:\n"
         "        return None\n"
         "    return _production_reason(acct)"),
        ("拒因里分不清哪道判据命中（运维看不出该改 wxid 还是该换 authcode）",
         "    if acct.self_wxid == PRODUCTION_WXID:\n"
         "        which = (f\"selfWxid 就是生产 bot「益融小助理」（{PRODUCTION_WXID}）\")\n"
         "    else:\n",
         "    if False:\n"
         "        which = (f\"selfWxid 就是生产 bot「益融小助理」（{PRODUCTION_WXID}）\")\n"
         "    else:\n"),
        ("env 兜底不再生效（default.json 的空串占位让凭据永远填不进去 —— 起不来的那半截）",
         "    for name in fallback_envs:\n"
         "        got = secret_env(name)\n        if got:\n            return got",
         "    for name in []:\n"
         "        got = secret_env(name)\n        if got:\n            return got"),
        ("runtime override 的 dict 值改成整体替换（同级键被抹掉：阈值/预算静默退回默认档）",
         "                cfg[key] = {**cfg[key], **val}",
         "                cfg[key] = val"),
        ("账号文件筛选不再滤 `_` 前缀（_notes.json 被当成账号）",
         '    if not name.endswith(".json") or name.startswith("_"):',
         '    if not name.endswith(".json"):'),
        ("credential_token 改回按传入顺序取（多账号下 token 随 dict 顺序漂）",
         "    for acct in sorted(accounts, key=lambda a: a.id):",
         "    for acct in accounts:"),
        ("未配 public url 时兜一个默认域名（plugin.yaml 自称 No default is applied）",
         '    public_url = _resolve_secret(cfg, "webhookPublicUrl", "WECHATPRO_WEBHOOK_PUBLIC_URL")',
         '    public_url = _resolve_secret(cfg, "webhookPublicUrl", "WECHATPRO_WEBHOOK_PUBLIC_URL")'
         ' or "https://hermes.juhe.chat"'),
        ("agent='main' 不再拦截（两个微信号共用 agent 上下文 ⇒ 串号）",
         "    if agent == FORBIDDEN_AGENT:",
         "    if False:"),
        # -- 死键（A8）：不生效可以，**静默**不行。
        ("★ 死键提醒没接线（配了 requireAtMention 的人以为群里只回 @，实际每条都进心流）",
         "    _warn_inert_keys(out.values())",
         "    pass"),
        ("死键提醒对谁都发（连没配这个键的账号也刷）",
         "        if not acct.require_at_mention or acct.id in _INERT_WARNED:",
         "        if True:"),
        ("死键提醒不设去重（每次加载刷一遍 journald）",
         "        if not acct.require_at_mention or acct.id in _INERT_WARNED:",
         "        if not acct.require_at_mention:"),
    ]),
    "registration": (ROOT / "plugin" / "toolkit" / "registration.py",
                     "plugin/tests/test_registration.py", [
        ("★ 独有能力导入前缀退回 f\"..{mod_name}\"（F5 原样复活：wecom 7 + phoneerp 4 + extras 3 静默消失）",
         'mod = importlib.import_module(f".{mod_name}", __package__)',
         'mod = importlib.import_module(f"..{mod_name}", __package__)'),
        ("注册失败只记 DEBUG（F5 的第二个条件：既丢掉工具、又不留痕）",
         '        log.warning("[WPP] %s 注册失败: %s", label, exc, exc_info=True)\n'
         '        return None',
         '        log.debug("[WPP] %s 注册失败: %s", label, exc)\n'
         '        return None'),
        ("独有能力只注册第一个（隔离退化成截断：另外两域整批消失）",
         "    for mod_name, label in _EXTRA_TOOL_MODULES:",
         "    for mod_name, label in _EXTRA_TOOL_MODULES[:1]:"),
        ("厂商端点表只拼第一份（107 个 EXTRA 端点全丢，而「少」在 Hermes 只是 WARN）",
         "    return [*vendor_defs.GROUPS, *vendor_defs_extra.GROUPS]",
         "    return [*vendor_defs.GROUPS]"),
    ]),

    # 技能落位（``skills/*/SKILL.md``）：失效形态与 F5 同一族 —— **静默丢失**。
    # Hermes 的 ``_discover_skills`` 对"目录在、但 SKILL.md 缺失/frontmatter 不合法"
    # 一律 ``continue``，**一行日志都不打**；技能正文里的工具名写错更是连加载都成功
    # （错到模型照抄那一刻才炸）。所以这四条变异分别钉：文件第一行不是 ``---``、
    # name 与目录不符、description 空、正文引用了不存在的工具。
    "skills": (ROOT / "skills" / "wpp-identity" / "SKILL.md",
               "plugin/tests/test_skills.py", [
        ("★ 开头 ``---`` 被吃掉（文件不再以 frontmatter 起始 ⇒ Hermes 静默跳过整个技能）",
         "---\nname: wpp-identity\n", "name: wpp-identity\n"),
        ("frontmatter name 与目录不符（目录还在 ⇒ 肉眼看着「已落位」，实际被丢弃）",
         "name: wpp-identity\n", "name: wpp-identity-x\n"),
        ("description 抽空（Hermes 的长度判据 1–1024 ⇒ 静默丢弃）",
         'description: "昵称↔wxid', 'description: "" #'),
        # 锚点必须是**被记号正则吃掉**的那种写法（反引号紧跟工具名）。
        # 第一版写成 ``wpp_identity_lookup(name=...)`` —— 它后面跟的是 ``(``，
        # 正则压根不认，于是变异**活着**：测试没错，是变异本身写歪了。
        ("正文工具名写错（技能照常加载，模型照抄时才会调空）",
         "`wpp_friend_contract_detail` / `wpp_friend_search`",
         "`wpp_friend_contract_detailx` / `wpp_friend_search`"),
    ]),

    # 语音转写（STT）：失效形态是"**转写没进正文**"—— 现场完全看不出来（消息收下了、
    # 音频落了、日志正常），只有模型看到的是 ``[语音]`` 一行，老板发语音＝没说话。
    # 三套分别钉：解析层怎么算（stt.py）/ 正文怎么写（parser.py）/ 接线还在不在（adapter）。
    "voice-stt": (ROOT / "plugin" / "inbound" / "stt.py",
                  "plugin/tests/test_voice.py", [
        ("★ WAV 采样率 24k → 16k（对方按错采样率识别 ⇒ 转出来是乱码，且听不出错在哪）",
         "WAV_SAMPLE_RATE = 24_000", "WAV_SAMPLE_RATE = 16_000"),
        ("★ RIFF 段长度写成裸 PCM 长度（头就错了 36 字节，对方 400）",
         'header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"',
         'header = b"RIFF" + struct.pack("<I", len(pcm)) + b"WAVE"'),
        ("块对齐用位宽而不是字节（24 而不是 2，ffmpeg 系一律拒收）",
         "block_align = WAV_CHANNELS * (WAV_BITS // 8)",
         "block_align = WAV_CHANNELS * WAV_BITS"),
        ("重试次数 3 → 1（429/5xx 抖动时直接放弃，语音全丢）",
         "MAX_ATTEMPTS = 3", "MAX_ATTEMPTS = 1"),
        ("只有 5xx 值得重试（429 是**配额抖动**，最常见的可恢复错误，却不再重试）",
         "retryable = status in RETRYABLE_STATUSES or status >= 500",
         "retryable = status >= 500"),
        ("400/403 也重试（拿同一个坏音频撞三次，白等 3 秒还多两条日志）",
         "if not retryable or attempt == MAX_ATTEMPTS:",
         "if attempt == MAX_ATTEMPTS:"),
        ("200 但空正文也重试（对方已经收下文件了，重发同一个只会再空一次）",
         'return (text or None), False, ("" if text else "HTTP 200 但正文里没有 text")',
         'return (text or None), True, ("" if text else "HTTP 200 但正文里没有 text")'),
        ("空 PCM 也当成功（套出 0 字节数据的 WAV ⇒ 对方 400）",
         "return pcm or None", "return pcm"),
        ("key 优先级反转（plugin.yaml 声明的是 WECHATPRO_STT_API_KEY，读不到就永远不转）",
         'secret_env("WECHATPRO_STT_API_KEY")\n            or secret_env("SILICONFLOW_API_KEY")',
         'secret_env("SILICONFLOW_API_KEY")\n            or secret_env("WECHATPRO_STT_API_KEY")'),
        ("降级日志只喊一次 → 每次都喊（一条语音一行 WARNING，把真异常淹掉）",
         "    global _WARNED_DEGRADED\n    if _WARNED_DEGRADED:\n"
         '        log.debug("[STT] 语音转写仍不可用: %s", reason)\n        return\n'
         "    _WARNED_DEGRADED = True\n",
         "    global _WARNED_DEGRADED\n"),
        ("「喊过了」的闸永远不置位（等于每次都喊）",
         "    _WARNED_DEGRADED = True\n", "    _WARNED_DEGRADED = False\n"),
        ("★ 降级改成 DEBUG（**一条都不喊** = 静默丢能力：语音转不出文字而没人知道）",
         'log.warning("[STT] 语音无法转写：%s ⇒ 这类语音在模型眼里只有 ``[语音]`` 一行", reason)',
         'log.debug("[STT] 语音无法转写：%s ⇒ 这类语音在模型眼里只有 ``[语音]`` 一行", reason)'),
        ("vendor 已转过还是自己再转一遍（多一次下载 + 一次外呼，还把更准的那个丢在一边）",
         'return not str((media or {}).get("vendor_transcript") or "").strip()',
         "return True"),
    ]),

    "voice-media": (ROOT / "plugin" / "inbound" / "media.py",
                    "plugin/tests/test_voice.py", [
        ("两个转写来源反了（自己转的盖掉微信官方的那个）",
         'str(media.get("vendor_transcript") or media.get("stt_text") or "").strip()',
         'str(media.get("stt_text") or media.get("vendor_transcript") or "").strip()'),
        ("转写不 strip（空白串当成「有转写」，正文多一行空的 [转写]）",
         'return str(media.get("vendor_transcript") or media.get("stt_text") or "").strip()',
         'return str(media.get("vendor_transcript") or media.get("stt_text") or "")'),
    ]),

    "voice-inject": (ROOT / "plugin" / "inbound" / "parser.py",
                     "plugin/tests/test_voice.py", [
        ("★ 转写没接进正文（算出来了却没人写 ⇒ 模型只看到 ``[语音]``，等于没说话）",
         "if transcript:\n            text = f\"{text}\\n[转写] {transcript}\"",
         "if False:\n            text = f\"{text}\\n[转写] {transcript}\""),
        ("``[转写]`` 记号改字（技能/提示词按 ``\\n[转写] `` 认，改了等于换协议）",
         'text = f"{text}\\n[转写] {transcript}"',
         'text = f"{text}\\n[语音转写] {transcript}"'),
        ("没转写也写这一行（``[转写] `` 后面空着 ⇒ 模型以为「语音内容是空的」）",
         "if transcript:\n            text = f\"{text}\\n[转写] {transcript}\"",
         "if True:\n            text = f\"{text}\\n[转写] {transcript}\""),
    ]),

    "adapter-voice": (ADAPTER, "plugin/tests/test_voice.py", [
        ("★ 语音分支不接转写（下载照做、音频照落 ⇒ 只有模型看到的是 [语音] 一行）",
         "                    await self._fill_voice_transcript(acct.id, msg.media, data)\n", ""),
        ("转写结果不写回 media（白转：正文读的是 media）",
         '            media["stt_text"] = text\n', "            _ = text\n"),
        ("vendor 优先那条短路被摘（有官方转写还去调 STT）",
         "        if not stt.should_attempt_stt(media):", "        if False:"),
        ("缺依赖照样往下走（缺解码器/缺 key 时白撞一次外呼）",
         "        reason = stt.unavailable_reason()\n        if reason:", "        reason = stt.unavailable_reason()\n        if False:"),
        # ⚠️ 这条的**载体换过一次**（W7-1，2026-10-02）：老锚点是 ``_build_event`` 里
        #    「``media_urls, media_types = await self._fetch_media(...)`` 紧挨
        #    ``text = pipeline.prepare_text(msg)``」，而 W7-1 把取媒体提到了 ``_handle_one``
        #    的 ④c、``_build_event`` 改成收 ``media=`` 参数 ⇒ 老锚点命中 0 次（BROKEN）。
        #    失效形态本身没变：**正文先算、媒体后取 ⇒ 语音转写永远赶不上正文这班车**。
        #    新的载体是"⑤ 自己取媒体且排在 ``prepare_text`` 之后"——同一条 ``str.replace``
        #    就能表达，且被 ``test_voice.py`` 的"``_build_event`` 里不许出现 ``_fetch_media``"
        #    与 W7-1 装配面的顺序判据一起杀掉。
        ("★ 先算正文再取媒体（语音转写永远赶不上正文这班车）",
         "        media_urls, media_types = media\n        text = pipeline.prepare_text(msg)\n",
         "        text = pipeline.prepare_text(msg)\n"
         "        media_urls, media_types = await self._fetch_media(acct, msg)\n"),
    ]),

    # V7：心流**热路径零 DB IO**。这一面的退化**不会报错、不会少回复、行为测试全绿**
    # —— 只是每条群消息多一次库往返，QPS 在暗处掉。所以只能用"数"的：
    # ``db_call_count()`` 是仪器，下面两条先杀仪器本身的坏法（哑掉的计数器
    # 会让热路径那几条断言**不可伪证**）。
    "hot-path-db": (ROOT / "plugin" / "inbound" / "heartflow" / "profile.py",
                    "plugin/tests/heartflow/test_hot_path_no_db.py", [
        ("★ 画像缓存冷了顺手查库补齐（judge 路径的画像本来只许读内存）",
         '    return c["text"] if c and c.get("text") else None\n',
         '    if not c:\n'
         '        from ...data import repo_hf\n'
         '        rows = list(repo_hf.list_profiles(account_id))\n'
         '        c = rows[0] if rows else None\n'
         '    return c["text"] if c and c.get("text") else None\n'),
        ("★ 画像预算缓存冷了顺手查库（编排段跟着多一次往返）",
         '    return c["budget"] if c else None\n',
         '    if not c:\n'
         '        from ...data import repo_hf\n'
         '        list(repo_hf.list_profiles(account_id))\n'
         '    return c["budget"] if c else None\n'),
    ]),

    # 闸门那条（预算 peek）在 budget.py —— 变异体必须落在**被测文件**里，
    # 第一次把它塞进 profile.py 的套件，脚本如实报了「锚点命中 0 次」。
    "hot-path-budget": (ROOT / "plugin" / "inbound" / "heartflow" / "budget.py",
                        "plugin/tests/heartflow/test_hot_path_no_db.py", [
        ("闸门里塞一次库查询（预算 peek 的契约是「不写 DB」，读也不许）",
         '"""门禁调用：判定 + 记拦截（**不写 DB**）。"""\n    if not cfg.enabled:\n',
         '"""门禁调用：判定 + 记拦截（**不写 DB**）。"""\n'
         '    from ...data import repo_hf      # 缓存只是加速，冷了回库补齐\n'
         '    repo_hf.list_group_states(account_id or "*")\n'
         '    if not cfg.enabled:\n'),
    ]),

    # V7 仪器的自证：计数器一旦哑掉，"热路径零 DB"那几条会**假装全绿**。
    # 所以仪器本身也要有变异来杀。
    "hot-path-instrument": (ROOT / "plugin" / "data" / "db.py",
                            "plugin/tests/heartflow/test_hot_path_no_db.py", [
        ("★ 计数器不计数（仪器哑 ⇒ 热路径的 0 变成不可伪证的空断言）",
         "    global _db_calls\n    with _count_lock:\n        _db_calls += 1\n",
         "    global _db_calls\n    with _count_lock:\n        pass\n"),
        ("计数读数恒 0（同上：仪器坏了，热路径却「全绿」）",
         "def db_call_count() -> int:\n"
         '    """本进程内 ``_get_conn()`` 被调用次数（**跨线程累计**）。"""\n'
         "    with _count_lock:\n        return _db_calls\n",
         "def db_call_count() -> int:\n"
         '    """本进程内 ``_get_conn()`` 被调用次数（**跨线程累计**）。"""\n'
         "    with _count_lock:\n        return 0\n"),
        ("归零不归零（上一条用例的计数漏到热路径用例里 ⇒ 真相被算术掩掉）",
         "    global _db_calls\n    with _count_lock:\n        _db_calls = 0\n",
         "    global _db_calls\n    with _count_lock:\n        pass\n"),
    ]),

    # 心流运维命令面（``/heartflow ...``）：这一面的失效形态是**回答得头头是道但内容是错的**
    # （停摆时它是唯一能问的地方）—— 所以要单独钉：状态页报账号档而不是生效档、
    # 计数口径错、写配置不说明"重启回退"、veto 后不删开窗。
    "hf-commands": (ROOT / "plugin" / "platform" / "hf_commands.py",
                    "plugin/tests/platform/test_hf_commands.py", [
        ("★ 状态页改报账号级阈值（09-26 停摆 3 天的隐形原因：judge 按生效值判、页面显示 0.60）",
         'hf_learn.resolve_hf_effective_threshold(account_id, g, hf_cfg, now) for g in shown',
         'hf_learn.resolve_reply_threshold(hf_cfg) for g in shown'),
        ("台账「回复」只数 sent（sweep 把 sent 推成 closed ⇒ 这行恒显示 0 条回复）",
         '    replied = int(by_status.get("sent", 0)) + int(by_status.get("closed", 0))',
         '    replied = int(by_status.get("sent", 0))'),
        ("重复闸拦下数改读另一个键（数字永远 0，误杀看不见）",
         '''{int(suppressed.get('repeat', 0))} 条"''',
         '''{int(suppressed.get('dedupe', 0))} 条"'''),
        ("群列表不折叠（20 群 ≈ 20 行，状态页长到看不清）",
         "HF_STATUS_MAX_GROUPS = 5", "HF_STATUS_MAX_GROUPS = 50"),
        ("停摆告警线 3 天 → 4 天（第 4 天还不报）",
         "STALL_WARN_DAYS = 3", "STALL_WARN_DAYS = 4"),
        ("「从未真发言」提示消失（金丝雀最该说话的那一格变空）",
         'return "\\n上次真发言: 从未 (检查群白名单 / 阈值 / 预算)"', 'return ""'),
        ("分钟数改成截断（Math.round → Math.floor）",
         'f"{_half_up(age / 60)} 分钟前"', 'f"{int(age / 60)} 分钟前"'),
        ("阈值区间不校验（-0.1 / 1.5 也写进去 ⇒ 闸要么全开要么永不触发）",
         "    if v is None or v < 0 or v > 1:", "    if v is None:"),
        ("非有限数照收（nan 闯过区间比较，写进覆盖 ⇒ 判定全 False）",
         "    return n if math.isfinite(n) else None", "    return n"),
        ("ValueError 不再吞（/heartflow threshold abc 抛穿到适配器 ⇒ 老板发命令毫无反应）",
         "    except (TypeError, ValueError):", "    except TypeError:"),
        ("bool 当数字（True 被当成 1）",
         "    if v is None or isinstance(v, bool):", "    if v is None:"),
        ("★ veto 后不删内存开窗（随后有人引用那条消息把 veto 覆盖成 engaged=1，比不点更糟）",
         "    hf_learn.forget_hf_open_window(account_id, gid)",
         "    _veto_no_forget = (account_id, gid)"),
        ("veto 多群歧义时不再拒绝（写错群不可撤销，污染 60 天窗口）",
         "        if len(active) >= 2:", "        if False:"),
        ("group del 也当 add 去补群聊白名单（把老板的群白名单越删越多）",
         "        if sub == \"add\":", "        if True:"),
        ("写覆盖丢掉底稿（一次改阈值把白名单/开关一起抹掉）",
         '    cfg["replyThreshold"] = v', '    cfg = {"replyThreshold": v}'),
        ("写配置的回复不再说「已落盘 + 当次生效 + 不回退」（老板无从分辨这条命令是真改了，"
         "还是旧那套「回了 ✅、重启就丢」的内存覆盖 —— 那正是本项要消灭的形态）",
         '            "已写入账号配置（accounts/<账号>.json），当次生效、重启不回退")',
         '            "")'),
        ("分段兜底改成返回空串（运维看不到「读取失败」这一事实）",
         "        return fallback", '        return ""'),
        ("report 把 legacy 旧标签行算进被制止分母（假信号当样本）",
         '    stopped_total = sum(v for k, v in sig.items() if k != "legacy")',
         "    stopped_total = sum(sig.values())"),
        ("report 天数上限 30 → 300（窗口一拉就查全表）",
         "REPORT_DAYS_MAX = 30", "REPORT_DAYS_MAX = 300"),
        ("why 不再显示影子建议（老板以为分层已经在改判了）",
         "    if d.shadow is not None:", "    if False:"),
        ("layers 丢掉「样本不足」标注（5/30 与 40/30 看起来一样）",
         '+ ("" if n >= LAY.min_samples else " (样本不足)")', '+ ""'),
        ("layers 丢掉「当前时段」游标（不知道哪一段是现在）",
         '                line += " ← 当前时段"', '                line += ""'),
        ("分层达标段不再按门槛筛（3 条样本的段也报成「已达标」）",
         '    qualified = [r for r in rows if int(r.get("n") or 0) >= LAY.min_samples]',
         "    qualified = list(rows)"),
        ("信号分布为 0 时那条解释消失",
         "    if not sig:", "    if False:"),
        ("白名单为空的提示消失（心流永不在任何群发言，而状态页看不出来）",
         '        out += "\\n(白名单无群 ⇒ 心流不会在任何群发言)"', '        out += ""'),
        ("★ 状态页丢了 DB 体检那一行（D7：半套 schema 又与「一切正常」长得一样了）",
         '    extra += _safe("\\nDB: (读体检状态失败)", lambda: _block_db_health())',
         '    extra += _safe("", lambda: _block_db_health())'),
        ("drift 不标「只报不改」（老板以为该修没修）",
         '    if state == "drift":\n        line += "（**只报不改**，R8）"',
         '    if False:\n        line += "（**只报不改**，R8）"'),
        ("体检状态一律画 ✅（越坏越像好）",
         '    mark = {"ok": "✅", "drift": "⚠️", "failed": "❌", "unavailable": "❌"}.get(state, "❔")',
         '    mark = {"ok": "✅", "drift": "✅", "failed": "✅", "unavailable": "✅"}.get(state, "❔")'),
    ]),

    # 真库用例的**护栏**（D1，2026-10-02）：``conftest._env_guard`` 是老板那条
    # 「生产库永不可用于调试」红线的唯一执行者，而它此前一条判据都没有。
    # 每条变异都是"**把某一道闸拆掉**"：拆了不会红 —— 它会绿着把真库用例放到
    # 别的项目（本机 3306 是 1Panel 的）或生产库上。
    "db-live-guard": (ROOT / "conftest.py", "plugin/tests/test_live_db_guard.py", [
        ("ALLOW_REMOTE 检查拆掉（测试进程放行远程库）",
         '    if os.environ.get("WECHATPRO_DB_ALLOW_REMOTE") == "1":',
         "    if False:"),
        ("缺省库名不再拦住（默默拿生产库名去连）",
         '    name = os.environ.get("WECHATPRO_DB_NAME", "").strip()\n    if not name:',
         '    name = os.environ.get("WECHATPRO_DB_NAME", "").strip()\n    if False:'),
        ("生产库名判据拆掉（钦定红线：可以连生产库）",
         "    if name == PROD_DB_NAME:", "    if False:"),
        ("_test 后缀判据拆掉（wechatpro_staging 之类「差一点」的名字放进来）",
         "    if not name.endswith(TEST_DB_SUFFIX):", "    if False:"),
        ("连不上也照跑（会去连不存在的库 / 别项目的 3306）",
         "    if not ok:", "    if False:"),
        ("WPP_REQUIRE_LIVE_DB 开关失效（.183 上跳过也算过 ⇒ 假绿）",
         '    if os.environ.get("WPP_REQUIRE_LIVE_DB") == "1":', "    if False:"),
    ]),

    "trace-learn": (HF / "learn.py", "plugin/tests/heartflow/test_trace.py", [
        ("判了没过的理由改成另一个词（与「投递结果没回来」混为一谈）",
         '                               "below-threshold", at_sec)',
         '                               "below-threshold-x", at_sec)'),
        ("INSERT 失败后照样 suppress（半个台账 + 吃掉唯一的 warn）",
         "        _insert_judged(record)\n        from ...data import repo_hf\n",
         "        from ...data import repo_hf\n"
         "        try:\n            _insert_judged(record)\n"
         "        except Exception:  # noqa: BLE001\n            pass\n"),
    ]),

    # ── Hermes 工具调用契约（W0，2026-10-02）───────────────────────────────
    # 失效形态的统一特征：**工具照样"注册上了"**，所以注册数、plugin.yaml 逐名对账、
    # ``doctor`` 全绿 —— 只有"点下去"才知道废了。这正是本仓反复吃亏的那种绿灯空转。
    "tool-contract": (ROOT / "plugin" / "toolkit" / "hermes_contract.py",
                      "plugin/tests/test_hermes_tool_contract.py", [
        # ⚠️ 锚点要带上函数体那一行：``guarded_handler``（W7-5）里也有**逐字相同**的
        #    ``async def handler(args: Any = None) -> Any:`` ⇒ 光写签名命中 2 次。
        ("★ 适配器声明 **kw（Hermes 的 _kwargs_accepted_by 见 **kwargs 就全部放行 ⇒ "
         "task_id/session_id/user_task 会被拼进厂商请求体，Go 静默忽略未知名 ⇒ 无声无息）",
         "        async def handler(args: Any = None) -> Any:\n"
         "            try:\n"
         "                payload = _payload(args)",
         "        async def handler(args: Any = None, **kw: Any) -> Any:\n"
         "            try:\n"
         "                payload = _payload(args)"),
        ("★ 设 __wrapped__（inspect.signature 默认穿透它 ⇒ Hermes 读到原函数的 "
         "(**params) 签名 ⇒ 上面那层过滤整个失效）",
         "    handler.__original_handler__ = fn  # type: ignore[attr-defined]",
         "    handler.__wrapped__ = fn  # type: ignore[attr-defined]\n"
         "    handler.__original_handler__ = fn  # type: ignore[attr-defined]"),
        ("收口处不再适配（wrap_ctx 收了 ctx 却不包 handler ⇒ 299 个工具全部退回 "
         "「接不住位置 dict」，而注册面全绿）",
         "            adapted = adapt_handler(fn)",
         "            adapted = fn"),
        ("多模态信封不再原样透传（被 JSON 编码 ⇒ 图片/附件从结果里消失）",
         "    if isinstance(value, Mapping) and value.get(MULTIMODAL_KEY) is True:",
         "    if False:"),
        ("非 str 结果不再 JSON 编码（Hermes 的 _normalize_handler_result 只认 str/信封 ⇒ "
         "工具一律变成 tool_error）",
         "        return json.dumps(value, ensure_ascii=False, default=str)",
         "        return value"),
        ("非 dict 载荷被静默当成空参数（协议异常不报错，工具拿着空参数去调厂商）",
         "    if args is None:\n        return {}\n    if not isinstance(args, Mapping):",
         "    if args is None or not isinstance(args, Mapping):\n        return {}\n    if False:"),
    ]),

    # 收口点本身（``register()`` 里那一行）。拆掉它，299 个工具全部不可调用，
    # **而 ``registration`` 那套变异全绿**（注册数、逐名对账、doctor 只看名字）——
    # 所以要单独一套，裁判换成"能不能真调"的那个测试文件。
    "tool-contract-seam": (ROOT / "plugin" / "toolkit" / "registration.py",
                           "plugin/tests/test_hermes_tool_contract.py", [
        ("★ 收口处被绕过（ctx 没被 wrap_ctx 包 ⇒ 299 个工具全部接不住位置 dict，"
         "而注册数 / plugin.yaml 逐名对账 / doctor 依然全绿）",
         # ⚠️ 锚点要带上 W7-5 加的那两个实参：光写 ``wrap_ctx(ctx)`` 命中 0 次
         #    （收口点现在同时装门禁）。
         "    ctx = _contract.wrap_ctx(ctx, _policy.make_tool_gate(lambda: _adapter._get_adapter()))",
         "    ctx = ctx"),
    ]),

    # 通用端点表（219 个工具走这一个 handler）。W0 修的第二处**静默**缺陷：
    # 老实现把 getter 做成同名参数、调用方又从不传 ⇒ 永远 noop ⇒
    # **每个通用工具都回「adapter 未就绪」，一次厂商请求都不发**。
    "tool-registry": (ROOT / "plugin" / "toolkit" / "registry.py",
                      "plugin/tests/test_hermes_tool_contract.py", [
        ("★ 恢复 adapter_getter 参数阴影（外层 getter 被自己的默认值挡住 ⇒ "
         "219 个工具全部回「adapter 未就绪」，一条厂商请求都不发，且不抛异常）",
         "        async def _handler(endpoint=endpoint, method=method,\n"
         "                           transform=td.get(\"transform\"), **params):",
         "        async def _handler(adapter_getter=noop_adapter_getter,\n"
         "                           endpoint=endpoint, method=method,\n"
         "                           transform=td.get(\"transform\"), **params):"),
        ("account_id 不 pop（路由键被当成厂商参数原样发出去 —— 模型照 schema 填的 "
         "那个键会跟着请求体一起发给厂商）",
         '            account_id = resolve_account_id(adapter, params.pop("account_id", "default"))',
         '            account_id = resolve_account_id(adapter, params.get("account_id", "default"))'),
    ]),

    # 工具策略层（W4）：撤注册 / 纠错 / 补注册。三条失效形态都**不是崩溃**，
    # 而是"AI 又拿到了本该封掉的能力"或"工具绑到了错端点上还回报成功"。
    "tool-policy": (ROOT / "plugin" / "toolkit" / "policy.py",
                    "plugin/tests/test_hermes_tool_contract.py", [
        ("★ 撤注册表丢掉 wpp_send_app_msg（原始 XML 发应用消息的能力又回到 AI 手里）",
         '    "wpp_send_app_msg",\n', ""),
        ("★ 步数端点退回 /Tools/setproxy（既没改步数，还可能把账号的出口代理清掉，"
         "而厂商照常回成功码）",
         '        "endpoint": "/Tools/SetStep",',
         '        "endpoint": "/Tools/setproxy",'),
        ("步数参数名退回 steps（厂商静默忽略未知名 ⇒ 与那条历史错绑同一种失效）",
         '            "step": {"type": "integer", "required": True, "desc": "步数（最高 98000）"},',
         '            "steps": {"type": "integer", "required": True, "desc": "步数（最高 98000）"},'),
        ("补注册的 setproxy 端点写错（TS 有、py2 没有的那条能力静默回归）",
         '         "endpoint": "/Tools/setproxy",',
         '         "endpoint": "/Tools/SetStep",'),
    ]),

    # 入站落库（W1）：13 列映射 + "永不抛"。失效形态是**写不进去但链路照跑**
    # （异常被吞成一条 warn），所以判据必须落在"写下去的那 13 列"上。
    "inbound-persist": (ROOT / "plugin" / "inbound" / "persist.py",
                        "plugin/tests/test_inbound_persist.py", [
        ("★ peer_kind 不再映射（私聊写 dm ⇒ ENUM('direct','group','room') 拒收 1265 ⇒ "
         "私聊一条都不落库，而日志只有一行 warn）",
         '    return _KIND_GROUP if str(peer_kind or "").strip().lower() == _KIND_GROUP else _KIND_DIRECT',
         '    return str(peer_kind or "")'),
        ("★ chat_id 不再按「是不是群」分岔（私聊也写 id ⇒ 列注释说的「群 ID」变成任意会话 id，"
         "按群查历史的分支语义糊掉）",
         "    return (peer_id or None) if is_group else None",
         "    return peer_id or None"),
        ("★ direction 不再映射（解析器默认值 incoming 直写 ⇒ ENUM 拒收 ⇒ 入站整批落不下来）",
         '    return _DIRECTION_OUTBOUND if str(raw or "").strip().lower().startswith("out") else _DIRECTION_INBOUND',
         '    return str(raw or "")'),
        ("新消息 id 取不到时**冒充**客户端 id（群友引用 bot 消息带的是 svrid ⇒ 永远匹配不上）",
         '        "new_msg_id": pick_new_msg_id(getattr(msg, "raw", None)),',
         '        "new_msg_id": pick_new_msg_id(getattr(msg, "raw", None))'
         ' or (str(getattr(msg, "msg_id", "") or "") or None),'),
        ("packed new_msg_id 不解包（{\"string\": …} 被 str() 成 Python dict 字面量 ⇒ "
         "看着有值、其实永远匹配不上）",
         "            val = _safe_str(raw.get(key))",
         "            val = str(raw.get(key) or \"\")"),
        ("msg_type 不转字符串（列是 VARCHAR(32)；蓝本是 String(msgType)）",
         '        "msg_type": str(getattr(msg, "msg_type", "") or "") or None,',
         '        "msg_type": getattr(msg, "msg_type", "") or None,'),
        ("raw_payload 不落（审计/回放那一列恒空 —— 事后查不了原始 vendor 报文）",
         '        "raw_payload": getattr(msg, "raw", None),',
         '        "raw_payload": None,'),
        ("★ 落库失败改成抛（对齐 TS enrich 的「吞错返 saved:false」被拆掉 ⇒ "
         "写库一抖就打断收信，消息连心流都进不去）",
         "        return False\n",
         "        raise\n"),
    ]),

    # 适配器上的两处接线（W1）：落库的位置 + 出站那两个真 bug 的收口。
    # 判据文件是同一个（它按 AST 读 adapter.py 的形状）。
    "adapter-inbound-persist": (ADAPTER, "plugin/tests/test_inbound_persist.py", [
        ("★ 入站落库整条拆掉（心流缓冲/judge/LLM 上下文又只剩 bot 自己说的话，"
         "而「注册面全绿」那种观感一模一样）",
         "        await asyncio.to_thread(persist_inbound, acct, msg)",
         "        pass"),
        ("★ 出站 peer_kind 绕过映射（私聊出站写 dm ⇒ ENUM 拒收 ⇒ 出站记录静默丢，"
         "与入站那半重新分叉）",
         '                peer_kind=db_peer_kind("group" if is_group else "dm"),',
         '                peer_kind="group" if is_group else "dm",'),
        ("★ 出站 chat_id 退回编码串（accountId:peerId ⇒ 按群查历史两个分支都不命中 ⇒ "
         "出站群消息整个隐形，正是 W1 修掉的那个真 bug）",
         "                chat_id=db_chat_id(peer_id, is_group=is_group),",
         "                chat_id=self._chat_id(acct.id, peer_id),"),
    ]),

    # 账号写回（W6）：落盘口。失效形态全是"回了 ✅ 而实际没生效 / 实际改坏了"，
    # 所以判据必须是**读回文件逐键对**与**跑一遍 resolve() 看生效值**。
    "account-writeback": (ROOT / "plugin" / "core" / "account_store.py",
                          "plugin/tests/test_account_store.py", [
        ("★ 凭证键不再拦（一条 /user 类命令就能把 authcode 改坏 ⇒ 收信立刻停，"
         "而回执还是 ✅）",
         "    if k in FORBIDDEN_KEYS:",
         "    if False:"),
        ("★ 读不出来也照写（坏 JSON / 缺文件被当成空配置覆盖 ⇒ 平台再也起不来）",
         '        raise AccountWriteError(f"账号文件不存在：{path}（写回会凭空造一份残缺配置）")',
         "        return {}"),
        ("★ 从零拼一份写回（只留本次改的那个键 ⇒ 本树不建模的键 + 凭证**静默全删**）",
         "        data[k] = value\n        _atomic_write(path, data)",
         "        data = {k: value}\n        _atomic_write(path, data)"),
        ("★ 写完不清同键覆盖（③ 层压在 ① 层上 ⇒ 文件改了、链路读到的还是旧值）",
         "        _clear_shadow_override(account_id, k)",
         "        pass"),
        ("★ 不备份就写（改错了没有回退点；老板定的「写前必须备份」落空）",
         "        backup = _backup(path)                        # 再备份（备不了就不写）",
         "        backup = path"),
        ("★ 权限退回 644（文件里有 authcode；实测 .183 上原来就是 644，同机任何人可读）",
         "        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)",
         "        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)"),
        ("★ 路径穿越守卫拆掉（账号 id 直接当文件名 ⇒ 一条命令能写到 accounts/ 之外）",
         '    if not aid or "/" in aid or "\\\\" in aid or aid in (".", ".."):',
         "    if False:"),
        ("★ 备份失败也照写（「改了但没得回退」比这次没生效更坏）",
         '        raise AccountWriteError(f"备份失败（未做任何改动）：{dst}: {exc}") from exc',
         "        return dst"),
    ]),

    # 命令面（W6）：**承诺**必须与实现一致。失效形态 = 回 ✅ 而没落盘 / 没生效 /
    # 把内部异常原文（可能带文件内容）发到微信里。
    "account-writeback-cmd": (ROOT / "plugin" / "platform" / "commands.py",
                              "plugin/tests/test_account_store.py", [
        ("★ 写失败也回成功（旧实现「写内存永不失败」的形态复活 ⇒ 又说谎）",
         '        return f"❌ 写入账号配置失败，**本次改动没有生效**：{head}"',
         '        return ""'),
        ("★ 未知异常的原文直接回给用户（回执走微信 ⇒ 文件内容/凭据可能被打出去）",
         '        return f"❌ 写入账号配置失败，**本次改动没有生效**：{exc.__class__.__name__}（详见日志）"',
         '        return f"❌ 写入账号配置失败，**本次改动没有生效**：{exc}"'),
        ("★ 写回后不重载（配置落盘了但**当次不生效**，而回复说「当次生效」）",
         "        adapter.reload_account(account_id)\n        return True",
         "        return True"),
    ]),

    # 适配器接线（W6）：单靠"文件写对了"还不够 —— 链路读的是缓存那份 Account
    # 与闭包里绑定的旧快照。失效形态 = "文件改了、行为照旧"，观感与成功一样。
    "account-writeback-adapter": (ADAPTER, "plugin/tests/test_account_store.py", [
        ("★ 入站不取最新 Account（WS/webhook 闭包绑的是 connect() 时的旧快照 ⇒ "
         "命令改的白名单/群策略永远走不到唯一活着的入站路径上）",
         "        acct = self._accounts.get(acct.id, acct)",
         "        acct = acct"),
        ("★ 重载不刷管线 cfg（准入判据读的是 connect() 时那份快照 ⇒ 白名单改了不生效）",
         "            pipeline.cfg = self._pipeline_cfg(acct)",
         "            pass"),
        ("★ 命令调用点不传 adapter（写回变成「落盘但不当次生效」）",
         "commands.handle_command(acct.id, content, adapter=self)",
         "commands.handle_command(acct.id, content)"),
    ]),

    # 好感度算法（W2）。失效形态**全是"数字变得不像话"而不是崩溃**：多扣一分、
    # 骂不动的 bot、账单翻倍 —— 累计一个月的数据才看得出来。
    "affection-core": (ROOT / "plugin" / "inbound" / "affection.py",
                       "plugin/tests/test_affection.py", [
        ("★ 默认开关翻成开（fail-open：老板没开的功能自己跑起来，DB 写与 LLM 账单一起变，"
         "而状态页显示的还是那个默认值）",
         "    enabled: bool = False",
         "    enabled: bool = True"),
        ("★ 解析器的 fail-closed 字面量翻成开（**真正生效的那一层**：键缺失的账号自己开跑 —— "
         "上面那条 dataclass 默认只是文档，两条都要钉，否则文档会悄悄跟实现分叉）",
         '            enabled=_as_bool(d.get("enabled"), False),',
         '            enabled=_as_bool(d.get("enabled"), True),'),
        ("★ THREAT 排到 NEGATIVE 之后（「去死」同时命中两表 ⇒ 从「威胁 −12 + anxious」"
         "降级成「侮辱 −8 + sad」；顺序即优先级，`:226`）",
         '    (THREAT_KEYWORDS, "threat"),\n    (NEGATIVE_KEYWORDS, "insult"),',
         '    (NEGATIVE_KEYWORDS, "insult"),\n    (THREAT_KEYWORDS, "threat"),'),
        ("★ 情绪修正改 floor（负向交互多扣：trunc(-3.2) = -3 → floor = -4，"
         "骂一句比蓝本狠一刀）",
         "        actual_change = math.trunc(rule.base_change * mod)",
         "        actual_change = math.floor(rule.base_change * mod)"),
        ("★ `|moodEffect| < 0.1` 闸拆掉（每次闲聊都写一次库，情绪强度还会被噪声推着漂）",
         "    if abs(mood_effect) < 0.1:\n        return None",
         "    if False:\n        return None"),
        ("★ 规则大小写不敏感被拆（`Thanks`/`THX` 判不出来 ⇒ 白夸一句一分不加）",
         '    lower = (message or "").lower().strip()',
         '    lower = (message or "").strip()'),
        ("★ LLM 幻觉类型不再校验（`{\"type\": \"anger\"}` 直进规则表 ⇒ 查空）",
         "    if isinstance(t, str) and t in AFFECTION_RULES:",
         "    if isinstance(t, str):"),
        ("重分配 `max(1, …)` 退化（比例算出来 0 ⇒ 扣不动，群总量永远超限）",
         "        decrease_amount = max(1, math.trunc(level * decrease_ratio * decay_rate))",
         "        decrease_amount = math.trunc(level * decrease_ratio * decay_rate)"),
        ("上限也挡负向（满好感时骂人不再扣分 ⇒ 骂不动的 bot）",
         "    if current_level >= max_user_affection and actual_change > 0:",
         "    if current_level >= max_user_affection:"),
    ]),

    # 好感度落库层（W2）。判据是 **SQL 形状**：这一层的失效形态是"写进去了、读回来是错的"，
    # 而且在真机上看起来一切正常（表在涨、状态页有数）。
    "affection-repo": (ROOT / "plugin" / "data" / "repo_affection.py",
                       "plugin/tests/test_affection.py", [
        ("★ 情绪行的哨兵不再排除（情绪行被当成一个 level=0 的群成员：人数 +1、"
         "重分配可能去扣那个空 wxid）",
         "        \"WHERE account_id = %s AND group_id = %s AND user_id <> '' \"",
         "        \"WHERE account_id = %s AND group_id = %s \""),
        ("★ 群总量把负分算进去（一个被骂惨的用户能把全群总量拉下来、重分配永不触发）",
         '"SELECT COALESCE(SUM(GREATEST(level, 0)), 0) AS total FROM wpp_affection "',
         '"SELECT COALESCE(SUM(level), 0) AS total FROM wpp_affection "'),
        ("★ 状态页的计数按群过滤（全账号总量变成一个群的量 —— 数字看着合理，只是小一号）",
         '        "WHERE account_id = %s AND user_id <> \'\'",\n        (account_id,),',
         '        "WHERE account_id = %s AND group_id = %s AND user_id <> \'\'",\n'
         '        (account_id, account_id),'),
        ("★ 过期情绪不写回（读的时候判过期、也只当次判 ⇒ 每次读都重算一遍，"
         "而「已经判过它过期」这件事没人记得）",
         "        set_mood(account_id, group_id, fresh, conn=conn)",
         "        pass"),
        ("★ 过期比较反向/失效（过期情绪一直生效 ⇒ bot 永远在生闷气）",
         "    if now_sec > expires_at:",
         "    if False:"),
        ("★ upsert 退化成裸 INSERT（同一个人第二次写就 1062 撞键 ⇒ 记账从此中断，"
         "且中断在 fire-and-forget 里只留一行 warn）",
         '        ON DUPLICATE KEY UPDATE level = VALUES(level), updated_at = VALUES(updated_at)',
         ''),
    ]),

    # 好感度接线（W2）。adapter.py 本机 import 不了 ⇒ 裁判是 AST 形状那一套
    # （``plugin/tests/platform/test_affection_wiring.py``）。失效形态**全部"看着正常"**：
    # 收到消息、回了话、状态页也报数，只是账没记 / 账记歪了 / 收信被拖慢。
    "affection-adapter": (ADAPTER, "plugin/tests/platform/test_affection_wiring.py", [
        ("★ 打分被 await（记账的 DB 读改写 + 5s 超时的 LLM 直接塞进收信链路 ⇒ "
         "现场表现只是「偶尔回得慢」）",
         "            self._affection_note_group(acct, msg)",
         "            await self._affection_note_group(acct, msg)"),
        ("★ 打分挪到落库之前、群分支之外（DM 与未过闸的消息也开始计分；顺序判据 H3 当场红）",
         "        await asyncio.to_thread(persist_inbound, acct, msg)",
         "        self._affection_note_group(acct, msg)\n"
         "        await asyncio.to_thread(persist_inbound, acct, msg)"),
        ("★ 任务句柄不存（asyncio 只持弱引用 ⇒ 任务跑到一半被 GC，静默丢账）",
         "        self._affection_tasks.add(task)",
         "        pass"),
        ("★ done-callback 不挂（异常只在 GC 时冒一句 \"Task exception was never "
         "retrieved\"，不说是哪个群、不说为什么）",
         "        task.add_done_callback(self._affection_done)",
         "        pass"),
        ("★ 句柄不回收（集合只涨不落 —— 长跑进程里的慢性内存泄漏）",
         "        self._affection_tasks.discard(task)",
         "        pass"),
        ("★ 情绪注入不接 channel_prompt（情绪状态机照常跑、库照常写，回复语气一点不变）",
         "            channel_prompt=self._mood_channel_prompt(acct, msg),",
         "            channel_prompt=None,"),
        ("★ 系统通知守卫拆掉（10000 系统消息被当成「有人在说话」计分）",
         "        if msg.msg_type == MT_SYSTEM:                    # 系统通知不算人说话\n"
         "            return\n",
         ""),
        ("★ 情绪注入的开关守卫挪到读库之后（没开开关也每条消息查一次库）",
         "        cfg = resolve_affection_cfg(acct.affection)\n"
         "        if not cfg.enabled:\n            return None\n        try:\n",
         "        cfg = resolve_affection_cfg(acct.affection)\n        try:\n"),
    ]),

    # 好感度状态页（W2）。失效形态是"回答得头头是道但数字是编的"，以及
    # **状态页在库坏掉那一刻跟着崩**（那正是最需要它的时候）。
    "affection-status": (ROOT / "plugin" / "platform" / "commands.py",
                         "plugin/tests/platform/test_affection_wiring.py", [
        ("★ 每次都去数好感度（`/jargon status` 也查好感度表 ⇒ 两个功能的状态页被绑在一起，"
         "好感度表没建时黑话开关的状态页也报「读取失败」）",
         '_affection_data_line(account_id) if feature == "affection" else ""',
         "_affection_data_line(account_id)"),
        ("★ 读库失败直接抛（状态页在库坏掉那一刻最有用，却在那时崩掉）",
         '        return "\\n数据: (读取失败，见日志)"',
         "        raise"),
        ("群数与行数对调（两个数各自都是真的，拼起来是假的）",
         'f"\\n数据: 有状态的群 {groups} 个 / 好感度行 {rows} 条"',
         'f"\\n数据: 有状态的群 {rows} 个 / 好感度行 {groups} 条"'),
        ("0 行时只回一个 0（运维会以为功能坏了，其实只是没说过话）",
         '        return "\\n数据: 暂无（好感度只在「过了触发判定④的群消息」上计分）"',
         '        return "\\n数据: 0"'),
    ]),

    # ---- W3 llmIntent（群聊意图）------------------------------------------------
    # 失效形态：**少一段背景 / 多一段噪声**，两者都不报错。
    "intent-core": (ROOT / "plugin" / "inbound" / "intent.py",
                    "plugin/tests/test_intent.py", [
        ("★ 去 @ 之后不 strip（只 @ 不发言也算 topic ⇒ 每条空 @ 都注入上下文）",
         '    stripped = _AT_MENTION_RE.sub("", str(content or "")).strip()',
         '    stripped = _AT_MENTION_RE.sub("", str(content or ""))'),
        ("媒体词命中改判 topic（用户发图 @ 你 ⇒ 那条图不在候选里）",
         '    if _MEDIA_WORD_RE.search(stripped):\n        return "media"',
         '    if _MEDIA_WORD_RE.search(stripped):\n        return "topic"'),
        ("★ 去转写标记的那条正则带上捕获组（把转写正文一起删掉 ⇒ 语音 @ 归一化成空"
         "⇒ 判 no-op ⇒ **语音永远拿不到上下文**）",
         '_STT_MARK_RE = re.compile(r"\\[转写\\]\\s*")',
         '_STT_MARK_RE = re.compile(r"\\[转写\\]\\s*([^\\n]+)")'),
        ("语音前缀不去（归一化文本里留着「收到一条语音」⇒ 判据与 TS 不同）",
         '    if stt:\n        s = _VOICE_PREFIX_RE.sub(" ", s)',
         '    if False:\n        s = _VOICE_PREFIX_RE.sub(" ", s)'),
        ("★ msgId 用 `or` 而不是 `??`（空 msgId 会退到 newMsgId ⇒ 过滤/注入选错消息）",
         '    if msg_id is None:\n        msg_id = record.get("new_msg_id")',
         '    if not msg_id:\n        msg_id = record.get("new_msg_id")'),
        ("inject 不问 relevant_ids 是不是数组（模型回一句字符串就 KeyError）",
         '    if action == "inject" and isinstance(obj.get("relevant_ids"), list):',
         '    if action == "inject":'),
        ("id 数组里不过滤非字符串（数字 id 混进去 ⇒ 后面按 id 过滤全落空）",
         '        ids = tuple(x for x in obj["relevant_ids"] if isinstance(x, str))',
         '        ids = tuple(obj["relevant_ids"])'),
        ("围栏正则不认 ```json（模型按提示词回了 json 围栏 ⇒ 解析失败 ⇒ 恒降级）",
         '_FENCE_RE = re.compile(r"```(?:json)?\\s*([\\s\\S]*?)```")',
         '_FENCE_RE = re.compile(r"```([\\s\\S]*?)```")'),
        ("system prompt 里 bot 名字没有中性兜底（名字空 ⇒ 提示词里一个空档）",
         '    name = (bot_name or "").strip() or "机器人"',
         '    name = (bot_name or "").strip()'),
        ("纯文本摘要上限 50 → 500（喂给 LLM 的候选被正文淹没）",
         '    text = _MEDIA_MARK_RE.sub(" ", c).strip()[:50]',
         '    text = _MEDIA_MARK_RE.sub(" ", c).strip()[:500]'),
    ]),

    "intent-select": (ROOT / "plugin" / "inbound" / "intent.py",
                      "plugin/tests/test_intent.py", [
        ("★ LLM 失败 + topic 也照注入（老板实测「AI 被无关文本带偏」就是这条）",
         '        if decision is None and not embed_selected:\n            if kind != "media":',
         '        if decision is None and not embed_selected:\n            if False:'),
        ("★ 无 LLM 时 media/topic 不分（topic 也注入全部候选）",
         '    else:\n        if kind != "media":',
         '    else:\n        if False:'),
        ("★ 意图不分类（调用方给的 intent 也不算 ⇒ media 降级路径整体失效）",
         '    kind = intent if intent is not None else classify_group_intent(trigger_content)',
         '    kind = intent or "topic"'),
        ("LLM 判 inject 与不判一样（该过滤的不过滤 ⇒ 一次注入全部候选）",
         '            filtered = filter_by_ids(kept, decision.relevant_ids)',
         '            filtered = list(kept)'),
        ("按 id 过滤只认 msg_id（new_msg_id 命中的那条被丢掉）",
         '        if (m.get("msg_id") or "") in id_set or (m.get("new_msg_id") or "") in id_set:',
         '        if (m.get("msg_id") or "") in id_set:'),
        ("media_only 恒空（media 意图的兜底等于不注入）",
         '    return [m for m in msgs if _HAS_MEDIA_RE.search(str(m.get("content") or ""))]',
         '    return []'),
        ("图片裁掉最新的 3 张 → 留下最旧的 3 张（图和问题对不上）",
         '    keep = set(indexes[-max_images:])',
         '    keep = set(indexes[:max_images])'),
        ("图片完全不裁（候选窗口被图占满，文本全被挤掉）",
         '    if len(indexes) <= max_images:\n        return list(msgs)',
         '    if True:\n        return list(msgs)'),
        ("★ 媒体候选不算媒体（纯相似度会把文档/图片挤掉 —— 老板实测的那条）",
         '    media_ids = [x.msg_id for x in candidates\n'
         '                 if x.type in ("file", "image", "voice", "video")]',
         '    media_ids = []'),
        ("文本名额不减媒体数（多注入 N 条无关文本）",
         '    room = max(top_n - len(media_ids), 0)',
         '    room = top_n'),
        ("★ 阈值边界 >= 改 >（分数恰好等于阈值的候选被静默丢掉）",
         '    relevant = [mid for mid, score in scored if score >= threshold][:room]',
         '    relevant = [mid for mid, score in scored if score > threshold][:room]'),
        ("空候选返 None 而不是 []（把「没候选」混成「embedding 坏了」⇒ 白跑一次 LLM）",
         '    if not candidates:\n        return []',
         '    if not candidates:\n        return None'),
    ]),

    "intent-cfg": (ROOT / "plugin" / "inbound" / "intent.py",
                   "plugin/tests/test_intent.py", [
        ("★ 窗口的布尔陷阱不挡（`true` 被当成 1 条窗口 ⇒ 上下文只有一条）",
         '    if isinstance(w, (int, float)) and not isinstance(w, bool) and 1 <= w <= 100:',
         '    if isinstance(w, (int, float)) and 1 <= w <= 100:'),
        ("窗口上界失效（配 10000 就真去查 10000 条）",
         '    if isinstance(w, (int, float)) and not isinstance(w, bool) and 1 <= w <= 100:',
         '    if isinstance(w, (int, float)) and not isinstance(w, bool) and 1 <= w:'),
        ("窗口默认值改成 30（不配的账号悄悄换了窗口）",
         '    return GROUP_CONTEXT_WINDOW',
         '    return 30'),
        ("窗口不取整（10.7 直接进 LIMIT）",
         '        return int(w)',
         '        return w'),
        ("超时脏值不挡（0/负数 ⇒ 每次调用立刻超时 ⇒ 恒降级成「不注入」）",
         "and not isinstance(t, bool) and t > 0 \\\n        else INTENT_DEFAULT_TIMEOUT_MS",
         "else INTENT_DEFAULT_TIMEOUT_MS"),
        ("模型名不 strip（带空格的模型名是不存在的模型）",
         '        model=str(d.get("llmIntentModel") or "").strip(),',
         '        model=str(d.get("llmIntentModel") or ""),'),
        ("bot 名字无兜底（提示词里出现空档）",
         '        bot_name=str(nickname or "").strip() or "机器人",',
         '        bot_name=str(nickname or "").strip(),'),
        ("★ embedding 加隐式兜底 env（任何环境里恰好有那把 key 的账号静默开始花钱）",
         '    key = env_key or str(d.get("embeddingApiKey") or "").strip()',
         '    key = env_key or (os.environ.get("BAILIAN_EMBEDDING_API_KEY") or "").strip()'
         ' or str(d.get("embeddingApiKey") or "").strip()'),
        ("embedding topN 默认 5 → 3（不配的账号换了注入条数）",
         '        top_n=_as_int(d.get("embedIntentTopN"), EMBED_DEFAULT_TOP_N),',
         '        top_n=_as_int(d.get("embedIntentTopN"), 3),'),
    ]),

    # 装配。失效形态全是"看着完全正常"：开关变装饰、上下文缺一段、异常穿透收信链路。
    "intent-adapter": (ADAPTER, "plugin/tests/platform/test_intent_wiring.py", [
        ("★ 总闸守卫变成装饰（body 改成 pass ⇒ 开关关着照样查库/外呼）",
         '        if not acct.group_context_enabled:\n            return None',
         '        if not acct.group_context_enabled:\n            pass'),
        ("非群消息守卫变成装饰（DM 也去查群消息）",
         '        if not msg.is_group:\n            return None\n'
         '        if not acct.group_context_enabled:',
         '        if not msg.is_group:\n            pass\n'
         '        if not acct.group_context_enabled:'),
        ("★ 不排除触发消息自身（before_sec 传 None ⇒ 上下文里混进用户刚 @ 你的那句）",
         'limit=resolve_intent_window(acct.raw), before_sec=ts,',
         'limit=resolve_intent_window(acct.raw), before_sec=None,'),
        ("干脆不传 before_sec",
         '                limit=resolve_intent_window(acct.raw), before_sec=ts,\n',
         '                limit=resolve_intent_window(acct.raw),\n'),
        ("窗口硬编码 10（账号的 groupContextWindow 改不动）",
         'limit=resolve_intent_window(acct.raw), before_sec=ts,',
         'limit=10, before_sec=ts,'),
        ("群 id 只用 peer_id（两种落库形态里的 chat_id 那条查不到）",
         '                acct.id, msg.chatroom_id or msg.peer_id,\n',
         '                acct.id, msg.peer_id,\n'),
        ("★ None（不注入）与空列表混为一谈",
         '            if kept is None:                      '
         '# None = "不注入"，不是"注入空"',
         '            if not kept:'),
        ("空文本也当上下文返回（模型收到一段空上下文）",
         '            if text:\n',
         '            if True:\n'),
        ("★ 上下文没接上 MessageEvent",
         '            channel_context=await self._intent_channel_context(acct, msg),\n',
         ''),
        ("上下文接成常量 None（总闸开着也永远不注入）",
         'channel_context=await self._intent_channel_context(acct, msg),',
         'channel_context=None,'),
        ("★ 注入失败改成上抛（一次 DB 抖动 ⇒ 整条消息被丢）",
         '            log.warning("[WPP:%s] 群聊上下文注入失败（照常回复）: %s", acct.id, exc)',
         '            raise'),
        ("LLM 那半的开关变装饰（llmIntentEnabled=false 也去调 LLM）",
         '        if not acct.llm_intent_enabled:\n            return None',
         '        if False:\n            return None'),
        ("bot 名字不取账号昵称（提示词里称呼错人）",
         '        return resolve_intent_llm_cfg(acct.raw, nickname=acct.nickname)',
         '        return resolve_intent_llm_cfg(acct.raw, nickname="机器人")'),
        ("适配器里另起一份模型解析（解析有了第二个家，改一处不生效）",
         '        return resolve_intent_llm_cfg(acct.raw, nickname=acct.nickname)',
         '        return IntentLlmConfig(model=str(acct.raw.get("llmIntentModel") or ""))'),
    ]),

    # ---- W5 增量兜底 + 心跳 -----------------------------------------------------
    # 失效形态：**一封告警都不出**。兜底坏了只表现为"某些消息没人回"（群里无法归因），
    # 而"首次回放历史"更糟 —— 机器人去回复旧消息。判据一律下在"盘上真发生了什么、按什么顺序"。
    "sync-core": (ROOT / "plugin" / "core" / "sync_fallback.py",
                  "plugin/tests/test_sync_fallback.py", [
        ("★ 首次也回放历史（新进程内存去重是空的 ⇒ 上千条历史走完 _handle_one ⇒ 回复旧消息）",
         '            if not prev:\n',
         '            if False:\n'),
        ("★ 首次不回放但**也不落基线**（永远停在'首次' ⇒ 兜底永不派发）",
         '                if new_key:\n'
         '                    await self._write_cursor(new_key)\n'
         '                    log.info("[WPP:%s SYNC] 首次同步：只建立游标（厂商回了 %d 条历史，"',
         '                if new_key:\n'
         '                    log.info("[WPP:%s SYNC] 首次同步：只建立游标（厂商回了 %d 条历史，"'),
        ("★ 游标先前进、后派发（崩溃窗口里那批消息永久丢失 —— TS 的 P1 教训）",
         '                      self.account_id, len(items), reason)\n'
         '            failed: list[str] = []\n'
         '            for item in items:',
         '                      self.account_id, len(items), reason)\n'
         '            if new_key:\n'
         '                await self._write_cursor(new_key)\n'
         '            failed: list[str] = []\n'
         '            for item in items:'),
        ("★ 空 KeyBuf 也拿来回写游标（每次都退化成首次全量 ⇒ 重放风暴）",
         '            if new_key:\n'
         '                await self._write_cursor(new_key)\n'
         '            else:\n'
         '                log.debug("[WPP:%s SYNC] 未返回 KeyBuf，游标不动（reason=%s）",',
         '            if True:\n'
         '                self._write_key(new_key)\n'
         '            else:\n'
         '                log.debug("[WPP:%s SYNC] 未返回 KeyBuf，游标不动（reason=%s）",'),
        ("★ in-flight 锁变装饰（WS 风暴/定时器/开连同时来 ⇒ 两个游标互相踩）",
         '        if self._in_flight:\n            log.debug(',
         '        if False:\n            log.debug('),
        ("异常路径不放开锁（一次网络抖动 ⇒ 兜底永久停止）",
         '        finally:\n            self._in_flight = False',
         '        finally:\n            pass'),
        ("★ 拉取失败上抛（兜底故障把收信链路带崩）",
         '        except Exception as exc:  # noqa: BLE001 —— 兜底失败不许影响收信\n'
         '            log.warning("[WPP:%s SYNC] /Msg/Sync 失败（reason=%s）: %s", self.account_id, reason, exc)\n'
         '            return 0',
         '        except Exception as exc:  # noqa: BLE001 —— 兜底失败不许影响收信\n'
         '            raise'),
        ("一条坏消息断掉整批（后面那些消息全丢）",
         '                except Exception as exc:  # noqa: BLE001 —— 单条坏消息不许断整批\n'
         '                    failed.append(str((item or {}).get("MsgId")\n'
         '                                      or (item or {}).get("NewMsgId") or "?"))\n'
         '                    log.warning("[WPP:%s SYNC] 派发失败（继续下一条）: %s", self.account_id, exc)',
         '                except Exception as exc:  # noqa: BLE001 —— 单条坏消息不许断整批\n'
         '                    raise'),
        ("★ 心跳不看连接态（断线后照样发 HTTP 心跳，刷错误日志）",
         '        if not self.is_connected():\n            return',
         '        if False:\n            return'),
        ("★ 开连不重开 AutoHeartBeat（新 ws 会话没告知厂商 ⇒ 跑一阵就掉线）",
         '        if reason == REASON_OPEN:\n            await self.auto_heartbeat()',
         '        if False:\n            await self.auto_heartbeat()'),
        ("★ 帧通知也去重开 AutoHeartBeat（每条消息打一次厂商）",
         '        if reason == REASON_OPEN:\n            await self.auto_heartbeat()',
         '        if True:\n            await self.auto_heartbeat()'),
        ("★ 读游标失败当成「没有游标」（拿空游标拉全窗、再把基线覆盖 ⇒ 上一窗静默跳过）",
         '            return _READ_FAILED',
         '            return None'),
        ("存游标失败上抛（幂等安全的降级变成把派发链路带崩）",
         '            log.warning("[WPP:%s SYNC] 存游标失败（下次退化为全量拉）: %s", self.account_id, exc)',
         '            raise'),
        ("KeyBuf 非字符串不校验（把 123 这种假游标存下去）",
         '        buf = kb.get("buffer")\n        return buf if isinstance(buf, str) else ""',
         '        buf = kb.get("buffer")\n        return buf or ""'),
        ("★ 心跳定时器改去拉取（保活没了，只剩拉取）",
         '            asyncio.create_task(self._tick_loop(lambda _r: self.heartbeat(), "heartbeat-timer"),',
         '            asyncio.create_task(self._tick_loop(self.trigger, "heartbeat-timer"),'),
    ]),

    "sync-ws": (ROOT / "plugin" / "platform" / "inbound_ws.py",
                "plugin/tests/platform/test_sync_wiring.py", [
        ("★ emit 的字符串与 REASON_OPEN 不一致（每次连上都被当成 push ⇒ 不重开自动心跳）",
         '                    self._emit("ws-open")',
         '                    self._emit("ws_open")'),
        ("★ connection_ready 也触发拉取（每次握手 ack 白打一次厂商）",
         '            if data.get("type") == "sync_update":\n                self._emit("sync-update")',
         '            if data.get("type") in _IGNORED_TYPES:\n                self._emit("sync-update")'),
        ("★ 心跳帧不再 return（没有消息体的帧被塞给 on_raw ⇒ 每次心跳一条解析失败告警）",
         '            if data.get("type") == "sync_update":\n'
         '                self._emit("sync-update")\n'
         '            return',
         '            if data.get("type") == "sync_update":\n                self._emit("sync-update")'),
        ("★ 钩子被 await（在 WS 读循环里等一趟 HTTP ⇒ 收帧一起被堵住）",
         '                    self._emit("ws-open")',
         '                    await self._emit("ws-open")'),
        ("★ 钩子异常穿透（旁路把连接搞断）",
         '            log.warning("[WPP:%s WS] 事件钩子 %s 失败: %s", self.account_id, reason, exc)',
         '            raise'),
        ("没接钩子时不早返回（on_event is None ⇒ TypeError，每次连上都炸一次）",
         '        if self.on_event is None:\n            return',
         '        if False:\n            return'),
        ("★ 连上不置 connected（心跳永不发 ⇒ vendor idle 超时断连）",
         '                    self.connected = True\n'
         '                    log.info("[WPP:%s WS] 已连接", self.account_id)',
         '                    log.info("[WPP:%s WS] 已连接", self.account_id)'),
        ("★ 断开不置 connected（断线后照样发心跳；且这里只在 finally 里才覆盖得到）",
         '                    finally:\n                        self.connected = False',
         '                    finally:\n                        pass'),
        ("stop() 不清连接态（停机过程中还在发心跳）",
         '        self._stop.set()\n        self.connected = False',
         '        self._stop.set()'),
        ("connected 初值改成 True（没连上就发心跳 —— fail-open）",
         '        self.connected = False\n'
         '\n'
         '    # ---------------------------------------------------------------- 生命周期',
         '        self.connected = True\n'
         '\n'
         '    # ---------------------------------------------------------------- 生命周期'),
        ("★ 关掉 WS 原生 ping（只剩 HTTP 心跳 ⇒ vendor 按 idle 每 10min 断一次）",
         '                    ping_interval=20, ping_timeout=20, close_timeout=5,',
         '                    ping_interval=None, ping_timeout=20, close_timeout=5,'),
    ]),

    "sync-adapter": (ADAPTER, "plugin/tests/platform/test_sync_wiring.py", [
        ("★ 没把 on_event 交给 WS 客户端（push 触发链整条不存在，而其它判据全绿）",
         '                on_event=lambda reason, a=acct.id: self._on_ws_event(a, reason),\n',
         ''),
        ("★ on_event 不透传 reason（把每次通知都当 ws-open ⇒ 每条消息重开一次自动心跳）",
         '                on_event=lambda reason, a=acct.id: self._on_ws_event(a, reason),',
         '                on_event=lambda reason, a=acct.id: self._on_ws_event(a, REASON_OPEN),'),
        ("★ is_connected 写成捕获下来的 bool（断线后照样发心跳）",
         '                is_connected=lambda w=ws: bool(w is not None and w.connected),',
         '                is_connected=lambda w=ws: bool(w is not None),'),
        ("★ is_connected 丢了默认参数闭包（多账号共用最后一个 ws）",
         '                is_connected=lambda w=ws: bool(w is not None and w.connected),',
         '                is_connected=lambda: bool(ws is not None and ws.connected),'),
        ("★ 拉回来的条目没接回统一入口（拉了个寂寞）",
         '                on_message=lambda item, a=acct: self._on_raw(a, item),',
         '                on_message=lambda item, a=acct: asyncio.sleep(0),'),
        ("★ 兜底建了不 start（定时器一个都没起，看着一切正常）",
         '            sync.start()\n            self._sync[acct.id] = sync',
         '            self._sync[acct.id] = sync'),
        ("★ 兜底没登记（disconnect 停不到它 ⇒ 孤儿定时器打已关闭的 client）",
         '            sync.start()\n            self._sync[acct.id] = sync\n',
         '            sync.start()\n'),
        ("★ 重复 connect 的闸变装饰（挂两个兜底 ⇒ 两个游标互相踩、静默丢消息）",
         '                        acct.id)\n            return\n        try:\n            sync = SyncFallback(',
         '                        acct.id)\n        try:\n            sync = SyncFallback('),
        ("★ WS 的 except 里 return（等于「WS 起不来就别起兜底」—— 那正是最需要它的时候）",
         '            log.error("[WPP:%s] WS 启动失败: %s", acct.id, exc, exc_info=True)\n',
         '            log.error("[WPP:%s] WS 启动失败: %s", acct.id, exc, exc_info=True)\n'
         '            return\n'),
        ("★ disconnect 里没真停（循环留着，停的动作没了）",
         '                await sync.stop()',
         '                await asyncio.sleep(0)'),
        ("★ disconnect 停的是个空集合（`list(self._sync.items())` 才是真遍历）",
         '        for aid, sync in list(self._sync.items()):',
         '        for aid, sync in []:'),
    ]),

    "sync-api": (ROOT / "plugin" / "core" / "api_client.py",
                 "plugin/tests/platform/test_sync_wiring.py", [
        ("★ Synckey 字段名拼错（vendor 静默忽略 ⇒ 每次都全量拉 —— 重放风暴）",
         '{"Scene": 0, "Synckey": prev_synckey or ""}',
         '{"Scene": 0, "SyncKey": prev_synckey or ""}'),
        ("★ 漏掉 Scene（vendor 拿不到会话上下文）",
         '{"Scene": 0, "Synckey": prev_synckey or ""}',
         '{"Synckey": prev_synckey or ""}'),
        ("★ 缺省游标发 None 而不是空串（vendor 那边是 Go 的 string 字段）",
         '{"Scene": 0, "Synckey": prev_synckey or ""}',
         '{"Scene": 0, "Synckey": prev_synckey}'),
        ("★ /Msg/Sync 走 GET（body 丢掉 ⇒ 每次都全量）",
         'return await self.call("/Msg/Sync", {"Scene": 0, "Synckey": prev_synckey or ""})',
         'return await self.call("/Msg/Sync", {"Scene": 0, "Synckey": prev_synckey or ""}, method="GET")'),
        ("★ HeartBeatLong 打成短心跳那条（保活强度不同，且它另有用途）",
         '        return await self.call("/Login/HeartBeatLong")',
         '        return await self.call("/Login/HeartBeat")'),
        ("★ AutoHeartBeat 打成短心跳那条（厂商侧自动维持长连接根本没开）",
         '        return await self.call("/Login/AutoHeartBeat")',
         '        return await self.call("/Login/HeartBeat")'),
    ]),

    "intent-repo": (ROOT / "plugin" / "data" / "repo_messages.py",
                    "plugin/tests/platform/test_intent_wiring.py", [
        ("★ 严格小于改 <=（同一秒的其它消息也进上下文）",
         '"  AND ts < FROM_UNIXTIME(%s)\\n"',
         '"  AND ts <= FROM_UNIXTIME(%s)\\n"'),
        ("★ before_sec=0 被当成「要过滤」（空窗口 ⇒ 上下文静默消失）",
         '    if before_sec:',
         '    if before_sec is not None:'),
        ("过滤条件整条不生效（触发消息被喂回给意图判断）",
         '    if before_sec:',
         '    if False:'),
        ("加了条件但不绑参数（SQL 与参数错位）",
         '        params.append(int(before_sec))\n',
         ''),
        ("参数少了群 id 那一份（两种落库形态的兼容分支拿不到值）",
         '    params: list = [account_id, group_id, group_id]',
         '    params: list = [account_id, group_id]'),
        ("★ chat_id 两种落库形态的兼容分支被拆（W1 落的历史行查不到）",
         '          AND (chat_id = %s OR (chat_id IS NULL AND peer_id = %s))\n',
         '          AND chat_id = %s\n'),
    ]),

    "app-card": (ROOT / "plugin" / "inbound" / "app_card.py",
                 "plugin/tests/test_app_card.py", [
        ("★ 分类判据放宽成「有 category 就算小卡片」（接龙/文件/引用全被当成小程序）",
         '    return _str(_as_record(_as_record(raw).get("app")).get("category"))'
         ' == CATEGORY_MINI_PROGRAM',
         '    return bool(_as_record(_as_record(raw).get("app")).get("category"))'),
        ("★ 空 cover 块被判成「没有封面」（JS 的 {} 是真值、Python 是假值）",
         '    if isinstance(cover_raw, dict):',
         '    if cover:'),
        ("★ bool 当成数字（version: true 变成 1 混进格式串）",
         '    if isinstance(value, bool):\n        return None\n    if isinstance(value, int):',
         '    if isinstance(value, int):'),
        ("download_context 不再优先（顶层零散字段盖掉官方直链）",
         '            url=_first(dc.get("url"), cover.get("url")),',
         '            url=_first(cover.get("url"), dc.get("url")),'),
        ("封面长度的 cdn_length 兜底拆掉（0 或 None 进转发参数）",
         '            length=_first_num(cover.get("cdn_length"), cdn.get("length")),',
         '            length=_num(cover.get("cdn_length")),'),
        ("描述与小程序名相同时照打一行（国补卡片实测两者同值 ⇒ 正文重复）",
         '    if info.description and info.description != info.source_display_name:',
         '    if info.description:'),
        ("★ omit_title 判据拆掉（厂商 content 本身就是标题 ⇒ 标题打两遍）",
         '        omit_title=(not card.title) or (card.title in _str(getattr(msg, "content", ""))),',
         '        omit_title=False,'),
        ("★ 图标 URL 混标成封面（模型以为看到了 720×576 大图）",
         '        cover_url=url if kind == "cover" else "",',
         '        cover_url=url,'),
        ("小程序名回退链拆掉（两处都没有就是空行，模型看不出是哪个小程序）",
         '    who = info.source_display_name or info.username or "小程序"',
         '    who = info.source_display_name or info.username'),
        ("★ 图标与封面同 URL 时也再试一遍（同一次外呼打两遍）",
         '    if cover.icon_url and cover.icon_url != cover.url:',
         '    if cover.icon_url:'),
        ("★ OSS 凭据检查拆掉（没凭据也先打一次厂商 —— 白跑的外呼）",
         '    if load_oss_config() is None:\n'
         '        return AssetResult(error="oss credentials missing")\n',
         ''),
        ("★ 太小的载荷不判（一张 3 字节的「图」照样传 OSS）",
         '            if len(buf) < MIN_ASSET_BYTES:\n'
         '                raise RuntimeError(f"too small (len={len(buf)})")\n',
         ''),
        ("★ 上传 kind 写错（OSS 目录按 image 归档，回头按 miniprogram 检索全查不到）",
         '                                          account_id=account_id, kind=OSS_KIND)',
         '                                          account_id=account_id, kind="image")'),
        ("★ 响应字段名大小写写错（Image → image ⇒ 每条都判「missing Image」）",
         '            b64 = _str(_as_record(_as_record(resp).get("Data")).get("Image"))',
         '            b64 = _str(_as_record(_as_record(resp).get("Data")).get("image"))'),
        # ⚠️ 这一条的锚点**必须带整行**（含 noqa 指令与那串中文注释）：只锚到
        # ``except Exception as exc:`` 的话，替换后会留下 ``: —— 富化失败绝不能冒泡…``
        # 这种悬空尾巴 ⇒ 收集期 SyntaxError ⇒ 脚本自曝 BROKEN（BROKEN 不算杀，
        # 等于这条白写）。第一版就是踩了这个 —— 而 BROKEN 只打印最后一行，
        # 现场看起来像"变异体把测试带崩了"，其实是我们自己写坏了变异。
        ("★ 富化不再吞异常（一次 OSS 抖动 = 丢消息，不只是丢图）",
         '        except Exception as exc:  # noqa: BLE001 —— 富化失败绝不能冒泡成"消息处理失败"\n',
         '        except ValueError as exc:  # noqa: BLE001 —— 富化失败绝不能冒泡成"消息处理失败"\n'),
    ]),

    "app-card-tag": (ROOT / "plugin" / "inbound" / "parser.py",
                     "plugin/tests/test_app_card.py", [
        ("★ 小程序卡片又掉回最后那道 [文件] 兜底（模型只答「你发了个文件」）",
         '        if is_mini_program_card(msg.raw):',
         '        if False and is_mini_program_card(msg.raw):'),
        ("★ 追加块不再生成（解析都对、正文里一个字都不多）",
         '        block = mini_program_block(msg)',
         '        block = ""'),
        ("★ 块被前置到正文之前（TS 是追加；顺序变了下游引用/解析口径全变）",
         '            text = f"{text}\\n{block}"',
         '            text = f"{block}\\n{text}"'),
        ("★ 空块也追加（非卡片消息的正文尾巴多一个换行 ⇒ 不再逐字节相同）",
         '        if block:',
         '        if True:'),
    ]),

    "app-card-adapter": (ADAPTER, "plugin/tests/platform/test_app_card_wiring.py", [
        ("★ 分支排到 `if not msg.media` 那道闸之后（整类卡片走不到它，现象是「卡片没图」）",
         '        if mtype == MT_APP and is_mini_program_card(msg.raw):',
         '        if not msg.media:\n            return [], []\n'
         '        if mtype == MT_APP and is_mini_program_card(msg.raw):'),
        ("★ 少了 msgType 那道闸（每条消息都要解析一次 app 块）",
         '        if mtype == MT_APP and is_mini_program_card(msg.raw):',
         '        if is_mini_program_card(msg.raw):'),
        ("★ 键名写成驼峰（富化成功、正文里一个字都不多 —— 跨模块约定没人管）",
         '                    msg.media["mp_asset_url"] = result.media_url',
         '                    msg.media["mpAssetUrl"] = result.media_url'),
        ("★ kind 键名写错（同上一族；URL 到了、谁都不标它）",
         '                    msg.media["mp_asset_kind"] = result.kind',
         '                    msg.media["mp_asset_type"] = result.kind'),
        ("★ 写侧条件加 and False（拿了图也白拿）",
         '                if result.media_url:',
         '                if result.media_url and False:'),
        ("★ 写侧条件写反（失败当成功、成功当失败）",
         '                if result.media_url:',
         '                if not result.media_url:'),
        ("★ 降级支一声不吭（真机上只剩「卡片没图」，查不出为什么）",
         '                    log.info("[WPP:%s] 小程序卡片封面未拿到（非致命）: %s", acct.id, result.error)',
         '                    pass'),
        ("★ 没有 cover_image 也去富化（每张卡片白跑一次厂商）",
         '            if card is not None and card.cover is not None:',
         '            if card is not None:'),
        ("★ 早退的元组形状被改坏（调用方 `a, b = await …` 立刻 ValueError）",
         '        if not msg.media:\n            return [], []',
         '        if not msg.media:\n            return []'),
        ("★ 富化根本不调（整段接线成摆设）",
         '                result = await enrich_mini_program_asset(client, acct.id, card.cover)',
         '                result = None'),
    ]),
    # ── W7-1（2026-10-02）：媒体富化 —— OSS 直链写回正文 ─────────────────────
    # 这一族的失效形态是"上传成功、日志干净、正文里那行 `[图片]` 就是没有 URL"：
    # 心流抠图只认 `^https?://` ⇒ 看图整段空转，而没有任何一行日志是红的。
    # 所以判据全下在**正文的字节**与**降级分支写了没有**上。
    "media-enrich": (ADAPTER, "plugin/tests/test_media_enrich.py", [
        ("★ 四个串的形状（除文件外都印成 `[图片]`，视频/语音的 URL 后面牌子是错的）",
         '    return f"[{_OSS_KIND_TAG[kind]}] {url}"',
         '    return f"[图片] {url}"'),
        ("★ 文件那行丢掉名字与字节数（模型只看到一个光 URL）",
         '    if kind == KIND_FILES:\n        return f"[文件] {filename} ({size} bytes) {url}"',
         '    if kind == KIND_IMAGES:\n        return f"[文件] {filename} ({size} bytes) {url}"'),
        ("★ `bytes` 字样丢了（下游按这个记认文件行）",
         '        return f"[文件] {filename} ({size} bytes) {url}"',
         '        return f"[文件] {filename} ({size}) {url}"'),
        ("★ 注入写成**前缀**（与 `_media_tag` 那套混血 ⇒ 正文两个 `[图片]`）",
         '    msg.content = f"{msg.content}\\n{line}"',
         '    msg.content = f"{line}\\n{msg.content}"'),
        ("★ 幂等闸拆掉（同一条消息取两次媒体 ⇒ 正文两行一样的 `[图片] url`）",
         '    if line in msg.content:\n        return',
         '    if False:\n        return'),
        ("★ `oss_kind` 写成字面量（诚实字段变谎话：图标标成封面那种错）",
         '    msg.media["oss_kind"] = kind',
         '    msg.media["oss_kind"] = KIND_IMAGES'),
        ("★ 抑制的键拿错（写成小程序封面那个 `mp_asset_url`）",
         '    msg.media["oss_url"] = url',
         '    msg.media["mp_asset_url"] = url'),
        ("★ `if not url` 这道降级闸拆掉（没凭据也往正文写 `[图片] None`）",
         '        if not url:\n            return',
         '        if False:\n            return'),
        ("★ 上传不带账号（多账号共用一个 OSS 目录 ⇒ 串号）",
         '        url = await upload_media_bytes(data, filename, account_id=acct.id, kind=kind)',
         '        url = await upload_media_bytes(data, filename, account_id="default", kind=kind)'),
        ("★ 文件行的大小写死成 0（下载截断时那句「多大」成了谎话）",
         '        _inject_media_enrich_line(msg, kind, url, filename=filename, size=len(data))',
         '        _inject_media_enrich_line(msg, kind, url, filename=filename, size=0)'),
        # ⚠️ 这条第一版写成 `"images": "图片"` —— 而 `KIND_IMAGES` 的值**就是**
        #    `"images"` ⇒ 那个变异**语义等价**（第一轮 SURVIVED 是应该的）。换成一个
        #    真会缺目录的写法：图片那一项被视频顶掉。
        ("★ 标记表缺一个目录、且被别的目录顶掉（上传成功那一刻 KeyError）",
         '    KIND_IMAGES: "图片",',
         '    KIND_VIDEOS: "图片",'),
    ]),
    "media-enrich-tag": (PARSER, "plugin/tests/test_media_enrich.py", [
        ("★ 抑制拆掉（URL 那行已在正文里，前缀标记又打一遍 ⇒ 两个 `[图片]`）",
         '        if msg.media.get("oss_url"):\n            return ""\n',
         '        if False:\n            return ""\n'),
        ("★ 读错键（去读小程序封面那个 `mp_asset_url` ⇒ 图片/视频/语音全漏）",
         '        if msg.media.get("oss_url"):',
         '        if msg.media.get("mp_asset_url"):'),
        ("★ 判据写反（有 URL 时不打标记、没 URL 时反倒不打 ⇒ 两种都错）",
         '        if msg.media.get("oss_url"):',
         '        if not msg.media.get("oss_url"):'),
        ("★ 抑制只管 49（图片/视频/语音的标记又打上去了）",
         '        if msg.media.get("oss_url"):',
         '        if msg.msg_type == MT_APP and msg.media.get("oss_url"):'),
    ]),
    "media-enrich-adapter": (ADAPTER, "plugin/tests/platform/test_media_enrich_wiring.py", [
        ("★ 图片分支不富化（整类消息回到「正文里没有 URL」）",
         '                    await self._publish_media(acct, msg, kind=KIND_IMAGES,\n'
         '                                              filename=f"{msg.msg_id or \'image\'}.jpg",\n'
         '                                              data=data)\n',
         '                    pass\n'),
        ("★ 图片传成 KIND_VIDEOS（OSS 目录与正文标记双双错位，上传本身却成功）",
         '                    await self._publish_media(acct, msg, kind=KIND_IMAGES,',
         '                    await self._publish_media(acct, msg, kind=KIND_VIDEOS,'),
        ("★ 语音传错目录",
         '                    await self._publish_media(acct, msg, kind=KIND_VOICES,',
         '                    await self._publish_media(acct, msg, kind=KIND_IMAGES,'),
        ("★ 视频传错目录",
         '                    await self._publish_media(acct, msg, kind=KIND_VIDEOS,',
         '                    await self._publish_media(acct, msg, kind=KIND_IMAGES,'),
        ("★ 文件传错目录",
         '                    await self._publish_media(acct, msg, kind=KIND_FILES,',
         '                    await self._publish_media(acct, msg, kind=KIND_IMAGES,'),
        ("★ 文件用另一个名字上传（模型看到的文件名与附件里的分家）",
         '                                              filename=fname, data=data)',
         '                                              filename="file.bin", data=data)'),
        ("★ 取媒体的位置退回 ⑤（④z/④a 都排在它之前 ⇒ judge 与 DB 拿不到 URL）",
         '        media_urls, media_types = media\n',
         '        media_urls, media_types = await self._fetch_media(acct, msg)\n'),
        ("★ 取到的媒体不交给 `_build_event`（附件悄悄丢，正文里的 URL 还在）",
         '        event = await self._build_event(acct, msg, pipeline, media=media)',
         '        event = await self._build_event(acct, msg, pipeline, media=([], []))'),
        ("★ 解包写死空表（同上；这条把「到底有没有解包」也钉住）",
         '        media_urls, media_types = media\n',
         '        media_urls, media_types = [], []\n'),
    ]),
    # ── W7-3（2026-10-02）：接龙解析 —— 识别对了，但喂给模型的是原样那坨 ──────
    # 这一族的失效形态是"识别没错、日志干净、模型还是得自己从那坨里数人"，所以判据
    # 全下在**解析产物与渲染字节**上（title / index / 条目正文），不测"调用了没有"。
    "relay-parse": (RELAY, "plugin/tests/test_relay.py", [
        ("★ 字面 `\\n` 不换回来（v1 的真 bug：title 塌成整坨、条目正文粘着两个字面字符）",
         '    text = (raw or "").replace("\\\\n", "\\n").replace("\\r\\n", "\\n")',
         '    text = (raw or "").replace("\\r\\n", "\\n")'),
        ("★ `_js_trim` 退回 Python 的 `.strip()`（BOM 不认 ⇒ 标题前面挂一个看不见的字符）",
         '    return _JS_TRIM_RE.sub("", s)',
         '    return s.strip()'),
        ("★ title 首行兜底拆掉（所有接龙 title 为空 ⇒ 节流 key 撞车、互相节流）",
         '    title = title or _js_trim(text.split("\\n")[0] if text else "")',
         '    title = title'),
        ("★ 单行无换行那条路拆掉（vendor 真格式一条都切不出来，items 为空）",
         '    if len(lines) == 1:',
         '    if False:'),
        ("★ 单行接龙退回「从首个 N. 起」而不是「从 `1. ` 起」（title 里的 `3. 周年庆` 变成条目）",
         '        body = re.sub(f"^[{_JS_WS}]+", "", text[one.start():])',
         '        body = strip_title_prefix(text)'),
        ("★ 条目内换行用 `\\n` 接而不是空格（`. ` 不跨行 ⇒ 那条条目后半截被吞掉）",
         '            merged[-1] += " " + line',
         '            merged[-1] += "\\n" + line'),
        ("★ 前导描述也当成条目行（接龙规则那行被折进上一条）",
         '        if _ITEM_LINE_RE.match(line):',
         '        if line:'),
        ("★ `<recorditem>` 那条路拆掉（真实 chat-history 掉进裸文本解析）",
         '] if blocks\n             else parse_plain_list(text))',
         '] if False and blocks\n             else parse_plain_list(text))'),
        ("★ `<recorditem>` 的序号从 0 起（少 1，下游按序号认人对不上）",
         '    return RelayItem(index=idx + 1, wxid=field_of("username"),',
         '    return RelayItem(index=idx, wxid=field_of("username"),'),
        ("★ 缺字段给 None（渲染时群里印出一个 `None`）",
         '        return _js_trim(m.group(1)) if m else ""',
         '        return _js_trim(m.group(1)) if m else None'),
        ("★ 渲染丢掉尾换行（一条都没接时的原样形态变了）",
         '    return f"[接龙] {parsed.title}\\n{joined}"',
         '    return f"[接龙] {parsed.title}{joined}"'),
        ("★ 渲染丢掉序号（模型自己从头数，而序数是接龙的全部意义）",
         '    joined = "\\n".join(f"{it.index}. {it.text}" for it in parsed.items)',
         '    joined = "\\n".join(f"{it.text}" for it in parsed.items)'),
        ("★ 非接龙也改写（每条普通消息都被套上 `[接龙]`）",
         '    if not is_relay_message(msg):\n        return False',
         '    if False:\n        return False'),
        ("★ 兜底 except 收窄（解析一炸整条消息从入站链路消失，TS 那边只 warn）",
         '    except Exception as exc:',
         '    except TypeError as exc:'),
    ]),
    "relay-tag": (PARSER, "plugin/tests/test_relay.py", [
        ("★ 抑制拆掉（改写后的正文又被加一个 `[接龙]` 前缀 ⇒ 正文里两个）",
         '        if msg.media.get("relay_rendered"):\n            return ""\n',
         '        if False:\n            return ""\n'),
        ("★ 读错键（少一个 `ed`）—— 跨模块的串，两边的单测各自都绿",
         '        if msg.media.get("relay_rendered"):\n            return ""\n',
         '        if msg.media.get("relay_render"):\n            return ""\n'),
        ("★ 判据写反（改写过的照样打、没改写的反倒不打）",
         '        if msg.media.get("relay_rendered"):\n            return ""\n',
         '        if not msg.media.get("relay_rendered"):\n            return ""\n'),
        ("★ 抑制只管 49（53 那条老路拿到 `[接龙] [接龙]`，因为 `_MEDIA_TAGS[53]` 就是它）",
         '        if msg.media.get("relay_rendered"):\n            return ""\n',
         '        if msg.msg_type == MT_APP and msg.media.get("relay_rendered"):\n            return ""\n'),
        ("★ 返回 None 而不是空串（正文看不出区别，而契约从 str 变成 str|None）",
         '        if msg.media.get("relay_rendered"):\n            return ""\n',
         '        if msg.media.get("relay_rendered"):\n            return None\n'),
    ]),
    "relay-adapter": (ADAPTER, "plugin/tests/platform/test_relay_wiring.py", [
        ("★ 接线成摆设（解析全对、正文一个字都不变 —— 等于这一项没做）",
         '        apply_relay_render(msg)\n',
         '        pass  # 接线成摆设\n'),
        ("★ 传正文而不是消息对象（两层宽兜底把它吞掉：返回 False、日志干净）",
         '        apply_relay_render(msg)\n',
         '        apply_relay_render(msg.content)\n'),
        ("★ 改写跑两遍（第二次解析的是已改写的正文 ⇒ `[接龙]` 叠两层）",
         '        apply_relay_render(msg)\n',
         '        apply_relay_render(msg)\n        apply_relay_render(msg)\n'),
        ("★ 从别的模块导入一个同名函数（import 看着干净、行为是另一套）",
         'from ..inbound.relay import apply_relay_render',
         'from ..inbound.hongbao import apply_relay_render'),
    ]),
    # ── W7-4 群黑话（2026-10-02）──────────────────────────────────────────
    # 三套按"失效形态"分工：``jargon-core`` 打分/过滤（现象 = 候选池悄悄变小/变大）、
    # ``jargon-mine`` 编排与 LLM 三步（现象 = 挖到了但没存/含义是空）、
    # ``jargon-store`` **store 契约**（现象 = 已确认的词每轮重新计费 + 词频被灌水，
    # 全链路无异常、无日志 —— 这一套是本项抓到真 bug 的那一处）。
    "jargon-core": (ROOT / "plugin" / "inbound" / "jargon.py", "plugin/tests/test_jargon.py", [
        ("★ JS 的 Math.round 换成 Python 的 round（银行家舍入 ⇒ 分数在 .5 上分叉）",
         '    return math.floor(x + 0.5)', '    return round(x)'),
        ("候选词频闸 5 → 4（没在用的词也进池 ⇒ 白花 LLM 钱）",
         'MIN_FREQUENCY = 5', 'MIN_FREQUENCY = 4'),
        ("★ 首见时间每次都覆盖（burst 恒等于 freq/1 天 ⇒ 「新爆发的词」这一维失效）",
         '        if token not in fs:\n            fs[token] = now',
         '        if token is not None:\n            fs[token] = now'),
        ("上下文样例上限 10 → 5（存的那份变短，喂 LLM 的例子跟着变少）",
         'MAX_CONTEXT_EXAMPLES = 10', 'MAX_CONTEXT_EXAMPLES = 5'),
        ("★ 输出截断 5 → 10（送 LLM 的上下文条数翻倍 ⇒ 白花 token）",
         '            context_examples=list(_stats.term_contexts.get(group_id, {}).get(term) or [])[:5],',
         '            context_examples=list(_stats.term_contexts.get(group_id, {}).get(term) or [])[:10],'),
        ("★ 硬过滤不再拉黑中文（中文候选涌进 LLM ⇒ 与 TS 分叉、且账单立刻变大）",
         'HARD_BLOCK_RE = re.compile(rf"[@{_CJK}]|[\\[\\]]|https?://|[\\s　]")',
         'HARD_BLOCK_RE = re.compile(rf"[@]|[\\[\\]]|https?://|[\\s　]")'),
        ("候选长度下界 2 → 1（单字候选进池，TS 明确挡掉）",
         '    if len(term) < 2 or len(term) > 8:', '    if len(term) < 1 or len(term) > 8:'),
        ("★ 裁剪只删词频表、不动三张副表（user_freq/first_seen/contexts 与词频表失同步）",
         '                    if t in low:\n'
         '                        del gf[t]\n'
         '                        self.user_term_freq.get(g, {}).pop(t, None)\n',
         '                    if t in low:\n'
         '                        del gf[t]\n'),
        ("集中度的分母下界 1 → 2（「一个人反复说」的分被压到与「多人都在说」持平）",
         '        concentration = 1.0 / max(unique_users, 1)',
         '        concentration = 1.0 / max(unique_users, 2)'),
        ("★ burst 年龄上限 14 天拆掉（老词的分一路衰减到 0 ⇒ 黑话永远挖不出来）",
         '    age_days = min(max((now - first_seen) / 86400, 1), BURST_AGE_CAP_DAYS)',
         '    age_days = max((now - first_seen) / 86400, 1)'),
        ("首见时间为 0 的守卫拆掉（落进分母 ⇒ 返一个近乎 0 的分而不是 0）",
         '    if first_seen == 0:\n        return 0.0',
         '    if False:\n        return 0.0'),
        ("★ 排除集不生效（已确认的词每轮重新送 LLM —— 与 store 契约那条是同一个病）",
         '        if term in exclude:\n            continue',
         '        if False:\n            continue'),
    ]),
    "jargon-mine": (ROOT / "plugin" / "inbound" / "jargon_mine.py", "plugin/tests/test_jargon.py", [
        ("★ enabled 默认翻成 True（fail-closed 破功：没开的开关自己开了）",
         '            enabled=_as_bool(d.get("enabled"), False),',
         '            enabled=_as_bool(d.get("enabled"), True),'),
        ("★ 整数键不收 bool（Python 的 bool 是 int ⇒ `timeoutMs: true` 变成超时 1 毫秒）",
         '    if isinstance(v, bool):\n        return default',
         '    if False:\n        return default'),
        ("一次送 LLM 的候选上限 15 → 20（成本上界破功）",
         'MAX_VALIDATE_TERMS = 15', 'MAX_VALIDATE_TERMS = 20'),
        ("含义推断的词数上限 5 → 8（每个词 3 次调用 ⇒ 一轮的账单翻近一倍）",
         'MAX_INFER_TERMS = 5', 'MAX_INFER_TERMS = 8'),
        ("★ 第二步失败降级成空串（「挖到了、含义丢了」—— 库里躺着 meaning=''）",
         '    if r2 is None:\n        return ctx_meaning',
         '    if r2 is None:\n        return ""'),
        ("★ is_similar 用真值判（JSON 里的 0 被当成「含义不同」⇒ 普通词也存成黑话）",
         '    if isinstance(o3, dict) and o3.get("is_similar") is False:',
         '    if isinstance(o3, dict) and not o3.get("is_similar"):'),
        ("回包不是 dict 时不判类型（数组/数字 ⇒ AttributeError，整轮挖掘中止）",
         '    if isinstance(o3, dict) and o3.get("is_similar") is False:',
         '    if o3.get("is_similar") is False:'),
        ("★ no_info 那条空串出口拆掉（「推不出含义」被写成上下文含义 ⇒ 模型照着编）",
         '    if no_info:\n        return ""\n',
         '    if no_info:\n        pass\n'),
        ("★ store 读排除集的那道闸反了（store 给了也读不到 ⇒ 已确认的词反复计费）",
         '    if store is not None:\n        try:\n            for t in store.list_terms(group_id, STORE_LIST_LIMIT):',
         '    if store is None:\n        try:\n            for t in store.list_terms(group_id, STORE_LIST_LIMIT):'),
        ("★ 落库那半拆掉（挖完不存 ⇒ 恒 confirmed=0，等于 TS 那个「只算不存」的形态）",
         '            if store is not None:\n'
         '                store.save_term(group_id=group_id, content=term,',
         '            if False:\n'
         '                store.save_term(group_id=group_id, content=term,'),
    ]),
    "jargon-store": (ROOT / "plugin" / "inbound" / "jargon_mine.py", "plugin/tests/test_jargon.py", [
        # ★★ 这一条就是本项抓到的**真 bug** 的变异体：DB 列叫 ``term``、store 契约叫
        #    ``content``，适配层不映射 ⇒ ``exclude`` 恒空集 ⇒ 已确认的词每轮重新送 LLM、
        #    重新 ``upsert``（那条 SQL 是 ``frequency = frequency + 1``，词频被反复灌水）。
        #    全链路无异常、无日志；TS 侧**永远暴露不出来**（它的 ``cfg.store`` 从未被注入）。
        ("★★ 去掉 term → content 的列名映射（排除集恒空：每轮重挖同一批词 + 词频被灌水）",
         '            {"content": r.get("term") or "", "meaning": r.get("meaning") or ""}',
         '            {"content": r.get("content") or "", "meaning": r.get("meaning") or ""}'),
        ("★ 读库不透传注入的 conn（真库探针那条路上读到另一个模块实例的连接）",
         '        rows = repo_messages.list_jargon(self.account_id, group_id, limit, conn=self._conn)',
         '        rows = repo_messages.list_jargon(self.account_id, group_id, limit, conn=None)'),
        ("★ 位置参数对调（``content`` 与 ``raw_content`` 互换 ⇒ 库里词条与上下文互相顶掉）",
         '            self.account_id, group_id, content, raw_content, meaning,',
         '            self.account_id, group_id, raw_content, content, meaning,'),
        # ⚠️ 曾试过把 ``1 if is_jargon else 0`` 改成 ``int(is_jargon)`` —— **语义等价**，
        #    第一轮 SURVIVED 是应该的：实测 ``pymysql.converters.escape_item`` 对
        #    ``True``/``1`` 都吐 ``'1'``、``False``/``0`` 都吐 ``'0'``（`bool` 是 `int` 的
        #    子类，走的是同一条转义路）⇒ 这一处的"显式转 int"在**行为上**分不出来。
        #    按规矩**换掉**它，而不是把断言改成 `is 1` 去凑一条假判据。
    ]),
    "jargon-adapter": (ADAPTER, "plugin/tests/platform/test_jargon_wiring.py", [
        ("★ 旁路拆成摆设（接线在、一次都不跑 ⇒ 表现为「开关开着、词库永远空」）",
         '            self._jargon_note_group(acct, msg)\n',
         '            pass  # 旁路拆成摆设\n'),
        ("★ 黑话旁路挪到好感度之后（与 TS 的批内次序 570→589→620 不符）",
         '            self._jargon_note_group(acct, msg)\n'
         '            # ④b 好感度旁路（W2，对齐 TS ``handler.ts:623-636``）。与心流**并列**：\n'
         '            #     都与"这条消息有没有触发 AI"无关 —— 群里的每句人话都算数。\n'
         '            #     fire-and-forget：见 :meth:`_affection_note_group` 的 docstring。\n'
         '            self._affection_note_group(acct, msg)\n',
         '            # ④b 好感度旁路（W2，对齐 TS ``handler.ts:623-636``）。与心流**并列**：\n'
         '            #     都与"这条消息有没有触发 AI"无关 —— 群里的每句人话都算数。\n'
         '            #     fire-and-forget：见 :meth:`_affection_note_group` 的 docstring。\n'
         '            self._affection_note_group(acct, msg)\n'
         '            self._jargon_note_group(acct, msg)\n'),
        ("★ store 写死 `default`（多账号共用一个命名空间：A 号挖的词让 B 号再也挖不到）",
         '        store = jargon_mine.RepoJargonStore(account_id)',
         '        store = jargon_mine.RepoJargonStore("default")'),
        ("★ 挖掘时不带 store（挖一轮、一条都不存，返回 confirmed=0）",
         '        result = await jargon_mine.mine_jargon_for_group(group_id, cfg, None, store)',
         '        result = await jargon_mine.mine_jargon_for_group(group_id, cfg, None, None)'),
        ("★ 群 id 只取 peer_id（统计与挖掘落在两个桶里：词库永远差那一群）",
         '        group_id = msg.chatroom_id or msg.peer_id',
         '        group_id = msg.peer_id'),
        ("★ 配置不从账号文件取（开关/节流全走默认 ⇒ 账号文件里写的一律不算数）",
         '        cfg = jargon_mine.resolve_jargon_cfg(acct.jargon)',
         '        cfg = jargon_mine.resolve_jargon_cfg({})'),
        ("★ 采集面不再排除系统通知（通知也进词频表，与 TS 第一个循环不符）",
         '        if msg.msg_type != MT_SYSTEM and content.strip():',
         '        if content.strip():'),
        ("★ 任务句柄不存（事件循环只持弱引用 ⇒ 任务可能在跑完前被 GC，「有时挖有时不挖」）",
         '        self._jargon_tasks.add(task)                     # 强引用，见 __init__',
         '        self._jargon_tasks.discard(task)                 # 强引用，见 __init__'),
        ("★ 不挂 done 回调（fire-and-forget 的异常只留一句「never retrieved」，没有群没有原因）",
         '        task.add_done_callback(self._jargon_done)\n',
         '        pass\n'),
        # ⚠️ 锚点必须带上下一行的 ``self._jargon_mine_run(``：光写 ``task = asyncio.create_task(``
        #    在本文件里命中 3 次（好感度、WS 兜底、黑话各一处），脚本自曝"锚点命中 3 次"。
        ("★ 不用 create_task（换 ensure_future：形状判据与语义都变，取消/命名都没了）",
         '            task = asyncio.create_task(\n                self._jargon_mine_run(acct.id, group_id, cfg),',
         '            task = asyncio.ensure_future(\n                self._jargon_mine_run(acct.id, group_id, cfg),'),
        ("★ 旁路写成 async（调用方不 await ⇒ 整条旁路一次都不执行，只留一行 RuntimeWarning）",
         '    def _jargon_note_group(self, acct: accounts_mod.Account, msg: WppMessage) -> None:',
         '    async def _jargon_note_group(self, acct: accounts_mod.Account, msg: WppMessage) -> None:'),
        ("★ 挖掘那半写成同步（跑不起来 —— 且这是「必须有 async」的形状判据）",
         '    async def _jargon_mine_run(self, account_id: str, group_id: str,',
         '    def _jargon_mine_run(self, account_id: str, group_id: str,'),
        ("★ 日志报候选数而不是入库数（运维唯一的观测点与表里的行数对不上）",
         '        if result.confirmed:', '        if result.extracted:'),
    ]),
    # ----------------------------------------------------------- W7-5 运行期门禁
    # 判据落在**行为**上：门要么真拦住了厂商请求、要么真放进去了 —— 而"工具回了什么"
    # 恰好是这条链上最容易骗人的东西（回一句"未启用"和"真的没执行"是两件事）。
    "tool-gate": (ROOT / "plugin" / "toolkit" / "policy.py", "plugin/tests/test_tool_gate.py", [
        ("★★ fail-open：读不到账号配置 ⇒ 放行（闸门在、最该拦的那一类全过）",
         '                return gate.closed("读不到账号配置")',
         '                return None'),
        ("★★ ``raw_account_cfg`` 交出**活的那份** dict（调用方就地改键 = 直接改运行期配置）",
         '    return dict(raw) if isinstance(raw, Mapping) else None',
         '    return raw if isinstance(raw, Mapping) else None'),
        ("★ 账号路由写死默认账号（多账号共用一份配置：B 号的开关跟着 A 号走）",
         '        acct = adapter.account(aid)',
         '        acct = adapter.account(DEFAULT_TOOL_ACCOUNT)'),
        ("★ 不受管的工具也加一层包装（299 个工具全走门禁 ⇒ 无关工具被误拦）",
         '        gate = gate_for(tool_name)\n        if gate is None:\n            return None',
         '        gate = gate_for(tool_name)\n        if gate is None or True:\n            return None'),
        ("★ ``closed()`` 丢掉 reason（「我读不到」与「你没开」说成一句 —— 门禁在撒谎）",
         '        if not reason:\n            return self.message',
         '        if not reason or True:\n            return self.message'),
        ("★ 关闭回话降级成空串（模型收到空 tool 结果，「为什么不生效」全靠猜）",
         '        return self.message\n', '        return ""\n'),
        ("★ 双认领不报错（静默取其中一条 ⇒ 门禁取决于字典顺序）",
         '            if name in index:\n                raise RuntimeError(',
         '            if False:\n                raise RuntimeError('),
        ("★ 门禁表可写（运行期一行赋值就能把整张安全表换掉）",
         'GATED_TOOLS: Mapping[str, RuntimeGate] = MappingProxyType(_build_gate_index(RUNTIME_GATES))',
         'GATED_TOOLS: Mapping[str, RuntimeGate] = _build_gate_index(RUNTIME_GATES)'),
        ("★ 小微键写成蛇形（门禁读的键与命令写的键对不上 ⇒ 开了也恒关）",
         '    key="xiaoweiEnabled",', '    key="xiaowei_enabled",'),
        ("★ 开关判定改成 in 判据（「配了 false」与「没配」混为一谈 ⇒ 清掉默认值就放行）",
         '            if not cfg.get(gate.key):', '            if gate.key not in cfg:'),
        ("★ 前缀名单漏一个工具（``wpp_xw_invite`` 不再受管 ⇒ 关着也能调）",
         '        "wpp_xw_invite",\n', ''),
        ("★ 门禁名单多一个不存在的工具（幽灵条目：AI 面上没有、门禁表却说受管）",
         '        "wpp_fc_publish_text",\n',
         '        "wpp_fc_publish_text",\n        "wpp_fc_publish_nope",\n'),
    ]),
    "tool-gate-seam": (ROOT / "plugin" / "toolkit" / "hermes_contract.py",
                       "plugin/tests/platform/test_tool_gate_wiring.py", [
        ("★★ 门禁工厂的结果被丢掉（``gate_pre = None``：注册期装了门、调用期一次都不判）",
         '                gate_pre = self._gate(str(kw.get("name") or ""))',
         '                gate_pre = None'),
        ("★ 不受管的工具也被包一层（``gate_pre is None`` 那道闸拆掉 ⇒ 多出 247 层间接）",
         '            if gate_pre is not None:', '            if True:'),
        ("★★ 设 ``__wrapped__``（``inspect.signature`` 会穿透它 ⇒ Hermes 读到原函数的 ``**params``）",
         '    handler.__original_handler__ = original  # type: ignore[attr-defined]',
         '    handler.__wrapped__ = fn  # type: ignore[attr-defined]\n'
         '    handler.__original_handler__ = original  # type: ignore[attr-defined]'),
        ("★ 签名放宽成 ``**kw``（dispatch 上下文键会被放行、原样发给厂商）",
         '        async def handler(args: Any = None) -> Any:\n            reject = pre(args)',
         '        async def handler(args: Any = None, **kw: Any) -> Any:\n            reject = pre(args)'),
        ("★ 判据装了不判（``if reject`` 短路掉 ⇒ 门说「未启用」、厂商请求照发）",
         '            if reject:\n                return reject\n            return await fn(args)',
         '            if False:\n                return reject\n            return await fn(args)'),
        ("★ ``__original_handler__`` 指到包装层而不是最里层（W0 的形状判据立刻失效）",
         '    handler.__original_handler__ = original  # type: ignore[attr-defined]',
         '    handler.__original_handler__ = fn  # type: ignore[attr-defined]'),
    ]),
    "tool-gate-registration": (ROOT / "plugin" / "toolkit" / "registration.py",
                               "plugin/tests/platform/test_tool_gate_wiring.py", [
        ("★★ 注册期不装门禁（``wrap_ctx(ctx)``：命令能改键、工具面没有消费者 = 谎开关）",
         "    ctx = _contract.wrap_ctx(ctx, _policy.make_tool_gate(lambda: _adapter._get_adapter()))",
         "    ctx = _contract.wrap_ctx(ctx)"),
        ("★ getter 传函数对象而不是 lambda（注册期就取死适配器 ⇒ 单测/探针的补丁落空）",
         'lambda: _adapter._get_adapter()', '_adapter._get_adapter'),
    ]),
    "tool-gate-cmd": (ROOT / "plugin" / "platform" / "commands.py",
                      "plugin/tests/test_tool_gate.py", [
        ("★★ 白名单写回传增量而不是整份清单（``/user add x`` 把现有白名单清空，回话却是 ✅）",
         '            new_items = [*items, *changed]', '            new_items = [*changed]'),
        ("★★ ``del`` 写回传本次点名的那些（``/user del b`` 把名单替换成 [b]）",
         '            new_items = [x for x in items if x not in gone]',
         '            new_items = list(targets)'),
        ("★★ 开关写回拍扁同级键（``/affection off`` 把 ``maxUserAffection`` 一起抹掉）",
         '        err = write_back(account_id, feature, {**base, "enabled": want}, adapter=adapter)',
         '        err = write_back(account_id, feature, {"enabled": want}, adapter=adapter)'),
        ("★★ ``/xiaowei`` 写成 dict（非空 dict 恒真 ⇒ 门永远开着、``off`` 完全无效）",
         '    err = write_back(account_id, "xiaoweiEnabled", want, adapter=adapter)',
         '    err = write_back(account_id, "xiaoweiEnabled", {"enabled": want}, adapter=adapter)'),
        ("★ 黑名单落错键（``triggers.py`` 读的是 ``blacklistGroups`` ⇒ 一票否决永不生效）",
         '"blacklist": "blacklistGroups"}', '"blacklist": "blacklist"}'),
        # ⚠️ 锚点要带上一行 ``"✅ 开启"``：光写 ``if current is None:`` 会同时命中
        #    ``_state_word``（两个函数在这三行上逐字相同），脚本自曝"锚点命中 2 次"。
        ("★ 状态页退化成二值（「我读不到」印成「❌ 关闭」⇒ 老板去做一个错误的动作）",
         '    if current is None:\n        return UNREADABLE_STATE_MSG\n'
         '    return "✅ 开启" if current else "❌ 关闭"',
         '    if False:\n        return UNREADABLE_STATE_MSG\n'
         '    return "✅ 开启" if current else "❌ 关闭"'),
        ("★ 状态改回经 ``_safe_cfg`` 读（解析失败与「真没配」同值 ⇒ 读不到被报成「关闭」）",
         '    raw = _account_file_cfg(account_id)\n    if raw is None:\n        return None\n'
         '    feat = raw.get(feature)',
         '    feat = _safe_cfg(account_id).get(feature)'),
        ("★★ 底稿读不出来时按空名单继续（``/user add x`` 拿空底稿整体替换 = 清空白名单）",
         '    if raw is None:\n        return None\n    val = raw.get(key)',
         '    if False:\n        return None\n    val = raw.get(key)'),
        ("★ 名单命令不拒不可写状态（同上，走另一条调用点）",
         '        if items is None:\n            return UNREADABLE_CFG_MSG\n        if action == "add":',
         '        if False:\n            return UNREADABLE_CFG_MSG\n        if action == "add":'),
    ]),
    "tool-gate-msg": (ROOT / "plugin" / "toolkit" / "msg.py", "plugin/tests/test_tool_gate.py", [
        # ⚠️ 四处守卫的文本**逐字相同** ⇒ 锚点必须带上那个工具独有的厂商调用行才唯一
        #    （只写守卫那几行时脚本自曝"锚点命中 4 次"）。同理，判据也必须**三个工具
        #    各判一遍** —— 只测一处的话，另外两处拆成摆设测试照样全绿。
        # ⚠️ 第四处 ``publish_circle_ai`` 不在变异面里：派发它会 fork ``mmx`` 子进程。
        #    它由 ``test_tool_gate_wiring`` 的**同形判据**兜住（4 处 ``raw_account_cfg``
        #    + 4 处 ``if acct_cfg is None``，拆一处就掉计数）。
        ("★★ 文字那条：白名单判定拆成摆设（非白名单的调用方也能发，且一切「看着正常」）",
         '        if caller and allow and caller not in allow:\n'
         '            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}\n'
         '        resp = await client.publish_circle_text(title)',
         '        if False:\n'
         '            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}\n'
         '        resp = await client.publish_circle_text(title)'),
        ("★★ 图片那条：白名单判定拆成摆设（同上，另一处同形代码）",
         '        if caller and allow and caller not in allow:\n'
         '            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}\n'
         '        resp = await client.publish_circle_images(title, image_base64_list)',
         '        if False:\n'
         '            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}\n'
         '        resp = await client.publish_circle_images(title, image_base64_list)'),
        ("★★ 视频那条：白名单判定拆成摆设（同上，另一处同形代码）",
         '        if caller and allow and caller not in allow:\n'
         '            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}\n'
         '        resp = await client.publish_circle_video(title, video_base64, thumb_base64)',
         '        if False:\n'
         '            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}\n'
         '        resp = await client.publish_circle_video(title, video_base64, thumb_base64)'),
        ("★★ 文字那条：读不到配置就放行（fail-open：把「读不出来」当成「没有白名单」）",
         '        acct_cfg = policy.raw_account_cfg(adapter, aid)\n'
         '        if acct_cfg is None:\n'
         '            return {"error": "读不到账号配置，拒绝发布朋友圈"}\n'
         '        allow = acct_cfg.get("friendCirclePublishAllowFrom") or acct_cfg.get("adminUsers") or []\n'
         '        caller = params.get("callerWxid") or ""\n'
         '        if caller and allow and caller not in allow:\n'
         '            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}\n'
         '        resp = await client.publish_circle_text(title)',
         '        acct_cfg = policy.raw_account_cfg(adapter, aid)\n'
         '        if False:\n'
         '            return {"error": "读不到账号配置，拒绝发布朋友圈"}\n'
         '        allow = acct_cfg.get("friendCirclePublishAllowFrom") or acct_cfg.get("adminUsers") or []\n'
         '        caller = params.get("callerWxid") or ""\n'
         '        if caller and allow and caller not in allow:\n'
         '            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}\n'
         '        resp = await client.publish_circle_text(title)'),
        ("★★ 视频那条：读不到配置就放行（同上，另一处同形代码）",
         '        acct_cfg = policy.raw_account_cfg(adapter, aid)\n'
         '        if acct_cfg is None:\n'
         '            return {"error": "读不到账号配置，拒绝发布朋友圈"}\n'
         '        allow = acct_cfg.get("friendCirclePublishAllowFrom") or acct_cfg.get("adminUsers") or []\n'
         '        caller = params.get("callerWxid") or ""\n'
         '        if caller and allow and caller not in allow:\n'
         '            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}\n'
         '        resp = await client.publish_circle_video(title, video_base64, thumb_base64)',
         '        acct_cfg = policy.raw_account_cfg(adapter, aid)\n'
         '        if False:\n'
         '            return {"error": "读不到账号配置，拒绝发布朋友圈"}\n'
         '        allow = acct_cfg.get("friendCirclePublishAllowFrom") or acct_cfg.get("adminUsers") or []\n'
         '        caller = params.get("callerWxid") or ""\n'
         '        if caller and allow and caller not in allow:\n'
         '            return {"error": f"朋友圈发布无权限 ({caller} 不在白名单)"}\n'
         '        resp = await client.publish_circle_video(title, video_base64, thumb_base64)'),
    ]),
    # ── W7-6 入站到达层聚合（2026-10-02）────────────────────────────────────
    # 两套按"失效形态"分工：``debounce-core`` 是聚合器本体（窗口 / 直通 / 保序 / 停机 /
    # 一条都不许丢），``debounce-adapter`` 是**接线**（`_on_raw` 的直通兜底、停机顺序、
    # 窗口取账号文件）。这一项的坏法几乎全是**安静的**：聚合器一旦成了消息的终点，
    # 现场只有"有些人说话机器人不理"，日志一行不出 ⇒ 判据必须下在"哪几条、按什么顺序、
    # 在第几拍到的"上。
    "debounce-core": (ROOT / "plugin" / "inbound" / "debounce.py",
                      "plugin/tests/test_debounce.py", [
        # ⚠️ 锚点要**整行**：本文件里 `return False` / `self._timers.clear()` 这类短句
        #    各出现多次，半行锚点会命中 >1 次（脚本自曝 BROKEN）。
        # ⚠️ 「窗口不重置」那一条**没有**写成 `if key not in self._timers:` 那种形状 ——
        #    实测它**语义等价**（第一轮 SURVIVED）：``enqueue`` 开头的 ``_take`` 已经把
        #    旧定时器 pop + cancel 了，所以"只在没有定时器时才挂"与原文走的是**同一条路**。
        #    「窗口从第一条算起」的**唯一**有效表达就是"不取消旧定时器" ⇒ 也就是下面
        #    第 5 条（``_take`` 不 cancel，旧窗口照旧到点 ⇒ [m1,m2] 在 0.20 就被切出去）。
        ("★ 窗口只在第一条上挂（后到的那些把自己的窗口挂没了 ⇒ 永远等不到到点）",
         '        self._timers[key] = loop.call_later(self.interval_ms / 1000.0, self._fire, key)\n',
         '        if len(pending) == 1:  # 变异：后续入队不再重置\n'
         '            self._timers[key] = loop.call_later(self.interval_ms / 1000.0,\n'
         '                                                self._fire, key)\n'),
        ("毫秒→秒换算漏掉 `/1000`（现场 = 消息在 60 秒后才被处理）",
         '        self._timers[key] = loop.call_later(self.interval_ms / 1000.0, self._fire, key)\n',
         '        self._timers[key] = loop.call_later(self.interval_ms, self._fire, key)\n'),
        ("★★ 直通不带同 key 已缓冲的那批（那几条**从缓冲里取走了又没派发** ⇒ 真丢消息）",
         '            self._schedule(key, pending + [msg])\n',
         '            self._schedule(key, [msg])\n'),
        ("★★ 保序破坏：不接上一条链（同 key 两批并发跑 ⇒ 派发顺序 ≠ 到达顺序）",
         '        prev = self._chains.get(key)\n',
         '        prev = None\n'),
        ("★ `_take` 不取消定时器：旧窗口到点会把**新**缓冲整批发出去（提前派发/重复派发）",
         '        handle = self._timers.pop(key, None)\n'
         '        if handle is not None:\n'
         '            handle.cancel()\n',
         '        handle = self._timers.pop(key, None)\n'
         '        if handle is not None:\n'
         '            pass  # 变异：不取消定时器\n'),
        ("★★ 前一条链的取消会吞掉后一批（裸 await 而不是 gather(return_exceptions=True)）",
         '            await asyncio.gather(prev, return_exceptions=True)\n',
         '            await prev\n'),
        ("★ 控制面钩子拆成摆设（`/heartflow status` 这类命令被窗口拖住）",
         '        return bool(self.is_control_command is not None and self.is_control_command(msg))\n',
         '        return False\n'),
        ("★ 直通类型判反（文本全直通、系统/语音/撤回反而进缓冲）",
         '        if msg.msg_type in PASSTHROUGH_TYPES:\n',
         '        if msg.msg_type not in PASSTHROUGH_TYPES:\n'),
        ("★ 聚合键少一个字段（群里两个人连着说话被合成一批，语境混掉）",
         '    return f"{msg.account_id}:{msg.peer_kind}:{msg.peer_id}:{msg.from_wxid}"\n',
         '    return f"{msg.account_id}:{msg.peer_kind}:{msg.peer_id}"\n'),
        ("★ 键里把 py2 自己的 peer 口径换成 TS 的 `direct`（与 py2 的 dm 分叉）",
         '    return f"{msg.account_id}:{msg.peer_kind}:{msg.peer_id}:{msg.from_wxid}"\n',
         '    return f"{msg.account_id}:direct:{msg.peer_id}:{msg.from_wxid}"\n'),
        ("★ `flush_all` 只排队不等跑完（停机时那批拿着已被拆掉的依赖去跑）",
         '        pending = list(self._chains.values())\n'
         '        if pending:\n'
         '            await asyncio.gather(*pending, return_exceptions=True)\n',
         '        pending = list(self._chains.values())\n'
         '        if pending:\n'
         '            pass  # 变异：只排队，不等跑完\n'),
        ("★ `stop()` 默认不 flush（停机丢一批 —— 缓冲区里那 500ms 内刚到的那批）",
         '        total = await self.flush_all() if flush else 0\n',
         '        total = 0\n'),
        ("★ 停机之后照样收（返 True ⇒ 塞进一个再也不会被 flush 的缓冲 = 消息没人派发）",
         '        if self._stopped:\n',
         '        if False:\n'),
        ("★ 没有事件循环也照样收（同步入口误调 ⇒ 那一条永远躺在缓冲里）",
         '        except RuntimeError:\n'
         '            log.warning("[WPP:%s DEBOUNCE] 无运行中的事件循环 ⇒ 交回调用方直通（%s）",\n'
         '                        self.account_id, msg.msg_id)\n'
         '            return False\n',
         '        except RuntimeError:\n'
         '            log.warning("[WPP:%s DEBOUNCE] 无运行中的事件循环 ⇒ 交回调用方直通（%s）",\n'
         '                        self.account_id, msg.msg_id)\n'
         '            return True\n'),
        ("★ `_fire` 不走 `_take`（缓冲不清、定时器不取消 ⇒ 同一批被反复派发）",
         '        batch = self._take(key)\n        if batch:\n',
         '        batch = self._buffers.get(key, [])\n        if batch:\n'),
        ("★ 派发失败不叫 `on_error`（旁路观测点丢掉）",
         '            if self.on_error is not None:\n',
         '            if False:\n'),
        ("★★ 派发失败往上抛（把这层旁路变成能把读帧循环带崩的东西）",
         '        except Exception as exc:  # noqa: BLE001 —— 旁路不许把读帧循环带崩\n'
         '            log.warning("[WPP:%s DEBOUNCE] 一批 %d 条派发失败: %s",\n'
         '                        self.account_id, len(batch), exc)\n',
         '        except Exception:  # noqa: BLE001 变异：冒到读帧循环\n'
         '            raise\n'),
        ("脏窗口值不夹（负数进 `call_later` ⇒ ValueError，直接炸在入队上）",
         '        self.interval_ms = max(0, int(interval_ms))\n',
         '        self.interval_ms = int(interval_ms)\n'),
    ]),
    # ⚠️ 这一套的判据全在 ``plugin/tests/platform/test_debounce_wiring.py``（AST 源码形状）——
    #    ``adapter.py`` 本机 import 不了（模块级 `from gateway.config import Platform`）。
    #    每个变异体对应"接线成摆设 / 接错对象"的一种：它们**都不会让任何单测变红**
    #    （聚合器本体全对），只会让这条能力在真机上不生效。
    "debounce-adapter": (ADAPTER, "plugin/tests/platform/test_debounce_wiring.py", [
        ("★★ 直通兜底成摆设（`enqueue` 返 False 的那条没人派发 ⇒ 没有循环/已停机时消息全丢）",
         '            if db is None or not db.enqueue(msg):\n'
         '                await self._dispatch_one(acct, msg, pipeline)\n',
         '            if db is None or not db.enqueue(msg):\n'
         '                pass  # 变异：直通成摆设\n'),
        ("★★ 守卫恒不成立（永远「已入队」 ⇒ 连 `enqueue` 都不调了，全部消息静默消失）",
         '            if db is None or not db.enqueue(msg):\n',
         '            if db is None or False:\n'),
        ("★ 窗口写死成 500（账号文件里的 `debounceMs` 又变回死键）",
         '                    interval_ms=acct.debounce_ms,\n',
         '                    interval_ms=500,\n'),
        ("★★ 账号 id 晚绑定（多账号下所有批都按**最后一个**账号派发，单账号测试全绿）",
         '                    lambda batch, a=acct.id: self._flush_inbound(a, batch),\n',
         '                    lambda batch: self._flush_inbound(acct.id, batch),\n'),
        ("★ 控制面钩子不接（filehelper 管理命令被窗口拖住 500ms）",
         '                    is_control_command=is_filehelper_session,\n',
         '                    is_control_command=None,\n'),
        ("★ 不按账号登记（`_on_raw` 扑空 ⇒ 聚合器形同不存在）",
         '                self._debounce[acct.id] = InboundDebouncer(\n',
         '                self._debounce["shared"] = InboundDebouncer(\n'),
        ("★★ flush 只看最后一条（触发语义被偷偷换成「批式」—— 本项明确不做的那一截）",
         '        for msg in batch:\n',
         '        for msg in batch[-1:]:\n'),
        ("★ 无处派发时不出声（安静地丢掉一整批 = 这条链上最坏的失效形态）",
         '            log.warning("[WPP:%s] 聚合的一批 %d 条无处派发（未连接/已停机），丢弃: %s",\n',
         '            log.debug("[WPP:%s] 聚合的一批 %d 条无处派发（未连接/已停机），丢弃: %s",\n'),
        ("★ 单条不保护（一条坏消息把整批带崩 —— 那几条本来能正常回的）",
         '        try:\n'
         '            await self._handle_one(acct, msg, pipeline)\n'
         '        except Exception as exc:  # noqa: BLE001\n'
         '            log.warning("[WPP:%s] 处理消息 %s 失败: %s", acct.id, msg.msg_id, exc, exc_info=True)\n',
         '        await self._handle_one(acct, msg, pipeline)\n'),
        ("★ 停机不 flush 聚合器（缓冲区里那批随进程消失）",
         '        for aid, db in list(self._debounce.items()):\n',
         '        for aid, db in []:  # 变异：停机不 flush\n'),
        ("★★ 停机 flush 但**丢掉内容**（`flush=False`：走了流程，一条都没派发）",
         '                await db.stop()\n',
         '                await db.stop(flush=False)\n'),
        ("★ 热加载不快照窗口（`/debounce` 类配置改了不生效 —— 又成一个说谎的开关）",
         '            db.interval_ms = max(0, int(acct.debounce_ms))\n',
         '            db.interval_ms = 500\n'),
        ("★ 窗口逻辑被搬进适配器（第二份真源：绕开聚合器模块导入它的内部常量）",
         'from ..inbound.debounce import InboundDebouncer\n',
         'from ..inbound.debounce import PASSTHROUGH_TYPES, InboundDebouncer\n'),
    ]),
    # ------------------------------------------- W-Q2 被回复人身份（纯逻辑）
    # 老板 2026-10-03 报的「群里 @ 机器人时回复了，但不会 @ 被回复的人」那一项。
    # 这条链的失效形态是**统一**的：不报错、日志干净、消息照样发出去（只是少一个 @）。
    "quote-ref": (ROOT / "plugin" / "inbound" / "quote_ref.py",
                  "plugin/tests/test_quote_ref.py", [
        ("★★ 群 id 不是人 这道闸拆掉（`111@chatroom` 会被当成被回复人 ⇒ @ 一个群）",
         '    if not val or val.endswith(GROUP_SUFFIX):\n        return ""\n',
         '    if not val:\n        return ""\n'),
        ("★★ 身份链第一档（行里的 from_wxid）不再过闸（business callback 的 FromUserName 就是群 id）",
         '    fromusr = is_usable_sender(row.get("from_wxid"))\n',
         '    fromusr = str(row.get("from_wxid") or "").strip()\n'),
        ("★ 正文前缀那一档不过闸（py2 比 TS 严的那一处，见模块 docstring 第 3 条）",
         '        fromusr = is_usable_sender(extract_group_sender_wxid(row.get("content")))\n',
         '        fromusr = extract_group_sender_wxid(row.get("content"))\n'),
        ("★★ 群记录也用 peer_id 当发送者（被引用人是群自己 ⇒ @ 的永远是那个群）",
         '    if not fromusr and str(row.get("peer_kind") or "").strip().lower() != "group":\n',
         '    if not fromusr:\n'),
        ("★ `matched` 恒假（「命中但取不到发送者」这条要留痕的分支永远不触发）",
         '        matched=True,\n', '        matched=False,\n'),
        ("★★ `raw_payload` 的 JSON 字符串不再解包（MariaDB 的 JSON 列回的就是字符串 ⇒ 中间两档全空）",
         '            loaded = json.loads(raw)\n', '            loaded = raw\n'),
        ("★ 前缀不做群判定（私聊里也 @ 人）",
         '    if not _safe_str(peer_id).endswith(GROUP_SUFFIX):\n',
         '    if False:\n'),
        ("★★ 没有名称时不再回退到 wxid（该 @ 人却整条前缀消失 —— 正是老板报的那个现象）",
         '        at_name = _safe_str(fromusr)\n', '        at_name = _safe_str(name)\n'),
        ("★ 两样都没有时照样产出前缀（群里出现一条光秃秃的 `@ `）",
         '    if not at_name:\n        return ""\n', '    if False:\n        return ""\n'),
    ]),
    # ------------------------------------------- W-Q1 数据层（按锚点回查一条消息）
    # 这一档的失效形态：查不到（悄悄不 @）或条件写宽（跨账号串号 / 引用 bot 自己的话查不到）。
    "quote-repo": (ROOT / "plugin" / "data" / "repo_messages.py",
                   "plugin/tests/test_db.py", [
        ("★★ OR 的括号拆掉（`AND` 优先级更高 ⇒ 任何账号下 new_msg_id 命中的行都会回给这个账号）",
         '"FROM wpp_messages WHERE account_id = %s AND ("\n',
         '"FROM wpp_messages WHERE account_id = %s AND "\n'),
        ("★★ 加回 direction 过滤（被引用的是 bot 自己发的 ⇒ 那类**永远查不到**，且不报错）",
         '"FROM wpp_messages WHERE account_id = %s AND ("\n',
         '"FROM wpp_messages WHERE account_id = %s AND direction = \'inbound\' AND ("\n'),
        ("★ 两个 id 都空时照样发查询（`msg_id = NULL` 恒不匹配 —— 白跑一次库）",
         '    if not mid and not nid:\n        return None\n',
         '    if False:\n        return None\n'),
        ("★ 空白 id 不 strip（「  」被当成真 id 发出去）",
         '    mid = str(msg_id or "").strip()\n    nid = str(new_msg_id or "").strip()\n',
         '    mid = str(msg_id or "")\n    nid = str(new_msg_id or "")\n'),
        ("★ 决胜键丢掉（同 id 多行时「选中的是哪一行」不可复现 ⇒ 昵称时有时无）",
         '") ORDER BY ts DESC, id DESC LIMIT 1",\n', '") LIMIT 1",\n'),
        ("★★ `raw_payload` 那一列不取（身份链的第 2、3 档静默失效，只表现为「@ 不出来」）",
         '"from_wxid, msg_type, content, raw_payload, UNIX_TIMESTAMP(ts) AS at_sec "\n',
         '"from_wxid, msg_type, content, UNIX_TIMESTAMP(ts) AS at_sec "\n'),
    ]),
    # ------------------------------------------- W-Q3 装配（适配器接线）
    # 这一段混着 AST 形状判据与真跑判据（见该测试文件头）：形状判据钉"接在对的地方"，
    # 行为判据钉"取到的东西对" —— 两者都不能少（这一项的失效形态全都长得一样）。
    "quote-adapter": (ADAPTER, "plugin/tests/platform/test_quote_identity_wiring.py", [
        ("★★ 不回查 DB（身份恒空 ⇒ 整条 @ 链的输入又死回去，正是老板报的那个 bug）",
         '            row = repo_messages.get_message_by_id_or_new_id(acct.id, msg_id=svrid,\n'
         '                                                           new_msg_id=svrid)\n',
         '            row = None\n'),
        ("★★ 只用 msg_id 查（锚点其实是服务端 new_msg_id 的那一半永远匹配不上）",
         '            row = repo_messages.get_message_by_id_or_new_id(acct.id, msg_id=svrid,\n'
         '                                                           new_msg_id=svrid)\n',
         '            row = repo_messages.get_message_by_id_or_new_id(acct.id, msg_id=svrid)\n'),
        ("★ metadata 与 DB 身份的优先级反过来（参数优先是 TS 的 params.fromWxid 那一档）",
         "(metadata or {}).get(\"quote_from_wxid\") or ident.fromusr\n",
         "ident.fromusr or (metadata or {}).get(\"quote_from_wxid\")\n"),
        ("★★ 行内 peer_name 不传下去（第 2 级永远空 —— 真机上那是**唯一**能拿到名字的一级）",
         '                                              db_name=ident.display_name)\n',
         '                                              db_name="")\n'),
        ("★★ 前缀不并进 XML（算了一路，发出去的还是没 @ 的那一句）",
         '                reply_content=f"{prefix}{chunk}".strip(), svrid=svrid, fromusr=fromusr)\n',
         '                reply_content=chunk, svrid=svrid, fromusr=fromusr)\n'),
        ("★ 记账记的是带前缀的文本（库里每条群回复都多一个 @名字，复读判据与引用回填跟着错位）",
         '        await self._persist_outbound(acct, peer_id, chunk, ids)\n',
         '        await self._persist_outbound(acct, peer_id, f"{prefix}{chunk}", ids)\n'),
        ("★★ 成本闸拆掉（私聊 / 拿不到人时也去打联系人表与厂商接口）",
         '        if not outbound.is_group_peer(peer_id) or not fromusr:\n            return ""\n',
         '        if not fromusr:\n            return ""\n'),
        ("★ 联系人表那一级跳过（少一个来源 ⇒ 未同步通讯录的账号只剩 wxid 兜底）",
         '            name = self._contact_name(acct.id, fromusr)\n',
         '            pass  # 变异：跳过联系人表\n'),
        ("★★ 厂商回包只认扁平键（多一层 ContactList ⇒ 永远取不到，旧写法就是这样）",
         '        if isinstance(contact_list, list) and contact_list:\n',
         '        if False and contact_list:\n'),
        ("★★ 厂商取昵称那一道群 id 闸拆掉（拿群 id 去查昵称，白跑一次外呼）",
         '        if not wxid or not quote_ref.is_usable_sender(wxid):\n            return ""\n',
         '        if not wxid:\n            return ""\n'),
        ("★★ 昵称缓存不写（引用回复在群里每条都走一遍 ⇒ 每句话多打一次厂商接口）",
         '        self._name_cache[f"{acct.id}:{wxid}"] = (name, time.time())\n',
         '        pass  # 变异：不缓存\n'),
    ]),
    # ------------------------------------------- R1 触发限速闸（纯逻辑）
    # 2026-10-03「两个机器人互相 @ 死循环」的止血闸。这一项的失效形态**全都是安静的**：
    # 没有回路时它本来就不吭声，所以"闸没生效"与"闸正常工作"在日志里长得一模一样
    # （只在真出回路那天才分得出来）⇒ 每个坏法都必须由用例**当场**钉住。
    "rate-limit-core": (ROOT / "plugin" / "inbound" / "rate_limit.py",
                        "plugin/tests/test_rate_limit.py", [
        ("★★ 上限判反（`>=` 改 `>`：要多刷一次才掐 —— 边界差一次，回路多活一轮）",
         "        if len(bucket.hits) >= self.max_triggers:\n",
         "        if len(bucket.hits) > self.max_triggers:\n"),
        ("★★ 静默期不生效（`muted_until` 恒过去 ⇒ 掐一次之后立刻放行，等于只丢一条）",
         "        if bucket.muted_until > t:\n",
         "        if False:\n"),
        ("★★ 静默时长写成 0（判定说「拒」，下一条立刻放行）",
         "            bucket.muted_until = t + self.mute_s\n",
         "            bucket.muted_until = t\n"),
        ("★★ 静默期继续计数（持续来刷 = 无限续期 = 永久封禁，比死循环更难发现）",
         "        if bucket.muted_until > t:\n            return Decision(False, \"muted\", hits=len(bucket.hits),\n"
         "                            muted_until=bucket.muted_until)\n",
         "        if False:\n            return Decision(False, \"muted\", hits=len(bucket.hits),\n"
         "                            muted_until=bucket.muted_until)\n"),
        ("★★ 出错时拒（fail-open 变 fail-closed：限速器一坏，所有 @ 全被吞掉）",
         '            return Decision(True, "error")\n',
         '            return Decision(False, "error")\n'),
        ("★★ 拿不到发送者也建桶（空 key 共享额度 ⇒ 群里不相干的人被一起静默）",
         "        if not sender_id:\n",
         "        if False:\n"),
        ("★★ 受管集合放宽成「所有 reason」（私聊/关键词/心流候选全被限速）",
         "        if reason not in GUARDED_REASONS:\n",
         "        if False:\n"),
        ("★★ 受管集合空掉（闸还在、日志还会打，但**一条都锁不住**）",
         'GUARDED_REASONS = frozenset({"group_mention", "group_solitaire"})',
         "GUARDED_REASONS = frozenset()"),
        ("★★ 两条腿分开计账（@ 与接龙交替用 ⇒ 上限直接翻倍）",
         '        key = (account_id or "", group_id or "", sender_id)\n',
         '        key = (account_id or "", group_id or "", sender_id, reason)\n'),
        ("★★ 桶不带账号（多账号同一群同一个人共享额度 ⇒ 互相牵连）",
         '        key = (account_id or "", group_id or "", sender_id)\n',
         '        key = ("", group_id or "", sender_id)\n'),
        ("★★ 桶不带群（同一个人在 A 群刷屏 ⇒ B 群的正常 @ 被掐）",
         '        key = (account_id or "", group_id or "", sender_id)\n',
         '        key = (account_id or "", "", sender_id)\n'),
        ("★★ 窗口不裁剪（计数只增不减 ⇒ 慢速说话的人迟早被掐）",
         "            bucket.hits = [h for h in bucket.hits if h > cutoff]\n",
         "            bucket.hits = list(bucket.hits)\n"),
        ("★★ 掐停不打 WARNING（journald 只采 WARNING+ ⇒ 出事时现场看不见）",
         '            log.warning("触发限速闸：',
         '            log.debug("触发限速闸：'),
        ("★ `reset(None)` 之外也清全部（清一个账号顺手清掉别人 ⇒ 额度被悄悄重置）",
         "        if account_id is None:\n            self._buckets.clear()\n            return\n",
         "        if True:\n            self._buckets.clear()\n            return\n"),
        ("★★ 从不清扫（长跑进程 + 群成员流动 ⇒ 桶只增不减）",
         "        if len(self._buckets) <= self.max_buckets:\n            return\n",
         "        if True:\n            return\n"),
        ("★★ 静默中的桶被清扫掉（刷得越狠越早被放行 —— 判据刚好反了）",
         "                 if b.muted_until <= now and (not b.hits or max(b.hits) <= cutoff)]\n",
         "                 if (not b.hits or max(b.hits) <= cutoff)]\n"),
        ("★★ `now=` 被忽略（显式时点失效 ⇒ 静默基准错到时钟上去）",
         "        t = self._clock() if now is None else float(now)\n"
         "        key = (account_id or \"\", group_id or \"\", sender_id)\n",
         "        t = self._clock()\n"
         "        key = (account_id or \"\", group_id or \"\", sender_id)\n"),
    ]),
    # ------------------------------------------- R1 装配（适配器接线）
    # ``adapter.py`` 本机 import 不了 ⇒ 这一套只能按 AST 源码形状验（见该测试文件头）。
    # ⚠️ **没有**"把闸整块搬到 ④z 之后"那条变异：忠实地破坏顺序要把整块代码搬家，
    #    变异框架的单次 ``str.replace`` 表达不了（同 W5 的 ``disconnect`` 顺序、
    #    W7-3 的 ④d 位置）。它只有正面判据 ``test_c4``（下标比较）。
    "rate-limit-adapter": (ADAPTER, "plugin/tests/platform/test_rate_limit_wiring.py", [
        ("★★ 闸每条消息新建一个（状态只有本次计数 ⇒ **一次都拦不住**，且没有任何痕迹）",
         "        _limit = self._trigger_limiter.guard(\n",
         "        _limit = TriggerRateLimiter().guard(\n"),
        ("★★ 闸不挂在实例上（`None` ⇒ 每条硬触发都 AttributeError，被兜底吞掉后消息全丢）",
         "        self._trigger_limiter = TriggerRateLimiter()\n",
         "        self._trigger_limiter = None  # 变异\n"),
        ("★★ 判了不丢（只打 WARNING 不 return ⇒ 日志一行行地刷，回路照旧）",
         "                        _limit.hits, _limit.muted_until, msg.msg_id)\n            return\n",
         "                        _limit.hits, _limit.muted_until, msg.msg_id)\n            pass\n"),
        ("★★ 掐停降成 debug（.183 的 journald 只采 WARNING+ ⇒ 现场看不见）",
         '            log.warning("[WPP:%s] 触发限速：',
         '            log.debug("[WPP:%s] 触发限速：'),
        ("★★ 桶的群维度丢掉（`peer_id` 兜底那半拆掉 ⇒ 群里撞别处会话的桶）",
         "            acct.id, msg.chatroom_id or msg.peer_id, msg.from_wxid, reason)\n",
         "            acct.id, msg.peer_id, msg.from_wxid, reason)\n"),
        ("★★ 拿 bot 自己当发送者（全群共用一个桶 ⇒ 一个人刷屏、全群被静默）",
         "            acct.id, msg.chatroom_id or msg.peer_id, msg.from_wxid, reason)\n",
         "            acct.id, msg.chatroom_id or msg.peer_id, acct.self_wxid, reason)\n"),
        ("★★ reason 写死成不受管的值（闸永不生效 —— 与「集合空掉」同形，但在接线侧）",
         "            acct.id, msg.chatroom_id or msg.peer_id, msg.from_wxid, reason)\n",
         '            acct.id, msg.chatroom_id or msg.peer_id, msg.from_wxid, "group_keyword")\n'),
        ("★ 阈值从闸模块导进适配器（第二份真源：迟早与 rate_limit.py 不一致）",
         "from ..inbound.rate_limit import TriggerRateLimiter\n",
         "from ..inbound.rate_limit import MUTE_S, WINDOW_S, TriggerRateLimiter\n"),
        ("★ 受管集合在适配器里又判一遍（分叉的那一天，闸与日志说的不是一回事）",
         "        if not _limit.allowed:\n",
         '        if reason in ("group_mention", "group_solitaire") and not _limit.allowed:\n'),
        ("★ 下游又判一次（第二个判定点 ⇒ 判定语义按最后一处为准）",
         "        await asyncio.to_thread(persist_inbound, acct, msg)\n",
         "        self._trigger_limiter.guard(acct.id, msg.chatroom_id or msg.peer_id,\n"
         "                                    msg.from_wxid, reason)\n"
         "        await asyncio.to_thread(persist_inbound, acct, msg)\n"),
    ]),
    # ------------------------------------------- OSS 上传（R2 补的第 1 套门，2026-10-03）
    # ★ 这一套是 R2 落的**补课**：``media_oss.py`` 是 W7-1 的产物，而它此前**没有任何变异套件**
    #   （gates 里的 ``media-enrich*`` 三套打的是 ``adapter.py`` / ``parser.py``，
    #   跑的是 ``test_media_enrich.py``）⇒ 上传器本身「失败必须安静降级」的四条不变量
    #   只有正面判据、没有反面判据。
    # ⚠️ 2026-10-03 在 ``.183`` 落 OSS 时才想明白这模块的**真实依赖**：AK/SK 读进来了却
    #    **一个字节都不往下传**（与 TS 同款），能上传成功全靠机器的 ``~/.ossutilconfig``
    #    ⇒ 第 1 条变异（把密钥摆到命令行上）既是安全红线，也是"顺手加个 -i"的典型走法。
    "media-oss": (MEDIA_OSS, "plugin/tests/test_media_oss.py", [
        ("★★ 密钥摆上命令行（argv 是全局可见的：ps / /proc/<pid>/cmdline / 报错截图）",
         '             "--endpoint", oss.endpoint, "-f"],\n',
         '             "-i", oss.access_key_id, "-k", oss.access_key_secret,\n'
         '             "--endpoint", oss.endpoint, "-f"],\n'),
        ("★★ 脱敏闸拆掉（ossutil 的 stderr 原样进日志 ⇒ 密钥泄漏，且「看不出泄漏了」）",
         "    for secret in _SECRETS:\n",
         "    for secret in ():\n"),
        ("★ 降级变抛（媒体富化失败把整条消息带崩 —— TS 六个 enrich 入口都写着不抛）",
         '    except Exception as exc:  # noqa: BLE001 —— 媒体富化失败绝不能冒泡成"消息处理失败"\n',
         '    except ZeroDivisionError as exc:  # noqa: BLE001\n'),
        ("★ 空字节也去上传（0 字节对象进 OSS，公网直链指着空文件）",
         "    if not buf:\n        return None\n",
         "    if not buf and False:\n        return None\n"),
        ("★ 非 ASCII 也进 OSS key（JS 的 \\w 是 ASCII，Python 的不是 ⇒ 两边产出两套目录）",
         '_UNSAFE_NAME_RE = re.compile(r"[^\\w.\\-]", re.ASCII)',
         '_UNSAFE_NAME_RE = re.compile(r"[^\\w.\\-]")'),
        ("★ 缓存永不命中（TTL 写反 ⇒ 媒体热路径每条消息都读一次盘）",
         "    _CACHE[p] = (t + CRED_TTL_S, cfg)\n",
         "    _CACHE[p] = (t - 1.0, cfg)\n"),
        ("★ 缺 endpoint 时不回默认值（TS 同款兜底拆掉 ⇒ 半份凭据静默变「没有凭据」）",
         '                    endpoint if isinstance(endpoint, str) and endpoint else DEFAULT_ENDPOINT)',
         '                    endpoint if isinstance(endpoint, str) and endpoint else "")'),
    ]),
}


def run_tests(test_file: str) -> tuple[bool, str]:
    """跑一个测试文件。**必须带超时** —— 变异体可能让测试「挂住」而不是「变红」。

    实测教训（W5，2026-10-02）：``sync_fallback`` 的并发用例把锁改成装饰之后，第二次触发
    会去等一个没人 ``set()`` 的闸门 ⇒ pytest 永久停在那里。而本脚本是**串行**跑整批：
    挂一次就等于整轮门禁停摆（这次卡了 11 分钟才被人发现），而且中途被 SIGTERM 杀掉时
    那个变异体**还留在盘上** —— 于是下一次"基线"红得莫名其妙，差点被当成真 bug 去修。
    所以两头都补：这里超时就判失败（诚实地标注成超时，不冒充"变红"），
    ``check()`` 那边注册信号处理器把原文写回去。
    """
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", test_file, "-q", "--no-header",
                            "-p", "no:cacheprovider"],
                           cwd=ROOT, capture_output=True, text=True, timeout=TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        out = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes)
               else (exc.stdout or "")) + ((exc.stderr or b"").decode("utf-8", "replace")
                                          if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
        return False, f"TIMEOUT（{TEST_TIMEOUT_S}s 内没跑完 —— 变异体把测试挂住了）\n{out}".strip()
    return p.returncode == 0, (p.stdout + p.stderr).strip()


#: 单次测试运行的墙钟上限。正常一个文件几秒到几十秒；真的慢了**先怀疑挂起**，别调大它。
TEST_TIMEOUT_S = 300

#: 当前正在变异的文件与它的原文 —— 只为**信号处理器**服务（见 ``_restore_now``）。
_CURRENT: list[Path] = []
_ORIGINAL: list[str] = []


def _restore_now(*_args: Any) -> None:
    """被 SIGTERM/SIGINT 打断时先把盘上的原文写回去，别把变异体留给下一个人。"""
    if _CURRENT and _ORIGINAL:
        try:
            _CURRENT[0].write_text(_ORIGINAL[0], encoding="utf-8")
            _purge_bytecode(_CURRENT[0])
            print(f"\n⚠️  收到中断信号：已把 {_CURRENT[0]} 还原（不留变异体在盘上）")
        except Exception:  # noqa: S110, BLE001 —— 还原失败也要让退出码说话
            pass
    raise SystemExit(130)


def _collection_reason(tail: str) -> str:
    """BROKEN 时给出**有信息量**的那一行（不是"最后一行"）。

    实测教训（W7-2，2026-10-02）：锚点只锚了半行 ⇒ 替换后在源文件里留下
    ``: —— 富化失败绝不能冒泡成"消息处理失败"`` 这种悬空尾巴 ⇒ 收集期 ``SyntaxError``。
    而这里原先只打印最后一行，那一行是 ``Interrupted: 1 error during collection`` ——
    **看起来像"变异体把测试带崩了"，其实是我们自己写坏了变异**，为此多花了一轮。
    所以按"具体错误优先"挑一行：源码位置 + 错误类型才是能直接动手的那句。
    """
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    for pat in ("SyntaxError", "IndentationError", "ImportError", "AttributeError",
                "NameError", "TypeError"):
        for ln in lines:
            if pat in ln:
                return ln[:220]
    return lines[-1][:220] if lines else "(无输出)"


def _purge_bytecode(target: Path) -> None:
    """跑测试之前删掉 ``target`` 所在目录的 ``__pycache__``。

    ★ 实测（W7-1，2026-10-02，已端到端复现）：Python 的 ``.pyc`` 头部只用
    「**秒级** mtime + 源文件大小」判新鲜度（``__pycache__/*.pyc`` 的 16 字节头）。
    于是这三步连在一起时 —— 写变异体 → 跑测试（编译出**变异体**的 pyc）→ 立刻还原
    （**同一秒**，且两版源码**长度相同**）—— 还原后的那次测试**跑的仍是变异体的字节码**。

    现象：``[media-enrich] … 还原后❌ 红了 —— 还原不干净！``，紧接着**下一个套件的
    基线也全红**（同一个陈旧 pyc 又被读一次）。看起来像"还原逻辑坏了"或"源码改坏了"，
    而盘上的字节其实**一直是对的** —— 这一轮为此白查了一次。

    不是假想：``KIND_IMAGES`` → ``KIND_VIDEOS`` 恰好同为 11 字符。复现方式是把还原后的
    mtime 拨回变异体那一秒（红）→ 删 ``__pycache__`` → 绿。只清目标文件旁那一个目录，
    代价可以忽略。
    """
    shutil.rmtree(target.parent / "__pycache__", ignore_errors=True)


def check(name: str) -> tuple[int, int, int]:
    target, test_file, mutants = SUITES[name]
    original = target.read_text(encoding="utf-8")

    def _run() -> tuple[bool, str]:
        # ★ 每次跑测试前清一次目标文件旁的 ``__pycache__`` —— 见 :func:`_purge_bytecode`
        #   的实测说明（同秒 + 同长度 ⇒ 读到陈旧字节码，看起来像"还原坏了/源码坏了"）。
        _purge_bytecode(target)
        return run_tests(test_file)

    green, tail = _run()
    if not green:
        print(f"\n❌ [{name}] 基线就没过，先修测试：\n{tail[-2000:]}")
        return 0, 1, 0
    print(f"\n[{name}] 基线全绿 ✓ —— {len(mutants)} 条变异（KILLED 才算过）")

    killed: list[str] = []
    survived: list[str] = []
    broken: list[str] = []
    _CURRENT.append(target)
    _ORIGINAL.append(original)
    try:
        for desc, old, new in mutants:
            n = original.count(old)
            if n != 1:
                print(f"  ⚠️  锚点命中 {n} 次（应为 1）—— 变异本身写坏了: {desc}")
                broken.append(desc)
                continue
            target.write_text(original.replace(old, new, 1), encoding="utf-8")
            ok, tail = _run()
            if "during collection" in tail or "Interrupted" in tail or "SyntaxError" in tail:
                print(f"  ⚙️  BROKEN（收集期就崩，不计入）: {desc} —— {_collection_reason(tail)}")
                broken.append(desc)
            elif ok:
                print(f"  ❌ SURVIVED（测试没抓到）: {desc}")
                survived.append(desc)
            elif tail.startswith("TIMEOUT"):
                # 挂住而非变红：**算杀**（测试确实对这条行为敏感），但要看得见是哪种杀法 ——
                # 否则"挂死"会伪装成一条普通 KILLED，下次真挂起又是 11 分钟。
                print(f"  ⏱️  KILLED（超时挂住，不是变红）: {desc}")
                killed.append(desc)
            else:
                print(f"  ✅ KILLED: {desc}")
                killed.append(desc)
    finally:
        target.write_text(original, encoding="utf-8")
        _CURRENT.clear()
        _ORIGINAL.clear()

    ok, _ = _run()
    print(f"[{name}] KILLED {len(killed)} / SURVIVED {len(survived)} / BROKEN {len(broken)}"
          f"；还原后{'全绿 ✓' if ok else '❌ 红了 —— 还原不干净！'}")
    if not ok:
        return len(killed), 1, len(broken)
    return len(killed), len(survived), len(broken)


def main() -> int:
    # 被 Ctrl-C / timeout 打断时先把变异体擦掉（见 ``_restore_now`` 的说明）。
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _restore_now)
        except (ValueError, OSError):  # 非主线程/平台不支持 —— 不致命
            pass
    names = sys.argv[1:] or list(SUITES)
    bad = 0
    for name in names:
        if name not in SUITES:
            print(f"未知模块: {name}（可选: {sorted(SUITES)}）")
            return 2
        k, s, b = check(name)
        bad += s + b
    print()
    if bad:
        print(f"❌ 有 {bad} 条未通过 —— 变异存活说明测试有盲区，锚点失效说明脚本自己坏了")
        return 1
    print("✅ 全部变异被杀死，未发现测试盲区")
    return 0


if __name__ == "__main__":
    sys.exit(main())
