"""心流重复内容闸回归 —— TS ``tests/unit/heartflow-dedupe.test.mjs`` (v1.9.0) 的 pytest 译本。

## 为什么这批测试重要

闸是**唯一会主动拦下 bot 发言**的机制 ⇒ **误杀的代价比漏放高**。
误杀信号 = 群里明显该接的话 bot 装死；老板看到的是"bot 变笨了"，而不是"闸太严了"。

⇒ 这里把三道误杀防护（前缀优先 / ``minChars`` 豁免 / 数字差异）**逐个钉死**，
外加一条**结构性**断言：闸与"重复率"观测必须共用 :func:`dedupe.hf_repeat_verdict`
（若各写一套，日报说重复率很高而闸一条都没拦，"观测驱动收口"就是假的 —— I4）。
"""

from __future__ import annotations

import re
from pathlib import Path

from plugin.inbound.heartflow import dedupe as d

CFG = d.resolve_dedupe_cfg(None)
_CFG_KEYS = ("enabled", "proactiveOnly", "windowSec", "simThreshold", "minChars", "historyMax")


def _hist(text: str, at_sec: int, norm: str | None = None) -> d.HfRecentReply:
    return d.HfRecentReply(text=text, at_sec=at_sec, norm=norm)


# ===== 1. 归一化 =====


def test_norm_strips_noise_but_keeps_digits():
    """去 emoji/@/全角/标点/空白，小写；**数字保留**（数字是语义，不是噪声）。"""
    assert d.norm_hf_text("你好，世界！") == "你好世界"
    assert d.norm_hf_text("@张三 在吗？") == "在吗"
    assert d.norm_hf_text("iPhone 15 多少钱") == "iphone15多少钱"
    assert d.norm_hf_text("ＡＢＣ　１２３") == "abc123", "全角字母/数字 + 全角空格"
    assert d.norm_hf_text("收到 👌🎉") == "收到"
    assert d.norm_hf_text("") == ""
    # 换行/多空格不该造成差异（同一句话换个排版不算两句）
    assert d.norm_hf_text("好的\n  \n我看看") == d.norm_hf_text("好的 我看看")


def test_emoji_regex_code_points_are_exact():
    """**Py 侧新增的守卫**：把 :data:`dedupe._EMOJI_RE` 的码点逐个钉死。

    为什么值得单测：这段正则里有零宽连接符（U+200D）与变体选择符（U+FE00–FE0F），
    它们在编辑器里**看不见** —— 一次不经意的编辑就可能吃掉区间端点，而症状是
    "emoji 偶尔没剥干净 ⇒ 相似度算低 ⇒ 闸静默少拦"，**没有任何报错**。
    （同一类坑：全角 ``！`` 与半角 ``!`` 长得几乎一样，差一个码点就归一化失效。）
    """
    inside = [0x1F000, 0x1F5FF, 0x1FAFF, 0x1F1E6, 0x1F1FF,
              0x2190, 0x21FF, 0x2300, 0x27BF, 0x2B00, 0x2BFF,
              0xFE00, 0xFE0F, 0x200D]
    outside = [0x1EFFF, 0x1FB00, 0x218F, 0x2200, 0x27C0,
               0x2AFF, 0x2C00, 0xFDFF, 0xFE10, 0x200C, 0x200E, 0x0041, 0x4E2D]
    for cp in inside:
        assert d._EMOJI_RE.match(chr(cp)), f"U+{cp:04X} 应在 emoji 区内"
    for cp in outside:
        assert not d._EMOJI_RE.match(chr(cp)), f"U+{cp:04X} 不该被当成 emoji"

    assert d._FULLWIDTH_RE.match("！") and d._FULLWIDTH_RE.match("～") and d._FULLWIDTH_RE.match("Ａ")
    assert not d._FULLWIDTH_RE.match("!") and not d._FULLWIDTH_RE.match("A")
    assert d._IDEOGRAPHIC_SPACE_RE.match("　")
    assert not d._IDEOGRAPHIC_SPACE_RE.match(" ")


# ===== 2. Jaccard =====


def test_jaccard_intersection_over_union_and_empty_is_zero():
    assert d.hf_jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert d.hf_jaccard({"a", "b"}, {"c", "d"}) == 0.0
    assert d.hf_jaccard(set(), {"a"}) == 0.0
    assert d.hf_jaccard({"a"}, set()) == 0.0
    assert d.hf_jaccard(set(), set()) == 0.0, "两边都空也必须 0（不得除零）"
    # {a,b,c} ∩ {a,x,y} = 1, ∪ = 5 ⇒ 0.2
    assert abs(d.hf_jaccard({"a", "b", "c"}, {"a", "x", "y"}) - 0.2) < 1e-9

    # 说明：`if not a or not b` 与 `union <= 0` 两处守卫都是**快路径**，
    # 去掉后走通用分支结果完全相同（两边非空 ⇒ union ≥ 1 恒成立）。
    # 它们是**等价变异**，杀不掉，所以不进 tools/mutation_check.py —— 见那里的注释。


# ===== 3. prefix 命中 =====


def test_prefix_hit_even_when_tail_differs():
    """归一化前缀命中（即使尾部不同）⇒ ``prefix``。"""
    # 归一化后公共前缀 = "亲这款现在有现货的哦今天下单明天到"（>minChars）—— 只有尾部不同
    hist = [_hist("亲，这款现在有现货的哦今天下单明天到，可以拍下", 1000)]
    v = d.hf_repeat_verdict("亲，这款现在有现货的哦今天下单明天到，要的话直接拍就行", hist, CFG, 1100)
    assert v is not None, "前 12 字相同必须命中"
    assert v.kind == "prefix"
    assert v.prev_at == 1000


# ===== 4. similar 命中 =====


def test_similar_hit_by_3gram_jaccard():
    """前缀不同但**整体**高度相似 ⇒ ``similar``（3-gram Jaccard ≥ 0.85）。"""
    body = "这款手机现在活动价是3999元，支持分期免息，可以到店自提也可以发顺丰，需要我帮您留一台吗"
    a = f"您问的{body}"
    b = f"老板您问的{body}"
    v = d.hf_repeat_verdict(b, [_hist(a, 500)], CFG, 600)
    assert v is not None, "整体相似必须命中（否则长回复换个称呼就复读了）"
    assert v.kind == "similar"
    assert v.sim >= CFG.sim_threshold, f"sim={v.sim}"
    # 前缀判定确实没中（证明这条测的是 similar 分支，而不是被 prefix 抢走）
    assert d.norm_hf_text(a)[:CFG.min_chars] != d.norm_hf_text(b)[:CFG.min_chars]


def test_similarity_threshold_is_inclusive():
    """阈值判定是 ``sim >= threshold`` ⇒ 命中（**含**边界，与 TS 一致）。

    ⚠️ 这条用的是**构造出来恰好相等**的一对串，不是真实语料 —— 浮点相等本来就难碰上，
    但"恰好等于阈值"正是 ``<`` 与 ``<=`` 唯一分道扬镳的地方，边界必须钉死：
    写成 ``<=`` 的后果是**阈值 1.0 的配置永远不拦任何东西**（静默失效）。
    """
    cfg = d.resolve_dedupe_cfg({"dedupe": {"simThreshold": 1}})
    # 归一化后 3-gram 集合**相同**（只由 aba/bab 构成），但前缀不同 ⇒ 只能走 similar 分支
    tgt, prev = "ababababababab", "bababababababa"
    assert d.norm_hf_text(tgt)[:cfg.min_chars] != d.norm_hf_text(prev)[:cfg.min_chars], "前缀必须不同"
    assert d.hf_jaccard(d.hf_char_grams(d.norm_hf_text(tgt)),
                        d.hf_char_grams(d.norm_hf_text(prev))) == 1.0, "相似度必须恰好 1.0"
    v = d.hf_repeat_verdict(tgt, [_hist(prev, 100)], cfg, 200)
    assert v is not None, "sim == threshold 属于命中"
    assert v.kind == "similar"


def test_prefix_outranks_similar_regardless_of_order():
    """同一批历史里 prefix 命中优先于 similar 命中（**不看谁先扫到**）。

    为什么重要：prefix 是"开头一字不差"这个更强证据，而 similar 只是"整体像"。
    若实现写成"第一条命中的算数"，结论就取决于 ``history`` 的排列 —— 而那个排列
    由上一次发言的先后决定，于是**同一条候选消息的判定会随群里的发言顺序漂移**，
    复现 Bug 时怎么都拼不出来。
    """
    body = "亲这款现在有现货的哦今天下单明天到要的话直接拍就行"
    similar_src = "您问的" + body                 # 整体像，但开头不同
    prefix_src = body[:12] + "可以拍下哦亲"        # 开头 12 字归一化后相同，尾巴不同
    hist = [_hist(similar_src, 200), _hist(prefix_src, 100)]     # similar 排在**前面**
    v = d.hf_repeat_verdict(body, hist, CFG, 300)
    assert v is not None
    assert v.kind == "prefix", "prefix 必须覆盖 similar（与扫描顺序无关）"
    assert v.prev_at == 100, "命中的应是那条 prefix 历史，而不是先扫到的 similar"


# ===== 5. 误杀防护 ①：短句豁免 =====


def test_short_phrase_exemption():
    """``minChars`` 豁免 —— "好的/收到" 不进判定。"""
    hist = [_hist("好的", 100), _hist("收到👌", 200)]
    assert d.hf_repeat_verdict("好的", hist, CFG, 300) is None
    assert d.hf_repeat_verdict("收到", hist, CFG, 300) is None
    # 反过来：历史里的短句也不参与（与闸/日报**两侧对称**）
    assert d.hf_repeat_verdict("好的好的好的好的好的", [_hist("好的", 100)], CFG, 300) is None


def test_short_phrase_exemption_is_independent_on_both_sides():
    """短句豁免**两侧各自成立**，不能靠另一侧兜住。

    为什么单独有一条（这条是被变异测试逼出来的）：上面那条用"短句 vs 短句"来测，
    但**历史侧**本来就会跳过不足 ``minChars`` 的条目 ⇒ 即使**文本侧**那道豁免
    写坏了（把 ``< minChars`` 写成 ``<= 0``），也永远走不到、测不出来。
    而它一旦失效，短的候选就会被拿去和长历史比相似度并可能判重 ——
    bot 连"好的"都不敢说，比重复更不像人。

    ⇒ 这里让**历史那侧够长**（12 字，正好压在豁免线上）、**候选那侧偏短**（11 字），
    且两者的 3-gram 相似度**够高**（0.9）。于是"放行"只可能来自文本侧那道豁免：
    豁免一旦失效，这条立刻变成命中。
    """
    prev = "这款现在有现货哦可以拍下"          # 归一化 12 字 ⇒ 恰好不触发历史侧豁免
    cand = prev[:-1]                          # 归一化 11 字 ⇒ 只可能被文本侧豁免挡下

    assert len(d.norm_hf_text(cand)) == CFG.min_chars - 1, "前提：候选短于豁免线"
    assert len(d.norm_hf_text(prev)) == CFG.min_chars, "前提：历史够长，历史侧不会跳过它"
    assert d.norm_hf_text(cand)[:CFG.min_chars] != d.norm_hf_text(prev)[:CFG.min_chars], "前缀不同"
    assert (d.hf_jaccard(d.hf_char_grams(d.norm_hf_text(cand)),
                         d.hf_char_grams(d.norm_hf_text(prev)))
            >= CFG.sim_threshold), "前提：相似度够高 —— 否则放行可能来自「不够像」而不是豁免"

    assert d.hf_repeat_verdict(cand, [_hist(prev, 100)], CFG, 300) is None, (
        "短的候选必须被文本侧豁免挡住")

    # 短句也不该被记进历史（两侧对称）
    d.note_recent_reply("a", "g", "好的好的好的好的好的", 100, CFG)
    assert d.hf_recent_count("a", "g") == 0


# ===== 6. 误杀防护 ②：数字差异 =====


def test_model_number_difference_is_not_a_repeat():
    """只差型号数字 ⇒ 不算重复（**保留数字**的意义）。"""
    a = "iPhone 15 128G 现在报价是4599元"
    b = "iPhone 16 128G 现在报价是4599元"
    assert d.hf_repeat_verdict(b, [_hist(a, 100)], CFG, 200) is None, "型号不同必须放行"


# ===== 7. 误杀防护 ③：窗口边界 =====


def test_window_boundary_is_inclusive():
    """窗口外不命中（6 小时前的旧话不算复读）。"""
    hist = [_hist("这款现在有现货哦，可以拍下", 1000)]
    assert d.hf_repeat_verdict("这款现在有现货哦，可以拍下", hist, CFG, 1000 + CFG.window_sec), (
        "窗口边界内仍命中（``>`` 不是 ``>=``）")
    assert d.hf_repeat_verdict("这款现在有现货哦，可以拍下", hist, CFG,
                               1001 + CFG.window_sec) is None, "超出 windowSec 必须放行"


# ===== 8. 闸：同群隔离 + 开关 =====


def test_check_isolates_groups_accounts_and_honours_switches():
    """只比同群；不同群/不同账号各自独立（含开关与 ``proactiveOnly``）。"""
    t0 = 10_000
    text = "这款现在有现货哦，可以拍下"
    d.note_recent_reply("acct", "groupA", text, t0, CFG)
    assert d.hf_recent_count("acct", "groupA") == 1
    assert d.hf_recent_count("acct", "groupB") == 0

    assert d.check_hf_repeat("acct", "groupA", text, CFG, t0 + 60) is not None, "同群必须命中"
    assert d.check_hf_repeat("acct", "groupB", text, CFG, t0 + 60) is None, "不同群不互比"
    assert d.check_hf_repeat("acct2", "groupA", text, CFG, t0 + 60) is None, "不同账号不互比"

    # 开关：enabled=False ⇒ 一律放行（热重载即生效）
    off = d.HfDedupeConfig(enabled=False, proactive_only=CFG.proactive_only,
                           window_sec=CFG.window_sec, sim_threshold=CFG.sim_threshold,
                           min_chars=CFG.min_chars, history_max=CFG.history_max)
    assert d.check_hf_repeat("acct", "groupA", text, off, t0 + 60) is None

    # proactiveOnly：非主动插话（被 @/引用叫到）不拦 —— 人家点名问你，装死更失礼
    assert d.check_hf_repeat("acct", "groupA", text, CFG, t0 + 60, proactive=False) is None
    assert d.check_hf_repeat("acct", "groupA", text, CFG, t0 + 60, proactive=True) is not None


# ===== 9. 记账：短句不进 + 双限裁剪 =====


def test_note_skips_short_and_trims_by_window_and_count():
    cfg = d.HfDedupeConfig(enabled=True, proactive_only=True, window_sec=CFG.window_sec,
                           sim_threshold=CFG.sim_threshold, min_chars=CFG.min_chars,
                           history_max=3)
    d.note_recent_reply("a", "g", "好的", 100, cfg)
    assert d.hf_recent_count("a", "g") == 0, '短句不进历史（否则"好的"会互相判重）'

    for i in range(6):
        d.note_recent_reply("a", "g", f"第{i}条不同内容的较长发言内容", 100 + i, cfg)
    assert d.hf_recent_count("a", "g") == 3, "historyMax 生效"

    # 窗口裁剪：推进到窗口外再记一条 ⇒ 旧的按窗口裁掉（只留新的）
    d.note_recent_reply("a", "g", "很久以后的一条完全不同发言", 100 + cfg.window_sec + 10, cfg)
    assert d.hf_recent_count("a", "g") == 1, "窗口外的历史必须被裁掉"


def test_note_keeps_newest_when_over_capacity():
    """超容量时丢的是**最旧**的（对齐 JS ``while (kept.length > max) kept.shift()``）。"""
    cfg = d.HfDedupeConfig(enabled=True, proactive_only=True, window_sec=10 ** 6,
                           sim_threshold=0.85, min_chars=12, history_max=2)
    d.note_recent_reply("a", "g", "第一条够长的发言内容甲甲甲", 100, cfg)
    d.note_recent_reply("a", "g", "第二条够长的发言内容乙乙乙", 101, cfg)
    d.note_recent_reply("a", "g", "第三条够长的发言内容丙丙丙", 102, cfg)
    assert d.hf_recent_count("a", "g") == 2
    # 第一条应已被丢掉：拿它去判不该命中
    assert d.check_hf_repeat("a", "g", "第一条够长的发言内容甲甲甲", cfg, 103) is None


# ===== 10. sweep 清理 =====


def test_prune_drops_expired_history():
    """sweep 清理过期历史（防长跑进程里内存无限增长）。"""
    cfg = CFG
    d.note_recent_reply("a", "g1", "这是一条够长的发言内容甲", 1000, cfg)
    d.note_recent_reply("a", "g2", "这是一条够长的发言内容乙", 1000, cfg)

    d.prune_recent_replies(1000 + cfg.window_sec, cfg)          # 边界内保留
    assert d.hf_recent_count("a", "g1") == 1

    d.prune_recent_replies(1001 + cfg.window_sec, cfg)
    assert d.hf_recent_count("a", "g1") == 0
    assert d.hf_recent_count("a", "g2") == 0
    assert d.tracked_groups() == 0, "空掉的键要被删掉，不留空壳"


# ===== 10b. 启动预热（B7，2026-10-02 修）=====
#
# ``_recent`` 只在内存里 ⇒ 不预热就等于"每次重启后 6 小时内复读闸不存在"。
# 这是本批最隐蔽的一条：**台账、日志、/heartflow status 全绿**（它们只看"发没发出去"），
# 唯一能看出异常的是群友 —— 而群友只会觉得"bot 怎么又说了一遍"。
# 所以这里不满足于"函数被调到"，要钉到"灌进去以后闸真能拦住那句"。


def _fake_outbound(monkeypatch, rows, *, record: list | None = None, raises=None):
    """把 ``repo_messages.list_outbound_texts`` 换成固定返回值。"""
    from plugin.data import repo_messages as M

    def _fn(account_id, since_sec, limit=200):
        if record is not None:
            record.append({"account_id": account_id, "since_sec": since_sec, "limit": limit})
        if raises is not None:
            raise raises
        return rows

    monkeypatch.setattr(M, "list_outbound_texts", _fn)


def test_preload_fills_the_gate_from_outbound_history(monkeypatch):
    """★ 预热完，同一句话**必须被闸拦下** —— 否则"预热了"只是个无效果的假象。"""
    now = 1_000_000
    said = "这款机器今天现货还有三台可以自提"
    _fake_outbound(monkeypatch, [{"gid": "gA", "content": said, "at_sec": now - 60}])

    assert d.load_hf_dedupe_history("acct1", now) == 1
    assert d.hf_recent_count("acct1", "gA") == 1

    cfg = d.resolve_dedupe_cfg(None)
    v = d.check_hf_repeat("acct1", "gA", said, cfg, now, proactive=True)
    assert v is not None, "预热进来的那句必须让复读闸生效（这正是预热的目的）"
    assert v.kind == "prefix" or v.sim >= cfg.sim_threshold


def test_preload_only_asks_for_the_gate_window(monkeypatch):
    """窗口与条数上限取自闸的配置 —— 灌得比窗口宽，等于让"过期历史"参与判定。"""
    now = 1_000_000
    seen: list[dict] = []
    _fake_outbound(monkeypatch, [], record=seen)

    cfg = d.resolve_dedupe_cfg(None)
    d.load_hf_dedupe_history("acct1", now)
    assert len(seen) == 1
    assert seen[0]["account_id"] == "acct1"
    assert seen[0]["since_sec"] == now - cfg.window_sec
    assert seen[0]["limit"] == d.HF_DEDUPE_PRELOAD_LIMIT


def test_preload_skips_dirty_rows_and_counts_only_good_ones(monkeypatch):
    """脏行（缺群 / 缺时刻 / 空文本 / ``at_sec`` 不是数）不许进历史，也不计入返回值。

    返回值是"灌进去多少条"，巡检靠它判断预热到底有没有拿到东西 ——
    把脏行算进去就会让"实际一条都没灌进去"报成一个正数。
    """
    now = 1_000_000
    good = "今天下午三点前来店里可以走活动价"
    _fake_outbound(monkeypatch, [
        {"gid": "gA", "content": good, "at_sec": now - 30},
        {"gid": "", "content": good, "at_sec": now - 30},          # 缺群 ⇒ 归不到桶
        {"gid": "gA", "content": "", "at_sec": now - 30},          # 空文本
        {"gid": "gA", "content": good, "at_sec": None},            # 缺时刻
        {"gid": "gA", "content": good, "at_sec": "不是数"},         # 脏时刻
        {"gid": "gA", "content": good, "at_sec": 0},               # 时刻 0（prune 会即时清掉）
        {"gid": "gB", "content": good, "at_sec": now - 30},        # 另一群，正常
    ])

    assert d.load_hf_dedupe_history("acct1", now) == 2, "只有两条真进了历史"
    assert d.hf_recent_count("acct1", "gA") == 1
    assert d.hf_recent_count("acct1", "gB") == 1
    assert d.tracked_groups() == 2, "脏行不该凭空建桶"


def test_preload_degrades_loudly_when_the_query_fails(monkeypatch, caplog):
    """查询炸了 ⇒ 返回 0 + **吵** + 不抛（预热失败不是账号接入的阻塞点）。

    必须吵：这一条静默的话，"复读闸这 6 小时等于没有"就只体现在群友的体感里。
    """
    import logging

    _fake_outbound(monkeypatch, [], raises=RuntimeError("表被锁"))
    with caplog.at_level(logging.WARNING, logger=d.__name__):
        assert d.load_hf_dedupe_history("acct1", 1_000_000) == 0
    assert any("复读历史预热失败" in r.message for r in caplog.records), \
        "预热失败必须吵（否则复读闸空转只有群友看得出来）"
    assert d.tracked_groups() == 0


def test_preload_does_not_even_query_when_the_gate_is_off(monkeypatch):
    """闸关掉 ⇒ 连查都不查（拿缺省策略判的：预热发生在账号启动，真配置还没进 sweep 手里）。"""
    seen: list[dict] = []
    _fake_outbound(monkeypatch, [{"gid": "gA", "content": "x" * 40, "at_sec": 1}],
                   record=seen)
    monkeypatch.setattr(d, "resolve_dedupe_cfg",
                        lambda cfg=None: d.HfDedupeConfig(
                            enabled=False, proactive_only=True,
                            window_sec=CFG.window_sec, sim_threshold=CFG.sim_threshold,
                            min_chars=CFG.min_chars, history_max=CFG.history_max))
    assert d.load_hf_dedupe_history("acct1", 1_000_000) == 0
    assert seen == [], "闸关着就不该打这次 DB"
    assert d.tracked_groups() == 0


# ===== 11. 参数解析 =====


def test_resolve_defaults_and_fallbacks():
    r = d.resolve_dedupe_cfg(None)
    assert r.as_dict() == d.HF_DEDUPE_DEFAULTS
    assert r.window_sec == 21600, "6 小时窗（老板拍板）"
    assert r.sim_threshold == 0.85, "相似阈值 0.85（老板拍板）"
    assert r.proactive_only is True, "只对心流主动插话生效"

    # 非法：相似度越界 ⇒ 回落；窗口/豁免 ≤0 ⇒ 回落；historyMax 钳到 500
    bad = d.resolve_dedupe_cfg({"dedupe": {"simThreshold": 1.5, "windowSec": 0,
                                           "minChars": -3, "historyMax": 9999}})
    assert bad.sim_threshold == 0.85
    assert bad.window_sec == 21600
    assert bad.min_chars == 12
    assert bad.history_max == d.HF_HISTORY_MAX_CAP, "historyMax 必须被钳到上限"

    for v in (0, -0.2, 1.0001, float("nan"), float("inf")):
        assert d.resolve_dedupe_cfg({"dedupe": {"simThreshold": v}}).sim_threshold == 0.85, (
            f"simThreshold={v!r} 必须回落默认（否则闸会静默变成「永远不拦」）")


def test_resolve_threshold_one_is_legal():
    """``simThreshold = 1`` 是**合法**的（只拦完全相同）—— 别把它和越界一起回落掉。"""
    assert d.resolve_dedupe_cfg({"dedupe": {"simThreshold": 1}}).sim_threshold == 1.0


# ===== 15. 无环 + 零分词依赖 =====


def test_no_cycle_and_no_tokenizer_dependency():
    """本模块不 import ``.learn``（反向依赖会成环；有源级守卫）。

    只看 ``import`` 行 —— 头注释里提到 ``jargon`` 是在解释"为什么不用它"，
    不该被自己的守卫误判。
    """
    src = (Path(__file__).resolve().parents[3]
           / "plugin" / "inbound" / "heartflow" / "dedupe.py").read_text(encoding="utf-8")
    import_lines = "\n".join(l for l in src.splitlines() if re.match(r"\s*(from|import)\b", l))
    assert not re.search(r"\.learn\b", import_lines), "不得 import .learn（成环）"
    assert not re.search(r"jieba|jargon", import_lines), (
        "不得依赖分词（ngramTokens 未导出，且 tokenize 会把短句滤空 ⇒ 相似度恒 0 静默失效）")


def test_dedupe_cache_is_registered():
    from plugin.inbound.heartflow.runtime import CACHE_REGISTRY

    assert "dedupe" in CACHE_REGISTRY.names()


# ===== 12 / 13 / 14 / 16. 源级接线 =====
#
# TS 那四条断言的主语（``heartflow-observe.ts`` 的重复率、``heartflow-learn.ts`` 的
# suppressed 分支、``dispatcher.ts`` 的 deliver 回调、``heartflow.ts`` + plugin 清单）
# 在 Py 侧分别落到 ``observe.py`` / ``learn.py`` / ``adapter`` 的出站路径 / ``heartflow.py``
# + ``plugin.yaml`` —— 它们**尚未移植**。
#
# **故意不在这里放"永远 skip"的壳**：跳过看起来像通过，比没有更糟。
# 等对应模块落地时，按同一意图（I4 共用判定 / suppressed 不占额度不开窗不进历史 /
# 闸在真发之前 / 配置不进 UI schema）补测，并加进 ``tools/mutation_check_*.py``。
