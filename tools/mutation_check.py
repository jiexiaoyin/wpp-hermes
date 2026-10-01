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

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HF = ROOT / "plugin" / "inbound" / "heartflow"
ADAPTER = ROOT / "plugin" / "platform" / "adapter.py"

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
         "repo_hf.set_sent(account_id, inbound_msg_id, at_sec, at_sec + int(observe_sec),",
         "repo_hf.set_sent(account_id, inbound_msg_id, at_sec, at_sec,"),
        ("观察窗不按 observeWindowSec（收窗时刻与声明不符）",
         "at_sec + int(observe_sec)", "at_sec + 3600"),
        ("无文本也进重复历史（拿没说的话拦下一句）",
         "            if text:\n", "            if True:\n"),
        ("sent 不记真发计数（金丝雀无源）", "metrics.inc_hf_sent()", "pass"),
        ("reason 映射：重复闸记成 dedup",
         '            reason = "repeat"', '            reason = "dedup"'),
        ("reason 映射：空文本记成 repeat",
         '            reason = "empty-text"', '            reason = "repeat"'),
        ("pending 也当 suppressed 落库（重试语义被吃掉）",
         "        if outcome == HF_SEND_OUTCOME_SUPPRESSED:",
         "        if outcome != HF_SEND_OUTCOME_SENT:"),
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
         "            usable = [s for s in samples\n                      if label.is_hf_sample_informative(",
         "            usable = [s for s in samples\n                      if True or label.is_hf_sample_informative("),
        ("调阈不判冷却（每轮都改）",
         '            if not hf_cooldown_ok(None if st is None else st.get("last_change_at"), ts,\n'
         '                                  L["minChangeCooldownSec"]):\n                continue\n', ""),
        ("审计 reason 漏掉 skipped/ambientP（归因没了）",
         'detail = (f"{res.reason} skipped={len(samples) - len(usable)} "',
         'detail = (f"{res.reason} "'),
        ("金丝雀从未发过报 0（停摆看起来正常）",
         "set_hf_last_send_age_sec(-1 if last is None else max(0, ts - int(last)))",
         "set_hf_last_send_age_sec(0 if last is None else max(0, ts - int(last)))"),
        ("金丝雀允许负数（时钟回拨掩盖停摆）",
         "max(0, ts - int(last))", "ts - int(last)"),
        ("本底查询失败不吭声（阈值悄悄漂）",
         'log.warning("[WPP HF] ambient baseline query failed (弱信号将不过滤): %s", exc)',
         'log.debug("[WPP HF] ambient baseline query failed: %s", exc)'),
        ("本底不看小时段（整天本底混进判定）",
         "            if hour != cur_hour:\n                continue\n", ""),
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
         "    if L.classify_hf_send(result) == L.HF_SEND_OUTCOME_PENDING:\n", "    if False:\n"),
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
         "            if text:\n"
         "                dedupe.note_recent_reply(account_id, group_id, text, at_sec,\n"
         "                                         dedupe_cfg or dedupe.resolve_dedupe_cfg(None))",
         "            if False:\n"
         "                dedupe.note_recent_reply(account_id, group_id, text, at_sec,\n"
         "                                         dedupe_cfg or dedupe.resolve_dedupe_cfg(None))"),
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
        ("接龙判据改成恒 False（100% 必回这一路静默失效）",
         '    return "接龙" in (msg.content or "")',
         "    return False"),
        ("私聊非白名单也放行（P0 联系人 fan-out 的形态）",
         '        return (True, "dm") if msg.from_wxid in allow_from else (False, "dm_not_allowlisted")',
         '        return True, "dm"'),
        ("关键词路恒不命中（配了关键词也不触发，日志上只看到「没命中」）",
         '    if _matches_keyword(content, cfg.get("keywordTrigger") or {}, chatroom_id):',
         "    if False:"),
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
         "    if not credentials.any_configured(accounts):\n        return None",
         "    if False:\n        return None"),
        ("home_channel 的名字读错变量（``_NAME`` 后缀丢了 ⇒ 群名恒为 Home）",
         'str(os.environ.get(f"{_HOME_ENV}_NAME") or "Home")',
         "str(os.environ.get(_HOME_ENV) or \"Home\")"),
        ("validate_config 恒真（`hermes config` 说配好了、凭证门却说不）",
         "    return is_connected(config)",
         "    return True"),
        ("平台名漂一个字（工具挂不到平台上，而注册计数全对 ⇒ 两边都不报错）",
         'PLATFORM_NAME = "wechatpadpro"',
         'PLATFORM_NAME = "wechatpadprox"'),
    ]),
    "accounts": (ROOT / "plugin" / "core" / "accounts.py",
                 "plugin/tests/test_accounts.py", [
        ("authcode 回到默认 repr（凭据随任意一行日志进 journald / /tmp）",
         "    authcode: str = field(repr=False)",
         "    authcode: str"),
        ("生产 bot 身份守卫删掉（红线 2：能拿生产号去连调试实例）",
         "    if acct.is_production_identity and not _env_bool(ALLOW_PRODUCTION_ENV):",
         "    if False:"),
        ("env 兜底不再生效（default.json 的空串占位让凭据永远填不进去 —— 起不来的那半截）",
         "    for name in fallback_envs:\n        got = _env(name)\n        if got:\n            return got",
         "    for name in []:\n        got = _env(name)\n        if got:\n            return got"),
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
    ]),
    "registration": (ROOT / "plugin" / "registration.py",
                     "plugin/tests/test_registration.py", [
        ("★ 独有能力导入前缀退回 f\".{mod_name}\"（F5 原样复活：wecom 7 + phoneerp 4 + extras 3 静默消失）",
         'importlib.import_module(f".tools.{mod_name}", __package__)',
         'importlib.import_module(f".{mod_name}", __package__)'),
        ("注册失败只记 DEBUG（F5 的第二个条件：既丢掉工具、又不留痕）",
         'log.warning("[WPP] %s 注册失败: %s", label, exc, exc_info=True)',
         'log.debug("[WPP] %s 注册失败: %s", label, exc)'),
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
         'os.environ.get("WECHATPRO_STT_API_KEY")\n            or os.environ.get("SILICONFLOW_API_KEY")',
         'os.environ.get("SILICONFLOW_API_KEY")\n            or os.environ.get("WECHATPRO_STT_API_KEY")'),
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
        ("★ 先算正文再取媒体（语音转写永远赶不上正文这班车）",
         "        media_urls, media_types = await self._fetch_media(acct, msg)\n"
         "        text = pipeline.prepare_text(msg)\n",
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
        ("group del 也去动群聊白名单（把老板的群白名单一起删了）",
         "        if sub == \"add\":", "        if True:"),
        ("写覆盖丢掉底稿（一次改阈值把白名单/开关一起抹掉）",
         '    cfg["replyThreshold"] = v', '    cfg = {"replyThreshold": v}'),
        ("写配置不说明「重启回退」（老板以为改好了，重启后无声回到旧值）",
         '"⚠️ 这是**内存覆盖**：进程重启后回退到账号配置里的值（要持久请改 accounts/*.json）"',
         '""'),
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
}


def run_tests(test_file: str) -> tuple[bool, str]:
    p = subprocess.run([sys.executable, "-m", "pytest", test_file, "-q", "--no-header", "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def check(name: str) -> tuple[int, int, int]:
    target, test_file, mutants = SUITES[name]
    original = target.read_text(encoding="utf-8")
    green, tail = run_tests(test_file)
    if not green:
        print(f"\n❌ [{name}] 基线就没过，先修测试：\n{tail[-2000:]}")
        return 0, 1, 0
    print(f"\n[{name}] 基线全绿 ✓ —— {len(mutants)} 条变异（KILLED 才算过）")

    killed: list[str] = []
    survived: list[str] = []
    broken: list[str] = []
    try:
        for desc, old, new in mutants:
            n = original.count(old)
            if n != 1:
                print(f"  ⚠️  锚点命中 {n} 次（应为 1）—— 变异本身写坏了: {desc}")
                broken.append(desc)
                continue
            target.write_text(original.replace(old, new, 1), encoding="utf-8")
            ok, tail = run_tests(test_file)
            last = tail.splitlines()[-1] if tail else "(无输出)"
            if "during collection" in tail or "Interrupted" in tail or "SyntaxError" in tail:
                print(f"  ⚙️  BROKEN（收集期就崩，不计入）: {desc} —— {last}")
                broken.append(desc)
            elif ok:
                print(f"  ❌ SURVIVED（测试没抓到）: {desc}")
                survived.append(desc)
            else:
                print(f"  ✅ KILLED: {desc}")
                killed.append(desc)
    finally:
        target.write_text(original, encoding="utf-8")

    ok, _ = run_tests(test_file)
    print(f"[{name}] KILLED {len(killed)} / SURVIVED {len(survived)} / BROKEN {len(broken)}"
          f"；还原后{'全绿 ✓' if ok else '❌ 红了 —— 还原不干净！'}")
    if not ok:
        return len(killed), 1, len(broken)
    return len(killed), len(survived), len(broken)


def main() -> int:
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
